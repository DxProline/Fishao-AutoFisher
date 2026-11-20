from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import cv2
import mss
import numpy as np
import win32con
import win32gui
import win32ui
from numpy.typing import NDArray

Image = NDArray[np.uint8]


class FrameUnavailable(Exception):
    """Přechodný stav, při kterém není bezpečné snímat."""


@dataclass(frozen=True, slots=True)
class Region:
    hwnd: int
    x: int
    y: int
    width: int
    height: int


class WindowCapture:
    def __init__(self, title: str, *, background: bool = False) -> None:
        self.title = title
        self.background = background
        self._mss = None if background else mss.MSS()

    def __enter__(self) -> WindowCapture:
        return self

    def __exit__(self, *_: object) -> None:
        if self._mss is not None:
            self._mss.close()

    def _window_region(self) -> Region:
        try:
            hwnd = win32gui.FindWindow(None, self.title)
            if not hwnd or not win32gui.IsWindowVisible(hwnd):
                raise FrameUnavailable(f"Čekám na spuštěné okno „{self.title}“.")
            if win32gui.IsIconic(hwnd):
                raise FrameUnavailable("Hra je minimalizovaná. Obnov její okno, může zůstat překryté.")
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            x, y = win32gui.ClientToScreen(hwnd, (left, top))
            return Region(hwnd, x, y, right - left, bottom - top)
        except win32gui.error as exc:
            raise FrameUnavailable("Okno právě změnilo stav.") from exc

    @staticmethod
    def _window_pixels(region: Region, box: tuple[int, int, int, int], *, print_client: bool = False) -> Image:
        x, y, w, h = box
        handle = win32gui.GetDC(region.hwnd)
        try:
            source = win32ui.CreateDCFromHandle(handle)
            target = source.CreateCompatibleDC()
            try:
                bitmap = win32ui.CreateBitmap()
                bitmap.CreateCompatibleBitmap(source, w, h)
                try:
                    previous = target.SelectObject(bitmap)
                    try:
                        if print_client:
                            printer = ctypes.windll.user32.PrintWindow
                            printer.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
                            printer.restype = wintypes.BOOL
                            if not printer(region.hwnd, target.GetSafeHdc(), 3):
                                raise FrameUnavailable("Okno nepředalo obraz pro snímání na pozadí.")
                        else:
                            target.BitBlt((0, 0), (w, h), source, (x, y), win32con.SRCCOPY)
                        pixels = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8)
                        return cv2.cvtColor(pixels.reshape(h, w, 4), cv2.COLOR_BGRA2BGR)
                    finally:
                        target.SelectObject(previous)
                finally:
                    win32gui.DeleteObject(bitmap.GetHandle())
            finally:
                target.DeleteDC()
        finally:
            win32gui.ReleaseDC(region.hwnd, handle)

    def _foreground_region(self) -> Region:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if (
                not hwnd
                or win32gui.GetWindowText(hwnd).strip().casefold() != self.title.casefold()
                or not win32gui.IsWindowVisible(hwnd)
                or win32gui.IsIconic(hwnd)
            ):
                raise FrameUnavailable(f"Čekám na aktivní okno „{self.title}“.")
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            x, y = win32gui.ClientToScreen(hwnd, (left, top))
        except win32gui.error as exc:
            raise FrameUnavailable("Okno právě změnilo stav.") from exc
        return Region(hwnd, x, y, right - left, bottom - top)

    def grab(self, box: tuple[int, int, int, int] | None = None,
             *, expected: tuple[int, int] | None = None) -> tuple[Image, Region]:
        from mss.exception import ScreenShotError

        region = self._window_region() if self.background else self._foreground_region()
        if expected is not None and expected != (region.width, region.height):
            raise FrameUnavailable("Změnil se rozměr okna.")
        if region.width < 2 or region.height < 2:
            raise FrameUnavailable("Okno má příliš malou klientskou plochu.")
        x, y, w, h = box or (0, 0, region.width, region.height)
        if not (0 <= x < x + w <= region.width and 0 <= y < y + h <= region.height):
            raise FrameUnavailable("Výřez už neleží uvnitř okna.")
        if self.background:
            try:
                image = self._window_pixels(region, (x, y, w, h))
            except (win32gui.error, win32ui.error, ValueError):
                image = None
            if image is None or not image.any():
                try:
                    full = self._window_pixels(region, (0, 0, region.width, region.height), print_client=True)
                except (win32gui.error, win32ui.error, ValueError) as exc:
                    raise FrameUnavailable("Okno hry dočasně neposkytuje snímky.") from exc
                image = full[y:y+h, x:x+w].copy()
            if self._window_region() != region or not image.any():
                raise FrameUnavailable("Čekám na platný snímek hry.")
            return image, region
        assert self._mss is not None
        if not any(
            monitor["left"] <= region.x
            and monitor["top"] <= region.y
            and region.x + region.width <= monitor["left"] + monitor["width"]
            and region.y + region.height <= monitor["top"] + monitor["height"]
            for monitor in self._mss.monitors[1:]
        ):
            raise FrameUnavailable("Přesuň celé okno hry na jeden monitor.")
        try:
            screenshot = self._mss.grab({
                "left": region.x + x, "top": region.y + y,
                "width": w, "height": h,
            })
        except ScreenShotError as exc:
            raise FrameUnavailable("Windows právě neumožnil snímek pořídit.") from exc
        if self._foreground_region() != region:
            raise FrameUnavailable("Okno se právě přesouvá nebo mění velikost.")
        return cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR), region

