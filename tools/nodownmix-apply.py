#!/usr/bin/env python3
"""Add or remove a nodownmix tag on .mkv filenames.

The tag is inserted immediately before the extension, e.g.
``Movie.2016.mkv`` becomes ``Movie.2016.nodownmix.mkv``. Leading
queue prefixes such as ``01 `` are left untouched. Use --remove to
strip the tag again (also accepting space, underscore or hyphen as
the separator). Intended as a helper for encoders that treat the
tag as a per-file --no-downmix switch.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_TAG = "nodownmix"
SEP_CLASS = r"[._\-\s]"
WORK_SUFFIXES = (
    ".prep.mkv",
    ".cfr.mkv",
    ".cfr_full.mkv",
    ".x264.mkv",
    ".hevc.mkv",
    ".ut.mkv",
    ".cfr_temp.mkv",
    "_xav.mkv",
)
WORK_PREFIXES = ("temp-", "output-")


def is_mkv(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".mkv"


def is_work_file(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(WORK_SUFFIXES) or name.startswith(WORK_PREFIXES)


def tag_re(tag: str) -> re.Pattern[str]:
    return re.compile(rf"(?:^|{SEP_CLASS}){re.escape(tag)}$", re.IGNORECASE)


def target_key(path: Path) -> tuple[Path, str]:
    try:
        parent = path.parent.resolve()
    except OSError:
        parent = path.parent
    return parent, path.name.casefold()


def planned_name(
    path: Path,
    tag: str,
    remove: bool,
    pattern: re.Pattern[str] | None = None,
    claimed_targets: set[tuple[Path, str]] | None = None,
) -> tuple[Path | None, str | None]:
    """Return (target, None) to rename, or (None, reason) to skip."""
    if is_work_file(path):
        return None, f"skip work file: {path}"
    rx = pattern if pattern is not None else tag_re(tag)
    stem, suffix = path.stem, path.suffix
    if remove:
        if not rx.search(stem):
            return None, f"skip (no tag): {path}"
        new_stem = rx.sub("", stem)
        if not new_stem:
            return None, f"skip (empty name after removing tag): {path}"
    else:
        if rx.search(stem):
            return None, f"skip (already tagged): {path}"
        new_stem = f"{stem}.{tag}"
    target = path.with_name(new_stem + suffix)
    if target.exists() or (claimed_targets is not None and target_key(target) in claimed_targets):
        return None, f"warning: target exists, skip: {path} -> {target}"
    return target, None


def collect_targets(
    paths: list[Path], recursive: bool
) -> tuple[list[Path], list[str], list[str]]:
    """Return (mkv files, skip notes for explicit non-mkv paths, errors)."""
    files: list[Path] = []
    skips: list[str] = []
    errors: list[str] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError as exc:
            errors.append(f"error: cannot access {path}: {exc}")
            return
        if key not in seen:
            seen.add(key)
            files.append(path)

    for raw in paths:
        if not raw.exists():
            errors.append(f"error: path not found: {raw}")
        elif raw.is_file():
            if is_mkv(raw):
                add(raw)
            else:
                skips.append(f"skip (not an .mkv file): {raw}")
        elif raw.is_dir():
            try:
                children = raw.rglob("*") if recursive else raw.iterdir()
                for child in children:
                    if is_mkv(child):
                        add(child)
            except OSError as exc:
                errors.append(f"error: cannot read {raw}: {exc}")
        else:
            errors.append(f"error: not a file or folder: {raw}")

    files.sort(key=lambda p: str(p).casefold())
    return files, skips, errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodownmix-apply.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Add or remove a filename tag on .mkv files so an encoder can\n"
            "apply --no-downmix per file.\n"
            "\n"
            "By default the tag is inserted immediately before the extension:\n"
            "  Movie.2016.mkv  ->  Movie.2016.nodownmix.mkv\n"
            "\n"
            "Folders are scanned non-recursively unless -r is given. Encoder work\n"
            "files (*.prep.mkv, *.cfr.mkv, temp-*.mkv, output-*.mkv, etc.) are skipped."
        ),
        epilog=(
            "examples:\n"
            "  nodownmix-apply.py\n"
            "      Tag every .mkv file in the current folder.\n"
            "  nodownmix-apply.py file1.mkv file2.mkv\n"
            "      Tag only the given files.\n"
            "  nodownmix-apply.py -n /path/to/folder\n"
            "      Show what would be renamed, without changing anything.\n"
            "  nodownmix-apply.py --remove .\n"
            "      Strip the tag from .mkv files in the current folder.\n"
            "  nodownmix-apply.py -r /path/to/folder\n"
            "      Also process .mkv files in subfolders."
        ),
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="Files or folders to process (default: current folder)",
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="Strip the tag from filenames instead of adding it",
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="Scan folders recursively",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Show what would change without renaming",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Only print warnings and errors",
    )
    parser.add_argument(
        "--tag", default=DEFAULT_TAG, metavar="TEXT",
        help=f"Tag to add or remove (default: {DEFAULT_TAG})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tag = args.tag.strip().lstrip(".")
    if not tag:
        print("error: --tag must not be empty", file=sys.stderr)
        return 1
    if any(ch in tag for ch in "/\\\x00\r\n"):
        print("error: --tag must not contain path separators or control characters", file=sys.stderr)
        return 1

    pattern = tag_re(tag)
    files, skips, errors = collect_targets(args.paths or [Path(".")], args.recursive)
    renamed = 0
    skipped = len(skips)
    claimed_targets: set[tuple[Path, str]] = set()
    if not args.quiet:
        for note in skips:
            print(note)

    for path in files:
        target, reason = planned_name(path, tag, args.remove, pattern=pattern, claimed_targets=claimed_targets)
        if reason is not None:
            skipped += 1
            if reason.startswith("warning:"):
                print(reason, file=sys.stderr)
            elif not args.quiet:
                print(reason)
            continue
        assert target is not None
        if not args.quiet:
            print(f"{path} -> {target}")
        if not args.dry_run:
            try:
                path.rename(target)
            except OSError as exc:
                errors.append(f"error: failed to rename {path} -> {target}: {exc}")
                continue
        claimed_targets.add(target_key(target))
        renamed += 1

    if not args.quiet:
        action = "would rename" if args.dry_run else "renamed"
        print(f"{action}: {renamed}, skipped: {skipped}, errors: {len(errors)}")
    for msg in errors:
        print(msg, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
