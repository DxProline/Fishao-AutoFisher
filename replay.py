from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TextIO

from game_ui import Interface
from minigame import BalanceDetector, BalancePolicy
from recorded import Recording
from vision import VisualDetector


def replay(recording: Recording, stream: TextIO, *, every: bool = True) -> None:
    detector = VisualDetector()
    ui = Interface()
    balance = BalanceDetector()
    policy = BalancePolicy()
    events = iter(recording.events())
    pending = next(events, None)
    previous = None
    for frame in recording.frames:
        related = []
        while pending is not None and pending["t_ns"] <= frame.stamp:
            related.append(pending)
            pending = next(events, None)
        image = frame.load()
        result = detector.analyze(image, frame.stamp)
        if result["visual_state"] == "gap":
            policy.choose(None)
        reading = balance.analyze(image)
        direction = policy.choose(reading, frame.stamp / 1_000_000_000)
        hud = inventory = None
        if reading:
            game_state = "minigame"
        else:
            view = ui.normalize(image)
            result_view = ui.result(view)
            if result_view:
                game_state = result_view
            else:
                inventory = ui.inventory(view)
                hud = ui.hud(view) if not inventory else None
                game_state = (f"inventory_{inventory.tab or 'unknown'}" if inventory
                              else "energy_wait" if hud and hud.ready and hud.energy == 0
                              else "ready" if hud and hud.ready else "waiting" if hud else "unknown")
        state = result["visual_state"]
        current = (state, direction, game_state)
        if every or current != previous or related:
            decision = (f"hold_{direction}" if direction else "close_result" if game_state in ("caught","escaped")
                        else "release" if reading or previous and previous[2] == "minigame" else "observe")
            payload = {"t_ns": frame.stamp, "frame": frame.index, **result, "events": related,
                       "game_state": game_state, "decision": decision}
            if hud:
                payload["hud"] = {"energy": hud.energy, "rod": hud.rod, "bait": hud.bait}
            if inventory:
                payload["inventory"] = {"tab": inventory.tab, "count": inventory.count, "slots": inventory.slots}
            if reading is not None:
                height, width = image.shape[:2]
                x, y, w, h = reading.bar
                payload["balance"] = {"indicator": reading.indicator, "target": reading.target,
                                      "bar": [x / width, y / height, w / width, h / height]}
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        previous = current
    if pending is not None:
        from itertools import chain

        for event in chain([pending], events):
            stream.write(json.dumps({"t_ns": event["t_ns"], "event_after_last_frame": event}, ensure_ascii=False) + "\n")


def main() -> None:
    import sys

    parser = argparse.ArgumentParser(description="Offline rozbor obrazových změn v záznamu Fishao.")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--changes-only", action="store_true")
    args = parser.parse_args()
    try:
        recording = Recording(args.recording)
        if args.output:
            with args.output.open("x", encoding="utf-8") as destination:
                replay(recording, destination, every=not args.changes_only)
        else:
            replay(recording, sys.stdout, every=not args.changes_only)
    except (KeyError, OSError, ValueError) as exc:
        parser.exit(1, f"Rozbor se nepodařil. {exc}\n")


if __name__ == "__main__":
    main()

