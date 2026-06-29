from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPORT_SCHEMA_VERSION = 1
PROCESSING_PIPELINE_VERSION = 4


class Classification(str, enum.Enum):
    PROGRESSIVE = "PROGRESSIVE"
    FIELD_MATCHABLE = "FIELD_MATCHABLE"
    HYBRID = "HYBRID"
    TRUE_INTERLACED = "TRUE_INTERLACED"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"


class ProcessingMode(str, enum.Enum):
    AUTO = "auto"
    COPY = "copy"
    FIELDMATCH = "fieldmatch"
    HYBRID50 = "hybrid50"
    QTGMC = "qtgmc"


class CodecProfile(str, enum.Enum):
    H264 = "h264"
    HEVC10 = "hevc10"
    FFV1 = "ffv1"


@dataclass(slots=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str = ""
    tags: dict[str, Any] = field(default_factory=dict)
    disposition: dict[str, int] = field(default_factory=dict)
    width: int | None = None
    height: int | None = None
    pixel_format: str | None = None
    field_order: str | None = None
    average_frame_rate: str | None = None
    real_frame_rate: str | None = None
    sample_aspect_ratio: str | None = None
    display_aspect_ratio: str | None = None
    color_range: str | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: str | None = None


@dataclass(slots=True)
class MediaInfo:
    path: str
    duration: float
    start_time: float
    bit_rate: int | None
    format_name: str
    format_tags: dict[str, Any]
    streams: list[StreamInfo]
    chapters: list[dict[str, Any]]

    @property
    def video(self) -> StreamInfo | None:
        return next((stream for stream in self.streams if stream.codec_type == "video"), None)

    def count_streams(self, codec_type: str) -> int:
        return sum(stream.codec_type == codec_type for stream in self.streams)


@dataclass(slots=True)
class IDetStats:
    frames: int = 0
    tff: int = 0
    bff: int = 0
    progressive: int = 0
    undetermined: int = 0
    repeated_neither: int = 0
    repeated_top: int = 0
    repeated_bottom: int = 0

    @property
    def interlaced_frames(self) -> int:
        return self.tff + self.bff

    @property
    def interlaced_percent(self) -> float:
        return 100.0 * self.interlaced_frames / self.frames if self.frames else 0.0

    @property
    def repeated_percent(self) -> float:
        total = self.repeated_neither + self.repeated_top + self.repeated_bottom
        repeated = self.repeated_top + self.repeated_bottom
        return 100.0 * repeated / total if total else 0.0

    @property
    def field_order(self) -> str | None:
        if self.tff == self.bff == 0:
            return None
        return "tff" if self.tff >= self.bff else "bff"

    @property
    def field_order_consistency(self) -> float:
        total = self.tff + self.bff
        return max(self.tff, self.bff) / total if total else 0.0


@dataclass(slots=True)
class TimelineSegment:
    start: float
    end: float
    interlaced_percent: float


@dataclass(slots=True)
class AnalysisResult:
    media: MediaInfo
    classification: Classification
    confidence: float
    reason: str
    idet: IDetStats = field(default_factory=IDetStats)
    fieldmatch_residual_frames: int | None = None
    fieldmatch_residual_percent: float | None = None
    field_order: str | None = None
    cadence: str | None = None
    input_fps: float | None = None
    suggested_output_fps: str | None = None
    crop_suggestion: str | None = None
    hybrid_segments: list[TimelineSegment] = field(default_factory=list)
    fieldmatch_residual_segments: list[TimelineSegment] = field(default_factory=list)
    suggested_mode: ProcessingMode | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CropMargins:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    @property
    def enabled(self) -> bool:
        return any((self.left, self.top, self.right, self.bottom))

    @classmethod
    def parse(cls, value: str | None) -> "CropMargins":
        if not value:
            return cls()
        try:
            numbers = [int(part.strip()) for part in value.split(":")]
        except ValueError as exc:
            raise ValueError("crop deve usar o formato esquerda:topo:direita:fundo") from exc
        if len(numbers) != 4 or any(number < 0 or number % 2 for number in numbers):
            raise ValueError("as quatro margens de crop têm de ser pares e não negativas")
        return cls(*numbers)


@dataclass(slots=True)
class JobConfig:
    codec: CodecProfile = CodecProfile.H264
    mode: ProcessingMode = ProcessingMode.AUTO
    output_directory: str | None = None
    crop: CropMargins = field(default_factory=CropMargins)
    auto_crop: bool = False
    denoise: bool = False
    replace_output: bool = False

    def fingerprint(self) -> str:
        document = {
            "pipeline_version": PROCESSING_PIPELINE_VERSION,
            "config": to_dict(self),
        }
        payload = json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    duration_delta: float = 0.0
    decoded_without_errors: bool = False
    streams_preserved: bool = False
    progressive_output: bool = False
    expected_fps: float | None = None
    output_fps: float | None = None
    frame_rate_valid: bool = False
    expected_dar: str | None = None
    output_dar: str | None = None
    aspect_ratio_valid: bool = False
    messages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProcessingResult:
    source: str
    output: str
    action: str
    skipped: bool
    source_sha256: str
    output_sha256: str | None = None
    validation: ValidationResult | None = None
    manifest: str | None = None


def to_dict(value: Any) -> Any:
    if isinstance(value, IDetStats):
        document = {item.name: to_dict(getattr(value, item.name)) for item in dataclasses.fields(value)}
        document.update(
            {
                "interlaced_frames": value.interlaced_frames,
                "interlaced_percent": value.interlaced_percent,
                "repeated_percent": value.repeated_percent,
                "field_order": value.field_order,
                "field_order_consistency": value.field_order_consistency,
            }
        )
        return document
    if dataclasses.is_dataclass(value):
        return {item.name: to_dict(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(item) for item in value]
    return value


def report_document(results: list[AnalysisResult], generated_at: str) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "results": [to_dict(result) for result in results],
    }
