# Real-source testing

Synthetic fixtures cover known cadence and field-order cases, but real DVDs expose authoring faults and mixed mastering that generators rarely reproduce. Short lossless excerpts from unrelated discs are therefore particularly useful.

## Highest-priority samples

1. NTSC 29.97i with a stable 3:2 film cadence.
2. NTSC hybrid material that switches between 23.976p film and genuine 59.94-field video.
3. PAL 25i with genuine 50-field motion, such as studio credits, scrolling captions or live-action camera movement.
4. Bottom-field-first PAL or NTSC video.
5. A source whose field order changes inside one title.
6. Animation with blended fields, duplicate frames, cadence breaks or edits made after telecine.
7. Noisy, grainy or dark material that can challenge comb detection.

Clean progressive DVD material and ordinary PAL 2:2 are still useful as negative and regression cases, but are less urgent.

## Creating an excerpt

Remux a short section without re-encoding so that the original fields and timestamps remain intact:

```powershell
ffmpeg -ss 00:10:00 -i episode.mkv -t 00:00:30 -map 0 -c copy sample.mkv
```

Use 20–60 seconds around the problem. If a transition is important, begin several seconds before it and end several seconds after it. Include the following in a text file:

- PAL or NTSC, if known;
- the expected visual behavior or suspicious timestamp;
- whether the source is TFF or BFF, if known;
- the disc region and release year, when available;
- the output of `dvd-fieldfix analyze sample.mkv --report sample.json`.

Do not process the sample before sharing it. Avoid HandBrake, filtering, frame-rate conversion or a lossy intermediate.

## Privacy and copyright

Do not open a public pull request containing copyrighted programme footage. Share only the shortest excerpt needed for private testing and only when you are entitled to do so. Reports and fully synthetic fixtures may be committed publicly.
