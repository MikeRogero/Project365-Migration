#!/usr/bin/env python3
"""Import validated Project365 exports into a local canonical SQLite archive."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import project365_export_validator as validator
from project365_paths import PROJECT365_PRO_EXPORT_ZIPS_DIR


SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ImportSummary:
    canonical_root: str
    database_path: str
    source_files: int
    entries: int
    entry_sources: int
    media_assets: int
    text_entries: int
    import_batch_id: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import validated Project365 zips into a canonical archive."
    )
    parser.add_argument(
        "--import-dir",
        default=str(PROJECT365_PRO_EXPORT_ZIPS_DIR),
        help="Folder containing Project365 Pro export zips.",
    )
    parser.add_argument(
        "--canonical-root",
        default="Project365Canonical",
        help="Canonical archive root folder.",
    )
    parser.add_argument(
        "--report-dir",
        default="Reports",
        help="Folder where validation reports and source manifest are written.",
    )
    parser.add_argument("--start-month", help="Expected first month, YYYY-MM.")
    parser.add_argument("--end-month", help="Expected last month, YYYY-MM.")
    args = parser.parse_args()

    summary = import_project365_exports(
        import_dir=Path(args.import_dir),
        canonical_root=Path(args.canonical_root),
        report_dir=Path(args.report_dir),
        start_month=args.start_month,
        end_month=args.end_month,
    )

    print("Project365 canonical import: PASS")
    print(f"Canonical root: {summary.canonical_root}")
    print(f"Database: {summary.database_path}")
    print(f"Source files imported: {summary.source_files}")
    print(f"Entries imported: {summary.entries}")
    print(f"Entry-source links: {summary.entry_sources}")
    print(f"Project365 media assets: {summary.media_assets}")
    print(f"Entries with original text: {summary.text_entries}")
    print(f"Import batch: {summary.import_batch_id}")
    return 0


def import_project365_exports(
    import_dir: Path,
    canonical_root: Path,
    report_dir: Path | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
) -> ImportSummary:
    report = validator.validate_exports(import_dir, start_month, end_month)
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        validator.write_json_report(
            report, report_dir / "project365_export_validation.json"
        )
        validator.write_markdown_report(
            report, report_dir / "project365_export_validation.md"
        )
        validator.write_source_manifest(report, report_dir / "source_files.csv")
    if not report.ok:
        raise ValueError(
            "Project365 exports must pass validation before canonical import."
        )

    canonical_root = canonical_root.resolve()
    db_path = canonical_root / "canonical.db"
    media_root = canonical_root / "media" / "project365_exports"
    canonical_root.mkdir(parents=True, exist_ok=True)
    media_root.mkdir(parents=True, exist_ok=True)

    batch_id = str(uuid.uuid4())
    imported_at = dt.datetime.now(dt.UTC).isoformat()

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_schema(connection)
        _start_import_batch(connection, batch_id, imported_at, report.import_dir)

        counters = {
            "source_files": 0,
            "entries": 0,
            "entry_sources": 0,
            "media_assets": 0,
            "text_entries": 0,
        }

        for export in report.month_exports:
            source_file_id = _upsert_source_file(
                connection, export, batch_id, imported_at
            )
            counters["source_files"] += 1
            month_media_root = media_root / (export.month or "unknown")
            month_media_root.mkdir(parents=True, exist_ok=True)
            _import_zip_entries(
                connection=connection,
                export=export,
                source_file_id=source_file_id,
                month_media_root=month_media_root,
                batch_id=batch_id,
                counters=counters,
            )

        _finish_import_batch(connection, batch_id, counters)
        connection.commit()
    finally:
        connection.close()

    return ImportSummary(
        canonical_root=str(canonical_root),
        database_path=str(db_path),
        source_files=counters["source_files"],
        entries=counters["entries"],
        entry_sources=counters["entry_sources"],
        media_assets=counters["media_assets"],
        text_entries=counters["text_entries"],
        import_batch_id=batch_id,
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR REPLACE INTO schema_meta (key, value)
        VALUES ('schema_version', '2');

        CREATE TABLE IF NOT EXISTS import_batches (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            import_dir TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            source_file_count INTEGER NOT NULL DEFAULT 0,
            entry_count INTEGER NOT NULL DEFAULT 0,
            entry_source_count INTEGER NOT NULL DEFAULT 0,
            media_asset_count INTEGER NOT NULL DEFAULT 0,
            text_entry_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_path TEXT NOT NULL UNIQUE,
            zip_filename TEXT NOT NULL,
            month TEXT,
            zip_sha256 TEXT,
            zip_bytes INTEGER NOT NULL,
            file_modified_at TEXT,
            validation_status TEXT NOT NULL,
            import_batch_id TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            FOREIGN KEY (import_batch_id) REFERENCES import_batches(id)
        );

        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            entry_date TEXT NOT NULL,
            source_app TEXT NOT NULL,
            original_text TEXT,
            corrected_text TEXT,
            correction_status TEXT NOT NULL,
            import_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entry_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL,
            source_file_id INTEGER NOT NULL,
            internal_filename TEXT NOT NULL,
            source_role TEXT NOT NULL,
            internal_sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            import_batch_id TEXT NOT NULL,
            UNIQUE (entry_id, source_file_id, internal_filename, source_role),
            FOREIGN KEY (entry_id) REFERENCES entries(id),
            FOREIGN KEY (source_file_id) REFERENCES source_files(id),
            FOREIGN KEY (import_batch_id) REFERENCES import_batches(id)
        );

        CREATE TABLE IF NOT EXISTS media_assets (
            id TEXT PRIMARY KEY,
            entry_id TEXT NOT NULL,
            role TEXT NOT NULL,
            source_file_id INTEGER,
            internal_filename TEXT,
            storage_path TEXT,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            mime_type TEXT NOT NULL,
            status TEXT NOT NULL,
            review_status TEXT NOT NULL,
            selected_default INTEGER NOT NULL DEFAULT 0,
            transformation_json TEXT NOT NULL,
            import_batch_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (entry_id, role, source_file_id, internal_filename),
            FOREIGN KEY (entry_id) REFERENCES entries(id),
            FOREIGN KEY (source_file_id) REFERENCES source_files(id),
            FOREIGN KEY (import_batch_id) REFERENCES import_batches(id)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL,
            tag_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            diarium_name TEXT,
            review_status TEXT NOT NULL,
            source TEXT NOT NULL,
            UNIQUE (entry_id, tag_type, canonical_name),
            FOREIGN KEY (entry_id) REFERENCES entries(id)
        );

        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            diarium_tag TEXT,
            review_status TEXT NOT NULL,
            source TEXT NOT NULL,
            UNIQUE (entry_id, canonical_name),
            FOREIGN KEY (entry_id) REFERENCES entries(id)
        );

        CREATE TABLE IF NOT EXISTS media_people (
            media_asset_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            diarium_tag TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (media_asset_id, canonical_name),
            FOREIGN KEY (media_asset_id) REFERENCES media_assets(id)
        );

        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL,
            location_status TEXT NOT NULL,
            source TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            label TEXT,
            confidence TEXT,
            review_status TEXT NOT NULL,
            UNIQUE (entry_id, location_status, source, label),
            FOREIGN KEY (entry_id) REFERENCES entries(id)
        );
        """
    )


def _start_import_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    imported_at: str,
    import_dir: str,
) -> None:
    connection.execute(
        """
        INSERT INTO import_batches (id, source_type, import_dir, started_at, status)
        VALUES (?, 'project365_zip', ?, ?, 'running')
        """,
        (batch_id, import_dir, imported_at),
    )


def _finish_import_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    counters: dict[str, int],
) -> None:
    connection.execute(
        """
        UPDATE import_batches
        SET finished_at = ?,
            source_file_count = ?,
            entry_count = ?,
            entry_source_count = ?,
            media_asset_count = ?,
            text_entry_count = ?,
            status = 'pass'
        WHERE id = ?
        """,
        (
            dt.datetime.now(dt.UTC).isoformat(),
            counters["source_files"],
            counters["entries"],
            counters["entry_sources"],
            counters["media_assets"],
            counters["text_entries"],
            batch_id,
        ),
    )


def _upsert_source_file(
    connection: sqlite3.Connection,
    export: validator.MonthExport,
    batch_id: str,
    imported_at: str,
) -> int:
    source_path = str(Path(export.zip_file).resolve())
    connection.execute(
        """
        INSERT INTO source_files (
            source_type,
            source_path,
            zip_filename,
            month,
            zip_sha256,
            zip_bytes,
            file_modified_at,
            validation_status,
            import_batch_id,
            imported_at
        )
        VALUES ('project365_zip', ?, ?, ?, ?, ?, ?, 'pass', ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            month = excluded.month,
            zip_sha256 = excluded.zip_sha256,
            zip_bytes = excluded.zip_bytes,
            file_modified_at = excluded.file_modified_at,
            validation_status = excluded.validation_status,
            import_batch_id = excluded.import_batch_id,
            imported_at = excluded.imported_at
        """,
        (
            source_path,
            Path(export.zip_file).name,
            export.month,
            export.zip_sha256,
            export.zip_bytes,
            export.file_modified_at,
            batch_id,
            imported_at,
        ),
    )
    row = connection.execute(
        "SELECT id FROM source_files WHERE source_path = ?", (source_path,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Could not load source file row for {source_path}")
    return int(row[0])


def _import_zip_entries(
    connection: sqlite3.Connection,
    export: validator.MonthExport,
    source_file_id: int,
    month_media_root: Path,
    batch_id: str,
    counters: dict[str, int],
) -> None:
    entry_dates = {entry.date for entry in export.entries}
    now = dt.datetime.now(dt.UTC).isoformat()
    text_by_date: dict[str, str] = {}
    png_by_date: dict[str, tuple[str, bytes]] = {}

    with zipfile.ZipFile(export.zip_file) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            date_text = info.filename[:10]
            if date_text not in entry_dates:
                continue
            payload = archive.read(info)
            if info.filename.endswith(".txt"):
                text_by_date[date_text] = payload.decode("utf-8-sig")
            elif info.filename.endswith(".png"):
                png_by_date[date_text] = (info.filename, payload)

    for day in export.entries:
        entry_id = _entry_id(day.date)
        original_text = text_by_date.get(day.date)
        connection.execute(
            """
            INSERT INTO entries (
                id,
                entry_date,
                source_app,
                original_text,
                corrected_text,
                correction_status,
                import_status,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'project365', ?, NULL, 'uncorrected', 'not_exported', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                original_text = excluded.original_text,
                updated_at = excluded.updated_at
            """,
            (entry_id, day.date, original_text, now, now),
        )
        counters["entries"] += 1
        if original_text is not None:
            counters["text_entries"] += 1

        if original_text is not None:
            txt_filename = f"{day.date}.txt"
            _upsert_entry_source(
                connection=connection,
                entry_id=entry_id,
                source_file_id=source_file_id,
                internal_filename=txt_filename,
                source_role="project365_text",
                payload=original_text.encode("utf-8"),
                batch_id=batch_id,
            )
            counters["entry_sources"] += 1

        if day.date in png_by_date:
            png_filename, payload = png_by_date[day.date]
            storage_path = month_media_root / png_filename
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes_if_changed(storage_path, payload)
            png_sha256 = _sha256_bytes(payload)
            _upsert_entry_source(
                connection=connection,
                entry_id=entry_id,
                source_file_id=source_file_id,
                internal_filename=png_filename,
                source_role="project365_png",
                payload=payload,
                batch_id=batch_id,
            )
            counters["entry_sources"] += 1
            _upsert_media_asset(
                connection=connection,
                entry_id=entry_id,
                source_file_id=source_file_id,
                internal_filename=png_filename,
                storage_path=storage_path,
                sha256=png_sha256,
                byte_size=len(payload),
                batch_id=batch_id,
                now=now,
            )
            counters["media_assets"] += 1


def _upsert_entry_source(
    connection: sqlite3.Connection,
    entry_id: str,
    source_file_id: int,
    internal_filename: str,
    source_role: str,
    payload: bytes,
    batch_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO entry_sources (
            entry_id,
            source_file_id,
            internal_filename,
            source_role,
            internal_sha256,
            byte_size,
            import_batch_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entry_id, source_file_id, internal_filename, source_role)
        DO UPDATE SET
            internal_sha256 = excluded.internal_sha256,
            byte_size = excluded.byte_size,
            import_batch_id = excluded.import_batch_id
        """,
        (
            entry_id,
            source_file_id,
            internal_filename,
            source_role,
            _sha256_bytes(payload),
            len(payload),
            batch_id,
        ),
    )


def _upsert_media_asset(
    connection: sqlite3.Connection,
    entry_id: str,
    source_file_id: int,
    internal_filename: str,
    storage_path: Path,
    sha256: str,
    byte_size: int,
    batch_id: str,
    now: str,
) -> None:
    asset_id = f"{entry_id}:project365_export_png"
    connection.execute(
        """
        INSERT INTO media_assets (
            id,
            entry_id,
            role,
            source_file_id,
            internal_filename,
            storage_path,
            sha256,
            byte_size,
            mime_type,
            status,
            review_status,
            selected_default,
            transformation_json,
            import_batch_id,
            created_at,
            updated_at
        )
        VALUES (?, ?, 'project365_export_png', ?, ?, ?, ?, ?, 'image/png',
                'available', 'unreviewed', 1, ?, ?, ?, ?)
        ON CONFLICT(id)
        DO UPDATE SET
            source_file_id = excluded.source_file_id,
            internal_filename = excluded.internal_filename,
            storage_path = excluded.storage_path,
            sha256 = excluded.sha256,
            byte_size = excluded.byte_size,
            status = excluded.status,
            review_status = excluded.review_status,
            selected_default = excluded.selected_default,
            transformation_json = excluded.transformation_json,
            import_batch_id = excluded.import_batch_id,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            entry_id,
            source_file_id,
            internal_filename,
            str(storage_path),
            sha256,
            byte_size,
            json.dumps(
                {
                    "role": "project365_export_png",
                    "source": "Project365 export",
                    "transformations": [],
                },
                sort_keys=True,
            ),
            batch_id,
            now,
            now,
        ),
    )


def _entry_id(date_text: str) -> str:
    return f"project365:{date_text}"


def _write_bytes_if_changed(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() == payload:
        return
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_bytes(payload)
    shutil.move(str(temp_path), path)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
