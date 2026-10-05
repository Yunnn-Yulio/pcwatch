"""Exercise real Shell Link files exclusively in temporary directories."""

import os
from pathlib import Path
import tempfile
import unittest

from pcwatch import startup


@unittest.skipUnless(os.name == "nt", "Windows Shell Link APIs are unavailable")
class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pcwatch-startup-test-")
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data with spaces"
        self.shortcut = self.root / "test Startup" / "PCWatch.lnk"

    def tearDown(self):
        self.temporary.cleanup()

    def test_install_is_opt_in_idempotent_and_uses_background_python(self):
        self.assertFalse(startup.is_enabled(self.data_root, self.shortcut))
        startup.install(self.data_root, self.shortcut)
        self.assertTrue(startup.is_enabled(self.data_root, self.shortcut))
        with startup.ShellLink() as link:
            link.load(self.shortcut)
            target, arguments = link.target()
        expected_target, expected_arguments = startup.launch_command(self.data_root)
        self.assertEqual(os.path.normcase(target), os.path.normcase(str(expected_target)))
        self.assertEqual(arguments, expected_arguments)
        self.assertEqual(Path(target).name.lower(), "pythonw.exe")
        self.assertIn("--data-dir", arguments)
        startup.install(self.data_root, self.shortcut)
        self.assertTrue(startup.is_enabled(self.data_root, self.shortcut))
        startup.remove(self.data_root, self.shortcut)
        self.assertFalse(self.shortcut.exists())
        startup.remove(self.data_root, self.shortcut)

    def test_existing_unrelated_shortcut_is_never_replaced_or_deleted(self):
        self.shortcut.parent.mkdir(parents=True)
        interpreter, _ = startup.launch_command(self.data_root)
        with startup.ShellLink() as link:
            link.save(self.shortcut, interpreter, "-c pass")
        original = self.shortcut.read_bytes()
        self.assertFalse(startup.is_enabled(self.data_root, self.shortcut))
        with self.assertRaises(FileExistsError):
            startup.install(self.data_root, self.shortcut)
        with self.assertRaises(FileExistsError):
            startup.remove(self.data_root, self.shortcut)
        self.assertEqual(self.shortcut.read_bytes(), original)

    def test_shortcut_for_another_data_directory_is_protected(self):
        other_data = self.root / "another recorder"
        startup.install(other_data, self.shortcut)
        self.assertTrue(startup.is_enabled(other_data, self.shortcut))
        self.assertFalse(startup.is_enabled(self.data_root, self.shortcut))
        with self.assertRaises(FileExistsError):
            startup.remove(self.data_root, self.shortcut)
        startup.remove(other_data, self.shortcut)


if __name__ == "__main__":
    unittest.main()
