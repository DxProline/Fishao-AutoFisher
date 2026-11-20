from __future__ import annotations

import logging
import os
import tkinter as tk
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, SimpleQueue
from time import perf_counter
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2

from assist import Assist
from bot import Auto
from destinations import CURRENT, Destination, Marker, TravelSettings
from game_ui import Interface, Point
from hotkeys import Hotkeys
from recorder import NS, Recorder, Status
from version import VERSION
from window import Image

BG = "#f2f4f6"
PANEL = "#ffffff"
INK = "#19232d"
MUTED = "#53616f"
LINE = "#d7dde3"
ACCENT = "#235b70"
PREVIEW_BG = "#1b232b"
PREVIEW_INK = "#cad3dc"


class GuiLog(logging.Handler):
    def __init__(self, destination: SimpleQueue[str]) -> None:
        super().__init__()
        self.destination = destination

    def emit(self, record: logging.LogRecord) -> None:
        self.destination.put(self.format(record))


class App:
    def __init__(self, root: tk.Tk, output_dir: Path, fps: int, *, assist: bool = False,
                 cast_once: bool = False, autofish: bool = False, xp_farm: bool = False,
                 image_format: str = "jpg", agent: bool = False, auto_casts: int | None = None) -> None:
        self.root = root
        self.output_dir = output_dir
        self.fps = fps
        self.image_format = image_format
        self.recorder: Recorder | Assist | None = None
        self.assist = assist
        self.cast_once = cast_once
        self.xp_farm = xp_farm
        self.auto_casts = auto_casts
        self.agent = tk.BooleanVar(master=root, value=agent)
        self.join_tournaments = tk.BooleanVar(master=root, value=False)
        self.background = tk.BooleanVar(master=root, value=False)
        self.refill_bread = tk.BooleanVar(master=root, value=True)
        self.rotate_ponds = tk.BooleanVar(master=root, value=False)
        self.fishing_place = tk.StringVar(master=root, value=CURRENT)
        self.places_folder = output_dir / "destinations"
        self.places = Destination.load_all(self.places_folder) if autofish else []
        self.autofish = autofish
        self.action_name = "lov" if autofish else "asistenci" if assist else "záznam"
        if autofish:
            self.title = "Fishao Agent" if agent else "Fishao XP Farm" if xp_farm else "Fishao AutoFish"
        else:
            self.title = "Fishao Asistence" if assist else "Fishao Záznamník"
        self.closing = False
        self.actions: SimpleQueue[str] = SimpleQueue()
        self.messages: SimpleQueue[str] = SimpleQueue()
        self.status = tk.StringVar(value="Připraveno.")
        self.folder = tk.StringVar(value=str(output_dir))
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_index = -1
        self.preview_frame: Image | None = None
        self.preview_times: deque[float] = deque()
        self.shortcut_warning: str | None = None
        self.log_handlers = self._log_setup()
        self.hotkeys = Hotkeys(lambda: self.actions.put("toggle"), self._emergency)
        self._build()
        self.hotkeys.start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(8 if isinstance(self.recorder, Assist) and not self.recorder.snapshot().finished else 16,
                            self._poll)

    def _log_setup(self) -> tuple[logging.Handler, logging.Handler]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logs = self.output_dir / "runtime" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
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

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Surface.TFrame", background=PANEL)
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL)
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Section.TLabel", background=PANEL, font=("Segoe UI", 10, "bold"))
        style.configure("Metric.TLabel", background=PANEL, font=("Segoe UI", 11, "bold"))
        style.configure("TSeparator", background=LINE)
        style.configure("TButton", background=PANEL, padding=(12, 6), bordercolor=LINE,
                        lightcolor=PANEL, darkcolor=PANEL, focuscolor=ACCENT)
        style.map("TButton", background=[("disabled", BG), ("pressed", "#dce5eb"), ("active", "#e9eef2")],
                  foreground=[("disabled", "#6d7984")])
        style.configure("Start.TButton", background=ACCENT, foreground=PANEL, bordercolor=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=PANEL)
        style.map("Start.TButton", background=[("disabled", BG), ("pressed", "#184455"), ("active", "#1c4e62")],
                  foreground=[("disabled", "#6d7984")], bordercolor=[("disabled", LINE)])
        style.configure("Options.TCheckbutton", background=PANEL, foreground=INK, focuscolor=ACCENT,
                        indicatorbackground=PANEL, indicatorforeground=ACCENT, padding=(0, 4))
        style.map("Options.TCheckbutton",
                  background=[("active", "#eef3f6")], foreground=[("disabled", "#6d7984")],
                  indicatorbackground=[("disabled", BG), ("pressed", "#dce5eb")])
        style.configure("TNotebook", background=BG, bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL,
                        tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(14, 7),
                        bordercolor=LINE, focuscolor=ACCENT)
        style.map("TNotebook.Tab", background=[("selected", PANEL), ("active", "#e7ecf0")],
                  foreground=[("selected", INK)])

    def _build(self) -> None:
        self.root.title(self.title)
        self.root.geometry("1040x680")
        self.root.minsize(780, 480)
        self.root.configure(bg=BG)
        self._configure_styles()

        shell = ttk.Frame(self.root, padding=(16, 12))
        shell.pack(fill="both", expand=True)

        bar = ttk.Frame(shell)
        bar.pack(fill="x", pady=(0, 12))
        ttk.Label(bar, text=self.title, style="Title.TLabel").pack(side="left")
        ttk.Label(bar, text=VERSION, style="Muted.TLabel").pack(side="left", padx=(9, 0))
        ttk.Button(bar, text="Otevřít složku", command=self.open_folder).pack(side="right")
        self.stop_button = ttk.Button(bar, command=self.stop, width=13)
        self.stop_button.pack(side="right", padx=(0, 6))
        self.toggle_button = ttk.Button(bar, command=self.toggle, style="Start.TButton", width=20)
        self.toggle_button.pack(side="right", padx=(0, 6))

        workspace = ttk.Frame(shell)
        workspace.pack(fill="both", expand=True)
        workspace.columnconfigure(0, weight=1)
        workspace.columnconfigure(1, minsize=240)
        workspace.rowconfigure(0, weight=1)
        tabs = ttk.Notebook(workspace)
        tabs.grid(row=0, column=0, sticky="nsew")
        tabs.enable_traversal()
        preview_tab = ttk.Frame(tabs, style="Surface.TFrame")
        log_tab = ttk.Frame(tabs, style="Surface.TFrame")
        tabs.add(preview_tab, text="Náhled hry")
        tabs.add(log_tab, text="Události")
        if self.autofish:
            places_tab = ttk.Frame(tabs, style="Surface.TFrame", padding=16)
            tabs.add(places_tab, text="Lovná místa")
            ttk.Label(places_tab, text="Místo lovu").pack(anchor="w")
            self.place_picker = ttk.Combobox(places_tab, state="readonly", textvariable=self.fishing_place,
                                             values=[place.name for place in self.places], width=33)
            self.place_picker.pack(anchor="w", pady=(4, 12))
            self.travel_options = []
            for label, variable in (
                    ("Vyzvednout bezplatný chléb u Jamieho", self.refill_bread),
                    ("Střídat viditelné uložené břehy na stejné mapě", self.rotate_ponds)):
                option = ttk.Checkbutton(places_tab, text=label, variable=variable, style="Options.TCheckbutton")
                option.pack(anchor="w", pady=3)
                self.travel_options.append(option)
            add = ttk.Button(places_tab, text="Přidat lovné místo…", command=self._add_place)
            add.pack(anchor="w", pady=(12, 6))
            self.travel_options.append(add)
            ttk.Label(places_tab, wraplength=440, justify="left", style="Muted.TLabel",
                      text="Vybranou mapu otevře při spuštění. Pak loví ve stejné oblasti. "
                           "Při odpočtu v Trout Farm dostává přednost lov.").pack(anchor="w")
        self.canvas = tk.Canvas(preview_tab, bg=PREVIEW_BG, highlightthickness=0,
                                borderwidth=0, width=1, height=1)
        self.canvas.pack(fill="both", expand=True)
        self.canvas_message = self.canvas.create_text(0, 0, fill=PREVIEW_INK, font=("Segoe UI", 10), justify="center")
        self._update_preview_hint()
        self.canvas_image = self.canvas.create_image(0, 0)
        self.canvas.bind("<Configure>", self._resize_preview)
        self.log_view = tk.Text(log_tab, bg=PANEL, fg=INK, relief="flat", wrap="word",
                                font=("Consolas", 10), state="disabled", padx=12, pady=8,
                                highlightthickness=0, width=1, height=1)
        path = ttk.Label(log_tab, textvariable=self.folder, style="PanelMuted.TLabel", padding=(12, 6), width=1)
        path.pack(side="bottom", fill="x")
        ttk.Separator(log_tab).pack(side="bottom", fill="x")
        scroll = ttk.Scrollbar(log_tab, command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_view.pack(fill="both", expand=True)
        self._build_sidebar(workspace)

        ttk.Separator(shell).pack(fill="x", pady=(12, 0))
        status = ttk.Label(shell, textvariable=self.status, padding=(0, 7, 0, 0))
        status.pack(fill="x")
        status.bind("<Configure>", lambda event: status.configure(wraplength=event.width))
        self._buttons()

    def _build_sidebar(self, workspace: ttk.Frame) -> None:
        sidebar = ttk.Frame(workspace, style="Surface.TFrame", padding=(14, 12))
        sidebar.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        if self.autofish:
            ttk.Label(sidebar, text="Nastavení lovu", style="Section.TLabel").pack(anchor="w", pady=(0, 3))
            self.agent_option = ttk.Checkbutton(sidebar, text="Mise, postup a vybavení",
                                                variable=self.agent, style="Options.TCheckbutton",
                                                command=self._buttons)
            self.agent_option.pack(fill="x")
            self.tournament_option = ttk.Checkbutton(
                sidebar, text="Účastnit se turnajů", variable=self.join_tournaments,
                style="Options.TCheckbutton",
            )
            self.tournament_option.pack(fill="x", padx=(14, 0))
            self.background_option = ttk.Checkbutton(sidebar, text="Lovit v překrytém okně",
                                                     variable=self.background, style="Options.TCheckbutton",
                                                     command=self._update_preview_hint)
            self.background_option.pack(fill="x")
            ttk.Separator(sidebar).pack(fill="x", pady=12)

        ttk.Label(sidebar, text="Statistiky", style="Section.TLabel").pack(anchor="w", pady=(0, 5))
        if not self.autofish:
            mode = ("Jeden zkušební hod" if self.cast_once else "Ruční nahození") if self.assist else f"Cíl {self.fps} sn./s"
            ttk.Label(sidebar, text=mode, style="PanelMuted.TLabel").pack(anchor="w", pady=(0, 8))
        self.metrics: dict[str, tk.StringVar] = {}
        labels = ("Minihry", "Vzorky", "Detekce sn./s", "Náhled sn./s", "Čas snímání") if self.assist else (
            "Uloženo", "Vynecháno", "Průměr sn./s", "Čas snímání", "Vstupy",
        )
        if self.autofish:
            labels = ("Hody", "Chyceno", "Uteklo", "Detekce sn./s", "Náhled sn./s", "Čas snímání")
        for label in labels:
            row = ttk.Frame(sidebar, style="Surface.TFrame", padding=(0, 4))
            row.pack(fill="x")
            ttk.Label(row, text=label, style="PanelMuted.TLabel").pack(side="left")
            self.metrics[label] = tk.StringVar(master=self.root, value="–")
            ttk.Label(row, textvariable=self.metrics[label], style="Metric.TLabel",
                      anchor="e", width=9).pack(side="right")

    def _pick_marker(self, title: str) -> Marker | None:
        filename = filedialog.askopenfilename(parent=self.root, title=title,
                                              filetypes=[("Snímky", "*.png *.jpg *.jpeg")])
        if not filename:
            return None
        frame = Interface.normalize(Interface._image(Path(filename)))
        thumbnail = cv2.resize(frame, (820, 442), interpolation=cv2.INTER_AREA)
        pixels = cv2.cvtColor(thumbnail, cv2.COLOR_BGR2RGB)
        popup = tk.Toplevel(self.root)
        popup.title(title)
        popup.transient(self.root)
        picture = tk.PhotoImage(master=popup, data=b"P6\n820 442\n255\n" + pixels.tobytes(), format="ppm")
        canvas = tk.Canvas(popup, width=820, height=442, highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=picture)
        point: Point | None = None

        def select(event: tk.Event) -> None:
            nonlocal point
            point = (event.x / 820, event.y / 442)
            popup.destroy()

        canvas.bind("<Button-1>", select)
        popup.grab_set()
        self.root.wait_window(popup)
        return Marker.capture(frame, point) if point is not None else None

    def _add_place(self) -> None:
        name = simpledialog.askstring("Lovné místo", "Název lovného místa", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if any(name.casefold() == place.name.casefold() for place in self.places):
            messagebox.showerror("Lovné místo", "Tento název už existuje.", parent=self.root)
            return
        area = simpledialog.askstring("Lovné místo", "Název mapy (stejný pro rybníky na stejné mapě)",
                                      parent=self.root)
        if not area or not area.strip():
            return
        area = area.strip()
        area = next((p.area for p in self.places if p.area.casefold() == area.casefold()), area)
        try:
            map_marker = self._pick_marker("Snímek otevřené mapy · klikni na vstup do oblasti")
            if map_marker is None:
                return
            bank = self._pick_marker("Snímek po příchodu na mapu · klikni na výrazný bod na suchém břehu")
            if bank is None:
                return
            Destination(name, area, map_marker, bank).save(self.places_folder)
        except (OSError, ValueError, cv2.error) as exc:
            messagebox.showerror("Lovné místo", str(exc), parent=self.root)
            return
        self.places = Destination.load_all(self.places_folder)
        self.place_picker.configure(values=[place.name for place in self.places])
        self.fishing_place.set(name)

    def _resize_preview(self, event: tk.Event) -> None:
        for item in (self.canvas_message, self.canvas_image):
            self.canvas.coords(item, event.width // 2, event.height // 2)
        self.canvas.itemconfigure(self.canvas_message, width=max(1, event.width - 40))
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
        h, w = image.shape[:2]
        pixels = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        data = f"P6\n{w} {h}\n255\n".encode("ascii") + pixels.tobytes()
        if self.preview_photo is None:
            self.preview_photo = tk.PhotoImage(master=self.root, data=data, format="ppm")
            self.canvas.itemconfigure(self.canvas_image, image=self.preview_photo)
            self.canvas.itemconfigure(self.canvas_message, state="hidden")
        else:
            self.preview_photo.configure(data=data, format="ppm")

    def _buttons(self) -> None:
        recorder = self.recorder
        active = recorder is not None and not recorder.snapshot().finished
        stopping = active and recorder.stopping
        label = f"Spustit {self.action_name}"
        if active:
            label = "Pokračovat" if recorder.paused else "Pozastavit"
        if self.shortcut_warning is None:
            label += " · F8"
        self.toggle_button.configure(text=label, state="disabled" if stopping or self.closing else "normal")
        self.stop_button.configure(text="Zastavit" if self.shortcut_warning else "Zastavit · F9",
                                   state="normal" if active and not stopping else "disabled")
        if self.autofish:
            self.agent_option.configure(state="disabled" if active else "normal")
            self.tournament_option.configure(state="disabled" if active or not self.agent.get() else "normal")
            self.background_option.configure(state="disabled" if active else "normal")
            for option in self.travel_options:
                option.configure(state="disabled" if active else "normal")
            self.place_picker.configure(state="disabled" if active else "readonly")

    def toggle(self) -> None:
        if self.closing:
            return
        if self.recorder is not None and not self.recorder.snapshot().finished:
            self.recorder.toggle_pause()
        else:
            self.preview_index = -1
            self.preview_photo = None
            self.preview_frame = None
            self.preview_times.clear()
            self.canvas.itemconfigure(self.canvas_image, image="")
            self.canvas.itemconfigure(self.canvas_message, state="normal")
            if self.autofish:
                place = next(p for p in self.places if p.name == self.fishing_place.get())
                travel = TravelSettings(place, tuple(self.places), self.refill_bread.get(),
                                        self.rotate_ponds.get())
                self.recorder = Auto(self.auto_casts, xp_farm=self.xp_farm, agent=self.agent.get(),
                                     background=self.background.get(),
                                     join_tournaments=self.join_tournaments.get(), travel=travel)
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
        now = perf_counter()
        if state.preview is not None and state.preview[0] != self.preview_index:
            self.preview_index, self.preview_frame = state.preview
            self._render_preview()
            self.preview_times.append(now)
        while self.preview_times and now - self.preview_times[0] > 1:
            self.preview_times.popleft()
        times = self.preview_times
        preview_fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else None
        for label, value in {
            "Uloženo": str(state.saved),
            "Vynecháno": str(state.dropped + state.missed),
            "Průměr sn./s": f"{fps:.1f}" if fps is not None else "—",
            "Detekce sn./s": f"{state.tracking_fps:.1f}" if state.tracking_fps is not None else "—",
            "Náhled sn./s": f"{preview_fps:.1f}" if preview_fps is not None else "—",
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
            self.root.after(8 if isinstance(self.recorder, Assist) and not self.recorder.snapshot().finished else 16,
                            self._poll)

    def close(self) -> None:
        self.closing = True
        self.hotkeys.close()
        self.stop()
