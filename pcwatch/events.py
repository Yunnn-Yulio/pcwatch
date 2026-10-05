"""Read a small, privacy-filtered subset of the local Windows Event Log.

The native API is loaded only when polling starts on Windows. Parsing, cursor
handling and tests require only the Python standard library. Raw XML, rendered
messages, computer/user names, source paths and hardware serials never reach
the output callback.
"""

from __future__ import annotations

import ctypes
import json
import ntpath
import os
from pathlib import Path
import re
import struct
from typing import Callable, Iterator
import uuid
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

from .storage import atomic_json


LOG_NAMES = ("System", "Application")
MAXIMUM_EVENTS_PER_LOG = 100
MAX_XML_BYTES = 8 * 1024 * 1024
_NAMESPACE = "{http://schemas.microsoft.com/win/2004/08/events/event}"

_NUMERIC_FIELDS = frozenset(name.casefold() for name in """
BugcheckCode BugcheckParameter1 BugcheckParameter2 BugcheckParameter3
BugcheckParameter4 SleepInProgress PowerButtonTimestamp BootAppStatus Checkpoint
CheckpointStatus ConnectedStandbyInProgress SystemSleepTransitionsToOn
CsEntryScenarioInstanceId CsEntryScenarioInstanceIdV2 BugcheckInfoFromEFI
LongPowerButtonPressDetected LidReliability InputSuppressionState
PowerButtonSuppressionState LidState WHEABootErrorCount ErrorSource ApicId
MCABank MciStat MciAddr MciMisc ErrorType TransactionType Participation RequestType
MemorIO MemHierarchyLvl Timeout OperationType Channel Length ValidBits ErrorStatus
PhysicalAddress PhysicalAddressMask Node Card Module Bank Device Row Column
BitPosition RankNumber CardHandle ModuleHandle Bus DeviceId Function Segment
VendorID ClassCode Command Status BridgeControl BridgeStatus SleepState TargetState
EffectiveState Reason Flags WakeSourceType WakeSourceLength TransitionsToOn
ShutdownActionType ShutdownEventCode ShutdownReason Group Number
CapDurationInSeconds PpcChanges TpcChanges PccChanges SystemCommitLimit
SystemCommitCharge ProcessCommitCharge PagedPoolUsage NonPagedPoolUsage
PhysicalMemorySize PhysicalMemoryUsage NumProcesses ExceptionCode FaultingOffset
ProcessId ProcessCreationTime StartTime TerminationTime HangType BucketType CabId
AppTimeStamp ModuleTimeStamp
""".split())
_APP_FIELDS = frozenset(name.casefold() for name in """
AppName ApplicationName ModuleName FaultingApplicationName FaultingModuleName
ExeFileName
""".split())
_WER_EVENT_NAMES = frozenset(name.casefold() for name in """
APPCRASH APPHANG AppHangB1 AppHangXProcB1 AppHangTransient BEX BEX64 BlueScreen
LiveKernelEvent CLR20r3 MoAppCrash MoAppHang MoBEX MoBEX64 RADAR_PRE_LEAK_32
RADAR_PRE_LEAK_64 WindowsUpdateFailure3 PnPDeviceProblemCode
""".split())
_NUMERIC_VALUE = re.compile(r"(?:0[xX][0-9a-fA-F]{1,32}|[0-9]{1,24}|[0-9a-fA-F]{1,16})\Z")
_SAFE_APP_NAME = re.compile(r"[A-Za-z0-9 _+.()\[\]-]{1,120}\.(?:exe|dll|sys|ocx|com|scr)\Z", re.I)
_HEX = re.compile(r"[0-9a-fA-F]+\Z")

_FILTERS = {
    "Application": (
        "Provider[@Name='Application Hang'] and EventID=1002",
        "Provider[@Name='Application Error'] and EventID=1000",
        "Provider[@Name='Windows Error Reporting'] and EventID=1001",
    ),
    "System": (
        "Provider[@Name='Microsoft-Windows-WHEA-Logger']",
        "Provider[@Name='Microsoft-Windows-Resource-Exhaustion-Detector'] and EventID=2004",
        "Provider[@Name='Display'] and EventID=4101",
        "Provider[@Name='nvlddmkm']",
        "(Provider[@Name='disk'] or Provider[@Name='Disk']) and (EventID=7 or EventID=51 or EventID=153 or EventID=157)",
        "(Provider[@Name='stornvme'] or Provider[@Name='storahci']) and EventID=129",
        "Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=41 or EventID=42 or EventID=107 or EventID=506 or EventID=507)",
        "Provider[@Name='Microsoft-Windows-Kernel-Processor-Power'] and EventID=37",
        "Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1",
        "Provider[@Name='EventLog'] and (EventID=6005 or EventID=6006 or EventID=6008)",
    ),
}
_PROVIDER_NAMES = frozenset((
    "Application Hang", "Application Error", "Windows Error Reporting",
    "Microsoft-Windows-WHEA-Logger", "Microsoft-Windows-Resource-Exhaustion-Detector",
    "Display", "nvlddmkm", "disk", "Disk", "stornvme", "storahci",
    "Microsoft-Windows-Kernel-Power", "Microsoft-Windows-Kernel-Processor-Power",
    "Microsoft-Windows-Power-Troubleshooter", "EventLog",
))


def _utc(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _valid_utc(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() == timedelta(0)
    except ValueError:
        return False


def _xml_root(xml: str) -> ET.Element:
    if not isinstance(xml, str) or len(xml) > MAX_XML_BYTES // 2:
        raise ValueError("Invalid event XML length")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("Event XML declarations are prohibited")
    root = ET.fromstring(xml)
    if root.tag not in (_NAMESPACE + "Event", "Event"):
        raise ValueError("Invalid event XML root")
    return root


def _child(parent: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in parent if child.tag in (_NAMESPACE + name, name)), None)


def _number(parent: ET.Element, name: str, *, required: bool = False) -> int | None:
    child = _child(parent, name)
    value = child.text if child is not None else None
    if value is None:
        if required:
            raise ValueError("Missing event numeric metadata")
        return None
    if not re.fullmatch(r"[0-9]{1,20}", value.strip()):
        raise ValueError("Invalid event numeric metadata")
    return int(value)


def _record_id(xml: str) -> int:
    system = _child(_xml_root(xml), "System")
    if system is None:
        raise ValueError("Missing event system metadata")
    return _number(system, "EventRecordID", required=True)  # type: ignore[return-value]


def _add_allowed_field(data: dict, name: str, value: str) -> None:
    value = value.strip()
    lowered = name.casefold()
    if lowered in _NUMERIC_FIELDS:
        if value.casefold() in ("true", "false"):
            data[name] = value.casefold() == "true"
        elif _NUMERIC_VALUE.fullmatch(value):
            data[name] = value
    elif lowered in _APP_FIELDS:
        basename = ntpath.basename(value)
        if _SAFE_APP_NAME.fullmatch(basename):
            data[name] = basename
    elif lowered == "eventname" and value.casefold() in _WER_EVENT_NAMES:
        data[name] = value


def cper_summary(raw_hex: str) -> dict:
    """Return CPER format metadata without retaining any error-record payload.

    GUIDs identify public notification/section types. Firmware identifiers,
    serials, record IDs and binary section contents are intentionally omitted.
    """
    if (not isinstance(raw_hex, str) or not 256 <= len(raw_hex) <= 2 * 1024 * 1024
            or len(raw_hex) % 2 or not raw_hex[:8].upper() == "43504552"
            or not _HEX.fullmatch(raw_hex)):
        return {}
    header = bytes.fromhex(raw_hex[:256])
    count = struct.unpack_from("<H", header, 10)[0]
    flags = struct.unpack_from("<I", header, 104)[0]
    types = []
    for index in range(min(count, 32)):
        offset = 128 + index * 72 + 16
        if (offset + 16) * 2 > len(raw_hex):
            break
        types.append(str(uuid.UUID(bytes_le=bytes.fromhex(raw_hex[offset * 2:(offset + 16) * 2]))))
    return {
        "cper_severity": struct.unpack_from("<I", header, 12)[0],
        "cper_flags": flags,
        "cper_previous_error": bool(flags & 2),
        "cper_sections_truncated": bool(flags & 64),
        "cper_section_count": count,
        "cper_notification_type": str(uuid.UUID(bytes_le=header[80:96])),
        "cper_section_types": types,
    }


def sanitize_event(xml: str, log: str | None = None) -> dict:
    """Parse event XML into an allowlisted diagnostic record.

    Malformed XML raises a safe exception, so the collector cannot silently
    acknowledge a record it failed to inspect. Exception messages are not logged.
    """
    root = _xml_root(xml)
    system = _child(root, "System")
    if system is None:
        raise ValueError("Missing event system metadata")
    provider_node = _child(system, "Provider")
    provider = provider_node.get("Name") if provider_node is not None else None
    if provider not in _PROVIDER_NAMES:
        raise ValueError("Event provider is outside the diagnostic allowlist")
    channel = _child(system, "Channel")
    if log is None:
        log = channel.text if channel is not None else None
    if log not in LOG_NAMES:
        raise ValueError("Event channel is outside the diagnostic allowlist")
    created = _child(system, "TimeCreated")
    timestamp = created.get("SystemTime") if created is not None else None
    utc = _utc(datetime.fromisoformat(timestamp.replace("Z", "+00:00"))) if _valid_utc(timestamp) else None
    data: dict = {}
    event_data = _child(root, "EventData")
    named_values: dict[str, str] = {}
    if event_data is not None:
        for node in event_data:
            if node.tag not in (_NAMESPACE + "Data", "Data"):
                continue
            name = node.get("Name")
            if name:
                value = "".join(node.itertext())
                named_values[name] = value
                _add_allowed_field(data, name, value)
    if provider == "Microsoft-Windows-WHEA-Logger":
        data.update(cper_summary(named_values.get("RawData", "")))
    event_name = next((value for key, value in data.items() if key.casefold() == "eventname"), "")
    if provider == "Windows Error Reporting" and event_name.casefold() in ("livekernelevent", "bluescreen"):
        code = named_values.get("P1", "").strip()
        if _NUMERIC_VALUE.fullmatch(code):
            data["KernelEventCode"] = code
    user_data = _child(root, "UserData")
    if user_data is not None:
        for node in user_data.iter():
            if not len(node):
                _add_allowed_field(data, node.tag.rsplit("}", 1)[-1], node.text or "")
    return {
        "type": "event", "log": log, "provider": provider,
        "event_id": _number(system, "EventID", required=True),
        "level": _number(system, "Level"), "utc": utc,
        "record_id": _number(system, "EventRecordID", required=True), "data": data,
    }


def build_query(log: str, cursor: dict, latest: int) -> str:
    """Build short XPath selectors to avoid Windows' per-selector complexity cap."""
    if log not in LOG_NAMES or isinstance(latest, bool) or not isinstance(latest, int) or latest < 0:
        raise ValueError("Invalid event query channel or snapshot")
    if cursor["initialized"]:
        record = cursor["cursor"]
        if isinstance(record, bool) or not isinstance(record, int) or record < 0:
            raise ValueError("Invalid event query cursor")
        range_filter = f"EventRecordID > {record}"
    else:
        start = cursor["start_utc"]
        if not _valid_utc(start):
            raise ValueError("Invalid event query start time")
        # Normalize before interpolation; even loaded state cannot inject XPath.
        start = _utc(datetime.fromisoformat(start.replace("Z", "+00:00")))
        range_filter = f"TimeCreated[@SystemTime >= '{start}']"
    root = ET.Element("QueryList")
    query = ET.SubElement(root, "Query", Id="0")
    for selector in _FILTERS[log]:
        ET.SubElement(query, "Select", Path=log).text = (
            f"*[System[({selector}) and ({range_filter} and EventRecordID <= {latest})]]"
        )
    return ET.tostring(root, encoding="unicode")


class _WindowsEventBackend:
    """Owned synchronous query handles; no subscription or background thread."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Event Log is available only on Windows")
        self._api = ctypes.WinDLL("wevtapi", use_last_error=True)
        handle = ctypes.c_void_p
        dword = ctypes.c_uint32
        boolean = ctypes.c_int
        self._query = self._api.EvtQuery
        self._query.argtypes = (handle, ctypes.c_wchar_p, ctypes.c_wchar_p, dword)
        self._query.restype = handle
        self._next = self._api.EvtNext
        self._next.argtypes = (handle, dword, ctypes.POINTER(handle), dword, dword, ctypes.POINTER(dword))
        self._next.restype = boolean
        self._render = self._api.EvtRender
        self._render.argtypes = (handle, handle, dword, dword, ctypes.c_void_p, ctypes.POINTER(dword), ctypes.POINTER(dword))
        self._render.restype = boolean
        self._close = self._api.EvtClose
        self._close.argtypes = (handle,)
        self._close.restype = boolean

    def _render_xml(self, event: int) -> str:
        used = ctypes.c_uint32()
        properties = ctypes.c_uint32()
        ctypes.set_last_error(0)
        ok = self._render(None, event, 1, 0, None, ctypes.byref(used), ctypes.byref(properties))
        error = ctypes.get_last_error()
        if not ok and error != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(error)
        if not 2 <= used.value <= MAX_XML_BYTES:
            raise ValueError("Invalid rendered event buffer size")
        buffer = ctypes.create_unicode_buffer((used.value + ctypes.sizeof(ctypes.c_wchar) - 1) // ctypes.sizeof(ctypes.c_wchar))
        ctypes.set_last_error(0)
        if not self._render(None, event, 1, ctypes.sizeof(buffer), buffer, ctypes.byref(used), ctypes.byref(properties)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def events(self, log: str, query: str, *, reverse: bool = False) -> Iterator[str]:
        if log not in LOG_NAMES:
            raise ValueError("Invalid event channel")
        ctypes.set_last_error(0)
        # EvtQueryChannelPath=1, ForwardDirection=0x100, ReverseDirection=0x200.
        result = self._query(None, log, query, 1 | (0x200 if reverse else 0x100))
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            while True:
                events = (ctypes.c_void_p * 1)()
                returned = ctypes.c_uint32()
                ctypes.set_last_error(0)
                if not self._next(result, 1, events, 1000, 0, ctypes.byref(returned)):
                    error = ctypes.get_last_error()
                    if error == 259:  # ERROR_NO_MORE_ITEMS
                        return
                    raise ctypes.WinError(error)
                if returned.value != 1 or not events[0]:
                    raise ValueError("Invalid event handle result")
                try:
                    yield self._render_xml(events[0])
                finally:
                    self._close(events[0])
        finally:
            self._close(result)

    def latest_record_id(self, log: str) -> int:
        records = self.events(log, "*", reverse=True)
        try:
            xml = next(records, None)
            return 0 if xml is None else _record_id(xml)
        finally:
            records.close()

    def close(self) -> None:
        # Every query and event handle is closed by its iterator's finally block.
        pass


class EventEmissionError(RuntimeError):
    """The log writer rejected output; the rejected event remains unacknowledged."""


def _error_code(error: Exception) -> int | None:
    return getattr(error, "winerror", None) or getattr(error, "errno", None)


def _new_cursor() -> dict:
    return {"cursor": 0, "initialized": False, "start_utc": _utc(datetime.now(timezone.utc) - timedelta(hours=2))}


class EventCollector:
    """Poll System/Application events without replaying the entire log on restart.

    ``emit`` must return only after its record is successfully written. If it
    raises, the cursor for that record never advances. ``backend`` is an optional
    test double providing latest_record_id(log), events(log, query), close().
    """

    def __init__(self, state_file: Path, emit: Callable[[dict], None], backend=None):
        if not callable(emit):
            raise TypeError("emit must be callable")
        self.state_file = Path(state_file)
        self.emit = emit
        self._backend = backend
        self._cursors = {log: _new_cursor() for log in LOG_NAMES}
        self._failures: dict[str, tuple] = {}
        self._unavailable: set[str] = set()
        self._covered: set[str] = set()
        self._state_failure: tuple | None = None
        self._reported_state_failure: tuple | None = None
        self._closed = False
        self._load_state()

    def _emit(self, record: dict) -> None:
        try:
            self.emit(record)
        except Exception as error:
            raise EventEmissionError("Event output could not be saved") from error

    def _coverage(self, log: str, status: str, error: Exception | None = None) -> None:
        record = {"type": "event_coverage", "utc": _utc(), "log": log, "status": status}
        if error is not None:
            record.update(error_type=type(error).__name__, error_code=_error_code(error))
        self._emit(record)

    def _report_state_error(self) -> None:
        key = self._state_failure
        if key is None or key == self._reported_state_failure:
            return
        self._emit({"type": "event_state_error", "utc": _utc(), "error_type": key[0], "error_code": key[1]})
        self._reported_state_failure = key

    def _state_error(self, error: Exception, *, report: bool = True) -> None:
        self._state_failure = (type(error).__name__, _error_code(error))
        if report:
            self._report_state_error()

    def _load_state(self) -> None:
        try:
            if not self.state_file.exists():
                return
            if self.state_file.stat().st_size > 65536:
                raise ValueError("Event cursor state is too large")
            saved = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
            # The original C# monitor used PascalCase cursor properties. Import
            # its state once, then persist the same cursors in the Python format.
            if isinstance(saved, dict) and "version" not in saved and "Version" in saved:
                legacy_logs = saved.get("Logs")
                if not isinstance(legacy_logs, dict):
                    raise ValueError("Invalid legacy event cursor state")
                converted_logs = {}
                for log in LOG_NAMES:
                    legacy = legacy_logs.get(log)
                    if legacy is not None:
                        if not isinstance(legacy, dict):
                            raise ValueError("Invalid legacy event cursor state")
                        converted_logs[log] = {
                            "cursor": legacy.get("Cursor"),
                            "initialized": legacy.get("Initialized"),
                            "start_utc": legacy.get("StartUtc"),
                        }
                saved = {"version": saved["Version"], "logs": converted_logs}
            if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("logs"), dict):
                raise ValueError("Invalid event cursor state")
            restored = {}
            for log in LOG_NAMES:
                value = saved["logs"].get(log)
                if value is None:
                    continue
                if (not isinstance(value, dict) or isinstance(value.get("cursor"), bool)
                        or not isinstance(value.get("cursor"), int) or value["cursor"] < 0
                        or not isinstance(value.get("initialized"), bool)
                        or not _valid_utc(value.get("start_utc"))):
                    raise ValueError("Invalid event cursor state")
                restored[log] = {"cursor": value["cursor"], "initialized": value["initialized"], "start_utc": value["start_utc"]}
            self._cursors.update(restored)
        except (OSError, ValueError, TypeError) as error:
            # Construction must not depend on a currently writable log sink.
            # Keep health metadata and retry publishing it from poll instead.
            self._state_error(error, report=False)

    def _save_state(self) -> None:
        try:
            atomic_json(self.state_file, {"version": 1, "logs": self._cursors})
            self._state_failure = None
            self._reported_state_failure = None
        except OSError as error:
            self._state_error(error)

    def _poll_log(self, log: str) -> None:
        if self._backend is None:
            self._backend = _WindowsEventBackend()
        latest = self._backend.latest_record_id(log)
        if isinstance(latest, bool) or not isinstance(latest, int) or latest < 0:
            raise ValueError("Invalid latest event record ID")
        state = self._cursors[log]
        if state["initialized"] and latest < state["cursor"]:
            self._coverage(log, "reset")
            state = self._cursors[log] = _new_cursor()
        if log not in self._covered:
            self._coverage(log, "available")
            self._covered.add(log)
        if state["initialized"] and latest == state["cursor"]:
            return
        if latest == 0:
            state.update(cursor=0, initialized=True)
            return
        records = self._backend.events(log, build_query(log, state, latest))
        count = 0
        try:
            for xml in records:
                record = sanitize_event(xml, log)
                record_id = record["record_id"]
                if record_id > latest or (state["initialized"] and record_id <= state["cursor"]):
                    raise ValueError("Event query yielded an out-of-range record")
                self._emit(record)
                state["cursor"] = max(state["cursor"], record_id)
                state["initialized"] = True
                count += 1
                if count >= MAXIMUM_EVENTS_PER_LOG:
                    break
        finally:
            close = getattr(records, "close", None)
            if close is not None:
                close()
        # Only a completely drained query can acknowledge unmatched records.
        if count < MAXIMUM_EVENTS_PER_LOG:
            state.update(cursor=latest, initialized=True)

    def poll(self) -> None:
        if self._closed:
            raise RuntimeError("EventCollector is closed")
        self._report_state_error()
        for log in LOG_NAMES:
            if log in self._unavailable:
                continue
            try:
                self._poll_log(log)
                if log in self._failures:
                    self._coverage(log, "available")
                self._failures.pop(log, None)
            except EventEmissionError:
                raise
            except Exception as error:
                code = _error_code(error)
                key = (type(error).__name__, code)
                permanent = isinstance(error, PermissionError) or code in (5, 1314, 15007)
                if self._failures.get(log) != key:
                    self._coverage(log, "unavailable" if permanent else "error", error)
                    self._failures[log] = key
                if permanent:
                    self._unavailable.add(log)
        self._save_state()

    @property
    def coverage(self) -> dict[str, str]:
        """Snapshot channel availability without exposing exception text."""
        return {
            log: ("unavailable" if log in self._unavailable else
                  "error" if log in self._failures else
                  "available" if log in self._covered else "pending")
            for log in LOG_NAMES
        }

    @property
    def state_error(self) -> dict[str, str | int | None] | None:
        """Current cursor-state failure metadata, cleared by successful saving."""
        if self._state_failure is None:
            return None
        return {"error_type": self._state_failure[0], "error_code": self._state_failure[1]}

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._backend is not None:
                self._backend.close()
