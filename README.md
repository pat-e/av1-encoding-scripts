# AV1 Encoding Scripts

Batch-encode `.mkv` files to **AV1** video and **Opus** audio. Drop the MKVs in a folder, run one of the scripts, get `completed/`, `original/`, and `failed/` plus per-file logs.

| Script | Encoder | Notes |
| :--- | :--- | :--- |
| [`svt_opus_encoder.py`](svt_opus_encoder.py) | [SVT-AV1-Essential](https://github.com/nekotrix/SVT-AV1-Essential/) via av1an | Auto SDR/HDR and 1080p/4K. Default CRF 30, tune SSIM. |
| [`aom_opus_encoder.py`](aom_opus_encoder.py) | [aom-psy101](https://gitlab.com/damian101/aom-psy101) via av1an | Same prep path as SVT. Default cq-level 25, two-pass. |
| [`xav_automation.py`](xav_automation.py) | [xav](https://github.com/emrakyz/xav) + SVT-AV1-Essential | Native autocrop/chunking. Packet-CFR probe before HandBrake. CRF 30 always (not Essential’s 35 above 1080p). |
| [`cropdetect/cropdetect.py`](cropdetect/cropdetect.py) | ffmpeg cropdetect | Safe crop for mixed AR (flashbacks, IMAX). Own [readme](cropdetect/). |

Put the scripts (or symlinks) on your `PATH`, `cd` to a directory of `.mkv` files, and run the script you want:

```bash
svt_opus_encoder.py
# or: aom_opus_encoder.py
# or: xav_automation.py
```

**Requires in `PATH`:** ffmpeg, ffprobe, mkvmerge, mkvextract, mkvpropedit, opusenc, mediainfo, HandBrakeCLI. Av1an scripts also need av1an, ffmsindex, and VapourSynth. xav needs `xav` built with SVT-AV1-Essential. Python [fonttools](https://github.com/fonttools/fonttools) is optional (Arch: `python-fonttools`, otherwise `pip install fonttools`). Without it, unused-font cleanup is skipped and every font stays attached.

Common flags: `--no-downmix`, `--autocrop` (av1an scripts), `--crf` / `--preset` / `--tune` / `--grain` where the encoder supports them, `--norm-i` / `--norm-tp` (defaults −18 LUFS / −1.5 dBTP), `--nofontsclean` / `-nfc` (keep every attached font).

Audio: AAC/Opus remuxed; everything else loudnorm → Opus. Output folders: `completed/`, `original/`, `failed/`, `conv_logs/`. Encodes resume from `.prep.mkv` / `temp-*.mkv` when present.

**Subtitle fonts:** On the final mux, a font attachment is kept only when a remaining ASS/SSA track names it. Every other attachment stays. `--nofontsclean` / `-nfc` copies every font. The encode log records one line each for fonts kept, dropped, and missing. Kept and dropped use the font’s full name, not the attachment filename, so the same font matches across MKVs that attach it under different file names. Missing names are the ones written in the subtitles. A semicolon separates names, because a comma can be part of a font name. `(none)` means that list is empty.

```
    - FONT_CLEAN kept: Arial Bold; Noto Sans CJK JP
    - FONT_CLEAN dropped: Comic Sans MS
    - FONT_CLEAN missing: Open Sans
```

How it works and why (HandBrake vs remux, VFR, downmix, folders): **[Encoding guide](docs/GUIDE.md)**  
Encoder flags, loudnorm, CFR probe, av1an/xav command lines: **[Parameters](docs/parameters.md)**

MIT — see [`LICENSE.md`](LICENSE.md).
