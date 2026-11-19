from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter

from assist import TARGET_HZ, TRACK_MISSES, Assist
from controls import Controls
from game_ui import Hud, Interface, Inventory, Point
from minigame import BalanceDetector, BalancePolicy, PixelBox
from recorder import NS
from spots import casting_spot
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
    candidate: Point | None = None
    next_scan: float = 0.
    score: dict[Point, int] = field(default_factory=dict)
    gear: str | None = None
    gear_step: str = ""
    next_action: float = 0.
    attempted: set[Point] = field(default_factory=set)
    pages: set[tuple[int,int]] = field(default_factory=set)
    select_attempts: int = 0
    original_energy: int | None = None
    saw_waiting_hud: bool = False

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

    def __init__(self, max_casts: int | None = None) -> None:
        super().__init__()
        self.max_casts = max_casts
        self.name = "AutoFish"

    def _limit_reached(self, cycle: Cycle) -> bool:
        return self.max_casts is not None and cycle.casts >= self.max_casts

    def _loop(self, capture: WindowCapture, controls: Controls) -> str:
        ui, detector, policy = Interface(), BalanceDetector(), BalancePolicy()
        cycle = Cycle()
        crop: PixelBox | None = None
        expected: tuple[int,int] | None = None
        agreement: tuple[int|None,int|None,int|None] | None = None
        repetitions = 0
        last_seen = 0.
        observed_result: str | None = None
        result_seen = 0
        next_preview = active = 0.
        last_tick: float | None = None
        crop_misses = 0
        while not self.stopping:
            tick = perf_counter()
            if self.paused:
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                last_tick = None
                self._update(text="Pozastaveno. Pokračuj přes F8.", tracking_fps=None)
                self._stop_requested.wait(.05)
                continue
            try:
                frame, region = capture.grab(crop, expected=expected if crop else None)
            except FrameUnavailable as exc:
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
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
                crop_misses = 0
                if cycle.phase is Phase.CAST:
                    cycle.casts += 1
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
                if result and result_seen >= 2 and cycle.phase in (Phase.WAIT,Phase.BALANCE,Phase.RESULT):
                    cycle.finish_attempt(active,result)
                    cycle.advance(Phase.CLOSE,active)
                    cycle.next_action = active
                    self._log.info("Výsledek: %s. Úlovky: %s.", result, cycle.caught)
                inventory = ui.inventory(image)
                hud = ui.hud(image)
                key = (hud.energy,hud.rod,hud.bait) if hud and hud.ready and not inventory and not result else None
                repetitions = repetitions + 1 if key is not None and key == agreement else 1
                agreement = key
                stable = key is not None and repetitions >= 2
                if self.stopping or self.paused:
                    continue
                if cycle.phase is Phase.GEAR:
                    self._gear(cycle, inventory, hud, active, controls, region)
                elif cycle.phase is Phase.CLOSE:
                    if result and active >= cycle.next_action:
                        if controls.escape(region.hwnd):
                            cycle.retries += 1
                        cycle.next_action = active + .7
                    elif stable:
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
                    elif stable and active - cycle.since >= CAST_ACK_S:
                        cycle.retries += 1
                        cycle.finish_attempt(active)
                        cycle.advance(Phase.READY,active,progress=False)
                        stable = False
                elif cycle.phase in (Phase.WAIT, Phase.RESULT):
                    if hud and not hud.ready and not inventory and not result:
                        cycle.saw_waiting_hud = True
                    # Hra může ukončit hod bez minihry. Rozhoduje návrat k aktivní liště.
                    returned = cycle.phase is Phase.RESULT or cycle.saw_waiting_hud
                    if stable and returned:
                        cycle.finish_attempt(active)
                        if self._limit_reached(cycle):
                            return "Dosažen zvolený limit nahození. Hotovo."
                        cycle.advance(Phase.READY,active)
                if cycle.phase in (Phase.READY,Phase.ENERGY) and stable and hud:
                    self._ready(cycle,hud,frame,active,controls,region)
                message = cycle.phase.value
                if hud and cycle.phase in (Phase.READY, Phase.ENERGY, Phase.WAIT):
                    message += f" · energie {hud.energy if hud.energy is not None else '?'}"
                if cycle.phase is Phase.GEAR and cycle.gear_step == "unavailable":
                    message = "Čekám na dostupný prut nebo návnadu."
            if active >= next_preview:
                next_preview = active + (1/24 if selected_crop else .12)
                preview = frame.copy() if balance else frame
                if balance:
                    self._annotate(preview,balance,selected_crop)
                self._update(preview=(round(tick*NS),preview))
            self._update(text=message,casts=cycle.casts,caught=cycle.caught,escaped=cycle.escaped,
                         active_ns=round(active*NS), tracking_fps=tracking_fps)
            interval = 1/TARGET_HZ if crop else .06
            self._stop_requested.wait(max(0., interval-(perf_counter()-tick)))
        return "AutoFish vypnutý. Šipky uvolněné."

    def _ready(self, cycle: Cycle, hud: Hud, frame: Image, now: float, controls: Controls, region: Region) -> str | None:
        if hud.energy is None:
            return None
        if hud.energy == 0:
            cycle.advance(Phase.ENERGY,now)
            return None
        if cycle.phase is Phase.ENERGY:
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
        if now >= cycle.next_scan:
            point = casting_spot(frame,cycle.preferred(),avoid=cycle.position)
            cycle.next_scan = now if point is not None else now + .2
            same = point is not None and cycle.candidate is not None and math.dist(point,cycle.candidate) <= .04
            if same and not self.stopping and not self.paused and controls.click(region,*point):
                cycle.position = point
                cycle.original_energy = hud.energy
                cycle.saw_waiting_hud = False
                cycle.advance(Phase.CAST,now,progress=False)
            cycle.candidate = point
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

