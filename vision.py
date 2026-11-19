from __future__ import annotations

import cv2
import numpy as np

from spots import water_candidates
from window import Image

Box = tuple[float, float, float, float]


def intersection(a: Box, b: Box) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    shared = max(0.0, right - left) * max(0.0, bottom - top)
    union = a[2] * a[3] + b[2] * b[3] - shared
    return shared / union if union else 0.0


class VisualDetector:
    """Měří změny scény a sleduje oblasti pohybu v poměrových souřadnicích."""

    def __init__(self) -> None:
        self.previous: Image | None = None
        self.background: np.ndarray | None = None
        self.previous_stamp: int | None = None
        self.tracks: dict[int, tuple[Box, int]] = {}
        self.next_track = 1
        self.spots: list[dict[str, object]] = []
        self.last_spots: int | None = None

    @staticmethod
    def _change_mask(a: Image, b: Image) -> Image:
        difference = cv2.absdiff(a, b).max(axis=2)
        mask = cv2.inRange(difference, 23, 255)
        kernel = np.ones((5, 5), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    def analyze(self, image: Image, stamp: int) -> dict[str, object]:
        normalized = cv2.resize(image, (480, 270), interpolation=cv2.INTER_AREA)
        smooth = cv2.GaussianBlur(normalized, (5, 5), 0)
        height, width = smooth.shape[:2]
        interrupted = self.previous_stamp is None or stamp - self.previous_stamp > 500_000_000
        if interrupted or self.last_spots is None or stamp - self.last_spots >= 500_000_000:
            self.spots = water_candidates(normalized)
            self.last_spots = stamp
        regions: list[dict[str, object]] = []
        appeared: list[list[float]] = []
        changed = 0.0
        state = "initial_frame" if self.previous is None else "gap"
        if self.previous is not None and not interrupted:
            mask = self._change_mask(smooth, self.previous)
            changed = cv2.countNonZero(mask) / (width * height)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            boxes = []
            for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
                if cv2.contourArea(contour) < 45:
                    break
                x, y, w, h = cv2.boundingRect(contour)
                boxes.append((x / width, y / height, w / width, h / height))
            state = "scene_change" if changed >= 0.25 else "local_change" if boxes else "stable"
            if state == "scene_change":
                self.tracks.clear()
                regions = [{"box": [round(n, 4) for n in box]} for box in boxes]
            else:
                regions = self._track(boxes)
        else:
            self.tracks.clear()
        if self.background is None or interrupted or state == "scene_change":
            self.background = smooth.astype(np.float32)
        else:
            # Druhé srovnání drží několik sekund i oblast, která se právě objevila a stojí.
            residual = self._change_mask(smooth, cv2.convertScaleAbs(self.background))
            contours, _ = cv2.findContours(residual, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
                if cv2.contourArea(contour) < width * height * 0.008:
                    break
                x, y, w, h = cv2.boundingRect(contour)
                appeared.append([round(x / width, 4), round(y / height, 4), round(w / width, 4), round(h / height, 4)])
            elapsed = (stamp - self.previous_stamp) / 1_000_000_000
            cv2.accumulateWeighted(smooth, self.background, min(0.25, elapsed / 2.5))
        self.previous = smooth
        self.previous_stamp = stamp
        return {"visual_state": state, "changed_ratio": round(changed, 5),
                "regions": regions, "appeared_regions": appeared, "water_candidates": self.spots}

    def _track(self, boxes: list[Box]) -> list[dict[str, object]]:
        remaining = dict(self.tracks)
        current = {}
        regions = []
        for box in boxes:
            center_x = box[0] + box[2] / 2
            center_y = box[1] + box[3] / 2
            matches = []
            for identity, (prior, count) in remaining.items():
                iou = intersection(prior, box)
                dx = center_x - (prior[0] + prior[2] / 2)
                dy = center_y - (prior[1] + prior[3] / 2)
                distance = (dx * dx + dy * dy) ** 0.5
                if iou >= 0.16 or distance <= 0.032:
                    matches.append((iou + 0.05 - min(distance, 0.05), identity, count, dx, dy))
            if matches:
                _, identity, age, dx, dy = max(matches)
                remaining.pop(identity)
                movement = [round(dx, 4), round(dy, 4)]
            else:
                identity, age, movement = self.next_track, 0, None
                self.next_track += 1
            current[identity] = (box, age + 1)
            regions.append({
                "track": identity, "age": age + 1, "box": [round(x, 4) for x in box],
                "center": [round(center_x, 4), round(center_y, 4)], "delta": movement,
            })
        self.tracks = current
        return regions

