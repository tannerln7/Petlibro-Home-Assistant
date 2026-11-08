"""Fresh runtime projection for the Home Assistant dispensing-status entity."""

from __future__ import annotations

from dataclasses import dataclass
import enum
import time
from typing import Protocol


class FoodOutputProgress(enum.Enum):
    IDLE = 0
    RUNNING = 1
    BLOCKED = 2
    ERROR = 3
    RECOVERING = 4


@dataclass(frozen=True)
class RuntimeSnapshotRequest:
    message_id: str
    connection_generation: int
    runtime_event_generation: int
    reason: str


@dataclass(frozen=True)
class RuntimeSnapshotResult:
    request: RuntimeSnapshotRequest
    motor_state: int | None = None
    failure_reason: str | None = None

    @property
    def successful(self) -> bool:
        return self.failure_reason is None


class StatePublisher(Protocol):
    def publish(self, topic: str, value: object, *, retain: bool = False) -> None: ...

    def clear_retained(self, topic: str) -> None: ...


class DispensingStatusProjector:
    """Own dispensing state, freshness, availability, and runtime ordering."""

    STATE_TOPIC = "food_output/progress"
    AVAILABILITY_TOPIC = "food_output/progress_available"
    MOTOR_STATE_MAPPING = {
        1: FoodOutputProgress.RUNNING,
        2: FoodOutputProgress.IDLE,
        3: FoodOutputProgress.RECOVERING,
    }

    def __init__(self, state: StatePublisher, logger):
        self.state = state
        self.logger = logger
        self.current_state: FoodOutputProgress | None = None
        self.available = False
        self.connection_generation: int | None = None
        self.runtime_event_generation = 0
        self._idle_confirmed_at: float | None = None
        self._retained_progress_tombstone_issued = False

    def initialize(self) -> None:
        # Progress is transient. Remove any retained value left by an older
        # development build before this process can publish live state.
        if not self._retained_progress_tombstone_issued:
            self.state.clear_retained(self.STATE_TOPIC)
            self._retained_progress_tombstone_issued = True
        self.mark_unavailable()

    def runtime_event_generation_get(self) -> int:
        return self.runtime_event_generation

    def on_feeder_connected(self, connection_generation: int) -> None:
        self.connection_generation = connection_generation
        self.mark_unavailable()

    def on_feeder_disconnected(self) -> None:
        self.connection_generation = None
        self.mark_unavailable()

    def mark_unavailable(self) -> None:
        self._idle_confirmed_at = None
        self.available = False
        self.state.publish(self.AVAILABILITY_TOPIC, False)

    def runtime_snapshot_started(self, request: RuntimeSnapshotRequest) -> None:
        if request.connection_generation != self.connection_generation:
            return
        self.mark_unavailable()

    def apply_runtime_snapshot(self, result: RuntimeSnapshotResult) -> bool:
        request = result.request
        if request.connection_generation != self.connection_generation:
            self.logger.debug(
                "ignored runtime snapshot from stale feeder connection",
                reason=request.reason,
            )
            return False
        if request.runtime_event_generation != self.runtime_event_generation:
            self.logger.debug(
                "ignored runtime snapshot superseded by grain event",
                reason=request.reason,
            )
            return False
        if not result.successful:
            self.logger.warning(
                "dispensing runtime snapshot unavailable",
                reason=result.failure_reason,
            )
            self.mark_unavailable()
            return False

        progress = self.MOTOR_STATE_MAPPING.get(result.motor_state)
        if progress is None:
            self.logger.warning(
                "unsupported feeder motor state",
                motor_state=result.motor_state,
            )
            self.mark_unavailable()
            return False

        self._publish(progress)
        self._idle_confirmed_at = (
            time.monotonic() if progress is FoodOutputProgress.IDLE else None
        )
        return True

    def idle_confirmed_recently(self, max_age_seconds: float) -> bool:
        """Only a fresh correlated motor snapshot authorizes automatic reboot."""
        return (
            self.available
            and self.current_state is FoodOutputProgress.IDLE
            and self._idle_confirmed_at is not None
            and 0 <= time.monotonic() - self._idle_confirmed_at <= max_age_seconds
        )

    def apply_grain_event(
        self,
        progress: FoodOutputProgress,
        connection_generation: int,
    ) -> bool:
        if connection_generation != self.connection_generation:
            self.logger.debug("ignored grain event from stale feeder connection")
            return False
        if progress not in (
            FoodOutputProgress.RUNNING,
            FoodOutputProgress.BLOCKED,
            FoodOutputProgress.IDLE,
        ):
            self.logger.warning("unsupported grain-event progress", progress=progress)
            return False

        self.runtime_event_generation += 1
        self._idle_confirmed_at = None
        self._publish(progress)
        return True

    def _publish(self, progress: FoodOutputProgress) -> None:
        self.current_state = progress
        self.state.publish(self.STATE_TOPIC, progress)
        self.available = True
        self.state.publish(self.AVAILABILITY_TOPIC, True)
