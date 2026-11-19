from __future__ import annotations

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
    def __init__(self, title: str) -> None:
        self.title = title
        self._mss = mss.MSS()

    def __enter__(self) -> WindowCapture:
        return self

    def __exit__(self, *_: object) -> None:
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

    def grab(self, box: tuple[int, int, int, int] | None = None,
             *, expected: tuple[int, int] | None = None) -> tuple[Image, Region]:
        from mss.exception import ScreenShotError

        region = self._foreground_region()
        if expected is not None and expected != (region.width, region.height):
            raise FrameUnavailable("Změnil se rozměr okna.")
        if region.width < 2 or region.height < 2:
            raise FrameUnavailable("Okno má příliš malou klientskou plochu.")
        if not any(
            monitor["left"] <= region.x
            and monitor["top"] <= region.y
            and region.x + region.width <= monitor["left"] + monitor["width"]
            and region.y + region.height <= monitor["top"] + monitor["height"]
            for monitor in self._mss.monitors[1:]
        ):
            raise FrameUnavailable("Přesuň celé okno hry na jeden monitor.")
        try:
            x, y, w, h = box or (0, 0, region.width, region.height)
            if not (0 <= x < x + w <= region.width and 0 <= y < y + h <= region.height):
                raise FrameUnavailable("Výřez už neleží uvnitř okna.")
            screenshot = self._mss.grab({
                "left": region.x + x, "top": region.y + y,
                "width": w, "height": h,
            })
        except ScreenShotError as exc:
            raise FrameUnavailable("Windows právě neumožnil snímek pořídit.") from exc
        if self._foreground_region() != region:
            raise FrameUnavailable("Okno se právě přesouvá nebo mění velikost.")
        return cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR), region

