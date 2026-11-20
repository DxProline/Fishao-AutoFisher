from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from random import choice

import cv2
import numpy as np

from controls import Controls
from game_ui import ROOT, SIZE, Hud, Interface, Point
from spots import avatar_feet, casting_hover, casting_target, shore_approach, walking_direction
from window import Image, Region

CURRENT = "Aktuální místo"
LAKETOWN = "Laketown · u pekárny"
ROTATE_EVERY = 10
MAX_MAP_ATTEMPTS = 3


def locate(frame: Image, tile: Image, area: tuple[int, int, int, int], minimum: float = .86) -> Point | None:
    x, y, right, bottom = area
    sample = frame[y:bottom, x:right]
    h, w = tile.shape[:2]
    if sample.shape[0] < h or sample.shape[1] < w:
        return None
    _, score, _, (dx, dy) = cv2.minMaxLoc(cv2.matchTemplate(sample, tile, cv2.TM_CCOEFF_NORMED))
    return ((x + dx + w / 2) / SIZE[0], (y + dy + h / 2) / SIZE[1]) if score >= minimum else None


@dataclass(frozen=True, slots=True)
class Marker:
    point: Point
    tile: Image

    @classmethod
    def capture(cls, image: Image, point: Point) -> Marker:
        x, y = (round(p * size) for p, size in zip(point, SIZE))
        if not 28 <= x < SIZE[0] - 28 or not 22 <= y < SIZE[1] - 22:
            raise ValueError("Vyber výrazné místo dál od okraje okna.")
        tile = image[y-22:y+22, x-28:x+28].copy()
        if float(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY).std()) < 14:
            raise ValueError("Vyber výrazný a nepohyblivý orientační bod.")
        return cls(point, tile)

    def find(self, image: Image) -> Point | None:
        x, y = (round(p * size) for p, size in zip(self.point, SIZE))
        return locate(image, self.tile, (max(0, x-150), max(0, y-120),
                                       min(SIZE[0], x+150), min(SIZE[1], y+120)), .90)


@dataclass(frozen=True, slots=True)
class Destination:
    name: str
    area: str
    map_marker: Marker | None = None
    bank: Marker | None = None

    def save(self, folder: Path) -> None:
        if self.map_marker is None or self.bank is None:
            raise ValueError("Lovné místo potřebuje bod na mapě i na břehu.")
        folder.mkdir(parents=True, exist_ok=True)
        file = next(folder / f"{i}.npz" for i in range(1, len(list(folder.glob("*.npz"))) + 2)
                    if not (folder / f"{i}.npz").exists())
        np.savez_compressed(file, name=self.name, area=self.area, map_point=self.map_marker.point,
                            map_tile=self.map_marker.tile, bank_point=self.bank.point, bank_tile=self.bank.tile)

    @classmethod
    def load_all(cls, folder: Path) -> list[Destination]:
        destinations = [cls(CURRENT, ""), cls(LAKETOWN, "Laketown")]
        for file in sorted(folder.glob("*.npz")):
            try:
                with np.load(file, allow_pickle=False) as data:
                    name, area = str(data["name"].item()), str(data["area"].item())
                    if not name or not area or any(d.name == name for d in destinations):
                        continue
                    markers = []
                    for prefix in ("map", "bank"):
                        x, y = map(float, data[prefix + "_point"])
                        tile = data[prefix + "_tile"]
                        if not (0 < x < 1 and 0 < y < 1 and tile.shape == (44, 56, 3)
                                and tile.dtype == np.uint8):
                            raise ValueError("Neplatný orientační bod.")
                        markers.append(Marker((x, y), tile))
                    destinations.append(cls(name, area, *markers))
            except (OSError, KeyError, ValueError):
                continue
        return destinations


@dataclass(frozen=True, slots=True)
class TravelSettings:
    destination: Destination
    places: tuple[Destination, ...]
    refill: bool = True
    ponds: bool = False


class Navigator:
    AREAS = {
        "bakery_door": (65, 75, 1150, 681),
        "bakery_inside": (362, 50, 890, 190),
        "bakery_counter": (460, 235, 800, 380),
        "bakery_exit": (560, 480, 715, 607),
        "jamie_offer": (480, 140, 793, 258),
        "jamie_yes": (770, 145, 875, 244),
        "jamie_reward": (970, 85, 1250, 175),
        "jamie_done": (510, 152, 778, 252),
        "map_button": (285, 544, 640, 675),
        "map_title": (480, 0, 799, 70),
        "map_laketown": (480, 95, 732, 207),
    }

    def __init__(self, settings: TravelSettings) -> None:
        self.settings = settings
        self.destination = settings.destination
        self.laketown = Destination(LAKETOWN, "Laketown")
        self.templates = {key: Interface._image(ROOT / (key + ".png")) for key in self.AREAS}
        self.visit = self.destination.name != CURRENT
        self.refill = ""
        self.next_action = 0.
        self.previous: tuple[str, Point] | None = None
        self.previous_at = 0.
        self.next_rotation = ROTATE_EVERY
        self.map_pending = False
        self.map_attempts = self.map_open_attempts = 0
        self.arrived = False
        self.wants_bread = False
        self.jamie_close_sent = False

    def match(self, image: Image, name: str) -> Point | None:
        return locate(image, self.templates[name], self.AREAS[name])

    def reset_confirmation(self) -> None:
        self.previous = None

    def _click(self, action: str, point: Point, now: float, controls: Controls, region: Region,
               delay: float = 1.3) -> bool:
        old, self.previous = self.previous, (action, point)
        previous_at, self.previous_at = self.previous_at, now
        if (not old or now - previous_at > .6 or old[0] != action
                or max(abs(a-b) for a,b in zip(old[1], point)) > .008):
            return False
        if not controls.click(region, *point):
            return False
        self.previous = None
        self.next_action = now + delay
        return True

    def _reset_trip(self) -> None:
        self.map_pending = False
        self.map_attempts = self.map_open_attempts = 0
        self.reset_confirmation()

    def _rotate(self, casts: int, image: Image) -> None:
        if not self.settings.ponds or not self.destination.area or casts < self.next_rotation:
            return
        self.next_rotation = casts + ROTATE_EVERY
        candidates = [place for place in self.settings.places if place.name != self.destination.name
                      and place.area == self.destination.area and place.bank and place.bank.find(image)]
        if candidates:
            self.destination = choice(candidates)
            self.visit = self.arrived = True
            self._reset_trip()

    def _map(self, image: Image, goal: Destination, now: float, controls: Controls, region: Region,
             stable: bool, *, map_visible: bool = False) -> str:
        if map_visible:
            if self.map_attempts >= MAX_MAP_ATTEMPTS:
                return f"Mapa · hra nepotvrdila {goal.area}. Vyber oblast ručně."
            if goal.map_marker:
                point = goal.map_marker.find(image)
            else:
                anchor = self.match(image, "map_laketown")
                point = (anchor[0] - 21 / SIZE[0], anchor[1] + 46 / SIZE[1]) if anchor else None
            if point is None:
                self.reset_confirmation()
                return f"Mapa · čekám na značku oblasti {goal.area}"
            if self._click("map-destination", point, now, controls, region, 2.8):
                self.map_pending = True
                self.map_attempts += 1
            return f"Mapa · vybírám {goal.area}"
        if not stable:
            self.reset_confirmation()
        elif not self.map_pending and self.map_open_attempts < MAX_MAP_ATTEMPTS:
            button = self.match(image, "map_button")
            if button and self._click("map-open", button, now, controls, region):
                self.map_open_attempts += 1
        return f"Přesun do oblasti {goal.area} · čekám na načtení hry"

    def tick(self, image: Image, frame: Image, hud: Hud | None, stable: bool, casts: int,
             now: float, controls: Controls, region: Region, *, timed: bool = False,
             cancelled: Callable[[], bool] = lambda: False) -> str | None:
        self.wants_bread = False
        if timed and not self.refill:
            self.reset_confirmation()
            return None
        if now < self.next_action:
            return "Čekám na dokončení přesunu"
        if (not self.refill and stable and hud is not None and hud.bait == 0 and self.settings.refill
                and (self.match(image, "bakery_door") or self._bakery_inside(image))):
            self.refill = "enter"
            self._reset_trip()
        if self.refill:
            return self._bakery(image, frame, hud, stable, now, controls, region, cancelled)
        if not self.visit and stable and hud and hud.energy and hud.bait:
            self._rotate(casts, image)
        if self.visit:
            return self._visit(image, frame, stable, now, controls, region, cancelled)
        self.reset_confirmation()
        return None

    def _bakery_inside(self, image: Image) -> bool:
        return bool(self.match(image, "bakery_inside") or (
            self.match(image, "bakery_counter") and self.match(image, "bakery_exit")
        ))

    def _bakery(self, image: Image, frame: Image, hud: Hud | None, stable: bool, now: float,
                controls: Controls, region: Region, cancelled: Callable[[], bool]) -> str | None:
        inside = self._bakery_inside(image)
        if self.refill == "enter":
            if inside:
                self.refill = "counter"
                self.reset_confirmation()
            else:
                door = self.match(image, "bakery_door")
                if door and stable:
                    self._click("bakery-door", door, now, controls, region, 2.3)
                    return "Přesun ke dveřím pekárny"
                if stable:
                    self.refill = ""
                    self._reset_trip()
                    return None
                return "Pekárna · čekám na načtení vstupu"

        if self.refill == "counter":
            if self.match(image, "jamie_offer"):
                yes = self.match(image, "jamie_yes")
                if yes and self._click("dough-free", yes, now, controls, region):
                    self.refill = "receipt"
                return "Jamie · potvrzuji bezplatnou návnadu"
            counter = self.match(image, "bakery_counter") if inside and stable else None
            if counter:
                stand = (counter[0], counter[1] + 43 / SIZE[1])
                feet = avatar_feet(frame, stand)
                if feet and abs(feet[0] - stand[0]) <= .045 and abs(feet[1] - stand[1]) <= .065:
                    baker = (counter[0], counter[1] - 65 / SIZE[1])
                    self._click("bakery-counter", baker, now, controls, region, 2.)
                elif feet:
                    self._walk("walk-jamie", frame, stand, now, controls, region, cancelled)
                else:
                    self._click("approach-jamie", stand, now, controls, region)
            return "Pekárna · přicházím k Jamiemu a čekám na nabídku"

        if self.refill == "receipt":
            if self.match(image, "jamie_reward") or self.match(image, "jamie_done"):
                self.refill = "dialog"
                self.jamie_close_sent = False
                self.reset_confirmation()
            else:
                return "Pekárna · čekám na potvrzení přijetí chleba"

        if self.refill == "dialog":
            done = self.match(image, "jamie_done")
            if done:
                counter = self.match(image, "bakery_counter") if not self.jamie_close_sent else None
                point = (counter[0], counter[1] - 65 / SIZE[1]) if counter else done
                if self._click("jamie-close", point, now, controls, region):
                    self.jamie_close_sent = True
                return "Jamie · zavírám dialog po vyzvednutí chleba"
            if not (inside and stable):
                return "Jamie · čekám na zavření dialogu"
            self.refill = "exit"
            self.reset_confirmation()

        if self.refill in ("exit", "leaving"):
            if inside:
                if not stable:
                    return "Pekárna · čekám na přístup k východu"
                rug = self.match(image, "bakery_exit")
                if rug and self._click("bakery-exit", rug, now, controls, region, 2.3):
                    self.refill = "leaving"
                return "Návnada vyzvednuta · odcházím z pekárny"
            if (self.refill == "leaving" and stable and not self.match(image, "map_title")
                    and (self.match(image, "bakery_door") or self._fishable(frame))):
                self.refill = "return"
                if self.destination.name == CURRENT:
                    self.destination = self.laketown
                self.visit = True
                self._reset_trip()
                self.arrived = self.destination.area.casefold() == "laketown"
            else:
                return "Pekárna · ověřuji odchod ven"

        if self.refill == "return":
            status = self._visit(image, frame, stable, now, controls, region, cancelled)
            if status is not None:
                return status
            self.refill = "equip"
            return "Jsem u vody · připravuji výběr chleba"

        if stable and hud and hud.bread and hud.bait:
            self.refill = ""
            return None
        self.wants_bread = bool(stable and hud)
        return "U vody · vybírám chléb v inventáři"

    @staticmethod
    def _fishable(frame: Image) -> bool:
        return casting_hover(frame) is not None or casting_target(frame) is not None

    def _walk(self, action: str, frame: Image, target: Point, now: float, controls: Controls,
              region: Region, cancelled: Callable[[], bool]) -> None:
        key = walking_direction(frame, target)
        if key is None:
            self.reset_confirmation()
            return
        old, self.previous = self.previous, (f"{action}-{key}", target)
        previous_at, self.previous_at = self.previous_at, now
        if (old is not None and old[0] == self.previous[0] and now - previous_at <= .6
                and max(abs(a-b) for a, b in zip(old[1], target)) <= .012
                and controls.walk(region.hwnd, key, cancelled=cancelled)):
            self.reset_confirmation()
            self.next_action = now + .42

    def _visit(self, image: Image, frame: Image, stable: bool, now: float, controls: Controls,
               region: Region, cancelled: Callable[[], bool]) -> str | None:
        map_visible = self.match(image, "map_title") is not None
        if map_visible or not stable or self._bakery_inside(image):
            return self._map(image, self.destination, now, controls, region, stable,
                             map_visible=map_visible)

        if self.map_pending:
            self.arrived = True
            self._reset_trip()
        bank = self.destination.bank.find(image) if self.destination.bank else None
        self.arrived |= bank is not None or bool(
            self.destination.name == LAKETOWN and self.match(image, "bakery_door")
        )
        if bank is not None:
            feet = avatar_feet(frame, bank)
            if feet and max(abs(a-b) for a,b in zip(feet, bank)) < .040 and self._fishable(frame):
                self.visit = False
                return None
            if feet and walking_direction(frame, bank):
                self._walk("walk-bank", frame, bank, now, controls, region, cancelled)
                return f"Jdu na místo · {self.destination.name}"

        if self.arrived:
            step = shore_approach(frame, max_distance_hats=35)
            if step is not None:
                self._walk("walk-shore", frame, step, now, controls, region, cancelled)
            elif self._fishable(frame):
                self.visit = False
                return None
            return f"{self.destination.area} · hledám přístup k viditelnému břehu"
        return self._map(image, self.destination, now, controls, region, stable)
