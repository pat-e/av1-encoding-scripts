#!/usr/bin/env python3

"""Recommend a safe FFmpeg crop, including short artistic aspect-ratio inserts.

Different "odd frames" problems are handled separately:

* Opening logos / end credits (first and last 5% by default) are ignored.
  Scrolling credits are a sea of black with a small text column, so cropdetect
  reports a much *tighter* window and would cut the movie. Logos are often a
  different AR. Neither should vote.
* Dirt / reel flakes: a bright speck in the letterbox makes cropdetect report a
  *larger* picture. A luma check on those bar strips still sees almost-black
  (one speck does not move YAVG), so the expansion is ignored. Brief hits are
  also ignored by --min-insert.
* Too-dark scenes: cropdetect treats dark picture as extra bars and reports a
  *tighter* crop. Frames with low full-frame YAVG (normalized to 8-bit 0–255)
  are skipped so they cannot vote. Tighter dark-edge detections strictly inside
  the majority crop also cannot falsely trigger mixed-AR warnings.
* Artistic AR changes (Fallout 4:3 flashback, IMAX, open matte): these last
  *seconds*, and the would-be bars are full of picture (bar YAVG jumps vs the
  majority letterbox). They are kept; the recommended crop is the outer box.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_RESET = "\033[0m"

# Normalized 0..1 limit (24/255 = FFmpeg's default) scales automatically to
# 8-bit (24), 10-bit (96), and 12-bit (385) sources. Passing integer 24 breaks
# 10-bit limited-range video where black bars sit at Y=64.
# reset=1 recalculates per frame; skip=0 avoids skipping the first 2 frames.
CROPDETECT_LIMIT = 24.0 / 255.0
CROPDETECT_FILTER = f"cropdetect=limit={CROPDETECT_LIMIT:.7f}:round=4:reset=1:skip=0"

# Full-frame YAVG on 8-bit full-range gray (0=black, 255=white).
# Below this, cropdetect sees dark picture as bars (false tight crop).
DEFAULT_LUMA_MIN = 16.0
# Bar-strip YAVG must beat the majority-letterbox baseline by this much (0–255)
# to count as real picture. A flake leaves bar YAVG almost unchanged.
DEFAULT_BAR_DELTA = 12.0
# Opening logos and scrolling end credits. Not used for artistic inserts in the body.
DEFAULT_EDGE_FRAC = 0.05
# A different AR must last at least this long in the body before it can veto the
# majority crop. Dirt/flakes are 1–2 frames; a Fallout flashback is seconds.
DEFAULT_MIN_INSERT = 6.0

# Auto mode uses keyframe-only scanning (-skip_frame nokey) for files up to this
# size when the codec is inter-frame; larger files (e.g. 50+ GB UHD remuxes over
# HDD/NAS) fall back to fast keyframe-seeking so they don't read tens of GBs.
AUTO_KEYFRAME_MAX_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB
INTRA_ONLY_CODECS = frozenset({
    "prores", "dnxhd", "mjpeg", "ffv1", "rawvideo", "utvideo",
    "huffyuv", "ffvhuff", "magicyuv", "cfhd", "v210", "v410",
})
TARGET_AUTO_SEEKS = 360
MAX_KEYFRAME_SPAN = 12.0
MAX_BAR_LUMA_SAMPLES = 24

XY_TOL = 8
SIZE_TOL = 16
AR_TOLERANCE = 0.03

CROP_LINE_RE = re.compile(r"\bt:([0-9.-]+).*?\bcrop=(\d+):(\d+):(\d+):(\d+)")
YAVG_LINE_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")

KNOWN_ASPECT_RATIOS = (
    {"name": "HDTV (16:9)", "ratio": 16 / 9},
    {"name": "Widescreen (Scope)", "ratio": 2.39},
    {"name": "Widescreen (CinemaScope)", "ratio": 2.35},
    {"name": "Widescreen (Flat)", "ratio": 1.85},
    {"name": "IMAX Digital (1.90:1)", "ratio": 1.90},
    {"name": "Fullscreen (4:3)", "ratio": 4 / 3},
    {"name": "IMAX 70mm (1.43:1)", "ratio": 1.43},
)


def check_prerequisites():
    print("--- Prerequisite Check ---")
    all_found = True
    for tool in ["ffmpeg", "ffprobe"]:
        if not shutil.which(tool):
            print(f"Error: '{tool}' command not found. Is it installed and in your PATH?")
            all_found = False
    if not all_found:
        sys.exit(1)
    print("All required tools found.")


def format_timestamp(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_crop_string(crop_str):
    try:
        _, values = crop_str.split("=")
        w, h, x, y = map(int, values.split(":"))
        return {"w": w, "h": h, "x": x, "y": y}
    except (ValueError, IndexError, AttributeError):
        return None


def crop_to_string(rect):
    return f"crop={rect['w']}:{rect['h']}:{rect['x']}:{rect['y']}"


def round_up_mod4(value):
    if value % 4 == 0:
        return value
    return value + (4 - (value % 4))


def even_offset(value):
    return value - 1 if value % 2 else value


def crops_similar(a, b, xy_tol=XY_TOL, size_tol=SIZE_TOL):
    return (
        abs(a["x"] - b["x"]) <= xy_tol
        and abs(a["y"] - b["y"]) <= xy_tol
        and abs(a["w"] - b["w"]) <= size_tol
        and abs(a["h"] - b["h"]) <= size_tol
    )


def crop_contained_in(inner, outer, margin=0):
    """True if inner's picture lies fully inside outer (with optional px margin)."""
    return (
        inner["x"] >= outer["x"] - margin
        and inner["y"] >= outer["y"] - margin
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + margin
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + margin
    )


def median(values):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def pick_evenly(items, max_count):
    if len(items) <= max_count:
        return list(items)
    if max_count <= 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (max_count - 1)
    return [items[round(i * step)] for i in range(max_count)]


def expanding_bar_regions(majority, candidate, video_w, video_h, min_side=2):
    """Bar strips of the majority crop that `candidate` actually expands into.

    If `candidate` is None, returns all bar strips discarded by `majority`.
    """
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


def is_major_crop(crop_str, video_w, video_h, min_crop_size):
    parsed = parse_crop_string(crop_str)
    if not parsed:
        return False
    top = parsed["y"]
    bottom = video_h - (parsed["y"] + parsed["h"])
    left = parsed["x"]
    right = video_w - (parsed["x"] + parsed["w"])
    return max(top, bottom, left, right) >= min_crop_size


def is_full_frame(crop_str, video_w, video_h):
    parsed = parse_crop_string(crop_str)
    return bool(parsed and parsed["w"] == video_w and parsed["h"] == video_h)


def is_edge_timestamp(ts, duration, edge_frac):
    if duration <= 0 or edge_frac <= 0:
        return False
    edge = duration * edge_frac
    return ts < edge or ts > duration - edge


def min_body_hits(interval, min_insert):
    """How many grid samples a minority AR needs in seek mode."""
    interval = max(float(interval), 1e-6)
    return max(2, math.ceil(float(min_insert) / interval))


def sample_times(duration, interval, offset=0.0):
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


def parse_crop_and_luma_stderr(stderr_text, base_ts=0.0):
    """Pair each cropdetect line with the matching signalstats YAVG line."""
    samples = []
    pending_crop = None
    pending_ts = None
    for line in stderr_text.splitlines():
        m_crop = CROP_LINE_RE.search(line)
        if m_crop:
            if pending_crop is not None:
                samples.append({"ts": pending_ts, "crop": pending_crop, "luma": None})
            raw_t = float(m_crop.group(1))
            w, h, x, y = map(int, m_crop.groups()[1:])
            pending_ts = max(0.0, base_ts + raw_t)
            pending_crop = f"crop={w}:{h}:{x}:{y}"
            continue
        if pending_crop is not None:
            m_yavg = YAVG_LINE_RE.search(line)
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


def build_sample_vf():
    # cropdetect runs at full native resolution; signalstats runs on a tiny
    # 8-bit full-range gray copy (0=black, 255=white) for speed and bit-depth
    # independence across 8/10/12-bit SDR & HDR sources.
    return (
        f"{CROPDETECT_FILTER},"
        "scale=w='min(iw,160)':h=-2:flags=fast_bilinear,"
        "format=gray,signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG"
    )


def scan_keyframe_chunk(task_args):
    """Scan all keyframes in [start, start + length] with one ffmpeg process."""
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
        "-vf", build_sample_vf(),
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
    samples = parse_crop_and_luma_stderr(result.stderr, base_ts=start)
    # Trim any keyframe that fell well outside our chunk window on seek-back.
    end = start + length
    return [s for s in samples if s["ts"] <= end + 0.5]


def run_keyframe_scan(input_file, duration, video_stream_idx, num_workers, label):
    """Split the file into parallel chunks and decode keyframes only."""
    # Use enough chunks to keep all workers busy without over-splitting short clips.
    num_chunks = max(1, min(num_workers * 2, max(1, int(duration // 15))))
    if duration < 30:
        num_chunks = 1
    chunk_len = duration / num_chunks
    tasks = [
        (round(i * chunk_len, 3), round(chunk_len, 3), input_file, video_stream_idx)
        for i in range(num_chunks)
    ]
    workers = max(1, min(num_workers, len(tasks)))
    print(f"{label}: scanning all keyframes in {len(tasks)} chunk(s) across {workers} worker(s)...")
    raw_samples = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan_keyframe_chunk, task) for task in tasks]
        total = len(futures)
        for i, fut in enumerate(as_completed(futures), 1):
            raw_samples.extend(fut.result())
            sys.stdout.write(f"\r{label}: {i}/{total} chunks completed...")
            sys.stdout.flush()
    print()

    # Sort by timestamp and deduplicate boundary keyframes.
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


def analyze_seek_sample(task_args):
    """Fast keyframe seek (-noaccurate_seek) for 1 frame of cropdetect + luma."""
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
        "-vf", build_sample_vf(),
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
    parsed = parse_crop_and_luma_stderr(result.stderr, base_ts=seek_time)
    if not parsed:
        return {"ts": seek_time, "crop": None, "luma": None}
    sample = parsed[-1]
    sample["ts"] = seek_time
    return sample


def run_seek_samples(input_file, times, video_stream_idx, num_workers, label):
    if not times:
        return []
    tasks = [(t, input_file, video_stream_idx) for t in times]
    results = []
    workers = max(1, min(num_workers, len(tasks)))
    print(f"{label}: {len(tasks)} seeks across {workers} worker(s)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(analyze_seek_sample, task) for task in tasks]
        total = len(futures)
        for i, fut in enumerate(as_completed(futures), 1):
            results.append(fut.result())
            sys.stdout.write(f"\r{label}: {i}/{total} completed...")
            sys.stdout.flush()
    print()
    results.sort(key=lambda s: s["ts"])
    return results


def attach_sample_durations(samples, duration, fixed_interval=None):
    """Attach estimated scene coverage `dt` (seconds) to each sample."""
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
        s["dt"] = min(span, MAX_KEYFRAME_SPAN)
    return ordered


def analyze_bar_luma(task_args):
    """Measure normalized 8-bit YAVG (0–255) of the strips the majority crop discards.

    A reel flake is a few bright pixels in a black bar: YAVG barely moves.
    A 4:3 / IMAX insert fills those strips with picture: YAVG jumps.
    """
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
    for raw in YAVG_LINE_RE.findall(result.stderr):
        try:
            yavgs.append(float(raw))
        except ValueError:
            pass
    # Max across expanded strips so an asymmetric expansion isn't diluted by a black bar.
    bar_luma = max(yavgs) if yavgs else None
    return {"ts": seek_time, "bar_luma": bar_luma}


def run_bar_luma(input_file, video_stream_idx, times, crops, num_workers, label, accurate=False):
    if not times or not crops:
        return []
    tasks = [(t, input_file, video_stream_idx, crops, accurate) for t in times]
    results = []
    workers = max(1, min(num_workers, len(tasks)))
    print(f"{label}: {len(tasks)} bar-luma check(s) across {workers} worker(s)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(analyze_bar_luma, task) for task in tasks]
        total = len(futures)
        for i, fut in enumerate(as_completed(futures), 1):
            results.append(fut.result())
            sys.stdout.write(f"\r{label}: {i}/{total} completed...")
            sys.stdout.flush()
    print()
    return results


def snap_to_known_ar(w, h, x, y, video_w, video_h, tolerance=AR_TOLERANCE):
    """Snap to a known AR without shrinking the picture or mutating the AR table."""
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
        new_h = round_up_mod4(round(video_w / best_match["ratio"]))
        if new_h < h:
            new_h = round_up_mod4(h)
            ar_name = f"Custom AR (Near {best_match['name']})"
        new_h = min(new_h, video_h)
        new_y = y + round((h - new_h) / 2)
        new_y = max(0, min(new_y, video_h - new_h))
        new_y = even_offset(new_y)
        new_y = max(0, new_y)
        return f"crop={video_w}:{new_h}:0:{new_y}", ar_name

    if abs(h - video_h) < 16:
        new_w = round_up_mod4(round(video_h * best_match["ratio"]))
        if new_w < w:
            new_w = round_up_mod4(w)
            ar_name = f"Custom AR (Near {best_match['name']})"
        new_w = min(new_w, video_w)
        new_x = x + round((w - new_w) / 2)
        new_x = max(0, min(new_x, video_w - new_w))
        new_x = even_offset(new_x)
        new_x = max(0, new_x)
        return f"crop={new_w}:{video_h}:{new_x}:0", ar_name

    return f"crop={w}:{h}:{x}:{y}", None


def calculate_bounding_box(rects):
    """Union of crop rectangles. Outer box never cuts into any member."""
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


def cluster_samples(samples):
    """Group similar crops by position and size so 2.39 and 4:3 stay apart.

    Uses the cluster's modal `center` as its representative crop rather than the
    max outlier across the tolerance bucket, so 1 noisy frame cannot poison snapping.
    """
    remaining = list(samples)
    clusters = []
    while remaining:
        counts = Counter(s["crop"] for s in remaining)
        center_str, _ = counts.most_common(1)[0]
        center = parse_crop_string(center_str)
        members = []
        rest = []
        for sample in remaining:
            parsed = parse_crop_string(sample["crop"])
            if center and parsed and crops_similar(parsed, center):
                members.append(sample)
            else:
                rest.append(sample)
        remaining = rest
        members.sort(key=lambda s: s["ts"])
        timestamps = [s["ts"] for s in members]
        clusters.append({
            "center": center_str,
            "members": members,
            "timestamps": timestamps,
            "count": len(members),
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def max_contiguous_run(body_members, max_gap):
    """Longest continuous duration (seconds) of a cluster in the body."""
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


def classify_clusters(
    clusters,
    duration,
    edge_frac,
    significant_crop_threshold,
    min_insert,
    seek_interval=None,
):
    """Split clusters into real body ARs, opening/credits, and brief noise."""
    total = sum(c["count"] for c in clusters) or 1
    needed_hits = min_body_hits(seek_interval, min_insert) if seek_interval else 1
    max_gap = max(MAX_KEYFRAME_SPAN, min_insert * 1.5)
    if seek_interval:
        max_gap = max(max_gap, seek_interval * 2.5)

    body_clusters = []
    edge_only = []
    noise = []
    significant = []

    for cluster in clusters:
        body_members = [
            m for m in cluster["members"]
            if not is_edge_timestamp(m["ts"], duration, edge_frac)
        ]
        body_ts = [m["ts"] for m in body_members]
        body_dur = sum(m.get("dt", 0.0) for m in body_members)
        longest_run = max_contiguous_run(body_members, max_gap)

        cluster["body_members"] = body_members
        cluster["body_timestamps"] = body_ts
        cluster["body_duration"] = body_dur
        cluster["max_run_duration"] = longest_run
        cluster["percentage"] = (cluster["count"] / total) * 100
        cluster["ar_label"] = None

        if cluster["percentage"] >= significant_crop_threshold:
            significant.append(cluster)

        if not body_ts and cluster["timestamps"]:
            edge_only.append(cluster)
            continue

        if not body_ts:
            continue

        if seek_interval is not None:
            sustained = len(body_ts) >= needed_hits and body_dur >= min_insert - 1e-6
        else:
            # Keyframe mode: either a single/multi-shot contiguous scene lasting
            # >= min_insert, or >= 2 body keyframes covering >= min_insert total.
            sustained = (longest_run >= min_insert - 1e-6) or (
                len(body_ts) >= 2 and body_dur >= min_insert - 1e-6
            )

        if sustained:
            body_clusters.append(cluster)
        else:
            noise.append(cluster)

    return body_clusters, edge_only, noise, significant, needed_hits


def verify_expanding_clusters(
    body_clusters,
    noise,
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
    """Drop false expansions (flakes/specks) and absorb minor dark-edge tightenings.

    * Expanding clusters (not contained in the majority crop) are verified with
      bar-strip luma against the majority baseline. A speck leaves bar YAVG near
      baseline; a real 4:3 / IMAX insert raises bar YAVG by >= bar_delta.
    * Minor clusters strictly *contained* inside the majority crop (dark edges
      during night scenes) cannot shrink the outer box and are not mixed ARs.
    """
    if len(body_clusters) <= 1:
        return body_clusters, noise, None, None

    majority = max(body_clusters, key=lambda c: c["count"])
    maj = parse_crop_string(majority["center"])
    if not maj:
        return body_clusters, noise, None, None

    expanding = []
    kept = [majority]
    for cluster in body_clusters:
        if cluster is majority:
            continue
        cand = parse_crop_string(cluster["center"])
        if not cand:
            continue
        if not crop_contained_in(cand, maj, margin=4):
            expanding.append(cluster)
        elif cluster["percentage"] >= significant_crop_threshold:
            # Large genuinely narrower/shorter ratio (e.g. 40% 2.39 in a 16:9 movie).
            kept.append(cluster)
        elif debug:
            print(
                f"    - Ignored minor tighter cluster {cluster['center']} "
                f"({cluster['percentage']:.1f}%, contained in majority {majority['center']})"
            )

    if not expanding:
        return kept, noise, None, None

    all_maj_bars = expanding_bar_regions(maj, None, video_w, video_h)
    if not all_maj_bars:
        return kept, noise, None, None

    base_times = pick_evenly(majority.get("body_timestamps") or majority["timestamps"], 8)
    print("Luma check: measuring letterbox/pillarbox strips of the majority crop...")
    baseline_hits = run_bar_luma(
        input_file, video_stream_idx, base_times, all_maj_bars,
        num_workers, "Bar luma (majority baseline)", accurate=False,
    )
    baseline = median([h["bar_luma"] for h in baseline_hits])
    if baseline is None:
        baseline = 0.0
    threshold = max(8.0, baseline + bar_delta)
    if debug:
        print(
            f"    - Majority bar YAVG baseline={baseline:.2f}, "
            f"picture threshold={threshold:.2f} (baseline + {bar_delta})"
        )

    still_body = list(kept)
    for cluster in expanding:
        cand = parse_crop_string(cluster["center"])
        bars = expanding_bar_regions(maj, cand, video_w, video_h) or all_maj_bars
        raw_times = cluster.get("body_timestamps") or cluster["timestamps"]
        check_times = pick_evenly(raw_times, MAX_BAR_LUMA_SAMPLES)

        # If a single long-GOP keyframe triggered this candidate, also probe
        # mid-scene timestamps accurately to confirm it isn't a 1-frame flash.
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

        hits = run_bar_luma(
            input_file, video_stream_idx, check_times, bars, num_workers,
            f"Bar luma (candidate {cluster['center']})",
            accurate=single_hit_probe,
        )
        valid_hits = [h for h in hits if h.get("bar_luma") is not None]
        picture = [h for h in valid_hits if h["bar_luma"] >= threshold]
        cand_med = median([h["bar_luma"] for h in valid_hits])
        cluster["bar_luma_median"] = cand_med
        cluster["bar_picture_hits"] = len(picture)
        needed_picture = max(2, math.ceil(len(check_times) * 0.5)) if len(check_times) >= 2 else 1

        if len(picture) >= needed_picture:
            still_body.append(cluster)
            med_txt = f"{cand_med:.1f}" if cand_med is not None else "?"
            print(
                f"    - {cluster['center']}: bar YAVG {med_txt} "
                f"vs baseline {baseline:.2f} → picture in the bars (artistic insert)"
            )
        else:
            cluster["ignore_reason"] = "bar_luma"
            noise.append(cluster)
            med_txt = f"{cand_med:.1f}" if cand_med is not None else "?"
            print(
                f"    - {cluster['center']}: bar YAVG {med_txt} "
                f"vs baseline {baseline:.2f} → still black (flake/speck, not a new AR)"
            )
    return still_body, noise, baseline, threshold


def label_ar(cluster, video_w, video_h):
    parsed = parse_crop_string(cluster["center"])
    if not parsed:
        cluster["ar_label"] = None
        return
    _, ar_label = snap_to_known_ar(
        parsed["w"], parsed["h"], parsed["x"], parsed["y"], video_w, video_h
    )
    cluster["ar_label"] = ar_label


def recommend_from_clusters(body_clusters, video_w, video_h, min_crop):
    if not body_clusters:
        return None, None, "none"
    rects = []
    for cluster in body_clusters:
        parsed = parse_crop_string(cluster["center"])
        if parsed:
            rects.append(parsed)
    raw = calculate_bounding_box(rects)
    if not raw:
        return None, None, "none"
    parsed = parse_crop_string(raw)
    snapped, ar_label = snap_to_known_ar(
        parsed["w"], parsed["h"], parsed["x"], parsed["y"], video_w, video_h
    )
    if is_full_frame(snapped, video_w, video_h):
        return snapped, ar_label, "full"
    if not is_major_crop(snapped, video_w, video_h, min_crop):
        return snapped, ar_label, "tiny"
    kind = "single" if len(body_clusters) == 1 else "mixed"
    return snapped, ar_label, kind


def print_cluster_lines(clusters, significant, video_w, video_h, note_mixed=False):
    for cluster in clusters:
        label_ar(cluster, video_w, video_h)
        label = f"'{cluster['ar_label']}'" if cluster["ar_label"] else "Custom AR"
        ts = cluster["timestamps"]
        span = f"{format_timestamp(ts[0])}–{format_timestamp(ts[-1])}" if ts else "?"
        body_n = len(cluster.get("body_timestamps") or [])
        if note_mixed:
            tag = "Primary" if cluster in significant else "Minority / artistic AR"
        else:
            tag = "body"
        print(
            f"  - {label} ({cluster['center']}) in {cluster['percentage']:.1f}% of samples, "
            f"{span} ({body_n} body hits, {tag})"
        )


def resolve_scan_mode(scan_mode, interval, duration, codec_name, file_size):
    """Choose between fast keyframe scanning and fast keyframe seeking."""
    if interval is not None:
        return "seek", float(interval), "explicit --interval"
    if scan_mode == "keyframes":
        return "keyframes", None, "explicit --scan keyframes"
    if scan_mode == "seek":
        eff = round(max(2.0, min(10.0, duration / TARGET_AUTO_SEEKS)), 2)
        return "seek", eff, "explicit --scan seek"

    # Auto mode
    if codec_name and codec_name.lower() in INTRA_ONLY_CODECS:
        eff = round(max(2.0, min(10.0, duration / TARGET_AUTO_SEEKS)), 2)
        return "seek", eff, f"intra-only codec ({codec_name})"
    if file_size > AUTO_KEYFRAME_MAX_BYTES:
        eff = round(max(2.0, min(10.0, duration / TARGET_AUTO_SEEKS)), 2)
        size_gb = file_size / (1024 ** 3)
        return "seek", eff, f"large file ({size_gb:.1f} GiB > 20 GiB)"
    return "keyframes", None, "auto (keyframe scan)"


def analyze_video(
    input_file,
    duration,
    width,
    height,
    video_stream_idx,
    num_workers,
    significant_crop_threshold,
    min_crop,
    scan_mode,
    effective_interval,
    edge_frac,
    luma_min,
    min_insert,
    bar_delta,
    verify,
    debug=False,
):
    print(f"\n--- Analyzing Video: {os.path.basename(input_file)} ---")

    if scan_mode == "keyframes":
        samples = run_keyframe_scan(
            input_file, duration, video_stream_idx, num_workers, "Keyframe scan"
        )
        # Fallback if a strange container/stream yielded almost no keyframes.
        if len(samples) < 5 and duration > 10:
            fallback_interval = round(max(2.0, min(10.0, duration / TARGET_AUTO_SEEKS)), 2)
            print(
                f"Keyframe scan returned only {len(samples)} sample(s); "
                f"falling back to seek mode ({fallback_interval}s interval)..."
            )
            scan_mode = "seek"
            effective_interval = fallback_interval
            times = sample_times(duration, effective_interval, offset=0.0)
            samples = run_seek_samples(
                input_file, times, video_stream_idx, num_workers, "Pass 1 (grid)"
            )
    else:
        times = sample_times(duration, effective_interval, offset=0.0)
        samples = run_seek_samples(
            input_file, times, video_stream_idx, num_workers, "Pass 1 (grid)"
        )

    attach_sample_durations(
        samples, duration, fixed_interval=effective_interval if scan_mode == "seek" else None
    )

    def usable(sample):
        if not sample.get("crop"):
            return False
        luma = sample.get("luma")
        if luma is None:
            return True
        return luma >= luma_min

    valid = [s for s in samples if usable(s)]
    dropped_dark = sum(
        1 for s in samples
        if s.get("crop") and s.get("luma") is not None and s["luma"] < luma_min
    )
    if dropped_dark:
        print(
            f"Skipped {dropped_dark} too-dark sample(s) "
            f"(full-frame YAVG < {luma_min}; cropdetect would false-tighten)."
        )
    if debug:
        print(
            f"\n--- Debug: {len(samples)} samples, {len(valid)} usable, "
            f"{dropped_dark} dropped as too dark ---"
        )

    if not valid:
        print(f"\n{COLOR_GREEN}Analysis complete. No black bars detected.{COLOR_RESET}")
        return

    def decide(valid_samples, cur_seek_interval):
        clusters = cluster_samples(valid_samples)
        if debug:
            print("\n--- Debug: Clusters ---")
            for cluster in clusters[:15]:
                print(
                    f"  - {cluster['center']}  n={cluster['count']}  "
                    f"ts={len(cluster['timestamps'])}"
                )
        body, edge_only, noise, significant, needed = classify_clusters(
            clusters,
            duration,
            edge_frac,
            significant_crop_threshold,
            min_insert,
            seek_interval=cur_seek_interval,
        )
        body, noise, bar_baseline, bar_threshold = verify_expanding_clusters(
            body,
            noise,
            input_file,
            video_stream_idx,
            width,
            height,
            duration,
            num_workers,
            bar_delta,
            min_insert,
            significant_crop_threshold,
            debug=debug,
        )
        for cluster in body + edge_only + noise:
            label_ar(cluster, width, height)
        snapped, ar_label, kind = recommend_from_clusters(body, width, height, min_crop)
        return {
            "clusters": clusters,
            "body": body,
            "edge_only": edge_only,
            "noise": noise,
            "significant": significant,
            "snapped": snapped,
            "ar_label": ar_label,
            "kind": kind,
            "needed_hits": needed,
            "bar_baseline": bar_baseline,
            "bar_threshold": bar_threshold,
        }

    cur_interval = effective_interval if scan_mode == "seek" else None
    decision = decide(valid, cur_interval)

    # Gap-fill pass only applies in seek mode when the grid spacing is wider
    # than half of --min-insert and a crop would actually be applied.
    if (
        scan_mode == "seek"
        and verify
        and decision["kind"] in ("single", "mixed")
        and effective_interval is not None
        and effective_interval > max(1.0, min_insert / 2.0)
    ):
        already = {round(s["ts"], 3) for s in samples}
        gap_times = [
            t for t in sample_times(duration, effective_interval, offset=effective_interval / 2.0)
            if round(t, 3) not in already
        ]
        if gap_times:
            print(
                "Pass 2 (gap fill): looking for short aspect-ratio inserts "
                "between Pass 1 samples..."
            )
            extra = run_seek_samples(
                input_file, gap_times, video_stream_idx, num_workers, "Pass 2 (gaps)"
            )
            extra_valid = [s for s in extra if usable(s)]
            if extra_valid:
                before = decision["snapped"]
                valid = attach_sample_durations(
                    valid + extra_valid, duration, fixed_interval=effective_interval / 2.0
                )
                decision = decide(valid, effective_interval / 2.0)
                if debug and before != decision["snapped"]:
                    print(f"    - Crop changed after gap fill: {before} → {decision['snapped']}")

    print("\n--- Determining Final Crop Recommendation ---")

    if decision["edge_only"]:
        print("Ignored opening logos / end credits (first/last edge of the file):")
        for cluster in decision["edge_only"][:10]:
            label_ar(cluster, width, height)
            label = f"'{cluster['ar_label']}'" if cluster["ar_label"] else "Custom AR"
            ts = cluster["timestamps"]
            span = f"{format_timestamp(ts[0])}–{format_timestamp(ts[-1])}" if ts else "?"
            print(f"  - {label} ({cluster['center']}) only at {span}")
        if len(decision["edge_only"]) > 10:
            print(f"  - ... and {len(decision['edge_only']) - 10} more edge cluster(s)")
        print(
            "  (Scrolling credits are mostly black with a small text column; "
            "using them would crop too tightly. Logos are often a different AR.)"
        )

    noise_short = [c for c in decision["noise"] if c.get("ignore_reason") != "bar_luma"]
    noise_flake = [c for c in decision["noise"] if c.get("ignore_reason") == "bar_luma"]
    if noise_short:
        print(
            f"Ignored brief detections (under --min-insert {min_insert}s; "
            f"will not block a majority crop):"
        )
        for cluster in noise_short[:10]:
            label_ar(cluster, width, height)
            label = f"'{cluster['ar_label']}'" if cluster["ar_label"] else "Custom AR"
            ts = cluster.get("body_timestamps") or cluster["timestamps"]
            span = f"{format_timestamp(ts[0])}–{format_timestamp(ts[-1])}" if ts else "?"
            n = len(cluster.get("body_timestamps") or [])
            print(f"  - {label} ({cluster['center']}) at {span} ({n} body hits)")
        if len(noise_short) > 10:
            print(f"  - ... and {len(noise_short) - 10} more brief cluster(s)")
    if noise_flake:
        print(
            "Ignored letterbox flakes (cropdetect grew the frame, but bar-strip luma "
            "did not rise vs the majority crop — a speck, not picture):"
        )
        for cluster in noise_flake:
            label_ar(cluster, width, height)
            label = f"'{cluster['ar_label']}'" if cluster["ar_label"] else "Custom AR"
            ts = cluster.get("body_timestamps") or cluster["timestamps"]
            span = f"{format_timestamp(ts[0])}–{format_timestamp(ts[-1])}" if ts else "?"
            yavg = cluster.get("bar_luma_median")
            ytxt = f"{yavg:.1f}" if yavg is not None else "?"
            print(f"  - {label} ({cluster['center']}) at {span} (bar YAVG {ytxt})")

    kind = decision["kind"]
    snapped = decision["snapped"]
    ar_label = decision["ar_label"]
    body = decision["body"]

    if kind == "none":
        print(f"{COLOR_RED}No safe crop clusters in the body of the video.{COLOR_RESET}")
        print("Recommendation: Do not crop.")
        return

    if kind == "full":
        if len(body) > 1:
            print(f"{COLOR_YELLOW}Mixed aspect ratios in the body of the video.{COLOR_RESET}")
            print(
                "The outer box around every in-body ratio is the full frame, "
                "so a single crop would cut an artistic insert (4:3 flashback, IMAX, open matte)."
            )
            print("\n--- Detected Aspect Ratios (body) ---")
            print_cluster_lines(body, decision["significant"], width, height, note_mixed=True)
        print(f"\n{COLOR_GREEN}The safe crop matches the source resolution. No crop is needed.{COLOR_RESET}")
        return

    if kind == "tiny":
        print(
            f"{COLOR_GREEN}Detected crop is under the minimum ({min_crop}px per side). "
            f"No crop is needed.{COLOR_RESET}"
        )
        return

    if kind == "single":
        print("A single dominant aspect ratio was found in the body of the video.")
        if ar_label:
            print(f"The detected crop snaps to the '{ar_label}' aspect ratio.")
        print(f"\n{COLOR_GREEN}Recommended crop filter: -vf {snapped}{COLOR_RESET}")
        return

    print(f"{COLOR_YELLOW}Mixed aspect ratios detected in the body of the video.{COLOR_RESET}")
    print(
        "This includes short artistic inserts (4:3 flashbacks, IMAX, open matte), "
        "not just the majority ratio."
    )
    print("Recommending a bounding-box crop that contains every in-body scene.")
    print("\n--- Detected Aspect Ratios (body) ---")
    print_cluster_lines(body, decision["significant"], width, height, note_mixed=True)
    print(f"\n{COLOR_GREEN}Analysis complete.{COLOR_RESET}")
    if ar_label:
        print(f"The calculated master crop snaps to the '{ar_label}' aspect ratio.")
    print(f"{COLOR_GREEN}Recommended safe crop filter: -vf {snapped}{COLOR_RESET}")
    print(
        f"{COLOR_YELLOW}A tight crop to the majority ratio would cut the minority scenes.{COLOR_RESET}"
    )


def probe_video(input_file):
    duration_str = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_file,
        ],
        stderr=subprocess.STDOUT,
        text=True,
    )
    duration = float(duration_str.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid duration: {duration_str!r}")

    probe_output = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v",
            "-show_entries", "stream=index,codec_name,width,height,pix_fmt,disposition",
            "-of", "json",
            input_file,
        ],
        stderr=subprocess.STDOUT,
        text=True,
    )
    streams_data = json.loads(probe_output)
    video_stream = None
    for stream in streams_data.get("streams", []):
        if stream.get("disposition", {}).get("attached_pic", 0) == 0:
            video_stream = stream
            break
    if not video_stream or "width" not in video_stream or "height" not in video_stream:
        raise ValueError("Could not find a valid video stream to probe for resolution.")
    width = int(video_stream["width"])
    height = int(video_stream["height"])
    stream_idx = int(video_stream.get("index", 0))
    codec_name = video_stream.get("codec_name", "")
    pix_fmt = video_stream.get("pix_fmt", "unknown")
    file_size = os.path.getsize(input_file)
    return duration, width, height, stream_idx, codec_name, pix_fmt, file_size


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Analyze a video for black bars and recommend a crop.\n"
            "Ignores opening logos and scrolling end credits (edge of the file).\n"
            "Skips too-dark frames (cropdetect would false-tighten).\n"
            "Ignores letterbox flakes: cropdetect may grow the frame, but bar-strip luma does not.\n"
            "Keeps artistic AR changes that last several seconds (4:3 flashbacks, "
            "IMAX, open matte) and uses the outer bounding box so those scenes are not cut."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("input", help="Input video file")
    parser.add_argument(
        "-n", "--num_workers",
        type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Worker threads. Default: half of available cores.",
    )
    parser.add_argument(
        "--scan",
        choices=("auto", "keyframes", "seek"),
        default="auto",
        help=(
            "Sampling strategy (default: auto). "
            "'keyframes' scans every keyframe in parallel chunks (fast & thorough); "
            "'seek' uses fast keyframe-seeking at --interval; "
            "'auto' uses keyframes for files <= 20 GiB and seek for larger or intra-only files."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Seconds between seek samples. Setting this switches to seek mode.",
    )
    parser.add_argument(
        "-sct", "--significant_crop_threshold",
        type=float,
        default=5.0,
        help="Percentage for the Primary label in mixed-AR reports (default: 5.0). Not the dirt filter.",
    )
    parser.add_argument(
        "--min-insert",
        type=float,
        default=DEFAULT_MIN_INSERT,
        metavar="SECONDS",
        help=(
            f"Seconds a different AR must occupy in the body before it vetoes the "
            f"majority crop (default: {DEFAULT_MIN_INSERT}). Dirt/flakes are 1–2 frames; "
            f"a 4:3 flashback is several seconds."
        ),
    )
    parser.add_argument(
        "-mc", "--min_crop",
        type=int,
        default=10,
        help="Do not recommend a crop unless at least one side is this many pixels (default: 10).",
    )
    parser.add_argument(
        "--edge",
        type=float,
        default=DEFAULT_EDGE_FRAC,
        metavar="FRAC",
        help=(
            f"Ignore clusters that exist only in the first/last this fraction of the file "
            f"(default: {DEFAULT_EDGE_FRAC} = opening logos and scrolling credits)."
        ),
    )
    parser.add_argument(
        "--luma-min",
        type=float,
        default=DEFAULT_LUMA_MIN,
        help=(
            f"Skip frames with normalized 8-bit full-frame YAVG below this (default: {DEFAULT_LUMA_MIN}). "
            f"Too-dark scenes make cropdetect report extra bars."
        ),
    )
    parser.add_argument(
        "--bar-delta",
        type=float,
        default=DEFAULT_BAR_DELTA,
        help=(
            f"Bar-strip YAVG (0–255) must exceed the majority-letterbox baseline by this much "
            f"to count as picture (default: {DEFAULT_BAR_DELTA}). Flakes stay near baseline."
        ),
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the gap-fill pass when running in seek mode.",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose cluster/sample logging.")
    args = parser.parse_args()

    if args.interval is not None and args.interval <= 0:
        print(f"{COLOR_RED}Error: --interval must be > 0.{COLOR_RESET}")
        sys.exit(1)
    if args.min_insert <= 0:
        print(f"{COLOR_RED}Error: --min-insert must be > 0.{COLOR_RESET}")
        sys.exit(1)
    if not os.path.isfile(args.input):
        print(f"{COLOR_RED}Error: Input file does not exist.{COLOR_RESET}")
        sys.exit(1)

    print("--- Probing video file for metadata ---")
    try:
        duration, width, height, stream_idx, codec_name, pix_fmt, file_size = probe_video(args.input)
        print(f"Detected duration: {duration:.1f}s ({format_timestamp(duration)})")
        print(f"Detected resolution: {width}x{height} ({codec_name}, {pix_fmt}, stream #{stream_idx})")
    except Exception as e:
        print(f"{COLOR_RED}Error probing video file: {e}{COLOR_RESET}")
        sys.exit(1)

    scan_mode, effective_interval, scan_reason = resolve_scan_mode(
        args.scan, args.interval, duration, codec_name, file_size
    )

    print("\n--- Video Analysis Parameters ---")
    print(f"Input File: {os.path.basename(args.input)}")
    print(f"Duration: {duration:.1f}s")
    print(f"Resolution: {width}x{height} ({pix_fmt})")
    print(f"Number of Workers: {args.num_workers}")
    if scan_mode == "keyframes":
        print(f"Scan mode: keyframes ({scan_reason})")
    else:
        print(f"Scan mode: seek every {effective_interval}s ({scan_reason})")
        print(f"Gap-fill verify: {'off' if args.no_verify else 'on'}")
    print(f"Edge ignore: first/last {args.edge * 100:.1f}% (logos / scrolling credits)")
    print(f"Min artistic insert: {args.min_insert}s — shorter is dirt/noise")
    print(f"Full-frame luma min: {args.luma_min} (0–255 gray; darker = skip)")
    print(f"Bar-luma delta: {args.bar_delta} above majority letterbox (flake vs real insert)")
    print(f"Significance label threshold: {args.significant_crop_threshold}% (report only)")
    print(f"Minimum Crop Size: {args.min_crop}px")

    check_prerequisites()
    analyze_video(
        args.input,
        duration,
        width,
        height,
        stream_idx,
        args.num_workers,
        args.significant_crop_threshold,
        args.min_crop,
        scan_mode,
        effective_interval,
        args.edge,
        args.luma_min,
        args.min_insert,
        args.bar_delta,
        verify=not args.no_verify,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
