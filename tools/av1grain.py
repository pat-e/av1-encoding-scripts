#!/usr/bin/env python3

# av1grain.py - add AV1 film grain synthesis to finished AV1 MKVs with grav1synth (no re-encode).
#
# cd into a folder of finished AV1 MKVs (e.g. xav_automation.py results) and run
# `av1grain.py --iso 400` or `av1grain.py --preset 16mm`. For every *.mkv in that folder:
#   1. extract the AV1 video track to a video-only temp MKV with mkvmerge (so grav1synth
#      never sees attachments, cover art, audio, or subtitles that libavformat can mangle)
#   2. grav1synth apply on the video-only temp (grain is only signalled in the AV1 headers;
#      video data is untouched)
#   3. remux the grained video back with all original audio, subtitles, attachments,
#      chapters, tags, and video track metadata using mkvmerge
#   4. checks: frame count, duration, tracks (including IETF tags & flags), attachments,
#      and chapters must match the input
#   5. result -> completed/<same name>, input -> original/<same name>,
#      non-AV1 input -> skipped/<same name> (folder created only when needed),
#      on error the input -> failed/<same name>. One log per file in conv_logs/.

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DIR_COMPLETED = Path("completed")
DIR_ORIGINAL = Path("original")
DIR_CONV_LOGS = Path("conv_logs")
DIR_FAILED = Path("failed")
DIR_SKIPPED = Path("skipped")
REQUIRED_TOOLS = ["grav1synth", "mkvmerge", "ffprobe"]
GRAV1SYNTH_URL = "https://github.com/rust-av/grav1synth"
DEFAULT_ISO = 1200  # = Av1an --photon-noise 12 (Av1an uses ISO = strength * 100)
INTERRUPTED = False

# mkvmerge -J property -> mkvmerge CLI flag name
VIDEO_FLAG_OPTIONS = (
    ("default_track", "--default-track-flag"),
    ("forced_track", "--forced-display-flag"),
    ("enabled_track", "--track-enabled-flag"),
    ("flag_hearing_impaired", "--hearing-impaired-flag"),
    ("flag_visual_impaired", "--visual-impaired-flag"),
    ("flag_text_descriptions", "--text-descriptions-flag"),
    ("flag_original", "--original-flag"),
    ("flag_commentary", "--commentary-flag"),
)

# mkvmerge -J property -> mkvmerge CLI option for colour / HDR container metadata
VIDEO_COLOR_OPTIONS = (
    ("color_matrix_coefficients", "--colour-matrix-coefficients"),
    ("color_bits_per_channel", "--colour-bits-per-channel"),
    ("chroma_subsample", "--chroma-subsample"),
    ("chroma_siting", "--chroma-siting"),
    ("color_range", "--colour-range"),
    ("color_transfer_characteristics", "--colour-transfer-characteristics"),
    ("color_primaries", "--colour-primaries"),
    ("max_content_light", "--max-content-light"),
    ("max_frame_light", "--max-frame-light"),
    ("chromaticity_coordinates", "--chromaticity-coordinates"),
    ("white_color_coordinates", "--white-colour-coordinates"),
    ("max_luminance", "--max-luminance"),
    ("min_luminance", "--min-luminance"),
)


def on_sigint(signum, frame):
    global INTERRUPTED
    INTERRUPTED = True
    raise KeyboardInterrupt


class Tee:
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


def safe_path_arg(path):
    """Avoid leading '-' or '@' being parsed as CLI options or mkvmerge option files."""
    text = str(path)
    if text.startswith(("-", "@")):
        return str(Path(path).resolve())
    return text


def mkv_option_value(value):
    """Escape a leading '@' so mkvmerge does not treat it as an options file."""
    text = str(value)
    if text.startswith("@"):
        return "@" + text
    return text


def stop_proc(proc):
    if proc.poll() is not None:
        return
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    else:
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


def run_capture(args):
    """Run a subprocess in its own session (POSIX) and guarantee termination on interrupt."""
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    try:
        stdout, _ = proc.communicate()
    except BaseException:
        stop_proc(proc)
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr=None)


def safe_move(src, dest):
    """Move src to dest without overwriting an existing file (like mv -n)."""
    src = Path(src)
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"destination already exists: {dest}")
    try:
        os.link(src, dest)
    except FileExistsError:
        raise FileExistsError(f"destination already exists: {dest}") from None
    except OSError:
        # Cross-device move or filesystem without hardlinks; check again and fall back.
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(f"destination already exists: {dest}") from None
        shutil.move(str(src), str(dest))
        return
    try:
        src.unlink()
    except OSError:
        # Both links exist; remove the new destination link to leave src intact.
        dest.unlink(missing_ok=True)
        raise


def format_mkv_decimal(value):
    """Format a float as plain decimal for mkvmerge (which rejects scientific notation)."""
    if not math.isfinite(value):
        return None
    return format(value, ".10f").rstrip("0").rstrip(".") or "0"


def format_mkv_color_value(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return format_mkv_decimal(value)
    if isinstance(value, str) and ("e" in value or "E" in value):
        parts = []
        for token in value.split(","):
            s = token.strip()
            try:
                num = float(s)
            except ValueError:
                parts.append(s)
                continue
            formatted = format_mkv_decimal(num)
            if formatted is None:
                return None
            parts.append(formatted)
        return ",".join(parts)
    return str(value)


def run_json(cmd):
    return json.loads(subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     check=True, text=True).stdout)


def run_mkvmerge(args, label):
    print(f"    - {label}: {' '.join(args)}")
    res = run_capture(args)
    for line in res.stdout.splitlines():
        if not line.startswith("Progress:"):
            print(f"      {line}")
    if res.returncode >= 2:  # 1 = warnings only
        raise RuntimeError(f"mkvmerge ({label}) exited with code {res.returncode}")


def file_is_usable(path):
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def list_source_mkvs(current_dir):
    return sorted(f for f in current_dir.glob("*.mkv")
                  if f.is_file() and not f.name.startswith("."))


def count_video_packets(path):
    info = run_json(["ffprobe", "-v", "error", "-select_streams", "V:0", "-count_packets",
                     "-show_entries", "stream=nb_read_packets", "-of", "json", safe_path_arg(path)])
    streams = info.get("streams") or []
    if not streams or "nb_read_packets" not in streams[0]:
        raise RuntimeError(f"ffprobe could not count video packets in {path}")
    return int(streams[0]["nb_read_packets"])


def track_summary(identify):
    rows = []
    for t in identify.get("tracks", []):
        p = t.get("properties", {})
        rows.append((
            t["type"],
            t["codec"],
            p.get("language_ietf") or p.get("language"),
            p.get("track_name"),
            bool(p.get("default_track", False)),
            bool(p.get("forced_track", False)),
            bool(p.get("enabled_track", True)),
            bool(p.get("flag_hearing_impaired", False)),
            bool(p.get("flag_visual_impaired", False)),
            bool(p.get("flag_text_descriptions", False)),
            bool(p.get("flag_original", False)),
            bool(p.get("flag_commentary", False)),
        ))
    return rows


def attachment_summary(identify):
    return [
        (a.get("file_name"), a.get("content_type"), a.get("size"))
        for a in identify.get("attachments", [])
    ]


def chapter_summary(identify):
    return [c.get("num_entries") for c in identify.get("chapters", [])]


def container_duration(identify):
    return int(identify.get("container", {}).get("properties", {}).get("duration", 0)) / 1e9


def extract_video_only(src_path, vid_tmp, vid_id, normalize_sar=None):
    """Extract only the AV1 video track to a clean MKV for grav1synth."""
    args = [
        "mkvmerge", "-o", safe_path_arg(vid_tmp),
        "--title", "",
        "--video-tracks", str(vid_id),
        "--no-audio", "--no-subtitles", "--no-attachments",
        "--no-chapters", "--no-global-tags",
    ]
    if normalize_sar:
        args += [
            "--display-dimensions", f"{vid_id}:{normalize_sar}",
            "--cropping", f"{vid_id}:0,0,0,0",
        ]
    args.append(safe_path_arg(src_path))
    run_mkvmerge(args, "extract video")
    if not file_is_usable(vid_tmp):
        raise RuntimeError("mkvmerge produced an empty video-only intermediate")


def run_grav1synth(vid_tmp, grained_tmp, grain_args):
    args = [
        "grav1synth", "apply", *grain_args, "-y",
        "-o", safe_path_arg(grained_tmp),
        safe_path_arg(vid_tmp),
    ]
    print(f"    - grav1synth: {' '.join(args)}")
    res = run_capture(args)
    for line in res.stdout.splitlines():
        print(f"      {line}")
    return res


def video_restore_args(src_vtrack):
    """mkvmerge options to restore video track metadata that grav1synth can strip."""
    props = src_vtrack.get("properties", {})
    lang = props.get("language_ietf") or props.get("language") or "und"
    name = props.get("track_name") or ""
    args = [
        "--language", f"0:{lang}",
        "--track-name", f"0:{name}",
    ]
    for json_key, flag_opt in VIDEO_FLAG_OPTIONS:
        if json_key in props:
            args += [flag_opt, f"0:{'yes' if props[json_key] else 'no'}"]

    disp_dims = props.get("display_dimensions")
    disp_unit = props.get("display_unit", 0)
    if disp_dims:
        if disp_unit in (None, 0):
            args += ["--display-dimensions", f"0:{disp_dims}"]
        elif disp_unit == 3 and "x" in str(disp_dims):
            args += ["--aspect-ratio", f"0:{str(disp_dims).replace('x', '/')}"]

    if props.get("cropping"):
        args += ["--cropping", f"0:{props['cropping']}"]
    if props.get("stereo_mode") is not None:
        args += ["--stereo-mode", f"0:{props['stereo_mode']}"]

    for json_key, opt_name in VIDEO_COLOR_OPTIONS:
        value = props.get(json_key)
        if value is not None:
            formatted = format_mkv_color_value(value)
            if formatted is not None:
                args += [opt_name, f"0:{formatted}"]

    return args


def remux_with_source(mux_tmp, grained_tmp, src_path, src_id, src_vtrack, num_video_tracks):
    """Combine the grained video with all non-video tracks, attachments, chapters and tags."""
    vid_id = src_vtrack["id"]
    src_title = src_id.get("container", {}).get("properties", {}).get("title")
    args = [
        "mkvmerge", "-o", safe_path_arg(mux_tmp),
        "--title", mkv_option_value(src_title) if src_title else "",
        *video_restore_args(src_vtrack),
        "--no-global-tags", "--no-chapters",
        safe_path_arg(grained_tmp),
    ]
    if num_video_tracks <= 1:
        args.append("--no-video")
    else:
        args += ["--video-tracks", f"!{vid_id}"]
    args.append(safe_path_arg(src_path))

    order = [
        "0:0" if t["id"] == vid_id else f"1:{t['id']}"
        for t in src_id.get("tracks", [])
    ]
    if order:
        args += ["--track-order", ",".join(order)]

    run_mkvmerge(args, "remux")
    if not file_is_usable(mux_tmp):
        raise RuntimeError("mkvmerge produced an empty output file")


def describe_mismatch(label, src_items, out_items):
    print(f"    - {label} mismatch:")
    print(f"      input  ({len(src_items)}): {src_items}")
    print(f"      output ({len(out_items)}): {out_items}")


def is_av1_video(track):
    codec_id = str(track.get("properties", {}).get("codec_id") or "").upper()
    codec = str(track.get("codec") or "").upper()
    return codec_id == "V_AV1" or "AV1" in codec


def check_av1_source(src_id):
    """Return (videos, skip_reason). skip_reason is None when the primary video is AV1.

    Raises RuntimeError if mkvmerge could not recognize or read the container.
    """
    container = src_id.get("container") or {}
    errors = src_id.get("errors") or []
    if not container.get("recognized", False) or not container.get("supported", True):
        detail = "; ".join(str(e) for e in errors) if errors else "unrecognized or unsupported container"
        raise RuntimeError(f"invalid MKV ({detail})")
    videos = [t for t in src_id.get("tracks", []) if t["type"] == "video"]
    if not videos:
        return videos, "no video track"
    if not is_av1_video(videos[0]):
        codec = videos[0].get("codec") or videos[0].get("properties", {}).get("codec_id") or "unknown"
        return videos, f"video codec is not AV1 ({codec})"
    return videos, None


def process_file(file_path, grain_args, src_id=None):
    vid_tmp = file_path.with_name(f".{file_path.stem}.grain-vid.mkv")
    grained_tmp = file_path.with_name(f".{file_path.stem}.grain-tmp.mkv")
    mux_tmp = file_path.with_name(f".{file_path.stem}.grain-mux.mkv")
    temps = (vid_tmp, grained_tmp, mux_tmp)
    completed_dest = DIR_COMPLETED / file_path.name
    original_dest = DIR_ORIGINAL / file_path.name
    try:
        print(f"Analyzing file: {file_path.resolve()}")
        for dest in (completed_dest, original_dest):
            if dest.exists() or dest.is_symlink():
                raise FileExistsError(f"destination already exists: {dest}")
        if src_id is None:
            src_id = run_json(["mkvmerge", "-J", safe_path_arg(file_path)])
        videos, skip_reason = check_av1_source(src_id)
        if skip_reason:
            raise RuntimeError(skip_reason)
        src_vtrack = videos[0]
        vid_id = src_vtrack["id"]

        extract_video_only(file_path, vid_tmp, vid_id)

        res = run_grav1synth(vid_tmp, grained_tmp, grain_args)
        if res.returncode != 0 and "Aspect ratio mismatch" in res.stdout:
            pixel_dims = src_vtrack.get("properties", {}).get("pixel_dimensions")
            if pixel_dims:
                print(
                    "    - grav1synth hit container aspect-ratio mismatch; retrying with "
                    f"normalized intermediate ({pixel_dims}) and restoring container display/crop on remux..."
                )
                vid_tmp.unlink(missing_ok=True)
                grained_tmp.unlink(missing_ok=True)
                extract_video_only(file_path, vid_tmp, vid_id, normalize_sar=pixel_dims)
                res = run_grav1synth(vid_tmp, grained_tmp, grain_args)

        if "Skipped: grain headers already exist" in res.stdout:
            raise RuntimeError("file already has grain (use --replace to overwrite it)")
        if res.returncode != 0:
            raise RuntimeError(f"grav1synth exited with code {res.returncode}")
        if not file_is_usable(grained_tmp):
            raise RuntimeError("grav1synth did not write an output file")

        # Free the video-only extract before remuxing the full container.
        vid_tmp.unlink(missing_ok=True)

        remux_with_source(mux_tmp, grained_tmp, file_path, src_id, src_vtrack, len(videos))
        grained_tmp.unlink(missing_ok=True)

        print("Checking output...")
        out_id = run_json(["mkvmerge", "-J", safe_path_arg(mux_tmp)])
        src_frames, out_frames = count_video_packets(file_path), count_video_packets(mux_tmp)
        print(f"    - Frames: input {src_frames}, output {out_frames}")
        if src_frames != out_frames:
            raise RuntimeError(f"frame count mismatch (input {src_frames}, output {out_frames})")

        src_dur, out_dur = container_duration(src_id), container_duration(out_id)
        if src_dur > 0 and abs(src_dur - out_dur) > 0.5:
            raise RuntimeError(f"duration mismatch (input {src_dur:.3f}s, output {out_dur:.3f}s)")

        src_tracks, out_tracks = track_summary(src_id), track_summary(out_id)
        if src_tracks != out_tracks:
            describe_mismatch("Track list", src_tracks, out_tracks)
            raise RuntimeError("track list differs from input")

        src_atts, out_atts = attachment_summary(src_id), attachment_summary(out_id)
        if src_atts != out_atts:
            describe_mismatch("Attachments", src_atts, out_atts)
            raise RuntimeError("attachments differ from input")

        src_chaps, out_chaps = chapter_summary(src_id), chapter_summary(out_id)
        if src_chaps != out_chaps:
            describe_mismatch("Chapters", src_chaps, out_chaps)
            raise RuntimeError("chapters differ from input")

        att_note = f", {len(out_atts)} attachment(s)" if out_atts else ""
        print(f"    - OK: frames, tracks, chapters{att_note} match the input.")

        print("Moving files to final destinations...")
        for dest in (completed_dest, original_dest):
            if dest.exists() or dest.is_symlink():
                raise FileExistsError(f"destination already exists: {dest}")
        safe_move(mux_tmp, completed_dest)
        safe_move(file_path, original_dest)
    finally:
        for tmp in temps:
            if tmp.exists():
                print(f"    Deleting: {tmp}")
                tmp.unlink(missing_ok=True)


def missing_tools():
    return [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]


def grav1synth_hint_lines(missing):
    if "grav1synth" not in missing:
        return []
    return [
        f"grav1synth: {GRAV1SYNTH_URL}",
        "  cargo install grav1synth",
        "  Then put the binary on PATH (for example ~/bin).",
    ]


def check_tools(report_only=False):
    """Exit if a required program is missing. --check prints every line and does not apply grain."""
    missing = missing_tools()
    if not report_only:
        if missing:
            print(f"Required tool(s) not found in PATH: {', '.join(missing)}")
            for line in grav1synth_hint_lines(missing):
                print(line)
            sys.exit(1)
        return

    print("Check (no grain apply)")
    for tool in REQUIRED_TOOLS:
        state = "MISSING" if tool in missing else "OK"
        suffix = f"  {GRAV1SYNTH_URL}" if tool == "grav1synth" and tool in missing else ""
        print(f"  {state:7} {tool}{suffix}")
    for line in grav1synth_hint_lines(missing):
        print(line)
    if missing:
        print(f"Missing required tools: {', '.join(missing)}")
        sys.exit(1)
    print("All required tools are available.")


def main(grain_args):
    signal.signal(signal.SIGINT, on_sigint)
    check_tools()

    current_dir = Path(".")
    if not list_source_mkvs(current_dir):
        print("No MKV files found to process. Exiting.")
        return
    for d in (DIR_COMPLETED, DIR_ORIGINAL, DIR_CONV_LOGS, DIR_FAILED):
        d.mkdir(exist_ok=True, parents=True)
    print(f"av1grain: {' '.join(grain_args)}")

    any_failed = False
    unmoved = set()

    while True:
        files = [f for f in list_source_mkvs(current_dir) if f.resolve() not in unmoved]
        if not files:
            print("No more .mkv files found to process in the current directory. The script will now exit.")
            break
        file_path = files[0]
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        print("-" * width)
        log_file_path = DIR_CONV_LOGS / f"{file_path.stem}.log"
        console_out, console_err = sys.stdout, sys.stderr
        started = datetime.now()

        # Quick pre-check: only AV1 MKVs are processed; non-AV1 files are skipped
        # cleanly without touching or creating skipped/ unless actually needed.
        src_id = None
        skip_reason = None
        try:
            src_id = run_json(["mkvmerge", "-J", safe_path_arg(file_path)])
            _, skip_reason = check_av1_source(src_id)
        except KeyboardInterrupt:
            print("\nInterrupted. File left in place.")
            sys.exit(130)
        except Exception:
            # Let the normal processing block log and move unreadable files to failed/.
            src_id = None

        if skip_reason is not None:
            DIR_SKIPPED.mkdir(exist_ok=True, parents=True)
            dest = DIR_SKIPPED / file_path.name
            try:
                safe_move(file_path, dest)
            except FileExistsError as e:
                any_failed = True
                unmoved.add(file_path.resolve())
                log_file_path.write_text(
                    f"SKIPPED (LEFT IN PLACE): {file_path.name}\n"
                    f"Reason: {skip_reason}\n"
                    f"Error: {e}\n"
                    f"Time: {started.strftime('%Y-%m-%d %H:%M:%S')}\n",
                    encoding="utf-8",
                )
                print(f"Skipping: {file_path.name} ({skip_reason})")
                print(f"ERROR: Could not move '{file_path.name}': {e}. Left in place.", file=sys.stderr)
                continue
            log_file_path.write_text(
                f"SKIPPED: {file_path.name}\n"
                f"Reason: {skip_reason}\n"
                f"Time: {started.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Moved to: {dest}\n",
                encoding="utf-8",
            )
            print(f"Skipping: {file_path.name} ({skip_reason})")
            print(f"Moved to {dest}")
            continue

        print(f"Processing: {file_path.name}")
        failed = False
        with open(log_file_path, "w", encoding="utf-8", buffering=1) as log:
            sys.stdout = Tee(log, console_out)
            sys.stderr = Tee(log, console_err)
            try:
                print(f"STARTING LOG FOR: {file_path.name}")
                print(f"Processing started at: {started}")
                process_file(file_path, grain_args, src_id=src_id)
            except KeyboardInterrupt:
                print("\nInterrupted. File left in place, temp files removed.")
                sys.stdout, sys.stderr = console_out, console_err
                sys.exit(130)
            except Exception as e:
                if INTERRUPTED:
                    print("\nInterrupted. File left in place, temp files removed.")
                    sys.stdout, sys.stderr = console_out, console_err
                    sys.exit(130)
                print(f"ERROR: An error occurred while processing '{file_path.name}': {e}", file=sys.stderr)
                failed = True
            finally:
                runtime = str(datetime.now() - started).split(".")[0]
                print(f"FINISHED LOG FOR: {file_path.name} (runtime {runtime})")
                sys.stdout, sys.stderr = console_out, console_err
        if failed:
            any_failed = True
            if file_path.exists():
                failed_dest = DIR_FAILED / file_path.name
                try:
                    safe_move(file_path, failed_dest)
                    console_err.write(f"Moved to {failed_dest}\n")
                except FileExistsError as e:
                    unmoved.add(file_path.resolve())
                    console_err.write(f"ERROR: Could not move '{file_path.name}': {e}. Left in place.\n")

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="av1grain.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Add AV1 film grain synthesis to every MKV in the current folder, one file at a time,
with grav1synth (no re-encode: the grain is only signalled in the AV1 headers and
the decoder/player draws it). The AV1 video track is extracted into a video-only
intermediate for grav1synth and remuxed back with mkvmerge so audio, subs,
attachments (fonts, cover art), chapters, tags and names stay untouched.

Folders (created in the current folder, like xav_automation.py):
  completed/   files with grain added
  original/    the untouched input files
  skipped/     non-AV1 inputs (only created when a file is skipped)
  failed/      inputs that failed (e.g. already has grain, check mismatch)
  conv_logs/   one log per file (simplified entry for skipped files)

Default (no grain option given): --iso 1200, the same grain as Av1an's
--photon-noise 12 (Av1an turns its strength into ISO = strength x 100, luma only
unless --chroma-noise). Use either --iso or --preset, not both.

--check reports grav1synth, mkvmerge and ffprobe, then exits.
It does not apply grain. grav1synth is at https://github.com/rust-av/grav1synth.""",
        epilog="""\
examples:
  av1grain.py --check
      check that grav1synth, mkvmerge and ffprobe are on PATH

  av1grain.py
      default: --iso 1200 (= Av1an --photon-noise 12), "normal" TV shows

  av1grain.py --iso 400
      light grain (= Av1an --photon-noise 4), e.g. clean modern shows / anime

  av1grain.py --iso 800
      = Av1an --photon-noise 8

  av1grain.py --iso 1600
      = Av1an --photon-noise 16, grainier film-based shows

  av1grain.py --iso 2400
      = Av1an --photon-noise 24, heavy grain (old film)

  av1grain.py --iso 800 --chroma
      photon-noise grain also on the colour planes (= Av1an --photon-noise 8 --chroma-noise)

  av1grain.py --preset 16mm
      16mm film stock preset (Fujifilm Eterna 250D)

  av1grain.py --preset Classic35-2
      Super 35mm preset with Kodak Vision3 250D

  av1grain.py --iso 400 --replace
      overwrite grain that is already in the files

Av1an --photon-noise N  ->  --iso N*100   (4 -> 400, 8 -> 800, 12 -> 1200, 16 -> 1600)

presets: Super8, MaxMid, 16mm, Classic35, Modern35
stock suffix (16mm, Classic35, Modern35): none = Fujifilm Eterna 250D,
  -1 = Fujifilm Eterna 500T, -2 = Kodak Vision3 250D, -3 = Kodak Vision3 200T
see also: grav1synth presets""",
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--iso", type=int, metavar="N",
                   help=f"photon-noise grain strength, 100-6400 recommended (default: {DEFAULT_ISO} = Av1an --photon-noise 12)")
    g.add_argument("--preset", metavar="NAME", help="built-in film stock preset, e.g. 16mm or Classic35-2")
    parser.add_argument("--chroma", action="store_true", help="with --iso: add grain to chroma as well")
    parser.add_argument("--replace", action="store_true", help="overwrite existing grain (otherwise such files go to failed/)")
    parser.add_argument("--check", action="store_true",
                        help="Check required programs, then exit. Does not apply grain. "
                             f"Prints {GRAV1SYNTH_URL} when grav1synth is missing.")
    a = parser.parse_args()
    if a.check:
        check_tools(report_only=True)
        sys.exit(0)
    if a.iso is not None and a.iso < 1:
        parser.error("--iso must be >= 1")
    if a.chroma and a.preset is not None:
        parser.error("--chroma only works with --iso")
    if a.iso is None and a.preset is None:
        a.iso = DEFAULT_ISO
    grain = ["--iso", str(a.iso)] if a.iso is not None else ["--preset", a.preset]
    if a.chroma:
        grain.append("--chroma")
    if a.replace:
        grain.append("--replace")
    main(grain)
