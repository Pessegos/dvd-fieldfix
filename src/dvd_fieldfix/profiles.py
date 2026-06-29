from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import CodecProfile, CropMargins, JobConfig
from .tools import FieldFixError, json_dump_atomic


SERIES_PROFILE_SCHEMA_VERSION = 1


@dataclass(slots=True)
class SeriesProfile:
    name: str
    config: JobConfig
    parallel_jobs: int = 1


def save_series_profile(
    path: str | Path,
    name: str,
    config: JobConfig,
    parallel_jobs: int,
) -> None:
    if parallel_jobs not in {1, 2}:
        raise ValueError("Parallel jobs must be 1 or 2")
    document = {
        "schema_version": SERIES_PROFILE_SCHEMA_VERSION,
        "name": name.strip() or Path(path).stem,
        "settings": {
            "codec": config.codec.value,
            "crf": config.crf,
            "crop": {
                "left": config.crop.left,
                "top": config.crop.top,
                "right": config.crop.right,
                "bottom": config.crop.bottom,
            },
            "auto_crop": config.auto_crop,
            "denoise": config.denoise,
            "dotcrawl": config.dotcrawl,
            "parallel_jobs": parallel_jobs,
        },
    }
    json_dump_atomic(Path(path), document)


def load_series_profile(path: str | Path) -> SeriesProfile:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        if document.get("schema_version") != SERIES_PROFILE_SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        settings = document["settings"]
        crop_data = settings.get("crop", {})
        crop = CropMargins(
            int(crop_data.get("left", 0)),
            int(crop_data.get("top", 0)),
            int(crop_data.get("right", 0)),
            int(crop_data.get("bottom", 0)),
        )
        if any(value < 0 or value % 2 for value in (crop.left, crop.top, crop.right, crop.bottom)):
            raise ValueError("crop margins must be even and non-negative")
        parallel_jobs = int(settings.get("parallel_jobs", 1))
        if parallel_jobs not in {1, 2}:
            raise ValueError("parallel_jobs must be 1 or 2")
        config = JobConfig(
            codec=CodecProfile(settings["codec"]),
            crf=float(settings.get("crf", 14.0)),
            crop=crop,
            auto_crop=bool(settings.get("auto_crop", False)),
            denoise=bool(settings.get("denoise", False)),
            dotcrawl=bool(settings.get("dotcrawl", False)),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FieldFixError(f"Invalid series profile {source.name}: {exc}") from exc
    return SeriesProfile(str(document.get("name") or source.stem), config, parallel_jobs)
