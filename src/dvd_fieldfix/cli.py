from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import __version__
from .analysis import analyze_file, collect_inputs, write_analysis_report
from .models import CodecProfile, CropMargins, JobConfig, ProcessingMode, to_dict
from .processing import process_file
from .tools import FieldFixError, ProgressDetails, Toolchain


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_AMBIGUOUS = 2
EXIT_DEPENDENCY = 3
EXIT_CANCELLED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dvd-fieldfix",
        description="Detect and correct interlacing in DVD rips without modifying the originals.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser(
        "doctor", help="Check whether FFmpeg, encoders and VapourSynth are ready"
    )
    doctor.add_argument("--quick", action="store_true", help="Skip the QTGMC/VFM frame test")

    analyze = subparsers.add_parser("analyze", help="Analyze one or more MKVs")
    _add_input_arguments(analyze)
    analyze.add_argument("--report", type=Path, help="Save a JSON v1 report")

    process = subparsers.add_parser("process", help="Analyze and process one or more MKVs")
    _add_input_arguments(process)
    process.add_argument("--codec", choices=[item.value for item in CodecProfile], default="h264")
    process.add_argument(
        "--crf",
        type=float,
        default=14.0,
        help="Constant-quality target for H.264/HEVC (0-51; lower is higher quality)",
    )
    process.add_argument("--mode", choices=[item.value for item in ProcessingMode], default="auto")
    process.add_argument("--output", type=Path, help="Output folder; defaults to _fixed")
    process.add_argument(
        "--crop",
        metavar="L:T:R:B",
        help="Even crop margins; disabled by default",
    )
    process.add_argument(
        "--dotcrawl",
        action="store_true",
        help="Apply one conservative spatial DotKillS pass; disabled by default",
    )
    process.add_argument(
        "--auto-crop",
        action="store_true",
        help="Automatically remove stable black borders only; manual crop takes priority",
    )
    process.add_argument(
        "--denoise",
        choices=("off", "light"),
        default="off",
        help="Light hqdn3d cleanup; disabled by default",
    )
    process.add_argument("--replace-output", action="store_true", help="Replace an existing output only")
    process.add_argument("--report", type=Path, help="Save the aggregate analysis report")

    subparsers.add_parser("gui", help="Open the graphical interface")
    return parser


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="+", help="MKV files or folders")
    parser.add_argument("--recursive", action="store_true", help="Search for MKVs in subfolders")


def _clock(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "--:--:--"
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _progress_printer(label: str) -> Callable[[float, str, ProgressDetails | None], None]:
    last = {"time": 0.0, "text": ""}

    def update(value: float, stage: str, details: ProgressDetails | None = None) -> None:
        now = time.monotonic()
        metrics = ""
        if details:
            frame = (
                f"{details.current_frame:,}/{details.total_frames:,} frames"
                if details.current_frame is not None and details.total_frames is not None
                else ""
            )
            fps = f"{details.fps:.2f} fps" if details.fps is not None else ""
            timing = (
                f"elapsed {_clock(details.elapsed_seconds)}  ETA {_clock(details.eta_seconds)}"
                if details.elapsed_seconds is not None
                else ""
            )
            metrics = "  ".join(part for part in (frame, fps, timing) if part)
        text = f"\r{label}: {value:6.1%}  {stage:<30}  {metrics:<56}"
        if now - last["time"] >= 0.25 or value >= 1:
            try:
                print(text, end="", flush=True)
            except OSError:
                return
            last["time"] = now
            last["text"] = text

    return update


def run_doctor(args: argparse.Namespace) -> int:
    report = Toolchain.discover().doctor(deep_qtgmc=not args.quick)
    for check in report.checks:
        marker = "OK" if check.ok else "FAIL"
        print(f"[{marker:5}] {check.name}: {check.detail}")
    print(f"\nAnalysis: {'ready' if report.analysis_ready else 'unavailable'}")
    print(f"Full processing: {'ready' if report.processing_ready else 'unavailable'}")
    return EXIT_OK if report.processing_ready else EXIT_DEPENDENCY


def run_analyze(args: argparse.Namespace) -> int:
    tools = Toolchain.discover()
    paths = collect_inputs(args.inputs, recursive=args.recursive)
    if not paths:
        print("No MKV files found.", file=sys.stderr)
        return EXIT_FAILED
    results = []
    failures = 0
    for index, path in enumerate(paths, 1):
        print(f"[{index}/{len(paths)}] {path.name}")
        try:
            result = analyze_file(path, tools, progress=_progress_printer(path.name))
            print()
            print(
                f"  {result.classification.value} | confidence {result.confidence:.0%} | "
                f"IDet {result.idet.interlaced_percent:.3f}% | {result.reason}"
            )
            results.append(result)
            if args.report:
                # Atomic checkpoint: an interruption never discards completed analyses.
                write_analysis_report(args.report, results)
        except FieldFixError as exc:
            print(f"\n  ERROR: {exc}", file=sys.stderr)
            failures += 1
    if args.report and results:
        print(f"Report: {args.report.resolve()}")
    elif results:
        print(json.dumps([to_dict(result) for result in results], ensure_ascii=False, indent=2))
    return EXIT_FAILED if failures else EXIT_OK


def run_process(args: argparse.Namespace) -> int:
    tools = Toolchain.discover()
    paths = collect_inputs(args.inputs, recursive=args.recursive)
    if not paths:
        print("No MKV files found.", file=sys.stderr)
        return EXIT_FAILED
    try:
        crop = CropMargins.parse(args.crop)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if not 0 <= args.crf <= 51:
        print("Error: CRF must be between 0 and 51", file=sys.stderr)
        return EXIT_FAILED
    config = JobConfig(
        codec=CodecProfile(args.codec),
        crf=args.crf,
        mode=ProcessingMode(args.mode),
        output_directory=str(args.output.resolve()) if args.output else None,
        crop=crop,
        auto_crop=args.auto_crop,
        denoise=args.denoise == "light",
        dotcrawl=args.dotcrawl,
        replace_output=args.replace_output,
    )
    analyses = []
    failures = 0
    ambiguous = 0
    for index, path in enumerate(paths, 1):
        print(f"[{index}/{len(paths)}] Analyzing {path.name}")
        try:
            analysis = analyze_file(path, tools, progress=_progress_printer(path.name))
            analyses.append(analysis)
            if args.report:
                write_analysis_report(args.report, analyses)
            print(f"\n  {analysis.classification.value}: {analysis.reason}")
            result = process_file(analysis, config, tools, progress=_progress_printer(path.name))
            print()
            status = "already completed" if result.skipped else "completed"
            print(f"  {status}: {result.output}")
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
            return EXIT_CANCELLED
        except FieldFixError as exc:
            print(f"\n  ERROR: {exc}", file=sys.stderr)
            failures += 1
            if "ambigu" in str(exc).lower():
                ambiguous += 1
    if args.report and analyses:
        print(f"Report: {args.report.resolve()}")
    if ambiguous:
        return EXIT_AMBIGUOUS
    return EXIT_FAILED if failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "gui"}:
        from .gui import main as gui_main

        gui_main()
        return EXIT_OK
    try:
        if args.command == "doctor":
            return run_doctor(args)
        if args.command == "analyze":
            return run_analyze(args)
        if args.command == "process":
            return run_process(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return EXIT_CANCELLED
    except FieldFixError as exc:
        message = str(exc)
        print(f"Error: {message}", file=sys.stderr)
        if "depend" in message.lower() or "qtgmc" in message.lower():
            return EXIT_DEPENDENCY
        return EXIT_FAILED
    parser.print_help()
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
