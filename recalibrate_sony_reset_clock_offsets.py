#!/usr/bin/env python3
"""Recalculate Sony reset-clock folders from a corrected baseline."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path("/Users/mikerogero/Programming/Project365 Migration ")
REPORT_DIR = PROJECT_ROOT / "Project365Canonical" / "reports"
NEW_ACTUAL_BASELINE = dt.datetime(2024, 8, 5, 22, 55, 8)
CAMERA_1_BASELINE_REPORT = REPORT_DIR / "sony_camera_1_offset_repair_applied_20260913_192615.json"
CAMERA_2_BASELINE_REPORT = REPORT_DIR / "sony_camera_2_offset_repair_applied_20260913_194124.json"
ROOT = Path(
    "/Volumes/SanDisk Extreme 8T/Photos/Photos - Travels Places Events & Homes/"
    "Taiwan - 2024-08 Jade in Taiwan Wulai Yilan Tainan (video + nude shoots)"
)
TARGETS = [
    ("sony_camera_1_broken_jpg", ROOT / "broken" / "Sony Camera 1 (Origional & Raw)", CAMERA_1_BASELINE_REPORT, {".jpg", ".jpeg"}),
    ("sony_camera_2_broken_jpg", ROOT / "broken" / "Sony Camera 2 (Origional & Raw)", CAMERA_2_BASELINE_REPORT, {".jpg", ".jpeg"}),
    ("sony_camera_1_original_arw", ROOT / "Sony Camera 1 (Origional & Raw)", None, {".arw"}),
]
EVENT_START = dt.datetime(2024, 8, 2, 0, 0, 0)
EVENT_END = dt.datetime(2024, 8, 7, 23, 59, 59)
LOCAL_ZONE = ZoneInfo("Asia/Taipei")

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
VERIFY_TAGS = [
    "MacOS:FileCreateDate",
    "System:FileModifyDate",
    "ExifIFD:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "IFD0:ModifyDate",
    "XMP-exif:DateTimeOriginal",
    "XMP-exif:DateTimeDigitized",
    "XMP-xmp:CreateDate",
    "XMP-xmp:ModifyDate",
    "XMP-xmp:MetadataDate",
    "XMP-photoshop:DateCreated",
]


@dataclass(frozen=True)
class Baseline:
    camera_timestamp: dt.datetime
    previous_actual_timestamp: dt.datetime
    corrected_actual_timestamp: dt.datetime
    source_report: str

    @property
    def offset(self) -> dt.timedelta:
        return self.corrected_actual_timestamp - self.camera_timestamp

    @property
    def correction_delta(self) -> dt.timedelta:
        return self.corrected_actual_timestamp - self.previous_actual_timestamp


@dataclass
class PlannedRepair:
    target_id: str
    target_folder: Path
    path: Path
    camera_timestamp: dt.datetime | None = None
    repaired_timestamp: dt.datetime | None = None
    date_added_utc: dt.datetime | None = None
    action: str = "skip"
    reasons: list[str] = field(default_factory=list)

    def to_report_row(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "relative_path": str(self.path.relative_to(self.target_folder)),
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
    plans: list[PlannedRepair] = []
    target_summaries: dict[str, dict[str, object]] = {}
    for target_id, folder, source_report, extensions in TARGETS:
        if not folder.exists():
            raise SystemExit(f"Missing target folder for {target_id}.")
        files = collect_files(folder, extensions)
        camera_times = camera_times_from_report(folder, source_report) if source_report else camera_times_from_metadata(files)
        target_plans = [plan_file(target_id, folder, path, camera_times.get(path.resolve()), baseline) for path in files]
        validate_target(target_id, files, target_plans)
        plans.extend(target_plans)
        target_summaries[target_id] = {
            "folder": str(folder),
            "supported_extensions": sorted(extensions),
            "file_count": len(files),
            "source_camera_time_report": str(source_report) if source_report else "",
        }

    if args.apply:
        apply_repairs(plans)

    report_path = Path(args.report) if args.report else default_report_path(args.apply)
    write_report(report_path, baseline, target_summaries, plans, applied=args.apply)
    print_summary(report_path, baseline, plans, applied=args.apply)
    return 0


def load_camera_1_baseline() -> Baseline:
    payload = json.loads(CAMERA_1_BASELINE_REPORT.read_text(encoding="utf-8"))
    baseline = payload["baseline"]
    return Baseline(
        camera_timestamp=dt.datetime.fromisoformat(baseline["camera_timestamp_used"]),
        previous_actual_timestamp=dt.datetime.fromisoformat(baseline["actual_timestamp"]),
        corrected_actual_timestamp=NEW_ACTUAL_BASELINE,
        source_report=str(CAMERA_1_BASELINE_REPORT),
    )


def collect_files(folder: Path, extensions: set[str]) -> list[Path]:
    files: list[Path] = []
    for dirpath, _, filenames in os.walk(folder):
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in extensions:
                files.append(path.resolve())
    return sorted(files)


def camera_times_from_report(folder: Path, report_path: Path) -> dict[Path, dt.datetime]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    source_folder = Path(payload["target_folder"])
    result: dict[Path, dt.datetime] = {}
    for row in payload["files"]:
        camera_text = row.get("camera_timestamp", "")
        if not camera_text:
            continue
        result[(folder / Path(row["relative_path"])).resolve()] = dt.datetime.fromisoformat(camera_text)
    if not result:
        raise SystemExit(f"No camera timestamps found in report for {folder.name}.")
    if source_folder.name != folder.name:
        raise SystemExit(f"Report folder mismatch for {folder.name}.")
    return result


def camera_times_from_metadata(files: list[Path]) -> dict[Path, dt.datetime]:
    rows = read_time_metadata(files)
    result: dict[Path, dt.datetime] = {}
    for path, row in rows.items():
        timestamp = best_camera_timestamp(row)
        if timestamp:
            result[path.resolve()] = timestamp
    return result


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


def plan_file(target_id: str, folder: Path, path: Path, camera_timestamp: dt.datetime | None, baseline: Baseline) -> PlannedRepair:
    repair = PlannedRepair(target_id=target_id, target_folder=folder, path=path)
    if camera_timestamp is None:
        repair.reasons.append("missing_camera_timestamp")
        return repair
    repaired_timestamp = camera_timestamp + baseline.offset
    repair.camera_timestamp = camera_timestamp
    repair.repaired_timestamp = repaired_timestamp
    repair.date_added_utc = local_to_utc_naive(repaired_timestamp)
    if not (EVENT_START <= repaired_timestamp <= EVENT_END):
        repair.reasons.append("computed_timestamp_outside_event_range")
        return repair
    repair.action = "repair"
    return repair


def validate_target(target_id: str, files: list[Path], plans: list[PlannedRepair]) -> None:
    if len(files) != len(plans):
        raise SystemExit(f"Planning mismatch for {target_id}.")
    skipped = [plan for plan in plans if plan.action != "repair"]
    if skipped:
        raise SystemExit(f"{target_id} has {len(skipped)} unsafe skipped files; refusing automatic recalibration.")


def apply_repairs(plans: list[PlannedRepair]) -> None:
    for plan in plans:
        if plan.action != "repair" or plan.repaired_timestamp is None:
            continue
        rewrite_timestamps(plan.path, plan.repaired_timestamp)


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
    return REPORT_DIR / f"sony_reset_clock_recalibration_{suffix}_{stamp}.json"


def write_report(
    report_path: Path,
    baseline: Baseline,
    target_summaries: dict[str, dict[str, object]],
    plans: list[PlannedRepair],
    applied: bool,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "applied": applied,
        "baseline": {
            "camera_timestamp": baseline.camera_timestamp.isoformat(sep=" "),
            "previous_actual_timestamp": baseline.previous_actual_timestamp.isoformat(sep=" "),
            "corrected_actual_timestamp": baseline.corrected_actual_timestamp.isoformat(sep=" "),
            "previous_offset_seconds": int((baseline.previous_actual_timestamp - baseline.camera_timestamp).total_seconds()),
            "corrected_offset_seconds": int(baseline.offset.total_seconds()),
            "correction_delta_seconds": int(baseline.correction_delta.total_seconds()),
            "source_report": baseline.source_report,
            "timezone_assumption": "Asia/Taipei",
        },
        "targets": target_summaries,
        "summary": summary_counts(plans),
        "files": [plan.to_report_row() for plan in plans],
    }
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def summary_counts(plans: list[PlannedRepair]) -> dict[str, object]:
    by_target: dict[str, dict[str, int]] = {}
    for plan in plans:
        counts = by_target.setdefault(plan.target_id, {"total": 0, "repair": 0, "skip": 0})
        counts["total"] += 1
        counts[plan.action] = counts.get(plan.action, 0) + 1
    return {
        "total": len(plans),
        "repair": sum(1 for plan in plans if plan.action == "repair"),
        "skip": sum(1 for plan in plans if plan.action != "repair"),
        "by_target": by_target,
    }


def print_summary(report_path: Path, baseline: Baseline, plans: list[PlannedRepair], applied: bool) -> None:
    summary = summary_counts(plans)
    print("Project365 Sony reset-clock recalibration")
    print(f"Mode: {'APPLY' if applied else 'DRY-RUN'}")
    print(f"Previous actual baseline: {baseline.previous_actual_timestamp.isoformat(sep=' ')}")
    print(f"Corrected actual baseline: {baseline.corrected_actual_timestamp.isoformat(sep=' ')}")
    print(f"Correction delta seconds: {int(baseline.correction_delta.total_seconds())}")
    print(f"Corrected offset seconds: {int(baseline.offset.total_seconds())}")
    print(f"Total supported files planned: {summary['total']}")
    print(f"Repairable files: {summary['repair']}")
    print(f"Skipped files: {summary['skip']}")
    for target_id, counts in sorted(summary["by_target"].items()):
        print(f"{target_id}: total={counts['total']} repair={counts.get('repair', 0)} skip={counts.get('skip', 0)}")
    print(f"Local report written: {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
