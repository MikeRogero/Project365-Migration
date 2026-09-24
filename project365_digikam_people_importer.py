#!/usr/bin/env python3
"""Import local digiKam person suggestions into the Project365 review queue."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from project365_media_derivatives import DEFAULT_DERIVATIVE_POLICY
from project365_tag_enrichment import normalize_diarium_tag, run_tag_enrichment


WORKING_COPY_RE = re.compile(
    r"(?:^|/)(?:project365_(\d{4}-\d{2}-\d{2})\.[^.]+|"
    r"Project365 Working Copy - (\d{4}-\d{2}-\d{2}) - "
    r"(?:sq\d+|associated sq\d+ - [0-9a-f]{12})\.(?:jpe?g|heic))$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DigiKamImportSummary:
    report_path: str
    tag_queue_path: str
    scanned_count: int
    suggested_count: int
    applied_count: int
    skipped_count: int
    error_count: int
    media_people_count: int = 0
    unmatched_sidecar_count: int = 0
    removed_suggestion_count: int = 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import local digiKam face/person suggestions.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--xmp-root", action="append", default=[], help="Folder containing digiKam XMP sidecars.")
    parser.add_argument("--suggestions-csv", help="CSV with media_path/path/file_path and person/name columns.")
    parser.add_argument(
        "--report-out",
        default="Project365Canonical/exports/verification_reports/digikam_people_import_report.csv",
    )
    parser.add_argument(
        "--queue-out",
        default="Project365Canonical/exports/verification_reports/tag_review_queue.csv",
    )
    args = parser.parse_args()

    summary = import_digikam_people(
        canonical_root=Path(args.canonical_root),
        xmp_roots=[Path(path) for path in args.xmp_root],
        suggestions_csv=Path(args.suggestions_csv) if args.suggestions_csv else None,
        report_path=Path(args.report_out),
        queue_path=Path(args.queue_out),
    )
    print("Project365 digiKam people import: PASS")
    print(f"Report: {summary.report_path}")
    print(f"Tag queue: {summary.tag_queue_path}")
    print(f"Scanned rows/files: {summary.scanned_count}")
    print(f"Suggested people: {summary.suggested_count}")
    print(f"Applied suggestions: {summary.applied_count}")
    print(f"Skipped suggestions: {summary.skipped_count}")
    print(f"Errors: {summary.error_count}")
    print(f"Photo person links: {summary.media_people_count}")
    print(f"Unmatched sidecars: {summary.unmatched_sidecar_count}")
    print(f"Removed stale suggestions: {summary.removed_suggestion_count}")
    return 0


def import_digikam_people(
    canonical_root: Path,
    xmp_roots: list[Path],
    suggestions_csv: Path | None,
    report_path: Path,
    queue_path: Path,
) -> DigiKamImportSummary:
    if not xmp_roots and suggestions_csv is None:
        raise ValueError("Provide --xmp-root or --suggestions-csv")
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    for root in xmp_roots:
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Missing XMP root: {root}")
    if suggestions_csv is not None and not suggestions_csv.exists():
        raise FileNotFoundError(f"Missing suggestions CSV: {suggestions_csv}")

    connection = sqlite3.connect(db_path)
    try:
        media_by_path = {
            str(Path(path).resolve()): asset_id
            for asset_id, path in connection.execute(
                """
                SELECT id, storage_path FROM media_assets
                WHERE role IN ('diarium_derivative', 'diarium_associated_derivative')
                    AND status = 'available' AND storage_path IS NOT NULL
                """
            )
        }
    finally:
        connection.close()
    resolved_roots = [root.resolve() for root in xmp_roots]
    working_copy_root = canonical_root / "media" / "diarium_derivatives" / DEFAULT_DERIVATIVE_POLICY
    full_working_copy_scan = working_copy_root.resolve() in resolved_roots
    scoped_media_ids = {
        asset_id
        for path, asset_id in media_by_path.items()
        if any(Path(path).is_relative_to(root) for root in resolved_roots)
    }
    suggestions: list[dict[str, str]] = []
    media_people: dict[str, set[str]] = {}
    parse_errors: list[dict[str, str]] = []
    malformed_media_ids: set[str] = set()
    unmatched_sidecar_count = 0
    scanned_count = 0
    for root in xmp_roots:
        for path in sorted(root.rglob("*.xmp")):
            scanned_count += 1
            media_path = _media_path_from_sidecar(path)
            try:
                names = _extract_people_from_xmp(path)
            except ET.ParseError:
                media_asset_id = media_by_path.get(str(media_path.resolve()))
                if media_asset_id is not None:
                    malformed_media_ids.add(media_asset_id)
                parse_errors.append({
                    "media_path": str(media_path), "entry_id": "", "person_name": "",
                    "status": "error", "reason": "malformed_xmp", "source": "digikam_xmp",
                })
                continue
            media_asset_id = media_by_path.get(str(media_path.resolve())) if media_path.is_file() else None
            if media_asset_id is not None:
                media_people.setdefault(media_asset_id, set()).update(names)
            elif names:
                unmatched_sidecar_count += 1
            for name in names:
                suggestions.append({
                    "media_path": str(media_path), "person_name": name,
                    "source": "digikam_xmp", "media_asset_id": media_asset_id or "",
                })
    if suggestions_csv is not None:
        with suggestions_csv.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                scanned_count += 1
                media_path = _first_value(row, ["media_path", "file_path", "path", "filename"])
                person_name = _first_value(row, ["person_name", "person", "name", "tag"])
                suggestions.append({"media_path": media_path, "person_name": person_name, "source": "digikam_csv"})

    report_rows = list(parse_errors)
    applied_count = 0
    skipped_count = 0
    error_count = len(parse_errors)
    removed_suggestion_count = 0
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS media_people (
                media_asset_id TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                diarium_tag TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (media_asset_id, canonical_name),
                FOREIGN KEY (media_asset_id) REFERENCES media_assets(id)
            )
            """
        )
        connection.execute(
            "UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'"
        )
        for media_asset_id in sorted(scoped_media_ids - malformed_media_ids):
            connection.execute(
                "DELETE FROM media_people WHERE media_asset_id = ? AND source = 'digikam_xmp'",
                (media_asset_id,),
            )
        for media_asset_id, names in media_people.items():
            connection.executemany(
                """
                INSERT OR IGNORE INTO media_people (media_asset_id, canonical_name, diarium_tag, source)
                VALUES (?, ?, ?, 'digikam_xmp')
                """,
                (
                    (media_asset_id, name, normalize_diarium_tag("person", name))
                    for name in sorted(names)
                ),
            )
        known_entries = _known_entry_ids(connection)
        seen_suggestions: set[tuple[str, str]] = set()
        for suggestion in suggestions:
            media_path = suggestion["media_path"].strip()
            person_name = _clean_person_name(suggestion["person_name"])
            entry_id = _entry_id_from_media_path(media_path)
            status = "skipped"
            reason = ""
            if not media_path or not person_name:
                reason = "missing_media_or_person"
                error_count += 1
            elif entry_id is None or entry_id not in known_entries:
                reason = "unknown_working_copy"
                error_count += 1
            elif full_working_copy_scan and suggestion["source"] == "digikam_xmp" and not suggestion.get("media_asset_id"):
                reason = "sidecar_not_current_working_copy"
                skipped_count += 1
            elif (entry_id, person_name) in seen_suggestions:
                reason = "duplicate_suggestion"
                skipped_count += 1
            else:
                seen_suggestions.add((entry_id, person_name))
                applied, reason = _upsert_suggested_person(
                    connection,
                    entry_id,
                    person_name,
                    suggestion["source"],
                )
                if applied:
                    applied_count += 1
                    status = "applied"
                else:
                    skipped_count += 1
            report_rows.append(
                {
                    "media_path": media_path,
                    "entry_id": entry_id or "",
                    "person_name": person_name,
                    "status": status,
                    "reason": reason,
                    "source": suggestion["source"],
                }
            )
        if full_working_copy_scan:
            before_cleanup = connection.total_changes
            connection.execute(
                """
                DELETE FROM people
                WHERE source = 'digikam_xmp' AND review_status = 'suggested'
                    AND NOT EXISTS (
                        SELECT 1 FROM media_people
                        JOIN media_assets ON media_assets.id = media_people.media_asset_id
                        WHERE media_assets.entry_id = people.entry_id
                            AND media_people.canonical_name = people.canonical_name
                            AND media_people.source = 'digikam_xmp'
                    )
                """
            )
            removed_suggestion_count = connection.total_changes - before_cleanup
        connection.commit()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(report_path, report_rows)
    tag_summary = run_tag_enrichment(canonical_root=canonical_root, queue_path=queue_path)
    return DigiKamImportSummary(
        report_path=str(report_path),
        tag_queue_path=tag_summary.queue_path,
        scanned_count=scanned_count,
        suggested_count=len(suggestions),
        applied_count=applied_count,
        skipped_count=skipped_count,
        error_count=error_count,
        media_people_count=sum(len(names) for names in media_people.values()),
        unmatched_sidecar_count=unmatched_sidecar_count,
        removed_suggestion_count=removed_suggestion_count,
    )


def _upsert_suggested_person(
    connection: sqlite3.Connection,
    entry_id: str,
    person_name: str,
    source: str,
) -> tuple[bool, str]:
    existing = connection.execute(
        """
        SELECT diarium_tag, review_status, source
        FROM people
        WHERE entry_id = ? AND canonical_name = ?
        """,
        (entry_id, person_name),
    ).fetchone()
    if existing and existing["review_status"] in {"confirmed", "reviewed"}:
        return False, "preserved_reviewed_person"
    diarium_tag = normalize_diarium_tag("person", person_name)
    if (
        existing
        and existing["review_status"] == "suggested"
        and existing["diarium_tag"] == diarium_tag
        and existing["source"] == source
    ):
        return False, "existing_suggestion"
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO people (entry_id, canonical_name, diarium_tag, review_status, source)
        VALUES (?, ?, ?, 'suggested', ?)
        ON CONFLICT(entry_id, canonical_name)
        DO UPDATE SET
            diarium_tag = excluded.diarium_tag,
            source = excluded.source
        WHERE people.review_status = 'suggested'
        """,
        (entry_id, person_name, diarium_tag, source),
    )
    return connection.total_changes > before, "suggested"


def _extract_people_from_xmp(path: Path) -> list[str]:
    root = ET.parse(path).getroot()
    people: list[str] = []
    for element in root.iter():
        for key, value in element.attrib.items():
            _collect_person_value(people, _local_name(key), value)
        text = (element.text or "").strip()
        if text:
            _collect_person_value(people, _local_name(element.tag), text)
    return _dedupe(people)


def _collect_person_value(people: list[str], key: str, value: str) -> None:
    clean = _clean_person_name(value)
    if not clean:
        return
    if key in {"Name", "PersonDisplayName", "RegionName"}:
        people.append(clean)
        return
    if key in {"subject", "hierarchicalSubject", "TagsList"}:
        for item in re.split(r"[,;]\s*", value):
            person = _person_from_tag_path(item)
            if person:
                people.append(person)
    if key in {"li"}:
        person = _person_from_tag_path(value)
        if person:
            people.append(person)


def _person_from_tag_path(value: str) -> str:
    parts = [part.strip() for part in re.split(r"[|/\\\\]", value) if part.strip()]
    if len(parts) >= 2 and parts[0].lower() in {"people", "person", "persons"}:
        return _clean_person_name(parts[-1])
    return ""


def _media_path_from_sidecar(path: Path) -> Path:
    if path.name.lower().endswith(".jpg.xmp") or path.name.lower().endswith(".jpeg.xmp"):
        return path.with_suffix("")
    return path.with_suffix("")


def _entry_id_from_media_path(media_path: str) -> str | None:
    match = WORKING_COPY_RE.search(media_path.replace("\\", "/"))
    if not match:
        return None
    return f"project365:{match.group(1) or match.group(2)}"


def _known_entry_ids(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT id FROM entries")}


def _first_value(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key, "")
        if value and value.strip():
            return value.strip()
    return ""


def _write_report(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["media_path", "entry_id", "person_name", "status", "reason", "source"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _clean_person_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
