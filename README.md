# AV1 Encoding Scripts

## Overview
This repository contains Python scripts for batch-processing MKV files to encode video to AV1 and audio to Opus. The scripts prioritize high-quality encoding, handle complex audio track downmixing, VFR (Variable Frame Rate) conversions, and offer features like automatic cropping and resumable encoding.

## Scripts Overview

*   **`aom_opus_encoder.py`**: Uses the `aom` encoder (specifically designed for the `aom-psy101` fork) via `av1an`. It is tuned for high perceptual quality with specific psychovisual parameters and film grain synthesis.
*   **`svt_opus_encoder.py`**: Uses the `svt-av1` encoder (specifically designed for the `SVT-AV1-Essential` fork) via `av1an`. It provides a good balance between encoding speed and quality, and allows customization of speed, quality, and film-grain presets from the command line.
*   **`hdr_svt_opus_encoder.py`**: A specialized script for 4K HDR movies using the `SVT-AV1-Essential` encoder. It is designed for pre-processed CFR inputs, preserves original surround sound audio without downmixing, and retains HDR metadata.

## Encoding Parameters Documentation

For detailed information on the specific FFmpeg arguments, audio downmixing logic, VFR-to-CFR conversion processes, and the special SVT-AV1/AomEnc parameters used by the standard scripts, please refer to the [`parameters.md`](parameters.md) file.

For the HDR-specific encoder parameters and settings used in `hdr_svt_opus_encoder.py`, please see [`parameters_hdr.md`](parameters_hdr.md).

## Prerequisites

Both scripts require several external tools to be installed and available in your system's `PATH`:

*   **ffmpeg** & **ffprobe**: For video/audio extraction, filtering (cropdetect), and loudnorm analysis.
*   **mkvtoolnix** (`mkvmerge`, `mkvpropedit`): For remuxing the final MKV file.
*   **opusenc** (opus-tools): For encoding audio tracks to the Opus codec.
*   **mediainfo**: For extracting detailed media information (especially frame rate details).
*   **av1an**: The core chunking encoder used to run multiple encode workers in parallel.
*   **HandBrakeCLI**: Used as a fallback/pre-processor to convert VFR (Variable Frame Rate) video to CFR (Constant Frame Rate) before the main encode.
*   **ffmsindex** (ffms2): For indexing the intermediate UTVideo file for Vapoursynth.
*   **Vapoursynth**: Required by `av1an` as the frame server via the generated `.vpy` scripts.
*   *(Specific to `aom_opus_encoder.py`)*: **aom-psy101** encoder. You must download the correct version from [Damian101's aom-psy101 GitLab](https://gitlab.com/damian101/aom-psy101).
*   *(Specific to `svt_opus_encoder.py`)*: **SVT-AV1-Essential** encoder. You must download the correct version from [nekotrix's SVT-AV1-Essential GitHub](https://github.com/nekotrix/SVT-AV1-Essential/).

## Features

*   **Automated Batch Processing**: Simply place your `.mkv` files in the same directory as the script. The script will process them one by one.
*   **Resumable Encoding**: Because it uses `av1an`, if an encode is interrupted, you can restart the script, and it will resume from where it left off.
*   **Audio Normalization and Downmixing**: 
    *   Extracts audio tracks to FLAC.
    *   Applies a 2-pass `loudnorm` normalization (Target: -23 LUFS, True Peak: -1 dB).
    *   Downmixes 5.1/7.1 surround sound to stereo (unless `--no-downmix` is specified).
    *   Encodes to Opus with bitrates automatically chosen based on the channel count (e.g., 128k for Stereo, 256k for 5.1).
    *   Directly remuxes existing `aac` or `opus` tracks without re-encoding.
    *   Preserves track languages, titles, and delays.
*   **VFR to CFR Conversion**: Detects Variable Frame Rate (VFR) media and automatically converts it to Constant Frame Rate (CFR) using HandBrakeCLI (virtually lossless `x264_10bit` CRF 0 intermediate) to prevent audio desync issues.
*   **Automatic Cropping**: Optional `--autocrop` flag detects black bars and determines the optimal cropping parameters before encoding.
*   **Organized Output**: 
    *   Completed files are moved to a `completed/` directory.
    *   Original files are moved to an `original/` directory.
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
*   `--no-downmix`: Preserve original audio channel layout (do not downmix 5.1/7.1 to stereo).
*   `--autocrop`: Automatically detect and crop black bars from the video.
*   `--grain <int>`: Set the `photon-noise` value for grain synthesis (default: 8).
*   `--crf <int>`: Set the constant quality level (`cq-level`) for video encoding (default: 25).

### `svt_opus_encoder.py`

```bash
svt_opus_encoder.py [options]
```

**Options:**
*   `--no-downmix`: Preserve original audio channel layout (do not downmix 5.1/7.1 to stereo).
*   `--autocrop`: Automatically detect and crop black bars from the video.
*   `--speed <str>`: Set the SVT-AV1 encoding speed preset (e.g., `slower`, `slow`, `medium`, `fast`, `faster`). Defaults to `slower`.
*   `--quality <str>`: Set the SVT-AV1 encoding quality preset (e.g., `lowest`, `low`, `medium`, `high`, `higher`). Defaults to `medium`.
*   `--grain <int>`: Set the `film-grain` value. Adjusts the film grain synthesis level. Defaults to 6.

### `hdr_svt_opus_encoder.py`

```bash
hdr_svt_opus_encoder.py [options]
```

**Options:**
*   `--speed <str>`: Set the SVT-AV1 encoding speed preset (e.g., `slower`, `slow`, `medium`, `fast`, `faster`). Defaults to `slower`.
*   `--quality <str>`: Set the SVT-AV1 encoding quality preset (e.g., `lowest`, `low`, `medium`, `high`, `higher`). Defaults to `medium`.
*   `--grain <int>`: Set the `film-grain` value. Adjusts the film grain synthesis level. Defaults to 12.

## Process Workflow

1.  **Preparation**: Scans for `.mkv` files and checks for required tools.
2.  **Analysis**: Examines video and audio tracks using `ffprobe` and `mediainfo`.
3.  **Video Processing**:
    *   Runs crop detection (if `--autocrop` is enabled).
    *   Converts VFR to CFR (if VFR is detected).
    *   Extracts an intermediate lossless video (`utvideo`).
    *   Encodes the video using `av1an`.
4.  **Audio Processing**:
    *   Remuxes AAC/Opus.
    *   Normalizes, downmixes (if applicable), and encodes other formats to Opus.
5.  **Muxing**: Combines the newly encoded video and audio tracks using `mkvmerge`, preserving synchronization delays, metadata, and languages.
6.  **Cleanup**: Moves files to respective folders (`completed/`, `original/`) and deletes temporary working files.

## Notes

- Encoding AV1 takes a significant amount of time and CPU resources. 
- Ensure you have sufficient disk space, as the scripts generate intermediate lossless `utvideo` files which can be very large depending on the length and resolution of the source media.

## License

This project is licensed under the MIT License - see the [`LICENSE.md`](LICENSE.md) file for details.
