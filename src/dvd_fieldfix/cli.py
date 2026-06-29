from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import __version__
from .analysis import analyze_file, collect_inputs, write_analysis_report
from .models import CodecProfile, CropMargins, JobConfig, ProcessingMode, to_dict
from .processing import process_file
from .tools import FieldFixError, Toolchain


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_AMBIGUOUS = 2
EXIT_DEPENDENCY = 3
EXIT_CANCELLED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dvd-fieldfix",
        description="Deteta e corrige entrelaçamento em rips de DVD sem alterar os originais.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="Validar FFmpeg, encoders e QTGMC/VFM híbrido")
    doctor.add_argument("--quick", action="store_true", help="Não executar o teste de frames QTGMC/VFM")

    analyze = subparsers.add_parser("analyze", help="Analisar um ou mais MKVs")
    _add_input_arguments(analyze)
    analyze.add_argument("--report", type=Path, help="Guardar relatório JSON v1")

    process = subparsers.add_parser("process", help="Analisar e processar um ou mais MKVs")
    _add_input_arguments(process)
    process.add_argument("--codec", choices=[item.value for item in CodecProfile], default="h264")
    process.add_argument("--mode", choices=[item.value for item in ProcessingMode], default="auto")
    process.add_argument("--output", type=Path, help="Pasta de saída; por defeito usa _fixed")
    process.add_argument(
        "--crop",
        metavar="L:T:R:B",
        help="Margens pares a cortar; desligado por defeito",
    )
    process.add_argument(
        "--auto-crop",
        action="store_true",
        help="Remover automaticamente apenas margens pretas estáveis; crop manual tem prioridade",
    )
    process.add_argument(
        "--denoise",
        choices=("off", "light"),
        default="off",
        help="Limpeza hqdn3d leve; desligada por defeito",
    )
    process.add_argument("--replace-output", action="store_true", help="Substituir apenas uma saída existente")
    process.add_argument("--report", type=Path, help="Guardar relatório agregado da análise")

    subparsers.add_parser("gui", help="Abrir a interface gráfica")
    return parser


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="+", help="Ficheiros MKV ou pastas")
    parser.add_argument("--recursive", action="store_true", help="Procurar MKVs em subpastas")


def _progress_printer(label: str) -> Callable[[float, str], None]:
    last = {"time": 0.0, "text": ""}

    def update(value: float, stage: str) -> None:
        now = time.monotonic()
        text = f"\r{label}: {value:6.1%}  {stage:<38}"
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
        marker = "OK" if check.ok else "FALHA"
        print(f"[{marker:5}] {check.name}: {check.detail}")
    print(f"\nAnálise: {'pronta' if report.analysis_ready else 'indisponível'}")
    print(f"Processamento completo: {'pronto' if report.processing_ready else 'indisponível'}")
    return EXIT_OK if report.processing_ready else EXIT_DEPENDENCY


def run_analyze(args: argparse.Namespace) -> int:
    tools = Toolchain.discover()
    paths = collect_inputs(args.inputs, recursive=args.recursive)
    if not paths:
        print("Nenhum MKV encontrado.", file=sys.stderr)
        return EXIT_FAILED
    results = []
    failures = 0
    for index, path in enumerate(paths, 1):
        print(f"[{index}/{len(paths)}] {path.name}")
        try:
            result = analyze_file(path, tools, progress=_progress_printer(path.name))
            print()
            print(
                f"  {result.classification.value} | confiança {result.confidence:.0%} | "
                f"IDet {result.idet.interlaced_percent:.3f}% | {result.reason}"
            )
            results.append(result)
            if args.report:
                # Atomic checkpoint: an interruption never discards completed analyses.
                write_analysis_report(args.report, results)
        except FieldFixError as exc:
            print(f"\n  ERRO: {exc}", file=sys.stderr)
            failures += 1
    if args.report and results:
        print(f"Relatório: {args.report.resolve()}")
    elif results:
        print(json.dumps([to_dict(result) for result in results], ensure_ascii=False, indent=2))
    return EXIT_FAILED if failures else EXIT_OK


def run_process(args: argparse.Namespace) -> int:
    tools = Toolchain.discover()
    paths = collect_inputs(args.inputs, recursive=args.recursive)
    if not paths:
        print("Nenhum MKV encontrado.", file=sys.stderr)
        return EXIT_FAILED
    try:
        crop = CropMargins.parse(args.crop)
    except ValueError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return EXIT_FAILED
    config = JobConfig(
        codec=CodecProfile(args.codec),
        mode=ProcessingMode(args.mode),
        output_directory=str(args.output.resolve()) if args.output else None,
        crop=crop,
        auto_crop=args.auto_crop,
        denoise=args.denoise == "light",
        replace_output=args.replace_output,
    )
    analyses = []
    failures = 0
    ambiguous = 0
    for index, path in enumerate(paths, 1):
        print(f"[{index}/{len(paths)}] A analisar {path.name}")
        try:
            analysis = analyze_file(path, tools, progress=_progress_printer(path.name))
            analyses.append(analysis)
            if args.report:
                write_analysis_report(args.report, analyses)
            print(f"\n  {analysis.classification.value}: {analysis.reason}")
            result = process_file(analysis, config, tools, progress=_progress_printer(path.name))
            print()
            status = "já concluído" if result.skipped else "concluído"
            print(f"  {status}: {result.output}")
        except KeyboardInterrupt:
            print("\nCancelado.", file=sys.stderr)
            return EXIT_CANCELLED
        except FieldFixError as exc:
            print(f"\n  ERRO: {exc}", file=sys.stderr)
            failures += 1
            if "ambígu" in str(exc).lower():
                ambiguous += 1
    if args.report and analyses:
        print(f"Relatório: {args.report.resolve()}")
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
        print("\nCancelado.", file=sys.stderr)
        return EXIT_CANCELLED
    except FieldFixError as exc:
        message = str(exc)
        print(f"Erro: {message}", file=sys.stderr)
        if "depend" in message.lower() or "qtgmc" in message.lower():
            return EXIT_DEPENDENCY
        return EXIT_FAILED
    parser.print_help()
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
