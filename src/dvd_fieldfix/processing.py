from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path

from .analysis import parse_rate, probe_media, scan_fieldmatch_residual
from .models import (
    AnalysisResult,
    Classification,
    CodecProfile,
    CropMargins,
    JobConfig,
    MediaInfo,
    PROCESSING_PIPELINE_VERSION,
    ProcessingMode,
    ProcessingResult,
    ValidationResult,
    to_dict,
)
from .tools import (
    AmbiguousSourceError,
    CancelledError,
    DependencyError,
    FFmpegProgressState,
    OutputCollisionError,
    ProcessingError,
    ProgressCallback,
    Toolchain,
    drain_text_stream,
    json_dump_atomic,
    parse_ffmpeg_progress_line,
    popen_kwargs,
    sha256_file,
    terminate_process_tree,
)


MANIFEST_SUFFIX = ".dvd-fieldfix.json"
SIGNALSTATS_FRAME_RE = re.compile(r"frame:\s*(\d+)")
SIGNALSTATS_YDIF_RE = re.compile(r"lavfi\.signalstats\.YDIF=([0-9.+-]+)")
CLEAN_PAIR_YDIF_THRESHOLD = 0.75
CLEAN_PAIR_REQUIRED_PERCENT = 98.0


def resolve_mode(
    analysis: AnalysisResult,
    requested: ProcessingMode,
    *,
    restoration: bool = False,
) -> ProcessingMode:
    if requested != ProcessingMode.AUTO:
        return requested
    if analysis.classification == Classification.PROGRESSIVE:
        return ProcessingMode.RESTORE if restoration else ProcessingMode.COPY
    if analysis.classification == Classification.FIELD_MATCHABLE:
        return ProcessingMode.FIELDMATCH
    if analysis.classification == Classification.HYBRID:
        return ProcessingMode.HYBRID50
    if analysis.classification == Classification.TRUE_INTERLACED:
        return ProcessingMode.QTGMC
    if analysis.classification == Classification.AMBIGUOUS:
        raise AmbiguousSourceError(
            f"{Path(analysis.media.path).name}: ambiguous analysis; choose fieldmatch, hybrid50 or QTGMC manually"
        )
    raise ProcessingError(f"Unsupported source: {analysis.reason}")


def codec_arguments(profile: CodecProfile, crf: float = 14.0) -> list[str]:
    crf_text = f"{crf:g}"
    if profile == CodecProfile.H264:
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryslow",
            "-crf",
            crf_text,
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-x264-params",
            "subme=11:merange=32:fast-pskip=0:dct-decimate=0",
        ]
    if profile == CodecProfile.HEVC10:
        return [
            "-c:v",
            "libx265",
            "-preset",
            "veryslow",
            "-crf",
            crf_text,
            "-pix_fmt",
            "yuv420p10le",
            "-x265-params",
            "rd-refine=1",
        ]
    if profile == CodecProfile.FFV1:
        return [
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-coder",
            "1",
            "-context",
            "1",
            "-g",
            "1",
            "-slicecrc",
            "1",
            "-pix_fmt",
            "yuv420p",
        ]
    raise ProcessingError(f"Unknown profile: {profile}")


def restoration_filters(analysis: AnalysisResult, config: JobConfig) -> list[str]:
    filters: list[str] = []
    crop = effective_crop(analysis, config)
    video = analysis.media.video
    if crop.enabled:
        if not video or not video.width or not video.height:
            raise ProcessingError("Crop cannot be applied without known dimensions")
        new_width = video.width - crop.left - crop.right
        new_height = video.height - crop.top - crop.bottom
        if new_width <= 0 or new_height <= 0 or new_width % 2 or new_height % 2:
            raise ProcessingError("The crop produces invalid dimensions; use smaller even margins")
        filters.append(
            f"crop=iw-{crop.left + crop.right}:ih-{crop.top + crop.bottom}:{crop.left}:{crop.top}"
        )
        sar = _sar_after_crop(video.display_aspect_ratio, video.sample_aspect_ratio, video.width, video.height, new_width, new_height)
        if sar:
            filters.append(f"setsar={sar.numerator}/{sar.denominator}")
    elif video and video.width and video.height:
        sar = _sar_after_crop(
            video.display_aspect_ratio,
            video.sample_aspect_ratio,
            video.width,
            video.height,
            video.width,
            video.height,
        )
        if sar:
            filters.append(f"setsar={sar.numerator}/{sar.denominator}")
    if config.denoise:
        filters.append("hqdn3d=1:1:3:3")
    return filters


def effective_crop(analysis: AnalysisResult, config: JobConfig) -> CropMargins:
    if config.crop.enabled:
        return config.crop
    if not config.auto_crop or not analysis.crop_suggestion or not analysis.media.video:
        return CropMargins()
    match = re.fullmatch(r"crop=(\d+):(\d+):(\d+):(\d+)", analysis.crop_suggestion)
    if not match:
        return CropMargins()
    crop_width, crop_height, x, y = map(int, match.groups())
    video = analysis.media.video
    if not video.width or not video.height:
        return CropMargins()
    right = video.width - x - crop_width
    bottom = video.height - y - crop_height
    if min(x, y, right, bottom) < 0 or any(value % 2 for value in (x, y, right, bottom)):
        return CropMargins()
    return CropMargins(left=x, top=y, right=right, bottom=bottom)


def _sar_after_crop(
    dar_text: str | None,
    sar_text: str | None,
    old_width: int,
    old_height: int,
    new_width: int,
    new_height: int,
) -> Fraction | None:
    try:
        if dar_text and dar_text not in {"N/A", "0:1"}:
            dar = Fraction(dar_text.replace(":", "/"))
        elif sar_text and sar_text not in {"N/A", "0:1"}:
            dar = Fraction(old_width, old_height) * Fraction(sar_text.replace(":", "/"))
        else:
            return None
    except (ValueError, ZeroDivisionError):
        return None
    return (dar * Fraction(new_height, new_width)).limit_denominator(65535)


def _display_aspect_ratio(media: MediaInfo) -> str | None:
    video = media.video
    if not video or not video.width or not video.height:
        return None
    try:
        if video.display_aspect_ratio and video.display_aspect_ratio not in {"N/A", "0:1"}:
            dar = Fraction(video.display_aspect_ratio.replace(":", "/"))
        elif video.sample_aspect_ratio and video.sample_aspect_ratio not in {"N/A", "0:1"}:
            sar = Fraction(video.sample_aspect_ratio.replace(":", "/"))
            dar = Fraction(video.width, video.height) * sar
        else:
            return None
    except (ValueError, ZeroDivisionError):
        return None
    dar = dar.limit_denominator(65535)
    return f"{dar.numerator}:{dar.denominator}"


def color_arguments(analysis: AnalysisResult) -> list[str]:
    video = analysis.media.video
    if not video:
        return []
    color_range = video.color_range or "tv"
    space = video.color_space
    transfer = video.color_transfer
    primaries = video.color_primaries
    fps = analysis.input_fps or 0.0
    if not space and video.height and video.height <= 576:
        if math.isclose(fps, 25.0, abs_tol=0.1) or math.isclose(fps, 50.0, abs_tol=0.1):
            space, transfer, primaries = "bt470bg", "gamma28", "bt470bg"
        else:
            space = transfer = primaries = "smpte170m"
    args = ["-color_range", color_range]
    if space:
        args.extend(["-colorspace", space])
    if transfer:
        args.extend(["-color_trc", transfer])
    if primaries:
        args.extend(["-color_primaries", primaries])
    return args


def process_file(
    analysis: AnalysisResult,
    config: JobConfig,
    tools: Toolchain | None = None,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> ProcessingResult:
    tools = tools or Toolchain.discover()
    tools.require_analysis()
    mode = resolve_mode(analysis, config.mode, restoration=config.has_restoration)
    if mode in {
        ProcessingMode.RESTORE,
        ProcessingMode.FIELDMATCH,
        ProcessingMode.HYBRID50,
        ProcessingMode.QTGMC,
    }:
        tools.require_qtgmc()
        doctor = tools.doctor(deep_qtgmc=True)
        failed = [check.detail for check in doctor.checks if check.name == "QTGMC" and not check.ok]
        if failed:
            raise DependencyError("QTGMC unavailable: " + failed[0])
    if config.dotcrawl and mode != ProcessingMode.COPY:
        tools.require_dotkill()
    source = Path(analysis.media.path)
    output_directory = Path(config.output_directory).expanduser().resolve() if config.output_directory else source.parent / "_fixed"
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / source.name
    manifest = output.with_name(output.name + MANIFEST_SUFFIX)
    source_hash = sha256_file(source)
    existing = _existing_result(output, manifest, source_hash, config, mode)
    if existing:
        return existing
    if output.exists() and not config.replace_output:
        raise OutputCollisionError(
            f"An incompatible output already exists: {output}. Choose another destination or use --replace-output."
        )
    token = uuid.uuid4().hex[:12]
    partial = output_directory / f".{source.stem}.{token}.partial.mkv"
    work = output_directory / f".fieldfix-work-{token}"
    work.mkdir(parents=True, exist_ok=False)
    try:
        if progress:
            progress(0.0, "Preparing output", None)
        if mode == ProcessingMode.COPY:
            shutil.copy2(source, partial)
            copied_hash = sha256_file(partial)
            if copied_hash != source_hash:
                raise ProcessingError("The byte-for-byte copy failed SHA-256 verification")
            validation = ValidationResult(
                valid=True,
                decoded_without_errors=True,
                streams_preserved=True,
                progressive_output=analysis.classification == Classification.PROGRESSIVE,
                expected_fps=analysis.input_fps,
                output_fps=analysis.input_fps,
                frame_rate_valid=True,
                expected_dar=_display_aspect_ratio(analysis.media),
                output_dar=_display_aspect_ratio(analysis.media),
                aspect_ratio_valid=True,
                expected_frames=analysis.idet.frames or None,
                decoded_frames=analysis.idet.frames or None,
                frame_count_valid=True,
                clean_pair_match_percent=100.0,
                cadence_valid=True,
                messages=["Byte-for-byte copy confirmed by SHA-256"],
            )
            action = "copy"
        elif mode == ProcessingMode.RESTORE:
            _encode_restore(analysis, config, tools, partial, work, cancel_event, progress)
            validation = validate_output(
                analysis, partial, tools, mode=mode, cancel_event=cancel_event, progress=progress
            )
            action = "restore"
        elif mode == ProcessingMode.FIELDMATCH:
            _encode_fieldmatch(analysis, config, tools, partial, work, cancel_event, progress)
            validation = validate_output(
                analysis, partial, tools, mode=mode, cancel_event=cancel_event, progress=progress
            )
            action = "fieldmatch"
        elif mode == ProcessingMode.HYBRID50:
            _encode_hybrid(analysis, config, tools, partial, work, cancel_event, progress)
            validation = validate_output(
                analysis, partial, tools, mode=mode, cancel_event=cancel_event, progress=progress
            )
            action = "hybrid50"
        elif mode == ProcessingMode.QTGMC:
            _encode_qtgmc(analysis, config, tools, partial, work, cancel_event, progress)
            validation = validate_output(
                analysis, partial, tools, mode=mode, cancel_event=cancel_event, progress=progress
            )
            action = "qtgmc"
        else:
            raise ProcessingError(f"Mode not implemented: {mode}")
        if not validation.valid:
            raise ProcessingError("Output validation failed: " + "; ".join(validation.messages))
        output_hash = sha256_file(partial)
        if output.exists():
            output.unlink()
        os.replace(partial, output)
        document = {
            "schema_version": 1,
            "pipeline_version": PROCESSING_PIPELINE_VERSION,
            "source": str(source),
            "source_sha256": source_hash,
            "output": str(output),
            "output_sha256": output_hash,
            "action": action,
            "config_fingerprint": config.fingerprint(),
            "config": to_dict(config),
            "analysis": to_dict(analysis),
            "validation": to_dict(validation),
        }
        json_dump_atomic(manifest, document)
        if progress:
            progress(1.0, "Completed", None)
        return ProcessingResult(
            source=str(source),
            output=str(output),
            action=action,
            skipped=False,
            source_sha256=source_hash,
            output_sha256=output_hash,
            validation=validation,
            manifest=str(manifest),
        )
    finally:
        partial.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def _existing_result(
    output: Path,
    manifest: Path,
    source_hash: str,
    config: JobConfig,
    mode: ProcessingMode,
) -> ProcessingResult | None:
    if not output.exists() or not manifest.exists():
        return None
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if document.get("source_sha256") != source_hash or document.get("config_fingerprint") != config.fingerprint():
        return None
    if document.get("action") != mode.value:
        return None
    expected_output_hash = document.get("output_sha256")
    if not expected_output_hash or sha256_file(output) != expected_output_hash:
        return None
    return ProcessingResult(
        source=str(document.get("source", "")),
        output=str(output),
        action=mode.value,
        skipped=True,
        source_sha256=source_hash,
        output_sha256=expected_output_hash,
        manifest=str(manifest),
    )


def _encode_fieldmatch(
    analysis: AnalysisResult,
    config: JobConfig,
    tools: Toolchain,
    partial: Path,
    work: Path,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
) -> None:
    assert tools.ffmpeg and tools.vspipe
    script = work / "fieldmatch.vpy"
    cache = work / "bestsource.bsindex"
    tff = (analysis.field_order or "tff") == "tff"
    script.write_text(
        _fieldmatch_script(
            analysis.media.path,
            cache,
            tff,
            decimate=analysis.cadence == "3:2",
            dotcrawl=config.dotcrawl,
        ),
        encoding="utf-8",
    )
    _encode_vapoursynth(
        analysis, config, tools, partial, script, ProcessingMode.FIELDMATCH, cancel_event, progress
    )


def _encode_restore(
    analysis: AnalysisResult,
    config: JobConfig,
    tools: Toolchain,
    partial: Path,
    work: Path,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
) -> None:
    assert tools.ffmpeg and tools.vspipe
    script = work / "restore.vpy"
    cache = work / "bestsource.bsindex"
    script.write_text(
        _progressive_script(analysis.media.path, cache, dotcrawl=config.dotcrawl),
        encoding="utf-8",
    )
    _encode_vapoursynth(
        analysis, config, tools, partial, script, ProcessingMode.RESTORE, cancel_event, progress
    )


def _copy_nonvideo_arguments() -> list[str]:
    return ["-c:a", "copy", "-c:s", "copy", "-c:t", "copy"]


def _video_metadata_arguments(analysis: AnalysisResult) -> list[str]:
    video = analysis.media.video
    args = [
        "-metadata:s:v:0",
        "BPS-eng=",
        "-metadata:s:v:0",
        "DURATION-eng=",
        "-metadata:s:v:0",
        "NUMBER_OF_FRAMES-eng=",
        "-metadata:s:v:0",
        "NUMBER_OF_BYTES-eng=",
        "-metadata:s:v:0",
        "_STATISTICS_TAGS-eng=",
    ]
    if video:
        language = video.tags.get("language")
        title = video.tags.get("title")
        if language:
            args.extend(["-metadata:s:v:0", f"language={language}"])
        if title:
            args.extend(["-metadata:s:v:0", f"title={title}"])
    return args


def _encode_qtgmc(
    analysis: AnalysisResult,
    config: JobConfig,
    tools: Toolchain,
    partial: Path,
    work: Path,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
) -> None:
    assert tools.ffmpeg and tools.vspipe
    script = work / "qtgmc.vpy"
    cache = work / "bestsource.bsindex"
    tff = (analysis.field_order or "tff") == "tff"
    script.write_text(
        _qtgmc_script(analysis.media.path, cache, tff, dotcrawl=config.dotcrawl),
        encoding="utf-8",
    )
    _encode_vapoursynth(
        analysis, config, tools, partial, script, ProcessingMode.QTGMC, cancel_event, progress
    )


def _encode_hybrid(
    analysis: AnalysisResult,
    config: JobConfig,
    tools: Toolchain,
    partial: Path,
    work: Path,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
) -> None:
    assert tools.ffmpeg and tools.vspipe
    fps = analysis.input_fps or 0.0
    if not math.isclose(fps, 25.0, abs_tol=0.08):
        raise ProcessingError(
            "Automatic hybrid50 currently supports PAL 25/50; "
            "hybrid NTSC is blocked for cadence review."
        )
    script = work / "hybrid50.vpy"
    cache = work / "bestsource.bsindex"
    tff = (analysis.field_order or "tff") == "tff"
    ranges = _hybrid_frame_ranges(analysis)
    if not ranges:
        raise ProcessingError("hybrid50 requires at least one confirmed 50i segment")
    script.write_text(
        _hybrid_script(
            analysis.media.path, cache, tff, fps, ranges, dotcrawl=config.dotcrawl
        ),
        encoding="utf-8",
    )
    _encode_vapoursynth(
        analysis, config, tools, partial, script, ProcessingMode.HYBRID50, cancel_event, progress
    )


def _encode_vapoursynth(
    analysis: AnalysisResult,
    config: JobConfig,
    tools: Toolchain,
    partial: Path,
    script: Path,
    mode: ProcessingMode,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
) -> None:
    assert tools.ffmpeg and tools.vspipe
    vspipe_command = [tools.vspipe, str(script), "-", "--container", "y4m", "--progress"]
    ffmpeg_command = [
        tools.ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        "pipe:0",
        "-i",
        analysis.media.path,
    ]
    ffmpeg_command.extend(_source_maps_for_qtgmc())
    ffmpeg_command.extend(["-map_metadata", "1", "-map_chapters", "1"])
    post_filters = restoration_filters(analysis, config)
    post_filters.append("setfield=prog")
    ffmpeg_command.extend(["-vf", ",".join(post_filters)])
    ffmpeg_command.extend(codec_arguments(config.codec, config.crf))
    ffmpeg_command.extend(color_arguments(analysis))
    ffmpeg_command.extend(_copy_nonvideo_arguments())
    ffmpeg_command.extend(_video_metadata_arguments(analysis))
    ffmpeg_command.extend(
        ["-max_muxing_queue_size", "4096", "-progress", "pipe:1", "-nostats", str(partial)]
    )
    _run_pipeline_progress(
        vspipe_command,
        ffmpeg_command,
        analysis.media.duration,
        _expected_output_frames(analysis, mode),
        cancel_event,
        progress,
    )


def _source_maps_for_qtgmc() -> list[str]:
    return [
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-map",
        "1:s?",
        "-map",
        "1:t?",
    ]


def _progressive_script(source: str, cache: Path, *, dotcrawl: bool = False) -> str:
    dotcrawl_line = (
        "output = core.dotkill.DotKillS(output, iterations=1)" if dotcrawl else ""
    )
    return f"""\
import vapoursynth as vs
from vapoursynth import core

SOURCE = {source!r}
CACHE = {str(cache)!r}

output = core.bs.VideoSource(source=SOURCE, cachepath=CACHE)
output = core.std.SetFrameProps(output, _FieldBased=0)
{dotcrawl_line}
output = core.std.SetFrameProps(output, _FieldBased=0)
output.set_output()
"""


def _fieldmatch_script(
    source: str,
    cache: Path,
    tff: bool,
    *,
    decimate: bool,
    dotcrawl: bool = False,
) -> str:
    order = 1 if tff else 0
    dotcrawl_line = (
        "output = core.dotkill.DotKillS(output, iterations=1)" if dotcrawl else ""
    )
    decimate_line = "output = core.vivtc.VDecimate(output)" if decimate else ""
    return f"""\
import vapoursynth as vs
from vapoursynth import core
from vsdeinterlace import QTempGaussMC

SOURCE = {source!r}
CACHE = {str(cache)!r}
TFF = {tff!r}

source = core.bs.VideoSource(source=SOURCE, cachepath=CACHE)
source = core.std.SetFrameProps(source, _FieldBased={2 if tff else 1})

matched = core.vivtc.VFM(
    source, order={order}, field=2, mode=1, micmatch=1
)
matched = core.std.SetFrameProps(matched, _FieldBased=0)

# QTGMC remains lazy: only a VFM frame still marked as combed requests this
# graph. Selecting the first bobbed frame keeps the same reference field used
# by VFM for both TFF and BFF input.
qtgmc = QTempGaussMC().source_match(
    mode=QTempGaussMC.SourceMatchMode.BASIC
).sharpen(strength=0)
bobbed = qtgmc.bob(source, tff=TFF)
fallback = core.std.SelectEvery(bobbed, cycle=2, offsets=0)
fallback = core.std.SetFrameProps(fallback, _FieldBased=0)

def choose(n, f):
    return fallback if int(f.props.get('_Combed', 0)) > 0 else matched

output = core.std.FrameEval(
    matched,
    choose,
    prop_src=matched,
    clip_src=[matched, fallback],
)
{dotcrawl_line}
{decimate_line}
output = core.std.SetFrameProps(output, _FieldBased=0)
output.set_output()
"""


def _qtgmc_script(
    source: str,
    cache: Path,
    tff: bool,
    *,
    dotcrawl: bool = False,
) -> str:
    dotcrawl_line = (
        "output = core.dotkill.DotKillS(output, iterations=1)" if dotcrawl else ""
    )
    return f"""\
import vapoursynth as vs
from vapoursynth import core
from vsdeinterlace import QTempGaussMC

SOURCE = {source!r}
CACHE = {str(cache)!r}
clip = core.bs.VideoSource(source=SOURCE, cachepath=CACHE)
clip = core.std.SetFrameProps(clip, _FieldBased={2 if tff else 1})
qtgmc = QTempGaussMC().source_match(
    mode=QTempGaussMC.SourceMatchMode.BASIC
).sharpen(strength=0)
output = qtgmc.bob(clip, tff={tff!r})
output = core.std.SetFrameProps(output, _FieldBased=0)
{dotcrawl_line}
output = core.std.SetFrameProps(output, _FieldBased=0)
output.set_output()
"""


def _hybrid_frame_ranges(analysis: AnalysisResult) -> list[tuple[int, int]]:
    fps = analysis.input_fps or 25.0
    total = max(1, round(analysis.media.duration * fps) * 2)
    ranges: list[tuple[int, int]] = []
    for segment in analysis.fieldmatch_residual_segments:
        start = max(0, math.floor(segment.start * fps) * 2)
        end = min(total, math.ceil(segment.end * fps) * 2)
        if end <= start:
            continue
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _hybrid_script(
    source: str,
    cache: Path,
    tff: bool,
    fps: float,
    ranges: list[tuple[int, int]],
    *,
    dotcrawl: bool = False,
) -> str:
    order = 1 if tff else 0
    dotcrawl_line = (
        "output = core.dotkill.DotKillS(output, iterations=1)" if dotcrawl else ""
    )
    return f"""\
import vapoursynth as vs
from vapoursynth import core
from vsdeinterlace import QTempGaussMC

SOURCE = {source!r}
CACHE = {str(cache)!r}
TFF = {tff!r}
FPS = {fps!r}
HYBRID_RANGES = {ranges!r}

source = core.bs.VideoSource(source=SOURCE, cachepath=CACHE)
source = core.std.SetFrameProps(source, _FieldBased={2 if tff else 1})

# VFM mode 1 is the conservative p/c + n strategy used for recoverable 2:2.
# field=2 keeps its reference field equal to the detected field order.
matched = core.vivtc.VFM(
    source, order={order}, field=2, mode=1, micmatch=1
)
matched = core.std.SetFrameProps(matched, _FieldBased=0)
matched50 = core.std.Interleave([matched, matched])

# QTGMC is evaluated lazily. It runs for confirmed 50i ranges and for an
# occasional VFM-combed frame outside them, not for the clean 25p body.
qtgmc = QTempGaussMC().source_match(
    mode=QTempGaussMC.SourceMatchMode.BASIC
).sharpen(strength=0)
bobbed50 = qtgmc.bob(source, tff=TFF)
bobbed50 = core.std.SetFrameProps(bobbed50, _FieldBased=0)

def choose(n, f):
    in_confirmed_range = any(start <= n < end for start, end in HYBRID_RANGES)
    still_combed = int(f.props.get('_Combed', 0)) > 0
    return bobbed50 if in_confirmed_range or still_combed else matched50

output = core.std.FrameEval(
    matched50,
    choose,
    prop_src=matched50,
    clip_src=[matched50, bobbed50],
)
{dotcrawl_line}
output = core.std.SetFrameProps(output, _FieldBased=0)
output.set_output()
"""


def _run_pipeline_progress(
    producer_command: list[str],
    consumer_command: list[str],
    duration: float,
    total_frames: int | None,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
) -> None:
    started = time.monotonic()
    state = FFmpegProgressState()
    producer = subprocess.Popen(
        producer_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_kwargs(),
    )
    assert producer.stdout
    consumer = subprocess.Popen(
        consumer_command,
        stdin=producer.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    producer.stdout.close()
    producer_errors: list[str] = []
    consumer_errors: list[str] = []
    producer_thread = threading.Thread(
        target=_drain_binary_lines, args=(producer.stderr, producer_errors), daemon=True
    )
    consumer_thread = threading.Thread(
        target=drain_text_stream, args=(consumer.stderr, consumer_errors), daemon=True
    )
    producer_thread.start()
    consumer_thread.start()
    assert consumer.stdout
    for line in consumer.stdout:
        if cancel_event and cancel_event.is_set():
            terminate_process_tree(consumer)
            terminate_process_tree(producer)
            raise CancelledError("Encoding cancelled")
        state.feed(line)
        value = parse_ffmpeg_progress_line(line, duration)
        if value is not None and progress:
            progress(
                value * 0.82,
                "Filtering and encoding",
                state.details(total_frames, time.monotonic() - started),
            )
    consumer_return = consumer.wait()
    producer_return = producer.wait()
    producer_thread.join(timeout=5)
    consumer_thread.join(timeout=5)
    if producer_return or consumer_return:
        messages = producer_errors[-20:] + consumer_errors[-30:]
        raise ProcessingError("QTGMC pipeline failed:\n" + "\n".join(messages))


def _drain_binary_lines(stream: object, collector: list[str]) -> None:
    if stream is None:
        return
    for line in stream:  # type: ignore[union-attr]
        if isinstance(line, bytes):
            collector.append(line.decode("utf-8", errors="replace").rstrip())
        else:
            collector.append(str(line).rstrip())


def _expected_output_frames(analysis: AnalysisResult, mode: ProcessingMode) -> int | None:
    input_frames = analysis.idet.frames
    if not input_frames and analysis.input_fps and analysis.media.duration > 0:
        input_frames = round(analysis.input_fps * analysis.media.duration)
    if not input_frames:
        return None
    if mode in {ProcessingMode.HYBRID50, ProcessingMode.QTGMC}:
        return input_frames * 2
    if mode == ProcessingMode.FIELDMATCH and analysis.cadence == "3:2":
        return round(input_frames * 4 / 5)
    return input_frames


def _frame_in_ranges(frame: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= frame < end for start, end in ranges)


def _scan_temporal_cadence(
    analysis: AnalysisResult,
    output_media: MediaInfo,
    path: Path,
    tools: Toolchain,
    mode: ProcessingMode,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
) -> tuple[int, float | None]:
    assert tools.ffmpeg
    command = [
        tools.ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-vf",
        "signalstats,metadata=mode=print:key=lavfi.signalstats.YDIF",
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
    decoded_frames = 0
    clean_pairs = 0
    matched_clean_pairs = 0
    current_frame: int | None = None
    error_lines: list[str] = []
    hybrid_ranges = _hybrid_frame_ranges(analysis) if mode == ProcessingMode.HYBRID50 else []
    expected_frames = _expected_output_frames(analysis, mode)
    started = time.monotonic()
    state = FFmpegProgressState()

    def read_metadata() -> None:
        nonlocal current_frame, decoded_frames, clean_pairs, matched_clean_pairs
        assert process.stderr
        for line in process.stderr:
            frame_match = SIGNALSTATS_FRAME_RE.search(line)
            if frame_match:
                current_frame = int(frame_match.group(1))
                decoded_frames = max(decoded_frames, current_frame + 1)
                continue
            ydif_match = SIGNALSTATS_YDIF_RE.search(line)
            if ydif_match and current_frame is not None:
                if (
                    mode == ProcessingMode.HYBRID50
                    and current_frame % 2 == 1
                    and not _frame_in_ranges(current_frame, hybrid_ranges)
                ):
                    clean_pairs += 1
                    if float(ydif_match.group(1)) <= CLEAN_PAIR_YDIF_THRESHOLD:
                        matched_clean_pairs += 1
                continue
            error_lines.append(line.rstrip())
            if len(error_lines) > 40:
                error_lines.pop(0)

    thread = threading.Thread(target=read_metadata, daemon=True)
    thread.start()
    assert process.stdout
    for line in process.stdout:
        if cancel_event and cancel_event.is_set():
            terminate_process_tree(process)
            thread.join(timeout=2)
            raise CancelledError("Temporal validation cancelled")
        state.feed(line)
        value = parse_ffmpeg_progress_line(line, output_media.duration)
        if value is not None and progress:
            progress(
                0.94 + value * 0.04,
                "Validating temporal cadence",
                state.details(expected_frames, time.monotonic() - started),
            )
    returncode = process.wait()
    thread.join(timeout=5)
    if returncode:
        raise ProcessingError("Temporal cadence scan failed:\n" + "\n".join(error_lines[-15:]))
    pair_percent = 100.0 * matched_clean_pairs / clean_pairs if clean_pairs else None
    return decoded_frames, pair_percent


def validate_output(
    analysis: AnalysisResult,
    path: Path,
    tools: Toolchain,
    *,
    mode: ProcessingMode,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> ValidationResult:
    assert tools.ffmpeg
    result = ValidationResult(valid=False)
    if progress:
        progress(0.83, "Validating structure", None)
    output_media = probe_media(path, tools)
    result.expected_fps = _expected_output_fps(analysis, mode)
    output_video = output_media.video
    result.output_fps = (
        parse_rate(output_video.average_frame_rate) or parse_rate(output_video.real_frame_rate)
        if output_video
        else None
    )
    tolerance = max(0.01, (result.expected_fps or 0.0) * 0.0005)
    result.frame_rate_valid = bool(
        result.expected_fps
        and result.output_fps
        and math.isclose(result.output_fps, result.expected_fps, abs_tol=tolerance)
    )
    if not result.frame_rate_valid:
        result.messages.append(
            f"Unexpected frame rate: {result.output_fps!r}; expected {result.expected_fps!r}"
        )
    result.expected_dar = _display_aspect_ratio(analysis.media)
    result.output_dar = _display_aspect_ratio(output_media)
    if result.expected_dar is None:
        result.aspect_ratio_valid = True
    elif result.output_dar is not None:
        expected_dar = Fraction(result.expected_dar.replace(":", "/"))
        output_dar = Fraction(result.output_dar.replace(":", "/"))
        result.aspect_ratio_valid = math.isclose(
            float(output_dar), float(expected_dar), rel_tol=0.001, abs_tol=0.001
        )
    if not result.aspect_ratio_valid:
        result.messages.append(
            f"Unexpected display aspect ratio: {result.output_dar!r}; expected {result.expected_dar!r}"
        )
    result.duration_delta = abs(output_media.duration - analysis.media.duration)
    if result.duration_delta > 0.100:
        result.messages.append(f"Duration differs by {result.duration_delta:.3f}s")
    stream_types = ("audio", "subtitle", "attachment")
    result.streams_preserved = all(
        output_media.count_streams(kind) == analysis.media.count_streams(kind) for kind in stream_types
    )
    if not result.streams_preserved:
        result.messages.append("Audio/subtitle/attachment stream counts were not preserved")
    decode_command = [
        tools.ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-f",
        "null",
        os.devnull,
    ]
    process = subprocess.Popen(
        decode_command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    while process.poll() is None:
        if cancel_event and cancel_event.wait(0.2):
            terminate_process_tree(process)
            raise CancelledError("Validation cancelled")
    errors = process.stderr.read().strip() if process.stderr else ""
    result.decoded_without_errors = process.returncode == 0 and not errors
    if not result.decoded_without_errors:
        result.messages.append("Full decoding found errors: " + errors[-500:])
    if progress:
        progress(0.90, "Checking for residual combing", None)
    residual_frames, residual_percent, _ = scan_fieldmatch_residual(
        output_media,
        tools,
        analysis.field_order or "tff",
        cancel_event=cancel_event,
    )
    field_flag = output_media.video.field_order if output_media.video else None
    result.progressive_output = residual_percent <= 1.0 and field_flag in {"progressive", "unknown", None}
    if not result.progressive_output:
        result.messages.append(
            f"Output has {residual_percent:.3f}% residual combing "
            f"({residual_frames} frames) or field_order={field_flag}"
        )
    if progress:
        progress(0.94, "Validating temporal cadence", None)
    result.expected_frames = _expected_output_frames(analysis, mode)
    result.decoded_frames, result.clean_pair_match_percent = _scan_temporal_cadence(
        analysis,
        output_media,
        path,
        tools,
        mode,
        cancel_event,
        progress,
    )
    result.frame_count_valid = bool(
        result.expected_frames is not None
        and result.decoded_frames is not None
        and abs(result.decoded_frames - result.expected_frames) <= 2
    )
    if not result.frame_count_valid:
        result.messages.append(
            f"Decoded frame count is {result.decoded_frames}; expected {result.expected_frames}"
        )
    pair_pattern_valid = bool(
        mode != ProcessingMode.HYBRID50
        or result.clean_pair_match_percent is None
        or result.clean_pair_match_percent >= CLEAN_PAIR_REQUIRED_PERCENT
    )
    result.cadence_valid = result.frame_count_valid and pair_pattern_valid
    if not pair_pattern_valid:
        result.messages.append(
            f"Only {result.clean_pair_match_percent:.3f}% of clean hybrid pairs preserve the expected 25p-to-50p cadence"
        )
    result.valid = (
        result.duration_delta <= 0.100
        and result.streams_preserved
        and result.decoded_without_errors
        and result.progressive_output
        and result.frame_rate_valid
        and result.aspect_ratio_valid
        and result.cadence_valid
    )
    if result.valid:
        result.messages.append(
            "Duration, streams, decoding, frame count, cadence, frame rate, aspect ratio and progressiveness validated"
        )
    if progress:
        progress(0.99, "Validation completed", None)
    return result


def _expected_output_fps(analysis: AnalysisResult, mode: ProcessingMode) -> float | None:
    fps = analysis.input_fps
    if not fps:
        return None
    if mode in {ProcessingMode.HYBRID50, ProcessingMode.QTGMC}:
        return fps * 2
    if mode == ProcessingMode.FIELDMATCH and analysis.cadence == "3:2":
        return 24000 / 1001
    return fps
