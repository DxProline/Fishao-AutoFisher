from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from contacts import contact_sheets
from minigame import BalanceDetector, BalancePolicy
from recorded import Recording
from recorder import Recorder
from replay import replay
from spots import casting_spot
from vision import VisualDetector
from window import Region

STEP = 66_666_667


class RecordedWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "frames").mkdir()
        self.base = np.full((360, 640, 3), (32, 120, 37), np.uint8)
        self.base[:, 300:] = (225, 140, 30)

    def make_recording(self) -> Recording:
        frames = []
        for i in range(20):
            image = self.base.copy()
            if 6 <= i <= 12:
                x = 100 + 5 * (i - 6)
                cv2.rectangle(image, (x, 80), (x + 85, 205), (220, 238, 250), -1)
            if i == 8:
                image = cv2.resize(image, (960, 540))
            path = self.root / "frames" / f"{i}.png"
            path.write_bytes(cv2.imencode(".png", image)[1].tobytes())
            frames.append({"file": f"frames/{path.name}", "frame": i, "t_ns": i * STEP})
        clicks = [
            {"t_ns": 6 * STEP - 14_000_000, "type": "mouse_down", "button": "left",
             "x": 123, "y": 100, "x_ratio": 123 / 640, "y_ratio": 100 / 360},
            {"t_ns": 7 * STEP, "type": "mouse_up", "button": "left",
             "x": 123, "y": 100, "x_ratio": 123 / 640, "y_ratio": 100 / 360},
            {"t_ns": 21 * STEP, "type": "key_up", "key": "SPACE"},
        ]
        self.rows("frames", frames)
        self.rows("inputs", clicks)
        self.rows("events", [{"t_ns": 0, "type": "recording"}, {"t_ns": 22 * STEP, "type": "capture_ended"}])
        return Recording(self.root)

    def rows(self, name: str, data: list[dict]) -> None:
        (self.root / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in data), encoding="utf-8")

    def test_replay_and_contacts(self) -> None:
        recording = self.make_recording()
        stream = io.StringIO()
        replay(recording, stream)
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(rows[0]["visual_state"], "initial_frame")
        self.assertEqual(rows[5]["visual_state"], "stable")
        self.assertEqual(rows[6]["visual_state"], "local_change")
        self.assertTrue(rows[6]["regions"])
        self.assertTrue(rows[6]["appeared_regions"])
        self.assertTrue(rows[6]["water_candidates"])
        self.assertEqual(rows[6]["events"][0]["type"], "mouse_down")
        self.assertEqual(rows[6]["decision"], "observe")
        self.assertIn("event_after_last_frame", rows[-1])
        self.assertEqual(contact_sheets(recording, self.root / "contacts"), 2)
        html = (self.root / "contacts" / "index.html").read_text(encoding="utf-8")
        self.assertIn("mouse_down", html)
        self.assertIn("udalosti_0002.jpg", html)

    def test_gap_and_resize(self) -> None:
        detector = VisualDetector()
        self.assertEqual(detector.analyze(self.base, 0)["visual_state"], "initial_frame")
        resized = cv2.resize(self.base, (960, 540))
        self.assertEqual(detector.analyze(resized, STEP)["visual_state"], "stable")
        self.assertEqual(detector.analyze(resized, 8 * STEP + STEP)["visual_state"], "gap")
        self.assertEqual(detector.analyze(255 - resized, 10 * STEP)["visual_state"], "scene_change")

    def test_balance_holds_opposite_direction_and_releases(self) -> None:
        def dialog(position: float) -> np.ndarray:
            image = np.full((580, 740, 3), (155, 218, 186), np.uint8)
            x, y, width, height = 280, 403, 284, 31
            color = np.zeros((height, width, 3), np.uint8)
            color[:, :, 0] = np.minimum(np.linspace(0, 61, width), np.linspace(61, 0, width))
            color[:, :, 0] *= 2
            color[:, :, 1:] = 251
            image[y : y + height, x : x + width] = cv2.cvtColor(color, cv2.COLOR_HSV2BGR)
            center = x + round(position * width)
            for edge, inset in ((y, y + 9), (y + height - 1, y + height - 10)):
                points = np.array([(center - 5, edge), (center + 5, edge), (center, inset)], np.int32)
                cv2.fillConvexPoly(image, points, (210, 98, 12))
            return image

        detector = BalanceDetector()
        policy = BalancePolicy()
        for fraction, direction in ((.35, "right"), (.72, "left"), (.54, "left"), (.50, None)):
            frame = dialog(fraction)
            result = detector.analyze(frame)
            self.assertIsNotNone(result)
            self.assertAlmostEqual(result.indicator, fraction, delta=.015)
            self.assertEqual(policy.choose(result), direction)
        resized = cv2.resize(dialog(.30), (1332, 1044))
        self.assertEqual(policy.choose(detector.analyze(resized)), "right")
        self.assertIsNone(policy.choose(detector.analyze(self.base)))

    def test_test_cast_requires_circle_and_water(self) -> None:
        scene = np.full((550, 680, 3), (174, 222, 237), np.uint8)
        scene[:230] = (207, 206, 63)
        self.assertIsNone(casting_spot(scene))
        cv2.circle(scene, (305, 254), 166, (241, 247, 249), 4, cv2.LINE_AA)
        point = casting_spot(scene)
        self.assertIsNotNone(point)
        px, py = point[0] * 680, point[1] * 550
        self.assertLess(py, 230)
        self.assertLess((px - 305) ** 2 + (py - 254) ** 2, 166**2)
        alternate = casting_spot(scene, avoid=point)
        self.assertIsNotNone(alternate)
        self.assertNotEqual(alternate, point)
        ax, ay = alternate[0] * 680, alternate[1] * 550
        self.assertLess(ay, 230)
        self.assertLess((ax - 305) ** 2 + (ay - 254) ** 2, 166**2)

    def test_write_inputs_and_frames(self) -> None:
        base = self.base

        class Capture:
            def __init__(self, title): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def grab(self): return base, Region(1, 0, 0, 640, 360)

        class Input:
            error = None
            def __init__(self, title, enabled, emit):
                self.emit = emit
            def start(self):
                self.emit(time.perf_counter_ns(), {"type": "key_down", "vk": 32, "key": "Space"})
            def close(self): pass
            def join(self): pass

        with patch("recorder.WindowCapture", Capture), patch("recorder.GameInput", Input):
            recorder = Recorder(self.root / "capture", 15)
            recorder.start()
            until = time.monotonic() + 3
            while recorder.snapshot().saved < 3 and not recorder.snapshot().finished and time.monotonic() < until:
                time.sleep(.02)
            recorder.stop()
            recorder.join(3)
        status = recorder.snapshot()
        self.assertTrue(status.finished)
        self.assertFalse(status.error, status.text)
        self.assertGreaterEqual(status.saved, 3)
        self.assertEqual(status.inputs, 1)
        recording = Recording(status.folder)
        self.assertEqual([e["key"] for e in recording.events() if e["type"] == "key_down"], ["Space"])


if __name__ == "__main__":
    unittest.main()

