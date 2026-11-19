from __future__ import annotations

import ctypes
from ctypes import wintypes
from time import sleep

import win32api
import win32con
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


class BackgroundControls(Controls):
    """Míří na okno hry, pro šipky v popředí používá vstup Windows."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title
        self._held_window = 0
        self._native_arrow = False
        self._hovered: tuple[Region, int] | None = None
        self.problem: str | None = None

    def _game_window(self, hwnd: int) -> bool:
        try:
            return bool(hwnd and win32gui.IsWindow(hwnd) and win32gui.GetWindowText(hwnd) == self.title)
        except win32gui.error:
            return False

    @property
    def uses_window_messages(self) -> bool:
        return not self._native_arrow

    def _post(self, hwnd: int, message: int, key: int, data: int) -> bool:
        if not self._game_window(hwnd):
            return False
        try:
            win32gui.PostMessage(hwnd, message, key, data)
        except win32gui.error:
            self.problem = "Windows zamítl přímý vstup do okna hry. Zkus běžný režim nebo stejná oprávnění obou aplikací."
            return False
        self.problem = None
        return True

    def _point(self, region: Region, x: float, y: float) -> int | None:
        if not (0 <= x < 1 and 0 <= y < 1) or not self._game_window(region.hwnd):
            return None
        try:
            if win32gui.IsIconic(region.hwnd) or win32gui.GetClientRect(region.hwnd)[2:] != (region.width, region.height):
                return None
        except win32gui.error:
            return None
        return int(x * region.width) | (int(y * region.height) << 16)

    def pointer_at(self, region: Region, x: float, y: float) -> bool:
        point = self._point(region, x, y)
        if point is None or self._hovered is None or self._hovered[0] != region:
            return False
        previous = self._hovered[1]
        return abs((point & 0xFFFF) - (previous & 0xFFFF)) <= 2 and abs((point >> 16) - (previous >> 16)) <= 2

    def hover(self, region: Region, x: float, y: float) -> bool:
        point = self._point(region, x, y)
        if point is None or not self._post(region.hwnd, win32con.WM_MOUSEMOVE, 0, point):
            self._hovered = None
            return False
        self._hovered = (region, point)
        return True

    def click(self, region: Region, x: float, y: float) -> bool:
        point = self._point(region, x, y)
        if point is None or not self.hover(region, x, y):
            return False
        if not self._post(region.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, point):
            return False
        sleep(.055)
        return self._post(region.hwnd, win32con.WM_LBUTTONUP, 0, point)

    def _key(self, hwnd: int, key: int, scan: int, *, up: bool, extended: bool = False) -> bool:
        data = 1 | scan << 16 | (1 << 24 if extended else 0) | (0xC0000000 if up else 0)
        message = win32con.WM_KEYUP if up else win32con.WM_KEYDOWN
        return self._post(hwnd, message, key, data)

    def release(self) -> None:
        if self.held is not None:
            if self._native_arrow:
                super().release()
                self._native_arrow = False
                return
            vk = win32con.VK_LEFT if self.held == "left" else win32con.VK_RIGHT
            if self._key(self._held_window, vk, self._SCANS[self.held], up=True, extended=True):
                self.held = None
            elif not self._game_window(self._held_window):
                self.held = None

    def direction(self, hwnd: int, requested: str | None) -> None:
        foreground = win32gui.GetForegroundWindow() == hwnd
        if requested == self.held and hwnd == self._held_window and foreground == self._native_arrow:
            return
        self.release()
        if requested is None or self.held is not None or not self._game_window(hwnd) or win32gui.IsIconic(hwnd):
            return
        if foreground:
            # Zpráva WM_KEYDOWN sama nemění stav fyzicky držené klávesy ve Windows.
            # Před odesláním znovu ověř fokus přes běžnou cestu vstupu.
            super().direction(hwnd, requested)
            if self.held is not None:
                self._native_arrow = True
                self._held_window = hwnd
                self.problem = None
            return
        vk = win32con.VK_LEFT if requested == "left" else win32con.VK_RIGHT
        if self._key(hwnd, vk, self._SCANS[requested], up=False, extended=True):
            self.held = requested
            self._held_window = hwnd

    def escape(self, hwnd: int) -> bool:
        if not self._game_window(hwnd) or win32gui.IsIconic(hwnd):
            return False
        if not self._key(hwnd, win32con.VK_ESCAPE, 0x01, up=False):
            return False
        sleep(.075)
        return self._key(hwnd, win32con.VK_ESCAPE, 0x01, up=True)

