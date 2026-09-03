# Encoding Configuration Parameters

This document details the configuration parameters used across the AomEnc, SVT-AV1, and xav encoding scripts.

## Audio Loudness Normalization

All scripts use a constant-gain loudness normalization approach (no LRA compressor). The two-pass process measures integrated loudness, then applies a single gain with brickwall true-peak clamping.

- **Target Integrated Loudness (I)**: `-16.0` LUFS
- **True Peak Ceiling (TP)**: `-1.5` dBTP

These defaults can be overridden at runtime with `--norm-i` and `--norm-tp`.

## Audio Demuxing & Downmixing

The audio processing extracts streams using `ffmpeg` and automatically downmixes surround layouts to stereo if requested.

### Downmixing Parameters
- **5.1 Channel Layouts (6 channels)**
  ```text
  -af "pan=stereo|c0=c2+0.30*c0+0.30*c4|c1=c2+0.30*c1+0.30*c5"
  ```

- **7.1 Channel Layouts (8 channels)**
  ```text
  -af "pan=stereo|c0=c2+0.30*c0+0.30*c4+0.30*c6|c1=c2+0.30*c1+0.30*c5+0.30*c7"
  ```

### Non-Downmixed Encoding Bitrates (Opus)
When preserving the original channel layout (no downmixing) or if the source is already stereo/mono, audio is encoded with the following bitrates based on channel count:

- **Mono (1 channel)**: `64k`
- **Stereo (2 channels)**: `128k`
- **5.1 Surround (6 channels)**: `256k`
- **7.1 Surround (8 channels)**: `384k`
- **Other/Uncommon Layouts**: `192k` (fallback default)

## VFR to CFR Conversion

### `svt_opus_encoder.py`

To handle Variable Frame Rate (VFR) sources reliably before UTVideo intermediate generation, `HandBrakeCLI` is used to convert them to Constant Frame Rate (CFR). Only detected VFR sources are converted; CFR sources skip this step.

The exact HandBrakeCLI arguments used:
```text
HandBrakeCLI \
  --input <source_file> \
  --output <intermediate_cfr_file> \
  --cfr \
  --rate <target_cfr_fps> \
  --encoder x264_10bit \
  --quality 0 \
  --encoder-preset superfast \
  --encoder-tune fastdecode \
  --audio none \
  --subtitle none \
  --crop-mode none
```

### `aom_opus_encoder.py`

`aom_opus_encoder.py` creates a HandBrakeCLI CFR intermediate for **all** sources (both VFR and CFR), replacing the previous UTVideo pass. The intermediate is indexed with `ffmsindex` and fed directly to VapourSynth. The intermediate encoder is selected based on bit depth (1080p SDR only — no HEVC/HDR path):

- **8-bit SDR**: `x264` CRF 0, all-intra (`keyint=1:bframes=0`)
- **10-bit SDR (Hi10p)**: `x264_10bit` CRF 0, all-intra (`keyint=1:bframes=0`)

If HandBrakeCLI fails or cannot determine the frame rate, ffmpeg is used as a fallback with equivalent settings (forced CFR via `-fps_mode cfr`).

### `xav_automation.py`

`xav_automation.py` creates a HandBrakeCLI CFR intermediate for **all** sources (both VFR and CFR) because xav requires seekable, constant-frame-rate input. The intermediate encoder is selected automatically based on resolution and HDR status:

- **≤1080p SDR (8-bit)**: `x264` CRF 0, all-intra (`keyint=1:bframes=0`)
- **≤1080p SDR (10-bit / Hi10p)**: `x264_10bit` CRF 0, all-intra (`keyint=1:bframes=0`)
- **>1080p or HDR**: `x265_10bit` CRF 0, normal GOP (not all-intra)

If HandBrakeCLI fails or cannot determine the frame rate, ffmpeg is used as a fallback with equivalent settings.

## Encoder-Specific Parameters

### AomEnc (aom-psy101)
> **Special Version Repository**: [https://gitlab.com/damian101/aom-psy101](https://gitlab.com/damian101/aom-psy101)

Parameters parsed to the `aom` encoder:

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `--bit-depth` | `10` | Force 10-bit encoding for better color precision and less banding |
| `--cpu-used` | `2` | Speed preset. Lower is slower/better quality. 4 is default, 2 is slow/high quality |
| `--end-usage` | `q` | Constant Quality mode |
| `--cq-level` | `24` | The target quality level (0-63). Lower is better quality/larger file |
| `--min-q` | `8` | Minimum allowable quantizer to prevent bitrate spikes on flat frames |
| `--threads` | `2` | Threads per av1an worker |
| `--tune-content` | `psy` | Specialized tuning for psychovisual quality (needs aom-psy101) |
| `--tune` | `ssim` | Protects structural edges universally |
| `--sharpness` | `2` | Edge protection that won't cause halos in live-action |
| `--arnr-maxframes` | `7` | Middle-ground temporal filtering (default is 7) |
| `--arnr-strength` | `2` | Middle-ground filtering strength (default is 5) |
| `--quant-b-adapt` | `1` | Universal B-frame efficiency |
| `--frame-parallel` | `1` | Enable frame parallel decoding |
| `--tile-columns` | `1` | Use 2 tile columns (2^1) for faster decoding |
| `--gf-max-pyr-height` | `5` | Golden Frame pyramid height (max is 5) |
| `--deltaq-mode` | `2` | Enable perceptual quantizer (AQ mode based on variance) |
| `--enable-keyframe-filtering` | `0` | We disable internal KF filtering as av1an handles chunking |
| `--disable-kf` | *(flag)* | Disable internal keyframes (av1an inserts them at scene cuts) |
| `--enable-fwd-kf` | `1` | Enable forward keyframes |
| `--kf-max-dist` | `9999` | Set max keyframe distance arbitrarily high |
| `--sb-size` | `64` | Allow the encoder to choose 64x64 or 128x128 superblocks dynamically |
| `--enable-chroma-deltaq` | `1` | Enable chroma quantization adjustment |
| `--enable-qm` | `1` | Enable quantization matrices for better high-frequency detail retention |
| `--lag-in-frames` | `64` | Max lookahead buffer (default is 19, max is 64) for improved temporal filtering and rate control |
| `--color-primaries` | `bt709` | Standard SDR color space |
| `--transfer-characteristics`| `bt709` | Standard SDR transfer characteristics |
| `--matrix-coefficients` | `bt709` | Standard SDR matrix coefficients |

*(Note: `--cq-level` dynamically defaults to `24` but can be overwritten when executing the script via the `--crf` argument. `--photon-noise` is omitted by default unless `--grain` is provided.)*

### SVT-AV1 (SVT-AV1-Essential)
> **Special Version Repository**: [https://github.com/nekotrix/SVT-AV1-Essential/](https://github.com/nekotrix/SVT-AV1-Essential/)

Parameters initialized for the `svt-av1` encoder (as used in `svt_opus_encoder.py`):

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `--preset` | `1` | Speed preset. Lower is slower and yields better compression efficiency. |
| `--color-primaries` | `1` | BT.709 color primaries (Standard SDR). |
| `--transfer-characteristics`| `1` | BT.709 transfer characteristics (Standard SDR). |
| `--matrix-coefficients` | `1` | BT.709 matrix coefficients (Standard SDR). |
| `--scd` | `0` | Scene change detection OFF (av1an handles scene cuts). |
| `--scm` | `0` | Screen content detection OFF (0: off, 1: on, 2: content adaptive). |
| `--keyint` | `0` | Keyframe interval OFF (av1an inserts keyframes). |
| `--auto-tiling` | `1` | Automatically determine the number of tiles based on resolution. |
| `--progress` | `2` | Detailed progress output. |

*(Note: `--preset` can be overridden when executing the script. Grain synthesis (`--film-grain`) is omitted by default unless `--grain` is provided. CRF is not set in the default params and must be provided via the chunking encoder.)*

### SVT-AV1 via xav (SVT-AV1-Essential)

Parameters used for the `svt-av1` encoder when invoked via `xav` (as used in `xav_automation.py`):

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `--preset` | `1` (≤1080p) / `2` (>1080p) | Speed preset. Automatically chosen based on video height. |
| `--tune` | `2` | SVT-AV1-Essential tune mode: 0=VQ, 1=PSNR, 2=SSIM, 3=IQ, 4=MS_SSIM. |

*(Note: `--preset` and `--tune` can be overridden when executing the script. CRF is not passed as a default parameter.)*

## Chunking Encoder Initiation Commands

### av1an (AomEnc)
Arguments used to start `av1an` using the AomEnc encoder:
```text
av1an -i <vpy_script> -o <encoded_mkv> -n \
  -e aom \
  --resume \
  --sc-pix-format yuv420p \
  -c mkvmerge \
  --set-thread-affinity 2 \
  --pix-format yuv420p10le \
  --force \
  --no-defaults \
  -w <calculated_workers> \
  --passes 2 \
  -v "<aom_encoder_parameters_above>"
```

*(Note: `--photon-noise <int>` is appended to the `av1an` arguments only when `--grain` is provided at runtime.)*

### av1an (SVT-AV1)
Arguments used to start `av1an` using the SVT-AV1 encoder:
```text
av1an -i <vpy_script> -o <encoded_mkv> -n \
  -e svt-av1 \
  --resume \
  --sc-pix-format yuv420p \
  -c mkvmerge \
  --set-thread-affinity 2 \
  --pix-format yuv420p10le \
  --force \
  --no-defaults \
  -w <calculated_workers> \
  -v "<svt_av1_encoder_parameters_above>"
```

### xav (SVT-AV1)
Arguments used to start `xav` using the SVT-AV1 encoder (as used in `xav_automation.py`):
```text
xav -e svt-av1 \
  -p "--preset <preset> --tune <tune>" \
  -w 4 \
  -b 1 \
  <intermediate_file> \
  <encoded_video_file>
```
- `-w 4`: Fixed at 4 workers.
- `-b 1`: Buffer size of 1.
- `--preset`: Defaults to `1` for ≤1080p, `2` for >1080p (overridable via `--preset`).
- `--tune`: Defaults to `2` (SSIM) (overridable via `--tune`).

*(Note: `--preset` and `--tune` can be overridden when executing the script, which modifies the arguments passed to `-p`. No `-a` flag is used — audio processing is handled entirely by the script.)*
