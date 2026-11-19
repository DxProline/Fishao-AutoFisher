from __future__ import annotations

from dataclasses import dataclass
from math import hypot

import cv2
import numpy as np

from window import Image

Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class CastTarget:
    point: Point
    center: Point
    radius: float
    aspect: float

    def agrees(self, previous: CastTarget | None) -> bool:
        if previous is None or abs(self.aspect / previous.aspect - 1.) > .015:
            return False
        tolerance = min(self.radius, previous.radius)
        def distance(a: Point, b: Point) -> float:
            return hypot(a[0] - b[0], (a[1] - b[1]) * self.aspect)
        return (abs(self.radius - previous.radius) <= .06 * tolerance
                and distance(self.center, previous.center) <= .07 * tolerance
                and distance(self.point, previous.point) <= .16 * tolerance)


def _open_water(hsv: Image) -> np.ndarray:
    # Noční filtr barví i písek do modra, ale moře má v záznamu vyšší sytost.
    sea = cv2.inRange(hsv, (76, 54, 75), (95, 255, 255))
    deep_sea = cv2.inRange(hsv, (96, 86, 75), (124, 255, 255))
    kernel = np.ones((7, 7), np.uint8)
    water = cv2.morphologyEx(cv2.bitwise_or(sea, deep_sea), cv2.MORPH_OPEN, kernel)
    # Světlý kruh může rozdělit vodní plochu. Spojení slouží jen k označení plochy.
    joined = cv2.morphologyEx(water, cv2.MORPH_CLOSE, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(joined)
    minimum = water.size * .024
    accepted = np.array([False] + [stats[i, cv2.CC_STAT_AREA] >= minimum for i in range(1, count)])
    return accepted[labels] & (water != 0)


def _circles(gray: np.ndarray, avatars: list[tuple[float, float, int, int]]) -> list[tuple[float, float, float, float]]:
    """Změří poloměr z oblouku se středem u nohou postavy, včetně oříznutého kruhu."""
    height, width = gray.shape
    angles = np.linspace(0, 2 * np.pi, 96, endpoint=False, dtype=np.float32)
    radii = np.arange(max(12, min(height, width) * .04), hypot(width, height), 2., dtype=np.float32)
    ray_x = radii[:, None] * np.cos(angles)
    ray_y = radii[:, None] * np.sin(angles)
    result = []
    for ax, ay, hat_w, hat_h in avatars:
        best = None
        for cx in (ax - .14 * hat_w, ax, ax + .14 * hat_w):
            for cy in (ay + step * hat_h for step in (.6, .8, 1., 1.2, 1.4, 1.6)):
                mx, my = ray_x + cx, ray_y + cy
                values = cv2.remap(gray, mx.astype(np.float32), my.astype(np.float32), cv2.INTER_LINEAR).astype(np.float32)
                valid = (mx >= 1) & (mx < width - 2) & (my >= 1) & (my < height - 2)
                valid = valid[:-6] & valid[6:]
                contrast = values[3:-3] - np.maximum(values[:-6], values[6:])
                hits = valid & (contrast >= 4)
                sectors = (hits.reshape(-1, 8, 12).sum(axis=2) >= 4).sum(axis=1)
                visible = valid.sum(axis=1)
                support = hits.sum(axis=1) / np.maximum(1, visible)
                strength = np.minimum(contrast, 14).clip(0) * hits
                scores = np.where((sectors >= 3) & (visible >= 40) & (support >= .30),
                                  support + strength.sum(axis=1) / (np.maximum(1, visible) * 55), 0.)
                i = int(scores.argmax())
                if scores[i] > 0 and (best is None or scores[i] > best[0]):
                    best = (float(scores[i]), cx, cy, float(radii[i+3]))
        if best:
            result.append(best)
    return sorted(result, reverse=True)


def casting_target(image: Image, preferred: Point | None = None, avoid: Point | None = None) -> CastTarget | None:
    """Hledá přední sektor dosahu až po rozpoznání vlastního kruhu a břehu."""
    h0, w0 = image.shape[:2]
    scale = min(1.0, 900 / max(h0, w0))
    frame = cv2.resize(image, (round(w0 * scale), round(h0 * scale)), interpolation=cv2.INTER_AREA)
    height, width = frame.shape[:2]
    minimum = min(height, width)
    if minimum < 96:
        return None
    raw_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hat = cv2.inRange(hsv, (127, 115, 55), (166, 255, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(hat)
    avatars = []
    for i in range(1, count):
        _, _, w, h, area = stats[i]
        if 50 <= area <= 2200 and 7 <= w <= 60 and 8 <= h <= 60 and .4 <= w / h <= 2.1:
            ax, ay = centers[i]
            if 0 <= ay <= height * .90:
                avatars.append((float(ax), float(ay), int(w), int(h)))
    if not avatars:
        return None
    circles = _circles(raw_gray, avatars)
    if not circles:
        return None
    # Při dvou podobně silných kruzích nemáme jistotu, která postava je naše.
    score, cx, cy, radius = circles[0]
    if len(circles) > 1 and circles[1][0] >= score - .10:
        return None
    water = _open_water(hsv)
    yy, xx = np.ogrid[:height, :width]
    def choose_point() -> Point | None:
        dx, dy = xx - cx, yy - cy
        range_sq = dx ** 2 + dy ** 2
        survey = range_sq <= (radius * 1.12) ** 2
        ys, xs = np.nonzero(survey & water)
        if len(xs) < np.count_nonzero(survey) * .10:
            return None
        # Osa vede od postavy k převaze souvislé vody. Za její zadní polorovinu se nekliká.
        vx, vy = float(xs.mean() - cx), float(ys.mean() - cy)
        length = float(np.hypot(vx, vy))
        if length < radius * .24:
            return None
        forward = (dx * vx + dy * vy) / length
        front_band = survey & (forward > radius * .25)
        back_band = survey & (forward < -radius * .25)
        if back_band.any() and float(water[back_band].mean()) > .28:
            return None
        if not front_band.any() or float(water[front_band].mean()) < .43:
            return None
        eligible = (water & (range_sq <= (radius * .85) ** 2) & (forward >= radius * .22)).astype(np.uint8)
        distance = cv2.distanceTransform(eligible, cv2.DIST_L2, 3)
        _, deep, _, (x, y) = cv2.minMaxLoc(distance)
        margin = max(8, radius * .075)
        if deep >= margin:
            if avoid:
                ax, ay = avoid[0] * width, avoid[1] * height
                nearby = (xx - ax) ** 2 + (yy - ay) ** 2 < (radius * .17) ** 2
                other = distance.copy()
                other[nearby] = 0
                _, strength, _, alternate = cv2.minMaxLoc(other)
                if strength >= margin:
                    distance = other
                    deep, (x, y) = strength, alternate
            if preferred:
                sx = min(width-1, max(0, round(preferred[0]*width)))
                sy = min(height-1, max(0, round(preferred[1]*height)))
                if distance[sy,sx] >= margin:
                    return sx / width, sy / height
            return x / width, y / height
        return None

    point = choose_point()
    return CastTarget(point, (cx / width, cy / height), radius / width, height / width) if point else None


def casting_spot(image: Image, preferred: Point | None = None, avoid: Point | None = None) -> Point | None:
    target = casting_target(image, preferred, avoid)
    return target.point if target else None


def water_candidates(image: Image) -> list[dict[str, object]]:
    """Vizuální kandidáti v modré a tyrkysové barvě, bez určení přístupnosti."""
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    water = cv2.inRange(hsv, (78, 60, 48), (125, 255, 255))
    kernel = np.ones((5, 5), dtype=np.uint8)
    water = cv2.morphologyEx(water, cv2.MORPH_CLOSE, kernel)
    water = cv2.morphologyEx(water, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(water, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    results = []
    area = width * height
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        ratio = cv2.contourArea(contour) / area
        if ratio < 0.025:
            break
        # Maximum vzdálenosti od okraje dává bod uvnitř skutečné masky.
        region = np.zeros((height, width), dtype=np.uint8)
        cv2.drawContours(region, [contour], -1, 255, thickness=cv2.FILLED)
        region = cv2.bitwise_and(water, region)
        distances = cv2.distanceTransform(cv2.copyMakeBorder(region, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0),
                                          cv2.DIST_L2, 3)[1:-1, 1:-1]
        _, depth, _, (x, y) = cv2.minMaxLoc(distances)
        if depth < 4:
            continue
        left, top, w, h = cv2.boundingRect(contour)
        results.append({
            "position": [round(x / width, 4), round(y / height, 4)],
            "box": [round(left / width, 4), round(top / height, 4), round(w / width, 4), round(h / height, 4)],
            "area_ratio": round(ratio, 4),
        })
    return results

