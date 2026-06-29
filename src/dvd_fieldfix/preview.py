from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .analysis import parse_rate
from .models import AnalysisResult, JobConfig, ProcessingMode
from .processing import (
    _hybrid_frame_ranges,
    _hybrid_script,
    _qtgmc_script,
    fieldmatch_filter,
    resolve_mode,
    restoration_filters,
)
from .tools import DependencyError, ProcessingError, Toolchain, popen_kwargs


def preview_timestamp(analysis: AnalysisResult) -> float:
    if analysis.fieldmatch_residual_segments:
        segment = analysis.fieldmatch_residual_segments[0]
        return max(1.0, (segment.start + segment.end) / 2)
    if analysis.hybrid_segments:
        segment = analysis.hybrid_segments[0]
        return max(1.0, (segment.start + segment.end) / 2)
    return max(1.0, min(analysis.media.duration - 1, analysis.media.duration / 3))


def generate_preview(
    analysis: AnalysisResult,
    config: JobConfig,
    tools: Toolchain | None = None,
    directory: str | os.PathLike[str] | None = None,
) -> tuple[Path, Path, Path]:
    tools = tools or Toolchain.discover()
    tools.require_analysis()
    assert tools.ffmpeg
    destination = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="dvd-fieldfix-preview-"))
    destination.mkdir(parents=True, exist_ok=True)
    source_png = destination / "original.png"
    corrected_png = destination / "corrected.png"
    timestamp = preview_timestamp(analysis)
    _extract_source_frame(tools.ffmpeg, analysis.media.path, timestamp, source_png)
    mode = resolve_mode(analysis, config.mode)
    if mode == ProcessingMode.COPY:
        shutil.copy2(source_png, corrected_png)
    elif mode == ProcessingMode.FIELDMATCH:
        _extract_fieldmatched_frame(tools.ffmpeg, analysis, config, timestamp, corrected_png)
    elif mode in {ProcessingMode.HYBRID50, ProcessingMode.QTGMC}:
        tools.require_qtgmc()
        assert tools.vspipe
        _extract_vapoursynth_frame(
            tools, analysis, config, mode, timestamp, destination, corrected_png
        )
    else:
        raise ProcessingError(f"Preview does not support {mode}")
    return source_png, corrected_png, destination


def _extract_source_frame(ffmpeg: str, source: str, timestamp: float, output: Path) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-y",
        str(output),
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs())
    if completed.returncode:
        raise ProcessingError(completed.stderr.decode("utf-8", errors="replace"))


def _extract_fieldmatched_frame(
    ffmpeg: str,
    analysis: AnalysisResult,
    config: JobConfig,
    timestamp: float,
    output: Path,
) -> None:
    seek = max(0.0, timestamp - 1.0)
    offset = timestamp - seek
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seek:.3f}",
        "-i",
        analysis.media.path,
        "-map",
        "0:v:0",
        "-vf",
        fieldmatch_filter(analysis, config),
        "-ss",
        f"{offset:.3f}",
        "-frames:v",
        "1",
        "-y",
        str(output),
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs())
    if completed.returncode:
        raise ProcessingError(completed.stderr.decode("utf-8", errors="replace"))


def _extract_vapoursynth_frame(
    tools: Toolchain,
    analysis: AnalysisResult,
    config: JobConfig,
    mode: ProcessingMode,
    timestamp: float,
    directory: Path,
    output: Path,
) -> None:
    assert tools.ffmpeg and tools.vspipe
    script = directory / "preview.vpy"
    cache = directory / "preview.bsindex"
    tff = (analysis.field_order or "tff") == "tff"
    fps = parse_rate(analysis.media.video.average_frame_rate) if analysis.media.video else 25.0
    if mode == ProcessingMode.HYBRID50:
        ranges = _hybrid_frame_ranges(analysis)
        script_text = _hybrid_script(
            analysis.media.path, cache, tff, fps or 25.0, ranges
        )
    else:
        script_text = _qtgmc_script(analysis.media.path, cache, tff)
    script.write_text(
        script_text,
        encoding="utf-8",
    )
    frame = max(0, round(timestamp * (fps or 25.0) * 2))
    producer = subprocess.Popen(
        [
            tools.vspipe,
            str(script),
            "-",
            "--container",
            "y4m",
            "--start",
            str(frame),
            "--end",
            str(frame),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_kwargs(),
    )
    assert producer.stdout
    filters = restoration_filters(analysis, config)
    command = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if filters:
        command.extend(["-vf", ",".join(filters)])
    command.extend(["-frames:v", "1", "-y", str(output)])
    consumer = subprocess.Popen(
        command,
        stdin=producer.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_kwargs(),
    )
    producer.stdout.close()
    _, consumer_error = consumer.communicate(timeout=180)
    _, producer_error = producer.communicate(timeout=30)
    if consumer.returncode or producer.returncode:
        message = (producer_error + b"\n" + consumer_error).decode("utf-8", errors="replace")
        raise DependencyError("Could not generate the QTGMC preview:\n" + message[-2000:])
