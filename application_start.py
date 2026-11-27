"""Jednotný vstupní bod aplikace Fishao-AutoFisher."""

from __future__ import annotations

import argparse
import importlib
import sys
import tkinter as tk
from pathlib import Path


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()


class ApplicationStart:
    """Ověří prostředí a spustí požadovaný režim."""

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="Fishao-AutoFisher")
        parser.add_argument("--assist", action="store_true")
        parser.add_argument("--autofish", action="store_true")
        parser.add_argument("--xp-farm", action="store_true")
        parser.add_argument("--agent", action="store_true")
        parser.add_argument("--test-cast", action="store_true")
        parser.add_argument("--validate-only", action="store_true")
        parser.add_argument("--output-dir", type=Path, default=ROOT / "recordings")
        parser.add_argument("--fps", type=int, default=30)
        parser.add_argument("--image-format", choices=("jpg", "png"), default="jpg")
        parser.add_argument("--max-casts", type=int)
        return parser

    @staticmethod
    def validate(args: argparse.Namespace) -> list[str]:
        errors: list[str] = []
        if sys.platform != "win32":
            errors.append("Aplikace podporuje pouze Windows.")
        if args.fps < 1 or args.fps > 120:
            errors.append("--fps musí být mezi 1 a 120.")
        if args.max_casts is not None and not 1 <= args.max_casts <= 500:
            errors.append("--max-casts musí být mezi 1 a 500.")

        if not (ROOT / "config.json").is_file():
            errors.append("Chybí config.json.")
        else:
            try:
                from config import load_config
                load_config(ROOT / "config.json")
            except Exception as exc:
                errors.append(f"Neplatná konfigurace: {exc}")

        for package in ("cv2", "numpy", "mss", "win32gui", "win32api"):
            try:
                importlib.import_module(package)
            except Exception as exc:
                errors.append(f"Chybí nebo není možné načíst {package}: {exc}")

        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            if not args.output_dir.is_dir():
                errors.append("Výstupní složku se nepodařilo vytvořit.")
        except OSError as exc:
            errors.append(f"Výstupní složka není zapisovatelná: {exc}")

        if args.autofish or args.xp_farm or args.agent:
            reference = ROOT / "ui_reference"
            missing = sorted(reference.glob("*.png"))
            if not reference.is_dir() or len(missing) == 0:
                errors.append("Chybí složka ui_reference/*.png.")
        return errors

    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        args = cls.parser().parse_args(argv)
        modes = sum(bool(v) for v in (args.assist, args.autofish, args.xp_farm, args.agent))
        if modes > 1:
            raise SystemExit("Vyber pouze jeden režim: --assist, --autofish, --xp-farm nebo --agent.")

        errors = cls.validate(args)
        if args.validate_only:
            if errors:
                raise SystemExit("\n".join(errors))
            print("Ověření prostředí bylo úspěšné.")
            return 0
        if errors:
            raise SystemExit("\n".join(errors))

        from gui import App

        root = tk.Tk()
        root.title("Fishao")
        App(
            root,
            args.output_dir,
            args.fps,
            assist=args.assist,
            cast_once=args.test_cast,
            autofish=args.autofish or args.xp_farm or args.agent,
            xp_farm=args.xp_farm,
            image_format=args.image_format,
            agent=args.agent,
            auto_casts=args.max_casts,
        )
        root.mainloop()
        return 0


if __name__ == "__main__":
    raise SystemExit(ApplicationStart.main())
