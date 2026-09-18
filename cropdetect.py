#!/usr/bin/env python3

"""Recommend a safe FFmpeg crop, including short artistic aspect-ratio inserts.

Three different "odd frames" problems are handled separately:

* Opening logos / end credits (first and last 5% by default) are ignored.
  Scrolling credits are a sea of black with a small text column, so cropdetect
  reports a much *tighter* window and would cut the movie. Logos are often a
  different AR. Neither should vote.
* Dirt / reel flakes: a bright speck in the letterbox makes cropdetect report a
  *larger* picture. A luma check on those bar strips still sees almost-black
  (one speck does not move YAVG), so the expansion is ignored. Brief hits are
  also ignored by --min-insert.
* Too-dark scenes: cropdetect treats dark picture as extra bars and reports a
  *tighter* crop. Frames with low full-frame YAVG are skipped so they cannot
  vote.
* Artistic AR changes (Fallout 4:3 flashback, IMAX, open matte): these last
  *seconds*, and the would-be bars are full of picture (bar YAVG jumps vs the
  majority letterbox). They are kept; the recommended crop is the outer box.
"""

import argparse
import json
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_RESET = "\033[0m"

# Cheap seeks, not 1-second windows. A ~10s 4:3 flashback at 2s interval is
# several hits; a 30s interval can miss it entirely.
DEFAULT_INTERVAL = 2.0
FRAMES_PER_SAMPLE = 2
CROPDETECT_LIMIT = 24
CROPDETECT_FILTER = f"cropdetect={CROPDETECT_LIMIT}:4:0"
# Full-frame YAVG. Below this, cropdetect sees dark picture as bars (false tight crop).
# A dim-but-visible flashback is usually well above this.
DEFAULT_LUMA_MIN = 16.0
# Bar-strip YAVG must beat the majority-letterbox baseline by this much to count
# as real picture. A flake leaves bar YAVG almost unchanged.
DEFAULT_BAR_DELTA = 12.0
# Opening logos and scrolling end credits. Not used for artistic inserts in the body.
DEFAULT_EDGE_FRAC = 0.05
# A different AR must be seen for this long in the body before it can veto the
# majority crop. Dirt/flakes are 1–2 frames; a Fallout flashback is seconds.
DEFAULT_MIN_INSERT = 6.0
XY_TOL = 8
SIZE_TOL = 16
AR_TOLERANCE = 0.03

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


def crop_contained_in(inner, outer):
    """True if inner's picture lies fully inside outer."""
    return (
        inner["x"] >= outer["x"]
        and inner["y"] >= outer["y"]
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"]
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"]
    )


def median(values):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def bar_region_crops(majority, video_w, video_h, min_side=2):
    """Pixel rectangles the majority crop would discard (letterbox / pillarbox)."""
    bars = []
    top = majority["y"]
    bottom = video_h - (majority["y"] + majority["h"])
    left = majority["x"]
    right = video_w - (majority["x"] + majority["w"])
    if top >= min_side:
        bars.append((video_w, top, 0, 0))
    if bottom >= min_side:
        bars.append((video_w, bottom, 0, majority["y"] + majority["h"]))
    if left >= min_side:
        bars.append((left, video_h, 0, 0))
    if right >= min_side:
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
    """How many body samples a minority AR needs before it counts as real.

    interval=2s and min_insert=6s → 3 hits. A single dirty frame is 1 hit.
    """
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


def analyze_segment(task_args):
    """One seek: a few frames of cropdetect + luma. Returns one sample dict."""
    seek_time, input_file = task_args
    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-ss", str(seek_time),
        "-i", input_file,
        "-frames:v", str(FRAMES_PER_SAMPLE),
        "-an", "-sn", "-dn",
        "-vf", f"{CROPDETECT_FILTER},signalstats",
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

    crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr)
    luma = None
    luma_matches = re.findall(r"YAVG:([0-9.]+)", result.stderr)
    if luma_matches:
        try:
            luma = float(luma_matches[-1])
        except ValueError:
            luma = None

    crop = None
    if crops:
        w, h, x, y = map(int, crops[-1])
        crop = f"crop={w}:{h}:{x}:{y}"
    return {"ts": seek_time, "crop": crop, "luma": luma}


def run_samples(input_file, times, num_workers, label):
    if not times:
        return []
    tasks = [(t, input_file) for t in times]
    results = []
    workers = max(1, min(num_workers, len(tasks)))
    print(f"{label}: {len(tasks)} seeks across {workers} worker(s)...")
    with multiprocessing.Pool(processes=workers) as pool:
        total = len(tasks)
        for i, sample in enumerate(pool.imap_unordered(analyze_segment, tasks), 1):
            results.append(sample)
            sys.stdout.write(f"\r{label}: {i}/{total} completed...")
            sys.stdout.flush()
    print()
    return results


def analyze_bar_luma(task_args):
    """YAVG of the strips a majority crop would throw away.

    A reel flake is a few bright pixels in a black bar: YAVG barely moves.
    A 4:3 / IMAX insert fills those strips with picture: YAVG jumps.
    """
    seek_time, input_file, crops = task_args
    if not crops:
        return {"ts": seek_time, "bar_luma": None, "bar_ymax": None}
    n = len(crops)
    if n == 1:
        w, h, x, y = crops[0]
        filter_complex = f"[0:v]crop={w}:{h}:{x}:{y},signalstats[o0]"
        maps = ["-map", "[o0]"]
    else:
        labels = "".join(f"[s{i}]" for i in range(n))
        parts = [f"[0:v]split={n}{labels}"]
        maps = []
        for i, (w, h, x, y) in enumerate(crops):
            parts.append(f"[s{i}]crop={w}:{h}:{x}:{y},signalstats[o{i}]")
            maps.extend(["-map", f"[o{i}]"])
        filter_complex = ";".join(parts)
    ffmpeg_args = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-ss", str(seek_time),
        "-i", input_file,
        "-frames:v", "1",
        "-an", "-sn", "-dn",
        "-filter_complex", filter_complex,
        *maps,
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
        return {"ts": seek_time, "bar_luma": None, "bar_ymax": None}
    yavgs = []
    for raw in re.findall(r"YAVG:([0-9.]+)", result.stderr):
        try:
            yavgs.append(float(raw))
        except ValueError:
            pass
    ymaxs = []
    for raw in re.findall(r"YMAX:([0-9.]+)", result.stderr):
        try:
            ymaxs.append(float(raw))
        except ValueError:
            pass
    weights = [max(1, w * h) for w, h, x, y in crops]
    bar_luma = None
    if yavgs:
        n_use = min(len(yavgs), len(weights))
        total_w = sum(weights[:n_use])
        if total_w:
            bar_luma = sum(yavgs[i] * weights[i] for i in range(n_use)) / total_w
    bar_ymax = max(ymaxs) if ymaxs else None
    return {"ts": seek_time, "bar_luma": bar_luma, "bar_ymax": bar_ymax}


def run_bar_luma(input_file, times, crops, num_workers, label):
    if not times or not crops:
        return []
    tasks = [(t, input_file, crops) for t in times]
    results = []
    workers = max(1, min(num_workers, len(tasks)))
    print(f"{label}: {len(tasks)} bar-luma seeks across {workers} worker(s)...")
    with multiprocessing.Pool(processes=workers) as pool:
        total = len(tasks)
        for i, sample in enumerate(pool.imap_unordered(analyze_bar_luma, tasks), 1):
            results.append(sample)
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
    """Group similar crops. Uses width/height as well as top-left so 2.39 and 4:3 stay apart."""
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
        timestamps = sorted({s["ts"] for s in members})
        member_rects = [parse_crop_string(s["crop"]) for s in members]
        outer = calculate_bounding_box(member_rects) or center_str
        clusters.append({
            "center": center_str,
            "outer": outer,
            "members": members,
            "timestamps": timestamps,
            "count": len(members),
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def classify_clusters(clusters, duration, edge_frac, significant_crop_threshold, interval, min_insert):
    """Split clusters into: real body ARs, opening/credits, and brief noise.

    Real body ARs need enough in-body samples to cover --min-insert seconds.
    That is the majority/noise filter: 1–2 dirty frames cannot veto a crop.
    A 6–10s 4:3 flashback still qualifies. Logos/credits (edge-only) are
    ignored even if they last a long time.
    """
    needed = min_body_hits(interval, min_insert)
    total = sum(c["count"] for c in clusters) or 1
    body_clusters = []
    edge_only = []
    noise = []
    significant = []
    for cluster in clusters:
        body_ts = [t for t in cluster["timestamps"] if not is_edge_timestamp(t, duration, edge_frac)]
        cluster["body_timestamps"] = body_ts
        cluster["percentage"] = (cluster["count"] / total) * 100
        cluster["ar_label"] = None
        if cluster["percentage"] >= significant_crop_threshold:
            significant.append(cluster)
        if not body_ts and cluster["timestamps"]:
            edge_only.append(cluster)
        elif len(body_ts) >= needed:
            body_clusters.append(cluster)
        elif body_ts:
            noise.append(cluster)
    return body_clusters, edge_only, noise, significant


def verify_expanding_clusters(
    body_clusters,
    noise,
    input_file,
    video_w,
    video_h,
    num_workers,
    bar_delta,
    debug=False,
):
    """Drop expansions whose extra pixels are still black (flake, not picture).

    Majority letterbox YAVG is the baseline (previous/later 'normal' frames).
    A dirt speck does not move bar YAVG. A flashback fills the bars with picture.
    """
    if len(body_clusters) <= 1:
        return body_clusters, noise, None, None

    majority = max(body_clusters, key=lambda c: c["count"])
    maj = parse_crop_string(majority["outer"])
    if not maj:
        return body_clusters, noise, None, None
    bars = bar_region_crops(maj, video_w, video_h)
    if not bars:
        return body_clusters, noise, None, None

    expanding = []
    kept = []
    for cluster in body_clusters:
        if cluster is majority:
            kept.append(cluster)
            continue
        cand = parse_crop_string(cluster["outer"])
        if cand and not crop_contained_in(cand, maj):
            expanding.append(cluster)
        else:
            kept.append(cluster)
    if not expanding:
        return body_clusters, noise, None, None

    base_times = majority.get("body_timestamps") or majority["timestamps"]
    step = max(1, len(base_times) // 8)
    baseline_times = base_times[::step][:8]
    print("Luma check: measuring letterbox/pillarbox strips of the majority crop...")
    baseline_hits = run_bar_luma(
        input_file, baseline_times, bars, num_workers, "Bar luma (majority baseline)"
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
        times = cluster.get("body_timestamps") or cluster["timestamps"]
        hits = run_bar_luma(
            input_file, times, bars, num_workers,
            f"Bar luma (candidate {cluster['outer']})",
        )
        picture = [h for h in hits if h.get("bar_luma") is not None and h["bar_luma"] >= threshold]
        cand_med = median([h["bar_luma"] for h in hits])
        cluster["bar_luma_median"] = cand_med
        cluster["bar_picture_hits"] = len(picture)
        needed_picture = max(2, math.ceil(len(times) * 0.5)) if times else 1
        if len(picture) >= needed_picture:
            still_body.append(cluster)
            print(
                f"    - {cluster['outer']}: bar YAVG {cand_med if cand_med is not None else '?'} "
                f"vs baseline {baseline:.2f} → picture in the bars (artistic insert)"
            )
        else:
            cluster["ignore_reason"] = "bar_luma"
            noise.append(cluster)
            print(
                f"    - {cluster['outer']}: bar YAVG {cand_med if cand_med is not None else '?'} "
                f"vs baseline {baseline:.2f} → still black (flake/speck, not a new AR)"
            )
    return still_body, noise, baseline, threshold


def label_ar(cluster, video_w, video_h):
    parsed = parse_crop_string(cluster["outer"])
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
        for member in cluster["members"]:
            parsed = parse_crop_string(member["crop"])
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
            if cluster in significant:
                tag = "Primary"
            else:
                tag = "Minority / artistic AR"
        else:
            tag = "body"
        print(
            f"  - {label} ({cluster['outer']}) in {cluster['percentage']:.1f}% of samples, "
            f"{span} ({body_n} body hits, {tag})"
        )


def analyze_video(
    input_file,
    duration,
    width,
    height,
    num_workers,
    significant_crop_threshold,
    min_crop,
    interval,
    edge_frac,
    luma_min,
    min_insert,
    bar_delta,
    verify,
    debug=False,
):
    print(f"\n--- Analyzing Video: {os.path.basename(input_file)} ---")
    times = sample_times(duration, interval, offset=0.0)
    samples = run_samples(input_file, times, num_workers, "Pass 1 (grid)")

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
        print(f"\n--- Debug: {len(samples)} samples, {len(valid)} usable, {dropped_dark} dropped as too dark ---")

    if not valid:
        print(f"\n{COLOR_GREEN}Analysis complete. No black bars detected.{COLOR_RESET}")
        return

    def decide(valid_samples, extra_note=None):
        clusters = cluster_samples(valid_samples)
        if debug:
            print("\n--- Debug: Clusters ---")
            for cluster in clusters:
                print(
                    f"  - {cluster['center']}  n={cluster['count']}  "
                    f"ts={len(cluster['timestamps'])}  outer={cluster['outer']}"
                )
        body, edge_only, noise, significant = classify_clusters(
            clusters, duration, edge_frac, significant_crop_threshold, interval, min_insert
        )
        body, noise, bar_baseline, bar_threshold = verify_expanding_clusters(
            body, noise, input_file, width, height, num_workers, bar_delta, debug=debug
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
            "extra_note": extra_note,
            "needed_hits": min_body_hits(interval, min_insert),
            "bar_baseline": bar_baseline,
            "bar_threshold": bar_threshold,
        }

    decision = decide(valid)

    # If we would actually crop, fill the gaps between grid points so a short
    # 4:3 / IMAX / open-matte insert cannot hide between two 2.39 samples.
    if verify and decision["kind"] in ("single", "mixed") and interval > 0.5:
        already = {round(s["ts"], 3) for s in samples}
        gap_times = [
            t for t in sample_times(duration, interval, offset=interval / 2.0)
            if round(t, 3) not in already
        ]
        if gap_times:
            print(
                "Pass 2 (gap fill): looking for short aspect-ratio inserts "
                "between Pass 1 samples..."
            )
            extra = run_samples(input_file, gap_times, num_workers, "Pass 2 (gaps)")
            extra_valid = [s for s in extra if usable(s)]
            if extra_valid:
                before = decision["snapped"]
                valid = valid + extra_valid
                decision = decide(valid, extra_note="after gap-fill verification")
                if debug and before != decision["snapped"]:
                    print(f"    - Crop changed after gap fill: {before} → {decision['snapped']}")

    print("\n--- Determining Final Crop Recommendation ---")

    if decision["edge_only"]:
        print("Ignored opening logos / end credits (first/last edge of the file):")
        for cluster in decision["edge_only"]:
            label_ar(cluster, width, height)
            label = f"'{cluster['ar_label']}'" if cluster["ar_label"] else "Custom AR"
            ts = cluster["timestamps"]
            span = f"{format_timestamp(ts[0])}–{format_timestamp(ts[-1])}" if ts else "?"
            print(f"  - {label} ({cluster['outer']}) only at {span}")
        print(
            "  (Scrolling credits are mostly black with a small text column; "
            "using them would crop too tightly. Logos are often a different AR.)"
        )

    noise_short = [c for c in decision["noise"] if c.get("ignore_reason") != "bar_luma"]
    noise_flake = [c for c in decision["noise"] if c.get("ignore_reason") == "bar_luma"]
    if noise_short:
        needed = decision["needed_hits"]
        print(
            f"Ignored brief dirt/noise (fewer than {needed} body samples, "
            f"under --min-insert {min_insert}s; will not block a majority crop):"
        )
        for cluster in noise_short:
            label_ar(cluster, width, height)
            label = f"'{cluster['ar_label']}'" if cluster["ar_label"] else "Custom AR"
            ts = cluster.get("body_timestamps") or cluster["timestamps"]
            span = f"{format_timestamp(ts[0])}–{format_timestamp(ts[-1])}" if ts else "?"
            n = len(cluster.get("body_timestamps") or [])
            print(f"  - {label} ({cluster['outer']}) at {span} ({n} body hits)")
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
            print(f"  - {label} ({cluster['outer']}) at {span} (bar YAVG {ytxt})")

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
            "-show_entries", "stream=width,height,disposition",
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
    return duration, width, height


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
        default=max(1, multiprocessing.cpu_count() // 2),
        help="Worker processes. Default: half of available cores.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"Seconds between samples (default: {DEFAULT_INTERVAL}). Use 1 for very short inserts.",
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
            f"Skip frames with full-frame YAVG below this (default: {DEFAULT_LUMA_MIN}). "
            f"Too-dark scenes make cropdetect report extra bars."
        ),
    )
    parser.add_argument(
        "--bar-delta",
        type=float,
        default=DEFAULT_BAR_DELTA,
        help=(
            f"Bar-strip YAVG must exceed the majority-letterbox baseline by this much "
            f"to count as picture (default: {DEFAULT_BAR_DELTA}). Flakes stay near baseline."
        ),
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the gap-fill pass that hunts for short AR inserts between grid samples.",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose cluster/sample logging.")
    args = parser.parse_args()

    if args.interval <= 0:
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
        duration, width, height = probe_video(args.input)
        print(f"Detected duration: {duration:.1f}s ({format_timestamp(duration)})")
        print(f"Detected resolution: {width}x{height}")
    except Exception as e:
        print(f"{COLOR_RED}Error probing video file: {e}{COLOR_RESET}")
        sys.exit(1)

    print("\n--- Video Analysis Parameters ---")
    print(f"Input File: {os.path.basename(args.input)}")
    print(f"Duration: {duration:.1f}s")
    print(f"Resolution: {width}x{height}")
    print(f"Number of Workers: {args.num_workers}")
    print(f"Sample interval: {args.interval}s ({FRAMES_PER_SAMPLE} frames/seek)")
    print(f"Gap-fill verify: {'off' if args.no_verify else 'on'}")
    print(f"Edge ignore: first/last {args.edge * 100:.1f}% (logos / scrolling credits)")
    print(
        f"Min artistic insert: {args.min_insert}s "
        f"({min_body_hits(args.interval, args.min_insert)} body samples) — shorter is dirt/noise"
    )
    print(f"Full-frame luma min: {args.luma_min} (darker = skip, cropdetect would false-tighten)")
    print(f"Bar-luma delta: {args.bar_delta} above majority letterbox (flake vs real insert)")
    print(f"Significance label threshold: {args.significant_crop_threshold}% (report only)")
    print(f"Minimum Crop Size: {args.min_crop}px")

    check_prerequisites()
    analyze_video(
        args.input,
        duration,
        width,
        height,
        args.num_workers,
        args.significant_crop_threshold,
        args.min_crop,
        args.interval,
        args.edge,
        args.luma_min,
        args.min_insert,
        args.bar_delta,
        verify=not args.no_verify,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
