from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from window import Image

PixelBox = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class Balance:
    bar: PixelBox
    indicator: float
    target: float

    @property
    def error(self) -> float:
        return self.indicator - self.target


class BalanceDetector:
    """Zelený střed a dvojice modrých hrotů z dialogu YOU HAVE A BITE."""

    def __init__(self) -> None:
        self._prior: Balance | None = None

    def analyze(self, frame: Image) -> Balance | None:
        height, width = frame.shape[:2]
        if self._prior:
            x, y, w, h = self._prior.bar
            left, top = max(0, x - w // 5), max(0, y - h)
            right, bottom = min(width, x + w + w // 5), min(height, y + 2 * h)
            found = self._find(frame[top:bottom, left:right], left, top, 1.0, self._prior)
            if found:
                self._prior = found
                return found
        scale = min(1.0, 1280 / width)
        sample = frame if scale == 1 else cv2.resize(frame, (round(width * scale), round(height * scale)),
                                                      interpolation=cv2.INTER_AREA)
        result = self._find(sample, 0, 0, scale)
        self._prior = result
        return result

    def analyze_crop(self, image: Image, x: int, y: int) -> Balance | None:
        result = self._find(image, x, y, 1.0, self._prior)
        if result is not None:
            self._prior = result
        return result

    @staticmethod
    def _find(image: Image, origin_x: int, origin_y: int, scale: float,
              prior: Balance | None = None) -> Balance | None:
        if min(image.shape[:2]) < 8:
            return None
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        low = cv2.inRange(hsv, (0, 165, 138), (84, 255, 255))
        red = cv2.inRange(hsv, (170, 165, 138), (179, 255, 255))
        mask = cv2.bitwise_or(low, red)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 7), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            x, y, w, h = cv2.boundingRect(contour)
            if w < 62 or h < 7 or not 5 <= w / h <= 15 or cv2.contourArea(contour) < w * h * 0.78:
                continue
            hue = hsv[y + h // 2, x : x + w, 0]
            color = [int(hue[round((w - 1) * pos)]) for pos in (0.04, 0.18, 0.50, 0.82, 0.96)]
            first, near_left, center, near_right, last = color
            if not ((first <= 14 or first >= 170) and (last <= 14 or last >= 170)
                    and 18 <= near_left <= 49 and 18 <= near_right <= 49 and 47 <= center <= 79):
                continue
            band = np.flatnonzero((hue >= 47) & (hue <= 79))
            if band.size < w * 0.15:
                continue
            geometry = (origin_x + round(x / scale), origin_y + round(y / scale),
                        round(w / scale), round(h / scale))
            last = None
            if prior and all(abs(a - b) <= max(3, h // 3) for a, b in zip(geometry, prior.bar)):
                last = prior.indicator * (w - 1)
            marker = BalanceDetector._marker(hsv, x, y, w, h, last)
            if marker is not None:
                return Balance(geometry, marker / (w - 1), (int(band[0]) + int(band[-1])) / (2 * (w - 1)))
        return None

    @staticmethod
    def _marker(hsv: Image, x: int, y: int, w: int, h: int, last: float | None = None) -> float | None:
        reach = max(2, round(h * 0.14))
        depth = max(3, round(h * 0.40))
        mask = cv2.inRange(hsv[max(0, y - reach) : min(hsv.shape[0], y + h + reach), x : x + w],
                           (88, 115, 95), (135, 255, 255))
        upper = mask[:reach + depth, :]
        lower = mask[-reach - depth :, :]

        def centers(part: Image) -> list[float]:
            count, _, statistics, points = cv2.connectedComponentsWithStats(part)
            return [
                float(points[i, 0]) for i in range(1, count)
                if max(3, h * h * 0.014) <= statistics[i, cv2.CC_STAT_AREA] <= h * h * 0.35
                and statistics[i, cv2.CC_STAT_WIDTH] <= h * 0.80
            ]

        a, b = centers(upper), centers(lower)
        pairs = [(abs(left - right), (left + right) / 2) for left in a for right in b]
        if pairs:
            distance, center = min(pairs)
            if distance <= max(3, h * 0.22):
                return center
        # Jeden hrot stačí až po ověření stejné lišty a návaznosti pohybu.
        if last is not None and bool(a) != bool(b):
            plausible = [center for center in a + b if abs(center - last) <= w * .16]
            if len(plausible) == 1:
                return plausible[0]
        return None


class BalancePolicy:
    def __init__(self) -> None:
        self.held: str | None = None
        self.last_error: float | None = None
        self.last_stamp: float | None = None
        self.velocity = 0.

    def choose(self, state: Balance | None, stamp: float | None = None) -> str | None:
        if state is None:
            self.held = self.last_stamp = self.last_error = None
            self.velocity = 0.
            return None
        error = state.error
        dt = stamp - self.last_stamp if stamp is not None and self.last_stamp is not None else 0.
        if self.last_error is not None and .004 <= dt <= .12:
            velocity = max(-4., min(4., (error - self.last_error) / dt))
            weight = dt / (dt + .022)
            self.velocity += weight * (velocity - self.velocity)
        else:
            self.velocity = 0.
        self.last_stamp, self.last_error = stamp, error
        # Předvídej směr v nejbližších milisekundách, aby korekce přišla dřív.
        correction = max(-.095, min(.095, .045 * self.velocity))
        predicted = error + correction
        # Daleko od středu nejdřív zmenši skutečné vychýlení.
        if abs(error) >= .12 and error * predicted < 0:
            predicted = error
        pixel = 1 / state.bar[2]
        engage, release = max(.014, 2.5 * pixel), max(.005, 1.25 * pixel)
        if predicted > (release if self.held == "left" else engage):
            self.held = "left"
        elif predicted < -(release if self.held == "right" else engage):
            self.held = "right"
        else:
            self.held = None
        return self.held

