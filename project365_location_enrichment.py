#!/usr/bin/env python3
"""Local location enrichment helpers for Project365 canonical entries."""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path


EXPORTABLE_LOCATION_STATUSES = {"confirmed", "reviewed"}
EXPORTABLE_REVIEW_STATUSES = {"confirmed", "reviewed"}


@dataclass(frozen=True)
class LocationEnrichmentSummary:
    queue_path: str
    applied_count: int
    queue_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local Project365 location enrichment.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--review-csv", help="Optional reviewed location CSV to apply.")
    parser.add_argument(
        "--queue-out",
        default="Project365Canonical/exports/verification_reports/location_review_queue.csv",
        help="Metadata-only location review queue CSV output.",
    )
    args = parser.parse_args()

    summary = run_location_enrichment(
        canonical_root=Path(args.canonical_root),
        queue_path=Path(args.queue_out),
        review_csv=Path(args.review_csv) if args.review_csv else None,
    )
    print("Project365 location enrichment: PASS")
    print(f"Queue: {summary.queue_path}")
    print(f"Applied locations: {summary.applied_count}")
    print(f"Queue rows: {summary.queue_count}")
    return 0


def run_location_enrichment(
    canonical_root: Path,
    queue_path: Path,
    review_csv: Path | None = None,
) -> LocationEnrichmentSummary:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    applied = 0
    connection = sqlite3.connect(db_path)
    try:
        if review_csv is not None:
            applied = apply_review_csv(connection, review_csv)
        queue_rows = review_queue_rows(connection)
        connection.commit()
    finally:
        connection.close()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    _write_queue(queue_path, queue_rows)
    return LocationEnrichmentSummary(
        queue_path=str(queue_path),
        applied_count=applied,
        queue_count=len(queue_rows),
    )


def apply_review_csv(connection: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        raise FileNotFoundError(f"Missing review CSV: {path}")
    applied = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            entry_id = row.get("entry_id", "").strip()
            location_status = row.get("location_status", "reviewed").strip().lower()
            source = row.get("source", "manual_review").strip() or "manual_review"
            label = row.get("label", "").strip() or "unlabeled"
            confidence = row.get("confidence", "manual").strip() or "manual"
            review_status = row.get("review_status", "reviewed").strip().lower()
            latitude = _float(row.get("latitude", ""))
            longitude = _float(row.get("longitude", ""))
            if not entry_id:
                raise ValueError("Review CSV rows require entry_id")
            if location_status not in {"confirmed", "reviewed", "inferred", "rejected", "unknown"}:
                raise ValueError(f"Unsupported location_status: {location_status}")
            if review_status not in {"suggested", "reviewed", "confirmed", "rejected"}:
                raise ValueError(f"Unsupported review_status: {review_status}")
            applied += upsert_location(
                connection,
                entry_id,
                location_status,
                source,
                latitude,
                longitude,
                label,
                confidence,
                review_status,
            )
    return applied


def upsert_location(
    connection: sqlite3.Connection,
    entry_id: str,
    location_status: str,
    source: str,
    latitude: float | None,
    longitude: float | None,
    label: str,
    confidence: str,
    review_status: str,
) -> int:
    if location_status in EXPORTABLE_LOCATION_STATUSES and (latitude is None or longitude is None):
        raise ValueError("Confirmed/reviewed locations require latitude and longitude")
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO locations (
            entry_id, location_status, source, latitude, longitude, label, confidence, review_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entry_id, location_status, source, label)
        DO UPDATE SET
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            confidence = excluded.confidence,
            review_status = excluded.review_status
        """,
        (
            entry_id,
            location_status,
            source,
            latitude,
            longitude,
            label,
            confidence,
            review_status,
        ),
    )
    return connection.total_changes - before


def load_exportable_locations(db_path: Path, entry_ids: list[str]) -> dict[str, dict[str, float]]:
    if not entry_ids:
        return {}
    placeholders = ",".join("?" for _ in entry_ids)
    output: dict[str, dict[str, float]] = {}
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = list(
            connection.execute(
                f"""
                SELECT entry_id, latitude, longitude, location_status, source, review_status
                FROM locations
                WHERE entry_id IN ({placeholders})
                    AND location_status IN ('confirmed', 'reviewed')
                    AND review_status IN ('confirmed', 'reviewed')
                    AND latitude IS NOT NULL
                    AND longitude IS NOT NULL
                ORDER BY
                    CASE location_status WHEN 'confirmed' THEN 0 ELSE 1 END,
                    CASE source WHEN 'exif' THEN 0 WHEN 'swarm' THEN 1 WHEN 'diarium_export' THEN 2 ELSE 3 END,
                    source
                """,
                entry_ids,
            )
        )
    finally:
        connection.close()
    by_entry: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_entry.setdefault(row["entry_id"], []).append(row)
    for entry_id, candidates in by_entry.items():
        first = candidates[0]
        if any(
            not _same_location(first["latitude"], first["longitude"], candidate["latitude"], candidate["longitude"])
            for candidate in candidates[1:]
        ):
            continue
        output[entry_id] = {
            "latitude": first["latitude"],
            "longitude": first["longitude"],
        }
    return output


def review_queue_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    connection.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT
                entries.id AS entry_id,
                entries.entry_date,
                CASE WHEN entries.original_text IS NULL THEN 0 ELSE 1 END AS has_text,
                COUNT(DISTINCT media_assets.id) AS media_count,
                COUNT(DISTINCT locations.id) AS location_count,
                SUM(
                    CASE
                        WHEN locations.location_status IN ('confirmed', 'reviewed')
                            AND locations.review_status IN ('confirmed', 'reviewed')
                            AND locations.latitude IS NOT NULL
                            AND locations.longitude IS NOT NULL
                        THEN 1
                        ELSE 0
                    END
                ) AS exportable_location_count
            FROM entries
            LEFT JOIN media_assets
                ON media_assets.entry_id = entries.id
                AND media_assets.selected_default = 1
            LEFT JOIN locations
                ON locations.entry_id = entries.id
            GROUP BY entries.id, entries.entry_date, entries.original_text
            HAVING exportable_location_count IS NULL OR exportable_location_count = 0
            ORDER BY entries.entry_date, entries.id
            """
        )
    ]


def _write_queue(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "entry_id",
                "entry_date",
                "has_text",
                "media_count",
                "location_count",
                "exportable_location_count",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _same_location(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> bool:
    return math.isclose(latitude_a, latitude_b, abs_tol=0.000001) and math.isclose(
        longitude_a,
        longitude_b,
        abs_tol=0.000001,
    )


def _float(value: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main())
