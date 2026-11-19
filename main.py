from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Fishao Záznamník vyžaduje Windows.")
    parser = argparse.ArgumentParser(description="Záznam ručního hraní FISHAO - Reborn.")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "recordings")
    parser.add_argument("--fps", type=int, choices=range(1, 31), default=15, metavar="1–30")
    parser.add_argument("--assist", action="store_true", help="Spustí asistenci jedné minihry.")
    parser.add_argument("--autofish", "--auto", dest="autofish", action="store_true",
                        help="Spustí AutoFish, který loví do ručního vypnutí.")
    parser.add_argument("--max-casts", type=int, choices=range(1,501), metavar="1-500",
                        help="Volitelný limit pro krátký test AutoFish.")
    parser.add_argument("--test-cast", action="store_true", help="S asistencí umožní jeden zkušební klik do vody.")
    args = parser.parse_args()
    if args.test_cast and not args.assist:
        parser.error("--test-cast vyžaduje --assist")
    if args.assist and args.autofish:
        parser.error("Zvol --assist nebo --autofish.")
    if args.max_casts is not None and not args.autofish:
        parser.error("--max-casts vyžaduje --autofish.")

    # Win32, Tkinter i MSS musí používat fyzické souřadnice při škálování Windows.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    try:
        from gui import App

        App(root, args.output_dir.resolve(), args.fps, assist=args.assist, cast_once=args.test_cast,
            autofish=args.autofish, auto_casts=args.max_casts)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("Fishao Záznamník", f"Aplikaci nelze otevřít.\n{exc}", parent=root)
        root.destroy()
        raise SystemExit(1) from exc
    root.mainloop()


if __name__ == "__main__":
    main()

