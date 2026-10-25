#!/usr/bin/env python3

# mdegrain.py - standalone denoise. Output goes straight into the xav_automation.py queue.
#
# cd into a folder of MKVs and run `mdegrain.py`. For every *.mkv in that folder:
#   1. If packets are not CFR, or MediaInfo says VFR: HandBrake --cfr --rate
#      (ffmpeg -fps_mode cfr is the fallback). Same probe and FPS choice as
#      xav_automation.py. Normal GOP, no all-intra.
#   2. MDegrain2 (mvtools), same filter as ~/bin/mdegrain.vpy
#      Strength: default thsad 350, --low 150, --high 500, or --thsad N.
#   3. Encode to the only pixel formats xav accepts:
#        1080p SDR 8-bit 4:2:0  -> libx264 CRF 0 yuv420p
#        other 1080p SDR        -> libx264 CRF 0 yuv420p10le (Hi10p, 4:2:2, 4:4:4, RGB, 12-bit+)
#        above 1080p or HDR     -> libx265 CRF 0 yuv420p10le
#      x265 CRF 0 is not bit-exact. xav only accepts 4:2:0, so that is the intermediate.
#   4. mkvmerge: new video first, then everything from the source except its video
#   5. Result -> completed/<same name>, source -> original/<same name>,
#      on error the source -> failed/<same name>. One log per file in conv_logs/.
# CPU pin matches xav_automation.py. Detect the logical CPUs, leave the last
# --reserve-threads (default 2) free, and run on the first cores (32 threads
# → 0-29). The process mask is set directly so VapourSynth stays inside it.
# ffmpeg, HandBrake, and mkvmerge inherit that mask.

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs")
DIR_FAILED = Path("failed")
TMP_SUFFIX = ".mdeg-tmp.mkv"
CFR_TMP_SUFFIX = ".mdeg-cfr.mkv"
REQUIRED_TOOLS = ["ffmpeg", "ffprobe", "mkvmerge", "mediainfo"]
# namespace, install name. ffms2 lives in /usr/lib/vapoursynth and needs the profile.d path.
REQUIRED_PLUGINS = (
    ("ffms2", "ffms2"),
    ("mv", "vapoursynth-plugin-mvtools"),
    ("zsmooth", "vapoursynth-plugin-zsmooth-bin"),
)
PLUGIN_PATH_FILE = Path("/etc/profile.d/vapoursynth-plugins.sh")
PLUGIN_PATH_LINE = (
    "echo 'export VAPOURSYNTH_EXTRA_PLUGIN_PATH=/usr/lib/vapoursynth' "
    "| sudo tee /etc/profile.d/vapoursynth-plugins.sh"
)

DEFAULT_THSAD = 350
THSAD_LOW = 150
THSAD_HIGH = 500
# Same reservation as xav_automation.py. 32 threads → CPUs 0-29.
RESERVED_THREADS = 2
INTERRUPTED = False  # set by Ctrl+C, so a dying child is never counted as a failure

# Height above 1080 uses the 4K/HDR codec. Same cutoff as xav_automation.py.
HEIGHT_4K = 1080
XAV_PIX_FMTS = {"yuv420p", "yuv420p10le"}

# Packet-PTS CFR probe, copied from xav_automation.py. Fail closed.
# 1.2% is too tight for MKV 1ms ticks (24 fps = 41ms vs 42ms).
# A handful of outliers is GOP-start / window-edge noise, not VFR.
CFR_DURATION_REL_TOL = 0.012
CFR_DURATION_ABS_TOL = 0.002
CFR_PTS_MERGE = 0.0005
CFR_MAX_OUTLIER_RATIO = 0.01
CFR_MAX_OUTLIER_ABS = 4
CFR_EDGE_DROP = 1
CFR_MIN_PACKETS = 120
CFR_PROBE_PACKETS = 800
CFR_HEADER_FPS_REL_TOL = 0.03

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

# ffmpeg color name -> Matroska Colour element. Names x264 and x265 both accept.
PRIMARIES_ISO = {
    "bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
    "film": 8, "bt2020": 9, "smpte428": 10, "smpte431": 11, "smpte432": 12,
}
TRANSFER_ISO = {
    "bt709": 1, "smpte170m": 6, "smpte240m": 7, "linear": 8,
    "iec61966-2-4": 11, "iec61966-2-1": 13, "bt2020-10": 14, "bt2020-12": 15,
    "smpte2084": 16, "smpte428": 17, "arib-std-b67": 18,
}
MATRIX_ISO = {
    "gbr": 0, "rgb": 0, "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6,
    "smpte240m": 7, "ycgco": 8, "bt2020nc": 9, "bt2020c": 10,
}
ISO_PRIMARIES = {v: k for k, v in PRIMARIES_ISO.items()}
ISO_TRANSFER = {v: k for k, v in TRANSFER_ISO.items()}
ISO_MATRIX = {v: k for k, v in MATRIX_ISO.items() if k != "rgb"}

# mkvmerge -J property -> mkvmerge output flag. Applied to the new video track.
CONTAINER_COLOR = (
    ("color_matrix_coefficients", "--color-matrix-coefficients"),
    ("color_primaries", "--color-primaries"),
    ("color_transfer_characteristics", "--color-transfer-characteristics"),
    ("color_range", "--color-range"),
    ("color_bits_per_channel", "--color-bits-per-channel"),
    ("max_content_light", "--max-content-light"),
    ("max_frame_light", "--max-frame-light"),
    ("chromaticity_coordinates", "--chromaticity-coordinates"),
    ("white_color_coordinates", "--white-color-coordinates"),
    ("max_luminance", "--max-luminance"),
    ("min_luminance", "--min-luminance"),
)

_X264_10BIT = None


def on_sigint(signum, frame):
    global INTERRUPTED
    INTERRUPTED = True
    raise KeyboardInterrupt


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


def logical_cpu_count():
    """Visible logical CPUs. An outer taskset or cpuset narrows this."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        pass
    if sys.platform == "win32":
        total = windows_active_processor_count()
        if total:
            return total
    return os.cpu_count() or 1


def mask_reserving_high_cpus(system_mask, reserved):
    """Clear the highest `reserved` set bits. Too large a reserve keeps the mask."""
    system_mask = int(system_mask)
    reserved = max(0, int(reserved))
    if system_mask <= 0 or reserved <= 0:
        return system_mask
    bits = [i for i in range(system_mask.bit_length()) if system_mask & (1 << i)]
    if reserved >= len(bits):
        return system_mask
    mask = 0
    for bit in bits[:-reserved]:
        mask |= 1 << bit
    return mask


def format_cpu_list(cpus):
    cpus = sorted(int(cpu) for cpu in cpus)
    if not cpus:
        return ""
    ranges = []
    start = prev = cpus[0]
    for cpu in cpus[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = cpu
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def cpus_from_mask(mask):
    mask = int(mask)
    return [i for i in range(mask.bit_length()) if mask & (1 << i)]


def windows_active_processor_count():
    """All logical CPUs, including ones outside this process's processor group."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetActiveProcessorCount.argtypes = [wintypes.WORD]
    kernel32.GetActiveProcessorCount.restype = wintypes.DWORD
    return int(kernel32.GetActiveProcessorCount(0xFFFF))  # ALL_PROCESSOR_GROUPS


def windows_process_affinity_mask():
    """This process's affinity in its primary group. 0 if the query fails.

    One mask is at most 64 bits. On Windows 11 and Server 2022 a machine with
    more than 64 logical CPUs still reports only that group here.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessAffinityMask.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
    process_mask = ctypes.c_size_t()
    system_mask = ctypes.c_size_t()
    if not kernel32.GetProcessAffinityMask(
        kernel32.GetCurrentProcess(),
        ctypes.byref(process_mask),
        ctypes.byref(system_mask),
    ):
        return 0
    return int(process_mask.value or system_mask.value)


def cpu_plan(reserved=RESERVED_THREADS):
    """CPU list and counts. Same rule as xav_automation.xav_cpu_plan.

    The last `reserved` logical CPUs stay free. Too few CPUs → use all of the
    pinnable set rather than an empty affinity set. On Windows the pinnable
    set is one processor group (the process affinity mask).
    """
    total = max(1, logical_cpu_count())
    reserved = max(0, int(reserved))
    if sys.platform == "win32":
        visible = windows_process_affinity_mask()
        if not visible:
            pinnable = min(total, 64)
            visible = (1 << pinnable) - 1
        mask = mask_reserving_high_cpus(visible, reserved)
        allowed = mask.bit_count() or 1
        cpu_list = format_cpu_list(cpus_from_mask(mask)) or "0"
        return {
            "cpu_list": cpu_list,
            "allowed": allowed,
            "total": total,
            "mask": mask,
        }
    allowed = total - reserved
    if allowed < 1:
        allowed = total
    cpu_list = "0" if allowed == 1 else f"0-{allowed - 1}"
    return {
        "cpu_list": cpu_list,
        "allowed": allowed,
        "total": total,
        "mask": None,
    }


def affinity_log_line(plan):
    total = plan["total"]
    allowed = plan["allowed"]
    left = total - allowed
    if sys.platform == "win32":
        return (
            f"Windows CPUs {plan['cpu_list']} "
            f"({total} logical CPUs, {allowed} allowed, {left} left free)"
        )
    return (
        f"taskset -c {plan['cpu_list']}  "
        f"({total} logical CPUs, last {left} reserved)"
    )


def apply_windows_affinity(mask):
    """Pin this process. Later children, including ffmpeg, inherit the mask."""
    import ctypes
    from ctypes import wintypes

    if not mask:
        raise RuntimeError("Windows affinity mask is empty.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
    if not kernel32.SetProcessAffinityMask(
        kernel32.GetCurrentProcess(), ctypes.c_size_t(int(mask))
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def apply_cpu_plan(plan):
    """Pin this process to the plan. Returns how many CPUs the mask actually holds.

    VapourSynth runs in this process, so a taskset wrapper on ffmpeg alone
    would still leave the denoise on the reserved cores. Children inherit.
    """
    if sys.platform == "win32":
        apply_windows_affinity(plan["mask"])
        return plan["allowed"]
    wanted = set(range(plan["allowed"]))
    try:
        os.sched_setaffinity(0, wanted)
    except AttributeError:
        print("CPU affinity is not available on this platform. Using every logical CPU.")
        return plan["total"]
    except OSError as exc:
        raise RuntimeError(f"Could not pin CPUs {plan['cpu_list']}: {exc}") from exc
    actual = sorted(os.sched_getaffinity(0))
    if set(actual) != wanted:
        plan["allowed"] = len(actual)
        plan["cpu_list"] = format_cpu_list(actual)
    return len(actual)


def run_json(cmd):
    return json.loads(subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     check=True, text=True).stdout)


def file_is_usable(path):
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def stop_proc(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()


def pipe_output(stream):
    while True:
        chunk = stream.read(256)
        if not chunk:
            break
        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
        sys.stdout.flush()


def run_logged(args):
    """Run a command. Its output is teed to the console and the log file."""
    print(f"    - Running: {' '.join(str(a) for a in args)}")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0, start_new_session=True,
    )
    try:
        pipe_output(proc.stdout)
    except KeyboardInterrupt:
        stop_proc(proc)
        raise
    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"{args[0]} exited with code {ret}")


def list_source_mkvs(current_dir):
    return sorted(f for f in current_dir.glob("*.mkv")
                  if f.is_file() and not f.name.startswith("."))


def mediainfo_json(path):
    # -f so HDR_Format and the original frame rate are present.
    return run_json(["mediainfo", "--Output=JSON", "-f", str(path)])


def video_track(media_info):
    for track in media_info.get("media", {}).get("track", []):
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
            info = run_json([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                "-of", "json", str(source_file),
            ])
            streams = info.get("streams") or []
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
    if any(marker in text for marker in HDR_FORMAT_MARKERS):
        return True
    return any(marker in text for marker in HDR_TRANSFER_MARKERS)


def is_hlg(track):
    return any(marker in _hdr_text(track) for marker in HLG_MARKERS)


def is_4k_path(track):
    return video_height(track) > HEIGHT_4K


def is_4k_or_hdr(track):
    return is_4k_path(track) or is_hdr(track)


def x264_supports_10bit():
    """libx264 is 10-bit capable on some builds and 8-bit only on others."""
    global _X264_10BIT
    if _X264_10BIT is None:
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-h", "encoder=libx264"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=False,
            ).stdout
        except OSError:
            out = ""
        line = next((ln for ln in out.splitlines() if "Supported pixel formats" in ln), "")
        _X264_10BIT = "yuv420p10le" in line.split()
    return _X264_10BIT


def codec_plan(track):
    """xav-compatible 4:2:0. Normal GOP (all-intra is omitted on purpose).

    xav re-encodes this file, so intra-only frames only cost space.
    Above 1080p or HDR uses libx265 10-bit CRF 0, which is not bit-exact.
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
        kind = "HDR" if is_hdr(track) else "above 1080p"
        return {
            "codec": "libx265",
            "pix_fmt": "yuv420p10le",
            "hb_encoder": "x265_10bit",
            "label": f"libx265 10-bit CRF 0 yuv420p10le, normal GOP ({kind})",
            "bit_exact": False,
        }
    if need_10:
        why = "Hi10p" if depth >= 10 else f"convert {space or 'YUV'} {chroma or '?'} {depth}-bit"
        codec = "libx264"
        bit_exact = True
        note = ""
        if not x264_supports_10bit():
            codec = "libx265"
            bit_exact = False
            note = "; ffmpeg libx264 has no yuv420p10le, so libx265 CRF 0"
        return {
            "codec": codec,
            "pix_fmt": "yuv420p10le",
            "hb_encoder": "x264_10bit",
            "label": f"{codec} 10-bit CRF 0 yuv420p10le, normal GOP ({why}{note})",
            "bit_exact": bit_exact,
        }
    return {
        "codec": "libx264",
        "pix_fmt": "yuv420p",
        "hb_encoder": "x264",
        "label": "libx264 8-bit CRF 0 yuv420p, normal GOP (1080p SDR)",
        "bit_exact": True,
    }


def detect_vfr(media_info):
    """MediaInfo FrameRate_Mode. Same target-FPS choice as xav_automation.py."""
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
        packets = run_json(interval_cmd).get("packets") or []
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


def prep_is_vfr(path):
    try:
        mode = (video_track(mediainfo_json(path)) or {}).get("FrameRate_Mode") or ""
    except Exception:
        return False
    return str(mode).upper() in ("VFR", "VARIABLE")


def intermediate_cfr_ok(path):
    """A CFR intermediate we just wrote. Short files cannot fill the packet probe."""
    if prep_is_vfr(path):
        return False, "MediaInfo still VFR"
    ok, reason, _nominal = probe_true_cfr(path)
    if ok or "too few" in reason:
        return True, reason
    return False, reason


def run_handbrake_cfr(source_file, output_file, plan, target_fps):
    """Video-only CFR. Same HandBrake flags as xav, without all-intra --encopts."""
    if not shutil.which("HandBrakeCLI"):
        print("    - HandBrakeCLI is not in PATH.")
        return False
    print(f"    - HandBrakeCLI CFR: encoder={plan['hb_encoder']} ({plan['label']}), rate {target_fps}")
    args = [
        "HandBrakeCLI",
        "--input", str(source_file),
        "--output", str(output_file),
        "--cfr",
        "--rate", str(target_fps),
        "--encoder", plan["hb_encoder"],
        "--quality", "0",
        "--encoder-preset", "superfast",
        "--encoder-tune", "fastdecode",
        "--audio", "none",
        "--subtitle", "none",
        "--crop-mode", "none",
        "--no-markers",
    ]
    try:
        run_logged(args)
    except RuntimeError as exc:
        print(f"    - Warning: {exc}")
        return False
    return file_is_usable(output_file)


def run_ffmpeg_cfr(source_file, output_file, plan, track, color):
    """Fallback when HandBrake produces nothing usable. Forced constant timestamps."""
    fps = video_fps(track, source_file)
    print(f"    - ffmpeg CFR fallback: {plan['label']}")
    args = [
        "ffmpeg", "-hide_banner", "-v", "error", "-stats", "-y",
        "-fflags", "+genpts",
        "-i", str(source_file),
        "-map", "0:v:0",
        *ffmpeg_codec_args(plan, color),
        "-fps_mode", "cfr",
    ]
    if fps:
        args += ["-r", str(fps)]
        print(f"    - Forcing CFR at {fps}")
    args += [
        "-an", "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        str(output_file),
    ]
    try:
        run_logged(args)
    except RuntimeError as exc:
        print(f"    - Warning: {exc}")
        return False
    return file_is_usable(output_file)


def prepare_cfr_source(file_path, track, is_vfr, target_cfr_fps, plan, color):
    """Return the file MDegrain should read.

    Packet-CFR and MediaInfo not VFR: the source itself.
    Otherwise a video-only CFR intermediate. Raises if that cannot be made.
    """
    true_cfr, cfr_reason, _nominal = probe_true_cfr(file_path, track)
    if true_cfr:
        print(f"    - Packet CFR probe: PASS ({cfr_reason})")
    else:
        print(f"    - Packet CFR probe: FAIL ({cfr_reason}) → CFR conversion")
    if is_vfr:
        print("    - MediaInfo VFR: HandBrake CFR hammer (probe cannot skip)")
    if true_cfr and not is_vfr:
        print("    - Denoise source is the original file.")
        return file_path

    cfr_file = file_path.with_name(f".{file_path.stem}{CFR_TMP_SUFFIX}")
    cfr_file.unlink(missing_ok=True)
    fps = handbrake_rate(track, file_path, target_cfr_fps if is_vfr else None)
    made = False
    if fps:
        made = run_handbrake_cfr(file_path, cfr_file, plan, fps)
        if made:
            ok, why = intermediate_cfr_ok(cfr_file)
            if not ok:
                print(f"    - HandBrake output is not CFR ({why}). Falling back to ffmpeg.")
                made = False
        else:
            print("    - Warning: HandBrakeCLI produced no usable file. Falling back to ffmpeg.")
    else:
        print("    - Warning: could not determine FPS for HandBrake. Falling back to ffmpeg.")
    if not made:
        cfr_file.unlink(missing_ok=True)
        if not run_ffmpeg_cfr(file_path, cfr_file, plan, track, color):
            raise RuntimeError("CFR conversion failed (HandBrake and ffmpeg)")
        ok, why = intermediate_cfr_ok(cfr_file)
        if not ok:
            raise RuntimeError(f"CFR conversion output is not CFR ({why})")
    print(f"    - Denoise source is the CFR intermediate: {cfr_file.name}")
    return cfr_file


def _known_color(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "unknown", "unspecified", "n/a"):
        return None
    return text


def _as_int(value):
    try:
        return int(float(str(value).split()[0]))
    except (TypeError, ValueError):
        return None


def _frac_float(text):
    raw = str(text)
    if "/" in raw:
        num, den = raw.split("/", 1)
        den_f = float(den)
        if not den_f:
            raise ValueError(raw)
        return float(num) / den_f
    return float(raw)


def _scaled_int(text, scale):
    return int(round(_frac_float(text) * scale))


def _master_from_side(item):
    if not item:
        return None
    try:
        rx, ry = _scaled_int(item["red_x"], 50000), _scaled_int(item["red_y"], 50000)
        gx, gy = _scaled_int(item["green_x"], 50000), _scaled_int(item["green_y"], 50000)
        bx, by = _scaled_int(item["blue_x"], 50000), _scaled_int(item["blue_y"], 50000)
        wx = _scaled_int(item["white_point_x"], 50000)
        wy = _scaled_int(item["white_point_y"], 50000)
        lmax = _scaled_int(item["max_luminance"], 10000)
        lmin = _scaled_int(item["min_luminance"], 10000)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if lmax <= lmin:
        return None
    return f"G({gx},{gy})B({bx},{by})R({rx},{ry})WP({wx},{wy})L({lmax},{lmin})"


def _master_from_container(props):
    chroma = props.get("chromaticity_coordinates")
    white = props.get("white_color_coordinates")
    if not chroma or not white:
        return None
    if props.get("max_luminance") is None or props.get("min_luminance") is None:
        return None
    try:
        rx, ry, gx, gy, bx, by = (float(part) for part in str(chroma).split(","))
        wx, wy = (float(part) for part in str(white).split(","))
        lmax = int(round(float(props["max_luminance"]) * 10000))
        lmin = int(round(float(props["min_luminance"]) * 10000))
    except (TypeError, ValueError):
        return None
    if lmax <= lmin:
        return None

    def unit(value):
        return int(round(value * 50000))

    return (
        f"G({unit(gx)},{unit(gy)})B({unit(bx)},{unit(by)})"
        f"R({unit(rx)},{unit(ry)})WP({unit(wx)},{unit(wy)})L({lmax},{lmin})"
    )


def _cll_from_side(item):
    if not item:
        return None
    try:
        return f"{int(item['max_content'])},{int(item['max_average'])}"
    except (KeyError, TypeError, ValueError):
        return None


def ffprobe_color(path):
    """Stream color tags, plus mastering-display / CLL from the first frame."""
    info = run_json([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=color_range,color_space,color_transfer,color_primaries,pix_fmt",
        "-of", "json", str(path),
    ])
    stream = (info.get("streams") or [{}])[0]
    side = {}
    try:
        frames = run_json([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-read_intervals", "%+#1",
            "-show_frames", "-show_entries", "frame_side_data",
            "-of", "json", str(path),
        ])
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError):
        frames = {}
    for frame in frames.get("frames") or []:
        for item in frame.get("side_data_list") or []:
            kind = item.get("side_data_type") or ""
            if "Mastering display" in kind and "master" not in side:
                side["master"] = item
            elif "Content light level" in kind and "cll" not in side:
                side["cll"] = item
    return stream, side


def _put_mux(mux, flag, value):
    if value is None or value == "":
        return
    mux[flag] = value


def _decimal_text(value):
    """mkvmerge rejects luminance written as '1e-05'."""
    number = float(value)
    text = f"{number:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def resolve_color(src_props, probed, side, track):
    """One color description for the encoder VUI and the MKV Colour elements.

    libx264 / libx265 ignore ffmpeg -color_primaries and -color_trc. The VUI
    has to be set with encoder params. Container Colour is set again in
    mkvmerge so a later video-only remux still carries HDR10 metadata.
    """
    props = src_props or {}
    names = {
        "color_primaries": None,
        "color_transfer": None,
        "color_space": None,
        "color_range": None,
    }
    iso = {
        "primaries": _as_int(props.get("color_primaries")),
        "transfer": _as_int(props.get("color_transfer_characteristics")),
        "matrix": _as_int(props.get("color_matrix_coefficients")),
        "range": _as_int(props.get("color_range")),
    }
    if iso["primaries"] in ISO_PRIMARIES:
        names["color_primaries"] = ISO_PRIMARIES[iso["primaries"]]
    if iso["transfer"] in ISO_TRANSFER:
        names["color_transfer"] = ISO_TRANSFER[iso["transfer"]]
    if iso["matrix"] in ISO_MATRIX:
        names["color_space"] = ISO_MATRIX[iso["matrix"]]
    if iso["range"] == 1:
        names["color_range"] = "tv"
    elif iso["range"] == 2:
        names["color_range"] = "pc"

    probed_names = {
        "color_primaries": _known_color(probed.get("color_primaries")),
        "color_transfer": _known_color(probed.get("color_transfer")),
        "color_space": _known_color(probed.get("color_space")),
        "color_range": _known_color(probed.get("color_range")),
    }
    if probed_names["color_range"] in ("mpeg", "limited"):
        probed_names["color_range"] = "tv"
    elif probed_names["color_range"] in ("jpeg", "full"):
        probed_names["color_range"] = "pc"
    for key, value in probed_names.items():
        if names[key] is None and value:
            names[key] = value

    if is_hdr(track):
        if names["color_primaries"] is None:
            names["color_primaries"] = "bt2020"
        if names["color_transfer"] is None:
            names["color_transfer"] = "arib-std-b67" if is_hlg(track) else "smpte2084"
        if names["color_space"] in (None, "gbr", "rgb"):
            names["color_space"] = "bt2020nc"
        if names["color_range"] is None:
            names["color_range"] = "tv"
    elif names["color_space"] in ("gbr", "rgb"):
        # Output is YUV. A GBR matrix tag would describe the source packing.
        names["color_space"] = "bt709"

    if names["color_primaries"] not in PRIMARIES_ISO:
        names["color_primaries"] = None
    if names["color_transfer"] not in TRANSFER_ISO:
        names["color_transfer"] = None
    if names["color_space"] not in MATRIX_ISO:
        names["color_space"] = None

    mux = {}
    if names["color_primaries"]:
        _put_mux(mux, "--color-primaries", PRIMARIES_ISO[names["color_primaries"]])
    if names["color_transfer"]:
        _put_mux(mux, "--color-transfer-characteristics", TRANSFER_ISO[names["color_transfer"]])
    if names["color_space"]:
        _put_mux(mux, "--color-matrix-coefficients", MATRIX_ISO[names["color_space"]])
    if names["color_range"] == "tv":
        _put_mux(mux, "--color-range", 1)
    elif names["color_range"] == "pc":
        _put_mux(mux, "--color-range", 2)
    bits = props.get("color_bits_per_channel")
    if bits is not None:
        _put_mux(mux, "--color-bits-per-channel", bits)

    master = _master_from_side(side.get("master")) or _master_from_container(props)
    cll = _cll_from_side(side.get("cll"))
    if cll is None and props.get("max_content_light") is not None and props.get("max_frame_light") is not None:
        cll = f"{int(float(props['max_content_light']))},{int(float(props['max_frame_light']))}"
    if props.get("chromaticity_coordinates"):
        _put_mux(mux, "--chromaticity-coordinates", props["chromaticity_coordinates"])
    elif side.get("master"):
        item = side["master"]
        try:
            coords = ",".join(
                f"{_frac_float(item[key]):.5f}"
                for key in ("red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y")
            )
            white = f"{_frac_float(item['white_point_x']):.5f},{_frac_float(item['white_point_y']):.5f}"
            _put_mux(mux, "--chromaticity-coordinates", coords)
            _put_mux(mux, "--white-color-coordinates", white)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
    if props.get("white_color_coordinates") and "--white-color-coordinates" not in mux:
        _put_mux(mux, "--white-color-coordinates", props["white_color_coordinates"])
    if props.get("max_luminance") is not None:
        _put_mux(mux, "--max-luminance", _decimal_text(props["max_luminance"]))
    elif side.get("master") and side["master"].get("max_luminance") is not None:
        try:
            _put_mux(mux, "--max-luminance", _decimal_text(_frac_float(side["master"]["max_luminance"])))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if props.get("min_luminance") is not None:
        _put_mux(mux, "--min-luminance", _decimal_text(props["min_luminance"]))
    elif side.get("master") and side["master"].get("min_luminance") is not None:
        try:
            _put_mux(mux, "--min-luminance", _decimal_text(_frac_float(side["master"]["min_luminance"])))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if cll:
        content, average = cll.split(",", 1)
        _put_mux(mux, "--max-content-light", content)
        _put_mux(mux, "--max-frame-light", average)
    return {"names": names, "master": master, "cll": cll, "mux": mux}


def vui_parts(color, codec):
    names = color["names"]
    parts = []
    if names.get("color_primaries"):
        parts.append(f"colorprim={names['color_primaries']}")
    if names.get("color_transfer"):
        parts.append(f"transfer={names['color_transfer']}")
    if names.get("color_space"):
        parts.append(f"colormatrix={names['color_space']}")
    rng = names.get("color_range")
    if codec == "libx265":
        if rng == "tv":
            parts.append("range=limited")
        elif rng == "pc":
            parts.append("range=full")
    elif codec == "libx264" and rng in ("tv", "pc"):
        parts.append(f"range={rng}")
    return parts


def ffmpeg_codec_args(plan, color):
    """CRF 0, superfast, fastdecode. No -g 1 / -bf 0."""
    codec = plan["codec"]
    args = [
        "-c:v", codec,
        "-crf", "0",
        "-preset", "superfast",
        "-tune", "fastdecode",
        "-pix_fmt", plan["pix_fmt"],
    ]
    if codec == "libx265":
        params = ["info=0", *vui_parts(color, codec)]
        if color.get("master"):
            params.append("master-display=" + color["master"])
        if color.get("cll"):
            params.append("max-cll=" + color["cll"])
        trc = color["names"].get("color_transfer")
        if color.get("master") or color.get("cll") or trc in ("smpte2084", "arib-std-b67"):
            params.append("repeat-headers=1")
        args += ["-x265-params", ":".join(params)]
    else:
        params = vui_parts(color, codec)
        if params:
            args += ["-x264-params", ":".join(params)]
    return args


def ffprobe_pix_fmt(path):
    try:
        info = run_json([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt", "-of", "json", str(path),
        ])
        return str((info.get("streams") or [{}])[0].get("pix_fmt") or "").lower()
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError, ValueError, IndexError):
        return ""


def mkv_video_track(identify):
    for track in identify.get("tracks", []):
        if track.get("type") == "video":
            return track
    raise RuntimeError("no video track found")


def conform_format(clip, pix_fmt, track):
    """4:2:2 / 4:4:4 / RGB / 12-bit+ become the xav pixel format. Matching clips pass through."""
    import vapoursynth as vs
    core = vs.core
    target = vs.YUV420P8 if pix_fmt == "yuv420p" else vs.YUV420P10
    if clip.format.id == target:
        return clip
    family = clip.format.color_family
    kwargs = {"format": target}
    if family in (vs.RGB, vs.GRAY):
        kwargs["matrix_s"] = "2020ncl" if is_hdr(track) else "709"
    elif family != vs.YUV:
        raise RuntimeError(f"unsupported color family {clip.format.name}")
    print(f"    - Converting {clip.format.name} -> {pix_fmt}")
    return core.resize.Point(clip, **kwargs)


def build_degrain(path, cachefile, thsad, threads):
    """Same filter chain as ~/bin/mdegrain.vpy."""
    import vapoursynth as vs
    core = vs.core
    core.num_threads = threads

    clip = core.ffms2.Source(str(path), cachefile=str(cachefile))
    pre = core.zsmooth.RemoveGrain(clip, mode=12)
    sup_search = core.mv.Super(pre, pel=2, sharp=2, hpad=16, vpad=16)
    sup_render = core.mv.Super(clip, pel=2, sharp=2, hpad=16, vpad=16, levels=1)
    common = dict(blksize=16, overlap=8, search=4, searchparam=2, truemotion=1, chroma=1)
    bv1 = core.mv.Analyse(sup_search, isb=True, delta=1, **common)
    fv1 = core.mv.Analyse(sup_search, isb=False, delta=1, **common)
    bv2 = core.mv.Analyse(sup_search, isb=True, delta=2, **common)
    fv2 = core.mv.Analyse(sup_search, isb=False, delta=2, **common)
    out = core.mv.Degrain2(clip, sup_render, bv1, fv1, bv2, fv2,
                           thsad=thsad, thsadc=thsad, thscd1=400, thscd2=130)
    return clip, out


def encode(out, tmp_video, plan, color):
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
        "-f", "yuv4mpegpipe", "-i", "-",
        "-map", "0:v:0",
        *ffmpeg_codec_args(plan, color),
        "-fps_mode", "cfr",
    ]
    if out.fps_num and out.fps_den:
        args += ["-r", f"{out.fps_num}/{out.fps_den}"]
    for key, opt in (
        ("color_range", "-color_range"),
        ("color_space", "-colorspace"),
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
    ):
        value = color["names"].get(key)
        if value:
            args += [opt, value]
    args += ["-map_metadata", "-1", "-map_chapters", "-1", str(tmp_video)]
    print(f"    - ffmpeg: {' '.join(args)}")

    # Own session: Ctrl+C in the terminal only reaches this script, not ffmpeg.
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        start_new_session=True,
    )

    def pump():
        while True:
            chunk = proc.stderr.read(256)
            if not chunk:
                break
            sys.stdout.write(chunk.decode("utf-8", errors="replace"))
            sys.stdout.flush()

    feed_error = []

    def feed():
        try:
            out.output(proc.stdin, y4m=True)
            proc.stdin.close()
        except BaseException as exc:
            feed_error.append(exc)

    reader = threading.Thread(target=pump, daemon=True)
    feeder = threading.Thread(target=feed, daemon=True)
    reader.start()
    start = time.monotonic()
    feeder.start()
    try:
        while feeder.is_alive():
            feeder.join(0.5)  # short waits so Ctrl+C is handled right away
    except KeyboardInterrupt:
        stop_proc(proc)
        feeder.join(10)
        reader.join(5)
        raise
    if feed_error:
        stop_proc(proc)
        reader.join(5)
        raise feed_error[0]
    ret = proc.wait()
    reader.join()
    elapsed = time.monotonic() - start
    print()
    if ret != 0:
        raise RuntimeError(f"ffmpeg exited with code {ret}")
    return elapsed


def remux(dest, tmp_video, source, src_vtrack, color):
    props = src_vtrack.get("properties", {})
    lang = props.get("language_ietf") or props.get("language") or "und"
    default_flag = "yes" if props.get("default_track", True) else "no"
    args = [
        "mkvmerge", "-o", str(dest),
        "--title", "",
        "--language", f"0:{lang}",
        "--track-name", "0:",
        "--default-track-flag", f"0:{default_flag}",
    ]
    # No --default-duration: the ffmpeg temp MKV already has correct timestamps, and
    # mkvmerge 102 can hang (stuck at ~20%) when it is forced on an MKV input.
    if props.get("display_dimensions"):
        args += ["--display-dimensions", f"0:{props['display_dimensions']}"]
    for flag, value in color["mux"].items():
        args += [flag, f"0:{value}"]
    args += [
        "--no-global-tags", "--no-track-tags", "--no-chapters", str(tmp_video),
        "--no-video", str(source),
    ]
    print(f"    - mkvmerge: {' '.join(str(a) for a in args)}")
    res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in res.stdout.splitlines():
        if not line.startswith("Progress:"):
            print(f"      {line}")
    if res.returncode >= 2:  # 1 = warnings only
        raise RuntimeError(f"mkvmerge exited with code {res.returncode}")


def count_video_packets(path):
    info = run_json([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=nb_read_packets", "-of", "json", str(path),
    ])
    return int(info["streams"][0]["nb_read_packets"])


def track_summary(identify):
    rows = []
    for track in identify.get("tracks", []):
        if track["type"] == "video":
            continue
        props = track.get("properties", {})
        rows.append((
            track["type"], track["codec"],
            props.get("language_ietf") or props.get("language"),
            props.get("track_name"), props.get("default_track"), props.get("forced_track"),
        ))
    return rows


def container_duration(identify):
    return int(identify["container"]["properties"].get("duration", 0)) / 1e9


def process_file(file_path, thsad, threads):
    tmp_video = file_path.with_name(f".{file_path.stem}{TMP_SUFFIX}")
    ffindex = file_path.with_name(f".{file_path.stem}.mdeg-tmp.ffindex")
    muxed = file_path.with_name(f".{file_path.stem}.mdeg-mux.mkv")
    # Registered up front so a failed CFR conversion still deletes its file.
    cfr_file = file_path.with_name(f".{file_path.stem}{CFR_TMP_SUFFIX}")
    temps = [tmp_video, ffindex, muxed, cfr_file]
    denoise_path = file_path
    try:
        print(f"Analyzing file: {file_path.resolve()}")
        media_info = mediainfo_json(file_path)
        track = video_track(media_info)
        if not track:
            raise RuntimeError("no video track in MediaInfo")
        scan = track.get("ScanType") or "Progressive"
        if str(scan).lower() != "progressive":
            print(f"    - WARNING: ScanType is '{scan}'. MDegrain runs on full frames (no deinterlacing).")
        hdr_text = _hdr_text(track)
        if "dolby vision" in hdr_text or "dolbyvision" in hdr_text:
            print("    - WARNING: Dolby Vision RPU is not kept. Output is the HDR10/HLG base layer.")
        if "hdr10+" in hdr_text:
            print("    - WARNING: HDR10+ dynamic metadata is not kept. Static HDR10 metadata is copied.")

        plan = codec_plan(track)
        print(f"    - Encoder: {plan['label']}")
        if not plan["bit_exact"]:
            print("    - x265 CRF 0 is the intermediate xav accepts. It is not bit-exact.")
        is_vfr, target_cfr_fps = detect_vfr(media_info)
        probed, side = ffprobe_color(file_path)
        src_id = run_json(["mkvmerge", "-J", str(file_path)])
        src_vtrack = mkv_video_track(src_id)
        color = resolve_color(src_vtrack.get("properties") or {}, probed, side, track)
        src_fmt = (probed.get("pix_fmt") or ffprobe_pix_fmt(file_path) or "unknown").lower()
        if src_fmt not in XAV_PIX_FMTS:
            print(f"    - Source pixels {src_fmt} are not yuv420p / yuv420p10le. Converting during denoise.")

        denoise_path = prepare_cfr_source(
            file_path, track, is_vfr, target_cfr_fps, plan, color,
        )

        print(f"    - Indexing (ffms2) and building MDegrain2 (thsad={thsad}, threads={threads})...")
        clip, out = build_degrain(denoise_path, ffindex, thsad, threads)
        out = conform_format(out, plan["pix_fmt"], track)
        print(
            f"    - Denoise clip: {clip.width}x{clip.height} {clip.format.name} -> {plan['pix_fmt']}, "
            f"{clip.fps_num}/{clip.fps_den} fps, {clip.num_frames} frames, scan={scan}"
        )

        print("    - Denoise encode...")
        elapsed = encode(out, tmp_video, plan, color)
        print(
            f"    - Denoise speed: {clip.num_frames / elapsed:.1f} fps "
            f"({clip.num_frames} frames in {elapsed:.0f} s)"
        )

        print("Remuxing with mkvmerge...")
        remux(muxed, tmp_video, file_path, src_vtrack, color)

        print("Checking output...")
        out_id = run_json(["mkvmerge", "-J", str(muxed)])
        out_frames = count_video_packets(muxed)
        denoise_frames = count_video_packets(denoise_path)
        print(f"    - Frames: denoise source {denoise_frames}, ffms2 {clip.num_frames}, output {out_frames}")
        if not (out_frames == denoise_frames == clip.num_frames):
            raise RuntimeError("frame count mismatch against the denoise source")
        if denoise_path != file_path:
            src_frames = count_video_packets(file_path)
            print(f"    - VFR/CFR frame counts differ as expected: source {src_frames}, CFR {denoise_frames}")
        src_dur = container_duration(src_id)
        out_dur = container_duration(out_id)
        frame_dur = clip.fps_den / clip.fps_num if clip.fps_num else 0.04
        print(f"    - Duration: source {src_dur:.3f} s, output {out_dur:.3f} s")
        if abs(src_dur - out_dur) > max(frame_dur * 3, 0.15):
            raise RuntimeError("duration mismatch")
        if track_summary(src_id) != track_summary(out_id):
            raise RuntimeError("non-video track list differs from source")
        if bool(src_id.get("chapters")) != bool(out_id.get("chapters")):
            raise RuntimeError("chapters differ from source")
        out_fmt = ffprobe_pix_fmt(muxed)
        if out_fmt not in XAV_PIX_FMTS:
            raise RuntimeError(f"output pix_fmt {out_fmt or 'unknown'} is not yuv420p or yuv420p10le")
        if out_fmt != plan["pix_fmt"]:
            raise RuntimeError(f"output pix_fmt {out_fmt} != planned {plan['pix_fmt']}")
        if prep_is_vfr(muxed):
            raise RuntimeError("output is still VFR")
        cfr_ok, cfr_why, _nominal = probe_true_cfr(muxed)
        if cfr_ok:
            print(f"    - Output packet CFR probe: PASS ({cfr_why})")
        elif "too few" in cfr_why:
            print(f"    - Output packet CFR probe skipped ({cfr_why}). MediaInfo is not VFR.")
        else:
            raise RuntimeError(f"output is not packet-CFR ({cfr_why})")
        print(f"    - OK: {out_fmt}, CFR, duration, audio/subtitle tracks and chapters match.")

        print("Moving files to final destinations...")
        shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
        shutil.move(str(muxed), DIR_COMPLETED / file_path.name)
    finally:
        for temp in temps:
            if temp != file_path and temp.exists():
                print(f"    Deleting: {temp}")
                temp.unlink(missing_ok=True)


def loaded_plugin_namespaces():
    """Namespaces VapourSynth actually loaded. Empty if the package is missing."""
    try:
        import vapoursynth as vs
    except ImportError:
        return None
    return {plugin.namespace for plugin in vs.core.plugins()}


def plugin_setup_lines(missing, path_file=PLUGIN_PATH_FILE):
    """Hints for a failed plugin check. `missing` is (namespace, package) pairs."""
    lines = ["Required VapourSynth plugin(s) not loaded:"]
    for namespace, package in missing:
        lines.append(f"  {namespace:8} package: {package}")
    namespaces = {namespace for namespace, _package in missing}
    if not path_file.is_file():
        lines.append("VapourSynth is not looking in /usr/lib/vapoursynth, so ffms2 stays hidden.")
        lines.append("Add the plugin path, then log out and back in, or reboot:")
        lines.append(f"  {PLUGIN_PATH_LINE}")
    elif "ffms2" in namespaces and not os.environ.get("VAPOURSYNTH_EXTRA_PLUGIN_PATH"):
        lines.append(f"{path_file} is installed, but this session does not have VAPOURSYNTH_EXTRA_PLUGIN_PATH.")
        lines.append("Log out and back in, or reboot, so new terminals inherit it.")
        lines.append(f"In this terminal: source {path_file}")
    return lines


def environment_status():
    """Required programs, HandBrake, and the VapourSynth plugins."""
    tool_missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    handbrake = shutil.which("HandBrakeCLI") is not None
    loaded = loaded_plugin_namespaces()
    if loaded is None:
        plugin_missing = list(REQUIRED_PLUGINS)
    else:
        plugin_missing = [(namespace, package) for namespace, package in REQUIRED_PLUGINS if namespace not in loaded]
    return tool_missing, handbrake, loaded, plugin_missing


def check_environment(report_only=False):
    """Exit if a required program or plugin is missing.

    report_only prints every item and does not encode. A normal run prints
    only the failures.
    """
    tool_missing, handbrake, loaded, plugin_missing = environment_status()
    if not report_only:
        if tool_missing:
            sys.exit(f"Required tool(s) not found in PATH: {', '.join(tool_missing)}")
        if loaded is None:
            sys.exit("Required Python package 'vapoursynth' is not installed.")
        if plugin_missing:
            for line in plugin_setup_lines(plugin_missing):
                print(line)
            sys.exit(1)
        return

    print("Check (no encode)")
    for tool in REQUIRED_TOOLS:
        state = "MISSING" if tool in tool_missing else "OK"
        print(f"  {state:7} {tool}")
    hb_state = "OK" if handbrake else "MISSING"
    print(f"  {hb_state:7} HandBrakeCLI (optional; VFR uses ffmpeg without it)")
    if loaded is None:
        print("  MISSING vapoursynth (Python package)")
    else:
        print("  OK      vapoursynth (Python package)")
    missing_namespaces = {namespace for namespace, _package in plugin_missing}
    for namespace, package in REQUIRED_PLUGINS:
        state = "MISSING" if loaded is None or namespace in missing_namespaces else "OK"
        print(f"  {state:7} {namespace} (package {package})")
    path_state = "OK" if PLUGIN_PATH_FILE.is_file() else "MISSING"
    print(f"  {path_state:7} {PLUGIN_PATH_FILE}")
    extra = os.environ.get("VAPOURSYNTH_EXTRA_PLUGIN_PATH")
    if extra:
        print(f"  OK      VAPOURSYNTH_EXTRA_PLUGIN_PATH={extra}")
    else:
        print("  MISSING VAPOURSYNTH_EXTRA_PLUGIN_PATH in this session")
    if x264_supports_10bit():
        print("  OK      libx264 yuv420p10le")
    else:
        print("  note    libx264 has no yuv420p10le; 1080p 10-bit uses libx265 CRF 0")
    failed = bool(tool_missing or loaded is None or plugin_missing)
    if loaded is None:
        print("Required Python package 'vapoursynth' is not installed.")
    elif plugin_missing:
        for line in plugin_setup_lines(plugin_missing):
            print(line)
    if tool_missing:
        print(f"Missing required tools: {', '.join(tool_missing)}")
    if not handbrake:
        print("HandBrakeCLI was not found. VFR and failed CFR probes will use the ffmpeg fallback.")
    if failed:
        sys.exit(1)
    print("All required tools and plugins are available.")


def main(thsad, reserved_threads):
    signal.signal(signal.SIGINT, on_sigint)
    plan = cpu_plan(reserved_threads)
    threads = apply_cpu_plan(plan)

    check_environment()
    if not shutil.which("HandBrakeCLI"):
        print("Warning: HandBrakeCLI not found. VFR and failed CFR probes will use the ffmpeg fallback.")

    current_dir = Path(".")
    if not list_source_mkvs(current_dir):
        print("No MKV files found to process. Exiting.")
        return
    for folder in (DIR_COMPLETED, DIR_ORIGINAL, DIR_CONV_LOGS, DIR_FAILED):
        folder.mkdir(exist_ok=True, parents=True)
    print(f"mdegrain: thsad={thsad}, CPU affinity {affinity_log_line(plan)} ({threads} threads)")

    while True:
        files = list_source_mkvs(current_dir)
        if not files:
            print("No more .mkv files found to process in the current directory. The script will now exit.")
            break
        file_path = files[0]
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        print("-" * width)
        log_file_path = DIR_CONV_LOGS / f"{file_path.stem}.log"
        console_out, console_err = sys.stdout, sys.stderr
        print(f"Processing: {file_path.name}")
        print(f"Logging output to: {log_file_path}")
        started = datetime.now()
        failed = False
        with open(log_file_path, "w", encoding="utf-8", buffering=1) as log:
            sys.stdout = Tee(log, console_out)
            sys.stderr = Tee(log, console_err)
            try:
                print(f"STARTING LOG FOR: {file_path.name}")
                print(f"Processing started at: {started}")
                print("-" * width)
                process_file(file_path, thsad, threads)
            except KeyboardInterrupt:
                print("\nInterrupted. Source left in place, temp files removed.")
                sys.stdout, sys.stderr = console_out, console_err
                sys.exit(130)
            except Exception as exc:
                if INTERRUPTED:
                    print("\nInterrupted. Source left in place, temp files removed.")
                    sys.stdout, sys.stderr = console_out, console_err
                    sys.exit(130)
                print(f"ERROR: An error occurred while processing '{file_path.name}': {exc}", file=sys.stderr)
                failed = True
            finally:
                runtime = str(datetime.now() - started).split(".")[0]
                print(f"FINISHED LOG FOR: {file_path.name}")
                print(f"\nTotal runtime for this file: {runtime}")
                sys.stdout, sys.stderr = console_out, console_err
        if failed:
            if file_path.exists():
                shutil.move(str(file_path), DIR_FAILED / file_path.name)
                console_err.write(f"Moved to {DIR_FAILED / file_path.name}\n")
            console_err.write(f"File: {file_path.name}\nLog: {log_file_path}\nRuntime: {runtime}\n")
        else:
            console_out.write(f"File: {file_path.name}\nLog: {log_file_path}\nRuntime: {runtime}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="mdegrain.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Denoise every MKV in the current folder, one file at a time.

For each *.mkv:
  1. VFR, or a failed packet-CFR probe, is converted to CFR first
     (HandBrake --cfr --rate, same FPS choice as xav_automation.py;
     ffmpeg is the fallback). Output uses a normal GOP.
  2. MDegrain2 denoise (mvtools, same filter as ~/bin/mdegrain.vpy).
     Strength is thsad 350, or --low (150), --high (500), or --thsad N.
  3. encode to yuv420p or yuv420p10le, the formats xav accepts
       1080p SDR 8-bit 4:2:0 -> libx264 CRF 0
       other 1080p SDR        -> libx264 10-bit CRF 0
       above 1080p or HDR     -> libx265 10-bit CRF 0 (not bit-exact)
  4. remux: new video + all audio, subs, chapters, attachments from the source
  5. checks frames, duration, pixel format, CFR, tracks and chapters

Folders (created in the current folder, like xav_automation.py):
  completed/   finished files, ready for the xav_automation.py queue
  original/    the untouched sources
  failed/      sources that failed
  conv_logs/   one log per file

HDR10 static metadata (primaries, PQ/HLG, mastering display, MaxCLL) is
copied onto the new video. Dolby Vision RPU and HDR10+ dynamic metadata
are not kept. x265 CRF 0 above 1080p or for HDR is not bit-exact.

Needs ffmpeg, ffprobe, mkvmerge, mediainfo, and the VapourSynth plugins
ffms2, mvtools, and zsmooth. If a plugin does not load, the script names
the package and, when needed, the /etc/profile.d plugin-path line.

CPU pin matches xav_automation.py. The script counts logical CPUs and
leaves the last --reserve-threads (default 2) free, so a 32-thread CPU
runs on CPUs 0-29. VapourSynth, ffmpeg, and HandBrake all stay on that
set. The last cores stay free for the desktop.

--help prints this text and exits. --check reports the CPU plan, programs,
and plugins, then exits. Neither one encodes.""",
        epilog="""\
examples:
  mdegrain.py --help
      show this help and exit

  mdegrain.py --check
      show the CPU plan and check programs and plugins, then exit. Does not encode.

  mdegrain.py
      denoise all MKVs in the current folder (thsad 350). On a 32-thread CPU
      this pins CPUs 0-29 and leaves CPUs 30-31 free.

  mdegrain.py --low
      lighter denoise (thsad 150, keeps more grain/detail)

  mdegrain.py --high
      stronger denoise (thsad 500)

  mdegrain.py --thsad 250
      set the strength yourself

  mdegrain.py --reserve-threads 4
      leave the last 4 logical CPUs free (default = 2; 32 threads → CPUs 0-27)

  cd ~/video-input/somefolder && mdegrain.py
      typical run inside a GNU screen session

thsad guide: 150 = light, 350 = default, 500+ = strong""",
    )
    parser.add_argument("--thsad", type=int, default=None, metavar="N",
                        help=f"MDegrain2 thsad/thsadc strength (default: {DEFAULT_THSAD}).")
    parser.add_argument("--low", action="store_true",
                        help=f"Light denoise. Same as --thsad {THSAD_LOW}.")
    parser.add_argument("--high", action="store_true",
                        help=f"Strong denoise. Same as --thsad {THSAD_HIGH}.")
    parser.add_argument(
        "--reserve-threads",
        type=int,
        default=RESERVED_THREADS,
        metavar="N",
        help=(
            "Logical CPUs at the high end that denoise and encode must not use "
            f"(default: {RESERVED_THREADS}; 32 threads → CPUs 0-29). "
            "Same reservation as xav_automation.py. "
            "VapourSynth thread count is the CPUs that remain."
        ),
    )
    parser.add_argument("--check", action="store_true",
                        help="Show the CPU plan and check required programs and VapourSynth plugins, then exit. Does not encode.")
    args = parser.parse_args()
    chosen = []
    if args.low:
        chosen.append("--low")
    if args.high:
        chosen.append("--high")
    if args.thsad is not None:
        chosen.append("--thsad")
    if len(chosen) > 1:
        print(
            "Warning: " + " and ".join(chosen) + " cannot be combined. "
            "Nothing was done.\n"
            "Use one of these:\n"
            "  mdegrain.py\n"
            "  mdegrain.py --low\n"
            "  mdegrain.py --high\n"
            "  mdegrain.py --thsad N"
        )
        sys.exit(0)
    if args.low:
        thsad = THSAD_LOW
    elif args.high:
        thsad = THSAD_HIGH
    elif args.thsad is not None:
        thsad = args.thsad
    else:
        thsad = DEFAULT_THSAD
    if args.check:
        print(f"CPU plan: {affinity_log_line(cpu_plan(args.reserve_threads))}")
        check_environment(report_only=True)
        sys.exit(0)
    main(thsad, args.reserve_threads)
