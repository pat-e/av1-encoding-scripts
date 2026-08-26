#!/usr/bin/env python3

# This script is centered around the "xav" batch AV1 encoder tool.
# For more information and to install xav, visit: https://github.com/emrakyz/xav
#
# Batch encode: xav does video + audio. Intermediate only for VFR (HandBrake CFR).
# 1080p or lower: -p "--preset 1"  -w 4  -b 1
# Above 1080p:    -p "--preset 2"  -w 4  -b 1
# Default audio:  -a "norm <ids>"  (xav downmix+norm on non-AAC/Opus)
# --no-downmix:   -a "auto <ids>"  (keep layout on those tracks)
# AAC/Opus: never passed to xav; remuxed from the xav input after encode.
# CFR: xav reads the source MKV directly. No x264/HEVC remux.

import json
import re
import shutil
import subprocess
import sys
import argparse
from datetime import datetime
from pathlib import Path

REQUIRED_TOOLS = [
    "ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit",
    "mediainfo", "xav", "HandBrakeCLI",
]
DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs")
DIR_FAILED = Path("failed")
REMUX_CODECS = {"aac", "opus"}

XAV_ENCODER = "svt-av1"
XAV_BUFF = 1
XAV_WORKERS = 4
PRESET_1080 = 1
PRESET_4K = 2
HEIGHT_4K = 1080

CFR_SUFFIX = ".cfr.mkv"
CFR_FULL_SUFFIX = ".cfr_full.mkv"


class Tee:
    """Write to the log file and the real console at the same time."""

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


def file_is_usable(path):
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def mediainfo_json(path):
    raw = run_cmd(["mediainfo", "--Output=JSON", "-f", str(path)], capture_output=True)
    return json.loads(raw)


def video_track(media_info):
    if not (media_info.get("media") and media_info["media"].get("track")):
        return None
    for track in media_info["media"]["track"]:
        if track.get("@type") == "Video":
            return track
    return None


def video_height(track):
    if not track:
        return 0
    try:
        return int(float(str(track.get("Height", "0")).split()[0]))
    except (TypeError, ValueError):
        return 0


def video_bit_depth(track):
    if not track:
        return 8
    raw = track.get("BitDepth") or track.get("Bit_depth") or "8"
    try:
        return int(float(str(raw).split()[0]))
    except (TypeError, ValueError):
        return 8


def is_4k_path(track):
    return video_height(track) > HEIGHT_4K


def xav_worker_count(override=None):
    if override is not None:
        return max(1, int(override))
    return max(1, XAV_WORKERS)


def xav_preset(track, override=None):
    if override is not None:
        return int(override)
    return PRESET_4K if is_4k_path(track) else PRESET_1080


def detect_vfr(media_info):
    is_vfr = False
    target_cfr_fps = None
    track = video_track(media_info)
    if not track:
        return is_vfr, target_cfr_fps

    frame_rate_mode = track.get("FrameRate_Mode")
    if not (frame_rate_mode and frame_rate_mode.upper() in ["VFR", "VARIABLE"]):
        print("    - Video appears to be CFR or FrameRate_Mode not specified as VFR/Variable by MediaInfo.")
        return is_vfr, target_cfr_fps

    is_vfr = True
    print(f"    - Detected VFR based on MediaInfo FrameRate_Mode: {frame_rate_mode}")
    original_fps_str = track.get("FrameRate_Original_String")
    if original_fps_str:
        match = re.search(r"\((\d+/\d+)\)", original_fps_str)
        if match:
            target_cfr_fps = match.group(1)
        else:
            target_cfr_fps = track.get("FrameRate_Original")
    if not target_cfr_fps:
        target_cfr_fps = track.get("FrameRate_Original")
    if not target_cfr_fps:
        target_cfr_fps = track.get("FrameRate")
        if target_cfr_fps:
            print(f"    - Using MediaInfo FrameRate ({target_cfr_fps}) as fallback for HandBrake target FPS.")
    if target_cfr_fps:
        print(f"    - Target CFR for HandBrake: {target_cfr_fps}")
        if isinstance(target_cfr_fps, str) and "/" in target_cfr_fps:
            try:
                num, den = map(float, target_cfr_fps.split("/"))
                target_cfr_fps = f"{num / den:.3f}"
                print(f"    - Converted fractional FPS to decimal for HandBrake: {target_cfr_fps}")
            except ValueError:
                print(f"    - Warning: Could not parse fractional FPS '{target_cfr_fps}'. Sending source to xav as-is.")
                is_vfr = False
    else:
        print("    - Warning: VFR detected, but could not determine target CFR. Sending source to xav as-is.")
        is_vfr = False
    return is_vfr, target_cfr_fps


def source_has_attachments(path):
    try:
        data = json.loads(run_cmd(["mkvmerge", "-J", str(path)], capture_output=True))
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        return False
    return bool(data.get("attachments"))


def run_handbrake_cfr(source_file, output_file, target_cfr_fps, use_10bit):
    """VFR → CFR. Copy all audio + subtitles. Video re-encode (required to make CFR)."""
    encoder = "x265_10bit" if use_10bit else "x264"
    print(f"    - Source is VFR. HandBrakeCLI CFR ({target_cfr_fps}) encoder={encoder}, copy audio/subs...")
    handbrake_args = [
        "HandBrakeCLI",
        "--input", str(source_file),
        "--output", str(output_file),
        "--cfr",
        "--rate", str(target_cfr_fps),
        "--encoder", encoder,
        "--quality", "0",
        "--encoder-preset", "superfast",
        "--all-audio",
        "--aencoder", "copy",
        "--audio-copy-mask", "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,flac,opus,vorbis",
        "--all-subtitles",
        "--subtitle-burned", "none",
        "--markers",
        "--crop-mode", "none",
    ]
    print(f"    - Running HandBrakeCLI: {' '.join(handbrake_args)}")
    run_cmd(handbrake_args)
    return file_is_usable(output_file)


def attach_fonts_from_source(cfr_file, source_file, output_file):
    """HandBrake does not copy MKV font attachments. Pull them from the source."""
    print("    - Merging font/attachments from source into CFR file...")
    run_cmd([
        "mkvmerge", "-o", str(output_file),
        str(cfr_file),
        "--no-video", "--no-audio", "--no-subtitles", "--no-chapters", "--no-global-tags",
        str(source_file),
    ])
    return file_is_usable(output_file)


def prepare_xav_input(file_path, is_vfr, target_cfr_fps, track):
    """CFR: original file. VFR: HandBrake CFR MKV with audio/subs + source attachments."""
    if not (is_vfr and target_cfr_fps):
        print("    - CFR (or VFR skipped): xav will read the source file directly.")
        return file_path, []

    cfr_file = Path(f"{file_path.stem}{CFR_SUFFIX}")
    temps = [cfr_file]
    if file_is_usable(cfr_file):
        print(f"    - Reusing existing CFR intermediate (resume): {cfr_file}")
    else:
        use_10bit = video_bit_depth(track) >= 10 or is_4k_path(track)
        if not run_handbrake_cfr(file_path, cfr_file, target_cfr_fps, use_10bit):
            print("    - Warning: HandBrakeCLI produced an empty file. Sending source to xav as-is.")
            return file_path, temps

    if not source_has_attachments(file_path):
        print("    - No MKV attachments on source; using HandBrake output as xav input.")
        return cfr_file, temps

    full_file = Path(f"{file_path.stem}{CFR_FULL_SUFFIX}")
    temps.append(full_file)
    if file_is_usable(full_file):
        print(f"    - Reusing CFR+attachments (resume): {full_file}")
        return full_file, temps
    try:
        if attach_fonts_from_source(cfr_file, file_path, full_file):
            return full_file, temps
        print("    - Warning: attachment merge failed. Using HandBrake output without extra fonts.")
    except subprocess.CalledProcessError as e:
        print(f"    - Warning: attachment merge failed ({e}). Using HandBrake output without extra fonts.")
    return cfr_file, temps


def strip_titles(mkv_path):
    print("    - Clearing container and video-track titles...")
    try:
        run_cmd([
            "mkvpropedit", str(mkv_path),
            "--delete", "title",
            "--edit", "track:v1",
            "--delete", "name",
        ])
    except subprocess.CalledProcessError as e:
        print(f"    - Warning: mkvpropedit could not clear titles ({e}). Continuing.")


# mkvmerge -J property → mkvpropedit --set name. Missing JSON keys use these defaults.
TRACK_FLAG_MAP = (
    ("default_track", "flag-default", 0),
    ("forced_track", "flag-forced", 0),
    ("enabled_track", "flag-enabled", 1),
    ("flag_hearing_impaired", "flag-hearing-impaired", 0),
    ("flag_visual_impaired", "flag-visual-impaired", 0),
    ("flag_text_descriptions", "flag-text-descriptions", 0),
    ("flag_original", "flag-original", 0),
    ("flag_commentary", "flag-commentary", 0),
)


def collect_track_meta(path):
    """Audio/subtitle names and Matroska flags from the prepared source."""
    mkv = mkvmerge_identify(path)
    audio, subs = [], []
    for t in mkv.get("tracks", []):
        kind = t.get("type")
        if kind not in ("audio", "subtitles"):
            continue
        props = t.get("properties") or {}
        flags = {}
        for json_key, prop_name, default in TRACK_FLAG_MAP:
            if json_key in props:
                flags[prop_name] = 1 if props[json_key] else 0
            else:
                flags[prop_name] = default
        meta = {
            "name": props.get("track_name") or "",
            "language": props.get("language") or "und",
            "language_ietf": props.get("language_ietf") or "",
            "flags": flags,
        }
        if kind == "audio":
            audio.append(meta)
        else:
            subs.append(meta)
    return audio, subs


def _append_track_restore(args, selector, meta, label):
    name = meta.get("name") or ""
    flags = meta.get("flags") or {}
    language = meta.get("language") or "und"
    language_ietf = meta.get("language_ietf") or ""
    args += ["--edit", selector]
    if name:
        args += ["--set", f"name={name}"]
        shown = name
    else:
        args += ["--delete", "name"]
        shown = "(no title)"
    args += ["--set", f"language={language}"]
    if language_ietf:
        args += ["--set", f"language-ietf={language_ietf}"]
    bits = []
    for _json_key, prop_name, _default in TRACK_FLAG_MAP:
        value = flags.get(prop_name, 0)
        args += ["--set", f"{prop_name}={value}"]
        if value:
            bits.append(prop_name.replace("flag-", ""))
    extra = f" [{', '.join(bits)}]" if bits else ""
    ietf = f"/{language_ietf}" if language_ietf else ""
    print(f"      - {label}: {language}{ietf}  {shown}{extra}")


def restore_track_meta(mkv_path, audio_meta, sub_meta):
    """Re-apply source audio/subtitle titles and flags. xav does not keep them."""
    if not audio_meta and not sub_meta:
        return
    print("    - Restoring audio and subtitle language, titles, and flags from source...")
    args = ["mkvpropedit", str(mkv_path)]
    out = mkvmerge_identify(mkv_path)
    out_audio = [t for t in out.get("tracks", []) if t.get("type") == "audio"]
    out_subs = [t for t in out.get("tracks", []) if t.get("type") == "subtitles"]
    for i, meta in enumerate(audio_meta[: len(out_audio)], start=1):
        _append_track_restore(args, f"track:a{i}", meta, f"audio a{i}")
    for i, meta in enumerate(sub_meta[: len(out_subs)], start=1):
        _append_track_restore(args, f"track:s{i}", meta, f"subtitle s{i}")
    if args == ["mkvpropedit", str(mkv_path)]:
        return
    try:
        run_cmd(args)
    except subprocess.CalledProcessError as e:
        print(f"    - Warning: mkvpropedit could not restore track titles/flags ({e}). Continuing.")


def ffprobe_json(path):
    raw = run_cmd(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
    )
    return json.loads(raw)


def mkvmerge_identify(path):
    return json.loads(run_cmd(["mkvmerge", "-J", str(path)], capture_output=True))


def classify_audio_tracks(path):
    """AAC/Opus → remux (mkvmerge track id). Everything else → xav (ffmpeg stream index)."""
    probe = ffprobe_json(path)
    mkv = mkvmerge_identify(path)
    mkv_audio = [t for t in mkv.get("tracks", []) if t.get("type") == "audio"]
    encode_ids = []
    remux_ids = []
    plan = []
    audio_i = 0
    print("    - Audio tracks:")
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        idx = int(stream["index"])
        codec = (stream.get("codec_name") or "").lower()
        mkv_track = None
        for t in mkv_audio:
            if t.get("properties", {}).get("stream_id") == idx:
                mkv_track = t
                break
        if mkv_track is None and audio_i < len(mkv_audio):
            mkv_track = mkv_audio[audio_i]
        audio_i += 1
        mkv_id = mkv_track.get("id") if mkv_track else None
        if codec in REMUX_CODECS and mkv_id is not None:
            print(f"      - ffmpeg stream {idx} (TID {mkv_id}, {codec}): remux, skip xav")
            remux_ids.append(str(mkv_id))
            plan.append(("remux", str(mkv_id)))
        else:
            print(f"      - ffmpeg stream {idx} (TID {mkv_id}, {codec}): encode with xav")
            encode_ids.append(str(idx))
            plan.append(("encode", str(idx)))
    return encode_ids, remux_ids, plan


def build_xav_audio_arg(encode_ids, remux_ids, no_downmix):
    if not encode_ids:
        return None
    mode = "auto" if no_downmix else "norm"
    if not remux_ids:
        return f"{mode} all"
    return f"{mode} {','.join(encode_ids)}"


def mkv_audio_ids(path):
    mkv = mkvmerge_identify(path)
    return [str(t["id"]) for t in mkv.get("tracks", []) if t.get("type") == "audio"]


def mux_xav_with_remux(xav_output, remux_source, plan, dest):
    """Keep original audio order: xav-encoded tracks + AAC/Opus copies from remux_source."""
    xav_audio_ids = mkv_audio_ids(xav_output)
    encode_iter = iter(xav_audio_ids)
    extra = [
        "--no-video", "--no-subtitles", "--no-attachments",
        "--no-chapters", "--no-global-tags",
    ]
    args = [
        "mkvmerge", "-o", str(dest),
        "--title", "",
        "--track-name", "0:",
        "--no-audio",
        str(xav_output),
    ]
    for kind, ident in plan:
        if kind == "remux":
            args += extra + ["--audio-tracks", ident, str(remux_source)]
        else:
            xav_tid = next(encode_iter, None)
            if xav_tid is None:
                raise RuntimeError("xav output has fewer audio tracks than encoded stream IDs.")
            args += extra + ["--audio-tracks", xav_tid, str(xav_output)]
    print("    - Merging xav output with remuxed AAC/Opus in original track order...")
    print(f"    - mkvmerge: {' '.join(args)}")
    run_cmd(args)
    if not file_is_usable(dest):
        raise RuntimeError(f"mkvmerge produced an empty file: {dest}")


def run_xav(xav_input, xav_output, track, audio_arg, preset_override=None, workers_override=None, buff_override=None):
    if file_is_usable(xav_output):
        print(f"    - Reusing existing xav output (resume): {xav_output}")
        return

    preset = xav_preset(track, preset_override)
    workers = xav_worker_count(workers_override)
    buff = XAV_BUFF if buff_override is None else max(0, int(buff_override))
    path_label = "4K+" if is_4k_path(track) else "1080p or lower"
    print(f"    - Path: {path_label} (height={video_height(track)})")
    print(f"    - Workers: {workers}  preset: {preset}  -b {buff}  audio: {audio_arg or '(copy, no -a)'}")

    xav_args = [
        "xav",
        "-e", XAV_ENCODER,
        "-p", f"--preset {preset}",
        "-w", str(workers),
        "-b", str(buff),
    ]
    if audio_arg:
        xav_args += ["-a", audio_arg]
    xav_args += [str(xav_input), str(xav_output)]
    print("    - Starting xav (this will take a long time)...")
    print(f"    - xav command: {' '.join(xav_args)}")
    run_cmd(xav_args)
    if not file_is_usable(xav_output):
        raise RuntimeError(f"xav finished but output is missing or empty: {xav_output}")


def is_ffmpeg_decodable(file_path):
    try:
        subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(file_path),
            "-map", "0:v:0", "-t", "1", "-f", "null", "-",
        ], check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def list_source_mkvs(current_dir):
    return sorted(
        f for f in current_dir.glob("*.mkv")
        if not (
            f.name.endswith(CFR_SUFFIX)
            or f.name.endswith(CFR_FULL_SUFFIX)
            or f.name.endswith(".x264.mkv")
            or f.name.endswith(".hevc.mkv")
            or f.name.endswith(".ut.mkv")
            or f.name.endswith(".cfr_temp.mkv")
            or f.name.endswith("_xav.mkv")
            or f.name.startswith("temp-")
            or f.name.startswith("output-")
        )
    )


def video_temp_files(current_dir, file_path, extra):
    files = [
        current_dir / f"{file_path.stem}{CFR_SUFFIX}",
        current_dir / f"{file_path.stem}{CFR_FULL_SUFFIX}",
        current_dir / f"temp-{file_path.stem}.mkv",
        current_dir / f"output-{file_path.name}",
        current_dir / f"{file_path.stem}_scd.txt",
        current_dir / f"{file_path.stem}.cfr_scd.txt",
        current_dir / f"{file_path.stem}.cfr_full_scd.txt",
    ]
    for path in extra:
        if path and path not in files:
            files.append(path)
    return files


def main(no_downmix=False, preset=None, workers=None, buff=None):
    check_tools()
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
            print(f"ERROR: ffmpeg cannot decode video in '{file_path.name}'. Skipping this file.", file=sys.stderr)
            shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
            continue

        print("-" * shutil.get_terminal_size(fallback=(80, 24)).columns)
        log_file_path = DIR_CONV_LOGS / f"{file_path.stem}.log"
        original_stdout_console = sys.stdout
        original_stderr_console = sys.stderr
        print(f"Processing: {file_path.name}", file=original_stdout_console)
        print(f"Logging output to: {log_file_path}", file=original_stdout_console)
        log_file_handle = None
        processing_error_occurred = False
        date_for_runtime_calc = datetime.now()
        extra_temps = []
        try:
            log_file_handle = open(log_file_path, "w", encoding="utf-8", buffering=1)
            sys.stdout = Tee(log_file_handle, original_stdout_console)
            sys.stderr = Tee(log_file_handle, original_stderr_console)
            print(f"STARTING LOG FOR: {file_path.name}")
            print(f"Processing started at: {date_for_runtime_calc}")
            print(f"Full input file path: {file_path.resolve()}")
            print("-" * shutil.get_terminal_size(fallback=(80, 24)).columns)

            print(f"Analyzing file: {file_path.resolve()}")
            media_info = mediainfo_json(file_path)
            track = video_track(media_info)
            audio_meta, sub_meta = collect_track_meta(file_path)
            is_vfr, target_cfr_fps = detect_vfr(media_info)
            xav_input, extra_temps = prepare_xav_input(file_path, is_vfr, target_cfr_fps, track)
            encode_ids, remux_ids, plan = classify_audio_tracks(xav_input)
            audio_arg = build_xav_audio_arg(encode_ids, remux_ids, no_downmix)
            xav_output = Path(f"temp-{file_path.stem}.mkv")

            run_xav(
                xav_input,
                xav_output,
                track,
                audio_arg,
                preset_override=preset,
                workers_override=workers,
                buff_override=buff,
            )

            final_file = xav_output
            if encode_ids and remux_ids:
                muxed = Path(f"output-{file_path.name}")
                extra_temps.append(muxed)
                if file_is_usable(muxed):
                    print(f"    - Reusing remuxed output (resume): {muxed}")
                else:
                    mux_xav_with_remux(xav_output, xav_input, plan, muxed)
                final_file = muxed
            elif not encode_ids:
                print("    - All audio is AAC/Opus (or none to encode); xav copied audio, no extra remux.")

            strip_titles(final_file)
            restore_track_meta(final_file, audio_meta, sub_meta)

            print("Moving files to final destinations...")
            shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
            shutil.move(str(final_file), DIR_COMPLETED / file_path.name)

            print("Cleaning up temporary files (after successful processing)...")
            for temp_vid_file in video_temp_files(current_dir, file_path, extra_temps):
                if temp_vid_file.exists() and temp_vid_file.resolve() != (DIR_COMPLETED / file_path.name).resolve():
                    print(f"    Deleting: {temp_vid_file}")
                    temp_vid_file.unlink(missing_ok=True)

        except Exception as e:
            print(f"ERROR: An error occurred while processing '{file_path.name}': {e}", file=sys.stderr)
            original_stderr_console.write(
                f"ERROR during processing of '{file_path.name}': {e}\nSee log '{log_file_path}' for details.\n"
            )
            processing_error_occurred = True
        finally:
            runtime = datetime.now() - date_for_runtime_calc
            runtime_str = str(runtime).split(".")[0]
            print(f"FINISHED LOG FOR: {file_path.name}")
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
                        f"Moved to {failed_dest}. Intermediates were kept so a retry can resume.\n"
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
        description="Batch-process prepared MKV files with xav only. VFR gets a HandBrake CFR pass; CFR goes straight to xav."
    )
    parser.add_argument(
        "--no-downmix",
        action="store_true",
        help='Keep surround on tracks xav encodes (-a "auto IDs"). AAC/Opus are always remuxed. Default is -a "norm IDs".',
    )
    parser.add_argument(
        "--preset",
        type=int,
        default=None,
        help=f"Override SVT-AV1 preset. Default: {PRESET_1080} if height<={HEIGHT_4K}, else {PRESET_4K}.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"Override xav -w. Default: {XAV_WORKERS}.",
    )
    parser.add_argument(
        "--buff",
        type=int,
        default=None,
        help=f"xav -b extra pre-decoded chunks (default: {XAV_BUFF}).",
    )
    args = parser.parse_args()
    main(
        no_downmix=args.no_downmix,
        preset=args.preset,
        workers=args.workers,
        buff=args.buff,
    )
