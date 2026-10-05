"""Manage an opt-in user Startup shortcut through Windows' Shell Link API."""

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import uuid


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def from_string(cls, value):
        return cls((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


def startup_path():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/PCWatch.lnk"


def launcher_path():
    return Path(__file__).resolve().parent.parent / "PCWatch.pyw"


def launch_command(data_root):
    interpreter = Path(sys.executable).with_name("pythonw.exe")
    if not interpreter.is_file():
        raise FileNotFoundError("pythonw.exe is required for background startup")
    arguments = subprocess.list2cmdline([str(launcher_path()), "--data-dir", str(Path(data_root).absolute())])
    return interpreter, arguments


class ShellLink:
    """Small COM wrapper; only shortcut metadata is accessed."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Startup shortcuts require Windows")
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
        self.ole.CoInitializeEx.restype = ctypes.c_long
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, w.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self.ole.CoCreateInstance.restype = ctypes.c_long
        result = self.ole.CoInitializeEx(None, 2)
        self.initialized = result in (0, 1)
        if result < 0 and result != -2147417850:  # RPC_E_CHANGED_MODE
            raise OSError(result, "COM initialization failed")
        self.link = ctypes.c_void_p()
        self.persist = ctypes.c_void_p()
        try:
            clsid = GUID.from_string("00021401-0000-0000-C000-000000000046")
            iid = GUID.from_string("000214F9-0000-0000-C000-000000000046")
            self.check(self.ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(self.link)))
            persist_iid = GUID.from_string("0000010B-0000-0000-C000-000000000046")
            self.check(self.call(self.link, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(persist_iid), ctypes.byref(self.persist)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(result):
        if result < 0:
            raise OSError(result, "Shell Link operation failed")

    @staticmethod
    def call(pointer, index, types, *args):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])
        return function(pointer, *args)

    def load(self, path):
        self.check(self.call(self.persist, 5, [w.LPCWSTR, w.DWORD], str(path), 0))

    def target(self):
        path = ctypes.create_unicode_buffer(32768)
        arguments = ctypes.create_unicode_buffer(32768)
        self.check(self.call(self.link, 3, [w.LPWSTR, ctypes.c_int, ctypes.c_void_p, w.DWORD], path, len(path), None, 4))
        self.check(self.call(self.link, 10, [w.LPWSTR, ctypes.c_int], arguments, len(arguments)))
        return path.value, arguments.value

    def save(self, path, interpreter, arguments):
        for index, value in ((20, str(interpreter)), (11, arguments), (9, str(launcher_path().parent)), (7, "PCWatch local diagnostic recorder")):
            self.check(self.call(self.link, index, [w.LPCWSTR], value))
        self.check(self.call(self.persist, 6, [w.LPCWSTR, w.BOOL], str(path), True))

    def close(self):
        for name in ("persist", "link"):
            pointer = getattr(self, name, None)
            if pointer and pointer.value:
                self.call(pointer, 2, [])
                pointer.value = None
        if self.initialized:
            self.ole.CoUninitialize()
            self.initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _matches(link, data_root):
    target, arguments = link.target()
    interpreter, expected = launch_command(data_root)
    return os.path.normcase(target) == os.path.normcase(str(interpreter)) and arguments == expected


def is_enabled(data_root, path=None):
    path = Path(path) if path else startup_path()
    if not path.is_file():
        return False
    try:
        with ShellLink() as link:
            link.load(path)
            return _matches(link, data_root)
    except OSError:
        return False


def install(data_root, path=None):
    path = Path(path) if path else startup_path()
    interpreter, arguments = launch_command(data_root)
    with ShellLink() as link:
        if path.exists():
            link.load(path)
            if not _matches(link, data_root):
                raise FileExistsError("An unrelated PCWatch shortcut already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        link.save(path, interpreter, arguments)


def remove(data_root, path=None):
    path = Path(path) if path else startup_path()
    if not path.exists():
        return
    with ShellLink() as link:
        link.load(path)
        if not _matches(link, data_root):
            raise FileExistsError("Refusing to remove an unrelated PCWatch shortcut")
    path.unlink()
