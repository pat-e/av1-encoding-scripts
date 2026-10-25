# Tools

Two standalone helpers. They use the same folder workflow as the encoders: `cd` into a directory of `.mkv` files and run the script. One file at a time. Results land in `completed/`, untouched inputs in `original/`, failures in `failed/`, and one log per file in `conv_logs/`.

| Script | When to run it |
| :--- | :--- |
| [`mdegrain.py`](mdegrain.py) | **Before** [`xav_automation.py`](../xav_automation.py). Denoise, and make the file something xav can take. |
| [`av1grain.py`](av1grain.py) | **After** an AV1 encode. Add film grain to finished AV1 MKVs. The video frames are not re-encoded. |

Both start with `#!/usr/bin/env python3`. Copy them (or a symlink) onto your `PATH`, the same way as [`cropdetect.py`](../cropdetect/cropdetect.py). `--help` prints the full text and exits. `--check` reports what is missing and does not touch any MKV.

## `mdegrain.py`

Denoise every MKV in the current folder with MDegrain2 (mvtools). The finished file is CFR `yuv420p` or `yuv420p10le`, which is what `xav_automation.py` accepts. Put `completed/` on the xav queue afterwards.

For each `*.mkv`:

1. If packets are not CFR, or MediaInfo says VFR, convert to CFR first. HandBrake `--cfr --rate` uses the same FPS choice as `xav_automation.py`. ffmpeg `-fps_mode cfr` is the fallback. The GOP stays a normal GOP. xav re-encodes this file later, so all-intra is skipped.
2. MDegrain2, two frames each way. `--thsad` is both luma and chroma strength (default 350). Block size stays 16.
3. Encode to the pixel format xav accepts. See the table below.
4. mkvmerge: the new video first, then audio, subtitles, chapters, and attachments from the source.
5. Check frame count, duration, pixel format, CFR, tracks, and chapters.

| Source | Encode |
| :--- | :--- |
| 1080p SDR, already 8-bit 4:2:0 | libx264 CRF 0 `yuv420p` |
| Other 1080p SDR (Hi10p, 4:2:2, 4:4:4, RGB, 12-bit and up) | libx264 10-bit CRF 0 `yuv420p10le` |
| Above 1080p, or HDR (PQ, HLG, Dolby Vision) | libx265 10-bit CRF 0 `yuv420p10le` |

libx265 CRF 0 is the intermediate xav accepts. It is not a bit-exact copy. A 10-bit BT.709 source at 1080p stays on the x264 10-bit path. If this ffmpeg's libx264 has no `yuv420p10le`, that 1080p 10-bit path uses libx265 CRF 0 instead. `--check` says which one you have.

HDR10 static metadata (primaries, PQ/HLG transfer, mastering display, MaxCLL, MaxFALL) is copied onto the new video, including into the encoder VUI, so a later xav video-only remux still carries it. Dolby Vision RPU and HDR10+ dynamic metadata are not kept. The log says so when the source has them.

### CPU

Same reservation as `xav_automation.py`. The script counts logical CPUs and leaves the last `--reserve-threads` free (default 2). A 32-thread CPU runs on CPUs 0–29. The mask is set on this process, so the VapourSynth threads stay inside it. ffmpeg, HandBrake, and mkvmerge inherit the mask. On Windows 11 and Windows Server 2022 the mask covers one processor group (at most 64 logical CPUs). Further CPUs stay free. `--reserve-threads 0` uses every pinnable CPU. If the reserve is larger than the CPU count, the script uses all of them.

### Prerequisites

Required on `PATH`: `ffmpeg`, `ffprobe`, `mkvmerge`, `mediainfo`.

`HandBrakeCLI` is optional. Without it, VFR and a failed CFR probe use the ffmpeg fallback.

Python package `vapoursynth`, plus three plugins:

| Namespace the script calls | Package |
| :--- | :--- |
| `ffms2` | `ffms2` |
| `mv` | `vapoursynth-plugin-mvtools` |
| `zsmooth` | `vapoursynth-plugin-zsmooth-bin` |

On Arch, `ffms2` and `vapoursynth-plugin-mvtools` are repo packages. `vapoursynth-plugin-zsmooth-bin` is AUR. `ffms2` is installed under `/usr/lib/vapoursynth`, which VapourSynth does not scan unless this is set:

```bash
echo 'export VAPOURSYNTH_EXTRA_PLUGIN_PATH=/usr/lib/vapoursynth' | sudo tee /etc/profile.d/vapoursynth-plugins.sh
```

Log out and back in, or reboot, so new sessions inherit it. If the file is already there but this terminal does not have the variable, `source /etc/profile.d/vapoursynth-plugins.sh`. `--check` tells these cases apart from a package that is actually missing. mvtools and zsmooth load from the Python plugin directory without that variable.

### Usage

```bash
mdegrain.py --check
mdegrain.py
mdegrain.py --thsad 150
mdegrain.py --thsad 500
mdegrain.py --reserve-threads 4
```

`--thsad` guide: 150 is light (keeps more grain and detail), 350 is the default, 500 and up is strong.

## `av1grain.py`

Add AV1 film grain synthesis to every MKV in the current folder with [grav1synth](https://github.com/rust-av/grav1synth). Grain is only signalled in the AV1 headers. The decoder or player draws it. Video frames, audio, subtitles, chapters, and names stay as they were. The input must already be AV1 (a file from `completed/` after xav, SVT, or aom). A non-AV1 file goes to `failed/`.

grav1synth writes old 3-letter language tags. The script puts the source IETF tags (`en`, `de`, …) back with `mkvpropedit`. It then checks that the frame count, the track list, and the chapter count match the input.

A file that already has grain goes to `failed/` unless you pass `--replace`.

There is no CPU pin. Applying grain does not re-encode.

### Strength

Default is `--iso 1200`, the same grain as Av1an `--photon-noise 12`. Av1an stores ISO as strength × 100, luma only, unless you also pass `--chroma`.

| Av1an | this script | Typical use |
| :--- | :--- | :--- |
| `--photon-noise 4` | `--iso 400` | Light. Clean modern shows, anime |
| `--photon-noise 8` | `--iso 800` | |
| `--photon-noise 12` | `--iso 1200` | Default. Ordinary TV |
| `--photon-noise 16` | `--iso 1600` | Grainier, film-based shows |
| `--photon-noise 24` | `--iso 2400` | Heavy, old film |

Recommended ISO range is 100–6400. `--chroma` adds grain on the colour planes as well (Av1an's `--chroma-noise`). It only works with `--iso`.

### Presets

Use either `--iso` or `--preset`, not both. Built-in stock names: `Super8`, `MaxMid`, `16mm`, `Classic35`, `Modern35`.

For `16mm`, `Classic35`, and `Modern35` the suffix picks the stock:

| Suffix | Stock |
| :--- | :--- |
| *(none)* | Fujifilm Eterna 250D |
| `-1` | Fujifilm Eterna 500T |
| `-2` | Kodak Vision3 250D |
| `-3` | Kodak Vision3 200T |

`--preset 16mm` is Fujifilm Eterna 250D. `--preset Classic35-2` is Super 35mm with Kodak Vision3 250D.

### Prerequisites

Required on `PATH`: `grav1synth`, `mkvmerge`, `mkvpropedit`, `ffprobe`.

```bash
cargo install grav1synth
```

Put the binary on `PATH` (for example `~/bin`). If `--check` cannot find it, the script prints <https://github.com/rust-av/grav1synth> and that install line.

### Usage

```bash
av1grain.py --check
av1grain.py
av1grain.py --iso 400
av1grain.py --iso 800 --chroma
av1grain.py --preset 16mm
av1grain.py --iso 400 --replace
```

## Suggested order

```text
noisy source  →  mdegrain.py  →  completed/  →  xav_automation.py  →  completed/  →  av1grain.py
```

Skip `mdegrain.py` when the source is already clean. Skip `av1grain.py` when you want the encode left grain-free. Do not point `av1grain.py` at an `mdegrain.py` result: that file is still H.264 or H.265.
