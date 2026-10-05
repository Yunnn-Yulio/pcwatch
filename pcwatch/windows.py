"""Small Windows integration layer using only Python's standard library.

Native APIs are loaded only when needed. Importing this module does not create a
window, start a thread, modify startup settings, or access monitoring data.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import threading
from typing import Callable
import uuid


def signal_token(data_root: Path) -> str:
    """Use the same per-directory identity as the previous Windows version."""
    path = os.path.abspath(os.fspath(data_root)).upper()
    return hashlib.sha256(path.encode("utf-8")).hexdigest().upper()[:24]


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("PCWatch requires Windows.")


@lru_cache(maxsize=1)
def _kernel():
    _require_windows()
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    dll.CreateMutexW.restype = wintypes.HANDLE
    dll.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    dll.ReleaseMutex.restype = wintypes.BOOL
    dll.CreateEventW.argtypes = (
        ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
    )
    dll.CreateEventW.restype = wintypes.HANDLE
    dll.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    dll.OpenEventW.restype = wintypes.HANDLE
    dll.SetEvent.argtypes = (wintypes.HANDLE,)
    dll.SetEvent.restype = wintypes.BOOL
    dll.ResetEvent.argtypes = (wintypes.HANDLE,)
    dll.ResetEvent.restype = wintypes.BOOL
    dll.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.CloseHandle.argtypes = (wintypes.HANDLE,)
    dll.CloseHandle.restype = wintypes.BOOL
    dll.GetCurrentProcess.argtypes = ()
    dll.GetCurrentProcess.restype = wintypes.HANDLE
    dll.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    dll.SetPriorityClass.restype = wintypes.BOOL
    dll.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    dll.GetModuleHandleW.restype = wintypes.HMODULE
    return dll


class MonitorSignal:
    """Own a named mutex and a named manual-reset stop event.

    A second monitor for the same directory gets ``acquired == False``. It does
    not reset the first monitor's stop event. Close this object on the thread
    that constructed it, because Windows mutex ownership belongs to a thread.
    """

    def __init__(self, data_root: Path):
        self.acquired = False
        self._mutex = None
        self._event = None
        dll = _kernel()
        token = signal_token(data_root)
        self._mutex = dll.CreateMutexW(None, True, "Local\\PCWatchRun-" + token)
        if not self._mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Windows mutex ownership is recursive within one thread. Checking
            # creation, instead of taking an existing mutex again, also rejects
            # a duplicate instance constructed on that same thread.
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS.
                return
            self.acquired = True
            self._event = dll.CreateEventW(
                None, True, False, "Local\\PCWatchStop-" + token
            )
            if not self._event:
                raise ctypes.WinError(ctypes.get_last_error())
            if not dll.ResetEvent(self._event):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def is_set(self) -> bool:
        return self.wait(0)

    def set(self) -> None:
        if self._event and not _kernel().SetEvent(self._event):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, seconds: float) -> bool:
        if not self._event:
            return True
        milliseconds = max(0, min(int(seconds * 1000), 0xFFFFFFFE))
        result = _kernel().WaitForSingleObject(self._event, milliseconds)
        if result == 0:
            return True
        if result == 258:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        dll = _kernel()
        if self._event:
            dll.CloseHandle(self._event)
            self._event = None
        if self._mutex:
            if self.acquired:
                dll.ReleaseMutex(self._mutex)
            dll.CloseHandle(self._mutex)
            self._mutex = None
        self.acquired = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def signal_stop(data_root: Path) -> bool:
    """Signal a running instance. Return False when its event does not exist."""
    dll = _kernel()
    event = dll.OpenEventW(0x0002, False, "Local\\PCWatchStop-" + signal_token(data_root))
    if not event:
        error = ctypes.get_last_error()
        if error == 2:
            return False
        raise ctypes.WinError(error)
    try:
        if not dll.SetEvent(event):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        dll.CloseHandle(event)


def set_below_normal_priority() -> bool:
    dll = _kernel()
    return bool(dll.SetPriorityClass(dll.GetCurrentProcess(), 0x4000))


_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = _WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8),
    ]

    @classmethod
    def from_string(cls, value: str):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", WPARAM), ("lParam", LPARAM), ("time", wintypes.DWORD),
        ("pt", POINT), ("lPrivate", wintypes.DWORD),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", GUID), ("DataLength", wintypes.DWORD)]


DISPLAY_GUID = "6fe69556-704a-47a0-8f24-c28d936fda47"
WM_TIMER = 0x0113
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WM_TRAY = 0x8001
WM_HEALTH = 0x8002


@lru_cache(maxsize=1)
def _gui():
    _require_windows()
    user = ctypes.WinDLL("user32", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    prototypes = {
        "RegisterClassW": ((ctypes.POINTER(WNDCLASSW),), wintypes.WORD),
        "UnregisterClassW": ((wintypes.LPCWSTR, wintypes.HINSTANCE), wintypes.BOOL),
        "CreateWindowExW": ((wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p), wintypes.HWND),
        "DestroyWindow": ((wintypes.HWND,), wintypes.BOOL),
        "DefWindowProcW": ((wintypes.HWND, wintypes.UINT, WPARAM, LPARAM), LRESULT),
        "GetMessageW": ((ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT,
            wintypes.UINT), wintypes.BOOL),
        "TranslateMessage": ((ctypes.POINTER(MSG),), wintypes.BOOL),
        "DispatchMessageW": ((ctypes.POINTER(MSG),), LRESULT),
        "PostQuitMessage": ((ctypes.c_int,), None),
        "PostMessageW": ((wintypes.HWND, wintypes.UINT, WPARAM, LPARAM), wintypes.BOOL),
        "SetTimer": ((wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
            ctypes.c_void_p), ctypes.c_size_t),
        "KillTimer": ((wintypes.HWND, ctypes.c_size_t), wintypes.BOOL),
        "LoadIconW": ((wintypes.HINSTANCE, ctypes.c_void_p), wintypes.HICON),
        "CreatePopupMenu": ((), wintypes.HMENU),
        "AppendMenuW": ((wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
            wintypes.LPCWSTR), wintypes.BOOL),
        "DestroyMenu": ((wintypes.HMENU,), wintypes.BOOL),
        "GetCursorPos": ((ctypes.POINTER(POINT),), wintypes.BOOL),
        "SetForegroundWindow": ((wintypes.HWND,), wintypes.BOOL),
        "TrackPopupMenu": ((wintypes.HMENU, wintypes.UINT, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p), wintypes.UINT),
        "MessageBoxW": ((wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.UINT), ctypes.c_int),
        "RegisterPowerSettingNotification": ((wintypes.HANDLE,
            ctypes.POINTER(GUID), wintypes.DWORD), wintypes.HANDLE),
        "UnregisterPowerSettingNotification": ((wintypes.HANDLE,), wintypes.BOOL),
        "RegisterWindowMessageW": ((wintypes.LPCWSTR,), wintypes.UINT),
    }
    for name, (arguments, result) in prototypes.items():
        function = getattr(user, name)
        function.argtypes = arguments
        function.restype = result
    shell.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    shell.Shell_NotifyIconW.restype = wintypes.BOOL
    wts.WTSRegisterSessionNotification.argtypes = (wintypes.HWND, wintypes.DWORD)
    wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
    wts.WTSUnRegisterSessionNotification.argtypes = (wintypes.HWND,)
    wts.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
    return user, shell, wts


class NativeWindow:
    """Hidden native message window, optional tray icon, and power notifications."""

    def __init__(
        self, on_activity: Callable[[dict], None], status_text: Callable[[], str],
        on_stop: Callable[[], None], on_disable_startup: Callable[[], None],
        log_folder: Path, interval: int, show_tray: bool = True,
    ):
        self.on_activity = on_activity
        self.status_text = status_text
        self.on_stop = on_stop
        self.on_disable_startup = on_disable_startup
        self.log_folder = Path(log_folder)
        self.interval = interval
        self.show_tray = show_tray
        self.hwnd = None
        self._finished = None
        self._healthy = True
        self._tray_added = False
        self._power = None
        self._session_registered = False
        self._callback_errors = set()
        self._callback = WNDPROC(self._window_proc)
        self._class_name = f"PCWatchPythonWindow-{os.getpid()}-{id(self):x}"
        self._taskbar_created = 0
        self._notify = None

    def _emit(self, **fields) -> None:
        fields["utc"] = datetime.now(timezone.utc).isoformat()
        self.on_activity(fields)

    def _callback_error(self, component: str, error: Exception) -> None:
        # Avoid repeated errors filling the log, and never let Python exceptions
        # escape a ctypes window-procedure callback into native Windows code.
        key = (component, type(error).__name__, getattr(error, "winerror", None))
        if key in self._callback_errors:
            return
        self._callback_errors.add(key)
        try:
            self._emit(type="collection_error", component=component,
                error_type=type(error).__name__, error_code=getattr(error, "winerror", None))
        except Exception:
            pass

    def set_health(self, healthy: bool) -> None:
        self._healthy = bool(healthy)
        if self.hwnd:
            _gui()[0].PostMessageW(self.hwnd, WM_HEALTH, 0, 0)

    def _tray(self, operation: int) -> None:
        if not self.show_tray or not self.hwnd:
            return
        user, shell, _ = _gui()
        if self._notify is None:
            self._notify = NOTIFYICONDATAW()
            self._notify.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self._notify.hWnd = self.hwnd
            self._notify.uID = 1
            self._notify.uFlags = 0x01 | 0x02 | 0x04  # MESSAGE | ICON | TIP
            self._notify.uCallbackMessage = WM_TRAY
            self._notify.hIcon = user.LoadIconW(None, ctypes.c_void_p(32516))
        self._notify.szTip = (
            f"PCWatch - running ({self.interval}s samples)"
            if self._healthy else "PCWatch - logging error; open Status"
        )
        if not shell.Shell_NotifyIconW(operation, ctypes.byref(self._notify)):
            if operation != 2:  # NIM_DELETE failures during shutdown are harmless.
                self._callback_error("tray", ctypes.WinError(ctypes.get_last_error()))
            return
        self._tray_added = operation != 2

    def _command(self, command: int) -> None:
        user = _gui()[0]
        if command == 1:
            user.MessageBoxW(self.hwnd, str(self.status_text()), "PCWatch status", 0x40)
        elif command == 2:
            os.startfile(str(self.log_folder))
        elif command == 3:
            self._emit(type="user_mark")
        elif command == 4:
            self.on_disable_startup()
            self.on_stop()
        elif command == 5:
            self.on_stop()

    def _menu(self) -> None:
        user = _gui()[0]
        menu = user.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for command, text in (
                (1, "Status"), (2, "Open logs"), (3, "Mark incident now"),
                (4, "Disable startup and stop"), (5, "Stop"),
            ):
                if not user.AppendMenuW(menu, 0, command, text):
                    raise ctypes.WinError(ctypes.get_last_error())
            point = POINT()
            if not user.GetCursorPos(ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
            user.SetForegroundWindow(self.hwnd)
            command = user.TrackPopupMenu(menu, 0x100 | 0x002,
                point.x, point.y, 0, self.hwnd, None)  # RETURN CMD | RIGHT BUTTON.
            user.PostMessageW(self.hwnd, 0, 0, 0)  # Allow menu dismissal on outside clicks.
            if command:
                self._command(command)
        finally:
            user.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        user = _gui()[0]
        try:
            if message == WM_TIMER:
                if self._finished is not None and self._finished.is_set():
                    user.DestroyWindow(hwnd)
                return 0
            if message == WM_CLOSE:
                self.on_stop()
                user.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self._cleanup_window()
                user.PostQuitMessage(0)
                return 0
            if message == WM_HEALTH:
                if self._tray_added:
                    self._tray(1)
                return 0
            if message == self._taskbar_created and self._taskbar_created:
                self._tray(0)
                return 0
            if message == WM_TRAY:
                if lparam in (0x0205, 0x007B):  # Right click / context menu.
                    self._menu()
                elif lparam == 0x0203:  # Double left click.
                    self._command(1)
                return 0
            if message == WM_WTSSESSION_CHANGE and wparam in (7, 8):
                self._emit(type="session", state="locked" if wparam == 7 else "unlocked")
                return 0
            if message == WM_POWERBROADCAST:
                if wparam in (4, 7, 18):
                    states = {4: "suspend", 7: "resume", 18: "resume_automatic"}
                    self._emit(type="power", state=states[wparam])
                elif wparam == 0x8013 and lparam:
                    header = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                    wanted = GUID.from_string(DISPLAY_GUID)
                    if bytes(header.PowerSetting) == bytes(wanted) and header.DataLength == 4:
                        state = wintypes.DWORD.from_address(
                            lparam + ctypes.sizeof(POWERBROADCAST_SETTING)
                        ).value
                        self._emit(type="display", state={0: "off", 1: "on", 2: "dimmed"}.get(state, "unknown"))
                return 1
            if message == 0x0011:  # WM_QUERYENDSESSION: permit Windows shutdown.
                return 1
            if message == 0x0016 and wparam:  # WM_ENDSESSION.
                self.on_stop()
                return 0
        except Exception as error:
            self._callback_error("native_window", error)
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _cleanup_window(self) -> None:
        user, _, wts = _gui()
        if self._tray_added:
            self._tray(2)
        if self._power:
            user.UnregisterPowerSettingNotification(self._power)
            self._power = None
        if self._session_registered and self.hwnd:
            wts.WTSUnRegisterSessionNotification(self.hwnd)
            self._session_registered = False
        if self.hwnd:
            user.KillTimer(self.hwnd, 1)

    def run(self, finished: threading.Event) -> None:
        """Run on a dedicated GUI thread or on the application's main thread."""
        user, _, wts = _gui()
        self._finished = finished
        instance = _kernel().GetModuleHandleW(None)
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self._callback
        window_class.hInstance = instance
        window_class.lpszClassName = self._class_name
        if not user.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # A hidden top-level window receives power/session broadcasts. A
            # message-only window would miss some of those Windows broadcasts.
            self.hwnd = user.CreateWindowExW(0, self._class_name, "PCWatch", 0,
                0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            if not user.SetTimer(self.hwnd, 1, 1000, None):
                raise ctypes.WinError(ctypes.get_last_error())
            self._taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")
            display = GUID.from_string(DISPLAY_GUID)
            self._power = user.RegisterPowerSettingNotification(self.hwnd, ctypes.byref(display), 0)
            if not self._power:
                self._emit(type="power_notification_unavailable", error_code=ctypes.get_last_error())
            self._session_registered = bool(wts.WTSRegisterSessionNotification(self.hwnd, 0))
            if not self._session_registered:
                self._emit(type="session_notification_unavailable", error_code=ctypes.get_last_error())
            self._tray(0)
            message = MSG()
            while True:
                result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        finally:
            self._cleanup_window()
            if self.hwnd:
                user.DestroyWindow(self.hwnd)
                self.hwnd = None
            user.UnregisterClassW(self._class_name, instance)
