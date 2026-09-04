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
import math

LOUDNESS_I = -16.0
LOUDNESS_TP = -1.5
LOUDNESS_LRA = 20.0

REQUIRED_TOOLS = [
    "ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit",
    "opusenc", "mediainfo", "av1an"
]
DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs")

REMUX_CODECS = {"aac", "opus"}

SVT_AV1_PARAMS = {
    "preset": 2,                       # Speed preset. Lower is slower and yields better compression efficiency.
    "crf": 30,                         # Constant Rate Factor (CRF). Lower is better quality.
    "color-primaries": 9,              # BT.2020 color primaries for HDR.
    "transfer-characteristics": 16,    # SMPTE 2084 (PQ) transfer characteristics for HDR10.
    "matrix-coefficients": 9,          # BT.2020 non-constant luminance matrix coefficients for HDR.
    "scd": 0,                          # Scene change detection OFF (av1an handles scene cuts).
    "scm": 0,			               # Set screen content detection level, default is 2 (0: off, 1: on, 2: content adaptive)
    "keyint": 0,                       # Keyframe interval OFF (av1an inserts keyframes).
    "lp": 2,                           # Logical Processors to use per av1an worker (perfect for leaving cores free).
    "auto-tiling": 1,                  # Automatically determine the number of tiles based on resolution.
    "tune": 2,                         # 0 = VQ, 1 = PSNR, 2 = SSIM (SVT-AV1-Essential default recommended).
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

def run_ffmpeg_logged(args):
    """Run ffmpeg so -stats is teed to console and log as it happens."""
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    try:
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            sys.stdout.write(chunk.decode("utf-8", errors="replace"))
            sys.stdout.flush()
    finally:
        ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, args)

def _parse_loudnorm_json(stderr_output):
    json_start_index = stderr_output.find("{")
    if json_start_index == -1:
        raise ValueError("Could not find start of JSON block in ffmpeg output for loudness analysis.")
    brace_level = 0
    json_end_index = -1
    for i, char in enumerate(stderr_output[json_start_index:]):
        if char == "{":
            brace_level += 1
        elif char == "}":
            brace_level -= 1
            if brace_level == 0:
                json_end_index = json_start_index + i + 1
                break
    if json_end_index == -1:
        raise ValueError("Could not find end of JSON block in ffmpeg output for loudness analysis.")
    return json.loads(stderr_output[json_start_index:json_end_index])

def _finite_float(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback

def apply_constant_gain_loudness(input_path, output_path, track_index):
    """Two-pass ffmpeg loudnorm, linear (constant gain + true-peak). No asoftclip."""
    print(f"    - Normalizing Audio Track #{track_index} (loudnorm 2-pass linear)...")
    print(
        f"      - Targets: I={LOUDNESS_I} LUFS, TP={LOUDNESS_TP} dBTP, "
        f"LRA={LOUDNESS_LRA} LU (linear; not a compressor)"
    )
    print("      - Pass 1: Measuring integrated loudness and true peak...")
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "info", "-i", str(input_path),
            "-af", (
                f"loudnorm=I={LOUDNESS_I}:LRA={LOUDNESS_LRA}:tp={LOUDNESS_TP}"
                f":print_format=json"
            ),
            "-f", "null", "-",
        ],
        capture_output=True, text=True, check=True,
    )
    stats = _parse_loudnorm_json(result.stderr)
    measured_i = _finite_float(stats.get("input_i"), None)
    if measured_i is None:
        print("      - Could not measure integrated loudness; copying without gain.")
        run_cmd(["ffmpeg", "-v", "quiet", "-y", "-i", str(input_path), "-c:a", "flac", str(output_path)])
        return

    measured_tp = _finite_float(stats.get("input_tp"), -99.0)
    measured_lra = _finite_float(stats.get("input_lra"), 0.0)
    measured_thresh = _finite_float(stats.get("input_thresh"), -70.0)
    offset = _finite_float(stats.get("target_offset"), 0.0)
    gain_db = LOUDNESS_I - measured_i
    print(
        f"      - Measured I={measured_i:.2f} LUFS, TP={measured_tp:.2f} dBTP, "
        f"LRA={measured_lra:.2f} LU → {gain_db:+.2f} dB (offset {offset:+.2f})"
    )
    if measured_lra > LOUDNESS_LRA:
        print(
            f"      - Warning: source LRA {measured_lra:.2f} > {LOUDNESS_LRA}; "
            "loudnorm may use dynamic mode. Prefer LRA<=20 for constant gain."
        )
    print("      - Pass 2: loudnorm linear=true (true-peak aware, not hard clip)...")
    loudnorm_apply = (
        f"loudnorm=I={LOUDNESS_I}:LRA={LOUDNESS_LRA}:tp={LOUDNESS_TP}"
        f":measured_I={measured_i:.2f}"
        f":measured_LRA={measured_lra:.2f}"
        f":measured_TP={measured_tp:.2f}"
        f":measured_thresh={measured_thresh:.2f}"
        f":offset={offset:.2f}"
        f":linear=true"
        f":print_format=summary"
    )
    run_ffmpeg_logged([
        "ffmpeg", "-hide_banner", "-v", "error", "-stats", "-y",
        "-i", str(input_path),
        "-af", f"{loudnorm_apply},aformat=sample_fmts=s32",
        "-c:a", "flac", "-sample_fmt", "s32",
        str(output_path),
    ])

def downmix_filters(ch):
    """Nightmode Dialogue (Collier / Harrelson). pan '<' renormalizes so the mix cannot clip."""
    if ch == 6:
        return [
            "pan=stereo|FL<FC+0.30*FL+0.30*SL|FR<FC+0.30*FR+0.30*SR",
            "pan=stereo|FL<FC+0.30*FL+0.30*BL|FR<FC+0.30*FR+0.30*BR",
            "aformat=ch_layouts=5.1,pan=stereo|FL<FC+0.30*FL+0.30*BL|FR<FC+0.30*FR+0.30*BR",
            "pan=stereo|c0<c2+0.30*c0+0.30*c4|c1<c2+0.30*c1+0.30*c5",
        ]
    if ch == 8:
        return [
            "pan=stereo|FL<FC+0.30*FL+0.30*SL+0.30*BL|FR<FC+0.30*FR+0.30*SR+0.30*BR",
            "pan=stereo|c0<c2+0.30*c0+0.30*c4+0.30*c6|c1<c2+0.30*c1+0.30*c5+0.30*c7",
        ]
    return []

def convert_audio_track(index, ch, audio_temp_dir, source_file, should_downmix):
    audio_temp_path = Path(audio_temp_dir)
    temp_extracted = audio_temp_path / f"track_{index}_extracted.flac"
    temp_normalized = audio_temp_path / f"track_{index}_normalized.flac"
    final_opus = audio_temp_path / f"track_{index}_final.opus"

    print(f"    - Extracting Audio Track #{index} to FLAC...")
    base_args = [
        "ffmpeg", "-hide_banner", "-v", "error", "-stats", "-y",
        "-drc_scale", "0",
        "-i", str(source_file),
        "-map", f"0:{index}",
        "-map_metadata", "-1",
    ]
    downmix_attempts = []
    if should_downmix and ch >= 6:
        downmix_attempts.extend(downmix_filters(ch))
        downmix_attempts.append(None)
    else:
        downmix_attempts.append("keep")

    last_error = None
    extracted = False
    for attempt, filt in enumerate(downmix_attempts, start=1):
        ffmpeg_args = list(base_args)
        if filt == "keep":
            pass
        elif filt is None:
            ffmpeg_args += ["-ac", "2"]
            print("      - Downmix fallback: -ac 2")
        else:
            ffmpeg_args += ["-af", filt]
            print(f"      - Downmix filter (try {attempt}): {filt}")
        ffmpeg_args += ["-c:a", "flac", str(temp_extracted)]
        try:
            run_ffmpeg_logged(ffmpeg_args)
            extracted = True
            break
        except subprocess.CalledProcessError as e:
            last_error = e
            print(f"      - Downmix try {attempt} failed, trying next option...")
    if not extracted:
        raise last_error

    apply_constant_gain_loudness(temp_extracted, temp_normalized, index)

    is_being_downmixed = should_downmix and ch >= 6
    if is_being_downmixed:
        bitrate = "128k"
    elif ch == 1:
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
    run_cmd(["opusenc", "--vbr", "--bitrate", bitrate, str(temp_normalized), str(final_opus)])
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
clip = core.resize.Point(clip, format=vs.YUV420P10, matrix_in_s="2020ncl")
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

def main(preset=None, crf=None, grain=None, norm_i=None, norm_tp=None, no_downmix=False):
    global LOUDNESS_I, LOUDNESS_TP
    if norm_i is not None:
        LOUDNESS_I = norm_i
    if norm_tp is not None:
        LOUDNESS_TP = norm_tp
    check_tools()

    if preset is not None:
        SVT_AV1_PARAMS["preset"] = preset
    if crf is not None:
        SVT_AV1_PARAMS["crf"] = crf
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
                        opus_file = convert_audio_track(stream_index, channels, audio_temp_dir, str(input_file_abs), not no_downmix)
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
                    current_dir / f"{file_path.name}.ffindex",
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
    parser.add_argument("--preset", type=int, help=f"Set the encoding preset for SVT-AV1. Lower is slower/better compression. (default: {SVT_AV1_PARAMS['preset']})")
    parser.add_argument("--crf", type=int, help=f"Set the Constant Rate Factor (CRF) for SVT-AV1. Lower is better quality. (default: {SVT_AV1_PARAMS['crf']})")
    parser.add_argument("--grain", type=int, help="Set the film-grain value for SVT-AV1. (If omitted, grain synthesis is disabled.)")
    parser.add_argument("--norm-i", type=float, help=f"Target integrated loudness in LUFS (default: {LOUDNESS_I})")
    parser.add_argument("--norm-tp", type=float, help=f"True-peak ceiling in dBTP (default: {LOUDNESS_TP})")
    parser.add_argument("--no-downmix", action="store_true", help="Preserve original audio channel layout.")
    args = parser.parse_args()
    main(preset=args.preset, crf=args.crf, grain=args.grain, norm_i=args.norm_i, norm_tp=args.norm_tp, no_downmix=args.no_downmix)
