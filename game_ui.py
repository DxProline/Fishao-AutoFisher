from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from window import Image

ROOT = Path(__file__).resolve().parent / "ui_reference"
SIZE = 1264, 681
Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Match:
    x: int
    y: int
    score: float

    def point(self, dx: int, dy: int) -> Point:
        return (self.x + dx) / SIZE[0], (self.y + dy) / SIZE[1]


@dataclass(frozen=True, slots=True)
class Hud:
    energy: int | None
    rod: int | None
    bait: int | None
    ready: bool
    rod_button: Point
    bait_button: Point


@dataclass(frozen=True, slots=True)
class Inventory:
    tab: str | None
    count: int | None
    slots: tuple[Point, ...]
    select: Point | None
    close: Point
    next_page: Point | None
    page: tuple[int, int] | None


class Interface:
    def __init__(self) -> None:
        self.templates = {key: self._image(ROOT / f"{key}.png") for key in
                          ("energy", "rod", "bait", "inventory", "select", "select_hover",
                           "caught", "escaped", "fishdex_button", "result_x")}
        # Porovnáváme vnitřek blesku. Okraje obsahují průhledné pozadí mapy.
        lightning = cv2.cvtColor(self.templates["energy"], cv2.COLOR_BGR2HSV)
        self.energy_mask = cv2.erode(cv2.inRange(lightning, (75, 125, 130), (110, 255, 255)),
                                     np.ones((3, 3), np.uint8))
        self.digits: dict[int, list[np.ndarray]] = {number: [] for number in range(10)}
        for file in ROOT.glob("digit_*.png"):
            digit = int(file.stem.split("_")[1])
            self.digits[digit].append(self._glyph(cv2.cvtColor(self._image(file), cv2.COLOR_BGR2GRAY)))
        for digit in range(10):
            for font in (cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX):
                image = np.zeros((30, 24), np.uint8)
                cv2.putText(image, str(digit), (1, 24), font, .85, 255, 2, cv2.LINE_AA)
                self.digits[digit].append(self._glyph(image))

    @staticmethod
    def _image(file: Path) -> Image:
        result = cv2.imdecode(np.frombuffer(file.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if result is None:
            raise OSError(f"Nelze načíst referenci {file.name}.")
        return result

    @staticmethod
    def _glyph(mask: np.ndarray) -> np.ndarray:
        points = cv2.findNonZero(mask)
        x, y, w, h = cv2.boundingRect(points)
        return cv2.resize(mask[y:y+h, x:x+w], (12, 18), interpolation=cv2.INTER_AREA) / 255.0

    def number(self, patch: Image, *, white: bool) -> int | None:
        if min(patch.shape[:2]) < 3:
            return None
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        mask = cv2.inRange(gray, 219, 255) if white else cv2.inRange(gray, 0, 127)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        found = []
        for i, (x,y,w,h,area) in enumerate(stats[1:], 1):
            if not (2 <= w <= 17 and 6 <= h <= 24 and 7 <= area <= 230):
                continue
            glyph = self._glyph(((labels[y:y+h, x:x+w] == i) * 255).astype(np.uint8))
            scores = sorted((min(float(np.abs(g - glyph).mean()) for g in templates), n)
                            for n, templates in self.digits.items())
            if scores[0][0] > .29 or scores[1][0] - scores[0][0] < .018:
                return None
            found.append((x, scores[0][1]))
        if not 1 <= len(found) <= 3:
            return None
        return int("".join(str(digit) for _, digit in sorted(found)))

    def normalize(self, frame: Image) -> Image:
        if frame.shape[:2] == (SIZE[1], SIZE[0]):
            return frame
        return cv2.resize(frame, SIZE, interpolation=cv2.INTER_AREA)

    def _find(self, image: Image, key: str, part: tuple[int,int,int,int], threshold: float) -> Match | None:
        x, y, r, b = part
        sample = image[y:b,x:r]
        template = self.templates[key]
        if sample.shape[0] < template.shape[0] or sample.shape[1] < template.shape[1]:
            return None
        values = cv2.matchTemplate(sample, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, (dx,dy) = cv2.minMaxLoc(values)
        return Match(x+dx,y+dy,score) if score >= threshold else None

    def result(self, image: Image) -> str | None:
        if self._find(image,"escaped",(350,225,925,337),.87):
            return "escaped"
        if self._find(image,"caught",(330,93,1000,165),.83):
            return "caught"
        if (self._find(image, "fishdex_button", (455, 480, 800, 573), .84)
                and self.result_close(image, "caught") is not None):
            return "caught"
        return None

    def result_close(self, image: Image, result: str) -> Point | None:
        # Záložní klik smí mířit jen na rozpoznaný křížek konkrétního výsledku.
        area = (850, 65, 1015, 164) if result == "caught" else (775, 195, 928, 301)
        match = self._find(image, "result_x", area, .85)
        if match is None:
            return None
        height, width = self.templates["result_x"].shape[:2]
        return match.point(width // 2, height // 2)

    def hud(self, image: Image) -> Hud | None:
        values = cv2.matchTemplate(image[:74, 450:640], self.templates["energy"],
                                   cv2.TM_SQDIFF_NORMED, mask=self.energy_mask)
        values[~np.isfinite(values)] = np.inf
        error, _, (energy_x, energy_y), _ = cv2.minMaxLoc(values)
        rod = self._find(image,"rod",(1070,280,1264,392),.78)
        bait = self._find(image,"bait",(1070,396,1264,519),.78)
        if error > .06 or rod is None or bait is None:
            return None
        # Barevné záhlaví ROD zšedne, dokud hráč čeká na záběr.
        cell = image[rod.y+4:rod.y+11,rod.x+8:rod.x+23]
        hsv = cv2.cvtColor(cell,cv2.COLOR_BGR2HSV)
        ready = float(hsv[:,:,1].mean()) >= 100 and float(hsv[:,:,2].mean()) >= 160
        energy_x += 450
        e = self.number(image[energy_y+11:energy_y+31,energy_x+55:energy_x+98],white=False)
        r = self.number(image[rod.y+76:rod.y+93,rod.x+44:rod.x+79],white=True)
        b = self.number(image[bait.y+76:bait.y+93,bait.x+44:bait.x+79],white=True)
        return Hud(e,r,b,ready,rod.point(50,48),bait.point(50,48))

    def inventory(self, image: Image) -> Inventory | None:
        anchor = self._find(image,"inventory",(385,15,880,150),.85)
        if anchor is None:
            return None
        x,y = anchor.x,anchor.y
        # Oranžová označuje otevřenou záložku, zbylé jsou žluté.
        hsv = cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        rod_hue = float(np.median(hsv[y+78:y+106,x-252:x-225,0]))
        bait_hue = float(np.median(hsv[y+135:y+161,x-252:x-225,0]))
        tab = "rod" if rod_hue + 4 < bait_hue else "bait" if bait_hue + 4 < rod_hue else None
        select_area = (x+270,y+102,x+471,y+192)
        select = self._find(image,"select",select_area,.83) or self._find(image,"select_hover",select_area,.83)
        count = self.number(image[y+70:y+100,x+319:x+411],white=True)
        slots = []
        if tab:
            for row in range(2):
                for col in range(5):
                    dx,dy=-50+102*col,313+101*row
                    patch = image[y+dy-27:y+dy+27,x+dx-26:x+dx+26]
                    if patch.shape != (54,52,3):
                        continue
                    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
                    if float(np.mean(gray < 147)) >= .014 and float(np.mean(gray > 175)) >= .35:
                        slots.append(anchor.point(dx,dy))
        # Ovládání stránky je ověřené na jednom listu; klik vzniká pouze po čtení obou čísel.
        current = self.number(image[y+486:y+505,x+327:x+337],white=False)
        total = self.number(image[y+486:y+505,x+352:x+363],white=False)
        page = (current,total) if current and total and current <= total else None
        return Inventory(tab,count,tuple(slots),select.point(51,14) if select else None,
                         anchor.point(464,23),anchor.point(400,496) if page and page[0]<page[1] else None,page)

