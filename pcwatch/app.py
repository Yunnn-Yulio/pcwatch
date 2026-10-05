"""Sampling, component health, command-line controls, and the tray lifecycle."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time

from . import __version__
from .events import EventCollector
from .metrics import Metrics
from .storage import LogStore, atomic_json, utc_now
from . import startup
from .windows import MonitorSignal, NativeWindow, signal_stop, set_below_normal_priority


def error_metadata(error):
    """Do not put exception messages, paths, or arbitrary payloads in logs."""
    return {"error_type": type(error).__name__, "error_code": getattr(error, "winerror", None) or getattr(error, "errno", None)}


def default_data_root():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PCWatch"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="PCWatch: local Windows diagnostic logs, without external packages.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", type=Path, default=default_data_root())
    parser.add_argument("--interval", type=int, default=15, help="Resource sampling seconds (2-300)")
    parser.add_argument("--details", type=int, default=60, help="Process/event sampling seconds (interval-3600)")
    parser.add_argument("--max-seconds", type=int, default=0, help="Stop after this duration; 0 runs until stopped")
    parser.add_argument("--max-log-mb", type=int, default=100)
    parser.add_argument("--segment-kb", type=int, default=5120)
    parser.add_argument("--retention-days", type=int, default=14)
    parser.add_argument("--no-tray", action="store_true", help="Keep collecting power events without a tray icon")
    controls = parser.add_mutually_exclusive_group()
    for flag in ("stop", "install-startup", "remove-startup", "cleanup-only"):
        controls.add_argument("--" + flag, action="store_true")
    options = parser.parse_args(argv)
    for name, low, high in (("interval", 2, 300), ("details", options.interval, 3600), ("max_seconds", 0, 86400), ("max_log_mb", 1, 1024), ("segment_kb", 16, 65536), ("retention_days", 1, 365)):
        if not low <= getattr(options, name) <= high:
            parser.error(f"--{name.replace('_', '-')} must be between {low} and {high}")
    options.data_dir = Path(os.path.abspath(options.data_dir))
    return options


class Monitor:
    def __init__(self, options, signal, store=None):
        self.options = options
        self.signal = signal
        self.logs = store or LogStore(options.data_dir, max_bytes=options.max_log_mb * 1048576,
                                     segment_bytes=min(options.segment_kb * 1024, options.max_log_mb * 1048576 // 4),
                                     retention_days=options.retention_days)
        self.lock = threading.RLock()
        self.errors = {}
        self.collection_errors = {}
        self.reported_errors = set()
        self.latest = {}
        self.last_sample_utc = None
        self.started = time.monotonic()
        self.finished = threading.Event()
        self.window = None
        self.coverage = {}
        self.startup_enabled = startup.is_enabled(options.data_dir)

    def set_log_error(self, component, error=None):
        with self.lock:
            if error is None:
                self.errors.pop(component, None)
            else:
                self.errors[component] = error_metadata(error)
            if self.window:
                self.window.set_health(not self.errors)

    def write_json(self, kind, record):
        try:
            self.logs.write_json(kind, record)
        except Exception as error:
            self.set_log_error(kind, error)
            raise  # Event cursors must not advance after a failed log write.
        self.set_log_error(kind)

    def emit(self, record):
        try:
            self.write_json("activity", record)
        except Exception:
            pass

    def collection_error(self, component, error):
        info = error_metadata(error)
        with self.lock:
            self.collection_errors[component] = info
            key = (component, info["error_type"], info["error_code"])
            if key in self.reported_errors:
                return
            self.reported_errors.add(key)
        self.emit({"type": "collection_error", "component": component, "utc": utc_now(), **info})

    def status(self, running=True, include_status_error=True):
        with self.lock:
            errors = {key: value.copy() for key, value in self.errors.items() if include_status_error or key != "status"}
            return {**self.latest, "utc": self.latest.get("utc", utc_now()), "running": running,
                    "monitor_pid": os.getpid(), "monitor_version": __version__, "implementation": "python",
                    "startup_enabled": self.startup_enabled, "last_sample_utc": self.last_sample_utc,
                    "logging_healthy": not errors, "log_error_components": sorted(errors), "log_errors": errors,
                    "collection_errors": dict(self.collection_errors), "collection_coverage": dict(self.coverage)}

    def save_status(self, running=True):
        with self.lock:
            try:
                atomic_json(self.options.data_dir / "status.json", self.status(running, include_status_error=False))
            except Exception as error:
                self.set_log_error("status", error)
            else:
                self.set_log_error("status")

    def status_text(self):
        status = self.status()
        lines = ["PCWatch is running." if status["logging_healthy"] else "PCWatch: log storage error.",
                 "Last saved sample (UTC): " + (status["last_sample_utc"] or "none this session"),
                 f"Samples every {self.options.interval} seconds; details every {self.options.details} seconds."]
        for key, label in (("cpu_pct", "CPU (%)"), ("ram_available_MiB", "Available RAM (MiB)"), ("monitor_working_MiB", "Monitor RAM (MiB)")):
            lines.append(f"{label}: {status.get(key)}")
        if status["log_error_components"]:
            lines.append("Failed storage components: " + ", ".join(status["log_error_components"]))
        if status["collection_errors"]:
            lines.append("Limited collection: " + ", ".join(sorted(status["collection_errors"])))
        lines.extend([f"Log target: {self.options.max_log_mb} MiB; retention: {self.options.retention_days} days.", str(self.options.data_dir / "Logs")])
        return "\n".join(lines)

    def disable_startup(self):
        startup.remove(self.options.data_dir)
        self.startup_enabled = False
        self.signal.set()

    def check_previous_session(self):
        try:
            path = self.options.data_dir / "status.json"
            if path.stat().st_size > 65536:
                return
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("running"):
                self.emit({"type": "previous_monitor_session_without_stop", "utc": utc_now(),
                           "last_sample_utc": previous.get("last_sample_utc"), "interpretation": "previous_session_end_cause_unknown"})
        except (OSError, ValueError, AttributeError):
            pass

    def run(self):
        metrics = events = None
        try:
            self.check_previous_session()
            self.emit({"type": "start", "utc": utc_now(), "pid": os.getpid(), "monitor_version": __version__,
                       "implementation": "python", "interval_seconds": self.options.interval, "details_seconds": self.options.details,
                       "max_log_mib": self.options.max_log_mb, "retention_days": self.options.retention_days})
            self.emit({"type": "baseline", "utc": utc_now(), "logical_processors": os.cpu_count(),
                       "os_version": platform.win32_ver()[1], "python_version": platform.python_version(), "architecture": platform.machine()})
            metrics = Metrics()
            events = EventCollector(self.options.data_dir / "event-cursors.json", lambda record: self.write_json("events", record))
            self.emit({"type": "counter_coverage", "utc": utc_now(), "missing": metrics.missing})
            last_mono = last_wall = None
            last_detail = None
            while not self.signal.is_set():
                cycle = time.monotonic()
                wall = datetime.now(timezone.utc)
                elapsed = cycle - last_mono if last_mono is not None else 0.0
                if last_wall is not None:
                    wall_elapsed = (wall - last_wall).total_seconds()
                    if max(elapsed, wall_elapsed) > self.options.interval * 2.5:
                        self.emit({"type": "sampling_gap", "utc": utc_now(), "seconds": round(wall_elapsed, 2),
                                   "monotonic_seconds": round(elapsed, 2), "interpretation": "sampling_interrupted_cause_unknown"})
                try:
                    sample = {**metrics.sample(), "utc": utc_now(), "elapsed_seconds": round(elapsed, 3)}
                    with self.lock:
                        self.latest = sample
                        self.collection_errors.pop("resources", None)
                        self.coverage["missing_counters"] = list(metrics.missing)
                        self.coverage["unavailable_counters"] = list(metrics.counter_unavailable)
                        self.coverage["native_errors"] = dict(metrics.errors)
                    try:
                        self.logs.write_resource(sample)
                    except Exception as error:
                        self.set_log_error("resources", error)
                    else:
                        self.set_log_error("resources")
                        with self.lock:
                            self.last_sample_utc = sample["utc"]
                except Exception as error:
                    self.collection_error("resources", error)
                last_mono, last_wall = cycle, wall
                if last_detail is None or cycle - last_detail >= self.options.details:
                    for name, action in (("processes", lambda: self.write_json("processes", {**metrics.process_snapshot(), "utc": utc_now()})),
                                         ("events", events.poll), ("retention", self.logs.cleanup)):
                        try:
                            action()
                        except Exception as error:
                            self.collection_error(name, error)
                        else:
                            with self.lock:
                                self.collection_errors.pop(name, None)
                    self.startup_enabled = startup.is_enabled(self.options.data_dir)
                    with self.lock:
                        self.coverage["event_channels"] = events.coverage
                    if events.state_error:
                        with self.lock:
                            self.errors["event_state"] = events.state_error
                        if self.window:
                            self.window.set_health(False)
                    else:
                        self.set_log_error("event_state")
                    last_detail = cycle
                self.save_status()
                remaining = max(0.0, self.options.interval - (time.monotonic() - cycle))
                if self.options.max_seconds:
                    duration_left = self.options.max_seconds - (time.monotonic() - self.started)
                    if duration_left <= 0:
                        self.signal.set()
                        break
                    remaining = min(remaining, duration_left)
                self.signal.wait(remaining)
        except Exception as error:
            self.collection_error("worker", error)
        finally:
            for resource in (events, metrics):
                if resource:
                    try:
                        resource.close()
                    except Exception as error:
                        self.collection_error("close", error)
            self.emit({"type": "stop", "utc": utc_now()})
            self.save_status(False)
            self.logs.close()
            self.finished.set()


def main(argv=None):
    options = parse_args(argv)
    if os.name != "nt":
        if sys.stderr:
            print("PCWatch requires Windows.", file=sys.stderr)
        return 1
    try:
        if options.stop:
            signal_stop(options.data_dir)
            return 0
        if options.install_startup:
            startup.install(options.data_dir)
            return 0
        if options.remove_startup:
            startup.remove(options.data_dir)
            signal_stop(options.data_dir)
            return 0
        with MonitorSignal(options.data_dir) as signal:
            if not signal.acquired:
                return 0
            set_below_normal_priority()
            if options.cleanup_only:
                with_store = LogStore(options.data_dir, max_bytes=options.max_log_mb * 1048576, retention_days=options.retention_days)
                try:
                    with_store.cleanup()
                finally:
                    with_store.close()
                return 0
            monitor = Monitor(options, signal)
            monitor.window = NativeWindow(monitor.emit, monitor.status_text, signal.set, monitor.disable_startup,
                                          options.data_dir / "Logs", options.interval, not options.no_tray)
            worker = threading.Thread(target=monitor.run, name="PCWatch sampler", daemon=True)
            worker.start()
            try:
                monitor.window.run(monitor.finished)
            finally:
                signal.set()
                worker.join(15)
            return 1 if worker.is_alive() or "worker" in monitor.collection_errors else 0
    except Exception as error:
        try:
            options.data_dir.mkdir(parents=True, exist_ok=True)
            atomic_json(options.data_dir / "last-error.json", {"utc": utc_now(), **error_metadata(error)})
        except Exception:
            pass
        if sys.stderr:
            print(f"PCWatch failed: {type(error).__name__} (code {error_metadata(error)['error_code']}).", file=sys.stderr)
        return 1
