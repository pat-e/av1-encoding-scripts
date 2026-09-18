# Advanced Crop Detection Script

`cropdetect.py` recommends a **safe** FFmpeg crop. Several different “this frame does not match the movie” problems are **not** the same:

| What you see | Why cropdetect lies | What this script does |
| :--- | :--- | :--- |
| Opening **logo** | Different AR for a few seconds at the start | Ignore the first 5% of the file |
| Scrolling **credits** | Huge black field, small text column → a **tighter** window than the movie. Applying that crop cuts too much off the picture. (Some credits instead use more of the 16:9 frame and would *block* a 2.39 crop.) | Ignore the last 5% of the file |
| **Too-dark scene** | cropdetect treats dark picture as extra bars → **too-small** frame | Skip samples whose **full-frame YAVG** is below 16 (cropdetect’s black limit is 24) |
| **Dirt / reel flakes** | A bright speck in the letterbox makes cropdetect report a **larger** frame. One speck does not move the average luma of that bar | Measure YAVG of the strips the majority crop would throw away. If it stays near the majority-letterbox baseline, ignore the expansion. Also ignore ARs shorter than `--min-insert` (6s) |
| **Artistic AR change** (Fallout 4:3 flashback, IMAX, open matte) | Real picture in a different shape, for **seconds**. Those “bars” are full of picture, so bar YAVG **jumps** vs earlier/later letterboxed frames | Keep it. Outer box so those scenes are not cut. For 2.39 + 4:3 in 1920×1080 that box is usually the full frame (no crop) |

A 5% “majority of the whole movie” threshold would throw away a 10-second flashback in a 45-minute episode. Dirt is filtered by **bar luma** (speck vs picture) and **duration**, not by percent of the file. `-sct 5` is only a **Primary** label in the report.

## How it works

1. **Probe**: `ffprobe` reads duration and resolution. Attached cover-art streams (`attached_pic`) are skipped so a poster is not mistaken for the movie.
2. **Pass 1 (grid)**: Every **2 seconds** (configurable), seek and decode **2 frames** with `cropdetect=24:4:0` plus `signalstats` (full-frame luma).
3. **Dark-scene luma gate**: Frames with full-frame YAVG below 16 are skipped. cropdetect would see extra bars and vote for a crop that is too tight.
4. **Cluster**: Similar crops are grouped by **position and size** (not just top-left), so 2.39 letterbox and 4:3 pillarbox stay distinct.
5. **Opening / credits**: Clusters that exist **only** in the first/last 5% do not vote. Scrolling credits would otherwise report a much smaller frame; logos are often a different AR.
6. **Duration vs dirt**: A cluster in the body needs enough samples to cover `--min-insert` seconds (default 6s → 3 hits at a 2s grid). Shorter = flake and **cannot** veto the majority crop.
7. **Bar-strip luma (the flake vs flashback test)**: If another cluster would *grow* the frame, the script measures YAVG of the pixels the majority crop would discard, and compares that to the same strips on typical majority frames (earlier/later letterbox). A speck barely moves YAVG; a 4:3 insert fills those strips with picture and YAVG jumps by `--bar-delta` (default 12).
8. **Pass 2 (gap fill)**: If Pass 1 would actually crop, a second grid runs at **+1s offset** so a 6–10s insert cannot hide between two Scope samples.
9. **Recommend**:
   - One sustained in-body ratio → snap to a known AR (if close) and print `-vf crop=w:h:x:y`.
   - Several sustained in-body ratios → mixed AR. Outer bounding box (never the tighter credit-like window).
   - Full-frame box, tiny crop (`--min_crop`), or nothing usable → do not crop.

The outer box already will not *shrink* because of tighter credit detections. The end-edge skip is still required so credits that fill *more* of the frame (16:9 text on a 2.39 movie) cannot expand the box and cancel a correct Scope crop.

Snapping only runs when the detection is clearly letterboxed (width ≈ full frame) or pillarboxed (height ≈ full frame). Dimensions round **up** to a multiple of 4; offsets stay even. If snapping would make the box smaller than the detected picture, the snap is cancelled (`Custom AR (Near …)`). Asymmetric bars keep their original offset. The known-AR table is never mutated.

### Known aspect ratios (3% relative tolerance)

| Name | Ratio |
| :--- | :--- |
| HDTV | 16:9 |
| Widescreen (Scope) | 2.39 |
| Widescreen (CinemaScope) | 2.35 |
| Widescreen (Flat) | 1.85 |
| IMAX Digital | 1.90:1 |
| Fullscreen | 4:3 |
| IMAX 70mm | 1.43:1 |

## Key Features

- **Dense parallel sampling:** 2s grid, 2 frames per seek, process pool (default: half the CPU cores).
- **Gap-fill pass:** Extra samples between grid points whenever a crop would be applied.
- **Logos / credits skipped** at the first and last 5% (configurable).
- **Too-dark scenes skipped** so cropdetect cannot false-tighten (full-frame YAVG, default 16).
- **Letterbox flakes ignored** when bar-strip luma stays near the majority baseline (a speck, not picture).
- **Brief dirt ignored** unless a different AR lasts `--min-insert` seconds (default 6).
- **Artistic inserts kept:** 4:3 flashback, IMAX, open matte → bar luma jumps, outer box, even at a small % of runtime.
- **`--min_crop`:** No recommendation unless at least one side is ≥ 10 px (default).
- **Cover-art safe:** Resolution comes from the first real video stream.
- **Color-coded report:** Green for a recommendation or “no crop”, yellow for mixed AR (with timestamps), plus lists of ignored credits and ignored noise.

## Prerequisites

- **Python 3**
- **FFmpeg**: Both `ffmpeg` and `ffprobe` must be installed and in your system's `PATH`.

## Installation

The script starts with `#!/usr/bin/env python3`. Copy it into your `bin` folder (or run it from this directory) and invoke it by name.

## Usage

```bash
cropdetect.py "path/to/your/video.mkv"
```

From this folder, same thing:

```bash
./cropdetect.py "path/to/your/video.mkv"
```

A 45-minute episode at the default 2s interval is on the order of ~1,300 seeks per pass (Pass 2 only runs if a crop would be applied). Use more workers if you have the cores.

### Options

- `-n, --num_workers`: Worker processes (default: half your CPU cores, minimum 1).
- `--interval <seconds>`: Grid spacing (default: `2`). Use `1` if you suspect a very short insert.
- `--min-insert <seconds>`: How long a different AR must last in the **body** before it can veto the majority crop (default: `6`). Complements the bar-luma flake test; not a percent of the movie.
- `--edge <frac>`: Ignore clusters that exist only in the first/last this fraction (default: `0.05` = logos and scrolling credits).
- `-sct, --significant_crop_threshold`: Percentage for the **Primary** label in a mixed-AR report (default: `5.0`). Does **not** decide the crop.
- `-mc, --min_crop`: Do not recommend a crop unless some side is at least this many pixels (default: `10`).
- `--luma-min <YAVG>`: Skip frames darker than this (default: `16`). Too-dark scenes make cropdetect report extra bars.
- `--bar-delta <YAVG>`: How much brighter the discarded strips must be vs the majority letterbox to count as picture (default: `12`).
- `--no-verify`: Skip the gap-fill pass.
- `--debug`: Sample counts and cluster centers.

## Example Output

### Confident crop recommendation

Single dominant AR in the body (for example 2.39 Scope on a 4K source). Brief flakes and the credit roll were ignored:

```
Ignored opening logos / end credits (first/last edge of the file):
  - 'HDTV (16:9)' (crop=1920:1080:0:0) only at 47:10–48:02
  (Scrolling credits are mostly black with a small text column; using them would crop too tightly. Logos are often a different AR.)
Ignored dirt/noise (fewer than 3 body samples, under --min-insert 6.0s; will not block a majority crop):
  - Custom AR (crop=1920:1080:0:0) at 12:04–12:04 (1 body hits)

A single dominant aspect ratio was found in the body of the video.
The detected crop snaps to the 'Widescreen (Scope)' aspect ratio.

Recommended crop filter: -vf crop=1920:804:0:138
```

### Mixed aspect ratios (the Fallout case)

2.39 for most of the episode, a ~10s 4:3 flashback in the body. That is longer than `--min-insert`, so it is not dirt. The outer box is the full 16:9 frame:

```
Mixed aspect ratios in the body of the video.
The outer box around every in-body ratio is the full frame, so a single crop would cut an artistic insert (4:3 flashback, IMAX, open matte).

--- Detected Aspect Ratios (body) ---
  - 'Widescreen (Scope)' (crop=1920:804:0:138) in 96.4% of samples, 0:12–47:02 (412 body hits, Primary)
  - 'Fullscreen (4:3)' (crop=1440:1080:240:0) in 1.1% of samples, 18:32–18:41 (5 body hits, Minority / artistic AR)

The safe crop matches the source resolution. No crop is needed.
```

If both ratios still share a smaller outer box (for example Scope + IMAX 1.90), you get a **safe** crop of that box, not the tighter majority ratio.

### No crop needed

```
Analysis complete. No black bars detected.
```

or:

```
Detected crop is under the minimum (10px per side). No crop is needed.
```

or:

```
No safe crop clusters in the body of the video.
Recommendation: Do not crop.
```

## Integration with Other Scripts

[`svt_opus_encoder.py`](../svt_opus_encoder.py) and [`aom_opus_encoder.py`](../aom_opus_encoder.py) still contain an **older copy** of this logic behind `--autocrop`. They have not been switched to this standalone script yet. [`xav_automation.py`](../xav_automation.py) uses xav's native autocrop and does not call this tool.

## Notes

- **Largest box** is correct for a real second AR, and wrong for a flake. Bar-strip luma (speck vs picture) plus `--min-insert` (seconds vs 1–2 frames) are the two lines between those.
- **Tighter** detections (credit text in a black field) never shrink an outer-box crop. They *are* ignored at the end so they cannot become the only cluster you trust, and so 16:9 credit cards cannot expand a 2.39 movie to “no crop”.
- A 4:3 insert inside a 16:9 container together with 2.39 letterbox almost always unions to **no crop**. That is correct.
- `--min-insert 4` or `--interval 1` if a real insert is shorter than ~6 seconds.
- `--debug` prints how many samples were usable and how they clustered.
