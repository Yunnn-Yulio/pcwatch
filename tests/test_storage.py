"""Exercise real temporary files; never touch the user's recorder state."""

import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from pcwatch.storage import LogStore, RESOURCE_FIELDS, atomic_json, utc_now


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_csv_rotation_repeats_header_and_ignores_extra_fields(self):
        header_size = len(",".join(RESOURCE_FIELDS).encode()) + 1
        store = LogStore(self.root, segment_bytes=header_size + 140)
        for index in range(8):
            store.write_resource({"utc": f"2026-10-04T00:00:0{index}Z", "cpu_pct": index, "secret_extra": "NEVER_LOG_THIS"})
        files = sorted(store.log_dir.glob("resources-*.csv"))
        self.assertGreater(len(files), 1)
        samples = []
        for path in files:
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("NEVER_LOG_THIS", raw)
            rows = list(csv.reader(raw.splitlines()))
            self.assertEqual(rows[0], list(RESOURCE_FIELDS))
            samples.extend(rows[1:])
        self.assertEqual(len(samples), 8)

    def test_unicode_and_csv_escaping_are_valid(self):
        store = LogStore(self.root)
        store.write_json("activity", {"type": "test", "text": "雪\nquoted"})
        store.write_resource({"utc": "comma,quote\"newline\n"})
        path = next(store.log_dir.glob("activity-*.jsonl"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["text"], "雪\nquoted")
        with next(store.log_dir.glob("resources-*.csv")).open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[1][0], "comma,quote\"newline\n")

    def test_unknown_kind_cannot_choose_a_path(self):
        store = LogStore(self.root)
        with self.assertRaises(ValueError):
            store.write_json("../outside", {"type": "test"})
        self.assertEqual(list(self.root.iterdir()), [store.log_dir])

    def test_retention_and_size_only_remove_own_regular_files(self):
        store = LogStore(self.root, max_bytes=40, segment_bytes=40, retention_days=2)
        old = store.log_dir / "activity-20200101-001.jsonl"
        old.write_bytes(b"expired")
        old_time = time.time() - 3 * 86400
        os.utime(old, (old_time, old_time))
        first = store.log_dir / "processes-20260101-001.jsonl"
        second = store.log_dir / "events-20260101-001.jsonl"
        first.write_bytes(b"a" * 30)
        second.write_bytes(b"b" * 30)
        os.utime(first, (time.time() - 60, time.time() - 60))
        foreign = store.log_dir / "notes.txt"
        foreign.write_bytes(b"private" * 100)
        wrong_extension = store.log_dir / "events-20200101-001.csv"
        wrong_extension.write_bytes(b"not a recorder log")
        store.cleanup()
        self.assertFalse(old.exists())
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(foreign.read_bytes(), b"private" * 100)
        self.assertTrue(wrong_extension.exists())

    def test_link_file_is_not_written_or_cleaned(self):
        store = LogStore(self.root, max_bytes=1)
        outside = self.root / "outside.json"
        outside.write_text("KEEP", encoding="utf-8")
        day = utc_now()[:10].replace("-", "")
        link = store.log_dir / f"activity-{day}-001.jsonl"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Creating symlinks is unavailable ({type(error).__name__}).")
        with self.assertRaises(OSError):
            store.write_json("activity", {"type": "test"})
        store.cleanup()
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "KEEP")

    @unittest.skipUnless(os.name == "nt", "Windows directory junction behavior")
    def test_logs_directory_junction_is_rejected(self):
        data_root = self.root / "data"
        outside = self.root / "outside"
        data_root.mkdir()
        outside.mkdir()
        sentinel = outside / "activity-20000101-001.jsonl"
        sentinel.write_text("KEEP", encoding="utf-8")
        junction = data_root / "Logs"
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace"))
        try:
            with self.assertRaises(OSError):
                LogStore(data_root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "KEEP")
        finally:
            # Remove the checked link itself, never recurse into its target.
            self.assertTrue(junction.lstat().st_file_attributes & 0x400)
            os.rmdir(junction)

    def test_durable_write_failure_reaches_caller_and_rolls_back(self):
        store = LogStore(self.root)
        store.write_json("events", {"record_id": 1})
        path = next(store.log_dir.glob("events-*.jsonl"))
        previous = path.read_bytes()
        with mock.patch("pcwatch.storage.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                store.write_json("events", {"record_id": 2})
        self.assertEqual(path.read_bytes(), previous)

    def test_parallel_writes_produce_complete_lines(self):
        store = LogStore(self.root)
        workers = [threading.Thread(target=lambda n=number: [store.write_json("activity", {"worker": n, "index": index}) for index in range(8)]) for number in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        values = [json.loads(line) for line in next(store.log_dir.glob("activity-*.jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual({(value["worker"], value["index"]) for value in values}, {(worker, index) for worker in range(4) for index in range(8)})

    def test_atomic_json_replacement_and_failed_write_keep_old_state(self):
        state = self.root / "state.json"
        atomic_json(state, {"cursor": 1})
        with mock.patch("pcwatch.storage.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_json(state, {"cursor": 2})
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), {"cursor": 1})
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["state.json"])
        atomic_json(state, {"cursor": 3})
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), {"cursor": 3})

    def test_closed_store_rejects_writes_and_cleanup(self):
        store = LogStore(self.root)
        store.close()
        with self.assertRaises(ValueError):
            store.write_json("activity", {"type": "test"})
        with self.assertRaises(ValueError):
            store.cleanup()


if __name__ == "__main__":
    unittest.main()
