from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from activities import Action
from controls import Controls
from game_ui import Interface, SIZE
from window import Image, Region


@dataclass(frozen=True, slots=True)
class Supply:
    label: str
    cost: int
    action: Action


class Progression:
    """Volí ověřené akce, které pomohou pokračovat v lovu."""

    def __init__(self, ui: Interface) -> None:
        self.ui = ui
        self.log = logging.getLogger("fishao")
        self.goal: str | None = None
        self.previous: Action | None = None
        self.until = 0.
        self.next_click = 0.
        self.recheck_shop = 0.
        self.farm_tried = False
        self.line_tried = False
        self.purchased = False
        self.tab_requested = False
        self.awaiting_spawn = False

    def reset_observation(self) -> None:
        self.previous = None

    def tick(self, image: Image, *, ready: bool, now: float, controls: Controls, region: Region,
             missing: str | None, casts: int, caught: int, escaped: int, rare_mission: bool,
             allow_farm: bool = True) -> str | None:
        find = self.ui._find
        if self.goal is None:
            if not ready:
                return None
            if missing and now >= self.recheck_shop:
                self.goal = missing
                self.recheck_shop = now + 180.
            elif not self.line_tried and escaped >= 8 and escaped > caught:
                self.goal = "line"
                self.line_tried = True
            elif allow_farm and not self.farm_tried and (casts >= 18 or rare_mission and casts >= 8) and caught * 3 < casts:
                self.goal = "farm"
                self.farm_tried = True
            else:
                return None
            self.until = now + 16.
            self.purchased = self.tab_requested = self.awaiting_spawn = False

        if now < self.next_click:
            return "Agent čeká na změnu obrazu."
        if now >= self.until:
            self.previous = None
            self.goal = None
            return None

        action = None
        if self.goal == "farm":
            title = find(image, "world_title", (495, 0, 771, 51), .90)
            if title:
                spot = find(image, "trout_destination", (506, 110, 665, 181), .89)
                if spot and not self.awaiting_spawn:
                    action = Action("Vstoupit na Trout Farm", spot.point(59, 18))
            elif self.awaiting_spawn:
                bank = find(image, "trout_spawn", (484, 214, 582, 285), .90) if ready else None
                if bank:
                    action = Action("Dojít ke břehu farmy", bank.point(116, 17))
            elif ready:
                action = self._icon(image, "world_icon", (359, 601, 428, 675), (17, 18), "Otevřít mapu")
        elif self.goal == "line":
            if find(image, "line_title", (474, 35, 686, 92), .88):
                price = find(image, "line_upgrade", (386, 530, 454, 597), .86)
                wallet = find(image, "wallet_coin", (242, 3, 300, 55), .87)
                funds = self.ui.number(cv2.convertScaleAbs(image[18:39, 289:350], alpha=1.45),
                                       white=False, max_digits=6) if wallet else None
                cost = self.ui.number(image[556:582, 452:504], white=True) if price else None
                if price and self._affordable(funds, cost, reserve=5) and not self.purchased:
                    action = Action("Vylepšit vlasec", price.point(69, 18))
                else:
                    action = self._close(image)
            elif ready and not self.purchased:
                icon = find(image, "line_icon", (1154, 556, 1248, 603), .85)
                if icon:
                    action = Action("Prověřit vlasec", icon.point(34, 57))
        elif find(image, "supply_title", (538, 51, 689, 117), .90):
            if self.goal == "bait" and not self.tab_requested and not find(image, "worms_bait", (426, 155, 536, 207), .89):
                action = self._icon(image, "bait_tab", (386, 106, 474, 145), (23, 13), "Otevřít návnady")
            elif self.goal == "rod" and not self.tab_requested and find(image, "worms_bait", (426, 155, 536, 207), .89):
                action = self._icon(image, "rod_tab", (298, 106, 382, 145), (24, 13), "Otevřít pruty")
            else:
                offer = self._supply(image, self.goal)
                action = offer.action if offer and not self.purchased else self._close(image)
        elif ready and not self.purchased and not self.tab_requested:
            action = self._icon(image, "supply_icon", (412, 599, 476, 668), (16, 21), "Otevřít výbavu")

        previous, self.previous = self.previous, action
        if action and action == previous and controls.click(region, *action.point):
            self.previous = None
            self.next_click = now + .75
            self.log.info("Agent: %s.", action.name)
            if action.name.startswith(("Koupit", "Vylepšit")):
                self.purchased = True
            elif action.name in ("Otevřít návnady", "Otevřít pruty"):
                self.tab_requested = True
            elif action.name == "Vstoupit na Trout Farm":
                self.awaiting_spawn = True
            elif action.name in ("Zavřít nabídku", "Dojít ke břehu farmy"):
                self.goal = None
                if action.name == "Dojít ke břehu farmy":
                    self.next_click = now + 2.
        return action.name if action else "Agent ověřuje další krok."

    @staticmethod
    def _affordable(funds: int | None, cost: int | None, *, reserve: int) -> bool:
        return funds is not None and cost is not None and 0 < cost <= funds - reserve

    def _supply(self, image: Image, missing: str) -> Supply | None:
        find = self.ui._find
        wallet = find(image, "supply_cash", (862, 95, 926, 139), .90)
        if wallet is None:
            return None
        funds = self.ui.number(image[109:135, 910:976], white=False, max_digits=6)
        offers = (
            ("big_rod", "velký prut", (431, 297, 533, 345), "supply_price", (710, 365, 781, 423), 500),
            ("simple_rod", "jednoduchý prut", (430, 158, 573, 206), "supply_price", (710, 229, 781, 285), 99),
        ) if missing == "rod" else (
            ("worms_bait", "červy", (426, 155, 536, 207), "bait_price", (792, 230, 858, 287), 150),
        )
        for item, label, area, price_key, price_area, expected in offers:
            if not find(image, item, area, .89):
                continue
            price = find(image, price_key, price_area, .86)
            if not price:
                continue
            sample = image[price.y-3:price.y+36, price.x+42:price.x+96]
            cost = self.ui.number(sample, white=True)
            if cost is None:
                larger = cv2.resize(sample, None, fx=1.25, fy=1.25, interpolation=cv2.INTER_LINEAR)
                cost = self.ui.number(larger, white=True)
            if cost != expected or not self._affordable(funds, cost, reserve=30):
                continue
            assert cost is not None
            return Supply(label, cost, Action(f"Koupit {label} za {cost} bankovek", price.point(67, 15)))
        return None

    def _icon(self, image: Image, key: str, area: tuple[int, int, int, int], offset: tuple[int, int],
              label: str) -> Action | None:
        icon = self.ui._find(image, key, area, .87)
        return Action(label, icon.point(*offset)) if icon else None

    def _close(self, image: Image) -> Action | None:
        return (self._icon(image, "shop_close", (973, 51, 1037, 107), (14, 14), "Zavřít nabídku")
                or self._icon(image, "activity_close", (952, 41, 1025, 97), (14, 14), "Zavřít nabídku"))
