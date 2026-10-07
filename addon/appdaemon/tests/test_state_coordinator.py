from collections import deque
from dataclasses import replace

import pytest
from state_agent import (
    FeederTruth,
    RevisionSnapshot,
    SettingClass,
    StateAgentUnavailable,
)
from state_coordinator import (
    CommandReceipt,
    FeederState,
    FeederStateCoordinator,
    MqttAck,
    PendingStage,
    PersistentWriteRequest,
    PlanCollectionPredicate,
    PlanIdMapping,
    PlanIdNormalizationPredicate,
    PlanIdNormalizationRefused,
    PlanOperation,
    PlanPatch,
    SettingEqualsPredicate,
    build_deleted_plan_collection,
    build_plan_id_normalization,
    build_patched_plan_collection,
    validate_plan_transport_fields,
)
from test_state_agent import core_payload


class FakeLogger:
    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def log(message, **fields):
            self.records.append((level, message, fields))

        return log


class FakeAD:
    def __init__(self):
        self.timers = []

    def submit_to_executor(self, function, *args, callback):
        callback(result=function(*args))
        return object()

    def run_in(self, callback, delay, **kwargs):
        handle = object()
        self.timers.append([handle, callback, delay, kwargs, False])
        return handle

    def cancel_timer(self, handle, _silent):
        for timer in self.timers:
            if timer[0] is handle:
                timer[4] = True

    def run_next_timer(self):
        while self.timers:
            _handle, callback, _delay, kwargs, cancelled = self.timers.pop(0)
            if not cancelled:
                callback(kwargs)
                return
        raise AssertionError("no timer available")


class FakeAgent:
    def __init__(self, cores):
        self.cores = deque(cores)
        self.core_calls = 0
        self.core_raw_calls = []
        self.revision_calls = 0
        self.revision_value = None

    def core(self, *, raw=False):
        self.core_calls += 1
        self.core_raw_calls.append(raw)
        value = self.cores.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    def revisions(self):
        self.revision_calls += 1
        return self.revision_value


def truth(**kwargs):
    return FeederTruth.from_dict(core_payload(**kwargs))


def truth_with_plan_ids(*plan_ids):
    current = truth()
    template = replace(
        current.plans.semantic_records[0],
        opaque_hex="00" * 10,
    )
    plans = tuple(
        replace(
            template,
            id=plan_id,
            hour_utc=(10 + index) % 24,
            time_utc=f"{(10 + index) % 24:02d}:00",
            time_local_candidate=f"{(6 + index) % 24:02d}:00",
            portions=3 + index,
            sync_time=template.sync_time + index,
        )
        for index, plan_id in enumerate(plan_ids)
    )
    return replace(
        current,
        plans=replace(current.plans, count=len(plans), semantic_records=plans),
    )


def ready_coordinator(agent=None):
    ad = FakeAD()
    agent = agent or FakeAgent([truth()])
    logger = FakeLogger()
    mirrored = []
    availability = []
    coordinator = FeederStateCoordinator(
        ad,
        agent,
        logger,
        mirrored.append,
        availability.append,
    )
    coordinator.on_feeder_connected()
    assert coordinator.state == FeederState.READY
    return coordinator, ad, agent, logger, mirrored, availability


def test_plan_id_normalization_maps_opaque_ids_in_record_order_to_free_slots():
    current = truth_with_plan_ids(1_648_218, 3, 9_900_001)
    baseline = current.plans.semantic_records

    result = build_plan_id_normalization(baseline)

    assert result.mapping == (
        PlanIdMapping(record_index=0, source_id=1_648_218, target_id=1),
        PlanIdMapping(record_index=2, source_id=9_900_001, target_id=2),
    )
    assert tuple(plan.id for plan in result.normalized) == (1, 3, 2)
    assert result.normalized[1] is baseline[1]
    for mapping in result.mapping:
        before = baseline[mapping.record_index]
        after = result.normalized[mapping.record_index]
        assert replace(before, id=after.id, sync_time=after.sync_time) == after
        assert after.sync_time > before.sync_time


def test_plan_id_normalization_is_noop_for_already_canonical_collection():
    # No rewrite means an otherwise unrepresentable opaque tail is never touched.
    plans = truth().plans.semantic_records

    result = build_plan_id_normalization(plans)

    assert result.mapping == ()
    assert result.normalized is plans


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: replace(plan, opaque_hex="01" + "00" * 9), "opaque tail"),
        (lambda plan: replace(plan, one_shot_raw=2), "one_shot_raw"),
        (
            lambda plan: replace(
                plan, days_raw=tuple(reversed(plan.days_raw))
            ),
            "weekday ordering",
        ),
        (lambda plan: replace(plan, time_utc="09:59"), "UTC time"),
        (
            lambda plan: replace(plan, execution_state=1),
            "non-idle execution_state",
        ),
        (
            lambda plan: replace(plan, sync_time=0xFFFFFFFFFFFFFFFF),
            "sync_time cannot be advanced",
        ),
    ],
)
def test_plan_id_normalization_refuses_collections_that_cannot_round_trip(
    mutate, message
):
    current = truth_with_plan_ids(1_648_218)
    plans = (mutate(current.plans.semantic_records[0]),)

    with pytest.raises(PlanIdNormalizationRefused, match=message) as error:
        build_plan_id_normalization(plans)

    assert error.value.mapping == (
        PlanIdMapping(record_index=0, source_id=1_648_218, target_id=1),
    )


def test_plan_id_normalization_refuses_more_than_nine_records():
    plans = truth_with_plan_ids(*range(100, 110)).plans.semantic_records

    with pytest.raises(PlanIdNormalizationRefused, match="more than nine"):
        build_plan_id_normalization(plans)


def test_plan_id_normalization_verification_allows_only_runtime_readback_changes():
    current = truth_with_plan_ids(1_648_218)
    normalization = build_plan_id_normalization(current.plans.semantic_records)
    predicate = PlanIdNormalizationPredicate(normalization)
    expected = normalization.normalized[0]
    runtime_changed = replace(
        expected,
        execution_state=7,
        sync_time=expected.sync_time + 100,
        time_local_candidate="05:00",
    )
    matching = replace(
        current,
        plans=replace(
            current.plans,
            count=1,
            semantic_records=(runtime_changed,),
        ),
    )

    assert predicate.matches(matching)
    assert not predicate.matches(
        replace(
            matching,
            plans=replace(
                matching.plans,
                semantic_records=(replace(runtime_changed, portions=99),),
            ),
        )
    )


def test_reconciliation_normalizes_ids_through_serialized_verified_write():
    initial = truth_with_plan_ids(1_648_218)
    agent = FakeAgent([initial])
    ad = FakeAD()
    logger = FakeLogger()
    mirrored = []
    availability = []
    sent = []

    coordinator = FeederStateCoordinator(
        ad,
        agent,
        logger,
        mirrored.append,
        availability.append,
        plan_collection_publisher=lambda plans: (
            sent.append(plans)
            or CommandReceipt("FEEDING_PLAN_SERVICE", "normalize-message")
        ),
    )
    coordinator.on_feeder_connected()

    assert coordinator.state == FeederState.PENDING_WRITE
    assert coordinator.pending_write.request.control == "food.plan_id_normalization"
    assert coordinator.pending_write.request.target == (
        PlanIdMapping(record_index=0, source_id=1_648_218, target_id=1),
    )
    assert tuple(plan.id for plan in sent[0]) == (1,)
    mapping_log = next(
        fields["mapping"]
        for _level, message, fields in logger.records
        if message == "automatic feeding-plan ID normalization pending"
    )
    assert mapping_log == [
        {"record_index": 0, "source_id": 1_648_218, "target_id": 1}
    ]

    verified = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:normalized"),
        plans=replace(initial.plans, semantic_records=sent[0]),
    )
    agent.cores.append(verified)
    coordinator.on_mqtt_ack(
        MqttAck("FEEDING_PLAN_SERVICE", "normalize-message", True)
    )
    ad.run_next_timer()

    assert coordinator.state == FeederState.READY
    assert coordinator.latest_truth().plans.by_id(1) is not None
    assert mirrored[-1].plans.by_id(1) is not None
    assert availability[-1] is True


def test_failed_plan_id_normalization_does_not_retry_indefinitely():
    initial = truth_with_plan_ids(1_648_218)
    unchanged = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:unchanged"),
    )
    agent = FakeAgent([initial, unchanged, unchanged, unchanged, unchanged])
    ad = FakeAD()
    sent = []
    coordinator = FeederStateCoordinator(
        ad,
        agent,
        FakeLogger(),
        lambda _truth: None,
        lambda _available: None,
        plan_collection_publisher=lambda plans: (
            sent.append(plans)
            or CommandReceipt("FEEDING_PLAN_SERVICE", "normalize-message")
        ),
    )
    coordinator.on_feeder_connected()
    coordinator.on_mqtt_ack(
        MqttAck("FEEDING_PLAN_SERVICE", "normalize-message", True)
    )

    for _ in range(4):
        ad.run_next_timer()

    assert coordinator.state == FeederState.READY
    assert coordinator.pending_write is None
    assert len(sent) == 1
    assert coordinator.latest_truth().plans.by_id(1_648_218) is not None


def test_reconciliation_refuses_unsafe_plan_normalization_without_writing():
    initial = truth()
    unsafe = replace(
        initial,
        plans=replace(
            initial.plans,
            semantic_records=(
                replace(initial.plans.semantic_records[0], id=1_648_218),
            ),
        ),
    )
    sent = []
    logger = FakeLogger()
    coordinator = FeederStateCoordinator(
        FakeAD(),
        FakeAgent([unsafe]),
        logger,
        lambda _truth: None,
        lambda _available: None,
        plan_collection_publisher=lambda plans: sent.append(plans),
    )

    coordinator.on_feeder_connected()

    assert coordinator.state == FeederState.READY
    assert coordinator.latest_truth().plans.by_id(1_648_218) is not None
    assert sent == []
    refusal = next(
        fields
        for _level, message, fields in logger.records
        if message == "automatic feeding-plan ID normalization refused"
    )
    assert "opaque tail" in refusal["reason"]
    assert refusal["mapping"] == [
        {"record_index": 0, "source_id": 1_648_218, "target_id": 1}
    ]


def test_fresh_plan_snapshot_triggers_future_oem_id_normalization_after_callback():
    initial = truth()
    future_oem_truth = truth_with_plan_ids(8_765_432)
    future_oem_truth = replace(
        future_oem_truth,
        revisions=replace(
            future_oem_truth.revisions,
            core_rev="fnv64:future-oem-plan",
        ),
    )
    agent = FakeAgent([initial, future_oem_truth])
    events = []
    coordinator = FeederStateCoordinator(
        FakeAD(),
        agent,
        FakeLogger(),
        lambda _truth: None,
        lambda _available: None,
        plan_collection_publisher=lambda plans: (
            events.append(("normalization", plans))
            or CommandReceipt("FEEDING_PLAN_SERVICE", "normalization-message")
        ),
    )
    coordinator.on_feeder_connected()

    coordinator.request_plan_snapshot(
        lambda plans: events.append(("snapshot", plans))
    )

    assert events[0][0] == "snapshot"
    assert events[0][1][0].id == 8_765_432
    assert events[1][0] == "normalization"
    assert events[1][1][0].id == 1
    assert coordinator.state == FeederState.PENDING_WRITE


def test_startup_reconciles_before_ready_and_mirrors_feeder_truth():
    coordinator, _ad, agent, _logger, mirrored, availability = ready_coordinator()

    assert agent.core_calls == 1
    assert mirrored[-1].plans.by_id(1).hour_utc == 11
    assert availability[-2:] == [False, True]
    assert coordinator.latest_revisions().core_rev == "fnv64:core"


def test_duplicate_startup_signal_does_not_repeat_reconciliation():
    coordinator, _ad, agent, _logger, _mirrored, _availability = ready_coordinator()

    coordinator.on_feeder_connected()

    assert agent.core_calls == 1


def test_truth_projection_failure_stays_out_of_ready_without_framework_error():
    availability = []
    coordinator = FeederStateCoordinator(
        FakeAD(),
        FakeAgent([truth()]),
        FakeLogger(),
        lambda _truth: (_ for _ in ()).throw(RuntimeError("projection failed")),
        availability.append,
    )

    coordinator.on_feeder_connected()

    assert coordinator.state == FeederState.DISCONNECTED
    assert coordinator.latest_truth() is not None
    assert availability[-1] is False


def test_setting_write_ack_then_core_verification_updates_truth():
    first = truth()
    second = replace(
        first,
        settings=replace(
            first.settings, values={**first.settings.values, "volume": 80}
        ),
        revisions=replace(first.revisions, core_rev="fnv64:core-2"),
    )
    coordinator, ad, _agent, _logger, mirrored, _availability = ready_coordinator(
        FakeAgent([first, second])
    )
    sent = []
    request = PersistentWriteRequest(
        control="sound.volume",
        target=80,
        publisher=lambda _truth: (
            sent.append(80) or CommandReceipt("ATTR_SET_SERVICE", "message-1")
        ),
        predicate=SettingEqualsPredicate("volume", 80),
    )

    assert coordinator.request_persistent_write(request)
    assert sent == [80]
    assert coordinator.pending_write.stage == PendingStage.AWAITING_ACK
    coordinator.on_mqtt_ack(MqttAck("ATTR_SET_SERVICE", "message-1", True))
    assert coordinator.state == FeederState.VERIFYING_WRITE
    assert coordinator.latest_truth() is first
    assert mirrored[-1].settings["volume"] != 80
    ad.run_next_timer()

    assert coordinator.state == FeederState.READY
    assert coordinator.latest_truth().settings["volume"] == 80
    assert mirrored[-1].settings["volume"] == 80


def test_api_unavailable_blocks_persistent_writes():
    ad = FakeAD()
    agent = FakeAgent([StateAgentUnavailable("offline")])
    coordinator = FeederStateCoordinator(
        ad, agent, FakeLogger(), lambda _truth: None, lambda _available: None
    )
    coordinator.on_feeder_connected()

    called = []
    accepted = coordinator.request_persistent_write(
        PersistentWriteRequest(
            control="camera.resolution",
            target="1080p",
            publisher=lambda _truth: (
                called.append(True) or CommandReceipt("ATTR_SET_SERVICE", "message")
            ),
            predicate=SettingEqualsPredicate("camera_resolution", "1080p"),
        )
    )
    assert not accepted
    assert called == []
    assert coordinator.state == FeederState.DISCONNECTED


def test_plan_preflight_uses_fresh_core_and_preserves_opaque_fields():
    initial = truth()
    preflight = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:preflight"),
    )
    verified_plans = build_patched_plan_collection(
        preflight.plans.semantic_records,
        PlanPatch(1, 12, 30, (1, 3, 5), 12),
    )
    verified = replace(
        preflight,
        revisions=replace(preflight.revisions, core_rev="fnv64:verified"),
        plans=replace(preflight.plans, semantic_records=verified_plans),
    )
    coordinator, ad, agent, _logger, _mirrored, _availability = ready_coordinator(
        FakeAgent([initial, preflight, verified])
    )
    sent = []
    patch = PlanPatch(1, 12, 30, (1, 3, 5), 12)
    request = PersistentWriteRequest(
        control="food.plan_1",
        target=patch,
        publisher=lambda current_truth: (
            sent.append(current_truth.plans.semantic_records)
            or CommandReceipt("FEEDING_PLAN_SERVICE", "plan-message")
        ),
        predicate=None,
        requires_fresh_preflight=True,
        plan_patch=patch,
    )

    assert coordinator.request_persistent_write(request)
    assert agent.core_calls == 2
    assert len(sent) == 1
    emitted = sent[0][0]
    assert emitted.hour_utc == 12
    assert emitted.minute == 30
    assert emitted.days_raw == (1, 3, 5)
    assert emitted.portions == 12
    assert emitted.enable_audio_raw == 1
    assert emitted.audio_times == 2

    initial_plan = initial.plans.semantic_records[0]
    assert emitted.skip_end_time == initial_plan.skip_end_time
    assert emitted.opaque_hex == initial_plan.opaque_hex

    coordinator.on_mqtt_ack(MqttAck("FEEDING_PLAN_SERVICE", "plan-message", True))
    ad.run_next_timer()
    assert coordinator.state == FeederState.READY
    assert coordinator.latest_revisions().core_rev == "fnv64:verified"


def test_plan_predicate_rejects_collateral_mutation_and_missing_plan():
    current = truth()
    first = current.plans.semantic_records[0]
    second = replace(
        first,
        id=2,
        hour_utc=19,
        time_utc="19:00",
        time_local_candidate="15:00",
        portions=8,
    )
    baseline = (first, second)
    expected = build_patched_plan_collection(
        baseline, PlanPatch(1, 12, 30, (1, 3, 5), 12)
    )
    predicate = PlanCollectionPredicate(
        baseline,
        expected,
        1,
        PlanOperation.UPDATE,
    )

    matching = replace(
        current,
        plans=replace(current.plans, count=2, semantic_records=expected),
    )
    assert predicate.matches(matching)

    mutated = (expected[0], replace(expected[1], portions=9))
    collateral = replace(
        current,
        plans=replace(current.plans, count=2, semantic_records=mutated),
    )
    assert not predicate.matches(collateral)

    opaque_mutation = replace(
        current,
        plans=replace(
            current.plans,
            count=2,
            semantic_records=(
                replace(expected[0], audio_times=3),
                expected[1],
            ),
        ),
    )
    assert not predicate.matches(opaque_mutation)

    missing = replace(
        current,
        plans=replace(current.plans, count=1, semantic_records=(expected[0],)),
    )
    assert not predicate.matches(missing)


def test_plan_delete_predicate_requires_only_target_to_disappear():
    current = truth()
    first = current.plans.semantic_records[0]
    second = replace(
        first,
        id=2,
        hour_utc=19,
        time_utc="19:00",
        time_local_candidate="15:00",
        portions=8,
    )
    baseline = (first, second)
    expected = build_deleted_plan_collection(baseline, 2)
    predicate = PlanCollectionPredicate(
        baseline=baseline,
        expected=expected,
        target_plan_id=2,
        operation=PlanOperation.DELETE,
    )

    matching = replace(
        current,
        plans=replace(current.plans, count=1, semantic_records=expected),
    )
    assert predicate.matches(matching)

    target_remains = replace(
        current,
        plans=replace(current.plans, count=2, semantic_records=baseline),
    )
    assert not predicate.matches(target_remains)

    collateral = replace(
        current,
        plans=replace(
            current.plans,
            count=1,
            semantic_records=(replace(first, portions=9),),
        ),
    )
    assert not predicate.matches(collateral)


def test_plan_delete_runs_through_preflight_ack_and_verified_readback():
    initial = truth()
    first = initial.plans.semantic_records[0]
    second = replace(
        first,
        id=2,
        hour_utc=19,
        time_utc="19:00",
        time_local_candidate="15:00",
        portions=8,
    )
    initial = replace(
        initial,
        plans=replace(initial.plans, count=2, semantic_records=(first, second)),
    )
    preflight = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:delete-preflight"),
    )
    verified = replace(
        preflight,
        revisions=replace(preflight.revisions, core_rev="fnv64:delete-verified"),
        plans=replace(preflight.plans, count=1, semantic_records=(first,)),
    )
    coordinator, ad, agent, _logger, _mirrored, _availability = ready_coordinator(
        FakeAgent([initial, preflight, verified])
    )
    sent = []
    request = PersistentWriteRequest(
        control="food.plan_2",
        target=2,
        publisher=lambda current_truth: (
            sent.append(current_truth.plans.semantic_records)
            or CommandReceipt("FEEDING_PLAN_SERVICE", "delete-message")
        ),
        predicate=None,
        requires_fresh_preflight=True,
        plan_delete_id=2,
    )

    assert coordinator.request_persistent_write(request)
    assert agent.core_calls == 2
    assert sent == [(first,)]

    coordinator.on_mqtt_ack(
        MqttAck("FEEDING_PLAN_SERVICE", "delete-message", True)
    )
    ad.run_next_timer()

    assert coordinator.state == FeederState.READY
    assert coordinator.latest_revisions().core_rev == "fnv64:delete-verified"


def test_plan_delete_can_remove_last_plan_and_rejects_missing_target():
    current = truth()
    only_plan = current.plans.semantic_records[0]

    assert build_deleted_plan_collection((only_plan,), only_plan.id) == ()
    with pytest.raises(ValueError, match="does not exist"):
        build_deleted_plan_collection((only_plan,), 2)


def test_plan_predicate_allows_first_plan_creation():
    patch = PlanPatch(1, 7, 0, (1, 2, 3), 10)

    expected = build_patched_plan_collection(
        (),
        patch,
    )

    predicate = PlanCollectionPredicate(
        baseline=(),
        expected=expected,
        target_plan_id=1,
        operation=PlanOperation.CREATE,
    )

    matching = truth()

    firmware_created_plan = replace(
        expected[0],
        opaque_hex="00000000000000000000",
    )

    matching = replace(
        matching,
        plans=replace(
            matching.plans,
            count=1,
            semantic_records=(firmware_created_plan,),
        ),
    )

    assert predicate.matches(matching)


def test_plan_predicate_allows_additional_plan_without_mutating_existing():
    current = truth()
    existing = current.plans.semantic_records

    patch = PlanPatch(2, 18, 0, (1, 3, 5), 8)

    expected = build_patched_plan_collection(
        existing,
        patch,
    )

    predicate = PlanCollectionPredicate(
        baseline=existing,
        expected=expected,
        target_plan_id=2,
        operation=PlanOperation.CREATE,
    )

    firmware_created = tuple(
        replace(plan, opaque_hex="00000000000000000000")
        if plan.id == patch.plan_id
        else plan
        for plan in expected
    )

    matching = replace(
        current,
        plans=replace(
            current.plans,
            count=len(expected),
            semantic_records=firmware_created,
        ),
    )

    assert predicate.matches(matching)


def test_plan_predicate_rejects_creation_that_removes_existing_plan():
    current = truth()
    patch = PlanPatch(2, 18, 0, (1,), 8)

    expected = build_patched_plan_collection(
        current.plans.semantic_records,
        patch,
    )

    predicate = PlanCollectionPredicate(
        baseline=current.plans.semantic_records,
        expected=expected,
        target_plan_id=2,
        operation=PlanOperation.CREATE,
    )

    missing_existing = replace(
        current,
        plans=replace(
            current.plans,
            count=1,
            semantic_records=(expected[-1],),
        ),
    )

    assert not predicate.matches(missing_existing)


def test_plan_predicate_rejects_create_when_semantic_fields_do_not_match():
    patch = PlanPatch(1, 7, 0, (1, 2, 3), 10)

    expected = build_patched_plan_collection(
        (),
        patch,
    )

    predicate = PlanCollectionPredicate(
        baseline=(),
        expected=expected,
        target_plan_id=1,
        operation=PlanOperation.CREATE,
    )

    corrupted = replace(
        expected[0],
        portions=11,
        opaque_hex="00000000000000000000",
    )

    actual = replace(
        truth(),
        plans=replace(
            truth().plans,
            count=1,
            semantic_records=(corrupted,),
        ),
    )

    assert not predicate.matches(actual)


def test_plan_preflight_failure_does_not_publish_mqtt_command():
    initial = truth()
    coordinator, _ad, _agent, _logger, _mirrored, _availability = ready_coordinator(
        FakeAgent([initial, StateAgentUnavailable("offline")])
    )
    sent = []
    request = PersistentWriteRequest(
        control="food.plan_1",
        target=PlanPatch(1, 12, 0, (1,), 10),
        publisher=lambda _truth: (
            sent.append(True) or CommandReceipt("FEEDING_PLAN_SERVICE", "plan-message")
        ),
        predicate=None,
        requires_fresh_preflight=True,
        plan_patch=PlanPatch(1, 12, 0, (1,), 10),
    )

    assert coordinator.request_persistent_write(request)

    assert sent == []
    assert coordinator.pending_write is None
    assert coordinator.state == FeederState.DISCONNECTED


def test_feeder_plan_request_uses_fresh_core_not_cached_truth():
    initial = truth()
    changed_plan = replace(initial.plans.semantic_records[0], portions=14)
    fresh = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:fresh"),
        plans=replace(initial.plans, semantic_records=(changed_plan,)),
    )
    coordinator, _ad, agent, _logger, _mirrored, _availability = ready_coordinator(
        FakeAgent([initial, fresh])
    )
    results = []

    coordinator.request_plan_snapshot(results.append)

    assert agent.core_calls == 2
    assert results[0][0].portions == 14
    assert coordinator.latest_revisions().core_rev == "fnv64:fresh"


def test_enable_audio_raw_other_than_zero_or_one_is_rejected():
    plans = truth(enable_audio_raw=2).plans.semantic_records
    try:
        validate_plan_transport_fields(plans)
    except ValueError as error:
        assert "enable_audio_raw" in str(error)
    else:
        raise AssertionError("unsupported enable_audio_raw was accepted")


def test_plan_write_coalesces_only_while_preparing():
    coordinator, _ad, _agent, _logger, _mirrored, _availability = ready_coordinator()
    coordinator._operation_token = "busy"
    first = PersistentWriteRequest(
        control="food.plan_1",
        target=PlanPatch(1, 12, 0, (1,), 10),
        publisher=lambda _truth: CommandReceipt("FEEDING_PLAN_SERVICE", "one"),
        predicate=None,
        requires_fresh_preflight=True,
        plan_patch=PlanPatch(1, 12, 0, (1,), 10),
    )
    second = replace(
        first,
        target=PlanPatch(1, 13, 0, (1,), 10),
        plan_patch=PlanPatch(1, 13, 0, (1,), 10),
    )
    coordinator._start_write(first)
    assert coordinator.pending_write.stage == PendingStage.PREPARING
    assert coordinator.request_persistent_write(second)
    assert coordinator.pending_write.request.plan_patch.hour_utc == 13


def test_plan_preflight_runs_after_an_inflight_revision_read_completes():
    initial = truth()
    preflight = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:preflight"),
    )
    agent = FakeAgent([initial, preflight])
    coordinator, _ad, agent, _logger, _mirrored, _availability = ready_coordinator(
        agent
    )
    sent = []
    coordinator._operation_token = "revision-in-flight"
    request = PersistentWriteRequest(
        control="food.plan_1",
        target=PlanPatch(1, 12, 0, (1,), 10),
        publisher=lambda current: (
            sent.append(current)
            or CommandReceipt("FEEDING_PLAN_SERVICE", "plan-message")
        ),
        predicate=None,
        requires_fresh_preflight=True,
        plan_patch=PlanPatch(1, 12, 0, (1,), 10),
    )

    assert coordinator.request_persistent_write(request)
    assert sent == []
    assert coordinator.pending_write is None
    assert len(coordinator._queued_writes) == 1

    coordinator._operation_token = None
    coordinator._maybe_process_deferred()

    assert agent.core_calls == 2
    assert len(sent) == 1
    assert coordinator.pending_write.stage == PendingStage.AWAITING_ACK


def test_heartbeat_uses_revision_only_when_core_is_unchanged():
    initial = truth()
    agent = FakeAgent([initial])
    agent.revision_value = RevisionSnapshot(
        read_ms=0,
        revisions=initial.revisions,
        queue=initial.queue,
    )
    coordinator, _ad, agent, _logger, mirrored, _availability = ready_coordinator(agent)

    coordinator.on_heartbeat()

    assert agent.revision_calls == 1
    assert agent.core_calls == 1
    assert len(mirrored) == 1


def test_persistent_push_hint_checks_agent_revisions_without_applying_event_values():
    initial = truth()
    agent = FakeAgent([initial])
    agent.revision_value = RevisionSnapshot(
        read_ms=0,
        revisions=initial.revisions,
        queue=initial.queue,
    )
    coordinator, _ad, agent, _logger, mirrored, _availability = ready_coordinator(agent)

    coordinator.on_persistent_state_hint()

    assert agent.revision_calls == 1
    assert agent.core_calls == 1
    assert coordinator.latest_truth() is initial
    assert mirrored == [initial]


def test_heartbeat_changed_revision_fetches_and_applies_core():
    initial = truth()
    changed = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:changed"),
        settings=replace(
            initial.settings, values={**initial.settings.values, "volume": 81}
        ),
    )
    agent = FakeAgent([initial, changed])
    agent.revision_value = RevisionSnapshot(
        read_ms=0,
        revisions=changed.revisions,
        queue=changed.queue,
    )
    coordinator, _ad, agent, _logger, mirrored, _availability = ready_coordinator(agent)

    coordinator.on_heartbeat()

    assert agent.revision_calls == 1
    assert agent.core_calls == 2
    assert mirrored[-1].settings["volume"] == 81


def test_ack_match_but_persisted_plan_mismatch_applies_feeder_truth():
    initial = truth()
    preflight = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:preflight"),
    )
    mismatch = replace(
        preflight,
        revisions=replace(preflight.revisions, core_rev="fnv64:mismatch"),
    )
    coordinator, ad, _agent, _logger, mirrored, _availability = ready_coordinator(
        FakeAgent([initial, preflight, mismatch, mismatch, mismatch, mismatch])
    )
    patch = PlanPatch(1, 12, 30, (1, 3, 5), 12)
    coordinator.request_persistent_write(
        PersistentWriteRequest(
            control="food.plan_1",
            target=patch,
            publisher=lambda _truth: CommandReceipt(
                "FEEDING_PLAN_SERVICE", "plan-message"
            ),
            predicate=None,
            requires_fresh_preflight=True,
            plan_patch=patch,
        )
    )
    coordinator.on_mqtt_ack(MqttAck("FEEDING_PLAN_SERVICE", "plan-message", True))
    for _ in range(4):
        ad.run_next_timer()

    assert coordinator.state == FeederState.READY
    assert coordinator.latest_revisions().core_rev == "fnv64:mismatch"
    assert mirrored[-1].plans.by_id(1).hour_utc == 11


def test_verification_api_failure_never_returns_coordinator_to_ready():
    initial = truth()
    mismatch = replace(
        initial,
        revisions=replace(initial.revisions, core_rev="fnv64:mismatch"),
    )
    agent = FakeAgent(
        [
            initial,
            mismatch,
            StateAgentUnavailable("offline"),
            StateAgentUnavailable("offline"),
            StateAgentUnavailable("offline"),
        ]
    )
    coordinator, ad, _agent, _logger, _mirrored, availability = ready_coordinator(agent)
    coordinator.request_persistent_write(
        PersistentWriteRequest(
            control="sound.volume",
            target=80,
            publisher=lambda _truth: CommandReceipt("ATTR_SET_SERVICE", "message-1"),
            predicate=SettingEqualsPredicate("volume", 80),
        )
    )
    coordinator.on_mqtt_ack(MqttAck("ATTR_SET_SERVICE", "message-1", True))
    for _ in range(4):
        ad.run_next_timer()

    assert coordinator.state == FeederState.DISCONNECTED
    assert not coordinator.state_api_healthy
    assert availability[-1] is False


def test_sound_verification_failure_logs_only_numeric_raw_setting_changes():
    initial = truth(
        settings_raw={
            "motor_state_u8_0x0e": 2,
            "sound_switch_u8_0x21": 0,
            "opaque_text": "before-sensitive-value",
        }
    )
    initial = replace(
        initial,
        settings=replace(
            initial.settings,
            values={**initial.settings.values, "sound_switch": "disabled"},
        ),
    )
    preflight = initial
    mismatch = replace(
        initial,
        settings_raw={
            "motor_state_u8_0x0e": 2,
            "sound_switch_u8_0x21": 2,
            "opaque_text": "after-sensitive-value",
        },
    )
    agent = FakeAgent([initial, preflight, mismatch, mismatch, mismatch, mismatch])
    coordinator, ad, agent, logger, _mirrored, _availability = ready_coordinator(agent)
    coordinator.request_persistent_write(
        PersistentWriteRequest(
            control="sound.enable",
            target="enabled",
            publisher=lambda _truth: CommandReceipt(
                "ATTR_SET_SERVICE", "sound-message"
            ),
            predicate=SettingEqualsPredicate("sound_switch", "enabled"),
            requires_fresh_preflight=True,
            raw_settings_diagnostics=True,
        )
    )
    coordinator.on_mqtt_ack(MqttAck("ATTR_SET_SERVICE", "sound-message", True))
    for _ in range(4):
        ad.run_next_timer()

    assert agent.core_raw_calls == [False, True, True, True, True, True]
    diagnostic = next(
        fields
        for _level, message, fields in logger.records
        if message == "persistent write raw settings diff"
    )
    assert diagnostic == {
        "control": "sound.enable",
        "changes": [
            {
                "field": "sound_switch_u8_0x21",
                "before": 0,
                "after": 2,
            }
        ],
    }
    assert "sensitive-value" not in repr(logger.records)


def test_sound_raw_diff_is_logged_when_persistent_switch_matches():
    initial = truth(settings_raw={"sound_switch_u8_0x21": 1})
    initial = replace(
        initial,
        settings=replace(
            initial.settings,
            values={
                **initial.settings.values,
                "sound_switch": "disabled",
            },
        ),
    )
    verified = replace(
        initial,
        settings_raw={"sound_switch_u8_0x21": 0},
    )
    agent = FakeAgent([initial, initial, verified])
    coordinator, ad, _agent, logger, _mirrored, _availability = ready_coordinator(agent)
    coordinator.request_persistent_write(
        PersistentWriteRequest(
            control="sound.enable",
            target="disabled",
            publisher=lambda _truth: CommandReceipt(
                "ATTR_SET_SERVICE", "sound-off-message"
            ),
            predicate=SettingEqualsPredicate("sound_switch", "disabled"),
            requires_fresh_preflight=True,
            raw_settings_diagnostics=True,
        )
    )
    coordinator.on_mqtt_ack(MqttAck("ATTR_SET_SERVICE", "sound-off-message", True))
    ad.run_next_timer()

    diagnostic = next(
        fields
        for _level, message, fields in logger.records
        if message == "persistent write raw settings diff"
    )
    assert diagnostic["changes"] == [
        {
            "field": "sound_switch_u8_0x21",
            "before": 1,
            "after": 0,
        }
    ]


def test_truth_application_guard_suppresses_nested_writeback():
    ad = FakeAD()
    agent = FakeAgent([truth()])
    logger = FakeLogger()
    attempted = []
    holder = {}

    def sink(_truth):
        request = PersistentWriteRequest(
            control="camera.resolution",
            target="1080p",
            publisher=lambda _current: (
                attempted.append(True) or CommandReceipt("ATTR_SET_SERVICE", "nested")
            ),
            predicate=SettingEqualsPredicate("camera_resolution", "1080p"),
        )
        assert not holder["coordinator"].request_persistent_write(request)

    coordinator = FeederStateCoordinator(
        ad, agent, logger, sink, lambda _available: None
    )
    holder["coordinator"] = coordinator
    coordinator.on_feeder_connected()

    assert attempted == []


def test_setting_predicate_supports_every_exposed_controllable_field():
    settings = {
        "bowl_mode": "dual_bowl",
        "sound_switch": "enabled",
        "volume": 76,
        "auto_change_mode": 1,
        "auto_threshold": 20,
        "feeding_audio_enabled": "disabled",
        "light_switch": "enabled",
        "button_lights_mode": "always_active",
        "sound_mode": "always_active",
        "camera_switch": "enabled",
        "camera_mode": "always_active",
        "camera_resolution": "1080p",
        "night_vision_mode": "off",
        "video_record_switch": "disabled",
        "local_camera_recording_type": "continuous",
        "local_recording_mode": "always_active",
        "feeding_video_recording_enable": "enabled",
        "record_scheduled_feedings": "disabled",
        "record_manual_feedings": "enabled",
        "before_feeding_plan_minutes": 2,
        "automatic_recording_minutes": 2,
        "after_manual_feeding_minutes": 1,
        "video_watermark_enable": "enabled",
        "motion_detection_switch": "disabled",
        "motion_detection_mode": "always_active",
        "motion_detection_sensitivity": "medium",
        "motion_detection_range": "small",
        "sound_detection_switch": "disabled",
        "sound_detection_mode": "always_active",
        "sound_detection_sensitivity": "low",
        "cloud_video_record_switch": "disabled",
    }
    current = truth()
    current = replace(
        current,
        settings=replace(
            current.settings,
            values=settings,
            classes={field: SettingClass.PERSISTENT for field in settings},
        ),
    )
    for field, expected in settings.items():
        assert SettingEqualsPredicate(field, expected).matches(current), field


def test_setting_predicate_never_verifies_effective_cached_or_runtime_fields():
    current = truth()
    current = replace(
        current,
        settings=replace(
            current.settings,
            values={"sound_effective_cached": "enabled", "motor_state_raw": 1},
            classes={
                "sound_effective_cached": SettingClass.EFFECTIVE_CACHED,
                "motor_state_raw": SettingClass.RUNTIME,
            },
        ),
    )

    assert not SettingEqualsPredicate("sound_effective_cached", "enabled").matches(
        current
    )
    assert not SettingEqualsPredicate("motor_state_raw", 1).matches(current)


def test_matching_persistent_switch_ignores_mismatching_effective_cache():
    current = truth()
    current = replace(
        current,
        settings=replace(
            current.settings,
            values={
                "sound_switch": "enabled",
                "sound_effective_cached": "disabled",
            },
            classes={
                "sound_switch": SettingClass.PERSISTENT,
                "sound_effective_cached": SettingClass.EFFECTIVE_CACHED,
            },
        ),
    )

    assert SettingEqualsPredicate("sound_switch", "enabled").matches(current)
    assert not SettingEqualsPredicate("sound_effective_cached", "disabled").matches(
        current
    )
