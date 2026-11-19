from __future__ import annotations

import unittest
from itertools import count
from unittest.mock import Mock, patch

import cv2
import numpy as np

from bot import Auto, Cycle, Phase
from game_ui import EnergyShop, Hud, Interface, Inventory, Match, SIZE
from minigame import Balance, BalancePolicy
from spots import CastTarget
from window import Region


class AutoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.auto = Auto()
        self.cycle = Cycle()
        self.region = Region(1,0,0,*SIZE)
        self.controls = Mock()
        self.frame = np.zeros((SIZE[1],SIZE[0],3), np.uint8)

    def test_waits_for_energy_and_requires_two_cast_points(self) -> None:
        available = (0.9,.5)
        empty = Hud(0,7,12,True,available,available)
        self.auto._ready(self.cycle,empty,self.frame,0.,self.controls,self.region)
        self.assertEqual(self.cycle.phase,Phase.ENERGY)
        self.controls.click.assert_not_called()
        good = Hud(1,7,12,True,available,available)
        with patch("bot.casting_target",return_value=CastTarget((.4,.2),(.4,.3),.15,.54)):
            self.auto._ready(self.cycle,good,self.frame,2.,self.controls,self.region)
            self.controls.click.assert_not_called()
            self.auto._ready(self.cycle,good,self.frame,3.,self.controls,self.region)
        self.assertEqual(self.cycle.phase,Phase.CAST)
        self.controls.click.assert_called_once_with(self.region,.4,.2)

    def test_changes_equipment_only_after_inventory_and_verifies_hud(self) -> None:
        rod,bait = (.91,.5),(.91,.7)
        self.auto._ready(self.cycle,Hud(2,11,0,True,rod,bait),self.frame,0.,self.controls,self.region)
        self.assertEqual(self.cycle.phase,Phase.GEAR)
        self.controls.click.assert_called_once_with(self.region,*bait)
        slots = ((.4,.55),(.48,.55))
        select = (.72,.3)
        no_stock = Inventory("bait",0,slots,select,(.8,.1),None,None)
        self.auto._gear(self.cycle,no_stock,None,1.,self.controls,self.region)
        self.controls.click.assert_called_with(self.region,*slots[0])
        self.auto._gear(self.cycle,no_stock,None,2.,self.controls,self.region)
        self.controls.click.assert_called_with(self.region,*slots[1])
        supply = Inventory("bait",28,slots,select,(.8,.1),None,None)
        self.auto._gear(self.cycle,supply,None,3.,self.controls,self.region)
        self.controls.click.assert_called_with(self.region,*select)
        self.assertEqual(self.cycle.phase,Phase.GEAR)
        self.auto._gear(self.cycle,None,Hud(2,11,28,True,rod,bait),4.,self.controls,self.region)
        self.assertEqual(self.cycle.phase,Phase.READY)

    def test_changed_rod_range_requires_two_fresh_measurements(self) -> None:
        default = (.9, .5)
        hud = Hud(1, 7, 12, True, default, default)
        small = CastTarget((.4, .2), (.4, .3), .075, .54)
        large = CastTarget(small.point, small.center, .15, .54)
        with patch("bot.casting_target", side_effect=(small, large, large)):
            self.auto._ready(self.cycle, hud, self.frame, 1., self.controls, self.region)
            self.auto._ready(self.cycle, hud, self.frame, 2., self.controls, self.region)
            self.controls.click.assert_not_called()
            self.auto._ready(self.cycle, hud, self.frame, 3., self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, *large.point)

    def test_cast_waits_until_pointer_is_over_confirmed_water(self) -> None:
        p = (.43, .35)
        hud = Hud(2, 7, 12, True, (.9,.5), (.9,.7))
        target = CastTarget(p, (.48, .48), .15, .54)
        self.controls.pointer_at.side_effect = (False, False, True)
        with patch("bot.casting_target", return_value=target):
            self.auto._ready(self.cycle, hud, self.frame, 1., self.controls, self.region)
            self.auto._ready(self.cycle, hud, self.frame, 1.1, self.controls, self.region)
            self.controls.click.assert_not_called()
            self.auto._ready(self.cycle, hud, self.frame, 1.2, self.controls, self.region)
        self.assertEqual(self.controls.hover.call_count, 2)
        self.controls.click.assert_called_once_with(self.region, *p)

    def test_missing_circle_uses_hover_only_until_two_confirmations(self) -> None:
        p = (.43, .35)
        hud = Hud(2, 7, 12, True, (.9,.5), (.9,.7))
        target = CastTarget(p, (.48, .48), .15, .54)
        with patch("bot.casting_target", side_effect=(None, None, target, target)), patch("bot.casting_hover", return_value=p):
            for stamp in (1., 1.5, 2.1):
                self.auto._ready(self.cycle, hud, self.frame, stamp, self.controls, self.region)
                self.controls.click.assert_not_called()
            self.auto._ready(self.cycle, hud, self.frame, 2.2, self.controls, self.region)
        self.controls.hover.assert_called_once_with(self.region, *p)
        self.controls.click.assert_called_once_with(self.region, *p)

    def test_result_matching_at_different_sizes(self) -> None:
        ref = Interface()
        for label,(x,y) in {"caught":(646,116),"escaped":(506,263)}.items():
            frame = self.frame.copy()
            sample = ref.templates[label]
            h,w = sample.shape[:2]
            frame[y:y+h,x:x+w] = sample
            self.assertEqual(ref.result(frame),label)
            resized = cv2.resize(frame,(900,485),interpolation=cv2.INTER_AREA)
            self.assertEqual(ref.result(ref.normalize(resized)),label)
        self.assertIsNone(ref.result(self.frame))

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
        for key, x, y in (("fishdex_button",550,518), ("result_x",920,103)):
            tile = ui.templates[key]
            h,w = tile.shape[:2]
            frame[y:y+h,x:x+w] = tile
            if key == "fishdex_button":
                self.assertIsNone(ui.result(frame))
        self.assertEqual(ui.result(frame), "caught")
        x,y = ui.result_close(frame, "caught")
        self.assertAlmostEqual(x * SIZE[0], 933)
        self.assertAlmostEqual(y * SIZE[1], 116)

    def test_progress_resets_retries(self) -> None:
        cycle = Cycle(retries=2)
        cycle.advance(Phase.CAST,1.,progress=False)
        self.assertEqual(cycle.retries,2)
        cycle.advance(Phase.WAIT,2.)
        self.assertEqual(cycle.retries,0)

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

    def test_refill_confirms_quote_and_sends_one_purchase(self) -> None:
        auto = Auto(xp_farm=True)
        cycle = Cycle(phase=Phase.REFILL)
        buy, close = (.70, .27), (.76, .13)
        shop = EnergyShop(35, 35, 0, buy, close)
        auto._refill(cycle, shop, False, 1., self.controls, self.region)
        self.controls.click.assert_not_called()
        auto._refill(cycle, shop, False, 2., self.controls, self.region)
        self.controls.click.assert_called_once_with(self.region, *buy)
        auto._refill(cycle, shop, False, 4., self.controls, self.region)
        self.controls.click.assert_called_with(self.region, *close)
        auto._refill(cycle, None, True, 5., self.controls, self.region)
        self.assertEqual(cycle.phase, Phase.ENERGY)

    def test_refill_closes_when_balance_energy_or_price_is_not_confirmed(self) -> None:
        auto = Auto(xp_farm=True)
        buy, close = (.70, .27), (.76, .13)
        for balance, price, energy in ((34, 35, 0), (None, 35, 0), (40, 30, 0), (40, 35, 1), (40, 35, None)):
            with self.subTest(balance=balance, price=price, energy=energy):
                self.controls.reset_mock()
                cycle = Cycle(phase=Phase.REFILL)
                shop = EnergyShop(balance, price, energy, buy, close)
                auto._refill(cycle, shop, False, 1., self.controls, self.region)
                auto._refill(cycle, shop, False, 2., self.controls, self.region)
                self.controls.click.assert_called_once_with(self.region, *close)

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

        class Ui:
            def normalize(self, image): return image
            def inventory(self, image): return None
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

        def click(region, x, y):
            if (x, y) == close_button:
                close_frames.append(len(states_seen))
                return True
            input_frames.append(len(states_seen))
            if len(input_frames) == 3:
                auto.stop()
            return True

        self.controls.click.side_effect = click
        self.controls.escape.return_value = True
        with (
            patch("bot.Interface", Ui), patch("bot.BalanceDetector", Detector),
            patch("bot.perf_counter", side_effect=count(step=1.)),
            patch("bot.casting_target", return_value=CastTarget(position,position,.15,.54)),
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

