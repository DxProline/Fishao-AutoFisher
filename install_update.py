"""Instalace přiložené aktualizace a zástupce bez ručního zadávání příkazů."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

from pywintypes import com_error
from win32com.client import Dispatch


def git(project: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(project), *arguments], check=True)


def release_version(project: Path) -> str | None:
    file = project / "version.py"
    if file.is_file():
        match = re.fullmatch(r'VERSION = "(\d+\.\d+\.\d+)"\s*', file.read_text(encoding="utf-8"))
        if match:
            return match[1]
    return None


def install(project: Path, desktop: Path, patch: Path, version: str) -> Path:
    interpreter = project / ".venv" / "Scripts" / "pythonw.exe"
    launch = project / "main.py"
    if not interpreter.is_file() or not launch.is_file():
        raise RuntimeError("Původní projekt nebo jeho Python nebyl nalezen.")
    current = release_version(project)
    if current != version:
        if current and tuple(map(int, current.split("."))) > tuple(map(int, version.split("."))):
            raise RuntimeError("V projektu je novější verze.")
        git(project, "apply", "--index", "--check", str(patch))
        git(project, "am", str(patch))
        if release_version(project) != version:
            raise RuntimeError("Po instalaci se nepodařilo potvrdit požadovanou verzi.")
    target = desktop / "Fishao Agent.lnk"
    shell = Dispatch("WScript.Shell")
    link = shell.CreateShortcut(str(target))
    link.TargetPath = str(interpreter)
    link.Arguments = subprocess.list2cmdline([str(launch), "--agent"])
    link.WorkingDirectory = str(project)
    link.IconLocation = f"{interpreter},0"
    link.Description = f"Fishao Agent {version}"
    link.Save()
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("patch", type=Path)
    parser.add_argument("version")
    parser.add_argument("--project", type=Path, default=Path.home() / "PycharmProjects" / "PythonProject6")
    args = parser.parse_args()
    try:
        desktop = Path(Dispatch("WScript.Shell").SpecialFolders("Desktop"))
        target = install(args.project.resolve(), desktop, args.patch.resolve(), args.version)
    except (OSError, RuntimeError, subprocess.CalledProcessError, com_error) as error:
        print(f"Aktualizace se nepodařila: {error}", file=sys.stderr)
        return 1
    print(f"Zástupce je připravený: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
