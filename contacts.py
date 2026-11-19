from __future__ import annotations

import argparse
import html
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from recorded import Recording
from window import Image

NS = 1_000_000_000
OFFSETS = (-0.40, -0.20, 0.0, 0.20, 0.40)
ROWS = 4
TILE_W = 256
TILE_H = 166
ROW_H = TILE_H + 40


def label(event: dict[str, Any]) -> str:
    kind = str(event.get("type", "událost"))
    if kind in {"key_down", "key_up"}:
        return f"{kind} {event.get('key', '')}".strip()
    if kind in {"mouse_down", "mouse_up"}:
        return f"{kind} {event.get('button', '')} ({event.get('x')}, {event.get('y')})"
    detail = event.get("label") or event.get("message") or ""
    return f"{kind} {detail}".strip()


def text(image: Image, value: str, x: int, y: int, scale: float = 0.48) -> None:
    latin = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cv2.putText(image, latin, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (235, 241, 245), 1, cv2.LINE_AA)


def resize_tile(image: Image) -> tuple[Image, tuple[int, int, int, int]]:
    height, width = image.shape[:2]
    scale = min((TILE_W - 12) / width, (TILE_H - 28) / height)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    return cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA), ((TILE_W - w) // 2, (TILE_H - 22 - h) // 2, w, h)


def contact_sheets(recording: Recording, destination: Path) -> int:
    events = recording.events()
    if not events:
        raise ValueError("Relace neobsahuje události pro kontaktní list.")
    destination.mkdir(parents=True, exist_ok=False)
    load_tile = lru_cache(maxsize=48)(lambda frame: resize_tile(frame.load()))
    page_links = []
    for start in range(0, len(events), ROWS):
        subset = events[start : start + ROWS]
        sheet = np.full((ROW_H * len(subset) + 52, TILE_W * len(OFFSETS), 3), (32, 24, 18), np.uint8)
        text(sheet, "FISHAO | snimky kolem udalosti", 12, 30, 0.72)
        entries = []
        for row_index, event in enumerate(subset):
            y0 = row_index * ROW_H + 52
            stamp = event["t_ns"]
            title = f"{start + row_index + 1:04d}   {stamp / NS:8.3f} s   {label(event)}"
            text(sheet, title, 12, y0 + 22)
            entries.append("<li>" + html.escape(title) + "</li>")
            for column, offset in enumerate(OFFSETS):
                x0 = column * TILE_W
                y1 = y0 + 32
                canvas = sheet[y1 : y1 + TILE_H, x0 : x0 + TILE_W]
                canvas[1:-1, 1:-1] = (65, 47, 35) if offset == 0 else (50, 38, 29)
                frame = recording.nearest(stamp + round(offset * NS), 160_000_000)
                if frame is None:
                    text(canvas, "Bez snimku", 12, 27)
                    continue
                tile, (x, y, w, h) = load_tile(frame)
                canvas[y : y + h, x : x + w] = tile
                if offset == 0 and event.get("type") in {"mouse_down", "mouse_up"}:
                    mx = x + min(w - 1, max(0, round(float(event["x_ratio"]) * w)))
                    my = y + min(h - 1, max(0, round(float(event["y_ratio"]) * h)))
                    cv2.drawMarker(canvas, (mx, my), (26, 180, 255), cv2.MARKER_CROSS, 18, 2, cv2.LINE_AA)
                text(canvas, f"{(frame.stamp - stamp) / NS:+.3f} s   #{frame.index}", 12, TILE_H - 7, 0.4)
        page = f"udalosti_{len(page_links) + 1:04d}.jpg"
        ok, data = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise OSError("Kontaktní list se nepodařilo zakódovat.")
        (destination / page).write_bytes(data.tobytes())
        page_links.append(f'<section><h2><a href="{page}">Strana {len(page_links) + 1}</a></h2>'
                          f'<img loading="lazy" src="{page}" alt="Kontaktní list"><ul>{"".join(entries)}</ul></section>')
    (destination / "index.html").write_text(
        '<!doctype html><html lang="cs"><meta charset="utf-8"><title>Fishao · kontaktní list</title>'
        '<style>body{font:16px system-ui;background:#141e27;color:#eef5fa;margin:2rem}'
        'a{color:#6de8d0}img{max-width:100%;height:auto}section{margin-bottom:2rem}</style>'
        '<h1>Fishao · kontaktní list</h1>' + "".join(page_links) + "</html>", encoding="utf-8",
    )
    return len(page_links)


def main() -> None:
    parser = argparse.ArgumentParser(description="Vytvoří kontaktní list snímků kolem každé události.")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        recording = Recording(args.recording)
        destination = args.output_dir or recording.directory / "kontakty"
        pages = contact_sheets(recording, destination)
    except (KeyError, OSError, ValueError) as exc:
        parser.exit(1, f"Kontaktní list se nepodařilo vytvořit. {exc}\n")
    print(f"Stran: {pages}. Přehled: {destination / 'index.html'}")


if __name__ == "__main__":
    main()

