from __future__ import annotations

from pathlib import Path

import pytest

from dvd_fieldfix.models import (
    AnalysisResult,
    Classification,
    CodecProfile,
    CropMargins,
    IDetStats,
    JobConfig,
    MediaInfo,
    ProcessingMode,
    StreamInfo,
    TimelineSegment,
)
from dvd_fieldfix.processing import (
    _expected_output_fps,
    _hybrid_frame_ranges,
    _hybrid_script,
    _sar_after_crop,
    codec_arguments,
    effective_crop,
    fieldmatch_filter,
    resolve_mode,
)
from dvd_fieldfix.tools import AmbiguousSourceError, parse_ffmpeg_progress_line


def analysis(classification: Classification = Classification.FIELD_MATCHABLE) -> AnalysisResult:
    info = MediaInfo(
        path=str(Path("input.mkv").resolve()),
        duration=60,
        start_time=0,
        bit_rate=None,
        format_name="matroska",
        format_tags={},
        streams=[
            StreamInfo(
                index=0,
                codec_type="video",
                width=720,
                height=576,
                average_frame_rate="25/1",
                sample_aspect_ratio="16:15",
                display_aspect_ratio="4:3",
            )
        ],
        chapters=[],
    )
    return AnalysisResult(
        media=info,
        classification=classification,
        confidence=0.99,
        reason="test",
        idet=IDetStats(frames=1500, tff=1500),
        field_order="tff",
        cadence="2:2",
        input_fps=25,
        suggested_mode=ProcessingMode.FIELDMATCH,
    )


def test_codec_profiles() -> None:
    h264 = codec_arguments(CodecProfile.H264)
    hevc10 = codec_arguments(CodecProfile.HEVC10)
    assert "libx264" in h264
    assert "yuv420p10le" in hevc10
    assert h264[h264.index("-preset") + 1] == "veryslow"
    assert hevc10[hevc10.index("-preset") + 1] == "veryslow"
    assert "ffv1" in codec_arguments(CodecProfile.FFV1)


def test_fieldmatch_filter_is_conditional_and_progressive() -> None:
    value = fieldmatch_filter(analysis(), JobConfig())
    assert "fieldmatch=order=tff" in value
    assert "yadif=mode=send_frame:parity=auto:deint=interlaced" in value
    assert "setsar=16/15" in value
    assert value.endswith("setfield=prog")


def test_restoration_is_opt_in() -> None:
    value = fieldmatch_filter(
        analysis(),
        JobConfig(crop=CropMargins(8, 0, 8, 0), denoise=True),
    )
    assert "crop=iw-16" in value
    assert "hqdn3d=1:1:3:3" in value


def test_auto_crop_uses_analysis_suggestion() -> None:
    item = analysis()
    item.crop_suggestion = "crop=712:576:0:0"
    config = JobConfig(auto_crop=True)
    assert effective_crop(item, config) == CropMargins(0, 0, 8, 0)
    assert "crop=iw-8:ih-0:0:0" in fieldmatch_filter(item, config)


def test_manual_crop_overrides_auto_crop() -> None:
    item = analysis()
    item.crop_suggestion = "crop=712:576:0:0"
    config = JobConfig(auto_crop=True, crop=CropMargins(4, 2, 6, 2))
    assert effective_crop(item, config) == CropMargins(4, 2, 6, 2)


def test_sar_preserves_four_by_three_after_crop() -> None:
    assert _sar_after_crop("4:3", "16:15", 720, 576, 704, 576) == pytest.approx(12 / 11)


def test_auto_mode_and_ambiguity() -> None:
    assert resolve_mode(analysis(), ProcessingMode.AUTO) == ProcessingMode.FIELDMATCH
    assert (
        resolve_mode(analysis(Classification.HYBRID), ProcessingMode.AUTO)
        == ProcessingMode.HYBRID50
    )
    with pytest.raises(AmbiguousSourceError):
        resolve_mode(analysis(Classification.AMBIGUOUS), ProcessingMode.AUTO)


def test_progress_parser() -> None:
    assert parse_ffmpeg_progress_line("out_time_us=5000000", 10) == 0.5
    assert parse_ffmpeg_progress_line("progress=end", 10) == 1.0


def test_hybrid_ranges_and_script_preserve_25p_and_50i() -> None:
    item = analysis(Classification.HYBRID)
    item.fieldmatch_residual_segments = [TimelineSegment(10.5, 13.5, 75.0)]
    assert _hybrid_frame_ranges(item) == [(524, 676)]
    script = _hybrid_script(item.media.path, Path("cache.bsindex"), True, 25.0, [(524, 676)])
    assert "core.vivtc.VFM" in script
    assert "core.std.Interleave([matched, matched])" in script
    assert "SourceMatchMode.BASIC" in script
    assert "qtgmc.bob" in script
    assert "FrameEval" in script
    assert "_Combed" in script


def test_expected_output_rate_tracks_processing_mode() -> None:
    item = analysis()
    assert _expected_output_fps(item, ProcessingMode.FIELDMATCH) == 25
    assert _expected_output_fps(item, ProcessingMode.HYBRID50) == 50
    assert _expected_output_fps(item, ProcessingMode.QTGMC) == 50
