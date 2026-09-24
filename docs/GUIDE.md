# Encoding guide

How the batch AV1 scripts run, and why they take the paths they do. Encoder flag tables live in [`parameters.md`](parameters.md). Crop detection is documented in [`../cropdetect/`](../cropdetect/).

## Scripts

*   **`aom_opus_encoder.py`**: Uses the `aom` encoder (specifically designed for the `aom-psy101` fork) via `av1an`. Auto-detects SDR vs HDR and 1080p vs 4K with the same intermediate/automation path as `svt_opus_encoder.py`. Default `cq-level` is 25. Av1an worker count is `(cpu_count // 2) - 1`.
*   **`svt_opus_encoder.py`**: Uses the `svt-av1` encoder (specifically designed for the `SVT-AV1-Essential` fork) via `av1an`. Auto-detects SDR vs HDR and 1080p vs 4K, then selects the matching intermediate, VapourSynth matrix, and SVT color/preset settings. Av1an worker count is `(cpu_count // 2) - 1`.
*   **`xav_automation.py`**: Uses the `xav` chunking encoder and `svt-av1` (SVT-AV1-Essential) instead of `av1an`. Features native autocrop, scene-detect, and chunking. A fail-closed packet-PTS CFR probe plus pixel-format checks decide whether to remux video-only or run HandBrake (with format conversion to `yuv420p` / `yuv420p10le` when needed).

A standalone crop detector lives in [`../cropdetect/`](../cropdetect/). The same logic is still embedded in `svt_opus_encoder.py` and `aom_opus_encoder.py` via `--autocrop` (an older copy; not imported from that folder yet).

## Prerequisites

The scripts require several external tools to be installed and available in your system's `PATH`:

*   **ffmpeg** & **ffprobe**: For video/audio extraction, filtering (cropdetect), loudnorm analysis, and (in `xav_automation.py`) packet-level CFR probing.
*   **mkvtoolnix** (`mkvmerge`, `mkvextract`, `mkvpropedit`): For remuxing the final MKV file, reading subtitle tracks and attachments during font cleanup, and restoring track metadata.
*   **fonttools** (optional Python package): Reads the name inside a font file so unused attachments can be dropped. Arch: `sudo pacman -S python-fonttools`. Elsewhere: `pip install fonttools`. If it is missing, font cleanup is skipped and every font stays attached.
*   **opusenc** (opus-tools): For encoding audio tracks to the Opus codec.
*   **mediainfo**: For extracting detailed media information (especially frame rate details and HDR metadata).
*   **av1an**: The core chunking encoder used by `svt_opus_encoder.py` and `aom_opus_encoder.py` to run multiple encode workers in parallel.
*   **HandBrakeCLI**: Used as a CFR pre-processor when a re-encode is required. `svt_opus_encoder.py` and `aom_opus_encoder.py` use it for ≤1080p SDR and for VFR 4K/HDR. `xav_automation.py` uses it unless packets prove CFR, MediaInfo is not VFR, and the pixel format is already `yuv420p` or `yuv420p10le`.
*   **ffmsindex** (ffms2): For indexing the video intermediate for Vapoursynth (`svt_opus_encoder.py` and `aom_opus_encoder.py`).
*   **Vapoursynth**: Required by `av1an` as the frame server via the generated `.vpy` scripts (`svt_opus_encoder.py` and `aom_opus_encoder.py`).
*   *(Specific to `aom_opus_encoder.py`)*: **aom-psy101** encoder. You must download the correct version from [Damian101's aom-psy101 GitLab](https://gitlab.com/damian101/aom-psy101).
*   *(Specific to `svt_opus_encoder.py`)*: **SVT-AV1-Essential** encoder. You must download the correct version from [nekotrix's SVT-AV1-Essential GitHub](https://github.com/nekotrix/SVT-AV1-Essential/).
*   *(Specific to `xav_automation.py`)*: **xav** chunking encoder. You must download the source from [emrakyz's xav GitHub](https://github.com/emrakyz/xav) and build it specifically with the SVT-AV1-Essential encoder. xav does not use `av1an`, `ffmsindex`, or VapourSynth.

## Features

*   **Automated Batch Processing**: Simply place your `.mkv` files in the same directory as the script. The script will process them one by one.
*   **Resumable Encoding**: `svt_opus_encoder.py` and `aom_opus_encoder.py` pass `--resume` to `av1an`. All three scripts reuse an existing `.prep.mkv` intermediate, encoded video (`temp-*.mkv`), and remuxed `output-*.mkv` when those files are already usable. If an encode fails, the source is moved to `failed/` and intermediates are kept so a retry can continue.
*   **Audio Normalization and Downmixing**:
    *   Extracts audio tracks to FLAC with DRC disabled (`-drc_scale 0`).
    *   Applies a two-pass linear constant-gain loudness normalization (Target: -18.0 LUFS, True Peak: -1.5 dBTP, LRA: 20 LU). Configurable via `--norm-i` and `--norm-tp`.
    *   After loudnorm, the FLAC is pinned back to the source sample rate so `opusenc` tags Input Sample Rate correctly (Opus still encodes at 48 kHz).
    *   Downmixes 5.1/7.1 surround sound to stereo (unless `--no-downmix` is specified). All scripts use a robust multi-pass downmix filter fallback system to prevent clipping.
    *   Encodes to Opus with bitrates automatically chosen based on the channel count (e.g., 128k for Stereo, 256k for 5.1).
    *   Directly remuxes existing `aac` or `opus` tracks without re-encoding.
    *   Preserves track languages (including IETF tags), titles, Matroska flags, and delays.
*   **VFR to CFR Conversion**: Detects Variable Frame Rate (VFR) media and converts it to Constant Frame Rate (CFR) when needed.
    *   `svt_opus_encoder.py` and `aom_opus_encoder.py` use MediaInfo `FrameRate_Mode`. ≤1080p SDR always gets a HandBrake x264 all-intra intermediate. 4K/HDR CFR is an mkvmerge video-only remux (keeps HDR/DoVi metadata). VFR 4K/HDR uses HandBrake `x265_10bit`. ffmpeg is a fallback.
    *   `xav_automation.py` does not trust MediaInfo/ffprobe "CFR" flags. A packet-PTS probe must prove CFR, MediaInfo must not report VFR, and the pixel format must already be `yuv420p` or `yuv420p10le` before HandBrake is skipped. Then mkvmerge video-only remuxes (1080p or 4K/HDR). Otherwise HandBrake converts to CFR and to an xav-compatible 4:2:0 8-bit or 10-bit format. ffmpeg is a fallback.
*   **Automatic Cropping**: Optional `--autocrop` flag detects black bars and applies the crop in VapourSynth before encoding (in `svt_opus_encoder.py` and `aom_opus_encoder.py`). `xav_automation.py` relies on xav's native autocrop (no CLI flag).
*   **Subtitle font cleanup**: On the final mux, a font attachment is kept only when a remaining ASS/SSA style or `\fn` override names it (the attachment’s filename, or the font’s family, full, or typographic name). Every non-font attachment is kept. Names the subtitles ask for but no attachment provides are not in the output; they are logged as missing. With no ASS/SSA tracks, every font attachment is dropped. `--nofontsclean` / `-nfc` copies every font instead.
    The per-file log in `conv_logs/` has one line for kept, one for dropped, and one for missing. Kept and dropped use the font’s full name, not the attachment filename, so the same font can be tracked across MKVs that use different file names. Names are separated with `; ` because a comma can be part of a font name (`Arial, Bold`). An empty list is `(none)`.

    ```
        - FONT_CLEAN kept: Arial Bold; Noto Sans CJK JP
        - FONT_CLEAN dropped: Comic Sans MS
        - FONT_CLEAN missing: Open Sans
    ```
*   **Organized Output**:
    *   Completed files are moved to a `completed/` directory.
    *   Original files are moved to an `original/` directory.
    *   Failed files are moved to a `failed/` directory, preserving intermediates for retry.
    *   Files ffmpeg cannot decode are moved to `original/` and skipped.
    *   Per-file processing logs are saved in a `conv_logs/` directory.
    *   Temporary files are automatically cleaned up upon success.

## Usage

It is highly recommended to place these scripts (or symbolic links to them) in a directory that is included in your system's `PATH` variable (e.g., `~/bin` on Linux/macOS, or a custom Scripts folder on Windows). This allows you to run the commands directly from any directory.

To use the scripts, open your terminal (bash, PowerShell, etc.), navigate to the folder containing your `.mkv` files, and simply type the name of the script.

### `aom_opus_encoder.py`

```bash
aom_opus_encoder.py [options]
```

**Options:**
*   `--no-downmix`: Preserve original audio channel layout (do not downmix 5.1/7.1 to stereo). AAC/Opus tracks are always remuxed.
*   `--autocrop`: Automatically detect and crop black bars from the video.
*   `--grain <int>`: Set the `photon-noise` value for grain synthesis. Disabled by default.
*   `--crf <int>`: Override aom `cq-level`. Default: 25 for all resolutions (SDR and HDR).
*   `--norm-i <float>`: Target integrated loudness in LUFS (default: -18.0).
*   `--norm-tp <float>`: True-peak ceiling in dBTP (default: -1.5).
*   `--nofontsclean`, `-nfc`: Keep every font attachment. Default: drop fonts that remaining ASS/SSA tracks do not name.

**Workflow specific to `aom_opus_encoder.py`:**
1. **Detection**: Same MediaInfo HDR/4K rules as `svt_opus_encoder.py`. 10-bit BT.709 Hi10p stays SDR.
2. **Video Preparation**: ≤1080p SDR gets a HandBrake x264 all-intra CFR intermediate. 4K/HDR CFR is an mkvmerge video-only remux. VFR 4K/HDR uses HandBrake `x265_10bit`. ffmpeg is a fallback. The prep file is indexed with `ffmsindex` and fed to VapourSynth (`709` for SDR, `2020ncl` for HDR).
3. **Video Encode**: `av1an` + aom-psy101, two passes. Workers are `(cpu_count // 2) - 1`. `cq-level` is always 25 unless `--crf` is set. HDR sets BT.2020 / PQ (or HLG) color metadata.
4. **Audio / Remux / Failures**: Same as `svt_opus_encoder.py`.

### `svt_opus_encoder.py`

```bash
svt_opus_encoder.py [options]
```

**Options:**
*   `--no-downmix`: Preserve original audio channel layout (do not downmix 5.1/7.1 to stereo). AAC/Opus tracks are always remuxed.
*   `--autocrop`: Automatically detect and crop black bars from the video.
*   `--preset <int>`: Override SVT-AV1 preset. Default: 1 if height ≤1080 and SDR, else 2.
*   `--crf <int>`: Override SVT-AV1 CRF. Default: 30 for all resolutions (SDR and HDR).
*   `--grain <int>`: Set the `film-grain` value. Adjusts the film grain synthesis level. Disabled by default.
*   `--tune <int>`: SVT-AV1-Essential `--tune` mode: 0=VQ, 1=PSNR, 2=SSIM, 3=IQ, 4=MS_SSIM (default: 2 = SSIM).
*   `--norm-i <float>`: Target integrated loudness in LUFS (default: -18.0).
*   `--norm-tp <float>`: True-peak ceiling in dBTP (default: -1.5).
*   `--nofontsclean`, `-nfc`: Keep every font attachment. Default: drop fonts that remaining ASS/SSA tracks do not name.

**Workflow specific to `svt_opus_encoder.py`:**
1. **Detection**: MediaInfo HDR format/transfer (PQ/HLG/DoVi) and height `>1080` choose the encode path. 10-bit BT.709 Hi10p stays SDR.
2. **Video Preparation**: ≤1080p SDR gets a HandBrake x264 all-intra CFR intermediate. 4K/HDR CFR is an mkvmerge video-only remux. VFR 4K/HDR uses HandBrake `x265_10bit`. ffmpeg is a fallback. The prep file is indexed with `ffmsindex` and fed to VapourSynth (`709` for SDR, `2020ncl` for HDR).
3. **Video Encode**: `av1an` + SVT-AV1-Essential. Workers are `(cpu_count // 2) - 1`. CRF is always 30 unless `--crf` is set. HDR 1080p uses preset 2 (same as 4K). HDR sets BT.2020 / PQ (or HLG) color metadata.
4. **Audio / Remux / Failures**: Same as `xav_automation.py` (ordered AAC/Opus remux or LUFS→Opus, restore track titles/flags, move failures to `failed/`).

### `xav_automation.py`

```bash
xav_automation.py [options]
```

**Options:**
*   `--no-downmix`: Keep surround on re-encoded tracks (no Nightmode Dialogue pan). AAC/Opus are always remuxed.
*   `--preset <int>`: Override SVT-AV1 preset. Default: 1 if height ≤1080, else 2 (height only; HDR 1080p still uses preset 1 unless overridden).
*   `--tune <int>`: SVT-AV1-Essential `--tune` mode: 0=VQ, 1=PSNR, 2=SSIM, 3=IQ, 4=MS_SSIM (default: 2 = SSIM).
*   `--crf <int>`: Override SVT-AV1 CRF. Default: 30 for all resolutions (SDR and HDR). Always passed so Essential does not use CRF 35 above 1080p.
*   `--norm-i <float>`: Target integrated loudness in LUFS (default: -18.0).
*   `--norm-tp <float>`: True-peak ceiling in dBTP (default: -1.5).
*   `--nofontsclean`, `-nfc`: Keep every font attachment. Default: drop fonts that remaining ASS/SSA tracks do not name.

**Workflow specific to `xav_automation.py`:**
1. **CFR / pixel-format gate**: ffprobe packet PTS must prove CFR (fail-closed). MediaInfo `FrameRate_Mode` is not treated as proof of CFR. If MediaInfo reports VFR, HandBrake always runs. A few GOP-start or probe-window duration outliers are allowed so real CFR MKVs are not sent to HandBrake. xav accepts only `yuv420p` or `yuv420p10le`; anything else (4:2:2, 4:4:4, RGB, 12-bit+) is converted.
2. **Video Preparation**: Skip HandBrake only when packets prove CFR, MediaInfo is not VFR, and the pixel format is already xav-compatible. Then mkvmerge video-only remuxes (1080p or 4K/HDR; no re-encode; keeps HDR10/DoVi track properties). Otherwise HandBrake: x264 all-intra for ≤1080p SDR 8-bit 4:2:0, `x264_10bit` all-intra for 1080p SDR that needs 10-bit, `x265_10bit` normal GOP for >1080p or HDR. ffmpeg is a fallback if HandBrake produces an empty file.
3. **Video Encode**: `xav` handles autocrop, scene-detect, and chunking natively (`xav -e svt-av1 -p "--preset <p> --tune <t> --crf <c>" -w 4 -b 1`). No `av1an` or `.vpy` required. CRF is always 30 unless `--crf` is set (same as `svt_opus_encoder.py`), so Essential does not apply `--quality medium` / CRF 35 above 1080p.
4. **Audio Processing**: Audio is extracted, optionally downmixed (with multiple filter fallbacks), normalized with a two-pass linear constant-gain loudnorm (sample rate restored after loudnorm), and encoded to Opus. AAC/Opus tracks are remuxed directly.
5. **Remuxing**: Combines using `mkvmerge` (xav video + processed/remuxed audio + source subs/chapters). Font attachments are filtered as described under Subtitle font cleanup; other attachments are copied. The `FONT_CLEAN` lines are written to that file’s log. Track metadata (flags, titles, languages) is restored from the source. Failed files move the source MKV to `failed/` while keeping video intermediates for easy retry resuming.

## Process workflow

1.  **Preparation**: Scans for `.mkv` files (skipping intermediates such as `.prep.mkv`, `temp-*`, `output-*`) and checks for required tools.
2.  **Analysis**: Examines video and audio tracks using `ffprobe` and `mediainfo`. `xav_automation.py` also probes packet timestamps to confirm true CFR.
3.  **Video Processing**:
    *   Runs crop detection (if `--autocrop` is enabled in the av1an scripts).
    *   Creates a video intermediate when required (see per-script workflows above).
    *   Encodes the video using `av1an` (or `xav` for `xav_automation.py`).
4.  **Audio Processing**:
    *   Remuxes AAC/Opus.
    *   Normalizes, downmixes (if applicable), and encodes other formats to Opus.
5.  **Muxing**: Combines the newly encoded video and audio tracks using `mkvmerge`, copying source subtitles, non-font attachments, and chapters. Font attachments are kept only when a remaining ASS/SSA track names them, unless `--nofontsclean` is set. The encode log records `FONT_CLEAN kept`, `dropped`, and `missing` (one line each, names separated by `; `). Synchronization delays, metadata, and languages are preserved.
6.  **Cleanup**: Moves files to respective folders (`completed/`, `original/`) and deletes temporary working files. On failure, the source goes to `failed/` and intermediates are kept.

## Notes

- Encoding AV1 takes a significant amount of time and CPU resources.
- Ensure you have sufficient disk space, as the scripts generate intermediate CRF 0 video files which can be very large depending on the length and resolution of the source media.
- Dolby Vision sources are encoded as HDR10/HLG AV1. Av1an/aom/SVT and xav do not emit a DoVi RPU; the mkvmerge remux path keeps source DoVi track properties on the *intermediate* only.

## License

This project is licensed under the MIT License - see [`LICENSE.md`](../LICENSE.md).
