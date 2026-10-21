"""Non-authoritative AppDaemon persistence for local preferences and diagnostics."""

from __future__ import annotations

import json

import appdaemon.adapi as adapi

from state_agent import FeederTruth

class Storage:
    def __init__(self, ad: adapi.ADAPI, namespace: str, serial_number: str):
        self.ad: adapi.ADAPI = ad
        self.namespace: str = namespace
        self.food_manual_feed_grain_num_entity_id: str = 'sensor.plaf203_{}_food_manual_feed_grain_num'.format(serial_number)
        self.verified_truth_entity_id: str = 'sensor.plaf203_{}_verified_feeder_truth'.format(serial_number)
        self.compatibility_reboot_entity_id = (
            'sensor.plaf203_{}_compatibility_reboot_attempt'.format(serial_number)
        )

    def initialize(self):
        self.ad.set_namespace(self.namespace)

        if not self._entity_state_exists(self.food_manual_feed_grain_num_entity_id):
            self._entity_state_int_set(
                self.food_manual_feed_grain_num_entity_id,
                1,
                check_existence = False,
            )

    def terminate(self):
        self.ad.save_namespace()

    def food_manual_feed_grain_num_get(self) -> int:
        return self._entity_state_int_get(self.food_manual_feed_grain_num_entity_id)

    def food_manual_feed_grain_num_set(self, grain_num: int):
        self._entity_state_int_set(self.food_manual_feed_grain_num_entity_id, grain_num)

    def verified_truth_set(self, truth: FeederTruth):
        self._entity_state_dict_set(
            self.verified_truth_entity_id,
            truth.to_dict(),
            check_existence=self._entity_state_exists(self.verified_truth_entity_id),
        )

    def compatibility_reboot_version_get(self) -> str | None:
        if not self._entity_state_exists(self.compatibility_reboot_entity_id):
            return None
        return self.ad.get_state(
            self.compatibility_reboot_entity_id, namespace=self.namespace
        )

    def compatibility_reboot_version_set(self, version: str) -> None:
        # Save before publishing a reboot, so a controller restart does not
        # repeatedly reboot a feeder whose activation never became healthy.
        previous = self.compatibility_reboot_version_get()
        try:
            self.ad.set_state(
                self.compatibility_reboot_entity_id, state=version,
                namespace=self.namespace, check_existence=False,
            )
            if self.ad.save_namespace(namespace=self.namespace) is not True:
                raise RuntimeError("compatibility reboot guard could not be saved")
        except Exception:
            self.ad.set_state(
                self.compatibility_reboot_entity_id, state=previous or "",
                namespace=self.namespace, check_existence=False,
            )
            raise

    def _entity_state_exists(self, name: str) -> bool:
        return self.ad.entity_exists(name, namespace=self.namespace)

    def _entity_state_int_get(self, name: str) -> int:
        return int(self.ad.get_state(name, namespace=self.namespace))

    def _entity_state_dict_set(
        self,
        name: str,
        state: dict,
        check_existence: bool = True,
    ):
        self.ad.set_state(
            name,
            state=json.dumps(state),
            namespace=self.namespace,
            check_existence=check_existence,
        )

    def _entity_state_int_set(
        self,
        name: str,
        state: int,
        check_existence: bool = True,
    ):
        self.ad.set_state(
            name,
            state=state,
            namespace=self.namespace,
            check_existence=check_existence,
        )
