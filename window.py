from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import cv2
import mss
import numpy as np
import win32gui
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

    def _background_region(self) -> Region:
        try:
            hwnd = win32gui.FindWindow(None, self.title)
            if not hwnd or not win32gui.IsWindowVisible(hwnd):
                raise FrameUnavailable(f"Čekám na okno „{self.title}“.")
            if win32gui.IsIconic(hwnd):
                raise FrameUnavailable("Hra je minimalizovaná. Obnov její okno.")
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            x, y = win32gui.ClientToScreen(hwnd, (left, top))
        except win32gui.error as exc:
            raise FrameUnavailable("Okno právě změnilo stav.") from exc
        return Region(hwnd, x, y, right - left, bottom - top)

    @staticmethod
    def _background_frame(region: Region) -> Image:
        import win32ui

        handle = win32gui.GetDC(region.hwnd)
        if not handle:
            raise FrameUnavailable("Okno hry právě neposkytuje obraz.")
        bitmap = memory = None
        try:
            source = win32ui.CreateDCFromHandle(handle)
            memory = source.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, region.width, region.height)
            memory.SelectObject(bitmap)
            printer = ctypes.windll.user32.PrintWindow
            printer.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
            printer.restype = wintypes.BOOL
            if not printer(region.hwnd, memory.GetSafeHdc(), 3):
                raise FrameUnavailable("Hra neposkytuje obraz na pozadí. Použij režim v popředí.")
            pixels = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8)
        except (OSError, win32gui.error, win32ui.error) as exc:
            raise FrameUnavailable("Okno hry právě neposkytuje obraz.") from exc
        finally:
            if memory is not None:
                memory.DeleteDC()
            if bitmap is not None and bitmap.GetHandle():
                win32gui.DeleteObject(bitmap.GetHandle())
            win32gui.ReleaseDC(region.hwnd, handle)
        if pixels.size != region.width * region.height * 4 or not np.any(pixels.reshape(-1, 4)[:, :3]):
            raise FrameUnavailable("Hra neposkytuje platný obraz na pozadí. Použij režim v popředí.")
        return cv2.cvtColor(pixels.reshape(region.height, region.width, 4), cv2.COLOR_BGRA2BGR)

    def grab(self, box: tuple[int, int, int, int] | None = None,
             *, expected: tuple[int, int] | None = None) -> tuple[Image, Region]:
        from mss.exception import ScreenShotError

        locate = self._background_region if self.background else self._foreground_region
        region = locate()
        if expected is not None and expected != (region.width, region.height):
            raise FrameUnavailable("Změnil se rozměr okna.")
        if region.width < 2 or region.height < 2:
            raise FrameUnavailable("Okno má příliš malou klientskou plochu.")
        x, y, w, h = box or (0, 0, region.width, region.height)
        if not (0 <= x < x + w <= region.width and 0 <= y < y + h <= region.height):
            raise FrameUnavailable("Výřez už neleží uvnitř okna.")
        if self.background:
            image = self._background_frame(region)
            if locate() != region:
                raise FrameUnavailable("Okno se právě přesouvá nebo mění velikost.")
            return image[y:y+h, x:x+w].copy(), region
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
        if locate() != region:
            raise FrameUnavailable("Okno se právě přesouvá nebo mění velikost.")
        return cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR), region

