"""Rozpoznání turnajové nabídky podle snímků uživatele."""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from controls import Controls
from game_ui import Interface, Point, SIZE
from window import Image, Region

# Souřadnice se vztahují k obrazu normalizovanému na SIZE.
BOXES = {
    "icon": (602, 619, 636, 649),
    "wallet": (251, 11, 288, 50),
    "panel": (546, 310, 609, 343),
    "signup": (850, 560, 946, 588),
    "coin": (797, 557, 826, 583),
    "close": (972, 53, 1000, 81),
}
FEE = (770, 559, 799, 587)
BALANCE = (289, 18, 350, 39)


def _crop(image: Image, box: tuple[int, int, int, int]) -> Image:
    x, y, r, b = box
    return image[y:b, x:r]


def _center(key: str) -> Point:
    x, y, r, b = BOXES[key]
    return (x + r) / (2 * SIZE[0]), (y + b) / (2 * SIZE[1])


def _signup_active(image: Image) -> bool:
    button = cv2.cvtColor(_crop(image, BOXES["signup"]), cv2.COLOR_BGR2HSV)
    return float(np.mean(button[:, :, 1] >= 132)) >= .45


class Tournament:
    def __init__(self, ui: Interface, profile: Path) -> None:
        self.ui = ui
        with np.load(profile, allow_pickle=False) as data:
            self.templates = {key: data[key] for key in BOXES}
            self.max_fee = int(data["fee"].item())
        if self.max_fee <= 0 or any(
            tile.shape != (BOXES[key][3] - BOXES[key][1], BOXES[key][2] - BOXES[key][0], 3)
            or tile.dtype != np.uint8 for key, tile in self.templates.items()
        ):
            raise ValueError("Turnajový profil není platný.")
        self.next_check = 30.
        self.opening_until = 0.
        self.last_signup = -600.
        self.previous: str | None = None
        self.log = logging.getLogger("fishao")

    @staticmethod
    def calibrate(path: Path, hud: Image, panel: Image) -> int:
        ui = Interface()
        hud, panel = ui.normalize(hud), ui.normalize(panel)
        fee = ui.number(_crop(panel, FEE), white=True)
        balance = ui.number(cv2.convertScaleAbs(_crop(panel, BALANCE), alpha=1.45), white=False)
        tiles = {key: _crop(hud if key in ("icon", "wallet") else panel, box)
                 for key, box in BOXES.items()}
        if (ui.hud(hud) is None or fee is None or fee <= 0 or balance is None or balance < fee
                or not _signup_active(panel) or any(float(tile.std()) < 12 for tile in tiles.values())):
            raise ValueError("Na snímcích musí být herní lišta a aktivní turnajová přihláška s čitelnou cenou v mincích.")
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, fee=np.int64(fee), **tiles)
        return fee

    def _matches(self, image: Image, key: str) -> bool:
        x, y, r, b = BOXES[key]
        search = image[max(y - 5, 0):b + 5, max(x - 5, 0):r + 5]
        return float(cv2.matchTemplate(search, self.templates[key], cv2.TM_CCOEFF_NORMED).max()) >= .87

    def tick(self, image: Image, ready: bool, now: float, controls: Controls, region: Region) -> str | None:
        if now < self.next_check and now >= self.opening_until:
            return None
        action = None
        if self._matches(image, "panel"):
            fee = self.ui.number(_crop(image, FEE), white=True)
            balance = self.ui.number(cv2.convertScaleAbs(_crop(image, BALANCE), alpha=1.45), white=False)
            if (now - self.last_signup >= 600. and fee == self.max_fee
                    and balance is not None and balance >= fee
                    and all(self._matches(image, k) for k in ("signup", "coin", "wallet"))
                    and _signup_active(image)):
                action = "signup"
            elif self._matches(image, "close"):
                action = "close"
            else:
                return "Čekám na zavření turnajového panelu."
        elif now < self.opening_until:
            return "Ověřuji turnajový panel."
        elif ready and now >= self.next_check and self._matches(image, "icon"):
            action = "icon"
        previous, self.previous = self.previous, action
        if action is not None and previous == action and controls.click(region, *_center(action)):
            self.previous = None
            self.next_check = now + 300.
            self.opening_until = now + 3. if action == "icon" else 0.
            if action == "signup":
                self.last_signup = now
                self.log.info("Odeslána přihláška do turnaje. Vstupné: %s mincí.", self.max_fee)
        return {"icon": "Otevírám turnaje.", "signup": "Přihlašuji turnaj.", "close": "Zavírám turnaje."}.get(action)
