#!/usr/bin/env python3

# Batch encoder: all-intra x264 intermediate + xav (SVT-AV1-Essential) + existing audio path.
# Video: VFR -> HandBrake all-intra x264 (CFR + seekable) -> xav
#        CFR -> ffmpeg all-intra x264 (-g 1) -> xav
#        Never both. xav cannot decode UTVideo.
#        xav -e svt-av1 -p "--preset 1 --lp 2" -w ((cpu_count - 2) // 2) -b 1
# Audio: outside xav. AAC/Opus remuxed. Else: pan (optional) → constant-gain LUFS → Opus.
#        Measure I, apply one gain to hit LOUDNESS_I, brickwall to LOUDNESS_TP. No LRA compressor.
# Crop / scene-detect / chunking: owned by xav. No cropdetect, no .vpy, no av1an.

import math
import os
import sys
import subprocess
import shutil
import tempfile
import json
import re
import argparse
from datetime import datetime
from pathlib import Path

REQUIRED_TOOLS = [
    "ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit",
    "opusenc", "mediainfo", "xav", "HandBrakeCLI",
]
DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs")
DIR_FAILED = Path("failed")

REMUX_CODECS = {"aac", "opus"}

# Locked xav / SVT-AV1-Essential video params. CRF is left to xav/SVT unless --crf is set.
XAV_ENCODER = "svt-av1"
XAV_PRESET = 1
XAV_LP = 2
XAV_BUFF = 1
WORKER_CORES_RESERVED = 2

# All-intra x264 intermediate so xav workers can seek. UTVideo is not decodable by xav.
INTERMEDIATE_SUFFIX = ".x264.mkv"

# Constant-gain loudness (XAV-style, no LRA pull-down).
LOUDNESS_I = -16.0
LOUDNESS_TP = -1.5


class Tee:
    """Write to the log file and the real console at the same time (ffmpeg -stats, prints)."""

    def __init__(self, *files):
        self.files = files

    def write(self, data):
        for f in self.files:
            try:
                f.write(data)
            except Exception:
                pass

    def flush(self):
        for f in self.files:
            try:
                f.flush()
            except Exception:
                pass

    def isatty(self):
        return any(getattr(f, "isatty", lambda: False)() for f in self.files)


def check_tools():
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            print(f"Required tool '{tool}' not found in PATH.")
            sys.exit(1)


def run_cmd(cmd, capture_output=False, check=True):
    if capture_output:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check, text=True)
        return result.stdout
    subprocess.run(cmd, check=check)


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
    """Measure integrated LUFS, apply one gain, brickwall-clamp true peak. No LRA compressor."""
    print(f"    - Normalizing Audio Track #{track_index} (constant-gain LUFS, 2-pass)...")
    print(f"      - Targets: I={LOUDNESS_I} LUFS, TP={LOUDNESS_TP} dBTP (no LRA processing)")
    print("      - Pass 1: Measuring integrated loudness...")
    result = subprocess.run(
        [
            "ffmpeg", "-v", "info", "-i", str(input_path),
            "-af", f"loudnorm=I={LOUDNESS_I}:LRA=20:tp={LOUDNESS_TP}:print_format=json",
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

    gain_db = LOUDNESS_I - measured_i
    gain = 10 ** (gain_db / 20.0)
    tp_linear = 10 ** (LOUDNESS_TP / 20.0)
    print(f"      - Measured I={measured_i:.2f} LUFS → constant gain {gain_db:+.2f} dB")
    print(f"      - Pass 2: Apply gain, brickwall clamp {LOUDNESS_TP} dBTP...")
    run_ffmpeg_logged([
        "ffmpeg", "-hide_banner", "-v", "error", "-stats", "-y",
        "-i", str(input_path),
        "-af", (
            f"volume={gain:.10f},"
            f"asoftclip=type=hard:threshold={tp_linear:.10f},"
            f"aformat=sample_fmts=s32"
        ),
        "-c:a", "flac", "-sample_fmt", "s32",
        str(output_path),
    ])


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


def downmix_filters(ch):
    """Same 0.30 center-forward mix. ffmpeg 9 renamed ocl → ochl; named pan avoids that."""
    if ch == 6:
        return [
            "pan=stereo|FL=FC+0.30*FL+0.30*SL|FR=FC+0.30*FR+0.30*SR",
            "pan=stereo|FL=FC+0.30*FL+0.30*BL|FR=FC+0.30*FR+0.30*BR",
            "aformat=ch_layouts=5.1,pan=stereo|FL=FC+0.30*FL+0.30*BL|FR=FC+0.30*FR+0.30*BR",
            "pan=stereo|c0=c2+0.30*c0+0.30*c4|c1=c2+0.30*c1+0.30*c5",
        ]
    if ch == 8:
        return [
            "pan=stereo|FL=FC+0.30*FL+0.30*SL+0.30*BL|FR=FC+0.30*FR+0.30*SR+0.30*BR",
            "pan=stereo|c0=c2+0.30*c0+0.30*c4+0.30*c6|c1=c2+0.30*c1+0.30*c5+0.30*c7",
        ]
    return []


def convert_audio_track(index, ch, lang, audio_temp_dir, source_file, should_downmix):
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
        downmix_attempts.append(None)  # last resort: -ac 2
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
    run_cmd([
        "opusenc", "--vbr", "--bitrate", bitrate, str(temp_normalized), str(final_opus)
    ])
    return final_opus


def xav_worker_count(override=None):
    if override is not None:
        return max(1, int(override))
    total_cores = os.cpu_count() or 4
    usable = max(1, total_cores - WORKER_CORES_RESERVED)
    return max(1, usable // 2)


def file_is_usable(path):
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def run_handbrake_all_intra(source_file_full, output_file, target_cfr_fps):
    """VFR → CFR and all-intra x264 in one pass. keyint=1 is ffmpeg -g 1."""
    print(f"    - Source is VFR. HandBrakeCLI CFR ({target_cfr_fps}) + all-intra x264...")
    handbrake_args = [
        "HandBrakeCLI",
        "--input", str(source_file_full),
        "--output", str(output_file),
        "--cfr",
        "--rate", str(target_cfr_fps),
        "--encoder", "x264",
        "--quality", "0",
        "--encoder-preset", "superfast",
        "--encoder-tune", "fastdecode",
        "--encopts", "keyint=1:min-keyint=1",
        "--audio", "none",
        "--subtitle", "none",
        "--crop-mode", "none",
    ]
    print(f"    - Running HandBrakeCLI: {' '.join(handbrake_args)}")
    run_cmd(handbrake_args)
    return file_is_usable(output_file)


def create_ffmpeg_x264_intermediate(current_input, intermediate_file):
    print("    - Creating all-intra x264 intermediate (libx264 CRF 0, -g 1, video only)...")
    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-v", "quiet", "-stats", "-y",
        "-i", str(current_input),
        "-map", "0:v:0",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-an", "-sn", "-dn",
        "-c:v", "libx264",
        "-crf", "0",
        "-preset", "superfast",
        "-tune", "fastdecode",
        "-g", "1",
        str(intermediate_file),
    ]
    print(f"    - Running ffmpeg: {' '.join(ffmpeg_args)}")
    run_cmd(ffmpeg_args)
    return intermediate_file


def create_xav_intermediate(source_file_base, source_file_full, is_vfr, target_cfr_fps_for_handbrake):
    """One all-intra x264 file for xav. VFR uses HandBrake only; CFR uses ffmpeg only."""
    intermediate_file = Path(f"{source_file_base}{INTERMEDIATE_SUFFIX}")
    if file_is_usable(intermediate_file):
        print(f"    - Reusing existing x264 intermediate (resume): {intermediate_file}")
        return intermediate_file

    if is_vfr and target_cfr_fps_for_handbrake:
        try:
            if run_handbrake_all_intra(source_file_full, intermediate_file, target_cfr_fps_for_handbrake):
                print(f"    - HandBrake all-intra CFR ready: {intermediate_file}")
                return intermediate_file
            print("    - Warning: HandBrakeCLI produced an empty file. Falling back to ffmpeg on the original source.")
        except subprocess.CalledProcessError as e:
            print(f"    - Error during HandBrakeCLI execution: {e}")
            print("    - Falling back to ffmpeg on the original source.")

    return create_ffmpeg_x264_intermediate(source_file_full, intermediate_file)


def convert_video(source_file_base, source_file_full, is_vfr, target_cfr_fps_for_handbrake, preset=XAV_PRESET, crf=None, workers=None, buff=None):
    print("  --- Starting Video Processing ---")
    encoded_video_file = Path(f"temp-{source_file_base}.mkv")
    intermediate_file = create_xav_intermediate(
        source_file_base, source_file_full, is_vfr, target_cfr_fps_for_handbrake
    )

    if file_is_usable(encoded_video_file):
        print(f"    - Reusing existing xav output (resume): {encoded_video_file}")
        print("  --- Finished Video Processing ---")
        return encoded_video_file, intermediate_file

    worker_count = xav_worker_count(workers)
    buff_count = XAV_BUFF if buff is None else max(0, int(buff))
    total_cores = os.cpu_count() or 4
    print(f"    - Using {worker_count} xav workers (Total Cores: {total_cores}, Logic: (cores - {WORKER_CORES_RESERVED}) / 2).")
    print(f"    - xav decode buffer: -b {buff_count} (one extra pre-decoded chunk when 1).")

    param_parts = [f"--preset {preset}", f"--lp {XAV_LP}"]
    if crf is not None:
        param_parts.append(f"--crf {crf}")
    xav_params = " ".join(param_parts)

    xav_args = [
        "xav",
        "-e", XAV_ENCODER,
        "-p", xav_params,
        "-w", str(worker_count),
        "-b", str(buff_count),
        str(intermediate_file),
        str(encoded_video_file),
    ]
    print("    - Starting AV1 encode with xav (this will take a long time)...")
    print(f"    - xav command: {' '.join(xav_args)}")
    print("    - No -a flag: audio stays outside xav. Autocrop/SCD are xav's.")
    run_cmd(xav_args)
    print("  --- Finished Video Processing ---")
    return encoded_video_file, intermediate_file


def is_ffmpeg_decodable(file_path):
    try:
        subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(file_path), "-map", "0:a:0", "-t", "1", "-f", "null", "-"
        ], check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def list_source_mkvs(current_dir):
    return sorted(
        f for f in current_dir.glob("*.mkv")
        if not (
            f.name.endswith(".ut.mkv")
            or f.name.endswith(INTERMEDIATE_SUFFIX)
            or f.name.endswith(".cfr_temp.mkv")
            or f.name.endswith("_xav.mkv")
            or f.name.startswith("temp-")
            or f.name.startswith("output-")
        )
    )


def detect_vfr(media_info):
    is_vfr = False
    target_cfr_fps_for_handbrake = None
    video_track_info = None
    if media_info.get("media") and media_info["media"].get("track"):
        for track in media_info["media"]["track"]:
            if track.get("@type") == "Video":
                video_track_info = track
                break
    if not video_track_info:
        return is_vfr, target_cfr_fps_for_handbrake

    frame_rate_mode = video_track_info.get("FrameRate_Mode")
    if not (frame_rate_mode and frame_rate_mode.upper() in ["VFR", "VARIABLE"]):
        print("    - Video appears to be CFR or FrameRate_Mode not specified as VFR/Variable by MediaInfo.")
        return is_vfr, target_cfr_fps_for_handbrake

    is_vfr = True
    print(f"    - Detected VFR based on MediaInfo FrameRate_Mode: {frame_rate_mode}")
    original_fps_str = video_track_info.get("FrameRate_Original_String")
    if original_fps_str:
        match = re.search(r"\((\d+/\d+)\)", original_fps_str)
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
                num, den = map(float, target_cfr_fps_for_handbrake.split("/"))
                target_cfr_fps_for_handbrake = f"{num / den:.3f}"
                print(f"    - Converted fractional FPS to decimal for HandBrake: {target_cfr_fps_for_handbrake}")
            except ValueError:
                print(f"    - Warning: Could not parse fractional FPS '{target_cfr_fps_for_handbrake}'. HandBrakeCLI might fail.")
                is_vfr = False
    else:
        print("    - Warning: VFR detected, but could not determine target CFR from MediaInfo. Will attempt the x264 intermediate without HandBrake.")
        is_vfr = False
    return is_vfr, target_cfr_fps_for_handbrake


def video_temp_files(current_dir, file_path, handbrake_intermediate):
    files = [
        current_dir / f"{file_path.stem}{INTERMEDIATE_SUFFIX}",
        current_dir / f"{file_path.stem}.cfr_temp.mkv",
        current_dir / f"{file_path.stem}.ut.mkv",
        current_dir / f"temp-{file_path.stem}.mkv",
        current_dir / f"{file_path.stem}.x264_scd.txt",
        current_dir / f"{file_path.stem}.ut_scd.txt",
        current_dir / f"{file_path.stem}.ut.mkv.ffindex",
        current_dir / f"{file_path.stem}.ut.mkv.lwi",
        current_dir / f"{file_path.stem}.vpy",
    ]
    if handbrake_intermediate and handbrake_intermediate.exists():
        files.append(handbrake_intermediate)
    return files


def main(no_downmix=False, preset=None, crf=None, workers=None, buff=None, norm_i=None, norm_tp=None):
    check_tools()
    global LOUDNESS_I, LOUDNESS_TP
    if norm_i is not None:
        LOUDNESS_I = norm_i
    if norm_tp is not None:
        LOUDNESS_TP = norm_tp
    encode_preset = XAV_PRESET if preset is None else preset
    current_dir = Path(".")
    if not list_source_mkvs(current_dir):
        print("No MKV files found to process. Exiting.")
        return
    DIR_COMPLETED.mkdir(exist_ok=True, parents=True)
    DIR_ORIGINAL.mkdir(exist_ok=True, parents=True)
    DIR_CONV_LOGS.mkdir(exist_ok=True, parents=True)
    DIR_FAILED.mkdir(exist_ok=True, parents=True)
    failed_this_run = set()

    while True:
        files_to_process = [
            f for f in list_source_mkvs(current_dir)
            if f.resolve() not in failed_this_run
        ]
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
            log_file_handle = open(log_file_path, "w", encoding="utf-8", buffering=1)
            sys.stdout = Tee(log_file_handle, original_stdout_console)
            sys.stderr = Tee(log_file_handle, original_stderr_console)
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
                ffprobe_info = json.loads(run_cmd([
                    "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(input_file_abs)
                ], capture_output=True))
                mkv_info = json.loads(run_cmd([
                    "mkvmerge", "-J", str(input_file_abs)
                ], capture_output=True))
                media_info = json.loads(run_cmd([
                    "mediainfo", "--Output=JSON", "-f", str(input_file_abs)
                ], capture_output=True))

                is_vfr, target_cfr_fps_for_handbrake = detect_vfr(media_info)
                encoded_video_file, handbrake_intermediate_for_cleanup = convert_video(
                    file_path.stem,
                    str(input_file_abs),
                    is_vfr,
                    target_cfr_fps_for_handbrake,
                    preset=encode_preset,
                    crf=crf,
                    workers=workers,
                    buff=buff,
                )

                print("--- Starting Audio Processing ---")
                processed_audio_files = []
                audio_tracks_to_remux = []
                audio_streams = [s for s in ffprobe_info.get("streams", []) if s.get("codec_type") == "audio"]
                media_tracks_data = media_info.get("media", {}).get("track", [])
                mediainfo_audio_tracks = {int(t.get("StreamOrder", -1)): t for t in media_tracks_data if t.get("@type") == "Audio"}

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
                    audio_track_info = mediainfo_audio_tracks.get(stream_index)
                    track_delay = 0
                    delay_raw = audio_track_info.get("Video_Delay") if audio_track_info else None
                    if delay_raw is not None:
                        try:
                            delay_val = float(delay_raw)
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
                        opus_file = convert_audio_track(
                            stream_index, channels, language, audio_temp_dir, str(input_file_abs), not no_downmix
                        )
                        processed_audio_files.append({
                            "Path": opus_file,
                            "Language": language,
                            "Title": track_title,
                            "Delay": track_delay,
                        })

                print("--- Finished Audio Processing ---")
                print("Assembling final file with mkvmerge...")
                mkvmerge_args = [
                    "mkvmerge", "-o", str(intermediate_output_file),
                    "--title", "",
                    "--track-name", "0:",
                    str(encoded_video_file),
                ]
                for file_info in processed_audio_files:
                    sync_switch = ["--sync", f"0:{file_info['Delay']}"] if file_info["Delay"] else []
                    mkvmerge_args += [
                        "--language", f"0:{file_info['Language']}",
                        "--track-name", f"0:{file_info['Title']}",
                    ] + sync_switch + [str(file_info["Path"])]

                source_copy_args = ["--no-video"]
                if audio_tracks_to_remux:
                    source_copy_args += ["--audio-tracks", ",".join(audio_tracks_to_remux)]
                else:
                    source_copy_args += ["--no-audio"]
                mkvmerge_args += source_copy_args + [str(input_file_abs)]
                run_cmd(mkvmerge_args)

                print("    - Clearing container and video-track titles...")
                try:
                    run_cmd([
                        "mkvpropedit", str(intermediate_output_file),
                        "--delete", "title",
                        "--edit", "track:v1",
                        "--delete", "name",
                    ])
                except subprocess.CalledProcessError as e:
                    print(f"    - Warning: mkvpropedit could not clear titles ({e}). Continuing.")

                print("Moving files to final destinations...")
                shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
                shutil.move(str(intermediate_output_file), DIR_COMPLETED / file_path.name)

                print("Cleaning up persistent video temporary files (after successful processing)...")
                for temp_vid_file in video_temp_files(current_dir, file_path, handbrake_intermediate_for_cleanup):
                    if temp_vid_file.exists():
                        print(f"    Deleting: {temp_vid_file}")
                        temp_vid_file.unlink(missing_ok=True)
                    else:
                        print(f"    Skipping (not found): {temp_vid_file}")

            except Exception as e:
                print(f"ERROR: An error occurred while processing '{file_path.name}': {e}", file=sys.stderr)
                original_stderr_console.write(f"ERROR during processing of '{file_path.name}': {e}\nSee log '{log_file_path}' for details.\n")
                processing_error_occurred = True
            finally:
                print("--- Starting Universal Cleanup (for this file) ---")
                print("  - Cleaning up disposable audio temporary directory...")
                if audio_temp_dir and Path(audio_temp_dir).exists():
                    shutil.rmtree(audio_temp_dir, ignore_errors=True)
                    print(f"    - Deleted audio temp dir: {audio_temp_dir}")
                elif audio_temp_dir:
                    print(f"    - Audio temp dir not found or already cleaned: {audio_temp_dir}")
                else:
                    print("    - Audio temp dir was not created.")

                print("  - Cleaning up intermediate output file (if it wasn't moved on success)...")
                if intermediate_output_file.exists():
                    if processing_error_occurred:
                        print(f"    - WARNING: Processing error occurred. Intermediate output file '{intermediate_output_file}' is being preserved at its original path for inspection.")
                    else:
                        print(f"    - INFO: Intermediate output file '{intermediate_output_file}' found at original path despite no errors (expected to be moved). Cleaning up.")
                        intermediate_output_file.unlink(missing_ok=True)
                        print(f"    - Deleted intermediate output file from original path: {intermediate_output_file}")
                else:
                    if not processing_error_occurred:
                        print(f"    - Intermediate output file successfully moved (not found at original path, as expected): {intermediate_output_file}")
                    else:
                        print(f"    - Processing error occurred, and intermediate output file '{intermediate_output_file}' not found at original path (likely not created or cleaned by another step).")

            print(f"FINISHED LOG FOR: {file_path.name}")

        finally:
            runtime = datetime.now() - date_for_runtime_calc
            runtime_str = str(runtime).split(".")[0]
            print(f"\nTotal runtime for this file: {runtime_str}")
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass

            if sys.stdout != original_stdout_console:
                sys.stdout = original_stdout_console
            if sys.stderr != original_stderr_console:
                sys.stderr = original_stderr_console
            if log_file_handle:
                log_file_handle.close()

            if processing_error_occurred:
                failed_this_run.add(file_path.resolve())
                if file_path.exists():
                    failed_dest = DIR_FAILED / file_path.name
                    shutil.move(str(file_path), failed_dest)
                    original_stderr_console.write(
                        f"Moved to {failed_dest}. Video intermediates were kept so a retry can resume.\n"
                    )
                original_stderr_console.write(f"File: {file_path.name}\n")
                original_stderr_console.write(f"Log: {log_file_path}\n")
                original_stderr_console.write(f"Runtime: {runtime_str}\n")
            else:
                original_stdout_console.write(f"File: {file_path.name}\n")
                original_stdout_console.write(f"Log: {log_file_path}\n")
                original_stdout_console.write(f"Runtime: {runtime_str}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Batch-process MKV files with xav (SVT-AV1-Essential), an all-intra x264 intermediate, and the existing Opus audio path."
    )
    parser.add_argument("--no-downmix", action="store_true", help="Preserve original audio channel layout.")
    parser.add_argument("--preset", type=int, default=None, help=f"SVT-AV1 preset passed to xav -p (default: {XAV_PRESET}).")
    parser.add_argument("--crf", type=float, default=None, help="Optional SVT-AV1 CRF passed to xav -p. Omitted = xav/SVT default.")
    parser.add_argument("--workers", type=int, default=None, help=f"Override xav -w. Default is (cpu_count - {WORKER_CORES_RESERVED}) // 2.")
    parser.add_argument("--buff", type=int, default=None, help=f"xav -b extra pre-decoded chunks (default: {XAV_BUFF}).")
    parser.add_argument("--norm-i", type=float, default=None, help=f"Target integrated loudness in LUFS (default: {LOUDNESS_I}).")
    parser.add_argument("--norm-tp", type=float, default=None, help=f"True-peak ceiling in dBTP (default: {LOUDNESS_TP}).")
    args = parser.parse_args()
    main(
        no_downmix=args.no_downmix,
        preset=args.preset,
        crf=args.crf,
        workers=args.workers,
        buff=args.buff,
        norm_i=args.norm_i,
        norm_tp=args.norm_tp,
    )
