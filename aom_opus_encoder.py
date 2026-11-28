#!/usr/bin/env python3

# Note: This script is configured to use a custom version of aom
# called "aom-psy101" from https://gitlab.com/damian101/aom-psy101
#
# Batch encode: av1an is VIDEO ONLY. Audio, subs, attachments, mux: this script.
# Auto-detects SDR vs HDR and 1080p vs 4K (same intermediate/automation as svt_opus_encoder.py).
# 1080p SDR: HandBrake x264 all-intra intermediate, then VapourSynth BT.709.
# 4K or HDR CFR: mkvmerge video-only remux (no re-encode; keeps HDR/DoVi track metadata).
# VFR 4K/HDR (rare): HandBrake x265_10bit CFR fallback.
# ffmpeg is only a fallback if HandBrake produces an empty 1080p SDR file.
# --cq-level 25 always (overridable with --crf). Two-pass av1an.
# Av1an workers: (cpu_count // 2) - 1, not a fixed count.
# Audio: AAC/Opus remuxed. Else: Nightmode Dialogue pan (`<` so the mix cannot clip;
# skip globally with --no-downmix or per file with a *nodownmix.mkv tag, kept on output)
# → ffmpeg loudnorm 2-pass linear (I=-18, TP=-1.5, LRA=20) → opusenc.
# Final mkvmerge: av1an video + processed/remuxed audio + source subs/chapters.
# Font attachments: only those referenced by remaining ASS/SSA (skip with --nofontsclean / -nfc).
# Matching ignores case, spaces, hyphens, and underscores, and one leading @. Every face
# in a .ttf/.otf/.ttc is checked, including variable-font instances. A font that cannot
# be read is kept. Byte-identical font attachments are kept once.
# Encode log lines FONT_CLEAN kept/dropped/missing use the font's full name, not the attachment filename.
# Non-font attachments are always kept.

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter as _Counter_cropdetect
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor_cropdetect
from datetime import datetime
from pathlib import Path

REQUIRED_TOOLS = [
    "ffmpeg", "ffprobe", "mkvmerge", "mkvextract", "mkvpropedit",
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
NODOWNMIX_TAG_RE = re.compile(r"(?:^|[._\-\s])nodownmix$", re.IGNORECASE)

HEIGHT_4K = 1080
DEFAULT_CQ = 25

LOUDNESS_I = -18.0
LOUDNESS_TP = -1.5
LOUDNESS_LRA = 20.0

AOM_AV1_BASE = {
    "bit-depth": 10,
    "cpu-used": 2,
    "good": "",
    "end-usage": "q",
    "cq-level": DEFAULT_CQ,
    "min-q": 8,
    "threads": 2,
    "tune-content": "psy",
    "tune": "psnr",
    "sharpness": 2,
    "arnr-maxframes": 7,
    "arnr-strength": 2,
    "quant-b-adapt": 1,
    "frame-parallel": 1,
    "tile-columns": 1,
    "gf-max-pyr-height": 5,
    "deltaq-mode": 2,
    "enable-keyframe-filtering": 0,
    "disable-kf": "",
    "enable-fwd-kf": 1,
    "kf-max-dist": 9999,
    "sb-size": "64",
    "enable-chroma-deltaq": 1,
    "enable-qm": 1,
    "lag-in-frames": 64,
    "color-primaries": "bt709",
    "transfer-characteristics": "bt709",
    "matrix-coefficients": "bt709",
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


def fonttools_available():
    try:
        import fontTools.ttLib  # noqa: F401
        return True
    except ImportError:
        return False


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


def aom_params_for_track(track, cq_override=None):
    params = dict(AOM_AV1_BASE)
    params["cq-level"] = int(cq_override) if cq_override is not None else DEFAULT_CQ
    if is_hdr(track):
        params["color-primaries"] = "bt2020"
        params["matrix-coefficients"] = "bt2020ncl"
        params["transfer-characteristics"] = "arib-std-b67" if is_hlg(track) else "smpte2084"
    return params


def vs_matrix_in(track):
    return "2020ncl" if is_hdr(track) else "709"


def av1an_aom_param_string(params):
    return " ".join(
        f"--{key}={value}" if value != "" else f"--{key}"
        for key, value in params.items()
    )


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


def has_nodownmix_tag(file_path):
    """True when the filename ends with a case-insensitive 'nodownmix' tag before the extension."""
    return bool(NODOWNMIX_TAG_RE.search(Path(file_path).stem))


def resolve_file_no_downmix(file_path, global_no_downmix):
    """Return (no_downmix_bool, log_reason) for one file."""
    tagged = has_nodownmix_tag(file_path)
    if global_no_downmix and tagged:
        return True, "yes (--no-downmix flag + filename tag)"
    if global_no_downmix:
        return True, "yes (--no-downmix flag)"
    if tagged:
        return True, "yes (filename tag)"
    return False, "no (default)"


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
    print(f"    - Audio downmix: {'disabled (no-downmix)' if no_downmix else 'enabled (5.1/7.1 → stereo)'}")
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


def mux_final(dest, encoded_video, source_file, audio_plan, clean_fonts=True, work_dir=None):
    """av1an video only + audio in source order + source subs/chapters.

    Default: drop font attachments not referenced by remaining ASS/SSA.
    Matching ignores case, spaces, hyphens, and underscores. Every face in a
    collection is checked, including variable-font instances. Unreadable fonts
    stay. Non-font attachments stay. --nofontsclean copies every attachment.
    """
    extra = [
        "--no-video", "--no-subtitles", "--no-attachments",
        "--no-chapters", "--no-global-tags",
    ]
    source_tail = ["--no-video", "--no-audio"]
    attach_args = []
    if clean_fonts:
        kept = fonts_to_keep(source_file, work_dir)
        if kept is not None:
            source_tail.append("--no-attachments")
            attach_args = attachment_merge_args(kept)
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
    args += source_tail + [str(source_file)] + attach_args
    print("Assembling final file with mkvmerge...")
    print(f"    - mkvmerge: {' '.join(args)}")
    run_cmd(args)
    if not file_is_usable(dest):
        raise RuntimeError(f"mkvmerge produced an empty file: {dest}")


FONT_MIME_MARKERS = ("font", "truetype", "opentype", "sfnt")
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".tte", ".dfont", ".woff", ".woff2"}


def is_font_attachment(att):
    mime = str(att.get("content_type") or "").lower()
    if any(marker in mime for marker in FONT_MIME_MARKERS):
        return True
    suffix = Path(str(att.get("file_name") or "")).suffix.lower()
    return suffix in FONT_EXTENSIONS


def safe_font_filename(name):
    return "".join(c for c in name if c.isalpha() or c.isdigit() or c in " .-_").rstrip() or "font"


def _remember_font_name(bucket, name):
    if name:
        bucket.setdefault(name.casefold(), name)


def log_font_clean(kept, dropped, missing):
    """One encode-log line per list. Names are separated with '; '.

    Commas appear in real font names, so they are not the delimiter.
    """
    for label, bucket in (("kept", kept), ("dropped", dropped), ("missing", missing)):
        names = sorted(bucket.values(), key=str.casefold)
        shown = "; ".join(names) if names else "(none)"
        print(f"    - FONT_CLEAN {label}: {shown}")


_FONT_NAME_IDS = (1, 2, 4, 6, 16, 17)
_ASS_STYLE_SECTIONS = {"[v4+ styles]", "[v4 styles]"}
_ASS_FN_TAG = re.compile(r"\\fn([^\\}]+)")


def normalize_font_key(name):
    """Compare font names ignoring case, spaces, hyphens, and underscores.

    A leading @ is the ASS marker for vertical text and is not part of the
    family name. Only one leading marker is removed.
    """
    text = name.strip().replace("\ufeff", "").replace("\x00", "")
    if text.startswith("@") or text.startswith("\uff20"):
        text = text[1:].strip()
    return "".join(ch for ch in text.casefold() if not ch.isspace() and ch not in "-_")


def _read_text(path):
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="ignore")
    return raw.decode("utf-8", errors="ignore")


def read_ass_font_names(ass_path):
    """Fonts requested by Style lines and inline \\fn tags, in original spelling.

    Names come from every [V4 Styles] / [V4+ Styles] line and from every \\fn
    override in [Events], including Comment lines. The caller strips a leading
    @ when matching.
    """
    fonts = set()
    in_styles = False
    in_events = False
    fontname_idx = -1

    for raw_line in _read_text(ass_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            header = line.lower()
            in_styles = header in _ASS_STYLE_SECTIONS
            in_events = header == "[events]"
            fontname_idx = -1
            continue
        if in_styles:
            lowered = line.lower()
            if lowered.startswith("format:"):
                columns = [part.strip().lower() for part in line.split(":", 1)[1].split(",")]
                fontname_idx = columns.index("fontname") if "fontname" in columns else -1
            elif lowered.startswith("style:") and fontname_idx != -1:
                columns = [part.strip() for part in line.split(":", 1)[1].split(",")]
                if len(columns) > fontname_idx and columns[fontname_idx]:
                    fonts.add(columns[fontname_idx])
        elif in_events:
            for match in _ASS_FN_TAG.findall(line):
                name = match.strip()
                if name:
                    fonts.add(name)
    return fonts


def _clean_font_name(text):
    if not text:
        return None
    cleaned = text.replace("\x00", "").replace("\ufeff", "").strip()
    if not cleaned or len(cleaned) > 512:
        return None
    if "\n" in cleaned or "\r" in cleaned:
        return None
    return cleaned


def _name_strings(font, name_id):
    if not isinstance(name_id, int) or name_id <= 0 or name_id == 0xFFFF:
        return []
    if "name" not in font:
        return []
    found = []
    seen = set()
    for record in font["name"].names:
        if record.nameID != name_id:
            continue
        try:
            text = _clean_font_name(record.toUnicode())
        except Exception:
            continue
        if text and text not in seen:
            seen.add(text)
            found.append(text)
    return found


def _combine_font_names(names, families, subfamilies):
    for family in families:
        for subfamily in subfamilies:
            if family.casefold() == subfamily.casefold():
                continue
            names.add(f"{family} {subfamily}")


def _display_name_score(record):
    """Prefer the English full name, then typographic family, then family."""
    if record.nameID not in (1, 4, 16):
        return None
    score = {4: 300, 16: 200, 1: 100}[record.nameID]
    platform = getattr(record, "platformID", None)
    lang = getattr(record, "langID", None)
    if platform == 3 and lang == 0x409:
        score += 30
    elif platform == 3:
        score += 20
    elif platform == 1 and lang in (0, 0x409):
        score += 10
    return score


def _face_match_names(font):
    """Names one face can be requested by, plus the best log name and its score.

    Matching uses the family, subfamily, full name, PostScript name, and
    typographic family/subfamily in every language, plus family + subfamily
    and each variable-font named instance.
    """
    by_id = {name_id: _name_strings(font, name_id) for name_id in _FONT_NAME_IDS}
    names = set()
    for values in by_id.values():
        names.update(values)

    legacy_family = by_id[1]
    legacy_sub = by_id[2]
    typo_family = by_id[16]
    typo_sub = by_id[17]
    _combine_font_names(names, legacy_family, legacy_sub)
    _combine_font_names(names, typo_family, typo_sub)
    _combine_font_names(names, legacy_family, typo_sub)
    _combine_font_names(names, typo_family, legacy_sub)

    families = []
    for family in legacy_family + typo_family:
        if family not in families:
            families.append(family)

    if "fvar" in font:
        for instance in font["fvar"].instances:
            styles = _name_strings(font, instance.subfamilyNameID)
            for style in styles:
                names.add(style)
                for family in families:
                    names.add(f"{family} {style}")
            ps_id = getattr(instance, "postscriptNameID", 0xFFFF)
            for ps_name in _name_strings(font, ps_id):
                names.add(ps_name)

    display = None
    best = -1
    if "name" in font:
        for record in font["name"].names:
            score = _display_name_score(record)
            if score is None or score <= best:
                continue
            try:
                text = _clean_font_name(record.toUnicode())
            except Exception:
                continue
            if text:
                best = score
                display = text
    return {name for name in names if name.strip()}, display, best


def _open_font_faces(path):
    """Return (closable, faces) for a single font or a collection.

    .ttc/.otc files start with 'ttcf' and hold more than one face. Reading
    only face 0 drops later faces that the subtitles actually use.
    """
    from fontTools.ttLib import TTCollection, TTFont

    with Path(path).open("rb") as handle:
        header = handle.read(4)
    if header == b"ttcf":
        collection = TTCollection(str(path), lazy=True)
        return collection, list(collection.fonts)
    font = TTFont(str(path), lazy=True)
    return font, [font]


def _short_error(exc):
    text = str(exc).strip().splitlines()
    message = text[0] if text else exc.__class__.__name__
    if len(message) > 200:
        return message[:197] + "..."
    return message


def _collect_font_names(path):
    """Read every face. Raise if the file yields no names at all.

    Returns (names, display name or None, partial error or None).
    """
    opener, faces = _open_font_faces(path)
    try:
        if not faces:
            raise RuntimeError("no faces in font file")
        names = set()
        failures = []
        display = None
        best = -1
        for index, face in enumerate(faces):
            try:
                face_names, face_display, face_score = _face_match_names(face)
            except Exception as exc:
                failures.append(f"face {index}: {_short_error(exc)}")
                continue
            names.update(face_names)
            if face_display and face_score > best:
                best = face_score
                display = face_display
        if not names:
            detail = "; ".join(failures) if failures else "no readable font names"
            raise RuntimeError(detail)
        partial = "; ".join(failures) if failures else None
        return names, display, partial
    finally:
        close = getattr(opener, "close", None)
        if close is not None:
            close()


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _FontInfo:
    def __init__(self, file_name, attachment_id, order, attachment):
        self.file_name = file_name
        self.attachment_id = attachment_id
        self.order = order
        self.attachment = attachment
        self.names = set()
        self.display = None
        self.content_hash = None
        self.problem = None
        self.partial_error = None


class _FontPlan:
    def __init__(self, decisions, warnings, unmatched):
        self.decisions = decisions
        self.warnings = warnings
        self.unmatched = unmatched

    @property
    def keep_count(self):
        return sum(1 for _order, action, _reason in self.decisions if action == "keep")

    @property
    def remove_count(self):
        return sum(1 for _order, action, _reason in self.decisions if action == "remove")


def _unique_requested(requested):
    seen = set()
    ordered = []
    for raw in requested:
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    ordered.sort(key=lambda item: (normalize_font_key(item), item.casefold(), item))
    return ordered


def _match_labels(keys, requested_by_key):
    labels = []
    seen = set()
    for key in sorted(keys & requested_by_key.keys()):
        for label in requested_by_key[key]:
            if label not in seen:
                seen.add(label)
                labels.append(label)
    return labels


def _display_font_name(name):
    """Drop one leading ASS vertical-text marker for the match reason."""
    text = name.strip()
    if text.startswith("@") or text.startswith("\uff20"):
        return text[1:].strip()
    return text


def _format_match_reason(labels):
    shown_labels = []
    seen = set()
    for label in labels:
        display = _display_font_name(label)
        if display and display not in seen:
            seen.add(display)
            shown_labels.append(display)
    shown = ", ".join(f'"{label}"' for label in shown_labels[:4])
    extra = len(shown_labels) - 4
    if extra > 0:
        shown += f" and {extra} more"
    noun = "font" if len(shown_labels) == 1 else "fonts"
    return f"matches requested {noun} {shown}"


def _decide_fonts(fonts, requested, keep_unused=False, unused_reason="not used by any subtitle"):
    """Choose keep or remove for each font.

    A font is kept when any face matches a requested name, when its file name
    matches, or when its contents could not be read. Byte-identical fonts are
    collapsed to the earliest attachment. Files whose names differ only by
    case but whose bytes differ are both kept and reported.

    Each decision is (order, action, reason).
    """
    unique_requested = _unique_requested(requested)
    requested_by_key = {}
    for label in unique_requested:
        key = normalize_font_key(label)
        if not key:
            continue
        requested_by_key.setdefault(key, [])
        if label not in requested_by_key[key]:
            requested_by_key[key].append(label)

    font_keys = {}
    for font in fonts:
        keys = set()
        candidates = set(font.names)
        # The file name is only a hint after the font itself was readable.
        if not font.problem:
            stem = Path(font.file_name).stem
            if stem:
                candidates.add(stem)
        for name in candidates:
            key = normalize_font_key(name)
            if key:
                keys.add(key)
        font_keys[font.order] = keys

    actions = {}
    warnings = []
    for font in fonts:
        labels = _match_labels(font_keys[font.order], requested_by_key)
        # An unreadable file is kept even when its name looks like a match.
        if font.problem:
            actions[font.order] = (
                "keep",
                f"WARNING: could not read font ({font.problem}); keeping it",
            )
        elif labels:
            actions[font.order] = ("keep", _format_match_reason(labels))
            if font.partial_error:
                warnings.append(
                    f'"{font.file_name}" matched a subtitle font, but face '
                    f"reading was incomplete ({font.partial_error})"
                )
        elif font.partial_error:
            actions[font.order] = (
                "keep",
                "WARNING: could not read every face "
                f"({font.partial_error}); keeping it",
            )
        elif keep_unused:
            actions[font.order] = ("keep", unused_reason)
        else:
            actions[font.order] = ("remove", unused_reason)

    by_filename = {}
    for font in fonts:
        by_filename.setdefault(font.file_name.casefold(), []).append(font)
    for group in by_filename.values():
        if len(group) < 2:
            continue
        hashes = {font.content_hash for font in group if font.content_hash}
        if len(hashes) > 1:
            quoted = " and ".join(f'"{font.file_name}"' for font in group)
            warnings.append(
                f"{quoted} differ only by case but their contents differ; "
                "not treating them as duplicates"
            )

    by_hash = {}
    for font in fonts:
        action, _reason = actions[font.order]
        if action == "keep" and font.content_hash:
            by_hash.setdefault(font.content_hash, []).append(font)
    for group in by_hash.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda font: font.order)
        primary = group[0]
        for duplicate in group[1:]:
            actions[duplicate.order] = (
                "remove",
                f'duplicate of "{primary.file_name}" (identical content)',
            )

    covered = set()
    for font in fonts:
        covered.update(font_keys[font.order] & requested_by_key.keys())
    unmatched = [
        label
        for label in unique_requested
        if normalize_font_key(label) and normalize_font_key(label) not in covered
    ]
    decisions = [
        (font.order, actions[font.order][0], actions[font.order][1]) for font in fonts
    ]
    return _FontPlan(decisions, warnings, unmatched)


def _font_log_name(font):
    """Encode-log name: full name, not the attachment filename."""
    if font.display:
        return font.display
    if font.names:
        return sorted(font.names, key=str.casefold)[0]
    return f"(unreadable: {font.file_name or 'font'})"


def _extract_attachment(source_file, attachment_id, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["mkvextract", "attachments", str(source_file), f"{attachment_id}:{dest}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode > 1 or not dest.is_file():
        detail = (result.stderr or result.stdout or "mkvextract failed").strip().splitlines()
        message = detail[-1] if detail else "mkvextract failed"
        raise RuntimeError(message)


def _inspect_attached_font(info, dest):
    try:
        info.content_hash = _file_sha256(dest)
    except OSError as exc:
        info.problem = _short_error(exc)
        return info
    try:
        names, display, partial = _collect_font_names(dest)
    except Exception as exc:
        info.problem = _short_error(exc)
        return info
    info.names = names
    info.display = display
    info.partial_error = partial
    return info


def _mkv_option_value(value):
    """A value that starts with @ would be read as an mkvmerge options file."""
    text = str(value)
    if text.startswith("@"):
        return "@" + text
    return text


def _ass_tracks(info):
    tracks = []
    for track in info.get("tracks") or []:
        if track.get("type") != "subtitles":
            continue
        codec_id = str((track.get("properties") or {}).get("codec_id") or "")
        codec = str(track.get("codec") or "")
        if "S_TEXT/ASS" in codec_id or "S_TEXT/SSA" in codec_id or "SubStationAlpha" in codec:
            tracks.append(track)
    return tracks


def _ensure_work_dir(work_dir):
    if work_dir:
        path = Path(work_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="font_tmp_"))


def collect_referenced_fonts(source_file, info, work_dir):
    """ASS/SSA font names.

    Returns (spellings, error or None, has_ass_tracks). On error the spelling
    list is empty and the caller keeps every font.
    """
    ass_tracks = _ass_tracks(info)
    if not ass_tracks:
        return [], None, False
    temp_dir = _ensure_work_dir(work_dir)
    extract = ["mkvextract", "tracks", str(source_file)]
    ass_files = []
    for track in ass_tracks:
        out_ass = temp_dir / f"subs_{track['id']}.ass"
        extract.append(f"{track['id']}:{out_ass}")
        ass_files.append(out_ass)
    print(f"    - Font cleaner: reading {len(ass_tracks)} ASS/SSA track(s)...")
    result = subprocess.run(
        extract,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode > 1 or any(not path.is_file() for path in ass_files):
        detail = (result.stderr or result.stdout or "mkvextract failed").strip().splitlines()
        message = detail[-1] if detail else "could not extract subtitles"
        print(f"      Warning: {message}")
        return [], message, True
    requested = []
    for ass_file in ass_files:
        try:
            found = read_ass_font_names(ass_file)
        except OSError as exc:
            message = _short_error(exc)
            print(f"      Warning: {message}")
            return [], message, True
        print(f"      - {ass_file.name}: {len(found)} font name(s)")
        requested.extend(found)
    shown = _unique_requested(requested)
    print(f"    - Fonts referenced by subtitles: {'; '.join(shown) if shown else '(none)'}")
    return requested, None, True


def _missing_font_names(labels):
    missing = {}
    for label in labels:
        _remember_font_name(missing, label)
    return missing


def fonts_to_keep(source_file, work_dir):
    """Attachments to re-add, or None when every attachment should be copied.

    A font stays when any face matches a subtitle name, when its filename
    matches, or when the file cannot be read. Names match with case, spaces,
    hyphens, and underscores ignored, and one leading @ removed. Every face
    in a collection is read, including variable-font instances. Byte-identical
    copies are kept once. Other attachments are always kept.

    None means the caller copies the source attachments unchanged. That happens
    when there is nothing to filter, fonttools is missing, or a kept attachment
    could not be extracted.

    Prints FONT_CLEAN lines for the encode log: kept and dropped use the font's
    full name, missing uses the name written in the subtitles.
    """
    info = mkvmerge_identify(source_file)
    attachments = info.get("attachments") or []
    font_atts = [item for item in attachments if is_font_attachment(item)]
    font_ids = {id(item) for item in font_atts}
    other_atts = [item for item in attachments if id(item) not in font_ids]
    if not font_atts:
        print("    - Font cleaner: no font attachments.")
        temp_dir = _ensure_work_dir(work_dir) if _ass_tracks(info) else None
        try:
            requested, subtitle_error, _has_ass = collect_referenced_fonts(
                source_file, info, temp_dir,
            )
            if subtitle_error:
                print(f"    - Font cleaner: subtitles could not be read ({subtitle_error}).")
                log_font_clean({}, {}, {})
            else:
                log_font_clean({}, {}, _missing_font_names(_unique_requested(requested)))
        finally:
            if temp_dir is not None and work_dir is None:
                shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    if not fonttools_available():
        print(
            "    - Font cleanup could not be completed because fonttools is missing. "
            "Keeping every font attachment."
        )
        print("      Arch: sudo pacman -S python-fonttools    Else: pip install fonttools")
        print("    - FONT_CLEAN skipped: fonttools missing")
        return None

    temp_dir = _ensure_work_dir(work_dir)
    requested, subtitle_error, has_ass = collect_referenced_fonts(source_file, info, temp_dir)
    keep_unused = subtitle_error is not None
    if subtitle_error:
        unused_reason = (
            f"WARNING: subtitles could not be read ({subtitle_error}); keeping it"
        )
        print(f"    - Font cleaner: subtitles could not be read ({subtitle_error}); keeping every font.")
    elif not has_ass:
        unused_reason = "not used by any subtitle"
        print("    - Font cleaner: no ASS/SSA tracks; dropping every font attachment.")
    elif not requested:
        unused_reason = "not used by any subtitle"
        print(
            f"    - Font cleaner: subtitles name no fonts; "
            f"dropping all {len(font_atts)} font attachment(s)."
        )
    else:
        unused_reason = "not used by any subtitle"

    print(f"    - Font cleaner: checking {len(font_atts)} font attachment(s)...")
    fonts = []
    for order, att in enumerate(font_atts):
        file_name = str(att.get("file_name") or f"id {att.get('id')}")
        dest = temp_dir / f"att_{att['id']}_{safe_font_filename(file_name)}"
        att["temp_path"] = dest
        font = _FontInfo(file_name, att.get("id"), order, att)
        try:
            _extract_attachment(source_file, att["id"], dest)
        except RuntimeError as exc:
            font.problem = _short_error(exc)
            fonts.append(font)
            continue
        fonts.append(_inspect_attached_font(font, dest))

    plan = _decide_fonts(
        fonts,
        [] if subtitle_error else requested,
        keep_unused=keep_unused,
        unused_reason=unused_reason,
    )
    by_order = {font.order: font for font in fonts}

    kept = []
    missing_files = []
    for order, action, _reason in plan.decisions:
        if action != "keep":
            continue
        font = by_order[order]
        path = font.attachment.get("temp_path")
        if not path or not Path(path).is_file():
            missing_files.append(font.file_name)
            continue
        kept.append(font.attachment)

    for att in other_atts:
        file_name = str(att.get("file_name") or "att")
        dest = temp_dir / f"other_{att['id']}_{safe_font_filename(file_name)}"
        att["temp_path"] = dest
        try:
            _extract_attachment(source_file, att["id"], dest)
        except RuntimeError as exc:
            missing_files.append(f"{file_name} ({_short_error(exc)})")
            continue
        kept.append(att)

    if missing_files:
        print(
            "    - Font cleaner: could not extract attachments that must be kept "
            f"({'; '.join(missing_files)}). Leaving every attachment in place."
        )
        print("    - FONT_CLEAN skipped: extract failed")
        return None

    if other_atts:
        print(f"    - Font cleaner: keeping {len(other_atts)} non-font attachment(s).")
    kept_names = {}
    dropped_names = {}
    for order, action, reason in plan.decisions:
        font = by_order[order]
        logged = _font_log_name(font)
        if action == "keep" and reason.startswith("WARNING"):
            tag = "[KEEP]"
        elif action == "keep":
            tag = "[MATCH]"
        else:
            tag = "[SKIP]"
        print(f"      {tag:7} {logged} (file '{font.file_name}') {reason}")
        if action == "keep":
            _remember_font_name(kept_names, logged)
        else:
            _remember_font_name(dropped_names, logged)
    for warning in plan.warnings:
        print(f"      Warning: {warning}")

    print(
        f"    - Font cleaner: keeping {plan.keep_count} font(s), "
        f"dropping {plan.remove_count}."
    )
    log_font_clean(kept_names, dropped_names, _missing_font_names(plan.unmatched))
    return kept


def attachment_merge_args(attachments):
    args = []
    for att in attachments:
        path = att.get("temp_path")
        if not path:
            continue
        mime = att.get("content_type") or "application/octet-stream"
        name = att.get("file_name") or Path(path).name
        args += [
            "--attachment-name", _mkv_option_value(name),
            "--attachment-mime-type", mime,
            "--attach-file", str(path),
        ]
    return args


# --- CROPDETECT LOGIC FROM cropdetect/cropdetect.py ---

_CROPDETECT_LIMIT = 24.0 / 255.0
_CROPDETECT_FILTER = f"cropdetect=limit={_CROPDETECT_LIMIT:.7f}:round=4:reset=1:skip=0"
_CROPDETECT_LUMA_MIN = 16.0
_CROPDETECT_BAR_DELTA = 12.0
_CROPDETECT_EDGE_FRAC = 0.05
_CROPDETECT_MIN_INSERT = 6.0
_CROPDETECT_AUTO_KEYFRAME_MAX_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB
_CROPDETECT_INTRA_ONLY_CODECS = frozenset({
    "prores", "dnxhd", "mjpeg", "ffv1", "rawvideo", "utvideo",
    "huffyuv", "ffvhuff", "magicyuv", "cfhd", "v210", "v410",
})
_CROPDETECT_TARGET_AUTO_SEEKS = 360
_CROPDETECT_MAX_KEYFRAME_SPAN = 12.0
_CROPDETECT_MAX_BAR_LUMA_SAMPLES = 24
_CROPDETECT_XY_TOL = 8
_CROPDETECT_SIZE_TOL = 16
_CROPDETECT_AR_TOLERANCE = 0.03
_CROPDETECT_LINE_RE = re.compile(r"\bt:([0-9.-]+).*?\bcrop=(\d+):(\d+):(\d+):(\d+)")
_CROPDETECT_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")

KNOWN_ASPECT_RATIOS = (
    {"name": "HDTV (16:9)", "ratio": 16 / 9},
    {"name": "Widescreen (Scope)", "ratio": 2.39},
    {"name": "Widescreen (CinemaScope)", "ratio": 2.35},
    {"name": "Widescreen (Flat)", "ratio": 1.85},
    {"name": "IMAX Digital (1.90:1)", "ratio": 1.90},
    {"name": "Fullscreen (4:3)", "ratio": 4 / 3},
    {"name": "IMAX 70mm (1.43:1)", "ratio": 1.43},
)


def _check_prerequisites_cropdetect():
    for tool in ["ffmpeg", "ffprobe"]:
        if not shutil.which(tool):
            print(f"Error: '{tool}' command not found. Is it installed and in your PATH?")
            return False
    return True


def _parse_crop_string_cropdetect(crop_str):
    try:
        _, values = crop_str.split("=")
        w, h, x, y = map(int, values.split(":"))
        return {"w": w, "h": h, "x": x, "y": y}
    except (ValueError, IndexError, AttributeError):
        return None


def _round_up_mod4_cropdetect(value):
    return value if value % 4 == 0 else value + (4 - (value % 4))


def _even_offset_cropdetect(value):
    return value - 1 if value % 2 else value


def _crops_similar_cropdetect(a, b, xy_tol=_CROPDETECT_XY_TOL, size_tol=_CROPDETECT_SIZE_TOL):
    return (
        abs(a["x"] - b["x"]) <= xy_tol
        and abs(a["y"] - b["y"]) <= xy_tol
        and abs(a["w"] - b["w"]) <= size_tol
        and abs(a["h"] - b["h"]) <= size_tol
    )


def _crop_contained_in_cropdetect(inner, outer, margin=0):
    return (
        inner["x"] >= outer["x"] - margin
        and inner["y"] >= outer["y"] - margin
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + margin
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + margin
    )


def _median_cropdetect(values):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def _pick_evenly_cropdetect(items, max_count):
    if len(items) <= max_count:
        return list(items)
    if max_count <= 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (max_count - 1)
    return [items[round(i * step)] for i in range(max_count)]


def _expanding_bar_regions_cropdetect(majority, candidate, video_w, video_h, min_side=2):
    bars = []
    top = majority["y"]
    bottom = video_h - (majority["y"] + majority["h"])
    left = majority["x"]
    right = video_w - (majority["x"] + majority["w"])

    if top >= min_side and (candidate is None or candidate["y"] < majority["y"]):
        bars.append((video_w, top, 0, 0))
    if bottom >= min_side and (
        candidate is None
        or (candidate["y"] + candidate["h"]) > (majority["y"] + majority["h"])
    ):
        bars.append((video_w, bottom, 0, majority["y"] + majority["h"]))
    if left >= min_side and (candidate is None or candidate["x"] < majority["x"]):
        bars.append((left, video_h, 0, 0))
    if right >= min_side and (
        candidate is None
        or (candidate["x"] + candidate["w"]) > (majority["x"] + majority["w"])
    ):
        bars.append((right, video_h, majority["x"] + majority["w"], 0))
    return bars


def _is_major_crop_cropdetect(crop_str, video_w, video_h, min_crop_size):
    parsed = _parse_crop_string_cropdetect(crop_str)
    if not parsed:
        return False
    top = parsed["y"]
    bottom = video_h - (parsed["y"] + parsed["h"])
    left = parsed["x"]
    right = video_w - (parsed["x"] + parsed["w"])
    return max(top, bottom, left, right) >= min_crop_size


def _is_full_frame_cropdetect(crop_str, video_w, video_h):
    parsed = _parse_crop_string_cropdetect(crop_str)
    return bool(parsed and parsed["w"] == video_w and parsed["h"] == video_h)


def _is_edge_timestamp_cropdetect(ts, duration, edge_frac):
    if duration <= 0 or edge_frac <= 0:
        return False
    edge = duration * edge_frac
    return ts < edge or ts > duration - edge


def _sample_times_cropdetect(duration, interval, offset=0.0):
    if duration <= 0:
        return [0.0]
    end = max(0.0, duration - 0.20)
    times = []
    t = offset
    while t <= end + 1e-9:
        times.append(round(t, 3))
        t += interval
    if not times:
        times = [min(offset, end)]
    return times


def _parse_crop_and_luma_cropdetect(stderr_text, base_ts=0.0):
    samples = []
    pending_crop = None
    pending_ts = None
    for line in stderr_text.splitlines():
        m_crop = _CROPDETECT_LINE_RE.search(line)
        if m_crop:
            if pending_crop is not None:
                samples.append({"ts": pending_ts, "crop": pending_crop, "luma": None})
            raw_t = float(m_crop.group(1))
            w, h, x, y = map(int, m_crop.groups()[1:])
            pending_ts = max(0.0, base_ts + raw_t)
            pending_crop = f"crop={w}:{h}:{x}:{y}"
            continue
        if pending_crop is not None:
            m_yavg = _CROPDETECT_YAVG_RE.search(line)
            if m_yavg:
                try:
                    luma = float(m_yavg.group(1))
                except ValueError:
                    luma = None
                samples.append({"ts": pending_ts, "crop": pending_crop, "luma": luma})
                pending_crop = None
                pending_ts = None
    if pending_crop is not None:
        samples.append({"ts": pending_ts, "crop": pending_crop, "luma": None})
    return samples


def _build_sample_vf_cropdetect():
    return (
        f"{_CROPDETECT_FILTER},"
        "scale=w='min(iw,160)':h=-2:flags=fast_bilinear,"
        "format=gray,signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG"
    )


def _scan_keyframe_chunk_cropdetect(task_args):
    start, length, input_file, video_stream_idx = task_args
    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-threads", "1",
        "-skip_frame", "nokey",
        "-ss", f"{start:.3f}",
        "-t", f"{length:.3f}",
        "-i", input_file,
        "-map", f"0:{video_stream_idx}",
        "-an", "-sn", "-dn",
        "-vf", _build_sample_vf_cropdetect(),
        "-f", "null", "-",
    ]
    result = subprocess.run(
        ffmpeg_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return []
    samples = _parse_crop_and_luma_cropdetect(result.stderr, base_ts=start)
    end = start + length
    return [s for s in samples if s["ts"] <= end + 0.5]


def _run_keyframe_scan_cropdetect(input_file, duration, video_stream_idx, num_workers):
    num_chunks = max(1, min(num_workers * 2, max(1, int(duration // 15))))
    if duration < 30:
        num_chunks = 1
    chunk_len = duration / num_chunks
    tasks = [
        (round(i * chunk_len, 3), round(chunk_len, 3), input_file, video_stream_idx)
        for i in range(num_chunks)
    ]
    workers = max(1, min(num_workers, len(tasks)))
    raw_samples = []
    with _ThreadPoolExecutor_cropdetect(max_workers=workers) as pool:
        for chunk_samples in pool.map(_scan_keyframe_chunk_cropdetect, tasks):
            raw_samples.extend(chunk_samples)
    raw_samples.sort(key=lambda s: s["ts"])
    deduped = []
    seen = set()
    for sample in raw_samples:
        key = round(sample["ts"], 1)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sample)
    return deduped


def _analyze_seek_sample_cropdetect(task_args):
    seek_time, input_file, video_stream_idx = task_args
    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-threads", "1",
        "-noaccurate_seek",
        "-ss", f"{seek_time:.3f}",
        "-i", input_file,
        "-map", f"0:{video_stream_idx}",
        "-frames:v", "1",
        "-an", "-sn", "-dn",
        "-vf", _build_sample_vf_cropdetect(),
        "-f", "null", "-",
    ]
    result = subprocess.run(
        ffmpeg_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return {"ts": seek_time, "crop": None, "luma": None}
    parsed = _parse_crop_and_luma_cropdetect(result.stderr, base_ts=seek_time)
    if not parsed:
        return {"ts": seek_time, "crop": None, "luma": None}
    sample = parsed[-1]
    sample["ts"] = seek_time
    return sample


def _run_seek_samples_cropdetect(input_file, times, video_stream_idx, num_workers):
    if not times:
        return []
    tasks = [(t, input_file, video_stream_idx) for t in times]
    workers = max(1, min(num_workers, len(tasks)))
    with _ThreadPoolExecutor_cropdetect(max_workers=workers) as pool:
        results = list(pool.map(_analyze_seek_sample_cropdetect, tasks))
    results.sort(key=lambda s: s["ts"])
    return results


def _attach_sample_durations_cropdetect(samples, duration, fixed_interval=None):
    ordered = sorted(samples, key=lambda s: s["ts"])
    if fixed_interval is not None:
        for s in ordered:
            s["dt"] = float(fixed_interval)
        return ordered
    n = len(ordered)
    for i, s in enumerate(ordered):
        if i + 1 < n:
            span = max(0.1, ordered[i + 1]["ts"] - s["ts"])
        else:
            span = max(0.1, duration - s["ts"])
        s["dt"] = min(span, _CROPDETECT_MAX_KEYFRAME_SPAN)
    return ordered


def _analyze_bar_luma_cropdetect(task_args):
    seek_time, input_file, video_stream_idx, crops, accurate = task_args
    if not crops:
        return {"ts": seek_time, "bar_luma": None}
    n = len(crops)
    scale_chain = (
        "scale=w='min(iw,160)':h='min(ih,160)':flags=fast_bilinear,"
        "format=gray,signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG"
    )
    if n == 1:
        w, h, x, y = crops[0]
        filter_args = ["-map", f"0:{video_stream_idx}", "-vf", f"crop={w}:{h}:{x}:{y},{scale_chain}"]
    else:
        labels = "".join(f"[s{i}]" for i in range(n))
        parts = [f"[0:{video_stream_idx}]split={n}{labels}"]
        maps = []
        for i, (w, h, x, y) in enumerate(crops):
            parts.append(f"[s{i}]crop={w}:{h}:{x}:{y},{scale_chain}[o{i}]")
            maps.extend(["-map", f"[o{i}]"])
        filter_args = ["-filter_complex", ";".join(parts), *maps]

    seek_flags = [] if accurate else ["-noaccurate_seek"]
    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-threads", "1",
        *seek_flags,
        "-ss", f"{seek_time:.3f}",
        "-i", input_file,
        "-frames:v", "1",
        "-an", "-sn", "-dn",
        *filter_args,
        "-f", "null", "-",
    ]
    result = subprocess.run(
        ffmpeg_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return {"ts": seek_time, "bar_luma": None}
    yavgs = []
    for raw in _CROPDETECT_YAVG_RE.findall(result.stderr):
        try:
            yavgs.append(float(raw))
        except ValueError:
            pass
    return {"ts": seek_time, "bar_luma": max(yavgs) if yavgs else None}


def _run_bar_luma_cropdetect(input_file, video_stream_idx, times, crops, num_workers, accurate=False):
    if not times or not crops:
        return []
    tasks = [(t, input_file, video_stream_idx, crops, accurate) for t in times]
    workers = max(1, min(num_workers, len(tasks)))
    with _ThreadPoolExecutor_cropdetect(max_workers=workers) as pool:
        return list(pool.map(_analyze_bar_luma_cropdetect, tasks))


def _snap_to_known_ar_cropdetect(w, h, x, y, video_w, video_h, tolerance=_CROPDETECT_AR_TOLERANCE):
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

    ar_name = best_match["name"]
    if abs(w - video_w) < 16:
        new_h = _round_up_mod4_cropdetect(round(video_w / best_match["ratio"]))
        if new_h < h:
            new_h = _round_up_mod4_cropdetect(h)
            ar_name = f"Custom AR (Near {best_match['name']})"
        new_h = min(new_h, video_h)
        new_y = y + round((h - new_h) / 2)
        new_y = max(0, min(new_y, video_h - new_h))
        new_y = max(0, _even_offset_cropdetect(new_y))
        return f"crop={video_w}:{new_h}:0:{new_y}", ar_name

    if abs(h - video_h) < 16:
        new_w = _round_up_mod4_cropdetect(round(video_h * best_match["ratio"]))
        if new_w < w:
            new_w = _round_up_mod4_cropdetect(w)
            ar_name = f"Custom AR (Near {best_match['name']})"
        new_w = min(new_w, video_w)
        new_x = x + round((w - new_w) / 2)
        new_x = max(0, min(new_x, video_w - new_w))
        new_x = max(0, _even_offset_cropdetect(new_x))
        return f"crop={new_w}:{video_h}:{new_x}:0", ar_name

    return f"crop={w}:{h}:{x}:{y}", None


def _calculate_bounding_box_cropdetect(rects):
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    valid = 0
    for rect in rects:
        if not rect:
            continue
        min_x = min(min_x, rect["x"])
        min_y = min(min_y, rect["y"])
        max_x = max(max_x, rect["x"] + rect["w"])
        max_y = max(max_y, rect["y"] + rect["h"])
        valid += 1
    if valid == 0:
        return None
    return f"crop={max_x - min_x}:{max_y - min_y}:{min_x}:{min_y}"


def _cluster_samples_cropdetect(samples):
    remaining = list(samples)
    clusters = []
    while remaining:
        counts = _Counter_cropdetect(s["crop"] for s in remaining)
        center_str, _ = counts.most_common(1)[0]
        center = _parse_crop_string_cropdetect(center_str)
        members = []
        rest = []
        for sample in remaining:
            parsed = _parse_crop_string_cropdetect(sample["crop"])
            if center and parsed and _crops_similar_cropdetect(parsed, center):
                members.append(sample)
            else:
                rest.append(sample)
        remaining = rest
        members.sort(key=lambda s: s["ts"])
        clusters.append({
            "center": center_str,
            "members": members,
            "timestamps": [s["ts"] for s in members],
            "count": len(members),
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def _max_contiguous_run_cropdetect(body_members, max_gap):
    if not body_members:
        return 0.0
    ordered = sorted(body_members, key=lambda s: s["ts"])
    best = 0.0
    run_dur = ordered[0].get("dt", 0.0)
    prev_end = ordered[0]["ts"] + ordered[0].get("dt", 0.0)
    for s in ordered[1:]:
        if s["ts"] - prev_end <= max_gap:
            run_dur += s.get("dt", 0.0)
        else:
            best = max(best, run_dur)
            run_dur = s.get("dt", 0.0)
        prev_end = s["ts"] + s.get("dt", 0.0)
    return max(best, run_dur)


def _classify_clusters_cropdetect(
    clusters, duration, edge_frac, significant_crop_threshold, min_insert, seek_interval=None
):
    total = sum(c["count"] for c in clusters) or 1
    needed_hits = max(2, math.ceil(min_insert / max(float(seek_interval), 1e-6))) if seek_interval else 1
    max_gap = max(_CROPDETECT_MAX_KEYFRAME_SPAN, min_insert * 1.5)
    if seek_interval:
        max_gap = max(max_gap, seek_interval * 2.5)

    body_clusters = []
    for cluster in clusters:
        body_members = [
            m for m in cluster["members"]
            if not _is_edge_timestamp_cropdetect(m["ts"], duration, edge_frac)
        ]
        body_ts = [m["ts"] for m in body_members]
        body_dur = sum(m.get("dt", 0.0) for m in body_members)
        longest_run = _max_contiguous_run_cropdetect(body_members, max_gap)
        cluster["body_timestamps"] = body_ts
        cluster["percentage"] = (cluster["count"] / total) * 100
        if not body_ts:
            continue
        if seek_interval is not None:
            sustained = len(body_ts) >= needed_hits and body_dur >= min_insert - 1e-6
        else:
            sustained = (longest_run >= min_insert - 1e-6) or (
                len(body_ts) >= 2 and body_dur >= min_insert - 1e-6
            )
        if sustained:
            body_clusters.append(cluster)
    return body_clusters


def _verify_expanding_clusters_cropdetect(
    body_clusters,
    input_file,
    video_stream_idx,
    video_w,
    video_h,
    duration,
    num_workers,
    bar_delta,
    min_insert,
    significant_crop_threshold,
    debug=False,
):
    if len(body_clusters) <= 1:
        return body_clusters
    majority = max(body_clusters, key=lambda c: c["count"])
    maj = _parse_crop_string_cropdetect(majority["center"])
    if not maj:
        return body_clusters

    expanding = []
    kept = [majority]
    for cluster in body_clusters:
        if cluster is majority:
            continue
        cand = _parse_crop_string_cropdetect(cluster["center"])
        if not cand:
            continue
        if not _crop_contained_in_cropdetect(cand, maj, margin=4):
            expanding.append(cluster)
        elif cluster["percentage"] >= significant_crop_threshold:
            kept.append(cluster)

    if not expanding:
        return kept

    all_maj_bars = _expanding_bar_regions_cropdetect(maj, None, video_w, video_h)
    if not all_maj_bars:
        return kept

    base_times = _pick_evenly_cropdetect(majority.get("body_timestamps") or majority["timestamps"], 8)
    baseline_hits = _run_bar_luma_cropdetect(
        input_file, video_stream_idx, base_times, all_maj_bars, num_workers, accurate=False
    )
    baseline = _median_cropdetect([h["bar_luma"] for h in baseline_hits])
    if baseline is None:
        baseline = 0.0
    threshold = max(8.0, baseline + bar_delta)

    still_body = list(kept)
    for cluster in expanding:
        cand = _parse_crop_string_cropdetect(cluster["center"])
        bars = _expanding_bar_regions_cropdetect(maj, cand, video_w, video_h) or all_maj_bars
        raw_times = cluster.get("body_timestamps") or cluster["timestamps"]
        check_times = _pick_evenly_cropdetect(raw_times, _CROPDETECT_MAX_BAR_LUMA_SAMPLES)
        single_hit_probe = len(check_times) == 1
        if single_hit_probe:
            t0 = check_times[0]
            probe_end = min(duration - 0.2, t0 + min_insert)
            extra = [
                round(t0 + (probe_end - t0) * frac, 3)
                for frac in (0.4, 0.8)
                if t0 + (probe_end - t0) * frac < duration - 0.2
            ]
            check_times = [t0] + extra

        hits = _run_bar_luma_cropdetect(
            input_file, video_stream_idx, check_times, bars, num_workers, accurate=single_hit_probe
        )
        valid_hits = [h for h in hits if h.get("bar_luma") is not None]
        picture = [h for h in valid_hits if h["bar_luma"] >= threshold]
        needed_picture = max(2, math.ceil(len(check_times) * 0.5)) if len(check_times) >= 2 else 1
        if len(picture) >= needed_picture:
            still_body.append(cluster)
            if debug:
                med = _median_cropdetect([h["bar_luma"] for h in valid_hits])
                print(f"    - Expanding AR kept ({cluster['center']}, bar YAVG={med:.1f} vs {baseline:.1f})")
    return still_body


def _recommend_from_clusters_cropdetect(body_clusters, video_w, video_h, min_crop):
    if not body_clusters:
        return None, None, "none"
    rects = [_parse_crop_string_cropdetect(c["center"]) for c in body_clusters]
    raw = _calculate_bounding_box_cropdetect(rects)
    if not raw:
        return None, None, "none"
    parsed = _parse_crop_string_cropdetect(raw)
    snapped, ar_label = _snap_to_known_ar_cropdetect(
        parsed["w"], parsed["h"], parsed["x"], parsed["y"], video_w, video_h
    )
    if _is_full_frame_cropdetect(snapped, video_w, video_h):
        return snapped, ar_label, "full"
    if not _is_major_crop_cropdetect(snapped, video_w, video_h, min_crop):
        return snapped, ar_label, "tiny"
    kind = "single" if len(body_clusters) == 1 else "mixed"
    return snapped, ar_label, kind


def _analyze_video_cropdetect(
    input_file,
    duration,
    width,
    height,
    video_stream_idx,
    codec_name,
    file_size,
    num_workers,
    significant_crop_threshold,
    min_crop,
    debug=False,
):
    use_seek = (
        (codec_name and codec_name.lower() in _CROPDETECT_INTRA_ONLY_CODECS)
        or file_size > _CROPDETECT_AUTO_KEYFRAME_MAX_BYTES
    )
    if use_seek:
        effective_interval = round(max(2.0, min(10.0, duration / _CROPDETECT_TARGET_AUTO_SEEKS)), 2)
        print(f"    - Autocrop scan: seek every {effective_interval}s across {num_workers} worker(s)...")
        times = _sample_times_cropdetect(duration, effective_interval, offset=0.0)
        samples = _run_seek_samples_cropdetect(input_file, times, video_stream_idx, num_workers)
        scan_mode = "seek"
    else:
        print(f"    - Autocrop scan: keyframes across {num_workers} worker(s)...")
        samples = _run_keyframe_scan_cropdetect(input_file, duration, video_stream_idx, num_workers)
        scan_mode = "keyframes"
        effective_interval = None
        if len(samples) < 5 and duration > 10:
            effective_interval = round(max(2.0, min(10.0, duration / _CROPDETECT_TARGET_AUTO_SEEKS)), 2)
            times = _sample_times_cropdetect(duration, effective_interval, offset=0.0)
            samples = _run_seek_samples_cropdetect(input_file, times, video_stream_idx, num_workers)
            scan_mode = "seek"

    _attach_sample_durations_cropdetect(
        samples, duration, fixed_interval=effective_interval if scan_mode == "seek" else None
    )

    def usable(sample):
        if not sample.get("crop"):
            return False
        luma = sample.get("luma")
        return True if luma is None else luma >= _CROPDETECT_LUMA_MIN

    valid = [s for s in samples if usable(s)]
    if not valid:
        return None

    def decide(valid_samples, cur_interval):
        clusters = _cluster_samples_cropdetect(valid_samples)
        body = _classify_clusters_cropdetect(
            clusters,
            duration,
            _CROPDETECT_EDGE_FRAC,
            significant_crop_threshold,
            _CROPDETECT_MIN_INSERT,
            seek_interval=cur_interval,
        )
        body = _verify_expanding_clusters_cropdetect(
            body,
            input_file,
            video_stream_idx,
            width,
            height,
            duration,
            num_workers,
            _CROPDETECT_BAR_DELTA,
            _CROPDETECT_MIN_INSERT,
            significant_crop_threshold,
            debug=debug,
        )
        return _recommend_from_clusters_cropdetect(body, width, height, min_crop)

    cur_interval = effective_interval if scan_mode == "seek" else None
    snapped, ar_label, kind = decide(valid, cur_interval)

    if (
        scan_mode == "seek"
        and kind in ("single", "mixed")
        and effective_interval is not None
        and effective_interval > max(1.0, _CROPDETECT_MIN_INSERT / 2.0)
    ):
        already = {round(s["ts"], 3) for s in samples}
        gap_times = [
            t for t in _sample_times_cropdetect(duration, effective_interval, offset=effective_interval / 2.0)
            if round(t, 3) not in already
        ]
        if gap_times:
            extra = _run_seek_samples_cropdetect(input_file, gap_times, video_stream_idx, num_workers)
            extra_valid = [s for s in extra if usable(s)]
            if extra_valid:
                valid = _attach_sample_durations_cropdetect(
                    valid + extra_valid, duration, fixed_interval=effective_interval / 2.0
                )
                snapped, ar_label, kind = decide(valid, effective_interval / 2.0)

    if kind in ("single", "mixed") and snapped:
        if debug and ar_label:
            print(f"    - Autocrop snapped to '{ar_label}' ({kind})")
        return snapped
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
        duration = float(duration_str.strip())
        if not math.isfinite(duration) or duration <= 0:
            return None
        probe_res_args = [
            "ffprobe", "-v", "error",
            "-select_streams", "v",
            "-show_entries", "stream=index,codec_name,width,height,disposition",
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
        stream_idx = int(video_stream.get("index", 0))
        codec_name = video_stream.get("codec_name", "")
        file_size = os.path.getsize(input_file)
    except Exception:
        return None
    return _analyze_video_cropdetect(
        input_file,
        duration,
        width,
        height,
        stream_idx,
        codec_name,
        file_size,
        max(1, (os.cpu_count() or 2) // 2),
        significant_crop_threshold,
        min_crop,
        debug,
    )


def convert_video(
    file_path,
    is_vfr,
    target_cfr_fps,
    track,
    autocrop_filter=None,
    cq_override=None,
    photon_noise=None,
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

    params = aom_params_for_track(track, cq_override=cq_override)
    av1an_video_params_str = av1an_aom_param_string(params)
    workers, total_cores = av1an_worker_count()
    print(f"    - Path: {path_label(track)}")
    print(
        f"    - Using {workers} workers for av1an "
        f"(Total Cores: {total_cores}, Logic: (Cores/2)-1)."
    )
    print(
        f"    - cpu-used: {params['cpu-used']}  cq-level: {params['cq-level']}  "
        f"matrix_in: {matrix_in}"
    )
    print(f"    - Using aom-psy101 parameters: {av1an_video_params_str}")

    if file_is_usable(encoded_video_file):
        print(f"    - Reusing existing av1an output (resume): {encoded_video_file}")
        print("  --- Finished Video Processing ---")
        return encoded_video_file, temp_files

    print("    - Starting AV1 encode with av1an (this will take a long time)...")
    av1an_enc_args = [
        "av1an", "-i", str(vpy_file), "-o", str(encoded_video_file), "-n",
        "-e", "aom", "--resume", "--sc-pix-format", "yuv420p", "-c", "mkvmerge",
        "--set-thread-affinity", "2", "--pix-format", "yuv420p10le", "--force", "--no-defaults",
        "-w", str(workers), "--passes", "2",
    ]
    if photon_noise is not None:
        av1an_enc_args.extend(["--photon-noise", str(photon_noise)])
    av1an_enc_args.extend(["-v", av1an_video_params_str])
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


def main(no_downmix=False, autocrop=False, crf=None, grain=None, norm_i=None, norm_tp=None, no_font_clean=False):
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
            file_no_downmix, no_downmix_reason = resolve_file_no_downmix(file_path, no_downmix)
            print(f"STARTING LOG FOR: {file_path.name}")
            print(f"Processing started at: {date_for_runtime_calc}")
            print(f"Full input file path: {file_path.resolve()}")
            print(f"No-downmix: {no_downmix_reason}")
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
                cq_override=crf,
                photon_noise=grain,
            )

            muxed = Path(f"output-{file_path.name}")
            extra_temps.append(muxed)
            if file_is_usable(muxed):
                print(f"    - Reusing remuxed output (resume): {muxed}")
            else:
                audio_temp_dir = tempfile.mkdtemp(prefix="audio_tmp_")
                print(f"Audio temporary directory created at: {audio_temp_dir}")
                audio_plan = process_audio_tracks(file_path, audio_temp_dir, file_no_downmix)
                mux_final(
                    muxed, encoded_video_file, file_path, audio_plan,
                    clean_fonts=not no_font_clean,
                    work_dir=audio_temp_dir,
                )

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
            "Batch-process MKV files with av1an + aom-psy101. "
            "Auto-detects SDR/HDR and 1080p/4K. Audio is LUFS/Opus or AAC remux; "
            "final mkvmerge is this script."
        )
    )
    parser.add_argument(
        "--no-downmix",
        action="store_true",
        help=(
            "Keep surround on re-encoded tracks (no Nightmode Dialogue pan) for all files. "
            "Can also be enabled per file by ending the filename with 'nodownmix' before '.mkv' "
            "(e.g. Movie.2016.GROUP.nodownmix.mkv, case-insensitive; tag is kept on the output). "
            "AAC/Opus are always remuxed."
        ),
    )
    parser.add_argument(
        "--autocrop",
        action="store_true",
        help="Automatically detect and crop black bars from video using cropdetect.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=None,
        help=f"Override aom cq-level. Default: {DEFAULT_CQ} for all resolutions (SDR and HDR).",
    )
    parser.add_argument(
        "--grain",
        type=int,
        help="Set the photon-noise value for grain synthesis. Disabled by default.",
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
    parser.add_argument(
        "--nofontsclean", "-nfc",
        action="store_true",
        help="Keep every font attachment. Default: drop fonts not used by remaining ASS/SSA tracks. A font that cannot be read is kept.",
    )
    args = parser.parse_args()
    main(
        no_downmix=args.no_downmix,
        autocrop=args.autocrop,
        crf=args.crf,
        grain=args.grain,
        norm_i=args.norm_i,
        norm_tp=args.norm_tp,
        no_font_clean=args.nofontsclean,
    )
