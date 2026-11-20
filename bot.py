from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import perf_counter

from activities import Activities
from assist import TARGET_HZ, TRACK_MISSES, Assist, PreviewClock
from controls import BackgroundControls, Controls
from destinations import CURRENT, Navigator, TravelSettings
from game_ui import EnergyShop, Hud, Interface, Inventory, Point
from minigame import BalanceDetector, BalancePolicy, PixelBox
from progression import Progression
from recorder import NS
from spots import CastTarget, casting_hover, casting_target, shore_approach, timed_farm
from tournament import Tournament
from window import FrameUnavailable, Image, Region, WindowCapture


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
    pages: set[tuple[int,int]] = field(default_factory=set)
    select_attempts: int = 0
    original_energy: int | None = None
    attempt_pending: bool = False
    farm_checked: bool = False
    farm_missing_frames: int = 0
    quote: EnergyShop | None = None
    refill_sent: bool = False
    basic_rod: bool | None = None
    bread_only: bool = False

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
                 tournament_profile: Path | None = None, travel: TravelSettings | None = None) -> None:
        super().__init__(background=background)
        self.xp_farm = xp_farm
        self.name = "Agent Fishao" if agent else "XP Farm" if xp_farm else "AutoFish"
        self.agent = agent
        self.join_tournaments = join_tournaments
        self.max_casts = max_casts
        self.travel = travel
        self.tournament_profile = tournament_profile

    def _limit_reached(self, cycle: Cycle) -> bool:
        return self.max_casts is not None and cycle.casts >= self.max_casts

    def _loop(self, capture: WindowCapture, controls: Controls) -> str:
        ui, detector, policy = Interface(), BalanceDetector(), BalancePolicy()
        activities = Activities(ui, join_tournaments=self.join_tournaments and self.tournament_profile is None) if self.agent else None
        tournament = Tournament(ui, self.tournament_profile) if self.tournament_profile else None
        navigator = Navigator(self.travel) if self.travel else None
        allow_auto_farm = (self.travel is None or self.travel.destination.name == CURRENT
                           and not (self.travel.ponds or self.travel.maps))
        progression = Progression(ui) if self.agent else None
        cycle = Cycle()
        crop: PixelBox | None = None
        expected: tuple[int,int] | None = None
        agreement: tuple[int|None,int|None,int|None,bool] | None = None
        repetitions = ready_frames = 0
        last_seen = 0.
        first_arrow = False
        observed_result: str | None = None
        result_seen = 0
        preview_clock = PreviewClock()
        active = 0.
        last_tick: float | None = None
        crop_misses = 0
        while not self.stopping:
            tick = perf_counter()
            if self.paused:
                if navigator:
                    navigator.reset_confirmation()
                if tournament:
                    tournament.previous = None
                if activities:
                    activities.reset_observation()
                if progression:
                    progression.reset_observation()
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
                cycle.shore_step = None
                agreement, repetitions, ready_frames = None, 0, 0
                last_tick = None
                self._update(text="Pozastaveno. Pokračuj přes F8.", tracking_fps=None)
                self._stop_requested.wait(.05)
                continue
            try:
                frame, region = capture.grab(crop, expected=expected if crop else None)
            except FrameUnavailable as exc:
                if navigator:
                    navigator.reset_confirmation()
                if tournament:
                    tournament.previous = None
                if activities:
                    activities.reset_observation()
                if progression:
                    progression.reset_observation()
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
                cycle.shore_step = None
                agreement, repetitions, ready_frames = None, 0, 0
                crop = None
                crop_misses = 0
                last_tick = None
                self._update(text=str(exc), tracking_fps=None)
                self._stop_requested.wait(.05)
                continue
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
            if balance:
                if activities:
                    activities.reset_observation()
                ready_frames, agreement, repetitions = 0, None, 0
                cycle.candidate = None
                crop_misses = 0
                cycle.confirm_cast()
                if cycle.phase is not Phase.BALANCE:
                    self._log.info("Záběr rozpoznán. Řídím šipky podle ukazatele.")
                    first_arrow = False
                if not first_arrow and direction is not None and controls.held == direction:
                    self._log.info("První šipka po záběru: %s (%s).",
                                   "doleva" if direction == "left" else "doprava",
                                   "zpráva do okna" if isinstance(controls, BackgroundControls)
                                   and controls.uses_window_messages else "vstup Windows do aktivní hry")
                    first_arrow = True
                cycle.advance(Phase.BALANCE, active)
                last_seen = active
                expected = (region.width,region.height)
                x,y,w,h = balance.bar
                left, top = max(0,x-w//6),max(0,y-h)
                right,bottom = min(region.width,x+w+w//6),min(region.height,y+h*2)
                crop = (left,top,right-left,bottom-top)
                message = cycle.phase.value + (" · ←" if direction == "left" else " · →" if direction == "right" else " · střed")
            elif selected_crop:
                crop_misses += 1
                if crop_misses >= TRACK_MISSES:
                    crop = None
                    crop_misses = 0
                message = "Hledám ukazatel na liště minihry."
            else:
                image = ui.normalize(frame)
                result = ui.result(image) if cycle.phase is not Phase.GEAR else None
                if result != observed_result:
                    observed_result, result_seen = result, 0
                result_seen += 1
                if cycle.phase is Phase.BALANCE and active - last_seen > .10:
                    cycle.advance(Phase.RESULT, active)
                if result and result_seen >= 2 and cycle.phase not in (Phase.CLOSE, Phase.GEAR):
                    cycle.confirm_cast()
                    cycle.finish_attempt(active, result)
                    cycle.advance(Phase.CLOSE,active)
                    cycle.next_action = active
                    self._log.info("Výsledek: %s. Úlovky: %s.", result, cycle.caught)
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
                key = (hud.energy,hud.rod,hud.bait,hud.basic_rod) if ready and hud else None
                repetitions = repetitions + 1 if key is not None and key == agreement else 1
                agreement = key
                stable = key is not None and repetitions >= 2
                if key is None or (cycle.candidate is not None and not stable):
                    cycle.candidate = None
                    cycle.shore_step = None
                if self.stopping or self.paused:
                    continue
                if cycle.phase is Phase.REFILL:
                    self._farm_refill(cycle, ui.energy_shop(image), ready, active, controls, region)
                elif cycle.phase is Phase.GEAR:
                    self._gear(cycle, inventory, hud, active, controls, region)
                    if (progression and not cycle.bread_only and cycle.gear_step == "unavailable" and inventory
                            and active >= progression.recheck_shop and controls.click(region, *inventory.close)):
                        cycle.gear_step = "shop"
                        cycle.advance(Phase.READY, active)
                elif cycle.phase is Phase.CLOSE:
                    if result and result_seen >= 2 and active >= cycle.next_action:
                        close = ui.result_close(image, result) if cycle.retries else None
                        closed = close is not None and controls.click(region, *close)
                        if not closed:
                            closed = controls.escape(region.hwnd)
                        if closed:
                            cycle.retries += 1
                        cycle.next_action = active + .35
                    elif ready_frames >= 2:
                        self._log.info("Výsledek zavřen, hra je připravena.")
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY,active)
                elif cycle.phase is Phase.CAST:
                    if hud and not inventory and not result and (
                        hud.ready is False or hud.energy is not None and cycle.original_energy is not None
                        and hud.energy < cycle.original_energy
                    ):
                        cycle.confirm_cast()
                        cycle.advance(Phase.WAIT,active)
                        self._log.info("Nahození potvrzeno. Pokusy: %s.", cycle.casts)
                    elif ready_frames >= 2 and active - cycle.since >= CAST_ACK_S:
                        cycle.retries += 1
                        if cycle.position is not None:
                            cycle.rejected.append(cycle.position)
                            del cycle.rejected[:-6]
                        cycle.finish_attempt(active)
                        cycle.advance(Phase.READY,active,progress=False)
                        stable = False
                elif cycle.phase in (Phase.WAIT, Phase.RESULT):
                    # Zšedlá nebo nečitelná lišta znamená čekání bez časového limitu.
                    if ready_frames >= 2:
                        cycle.finish_attempt(active)
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY,active)
                activity = None
                can_travel = cycle.phase in (Phase.READY, Phase.ENERGY) and not inventory and not result
                if navigator and can_travel:
                    activity = navigator.tick(image, frame, hud, stable, cycle.casts, active, controls, region,
                                              timed=farm)
                    if activity:
                        cycle.candidate = cycle.shore_step = None
                        if navigator.wants_bread and hud:
                            self._open_gear(cycle, "bait", hud.bait_button, active, controls, region, bread_only=True)
                if not activity and tournament and not farm and can_travel:
                    activity = tournament.tick(image, bool(stable and hud and hud.energy), active, controls, region)
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
                if not activity and cycle.phase in (Phase.READY,Phase.ENERGY) and stable and hud:
                    shore_status = self._ready(cycle,hud,frame,active,controls,region,timed=farm)
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
                    self._annotate(preview,balance,selected_crop)
                self._update(preview=(round(tick*NS),preview))
            if isinstance(controls, BackgroundControls) and controls.problem:
                message = controls.problem
            self._update(text=message,casts=cycle.casts,caught=cycle.caught,escaped=cycle.escaped,
                         active_ns=round(active*NS), tracking_fps=tracking_fps)
            interval = 1/TARGET_HZ if crop else .025 if cycle.phase in (Phase.CAST, Phase.WAIT) else .06
            self._wait_frame(tick, interval, tracking=crop is not None)
        return "AutoFish vypnutý. Šipky uvolněné."

    def _open_gear(self, cycle: Cycle, gear: str, slot: Point, now: float,
                   controls: Controls, region: Region, *, bread_only: bool = False) -> None:
        if self.stopping or self.paused or not controls.click(region, *slot):
            return
        cycle.reset_range(now)
        cycle.gear = gear
        cycle.bread_only = bread_only
        cycle.gear_step = "choose"
        cycle.attempted.clear()
        cycle.pages.clear()
        cycle.select_attempts = 0
        cycle.next_action = now + .7
        cycle.advance(Phase.GEAR, now)

    def _ready(self, cycle: Cycle, hud: Hud, frame: Image, now: float, controls: Controls, region: Region,
               *, timed: bool = False) -> str | None:
        if hud.energy is None:
            return None
        if cycle.phase is Phase.ENERGY and hud.energy > 0:
            cycle.advance(Phase.READY,now)
        if cycle.basic_rod != hud.basic_rod:
            cycle.reset_range(now)
            cycle.basic_rod = hud.basic_rod
        for gear, count, slot in (("rod",hud.rod,hud.rod_button),("bait",hud.bait,hud.bait_button)):
            if gear == "rod" and hud.basic_rod:
                continue
            if count is None:
                return None
            if count == 0:
                self._open_gear(cycle, gear, slot, now, controls, region)
                return None
        if hud.energy == 0:
            cycle.advance(Phase.ENERGY,now)
            return None
        if now < cycle.next_step:
            return "Přicházím k vodě. Čekám na zastavení postavy."
        if now >= cycle.next_scan:
            preferred = cycle.candidate.point if cycle.candidate else cycle.preferred()
            target = casting_target(frame, preferred, avoid=cycle.position, rejected=tuple(cycle.rejected))
            cycle.next_scan = now if target is not None else now + .2
            if (target is None and (self.agent or timed) and cycle.shore_moves < 5
                    and not self.stopping and not self.paused):
                step = shore_approach(frame)
                previous, cycle.shore_step = cycle.shore_step, step
                if step and previous and max(abs(a - b) for a, b in zip(step, previous)) <= .012:
                    if controls.click(region, *step):
                        cycle.shore_step = None
                        cycle.shore_moves += 1
                        cycle.next_step = now + 1.15
                        cycle.candidate = None
                        self._log.info("Přesun k břehu před měřením dosahu prutu.")
                        return "Přicházím k vodě."
            else:
                cycle.shore_step = None
            if target is None and now >= cycle.next_hover and not self.stopping and not self.paused:
                point = casting_hover(frame, cycle.hover_probe)
                if point is not None:
                    if controls.hover(region, *point):
                        cycle.hover_probe += 1
                    cycle.next_hover = now + 1.5
            if target is not None and not self.stopping and not self.paused:
                if not controls.pointer_at(region, *target.point):
                    controls.hover(region, *target.point)
                elif target.agrees(cycle.candidate) and controls.cast(
                        region, *target.point, cancelled=lambda: self.stopping or self.paused):
                    cycle.position = target.point
                    cycle.original_energy = hud.energy
                    cycle.advance(Phase.CAST,now,progress=False)
                    self._log.info("Klik na vodu v kruhu dosahu: %.3f, %.3f.", *target.point)
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
                cycle.advance(Phase.READY,now)
                cycle.reset_range(now)
                if basic:
                    self._log.info("Rozpoznán základní dřevěný prut. Měřím nový dosah.")
                else:
                    self._log.info("Vybavení ověřeno v HUD. Zbývající hody: %s.", count)
            elif cycle.gear_step == "verify" and inv is not None and inv.tab == cycle.gear and inv.select and cycle.select_attempts < 2:
                if controls.click(region,*inv.select):
                    cycle.select_attempts += 1
                    cycle.next_action = now + .6
            return None
        if inv is None or inv.tab != cycle.gear:
            return None
        if cycle.gear_step == "read" and inv.count is not None and inv.count > 0 and inv.select:
            if controls.click(region,*inv.select):
                cycle.gear_step = "verify"
                cycle.select_attempts = 1
                cycle.next_action = now + .65
            return None
        slots = inv.bread_slots if cycle.bread_only else inv.slots
        slot = next((point for point in slots if point not in cycle.attempted),None)
        if slot is not None:
            if controls.click(region,*slot):
                cycle.attempted.add(slot)
                cycle.gear_step = "read"
                cycle.next_action = now + .55
            return None
        if inv.page and inv.next_page and inv.page not in cycle.pages:
            if controls.click(region,*inv.next_page):
                cycle.pages.add(inv.page)
                cycle.attempted.clear()
                cycle.gear_step = "choose"
                cycle.next_action = now + .65
            return None
        cycle.gear_step = "unavailable"
        return None

