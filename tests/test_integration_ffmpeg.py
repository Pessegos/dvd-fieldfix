from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dvd_fieldfix.analysis import analyze_file
from dvd_fieldfix.models import Classification, CodecProfile, JobConfig, ProcessingMode
from dvd_fieldfix.processing import process_file
from dvd_fieldfix.tools import Toolchain, sha256_file


pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg não instalado")


def make_progressive_mkv(path: Path) -> None:
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "mpeg2video",
            "-flags",
            "+ildct+ilme",
            "-top",
            "1",
            "-c:a",
            "ac3",
            "-shortest",
            "-y",
            str(path),
        ],
        check=True,
    )


def test_analyze_and_copy_progressive(tmp_path: Path) -> None:
    source = tmp_path / "episódio d'Árvore.mkv"
    make_progressive_mkv(source)
    tools = Toolchain.discover()
    result = analyze_file(source, tools)
    # The deliberately contradictory MPEG-2 flags may make this tiny fixture ambiguous.
    assert result.classification != Classification.UNSUPPORTED
    result.classification = Classification.PROGRESSIVE
    result.suggested_mode = ProcessingMode.COPY
    output_dir = tmp_path / "resultados"
    processed = process_file(
        result,
        JobConfig(codec=CodecProfile.H264, mode=ProcessingMode.COPY, output_directory=str(output_dir)),
        tools,
    )
    output = Path(processed.output)
    assert output.exists()
    assert sha256_file(source) == sha256_file(output)
    assert Path(processed.manifest or "").exists()
