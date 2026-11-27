from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter

from activities import Activities
from assist import TARGET_HZ, TRACK_MISSES, Assist, PreviewClock
from controls import BackgroundControls, Controls
from destinations import Navigator, TravelSettings
from game_ui import EnergyShop, GameUI, Hud, Interface, Inventory, Point
from minigame import BalanceDetector, BalancePolicy, PixelBox
from progression import Progression
from recorder import NS
from runtime_state import BotState, LevelDialogController
from spots import CastTarget, casting_hover, casting_target, shore_approach, timed_farm, walking_direction
from window import (
    FrameUnavailable,
    Image,
    Region,
    WindowCapture,
    WorkstationLocked,
    is_workstation_locked,
    set_keep_awake,
)


class Phase(str, Enum):
    READY = "Hledám místo u vody"
    CAST = "Ověřuji nahození"
    WAIT = "Čekám na záběr"
    BALANCE = "Vyvažuji rybu"
    RESULT = "Čekám na výsledek"
    CLOSE = "Zavírám výsledek"
    ENERGY = "Čekám na dobití energie"
    REFILL = "Ověřuji doplnění energie na farmě"
    GEAR = "Vybírám vybavení"


CAST_ACK_S = 1.2
CAST_TIMEOUT_S = 8.0
WAIT_BITE_TIMEOUT_S = 360.0
WAIT_UNREADABLE_TIMEOUT_S = 18.0
RESULT_TIMEOUT_S = 14.0
CLOSE_TIMEOUT_S = 12.0
GEAR_TIMEOUT_S = 20.0
REFILL_TIMEOUT_S = 18.0
UNKNOWN_SCREEN_TIMEOUT_S = 12.0


@dataclass(slots=True)
class Cycle:
    phase: Phase = Phase.READY
    since: float = 0.
    retries: int = 0
    casts: int = 0
    caught: int = 0
    escaped: int = 0
    position: Point | None = None
    candidate: CastTarget | None = None
    rejected: list[Point] = field(default_factory=list)
    next_scan: float = 0.
    next_hover: float = 0.
    hover_probe: int = 0
    shore_step: Point | None = None
    shore_moves: int = 0
    next_step: float = 0.
    score: dict[Point, int] = field(default_factory=dict)
    gear: str | None = None
    gear_step: str = ""
    next_action: float = 0.
    attempted: set[Point] = field(default_factory=set)
    pages: set[tuple[int, int]] = field(default_factory=set)
    select_attempts: int = 0
    original_energy: int | None = None
    attempt_pending: bool = False
    farm_checked: bool = False
    farm_missing_frames: int = 0
    quote: EnergyShop | None = None
    refill_sent: bool = False
    basic_rod: bool | None = None
    bread_only: bool = False
    level_button: Point | None = None
    level_click_after: float = 0.
    level_clicks: int = 0
    level_ctrl: LevelDialogController = field(default_factory=LevelDialogController)
    modal_kind: str = ""
    unknown_since: float | None = None
    unreadable_since: float | None = None
    gear_tab_clicks: int = 0
    learned_spot_reported: tuple[Point, int] | None = None

    def reset_range(self, now: float) -> None:
        self.candidate = self.position = self.shore_step = None
        self.score.clear()
        self.rejected.clear()
        self.next_scan = self.next_hover = self.next_step = now
        self.shore_moves = self.hover_probe = 0

    def confirm_cast(self) -> None:
        if self.phase is Phase.CAST:
            self.attempt_pending = True
            self.casts += 1
            self.rejected.clear()
            self.hover_probe = 0
            self.shore_moves = 0
            self.shore_step = None

    def advance(self, phase: Phase, now: float, *, progress: bool = True) -> None:
        if self.phase != phase:
            self.phase, self.since = phase, now
            self.unreadable_since = None
            if progress:
                self.retries = 0

    def finish_attempt(self, now: float, result: str | None = None) -> None:
        if result is not None and self.attempt_pending:
            if result == "caught":
                self.caught += 1
            else:
                self.escaped += 1
            if self.position is not None:
                self.score[self.position] = self.score.get(self.position, 0) + (2 if result == "caught" else -1)
                if len(self.score) > 48:
                    del self.score[min(self.score, key=self.score.__getitem__)]
        self.attempt_pending = False
        self.candidate = None
        self.next_scan = now

    def preferred(self) -> Point | None:
        return max((p for p in self.score if self.score[p] > 0), key=self.score.__getitem__, default=None)


class Auto(Assist):
    """Opakování ověřených akcí nad obrazem hry, v samostatném vlákně."""

    def __init__(self, max_casts: int | None = None, *, xp_farm: bool = False, agent: bool = False,
                 background: bool = False, join_tournaments: bool = False,
                 travel: TravelSettings | None = None, continuous: bool = True,
                 prevent_sleep: bool = True) -> None:
        super().__init__(background=background, prevent_sleep=prevent_sleep)
        self.xp_farm = xp_farm
        self.name = "Agent Fishao" if agent else "XP Farm" if xp_farm else "AutoFish"
        self.agent = agent
        self.join_tournaments = join_tournaments
        self.max_casts = max_casts
        self.travel = travel
        self.continuous = continuous

    def _limit_reached(self, cycle: Cycle) -> bool:
        return self.max_casts is not None and cycle.casts >= self.max_casts

    @staticmethod
    def _bot_state_for_phase(phase: Phase) -> BotState:
        return {
            Phase.READY: BotState.READY,
            Phase.CAST: BotState.CASTING,
            Phase.WAIT: BotState.WAITING_BITE,
            Phase.BALANCE: BotState.MINIGAME,
            Phase.RESULT: BotState.HANDLING_RESULT,
            Phase.CLOSE: BotState.HANDLING_RESULT,
            Phase.ENERGY: BotState.ENERGY_WAIT,
            Phase.REFILL: BotState.REFILL,
            Phase.GEAR: BotState.GEAR_SELECT,
        }[phase]

    def _afk_cooldown_recovery(
        self,
        cycle: Cycle,
        controls: Controls,
        region: Region,
        navigator: Navigator | None,
        activities: Activities | None,
        progression: Progression | None,
        now: float,
        reason: str,
    ) -> None:
        """Bezpečná pauza a návrat k lovu místo trvalého zastavení AFK farmy."""
        controls.release()
        controls.escape(region.hwnd)
        if navigator:
            navigator.reset_confirmation()
        if activities:
            activities.reset_observation()
        if progression:
            progression.reset_observation()
            progression.goal = None
        cycle.finish_attempt(now)
        cycle.reset_range(now)
        cycle.level_ctrl.reset()
        cycle.advance(Phase.READY, now, progress=False)
        self._events.emit(
            f"Obnovuji lov po chybě · důvod: {reason} (krátká pauza 5 s).",
            key="afk_recover",
            level=logging.WARNING,
            now=now,
        )
        self._update(
            text=f"AFK zotavení ({reason}) — pokračuji v lovu za chvíli…",
            tracking_fps=None,
            bot_state=BotState.RECOVERING.value,
        )
        self._stop_requested.wait(5.0)
        self.machine.reset_after_cooldown(now, reason=reason)

    @staticmethod
    def _activity_reason(activity: str) -> str:
        low = activity.casefold()
        if "mise" in low or "úkol" in low or "turnaj" in low:
            return "mise_turnaje"
        if "pekárn" in low or "jamie" in low or "chléb" in low:
            return "pekarna"
        if "map" in low or "oblast" in low or "přesun" in low or "jdu na místo" in low:
            return "cestovani"
        if "výbav" in low or "návnad" in low or "prut" in low or "vlasec" in low or "farm" in low:
            return "vybava_postup"
        if "k vodě" in low:
            return "prichod_k_vode"
        return "aktivita_agenta"

    def _loop(self, capture: WindowCapture, controls: Controls) -> str:
        ui, detector, policy = Interface(), BalanceDetector(), BalancePolicy()
        activities = Activities(ui, join_tournaments=self.join_tournaments, events=self._events) if self.agent else None
        navigator = Navigator(self.travel, events=self._events) if self.travel else None
        allow_auto_farm = self.travel is None
        progression = Progression(ui, events=self._events) if self.agent else None
        cycle = Cycle()
        crop: PixelBox | None = None
        expected: tuple[int, int] | None = None
        agreement: tuple[int | None, int | None, int | None, bool] | None = None
        repetitions = ready_frames = 0
        last_seen = 0.
        first_arrow = False
        observed_result: str | None = None
        result_seen = 0
        preview_clock = PreviewClock()
        active = 0.
        last_tick: float | None = None
        crop_misses = 0
        wait_reason: str | None = None
        primary_controls = controls
        locked_bg_controls: BackgroundControls | None = None
        using_locked_bg = False
        awake_active = False
        next_awake_refresh = 0.0
        if self.agent:
            self._events.emit(
                "Spouštím agenta · zkušenosti z minulých spuštění se neukládají (učení jen v rámci tohoto běhu).",
                key="session_start",
            )
        else:
            self._events.emit(f"Spouštím {self.name}.", key="session_start")
        while not self.stopping:
            tick = perf_counter()
            if self.paused:
                if awake_active:
                    set_keep_awake(False)
                    awake_active = False
                if navigator:
                    navigator.reset_confirmation()
                if activities:
                    activities.reset_observation()
                if progression:
                    progression.reset_observation()
                primary_controls.release()
                if locked_bg_controls is not None:
                    locked_bg_controls.release()
                using_locked_bg = False
                controls = primary_controls
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
                cycle.shore_step = cycle.level_button = None
                cycle.level_ctrl.reset()
                agreement, repetitions, ready_frames = None, 0, 0
                last_tick = None
                self.machine.transition(BotState.PAUSED_MANUAL, tick, action="pause", reason="manual_pause")
                self._update(text="Pozastaveno. Pokračuj přes F8.", tracking_fps=None, bot_state=BotState.PAUSED_MANUAL.value)
                self._stop_requested.wait(.05)
                continue
            if self.prevent_sleep and (not awake_active or tick >= next_awake_refresh):
                set_keep_awake(True)
                awake_active = True
                next_awake_refresh = tick + 30.0
            try:
                frame, region = capture.grab(crop, expected=expected if crop else None)
                if using_locked_bg:
                    if locked_bg_controls is not None:
                        locked_bg_controls.release()
                    using_locked_bg = False
                    controls = primary_controls
                    self._events.emit("Plocha odemčena · vracím se k běžnému ovládání.", key="grab_wait", now=tick)
                elif self.background and is_workstation_locked():
                    self._events.emit(
                        "Počítač je zamčen · lov pokračuje na pozadí v režimu překrytého okna.",
                        key="locked_bg",
                        cooldown_s=60.0,
                        now=tick,
                    )
            except WorkstationLocked as exc:
                recovered_frame: tuple[Image, Region] | None = None
                if self.continuous and hasattr(capture, "grab_locked_background"):
                    try:
                        recovered_frame = capture.grab_locked_background(crop, expected=expected if crop else None)
                    except (WorkstationLocked, FrameUnavailable) as bg_exc:
                        if isinstance(bg_exc, WorkstationLocked):
                            exc = bg_exc
                if recovered_frame is not None:
                    frame, region = recovered_frame
                    if not using_locked_bg:
                        primary_controls.release()
                        if isinstance(primary_controls, BackgroundControls):
                            controls = primary_controls
                        else:
                            if locked_bg_controls is None:
                                locked_bg_controls = BackgroundControls(getattr(capture, "title", "FISHAO - Reborn"))
                                self._extra_controls = locked_bg_controls
                            controls = locked_bg_controls
                        using_locked_bg = True
                        wait_reason = None
                        self._events.emit(
                            "Počítač je zamčen · pokračuji v lovu na pozadí (okno hry poskytuje obraz).",
                            key="locked_bg",
                            cooldown_s=60.0,
                            now=tick,
                        )
                else:
                    if navigator:
                        navigator.reset_confirmation()
                    if activities:
                        activities.reset_observation()
                    if progression:
                        progression.reset_observation()
                    primary_controls.release()
                    if locked_bg_controls is not None:
                        locked_bg_controls.release()
                    using_locked_bg = False
                    controls = primary_controls
                    policy.choose(None)
                    self._tracking_times.clear()
                    cycle.candidate = None
                    cycle.shore_step = cycle.level_button = None
                    cycle.level_ctrl.reset()
                    agreement, repetitions, ready_frames = None, 0, 0
                    crop = None
                    crop_misses = 0
                    last_tick = None
                    wait_reason = "locked"
                    self.machine.transition(BotState.PAUSED_LOCKED, tick, action="grab", result="locked", reason=str(exc))
                    self._events.emit(
                        "Počítač je uzamčen a Windows blokuje obraz hry · čekám v pauze na odemčení (lov se nevypíná).",
                        key="grab_wait",
                        level=logging.WARNING,
                        now=tick,
                    )
                    self._update(text=str(exc), tracking_fps=None, bot_state=BotState.PAUSED_LOCKED.value)
                    self._stop_requested.wait(.25)
                    continue
            except FrameUnavailable as exc:
                if navigator:
                    navigator.reset_confirmation()
                if activities:
                    activities.reset_observation()
                if progression:
                    progression.reset_observation()
                primary_controls.release()
                if locked_bg_controls is not None:
                    locked_bg_controls.release()
                using_locked_bg = False
                controls = primary_controls
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
                cycle.shore_step = cycle.level_button = None
                cycle.level_ctrl.reset()
                agreement, repetitions, ready_frames = None, 0, 0
                crop = None
                crop_misses = 0
                last_tick = None
                wait_reason = "unavailable"
                self.machine.transition(BotState.RECOVERING, tick, action="grab", result="unavailable", reason=str(exc))
                self._events.emit(f"Čekám na okno hry · {exc}", key="grab_wait", cooldown_s=15.0, now=tick)
                self._update(text=str(exc), tracking_fps=None, bot_state=BotState.RECOVERING.value)
                self._stop_requested.wait(.05)
                continue
            if wait_reason == "locked":
                self._events.emit("Plocha odemčena · obnovuji lov.", key="grab_wait", now=tick)
            elif wait_reason == "unavailable":
                self._events.emit("Okno hry je opět dostupné · pokračuji v lovu.", key="grab_wait", now=tick)
            wait_reason = None
            if last_tick is not None:
                active += tick - last_tick
            last_tick = tick
            selected_crop = crop
            captured_at = perf_counter()
            balance = (detector.analyze_crop(frame, crop[0], crop[1]) if crop else detector.analyze(frame)
                       ) if cycle.phase not in (Phase.CLOSE, Phase.GEAR, Phase.ENERGY, Phase.REFILL) else None
            direction = policy.choose(balance, captured_at)
            if self.stopping or self.paused:
                continue
            controls.direction(region.hwnd, direction)
            tracking_fps = self._tracking_rate(balance is not None, captured_at)
            image = ui.normalize(frame) if not balance and selected_crop is None else None
            if balance:
                if activities:
                    activities.reset_observation()
                ready_frames, agreement, repetitions = 0, None, 0
                cycle.candidate = None
                cycle.unknown_since = None
                crop_misses = 0
                cycle.confirm_cast()
                if cycle.phase is not Phase.BALANCE:
                    self._log.info("Záběr rozpoznán. Řídím šipky podle ukazatele.")
                    self._events.emit("Záběr rozpoznán · vyvažuji rybu.", key="bite_phase", now=active)
                    first_arrow = False
                if not first_arrow and direction is not None and controls.held == direction:
                    self._log.debug("První šipka po záběru: %s (%s).",
                                    "doleva" if direction == "left" else "doprava",
                                    "zpráva do okna" if isinstance(controls, BackgroundControls)
                                    and controls.uses_window_messages else "vstup Windows do aktivní hry")
                    first_arrow = True
                cycle.advance(Phase.BALANCE, active)
                self.machine.record_progress()
                self.machine.transition(BotState.MINIGAME, active, action="balance")
                last_seen = active
                expected = (region.width, region.height)
                x, y, w, h = balance.bar
                left, top = max(0, x - w // 6), max(0, y - h)
                right, bottom = min(region.width, x + w + w // 6), min(region.height, y + h * 2)
                crop = (left, top, right - left, bottom - top)
                message = cycle.phase.value + (" · ←" if direction == "left" else " · →" if direction == "right" else " · střed")
            elif selected_crop:
                crop_misses += 1
                if crop_misses >= TRACK_MISSES:
                    crop = None
                    crop_misses = 0
                message = "Hledám ukazatel na liště minihry."
            elif image is not None and self._modal_dialog(cycle, image, ui, active, controls, region, navigator):
                agreement, repetitions, ready_frames = None, 0, 0
                cycle.candidate = cycle.shore_step = None
                cycle.unknown_since = None
                self.machine.transition(BotState.HANDLING_LEVEL, active, action="modal_ok_collect")
                message = "Potvrzuji dialog / odměnu (OK / Collect)"
            else:
                assert image is not None
                result = ui.result(image) if cycle.phase is not Phase.GEAR and not (navigator and navigator.refill) else None
                if result != observed_result:
                    observed_result, result_seen = result, 0
                result_seen += 1
                if cycle.phase is Phase.BALANCE and active - last_seen > .10:
                    cycle.advance(Phase.RESULT, active)
                if result and result_seen >= 2 and cycle.phase not in (Phase.CLOSE, Phase.GEAR):
                    cycle.confirm_cast()
                    cycle.finish_attempt(active, result)
                    cycle.advance(Phase.CLOSE, active)
                    cycle.next_action = active
                    self.machine.record_progress()
                    self.machine.transition(BotState.HANDLING_RESULT, active, action="result_detected", result=result)
                    self._log.info("Výsledek: %s. Úlovky: %s.", result, cycle.caught)
                    if result == "caught":
                        self._events.emit(f"Úlovek potvrzen (chyceno: {cycle.caught}).", key="fish_result", now=active)
                    else:
                        self._events.emit(f"Ryba utekla (uteklo: {cycle.escaped}).", key="fish_result", now=active)
                inventory = ui.inventory(image)
                hud = ui.hud(image)
                timed = (not inventory and not result and cycle.phase in (Phase.READY, Phase.ENERGY, Phase.WAIT)
                         and ui.timed_fishing(image))
                farm, farm_time = timed_farm(image) if self.agent and cycle.phase in (Phase.READY, Phase.ENERGY) else (False, False)
                farm = farm or timed
                if cycle.phase in (Phase.READY, Phase.ENERGY) and hud and hud.ready is True and not inventory and not result:
                    cycle.farm_missing_frames = 0 if farm else cycle.farm_missing_frames + 1
                    if cycle.farm_missing_frames >= 4:
                        cycle.farm_checked = False
                ready = hud is not None and hud.ready is True and inventory is None and result is None
                busy = hud is not None and hud.ready is False and inventory is None and result is None
                ready_frames = ready_frames + 1 if ready else 0
                key = (hud.energy, hud.rod, hud.bait, hud.basic_rod) if ready and hud else None
                repetitions = repetitions + 1 if key is not None and key == agreement else 1
                agreement = key
                stable = key is not None and repetitions >= 2
                if key is None or (cycle.candidate is not None and not stable):
                    cycle.candidate = None
                    cycle.shore_step = None
                if self.stopping or self.paused:
                    continue
                if cycle.phase is Phase.REFILL:
                    shop = ui.energy_shop(image)
                    self._farm_refill(cycle, shop, ready, active, controls, region)
                    if shop is None and not ready and active - cycle.since >= REFILL_TIMEOUT_S:
                        if not self.machine.record_recovery(active, action="refill_timeout", reason="Obchod s energií neodpovídá."):
                            if self.continuous:
                                self._afk_cooldown_recovery(cycle, controls, region, navigator, activities, progression, active, "Obchod s energií neodpovídá.")
                                last_tick = None
                                continue
                            return "Trvalá chyba při doplňování energie. Zkontroluj okno hry."
                        self._events.emit("Obchod s energií neodpovídá · zkouším zotavení.", key="recovery_notice", level=logging.WARNING, now=active)
                        controls.escape(region.hwnd)
                        cycle.advance(Phase.ENERGY, active, progress=False)
                elif cycle.phase is Phase.GEAR:
                    self._gear(cycle, inventory, hud, active, controls, region)
                    if (navigator and navigator.refill == "equip" and cycle.bread_only and inventory
                            and cycle.gear_step == "unavailable" and controls.click(region, *inventory.close)):
                        navigator.bread_unavailable(active)
                        cycle.next_step = active + .7
                        cycle.advance(Phase.READY, active)
                    if (progression and not cycle.bread_only and cycle.gear_step == "unavailable" and inventory
                            and active >= progression.recheck_shop and controls.click(region, *inventory.close)):
                        self._events.emit("V inventáři není dostupné další vybavení · jdu ověřit obchod.", key="gear_unavailable", now=active)
                        cycle.gear_step = "shop"
                        cycle.advance(Phase.READY, active)
                    if (cycle.phase is Phase.GEAR and active - cycle.since >= GEAR_TIMEOUT_S
                            and (cycle.gear_step != "unavailable" or inventory is None)):
                        if not self.machine.record_recovery(active, action="gear_timeout", reason="Výběr vybavení vypršel."):
                            if self.continuous:
                                self._afk_cooldown_recovery(cycle, controls, region, navigator, activities, progression, active, "Výběr vybavení vypršel.")
                                last_tick = None
                                continue
                            return "Trvalá chyba výběru vybavení. Zkontroluj prut a návnadu ve hře."
                        self._events.emit("Výběr vybavení vypršel · zavírám inventář a obnovuji lov.", key="recovery_notice", level=logging.WARNING, now=active)
                        if inventory:
                            controls.click(region, *inventory.close)
                        else:
                            controls.escape(region.hwnd)
                        cycle.advance(Phase.READY, active, progress=False)
                elif cycle.phase is Phase.CLOSE:
                    if result and result_seen >= 2 and active >= cycle.next_action:
                        if active - cycle.since >= CLOSE_TIMEOUT_S:
                            if not self.machine.record_recovery(active, action="close_timeout", reason="Výsledkový dialog nešel zavřít."):
                                if self.continuous:
                                    self._afk_cooldown_recovery(cycle, controls, region, navigator, activities, progression, active, "Výsledkový dialog nešel zavřít.")
                                    last_tick = None
                                    continue
                                return "Výsledkový dialog nelze zavřít. Zkontroluj okno hry."
                            self._events.emit("Výsledkový dialog nešel zavřít · zkouším Escape a obnovuji lov.", key="recovery_notice", level=logging.WARNING, now=active)
                            controls.escape(region.hwnd)
                            cycle.advance(Phase.READY, active, progress=False)
                        else:
                            close = ui.result_close(image, result) if cycle.retries else None
                            closed = close is not None and controls.click(region, *close)
                            if not closed:
                                closed = controls.escape(region.hwnd)
                                if closed and cycle.retries == 0:
                                    self._log.debug("Výsledek: odeslán Escape.")
                            if closed:
                                cycle.retries += 1
                            cycle.next_action = active + .35
                    elif ready_frames >= 2:
                        self._log.debug("Výsledek zavřen, hra je připravena.")
                        self.machine.record_progress()
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY, active)
                    elif not result and active - cycle.since >= CLOSE_TIMEOUT_S:
                        cycle.advance(Phase.READY, active, progress=False)
                elif cycle.phase is Phase.CAST:
                    if hud and not inventory and not result and (
                        hud.ready is False or hud.energy is not None and cycle.original_energy is not None
                        and hud.energy < cycle.original_energy
                    ):
                        cycle.confirm_cast()
                        cycle.advance(Phase.WAIT, active)
                        self.machine.record_progress()
                        self._log.info("Nahození potvrzeno. Pokusy: %s.", cycle.casts)
                        self._events.emit(f"Nahozeno (hod č. {cycle.casts}).", key="cast_ok", now=active)
                    elif (ready_frames >= 2 and active - cycle.since >= CAST_ACK_S) or active - cycle.since >= CAST_TIMEOUT_S:
                        cycle.retries += 1
                        if cycle.position is not None:
                            cycle.rejected.append(cycle.position)
                            del cycle.rejected[:-6]
                        if cycle.retries >= 2:
                            self._events.emit(
                                "Nahození se nepotvrdilo · vyřazuji bod pro tento běh a hledám jiné místo.",
                                key="cast_retry",
                                cooldown_s=8.0,
                                now=active,
                            )
                        cycle.finish_attempt(active)
                        cycle.advance(Phase.READY, active, progress=False)
                        stable = False
                elif cycle.phase is Phase.WAIT:
                    if ready_frames >= 2:
                        cycle.finish_attempt(active)
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY, active)
                    elif busy:
                        cycle.unreadable_since = None
                        if active - cycle.since >= WAIT_BITE_TIMEOUT_S:
                            if not self.machine.record_recovery(active, action="wait_timeout", reason="Vypršel limit čekání na záběr."):
                                if self.continuous:
                                    self._afk_cooldown_recovery(cycle, controls, region, navigator, activities, progression, active, "Vypršel limit čekání na záběr.")
                                    last_tick = None
                                    continue
                                return "Čekání na záběr vypršelo opakovaně. Zkontroluj hru."
                            self._events.emit("Vypršel limit čekání na záběr · obnovuji lov.", key="recovery_notice", level=logging.WARNING, now=active)
                            cycle.finish_attempt(active)
                            cycle.advance(Phase.READY, active, progress=False)
                    else:
                        if cycle.unreadable_since is None:
                            cycle.unreadable_since = active
                        elif active - cycle.unreadable_since >= WAIT_UNREADABLE_TIMEOUT_S:
                            if not self.machine.record_recovery(active, action="wait_unreadable", reason="HUD během čekání na záběr zmizel."):
                                if self.continuous:
                                    self._afk_cooldown_recovery(cycle, controls, region, navigator, activities, progression, active, "HUD během čekání na záběr zmizel.")
                                    last_tick = None
                                    continue
                                return "Hra při čekání na záběr opakovaně nereaguje."
                            self._events.emit("HUD během čekání na záběr zmizel · obnovuji lov.", key="recovery_notice", level=logging.WARNING, now=active)
                            cycle.finish_attempt(active)
                            cycle.advance(Phase.READY, active, progress=False)
                elif cycle.phase is Phase.RESULT:
                    if ready_frames >= 2:
                        cycle.finish_attempt(active)
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY, active)
                    elif active - cycle.since >= RESULT_TIMEOUT_S:
                        if not self.machine.record_recovery(active, action="result_timeout", reason="Výsledek lovu se neobjevil."):
                            if self.continuous:
                                self._afk_cooldown_recovery(cycle, controls, region, navigator, activities, progression, active, "Výsledek lovu se neobjevil.")
                                last_tick = None
                                continue
                            return "Výsledek lovu se opakovaně nepodařilo rozpoznat."
                        self._events.emit("Výsledek lovu se neobjevil · obnovuji lov.", key="recovery_notice", level=logging.WARNING, now=active)
                        cycle.finish_attempt(active)
                        cycle.advance(Phase.READY, active, progress=False)
                activity = None
                can_travel = cycle.phase in (Phase.READY, Phase.ENERGY) and not inventory and not result
                if (navigator and navigator.settings.refill and not can_travel
                        and cycle.phase in (Phase.WAIT, Phase.RESULT) and not inventory and not result
                        and navigator.in_bakery(image)):
                    cycle.finish_attempt(active)
                    cycle.advance(Phase.READY, active)
                    self._log.info("Pekárna: opouštím neplatné čekání na záběr.")
                    can_travel = True
                if navigator and can_travel:
                    was_refilling = bool(navigator.refill)
                    activity = navigator.tick(image, frame, hud, stable, cycle.casts, active, controls, region,
                                              timed=farm, cancelled=lambda: self.stopping or self.paused)
                    if was_refilling and not navigator.refill:
                        cycle.reset_range(active)
                    if activity:
                        cycle.candidate = cycle.shore_step = None
                        if navigator.wants_bread and hud:
                            self._open_gear(cycle, "bait", hud.bait_button, active, controls, region, bread_only=True)
                if (not activity and activities and not farm and cycle.phase in (Phase.READY, Phase.ENERGY)
                        and not inventory and not result
                        and not self.stopping and not self.paused):
                    if progression:
                        activity = progression.tick(
                            image, ready=ready_frames >= 2, now=active, controls=controls, region=region,
                            missing=cycle.gear if cycle.gear_step == "shop" else None, casts=cycle.casts,
                            caught=cycle.caught, escaped=cycle.escaped, rare_mission=activities.rare_fish, allow_farm=allow_auto_farm,
                        )
                    if not activity:
                        activity = activities.tick(image, ready_frames >= 2, active, controls, region,
                                                   caught=cycle.caught)
                    if activity:
                        cycle.candidate = None
                        if progression and progression.goal == "farm":
                            cycle.position = None
                            cycle.score.clear()
                            cycle.rejected.clear()
                if not activity and cycle.phase in (Phase.READY, Phase.ENERGY) and busy:
                    cycle.advance(Phase.WAIT, active)
                if not activity and cycle.phase in (Phase.READY, Phase.ENERGY) and stable and hud:
                    cycle.unknown_since = None
                    self.machine.record_progress()
                    shore_status = self._ready(cycle, hud, frame, active, controls, region, timed=farm)
                    if shore_status:
                        activity = shore_status
                    if (farm_time and cycle.phase is Phase.ENERGY and not cycle.farm_checked
                            and hud.rod is not None and hud.rod >= 5 and hud.bait is not None and hud.bait >= 5):
                        button = ui.energy_button(image)
                        if button and controls.click(region, *button):
                            cycle.farm_checked = True
                            cycle.quote = None
                            cycle.refill_sent = False
                            cycle.advance(Phase.REFILL, active)
                            cycle.next_action = active + .45
                if (not activity and cycle.phase is Phase.READY and hud is None and inventory is None
                        and result is None and not (navigator and (navigator.visit or navigator.refill))
                        and not (activities and activities.reading_missions)):
                    if cycle.unknown_since is None:
                        cycle.unknown_since = active
                    elif active - cycle.unknown_since >= UNKNOWN_SCREEN_TIMEOUT_S:
                        if not self.machine.record_recovery(active, action="unknown_screen", reason="Neznámá obrazovka bez HUD."):
                            if self.continuous:
                                self._afk_cooldown_recovery(cycle, controls, region, navigator, activities, progression, active, "Neznámá obrazovka bez HUD.")
                                last_tick = None
                                continue
                            return "Opakovaná neznámá obrazovka. Zkontroluj herní okno."
                        self._events.emit(
                            "Neznámá obrazovka bez HUD · zkouším Escape a obnovuji lov.",
                            key="recovery_notice",
                            level=logging.WARNING,
                            now=active,
                        )
                        controls.escape(region.hwnd)
                        cycle.unknown_since = active
                else:
                    cycle.unknown_since = None

                if activity:
                    self.machine.transition(
                        BotState.MISSION_WORK,
                        active,
                        action="activity",
                        reason=self._activity_reason(activity),
                    )
                else:
                    self.machine.transition(self._bot_state_for_phase(cycle.phase), active, action=cycle.phase.name.lower())

                message = cycle.phase.value
                if hud and cycle.phase in (Phase.READY, Phase.ENERGY, Phase.WAIT):
                    message += f" · energie {hud.energy if hud.energy is not None else '?'}"
                if cycle.phase is Phase.WAIT:
                    message += f" · {active - cycle.since:.0f} s od nahození"
                if cycle.phase is Phase.READY and cycle.candidate is None and active < cycle.next_hover:
                    message += " · čekám na kruh dosahu nad vodou"
                if cycle.phase is Phase.GEAR and cycle.gear_step == "unavailable":
                    message = ("Čekám na ruční výběr chleba v inventáři." if cycle.bread_only else
                               "Čekám na dostupný prut nebo návnadu.")
                if activity:
                    message = activity
            if preview_clock.ready(captured_at, selected_crop is not None):
                preview = frame.copy() if balance else frame
                if balance:
                    self._annotate(preview, balance, selected_crop)
                self._update(preview=(round(tick * NS), preview))
            if isinstance(controls, BackgroundControls) and controls.problem:
                message = controls.problem
            elif getattr(controls, "problem", None):
                message = str(controls.problem)
            self._update(text=message, casts=cycle.casts, caught=cycle.caught, escaped=cycle.escaped,
                         active_ns=round(active * NS), tracking_fps=tracking_fps,
                         bot_state=self.machine.state.value)
            interval = 1 / TARGET_HZ if crop else .025 if cycle.phase in (Phase.CAST, Phase.WAIT) else .06
            self._wait_frame(tick, interval, tracking=crop is not None)
        if locked_bg_controls is not None:
            locked_bg_controls.release()
        if awake_active:
            set_keep_awake(False)
        return "AutoFish vypnutý. Šipky uvolněné."

    def _modal_dialog(
        self,
        cycle: Cycle,
        image: Image,
        ui: GameUI,
        now: float,
        controls: Controls,
        region: Region,
        navigator: Navigator | None = None,
    ) -> bool:
        kind = cycle.modal_kind or "level"
        button = ui.level_ok(image)
        if button is not None:
            kind = "level"
        else:
            in_bakery = bool(navigator and navigator.refill and navigator.in_bakery(image))
            if not in_bakery:
                popup = ui.mission_popup_action(image)
                if popup is not None:
                    kind, button = popup
        return self._level_dialog(cycle, button, now, controls, region, kind=kind)

    def _level_dialog(self, cycle: Cycle, button: Point | None, now: float,
                      controls: Controls, region: Region, *, kind: str = "level") -> bool:
        was_clicked = cycle.level_ctrl.clicks > 0 or cycle.level_ctrl.escape_sent
        previous_kind = cycle.modal_kind or kind
        step = cycle.level_ctrl.step(button, now)
        cycle.level_button = cycle.level_ctrl.button
        if step == "none":
            if was_clicked:
                self.machine.record_progress()
                self._log.info("Dialog potvrzen a zmizel z obrazovky.")
                done_msg = {
                    "claim": "Odměna v dialogu potvrzena a vyzvednuta.",
                    "accept": "Mise v dialogu potvrzena a přijata.",
                }.get(previous_kind, "Dialog nové úrovně potvrzen a zavřen.")
                self._events.emit(done_msg, key="modal_done", now=now)
            cycle.modal_kind = ""
            cycle.level_clicks = 0
            cycle.level_click_after = 0.
            return False
        if button is not None:
            cycle.modal_kind = kind
        if step == "click" and button is not None and not self.stopping and not self.paused:
            if controls.click(region, *button):
                cycle.level_ctrl.mark_clicked(now)
                cycle.level_clicks = cycle.level_ctrl.clicks
                cycle.level_click_after = cycle.level_ctrl.click_after
                self._log.info("Potvrzuji dialog tlačítkem OK / Collect / Claim.")
                if cycle.level_ctrl.clicks == 1:
                    click_msg = {
                        "claim": "Vyskakovací odměna · klikám na Collect / Claim.",
                        "accept": "Vyskakovací nabídka mise · klikám na Accept.",
                    }.get(kind, "Zobrazena nová úroveň · klikám na OK.")
                    self._events.emit(click_msg, key="modal_click", now=now)
        elif step == "escape" and not self.stopping and not self.paused:
            if controls.escape(region.hwnd):
                cycle.level_ctrl.mark_escaped(now)
                cycle.level_click_after = cycle.level_ctrl.click_after
                self._log.info("Dialog se po kliknutí nezavřel, odesílám Escape.")
                self._events.emit("Dialog se po kliknutí nezavřel · zkouším Escape.", key="modal_escape", level=logging.WARNING, now=now)
        elif step == "timeout":
            self._log.warning("Dialog nelze zavřít; přecházím do zotavení.")
            self._events.emit("Dialog se nepodařilo zavřít (vypršel časový limit) · přecházím do zotavení.", key="modal_timeout", level=logging.WARNING, now=now)
            self.machine.record_recovery(now, action="modal_timeout", reason="Modální dialog se nezavřel.")
            cycle.level_ctrl.reset()
            cycle.modal_kind = ""
            cycle.level_button = None
            cycle.level_clicks = 0
            cycle.level_click_after = 0.
            return False
        return True

    def _open_gear(self, cycle: Cycle, gear: str, slot: Point, now: float,
                   controls: Controls, region: Region, *, bread_only: bool = False) -> None:
        if self.stopping or self.paused or not controls.click(region, *slot):
            return
        cycle.reset_range(now)
        cycle.gear = gear
        cycle.bread_only = bread_only
        cycle.gear_step = "choose"
        cycle.gear_tab_clicks = 0
        cycle.attempted.clear()
        cycle.pages.clear()
        cycle.select_attempts = 0
        cycle.next_action = now + .7
        cycle.advance(Phase.GEAR, now)
        if bread_only:
            self._events.emit("Nasazuji chléb z inventáře.", key="gear_open", now=now)
        else:
            label = "prut" if gear == "rod" else "návnada"
            self._events.emit(f"Došel {label} · vybírám náhradní v inventáři.", key="gear_open", now=now)

    def _ready(self, cycle: Cycle, hud: Hud, frame: Image, now: float, controls: Controls, region: Region,
               *, timed: bool = False) -> str | None:
        if hud.energy is None:
            return None
        if cycle.phase is Phase.ENERGY and hud.energy > 0:
            cycle.advance(Phase.READY, now)
        if cycle.basic_rod != hud.basic_rod:
            cycle.reset_range(now)
            cycle.basic_rod = hud.basic_rod
        for gear, count, slot in (("rod", hud.rod, hud.rod_button), ("bait", hud.bait, hud.bait_button)):
            if gear == "rod" and hud.basic_rod:
                continue
            if count is None:
                return None
            if count == 0:
                self._open_gear(cycle, gear, slot, now, controls, region)
                return None
        if hud.energy == 0:
            if cycle.phase is not Phase.ENERGY:
                self._events.emit("Došla energie · čekám na dobití.", key="energy_wait", now=now)
            cycle.advance(Phase.ENERGY, now)
            return None
        if now < cycle.next_step:
            return "Přicházím k vodě. Čekám na zastavení postavy."
        if now >= cycle.next_scan:
            preferred = cycle.candidate.point if cycle.candidate else cycle.preferred()
            target = casting_target(frame, preferred, avoid=cycle.position, rejected=tuple(cycle.rejected))
            cycle.next_scan = now if target is not None else now + .2
            if target is None and now >= cycle.next_hover and not self.stopping and not self.paused:
                point = casting_hover(frame, cycle.hover_probe)
                if point is not None:
                    if controls.hover(region, *point):
                        cycle.hover_probe += 1
                        if cycle.hover_probe <= 2:
                            cycle.next_hover = now + .65
                            return None
                    cycle.next_hover = now + 1.5
            if target is None and 0 < cycle.hover_probe <= 2 and now < cycle.next_hover:
                return None
            if (target is None and (self.agent or timed) and cycle.shore_moves < 5
                    and not self.stopping and not self.paused):
                step = shore_approach(frame)
                previous, cycle.shore_step = cycle.shore_step, step
                if step and previous and max(abs(a - b) for a, b in zip(step, previous)) <= .012:
                    key = walking_direction(frame, step)
                    if key and controls.walk(region.hwnd, key, cancelled=lambda: self.stopping or self.paused):
                        cycle.shore_step = None
                        cycle.shore_moves += 1
                        cycle.next_step = now + .42
                        cycle.candidate = None
                        self._log.info("Přesun k břehu před měřením dosahu prutu.")
                        self._events.emit("Přicházím blíž k vodě pro zaměření dosahu.", key="shore_approach", cooldown_s=8.0, now=now)
                        return "Přicházím k vodě."
            else:
                cycle.shore_step = None
            if target is not None and not self.stopping and not self.paused:
                if not controls.pointer_at(region, *target.point):
                    controls.hover(region, *target.point)
                elif target.agrees(cycle.candidate) and controls.cast(
                        region, *target.point, cancelled=lambda: self.stopping or self.paused):
                    learned = cycle.preferred()
                    if learned is not None and max(abs(a - b) for a, b in zip(learned, target.point)) <= .025:
                        score = cycle.score.get(learned, 0)
                        if score > 0 and cycle.learned_spot_reported != (learned, score):
                            cycle.learned_spot_reported = (learned, score)
                            self._events.emit(
                                f"Zkušenost z tohoto běhu: vybírám osvědčené místo u vody (skóre +{score}).",
                                key="learned_spot",
                                cooldown_s=15.0,
                                now=now,
                            )
                    cycle.position = target.point
                    cycle.original_energy = hud.energy
                    cycle.advance(Phase.CAST, now, progress=False)
                    self._log.debug("Klik na vodu v kruhu dosahu: %.3f, %.3f.", *target.point)
            cycle.candidate = target
        return None

    def _farm_refill(self, cycle: Cycle, shop: EnergyShop | None, ready: bool, now: float,
                     controls: Controls, region: Region) -> None:
        if now < cycle.next_action or self.stopping or self.paused:
            return
        if shop is None:
            cycle.quote = None
            if ready:
                cycle.advance(Phase.ENERGY, now)
            return
        previous, cycle.quote = cycle.quote, shop
        if previous != shop:
            return
        if (not cycle.refill_sent and shop.energy == 0 and shop.price == 35
                and shop.balance is not None and shop.balance >= 135 and shop.buy is not None):
            if controls.click(region, *shop.buy):
                cycle.refill_sent = True
                cycle.next_action = now + 1.
                self._log.info("Placená farma: odeslán nákup 5 energie za 35 bankovek.")
                self._events.emit("Placená farma: dokupuji 5 energie za 35 bankovek.", key="farm_refill", now=now)
        elif shop.close and controls.click(region, *shop.close):
            cycle.next_action = now + .45

    def _gear(self, cycle: Cycle, inv: Inventory | None, hud: Hud | None, now: float,
              controls: Controls, region: Region) -> str | None:
        if now < cycle.next_action or self.stopping or self.paused:
            return None
        if cycle.gear_step in ("verify", "unavailable"):
            count = (hud.rod if cycle.gear == "rod" else hud.bait) if hud and hud.ready and inv is None else None
            basic = bool(hud and hud.ready and inv is None and cycle.gear == "rod" and hud.basic_rod)
            confirmed = count is not None and count > 0 and (not cycle.bread_only or hud is not None and hud.bread)
            if basic or confirmed:
                cycle.advance(Phase.READY, now)
                cycle.reset_range(now)
                if basic:
                    self._log.info("Rozpoznán základní dřevěný prut. Měřím nový dosah.")
                    self._events.emit("Nasadil jsem základní dřevěný prut · pokračuji v lovu.", key="gear_ready", now=now)
                else:
                    self._log.info("Vybavení ověřeno v HUD. Zbývající hody: %s.", count)
                    self._events.emit(f"Nové vybavení ověřeno v HUD (zbývá hodů: {count}).", key="gear_ready", now=now)
            elif cycle.gear_step == "verify" and inv is not None and inv.tab == cycle.gear and inv.select and cycle.select_attempts < 2:
                if controls.click(region, *inv.select):
                    cycle.select_attempts += 1
                    cycle.next_action = now + .6
            return None
        if inv is None:
            return None
        if inv.tab != cycle.gear:
            if hud and cycle.gear_tab_clicks < 2:
                tab_btn = hud.rod_button if cycle.gear == "rod" else hud.bait_button
                if controls.click(region, *tab_btn):
                    cycle.gear_tab_clicks += 1
                    cycle.next_action = now + .55
            return None
        if cycle.gear_step == "read" and inv.count is not None and inv.count > 0 and inv.select:
            if controls.click(region, *inv.select):
                cycle.gear_step = "verify"
                cycle.select_attempts = 1
                cycle.next_action = now + .65
            return None
        slots = inv.bread_slots if cycle.bread_only else inv.slots
        slot = next((point for point in slots if point not in cycle.attempted), None)
        if slot is not None:
            if controls.click(region, *slot):
                cycle.attempted.add(slot)
                cycle.gear_step = "read"
                cycle.next_action = now + .55
            return None
        if inv.page and inv.next_page and inv.page not in cycle.pages:
            if controls.click(region, *inv.next_page):
                cycle.pages.add(inv.page)
                cycle.attempted.clear()
                cycle.gear_step = "choose"
                cycle.next_action = now + .65
            return None
        cycle.gear_step = "unavailable"
        return None
