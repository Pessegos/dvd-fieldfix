from __future__ import annotations

from pathlib import Path

from dvd_fieldfix.decision_summary import DecisionEntry, build_decision_summary
from dvd_fieldfix.models import (
    AnalysisResult,
    Classification,
    CodecProfile,
    IDetStats,
    JobConfig,
    MediaInfo,
    ProcessingMode,
    StreamInfo,
)


def sample_analysis(path: Path) -> AnalysisResult:
    media = MediaInfo(
        path=str(path),
        duration=60.0,
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
                field_order="progressive",
                average_frame_rate="25/1",
                sample_aspect_ratio="16:15",
                display_aspect_ratio="4:3",
            ),
            StreamInfo(index=1, codec_type="audio", codec_name="ac3"),
        ],
        chapters=[],
    )
    return AnalysisResult(
        media=media,
        classification=Classification.PROGRESSIVE,
        confidence=0.99,
        reason="IDet found only progressive frames",
        idet=IDetStats(frames=1500, progressive=1500),
        cadence="progressive",
        input_fps=25.0,
        suggested_output_fps="25/1",
        suggested_mode=ProcessingMode.COPY,
    )


def test_decision_summary_explains_analyzed_and_pending_files(tmp_path: Path) -> None:
    analyzed_path = tmp_path / "episode 1.mkv"
    pending_path = tmp_path / "episode 2.mkv"
    summary = build_decision_summary(
        [
            DecisionEntry(
                analysis=sample_analysis(analyzed_path),
                override=ProcessingMode.AUTO,
                status="Analyzed",
                source=analyzed_path,
            ),
            DecisionEntry(
                analysis=None,
                override=ProcessingMode.AUTO,
                status="Not analyzed",
                source=pending_path,
            ),
        ],
        JobConfig(codec=CodecProfile.H264, crf=14),
        parallel_jobs=2,
        profile_name="Test series",
    )
    assert "Series profile: Test series" in summary
    assert "Chosen action: copy" in summary
    assert "preserve it byte for byte" in summary
    assert "Analysis: pending" in summary
    assert "its label shows Analyze + Process when that work is pending" in summary


def test_decision_summary_hides_crf_meaning_for_ffv1(tmp_path: Path) -> None:
    path = tmp_path / "episode.mkv"
    summary = build_decision_summary(
        [
            DecisionEntry(
                analysis=sample_analysis(path),
                override=ProcessingMode.AUTO,
                status="Analyzed",
                source=path,
            )
        ],
        JobConfig(codec=CodecProfile.FFV1),
        parallel_jobs=1,
        profile_name="Lossless",
    )
    assert "CRF: not applicable (lossless)" in summary
