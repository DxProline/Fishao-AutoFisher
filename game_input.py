from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes
from time import perf_counter_ns

import win32gui

_PTR = ctypes.c_size_t
_RESULT = ctypes.c_ssize_t
_CALLBACK = ctypes.WINFUNCTYPE(_RESULT, ctypes.c_int, _PTR, _RESULT)
_KEY_MESSAGES = {0x100: "key_down", 0x101: "key_up", 0x104: "key_down", 0x105: "key_up"}
_MOUSE_MESSAGES = {
    0x201: ("left", "mouse_down"), 0x202: ("left", "mouse_up"),
    0x204: ("right", "mouse_down"), 0x205: ("right", "mouse_up"),
    0x207: ("middle", "mouse_down"), 0x208: ("middle", "mouse_up"),
    0x20B: ("extra", "mouse_down"), 0x20C: ("extra", "mouse_up"),
}


class KeyboardData(ctypes.Structure):
    _fields_ = [
        ("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("extra", _PTR),
    ]


class MouseData(ctypes.Structure):
    _fields_ = [
        ("point", wintypes.POINT), ("mouse_data", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("extra", _PTR),
    ]


class GameInput(threading.Thread):
    """Pasivní události pouze v popředí hry a během aktivní relace."""

    def __init__(self, title: str, enabled: Callable[[], bool], emit: Callable[[int, dict[str, object]], None],
                 *, shortcuts: dict[int, Callable[[], None]] | None = None, mouse: bool = True) -> None:
        super().__init__(name="FishaoGameInput", daemon=True)
        self.title = title.casefold()
        self.enabled = enabled
        self.emit = emit
        self.shortcuts = shortcuts or {}
        self.mouse = mouse
        self.shutdown = threading.Event()
        self.ready = threading.Event()
        self.error: str | None = None

    def close(self) -> None:
        self.shutdown.set()

    def _game(self) -> int | None:
        if not self.enabled():
            return None
        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd and win32gui.GetWindowText(hwnd).strip().casefold() == self.title and not win32gui.IsIconic(hwnd):
                return hwnd
        except win32gui.error:
            return None
        return None

    def run(self) -> None:
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        user.SetWindowsHookExW.argtypes = [ctypes.c_int, _CALLBACK, wintypes.HINSTANCE, wintypes.DWORD]
        user.SetWindowsHookExW.restype = wintypes.HANDLE
        user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, _PTR, _RESULT]
        user.CallNextHookEx.restype = _RESULT
        user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user.UnhookWindowsHookEx.restype = wintypes.BOOL
        user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user.PeekMessageW.restype = wintypes.BOOL
        user.GetKeyNameTextW.argtypes = [wintypes.LONG, wintypes.LPWSTR, ctypes.c_int]
        user.GetKeyNameTextW.restype = ctypes.c_int
        held: set[int] = set()

        @_CALLBACK
        def on_key(code: int, message: int, address: int) -> int:
            try:
                if code == 0 and message in _KEY_MESSAGES:
                    stamp = perf_counter_ns()
                    item = ctypes.cast(address, ctypes.POINTER(KeyboardData)).contents
                    kind = _KEY_MESSAGES[message]
                    shortcut = self.shortcuts.get(item.vk)
                    if shortcut is not None:
                        if kind == "key_up":
                            held.discard(item.vk)
                        elif item.vk not in held:
                            held.add(item.vk)
                            shortcut()
                    elif item.vk not in (0x77, 0x78) and self._game():
                        buffer = ctypes.create_unicode_buffer(64)
                        user.GetKeyNameTextW((item.scan << 16) | ((item.flags & 1) << 24), buffer, len(buffer))
                        self.emit(stamp, {
                            "type": kind, "key": buffer.value or f"VK_{item.vk:02X}",
                            "vk": item.vk, "scan": item.scan, "extended": bool(item.flags & 1),
                            "injected": bool(item.flags & 0x10),
                        })
            except Exception as exc:
                self.error = f"Záznam kláves skončil. {exc}"
                self.shutdown.set()
            return user.CallNextHookEx(None, code, message, address)

        @_CALLBACK
        def on_mouse(code: int, message: int, address: int) -> int:
            try:
                stamp = perf_counter_ns()
                hwnd = self._game() if code == 0 and message in _MOUSE_MESSAGES else None
                if hwnd is not None:
                    item = ctypes.cast(address, ctypes.POINTER(MouseData)).contents
                    x, y = win32gui.ScreenToClient(hwnd, (item.point.x, item.point.y))
                    l, t, r, b = win32gui.GetClientRect(hwnd)
                    if l <= x < r and t <= y < b:
                        button, kind = _MOUSE_MESSAGES[message]
                        if button == "extra":
                            button = f"extra_{item.mouse_data >> 16}"
                        self.emit(stamp, {
                            "type": kind, "button": button, "x": x - l, "y": y - t,
                            "x_ratio": (x - l) / (r - l), "y_ratio": (y - t) / (b - t),
                            "width": r - l, "height": b - t, "injected": bool(item.flags & 1),
                        })
            except win32gui.error:
                # Přesunutí nebo zavření okna mezi oběma voláními Win32 není chyba relace.
                pass
            except Exception as exc:
                self.error = f"Záznam kliknutí skončil. {exc}"
                self.shutdown.set()
            return user.CallNextHookEx(None, code, message, address)

        installs = []
        try:
            message = wintypes.MSG()
            user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            module = kernel.GetModuleHandleW(None)
            handlers = [(13, on_key)]
            if self.mouse:
                handlers.append((14, on_mouse))
            for kind, handler in handlers:
                hook = user.SetWindowsHookExW(kind, handler, module, 0)
                if not hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                installs.append(hook)
            self.ready.set()
            while not self.shutdown.is_set():
                # Vyprázdnění fronty obsluhuje i volání obou nízkoúrovňových háků.
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == 0x12:
                        return
                self.shutdown.wait(0.005)
        except Exception as exc:
            self.error = f"Záznam vstupů není dostupný. {exc}"
        finally:
            for hook in installs:
                user.UnhookWindowsHookEx(hook)
            self.ready.set()

