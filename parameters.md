# Encoding Configuration Parameters

This document details the configuration parameters used across the AomEnc and SVT-AV1 encoding scripts.

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

To handle Variable Frame Rate (VFR) sources reliably on UTVideo intermediate generation, `HandBrakeCLI` is used to convert them to Constant Frame Rate (CFR) before processing.

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

## Encoder-Specific Parameters

### AomEnc (aom-psy101)
> **Special Version Repository**: [https://gitlab.com/damian101/aom-psy101](https://gitlab.com/damian101/aom-psy101)

Parameters parsed to the `aom` encoder:

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `--bit-depth` | `10` | Force 10-bit encoding for better color precision and less banding |
| `--cpu-used` | `2` | Speed preset. Lower is slower/better quality. 4 is default, 2 is slow/high quality |
| `--end-usage` | `q` | Constant Quality mode |
| `--cq-level` | `25` | The target quality level (0-63). Lower is better quality/larger file |
| `--min-q` | `8` | Minimum allowable quantizer to prevent bitrate spikes on flat frames |
| `--threads` | `2` | Threads per av1an worker |
| `--tune-content` | `psy` | Specialized tuning for psychovisual quality (needs aom-psy101) |
| `--tune` | `ssim` | Protects structural edges universally |
| `--sharpness` | `2` | Edge protection that won't cause halos in live-action |
| `--arnr-maxframes` | `7` | Middle-ground temporal filtering (default is 7) |
| `--arnr-strength` | `1` | Middle-ground filtering strength (default is 5) |
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

*(Note: `--cq-level` dynamically defaults to `25` but can be overwritten when executing the script via the `--crf` argument).*

### SVT-AV1 (SVT-AV1-Essential)
> **Special Version Repository**: [https://github.com/nekotrix/SVT-AV1-Essential/](https://github.com/nekotrix/SVT-AV1-Essential/)

Parameters initialized for the `svt-av1` encoder:

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `--preset` | `2` | Speed preset. Lower is slower and yields better compression efficiency. |
| `--crf` | `30` | Constant Rate Factor (CRF). Lower is better quality. |
| `--film-grain` | `6` | Film grain synthesis level. Adds artificial grain to preserve detail and prevent banding. |
| `--color-primaries` | `1` | BT.709 color primaries (Standard SDR). |
| `--transfer-characteristics`| `1` | BT.709 transfer characteristics (Standard SDR). |
| `--matrix-coefficients` | `1` | BT.709 matrix coefficients (Standard SDR). |
| `--scd` | `0` | Scene change detection OFF (av1an handles scene cuts). |
| `--keyint` | `0` | Keyframe interval OFF (av1an inserts keyframes). |
| `--lp` | `2` | Logical Processors to use per av1an worker. |
| `--auto-tiling` | `1` | Automatically determine the number of tiles based on resolution. |
| `--tune` | `0` | 0 = VQ, 1 = PSNR, 2 = SSIM (SVT-AV1-Essential default recommended). |
| `--progress` | `2` | Detailed progress output. |

*(Note: Parameters such as `--preset`, `--crf`, and `--film-grain` can be overridden when executing the script).*

## av1an Initiation Commands

### AomEnc
Arguments used to start `av1an` using the AomEnc encoder:
```text
av1an -i <vpy_script> -o <encoded_mkv> -n \
  -e aom \
  --photon-noise <grain> \
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

### SVT-AV1
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
