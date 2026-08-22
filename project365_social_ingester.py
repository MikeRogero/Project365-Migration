#!/usr/bin/env python3
"""Import local social staging records into the Project365 canonical archive."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from project365_canonical_importer import _create_schema
from project365_location_enrichment import upsert_location
from project365_tag_enrichment import upsert_tag


@dataclass(frozen=True)
class SocialIngestSummary:
    import_batch_id: str
    imported_count: int
    skipped_count: int
    canonical_root: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Import social staging JSONL into canonical archive.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--staging-jsonl", required=True)
    parser.add_argument(
        "--selected-only",
        action="store_true",
        help="Import only records with diarium_action/import_decision selected for import.",
    )
    args = parser.parse_args()

    summary = import_social_staging(
        canonical_root=Path(args.canonical_root),
        staging_jsonl=Path(args.staging_jsonl),
        selected_only=args.selected_only,
    )
    print("Project365 social ingest: PASS")
    print(f"Batch: {summary.import_batch_id}")
    print(f"Imported: {summary.imported_count}")
    print(f"Skipped: {summary.skipped_count}")
    print(f"Canonical root: {summary.canonical_root}")
    return 0


def import_social_staging(
    canonical_root: Path,
    staging_jsonl: Path,
    selected_only: bool = False,
) -> SocialIngestSummary:
    if not staging_jsonl.exists():
        raise FileNotFoundError(f"Missing staging JSONL: {staging_jsonl}")
    canonical_root.mkdir(parents=True, exist_ok=True)
    db_path = canonical_root / "canonical.db"
    batch_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC).isoformat()
    records = _read_jsonl(staging_jsonl)
    imported = 0
    skipped = 0
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_schema(connection)
        _start_batch(connection, batch_id, str(staging_jsonl), now)
        source_file_ids: dict[str, int] = {}
        for record in records:
            if selected_only and not _selected_for_diarium(record):
                skipped += 1
                continue
            if _is_deleted_or_empty(record):
                skipped += 1
                continue
            source_path = str(record["provenance"]["source_file_path"])
            if source_path not in source_file_ids:
                source_file_ids[source_path] = _upsert_source_file(
                    connection,
                    record,
                    staging_jsonl,
                    batch_id,
                    now,
                )
            entry_id = _entry_id(record)
            timestamp = _parse_timestamp(str(record["timestamp"]))
            original_text = record.get("text") or ""
            connection.execute(
                """
                INSERT INTO entries (
                    id, entry_date, source_app, original_text, corrected_text,
                    correction_status, import_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, 'uncorrected', ?, ?, ?)
                ON CONFLICT(id)
                DO UPDATE SET
                    entry_date = excluded.entry_date,
                    original_text = excluded.original_text,
                    import_status = excluded.import_status,
                    updated_at = excluded.updated_at
                """,
                (
                    entry_id,
                    timestamp.date().isoformat(),
                    str(record["source_name"]),
                    original_text,
                    _diarium_action(record),
                    now,
                    now,
                ),
            )
            _upsert_entry_source(
                connection,
                entry_id,
                source_file_ids[source_path],
                record,
                batch_id,
            )
            upsert_tag(connection, entry_id, "source", str(record["source_name"]), "confirmed", "social_export")
            for url in record.get("urls", []) or []:
                upsert_tag(connection, entry_id, "url", str(url), "reviewed", "social_export")
            if record.get("place_name"):
                upsert_tag(connection, entry_id, "place", str(record["place_name"]), "reviewed", "social_export")
            if record.get("latitude") is not None and record.get("longitude") is not None:
                upsert_location(
                    connection,
                    entry_id,
                    "confirmed",
                    str(record["source_name"]),
                    float(record["latitude"]),
                    float(record["longitude"]),
                    str(record.get("place_name") or "social-export"),
                    "source_feed",
                    "confirmed",
                )
            imported += 1
        _finish_batch(connection, batch_id, imported, len(source_file_ids), now)
        connection.commit()
    finally:
        connection.close()
    return SocialIngestSummary(
        import_batch_id=batch_id,
        imported_count=imported,
        skipped_count=skipped,
        canonical_root=str(canonical_root),
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("Each staging line must be a JSON object")
            records.append(record)
    return records


def _entry_id(record: dict[str, object]) -> str:
    return f"social:{record['source_name']}:{record['source_id']}"


def _parse_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _upsert_source_file(
    connection: sqlite3.Connection,
    record: dict[str, object],
    staging_jsonl: Path,
    batch_id: str,
    now: str,
) -> int:
    source_path = str(record["provenance"]["source_file_path"])
    synthetic_path = f"{staging_jsonl.resolve()}::{source_path}"
    connection.execute(
        """
        INSERT INTO source_files (
            source_type, source_path, zip_filename, month, zip_sha256, zip_bytes,
            file_modified_at, validation_status, import_batch_id, imported_at
        )
        VALUES ('social_export', ?, ?, NULL, ?, ?, NULL, 'staged', ?, ?)
        ON CONFLICT(source_path)
        DO UPDATE SET
            validation_status = excluded.validation_status,
            import_batch_id = excluded.import_batch_id,
            imported_at = excluded.imported_at
        """,
        (
            synthetic_path,
            source_path,
            _sha256_file(staging_jsonl),
            staging_jsonl.stat().st_size,
            batch_id,
            now,
        ),
    )
    return int(
        connection.execute(
            "SELECT id FROM source_files WHERE source_path = ?",
            (synthetic_path,),
        ).fetchone()[0]
    )


def _upsert_entry_source(
    connection: sqlite3.Connection,
    entry_id: str,
    source_file_id: int,
    record: dict[str, object],
    batch_id: str,
) -> None:
    source_record_id = str(record["provenance"]["source_record_id"])
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    connection.execute(
        """
        INSERT INTO entry_sources (
            entry_id, source_file_id, internal_filename, source_role,
            internal_sha256, byte_size, import_batch_id
        )
        VALUES (?, ?, ?, 'social_record', ?, ?, ?)
        ON CONFLICT(entry_id, source_file_id, internal_filename, source_role)
        DO UPDATE SET
            internal_sha256 = excluded.internal_sha256,
            byte_size = excluded.byte_size,
            import_batch_id = excluded.import_batch_id
        """,
        (
            entry_id,
            source_file_id,
            source_record_id,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            batch_id,
        ),
    )


def _start_batch(connection: sqlite3.Connection, batch_id: str, import_dir: str, now: str) -> None:
    connection.execute(
        """
        INSERT INTO import_batches (id, source_type, import_dir, started_at, status)
        VALUES (?, 'social_export', ?, ?, 'running')
        """,
        (batch_id, import_dir, now),
    )


def _finish_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    imported: int,
    source_files: int,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE import_batches
        SET finished_at = ?,
            source_file_count = ?,
            entry_count = ?,
            entry_source_count = ?,
            status = 'completed'
        WHERE id = ?
        """,
        (now, source_files, imported, imported, batch_id),
    )


def _is_deleted_or_empty(record: dict[str, object]) -> bool:
    text = str(record.get("text") or "").strip()
    has_media = bool(record.get("media_references") or [])
    has_url = bool(record.get("urls") or [])
    has_place = bool(record.get("place_name"))
    return not (text or has_media or has_url or has_place)


def _selected_for_diarium(record: dict[str, object]) -> bool:
    return _diarium_action(record) in {"import", "selected", "reviewed_import"}


def _diarium_action(record: dict[str, object]) -> str:
    return str(record.get("diarium_action") or record.get("import_decision") or "canonical_only")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
