from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from controls import Controls
from game_ui import SIZE, Interface, Match, Point
from mission_text import log_cards
from window import Image, Region


class Panel(Enum):
    MISSIONS = "mise"
    TOURNAMENT = "turnaj"


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    point: Point


class Activities:
    """Kontroluje známé herní panely mezi hody."""

    def __init__(self, ui: Interface, *, join_tournaments: bool = False) -> None:
        self.ui = ui
        self.join_tournaments = join_tournaments
        self.log = logging.getLogger("fishao")
        self.due = {Panel.MISSIONS: 0., Panel.TOURNAMENT: 30.}
        self.last_mission_catch = -8
        self.next_action = 0.
        self.opening_until = 0.
        self.previous: Action | None = None
        self.mission_pages: dict[bytes, tuple[str, Image]] = {}
        self.rewind_missions = True
        self.pending_page: tuple[bytes, int] | None = None
        self.mission_deadline = 0.
        self.reported_missions = False
        self.rare_fish = False
        self.reading_missions = False
        self.last_signup = -600.

    def reset_observation(self) -> None:
        self.previous = None

    def tick(self, image: Image, ready: bool, now: float, controls: Controls, region: Region,
             *, caught: int = 0) -> str | None:
        find = self.ui._find
        missions = find(image, "missions_title", (532, 37, 738, 96), .88)
        tournament = (
            find(image, "tournament_podium", (530, 297, 745, 357), .88) is not None
            and find(image, "tournament_start", (295, 526, 537, 580), .88) is not None
        )
        if tournament and not self.join_tournaments:
            self.reset_observation()
            return "Otevřený turnaj čeká na ruční ovládání."
        watching = missions is not None or tournament or now < self.opening_until
        if not watching and not ready:
            self.reading_missions = False
            self.reset_observation()
            return None
        if now < self.next_action:
            return "Agent ověřuje herní obrazovku." if watching else None

        action = None
        if missions is not None:
            self.opening_until = 0.
            if not self.reading_missions:
                self.reading_missions = True
                self.rare_fish = False
                self.mission_pages.clear()
                self.reported_missions = False
                self.rewind_missions = True
                self.pending_page = None
                self.mission_deadline = now + 35.
            action = self._mission_action(image, missions, now)
        elif tournament:
            self.opening_until = 0.
            action = self._signup(image) if now - self.last_signup >= 600. else None
            if action is None:
                action = self._close(image, "Turnaj zatím nelze přihlásit")
        elif not watching:
            self.reading_missions = False
            for panel, key, area, center in (
                (Panel.MISSIONS, "missions_icon", (0, 180, 84, 279), (22, 20)),
                (Panel.TOURNAMENT, "tournament_icon", (590, 604, 658, 666), (17, 15)),
            ):
                if panel is Panel.TOURNAMENT and not self.join_tournaments:
                    continue
                if now < self.due[panel]:
                    continue
                if panel is Panel.MISSIONS and caught < self.last_mission_catch + 8:
                    continue
                anchor = find(image, key, area, .87)
                if anchor is not None:
                    action = Action(f"Otevřít {panel.value}", anchor.point(*center))
                    break

        previous, self.previous = self.previous, action
        if action is not None:
            if action == previous and controls.click(region, *action.point):
                if action.name == "Otevřít mise":
                    self.last_mission_catch = caught
                self._applied(action, now)
            return action.name
        return "Čekám na ověřený herní panel." if watching else None

    @staticmethod
    def _mission_arrow(image: Image, title: Match, *, next_page: bool) -> Point | None:
        # Aktivní šipka je modrá, zakázaná má stejné obrysy, ale je šedá.
        x = title.x - (132 if next_page else 248)
        y = title.y + 433
        hsv = cv2.cvtColor(image[y:y+44, x:x+57], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (85, 110, 90), (112, 255, 255))
        _, _, boxes, _ = cv2.connectedComponentsWithStats(mask)
        for dx, dy, width, height, area in boxes[1:]:
            if 25 <= width <= 49 and 15 <= height <= 35 and area >= 340:
                return (x + dx + width / 2) / SIZE[0], (y + dy + height / 2) / SIZE[1]
        return None

    def _mission_action(self, image: Image, title: Match, now: float) -> Action | None:
        x, y = title.x, title.y
        card = image[y+167:y+420, x-243:x+202]
        text = cv2.cvtColor(card[:108], cv2.COLOR_BGR2GRAY)
        signature = np.packbits(text < 130).tobytes()
        current = self.ui.number(image[y+442:y+469, x-192:x-174], white=False)
        total = self.ui.number(image[y+442:y+469, x-154:x-135], white=False)
        if signature not in self.mission_pages:
            if self.ui._find(image, "mission_rare_fish", (x-252, y+164, x+190, y+212), .85):
                goal = "Chytit čtyřhvězdičkovou rybu"
                self.rare_fish = True
            elif self.ui._find(image, "mission_daily_quests", (x-252, y+164, x+190, y+212), .85):
                goal = "Denní úkoly · samostatný postup zatím chybí"
            else:
                goal = "Úkol bez rozpoznaného postupu · pokračuji v lovu"
            number = f"{current}/{total}" if current and total and current <= total else "stránka"
            self.mission_pages[signature] = (number, card.copy())
            self.log.info("Mise %s: %s.", number, goal)

        if self.pending_page is None or self.pending_page[0] != signature:
            self.pending_page = (signature, 0)
        if now >= self.mission_deadline or self.pending_page[1] >= 3:
            return self._close(image, "Nedokončený přehled misí, pokračuji v lovu")

        if self.rewind_missions:
            back = self._mission_arrow(image, title, next_page=False)
            if back:
                return Action("Předchozí mise", back)
            if current and current > 1:
                return None
            self.rewind_missions = False

        forward = self._mission_arrow(image, title, next_page=True)
        if forward:
            return Action("Další mise", forward)
        if (current and total and current < total
                or total and len(self.mission_pages) < total):
            return None
        return self._close(image, "Dokončeno čtení misí")

    def _close(self, image: Image, name: str) -> Action | None:
        anchor = self.ui._find(image, "activity_close", (952, 42, 1025, 97), .82)
        return Action(name, anchor.point(14, 14)) if anchor else None

    def _signup(self, image: Image) -> Action | None:
        find = self.ui._find
        if find(image, "tournament_countdown", (540, 531, 762, 583), .82) is not None:
            return None
        button = find(image, "tournament_signup", (836, 549, 969, 603), .86)
        coin = find(image, "tournament_coin", (794, 554, 829, 592), .83)
        wallet = find(image, "wallet_coin", (242, 3, 300, 55), .87)
        if button is None or coin is None or wallet is None:
            return None
        # Aktivní přihláška musí být barevná a mít čitelné vstupné v herních mincích.
        cell = cv2.cvtColor(image[553:596, 837:963], cv2.COLOR_BGR2HSV)
        if float(np.mean(cell[:, :, 1] >= 132)) < .45:
            return None
        fee = self.ui.number(image[559:587, 770:799], white=True)
        amount = self.ui.number(cv2.convertScaleAbs(image[18:39, 289:350], alpha=1.45),
                                white=False, max_digits=6)
        if fee is None or amount is None or fee <= 0 or amount < fee:
            return None
        return Action("Přihlásit herní turnaj", button.point(47, 15))

    def _applied(self, action: Action, now: float) -> None:
        self.previous = None
        self.next_action = now + .55
        if action.name == "Otevřít mise":
            self.due[Panel.MISSIONS] = now + 120.
            self.opening_until = now + 3.
        elif action.name == "Otevřít turnaj":
            self.due[Panel.TOURNAMENT] = now + 300.
            self.opening_until = now + 3.
        elif action.name in ("Další mise", "Předchozí mise") and self.pending_page is not None:
            signature, attempts = self.pending_page
            self.pending_page = (signature, attempts + 1)
        elif action.name in ("Dokončeno čtení misí", "Nedokončený přehled misí, pokračuji v lovu"):
            if not self.reported_missions:
                self.reported_missions = True
                self.log.info("Prohlédnuté stránky misí: %s.", len(self.mission_pages))
                log_cards(self.log, tuple(self.mission_pages.values()))
                if self.rare_fish:
                    self.log.info("Aktivní mise: chytit čtyřhvězdičkovou rybu. Pokračuji v lovu.")
        elif action.name == "Přihlásit herní turnaj":
            self.last_signup = now
        self.log.info("Agent: %s.", action.name)
