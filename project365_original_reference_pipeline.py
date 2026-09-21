#!/usr/bin/env python3
"""Find and record read-only external original-photo references."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

from project365_crop_align import suggest_crop
from project365_original_matcher import IMAGE_EXTENSIONS
import project365_photo_library_index as photo_index
from project365_photo_library_index import default_index_db, query_index_candidates
from project365_visual_ranker import visual_fieldnames


ACCEPT_DECISIONS = {"use_external_original", "selected", "confirmed"}
ASSOCIATED_PHOTO_DECISIONS = {"external_original_associated_photo", "associated_photo"}
REJECT_DECISIONS = {"rejected"}
FALLBACK_DECISIONS = {"keep_project365_export", "keep_fallback", "no_external_candidate", "fallback"}
CANDIDATE_FILTER_EXPORT_EQUIVALENT = "export_equivalent"
HIDDEN_CANDIDATE_FILTER_REASONS = {CANDIDATE_FILTER_EXPORT_EQUIVALENT}
ORIGINAL_PICKER_TARGET_ROLES = ("project365_export_png",)
REJECT_ALL_RANGE_STATE_FILENAME = "original_photo_reject_all_range_state.json"
AUTO_EXPAND_RANGE_DAYS = (0, 1, 3, 5, 15)
AUTO_EXPAND_MAX_RANGE_DAYS = 15
MANUAL_SEARCH_REQUIRED_MESSAGE = "+/- 15 had no candidates - do manual search"
LOW_QUALITY_ORIGINAL_MIN_AXIS_PX = 1000
FILENAME_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-_](0[1-9]|1[0-2])[-_](0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2}|19\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),
]
MAX_SCAN_WORKERS = min(8, max(2, (os.cpu_count() or 2)))


@dataclass(frozen=True)
class SearchSummary:
    search_queue_path: str
    group_report_path: str
    batch_plan_path: str
    attempt_log_path: str
    unclear_entry_count: int
    candidate_count: int


@dataclass(frozen=True)
class ApplySummary:
    selected_count: int
    rejected_count: int
    fallback_count: int = 0
    associated_count: int = 0
    skipped_unknown_media_count: int = 0

    @property
    def applied_count(self) -> int:
        return self.selected_count + self.rejected_count + self.fallback_count + self.associated_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search external folders for original photos and record reviewed path references."
    )
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument(
        "--search-root",
        action="append",
        default=[],
        help="Folder or mounted drive to search. May be repeated.",
    )
    parser.add_argument(
        "--review-queue",
        default="Project365Canonical/exports/verification_reports/original_photo_review_queue.csv",
        help="Existing original-photo review queue used to identify unclear entries.",
    )
    parser.add_argument(
        "--report-dir",
        default="Project365Canonical/exports/verification_reports",
    )
    parser.add_argument(
        "--skip-metadata-date-scan",
        action="store_true",
        help="Only use filename dates. By default JPEG EXIF and macOS metadata dates are also checked.",
    )
    parser.add_argument(
        "--entry-id",
        action="append",
        default=[],
        help="Only crawl candidates for this Project365 entry ID. May be repeated.",
    )
    parser.add_argument(
        "--entry-date",
        action="append",
        default=[],
        help="Only crawl candidates for this Project365 entry date, YYYY-MM-DD. May be repeated.",
    )
    parser.add_argument(
        "--start-date",
        help="Only crawl unmatched entries on or after this date, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        help="Only crawl unmatched entries on or before this date, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--max-targets",
        type=int,
        help="Only crawl the first N unmatched entries after other filters.",
    )
    parser.add_argument(
        "--photo-index",
        help="SQLite photo-library index to use as a fallback candidate source.",
    )
    parser.add_argument(
        "--disable-photo-index-fallback",
        action="store_true",
        help="Do not use the canonical photo-library index when folder search finds no candidates.",
    )
    parser.add_argument(
        "--indexed-fallback-limit",
        type=int,
        help="Optional maximum indexed candidates per entry date. By default all candidates in the first matching date tier are listed.",
    )
    parser.add_argument(
        "--photo-index-folder",
        help="Only use photo-index candidates inside this folder and its subfolders.",
    )
    parser.add_argument(
        "--merge-existing-queue",
        action="store_true",
        help="Merge targeted search results into the existing queue instead of replacing unrelated rows.",
    )
    parser.add_argument(
        "--replace-existing-queue",
        action="store_true",
        help="Clear existing search queue outputs before building the new queue.",
    )
    parser.add_argument(
        "--include-low-quality-matches",
        action="store_true",
        help="Also search entries whose confirmed external original is below 1000 px on either axis.",
    )
    parser.add_argument(
        "--apply-reviewed",
        help="Apply a reviewed external search queue CSV. Rows must set review_decision to use_external_original.",
    )
    parser.add_argument(
        "--mark-fallback",
        action="store_true",
        help="Mark filtered unresolved entries as reviewed fallback/no external original.",
    )
    parser.add_argument(
        "--prune-applied-reviewed",
        help="Remove already-applied reviewed rows from an external search queue CSV and refresh reports.",
    )
    parser.add_argument(
        "--score-queue-alignment",
        help="Add local crop-alignment scores to candidate rows in an external search queue CSV.",
    )
    parser.add_argument(
        "--alignment-sample-size",
        type=int,
        default=24,
        help="Sample size passed to the local crop-alignment scorer.",
    )
    parser.add_argument(
        "--rescore-alignment",
        action="store_true",
        help="Recompute alignment rows that already have an alignment score or error.",
    )
    parser.add_argument(
        "--score-max-candidates",
        type=int,
        help="Only score the first N candidate rows after entry/date filters.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=25,
        help="Print alignment-scoring progress every N candidate rows. Use 0 to disable.",
    )
    args = parser.parse_args()

    canonical_root = Path(args.canonical_root)
    if args.apply_reviewed:
        summary = apply_reviewed_external_references(
            canonical_root=canonical_root,
            reviewed_csv=Path(args.apply_reviewed),
        )
        print("Project365 external original references: PASS")
        print(f"Selected references: {summary.selected_count}")
        print(f"Rejected candidates: {summary.rejected_count}")
        print(f"Fallback decisions: {summary.fallback_count}")
        return 0
    if args.mark_fallback:
        summary = mark_fallback_external_references(
            canonical_root=canonical_root,
            review_queue_path=Path(args.review_queue),
            target_entry_ids=set(args.entry_id),
            target_entry_dates=set(args.entry_date),
            start_date=args.start_date,
            end_date=args.end_date,
            max_targets=args.max_targets,
        )
        print("Project365 external original fallbacks: PASS")
        print(f"Fallback decisions: {summary.fallback_count}")
        return 0
    if args.prune_applied_reviewed:
        summary = prune_applied_review_queue(
            canonical_root=canonical_root,
            queue_path=Path(args.prune_applied_reviewed),
            report_dir=Path(args.report_dir),
        )
        print("Project365 external original review queue prune: PASS")
        print(f"Remaining queue rows: {summary['queue_rows']}")
        print(f"Remaining entries: {summary['entry_count']}")
        print(f"Removed completed entries: {summary['removed_completed_entries']}")
        print(f"Removed rejected candidates: {summary['removed_rejected_candidates']}")
        return 0
    if args.score_queue_alignment:
        summary = score_review_queue_alignment(
            canonical_root=canonical_root,
            queue_path=Path(args.score_queue_alignment),
            sample_size=args.alignment_sample_size,
            rescore=args.rescore_alignment,
            target_entry_ids=set(args.entry_id),
            target_entry_dates=set(args.entry_date),
            max_candidates=args.score_max_candidates,
            progress_interval=args.progress_interval,
            progress_sink=_print_progress,
        )
        print("Project365 external original alignment scoring: PASS")
        print(f"Candidate rows: {summary['candidate_rows']}")
        print(f"Scored rows: {summary['scored_rows']}")
        print(f"Skipped rows: {summary['skipped_rows']}")
        print(f"Error rows: {summary['error_rows']}")
        print(f"Queue: {summary['queue_path']}")
        return 0

    summary = build_external_original_search_queue(
        canonical_root=canonical_root,
        search_roots=[Path(path) for path in args.search_root],
        review_queue_path=Path(args.review_queue),
        report_dir=Path(args.report_dir),
        scan_metadata_dates=not args.skip_metadata_date_scan,
        target_entry_ids=set(args.entry_id),
        target_entry_dates=set(args.entry_date),
        start_date=args.start_date,
        end_date=args.end_date,
        max_targets=args.max_targets,
        photo_index_path=Path(args.photo_index) if args.photo_index else None,
        use_photo_index_fallback=not args.disable_photo_index_fallback,
        indexed_fallback_limit=args.indexed_fallback_limit,
        photo_index_folder=Path(args.photo_index_folder) if args.photo_index_folder else None,
        merge_existing_queue=args.merge_existing_queue,
        replace_existing_queue=args.replace_existing_queue,
        include_low_quality_matches=args.include_low_quality_matches,
    )
    print("Project365 external original search: PASS")
    print(f"Search queue: {summary.search_queue_path}")
    print(f"Group report: {summary.group_report_path}")
    print(f"Batch plan: {summary.batch_plan_path}")
    print(f"Attempt log: {summary.attempt_log_path}")
    print(f"Unclear entries: {summary.unclear_entry_count}")
    print(f"Candidates: {summary.candidate_count}")
    return 0


def build_external_original_search_queue(
    canonical_root: Path,
    search_roots: list[Path],
    review_queue_path: Path,
    report_dir: Path,
    scan_metadata_dates: bool = True,
    target_entry_ids: set[str] | None = None,
    target_entry_dates: set[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_targets: int | None = None,
    merge_existing_queue: bool = False,
    photo_index_path: Path | None = None,
    use_photo_index_fallback: bool = True,
    indexed_fallback_limit: int | None = None,
    photo_index_folder: Path | None = None,
    replace_existing_queue: bool = False,
    include_low_quality_matches: bool = False,
) -> SearchSummary:
    if merge_existing_queue and replace_existing_queue:
        raise ValueError("Cannot merge and replace the existing search queue in the same run.")
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    for root in search_roots:
        if not root.exists():
            raise FileNotFoundError(f"Missing search root: {root}")
    _validate_target_filters(target_entry_dates, start_date, end_date, max_targets)

    report_dir.mkdir(parents=True, exist_ok=True)
    search_queue_path = report_dir / "original_photo_external_search_queue.csv"
    group_report_path = report_dir / "original_photo_unclear_groups.csv"
    batch_plan_path = report_dir / "original_photo_search_batch_plan.csv"
    attempt_log_path = report_dir / "original_photo_search_attempts.csv"
    if replace_existing_queue:
        _clear_existing_search_outputs(search_queue_path, group_report_path, batch_plan_path)
    known_candidate_filters = _load_candidate_filter_reasons(search_queue_path)
    reject_all_range_state = load_reject_all_range_state(report_dir)
    started_at = dt.datetime.now(dt.UTC)

    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        pending_crop_rejected_candidates = _load_pending_crop_rejected_candidates(search_queue_path)
        exports = _load_unclear_exports(
            connection,
            review_queue_path,
            include_low_quality_matches=include_low_quality_matches,
            pending_rejected_candidates=pending_crop_rejected_candidates,
        )
        rejected_candidates = _load_rejected_candidates(connection)
        pending_rejected_candidates = _load_pending_rejected_candidates(search_queue_path)
    finally:
        connection.close()
    rejected_candidates = _merge_rejected_candidates(rejected_candidates, pending_rejected_candidates)
    rejected_candidates = _merge_rejected_candidates(rejected_candidates, pending_crop_rejected_candidates)
    exports = _filter_target_exports(
        exports,
        target_entry_ids=target_entry_ids,
        target_entry_dates=target_entry_dates,
        start_date=start_date,
        end_date=end_date,
        max_targets=max_targets,
    )

    targets_by_date = {}
    for export in exports:
        targets_by_date.setdefault(export["entry_date"], []).append(export)

    target_dates = set(targets_by_date)
    candidates_by_date = _scan_search_roots(search_roots, target_dates, scan_metadata_dates)
    indexed_candidates_by_date = _indexed_fallback_candidates(
        canonical_root=canonical_root,
        photo_index_path=photo_index_path,
        target_dates=target_dates,
        use_photo_index_fallback=use_photo_index_fallback,
        indexed_fallback_limit=indexed_fallback_limit,
        photo_index_folder=photo_index_folder,
    )
    auto_expanded_candidates_by_entry = _auto_expanded_reject_all_candidates(
        canonical_root=canonical_root,
        photo_index_path=photo_index_path,
        exports=exports,
        use_photo_index_fallback=use_photo_index_fallback,
        reject_all_range_state=reject_all_range_state,
        indexed_fallback_limit=indexed_fallback_limit,
        photo_index_folder=photo_index_folder,
        rejected_candidates=rejected_candidates,
        known_candidate_filters=known_candidate_filters,
    )
    manual_search_required_entry_ids = _manual_search_required_entry_ids(exports, reject_all_range_state)
    queue_rows = []
    group_rows = []
    available_candidate_count = 0
    for entry_date in sorted(targets_by_date):
        date_exports = targets_by_date[entry_date]
        date_candidates = candidates_by_date.get(entry_date, [])
        indexed_date_candidates = indexed_candidates_by_date.get(entry_date, [])
        filtered_candidate_count = 0
        hidden_rejected_candidate_count = 0
        hidden_export_equivalent_candidate_count = 0
        export_candidate_rows: list[tuple[dict[str, object], list[dict[str, object]]]] = []
        for export in date_exports:
            export_candidates, hidden_count = _candidate_rows_for_export(
                export,
                date_candidates,
                indexed_date_candidates + auto_expanded_candidates_by_entry.get(str(export["entry_id"]), []),
                rejected_candidates,
                known_candidate_filters,
            )
            export_candidate_rows.append((export, export_candidates))
            filtered_candidate_count += _visible_candidate_count(export_candidates)
            hidden_rejected_candidate_count += hidden_count
            hidden_export_equivalent_candidate_count += _hidden_export_equivalent_candidate_count(export_candidates)
        available_candidate_count += filtered_candidate_count
        group_rows.append(
            {
                "entry_date": entry_date,
                "entry_count": len(date_exports),
                "candidate_count": filtered_candidate_count,
                "hidden_rejected_candidate_count": hidden_rejected_candidate_count,
                "hidden_export_equivalent_candidate_count": hidden_export_equivalent_candidate_count,
                "status": _group_status(
                    filtered_candidate_count,
                    hidden_rejected_candidate_count,
                    hidden_export_equivalent_candidate_count,
                ),
            }
        )
        for export, export_candidates in export_candidate_rows:
            if not export_candidates:
                if str(export["entry_id"]) in manual_search_required_entry_ids:
                    queue_rows.append(_manual_search_required_queue_row(export))
                else:
                    queue_rows.append(_empty_queue_row(export))
                continue
            for candidate in export_candidates:
                queue_rows.append(_queue_row(export, candidate))

    target_ids = {str(export["entry_id"]) for export in exports}
    if merge_existing_queue:
        queue_rows = _merge_queue_rows(search_queue_path, queue_rows, target_ids)
    else:
        queue_rows = _preserve_user_queue_rows(search_queue_path, queue_rows, target_ids)
    queue_rows = _suppress_duplicate_visible_candidate_rows(queue_rows, target_ids)
    group_rows = _group_rows_from_queue(queue_rows, rejected_candidates)
    available_candidate_count = sum(
        _visible_candidate_count([row])
        for row in queue_rows
        if str(row.get("entry_id", "")) in target_ids
    )
    batch_rows = _search_batch_plan_rows(group_rows, queue_rows)
    _write_csv(search_queue_path, queue_rows, _alignment_fieldnames(_search_queue_fieldnames()))
    _write_csv(group_report_path, group_rows, _group_fieldnames())
    _write_csv(batch_plan_path, batch_rows, _batch_plan_fieldnames())
    _append_search_attempt(
        attempt_log_path,
        started_at=started_at,
        search_roots=search_roots,
        target_entry_ids=target_entry_ids,
        target_entry_dates=target_entry_dates,
        start_date=start_date,
        end_date=end_date,
        max_targets=max_targets,
        scan_metadata_dates=scan_metadata_dates,
        merge_existing_queue=merge_existing_queue,
        replace_existing_queue=replace_existing_queue,
        include_low_quality_matches=include_low_quality_matches,
        unclear_entry_count=len(exports),
        candidate_count=available_candidate_count,
        hidden_rejected_candidate_count=sum(
            int(row.get("hidden_rejected_candidate_count", 0) or 0)
            for row in group_rows
        ),
        hidden_export_equivalent_candidate_count=sum(
            int(row.get("hidden_export_equivalent_candidate_count", 0) or 0)
            for row in group_rows
        ),
        search_queue_path=search_queue_path,
        group_report_path=group_report_path,
        batch_plan_path=batch_plan_path,
    )
    return SearchSummary(
        search_queue_path=str(search_queue_path),
        group_report_path=str(group_report_path),
        batch_plan_path=str(batch_plan_path),
        attempt_log_path=str(attempt_log_path),
        unclear_entry_count=len(exports),
        candidate_count=available_candidate_count,
    )


def _append_search_attempt(
    attempt_log_path: Path,
    *,
    started_at: dt.datetime,
    search_roots: list[Path],
    target_entry_ids: set[str] | None,
    target_entry_dates: set[str] | None,
    start_date: str | None,
    end_date: str | None,
    max_targets: int | None,
    scan_metadata_dates: bool,
    merge_existing_queue: bool,
    replace_existing_queue: bool,
    include_low_quality_matches: bool,
    unclear_entry_count: int,
    candidate_count: int,
    hidden_rejected_candidate_count: int,
    hidden_export_equivalent_candidate_count: int,
    search_queue_path: Path,
    group_report_path: Path,
    batch_plan_path: Path,
) -> None:
    finished_at = dt.datetime.now(dt.UTC)
    attempt_key = "|".join(
        [
            started_at.isoformat(),
            ";".join(str(root) for root in search_roots),
            ";".join(sorted(target_entry_ids or set())),
            ";".join(sorted(target_entry_dates or set())),
            str(start_date or ""),
            str(end_date or ""),
            str(max_targets or ""),
        ]
    )
    row = {
        "attempt_id": hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()[:16],
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "search_roots": ";".join(str(root) for root in search_roots),
        "target_entry_ids": ";".join(sorted(target_entry_ids or set())),
        "target_entry_dates": ";".join(sorted(target_entry_dates or set())),
        "start_date": start_date or "",
        "end_date": end_date or "",
        "max_targets": max_targets or "",
        "scan_metadata_dates": str(scan_metadata_dates).lower(),
        "merge_existing_queue": str(merge_existing_queue).lower(),
        "replace_existing_queue": str(replace_existing_queue).lower(),
        "include_low_quality_matches": str(include_low_quality_matches).lower(),
        "unclear_entry_count": unclear_entry_count,
        "candidate_count": candidate_count,
        "hidden_rejected_candidate_count": hidden_rejected_candidate_count,
        "hidden_export_equivalent_candidate_count": hidden_export_equivalent_candidate_count,
        "search_queue_path": str(search_queue_path),
        "group_report_path": str(group_report_path),
        "batch_plan_path": str(batch_plan_path),
    }
    _append_csv_row(attempt_log_path, row, _search_attempt_fieldnames())


def _clear_existing_search_outputs(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _merge_queue_rows(
    search_queue_path: Path,
    replacement_rows: list[dict[str, object]],
    target_entry_ids: set[str],
) -> list[dict[str, object]]:
    if not search_queue_path.exists() or not target_entry_ids:
        return replacement_rows
    existing_rows = _read_csv(search_queue_path)
    merged_rows: list[dict[str, object]] = [
        row for row in existing_rows if row.get("entry_id", "") not in target_entry_ids
    ]
    for entry_id in sorted(target_entry_ids):
        target_rows = [
            row
            for row in existing_rows
            if row.get("entry_id", "") == entry_id
        ] + [
            row
            for row in replacement_rows
            if row.get("entry_id", "") == entry_id
        ]
        candidate_rows = [row for row in target_rows if row.get("candidate_path", "")]
        if candidate_rows:
            merged_rows.extend(_dedupe_candidate_rows(candidate_rows))
        elif target_rows:
            merged_rows.append(target_rows[-1])
    return sorted(
        merged_rows,
        key=lambda row: (
            str(row.get("entry_date", "")),
            str(row.get("entry_id", "")),
            str(row.get("candidate_path", "")),
        ),
    )


def _preserve_user_queue_rows(
    search_queue_path: Path,
    replacement_rows: list[dict[str, object]],
    target_entry_ids: set[str],
) -> list[dict[str, object]]:
    if not search_queue_path.exists() or not target_entry_ids:
        return replacement_rows
    existing_rows = _read_csv(search_queue_path)
    preserved_rows = [
        row
        for row in existing_rows
        if row.get("entry_id", "") in target_entry_ids and _is_user_queue_row(row)
    ]
    if not preserved_rows:
        return replacement_rows

    merged_rows: list[dict[str, object]] = []
    for entry_id in sorted(target_entry_ids):
        existing_for_entry = [row for row in preserved_rows if row.get("entry_id", "") == entry_id]
        replacement_for_entry = [row for row in replacement_rows if row.get("entry_id", "") == entry_id]
        candidate_rows = [
            row
            for row in existing_for_entry + replacement_for_entry
            if row.get("candidate_path", "")
        ]
        if candidate_rows:
            merged_rows.extend(_dedupe_candidate_rows(candidate_rows))
            merged_rows.extend(
                row
                for row in existing_for_entry
                if not row.get("candidate_path", "")
                and str(row.get("review_decision", "")).strip().lower() in FALLBACK_DECISIONS
            )
        elif existing_for_entry:
            merged_rows.append(existing_for_entry[0])
        elif replacement_for_entry:
            merged_rows.append(replacement_for_entry[-1])
    return sorted(
        merged_rows,
        key=lambda row: (
            str(row.get("entry_date", "")),
            str(row.get("entry_id", "")),
            str(row.get("candidate_path", "")),
        ),
    )


def _is_user_queue_row(row: dict[str, object]) -> bool:
    decision = str(row.get("review_decision", "")).strip().lower()
    evidence = str(row.get("evidence", "")).strip().lower()
    return (
        decision in ACCEPT_DECISIONS | ASSOCIATED_PHOTO_DECISIONS | REJECT_DECISIONS | FALLBACK_DECISIONS
        or "manual_link" in evidence
        or "manual_drop_copy" in evidence
    )


def _suppress_duplicate_visible_candidate_rows(
    queue_rows: list[dict[str, object]],
    target_entry_ids: set[str],
) -> list[dict[str, object]]:
    rows_by_candidate: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for index, row in enumerate(queue_rows):
        if str(row.get("entry_id", "")) not in target_entry_ids:
            continue
        if not _is_assignable_candidate_row(row):
            continue
        key = str(row.get("candidate_path", "")).strip()
        rows_by_candidate.setdefault(key, []).append((index, row))

    suppressed_indexes: set[int] = set()
    for candidate_rows in rows_by_candidate.values():
        if len(candidate_rows) <= 1:
            continue
        winner_index, _winner = min(
            candidate_rows,
            key=lambda indexed_row: _candidate_assignment_sort_key(indexed_row[1]),
        )
        suppressed_indexes.update(index for index, _row in candidate_rows if index != winner_index)

    if not suppressed_indexes:
        return queue_rows

    kept_rows = [
        row
        for index, row in enumerate(queue_rows)
        if index not in suppressed_indexes
    ]
    kept_entry_ids = {str(row.get("entry_id", "")) for row in kept_rows}
    suppressed_templates: dict[str, dict[str, object]] = {}
    for index in suppressed_indexes:
        row = queue_rows[index]
        entry_id = str(row.get("entry_id", ""))
        suppressed_templates.setdefault(entry_id, row)
    for entry_id, template in suppressed_templates.items():
        if entry_id not in kept_entry_ids:
            kept_rows.append(_empty_queue_row_from_existing({key: str(value) for key, value in template.items()}))
    return sorted(
        kept_rows,
        key=lambda row: (
            str(row.get("entry_date", "")),
            str(row.get("entry_id", "")),
            str(row.get("candidate_path", "")),
        ),
    )


def _is_assignable_candidate_row(row: dict[str, object]) -> bool:
    return (
        bool(str(row.get("candidate_path", "")).strip())
        and not _is_hidden_candidate_row(row)
        and not _is_rejected_candidate_row(row)
        and not str(row.get("review_decision", "")).strip().lower()
    )


def _candidate_assignment_sort_key(row: dict[str, object]) -> tuple[int, str, str]:
    return (
        _candidate_assignment_distance(row),
        str(row.get("entry_date", "")),
        str(row.get("entry_id", "")),
    )


def _candidate_assignment_distance(row: dict[str, object]) -> int:
    try:
        return int(str(row.get("date_distance", "")).strip())
    except ValueError:
        pass
    distance = _candidate_capture_date_distance(row, str(row.get("entry_date", "")))
    return distance if distance is not None else 999999


def _dedupe_candidate_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        key = (
            str(row.get("entry_id", "")),
            str(row.get("candidate_sha256", "")),
            str(row.get("candidate_path", "")),
        )
        if key not in deduped:
            deduped[key] = row
    return list(deduped.values())


def _load_candidate_filter_reasons(search_queue_path: Path) -> dict[tuple[str, str, str], str]:
    if not search_queue_path.exists():
        return {}
    filters: dict[tuple[str, str, str], str] = {}
    for row in _read_csv(search_queue_path):
        reason = str(row.get("candidate_filter_reason", "")).strip()
        if not reason:
            continue
        key = _candidate_filter_key(
            str(row.get("entry_id", "")),
            str(row.get("candidate_sha256", "")),
            str(row.get("candidate_path", "")),
        )
        if key[0] and key[1] and key[2]:
            filters[key] = reason
    return filters


def _candidate_filter_key(entry_id: str, sha256: str, path: str) -> tuple[str, str, str]:
    return (entry_id, sha256, path)


def _search_batch_plan_rows(
    group_rows: list[dict[str, object]],
    queue_rows: list[dict[str, object]],
    max_gap_days: int = 3,
) -> list[dict[str, object]]:
    entry_ids_by_date: dict[str, list[str]] = {}
    for row in queue_rows:
        entry_id = str(row.get("entry_id", "")).strip()
        entry_date = str(row.get("entry_date", "")).strip()
        if not entry_id or not entry_date:
            continue
        entry_ids_by_date.setdefault(entry_date, [])
        if entry_id not in entry_ids_by_date[entry_date]:
            entry_ids_by_date[entry_date].append(entry_id)

    sorted_groups = sorted(group_rows, key=lambda row: str(row.get("entry_date", "")))
    batches: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    previous_date: dt.date | None = None
    for row in sorted_groups:
        entry_date = dt.date.fromisoformat(str(row["entry_date"]))
        if previous_date is None or (entry_date - previous_date).days <= max_gap_days:
            current.append(row)
        else:
            batches.append(current)
            current = [row]
        previous_date = entry_date
    if current:
        batches.append(current)

    plan_rows = []
    for index, batch in enumerate(batches, start=1):
        dates = [str(row["entry_date"]) for row in batch]
        entry_ids = []
        for entry_date in dates:
            entry_ids.extend(entry_ids_by_date.get(entry_date, []))
        candidate_count = sum(int(row.get("candidate_count", 0) or 0) for row in batch)
        review_date_count = sum(
            1
            for row in batch
            if int(row.get("candidate_count", 0) or 0) > 0
        )
        folder_needed_date_count = len(batch) - review_date_count
        hidden_rejected_candidate_count = sum(
            int(row.get("hidden_rejected_candidate_count", 0) or 0)
            for row in batch
        )
        hidden_export_equivalent_candidate_count = sum(
            int(row.get("hidden_export_equivalent_candidate_count", 0) or 0)
            for row in batch
        )
        entry_count = sum(int(row.get("entry_count", 0) or 0) for row in batch)
        statuses = sorted({str(row.get("status", "")) for row in batch if str(row.get("status", ""))})
        plan_rows.append(
            {
                "batch_id": f"B{index:03d}",
                "start_date": dates[0],
                "end_date": dates[-1],
                "date_count": len(dates),
                "entry_count": entry_count,
                "candidate_count": candidate_count,
                "review_date_count": review_date_count,
                "folder_needed_date_count": folder_needed_date_count,
                "hidden_rejected_candidate_count": hidden_rejected_candidate_count,
                "hidden_export_equivalent_candidate_count": hidden_export_equivalent_candidate_count,
                "statuses": ";".join(statuses),
                "entry_dates": ";".join(dates),
                "entry_ids": ";".join(entry_ids),
                "control_app_start_date": dates[0],
                "control_app_end_date": dates[-1],
                "recommended_action": "review_candidates" if candidate_count else "choose_search_folder",
            }
        )
    return plan_rows


def apply_reviewed_external_references(
    canonical_root: Path,
    reviewed_csv: Path,
) -> ApplySummary:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    if not reviewed_csv.exists():
        raise FileNotFoundError(f"Missing reviewed CSV: {reviewed_csv}")

    rows = _read_csv(reviewed_csv)
    selected_rows = [
        row
        for row in rows
        if row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
        and row.get("candidate_path", "").strip()
    ]
    selected_entry_ids = {row.get("entry_id", "").strip() for row in selected_rows}
    associated_rows = [
        row
        for row in rows
        if row.get("review_decision", "").strip().lower() in ASSOCIATED_PHOTO_DECISIONS
        and row.get("candidate_path", "").strip()
    ]
    rejected_rows = [
        row
        for row in rows
        if row.get("review_decision", "").strip().lower() in REJECT_DECISIONS
        and row.get("candidate_path", "").strip()
        and row.get("entry_id", "").strip() not in selected_entry_ids
    ]
    fallback_rows = [
        row
        for row in rows
        if row.get("review_decision", "").strip().lower() in FALLBACK_DECISIONS
        and row.get("entry_id", "").strip()
    ]
    fallback_rows = _dedupe_fallback_rows(fallback_rows, selected_rows)
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        reviewed_rows = selected_rows + associated_rows + rejected_rows + fallback_rows
        reviewed_media_ids = {
            row.get("project365_media_asset_id", "").strip()
            for row in reviewed_rows
            if row.get("project365_media_asset_id", "").strip()
        }
        known_media_ids = _known_media_asset_ids(connection, reviewed_media_ids)
        skipped_unknown_media_count = 0

        def known_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
            nonlocal skipped_unknown_media_count
            result = []
            for row in rows:
                media_id = row.get("project365_media_asset_id", "").strip()
                if media_id not in known_media_ids:
                    skipped_unknown_media_count += 1
                    continue
                result.append(row)
            return result

        selected_rows = known_rows(selected_rows)
        associated_rows = known_rows(associated_rows)
        rejected_rows = known_rows(rejected_rows)
        fallback_rows = known_rows(fallback_rows)
        selected_count = 0
        for row in selected_rows:
            _upsert_external_decision(connection, row, "external_original_reference", "available", "confirmed")
            selected_count += 1
        associated_count = 0
        for row in associated_rows:
            _upsert_external_decision(connection, row, "external_original_associated_photo", "available", "confirmed")
            associated_count += 1
        rejected_count = 0
        for row in rejected_rows:
            _upsert_external_decision(connection, row, "external_original_rejected", "rejected", "rejected")
            rejected_count += 1
        fallback_count = 0
        for row in fallback_rows:
            _upsert_fallback_decision(connection, row)
            fallback_count += 1
        connection.commit()
    finally:
        connection.close()
    return ApplySummary(
        selected_count=selected_count,
        rejected_count=rejected_count,
        fallback_count=fallback_count,
        associated_count=associated_count,
        skipped_unknown_media_count=skipped_unknown_media_count,
    )


def _dedupe_fallback_rows(
    fallback_rows: list[dict[str, str]],
    selected_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    selected_entry_ids = {row.get("entry_id", "").strip() for row in selected_rows}
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in fallback_rows:
        entry_id = row.get("entry_id", "").strip()
        if not entry_id or entry_id in selected_entry_ids or entry_id in seen:
            continue
        deduped.append(row)
        seen.add(entry_id)
    return deduped


def _known_media_asset_ids(connection: sqlite3.Connection, media_ids: set[str]) -> set[str]:
    if not media_ids:
        return set()
    known: set[str] = set()
    ordered_ids = sorted(media_ids)
    for index in range(0, len(ordered_ids), 500):
        chunk = ordered_ids[index : index + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT id FROM media_assets WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        known.update(str(row[0]) for row in rows)
    return known


def mark_fallback_external_references(
    canonical_root: Path,
    review_queue_path: Path,
    target_entry_ids: set[str] | None = None,
    target_entry_dates: set[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_targets: int | None = None,
) -> ApplySummary:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    _validate_target_filters(target_entry_dates, start_date, end_date, max_targets)
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        exports = _load_unclear_exports(connection, review_queue_path)
        exports = _filter_target_exports(
            exports,
            target_entry_ids=target_entry_ids,
            target_entry_dates=target_entry_dates,
            start_date=start_date,
            end_date=end_date,
            max_targets=max_targets,
        )
        fallback_count = 0
        for export in exports:
            _upsert_fallback_decision(
                connection,
                {
                    "entry_id": str(export["entry_id"]),
                    "project365_media_asset_id": str(export["project365_media_asset_id"]),
                    "review_decision": "keep_project365_export",
                    "review_notes": "No external original selected; keep Project365 export as fallback.",
                },
            )
            fallback_count += 1
        connection.commit()
    finally:
        connection.close()
    return ApplySummary(selected_count=0, rejected_count=0, fallback_count=fallback_count)


def prune_applied_review_queue(
    canonical_root: Path,
    queue_path: Path,
    report_dir: Path,
) -> dict[str, int | str]:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    if not queue_path.exists():
        raise FileNotFoundError(f"Missing search queue: {queue_path}")

    fieldnames, rows = _read_csv_with_fieldnames(queue_path)
    connection = sqlite3.connect(db_path)
    try:
        completed_entry_ids = _completed_external_entry_ids(connection)
        rejected_candidates = _load_rejected_candidates(connection)
        queued_media_ids = {
            row.get("project365_media_asset_id", "").strip()
            for row in rows
            if row.get("project365_media_asset_id", "").strip()
        }
        known_media_ids = _known_media_asset_ids(connection, queued_media_ids)
    finally:
        connection.close()

    kept_rows: list[dict[str, object]] = []
    removed_completed_entries: set[str] = set()
    removed_unknown_media_entries: set[str] = set()
    removed_rejected_candidates = 0
    for entry_id, entry_rows in _rows_by_entry(rows).items():
        if entry_id in completed_entry_ids:
            removed_completed_entries.add(entry_id)
            continue
        entry_media_ids = {
            row.get("project365_media_asset_id", "").strip()
            for row in entry_rows
            if row.get("project365_media_asset_id", "").strip()
        }
        if entry_media_ids and not (entry_media_ids & known_media_ids):
            removed_unknown_media_entries.add(entry_id)
            continue
        filtered_rows = []
        for row in entry_rows:
            if _is_applied_rejected_row(row, rejected_candidates):
                removed_rejected_candidates += 1
                continue
            filtered_rows.append(row)
        if filtered_rows:
            kept_rows.extend(filtered_rows)
        else:
            kept_rows.append(_empty_queue_row_from_existing(entry_rows[0]))

    kept_rows = sorted(
        kept_rows,
        key=lambda row: (
            str(row.get("entry_date", "")),
            str(row.get("entry_id", "")),
            str(row.get("candidate_path", "")),
        ),
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    group_rows = _group_rows_from_queue(kept_rows, rejected_candidates)
    batch_rows = _search_batch_plan_rows(group_rows, kept_rows)
    _write_csv(queue_path, kept_rows, fieldnames or _search_queue_fieldnames())
    _write_csv(report_dir / "original_photo_unclear_groups.csv", group_rows, _group_fieldnames())
    _write_csv(report_dir / "original_photo_search_batch_plan.csv", batch_rows, _batch_plan_fieldnames())
    return {
        "queue_rows": len(kept_rows),
        "entry_count": len({str(row.get("entry_id", "")) for row in kept_rows if row.get("entry_id")}),
        "removed_completed_entries": len(removed_completed_entries),
        "removed_unknown_media_entries": len(removed_unknown_media_entries),
        "removed_rejected_candidates": removed_rejected_candidates,
        "search_queue_path": str(queue_path),
        "group_report_path": str(report_dir / "original_photo_unclear_groups.csv"),
        "batch_plan_path": str(report_dir / "original_photo_search_batch_plan.csv"),
    }


def score_review_queue_alignment(
    canonical_root: Path,
    queue_path: Path,
    sample_size: int = 24,
    rescore: bool = False,
    target_entry_ids: set[str] | None = None,
    target_entry_dates: set[str] | None = None,
    max_candidates: int | None = None,
    progress_interval: int = 0,
    progress_sink: Callable[[str], None] | None = None,
) -> dict[str, int | str]:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    if not queue_path.exists():
        raise FileNotFoundError(f"Missing search queue: {queue_path}")
    if sample_size < 8:
        raise ValueError("sample-size must be at least 8")
    if max_candidates is not None and max_candidates <= 0:
        raise ValueError("max-candidates must be positive")
    if progress_interval < 0:
        raise ValueError("progress-interval must be zero or positive")

    fieldnames, rows = _read_csv_with_fieldnames(queue_path)
    fieldnames = _alignment_fieldnames(fieldnames or _search_queue_fieldnames())
    media_ids = {
        row.get("project365_media_asset_id", "").strip()
        for row in rows
        if row.get("project365_media_asset_id", "").strip()
    }
    reference_paths = _project365_media_paths(db_path, media_ids)

    candidate_work_rows: list[dict[str, str]] = []
    for row in rows:
        candidate_path = row.get("candidate_path", "").strip()
        if not candidate_path:
            continue
        if target_entry_ids and row.get("entry_id", "").strip() not in target_entry_ids:
            continue
        if target_entry_dates and row.get("entry_date", "").strip() not in target_entry_dates:
            continue
        if max_candidates is not None and len(candidate_work_rows) >= max_candidates:
            break
        candidate_work_rows.append(row)

    candidate_rows = 0
    scored_rows = 0
    skipped_rows = 0
    error_rows = 0
    progress_started_at = dt.datetime.now(dt.UTC)
    if progress_sink and progress_interval:
        progress_sink(
            _alignment_progress_message(
                processed=0,
                total=len(candidate_work_rows),
                scored=scored_rows,
                skipped=skipped_rows,
                errors=error_rows,
                started_at=progress_started_at,
            )
        )
    for row in candidate_work_rows:
        candidate_rows += 1
        candidate_path = row.get("candidate_path", "").strip()
        if not rescore and (
            row.get("alignment_score", "").strip()
            or row.get("alignment_error", "").strip()
        ):
            _update_candidate_filter_from_alignment(row)
            skipped_rows += 1
            _emit_alignment_progress(
                progress_sink,
                progress_interval,
                processed=candidate_rows,
                total=len(candidate_work_rows),
                scored=scored_rows,
                skipped=skipped_rows,
                errors=error_rows,
                started_at=progress_started_at,
            )
            continue
        reference_path = reference_paths.get(row.get("project365_media_asset_id", "").strip())
        if reference_path is None:
            _set_alignment_error(row, "missing_reference")
            error_rows += 1
            _emit_alignment_progress(
                progress_sink,
                progress_interval,
                processed=candidate_rows,
                total=len(candidate_work_rows),
                scored=scored_rows,
                skipped=skipped_rows,
                errors=error_rows,
                started_at=progress_started_at,
            )
            continue
        try:
            suggestion = suggest_crop(
                reference_path=reference_path,
                candidate_path=Path(candidate_path),
                sample_size=sample_size,
            )
        except Exception as exc:  # noqa: BLE001 - stored as review evidence, not hidden.
            _set_alignment_error(row, type(exc).__name__)
            error_rows += 1
            _emit_alignment_progress(
                progress_sink,
                progress_interval,
                processed=candidate_rows,
                total=len(candidate_work_rows),
                scored=scored_rows,
                skipped=skipped_rows,
                errors=error_rows,
                started_at=progress_started_at,
            )
            continue
        _set_alignment_score(row, asdict(suggestion))
        _update_candidate_filter_from_alignment(row)
        scored_rows += 1
        _emit_alignment_progress(
            progress_sink,
            progress_interval,
            processed=candidate_rows,
            total=len(candidate_work_rows),
            scored=scored_rows,
            skipped=skipped_rows,
            errors=error_rows,
            started_at=progress_started_at,
        )

    _write_csv(queue_path, rows, fieldnames)
    _refresh_queue_reports_from_rows(db_path, queue_path.parent, rows)
    return {
        "candidate_rows": candidate_rows,
        "scored_rows": scored_rows,
        "skipped_rows": skipped_rows,
        "error_rows": error_rows,
        "queue_path": str(queue_path),
    }


def _print_progress(message: str) -> None:
    print(message, flush=True)


def _emit_alignment_progress(
    progress_sink: Callable[[str], None] | None,
    progress_interval: int,
    *,
    processed: int,
    total: int,
    scored: int,
    skipped: int,
    errors: int,
    started_at: dt.datetime,
) -> None:
    if not progress_sink or not progress_interval:
        return
    if processed != total and processed % progress_interval != 0:
        return
    progress_sink(
        _alignment_progress_message(
            processed=processed,
            total=total,
            scored=scored,
            skipped=skipped,
            errors=errors,
            started_at=started_at,
        )
    )


def _alignment_progress_message(
    *,
    processed: int,
    total: int,
    scored: int,
    skipped: int,
    errors: int,
    started_at: dt.datetime,
) -> str:
    elapsed_seconds = max(0.0, (dt.datetime.now(dt.UTC) - started_at).total_seconds())
    percent = (processed / total * 100) if total else 100.0
    rate = processed / elapsed_seconds if elapsed_seconds and processed else 0.0
    remaining = total - processed
    eta = _format_duration(remaining / rate) if rate else "unknown"
    return (
        "Alignment progress: "
        f"{processed}/{total} candidates ({percent:.1f}%) · "
        f"scored {scored} · skipped {skipped} · errors {errors} · "
        f"elapsed {_format_duration(elapsed_seconds)} · eta {eta}"
    )


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _project365_media_paths(db_path: Path, media_ids: set[str]) -> dict[str, Path]:
    if not media_ids:
        return {}
    placeholders = ",".join("?" for _ in media_ids)
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            f"""
            SELECT id, storage_path
            FROM media_assets
            WHERE id IN ({placeholders})
            """,
            sorted(media_ids),
        ).fetchall()
    finally:
        connection.close()
    return {
        media_id: Path(storage_path)
        for media_id, storage_path in rows
        if storage_path
    }


def _set_alignment_score(row: dict[str, str], suggestion: dict[str, object]) -> None:
    row["alignment_score"] = f"{float(suggestion['score']):.2f}"
    row["alignment_confidence"] = str(suggestion["confidence"])
    row["alignment_crop"] = "{x},{y},{width},{height}".format(**suggestion)
    row["alignment_reference_size"] = "{reference_width}x{reference_height}".format(**suggestion)
    row["alignment_candidate_size"] = "{candidate_width}x{candidate_height}".format(**suggestion)
    row["alignment_error"] = ""


def _set_alignment_error(row: dict[str, str], error: str) -> None:
    row["alignment_score"] = ""
    row["alignment_confidence"] = ""
    row["alignment_crop"] = ""
    row["alignment_reference_size"] = ""
    row["alignment_candidate_size"] = ""
    row["alignment_error"] = error
    if row.get("candidate_filter_reason") == CANDIDATE_FILTER_EXPORT_EQUIVALENT:
        row["candidate_filter_reason"] = ""


def _alignment_fieldnames(fieldnames: list[str]) -> list[str]:
    updated = list(fieldnames)
    for field in ["candidate_filter_reason", *_alignment_queue_fieldnames(), *visual_fieldnames()]:
        if field not in updated:
            updated.append(field)
    return updated


def _update_candidate_filter_from_alignment(row: dict[str, str]) -> None:
    if row.get("review_decision", "").strip().lower():
        return
    if _is_export_equivalent_alignment(row):
        row["candidate_filter_reason"] = CANDIDATE_FILTER_EXPORT_EQUIVALENT
    elif row.get("candidate_filter_reason") == CANDIDATE_FILTER_EXPORT_EQUIVALENT:
        row["candidate_filter_reason"] = ""


def _is_export_equivalent_alignment(row: dict[str, str]) -> bool:
    try:
        score = float(row.get("alignment_score", "").strip())
    except ValueError:
        return False
    return (
        score <= 0.01
        and row.get("alignment_reference_size", "").strip()
        and row.get("alignment_reference_size", "").strip()
        == row.get("alignment_candidate_size", "").strip()
    )


def _is_hidden_candidate_row(row: dict[str, object]) -> bool:
    if not str(row.get("candidate_path", "")).strip():
        return False
    if str(row.get("review_decision", "")).strip().lower():
        return False
    return str(row.get("candidate_filter_reason", "")).strip().lower() in HIDDEN_CANDIDATE_FILTER_REASONS


def _hidden_export_equivalent_candidate_count(rows: list[dict[str, object]]) -> int:
    return sum(
        1
        for row in rows
        if str(row.get("candidate_filter_reason", "")).strip().lower()
        == CANDIDATE_FILTER_EXPORT_EQUIVALENT
        and not str(row.get("review_decision", "")).strip().lower()
        and str(row.get("candidate_path", "")).strip()
    )


def _visible_candidate_count(rows: list[dict[str, object]]) -> int:
    return sum(
        1
        for row in rows
        if str(row.get("candidate_path", "")).strip()
        and not _is_hidden_candidate_row(row)
        and not _is_rejected_candidate_row(row)
    )


def _is_rejected_candidate_row(row: dict[str, object]) -> bool:
    return (
        str(row.get("candidate_path", "")).strip()
        and str(row.get("review_decision", "")).strip().lower() in REJECT_DECISIONS
    )


def _refresh_queue_reports_from_rows(
    db_path: Path,
    report_dir: Path,
    rows: list[dict[str, object]],
) -> None:
    connection = sqlite3.connect(db_path)
    try:
        rejected_candidates = _load_rejected_candidates(connection)
    finally:
        connection.close()
    group_rows = _group_rows_from_queue(rows, rejected_candidates)
    batch_rows = _search_batch_plan_rows(group_rows, rows)
    _write_csv(report_dir / "original_photo_unclear_groups.csv", group_rows, _group_fieldnames())
    _write_csv(report_dir / "original_photo_search_batch_plan.csv", batch_rows, _batch_plan_fieldnames())


def refresh_external_original_queue_reports(
    canonical_root: Path,
    report_dir: Path,
    rows: list[dict[str, object]],
) -> None:
    _refresh_queue_reports_from_rows(canonical_root / "canonical.db", report_dir, rows)


def _completed_external_entry_ids(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT entry_id
            FROM media_assets
            WHERE role IN ('external_original_reference', 'external_original_fallback')
            AND review_status = 'confirmed'
            """
        ).fetchall()
    }


def _confirmed_external_original_rows_by_entry(
    connection: sqlite3.Connection,
) -> dict[str, list[sqlite3.Row]]:
    rows = connection.execute(
        """
        SELECT entry_id, sha256, storage_path, transformation_json
        FROM media_assets
        WHERE role = 'external_original_reference'
            AND review_status = 'confirmed'
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["entry_id"]), []).append(row)
    return grouped


def _fallback_confirmed_entry_ids(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT entry_id
            FROM media_assets
            WHERE role = 'external_original_fallback'
                AND review_status = 'confirmed'
            """
        ).fetchall()
    }


def _confirmed_original_rows_are_low_quality(rows: list[sqlite3.Row]) -> bool:
    if not rows:
        return False
    return all(_confirmed_original_row_is_low_quality(row) for row in rows)


def _confirmed_original_row_is_low_quality(row: sqlite3.Row) -> bool:
    transformation = _parse_json_object(str(row["transformation_json"] or ""))
    if transformation.get("original_low_quality") is True:
        return True
    dimensions = _dimensions_from_transformation(transformation)
    if dimensions is None:
        dimensions = _image_dimensions_tuple(Path(str(row["storage_path"] or "")))
    return _dimensions_are_low_quality(dimensions)


def _dimensions_from_transformation(transformation: dict[str, object]) -> tuple[int, int] | None:
    dimensions = transformation.get("original_dimensions")
    if not isinstance(dimensions, dict):
        return None
    try:
        width = int(dimensions["width"])
        height = int(dimensions["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _rows_by_entry(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row.get("entry_id", ""), []).append(row)
    return grouped


def _is_applied_rejected_row(
    row: dict[str, str],
    rejected_candidates: dict[str, set[tuple[str, str]]],
) -> bool:
    decision = row.get("review_decision", "").strip().lower()
    entry_id = row.get("entry_id", "")
    candidate_sha256 = row.get("candidate_sha256", "")
    candidate_path = row.get("candidate_path", "")
    rejected = rejected_candidates.get(entry_id, set())
    return (
        decision in REJECT_DECISIONS
        and (
            (candidate_sha256, candidate_path) in rejected
            or any(candidate_sha256 == sha256 for sha256, _path in rejected)
        )
    )


def _empty_queue_row_from_existing(row: dict[str, str]) -> dict[str, object]:
    empty = {field: "" for field in _search_queue_fieldnames()}
    empty.update(
        {
            "entry_id": row.get("entry_id", ""),
            "entry_date": row.get("entry_date", ""),
            "project365_media_asset_id": row.get("project365_media_asset_id", ""),
            "current_match_status": row.get("current_match_status", ""),
            "current_decision": row.get("current_decision", ""),
            "review_decision": "search_needed",
        }
    )
    return empty


def _group_rows_from_queue(
    queue_rows: list[dict[str, object]],
    rejected_candidates: dict[str, set[tuple[str, str]]],
) -> list[dict[str, object]]:
    rows_by_date: dict[str, list[dict[str, object]]] = {}
    for row in queue_rows:
        rows_by_date.setdefault(str(row.get("entry_date", "")), []).append(row)
    group_rows = []
    for entry_date in sorted(rows_by_date):
        date_rows = rows_by_date[entry_date]
        entry_ids = {str(row.get("entry_id", "")) for row in date_rows if row.get("entry_id")}
        candidate_count = sum(
            1
            for row in date_rows
            if row.get("candidate_path")
            and not _is_hidden_candidate_row(row)
            and not _is_rejected_candidate_row(row)
        )
        hidden_rejected_keys = {
            (entry_id, sha256, storage_path)
            for entry_id in entry_ids
            for sha256, storage_path in rejected_candidates.get(entry_id, set())
        }
        hidden_rejected_keys.update(
            (
                str(row.get("entry_id", "")),
                str(row.get("candidate_sha256", "")),
                str(row.get("candidate_path", "")),
            )
            for row in date_rows
            if _is_rejected_candidate_row(row)
        )
        hidden_rejected_count = len(hidden_rejected_keys)
        hidden_export_equivalent_count = _hidden_export_equivalent_candidate_count(date_rows)
        group_rows.append(
            {
                "entry_date": entry_date,
                "entry_count": len(entry_ids),
                "candidate_count": candidate_count,
                "hidden_rejected_candidate_count": hidden_rejected_count,
                "hidden_export_equivalent_candidate_count": hidden_export_equivalent_count,
                "status": _group_status(
                    candidate_count,
                    hidden_rejected_count,
                    hidden_export_equivalent_count,
                ),
            }
        )
    return group_rows


def _upsert_external_decision(
    connection: sqlite3.Connection,
    row: dict[str, str],
    role: str,
    status: str,
    review_status: str,
) -> None:
    source_path = Path(row["candidate_path"])
    if not source_path.exists():
        raise FileNotFoundError(f"Missing reviewed original: {source_path}")
    export_media = connection.execute(
        """
        SELECT import_batch_id
        FROM media_assets
        WHERE id = ?
        """,
        (row["project365_media_asset_id"],),
    ).fetchone()
    if export_media is None:
        raise ValueError(f"Unknown Project365 media asset: {row['project365_media_asset_id']}")
    sha256 = _sha256_file(source_path)
    decision_id = _external_decision_id(row["entry_id"], sha256, role)
    existing_reference = None
    if role == "external_original_reference":
        existing_reference = connection.execute(
            """
            SELECT id, transformation_json
            FROM media_assets
            WHERE entry_id = ?
                AND role = 'external_original_reference'
                AND review_status = 'confirmed'
                AND id = ?
            """,
            (row["entry_id"], decision_id),
        ).fetchone()
    now = dt.datetime.now(dt.UTC).isoformat()
    source_name_by_role = {
        "external_original_reference": "external_original_reference",
        "external_original_associated_photo": "external_original_associated_photo",
        "external_original_rejected": "external_original_rejection",
    }
    source_name = source_name_by_role.get(role, role)
    review_crop = _review_crop_from_row(row)
    if review_crop is None and existing_reference is not None:
        existing_transformation = _parse_json_object(str(existing_reference["transformation_json"] or ""))
        existing_crop = existing_transformation.get("review_crop")
        if isinstance(existing_crop, dict):
            review_crop = existing_crop
    transformation = {
        "source": source_name,
        "source_path": str(source_path),
        "evidence": row.get("evidence", ""),
        "review_decision": row.get("review_decision", ""),
        "review_notes": row.get("review_notes", ""),
        "review_crop": review_crop,
        "original_is_read_only": True,
    }
    associated_date = row.get("associated_entry_date", "").strip()
    if role == "external_original_associated_photo":
        transformation["associated_entry_date"] = associated_date or row.get("entry_date", "")
        transformation["associated_date_source"] = row.get("associated_date_source", "manual").strip() or "manual"
    if role == "external_original_reference":
        dimensions = _image_dimensions_tuple(source_path)
        if dimensions is not None:
            width, height = dimensions
            transformation["original_dimensions"] = {"width": width, "height": height}
            transformation["original_low_quality"] = _dimensions_are_low_quality(dimensions)
            if transformation["original_low_quality"]:
                transformation["low_quality_reason"] = "dimension_below_1000px"
                transformation["better_deal_search_eligible"] = True
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
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT(id)
        DO UPDATE SET
            storage_path = excluded.storage_path,
            sha256 = excluded.sha256,
            byte_size = excluded.byte_size,
            mime_type = excluded.mime_type,
            status = excluded.status,
            review_status = excluded.review_status,
            selected_default = excluded.selected_default,
            transformation_json = excluded.transformation_json,
            updated_at = excluded.updated_at
        """,
        (
            decision_id,
            row["entry_id"],
            role,
            source_path.name,
            str(source_path),
            sha256,
            source_path.stat().st_size,
            _mime_type(source_path),
            status,
            review_status,
            json.dumps(transformation, sort_keys=True),
            export_media["import_batch_id"],
            now,
            now,
        ),
    )
    if role == "external_original_reference":
        connection.execute(
            """
            DELETE FROM media_assets
            WHERE entry_id = ?
                AND role = 'external_original_reference'
                AND id != ?
            """,
            (row["entry_id"], decision_id),
        )
        connection.execute(
            """
            DELETE FROM media_assets
            WHERE entry_id = ?
                AND role = 'external_original_rejected'
            """,
            (row["entry_id"],),
        )
        connection.execute(
            """
            DELETE FROM media_assets
            WHERE entry_id = ?
                AND role = 'external_original_fallback'
            """,
            (row["entry_id"],),
        )
    elif role == "external_original_rejected":
        connection.execute(
            """
            DELETE FROM media_assets
            WHERE id = ?
            """,
            (_external_decision_id(row["entry_id"], sha256, "external_original_reference"),),
        )


def _upsert_fallback_decision(
    connection: sqlite3.Connection,
    row: dict[str, str],
) -> None:
    export_media = connection.execute(
        """
        SELECT import_batch_id
        FROM media_assets
        WHERE id = ?
        """,
        (row["project365_media_asset_id"],),
    ).fetchone()
    if export_media is None:
        raise ValueError(f"Unknown Project365 media asset: {row['project365_media_asset_id']}")
    now = dt.datetime.now(dt.UTC).isoformat()
    sha256 = hashlib.sha256(f"{row['entry_id']}:external_original_fallback".encode("utf-8")).hexdigest()
    transformation = {
        "source": "external_original_fallback",
        "review_decision": row.get("review_decision", ""),
        "review_notes": row.get("review_notes", ""),
        "fallback_media_asset_id": row["project365_media_asset_id"],
        "original_is_read_only": True,
    }
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
        VALUES (?, ?, 'external_original_fallback', NULL, ?, '', ?, 0,
                'application/x-project365-fallback', 'fallback', 'confirmed', 0,
                ?, ?, ?, ?)
        ON CONFLICT(id)
        DO UPDATE SET
            status = excluded.status,
            review_status = excluded.review_status,
            transformation_json = excluded.transformation_json,
            updated_at = excluded.updated_at
        """,
        (
            f"{row['entry_id']}:external_original_fallback",
            row["entry_id"],
            "project365-export-fallback",
            sha256,
            json.dumps(transformation, sort_keys=True),
            export_media["import_batch_id"],
            now,
            now,
        ),
    )
    connection.execute(
        """
        DELETE FROM media_assets
        WHERE entry_id = ?
            AND role = 'external_original_reference'
        """,
        (row["entry_id"],),
    )


def _load_unclear_exports(
    connection: sqlite3.Connection,
    review_queue_path: Path,
    include_low_quality_matches: bool = False,
    pending_rejected_candidates: dict[str, set[tuple[str, str]]] | None = None,
) -> list[dict[str, object]]:
    fallback_entry_ids = _fallback_confirmed_entry_ids(connection)
    confirmed_originals_by_entry = _confirmed_external_original_rows_by_entry(connection)
    pending_rejected_candidates = pending_rejected_candidates or {}
    all_exports = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT
                entries.id AS entry_id,
                entries.entry_date,
                media_assets.id AS project365_media_asset_id
            FROM entries
            JOIN media_assets
                ON media_assets.entry_id = entries.id
                AND media_assets.role IN ({",".join("?" for _ in ORIGINAL_PICKER_TARGET_ROLES)})
            ORDER BY entries.entry_date, entries.id
            """,
            ORIGINAL_PICKER_TARGET_ROLES,
        )
    ]
    exports: list[dict[str, object]] = []
    for export in all_exports:
        entry_id = str(export["entry_id"])
        if entry_id in fallback_entry_ids:
            continue
        confirmed_rows = [
            row
            for row in confirmed_originals_by_entry.get(entry_id, [])
            if not _confirmed_original_row_is_pending_rejected(row, pending_rejected_candidates)
        ]
        if not confirmed_rows:
            exports.append(export)
            continue
        if include_low_quality_matches and _confirmed_original_rows_are_low_quality(confirmed_rows):
            export["current_match_status"] = "low_quality_match"
            export["current_decision"] = "better_deal_search"
            exports.append(export)
    review_rows = _review_rows_by_entry(review_queue_path)
    if not review_rows:
        return exports

    unclear = []
    for export in exports:
        rows = review_rows.get(str(export["entry_id"]), [])
        if str(export.get("current_decision", "")) == "better_deal_search":
            export["current_match_status"] = str(export.get("current_match_status") or "low_quality_match")
            export["current_decision"] = "better_deal_search"
            unclear.append(export)
            continue
        if not rows or not _has_clear_match(rows):
            export["current_match_status"] = ";".join(sorted({row["match_status"] for row in rows})) if rows else ""
            export["current_decision"] = ";".join(sorted({row["decision"] for row in rows})) if rows else ""
            unclear.append(export)
    return unclear


def _validate_target_filters(
    target_entry_dates: set[str] | None,
    start_date: str | None,
    end_date: str | None,
    max_targets: int | None,
) -> None:
    for value in target_entry_dates or set():
        _validate_date(value, "entry-date")
    if start_date:
        _validate_date(start_date, "start-date")
    if end_date:
        _validate_date(end_date, "end-date")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start-date must be before or equal to end-date")
    if max_targets is not None and max_targets <= 0:
        raise ValueError("max-targets must be positive")


def _filter_target_exports(
    exports: list[dict[str, object]],
    target_entry_ids: set[str] | None,
    target_entry_dates: set[str] | None,
    start_date: str | None,
    end_date: str | None,
    max_targets: int | None,
) -> list[dict[str, object]]:
    entry_ids = {value.strip() for value in target_entry_ids or set() if value.strip()}
    entry_dates = {value.strip() for value in target_entry_dates or set() if value.strip()}
    filtered = []
    for export in exports:
        entry_id = str(export["entry_id"])
        entry_date = str(export["entry_date"])
        if entry_ids and entry_id not in entry_ids:
            continue
        if entry_dates and entry_date not in entry_dates:
            continue
        if start_date and entry_date < start_date:
            continue
        if end_date and entry_date > end_date:
            continue
        filtered.append(export)
    if max_targets is not None:
        return filtered[:max_targets]
    return filtered


def _validate_date(value: str, name: str) -> None:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be YYYY-MM-DD")


def _review_rows_by_entry(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {}
    rows_by_entry: dict[str, list[dict[str, str]]] = {}
    for row in _read_csv(path):
        rows_by_entry.setdefault(row["entry_id"], []).append(row)
    return rows_by_entry


def _has_clear_match(rows: list[dict[str, str]]) -> bool:
    return any(
        row.get("decision") == "auto_accept"
        or (row.get("match_status") == "candidate" and row.get("confidence") == "high")
        for row in rows
    )


def _load_rejected_candidates(connection: sqlite3.Connection) -> dict[str, set[tuple[str, str]]]:
    rows = connection.execute(
        """
        SELECT entry_id, sha256, storage_path
        FROM media_assets
        WHERE role = 'external_original_rejected'
            AND review_status = 'rejected'
        """
    ).fetchall()
    rejected: dict[str, set[tuple[str, str]]] = {}
    for entry_id, sha256, storage_path in rows:
        rejected.setdefault(entry_id, set()).add((sha256, storage_path))
    return rejected


def _load_pending_rejected_candidates(search_queue_path: Path) -> dict[str, set[tuple[str, str]]]:
    decision_path = search_queue_path.with_name(f"{search_queue_path.stem}_picker_decisions.json")
    if not decision_path.exists() or not search_queue_path.exists():
        return {}
    try:
        payload = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return {}
    rejected_paths: dict[str, set[str]] = {}
    for entry_id, overrides in entries.items():
        if not isinstance(overrides, dict):
            continue
        for key, record in overrides.items():
            if not isinstance(record, dict):
                continue
            if str(record.get("review_decision", "")).strip().lower() not in REJECT_DECISIONS:
                continue
            path = str(key).removeprefix("candidate:").strip()
            if path:
                rejected_paths.setdefault(str(entry_id), set()).add(path)
    if not rejected_paths:
        return {}
    pending: dict[str, set[tuple[str, str]]] = {}
    for row in _read_csv(search_queue_path):
        entry_id = str(row.get("entry_id", ""))
        path = str(row.get("candidate_path", ""))
        if path and path in rejected_paths.get(entry_id, set()):
            pending.setdefault(entry_id, set()).add((str(row.get("candidate_sha256", "")), path))
    return pending


def _load_pending_crop_rejected_candidates(search_queue_path: Path) -> dict[str, set[tuple[str, str]]]:
    staging_path = search_queue_path.with_name(f"{search_queue_path.stem}_crop_staging.json")
    if not staging_path.exists():
        return {}
    try:
        payload = json.loads(staging_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return {}
    pending: dict[str, set[tuple[str, str]]] = {}
    for entry_id, candidates in entries.items():
        if not isinstance(candidates, dict):
            continue
        for candidate_path, record in candidates.items():
            if not isinstance(record, dict):
                continue
            if str(record.get("action", "")).strip().lower() != "reject_original":
                continue
            if record.get("commit_pending") is False:
                continue
            rejected_row = record.get("rejected_row")
            if not isinstance(rejected_row, dict):
                rejected_row = {}
            path = str(rejected_row.get("candidate_path") or candidate_path).strip()
            sha256 = str(rejected_row.get("candidate_sha256", "")).strip()
            if path or sha256:
                pending.setdefault(str(entry_id), set()).add((sha256, path))
    return pending


def _confirmed_original_row_is_pending_rejected(
    row: sqlite3.Row,
    pending_rejected_candidates: dict[str, set[tuple[str, str]]],
) -> bool:
    entry_id = str(row["entry_id"])
    rejected = pending_rejected_candidates.get(entry_id, set())
    if not rejected:
        return False
    sha256 = str(row["sha256"] or "").strip()
    storage_path = str(row["storage_path"] or "").strip()
    return (
        (sha256, storage_path) in rejected
        or bool(sha256 and any(sha256 == rejected_sha for rejected_sha, _ in rejected))
        or bool(storage_path and any(storage_path == rejected_path for _, rejected_path in rejected))
    )


def _merge_rejected_candidates(
    first: dict[str, set[tuple[str, str]]],
    second: dict[str, set[tuple[str, str]]],
) -> dict[str, set[tuple[str, str]]]:
    if not second:
        return first
    merged = {entry_id: set(values) for entry_id, values in first.items()}
    for entry_id, values in second.items():
        merged.setdefault(entry_id, set()).update(values)
    return merged


def _filter_rejected_candidates(
    export: dict[str, object],
    candidates: list[dict[str, object]],
    rejected_candidates: dict[str, set[tuple[str, str]]],
) -> list[dict[str, object]]:
    rejected = rejected_candidates.get(str(export["entry_id"]), set())
    if not rejected:
        return candidates
    return [
        candidate
        for candidate in candidates
        if (
            str(candidate["candidate_sha256"]),
            str(candidate["candidate_path"]),
        )
        not in rejected
        and not any(str(candidate["candidate_sha256"]) == sha256 for sha256, _ in rejected)
        and not any(str(candidate["candidate_path"]) == path for _sha256, path in rejected)
    ]


def _hidden_rejected_candidate_count(
    export: dict[str, object],
    candidates: list[dict[str, object]],
    rejected_candidates: dict[str, set[tuple[str, str]]],
) -> int:
    return len(candidates) - len(_filter_rejected_candidates(export, candidates, rejected_candidates))


def _group_status(
    candidate_count: int,
    hidden_rejected_candidate_count: int,
    hidden_export_equivalent_candidate_count: int = 0,
) -> str:
    if candidate_count:
        return "needs_choice"
    if hidden_rejected_candidate_count:
        return "all_candidates_rejected"
    if hidden_export_equivalent_candidate_count:
        return "only_export_equivalent_candidates"
    return "no_external_candidates"


def _candidate_rows_for_export(
    export: dict[str, object],
    source_candidates: list[dict[str, object]],
    indexed_candidates: list[dict[str, object]],
    rejected_candidates: dict[str, set[tuple[str, str]]],
    known_candidate_filters: dict[tuple[str, str, str], str] | None = None,
) -> tuple[list[dict[str, object]], int]:
    source_candidates = _apply_known_candidate_filters(export, source_candidates, known_candidate_filters or {})
    indexed_candidates = _apply_known_candidate_filters(export, indexed_candidates, known_candidate_filters or {})
    source_rows = _filter_rejected_candidates(export, source_candidates, rejected_candidates)
    source_hidden = _hidden_rejected_candidate_count(export, source_candidates, rejected_candidates)
    indexed_rows = _filter_rejected_candidates(export, indexed_candidates, rejected_candidates)
    indexed_hidden = _hidden_rejected_candidate_count(export, indexed_candidates, rejected_candidates)
    combined_rows = _dedupe_candidate_rows(source_rows + indexed_rows)
    return _best_date_tier_rows(export, combined_rows), source_hidden + indexed_hidden


def reject_all_range_state_path(report_dir: Path) -> Path:
    return report_dir / REJECT_ALL_RANGE_STATE_FILENAME


def load_reject_all_range_state(report_dir: Path) -> dict[str, dict[str, object]]:
    path = reject_all_range_state_path(report_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return {}
    return {
        str(entry_id): dict(record)
        for entry_id, record in entries.items()
        if isinstance(record, dict)
    }


def record_reject_all_range_state(
    report_dir: Path,
    entry_id: str,
    *,
    last_search_range_days: int | None = None,
    rejected_all_range_days: int | None = None,
    manual_search_required: bool | None = None,
    message: str | None = None,
    photo_index_folder: str | Path | None = None,
) -> None:
    path = reject_all_range_state_path(report_dir)
    state = load_reject_all_range_state(report_dir)
    record = dict(state.get(entry_id, {}))
    if last_search_range_days is not None:
        record["last_search_range_days"] = int(last_search_range_days)
    if rejected_all_range_days is not None:
        record["rejected_all_range_days"] = int(rejected_all_range_days)
    if manual_search_required is not None:
        record["manual_search_required"] = bool(manual_search_required)
    if message is not None:
        record["message"] = message
    if photo_index_folder is not None:
        record["photo_index_folder"] = _normalized_range_photo_index_folder(photo_index_folder)
    record["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    state[entry_id] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "entries": state}, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def clear_reject_all_range_state(report_dir: Path, entry_id: str) -> None:
    path = reject_all_range_state_path(report_dir)
    state = load_reject_all_range_state(report_dir)
    if entry_id not in state:
        return
    state.pop(entry_id, None)
    if not state:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "entries": state}, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _auto_expanded_reject_all_candidates(
    canonical_root: Path,
    photo_index_path: Path | None,
    exports: list[dict[str, object]],
    use_photo_index_fallback: bool,
    reject_all_range_state: dict[str, dict[str, object]],
    indexed_fallback_limit: int | None,
    photo_index_folder: Path | None,
    rejected_candidates: dict[str, set[tuple[str, str]]],
    known_candidate_filters: dict[tuple[str, str, str], str],
) -> dict[str, list[dict[str, object]]]:
    if not use_photo_index_fallback:
        return {}
    entry_by_date_by_range: dict[int, dict[str, list[dict[str, object]]]] = {}
    for export in exports:
        entry_id = str(export["entry_id"])
        record = reject_all_range_state.get(entry_id, {})
        if not _range_state_matches_photo_index_folder(record, photo_index_folder):
            continue
        next_range = _next_auto_expand_range_days(record)
        if next_range is None:
            continue
        entry_by_date_by_range.setdefault(next_range, {}).setdefault(str(export["entry_date"]), []).append(export)
    if not entry_by_date_by_range:
        return {}

    index_path = photo_index_path or default_index_db(canonical_root)
    expanded_by_entry: dict[str, list[dict[str, object]]] = {}
    for range_days, exports_by_date in entry_by_date_by_range.items():
        candidates_by_date = query_index_candidates(
            index_path,
            set(exports_by_date),
            limit_per_date=indexed_fallback_limit,
            max_distance_days=range_days,
            folder_root=photo_index_folder,
        )
        hashed_by_date = {
            entry_date: _hash_candidate_rows(candidates)
            for entry_date, candidates in candidates_by_date.items()
        }
        for entry_date, date_exports in exports_by_date.items():
            candidates = _mark_auto_range_candidates(
                hashed_by_date.get(entry_date, []),
                range_days,
                photo_index_folder=photo_index_folder,
            )
            for export in date_exports:
                filtered, _hidden_count = _candidate_rows_for_export(
                    export,
                    [],
                    candidates,
                    rejected_candidates,
                    known_candidate_filters,
                )
                expanded_by_entry[str(export["entry_id"])] = filtered
    return expanded_by_entry


def _mark_auto_range_candidates(
    candidates: list[dict[str, object]],
    range_days: int,
    *,
    photo_index_folder: Path | None = None,
) -> list[dict[str, object]]:
    rows = []
    for candidate in candidates:
        row = dict(candidate)
        evidence = str(row.get("evidence", "")).strip(";")
        scope_evidence = "auto_range_scope_folder" if photo_index_folder else "auto_range_scope_whole_index"
        auto_evidence = f"auto_range_{range_days}_days;{scope_evidence}"
        row["evidence"] = f"{evidence};{auto_evidence}" if evidence else auto_evidence
        rows.append(row)
    return rows


def _normalized_range_photo_index_folder(photo_index_folder: str | Path | None) -> str:
    folder_text = str(photo_index_folder or "").strip()
    if not folder_text:
        return ""
    return str(Path(folder_text).expanduser())


def _range_state_matches_photo_index_folder(
    record: dict[str, object],
    photo_index_folder: Path | None,
) -> bool:
    current_folder = _normalized_range_photo_index_folder(photo_index_folder)
    recorded_folder = _normalized_range_photo_index_folder(record.get("photo_index_folder", ""))
    if current_folder:
        return recorded_folder == current_folder
    return not recorded_folder


def _next_auto_expand_range_days(record: dict[str, object]) -> int | None:
    if not record:
        return None
    try:
        rejected_range = int(str(record.get("rejected_all_range_days", "")).strip())
    except ValueError:
        return None
    if rejected_range >= AUTO_EXPAND_MAX_RANGE_DAYS:
        return None
    for range_days in AUTO_EXPAND_RANGE_DAYS:
        if range_days > rejected_range:
            return range_days
    return None


def _manual_search_required_entry_ids(
    exports: list[dict[str, object]],
    reject_all_range_state: dict[str, dict[str, object]],
) -> set[str]:
    entry_ids = set()
    for export in exports:
        entry_id = str(export["entry_id"])
        record = reject_all_range_state.get(entry_id, {})
        try:
            rejected_range = int(str(record.get("rejected_all_range_days", "")).strip())
        except ValueError:
            continue
        if rejected_range >= AUTO_EXPAND_MAX_RANGE_DAYS:
            entry_ids.add(entry_id)
    return entry_ids


def _best_date_tier_rows(
    export: dict[str, object],
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    visible_rows = [row for row in candidates if not _is_hidden_candidate_row(row)]
    distances = [
        (row, _candidate_capture_date_distance(row, str(export["entry_date"])))
        for row in visible_rows
    ]
    known_distances = [distance for _row, distance in distances if distance is not None]
    if not known_distances:
        return candidates
    included_visible_ids: set[int] = set()
    for distance in sorted(set(known_distances)):
        tier_rows = [row for row, row_distance in distances if row_distance == distance]
        included_visible_ids.update(id(row) for row in tier_rows)
        if any(not _is_png_candidate(row) for row in tier_rows):
            break
    return [
        row
        for row in candidates
        if _is_hidden_candidate_row(row) or id(row) in included_visible_ids
    ]


def _candidate_capture_date_distance(
    candidate: dict[str, object],
    entry_date: str,
) -> int | None:
    target = dt.date.fromisoformat(entry_date)
    distances = []
    for field in ("filename_dates", "media_creation_dates"):
        for value in str(candidate.get(field, "")).split(";"):
            try:
                candidate_date = dt.date.fromisoformat(value.strip())
            except ValueError:
                continue
            distances.append(abs((candidate_date - target).days))
    return min(distances) if distances else None


def _is_png_candidate(candidate: dict[str, object]) -> bool:
    path = str(candidate.get("candidate_path", "") or candidate.get("candidate_filename", ""))
    return Path(path).suffix.lower() == ".png" or str(candidate.get("mime_type", "")).lower() == "image/png"


def _apply_known_candidate_filters(
    export: dict[str, object],
    candidates: list[dict[str, object]],
    known_candidate_filters: dict[tuple[str, str, str], str],
) -> list[dict[str, object]]:
    if not known_candidate_filters:
        return candidates
    entry_id = str(export["entry_id"])
    rows = []
    for candidate in candidates:
        row = dict(candidate)
        reason = known_candidate_filters.get(
            _candidate_filter_key(
                entry_id,
                str(candidate.get("candidate_sha256", "")),
                str(candidate.get("candidate_path", "")),
            )
        )
        if reason:
            row["candidate_filter_reason"] = reason
        rows.append(row)
    return rows


def _indexed_fallback_candidates(
    canonical_root: Path,
    photo_index_path: Path | None,
    target_dates: set[str],
    use_photo_index_fallback: bool,
    indexed_fallback_limit: int | None,
    photo_index_folder: Path | None,
) -> dict[str, list[dict[str, object]]]:
    if not use_photo_index_fallback or not target_dates:
        return {date: [] for date in target_dates}
    if indexed_fallback_limit is not None and indexed_fallback_limit <= 0:
        raise ValueError("indexed-fallback-limit must be positive")
    if photo_index_folder is not None:
        if not photo_index_folder.exists():
            raise FileNotFoundError(f"Missing photo index folder: {photo_index_folder}")
        if not photo_index_folder.is_dir():
            raise ValueError(f"Photo index folder is not a folder: {photo_index_folder}")
    index_path = photo_index_path or default_index_db(canonical_root)
    raw_candidates = query_index_candidates(
        index_path,
        target_dates,
        limit_per_date=indexed_fallback_limit,
        folder_root=photo_index_folder,
    )
    return {
        entry_date: _hash_candidate_rows(candidates)
        for entry_date, candidates in raw_candidates.items()
    }


def _hash_candidate_rows(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for candidate in candidates:
        candidate_path = Path(str(candidate["candidate_path"]))
        try:
            row = dict(candidate)
            row["candidate_sha256"] = str(candidate.get("candidate_sha256", "")).strip() or _sha256_file(candidate_path)
            row["byte_size"] = candidate.get("byte_size", "")
            if row["byte_size"] in (None, ""):
                row["byte_size"] = candidate_path.stat().st_size
        except OSError:
            continue
        rows.append(row)
    return rows


def _scan_search_roots(
    search_roots: list[Path],
    target_dates: set[str],
    scan_metadata_dates: bool,
) -> dict[str, list[dict[str, object]]]:
    expanded_dates = {
        nearby_date
        for target_date in target_dates
        for tier in _candidate_date_tiers(target_date)
        for nearby_date in tier
    }
    candidates_by_matching_date: dict[str, list[dict[str, object]]] = {
        date: [] for date in expanded_dates
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as executor:
        pending: set[concurrent.futures.Future[list[tuple[str, dict[str, object]]]]] = set()
        max_pending = MAX_SCAN_WORKERS * 4
        for path in _iter_image_paths(search_roots):
            pending.add(
                executor.submit(_candidate_rows_for_path, path, expanded_dates, scan_metadata_dates)
            )
            if len(pending) >= max_pending:
                _drain_completed_candidate_futures(pending, candidates_by_matching_date)
        while pending:
            _drain_completed_candidate_futures(pending, candidates_by_matching_date)
    candidates_by_date: dict[str, list[dict[str, object]]] = {}
    for target_date in target_dates:
        candidates_by_date[target_date] = _first_nonempty_candidate_tier(
            target_date,
            candidates_by_matching_date,
        )
    return candidates_by_date


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


def _first_nonempty_candidate_tier(
    target_date: str,
    candidates_by_matching_date: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    target = dt.date.fromisoformat(target_date)
    candidates_by_path: dict[str, dict[str, object]] = {}
    for tier in _candidate_date_tiers(target_date):
        for matching_date in tier:
            distance = abs((dt.date.fromisoformat(matching_date) - target).days)
            for candidate in candidates_by_matching_date.get(matching_date, []):
                candidate_path = str(candidate["candidate_path"])
                if candidate_path in candidates_by_path:
                    continue
                row = dict(candidate)
                if distance:
                    row["evidence"] = f'{row["evidence"]};date_within_{distance}_days'
                candidates_by_path[candidate_path] = row
        if candidates_by_path and any(not _is_png_candidate(row) for row in candidates_by_path.values()):
            break
    return sorted(candidates_by_path.values(), key=lambda row: str(row["candidate_path"]))


def _iter_image_paths(search_roots: list[Path]) -> Iterator[Path]:
    for root in search_roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                yield path


def _drain_completed_candidate_futures(
    pending: set[concurrent.futures.Future[list[tuple[str, dict[str, object]]]]],
    candidates_by_date: dict[str, list[dict[str, object]]],
) -> None:
    done, remaining = concurrent.futures.wait(
        pending,
        return_when=concurrent.futures.FIRST_COMPLETED,
    )
    pending.clear()
    pending.update(remaining)
    for future in done:
        for date, candidate in future.result():
            candidates_by_date[date].append(candidate)


def _candidate_rows_for_path(
    path: Path,
    target_dates: set[str],
    scan_metadata_dates: bool,
) -> list[tuple[str, dict[str, object]]]:
    try:
        filename_dates = _filename_dates(path)
        filename_timestamps = photo_index._filename_timestamps(path)
        filename_timestamp = min(filename_timestamps) if filename_timestamps else ""
        embedded_capture = _embedded_capture_timestamp(path) if scan_metadata_dates else ("", "")
        capture_timestamp, capture_timestamp_source = embedded_capture
        if not capture_timestamp and filename_timestamp:
            capture_timestamp = filename_timestamp
            capture_timestamp_source = "filename_timestamp"
        media_dates = _media_creation_dates(path) if scan_metadata_dates else set()
        if capture_timestamp and capture_timestamp_source != "filename_timestamp":
            media_dates = set(media_dates)
            media_dates.add(capture_timestamp[:10])
        filesystem_dates = _filesystem_dates(path)
        matching_dates = (filename_dates | media_dates) & target_dates
        if not matching_dates:
            return []
        sha256 = _sha256_file(path)
        byte_size = path.stat().st_size
    except OSError:
        return []
    rows = []
    for date in sorted(matching_dates):
        evidence = []
        if date in filename_dates:
            evidence.append("filename_date")
        if date in media_dates:
            evidence.append("media_creation_date")
        rows.append(
            (
                date,
                {
                    "candidate_path": str(path),
                    "candidate_filename": path.name,
                    "candidate_sha256": sha256,
                    "byte_size": byte_size,
                    "mime_type": _mime_type(path),
                    "filename_dates": ";".join(sorted(filename_dates)),
                    "media_creation_dates": ";".join(sorted(media_dates)),
                    "filesystem_dates": ";".join(sorted(filesystem_dates)),
                    "capture_timestamp": capture_timestamp,
                    "capture_timestamp_source": capture_timestamp_source,
                    "evidence": ";".join(evidence),
                },
            )
        )
    return rows


def _queue_row(export: dict[str, object], candidate: dict[str, object]) -> dict[str, object]:
    return {
        "entry_id": export["entry_id"],
        "entry_date": export["entry_date"],
        "project365_media_asset_id": export["project365_media_asset_id"],
        "current_match_status": export.get("current_match_status", ""),
        "current_decision": export.get("current_decision", ""),
        "candidate_path": candidate["candidate_path"],
        "candidate_filename": candidate["candidate_filename"],
        "candidate_sha256": candidate["candidate_sha256"],
        "byte_size": candidate["byte_size"],
        "mime_type": candidate["mime_type"],
        "filename_dates": candidate["filename_dates"],
        "media_creation_dates": candidate["media_creation_dates"],
        "filesystem_dates": candidate["filesystem_dates"],
        "capture_timestamp": candidate.get("capture_timestamp", ""),
        "capture_timestamp_source": candidate.get("capture_timestamp_source", ""),
        "gps_latitude": candidate.get("gps_latitude", ""),
        "gps_longitude": candidate.get("gps_longitude", ""),
        "gps_source": candidate.get("gps_source", ""),
        "has_gps": candidate.get("has_gps", ""),
        "date_distance": candidate.get("date_distance", ""),
        "evidence": candidate["evidence"],
        "candidate_filter_reason": candidate.get("candidate_filter_reason", ""),
        "review_decision": "",
        "review_notes": "",
    }


def _empty_queue_row(export: dict[str, object]) -> dict[str, object]:
    row = {field: "" for field in _search_queue_fieldnames()}
    row.update(
        {
            "entry_id": export["entry_id"],
            "entry_date": export["entry_date"],
            "project365_media_asset_id": export["project365_media_asset_id"],
            "current_match_status": export.get("current_match_status", ""),
            "current_decision": export.get("current_decision", ""),
            "review_decision": "search_needed",
        }
    )
    return row


def _manual_search_required_queue_row(export: dict[str, object]) -> dict[str, object]:
    row = _empty_queue_row(export)
    row["evidence"] = "manual_search_required;auto_range_15_exhausted"
    row["review_notes"] = MANUAL_SEARCH_REQUIRED_MESSAGE
    return row


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


def _media_creation_dates(path: Path) -> set[str]:
    return _jpeg_exif_dates(path) | _spotlight_content_creation_dates(path)


def _embedded_capture_timestamp(path: Path) -> tuple[str, str]:
    try:
        return photo_index._exiftool_capture_metadata([path]).get(str(path.resolve()), ("", ""))
    except RuntimeError:
        return "", ""


def _jpeg_exif_dates(path: Path) -> set[str]:
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return set()
    try:
        data = path.read_bytes()
    except OSError:
        return set()
    dates = set()
    offset = 2
    if data[:2] != b"\xff\xd8":
        return dates
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            break
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            break
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        segment = data[offset + 2 : offset + segment_length]
        offset += segment_length
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            dates |= _tiff_dates(segment[6:])
    return dates


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


def _spotlight_content_creation_dates(path: Path) -> set[str]:
    try:
        result = subprocess.run(
            [
                "mdls",
                "-raw",
                "-name",
                "kMDItemContentCreationDate",
                str(path),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    dates = set()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "(null)":
            continue
        candidate = stripped[:10]
        try:
            dates.add(dt.date.fromisoformat(candidate).isoformat())
        except ValueError:
            pass
    return dates


def _filesystem_dates(path: Path) -> set[str]:
    dates = set()
    try:
        stat = path.stat()
    except OSError:
        return dates
    for timestamp in (getattr(stat, "st_birthtime", None), stat.st_mtime):
        if timestamp is None:
            continue
        dates.add(dt.datetime.fromtimestamp(timestamp).date().isoformat())
    return dates


def _external_decision_id(entry_id: str, sha256: str, role: str) -> str:
    return f"{entry_id}:{role}:{sha256[:16]}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_csv_with_fieldnames(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)


def _append_csv_row(path: Path, row: dict[str, object], fieldnames: list[str]) -> None:
    write_header = not path.exists() or path.stat().st_size == 0
    if path.exists() and not write_header:
        existing_fieldnames, existing_rows = _read_csv_with_fieldnames(path)
        if existing_fieldnames != fieldnames:
            merged_fieldnames = list(existing_fieldnames)
            for field in fieldnames:
                if field not in merged_fieldnames:
                    merged_fieldnames.append(field)
            rows: list[dict[str, object]] = [dict(existing_row) for existing_row in existing_rows]
            rows.append(row)
            _write_csv(path, rows, merged_fieldnames)
            return
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def _search_queue_fieldnames() -> list[str]:
    return [
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
        "gps_latitude",
        "gps_longitude",
        "gps_source",
        "has_gps",
        "date_distance",
        "evidence",
        "candidate_filter_reason",
        "review_decision",
        "review_notes",
        "associated_entry_date",
        "associated_date_source",
        *_review_crop_fieldnames(),
    ]


def _alignment_queue_fieldnames() -> list[str]:
    return [
        "alignment_score",
        "alignment_confidence",
        "alignment_crop",
        "alignment_reference_size",
        "alignment_candidate_size",
        "alignment_error",
    ]


def _review_crop_fieldnames() -> list[str]:
    return [
        "review_crop_x",
        "review_crop_y",
        "review_crop_size",
        "review_crop_candidate_width",
        "review_crop_candidate_height",
        "review_crop_source",
        "review_crop_fill_color",
        "review_crop_rotation_degrees",
    ]


def _review_crop_from_row(row: dict[str, str]) -> dict[str, object] | None:
    values: dict[str, int] = {}
    for key, field in [
        ("x", "review_crop_x"),
        ("y", "review_crop_y"),
        ("size", "review_crop_size"),
        ("candidate_width", "review_crop_candidate_width"),
        ("candidate_height", "review_crop_candidate_height"),
    ]:
        try:
            value = int(str(row.get(field, "")).strip())
        except ValueError:
            return None
        if key not in {"x", "y"} and value <= 0:
            return None
        values[key] = value
    if (
        values["x"] >= values["candidate_width"]
        or values["y"] >= values["candidate_height"]
        or values["x"] + values["size"] <= 0
        or values["y"] + values["size"] <= 0
    ):
        return None
    result: dict[str, object] = {
        **values,
        "source": row.get("review_crop_source", "").strip() or "manual",
        "unit": "source_pixels",
        "shape": "square",
    }
    fill_color = _normalized_fill_color(row.get("review_crop_fill_color", ""))
    if fill_color:
        result["fill_color"] = fill_color
    rotation_degrees = _normalized_rotation_degrees(row.get("review_crop_rotation_degrees", ""))
    if rotation_degrees:
        result["rotation_degrees"] = rotation_degrees
    return result


def _normalized_fill_color(value: object) -> str:
    text = str(value or "").strip()
    if re.match(r"^#[0-9a-fA-F]{6}$", text):
        return text.lower()
    return ""


def _normalized_rotation_degrees(value: object) -> float:
    try:
        number = float(str(value or "0"))
    except (TypeError, ValueError):
        return 0.0
    if not -180 <= number <= 180:
        return 0.0
    return round(number, 3)


def _group_fieldnames() -> list[str]:
    return [
        "entry_date",
        "entry_count",
        "candidate_count",
        "hidden_rejected_candidate_count",
        "hidden_export_equivalent_candidate_count",
        "status",
    ]


def _batch_plan_fieldnames() -> list[str]:
    return [
        "batch_id",
        "start_date",
        "end_date",
        "date_count",
        "entry_count",
        "candidate_count",
        "review_date_count",
        "folder_needed_date_count",
        "hidden_rejected_candidate_count",
        "hidden_export_equivalent_candidate_count",
        "statuses",
        "entry_dates",
        "entry_ids",
        "control_app_start_date",
        "control_app_end_date",
        "recommended_action",
    ]


def _search_attempt_fieldnames() -> list[str]:
    return [
        "attempt_id",
        "started_at",
        "finished_at",
        "search_roots",
        "target_entry_ids",
        "target_entry_dates",
        "start_date",
        "end_date",
        "max_targets",
        "scan_metadata_dates",
        "merge_existing_queue",
        "replace_existing_queue",
        "include_low_quality_matches",
        "unclear_entry_count",
        "candidate_count",
        "hidden_rejected_candidate_count",
        "hidden_export_equivalent_candidate_count",
        "search_queue_path",
        "group_report_path",
        "batch_plan_path",
    ]


def _parse_json_object(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _image_dimensions_tuple(path: Path | None) -> tuple[int, int] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = path.read_bytes()[:128 * 1024]
    except OSError:
        return None
    try:
        dimensions = _parse_image_dimensions(payload)
    except (OSError, ValueError, struct.error):
        return None
    if dimensions is None:
        return None
    width, height = dimensions
    if width <= 0 or height <= 0:
        return None
    return width, height


def _dimensions_are_low_quality(dimensions: tuple[int, int] | None) -> bool:
    if dimensions is None:
        return False
    width, height = dimensions
    return width < LOW_QUALITY_ORIGINAL_MIN_AXIS_PX or height < LOW_QUALITY_ORIGINAL_MIN_AXIS_PX


def _parse_image_dimensions(payload: bytes) -> tuple[int, int] | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
        return struct.unpack(">II", payload[16:24])
    if payload.startswith((b"GIF87a", b"GIF89a")) and len(payload) >= 10:
        return struct.unpack("<HH", payload[6:10])
    if payload.startswith(b"8BPS") and len(payload) >= 26:
        height = struct.unpack(">I", payload[14:18])[0]
        width = struct.unpack(">I", payload[18:22])[0]
        return width, height
    if payload.startswith(b"BM") and len(payload) >= 26:
        width = abs(struct.unpack_from("<i", payload, 18)[0])
        height = abs(struct.unpack_from("<i", payload, 22)[0])
        return width, height
    if payload.startswith(b"\xff\xd8"):
        return _parse_jpeg_dimensions(payload)
    return None


def _parse_jpeg_dimensions(payload: bytes) -> tuple[int, int] | None:
    offset = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset + 3 < len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return None
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(payload):
            return None
        segment_length = struct.unpack(">H", payload[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(payload):
            return None
        if marker in sof_markers and segment_length >= 7:
            height = struct.unpack(">H", payload[offset + 3 : offset + 5])[0]
            width = struct.unpack(">H", payload[offset + 5 : offset + 7])[0]
            return width, height
        offset += segment_length
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
