"""Read mission cards with Windows OCR without blocking the fishing loop."""
from __future__ import annotations

import base64
import logging
import subprocess
from threading import Thread

import cv2

from window import Image

_OCR = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Length -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($operation, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US'))
if ($null -eq $engine) { exit 2 }
while ($null -ne ($line = [Console]::ReadLine())) {
    $stream = [IO.MemoryStream]::new([Convert]::FromBase64String($line))
    $access = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
    try {
        $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($access)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime])
            [Console]::WriteLine($result.Text)
        } finally { $bitmap.Dispose() }
    } finally { $access.Dispose() }
}
"""


def read_cards(cards: list[Image]) -> list[str]:
    packets = []
    for card in cards:
        enlarged = cv2.resize(card, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        ok, pixels = cv2.imencode(".png", enlarged)
        if not ok:
            raise ValueError("Snímek mise nelze připravit.")
        packets.append(base64.b64encode(pixels))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _OCR],
        input=b"\n".join(packets) + b"\n", capture_output=True, timeout=25, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def log_cards(log: logging.Logger, pages: tuple[tuple[str, Image], ...]) -> None:
    def read() -> None:
        try:
            texts = read_cards([card for _, card in pages])
        except (OSError, ValueError, subprocess.SubprocessError):
            log.info("Přepis textu misí je nedostupný. Pokračuji podle rozpoznaných herních prvků.")
            return
        for (number, _), text in zip(pages, texts):
            if text.strip():
                log.info("Text mise %s: %s", number, text.strip())

    if pages:
        Thread(target=read, name="fishao-mission-text", daemon=True).start()
