from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import replace
from math import floor
from time import perf_counter, sleep

import cv2

from controls import BackgroundControls, Controls
from minigame import Balance, BalanceDetector, BalancePolicy, PixelBox
from recorder import NS, Status, TITLE
from runtime_state import BotState, EventFeed, StateMachine
from spots import CastTarget, casting_hover, casting_target
from window import FrameUnavailable, Image, WindowCapture, WorkstationLocked, set_keep_awake

WAIT_BITE_S = 120.0
BALANCE_LIMIT_S = 120.0
FIND_SPOT_S = 20.0
TARGET_HZ = 120
PREVIEW_HZ = 60
TRACK_MISSES = 4


class PreviewClock:
    def __init__(self) -> None:
        self._next = 0.
        self._tracking = False

    def ready(self, now: float, tracking: bool) -> bool:
        if tracking != self._tracking:
            self._tracking = tracking
            self._next = now
        if now < self._next:
            return False
        interval = 1 / PREVIEW_HZ if tracking else .12
        self._next += (floor((now - self._next) / interval) + 1) * interval
        return True


class Assist(threading.Thread):
    """Jedna potvrzená minihra, volitelně s jedním zkušebním nahozením."""

    def __init__(self, cast_once: bool = False, *, background: bool = False, prevent_sleep: bool = True) -> None:
        super().__init__(name="FishaoAssist", daemon=True)
        self.cast_once = cast_once
        self.background = background
        self.prevent_sleep = prevent_sleep
        self.continuous = False
        self._state = Status(text="Čekám na aktivní hru.")
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._paused = threading.Event()
        self._log = logging.getLogger("fishao")
        self._events = EventFeed()
        self._tracking_times: deque[float] = deque()
        self._extra_controls: Controls | None = None
        self.machine = StateMachine()

    @property
    def stopping(self) -> bool:
        return self._stop_requested.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def snapshot(self) -> Status:
        with self._lock:
            return self._state

    def toggle_pause(self) -> None:
        if not self.stopping:
            if self.paused:
                self._paused.clear()
                self._log.info("Pokračuji po pauze.")
                self._events.emit("Pokračuji v lovu.", key="pause")
            else:
                self._paused.set()
                self._log.info("Ruční pauza. Šipky budou uvolněny.")
                self._events.emit("Pozastaveno uživatelem (F8). Šipky uvolněné.", key="pause")

    def stop(self) -> None:
        self._stop_requested.set()

    def _update(self, **changes: object) -> None:
        with self._lock:
            self._state = replace(self._state, **changes)

    def _wait_frame(self, started: float, interval: float, *, tracking: bool) -> None:
        remaining = interval - (perf_counter() - started)
        if remaining <= 0 or self.stopping:
            return
        if tracking:
            sleep(remaining)
        else:
            self._stop_requested.wait(remaining)

    def _tracking_rate(self, detected: bool, now: float) -> float | None:
        times = self._tracking_times
        while times and now - times[0] > .75:
            times.popleft()
        if detected:
            times.append(now)
        if len(times) < 2 or times[0] == times[-1]:
            return None
        return (len(times) - 1) / (now - times[0])

    def run(self) -> None:
        final = "Asistence ukončena."
        error = False
        while True:
            controls: Controls | None = None
            try:
                self.machine.transition(BotState.CHECKING_ENV, perf_counter(), action="init_controls")
                controls = BackgroundControls(TITLE) if self.background else Controls()
                try:
                    with WindowCapture(TITLE, background=self.background) as capture:
                        final = self._loop(capture, controls)
                finally:
                    set_keep_awake(False)
                    if self._extra_controls is not None:
                        try:
                            self._extra_controls.release()
                        except Exception:
                            pass
                        self._extra_controls = None
                    try:
                        controls.release()
                    except Exception:
                        self._log.debug("Uvolnění kláves při ukončení selhalo.", exc_info=True)
            except Exception as exc:
                self._log.exception("Chyba běhu automatizace.")
                if self.continuous and not self.stopping:
                    self.machine.transition(
                        BotState.RECOVERING,
                        perf_counter(),
                        action="worker_restart",
                        result="retrying",
                        reason=str(exc),
                        level=logging.WARNING,
                    )
                    self._events.emit(
                        f"Obnovuji lov po chybě · důvod: {exc}",
                        key="worker_recover",
                        level=logging.WARNING,
                    )
                    self._update(
                        text=f"Zotavuji AFK lov po chybě: {exc}",
                        tracking_fps=None,
                        bot_state=BotState.RECOVERING.value,
                    )
                    self._stop_requested.wait(5.0)
                    if not self.stopping:
                        continue
                final = f"Chyba asistence. {exc}"
                error = True
                self.machine.transition(
                    BotState.ERROR_STOPPED,
                    perf_counter(),
                    action="worker_run",
                    result="error",
                    reason=str(exc),
                    level=logging.ERROR,
                )
                self._events.emit(f"Automatizace skončila chybou · důvod: {exc}", key="worker_stop", level=logging.ERROR)
            else:
                self.machine.transition(BotState.STOPPED, perf_counter(), action="worker_run", result="stopped")
            break
        self.stop()
        self._update(text=final, finished=True, error=error, bot_state=self.machine.state.value)
        self._log.info("%s", final)
        if not error:
            self._events.emit(final, key="worker_stop")

    def _loop(self, capture: WindowCapture, controls: Controls) -> str:
        detector = BalanceDetector()
        policy = BalancePolicy()
        roi: PixelBox | None = None
        expected: tuple[int, int] | None = None
        failed_in_crop = 0
        active = 0.0
        samples = 0
        start = elapsed_previous = perf_counter()
        last_seen: float | None = None
        balance_start: float | None = None
        throw_time: float | None = None
        last_spot: CastTarget | None = None
        next_spot = 0.0
        next_hover = 0.0
        preview_clock = PreviewClock()
        was_active = False
        wait_reason: str | None = None
        awake_active = False
        next_awake_refresh = 0.0
        while not self.stopping:
            iteration = perf_counter()
            if self.paused:
                if awake_active:
                    set_keep_awake(False)
                    awake_active = False
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                last_spot = None
                was_active = False
                self.machine.transition(BotState.PAUSED_MANUAL, iteration, action="pause", reason="manual_pause")
                self._update(text="Pozastaveno. Pokračuj přes F8.", tracking_fps=None, bot_state=BotState.PAUSED_MANUAL.value)
                self._stop_requested.wait(0.05)
                continue
            if self.prevent_sleep and (not awake_active or iteration >= next_awake_refresh):
                set_keep_awake(True)
                awake_active = True
                next_awake_refresh = iteration + 30.0
            try:
                frame, region = capture.grab(roi, expected=expected if roi else None)
            except WorkstationLocked as exc:
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                last_spot = None
                roi = None
                was_active = False
                wait_reason = "locked"
                self.machine.transition(BotState.PAUSED_LOCKED, iteration, action="grab", result="locked", reason=str(exc))
                self._events.emit("Počítač je uzamčen · pozastaveno do odemčení.", key="grab_wait", level=logging.WARNING, now=iteration)
                self._update(text=str(exc), tracking_fps=None, bot_state=BotState.PAUSED_LOCKED.value)
                self._stop_requested.wait(0.25)
                continue
            except FrameUnavailable as exc:
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                last_spot = None
                roi = None
                was_active = False
                wait_reason = "unavailable"
                self.machine.transition(BotState.RECOVERING, iteration, action="grab", result="unavailable", reason=str(exc))
                self._events.emit(f"Čekám na okno hry · {exc}", key="grab_wait", cooldown_s=15.0, now=iteration)
                self._update(text=str(exc), tracking_fps=None, bot_state=BotState.RECOVERING.value)
                self._stop_requested.wait(0.05)
                continue
            if wait_reason == "locked":
                self._events.emit("Plocha odemčena · pokračuji v asistenci.", key="grab_wait", now=iteration)
            elif wait_reason == "unavailable":
                self._events.emit("Okno hry je opět dostupné · pokračuji.", key="grab_wait", now=iteration)
            wait_reason = None
            now = perf_counter()
            if was_active:
                active += now - elapsed_previous
            elapsed_previous = now
            was_active = True
            entry_box = roi
            if roi:
                x, y, _, _ = roi
                reading = detector.analyze_crop(frame, x, y)
                if reading is None:
                    failed_in_crop += 1
                    if failed_in_crop >= TRACK_MISSES:
                        roi = None
                else:
                    failed_in_crop = 0
            else:
                reading = detector.analyze(frame)
            if self.stopping or self.paused:
                continue
            direction = policy.choose(reading, now)
            controls.direction(region.hwnd, direction)
            if reading:
                self.machine.transition(BotState.MINIGAME, now, action="balance")
                if balance_start is None:
                    balance_start = active
                    self._log.info("Rozpoznán začátek minihry.")
                    self._events.emit("Záběr rozpoznán · vyvažuji rybu.", key="assist_minigame", now=now)
                if active - balance_start >= BALANCE_LIMIT_S:
                    return "Vypršel vlastní limit minihry."
                last_seen = active
                expected = (region.width, region.height)
                bx, by, bw, bh = reading.bar
                l, t = max(0, bx - bw // 6), max(0, by - bh)
                r, b = min(region.width, bx + bw + bw // 6), min(region.height, by + 2 * bh)
                roi = (l, t, r - l, b - t)
                samples += 1
                status = "MINIHRA · " + {"left": "držím ←", "right": "držím →", None: "ukazatel u středu"}[direction]
            elif last_seen is not None:
                if active - last_seen >= 0.8:
                    return "Lišta zmizela. Výsledek zkontroluj a zavři ve hře klávesou Esc."
                status = "Lišta není čitelná. Šipky uvolněné."
            elif throw_time is not None:
                self.machine.transition(BotState.WAITING_BITE, now, action="wait_bite")
                if active - throw_time >= WAIT_BITE_S:
                    return "Vypršel čas čekání na záběr po zkušebním hodu."
                status = f"Čekám na záběr · {active - throw_time:.0f}/{WAIT_BITE_S:.0f} s."
            elif self.cast_once:
                self.machine.transition(BotState.READY, now, action="scan_spot")
                status = "Hledám vodu uvnitř kruhu pro nahození."
                if active >= FIND_SPOT_S:
                    return "Nenalezen vodní bod uvnitř kruhu. Zkontroluj postavení u vody."
                if active >= next_spot:
                    next_spot = active + 0.40
                    spot = casting_target(frame, last_spot.point if last_spot else None)
                    if spot is None and active >= next_hover and not self.stopping and not self.paused:
                        point = casting_hover(frame)
                        if point is not None:
                            controls.hover(region, *point)
                            next_hover = active + 1.5
                    if spot is not None and not self.stopping and not self.paused:
                        if not controls.pointer_at(region, *spot.point):
                            controls.hover(region, *spot.point)
                        elif spot.agrees(last_spot) and controls.cast(
                                region, *spot.point, cancelled=lambda: self.stopping or self.paused):
                            throw_time = active
                            self._log.info("Proveden jeden zkušební hod.")
                            self._events.emit("Zkušební hod proveden · čekám na záběr.", key="assist_cast", now=now)
                    last_spot = spot
            else:
                self.machine.transition(BotState.READY, now, action="wait_manual_cast")
                status = "Čekám na lištu minihry. Nahoď ručně."
            if preview_clock.ready(now, entry_box is not None):
                shown = frame.copy() if reading else frame
                if reading:
                    self._annotate(shown, reading, entry_box)
                self._update(preview=(round(now * NS), shown))
            self._update(
                text=status, captured=samples, saved=int(balance_start is not None),
                active_ns=round((active - balance_start) * NS) if balance_start is not None else 0,
                elapsed_ns=round((now - start) * NS),
                tracking_fps=self._tracking_rate(reading is not None, now),
                bot_state=self.machine.state.value,
            )
            self._wait_frame(iteration, 1 / TARGET_HZ, tracking=roi is not None)
        return "Asistence zastavena. Šipky uvolněné."

    @staticmethod
    def _annotate(image: Image, reading: Balance, crop: PixelBox | None) -> None:
        x, y, w, h = reading.bar
        if crop:
            x, y = x - crop[0], y - crop[1]
        for ratio, color in ((reading.target, (255, 255, 255)), (reading.indicator, (245, 85, 25))):
            px = x + round(ratio * (w - 1))
            cv2.line(image, (px, y), (px, y + h - 1), color, 2, cv2.LINE_AA)
