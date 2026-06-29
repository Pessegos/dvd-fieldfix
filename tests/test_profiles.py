from __future__ import annotations

import json

import pytest

from dvd_fieldfix.models import CodecProfile, CropMargins, JobConfig
from dvd_fieldfix.profiles import load_series_profile, save_series_profile
from dvd_fieldfix.tools import FieldFixError


def test_series_profile_roundtrip(tmp_path) -> None:
    path = tmp_path / "zoo-series.json"
    config = JobConfig(
        codec=CodecProfile.HEVC10,
        crf=13.5,
        crop=CropMargins(4, 2, 6, 0),
        auto_crop=False,
        denoise=True,
        dotcrawl=True,
    )
    save_series_profile(path, "Zoo Series", config, parallel_jobs=2)
    loaded = load_series_profile(path)
    assert loaded.name == "Zoo Series"
    assert loaded.parallel_jobs == 2
    assert loaded.config.codec == CodecProfile.HEVC10
    assert loaded.config.crf == 13.5
    assert loaded.config.crop == CropMargins(4, 2, 6, 0)
    assert loaded.config.denoise
    assert loaded.config.dotcrawl
    assert loaded.config.output_directory is None


def test_series_profile_rejects_unknown_schema(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(FieldFixError):
        load_series_profile(path)
