from __future__ import annotations

import os
import shutil
import subprocess
import threading
import tkinter as tk
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseWindow = TkinterDnD.Tk
    HAS_DND = True
except ImportError:
    DND_FILES = None
    BaseWindow = tk.Tk
    HAS_DND = False

from .analysis import analyze_file, collect_inputs, write_analysis_report
from . import __version__
from .decision_summary import DecisionEntry, build_decision_summary
from .models import (
    AnalysisResult,
    CodecProfile,
    CropMargins,
    JobConfig,
    ProcessingMode,
    ProcessingResult,
)
from .preview import generate_preview
from .profiles import load_series_profile, save_series_profile
from .processing import process_file
from .tools import CancelledError, FieldFixError, ProgressDetails, Toolchain


BG = "#181a1f"
SURFACE = "#22252b"
SURFACE_ALT = "#2b2f37"
TEXT = "#e8eaf0"
MUTED = "#a9afbb"
ACCENT = "#5b8def"
ACCENT_ACTIVE = "#76a2f3"
BORDER = "#3a3f49"


class ToolTip:
    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget = widget
        self.text = text
        self.pending: str | None = None
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self.pending = self.widget.after(550, self._show)

    def _cancel(self) -> None:
        if self.pending:
            self.widget.after_cancel(self.pending)
            self.pending = None

    def _show(self) -> None:
        self.pending = None
        if self.window or not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.window.geometry(f"+{x}+{y}")
        tk.Label(
            self.window,
            text=self.text,
            justify=tk.LEFT,
            wraplength=380,
            background=SURFACE_ALT,
            foreground=TEXT,
            relief=tk.SOLID,
            borderwidth=1,
            padx=9,
            pady=7,
        ).pack()

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self.window:
            self.window.destroy()
            self.window = None


@dataclass
class QueueItem:
    path: Path
    analysis: AnalysisResult | None = None
    override: ProcessingMode = ProcessingMode.AUTO
    status: str = "Not analyzed"
    output: Path | None = None
    result: ProcessingResult | None = None


def process_button_text(items: list[QueueItem]) -> str:
    """Describe whether the current processing scope still needs analysis."""
    return "Process" if items and all(item.analysis is not None for item in items) else "Analyze + Process"


class FieldFixWindow(BaseWindow):  # type: ignore[misc,valid-type]
    def __init__(self) -> None:
        super().__init__()
        self.title("DVD FieldFix")
        self.geometry("1240x760")
        self.minsize(1000, 640)
        self._apply_dark_theme()
        self.tools = Toolchain.discover()
        self.items: dict[str, QueueItem] = {}
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.preview_directories: list[Path] = []
        self.tooltips: list[ToolTip] = []
        self._build_ui()
        self.after(50, _set_dark_titlebar, self)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _apply_dark_theme(self) -> None:
        self.configure(background=BG)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("TFrame", background=BG)
        style.configure("TLabelframe", background=BG, foreground=TEXT, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TButton", background=SURFACE_ALT, foreground=TEXT, bordercolor=BORDER, padding=(9, 5))
        style.map(
            "TButton",
            background=[("active", ACCENT), ("pressed", ACCENT_ACTIVE), ("disabled", SURFACE)],
            foreground=[("disabled", "#6f7580")],
        )
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("disabled", "#6f7580")])
        style.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER)
        style.configure(
            "TSpinbox",
            fieldbackground=SURFACE,
            background=SURFACE_ALT,
            foreground=TEXT,
            insertcolor=TEXT,
            arrowcolor=TEXT,
            bordercolor=BORDER,
        )
        style.map(
            "TSpinbox",
            fieldbackground=[("disabled", SURFACE), ("readonly", SURFACE)],
            foreground=[("disabled", MUTED), ("readonly", TEXT)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE,
            background=SURFACE_ALT,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=BORDER,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", SURFACE)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "Treeview",
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=TEXT,
            rowheight=26,
            bordercolor=BORDER,
        )
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=SURFACE_ALT, foreground=TEXT, bordercolor=BORDER, padding=6)
        style.map("Treeview.Heading", background=[("active", "#353a44")])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=SURFACE, bordercolor=BORDER)
        self.option_add("*TCombobox*Listbox.background", SURFACE)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _tooltip(self, widget: tk.Misc, text: str) -> None:
        self.tooltips.append(ToolTip(widget, text))

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill=tk.X)
        add_files = ttk.Button(toolbar, text="Add files", command=self._add_files)
        add_files.pack(side=tk.LEFT, padx=3)
        self._tooltip(add_files, "Add one or more Matroska files to the queue.")
        add_folder = ttk.Button(toolbar, text="Add folder", command=self._add_folder)
        add_folder.pack(side=tk.LEFT, padx=3)
        self._tooltip(add_folder, "Add every MKV found directly inside a folder.")
        remove = ttk.Button(toolbar, text="Remove", command=self._remove_selected)
        remove.pack(side=tk.LEFT, padx=3)
        self._tooltip(remove, "Remove selected entries from the queue. Source files are never deleted.")
        check_setup = ttk.Button(toolbar, text="Check setup", command=self._doctor)
        check_setup.pack(side=tk.LEFT, padx=(12, 3))
        self._tooltip(
            check_setup,
            "Verify FFmpeg, encoders, VapourSynth, QTGMC and optional restoration plugins.",
        )
        save_report = ttk.Button(toolbar, text="Save report", command=self._save_report)
        save_report.pack(side=tk.LEFT, padx=3)
        self._tooltip(save_report, "Save completed detection results as a JSON report.")
        decision_summary = ttk.Button(
            toolbar, text="Decision summary", command=self._show_decision_summary
        )
        decision_summary.pack(side=tk.LEFT, padx=3)
        self._tooltip(
            decision_summary,
            "Explain every series-wide and per-file decision, including evidence and validation.",
        )
        load_profile = ttk.Button(toolbar, text="Load series profile", command=self._load_profile)
        load_profile.pack(side=tk.LEFT, padx=(12, 3))
        self._tooltip(
            load_profile,
            "Load one consistent codec, CRF and restoration setup for the whole series.",
        )
        save_profile = ttk.Button(toolbar, text="Save series profile", command=self._save_profile)
        save_profile.pack(side=tk.LEFT, padx=3)
        self._tooltip(
            save_profile,
            "Save the current quality and restoration settings for reuse on every episode.",
        )
        about = ttk.Button(toolbar, text="About", command=self._about)
        about.pack(side=tk.RIGHT, padx=3)
        self._tooltip(about, "Show the version and application icon provenance.")
        self.profile_var = tk.StringVar(value="Series profile: unsaved settings")
        ttk.Label(toolbar, textvariable=self.profile_var).pack(side=tk.RIGHT, padx=12)

        columns = ("file", "classification", "confidence", "action", "crop", "status")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="extended")
        headings = {
            "file": "File",
            "classification": "Detection",
            "confidence": "Confidence",
            "action": "Action",
            "crop": "Detected crop",
            "status": "Status",
        }
        widths = {
            "file": 350,
            "classification": 140,
            "confidence": 80,
            "action": 105,
            "crop": 135,
            "status": 260,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self._update_process_button_label, add="+")
        if HAS_DND and DND_FILES:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._drop_files)

        options = ttk.LabelFrame(self, text="Series encoding profile", padding=8)
        options.pack(fill=tk.X, padx=10, pady=4)
        ttk.Label(options, text="Codec:").grid(row=0, column=0, sticky=tk.W)
        self.codec_var = tk.StringVar(value=CodecProfile.H264.value)
        self.codec_box = ttk.Combobox(
            options,
            textvariable=self.codec_var,
            values=[item.value for item in CodecProfile],
            state="readonly",
            width=10,
        )
        self.codec_box.grid(row=0, column=1, padx=(4, 14), sticky=tk.W)
        self.codec_box.bind("<<ComboboxSelected>>", self._codec_changed)
        self._tooltip(
            self.codec_box,
            "H.264 maximizes compatibility; HEVC 10-bit improves compression and gradients; FFV1 is mathematically lossless but very large.",
        )
        self.crf_label = ttk.Label(options, text="CRF:")
        self.crf_label.grid(row=0, column=2, sticky=tk.W)
        self.crf_var = tk.StringVar(value="14")
        self.crf_spin = ttk.Spinbox(
            options,
            from_=0,
            to=51,
            increment=0.5,
            textvariable=self.crf_var,
            width=6,
        )
        self.crf_spin.grid(row=0, column=3, padx=(4, 14), sticky=tk.W)
        self._tooltip(
            self.crf_spin,
            "Constant-quality target. Lower means higher quality and larger files. 14 is the quality-first series default. FFV1 ignores CRF.",
        )
        ttk.Label(options, text="Override selected:").grid(row=0, column=4, sticky=tk.W)
        self.mode_var = tk.StringVar(value=ProcessingMode.AUTO.value)
        mode_box = ttk.Combobox(
            options,
            textvariable=self.mode_var,
            values=[item.value for item in ProcessingMode],
            state="readonly",
            width=12,
        )
        mode_box.grid(row=0, column=5, padx=4)
        self._tooltip(
            mode_box,
            "Keep auto unless a reviewed file needs an explicit copy, restoration, field-match, hybrid50 or QTGMC decision.",
        )
        apply_override = ttk.Button(options, text="Apply", command=self._apply_override)
        apply_override.grid(row=0, column=6, padx=(0, 14))
        self._tooltip(apply_override, "Apply this temporal-mode override only to selected queue entries.")
        ttk.Label(options, text="Parallel jobs:").grid(row=0, column=7, sticky=tk.W)
        self.jobs_var = tk.StringVar(value="1")
        jobs_box = ttk.Combobox(
            options,
            textvariable=self.jobs_var,
            values=("1", "2"),
            state="readonly",
            width=4,
        )
        jobs_box.grid(row=0, column=8, padx=4, sticky=tk.W)
        self._tooltip(
            jobs_box,
            "Use 2 for better total CPU utilization on SD x265 queues. Use 1 when RAM is limited or for a single episode.",
        )

        ttk.Label(options, text="Manual crop L:T:R:B:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.crop_var = tk.StringVar()
        crop_entry = ttk.Entry(options, textvariable=self.crop_var, width=13)
        crop_entry.grid(row=1, column=1, padx=4, sticky=tk.W, pady=(8, 0))
        self._tooltip(
            crop_entry,
            "Even left:top:right:bottom margins. Manual values override auto-crop and preserve the original display aspect ratio.",
        )
        self.auto_crop_var = tk.BooleanVar(value=False)
        auto_crop = ttk.Checkbutton(options, text="Auto crop", variable=self.auto_crop_var)
        auto_crop.grid(row=1, column=2, columnspan=2, padx=10, pady=(8, 0), sticky=tk.W)
        self._tooltip(
            auto_crop,
            "Remove only stable black borders found across seven samples. Disabled by default; preview before processing.",
        )
        self.denoise_var = tk.BooleanVar(value=False)
        self.dotcrawl_var = tk.BooleanVar(value=False)
        self.restoration_status_var = tk.StringVar(value="Optional cleanup: preservation default")
        restoration_status = ttk.Label(options, textvariable=self.restoration_status_var)
        restoration_status.grid(
            row=1, column=4, columnspan=3, padx=10, pady=(8, 0), sticky=tk.W
        )
        self._tooltip(
            restoration_status,
            "Denoise and composite-artifact filters remain off unless an advanced override is explicitly loaded or selected.",
        )
        restoration_overrides = ttk.Button(
            options,
            text="Advanced cleanup…",
            command=self._advanced_restoration,
        )
        restoration_overrides.grid(row=1, column=7, columnspan=2, pady=(8, 0), sticky=tk.E)
        self._tooltip(
            restoration_overrides,
            "Expert fallback only. Automatic per-series restoration requires calibrated evidence and is not guessed silently.",
        )

        ttk.Label(options, text="Output (blank = _fixed):").grid(
            row=2, column=0, sticky=tk.W, pady=(8, 0)
        )
        self.output_var = tk.StringVar()
        output_entry = ttk.Entry(options, textvariable=self.output_var)
        output_entry.grid(
            row=2, column=1, columnspan=7, sticky=tk.EW, padx=4, pady=(8, 0)
        )
        self._tooltip(
            output_entry,
            "Destination for every episode in this queue. Blank creates a _fixed folder beside each source.",
        )
        browse_output = ttk.Button(options, text="Browse…", command=self._choose_output)
        browse_output.grid(row=2, column=8, pady=(8, 0))
        self._tooltip(browse_output, "Choose a common destination folder for this series.")
        options.columnconfigure(7, weight=1)

        actions = ttk.Frame(self, padding=(10, 6))
        actions.pack(fill=tk.X)
        self.analyze_button = ttk.Button(actions, text="Analyze", command=self._start_analysis)
        self.analyze_button.pack(side=tk.LEFT, padx=3)
        self._tooltip(self.analyze_button, "Inspect every selected file in full without creating video output.")
        self.preview_button = ttk.Button(actions, text="Preview", command=self._start_preview)
        self.preview_button.pack(side=tk.LEFT, padx=3)
        self._tooltip(self.preview_button, "Generate an original/corrected comparison for one selected file.")
        self.process_button = ttk.Button(
            actions, text="Analyze + Process", command=self._start_processing
        )
        self.process_button.pack(side=tk.LEFT, padx=3)
        self._tooltip(
            self.process_button,
            "No separate analysis is required. Missing analysis runs first, then each output is processed and fully validated.",
        )
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=12)
        self._tooltip(self.cancel_button, "Stop the active analysis or encode and remove its partial output.")
        open_output = ttk.Button(actions, text="Open output", command=self._open_output)
        open_output.pack(side=tk.LEFT, padx=3)
        self._tooltip(open_output, "Open the selected file's completed output folder.")
        self.progress = ttk.Progressbar(actions, maximum=100)
        self.progress.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(20, 0))
        self._tooltip(self.progress, "Overall progress across the selected queue.")

        self.status_var = tk.StringVar(
            value="Drag MKVs into the queue." if HAS_DND else "Add MKVs using the buttons above."
        )
        ttk.Label(self, textvariable=self.status_var, padding=(10, 4)).pack(fill=tk.X)

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(filetypes=[("Matroska", "*.mkv"), ("All files", "*.*")])
        self._add_paths(paths)

    def _add_folder(self) -> None:
        directory = filedialog.askdirectory()
        if directory:
            self._add_paths([directory])

    def _drop_files(self, event: object) -> None:
        data = getattr(event, "data", "")
        self._add_paths(self.tk.splitlist(data))

    def _add_paths(self, raw_paths: object) -> None:
        try:
            paths = collect_inputs(raw_paths, recursive=False)  # type: ignore[arg-type]
        except FieldFixError as exc:
            messagebox.showerror("DVD FieldFix", str(exc))
            return
        for path in paths:
            key = str(path)
            if key in self.items:
                continue
            self.items[key] = QueueItem(path)
            self.tree.insert("", tk.END, iid=key, values=(path.name, "—", "—", "auto", "—", "Not analyzed"))
        self._update_process_button_label()
        self.status_var.set(f"{len(self.items)} file(s) in the queue")

    def _remove_selected(self) -> None:
        for key in self.tree.selection():
            self.tree.delete(key)
            self.items.pop(key, None)
        self._update_process_button_label()

    def _selected_items(self, require_one: bool = False) -> list[QueueItem]:
        keys = list(self.tree.selection())
        if not keys and not require_one:
            keys = list(self.items)
        return [self.items[key] for key in keys if key in self.items]

    def _update_process_button_label(self, _event: object = None) -> None:
        if hasattr(self, "process_button"):
            self.process_button.configure(text=process_button_text(self._selected_items()))

    def _apply_override(self) -> None:
        mode = ProcessingMode(self.mode_var.get())
        for key in self.tree.selection():
            item = self.items[key]
            item.override = mode
            self._refresh_item(item)

    def _choose_output(self) -> None:
        directory = filedialog.askdirectory()
        if directory:
            self.output_var.set(directory)

    def _advanced_restoration(self) -> None:
        window = tk.Toplevel(self)
        window.title("Advanced cleanup overrides")
        window.resizable(False, False)
        window.configure(background=BG)
        window.after(50, _set_dark_titlebar, window)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text=(
                "These are expert overrides, not universal improvements. Leave them off unless "
                "a preview and representative series samples show a real defect. Automatic "
                "restoration will only be enabled after its detector is calibrated against "
                "different real DVDs."
            ),
            wraplength=520,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(0, 12))
        denoise = ttk.Checkbutton(
            frame,
            text="Force light denoise (hqdn3d=1:1:3:3)",
            variable=self.denoise_var,
            command=self._update_restoration_status,
        )
        denoise.pack(anchor=tk.W, pady=4)
        self._tooltip(
            denoise,
            "May improve compression but can remove intended grain and fine texture.",
        )
        dotcrawl = ttk.Checkbutton(
            frame,
            text="Force one spatial DotKillS pass",
            variable=self.dotcrawl_var,
            command=self._update_restoration_status,
        )
        dotcrawl.pack(anchor=tk.W, pady=4)
        self._tooltip(
            dotcrawl,
            "Targets dot crawl and rainbow artifacts after field reconstruction; may alter legitimate fine patterns.",
        )
        ttk.Button(frame, text="Close", command=window.destroy).pack(anchor=tk.E, pady=(12, 0))

    def _update_restoration_status(self) -> None:
        enabled: list[str] = []
        if self.denoise_var.get():
            enabled.append("forced light denoise")
        if self.dotcrawl_var.get():
            enabled.append("forced DotKillS")
        self.restoration_status_var.set(
            "Optional cleanup: " + (", ".join(enabled) if enabled else "preservation default")
        )

    def _codec_changed(self, _event: object = None) -> None:
        if self.codec_var.get() == CodecProfile.FFV1.value:
            self.crf_label.grid_remove()
            self.crf_spin.grid_remove()
        else:
            self.crf_label.grid()
            self.crf_spin.grid()
            self.crf_spin.configure(state=tk.NORMAL)

    def _load_profile(self) -> None:
        source = filedialog.askopenfilename(
            title="Load series profile",
            filetypes=[("DVD FieldFix series profile", "*.json"), ("All files", "*.*")],
        )
        if not source:
            return
        try:
            profile = load_series_profile(source)
        except FieldFixError as exc:
            messagebox.showerror("Invalid series profile", str(exc))
            return
        config = profile.config
        self.codec_var.set(config.codec.value)
        self.crf_var.set(f"{config.crf:g}")
        margins = config.crop
        self.crop_var.set(
            f"{margins.left}:{margins.top}:{margins.right}:{margins.bottom}"
            if margins.enabled
            else ""
        )
        self.auto_crop_var.set(config.auto_crop)
        self.denoise_var.set(config.denoise)
        self.dotcrawl_var.set(config.dotcrawl)
        self._update_restoration_status()
        self.jobs_var.set(str(profile.parallel_jobs))
        self.profile_var.set(f"Series profile: {profile.name}")
        self._codec_changed()
        self.status_var.set(f"Loaded series profile: {profile.name}")

    def _save_profile(self) -> None:
        try:
            config = self._config()
        except ValueError as exc:
            messagebox.showerror("Invalid options", str(exc))
            return
        destination = filedialog.asksaveasfilename(
            title="Save series profile",
            defaultextension=".json",
            filetypes=[("DVD FieldFix series profile", "*.json")],
            initialfile="series-profile.json",
        )
        if not destination:
            return
        name = Path(destination).stem
        try:
            save_series_profile(destination, name, config, int(self.jobs_var.get()))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not save series profile", str(exc))
            return
        self.profile_var.set(f"Series profile: {name}")
        self.status_var.set(f"Saved series profile: {destination}")

    def _about(self) -> None:
        messagebox.showinfo(
            "About DVD FieldFix",
            f"DVD FieldFix {__version__}\n\n"
            "Intelligent field reconstruction and conservative DVD restoration.\n\n"
            "Feather icon: this is Tcl/Tk's built-in default window icon. "
            "DVD FieldFix does not currently bundle or claim authorship of it.",
        )

    def _config(self, mode: ProcessingMode = ProcessingMode.AUTO) -> JobConfig:
        return JobConfig(
            codec=CodecProfile(self.codec_var.get()),
            crf=float(self.crf_var.get()),
            mode=mode,
            output_directory=self.output_var.get().strip() or None,
            crop=CropMargins.parse(self.crop_var.get().strip()),
            auto_crop=self.auto_crop_var.get(),
            denoise=self.denoise_var.get(),
            dotcrawl=self.dotcrawl_var.get(),
        )

    def _start_analysis(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showinfo("DVD FieldFix", "Add at least one MKV.")
            return
        self._run_worker(lambda: self._analysis_worker(items))

    def _analysis_worker(self, items: list[QueueItem]) -> None:
        for index, item in enumerate(items, 1):
            if self.cancel_event.is_set():
                raise CancelledError("Analysis cancelled")
            self._set_item_status(item, "Analyzing")

            def callback(
                value: float,
                stage: str,
                details: ProgressDetails | None = None,
                current: int = index,
            ) -> None:
                overall = ((current - 1) + value) / len(items)
                self.after(
                    0,
                    self._set_progress,
                    overall,
                    f"{item.path.name}: {stage}",
                    details,
                )

            item.analysis = analyze_file(
                item.path,
                self.tools,
                cancel_event=self.cancel_event,
                progress=callback,
            )
            item.status = item.analysis.reason
            self.after(0, self._refresh_item, item)

    def _start_processing(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showinfo("DVD FieldFix", "Add at least one MKV.")
            return
        try:
            config = self._config()
        except ValueError as exc:
            messagebox.showerror("Invalid options", str(exc))
            return
        jobs = int(self.jobs_var.get())
        self._run_worker(lambda: self._processing_worker(items, config, jobs))

    def _processing_worker(
        self,
        items: list[QueueItem],
        base_config: JobConfig,
        jobs: int,
    ) -> None:
        missing = [item for item in items if item.analysis is None]
        for index, item in enumerate(missing, 1):
            if self.cancel_event.is_set():
                raise CancelledError("Processing cancelled")
            self._set_item_status(item, "Analyzing")

            def analysis_progress(
                value: float,
                stage: str,
                details: ProgressDetails | None = None,
                current: int = index,
                current_item: QueueItem = item,
            ) -> None:
                overall = ((current - 1) + value) / max(1, len(missing))
                self.after(
                    0,
                    self._set_progress,
                    overall,
                    f"{current_item.path.name}: {stage}",
                    details,
                )

            item.analysis = analyze_file(
                item.path,
                self.tools,
                cancel_event=self.cancel_event,
                progress=analysis_progress,
            )
            self.after(0, self._refresh_item, item)

        progress_values = {str(item.path): 0.0 for item in items}
        progress_lock = threading.Lock()

        def process_one(item: QueueItem) -> None:
            if self.cancel_event.is_set():
                raise CancelledError("Processing cancelled")
            assert item.analysis is not None
            config = replace(base_config, mode=item.override)
            self._set_item_status(item, "Processing")

            def callback(
                value: float,
                stage: str,
                details: ProgressDetails | None = None,
            ) -> None:
                with progress_lock:
                    progress_values[str(item.path)] = value
                    overall = sum(progress_values.values()) / len(items)
                self.after(
                    0,
                    self._set_progress,
                    overall,
                    f"{item.path.name}: {stage}",
                    details,
                )

            result = process_file(
                item.analysis,
                config,
                self.tools,
                cancel_event=self.cancel_event,
                progress=callback,
            )
            item.result = result
            item.output = Path(result.output)
            item.status = "Already completed" if result.skipped else "Completed and validated"
            with progress_lock:
                progress_values[str(item.path)] = 1.0
            self.after(0, self._refresh_item, item)

        with ThreadPoolExecutor(max_workers=min(jobs, len(items))) as executor:
            futures = [executor.submit(process_one, item) for item in items]
            for future in as_completed(futures):
                future.result()

    def _start_preview(self) -> None:
        selected = self._selected_items(require_one=True)
        if len(selected) != 1:
            messagebox.showinfo("DVD FieldFix", "Select exactly one file.")
            return
        item = selected[0]
        try:
            config = self._config(item.override)
        except ValueError as exc:
            messagebox.showerror("Invalid options", str(exc))
            return
        self._run_worker(lambda: self._preview_worker(item, config))

    def _preview_worker(self, item: QueueItem, config: JobConfig) -> None:
        if item.analysis is None:
            self._set_item_status(item, "Analyzing for preview")
            item.analysis = analyze_file(item.path, self.tools, cancel_event=self.cancel_event)
        original, corrected, directory = generate_preview(item.analysis, config, self.tools)
        self.preview_directories.append(directory)
        self.after(0, self._show_preview, item, original, corrected)

    def _show_preview(self, item: QueueItem, original: Path, corrected: Path) -> None:
        window = tk.Toplevel(self)
        window.title(f"Preview — {item.path.name}")
        window.configure(background=BG)
        window.after(50, _set_dark_titlebar, window)
        frame = ttk.Frame(window, padding=8)
        frame.pack(fill=tk.BOTH, expand=True)
        original_image = tk.PhotoImage(file=str(original))
        corrected_image = tk.PhotoImage(file=str(corrected))
        ttk.Label(frame, text="Original").grid(row=0, column=0)
        ttk.Label(frame, text="Corrected").grid(row=0, column=1)
        left = ttk.Label(frame, image=original_image)
        right = ttk.Label(frame, image=corrected_image)
        left.grid(row=1, column=0, padx=4)
        right.grid(row=1, column=1, padx=4)
        buttons = ttk.Frame(frame, padding=(0, 8, 0, 0))
        buttons.grid(row=2, column=0, columnspan=2)
        save_original = ttk.Button(
            buttons,
            text="Save original PNG…",
            command=lambda: self._save_preview_image(
                original, f"{item.path.stem}-original.png"
            ),
        )
        save_original.pack(side=tk.LEFT, padx=4)
        self._tooltip(save_original, "Export the displayed source frame as a lossless PNG.")
        save_corrected = ttk.Button(
            buttons,
            text="Save corrected PNG…",
            command=lambda: self._save_preview_image(
                corrected, f"{item.path.stem}-corrected.png"
            ),
        )
        save_corrected.pack(side=tk.LEFT, padx=4)
        self._tooltip(save_corrected, "Export the displayed corrected frame as a lossless PNG.")
        save_both = ttk.Button(
            buttons,
            text="Save both PNGs…",
            command=lambda: self._save_preview_pair(item, original, corrected),
        )
        save_both.pack(side=tk.LEFT, padx=4)
        self._tooltip(save_both, "Export both frames into one chosen folder.")
        window._images = (original_image, corrected_image)  # type: ignore[attr-defined]

    def _save_preview_image(self, source: Path, suggested_name: str) -> None:
        destination = filedialog.asksaveasfilename(
            title="Save preview frame",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
            initialfile=suggested_name,
        )
        if destination:
            shutil.copy2(source, destination)
            self.status_var.set(f"Saved preview frame: {destination}")

    def _save_preview_pair(
        self,
        item: QueueItem,
        original: Path,
        corrected: Path,
    ) -> None:
        directory = filedialog.askdirectory(title="Choose a folder for both preview frames")
        if not directory:
            return
        destination = Path(directory)
        original_output = destination / f"{item.path.stem}-original.png"
        corrected_output = destination / f"{item.path.stem}-corrected.png"
        collisions = [path for path in (original_output, corrected_output) if path.exists()]
        if collisions and not messagebox.askyesno(
            "Replace preview images?",
            "One or both preview PNGs already exist. Replace them?",
        ):
            return
        shutil.copy2(original, original_output)
        shutil.copy2(corrected, corrected_output)
        self.status_var.set(f"Saved both preview frames to {destination}")

    def _doctor(self) -> None:
        self.status_var.set("Checking the processing setup…")

        def worker() -> None:
            report = self.tools.doctor(deep_qtgmc=True)
            lines = [f"{'OK' if check.ok else 'FAIL'} — {check.name}: {check.detail}" for check in report.checks]
            self.after(0, messagebox.showinfo, "Setup check", "\n\n".join(lines))
            self.after(0, self.status_var.set, "Setup check completed")

        threading.Thread(target=worker, daemon=True).start()

    def _decision_summary_text(self) -> str:
        config = self._config()
        entries = [
            DecisionEntry(
                analysis=item.analysis,
                override=item.override,
                status=item.status,
                source=item.path,
                result=item.result,
            )
            for item in self.items.values()
        ]
        profile_name = self.profile_var.get().removeprefix("Series profile: ")
        return build_decision_summary(
            entries,
            config,
            parallel_jobs=int(self.jobs_var.get()),
            profile_name=profile_name,
        )

    def _show_decision_summary(self) -> None:
        if not self.items:
            messagebox.showinfo("DVD FieldFix", "Add at least one MKV first.")
            return
        try:
            summary = self._decision_summary_text()
        except ValueError as exc:
            messagebox.showerror("Invalid options", str(exc))
            return
        window = tk.Toplevel(self)
        window.title("DVD FieldFix — Decision summary")
        window.geometry("980x720")
        window.configure(background=BG)
        window.after(50, _set_dark_titlebar, window)
        body = ttk.Frame(window, padding=10)
        body.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(body)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text = tk.Text(
            body,
            wrap=tk.WORD,
            background=SURFACE,
            foreground=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            relief=tk.FLAT,
            padx=12,
            pady=12,
            yscrollcommand=scrollbar.set,
        )
        text.insert("1.0", summary)
        text.configure(state=tk.DISABLED)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.configure(command=text.yview)
        actions = ttk.Frame(window, padding=(10, 0, 10, 10))
        actions.pack(fill=tk.X)
        copy_button = ttk.Button(
            actions,
            text="Copy to clipboard",
            command=lambda: self._copy_summary(summary),
        )
        copy_button.pack(side=tk.LEFT, padx=4)
        save_button = ttk.Button(
            actions,
            text="Save as text…",
            command=lambda: self._save_summary(summary),
        )
        save_button.pack(side=tk.LEFT, padx=4)
        ttk.Button(actions, text="Close", command=window.destroy).pack(side=tk.RIGHT, padx=4)

    def _copy_summary(self, summary: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(summary)
        self.update_idletasks()
        self.status_var.set("Decision summary copied to the clipboard")

    def _save_summary(self, summary: str) -> None:
        destination = filedialog.asksaveasfilename(
            title="Save decision summary",
            defaultextension=".txt",
            filetypes=[("Text document", "*.txt")],
            initialfile="dvd-fieldfix-decision-summary.txt",
        )
        if destination:
            Path(destination).write_text(summary, encoding="utf-8")
            self.status_var.set(f"Saved decision summary: {destination}")

    def _save_report(self) -> None:
        results = [item.analysis for item in self.items.values() if item.analysis]
        if not results:
            messagebox.showinfo("DVD FieldFix", "Analyze the files first.")
            return
        destination = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")], initialfile="fieldfix-report.json"
        )
        if destination:
            write_analysis_report(destination, results)  # type: ignore[arg-type]
            self.status_var.set(f"Report saved to {destination}")

    def _open_output(self) -> None:
        selected = self._selected_items(require_one=True)
        if selected and selected[0].output:
            directory = selected[0].output.parent
        elif self.output_var.get().strip():
            directory = Path(self.output_var.get().strip())
        elif selected:
            directory = selected[0].path.parent / "_fixed"
        else:
            return
        if not directory.exists():
            messagebox.showinfo("DVD FieldFix", "The output folder does not exist yet.")
            return
        if os.name == "nt":
            os.startfile(directory)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(directory)])

    def _run_worker(self, target: object) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("DVD FieldFix", "Another operation is already running.")
            return
        self.cancel_event.clear()
        self._busy(True)

        def wrapper() -> None:
            try:
                target()  # type: ignore[operator]
            except CancelledError as exc:
                self.after(0, self.status_var.set, str(exc))
            except (FieldFixError, ValueError) as exc:
                self.after(0, messagebox.showerror, "DVD FieldFix", str(exc))
                self.after(0, self.status_var.set, "Operation failed")
            except Exception as exc:  # defensive boundary for GUI callbacks
                self.after(0, messagebox.showerror, "Unexpected error", repr(exc))
                self.after(0, self.status_var.set, "Unexpected error")
            else:
                self.after(0, self.status_var.set, "Operation completed")
                self.after(0, self.progress.configure, {"value": 100})
            finally:
                self.after(0, self._busy, False)

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _set_item_status(self, item: QueueItem, status: str) -> None:
        item.status = status
        self.after(0, self._refresh_item, item)

    def _refresh_item(self, item: QueueItem) -> None:
        if not self.tree.exists(str(item.path)):
            return
        classification = item.analysis.classification.value if item.analysis else "—"
        confidence = f"{item.analysis.confidence:.0%}" if item.analysis else "—"
        action = item.override.value
        crop = item.analysis.crop_suggestion if item.analysis and item.analysis.crop_suggestion else "none"
        if item.override == ProcessingMode.AUTO and item.analysis and item.analysis.suggested_mode:
            action = item.analysis.suggested_mode.value
            restoration_selected = bool(
                self.crop_var.get().strip()
                or self.auto_crop_var.get()
                or self.denoise_var.get()
                or self.dotcrawl_var.get()
            )
            if action == ProcessingMode.COPY.value and restoration_selected:
                action = ProcessingMode.RESTORE.value
        self.tree.item(
            str(item.path),
            values=(item.path.name, classification, confidence, action, crop, item.status),
        )
        self._update_process_button_label()

    def _set_progress(
        self,
        value: float,
        status: str,
        details: ProgressDetails | None = None,
    ) -> None:
        self.progress.configure(value=max(0, min(100, value * 100)))
        metrics: list[str] = []
        if details:
            if details.current_frame is not None and details.total_frames is not None:
                metrics.append(f"{details.current_frame:,} / {details.total_frames:,} frames")
            if details.fps is not None:
                metrics.append(f"{details.fps:.2f} fps")
            if details.elapsed_seconds is not None:
                metrics.append(f"elapsed {_clock(details.elapsed_seconds)}")
                metrics.append(f"ETA {_clock(details.eta_seconds)}")
        self.status_var.set(status + ("  •  " + "  •  ".join(metrics) if metrics else ""))

    def _busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.analyze_button.configure(state=state)
        self.preview_button.configure(state=state)
        self.process_button.configure(state=state)
        self.cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)
        if not busy:
            self._update_process_button_label()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Cancelling…")

    def _close(self) -> None:
        self.cancel_event.set()
        for directory in self.preview_directories:
            shutil.rmtree(directory, ignore_errors=True)
        self.destroy()


def _set_dark_titlebar(window: tk.Misc) -> None:
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
            if result == 0:
                break
    except (AttributeError, OSError, tk.TclError):
        pass


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--:--"
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def main() -> None:
    app = FieldFixWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
