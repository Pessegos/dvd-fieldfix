# Changelog

All notable changes to this project will be documented in this file. The project follows [Semantic Versioning](https://semver.org/).

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
