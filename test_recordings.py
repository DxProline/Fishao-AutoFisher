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
from spots import casting_hover, casting_spot, casting_target, shore_approach, timed_farm
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
        def dialog(position: float, partial: bool = False) -> np.ndarray:
            image = np.full((580, 740, 3), (155, 218, 186), np.uint8)
            x, y, width, height = 280, 403, 284, 31
            color = np.zeros((height, width, 3), np.uint8)
            color[:, :, 0] = np.minimum(np.linspace(0, 61, width), np.linspace(61, 0, width))
            color[:, :, 0] *= 2
            color[:, :, 1:] = 251
            image[y : y + height, x : x + width] = cv2.cvtColor(color, cv2.COLOR_HSV2BGR)
            center = x + round(position * width)
            rows = ((y, y + 9),) if partial else ((y, y + 9), (y + height - 1, y + height - 10))
            for edge, inset in rows:
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
        self.assertEqual(policy.choose(detector.analyze(dialog(.64, partial=True))), "left")
        self.assertIsNotNone(detector.analyze(dialog(.64)))
        partial = detector.analyze_crop(dialog(.70, partial=True), 0, 0)
        self.assertIsNotNone(partial)
        self.assertEqual(policy.choose(partial), "left")

    def test_test_cast_requires_circle_and_water(self) -> None:
        scene = np.full((550, 680, 3), (174, 222, 237), np.uint8)
        scene[:230] = (207, 206, 63)
        cv2.ellipse(scene, (305, 227), (14, 13), 0, 0, 360, (170, 50, 105), -1)
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

    def test_sizes_changes_and_land_for_each_rod(self) -> None:
        center = (380, 277)
        previous = None
        for radius in (40, 85, 166, 270, 360):
            with self.subTest(radius=radius):
                scene = np.full((550, 760, 3), (24, 160, 65), np.uint8)
                scene[:, :370] = (207, 134, 20)
                cv2.ellipse(scene, (380, 250), (14, 13), 0, 0, 360, (170, 50, 105), -1)
                self.assertIsNone(casting_target(scene))
                cv2.circle(scene, center, radius, (225, 230, 235), 3, cv2.LINE_AA)
                selection = casting_target(scene, preferred=(.58, .50))
                self.assertIsNotNone(selection)
                x, y = selection.point[0] * 760, selection.point[1] * 550
                self.assertLess(x, 370)
                self.assertLess(np.hypot(x - center[0], y - center[1]), .86 * radius)
                self.assertAlmostEqual(selection.radius * 760, radius, delta=4)
                self.assertFalse(selection.agrees(previous))
                self.assertTrue(selection.agrees(selection))
                previous = selection

    def test_cast_uses_opposite_arcs_of_a_clipped_rod_circle(self) -> None:
        width, height = 900, 485
        center = (450, 242)
        yy, xx = np.ogrid[:height, :width]
        water = xx > 454 - .9 * (yy - center[1])
        scene = np.full((height, width, 3), (12, 155, 98), np.uint8)
        scene[water] = (211, 135, 9)
        cv2.ellipse(scene, (450, 222), (11, 10), 0, 0, 360, (158, 37, 97), -1, cv2.LINE_AA)
        self.assertIsNone(casting_target(scene))

        for radius in (355, 425):
            with self.subTest(radius=radius):
                rim = np.zeros((height, width), np.uint8)
                cv2.circle(rim, center, radius, 255, 2, cv2.LINE_AA)
                if radius == 355:
                    rim[center[1]:] = 0
                alpha = .10 * rim[:, :, None] / 255
                frame = np.rint(scene * (1 - alpha) + 255 * alpha).astype(np.uint8)
                target = casting_target(frame)
                self.assertIsNotNone(target)
                x, y = round(target.point[0] * width), round(target.point[1] * height)
                self.assertTrue(water[y, x])
                self.assertLess(np.hypot(x - center[0], y - center[1]), .86 * radius)
                self.assertAlmostEqual(target.radius * width, radius, delta=4)

                frame[:, :center[0]] = scene[:, :center[0]]
                self.assertIsNone(casting_target(frame))

    def test_large_circle_survives_partial_hat_detection(self) -> None:
        width, height = 900, 485
        center = (455, 117)
        yy, xx = np.ogrid[:height, :width]
        water = xx > 468 - .85 * (yy - center[1])
        scene = np.full((height, width, 3), (16, 160, 78), np.uint8)
        scene[water] = (216, 135, 8)
        cv2.circle(scene, (460, 86), 6, (158, 37, 97), -1)
        self.assertIsNone(casting_target(scene))
        rim = np.zeros((height, width), np.uint8)
        cv2.circle(rim, center, 356, 255, 2, cv2.LINE_AA)
        alpha = .08 * rim[:, :, None] / 255
        frame = np.rint(scene * (1 - alpha) + 255 * alpha).astype(np.uint8)
        target = casting_target(frame)
        self.assertIsNotNone(target)
        x, y = round(target.point[0] * width), round(target.point[1] * height)
        self.assertTrue(water[y, x])
        self.assertAlmostEqual(target.radius * width, 356, delta=4)

    def test_pier_uses_only_the_nearest_side_of_the_water(self) -> None:
        scene = np.full((550, 760, 3), (207, 134, 20), np.uint8)
        scene[:, 308:414] = (55, 118, 185)
        cv2.ellipse(scene, (402, 249), (14, 13), 0, 0, 360, (170, 50, 105), -1)
        cv2.circle(scene, (402, 277), 180, (225, 230, 235), 3, cv2.LINE_AA)
        target = casting_target(scene)
        self.assertIsNotNone(target)
        x, y = target.point[0] * 760, target.point[1] * 550
        self.assertGreater(x, 414)
        self.assertLess(np.hypot(x - 402, y - 277), .86 * 180)

    def test_blueshifted_sand_does_not_pass_as_water(self) -> None:
        scene = np.full((550, 680, 3), (106, 67, 220), np.uint8)
        scene[:245] = (100, 117, 209)
        scene = cv2.cvtColor(scene, cv2.COLOR_HSV2BGR)
        cv2.circle(scene, (305, 254), 166, (218, 224, 229), 3, cv2.LINE_AA)
        self.assertIsNone(casting_target(scene))
        cv2.ellipse(scene, (305, 227), (14, 13), 0, 0, 360, (170, 50, 105), -1)
        point = casting_spot(scene, preferred=(305 / 680, 365 / 550))
        self.assertIsNotNone(point)
        self.assertLess(point[1] * 550, 245)

    def test_shallow_water_requires_connection_to_deep_water(self) -> None:
        scene = np.full((550, 680, 3), (31, 40, 237), np.uint8)
        scene[:140] = (86, 83, 235)
        scene[140:247] = (58, 32, 230)
        scene[350:490, 230:360] = (58, 32, 230)
        scene = cv2.cvtColor(scene, cv2.COLOR_HSV2BGR)
        cv2.ellipse(scene, (305, 224), (15, 14), 0, 0, 360, (170, 50, 105), -1)
        self.assertIsNone(casting_target(scene))
        hover = casting_hover(scene)
        self.assertIsNotNone(hover)
        self.assertLess(hover[1] * 550, 247)
        cv2.circle(scene, (305, 254), 152, (248, 250, 250), 3, cv2.LINE_AA)
        target = casting_target(scene, preferred=(.44, .70))
        self.assertIsNotNone(target)
        self.assertLess(target.point[1] * 550, 247)

    def test_narrow_river_reroutes_rejected_casts_in_water(self) -> None:
        scene = np.full((550, 680, 3), (24, 160, 65), np.uint8)
        scene[155:225] = (207, 134, 20)
        cv2.ellipse(scene, (305, 232), (15, 14), 0, 0, 360, (170, 50, 105), -1)
        cv2.circle(scene, (305, 260), 145, (245, 248, 250), 3, cv2.LINE_AA)
        first = casting_target(scene)
        self.assertIsNotNone(first)
        other = casting_target(scene, preferred=first.point, rejected=(first.point,))
        self.assertIsNotNone(other)
        self.assertNotEqual(first.point, other.point)
        for target in (first, other):
            self.assertTrue(155 < target.point[1] * 550 < 225)
            self.assertLess(np.hypot(target.point[0] * 680 - 305, target.point[1] * 550 - 260), 145)

    def test_approach_stops_on_land_and_does_not_walk_when_at_shore(self) -> None:
        scene = np.full((550, 680, 3), (24, 160, 65), np.uint8)
        scene[:200] = (207, 134, 20)
        cv2.ellipse(scene, (305, 269), (15, 14), 0, 0, 360, (170, 50, 105), -1)
        step = shore_approach(scene)
        self.assertIsNotNone(step)
        self.assertGreater(step[1] * 550, 200)
        self.assertLess(step[1] * 550, 270)
        scene[200:] = (24, 160, 65)
        cv2.ellipse(scene, (305, 165), (15, 14), 0, 0, 360, (170, 50, 105), -1)
        self.assertIsNone(shore_approach(scene))

    def test_olive_pond_requires_casting_rim_and_rejects_textured_ground(self) -> None:
        height, width = 485, 900
        pond_end = 235
        center, radius = (450, 240), 147
        hsv = np.full((height, width, 3), (41, 248, 167), np.uint8)
        hsv[:pond_end, 240:675] = (28, 150, 130)
        hsv[300:, :, :2] = (28, 150)
        hsv[300:, ::2, 2] = 109
        hsv[300:, 1::2, 2] = 171
        hsv[:, :120] = hsv[:, 795:] = 0
        scene = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        cv2.ellipse(scene, (450, 212), (14, 13), 0, 0, 360, (170, 50, 105), -1)
        wet = casting_hover(scene)
        self.assertIsNotNone(wet)
        self.assertLess(wet[1] * height, pond_end)
        with patch("spots._circles", return_value=[(.8, *center, radius)]):
            self.assertIsNone(casting_target(scene))
        cv2.circle(scene, center, radius, (88, 167, 170), 3, cv2.LINE_AA)
        target = casting_target(scene, preferred=(.50, .76))
        self.assertIsNotNone(target)
        x, y = target.point[0] * width, target.point[1] * height
        self.assertLess(y, pond_end)
        self.assertLess(np.hypot(x - center[0], y - center[1]), radius)

    def test_turquoise_river_edge_is_reachable_with_short_rod(self) -> None:
        height, width = 485, 900
        river_start, river_end = 490, 745
        hsv = np.full((height, width, 3), (46, 213, 130), np.uint8)
        hsv[:, river_start:river_end] = (89, 211, 155)
        hsv[:, river_start:535] = (70, 160, 207)
        hsv[225:305, 260:364] = (70, 160, 207)
        frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        center, radius = (483, 260), 65
        cv2.ellipse(frame, (483, 232), (14, 13), 0, 0, 360, (170, 50, 105), -1)
        hover = casting_hover(frame)
        self.assertIsNotNone(hover)
        self.assertGreater(hover[0] * width, river_start)
        self.assertLess(hover[0] * width, 535)
        self.assertIsNone(casting_target(frame))

        cv2.circle(frame, center, radius, (235, 245, 250), 3, cv2.LINE_AA)
        target = casting_target(frame, preferred=(.36, .54))
        self.assertIsNotNone(target)
        x, y = target.point[0] * width, target.point[1] * height
        self.assertGreater(x, river_start)
        self.assertLess(x, 535)
        self.assertLess(np.hypot(x - center[0], y - center[1]), radius)

    def test_paid_farm_water_requires_fenced_layout(self) -> None:
        hsv = np.full((681, 1264, 3), (54, 211, 160), np.uint8)
        hsv[:, :150, :] = hsv[:, 1104:, :] = 0
        hsv[:340, 360:880] = (29, 150, 133)
        hsv[117:139, 171:305] = (61, 170, 170)
        hsv[143:155, 155:320] = (28, 71, 230)
        scene = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        cv2.ellipse(scene, (680, 340), (18, 15), 0, 0, 360, (170, 50, 105), -1)
        cv2.circle(scene, (680, 370), 164, (245, 248, 250), 3, cv2.LINE_AA)
        self.assertEqual(timed_farm(scene), (True, True))
        target = casting_target(scene)
        self.assertIsNotNone(target)
        self.assertLess(target.point[1] * 681, 340)
        scene[:, :150] = (24, 160, 65)
        self.assertEqual(timed_farm(scene), (False, False))
        self.assertIsNone(casting_target(scene))

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
            recorder = Recorder(self.root / "capture", 60)
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
        self.assertEqual(recording.frames[0].path.suffix, ".jpg")
        self.assertEqual(recording.frames[0].load().shape, base.shape)
        self.assertEqual([e["key"] for e in recording.events() if e["type"] == "key_down"], ["Space"])


if __name__ == "__main__":
    unittest.main()
