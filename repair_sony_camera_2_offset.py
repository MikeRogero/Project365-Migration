#!/usr/bin/env python3
"""Repair Sony Camera 2 reset-clock timestamps from the Sony Camera 1 baseline."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from repair_taiwan_2024_metadata import infer_target_root


BROKEN_FOLDER_NAME = "Sony Camera 2 (Origional & Raw)"
ANCHOR_FILENAME = "DSC00001.JPG"
ANCHOR_EXPECTED_CAMERA_MINUTE = dt.datetime(2014, 1, 2, 15, 35, 0)
EVENT_START = dt.datetime(2024, 8, 2, 0, 0, 0)
EVENT_END = dt.datetime(2024, 8, 7, 23, 59, 59)
LOCAL_ZONE = ZoneInfo("Asia/Taipei")
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


@dataclass(frozen=True)
class Baseline:
    camera_timestamp: dt.datetime
    actual_timestamp: dt.datetime
    source_report: str

    @property
    def offset(self) -> dt.timedelta:
        return self.actual_timestamp - self.camera_timestamp


@dataclass
class PlannedRepair:
    path: Path
    camera_timestamp: dt.datetime | None = None
    repaired_timestamp: dt.datetime | None = None
    date_added_utc: dt.datetime | None = None
    action: str = "skip"
    reasons: list[str] = field(default_factory=list)

    def to_report_row(self, root: Path) -> dict[str, object]:
        return {
            "relative_path": str(self.path.relative_to(root)),
            "action": self.action,
            "reasons": self.reasons,
            "camera_timestamp": self.camera_timestamp.isoformat(sep=" ") if self.camera_timestamp else "",
            "repaired_timestamp_local": self.repaired_timestamp.isoformat(sep=" ") if self.repaired_timestamp else "",
            "date_added_utc": self.date_added_utc.isoformat(sep=" ") if self.date_added_utc else "",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite timestamps in-place.")
    parser.add_argument("--report", help="Write JSON report to this path.")
    args = parser.parse_args()

    baseline = load_camera_1_baseline()
    root = infer_target_root()
    target = root / "broken" / BROKEN_FOLDER_NAME
    if not target.exists():
        raise SystemExit("Sony Camera 2 broken folder was not found.")

    files = collect_files(target)
    rows = read_time_metadata(files)
    repairs = [plan_repair(path, rows.get(path.resolve(), {}), baseline) for path in files]
    anchor = validate_anchor(repairs, baseline)
    if args.apply:
        apply_repairs(repairs)

    report_path = Path(args.report) if args.report else default_report_path(args.apply)
    write_report(report_path, target, baseline, anchor, repairs, applied=args.apply)
    print_summary(report_path, files, baseline, anchor, repairs, applied=args.apply)
    return 0


def load_camera_1_baseline() -> Baseline:
    reports = sorted(glob.glob("Project365Canonical/reports/sony_camera_1_offset_repair_applied_*.json"))
    if not reports:
        raise SystemExit("Could not find the applied Sony Camera 1 baseline report.")
    report_path = reports[-1]
    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    baseline = payload.get("baseline", {})
    camera_text = str(baseline.get("camera_timestamp_used", ""))
    actual_text = str(baseline.get("actual_timestamp", ""))
    if not camera_text or not actual_text:
        raise SystemExit("Sony Camera 1 baseline report is missing required timestamps.")
    return Baseline(
        camera_timestamp=dt.datetime.fromisoformat(camera_text),
        actual_timestamp=dt.datetime.fromisoformat(actual_text),
        source_report=report_path,
    )


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


def plan_repair(path: Path, row: dict[str, object], baseline: Baseline) -> PlannedRepair:
    repair = PlannedRepair(path=path)
    camera_timestamp = best_camera_timestamp(row)
    if camera_timestamp is None:
        repair.action = "skip"
        repair.reasons.append("no_parseable_camera_timestamp")
        return repair
    repaired_timestamp = camera_timestamp + baseline.offset
    repair.camera_timestamp = camera_timestamp
    repair.repaired_timestamp = repaired_timestamp
    repair.date_added_utc = local_to_utc_naive(repaired_timestamp)
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


def validate_anchor(repairs: list[PlannedRepair], baseline: Baseline) -> PlannedRepair:
    anchors = [repair for repair in repairs if repair.path.name.upper() == ANCHOR_FILENAME.upper()]
    if len(anchors) != 1:
        raise SystemExit(f"Expected one {ANCHOR_FILENAME} anchor file, found {len(anchors)}.")
    anchor = anchors[0]
    if anchor.camera_timestamp is None:
        raise SystemExit("Sony Camera 2 anchor has no parseable camera timestamp.")
    if anchor.camera_timestamp.replace(second=0, microsecond=0) != ANCHOR_EXPECTED_CAMERA_MINUTE:
        raise SystemExit("Sony Camera 2 anchor did not have a camera timestamp in the expected minute.")
    if anchor.repaired_timestamp != anchor.camera_timestamp + baseline.offset:
        raise SystemExit("Sony Camera 2 anchor did not map through the Sony Camera 1 offset.")
    if not (EVENT_START <= anchor.repaired_timestamp <= EVENT_END):
        raise SystemExit("Sony Camera 2 anchor maps outside the expected event range.")
    return anchor


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
    set_date_added_xattr(path, timestamp)


def set_date_added_xattr(path: Path, timestamp: dt.datetime) -> None:
    date_added_utc = local_to_utc_naive(timestamp)
    plist_hex = plistlib.dumps(date_added_utc, fmt=plistlib.FMT_BINARY).hex()
    subprocess.run(
        ["xattr", "-w", "-x", "com.apple.metadata:kMDItemDateAdded", plist_hex, str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def local_to_utc_naive(timestamp: dt.datetime) -> dt.datetime:
    aware_local = timestamp.replace(tzinfo=LOCAL_ZONE)
    return aware_local.astimezone(dt.timezone.utc).replace(tzinfo=None)


def default_report_path(applied: bool) -> Path:
    suffix = "applied" if applied else "dry_run"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPORT_DIR / f"sony_camera_2_offset_repair_{suffix}_{stamp}.json"


def write_report(
    report_path: Path,
    target: Path,
    baseline: Baseline,
    anchor: PlannedRepair,
    repairs: list[PlannedRepair],
    applied: bool,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "applied": applied,
        "target_folder": str(target),
        "source_baseline": {
            "folder": "Sony Camera 1 (Origional & Raw)",
            "camera_timestamp_used": baseline.camera_timestamp.isoformat(sep=" "),
            "actual_timestamp": baseline.actual_timestamp.isoformat(sep=" "),
            "offset_seconds": int(baseline.offset.total_seconds()),
            "source_report": baseline.source_report,
        },
        "anchor": {
            "filename": ANCHOR_FILENAME,
            "camera_timestamp_expected_minute": ANCHOR_EXPECTED_CAMERA_MINUTE.isoformat(sep=" "),
            "camera_timestamp_used": anchor.camera_timestamp.isoformat(sep=" ") if anchor.camera_timestamp else "",
            "computed_timestamp_local": anchor.repaired_timestamp.isoformat(sep=" ") if anchor.repaired_timestamp else "",
            "date_added_utc": anchor.date_added_utc.isoformat(sep=" ") if anchor.date_added_utc else "",
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


def print_summary(
    report_path: Path,
    files: list[Path],
    baseline: Baseline,
    anchor: PlannedRepair,
    repairs: list[PlannedRepair],
    applied: bool,
) -> None:
    counts = summary_counts(repairs)
    skipped_out_of_range = sum(1 for repair in repairs if "computed_timestamp_outside_event_range" in repair.reasons)
    skipped_missing_timestamp = sum(1 for repair in repairs if "no_parseable_camera_timestamp" in repair.reasons)
    print("Project365 Sony Camera 2 offset timestamp repair")
    print(f"Mode: {'APPLY' if applied else 'DRY-RUN'}")
    print(f"Total supported files scanned: {len(files)}")
    print(f"Repairable files: {counts.get('repair', 0)}")
    print(f"Skipped files: {counts.get('skip', 0)}")
    print(f"Skipped missing camera timestamp: {skipped_missing_timestamp}")
    print(f"Skipped computed outside event range: {skipped_out_of_range}")
    print(f"Source offset seconds: {int(baseline.offset.total_seconds())}")
    print(f"Anchor maps into range: yes")
    print(f"Local report written: {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
