#!/usr/bin/env python3

import argparse
import subprocess
import sys
import os
import re
from collections import Counter
import shutil
import multiprocessing
import json

# ANSI color codes
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_RESET = "\033[0m"

def check_prerequisites():
    """Checks if required tools are available."""
    print("--- Prerequisite Check ---")
    all_found = True
    for tool in ['ffmpeg', 'ffprobe']:
        if not shutil.which(tool):
            print(f"Error: '{tool}' command not found. Is it installed and in your PATH?")
            all_found = False
    if not all_found:
        sys.exit(1)
    print("All required tools found.")

def analyze_segment(task_args):
    """Function to be run by each worker process. Analyzes one video segment."""
    seek_time, input_file, width, height = task_args
    
    ffmpeg_args = [
        'ffmpeg', '-hide_banner',
        '-ss', str(seek_time),
        '-i', input_file, '-t', '1', '-vf', 'cropdetect',
        '-f', 'null', '-'
    ]

    result = subprocess.run(ffmpeg_args, capture_output=True, text=True, encoding='utf-8')
    
    if result.returncode != 0:
        return [] # Return empty list on error

    crop_detections = re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', result.stderr)
    
    significant_crops = []
    for w_str, h_str, x_str, y_str in crop_detections:
        w, h, x, y = map(int, [w_str, h_str, x_str, y_str])
        
        # Return the crop string along with the timestamp it was found at
        significant_crops.append((f"crop={w}:{h}:{x}:{y}", seek_time))
        
    return significant_crops

def get_frame_luma(input_file, seek_time):
    """Analyzes a single frame at a given timestamp to get its average luma."""
    ffmpeg_args = [
        'ffmpeg', '-hide_banner',
        '-ss', str(seek_time),
        '-i', input_file,
        '-t', '1',
        '-vf', 'signalstats',
        '-f', 'null', '-'
    ]
    result = subprocess.run(ffmpeg_args, capture_output=True, text=True, encoding='utf-8')
    
    if result.returncode != 0:
        return None # Error during analysis

    # Find the average luma (YAVG) for the frame
    match = re.search(r'YAVG:([0-9.]+)', result.stderr)
    if match:
        return float(match.group(1))
    
    return None

def check_luma_for_group(task_args):
    """Worker function to check the luma for a single group."""
    group_key, sample_ts, input_file, luma_threshold = task_args
    luma = get_frame_luma(input_file, sample_ts)
    is_bright = luma is not None and luma >= luma_threshold
    return (group_key, is_bright)

KNOWN_ASPECT_RATIOS = [
    {"name": "HDTV (16:9)", "ratio": 16/9},
    {"name": "Widescreen (Scope)", "ratio": 2.39},
    {"name": "Widescreen (Flat)", "ratio": 1.85},
    {"name": "IMAX Digital (1.90:1)", "ratio": 1.90},
    {"name": "Fullscreen (4:3)", "ratio": 4/3},
    {"name": "IMAX 70mm (1.43:1)", "ratio": 1.43},
]

def snap_to_known_ar(w, h, x, y, video_w, video_h, tolerance=0.03):
    """Snaps a crop rectangle to the nearest standard aspect ratio if it's close enough."""
    if h == 0: return f"crop={w}:{h}:{x}:{y}", None
    detected_ratio = w / h
    
    best_match = None
    smallest_diff = float('inf')

    for ar in KNOWN_ASPECT_RATIOS:
        diff = abs(detected_ratio - ar['ratio'])
        if diff < smallest_diff:
            smallest_diff = diff
            best_match = ar

    # If the best match is not within the tolerance, return the original
    if not best_match or (smallest_diff / best_match['ratio']) >= tolerance:
        return f"crop={w}:{h}:{x}:{y}", None

    # Match found, now snap the dimensions.
    # Heuristic: if width is close to full video width, it's letterboxed.
    if abs(w - video_w) < 16:
        new_h = round(video_w / best_match['ratio'])
        
        # Round height up to the nearest multiple of 8 for cleaner dimensions and less aggressive cropping.
        if new_h % 8 != 0:
            new_h = new_h + (8 - (new_h % 8))

        new_y = round((video_h - new_h) / 2)
        # Ensure y offset is an even number for compatibility.
        if new_y % 2 != 0:
            new_y -= 1
            
        return f"crop={video_w}:{new_h}:0:{new_y}", best_match['name']
    
    # Heuristic: if height is close to full video height, it's pillarboxed.
    if abs(h - video_h) < 16:
        new_w = round(video_h * best_match['ratio'])

        # Round width up to the nearest multiple of 8.
        if new_w % 8 != 0:
            new_w = new_w + (8 - (new_w % 8))

        new_x = round((video_w - new_w) / 2)
        # Ensure x offset is an even number.
        if new_x % 2 != 0:
            new_x -= 1

        return f"crop={new_w}:{video_h}:{new_x}:0", best_match['name']

    # If not clearly letterboxed or pillarboxed, don't snap.
    return f"crop={w}:{h}:{x}:{y}", None

def cluster_crop_values(crop_counts, tolerance=8):
    """Groups similar crop values into clusters based on the top-left corner."""
    clusters = []
    temp_counts = crop_counts.copy()

    while temp_counts:
        # Get the most frequent remaining crop as the new cluster center
        center_str, _ = temp_counts.most_common(1)[0]
        
        try:
            _, values = center_str.split('=')
            cw, ch, cx, cy = map(int, values.split(':'))
        except (ValueError, IndexError):
            del temp_counts[center_str] # Skip malformed strings
            continue

        cluster_total_count = 0
        crops_to_remove = []

        # Find all crops "close" to the center
        for crop_str, count in temp_counts.items():
            try:
                _, values = crop_str.split('=')
                w, h, x, y = map(int, values.split(':'))
                if abs(x - cx) <= tolerance and abs(y - cy) <= tolerance:
                    cluster_total_count += count
                    crops_to_remove.append(crop_str)
            except (ValueError, IndexError):
                continue
        
        if cluster_total_count > 0:
            clusters.append({'center': center_str, 'count': cluster_total_count})

        # Remove the clustered crops from the temporary counter
        for crop_str in crops_to_remove:
            del temp_counts[crop_str]
            
    clusters.sort(key=lambda c: c['count'], reverse=True)
    return clusters

def parse_crop_string(crop_str):
    """Parses a 'crop=w:h:x:y' string into a dictionary of integers."""
    try:
        _, values = crop_str.split('=')
        w, h, x, y = map(int, values.split(':'))
        return {'w': w, 'h': h, 'x': x, 'y': y}
    except (ValueError, IndexError):
        return None

def calculate_bounding_box(crop_keys):
    """Calculates a bounding box that contains all given crop rectangles."""
    min_x = min_w = min_y = min_h = float('inf')
    max_x = max_w = max_y = max_h = float('-inf')

    for key in crop_keys:
        parsed = parse_crop_string(key)
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

    # Heuristic: if the bounding box is very close to the min/max, it means all crops were similar
    if (max_x - min_x) <= 2 and (max_y - min_y) <= 2:
        return None # Too uniform, don't create a bounding box

    # Create a crop that spans the entire bounding box
    bounding_crop = f"crop={max_x - min_x}:{max_y - min_y}:{min_x}:{min_y}"
    
    return bounding_crop

def is_major_crop(crop_str, video_w, video_h, min_crop_size):
    """Checks if a crop is significant enough to be recommended by checking if any side is cropped by at least min_crop_size pixels."""
    parsed = parse_crop_string(crop_str)
    if not parsed:
        return False

    w, h, x, y = parsed['w'], parsed['h'], parsed['x'], parsed['y']

    # Calculate how much is cropped from each side
    crop_top = y
    crop_bottom = video_h - (y + h)
    crop_left = x
    crop_right = video_w - (x + w)

    # Return True if the largest crop on any single side meets the threshold
    if max(crop_top, crop_bottom, crop_left, crop_right) >= min_crop_size:
        return True
    
    return False

def analyze_video(input_file, duration, width, height, num_workers, significant_crop_threshold, min_crop, debug=False):
    """Main analysis function for the video."""
    print(f"\n--- Analyzing Video: {os.path.basename(input_file)} ---")
    
    # Step 1: Analyze video in segments to detect crops
    num_tasks = num_workers * 4 
    segment_duration = max(1, duration // num_tasks)
    tasks = [(i * segment_duration, input_file, width, height) for i in range(num_tasks)]
    
    print(f"Analyzing {len(tasks)} segments across {num_workers} worker(s)...")
    
    crop_results = []
    with multiprocessing.Pool(processes=num_workers) as pool:
        total_tasks = len(tasks)
        results_iterator = pool.imap_unordered(analyze_segment, tasks)
        
        for i, result in enumerate(results_iterator, 1):
            crop_results.append(result)
            progress_message = f"Analyzing Segments: {i}/{total_tasks} completed..."
            sys.stdout.write(f"\r{progress_message}")
            sys.stdout.flush()
    print()

    all_crops_with_ts = [crop for sublist in crop_results for crop in sublist]
    all_crop_strings = [item[0] for item in all_crops_with_ts]
    if not all_crop_strings:
        print(f"\n{COLOR_GREEN}Analysis complete. No black bars detected.{COLOR_RESET}")
        return
    
    crop_counts = Counter(all_crop_strings)
    
    if debug:
        print("\n--- Debug: Most Common Raw Detections ---")
        for crop_str, count in crop_counts.most_common(10):
            print(f"  - {crop_str} (Count: {count})")

    # Step 2: Cluster similar crop values
    clusters = cluster_crop_values(crop_counts)
    total_detections = sum(c['count'] for c in clusters)

    if debug:
        print("\n--- Debug: Detected Clusters ---")
        for cluster in clusters:
            percentage = (cluster['count'] / total_detections) * 100
            print(f"  - Center: {cluster['center']}, Count: {cluster['count']} ({percentage:.1f}%)")

    # Step 3: Filter clusters that are below the significance threshold
    significant_clusters = []
    for cluster in clusters:
        percentage = (cluster['count'] / total_detections) * 100
        if percentage >= significant_crop_threshold:
            significant_clusters.append(cluster)

    # Step 4: Determine final recommendation based on significant clusters
    print("\n--- Determining Final Crop Recommendation ---")

    for cluster in significant_clusters:
        parsed_crop = parse_crop_string(cluster['center'])
        if parsed_crop:
            _, ar_label = snap_to_known_ar(
                parsed_crop['w'], parsed_crop['h'], parsed_crop['x'], parsed_crop['y'], width, height
            )
            cluster['ar_label'] = ar_label
        else:
            cluster['ar_label'] = None

    if not significant_clusters:
        print(f"{COLOR_RED}No single crop value meets the {significant_crop_threshold}% significance threshold.{COLOR_RESET}")
        print("Recommendation: Do not crop. Try lowering the -sct threshold.")

    elif len(significant_clusters) == 1:
        dominant_cluster = significant_clusters[0]
        parsed_crop = parse_crop_string(dominant_cluster['center'])
        snapped_crop, ar_label = snap_to_known_ar(
            parsed_crop['w'], parsed_crop['h'], parsed_crop['x'], parsed_crop['y'], width, height
        )
        
        print("A single dominant aspect ratio was found.")
        if ar_label:
            print(f"The detected crop snaps to the '{ar_label}' aspect ratio.")
        
        # Check if the final crop is a no-op (i.e., matches source dimensions)
        parsed_snapped = parse_crop_string(snapped_crop)
        if parsed_snapped and parsed_snapped['w'] == width and parsed_snapped['h'] == height:
            print(f"\n{COLOR_GREEN}The detected crop matches the source resolution. No crop is needed.{COLOR_RESET}")
        else:
            print(f"\n{COLOR_GREEN}Recommended crop filter: -vf {snapped_crop}{COLOR_RESET}")

    else: # len > 1, mixed AR case
        print(f"{COLOR_YELLOW}Mixed aspect ratios detected (e.g., IMAX scenes).{COLOR_RESET}")
        print("Calculating a safe 'master' crop to contain all significant scenes.")

        crop_keys = [c['center'] for c in significant_clusters]
        bounding_box_crop = calculate_bounding_box(crop_keys)
        
        if bounding_box_crop:
            parsed_bb = parse_crop_string(bounding_box_crop)
            snapped_crop, ar_label = snap_to_known_ar(
                parsed_bb['w'], parsed_bb['h'], parsed_bb['x'], parsed_bb['y'], width, height
            )

            print("\n--- Detected Significant Ratios ---")
            for cluster in significant_clusters:
                percentage = (cluster['count'] / total_detections) * 100
                label = f"'{cluster['ar_label']}'" if cluster['ar_label'] else "Custom AR"
                print(f"  - {label} ({cluster['center']}) was found in {percentage:.1f}% of samples.")

            print(f"\n{COLOR_GREEN}Analysis complete.{COLOR_RESET}")
            if ar_label:
                print(f"The calculated master crop snaps to the '{ar_label}' aspect ratio.")
            
            # Check if the final crop is a no-op
            parsed_snapped = parse_crop_string(snapped_crop)
            if parsed_snapped and parsed_snapped['w'] == width and parsed_snapped['h'] == height:
                print(f"{COLOR_GREEN}The final calculated crop matches the source resolution. No crop is needed.{COLOR_RESET}")
            else:
                print(f"{COLOR_GREEN}Recommended safe crop filter: -vf {snapped_crop}{COLOR_RESET}")
        else:
            print(f"{COLOR_RED}Could not calculate a bounding box. Manual review is required.{COLOR_RESET}")

def main():
    parser = argparse.ArgumentParser(
        description="Analyzes a video file to detect black bars and recommend crop values. "
                    "Handles mixed aspect ratios by calculating a safe bounding box.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("input", help="Input video file")
    parser.add_argument("-n", "--num_workers", type=int, default=max(1, multiprocessing.cpu_count() // 2), help="Number of worker threads. Defaults to half of available cores.")
    parser.add_argument("-sct", "--significant_crop_threshold", type=float, default=5.0, help="Percentage a crop must be present to be considered 'significant'. Default is 5.0.")
    parser.add_argument("-mc", "--min_crop", type=int, default=10, help="Minimum pixels to crop on any side for it to be considered a 'major' crop. Default is 10.")
    parser.add_argument("--debug", action="store_true", help="Enable detailed debug logging.")
    
    args = parser.parse_args()

    input_file = args.input
    num_workers = args.num_workers
    significant_crop_threshold = args.significant_crop_threshold
    min_crop = args.min_crop

    # Validate input file
    if not os.path.isfile(input_file):
        print(f"{COLOR_RED}Error: Input file does not exist.{COLOR_RESET}")
        sys.exit(1)

    # Always probe the video file for metadata
    print("--- Probing video file for metadata ---")
    
    try:
        probe_duration_args = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
            input_file
        ]
        duration_str = subprocess.check_output(probe_duration_args, stderr=subprocess.STDOUT, text=True)
        duration = int(float(duration_str))
        print(f"Detected duration: {duration}s")

        # Probe for resolution, handling multiple video streams (e.g., with cover art)
        probe_res_args = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v',  # Select all video streams
            '-show_entries', 'stream=width,height,disposition',
            '-of', 'json',
            input_file
        ]
        probe_output = subprocess.check_output(probe_res_args, stderr=subprocess.STDOUT, text=True)
        streams_data = json.loads(probe_output)
        
        video_stream = None
        # Find the first video stream that is NOT an attached picture
        for stream in streams_data.get('streams', []):
            if stream.get('disposition', {}).get('attached_pic', 0) == 0:
                video_stream = stream
                break
        
        if not video_stream or 'width' not in video_stream or 'height' not in video_stream:
            # If no suitable stream is found, raise an error.
            raise ValueError("Could not find a valid video stream to probe for resolution.")

        width = int(video_stream['width'])
        height = int(video_stream['height'])
        print(f"Detected resolution: {width}x{height}")

    except Exception as e:
        print(f"{COLOR_RED}Error probing video file: {e}{COLOR_RESET}")
        sys.exit(1)

    print(f"\n--- Video Analysis Parameters ---")
    print(f"Input File: {os.path.basename(input_file)}")
    print(f"Duration: {duration}s")
    print(f"Resolution: {width}x{height}")
    print(f"Number of Workers: {num_workers}")
    print(f"Significance Threshold: {significant_crop_threshold}%")
    print(f"Minimum Crop Size: {min_crop}px")

    # Check for required tools
    check_prerequisites()

    # Analyze the video
    analyze_video(input_file, duration, width, height, num_workers, significant_crop_threshold, min_crop, args.debug)

if __name__ == "__main__":
    main()

