from __future__ import annotations

import cv2
import numpy as np

from window import Image


def casting_spot(image: Image, preferred: tuple[float,float] | None = None,
                 avoid: tuple[float,float] | None = None) -> tuple[float, float] | None:
    """Navrhne vodní bod jen při zřetelném světlém obvodu v dosahu postavy."""
    h0, w0 = image.shape[:2]
    scale = min(1.0, 900 / max(h0, w0))
    frame = cv2.resize(image, (round(w0 * scale), round(h0 * scale)), interpolation=cv2.INTER_AREA)
    height, width = frame.shape[:2]
    minimum = min(height, width)
    if minimum < 96:
        return None
    gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, 1.3, minimum / 7, param1=55, param2=28,
                              minRadius=max(28, round(minimum * .07)), maxRadius=round(minimum * .49))
    if circles is None:
        return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    water = cv2.inRange(hsv, (77, 36, 70), (124, 255, 255))
    water = cv2.morphologyEx(water, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    yy, xx = np.ogrid[:height, :width]
    angles = np.linspace(0, 2 * np.pi, 72, endpoint=False)
    results = []
    for cx, cy, radius in circles[0][:12]:
        inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= (radius * 0.68) ** 2
        eligible = (inside & (water > 0)).astype(np.uint8)
        if np.count_nonzero(eligible) < np.count_nonzero(inside) * .09:
            continue
        # Kruh může být zčásti skrytý pěnou. Hodnotíme pozorovatelnou část obvodu.
        arc_scores = []
        for offset in (-3, 0, 3):
            xs = np.rint(cx + (radius + offset) * np.cos(angles)).astype(int)
            ys = np.rint(cy + (radius + offset) * np.sin(angles)).astype(int)
            valid = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
            if np.count_nonzero(valid) < 24:
                continue
            pixels = hsv[ys[valid], xs[valid]]
            arc_scores.append(np.count_nonzero((pixels[:, 1] <= 66) & (pixels[:, 2] >= 193)) / len(pixels))
        score = max(arc_scores, default=0)
        if score < .33:
            continue
        distance = cv2.distanceTransform(eligible, cv2.DIST_L2, 3)
        _, deep, _, (x, y) = cv2.minMaxLoc(distance)
        if deep >= max(5, radius * .08):
            if avoid:
                ax, ay = avoid[0] * width, avoid[1] * height
                nearby = (xx - ax) ** 2 + (yy - ay) ** 2 < (radius * .17) ** 2
                other = distance.copy()
                other[nearby] = 0
                _, strength, _, alternate = cv2.minMaxLoc(other)
                if strength >= max(5, radius * .08):
                    distance = other
                    deep, (x, y) = strength, alternate
            if preferred:
                sx = min(width-1, max(0, round(preferred[0]*width)))
                sy = min(height-1, max(0, round(preferred[1]*height)))
                if distance[sy,sx] >= max(5, radius*.08):
                    results.append((score+.15, float(distance[sy,sx]), sx, sy))
                    continue
            results.append((score, deep, x, y))
    if not results:
        return None
    _, _, x, y = max(results)
    return x / width, y / height


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

