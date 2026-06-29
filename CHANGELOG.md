# Changelog

All notable changes to this project will be documented in this file. The project follows [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-06-29

Temporal-fidelity release.

### Changed

- Replaced the remaining Yadif fallback after VFM with conditional QTGMC source matching for both PAL 2:2 and NTSC 3:2 sources.
- Made field-matched previews use the exact VapourSynth graph used by production encoding.
- Extended `doctor` to execute the conditional field-match, hybrid 50p and NTSC decimation graphs instead of only loading their plugins.
- Bumped the processing pipeline version so outputs made by the earlier temporal graph are never mistaken for current results.

### Added

- Full decoded-frame-count validation for 25p, hybrid/true-interlaced 50p and NTSC 23.976p outputs.
- Hybrid cadence validation that confirms clean 25p sections remain exact duplicated pairs at 50p while true 50i sections retain their temporal resolution.
- Real-source sample guide covering the DVD variants most useful for further validation.
- End-to-end PAL TFF and BFF true-interlace fixtures, plus real-source PAL field-match and hybrid-path verification.

## [0.1.0] - 2026-06-29

First public release.

### Added

- English dark-mode Windows GUI with drag-and-drop, queue management, progress, cancellation and preview.
- CLI with `doctor`, `analyze`, `process`, and automatic or manual modes.
- Full-file detection of progressive, PAL 2:2, NTSC 3:2, hybrid and true interlaced material.
- `hybrid50` pipeline: VFM for 25p and QTGMC only for confirmed PAL 50i segments.
- QTGMC with conservative source matching, TFF/BFF support and 50p/59.94p output.
- H.264, 10-bit HEVC and FFV1 profiles, with audio, subtitles, attachments and chapters copied.
- Conservative opt-in auto-crop, disabled by default.
- Atomic outputs, SHA-256 verification, versioned manifests and full FPS, DAR/SAR and combing validation.
- Portable VapourSynth R76 + vs-jetpack environment and PyInstaller executables.

[0.1.0]: https://github.com/Pessegos/dvd-fieldfix/releases/tag/v0.1.0
[0.2.0]: https://github.com/Pessegos/dvd-fieldfix/releases/tag/v0.2.0
