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
```text
--bit-depth=10 \
--cpu-used=2 \
--end-usage=q \
--cq-level=<crf_value> \
--min-q=6 \
--threads=2 \
--tune-content=psy \
--frame-parallel=1 \
--tile-columns=1 \
--gf-max-pyr-height=4 \
--deltaq-mode=2 \
--enable-keyframe-filtering=0 \
--disable-kf \
--enable-fwd-kf=0 \
--kf-max-dist=9999 \
--sb-size=dynamic \
--enable-chroma-deltaq=1 \
--enable-qm=1 \
--color-primaries=bt709 \
--transfer-characteristics=bt709 \
--matrix-coefficients=bt709
```
*(Note: `--cq-level` dynamically defaults to `28` but can be overwritten when executing the script via the `--crf` argument).*

### SVT-AV1 (SVT-AV1-Essential)
> **Special Version Repository**: [https://github.com/nekotrix/SVT-AV1-Essential/](https://github.com/nekotrix/SVT-AV1-Essential/)

Parameters initialized for the `svt-av1` encoder:
```text
--speed slower \
--quality medium \
--film-grain <grain_value> \
--color-primaries 1 \
--transfer-characteristics 1 \
--matrix-coefficients 1 \
--scd 0 \
--keyint 0 \
--lp 2 \
--auto-tiling 1 \
--tune 1 \
--progress 2
```
*(Note: Parameters such as `--speed`, `--quality`, and `--film-grain` can be overridden when executing the script).*

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
