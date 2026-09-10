#!/usr/bin/env python3

# Note: This script is configured to use a custom version of SVT-AV1
# called "SVT-AV1-Essential" from https://github.com/nekotrix/SVT-AV1-Essential
#
# Batch encode: av1an is VIDEO ONLY. Audio, subs, attachments, mux: this script.
# Auto-detects SDR vs HDR and 1080p vs 4K.
# 1080p SDR: HandBrake x264 all-intra intermediate, then VapourSynth BT.709.
# 4K or HDR CFR: mkvmerge video-only remux (no re-encode; keeps HDR/DoVi track metadata).
# VFR 4K/HDR (rare): HandBrake x265_10bit CFR fallback.
# ffmpeg is only a fallback if HandBrake produces an empty 1080p SDR file.
# ≤1080p SDR: --preset 1. Above 1080p or HDR: --preset 2.
# --crf 30 always (overrides Essential --quality medium, which would be CRF 35 above 1080p).
# Av1an workers: (cpu_count // 2) - 1, not a fixed count.
# Audio: AAC/Opus remuxed. Else: Nightmode Dialogue pan (`<` so the mix cannot clip)
# → ffmpeg loudnorm 2-pass linear (I=-16, TP=-1.5, LRA=20) → opusenc.
# Final mkvmerge: av1an video + processed/remuxed audio + source subs/attachments/chapters.

import argparse
import json
import math
import multiprocessing as _multiprocessing_cropdetect
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter as _Counter_cropdetect
from datetime import datetime
from pathlib import Path

REQUIRED_TOOLS = [
    "ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit",
    "opusenc", "mediainfo", "av1an", "HandBrakeCLI", "ffmsindex",
]
DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs")
DIR_FAILED = Path("failed")
REMUX_CODECS = {"aac", "opus"}

PREP_SUFFIX = ".prep.mkv"
CFR_SUFFIX = ".cfr.mkv"
CFR_FULL_SUFFIX = ".cfr_full.mkv"

HEIGHT_4K = 1080
PRESET_1080 = 1
PRESET_4K = 2
DEFAULT_CRF = 30
DEFAULT_TUNE = 2
DEFAULT_LP = 2
TUNE_NAMES = {
    0: "VQ",
    1: "PSNR",
    2: "SSIM",
    3: "IQ (Image Quality)",
    4: "MS_SSIM",
}

LOUDNESS_I = -16.0
LOUDNESS_TP = -1.5
LOUDNESS_LRA = 20.0

SVT_AV1_BASE = {
    "preset": PRESET_1080,
    "crf": DEFAULT_CRF,
    "color-primaries": 1,
    "transfer-characteristics": 1,
    "matrix-coefficients": 1,
    "scd": 0,
    "scm": 0,
    "keyint": 0,
    "lp": DEFAULT_LP,
    "auto-tiling": 1,
    "tune": DEFAULT_TUNE,
    "progress": 2,
}

HDR_TRANSFER_MARKERS = (
    "smpte2084", "smpte st 2084", "pq", "bt.2100", "bt2100",
    "arib-std-b67", "arib std-b67", "hlg", "hybrid log-gamma",
)
HDR_FORMAT_MARKERS = ("hdr10", "hdr10+", "dolby vision", "dolbyvision", "hlg")
HLG_MARKERS = ("arib-std-b67", "arib std-b67", "hlg", "hybrid log-gamma")
HDR_TRACK_KEYS = (
    "HDR_Format", "HDR_Format_String", "HDR_Format_Compatibility",
    "transfer_characteristics", "TransferCharacteristics",
    "Transfer_characteristics", "colour_transfer", "color_transfer",
)


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


def video_fps(track, source_file=None):
    """MediaInfo original rate first. ffprobe r_frame_rate is often a fake 29.97 on MKV."""
    if track:
        orig_str = track.get("FrameRate_Original_String") or ""
        match = re.search(r"\((\d+/\d+)\)", str(orig_str))
        if match:
            return match.group(1)
        orig_num = track.get("FrameRate_Original_Num")
        orig_den = track.get("FrameRate_Original_Den")
        if orig_num and orig_den:
            return f"{orig_num}/{orig_den}"
        orig = track.get("FrameRate_Original")
        if orig:
            return str(orig).split()[0]
        num, den = track.get("FrameRate_Num"), track.get("FrameRate_Den")
        if num and den:
            return f"{num}/{den}"
        fr = track.get("FrameRate")
        if fr:
            return str(fr).split()[0]
    if source_file:
        try:
            raw = run_cmd([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "json", str(source_file),
            ], capture_output=True)
            streams = (json.loads(raw).get("streams") or [])
            if streams:
                for key in ("avg_frame_rate", "r_frame_rate"):
                    val = streams[0].get(key)
                    if val and val not in ("0/0", "0"):
                        return val
        except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return None


def is_4k_path(track):
    return video_height(track) > HEIGHT_4K


def _hdr_text(track):
    if not track:
        return ""
    parts = []
    for key in HDR_TRACK_KEYS:
        val = track.get(key)
        if val:
            parts.append(str(val).lower())
    return " ".join(parts)


def is_hdr(track):
    """True HDR (PQ/HLG/DoVi). 10-bit BT.709 Hi10p is SDR, not HDR."""
    text = _hdr_text(track)
    if any(m in text for m in HDR_FORMAT_MARKERS):
        return True
    return any(m in text for m in HDR_TRANSFER_MARKERS)


def is_hlg(track):
    return any(m in _hdr_text(track) for m in HLG_MARKERS)


def is_4k_or_hdr(track):
    return is_4k_path(track) or is_hdr(track)


def path_label(track):
    height = video_height(track)
    if is_hdr(track):
        kind = "HLG" if is_hlg(track) else "PQ/HDR10"
        return f"HDR {kind} (height={height})"
    if is_4k_path(track):
        return f"4K+ SDR (height={height})"
    return f"1080p or lower SDR (height={height})"


def svt_preset(track, override=None):
    if override is not None:
        return int(override)
    return PRESET_4K if is_4k_path(track) or is_hdr(track) else PRESET_1080


def svt_params_for_track(track, preset_override=None, crf_override=None, grain=None, tune_override=None):
    params = dict(SVT_AV1_BASE)
    params["preset"] = svt_preset(track, preset_override)
    params["crf"] = int(crf_override) if crf_override is not None else DEFAULT_CRF
    params["tune"] = int(tune_override) if tune_override is not None else DEFAULT_TUNE
    if is_hdr(track):
        params["color-primaries"] = 9
        params["matrix-coefficients"] = 9
        params["transfer-characteristics"] = 18 if is_hlg(track) else 16
    if grain is not None:
        params["film-grain"] = grain
    return params


def vs_matrix_in(track):
    return "2020ncl" if is_hdr(track) else "709"


def av1an_svt_param_string(params):
    return " ".join(f"--{key} {value}" for key, value in params.items())


def av1an_worker_count():
    total_cores = os.cpu_count() or 4
    workers = max(1, (total_cores // 2) - 1)
    return workers, total_cores


def intermediate_encoder(track):
    """Return (ffmpeg codec args, handbrake encoder, label, handbrake --encopts or None)."""
    ten = video_bit_depth(track) >= 10 or is_4k_or_hdr(track)
    if is_4k_or_hdr(track):
        ffmpeg_args = [
            "-c:v", "libx265",
            "-crf", "0",
            "-preset", "superfast",
            "-tune", "fastdecode",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "info=0",
        ]
        return ffmpeg_args, "x265_10bit", "libx265 10-bit CRF 0, normal GOP (VFR 4K/HDR fallback)", None
    if ten:
        ffmpeg_args = [
            "-c:v", "libx264",
            "-crf", "0",
            "-preset", "superfast",
            "-tune", "fastdecode",
            "-pix_fmt", "yuv420p10le",
            "-g", "1",
            "-bf", "0",
        ]
        return ffmpeg_args, "x264_10bit", "libx264 10-bit CRF 0 all-intra (1080p SDR Hi10p)", "keyint=1:bframes=0"
    ffmpeg_args = [
        "-c:v", "libx264",
        "-crf", "0",
        "-preset", "superfast",
        "-tune", "fastdecode",
        "-g", "1",
        "-bf", "0",
    ]
    return ffmpeg_args, "x264", "libx264 8-bit CRF 0 all-intra (1080p SDR)", "keyint=1:bframes=0"


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
                print(f"    - Warning: Could not parse fractional FPS '{target_cfr_fps}'. Sending source as-is.")
                is_vfr = False
    else:
        print("    - Warning: VFR detected, but could not determine target CFR. Sending source as-is.")
        is_vfr = False
    return is_vfr, target_cfr_fps


def handbrake_rate(track, source_file, vfr_target=None):
    """Always pass --rate. Prefer MediaInfo original FPS (never HandBrake's fake 29.97)."""
    raw = vfr_target or video_fps(track, source_file)
    if not raw:
        return None
    raw = str(raw).split()[0]
    if "/" in raw:
        try:
            num, den = map(float, raw.split("/", 1))
            if den:
                return f"{num / den:.3f}"
        except ValueError:
            return raw
    try:
        return f"{float(raw):.3f}"
    except ValueError:
        return raw


def strip_prep_tags(path):
    """HandBrake/ffmpeg can copy a video title; av1an would copy it again."""
    try:
        run_cmd([
            "mkvpropedit", str(path),
            "--delete", "title",
            "--edit", "track:v1",
            "--delete", "name",
        ])
    except subprocess.CalledProcessError as e:
        print(f"    - Warning: could not strip intermediate titles ({e}).")


def prep_is_vfr(path):
    try:
        mode = (video_track(mediainfo_json(path)) or {}).get("FrameRate_Mode") or ""
    except Exception:
        return False
    return str(mode).upper() in ("VFR", "VARIABLE")


def first_video_track_id(source_file):
    """mkvmerge track id of the first video track (BL if dual-layer DoVi)."""
    try:
        for t in mkvmerge_identify(source_file).get("tracks") or []:
            if t.get("type") == "video":
                return t.get("id", 0)
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        pass
    return 0


def run_mkvmerge_video_only(source_file, output_file):
    """Copy the video track only. No re-encode. Keeps HDR10/DoVi track properties."""
    vid = first_video_track_id(source_file)
    print(
        f"    - mkvmerge video-only remux (no re-encode, keep HDR/DoVi metadata), "
        f"video TID {vid}"
    )
    args = [
        "mkvmerge", "-o", str(output_file),
        "--title", "",
        "--no-audio",
        "--no-subtitles",
        "--no-buttons",
        "--no-attachments",
        "--no-chapters",
        "--no-global-tags",
        "--video-tracks", str(vid),
        "--track-name", f"{vid}:",
        str(source_file),
    ]
    print(f"    - Running mkvmerge: {' '.join(args)}")
    run_cmd(args)
    return file_is_usable(output_file)


def prep_is_handbrake_reencode(path):
    """Old 4K/HDR preps were HandBrake HEVC CRF 0. Remux path should not reuse those."""
    try:
        media = mediainfo_json(path)
        general = {}
        for t in media.get("media", {}).get("track", []):
            if t.get("@type") == "General":
                general = t
                break
        writing = " ".join(
            str(general.get(k) or "")
            for k in ("WritingApplication", "Encoded_Application", "Writing_library")
        )
        return "handbrake" in writing.lower()
    except Exception:
        return False


def run_handbrake_intermediate(source_file, output_file, track, target_fps):
    """Video-only CFR intermediate. All-intra for 1080p SDR; VFR 4K/HDR uses HEVC."""
    _ffmpeg_args, encoder, label, encopts = intermediate_encoder(track)
    print(f"    - HandBrakeCLI intermediate: encoder={encoder} ({label}), CFR {target_fps}")
    handbrake_args = [
        "HandBrakeCLI",
        "--input", str(source_file),
        "--output", str(output_file),
        "--cfr",
        "--rate", str(target_fps),
        "--encoder", encoder,
        "--quality", "0",
        "--encoder-preset", "superfast",
        "--encoder-tune", "fastdecode",
    ]
    if encopts:
        handbrake_args += ["--encopts", encopts]
    handbrake_args += [
        "--audio", "none",
        "--subtitle", "none",
        "--crop-mode", "none",
        "--no-markers",
    ]
    print(f"    - Running HandBrakeCLI: {' '.join(handbrake_args)}")
    run_cmd(handbrake_args)
    return file_is_usable(output_file)


def create_ffmpeg_intermediate(source_file, output_file, track):
    """Video-only all-intra CFR fallback. Strip metadata/chapters/titles."""
    video_args, _hb, label, _encopts = intermediate_encoder(track)
    fps = video_fps(track, source_file)
    print(f"    - Creating ffmpeg intermediate: {label} (forced CFR)")
    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-v", "error", "-stats", "-y",
        "-fflags", "+genpts",
        "-i", str(source_file),
        "-map", "0:v:0",
        *video_args,
        "-fps_mode", "cfr",
    ]
    if fps:
        ffmpeg_args += ["-r", str(fps)]
        print(f"    - Forcing CFR at {fps}")
    ffmpeg_args += [
        "-an", "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-metadata", "title=",
        "-metadata:s:v:0", "title=",
        str(output_file),
    ]
    print(f"    - Running ffmpeg: {' '.join(ffmpeg_args)}")
    run_ffmpeg_logged(ffmpeg_args)
    return file_is_usable(output_file)


def prepare_av1an_input(file_path, is_vfr, target_cfr_fps, track):
    """1080p SDR: HandBrake x264 all-intra. 4K/HDR CFR: mkvmerge video-only remux."""
    prep_file = Path(f"{file_path.stem}{PREP_SUFFIX}")
    temps = [prep_file]
    uhd_or_hdr = is_4k_or_hdr(track)
    if file_is_usable(prep_file):
        if prep_is_vfr(prep_file):
            print(f"    - Existing intermediate is VFR; deleting and remaking: {prep_file}")
            prep_file.unlink(missing_ok=True)
        elif uhd_or_hdr and not is_vfr and prep_is_handbrake_reencode(prep_file):
            print(
                f"    - Existing intermediate is a HandBrake re-encode; "
                f"deleting and remuxing video-only: {prep_file}"
            )
            prep_file.unlink(missing_ok=True)
        else:
            print(f"    - Reusing existing intermediate (resume): {prep_file}")
            return prep_file, temps

    if uhd_or_hdr and not is_vfr:
        if run_mkvmerge_video_only(file_path, prep_file):
            strip_prep_tags(prep_file)
            return prep_file, temps
        print("    - Warning: mkvmerge video-only remux failed. Falling back to HandBrake.")

    fps = handbrake_rate(track, file_path, target_cfr_fps if is_vfr else None)
    if fps:
        if run_handbrake_intermediate(file_path, prep_file, track, fps):
            strip_prep_tags(prep_file)
            return prep_file, temps
        print("    - Warning: HandBrakeCLI produced an empty file. Falling back to ffmpeg.")
    else:
        print("    - Warning: could not determine FPS for HandBrake. Falling back to ffmpeg.")

    if create_ffmpeg_intermediate(file_path, prep_file, track):
        strip_prep_tags(prep_file)
        return prep_file, temps
    print("    - Warning: ffmpeg intermediate failed. Sending source to av1an as-is.")
    return file_path, temps


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
    """Re-apply source audio/subtitle titles and flags."""
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


def stream_sample_rate(source_file, stream_index):
    """Source track sample rate in Hz. loudnorm leaks 192 kHz; we restore this for opusenc's header."""
    try:
        for stream in ffprobe_json(source_file).get("streams", []):
            if int(stream.get("index", -1)) != int(stream_index):
                continue
            rate = int(str(stream.get("sample_rate") or "0").split()[0])
            if rate > 0:
                return rate
    except (TypeError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return 48000


def apply_constant_gain_loudness(input_path, output_path, track_index, sample_rate=48000):
    """Two-pass ffmpeg loudnorm, linear (constant gain + true-peak). No asoftclip.

    loudnorm true-peak uses 4× oversampling (48 kHz → 192 kHz). Pin the FLAC back
    to the source rate so opusenc tags Input Sample Rate correctly (still encodes at 48 kHz).
    """
    print(f"    - Normalizing Audio Track #{track_index} (loudnorm 2-pass linear)...")
    print(
        f"      - Targets: I={LOUDNESS_I} LUFS, TP={LOUDNESS_TP} dBTP, "
        f"LRA={LOUDNESS_LRA} LU (linear; not a compressor)"
    )
    print(f"      - Restore sample rate after loudnorm: {sample_rate} Hz (source; Opus encode stays 48 kHz)")
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
        "-af", f"{loudnorm_apply},aformat=sample_fmts=s32:sample_rates={sample_rate}",
        "-ar", str(sample_rate),
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

    apply_constant_gain_loudness(
        temp_extracted,
        temp_normalized,
        index,
        sample_rate=stream_sample_rate(source_file, index),
    )

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


def process_audio_tracks(source_file, audio_temp_dir, no_downmix):
    """AAC/Opus remux. Other codecs: pan (optional) → LUFS → opusenc. Original order."""
    probe = ffprobe_json(source_file)
    mkv = mkvmerge_identify(source_file)
    media = mediainfo_json(source_file)
    mkv_audio = [t for t in mkv.get("tracks", []) if t.get("type") == "audio"]
    media_audio = {
        int(t.get("StreamOrder", -1)): t
        for t in media.get("media", {}).get("track", [])
        if t.get("@type") == "Audio"
    }
    plan = []
    audio_i = 0
    print("--- Starting Audio Processing ---")
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        idx = int(stream["index"])
        codec = (stream.get("codec_name") or "").lower()
        channels = stream.get("channels", 2)
        mkv_track = None
        for t in mkv_audio:
            if t.get("properties", {}).get("stream_id") == idx:
                mkv_track = t
                break
        if mkv_track is None and audio_i < len(mkv_audio):
            mkv_track = mkv_audio[audio_i]
        audio_i += 1
        props = (mkv_track or {}).get("properties") or {}
        mkv_id = (mkv_track or {}).get("id")
        language = props.get("language") or stream.get("tags", {}).get("language") or "und"
        title = props.get("track_name") or ""
        delay = 0
        delay_raw = (media_audio.get(idx) or {}).get("Video_Delay")
        if delay_raw is not None:
            try:
                delay_val = float(delay_raw)
                delay = int(round(delay_val * 1000 if delay_val < 1 else delay_val))
            except Exception:
                delay = 0
        print(f"Processing Audio Stream #{idx} (TID: {mkv_id}, Codec: {codec}, Channels: {channels}, Lang: {language})")
        if codec in REMUX_CODECS and mkv_id is not None:
            print("    - Remux (AAC/Opus), skip re-encode")
            plan.append({"kind": "remux", "mkv_id": str(mkv_id)})
        else:
            opus_path = convert_audio_track(idx, channels, audio_temp_dir, source_file, not no_downmix)
            plan.append({
                "kind": "encode",
                "path": opus_path,
                "language": language,
                "title": title,
                "delay": delay,
            })
    print("--- Finished Audio Processing ---")
    return plan


def mux_final(dest, encoded_video, source_file, audio_plan):
    """av1an video only + audio in source order + source subs/attachments/chapters."""
    extra = [
        "--no-video", "--no-subtitles", "--no-attachments",
        "--no-chapters", "--no-global-tags",
    ]
    args = [
        "mkvmerge", "-o", str(dest),
        "--title", "",
        "--track-name", "0:",
        "--no-audio", "--no-subtitles", "--no-attachments", "--no-chapters",
        str(encoded_video),
    ]
    for item in audio_plan:
        if item["kind"] == "remux":
            args += extra + ["--audio-tracks", item["mkv_id"], str(source_file)]
        else:
            sync = ["--sync", f"0:{item['delay']}"] if item.get("delay") else []
            args += [
                "--language", f"0:{item['language']}",
                "--track-name", f"0:{item.get('title') or ''}",
            ] + sync + [str(item["path"])]
    args += ["--no-video", "--no-audio", str(source_file)]
    print("Assembling final file with mkvmerge...")
    print(f"    - mkvmerge: {' '.join(args)}")
    run_cmd(args)
    if not file_is_usable(dest):
        raise RuntimeError(f"mkvmerge produced an empty file: {dest}")


# --- CROPDETECT LOGIC FROM cropdetect.py ---

COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_RESET = "\033[0m"

KNOWN_ASPECT_RATIOS = [
    {"name": "HDTV (16:9)", "ratio": 16 / 9},
    {"name": "Widescreen (Scope)", "ratio": 2.39},
    {"name": "Widescreen (Flat)", "ratio": 1.85},
    {"name": "IMAX Digital (1.90:1)", "ratio": 1.90},
    {"name": "Fullscreen (4:3)", "ratio": 4 / 3},
    {"name": "IMAX 70mm (1.43:1)", "ratio": 1.43},
]


def _check_prerequisites_cropdetect():
    for tool in ["ffmpeg", "ffprobe"]:
        if not shutil.which(tool):
            print(f"Error: '{tool}' command not found. Is it installed and in your PATH?")
            return False
    return True


def _analyze_segment_cropdetect(task_args):
    seek_time, input_file, width, height = task_args
    ffmpeg_args = [
        "ffmpeg", "-hide_banner",
        "-ss", str(seek_time),
        "-i", input_file, "-t", "1", "-vf", "cropdetect",
        "-f", "null", "-",
    ]
    result = subprocess.run(ffmpeg_args, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        return []
    crop_detections = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr)
    significant_crops = []
    for w_str, h_str, x_str, y_str in crop_detections:
        w, h, x, y = map(int, [w_str, h_str, x_str, y_str])
        significant_crops.append((f"crop={w}:{h}:{x}:{y}", seek_time))
    return significant_crops


def _snap_to_known_ar_cropdetect(w, h, x, y, video_w, video_h, tolerance=0.03):
    if h == 0:
        return f"crop={w}:{h}:{x}:{y}", None
    detected_ratio = w / h
    best_match = None
    smallest_diff = float("inf")
    for ar in KNOWN_ASPECT_RATIOS:
        diff = abs(detected_ratio - ar["ratio"])
        if diff < smallest_diff:
            smallest_diff = diff
            best_match = ar
    if not best_match or (smallest_diff / best_match["ratio"]) >= tolerance:
        return f"crop={w}:{h}:{x}:{y}", None
    if abs(w - video_w) < 16:
        new_h = round(video_w / best_match["ratio"])
        if new_h % 8 != 0:
            new_h = new_h + (8 - (new_h % 8))
        new_h = min(new_h, video_h)
        new_y = round((video_h - new_h) / 2)
        if new_y % 2 != 0:
            new_y -= 1
        new_y = max(0, new_y)
        return f"crop={video_w}:{new_h}:0:{new_y}", best_match["name"]
    if abs(h - video_h) < 16:
        new_w = round(video_h * best_match["ratio"])
        if new_w % 8 != 0:
            new_w = new_w + (8 - (new_w % 8))
        new_w = min(new_w, video_w)
        new_x = round((video_w - new_w) / 2)
        if new_x % 2 != 0:
            new_x -= 1
        new_x = max(0, new_x)
        return f"crop={new_w}:{video_h}:{new_x}:0", best_match["name"]
    return f"crop={w}:{h}:{x}:{y}", None


def _cluster_crop_values_cropdetect(crop_counts, tolerance=8):
    clusters = []
    temp_counts = crop_counts.copy()
    while temp_counts:
        center_str, _ = temp_counts.most_common(1)[0]
        try:
            _, values = center_str.split("=")
            cw, ch, cx, cy = map(int, values.split(":"))
        except (ValueError, IndexError):
            del temp_counts[center_str]
            continue
        cluster_total_count = 0
        crops_to_remove = []
        for crop_str, count in temp_counts.items():
            try:
                _, values = crop_str.split("=")
                w, h, x, y = map(int, values.split(":"))
                if abs(x - cx) <= tolerance and abs(y - cy) <= tolerance:
                    cluster_total_count += count
                    crops_to_remove.append(crop_str)
            except (ValueError, IndexError):
                continue
        if cluster_total_count > 0:
            clusters.append({"center": center_str, "count": cluster_total_count})
        for crop_str in crops_to_remove:
            del temp_counts[crop_str]
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def _parse_crop_string_cropdetect(crop_str):
    try:
        _, values = crop_str.split("=")
        w, h, x, y = map(int, values.split(":"))
        return {"w": w, "h": h, "x": x, "y": y}
    except (ValueError, IndexError):
        return None


def _calculate_bounding_box_cropdetect(crop_keys):
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for key in crop_keys:
        parsed = _parse_crop_string_cropdetect(key)
        if not parsed:
            continue
        w, h, x, y = parsed["w"], parsed["h"], parsed["x"], parsed["y"]
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x + w)
        max_y = max(max_y, y + h)
    if (max_x - min_x) <= 2 and (max_y - min_y) <= 2:
        return None
    return f"crop={max_x - min_x}:{max_y - min_y}:{min_x}:{min_y}"


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
    total_detections = sum(c["count"] for c in clusters)
    significant_clusters = []
    for cluster in clusters:
        percentage = (cluster["count"] / total_detections) * 100
        if percentage >= significant_crop_threshold:
            significant_clusters.append(cluster)
    for cluster in significant_clusters:
        parsed_crop = _parse_crop_string_cropdetect(cluster["center"])
        if parsed_crop:
            _, ar_label = _snap_to_known_ar_cropdetect(
                parsed_crop["w"], parsed_crop["h"], parsed_crop["x"], parsed_crop["y"], width, height
            )
            cluster["ar_label"] = ar_label
        else:
            cluster["ar_label"] = None
    if not significant_clusters:
        return None
    if len(significant_clusters) == 1:
        dominant_cluster = significant_clusters[0]
        parsed_crop = _parse_crop_string_cropdetect(dominant_cluster["center"])
        snapped_crop, ar_label = _snap_to_known_ar_cropdetect(
            parsed_crop["w"], parsed_crop["h"], parsed_crop["x"], parsed_crop["y"], width, height
        )
        parsed_snapped = _parse_crop_string_cropdetect(snapped_crop)
        if parsed_snapped and parsed_snapped["w"] == width and parsed_snapped["h"] == height:
            return None
        return snapped_crop
    crop_keys = [c["center"] for c in significant_clusters]
    bounding_box_crop = _calculate_bounding_box_cropdetect(crop_keys)
    if bounding_box_crop:
        parsed_bb = _parse_crop_string_cropdetect(bounding_box_crop)
        snapped_crop, ar_label = _snap_to_known_ar_cropdetect(
            parsed_bb["w"], parsed_bb["h"], parsed_bb["x"], parsed_bb["y"], width, height
        )
        parsed_snapped = _parse_crop_string_cropdetect(snapped_crop)
        if parsed_snapped and parsed_snapped["w"] == width and parsed_snapped["h"] == height:
            return None
        return snapped_crop
    return None


def detect_autocrop_filter(input_file, significant_crop_threshold=5.0, min_crop=10, debug=False):
    if not _check_prerequisites_cropdetect():
        return None
    try:
        probe_duration_args = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_file,
        ]
        duration_str = subprocess.check_output(probe_duration_args, stderr=subprocess.STDOUT, text=True)
        duration = int(float(duration_str))
        probe_res_args = [
            "ffprobe", "-v", "error",
            "-select_streams", "v",
            "-show_entries", "stream=width,height,disposition",
            "-of", "json",
            input_file,
        ]
        probe_output = subprocess.check_output(probe_res_args, stderr=subprocess.STDOUT, text=True)
        streams_data = json.loads(probe_output)
        video_stream = None
        for stream in streams_data.get("streams", []):
            if stream.get("disposition", {}).get("attached_pic", 0) == 0:
                video_stream = stream
                break
        if not video_stream or "width" not in video_stream or "height" not in video_stream:
            return None
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except Exception:
        return None
    return _analyze_video_cropdetect(
        input_file, duration, width, height, max(1, os.cpu_count() // 2),
        significant_crop_threshold, min_crop, debug,
    )


def convert_video(
    file_path,
    is_vfr,
    target_cfr_fps,
    track,
    autocrop_filter=None,
    preset_override=None,
    crf_override=None,
    grain=None,
    tune_override=None,
):
    print("  --- Starting Video Processing ---")
    vpy_file = Path(f"{file_path.stem}.vpy")
    encoded_video_file = Path(f"temp-{file_path.stem}.mkv")

    prep_file, temp_files = prepare_av1an_input(file_path, is_vfr, target_cfr_fps, track)
    temp_files.append(Path(f"{prep_file}.ffindex"))
    temp_files.append(Path(f"{prep_file}.lwi"))
    temp_files.append(vpy_file)
    temp_files.append(encoded_video_file)

    print("    - Indexing intermediate file with ffmsindex for VapourSynth...")
    run_cmd(["ffmsindex", "-f", str(prep_file)])

    prep_full_path = str(Path(prep_file).resolve())
    matrix_in = vs_matrix_in(track)
    vpy_lines = [
        "import vapoursynth as vs",
        "core = vs.core",
        "core.num_threads = 4",
        f"clip = core.ffms2.Source(source=r'''{prep_full_path}''')",
    ]
    if autocrop_filter:
        crop_match = re.match(r"crop=(\d+):(\d+):(\d+):(\d+)", autocrop_filter)
        if crop_match:
            cw, ch, cx, cy = crop_match.groups()
            vpy_lines.append(
                f"clip = core.std.CropAbs(clip, width={cw}, height={ch}, left={cx}, top={cy})"
            )
            print(f"    - Applying autocrop in VapourSynth: CropAbs(width={cw}, height={ch}, left={cx}, top={cy})")
    vpy_lines.extend([
        f'clip = core.resize.Point(clip, format=vs.YUV420P10, matrix_in_s="{matrix_in}") # type: ignore',
        "clip.set_output()",
    ])
    with vpy_file.open("w", encoding="utf-8") as f:
        f.write("\n".join(vpy_lines) + "\n")

    params = svt_params_for_track(
        track,
        preset_override=preset_override,
        crf_override=crf_override,
        grain=grain,
        tune_override=tune_override,
    )
    av1an_video_params_str = av1an_svt_param_string(params)
    workers, total_cores = av1an_worker_count()
    tune = params["tune"]
    print(f"    - Path: {path_label(track)}")
    print(
        f"    - Using {workers} workers for av1an "
        f"(Total Cores: {total_cores}, Logic: (Cores/2)-1)."
    )
    print(
        f"    - preset: {params['preset']}  crf: {params['crf']}  "
        f"tune: {tune} ({TUNE_NAMES.get(tune, '?')})  "
        f"matrix_in: {matrix_in}"
    )
    print(f"    - Using SVT-AV1 parameters: {av1an_video_params_str}")

    if file_is_usable(encoded_video_file):
        print(f"    - Reusing existing av1an output (resume): {encoded_video_file}")
        print("  --- Finished Video Processing ---")
        return encoded_video_file, temp_files

    print("    - Starting AV1 encode with av1an (this will take a long time)...")
    av1an_enc_args = [
        "av1an", "-i", str(vpy_file), "-o", str(encoded_video_file), "-n",
        "-e", "svt-av1", "--resume", "--sc-pix-format", "yuv420p", "-c", "mkvmerge",
        "--set-thread-affinity", "2", "--pix-format", "yuv420p10le", "--force", "--no-defaults",
        "-w", str(workers),
        "-v", av1an_video_params_str,
    ]
    run_cmd(av1an_enc_args)
    if not file_is_usable(encoded_video_file):
        raise RuntimeError(f"av1an finished but output is missing or empty: {encoded_video_file}")
    print("  --- Finished Video Processing ---")
    return encoded_video_file, temp_files


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
            or f.name.endswith(PREP_SUFFIX)
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
        current_dir / f"{file_path.stem}{PREP_SUFFIX}",
        current_dir / f"{file_path.stem}.vpy",
        current_dir / f"{file_path.stem}.prep.mkv.ffindex",
        current_dir / f"{file_path.stem}.prep.mkv.lwi",
        current_dir / f"{file_path.stem}.ut.mkv",
        current_dir / f"{file_path.stem}.ut.mkv.lwi",
        current_dir / f"{file_path.stem}.ut.mkv.ffindex",
        current_dir / f"{file_path.stem}.cfr_temp.mkv",
        current_dir / f"temp-{file_path.stem}.mkv",
        current_dir / f"output-{file_path.name}",
        current_dir / f"{file_path.name}.ffindex",
    ]
    for path in current_dir.glob(f"{file_path.stem}*_scd.txt"):
        if path not in files:
            files.append(path)
    for path in extra:
        if path and path not in files:
            files.append(path)
    return files


def main(no_downmix=False, autocrop=False, preset=None, crf=None, grain=None, tune=None, norm_i=None, norm_tp=None):
    check_tools()
    global LOUDNESS_I, LOUDNESS_TP
    if norm_i is not None:
        LOUDNESS_I = norm_i
    if norm_tp is not None:
        LOUDNESS_TP = norm_tp
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
        audio_temp_dir = None
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

            autocrop_filter = None
            if autocrop:
                print("--- Running autocrop detection ---")
                autocrop_filter = detect_autocrop_filter(str(file_path.resolve()))
                if autocrop_filter:
                    print(f"    - Autocrop filter detected: {autocrop_filter}")
                else:
                    print("    - No crop needed or detected.")

            encoded_video_file, extra_temps = convert_video(
                file_path,
                is_vfr,
                target_cfr_fps,
                track,
                autocrop_filter=autocrop_filter,
                preset_override=preset,
                crf_override=crf,
                grain=grain,
                tune_override=tune,
            )

            muxed = Path(f"output-{file_path.name}")
            extra_temps.append(muxed)
            if file_is_usable(muxed):
                print(f"    - Reusing remuxed output (resume): {muxed}")
            else:
                audio_temp_dir = tempfile.mkdtemp(prefix="audio_tmp_")
                print(f"Audio temporary directory created at: {audio_temp_dir}")
                audio_plan = process_audio_tracks(file_path, audio_temp_dir, no_downmix)
                mux_final(muxed, encoded_video_file, file_path, audio_plan)

            strip_titles(muxed)
            restore_track_meta(muxed, audio_meta, sub_meta)

            print("Moving files to final destinations...")
            shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
            shutil.move(str(muxed), DIR_COMPLETED / file_path.name)

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
            if audio_temp_dir and Path(audio_temp_dir).exists():
                shutil.rmtree(audio_temp_dir, ignore_errors=True)
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
        description=(
            "Batch-process MKV files with av1an + SVT-AV1-Essential. "
            "Auto-detects SDR/HDR and 1080p/4K. Audio is LUFS/Opus or AAC remux; "
            "final mkvmerge is this script."
        )
    )
    parser.add_argument(
        "--no-downmix",
        action="store_true",
        help="Keep surround on re-encoded tracks (no Nightmode Dialogue pan). AAC/Opus are always remuxed.",
    )
    parser.add_argument(
        "--autocrop",
        action="store_true",
        help="Automatically detect and crop black bars from video using cropdetect.",
    )
    parser.add_argument(
        "--preset",
        type=int,
        default=None,
        help=(
            f"Override SVT-AV1 preset. Default: {PRESET_1080} if height<={HEIGHT_4K} and SDR, "
            f"else {PRESET_4K}."
        ),
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=None,
        help=f"Override SVT-AV1 CRF. Default: {DEFAULT_CRF} for all resolutions (SDR and HDR).",
    )
    parser.add_argument(
        "--grain",
        type=int,
        help="Set the film-grain value (number). Adjusts the film grain synthesis level. (If omitted, grain synthesis is disabled.)",
    )
    parser.add_argument(
        "--tune",
        type=int,
        choices=sorted(TUNE_NAMES),
        default=None,
        help=(
            "SVT-AV1-Essential --tune: optimize the encoding process for different desired outcomes "
            "[0 = VQ, 1 = PSNR, 2 = SSIM, 3 = IQ (Image Quality), 4 = MS_SSIM] "
            f"(default: {DEFAULT_TUNE} = {TUNE_NAMES[DEFAULT_TUNE]})."
        ),
    )
    parser.add_argument(
        "--norm-i",
        type=float,
        default=None,
        help=f"Target integrated loudness in LUFS (default: {LOUDNESS_I}).",
    )
    parser.add_argument(
        "--norm-tp",
        type=float,
        default=None,
        help=f"True-peak ceiling in dBTP (default: {LOUDNESS_TP}).",
    )
    args = parser.parse_args()
    main(
        no_downmix=args.no_downmix,
        autocrop=args.autocrop,
        preset=args.preset,
        crf=args.crf,
        grain=args.grain,
        tune=args.tune,
        norm_i=args.norm_i,
        norm_tp=args.norm_tp,
    )
