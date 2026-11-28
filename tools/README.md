# Tools

Three standalone helpers. `mdegrain.py` and `av1grain.py` use the same folder workflow as the encoders: `cd` into a directory of `.mkv` files and run the script (one file at a time; results land in `completed/`, untouched inputs in `original/`, failures in `failed/`, and one log per file in `conv_logs/`). `nodownmix-apply.py` renames `.mkv` files in place with no external dependencies.

| Script | When to run it |
| :--- | :--- |
| [`nodownmix-apply.py`](nodownmix-apply.py) | **Before** encoding to tag files with `.nodownmix.mkv` (per-file `--no-downmix`), or **after** with `--remove` to strip the tag. |
| [`mdegrain.py`](mdegrain.py) | **Before** [`xav_automation.py`](../xav_automation.py). Denoise, and make the file something xav can take. |
| [`av1grain.py`](av1grain.py) | **After** an AV1 encode. Add film grain to finished AV1 MKVs. The video frames are not re-encoded. |

All three start with `#!/usr/bin/env python3`. Copy them (or a symlink) onto your `PATH`, the same way as [`cropdetect.py`](../cropdetect/cropdetect.py). `--help` prints the full text and exits. `mdegrain.py` and `av1grain.py` also support `--check` to report missing tools without touching any MKV.

## `mdegrain.py`

Denoise every MKV in the current folder with 16-bit MDegrain2 (mvtools) and error-diffusion dither to CFR `yuv420p10le`, which is what `xav_automation.py` and `xav` encode. Put `completed/` on the xav queue afterwards.

For each `*.mkv`:

1. If packets are not CFR, or MediaInfo says VFR, convert to CFR in-memory via `ffms2` (`fpsnum`/`fpsden`, avoiding an extra temp file and generation loss). HandBrake `--cfr --rate` and ffmpeg `-fps_mode cfr` are fallbacks when timestamps are negative or FPS cannot be parsed.
2. Promote to 16-bit 4:2:0 (`Spline36` for RGB/GRAY/4:2:2/4:4:4; lossless bit-shift for 8/10/12-bit 4:2:0), run resolution-adaptive MDegrain2 (`pel=2, blksize=8/4` for SD ≤576p; `pel=2, blksize=16/8` for 720p/1080p; `pel=1, blksize=32/16` for >1080p; `rfilter=4`, `truemotion=0`, plus a 2-pass `RemoveGrain(mode=12)` search prefilter for `thsad >= 350` or >1080p), then error-diffusion dither to `yuv420p10le`. Output is always 10-bit `yuv420p10le` (never 12-bit/16-bit/4:2:2/4:4:4/RGB). Default luma strength is `thsad` 250 (`--low` is 150, `--high` is 350, or `--thsad N`); chroma strength `thsadc` is half of `thsad`.
3. Encode to visually transparent `yuv420p10le` (normal GOP, no `-tune fastdecode` bloat). See the table below.
4. mkvmerge: the new video first, then audio, subtitles, chapters, and attachments from the source.
5. Check frame count, duration, pixel format, CFR, tracks, and chapters.

| Source | Encode |
| :--- | :--- |
| ≤1080p SDR (any input depth / chroma / RGB) | libx264 10-bit CRF 0 `veryfast` `yuv420p10le` |
| Above 1080p, or HDR (PQ, HLG, Dolby Vision) | libx265 10-bit CRF 0 `superfast` `yuv420p10le` |

Both 10-bit libx264 CRF 0 (QP 12) and libx265 CRF 0 are visually transparent near-lossless (~57–61 dB PSNR) rather than bit-exact, avoiding 2.5–3.5× lossless filesize bloat while staying clean for `xav`. A 10-bit BT.709 source at 1080p stays on the fast x264 10-bit path. If this ffmpeg's libx264 has no `yuv420p10le`, ≤1080p SDR falls back to libx265 CRF 0. `--check` says which one you have.

HDR10 static metadata (primaries, PQ/HLG transfer, chroma siting, mastering display, MaxCLL, MaxFALL) is copied onto the new video, including into the encoder VUI, so a later xav video-only remux still carries it. Dolby Vision RPU and HDR10+ dynamic metadata are not kept. The log says so when the source has them.

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
mdegrain.py --low
mdegrain.py --high
mdegrain.py --thsad 200
mdegrain.py --reserve-threads 4
```

`--reserve-threads` defaults to 2. `--thsad` guide: 150 is light (`--low`, keeps more grain and detail), 250 is the default, 350 is strong (`--high`), and custom values use `--thsad`. Chroma strength (`thsadc`) is automatically half of `thsad`. Use only one of `--low`, `--high`, and `--thsad`. Combining them prints a warning and exits. No file is encoded or moved.

## `av1grain.py`

Add AV1 film grain synthesis to every MKV in the current folder with [grav1synth](https://github.com/rust-av/grav1synth). Grain is only signalled in the AV1 headers. The decoder or player draws it. Video frames, audio, subtitles, attachments (fonts, cover art), chapters, and names stay as they were. Only AV1 MKVs are processed: non-AV1 files are moved to `skipped/` (which is only created when a file is actually skipped) with a short log entry in `conv_logs/` explaining why.

To avoid libavformat/grav1synth dropping attachments, turning cover art into a second video track, or stripping IETF language tags, the script extracts the AV1 video track into a video-only intermediate with `mkvmerge`, runs `grav1synth apply` on that intermediate, and remuxes the grained video back with all original audio, subtitles, attachments, chapters, tags, and video metadata via `mkvmerge`. It then verifies that the frame count, duration, track list (including IETF tags and flags), attachments, and chapters match the input.

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

Required on `PATH`: `grav1synth`, `mkvmerge`, `ffprobe`.

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

## `nodownmix-apply.py`

Batch-add or remove the `.nodownmix` filename tag on `.mkv` files right before the `.mkv` extension (`Movie.2016.mkv` → `Movie.2016.nodownmix.mkv`). Leading queue prefixes such as `01 ` stay untouched so queue sorting is preserved. All three encoder scripts (`xav_automation.py`, `svt_opus_encoder.py`, `aom_opus_encoder.py`) recognize the tag case-insensitively and skip surround-to-stereo downmixing for that file while keeping the tag on the output in `completed/`.

### Prerequisites

Python 3 standard library only (no external tools).

### Behavior

- Renames files **in place** (does not create `completed/`, `original/`, `failed/`, or `conv_logs/`).
- Defaults to the current directory (non-recursive) when no paths are passed; accepts any mix of `.mkv` files and folders.
- Skips encoder work files (`*.prep.mkv`, `*.cfr.mkv`, `*.cfr_full.mkv`, `*.x264.mkv`, `*.hevc.mkv`, `*.ut.mkv`, `*.cfr_temp.mkv`, `*_xav.mkv`, `temp-*.mkv`, `output-*.mkv`).
- Skips files that are already tagged (or untagged when using `--remove`) and refuses to overwrite an existing target file.
- `--remove` strips a trailing `nodownmix` tag preceded by `.`, `-`, `_`, or whitespace (case-insensitive).

### Usage

```bash
nodownmix-apply.py                       # Tag all .mkv files in the current folder
nodownmix-apply.py file1.mkv file2.mkv   # Tag only specific files
nodownmix-apply.py -n /path/to/folder    # Dry-run: preview renames without changing files
nodownmix-apply.py --remove .            # Strip the nodownmix tag in the current folder
nodownmix-apply.py -r /path/to/folder    # Scan subfolders recursively
nodownmix-apply.py -q ...                # Quiet: only print warnings and errors
nodownmix-apply.py --tag custom ...      # Use a custom tag instead of "nodownmix"
```

## Suggested order

```text
noisy source  →  mdegrain.py  →  completed/  →  [nodownmix-apply.py]  →  xav_automation.py  →  completed/  →  av1grain.py
```

Run `nodownmix-apply.py` on any surround files you want kept surround before starting an encoder queue (and optionally `nodownmix-apply.py --remove` afterwards once you no longer need the visual marker in `completed/`). Skip `mdegrain.py` when the source is already clean. Skip `av1grain.py` when you want the encode left grain-free. Do not point `av1grain.py` at an `mdegrain.py` result: that file is still H.264 or H.265.

[`mdegrain_old.py`](mdegrain_old.py) is a legacy version kept in this folder only as a reference.
