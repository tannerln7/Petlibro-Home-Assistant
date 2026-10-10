import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appdaemon.models.config.app import AllAppConfig

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "render_config.py"
TEMPLATES = ROOT / "templates"
SPEC = importlib.util.spec_from_file_location("render_config", MODULE_PATH)
render_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_config)


class RenderConfigTests(unittest.TestCase):
    def test_addon_manifest_uses_prebuilt_amd64_image(self):
        manifest = (ROOT / "config.yaml").read_text(encoding="utf-8")

        self.assertIn("image: ghcr.io/tannerln7/ha-addon-petlibro-local\n", manifest)
        self.assertIn("arch:\n  - amd64\n", manifest)

    def test_state_agent_manifest_default_matches_signed_release_feed(self):
        expected = (
            "https://raw.githubusercontent.com/tannerln7/Petlibro-Home-Assistant/"
            "state-agent-releases/state-agent/latest.json"
        )
        self.assertEqual(
            expected,
            render_config.DEFAULTS["state_agent_updates"]["manifest_url"],
        )
        self.assertIn(f"manifest_url: {expected}", (ROOT / "config.yaml").read_text())

    def test_generated_apps_do_not_override_appdaemon_log_level(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options(devices=[self.manual_device()])
            options["log_level"] = "trace"
            render_config.validate(options)
            self.render_all(options, data_dir)

            # The standalone model loader does not know the AppDaemon secrets
            # file that the runtime config points at. Replace only the secret
            # tag for this schema-focused assertion.
            model_apps = data_dir / "apps-model.yaml"
            model_apps.write_text(
                (data_dir / "apps.yaml")
                .read_text(encoding="utf-8")
                .replace(
                    "!secret petlibro_state_agent_token",
                    '"state-agent-token-placeholder"',
                ),
                encoding="utf-8",
            )
            config = AllAppConfig.from_config_file(model_apps)
            for app_name in ("petlibro_discovery", "plaf203_example123"):
                app = config[app_name]
                self.assertIsNone(app.log_level)
                self.assertEqual("trace", app.args["petlibro_log_level"])

    def options(self, *, devices=None):
        options = copy.deepcopy(render_config.DEFAULTS)
        options.update(
            {
                "mqtt_host": "192.0.2.10",
                "mqtt_username": "example-user",
                "mqtt_password": "example-password",
                "verbose_logs": True,
                "enable_debug_dumps": True,
                "devices": devices or [],
            }
        )
        return options

    @staticmethod
    def manual_device(serial="EXAMPLE123"):
        return {
            "name": "petlibro_feeder",
            "product": "PLAF203",
            "serial": serial,
            "uid": "PLAF20300000000ABCD0",
            "ip_address": "192.0.2.100",
        }

    def render_all(self, options, data_dir):
        module_environment_names = {
            "plaf203.py": "PETLIBRO_APP_SOURCE",
            "backend.py": "PETLIBRO_BACKEND_SOURCE",
            "camera_metadata.py": "PETLIBRO_CAMERA_METADATA_SOURCE",
            "commands.py": "PETLIBRO_COMMANDS_SOURCE",
            "device_discovery.py": "PETLIBRO_DEVICE_DISCOVERY_SOURCE",
            "dispensing_status.py": "PETLIBRO_DISPENSING_STATUS_SOURCE",
            "feed_plans.py": "PETLIBRO_FEED_PLANS_SOURCE",
            "feeder_mqtt_validation.py": "PETLIBRO_FEEDER_MQTT_VALIDATION_SOURCE",
            "ha_entities.py": "PETLIBRO_HA_ENTITIES_SOURCE",
            "mqtt_client.py": "PETLIBRO_MQTT_CLIENT_SOURCE",
            "petlibro_logging.py": "PETLIBRO_LOGGING_SOURCE",
            "protocol.py": "PETLIBRO_PROTOCOL_SOURCE",
            "settings_map.py": "PETLIBRO_SETTINGS_MAP_SOURCE",
            "state_agent.py": "PETLIBRO_STATE_AGENT_SOURCE",
            "state_agent_updates.py": "PETLIBRO_STATE_AGENT_UPDATES_SOURCE",
            "state_coordinator.py": "PETLIBRO_STATE_COORDINATOR_SOURCE",
            "storage.py": "PETLIBRO_STORAGE_SOURCE",
            "telemetry.py": "PETLIBRO_TELEMETRY_SOURCE",
        }
        sources = {
            filename: data_dir / filename for filename in module_environment_names
        }
        for path in sources.values():
            path.write_text("# synthetic app source\n", encoding="utf-8")
        environment = {
            environment_name: str(sources[filename])
            for filename, environment_name in module_environment_names.items()
        }
        go2rtc_changed = render_config.render_go2rtc(options, data_dir, TEMPLATES)
        with patch.dict(os.environ, environment):
            appdaemon_changed = render_config.render_appdaemon(
                options, data_dir, TEMPLATES
            )
        return go2rtc_changed, appdaemon_changed

    def test_renders_all_runtime_module_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options(devices=[self.manual_device()])
            render_config.validate(options)
            self.render_all(options, data_dir)

            app_dir = data_dir / "appdaemon" / "apps"
            expected_modules = {
                "plaf203.py",
                "backend.py",
                "camera_metadata.py",
                "commands.py",
                "device_discovery.py",
                "dispensing_status.py",
                "feed_plans.py",
                "feeder_mqtt_validation.py",
                "ha_entities.py",
                "mqtt_client.py",
                "petlibro_logging.py",
                "protocol.py",
                "settings_map.py",
                "state_agent.py",
                "state_agent_updates.py",
                "state_coordinator.py",
                "storage.py",
                "telemetry.py",
            }
            self.assertEqual(
                expected_modules,
                {path.name for path in app_dir.glob("*.py")},
            )
            self.assertTrue(all(path.is_symlink() for path in app_dir.glob("*.py")))

    def test_renders_discovery_and_direct_ip_stream_without_leaking_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            stale = data_dir / "petlibro_camera_status_petlibro_feeder.json"
            stale.write_text('{"status":"stale"}\n', encoding="utf-8")
            options = self.options(devices=[self.manual_device()])
            render_config.validate(options)
            self.assertEqual((True, True), self.render_all(options, data_dir))

            go2rtc = (data_dir / "go2rtc.yaml").read_text(encoding="utf-8")
            apps = (data_dir / "apps.yaml").read_text(encoding="utf-8")
            appdaemon = (data_dir / "appdaemon.yaml").read_text(encoding="utf-8")
            registry = (data_dir / "devices.json").read_text(encoding="utf-8")
            self.assertIn('  origin: "*"\n', go2rtc)
            self.assertIn("petlibro://192.0.2.100?", go2rtc)
            self.assertNotIn("subnet=", go2rtc)
            self.assertNotIn("ack=", go2rtc)
            self.assertNotIn("send_delay_ctrl=", go2rtc)
            self.assertNotIn("hd_probe_wait_ms=", go2rtc)
            self.assertIn(
                "status_file=%2Fdata%2Fpetlibro_camera_status_petlibro_feeder.json",
                go2rtc,
            )
            self.assertIn(
                "dump_c2d_plain=%2Fdata%2Fpetlibro_c2d_petlibro_feeder.dat", go2rtc
            )
            self.assertIn("petlibro_discovery:", apps)
            self.assertIn("plaf203_example123:", apps)
            self.assertIn('device_uid: "PLAF20300000000ABCD0"', apps)
            self.assertIn("persist_feeder_mqtt: false", apps)
            self.assertIn('feeder_mqtt_host: ""', apps)
            self.assertIn('petlibro_state_agent_url: "http://192.0.2.100:8765"', apps)
            self.assertIn(
                "state_agent_updates:",
                apps,
            )
            self.assertIn("  enabled: false", apps)
            self.assertIn(
                "petlibro_state_agent_token: !secret petlibro_state_agent_token",
                apps,
            )
            self.assertNotIn("\n  mqtt_host:", apps)
            self.assertEqual(2, apps.count('petlibro_log_level: "info"'))
            self.assertNotIn("\n  log_level:", apps)
            self.assertIn('client_id: "petlibro_local_backend"', appdaemon)
            self.assertIn(
                "namespaces:\n    plaf203:\n      writeback: safe\n      persistent: true",
                appdaemon,
            )
            self.assertNotIn("example-password", registry)
            self.assertNotIn("example-user", registry)
            saved_device = json.loads(registry)["devices"]["PLAF203/EXAMPLE123"]
            self.assertTrue(saved_device["ready"]["stream_configured"])
            self.assertFalse(stale.exists())
            self.assertEqual(0o600, (data_dir / "devices.json").stat().st_mode & 0o777)
            self.assertEqual(
                0o600, (data_dir / "appdaemon-secrets.yaml").stat().st_mode & 0o777
            )

    def test_state_agent_url_template_and_secret_rendering(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options(devices=[self.manual_device()])
            options["petlibro_state_agent_url"] = "http://{ip}:9876"
            options["petlibro_state_agent_token"] = "top-secret-state-token"
            render_config.validate(options)
            self.render_all(options, data_dir)

            apps = (data_dir / "apps.yaml").read_text(encoding="utf-8")
            secrets = (data_dir / "appdaemon-secrets.yaml").read_text(encoding="utf-8")
            registry = (data_dir / "devices.json").read_text(encoding="utf-8")
            self.assertIn('petlibro_state_agent_url: "http://192.0.2.100:9876"', apps)
            self.assertIn("state_agent_updates:", apps)
            self.assertIn(
                "petlibro_state_agent_token: !secret petlibro_state_agent_token",
                apps,
            )
            self.assertIn("top-secret-state-token", secrets)
            self.assertNotIn("top-secret-state-token", apps)
            self.assertNotIn("top-secret-state-token", registry)

    def test_validates_and_renders_state_agent_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options(devices=[self.manual_device()])
            options["state_agent_updates"] = {
                "enabled": True,
                "manifest_url": "https://downloads.example.invalid/plaf203/v1.2.3/latest.json",
                "check_on_connect": False,
                "check_interval_hours": 12,
            }
            render_config.validate(options)
            self.render_all(options, data_dir)

            apps = (data_dir / "apps.yaml").read_text(encoding="utf-8")
            self.assertIn("state_agent_updates:", apps)
            self.assertIn("enabled: true", apps)
            self.assertIn(
                'manifest_url: "https://downloads.example.invalid/plaf203/v1.2.3/latest.json"',
                apps,
            )
            self.assertIn("check_on_connect: false", apps)
            self.assertIn("check_interval_hours: 12", apps)

    def test_rejects_invalid_state_agent_updates_config(self):
        options = self.options()
        options["state_agent_updates"] = "not-json"
        with self.assertRaisesRegex(ValueError, "state_agent_updates"):
            render_config.validate(options)

        options = self.options()
        options["state_agent_updates"] = {
            "enabled": True,
            "manifest_url": "",
        }
        with self.assertRaisesRegex(ValueError, "manifest_url"):
            render_config.validate(options)

        options = self.options()
        options["state_agent_updates"] = {
            "enabled": True,
            "manifest_url": "http://example.invalid/latest.json",
        }
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            render_config.validate(options)

        options = self.options()
        options["state_agent_updates"] = {
            "enabled": True,
            "manifest_url": "https://example.invalid/latest.json?channel=stable",
        }
        with self.assertRaisesRegex(ValueError, "query"):
            render_config.validate(options)

        options = self.options()
        options["state_agent_updates"] = {
            "enabled": True,
            "manifest_url": "https://example.invalid/latest.json",
            "check_interval_hours": 0,
        }
        with self.assertRaisesRegex(ValueError, "between 1 and 168"):
            render_config.validate(options)

    def test_renders_multiple_resolved_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            second = self.manual_device("SECOND456")
            second.update(
                name="kitchen_feeder",
                uid="PLAF20300000000EFGH1",
                ip_address="192.0.2.101",
            )
            options = self.options(devices=[self.manual_device(), second])
            render_config.validate(options)
            self.render_all(options, data_dir)
            go2rtc = (data_dir / "go2rtc.yaml").read_text(encoding="utf-8")
            apps = (data_dir / "apps.yaml").read_text(encoding="utf-8")
            self.assertIn("petlibro_feeder:", go2rtc)
            self.assertIn("kitchen_feeder:", go2rtc)
            self.assertIn("plaf203_example123:", apps)
            self.assertIn("plaf203_second456:", apps)

    def test_unchanged_render_does_not_rewrite_configs(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options(devices=[self.manual_device()])
            self.assertEqual((True, True), self.render_all(options, data_dir))
            live_status = data_dir / "petlibro_camera_status_petlibro_feeder.json"
            live_status.write_text('{"status":"online"}\n', encoding="utf-8")
            self.assertEqual((False, False), self.render_all(options, data_dir))
            self.assertTrue(live_status.exists())

    def test_migrates_legacy_options_and_ignores_unknown_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options()
            options.update(
                device_ip="192.0.2.100",
                product="PLAF203",
                serial="EXAMPLE123",
                uid="PLAF20300000000ABCD0",
                product_secret="must-not-leak",
                send_delay_ctrl=False,
                hd_probe_wait_ms=60000,
                streamctrl_variant="standard",
                streamctrl_quality=2,
            )
            (data_dir / "options.json").write_text(
                json.dumps(options), encoding="utf-8"
            )
            loaded = render_config.load_options(data_dir)
            render_config.validate(loaded)
            self.render_all(loaded, data_dir)
            generated = "\n".join(
                path.read_text(encoding="utf-8") for path in data_dir.glob("*.yaml")
            )
            self.assertIn("petlibro://192.0.2.100?", generated)
            self.assertNotIn("must-not-leak", generated)
            self.assertNotIn("send_delay_ctrl", generated)
            self.assertNotIn("hd_probe_wait_ms", generated)
            self.assertNotIn("streamctrl_variant", generated)
            self.assertNotIn("streamctrl_quality", generated)

    def test_loads_discovery_first_home_assistant_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            raw = {"mqtt_host": "core-mosquitto", "lan_cidr": "10.20.0.0/16"}
            (data_dir / "options.json").write_text(json.dumps(raw), encoding="utf-8")
            loaded = render_config.load_options(data_dir)
            render_config.validate(loaded)
            self.assertTrue(loaded["device_discovery"])
            self.assertEqual([], loaded["devices"])
            self.assertEqual("10.20.0.0/16", loaded["lan_cidr"])
            self.assertFalse(loaded["persist_feeder_mqtt"])
            self.assertEqual("", loaded["feeder_mqtt_host"])

    def test_feeder_mqtt_persistence_requires_a_separate_destination(self):
        options = self.options()
        options["persist_feeder_mqtt"] = True
        with self.assertRaisesRegex(ValueError, "feeder_mqtt_host is required"):
            render_config.validate(options)

        options["feeder_mqtt_host"] = "mqtt.example.test"
        render_config.validate(options)

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options["devices"] = [self.manual_device()]
            self.render_all(options, data_dir)
            apps = (data_dir / "apps.yaml").read_text(encoding="utf-8")
            self.assertIn("persist_feeder_mqtt: true", apps)
            self.assertIn('feeder_mqtt_host: "mqtt.example.test"', apps)
            self.assertIn("feeder_mqtt_port: 1883", apps)
            self.assertNotIn('mqtt_host: "192.0.2.10"', apps)

    def test_log_level_validation_and_legacy_verbose_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "options.json").write_text(
                json.dumps({"verbose_logs": True}), encoding="utf-8"
            )
            loaded = render_config.load_options(data_dir)
            self.assertEqual("debug", loaded["log_level"])

        options = self.options()
        options["log_level"] = "noisy"
        with self.assertRaisesRegex(ValueError, "log_level"):
            render_config.validate(options)

    def test_trace_is_bounded_and_protocol_tracing_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options(devices=[self.manual_device()])
            options["log_level"] = "debug"
            render_config.validate(options)
            self.render_all(options, data_dir)
            go2rtc = (data_dir / "go2rtc.yaml").read_text(encoding="utf-8")
            self.assertIn("verbose=1", go2rtc)
            self.assertNotIn("trace_packets=1", go2rtc)
            self.assertFalse((data_dir / ".verbose_logs").exists())

            options["log_level"] = "trace"
            self.render_all(options, data_dir)
            go2rtc = (data_dir / "go2rtc.yaml").read_text(encoding="utf-8")
            self.assertNotIn("trace_packets=1", go2rtc)
            self.assertNotIn("trace_ack=1", go2rtc)
            self.assertIn('streams: "debug"', go2rtc)
            self.assertIn('webrtc: "debug"', go2rtc)

            options["camera_protocol_tracing"] = True
            self.render_all(options, data_dir)
            go2rtc = (data_dir / "go2rtc.yaml").read_text(encoding="utf-8")
            self.assertIn("trace_packets=1", go2rtc)
            self.assertIn("trace_ack=1", go2rtc)
            apps = (data_dir / "apps.yaml").read_text(encoding="utf-8")
            self.assertIn('petlibro_log_level: "trace"', apps)
            self.assertNotIn("\n  log_level:", apps)

    def test_empty_registry_starts_discovery_without_a_camera_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options()
            render_config.validate(options)
            self.render_all(options, data_dir)
            self.assertIn(
                "streams:\n  {}",
                (data_dir / "go2rtc.yaml").read_text(encoding="utf-8"),
            )
            apps = (data_dir / "apps.yaml").read_text(encoding="utf-8")
            self.assertIn("petlibro_discovery:", apps)
            self.assertNotIn("class: Plaf203", apps)

    def test_rejects_invalid_manual_uid_without_echoing_value(self):
        device = self.manual_device()
        device["uid"] = "too-short"
        with self.assertRaisesRegex(ValueError, r"devices\[0\]\.uid"):
            render_config.validate(self.options(devices=[device]))

    def test_rejects_oversized_or_non_ipv4_discovery_network(self):
        options = self.options()
        options["lan_cidr"] = "10.0.0.0/8"
        with self.assertRaisesRegex(ValueError, "no larger than /16"):
            render_config.validate(options)
        options["lan_cidr"] = "2001:db8::/64"
        with self.assertRaisesRegex(ValueError, "IPv4"):
            render_config.validate(options)

    def test_disabling_camera_metadata_omits_status_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            options = self.options(devices=[self.manual_device()])
            options["publish_camera_metadata"] = False
            render_config.validate(options)
            self.render_all(options, data_dir)
            self.assertNotIn(
                "status_file", (data_dir / "go2rtc.yaml").read_text(encoding="utf-8")
            )
            self.assertIn(
                "publish_camera_metadata: false",
                (data_dir / "apps.yaml").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
