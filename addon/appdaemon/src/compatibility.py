"""Opt-in feeder compatibility asset deployment and status projection."""

from __future__ import annotations

from collections.abc import Callable

from state_agent import CompatibilityStatus


class CompatibilityCoordinator:
    """Run fixed State Agent compatibility operations outside AppDaemon's loop."""

    def __init__(self, ad, state, state_agent, logger):
        self.ad = ad
        self.state = state
        self.state_agent = state_agent
        self.logger = logger
        self._busy = False
        self._latest: CompatibilityStatus | None = None

    def start(self) -> None:
        self._publish_unavailable("not_checked")

    def stop(self) -> None:
        return None

    def on_usable_connection(self) -> None:
        self.request_refresh()

    def on_home_assistant_birth(self) -> None:
        if self._latest is None:
            self._publish_unavailable("not_checked")
        else:
            self._publish(self._latest)
        self.request_refresh()

    def request_refresh(self) -> None:
        self._submit("refresh", self.state_agent.compatibility_status)

    def request_deploy(self) -> None:
        self._submit("deploy", self.state_agent.compatibility_deploy)

    def request_enabled(self, enabled: bool) -> None:
        operation = (
            self.state_agent.compatibility_enable
            if enabled
            else self.state_agent.compatibility_disable
        )
        self._submit("enable" if enabled else "disable", operation)

    def _submit(self, action: str, operation: Callable[[], CompatibilityStatus]) -> None:
        if self._busy:
            self.logger.warning(
                "feeder compatibility operation ignored while busy", action=action
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
        if isinstance(result, Exception):
            self.logger.warning(
                "feeder compatibility operation failed",
                action=action,
                error_type=type(result).__name__,
            )
            self._publish_unavailable(f"{action}_failed")
            if action != "refresh":
                self.request_refresh()
            return
        if not isinstance(result, CompatibilityStatus):
            self.logger.warning(
                "feeder compatibility operation returned an invalid result",
                action=action,
            )
            self._publish_unavailable(f"{action}_invalid_result")
            return
        self._latest = result
        self._publish(result)
        self.logger.info(
            "feeder compatibility status updated",
            action=action,
            deployed=result.deployed,
            enabled=result.enabled,
            active=result.active,
            reboot_required=result.reboot_required,
            reason=result.last_reason,
        )

    def _publish(self, status: CompatibilityStatus) -> None:
        deployment = (
            "unsupported"
            if not status.supported
            else "deployed"
            if status.deployed
            else "not_deployed"
        )
        payload = {
            "asset_version": status.asset_version,
            "original_ping_path": status.original_ping_path,
            "supported": status.supported,
            "deployed": status.deployed,
            "enabled": status.enabled,
            "active": status.active,
            "reboot_required": status.reboot_required,
            "last_reason": status.last_reason,
        }
        self.state.publish("compatibility/status", payload, retain=True)
        self.state.publish("compatibility/deployment", deployment, retain=True)
        self.state.publish("compatibility/deployed", status.deployed, retain=True)
        self.state.publish("compatibility/ping_mitigation", status.enabled, retain=True)
        self.state.publish("compatibility/active", status.active, retain=True)
        self.state.publish(
            "compatibility/reboot_required", status.reboot_required, retain=True
        )
        self.state.publish("compatibility/last_reason", status.last_reason, retain=True)

    def _publish_unavailable(self, reason: str) -> None:
        self.state.publish(
            "compatibility/status",
            {"available": False, "last_reason": reason},
            retain=True,
        )
        self.state.publish("compatibility/deployment", "unknown", retain=True)
        self.state.publish("compatibility/last_reason", reason, retain=True)
