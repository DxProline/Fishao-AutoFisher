from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from controls import Controls
from game_ui import Interface, Point
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

    def __init__(self, ui: Interface) -> None:
        self.ui = ui
        self.log = logging.getLogger("fishao")
        self.due = {Panel.MISSIONS: 0., Panel.TOURNAMENT: 30.}
        self.last_mission_catch = -8
        self.next_action = 0.
        self.opening_until = 0.
        self.previous: Action | None = None
        self.pages = 0
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
        watching = missions is not None or tournament or now < self.opening_until
        if not watching and not ready:
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
                self.pages = 0
            self.rare_fish |= find(image, "mission_rare_fish", (305, 214, 540, 263), .88) is not None
            next_button = find(image, "missions_next", (424, 481, 478, 529), .84)
            if next_button is not None and self.pages < 12:
                # Šipka poslední strany zešedne, samotný tvar nestačí.
                button = image[next_button.y:next_button.y+23, next_button.x:next_button.x+33]
                hsv = cv2.cvtColor(button, cv2.COLOR_BGR2HSV)
                if float(np.mean(hsv[:, :, 1] >= 150)) >= .35:
                    action = Action("Další mise", next_button.point(16, 11))
            if action is None:
                action = self._close(image, "Dokončeno čtení misí")
        elif tournament:
            self.opening_until = 0.
            action = self._signup(image) if now - self.last_signup >= 600. else None
            if action is None:
                action = self._close(image, "Turnaj zatím nelze přihlásit")
        elif not watching:
            for panel, key, area, center in (
                (Panel.MISSIONS, "missions_icon", (0, 180, 84, 279), (22, 20)),
                (Panel.TOURNAMENT, "tournament_icon", (590, 604, 658, 666), (17, 15)),
            ):
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
        elif action.name == "Další mise":
            self.pages += 1
        elif action.name == "Dokončeno čtení misí":
            self.reading_missions = False
            if self.rare_fish:
                self.log.info("Aktivní mise: chytit čtyřhvězdičkovou rybu. Pokračuji v lovu.")
        elif action.name == "Přihlásit herní turnaj":
            self.last_signup = now
        self.log.info("Agent: %s.", action.name)
