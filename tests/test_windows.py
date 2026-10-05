"""Isolated tests for native handle ownership and notification decoding.

Power/session notifications are synthetic inputs into our own hidden window;
they test message handling, not the state of the user's display or session.
"""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from pcwatch import windows


class TokenTests(unittest.TestCase):
    def test_token_matches_previous_version(self):
        path = Path("test-data")
        expected = hashlib.sha256(os.path.abspath(path).upper().encode("utf-8")).hexdigest().upper()[:24]
        self.assertEqual(windows.signal_token(path), expected)

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_path_case_and_dot_segments_have_same_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "monitor"
            self.assertEqual(windows.signal_token(root), windows.signal_token(Path(str(root).upper())))
            self.assertEqual(windows.signal_token(root), windows.signal_token(root / ".." / "monitor"))


@unittest.skipUnless(os.name == "nt", "Native Windows APIs")
class NativeSignalTests(unittest.TestCase):
    def test_same_thread_duplicate_does_not_acquire_or_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with windows.MonitorSignal(root) as first:
                self.assertTrue(first.acquired)
                self.assertFalse(first.is_set())
                first.set()
                with windows.MonitorSignal(root) as duplicate:
                    self.assertFalse(duplicate.acquired)
                self.assertTrue(first.is_set())
            with windows.MonitorSignal(root) as restarted:
                self.assertTrue(restarted.acquired)
                self.assertFalse(restarted.is_set())

    def test_duplicate_process_and_external_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with windows.MonitorSignal(root) as first:
                script = (
                    "import json,sys;from pathlib import Path;from pcwatch.windows import MonitorSignal,signal_stop;"
                    "\nwith MonitorSignal(Path(sys.argv[1])) as s: print(json.dumps({'acquired':s.acquired,'stopped':signal_stop(Path(sys.argv[1]))}))"
                )
                result = subprocess.run([sys.executable, "-c", script, str(root)],
                    text=True, capture_output=True, timeout=10, check=True)
                self.assertEqual(json.loads(result.stdout), {"acquired": False, "stopped": True})
                self.assertTrue(first.wait(1))
            self.assertFalse(windows.signal_stop(root))

    def test_wait_blocks_until_signal_and_closed_handles_are_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with windows.MonitorSignal(root) as signal:
                start = time.monotonic()
                self.assertFalse(signal.wait(0.05))
                self.assertGreaterEqual(time.monotonic() - start, 0.025)
                timer = threading.Timer(0.05, signal.set)
                timer.start()
                self.assertTrue(signal.wait(2))
                timer.join(2)
            for _ in range(20):
                with windows.MonitorSignal(root) as signal:
                    self.assertTrue(signal.acquired)
                    self.assertFalse(signal.is_set())
            self.assertFalse(windows.signal_stop(root))


@unittest.skipUnless(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8, "Windows x64 ABI")
class NativeWindowTests(unittest.TestCase):
    def test_native_structure_sizes(self):
        expected = {windows.GUID: 16, windows.WNDCLASSW: 72, windows.MSG: 48,
            windows.NOTIFYICONDATAW: 976, windows.POWERBROADCAST_SETTING: 20}
        for structure, size in expected.items():
            with self.subTest(structure=structure.__name__):
                self.assertEqual(ctypes.sizeof(structure), size)

    def test_hidden_message_pump_decodes_synthetic_display_and_session(self):
        records, errors = [], []
        finished = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            window = windows.NativeWindow(records.append, lambda: "Test status",
                finished.set, lambda: None, Path(directory), 15, False)

            def run():
                try:
                    window.run(finished)
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while not window.hwnd and not errors and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(errors, repr(errors))
                self.assertIsNotNone(window.hwnd)
                user = windows._gui()[0]
                user.SendMessageW.argtypes = (ctypes.wintypes.HWND, ctypes.wintypes.UINT,
                    windows.WPARAM, windows.LPARAM)
                user.SendMessageW.restype = windows.LRESULT

                class DisplayNotification(ctypes.Structure):
                    _fields_ = [("header", windows.POWERBROADCAST_SETTING),
                        ("state", ctypes.wintypes.DWORD)]

                notification = DisplayNotification()
                notification.header.PowerSetting = windows.GUID.from_string(windows.DISPLAY_GUID)
                notification.header.DataLength = 4
                for state in (0, 1, 2):
                    notification.state = state
                    user.SendMessageW(window.hwnd, windows.WM_POWERBROADCAST,
                        0x8013, ctypes.addressof(notification))
                user.SendMessageW(window.hwnd, windows.WM_POWERBROADCAST, 4, 0)
                user.SendMessageW(window.hwnd, windows.WM_WTSSESSION_CHANGE, 7, 0)
                user.SendMessageW(window.hwnd, windows.WM_WTSSESSION_CHANGE, 8, 0)
                window.set_health(False)
                window._command(3)
                states = [(record["type"], record.get("state")) for record in records]
                for item in [("display", "off"), ("display", "on"), ("display", "dimmed"),
                    ("power", "suspend"), ("session", "locked"), ("session", "unlocked"),
                    ("user_mark", None)]:
                    self.assertIn(item, states)
                self.assertTrue(all("utc" in record for record in records))
                self.assertFalse(any("title" in record for record in records))
            finally:
                finished.set()
                thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, repr(errors))

    def test_callback_exceptions_are_contained_and_deduplicated(self):
        calls = []

        def failing_callback(record):
            calls.append(record)
            raise OSError("Synthetic emitter failure")

        window = windows.NativeWindow(failing_callback, lambda: "Status", lambda: None,
            lambda: None, Path("unused"), 15, False)
        # The tested messages don't dereference hwnd, change system power, or
        # show UI. This calls only our decoder and a failing synthetic emitter.
        self.assertEqual(window._window_proc(None, windows.WM_POWERBROADCAST, 4, 0), 0)
        count = len(window._callback_errors)
        window._window_proc(None, windows.WM_POWERBROADCAST, 4, 0)
        self.assertEqual(len(window._callback_errors), count)
        self.assertEqual(count, 1)
        self.assertEqual(calls[1]["component"], "native_window")
        self.assertEqual(calls[1]["error_type"], "OSError")
        self.assertNotIn("message", calls[1])


if __name__ == "__main__":
    unittest.main()
