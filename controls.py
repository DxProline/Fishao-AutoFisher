from __future__ import annotations

import ctypes
from ctypes import wintypes
from time import sleep

import win32api
import win32gui

from window import Region


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
    ]


class KeyInput(ctypes.Structure):
    _fields_ = [
        ("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
    ]


class InputValue(ctypes.Union):
    _fields_ = [("mouse", MouseInput), ("key", KeyInput)]


class NativeInput(ctypes.Structure):
    _fields_ = [("kind", wintypes.DWORD), ("value", InputValue)]


class Controls:
    _SCANS = {"left": 0x4B, "right": 0x4D}

    def __init__(self) -> None:
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._send = self._user.SendInput
        self._send.argtypes = [wintypes.UINT, ctypes.POINTER(NativeInput), ctypes.c_int]
        self._send.restype = wintypes.UINT
        self.held: str | None = None

    def _inputs(self, *items: NativeInput) -> None:
        array = (NativeInput * len(items))(*items)
        written = self._send(len(items), array, ctypes.sizeof(NativeInput))
        if written != len(items):
            raise OSError(f"Windows přijal {written}/{len(items)} kroků vstupu.")

    def _arrow(self, direction: str, *, up: bool) -> None:
        flags = 0x0008 | 0x0001
        if up:
            flags |= 0x0002
        self._inputs(NativeInput(1, InputValue(key=KeyInput(0, self._SCANS[direction], flags, 0, 0))))

    def release(self) -> None:
        if self.held is not None:
            self._arrow(self.held, up=True)
            self.held = None

    def direction(self, hwnd: int, requested: str | None) -> None:
        if requested is not None and win32gui.GetForegroundWindow() != hwnd:
            requested = None
        if requested == self.held:
            return
        self.release()
        if requested is not None and win32gui.GetForegroundWindow() == hwnd:
            self._arrow(requested, up=False)
            self.held = requested

    @staticmethod
    def _pointer_move(region: Region, x: float, y: float) -> NativeInput | None:
        # Souřadnice jsou vzhledem k obsahu okna, v rozsahu 0 až 1.
        if not (0 <= x < 1 and 0 <= y < 1) or win32gui.GetForegroundWindow() != region.hwnd:
            return None
        client = (min(region.width - 1, int(x * region.width)), min(region.height - 1, int(y * region.height)))
        if win32gui.GetClientRect(region.hwnd)[2:] != (region.width, region.height):
            return None
        if win32gui.ClientToScreen(region.hwnd, (0, 0)) != (region.x, region.y):
            return None
        px, py = win32gui.ClientToScreen(region.hwnd, client)
        hovered = win32gui.WindowFromPoint((px, py))
        if not hovered or win32gui.GetAncestor(hovered, 2) != region.hwnd:
            return None
        sx, sy, sw, sh = (win32api.GetSystemMetrics(i) for i in range(76, 80))
        dx = round((px - sx) * 65535 / max(1, sw - 1))
        dy = round((py - sy) * 65535 / max(1, sh - 1))
        return NativeInput(0, InputValue(mouse=MouseInput(dx, dy, 0, 0xC001, 0, 0)))

    def hover(self, region: Region, x: float, y: float) -> bool:
        move = self._pointer_move(region, x, y)
        if move is None or win32gui.GetForegroundWindow() != region.hwnd:
            return False
        self._inputs(move)
        return True

    @staticmethod
    def pointer_at(region: Region, x: float, y: float) -> bool:
        px, py = win32api.GetCursorPos()
        return (abs(px - (region.x + int(x * region.width))) <= 2
                and abs(py - (region.y + int(y * region.height))) <= 2)

    def click(self, region: Region, x: float, y: float) -> bool:
        if not self.pointer_at(region, x, y):
            if not self.hover(region, x, y):
                return False
            sleep(.02)
        if self._pointer_move(region, x, y) is None or not self.pointer_at(region, x, y):
            return False
        down = NativeInput(0, InputValue(mouse=MouseInput(0, 0, 0, 0x0002, 0, 0)))
        up = NativeInput(0, InputValue(mouse=MouseInput(0, 0, 0, 0x0004, 0, 0)))
        if win32gui.GetForegroundWindow() != region.hwnd:
            return False
        try:
            self._inputs(down)
            # Hra musí dostat stisk a uvolnění v různých snímcích.
            sleep(.055)
        finally:
            self._inputs(up)
        return True

    def escape(self, hwnd: int) -> bool:
        if win32gui.GetForegroundWindow() != hwnd:
            return False
        down = NativeInput(1, InputValue(key=KeyInput(0, 0x01, 0x0008, 0, 0)))
        up = NativeInput(1, InputValue(key=KeyInput(0, 0x01, 0x000A, 0, 0)))
        try:
            self._inputs(down)
            sleep(.075)
        finally:
            self._inputs(up)
        return True

