from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
Box = tuple[int, int, int, int]
Config = dict[str, Any]

STEPS: dict[str, tuple[str, str]] = {
    "ready": ("Připraveno k nahození", "Označ rozpoznatelný prvek viditelný jen před nahozením."),
    "waiting": ("Čekání na záběr", "Označ nápis nebo značku, která se objeví po nahození."),
    "bite": ("Záběr (volitelné)", "Označ signál záběru. Použij, pokud hra vyžaduje potvrzení záběru."),
    "minigame": ("Minihra", "Označ pevný rám, ikonu nebo jiný prvek jedinečný pro minihru."),
    "arena": ("Oblast minihry", "Označ celou dráhu, v níž se pohybuje cíl i ukazatel."),
    "target": ("Cíl minihry", "Označ malou kontrastní část cíle. Vynech proměnlivé pozadí."),
    "cursor": ("Ukazatel minihry", "Označ malou kontrastní část ovládaného ukazatele."),
    "caught": ("Ryba chycena", "Označ nápis nebo ikonu úspěšného úlovku."),
    "escaped": ("Ryba utekla", "Označ prvek, který jednoznačně pozná neúspěch."),
    "dialog": ("Neočekávaný dialog (volitelné)", "Označ rozpoznatelnou část dialogu, při kterém se má zastavit."),
    "energy": ("Energie vyčerpaná (volitelné)", "Při energii 0 označ celé pole čísla i s místem pro další číslice."),
    "bait": ("Návnada vyčerpaná (volitelné)", "Při návnadě 0 označ celé pole čísla i s místem pro další číslice."),
}
# Kalibrace je volitelný zrychlovač. Základním vstupem je záznam manuálního hraní.
REQUIRED: set[str] = set()
KEY_NAMES = {"space", "enter", "tab", "esc", "escape", "left", "right", "up", "down",
             "backspace", "shift", "ctrl", "alt", "home", "end", "pageup", "pagedown",
             "insert", "delete"}


def load_config() -> Config:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    validate(cfg)
    return cfg


def save_config(cfg: Config) -> None:
    validate(cfg)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def validate(cfg: Config) -> None:
    if not isinstance(cfg["window_title"], str) or not cfg["window_title"].strip():
        raise ValueError("Chybí název okna hry.")
    size = cfg["window_size"]
    if size is not None and (len(size) != 2 or any(type(x) is not int or x < 1 for x in size)):
        raise ValueError("Neplatný rozměr okna.")
    for name in ("cast", "hook", "left", "right", "result"):
        key = cfg["keys"][name]
        optional = name in {"cast", "hook", "result"}
        if not isinstance(key, str) or not (
            (not key and optional) or key in KEY_NAMES or re.fullmatch(r"[a-z0-9]", key)
        ):
            raise ValueError(f"Neplatná klávesa pro {name}. Použij například space, enter nebo e.")
    if cfg["keys"]["left"] == cfg["keys"]["right"]:
        raise ValueError("Směry musí používat dvě různé klávesy.")
    timing = cfg["timing"]
    for key in ("capture_s", "reaction_s", "hold_s", "minigame_gap_s"):
        lo, hi = timing[key]
        if not (math.isfinite(lo) and math.isfinite(hi) and 0 < lo < hi):
            raise ValueError(f"Čas {key} vyžaduje dvě různé kladné hodnoty.")
    for key in ("startup_s", "calibration_wait_s", "unknown_s", "stuck_s", "result_s", "max_runtime_s"):
        if not math.isfinite(timing[key]) or timing[key] <= 0:
            raise ValueError(f"Čas {key} musí být kladný.")
    if timing["startup_s"] < 2:
        raise ValueError("Čas na přepnutí do hry musí být alespoň 2 s.")
    for key in ("state", "marker", "resource", "ambiguity"):
        if not 0 < cfg["thresholds"][key] < 1:
            raise ValueError(f"Práh {key} musí ležet mezi 0 a 1.")
    if not 0 < cfg["dead_zone_ratio"] < 0.5:
        raise ValueError("dead_zone_ratio musí ležet mezi 0 a 0,5.")
    for key in ("max_retries", "max_fish"):
        if type(cfg["limits"][key]) is not int or cfg["limits"][key] < 1:
            raise ValueError(f"{key} musí být kladné celé číslo.")
    if type(cfg["stable_frames"]) is not int or cfg["stable_frames"] < 2:
        raise ValueError("stable_frames musí být celé číslo alespoň 2.")
    if any(type(cfg["resources"][key]) is not bool for key in ("energy", "bait")) or type(cfg["debug"]) is not bool:
        raise ValueError("Přepínače resources a debug musí mít hodnotu true nebo false.")
    for name, entry in cfg["templates"].items():
        if name not in STEPS or size is None:
            raise ValueError(f"Neplatný krok kalibrace: {name}.")
        x, y, w, h = entry["box"]
        if any(type(v) is not int for v in (x, y, w, h)) or min(x, y) < 0 or min(w, h) < 4 or x + w > size[0] or y + h > size[1]:
            raise ValueError(f"Neplatná oblast šablony {name}.")
        if name != "arena":
            template_path(entry["path"])


def template_path(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    if not path.is_relative_to((ROOT / "templates").resolve()) or path.suffix.lower() != ".png":
        raise ValueError("Šablony musí být soubory PNG ve složce templates.")
    return path


def for_run(cfg: Config) -> Config:
    validate(cfg)
    # Ruční kalibrace se pro spuštění nevyžaduje. Může zlepšit detekci, ale není závislostí.
    arena = cfg["templates"].get("arena")
    if arena:
        ax, ay, aw, ah = arena["box"]
        for name in ("target", "cursor"):
            if name not in cfg["templates"]:
                continue
            x, y, w, h = cfg["templates"][name]["box"]
            if x < ax or y < ay or x + w > ax + aw or y + h > ay + ah:
                raise ValueError(f"{STEPS[name][0]} musí ležet uvnitř oblasti minihry.")
    return deepcopy(cfg)

