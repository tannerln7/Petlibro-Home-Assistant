"""Automatic reconciliation of release-managed feeder compatibility assets."""

from __future__ import annotations

from collections.abc import Callable

from state_agent import (
    CompatibilityStatus,
    StateAgentConflict,
    StateAgentTimeout,
    StateAgentUnavailable,
    StateAgentUnsupported,
)

RETRY_DELAYS_SECONDS = (30, 60, 120)
ACTIVATION_RETRY_SECONDS = 30


class CompatibilityCoordinator:
    """Run fixed State Agent compatibility operations outside AppDaemon's loop."""

    def __init__(
        self, ad, state, state_agent, logger,
        activate_if_safe: Callable[[str], bool] | None = None,
    ):
        self.ad = ad
        self.state = state
        self.state_agent = state_agent
        self.logger = logger
        self._busy = False
        self._pending = False
        self._retry_timer = None
        self._retry_attempt = 0
        self._latest: CompatibilityStatus | None = None
        self._activate_if_safe = activate_if_safe
        self._activation_timer = None
        self._stopped = False

    def start(self) -> None:
        self._stopped = False
        self._publish_unavailable("not_checked")

    def stop(self) -> None:
        self._stopped = True
        if self._retry_timer is not None:
            self.ad.cancel_timer(self._retry_timer, True)
            self._retry_timer = None
        self._cancel_activation()

    def on_usable_connection(self) -> None:
        self._reset_retry()
        self.request_reconcile(reason="connection")

    def on_state_agent_update_complete(self, version: str) -> None:
        self._reset_retry()
        self.request_reconcile(reason="state_agent_update", version=version)

    def on_runtime_snapshot(self) -> None:
        if self._activation_timer is not None:
            self._cancel_activation()
            self.request_reconcile(reason="activation_runtime")

    def on_home_assistant_birth(self) -> None:
        if self._latest is None:
            self._publish_unavailable("not_checked")
        else:
            self._publish(self._latest)
        self.request_reconcile(reason="ha_birth")

    def request_reconcile(self, *, reason: str, version: str | None = None) -> None:
        if self._stopped:
            return
        self.logger.debug(
            "feeder compatibility reconciliation requested",
            reason=reason,
            version=version or "current",
        )
        self._submit("reconcile", self._reconcile_release)

    def _reconcile_release(self) -> CompatibilityStatus:
        try:
            return self.state_agent.compatibility_reconcile()
        except StateAgentUnsupported:
            # State Agent 0.4.0-rc.1/rc.2 exposed the fixed operations before
            # the release-owned reconciliation endpoint was introduced.
            self.state_agent.compatibility_status()
            status = self.state_agent.compatibility_deploy()
            if not status.enabled:
                status = self.state_agent.compatibility_enable()
            return status

    def _submit(self, action: str, operation: Callable[[], CompatibilityStatus]) -> None:
        if self._busy:
            self._pending = self._pending or action == "reconcile"
            self.logger.debug(
                "feeder compatibility operation deferred while busy", action=action
            )
            return
        self._busy = True

        # AppDaemon does not invoke the callback when an executor worker raises.
        # Convert failures to results so this coordinator cannot remain busy.
        def captured_operation() -> CompatibilityStatus | Exception:
            try:
                return operation()
            except Exception as error:
                return error

        self.ad.submit_to_executor(
            captured_operation,
            callback=lambda result, **_kwargs: self._finished(
                action=action, result=result
            ),
        )

    def _finished(self, *, action: str, result: object) -> None:
        self._busy = False
        if self._stopped:
            return
        if isinstance(result, Exception):
            if isinstance(result, StateAgentUnsupported):
                self.logger.debug(
                    "automatic compatibility reconciliation requires a newer State Agent"
                )
                self._publish_unavailable("unsupported_agent")
            else:
                fields = {"action": action, "error_type": type(result).__name__}
                if isinstance(result, StateAgentConflict):
                    fields["reason"] = result.reason
                self.logger.warning(
                    "feeder compatibility reconciliation failed", **fields
                )
                self._publish_unavailable(f"{action}_failed")
                if self._retryable(result):
                    self._schedule_retry()
            self._run_pending()
            return
        if not isinstance(result, CompatibilityStatus):
            self.logger.warning(
                "feeder compatibility operation returned an invalid result",
                action=action,
            )
            self._publish_unavailable(f"{action}_invalid_result")
            self._run_pending()
            return
        previous = self._latest
        self._latest = result
        self._reset_retry()
        self._publish(result)
        log = self.logger.info if previous != result else self.logger.debug
        log(
            "feeder compatibility status updated",
            action=action,
            deployed=result.deployed,
            enabled=result.enabled,
            active=result.active,
            reboot_required=result.reboot_required,
            reason=result.last_reason,
        )
        self._activate_when_safe(result)
        self._run_pending()

    def _activate_when_safe(self, status: CompatibilityStatus) -> None:
        if self._activate_if_safe is None or not (
            status.supported and status.deployed and status.shim_current
            and status.startup_current and status.backup_available
            and status.enabled and not status.active and status.reboot_required
        ):
            self._cancel_activation()
            return
        try:
            requested = self._activate_if_safe(status.asset_version)
        except Exception as error:
            self.logger.warning(
                "compatibility activation could not request a safe reboot",
                error_type=type(error).__name__,
            )
            requested = False
        if requested:
            self._cancel_activation()
        elif self._activation_timer is None:
            self._activation_timer = self.ad.run_in(
                self._retry_activation, ACTIVATION_RETRY_SECONDS
            )

    def _retry_activation(self, _kwargs: dict[str, object]) -> None:
        self._activation_timer = None
        # Re-read release status and update exclusion before considering a
        # reboot; a cached pending result is not sufficient authority.
        self.request_reconcile(reason="activation")

    def _cancel_activation(self) -> None:
        if self._activation_timer is not None:
            self.ad.cancel_timer(self._activation_timer, True)
            self._activation_timer = None

    def _retryable(self, error: Exception) -> bool:
        if isinstance(error, StateAgentConflict):
            return error.reason == "state_agent_update_in_progress"
        return isinstance(error, (StateAgentTimeout, StateAgentUnavailable))

    def _schedule_retry(self) -> None:
        if (
            self._retry_timer is not None
            or self._retry_attempt >= len(RETRY_DELAYS_SECONDS)
        ):
            return
        delay = RETRY_DELAYS_SECONDS[self._retry_attempt]
        self._retry_attempt += 1
        self.logger.debug(
            "feeder compatibility reconciliation retry scheduled",
            delay_seconds=delay,
            attempt=self._retry_attempt,
        )
        self._retry_timer = self.ad.run_in(self._retry, delay)

    def _reset_retry(self) -> None:
        self._retry_attempt = 0
        if self._retry_timer is not None:
            self.ad.cancel_timer(self._retry_timer, True)
            self._retry_timer = None

    def _retry(self, _kwargs: dict[str, object]) -> None:
        self._retry_timer = None
        self.request_reconcile(reason="retry")

    def _run_pending(self) -> None:
        if not self._pending or self._busy:
            return
        self._pending = False
        self.request_reconcile(reason="deferred")

    def _publish(self, status: CompatibilityStatus) -> None:
        self.state.publish("compatibility/active", status.active, retain=True)

    def _publish_unavailable(self, reason: str) -> None:
        self.logger.debug("feeder compatibility status unavailable", reason=reason)
