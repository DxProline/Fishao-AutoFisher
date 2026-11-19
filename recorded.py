from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import cv2
import numpy as np

from window import Image


@dataclass(frozen=True, slots=True)
class SavedFrame:
    index: int
    stamp: int
    path: Path

    def load(self) -> Image:
        data = np.frombuffer(self.path.read_bytes(), np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Nelze přečíst snímek {self.path.name}.")
        return image


def read_rows(path: Path, optional: bool = False) -> Iterator[dict[str, Any]]:
    if optional and not path.exists():
        return
    with path.open(encoding="utf-8-sig") as stream:
        for i, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict) or type(data["t_ns"]) is not int or data["t_ns"] < 0:
                    raise ValueError("Neplatná časová značka.")
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path.name}, řádek {i}: {exc}") from exc
            yield data


class Recording:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve(strict=True)
        self.frames = []
        for row in read_rows(self.directory / "frames.jsonl"):
            path = (self.directory / row["file"]).resolve()
            if not path.is_relative_to(self.directory) or not path.is_file():
                raise ValueError(f"Chybí očekávaný snímek v relaci: {row['file']}")
            self.frames.append(SavedFrame(row["frame"], row["t_ns"], path))
        self.times = [frame.stamp for frame in self.frames]
        if not self.frames or any(a >= b for a, b in zip(self.times, self.times[1:])):
            raise ValueError("Relace nemá snímky nebo index nemá rostoucí časy.")

    def nearest(self, stamp: int, tolerance: int) -> SavedFrame | None:
        index = bisect_left(self.times, stamp)
        candidates = self.frames[max(0, index - 1) : index + 1]
        candidate = min(candidates, key=lambda item: abs(item.stamp - stamp))
        return candidate if abs(candidate.stamp - stamp) <= tolerance else None

    def events(self) -> list[dict[str, Any]]:
        events = []
        for name in ("events", "inputs", "markers"):
            events.extend(read_rows(self.directory / f"{name}.jsonl", optional=True))
        return sorted(events, key=lambda event: event["t_ns"])

