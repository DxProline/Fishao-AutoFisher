"""Instalace závislostí a vytvoření zástupce aktuálního klonu."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import venv


def create_shortcut(root: Path) -> Path:
    from win32com.client import Dispatch

    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    icon = root / "assets" / "Tkinter.ico"
    launch = root / "application_start.py"
    if not all(path.is_file() for path in (pythonw, icon, launch)):
        raise RuntimeError("Chybí spouštěcí soubor nebo ikona.")
    shell = Dispatch("WScript.Shell")
    desktop = Path(shell.SpecialFolders("Desktop"))
    if not desktop.is_dir():
        raise RuntimeError("Plochu Windows se nepodařilo najít.")
    path = desktop / "Fishao AutoFisher.lnk"
    shortcut = shell.CreateShortcut(str(path))
    shortcut.TargetPath = str(pythonw)
    shortcut.Arguments = subprocess.list2cmdline([str(launch), "--autofish"])
    shortcut.WorkingDirectory = str(root)
    shortcut.IconLocation = f"{icon},0"
    shortcut.Description = "Fishao AutoFisher"
    shortcut.Save()
    if not path.is_file():
        raise RuntimeError("Zástupce se nepodařilo uložit.")
    return path


def main() -> None:
    if sys.platform != "win32" or sys.version_info[:2] != (3, 14) or sys.maxsize <= 2**32:
        raise SystemExit("Instalace vyžaduje 64bitový Python 3.14 pro Windows.")
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    python = environment / "Scripts" / "python.exe"
    if Path(sys.executable).resolve() == python.resolve():
        import cv2
        import mss
        import tkinter
        import win32ui

        print("Zástupce byl vytvořen:", create_shortcut(root))
        return
    if not python.is_file():
        venv.create(environment, with_pip=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements.txt")], check=True)
    subprocess.run([str(python), str(Path(__file__).resolve())], check=True)


if __name__ == "__main__":
    main()
