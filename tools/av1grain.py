#!/usr/bin/env python3

# av1grain.py - add AV1 film grain synthesis to finished AV1 MKVs with grav1synth (no re-encode).
#
# cd into a folder of finished AV1 MKVs (e.g. xav_automation.py results) and run
# `av1grain.py --iso 400` or `av1grain.py --preset 16mm`. For every *.mkv in that folder:
#   1. grav1synth apply (grain is only signalled in the AV1 headers; video data is untouched)
#   2. checks: frame count, audio/subtitle tracks and chapters must match the input
#   3. result -> completed/<same name>, input -> original/<same name>,
#      on error the input -> failed/<same name>. One log per file in conv_logs/.

import argparse
import json
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
REQUIRED_TOOLS = ["grav1synth", "mkvmerge", "mkvpropedit", "ffprobe"]
GRAV1SYNTH_URL = "https://github.com/rust-av/grav1synth"
DEFAULT_ISO = 1200  # = Av1an --photon-noise 12 (Av1an uses ISO = strength * 100)
INTERRUPTED = False


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


def run_json(cmd):
    return json.loads(subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     check=True, text=True).stdout)


def list_source_mkvs(current_dir):
    return sorted(f for f in current_dir.glob("*.mkv")
                  if f.is_file() and not f.name.startswith("."))


def count_video_packets(path):
    info = run_json(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                     "-show_entries", "stream=nb_read_packets", "-of", "json", str(path)])
    return int(info["streams"][0]["nb_read_packets"])


def track_summary(identify):
    rows = []
    for t in identify.get("tracks", []):
        p = t.get("properties", {})
        rows.append((t["type"], t["codec"], p.get("language_ietf") or p.get("language"),
                     p.get("track_name"), p.get("default_track"), p.get("forced_track")))
    return rows


def process_file(file_path, grain_args):
    tmp = file_path.with_name(f".{file_path.stem}.grain-tmp.mkv")
    try:
        print(f"Analyzing file: {file_path.resolve()}")
        src_id = run_json(["mkvmerge", "-J", str(file_path)])
        videos = [t for t in src_id.get("tracks", []) if t["type"] == "video"]
        if not videos or "AV1" not in videos[0]["codec"]:
            raise RuntimeError(f"video is not AV1 ({videos[0]['codec'] if videos else 'no video'})")

        args = ["grav1synth", "apply", *grain_args, "-y", "-o", str(tmp), str(file_path)]
        print(f"    - grav1synth: {' '.join(args)}")
        res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             start_new_session=True)
        for line in res.stdout.splitlines():
            print(f"      {line}")
        if res.returncode != 0:
            raise RuntimeError(f"grav1synth exited with code {res.returncode}")
        if "Skipped" in res.stdout or not tmp.exists():
            raise RuntimeError("file already has grain (use --replace to overwrite it)")

        # grav1synth writes only the old 3-letter language tags; put the
        # source's IETF language tags (en, de, ...) back with mkvpropedit.
        edit = []
        for n, t in enumerate(src_id.get("tracks", []), start=1):
            ietf = t.get("properties", {}).get("language_ietf")
            if ietf:
                edit += ["--edit", f"track:{n}", "--set", f"language={ietf}"]
        if edit:
            res = subprocess.run(["mkvpropedit", "-q", str(tmp), *edit],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 start_new_session=True)
            if res.returncode != 0:
                raise RuntimeError(f"mkvpropedit failed: {res.stdout.strip()}")
            print("    - Restored IETF language tags from the input.")

        print("Checking output...")
        out_id = run_json(["mkvmerge", "-J", str(tmp)])
        src_frames, out_frames = count_video_packets(file_path), count_video_packets(tmp)
        print(f"    - Frames: input {src_frames}, output {out_frames}")
        if src_frames != out_frames:
            raise RuntimeError("frame count mismatch")
        if track_summary(src_id) != track_summary(out_id):
            raise RuntimeError("track list differs from input")
        if len(src_id.get("chapters", [])) != len(out_id.get("chapters", [])):
            raise RuntimeError("chapters differ from input")
        print("    - OK: frames, tracks and chapters match the input.")

        print("Moving files to final destinations...")
        shutil.move(str(file_path), DIR_ORIGINAL / file_path.name)
        shutil.move(str(tmp), DIR_COMPLETED / file_path.name)
    finally:
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
        started = datetime.now()
        failed = False
        with open(log_file_path, "w", encoding="utf-8", buffering=1) as log:
            sys.stdout = Tee(log, console_out)
            sys.stderr = Tee(log, console_err)
            try:
                print(f"STARTING LOG FOR: {file_path.name}")
                print(f"Processing started at: {started}")
                process_file(file_path, grain_args)
            except KeyboardInterrupt:
                print("\nInterrupted. File left in place, temp file removed.")
                sys.stdout, sys.stderr = console_out, console_err
                sys.exit(130)
            except Exception as e:
                if INTERRUPTED:
                    print("\nInterrupted. File left in place, temp file removed.")
                    sys.stdout, sys.stderr = console_out, console_err
                    sys.exit(130)
                print(f"ERROR: An error occurred while processing '{file_path.name}': {e}", file=sys.stderr)
                failed = True
            finally:
                runtime = str(datetime.now() - started).split(".")[0]
                print(f"FINISHED LOG FOR: {file_path.name} (runtime {runtime})")
                sys.stdout, sys.stderr = console_out, console_err
        if failed and file_path.exists():
            shutil.move(str(file_path), DIR_FAILED / file_path.name)
            console_err.write(f"Moved to {DIR_FAILED / file_path.name}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="av1grain.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Add AV1 film grain synthesis to every MKV in the current folder, one file at a time,
with grav1synth (no re-encode: the grain is only signalled in the AV1 headers and
the decoder/player draws it). Audio, subs, chapters and names stay untouched.

Folders (created in the current folder, like xav_automation.py):
  completed/   files with grain added
  original/    the untouched input files
  failed/      inputs that failed (e.g. not AV1, already has grain, check mismatch)
  conv_logs/   one log per file

Default (no grain option given): --iso 1200, the same grain as Av1an's
--photon-noise 12 (Av1an turns its strength into ISO = strength x 100, luma only
unless --chroma-noise). Use either --iso or --preset, not both.

--check reports grav1synth, mkvmerge, mkvpropedit and ffprobe, then exits.
It does not apply grain. grav1synth is at https://github.com/rust-av/grav1synth.""",
        epilog="""\
examples:
  av1grain.py --check
      check that grav1synth, mkvmerge, mkvpropedit and ffprobe are on PATH

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
