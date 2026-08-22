#!/usr/bin/env python3
"""Reconcile Diarium JSON exports against the Project365 canonical archive."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html.parser
import json
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_TIME_ZONE = "Asia/Taipei"


@dataclass(frozen=True)
class ReconciliationSummary:
    report_path: str
    export_entries: int
    matched_entries: int
    unmatched_entries: int
    missing_expected_entries: int
    applied_updates: int

    @property
    def passed(self) -> bool:
        return self.unmatched_entries == 0 and self.missing_expected_entries == 0


@dataclass(frozen=True)
class ManifestEntry:
    entry_id: str
    entry_date: str
    local_datetime: str


@dataclass(frozen=True)
class DiariumEntry:
    date: str
    html: str
    tags: tuple[str, ...]
    people: tuple[str, ...]
    location: tuple[float, float] | None
    media_count: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile a Diarium JSON/ZIP export against canonical Project365 entries."
    )
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--diarium-export", required=True, help="Diarium JSON or ZIP export path.")
    parser.add_argument(
        "--pilot-manifest",
        required=True,
        help="CSV manifest produced by project365_diarium_exporter.py.",
    )
    parser.add_argument(
        "--report-dir",
        default="Project365Canonical/exports/verification_reports",
        help="Folder for the metadata-safe reconciliation CSV.",
    )
    parser.add_argument(
        "--apply-reviewed",
        action="store_true",
        help="Persist Diarium tags, people, and locations as reviewed canonical updates.",
    )
    parser.add_argument("--time-zone", default=DEFAULT_TIME_ZONE)
    args = parser.parse_args()

    summary = reconcile_diarium_export(
        canonical_root=Path(args.canonical_root),
        diarium_export=Path(args.diarium_export),
        pilot_manifest=Path(args.pilot_manifest),
        report_dir=Path(args.report_dir),
        apply_reviewed=args.apply_reviewed,
        time_zone=args.time_zone,
    )
    status = "PASS" if summary.passed else "FAIL"
    print(f"Project365 Diarium reconciliation: {status}")
    print(f"Report: {summary.report_path}")
    print(f"Export entries: {summary.export_entries}")
    print(f"Matched entries: {summary.matched_entries}")
    print(f"Unmatched entries: {summary.unmatched_entries}")
    print(f"Missing expected entries: {summary.missing_expected_entries}")
    print(f"Applied updates: {summary.applied_updates}")
    return 0 if summary.passed else 1


def reconcile_diarium_export(
    canonical_root: Path,
    diarium_export: Path,
    pilot_manifest: Path,
    report_dir: Path,
    apply_reviewed: bool = False,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> ReconciliationSummary:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    if not diarium_export.exists():
        raise FileNotFoundError(f"Missing Diarium export: {diarium_export}")
    if not pilot_manifest.exists():
        raise FileNotFoundError(f"Missing pilot manifest: {pilot_manifest}")

    manifest_entries = _read_manifest(pilot_manifest, time_zone)
    entries = _read_diarium_export(diarium_export)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{diarium_export.stem}_reconciliation.csv"
    rows: list[dict[str, object]] = []
    matched = 0
    applied_updates = 0
    matched_manifest_ids: set[str] = set()

    by_datetime = {entry.local_datetime: entry for entry in manifest_entries}
    by_date: dict[str, list[ManifestEntry]] = {}
    for entry in manifest_entries:
        by_date.setdefault(entry.entry_date, []).append(entry)

    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        for export_entry in entries:
            manifest_entry = _match_manifest_entry(export_entry, by_datetime, by_date)
            if manifest_entry is None:
                rows.append(_report_row(None, export_entry, "unmatched", "", False))
                continue
            if manifest_entry.entry_id in matched_manifest_ids:
                rows.append(_report_row(manifest_entry, export_entry, "duplicate_export_match", "", False))
                continue

            matched += 1
            matched_manifest_ids.add(manifest_entry.entry_id)
            canonical = connection.execute(
                """
                SELECT id, original_text, corrected_text
                FROM entries
                WHERE id = ?
                """,
                (manifest_entry.entry_id,),
            ).fetchone()
            if canonical is None:
                rows.append(
                    _report_row(
                        manifest_entry,
                        export_entry,
                        "missing_canonical_entry",
                        "",
                        False,
                    )
                )
                continue

            exported_text = _html_to_text(export_entry.html)
            canonical_text = canonical["corrected_text"] or canonical["original_text"] or ""
            text_status = _text_status(canonical_text, exported_text)
            row = _report_row(
                manifest_entry,
                export_entry,
                "matched",
                text_status,
                _has_reviewable_updates(export_entry),
            )
            rows.append(row)

            if apply_reviewed:
                applied_updates += _apply_reviewed_updates(
                    connection,
                    manifest_entry.entry_id,
                    export_entry,
                )
        connection.commit()
    finally:
        connection.close()

    missing_expected = [
        entry for entry in manifest_entries if entry.entry_id not in matched_manifest_ids
    ]
    rows.extend(_missing_expected_report_row(entry) for entry in missing_expected)
    _write_report(report_path, rows)
    return ReconciliationSummary(
        report_path=str(report_path),
        export_entries=len(entries),
        matched_entries=matched,
        unmatched_entries=len(entries) - matched,
        missing_expected_entries=len(missing_expected),
        applied_updates=applied_updates,
    )


def _read_manifest(path: Path, time_zone: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            entry_id = row.get("entry_id", "")
            entry_date = row.get("entry_date", "")
            creation_date = row.get("creation_date_utc", "")
            if not entry_id or not entry_date or not creation_date:
                raise ValueError("Pilot manifest must include entry_id, entry_date, and creation_date_utc")
            entries.append(
                ManifestEntry(
                    entry_id=entry_id,
                    entry_date=entry_date,
                    local_datetime=_utc_to_local_naive(creation_date, time_zone),
                )
            )
    return entries


def _read_diarium_export(path: Path) -> list[DiariumEntry]:
    media_counts: dict[str, int] = {}
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            json_names = [name for name in archive.namelist() if name.lower().endswith(".json")]
            if len(json_names) != 1:
                raise ValueError("Diarium ZIP export must contain exactly one JSON file")
            payload = json.loads(archive.read(json_names[0]).decode("utf-8"))
            media_counts = _media_counts_from_zip(archive.namelist())
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ValueError("Diarium export must be a .json or .zip file")

    if not isinstance(payload, list):
        raise ValueError("Diarium JSON export must be a top-level array")

    entries = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each Diarium export entry must be an object")
        date = str(item.get("date", ""))
        if not date:
            raise ValueError("Each Diarium export entry must include date")
        location = _parse_location(item.get("location"))
        entries.append(
            DiariumEntry(
                date=date,
                html=str(item.get("html", "")),
                tags=tuple(str(tag) for tag in item.get("tags", []) if str(tag)),
                people=tuple(str(person) for person in item.get("people", []) if str(person)),
                location=location,
                media_count=media_counts.get(date, 0),
            )
        )
    return entries


def _media_counts_from_zip(names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in names:
        parts = name.split("/")
        if len(parts) < 3 or parts[0] != "media":
            continue
        local_datetime = _media_folder_to_datetime(parts[1])
        if local_datetime:
            counts[local_datetime] = counts.get(local_datetime, 0) + 1
    return counts


def _media_folder_to_datetime(folder: str) -> str | None:
    try:
        date_part, time_part = folder.split("_", 1)
    except ValueError:
        return None
    if len(time_part) < 6:
        return None
    return f"{date_part}T{time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"


def _match_manifest_entry(
    export_entry: DiariumEntry,
    by_datetime: dict[str, ManifestEntry],
    by_date: dict[str, list[ManifestEntry]],
) -> ManifestEntry | None:
    exact = by_datetime.get(export_entry.date)
    if exact is not None:
        return exact
    date_only = export_entry.date[:10]
    candidates = by_date.get(date_only, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _apply_reviewed_updates(
    connection: sqlite3.Connection,
    entry_id: str,
    export_entry: DiariumEntry,
) -> int:
    applied = 0
    for tag in export_entry.tags:
        if _is_source_tag(tag):
            continue
        if tag.startswith("person:") and len(tag) > len("person:"):
            applied += _upsert_person(connection, entry_id, tag.removeprefix("person:"), tag)
        else:
            tag_type = "place" if tag.startswith("place:") else "diarium"
            applied += _upsert_tag(connection, entry_id, tag_type, tag, tag)
    for person in export_entry.people:
        applied += _upsert_person(connection, entry_id, person, person)
    if export_entry.location is not None:
        applied += _upsert_location(connection, entry_id, export_entry.location)
    return applied


def _upsert_tag(
    connection: sqlite3.Connection,
    entry_id: str,
    tag_type: str,
    canonical_name: str,
    diarium_name: str,
) -> int:
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO tags (entry_id, tag_type, canonical_name, diarium_name, review_status, source)
        VALUES (?, ?, ?, ?, 'reviewed', 'diarium_export')
        ON CONFLICT(entry_id, tag_type, canonical_name)
        DO UPDATE SET
            diarium_name = excluded.diarium_name,
            review_status = excluded.review_status,
            source = excluded.source
        """,
        (entry_id, tag_type, canonical_name, diarium_name),
    )
    return connection.total_changes - before


def _upsert_person(
    connection: sqlite3.Connection,
    entry_id: str,
    canonical_name: str,
    diarium_tag: str,
) -> int:
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO people (entry_id, canonical_name, diarium_tag, review_status, source)
        VALUES (?, ?, ?, 'reviewed', 'diarium_export')
        ON CONFLICT(entry_id, canonical_name)
        DO UPDATE SET
            diarium_tag = excluded.diarium_tag,
            review_status = excluded.review_status,
            source = excluded.source
        """,
        (entry_id, canonical_name, diarium_tag),
    )
    return connection.total_changes - before


def _upsert_location(
    connection: sqlite3.Connection,
    entry_id: str,
    location: tuple[float, float],
) -> int:
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO locations (
            entry_id, location_status, source, latitude, longitude, label, confidence, review_status
        )
        VALUES (?, 'confirmed', 'diarium_export', ?, ?, 'diarium-export', 'manual', 'reviewed')
        ON CONFLICT(entry_id, location_status, source, label)
        DO UPDATE SET
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            confidence = excluded.confidence,
            review_status = excluded.review_status
        """,
        (entry_id, location[0], location[1]),
    )
    return connection.total_changes - before


def _report_row(
    manifest_entry: ManifestEntry | None,
    export_entry: DiariumEntry,
    match_status: str,
    text_status: str,
    has_reviewable_updates: bool,
) -> dict[str, object]:
    tags_without_source = [tag for tag in export_entry.tags if not _is_source_tag(tag)]
    location_status = "present" if export_entry.location is not None else "none"
    return {
        "entry_id": manifest_entry.entry_id if manifest_entry else "",
        "entry_date": manifest_entry.entry_date if manifest_entry else export_entry.date[:10],
        "diarium_date": export_entry.date,
        "match_status": match_status,
        "text_status": text_status,
        "text_sha256": _sha256_text(_html_to_text(export_entry.html)),
        "media_file_count": export_entry.media_count,
        "tag_count": len(export_entry.tags),
        "reviewable_tag_count": len(tags_without_source),
        "people_count": len(export_entry.people),
        "location_status": location_status,
        "has_reviewable_updates": str(has_reviewable_updates).lower(),
    }


def _missing_expected_report_row(manifest_entry: ManifestEntry) -> dict[str, object]:
    return {
        "entry_id": manifest_entry.entry_id,
        "entry_date": manifest_entry.entry_date,
        "diarium_date": "",
        "match_status": "missing_expected",
        "text_status": "",
        "text_sha256": "",
        "media_file_count": 0,
        "tag_count": 0,
        "reviewable_tag_count": 0,
        "people_count": 0,
        "location_status": "unknown",
        "has_reviewable_updates": "false",
    }


def _write_report(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "entry_id",
        "entry_date",
        "diarium_date",
        "match_status",
        "text_status",
        "text_sha256",
        "media_file_count",
        "tag_count",
        "reviewable_tag_count",
        "people_count",
        "location_status",
        "has_reviewable_updates",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _parse_location(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        latitude = float(value[0])
        longitude = float(value[1])
    except (TypeError, ValueError):
        return None
    return (latitude, longitude)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextParser()
    parser.feed(value)
    return "\n".join(part.strip() for part in parser.parts if part.strip())


def _text_status(canonical_text: str, exported_text: str) -> str:
    if canonical_text == exported_text:
        return "unchanged"
    if canonical_text.strip() == exported_text.strip():
        return "unchanged"
    if _collapse_whitespace(canonical_text) == _collapse_whitespace(exported_text):
        return "normalized_unchanged"
    return "changed"


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _has_reviewable_updates(entry: DiariumEntry) -> bool:
    return bool(
        [tag for tag in entry.tags if not _is_source_tag(tag)]
        or entry.people
        or entry.location is not None
    )


def _is_source_tag(tag: str) -> bool:
    return tag in {"source:project365", "source-project365"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_to_local_naive(value: str, time_zone: str) -> str:
    normalized = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    local = parsed.astimezone(ZoneInfo(time_zone)).replace(tzinfo=None, microsecond=0)
    return local.isoformat()


class _HTMLTextParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


if __name__ == "__main__":
    raise SystemExit(main())
