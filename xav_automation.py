#!/usr/bin/env python3

# This script is centered around the "xav" batch AV1 encoder tool.
# For more information and to install xav, visit: https://github.com/emrakyz/xav
#
# Batch encode: xav is VIDEO ONLY (no -a). Audio, subs, attachments, mux: this script.
# xav accepts only yuv420p (8-bit) or yuv420p10le (10-bit). 8-bit is upconverted inside xav.
# Packet-CFR probe (ffprobe PTS) gates HandBrake. MediaInfo/ffprobe "CFR" flags are not trusted.
# Skip HandBrake only if packets prove CFR AND MediaInfo is not VFR AND pix_fmt is already
# yuv420p / yuv420p10le. Then mkvmerge video-only remux (1080p or 4K/HDR).
# Otherwise HandBrake: 1080p SDR → x264 / x264_10bit all-intra; 4K/HDR → x265_10bit.
# Format convert: 12/16-bit → 10-bit; 4:2:2 / 4:4:4 / RGB → yuv420p10le.
# ffmpeg is only a fallback if HandBrake produces an empty file.
# 1080p or lower: -p "--preset 1 --tune 2 --crf 30"  -w 4  -b 1
# Above 1080p:    -p "--preset 2 --tune 2 --crf 30"  -w 4  -b 1
# --tune is SVT-AV1-Essential (default 2 = SSIM). --crf 30 always so Essential
# does not apply --quality medium (CRF 35) above 1080p. Workers/buff are fixed.
# Audio: AAC/Opus remuxed. Else: Nightmode Dialogue pan (`<` so the mix cannot clip)
# → ffmpeg loudnorm 2-pass linear (I=-18, TP=-1.5, LRA=20) → opusenc.
# Final mkvmerge: xav video + processed/remuxed audio + source subs/chapters.
# Font attachments: only those referenced by remaining ASS/SSA (skip with --nofontsclean / -nfc).
# Encode log lines FONT_CLEAN kept/dropped/missing use the font's full name, not the attachment filename.
# Non-font attachments are always kept. The untouched source stays in original/.

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
    "ffmpeg", "ffprobe", "mkvmerge", "mkvextract", "mkvpropedit",
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
# SVT-AV1-Essential --tune (default 2 = SSIM).
# https://github.com/nekotrix/SVT-AV1-Essential/blob/Essential-v4.0.1/Docs/Parameters.md
XAV_TUNE = 2
# Always pass --crf so Essential does not use --quality medium (CRF 35) above 1080p.
DEFAULT_CRF = 30
TUNE_NAMES = {
    0: "VQ",
    1: "PSNR",
    2: "SSIM",
    3: "IQ (Image Quality)",
    4: "MS_SSIM",
}

# Constant-gain loudness: EBU R128 / ffmpeg loudnorm 2-pass linear (no LRA compressor).
LOUDNESS_I = -18.0
LOUDNESS_TP = -1.5
# loudnorm max. If target LRA < measured LRA, it silently switches to dynamic (compresses).
LOUDNESS_LRA = 20.0

CFR_SUFFIX = ".cfr.mkv"
CFR_FULL_SUFFIX = ".cfr_full.mkv"
PREP_SUFFIX = ".prep.mkv"

# Fail-closed packet-PTS CFR probe. MediaInfo FrameRate_Mode is not proof.
# Relative 1.2% is too tight for MKV 1ms ticks (24 fps = 41ms vs 42ms → fake 33% outliers).
# A handful of outliers is GOP-start / window-edge noise, not VFR (2/799 used to fail at 0.2%).
CFR_DURATION_REL_TOL = 0.012
CFR_DURATION_ABS_TOL = 0.002
CFR_PTS_MERGE = 0.0005
CFR_MAX_OUTLIER_RATIO = 0.01
CFR_MAX_OUTLIER_ABS = 4
CFR_EDGE_DROP = 1
CFR_MIN_PACKETS = 120
CFR_PROBE_PACKETS = 800
CFR_HEADER_FPS_REL_TOL = 0.03

XAV_PIX_FMTS = {"yuv420p", "yuv420p10le"}


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


def check_tools(report_only=False):
    """PATH tools are required. fonttools is optional: missing means skip font cleanup."""
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    has_fonttools = fonttools_available()
    if report_only:
        print("Tool check (no encode)")
        for tool in REQUIRED_TOOLS:
            state = "OK" if tool not in missing else "MISSING"
            print(f"  {state:7} {tool}")
        if has_fonttools:
            print("  OK      fonttools (Python package)")
        else:
            print("  MISSING fonttools (Python package)")
            print("          Arch: sudo pacman -S python-fonttools")
            print("          Else: pip install fonttools")
            print("          Without it, font cleanup is skipped and every font stays attached.")
        if missing:
            print(f"Missing required tools: {', '.join(missing)}")
            sys.exit(1)
        if not has_fonttools:
            print("Required tools are present. fonttools is missing, so font cleanup will be skipped.")
            sys.exit(1)
        print("All tools available.")
        return
    if missing:
        for tool in missing:
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


def video_chroma(track):
    if not track:
        return ""
    raw = track.get("ChromaSubsampling") or track.get("Chroma_subsampling") or ""
    match = re.search(r"(\d:\d:\d)", str(raw))
    return match.group(1) if match else ""


def video_color_space(track):
    if not track:
        return ""
    return str(track.get("ColorSpace") or track.get("Color_space") or "").upper()


def ffprobe_pix_fmt(path):
    try:
        for stream in ffprobe_json(path).get("streams", []):
            if stream.get("codec_type") == "video":
                return str(stream.get("pix_fmt") or "").lower()
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return ""


def xav_input_ok(path, track=None):
    """xav: yuv420p or yuv420p10le only. No RGB / 4:2:2 / 4:4:4 / 12-bit+."""
    fmt = ffprobe_pix_fmt(path)
    if fmt:
        if fmt in XAV_PIX_FMTS:
            return True, fmt
        return False, fmt
    if track is None:
        try:
            track = video_track(mediainfo_json(path))
        except Exception:
            track = None
    if not track:
        return False, "unknown pixel format"
    space = video_color_space(track)
    chroma = video_chroma(track)
    depth = video_bit_depth(track)
    bits = []
    if space and space not in ("YUV", "YCBCR", "Y'UV"):
        bits.append(space)
    if chroma and chroma != "4:2:0":
        bits.append(chroma)
    if depth not in (8, 10):
        bits.append(f"{depth}-bit")
    if not chroma or not space:
        return False, "unknown pixel format"
    if bits:
        return False, " ".join(bits)
    return True, f"YUV 4:2:0 {depth}-bit"


def _fps_float(raw):
    if not raw:
        return None
    raw = str(raw).split()[0]
    try:
        if "/" in raw:
            num, den = map(float, raw.split("/", 1))
            return (num / den) if den else None
        return float(raw)
    except (TypeError, ValueError):
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


def is_4k_or_hdr(track):
    return is_4k_path(track) or is_hdr(track)


def intermediate_encoder(track):
    """Return (ffmpeg codec args, handbrake encoder, label, handbrake --encopts or None).

    Output is always 4:2:0. 12/16-bit and 4:2:2/4:4:4/RGB become yuv420p10le.
    8-bit 4:2:0 1080p SDR stays 8-bit x264 (xav upconverts).
    """
    depth = video_bit_depth(track)
    chroma = video_chroma(track)
    space = video_color_space(track)
    already_8bit_420 = (
        depth == 8
        and (not chroma or chroma == "4:2:0")
        and (not space or space in ("YUV", "YCBCR", "Y'UV"))
    )
    need_10 = (not already_8bit_420) or is_4k_or_hdr(track) or depth >= 10
    if is_4k_or_hdr(track):
        ffmpeg_args = [
            "-c:v", "libx265",
            "-crf", "0",
            "-preset", "superfast",
            "-tune", "fastdecode",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "info=0",
        ]
        return ffmpeg_args, "x265_10bit", "libx265 10-bit CRF 0 yuv420p10le, normal GOP (4K/HDR)", None
    if need_10:
        ffmpeg_args = [
            "-c:v", "libx264",
            "-crf", "0",
            "-preset", "superfast",
            "-tune", "fastdecode",
            "-pix_fmt", "yuv420p10le",
            "-g", "1",
            "-bf", "0",
        ]
        why = "Hi10p" if depth >= 10 else f"convert {space or 'YUV'} {chroma or '?'} {depth}-bit"
        return ffmpeg_args, "x264_10bit", f"libx264 10-bit CRF 0 all-intra yuv420p10le ({why})", "keyint=1:bframes=0"
    ffmpeg_args = [
        "-c:v", "libx264",
        "-crf", "0",
        "-preset", "superfast",
        "-tune", "fastdecode",
        "-pix_fmt", "yuv420p",
        "-g", "1",
        "-bf", "0",
    ]
    return ffmpeg_args, "x264", "libx264 8-bit CRF 0 all-intra yuv420p (1080p SDR)", "keyint=1:bframes=0"


def xav_worker_count():
    return max(1, XAV_WORKERS)


def xav_preset(track, override=None):
    if override is not None:
        return int(override)
    return PRESET_4K if is_4k_path(track) else PRESET_1080


def xav_tune(override=None):
    if override is not None:
        return int(override)
    return int(XAV_TUNE)


def xav_crf(override=None):
    if override is not None:
        return int(override)
    return int(DEFAULT_CRF)


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
                print(f"    - Warning: Could not parse fractional FPS '{target_cfr_fps}'. HandBrake will still run.")
    else:
        print("    - Warning: VFR detected, but could not determine target CFR. HandBrake will still run.")
    return is_vfr, target_cfr_fps


def _read_video_packets(source_file, limit):
    """First `limit` video packets. None on hard failure."""
    interval_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,dts_time,duration_time",
        "-read_intervals", f"%+#{limit}",
        "-of", "json",
        str(source_file),
    ]
    try:
        raw = run_cmd(interval_cmd, capture_output=True)
        packets = json.loads(raw).get("packets") or []
        if packets:
            return packets[:limit]
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        pass
    csv_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,dts_time,duration_time",
        "-of", "csv=p=0:nokey=1",
        str(source_file),
    ]
    try:
        proc = subprocess.Popen(
            csv_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        packets = []
        assert proc.stdout is not None
        for line in proc.stdout:
            parts = [p.strip() for p in line.strip().split(",")]
            if not parts:
                continue
            packets.append({
                "pts_time": parts[0] if len(parts) > 0 else None,
                "dts_time": parts[1] if len(parts) > 1 else None,
                "duration_time": parts[2] if len(parts) > 2 else None,
            })
            if len(packets) >= limit:
                break
        proc.kill()
        proc.wait()
        return packets
    except (OSError, subprocess.SubprocessError):
        return None


def probe_true_cfr(source_file, track=None):
    """Return (is_true_cfr, reason, nominal_fps). Fail closed on any doubt.

    Uses unique display PTS (merge same-frame NALs). Allows ±2ms so 24 fps
    MKV files with 41ms/42ms ticks still count as CFR.
    """
    packets = _read_video_packets(source_file, CFR_PROBE_PACKETS)
    if packets is None:
        return False, "ffprobe error", None
    times = []
    for packet in packets:
        raw = packet.get("pts_time")
        if raw in (None, "", "N/A"):
            raw = packet.get("dts_time")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        if value < 0:
            return False, "negative timestamp", None
        times.append(value)
    if len(times) < CFR_MIN_PACKETS:
        return False, f"too few packets ({len(times)})", None
    times.sort()
    unique = []
    for value in times:
        if unique and abs(value - unique[-1]) < CFR_PTS_MERGE:
            continue
        unique.append(value)
    if len(unique) < CFR_MIN_PACKETS:
        return False, f"too few unique PTS ({len(unique)})", None
    durations = [second - first for first, second in zip(unique, unique[1:])]
    if len(durations) < CFR_MIN_PACKETS:
        return False, f"too few durations ({len(durations)})", None
    if len(durations) > CFR_EDGE_DROP * 2 + 8:
        measured = durations[CFR_EDGE_DROP:-CFR_EDGE_DROP]
    else:
        measured = durations
    ordered = sorted(measured)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[mid]
    else:
        median = 0.5 * (ordered[mid - 1] + ordered[mid])
    if median <= 0:
        return False, "non-positive median duration", None
    slop = max(median * CFR_DURATION_REL_TOL, CFR_DURATION_ABS_TOL)
    outliers = sum(1 for duration in measured if abs(duration - median) > slop)
    allowed = max(CFR_MAX_OUTLIER_ABS, int(len(measured) * CFR_MAX_OUTLIER_RATIO))
    if outliers > allowed:
        pct = 100.0 * outliers / len(measured)
        return False, f"{pct:.1f}% outliers ({outliers}/{len(measured)}, slop={slop*1000:.1f}ms)", None
    fps = 1.0 / median
    header = _fps_float(video_fps(track, source_file) if track is not None else None)
    if header and header > 0:
        if abs(fps - header) / header > CFR_HEADER_FPS_REL_TOL:
            return False, f"header {header:.3f} fps vs packets {fps:.3f} fps", None
    return (
        True,
        f"median {median*1000:.2f} ms, {outliers}/{len(measured)} outliers, ~{fps:.3f} fps",
        f"{fps:.3f}",
    )


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
    """Remux if packet-CFR + xav-compatible pixels. Else HandBrake (CFR + 4:2:0 8/10-bit)."""
    prep_file = Path(f"{file_path.stem}{PREP_SUFFIX}")
    temps = [prep_file]
    uhd_or_hdr = is_4k_or_hdr(track)

    true_cfr, cfr_reason, _nominal = probe_true_cfr(file_path, track)
    pix_ok, pix_reason = xav_input_ok(file_path, track)
    if true_cfr:
        print(f"    - Packet CFR probe: PASS ({cfr_reason})")
    else:
        print(f"    - Packet CFR probe: FAIL ({cfr_reason}) → HandBrake")
    if pix_ok:
        print(f"    - xav pixel format: OK ({pix_reason})")
    else:
        print(f"    - xav pixel format: incompatible ({pix_reason}) → convert to yuv420p10le")
    if is_vfr:
        print("    - MediaInfo VFR: HandBrake CFR hammer (probe cannot skip)")

    skip_handbrake = true_cfr and (not is_vfr) and pix_ok

    if file_is_usable(prep_file):
        remake_why = None
        if prep_is_vfr(prep_file):
            remake_why = "existing intermediate is VFR"
        else:
            prep_cfr, prep_cfr_reason, _ = probe_true_cfr(prep_file)
            if not prep_cfr:
                remake_why = f"existing intermediate packet CFR fail ({prep_cfr_reason})"
            else:
                prep_pix, prep_pix_reason = xav_input_ok(prep_file)
                if not prep_pix:
                    remake_why = f"existing intermediate not xav-compatible ({prep_pix_reason})"
        if remake_why is None and uhd_or_hdr and skip_handbrake and prep_is_handbrake_reencode(prep_file):
            remake_why = "HandBrake re-encode; remuxing video-only instead"
        if remake_why:
            print(f"    - Deleting and remaking: {prep_file} ({remake_why})")
            prep_file.unlink(missing_ok=True)
        else:
            print(f"    - Reusing existing intermediate (resume): {prep_file}")
            return prep_file, temps

    if skip_handbrake:
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


def mux_final(dest, xav_output, source_file, audio_plan, clean_fonts=True, work_dir=None):
    """xav video only + audio in source order + source subs/chapters.

    Default: drop font attachments not referenced by remaining ASS/SSA.
    Non-font attachments stay. --nofontsclean copies every attachment.
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
    args += source_tail + [str(source_file)] + attach_args
    print("Assembling final file with mkvmerge...")
    print(f"    - mkvmerge: {' '.join(args)}")
    run_cmd(args)
    if not file_is_usable(dest):
        raise RuntimeError(f"mkvmerge produced an empty file: {dest}")


FONT_MIME_MARKERS = ("font", "truetype", "opentype", "sfnt", "application/x-truetype-font")


def is_font_attachment(att):
    mime = str(att.get("content_type") or "").lower()
    name = str(att.get("file_name") or "").lower()
    if any(marker in mime for marker in FONT_MIME_MARKERS):
        return True
    return name.endswith((".ttf", ".otf", ".ttc", ".otc"))


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


def get_ass_font_names(ass_path):
    """Style Fontname plus \\fn overrides. V4 and V4+.

    Returns {lowercase: first spelling} so the log can show the name as written.
    """
    fonts = {}
    in_styles = False
    in_events = False
    fontname_idx = -1

    def add(name):
        name = name.strip()
        if name:
            fonts.setdefault(name.lower(), name)

    with open(ass_path, "r", encoding="utf-8", errors="ignore") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("["):
                section = line.lower()
                in_styles = section in ("[v4+ styles]", "[v4 styles]")
                in_events = section == "[events]"
                continue
            if in_styles:
                if line.lower().startswith("format:"):
                    cols = [col.strip().lower() for col in line.split(":", 1)[1].split(",")]
                    fontname_idx = cols.index("fontname") if "fontname" in cols else -1
                elif line.lower().startswith("style:") and fontname_idx != -1:
                    cols = [col.strip() for col in line.split(":", 1)[1].split(",")]
                    if len(cols) > fontname_idx and cols[fontname_idx]:
                        add(cols[fontname_idx])
            if in_events and line.lower().startswith("dialogue:"):
                for match in re.findall(r"\\fn([^\\}]+)", line):
                    add(match)
    return fonts


def read_font_names(font_path):
    """Return (full name or None, lowercase names used for matching).

    The logged name prefers the English full name (nameID 4), then typographic
    family (16), then family (1). Matching still uses all three, lowercased.
    """
    from fontTools.ttLib import TTFont
    names = set()
    display = None
    best_score = -1
    try:
        font = TTFont(str(font_path), fontNumber=0)
        try:
            for record in font["name"].names:
                if record.nameID not in (1, 4, 16):
                    continue
                try:
                    text = record.toUnicode().strip()
                except Exception:
                    continue
                if not text:
                    continue
                names.add(text.lower())
                score = {4: 300, 16: 200, 1: 100}[record.nameID]
                if record.platformID == 3 and record.langID == 0x409:
                    score += 30
                elif record.platformID == 3:
                    score += 20
                elif record.platformID == 1 and record.langID in (0, 0x409):
                    score += 10
                if score > best_score:
                    best_score = score
                    display = text
        finally:
            font.close()
    except Exception as exc:
        print(f"      Warning: could not read font metadata for {font_path.name}: {exc}")
    return display, names


def classify_attached_font(file_name, display, internal, required):
    """Return (status, logged_name, satisfied required keys).

    status is 'kept' or 'dropped'. logged_name is the font's full name, not the
    attachment filename. required is the lowercase set of subtitle font names.
    """
    file_label = file_name or "font"
    logged = display or f"(unreadable: {file_label})"
    filename_stem = Path(file_name or "").stem.lower()
    internal_hit = set(internal or ()) & set(required)
    satisfied = set(internal_hit)
    if filename_stem and filename_stem in required:
        satisfied.add(filename_stem)
    status = "kept" if satisfied else "dropped"
    return status, logged, satisfied


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
    """ASS/SSA font names. Returns ({lowercase: spelling}, has_ass_tracks)."""
    ass_tracks = _ass_tracks(info)
    required = {}
    if not ass_tracks:
        return required, False
    temp_dir = _ensure_work_dir(work_dir)
    extract = ["mkvextract", "tracks", str(source_file)]
    ass_files = []
    for track in ass_tracks:
        out_ass = temp_dir / f"subs_{track['id']}.ass"
        extract.append(f"{track['id']}:{out_ass}")
        ass_files.append(out_ass)
    print(f"    - Font cleaner: reading {len(ass_tracks)} ASS/SSA track(s)...")
    run_cmd(extract)
    for ass_file in ass_files:
        found = get_ass_font_names(ass_file)
        print(f"      - {ass_file.name}: {len(found)} font name(s)")
        for key, spelling in found.items():
            required.setdefault(key, spelling)
    shown = sorted(required.values(), key=str.casefold)
    print(f"    - Fonts referenced by subtitles: {'; '.join(shown) if shown else '(none)'}")
    return required, True


def fonts_to_keep(source_file, work_dir):
    """Attachments to re-add, or None if the source has no font attachments.

    Fonts are kept only when an ASS/SSA style or \\fn names them (filename or
    internal family / full / typographic name). Other attachments are always kept.

    Prints FONT_CLEAN lines for the encode log: kept and dropped use the font's
    full name, missing uses the name written in the subtitles.
    """
    info = mkvmerge_identify(source_file)
    attachments = info.get("attachments") or []
    font_atts = [a for a in attachments if is_font_attachment(a)]
    other_atts = [a for a in attachments if a not in font_atts]
    if not font_atts:
        print("    - Font cleaner: no font attachments.")
        temp_dir = _ensure_work_dir(work_dir) if _ass_tracks(info) else None
        try:
            required, _has_ass = collect_referenced_fonts(source_file, info, temp_dir)
            log_font_clean({}, {}, required)
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
    required, has_ass = collect_referenced_fonts(source_file, info, temp_dir)
    if not has_ass:
        print("    - Font cleaner: no ASS/SSA tracks; dropping every font attachment.")
    elif not required:
        print(f"    - Font cleaner: subtitles name no fonts; dropping all {len(font_atts)} font attachment(s).")

    kept = []
    kept_names = {}
    dropped_names = {}
    satisfied = set()
    extract_fonts = ["mkvextract", "attachments", str(source_file)]
    for att in font_atts:
        out_font = temp_dir / f"att_{att['id']}_{safe_font_filename(att.get('file_name') or 'font')}"
        extract_fonts.append(f"{att['id']}:{out_font}")
        att["temp_path"] = out_font
    print(f"    - Font cleaner: checking {len(font_atts)} font attachment(s)...")
    run_cmd(extract_fonts)
    for att in font_atts:
        path = att.get("temp_path")
        file_label = att.get("file_name") or f"id {att.get('id')}"
        if not path or not Path(path).exists():
            logged = f"(unreadable: {file_label})"
            print(f"      [SKIP]  {logged} did not extract")
            _remember_font_name(dropped_names, logged)
            continue
        display, internal = read_font_names(path)
        status, logged, hit = classify_attached_font(
            att.get("file_name") or "", display, internal, required,
        )
        if status == "kept":
            satisfied |= hit
            internal_hit = sorted(set(internal) & set(required))
            if internal_hit:
                print(f"      [MATCH] {logged} (file '{file_label}') via internal names: {internal_hit}")
            else:
                print(f"      [MATCH] {logged} (file '{file_label}') via filename")
            _remember_font_name(kept_names, logged)
            kept.append(att)
        else:
            print(f"      [SKIP]  {logged} (file '{file_label}') unused")
            _remember_font_name(dropped_names, logged)

    font_kept = len(kept)
    missing = {
        key: spelling
        for key, spelling in required.items()
        if key not in satisfied
    }

    if other_atts:
        extract_other = ["mkvextract", "attachments", str(source_file)]
        for att in other_atts:
            out_other = temp_dir / f"other_{att['id']}_{safe_font_filename(att.get('file_name') or 'att')}"
            extract_other.append(f"{att['id']}:{out_other}")
            att["temp_path"] = out_other
        print(f"    - Font cleaner: keeping {len(other_atts)} non-font attachment(s).")
        run_cmd(extract_other)
        kept.extend(a for a in other_atts if a.get("temp_path") and Path(a["temp_path"]).exists())

    print(
        f"    - Font cleaner: keeping {font_kept} font(s), "
        f"dropping {len(font_atts) - font_kept}."
    )
    log_font_clean(kept_names, dropped_names, missing)
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
            "--attachment-name", name,
            "--attachment-mime-type", mime,
            "--attach-file", str(path),
        ]
    return args


def run_xav(xav_input, xav_output, track, preset_override=None, tune_override=None, crf_override=None):
    if file_is_usable(xav_output):
        print(f"    - Reusing existing xav output (resume): {xav_output}")
        return

    preset = xav_preset(track, preset_override)
    tune = xav_tune(tune_override)
    crf = xav_crf(crf_override)
    workers = xav_worker_count()
    buff = XAV_BUFF
    path_label = "4K+" if is_4k_path(track) else "1080p or lower"
    print(f"    - Path: {path_label} (height={video_height(track)})")
    print(
        f"    - Workers: {workers}  preset: {preset}  crf: {crf}  "
        f"tune: {tune} ({TUNE_NAMES.get(tune, '?')})  "
        f"-b {buff}  (no -a: audio is this script)"
    )

    encoder_params = f"--preset {preset} --tune {tune} --crf {crf}"
    xav_args = [
        "xav",
        "-e", XAV_ENCODER,
        "-p", encoder_params,
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


def main(no_downmix=False, preset=None, tune=None, crf=None, norm_i=None, norm_tp=None, no_font_clean=False, check_only=False):
    if check_only:
        check_tools(report_only=True)
        return
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
                tune_override=tune,
                crf_override=crf,
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
                mux_final(
                    muxed, xav_output, file_path, audio_plan,
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
        description="xav encodes video only. This script does audio (LUFS/Opus or AAC remux) and the final mkvmerge."
    )
    parser.add_argument(
        "--no-downmix",
        action="store_true",
        help="Keep surround on re-encoded tracks (no Nightmode Dialogue pan). AAC/Opus are always remuxed.",
    )
    parser.add_argument(
        "--preset",
        type=int,
        default=None,
        help=f"Override SVT-AV1 preset. Default: {PRESET_1080} if height<={HEIGHT_4K}, else {PRESET_4K}.",
    )
    parser.add_argument(
        "--tune",
        type=int,
        choices=sorted(TUNE_NAMES),
        default=None,
        help=(
            "SVT-AV1-Essential --tune: optimize the encoding process for different desired outcomes "
            "[0 = VQ, 1 = PSNR, 2 = SSIM, 3 = IQ (Image Quality), 4 = MS_SSIM] "
            f"(default: {XAV_TUNE} = {TUNE_NAMES[XAV_TUNE]})."
        ),
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=None,
        help=(
            f"Override SVT-AV1 CRF. Default: {DEFAULT_CRF} for all resolutions "
            "(overrides Essential --quality medium, which would be CRF 35 above 1080p)."
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
    parser.add_argument(
        "--nofontsclean", "-nfc",
        action="store_true",
        help="Keep every font attachment. Default: drop fonts not used by remaining ASS/SSA tracks.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check required tools and fonttools, then exit. Does not encode.",
    )
    args = parser.parse_args()
    main(
        no_downmix=args.no_downmix,
        preset=args.preset,
        tune=args.tune,
        crf=args.crf,
        norm_i=args.norm_i,
        norm_tp=args.norm_tp,
        no_font_clean=args.nofontsclean,
        check_only=args.check,
    )
