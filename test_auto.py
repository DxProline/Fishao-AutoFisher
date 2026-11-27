from __future__ import annotations

import logging
import tempfile
import unittest
from itertools import count
from pathlib import Path
from queue import Empty, SimpleQueue
from unittest.mock import ANY, Mock, call, patch

import cv2
import numpy as np
import win32con

from activities import Action, Activities, Panel
from assist import PreviewClock
from bot import Auto, Cycle, Phase
from controls import BackgroundControls, Controls
from destinations import CURRENT, LAKETOWN, MAX_MAP_ATTEMPTS, Destination, Navigator, TravelSettings
from game_ui import EnergyShop, GameUI, Hud, Interface, Inventory, Match, Point, SIZE
from gui import App
from minigame import Balance, BalanceDetector, BalancePolicy
from progression import Progression
from runtime_state import BotState, EventFeed, MissionStatus, log_structured
from spots import CastTarget
from window import Region, WindowCapture, WorkstationLocked


class _FakeUi:
    """Společná testovací náhrada dodržující stejný kontrakt GameUI jako Interface."""

    def normalize(self, frame): return frame
    def find(self, image, key, part, threshold): return None
    def _find(self, image, key, part, threshold): return self.find(image, key, part, threshold)
    def number(self, patch, *, white, max_digits=3, white_floor=219): return None
    def level_ok(self, image): return None
    def mission_card_action(self, image, title): return None
    def mission_popup_action(self, image): return None
    def find_green_button(self, image, area): return None
    def result(self, image): return None
    def timed_fishing(self, image): return False
    def result_close(self, image, result): return None
    def energy_button(self, image): return None
    def energy_shop(self, image): return None
    def hud(self, image): return None
    def inventory(self, image): return None


class AutoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.auto = Auto()
        self.cycle = Cycle()
        self.region = Region(1, 0, 0, *SIZE)
        self.controls = Mock()
        self.frame = np.zeros((SIZE[1], SIZE[0], 3), np.uint8)

    def test_waits_for_energy_and_requires_two_cast_points(self) -> None:
        available = (0.9, .5)
        empty = Hud(0, 7, 12, True, available, available)
        self.auto._ready(self.cycle, empty, self.frame, 0., self.controls, self.region)
        self.assertEqual(self.cycle.phase, Phase.ENERGY)
        self.controls.cast.assert_not_called()
        good = Hud(1, 7, 12, True, available, available)
        with patch("bot.casting_target", return_value=CastTarget((.4, .2), (.4, .3), .15, .54)):
            self.auto._ready(self.cycle, good, self.frame, 2., self.controls, self.region)
            self.controls.cast.assert_not_called()
            self.auto._ready(self.cycle, good, self.frame, 3., self.controls, self.region)
        self.assertEqual(self.cycle.phase, Phase.CAST)
        self.controls.cast.assert_called_once_with(self.region, .4, .2, cancelled=ANY)

    def test_changes_equipment_only_after_inventory_and_verifies_hud(self) -> None:
        rod, bait = (.91, .5), (.91, .7)
        self.auto._ready(self.cycle, Hud(2, 11, 0, True, rod, bait), self.frame, 0., self.controls, self.region)
        self.assertEqual(self.cycle.phase, Phase.GEAR)
        self.controls.click.assert_called_once_with(self.region, *bait)
        slots = ((.4, .55), (.48, .55))
        select = (.72, .3)
        no_stock = Inventory("bait", 0, slots, select, (.8, .1), None, None)
        self.auto._gear(self.cycle, no_stock, None, 1., self.controls, self.region)
        self.controls.click.assert_called_with(self.region, *slots[0])
        self.auto._gear(self.cycle, no_stock, None, 2., self.controls, self.region)
        self.controls.click.assert_called_with(self.region, *slots[1])
        supply = Inventory("bait", 28, slots, select, (.8, .1), None, None)
        self.auto._gear(self.cycle, supply, None, 3., self.controls, self.region)
        self.controls.click.assert_called_with(self.region, *select)
        self.assertEqual(self.cycle.phase, Phase.GEAR)
        self.auto._gear(self.cycle, None, Hud(2, 11, 28, True, rod, bait), 4., self.controls, self.region)
        self.assertEqual(self.cycle.phase, Phase.READY)

    def test_changed_rod_range_requires_two_fresh_measurements(self) -> None:
        default = (.9, .5)
        hud = Hud(1, 7, 12, True, default, default)
        small = CastTarget((.4, .2), (.4, .3), .075, .54)
        large = CastTarget(small.point, small.center, .15, .54)
        with patch("bot.casting_target", side_effect=(small, large, large)):
            self.auto._ready(self.cycle, hud, self.frame, 1., self.controls, self.region)
            self.auto._ready(self.cycle, hud, self.frame, 2., self.controls, self.region)
            self.controls.cast.assert_not_called()
            self.auto._ready(self.cycle, hud, self.frame, 3., self.controls, self.region)
        self.controls.cast.assert_called_once_with(self.region, *large.point, cancelled=ANY)

    def test_cast_waits_until_pointer_is_over_confirmed_water(self) -> None:
        p = (.43, .35)
        hud = Hud(2, 7, 12, True, (.9, .5), (.9, .7))
        target = CastTarget(p, (.48, .48), .15, .54)
        self.controls.pointer_at.side_effect = (False, False, True)
        with patch("bot.casting_target", return_value=target):
            self.auto._ready(self.cycle, hud, self.frame, 1., self.controls, self.region)
            self.auto._ready(self.cycle, hud, self.frame, 1.1, self.controls, self.region)
            self.controls.cast.assert_not_called()
            self.auto._ready(self.cycle, hud, self.frame, 1.2, self.controls, self.region)
        self.assertEqual(self.controls.hover.call_count, 2)
        self.controls.cast.assert_called_once_with(self.region, *p, cancelled=ANY)

    def test_missing_circle_uses_hover_only_until_two_confirmations(self) -> None:
        p = (.43, .35)
        hud = Hud(2, 7, 12, True, (.9, .5), (.9, .7))
        target = CastTarget(p, (.48, .48), .15, .54)
        with patch("bot.casting_target", side_effect=(None, None, target, target)), patch("bot.casting_hover", return_value=p):
            for stamp in (1., 1.5, 2.1):
                self.auto._ready(self.cycle, hud, self.frame, stamp, self.controls, self.region)
                self.controls.cast.assert_not_called()
            self.auto._ready(self.cycle, hud, self.frame, 2.2, self.controls, self.region)
        self.controls.hover.assert_called_once_with(self.region, *p)
        self.controls.cast.assert_called_once_with(self.region, *p, cancelled=ANY)

    def test_cast_sends_two_mouse_presses_in_both_input_modes(self) -> None:
        region, point = self.region, (.43, .35)
        with patch("controls.sleep") as delay:
            foreground = Controls()
            with (
                patch("controls.win32gui.GetForegroundWindow", return_value=region.hwnd),
                patch.object(foreground, "_pointer_move", return_value=object()),
                patch.object(foreground, "pointer_at", return_value=True),
                patch.object(foreground, "_inputs") as inputs,
            ):
                self.assertTrue(foreground.cast(region, *point, cancelled=lambda: False))
                flags = [entry.args[0].value.mouse.flags for entry in inputs.call_args_list]
                self.assertEqual(flags, [2, 4, 2, 4])
            self.assertEqual(delay.call_args_list, [call(.055), call(.035), call(.055)])

            background = BackgroundControls("FISHAO - Reborn")
            packed = 275 | (190 << 16)
            with patch.object(background, "_point", return_value=packed), patch.object(background, "_post") as post:
                self.assertTrue(background.cast(region, *point, cancelled=lambda: False))
                self.assertEqual(post.call_args_list, [
                    call(region.hwnd, win32con.WM_MOUSEMOVE, 0, packed),
                    call(region.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, packed),
                    call(region.hwnd, win32con.WM_LBUTTONUP, 0, packed),
                ] * 2)

    def test_cast_keeps_first_attempt_when_second_press_is_cancelled(self) -> None:
        controls = Controls()
        with patch("controls.sleep"), patch.object(controls, "click", return_value=True) as click:
            self.assertTrue(controls.cast(self.region, .43, .35, cancelled=Mock(side_effect=(False, True))))
            click.assert_called_once_with(self.region, .43, .35)

    def test_result_matching_at_different_sizes(self) -> None:
        ref = Interface()
        for label, (x, y) in {"caught": (646, 116), "escaped": (506, 263)}.items():
            frame = self.frame.copy()
            sample = ref.templates[label]
            h, w = sample.shape[:2]
            frame[y:y+h, x:x+w] = sample
            self.assertEqual(ref.result(frame), label)
            resized = cv2.resize(frame, (900, 485), interpolation=cv2.INTER_AREA)
            self.assertEqual(ref.result(ref.normalize(resized)), label)
        self.assertIsNone(ref.result(self.frame))

    def test_equipment_count_separates_touching_digits(self) -> None:
        levels = {".": 90, "+": 228, "#": 245}
        gray = np.array([[levels[pixel] for pixel in row] for row in (
            "................",
            ".....#+...####..",
            "....###..####...",
            "....#.#..##.....",
            "...#.+#..####...",
            "..+#.+#..#..##..",
            "..#+.##.....##..",
            "..######....##..",
            ".....+#.+#+##+..",
            "......#..####...",
            "................",
        )], dtype=np.uint8)
        ui = Interface()
        patch_image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        self.assertIsNone(ui.number(patch_image, white=True))
        self.assertEqual(ui._gear_count(patch_image), 45)

    def test_energy_anchor_ignores_background_behind_lightning(self) -> None:
        ui = Interface()
        frame = self.frame.copy()
        for name, x, y in (("energy", 503, 8), ("rod", 1151, 328), ("bait", 1151, 448)):
            tile = ui.templates[name]
            h, w = tile.shape[:2]
            frame[y:y+h, x:x+w] = tile
        lightning = ui.templates["energy"]
        h, w = lightning.shape[:2]
        patch = frame[8:8+h, 503:503+w]
        patch[ui.energy_mask == 0] = (53, 122, 201)
        frame[19:39, 558:601] = (225, 242, 255)
        cv2.putText(frame, "3", (569, 34), cv2.FONT_HERSHEY_SIMPLEX, .55, (35, 50, 65), 2)
        self.assertEqual(ui.hud(frame).energy, 3)
        patch[:] = (53, 122, 201)
        self.assertIsNone(ui.hud(frame))

    def test_catch_panel_without_readable_title_needs_close_anchor(self) -> None:
        ui = Interface()
        frame = self.frame.copy()
        for key, x, y in (("fishdex_button", 550, 518), ("result_x", 920, 103)):
            tile = ui.templates[key]
            h, w = tile.shape[:2]
            frame[y:y+h, x:x+w] = tile
            if key == "fishdex_button":
                self.assertIsNone(ui.result(frame))
        self.assertEqual(ui.result(frame), "caught")
        x, y = ui.result_close(frame, "caught")
        self.assertAlmostEqual(x * SIZE[0], 933)
        self.assertAlmostEqual(y * SIZE[1], 116)

    def test_progress_resets_retries(self) -> None:
        cycle = Cycle(retries=2)
        cycle.advance(Phase.CAST, 1., progress=False)
        self.assertEqual(cycle.retries, 2)
        cycle.advance(Phase.WAIT, 2.)
        self.assertEqual(cycle.retries, 0)

    def test_bottom_toolbar_blocks_cast_even_if_right_panel_is_colored(self) -> None:
        frame = self.frame.copy()
        rod, bait = Match(1151, 328, 1), Match(1151, 448, 1)
        for match in (rod, bait):
            frame[match.y+4:match.y+11, match.x+8:match.x+23] = (20, 115, 235)
        rectangles = ((441, 631, 461, 652), (504, 628, 524, 652), (736, 636, 763, 655))
        self.assertIsNone(Interface.fishing_ready(frame, rod, bait))
        for x, y, r, b in rectangles:
            frame[y:b, x:r] = (120, 120, 120)
        self.assertIs(Interface.fishing_ready(frame, rod, bait), False)
        for x, y, r, b in rectangles:
            frame[y:b, x:r] = (155, 45, 165)
        self.assertIs(Interface.fishing_ready(frame, rod, bait), True)
        frame[332:339, 1159:1174] = frame[452:459, 1159:1174] = (125, 125, 125)
        self.assertIs(Interface.fishing_ready(frame, rod, bait), False)

    def test_agent_opens_only_confirmed_panel_while_idle(self) -> None:
        ui = Interface()
        tile = ui.templates["missions_icon"]
        h, w = tile.shape[:2]
        self.frame[210:210+h, 8:8+w] = tile
        agent = Activities(ui)
        self.assertIsNone(agent.tick(self.frame, False, 1., self.controls, self.region))
        self.assertEqual(agent.tick(self.frame, True, 2., self.controls, self.region), "Otevřít mise")
        agent.reset_observation()
        agent.tick(self.frame, True, 3., self.controls, self.region)
        self.controls.click.assert_not_called()
        agent.tick(self.frame, True, 4., self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, 30 / SIZE[0], 230 / SIZE[1])
        self.assertGreater(agent.due[Panel.MISSIONS], 4.)

    def test_tournament_countdown_prevents_signup(self) -> None:
        ui = Interface()
        for key, x, y in (("tournament_podium", 548, 310), ("tournament_start", 309, 541),
                          ("tournament_countdown", 560, 542),
                          ("activity_close", 972, 53), ("tournament_signup", 851, 559)):
            tile = ui.templates[key]
            h, w = tile.shape[:2]
            self.frame[y:y+h, x:x+w] = tile
        agent = Activities(ui, join_tournaments=True)
        agent.tick(self.frame, False, 1., self.controls, self.region)
        self.controls.click.assert_not_called()
        agent.tick(self.frame, False, 2., self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, 986 / SIZE[0], 67 / SIZE[1])

    def test_mission_sweep_reads_five_pages_even_when_opened_on_the_last(self) -> None:
        ui = Interface()
        agent = Activities(ui)
        page = 5
        frames = []
        for number in range(1, 6):
            image = np.full_like(self.frame, (180, 231, 250))
            for name, x, y in (("missions_title", 554, 49), ("activity_close", 971, 54)):
                tile = ui.templates[name]
                height, width = tile.shape[:2]
                image[y:y+height, x:x+width] = tile
            cv2.putText(image, str(number), (366, 511), 0, .65, (30, 50, 80), 2)
            cv2.putText(image, "5", (403, 511), 0, .65, (30, 50, 80), 2)
            cv2.putText(image, f"Mission {number}", (330, 282), 0, .7, (30, 50, 80), 2)
            if number > 1:
                cv2.ellipse(image, (332, 503), (20, 14), 0, 0, 360, (220, 140, 14), -1)
            if number < 5:
                cv2.ellipse(image, (448, 503), (20, 14), 0, 0, 360, (220, 140, 14), -1)
            if number == 3:
                tile = ui.templates["mission_rare_fish"]
                height, width = tile.shape[:2]
                image[221:221+height, 315:315+width] = tile
            frames.append(image)

        def click(_, x, y):
            nonlocal page
            if x < .3:
                page -= 1
            elif x < .5:
                page += 1
            else:
                page = 0
            return True

        self.controls.click.side_effect = click
        with patch("activities.log_cards") as report:
            for step in range(140):
                if not page:
                    break
                agent.tick(frames[page - 1], False, step * .15, self.controls, self.region)
            self.assertEqual(page, 0)
            self.assertEqual(len(agent.mission_pages), 5)
            self.assertTrue(agent.rare_fish)
            report.assert_called_once()
            self.assertIsNone(agent.tick(self.frame, False, 25., self.controls, self.region))

    def test_tournament_icon_requires_opt_in(self) -> None:
        ui = Interface()
        tile = ui.templates["tournament_icon"]
        h, w = tile.shape[:2]
        self.frame[620:620+h, 603:603+w] = tile

        default = Activities(ui)
        for now in (31., 32.):
            self.assertIsNone(default.tick(self.frame, True, now, self.controls, self.region))
        self.controls.click.assert_not_called()

        enabled = Activities(ui, join_tournaments=True)
        for now in (31., 32.):
            self.assertEqual(enabled.tick(self.frame, True, now, self.controls, self.region), "Otevřít turnaj")
        self.controls.click.assert_called_once_with(self.region, 620 / SIZE[0], 635 / SIZE[1])

    def test_open_tournament_respects_opt_in_even_if_signup_is_available(self) -> None:
        ui = Interface()
        for key, x, y in (("tournament_podium", 548, 310), ("tournament_start", 309, 541)):
            tile = ui.templates[key]
            h, w = tile.shape[:2]
            self.frame[y:y+h, x:x+w] = tile
        signup = Action("Přihlásit herní turnaj", (.72, .8))
        with patch.object(Activities, "_signup", return_value=signup) as eligible:
            for enabled in (False, True):
                agent = Activities(ui, join_tournaments=enabled)
                for now in (31., 32.):
                    agent.tick(self.frame, False, now, self.controls, self.region)
                if not enabled:
                    eligible.assert_not_called()
                    self.controls.click.assert_not_called()
        self.controls.click.assert_called_once_with(self.region, *signup.point)

    def test_agent_chooses_an_affordable_rod_and_preserves_reserve(self) -> None:
        ui = Interface()
        strategy = Progression(ui)
        for key, x, y in (("supply_cash", 872, 102), ("simple_rod", 441, 168),
                          ("big_rod", 441, 309), ("supply_price", 722, 241), ("supply_price", 722, 380)):
            tile = ui.templates[key]
            h, w = tile.shape[:2]
            self.frame[y:y+h, x:x+w] = tile
        for wallet, expected in ((620, 500), (529, 99), (128, None), (None, None)):
            with self.subTest(wallet=wallet), patch.object(ui, "number", side_effect=[wallet, 500, 99]):
                offer = strategy._supply(self.frame, "rod")
                self.assertEqual(offer.cost if offer else None, expected)

    def test_agent_changes_area_only_after_poor_results(self) -> None:
        ui = Interface()
        strategy = Progression(ui)
        tile = ui.templates["world_icon"]
        h, w = tile.shape[:2]
        self.frame[617:617+h, 372:372+w] = tile
        def tick(caught, now):
            return strategy.tick(self.frame, ready=True, now=now, controls=self.controls, region=self.region,
                                 missing=None, casts=18, caught=caught, escaped=0, rare_mission=False)
        self.assertIsNone(tick(10, 1.))
        self.assertIsNotNone(tick(2, 2.))
        self.controls.click.assert_not_called()
        tick(2, 3.)
        self.controls.click.assert_called_once()

    def test_looks_for_casting_ring_before_walking_from_bank(self) -> None:
        auto = Auto(agent=True)
        hud = Hud(8, 20, 20, True, (.95, .55), (.95, .73))
        with (patch("bot.casting_target", return_value=None), patch("bot.shore_approach") as approach,
              patch("bot.casting_hover", return_value=(.56, .5))):
            for now in (1., 1.25, 1.75, 2.):
                auto._ready(self.cycle, hud, self.frame, now, self.controls, self.region)
        self.assertEqual(self.controls.hover.call_count, 2)
        approach.assert_not_called()
        self.controls.cast.assert_not_called()

    def test_agent_approaches_shore_before_casting(self) -> None:
        auto = Auto(agent=True)
        walk = (.5, .5)
        water = (.5, .3)
        target = CastTarget(water, walk, .16, .54)
        hud = Hud(8, 20, 20, True, (.95, .55), (.95, .73))
        with (patch("bot.shore_approach", return_value=walk), patch("bot.casting_hover", return_value=None),
              patch("bot.walking_direction", return_value="w")):
            with patch("bot.casting_target", return_value=None):
                auto._ready(self.cycle, hud, self.frame, 1., self.controls, self.region)
                self.controls.click.assert_not_called()
                auto._ready(self.cycle, hud, self.frame, 2., self.controls, self.region)
            self.controls.walk.assert_called_once_with(self.region.hwnd, "w", cancelled=ANY)
            with patch("bot.casting_target", return_value=target):
                auto._ready(self.cycle, hud, self.frame, 2.2, self.controls, self.region)
                auto._ready(self.cycle, hud, self.frame, 4., self.controls, self.region)
                self.controls.click.assert_not_called()
                auto._ready(self.cycle, hud, self.frame, 5., self.controls, self.region)
            self.controls.cast.assert_called_once_with(self.region, *water, cancelled=ANY)
            self.assertEqual(self.cycle.phase, Phase.CAST)

    def test_farm_energy_purchase_requires_confirmed_offer_and_reserve(self) -> None:
        bot = Auto(agent=True)
        self.cycle.phase = Phase.REFILL
        offer = EnergyShop(150, 35, 0, (.70, .27), (.76, .13))
        bot._farm_refill(self.cycle, offer, False, 1., self.controls, self.region)
        self.controls.click.assert_not_called()
        bot._farm_refill(self.cycle, offer, False, 2., self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, *offer.buy)
        bot._farm_refill(self.cycle, offer, False, 4., self.controls, self.region)
        self.controls.click.assert_called_with(self.region, *offer.close)
        low = EnergyShop(134, 35, 0, offer.buy, offer.close)
        self.controls.reset_mock()
        self.cycle.refill_sent = False
        bot._farm_refill(self.cycle, low, False, 6., self.controls, self.region)
        bot._farm_refill(self.cycle, low, False, 7., self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, *low.close)

    def test_autofish_recasts_after_long_wait_and_after_result(self) -> None:
        auto = Auto()
        ready, busy, balance, caught = range(4)
        sequence = iter([ready] * 3 + [busy] * 145 + [ready] * 3 + [busy] * 2
                        + [balance, caught, balance] + [caught] * 7 + [ready] * 3)
        states_seen: list[int] = []
        crop_used: list[bool] = []
        region = self.region
        position = (.5, .5)
        close_button = (.74, .17)

        class Capture:
            def grab(self, *args, **kwargs):
                state = next(sequence)
                states_seen.append(state)
                crop_used.append(bool(args[0]))
                return np.full((8, 8, 3), state, np.uint8), region

        class Ui(_FakeUi):
            def result(self, image): return "caught" if image[0, 0, 0] == caught else None
            def result_close(self, image, result): return close_button

            def hud(self, image):
                state = image[0, 0, 0]
                if state in (ready, busy):
                    return Hud(8 if state == ready else 7, 20, 20, bool(state == ready), position, position)
                return None

        class Detector:
            def analyze(self, image):
                return Balance((1, 1, 4, 1), .5, .5) if image[0, 0, 0] == balance else None

            def analyze_crop(self, image, x, y): return self.analyze(image)

        input_frames = []
        close_frames = []

        def cast(region, x, y, *, cancelled):
            input_frames.append(len(states_seen))
            if len(input_frames) == 3:
                auto.stop()
            return True

        def click(region, x, y):
            self.assertEqual((x, y), close_button)
            close_frames.append(len(states_seen))
            return True

        self.controls.cast.side_effect = cast
        self.controls.click.side_effect = click
        self.controls.escape.return_value = True
        with (
            patch("bot.Interface", Ui), patch("bot.BalanceDetector", Detector),
            patch("bot.perf_counter", side_effect=count(step=1.)),
            patch("bot.casting_target", return_value=CastTarget(position, position, .15, .54)),
            patch.object(auto._stop_requested, "wait"),
            patch.object(auto, "_annotate"),
        ):
            self.assertIn("vypnutý", auto._loop(Capture(), self.controls))
        self.assertEqual(input_frames, [3, 151, 166])
        self.assertEqual(len(close_frames), 1)
        self.assertTrue(crop_used[154])
        self.assertTrue(crop_used[155])
        self.controls.escape.assert_called_once()
        self.assertEqual(auto.snapshot().caught, 1)

    def test_map_selection_uses_two_frames_without_a_fishing_hud(self) -> None:
        place = Destination(LAKETOWN, "Laketown")
        nav = Navigator(TravelSettings(place, (place,)))
        map_frame = self.frame.copy()
        for name, x, y in (("map_title", 540, 10), ("map_laketown", 560, 117)):
            tile = nav.templates[name]
            h, w = tile.shape[:2]
            map_frame[y:y+h, x:x+w] = tile
        hud = Hud(8, 40, 8, True, (.9, .5), (.9, .7))
        toolbar = (.31, .93)
        original_match = nav.match
        with (patch.object(nav, "match", wraps=nav.match) as match,
              patch("destinations.shore_approach", return_value=None),
              patch("destinations.casting_hover", return_value=(.41, .50))):
            match.side_effect = lambda frame, name: toolbar if name == "map_button" else original_match(frame, name)
            for now in (1., 1.2):
                nav.tick(self.frame, self.frame, hud, True, 0, now, self.controls, self.region)
            self.controls.click.assert_called_once_with(self.region, *toolbar)

            nav.tick(map_frame, map_frame, None, False, 0, 2.6, self.controls, self.region)
            self.assertEqual(self.controls.click.call_count, 1)
            nav.tick(map_frame, map_frame, None, False, 0, 2.8, self.controls, self.region)
            self.assertTrue(nav.map_pending)
            self.assertEqual(self.controls.click.call_count, 2)

            for now, casts in ((6., 0), (9., 10), (12., 20), (15., 30)):
                self.assertIsNone(nav.tick(self.frame, self.frame, hud, True, casts, now,
                                           self.controls, self.region))
            self.assertEqual(self.controls.click.call_count, 2)
            self.assertFalse(nav.visit)

    def test_visible_map_retries_only_the_same_area_then_waits(self) -> None:
        place = Destination(LAKETOWN, "Laketown")
        nav = Navigator(TravelSettings(place, (place,)))
        marks = {"map_title": (.5, .025), "map_laketown": (.49, .22)}
        with patch.object(nav, "match", side_effect=lambda _, name: marks.get(name)):
            for turn in range(MAX_MAP_ATTEMPTS):
                start = 1. + turn * 4
                for now in (start, start + .2):
                    nav.tick(self.frame, self.frame, None, False, 0, now, self.controls, self.region)
            text = nav.tick(self.frame, self.frame, None, False, 0, 20., self.controls, self.region)
        self.assertIn("Vyber oblast ručně", text)
        self.assertEqual(self.controls.click.call_count, MAX_MAP_ATTEMPTS)
        self.assertTrue(all(args == self.controls.click.call_args_list[0]
                            for args in self.controls.click.call_args_list))

    def test_laketown_after_map_selection_walks_and_hands_off_without_bakery(self) -> None:
        place = Destination(LAKETOWN, "Laketown")
        nav = Navigator(TravelSettings(place, (place,)))
        nav.map_pending = True
        hud = Hud(8, 40, 8, True, (.9, .5), (.9, .7))
        with (patch.object(nav, "match", return_value=None),
              patch("destinations.shore_approach", side_effect=[(.46, .50), (.46, .50), None]),
              patch("destinations.walking_direction", return_value="a"),
              patch("destinations.casting_hover", return_value=(.41, .50))):
            for now in (3., 3.2):
                self.assertIn("břehu", nav.tick(self.frame, self.frame, hud, True, 0, now,
                                                 self.controls, self.region))
            self.controls.walk.assert_called_once_with(self.region.hwnd, "a", cancelled=ANY)
            self.assertIsNone(nav.tick(self.frame, self.frame, hud, True, 0, 4.,
                                       self.controls, self.region))
        self.assertFalse(nav.visit)
        self.controls.click.assert_not_called()

    def test_wasd_background_messages_include_key_release(self) -> None:
        controls = BackgroundControls("Fishao test")
        region = self.region
        with (patch.object(controls, "_game_window", return_value=True),
              patch("controls.win32gui.IsIconic", return_value=False),
              patch("controls.win32gui.GetForegroundWindow", return_value=0),
              patch("controls.sleep"), patch.object(controls, "_key", return_value=True) as key_message):
            for key, scan in (("w", 0x11), ("a", 0x1E), ("s", 0x1F), ("d", 0x20)):
                self.assertTrue(controls.walk(region.hwnd, key))
                self.assertEqual(key_message.call_args_list[-2:], [
                    call(region.hwnd, ord(key.upper()), scan, up=False),
                    call(region.hwnd, ord(key.upper()), scan, up=True),
                ])

    def test_navigator_does_not_leave_timed_pond(self) -> None:
        initial = Destination("Rybník A", "Laketown")
        other = Destination("Rybník B", "Laketown")
        nav = Navigator(TravelSettings(initial, (initial, other), refill=True, ponds=True))
        hud = Hud(8, 40, 0, True, (.9, .5), (.9, .7))
        self.assertIsNone(nav.tick(self.frame, self.frame, hud, True, 30, 1., self.controls,
                                   self.region, timed=True))
        self.assertEqual(nav.destination.name, initial.name)
        self.assertFalse(nav.refill)
        self.controls.click.assert_not_called()

    def test_level_panel_confirms_only_central_green_ok(self) -> None:
        ui = Interface()
        frame = self.frame.copy()
        frame[151:556, 367:896] = (186, 232, 250)
        cv2.putText(frame, "200", (530, 328), cv2.FONT_HERSHEY_DUPLEX, 3.2, (12, 70, 244), 11)
        frame[508:545, 563:701] = (20, 151, 42)
        cv2.putText(frame, "OK", (615, 535), cv2.FONT_HERSHEY_DUPLEX, .75, (245, 248, 250), 2)
        button = ui.level_ok(frame)
        self.assertIsNotNone(button)
        self.assertAlmostEqual(button[0], .5, places=2)
        self.assertAlmostEqual(button[1], 526 / SIZE[1], places=2)
        self.assertTrue(self.auto._level_dialog(self.cycle, button, 1., self.controls, self.region))
        self.controls.click.assert_not_called()
        self.assertTrue(self.auto._level_dialog(self.cycle, button, 1.1, self.controls, self.region))
        self.auto._level_dialog(self.cycle, button, 1.2, self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, *button)

        # Odlesk/tečka šumu uvnitř zeleného tlačítka nesmí bránit rozpoznání OK.
        noisy = frame.copy()
        noisy[514:517, 585:588] = (245, 248, 250)
        self.assertIsNotNone(ui.level_ok(noisy))

        no_number = frame.copy()
        no_number[200:390, 440:824] = (186, 232, 250)
        self.assertIsNone(ui.level_ok(no_number))
        frame[508:545, 563:701] = (20, 151, 42)
        cv2.putText(frame, "YES", (609, 535), cv2.FONT_HERSHEY_DUPLEX, .75, (245, 248, 250), 2)
        self.assertIsNone(ui.level_ok(frame))
        self.assertFalse(self.auto._level_dialog(self.cycle, None, 1.3, self.controls, self.region))

    def test_empty_bait_prefers_visible_bakery_and_falls_through_elsewhere(self) -> None:
        place = Destination(CURRENT, "")
        nav = Navigator(TravelSettings(place, (place,)))
        hud = Hud(8, 40, 0, True, (.9, .5), (.9, .7))
        self.assertIsNone(nav.tick(self.frame, self.frame, hud, True, 0, 1., self.controls, self.region))
        self.assertEqual(nav.refill, "")
        self.controls.click.assert_not_called()
        door = (.57, .90)
        x, y = round(door[0] * SIZE[0]), round(door[1] * SIZE[1])
        tile = nav.templates["bakery_door"]
        h, w = tile.shape[:2]
        frame = self.frame.copy()
        frame[y-h//2:y-h//2+h, x-w//2:x-w//2+w] = tile
        target = nav.match(frame, "bakery_door")
        self.assertIsNotNone(target)
        for now in (2., 2.2):
            nav.tick(frame, frame, hud, True, 0, now, self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, *target)
        self.assertEqual(nav.refill, "enter")

    def test_bakery_approaches_jamie_then_equips_bread_only_back_at_water(self) -> None:
        place = Destination(CURRENT, "")
        nav = Navigator(TravelSettings(place, (place,)))
        hud = Hud(8, 40, 0, True, (.9, .5), (.9, .7))
        filled = Hud(8, 40, 75, True, (.9, .5), (.9, .7), bread=True)
        counter, rug = (.5, .47), (.5, .83)
        markers = {"bakery_inside": (.48, .2), "bakery_counter": counter, "bakery_exit": rug}
        with (patch.object(nav, "match", side_effect=lambda _, key: markers.get(key)),
              patch("destinations.avatar_feet", return_value=(.5, .75)) as feet,
              patch("destinations.walking_direction", return_value="w"),
              patch("destinations.shore_approach", return_value=None),
              patch.object(nav, "_fishable", return_value=False) as fishable):
            def tick(now, reading=hud, ready=True):
                return nav.tick(self.frame, self.frame, reading, ready, 0, now, self.controls, self.region)

            nav.refill = "enter"
            tick(1., reading=None, ready=False)
            tick(1.1, reading=None, ready=False)
            self.controls.walk.assert_called_once_with(self.region.hwnd, "w", cancelled=ANY)
            self.controls.click.assert_not_called()
            feet.return_value = (.5, .53)
            tick(2., reading=None, ready=False)
            tick(2.1, reading=None, ready=False)
            self.controls.click.assert_called_once_with(self.region, .5, counter[1] - 65 / SIZE[1])
            markers.update(jamie_offer=(.55, .28), jamie_yes=(.65, .26))
            tick(4.2, ready=False)
            tick(4.3, ready=False)
            self.assertEqual(nav.refill, "receipt")

            markers.clear()
            tick(5.8, reading=None, ready=False)
            self.assertFalse(nav.wants_bread)
            self.assertEqual(nav.refill, "receipt")
            self.assertEqual(self.controls.click.call_count, 2)

            markers.update(bakery_inside=(.48, .2), bakery_counter=counter, bakery_exit=rug, jamie_done=(.48, .25))
            tick(6.6, ready=False)
            tick(6.7, ready=False)
            self.assertEqual(nav.refill, "dialog")
            self.assertFalse(nav.wants_bread)
            markers.pop("jamie_done")
            tick(8.1, ready=False)
            tick(8.2, ready=False)
            self.assertEqual(nav.refill, "leaving")

            markers.pop("bakery_inside")
            markers.pop("bakery_exit")
            markers["bakery_door"] = (.81, .85)
            tick(10.6)
            self.assertEqual(nav.refill, "return")
            self.assertFalse(nav.wants_bread)
            fishable.return_value = True
            tick(11.)
            self.assertEqual(nav.refill, "equip")
            self.assertFalse(nav.wants_bread)
            tick(11.1)
            self.assertTrue(nav.wants_bread)
            self.assertIsNone(tick(12., filled))
            self.assertEqual(nav.refill, "")
            self.assertFalse(nav.visit)

        self.assertEqual(self.controls.click.call_args_list, [
            call(self.region, .5, counter[1] - 65 / SIZE[1]),
            call(self.region, .65, .26),
            call(self.region, .5, counter[1] - 65 / SIZE[1]),
            call(self.region, *rug),
        ])

    def test_laketown_can_resume_when_only_the_casting_circle_is_visible(self) -> None:
        place = Destination(LAKETOWN, "Laketown")
        nav = Navigator(TravelSettings(place, (place,)))
        nav.arrived = True
        hud = Hud(8, 40, 75, True, (.9, .5), (.9, .7), bread=True)
        with (patch.object(nav, "match", return_value=None),
              patch("destinations.shore_approach", return_value=None),
              patch("destinations.casting_hover", return_value=None),
              patch("destinations.casting_target", return_value=CastTarget((.4, .5), (.5, .5), .15, .54))):
            self.assertIsNone(nav.tick(self.frame, self.frame, hud, True, 0, 1., self.controls, self.region))
        self.assertFalse(nav.visit)
        self.controls.click.assert_not_called()

    def test_jamie_needs_confirmed_offer_then_receipt(self) -> None:
        nav = Navigator(TravelSettings(Destination(CURRENT, ""), (), refill=True))
        nav.refill = "counter"
        hud = Hud(8, 40, None, False, (.9, .5), (.9, .7))
        markers = {"bakery_inside": (.48, .2), "jamie_yes": (.65, .26)}
        with patch.object(nav, "match", side_effect=lambda _, key: markers.get(key)):
            def tick(now):
                return nav.tick(self.frame, self.frame, hud, False, 0, now, self.controls, self.region)
            tick(1.)
            tick(1.1)
            self.controls.click.assert_not_called()
            markers["jamie_offer"] = (.55, .28)
            tick(1.2)
            self.controls.click.assert_not_called()
            tick(1.3)
            self.assertEqual(nav.refill, "receipt")
            tick(4.)
            self.assertEqual(nav.refill, "receipt")
            self.controls.click.assert_called_once_with(self.region, .65, .26)
            markers["jamie_reward"] = (.87, .15)
            tick(4.1)
            self.assertEqual(nav.refill, "dialog")
            self.assertFalse(nav.wants_bread)

    def test_manual_bakery_departure_after_yes_resumes_at_shore(self) -> None:
        nav = Navigator(TravelSettings(Destination(CURRENT, ""), ()))
        nav.refill = "receipt"
        hud = Hud(8, 40, 75, True, (.9, .5), (.9, .7), bread=True)
        with (patch.object(nav, "match", return_value=None),
              patch.object(nav, "_fishable", return_value=True),
              patch("destinations.shore_approach", return_value=None)):
            for now in (1., 2.):
                self.assertIsNotNone(nav.tick(self.frame, self.frame, hud, True, 0, now,
                                              self.controls, self.region))
            self.assertIsNone(nav.tick(self.frame, self.frame, hud, True, 0, 3.,
                                       self.controls, self.region))
        self.assertEqual(nav.refill, "")
        self.assertFalse(nav.wants_bread)
        self.controls.click.assert_not_called()

    def test_basic_rod_requires_matching_icon_without_red_badge(self) -> None:
        ui = Interface()
        frame = self.frame.copy()
        x, y = 1151, 328
        match = Match(x, y, 1.)
        icon = ui.templates["rod_basic"]
        h, w = icon.shape[:2]
        badge = frame[y+76:y+93, x+44:x+79]
        self.assertFalse(ui._basic_rod(frame, match, badge))
        frame[y+36:y+36+h, x+26:x+26+w] = icon
        self.assertTrue(ui._basic_rod(frame, match, badge))
        badge[-9:] = (5, 35, 240)
        self.assertFalse(ui._basic_rod(frame, match, badge))

    def test_basic_rod_discards_older_spot_and_waits_for_charge(self) -> None:
        slot = (.9, .55)
        large = CastTarget((.4, .2), (.5, .5), .24, .54)
        small = CastTarget((.49, .41), (.5, .5), .10, .54)
        cycle = self.cycle
        cycle.basic_rod = False
        cycle.candidate = large
        cycle.position = large.point
        cycle.score[large.point] = 6
        with patch("bot.casting_target", return_value=small) as measure:
            self.auto._ready(cycle, Hud(0, None, 55, True, slot, slot, True),
                             self.frame, 1., self.controls, self.region)
            self.assertEqual(cycle.phase, Phase.ENERGY)
            measure.assert_not_called()
            charged = Hud(1, None, 55, True, slot, slot, True)
            self.auto._ready(cycle, charged, self.frame, 2., self.controls, self.region)
            measure.assert_called_once_with(self.frame, None, avoid=None, rejected=())
            self.controls.cast.assert_not_called()
            self.auto._ready(cycle, charged, self.frame, 3., self.controls, self.region)
        self.controls.click.assert_not_called()
        self.controls.cast.assert_called_once_with(self.region, *small.point, cancelled=ANY)
        self.assertEqual(cycle.phase, Phase.CAST)

    def test_preview_keeps_sixty_slots_with_slightly_late_capture(self) -> None:
        clock = PreviewClock()
        times = [i * .00830 for i in range(1200)]
        published = sum(clock.ready(now, True) for now in times)
        self.assertEqual(published, 598)
        self.assertTrue(clock.ready(10., False))
        self.assertFalse(clock.ready(10.01, False))
        self.assertTrue(clock.ready(10.02, True))

    def test_mini_game_wait_uses_high_resolution_and_respects_stop(self) -> None:
        with (patch("assist.perf_counter", return_value=1.001),
              patch("assist.sleep") as fast_sleep,
              patch.object(self.auto._stop_requested, "wait") as stop_wait):
            self.auto._wait_frame(1., .008, tracking=True)
            self.assertAlmostEqual(fast_sleep.call_args.args[0], .007)
            stop_wait.assert_not_called()
            self.auto._wait_frame(1., .06, tracking=False)
            stop_wait.assert_called_once()
            self.auto.stop()
            self.auto._wait_frame(1., .008, tracking=True)
            fast_sleep.assert_called_once()

    def test_preview_sends_rgb_bytes_and_reuses_tk_image(self) -> None:
        app = App.__new__(App)
        app.root = Mock()
        app.canvas = Mock()
        app.canvas.winfo_width.return_value = 10
        app.canvas.winfo_height.return_value = 10
        app.canvas_image, app.canvas_message = 1, 2
        app.preview_photo = None
        app.preview_frame = np.array([[[5, 40, 200]]], np.uint8)
        with patch("gui.tk.PhotoImage") as photo:
            app._render_preview()
            photo.assert_called_once_with(master=app.root, data=b"P6\n1 1\n255\n\xc8(\x05", format="ppm")
            app.preview_frame = np.array([[[8, 1, 2], [6, 5, 4]]], np.uint8)
            app._render_preview()
            photo.return_value.configure.assert_called_once_with(
                data=b"P6\n2 1\n255\n\x02\x01\x08\x04\x05\x06", format="ppm")

    def test_tracking_rate_counts_detections_and_expires(self) -> None:
        self.assertIsNone(self.auto._tracking_rate(True, 1.))
        self.assertAlmostEqual(self.auto._tracking_rate(True, 1.02), 50.)
        self.assertAlmostEqual(self.auto._tracking_rate(False, 1.04), 25.)
        self.assertIsNone(self.auto._tracking_rate(False, 2.))

    def test_predicts_fast_drift_and_resets_after_missing_marker(self) -> None:
        policy = BalancePolicy()
        bar = (0, 0, 284, 30)
        self.assertIsNone(policy.choose(Balance(bar, .500, .5), 1.))
        self.assertEqual(policy.choose(Balance(bar, .511, .5), 1.012), "left")
        self.assertEqual(policy.choose(Balance(bar, .470, .5), 1.024), "right")
        self.assertIsNone(policy.choose(None, 1.033))
        self.assertIsNone(policy.choose(Balance(bar, .503, .5), 1.045))

    def test_detects_bite_with_one_marker_on_first_frame(self) -> None:
        for indicator, expected, lower in ((.08, "right", False), (.92, "left", True)):
            with self.subTest(indicator=indicator):
                frame = np.full((120, 380, 3), (180, 221, 237), np.uint8)
                gradient = np.zeros((30, 284, 3), np.uint8)
                gradient[:, :, 0] = np.interp(
                    np.linspace(0, 1, 284), (0, .25, .5, .75, 1), (0, 45, 60, 45, 0))
                gradient[:, :, 1:] = 255
                frame[40:70, 40:324] = cv2.cvtColor(gradient, cv2.COLOR_HSV2BGR)
                x = round(40 + 283 * indicator)
                base, tip = (69, 60) if lower else (40, 49)
                cv2.fillConvexPoly(frame, np.array([(x - 6, base), (x + 6, base), (x, tip)]),
                                   (233, 110, 27))
                reading = BalanceDetector().analyze(frame)
                self.assertIsNotNone(reading)
                self.assertAlmostEqual(reading.indicator, indicator, delta=.015)
                self.assertEqual(BalancePolicy().choose(reading, 1.), expected)

                frame[40:70, 40:324] = cv2.cvtColor(gradient, cv2.COLOR_HSV2BGR)
                self.assertIsNone(BalanceDetector().analyze(frame))

    def test_arrow_changes_transport_when_game_focus_changes(self) -> None:
        controls = BackgroundControls("FISHAO - Reborn")
        hwnd = 10
        with (
            patch("controls.win32gui.GetForegroundWindow", return_value=hwnd) as focus,
            patch("controls.win32gui.IsIconic", return_value=False),
            patch.object(controls, "_game_window", return_value=True),
            patch.object(controls, "_arrow") as native,
            patch.object(controls, "_key", return_value=True) as message,
        ):
            controls.direction(hwnd, "left")
            self.assertFalse(controls.uses_window_messages)
            controls.direction(hwnd, "left")
            native.assert_called_once_with("left", up=False)
            message.assert_not_called()

            focus.return_value = 20
            controls.direction(hwnd, "left")
            self.assertTrue(controls.uses_window_messages)
            self.assertEqual(native.call_args_list, [call("left", up=False), call("left", up=True)])
            self.assertEqual(controls.held, "left")
            controls.direction(hwnd, None)
            self.assertEqual(message.call_args_list, [
                call(hwnd, win32con.VK_LEFT, 0x4B, up=False, extended=True),
                call(hwnd, win32con.VK_LEFT, 0x4B, up=True, extended=True),
            ])
            self.assertIsNone(controls.held)

    def test_controls_survive_sendinput_error_on_desktop_lock(self) -> None:
        controls = Controls()
        with (
            patch("controls.win32gui.GetForegroundWindow", return_value=self.region.hwnd),
            patch.object(controls, "_arrow", side_effect=OSError("Windows přijal 0/1 kroků vstupu.")),
        ):
            controls.direction(self.region.hwnd, "left")
            self.assertIsNone(controls.held)
            self.assertIsNotNone(controls.problem)

        controls.held = "right"
        with patch.object(controls, "_arrow", side_effect=OSError("locked")):
            controls.release()
            self.assertIsNone(controls.held)

    def test_workstation_lock_pauses_and_resumes_safely(self) -> None:
        auto = Auto()
        region = self.region
        position = (.5, .5)
        calls = 0

        class Capture:
            def grab(self, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls in (1, 2):
                    raise WorkstationLocked("Počítač je uzamčen.")
                auto.stop()
                return np.zeros((8, 8, 3), np.uint8), region

        class Ui(_FakeUi):
            def hud(self, image): return Hud(8, 20, 20, True, position, position)

        class Detector:
            def analyze(self, image): return None
            def analyze_crop(self, image, x, y): return None

        with (
            patch("bot.Interface", Ui), patch("bot.BalanceDetector", Detector),
            patch.object(auto._stop_requested, "wait"),
        ):
            message = auto._loop(Capture(), self.controls)
        self.assertIn("vypnutý", message)
        self.assertGreaterEqual(calls, 3)

    def test_wait_unreadable_hud_recovers_to_ready_instead_of_hanging(self) -> None:
        auto = Auto()
        region = self.region
        position = (.5, .5)
        ready, busy, unreadable = 0, 1, 2
        sequence = iter([ready] * 2 + [busy] + [unreadable] * 7 + [ready] * 4)
        casts = 0

        class Capture:
            def grab(self, *args, **kwargs):
                state = next(sequence, ready)
                return np.full((8, 8, 3), state, np.uint8), region

        class Ui(_FakeUi):
            def hud(self, image):
                state = image[0, 0, 0]
                if state in (ready, busy):
                    return Hud(8, 20, 20, state == ready, position, position)
                return None

        class Detector:
            def analyze(self, image): return None
            def analyze_crop(self, image, x, y): return None

        def cast(r, x, y, *, cancelled):
            nonlocal casts
            casts += 1
            if casts == 2:
                auto.stop()
            return True

        self.controls.cast.side_effect = cast
        with (
            patch("bot.Interface", Ui), patch("bot.BalanceDetector", Detector),
            patch("bot.perf_counter", side_effect=count(step=2.)),
            patch("bot.casting_target", return_value=CastTarget(position, position, .15, .54)),
            patch.object(auto._stop_requested, "wait"),
        ):
            auto._loop(Capture(), self.controls)
        self.assertEqual(casts, 2)
        self.assertEqual(auto.machine.consecutive_errors, 0)

    def test_mission_accept_and_claim_are_idempotent(self) -> None:
        ui = Interface()
        agent = Activities(ui)
        image = np.full_like(self.frame, (180, 231, 250))
        title = ui.templates["missions_title"]
        image[49:49+title.shape[0], 554:554+title.shape[1]] = title
        close = ui.templates["activity_close"]
        image[54:54+close.shape[0], 971:971+close.shape[1]] = close

        with patch.object(ui, "mission_card_action", return_value=("accept", (.5, .6))) as card_btn:
            agent.tick(image, False, 1.0, self.controls, self.region)
            self.controls.click.assert_not_called()
            agent.tick(image, False, 1.2, self.controls, self.region)
            self.controls.click.assert_called_once_with(self.region, .5, .6)
            # Okamžitý další tik nesmí kliknout Přijmout podruhé (idempotence).
            agent.tick(image, False, 2.5, self.controls, self.region)
            self.assertEqual(self.controls.click.call_count, 1)

            card_btn.return_value = ("claim", (.5, .6))
            agent.tick(image, False, 3.0, self.controls, self.region)
            agent.tick(image, False, 3.2, self.controls, self.region)
            self.assertEqual(self.controls.click.call_count, 2)
            # Po zmizení tlačítka claim se mise označí jako COMPLETED.
            card_btn.return_value = None
            agent.tick(image, False, 4.5, self.controls, self.region)
            self.assertTrue(any(r.status is MissionStatus.COMPLETED for r in agent.tracker.records.values()))

            # Po dalším kliknutí claim a zavření panelu na 1 snímek s ready=False nesmí stav předčasně selhat.
            card_btn.return_value = ("claim", (.5, .6))
            frame2 = image.copy()
            frame2[220:240, 350:420] = 50
            agent.tick(frame2, False, 16.0, self.controls, self.region)
            agent.tick(frame2, False, 16.2, self.controls, self.region)
            self.assertIsNotNone(agent.tracker.pending_claim_sig)
            agent.tick(self.frame, False, 17.2, self.controls, self.region)
            agent.tick(self.frame, True, 17.3, self.controls, self.region)
            self.assertIsNone(agent.tracker.pending_claim_sig)
            self.assertGreaterEqual(
                sum(1 for r in agent.tracker.records.values() if r.status is MissionStatus.COMPLETED), 2
            )

    def test_collect_orange_popup_clicks_without_stopping_loop(self) -> None:
        ui = Interface()
        frame = self.frame.copy()
        # Krémový střed dialogu "WOW, YOU'RE FAST!" + oranžové tlačítko "Collect" s bílým textem.
        frame[250:530, 320:940] = (232, 246, 250)
        # BGR pro jasně oranžovou (HSV ~16, 230, 255 -> B=25, G=138, R=250)
        frame[490:535, 750:910] = (25, 138, 250)
        cv2.putText(frame, "Collect", (782, 520), cv2.FONT_HERSHEY_DUPLEX, 0.75, (248, 250, 252), 2)
        popup = ui.mission_popup_action(frame)
        self.assertIsNotNone(popup)
        kind, point = popup
        self.assertEqual(kind, "claim")
        self.assertAlmostEqual(point[0], 830 / SIZE[0], places=2)
        self.assertAlmostEqual(point[1], 512 / SIZE[1], places=2)

        auto = Auto(agent=True, continuous=True)
        self.assertTrue(auto._modal_dialog(self.cycle, frame, ui, 1.0, self.controls, self.region))
        self.controls.click.assert_not_called()
        self.assertTrue(auto._modal_dialog(self.cycle, frame, ui, 1.1, self.controls, self.region))
        self.controls.click.assert_called_once_with(self.region, *point)

    def test_ui_contract_shared_between_interface_and_test_fakes(self) -> None:
        fake = _FakeUi()
        self.assertIsInstance(fake, GameUI)
        self.assertTrue(issubclass(Interface, GameUI))

    def test_continuous_afk_cooldown_recovery_continues_until_stopped(self) -> None:
        auto = Auto(agent=True, continuous=True)
        auto.machine.max_consecutive_errors = 1
        region = self.region
        position = (.5, .5)
        ready, busy, unreadable = 0, 1, 2
        # Po vypršení Phase.WAIT (1. chyba) pokračuje neznámá obrazovka do 2. chyby (> max=1) -> AFK cooldown recovery -> další hod -> stop().
        sequence = iter([ready] * 2 + [busy] + [unreadable] * 11 + [ready] * 4)
        casts = 0

        class Capture:
            def grab(self, *args, **kwargs):
                state = next(sequence, ready)
                return np.full((8, 8, 3), state, np.uint8), region

        class Ui(_FakeUi):
            def hud(self, image):
                state = image[0, 0, 0]
                if state in (ready, busy):
                    return Hud(8, 20, 20, state == ready, position, position)
                return None

        class Detector:
            def analyze(self, image): return None
            def analyze_crop(self, image, x, y): return None

        def cast(r, x, y, *, cancelled):
            nonlocal casts
            casts += 1
            if casts == 2:
                auto.stop()
            return True

        self.controls.cast.side_effect = cast
        with (
            patch("bot.Interface", Ui), patch("bot.BalanceDetector", Detector),
            patch("bot.perf_counter", side_effect=count(step=2.)),
            patch("bot.casting_target", return_value=CastTarget(position, position, .15, .54)),
            patch.object(auto, "_afk_cooldown_recovery", wraps=auto._afk_cooldown_recovery) as afk_recovery,
            patch.object(auto._stop_requested, "wait"),
        ):
            msg = auto._loop(Capture(), self.controls)
        self.assertIn("vypnutý", msg)
        afk_recovery.assert_called_once()
        self.assertEqual(casts, 2)
        self.assertEqual(auto.machine.consecutive_errors, 0)

    def test_gui_log_isolates_events_panel_from_technical_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = App.__new__(App)
            app.output_dir = Path(tmp)
            app.messages = SimpleQueue()
            handlers = app._log_setup()
            try:
                log_structured(
                    app.log,
                    logging.INFO,
                    correlation_id="app-gui",
                    state=BotState.MISSION_WORK,
                    action="activity",
                    result="ok",
                    reason="mise_turnaje",
                )
                app.event_log.info("Otevřel jsem mise.")
                app.event_log.info("Otevřel jsem mise.")
                app.event_log.info("Mise přijata.")

                gui_lines: list[str] = []
                while True:
                    try:
                        gui_lines.append(app.messages.get_nowait())
                    except Empty:
                        break
                self.assertEqual(len(gui_lines), 2)
                self.assertTrue(all("state=" not in line for line in gui_lines))
                self.assertIn("Otevřel jsem mise.", gui_lines[0])
                self.assertIn("Mise přijata.", gui_lines[1])

                disk_text = (Path(tmp) / "runtime" / "logs" / "fishao.log").read_text(encoding="utf-8")
                self.assertIn("state=mission_work", disk_text)
                self.assertIn("Otevřel jsem mise.", disk_text)
            finally:
                for logger, handler in handlers:
                    logger.removeHandler(handler)
                    handler.close()

    def test_mission_events_verify_outcome_and_avoid_polling_flood(self) -> None:
        ui = Interface()
        records: list[tuple[int, str]] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append((record.levelno, record.getMessage()))

        event_logger = logging.getLogger("fishao.events.test_missions")
        event_logger.setLevel(logging.INFO)
        event_logger.propagate = False
        handler = CaptureHandler()
        event_logger.addHandler(handler)
        try:
            feed = EventFeed("fishao.events.test_missions")
            agent = Activities(ui, events=feed)
            image = np.full_like(self.frame, (180, 231, 250))
            title = ui.templates["missions_title"]
            image[49:49+title.shape[0], 554:554+title.shape[1]] = title
            close = ui.templates["activity_close"]
            image[54:54+close.shape[0], 971:971+close.shape[1]] = close

            with patch.object(ui, "mission_card_action", return_value=("accept", (.5, .6))) as card_btn:
                agent.tick(image, False, 1.0, self.controls, self.region)
                agent.tick(image, False, 1.2, self.controls, self.region)
                # Po samotném kliknutí nesmí být zapsáno, že je mise přijata.
                self.assertFalse(any("Mise" in msg and "přijata" in msg for _, msg in records))
                self.assertTrue(any("čekám na ověření" in msg for _, msg in records))

                card_btn.return_value = None
                agent.tick(image, False, 2.5, self.controls, self.region)
                self.assertTrue(any("přijata." in msg for _, msg in records))

                # Následný pokus o claim, který zmizí bez ověření, musí zapsat chybu s důvodem.
                card_btn.return_value = ("claim", (.5, .6))
                agent.tick(image, False, 3.0, self.controls, self.region)
                agent.tick(image, False, 3.2, self.controls, self.region)
                self.assertFalse(any("odměna potvrzena" in msg for _, msg in records))
                agent.tick(self.frame, False, 4.2, self.controls, self.region)
                agent.tick(self.frame, False, 5.8, self.controls, self.region)
                self.assertTrue(
                    any(lvl == logging.WARNING and "selhala" in msg and "důvod:" in msg for lvl, msg in records)
                )
                self.assertFalse(any("Agent ověřuje herní obrazovku" in msg for _, msg in records))
        finally:
            event_logger.removeHandler(handler)
            handler.close()

    def test_auto_emits_honest_learning_and_verified_modal_events(self) -> None:
        emitted: list[str] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                emitted.append(record.getMessage())

        event_logger = logging.getLogger("fishao.events.test_auto")
        event_logger.setLevel(logging.INFO)
        event_logger.propagate = False
        handler = CaptureHandler()
        event_logger.addHandler(handler)
        try:
            auto = Auto(agent=True, continuous=True)
            auto._events = EventFeed("fishao.events.test_auto")

            ui = _FakeUi()
            with patch.object(ui, "level_ok", side_effect=[(.5, .77), (.5, .77), None]):
                self.assertTrue(auto._modal_dialog(self.cycle, self.frame, ui, 1.0, self.controls, self.region))
                self.assertTrue(auto._modal_dialog(self.cycle, self.frame, ui, 1.1, self.controls, self.region))
                # Před zmizením dialogu nesmí být hlášeno potvrzení a zavření.
                self.assertFalse(any("potvrzen a zavřen" in msg for msg in emitted))
                self.assertFalse(auto._modal_dialog(self.cycle, self.frame, ui, 1.8, self.controls, self.region))
                self.assertTrue(any("Dialog nové úrovně potvrzen a zavřen." in msg for msg in emitted))

            # Využití in-session skóre spotu se zaznamená jako zkušenost z aktuálního běhu.
            spot = (.44, .32)
            self.cycle.score[spot] = 2
            self.cycle.candidate = CastTarget(spot, (.48, .48), .15, .54)
            self.controls.pointer_at.return_value = True
            hud = Hud(5, 10, 10, True, (.9, .5), (.9, .7))
            with patch("bot.casting_target", return_value=self.cycle.candidate):
                auto._ready(self.cycle, hud, self.frame, 5.0, self.controls, self.region)
            self.assertTrue(any("Zkušenost z tohoto běhu" in msg and "+2" in msg for msg in emitted))
        finally:
            event_logger.removeHandler(handler)
            handler.close()

    def test_locked_workstation_continues_via_background_fallback_when_frames_available(self) -> None:
        auto = Auto(agent=True, continuous=True)
        region = self.region
        position = (.5, .5)
        locked = True
        primary_casts = 0
        bg_casts = 0
        bg_controls = Mock(spec=BackgroundControls)
        bg_controls.problem = None

        class Capture:
            title = "FISHAO - Reborn"

            def grab(self, *args, **kwargs):
                if locked:
                    raise WorkstationLocked("Počítač je uzamčen.")
                return np.zeros((8, 8, 3), np.uint8), region

            def grab_locked_background(self, *args, **kwargs):
                return np.full((8, 8, 3), 20, np.uint8), region

        class Ui(_FakeUi):
            def hud(self, image): return Hud(8, 20, 20, True, position, position)

        class Detector:
            def analyze(self, image): return None
            def analyze_crop(self, image, x, y): return None

        def bg_cast(r, x, y, *, cancelled):
            nonlocal bg_casts, locked
            bg_casts += 1
            # Po hodu na zamčené ploše simuluj odemčení počítače.
            locked = False
            return True

        def fg_cast(r, x, y, *, cancelled):
            nonlocal primary_casts
            primary_casts += 1
            auto.stop()
            return True

        bg_controls.cast.side_effect = bg_cast
        bg_controls.pointer_at.return_value = True
        self.controls.cast.side_effect = fg_cast
        self.controls.pointer_at.return_value = True

        with (
            patch("bot.Interface", Ui),
            patch("bot.BalanceDetector", Detector),
            patch("bot.BackgroundControls", return_value=bg_controls),
            patch("bot.set_keep_awake") as keep_awake,
            patch("bot.perf_counter", side_effect=count(step=1.0)),
            patch("bot.casting_target", return_value=CastTarget(position, position, .15, .54)),
            patch.object(auto._stop_requested, "wait"),
        ):
            msg = auto._loop(Capture(), self.controls)

        self.assertIn("vypnutý", msg)
        self.assertEqual(bg_casts, 1)
        self.assertEqual(primary_casts, 1)
        bg_controls.release.assert_called()
        keep_awake.assert_any_call(True)
        self.assertEqual(keep_awake.call_args_list[-1], call(False))

    def test_locked_workstation_without_readable_pixels_waits_without_stopping(self) -> None:
        auto = Auto(agent=True, continuous=True)
        region = self.region
        position = (.5, .5)
        locked_ticks = 0

        class Capture:
            title = "FISHAO - Reborn"

            def grab(self, *args, **kwargs):
                if locked_ticks < 3:
                    raise WorkstationLocked("Počítač je uzamčen.")
                auto.stop()
                return np.zeros((8, 8, 3), np.uint8), region

            def grab_locked_background(self, *args, **kwargs):
                nonlocal locked_ticks
                locked_ticks += 1
                raise WorkstationLocked("Windows blokuje snímky na zamčené ploše.")

        class Ui(_FakeUi):
            def hud(self, image): return Hud(8, 20, 20, True, position, position)

        class Detector:
            def analyze(self, image): return None
            def analyze_crop(self, image, x, y): return None

        with (
            patch("bot.Interface", Ui),
            patch("bot.BalanceDetector", Detector),
            patch("bot.set_keep_awake"),
            patch.object(auto._stop_requested, "wait"),
        ):
            msg = auto._loop(Capture(), self.controls)

        self.assertIn("vypnutý", msg)
        self.assertEqual(locked_ticks, 3)
        self.assertEqual(auto.machine.consecutive_errors, 0)

    def test_window_capture_tries_print_window_during_lock_before_raising_locked(self) -> None:
        capture = WindowCapture("FISHAO - Reborn", background=True)
        region = self.region
        black = np.zeros((region.height, region.width, 3), np.uint8)
        visible = np.full((region.height, region.width, 3), 42, np.uint8)

        with (
            patch("window.is_workstation_locked", return_value=True),
            patch.object(capture, "_window_region", return_value=region),
            patch.object(capture, "_window_pixels", side_effect=[black, visible]),
        ):
            img, reg = capture.grab()
            self.assertEqual(reg, region)
            self.assertTrue(img.any())

        with (
            patch("window.is_workstation_locked", return_value=True),
            patch.object(capture, "_window_region", return_value=region),
            patch.object(capture, "_window_pixels", side_effect=[black, black]),
        ):
            with self.assertRaises(WorkstationLocked):
                capture.grab()

    def test_background_controls_work_when_foreground_query_errors_during_lock(self) -> None:
        import win32gui
        controls = BackgroundControls("Fishao test")
        region = self.region
        with (
            patch.object(controls, "_game_window", return_value=True),
            patch("controls.win32gui.IsIconic", return_value=False),
            patch("controls.win32gui.GetForegroundWindow", side_effect=win32gui.error(0, "GetForegroundWindow", "locked")),
            patch("controls.sleep"),
            patch.object(controls, "_key", return_value=True) as key_message,
        ):
            controls.direction(region.hwnd, "left")
            self.assertEqual(controls.held, "left")
            self.assertTrue(controls.walk(region.hwnd, "w"))
            self.assertTrue(controls.escape(region.hwnd))
            self.assertGreaterEqual(key_message.call_count, 5)


