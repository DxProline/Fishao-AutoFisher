from __future__ import annotations

from collections.abc import Callable

from game_input import GameInput


class Hotkeys(GameInput):
    def __init__(self, on_toggle: Callable[[], None], on_stop: Callable[[], None]) -> None:
        # Tento posluchač zpracuje jen F8 a F9. Herní vstupy patří do relace.
        super().__init__("", lambda: False, lambda *_: None,
                         shortcuts={0x77: on_toggle, 0x78: on_stop}, mouse=False)
        self.name = "FishaoHotkeys"

