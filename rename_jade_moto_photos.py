#!/usr/bin/env python3
"""Add Jade suffix to Motorola moto G Power (2021) photos in the Taiwan folder."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from project365_original_matcher import IMAGE_EXTENSIONS
from repair_taiwan_2024_metadata import infer_target_root


JADE_SUFFIX = " (Jade's Photo)"
DEVICE_MODEL = "moto g power (2021)"
DEVICE_MAKE = "motorola"
QUARANTINE_DIR = "duplicate_unsuffixed_moto_g_power"
EXCLUDED_DIRS = {"broken", QUARANTINE_DIR}
REPORT_DIR = Path("Project365Canonical/reports")


@dataclass(frozen=True)
class RenamePlan:
    source: Path
    destination: Path
    action: str
    reason: str

    def to_report_row(self, root: Path) -> dict[str, str]:
        return {
            "source_relative_path": str(self.source.relative_to(root)),
            "destination_relative_path": str(self.destination.relative_to(root)),
            "action": self.action,
            "reason": self.reason,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply renames and duplicate quarantine moves.")
    parser.add_argument("--report", help="Write JSON report to this path.")
    args = parser.parse_args()

    root = infer_target_root()
    files = collect_media_files(root)
    model_rows = read_model_metadata(files)
    plans = build_plans(root, model_rows)
    if args.apply:
        apply_plans(plans)
    report_path = Path(args.report) if args.report else default_report_path(args.apply)
    write_report(report_path, root, files, model_rows, plans, applied=args.apply)
    print_summary(report_path, files, model_rows, plans, applied=args.apply)
    return 0


def collect_media_files(root: Path) -> list[Path]:
    suffixes = {suffix.lower() for suffix in IMAGE_EXTENSIONS}
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in suffixes:
                files.append(path.resolve())
    return sorted(files)


def read_model_metadata(paths: list[Path]) -> dict[Path, dict[str, object]]:
    if not paths:
        return {}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as argfile:
        argfile_path = Path(argfile.name)
        argfile.write("-json\n")
        argfile.write("-G1\n")
        argfile.write("-a\n")
        for tag in (
            "IFD0:Make",
            "IFD0:Model",
            "EXIF:Model",
            "XMP-tiff:Model",
            "ExifIFD:LensModel",
            "XMP-aux:Lens",
            "XMP-exifEX:LensModel",
        ):
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
    return {
        Path(row["SourceFile"]).resolve(): row
        for row in rows
        if row.get("SourceFile")
    }


def build_plans(root: Path, model_rows: dict[Path, dict[str, object]]) -> list[RenamePlan]:
    plans: list[RenamePlan] = []
    planned_destinations: set[Path] = set()
    for path, row in sorted(model_rows.items()):
        if not is_jade_device(row) or has_jade_suffix(path):
            continue
        direct_destination = path.with_name(f"{path.stem}{JADE_SUFFIX}{path.suffix}")
        if direct_destination.exists():
            if same_file_content(path, direct_destination):
                destination = quarantine_destination(root, path)
                action = "quarantine_identical_unsuffixed_duplicate"
                reason = "target_suffixed_file_exists_with_same_content"
            else:
                destination = collision_destination(direct_destination, planned_destinations)
                action = "rename_with_collision_suffix"
                reason = "target_suffixed_file_exists_with_different_content"
        else:
            destination = direct_destination
            action = "rename"
            reason = "missing_jade_suffix"
        planned_destinations.add(destination)
        plans.append(RenamePlan(source=path, destination=destination, action=action, reason=reason))
    return plans


def is_jade_device(row: dict[str, object]) -> bool:
    make_values = normalized_values(row, ("IFD0:Make",))
    model_values = normalized_values(row, ("IFD0:Model", "EXIF:Model", "XMP-tiff:Model"))
    lens_values = normalized_values(row, ("ExifIFD:LensModel", "XMP-aux:Lens", "XMP-exifEX:LensModel"))
    if DEVICE_MODEL in model_values:
        return True
    if DEVICE_MAKE in make_values and any(DEVICE_MODEL in value for value in lens_values):
        return True
    return False


def normalized_values(row: dict[str, object], keys: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            candidates = value
        elif value:
            candidates = [value]
        else:
            candidates = []
        values.update(str(candidate).strip().lower() for candidate in candidates if str(candidate).strip())
    return values


def has_jade_suffix(path: Path) -> bool:
    return path.stem.endswith(JADE_SUFFIX)


def same_file_content(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    return sha256(left) == sha256(right)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quarantine_destination(root: Path, source: Path) -> Path:
    destination = root / QUARANTINE_DIR / source.relative_to(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return collision_path(destination)


def collision_destination(direct_destination: Path, planned_destinations: set[Path]) -> Path:
    base = direct_destination.stem
    if base.endswith(JADE_SUFFIX):
        base = base[: -len(JADE_SUFFIX)]
    for index in range(2, 10_000):
        candidate = direct_destination.with_name(f"{base} {index}{JADE_SUFFIX}{direct_destination.suffix}")
        if not candidate.exists() and candidate not in planned_destinations:
            return candidate
    raise RuntimeError(f"Could not find collision-free Jade filename for {direct_destination.name}")


def collision_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} {index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find collision-free quarantine path for {path.name}")


def apply_plans(plans: list[RenamePlan]) -> None:
    for plan in plans:
        plan.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plan.source), str(plan.destination))


def default_report_path(applied: bool) -> Path:
    suffix = "applied" if applied else "dry_run"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPORT_DIR / f"taiwan_2024_jade_moto_rename_{suffix}_{stamp}.json"


def write_report(
    report_path: Path,
    root: Path,
    files: list[Path],
    model_rows: dict[Path, dict[str, object]],
    plans: list[RenamePlan],
    applied: bool,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    device_count = sum(1 for row in model_rows.values() if is_jade_device(row))
    suffixed_count = sum(1 for path, row in model_rows.items() if is_jade_device(row) and has_jade_suffix(path))
    action_counts = Counter(plan.action for plan in plans)
    payload = {
        "applied": applied,
        "root": str(root),
        "summary": {
            "total_images_scanned": len(files),
            "jade_device_images": device_count,
            "jade_device_already_suffixed": suffixed_count,
            "planned_changes": len(plans),
            "actions": dict(sorted(action_counts.items())),
        },
        "files": [plan.to_report_row(root) for plan in plans],
    }
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def print_summary(report_path: Path, files: list[Path], model_rows: dict[Path, dict[str, object]], plans: list[RenamePlan], applied: bool) -> None:
    device_count = sum(1 for row in model_rows.values() if is_jade_device(row))
    suffixed_count = sum(1 for path, row in model_rows.items() if is_jade_device(row) and has_jade_suffix(path))
    action_counts = Counter(plan.action for plan in plans)
    print("Project365 Taiwan 2024 Jade Moto rename")
    print(f"Mode: {'APPLY' if applied else 'DRY-RUN'}")
    print(f"Total image files scanned: {len(files)}")
    print(f"Jade device images: {device_count}")
    print(f"Already suffixed Jade device images: {suffixed_count}")
    print(f"Planned changes: {len(plans)}")
    for action, count in sorted(action_counts.items()):
        print(f"{action}: {count}")
    print(f"Local report written: {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
