#!/usr/bin/env python3

# This script is centered around the "xav" batch AV1 encoder tool.
# For more information and to install xav, visit: https://github.com/emrakyz/xav
#
# Batch encode: xav is VIDEO ONLY (no -a). Audio, subs, attachments, mux: this script.
# Every file gets a video intermediate so xav has a clean, seekable input.
# HandBrakeCLI for ALL intermediates (CFR and VFR): --cfr, video only, keyint=1.
# ffmpeg is only a fallback if HandBrake produces an empty file.
# Forced CFR — xav crashes on VFR. 1080p SDR stays x264 (10-bit kept).
# 1080p or lower: -p "--preset 1"  -w 4  -b 1
# Above 1080p:    -p "--preset 2"  -w 4  -b 1
# Audio: AAC/Opus remuxed. Else: optional 0.30 pan → constant-gain LUFS (xav-style) → opusenc.
# Final mkvmerge: xav video + processed/remuxed audio + source subs/attachments/chapters.

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
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

XAV_ENCODER = "svt-av1"
XAV_BUFF = 1
XAV_WORKERS = 4
PRESET_1080 = 1
PRESET_4K = 2
HEIGHT_4K = 1080

# Constant-gain loudness (xav-style, no LRA compressor).
LOUDNESS_I = -16.0
LOUDNESS_TP = -1.5

CFR_SUFFIX = ".cfr.mkv"
CFR_FULL_SUFFIX = ".cfr_full.mkv"
PREP_SUFFIX = ".prep.mkv"


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


HDR_TRANSFER_MARKERS = (
    "smpte2084", "smpte st 2084", "pq", "bt.2100", "bt2100",
    "arib-std-b67", "arib std-b67", "hlg", "hybrid log-gamma",
)
HDR_FORMAT_MARKERS = ("hdr10", "hdr10+", "dolby vision", "dolbyvision", "hlg")


def is_hdr(track):
    """True HDR (PQ/HLG/DoVi). 10-bit BT.709 Hi10p is SDR, not HDR."""
    if not track:
        return False
    parts = []
    for key in (
        "HDR_Format", "HDR_Format_String", "HDR_Format_Compatibility",
        "transfer_characteristics", "TransferCharacteristics",
        "Transfer_characteristics", "colour_transfer", "color_transfer",
    ):
        val = track.get(key)
        if val:
            parts.append(str(val).lower())
    text = " ".join(parts)
    if any(m in text for m in HDR_FORMAT_MARKERS):
        return True
    return any(m in text for m in HDR_TRANSFER_MARKERS)


def needs_hevc_intermediate(track):
    """HEVC only for >1080p or real HDR. 1080p SDR (including 10-bit) stays AVC."""
    return is_4k_path(track) or is_hdr(track)


def intermediate_encoder(track):
    """Return (ffmpeg codec args, handbrake encoder, label, handbrake --encopts or None)."""
    ten = video_bit_depth(track) >= 10 or needs_hevc_intermediate(track)
    if needs_hevc_intermediate(track):
        ffmpeg_args = [
            "-c:v", "libx265",
            "-crf", "0",
            "-preset", "superfast",
            "-tune", "fastdecode",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "info=0",
        ]
        return ffmpeg_args, "x265_10bit", "libx265 10-bit CRF 0, normal GOP (4K or HDR)", None
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
    """HandBrake/ffmpeg can copy a video title; xav would copy it again."""
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


def run_handbrake_intermediate(source_file, output_file, track, target_fps):
    """Video-only CFR intermediate. All-intra only for 1080p SDR; 4K/HDR keeps a normal GOP."""
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
    """Video-only all-intra CFR for xav. Strip metadata/chapters/titles. Force constant timestamps."""
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


def prepare_xav_input(file_path, is_vfr, target_cfr_fps, track):
    """Always HandBrake CFR video intermediate. ffmpeg only if HandBrake fails."""
    prep_file = Path(f"{file_path.stem}{PREP_SUFFIX}")
    temps = [prep_file]
    if file_is_usable(prep_file):
        if prep_is_vfr(prep_file):
            print(f"    - Existing intermediate is VFR; deleting and remaking: {prep_file}")
            prep_file.unlink(missing_ok=True)
        else:
            print(f"    - Reusing existing intermediate (resume): {prep_file}")
            return prep_file, temps

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
    print("    - Warning: ffmpeg intermediate failed. Sending source to xav as-is.")
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
    """Measure integrated LUFS, apply one gain, brickwall-clamp true peak. Same as xav (no LRA)."""
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


def downmix_filters(ch):
    """Same 0.30 center-forward mix as xav_opus_encoder.py."""
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


def mux_final(dest, xav_output, source_file, audio_plan):
    """xav video only + audio in source order + source subs/attachments/chapters."""
    extra = [
        "--no-video", "--no-subtitles", "--no-attachments",
        "--no-chapters", "--no-global-tags",
    ]
    args = [
        "mkvmerge", "-o", str(dest),
        "--title", "",
        "--track-name", "0:",
        "--no-audio", "--no-subtitles", "--no-attachments", "--no-chapters",
        str(xav_output),
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


def run_xav(xav_input, xav_output, track, preset_override=None, workers_override=None, buff_override=None):
    if file_is_usable(xav_output):
        print(f"    - Reusing existing xav output (resume): {xav_output}")
        return

    preset = xav_preset(track, preset_override)
    workers = xav_worker_count(workers_override)
    buff = XAV_BUFF if buff_override is None else max(0, int(buff_override))
    path_label = "4K+" if is_4k_path(track) else "1080p or lower"
    print(f"    - Path: {path_label} (height={video_height(track)})")
    print(f"    - Workers: {workers}  preset: {preset}  -b {buff}  (no -a: audio is this script)")

    xav_args = [
        "xav",
        "-e", XAV_ENCODER,
        "-p", f"--preset {preset}",
        "-w", str(workers),
        "-b", str(buff),
        str(xav_input),
        str(xav_output),
    ]
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
        current_dir / f"temp-{file_path.stem}.mkv",
        current_dir / f"output-{file_path.name}",
        current_dir / f"{file_path.stem}_scd.txt",
        current_dir / f"{file_path.stem}.prep_scd.txt",
        current_dir / f"{file_path.stem}.cfr_scd.txt",
        current_dir / f"{file_path.stem}.cfr_full_scd.txt",
    ]
    for path in current_dir.glob(f"{file_path.stem}*_scd.txt"):
        if path not in files:
            files.append(path)
    for path in extra:
        if path and path not in files:
            files.append(path)
    return files


def main(no_downmix=False, preset=None, workers=None, buff=None, norm_i=None, norm_tp=None):
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
            xav_input, extra_temps = prepare_xav_input(file_path, is_vfr, target_cfr_fps, track)
            xav_output = Path(f"temp-{file_path.stem}.mkv")

            run_xav(
                xav_input,
                xav_output,
                track,
                preset_override=preset,
                workers_override=workers,
                buff_override=buff,
            )

            audio_temp_dir = None
            muxed = Path(f"output-{file_path.name}")
            extra_temps.append(muxed)
            if file_is_usable(muxed):
                print(f"    - Reusing remuxed output (resume): {muxed}")
            else:
                audio_temp_dir = tempfile.mkdtemp(prefix="audio_tmp_")
                print(f"Audio temporary directory created at: {audio_temp_dir}")
                audio_plan = process_audio_tracks(file_path, audio_temp_dir, no_downmix)
                mux_final(muxed, xav_output, file_path, audio_plan)

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
        description="xav encodes video only. This script does audio (LUFS/Opus or AAC remux) and the final mkvmerge."
    )
    parser.add_argument(
        "--no-downmix",
        action="store_true",
        help="Keep surround on re-encoded tracks (no 0.30 pan). AAC/Opus are always remuxed.",
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
        preset=args.preset,
        workers=args.workers,
        buff=args.buff,
        norm_i=args.norm_i,
        norm_tp=args.norm_tp,
    )
