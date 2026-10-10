import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from camera_metadata import CameraMetadataPublisher


class FakeAd:
    def __init__(self):
        self.logs = []
        self.log_levels = []
        self.cancelled = []

    def log(self, message, **kwargs):
        self.logs.append(message)
        self.log_levels.append(kwargs.get("level"))

    def run_every(self, callback, start, interval):
        self.scheduled = (callback, start, interval)
        return "timer-handle"

    def cancel_timer(self, handle, silent):
        self.cancelled.append((handle, silent))


class FakeMqtt:
    def __init__(self):
        self.published = []

    def mqtt_publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))


def runtime_status(last_update, observed_at=None, **overrides):
    observed_at = observed_at or last_update
    status = {
        "schema_version": 2,
        "status": "online",
        "requested_quality": "hd",
        "probe_resolution": {
            "width": 640,
            "height": 360,
            "profile_idc": 66,
            "level_idc": 30,
            "observed_at": observed_at,
        },
        "actual_resolution": {
            "width": 1920,
            "height": 1080,
            "profile_idc": 66,
            "level_idc": 41,
            "observed_at": observed_at,
        },
        "hd_transition": {"observed": True, "elapsed_ms": 10000},
        "last_update": last_update,
        "health": {
            "gapped_idrs": 0,
            "dropped_frames": 1,
            "missing_fragments": 2,
            "ack_pending": 0,
            "extended_media_rejected": 0,
        },
    }
    status.update(overrides)
    return status


class CameraMetadataPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.status_file = Path(self.temporary.name) / "camera.json"
        self.current = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.observed_at = self.current.isoformat().replace("+00:00", "Z")
        self.ad = FakeAd()
        self.mqtt = FakeMqtt()
        self.publisher = CameraMetadataPublisher(
            self.ad,
            self.mqtt,
            enabled=True,
            product="PLAF203",
            serial="YOUR_DEVICE_SERIAL",
            stream_name="petlibro_feeder",
            requested_quality="hd",
            rtsp_port=8554,
            status_file=str(self.status_file),
            topic_prefix="",
            heartbeat_seconds=30,
            now=lambda: self.current,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_status(self, **overrides):
        timestamp = self.current.isoformat().replace("+00:00", "Z")
        self.status_file.write_text(
            json.dumps(
                runtime_status(timestamp, observed_at=self.observed_at, **overrides)
            ),
            encoding="utf-8",
        )

    def test_topics_payload_sanitization_qos_and_deduplication(self):
        self.write_status(
            mqtt_password="must-not-leak",
            product_secret="must-not-leak",
            device_ip="192.0.2.5",
        )
        self.publisher.poll()

        self.assertEqual(2, len(self.mqtt.published))
        state_topic, state_json, state_kwargs = self.mqtt.published[0]
        availability_topic, availability, availability_kwargs = self.mqtt.published[1]
        self.assertEqual(
            "petlibro_local/PLAF203/YOUR_DEVICE_SERIAL/camera/state", state_topic
        )
        self.assertEqual(
            "petlibro_local/PLAF203/YOUR_DEVICE_SERIAL/camera/availability",
            availability_topic,
        )
        payload = json.loads(state_json)
        self.assertNotIn("mqtt_password", payload)
        self.assertNotIn("product_secret", payload)
        self.assertNotIn("device_ip", payload)
        self.assertNotIn("configured_hd_probe_wait_ms", payload)
        self.assertEqual(
            "rtsp://<backend-host>:8554/petlibro_feeder",
            payload["rtsp_url_hint"],
        )
        self.assertEqual("online", availability)
        self.assertEqual({"namespace": "mqtt", "qos": 1, "retain": True}, state_kwargs)
        self.assertEqual(state_kwargs, availability_kwargs)

        self.current += datetime.timedelta(seconds=5)
        self.write_status()
        self.publisher.poll()
        self.assertEqual(2, len(self.mqtt.published))

        self.current += datetime.timedelta(seconds=25)
        self.write_status()
        self.publisher.poll()
        self.assertEqual(4, len(self.mqtt.published))

    def test_missing_malformed_and_stale_status_are_safe(self):
        self.publisher.poll()
        self.assertEqual("offline", self.mqtt.published[1][1])
        self.assertIn("[INFO]", self.ad.logs[-1])

        self.current += datetime.timedelta(seconds=1)
        self.status_file.write_text("{not-json", encoding="utf-8")
        self.publisher.poll()
        self.assertEqual("error", json.loads(self.mqtt.published[-1][1])["status"])

        self.current += datetime.timedelta(seconds=1)
        stale = self.current - datetime.timedelta(seconds=91)
        stale_text = stale.isoformat().replace("+00:00", "Z")
        self.status_file.write_text(
            json.dumps(runtime_status(stale_text)), encoding="utf-8"
        )
        self.publisher.poll()
        state_messages = [
            json.loads(payload)
            for topic, payload, _kwargs in self.mqtt.published
            if topic.endswith("/state")
        ]
        self.assertEqual("offline", state_messages[-1]["status"])
        self.assertEqual(3, len(self.ad.logs))

    def test_missing_status_becomes_warning_after_runtime_status_was_seen(self):
        self.write_status()
        self.publisher.poll()
        self.status_file.unlink()
        self.current += datetime.timedelta(seconds=1)
        self.publisher.poll()

        self.assertIn("[WARNING]", self.ad.logs[-1])
        self.assertEqual("WARNING", self.ad.log_levels[-1])

    def test_stale_inactive_status_is_expected(self):
        stale = self.current - datetime.timedelta(minutes=10)
        stale_text = stale.isoformat().replace("+00:00", "Z")
        for status in ("offline", "idle"):
            with self.subTest(status=status):
                self.status_file.write_text(
                    json.dumps(runtime_status(stale_text, status=status)),
                    encoding="utf-8",
                )
                self.publisher.poll()
                state_messages = [
                    json.loads(payload)
                    for topic, payload, _kwargs in self.mqtt.published
                    if topic.endswith("/state")
                ]
                self.assertEqual(status, state_messages[-1]["status"])
        self.assertEqual([], self.ad.logs)

    def test_start_and_stop_manage_timer_and_offline_state(self):
        self.write_status()
        self.publisher.start()
        callback, start, interval = self.ad.scheduled
        self.assertEqual("immediate", start)
        self.assertEqual(2, interval)
        callback()
        self.publisher.stop()

        self.assertEqual([("timer-handle", True)], self.ad.cancelled)
        self.assertEqual("offline", self.mqtt.published[-1][1])
        state_messages = [
            json.loads(payload)
            for topic, payload, _kwargs in self.mqtt.published
            if topic.endswith("/state")
        ]
        self.assertEqual("offline", state_messages[-1]["status"])


if __name__ == "__main__":
    unittest.main()
