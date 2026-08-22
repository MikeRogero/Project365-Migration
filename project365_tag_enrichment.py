#!/usr/bin/env python3
"""Local people and tag enrichment helpers for Project365 canonical entries."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


EXPORTABLE_REVIEW_STATUSES = {"confirmed", "reviewed"}


@dataclass(frozen=True)
class TagEnrichmentSummary:
    queue_path: str
    applied_count: int
    queue_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local Project365 tag enrichment.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--review-csv", help="Optional reviewed tag CSV to apply.")
    parser.add_argument(
        "--queue-out",
        default="Project365Canonical/exports/verification_reports/tag_review_queue.csv",
        help="Metadata-only review queue CSV output.",
    )
    args = parser.parse_args()

    summary = run_tag_enrichment(
        canonical_root=Path(args.canonical_root),
        queue_path=Path(args.queue_out),
        review_csv=Path(args.review_csv) if args.review_csv else None,
    )
    print("Project365 tag enrichment: PASS")
    print(f"Queue: {summary.queue_path}")
    print(f"Applied tags/people: {summary.applied_count}")
    print(f"Queue rows: {summary.queue_count}")
    return 0


def run_tag_enrichment(
    canonical_root: Path,
    queue_path: Path,
    review_csv: Path | None = None,
) -> TagEnrichmentSummary:
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
    return TagEnrichmentSummary(
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
            tag_type = row.get("tag_type", "").strip().lower()
            name = _clean_name(row.get("name", ""))
            review_status = row.get("review_status", "reviewed").strip().lower()
            source = row.get("source", "manual_review").strip() or "manual_review"
            if not entry_id or not tag_type or not name:
                raise ValueError("Review CSV rows require entry_id, tag_type, and name")
            if review_status not in {"suggested", "reviewed", "confirmed", "rejected"}:
                raise ValueError(f"Unsupported review_status: {review_status}")
            if tag_type == "person":
                applied += upsert_person(connection, entry_id, name, review_status, source)
            else:
                applied += upsert_tag(connection, entry_id, tag_type, name, review_status, source)
    return applied


def upsert_tag(
    connection: sqlite3.Connection,
    entry_id: str,
    tag_type: str,
    name: str,
    review_status: str,
    source: str,
) -> int:
    canonical_name = _clean_name(name)
    diarium_name = normalize_diarium_tag(tag_type, canonical_name)
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO tags (entry_id, tag_type, canonical_name, diarium_name, review_status, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(entry_id, tag_type, canonical_name)
        DO UPDATE SET
            diarium_name = excluded.diarium_name,
            review_status = excluded.review_status,
            source = excluded.source
        """,
        (entry_id, tag_type, canonical_name, diarium_name, review_status, source),
    )
    return connection.total_changes - before


def upsert_person(
    connection: sqlite3.Connection,
    entry_id: str,
    name: str,
    review_status: str,
    source: str,
) -> int:
    canonical_name = _clean_name(name)
    diarium_tag = normalize_diarium_tag("person", canonical_name)
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO people (entry_id, canonical_name, diarium_tag, review_status, source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(entry_id, canonical_name)
        DO UPDATE SET
            diarium_tag = excluded.diarium_tag,
            review_status = excluded.review_status,
            source = excluded.source
        """,
        (entry_id, canonical_name, diarium_tag, review_status, source),
    )
    return connection.total_changes - before


def load_exportable_diarium_tags(db_path: Path, entry_ids: list[str]) -> dict[str, list[str]]:
    if not entry_ids:
        return {}
    placeholders = ",".join("?" for _ in entry_ids)
    tags_by_entry = {entry_id: [] for entry_id in entry_ids}
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            f"""
            SELECT id, source_app
            FROM entries
            WHERE id IN ({placeholders})
            """,
            entry_ids,
        ):
            tags_by_entry[row["id"]].append(normalize_diarium_tag("source", row["source_app"]))
        for row in connection.execute(
            f"""
            SELECT entry_id, diarium_name
            FROM tags
            WHERE entry_id IN ({placeholders})
                AND review_status IN ('confirmed', 'reviewed')
            ORDER BY tag_type, canonical_name
            """,
            entry_ids,
        ):
            tags_by_entry[row["entry_id"]].append(row["diarium_name"])
        for row in connection.execute(
            f"""
            SELECT entry_id, diarium_tag
            FROM people
            WHERE entry_id IN ({placeholders})
                AND review_status IN ('confirmed', 'reviewed')
            ORDER BY canonical_name
            """,
            entry_ids,
        ):
            tags_by_entry[row["entry_id"]].append(row["diarium_tag"])
        return {entry_id: _dedupe(tags) for entry_id, tags in tags_by_entry.items()}
    finally:
        connection.close()


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
                COUNT(DISTINCT tags.id) AS tag_count,
                COUNT(DISTINCT people.id) AS people_count
            FROM entries
            LEFT JOIN media_assets
                ON media_assets.entry_id = entries.id
                AND media_assets.selected_default = 1
            LEFT JOIN tags
                ON tags.entry_id = entries.id
                AND tags.review_status IN ('confirmed', 'reviewed')
            LEFT JOIN people
                ON people.entry_id = entries.id
                AND people.review_status IN ('confirmed', 'reviewed')
            GROUP BY entries.id, entries.entry_date, entries.original_text
            HAVING tag_count = 0 OR people_count = 0
            ORDER BY entries.entry_date, entries.id
            """
        )
    ]


def normalize_diarium_tag(tag_type: str, name: str) -> str:
    cleaned = _clean_name(name)
    if not cleaned:
        raise ValueError("Tag name cannot be empty")
    if tag_type == "source":
        return f"source:{_slug(cleaned)}"
    if tag_type == "person":
        return f"person:{cleaned}"
    if tag_type == "place":
        return f"place:{cleaned}"
    return f"{_slug(tag_type)}:{cleaned}"


def _write_queue(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "entry_id",
                "entry_date",
                "has_text",
                "media_count",
                "tag_count",
                "people_count",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _dedupe(tags: list[str]) -> list[str]:
    seen = set()
    output = []
    for tag in tags:
        if tag and tag not in seen:
            output.append(tag)
            seen.add(tag)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
