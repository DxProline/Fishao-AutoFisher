from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import replace
from time import perf_counter

import cv2

from controls import Controls
from minigame import Balance, BalanceDetector, BalancePolicy, PixelBox
from recorder import NS, Status, TITLE
from spots import CastTarget, casting_hover, casting_target
from window import FrameUnavailable, Image, WindowCapture

WAIT_BITE_S = 120.0
BALANCE_LIMIT_S = 120.0
FIND_SPOT_S = 20.0
TARGET_HZ = 90
TRACK_MISSES = 4


class Assist(threading.Thread):
    """Jedna potvrzená minihra, volitelně s jedním zkušebním nahozením."""

    def __init__(self, cast_once: bool = False) -> None:
        super().__init__(name="FishaoAssist", daemon=True)
        self.cast_once = cast_once
        self._state = Status(text="Čekám na aktivní hru.")
        self._stop_requested = threading.Event()
        self._paused = threading.Event()
        self._log = logging.getLogger("fishao")
        self._tracking_times: deque[float] = deque()

    @property
    def stopping(self) -> bool:
        return self._stop_requested.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def snapshot(self) -> Status:
        return self._state

    def toggle_pause(self) -> None:
        if not self.stopping:
            if self.paused:
                self._paused.clear()
            else:
                self._paused.set()

    def stop(self) -> None:
        self._stop_requested.set()

    def _update(self, **changes: object) -> None:
        self._state = replace(self._state, **changes)

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
        try:
            controls = Controls()
            try:
                with WindowCapture(TITLE) as capture:
                    final = self._loop(capture, controls)
            finally:
                controls.release()
        except Exception as exc:
            final = f"Chyba asistence. {exc}"
            error = True
            self._log.exception("Chyba asistence.")
        self.stop()
        self._update(text=final, finished=True, error=error)
        self._log.info("%s", final)

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
        next_preview = 0.0
        was_active = False
        while not self.stopping:
            iteration = perf_counter()
            if self.paused:
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                last_spot = None
                was_active = False
                self._update(text="Pozastaveno. Pokračuj přes F8.", tracking_fps=None)
                self._stop_requested.wait(0.05)
                continue
            try:
                frame, region = capture.grab(roi, expected=expected if roi else None)
            except FrameUnavailable as exc:
                controls.release()
                policy.choose(None)
                self._tracking_times.clear()
                last_spot = None
                roi = None
                was_active = False
                self._update(text=str(exc), tracking_fps=None)
                self._stop_requested.wait(0.05)
                continue
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
                if balance_start is None:
                    balance_start = active
                    self._log.info("Rozpoznán začátek minihry.")
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
                if active - throw_time >= WAIT_BITE_S:
                    return "Vypršel čas čekání na záběr po zkušebním hodu."
                status = f"Čekám na záběr · {active - throw_time:.0f}/{WAIT_BITE_S:.0f} s."
            elif self.cast_once:
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
                    last_spot = spot
            else:
                status = "Čekám na lištu minihry. Nahoď ručně."
            if now >= next_preview:
                next_preview = now + (1/24 if entry_box else .12)
                shown = frame.copy() if reading else frame
                if reading:
                    self._annotate(shown, reading, entry_box)
                self._update(preview=(round(now * NS), shown))
            self._update(
                text=status, captured=samples, saved=int(balance_start is not None),
                active_ns=round((active - balance_start) * NS) if balance_start is not None else 0,
                elapsed_ns=round((now - start) * NS),
                tracking_fps=self._tracking_rate(reading is not None, now),
            )
            self._stop_requested.wait(max(0.0, 1 / TARGET_HZ - (perf_counter() - iteration)))
        return "Asistence zastavena. Šipky uvolněné."

    @staticmethod
    def _annotate(image: Image, reading: Balance, crop: PixelBox | None) -> None:
        x, y, w, h = reading.bar
        if crop:
            x, y = x - crop[0], y - crop[1]
        for ratio, color in ((reading.target, (255, 255, 255)), (reading.indicator, (245, 85, 25))):
            px = x + round(ratio * (w - 1))
            cv2.line(image, (px, y), (px, y + h - 1), color, 2, cv2.LINE_AA)

