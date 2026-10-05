"""Event privacy, backlog, failure and cursor persistence regression tests."""

import json
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

from pcwatch.events import (
    EventCollector, EventEmissionError, build_query, cper_summary, sanitize_event,
)


def event_xml(record_id=1, provider="Microsoft-Windows-Kernel-Power", event_id=41,
              channel="System", data="", user_data=""):
    return f'''<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
<System><Provider Name="{provider}"/><EventID>{event_id}</EventID><Level>2</Level>
<TimeCreated SystemTime="2026-10-04T12:34:56.1234567Z"/>
<EventRecordID>{record_id}</EventRecordID><Channel>{channel}</Channel>
<Computer>PRIVATE-HOST</Computer><Security UserID="S-1-5-21-PRIVATE"/></System>
<EventData>{data}</EventData>{user_data}</Event>'''


class FakeBackend:
    def __init__(self, records=None, latest=None):
        self.records = records or {"System": [], "Application": []}
        self.latest = latest or {
            log: max((sanitize_event(xml, log)["record_id"] for xml in values), default=0)
            for log, values in self.records.items()
        }
        self.failures = {}
        self.calls = []
        self.closed = False
        self.iterators_closed = 0

    def latest_record_id(self, log):
        self.calls.append(("latest", log))
        if log in self.failures:
            raise self.failures[log]
        return self.latest[log]

    def events(self, log, query):
        self.calls.append(("events", log, query))
        selectors = " ".join(node.text or "" for node in ET.fromstring(query).iter("Select"))
        lower = re.search(r"EventRecordID > (\d+)", selectors)
        upper = re.search(r"EventRecordID <= (\d+)", selectors)
        low = int(lower.group(1)) if lower else 0
        high = int(upper.group(1))
        try:
            for xml in self.records[log]:
                match = re.search(r"<EventRecordID>(\d+)</EventRecordID>", xml)
                record = int(match.group(1)) if match else low + 1
                if low < record <= high:
                    yield xml
        finally:
            self.iterators_closed += 1

    def close(self):
        self.closed = True


class EventPrivacyTests(unittest.TestCase):
    def test_only_allowed_values_survive(self):
        xml = event_xml(data='''
<Data Name="BugcheckCode">278</Data>
<Data Name="SleepInProgress">true</Data>
<Data Name="ExceptionCode">0xc0000005</Data>
<Data Name="ModuleName">C:\\Users\\secret-person\\Documents\\driver.sys</Data>
<Data Name="AppName">/private/path/program.exe</Data>
<Data Name="ProcessId">C:\\PRIVATE</Data>
<Data Name="UserName">PRIVATE USER</Data>
<Data Name="SerialNumber">PRIVATE-SERIAL</Data>
<Data Name="AppPath">C:\\PRIVATE\\program.exe</Data>
<Data Name="RawData">DEADBEEF</Data>
<Data>unstructured PRIVATE MESSAGE</Data>''')
        value = sanitize_event(xml)
        self.assertEqual(value["record_id"], 1)
        self.assertEqual(value["event_id"], 41)
        self.assertEqual(value["utc"], "2026-10-04T12:34:56.123456Z")
        self.assertEqual(value["data"], {
            "BugcheckCode": "278", "SleepInProgress": True,
            "ExceptionCode": "0xc0000005", "ModuleName": "driver.sys",
            "AppName": "program.exe",
        })
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("secret-person", json.dumps(value))

    def test_application_basename_validation(self):
        for name in ("invoice.pdf", "https://example.com/a.exe?token=secret", "bad\nname.exe", "报告.exe"):
            with self.subTest(name=name):
                root = ET.fromstring(event_xml())
                event_data = root.find("{http://schemas.microsoft.com/win/2004/08/events/event}EventData")
                ET.SubElement(event_data, "Data", Name="AppName").text = name
                self.assertNotIn("AppName", sanitize_event(ET.tostring(root, encoding="unicode"))["data"])

    def test_user_data_leaves_receive_same_allowlist(self):
        value = sanitize_event(event_xml(user_data='''
<UserData><Memory xmlns="custom"><PhysicalMemoryUsage>123</PhysicalMemoryUsage>
<UserName>PRIVATE</UserName><Parent><AppName>C:\\Secret\\editor.exe</AppName></Parent>
</Memory></UserData>'''))
        self.assertEqual(value["data"], {"PhysicalMemoryUsage": "123", "AppName": "editor.exe"})

    def test_wer_kernel_code_only_for_relevant_events(self):
        for name, expected in (("LiveKernelEvent", True), ("BlueScreen", True), ("APPCRASH", False), ("PRIVATE", False)):
            with self.subTest(name=name):
                value = sanitize_event(event_xml(provider="Windows Error Reporting", event_id=1001, channel="Application", data=f'''
<Data Name="EventName">{name}</Data><Data Name="P1">141</Data>
<Data Name="P2">PRIVATE</Data><Data Name="ReportId">PRIVATE</Data>'''))
                self.assertEqual("KernelEventCode" in value["data"], expected)
                self.assertNotIn("PRIVATE", json.dumps(value))

    def test_invalid_xml_entities_and_provider_are_rejected(self):
        for xml in ("<broken", '<!DOCTYPE Event [<!ENTITY secret "PRIVATE">]>' + event_xml(),
                    event_xml(provider="PRIVATE-PROVIDER"), event_xml().replace("<EventRecordID>1", "<EventRecordID>invalid")):
            with self.subTest(xml=xml[:40]):
                with self.assertRaises((ValueError, ET.ParseError)):
                    sanitize_event(xml)

    def test_large_xml_is_rejected_before_parsing(self):
        with self.assertRaises(ValueError):
            sanitize_event(" " * (4 * 1024 * 1024 + 1))

    def test_cper_summary_omits_identifier_and_payload(self):
        raw = bytearray(128 + 72 + 64)
        raw[0:4] = b"CPER"
        struct.pack_into("<H", raw, 10, 1)
        struct.pack_into("<I", raw, 12, 1)
        struct.pack_into("<I", raw, 104, 66)
        notification = uuid.UUID("e8f56ffe-919c-4cc5-ba88-65abe14913bb")
        section = uuid.UUID("a5bc1114-6f64-4ede-b863-3e83ed7c83b1")
        raw[80:96] = notification.bytes_le
        raw[128 + 16:128 + 32] = section.bytes_le
        raw[96:104] = b"SERIAL!!"
        raw[-64:] = b"PRIVATEPAYLOAD".ljust(64, b"!")
        value = cper_summary(raw.hex())
        self.assertEqual(value, {
            "cper_severity": 1, "cper_flags": 66, "cper_previous_error": True,
            "cper_sections_truncated": True, "cper_section_count": 1,
            "cper_notification_type": str(notification), "cper_section_types": [str(section)],
        })
        wrapped = sanitize_event(event_xml(provider="Microsoft-Windows-WHEA-Logger", event_id=1,
                                           data=f'<Data Name="RawData">{raw.hex()}</Data>'))
        self.assertEqual(wrapped["data"], value)
        output = json.dumps(wrapped)
        self.assertNotIn(raw.hex(), output)
        self.assertNotIn("SERIAL", output)
        self.assertNotIn("PRIVATE", output)

    def test_cper_malformed_and_bounded(self):
        for raw in (None, "", "DEADBEEF", "43504552" + "0" * 247,
                    "43504552" + "Z" * 248, "43504552" + "0" * (2 * 1024 * 1024)):
            with self.subTest(length=len(raw) if raw else 0):
                self.assertEqual(cper_summary(raw), {})
        raw = bytearray(128 + 72 * 40)
        raw[0:4] = b"CPER"
        struct.pack_into("<H", raw, 10, 40)
        self.assertEqual(len(cper_summary(raw.hex())["cper_section_types"]), 32)


class EventCursorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temporary.name) / "cursor.json"
        self.output = []

    def tearDown(self):
        self.temporary.cleanup()

    def events(self):
        return [record for record in self.output if record["type"] == "event"]

    def collector(self, backend, emit=None):
        return EventCollector(self.state_file, emit or self.output.append, backend)

    def test_backlog_cap_continues_in_order_and_restart_avoids_replay(self):
        backend = FakeBackend({"System": [event_xml(number) for number in range(1, 206)], "Application": []})
        collector = self.collector(backend)
        collector.poll()
        self.assertEqual([value["record_id"] for value in self.events()], list(range(1, 101)))
        self.assertEqual(json.loads(self.state_file.read_text())["logs"]["System"]["cursor"], 100)
        collector.poll()
        self.assertEqual(len(self.events()), 200)
        collector.close()
        self.assertTrue(backend.closed)
        collector = self.collector(backend)
        collector.poll()
        self.assertEqual([value["record_id"] for value in self.events()], list(range(1, 206)))
        queries_before = len([call for call in backend.calls if call[0] == "events"])
        collector.poll()
        self.assertEqual(len([call for call in backend.calls if call[0] == "events"]), queries_before)
        self.assertEqual(len(self.events()), 205)

    def test_drained_filtered_query_advances_to_unmatched_snapshot(self):
        backend = FakeBackend({"System": [event_xml(3)], "Application": []}, latest={"System": 10, "Application": 0})
        collector = self.collector(backend)
        collector.poll()
        self.assertEqual(collector._cursors["System"]["cursor"], 10)
        collector.poll()
        self.assertEqual(len(self.events()), 1)

    def test_log_reset_restarts_and_reports_reset(self):
        backend = FakeBackend({"System": [event_xml(10)], "Application": []})
        collector = self.collector(backend)
        collector.poll()
        backend.records["System"] = [event_xml(1)]
        backend.latest["System"] = 1
        collector.poll()
        self.assertEqual([value["record_id"] for value in self.events()], [10, 1])
        self.assertEqual(len([record for record in self.output if record.get("status") == "reset"]), 1)

    def test_output_failure_leaves_rejected_record_unacknowledged(self):
        backend = FakeBackend({"System": [event_xml(1), event_xml(2), event_xml(3)], "Application": []})
        fail_once = [True]

        def emit(record):
            if record["type"] == "event" and record["record_id"] == 2 and fail_once[0]:
                fail_once[0] = False
                raise OSError("PRIVATE OUTPUT PATH")
            self.output.append(record)

        collector = self.collector(backend, emit)
        with self.assertRaises(EventEmissionError):
            collector.poll()
        self.assertEqual(collector._cursors["System"]["cursor"], 1)
        self.assertFalse(self.state_file.exists())
        self.assertEqual(backend.iterators_closed, 1)
        collector.poll()
        self.assertEqual([value["record_id"] for value in self.events()], [1, 2, 3])
        self.assertNotIn("PRIVATE", json.dumps(self.output))

    def test_parse_failure_does_not_acknowledge_later_records(self):
        malformed = event_xml(2).replace("</Event>", "")
        backend = FakeBackend({"System": [event_xml(1), malformed, event_xml(3)], "Application": []}, latest={"System": 3, "Application": 0})
        collector = self.collector(backend)
        collector.poll()
        self.assertEqual(collector._cursors["System"]["cursor"], 1)
        self.assertEqual([value["record_id"] for value in self.events()], [1])
        collector.poll()
        errors = [record for record in self.output if record.get("status") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(collector.coverage["System"], "error")
        backend.records["System"][1] = event_xml(2)
        collector.poll()
        self.assertEqual([value["record_id"] for value in self.events()], [1, 2, 3])
        self.assertEqual(collector.coverage["System"], "available")

    def test_permanent_denial_is_logged_once_and_skipped(self):
        backend = FakeBackend()
        backend.failures["System"] = PermissionError(13, "PRIVATE SECRET PATH")
        collector = self.collector(backend)
        collector.poll()
        collector.poll()
        self.assertEqual(len([call for call in backend.calls if call == ("latest", "System")]), 1)
        self.assertEqual(len([record for record in self.output if record.get("status") == "unavailable"]), 1)
        self.assertNotIn("PRIVATE", json.dumps(self.output))
        self.assertEqual(collector.coverage, {"System": "unavailable", "Application": "available"})

    def test_transient_errors_deduplicate_then_report_recovery(self):
        backend = FakeBackend()
        backend.failures["System"] = OSError(123, "PRIVATE ERROR MESSAGE")
        collector = self.collector(backend)
        collector.poll()
        collector.poll()
        self.assertEqual(len([record for record in self.output if record.get("status") == "error"]), 1)
        del backend.failures["System"]
        collector.poll()
        self.assertEqual(collector.coverage["System"], "available")
        self.assertEqual(len([record for record in self.output if record.get("log") == "System" and record.get("status") == "available"]), 2)
        self.assertNotIn("PRIVATE", json.dumps(self.output))

    def test_invalid_saved_state_is_rejected_without_query_injection(self):
        self.state_file.write_text(json.dumps({"version": 1, "logs": {"System": {
            "cursor": 20, "initialized": False, "start_utc": "x'] or true() or ['x",
        }}}), encoding="utf-8")
        collector = self.collector(FakeBackend())
        self.assertFalse(collector._cursors["System"]["initialized"])
        self.assertEqual(self.output, [])
        self.assertEqual(collector.state_error, {"error_type": "ValueError", "error_code": None})
        self.assertNotIn("true()", json.dumps(self.output))
        collector.poll()
        self.assertEqual(self.output[0]["type"], "event_state_error")
        saved = json.loads(self.state_file.read_text())
        self.assertEqual(saved["logs"]["System"]["cursor"], 0)

    def test_corrupt_state_and_unwritable_output_do_not_prevent_construction(self):
        self.state_file.write_text("{invalid json", encoding="utf-8")
        blocked = [True]
        attempts = []

        def emit(record):
            attempts.append(record["type"])
            if blocked[0]:
                raise OSError(28, "PRIVATE OUTPUT PATH")
            self.output.append(record)

        backend = FakeBackend()
        collector = self.collector(backend, emit)
        self.assertEqual(attempts, [])
        self.assertEqual(collector.state_error, {"error_type": "JSONDecodeError", "error_code": None})
        for _ in range(2):
            with self.assertRaises(EventEmissionError):
                collector.poll()
        self.assertEqual(attempts, ["event_state_error", "event_state_error"])
        self.assertEqual(backend.calls, [])
        self.assertEqual(self.state_file.read_text(), "{invalid json")
        blocked[0] = False
        collector.poll()
        self.assertEqual(self.output[0]["type"], "event_state_error")
        self.assertEqual(len([record for record in self.output if record["type"] == "event_state_error"]), 1)
        self.assertIsNone(collector.state_error)
        self.assertEqual(json.loads(self.state_file.read_text())["version"], 1)
        self.assertNotIn("PRIVATE", json.dumps(self.output))

    def test_atomic_save_failure_is_reported_without_disclosing_path(self):
        collector = self.collector(FakeBackend())
        with patch("pcwatch.events.os.replace", side_effect=PermissionError(13, "PRIVATE PATH")):
            collector.poll()
            collector.poll()
        self.assertFalse(self.state_file.exists())
        self.assertEqual(len([record for record in self.output if record["type"] == "event_state_error"]), 1)
        self.assertEqual(collector.state_error, {"error_type": "PermissionError", "error_code": 13})
        self.assertEqual(list(self.state_file.parent.glob("*.tmp")), [])
        self.assertNotIn("PRIVATE", json.dumps(self.output))
        collector.poll()
        self.assertTrue(self.state_file.exists())
        self.assertIsNone(collector.state_error)

    def test_legacy_csharp_state_preserves_cursors_without_replay(self):
        self.state_file.write_text(json.dumps({"Version": 1, "Logs": {
            "System": {"Cursor": 20, "Initialized": True, "StartUtc": "2026-10-04T10:00:00.0000000Z"},
            "Application": {"Cursor": 10, "Initialized": True, "StartUtc": "2026-10-04T10:00:00.0000000Z"},
        }}), encoding="utf-8-sig")
        backend = FakeBackend(latest={"System": 20, "Application": 10})
        collector = self.collector(backend)
        self.assertIsNone(collector.state_error)
        self.assertEqual(collector._cursors["System"]["cursor"], 20)
        self.assertEqual(collector._cursors["Application"]["cursor"], 10)
        collector.poll()
        self.assertEqual(self.events(), [])
        self.assertEqual([call for call in backend.calls if call[0] == "events"], [])
        saved = json.loads(self.state_file.read_text())
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["logs"]["System"]["cursor"], 20)
        self.assertEqual(saved["logs"]["Application"]["cursor"], 10)
        self.assertNotIn("Version", saved)

    def test_empty_log_and_close(self):
        backend = FakeBackend()
        collector = self.collector(backend)
        collector.poll()
        self.assertEqual(collector._cursors["System"]["cursor"], 0)
        self.assertEqual(collector.coverage["System"], "available")
        collector.close()
        collector.close()
        with self.assertRaises(RuntimeError):
            collector.poll()

    def test_queries_preserve_provider_filters_and_boundaries(self):
        query = build_query("System", {"cursor": 5, "initialized": True}, 9)
        root = ET.fromstring(query)
        selectors = list(root.iter("Select"))
        self.assertEqual(len(selectors), 10)
        self.assertTrue(all("EventRecordID > 5" in node.text and "EventRecordID <= 9" in node.text for node in selectors))
        self.assertIn("Microsoft-Windows-WHEA-Logger", query)
        self.assertNotIn("Security", query)
        for cursor in (-1, True, "5 or true()"):
            with self.subTest(cursor=cursor):
                with self.assertRaises(ValueError):
                    build_query("System", {"cursor": cursor, "initialized": True}, 9)


if __name__ == "__main__":
    unittest.main()
