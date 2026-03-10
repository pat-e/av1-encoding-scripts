#!/usr/bin/env python3

# Note: This script is configured to use a custom version of SVT-AV1 
# called "SVT-AV1-Essential" from https://github.com/nekotrix/SVT-AV1-Essential

import os
import sys
import subprocess
import shutil
import tempfile
import json
import re
from datetime import datetime
from pathlib import Path

REQUIRED_TOOLS = [
    "ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit",
    "opusenc", "mediainfo", "av1an"
]
DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs")

REMUX_CODECS = {"aac", "opus"}

SVT_AV1_PARAMS = {
    "speed": "slower",
    "quality": "medium",
    "film-grain": 12,
    "color-primaries": 9,
    "transfer-characteristics": 16,
    "matrix-coefficients": 9,
    "scd": 0,
    "keyint": 0,
    "lp": 2,
    "auto-tiling": 1,
    "tune": 1,
    "progress": 2,
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

def is_hdr(file_path):
    """Checks if the video file is HDR."""
    try:
        ffprobe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=color_space,color_transfer,color_primaries",
            "-of", "json", str(file_path)
        ]
        result = run_cmd(ffprobe_cmd, capture_output=True)
        video_stream_info = json.loads(result)["streams"][0]
        
        color_primaries = video_stream_info.get("color_primaries")
        color_transfer = video_stream_info.get("color_transfer")

        # Basic check for HDR characteristics
        if color_primaries == "bt2020" and color_transfer in ["smpte2084", "arib-std-b67"]:
            return True
        return False
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        return False

def convert_audio_track(index, ch, lang, audio_temp_dir, source_file):
    audio_temp_path = Path(audio_temp_dir)
    temp_extracted = audio_temp_path / f"track_{index}_extracted.flac"
    temp_normalized = audio_temp_path / f"track_{index}_normalized.flac"
    final_opus = audio_temp_path / f"track_{index}_final.opus"

    print(f"    - Extracting Audio Track #{index} to FLAC...")
    ffmpeg_args = [
        "ffmpeg", "-v", "quiet", "-stats", "-y", "-i", str(source_file),
        "-map", f"0:{index}", "-map_metadata", "-1", "-c:a", "flac", str(temp_extracted)
    ]
    run_cmd(ffmpeg_args)

    print(f"    - Normalizing Audio Track #{index} with ffmpeg (loudnorm 2-pass)...")
    print("      - Pass 1: Analyzing...")
    result = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(temp_extracted), "-af", "loudnorm=I=-23:LRA=7:tp=-1.5:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    
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

    print("      - Pass 2: Applying normalization...")
    run_cmd([
        "ffmpeg", "-v", "quiet", "-stats", "-y", "-i", str(temp_extracted), "-af",
        f"loudnorm=I=-23:LRA=7:tp=-1.5:measured_i={stats['input_i']}:measured_lra={stats['input_lra']}:measured_tp={stats['input_tp']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}",
        "-c:a", "flac", str(temp_normalized)
    ])

    if ch == 1:
        bitrate = "64k"
    elif ch == 2:
        bitrate = "128k"
    elif ch == 6:
        bitrate = "256k"
    elif ch == 8:
        bitrate = "384k"
    else:
        bitrate = "192k"

    print(f"    - Encoding Audio Track #{index} to Opus at {bitrate}...")
    run_cmd([
        "opusenc", "--vbr", "--bitrate", bitrate, str(temp_normalized), str(final_opus)
    ])
    return final_opus

def convert_video(source_file_base, source_file_full):
    print("  --- Starting Video Processing ---")
    vpy_file = Path(f"{source_file_base}.vpy")
    encoded_video_file = Path(f"temp-{source_file_base}.mkv")

    source_full_path = os.path.abspath(source_file_full)
    vpy_script_content = f'''import vapoursynth as vs
core = vs.core
core.num_threads = 4
clip = core.ffms2.Source(source=r'{source_full_path}')
clip = core.resize.Point(clip, format=vs.YUV420P10, matrix_in_s="2020_ncl")
clip.set_output()
'''
    with vpy_file.open("w", encoding="utf-8") as f:
        f.write(vpy_script_content)

    print("    - Starting AV1 encode with av1an (this will take a long time)...")
    total_cores = os.cpu_count() or 4
    workers = max(1, (total_cores // 2) - 1)
    print(f"    - Using {workers} workers for av1an (Total Cores: {total_cores}, Logic: (Cores/2)-1).")

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
    return encoded_video_file

def main(speed=None, quality=None, grain=None):
    check_tools()

    if speed:
        SVT_AV1_PARAMS["speed"] = speed
    if quality:
        SVT_AV1_PARAMS["quality"] = quality
    if grain is not None:
        SVT_AV1_PARAMS["film-grain"] = grain

    current_dir = Path(".")
    files_to_process = sorted(
        f for f in current_dir.glob("*.mkv")
        if not (f.name.endswith(".ut.mkv") or f.name.startswith("temp-") or f.name.startswith("output-"))
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
            if not (f.name.endswith(".ut.mkv") or f.name.startswith("temp-") or f.name.startswith("output-"))
        )
        if not files_to_process:
            print("No more .mkv files found to process. The script will now exit.")
            break
            
        file_path = files_to_process[0]

        if not is_hdr(file_path):
            print(f"'{file_path.name}' is not HDR. Moving to 'original' folder and skipping.")
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
            try:
                audio_temp_dir = tempfile.mkdtemp(prefix="hdr_audio_")
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

                encoded_video_file = convert_video(file_path.stem, str(input_file_abs))

                print("--- Starting Audio Processing ---")
                processed_audio_files = []
                audio_tracks_to_remux = []
                audio_streams = [s for s in ffprobe_info.get("streams", []) if s.get("codec_type") == "audio"]
                
                for stream in audio_streams:
                    stream_index = stream["index"]
                    codec = stream.get("codec_name")
                    channels = stream.get("channels", 2)
                    language = stream.get("tags", {}).get("language", "und")

                    mkv_track = None
                    for t in mkv_info.get("tracks", []):
                        if t.get("type") == "audio" and t.get("properties", {}).get("stream_id") == stream_index:
                            mkv_track = t
                            break
                    if not mkv_track:
                        mkv_track = mkv_info.get("tracks", [])[stream_index] if stream_index < len(mkv_info.get("tracks", [])) else {}

                    track_id = mkv_track.get("id", -1)
                    track_title = mkv_track.get("properties", {}).get("track_name", "")

                    print(f"Processing Audio Stream #{stream_index} (TID: {track_id}, Codec: {codec}, Channels: {channels})")
                    if codec in REMUX_CODECS:
                        audio_tracks_to_remux.append(str(track_id))
                    else:
                        opus_file = convert_audio_track(stream_index, channels, language, audio_temp_dir, str(input_file_abs))
                        processed_audio_files.append({
                            "Path": opus_file,
                            "Language": language,
                            "Title": track_title,
                        })

                print("--- Finished Audio Processing ---")

                print("Assembling final file with mkvmerge...")
                mkvmerge_args = ["mkvmerge", "-o", str(intermediate_output_file), str(encoded_video_file)]
                for file_info in processed_audio_files:
                    mkvmerge_args += [
                        "--language", f"0:{file_info['Language']}",
                        "--track-name", f"0:{file_info['Title']}",
                        str(file_info["Path"])
                    ]

                source_copy_args = ["--no-video"]
                if audio_tracks_to_remux:
                    source_copy_args += ["--audio-tracks", ",".join(audio_tracks_to_remux)]
                else:
                    source_copy_args += ["--no-audio"]
                mkvmerge_args += source_copy_args + [str(input_file_abs)]
                run_cmd(mkvmerge_args)

                print("Moving files to final destinations...")
                shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
                shutil.move(str(intermediate_output_file), DIR_COMPLETED / file_path.name)

                print("Cleaning up persistent video temporary files...")
                video_temp_files = [
                    current_dir / f"{file_path.stem}.vpy",
                    current_dir / f"temp-{file_path.stem}.mkv",
                ]
                for temp_vid_file in video_temp_files:
                    if temp_vid_file.exists():
                        temp_vid_file.unlink()

            except Exception as e:
                print(f"ERROR: An error occurred while processing '{file_path.name}': {e}", file=sys.stderr)
                original_stderr_console.write(f"ERROR during processing of '{file_path.name}': {e}\nSee log '{log_file_path}' for details.\n")
                processing_error_occurred = True
            finally:
                print("--- Starting Universal Cleanup ---")
                if audio_temp_dir and Path(audio_temp_dir).exists():
                    shutil.rmtree(audio_temp_dir, ignore_errors=True)
                
                if intermediate_output_file.exists() and not processing_error_occurred:
                    intermediate_output_file.unlink()

        finally:
            runtime = datetime.now() - date_for_runtime_calc
            runtime_str = str(runtime).split('.')[0]
            
            print(f"\nTotal runtime for this file: {runtime_str}")
            
            if sys.stdout != original_stdout_console:
                sys.stdout = original_stdout_console
            if sys.stderr != original_stderr_console:
                sys.stderr = original_stderr_console
            if log_file_handle:
                log_file_handle.close()
            
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
    parser = argparse.ArgumentParser(description="Batch-process HDR MKV files.")
    parser.add_argument("--speed", type=str, help="Set the encoding speed for SVT-AV1.")
    parser.add_argument("--quality", type=str, help="Set the encoding quality for SVT-AV1.")
    parser.add_argument("--grain", type=int, help="Set the film-grain value for SVT-AV1.")
    args = parser.parse_args()
    main(speed=args.speed, quality=args.quality, grain=args.grain)
