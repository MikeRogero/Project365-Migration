#!/usr/bin/env python3
"""Repair local Taiwan 2024 photo metadata without exposing per-file details."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from project365_original_matcher import IMAGE_EXTENSIONS


INDEX_DB = Path("Project365Canonical/photo_library_index.sqlite")
TARGET_FOLDER_NAME = "Taiwan - 2024-08 Jade in Taiwan Wulai Yilan Tainan (video + nude shoots)"
START_DATE = dt.date(2024, 8, 2)
END_DATE = dt.date(2024, 8, 7)
BROKEN_DIR_NAME = "broken"

FILENAME_TIMESTAMP_PATTERNS = [
    re.compile(
        r"(?<!\d)(20\d{2}|19\d{2})[-_](0[1-9]|1[0-2])[-_]([0-2]\d|3[01])"
        r"[ T_-]+([01]\d|2[0-3])[:._-]?([0-5]\d)[:._-]?([0-5]\d)(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\d)(20\d{2}|19\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])"
        r"[ T_-]*([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)",
        re.IGNORECASE,
    ),
]

METADATA_TAGS = [
    "EXIF:DateTimeOriginal",
    "ExifIFD:DateTimeOriginal",
    "EXIF:CreateDate",
    "ExifIFD:CreateDate",
    "EXIF:ModifyDate",
    "IFD0:ModifyDate",
    "XMP-exif:DateTimeOriginal",
    "XMP-exif:DateTimeDigitized",
    "XMP-xmp:CreateDate",
    "QuickTime:ContentCreateDate",
    "QuickTime:CreationDate",
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:TrackCreateDate",
    "MacOS:FileCreateDate",
    "File:FileCreateDate",
    "File:FileModifyDate",
]

CREATE_OR_CAPTURE_TAGS = {
    "EXIF:DateTimeOriginal",
    "ExifIFD:DateTimeOriginal",
    "EXIF:CreateDate",
    "ExifIFD:CreateDate",
    "XMP-exif:DateTimeOriginal",
    "XMP-exif:DateTimeDigitized",
    "XMP-xmp:CreateDate",
    "QuickTime:ContentCreateDate",
    "QuickTime:CreationDate",
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:TrackCreateDate",
    "MacOS:FileCreateDate",
    "File:FileCreateDate",
}
MODIFY_TAGS = {
    "EXIF:ModifyDate",
    "IFD0:ModifyDate",
    "File:FileModifyDate",
}
FILE_CREATE_TAG = "File:FileCreateDate"
MACOS_FILE_CREATE_TAG = "MacOS:FileCreateDate"
FILENAME_NORMALIZATION_TAGS = [
    "MacOS:FileCreateDate",
    "ExifIFD:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "XMP-exif:DateTimeOriginal",
    "XMP-exif:DateTimeDigitized",
    "XMP-xmp:CreateDate",
]


@dataclass
class Classification:
    path: Path
    relpath: str
    action: str
    issues: list[str] = field(default_factory=list)
    filename_timestamp: str = ""
    error: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply metadata edits and moves.")
    parser.add_argument("--target-root", help="Exact Taiwan folder to repair. Defaults to resolving by index.")
    parser.add_argument("--report", help="Write JSON report to this path.")
    args = parser.parse_args()

    root = Path(args.target_root).expanduser().resolve() if args.target_root else infer_target_root()
    if root.name != TARGET_FOLDER_NAME:
        raise SystemExit("Target root does not match the requested Taiwan 2024 folder name.")
    files = collect_media_files(root)
    records = read_metadata(files)
    classifications = [classify(path, root, records.get(str(path.resolve()), {})) for path in files]

    if args.apply:
        apply_changes(root, classifications)

    report_path = Path(args.report) if args.report else default_report_path(root, args.apply)
    write_report(report_path, root, classifications, applied=args.apply)
    print_summary(root, report_path, classifications, applied=args.apply)
    return 0


def infer_target_root() -> Path:
    with sqlite3.connect(INDEX_DB) as connection:
        rows = connection.execute(
            "SELECT path FROM photo_library_files WHERE path LIKE ? ORDER BY path",
            (f"%/{TARGET_FOLDER_NAME}/%",),
        ).fetchall()
    if not rows:
        raise SystemExit("Requested Taiwan folder was not found in the photo-library index.")
    matches: set[Path] = set()
    for (path_text,) in rows:
        path = Path(path_text)
        for index, part in enumerate(path.parts):
            if part == TARGET_FOLDER_NAME:
                matches.add(Path(*path.parts[: index + 1]).resolve())
                break
    unique = sorted(matches)
    if len(unique) != 1:
        raise SystemExit(f"Expected one matching exact Taiwan folder, found {len(unique)}.")
    return unique[0]


def collect_media_files(root: Path) -> list[Path]:
    suffixes = {suffix.lower() for suffix in IMAGE_EXTENSIONS}
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath).resolve() == (root / BROKEN_DIR_NAME).resolve():
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name != BROKEN_DIR_NAME]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in suffixes:
                files.append(path.resolve())
    return sorted(files)


def read_metadata(paths: list[Path]) -> dict[str, dict[str, object]]:
    if not paths:
        return {}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as argfile:
        argfile_path = Path(argfile.name)
        argfile.write("-json\n")
        argfile.write("-G1\n")
        argfile.write("-a\n")
        for tag in METADATA_TAGS:
            argfile.write(f"-{tag}\n")
        for path in paths:
            argfile.write(f"{path}\n")
    try:
        result = subprocess.run(
            ["exiftool", "-api", "RequestAll=2", "-@", str(argfile_path)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        argfile_path.unlink(missing_ok=True)
    rows = json.loads(result.stdout or "[]")
    return {str(Path(row.get("SourceFile", "")).resolve()): row for row in rows if row.get("SourceFile")}


def classify(path: Path, root: Path, record: dict[str, object]) -> Classification:
    relpath = str(path.relative_to(root))
    item = Classification(path=path, relpath=relpath, action="skip")
    if not record:
        item.action = "move_broken"
        item.issues.append("metadata_unreadable")
        return item

    filename_dt = timestamp_from_filename(path.name)
    if filename_dt:
        item.filename_timestamp = filename_dt.isoformat(sep=" ")

    date_issues: list[str] = []
    for tag in CREATE_OR_CAPTURE_TAGS:
        value = record.get(tag)
        parsed = metadata_date(value)
        if not parsed:
            continue
        if parsed < START_DATE:
            date_issues.append(f"{tag}:before_range")
        elif parsed > END_DATE:
            date_issues.append(f"{tag}:after_range")

    for tag in MODIFY_TAGS:
        parsed = metadata_date(record.get(tag))
        if parsed and parsed < START_DATE:
            date_issues.append(f"{tag}:before_range")

    item.issues = sorted(set(date_issues))
    if filename_dt and START_DATE <= filename_dt.date() <= END_DATE and needs_filename_normalization(record, filename_dt):
        item.action = "repair_from_filename"
    elif not item.issues:
        return item
    elif filename_dt and START_DATE <= filename_dt.date() <= END_DATE:
        item.action = "repair_from_filename"
    else:
        item.action = "move_broken"
    return item


def timestamp_from_filename(filename: str) -> dt.datetime | None:
    for pattern in FILENAME_TIMESTAMP_PATTERNS:
        match = pattern.search(filename)
        if not match:
            continue
        year, month, day, hour, minute, second = map(int, match.groups())
        try:
            return dt.datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    return None


def metadata_date(value: object) -> dt.date | None:
    if isinstance(value, list):
        value = value[0] if value else ""
    text = str(value or "").strip()
    match = re.search(r"(19\d{2}|20\d{2})[:\-](\d{2})[:\-](\d{2})", text)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def metadata_datetime(value: object) -> dt.datetime | None:
    if isinstance(value, list):
        value = value[0] if value else ""
    text = str(value or "").strip()
    match = re.search(
        r"(19\d{2}|20\d{2})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
        text,
    )
    if not match:
        return None
    try:
        return dt.datetime(*map(int, match.groups()))
    except ValueError:
        return None


def needs_filename_normalization(record: dict[str, object], filename_dt: dt.datetime) -> bool:
    for tag in FILENAME_NORMALIZATION_TAGS:
        if metadata_datetime(record.get(tag)) != filename_dt:
            return True
    return False


def apply_changes(root: Path, classifications: list[Classification]) -> None:
    for item in classifications:
        if item.action == "repair_from_filename":
            repair_metadata(item)
    for item in classifications:
        if item.action == "move_broken":
            move_to_broken(root, item)


def repair_metadata(item: Classification) -> None:
    exif_ts = item.filename_timestamp.replace("-", ":")
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-P",
            f"-AllDates={exif_ts}",
            f"-XMP-exif:DateTimeOriginal={exif_ts}",
            f"-XMP-exif:DateTimeDigitized={exif_ts}",
            f"-XMP-xmp:CreateDate={exif_ts}",
            str(item.path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    set_file_create_date(item.path, item.filename_timestamp)


def set_file_create_date(path: Path, timestamp: str) -> None:
    parsed = dt.datetime.fromisoformat(timestamp)
    setfile_value = parsed.strftime("%m/%d/%Y %H:%M:%S")
    subprocess.run(
        ["SetFile", "-d", setfile_value, str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def move_to_broken(root: Path, item: Classification) -> None:
    destination = root / BROKEN_DIR_NAME / item.relpath
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = collision_path(destination)
    shutil.move(str(item.path), str(destination))


def collision_path(path: Path) -> Path:
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}__broken_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a collision-free destination for {path.name}")


def default_report_path(root: Path, applied: bool) -> Path:
    suffix = "applied" if applied else "dry_run"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("Project365Canonical") / "reports" / f"taiwan_2024_metadata_repair_{suffix}_{stamp}.json"


def write_report(report_path: Path, root: Path, classifications: list[Classification], applied: bool) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "applied": applied,
        "root": str(root),
        "event_date_range": [START_DATE.isoformat(), END_DATE.isoformat()],
        "summary": counts(classifications),
        "files": [
            {
                "relative_path": item.relpath,
                "action": item.action,
                "issues": item.issues,
                "filename_timestamp": item.filename_timestamp,
                "error": item.error,
            }
            for item in classifications
            if item.action != "skip"
        ],
    }
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def counts(classifications: list[Classification]) -> dict[str, int]:
    result = {
        "total_scanned": len(classifications),
        "skip": 0,
        "repair_from_filename": 0,
        "move_broken": 0,
    }
    for item in classifications:
        result[item.action] = result.get(item.action, 0) + 1
    return result


def print_summary(root: Path, report_path: Path, classifications: list[Classification], applied: bool) -> None:
    summary = counts(classifications)
    print("Project365 Taiwan 2024 metadata repair")
    print(f"Mode: {'APPLY' if applied else 'DRY-RUN'}")
    print(f"Target folder found: {'yes' if root.exists() else 'no'}")
    print(f"Total image files scanned: {summary['total_scanned']}")
    print(f"Already rational / skipped: {summary['skip']}")
    print(f"Repaired from filename timestamp: {summary['repair_from_filename']}")
    print(f"Moved to broken: {summary['move_broken']}")
    print(f"Local report written: {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
