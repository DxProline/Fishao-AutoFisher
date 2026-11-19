from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter

from activities import Activities
from assist import TARGET_HZ, TRACK_MISSES, Assist
from controls import BackgroundControls, Controls
from game_ui import Hud, Interface, Inventory, Point
from minigame import BalanceDetector, BalancePolicy, PixelBox
from progression import Progression
from recorder import NS
from spots import CastTarget, casting_hover, casting_target
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
    rejected: list[Point] = field(default_factory=list)
    next_scan: float = 0.
    next_hover: float = 0.
    hover_probe: int = 0
    score: dict[Point, int] = field(default_factory=dict)
    gear: str | None = None
    gear_step: str = ""
    next_action: float = 0.
    attempted: set[Point] = field(default_factory=set)
    pages: set[tuple[int,int]] = field(default_factory=set)
    select_attempts: int = 0
    original_energy: int | None = None
    attempt_pending: bool = False

    def confirm_cast(self) -> None:
        if self.phase is Phase.CAST:
            self.attempt_pending = True
            self.casts += 1
            self.rejected.clear()
            self.hover_probe = 0

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

    def __init__(self, *, xp_farm: bool = False, agent: bool = False,
                 background: bool = False) -> None:
        super().__init__(background=background)
        self.xp_farm = xp_farm
        self.name = "Agent Fishao" if agent else "XP Farm" if xp_farm else "AutoFish"
        self.agent = agent

    def _loop(self, capture: WindowCapture, controls: Controls) -> str:
        ui, detector, policy = Interface(), BalanceDetector(), BalancePolicy()
        activities = Activities(ui) if self.agent else None
        progression = Progression(ui) if self.agent else None
        cycle = Cycle()
        crop: PixelBox | None = None
        expected: tuple[int,int] | None = None
        agreement: tuple[int|None,int|None,int|None] | None = None
        repetitions = ready_frames = 0
        last_seen = 0.
        observed_result: str | None = None
        result_seen = 0
        next_preview = active = 0.
        last_tick: float | None = None
        crop_misses = 0
        while not self.stopping:
            tick = perf_counter()
            if self.paused:
                if activities:
                    activities.reset_observation()
                if progression:
                    progression.reset_observation()
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
                agreement, repetitions, ready_frames = None, 0, 0
                last_tick = None
                self._update(text="Pozastaveno. Pokračuj přes F8.", tracking_fps=None)
                self._stop_requested.wait(.05)
                continue
            try:
                frame, region = capture.grab(crop, expected=expected if crop else None)
            except FrameUnavailable as exc:
                if activities:
                    activities.reset_observation()
                if progression:
                    progression.reset_observation()
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                cycle.candidate = None
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
                       ) if cycle.phase not in (Phase.CLOSE, Phase.GEAR, Phase.ENERGY) else None
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
                ready = hud is not None and hud.ready is True and inventory is None and result is None
                busy = hud is not None and hud.ready is False and inventory is None and result is None
                ready_frames = ready_frames + 1 if ready else 0
                key = (hud.energy,hud.rod,hud.bait) if ready and hud else None
                repetitions = repetitions + 1 if key is not None and key == agreement else 1
                agreement = key
                stable = key is not None and repetitions >= 2
                if key is None or (cycle.candidate is not None and not stable):
                    cycle.candidate = None
                if self.stopping or self.paused:
                    continue
                if cycle.phase is Phase.GEAR:
                    self._gear(cycle, inventory, hud, active, controls, region)
                    if (progression and cycle.gear_step == "unavailable" and inventory
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
                        cycle.advance(Phase.READY,active)
                activity = None
                if (activities and cycle.phase in (Phase.READY, Phase.ENERGY)
                        and not inventory and not result
                        and not self.stopping and not self.paused):
                    if progression:
                        activity = progression.tick(
                            image, ready=ready_frames >= 2, now=active, controls=controls, region=region,
                            missing=cycle.gear if cycle.gear_step == "shop" else None, casts=cycle.casts,
                            caught=cycle.caught, escaped=cycle.escaped, rare_mission=activities.rare_fish,
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
                    self._ready(cycle,hud,frame,active,controls,region)
                message = cycle.phase.value
                if hud and cycle.phase in (Phase.READY, Phase.ENERGY, Phase.WAIT):
                    message += f" · energie {hud.energy if hud.energy is not None else '?'}"
                if cycle.phase is Phase.READY and cycle.candidate is None and active < cycle.next_hover:
                    message += " · čekám na kruh dosahu nad vodou"
                if cycle.phase is Phase.GEAR and cycle.gear_step == "unavailable":
                    message = "Čekám na dostupný prut nebo návnadu."
                if activity:
                    message = activity
            if active >= next_preview:
                next_preview = active + (1/24 if selected_crop else .12)
                preview = frame.copy() if balance else frame
                if balance:
                    self._annotate(preview,balance,selected_crop)
                self._update(preview=(round(tick*NS),preview))
            if isinstance(controls, BackgroundControls) and controls.problem:
                message = controls.problem
            self._update(text=message,casts=cycle.casts,caught=cycle.caught,escaped=cycle.escaped,
                         active_ns=round(active*NS), tracking_fps=tracking_fps)
            interval = 1/TARGET_HZ if crop else .06
            self._stop_requested.wait(max(0., interval-(perf_counter()-tick)))
        return "AutoFish vypnutý. Šipky uvolněné."

    def _ready(self, cycle: Cycle, hud: Hud, frame: Image, now: float, controls: Controls, region: Region) -> str | None:
        if hud.energy is None:
            return None
        if cycle.phase is Phase.ENERGY and hud.energy > 0:
            cycle.advance(Phase.READY,now)
        for gear, count, slot in (("rod",hud.rod,hud.rod_button),("bait",hud.bait,hud.bait_button)):
            if count is None:
                return None
            if count == 0:
                if not self.stopping and not self.paused and controls.click(region,*slot):
                    cycle.gear = gear
                    cycle.gear_step = "choose"
                    cycle.attempted.clear()
                    cycle.pages.clear()
                    cycle.select_attempts = 0
                    cycle.next_action = now + .7
                    cycle.advance(Phase.GEAR,now)
                return None
        if hud.energy == 0:
            cycle.advance(Phase.ENERGY,now)
            return None
        if now >= cycle.next_scan:
            preferred = cycle.candidate.point if cycle.candidate else cycle.preferred()
            target = casting_target(frame, preferred, avoid=cycle.position, rejected=tuple(cycle.rejected))
            cycle.next_scan = now if target is not None else now + .2
            if target is None and now >= cycle.next_hover and not self.stopping and not self.paused:
                point = casting_hover(frame, cycle.hover_probe)
                if point is not None:
                    if controls.hover(region, *point):
                        cycle.hover_probe += 1
                    cycle.next_hover = now + 1.5
            if target is not None and not self.stopping and not self.paused:
                if not controls.pointer_at(region, *target.point):
                    controls.hover(region, *target.point)
                elif target.agrees(cycle.candidate) and controls.click(region, *target.point):
                    cycle.position = target.point
                    cycle.original_energy = hud.energy
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
            if count is not None and count > 0:
                cycle.advance(Phase.READY,now)
                cycle.next_scan = now
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
        slot = next((point for point in inv.slots if point not in cycle.attempted),None)
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

