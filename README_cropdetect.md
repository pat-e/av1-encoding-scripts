# Advanced Crop Detection Script

This Python script (`cropdetect.py`) provides robust, parallelized, and intelligent crop detection for video files. It is much more than a simple wrapper for `ffmpeg-cropdetect`—it uses parallel processing, aspect ratio heuristics, luma verification, and bounding box logic to recommend safe crop values, even for complex videos with mixed aspect ratios.

## Key Features

- **Parallel Processing:** Analyzes video segments in parallel for speed and reliability.
- **Aspect Ratio Snapping:** Automatically snaps detected crops to known cinematic standards (16:9, 2.39:1, 1.85:1, 4:3, IMAX, etc.), correcting minor detection errors.
- **Mixed Aspect Ratio Handling:** Detects and safely handles videos with changing aspect ratios (e.g., IMAX scenes), recommending a bounding box crop that never cuts into image data.
- **Luma Verification:** Discards unreliable crop detections from very dark scenes using a second analysis pass.
- **Credits/Logo Filtering:** Ignores crops that only appear in the first/last 5% of the video, preventing opening logos or credits from affecting the result.
- **No Crop Recommendation:** If the video is overwhelmingly detected as not needing a crop, the script will confidently recommend leaving it as is.
- **User-Friendly Output:** Color-coded recommendations and warnings for easy review.
- **Safe for Automation:** The recommended crop is always the most outer cropable frame, so no image data is lost—even with variable crops.

## Prerequisites

- **Python 3**
- **FFmpeg**: Both `ffmpeg` and `ffprobe` must be installed and in your system's `PATH`.

## Installation

Just save the script as `cropdetect.py` and make it executable if needed.

## Usage

Run the script from your terminal, passing the path to the video file as an argument:

```bash
python cropdetect.py "path/to/your/video.mkv"
```

### Options

- `-n, --num_workers`: Number of parallel worker threads (default: half your CPU cores).
- `-sct, --significant_crop_threshold`: Percentage a crop must be present to be considered significant (default: 5.0).
- `-mc, --min_crop`: Minimum pixels to crop on any side for it to be considered a major crop (default: 10).
- `--debug`: Enable detailed debug logging.

## Example Output

### Confident Crop Recommendation

For a standard widescreen movie:

```
Recommended crop filter: -vf crop=3840:2080:0:40
```

### Mixed Aspect Ratio Warning

For a movie with changing aspect ratios:

```
WARNING: Potentially Mixed Aspect Ratios Detected!
Recommendation: Manually check the video before applying a single crop.
```

### No Crop Needed

For a video that is already perfectly formatted:

```
Recommendation: No crop needed.
```

## Integration with Other Scripts

This crop detection logic is now integrated into `anime_audio_encoder.py` and `tv_audio_encoder.py` via the `--autocrop` option. When enabled, those scripts will automatically detect and apply the safest crop to the UTVideo intermediate file, ensuring no image data is lost—even with variable crops.

## Notes

- The script is safe for automation and batch workflows.
- The recommended crop will never cut into the actual image, only remove black bars.
- For complex videos, a bounding box crop is calculated to contain all significant scenes.
- If no crop is needed, none will be applied.