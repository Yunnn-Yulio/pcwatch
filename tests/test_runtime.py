"""Isolated Windows subprocess checks for capture, control, and write recovery."""

import csv
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows recorder APIs are unavailable")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pcwatch-runtime-test-")
        self.root = Path(self.temporary.name)
        self.instances = []

    def tearDown(self):
        for process, data_root in self.instances:
            if process.poll() is None:
                self.control(data_root, "--stop")
                try:
                    process.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
            else:
                process.communicate()
        self.temporary.cleanup()

    def start(self, name="recorder", duration=30):
        data_root = self.root / name
        process = subprocess.Popen(
            [sys.executable, "-m", "pcwatch", "--no-tray", "--data-dir", str(data_root),
             "--interval", "2", "--details", "2", "--max-seconds", str(duration)],
            cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.instances.append((process, data_root))
        self.wait_status(process, data_root, lambda status: status.get("last_sample_utc") is not None)
        return process, data_root

    def control(self, data_root, flag):
        result = subprocess.run(
            [sys.executable, "-m", "pcwatch", "--data-dir", str(data_root), flag],
            cwd=PROJECT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def wait_status(self, process, data_root, condition, timeout=15):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = json.loads((data_root / "status.json").read_text(encoding="utf-8"))
                if condition(last):
                    return last
            except (OSError, ValueError):
                pass
            if process.poll() is not None:
                _, error = process.communicate()
                self.fail(f"Recorder stopped before the expected state: code {process.returncode}; {error}")
            time.sleep(0.1)
        self.fail(f"Timed out waiting for recorder status; last state: {last}")

    def finish(self, process):
        _, error = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, error)

    def test_capture_uses_parseable_local_logs_and_graceful_stop(self):
        process, data_root = self.start()
        first = json.loads((data_root / "status.json").read_text(encoding="utf-8"))
        status = self.wait_status(process, data_root,
                                  lambda item: item.get("last_sample_utc") != first["last_sample_utc"])
        self.assertEqual(status["implementation"], "python")
        self.assertTrue(status["logging_healthy"])
        self.assertGreater(status["ram_total_MiB"], 0)
        self.assertGreaterEqual(status["cpu_pct"], 0)
        self.assertLessEqual(status["cpu_pct"], 100)
        self.assertGreater(status["monitor_working_MiB"], 0)
        self.control(data_root, "--stop")
        self.finish(process)
        saved = json.loads((data_root / "status.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["running"])
        rows = []
        for path in (data_root / "Logs").glob("resources-*.csv"):
            with path.open(encoding="utf-8", newline="") as stream:
                rows.extend(csv.DictReader(stream))
        self.assertGreaterEqual(len(rows), 2)
        activity = []
        for path in (data_root / "Logs").glob("activity-*.jsonl"):
            activity.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        self.assertIn("start", [item["type"] for item in activity])
        self.assertIn("stop", [item["type"] for item in activity])
        for path in (data_root / "Logs").glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.assertIsInstance(record, dict)
                self.assertNotIn("window_title", line)
                self.assertNotIn("command_line", line)

    def test_duplicate_guard_and_independent_data_directories(self):
        first, first_root = self.start("first")
        second, second_root = self.start("second")
        duplicate = subprocess.run(
            [sys.executable, "-m", "pcwatch", "--no-tray", "--data-dir", str(first_root),
             "--interval", "2", "--details", "2", "--max-seconds", "4"],
            cwd=PROJECT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        status = json.loads((first_root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["monitor_pid"], first.pid)
        self.control(first_root, "--stop")
        self.finish(first)
        self.assertIsNone(second.poll())
        self.control(second_root, "--stop")
        self.finish(second)

    def test_exclusive_csv_lock_reports_failure_and_resumes_saved_samples(self):
        process, data_root = self.start()
        resource = next((data_root / "Logs").glob("resources-*.csv"))
        native = ctypes.WinDLL("kernel32", use_last_error=True)
        native.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        native.CreateFileW.restype = wintypes.HANDLE
        native.CloseHandle.argtypes = [wintypes.HANDLE]
        native.CloseHandle.restype = wintypes.BOOL
        handle = native.CreateFileW(str(resource), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        try:
            failed = self.wait_status(process, data_root,
                                      lambda item: "resources" in item.get("log_error_components", []))
            self.assertFalse(failed["logging_healthy"])
            saved_time = failed["last_sample_utc"]
            latest_time = failed["utc"]
            another = self.wait_status(process, data_root,
                                       lambda item: item.get("utc") != latest_time)
            self.assertEqual(another["last_sample_utc"], saved_time)
            self.assertIn("resources", another["log_error_components"])
        finally:
            native.CloseHandle(handle)
        recovered = self.wait_status(process, data_root,
                                     lambda item: item.get("logging_healthy")
                                     and item.get("last_sample_utc") != saved_time)
        self.assertEqual(recovered["log_error_components"], [])
        self.control(data_root, "--stop")
        self.finish(process)

    def test_bounded_duration_finishes_without_external_control(self):
        process, data_root = self.start(duration=5)
        self.finish(process)
        status = json.loads((data_root / "status.json").read_text(encoding="utf-8"))
        self.assertFalse(status["running"])
        self.assertTrue(status["logging_healthy"])


if __name__ == "__main__":
    unittest.main()
