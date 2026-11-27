from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

from window import Image

ROOT = Path(__file__).resolve().parent / "ui_reference"
SIZE = 1264, 681
Point = tuple[float, float]

REQUIRED_TEMPLATES = (
    "energy", "rod", "bait", "inventory", "select", "select_hover",
    "caught", "escaped", "fishdex_button", "result_x", "empty_hook", "energy_plus",
    "energy_shop", "energy_cash", "energy_price_cash", "energy_offer_five",
    "missions_icon", "missions_title", "mission_rare_fish", "mission_daily_quests",
    "tournament_icon", "tournament_podium", "tournament_start", "tournament_countdown", "activity_close",
    "tournament_coin", "tournament_signup", "wallet_coin",
    "world_icon", "world_title", "trout_destination", "trout_spawn",
    "supply_icon", "supply_title", "supply_cash", "simple_rod", "big_rod",
    "supply_price", "bait_tab", "rod_tab", "worms_bait", "bait_price", "shop_close",
    "line_icon", "line_title", "line_upgrade",
    "rod_basic", "bait_empty", "bread_icon", "timed_fishing",
)

OPTIONAL_TEMPLATES = (
    "mission_accept", "mission_claim", "mission_complete", "mission_reward",
    "map_title", "bakery_inside",
)


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
    ready: bool | None
    rod_button: Point
    bait_button: Point
    basic_rod: bool = False
    bread: bool = False


@dataclass(frozen=True, slots=True)
class Inventory:
    tab: str | None
    count: int | None
    slots: tuple[Point, ...]
    select: Point | None
    close: Point
    next_page: Point | None
    page: tuple[int, int] | None
    bread_slots: tuple[Point, ...] = ()


@dataclass(frozen=True, slots=True)
class EnergyShop:
    balance: int | None
    price: int | None
    energy: int | None
    buy: Point | None
    close: Point | None


@runtime_checkable
class GameUI(Protocol):
    """Společný kontrakt mezi skutečným Interface a testovacími náhradami UI."""

    def normalize(self, frame: Image) -> Image: ...
    def find(self, image: Image, key: str, part: tuple[int, int, int, int], threshold: float) -> Match | None: ...
    def number(self, patch: Image, *, white: bool, max_digits: int = 3, white_floor: int = 219) -> int | None: ...
    def level_ok(self, image: Image) -> Point | None: ...
    def mission_card_action(self, image: Image, title: Match) -> tuple[str, Point] | None: ...
    def mission_popup_action(self, image: Image) -> tuple[str, Point] | None: ...
    def find_green_button(self, image: Image, area: tuple[int, int, int, int]) -> Point | None: ...
    def result(self, image: Image) -> str | None: ...
    def timed_fishing(self, image: Image) -> bool: ...
    def result_close(self, image: Image, result: str) -> Point | None: ...
    def energy_button(self, image: Image) -> Point | None: ...
    def energy_shop(self, image: Image) -> EnergyShop | None: ...
    def hud(self, image: Image) -> Hud | None: ...
    def inventory(self, image: Image) -> Inventory | None: ...


class Interface:
    def __init__(self) -> None:
        self.templates = {key: self._image(ROOT / f"{key}.png") for key in REQUIRED_TEMPLATES}
        for key in OPTIONAL_TEMPLATES:
            path = ROOT / f"{key}.png"
            if path.is_file():
                self.templates[key] = self._image(path)
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

        self.ok_glyphs: dict[str, list[np.ndarray]] = {"O": [], "K": []}
        for letter, glyphs in self.ok_glyphs.items():
            for font in (cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX, cv2.FONT_HERSHEY_TRIPLEX):
                for thickness in (1, 2):
                    tile = np.zeros((32, 30), np.uint8)
                    cv2.putText(tile, letter, (2, 26), font, .87, 255, thickness, cv2.LINE_8)
                    glyphs.append(self._glyph(tile))

    @staticmethod
    def _image(file: Path) -> Image:
        result = cv2.imdecode(np.frombuffer(file.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if result is None:
            raise OSError(f"Nelze načíst referenci {file.name}.")
        return result

    @staticmethod
    def _glyph(mask: np.ndarray) -> np.ndarray:
        points = cv2.findNonZero(mask)
        if points is None:
            return np.zeros((18, 12), np.float32)
        x, y, w, h = cv2.boundingRect(points)
        return cv2.resize(mask[y:y+h, x:x+w], (12, 18), interpolation=cv2.INTER_AREA) / 255.0

    def number(self, patch: Image, *, white: bool, max_digits: int = 3, white_floor: int = 219) -> int | None:
        if min(patch.shape[:2]) < 3:
            return None
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        mask = cv2.inRange(gray, white_floor, 255) if white else cv2.inRange(gray, 0, 127)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        found = []
        for i, (x, y, w, h, area) in enumerate(stats[1:], 1):
            if not (2 <= w <= 17 and 6 <= h <= 24 and 7 <= area <= 230):
                continue
            glyph = self._glyph(((labels[y:y+h, x:x+w] == i) * 255).astype(np.uint8))
            scores = sorted((min(float(np.abs(g - glyph).mean()) for g in templates), n)
                            for n, templates in self.digits.items())
            if scores[0][0] > .29 or scores[1][0] - scores[0][0] < .018:
                return None
            found.append((x, scores[0][1]))
        if not 1 <= len(found) <= max_digits:
            return None
        return int("".join(str(digit) for _, digit in sorted(found)))

    @staticmethod
    def normalize(frame: Image) -> Image:
        if frame.shape[:2] == (SIZE[1], SIZE[0]):
            return frame
        return cv2.resize(frame, SIZE, interpolation=cv2.INTER_AREA)

    def find(self, image: Image, key: str, part: tuple[int, int, int, int], threshold: float) -> Match | None:
        template = self.templates.get(key)
        if template is None:
            return None
        h_img, w_img = image.shape[:2]
        x = max(0, part[0])
        y = max(0, part[1])
        r = min(w_img, part[2])
        b = min(h_img, part[3])
        if r <= x or b <= y:
            return None
        sample = image[y:b, x:r]
        if sample.shape[0] < template.shape[0] or sample.shape[1] < template.shape[1]:
            return None
        values = cv2.matchTemplate(sample, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, (dx, dy) = cv2.minMaxLoc(values)
        return Match(x + dx, y + dy, score) if score >= threshold else None

    def _find(self, image: Image, key: str, part: tuple[int, int, int, int], threshold: float) -> Match | None:
        return self.find(image, key, part, threshold)

    def level_ok(self, image: Image) -> Point | None:
        """Rozpozná zelené OK dole na světlém panelu s velkým číslem úrovně."""
        for strip in (image[188:475, 390:442], image[188:475, 817:869]):
            hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
            cream = (hsv[:, :, 1] <= 140) & (hsv[:, :, 2] >= 165)
            if np.count_nonzero(cream) < .42 * cream.size:
                return None
        digits = cv2.cvtColor(image[200:390, 440:824], cv2.COLOR_BGR2HSV)
        ink = cv2.inRange(digits, (0, 100, 90), (42, 255, 255))
        ink |= cv2.inRange(digits, (168, 100, 90), (179, 255, 255))
        _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
        if not any(8 <= w <= 260 and 32 <= h <= 140 and area >= 180
                   and abs(x + w / 2 - digits.shape[1] / 2) <= 115
                   for x, _, w, h, area in stats[1:]):
            return None
        x0, y0, x1, y1 = 460, 440, 804, 610
        hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, (32, 80, 75), (92, 255, 255))
        green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(green)
        points = []
        for x, y, w, h, area in stats[1:count]:
            if not (32 <= w <= 250 and 16 <= h <= 64 and 1.4 <= w / h <= 8.5 and area >= .62 * w * h):
                continue
            inner = hsv[y + 2:y + h - 2, x + w // 6:x + w - w // 6]
            if min(inner.shape[:2]) < 6 or not self._reads_ok(inner):
                continue
            if abs(x + x0 + w / 2 - SIZE[0] / 2) <= 64:
                points.append(Match(x + x0, y + y0, 1.).point(w // 2, h // 2))
        return points[0] if len(points) == 1 else None

    def _reads_ok(self, hsv: Image) -> bool:
        ink = cv2.inRange(hsv, (0, 0, 175), (179, 105, 255))
        _, labels, stats, centers = cv2.connectedComponentsWithStats(ink)
        candidates = [
            i for i, (_, _, w, h, a) in enumerate(stats)
            if i and 4 <= w <= 32 and 6 <= h <= 34 and a >= 10
        ]
        if len(candidates) < 2:
            return False
        # Ponechej hlavní písmena podobné výšky (ignoruj drobné 1-2px odlesky/tečky),
        # ale odmítni třípísmenné popisky jako YES.
        max_h = max(stats[i, cv2.CC_STAT_HEIGHT] for i in candidates)
        letters = [i for i in candidates if stats[i, cv2.CC_STAT_HEIGHT] >= max(6, round(max_h * 0.55))]
        if len(letters) != 2:
            return False
        letters.sort(key=lambda i: centers[i, 0])
        if abs(centers[letters[0], 1] - centers[letters[1], 1]) > 4:
            return False
        scores = []
        for letter, index in zip("OK", letters):
            x, y, w, h, _ = stats[index]
            glyph = self._glyph(((labels[y:y+h, x:x+w] == index) * 255).astype(np.uint8))
            scores.append(min(float(np.abs(glyph - reference).mean()) for reference in self.ok_glyphs[letter]))
        return max(scores) <= .31

    def mission_card_action(self, image: Image, title: Match) -> tuple[str, Point] | None:
        """
        Hledá potvrzovací tlačítko (přijmout / vyzvednout odměnu) na otevřené kartě mise.
        Využije volitelné šablony, případně zelené/oranžové tlačítko v oblasti karty.
        """
        x, y = title.x, title.y
        card_box = (max(0, x - 250), max(0, y + 160), min(SIZE[0], x + 230), min(SIZE[1], y + 435))
        for key, kind in (("mission_claim", "claim"), ("mission_complete", "claim"), ("mission_accept", "accept")):
            match = self._find(image, key, card_box, .85)
            if match is not None:
                tpl = self.templates[key]
                return kind, match.point(tpl.shape[1] // 2, tpl.shape[0] // 2)
        btn = self.find_action_button(
            image,
            (max(0, x - 240), max(0, y + 310), min(SIZE[0], x + 205), min(SIZE[1], y + 425)),
            allow_green=True,
            allow_orange=True,
        )
        if btn is not None:
            color, point = btn
            return ("claim" if color == "orange" else "action"), point
        return None

    def mission_popup_action(self, image: Image) -> tuple[str, Point] | None:
        """
        Hledá samostatné okno odměny, potvrzení mise nebo OK/Collect dialog
        (zelené i oranžové tlačítko uvnitř světlého modálního okna).
        """
        for key, area, kind in (
            ("mission_reward", (360, 110, 920, 585), "claim"),
            ("mission_claim", (310, 220, 955, 585), "claim"),
            ("mission_accept", (310, 220, 955, 585), "accept"),
        ):
            match = self._find(image, key, area, .86)
            if match is not None:
                tpl = self.templates[key]
                return kind, match.point(tpl.shape[1] // 2, tpl.shape[0] // 2)

        # Neklikej na vyskakovací tlačítka, pokud je otevřený známý jiný panel hry.
        if (
            self._find(image, "missions_title", (532, 37, 738, 96), .88) is not None
            or self._find(image, "inventory", (385, 15, 880, 150), .85) is not None
            or self._find(image, "energy_shop", (430, 79, 830, 149), .88) is not None
            or self._find(image, "supply_title", (538, 51, 689, 117), .90) is not None
            or self._find(image, "line_title", (474, 35, 686, 92), .88) is not None
            or self._find(image, "world_title", (495, 0, 771, 51), .90) is not None
            or self._find(image, "map_title", (480, 0, 799, 70), .88) is not None
            or self._find(image, "bakery_inside", (362, 50, 890, 190), .88) is not None
            or self._find(image, "tournament_podium", (530, 297, 745, 357), .88) is not None
            or self.result(image) is not None
        ):
            return None

        # Ověř přítomnost centrálního světlého/krémového dialogového okna (jako např. "WOW, YOU'RE FAST!" / quest popup).
        interior = image[255:515, 335:925]
        if min(interior.shape[:2]) < 10:
            return None
        interior_hsv = cv2.cvtColor(interior, cv2.COLOR_BGR2HSV)
        cream_ratio = float(np.mean((interior_hsv[:, :, 1] <= 95) & (interior_hsv[:, :, 2] >= 185)))
        if cream_ratio < 0.45:
            return None

        btn = self.find_action_button(image, (310, 330, 955, 585), allow_green=True, allow_orange=True)
        if btn is None:
            return None
        color, point = btn
        return ("claim" if color == "orange" else "accept"), point

    @staticmethod
    def find_action_button(
        image: Image,
        area: tuple[int, int, int, int],
        *,
        allow_green: bool = True,
        allow_orange: bool = True,
    ) -> tuple[str, Point] | None:
        """Najde výrazné zelené nebo oranžové tlačítko s bílým textem v zadaném výřezu."""
        h_img, w_img = image.shape[:2]
        x0 = max(0, area[0])
        y0 = max(0, area[1])
        x1 = min(w_img, area[2])
        y1 = min(h_img, area[3])
        if x1 - x0 < 24 or y1 - y0 < 16:
            return None
        hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        masks: list[tuple[str, np.ndarray]] = []
        if allow_orange:
            # Oranžová tlačítka jako "Collect" / "Claim" (medH~16, medS~230, medV~255); vynechá hnědý okraj.
            masks.append(("orange", cv2.inRange(hsv, (8, 185, 190), (22, 255, 255))))
        if allow_green:
            masks.append(("green", cv2.inRange(hsv, (34, 95, 80), (92, 255, 255))))

        candidates: list[tuple[int, str, Point]] = []
        kernel = np.ones((5, 5), np.uint8)
        for color_name, raw_mask in masks:
            closed = cv2.morphologyEx(raw_mask, cv2.MORPH_CLOSE, kernel)
            count, _, stats, _ = cv2.connectedComponentsWithStats(closed)
            for x, y, w, h, area_px in stats[1:count]:
                if 46 <= w <= 260 and 20 <= h <= 64 and 1.5 <= w / h <= 8.0 and area_px >= .65 * w * h:
                    inner = hsv[y + 2:y + h - 2, x + 4:x + w - 4]
                    if min(inner.shape[:2]) >= 4:
                        white = float(np.mean((inner[:, :, 1] <= 90) & (inner[:, :, 2] >= 185)))
                        if 0.02 <= white <= 0.48:
                            candidates.append((int(area_px), color_name, Match(x + x0, y + y0, 1.).point(w // 2, h // 2)))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        _, color_name, point = candidates[0]
        return color_name, point

    @classmethod
    def find_green_button(cls, image: Image, area: tuple[int, int, int, int]) -> Point | None:
        """Najde výrazné zelené tlačítko v zadaném výřezu obrazovky."""
        found = cls.find_action_button(image, area, allow_green=True, allow_orange=False)
        return found[1] if found is not None else None

    def result(self, image: Image) -> str | None:
        if self._find(image, "escaped", (350, 225, 925, 337), .87):
            return "escaped"
        if self._find(image, "caught", (330, 93, 1000, 165), .83):
            return "caught"
        if (self._find(image, "fishdex_button", (455, 480, 800, 573), .84)
                and self.result_close(image, "caught") is not None):
            return "caught"
        return None

    def timed_fishing(self, image: Image) -> bool:
        return self._find(image, "timed_fishing", (50, 97, 410, 200), .90) is not None

    def result_close(self, image: Image, result: str) -> Point | None:
        area = (850, 65, 1015, 164) if result == "caught" else (775, 195, 928, 301)
        match = self._find(image, "result_x", area, .85)
        if match is None:
            return None
        height, width = self.templates["result_x"].shape[:2]
        return match.point(width // 2, height // 2)

    def energy_button(self, image: Image) -> Point | None:
        match = self._find(image, "energy_plus", (596, 12, 634, 46), .85)
        return match.point(9, 10) if match else None

    def energy_shop(self, image: Image) -> EnergyShop | None:
        if self._find(image, "energy_shop", (430, 79, 830, 149), .88) is None:
            return None
        funds = self._find(image, "energy_cash", (632, 538, 705, 590), .86)
        button = self._find(image, "energy_price_cash", (803, 156, 866, 211), .86)
        offer = self._find(image, "energy_offer_five", (385, 156, 525, 211), .88)
        close = self._find(image, "result_x", (932, 73, 981, 116), .83)
        balance = self.number(image[555:580, 690:763], white=False, max_digits=6) if funds else None
        price = self.number(image[172:197, 863:910], white=True) if button else None
        energy_cell = image[19:39, 558:601]
        energy = self.number(cv2.convertScaleAbs(energy_cell, alpha=1.45), white=False)
        return EnergyShop(balance, price, energy, button.point(69, 18) if button and offer else None,
                          close.point(13, 13) if close else None)

    @staticmethod
    def fishing_ready(image: Image, rod: Match, bait: Match) -> bool | None:
        toolbar = cv2.cvtColor(image[628:655, 441:763], cv2.COLOR_BGR2HSV)
        colored = grey = 0
        for left, top, right, bottom in ((0, 3, 20, 24), (63, 0, 83, 24), (295, 8, 322, 27)):
            hue, saturation, value = np.moveaxis(toolbar[top:bottom, left:right], 2, 0)
            colored += np.mean((hue >= 35) & (saturation >= 80) & (value >= 65)) >= .35
            grey += np.mean((saturation <= 42) & (value >= 65)) >= .55
        labels = []
        for match in (rod, bait):
            header = image[match.y+4:match.y+11, match.x+8:match.x+23]
            labels.append(float(cv2.cvtColor(header, cv2.COLOR_BGR2HSV)[:, :, 1].mean()))
        if grey >= 2 or max(labels) <= 55:
            return False
        if colored >= 2 and min(labels) >= 100:
            return True
        return None

    def _gear_count(self, patch: Image) -> int | None:
        count = self.number(patch, white=True)
        if count is None:
            count = self.number(patch, white=True, white_floor=235)
        if count is None and self._red_badge(patch):
            faint = self.number(patch, white=True, white_floor=189)
            if faint is not None and faint == self.number(patch, white=True, white_floor=193):
                return faint
        return count

    @staticmethod
    def _red_badge(badge: Image) -> bool:
        hsv = cv2.cvtColor(badge, cv2.COLOR_BGR2HSV)
        red = (hsv[:, :, 0] <= 13) & (hsv[:, :, 1] >= 150) & (hsv[:, :, 2] >= 175)
        return float(red.mean()) > .10

    def _basic_rod(self, image: Image, rod: Match, badge: Image) -> bool:
        if self._red_badge(badge):
            return False
        return self._find(image, "rod_basic", (rod.x+23, rod.y+33, rod.x+78, rod.y+88), .9) is not None

    def hud(self, image: Image) -> Hud | None:
        values = cv2.matchTemplate(image[:74, 450:640], self.templates["energy"],
                                   cv2.TM_SQDIFF_NORMED, mask=self.energy_mask)
        values[~np.isfinite(values)] = np.inf
        error, _, (energy_x, energy_y), _ = cv2.minMaxLoc(values)
        rod = self._find(image, "rod", (1070, 280, 1264, 392), .78)
        bait = self._find(image, "bait", (1070, 396, 1264, 519), .78)
        if error > .06 or rod is None or bait is None:
            return None
        ready = self.fishing_ready(image, rod, bait)
        energy_x += 450
        e = self.number(image[energy_y+11:energy_y+31, energy_x+55:energy_x+98], white=False)
        rod_badge = image[rod.y+76:rod.y+93, rod.x+44:rod.x+79]
        basic = ready and self._basic_rod(image, rod, rod_badge)
        r = None if basic else self._gear_count(rod_badge)
        bait_area = (bait.x+20, bait.y+32, bait.x+78, bait.y+90)
        empty = ready and self._find(image, "bait_empty", bait_area, .89)
        b = 0 if empty else self._gear_count(image[bait.y+76:bait.y+93, bait.x+44:bait.x+79])
        if b is None and ready and self._find(image, "empty_hook", (1163, 462, 1251, 550), .90):
            b = 0
        bread = bool(ready and b and self._bread(image[bait.y+32:bait.y+90, bait.x+20:bait.x+78]))
        return Hud(e, r, b, ready, rod.point(50, 48), bait.point(50, 48), basic, bread)

    def _bread(self, patch: Image) -> bool:
        template = self.templates["bread_icon"]
        for scale in (.8, 1., 1.16):
            tile = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if patch.shape[0] >= tile.shape[0] and patch.shape[1] >= tile.shape[1]:
                _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(patch, tile, cv2.TM_CCOEFF_NORMED))
                if score >= .87:
                    return True
        return False

    def inventory(self, image: Image) -> Inventory | None:
        anchor = self._find(image, "inventory", (385, 15, 880, 150), .85)
        if anchor is None:
            return None
        x, y = anchor.x, anchor.y
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        rod_hue = float(np.median(hsv[y+78:y+106, x-252:x-225, 0]))
        bait_hue = float(np.median(hsv[y+135:y+161, x-252:x-225, 0]))
        tab = "rod" if rod_hue + 4 < bait_hue else "bait" if bait_hue + 4 < rod_hue else None
        select_area = (x+270, y+102, x+471, y+192)
        select = self._find(image, "select", select_area, .83) or self._find(image, "select_hover", select_area, .83)
        count = self.number(image[y+70:y+100, x+319:x+411], white=True)
        slots = []
        bread_slots = []
        if tab:
            for row in range(2):
                for col in range(5):
                    dx, dy = -50 + 102 * col, 313 + 101 * row
                    patch = image[y+dy-27:y+dy+27, x+dx-26:x+dx+26]
                    if patch.shape != (54, 52, 3):
                        continue
                    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
                    if float(np.mean(gray < 147)) >= .014 and float(np.mean(gray > 175)) >= .35:
                        point = anchor.point(dx, dy)
                        slots.append(point)
                        if tab == "bait" and self._bread(patch):
                            bread_slots.append(point)
        current = self.number(image[y+486:y+505, x+327:x+337], white=False)
        total = self.number(image[y+486:y+505, x+352:x+363], white=False)
        page = (current, total) if current and total and current <= total else None
        return Inventory(tab, count, tuple(slots), select.point(51, 14) if select else None,
                         anchor.point(464, 23), anchor.point(400, 496) if page and page[0] < page[1] else None,
                         page, tuple(bread_slots))
