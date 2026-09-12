#!/usr/bin/env python3
"""Build and query a lightweight local photo-library metadata index."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from project365_original_matcher import IMAGE_EXTENSIONS


FILENAME_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-_](0[1-9]|1[0-2])[-_](0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2}|19\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),
]
FILENAME_TIMESTAMP_PATTERNS = [
    re.compile(
        r"(?<!\d)(20\d{2}|19\d{2})[-_](0[1-9]|1[0-2])[-_]([0-2]\d|3[01])"
        r"[ T_-]+([01]\d|2[0-3])[:._-]?([0-5]\d)[:._-]?([0-5]\d)(?!\d)"
    ),
    re.compile(
        r"(?<!\d)(20\d{2}|19\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])"
        r"[ T_-]*([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)"
    ),
]
DEFAULT_INDEX_FILENAME = "photo_library_index.sqlite"
BROAD_VISUAL_INDEX_FILENAME = "broad_visual_match.sqlite"
PHOTO_INDEX_METADATA_VERSION = "gps-sha-v1"
BATCH_SIZE = 500
METADATA_WORKERS = 4
MAX_PENDING_METADATA_BATCHES = METADATA_WORKERS * 2
QUALITY_POSITIVE_TERMS = {
    "improved": 50,
    "enhanced": 45,
    "edited": 40,
    "edit": 30,
    "retouched": 35,
    "corrected": 30,
    "final": 25,
    "best": 25,
    "master": 25,
    "highres": 20,
    "hires": 20,
    "fullres": 20,
    "full": 10,
}
QUALITY_NEGATIVE_TERMS = {
    "web": -45,
    "small": -35,
    "thumb": -40,
    "thumbnail": -40,
    "preview": -30,
    "lowres": -35,
    "low": -15,
    "resized": -30,
    "compressed": -25,
    "reduced": -25,
    "email": -20,
    "facebook": -20,
    "instagram": -20,
    "screen": -15,
}


@dataclass(frozen=True)
class IndexSummary:
    index_db_path: str
    scanned_file_count: int
    indexed_file_count: int
    skipped_file_count: int
    new_file_count: int = 0
    moved_file_count: int = 0
    pruned_file_count: int = 0
    capture_source_counts: dict[str, int] | None = None


@dataclass(frozen=True)
class ExiftoolPhotoMetadata:
    capture_timestamp: str = ""
    capture_timestamp_source: str = ""
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    gps_source: str = ""


@dataclass(frozen=True)
class IndexedFileSnapshot:
    filesystem_mtime_utc: str
    filesystem_mtime_ns: int
    byte_size: int
    metadata_version: str
    sha256: str


@dataclass(frozen=True)
class ScannedPhotoFile:
    path: Path
    path_text: str
    stat: os.stat_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Index external photo-library metadata for Project365 matching.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--index-db", help="SQLite index path. Defaults inside the canonical root.")
    parser.add_argument(
        "--index-root",
        action="append",
        default=[],
        help="Photo folder or mounted drive to index. May be repeated.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear existing indexed rows before scanning.",
    )
    parser.add_argument(
        "--reconcile-moves-only",
        action="store_true",
        help="Only relink moved or renamed indexed files by content hash; skip metadata refresh and new files.",
    )
    parser.add_argument(
        "--prune-contained-roots",
        action="store_true",
        help="Relabel indexed rows from child roots to indexed parent roots without rescanning files.",
    )
    parser.add_argument(
        "--entry-date",
        action="append",
        default=[],
        help="Print indexed candidate paths for this date, YYYY-MM-DD. May be repeated.",
    )
    args = parser.parse_args()

    index_db = default_index_db(Path(args.canonical_root), args.index_db)
    if args.index_root:
        summary = build_photo_library_index(
            index_db=index_db,
            index_roots=[Path(root) for root in args.index_root],
            reset=args.reset,
            reconcile_moves_only=args.reconcile_moves_only,
            progress_every=5000,
        )
        print("Project365 photo-library index: PASS")
        print(f"Index DB: {summary.index_db_path}")
        print(f"Scanned files: {summary.scanned_file_count}")
        print(f"Indexed files: {summary.indexed_file_count}")
        print(f"New files added: {summary.new_file_count}")
        print(f"Moved files relinked: {summary.moved_file_count}")
        print(f"Missing files pruned: {summary.pruned_file_count}")
        print(f"Skipped files: {summary.skipped_file_count}")
        for source, count in sorted((summary.capture_source_counts or {}).items()):
            print(f"Capture source {source}: {count}")
        queue_path = Path(args.canonical_root) / "exports" / "verification_reports" / "original_photo_external_search_queue.csv"
        default_db = default_index_db(Path(args.canonical_root)).resolve()
        if not args.reconcile_moves_only and index_db.resolve() == default_db and queue_path.exists():
            enriched = enrich_candidate_queue(index_db, queue_path)
            print(f"Picker candidates updated with capture time: {enriched}")
    if args.prune_contained_roots:
        prune_summary = prune_contained_index_roots(index_db)
        print("Project365 photo-library contained-root pruning: PASS")
        print(f"Index DB: {index_db}")
        print(f"Roots before: {prune_summary['roots_before']}")
        print(f"Roots after: {prune_summary['roots_after']}")
        print(f"Rows relabeled: {prune_summary['rows_relabelled']}")
    for entry_date in args.entry_date:
        for candidate in query_index_candidates(index_db, {entry_date}).get(entry_date, []):
            print(candidate["candidate_path"])
    return 0


def default_index_db(canonical_root: Path, explicit_path: str | None = None) -> Path:
    if explicit_path:
        return Path(explicit_path)
    return canonical_root / DEFAULT_INDEX_FILENAME


def build_photo_library_index(
    index_db: Path,
    index_roots: list[Path],
    reset: bool = False,
    reconcile_moves_only: bool = False,
    progress_every: int = 0,
) -> IndexSummary:
    if reset and reconcile_moves_only:
        raise ValueError("Move-only reconciliation cannot replace the photo index.")
    index_roots = _canonical_index_roots(index_roots)
    for root in index_roots:
        if not root.exists():
            raise FileNotFoundError(f"Missing index root: {root}")
    index_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_db, timeout=60)
    try:
        _initialize_schema(connection)
        connection.execute("PRAGMA busy_timeout = 60000")
        started_at = dt.datetime.now(dt.UTC).isoformat()
        if reset:
            _reset_index(connection)
        else:
            _relabel_indexed_child_roots(connection, index_roots)
        file_count_before = _indexed_file_count(connection)
        indexed_snapshot = {} if reset else _indexed_file_snapshot(connection, index_roots)
        related_db_path = index_db.parent / BROAD_VISUAL_INDEX_FILENAME
        scanned = 0
        indexed = 0
        moved = 0
        seen_paths: set[str] = set()
        skipped = 0
        batch: list[dict[str, object]] = []
        pending: list[tuple[concurrent.futures.Future[None], list[dict[str, object]]]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=METADATA_WORKERS) as executor:
            for root in index_roots:
                if progress_every:
                    print(f"Scanning photo index root: {root}", flush=True)
                for scanned_file in _iter_image_files(root):
                    path = scanned_file.path
                    stat = scanned_file.stat
                    path_text = scanned_file.path_text
                    scanned += 1
                    seen_paths.add(path_text)
                    mtime_utc = _filesystem_mtime_utc(stat)
                    mtime_ns = _filesystem_mtime_ns(stat)
                    if _can_skip_indexed_file(indexed_snapshot.get(path_text), mtime_utc, mtime_ns, stat.st_size):
                        skipped += 1
                        continue
                    if reconcile_moves_only:
                        if indexed_snapshot.get(path_text) is not None:
                            skipped += 1
                            continue
                        row = _move_candidate_row(
                            root,
                            path,
                            stat=stat,
                            path_text=path_text,
                            filesystem_mtime_utc=mtime_utc,
                            filesystem_mtime_ns=mtime_ns,
                        )
                        if row is not None and _relink_moved_row(connection, row, related_db_path):
                            indexed += 1
                            moved += 1
                        else:
                            skipped += 1
                        continue
                    row = _index_row(
                        root,
                        path,
                        stat=stat,
                        path_text=path_text,
                        filesystem_mtime_utc=mtime_utc,
                        filesystem_mtime_ns=mtime_ns,
                    )
                    if row is None:
                        skipped += 1
                        continue
                    batch.append(row)
                    if len(batch) >= BATCH_SIZE:
                        pending.append((executor.submit(_enrich_capture_metadata, batch), batch))
                        batch = []
                    if len(pending) >= MAX_PENDING_METADATA_BATCHES:
                        future, ready_batch = pending.pop(0)
                        future.result()
                        written, moved_batch = _write_batch(connection, ready_batch, related_db_path)
                        indexed += written
                        moved += moved_batch
                    if progress_every and scanned % progress_every == 0:
                        print(
            "Photo index progress: "
                            f"{scanned} images scanned, {indexed + len(batch)} indexed, {skipped} skipped",
                            flush=True,
                        )
            if batch:
                pending.append((executor.submit(_enrich_capture_metadata, batch), batch))
            for future, ready_batch in pending:
                future.result()
                written, moved_batch = _write_batch(connection, ready_batch, related_db_path)
                indexed += written
                moved += moved_batch
        pruned = 0 if reset else _prune_missing_scanned_files(
            connection,
            indexed_snapshot.keys(),
            seen_paths,
            related_db_path,
        )
        file_count_after = _indexed_file_count(connection)
        new_file_count = max(0, file_count_after - file_count_before)
        capture_source_counts = {} if reconcile_moves_only else _capture_source_counts(connection)
        _record_index_run(
            connection,
            started_at=started_at,
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
            roots=index_roots,
            reset=reset,
            scanned=scanned,
            indexed=indexed,
            skipped=skipped,
            file_count_before=file_count_before,
            file_count_after=file_count_after,
            new_file_count=new_file_count,
        )
        connection.commit()
        return IndexSummary(
            index_db_path=str(index_db),
            scanned_file_count=scanned,
            indexed_file_count=indexed,
            skipped_file_count=skipped,
            new_file_count=new_file_count,
            moved_file_count=moved,
            pruned_file_count=pruned,
            capture_source_counts=capture_source_counts,
        )
    finally:
        connection.close()


def prune_contained_index_roots(index_db: Path) -> dict[str, int]:
    if not index_db.exists():
        return {"roots_before": 0, "roots_after": 0, "rows_relabelled": 0}
    connection = sqlite3.connect(index_db, timeout=60)
    try:
        _initialize_schema(connection)
        connection.execute("PRAGMA busy_timeout = 60000")
        roots_before = _indexed_roots(connection)
        canonical_roots = _canonical_index_roots([Path(root) for root in roots_before])
        rows_relabelled = _relabel_indexed_child_roots(connection, canonical_roots)
        connection.commit()
        roots_after = _indexed_roots(connection)
        return {
            "roots_before": len(roots_before),
            "roots_after": len(roots_after),
            "rows_relabelled": rows_relabelled,
        }
    finally:
        connection.close()


def query_index_candidates(
    index_db: Path,
    target_dates: set[str],
    limit_per_date: int | None = None,
    max_distance_days: int | None = None,
    folder_root: Path | None = None,
    include_filesystem_dates: bool = False,
    filename_dates_only: bool = False,
) -> dict[str, list[dict[str, object]]]:
    if not target_dates or not index_db.exists():
        return {date: [] for date in target_dates}
    if max_distance_days is not None and max_distance_days < 0:
        raise ValueError("max_distance_days must not be negative")
    folder_prefix = _folder_path_prefix(folder_root) if folder_root is not None else ""
    date_sources = ["filename_date"] if filename_dates_only else ["filename_date", "media_creation_date"]
    if include_filesystem_dates and not filename_dates_only:
        date_sources.append("filesystem_date")
    source_placeholders = ", ".join("?" for _ in date_sources)
    connection = sqlite3.connect(index_db, timeout=60)
    try:
        connection.execute("PRAGMA busy_timeout = 60000")
        connection.row_factory = sqlite3.Row
        _initialize_schema(connection)
        candidates_by_date: dict[str, list[dict[str, object]]] = {}
        for target_date in sorted(target_dates):
            _validate_date(target_date)
            rows_by_path: dict[str, sqlite3.Row] = {}
            date_tiers = _candidate_date_tiers(target_date)
            if max_distance_days is not None:
                target = dt.date.fromisoformat(target_date)
                date_tiers = [
                    [
                        (target + dt.timedelta(days=offset)).isoformat()
                        for offset in range(-max_distance_days, max_distance_days + 1)
                    ]
                ]
            for tier_dates in date_tiers:
                placeholders = ", ".join("?" for _ in tier_dates)
                folder_where = ""
                folder_params: tuple[object, ...] = ()
                if folder_prefix:
                    folder_where = "AND (files.path = ? OR substr(files.path, 1, ?) = ?)"
                    folder_params = (folder_prefix.rstrip(os.sep), len(folder_prefix), folder_prefix)
                tier_rows = connection.execute(
                    f"""
                    SELECT
                        files.path,
                        files.filename,
                        files.extension,
                        files.byte_size,
                        files.sha256,
                        files.filename_dates,
                        files.media_creation_dates,
                        files.filesystem_dates,
                        files.capture_timestamp,
                        files.capture_timestamp_source,
                        files.gps_latitude,
                        files.gps_longitude,
                        files.gps_source,
                        files.quality_score,
                        files.quality_evidence,
                        GROUP_CONCAT(DISTINCT dates.source) AS evidence_sources,
                        MIN(ABS(julianday(dates.date) - julianday(?))) AS date_distance
                    FROM photo_library_dates AS dates
                    JOIN photo_library_files AS files
                        ON files.path = dates.file_path
                    WHERE dates.source IN ({source_placeholders})
                        AND dates.date IN ({placeholders})
                        {folder_where}
                    GROUP BY files.path
                    ORDER BY
                        date_distance,
                        CASE WHEN files.capture_timestamp = '' THEN 1 ELSE 0 END,
                        files.capture_timestamp,
                        files.quality_score DESC,
                        files.byte_size DESC,
                        files.path
                    LIMIT ?
                    """,
                    (
                        target_date,
                        *date_sources,
                        *tier_dates,
                        *folder_params,
                        -1,
                    ),
                ).fetchall()
                for row in tier_rows:
                    if Path(row["path"]).exists():
                        rows_by_path.setdefault(str(row["path"]), row)
                if rows_by_path and any(not _is_png_index_row(row) for row in rows_by_path.values()):
                    break
            rows = list(rows_by_path.values())
            if limit_per_date is not None:
                rows = rows[:limit_per_date]
            candidates = []
            for row in rows:
                path = Path(row["path"])
                candidate = _candidate_from_index_row(row)
                distance = int(row["date_distance"] or 0)
                candidate["date_distance"] = distance
                if distance:
                    candidate["evidence"] += f";date_within_{distance}_days"
                candidates.append(candidate)
            candidates_by_date[target_date] = candidates
        return candidates_by_date
    finally:
        connection.close()


def _folder_path_prefix(folder_root: Path) -> str:
    resolved = str(folder_root.expanduser().resolve())
    return resolved if resolved.endswith(os.sep) else f"{resolved}{os.sep}"


def _canonical_index_roots(index_roots: list[Path]) -> list[Path]:
    roots: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for root in index_roots:
        stored_root = root.expanduser()
        resolved = stored_root.resolve()
        root_text = str(resolved)
        if root_text in seen:
            continue
        seen.add(root_text)
        roots.append((stored_root, root_text))
    redundant: set[str] = set()
    root_texts = [resolved for _, resolved in roots]
    for root_text in root_texts:
        for possible_parent in root_texts:
            if root_text != possible_parent and _path_is_within(root_text, possible_parent):
                redundant.add(root_text)
                break
    return [stored_root for stored_root, resolved in roots if resolved not in redundant]


def _path_is_within(path_text: str, parent_text: str) -> bool:
    parent_prefix = parent_text if parent_text.endswith(os.sep) else f"{parent_text}{os.sep}"
    return path_text.startswith(parent_prefix)


def _iter_image_files(root: Path) -> Iterator[ScannedPhotoFile]:
    root_scan = root.expanduser()
    root_resolved = root_scan.resolve()
    pending = [(str(root_scan), str(root_resolved))]
    while pending:
        scan_dir, resolved_dir = pending.pop()
        try:
            with os.scandir(scan_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append((entry.path, os.path.join(resolved_dir, entry.name)))
                            continue
                        if not entry.name.lower().endswith(tuple(IMAGE_EXTENSIONS)):
                            continue
                        if not entry.is_file():
                            continue
                        entry_stat = entry.stat()
                    except OSError:
                        continue
                    path = Path(entry.path)
                    if entry.is_symlink():
                        path_text = str(path.resolve())
                    else:
                        path_text = os.path.join(resolved_dir, entry.name)
                    yield ScannedPhotoFile(path=path, path_text=path_text, stat=entry_stat)
        except OSError:
            continue


def _relabel_indexed_child_roots(connection: sqlite3.Connection, index_roots: list[Path]) -> int:
    root_pairs = [(str(root), str(root.resolve())) for root in index_roots]
    if not root_pairs:
        return 0
    updates: list[tuple[str, str]] = []
    for path_text, root_text in connection.execute("SELECT path, root FROM photo_library_files"):
        path_text = str(path_text)
        root_text = str(root_text or "")
        for stored_root, resolved_root in root_pairs:
            if root_text != stored_root and _path_is_within(path_text, resolved_root):
                updates.append((stored_root, path_text))
                break
    if updates:
        connection.executemany("UPDATE photo_library_files SET root = ? WHERE path = ?", updates)
    return len(updates)


def _candidate_date_tiers(target_date: str) -> list[list[str]]:
    target = dt.date.fromisoformat(target_date)
    return [
        [target.isoformat()],
        [(target - dt.timedelta(days=1)).isoformat(), (target + dt.timedelta(days=1)).isoformat()],
        [
            (target - dt.timedelta(days=3)).isoformat(),
            (target - dt.timedelta(days=2)).isoformat(),
            (target + dt.timedelta(days=2)).isoformat(),
            (target + dt.timedelta(days=3)).isoformat(),
        ],
    ]


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_library_files (
            path TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            filename TEXT NOT NULL,
            extension TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            filesystem_mtime_utc TEXT NOT NULL,
            filesystem_mtime_ns INTEGER NOT NULL DEFAULT 0,
            filename_dates TEXT NOT NULL,
            media_creation_dates TEXT NOT NULL,
            filesystem_dates TEXT NOT NULL,
            capture_timestamp TEXT NOT NULL DEFAULT '',
            capture_timestamp_source TEXT NOT NULL DEFAULT '',
            gps_latitude REAL,
            gps_longitude REAL,
            gps_source TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            metadata_version TEXT NOT NULL DEFAULT '',
            quality_score INTEGER NOT NULL DEFAULT 0,
            quality_evidence TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(connection, "photo_library_files", "filesystem_mtime_ns", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "photo_library_files", "quality_score", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "photo_library_files", "quality_evidence", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "photo_library_files", "capture_timestamp", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "photo_library_files", "capture_timestamp_source", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "photo_library_files", "gps_latitude", "REAL")
    _ensure_column(connection, "photo_library_files", "gps_longitude", "REAL")
    _ensure_column(connection, "photo_library_files", "gps_source", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "photo_library_files", "sha256", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "photo_library_files", "metadata_version", "TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_library_dates (
            file_path TEXT NOT NULL,
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (file_path, date, source),
            FOREIGN KEY (file_path) REFERENCES photo_library_files(path) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_photo_library_dates_date ON photo_library_dates(date)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_photo_library_dates_source_date_path "
        "ON photo_library_dates(source, date, file_path)"
    )
    _ensure_photo_library_file_indexes(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_library_index_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            roots TEXT NOT NULL,
            reset INTEGER NOT NULL,
            scanned_file_count INTEGER NOT NULL,
            indexed_file_count INTEGER NOT NULL,
            skipped_file_count INTEGER NOT NULL,
            file_count_before INTEGER NOT NULL,
            file_count_after INTEGER NOT NULL,
            new_file_count INTEGER NOT NULL
        )
        """
    )


def _ensure_photo_library_file_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_photo_library_files_sha_size "
        "ON photo_library_files(sha256, byte_size)"
    )


def _reset_index(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM photo_library_dates")
    connection.execute("DELETE FROM photo_library_files")


def _indexed_file_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM photo_library_files").fetchone()[0])


def _indexed_roots(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT root
            FROM photo_library_files
            WHERE root != ''
            GROUP BY root
            ORDER BY root
            """
        )
        if str(row[0]).strip()
    ]


def _indexed_file_snapshot(
    connection: sqlite3.Connection,
    index_roots: list[Path] | None = None,
) -> dict[str, IndexedFileSnapshot]:
    root_prefixes = []
    for root in index_roots or []:
        root_text = str(root.resolve())
        root_prefixes.append(root_text if root_text.endswith(os.sep) else f"{root_text}{os.sep}")
    query = """
        SELECT path, filesystem_mtime_utc, filesystem_mtime_ns, byte_size, metadata_version, sha256
        FROM photo_library_files
    """
    params: list[str] = []
    if root_prefixes:
        clauses = []
        for prefix in root_prefixes:
            clauses.append("path LIKE ? ESCAPE '\\'")
            params.append(f"{_sqlite_like_escape(prefix)}%")
        query += " WHERE " + " OR ".join(clauses)
    snapshot: dict[str, IndexedFileSnapshot] = {}
    for path, mtime, mtime_ns, byte_size, metadata_version, sha256 in connection.execute(query, params):
        path_text = str(path)
        snapshot[path_text] = IndexedFileSnapshot(
            filesystem_mtime_utc=str(mtime or ""),
            filesystem_mtime_ns=int(mtime_ns or 0),
            byte_size=int(byte_size or 0),
            metadata_version=str(metadata_version or ""),
            sha256=str(sha256 or ""),
        )
    return snapshot


def _sqlite_like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _can_skip_indexed_file(
    snapshot: IndexedFileSnapshot | None,
    filesystem_mtime_utc: str,
    filesystem_mtime_ns: int,
    byte_size: int,
) -> bool:
    if snapshot is None:
        return False
    if snapshot.metadata_version != PHOTO_INDEX_METADATA_VERSION or not snapshot.sha256:
        return False
    if snapshot.byte_size != byte_size:
        return False
    if snapshot.filesystem_mtime_ns:
        return snapshot.filesystem_mtime_ns == filesystem_mtime_ns
    return snapshot.filesystem_mtime_utc == filesystem_mtime_utc


def _capture_source_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        str(source): int(count)
        for source, count in connection.execute(
            """
            SELECT capture_timestamp_source, COUNT(*)
            FROM photo_library_files
            WHERE capture_timestamp_source != ''
            GROUP BY capture_timestamp_source
            """
        )
    }


def _record_index_run(
    connection: sqlite3.Connection,
    started_at: str,
    finished_at: str,
    roots: list[Path],
    reset: bool,
    scanned: int,
    indexed: int,
    skipped: int,
    file_count_before: int,
    file_count_after: int,
    new_file_count: int,
) -> None:
    connection.execute(
        """
        INSERT INTO photo_library_index_runs (
            run_id,
            started_at,
            finished_at,
            roots,
            reset,
            scanned_file_count,
            indexed_file_count,
            skipped_file_count,
            file_count_before,
            file_count_after,
            new_file_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"photo-index:{started_at}",
            started_at,
            finished_at,
            ";".join(str(root) for root in roots),
            int(reset),
            scanned,
            indexed,
            skipped,
            file_count_before,
            file_count_after,
            new_file_count,
        ),
    )


def _write_batch(
    connection: sqlite3.Connection,
    rows: list[dict[str, object]],
    related_db_path: Path,
) -> tuple[int, int]:
    moved = 0
    for row in rows:
        old_path = _missing_content_match_path(connection, row)
        if old_path:
            new_path = str(row["path"])
            _move_indexed_path(connection, old_path, new_path)
            _move_related_photo_path(related_db_path, old_path, new_path)
            moved += 1
        connection.execute(
            """
            INSERT INTO photo_library_files (
                path,
                root,
                filename,
                extension,
                byte_size,
                filesystem_mtime_utc,
                filesystem_mtime_ns,
                filename_dates,
                media_creation_dates,
                filesystem_dates,
                capture_timestamp,
                capture_timestamp_source,
                gps_latitude,
                gps_longitude,
                gps_source,
                sha256,
                metadata_version,
                quality_score,
                quality_evidence,
                indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path)
            DO UPDATE SET
                root = excluded.root,
                filename = excluded.filename,
                extension = excluded.extension,
                byte_size = excluded.byte_size,
                filesystem_mtime_utc = excluded.filesystem_mtime_utc,
                filesystem_mtime_ns = excluded.filesystem_mtime_ns,
                filename_dates = excluded.filename_dates,
                media_creation_dates = excluded.media_creation_dates,
                filesystem_dates = excluded.filesystem_dates,
                capture_timestamp = excluded.capture_timestamp,
                capture_timestamp_source = excluded.capture_timestamp_source,
                gps_latitude = excluded.gps_latitude,
                gps_longitude = excluded.gps_longitude,
                gps_source = excluded.gps_source,
                sha256 = excluded.sha256,
                metadata_version = excluded.metadata_version,
                quality_score = excluded.quality_score,
                quality_evidence = excluded.quality_evidence,
                indexed_at = excluded.indexed_at
            """,
            (
                row["path"],
                row["root"],
                row["filename"],
                row["extension"],
                row["byte_size"],
                row["filesystem_mtime_utc"],
                row["filesystem_mtime_ns"],
                row["filename_dates"],
                row["media_creation_dates"],
                row["filesystem_dates"],
                row["capture_timestamp"],
                row["capture_timestamp_source"],
                row["gps_latitude"],
                row["gps_longitude"],
                row["gps_source"],
                row["sha256"],
                row["metadata_version"],
                row["quality_score"],
                row["quality_evidence"],
                row["indexed_at"],
            ),
        )
        connection.execute("DELETE FROM photo_library_dates WHERE file_path = ?", (row["path"],))
        for source, dates in row["date_sources"].items():
            for value in dates:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO photo_library_dates (file_path, date, source)
                    VALUES (?, ?, ?)
                    """,
                    (row["path"], value, source),
                )
    return len(rows), moved


def _move_candidate_row(
    root: Path,
    path: Path,
    stat: os.stat_result,
    path_text: str,
    filesystem_mtime_utc: str,
    filesystem_mtime_ns: int,
) -> dict[str, object] | None:
    try:
        sha256 = _sha256_file(path)
    except OSError:
        return None
    return {
        "path": path_text,
        "root": str(root),
        "filename": path.name,
        "extension": path.suffix.lower(),
        "byte_size": stat.st_size,
        "filesystem_mtime_utc": filesystem_mtime_utc,
        "filesystem_mtime_ns": filesystem_mtime_ns,
        "sha256": sha256,
        "metadata_version": PHOTO_INDEX_METADATA_VERSION,
        "indexed_at": dt.datetime.now(dt.UTC).isoformat(),
    }


def _relink_moved_row(
    connection: sqlite3.Connection,
    row: dict[str, object],
    related_db_path: Path,
) -> bool:
    old_path = _missing_content_match_path(connection, row)
    if not old_path:
        return False
    new_path = str(row["path"])
    _move_indexed_path(connection, old_path, new_path)
    connection.execute(
        """
        UPDATE photo_library_files
        SET root = ?,
            filename = ?,
            extension = ?,
            byte_size = ?,
            filesystem_mtime_utc = ?,
            filesystem_mtime_ns = ?,
            sha256 = ?,
            metadata_version = ?,
            indexed_at = ?
        WHERE path = ?
        """,
        (
            row["root"],
            row["filename"],
            row["extension"],
            int(row["byte_size"]),
            row["filesystem_mtime_utc"],
            int(row["filesystem_mtime_ns"]),
            row["sha256"],
            row["metadata_version"],
            row["indexed_at"],
            new_path,
        ),
    )
    _move_related_photo_path(related_db_path, old_path, new_path, str(row["root"]))
    return True


def _missing_content_match_path(connection: sqlite3.Connection, row: dict[str, object]) -> str:
    new_path = str(row["path"])
    sha256 = str(row.get("sha256") or "").strip()
    if not sha256:
        return ""
    if connection.execute(
        "SELECT 1 FROM photo_library_files WHERE path = ?",
        (new_path,),
    ).fetchone():
        return ""
    candidates = connection.execute(
        """
        SELECT path
        FROM photo_library_files
        WHERE sha256 = ?
            AND byte_size = ?
            AND path != ?
        ORDER BY path
        """,
        (sha256, int(row["byte_size"]), new_path),
    ).fetchall()
    for candidate in candidates:
        old_path = str(candidate[0])
        if not Path(old_path).exists():
            return old_path
    return ""


def _move_indexed_path(connection: sqlite3.Connection, old_path: str, new_path: str) -> None:
    connection.execute(
        "UPDATE photo_library_dates SET file_path = ? WHERE file_path = ?",
        (new_path, old_path),
    )
    connection.execute(
        "UPDATE photo_library_files SET path = ? WHERE path = ?",
        (new_path, old_path),
    )


def _prune_missing_scanned_files(
    connection: sqlite3.Connection,
    indexed_paths: Iterable[str],
    seen_paths: set[str],
    related_db_path: Path,
) -> int:
    stale_candidates = sorted(str(path) for path in indexed_paths if str(path) not in seen_paths)
    stale_paths = _existing_indexed_paths(connection, stale_candidates)
    if not stale_paths:
        return 0
    connection.executemany(
        "DELETE FROM photo_library_dates WHERE file_path = ?",
        [(path,) for path in stale_paths],
    )
    connection.executemany(
        "DELETE FROM photo_library_files WHERE path = ?",
        [(path,) for path in stale_paths],
    )
    _prune_related_photo_paths(related_db_path, stale_paths)
    return len(stale_paths)


def _existing_indexed_paths(connection: sqlite3.Connection, paths: list[str]) -> list[str]:
    if not paths:
        return []
    existing: list[str] = []
    for start in range(0, len(paths), 500):
        chunk = paths[start : start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT path FROM photo_library_files WHERE path IN ({placeholders}) ORDER BY path",
            chunk,
        )
        existing.extend(str(row[0]) for row in rows)
    return sorted(existing)


def _move_related_photo_path(
    related_db_path: Path,
    old_path: str,
    new_path: str,
    new_root: str | None = None,
) -> None:
    if not related_db_path.exists():
        return
    connection = sqlite3.connect(related_db_path, timeout=60)
    try:
        _move_related_table_path(connection, "broad_descriptors", old_path, new_path, new_root)
        _move_related_table_path(connection, "rough_prefilter_features", old_path, new_path, new_root)
        _move_related_table_path(connection, "rough_prefilter_bands", old_path, new_path)
        _move_related_match_links(connection, old_path, new_path)
        connection.commit()
    finally:
        connection.close()


def _prune_related_photo_paths(related_db_path: Path, paths: list[str]) -> None:
    if not related_db_path.exists() or not paths:
        return
    connection = sqlite3.connect(related_db_path, timeout=60)
    try:
        for table in ("broad_descriptors", "rough_prefilter_features", "rough_prefilter_bands"):
            if _sqlite_table_exists(connection, table):
                connection.executemany(f"DELETE FROM {table} WHERE path = ?", [(path,) for path in paths])
        if _sqlite_table_exists(connection, "broad_match_results"):
            connection.executemany(
                "DELETE FROM broad_match_results WHERE candidate_path = ?",
                [(path,) for path in paths],
            )
        if _sqlite_table_exists(connection, "broad_match_entry_decisions"):
            connection.executemany(
                "DELETE FROM broad_match_entry_decisions WHERE candidate_path = ?",
                [(path,) for path in paths],
            )
        connection.commit()
    finally:
        connection.close()


def _move_related_table_path(
    connection: sqlite3.Connection,
    table: str,
    old_path: str,
    new_path: str,
    new_root: str | None = None,
) -> None:
    if not _sqlite_table_exists(connection, table):
        return
    if connection.execute(f"SELECT 1 FROM {table} WHERE path = ?", (new_path,)).fetchone():
        connection.execute(f"DELETE FROM {table} WHERE path = ?", (old_path,))
        return
    columns = _sqlite_table_columns(connection, table)
    assignments = ["path = ?"]
    values: list[object] = [new_path]
    if "root" in columns and new_root is not None:
        assignments.append("root = ?")
        values.append(new_root)
    if "filename" in columns:
        assignments.append("filename = ?")
        values.append(Path(new_path).name)
    values.append(old_path)
    connection.execute(
        f"UPDATE {table} SET {', '.join(assignments)} WHERE path = ?",
        values,
    )


def _move_related_match_links(connection: sqlite3.Connection, old_path: str, new_path: str) -> None:
    if _sqlite_table_exists(connection, "broad_match_results"):
        connection.execute(
            """
            UPDATE OR IGNORE broad_match_results
            SET candidate_path = ?,
                candidate_filename = ?
            WHERE candidate_path = ?
            """,
            (new_path, Path(new_path).name, old_path),
        )
    if _sqlite_table_exists(connection, "broad_match_entry_decisions"):
        connection.execute(
            """
            UPDATE broad_match_entry_decisions
            SET candidate_path = ?
            WHERE candidate_path = ?
            """,
            (new_path, old_path),
        )


def _sqlite_table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def _sqlite_table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _index_row(
    root: Path,
    path: Path,
    stat: os.stat_result | None = None,
    path_text: str | None = None,
    filesystem_mtime_utc: str | None = None,
    filesystem_mtime_ns: int | None = None,
) -> dict[str, object] | None:
    try:
        if stat is None:
            stat = path.stat()
        if path_text is None:
            path_text = str(path.resolve())
        if filesystem_mtime_utc is None:
            filesystem_mtime_utc = _filesystem_mtime_utc(stat)
        if filesystem_mtime_ns is None:
            filesystem_mtime_ns = _filesystem_mtime_ns(stat)
        filename_dates = _filename_dates(path)
        filename_timestamps = _filename_timestamps(path)
        media_dates = _jpeg_exif_dates(path)
        filesystem_dates = _filesystem_dates(stat)
        sha256 = _sha256_file(path)
    except OSError:
        return None
    quality_score, quality_evidence = _quality_rank(path.name)
    filename_timestamp = min(filename_timestamps) if filename_timestamps else ""
    return {
        "path": path_text,
        "root": str(root),
        "filename": path.name,
        "extension": path.suffix.lower(),
        "byte_size": stat.st_size,
        "filesystem_mtime_utc": filesystem_mtime_utc,
        "filesystem_mtime_ns": filesystem_mtime_ns,
        "filename_dates": ";".join(sorted(filename_dates)),
        "media_creation_dates": ";".join(sorted(media_dates)),
        "filesystem_dates": ";".join(sorted(filesystem_dates)),
        "capture_timestamp": filename_timestamp,
        "capture_timestamp_source": "filename_timestamp" if filename_timestamp else "",
        "gps_latitude": None,
        "gps_longitude": None,
        "gps_source": "",
        "sha256": sha256,
        "metadata_version": PHOTO_INDEX_METADATA_VERSION,
        "quality_score": quality_score,
        "quality_evidence": ";".join(quality_evidence),
        "indexed_at": dt.datetime.now(dt.UTC).isoformat(),
        "date_sources": {
            "filename_date": filename_dates,
            "media_creation_date": media_dates,
            "filesystem_date": filesystem_dates,
        },
    }


def enrich_candidate_queue(index_db: Path, queue_path: Path) -> int:
    if not index_db.exists() or not queue_path.exists():
        return 0
    with queue_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    for field in ("capture_timestamp", "capture_timestamp_source", "date_distance"):
        if field not in fieldnames:
            fieldnames.append(field)
    relinked_paths = _relink_missing_queue_candidates(index_db, rows)
    candidate_paths = sorted(
        {
            str(Path(row["candidate_path"]).resolve())
            for row in rows
            if row.get("candidate_path", "").strip()
        }
    )
    metadata: dict[str, tuple[str, str]] = {}
    connection = sqlite3.connect(index_db, timeout=60)
    try:
        connection.execute("PRAGMA busy_timeout = 60000")
        for start in range(0, len(candidate_paths), 500):
            chunk = candidate_paths[start : start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            for path, timestamp, source in connection.execute(
                f"SELECT path, capture_timestamp, capture_timestamp_source FROM photo_library_files WHERE path IN ({placeholders})",
                chunk,
            ):
                metadata[str(Path(path).resolve())] = (str(timestamp), str(source))
    finally:
        connection.close()
    updated = 0
    for row in rows:
        path_text = row.get("candidate_path", "").strip()
        if not path_text:
            continue
        capture = metadata.get(str(Path(path_text).resolve()))
        if not capture:
            continue
        timestamp, source = capture
        row["capture_timestamp"] = timestamp
        row["capture_timestamp_source"] = source
        row["date_distance"] = _capture_date_distance(row.get("entry_date", ""), timestamp)
        if timestamp:
            updated += 1
    if relinked_paths:
        if "candidate_filename" not in fieldnames:
            fieldnames.append("candidate_filename")
        for row in rows:
            path_text = row.get("candidate_path", "").strip()
            if path_text and str(Path(path_text).resolve()) in relinked_paths:
                row["candidate_filename"] = Path(path_text).name
    temp_path = queue_path.with_suffix(queue_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(queue_path)
    return updated


def _relink_missing_queue_candidates(index_db: Path, rows: list[dict[str, str]]) -> set[str]:
    missing_rows: list[tuple[dict[str, str], Path, str, int]] = []
    keys: set[tuple[str, int]] = set()
    for row in rows:
        path_text = row.get("candidate_path", "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        if path.exists():
            continue
        sha256 = row.get("candidate_sha256", "").strip().lower()
        if len(sha256) != 64:
            continue
        try:
            byte_size = int(row.get("byte_size", ""))
        except ValueError:
            continue
        if byte_size < 0:
            continue
        missing_rows.append((row, path, sha256, byte_size))
        keys.add((sha256, byte_size))
    if not missing_rows:
        return set()
    matches: dict[tuple[str, int], list[str]] = {key: [] for key in keys}
    connection = sqlite3.connect(index_db, timeout=60)
    try:
        connection.execute("PRAGMA busy_timeout = 60000")
        _ensure_photo_library_file_indexes(connection)
        for sha256, byte_size in sorted(keys):
            for (path_text,) in connection.execute(
                """
                SELECT path
                FROM photo_library_files
                WHERE sha256 = ?
                    AND byte_size = ?
                ORDER BY path
                """,
                (sha256, byte_size),
            ):
                if Path(path_text).exists():
                    matches[(sha256, byte_size)].append(str(path_text))
    finally:
        connection.close()

    relinked: set[str] = set()
    assigned: dict[tuple[str, int], set[str]] = {}
    for row, old_path, sha256, byte_size in sorted(missing_rows, key=lambda item: str(item[1])):
        key = (sha256, byte_size)
        new_path = _best_queue_relink_path(old_path, matches.get(key, []), assigned.setdefault(key, set()))
        if not new_path:
            continue
        row["candidate_path"] = new_path
        assigned[key].add(new_path)
        relinked.add(str(Path(new_path).resolve()))
    return relinked


def _best_queue_relink_path(old_path: Path, candidates: list[str], assigned: set[str]) -> str:
    if not candidates:
        return ""
    old_parent = str(old_path.parent.resolve())
    same_parent = [
        candidate
        for candidate in candidates
        if str(Path(candidate).parent.resolve()) == old_parent
    ]
    for candidate in sorted(same_parent or candidates):
        if candidate not in assigned:
            return candidate
    return sorted(same_parent or candidates)[0]


def _capture_date_distance(entry_date: str, capture_timestamp: str) -> str:
    if not entry_date or not capture_timestamp:
        return ""
    try:
        target = dt.date.fromisoformat(entry_date)
        captured = dt.date.fromisoformat(capture_timestamp[:10])
    except ValueError:
        return ""
    return str(abs((captured - target).days))


def _candidate_from_index_row(row: sqlite3.Row) -> dict[str, object]:
    path = Path(row["path"])
    evidence = ["photo_library_index"]
    for source in str(row["evidence_sources"] or "").replace(",", ";").split(";"):
        if source and source not in evidence:
            evidence.append(source)
    for source in str(row["quality_evidence"] or "").split(";"):
        if source and source not in evidence:
            evidence.append(source)
    return {
        "candidate_path": str(path),
        "candidate_filename": row["filename"],
        "candidate_sha256": row["sha256"],
        "byte_size": int(row["byte_size"]),
        "mime_type": _mime_type(path),
        "filename_dates": row["filename_dates"],
        "media_creation_dates": row["media_creation_dates"],
        "filesystem_dates": row["filesystem_dates"],
        "capture_timestamp": row["capture_timestamp"],
        "capture_timestamp_source": row["capture_timestamp_source"],
        "gps_latitude": row["gps_latitude"],
        "gps_longitude": row["gps_longitude"],
        "gps_source": row["gps_source"],
        "evidence": ";".join(evidence),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_png_index_row(row: sqlite3.Row) -> bool:
    return str(row["extension"] or "").strip().lower() == ".png"


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _quality_rank(filename: str) -> tuple[int, list[str]]:
    tokens = _quality_tokens(filename)
    score = 0
    evidence = []
    for token, value in QUALITY_POSITIVE_TERMS.items():
        if token in tokens:
            score += value
            evidence.append(f"quality_positive_{token}")
    for token, value in QUALITY_NEGATIVE_TERMS.items():
        if token in tokens:
            score += value
            evidence.append(f"quality_negative_{token}")
    return score, evidence


def _quality_tokens(filename: str) -> set[str]:
    stem = Path(filename).stem.lower()
    compact = re.sub(r"[^a-z0-9]+", "", stem)
    separated = set(re.split(r"[^a-z0-9]+", stem))
    tokens = {token for token in separated if token}
    tokens.add(compact)
    return tokens


def _filename_dates(path: Path) -> set[str]:
    text = path.name
    dates = set()
    for pattern in FILENAME_DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                dates.add(dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat())
            except ValueError:
                pass
    return dates


def _filename_timestamps(path: Path) -> set[str]:
    timestamps = set()
    for pattern in FILENAME_TIMESTAMP_PATTERNS:
        for match in pattern.finditer(path.name):
            try:
                value = dt.datetime(*(int(part) for part in match.groups()))
            except ValueError:
                continue
            timestamps.add(value.isoformat())
    return timestamps


def _enrich_capture_metadata(rows: list[dict[str, object]]) -> None:
    metadata = _exiftool_photo_metadata([Path(str(row["path"])) for row in rows])
    for row in rows:
        photo_metadata = metadata.get(str(Path(str(row["path"])).resolve()))
        if not photo_metadata:
            continue
        if photo_metadata.capture_timestamp and photo_metadata.capture_timestamp_source:
            row["capture_timestamp"] = photo_metadata.capture_timestamp
            row["capture_timestamp_source"] = photo_metadata.capture_timestamp_source
            capture_date = photo_metadata.capture_timestamp[:10]
            media_dates = {value for value in str(row["media_creation_dates"]).split(";") if value}
            media_dates.add(capture_date)
            row["media_creation_dates"] = ";".join(sorted(media_dates))
            date_sources = row["date_sources"]
            if isinstance(date_sources, dict):
                date_sources["media_creation_date"] = media_dates
        if photo_metadata.gps_latitude is not None and photo_metadata.gps_longitude is not None:
            row["gps_latitude"] = photo_metadata.gps_latitude
            row["gps_longitude"] = photo_metadata.gps_longitude
            row["gps_source"] = photo_metadata.gps_source


def _exiftool_capture_metadata(paths: list[Path]) -> dict[str, tuple[str, str]]:
    return {
        path: (metadata.capture_timestamp, metadata.capture_timestamp_source)
        for path, metadata in _exiftool_photo_metadata(paths).items()
        if metadata.capture_timestamp and metadata.capture_timestamp_source
    }


def _exiftool_photo_metadata(paths: list[Path]) -> dict[str, ExiftoolPhotoMetadata]:
    exiftool = shutil.which("exiftool")
    if not exiftool or not paths:
        return {}
    command = [
        exiftool,
        "-json",
        "-G1",
        "-s",
        "-n",
        "-api",
        "QuickTimeUTC=1",
        "-DateTimeOriginal",
        "-DateTimeDigitized",
        "-CreateDate",
        "-ContentCreateDate",
        "-CreationDate",
        "-MediaCreateDate",
        "-TrackCreateDate",
        "-GPSDateTime",
        "-GPSLatitude",
        "-GPSLongitude",
        "-GPSLatitudeRef",
        "-GPSLongitudeRef",
        *[str(path) for path in paths],
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if not completed.stdout.strip():
        if completed.returncode:
            raise RuntimeError("ExifTool could not read capture metadata for an index batch.")
        return {}
    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ExifTool returned invalid capture metadata.") from exc
    result = {}
    for record in records:
        timestamp, source = _best_exiftool_capture_timestamp(record)
        latitude, longitude, gps_source = _gps_coordinates_from_exiftool_record(record)
        source_file = str(record.get("SourceFile", ""))
        if source_file and (timestamp or (latitude is not None and longitude is not None)):
            result[str(Path(source_file).resolve())] = ExiftoolPhotoMetadata(
                capture_timestamp=timestamp,
                capture_timestamp_source=source,
                gps_latitude=latitude,
                gps_longitude=longitude,
                gps_source=gps_source,
            )
    return result


def _best_exiftool_capture_timestamp(record: dict[str, object]) -> tuple[str, str]:
    accepted_fields = [
        (("EXIF:DateTimeOriginal", "ExifIFD:DateTimeOriginal", "IFD0:DateTimeOriginal"), "exif_datetime_original"),
        (("XMP-exif:DateTimeOriginal",), "xmp_datetime_original"),
        (("EXIF:DateTimeDigitized", "ExifIFD:CreateDate", "EXIF:CreateDate"), "exif_datetime_digitized"),
        (("XMP-exif:DateTimeDigitized",), "xmp_datetime_digitized"),
        (("XMP-xmp:CreateDate",), "xmp_create_date"),
        (("Composite:GPSDateTime",), "gps_datetime_utc"),
        (("QuickTime:ContentCreateDate",), "quicktime_content_create_date"),
        (("QuickTime:CreationDate",), "quicktime_creation_date"),
        (("QuickTime:MediaCreateDate",), "quicktime_media_create_date"),
        (("QuickTime:CreateDate",), "quicktime_create_date"),
        (("QuickTime:TrackCreateDate",), "quicktime_track_create_date"),
    ]
    for keys, source in accepted_fields:
        for key in keys:
            value = record.get(key)
            if isinstance(value, list):
                value = value[0] if value else ""
            timestamp = _normalize_capture_timestamp(str(value or ""))
            if timestamp:
                return timestamp, source
    return "", ""


def _gps_coordinates_from_exiftool_record(record: dict[str, object]) -> tuple[float | None, float | None, str]:
    latitude_keys = (
        "Composite:GPSLatitude",
        "GPS:GPSLatitude",
        "EXIF:GPSLatitude",
        "XMP-exif:GPSLatitude",
    )
    longitude_keys = (
        "Composite:GPSLongitude",
        "GPS:GPSLongitude",
        "EXIF:GPSLongitude",
        "XMP-exif:GPSLongitude",
    )
    latitude_key, latitude = _first_numeric_gps_value(record, latitude_keys)
    longitude_key, longitude = _first_numeric_gps_value(record, longitude_keys)
    if latitude is None or longitude is None:
        return None, None, ""
    latitude = _apply_gps_ref(latitude, record, ("Composite:GPSLatitudeRef", "GPS:GPSLatitudeRef", "EXIF:GPSLatitudeRef"))
    longitude = _apply_gps_ref(
        longitude,
        record,
        ("Composite:GPSLongitudeRef", "GPS:GPSLongitudeRef", "EXIF:GPSLongitudeRef"),
    )
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, None, ""
    source = "composite_gps" if latitude_key.startswith("Composite:") or longitude_key.startswith("Composite:") else "embedded_gps"
    return latitude, longitude, source


def _first_numeric_gps_value(record: dict[str, object], keys: tuple[str, ...]) -> tuple[str, float | None]:
    for key in keys:
        value = _numeric_gps_value(record.get(key))
        if value is not None:
            return key, value
    return "", None


def _numeric_gps_value(value: object) -> float | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    direction = text[-1:].upper()
    if direction in {"N", "S", "E", "W"}:
        text = text[:-1].strip()
    try:
        number = float(text)
    except ValueError:
        return None
    if direction in {"S", "W"} and number > 0:
        number = -number
    return number


def _apply_gps_ref(value: float, record: dict[str, object], keys: tuple[str, ...]) -> float:
    for key in keys:
        ref = str(record.get(key, "")).strip().upper()
        if ref in {"S", "W"} and value > 0:
            return -value
        if ref in {"N", "E"} and value < 0:
            return abs(value)
    return value


def _normalize_capture_timestamp(value: str) -> str:
    match = re.match(
        r"^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
        r"(?:\.(\d+))?(?:\s*(Z|[+-]\d{2}:?\d{2}))?$",
        value.strip(),
    )
    if not match:
        return ""
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    normalized = f"{year}-{month}-{day}T{hour}:{minute}:{second}"
    if fraction:
        normalized += f".{fraction}"
    if offset == "Z":
        normalized += "+00:00"
    elif offset:
        normalized += offset if ":" in offset else f"{offset[:3]}:{offset[3:]}"
    try:
        dt.datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    return normalized


def _jpeg_exif_dates(path: Path) -> set[str]:
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return set()
    dates = set()
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return dates
            while True:
                marker_prefix = handle.read(1)
                if marker_prefix != b"\xff":
                    return dates
                marker = handle.read(1)
                if not marker:
                    return dates
                marker_value = marker[0]
                if marker_value in {0xD8, 0xD9}:
                    continue
                length_bytes = handle.read(2)
                if len(length_bytes) != 2:
                    return dates
                segment_length = int.from_bytes(length_bytes, "big")
                payload_length = segment_length - 2
                if payload_length < 0:
                    return dates
                if marker_value == 0xE1:
                    segment = handle.read(payload_length)
                    if segment.startswith(b"Exif\x00\x00"):
                        return _tiff_dates(segment[6:])
                else:
                    handle.seek(payload_length, os.SEEK_CUR)
    except OSError:
        return set()


def _tiff_dates(data: bytes) -> set[str]:
    if len(data) < 8:
        return set()
    endian = data[:2]
    if endian == b"II":
        prefix = "<"
    elif endian == b"MM":
        prefix = ">"
    else:
        return set()
    if struct.unpack_from(prefix + "H", data, 2)[0] != 42:
        return set()
    first_ifd = struct.unpack_from(prefix + "I", data, 4)[0]
    dates, exif_offsets = _ifd_dates(data, first_ifd, prefix)
    for exif_offset in exif_offsets:
        nested_dates, _ = _ifd_dates(data, exif_offset, prefix)
        dates |= nested_dates
    return dates


def _ifd_dates(data: bytes, offset: int, prefix: str) -> tuple[set[str], list[int]]:
    if offset <= 0 or offset + 2 > len(data):
        return set(), []
    count = struct.unpack_from(prefix + "H", data, offset)[0]
    dates = set()
    exif_offsets = []
    entry_offset = offset + 2
    for index in range(count):
        item_offset = entry_offset + index * 12
        if item_offset + 12 > len(data):
            break
        tag, field_type, value_count = struct.unpack_from(prefix + "HHI", data, item_offset)
        value_or_offset = data[item_offset + 8 : item_offset + 12]
        if tag == 0x8769:
            exif_offsets.append(struct.unpack(prefix + "I", value_or_offset)[0])
        if tag in {0x9003, 0x9004} and field_type == 2:
            value = _ascii_tiff_value(data, value_or_offset, value_count, prefix)
            parsed = _exif_date(value)
            if parsed:
                dates.add(parsed)
    return dates, exif_offsets


def _ascii_tiff_value(data: bytes, value_or_offset: bytes, value_count: int, prefix: str) -> str:
    if value_count <= 4:
        payload = value_or_offset[:value_count]
    else:
        offset = struct.unpack(prefix + "I", value_or_offset)[0]
        payload = data[offset : offset + value_count]
    return payload.rstrip(b"\x00").decode("ascii", errors="ignore")


def _exif_date(value: str) -> str | None:
    match = re.match(r"^(\d{4}):(\d{2}):(\d{2})", value.strip())
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _filesystem_dates(stat: os.stat_result) -> set[str]:
    dates = set()
    for timestamp in (getattr(stat, "st_birthtime", None), stat.st_mtime):
        if timestamp is None:
            continue
        dates.add(dt.datetime.fromtimestamp(timestamp).date().isoformat())
    return dates


def _filesystem_mtime_utc(stat: os.stat_result) -> str:
    return dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC).isoformat()


def _filesystem_mtime_ns(stat: os.stat_result) -> int:
    return int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))


def _validate_date(value: str) -> None:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("target date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("target date must be YYYY-MM-DD")


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix in {".heic", ".heif"}:
        return "image/heic"
    if suffix == ".png":
        return "image/png"
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    return "application/octet-stream"


if __name__ == "__main__":
    raise SystemExit(main())
