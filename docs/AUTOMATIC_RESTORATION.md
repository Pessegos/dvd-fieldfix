# Automatic restoration policy

DVD FieldFix must not treat every available filter as an improvement. Fine animation lines, intended grain, chroma texture and analogue defects can produce similar simple metrics. An automatic system that merely measures how many pixels a filter changes would confidently damage some clean sources.

## Current safety policy

- Interlace and cadence detection never imply denoising or composite-artifact cleanup.
- Auto mode preserves the source when restoration evidence is absent or uncertain.
- Light denoise and DotKillS remain expert overrides in a separate Advanced cleanup dialog and stay off by default.
- Every active override appears in the decision summary, series profile, output fingerprint and manifest.
- Automatic crop remains separate because stable black borders can be measured geometrically and validated without altering active image content.

## Requirements for automatic cleanup

The intended assessor will operate per DVD/series, not independently on every episode:

1. Reconstruct the correct temporal format first. Raw interlaced motion can look like dot crawl to a metric.
2. Sample multiple scene types across several episodes: dark/flat areas, motion, detailed line art, credits and scene changes.
3. Measure luma/chroma high-frequency correlation and temporal chroma oscillation. DotKill or FFmpeg `dedot` response is supporting evidence, never proof by itself.
4. Estimate noise only in masked low-gradient regions. A whole-frame bit-plane or high-frequency score cannot distinguish noise from legitimate detail.
5. Aggregate robust statistics across the series. A single outlier frame cannot enable a filter for every episode.
6. Produce three confidence outcomes:
   - high-confidence absent: preserve;
   - high-confidence present: choose the weakest effective level;
   - contradictory/intermediate: preserve and request review.
7. Generate representative before/after PNGs and expose the evidence in the decision summary.
8. Validate that cleanup reduces the targeted metric without materially reducing edge energy, changing cadence or altering clean control regions.

## Calibration data needed

Before high-confidence automatic filtering is enabled, the thresholds need short lossless excerpts from:

- clean PAL and NTSC animation;
- severe and mild dot crawl;
- rainbowing added before and after telecine;
- genuine film grain and random electronic noise;
- clean digital animation authored to DVD;
- dark scenes, saturated line art and scrolling credits;
- TFF, BFF, PAL 2:2, NTSC 3:2 and genuine interlaced motion.

Until these controls exist, “preserve” is a deliberate quality decision rather than a missing filter.
