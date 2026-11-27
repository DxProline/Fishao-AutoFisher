from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from controls import Controls
from game_ui import SIZE, GameUI, Match, Point
from mission_text import classify_mission_text, log_cards
from runtime_state import EventFeed, MissionStatus, MissionTracker
from window import Image, Region


class Panel(Enum):
    MISSIONS = "mise"
    TOURNAMENT = "turnaj"


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    point: Point


class Activities:
    """Kontroluje známé herní panely mezi hody, včetně přijetí a dokončení misí."""

    def __init__(self, ui: GameUI, *, join_tournaments: bool = False, events: EventFeed | None = None) -> None:
        self.ui = ui
        self.join_tournaments = join_tournaments
        self.log = logging.getLogger("fishao")
        self.events = events or EventFeed()
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
        self.tracker = MissionTracker()
        self._active_signature: str | None = None
        self._page_keys: dict[bytes, str] = {}
        self._ocr_hints: dict[str, str] = {}
        self._ocr_action_pending = False
        self._unwatched_since: float | None = None
        self._close_failures = 0
        self._opening_panel: Panel | None = None

    def reset_observation(self) -> None:
        self.previous = None

    def _handle_ocr_hint(self, number: str, text: str) -> None:
        hint = classify_mission_text(text)
        if hint != "unknown":
            self._ocr_hints[number] = hint
            if hint in ("offer", "claim"):
                self._ocr_action_pending = True

    def tick(self, image: Image, ready: bool, now: float, controls: Controls, region: Region,
             *, caught: int = 0) -> str | None:
        find = self.ui.find
        missions = find(image, "missions_title", (532, 37, 738, 96), .88)
        popup = self.ui.mission_popup_action(image) if missions is None else None
        tournament = (
            find(image, "tournament_podium", (530, 297, 745, 357), .88) is not None
            and find(image, "tournament_start", (295, 526, 537, 580), .88) is not None
        )
        if tournament and not self.join_tournaments:
            self.reset_observation()
            self.events.emit("Otevřený turnaj čeká na ruční ovládání.", key="tournament_manual", cooldown_s=30.0, now=now)
            return "Otevřený turnaj čeká na ruční ovládání."
        watching = missions is not None or popup is not None or tournament or now < self.opening_until
        if not watching and not ready:
            if self.reading_missions:
                if self._unwatched_since is None:
                    self._unwatched_since = now
                elif now >= self.next_action and now - self._unwatched_since >= 1.5:
                    reason = "Panel misí zmizel před potvrzením akce."
                    had_pending = bool(self.tracker.pending_accept_sig or self.tracker.pending_claim_sig)
                    self.tracker.fail_pending(reason)
                    if had_pending:
                        self.events.emit(f"Akce s misí selhala · důvod: {reason}", key="mission_fail", level=logging.WARNING, now=now)
                    self.reading_missions = False
                    self._unwatched_since = None
            if self._opening_panel is not None:
                failed_label = "Panel misí" if self._opening_panel is Panel.MISSIONS else "Okno turnaje"
                self.events.emit(f"{failed_label} se neotevřel · pokračuji v lovu.", key="open_panel_fail", level=logging.WARNING, now=now)
                self._opening_panel = None
            self.reset_observation()
            return None
        self._unwatched_since = None
        if now < self.next_action:
            return "Agent ověřuje herní obrazovku." if watching else None

        action: Action | None = None
        if missions is not None:
            self.opening_until = 0.
            self._opening_panel = None
            if not self.reading_missions:
                self.reading_missions = True
                self.rare_fish = False
                self.mission_pages.clear()
                self._page_keys.clear()
                self.reported_missions = False
                self.rewind_missions = True
                self.pending_page = None
                self.mission_deadline = now + 35.
                self._close_failures = 0
                self.events.emit("Otevřel jsem mise.", key="missions_open", now=now)
            action = self._mission_action(image, missions, now)
        elif popup is not None:
            self.opening_until = 0.
            self._opening_panel = None
            kind, point = popup
            popup_sig = f"popup:{kind}:{round(point[0], 2)}:{round(point[1], 2)}"
            self.tracker.observe(
                popup_sig,
                goal="Nabídka nebo odměna mise",
                now=now,
                has_offer=(kind == "accept"),
                has_claim=(kind == "claim"),
            )
            if kind == "claim" and self.tracker.should_claim(popup_sig, now):
                action = Action("Potvrdit dokončení mise", point)
                self._active_signature = popup_sig
            elif kind == "accept" and self.tracker.should_accept(popup_sig, now):
                action = Action("Přijmout misi", point)
                self._active_signature = popup_sig
            else:
                close_pt = find(image, "activity_close", (952, 42, 1025, 97), .82)
                if close_pt:
                    action = Action("Zavřít dialog mise", close_pt.point(14, 14))
        elif tournament:
            self.opening_until = 0.
            if self._opening_panel is Panel.TOURNAMENT:
                self.events.emit("Otevřel jsem okno turnaje.", key="tournament_open", now=now)
            self._opening_panel = None
            action = self._signup(image) if now - self.last_signup >= 600. else None
            if action is None:
                action = self._close(image, "Turnaj zatím nelze přihlásit")
        elif not watching:
            if self._opening_panel is not None:
                failed_label = "Panel misí" if self._opening_panel is Panel.MISSIONS else "Okno turnaje"
                self.events.emit(f"{failed_label} se neotevřel · pokračuji v lovu.", key="open_panel_fail", level=logging.WARNING, now=now)
                self._opening_panel = None
            # Jestli čekala na potvrzení mise/odměny a panel se zavřel do připraveného stavu, ověř výsledek.
            if self.tracker.pending_accept_sig and self.tracker.confirm_accept():
                self.log.info("Mise potvrzena a přijata.")
                self.events.emit("Mise přijata.", key="mission_verified", now=now)
            if self.tracker.pending_claim_sig and self.tracker.verify_claim_completed(now):
                self.log.info("Dokončení mise potvrzeno.")
                self.events.emit("Mise dokončena · odměna potvrzena.", key="mission_verified", now=now)
            self.reading_missions = False
            recheck_ocr = self._ocr_action_pending
            catch_step = 4 if self.tracker.has_active_mission() else 8
            for panel, key, area, center in (
                (Panel.MISSIONS, "missions_icon", (0, 180, 84, 279), (22, 20)),
                (Panel.TOURNAMENT, "tournament_icon", (590, 604, 658, 666), (17, 15)),
            ):
                if panel is Panel.TOURNAMENT and not self.join_tournaments:
                    continue
                if panel is Panel.MISSIONS and recheck_ocr:
                    pass
                else:
                    if now < self.due[panel]:
                        continue
                    if panel is Panel.MISSIONS and caught < self.last_mission_catch + catch_step:
                        continue
                anchor = find(image, key, area, .87)
                if anchor is not None:
                    if panel is Panel.MISSIONS:
                        self._ocr_action_pending = False
                    action = Action(f"Otevřít {panel.value}", anchor.point(*center))
                    break

        previous, self.previous = self.previous, action
        if action is not None:
            if action == previous:
                if action.name == "__escape__":
                    if controls.escape(region.hwnd):
                        self._applied(action, now)
                        return "Zavřít mise (Escape)"
                elif controls.click(region, *action.point):
                    if action.name == "Otevřít mise":
                        self.last_mission_catch = caught
                    self._applied(action, now)
            return action.name
        return "Čekám na ověřený herní panel." if watching else None

    @staticmethod
    def _mission_arrow(image: Image, title: Match, *, next_page: bool) -> Point | None:
        x = title.x - (132 if next_page else 248)
        y = title.y + 433
        if x < 0 or y < 0 or y + 44 > image.shape[0] or x + 57 > image.shape[1]:
            return None
        hsv = cv2.cvtColor(image[y:y+44, x:x+57], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (85, 110, 90), (112, 255, 255))
        _, _, boxes, _ = cv2.connectedComponentsWithStats(mask)
        for dx, dy, width, height, area in boxes[1:]:
            if 25 <= width <= 49 and 15 <= height <= 35 and area >= 340:
                return (x + dx + width / 2) / SIZE[0], (y + dy + height / 2) / SIZE[1]
        return None

    def _mission_action(self, image: Image, title: Match, now: float) -> Action | None:
        x, y = title.x, title.y
        y0 = max(0, y + 167)
        y1 = min(image.shape[0], y + 420)
        x0 = max(0, x - 243)
        x1 = min(image.shape[1], x + 202)
        card = image[y0:y1, x0:x1]
        if card.size == 0:
            return self._close(image, "Nečitelná karta mise") or Action("__escape__", (0.5, 0.5))
        text = cv2.cvtColor(card[:min(108, card.shape[0])], cv2.COLOR_BGR2GRAY)
        signature = np.packbits(text < 130).tobytes()
        sig_hex = hashlib.sha1(signature).hexdigest()[:16]
        current = self.ui.number(image[max(0, y+442):y+469, max(0, x-196):max(1, x-170)], white=False)
        total = self.ui.number(image[max(0, y+442):y+469, max(0, x-158):max(1, x-131)], white=False)
        prog_cur = self.ui.number(image[max(0, y+350):min(image.shape[0], y+410), max(0, x-90):min(image.shape[1], x+20)], white=False)
        prog_tot = self.ui.number(image[max(0, y+350):min(image.shape[0], y+410), max(0, x+25):min(image.shape[1], x+120)], white=False)
        progress = (prog_cur, prog_tot) if prog_cur is not None and prog_tot and prog_tot > 0 else None

        card_btn = self.ui.mission_card_action(image, title)
        number = f"{current}/{total}" if current and total and current <= total else "stránka"
        if card_btn is not None and card_btn[0] == "action":
            hint = self._ocr_hints.get(number)
            inferred = (
                "claim"
                if hint == "claim" or (progress is not None and progress[0] >= progress[1])
                else "accept"
            )
            card_btn = (inferred, card_btn[1])
        has_offer = card_btn is not None and card_btn[0] == "accept"
        has_claim = card_btn is not None and card_btn[0] == "claim"
        if not has_offer and not has_claim and number in self._ocr_hints:
            hint = self._ocr_hints[number]
            green_btn = self.ui.find_green_button(image, (x - 240, y + 320, x + 200, y + 425))
            if green_btn is not None:
                if hint == "claim":
                    card_btn = ("claim", green_btn)
                    has_claim = True
                elif hint == "offer":
                    card_btn = ("accept", green_btn)
                    has_offer = True

        supported = True
        rare_now = False
        if signature not in self.mission_pages:
            if self.ui.find(image, "mission_rare_fish", (x-252, y+164, x+190, y+212), .85):
                goal = "Chytit čtyřhvězdičkovou rybu"
                self.rare_fish = True
                rare_now = True
            elif self.ui.find(image, "mission_daily_quests", (x-252, y+164, x+190, y+212), .85):
                goal = "Denní úkoly"
                supported = has_offer or has_claim
            else:
                goal = "Úkol podle stavu karty"
                supported = has_offer or has_claim
            self.mission_pages[signature] = (number, card.copy())
            self._page_keys[signature] = sig_hex
            self.log.info("Mise %s: %s.", number, goal)
        else:
            goal = "Aktivní mise"

        if current and total and current <= total:
            self.events.emit(f"Prohlížím stránku {current} z {total}.", key="mission_page", now=now)
        else:
            self.events.emit("Prohlížím kartu mise.", key="mission_page_card", cooldown_s=4.0, now=now)
        if rare_now:
            self.events.emit(
                "Nalezena aktivní mise: chytit čtyřhvězdičkovou rybu.",
                key="mission_rare",
                cooldown_s=60.0,
                now=now,
            )

        record = self.tracker.observe(
            sig_hex,
            goal=goal,
            now=now,
            has_offer=has_offer,
            has_claim=has_claim,
            progress=progress,
            supported=supported,
        )
        self._active_signature = sig_hex

        # Pokud se předchozí claim/accept na této kartě projevil zmizením tlačítka, potvrď výsledek.
        if self.tracker.pending_claim_sig == sig_hex and not has_claim and self.tracker.verify_claim_completed(now, sig_hex):
            self.log.info("Mise %s: dokončení ověřeno.", number)
            self.events.emit(f"Mise {number} dokončena · odměna potvrzena.", key="mission_verified", now=now)
        if self.tracker.pending_accept_sig == sig_hex and not has_offer and self.tracker.confirm_accept(sig_hex):
            self.log.info("Mise %s: přijetí ověřeno.", number)
            self.events.emit(f"Mise {number} přijata.", key="mission_verified", now=now)

        if has_claim and card_btn and self.tracker.should_claim(sig_hex, now):
            return Action("Potvrdit dokončení mise", card_btn[1])
        if has_offer and card_btn and self.tracker.should_accept(sig_hex, now):
            return Action("Přijmout misi", card_btn[1])

        if record.status is MissionStatus.UNSUPPORTED:
            self.log.debug("Mise %s nemá automatickou podporu; pokračuji v lovu.", number)

        if self.pending_page is None or self.pending_page[0] != signature:
            self.pending_page = (signature, 0)
        if now >= self.mission_deadline or self.pending_page[1] >= 3:
            close_action = self._close(image, "Nedokončený přehled misí, pokračuji v lovu")
            if close_action is not None:
                return close_action
            self._close_failures += 1
            if self._close_failures >= 2:
                return Action("__escape__", (0.5, 0.5))
            return None

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
        close_action = self._close(image, "Dokončeno čtení misí")
        if close_action is not None:
            return close_action
        self._close_failures += 1
        if self._close_failures >= 2:
            return Action("__escape__", (0.5, 0.5))
        return None

    def _close(self, image: Image, name: str) -> Action | None:
        anchor = self.ui.find(image, "activity_close", (952, 42, 1025, 97), .82)
        if anchor is None:
            anchor = self.ui.find(image, "result_x", (950, 40, 1030, 105), .82)
        return Action(name, anchor.point(14, 14)) if anchor else None

    def _signup(self, image: Image) -> Action | None:
        find = self.ui.find
        if find(image, "tournament_countdown", (540, 531, 762, 583), .82) is not None:
            return None
        button = find(image, "tournament_signup", (836, 549, 969, 603), .86)
        coin = find(image, "tournament_coin", (794, 554, 829, 592), .83)
        wallet = find(image, "wallet_coin", (242, 3, 300, 55), .87)
        if button is None or coin is None or wallet is None:
            return None
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
            self._opening_panel = Panel.MISSIONS
        elif action.name == "Otevřít turnaj":
            self.due[Panel.TOURNAMENT] = now + 300.
            self.opening_until = now + 3.
            self._opening_panel = Panel.TOURNAMENT
        elif action.name == "Přijmout misi" and self._active_signature:
            self.tracker.mark_accept_sent(self._active_signature, now)
            self.next_action = now + .9
            self.events.emit("Klikám na přijetí mise · čekám na ověření.", key="mission_accept_click", now=now)
        elif action.name == "Potvrdit dokončení mise" and self._active_signature:
            self.tracker.mark_claim_sent(self._active_signature, now)
            self.next_action = now + .9
            self.events.emit("Vybírám odměnu za misi · čekám na ověření.", key="mission_claim_click", now=now)
        elif action.name in ("Další mise", "Předchozí mise") and self.pending_page is not None:
            signature, attempts = self.pending_page
            self.pending_page = (signature, attempts + 1)
        elif action.name in ("Dokončeno čtení misí", "Nedokončený přehled misí, pokračuji v lovu", "__escape__"):
            if not self.reported_missions:
                self.reported_missions = True
                self.log.info("Prohlédnuté stránky misí: %s.", len(self.mission_pages))
                log_cards(self.log, tuple(self.mission_pages.values()), on_text=self._handle_ocr_hint)
                if action.name == "Dokončeno čtení misí":
                    self.events.emit(
                        f"Prohlédl jsem mise (stránek: {len(self.mission_pages)}) · vracím se k lovu.",
                        key="missions_done",
                        now=now,
                    )
                else:
                    self.events.emit(
                        f"Prohlídka misí ukončena předčasně ({len(self.mission_pages)} str.) · vracím se k lovu.",
                        key="missions_done",
                        level=logging.WARNING,
                        now=now,
                    )
                if self.rare_fish:
                    self.log.info("Aktivní mise: chytit čtyřhvězdičkovou rybu. Pokračuji v lovu.")
        elif action.name == "Přihlásit herní turnaj":
            self.last_signup = now
            self.events.emit("Přihlásil jsem se do turnaje.", key="tournament_signup", now=now)
        elif action.name == "Turnaj zatím nelze přihlásit":
            self.events.emit("Turnaj zatím nelze přihlásit · vracím se k lovu.", key="tournament_skip", now=now)
        if action.name != "__escape__":
            self.log.debug("Agent: %s.", action.name)
