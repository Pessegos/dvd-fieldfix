# Encoding profiles and quality decisions

DVD FieldFix treats the queue as one series project. Codec, CRF, crop and optional restoration settings are global, while the temporal decision remains automatic or individually overridable per episode. The GUI can save these choices as a small JSON series profile and load them for later discs.

## Rate control

CRF is the default for H.264 and HEVC because it targets broadly consistent perceptual quality and lets bitrate follow source complexity. The default is deliberately quality-first: **CRF 14**.

- Lower CRF increases quality and file size.
- Two-pass ABR is useful when a fixed file size or bitrate is mandatory; it is not inherently a higher-quality replacement for unconstrained CRF.
- Constant QP is less adaptive across scenes and is not used.
- FFV1 is the actual lossless choice when encoder loss is unacceptable, at a much larger storage cost.

CRF scales are encoder-specific. The same number is not a mathematical promise that x264 and x265 have identical quality. A series profile should be confirmed with representative dark, high-motion, grainy and line-art scenes before processing every episode.

## H.264 quality profile

The H.264 profile uses:

```text
libx264 / CRF 14 / preset veryslow / High / yuv420p
subme=11 / merange=32 / fast-pskip=0 / dct-decimate=0
```

The four explicit overrides spend additional time on subpixel analysis and avoid two early coefficient/skip shortcuts that can discard faint DVD detail. Other decisions—including references, B-frames, trellis, CABAC, lookahead and motion estimation—remain under x264's maintained `veryslow` preset.

DVD FieldFix deliberately does **not** copy release-group settings blindly. Values such as CRF 6, `mbtree=0`, strong negative deblock offsets or a particular AQ strength may suit one restoration and be wasteful or harmful on another. They should only become a series override after visual A/B tests.

No `tune=animation` is enabled. A tune changes several interacting encoder decisions; the neutral profile is safer for mixed animation, live-action inserts, grain, credits and analogue artifacts.

## HEVC 10-bit quality profile

The HEVC profile uses:

```text
libx265 / CRF 14 / preset veryslow / Main 10 / yuv420p10le / rd-refine=1
```

`rd-refine=1` performs an extra rate-distortion refinement at the RD level used by `veryslow`. WPP, frame threads and the x265 worker pool remain automatic. x265 already creates a worker for each detected logical processor, but SD frames contain few CTU rows and cannot always keep every worker busy.

For that reason, the GUI offers one or two parallel episode jobs. On the development Ryzen 7 7800X3D, two short QTGMC/HEVC jobs took 23.12 seconds together versus 38.41 seconds sequentially. This improves queue throughput without enabling x265's experimental `threaded-me`, which its documentation warns can reduce compression efficiency and is intended for much larger low-frequency CPUs.

VSPipe's automatic request count is retained. In a 600-frame QTGMC benchmark it produced 50.98 fps; manually limiting requests to 2, 4, 8 or 12 produced 14.58, 26.79, 42.92 and 49.57 fps respectively.

## Composite-video cleanup

The optional **Dot crawl / rainbow cleanup** setting applies one spatial `DotKillS` iteration after field reconstruction. It is disabled by default and included in previews and output fingerprints.

This is intentionally not the more aggressive temporal `DotKillT` or cadence-dependent `DotKillZ`. Those modes are NTSC-specific, require the correct pulldown offset, and can produce severe artifacts when their assumptions are wrong. Automatic composite-artifact detection needs representative real DVDs before it can be trusted.

Progressive sources are normally copied byte for byte. If crop, denoise or DotKill cleanup is selected, auto mode switches them to the explicit `restore` path so that the requested setting is never silently ignored.

## References

- [FFmpeg libx264/libx265 wrapper documentation](https://ffmpeg.org/ffmpeg-codecs.html#libx264_002c-libx264rgb)
- [x265 presets and tunes](https://x265.readthedocs.io/en/master/presets.html)
- [x265 threading](https://x265.readthedocs.io/en/master/threading.html)
- [x265 command-line options](https://x265.readthedocs.io/en/master/cli.html)
- [VSPipe output and concurrent requests](https://www.vapoursynth.com/doc/output.html)
- [DotKill source and documentation](https://github.com/myrsloik/DotKill)
