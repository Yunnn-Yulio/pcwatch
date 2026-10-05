"""CPU accounting, privacy selection, and real Windows API smoke checks."""

import ctypes
import math
import os
import time
import unittest

from pcwatch.metrics import (Metrics, CounterValue, ProcessEntry, ProcessMemory,
                             cpu_percent, process_cpu_percent, select_processes)


class AccountingTests(unittest.TestCase):
    def test_system_cpu_excludes_idle_from_kernel_ticks(self):
        self.assertEqual(cpu_percent((100, 200), (160, 300)), 40.0)

    def test_first_cpu_and_invalid_clock_return_unavailable(self):
        self.assertIsNone(cpu_percent(None, (100, 200)))
        self.assertIsNone(cpu_percent((100, 200), (100, 200)))
        self.assertIsNone(cpu_percent((100, 200), (90, 220)))

    def test_process_cpu_normalizes_all_cpus(self):
        self.assertEqual(process_cpu_percent((123, 1.0), (123, 3.0), 2.0, 8), 12.5)

    def test_process_cpu_rejects_pid_reuse(self):
        self.assertIsNone(process_cpu_percent((123, 1.0), (456, 3.0), 2.0, 8))

    def test_process_cpu_rejects_invalid_delta(self):
        self.assertIsNone(process_cpu_percent((123, 3.0), (123, 2.0), 2.0, 8))
        self.assertIsNone(process_cpu_percent((123, 1.0), (123, 3.0), 0.0, 8))

    def test_selection_keeps_hung_outside_leaders_and_deduplicates(self):
        items = [{"pid": pid, "name": "example.exe", "private_MiB": pid,
                  "cpu_pct": float(pid), "hung_window": pid == 1}
                 for pid in range(1, 26)]
        selected = select_processes(items)
        self.assertIn(1, [item["pid"] for item in selected])
        self.assertEqual(len(selected), len({item["pid"] for item in selected}))
        self.assertEqual(len(selected), 11)


@unittest.skipUnless(os.name == "nt", "Windows native APIs are unavailable")
class NativeTests(unittest.TestCase):
    def test_structure_layouts_are_pointer_safe(self):
        self.assertEqual(ctypes.sizeof(CounterValue), 16)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(ProcessEntry), 568)
            self.assertEqual(ctypes.sizeof(ProcessMemory), 80)

    def test_native_metrics_and_process_snapshot(self):
        with Metrics() as metrics:
            first = metrics.sample()
            self.assertGreater(first["ram_total_MiB"], 0)
            self.assertGreater(first["ram_available_MiB"], 0)
            self.assertIsNone(first["cpu_pct"])
            self.assertGreater(first["monitor_working_MiB"], 0)
            snapshot = metrics.process_snapshot()
            own = [item for item in snapshot["processes"] if item["pid"] == os.getpid()]
            self.assertEqual(snapshot["type"], "process_snapshot")
            self.assertGreater(len(snapshot["processes"]), 0)
            permitted = {"name", "pid", "working_MiB", "private_MiB", "cpu_pct", "hung_window"}
            for item in snapshot["processes"]:
                self.assertEqual(set(item), permitted)
                self.assertNotIn("\\", item["name"])
                self.assertNotIn("/", item["name"])
            time.sleep(0.05)
            second = metrics.sample()
            self.assertTrue(math.isfinite(second["cpu_pct"]))
            self.assertGreaterEqual(second["cpu_pct"], 0)
            self.assertLessEqual(second["cpu_pct"], 100)
            self.assertGreaterEqual(second["monitor_cpu_pct"], 0)


if __name__ == "__main__":
    unittest.main()
