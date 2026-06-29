from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import ctypes
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
from .models import AnalysisResult, CodecProfile, CropMargins, JobConfig, ProcessingMode
from .preview import generate_preview
from .processing import process_file
from .tools import CancelledError, FieldFixError, Toolchain


BG = "#181a1f"
SURFACE = "#22252b"
SURFACE_ALT = "#2b2f37"
TEXT = "#e8eaf0"
MUTED = "#a9afbb"
ACCENT = "#5b8def"
ACCENT_ACTIVE = "#76a2f3"
BORDER = "#3a3f49"


@dataclass
class QueueItem:
    path: Path
    analysis: AnalysisResult | None = None
    override: ProcessingMode = ProcessingMode.AUTO
    status: str = "Not analyzed"
    output: Path | None = None


class FieldFixWindow(BaseWindow):  # type: ignore[misc,valid-type]
    def __init__(self) -> None:
        super().__init__()
        self.title("DVD FieldFix")
        self.geometry("1120x680")
        self.minsize(900, 560)
        self._apply_dark_theme()
        self.tools = Toolchain.discover()
        self.items: dict[str, QueueItem] = {}
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.preview_directories: list[Path] = []
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

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Add files", command=self._add_files).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Add folder", command=self._add_folder).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Remove", command=self._remove_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Doctor", command=self._doctor).pack(side=tk.LEFT, padx=12)
        ttk.Button(toolbar, text="Save report", command=self._save_report).pack(side=tk.LEFT, padx=3)

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
        if HAS_DND and DND_FILES:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._drop_files)

        options = ttk.LabelFrame(self, text="Options", padding=8)
        options.pack(fill=tk.X, padx=10, pady=4)
        ttk.Label(options, text="Codec:").grid(row=0, column=0, sticky=tk.W)
        self.codec_var = tk.StringVar(value=CodecProfile.H264.value)
        ttk.Combobox(
            options,
            textvariable=self.codec_var,
            values=[item.value for item in CodecProfile],
            state="readonly",
            width=12,
        ).grid(row=0, column=1, padx=(4, 14))
        ttk.Label(options, text="Override selected:").grid(row=0, column=2, sticky=tk.W)
        self.mode_var = tk.StringVar(value=ProcessingMode.AUTO.value)
        ttk.Combobox(
            options,
            textvariable=self.mode_var,
            values=[item.value for item in ProcessingMode],
            state="readonly",
            width=12,
        ).grid(row=0, column=3, padx=4)
        ttk.Button(options, text="Apply", command=self._apply_override).grid(row=0, column=4, padx=(0, 14))
        ttk.Label(options, text="Manual crop L:T:R:B:").grid(row=0, column=5, sticky=tk.W)
        self.crop_var = tk.StringVar()
        ttk.Entry(options, textvariable=self.crop_var, width=13).grid(row=0, column=6, padx=4)
        self.auto_crop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Auto crop", variable=self.auto_crop_var).grid(row=0, column=7, padx=10)
        self.denoise_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Light denoise", variable=self.denoise_var).grid(row=1, column=8, padx=10, pady=(8, 0))

        ttk.Label(options, text="Output (blank = _fixed):").grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        self.output_var = tk.StringVar()
        ttk.Entry(options, textvariable=self.output_var).grid(
            row=1, column=2, columnspan=5, sticky=tk.EW, padx=4, pady=(8, 0)
        )
        ttk.Button(options, text="Browse…", command=self._choose_output).grid(row=1, column=7, pady=(8, 0))
        options.columnconfigure(6, weight=1)

        actions = ttk.Frame(self, padding=(10, 6))
        actions.pack(fill=tk.X)
        self.analyze_button = ttk.Button(actions, text="Analyze", command=self._start_analysis)
        self.analyze_button.pack(side=tk.LEFT, padx=3)
        self.preview_button = ttk.Button(actions, text="Preview", command=self._start_preview)
        self.preview_button.pack(side=tk.LEFT, padx=3)
        self.process_button = ttk.Button(actions, text="Process", command=self._start_processing)
        self.process_button.pack(side=tk.LEFT, padx=3)
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=12)
        ttk.Button(actions, text="Open output", command=self._open_output).pack(side=tk.LEFT, padx=3)
        self.progress = ttk.Progressbar(actions, maximum=100)
        self.progress.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(20, 0))

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
        self.status_var.set(f"{len(self.items)} file(s) in the queue")

    def _remove_selected(self) -> None:
        for key in self.tree.selection():
            self.tree.delete(key)
            self.items.pop(key, None)

    def _selected_items(self, require_one: bool = False) -> list[QueueItem]:
        keys = list(self.tree.selection())
        if not keys and not require_one:
            keys = list(self.items)
        return [self.items[key] for key in keys if key in self.items]

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

    def _config(self, mode: ProcessingMode = ProcessingMode.AUTO) -> JobConfig:
        return JobConfig(
            codec=CodecProfile(self.codec_var.get()),
            mode=mode,
            output_directory=self.output_var.get().strip() or None,
            crop=CropMargins.parse(self.crop_var.get().strip()),
            auto_crop=self.auto_crop_var.get(),
            denoise=self.denoise_var.get(),
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

            def callback(value: float, stage: str, current: int = index) -> None:
                overall = ((current - 1) + value) / len(items)
                self.after(0, self._set_progress, overall, f"{item.path.name}: {stage}")

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
        self._run_worker(lambda: self._processing_worker(items, config))

    def _processing_worker(self, items: list[QueueItem], base_config: JobConfig) -> None:
        for index, item in enumerate(items, 1):
            if self.cancel_event.is_set():
                raise CancelledError("Processing cancelled")
            if item.analysis is None:
                self._set_item_status(item, "Analyzing")
                item.analysis = analyze_file(item.path, self.tools, cancel_event=self.cancel_event)
            config = replace(base_config, mode=item.override)
            self._set_item_status(item, "Processing")

            def callback(value: float, stage: str, current: int = index) -> None:
                overall = ((current - 1) + value) / len(items)
                self.after(0, self._set_progress, overall, f"{item.path.name}: {stage}")

            result = process_file(
                item.analysis,
                config,
                self.tools,
                cancel_event=self.cancel_event,
                progress=callback,
            )
            item.output = Path(result.output)
            item.status = "Already completed" if result.skipped else "Completed and validated"
            self.after(0, self._refresh_item, item)

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
        window._images = (original_image, corrected_image)  # type: ignore[attr-defined]

    def _doctor(self) -> None:
        self.status_var.set("Checking dependencies…")

        def worker() -> None:
            report = self.tools.doctor(deep_qtgmc=True)
            lines = [f"{'OK' if check.ok else 'FAIL'} — {check.name}: {check.detail}" for check in report.checks]
            self.after(0, messagebox.showinfo, "Doctor", "\n\n".join(lines))
            self.after(0, self.status_var.set, "Doctor completed")

        threading.Thread(target=worker, daemon=True).start()

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
        self.tree.item(
            str(item.path),
            values=(item.path.name, classification, confidence, action, crop, item.status),
        )

    def _set_progress(self, value: float, status: str) -> None:
        self.progress.configure(value=max(0, min(100, value * 100)))
        self.status_var.set(status)

    def _busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.analyze_button.configure(state=state)
        self.preview_button.configure(state=state)
        self.process_button.configure(state=state)
        self.cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)

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


def main() -> None:
    app = FieldFixWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
