# Advanced Crop Detection Script

`cropdetect.py` recommends a **safe** FFmpeg crop for 8-bit, 10-bit, and 12-bit SDR/HDR sources. Several different “this frame does not match the movie” problems are **not** the same:

| What you see | Why cropdetect lies | What this script does |
| :--- | :--- | :--- |
| Opening **logo** | Different AR for a few seconds at the start | Ignore the first 5% of the file |
| Scrolling **credits** | Huge black field, small text column → a **tighter** window than the movie. Applying that crop cuts too much off the picture. (Some credits instead use more of the 16:9 frame and would *block* a 2.39 crop.) | Ignore the last 5% of the file |
| **Too-dark scene** | cropdetect treats dark picture as extra bars → **too-small** frame | Skip samples whose normalized 8-bit **full-frame YAVG** is below 16; minor tighter dark-edge clusters contained inside the majority crop do not trigger mixed-AR warnings |
| **Dirt / reel flakes** | A bright speck in the letterbox makes cropdetect report a **larger** frame. One speck does not move the average luma of that bar | Measure normalized 8-bit YAVG of the strips the majority crop would throw away. If it stays near the majority-letterbox baseline, ignore the expansion. Also ignore ARs shorter than `--min-insert` (6s) |
| **Artistic AR change** (Fallout 4:3 flashback, IMAX, open matte) | Real picture in a different shape, for **seconds**. Those “bars” are full of picture, so bar YAVG **jumps** vs earlier/later letterboxed frames | Keep it. Outer box so those scenes are not cut. For 2.39 + 4:3 in 1920×1080 that box is usually the full frame (no crop) |

A 5% “majority of the whole movie” threshold would throw away a 10-second flashback in a 45-minute episode. Dirt is filtered by **bar luma** (speck vs picture) and **duration**, not by percent of the file. `-sct 5` is only a **Primary** label in the report.

## How it works

1. **Probe**: `ffprobe` reads duration, resolution, codec, pixel format, and stream index. Attached cover-art streams (`attached_pic`) are skipped so a poster is not mistaken for the movie.
2. **Fast parallel scan**:
   - **Keyframe mode (default for inter-frame files ≤ 20 GiB)**: splits the video into parallel chunks and runs FFmpeg with `-skip_frame nokey`, decoding **every keyframe** in one pass (typically **3–5 seconds** for a 2-hour movie). Because encoders place keyframes on scene cuts, short artistic inserts are caught without thousands of separate FFmpeg spawns.
   - **Seek mode (automatic for > 20 GiB remuxes / intra-only codecs, or when `--interval` is set)**: uses fast keyframe-seeking (`-noaccurate_seek`) across parallel workers, with an optional gap-fill pass when the grid spacing exceeds half of `--min-insert`.
   - `cropdetect` uses FFmpeg's normalized `24/255` limit (`limit=0.0941176:round=4:reset=1:skip=0`), so 8-bit, 10-bit (where TV black is Y=64), and 12-bit sources all detect bars accurately.
3. **Dark-scene luma gate**: Full-frame luma is measured via `signalstats` on a downscaled 8-bit full-range gray copy (`0` = black, `255` = white) with `metadata=mode=print`. Frames below `--luma-min` (default 16) are skipped.
4. **Cluster**: Similar crops are grouped by **position and size** (not just top-left), so 2.39 letterbox and 4:3 pillarbox stay distinct. Each cluster is represented by its modal crop (`center`) so a single noisy frame within the tolerance bucket cannot poison snapping.
5. **Opening / credits**: Clusters that exist **only** in the first/last 5% do not vote.
6. **Duration vs dirt**: A cluster in the body must cover at least `--min-insert` seconds (default 6s). Minor tighter clusters strictly contained inside the majority crop (dark edges on night scenes) are absorbed rather than triggering false mixed-AR warnings.
7. **Bar-strip luma (the flake vs flashback test)**: If another cluster would *grow* the frame beyond the majority crop, the script measures normalized 8-bit YAVG of the expanded bar strips and compares that to the majority-letterbox baseline. A speck barely moves YAVG; a 4:3 / IMAX insert fills those strips with picture and YAVG jumps by `--bar-delta` (default 12).
8. **Recommend**:
   - One sustained in-body ratio → snap to a known AR (if close) and print `-vf crop=w:h:x:y`.
   - Several sustained in-body ratios → mixed AR. Outer bounding box (never the tighter credit-like window).
   - Full-frame box, tiny crop (`--min_crop`), or nothing usable → do not crop.

Snapping only runs when the detection is clearly letterboxed (width ≈ full frame) or pillarboxed (height ≈ full frame). Dimensions round **up** to a multiple of 4; offsets stay even. If snapping would make the box smaller than the detected picture, the snap is cancelled (`Custom AR (Near …)`). Asymmetric bars keep their original offset.

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

- **Fast parallel keyframe scan:** Decodes every keyframe in parallel chunks (`-skip_frame nokey`) — a full 2-hour 10-bit movie finishes in a few seconds instead of spawning thousands of FFmpeg processes.
- **Bit-depth safe (8/10/12-bit SDR & HDR):** Uses normalized `cropdetect` limit (`24/255`) and normalized 8-bit gray luma so 10-bit limited-range black bars (Y=64) work identically to 8-bit.
- **Large-file / remux safe:** Automatically switches to fast `-noaccurate_seek` sampling for files > 20 GiB or intra-only codecs so huge UHD remuxes don't read tens of gigabytes sequentially.
- **Logos / credits skipped** at the first and last 5% (configurable).
- **Too-dark scenes skipped** so cropdetect cannot false-tighten (normalized full-frame YAVG, default 16).
- **Letterbox flakes ignored** when bar-strip luma stays near the majority baseline (a speck, not picture).
- **Brief dirt ignored** unless a different AR lasts `--min-insert` seconds (default 6).
- **Artistic inserts kept:** 4:3 flashback, IMAX, open matte → bar luma jumps, outer box, even at a small % of runtime.
- **`--min_crop`:** No recommendation unless at least one side is ≥ 10 px (default).
- **Cover-art safe:** Resolution and stream index come from the first real video stream.

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

### Options

- `-n, --num_workers`: Worker threads (default: half your CPU cores, minimum 1).
- `--scan <auto|keyframes|seek>`: Sampling strategy (default: `auto`). `keyframes` scans every keyframe in parallel chunks; `seek` uses fast keyframe-seeking (`-noaccurate_seek`); `auto` uses `keyframes` for inter-frame files ≤ 20 GiB and `seek` for larger or intra-only files.
- `--interval <seconds>`: Grid spacing for seek mode. Passing this switches to seek mode at that exact interval.
- `--min-insert <seconds>`: How long a different AR must last in the **body** before it can veto the majority crop (default: `6`). Complements the bar-luma flake test; not a percent of the movie.
- `--edge <frac>`: Ignore clusters that exist only in the first/last this fraction (default: `0.05` = logos and scrolling credits).
- `-sct, --significant_crop_threshold`: Percentage for the **Primary** label in a mixed-AR report (default: `5.0`). Does **not** decide the crop.
- `-mc, --min_crop`: Do not recommend a crop unless some side is at least this many pixels (default: `10`).
- `--luma-min <YAVG>`: Skip frames with normalized 8-bit full-frame YAVG darker than this (default: `16`).
- `--bar-delta <YAVG>`: How much brighter (0–255) the discarded strips must be vs the majority letterbox to count as picture (default: `12`).
- `--no-verify`: Skip the gap-fill pass when running in seek mode.
- `--debug`: Sample counts and cluster centers.

## Example Output

### Confident crop recommendation

Single dominant AR in the body (for example 2.39 Scope on a 10-bit movie). Brief detections and the credit roll were ignored:

```
A single dominant aspect ratio was found in the body of the video.
The detected crop snaps to the 'Widescreen (Scope)' aspect ratio.

Recommended crop filter: -vf crop=1920:804:0:138
```

### Mixed aspect ratios (the Fallout case)

2.39 for most of the episode, a ~10s 4:3 flashback in the body. That is longer than `--min-insert`, so it is not dirt, and bar-strip luma confirms picture in the bars. The outer box is the full 16:9 frame:

```
Mixed aspect ratios in the body of the video.
The outer box around every in-body ratio is the full frame, so a single crop would cut an artistic insert (4:3 flashback, IMAX, open matte).

--- Detected Aspect Ratios (body) ---
  - 'Widescreen (Scope)' (crop=1920:804:0:138) in 96.4% of samples, 0:12–47:02 (412 body hits, Primary)
  - 'Fullscreen (4:3)' (crop=1440:1080:240:0) in 1.1% of samples, 18:32–18:41 (5 body hits, Minority / artistic AR)

The safe crop matches the source resolution. No crop is needed.
```

If both ratios still share a smaller outer box (for example Scope + IMAX 1.90), you get a **safe** crop of that box, not the tighter majority ratio.

## Integration with Other Scripts

[`svt_opus_encoder.py`](../svt_opus_encoder.py) and [`aom_opus_encoder.py`](../aom_opus_encoder.py) embed this same crop detection logic behind `--autocrop` so each script stays self-contained. [`xav_automation.py`](../xav_automation.py) uses xav's native autocrop and does not call this tool.

[`cropdetect_old.py`](cropdetect_old.py) is a legacy version kept in this folder only as a reference.
