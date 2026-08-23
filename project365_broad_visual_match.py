#!/usr/bin/env python3
"""Offline broad visual matching for unresolved Project365 originals."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import mimetypes
import os
import signal
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project365_original_matcher import IMAGE_EXTENSIONS
from project365_photo_library_index import (
    _filename_dates,
    _filesystem_dates,
    _filesystem_mtime_utc,
    _folder_path_prefix,
    _jpeg_exif_dates,
    _quality_rank,
)
from project365_visual_ranker import DEFAULT_SAMPLE_SIZE, DEFAULT_THUMBNAIL_TOOL, VisualImage, load_visual_image
from project365_visual_ranker import _view_descriptor, _visual_score


METHOD_VERSION = "broad_visual_square_window_v1"
THUMBNAIL_METHOD_VERSION = "broad_visual_square_window_thumb_v2"
DEFAULT_DB_FILENAME = "broad_visual_match.sqlite"
DEFAULT_TOP_N = 20
DEFAULT_DENSITY = 9
DEFAULT_THUMBNAIL_SIZE = 1024
DEFAULT_INDEX_WORKERS = 4
DEFAULT_RESULT_PAGE_SIZE = 50
DEFAULT_INDEX_COMMIT_INTERVAL = 100
REPORT_DIR = Path("exports") / "verification_reports"
BROAD_IMAGE_EXTENSIONS = set(IMAGE_EXTENSIONS) | {".bmp"}
_STOP_REQUESTED = False


@dataclass(frozen=True)
class BroadIndexSummary:
    db_path: str
    run_id: str
    scanned_count: int
    reused_descriptor_count: int
    indexed_descriptor_count: int
    error_count: int
    dry_run: bool
    skipped_candidate_count: int = 0


@dataclass(frozen=True)
class BroadMatchSummary:
    db_path: str
    run_id: str
    target_count: int
    scanned_count: int
    matched_entries: int
    result_count: int
    error_count: int
    dry_run: bool


@dataclass(frozen=True)
class BroadIndexWorkItem:
    candidate_index: int
    row: dict[str, Any]
    date_label: str


@dataclass(frozen=True)
class BroadIndexWorkResult:
    candidate_index: int
    row: dict[str, Any]
    date_label: str
    descriptor: dict[str, Any]
    error: str
    hash_error: str


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


def _request_stop(_signum: int, _frame: Any) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def main() -> int:
    _install_signal_handlers()
    parser = argparse.ArgumentParser(description="Run isolated broad visual matching for Project365 originals.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--broad-db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--candidate-root", action="append", default=[])
    index_parser.add_argument("--start-date", default="")
    index_parser.add_argument("--end-date", default="")
    index_parser.add_argument("--date-window-days", type=int, default=0)
    index_parser.add_argument("--density", type=int, default=DEFAULT_DENSITY)
    index_parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE)
    index_parser.add_argument("--thumbnail-tool", default=DEFAULT_THUMBNAIL_TOOL)
    index_parser.add_argument("--workers", type=int, default=DEFAULT_INDEX_WORKERS)
    index_parser.add_argument("--commit-interval", type=int, default=DEFAULT_INDEX_COMMIT_INTERVAL)
    index_parser.add_argument("--include-low-quality", action="store_true")
    index_parser.add_argument("--rebuild-stale", "--overwrite-existing", dest="rebuild_stale", action="store_true")
    index_parser.add_argument("--dry-run", action="store_true")

    confirmed_index_parser = subparsers.add_parser("index-confirmed")
    _add_scope_args(confirmed_index_parser)
    confirmed_index_parser.add_argument("--year", default="")
    confirmed_index_parser.add_argument("--density", type=int, default=DEFAULT_DENSITY)
    confirmed_index_parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE)
    confirmed_index_parser.add_argument("--thumbnail-tool", default=DEFAULT_THUMBNAIL_TOOL)
    confirmed_index_parser.add_argument("--workers", type=int, default=DEFAULT_INDEX_WORKERS)
    confirmed_index_parser.add_argument("--commit-interval", type=int, default=DEFAULT_INDEX_COMMIT_INTERVAL)
    confirmed_index_parser.add_argument("--include-low-quality", action="store_true")
    confirmed_index_parser.add_argument("--rebuild-stale", "--overwrite-existing", dest="rebuild_stale", action="store_true")
    confirmed_index_parser.add_argument("--dry-run", action="store_true")

    match_parser = subparsers.add_parser("match")
    _add_scope_args(match_parser)
    match_parser.add_argument("--candidate-scope", default="whole_indexed_library")
    match_parser.add_argument("--candidate-root", action="append", default=[])
    match_parser.add_argument("--date-window-days", type=int, default=0)
    match_parser.add_argument("--max-results", type=int, default=DEFAULT_TOP_N)
    match_parser.add_argument("--density", type=int, default=DEFAULT_DENSITY)
    match_parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE)
    match_parser.add_argument("--thumbnail-tool", default=DEFAULT_THUMBNAIL_TOOL)
    match_parser.add_argument("--include-low-quality", action="store_true")
    match_parser.add_argument("--resume-run", default="")
    match_parser.add_argument("--dry-run", action="store_true")

    benchmark_parser = subparsers.add_parser("benchmark")
    _add_scope_args(benchmark_parser)
    benchmark_parser.add_argument("--year", default="")
    benchmark_parser.add_argument("--max-results", type=int, default=DEFAULT_TOP_N)
    benchmark_parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE)
    benchmark_parser.add_argument("--thumbnail-tool", default=DEFAULT_THUMBNAIL_TOOL)
    benchmark_parser.add_argument("--report-dir", default="")

    args = parser.parse_args()
    canonical_root = Path(args.canonical_root)
    db_path = default_db_path(canonical_root, args.broad_db)
    if args.command == "index":
        summary = build_descriptor_index(
            canonical_root=canonical_root,
            db_path=db_path,
            candidate_roots=[Path(root) for root in args.candidate_root],
            start_date=args.start_date,
            end_date=args.end_date,
            date_window_days=args.date_window_days,
            density=args.density,
            thumbnail_size=args.thumbnail_size,
            thumbnail_tool=args.thumbnail_tool,
            workers=args.workers,
            commit_interval=args.commit_interval,
            include_low_quality=args.include_low_quality,
            rebuild_stale=args.rebuild_stale,
            dry_run=args.dry_run,
        )
        _print_summary("Broad visual descriptor index", summary.__dict__)
        return 0
    if args.command == "index-confirmed":
        scope = target_scope_from_args(args)
        scope.update(_year_scope(args.year))
        summary = build_confirmed_descriptor_index(
            canonical_root=canonical_root,
            db_path=db_path,
            target_scope=scope,
            density=args.density,
            thumbnail_size=args.thumbnail_size,
            thumbnail_tool=args.thumbnail_tool,
            workers=args.workers,
            commit_interval=args.commit_interval,
            include_low_quality=args.include_low_quality,
            rebuild_stale=args.rebuild_stale,
            dry_run=args.dry_run,
        )
        _print_summary("Broad visual confirmed descriptor index", summary.__dict__)
        return 130 if _STOP_REQUESTED else 0
    if args.command == "match":
        summary = run_match_batch(
            canonical_root=canonical_root,
            db_path=db_path,
            target_scope=target_scope_from_args(args),
            candidate_scope=args.candidate_scope,
            candidate_roots=[Path(root) for root in args.candidate_root],
            date_window_days=args.date_window_days,
            max_results=args.max_results,
            density=args.density,
            thumbnail_size=args.thumbnail_size,
            thumbnail_tool=args.thumbnail_tool,
            include_low_quality=args.include_low_quality,
            resume_run_id=args.resume_run,
            dry_run=args.dry_run,
        )
        _print_summary("Broad visual match batch", summary.__dict__)
        return 130 if _STOP_REQUESTED else 0
    if args.command == "benchmark":
        report = benchmark_confirmed_originals(
            canonical_root=canonical_root,
            db_path=db_path,
            target_scope={**target_scope_from_args(args), **_year_scope(args.year)},
            max_results=args.max_results,
            thumbnail_size=args.thumbnail_size,
            thumbnail_tool=args.thumbnail_tool,
            report_dir=Path(args.report_dir) if args.report_dir else canonical_root / REPORT_DIR,
        )
        _print_summary("Broad visual benchmark", report)
        return 0
    raise ValueError(f"Unsupported command: {args.command}")


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--entry-id", action="append", default=[])
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--broad-search-needed-list", default="")


def target_scope_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "entry_ids": list(args.entry_id or []),
        "start_date": str(args.start_date or ""),
        "end_date": str(args.end_date or ""),
        "broad_search_needed_list": str(args.broad_search_needed_list or ""),
    }


def _year_scope(year: str) -> dict[str, str]:
    year = str(year or "").strip()
    if not year:
        return {}
    if len(year) != 4 or not year.isdigit():
        raise ValueError("Year must use YYYY.")
    return {"start_date": f"{year}-01-01", "end_date": f"{year}-12-31"}


def default_db_path(canonical_root: Path, explicit_path: str | None = None) -> Path:
    return Path(explicit_path) if explicit_path else canonical_root / DEFAULT_DB_FILENAME


def connect_broad_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=60)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS broad_descriptors (
            path TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            filename TEXT NOT NULL,
            extension TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            filesystem_mtime_utc TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            filename_dates TEXT NOT NULL DEFAULT '',
            media_creation_dates TEXT NOT NULL DEFAULT '',
            filesystem_dates TEXT NOT NULL DEFAULT '',
            quality_score INTEGER NOT NULL DEFAULT 0,
            quality_evidence TEXT NOT NULL DEFAULT '',
            method_version TEXT NOT NULL,
            density INTEGER NOT NULL,
            descriptor_json TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS broad_descriptor_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            roots TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            total_candidate_count INTEGER NOT NULL DEFAULT 0,
            date_coverage_json TEXT NOT NULL DEFAULT '{}',
            scanned_count INTEGER NOT NULL,
            reused_descriptor_count INTEGER NOT NULL,
            indexed_descriptor_count INTEGER NOT NULL,
            skipped_candidate_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL,
            current_candidate_index INTEGER NOT NULL DEFAULT 0,
            current_candidate_path TEXT NOT NULL DEFAULT '',
            current_candidate_name TEXT NOT NULL DEFAULT '',
            current_candidate_extension TEXT NOT NULL DEFAULT '',
            current_candidate_byte_size INTEGER NOT NULL DEFAULT 0,
            current_candidate_date TEXT NOT NULL DEFAULT '',
            current_phase TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS broad_descriptor_errors (
            error_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            candidate_index INTEGER NOT NULL,
            path TEXT NOT NULL,
            filename TEXT NOT NULL,
            candidate_date TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS broad_match_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            target_scope_json TEXT NOT NULL,
            candidate_scope_json TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            target_count INTEGER NOT NULL DEFAULT 0,
            scanned_count INTEGER NOT NULL DEFAULT 0,
            matched_entries INTEGER NOT NULL DEFAULT 0,
            result_count INTEGER NOT NULL DEFAULT 0,
            reused_descriptor_count INTEGER NOT NULL DEFAULT 0,
            indexed_descriptor_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            current_entry_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS broad_match_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            project365_media_asset_id TEXT NOT NULL,
            candidate_path TEXT NOT NULL,
            candidate_filename TEXT NOT NULL,
            candidate_sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            mime_type TEXT NOT NULL,
            filename_dates TEXT NOT NULL DEFAULT '',
            media_creation_dates TEXT NOT NULL DEFAULT '',
            filesystem_dates TEXT NOT NULL DEFAULT '',
            date_distance INTEGER NOT NULL DEFAULT -1,
            score REAL NOT NULL,
            score_gap REAL NOT NULL,
            rank INTEGER NOT NULL,
            best_view TEXT NOT NULL,
            method_version TEXT NOT NULL,
            evidence TEXT NOT NULL,
            candidate_filter_reason TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            sent_to_picker_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (run_id, entry_id, candidate_path)
        );

        CREATE TABLE IF NOT EXISTS broad_match_entry_decisions (
            run_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            result_id INTEGER,
            candidate_path TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, entry_id)
        );
        CREATE INDEX IF NOT EXISTS idx_broad_results_run_entry_rank
            ON broad_match_results(run_id, entry_id, rank);
        CREATE INDEX IF NOT EXISTS idx_broad_results_entry_rank
            ON broad_match_results(entry_id, rank);
        """
    )
    _ensure_column(
        connection,
        "broad_descriptor_runs",
        "total_candidate_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        connection,
        "broad_descriptor_runs",
        "date_coverage_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    for column, definition in {
        "current_candidate_index": "INTEGER NOT NULL DEFAULT 0",
        "current_candidate_path": "TEXT NOT NULL DEFAULT ''",
        "current_candidate_name": "TEXT NOT NULL DEFAULT ''",
        "current_candidate_extension": "TEXT NOT NULL DEFAULT ''",
        "current_candidate_byte_size": "INTEGER NOT NULL DEFAULT 0",
        "current_candidate_date": "TEXT NOT NULL DEFAULT ''",
        "current_phase": "TEXT NOT NULL DEFAULT ''",
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
        "skipped_candidate_count": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _ensure_column(connection, "broad_descriptor_runs", column, definition)
    connection.commit()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def build_descriptor_index(
    canonical_root: Path,
    db_path: Path | None = None,
    candidate_roots: list[Path] | None = None,
    start_date: str = "",
    end_date: str = "",
    date_window_days: int = 0,
    density: int = DEFAULT_DENSITY,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
    workers: int = DEFAULT_INDEX_WORKERS,
    commit_interval: int = DEFAULT_INDEX_COMMIT_INTERVAL,
    include_low_quality: bool = False,
    rebuild_stale: bool = False,
    dry_run: bool = False,
) -> BroadIndexSummary:
    method_version = _descriptor_method_version(thumbnail_size)
    worker_count = _normalized_worker_count(workers)
    date_range = _expanded_date_range(start_date, end_date, date_window_days)
    effective_db_path = db_path or default_db_path(canonical_root)
    roots = _normalized_roots(candidate_roots or [])
    if not roots:
        raise ValueError("Choose at least one candidate root for the broad descriptor index.")
    run_id = _new_run_id("broad-index")
    started_at = _now()
    date_filter_source = "filesystem"
    path_roots = None
    if date_range:
        indexed_path_roots = _date_limited_photo_index_paths(
            canonical_root / "photo_library_index.sqlite",
            roots,
            date_range,
        )
        if indexed_path_roots is not None:
            path_roots = indexed_path_roots
            date_filter_source = "photo_library_index"
    if path_roots is None:
        path_roots = [(path, root) for root in roots for path in _iter_image_files(root)]
    return _build_descriptor_index_for_paths(
        effective_db_path=effective_db_path,
        run_id=run_id,
        started_at=started_at,
        path_roots=path_roots,
        roots_label=[str(root) for root in roots],
        settings={
            "density": density,
            "include_low_quality": include_low_quality,
            "rebuild_stale": rebuild_stale,
            "dry_run": dry_run,
            "start_date": start_date,
            "end_date": end_date,
            "date_window_days": date_window_days,
            "date_filter_source": date_filter_source,
            "commit_interval": commit_interval,
            "thumbnail_size": thumbnail_size,
            "thumbnail_tool": thumbnail_tool,
            "workers": worker_count,
            "method_version": method_version,
            "sha256_source": "photo_index_when_available",
        },
        density=density,
        thumbnail_size=thumbnail_size,
        thumbnail_tool=thumbnail_tool,
        workers=worker_count,
        method_version=method_version,
        date_range=date_range,
        commit_interval=commit_interval,
        include_low_quality=include_low_quality,
        rebuild_stale=rebuild_stale,
        dry_run=dry_run,
    )


def build_confirmed_descriptor_index(
    canonical_root: Path,
    db_path: Path | None = None,
    target_scope: dict[str, Any] | None = None,
    density: int = DEFAULT_DENSITY,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
    workers: int = DEFAULT_INDEX_WORKERS,
    commit_interval: int = DEFAULT_INDEX_COMMIT_INTERVAL,
    include_low_quality: bool = False,
    rebuild_stale: bool = False,
    dry_run: bool = False,
) -> BroadIndexSummary:
    method_version = _descriptor_method_version(thumbnail_size)
    worker_count = _normalized_worker_count(workers)
    effective_db_path = db_path or default_db_path(canonical_root)
    scope = target_scope or {}
    confirmed_rows = _confirmed_target_rows(canonical_root / "canonical.db", scope)
    path_roots = [
        (Path(row["confirmed_path"]), Path(row["confirmed_path"]).parent)
        for row in confirmed_rows
        if str(row.get("confirmed_path", "")).strip()
    ]
    if not path_roots:
        raise ValueError("No confirmed originals found for the selected scope.")
    run_id = _new_run_id("broad-index-confirmed")
    started_at = _now()
    return _build_descriptor_index_for_paths(
        effective_db_path=effective_db_path,
        run_id=run_id,
        started_at=started_at,
        path_roots=path_roots,
        roots_label=[f"confirmed:{scope.get('start_date', '')}:{scope.get('end_date', '')}"],
        settings={
            "source": "confirmed_originals",
            "target_scope": scope,
            "density": density,
            "include_low_quality": include_low_quality,
            "rebuild_stale": rebuild_stale,
            "dry_run": dry_run,
            "commit_interval": commit_interval,
            "thumbnail_size": thumbnail_size,
            "thumbnail_tool": thumbnail_tool,
            "workers": worker_count,
            "method_version": method_version,
            "sha256_source": "photo_index_when_available",
        },
        density=density,
        thumbnail_size=thumbnail_size,
        thumbnail_tool=thumbnail_tool,
        workers=worker_count,
        method_version=method_version,
        date_range=None,
        commit_interval=commit_interval,
        include_low_quality=include_low_quality,
        rebuild_stale=rebuild_stale,
        dry_run=dry_run,
    )


def _build_descriptor_index_for_paths(
    effective_db_path: Path,
    run_id: str,
    started_at: str,
    path_roots: list[tuple[Path, Path]],
    roots_label: list[str],
    settings: dict[str, Any],
    density: int,
    thumbnail_size: int,
    thumbnail_tool: str,
    workers: int,
    method_version: str,
    date_range: tuple[dt.date | None, dt.date | None] | None,
    commit_interval: int,
    include_low_quality: bool,
    rebuild_stale: bool,
    dry_run: bool,
) -> BroadIndexSummary:
    scanned = reused = indexed = errors = skipped = 0
    total_count = len(path_roots)
    date_coverage = _initial_date_coverage(path_roots)
    worker_count = _normalized_worker_count(workers)
    work_batch_size = max(1, min(commit_interval if commit_interval > 0 else worker_count * 8, worker_count * 8))
    connection = connect_broad_db(effective_db_path)
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    if worker_count > 1 and not dry_run:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    work_batch: list[BroadIndexWorkItem] = []

    def flush_work_batch() -> None:
        nonlocal indexed, errors
        if not work_batch:
            return
        batch = list(work_batch)
        work_batch.clear()
        first = batch[0]
        _record_index_current_candidate(
            connection,
            run_id,
            first.candidate_index,
            total_count,
            first.row,
            first.date_label,
            f"thumbnailing batch ({len(batch)} files, {worker_count} workers)",
            dry_run,
        )
        results: list[BroadIndexWorkResult] = []
        if executor is None:
            results = [
                _fingerprint_index_work_item(item, density, thumbnail_size, thumbnail_tool)
                for item in batch
            ]
        else:
            future_items = {
                executor.submit(_fingerprint_index_work_item, item, density, thumbnail_size, thumbnail_tool): item
                for item in batch
            }
            for future in concurrent.futures.as_completed(future_items):
                item = future_items[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - worker failures are recorded as per-file errors.
                    results.append(
                        BroadIndexWorkResult(
                            candidate_index=item.candidate_index,
                            row=item.row,
                            date_label=item.date_label,
                            descriptor={},
                            error="",
                            hash_error=type(exc).__name__,
                        )
                    )
        for result in sorted(results, key=lambda item: item.candidate_index):
            error = result.hash_error or result.error
            if error:
                errors += 1
                _update_date_coverage(date_coverage, result.date_label, "errors")
                _record_index_error(
                    connection,
                    run_id,
                    result.candidate_index,
                    result.row,
                    result.date_label,
                    "hashing" if result.hash_error else "fingerprinting",
                    error,
                    dry_run,
                )
            else:
                _update_date_coverage(date_coverage, result.date_label, "indexed")
                indexed += 1
            _upsert_descriptor(connection, result.row, density, method_version, result.descriptor, error)
        _commit_index_progress(
            connection,
            run_id,
            started_at,
            roots_label,
            settings,
            total_count,
            date_coverage,
            scanned,
            reused,
            indexed,
            skipped,
            errors,
            dry_run,
            commit_interval,
            force=True,
        )

    try:
        if not dry_run:
            _record_index_run(
                connection,
                run_id,
                started_at,
                "",
                "running",
                [Path(root) for root in roots_label],
                settings,
                total_count,
                _date_coverage_payload(date_coverage),
                scanned,
                reused,
                indexed,
                skipped,
                errors,
            )
            connection.commit()
            print(f"Broad visual index starting: {total_count} candidate files found", flush=True)
        seen_paths: set[str] = set()
        for path_item in path_roots:
            if _STOP_REQUESTED:
                break
            path, root, date_label, indexed_sha256 = _path_root_date(path_item)
            path = path.expanduser()
            path_text = str(path.resolve()) if path.exists() else str(path)
            if path_text in seen_paths:
                continue
            seen_paths.add(path_text)
            scanned += 1
            row = _descriptor_input_row(root, path, indexed_sha256)
            if row is None:
                errors += 1
                _update_date_coverage(date_coverage, date_label, "errors")
                _record_index_error(
                    connection,
                    run_id,
                    scanned,
                    {"path": str(path), "filename": path.name},
                    date_label,
                    "metadata",
                    "stat_failed",
                    dry_run,
                )
                _commit_index_progress(
                    connection, run_id, started_at, roots_label, settings,
                    total_count, date_coverage, scanned, reused, indexed, skipped, errors, dry_run, commit_interval
                )
                continue
            if not date_label and date_range:
                date_label = _first_row_date_in_range(row, date_range)
            if date_range and not date_label and not _row_within_date_range(row, date_range):
                skipped += 1
                _commit_index_progress(
                    connection, run_id, started_at, roots_label, settings,
                    total_count, date_coverage, scanned, reused, indexed, skipped, errors, dry_run, commit_interval
                )
                continue
            if not include_low_quality and int(row["quality_score"]) < 0:
                skipped += 1
                _update_date_coverage(date_coverage, date_label, "skipped")
                _commit_index_progress(
                    connection, run_id, started_at, roots_label, settings,
                    total_count, date_coverage, scanned, reused, indexed, skipped, errors, dry_run, commit_interval
                )
                continue
            _record_index_current_candidate(
                connection, run_id, scanned, total_count, row, date_label, "checking existing fingerprint", dry_run
            )
            if not rebuild_stale and _descriptor_is_current(connection, row, density, method_version):
                reused += 1
                _update_date_coverage(date_coverage, date_label, "reused")
                _commit_index_progress(
                    connection, run_id, started_at, roots_label, settings,
                    total_count, date_coverage, scanned, reused, indexed, skipped, errors, dry_run, commit_interval
                )
                continue
            if dry_run:
                indexed += 1
                _update_date_coverage(date_coverage, date_label, "indexed")
                _commit_index_progress(
                    connection, run_id, started_at, roots_label, settings,
                    total_count, date_coverage, scanned, reused, indexed, skipped, errors, dry_run, commit_interval
                )
                continue
            work_batch.append(BroadIndexWorkItem(scanned, row, date_label))
            if len(work_batch) >= work_batch_size:
                flush_work_batch()
        if work_batch and not _STOP_REQUESTED:
            flush_work_batch()
        status = "cancelled" if _STOP_REQUESTED else "pass" if errors == 0 else "partial"
        if not dry_run:
            _record_index_run(
                connection,
                run_id,
                started_at,
                _now(),
                status,
                [Path(root) for root in roots_label],
                settings,
                total_count,
                _date_coverage_payload(date_coverage),
                scanned,
                reused,
                indexed,
                skipped,
                errors,
            )
            connection.commit()
            print(
                "Broad visual index "
                f"{status}: {scanned}/{total_count} checked, {reused} reused, "
                f"{indexed} fingerprinted, {skipped} skipped, {errors} errors",
                flush=True,
            )
    finally:
        if executor is not None:
            executor.shutdown(cancel_futures=True)
        connection.close()
    return BroadIndexSummary(
        db_path=str(effective_db_path),
        run_id=run_id,
        scanned_count=scanned,
        reused_descriptor_count=reused,
        indexed_descriptor_count=indexed,
        error_count=errors,
        dry_run=dry_run,
        skipped_candidate_count=skipped,
    )


def run_match_batch(
    canonical_root: Path,
    db_path: Path | None = None,
    target_scope: dict[str, Any] | None = None,
    candidate_scope: str = "whole_indexed_library",
    candidate_roots: list[Path] | None = None,
    date_window_days: int = 0,
    max_results: int = DEFAULT_TOP_N,
    density: int = DEFAULT_DENSITY,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
    include_low_quality: bool = False,
    resume_run_id: str = "",
    dry_run: bool = False,
) -> BroadMatchSummary:
    if max_results <= 0:
        raise ValueError("max_results must be positive")
    if date_window_days < 0:
        raise ValueError("date_window_days must not be negative")
    effective_db_path = db_path or default_db_path(canonical_root)
    method_version = _descriptor_method_version(thumbnail_size)
    scope = target_scope or {}
    run_id = resume_run_id.strip() or _new_run_id("broad-match")
    started_at = _now()
    roots = _normalized_roots(candidate_roots or [])
    if candidate_scope in {"folder_limited", "same_setting_folder_limited"} and not roots:
        raise ValueError("Folder-limited broad matching requires at least one candidate root.")
    targets = _target_rows(canonical_root / "canonical.db", scope)
    rejected_candidates = _rejected_candidates_by_entry(canonical_root / "canonical.db")
    scanned = matched = result_count = errors = 0
    connection = connect_broad_db(effective_db_path)
    try:
        if not dry_run:
            _start_match_run(
                connection,
                run_id,
                started_at,
                scope,
                {
                    "candidate_scope": candidate_scope,
                    "candidate_roots": [str(root) for root in roots],
                    "date_window_days": date_window_days,
                },
                {
                    "max_results": max_results,
                    "density": density,
                    "thumbnail_size": thumbnail_size,
                    "thumbnail_tool": thumbnail_tool,
                    "method_version": method_version,
                    "include_low_quality": include_low_quality,
                    "dry_run": dry_run,
                },
                len(targets),
            )
        for target in targets:
            if not dry_run:
                connection.execute(
                    "UPDATE broad_match_runs SET phase = ?, current_entry_id = ? WHERE run_id = ?",
                    ("matching", target["entry_id"], run_id),
                )
                connection.commit()
            if resume_run_id and _result_exists_for_entry(connection, run_id, target["entry_id"]):
                continue
            source_path = Path(target["source_path"])
            source_descriptor, source_error = _build_descriptor_for_path(
                source_path, density, thumbnail_size, thumbnail_tool
            )
            if source_error:
                errors += 1
                continue
            candidates = _candidate_descriptor_rows(
                connection,
                entry_date=target["entry_date"],
                candidate_scope=candidate_scope,
                candidate_roots=roots,
                date_window_days=date_window_days,
                include_low_quality=include_low_quality,
                method_version=method_version,
            )
            candidates = _exclude_target_export_copies(target, candidates)
            candidates = _exclude_rejected_candidates(target, candidates, rejected_candidates)
            scanned += len(candidates)
            scored = _score_candidate_descriptors(source_descriptor, candidates, max_results)
            if scored:
                matched += 1
            result_count += len(scored)
            if not dry_run:
                connection.execute(
                    "DELETE FROM broad_match_results WHERE run_id = ? AND entry_id = ?",
                    (run_id, target["entry_id"]),
                )
                _insert_results(connection, run_id, target, scored)
                connection.commit()
        if not dry_run:
            _finish_match_run(connection, run_id, "pass", scanned, matched, result_count, errors)
            connection.commit()
    finally:
        connection.close()
    return BroadMatchSummary(
        db_path=str(effective_db_path),
        run_id=run_id,
        target_count=len(targets),
        scanned_count=scanned,
        matched_entries=matched,
        result_count=result_count,
        error_count=errors,
        dry_run=dry_run,
    )


def benchmark_confirmed_originals(
    canonical_root: Path,
    db_path: Path | None = None,
    target_scope: dict[str, Any] | None = None,
    max_results: int = DEFAULT_TOP_N,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    effective_db_path = db_path or default_db_path(canonical_root)
    method_version = _descriptor_method_version(thumbnail_size)
    effective_report_dir = report_dir or canonical_root / REPORT_DIR
    effective_report_dir.mkdir(parents=True, exist_ok=True)
    rows = _confirmed_target_rows(canonical_root / "canonical.db", target_scope or {})
    connection = connect_broad_db(effective_db_path)
    records: list[dict[str, Any]] = []
    try:
        all_candidates = _candidate_descriptor_rows(
            connection,
            entry_date="",
            candidate_scope="whole_indexed_library",
            candidate_roots=[],
            date_window_days=0,
            include_low_quality=True,
            method_version=method_version,
        )
        for target in rows:
            source_descriptor, source_error = _build_descriptor_for_path(
                Path(target["source_path"]), DEFAULT_DENSITY, thumbnail_size, thumbnail_tool
            )
            if source_error:
                records.append(_benchmark_record(target, "", "source_error", 0, "", []))
                continue
            scored = _score_candidate_descriptors(
                source_descriptor,
                _exclude_target_export_copies(
                    target,
                    all_candidates,
                    allowed_paths=[str(target.get("confirmed_path", "") or "")],
                ),
                max_results,
            )
            expected_path = str(Path(target["confirmed_path"]).resolve())
            rank = 0
            score = ""
            for result in scored:
                if str(Path(result["path"]).resolve()) == expected_path:
                    rank = int(result["rank"])
                    score = f"{float(result['score']):.4f}"
                    break
            records.append(
                _benchmark_record(
                    target,
                    "hit" if rank else "miss",
                    "",
                    rank,
                    score,
                    [_benchmark_candidate(target, result) for result in scored],
                )
            )
    finally:
        connection.close()
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = effective_report_dir / f"broad_visual_benchmark_{timestamp}.csv"
    json_path = effective_report_dir / f"broad_visual_benchmark_{timestamp}.json"
    fieldnames = ["entry_id", "entry_date", "confirmed_path", "status", "rank", "score", "error"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    summary = {
        "confirmed_count": len(records),
        "hit_count": sum(1 for row in records if row["status"] == "hit"),
        "miss_count": sum(1 for row in records if row["status"] == "miss"),
        "error_count": sum(1 for row in records if row["error"]),
        "top_1_count": sum(1 for row in records if int(row["rank"] or 0) == 1),
        "top_5_count": sum(1 for row in records if 1 <= int(row["rank"] or 0) <= 5),
        "top_n_count": sum(1 for row in records if 1 <= int(row["rank"] or 0) <= max_results),
        "max_results": max_results,
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }
    confirmed_count = max(1, int(summary["confirmed_count"]))
    summary["recall_at_1"] = round(int(summary["top_1_count"]) / confirmed_count, 4)
    summary["recall_at_5"] = round(int(summary["top_5_count"]) / confirmed_count, 4)
    summary["recall_at_n"] = round(int(summary["top_n_count"]) / confirmed_count, 4)
    json_path.write_text(json.dumps({"summary": summary, "records": records}, indent=2, sort_keys=True), encoding="utf-8")
    latest_path = effective_report_dir / "broad_visual_benchmark_latest.json"
    latest_path.write_text(json.dumps({"summary": summary, "records": records}, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def broad_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "exists": False,
            "path": str(db_path),
            "descriptor_count": 0,
            "descriptor_error_count": 0,
            "result_count": 0,
            "latest_run": {},
            "latest_index_run": {},
            "latest_index_errors": [],
        }
    connection = connect_broad_db(db_path)
    try:
        descriptor_count = int(
            connection.execute("SELECT COUNT(*) FROM broad_descriptors WHERE COALESCE(error, '') = ''").fetchone()[0]
        )
        descriptor_error_count = int(
            connection.execute("SELECT COUNT(*) FROM broad_descriptors WHERE COALESCE(error, '') != ''").fetchone()[0]
        )
        result_count = int(connection.execute("SELECT COUNT(*) FROM broad_match_results").fetchone()[0])
        latest_run = _single_dict(
            connection.execute(
                "SELECT * FROM broad_match_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        )
        latest_index_run = _single_dict(
            connection.execute(
                "SELECT * FROM broad_descriptor_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        )
        latest_index_errors = []
        latest_index_run_id = str(latest_index_run.get("run_id", "") or "")
        if latest_index_run_id:
            latest_index_errors = [
                _single_dict(row)
                for row in connection.execute(
                    """
                    SELECT candidate_index, path, filename, candidate_date, phase, error, created_at
                    FROM broad_descriptor_errors
                    WHERE run_id = ?
                    ORDER BY candidate_index
                    LIMIT 20
                    """,
                    (latest_index_run_id,),
                )
            ]
        return {
            "exists": True,
            "path": str(db_path),
            "descriptor_count": descriptor_count,
            "descriptor_error_count": descriptor_error_count,
            "result_count": result_count,
            "latest_run": latest_run,
            "latest_index_run": latest_index_run,
            "latest_index_errors": latest_index_errors,
        }
    finally:
        connection.close()


def review_results(
    db_path: Path,
    run_id: str = "",
    entry_id: str = "",
    limit: int = DEFAULT_RESULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    connection = connect_broad_db(db_path)
    try:
        effective_run_id = run_id or _latest_match_run_id(connection)
        if not effective_run_id:
            return {"run_id": "", "results": [], "returned_count": 0, "has_more": False, "offset": offset, "limit": limit}
        where = ["run_id = ?"]
        params: list[Any] = [effective_run_id]
        if entry_id:
            where.append("entry_id = ?")
            params.append(entry_id)
        rows = connection.execute(
            f"""
            SELECT *
            FROM broad_match_results
            WHERE {" AND ".join(where)}
            ORDER BY entry_date, entry_id, rank, candidate_path
            LIMIT ? OFFSET ?
            """,
            (*params, limit + 1, offset),
        ).fetchall()
        visible = rows[:limit]
        return {
            "run_id": effective_run_id,
            "results": [_single_dict(row) for row in visible],
            "returned_count": len(visible),
            "has_more": len(rows) > limit,
            "offset": offset,
            "limit": limit,
        }
    finally:
        connection.close()


def review_entries(
    canonical_root: Path,
    db_path: Path,
    run_id: str = "",
    entry_id: str = "",
    limit: int = 1,
    offset: int = 0,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    connection = connect_broad_db(db_path)
    try:
        effective_run_id = run_id or _latest_match_run_id(connection)
        if not effective_run_id:
            return {"run_id": "", "entries": [], "returned_count": 0, "has_more": False, "offset": offset, "limit": limit}
        _attach_canonical_db(connection, canonical_root / "canonical.db")
        where = ["run_id = ?"]
        params: list[Any] = [effective_run_id]
        if entry_id:
            where.append("entry_id = ?")
            params.append(entry_id)
        entry_rows = connection.execute(
            f"""
            SELECT entry_id, MIN(entry_date) AS entry_date
            FROM broad_match_results
            WHERE {" AND ".join(where)}
                AND EXISTS (
                    SELECT 1
                    FROM broad_match_results AS visible_results
                    WHERE visible_results.run_id = broad_match_results.run_id
                        AND visible_results.entry_id = broad_match_results.entry_id
                        AND NOT EXISTS (
                            SELECT 1
                            FROM canonical_db.media_assets AS rejected
                            WHERE rejected.entry_id = visible_results.entry_id
                                AND rejected.role = 'external_original_rejected'
                                AND rejected.review_status = 'rejected'
                                AND (
                                    (
                                        COALESCE(rejected.sha256, '') != ''
                                        AND rejected.sha256 = visible_results.candidate_sha256
                                    )
                                    OR (
                                        COALESCE(rejected.storage_path, '') != ''
                                        AND rejected.storage_path = visible_results.candidate_path
                                    )
                                )
                        )
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM broad_match_entry_decisions AS decisions
                    WHERE decisions.run_id = broad_match_results.run_id
                        AND decisions.entry_id = broad_match_results.entry_id
                )
            GROUP BY entry_id
            ORDER BY entry_date, entry_id
            LIMIT ? OFFSET ?
            """,
            (*params, limit + 1, offset),
        ).fetchall()
        visible_entries = [_single_dict(row) for row in entry_rows[:limit]]
        if not visible_entries:
            return {
                "run_id": effective_run_id,
                "entries": [],
                "returned_count": 0,
                "has_more": len(entry_rows) > limit,
                "offset": offset,
                "limit": limit,
            }
        entry_ids = [str(row["entry_id"]) for row in visible_entries]
        result_rows = _results_for_entries(connection, effective_run_id, entry_ids)
    finally:
        connection.close()
    target_rows = _review_target_rows(canonical_root / "canonical.db", entry_ids)
    rejected_candidates = _rejected_candidates_by_entry(canonical_root / "canonical.db")
    entries = []
    for entry in visible_entries:
        entry_id_text = str(entry["entry_id"])
        target = target_rows.get(entry_id_text, {})
        results = _exclude_target_export_copies(target, result_rows.get(entry_id_text, []))
        results = _exclude_rejected_candidates({"entry_id": entry_id_text}, results, rejected_candidates)
        entries.append(
            {
                "entry_id": entry_id_text,
                "entry_date": str(entry.get("entry_date") or target.get("entry_date") or ""),
                "source_path": str(target.get("source_path") or ""),
                "confirmed_path": str(target.get("confirmed_path") or ""),
                "results": results,
            }
        )
    return {
        "run_id": effective_run_id,
        "entries": entries,
        "returned_count": len(entries),
        "has_more": len(entry_rows) > limit,
        "offset": offset,
        "limit": limit,
    }


def benchmark_review_entries(
    canonical_root: Path,
    report_dir: Path | None = None,
    entry_id: str = "",
    limit: int = 1,
    offset: int = 0,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    effective_report_dir = report_dir or canonical_root / REPORT_DIR
    report_path = _latest_benchmark_json(effective_report_dir)
    if report_path is None:
        return {"report_path": "", "entries": [], "returned_count": 0, "has_more": False, "offset": offset, "limit": limit}
    data = json.loads(report_path.read_text(encoding="utf-8"))
    records = list(data.get("records", []))
    if entry_id:
        records = [record for record in records if str(record.get("entry_id", "")) == entry_id]
    visible = records[offset: offset + limit]
    entries = [
        {
            "entry_id": str(record.get("entry_id", "")),
            "entry_date": str(record.get("entry_date", "")),
            "source_path": str(record.get("source_path", "")),
            "confirmed_path": str(record.get("confirmed_path", "")),
            "status": str(record.get("status", "")),
            "expected_rank": int(record.get("rank") or 0),
            "expected_score": str(record.get("score", "")),
            "results": list(record.get("candidates", [])),
        }
        for record in visible
    ]
    return {
        "report_path": str(report_path),
        "summary": data.get("summary", {}),
        "entries": entries,
        "returned_count": len(entries),
        "has_more": offset + limit < len(records),
        "offset": offset,
        "limit": limit,
    }


def send_results_to_picker(
    canonical_root: Path,
    db_path: Path,
    queue_path: Path,
    result_ids: list[int],
) -> dict[str, Any]:
    if not result_ids:
        raise ValueError("Choose at least one broad visual result.")
    connection = connect_broad_db(db_path)
    try:
        placeholders = ", ".join("?" for _ in result_ids)
        rows = [
            _single_dict(row)
            for row in connection.execute(
                f"""
                SELECT *
                FROM broad_match_results
                WHERE result_id IN ({placeholders})
                ORDER BY entry_date, entry_id, rank
                """,
                result_ids,
            )
        ]
        if not rows:
            raise ValueError("No selected broad visual results were found.")
        queue_rows, fieldnames = _read_picker_queue(queue_path)
        fieldnames = _queue_fieldnames(fieldnames)
        existing = {
            (row.get("entry_id", ""), row.get("candidate_path", ""))
            for row in queue_rows
        }
        added = 0
        for result in rows:
            key = (str(result["entry_id"]), str(result["candidate_path"]))
            if key in existing:
                continue
            queue_rows.append(_picker_queue_row(canonical_root / "canonical.db", result, fieldnames))
            existing.add(key)
            added += 1
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        _write_picker_queue(queue_path, queue_rows, fieldnames)
        now = _now()
        connection.executemany(
            "UPDATE broad_match_results SET sent_to_picker_at = ? WHERE result_id = ?",
            [(now, result_id) for result_id in result_ids],
        )
        connection.commit()
        return {
            "selected_count": len(result_ids),
            "added_count": added,
            "queue_path": str(queue_path),
            "entry_count": len({row["entry_id"] for row in rows}),
        }
    finally:
        connection.close()


def confirm_broad_match(
    canonical_root: Path,
    db_path: Path,
    result_id: int,
    notes: str = "Confirmed in Broad Visual Review.",
) -> dict[str, Any]:
    if result_id <= 0:
        raise ValueError("Choose a broad visual candidate to match.")
    connection = connect_broad_db(db_path)
    try:
        result = _single_dict(
            connection.execute(
                "SELECT * FROM broad_match_results WHERE result_id = ?",
                (result_id,),
            ).fetchone()
        )
        if not result:
            raise ValueError("Unknown broad visual result.")
        _upsert_canonical_media_decision(
            canonical_root / "canonical.db",
            result,
            role="external_original_reference",
            status="confirmed",
            review_status="confirmed",
            decision="use_external_original",
            notes=notes,
        )
        _record_broad_entry_decision(
            connection,
            str(result["run_id"]),
            str(result["entry_id"]),
            "matched",
            int(result["result_id"]),
            str(result["candidate_path"]),
            notes,
        )
        connection.commit()
        return {
            "entry_id": str(result["entry_id"]),
            "result_id": int(result["result_id"]),
            "candidate_path": str(result["candidate_path"]),
            "decision": "matched",
        }
    finally:
        connection.close()


def reject_broad_entry(
    canonical_root: Path,
    db_path: Path,
    run_id: str = "",
    entry_id: str = "",
    notes: str = "Rejected in Broad Visual Review.",
) -> dict[str, Any]:
    entry_text = str(entry_id or "").strip()
    if not entry_text:
        raise ValueError("Choose an entry to reject.")
    connection = connect_broad_db(db_path)
    try:
        effective_run_id = run_id.strip() or _latest_match_run_id(connection)
        if not effective_run_id:
            raise ValueError("No broad visual search run is available.")
        rows = [
            _single_dict(row)
            for row in connection.execute(
                """
                SELECT *
                FROM broad_match_results
                WHERE run_id = ? AND entry_id = ?
                ORDER BY rank
                """,
                (effective_run_id, entry_text),
            )
        ]
        if not rows:
            raise ValueError("No broad visual candidates were found for this entry.")
        for row in rows:
            _upsert_canonical_media_decision(
                canonical_root / "canonical.db",
                row,
                role="external_original_rejected",
                status="rejected",
                review_status="rejected",
                decision="rejected",
                notes=notes,
            )
        _record_broad_entry_decision(
            connection,
            effective_run_id,
            entry_text,
            "rejected_all",
            None,
            "",
            notes,
        )
        connection.commit()
        return {
            "entry_id": entry_text,
            "run_id": effective_run_id,
            "decision": "rejected_all",
            "rejected_count": len(rows),
        }
    finally:
        connection.close()


def _descriptor_method_version(thumbnail_size: int) -> str:
    size = int(thumbnail_size or 0)
    if size > 0:
        return f"{THUMBNAIL_METHOD_VERSION}_{size}"
    return METHOD_VERSION


def _normalized_worker_count(workers: int) -> int:
    return max(1, int(workers or 1))


def _fingerprint_index_work_item(
    item: BroadIndexWorkItem,
    density: int,
    thumbnail_size: int,
    thumbnail_tool: str,
) -> BroadIndexWorkResult:
    row = dict(item.row)
    try:
        if not str(row.get("sha256", "") or "").strip():
            row["sha256"] = _sha256_file(Path(str(row["path"])))
    except OSError as exc:
        return BroadIndexWorkResult(
            candidate_index=item.candidate_index,
            row=row,
            date_label=item.date_label,
            descriptor={},
            error="",
            hash_error=type(exc).__name__,
        )
    descriptor, error = _build_descriptor_for_path(
        Path(str(row["path"])),
        density,
        thumbnail_size,
        thumbnail_tool,
    )
    return BroadIndexWorkResult(
        candidate_index=item.candidate_index,
        row=row,
        date_label=item.date_label,
        descriptor=descriptor,
        error=error,
        hash_error="",
    )


def _build_descriptor_for_path(
    path: Path,
    density: int,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
) -> tuple[dict[str, Any], str]:
    try:
        return build_dense_descriptor(
            path,
            density=density,
            thumbnail_size=thumbnail_size,
            thumbnail_tool=thumbnail_tool,
        ), ""
    except Exception as exc:  # noqa: BLE001 - local decode failures are recorded as metadata.
        return {}, type(exc).__name__


def build_dense_descriptor(
    path: Path,
    density: int = DEFAULT_DENSITY,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
) -> dict[str, Any]:
    method_version = _descriptor_method_version(thumbnail_size)
    image = load_visual_image(path, max_dimension=thumbnail_size, thumbnail_tool=thumbnail_tool)
    views = [
        _view_descriptor(image, name, x, y, width, height, sample_size)
        for name, x, y, width, height in dense_square_windows(image, density)
    ]
    return {
        "method": method_version,
        "width": image.width,
        "height": image.height,
        "density": density,
        "thumbnail_size": int(thumbnail_size or 0),
        "views": views,
    }


def dense_square_windows(image: VisualImage, density: int) -> list[tuple[str, int, int, int, int]]:
    width = image.width
    height = image.height
    windows = [("full", 0, 0, width, height)]
    square = min(width, height)
    if width == height:
        return windows
    count = max(1, int(density))
    if width > height:
        for index, x in enumerate(_scan_offsets(width - square, count), start=1):
            windows.append((f"square_x_{index:02d}", x, 0, square, square))
    else:
        for index, y in enumerate(_scan_offsets(height - square, count), start=1):
            windows.append((f"square_y_{index:02d}", 0, y, square, square))
    return windows


def _scan_offsets(span: int, count: int) -> list[int]:
    if span <= 0:
        return [0]
    if count <= 1:
        return [round(span / 2)]
    offsets = [round(index * span / (count - 1)) for index in range(count)]
    return sorted(set(offsets))


def _score_candidate_descriptors(
    source_descriptor: dict[str, Any],
    candidates: list[dict[str, Any]],
    max_results: int,
) -> list[dict[str, Any]]:
    scored = []
    for candidate in candidates:
        if candidate.get("error"):
            continue
        descriptor = json.loads(str(candidate["descriptor_json"]))
        score, view = _visual_score(source_descriptor, descriptor)
        scored.append((score, str(candidate["path"]), view, candidate))
    scored.sort(key=lambda item: (item[0], item[1]))
    best = scored[0][0] if scored else 0.0
    results = []
    for rank, (score, _path, view, candidate) in enumerate(scored[:max_results], start=1):
        result = dict(candidate)
        result["score"] = score
        result["score_gap"] = score - best
        result["rank"] = rank
        result["best_view"] = view
        results.append(result)
    return results


def _exclude_target_export_copies(
    target: dict[str, str],
    candidates: list[dict[str, Any]],
    allowed_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    source_path = str(target.get("source_path", "") or "").strip()
    source_filename = str(target.get("source_filename", "") or Path(source_path).name).strip()
    source_sha = str(target.get("source_sha256", "") or "").strip()
    resolved_source = str(Path(source_path).resolve()) if source_path else ""
    allowed = {str(Path(path).resolve()) for path in (allowed_paths or []) if str(path or "").strip()}
    filtered = []
    for candidate in candidates:
        candidate_path = str(candidate.get("path", "") or "").strip()
        resolved_candidate = str(Path(candidate_path).resolve())
        if resolved_candidate in allowed:
            filtered.append(candidate)
            continue
        if resolved_source and resolved_candidate == resolved_source:
            continue
        candidate_filename = str(
            candidate.get("filename") or candidate.get("candidate_filename") or Path(candidate_path).name
        ).strip()
        if source_filename and candidate_filename == source_filename:
            continue
        candidate_sha = str(candidate.get("sha256") or candidate.get("candidate_sha256") or "").strip()
        if source_sha and candidate_sha and candidate_sha == source_sha:
            continue
        filtered.append(candidate)
    return filtered


def _rejected_candidates_by_entry(canonical_db: Path) -> dict[str, dict[str, set[str]]]:
    if not canonical_db.exists():
        return {}
    connection = sqlite3.connect(canonical_db)
    try:
        rows = connection.execute(
            """
            SELECT entry_id, sha256, storage_path
            FROM media_assets
            WHERE role = 'external_original_rejected'
                AND review_status = 'rejected'
            """
        ).fetchall()
    finally:
        connection.close()
    rejected: dict[str, dict[str, set[str]]] = {}
    for entry_id, sha256, storage_path in rows:
        entry = rejected.setdefault(str(entry_id), {"sha256": set(), "paths": set(), "resolved_paths": set()})
        sha_text = str(sha256 or "").strip()
        path_text = str(storage_path or "").strip()
        if sha_text:
            entry["sha256"].add(sha_text)
        if path_text:
            entry["paths"].add(path_text)
            entry["resolved_paths"].add(str(Path(path_text).resolve()))
    return rejected


def _attach_canonical_db(connection: sqlite3.Connection, canonical_db: Path) -> None:
    connection.execute("ATTACH DATABASE ? AS canonical_db", (str(canonical_db),))


def _exclude_rejected_candidates(
    target: dict[str, str],
    candidates: list[dict[str, Any]],
    rejected_candidates: dict[str, dict[str, set[str]]],
) -> list[dict[str, Any]]:
    rejected = rejected_candidates.get(str(target.get("entry_id", "")))
    if not rejected:
        return candidates
    filtered = []
    for candidate in candidates:
        candidate_sha = str(candidate.get("sha256") or candidate.get("candidate_sha256") or "").strip()
        candidate_path = str(candidate.get("path") or candidate.get("candidate_path") or "").strip()
        resolved_candidate = str(Path(candidate_path).resolve()) if candidate_path else ""
        if candidate_sha and candidate_sha in rejected["sha256"]:
            continue
        if candidate_path and candidate_path in rejected["paths"]:
            continue
        if resolved_candidate and resolved_candidate in rejected["resolved_paths"]:
            continue
        filtered.append(candidate)
    return filtered


def _target_rows(db_path: Path, scope: dict[str, Any]) -> list[dict[str, str]]:
    entry_ids = set(str(value).strip() for value in scope.get("entry_ids", []) if str(value).strip())
    list_path = str(scope.get("broad_search_needed_list", "") or "").strip()
    if list_path:
        entry_ids.update(_entry_ids_from_list(Path(list_path)))
    start_date = str(scope.get("start_date", "") or "").strip()
    end_date = str(scope.get("end_date", "") or "").strip()
    if start_date:
        dt.date.fromisoformat(start_date)
    if end_date:
        dt.date.fromisoformat(end_date)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        clauses = [
            "exports.role = 'project365_export_png'",
            "COALESCE(exports.storage_path, '') != ''",
            """
            NOT EXISTS (
                SELECT 1
                FROM media_assets AS confirmed
                WHERE confirmed.entry_id = entries.id
                    AND confirmed.role = 'external_original_reference'
                    AND confirmed.review_status = 'confirmed'
            )
            """,
        ]
        params: list[Any] = []
        if entry_ids:
            placeholders = ", ".join("?" for _ in entry_ids)
            clauses.append(f"entries.id IN ({placeholders})")
            params.extend(sorted(entry_ids))
        if start_date:
            clauses.append("entries.entry_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("entries.entry_date <= ?")
            params.append(end_date)
        rows = connection.execute(
            f"""
            SELECT
                entries.id AS entry_id,
                entries.entry_date AS entry_date,
                exports.id AS project365_media_asset_id,
                exports.storage_path AS source_path,
                exports.internal_filename AS source_filename,
                exports.sha256 AS source_sha256
            FROM entries
            JOIN media_assets AS exports
                ON exports.entry_id = entries.id
            WHERE {" AND ".join(clauses)}
            ORDER BY entries.entry_date, entries.id
            """,
            params,
        ).fetchall()
        return [_single_dict(row) for row in rows]
    finally:
        connection.close()


def _confirmed_target_rows(db_path: Path, scope: dict[str, Any] | None = None) -> list[dict[str, str]]:
    scope = scope or {}
    entry_ids = set(str(value).strip() for value in scope.get("entry_ids", []) if str(value).strip())
    start_date = str(scope.get("start_date", "") or "").strip()
    end_date = str(scope.get("end_date", "") or "").strip()
    if start_date:
        dt.date.fromisoformat(start_date)
    if end_date:
        dt.date.fromisoformat(end_date)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        clauses = [
            "COALESCE(exports.storage_path, '') != ''",
            "COALESCE(confirmed.storage_path, '') != ''",
        ]
        params: list[Any] = []
        if entry_ids:
            placeholders = ", ".join("?" for _ in entry_ids)
            clauses.append(f"entries.id IN ({placeholders})")
            params.extend(sorted(entry_ids))
        if start_date:
            clauses.append("entries.entry_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("entries.entry_date <= ?")
            params.append(end_date)
        rows = connection.execute(
            f"""
            SELECT
                entries.id AS entry_id,
                entries.entry_date AS entry_date,
                exports.storage_path AS source_path,
                exports.internal_filename AS source_filename,
                exports.sha256 AS source_sha256,
                confirmed.storage_path AS confirmed_path
            FROM entries
            JOIN media_assets AS exports
                ON exports.entry_id = entries.id
                AND exports.role = 'project365_export_png'
            JOIN media_assets AS confirmed
                ON confirmed.entry_id = entries.id
                AND confirmed.role = 'external_original_reference'
                AND confirmed.review_status = 'confirmed'
            WHERE {" AND ".join(clauses)}
            ORDER BY entries.entry_date, entries.id
            """,
            params,
        ).fetchall()
        return [_single_dict(row) for row in rows]
    finally:
        connection.close()


def _review_target_rows(db_path: Path, entry_ids: list[str]) -> dict[str, dict[str, str]]:
    if not entry_ids:
        return {}
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ", ".join("?" for _ in entry_ids)
        rows = connection.execute(
            f"""
            SELECT
                entries.id AS entry_id,
                entries.entry_date AS entry_date,
                exports.storage_path AS source_path,
                exports.internal_filename AS source_filename,
                exports.sha256 AS source_sha256,
                COALESCE(confirmed.storage_path, '') AS confirmed_path
            FROM entries
            JOIN media_assets AS exports
                ON exports.entry_id = entries.id
                AND exports.role = 'project365_export_png'
            LEFT JOIN media_assets AS confirmed
                ON confirmed.entry_id = entries.id
                AND confirmed.role = 'external_original_reference'
                AND confirmed.review_status = 'confirmed'
            WHERE entries.id IN ({placeholders})
            ORDER BY entries.entry_date, entries.id
            """,
            entry_ids,
        ).fetchall()
        return {str(row["entry_id"]): _single_dict(row) for row in rows}
    finally:
        connection.close()


def _results_for_entries(
    connection: sqlite3.Connection,
    run_id: str,
    entry_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not entry_ids:
        return {}
    placeholders = ", ".join("?" for _ in entry_ids)
    rows = connection.execute(
        f"""
        SELECT *
        FROM broad_match_results
        WHERE run_id = ?
            AND entry_id IN ({placeholders})
        ORDER BY entry_date, entry_id, rank, candidate_path
        """,
        (run_id, *entry_ids),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {entry_id: [] for entry_id in entry_ids}
    for row in rows:
        result = _single_dict(row)
        grouped.setdefault(str(result["entry_id"]), []).append(result)
    return grouped


def _candidate_descriptor_rows(
    connection: sqlite3.Connection,
    entry_date: str,
    candidate_scope: str,
    candidate_roots: list[Path],
    date_window_days: int,
    include_low_quality: bool,
    method_version: str = METHOD_VERSION,
) -> list[dict[str, Any]]:
    clauses = ["error = ''", "method_version = ?"]
    params: list[Any] = [method_version]
    if not include_low_quality:
        clauses.append("quality_score >= 0")
    if candidate_scope in {"folder_limited", "same_setting_folder_limited"} and candidate_roots:
        root_clauses = []
        for root in candidate_roots:
            prefix = _folder_path_prefix(root)
            root_clauses.append("(path = ? OR substr(path, 1, ?) = ?)")
            params.extend([prefix.rstrip(os.sep), len(prefix), prefix])
        clauses.append("(" + " OR ".join(root_clauses) + ")")
    rows = [_single_dict(row) for row in connection.execute(
        f"""
        SELECT *
        FROM broad_descriptors
        WHERE {" AND ".join(clauses)}
        ORDER BY path
        """,
        params,
    )]
    if candidate_scope == "date_window_limited" and entry_date and date_window_days:
        target_date = dt.date.fromisoformat(entry_date)
        rows = [
            row
            for row in rows
            if _row_within_date_window(row, target_date, date_window_days)
        ]
    return rows


def _row_within_date_window(row: dict[str, Any], target_date: dt.date, window_days: int) -> bool:
    values = []
    for field in ("filename_dates", "media_creation_dates", "filesystem_dates"):
        values.extend(str(row.get(field, "") or "").split(";"))
    for value in values:
        if not value:
            continue
        try:
            candidate_date = dt.date.fromisoformat(value[:10])
        except ValueError:
            continue
        if abs((candidate_date - target_date).days) <= window_days:
            return True
    return False


def _expanded_date_range(
    start_date: str,
    end_date: str,
    window_days: int,
) -> tuple[dt.date | None, dt.date | None] | None:
    if window_days < 0:
        raise ValueError("date_window_days must not be negative")
    start = _optional_date(start_date)
    end = _optional_date(end_date)
    if start and end and start > end:
        raise ValueError("start_date must be on or before end_date")
    if not start and not end:
        return None
    window = dt.timedelta(days=window_days)
    return (
        start - window if start else None,
        end + window if end else None,
    )


def _optional_date(value: str) -> dt.date | None:
    text = str(value or "").strip()
    return dt.date.fromisoformat(text) if text else None


def _row_within_date_range(
    row: dict[str, Any],
    date_range: tuple[dt.date | None, dt.date | None],
) -> bool:
    return bool(_first_row_date_in_range(row, date_range))


def _first_row_date_in_range(
    row: dict[str, Any],
    date_range: tuple[dt.date | None, dt.date | None],
) -> str:
    start, end = date_range
    values = []
    for field in ("filename_dates", "media_creation_dates", "filesystem_dates"):
        values.extend(str(row.get(field, "") or "").split(";"))
    for value in values:
        if not value:
            continue
        try:
            candidate_date = dt.date.fromisoformat(value[:10])
        except ValueError:
            continue
        if start and candidate_date < start:
            continue
        if end and candidate_date > end:
            continue
        return candidate_date.isoformat()
    return ""


def _path_root_date(item: tuple[Any, ...]) -> tuple[Path, Path, str, str]:
    path = Path(item[0])
    root = Path(item[1])
    date_label = str(item[2]) if len(item) > 2 else ""
    indexed_sha256 = str(item[3]) if len(item) > 3 else ""
    return path, root, date_label, indexed_sha256


def _initial_date_coverage(path_roots: list[tuple[Any, ...]]) -> dict[str, dict[str, int]]:
    coverage: dict[str, dict[str, int]] = {}
    for item in path_roots:
        _path, _root, date_label, _indexed_sha256 = _path_root_date(item)
        if not date_label:
            continue
        row = coverage.setdefault(date_label, _empty_date_coverage())
        row["total"] += 1
    return coverage


def _empty_date_coverage() -> dict[str, int]:
    return {"total": 0, "checked": 0, "reused": 0, "indexed": 0, "skipped": 0, "errors": 0}


def _update_date_coverage(
    coverage: dict[str, dict[str, int]],
    date_label: str,
    outcome: str,
) -> None:
    if not date_label:
        return
    row = coverage.setdefault(date_label, _empty_date_coverage())
    if row["total"] <= row["checked"]:
        row["total"] = row["checked"] + 1
    row["checked"] += 1
    if outcome in row:
        row[outcome] += 1


def _date_coverage_payload(coverage: dict[str, dict[str, int]]) -> dict[str, Any]:
    dates = [
        {"date": date_label, **coverage[date_label]}
        for date_label in sorted(coverage)
        if int(coverage[date_label].get("total") or 0) > 0
    ]
    complete_prefix = []
    current = {}
    for row in dates:
        if int(row["checked"]) >= int(row["total"]):
            complete_prefix.append(row)
            continue
        current = row
        break
    payload: dict[str, Any] = {
        "dates": dates,
        "total_date_count": len(dates),
        "complete_date_count": len(complete_prefix),
    }
    if complete_prefix:
        payload["complete_start_date"] = complete_prefix[0]["date"]
        payload["complete_end_date"] = complete_prefix[-1]["date"]
    if current:
        payload["current_date"] = current["date"]
        payload["current_checked"] = current["checked"]
        payload["current_total"] = current["total"]
    return payload


def _date_limited_photo_index_paths(
    index_db: Path,
    roots: list[Path],
    date_range: tuple[dt.date | None, dt.date | None],
) -> list[tuple[Path, Path, str, str]] | None:
    if not index_db.exists():
        return None
    start, end = date_range
    clauses: list[str] = []
    params: list[Any] = []
    if start:
        clauses.append("d.date >= ?")
        params.append(start.isoformat())
    if end:
        clauses.append("d.date <= ?")
        params.append(end.isoformat())
    root_clauses: list[str] = []
    for root in roots:
        root_text = str(root)
        root_clauses.append("f.root = ?")
        params.append(root_text)
        root_clauses.append("f.root = ?")
        params.append(root.name)
        prefix = _folder_path_prefix(root)
        root_clauses.append("(f.path = ? OR substr(f.path, 1, ?) = ?)")
        params.extend([prefix.rstrip(os.sep), len(prefix), prefix])
    if root_clauses:
        clauses.append("(" + " OR ".join(root_clauses) + ")")
    try:
        connection = sqlite3.connect(index_db)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                f"""
                SELECT f.path, f.root, MIN(d.date) AS first_date, MAX(f.sha256) AS sha256
                FROM photo_library_files AS f
                JOIN photo_library_dates AS d ON d.file_path = f.path
                WHERE {" AND ".join(clauses)}
                GROUP BY f.path, f.root
                ORDER BY first_date, f.path
                """,
                params,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return [
        (Path(str(row["path"])), Path(str(row["root"])), str(row["first_date"]), str(row["sha256"] or ""))
        for row in rows
    ]


def _descriptor_input_row(root: Path, path: Path, indexed_sha256: str = "") -> dict[str, Any] | None:
    try:
        stat = path.stat()
        filename_dates = _filename_dates(path)
        media_dates = _jpeg_exif_dates(path)
        filesystem_dates = _filesystem_dates(stat)
        quality_score, quality_evidence = _quality_rank(path.name)
        return {
            "path": str(path.resolve()),
            "root": str(root),
            "filename": path.name,
            "extension": path.suffix.lower(),
            "byte_size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "filesystem_mtime_utc": _filesystem_mtime_utc(stat),
            "sha256": str(indexed_sha256 or ""),
            "filename_dates": ";".join(sorted(filename_dates)),
            "media_creation_dates": ";".join(sorted(media_dates)),
            "filesystem_dates": ";".join(sorted(filesystem_dates)),
            "quality_score": quality_score,
            "quality_evidence": ";".join(quality_evidence),
        }
    except OSError:
        return None


def _descriptor_is_current(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    density: int,
    method_version: str,
) -> bool:
    existing = connection.execute(
        """
        SELECT byte_size, mtime_ns, method_version, density
        FROM broad_descriptors
        WHERE path = ?
            AND COALESCE(error, '') = ''
            AND COALESCE(descriptor_json, '') NOT IN ('', '{}')
        """,
        (row["path"],),
    ).fetchone()
    return bool(
        existing
        and int(existing["byte_size"]) == int(row["byte_size"])
        and int(existing["mtime_ns"]) == int(row["mtime_ns"])
        and existing["method_version"] == method_version
        and int(existing["density"]) == int(density)
    )


def _upsert_descriptor(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    density: int,
    method_version: str,
    descriptor: dict[str, Any],
    error: str,
) -> None:
    width = int(descriptor.get("width") or 0)
    height = int(descriptor.get("height") or 0)
    connection.execute(
        """
        INSERT INTO broad_descriptors (
            path, root, filename, extension, byte_size, mtime_ns, filesystem_mtime_utc,
            sha256, width, height, filename_dates, media_creation_dates, filesystem_dates,
            quality_score, quality_evidence, method_version, density, descriptor_json, error, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            root = excluded.root,
            filename = excluded.filename,
            extension = excluded.extension,
            byte_size = excluded.byte_size,
            mtime_ns = excluded.mtime_ns,
            filesystem_mtime_utc = excluded.filesystem_mtime_utc,
            sha256 = excluded.sha256,
            width = excluded.width,
            height = excluded.height,
            filename_dates = excluded.filename_dates,
            media_creation_dates = excluded.media_creation_dates,
            filesystem_dates = excluded.filesystem_dates,
            quality_score = excluded.quality_score,
            quality_evidence = excluded.quality_evidence,
            method_version = excluded.method_version,
            density = excluded.density,
            descriptor_json = excluded.descriptor_json,
            error = excluded.error,
            indexed_at = excluded.indexed_at
        """,
        (
            row["path"],
            row["root"],
            row["filename"],
            row["extension"],
            row["byte_size"],
            row["mtime_ns"],
            row["filesystem_mtime_utc"],
            row["sha256"],
            width,
            height,
            row["filename_dates"],
            row["media_creation_dates"],
            row["filesystem_dates"],
            row["quality_score"],
            row["quality_evidence"],
            method_version,
            density,
            json.dumps(descriptor, sort_keys=True),
            error,
            _now(),
        ),
    )


def _insert_results(
    connection: sqlite3.Connection,
    run_id: str,
    target: dict[str, str],
    results: list[dict[str, Any]],
) -> None:
    for result in results:
        connection.execute(
            """
            INSERT OR REPLACE INTO broad_match_results (
                run_id, entry_id, entry_date, project365_media_asset_id, candidate_path,
                candidate_filename, candidate_sha256, byte_size, mime_type, filename_dates,
                media_creation_dates, filesystem_dates, date_distance, score, score_gap,
                rank, best_view, method_version, evidence, candidate_filter_reason,
                error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                target["entry_id"],
                target["entry_date"],
                target["project365_media_asset_id"],
                result["path"],
                result["filename"],
                result["sha256"],
                int(result["byte_size"]),
                mimetypes.guess_type(str(result["filename"]))[0] or "application/octet-stream",
                result["filename_dates"],
                result["media_creation_dates"],
                result["filesystem_dates"],
                _date_distance(target["entry_date"], result),
                float(result["score"]),
                float(result["score_gap"]),
                int(result["rank"]),
                result["best_view"],
                str(result.get("method_version") or _descriptor_method_version(DEFAULT_THUMBNAIL_SIZE)),
                f"broad_visual_match;run_id:{run_id}",
                "",
                "",
                _now(),
            ),
        )


def _record_broad_entry_decision(
    connection: sqlite3.Connection,
    run_id: str,
    entry_id: str,
    decision: str,
    result_id: int | None,
    candidate_path: str,
    notes: str,
) -> None:
    connection.execute(
        """
        INSERT INTO broad_match_entry_decisions (
            run_id, entry_id, decision, result_id, candidate_path, notes, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, entry_id)
        DO UPDATE SET
            decision = excluded.decision,
            result_id = excluded.result_id,
            candidate_path = excluded.candidate_path,
            notes = excluded.notes,
            created_at = excluded.created_at
        """,
        (run_id, entry_id, decision, result_id, candidate_path, notes, _now()),
    )


def _upsert_canonical_media_decision(
    canonical_db: Path,
    result: dict[str, Any],
    role: str,
    status: str,
    review_status: str,
    decision: str,
    notes: str,
) -> None:
    candidate_path = Path(str(result["candidate_path"]))
    if not candidate_path.exists():
        raise FileNotFoundError(f"Missing broad visual candidate: {candidate_path}")
    sha256 = str(result.get("candidate_sha256") or "").strip() or _sha256_file(candidate_path)
    entry_id = str(result["entry_id"])
    decision_id = f"{entry_id}:{role}:{sha256[:16]}"
    now = _now()
    transformation = {
        "source": "broad_visual_review" if role == "external_original_reference" else "broad_visual_rejection",
        "source_path": str(candidate_path),
        "evidence": str(result.get("evidence") or ""),
        "review_decision": decision,
        "review_notes": notes,
        "broad_result_id": int(result.get("result_id") or 0),
        "broad_run_id": str(result.get("run_id") or ""),
        "original_is_read_only": True,
    }
    connection = sqlite3.connect(canonical_db)
    connection.row_factory = sqlite3.Row
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(media_assets)")}
        export_media = connection.execute(
            "SELECT * FROM media_assets WHERE id = ?",
            (str(result["project365_media_asset_id"]),),
        ).fetchone()
        if export_media is None:
            raise ValueError(f"Unknown Project365 media asset: {result['project365_media_asset_id']}")
        values: dict[str, Any] = {
            "id": decision_id,
            "entry_id": entry_id,
            "role": role,
            "source_file_id": None,
            "internal_filename": candidate_path.name,
            "storage_path": str(candidate_path),
            "sha256": sha256,
            "byte_size": int(result.get("byte_size") or candidate_path.stat().st_size),
            "mime_type": str(result.get("mime_type") or mimetypes.guess_type(str(candidate_path))[0] or "application/octet-stream"),
            "status": status,
            "review_status": review_status,
            "selected_default": 0,
            "transformation_json": json.dumps(transformation, sort_keys=True),
            "import_batch_id": export_media["import_batch_id"] if "import_batch_id" in columns else "",
            "created_at": now,
            "updated_at": now,
        }
        insert_columns = [column for column in values if column in columns]
        placeholders = ", ".join("?" for _ in insert_columns)
        update_columns = [
            column
            for column in insert_columns
            if column not in {"id", "entry_id", "role", "created_at"}
        ]
        update_clause = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        connection.execute(
            f"""
            INSERT INTO media_assets ({", ".join(insert_columns)})
            VALUES ({placeholders})
            ON CONFLICT(id)
            DO UPDATE SET {update_clause}
            """,
            [values[column] for column in insert_columns],
        )
        if role == "external_original_reference":
            connection.execute(
                """
                DELETE FROM media_assets
                WHERE entry_id = ?
                    AND role = 'external_original_reference'
                    AND id != ?
                """,
                (entry_id, decision_id),
            )
            connection.execute(
                """
                DELETE FROM media_assets
                WHERE entry_id = ?
                    AND role = 'external_original_rejected'
                """,
                (entry_id,),
            )
        connection.commit()
    finally:
        connection.close()


def _date_distance(entry_date: str, result: dict[str, Any]) -> int:
    try:
        target = dt.date.fromisoformat(entry_date)
    except ValueError:
        return -1
    distances = []
    for field in ("filename_dates", "media_creation_dates", "filesystem_dates"):
        for value in str(result.get(field, "") or "").split(";"):
            if not value:
                continue
            try:
                distances.append(abs((dt.date.fromisoformat(value[:10]) - target).days))
            except ValueError:
                pass
    return min(distances) if distances else -1


def _start_match_run(
    connection: sqlite3.Connection,
    run_id: str,
    started_at: str,
    target_scope: dict[str, Any],
    candidate_scope: dict[str, Any],
    settings: dict[str, Any],
    target_count: int,
) -> None:
    connection.execute(
        """
        INSERT INTO broad_match_runs (
            run_id, started_at, finished_at, status, phase, target_scope_json,
            candidate_scope_json, settings_json, target_count
        )
        VALUES (?, ?, '', 'running', 'starting', ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status = 'running',
            phase = 'resuming',
            target_count = excluded.target_count,
            settings_json = excluded.settings_json
        """,
        (
            run_id,
            started_at,
            json.dumps(target_scope, sort_keys=True),
            json.dumps(candidate_scope, sort_keys=True),
            json.dumps(settings, sort_keys=True),
            target_count,
        ),
    )
    connection.commit()


def _finish_match_run(
    connection: sqlite3.Connection,
    run_id: str,
    status: str,
    scanned: int,
    matched: int,
    result_count: int,
    errors: int,
) -> None:
    connection.execute(
        """
        UPDATE broad_match_runs
        SET finished_at = ?, status = ?, phase = 'finished', scanned_count = ?,
            matched_entries = ?, result_count = ?, error_count = ?, current_entry_id = ''
        WHERE run_id = ?
        """,
        (_now(), status, scanned, matched, result_count, errors, run_id),
    )


def _commit_index_progress(
    connection: sqlite3.Connection,
    run_id: str,
    started_at: str,
    roots_label: list[str],
    settings: dict[str, Any],
    total_count: int,
    date_coverage: dict[str, dict[str, int]],
    scanned: int,
    reused: int,
    indexed: int,
    skipped: int,
    errors: int,
    dry_run: bool,
    commit_interval: int,
    force: bool = False,
) -> None:
    if dry_run or scanned <= 0:
        return
    if not force and (commit_interval <= 0 or scanned % commit_interval != 0):
        return
    _record_index_run(
        connection,
        run_id,
        started_at,
        "",
        "running",
        [Path(root) for root in roots_label],
        settings,
        total_count,
        _date_coverage_payload(date_coverage),
        scanned,
        reused,
        indexed,
        skipped,
        errors,
    )
    connection.commit()
    print(
        "Broad visual index progress: "
        f"{scanned}/{total_count} checked, {reused} reused, "
        f"{indexed} fingerprinted, {skipped} skipped, {errors} errors",
        flush=True,
    )


def _record_index_current_candidate(
    connection: sqlite3.Connection,
    run_id: str,
    candidate_index: int,
    total_count: int,
    row: dict[str, Any],
    date_label: str,
    phase: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    connection.execute(
        """
        UPDATE broad_descriptor_runs
        SET
            total_candidate_count = ?,
            current_candidate_index = ?,
            current_candidate_path = ?,
            current_candidate_name = ?,
            current_candidate_extension = ?,
            current_candidate_byte_size = ?,
            current_candidate_date = ?,
            current_phase = ?,
            heartbeat_at = ?
        WHERE run_id = ?
        """,
        (
            total_count,
            candidate_index,
            str(row.get("path") or ""),
            str(row.get("filename") or Path(str(row.get("path") or "")).name),
            str(row.get("extension") or ""),
            int(row.get("byte_size") or 0),
            date_label,
            phase,
            _now(),
            run_id,
        ),
    )
    connection.commit()


def _record_index_run(
    connection: sqlite3.Connection,
    run_id: str,
    started_at: str,
    finished_at: str,
    status: str,
    roots: list[Path],
    settings: dict[str, Any],
    total_count: int,
    date_coverage: dict[str, Any],
    scanned: int,
    reused: int,
    indexed: int,
    skipped: int,
    errors: int,
) -> None:
    connection.execute(
        """
        INSERT INTO broad_descriptor_runs (
            run_id, started_at, finished_at, status, roots, settings_json,
            total_candidate_count, date_coverage_json, scanned_count, reused_descriptor_count,
            indexed_descriptor_count, skipped_candidate_count, error_count, current_candidate_index,
            current_candidate_path, current_candidate_name, current_candidate_extension,
            current_candidate_byte_size, current_candidate_date, current_phase, heartbeat_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            finished_at = excluded.finished_at,
            status = excluded.status,
            roots = excluded.roots,
            settings_json = excluded.settings_json,
            total_candidate_count = excluded.total_candidate_count,
            date_coverage_json = excluded.date_coverage_json,
            scanned_count = excluded.scanned_count,
            reused_descriptor_count = excluded.reused_descriptor_count,
            indexed_descriptor_count = excluded.indexed_descriptor_count,
            skipped_candidate_count = excluded.skipped_candidate_count,
            error_count = excluded.error_count,
            current_candidate_index = excluded.current_candidate_index,
            current_candidate_path = excluded.current_candidate_path,
            current_candidate_name = excluded.current_candidate_name,
            current_candidate_extension = excluded.current_candidate_extension,
            current_candidate_byte_size = excluded.current_candidate_byte_size,
            current_candidate_date = excluded.current_candidate_date,
            current_phase = excluded.current_phase,
            heartbeat_at = excluded.heartbeat_at
        """,
        (
            run_id,
            started_at,
            finished_at,
            status,
            ";".join(str(root) for root in roots),
            json.dumps(settings, sort_keys=True),
            total_count,
            json.dumps(date_coverage, sort_keys=True),
            scanned,
            reused,
            indexed,
            skipped,
            errors,
            0,
            "",
            "",
            "",
            0,
            "",
            "",
            "",
        ),
    )


def _record_index_error(
    connection: sqlite3.Connection,
    run_id: str,
    candidate_index: int,
    row: dict[str, Any],
    date_label: str,
    phase: str,
    error: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    connection.execute(
        """
        INSERT INTO broad_descriptor_errors (
            run_id, candidate_index, path, filename, candidate_date, phase, error, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            candidate_index,
            str(row.get("path") or ""),
            str(row.get("filename") or Path(str(row.get("path") or "")).name),
            date_label,
            phase,
            error,
            _now(),
        ),
    )


def _picker_queue_row(canonical_db: Path, result: dict[str, Any], fieldnames: list[str]) -> dict[str, str]:
    source = _project365_media_for_entry(canonical_db, str(result["entry_id"]))
    values = {
        "entry_id": result["entry_id"],
        "entry_date": result["entry_date"],
        "project365_media_asset_id": result["project365_media_asset_id"],
        "current_match_status": "needs_review",
        "current_decision": "",
        "candidate_path": result["candidate_path"],
        "candidate_filename": result["candidate_filename"],
        "candidate_sha256": result["candidate_sha256"],
        "byte_size": str(result["byte_size"]),
        "mime_type": result["mime_type"],
        "filename_dates": result["filename_dates"],
        "media_creation_dates": result["media_creation_dates"],
        "filesystem_dates": result["filesystem_dates"],
        "capture_timestamp": "",
        "capture_timestamp_source": "",
        "date_distance": "" if int(result["date_distance"]) < 0 else str(result["date_distance"]),
        "evidence": f"{result['evidence']};sent_to_picker",
        "candidate_filter_reason": "",
        "review_decision": "",
        "review_notes": "",
        "visual_rank": str(result["rank"]),
        "visual_score": f"{float(result['score']):.4f}",
        "visual_score_gap": f"{float(result['score_gap']):.4f}",
        "visual_likely": "true",
        "visual_method": str(result.get("method_version") or ""),
        "visual_best_view": result["best_view"],
        "visual_error": "",
    }
    values.update(source)
    return {field: str(values.get(field, "")) for field in fieldnames}


def _project365_media_for_entry(canonical_db: Path, entry_id: str) -> dict[str, str]:
    connection = sqlite3.connect(canonical_db)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT id, storage_path, internal_filename, sha256, byte_size, mime_type
            FROM media_assets
            WHERE entry_id = ? AND role = 'project365_export_png'
            LIMIT 1
            """,
            (entry_id,),
        ).fetchone()
        if not row:
            return {}
        return {
            "project365_media_asset_id": str(row["id"] or ""),
            "project365_source_path": str(row["storage_path"] or ""),
            "project365_filename": str(row["internal_filename"] or Path(str(row["storage_path"] or "")).name),
            "project365_sha256": str(row["sha256"] or ""),
            "project365_byte_size": str(row["byte_size"] or ""),
            "project365_mime_type": str(row["mime_type"] or ""),
        }
    finally:
        connection.close()


def _read_picker_queue(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def _write_picker_queue(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _queue_fieldnames(existing: list[str]) -> list[str]:
    required = [
        "entry_id",
        "entry_date",
        "project365_media_asset_id",
        "current_match_status",
        "current_decision",
        "candidate_path",
        "candidate_filename",
        "candidate_sha256",
        "byte_size",
        "mime_type",
        "filename_dates",
        "media_creation_dates",
        "filesystem_dates",
        "capture_timestamp",
        "capture_timestamp_source",
        "date_distance",
        "evidence",
        "candidate_filter_reason",
        "review_decision",
        "review_notes",
        "visual_rank",
        "visual_score",
        "visual_score_gap",
        "visual_likely",
        "visual_method",
        "visual_best_view",
        "visual_error",
    ]
    merged = list(existing)
    for field in required:
        if field not in merged:
            merged.append(field)
    return merged


def _result_exists_for_entry(connection: sqlite3.Connection, run_id: str, entry_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM broad_match_results WHERE run_id = ? AND entry_id = ? LIMIT 1",
        (run_id, entry_id),
    ).fetchone()
    return row is not None


def _latest_match_run_id(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT run_id FROM broad_match_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return str(row["run_id"]) if row else ""


def _entry_ids_from_list(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing broad-search-needed list: {path}")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("entry_ids", payload) if isinstance(payload, dict) else payload
        return {str(value).strip() for value in values if str(value).strip()}
    with path.open(encoding="utf-8", newline="") as handle:
        if path.suffix.lower() == ".csv":
            reader = csv.DictReader(handle)
            return {str(row.get("entry_id", "")).strip() for row in reader if str(row.get("entry_id", "")).strip()}
        return {line.strip() for line in handle if line.strip()}


def _iter_image_files(root: Path) -> Any:
    if root.is_file() and root.suffix.lower() in BROAD_IMAGE_EXTENSIONS:
        yield root
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in BROAD_IMAGE_EXTENSIONS:
            yield path


def _normalized_roots(roots: list[Path]) -> list[Path]:
    normalized = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if str(resolved) not in [str(item) for item in normalized]:
            normalized.append(resolved)
    return normalized


def _single_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _latest_benchmark_json(report_dir: Path) -> Path | None:
    latest = report_dir / "broad_visual_benchmark_latest.json"
    if latest.exists():
        return latest
    reports = sorted(report_dir.glob("broad_visual_benchmark_*.json"))
    reports = [path for path in reports if path.name != "broad_visual_benchmark_latest.json"]
    return reports[-1] if reports else None


def _benchmark_candidate(target: dict[str, str], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "result_id": "",
        "rank": int(result.get("rank") or 0),
        "candidate_path": str(result.get("path", "")),
        "candidate_filename": str(result.get("filename", "")),
        "score": float(result.get("score") or 0),
        "score_gap": float(result.get("score_gap") or 0),
        "best_view": str(result.get("best_view", "")),
        "date_distance": _date_distance(str(target.get("entry_date", "")), result),
        "sent_to_picker_at": "",
    }


def _benchmark_record(
    target: dict[str, str],
    status: str,
    error: str,
    rank: int,
    score: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "entry_id": target["entry_id"],
        "entry_date": target["entry_date"],
        "source_path": target.get("source_path", ""),
        "confirmed_path": target.get("confirmed_path", ""),
        "status": status,
        "rank": rank,
        "score": score,
        "error": error,
        "candidates": candidates,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_run_id(prefix: str) -> str:
    return f"{prefix}:{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}:{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _print_summary(title: str, summary: dict[str, Any]) -> None:
    print(f"{title}: PASS")
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
