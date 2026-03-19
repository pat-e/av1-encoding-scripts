#!/usr/bin/env python3

# Note: This script is configured to use a custom version of SVT-AV1 
# called "SVT-AV1-Essential" from https://github.com/nekotrix/SVT-AV1-Essential

import os
import sys
import subprocess
import shutil
import tempfile
import json
import re # Added for VFR frame rate parsing
from datetime import datetime
from pathlib import Path

REQUIRED_TOOLS = [
    "ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit",
    "opusenc", "mediainfo", "av1an", "HandBrakeCLI", "ffmsindex" # Added HandBrakeCLI and ffmsindex
]
DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs") # Directory for conversion logs

REMUX_CODECS = {"aac", "opus"}  # Using a set for efficient lookups

SVT_AV1_PARAMS = {
    "preset": 0,                       # Speed preset. Lower is slower and yields better compression efficiency.
    "crf": 30,                         # Constant Rate Factor (CRF). Lower is better quality.
    "film-grain": 6,                   # Film grain synthesis level. Adds artificial grain to preserve detail and prevent banding.
    "color-primaries": 1,              # BT.709 color primaries (Standard SDR).
    "transfer-characteristics": 1,     # BT.709 transfer characteristics (Standard SDR).
    "matrix-coefficients": 1,          # BT.709 matrix coefficients (Standard SDR).
    "scd": 0,                          # Scene change detection OFF (av1an handles scene cuts).
    "keyint": 0,                       # Keyframe interval OFF (av1an inserts keyframes).
    "lp": 2,                           # Logical Processors to use per av1an worker.
    "auto-tiling": 1,                  # Automatically determine the number of tiles based on resolution.
    "tune": 1,                         # 0 = VQ, 1 = PSNR, 2 = SSIM (SVT-AV1-Essential default recommended).
    "progress": 2,                     # Detailed progress output.
}

def check_tools():
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            print(f"Required tool '{tool}' not found in PATH.")
            sys.exit(1)

def run_cmd(cmd, capture_output=False, check=True):
    if capture_output:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check, text=True)
        return result.stdout
    else:
        subprocess.run(cmd, check=check)

def convert_audio_track(index, ch, lang, audio_temp_dir, source_file, should_downmix):
    audio_temp_path = Path(audio_temp_dir)
    temp_extracted = audio_temp_path / f"track_{index}_extracted.flac"
    temp_normalized = audio_temp_path / f"track_{index}_normalized.flac"
    final_opus = audio_temp_path / f"track_{index}_final.opus"

    print(f"    - Extracting Audio Track #{index} to FLAC...")
    ffmpeg_args = [
        "ffmpeg", "-v", "quiet", "-stats", "-y", "-i", str(source_file), "-map", f"0:{index}", "-map_metadata", "-1"
    ]
    if should_downmix and ch >= 6:
        if ch == 6:
            ffmpeg_args += ["-af", "pan=stereo|c0=c2+0.30*c0+0.30*c4|c1=c2+0.30*c1+0.30*c5"]
        elif ch == 8:
            ffmpeg_args += ["-af", "pan=stereo|c0=c2+0.30*c0+0.30*c4+0.30*c6|c1=c2+0.30*c1+0.30*c5+0.30*c7"]
        else: # Other multi-channel (e.g. 7ch, 10ch)
            ffmpeg_args += ["-ac", "2"]
    ffmpeg_args += ["-c:a", "flac", str(temp_extracted)]
    run_cmd(ffmpeg_args)

    print(f"    - Normalizing Audio Track #{index} with ffmpeg (loudnorm 2-pass)...")
    # First pass: Analyze the audio to get loudnorm stats
    # The stats are printed to stderr, so we must use subprocess.run directly to capture it.
    print("      - Pass 1: Analyzing...")
    result = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(temp_extracted), "-af", "loudnorm=I=-23:LRA=7:tp=-1:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    
    # Find the start of the JSON block in stderr and parse it.
    # This is more robust than slicing the last N lines.
    # We find the start and end of the JSON block to avoid parsing extra data.
    stderr_output = result.stderr
    json_start_index = stderr_output.find('{')
    if json_start_index == -1:
        raise ValueError("Could not find start of JSON block in ffmpeg output for loudnorm analysis.")

    brace_level = 0
    json_end_index = -1
    for i, char in enumerate(stderr_output[json_start_index:]):
        if char == '{':
            brace_level += 1
        elif char == '}':
            brace_level -= 1
            if brace_level == 0:
                json_end_index = json_start_index + i + 1
                break
    
    stats = json.loads(stderr_output[json_start_index:json_end_index])

    # Second pass: Apply the normalization using the stats from the first pass
    print("      - Pass 2: Applying normalization...")
    run_cmd([
        "ffmpeg", "-v", "quiet", "-stats", "-y", "-i", str(temp_extracted), "-af",
        f"loudnorm=I=-23:LRA=7:tp=-1:measured_i={stats['input_i']}:measured_lra={stats['input_lra']}:measured_tp={stats['input_tp']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}",
        "-c:a", "flac", str(temp_normalized)
    ])

    # Set bitrate based on the final channel count of the Opus file.
    # If we are downmixing, the result is stereo.
    # If not, the result has the original channel count.
    is_being_downmixed = should_downmix and ch >= 6

    if is_being_downmixed:
        # Downmixing from 5.1 or 7.1 results in a stereo track.
        bitrate = "128k"
    else:
        # Not downmixing (or source is already stereo or less).
        # Base bitrate on the source channel count.
        if ch == 1:      # Mono
            bitrate = "64k"
        elif ch == 2:    # Stereo
            bitrate = "128k"
        elif ch == 6:    # 5.1 Surround
            bitrate = "256k"
        elif ch == 8:    # 7.1 Surround
            bitrate = "384k"
        else:            # Other layouts
            bitrate = "192k" # A sensible default for other/uncommon layouts.

    print(f"    - Encoding Audio Track #{index} to Opus at {bitrate}...")
    run_cmd([
        "opusenc", "--vbr", "--bitrate", bitrate, str(temp_normalized), str(final_opus)
    ])
    return final_opus

def convert_video(source_file_base, source_file_full, is_vfr, target_cfr_fps_for_handbrake, autocrop_filter=None):
    print("  --- Starting Video Processing ---")
    # source_file_base is file_path.stem (e.g., "my.anime.episode.01")
    vpy_file = Path(f"{source_file_base}.vpy")
    ut_video_file = Path(f"{source_file_base}.ut.mkv")
    encoded_video_file = Path(f"temp-{source_file_base}.mkv")
    handbrake_cfr_intermediate_file = None # To store path of HandBrake output if created

    current_input_for_utvideo = Path(source_file_full)

    if is_vfr and target_cfr_fps_for_handbrake:
        print(f"    - Source is VFR. Converting to CFR ({target_cfr_fps_for_handbrake}) with HandBrakeCLI...")
        handbrake_cfr_intermediate_file = Path(f"{source_file_base}.cfr_temp.mkv")
        handbrake_args = [
            "HandBrakeCLI", 
            "--input", str(source_file_full), 
            "--output", str(handbrake_cfr_intermediate_file),
            "--cfr", 
            "--rate", str(target_cfr_fps_for_handbrake),
            "--encoder", "x264_10bit", # Changed to x264_10bit for 10-bit CFR intermediate
            "--quality", "0", # CRF 0 for x264 is often considered visually lossless, or near-lossless
            "--encoder-preset", "superfast", # Use a fast preset for quicker processing
            "--encoder-tune", "fastdecode", # Added tune for faster decoding
            "--audio", "none", 
            "--subtitle", "none",
            "--crop-mode", "none" # Disable auto-cropping
        ]
        print(f"    - Running HandBrakeCLI: {' '.join(handbrake_args)}")
        try:
            run_cmd(handbrake_args)
            if handbrake_cfr_intermediate_file.exists() and handbrake_cfr_intermediate_file.stat().st_size > 0:
                print(f"    - HandBrake VFR to CFR conversion successful: {handbrake_cfr_intermediate_file}")
                current_input_for_utvideo = handbrake_cfr_intermediate_file
            else:
                print(f"    - Warning: HandBrakeCLI VFR-to-CFR conversion failed or produced an empty file. Proceeding with original source for UTVideo.")
                handbrake_cfr_intermediate_file = None # Ensure it's None if failed
        except subprocess.CalledProcessError as e:
            print(f"    - Error during HandBrakeCLI execution: {e}")
            print(f"    - Proceeding with original source for UTVideo.")
            handbrake_cfr_intermediate_file = None # Ensure it's None if failed


    print("    - Creating UTVideo intermediate file (overwriting if exists)...")
    # Check if source is already UTVideo
    ffprobe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
        str(current_input_for_utvideo) # Use current input, which might be HandBrake output
    ]
    source_codec = run_cmd(ffprobe_cmd, capture_output=True, check=True).strip()

    video_codec_args = ["-c:v", "utvideo"]
    if source_codec == "utvideo" and current_input_for_utvideo == Path(source_file_full): # Only copy if original was UTVideo
        print("    - Source is already UTVideo. Copying video stream...")
        video_codec_args = ["-c:v", "copy"]

    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-v", "quiet", "-stats", "-y", "-i", str(current_input_for_utvideo),
        "-map", "0:v:0", "-map_metadata", "-1", "-map_chapters", "-1", "-an", "-sn", "-dn",
    ]
    if autocrop_filter:
        ffmpeg_args += ["-vf", autocrop_filter]
    ffmpeg_args += video_codec_args + [str(ut_video_file)]
    run_cmd(ffmpeg_args)

    print("    - Indexing UTVideo file with ffmsindex for VapourSynth...")
    ffmsindex_args = ["ffmsindex", "-f", str(ut_video_file)]
    run_cmd(ffmsindex_args)

    ut_video_full_path = os.path.abspath(ut_video_file)
    vpy_script_content = f"""import vapoursynth as vs
core = vs.core
core.num_threads = 4
clip = core.ffms2.Source(source=r'''{ut_video_full_path}''')
clip = core.resize.Point(clip, format=vs.YUV420P10, matrix_in_s="709") # type: ignore
clip.set_output()
"""
    with vpy_file.open("w", encoding="utf-8") as f:
        f.write(vpy_script_content)

    print("    - Starting AV1 encode with av1an (this will take a long time)...")
    total_cores = os.cpu_count() or 4 # Fallback if cpu_count is None
    workers = max(1, (total_cores // 2) - 1) # Half the cores minus one, with a minimum of 1 worker.
    print(f"    - Using {workers} workers for av1an (Total Cores: {total_cores}, Logic: (Cores/2)-1).")

    # Create the parameter string for av1an's -v option, which expects a single string.
    av1an_video_params_str = " ".join([f"--{key} {value}" for key, value in SVT_AV1_PARAMS.items()])
    print(f"    - Using SVT-AV1 parameters: {av1an_video_params_str}")

    av1an_enc_args = [
        "av1an", "-i", str(vpy_file), "-o", str(encoded_video_file), "-n",
        "-e", "svt-av1", "--resume", "--sc-pix-format", "yuv420p", "-c", "mkvmerge",
        "--set-thread-affinity", "2", "--pix-format", "yuv420p10le", "--force", "--no-defaults",
        "-w", str(workers),
        "-v", av1an_video_params_str
    ]
    run_cmd(av1an_enc_args)
    print("  --- Finished Video Processing ---")
    return encoded_video_file, handbrake_cfr_intermediate_file

def is_ffmpeg_decodable(file_path):
    """Quickly check if ffmpeg can decode the input file."""
    try:
        # Try to decode a short segment of the first audio stream
        subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(file_path), "-map", "0:a:0", "-t", "1", "-f", "null", "-"
        ], check=True)
        return True
    except subprocess.CalledProcessError:
        return False

# --- CROPDETECT LOGIC FROM cropdetect.py ---
import argparse as _argparse_cropdetect
import multiprocessing as _multiprocessing_cropdetect
from collections import Counter as _Counter_cropdetect

COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_RESET = "\033[0m"

KNOWN_ASPECT_RATIOS = [
    {"name": "HDTV (16:9)", "ratio": 16/9},
    {"name": "Widescreen (Scope)", "ratio": 2.39},
    {"name": "Widescreen (Flat)", "ratio": 1.85},
    {"name": "IMAX Digital (1.90:1)", "ratio": 1.90},
    {"name": "Fullscreen (4:3)", "ratio": 4/3},
    {"name": "IMAX 70mm (1.43:1)", "ratio": 1.43},
]

def _check_prerequisites_cropdetect():
    for tool in ['ffmpeg', 'ffprobe']:
        if not shutil.which(tool):
            print(f"Error: '{tool}' command not found. Is it installed and in your PATH?")
            return False
    return True

def _analyze_segment_cropdetect(task_args):
    seek_time, input_file, width, height = task_args
    ffmpeg_args = [
        'ffmpeg', '-hide_banner',
        '-ss', str(seek_time),
        '-i', input_file, '-t', '1', '-vf', 'cropdetect',
        '-f', 'null', '-'
    ]
    result = subprocess.run(ffmpeg_args, capture_output=True, text=True, encoding='utf-8')
    if result.returncode != 0:
        return []
    crop_detections = re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', result.stderr)
    significant_crops = []
    for w_str, h_str, x_str, y_str in crop_detections:
        w, h, x, y = map(int, [w_str, h_str, x_str, y_str])
        significant_crops.append((f"crop={w}:{h}:{x}:{y}", seek_time))
    return significant_crops

def _snap_to_known_ar_cropdetect(w, h, x, y, video_w, video_h, tolerance=0.03):
    if h == 0: return f"crop={w}:{h}:{x}:{y}", None
    detected_ratio = w / h
    best_match = None
    smallest_diff = float('inf')
    for ar in KNOWN_ASPECT_RATIOS:
        diff = abs(detected_ratio - ar['ratio'])
        if diff < smallest_diff:
            smallest_diff = diff
            best_match = ar
    if not best_match or (smallest_diff / best_match['ratio']) >= tolerance:
        return f"crop={w}:{h}:{x}:{y}", None
    if abs(w - video_w) < 16:
        new_h = round(video_w / best_match['ratio'])
        if new_h % 8 != 0:
            new_h = new_h + (8 - (new_h % 8))
        new_h = min(new_h, video_h)
        new_y = round((video_h - new_h) / 2)
        if new_y % 2 != 0:
            new_y -= 1
        new_y = max(0, new_y)
        return f"crop={video_w}:{new_h}:0:{new_y}", best_match['name']
    if abs(h - video_h) < 16:
        new_w = round(video_h * best_match['ratio'])
        if new_w % 8 != 0:
            new_w = new_w + (8 - (new_w % 8))
        new_w = min(new_w, video_w)
        new_x = round((video_w - new_w) / 2)
        if new_x % 2 != 0:
            new_x -= 1
        new_x = max(0, new_x)
        return f"crop={new_w}:{video_h}:{new_x}:0", best_match['name']
    return f"crop={w}:{h}:{x}:{y}", None

def _cluster_crop_values_cropdetect(crop_counts, tolerance=8):
    clusters = []
    temp_counts = crop_counts.copy()
    while temp_counts:
        center_str, _ = temp_counts.most_common(1)[0]
        try:
            _, values = center_str.split('=');
            cw, ch, cx, cy = map(int, values.split(':'))
        except (ValueError, IndexError):
            del temp_counts[center_str]
            continue
        cluster_total_count = 0
        crops_to_remove = []
        for crop_str, count in temp_counts.items():
            try:
                _, values = crop_str.split('=');
                w, h, x, y = map(int, values.split(':'))
                if abs(x - cx) <= tolerance and abs(y - cy) <= tolerance:
                    cluster_total_count += count
                    crops_to_remove.append(crop_str)
            except (ValueError, IndexError):
                continue
        if cluster_total_count > 0:
            clusters.append({'center': center_str, 'count': cluster_total_count})
        for crop_str in crops_to_remove:
            del temp_counts[crop_str]
    clusters.sort(key=lambda c: c['count'], reverse=True)
    return clusters

def _parse_crop_string_cropdetect(crop_str):
    try:
        _, values = crop_str.split('=');
        w, h, x, y = map(int, values.split(':'))
        return {'w': w, 'h': h, 'x': x, 'y': y}
    except (ValueError, IndexError):
        return None

def _calculate_bounding_box_cropdetect(crop_keys):
    min_x = min_w = min_y = min_h = float('inf')
    max_x = max_w = max_y = max_h = float('-inf')
    for key in crop_keys:
        parsed = _parse_crop_string_cropdetect(key)
        if not parsed:
            continue
        w, h, x, y = parsed['w'], parsed['h'], parsed['x'], parsed['y']
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x + w)
        max_y = max(max_y, y + h)
        min_w = min(min_w, w)
        min_h = min(min_h, h)
        max_w = max(max_w, w)
        max_h = max(max_h, h)
    if (max_x - min_x) <= 2 and (max_y - min_y) <= 2:
        return None
    bounding_crop = f"crop={max_x - min_x}:{max_y - min_y}:{min_x}:{min_y}"
    return bounding_crop

def _analyze_video_cropdetect(input_file, duration, width, height, num_workers, significant_crop_threshold, min_crop, debug=False):
    num_tasks = num_workers * 4
    segment_duration = max(1, duration // num_tasks)
    tasks = [(i * segment_duration, input_file, width, height) for i in range(num_tasks)]
    crop_results = []
    with _multiprocessing_cropdetect.Pool(processes=num_workers) as pool:
        results_iterator = pool.imap_unordered(_analyze_segment_cropdetect, tasks)
        for result in results_iterator:
            crop_results.append(result)
    all_crops_with_ts = [crop for sublist in crop_results for crop in sublist]
    all_crop_strings = [item[0] for item in all_crops_with_ts]
    if not all_crop_strings:
        return None
    crop_counts = _Counter_cropdetect(all_crop_strings)
    clusters = _cluster_crop_values_cropdetect(crop_counts)
    total_detections = sum(c['count'] for c in clusters)
    significant_clusters = []
    for cluster in clusters:
        percentage = (cluster['count'] / total_detections) * 100
        if percentage >= significant_crop_threshold:
            significant_clusters.append(cluster)
    for cluster in significant_clusters:
        parsed_crop = _parse_crop_string_cropdetect(cluster['center'])
        if parsed_crop:
            _, ar_label = _snap_to_known_ar_cropdetect(
                parsed_crop['w'], parsed_crop['h'], parsed_crop['x'], parsed_crop['y'], width, height
            )
            cluster['ar_label'] = ar_label
        else:
            cluster['ar_label'] = None
    if not significant_clusters:
        return None
    elif len(significant_clusters) == 1:
        dominant_cluster = significant_clusters[0]
        parsed_crop = _parse_crop_string_cropdetect(dominant_cluster['center'])
        snapped_crop, ar_label = _snap_to_known_ar_cropdetect(
            parsed_crop['w'], parsed_crop['h'], parsed_crop['x'], parsed_crop['y'], width, height
        )
        parsed_snapped = _parse_crop_string_cropdetect(snapped_crop)
        if parsed_snapped and parsed_snapped['w'] == width and parsed_snapped['h'] == height:
            return None
        else:
            return snapped_crop
    else:
        crop_keys = [c['center'] for c in significant_clusters]
        bounding_box_crop = _calculate_bounding_box_cropdetect(crop_keys)
        if bounding_box_crop:
            parsed_bb = _parse_crop_string_cropdetect(bounding_box_crop)
            snapped_crop, ar_label = _snap_to_known_ar_cropdetect(
                parsed_bb['w'], parsed_bb['h'], parsed_bb['x'], parsed_bb['y'], width, height
            )
            parsed_snapped = _parse_crop_string_cropdetect(snapped_crop)
            if parsed_snapped and parsed_snapped['w'] == width and parsed_snapped['h'] == height:
                return None
            else:
                return snapped_crop
        else:
            return None

def detect_autocrop_filter(input_file, significant_crop_threshold=5.0, min_crop=10, debug=False):
    if not _check_prerequisites_cropdetect():
        return None
    try:
        probe_duration_args = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
            input_file
        ]
        duration_str = subprocess.check_output(probe_duration_args, stderr=subprocess.STDOUT, text=True)
        duration = int(float(duration_str))
        probe_res_args = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v',
            '-show_entries', 'stream=width,height,disposition',
            '-of', 'json',
            input_file
        ]
        probe_output = subprocess.check_output(probe_res_args, stderr=subprocess.STDOUT, text=True)
        streams_data = json.loads(probe_output)
        video_stream = None
        for stream in streams_data.get('streams', []):
            if stream.get('disposition', {}).get('attached_pic', 0) == 0:
                video_stream = stream
                break
        if not video_stream or 'width' not in video_stream or 'height' not in video_stream:
            return None
        width = int(video_stream['width'])
        height = int(video_stream['height'])
    except Exception:
        return None
    return _analyze_video_cropdetect(input_file, duration, width, height, max(1, os.cpu_count() // 2), significant_crop_threshold, min_crop, debug)

def main(no_downmix=False, autocrop=False, preset=None, crf=None, grain=None):
    check_tools()

    # Override default SVT-AV1 params if provided via command line
    if preset is not None:
        SVT_AV1_PARAMS["preset"] = preset
    if crf is not None:
        SVT_AV1_PARAMS["crf"] = crf
    if grain is not None:
        SVT_AV1_PARAMS["film-grain"] = grain

    current_dir = Path(".")
    files_to_process = sorted(
        f for f in current_dir.glob("*.mkv")
        if not (f.name.endswith(".ut.mkv") or f.name.startswith("temp-") or f.name.startswith("output-") or f.name.endswith(".cfr_temp.mkv"))
    )
    if not files_to_process:
        print("No MKV files found to process. Exiting.")
        return
    DIR_COMPLETED.mkdir(exist_ok=True, parents=True)
    DIR_ORIGINAL.mkdir(exist_ok=True, parents=True)
    DIR_CONV_LOGS.mkdir(exist_ok=True, parents=True)
    while True:
        files_to_process = sorted(
            f for f in current_dir.glob("*.mkv")
            if not (f.name.endswith(".ut.mkv") or f.name.startswith("temp-") or f.name.startswith("output-") or f.name.endswith(".cfr_temp.mkv"))
        )
        if not files_to_process:
            print("No more .mkv files found to process in the current directory. The script will now exit.")
            break
        file_path = files_to_process[0]
        if not is_ffmpeg_decodable(file_path):
            print(f"ERROR: ffmpeg cannot decode '{file_path.name}'. Skipping this file.", file=sys.stderr)
            shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
            continue
        print("-" * shutil.get_terminal_size(fallback=(80, 24)).columns)
        log_file_name = f"{file_path.stem}.log"
        log_file_path = DIR_CONV_LOGS / log_file_name
        original_stdout_console = sys.stdout
        original_stderr_console = sys.stderr
        print(f"Processing: {file_path.name}", file=original_stdout_console)
        print(f"Logging output to: {log_file_path}", file=original_stdout_console)
        log_file_handle = None
        processing_error_occurred = False
        date_for_runtime_calc = datetime.now()
        try:
            log_file_handle = open(log_file_path, 'w', encoding='utf-8')
            sys.stdout = log_file_handle
            sys.stderr = log_file_handle
            print(f"STARTING LOG FOR: {file_path.name}")
            print(f"Processing started at: {date_for_runtime_calc}")
            print(f"Full input file path: {file_path.resolve()}")
            print("-" * shutil.get_terminal_size(fallback=(80, 24)).columns)
            input_file_abs = file_path.resolve()
            intermediate_output_file = current_dir / f"output-{file_path.name}"
            audio_temp_dir = None
            handbrake_intermediate_for_cleanup = None
            try:
                audio_temp_dir = tempfile.mkdtemp(prefix="anime_audio_")
                print(f"Audio temporary directory created at: {audio_temp_dir}")
                print(f"Analyzing file: {input_file_abs}")
                ffprobe_info_json = run_cmd([
                    "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(input_file_abs)
                ], capture_output=True)
                ffprobe_info = json.loads(ffprobe_info_json)
                mkvmerge_info_json = run_cmd([
                    "mkvmerge", "-J", str(input_file_abs)
                ], capture_output=True)
                mkv_info = json.loads(mkvmerge_info_json)
                mediainfo_json = run_cmd([
                    "mediainfo", "--Output=JSON", "-f", str(input_file_abs)
                ], capture_output=True)
                media_info = json.loads(mediainfo_json)
                is_vfr = False
                target_cfr_fps_for_handbrake = None
                video_track_info = None
                if media_info.get("media") and media_info["media"].get("track"):
                    for track in media_info["media"]["track"]:
                        if track.get("@type") == "Video":
                            video_track_info = track
                            break
                if video_track_info:
                    frame_rate_mode = video_track_info.get("FrameRate_Mode")
                    if frame_rate_mode and frame_rate_mode.upper() in ["VFR", "VARIABLE"]:
                        is_vfr = True
                        print(f"    - Detected VFR based on MediaInfo FrameRate_Mode: {frame_rate_mode}")
                        original_fps_str = video_track_info.get("FrameRate_Original_String")
                        if original_fps_str:
                            match = re.search(r'\((\d+/\d+)\)', original_fps_str)
                            if match:
                                target_cfr_fps_for_handbrake = match.group(1)
                            else:
                                target_cfr_fps_for_handbrake = video_track_info.get("FrameRate_Original")
                        if not target_cfr_fps_for_handbrake:
                            target_cfr_fps_for_handbrake = video_track_info.get("FrameRate_Original")
                        if not target_cfr_fps_for_handbrake:
                            target_cfr_fps_for_handbrake = video_track_info.get("FrameRate")
                            if target_cfr_fps_for_handbrake:
                                print(f"    - Using MediaInfo FrameRate ({target_cfr_fps_for_handbrake}) as fallback for HandBrake target FPS.")
                        if target_cfr_fps_for_handbrake:
                            print(f"    - Target CFR for HandBrake: {target_cfr_fps_for_handbrake}")
                            if isinstance(target_cfr_fps_for_handbrake, str) and "/" in target_cfr_fps_for_handbrake:
                                try:
                                    num, den = map(float, target_cfr_fps_for_handbrake.split('/'))
                                    target_cfr_fps_for_handbrake = f"{num / den:.3f}"
                                    print(f"    - Converted fractional FPS to decimal for HandBrake: {target_cfr_fps_for_handbrake}")
                                except ValueError:
                                    print(f"    - Warning: Could not parse fractional FPS '{target_cfr_fps_for_handbrake}'. HandBrakeCLI might fail.")
                                    is_vfr = False
                        else:
                            print("    - Warning: VFR detected, but could not determine target CFR from MediaInfo. Will attempt standard UTVideo conversion without HandBrake.")
                            is_vfr = False
                    else:
                        print(f"    - Video appears to be CFR or FrameRate_Mode not specified as VFR/Variable by MediaInfo.")
                autocrop_filter = None
                if autocrop:
                    print("--- Running autocrop detection ---")
                    autocrop_filter = detect_autocrop_filter(str(input_file_abs))
                    if autocrop_filter:
                        print(f"    - Autocrop filter detected: {autocrop_filter}")
                    else:
                        print("    - No crop needed or detected.")
                encoded_video_file, handbrake_intermediate_for_cleanup = convert_video(
                    file_path.stem, str(input_file_abs), is_vfr, target_cfr_fps_for_handbrake, autocrop_filter=autocrop_filter
                )

                print("--- Starting Audio Processing ---")
                processed_audio_files = []
                audio_tracks_to_remux = []
                audio_streams = [s for s in ffprobe_info.get("streams", []) if s.get("codec_type") == "audio"]

                # Build mkvmerge track mapping by track ID
                mkv_audio_tracks = {t["id"]: t for t in mkv_info.get("tracks", []) if t.get("type") == "audio"}

                # Build mediainfo track mapping by StreamOrder
                media_tracks_data = media_info.get("media", {}).get("track", [])
                mediainfo_audio_tracks = {int(t.get("StreamOrder", -1)): t for t in media_tracks_data if t.get("@type") == "Audio"}

                for stream in audio_streams:
                    stream_index = stream["index"]
                    codec = stream.get("codec_name")
                    channels = stream.get("channels", 2)
                    language = stream.get("tags", {}).get("language", "und")

                    # Find mkvmerge track by matching ffprobe stream index to mkvmerge track's 'properties'->'stream_id'
                    mkv_track = None
                    for t in mkv_info.get("tracks", []):
                        if t.get("type") == "audio" and t.get("properties", {}).get("stream_id") == stream_index:
                            mkv_track = t
                            break
                    if not mkv_track:
                        # Fallback: try by position
                        mkv_track = mkv_info.get("tracks", [])[stream_index] if stream_index < len(mkv_info.get("tracks", [])) else {}

                    track_id = mkv_track.get("id", -1)
                    track_title = mkv_track.get("properties", {}).get("track_name", "")

                    # Find mediainfo track by StreamOrder
                    audio_track_info = mediainfo_audio_tracks.get(stream_index)
                    track_delay = 0
                    delay_raw = audio_track_info.get("Video_Delay") if audio_track_info else None
                    if delay_raw is not None:
                        try:
                            delay_val = float(delay_raw)
                            # If the value is a float < 1, it's seconds, so convert to ms.
                            if delay_val < 1:
                                track_delay = int(round(delay_val * 1000))
                            else:
                                track_delay = int(round(delay_val))
                        except Exception:
                            track_delay = 0

                    print(f"Processing Audio Stream #{stream_index} (TID: {track_id}, Codec: {codec}, Channels: {channels})")
                    if codec in REMUX_CODECS:
                        audio_tracks_to_remux.append(str(track_id))
                    else:
                        # Convert any codec that is not in REMUX_CODECS
                        opus_file = convert_audio_track(
                            stream_index, channels, language, audio_temp_dir, str(input_file_abs), not no_downmix
                        )
                        processed_audio_files.append({
                            "Path": opus_file,
                            "Language": language,
                            "Title": track_title,
                            "Delay": track_delay
                        })

                print("--- Finished Audio Processing ---")

                # Final mux
                print("Assembling final file with mkvmerge...")
                mkvmerge_args = ["mkvmerge", "-o", str(intermediate_output_file), str(encoded_video_file)]
                for file_info in processed_audio_files:
                    sync_switch = ["--sync", f"0:{file_info['Delay']}"] if file_info["Delay"] else []
                    mkvmerge_args += [
                        "--language", f"0:{file_info['Language']}",
                        "--track-name", f"0:{file_info['Title']}"
                    ] + sync_switch + [str(file_info["Path"])]

                source_copy_args = ["--no-video"]
                if audio_tracks_to_remux:
                    source_copy_args += ["--audio-tracks", ",".join(audio_tracks_to_remux)]
                else:
                    source_copy_args += ["--no-audio"]
                mkvmerge_args += source_copy_args + [str(input_file_abs)]
                run_cmd(mkvmerge_args)

                # Move files
                print("Moving files to final destinations...")
                shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
                shutil.move(str(intermediate_output_file), DIR_COMPLETED / file_path.name)

                print("Cleaning up persistent video temporary files (after successful processing)...")
                video_temp_files_on_success = [
                    current_dir / f"{file_path.stem}.vpy",
                    current_dir / f"{file_path.stem}.ut.mkv",
                    current_dir / f"temp-{file_path.stem}.mkv", # This is encoded_video_file
                    current_dir / f"{file_path.stem}.ut.mkv.lwi", 
                    current_dir / f"{file_path.stem}.ut.mkv.ffindex",
                ]
                if handbrake_intermediate_for_cleanup and handbrake_intermediate_for_cleanup.exists():
                    video_temp_files_on_success.append(handbrake_intermediate_for_cleanup)
                
                for temp_vid_file in video_temp_files_on_success:
                    if temp_vid_file.exists():
                        print(f"    Deleting: {temp_vid_file}")
                        temp_vid_file.unlink(missing_ok=True)
                    else:
                        print(f"    Skipping (not found): {temp_vid_file}")

            except Exception as e:
                print(f"ERROR: An error occurred while processing '{file_path.name}': {e}", file=sys.stderr) # Goes to log
                original_stderr_console.write(f"ERROR during processing of '{file_path.name}': {e}\nSee log '{log_file_path}' for details.\n")
                processing_error_occurred = True
            finally:
                # This is the original 'finally' block. Its prints go to the log file.
                print("--- Starting Universal Cleanup (for this file) ---")
                print("  - Cleaning up disposable audio temporary directory...")
                if audio_temp_dir and Path(audio_temp_dir).exists():
                    shutil.rmtree(audio_temp_dir, ignore_errors=True)
                    print(f"    - Deleted audio temp dir: {audio_temp_dir}")
                elif audio_temp_dir: # Was created but now not found
                    print(f"    - Audio temp dir not found or already cleaned: {audio_temp_dir}")
                else: # Was never created
                    print(f"    - Audio temp dir was not created.")
                
                print("  - Cleaning up intermediate output file (if it wasn't moved on success)...")
                if intermediate_output_file.exists(): # Check if it still exists (e.g. error before move)
                    if processing_error_occurred:
                        print(f"    - WARNING: Processing error occurred. Intermediate output file '{intermediate_output_file}' is being preserved at its original path for inspection.")
                    else:
                        # No processing error, so it should have been moved.
                        # If it's still here, it's unexpected but we'll clean it up.
                        print(f"    - INFO: Intermediate output file '{intermediate_output_file}' found at original path despite no errors (expected to be moved). Cleaning up.")
                        intermediate_output_file.unlink(missing_ok=True) # Only unlink if no error and it exists
                        print(f"    - Deleted intermediate output file from original path: {intermediate_output_file}")
                else:
                    # File does not exist at original path
                    if not processing_error_occurred:
                        print(f"    - Intermediate output file successfully moved (not found at original path, as expected): {intermediate_output_file}")
                    else:
                        print(f"    - Processing error occurred, and intermediate output file '{intermediate_output_file}' not found at original path (likely not created or cleaned by another step).")
            # --- End of original per-file processing block ---

            print(f"FINISHED LOG FOR: {file_path.name}")
            # --- End of log-specific messages ---

        finally: # Outer finally for restoring stdout/stderr and closing log file
            runtime = datetime.now() - date_for_runtime_calc
            runtime_str = str(runtime).split('.')[0]
            
            # This print goes to the log file, as stdout is not yet restored.
            print(f"\nTotal runtime for this file: {runtime_str}")
            
            if sys.stdout != original_stdout_console:
                sys.stdout = original_stdout_console
            if sys.stderr != original_stderr_console:
                sys.stderr = original_stderr_console
            if log_file_handle:
                log_file_handle.close()
            
            # Announce to console (original stdout/stderr) that this file is done
            if processing_error_occurred:
                original_stderr_console.write(f"File: {file_path.name}\n")
                original_stderr_console.write(f"Log: {log_file_path}\n")
                original_stderr_console.write(f"Runtime: {runtime_str}\n")
            else:
                original_stdout_console.write(f"File: {file_path.name}\n")
                original_stdout_console.write(f"Log: {log_file_path}\n")
                original_stdout_console.write(f"Runtime: {runtime_str}\n")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Batch-process MKV files with resumable video encoding, audio downmixing, per-file logging, and optional autocrop.")
    parser.add_argument("--no-downmix", action="store_true", help="Preserve original audio channel layout.")
    parser.add_argument("--autocrop", action="store_true", help="Automatically detect and crop black bars from video using cropdetect.")
    parser.add_argument("--preset", type=int, help=f"Set the encoding preset. Lower is slower/better compression. (default: {SVT_AV1_PARAMS['preset']})")
    parser.add_argument("--crf", type=int, help=f"Set the Constant Rate Factor (CRF). Lower is better quality. (default: {SVT_AV1_PARAMS['crf']})")
    parser.add_argument("--grain", type=int, help=f"Set the film-grain value (number). Adjusts the film grain synthesis level. (default: {SVT_AV1_PARAMS['film-grain']})")
    args = parser.parse_args()
    main(no_downmix=args.no_downmix, autocrop=args.autocrop, preset=args.preset, crf=args.crf, grain=args.grain)
