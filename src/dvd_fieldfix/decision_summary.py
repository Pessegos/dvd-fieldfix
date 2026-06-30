from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from .models import AnalysisResult, CodecProfile, JobConfig, ProcessingMode, ProcessingResult
from .processing import codec_arguments, effective_crop, resolve_mode


@dataclass(slots=True)
class DecisionEntry:
    analysis: AnalysisResult | None
    override: ProcessingMode
    status: str
    source: Path
    result: ProcessingResult | None = None


def _clock(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _segment_text(analysis: AnalysisResult) -> str:
    segments = analysis.fieldmatch_residual_segments or analysis.hybrid_segments
    if not segments:
        return "none"
    return ", ".join(
        f"{segment.start:.3f}-{segment.end:.3f}s ({segment.interlaced_percent:.1f}%)"
        for segment in segments
    )


def _mode_reason(analysis: AnalysisResult, mode: ProcessingMode) -> str:
    if mode == ProcessingMode.COPY:
        return "The source is progressive and no restoration was requested; preserve it byte for byte."
    if mode == ProcessingMode.RESTORE:
        return "The source is progressive, but selected restoration requires a validated video encode."
    if mode == ProcessingMode.FIELDMATCH:
        return "The original progressive frames are recoverable by field matching; QTGMC is only a residual fallback."
    if mode == ProcessingMode.HYBRID50:
        return "The source mixes recoverable 25p and genuine 50i; preserve both at progressive 50p."
    if mode == ProcessingMode.QTGMC:
        return "Motion remains genuinely interlaced after field matching; preserve temporal resolution with QTGMC bob."
    return analysis.reason


def build_decision_summary(
    entries: list[DecisionEntry],
    config: JobConfig,
    *,
    parallel_jobs: int,
    profile_name: str,
) -> str:
    lines = [
        "DVD FieldFix — Decision Summary",
        "=" * 34,
        f"Generated: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Series profile: {profile_name}",
        f"Files in scope: {len(entries)}",
        "",
        "Series-wide encoding decisions",
        "-" * 30,
        f"Codec: {config.codec.value}",
        f"CRF: {'not applicable (lossless)' if config.codec == CodecProfile.FFV1 else f'{config.crf:g}'}",
        "Encoder arguments: " + " ".join(codec_arguments(config.codec, config.crf)),
        f"Parallel episode jobs: {parallel_jobs}",
        "Rate-control rationale: CRF targets consistent quality; FFV1 is mathematically lossless.",
        "Animation tune: disabled; content-specific psychovisual changes are not assumed safe for a mixed DVD master.",
        "Upscaling: disabled.",
        "Audio/subtitles/attachments/chapters: copied without re-encoding.",
        "Original replacement: forbidden; outputs use partial files, full validation and atomic rename.",
        "",
        "Restoration decisions",
        "-" * 21,
        f"Manual crop: {config.crop.left}:{config.crop.top}:{config.crop.right}:{config.crop.bottom}",
        f"Automatic crop: {'enabled' if config.auto_crop else 'disabled'}",
        f"Light denoise: {'enabled' if config.denoise else 'disabled'}",
        f"Dot-crawl/rainbow cleanup: {'enabled' if config.dotcrawl else 'disabled'}",
        "Safety note: crop/denoise/DotKill are never inferred from interlace detection alone.",
        "Current automatic-restoration policy: preserve when evidence is uncertain; destructive filtering requires a calibrated series/DVD assessment.",
        "",
        "Workflow",
        "-" * 8,
        "Analyze is optional and exists for inspection. Analyze + Process always performs any missing analysis first.",
    ]

    for index, entry in enumerate(entries, 1):
        lines.extend(["", "", f"File {index}: {entry.source.name}", "-" * (8 + len(entry.source.name))])
        analysis = entry.analysis
        if analysis is None:
            lines.extend(
                [
                    f"Source: {entry.source}",
                    "Analysis: pending",
                    f"Queue status: {entry.status}",
                    "Decision: Analyze + Process will analyze this file before choosing a temporal path.",
                ]
            )
            continue

        media = analysis.media
        video = media.video
        try:
            selected_mode = resolve_mode(
                analysis,
                entry.override,
                restoration=config.has_restoration,
            )
            selected_mode_text = selected_mode.value
            rationale = _mode_reason(analysis, selected_mode)
        except Exception as exc:
            selected_mode_text = "blocked pending review"
            rationale = str(exc)

        lines.extend(
            [
                f"Source: {media.path}",
                f"Duration: {_clock(media.duration)} ({media.duration:.3f}s)",
                f"Container: {media.format_name}",
                f"Video: {video.codec_name if video else 'unknown'}, "
                f"{video.width if video else '?'}x{video.height if video else '?'}, "
                f"{analysis.input_fps or 0:.6f} fps, pixel format {video.pixel_format if video else 'unknown'}",
                f"Aspect ratio: SAR {video.sample_aspect_ratio if video else 'unknown'}, "
                f"DAR {video.display_aspect_ratio if video else 'unknown'}",
                f"Color tags: range={video.color_range if video else None}, "
                f"space={video.color_space if video else None}, transfer={video.color_transfer if video else None}, "
                f"primaries={video.color_primaries if video else None}",
                f"Streams: video={media.count_streams('video')}, audio={media.count_streams('audio')}, "
                f"subtitles={media.count_streams('subtitle')}, attachments={media.count_streams('attachment')}",
                "",
                f"Classification: {analysis.classification.value}",
                f"Confidence: {analysis.confidence:.2%}",
                f"Reason: {analysis.reason}",
                f"IDet frames: total={analysis.idet.frames}, TFF={analysis.idet.tff}, BFF={analysis.idet.bff}, "
                f"progressive={analysis.idet.progressive}, undetermined={analysis.idet.undetermined}",
                f"IDet interlaced: {analysis.idet.interlaced_percent:.6f}%",
                f"Field order: {analysis.field_order or 'not required'} "
                f"(consistency {analysis.idet.field_order_consistency:.2%})",
                f"Cadence: {analysis.cadence or 'not established'}",
                f"Field-match residual: {analysis.fieldmatch_residual_frames if analysis.fieldmatch_residual_frames is not None else 'not tested'} "
                f"frames / {analysis.fieldmatch_residual_percent if analysis.fieldmatch_residual_percent is not None else 'n/a'}%",
                f"Residual/hybrid segments: {_segment_text(analysis)}",
                f"Detected crop suggestion: {analysis.crop_suggestion or 'none'}",
                f"Effective crop: {effective_crop(analysis, config)}",
                f"Warnings: {'; '.join(analysis.warnings) if analysis.warnings else 'none'}",
                "",
                f"Per-file override: {entry.override.value}",
                f"Chosen action: {selected_mode_text}",
                f"Action rationale: {rationale}",
                f"Expected output frame rate: {analysis.suggested_output_fps or 'same as source'}",
                f"Queue status: {entry.status}",
            ]
        )

        if entry.result:
            validation = entry.result.validation
            lines.extend(
                [
                    f"Output: {entry.result.output}",
                    f"Completed action: {entry.result.action}",
                    f"Source SHA-256: {entry.result.source_sha256}",
                    f"Output SHA-256: {entry.result.output_sha256 or 'not available'}",
                    f"Manifest: {entry.result.manifest or 'not available'}",
                ]
            )
            if validation:
                lines.extend(
                    [
                        f"Validation: {'PASSED' if validation.valid else 'FAILED'}",
                        f"Decoded frames: {validation.decoded_frames}/{validation.expected_frames}",
                        f"Output FPS: {validation.output_fps} (expected {validation.expected_fps})",
                        f"Duration delta: {validation.duration_delta:.6f}s",
                        f"Progressive output: {validation.progressive_output}",
                        f"Streams preserved: {validation.streams_preserved}",
                        f"Aspect ratio preserved: {validation.aspect_ratio_valid}",
                        f"Temporal cadence valid: {validation.cadence_valid}",
                        f"Validation messages: {'; '.join(validation.messages)}",
                    ]
                )

    return "\n".join(lines) + "\n"
