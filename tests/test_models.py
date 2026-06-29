from __future__ import annotations

import pytest

from dvd_fieldfix.models import CodecProfile, CropMargins, JobConfig, ProcessingMode


def test_crop_parsing() -> None:
    assert CropMargins.parse(None) == CropMargins()
    assert CropMargins.parse("8:2:10:4") == CropMargins(8, 2, 10, 4)


@pytest.mark.parametrize("value", ["1:0:0:0", "-2:0:0:0", "2:2:2", "a:0:0:0"])
def test_crop_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        CropMargins.parse(value)


def test_job_fingerprint_is_stable_and_sensitive() -> None:
    first = JobConfig(codec=CodecProfile.H264, mode=ProcessingMode.AUTO)
    same = JobConfig(codec=CodecProfile.H264, mode=ProcessingMode.AUTO)
    different = JobConfig(codec=CodecProfile.HEVC10, mode=ProcessingMode.AUTO)
    assert first.fingerprint() == same.fingerprint()
    assert first.fingerprint() != different.fingerprint()

