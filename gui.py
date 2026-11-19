from __future__ import annotations

import base64
import logging
import os
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import messagebox, ttk

import cv2

from assist import Assist
from bot import Auto
from hotkeys import Hotkeys
from recorder import NS, Recorder, Status
from window import Image

BG = "#101e28"
PANEL = "#1a303c"
INK = "#f1f7fa"
MUTED = "#adc7d2"
ACCENT = "#55dabc"


class GuiLog(logging.Handler):
    def __init__(self, destination: SimpleQueue[str]) -> None:
        super().__init__()
        self.destination = destination

    def emit(self, record: logging.LogRecord) -> None:
        self.destination.put(self.format(record))


class App:
    def __init__(self, root: tk.Tk, output_dir: Path, fps: int, *, assist: bool = False,
                 cast_once: bool = False, autofish: bool = False, xp_farm: bool = False,
                 image_format: str = "jpg", agent: bool = False) -> None:
        self.root = root
        self.output_dir = output_dir
        self.fps = fps
        self.image_format = image_format
        self.recorder: Recorder | Assist | None = None
        self.assist = assist
        self.cast_once = cast_once
        self.xp_farm = xp_farm
        self.agent = tk.BooleanVar(master=root, value=agent)
        self.background = tk.BooleanVar(master=root, value=False)
        self.autofish = autofish
        self.action_name = ("XP Farm" if xp_farm else "AutoFish") if autofish else "asistenci" if assist else "záznam"
        self.title = f"Fishao {self.action_name}" if autofish else "Fishao Asistence" if assist else "Fishao Záznamník"
        self.closing = False
        self.actions: SimpleQueue[str] = SimpleQueue()
        self.messages: SimpleQueue[str] = SimpleQueue()
        self.status = tk.StringVar(value="Připraveno.")
        self.folder = tk.StringVar(value=str(output_dir))
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_index = -1
        self.preview_frame: Image | None = None
        self.shortcut_warning: str | None = None
        self.log_handlers = self._log_setup()
        self.hotkeys = Hotkeys(lambda: self.actions.put("toggle"), self._emergency)
        self._build()
        self.hotkeys.start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(33, self._poll)

    def _log_setup(self) -> tuple[logging.Handler, logging.Handler]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logs = self.output_dir / "logs"
        logs.mkdir(exist_ok=True)
        self.log = logging.getLogger("fishao")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        disk = RotatingFileHandler(logs / "fishao.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        disk.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        view = GuiLog(self.messages)
        view.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        for handler in (disk, view):
            self.log.addHandler(handler)
        return disk, view

    def _build(self) -> None:
        self.root.title(self.title)
        self.root.geometry("1040x740")
        self.root.minsize(690, 480)
        self.root.configure(bg=BG)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Hero.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Status.TLabel", foreground=ACCENT, font=("Segoe UI", 11, "bold"))
        style.configure("TButton", background=PANEL, padding=(12, 7), borderwidth=0)
        style.map("TButton", background=[("active", "#295163")], foreground=[("disabled", "#88a2ab")])
        style.configure("Start.TButton", background=ACCENT, foreground=BG)
        style.map("Start.TButton", background=[("disabled", PANEL), ("active", "#8aebd5")])
        style.configure("Options.TCheckbutton", background=BG, foreground=INK, focuscolor=MUTED,
                        indicatorcolor=PANEL, bordercolor=MUTED, padding=(2, 3))
        style.map("Options.TCheckbutton",
                  background=[("active", PANEL), ("focus", BG)],
                  foreground=[("disabled", MUTED)],
                  indicatorcolor=[("disabled", PANEL), ("selected", ACCENT)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(15, 7))
        style.map("TNotebook.Tab", background=[("selected", "#284c5e")], foreground=[("selected", INK)])
        style.configure("Card.TFrame", background=PANEL)
        style.configure("Card.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Metric.TLabel", background=PANEL, foreground=INK, font=("Segoe UI", 15, "bold"))

        shell = ttk.Frame(self.root, padding=16)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text=self.title, style="Hero.TLabel").pack(anchor="w")
        ttk.Label(shell, textvariable=self.status, style="Status.TLabel", wraplength=620).pack(anchor="w", pady=(3, 9))

        bar = ttk.Frame(shell)
        bar.pack(fill="x", pady=(0, 10))
        self.toggle_button = ttk.Button(bar, command=self.toggle, style="Start.TButton")
        self.toggle_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(bar, text="Zastavit · F9", command=self.stop)
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="Otevřít složku", command=self.open_folder).pack(side="left")
        mode = ("1 zkušební hod" if self.cast_once else "Ruční nahození") if self.assist else f"Cíl {self.fps} sn./s"
        if self.autofish:
            mode = "Do ručního vypnutí"
        ttk.Label(bar, text=mode, style="Muted.TLabel").pack(side="right")
        if self.autofish:
            self.agent_option = ttk.Checkbutton(shell, text="Agent · postup, vybavení a mise",
                                                variable=self.agent, style="Options.TCheckbutton")
            self.agent_option.pack(anchor="w", pady=(0, 10))
            self.background_option = ttk.Checkbutton(shell, text="Pracovat v překrytém okně hry",
                                                     variable=self.background, style="Options.TCheckbutton",
                                                     command=self._update_preview_hint)
            self.background_option.pack(anchor="w", pady=(0, 10))

        cards = ttk.Frame(shell)
        cards.pack(fill="x", pady=(0, 10))
        self.metrics: dict[str, tk.StringVar] = {}
        labels = ("Minihry", "Vzorky", "Sledování sn./s", "Čas snímání") if self.assist else (
            "Uloženo", "Vynecháno", "Průměr sn./s", "Čas snímání", "Vstupy",
        )
        if self.autofish:
            labels = ("Hody", "Chyceno", "Uteklo", "Sledování sn./s", "Čas snímání")
        for idx, label in enumerate(labels):
            cards.columnconfigure(idx, weight=1, uniform="cards")
            card = ttk.Frame(cards, style="Card.TFrame", padding=(10, 5))
            card.grid(row=0, column=idx, sticky="nsew", padx=(0, 6 if idx < len(labels) - 1 else 0))
            ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w")
            self.metrics[label] = tk.StringVar(value="—")
            ttk.Label(card, textvariable=self.metrics[label], style="Metric.TLabel").pack(anchor="w")

        tabs = ttk.Notebook(shell)
        tabs.pack(fill="both", expand=True)
        preview_tab = ttk.Frame(tabs)
        log_tab = ttk.Frame(tabs)
        tabs.add(preview_tab, text="Ladicí náhled")
        tabs.add(log_tab, text="Události")
        self.canvas = tk.Canvas(preview_tab, bg="#0b151d", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas_message = self.canvas.create_text(
            0, 0, fill=MUTED, font=("Segoe UI", 11),
        )
        self._update_preview_hint()
        self.canvas_image = self.canvas.create_image(0, 0)
        self.canvas.bind("<Configure>", self._resize_preview)
        self.log_view = tk.Text(log_tab, bg=PANEL, fg=INK, relief="flat", wrap="word",
                                state="disabled", padx=10, pady=8)
        scroll = ttk.Scrollbar(log_tab, command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_view.pack(fill="both", expand=True)
        ttk.Label(shell, textvariable=self.folder, style="Muted.TLabel", wraplength=630).pack(anchor="w", pady=(8, 0))
        self._buttons()

    def _resize_preview(self, event: tk.Event) -> None:
        for item in (self.canvas_message, self.canvas_image):
            self.canvas.coords(item, event.width // 2, event.height // 2)
        self._render_preview()

    def _update_preview_hint(self) -> None:
        hint = ("nech otevřené okno FISHAO - Reborn" if self.background.get()
                else "přepni do FISHAO - Reborn")
        self.canvas.itemconfigure(self.canvas_message, text=f"Spusť {self.action_name} a {hint}.")

    def _render_preview(self) -> None:
        image = self.preview_frame
        if image is None:
            return
        h, w = image.shape[:2]
        scale = min(1.0, 920/w, 520/h, self.canvas.winfo_width() / w, self.canvas.winfo_height() / h)
        if scale < 1.0:
            image = cv2.resize(image, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            return
        self.preview_photo = tk.PhotoImage(master=self.root, data=base64.b64encode(encoded).decode("ascii"), format="png")
        self.canvas.itemconfigure(self.canvas_image, image=self.preview_photo)
        self.canvas.itemconfigure(self.canvas_message, state="hidden")

    def _buttons(self) -> None:
        recorder = self.recorder
        active = recorder is not None and not recorder.snapshot().finished
        stopping = active and recorder.stopping
        label = f"Začít {self.action_name}"
        if active:
            label = "Pokračovat" if recorder.paused else "Pozastavit"
        if self.shortcut_warning is None:
            label += " · F8"
        self.toggle_button.configure(text=label, state="disabled" if stopping or self.closing else "normal")
        self.stop_button.configure(text="Zastavit" if self.shortcut_warning else "Zastavit · F9",
                                   state="normal" if active and not stopping else "disabled")
        if self.autofish:
            self.agent_option.configure(state="disabled" if active else "normal")
            self.background_option.configure(state="disabled" if active else "normal")

    def toggle(self) -> None:
        if self.closing:
            return
        if self.recorder is not None and not self.recorder.snapshot().finished:
            self.recorder.toggle_pause()
        else:
            self.preview_index = -1
            self.preview_photo = None
            self.preview_frame = None
            self.canvas.itemconfigure(self.canvas_image, image="")
            self.canvas.itemconfigure(self.canvas_message, state="normal")
            if self.autofish:
                self.recorder = Auto(xp_farm=self.xp_farm, agent=self.agent.get(),
                                     background=self.background.get())
            else:
                self.recorder = Assist(self.cast_once) if self.assist else Recorder(
                    self.output_dir, self.fps, image_format=self.image_format)
            self.recorder.start()
        self._buttons()

    def _emergency(self) -> None:
        current = self.recorder
        if current is not None:
            current.stop()
        self.actions.put("stop")

    def stop(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
        self._buttons()

    def open_folder(self) -> None:
        state = self.recorder.snapshot() if self.recorder is not None else None
        try:
            os.startfile(str(state.folder if state and state.folder else self.output_dir))
        except OSError as exc:
            messagebox.showerror("Výstupní složka", str(exc), parent=self.root)

    def _show(self, state: Status) -> None:
        self.status.set(state.text)
        if state.input_warning:
            self.status.set(state.text + " Záznam vstupů nedostupný. Podrobnosti v Událostech.")
        self.folder.set(str(state.folder or self.output_dir))
        sec = state.active_ns // NS
        fps = state.saved * NS / state.active_ns if state.active_ns else None
        for label, value in {
            "Uloženo": str(state.saved),
            "Vynecháno": str(state.dropped + state.missed),
            "Průměr sn./s": f"{fps:.1f}" if fps is not None else "—",
            "Sledování sn./s": f"{state.tracking_fps:.1f}" if state.tracking_fps is not None else "—",
            "Čas snímání": f"{sec // 3600:02}:{sec // 60 % 60:02}:{sec % 60:02}",
            "Vstupy": str(state.inputs),
            "Minihry": str(state.saved),
            "Vzorky": str(state.captured),
            "Hody": str(state.casts),
            "Chyceno": str(state.caught),
            "Uteklo": str(state.escaped),
        }.items():
            if label in self.metrics:
                self.metrics[label].set(value)
        if state.preview is not None and state.preview[0] != self.preview_index:
            self.preview_index, self.preview_frame = state.preview
            self._render_preview()

    def _events(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.messages.get_nowait())
            except Empty:
                break
        if lines:
            self.log_view.configure(state="normal")
            self.log_view.insert("end", "\n".join(lines) + "\n")
            last = int(self.log_view.index("end-1c").split(".")[0])
            if last > 250:
                self.log_view.delete("1.0", f"{last - 249}.0")
            self.log_view.see("end")
            self.log_view.configure(state="disabled")

    def _poll(self) -> None:
        toggles = 0
        while True:
            try:
                action = self.actions.get_nowait()
            except Empty:
                break
            if action == "stop":
                toggles = 0
                self.stop()
            else:
                toggles += 1
        for _ in range(toggles):
            self.toggle()

        if self.hotkeys.error and self.hotkeys.error != self.shortcut_warning:
            self.log.warning("%s", self.hotkeys.error)
            self.shortcut_warning = self.hotkeys.error
        if self.recorder is not None:
            self._show(self.recorder.snapshot())
        elif self.shortcut_warning:
            self.status.set(self.shortcut_warning + " Použij tlačítka v okně.")
        self._events()
        self._buttons()
        if self.closing and not self.hotkeys.is_alive() and (self.recorder is None or self.recorder.snapshot().finished):
            for handler in self.log_handlers:
                self.log.removeHandler(handler)
                handler.close()
            self.root.destroy()
        else:
            self.root.after(33, self._poll)

    def close(self) -> None:
        self.closing = True
        self.hotkeys.close()
        self.stop()
