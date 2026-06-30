# Changelog

All notable changes to this project will be documented in this file. The project follows [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-06-30

Decision-transparency and preview-export release.

### Added

- Extensive series/per-file decision summary with evidence, rationale, encoder arguments, stream policy, hashes and output validation; summaries can be copied or saved as text.
- Lossless PNG export for the original preview frame, corrected preview frame or both.
- A documented conservative automatic-restoration policy and calibration requirements for future per-DVD/series dot-crawl and noise assessment.

### Changed

- Renamed the main action to `Analyze + Process` and clarified that a separate analysis pass is optional.
- Hide the CRF label and input completely for lossless FFV1, then restore them when H.264 or HEVC is selected.
- Added a dedicated dark-theme `TSpinbox` style so the CRF value remains readable.
- Moved denoise and DotKill checkboxes out of the primary profile into an explicitly warned Advanced cleanup dialog.
- Kept preservation as the automatic result while restoration evidence is uncalibrated or contradictory.

## [0.3.0] - 2026-06-29

Series-quality and restoration release.

### Added

- Reusable JSON series profiles for consistent codec, CRF, crop, cleanup and parallel-job choices.
- Hover descriptions for the main GUI controls and a clearer `Check setup` button in place of `Doctor`.
- Detailed current/total frame, FPS, elapsed-time and ETA reporting in both the GUI and CLI.
- One- or two-episode GUI processing; two HEVC/QTGMC jobs improved measured queue time by about 40% on the development 8-core CPU.
- Conservative opt-in DotKillS cleanup for dot crawl and rainbow artifacts, included in previews, dependency checks and output fingerprints.
- A `restore` path so requested restoration is applied to progressive sources instead of being silently ignored.
- An About dialog documenting that the current feather is Tcl/Tk's built-in default icon.

### Changed

- Set the quality-first H.264 and HEVC default to editable CRF 14 while keeping `veryslow` and no automatic animation tune.
- Added x264 detail-oriented `subme=11`, 32-pixel motion range, disabled fast P-skip and disabled DCT decimation.
- Added x265 RD refinement while preserving its automatic WPP/thread-pool decisions.
- Kept VSPipe's automatic concurrent-request scheduling after benchmarks showed every tested manual limit was slower.
- Extended the portable runtime with the pinned `vapoursynth-dotkill` 3.0 plugin.
- Bumped the processing pipeline version for the new encoder and restoration graphs.

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
[0.3.0]: https://github.com/Pessegos/dvd-fieldfix/releases/tag/v0.3.0
[0.4.0]: https://github.com/Pessegos/dvd-fieldfix/releases/tag/v0.4.0
