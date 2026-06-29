from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import subprocess
import threading
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from .models import (
    AnalysisResult,
    Classification,
    IDetStats,
    MediaInfo,
    ProcessingMode,
    StreamInfo,
    TimelineSegment,
    report_document,
)
from .tools import (
    AnalysisError,
    CancelledError,
    ProgressCallback,
    Toolchain,
    json_dump_atomic,
    parse_ffmpeg_progress_line,
    popen_kwargs,
    terminate_process_tree,
)


IDET_MULTI_RE = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+"
    r"Progressive:\s*(\d+)\s+Undetermined:\s*(\d+)",
    re.IGNORECASE,
)
IDET_REPEAT_RE = re.compile(
    r"Repeated Fields:\s*Neither:\s*(\d+)\s+Top:\s*(\d+)\s+Bottom:\s*(\d+)",
    re.IGNORECASE,
)
METADATA_FRAME_RE = re.compile(r"frame:\s*(\d+).*?pts_time:([0-9.+-]+)")
METADATA_CLASS_RE = re.compile(r"lavfi\.idet\.multiple\.current_frame=(\S+)")
FIELDMATCH_FRAME_RE = re.compile(r"Frame #(\d+).*is still interlaced")
CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")

RESIDUAL_BIN_SECONDS = 1
RESIDUAL_LOCAL_THRESHOLD = 20.0
RESIDUAL_SEGMENT_PADDING = 0.5
RESIDUAL_MAX_GAP = 1.0


def parse_rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def collect_inputs(values: Iterable[str | os.PathLike[str]], recursive: bool = False) -> list[Path]:
    found: list[Path] = []
    for raw in values:
        path = Path(raw).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".mkv":
            found.append(path)
        elif path.is_dir():
            iterator = path.rglob("*.mkv") if recursive else path.glob("*.mkv")
            found.extend(item.resolve() for item in iterator if "_fixed" not in item.parts)
        elif not path.exists():
            raise AnalysisError(f"Path does not exist: {path}")
    return sorted(dict.fromkeys(found), key=lambda item: str(item).casefold())


def probe_media(path: str | os.PathLike[str], tools: Toolchain) -> MediaInfo:
    tools.require_analysis()
    assert tools.ffprobe
    completed = subprocess.run(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            "--",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    if completed.returncode:
        raise AnalysisError(completed.stderr.strip() or f"ffprobe failed for {path}")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"Invalid ffprobe response for {path}") from exc
    streams: list[StreamInfo] = []
    for raw in document.get("streams", []):
        streams.append(
            StreamInfo(
                index=int(raw.get("index", -1)),
                codec_type=str(raw.get("codec_type", "unknown")),
                codec_name=str(raw.get("codec_name", "")),
                tags=dict(raw.get("tags") or {}),
                disposition={key: int(value) for key, value in (raw.get("disposition") or {}).items()},
                width=_optional_int(raw.get("width")),
                height=_optional_int(raw.get("height")),
                pixel_format=raw.get("pix_fmt"),
                field_order=raw.get("field_order"),
                average_frame_rate=raw.get("avg_frame_rate"),
                real_frame_rate=raw.get("r_frame_rate"),
                sample_aspect_ratio=raw.get("sample_aspect_ratio"),
                display_aspect_ratio=raw.get("display_aspect_ratio"),
                color_range=raw.get("color_range"),
                color_space=raw.get("color_space"),
                color_transfer=raw.get("color_transfer"),
                color_primaries=raw.get("color_primaries"),
                channels=_optional_int(raw.get("channels")),
                channel_layout=raw.get("channel_layout"),
                sample_rate=raw.get("sample_rate"),
            )
        )
    raw_format = document.get("format") or {}
    return MediaInfo(
        path=str(Path(path).resolve()),
        duration=_optional_float(raw_format.get("duration")) or 0.0,
        start_time=_optional_float(raw_format.get("start_time")) or 0.0,
        bit_rate=_optional_int(raw_format.get("bit_rate")),
        format_name=str(raw_format.get("format_name", "")),
        format_tags=dict(raw_format.get("tags") or {}),
        streams=streams,
        chapters=list(document.get("chapters") or []),
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def scan_idet(
    media: MediaInfo,
    tools: Toolchain,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[IDetStats, list[TimelineSegment]]:
    tools.require_analysis()
    assert tools.ffmpeg
    command = [
        tools.ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        media.path,
        "-map",
        "0:v:0",
        "-vf",
        "idet,metadata=mode=print:key=lavfi.idet.multiple.current_frame",
        "-an",
        "-sn",
        "-dn",
        "-f",
        "null",
        os.devnull,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    bins: dict[int, Counter[str]] = defaultdict(Counter)
    current_time: float | None = None
    stats = IDetStats()
    last_lines: list[str] = []
    expected_frames = max(1, round(media.duration * (parse_rate(media.video.average_frame_rate) or 25))) if media.video else 1
    assert process.stderr
    try:
        for line in process.stderr:
            last_lines.append(line.rstrip())
            if len(last_lines) > 30:
                last_lines.pop(0)
            if cancel_event and cancel_event.is_set():
                terminate_process_tree(process)
                raise CancelledError("Analysis cancelled")
            frame_match = METADATA_FRAME_RE.search(line)
            if frame_match:
                current_time = float(frame_match.group(2))
                frame_number = int(frame_match.group(1))
                if progress and frame_number % 250 == 0:
                    progress(min(0.98, frame_number / expected_frames), "Measuring fields")
                continue
            class_match = METADATA_CLASS_RE.search(line)
            if class_match and current_time is not None:
                bins[int(current_time)][class_match.group(1).lower()] += 1
                continue
            multi_match = IDET_MULTI_RE.search(line)
            if multi_match:
                stats.tff, stats.bff, stats.progressive, stats.undetermined = map(int, multi_match.groups())
                stats.frames = stats.tff + stats.bff + stats.progressive + stats.undetermined
                continue
            repeat_match = IDET_REPEAT_RE.search(line)
            if repeat_match:
                stats.repeated_neither, stats.repeated_top, stats.repeated_bottom = map(int, repeat_match.groups())
    finally:
        if process.poll() is None:
            process.wait()
    if process.returncode:
        raise AnalysisError("IDet failed:\n" + "\n".join(last_lines[-15:]))
    if progress:
        progress(1.0, "IDet completed")
    return stats, _segments_from_bins(bins, media.duration)


def _segments_from_bins(bins: dict[int, Counter[str]], duration: float) -> list[TimelineSegment]:
    active: list[tuple[int, float]] = []
    for start, counts in sorted(bins.items()):
        total = sum(counts.values())
        interlaced = counts["tff"] + counts["bff"]
        percent = 100.0 * interlaced / total if total else 0.0
        if percent >= 50.0:
            active.append((start, percent))
    segments: list[TimelineSegment] = []
    for start, percent in active:
        end = min(duration, start + 1)
        if segments and start <= segments[-1].end + 0.001:
            previous = segments[-1]
            previous.interlaced_percent = (previous.interlaced_percent + percent) / 2
            previous.end = end
        else:
            segments.append(TimelineSegment(float(start), float(end), percent))
    return segments


def scan_fieldmatch_residual(
    media: MediaInfo,
    tools: Toolchain,
    field_order: str,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[int, float, list[TimelineSegment]]:
    assert tools.ffmpeg
    command = [
        tools.ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        media.path,
        "-map",
        "0:v:0",
        "-vf",
        f"fieldmatch=order={field_order}:mode=pc_n:combmatch=full",
        "-an",
        "-sn",
        "-dn",
        "-progress",
        "pipe:1",
        "-f",
        "null",
        os.devnull,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    residual = 0
    residual_bins: Counter[int] = Counter()
    error_lines: list[str] = []

    def read_errors() -> None:
        nonlocal residual
        assert process.stderr
        for line in process.stderr:
            if "is still interlaced" in line:
                residual += 1
                match = FIELDMATCH_FRAME_RE.search(line)
                if match:
                    frame_number = int(match.group(1))
                    fps = parse_rate(media.video.average_frame_rate) if media.video else 25.0
                    # One-second bins are precise enough to find short 50i inserts
                    # without switching ten seconds of clean 25p to QTGMC.
                    residual_bins[int(frame_number / (fps or 25.0))] += 1
            error_lines.append(line.rstrip())
            if len(error_lines) > 40:
                error_lines.pop(0)

    thread = threading.Thread(target=read_errors, daemon=True)
    thread.start()
    assert process.stdout
    for line in process.stdout:
        if cancel_event and cancel_event.is_set():
            terminate_process_tree(process)
            thread.join(timeout=2)
            raise CancelledError("Analysis cancelled")
        value = parse_ffmpeg_progress_line(line, media.duration)
        if value is not None and progress:
            progress(value, "Testing field reconstruction")
    returncode = process.wait()
    thread.join(timeout=5)
    if returncode:
        raise AnalysisError("Field matching failed:\n" + "\n".join(error_lines[-15:]))
    fps = parse_rate(media.video.average_frame_rate) if media.video else None
    total = max(1, round(media.duration * (fps or 25.0)))
    segments = _residual_segments(residual_bins, fps or 25.0, media.duration)
    return residual, 100.0 * residual / total, segments


def _residual_segments(
    bins: Counter[int],
    fps: float,
    duration: float,
    threshold: float = RESIDUAL_LOCAL_THRESHOLD,
    *,
    bin_seconds: float = RESIDUAL_BIN_SECONDS,
    padding: float = RESIDUAL_SEGMENT_PADDING,
    max_gap: float = RESIDUAL_MAX_GAP,
) -> list[TimelineSegment]:
    active: list[tuple[float, float]] = []
    for start, count in sorted(bins.items()):
        available = max(0.0, min(bin_seconds, duration - start))
        frames_per_bin = max(1.0, fps * available)
        percent = 100.0 * count / frames_per_bin
        if percent >= threshold:
            active.append((float(start), min(duration, float(start) + bin_seconds)))
    raw_segments: list[tuple[float, float]] = []
    for start, end in active:
        if raw_segments and start <= raw_segments[-1][1] + max_gap + 0.001:
            raw_segments[-1] = (raw_segments[-1][0], end)
        else:
            raw_segments.append((start, end))

    padded: list[tuple[float, float]] = []
    for start, end in raw_segments:
        candidate = (max(0.0, start - padding), min(duration, end + padding))
        if padded and candidate[0] <= padded[-1][1] + 0.001:
            padded[-1] = (padded[-1][0], max(padded[-1][1], candidate[1]))
        else:
            padded.append(candidate)

    segments: list[TimelineSegment] = []
    for start, end in padded:
        residual = sum(
            count
            for bin_start, count in bins.items()
            if float(bin_start) < end and float(bin_start) + bin_seconds > start
        )
        percent = 100.0 * residual / max(1.0, fps * (end - start))
        segments.append(TimelineSegment(start, end, min(100.0, percent)))
    return segments


def detect_crop(media: MediaInfo, tools: Toolchain) -> str | None:
    if not media.video or not tools.ffmpeg or media.duration <= 0:
        return None
    width, height = media.video.width, media.video.height
    if not width or not height:
        return None
    if media.duration < 12:
        windows = [(0.0, media.duration)]
    else:
        window_duration = min(6.0, media.duration / 10)
        positions = (0.08, 0.22, 0.36, 0.50, 0.64, 0.78, 0.92)
        windows = [
            (
                max(0.0, min(media.duration - window_duration, media.duration * position - window_duration / 2)),
                window_duration,
            )
            for position in positions
        ]
    rectangles: list[tuple[int, int, int, int]] = []
    for start, duration in windows:
        rectangle = _detect_crop_window(media, tools.ffmpeg, start, duration)
        if rectangle:
            rectangles.append(rectangle)
    minimum_samples = 1 if len(windows) == 1 else math.ceil(len(windows) * 0.70)
    if len(rectangles) < minimum_samples:
        return None
    combined = _combine_crop_rectangles(rectangles, width, height)
    if not combined or combined == (width, height, 0, 0):
        return None
    crop_width, crop_height, x, y = combined
    return f"crop={crop_width}:{crop_height}:{x}:{y}"


def _detect_crop_window(
    media: MediaInfo,
    ffmpeg: str,
    start: float,
    duration: float,
) -> tuple[int, int, int, int] | None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{start:.3f}",
        "-i",
        media.path,
        "-map",
        "0:v:0",
        "-t",
        f"{duration:.3f}",
        "-vf",
        "cropdetect=limit=20:round=2:reset=0",
        "-an",
        "-sn",
        "-f",
        "null",
        os.devnull,
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    matches = CROP_RE.findall(completed.stderr)
    if not matches:
        return None
    # reset=0 can only expand the content rectangle; the last result is the
    # least aggressive and therefore safest result for this window.
    return tuple(map(int, matches[-1]))  # type: ignore[return-value]


def _combine_crop_rectangles(
    rectangles: list[tuple[int, int, int, int]],
    frame_width: int,
    frame_height: int,
) -> tuple[int, int, int, int] | None:
    if not rectangles:
        return None
    valid = [
        (width, height, x, y)
        for width, height, x, y in rectangles
        if width > 0
        and height > 0
        and x >= 0
        and y >= 0
        and x + width <= frame_width
        and y + height <= frame_height
    ]
    if not valid:
        return None
    # Union the detected content rectangles. A pixel is cropped only if every
    # sampled window agreed that it was outside active picture.
    left = min(item[2] for item in valid)
    top = min(item[3] for item in valid)
    right = max(item[2] + item[0] for item in valid)
    bottom = max(item[3] + item[1] for item in valid)
    left = max(0, left - left % 2)
    top = max(0, top - top % 2)
    right = min(frame_width, right + right % 2)
    bottom = min(frame_height, bottom + bottom % 2)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        return None
    return width, height, left, top


def analyze_file(
    path: str | os.PathLike[str],
    tools: Toolchain | None = None,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> AnalysisResult:
    tools = tools or Toolchain.discover()
    media = probe_media(path, tools)
    video = media.video
    if not video or not video.width or not video.height:
        return AnalysisResult(media, Classification.UNSUPPORTED, 1.0, "The file does not contain supported video")
    fps = parse_rate(video.average_frame_rate) or parse_rate(video.real_frame_rate)
    if not fps:
        return AnalysisResult(media, Classification.UNSUPPORTED, 1.0, "Could not determine the frame rate")
    if progress:
        progress(0.0, "Analyzing with IDet")
    idet, segments = scan_idet(media, tools, cancel_event=cancel_event, progress=progress)
    crop = detect_crop(media, tools)
    base = dict(
        media=media,
        idet=idet,
        field_order=idet.field_order or _field_order_from_probe(video.field_order),
        input_fps=fps,
        crop_suggestion=crop,
        hybrid_segments=segments,
    )
    if idet.interlaced_percent <= 1.0 and not segments:
        return AnalysisResult(
            classification=Classification.PROGRESSIVE,
            confidence=max(0.90, 1.0 - idet.interlaced_percent / 10),
            reason=f"IDet found only {idet.interlaced_percent:.3f}% interlaced frames",
            cadence="progressive",
            suggested_output_fps=_rate_label(fps),
            suggested_mode=ProcessingMode.COPY,
            **base,
        )
    field_order = base["field_order"]
    if not field_order:
        return AnalysisResult(
            classification=Classification.AMBIGUOUS,
            confidence=0.25,
            reason="Combing is present, but the field order is inconclusive",
            suggested_mode=None,
            warnings=["Choose TFF or BFF manually after previewing the result."],
            **base,
        )
    if idet.field_order_consistency and idet.field_order_consistency < 0.85:
        return AnalysisResult(
            classification=Classification.AMBIGUOUS,
            confidence=0.35,
            reason=f"Field order is consistent in only {idet.field_order_consistency:.1%} of frames",
            suggested_mode=None,
            **base,
        )
    if progress:
        progress(0.0, "Testing field matching")
    residual_frames, residual_percent, residual_segments = scan_fieldmatch_residual(
        media,
        tools,
        str(field_order),
        cancel_event=cancel_event,
        progress=progress,
    )
    base.update(
        fieldmatch_residual_frames=residual_frames,
        fieldmatch_residual_percent=residual_percent,
        fieldmatch_residual_segments=residual_segments,
    )
    pal = math.isclose(fps, 25.0, abs_tol=0.08)
    ntsc = math.isclose(fps, 30000 / 1001, abs_tol=0.08)
    if pal and residual_percent < 20.0 and residual_segments:
        hybrid_duration = sum(segment.end - segment.start for segment in residual_segments)
        return AnalysisResult(
            classification=Classification.HYBRID,
            confidence=max(0.90, min(0.99, 0.94 + hybrid_duration / max(media.duration, 1.0) / 4)),
            reason=(
                f"Field matching recovers the 25p body, but {hybrid_duration:.1f}s "
                "of stable 50i segments remain; preserve the timeline at 50p"
            ),
            cadence="hybrid-2:2/50i",
            suggested_output_fps="50/1",
            suggested_mode=ProcessingMode.HYBRID50,
            **base,
        )
    if residual_percent <= 1.0 and pal:
        return AnalysisResult(
            classification=Classification.FIELD_MATCHABLE,
            confidence=max(0.88, 0.99 - residual_percent / 100),
            reason=(
                f"Field matching recovers 25p; {residual_percent:.3f}% uses "
                "conditional deinterlacing"
            ),
            cadence="2:2",
            suggested_output_fps="25/1",
            suggested_mode=ProcessingMode.FIELDMATCH,
            **base,
        )
    if (
        residual_percent <= 1.0
        and ntsc
        and not residual_segments
        and 12.0 <= idet.repeated_percent <= 30.0
    ):
        return AnalysisResult(
            classification=Classification.FIELD_MATCHABLE,
            confidence=0.92,
            reason="Stable NTSC 3:2 cadence with little residual combing after field matching",
            cadence="3:2",
            suggested_output_fps="24000/1001",
            suggested_mode=ProcessingMode.FIELDMATCH,
            **base,
        )
    if residual_percent >= 20.0:
        output_fps = "50/1" if pal else "60000/1001" if ntsc else _rate_label(fps * 2)
        return AnalysisResult(
            classification=Classification.TRUE_INTERLACED,
            confidence=min(0.99, 0.80 + residual_percent / 500),
            reason=f"{residual_percent:.2f}% remains combed after field matching; treat as true interlaced video",
            cadence="field-rate",
            suggested_output_fps=output_fps,
            suggested_mode=ProcessingMode.QTGMC,
            **base,
        )
    warnings = []
    if ntsc:
        if residual_segments:
            warnings.append(
                "Hybrid NTSC material: stop for review rather than damage an uncertain 3:2/59.94i cadence."
            )
        else:
            warnings.append("NTSC material without a sufficiently stable 3:2 pattern.")
    return AnalysisResult(
        classification=Classification.AMBIGUOUS,
        confidence=0.45,
        reason=f"A {residual_percent:.2f}% residual falls between the safe field-match and QTGMC thresholds",
        cadence="unknown",
        suggested_output_fps=None,
        suggested_mode=None,
        warnings=warnings,
        **base,
    )


def _field_order_from_probe(value: str | None) -> str | None:
    if value in {"tt", "tb", "tff"}:
        return "tff"
    if value in {"bb", "bt", "bff"}:
        return "bff"
    return None


def _rate_label(value: float) -> str:
    common = (
        (24000 / 1001, "24000/1001"),
        (25.0, "25/1"),
        (30000 / 1001, "30000/1001"),
        (50.0, "50/1"),
        (60000 / 1001, "60000/1001"),
    )
    for expected, label in common:
        if math.isclose(value, expected, abs_tol=0.02):
            return label
    return str(Fraction(value).limit_denominator(1001))


def write_analysis_report(path: str | os.PathLike[str], results: list[AnalysisResult]) -> None:
    generated = dt.datetime.now(dt.timezone.utc).isoformat()
    json_dump_atomic(Path(path), report_document(results, generated))
