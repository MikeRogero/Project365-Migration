#!/usr/bin/env python3
"""Offline broad visual matching for unresolved Project365 originals."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
import signal
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project365_original_matcher import IMAGE_EXTENSIONS
from project365_photo_library_index import (
    FILENAME_DATE_PATTERNS,
    _filename_dates,
    _filesystem_dates,
    _filesystem_mtime_utc,
    _folder_path_prefix,
    _jpeg_exif_dates,
    _quality_rank,
)
from project365_original_reference_pipeline import (
    ACCEPT_DECISIONS,
    ASSOCIATED_PHOTO_DECISIONS,
    FALLBACK_DECISIONS,
    REJECT_DECISIONS,
)
from project365_original_picker import _image_dimensions as _actual_image_dimensions_text
from project365_original_picker import _parse_dimensions_text
from project365_visual_ranker import DEFAULT_SAMPLE_SIZE, DEFAULT_THUMBNAIL_TOOL, VisualImage, load_visual_image
from project365_visual_ranker import _view_descriptor, _visual_score


METHOD_VERSION = "broad_visual_square_window_v1"
THUMBNAIL_METHOD_VERSION = "broad_visual_square_window_thumb_v2"
ROUGH_PREFILTER_METHOD_VERSION = "broad_visual_rough_prefilter_v1"
DEFAULT_DB_FILENAME = "broad_visual_match.sqlite"
DEFAULT_TOP_N = 20
DEFAULT_DENSITY = 9
DEFAULT_THUMBNAIL_SIZE = 1024
DEFAULT_INDEX_WORKERS = 4
DEFAULT_RESULT_PAGE_SIZE = 50
DEFAULT_INDEX_COMMIT_INTERVAL = 100
DEFAULT_PREFILTER_COMMIT_INTERVAL = 500
DEFAULT_PREFILTER_SHORTLIST_SIZE = 1000
DEFAULT_PREFILTER_BAND_HIT_LIMIT = 50000
DEFAULT_PREFILTER_BENCHMARK_SHORTLIST_SIZES = [100, 500, 1000, 5000]
DEFAULT_PREFILTER_SCORE_MULTIPLIER = 8
DEFAULT_PREFILTER_MAX_SCORE_CANDIDATES = 5000
DEFAULT_MIN_FINGERPRINT_COVERAGE = 0.80
DEFAULT_MIN_COVERAGE_PHOTO_COUNT = 20
VISUAL_SCORE_TIE_PRECISION = 6
REPORT_DIR = Path("exports") / "verification_reports"
BROAD_IMAGE_EXTENSIONS = set(IMAGE_EXTENSIONS) | {".bmp"}
CURRENT_BROAD_MATCH_SLOT = "broad_visual_match"
CURRENT_ROUGH_MATCH_SLOT = "rough_visual_match"
CURRENT_MATCH_SLOTS = {CURRENT_BROAD_MATCH_SLOT, CURRENT_ROUGH_MATCH_SLOT}
_STOP_REQUESTED = False
_PICKER_QUEUE_ENTRY_IDS_CACHE: dict[str, tuple[tuple[int, int], set[str]]] = {}
_PICKER_PENDING_DECISIONS_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


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
    coverage_warning: str = ""


@dataclass(frozen=True)
class RoughPrefilterSummary:
    db_path: str
    run_id: str
    total_descriptor_count: int
    scanned_count: int
    reused_feature_count: int
    indexed_feature_count: int
    error_count: int
    dry_run: bool


@dataclass(frozen=True)
class RoughShortlist:
    candidates: list[dict[str, Any]]
    total_hit_count: int
    capped_band_count: int
    shortlist_size: int
    band_query_count: int
    elapsed_ms: int
    low_confidence: bool
    error: str = ""


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

    prefilter_parser = subparsers.add_parser("prefilter")
    prefilter_parser.add_argument("--density", type=int, default=DEFAULT_DENSITY)
    prefilter_parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE)
    prefilter_parser.add_argument("--commit-interval", type=int, default=DEFAULT_PREFILTER_COMMIT_INTERVAL)
    prefilter_parser.add_argument("--rebuild-stale", "--overwrite-existing", dest="rebuild_stale", action="store_true")
    prefilter_parser.add_argument("--dry-run", action="store_true")

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

    no_date_parser = subparsers.add_parser("match-no-date")
    _add_scope_args(no_date_parser)
    no_date_parser.add_argument("--max-results", type=int, default=DEFAULT_TOP_N)
    no_date_parser.add_argument("--shortlist-size", type=int, default=DEFAULT_PREFILTER_SHORTLIST_SIZE)
    no_date_parser.add_argument("--per-band-hit-limit", type=int, default=DEFAULT_PREFILTER_BAND_HIT_LIMIT)
    no_date_parser.add_argument("--density", type=int, default=DEFAULT_DENSITY)
    no_date_parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE)
    no_date_parser.add_argument("--thumbnail-tool", default=DEFAULT_THUMBNAIL_TOOL)
    no_date_parser.add_argument("--include-low-quality", action="store_true")
    no_date_parser.add_argument("--resume-run", default="")
    no_date_parser.add_argument("--dry-run", action="store_true")

    benchmark_parser = subparsers.add_parser("benchmark")
    _add_scope_args(benchmark_parser)
    benchmark_parser.add_argument("--year", default="")
    benchmark_parser.add_argument("--max-results", type=int, default=DEFAULT_TOP_N)
    benchmark_parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE)
    benchmark_parser.add_argument("--thumbnail-tool", default=DEFAULT_THUMBNAIL_TOOL)
    benchmark_parser.add_argument("--report-dir", default="")
    benchmark_parser.add_argument("--use-prefilter", action="store_true")
    benchmark_parser.add_argument("--shortlist-size", type=int, default=DEFAULT_PREFILTER_SHORTLIST_SIZE)
    benchmark_parser.add_argument("--per-band-hit-limit", type=int, default=DEFAULT_PREFILTER_BAND_HIT_LIMIT)
    benchmark_parser.add_argument("--shortlist-sizes", default="")

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
    if args.command == "prefilter":
        summary = build_rough_prefilter(
            canonical_root=canonical_root,
            db_path=db_path,
            density=args.density,
            thumbnail_size=args.thumbnail_size,
            commit_interval=args.commit_interval,
            rebuild_stale=args.rebuild_stale,
            dry_run=args.dry_run,
        )
        _print_summary("Rough visual prefilter", summary.__dict__)
        return 130 if _STOP_REQUESTED else 0
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
    if args.command == "match-no-date":
        summary = run_no_date_match_batch(
            canonical_root=canonical_root,
            db_path=db_path,
            target_scope=target_scope_from_args(args),
            max_results=args.max_results,
            shortlist_size=args.shortlist_size,
            per_band_hit_limit=args.per_band_hit_limit,
            density=args.density,
            thumbnail_size=args.thumbnail_size,
            thumbnail_tool=args.thumbnail_tool,
            include_low_quality=args.include_low_quality,
            resume_run_id=args.resume_run,
            dry_run=args.dry_run,
        )
        _print_summary("No-date rough visual match batch", summary.__dict__)
        return 130 if _STOP_REQUESTED else 0
    if args.command == "benchmark":
        shortlist_sizes = _parse_int_list(args.shortlist_sizes) or DEFAULT_PREFILTER_BENCHMARK_SHORTLIST_SIZES
        report = benchmark_confirmed_originals(
            canonical_root=canonical_root,
            db_path=db_path,
            target_scope={**target_scope_from_args(args), **_year_scope(args.year)},
            max_results=args.max_results,
            thumbnail_size=args.thumbnail_size,
            thumbnail_tool=args.thumbnail_tool,
            report_dir=Path(args.report_dir) if args.report_dir else canonical_root / REPORT_DIR,
            use_prefilter=args.use_prefilter,
            shortlist_size=args.shortlist_size,
            per_band_hit_limit=args.per_band_hit_limit,
            benchmark_shortlist_sizes=shortlist_sizes,
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


def _parse_int_list(value: str) -> list[int]:
    result = []
    for chunk in str(value or "").replace(";", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        number = int(text)
        if number > 0:
            result.append(number)
    return result


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
            original_width INTEGER NOT NULL DEFAULT 0,
            original_height INTEGER NOT NULL DEFAULT 0,
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
        CREATE INDEX IF NOT EXISTS idx_broad_descriptors_error
            ON broad_descriptors(error);
        CREATE INDEX IF NOT EXISTS idx_broad_descriptors_method_error
            ON broad_descriptors(method_version, error);

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
        CREATE INDEX IF NOT EXISTS idx_broad_descriptor_errors_run_candidate
            ON broad_descriptor_errors(run_id, candidate_index);

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
            current_target_index INTEGER NOT NULL DEFAULT 0,
            current_candidate_count INTEGER NOT NULL DEFAULT 0,
            processed_target_count INTEGER NOT NULL DEFAULT 0,
            heartbeat_at TEXT NOT NULL DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS broad_current_runs (
            slot TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_broad_results_run_entry_rank
            ON broad_match_results(run_id, entry_id, rank);
        CREATE INDEX IF NOT EXISTS idx_broad_results_entry_rank
            ON broad_match_results(entry_id, rank);

        CREATE TABLE IF NOT EXISTS rough_prefilter_features (
            path TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            filename TEXT NOT NULL,
            extension TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            view_count INTEGER NOT NULL DEFAULT 0,
            source_method_version TEXT NOT NULL,
            source_density INTEGER NOT NULL,
            source_thumbnail_size INTEGER NOT NULL,
            prefilter_method_version TEXT NOT NULL,
            feature_json TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rough_prefilter_features_method_error
            ON rough_prefilter_features(prefilter_method_version, source_method_version, error);

        CREATE TABLE IF NOT EXISTS rough_prefilter_bands (
            path TEXT NOT NULL,
            view_name TEXT NOT NULL,
            band_name TEXT NOT NULL,
            band_value TEXT NOT NULL,
            PRIMARY KEY (path, view_name, band_name, band_value)
        );
        CREATE INDEX IF NOT EXISTS idx_rough_prefilter_bands_lookup
            ON rough_prefilter_bands(band_name, band_value, path);

        CREATE TABLE IF NOT EXISTS rough_prefilter_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            source_method_version TEXT NOT NULL,
            source_density INTEGER NOT NULL,
            source_thumbnail_size INTEGER NOT NULL,
            prefilter_method_version TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            total_descriptor_count INTEGER NOT NULL DEFAULT 0,
            scanned_count INTEGER NOT NULL DEFAULT 0,
            reused_feature_count INTEGER NOT NULL DEFAULT 0,
            indexed_feature_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            current_path TEXT NOT NULL DEFAULT '',
            current_phase TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS rough_prefilter_errors (
            error_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            candidate_index INTEGER NOT NULL,
            path TEXT NOT NULL,
            filename TEXT NOT NULL,
            phase TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rough_prefilter_errors_run_candidate
            ON rough_prefilter_errors(run_id, candidate_index);

        CREATE TABLE IF NOT EXISTS broad_match_prefilter_metrics (
            run_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            prefilter_method_version TEXT NOT NULL,
            source_method_version TEXT NOT NULL,
            shortlist_size INTEGER NOT NULL DEFAULT 0,
            prefilter_hit_count INTEGER NOT NULL DEFAULT 0,
            capped_band_count INTEGER NOT NULL DEFAULT 0,
            band_query_count INTEGER NOT NULL DEFAULT 0,
            descriptor_load_count INTEGER NOT NULL DEFAULT 0,
            final_scored_count INTEGER NOT NULL DEFAULT 0,
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            low_confidence INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, entry_id)
        );
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
    for column, definition in {
        "current_target_index": "INTEGER NOT NULL DEFAULT 0",
        "current_candidate_count": "INTEGER NOT NULL DEFAULT 0",
        "processed_target_count": "INTEGER NOT NULL DEFAULT 0",
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        _ensure_column(connection, "broad_match_runs", column, definition)
    for column, definition in {
        "prefilter_metrics_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        _ensure_column(connection, "broad_match_runs", column, definition)
    for column, definition in {
        "original_width": "INTEGER NOT NULL DEFAULT 0",
        "original_height": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _ensure_column(connection, "broad_descriptors", column, definition)
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
            path, root, date_label, indexed_sha256, photo_index_dates = _path_root_date(path_item)
            path = path.expanduser()
            path_text = str(path.resolve()) if path.exists() else str(path)
            if path_text in seen_paths:
                continue
            seen_paths.add(path_text)
            scanned += 1
            row = _descriptor_input_row(root, path, indexed_sha256, photo_index_dates)
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
                _refresh_descriptor_metadata(connection, row)
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


def build_rough_prefilter(
    canonical_root: Path,
    db_path: Path | None = None,
    density: int = DEFAULT_DENSITY,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    commit_interval: int = DEFAULT_PREFILTER_COMMIT_INTERVAL,
    rebuild_stale: bool = False,
    dry_run: bool = False,
    stop_after_batches: int = 0,
) -> RoughPrefilterSummary:
    if density <= 0:
        raise ValueError("density must be positive")
    if thumbnail_size < 0:
        raise ValueError("thumbnail_size must not be negative")
    effective_db_path = db_path or default_db_path(canonical_root)
    source_method_version = _descriptor_method_version(thumbnail_size)
    run_id = _new_run_id("rough-prefilter")
    started_at = _now()
    scanned = reused = indexed = errors = 0
    connection = connect_broad_db(effective_db_path)
    try:
        descriptor_rows = _prefilter_source_descriptor_rows(connection, source_method_version, density)
        total = len(descriptor_rows)
        settings = {
            "source": "broad_descriptors",
            "source_method_version": source_method_version,
            "source_density": density,
            "source_thumbnail_size": int(thumbnail_size or 0),
            "prefilter_method_version": ROUGH_PREFILTER_METHOD_VERSION,
            "commit_interval": commit_interval,
            "rebuild_stale": rebuild_stale,
            "dry_run": dry_run,
        }
        if not dry_run:
            _record_prefilter_run(
                connection,
                run_id,
                started_at,
                "",
                "running",
                source_method_version,
                density,
                thumbnail_size,
                settings,
                total,
                scanned,
                reused,
                indexed,
                errors,
                "",
                "starting",
            )
            connection.commit()
            print(f"Rough visual prefilter starting: {total} stored fingerprints found", flush=True)
        batches = 0
        for index, row in enumerate(descriptor_rows, start=1):
            if _STOP_REQUESTED:
                break
            scanned += 1
            if not dry_run:
                _record_prefilter_run(
                    connection,
                    run_id,
                    started_at,
                    "",
                    "running",
                    source_method_version,
                    density,
                    thumbnail_size,
                    settings,
                    total,
                    scanned,
                    reused,
                    indexed,
                    errors,
                    str(row["path"]),
                    "checking existing prefilter",
                    commit=False,
                )
            if not rebuild_stale and _prefilter_feature_is_current(
                connection, row, source_method_version, density, thumbnail_size
            ):
                reused += 1
            else:
                try:
                    descriptor = json.loads(str(row["descriptor_json"]))
                    feature_payload, band_rows = _rough_prefilter_payload(
                        descriptor,
                        source_method_version,
                    )
                    if not dry_run:
                        _upsert_prefilter_feature(
                            connection,
                            row,
                            source_method_version,
                            density,
                            thumbnail_size,
                            feature_payload,
                            band_rows,
                            "",
                        )
                    indexed += 1
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors += 1
                    if not dry_run:
                        _upsert_prefilter_feature(
                            connection,
                            row,
                            source_method_version,
                            density,
                            thumbnail_size,
                            {},
                            [],
                            type(exc).__name__,
                        )
                        _record_prefilter_error(
                            connection,
                            run_id,
                            index,
                            row,
                            "feature_extraction",
                            type(exc).__name__,
                        )
            if not dry_run and _should_commit(scanned, commit_interval):
                batches += 1
                _record_prefilter_run(
                    connection,
                    run_id,
                    started_at,
                    "",
                    "running",
                    source_method_version,
                    density,
                    thumbnail_size,
                    settings,
                    total,
                    scanned,
                    reused,
                    indexed,
                    errors,
                    str(row["path"]),
                    "building prefilter",
                )
                print(
                    "Rough visual prefilter progress: "
                    f"{scanned}/{total} checked, {reused} reused, {indexed} indexed, {errors} errors",
                    flush=True,
                )
                if stop_after_batches and batches >= stop_after_batches:
                    break
        status = "cancelled" if _STOP_REQUESTED else "partial" if errors else "pass"
        if not dry_run:
            _record_prefilter_run(
                connection,
                run_id,
                started_at,
                _now(),
                status,
                source_method_version,
                density,
                thumbnail_size,
                settings,
                total,
                scanned,
                reused,
                indexed,
                errors,
                "",
                "finished",
            )
            connection.commit()
            print(
                "Rough visual prefilter "
                f"{status}: {scanned}/{total} checked, {reused} reused, {indexed} indexed, {errors} errors",
                flush=True,
            )
    finally:
        connection.close()
    return RoughPrefilterSummary(
        db_path=str(effective_db_path),
        run_id=run_id,
        total_descriptor_count=total,
        scanned_count=scanned,
        reused_feature_count=reused,
        indexed_feature_count=indexed,
        error_count=errors,
        dry_run=dry_run,
    )


def run_no_date_match_batch(
    canonical_root: Path,
    db_path: Path | None = None,
    target_scope: dict[str, Any] | None = None,
    max_results: int = DEFAULT_TOP_N,
    shortlist_size: int = DEFAULT_PREFILTER_SHORTLIST_SIZE,
    per_band_hit_limit: int = DEFAULT_PREFILTER_BAND_HIT_LIMIT,
    density: int = DEFAULT_DENSITY,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
    include_low_quality: bool = False,
    resume_run_id: str = "",
    dry_run: bool = False,
) -> BroadMatchSummary:
    if max_results <= 0:
        raise ValueError("max_results must be positive")
    if shortlist_size <= 0:
        raise ValueError("shortlist_size must be positive")
    if per_band_hit_limit <= 0:
        raise ValueError("per_band_hit_limit must be positive")
    effective_db_path = db_path or default_db_path(canonical_root)
    source_method_version = _descriptor_method_version(thumbnail_size)
    scope = target_scope or {}
    run_id = resume_run_id.strip() or _new_run_id("rough-no-date-match")
    started_at = _now()
    targets = _target_rows(canonical_root / "canonical.db", scope)
    rejected_candidates = _rejected_candidates_by_entry(canonical_root / "canonical.db")
    scanned = matched = result_count = errors = 0
    coverage_warning = ""
    processed_targets = 0
    connection = connect_broad_db(effective_db_path)
    try:
        _ensure_prefilter_ready(connection, source_method_version, density, thumbnail_size)
        if not dry_run:
            _start_match_run(
                connection,
                run_id,
                started_at,
                scope,
                {
                    "candidate_scope": "rough_prefilter_no_date",
                    "date_constraints": "none",
                },
                {
                    "max_results": max_results,
                    "shortlist_size": shortlist_size,
                    "per_band_hit_limit": per_band_hit_limit,
                    "density": density,
                    "thumbnail_size": thumbnail_size,
                    "thumbnail_tool": thumbnail_tool,
                    "source_method_version": source_method_version,
                    "prefilter_method_version": ROUGH_PREFILTER_METHOD_VERSION,
                    "include_low_quality": include_low_quality,
                    "dry_run": dry_run,
                },
                len(targets),
            )
            _set_current_match_run(connection, CURRENT_ROUGH_MATCH_SLOT, run_id)
            connection.commit()
        try:
            for target_index, target in enumerate(targets, start=1):
                if _STOP_REQUESTED:
                    break
                if not dry_run:
                    _record_match_progress(
                        connection,
                        run_id,
                        "rough prefiltering",
                        target["entry_id"],
                        target_index,
                        len(targets),
                        0,
                        target_index - 1,
                        scanned,
                        matched,
                        result_count,
                        errors,
                    )
                if resume_run_id and _result_exists_for_entry(connection, run_id, target["entry_id"]):
                    processed_targets = target_index
                    continue
                source_descriptor, source_error = _build_descriptor_for_path(
                    Path(target["source_path"]), density, thumbnail_size, thumbnail_tool
                )
                if source_error:
                    errors += 1
                    if not dry_run:
                        _record_prefilter_match_metrics(
                            connection,
                            run_id,
                            target["entry_id"],
                            source_method_version,
                            shortlist=RoughShortlist([], 0, 0, 0, 0, 0, True, source_error),
                            descriptor_load_count=0,
                            final_scored_count=0,
                        )
                    processed_targets = target_index
                    continue
                shortlist = rough_prefilter_shortlist(
                    connection,
                    source_descriptor,
                    source_method_version=source_method_version,
                    density=density,
                    thumbnail_size=thumbnail_size,
                    shortlist_size=shortlist_size,
                    per_band_hit_limit=per_band_hit_limit,
                    include_low_quality=include_low_quality,
                )
                shortlist_candidates = _exclude_target_export_copies(target, shortlist.candidates)
                shortlist_candidates = _exclude_rejected_candidates(target, shortlist_candidates, rejected_candidates)
                candidate_paths = [str(row["path"]) for row in shortlist_candidates[:shortlist_size]]
                descriptor_rows = _descriptor_rows_by_paths(
                    connection,
                    candidate_paths,
                    source_method_version,
                    include_low_quality,
                )
                if len(descriptor_rows) > shortlist_size:
                    raise RuntimeError("No-date match guardrail failed: descriptor load exceeded shortlist size.")
                if not dry_run:
                    _record_match_progress(
                        connection,
                        run_id,
                        "dense scoring shortlist",
                        target["entry_id"],
                        target_index,
                        len(targets),
                        len(descriptor_rows),
                        target_index - 1,
                        scanned,
                        matched,
                        result_count,
                        errors,
                    )
                scanned += len(descriptor_rows)
                scored = _score_candidate_descriptors(source_descriptor, descriptor_rows, max_results)
                if scored:
                    matched += 1
                result_count += len(scored)
                if not dry_run:
                    connection.execute(
                        "DELETE FROM broad_match_results WHERE run_id = ? AND entry_id = ?",
                        (run_id, target["entry_id"]),
                    )
                    _insert_results(connection, run_id, target, scored, evidence_prefix="rough_visual_no_date")
                    _record_prefilter_match_metrics(
                        connection,
                        run_id,
                        target["entry_id"],
                        source_method_version,
                        shortlist=shortlist,
                        descriptor_load_count=len(descriptor_rows),
                        final_scored_count=len(scored),
                    )
                    _update_match_prefilter_summary(connection, run_id)
                    _record_match_progress(
                        connection,
                        run_id,
                        "matching",
                        target["entry_id"],
                        target_index,
                        len(targets),
                        len(descriptor_rows),
                        target_index,
                        scanned,
                        matched,
                        result_count,
                        errors,
                    )
                    print(
                        "No-date rough visual match progress: "
                        f"{target_index}/{len(targets)} entries, {len(shortlist_candidates)} shortlisted, "
                        f"{scanned} dense descriptor loads, {result_count} saved results, {errors} errors",
                        flush=True,
                    )
                processed_targets = target_index
        except Exception as exc:
            if not dry_run:
                errors += 1
                _finish_match_run(
                    connection,
                    run_id,
                    "fail",
                    scanned,
                    matched,
                    result_count,
                    errors,
                    error=str(exc),
                    processed_target_count=processed_targets,
                )
                connection.commit()
            raise
        if not dry_run:
            status = "cancelled" if _STOP_REQUESTED else "pass"
            _finish_match_run(
                connection,
                run_id,
                status,
                scanned,
                matched,
                result_count,
                errors,
                processed_target_count=processed_targets if _STOP_REQUESTED else None,
            )
            _update_match_prefilter_summary(connection, run_id)
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
        coverage_warning=coverage_warning,
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
            _set_current_match_run(connection, CURRENT_BROAD_MATCH_SLOT, run_id)
            connection.commit()
        candidate_availability_roots = _candidate_availability_roots(
            connection,
            candidate_scope=candidate_scope,
            candidate_roots=roots,
            method_version=method_version,
        )
        target_index = 0
        processed_targets = 0
        try:
            coverage_warning = ""
            if candidate_scope == "date_window_limited":
                coverage_warning = _fingerprint_coverage_warning_for_match(
                    canonical_root,
                    effective_db_path,
                    targets,
                    date_window_days,
                    method_version,
                )
                if coverage_warning:
                    print(f"coverage_warning: {coverage_warning}", flush=True)
            _ensure_candidate_roots_available(candidate_availability_roots)
            for target_index, target in enumerate(targets, start=1):
                if _STOP_REQUESTED:
                    break
                _ensure_candidate_roots_available(candidate_availability_roots)
                if not dry_run:
                    _record_match_progress(
                        connection,
                        run_id,
                        "matching",
                        target["entry_id"],
                        target_index,
                        len(targets),
                        0,
                        target_index - 1,
                        scanned,
                        matched,
                        result_count,
                        errors,
                    )
                if resume_run_id and _result_exists_for_entry(connection, run_id, target["entry_id"]):
                    if not dry_run:
                        _record_match_progress(
                            connection,
                            run_id,
                            "skipping existing result",
                            target["entry_id"],
                            target_index,
                            len(targets),
                            0,
                            target_index,
                            scanned,
                            matched,
                            result_count,
                            errors,
                        )
                    processed_targets = target_index
                    continue
                source_path = Path(target["source_path"])
                source_descriptor, source_error = _build_descriptor_for_path(
                    source_path, density, thumbnail_size, thumbnail_tool
                )
                if source_error:
                    errors += 1
                    if not dry_run:
                        _record_match_progress(
                            connection,
                            run_id,
                            "source fingerprint failed",
                            target["entry_id"],
                            target_index,
                            len(targets),
                            0,
                            target_index,
                            scanned,
                            matched,
                            result_count,
                            errors,
                        )
                    processed_targets = target_index
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
                if not dry_run:
                    _record_match_progress(
                        connection,
                        run_id,
                        "scoring candidates",
                        target["entry_id"],
                        target_index,
                        len(targets),
                        len(candidates),
                        target_index - 1,
                        scanned,
                        matched,
                        result_count,
                        errors,
                    )
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
                    _record_match_progress(
                        connection,
                        run_id,
                        "matching",
                        target["entry_id"],
                        target_index,
                        len(targets),
                        len(candidates),
                        target_index,
                        scanned,
                        matched,
                        result_count,
                        errors,
                    )
                    print(
                        "Broad visual match progress: "
                        f"{target_index}/{len(targets)} entries, {scanned} candidates scanned, "
                        f"{matched} matched, {result_count} saved results, {errors} errors",
                        flush=True,
                    )
                processed_targets = target_index
        except Exception as exc:
            if not dry_run:
                errors += 1
                _finish_match_run(
                    connection,
                    run_id,
                    "fail",
                    scanned,
                    matched,
                    result_count,
                    errors,
                    error=str(exc),
                    processed_target_count=processed_targets,
                )
                connection.commit()
            raise
        if not dry_run:
            status = "cancelled" if _STOP_REQUESTED else "pass"
            _finish_match_run(
                connection,
                run_id,
                status,
                scanned,
                matched,
                result_count,
                errors,
                processed_target_count=processed_targets if _STOP_REQUESTED else None,
            )
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
        coverage_warning=coverage_warning,
    )


def benchmark_confirmed_originals(
    canonical_root: Path,
    db_path: Path | None = None,
    target_scope: dict[str, Any] | None = None,
    max_results: int = DEFAULT_TOP_N,
    thumbnail_size: int = DEFAULT_THUMBNAIL_SIZE,
    thumbnail_tool: str = DEFAULT_THUMBNAIL_TOOL,
    report_dir: Path | None = None,
    use_prefilter: bool = False,
    shortlist_size: int = DEFAULT_PREFILTER_SHORTLIST_SIZE,
    per_band_hit_limit: int = DEFAULT_PREFILTER_BAND_HIT_LIMIT,
    benchmark_shortlist_sizes: list[int] | None = None,
) -> dict[str, Any]:
    effective_db_path = db_path or default_db_path(canonical_root)
    method_version = _descriptor_method_version(thumbnail_size)
    effective_report_dir = report_dir or canonical_root / REPORT_DIR
    effective_report_dir.mkdir(parents=True, exist_ok=True)
    rows = _confirmed_target_rows(canonical_root / "canonical.db", target_scope or {})
    connection = connect_broad_db(effective_db_path)
    records: list[dict[str, Any]] = []
    try:
        benchmark_sizes = sorted(
            set(int(value) for value in (benchmark_shortlist_sizes or DEFAULT_PREFILTER_BENCHMARK_SHORTLIST_SIZES) if int(value) > 0)
        )
        effective_shortlist_size = max([shortlist_size, *benchmark_sizes]) if use_prefilter else shortlist_size
        all_candidates: list[dict[str, Any]] = []
        if use_prefilter:
            _ensure_prefilter_ready(connection, method_version, DEFAULT_DENSITY, thumbnail_size)
        else:
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
                records.append(_benchmark_record(target, "", "source_fingerprint_error", 0, "", []))
                continue
            prefilter: RoughShortlist | None = None
            candidate_pool: list[dict[str, Any]]
            expected_path = str(Path(target["confirmed_path"]).resolve())
            prefilter_rank = 0
            prefilter_recall: dict[str, bool] = {}
            if use_prefilter:
                prefilter = rough_prefilter_shortlist(
                    connection,
                    source_descriptor,
                    source_method_version=method_version,
                    density=DEFAULT_DENSITY,
                    thumbnail_size=thumbnail_size,
                    shortlist_size=effective_shortlist_size,
                    per_band_hit_limit=per_band_hit_limit,
                    include_low_quality=True,
                )
                shortlist_paths = [str(Path(row["path"]).resolve()) for row in prefilter.candidates]
                for rank, path in enumerate(shortlist_paths, start=1):
                    if path == expected_path:
                        prefilter_rank = rank
                        break
                prefilter_recall = {
                    f"prefilter_recall_at_{size}": bool(prefilter_rank and prefilter_rank <= size)
                    for size in benchmark_sizes
                }
                candidate_pool = _descriptor_rows_by_paths(
                    connection,
                    [str(row["path"]) for row in prefilter.candidates],
                    method_version,
                    include_low_quality=True,
                )
            else:
                candidate_pool = all_candidates
            scored = _score_candidate_descriptors(
                source_descriptor,
                _exclude_target_export_copies(
                    target,
                    candidate_pool,
                    allowed_paths=[str(target.get("confirmed_path", "") or "")],
                ),
                max_results,
            )
            rank = 0
            score = ""
            for result in scored:
                if str(Path(result["path"]).resolve()) == expected_path:
                    rank = int(result["rank"])
                    score = f"{float(result['score']):.4f}"
                    break
            if rank:
                status = "hit"
                error = ""
            elif use_prefilter and not prefilter_rank:
                status = "miss"
                error = "prefilter_miss"
            elif use_prefilter:
                status = "miss"
                error = "dense_ranker_miss"
            else:
                status = "miss"
                error = ""
            records.append(
                {
                    **_benchmark_record(
                        target,
                        status,
                        error,
                        rank,
                        score,
                        [_benchmark_candidate(target, result) for result in scored],
                    ),
                    "prefilter_rank": prefilter_rank,
                    "prefilter_shortlist_size": prefilter.shortlist_size if prefilter else 0,
                    "prefilter_hit_count": prefilter.total_hit_count if prefilter else 0,
                    "prefilter_capped_band_count": prefilter.capped_band_count if prefilter else 0,
                    "prefilter_elapsed_ms": prefilter.elapsed_ms if prefilter else 0,
                    **prefilter_recall,
                }
            )
    finally:
        connection.close()
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = effective_report_dir / f"broad_visual_benchmark_{timestamp}.csv"
    json_path = effective_report_dir / f"broad_visual_benchmark_{timestamp}.json"
    fieldnames = [
        "entry_id",
        "entry_date",
        "confirmed_path",
        "status",
        "rank",
        "score",
        "error",
        "prefilter_rank",
        "prefilter_shortlist_size",
        "prefilter_hit_count",
        "prefilter_capped_band_count",
        "prefilter_elapsed_ms",
    ]
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
        "use_prefilter": use_prefilter,
        "prefilter_method_version": ROUGH_PREFILTER_METHOD_VERSION if use_prefilter else "",
        "prefilter_shortlist_size": shortlist_size if use_prefilter else 0,
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }
    if use_prefilter:
        for size in sorted(
            set(int(value) for value in (benchmark_shortlist_sizes or DEFAULT_PREFILTER_BENCHMARK_SHORTLIST_SIZES) if int(value) > 0)
        ):
            key = f"prefilter_recall_at_{size}"
            summary[f"{key}_count"] = sum(1 for row in records if row.get(key))
            summary[key] = round(int(summary[f"{key}_count"]) / max(1, len(records)), 4)
        summary["prefilter_miss_count"] = sum(1 for row in records if row.get("error") == "prefilter_miss")
        summary["dense_ranker_miss_count"] = sum(1 for row in records if row.get("error") == "dense_ranker_miss")
        summary["source_fingerprint_error_count"] = sum(
            1 for row in records if row.get("error") == "source_fingerprint_error"
        )
    confirmed_count = max(1, int(summary["confirmed_count"]))
    summary["recall_at_1"] = round(int(summary["top_1_count"]) / confirmed_count, 4)
    summary["recall_at_5"] = round(int(summary["top_5_count"]) / confirmed_count, 4)
    summary["recall_at_n"] = round(int(summary["top_n_count"]) / confirmed_count, 4)
    json_path.write_text(json.dumps({"summary": summary, "records": records}, indent=2, sort_keys=True), encoding="utf-8")
    latest_path = effective_report_dir / "broad_visual_benchmark_latest.json"
    latest_path.write_text(json.dumps({"summary": summary, "records": records}, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def broad_status(db_path: Path, *, include_prefilter_stale_count: bool = True) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "exists": False,
            "path": str(db_path),
            "descriptor_count": 0,
            "descriptor_error_count": 0,
            "result_count": 0,
            "latest_run": {},
            "latest_no_date_run": {},
            "latest_index_run": {},
            "rough_prefilter": {},
            "latest_index_errors": [],
        }
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        return {
            "exists": True,
            "path": str(db_path),
            "busy": True,
            "error": str(exc),
            "descriptor_count": 0,
            "descriptor_error_count": 0,
            "result_count": 0,
            "latest_run": {},
            "latest_no_date_run": {},
            "latest_index_run": {},
            "rough_prefilter": {},
            "latest_index_errors": [],
        }
    connection.row_factory = sqlite3.Row
    try:
        try:
            descriptor_count = int(
                connection.execute("SELECT COUNT(*) FROM broad_descriptors WHERE error = ''").fetchone()[0]
            )
            descriptor_error_count = int(
                connection.execute("SELECT COUNT(*) FROM broad_descriptors WHERE error != ''").fetchone()[0]
            )
            result_count = int(connection.execute("SELECT COUNT(*) FROM broad_match_results").fetchone()[0])
            latest_run = _single_dict(
                connection.execute(
                    "SELECT * FROM broad_match_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            )
            latest_no_date_run = _single_dict(
                connection.execute(
                    """
                    SELECT *
                    FROM broad_match_runs
                    WHERE candidate_scope_json LIKE '%rough_prefilter_no_date%'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
            )
            latest_index_run = _compact_index_run(
                _single_dict(
                    connection.execute(
                        "SELECT * FROM broad_descriptor_runs ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                )
            )
            prefilter_status = _rough_prefilter_status(
                connection,
                include_stale_count=include_prefilter_stale_count,
            )
        except sqlite3.Error as exc:
            return {
                "exists": True,
                "path": str(db_path),
                "busy": True,
                "error": str(exc),
                "descriptor_count": 0,
                "descriptor_error_count": 0,
                "result_count": 0,
                "latest_run": {},
                "latest_no_date_run": {},
                "latest_index_run": {},
                "rough_prefilter": {},
                "latest_index_errors": [],
            }
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
            "latest_no_date_run": _with_prefilter_metrics(latest_no_date_run),
            "latest_index_run": latest_index_run,
            "rough_prefilter": prefilter_status,
            "latest_index_errors": latest_index_errors,
        }
    finally:
        connection.close()


def rough_visual_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "exists": False,
            "path": str(db_path),
            "descriptor_count": 0,
            "descriptor_error_count": 0,
            "result_count": 0,
            "latest_run": {},
            "latest_no_date_run": {},
            "latest_index_run": {},
            "rough_prefilter": {},
            "latest_index_errors": [],
        }
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error as exc:
        return {
            "exists": True,
            "path": str(db_path),
            "busy": True,
            "error": str(exc),
            "descriptor_count": 0,
            "descriptor_error_count": 0,
            "result_count": 0,
            "latest_run": {},
            "latest_no_date_run": {},
            "latest_index_run": {},
            "rough_prefilter": {},
            "latest_index_errors": [],
        }
    connection.row_factory = sqlite3.Row
    try:
        try:
            prefilter_status = _rough_prefilter_status(connection, include_stale_count=False)
            latest_no_date_run = _single_dict(
                connection.execute(
                    """
                    SELECT *
                    FROM broad_match_runs
                    WHERE candidate_scope_json LIKE '%rough_prefilter_no_date%'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
            )
        except sqlite3.Error as exc:
            return {
                "exists": True,
                "path": str(db_path),
                "busy": True,
                "error": str(exc),
                "descriptor_count": 0,
                "descriptor_error_count": 0,
                "result_count": 0,
                "latest_run": {},
                "latest_no_date_run": {},
                "latest_index_run": {},
                "rough_prefilter": {},
                "latest_index_errors": [],
            }
        return {
            "exists": True,
            "path": str(db_path),
            "descriptor_count": int(prefilter_status.get("descriptor_count", 0)),
            "descriptor_error_count": 0,
            "result_count": 0,
            "latest_run": {},
            "latest_no_date_run": _with_prefilter_metrics(latest_no_date_run),
            "latest_index_run": {},
            "rough_prefilter": prefilter_status,
            "latest_index_errors": [],
        }
    finally:
        connection.close()


def _rough_prefilter_status(
    connection: sqlite3.Connection,
    *,
    include_stale_count: bool = True,
) -> dict[str, Any]:
    source_method_version = _descriptor_method_version(DEFAULT_THUMBNAIL_SIZE)
    source_density = DEFAULT_DENSITY
    source_thumbnail_size = DEFAULT_THUMBNAIL_SIZE
    latest_run = _single_dict(
        connection.execute(
            "SELECT * FROM rough_prefilter_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    )
    if not include_stale_count:
        feature_count = int(latest_run.get("reused_feature_count", 0) or 0) + int(
            latest_run.get("indexed_feature_count", 0) or 0
        )
        descriptor_count = int(latest_run.get("total_descriptor_count", 0) or feature_count)
        return {
            "method_version": ROUGH_PREFILTER_METHOD_VERSION,
            "source_method_version": source_method_version,
            "source_density": source_density,
            "source_thumbnail_size": source_thumbnail_size,
            "descriptor_count": descriptor_count,
            "feature_count": feature_count,
            "stale_count": max(descriptor_count - feature_count, 0),
            "stale_count_exact": False,
            "error_count": int(latest_run.get("error_count", 0) or 0),
            "band_count": 0,
            "band_count_exact": False,
            "ready": feature_count > 0,
            "latest_run": latest_run,
            "latest_errors": [],
            "method_counts": [],
        }
    if include_stale_count:
        descriptor_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM broad_descriptors
                WHERE error = '' AND method_version = ? AND density = ?
                """,
                (source_method_version, source_density),
            ).fetchone()[0]
        )
    else:
        descriptor_count = int(
            connection.execute("SELECT COUNT(*) FROM broad_descriptors WHERE error = ''").fetchone()[0]
        )
    feature_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM rough_prefilter_features
            WHERE error = ''
                AND prefilter_method_version = ?
                AND source_method_version = ?
                AND source_density = ?
                AND source_thumbnail_size = ?
            """,
            (
                ROUGH_PREFILTER_METHOD_VERSION,
                source_method_version,
                source_density,
                source_thumbnail_size,
            ),
        ).fetchone()[0]
    )
    stale_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM broad_descriptors AS descriptors
            LEFT JOIN rough_prefilter_features AS features
                ON features.path = descriptors.path
                AND features.byte_size = descriptors.byte_size
                AND features.mtime_ns = descriptors.mtime_ns
                AND features.sha256 = descriptors.sha256
                AND features.error = ''
                AND features.prefilter_method_version = ?
                AND features.source_method_version = descriptors.method_version
                AND features.source_density = descriptors.density
                AND features.source_thumbnail_size = ?
            WHERE descriptors.error = ''
                AND descriptors.method_version = ?
                AND descriptors.density = ?
                AND features.path IS NULL
            """,
            (
                ROUGH_PREFILTER_METHOD_VERSION,
                source_thumbnail_size,
                source_method_version,
                source_density,
            ),
        ).fetchone()[0]
    )
    error_count = int(connection.execute("SELECT COUNT(*) FROM rough_prefilter_features WHERE error != ''").fetchone()[0])
    band_count = int(connection.execute("SELECT COUNT(*) FROM rough_prefilter_bands").fetchone()[0])
    latest_errors = [
        _single_dict(row)
        for row in connection.execute(
            """
            SELECT candidate_index, path, filename, phase, error, created_at
            FROM rough_prefilter_errors
            ORDER BY created_at DESC, error_id DESC
            LIMIT 20
            """
        ).fetchall()
    ]
    method_counts = [
        _single_dict(row)
        for row in connection.execute(
            """
            SELECT prefilter_method_version, source_method_version, source_density,
                source_thumbnail_size, COUNT(*) AS feature_count
            FROM rough_prefilter_features
            WHERE error = ''
            GROUP BY prefilter_method_version, source_method_version, source_density, source_thumbnail_size
            ORDER BY feature_count DESC
            """
        ).fetchall()
    ]
    return {
        "method_version": ROUGH_PREFILTER_METHOD_VERSION,
        "source_method_version": source_method_version,
        "source_density": source_density,
        "source_thumbnail_size": source_thumbnail_size,
        "descriptor_count": descriptor_count,
        "feature_count": feature_count,
        "stale_count": stale_count,
        "stale_count_exact": True,
        "error_count": error_count,
        "band_count": band_count,
        "band_count_exact": True,
        "ready": feature_count > 0,
        "latest_run": latest_run,
        "latest_errors": latest_errors,
        "method_counts": method_counts,
    }


def _with_prefilter_metrics(run: dict[str, Any]) -> dict[str, Any]:
    if not run:
        return {}
    result = dict(run)
    try:
        result["prefilter_metrics"] = json.loads(str(result.get("prefilter_metrics_json") or "{}"))
    except json.JSONDecodeError:
        result["prefilter_metrics"] = {}
    return result


def monthly_fingerprint_coverage(
    canonical_root: Path,
    db_path: Path,
    target_scope: dict[str, Any] | None = None,
    months: set[str] | None = None,
    method_version: str | None = None,
    include_low_quality: bool = False,
) -> list[dict[str, Any]]:
    photo_counts = _photo_index_month_counts(
        canonical_root / "photo_library_index.sqlite",
        include_low_quality=include_low_quality,
    )
    descriptor_counts = _descriptor_month_counts(
        db_path,
        canonical_root / "photo_library_index.sqlite",
        method_version or _descriptor_method_version(DEFAULT_THUMBNAIL_SIZE),
        include_low_quality=include_low_quality,
    )
    target_counts = _unresolved_target_month_counts(canonical_root / "canonical.db", target_scope or {})
    selected_months = months or _project365_entry_months(canonical_root / "canonical.db")
    if not selected_months:
        selected_months = set(target_counts)
    rows = []
    for month in sorted(selected_months):
        if len(month) != 7 or month[4] != "-":
            continue
        photo_count = int(photo_counts.get(month, 0))
        fingerprint_count = int(descriptor_counts.get(month, 0))
        coverage_ratio = (fingerprint_count / photo_count) if photo_count else 0.0
        rows.append(
            {
                "month": month,
                "photo_index_count": photo_count,
                "fingerprint_count": fingerprint_count,
                "coverage_ratio": round(coverage_ratio, 4),
                "missing_original_targets": int(target_counts.get(month, 0)),
            }
        )
    return rows


def _fingerprint_coverage_warning_for_match(
    canonical_root: Path,
    db_path: Path,
    targets: list[dict[str, str]],
    date_window_days: int,
    method_version: str,
) -> str:
    if not targets or date_window_days <= 0:
        return ""
    months: set[str] = set()
    window = dt.timedelta(days=date_window_days)
    for target in targets:
        try:
            entry_date = dt.date.fromisoformat(str(target["entry_date"]))
        except (KeyError, ValueError):
            continue
        months.update(_months_between(entry_date - window, entry_date + window))
    coverage = monthly_fingerprint_coverage(
        canonical_root,
        db_path,
        target_scope={"entry_ids": [str(target["entry_id"]) for target in targets]},
        months=months,
        method_version=method_version,
    )
    low = [
        row
        for row in coverage
        if int(row["photo_index_count"]) >= DEFAULT_MIN_COVERAGE_PHOTO_COUNT
        and float(row["coverage_ratio"]) < DEFAULT_MIN_FINGERPRINT_COVERAGE
    ]
    if not low:
        return ""
    sample = "; ".join(
        f"{row['month']} {row['fingerprint_count']}/{row['photo_index_count']} "
        f"({float(row['coverage_ratio']) * 100:.1f}%)"
        for row in low[:4]
    )
    extra = "" if len(low) <= 4 else f"; +{len(low) - 4} more"
    return (
        "Fingerprint coverage too low for candidate months: "
        f"{sample}{extra}. Search will continue; rebuild Broad Visual fingerprints for better recall."
    )


def _months_between(start: dt.date, end: dt.date) -> list[str]:
    months = []
    current = dt.date(start.year, start.month, 1)
    last = dt.date(end.year, end.month, 1)
    while current <= last:
        months.append(current.isoformat()[:7])
        if current.month == 12:
            current = dt.date(current.year + 1, 1, 1)
        else:
            current = dt.date(current.year, current.month + 1, 1)
    return months


def _photo_index_month_counts(index_db: Path, include_low_quality: bool = False) -> dict[str, int]:
    if not index_db.exists():
        return {}
    connection = sqlite3.connect(index_db)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "photo_library_dates" in tables:
            file_columns = (
                {str(row[1]) for row in connection.execute("PRAGMA table_info(photo_library_files)")}
                if "photo_library_files" in tables
                else set()
            )
            if include_low_quality or "quality_score" not in file_columns:
                rows = connection.execute(
                    """
                    SELECT substr(date, 1, 7) AS month, COUNT(DISTINCT file_path)
                    FROM photo_library_dates
                    WHERE date >= '0000-00-00' AND date < '9999-99-99'
                    GROUP BY month
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT substr(dates.date, 1, 7) AS month, COUNT(DISTINCT dates.file_path)
                    FROM photo_library_dates AS dates
                    JOIN photo_library_files AS files
                        ON files.path = dates.file_path
                    WHERE dates.date >= '0000-00-00' AND dates.date < '9999-99-99'
                        AND files.quality_score >= 0
                    GROUP BY month
                    """
                ).fetchall()
        else:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(photo_library_files)")}
            date_columns = [
                column
                for column in ("media_creation_dates", "filename_dates", "filesystem_dates", "capture_timestamp")
                if column in columns
            ]
            if not date_columns:
                return {}
            expression = "COALESCE(" + ", ".join(
                f"NULLIF(substr({column}, 1, 10), '')" for column in date_columns
            ) + ")"
            rows = connection.execute(
                f"""
                SELECT substr({expression}, 1, 7) AS month, COUNT(*)
                FROM photo_library_files
                WHERE {expression} IS NOT NULL
                GROUP BY month
                """
            ).fetchall()
    finally:
        connection.close()
    return {str(month): int(count) for month, count in rows if str(month or "").strip()}


def _project365_entry_months(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT substr(entry_date, 1, 7) AS month
            FROM entries
            WHERE length(entry_date) >= 7
            """
        ).fetchall()
    finally:
        connection.close()
    return {str(month) for (month,) in rows if str(month or "").strip()}


def _descriptor_month_counts(
    db_path: Path,
    index_db: Path,
    method_version: str,
    include_low_quality: bool = False,
) -> dict[str, int]:
    if not db_path.exists():
        return {}
    if index_db.exists():
        try:
            connection = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True, timeout=5)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                if "photo_library_dates" in tables and "photo_library_files" in tables:
                    connection.execute("ATTACH DATABASE ? AS broad", (str(db_path),))
                    file_columns = {
                        str(row[1]) for row in connection.execute("PRAGMA table_info(photo_library_files)")
                    }
                    clauses = [
                        "dates.date >= '0000-00-00'",
                        "dates.date < '9999-99-99'",
                        "descriptors.error = ''",
                        "descriptors.method_version = ?",
                    ]
                    params: list[Any] = [method_version]
                    if not include_low_quality:
                        clauses.append("descriptors.quality_score >= 0")
                        if "quality_score" in file_columns:
                            clauses.append("files.quality_score >= 0")
                    rows = connection.execute(
                        f"""
                        SELECT substr(dates.date, 1, 7) AS month, COUNT(DISTINCT dates.file_path)
                        FROM photo_library_dates AS dates
                        JOIN photo_library_files AS files
                            ON files.path = dates.file_path
                        JOIN broad.broad_descriptors AS descriptors
                            ON descriptors.path = dates.file_path
                        WHERE {" AND ".join(clauses)}
                        GROUP BY month
                        """,
                        params,
                    ).fetchall()
                    return {str(month): int(count) for month, count in rows if str(month or "").strip()}
                if "photo_library_files" in tables:
                    connection.execute("ATTACH DATABASE ? AS broad", (str(db_path),))
                    file_columns = {
                        str(row[1]) for row in connection.execute("PRAGMA table_info(photo_library_files)")
                    }
                    date_columns = [
                        column
                        for column in ("media_creation_dates", "filename_dates", "filesystem_dates", "capture_timestamp")
                        if column in file_columns
                    ]
                    if date_columns:
                        expression = "COALESCE(" + ", ".join(
                            f"NULLIF(substr(files.{column}, 1, 10), '')" for column in date_columns
                        ) + ")"
                        clauses = [
                            f"{expression} IS NOT NULL",
                            "descriptors.error = ''",
                            "descriptors.method_version = ?",
                        ]
                        params = [method_version]
                        if not include_low_quality:
                            clauses.append("descriptors.quality_score >= 0")
                            if "quality_score" in file_columns:
                                clauses.append("files.quality_score >= 0")
                        rows = connection.execute(
                            f"""
                            SELECT substr({expression}, 1, 7) AS month, COUNT(*)
                            FROM photo_library_files AS files
                            JOIN broad.broad_descriptors AS descriptors
                                ON descriptors.path = files.path
                            WHERE {" AND ".join(clauses)}
                            GROUP BY month
                            """,
                            params,
                        ).fetchall()
                        return {str(month): int(count) for month, count in rows if str(month or "").strip()}
            finally:
                connection.close()
        except sqlite3.Error:
            pass
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    cursor: sqlite3.Cursor | None = None
    try:
        cursor = connection.execute(
            """
            SELECT path, filename_dates, media_creation_dates, filesystem_dates
            FROM broad_descriptors
            WHERE error = '' AND method_version = ?
            """,
            (method_version,),
        )
        rows = cursor.fetchall()
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()
    month_paths: dict[str, set[str]] = {}
    for path, filename_dates, media_creation_dates, filesystem_dates in rows:
        path_text = str(path)
        months = {
            value[:7]
            for field in (filename_dates, media_creation_dates, filesystem_dates)
            for value in _split_date_values(str(field or ""))
            if len(value) >= 7 and value[4] == "-"
        }
        for month in months:
            month_paths.setdefault(month, set()).add(path_text)
    return {month: len(paths) for month, paths in month_paths.items()}


def _unresolved_target_month_counts(db_path: Path, scope: dict[str, Any]) -> dict[str, int]:
    if not db_path.exists():
        return {}
    counts: dict[str, int] = {}
    for row in _target_rows(db_path, scope):
        month = str(row.get("entry_date", ""))[:7]
        if month:
            counts[month] = counts.get(month, 0) + 1
    return counts


def _compact_index_run(row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    compact = dict(row)
    compact["date_coverage_json"] = _compact_date_coverage_json(str(compact.get("date_coverage_json") or ""))
    return compact


def _compact_date_coverage_json(value: str) -> str:
    if not value:
        return "{}"
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return "{}"
    if not isinstance(payload, dict):
        return "{}"
    payload.pop("dates", None)
    return json.dumps(payload, sort_keys=True)


def review_results(
    db_path: Path,
    run_id: str = "",
    entry_id: str = "",
    limit: int = DEFAULT_RESULT_PAGE_SIZE,
    offset: int = 0,
    slot: str = CURRENT_BROAD_MATCH_SLOT,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    connection = connect_broad_db(db_path)
    try:
        effective_run_id = run_id or _current_match_run_id(connection, slot)
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


def review_runs(db_path: Path, limit: int = 25) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    connection = connect_broad_db(db_path)
    try:
        rows = connection.execute(
            """
            SELECT
                runs.run_id,
                runs.started_at,
                runs.finished_at,
                runs.status,
                runs.target_count,
                runs.processed_target_count,
                runs.scanned_count,
                runs.result_count AS recorded_result_count,
                COALESCE(results.result_count, 0) AS result_count,
                COALESCE(results.entry_count, 0) AS entry_count,
                COALESCE(decisions.decision_count, 0) AS decision_count
            FROM broad_match_runs AS runs
            LEFT JOIN (
                SELECT run_id, COUNT(*) AS result_count, COUNT(DISTINCT entry_id) AS entry_count
                FROM broad_match_results
                GROUP BY run_id
            ) AS results
                ON results.run_id = runs.run_id
            LEFT JOIN (
                SELECT run_id, COUNT(*) AS decision_count
                FROM broad_match_entry_decisions
                GROUP BY run_id
            ) AS decisions
                ON decisions.run_id = runs.run_id
            ORDER BY runs.started_at DESC, runs.run_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_single_dict(row) for row in rows]
    finally:
        connection.close()


def current_match_run_id(db_path: Path, slot: str = CURRENT_BROAD_MATCH_SLOT) -> str:
    if not db_path.exists():
        return ""
    connection = connect_broad_db(db_path)
    try:
        return _current_match_run_id(connection, slot)
    finally:
        connection.close()


def set_current_match_run(db_path: Path, slot: str, run_id: str) -> None:
    run_text = str(run_id or "").strip()
    if not run_text:
        raise ValueError("run_id is required")
    connection = connect_broad_db(db_path)
    try:
        _set_current_match_run(connection, slot, run_text)
        connection.commit()
    finally:
        connection.close()


def review_ready_summary(
    canonical_root: Path,
    db_path: Path,
    picker_queue_path: Path | None = None,
    search_limit: int = 25,
    slot: str = CURRENT_BROAD_MATCH_SLOT,
) -> dict[str, Any]:
    if search_limit <= 0:
        raise ValueError("search_limit must be positive")
    if not db_path.exists():
        return {}
    connection = connect_broad_db(db_path)
    try:
        run_id = _current_match_run_id(connection, slot)
        run = _single_dict(
            connection.execute(
                "SELECT * FROM broad_match_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        )
    finally:
        connection.close()
    if not run:
        return {}
    page = review_entries(
        canonical_root,
        db_path,
        run_id=run_id,
        limit=1,
        picker_queue_path=picker_queue_path,
    )
    entries = page.get("entries") or []
    visibility = _review_visibility_summary(
        canonical_root,
        db_path,
        run_id,
        picker_queue_path,
    )
    reviewable_count = int(page.get("total_count", visibility.get("review_ready_entry_count", 0)) or 0)
    run.update(visibility)
    run["review_ready_entry_count"] = reviewable_count if entries else 0
    run["preview_entry_id"] = str(entries[0].get("entry_id") or "") if entries else ""
    run["preview_entry_date"] = str(entries[0].get("entry_date") or "") if entries else ""
    run["review_filtered_entry_count"] = max(
        int(visibility.get("review_ready_entry_count", 0)) - int(run["review_ready_entry_count"]),
        0,
    )
    return run


def _review_visibility_summary(
    canonical_root: Path,
    db_path: Path,
    run_id: str,
    picker_queue_path: Path | None,
) -> dict[str, int]:
    connection = connect_broad_db(db_path)
    try:
        result_entry_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT entry_id FROM broad_match_results WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        }
        decision_entry_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT entry_id FROM broad_match_entry_decisions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        }
        confirmed_entry_ids: set[str] = set()
        canonical_db = canonical_root / "canonical.db"
        if canonical_db.exists() and result_entry_ids:
            _attach_canonical_db(connection, canonical_db)
            placeholders = ", ".join("?" for _ in result_entry_ids)
            confirmed_entry_ids = {
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT entry_id
                    FROM canonical_db.media_assets
                    WHERE entry_id IN ({placeholders})
                        AND role IN ('external_original_reference', 'external_original_fallback')
                        AND review_status = 'confirmed'
                    """,
                    sorted(result_entry_ids),
                ).fetchall()
            }
    finally:
        connection.close()
    queued_entry_ids = (
        result_entry_ids & _picker_queue_entry_ids(picker_queue_path)
        if picker_queue_path is not None
        else set()
    )
    unavailable_entry_ids = (decision_entry_ids | confirmed_entry_ids) & result_entry_ids
    return {
        "review_total_entry_count": len(result_entry_ids),
        "review_queued_entry_count": len(queued_entry_ids),
        "review_decision_entry_count": len(decision_entry_ids & result_entry_ids),
        "review_confirmed_entry_count": len(confirmed_entry_ids),
        "review_ready_entry_count": max(len(result_entry_ids) - len(unavailable_entry_ids), 0),
        "review_filtered_entry_count": 0,
    }


def clear_review_run(db_path: Path, run_id: str) -> dict[str, Any]:
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    connection = connect_broad_db(db_path)
    try:
        result_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM broad_match_results WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        decision_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM broad_match_entry_decisions WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        connection.execute("DELETE FROM broad_match_results WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM broad_match_entry_decisions WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM broad_current_runs WHERE run_id = ?", (run_id,))
        connection.execute(
            """
            UPDATE broad_match_runs
            SET result_count = 0, matched_entries = 0
            WHERE run_id = ?
            """,
            (run_id,),
        )
        connection.commit()
        return {"run_id": run_id, "cleared_results": result_count, "cleared_decisions": decision_count}
    finally:
        connection.close()


def clear_all_review_runs(db_path: Path) -> dict[str, Any]:
    connection = connect_broad_db(db_path)
    try:
        result_count = int(connection.execute("SELECT COUNT(*) FROM broad_match_results").fetchone()[0])
        decision_count = int(connection.execute("SELECT COUNT(*) FROM broad_match_entry_decisions").fetchone()[0])
        run_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM broad_match_runs WHERE result_count != 0 OR matched_entries != 0"
            ).fetchone()[0]
        )
        connection.execute("DELETE FROM broad_match_results")
        connection.execute("DELETE FROM broad_match_entry_decisions")
        connection.execute("DELETE FROM broad_current_runs")
        connection.execute("UPDATE broad_match_runs SET result_count = 0, matched_entries = 0")
        connection.commit()
        return {"cleared_runs": run_count, "cleared_results": result_count, "cleared_decisions": decision_count}
    finally:
        connection.close()


def review_entries(
    canonical_root: Path,
    db_path: Path,
    run_id: str = "",
    entry_id: str = "",
    limit: int = 1,
    offset: int = 0,
    after_date: str = "",
    before_date: str = "",
    picker_queue_path: Path | None = None,
    slot: str = CURRENT_BROAD_MATCH_SLOT,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    effective_run_id = run_id
    if not effective_run_id:
        ready = review_ready_summary(
            canonical_root,
            db_path,
            picker_queue_path=picker_queue_path,
            slot=slot,
        )
        effective_run_id = str(ready.get("run_id") or "")
    connection = connect_broad_db(db_path)
    try:
        if not effective_run_id:
            return {"run_id": "", "entries": [], "returned_count": 0, "has_more": False, "offset": offset, "limit": limit}
        candidate_scope = _match_run_candidate_scope(connection, effective_run_id)
        _attach_canonical_db(connection, canonical_root / "canonical.db")
        where = ["run_id = ?"]
        params: list[Any] = [effective_run_id]
        if entry_id:
            where.append("entry_id = ?")
            params.append(entry_id)
        if after_date:
            where.append("entry_date > ?")
            params.append(after_date)
        if before_date:
            where.append("entry_date < ?")
            params.append(before_date)
        order_direction = "DESC" if before_date and not after_date else "ASC"
        entry_filter_sql = f"""
            SELECT entry_id, MIN(entry_date) AS entry_date
            FROM broad_match_results
            WHERE {" AND ".join(where)}
                AND NOT EXISTS (
                    SELECT 1
                    FROM canonical_db.media_assets AS confirmed
                    WHERE confirmed.entry_id = broad_match_results.entry_id
                        AND confirmed.role IN ('external_original_reference', 'external_original_fallback')
                        AND confirmed.review_status = 'confirmed'
                )
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
        """
        entry_rows = connection.execute(
            f"""
            {entry_filter_sql}
            ORDER BY entry_date {order_direction}, entry_id {order_direction}
            """,
            tuple(params),
        ).fetchall()
        candidate_entries = [_single_dict(row) for row in entry_rows]
        if not candidate_entries:
            return {
                "run_id": effective_run_id,
                "entries": [],
                "returned_count": 0,
                "has_more": False,
                "offset": offset,
                "limit": limit,
                "total_count": 0,
            }
        entry_ids = [str(row["entry_id"]) for row in candidate_entries]
        result_rows = _results_for_entries(connection, effective_run_id, entry_ids)
    finally:
        connection.close()
    target_rows = _review_target_rows(canonical_root / "canonical.db", entry_ids)
    rejected_candidates = _rejected_candidates_by_entry(canonical_root / "canonical.db")
    pending_picker_decisions = _pending_picker_decisions(picker_queue_path)
    pending_completed_entry_ids = pending_picker_decisions["completed_entry_ids"]
    rejected_candidates = _merge_rejected_candidates(
        rejected_candidates,
        pending_picker_decisions["rejected_candidates"],
    )
    entries = []
    for entry in candidate_entries:
        entry_id_text = str(entry["entry_id"])
        if entry_id_text in pending_completed_entry_ids:
            continue
        target = target_rows.get(entry_id_text, {})
        results = _exclude_target_export_copies(target, result_rows.get(entry_id_text, []))
        results = _exclude_rejected_candidates({"entry_id": entry_id_text}, results, rejected_candidates)
        results = _filter_review_results_by_candidate_scope(
            results,
            str(entry.get("entry_date") or target.get("entry_date") or ""),
            candidate_scope,
        )
        if not results:
            continue
        entry_payload = (
            {
                "entry_id": entry_id_text,
                "entry_date": str(entry.get("entry_date") or target.get("entry_date") or ""),
                "source_path": str(target.get("source_path") or ""),
                "confirmed_path": str(target.get("confirmed_path") or ""),
                "results": results,
            }
        )
        entries.append(entry_payload)
    visible_entries = entries
    entries = visible_entries[offset : offset + limit]
    return {
        "run_id": effective_run_id,
        "entries": entries,
        "returned_count": len(entries),
        "has_more": offset + limit < len(visible_entries),
        "offset": offset,
        "limit": limit,
        "total_count": len(visible_entries),
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
        if _canonical_entry_has_completed_external_decision(canonical_root / "canonical.db", str(result["entry_id"])):
            raise ValueError("This entry already has a confirmed original-photo decision.")
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
            "entry_date": str(result.get("entry_date") or ""),
            "run_id": str(result["run_id"]),
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
    slot: str = CURRENT_BROAD_MATCH_SLOT,
) -> dict[str, Any]:
    entry_text = str(entry_id or "").strip()
    if not entry_text:
        raise ValueError("Choose an entry to reject.")
    connection = connect_broad_db(db_path)
    try:
        effective_run_id = run_id.strip() or _current_match_run_id(connection, slot)
        if not effective_run_id:
            raise ValueError("No broad visual search run is available.")
        if _canonical_entry_has_completed_external_decision(canonical_root / "canonical.db", entry_text):
            raise ValueError("This entry already has a confirmed original-photo decision.")
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
            "entry_date": str(rows[0].get("entry_date") or ""),
            "run_id": effective_run_id,
            "decision": "rejected_all",
            "rejected_count": len(rows),
        }
    finally:
        connection.close()


def keep_project365_export_for_broad_entry(
    canonical_root: Path,
    db_path: Path,
    run_id: str = "",
    entry_id: str = "",
    notes: str = "No external original selected; keep Project365 export as fallback.",
    slot: str = CURRENT_BROAD_MATCH_SLOT,
) -> dict[str, Any]:
    entry_text = str(entry_id or "").strip()
    if not entry_text:
        raise ValueError("Choose an entry to keep as the Project365 photo.")
    connection = connect_broad_db(db_path)
    try:
        effective_run_id = run_id.strip() or _current_match_run_id(connection, slot)
        if not effective_run_id:
            raise ValueError("No broad visual search run is available.")
        if _canonical_entry_has_completed_external_decision(canonical_root / "canonical.db", entry_text):
            raise ValueError("This entry already has a confirmed original-photo decision.")
        row = _single_dict(
            connection.execute(
                """
                SELECT *
                FROM broad_match_results
                WHERE run_id = ? AND entry_id = ?
                ORDER BY rank
                LIMIT 1
                """,
                (effective_run_id, entry_text),
            ).fetchone()
        )
        if not row:
            raise ValueError("No broad visual candidates were found for this entry.")
        _upsert_canonical_fallback_decision(
            canonical_root / "canonical.db",
            row,
            notes=notes,
        )
        _record_broad_entry_decision(
            connection,
            effective_run_id,
            entry_text,
            "keep_project365_export",
            None,
            "",
            notes,
        )
        connection.commit()
        return {
            "entry_id": entry_text,
            "entry_date": str(row.get("entry_date") or ""),
            "run_id": effective_run_id,
            "decision": "keep_project365_export",
        }
    finally:
        connection.close()


def undo_broad_entry_decision(
    canonical_root: Path,
    db_path: Path,
    run_id: str = "",
    entry_id: str = "",
    slot: str = CURRENT_BROAD_MATCH_SLOT,
) -> dict[str, Any]:
    entry_text = str(entry_id or "").strip()
    if not entry_text:
        raise ValueError("Choose an entry decision to undo.")
    connection = connect_broad_db(db_path)
    try:
        effective_run_id = run_id.strip() or _current_match_run_id(connection, slot)
        if not effective_run_id:
            raise ValueError("No broad visual search run is available.")
        decision = _single_dict(
            connection.execute(
                """
                SELECT *
                FROM broad_match_entry_decisions
                WHERE run_id = ? AND entry_id = ?
                """,
                (effective_run_id, entry_text),
            ).fetchone()
        )
        if not decision:
            raise ValueError("No broad visual decision was found for this entry.")
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
        deleted_media_count = _delete_canonical_broad_decision(
            canonical_root / "canonical.db",
            decision,
            rows,
        )
        connection.execute(
            "DELETE FROM broad_match_entry_decisions WHERE run_id = ? AND entry_id = ?",
            (effective_run_id, entry_text),
        )
        connection.commit()
        return {
            "entry_id": entry_text,
            "run_id": effective_run_id,
            "decision": str(decision["decision"]),
            "deleted_media_count": deleted_media_count,
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
        scored.append((
            score,
            _candidate_original_area(candidate),
            int(candidate.get("byte_size") or 0),
            str(candidate["path"]),
            view,
            candidate,
        ))
    scored.sort(key=lambda item: (round(item[0], VISUAL_SCORE_TIE_PRECISION), -item[1], -item[2], item[3]))
    best = min((item[0] for item in scored), default=0.0)
    results = []
    for rank, (score, _area, _byte_size, _path, view, candidate) in enumerate(scored[:max_results], start=1):
        result = dict(candidate)
        result["score"] = score
        result["score_gap"] = score - best
        result["rank"] = rank
        result["best_view"] = view
        results.append(result)
    return results


def _candidate_original_area(candidate: dict[str, Any]) -> int:
    width = int(candidate.get("original_width") or 0)
    height = int(candidate.get("original_height") or 0)
    return max(0, width) * max(0, height)


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
    start_bound, end_bound = _scope_date_bounds(start_date, end_date)
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
                    AND confirmed.role IN ('external_original_reference', 'external_original_fallback')
                    AND confirmed.review_status = 'confirmed'
            )
            """,
        ]
        params: list[Any] = []
        if entry_ids:
            placeholders = ", ".join("?" for _ in entry_ids)
            clauses.append(f"entries.id IN ({placeholders})")
            params.extend(sorted(entry_ids))
        if start_bound:
            clauses.append("entries.entry_date >= ?")
            params.append(start_bound)
        if end_bound:
            clauses.append("entries.entry_date <= ?")
            params.append(end_bound)
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
    start_bound, end_bound = _scope_date_bounds(start_date, end_date)
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
        if start_bound:
            clauses.append("entries.entry_date >= ?")
            params.append(start_bound)
        if end_bound:
            clauses.append("entries.entry_date <= ?")
            params.append(end_bound)
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
        targets: dict[str, dict[str, str]] = {}
        for chunk in _chunks(entry_ids, 500):
            placeholders = ", ".join("?" for _ in chunk)
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
                chunk,
            ).fetchall()
            targets.update({str(row["entry_id"]): _single_dict(row) for row in rows})
        return targets
    finally:
        connection.close()


def _results_for_entries(
    connection: sqlite3.Connection,
    run_id: str,
    entry_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not entry_ids:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {entry_id: [] for entry_id in entry_ids}
    for chunk in _chunks(entry_ids, 500):
        placeholders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT
                broad_match_results.*,
                COALESCE(descriptors.original_width, 0) AS candidate_width,
                COALESCE(descriptors.original_height, 0) AS candidate_height
            FROM broad_match_results
            LEFT JOIN broad_descriptors AS descriptors
                ON descriptors.path = broad_match_results.candidate_path
            WHERE broad_match_results.run_id = ?
                AND broad_match_results.entry_id IN ({placeholders})
            ORDER BY broad_match_results.entry_date, broad_match_results.entry_id, broad_match_results.rank, broad_match_results.candidate_path
            """,
            (run_id, *chunk),
        ).fetchall()
        for row in rows:
            result = _single_dict(row)
            grouped.setdefault(str(result["entry_id"]), []).append(result)
    return grouped


def _match_run_candidate_scope(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT candidate_scope_json FROM broad_match_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(str(row["candidate_scope_json"] or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _filter_review_results_by_candidate_scope(
    results: list[dict[str, Any]],
    entry_date: str,
    candidate_scope: dict[str, Any],
) -> list[dict[str, Any]]:
    if candidate_scope.get("candidate_scope") != "date_window_limited":
        return results
    try:
        target_date = dt.date.fromisoformat(entry_date)
    except ValueError:
        return results
    try:
        date_window_days = int(candidate_scope.get("date_window_days") or 0)
    except (TypeError, ValueError):
        return results
    if date_window_days <= 0:
        return results
    return [
        row
        for row in results
        if _row_within_date_window(row, target_date, date_window_days)
    ]


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
    if candidate_scope == "date_window_limited" and entry_date and date_window_days:
        target_date = dt.date.fromisoformat(entry_date)
        window = dt.timedelta(days=date_window_days)
        month_clauses = []
        for month in _months_between(target_date - window, target_date + window):
            month_clauses.append(
                "((instr(filename_dates, ?) > 0 OR instr(media_creation_dates, ?) > 0) "
                "OR (COALESCE(filename_dates, '') = '' AND COALESCE(media_creation_dates, '') = '' "
                "AND instr(filesystem_dates, ?) > 0))"
            )
            params.extend([month, month, month])
        if month_clauses:
            clauses.append("(" + " OR ".join(month_clauses) + ")")
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


def rough_prefilter_shortlist(
    connection: sqlite3.Connection,
    source_descriptor: dict[str, Any],
    source_method_version: str,
    density: int,
    thumbnail_size: int,
    shortlist_size: int = DEFAULT_PREFILTER_SHORTLIST_SIZE,
    per_band_hit_limit: int = DEFAULT_PREFILTER_BAND_HIT_LIMIT,
    include_low_quality: bool = False,
) -> RoughShortlist:
    if shortlist_size <= 0:
        raise ValueError("shortlist_size must be positive")
    if per_band_hit_limit <= 0:
        raise ValueError("per_band_hit_limit must be positive")
    started = time.monotonic()
    target_payload, target_bands = _rough_prefilter_payload(source_descriptor, source_method_version)
    target_views = {str(view["name"]): view for view in target_payload.get("views", [])}
    if not target_views:
        return RoughShortlist([], 0, 0, 0, 0, 0, True, "target_prefilter_empty")
    votes: dict[str, int] = {}
    hit_views: dict[str, set[str]] = {}
    rarity_votes: dict[str, float] = {}
    best_band_hit_count: dict[str, int] = {}
    capped_band_count = 0
    total_hits = 0
    for band_name, band_value in sorted(set(target_bands)):
        rows = _prefilter_band_hits(
            connection,
            band_name,
            band_value,
            source_method_version,
            density,
            thumbnail_size,
            include_low_quality,
            per_band_hit_limit + 1,
        )
        if len(rows) > per_band_hit_limit:
            capped_band_count += 1
            rows = rows[:per_band_hit_limit]
        total_hits += len(rows)
        band_hit_count = len(rows)
        band_weight = 1.0 / max(1, band_hit_count)
        for row in rows:
            path = str(row["path"])
            votes[path] = votes.get(path, 0) + 1
            rarity_votes[path] = rarity_votes.get(path, 0.0) + band_weight
            best_band_hit_count[path] = min(best_band_hit_count.get(path, band_hit_count), band_hit_count)
            hit_views.setdefault(path, set()).add(str(row["view_name"]))
    if not votes:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return RoughShortlist([], 0, capped_band_count, 0, len(set(target_bands)), elapsed_ms, True)
    score_candidate_limit = _rough_prefilter_score_candidate_limit(shortlist_size)
    candidate_paths = sorted(
        votes,
        key=lambda path: (
            -votes[path],
            -rarity_votes.get(path, 0.0),
            best_band_hit_count.get(path, sys.maxsize),
            path,
        ),
    )[:score_candidate_limit]
    feature_rows = _prefilter_feature_rows_by_paths(
        connection,
        candidate_paths,
        source_method_version,
        density,
        thumbnail_size,
        include_low_quality,
    )
    target_view = next(iter(target_views.values()))
    scored = []
    for row in feature_rows:
        feature_payload = json.loads(str(row["feature_json"]))
        distance, best_view = _rough_best_distance(target_view, feature_payload, hit_views.get(str(row["path"]), set()))
        vote_count = votes.get(str(row["path"]), 0)
        rough_score = distance - (vote_count * 25.0)
        scored.append((rough_score, distance, -vote_count, str(row["path"]), best_view, row))
    scored.sort(key=lambda item: (item[0], item[1], item[3]))
    candidates = []
    for rough_rank, (rough_score, distance, negative_votes, _path, best_view, row) in enumerate(
        scored[:shortlist_size],
        start=1,
    ):
        candidate = _single_dict(row)
        candidate["rough_rank"] = rough_rank
        candidate["rough_score"] = round(float(rough_score), 4)
        candidate["rough_distance"] = round(float(distance), 4)
        candidate["rough_votes"] = -negative_votes
        candidate["rough_best_view"] = best_view
        candidates.append(candidate)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return RoughShortlist(
        candidates=candidates,
        total_hit_count=total_hits,
        capped_band_count=capped_band_count,
        shortlist_size=len(candidates),
        band_query_count=len(set(target_bands)),
        elapsed_ms=elapsed_ms,
        low_confidence=bool(capped_band_count or len(votes) > score_candidate_limit or len(candidates) >= shortlist_size),
    )


def _rough_prefilter_score_candidate_limit(shortlist_size: int) -> int:
    return min(
        DEFAULT_PREFILTER_MAX_SCORE_CANDIDATES,
        max(shortlist_size, shortlist_size * DEFAULT_PREFILTER_SCORE_MULTIPLIER),
    )


def _prefilter_source_descriptor_rows(
    connection: sqlite3.Connection,
    source_method_version: str,
    density: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM broad_descriptors
        WHERE error = ''
            AND method_version = ?
            AND density = ?
            AND COALESCE(descriptor_json, '') NOT IN ('', '{}')
        ORDER BY path
        """,
        (source_method_version, int(density)),
    ).fetchall()
    return [_single_dict(row) for row in rows]


def _prefilter_feature_is_current(
    connection: sqlite3.Connection,
    descriptor_row: dict[str, Any],
    source_method_version: str,
    density: int,
    thumbnail_size: int,
) -> bool:
    row = connection.execute(
        """
        SELECT byte_size, mtime_ns, sha256, source_method_version, source_density,
            source_thumbnail_size, prefilter_method_version, feature_json, error
        FROM rough_prefilter_features
        WHERE path = ?
        """,
        (descriptor_row["path"],),
    ).fetchone()
    return bool(
        row
        and int(row["byte_size"]) == int(descriptor_row["byte_size"])
        and int(row["mtime_ns"]) == int(descriptor_row["mtime_ns"])
        and str(row["sha256"] or "") == str(descriptor_row.get("sha256") or "")
        and row["source_method_version"] == source_method_version
        and int(row["source_density"]) == int(density)
        and int(row["source_thumbnail_size"]) == int(thumbnail_size or 0)
        and row["prefilter_method_version"] == ROUGH_PREFILTER_METHOD_VERSION
        and str(row["feature_json"] or "") not in {"", "{}"}
        and str(row["error"] or "") == ""
    )


def _rough_prefilter_payload(
    descriptor: dict[str, Any],
    source_method_version: str,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    if str(descriptor.get("method") or "") != source_method_version:
        raise ValueError("descriptor_method_mismatch")
    width = int(descriptor.get("width") or 0)
    height = int(descriptor.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("descriptor_dimensions_missing")
    features = []
    bands: list[tuple[str, str]] = []
    for raw_view in descriptor.get("views", []):
        view = _rough_view_feature(raw_view, width, height)
        features.append(view)
        bands.extend(_rough_view_bands(view))
    if not features:
        raise ValueError("descriptor_views_missing")
    return (
        {
            "method": ROUGH_PREFILTER_METHOD_VERSION,
            "source_method": source_method_version,
            "width": width,
            "height": height,
            "orientation": _orientation_bucket(width, height),
            "views": features,
        },
        bands,
    )


def _rough_view_feature(raw_view: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    gray = [float(value) for value in raw_view.get("gray", [])]
    mean_rgb = [float(value) for value in raw_view.get("mean_rgb", [])]
    if not gray or len(mean_rgb) != 3:
        raise ValueError("descriptor_view_incomplete")
    side = int(math.sqrt(len(gray)))
    if side * side != len(gray):
        raise ValueError("descriptor_gray_not_square")
    coarse_gray = _coarse_gray_grid(gray, side, 4)
    return {
        "name": str(raw_view.get("name") or ""),
        "gray": [round(value, 3) for value in coarse_gray],
        "mean_rgb": [round(value, 3) for value in mean_rgb],
        "gray_bins": [int(value // 32) for value in coarse_gray],
        "color_bins": [int(value // 32) for value in mean_rgb],
        "orientation": _orientation_bucket(width, height),
        "aspect_bucket": int(round((width / max(1, height)) * 10)),
    }


def _coarse_gray_grid(gray: list[float], side: int, coarse_side: int) -> list[float]:
    values = []
    for coarse_y in range(coarse_side):
        y_start = round(coarse_y * side / coarse_side)
        y_end = round((coarse_y + 1) * side / coarse_side)
        for coarse_x in range(coarse_side):
            x_start = round(coarse_x * side / coarse_side)
            x_end = round((coarse_x + 1) * side / coarse_side)
            cells = [
                gray[y * side + x]
                for y in range(y_start, max(y_start + 1, y_end))
                for x in range(x_start, max(x_start + 1, x_end))
            ]
            values.append(sum(cells) / max(1, len(cells)))
    return values


def _rough_view_bands(view: dict[str, Any]) -> list[tuple[str, str]]:
    gray_bins = [str(value) for value in view["gray_bins"]]
    color_bins = [str(value) for value in view["color_bins"]]
    quadrants = [
        "-".join(gray_bins[0:2] + gray_bins[4:6]),
        "-".join(gray_bins[2:4] + gray_bins[6:8]),
        "-".join(gray_bins[8:10] + gray_bins[12:14]),
        "-".join(gray_bins[10:12] + gray_bins[14:16]),
    ]
    return [
        ("color", "-".join(color_bins)),
        ("gray_tl", quadrants[0]),
        ("gray_tr", quadrants[1]),
        ("gray_bl", quadrants[2]),
        ("gray_br", quadrants[3]),
        ("orientation", f"{view['orientation']}:{view['aspect_bucket']}"),
    ]


def _orientation_bucket(width: int, height: int) -> str:
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def _rough_best_distance(
    target_view: dict[str, Any],
    candidate_payload: dict[str, Any],
    preferred_views: set[str],
) -> tuple[float, str]:
    best: tuple[float, str] | None = None
    for view in candidate_payload.get("views", []):
        if preferred_views and str(view.get("name") or "") not in preferred_views:
            continue
        gray_score = _rough_mse(target_view["gray"], view["gray"])
        color_score = _rough_mse(target_view["mean_rgb"], view["mean_rgb"])
        score = gray_score + color_score * 0.15
        name = str(view.get("name") or "")
        if best is None or (score, name) < best:
            best = (score, name)
    if best is not None:
        return best
    for view in candidate_payload.get("views", []):
        gray_score = _rough_mse(target_view["gray"], view["gray"])
        color_score = _rough_mse(target_view["mean_rgb"], view["mean_rgb"])
        score = gray_score + color_score * 0.15
        name = str(view.get("name") or "")
        if best is None or (score, name) < best:
            best = (score, name)
    if best is None:
        return 999999.0, ""
    return best


def _rough_mse(left: list[Any], right: list[Any]) -> float:
    count = min(len(left), len(right))
    if count <= 0:
        return 999999.0
    total = 0.0
    for index in range(count):
        diff = float(left[index]) - float(right[index])
        total += diff * diff
    return total / count


def _upsert_prefilter_feature(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    source_method_version: str,
    density: int,
    thumbnail_size: int,
    feature_payload: dict[str, Any],
    band_rows: list[tuple[str, str]],
    error: str,
) -> None:
    view_count = len(feature_payload.get("views", [])) if feature_payload else 0
    width = int(feature_payload.get("width") or row.get("width") or 0) if feature_payload else int(row.get("width") or 0)
    height = int(feature_payload.get("height") or row.get("height") or 0) if feature_payload else int(row.get("height") or 0)
    connection.execute(
        """
        INSERT INTO rough_prefilter_features (
            path, root, filename, extension, byte_size, mtime_ns, sha256,
            width, height, view_count, source_method_version, source_density,
            source_thumbnail_size, prefilter_method_version, feature_json, error, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            root = excluded.root,
            filename = excluded.filename,
            extension = excluded.extension,
            byte_size = excluded.byte_size,
            mtime_ns = excluded.mtime_ns,
            sha256 = excluded.sha256,
            width = excluded.width,
            height = excluded.height,
            view_count = excluded.view_count,
            source_method_version = excluded.source_method_version,
            source_density = excluded.source_density,
            source_thumbnail_size = excluded.source_thumbnail_size,
            prefilter_method_version = excluded.prefilter_method_version,
            feature_json = excluded.feature_json,
            error = excluded.error,
            indexed_at = excluded.indexed_at
        """,
        (
            row["path"],
            row["root"],
            row["filename"],
            row["extension"],
            int(row["byte_size"]),
            int(row["mtime_ns"]),
            str(row.get("sha256") or ""),
            width,
            height,
            view_count,
            source_method_version,
            int(density),
            int(thumbnail_size or 0),
            ROUGH_PREFILTER_METHOD_VERSION,
            json.dumps(feature_payload, sort_keys=True),
            error,
            _now(),
        ),
    )
    connection.execute("DELETE FROM rough_prefilter_bands WHERE path = ?", (row["path"],))
    if not error:
        for view in feature_payload.get("views", []):
            view_name = str(view.get("name") or "")
            for band_name, band_value in _rough_view_bands(view):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO rough_prefilter_bands (path, view_name, band_name, band_value)
                    VALUES (?, ?, ?, ?)
                    """,
                    (row["path"], view_name, band_name, band_value),
                )


def _prefilter_band_hits(
    connection: sqlite3.Connection,
    band_name: str,
    band_value: str,
    source_method_version: str,
    density: int,
    thumbnail_size: int,
    include_low_quality: bool,
    limit: int,
) -> list[sqlite3.Row]:
    clauses = [
        "bands.band_name = ?",
        "bands.band_value = ?",
        "features.error = ''",
        "features.prefilter_method_version = ?",
        "features.source_method_version = ?",
        "features.source_density = ?",
        "features.source_thumbnail_size = ?",
        "descriptors.error = ''",
        "descriptors.method_version = ?",
        "descriptors.density = ?",
    ]
    params: list[Any] = [
        band_name,
        band_value,
        ROUGH_PREFILTER_METHOD_VERSION,
        source_method_version,
        int(density),
        int(thumbnail_size or 0),
        source_method_version,
        int(density),
    ]
    if not include_low_quality:
        clauses.append("descriptors.quality_score >= 0")
    return connection.execute(
        f"""
        SELECT bands.path, bands.view_name
        FROM rough_prefilter_bands AS bands
        JOIN rough_prefilter_features AS features ON features.path = bands.path
        JOIN broad_descriptors AS descriptors ON descriptors.path = bands.path
        WHERE {" AND ".join(clauses)}
        ORDER BY bands.path, bands.view_name
        LIMIT ?
        """,
        (*params, int(limit)),
    ).fetchall()


def _prefilter_feature_rows_by_paths(
    connection: sqlite3.Connection,
    paths: list[str],
    source_method_version: str,
    density: int,
    thumbnail_size: int,
    include_low_quality: bool,
) -> list[sqlite3.Row]:
    if not paths:
        return []
    rows: list[sqlite3.Row] = []
    for batch in _chunks(paths, 500):
        placeholders = ", ".join("?" for _ in batch)
        clauses = [
            f"features.path IN ({placeholders})",
            "features.error = ''",
            "features.prefilter_method_version = ?",
            "features.source_method_version = ?",
            "features.source_density = ?",
            "features.source_thumbnail_size = ?",
            "descriptors.error = ''",
            "descriptors.method_version = ?",
            "descriptors.density = ?",
        ]
        params: list[Any] = [
            *batch,
            ROUGH_PREFILTER_METHOD_VERSION,
            source_method_version,
            int(density),
            int(thumbnail_size or 0),
            source_method_version,
            int(density),
        ]
        if not include_low_quality:
            clauses.append("descriptors.quality_score >= 0")
        rows.extend(
            connection.execute(
                f"""
                SELECT
                    descriptors.path, descriptors.root, descriptors.filename, descriptors.extension,
                    descriptors.byte_size, descriptors.mtime_ns, descriptors.filesystem_mtime_utc,
                    descriptors.sha256, descriptors.width, descriptors.height,
                    descriptors.original_width, descriptors.original_height,
                    descriptors.filename_dates, descriptors.media_creation_dates,
                    descriptors.filesystem_dates, descriptors.quality_score, descriptors.quality_evidence,
                    descriptors.method_version, descriptors.density, features.feature_json
                FROM rough_prefilter_features AS features INDEXED BY sqlite_autoindex_rough_prefilter_features_1
                JOIN broad_descriptors AS descriptors INDEXED BY sqlite_autoindex_broad_descriptors_1
                    ON descriptors.path = features.path
                WHERE {" AND ".join(clauses)}
                ORDER BY descriptors.path
                """,
                params,
            ).fetchall()
        )
    return rows


def _descriptor_rows_by_paths(
    connection: sqlite3.Connection,
    paths: list[str],
    method_version: str,
    include_low_quality: bool,
) -> list[dict[str, Any]]:
    if not paths:
        return []
    rank = {path: index for index, path in enumerate(paths)}
    rows: list[dict[str, Any]] = []
    for batch in _chunks(paths, 500):
        placeholders = ", ".join("?" for _ in batch)
        clauses = [f"path IN ({placeholders})", "error = ''", "method_version = ?"]
        params: list[Any] = [*batch, method_version]
        if not include_low_quality:
            clauses.append("quality_score >= 0")
        rows.extend(
            _single_dict(row)
            for row in connection.execute(
                f"""
                SELECT *
                FROM broad_descriptors
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchall()
        )
    rows.sort(key=lambda row: rank.get(str(row["path"]), len(rank)))
    return rows


def _chunks(values: list[str], size: int) -> Any:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _ensure_prefilter_ready(
    connection: sqlite3.Connection,
    source_method_version: str,
    density: int,
    thumbnail_size: int,
) -> None:
    count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM rough_prefilter_features
            WHERE error = ''
                AND prefilter_method_version = ?
                AND source_method_version = ?
                AND source_density = ?
                AND source_thumbnail_size = ?
            """,
            (
                ROUGH_PREFILTER_METHOD_VERSION,
                source_method_version,
                int(density),
                int(thumbnail_size or 0),
            ),
        ).fetchone()[0]
    )
    if count <= 0:
        raise RuntimeError("No-date rough visual match requires a built rough prefilter index.")


def _record_prefilter_run(
    connection: sqlite3.Connection,
    run_id: str,
    started_at: str,
    finished_at: str,
    status: str,
    source_method_version: str,
    density: int,
    thumbnail_size: int,
    settings: dict[str, Any],
    total: int,
    scanned: int,
    reused: int,
    indexed: int,
    errors: int,
    current_path: str,
    phase: str,
    commit: bool = True,
) -> None:
    connection.execute(
        """
        INSERT INTO rough_prefilter_runs (
            run_id, started_at, finished_at, status, source_method_version, source_density,
            source_thumbnail_size, prefilter_method_version, settings_json,
            total_descriptor_count, scanned_count, reused_feature_count, indexed_feature_count,
            error_count, current_path, current_phase, heartbeat_at, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
        ON CONFLICT(run_id) DO UPDATE SET
            finished_at = excluded.finished_at,
            status = excluded.status,
            settings_json = excluded.settings_json,
            total_descriptor_count = excluded.total_descriptor_count,
            scanned_count = excluded.scanned_count,
            reused_feature_count = excluded.reused_feature_count,
            indexed_feature_count = excluded.indexed_feature_count,
            error_count = excluded.error_count,
            current_path = excluded.current_path,
            current_phase = excluded.current_phase,
            heartbeat_at = excluded.heartbeat_at
        """,
        (
            run_id,
            started_at,
            finished_at,
            status,
            source_method_version,
            int(density),
            int(thumbnail_size or 0),
            ROUGH_PREFILTER_METHOD_VERSION,
            json.dumps(settings, sort_keys=True),
            total,
            scanned,
            reused,
            indexed,
            errors,
            current_path,
            phase,
            _now(),
        ),
    )
    if commit:
        connection.commit()


def _record_prefilter_error(
    connection: sqlite3.Connection,
    run_id: str,
    candidate_index: int,
    row: dict[str, Any],
    phase: str,
    error: str,
) -> None:
    connection.execute(
        """
        INSERT INTO rough_prefilter_errors (
            run_id, candidate_index, path, filename, phase, error, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            candidate_index,
            str(row.get("path") or ""),
            str(row.get("filename") or Path(str(row.get("path") or "")).name),
            phase,
            error,
            _now(),
        ),
    )


def _record_prefilter_match_metrics(
    connection: sqlite3.Connection,
    run_id: str,
    entry_id: str,
    source_method_version: str,
    shortlist: RoughShortlist,
    descriptor_load_count: int,
    final_scored_count: int,
) -> None:
    connection.execute(
        """
        INSERT INTO broad_match_prefilter_metrics (
            run_id, entry_id, prefilter_method_version, source_method_version,
            shortlist_size, prefilter_hit_count, capped_band_count, band_query_count,
            descriptor_load_count, final_scored_count, elapsed_ms, low_confidence,
            error, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, entry_id) DO UPDATE SET
            shortlist_size = excluded.shortlist_size,
            prefilter_hit_count = excluded.prefilter_hit_count,
            capped_band_count = excluded.capped_band_count,
            band_query_count = excluded.band_query_count,
            descriptor_load_count = excluded.descriptor_load_count,
            final_scored_count = excluded.final_scored_count,
            elapsed_ms = excluded.elapsed_ms,
            low_confidence = excluded.low_confidence,
            error = excluded.error,
            created_at = excluded.created_at
        """,
        (
            run_id,
            entry_id,
            ROUGH_PREFILTER_METHOD_VERSION,
            source_method_version,
            shortlist.shortlist_size,
            shortlist.total_hit_count,
            shortlist.capped_band_count,
            shortlist.band_query_count,
            descriptor_load_count,
            final_scored_count,
            shortlist.elapsed_ms,
            1 if shortlist.low_confidence else 0,
            shortlist.error,
            _now(),
        ),
    )


def _update_match_prefilter_summary(connection: sqlite3.Connection, run_id: str) -> None:
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS target_count,
            COALESCE(SUM(shortlist_size), 0) AS shortlist_size,
            COALESCE(SUM(prefilter_hit_count), 0) AS prefilter_hit_count,
            COALESCE(SUM(capped_band_count), 0) AS capped_band_count,
            COALESCE(SUM(descriptor_load_count), 0) AS descriptor_load_count,
            COALESCE(SUM(final_scored_count), 0) AS final_scored_count,
            COALESCE(SUM(elapsed_ms), 0) AS elapsed_ms,
            COALESCE(SUM(low_confidence), 0) AS low_confidence_count
        FROM broad_match_prefilter_metrics
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    payload = _single_dict(row)
    connection.execute(
        "UPDATE broad_match_runs SET prefilter_metrics_json = ? WHERE run_id = ?",
        (json.dumps(payload, sort_keys=True), run_id),
    )


def _should_commit(scanned: int, commit_interval: int) -> bool:
    return scanned > 0 and commit_interval > 0 and scanned % commit_interval == 0


def _candidate_availability_roots(
    connection: sqlite3.Connection,
    candidate_scope: str,
    candidate_roots: list[Path],
    method_version: str,
) -> list[Path]:
    if candidate_scope in {"folder_limited", "same_setting_folder_limited"}:
        return candidate_roots
    rows = connection.execute(
        """
        SELECT DISTINCT root
        FROM broad_descriptors
        WHERE error = '' AND method_version = ?
        ORDER BY root
        """,
        (method_version,),
    ).fetchall()
    return [Path(str(row["root"])).expanduser() for row in rows if str(row["root"] or "").strip()]


def _ensure_candidate_roots_available(roots: list[Path]) -> None:
    missing = [str(root) for root in roots if not root.exists()]
    if missing:
        sample = "; ".join(missing[:3])
        extra = "" if len(missing) <= 3 else f"; +{len(missing) - 3} more"
        raise RuntimeError(f"Broad visual match aborted: candidate root unavailable: {sample}{extra}")


def _row_within_date_window(row: dict[str, Any], target_date: dt.date, window_days: int) -> bool:
    for value in _candidate_date_values(row):
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
    end = _optional_date(end_date, end_of_month=True)
    if start and end and start > end:
        raise ValueError("start_date must be on or before end_date")
    if not start and not end:
        return None
    window = dt.timedelta(days=window_days)
    return (
        start - window if start else None,
        end + window if end else None,
    )


def _optional_date(value: str, *, end_of_month: bool = False) -> dt.date | None:
    return _optional_date_bound(value, end_of_month=end_of_month)


def _optional_date_bound(value: str, *, end_of_month: bool) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 7 and text[4] == "-" and text[:4].isdigit() and text[5:7].isdigit():
        year = int(text[:4])
        month = int(text[5:7])
        if month < 1 or month > 12:
            raise ValueError(f"Invalid month date: {text}")
        if not end_of_month:
            return dt.date(year, month, 1)
        if month == 12:
            return dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
        return dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return dt.date.fromisoformat(text)


def _scope_date_bounds(start_date: str, end_date: str) -> tuple[str, str]:
    start = _optional_date_bound(start_date, end_of_month=False)
    end = _optional_date_bound(end_date, end_of_month=True)
    if start and end and start > end:
        raise ValueError("start_date must be on or before end_date")
    return (
        start.isoformat() if start else "",
        end.isoformat() if end else "",
    )


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


def _path_root_date(item: tuple[Any, ...]) -> tuple[Path, Path, str, str, dict[str, str]]:
    path = Path(item[0])
    root = Path(item[1])
    date_label = str(item[2]) if len(item) > 2 else ""
    indexed_sha256 = str(item[3]) if len(item) > 3 else ""
    photo_index_dates = item[4] if len(item) > 4 and isinstance(item[4], dict) else {}
    return path, root, date_label, indexed_sha256, photo_index_dates


def _split_date_values(value: str) -> set[str]:
    dates: set[str] = set()
    for chunk in str(value or "").replace(",", ";").split(";"):
        date_value = chunk.strip()
        if date_value:
            dates.add(date_value)
    return dates


def _initial_date_coverage(path_roots: list[tuple[Any, ...]]) -> dict[str, dict[str, int]]:
    coverage: dict[str, dict[str, int]] = {}
    for item in path_roots:
        _path, _root, date_label, _indexed_sha256, _photo_index_dates = _path_root_date(item)
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
) -> list[tuple[Path, Path, str, str, dict[str, str]]] | None:
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
                WITH selected AS (
                    SELECT f.path, f.root, MIN(d.date) AS first_date, MAX(f.sha256) AS sha256
                    FROM photo_library_files AS f
                    JOIN photo_library_dates AS d ON d.file_path = f.path
                    WHERE {" AND ".join(clauses)}
                    GROUP BY f.path, f.root
                )
                SELECT
                    selected.path,
                    selected.root,
                    selected.first_date,
                    selected.sha256,
                    GROUP_CONCAT(DISTINCT CASE WHEN all_dates.source = 'filename_date' THEN all_dates.date END)
                        AS filename_dates,
                    GROUP_CONCAT(DISTINCT CASE WHEN all_dates.source = 'media_creation_date' THEN all_dates.date END)
                        AS media_creation_dates,
                    GROUP_CONCAT(DISTINCT CASE WHEN all_dates.source = 'filesystem_date' THEN all_dates.date END)
                        AS filesystem_dates
                FROM selected
                LEFT JOIN photo_library_dates AS all_dates ON all_dates.file_path = selected.path
                GROUP BY selected.path, selected.root, selected.first_date, selected.sha256
                ORDER BY selected.first_date, selected.path
                """,
                params,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return [
        (
            Path(str(row["path"])),
            Path(str(row["root"])),
            str(row["first_date"]),
            str(row["sha256"] or ""),
            {
                "filename_dates": str(row["filename_dates"] or ""),
                "media_creation_dates": str(row["media_creation_dates"] or ""),
                "filesystem_dates": str(row["filesystem_dates"] or ""),
            },
        )
        for row in rows
    ]


def _descriptor_input_row(
    root: Path,
    path: Path,
    indexed_sha256: str = "",
    photo_index_dates: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    try:
        stat = path.stat()
        filename_dates = _filename_dates(path)
        media_dates = _jpeg_exif_dates(path)
        filesystem_dates = _filesystem_dates(stat)
        if photo_index_dates:
            filename_dates.update(_split_date_values(photo_index_dates.get("filename_dates", "")))
            media_dates.update(_split_date_values(photo_index_dates.get("media_creation_dates", "")))
            filesystem_dates.update(_split_date_values(photo_index_dates.get("filesystem_dates", "")))
        quality_score, quality_evidence = _quality_rank(path.name)
        original_width, original_height = _original_image_dimensions(path)
        return {
            "path": str(path.resolve()),
            "root": str(root),
            "filename": path.name,
            "extension": path.suffix.lower(),
            "byte_size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "filesystem_mtime_utc": _filesystem_mtime_utc(stat),
            "sha256": str(indexed_sha256 or ""),
            "original_width": original_width,
            "original_height": original_height,
            "filename_dates": ";".join(sorted(filename_dates)),
            "media_creation_dates": ";".join(sorted(media_dates)),
            "filesystem_dates": ";".join(sorted(filesystem_dates)),
            "quality_score": quality_score,
            "quality_evidence": ";".join(quality_evidence),
        }
    except OSError:
        return None


def _original_image_dimensions(path: Path) -> tuple[int, int]:
    dimensions = _parse_dimensions_text(_actual_image_dimensions_text(path))
    if dimensions is None:
        return (0, 0)
    return dimensions


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


def _refresh_descriptor_metadata(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    connection.execute(
        """
        UPDATE broad_descriptors
        SET
            root = ?,
            filename = ?,
            extension = ?,
            byte_size = ?,
            mtime_ns = ?,
            filesystem_mtime_utc = ?,
            sha256 = CASE WHEN ? != '' THEN ? ELSE sha256 END,
            filename_dates = ?,
            media_creation_dates = ?,
            filesystem_dates = ?,
            quality_score = ?,
            quality_evidence = ?,
            original_width = ?,
            original_height = ?
        WHERE path = ?
        """,
        (
            row["root"],
            row["filename"],
            row["extension"],
            row["byte_size"],
            row["mtime_ns"],
            row["filesystem_mtime_utc"],
            row["sha256"],
            row["sha256"],
            row["filename_dates"],
            row["media_creation_dates"],
            row["filesystem_dates"],
            row["quality_score"],
            row["quality_evidence"],
            row["original_width"],
            row["original_height"],
            row["path"],
        ),
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
            sha256, width, height, original_width, original_height,
            filename_dates, media_creation_dates, filesystem_dates,
            quality_score, quality_evidence, method_version, density, descriptor_json, error, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            original_width = excluded.original_width,
            original_height = excluded.original_height,
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
            row["original_width"],
            row["original_height"],
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
    evidence_prefix: str = "broad_visual_match",
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
                f"{evidence_prefix};run_id:{run_id}",
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


def _delete_canonical_broad_decision(
    canonical_db: Path,
    decision: dict[str, Any],
    result_rows: list[dict[str, Any]],
) -> int:
    entry_id = str(decision["entry_id"])
    decision_type = str(decision["decision"])
    if decision_type == "matched":
        roles = ["external_original_reference"]
        candidate_rows = [
            row
            for row in result_rows
            if int(row.get("result_id") or 0) == int(decision.get("result_id") or 0)
        ]
    elif decision_type == "rejected_all":
        roles = ["external_original_rejected"]
        candidate_rows = result_rows
    elif decision_type == "keep_project365_export":
        roles = ["external_original_fallback"]
        candidate_rows = []
    else:
        raise ValueError(f"Unsupported broad visual decision: {decision_type}")

    connection = sqlite3.connect(canonical_db)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(media_assets)")}
        if decision_type == "keep_project365_export":
            result = connection.execute(
                """
                DELETE FROM media_assets
                WHERE entry_id = ?
                    AND role = 'external_original_fallback'
                """,
                (entry_id,),
            )
            connection.commit()
            return max(0, result.rowcount)

        match_paths = sorted({str(row.get("candidate_path") or "").strip() for row in candidate_rows if str(row.get("candidate_path") or "").strip()})
        match_hashes = sorted({str(row.get("candidate_sha256") or "").strip() for row in candidate_rows if str(row.get("candidate_sha256") or "").strip()})
        match_clauses: list[str] = []
        params: list[Any] = [entry_id, *roles]
        if "storage_path" in columns and match_paths:
            match_clauses.append(f"storage_path IN ({', '.join('?' for _ in match_paths)})")
            params.extend(match_paths)
        if "sha256" in columns and match_hashes:
            match_clauses.append(f"sha256 IN ({', '.join('?' for _ in match_hashes)})")
            params.extend(match_hashes)
        if not match_clauses:
            return 0
        role_placeholders = ", ".join("?" for _ in roles)
        result = connection.execute(
            f"""
            DELETE FROM media_assets
            WHERE entry_id = ?
                AND role IN ({role_placeholders})
                AND ({" OR ".join(match_clauses)})
            """,
            params,
        )
        connection.commit()
        return max(0, result.rowcount)
    finally:
        connection.close()


def _canonical_entry_has_completed_external_decision(canonical_db: Path, entry_id: str) -> bool:
    if not canonical_db.exists():
        return False
    connection = sqlite3.connect(canonical_db)
    try:
        row = connection.execute(
            """
            SELECT 1
            FROM media_assets
            WHERE entry_id = ?
                AND role IN ('external_original_reference', 'external_original_fallback')
                AND review_status = 'confirmed'
            LIMIT 1
            """,
            (entry_id,),
        ).fetchone()
    finally:
        connection.close()
    return row is not None


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


def _upsert_canonical_fallback_decision(
    canonical_db: Path,
    result: dict[str, Any],
    notes: str,
) -> None:
    entry_id = str(result["entry_id"])
    decision_id = f"{entry_id}:external_original_fallback"
    now = _now()
    sha256 = hashlib.sha256(f"{entry_id}:external_original_fallback".encode("utf-8")).hexdigest()
    transformation = {
        "source": "external_original_fallback",
        "review_decision": "keep_project365_export",
        "review_notes": notes,
        "fallback_media_asset_id": str(result["project365_media_asset_id"]),
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
            "role": "external_original_fallback",
            "source_file_id": None,
            "internal_filename": "project365-export-fallback",
            "storage_path": "",
            "sha256": sha256,
            "byte_size": 0,
            "mime_type": "application/x-project365-fallback",
            "status": "fallback",
            "review_status": "confirmed",
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
        connection.execute(
            """
            DELETE FROM media_assets
            WHERE entry_id = ?
                AND role = 'external_original_reference'
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
    for value in _candidate_date_values(result):
        try:
            distances.append(abs((dt.date.fromisoformat(value[:10]) - target).days))
        except ValueError:
            pass
    return min(distances) if distances else -1


def _candidate_date_values(row: dict[str, Any]) -> list[str]:
    capture_values = _date_values_from_fields(row, ("filename_dates", "media_creation_dates"))
    if capture_values:
        return capture_values
    if _filename_has_date_token(row):
        return []
    return _date_values_from_fields(row, ("filesystem_dates",))


def _filename_has_date_token(row: dict[str, Any]) -> bool:
    filename = str(
        row.get("filename")
        or row.get("candidate_filename")
        or Path(str(row.get("path") or row.get("candidate_path") or "")).name
    )
    return any(pattern.search(filename) for pattern in FILENAME_DATE_PATTERNS)


def _date_values_from_fields(row: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for field in fields:
        for value in str(row.get(field, "") or "").split(";"):
            if value:
                values.append(value)
    return values


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
            candidate_scope_json, settings_json, target_count, heartbeat_at
        )
        VALUES (?, ?, '', 'running', 'starting', ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status = 'running',
            phase = 'resuming',
            target_count = excluded.target_count,
            settings_json = excluded.settings_json,
            heartbeat_at = excluded.heartbeat_at,
            error = ''
        """,
        (
            run_id,
            started_at,
            json.dumps(target_scope, sort_keys=True),
            json.dumps(candidate_scope, sort_keys=True),
            json.dumps(settings, sort_keys=True),
            target_count,
            _now(),
        ),
    )
    connection.commit()


def _record_match_progress(
    connection: sqlite3.Connection,
    run_id: str,
    phase: str,
    entry_id: str,
    target_index: int,
    target_count: int,
    current_candidate_count: int,
    processed_target_count: int,
    scanned: int,
    matched: int,
    result_count: int,
    errors: int,
) -> None:
    connection.execute(
        """
        UPDATE broad_match_runs
        SET phase = ?,
            current_entry_id = ?,
            current_target_index = ?,
            target_count = ?,
            current_candidate_count = ?,
            processed_target_count = ?,
            scanned_count = ?,
            matched_entries = ?,
            result_count = ?,
            error_count = ?,
            heartbeat_at = ?
        WHERE run_id = ?
        """,
        (
            phase,
            entry_id,
            target_index,
            target_count,
            current_candidate_count,
            processed_target_count,
            scanned,
            matched,
            result_count,
            errors,
            _now(),
            run_id,
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
    error: str = "",
    processed_target_count: int | None = None,
) -> None:
    connection.execute(
        """
        UPDATE broad_match_runs
        SET finished_at = ?, status = ?, phase = 'finished', scanned_count = ?,
            matched_entries = ?, result_count = ?, error_count = ?, current_entry_id = '',
            current_candidate_count = 0, processed_target_count = COALESCE(?, target_count),
            heartbeat_at = ?, error = ?
        WHERE run_id = ?
        """,
        (_now(), status, scanned, matched, result_count, errors, processed_target_count, _now(), error, run_id),
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


def _picker_queue_entry_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        stat = path.stat()
    except OSError:
        return set()
    cache_key = str(path)
    signature = (stat.st_size, stat.st_mtime_ns)
    cached = _PICKER_QUEUE_ENTRY_IDS_CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return set(cached[1])
    entry_ids: set[str] = set()
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            entry_id = str(row.get("entry_id", "")).strip()
            if entry_id and str(row.get("candidate_path", "")).strip():
                entry_ids.add(entry_id)
    _PICKER_QUEUE_ENTRY_IDS_CACHE[cache_key] = (signature, set(entry_ids))
    return entry_ids


def _pending_picker_decisions(queue_path: Path | None) -> dict[str, Any]:
    empty = {"completed_entry_ids": set(), "rejected_candidates": {}}
    if queue_path is None:
        return empty
    path = queue_path.with_name(f"{queue_path.stem}_picker_decisions.json")
    if not path.exists():
        return empty
    try:
        stat = path.stat()
    except OSError:
        return empty
    cache_key = str(path)
    signature = (stat.st_size, stat.st_mtime_ns)
    cached = _PICKER_PENDING_DECISIONS_CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return {
            "completed_entry_ids": set(cached[1]["completed_entry_ids"]),
            "rejected_candidates": _copy_rejected_candidates(cached[1]["rejected_candidates"]),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return empty
    completed_entry_ids: set[str] = set()
    rejected_candidates: dict[str, dict[str, set[str]]] = {}
    for entry_id, candidate_decisions in entries.items():
        entry_id_text = str(entry_id)
        if not isinstance(candidate_decisions, dict):
            continue
        for candidate_key, record in candidate_decisions.items():
            if not isinstance(record, dict):
                continue
            decision = str(record.get("review_decision", "")).strip().lower()
            if not decision:
                continue
            if decision in ACCEPT_DECISIONS or decision in FALLBACK_DECISIONS:
                completed_entry_ids.add(entry_id_text)
                continue
            if decision in REJECT_DECISIONS or decision in ASSOCIATED_PHOTO_DECISIONS:
                candidate_path = _candidate_path_from_picker_decision_key(str(candidate_key))
                if candidate_path:
                    _add_rejected_candidate_path(rejected_candidates, entry_id_text, candidate_path)
    value = {
        "completed_entry_ids": set(completed_entry_ids),
        "rejected_candidates": _copy_rejected_candidates(rejected_candidates),
    }
    _PICKER_PENDING_DECISIONS_CACHE[cache_key] = (signature, value)
    return {
        "completed_entry_ids": set(completed_entry_ids),
        "rejected_candidates": rejected_candidates,
    }


def _candidate_path_from_picker_decision_key(key: str) -> str:
    if key.startswith("candidate:"):
        return key[len("candidate:") :]
    return ""


def _add_rejected_candidate_path(
    rejected_candidates: dict[str, dict[str, set[str]]],
    entry_id: str,
    candidate_path: str,
) -> None:
    path_text = str(candidate_path or "").strip()
    if not path_text:
        return
    entry = rejected_candidates.setdefault(str(entry_id), {"sha256": set(), "paths": set(), "resolved_paths": set()})
    entry["paths"].add(path_text)
    entry["resolved_paths"].add(str(Path(path_text).resolve()))


def _merge_rejected_candidates(
    first: dict[str, dict[str, set[str]]],
    second: dict[str, dict[str, set[str]]],
) -> dict[str, dict[str, set[str]]]:
    merged = _copy_rejected_candidates(first)
    for entry_id, candidates in second.items():
        entry = merged.setdefault(str(entry_id), {"sha256": set(), "paths": set(), "resolved_paths": set()})
        for key in ("sha256", "paths", "resolved_paths"):
            entry[key].update(set(candidates.get(key, set())))
    return merged


def _copy_rejected_candidates(
    source: dict[str, dict[str, set[str]]],
) -> dict[str, dict[str, set[str]]]:
    return {
        str(entry_id): {
            "sha256": set(candidates.get("sha256", set())),
            "paths": set(candidates.get("paths", set())),
            "resolved_paths": set(candidates.get("resolved_paths", set())),
        }
        for entry_id, candidates in source.items()
    }


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


def _validate_current_match_slot(slot: str) -> str:
    slot_text = str(slot or "").strip()
    if slot_text not in CURRENT_MATCH_SLOTS:
        raise ValueError(f"Unsupported current match slot: {slot}")
    return slot_text


def _set_current_match_run(connection: sqlite3.Connection, slot: str, run_id: str) -> None:
    slot_text = _validate_current_match_slot(slot)
    run_text = str(run_id or "").strip()
    if not run_text:
        raise ValueError("run_id is required")
    connection.execute(
        """
        INSERT INTO broad_current_runs (slot, run_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(slot)
        DO UPDATE SET run_id = excluded.run_id,
            updated_at = excluded.updated_at
        """,
        (slot_text, run_text, _now()),
    )


def _current_match_run_id(connection: sqlite3.Connection, slot: str) -> str:
    slot_text = _validate_current_match_slot(slot)
    row = connection.execute(
        "SELECT run_id FROM broad_current_runs WHERE slot = ?",
        (slot_text,),
    ).fetchone()
    run_id = str(row["run_id"]) if row else ""
    if run_id and _run_matches_current_slot(connection, run_id, slot_text):
        return run_id
    fallback = _latest_match_run_id(connection, slot_text)
    if fallback:
        _set_current_match_run(connection, slot_text, fallback)
        connection.commit()
    return fallback


def _run_matches_current_slot(connection: sqlite3.Connection, run_id: str, slot: str) -> bool:
    row = connection.execute(
        "SELECT candidate_scope_json FROM broad_match_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if not row:
        return False
    is_rough = "rough_prefilter_no_date" in str(row["candidate_scope_json"] or "")
    return is_rough if slot == CURRENT_ROUGH_MATCH_SLOT else not is_rough


def _latest_match_run_id(connection: sqlite3.Connection, slot: str = "") -> str:
    slot_text = str(slot or "").strip()
    where = ""
    if slot_text == CURRENT_BROAD_MATCH_SLOT:
        where = "WHERE COALESCE(candidate_scope_json, '') NOT LIKE '%rough_prefilter_no_date%'"
    elif slot_text == CURRENT_ROUGH_MATCH_SLOT:
        where = "WHERE candidate_scope_json LIKE '%rough_prefilter_no_date%'"
    row = connection.execute(
        f"SELECT run_id FROM broad_match_runs {where} ORDER BY started_at DESC LIMIT 1"
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
