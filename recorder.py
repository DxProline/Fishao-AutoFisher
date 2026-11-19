from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns
from typing import TextIO

import cv2

from game_input import GameInput
from window import FrameUnavailable, Image, Region, WindowCapture

TITLE = "FISHAO - Reborn"
NS = 1_000_000_000
MIN_FREE_BYTES = 256 * 1024 * 1024
BUFFER_FRAMES = 8


@dataclass(frozen=True, slots=True)
class Frame:
    index: int
    offset_ns: int
    region: Region
    image: Image


@dataclass(frozen=True, slots=True)
class Status:
    text: str = "Čekám na spuštění."
    folder: Path | None = None
    captured: int = 0
    saved: int = 0
    dropped: int = 0
    missed: int = 0
    inputs: int = 0
    casts: int = 0
    caught: int = 0
    escaped: int = 0
    input_warning: str | None = None
    active_ns: int = 0
    elapsed_ns: int = 0
    finished: bool = False
    error: bool = False
    preview: tuple[int, bytes] | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def row(stream: TextIO, data: dict[str, object]) -> None:
    stream.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


class Recorder(threading.Thread):
    def __init__(self, output_dir: Path, fps: int, title: str = TITLE) -> None:
        super().__init__(name="FishaoRecorder", daemon=True)
        self.output_dir = output_dir
        self.fps = fps
        self.title = title
        self._state = Status()
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._paused = threading.Event()
        self._writing_done = threading.Event()
        self._writer_error: Exception | None = None
        self._frames: queue.Queue[Frame] = queue.Queue(maxsize=BUFFER_FRAMES)
        self._inputs: queue.Queue[tuple[int, dict[str, object]]] = queue.Queue(maxsize=2048)
        self._lost_inputs = 0
        self._log = logging.getLogger("fishao")

    @property
    def stopping(self) -> bool:
        return self._stop_requested.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def snapshot(self) -> Status:
        with self._lock:
            return self._state

    def _update(self, **changes: object) -> None:
        with self._lock:
            self._state = replace(self._state, **changes)

    def toggle_pause(self) -> None:
        if self.stopping:
            return
        if self.paused:
            self._paused.clear()
        else:
            self._paused.set()

    def stop(self) -> None:
        self._stop_requested.set()

    def _enqueue_input(self, stamp: int, event: dict[str, object]) -> None:
        try:
            self._inputs.put_nowait((stamp, event))
        except queue.Full:
            self._lost_inputs += 1

    def _drain_inputs(self, index: TextIO, started: int) -> None:
        batch = 0
        while batch < 2048:
            try:
                stamp, event = self._inputs.get_nowait()
            except queue.Empty:
                break
            row(index, {"t_ns": stamp - started, **event})
            batch += 1
        if batch:
            self._update(inputs=self.snapshot().inputs + batch)

    def _writer(self, folder: Path) -> None:
        written = 0
        try:
            with (folder / "frames.jsonl").open("x", encoding="utf-8") as index:
                while not self._writing_done.is_set() or not self._frames.empty():
                    try:
                        frame = self._frames.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    filename = f"frames/{frame.index:08d}.png"
                    path = folder / filename
                    ok, data = cv2.imencode(".png", frame.image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    if not ok:
                        raise OSError(f"Snímek {frame.index} se nepodařilo zakódovat.")
                    temporary = path.with_suffix(".tmp")
                    temporary.write_bytes(data.tobytes())
                    temporary.replace(path)
                    row(index, {
                        "frame": frame.index, "t_ns": frame.offset_ns, "file": filename,
                        "width": frame.region.width, "height": frame.region.height,
                        "screen_x": frame.region.x, "screen_y": frame.region.y,
                    })
                    written += 1
                    self._update(saved=written)
        except Exception as exc:
            self._writer_error = exc
            self._stop_requested.set()

    def _make_folder(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        local = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        folder = self.output_dir / f"fishao_{local}"
        folder.mkdir()
        (folder / "frames").mkdir()
        return folder

    @staticmethod
    def _manifest(folder: Path, data: dict[str, object]) -> None:
        target = folder / "session.json"
        temporary = folder / "session.tmp"
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(target)

    def run(self) -> None:
        folder: Path | None = None
        writer: threading.Thread | None = None
        started = perf_counter_ns()
        metadata: dict[str, object] = {
            "schema": 2, "title": self.title, "target_fps": self.fps, "format": "png",
            "started_utc": utc_now(), "frames_index": "frames.jsonl", "events_index": "events.jsonl",
            "inputs_index": "inputs.jsonl",
            "timestamp_unit": "nanoseconds_since_session_start", "status": "recording",
        }
        error: Exception | None = None
        try:
            folder = self._make_folder()
            self._check_disk(folder)
            self._manifest(folder, metadata)
            self._update(folder=folder, text="Čekám na aktivní okno hry.")
            self._log.info("Relace zahájena: %s", folder.name)
            writer = threading.Thread(target=self._writer, args=(folder,), name="FishaoPngWriter", daemon=True)
            writer.start()
            with (folder / "events.jsonl").open("x", encoding="utf-8") as events, (
                folder / "inputs.jsonl"
            ).open("x", encoding="utf-8") as inputs:
                monitor = GameInput(self.title, lambda: not self.stopping and not self.paused, self._enqueue_input)
                monitor.start()
                try:
                    with WindowCapture(self.title) as capture:
                        self._record(capture, events, inputs, started, folder, monitor)
                finally:
                    monitor.close()
                    monitor.join()
                    self._drain_inputs(inputs, started)
                    if monitor.error:
                        if self.snapshot().input_warning is None:
                            self._log.warning("%s", monitor.error)
                            row(events, {"type": "input_unavailable", "t_ns": perf_counter_ns() - started,
                                         "message": monitor.error})
                        self._update(input_warning=monitor.error)
                    row(events, {"type": "capture_ended", "t_ns": perf_counter_ns() - started})
        except Exception as exc:
            error = exc
        finally:
            self._writing_done.set()
            if writer is not None:
                self._update(text="Dokončuji zápis pořízených snímků…")
                writer.join()
            error = self._writer_error or error
            stats = self.snapshot()
            if folder is not None:
                metadata.update({
                    "status": "error" if error else "complete",
                    "ended_utc": utc_now(),
                    "error": str(error) if error else None,
                    "captured": stats.captured,
                    "saved": stats.saved,
                    "dropped": stats.dropped,
                    "missed": stats.missed,
                    "inputs": stats.inputs,
                    "inputs_dropped": self._lost_inputs,
                    "input_warning": stats.input_warning,
                    "active_ns": stats.active_ns,
                    "elapsed_ns": stats.elapsed_ns,
                    "unsaved": stats.captured - stats.saved,
                })
                try:
                    self._manifest(folder, metadata)
                except OSError as exc:
                    error = error or exc
            if error:
                message = f"Záznam ukončen kvůli chybě. {error}"
                self._log.error("%s", message)
            else:
                message = f"Záznam uložen. Snímků: {stats.saved}."
                self._log.info("%s", message)
            self._stop_requested.set()
            self._update(text=message, finished=True, error=error is not None)

    @staticmethod
    def _check_disk(folder: Path) -> None:
        if shutil.disk_usage(folder).free < MIN_FREE_BYTES:
            raise OSError("Na cílovém disku zbývá méně než 256 MB.")

    @staticmethod
    def _preview(image: Image) -> bytes:
        h, w = image.shape[:2]
        ratio = min(1.0, 920 / w, 520 / h)
        if ratio < 1.0:
            image = cv2.resize(image, (max(1, round(w * ratio)), max(1, round(h * ratio))), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise OSError("Ladicí náhled se nepodařilo zakódovat.")
        return encoded.tobytes()

    def _record(self, capture: WindowCapture, events: TextIO, inputs: TextIO, started: int,
                folder: Path, monitor: GameInput) -> None:
        interval = NS // self.fps
        next_frame = perf_counter_ns()
        next_disk_check = next_frame + 2 * NS
        next_preview = next_frame
        state: tuple[str, str] | None = None
        active_since: int | None = None
        accumulated = 0
        captured = dropped = missed = 0
        last_size: tuple[int, int] | None = None

        def transition(mode: str, text: str, stamp: int) -> None:
            nonlocal state, accumulated, active_since
            event = (mode, text)
            if event == state:
                return
            if mode == "recording" and active_since is None:
                active_since = stamp
            elif mode != "recording" and active_since is not None:
                accumulated += stamp - active_since
                active_since = None
            state = event
            self._update(text=text)
            row(events, {"type": mode, "t_ns": stamp - started, "message": text})
            self._log.info("%s", text)

        try:
            while not self.stopping:
                now = perf_counter_ns()
                self._drain_inputs(inputs, started)
                if monitor.error and self.snapshot().input_warning is None:
                    self._update(input_warning=monitor.error)
                    self._log.warning("%s", monitor.error)
                    row(events, {"type": "input_unavailable", "t_ns": now - started, "message": monitor.error})
                if now >= next_disk_check:
                    self._check_disk(folder)
                    next_disk_check = now + 2 * NS
                if self.paused:
                    transition("paused", "Ruční pauza. Pokračuj přes F8.", now)
                    next_frame = now
                    self._stop_requested.wait(0.05)
                    continue
                if now < next_frame:
                    self._stop_requested.wait(min((next_frame - now) / NS, 0.05))
                    continue
                if active_since is not None and now > next_frame + interval:
                    late = (now - next_frame) // interval
                    missed += late
                    next_frame += late * interval
                next_frame = max(next_frame + interval, now + interval // 2)
                try:
                    image, region = capture.grab()
                except FrameUnavailable as exc:
                    stamp = perf_counter_ns()
                    transition("waiting_for_window", str(exc), stamp)
                    self._update(elapsed_ns=stamp - started, active_ns=accumulated)
                    continue
                stamp = perf_counter_ns()
                if self.stopping or self.paused:
                    continue
                transition("recording", "Nahrávám okno hry.", stamp)
                size = (region.width, region.height)
                if size != last_size:
                    last_size = size
                    row(events, {"type": "window_size", "t_ns": stamp - started, "width": size[0], "height": size[1]})
                frame = Frame(captured, stamp - started, region, image)
                captured += 1
                try:
                    self._frames.put_nowait(frame)
                except queue.Full:
                    dropped += 1
                if stamp >= next_preview:
                    self._update(preview=(frame.index, self._preview(image)))
                    next_preview = stamp + NS // 4
                active = accumulated + (stamp - active_since if active_since is not None else 0)
                self._update(captured=captured, dropped=dropped, missed=missed, elapsed_ns=stamp - started, active_ns=active)
        finally:
            end = perf_counter_ns()
            active = accumulated + (end - active_since if active_since is not None else 0)
            self._update(captured=captured, dropped=dropped, missed=missed, elapsed_ns=end - started, active_ns=active)

