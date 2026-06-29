from __future__ import annotations

from collections import Counter
from unittest.mock import patch

import pytest

from dvd_fieldfix.analysis import (
    _combine_crop_rectangles,
    _residual_segments,
    _segments_from_bins,
    analyze_file,
    parse_rate,
)
from dvd_fieldfix.models import (
    Classification,
    IDetStats,
    MediaInfo,
    ProcessingMode,
    StreamInfo,
    TimelineSegment,
)
from dvd_fieldfix.tools import Toolchain


def media(fps: str = "25/1") -> MediaInfo:
    return MediaInfo(
        path="input.mkv",
        duration=100.0,
        start_time=0.0,
        bit_rate=None,
        format_name="matroska",
        format_tags={},
        streams=[
            StreamInfo(
                index=0,
                codec_type="video",
                codec_name="mpeg2video",
                width=720,
                height=576,
                pixel_format="yuv420p",
                field_order="tt",
                average_frame_rate=fps,
                sample_aspect_ratio="16:15",
                display_aspect_ratio="4:3",
            )
        ],
        chapters=[],
    )


def tools() -> Toolchain:
    return Toolchain("ffmpeg", "ffprobe", None)


def test_parse_rate() -> None:
    assert parse_rate("25/1") == 25
    assert parse_rate("30000/1001") == pytest.approx(29.97002997)
    assert parse_rate("0/0") is None


def test_segments_merge_adjacent_active_bins() -> None:
    bins = {
        0: Counter(tff=250),
        1: Counter(tff=200, progressive=50),
        2: Counter(progressive=250),
        4: Counter(tff=150, progressive=100),
    }
    segments = _segments_from_bins(bins, 5)
    assert [(item.start, item.end) for item in segments] == [(0.0, 2.0), (4.0, 5.0)]


def test_residual_segments_are_precise_padded_and_bridge_short_gaps() -> None:
    segments = _residual_segments(Counter({10: 20, 11: 22, 13: 25}), 25, 30)
    assert len(segments) == 1
    assert segments[0].start == 9.5
    assert segments[0].end == 14.5
    assert segments[0].interlaced_percent > 50


def test_crop_rectangles_use_conservative_union() -> None:
    rectangles = [
        (712, 570, 0, 2),
        (708, 576, 4, 0),
        (712, 576, 0, 0),
    ]
    # Only the 8-pixel right border is black in every sample.
    assert _combine_crop_rectangles(rectangles, 720, 576) == (712, 576, 0, 0)


def test_crop_rectangles_reject_invalid_values() -> None:
    assert _combine_crop_rectangles([(800, 576, 0, 0)], 720, 576) is None


@patch("dvd_fieldfix.analysis.detect_crop", return_value="crop=712:576:0:0")
@patch("dvd_fieldfix.analysis.scan_idet")
@patch("dvd_fieldfix.analysis.probe_media")
def test_progressive_classification(probe, scan, _crop) -> None:
    probe.return_value = media()
    scan.return_value = (IDetStats(frames=2500, tff=2, progressive=2498), [])
    result = analyze_file("input.mkv", tools())
    assert result.classification == Classification.PROGRESSIVE
    assert result.suggested_mode == ProcessingMode.COPY


@patch("dvd_fieldfix.analysis.detect_crop", return_value=None)
@patch(
    "dvd_fieldfix.analysis.scan_fieldmatch_residual",
    return_value=(50, 0.5, [TimelineSegment(40.5, 42.5, 100.0)]),
)
@patch("dvd_fieldfix.analysis.scan_idet")
@patch("dvd_fieldfix.analysis.probe_media")
def test_local_interlace_is_not_hidden_by_low_global_percentage(probe, scan, _residual, _crop) -> None:
    probe.return_value = media()
    scan.return_value = (
        IDetStats(frames=10000, tff=50, progressive=9950),
        [TimelineSegment(40.0, 42.0, 100.0)],
    )
    result = analyze_file("input.mkv", tools())
    assert result.classification == Classification.HYBRID
    assert result.suggested_mode == ProcessingMode.HYBRID50


@patch("dvd_fieldfix.analysis.detect_crop", return_value=None)
@patch("dvd_fieldfix.analysis.scan_fieldmatch_residual", return_value=(2, 0.08, []))
@patch("dvd_fieldfix.analysis.scan_idet")
@patch("dvd_fieldfix.analysis.probe_media")
def test_pal_fieldmatch_classification(probe, scan, _residual, _crop) -> None:
    probe.return_value = media()
    scan.return_value = (IDetStats(frames=2500, tff=2480, bff=2, progressive=18), [])
    result = analyze_file("input.mkv", tools())
    assert result.classification == Classification.FIELD_MATCHABLE
    assert result.cadence == "2:2"
    assert result.suggested_output_fps == "25/1"


@patch("dvd_fieldfix.analysis.detect_crop", return_value=None)
@patch(
    "dvd_fieldfix.analysis.scan_fieldmatch_residual",
    return_value=(90, 3.6, [TimelineSegment(80.5, 84.5, 80.0)]),
)
@patch("dvd_fieldfix.analysis.scan_idet")
@patch("dvd_fieldfix.analysis.probe_media")
def test_pal_hybrid_classification(probe, scan, _residual, _crop) -> None:
    probe.return_value = media()
    scan.return_value = (IDetStats(frames=2500, tff=2480, bff=2, progressive=18), [])
    result = analyze_file("input.mkv", tools())
    assert result.classification == Classification.HYBRID
    assert result.suggested_mode == ProcessingMode.HYBRID50
    assert result.suggested_output_fps == "50/1"


@patch("dvd_fieldfix.analysis.detect_crop", return_value=None)
@patch("dvd_fieldfix.analysis.scan_fieldmatch_residual", return_value=(700, 28.0, []))
@patch("dvd_fieldfix.analysis.scan_idet")
@patch("dvd_fieldfix.analysis.probe_media")
def test_true_interlace_classification(probe, scan, _residual, _crop) -> None:
    probe.return_value = media()
    scan.return_value = (IDetStats(frames=2500, tff=2490, progressive=10), [])
    result = analyze_file("input.mkv", tools())
    assert result.classification == Classification.TRUE_INTERLACED
    assert result.suggested_mode == ProcessingMode.QTGMC
    assert result.suggested_output_fps == "50/1"


@patch("dvd_fieldfix.analysis.detect_crop", return_value=None)
@patch("dvd_fieldfix.analysis.scan_fieldmatch_residual", return_value=(300, 12.0, []))
@patch("dvd_fieldfix.analysis.scan_idet")
@patch("dvd_fieldfix.analysis.probe_media")
def test_middle_residual_is_ambiguous(probe, scan, _residual, _crop) -> None:
    probe.return_value = media()
    scan.return_value = (IDetStats(frames=2500, tff=2490, progressive=10), [])
    result = analyze_file("input.mkv", tools())
    assert result.classification == Classification.AMBIGUOUS
    assert result.suggested_mode is None
