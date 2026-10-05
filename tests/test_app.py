"""Public controls, independent component health, and private error metadata."""

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcwatch.app import Monitor, error_metadata, parse_args


class SignalStub:
    def set(self):
        pass


class StoreStub:
    def __init__(self):
        self.failure = None
        self.records = []

    def write_json(self, kind, record):
        if self.failure:
            raise self.failure
        self.records.append((kind, record))


class OptionsTests(unittest.TestCase):
    def test_defaults_and_data_directory_are_explicit(self):
        options = parse_args(["--data-dir", "test-output"])
        self.assertTrue(options.data_dir.is_absolute())
        self.assertEqual((options.interval, options.details), (15, 60))
        self.assertEqual((options.max_log_mb, options.segment_kb, options.retention_days),
                         (100, 5120, 14))
        self.assertFalse(options.install_startup)

    def test_invalid_limits_and_conflicting_controls_are_rejected(self):
        cases = (["--interval", "0"], ["--details", "14"],
                 ["--max-log-mb", "0"], ["--segment-kb", "15"],
                 ["--retention-days", "366"], ["--max-seconds", "-1"],
                 ["--stop", "--install-startup"])
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    parse_args(args)
                self.assertEqual(result.exception.code, 2)


class ComponentHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pcwatch-app-test-")
        self.root = Path(self.temporary.name)
        self.store = StoreStub()
        options = parse_args(["--data-dir", str(self.root)])
        with patch("pcwatch.app.startup.is_enabled", return_value=False):
            self.monitor = Monitor(options, SignalStub(), store=self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def test_exception_metadata_discards_messages_and_paths(self):
        marker = "private-document-and-secret-token"
        metadata = error_metadata(OSError(13, marker, str(self.root / marker)))
        self.assertEqual(metadata, {"error_type": "PermissionError", "error_code": 13})
        self.assertNotIn(marker, json.dumps(metadata))

    def test_failed_event_write_propagates_and_recovers_independently(self):
        self.monitor.set_log_error("resources", OSError(13, "private path"))
        self.store.failure = OSError(32, "private event payload")
        with self.assertRaises(OSError):
            self.monitor.write_json("events", {"type": "event"})
        status = self.monitor.status()
        self.assertFalse(status["logging_healthy"])
        self.assertEqual(status["log_error_components"], ["events", "resources"])
        self.assertNotIn("private", json.dumps(status))
        self.store.failure = None
        self.monitor.write_json("events", {"type": "event"})
        self.assertEqual(self.monitor.status()["log_error_components"], ["resources"])
        self.monitor.set_log_error("resources")
        self.assertTrue(self.monitor.status()["logging_healthy"])

    def test_collection_error_is_deduplicated_without_failing_log_health(self):
        for message in ("private value one", "private value two"):
            self.monitor.collection_error("events", OSError(5, message))
        self.assertEqual(len(self.store.records), 1)
        status = self.monitor.status()
        self.assertTrue(status["logging_healthy"])
        self.assertEqual(status["collection_errors"]["events"]["error_code"], 5)
        self.assertNotIn("private value", json.dumps(self.store.records))

    def test_status_file_recovers_after_atomic_write_failure(self):
        with patch("pcwatch.app.atomic_json", side_effect=OSError(32, "private path")):
            self.monitor.save_status()
        self.assertEqual(self.monitor.status()["log_error_components"], ["status"])
        self.monitor.save_status()
        saved = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["logging_healthy"])
        self.assertEqual(saved["log_error_components"], [])
        self.assertTrue(self.monitor.status()["logging_healthy"])

    def test_previous_unclean_session_reports_uncertainty_without_payload(self):
        previous = {"running": True, "last_sample_utc": "2026-01-01T00:00:00Z",
                    "private_payload": "PRIVATE-MARKER"}
        (self.root / "status.json").write_text(json.dumps(previous), encoding="utf-8")
        self.monitor.check_previous_session()
        kind, record = self.store.records[0]
        self.assertEqual(kind, "activity")
        self.assertEqual(record["interpretation"], "previous_session_end_cause_unknown")
        self.assertNotIn("PRIVATE-MARKER", json.dumps(record))
        self.assertEqual(record["last_sample_utc"], previous["last_sample_utc"])


if __name__ == "__main__":
    unittest.main()
