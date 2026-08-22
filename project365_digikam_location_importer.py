#!/usr/bin/env python3
"""Import local digiKam GPS metadata from XMP sidecars into canonical locations."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from project365_location_enrichment import run_location_enrichment, upsert_location


WORKING_COPY_RE = re.compile(r"(?:^|/)project365_(\d{4}-\d{2}-\d{2})\.[^.]+$", re.IGNORECASE)


@dataclass(frozen=True)
class DigiKamLocationImportSummary:
    report_path: str
    location_queue_path: str
    scanned_count: int
    suggested_count: int
    applied_count: int
    skipped_count: int
    error_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Import local digiKam GPS metadata from XMP sidecars.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--xmp-root", action="append", default=[], help="Folder containing digiKam XMP sidecars.")
    parser.add_argument(
        "--report-out",
        default="Project365Canonical/exports/verification_reports/digikam_location_import_report.csv",
    )
    parser.add_argument(
        "--queue-out",
        default="Project365Canonical/exports/verification_reports/location_review_queue.csv",
    )
    args = parser.parse_args()

    summary = import_digikam_locations(
        canonical_root=Path(args.canonical_root),
        xmp_roots=[Path(path) for path in args.xmp_root],
        report_path=Path(args.report_out),
        queue_path=Path(args.queue_out),
    )
    print("Project365 digiKam location import: PASS")
    print(f"Report: {summary.report_path}")
    print(f"Location queue: {summary.location_queue_path}")
    print(f"Scanned files: {summary.scanned_count}")
    print(f"GPS suggestions: {summary.suggested_count}")
    print(f"Applied locations: {summary.applied_count}")
    print(f"Skipped locations: {summary.skipped_count}")
    print(f"Errors: {summary.error_count}")
    return 0


def import_digikam_locations(
    canonical_root: Path,
    xmp_roots: list[Path],
    report_path: Path,
    queue_path: Path,
) -> DigiKamLocationImportSummary:
    if not xmp_roots:
        raise ValueError("Provide --xmp-root")
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    for root in xmp_roots:
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Missing XMP root: {root}")

    suggestions: list[dict[str, object]] = []
    scanned_count = 0
    for root in xmp_roots:
        for path in sorted(root.rglob("*.xmp")):
            scanned_count += 1
            gps = _extract_gps_from_xmp(path)
            if gps is None:
                continue
            latitude, longitude = gps
            suggestions.append(
                {
                    "media_path": str(_media_path_from_sidecar(path)),
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )

    report_rows = []
    applied_count = 0
    skipped_count = 0
    error_count = 0
    with sqlite3.connect(db_path) as connection:
        known_entries = _known_entry_ids(connection)
        for suggestion in suggestions:
            media_path = str(suggestion["media_path"])
            entry_id = _entry_id_from_media_path(media_path)
            latitude = float(suggestion["latitude"])
            longitude = float(suggestion["longitude"])
            status = "skipped"
            reason = ""
            if entry_id is None or entry_id not in known_entries:
                reason = "unknown_working_copy"
                error_count += 1
            else:
                applied = upsert_location(
                    connection,
                    entry_id=entry_id,
                    location_status="reviewed",
                    source="digikam_xmp",
                    latitude=latitude,
                    longitude=longitude,
                    label="digiKam GPS",
                    confidence="metadata",
                    review_status="reviewed",
                )
                if applied:
                    applied_count += applied
                    status = "applied"
                    reason = "reviewed"
                else:
                    skipped_count += 1
                    reason = "existing_location"
            report_rows.append(
                {
                    "media_path": media_path,
                    "entry_id": entry_id or "",
                    "latitude": latitude,
                    "longitude": longitude,
                    "status": status,
                    "reason": reason,
                    "source": "digikam_xmp",
                }
            )
        connection.commit()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(report_path, report_rows)
    location_summary = run_location_enrichment(canonical_root=canonical_root, queue_path=queue_path)
    return DigiKamLocationImportSummary(
        report_path=str(report_path),
        location_queue_path=location_summary.queue_path,
        scanned_count=scanned_count,
        suggested_count=len(suggestions),
        applied_count=applied_count,
        skipped_count=skipped_count,
        error_count=error_count,
    )


def _extract_gps_from_xmp(path: Path) -> tuple[float, float] | None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    latitude = None
    longitude = None
    for element in root.iter():
        for key, value in element.attrib.items():
            local = _local_name(key)
            if local == "GPSLatitude":
                latitude = _parse_gps_coordinate(value, "NS")
            elif local == "GPSLongitude":
                longitude = _parse_gps_coordinate(value, "EW")
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


def _parse_gps_coordinate(value: str, refs: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    ref = text[-1].upper() if text[-1].upper() in refs else ""
    if ref:
        text = text[:-1]
    parts = [part.strip() for part in text.split(",") if part.strip()]
    try:
        if len(parts) >= 2:
            number = float(parts[0]) + (float(parts[1]) / 60.0)
        else:
            number = float(parts[0])
    except (IndexError, ValueError):
        return None
    if ref in {"S", "W"}:
        number *= -1
    return round(number, 8)


def _media_path_from_sidecar(path: Path) -> Path:
    return path.with_suffix("")


def _entry_id_from_media_path(media_path: str) -> str | None:
    match = WORKING_COPY_RE.search(media_path.replace("\\", "/"))
    if not match:
        return None
    return f"project365:{match.group(1)}"


def _known_entry_ids(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT id FROM entries")}


def _write_report(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = ["media_path", "entry_id", "latitude", "longitude", "status", "reason", "source"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


if __name__ == "__main__":
    raise SystemExit(main())
