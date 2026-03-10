# HDR Encoding Configuration Parameters

This document details the configuration parameters used in the `hdr_svt_opus_encoder.py` script.

## Audio Normalization & Encoding

Audio tracks are normalized using `ffmpeg`'s `loudnorm` filter and then encoded to Opus.

### Loudness Normalization Parameters
The script uses a two-pass loudness normalization with the following target values:

- **Integrated Loudness (I)**: `-23` LUFS
- **Loudness Range (LRA)**: `7` LU
- **True Peak (tp)**: `-1.5` dBTP

### Opus Encoding Bitrates
Audio is encoded with the following bitrates based on the original channel count:

- **Mono (1 channel)**: `64k`
- **Stereo (2 channels)**: `128k`
- **5.1 Surround (6 channels)**: `256k`
- **7.1 Surround (8 channels)**: `384k`
- **Other/Uncommon Layouts**: `192k` (fallback default)

## SVT-AV1 Encoder Parameters

> **Encoder Version**: SVT-AV1-Essential from [https://github.com/nekotrix/SVT-AV1-Essential/](https://github.com/nekotrix/SVT-AV1-Essential/)

Default parameters initialized for the `svt-av1` encoder for HDR content:
```text
--speed slower
--quality medium
--film-grain 12
--color-primaries 9
--transfer-characteristics 16
--matrix-coefficients 9
--scd 0
--keyint 0
--lp 2
--auto-tiling 1
--tune 1
--progress 2
```
*(Note: Parameters such as `--speed`, `--quality`, and `--film-grain` can be overridden with command-line arguments when executing the script).*\
\
## av1an Initiation Command

Arguments used to start `av1an` with the SVT-AV1 encoder:
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
