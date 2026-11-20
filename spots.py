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
    shallow = cv2.inRange(hsv, (47, 16, 145), (95, 92, 255))
    kernel = np.ones((7, 7), np.uint8)
    seeds = cv2.bitwise_or(sea, deep_sea)
    water = cv2.morphologyEx(cv2.bitwise_or(seeds, shallow), cv2.MORPH_OPEN, kernel)
    # Světlý kruh může rozdělit vodní plochu. Spojení slouží jen k označení plochy.
    joined = cv2.morphologyEx(water, cv2.MORPH_CLOSE, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(joined)
    minimum = water.size * .024
    seeded = np.bincount(labels[seeds != 0], minlength=count)
    accepted = (stats[:, cv2.CC_STAT_AREA] >= minimum) & (seeded >= water.size * .010)
    accepted[0] = False
    return accepted[labels] & (water != 0)


def _circles(gray: np.ndarray, avatars: list[tuple[float, float, int, int]]) -> list[tuple[float, float, float, float]]:
    """Změří poloměr z oblouku se středem u nohou postavy, včetně oříznutého kruhu."""
    height, width = gray.shape
    angles = np.linspace(0, 2 * np.pi, 96, endpoint=False, dtype=np.float32)
    radii = np.arange(max(12, min(height, width) * .04), hypot(width, height), dtype=np.float32)
    cosines, sines = np.cos(angles), np.sin(angles)
    ray_x = radii[:, None] * cosines
    ray_y = radii[:, None] * sines
    opposing_sides = tuple((axis >= .65, axis <= -.65) for axis in (cosines, sines))
    large_start = max(0, int(np.searchsorted(radii, height * .5)) - 7)
    result = []
    for ax, ay, hat_w, hat_h in avatars:
        best = None
        centers = [(ax + dx * hat_w, ay + dy * hat_h, 0)
                   for dx in (-.14, 0., .14) for dy in (.6, .8, 1., 1.2, 1.4, 1.6)]
        # U otočené postavy může být vidět jen část čepice.
        centers.extend((ax + dx * hat_w, ay + dy * hat_h, large_start)
                       for dx in (-.40, 0., .40) for dy in (1.8, 2.2, 2.6, 3.0))
        for cx, cy, start in centers:
            tested_radii = radii[start + 7:-7]
            mx, my = ray_x[start:] + cx, ray_y[start:] + cy
            values = cv2.remap(gray, mx, my, cv2.INTER_LINEAR).astype(np.float32)
            valid = (mx >= 1) & (mx < width - 2) & (my >= 1) & (my < height - 2)
            valid = valid[:-14] & valid[14:]
            contrast = values[7:-7] - np.maximum(values[:-14], values[14:])
            hits = valid & (contrast >= 3)
            sectors = (hits.reshape(-1, 8, 12).sum(axis=2) >= 4).sum(axis=1)
            visible = valid.sum(axis=1)
            support = hits.sum(axis=1) / np.maximum(1, visible)
            strength = np.minimum(contrast, 14).clip(0) * hits
            enough_sectors = (sectors >= 3) & (visible >= 40) & (support >= .30)
            enough_arcs = np.zeros_like(enough_sectors)
            # U kruhu přesahujícího obě hrany ověř dva protilehlé oblouky.
            for sides, span in zip(opposing_sides, (max(cy, height - cy), max(cx, width - cx))):
                clipped = np.flatnonzero((tested_radii > span) & (visible >= 24)
                                         & (support >= .38) & ~enough_sectors)
                if clipped.size:
                    for side in sides:
                        samples = np.ix_(clipped, side)
                        side_hits = hits[samples].sum(axis=1)
                        clipped = clipped[(side_hits >= 7) & (side_hits >= .38 * valid[samples].sum(axis=1))]
                    enough_arcs[clipped] = True
            scores = np.where((tested_radii >= 1.35 * hat_h) & (enough_sectors | enough_arcs),
                              support + strength.sum(axis=1) / (np.maximum(1, visible) * 55), 0.)
            i = int(scores.argmax())
            if scores[i] > 0 and (best is None or scores[i] > best[0]):
                best = (float(scores[i]), cx, cy, float(tested_radii[i]))
        if best:
            result.append(best)
    return sorted(result, reverse=True)


def _scene(image: Image) -> Image:
    h0, w0 = image.shape[:2]
    scale = min(1.0, 900 / max(h0, w0))
    return cv2.resize(image, (round(w0 * scale), round(h0 * scale)), interpolation=cv2.INTER_AREA)


def _avatars(hsv: Image) -> list[tuple[float, float, int, int]]:
    hat = cv2.inRange(hsv, (127, 115, 55), (166, 255, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(hat)
    avatars = []
    for i in range(1, count):
        _, _, w, h, area = stats[i]
        if 50 <= area <= 2200 and 7 <= w <= 60 and 8 <= h <= 60 and .4 <= w / h <= 2.1:
            ax, ay = centers[i]
            if 0 <= ay <= hsv.shape[0] * .90:
                avatars.append((float(ax), float(ay), int(w), int(h)))
    return avatars


def _shore_avatars(hsv: Image) -> list[tuple[float, float, int, int]]:
    avatars = _avatars(hsv)
    if len(avatars) <= 1:
        return avatars
    height, width = hsv.shape[:2]
    central = [a for a in avatars if .32 * width <= a[0] <= .68 * width
               and .25 * height <= a[1] <= .72 * height and a[3] >= .032 * height]
    return central or avatars


def shore_approach(image: Image) -> Point | None:
    """Krátký krok po souši, pokud je jedna postava a voda v dosažitelné blízkosti."""
    frame = _scene(image)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    height, width = hsv.shape[:2]
    avatars = _shore_avatars(hsv)
    if len(avatars) != 1:
        return None
    x, y, _, hat = avatars[0]
    feet = np.array((x, y + 1.25 * hat))
    yy, xx = np.ogrid[:height, :width]
    world = ((xx >= .06 * width) & (xx < .90 * width) & (yy >= .10 * height)
             & (yy < .85 * height))
    water = _open_water(hsv) & world
    depth = cv2.distanceTransform(water.astype(np.uint8), cv2.DIST_L2, 3)
    squared = (xx - feet[0]) ** 2 + (yy - feet[1]) ** 2
    if not (depth >= 4).any():
        return None
    row, col = np.unravel_index(np.where(depth >= 4, squared, np.inf).argmin(), depth.shape)
    distance = float(np.hypot(col - feet[0], row - feet[1]))
    if not .85 * hat < distance <= 12 * hat:
        return None
    path = np.rint(np.linspace(feet, (col, row), max(2, round(distance)))).astype(int)
    if not ((0 <= path[:, 0]).all() and (path[:, 0] < width).all()
            and (0 <= path[:, 1]).all() and (path[:, 1] < height).all()):
        return None
    wet = np.flatnonzero(water[path[:, 1], path[:, 0]])
    if not wet.size:
        return None
    step = min(round(4 * hat), int(wet[0]) - max(3, round(hat * .20)))
    if step < .65 * hat:
        return None
    sx, sy = path[step]
    if not world[sy, sx] or water[sy-2:sy+3, sx-2:sx+3].any():
        return None
    return int(sx) / width, int(sy) / height


def casting_hover(image: Image) -> Point | None:
    """Najde vodu u postavy pro najetí kurzorem, samostatně nepotvrzuje hod."""
    frame = _scene(image)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    height, width = hsv.shape[:2]
    avatars = _avatars(hsv)
    if not avatars:
        return None
    depth = cv2.distanceTransform(_open_water(hsv).astype(np.uint8), cv2.DIST_L2, 3)
    yy, xx = np.ogrid[:height, :width]
    candidates = []
    for x, y, _, hat_h in avatars:
        distance = (xx-x)**2 + (yy-y-hat_h)**2
        valid = ((depth >= 9) & (distance >= (1.5*hat_h)**2) & (distance <= (7*hat_h)**2)
                 & (xx >= .06*width) & (xx <= .90*width) & (yy >= .10*height) & (yy <= .86*height))
        if valid.any():
            sy, sx = np.unravel_index(np.where(valid, distance, np.inf).argmin(), depth.shape)
            candidates.append((float(distance[sy,sx]) / hat_h**2, (int(sx) / width, int(sy) / height)))
    candidates.sort()
    if not candidates or len(candidates) > 1 and candidates[1][0] <= candidates[0][0] + 1.0:
        return None
    return candidates[0][1]


def casting_target(image: Image, preferred: Point | None = None, avoid: Point | None = None) -> CastTarget | None:
    """Hledá přední sektor dosahu až po rozpoznání vlastního kruhu a břehu."""
    frame = _scene(image)
    height, width = frame.shape[:2]
    if min(height, width) < 96:
        return None
    raw_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    avatars = _avatars(hsv)
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
        area = np.count_nonzero(survey)
        available = (survey & water).astype(np.uint8)
        # Na molu může být voda na obou stranách. Vybereme jen nejbližší
        # dostatečně velkou souvislou plochu, pokud strany nejsou stejně blízko.
        count, labels, stats, _ = cv2.connectedComponentsWithStats(available)
        candidates = []
        for label in range(1, count):
            if stats[label, cv2.CC_STAT_AREA] >= area * .10:
                surface = labels == label
                candidates.append((float(range_sq[surface].min()) ** .5, surface))
        candidates.sort(key=lambda item: item[0])
        if not candidates or len(candidates) > 1 and candidates[1][0] - candidates[0][0] < radius * .15:
            return None
        surface = candidates[0][1]
        ys, xs = np.nonzero(surface)
        # Osa vede od postavy k převaze souvislé vody. Za její zadní polorovinu se nekliká.
        vx, vy = float(xs.mean() - cx), float(ys.mean() - cy)
        length = float(np.hypot(vx, vy))
        if length < radius * .24:
            return None
        forward = (dx * vx + dy * vy) / length
        front_band = survey & (forward > radius * .25)
        back_band = survey & (forward < -radius * .25)
        if back_band.any() and float(surface[back_band].mean()) > .28:
            return None
        if not front_band.any() or float(surface[front_band].mean()) < .43:
            return None
        eligible = (surface & (range_sq <= (radius * .85) ** 2) & (forward >= radius * .22)).astype(np.uint8)
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

