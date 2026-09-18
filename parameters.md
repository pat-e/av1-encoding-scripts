# Encoding Configuration Parameters

This document details the configuration parameters used across the AomEnc, SVT-AV1, and xav encoding scripts.

## Audio Loudness Normalization

All scripts use a two-pass linear constant-gain loudness normalization approach (no dynamic LRA compression). The process measures integrated loudness and true peak, then applies a linear loudnorm pass (`linear=true`) to reach the target LUFS while respecting the true-peak limit.

- **Target Integrated Loudness (I)**: `-18.0` LUFS
- **True Peak Ceiling (TP)**: `-1.5` dBTP
- **Loudness Range (LRA)**: `20.0` LU (set high so loudnorm stays in linear/constant-gain mode; it is not used as a compressor)

These I/TP defaults can be overridden at runtime with `--norm-i` and `--norm-tp`. If the measured source LRA is greater than 20, ffmpeg loudnorm may silently switch to dynamic mode.

loudnorm true-peak analysis uses 4× oversampling (48 kHz → 192 kHz). After the second pass, the FLAC is pinned back to the source track's sample rate (`aformat=sample_fmts=s32:sample_rates=<source>`) so `opusenc` tags Input Sample Rate correctly. Opus still encodes at 48 kHz internally. If the source rate cannot be read, 48000 Hz is used.

## Audio Demuxing & Downmixing

The audio processing extracts streams using `ffmpeg` (`-drc_scale 0` so decoder DRC is off) and automatically downmixes surround layouts to stereo if requested. Existing `aac` and `opus` tracks are remuxed without re-encoding.

### Downmixing Parameters (Nightmode Dialogue)

When downmixing, the scripts use a multi-pass fallback system ("Nightmode Dialogue" by Collier/Harrelson) that renormalizes via pan `<` to prevent clipping.

- **5.1 Channel Layouts (6 channels)**
  Attempts the following filters in order until one succeeds:
  1. `-af "pan=stereo|FL<FC+0.30*FL+0.30*SL|FR<FC+0.30*FR+0.30*SR"`
  2. `-af "pan=stereo|FL<FC+0.30*FL+0.30*BL|FR<FC+0.30*FR+0.30*BR"`
  3. `-af "aformat=ch_layouts=5.1,pan=stereo|FL<FC+0.30*FL+0.30*BL|FR<FC+0.30*FR+0.30*BR"`
  4. `-af "pan=stereo|c0<c2+0.30*c0+0.30*c4|c1<c2+0.30*c1+0.30*c5"`
  5. Fallback: `-ac 2`

- **7.1 Channel Layouts (8 channels)**
  Attempts the following filters in order until one succeeds:
  1. `-af "pan=stereo|FL<FC+0.30*FL+0.30*SL+0.30*BL|FR<FC+0.30*FR+0.30*SR+0.30*BR"`
  2. `-af "pan=stereo|c0<c2+0.30*c0+0.30*c4+0.30*c6|c1<c2+0.30*c1+0.30*c5+0.30*c7"`
  3. Fallback: `-ac 2`

### Non-Downmixed Encoding Bitrates (Opus)
When preserving the original channel layout (no downmixing) or if the source is already stereo/mono, audio is encoded with `opusenc --vbr` at the following bitrates based on channel count:

- **Mono (1 channel)**: `64k`
- **Stereo (2 channels)**: `128k`
- **5.1 Surround (6 channels)**: `256k`
- **7.1 Surround (8 channels)**: `384k`
- **Other/Uncommon Layouts**: `192k` (fallback default)
- **Downmixed 5.1/7.1**: `128k` (treated as stereo)

## VFR to CFR Conversion

### `svt_opus_encoder.py`

`svt_opus_encoder.py` uses MediaInfo `FrameRate_Mode` (not a packet-PTS probe). The prep file is indexed with `ffmsindex` and fed to VapourSynth / av1an:

- **≤1080p SDR (8-bit)**: HandBrakeCLI `x264` CRF 0, all-intra (`keyint=1:bframes=0`)
- **≤1080p SDR (10-bit / Hi10p)**: HandBrakeCLI `x264_10bit` CRF 0, all-intra
- **>1080p or HDR, CFR**: mkvmerge video-only remux (no re-encode; keeps HDR10/DoVi track properties)
- **>1080p or HDR, VFR**: HandBrakeCLI `x265_10bit` CRF 0, normal GOP

If VFR is reported but a target frame rate cannot be determined, the source is sent on without a HandBrake pass. If HandBrakeCLI fails or cannot determine the frame rate, ffmpeg is used as a fallback with equivalent settings (forced CFR via `-fps_mode cfr`). Existing 4K/HDR HandBrake re-encode intermediates are discarded and remuxed instead.

HandBrake always gets `--rate` from MediaInfo's original frame rate (never HandBrake's guessed 29.97).

### `aom_opus_encoder.py`

`aom_opus_encoder.py` uses the same intermediate strategy as `svt_opus_encoder.py`. The prep file is indexed with `ffmsindex` and fed to VapourSynth / av1an:

- **≤1080p SDR (8-bit)**: HandBrakeCLI `x264` CRF 0, all-intra (`keyint=1:bframes=0`)
- **≤1080p SDR (10-bit / Hi10p)**: HandBrakeCLI `x264_10bit` CRF 0, all-intra
- **>1080p or HDR, CFR**: mkvmerge video-only remux (no re-encode; keeps HDR10/DoVi track properties)
- **>1080p or HDR, VFR**: HandBrakeCLI `x265_10bit` CRF 0, normal GOP

If HandBrakeCLI fails or cannot determine the frame rate, ffmpeg is used as a fallback with equivalent settings (forced CFR via `-fps_mode cfr`).

### `xav_automation.py`

xav requires seekable, constant-frame-rate input in `yuv420p` or `yuv420p10le` only. MediaInfo/ffprobe "CFR" flags are not trusted.

**Skip HandBrake** (mkvmerge video-only remux, 1080p or 4K/HDR) only when all of these are true:

1. Packet-PTS probe proves CFR (fail-closed)
2. MediaInfo `FrameRate_Mode` is not VFR/Variable
3. Pixel format is already `yuv420p` or `yuv420p10le`

Otherwise HandBrake runs (CFR hammer plus format conversion). ffmpeg is only a fallback if HandBrake produces an empty file.

When HandBrake (or ffmpeg) re-encodes:

- **≤1080p SDR, 8-bit 4:2:0**: `x264` CRF 0, all-intra (`keyint=1:bframes=0`), `yuv420p` (xav upconverts 8-bit internally)
- **≤1080p SDR that needs 10-bit** (Hi10p, 12/16-bit, 4:2:2, 4:4:4, RGB): `x264_10bit` CRF 0, all-intra, `yuv420p10le`
- **>1080p or HDR**: `x265_10bit` CRF 0, normal GOP, `yuv420p10le`

An existing `.prep.mkv` is remade if it is VFR, fails the packet CFR probe, is not xav-compatible, or (for 4K/HDR that should remux) was a leftover HandBrake re-encode.

#### Packet-level CFR probe

The probe reads up to 800 video packets via ffprobe, merges same-frame NAL timestamps within 0.5 ms, and compares inter-frame durations to the median. It fails closed on any doubt (too few packets, negative timestamps, header FPS mismatch, too many outliers).

A handful of duration outliers is treated as GOP-start / probe-window noise, not VFR. The first and last measured duration are dropped before the median/outlier test. At 0.2% max outliers, 2/799 already failed; the floor is now `max(4, 1% of measured durations)`.

| Check | Value |
| :--- | :--- |
| Packets sampled | 800 |
| Minimum unique PTS / durations | 120 |
| Same-frame PTS merge | 0.5 ms |
| Edge durations dropped | first and last (when enough samples remain) |
| Duration slop | max(1.2% of median, 2 ms) — 2 ms so 24 fps MKV 41/42 ms ticks still count as CFR |
| Outliers allowed | `max(4, 1% of measured durations)` |
| Header FPS vs packet FPS | within 3% |

## Encoder-Specific Parameters

### AomEnc (aom-psy101)
> **Special Version Repository**: [https://gitlab.com/damian101/aom-psy101](https://gitlab.com/damian101/aom-psy101)

Parameters parsed to the `aom` encoder:

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `--bit-depth` | `10` | Force 10-bit encoding for better color precision and less banding |
| `--cpu-used` | `2` | Speed preset. Lower is slower/better quality. 4 is default, 2 is slow/high quality |
| `--good` | *(flag)* | Good quality mode (deadline preset) |
| `--end-usage` | `q` | Constant Quality mode |
| `--cq-level` | `25` | The target quality level (0-63). Lower is better quality/larger file. Always passed. |
| `--min-q` | `8` | Minimum allowable quantizer to prevent bitrate spikes on flat frames |
| `--threads` | `2` | Threads per av1an worker |
| `--tune-content` | `psy` | Specialized tuning for psychovisual quality (needs aom-psy101) |
| `--tune` | `psnr` | Tune distortion metric for PSNR |
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
| `--color-primaries` | `bt709` (SDR) / `bt2020` (HDR) | Color primaries |
| `--transfer-characteristics`| `bt709` (SDR) / `smpte2084` (PQ) / `arib-std-b67` (HLG) | Transfer characteristics |
| `--matrix-coefficients` | `bt709` (SDR) / `bt2020ncl` (HDR) | Matrix coefficients. VapourSynth uses `matrix_in_s="709"` or `"2020ncl"` to match. |

*(Note: `--cq-level` defaults to `25` for all resolutions and can be overwritten via `--crf`. `--photon-noise` is omitted by default unless `--grain` is provided. Dolby Vision sources are encoded as HDR10/HLG AV1; Av1an/aom does not emit a DoVi RPU.)*

### SVT-AV1 (SVT-AV1-Essential)
> **Special Version Repository**: [https://github.com/nekotrix/SVT-AV1-Essential/](https://github.com/nekotrix/SVT-AV1-Essential/)

Parameters initialized for the `svt-av1` encoder (as used in `svt_opus_encoder.py`). Color metadata and preset are chosen per file from MediaInfo (SDR vs HDR, height ≤1080 vs 4K). CRF is always `30` unless `--crf` is passed, so Essential does not apply `--quality medium` (CRF 35) above 1080p.

| Parameter | SDR | HDR (PQ) | HDR (HLG) | Description |
| :--- | :--- | :--- | :--- | :--- |
| `--preset` | `1` (≤1080p) / `2` (>1080p) | `2` | `2` | Speed preset. Lower is slower and yields better compression efficiency. HDR uses 2 even at 1080p. |
| `--crf` | `30` | `30` | `30` | Constant Rate Factor. Always passed. |
| `--color-primaries` | `1` (BT.709) | `9` (BT.2020) | `9` (BT.2020) | Color primaries. |
| `--transfer-characteristics` | `1` (BT.709) | `16` (PQ / SMPTE 2084) | `18` (HLG) | Transfer characteristics. |
| `--matrix-coefficients` | `1` (BT.709) | `9` (BT.2020 NCL) | `9` (BT.2020 NCL) | Matrix coefficients. VapourSynth uses `matrix_in_s="709"` or `"2020ncl"` to match. |
| `--scd` | `0` | `0` | `0` | Scene change detection OFF (av1an handles scene cuts). |
| `--scm` | `0` | `0` | `0` | Screen content detection OFF (0: off, 1: on, 2: content adaptive). |
| `--keyint` | `0` | `0` | `0` | Keyframe interval OFF (av1an inserts keyframes). |
| `--lp` | `2` | `2` | `2` | Logical processors per av1an worker (matches `--set-thread-affinity 2`). |
| `--auto-tiling` | `1` | `1` | `1` | Automatically determine the number of tiles based on resolution. |
| `--tune` | `2` | `2` | `2` | SVT-AV1-Essential tune: 0=VQ, 1=PSNR, 2=SSIM, 3=IQ, 4=MS_SSIM. |
| `--progress` | `2` | `2` | `2` | Detailed progress output. |

*(Note: `--preset`, `--crf`, and `--tune` can be overridden when executing the script. Grain synthesis (`--film-grain`) is omitted by default unless `--grain` is provided. Dolby Vision sources are encoded as HDR10/HLG AV1; Av1an/SVT does not emit a DoVi RPU.)*

### SVT-AV1 via xav (SVT-AV1-Essential)

Parameters used for the `svt-av1` encoder when invoked via `xav` (as used in `xav_automation.py`):

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `--preset` | `1` (≤1080p) / `2` (>1080p) | Speed preset. Chosen from video height only (HDR 1080p stays preset 1 unless `--preset` is set). |
| `--tune` | `2` | SVT-AV1-Essential tune mode: 0=VQ, 1=PSNR, 2=SSIM, 3=IQ, 4=MS_SSIM. |

*(Note: `--preset` and `--tune` can be overridden when executing the script. CRF is not passed as a default parameter. Color primaries/transfer/matrix are not set on the xav command line.)*

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

`<calculated_workers>` is `(cpu_count // 2) - 1` (minimum 1), not a fixed worker count.

The VapourSynth script uses `ffms2.Source`, optional `std.CropAbs` when `--autocrop` is set, then `resize.Point` to `YUV420P10` with `matrix_in_s="709"` (SDR) or `"2020ncl"` (HDR).

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
