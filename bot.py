from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import perf_counter

from assist import TARGET_HZ, TRACK_MISSES, Assist, PreviewClock
from controls import BackgroundControls, Controls
from destinations import Navigator, TravelSettings
from game_ui import Hud, Interface, Inventory, Point
from minigame import BalanceDetector, BalancePolicy, PixelBox
from recorder import NS
from spots import CastTarget, casting_hover, casting_target, shore_approach
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
    next_scan: float = 0.
    next_hover: float = 0.
    score: dict[Point, int] = field(default_factory=dict)
    gear: str | None = None
    gear_step: str = ""
    next_action: float = 0.
    attempted: set[Point] = field(default_factory=set)
    pages: set[tuple[int,int]] = field(default_factory=set)
    select_attempts: int = 0
    original_energy: int | None = None
    saw_waiting_hud: bool = False
    shore_step: Point | None = None
    shore_moves: int = 0
    next_step: float = 0.
    basic_rod: bool | None = None
    bread_only: bool = False

    def reset_range(self, now: float) -> None:
        self.candidate = self.position = self.shore_step = None
        self.score.clear()
        self.next_scan = self.next_hover = now
        self.shore_moves = 0

    def advance(self, phase: Phase, now: float, *, progress: bool = True) -> None:
        if self.phase != phase:
            self.phase, self.since = phase, now
            if progress:
                self.retries = 0

    def finish_attempt(self, now: float, result: str | None = None) -> None:
        if result is not None:
            if result == "caught":
                self.caught += 1
            else:
                self.escaped += 1
            if self.position is not None:
                self.score[self.position] = self.score.get(self.position, 0) + (2 if result == "caught" else -1)
        self.candidate = None
        self.next_scan = now

    def preferred(self) -> Point | None:
        return max((p for p in self.score if self.score[p] > 0), key=self.score.__getitem__, default=None)


class Auto(Assist):
    """Opakování ověřených akcí nad obrazem hry, v samostatném vlákně."""

    def __init__(self, max_casts: int | None = None, *, background: bool = False,
                 agent: bool = False, tournament_profile: Path | None = None,
                 travel: TravelSettings | None = None) -> None:
        super().__init__(background=background)
        self.max_casts = max_casts
        self.agent = agent
        self.tournament_profile = tournament_profile
        self.travel = travel
        self.name = "AutoFish"

    def _limit_reached(self, cycle: Cycle) -> bool:
        return self.max_casts is not None and cycle.casts >= self.max_casts

    def _loop(self, capture: WindowCapture, controls: Controls) -> str:
        ui, detector, policy = Interface(), BalanceDetector(), BalancePolicy()
        tournament = Tournament(ui, self.tournament_profile) if self.tournament_profile else None
        navigator = Navigator(self.travel) if self.travel else None
        cycle = Cycle()
        crop: PixelBox | None = None
        expected: tuple[int,int] | None = None
        agreement: tuple[int | None, int | None, int | None, bool] | None = None
        repetitions = 0
        last_seen = 0.
        observed_result: str | None = None
        result_seen = 0
        preview_clock = PreviewClock()
        active = 0.
        last_tick: float | None = None
        crop_misses = 0
        arrow_logged = False
        while not self.stopping:
            tick = perf_counter()
            if self.paused:
                if navigator:
                    navigator.reset_confirmation()
                if tournament is not None:
                    tournament.previous = None
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
                cycle.shore_step = None
                agreement, repetitions = None, 0
                last_tick = None
                self._update(text="Pozastaveno. Pokračuj přes F8.", tracking_fps=None)
                self._stop_requested.wait(.05)
                continue
            try:
                frame, region = capture.grab(crop, expected=expected if crop else None)
            except FrameUnavailable as exc:
                if navigator:
                    navigator.reset_confirmation()
                if tournament is not None:
                    tournament.previous = None
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
                cycle.shore_step = None
                agreement, repetitions = None, 0
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
            balance = detector.analyze_crop(frame, crop[0], crop[1]) if crop else detector.analyze(frame)
            direction = policy.choose(balance, captured_at)
            if self.stopping or self.paused:
                continue
            controls.direction(region.hwnd, direction)
            tracking_fps = self._tracking_rate(balance is not None, captured_at)
            if balance:
                cycle.candidate = None
                crop_misses = 0
                if cycle.phase is Phase.CAST:
                    cycle.casts += 1
                if cycle.phase is not Phase.BALANCE:
                    self._log.info("Záběr rozpoznán.")
                    arrow_logged = False
                if not arrow_logged and direction is not None and controls.held == direction:
                    self._log.info("První šipka po záběru: %s.", "doleva" if direction == "left" else "doprava")
                    arrow_logged = True
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
                    expected_result = cycle.phase in (Phase.CAST, Phase.WAIT, Phase.BALANCE, Phase.RESULT)
                    if cycle.phase is Phase.CAST:
                        cycle.casts += 1
                    cycle.finish_attempt(active, result if expected_result else None)
                    cycle.advance(Phase.CLOSE,active)
                    cycle.next_action = active
                    self._log.info("Výsledek: %s. Úlovky: %s.", result, cycle.caught)
                inventory = ui.inventory(image)
                hud = ui.hud(image)
                timed = not inventory and not result and cycle.phase in (
                    Phase.READY, Phase.ENERGY, Phase.WAIT,
                ) and ui.timed_fishing(image)
                key = (hud.energy, hud.rod, hud.bait, hud.basic_rod) if (
                    hud and hud.ready and not inventory and not result
                ) else None
                repetitions = repetitions + 1 if key is not None and key == agreement else 1
                agreement = key
                stable = key is not None and repetitions >= 2
                if key is None or (cycle.candidate is not None and not stable):
                    cycle.candidate = None
                    cycle.shore_step = None
                if self.stopping or self.paused:
                    continue
                if cycle.phase is Phase.GEAR:
                    self._gear(cycle, inventory, hud, active, controls, region)
                elif cycle.phase is Phase.CLOSE:
                    if result and result_seen >= 2 and active >= cycle.next_action:
                        close = ui.result_close(image, result) if cycle.retries else None
                        closed = close is not None and controls.click(region, *close)
                        if not closed:
                            closed = controls.escape(region.hwnd)
                        if closed:
                            cycle.retries += 1
                        cycle.next_action = active + .35
                    elif stable:
                        self._log.info("Výsledek zavřen, hra je připravena.")
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY,active)
                elif cycle.phase is Phase.CAST:
                    if hud and not inventory and not result and (
                        not hud.ready or hud.energy is not None and cycle.original_energy is not None
                        and hud.energy < cycle.original_energy
                    ):
                        cycle.saw_waiting_hud = not hud.ready
                        cycle.advance(Phase.WAIT,active)
                        cycle.casts += 1
                        cycle.shore_moves = 0
                        self._log.info("Nahození potvrzeno. Pokusy: %s.", cycle.casts)
                    elif stable and active - cycle.since >= CAST_ACK_S:
                        cycle.retries += 1
                        cycle.finish_attempt(active)
                        cycle.advance(Phase.READY,active,progress=False)
                        stable = False
                elif cycle.phase in (Phase.WAIT, Phase.RESULT):
                    if hud and not hud.ready and not inventory and not result:
                        cycle.saw_waiting_hud = True
                    # Hra může ukončit hod bez minihry. Rozhoduje návrat k aktivní liště.
                    # Obnovenou barevnou lištu bereme v časované zóně i po zmeškaném zšednutí.
                    returned = (cycle.phase is Phase.RESULT or cycle.saw_waiting_hud
                                or timed and repetitions >= 4 and active - cycle.since >= 2.)
                    if stable and returned:
                        cycle.finish_attempt(active)
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY,active)
                activity = None
                can_act = cycle.phase in (Phase.READY, Phase.ENERGY) and not inventory and not result
                if navigator and can_act:
                    activity = navigator.tick(image, frame, hud, stable, cycle.casts, active, controls, region,
                                              timed=timed)
                    if activity:
                        cycle.candidate = cycle.shore_step = None
                        if navigator.wants_bread and hud:
                            self._open_gear(cycle, "bait", hud.bait_button, active, controls, region, bread_only=True)
                if (not activity and not timed and tournament is not None and can_act
                        and hud is not None and hud.energy is not None and hud.energy > 0
                        ):
                    activity = tournament.tick(image, stable, active, controls, region)
                if not activity and can_act and stable and hud:
                    self._ready(cycle,hud,frame,active,controls,region,timed=timed)
                message = cycle.phase.value
                if hud and cycle.phase in (Phase.READY, Phase.ENERGY, Phase.WAIT):
                    message += f" · energie {hud.energy if hud.energy is not None else '?'}"
                    if hud.basic_rod:
                        message += " · základní prut"
                if cycle.phase is Phase.READY and cycle.candidate is None and active < cycle.next_hover:
                    message += " · čekám na kruh dosahu nad vodou"
                if cycle.phase is Phase.READY and active < cycle.next_step:
                    message = "Přicházím k vodě. Čekám na zastavení postavy."
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
            interval = 1 / TARGET_HZ if crop else .025 if cycle.phase in (Phase.CAST, Phase.WAIT) else .06
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
            cycle.candidate = cycle.shore_step = None
            return None
        if hud.energy == 0:
            cycle.candidate = cycle.shore_step = None
            cycle.advance(Phase.ENERGY,now)
            return None
        if cycle.phase is Phase.ENERGY:
            cycle.advance(Phase.READY,now)
        if cycle.basic_rod != hud.basic_rod:
            cycle.reset_range(now)
            cycle.basic_rod = hud.basic_rod
        for gear, count, slot in (("rod",hud.rod,hud.rod_button),("bait",hud.bait,hud.bait_button)):
            if gear == "rod" and hud.basic_rod:
                continue
            if count is None:
                cycle.candidate = cycle.shore_step = None
                return None
            if count == 0:
                self._open_gear(cycle, gear, slot, now, controls, region)
                return None
        if now < cycle.next_step:
            return None
        if now >= cycle.next_scan:
            preferred = cycle.candidate.point if cycle.candidate else cycle.preferred()
            target = casting_target(frame,preferred,avoid=cycle.position)
            cycle.next_scan = now if target is not None else now + .2
            if target is None and (self.agent or timed) and cycle.shore_moves < 5 and not self.stopping and not self.paused:
                step = shore_approach(frame)
                previous, cycle.shore_step = cycle.shore_step, step
                if step and previous and max(abs(a - b) for a, b in zip(step, previous)) <= .012:
                    if controls.click(region, *step):
                        cycle.shore_step = None
                        cycle.shore_moves += 1
                        cycle.next_step = now + 1.15
                        self._log.info("Přesun k břehu před měřením dosahu prutu.")
                        return None
            else:
                cycle.shore_step = None
            if target is None and now >= cycle.next_hover and not self.stopping and not self.paused:
                point = casting_hover(frame)
                if point is not None:
                    controls.hover(region, *point)
                    cycle.next_hover = now + 1.5
            if target is not None and not self.stopping and not self.paused:
                if not controls.pointer_at(region, *target.point):
                    controls.hover(region, *target.point)
                elif target.agrees(cycle.candidate) and controls.cast(
                        region, *target.point, cancelled=lambda: self.stopping or self.paused):
                    cycle.position = target.point
                    cycle.original_energy = hud.energy
                    cycle.saw_waiting_hud = False
                    cycle.advance(Phase.CAST,now,progress=False)
                    self._log.info("Klik na vodu v kruhu dosahu: %.3f, %.3f.", *target.point)
            cycle.candidate = target
        return None

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

