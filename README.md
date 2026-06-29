# DVD FieldFix

[![Build](https://github.com/Pessegos/dvd-fieldfix/actions/workflows/build-release.yml/badge.svg)](https://github.com/Pessegos/dvd-fieldfix/actions/workflows/build-release.yml)
[![Latest release](https://img.shields.io/github/v/release/Pessegos/dvd-fieldfix)](https://github.com/Pessegos/dvd-fieldfix/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> Intelligent field reconstruction, hybrid deinterlacing and archival encoding for DVD sources.

DVD FieldFix analyzes MKV files sourced from DVDs and applies the least destructive treatment possible:

- progressive video is copied byte for byte;
- recoverable PAL 2:2 or NTSC 3:2 uses VFM field matching, with conditional QTGMC only for isolated residual frames;
- hybrid PAL uses VFM for the 25p body and QTGMC for confirmed 50i segments, producing progressive 50p;
- true interlaced video uses VapourSynth/QTGMC at 50p or 59.94p;
- hybrid NTSC and contradictory results stop for manual review rather than risk an incorrect cadence;
- the dark-mode GUI provides drag-and-drop, tooltips, reusable series profiles, detailed encoding progress and conservative opt-in restoration.

Original files are never overwritten. Outputs are written to a `_fixed` subfolder by default, created as `.partial.mkv`, fully validated, and only then renamed atomically.

## Download

Download the ready-to-run Windows package and its SHA-256 checksum from [GitHub Releases](https://github.com/Pessegos/dvd-fieldfix/releases/latest).

Requirements:

- Windows 10 or later;
- FFmpeg and FFprobe available on `PATH`;
- the portable VapourSynth/QTGMC runtime installed once by running `setup_qtgmc.ps1` from the extracted release folder.

Run `DVD-FieldFix.exe` for the GUI or `DVD-FieldFix-CLI.exe doctor` to verify the complete toolchain.

## Development setup

The source build requires Windows, Python 3.10+ and FFmpeg/FFprobe on `PATH`.

```powershell
python -m pip install -e ".[gui,dev]"
dvd-fieldfix doctor
```

Install VapourSynth R76 and QTGMC into an isolated portable Python 3.12 environment:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_qtgmc.ps1
dvd-fieldfix doctor
```

The setup script verifies the official bootstrap SHA-256 and stores the environment under `.runtime/vapoursynth-portable`. It does not modify the system Python installation.

## Usage

Open the GUI:

```powershell
dvd-fieldfix gui
```

Analyze a folder:

```powershell
dvd-fieldfix analyze "C:\videos" --report report.json
```

Process automatically using H.264:

```powershell
dvd-fieldfix process "C:\videos" --codec h264
```

More examples:

```powershell
dvd-fieldfix process episode.mkv --codec hevc10 --mode fieldmatch
dvd-fieldfix process episode.mkv --codec hevc10 --mode hybrid50
dvd-fieldfix process episode.mkv --codec ffv1 --mode qtgmc
dvd-fieldfix process episode.mkv --codec h264 --crf 14
dvd-fieldfix process episode.mkv --crop 8:0:8:0 --denoise light
dvd-fieldfix process episode.mkv --dotcrawl
dvd-fieldfix process episode.mkv --auto-crop
```

Auto-crop samples seven positions across the episode and removes only borders that every valid sample considers outside the active image. It is disabled by default. A manual `L:T:R:B` crop always takes priority.

## Series and video profiles

- `h264`: x264 CRF 14, `veryslow`, High profile, 8-bit `yuv420p`, extended detail search;
- `hevc10`: x265 CRF 14, `veryslow`, 10-bit `yuv420p10le`, RD refinement;
- `ffv1`: lossless FFV1 level 3.

Audio, subtitles, chapters, attachments, languages and dispositions are copied without re-encoding.

The GUI saves codec, CRF, crop, cleanup and parallel-job choices as a reusable JSON profile so one series remains consistent. `tune=animation` is not applied automatically. See [Encoding profiles and quality decisions](docs/ENCODING_PROFILES.md) for the complete settings, CRF versus two-pass/lossless guidance, CPU benchmarks and the reasons behind each conservative override.

The status line reports current/total frames, encoding FPS, elapsed time and ETA. Two parallel jobs can improve total HEVC throughput on SD material; one remains the memory-conservative default.

Dot-crawl/rainbow cleanup uses one optional spatial DotKillS pass after field reconstruction. It is off by default and should be previewed. Cadence-dependent temporal variants are not selected automatically.

## Temporal pipeline

Residual combing after field matching is measured in one-second windows. Windows with stable 50i activity are merged, padded by 0.5 seconds for safe temporal transitions, and recorded in the JSON report.

In `hybrid50` mode:

- VFM recovers 25p frames using the same reference field as QTGMC;
- `Interleave` duplicates each clean 25p frame exactly, without motion interpolation;
- QTGMC with conservative source matching replaces confirmed 50i ranges and any frame still marked as combed;
- the result is progressive CFR 50p with the source duration and display aspect ratio preserved.

Every file is analyzed in full. No episode list or source-specific decision is hard-coded.

## Safety, manifests and validation

Each completed MKV receives an adjacent `filename.mkv.dvd-fieldfix.json` manifest containing source/output SHA-256 hashes, the pipeline version, configuration, analysis and validation results.

Validation checks:

- full decoding without errors;
- duration within 100 ms;
- audio, subtitle and attachment stream counts;
- exact decoded frame count for 25p, 50p and decimated 23.976p paths;
- duplicated-frame cadence outside confirmed 50i sections of hybrid PAL sources;
- expected frame rate and progressive field flag;
- residual combing;
- SAR/DAR preservation.

A completed output is skipped only when its source hash, pipeline version, configuration and output hash all match. Incompatible collisions are blocked.

## Real-source test samples

DVD mastering varies substantially between releases. Short samples from unrelated PAL and NTSC discs help exercise field order, cadence changes, blended fields and authoring faults that synthetic fixtures cannot reproduce.

See [Real-source testing](docs/TEST_SAMPLES.md) for the most useful sample types, how to create compact excerpts, and what metadata to include. Copyrighted video samples are never committed to this repository.

## Building the executables

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

The build creates `DVD-FieldFix.exe` and `DVD-FieldFix-CLI.exe` under `dist/DVD-FieldFix`. Tagged GitHub builds package this folder as a ZIP and publish a matching SHA-256 file.
