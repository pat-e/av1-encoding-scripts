# AV1 Encoding Scripts

Batch-encode `.mkv` files to **AV1** video and **Opus** audio. Drop the MKVs in a folder, run one of the scripts, get `completed/`, `original/`, and `failed/` plus per-file logs.

| Script | Encoder | Notes |
| :--- | :--- | :--- |
| [`svt_opus_encoder.py`](svt_opus_encoder.py) | [SVT-AV1-Essential](https://github.com/nekotrix/SVT-AV1-Essential/) via av1an | Auto SDR/HDR and 1080p/4K. Default CRF 30, tune SSIM. |
| [`aom_opus_encoder.py`](aom_opus_encoder.py) | [aom-psy101](https://gitlab.com/damian101/aom-psy101) via av1an | Same prep path as SVT. Default cq-level 25, two-pass. |
| [`xav_automation.py`](xav_automation.py) | [xav](https://github.com/emrakyz/xav) + SVT-AV1-Essential | Native autocrop/chunking. Packet-CFR probe before HandBrake. |
| [`cropdetect/cropdetect.py`](cropdetect/cropdetect.py) | ffmpeg cropdetect | Safe crop for mixed AR (flashbacks, IMAX). Own [readme](cropdetect/). |

Put the scripts (or symlinks) on your `PATH`, `cd` to a directory of `.mkv` files, and run the script you want:

```bash
svt_opus_encoder.py
# or: aom_opus_encoder.py
# or: xav_automation.py
```

**Requires in `PATH`:** ffmpeg, ffprobe, mkvmerge, mkvpropedit, opusenc, mediainfo, HandBrakeCLI. Av1an scripts also need av1an, ffmsindex, and VapourSynth. xav needs `xav` built with SVT-AV1-Essential.

Common flags: `--no-downmix`, `--autocrop` (av1an scripts), `--crf` / `--preset` / `--tune` / `--grain` where the encoder supports them, `--norm-i` / `--norm-tp` (defaults −18 LUFS / −1.5 dBTP).

Audio: AAC/Opus remuxed; everything else loudnorm → Opus. Output folders: `completed/`, `original/`, `failed/`, `conv_logs/`. Encodes resume from `.prep.mkv` / `temp-*.mkv` when present.

How it works and why (HandBrake vs remux, VFR, downmix, folders): **[Encoding guide](docs/GUIDE.md)**  
Encoder flags, loudnorm, CFR probe, av1an/xav command lines: **[Parameters](docs/parameters.md)**

MIT — see [`LICENSE.md`](LICENSE.md).
