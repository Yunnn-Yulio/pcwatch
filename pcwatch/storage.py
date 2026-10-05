"""Durable, bounded local logs using only the Python standard library."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Mapping


RESOURCE_FIELDS = (
    "utc", "elapsed_seconds", "cpu_pct", "ram_total_MiB", "ram_used_MiB",
    "ram_available_MiB", "ram_used_pct", "commit_MiB", "commit_limit_MiB",
    "commit_pct", "kernel_nonpaged_MiB", "kernel_paged_MiB",
    "pages_input_per_sec", "pages_output_per_sec", "disk_queue", "disk_read_ms",
    "disk_write_ms", "disk_read_Bps", "disk_write_Bps", "gpu_dedicated_MiB",
    "gpu_shared_MiB", "process_count", "monitor_working_MiB",
    "monitor_private_MiB", "monitor_cpu_pct", "monitor_pid",
)
_JSON_KINDS = frozenset(("processes", "events", "activity"))
_OWN_FILE = re.compile(
    r"^(?:resources-\d{8}-\d{3,6}\.csv|"
    r"(?:processes|events|activity)-\d{8}-\d{3,6}\.jsonl)$"
)
_REPARSE_POINT = 0x400


def utc_now() -> str:
    """Return an unambiguous UTC timestamp, with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _plain_directory(path: Path) -> None:
    info = path.lstat()
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError("The log or state directory must be a regular directory, not a link.")


def _plain_file(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise OSError("A log or state file must be a regular file, not a link.")
    return info


def atomic_json(path: Path, value: object) -> None:
    """Replace a state file only after its complete UTF-8 JSON is flushed."""
    path = Path(os.path.abspath(path))
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    _plain_directory(path.parent)
    _plain_file(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _plain_directory(path.parent)
        _plain_file(path)
        os.replace(temporary_path, path)
    finally:
        try:
            _plain_directory(path.parent)
            if _plain_file(temporary_path) is not None:
                temporary_path.unlink()
        except OSError:
            pass


class LogStore:
    """Write resources CSV and allowlisted JSONL streams below ``root/Logs``.

    Cleanup touches only this recorder's regular log files. Retention uses file
    modification time; size cleanup removes oldest files first. A single record
    larger than the configured segment size is kept intact in its own segment.
    Files are closed after every durable write, so cleanup has no open writers.
    """

    def __init__(
        self,
        root: Path,
        max_bytes: int = 100 * 1024 * 1024,
        segment_bytes: int = 5 * 1024 * 1024,
        retention_days: int = 14,
    ) -> None:
        if min(max_bytes, segment_bytes, retention_days) <= 0:
            raise ValueError("Log limits must be positive.")
        self.root = Path(os.path.abspath(root))
        self.log_dir = self.root / "Logs"
        self.max_bytes = max_bytes
        self.segment_bytes = min(segment_bytes, max_bytes)
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._segments: dict[tuple[str, str], int] = {}
        self._closed = False
        self.root.mkdir(parents=True, exist_ok=True)
        _plain_directory(self.root)
        self.log_dir.mkdir(exist_ok=True)
        self._guard()

    def _guard(self) -> None:
        _plain_directory(self.root)
        _plain_directory(self.log_dir)

    def _append(self, kind: str, payload: bytes, header: bytes = b"") -> None:
        extension = "csv" if kind == "resources" else "jsonl"
        with self._lock:
            if self._closed:
                raise ValueError("The log store is closed.")
            self._guard()
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            key = (kind, day)
            segment = self._segments.get(key, 1)
            while True:
                if segment > 999999:
                    raise OSError("The daily log segment limit was reached.")
                path = self.log_dir / f"{kind}-{day}-{segment:03d}.{extension}"
                info = _plain_file(path)
                size = info.st_size if info is not None else 0
                if size == 0 or size + len(payload) <= self.segment_bytes:
                    break
                segment += 1
            self._segments = {entry: index for entry, index in self._segments.items() if entry[1] == day}
            self._segments[key] = segment
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                actual = os.fstat(descriptor)
                if _is_link(actual) or not stat.S_ISREG(actual.st_mode):
                    raise OSError("The log file must be a regular file.")
                original_size = actual.st_size
                data = (header if original_size == 0 else b"") + payload
                try:
                    remaining = memoryview(data)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written == 0:
                            raise OSError("Writing the log made no progress.")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                except BaseException:
                    # Best-effort rollback preserves line boundaries after a
                    # partial write. The original error still reaches callers.
                    try:
                        os.ftruncate(descriptor, original_size)
                    except OSError:
                        pass
                    raise
            finally:
                os.close(descriptor)

    def write_json(self, kind: str, record: Mapping[str, object]) -> None:
        if kind not in _JSON_KINDS:
            raise ValueError("Unknown JSON log stream.")
        payload = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._append(kind, payload)

    def write_resource(self, record: Mapping[str, object]) -> None:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(RESOURCE_FIELDS)
        header = stream.getvalue().encode("utf-8")
        stream.seek(0)
        stream.truncate(0)
        writer.writerow(record.get(field) for field in RESOURCE_FIELDS)
        self._append("resources", stream.getvalue().encode("utf-8"), header)

    def _own_files(self) -> list[tuple[Path, os.stat_result]]:
        files = []
        for path in self.log_dir.iterdir():
            if not _OWN_FILE.fullmatch(path.name):
                continue
            try:
                info = _plain_file(path)
            except OSError:
                # A link is never followed, counted toward size, or removed.
                continue
            if info is not None:
                files.append((path, info))
        return sorted(files, key=lambda item: (item[1].st_mtime, item[0].name))

    def _delete(self, path: Path) -> bool:
        self._guard()
        if path.parent != self.log_dir or not _OWN_FILE.fullmatch(path.name):
            raise ValueError("Cleanup may only remove recorder log files.")
        if _plain_file(path) is None:
            return False
        path.unlink()
        return True

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                raise ValueError("The log store is closed.")
            self._guard()
            cutoff = time.time() - self.retention_days * 86400
            for path, info in self._own_files():
                if info.st_mtime < cutoff:
                    self._delete(path)
            files = self._own_files()
            total = sum(info.st_size for _, info in files)
            for path, info in files:
                if total <= self.max_bytes:
                    break
                if self._delete(path):
                    total -= info.st_size

    def close(self) -> None:
        with self._lock:
            self._closed = True
