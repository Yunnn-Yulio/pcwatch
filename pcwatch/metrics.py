"""Low-frequency Windows resource metrics using only the Python standard library.

Native libraries load when Metrics is constructed, so the math helpers can be
tested on any platform. Missing counters produce None, never a fabricated zero.
"""

from __future__ import annotations

import ctypes
import math
import os
import time
from datetime import datetime, timezone

MIB = 1024 * 1024
U32 = ctypes.c_uint32
U64 = ctypes.c_uint64
SIZE = ctypes.c_size_t
HANDLE = ctypes.c_void_p
BOOL = ctypes.c_int32
PDH_MORE_DATA = 0x800007D2
PDH_DOUBLE = 0x200


def cpu_percent(previous: tuple[int, int] | None, current: tuple[int, int]) -> float | None:
    """Calculate total CPU from (idle, kernel + user) 100-nanosecond ticks."""
    if previous is None:
        return None
    idle_delta = current[0] - previous[0]
    total_delta = current[1] - previous[1]
    if total_delta <= 0 or idle_delta < 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta)), 2)


def process_cpu_percent(previous: tuple[int, float] | None, current: tuple[int, float],
                        elapsed: float, logical_processors: int, precision: int = 3) -> float | None:
    """Normalize process CPU across all logical CPUs; reject reused PIDs."""
    if previous is None or previous[0] != current[0] or elapsed <= 0 or logical_processors <= 0:
        return None
    delta = current[1] - previous[1]
    if delta < 0:
        return None
    return round(min(100.0, delta / elapsed / logical_processors * 100.0), precision)


def select_processes(items: list[dict], limit: int = 40) -> list[dict]:
    """Select memory and CPU leaders plus visible windows flagged as hung."""
    selected = sorted(items, key=lambda item: item["private_MiB"], reverse=True)[:10]
    selected += sorted((item for item in items if item["cpu_pct"] is not None),
                       key=lambda item: item["cpu_pct"], reverse=True)[:10]
    selected += [item for item in items if item["hung_window"]]
    result, seen = [], set()
    for item in selected:
        if item["pid"] not in seen:
            seen.add(item["pid"])
            result.append(item)
            if len(result) == limit:
                break
    return result


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", U32), ("memory_load", U32), ("total_physical", U64),
                ("available_physical", U64), ("total_page_file", U64),
                ("available_page_file", U64), ("total_virtual", U64),
                ("available_virtual", U64), ("available_extended_virtual", U64)]


class PerformanceInfo(ctypes.Structure):
    _fields_ = [("size", U32)] + [(name, SIZE) for name in (
        "commit_total", "commit_limit", "commit_peak", "physical_total",
        "physical_available", "system_cache", "kernel_total", "kernel_paged",
        "kernel_nonpaged", "page_size")] + [("handle_count", U32),
        ("process_count", U32), ("thread_count", U32)]


class FileTime(ctypes.Structure):
    _fields_ = [("low", U32), ("high", U32)]

    def ticks(self) -> int:
        return (self.high << 32) | self.low


class ProcessMemory(ctypes.Structure):
    _fields_ = [("size", U32), ("page_fault_count", U32)] + [(name, SIZE) for name in (
        "peak_working_set", "working_set", "quota_peak_paged_pool", "quota_paged_pool",
        "quota_peak_nonpaged_pool", "quota_nonpaged_pool", "pagefile_usage",
        "peak_pagefile_usage", "private_usage")]


class ProcessEntry(ctypes.Structure):
    _fields_ = [("size", U32), ("usage", U32), ("pid", U32), ("default_heap", SIZE),
                ("module_id", U32), ("threads", U32), ("parent_pid", U32),
                ("priority", ctypes.c_int32), ("flags", U32),
                ("exe_file", ctypes.c_wchar * 260)]


class CounterValue(ctypes.Structure):
    _fields_ = [("status", U32), ("value", ctypes.c_double)]


class CounterItem(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("value", CounterValue)]


def _bind(library, name, args, result):
    function = getattr(library, name)
    function.argtypes = args
    function.restype = result
    return function


def _native_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise OSError("PCWatch resource collection requires Windows.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.memory_status = _bind(self.kernel, "GlobalMemoryStatusEx", [ctypes.POINTER(MemoryStatus)], BOOL)
        self.performance_info = _bind(self.psapi, "GetPerformanceInfo", [ctypes.POINTER(PerformanceInfo), U32], BOOL)
        self.system_times = _bind(self.kernel, "GetSystemTimes", [ctypes.POINTER(FileTime)] * 3, BOOL)
        self.process_times = _bind(self.kernel, "GetProcessTimes", [HANDLE] + [ctypes.POINTER(FileTime)] * 4, BOOL)
        self.process_memory = _bind(self.kernel, "K32GetProcessMemoryInfo", [HANDLE, ctypes.POINTER(ProcessMemory), U32], BOOL)
        self.current_process = _bind(self.kernel, "GetCurrentProcess", [], HANDLE)
        self.open_process = _bind(self.kernel, "OpenProcess", [U32, BOOL, U32], HANDLE)
        self.close_handle = _bind(self.kernel, "CloseHandle", [HANDLE], BOOL)
        self.snapshot = _bind(self.kernel, "CreateToolhelp32Snapshot", [U32, U32], HANDLE)
        self.process_first = _bind(self.kernel, "Process32FirstW", [HANDLE, ctypes.POINTER(ProcessEntry)], BOOL)
        self.process_next = _bind(self.kernel, "Process32NextW", [HANDLE, ctypes.POINTER(ProcessEntry)], BOOL)
        self.window_callback = ctypes.WINFUNCTYPE(BOOL, HANDLE, ctypes.c_ssize_t)
        self.enum_windows = _bind(self.user, "EnumWindows", [self.window_callback, ctypes.c_ssize_t], BOOL)
        self.is_visible = _bind(self.user, "IsWindowVisible", [HANDLE], BOOL)
        self.is_hung = _bind(self.user, "IsHungAppWindow", [HANDLE], BOOL)
        self.window_pid = _bind(self.user, "GetWindowThreadProcessId", [HANDLE, ctypes.POINTER(U32)], U32)

    def process_values(self, handle) -> tuple[int, float, ProcessMemory]:
        created, exited, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
        if not self.process_times(handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(kernel), ctypes.byref(user)):
            raise _native_error()
        memory = ProcessMemory(size=ctypes.sizeof(ProcessMemory))
        if not self.process_memory(handle, ctypes.byref(memory), memory.size):
            raise _native_error()
        return created.ticks(), (kernel.ticks() + user.ticks()) / 10_000_000.0, memory

    def hung_processes(self) -> set[int]:
        hung = set()

        @self.window_callback
        def visit(window, _):
            if self.is_visible(window) and self.is_hung(window):
                pid = U32()
                self.window_pid(window, ctypes.byref(pid))
                hung.add(pid.value)
            return True

        if not self.enum_windows(visit, 0):
            raise _native_error()
        return hung


class _Counters:
    PATHS = {
        "disk_queue": r"\PhysicalDisk(_Total)\Current Disk Queue Length",
        "disk_read_ms": r"\PhysicalDisk(_Total)\Avg. Disk sec/Read",
        "disk_write_ms": r"\PhysicalDisk(_Total)\Avg. Disk sec/Write",
        "disk_read_Bps": r"\PhysicalDisk(_Total)\Disk Read Bytes/sec",
        "disk_write_Bps": r"\PhysicalDisk(_Total)\Disk Write Bytes/sec",
        "pages_input_per_sec": r"\Memory\Pages Input/sec",
        "pages_output_per_sec": r"\Memory\Pages Output/sec",
        "gpu_dedicated_MiB": r"\GPU Adapter Memory(*)\Dedicated Usage",
        "gpu_shared_MiB": r"\GPU Adapter Memory(*)\Shared Usage",
    }

    def __init__(self):
        self.pdh = ctypes.WinDLL("pdh", use_last_error=True)
        self.open = _bind(self.pdh, "PdhOpenQueryW", [ctypes.c_wchar_p, SIZE, ctypes.POINTER(HANDLE)], U32)
        self.add = _bind(self.pdh, "PdhAddEnglishCounterW", [HANDLE, ctypes.c_wchar_p, SIZE, ctypes.POINTER(HANDLE)], U32)
        self.collect = _bind(self.pdh, "PdhCollectQueryData", [HANDLE], U32)
        self.read = _bind(self.pdh, "PdhGetFormattedCounterValue", [HANDLE, U32, ctypes.POINTER(U32), ctypes.POINTER(CounterValue)], U32)
        self.array = _bind(self.pdh, "PdhGetFormattedCounterArrayW", [HANDLE, U32, ctypes.POINTER(U32), ctypes.POINTER(U32), HANDLE], U32)
        self.close_query = _bind(self.pdh, "PdhCloseQuery", [HANDLE], U32)
        self.query = HANDLE()
        self.handles, self.missing, self.errors = {}, [], {}
        result = self.open(None, 0, ctypes.byref(self.query))
        if result:
            self.errors["pdh_query"] = result
            self.missing = list(self.PATHS)
            return
        for key, path in self.PATHS.items():
            handle = HANDLE()
            result = self.add(self.query, path, 0, ctypes.byref(handle))
            if result:
                self.missing.append(key)
                self.errors[key] = result
            else:
                self.handles[key] = handle
        self.collect(self.query)

    def scalar(self, key: str, scale: float = 1.0) -> float | None:
        handle = self.handles.get(key)
        if not handle:
            return None
        value = CounterValue()
        result = self.read(handle, PDH_DOUBLE, None, ctypes.byref(value))
        if result or value.status > 1 or not math.isfinite(value.value):
            return None
        return round(value.value * scale, 3)

    def array_mib(self, key: str) -> float | None:
        handle = self.handles.get(key)
        if not handle:
            return None
        size, count = U32(), U32()
        result = self.array(handle, PDH_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if result != PDH_MORE_DATA or not size.value or size.value > MIB:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        result = self.array(handle, PDH_DOUBLE, ctypes.byref(size), ctypes.byref(count), buffer)
        if result or count.value * ctypes.sizeof(CounterItem) > len(buffer):
            return None
        values = ctypes.cast(buffer, ctypes.POINTER(CounterItem))
        valid = [values[index].value.value for index in range(count.value)
                 if values[index].value.status <= 1 and math.isfinite(values[index].value.value)]
        return round(sum(valid) / MIB, 2) if valid else None

    def sample(self) -> dict:
        result = self.collect(self.query) if self.query else 1
        if result:
            self.errors["pdh_collect"] = result
            return dict.fromkeys(self.PATHS)
        self.errors.pop("pdh_collect", None)
        return {key: self.array_mib(key) if key.startswith("gpu_") else self.scalar(
            key, 1000.0 if key in ("disk_read_ms", "disk_write_ms") else 1.0)
            for key in self.PATHS}

    def close(self):
        if self.query:
            self.close_query(self.query)
            self.query = HANDLE()


class Metrics:
    """Collect resource samples and occasional process snapshots on Windows."""

    def __init__(self):
        self.native = _Windows()
        self.counters = _Counters()
        self.missing = list(self.counters.missing)
        self.counter_unavailable = list(self.missing)
        self.errors = dict(self.counters.errors)
        self.logical_processors = os.cpu_count() or 1
        self._system_cpu = None
        self._self_cpu = None
        self._sample_time = None
        self._process_time = None
        self._process_cpu = {}

    def sample(self) -> dict:
        now = time.monotonic()
        memory = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
        if not self.native.memory_status(ctypes.byref(memory)):
            raise _native_error()
        info = PerformanceInfo(size=ctypes.sizeof(PerformanceInfo))
        info_available = bool(self.native.performance_info(ctypes.byref(info), info.size))
        if info_available:
            self.errors.pop("performance_info", None)
        else:
            self.errors["performance_info"] = ctypes.get_last_error()
        idle, kernel, user = FileTime(), FileTime(), FileTime()
        cpu = None
        if self.native.system_times(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            current = (idle.ticks(), kernel.ticks() + user.ticks())
            cpu = cpu_percent(self._system_cpu, current)
            self._system_cpu = current
            self.errors.pop("system_times", None)
        else:
            self.errors["system_times"] = ctypes.get_last_error()
        sample = {
            "cpu_pct": cpu,
            "ram_total_MiB": round(memory.total_physical / MIB, 2),
            "ram_used_MiB": round((memory.total_physical - memory.available_physical) / MIB, 2),
            "ram_available_MiB": round(memory.available_physical / MIB, 2),
            "ram_used_pct": memory.memory_load,
            "commit_MiB": round(info.commit_total * info.page_size / MIB, 2) if info_available else None,
            "commit_limit_MiB": round(info.commit_limit * info.page_size / MIB, 2) if info_available else None,
            "commit_pct": round(100.0 * info.commit_total / info.commit_limit, 2) if info_available and info.commit_limit else None,
            "kernel_nonpaged_MiB": round(info.kernel_nonpaged * info.page_size / MIB, 2) if info_available else None,
            "kernel_paged_MiB": round(info.kernel_paged * info.page_size / MIB, 2) if info_available else None,
        }
        counter_values = self.counters.sample()
        sample.update(counter_values)
        self.counter_unavailable = [key for key, value in counter_values.items() if value is None]
        self.errors.update(self.counters.errors)
        if "pdh_collect" not in self.counters.errors:
            self.errors.pop("pdh_collect", None)
        sample["process_count"] = info.process_count if info_available else None
        try:
            created, seconds, own_memory = self.native.process_values(self.native.current_process())
            current = (created, seconds)
            own_cpu = process_cpu_percent(self._self_cpu, current,
                now - self._sample_time if self._sample_time is not None else 0,
                self.logical_processors, precision=4)
            self._self_cpu = current
            sample["monitor_working_MiB"] = round(own_memory.working_set / MIB, 2)
            sample["monitor_private_MiB"] = round(own_memory.private_usage / MIB, 2)
            sample["monitor_cpu_pct"] = own_cpu
            self.errors.pop("monitor_process", None)
        except OSError as error:
            sample.update(monitor_working_MiB=None, monitor_private_MiB=None, monitor_cpu_pct=None)
            self.errors["monitor_process"] = getattr(error, "winerror", None)
            self._self_cpu = None
        sample["monitor_pid"] = os.getpid()
        self._sample_time = now
        return sample

    def process_snapshot(self) -> dict:
        now = time.monotonic()
        elapsed = now - self._process_time if self._process_time is not None else 0
        hung = self.native.hung_processes()
        handle = self.native.snapshot(0x2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise _native_error()
        items, next_cpu = [], {}
        skipped = 0
        try:
            entry = ProcessEntry(size=ctypes.sizeof(ProcessEntry))
            found = self.native.process_first(handle, ctypes.byref(entry))
            if not found and ctypes.get_last_error() != 18:
                raise _native_error()
            while found:
                pid = entry.pid
                process = self.native.open_process(0x1000, False, pid)
                if process:
                    try:
                        created, seconds, memory = self.native.process_values(process)
                        current = (created, seconds)
                        pct = process_cpu_percent(self._process_cpu.get(pid), current, elapsed,
                                                  self.logical_processors)
                        next_cpu[pid] = current
                        items.append({"name": entry.exe_file, "pid": pid,
                                      "working_MiB": round(memory.working_set / MIB, 2),
                                      "private_MiB": round(memory.private_usage / MIB, 2),
                                      "cpu_pct": pct, "hung_window": pid in hung})
                    except OSError:
                        skipped += 1
                    finally:
                        self.native.close_handle(process)
                else:
                    skipped += 1
                found = self.native.process_next(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:
                raise _native_error()
        finally:
            self.native.close_handle(handle)
        self._process_cpu = next_cpu
        self._process_time = now
        return {"type": "process_snapshot", "utc": datetime.now(timezone.utc).isoformat(),
                "cpu_normalized_to_all_logical_processors": True,
                "selection": "top_private10_top_cpu10_hung_visible_windows",
                "skipped_processes": skipped, "processes": select_processes(items)}

    def close(self):
        self.counters.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
