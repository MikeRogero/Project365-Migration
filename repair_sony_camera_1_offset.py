#!/usr/bin/env python3
"""Repair Sony Camera 1 reset-clock timestamps using a known baseline offset."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from repair_taiwan_2024_metadata import infer_target_root


BROKEN_FOLDER_NAME = "Sony Camera 1 (Origional & Raw)"
BASELINE_FILENAME = "DSC00065.JPG"
CAMERA_BASELINE_EXPECTED_MINUTE = dt.datetime(2014, 1, 1, 0, 6, 0)
ACTUAL_BASELINE = dt.datetime(2024, 8, 5, 21, 45, 8)
EVENT_START = dt.datetime(2024, 8, 2, 0, 0, 0)
EVENT_END = dt.datetime(2024, 8, 7, 23, 59, 59)
REPORT_DIR = Path("Project365Canonical/reports")
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".arw", ".raw"}

CAMERA_CLOCK_TAGS = [
    "ExifIFD:DateTimeOriginal",
    "EXIF:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "EXIF:CreateDate",
    "IFD0:ModifyDate",
    "EXIF:ModifyDate",
    "XMP-exif:DateTimeOriginal",
    "XMP-exif:DateTimeDigitized",
    "XMP-xmp:CreateDate",
]

WRITE_TAGS = [
    "AllDates",
    "XMP-exif:DateTimeOriginal",
    "XMP-exif:DateTimeDigitized",
    "XMP-xmp:CreateDate",
    "XMP-xmp:ModifyDate",
    "XMP-xmp:MetadataDate",
    "XMP-photoshop:DateCreated",
    "IPTC:DateCreated",
    "IPTC:TimeCreated",
    "FileCreateDate",
    "FileModifyDate",
]


@dataclass
class PlannedRepair:
    path: Path
    camera_timestamp: dt.datetime | None = None
    repaired_timestamp: dt.datetime | None = None
    action: str = "skip"
    reasons: list[str] = field(default_factory=list)

    def to_report_row(self, root: Path) -> dict[str, object]:
        return {
            "relative_path": str(self.path.relative_to(root)),
            "action": self.action,
            "reasons": self.reasons,
            "camera_timestamp": self.camera_timestamp.isoformat(sep=" ") if self.camera_timestamp else "",
            "repaired_timestamp": self.repaired_timestamp.isoformat(sep=" ") if self.repaired_timestamp else "",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite timestamps in-place.")
    parser.add_argument("--report", help="Write JSON report to this path.")
    args = parser.parse_args()

    root = infer_target_root()
    target = root / "broken" / BROKEN_FOLDER_NAME
    if not target.exists():
        raise SystemExit("Sony Camera 1 broken folder was not found.")

    files = collect_files(target)
    rows = read_time_metadata(files)
    camera_baseline = baseline_camera_timestamp(files, rows)
    repairs = [plan_repair(path, rows.get(path.resolve(), {}), camera_baseline) for path in files]
    validate_baseline(repairs, camera_baseline)
    if args.apply:
        apply_repairs(repairs)

    report_path = Path(args.report) if args.report else default_report_path(args.apply)
    write_report(report_path, target, repairs, applied=args.apply)
    print_summary(report_path, files, repairs, applied=args.apply)
    return 0


def collect_files(target: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, _, filenames in os.walk(target):
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(path.resolve())
    return sorted(files)


def read_time_metadata(paths: list[Path]) -> dict[Path, dict[str, object]]:
    if not paths:
        return {}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as argfile:
        argfile_path = Path(argfile.name)
        argfile.write("-json\n")
        argfile.write("-G1\n")
        argfile.write("-a\n")
        for tag in CAMERA_CLOCK_TAGS:
            argfile.write(f"-{tag}\n")
        for path in paths:
            argfile.write(f"{path}\n")
    try:
        result = subprocess.run(
            ["exiftool", "-@", str(argfile_path)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    finally:
        argfile_path.unlink(missing_ok=True)
    rows = json.loads(result.stdout or "[]")
    return {Path(row["SourceFile"]).resolve(): row for row in rows if row.get("SourceFile")}


def plan_repair(path: Path, row: dict[str, object], camera_baseline: dt.datetime) -> PlannedRepair:
    repair = PlannedRepair(path=path)
    camera_timestamp = best_camera_timestamp(row)
    if camera_timestamp is None:
        repair.action = "skip"
        repair.reasons.append("no_parseable_camera_timestamp")
        return repair
    repaired_timestamp = ACTUAL_BASELINE + (camera_timestamp - camera_baseline)
    repair.camera_timestamp = camera_timestamp
    repair.repaired_timestamp = repaired_timestamp
    if not (EVENT_START <= repaired_timestamp <= EVENT_END):
        repair.action = "skip"
        repair.reasons.append("computed_timestamp_outside_event_range")
        return repair
    repair.action = "repair"
    return repair


def best_camera_timestamp(row: dict[str, object]) -> dt.datetime | None:
    for tag in CAMERA_CLOCK_TAGS:
        parsed = parse_metadata_datetime(row.get(tag))
        if parsed is not None:
            return parsed
    return None


def parse_metadata_datetime(value: object) -> dt.datetime | None:
    if isinstance(value, list):
        value = value[0] if value else ""
    match = re.search(
        r"(19\d{2}|20\d{2})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
        str(value or ""),
    )
    if not match:
        return None
    try:
        return dt.datetime(*map(int, match.groups()))
    except ValueError:
        return None


def baseline_camera_timestamp(files: list[Path], rows: dict[Path, dict[str, object]]) -> dt.datetime:
    baseline_paths = [path for path in files if path.name.upper() == BASELINE_FILENAME.upper()]
    if len(baseline_paths) != 1:
        raise SystemExit(f"Expected one {BASELINE_FILENAME} baseline file, found {len(baseline_paths)}.")
    camera_timestamp = best_camera_timestamp(rows.get(baseline_paths[0].resolve(), {}))
    if camera_timestamp is None:
        raise SystemExit("Baseline file did not have a parseable reset-clock timestamp.")
    if camera_timestamp.replace(second=0, microsecond=0) != CAMERA_BASELINE_EXPECTED_MINUTE:
        raise SystemExit("Baseline file did not have a reset-clock timestamp in the expected minute.")
    return camera_timestamp


def validate_baseline(repairs: list[PlannedRepair], camera_baseline: dt.datetime) -> None:
    baseline = [repair for repair in repairs if repair.path.name.upper() == BASELINE_FILENAME.upper()]
    if len(baseline) != 1:
        raise SystemExit(f"Expected one {BASELINE_FILENAME} baseline file, found {len(baseline)}.")
    repair = baseline[0]
    if repair.camera_timestamp != camera_baseline:
        raise SystemExit("Baseline file did not use the selected reset-clock timestamp.")
    if repair.repaired_timestamp != ACTUAL_BASELINE:
        raise SystemExit("Baseline file did not map to the expected actual timestamp.")


def apply_repairs(repairs: list[PlannedRepair]) -> None:
    for repair in repairs:
        if repair.action != "repair" or repair.repaired_timestamp is None:
            continue
        rewrite_timestamps(repair.path, repair.repaired_timestamp)


def rewrite_timestamps(path: Path, timestamp: dt.datetime) -> None:
    exif_timestamp = timestamp.strftime("%Y:%m:%d %H:%M:%S")
    iptc_date = timestamp.strftime("%Y:%m:%d")
    iptc_time = timestamp.strftime("%H:%M:%S")
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            f"-AllDates={exif_timestamp}",
            f"-XMP-exif:DateTimeOriginal={exif_timestamp}",
            f"-XMP-exif:DateTimeDigitized={exif_timestamp}",
            f"-XMP-xmp:CreateDate={exif_timestamp}",
            f"-XMP-xmp:ModifyDate={exif_timestamp}",
            f"-XMP-xmp:MetadataDate={exif_timestamp}",
            f"-XMP-photoshop:DateCreated={exif_timestamp}",
            f"-IPTC:DateCreated={iptc_date}",
            f"-IPTC:TimeCreated={iptc_time}",
            f"-FileCreateDate={exif_timestamp}",
            f"-FileModifyDate={exif_timestamp}",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    setfile_timestamp = timestamp.strftime("%m/%d/%Y %H:%M:%S")
    subprocess.run(
        ["SetFile", "-d", setfile_timestamp, "-m", setfile_timestamp, str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def default_report_path(applied: bool) -> Path:
    suffix = "applied" if applied else "dry_run"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPORT_DIR / f"sony_camera_1_offset_repair_{suffix}_{stamp}.json"


def write_report(report_path: Path, target: Path, repairs: list[PlannedRepair], applied: bool) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    camera_timestamp_used = next(
        repair.camera_timestamp
        for repair in repairs
        if repair.path.name.upper() == BASELINE_FILENAME.upper() and repair.camera_timestamp is not None
    )
    payload = {
        "applied": applied,
        "target_folder": str(target),
        "baseline": {
            "filename": BASELINE_FILENAME,
            "camera_timestamp_expected_minute": CAMERA_BASELINE_EXPECTED_MINUTE.isoformat(sep=" "),
            "camera_timestamp_used": camera_timestamp_used.isoformat(sep=" "),
            "actual_timestamp": ACTUAL_BASELINE.isoformat(sep=" "),
            "offset_seconds": int((ACTUAL_BASELINE - camera_timestamp_used).total_seconds()),
        },
        "summary": summary_counts(repairs),
        "files": [repair.to_report_row(target) for repair in repairs],
    }
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def summary_counts(repairs: list[PlannedRepair]) -> dict[str, int]:
    counts = {"total": len(repairs), "repair": 0, "skip": 0}
    for repair in repairs:
        counts[repair.action] = counts.get(repair.action, 0) + 1
    return counts


def print_summary(report_path: Path, files: list[Path], repairs: list[PlannedRepair], applied: bool) -> None:
    counts = summary_counts(repairs)
    skipped_out_of_range = sum(1 for repair in repairs if "computed_timestamp_outside_event_range" in repair.reasons)
    skipped_missing_timestamp = sum(1 for repair in repairs if "no_parseable_camera_timestamp" in repair.reasons)
    print("Project365 Sony Camera 1 offset timestamp repair")
    print(f"Mode: {'APPLY' if applied else 'DRY-RUN'}")
    print(f"Total supported files scanned: {len(files)}")
    print(f"Repairable files: {counts.get('repair', 0)}")
    print(f"Skipped files: {counts.get('skip', 0)}")
    print(f"Skipped missing camera timestamp: {skipped_missing_timestamp}")
    print(f"Skipped computed outside event range: {skipped_out_of_range}")
    print(f"Baseline maps correctly: yes")
    print(f"Local report written: {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
