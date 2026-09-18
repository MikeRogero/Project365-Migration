#!/usr/bin/env python3
"""Serve a local visual picker for Project365 original-photo review."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import urllib.parse
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from project365_crop_align import suggest_crop
from project365_original_matcher import IMAGE_EXTENSIONS
from project365_photo_library_index import (
    ExiftoolPhotoMetadata,
    _exiftool_capture_metadata,
    _exiftool_photo_metadata,
    default_index_db,
    query_index_candidates,
)
from project365_original_reference_pipeline import (
    ACCEPT_DECISIONS,
    ASSOCIATED_PHOTO_DECISIONS,
    FALLBACK_DECISIONS,
    HIDDEN_CANDIDATE_FILTER_REASONS,
    MANUAL_SEARCH_REQUIRED_MESSAGE,
    ORIGINAL_PICKER_TARGET_ROLES,
    REJECT_DECISIONS,
    _external_decision_id,
    apply_reviewed_external_references,
    build_external_original_search_queue,
    clear_reject_all_range_state,
    load_reject_all_range_state,
    prune_applied_review_queue,
    record_reject_all_range_state,
    refresh_external_original_queue_reports,
)
from project365_visual_ranker import (
    DEFAULT_LIKELY_LIMIT,
    DEFAULT_OVERSIZED_THRESHOLD,
    METHOD_VERSION as VISUAL_METHOD_VERSION,
    VisualDescriptorCache,
    default_cache_path,
    rank_candidate_rows,
    visual_fieldnames,
)


DEFAULT_QUEUE = "Project365Canonical/exports/verification_reports/original_photo_external_search_queue.csv"
DEFAULT_BATCH_PLAN = "Project365Canonical/exports/verification_reports/original_photo_search_batch_plan.csv"
MAX_DROP_BYTES = 250 * 1024 * 1024
DEFAULT_ENTRY_PAGE_SIZE = 20
DEFAULT_CANDIDATE_PAGE_SIZE = 8
MIN_PREVIEW_SIZE = 64
MAX_PREVIEW_SIZE = 2048
EXPAND_DATE_RANGE_DAYS = {1, 3, 5, 15, 30}
MAX_MANUAL_INDEX_DATE_SEARCH_DAYS = 366
SEARCH_RANGE_EVIDENCE_RE = re.compile(r"(?:manual_range|auto_range|date_within)_(\d+)_days")
BUTTON_RANGE_EVIDENCE_RE = re.compile(r"(?:manual_range|auto_range)_(\d+)_days")
APPLY_DECISIONS_CONFIRM_TOKEN = "apply-reviewed-decisions"
PROJECT_ORIGINALS_SOURCE_PARTS = ("Source Data", "Original Photos matching Project365 Entries")
PROJECT_ORIGINALS_SOURCE_RELATIVE_PATH = Path(*PROJECT_ORIGINALS_SOURCE_PARTS)
PROJECT_ADDITION_FILENAME_MARKER = "Project365 project file addition"
PROJECT_ADDITION_DROP_TIME = dt.time(0, 0, 1)
DROP_CONVERT_TO_HEIC_EXTENSIONS = {".avif", ".avifs", ".webp"}
DROP_IMAGE_EXTENSIONS = IMAGE_EXTENSIONS | DROP_CONVERT_TO_HEIC_EXTENSIONS
VIDEO_DROP_EXTENSIONS = {".mov", ".mp4", ".m4v"}
VIDEO_DROP_CONTENT_TYPE_SUFFIXES = {
    "video/quicktime": ".mov",
    "video/mp4": ".mp4",
    "video/x-m4v": ".m4v",
}
MAX_VIDEO_DROP_BYTES = 2 * 1024 * 1024 * 1024
VIDEO_FRAME_COUNT = 16


def _sql_in_placeholders(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def project_originals_source_path(canonical_root: Path) -> Path:
    return (Path(canonical_root).parent / PROJECT_ORIGINALS_SOURCE_RELATIVE_PATH).resolve()


def video_drop_suffix(filename: str, content_type: str = "") -> str:
    suffix = Path(str(filename).replace("\\", "/")).suffix.lower()
    if suffix in VIDEO_DROP_EXTENSIONS:
        return suffix
    normalized_type = str(content_type).split(";", 1)[0].strip().lower()
    return VIDEO_DROP_CONTENT_TYPE_SUFFIXES.get(normalized_type, "")


def video_drop_filename(filename: str, content_type: str = "") -> str:
    safe_name = Path(str(filename).replace("\\", "/")).name
    suffix = video_drop_suffix(safe_name, content_type)
    if not suffix:
        return safe_name or "dropped-video"
    if Path(safe_name).suffix.lower() in VIDEO_DROP_EXTENSIONS:
        return safe_name
    stem = Path(safe_name).stem if safe_name else ""
    return f"{stem or 'dropped-video'}{suffix}"


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _remove_tree_in_background(path: Path) -> None:
    def cleanup() -> None:
        shutil.rmtree(path, ignore_errors=True)

    threading.Thread(
        target=cleanup,
        name="project365-picker-video-cleanup",
        daemon=True,
    ).start()


def _rows_for_fieldnames(rows: list[dict[str, str]], fieldnames: list[str]) -> list[dict[str, str]]:
    return [{field: row.get(field, "") for field in fieldnames} for row in rows]


def project_originals_source_folder(canonical_root: Path) -> str:
    folder = project_originals_source_path(canonical_root)
    return str(folder) if folder.is_dir() else ""


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


@dataclass(frozen=True)
class PickerConfig:
    canonical_root: Path
    queue_path: Path
    matcher_review_queue_path: Path | None = None
    batch_plan_path: Path | None = None


class PickerQueueShards:
    SHARD_COUNT = 20
    SUMMARY_VERSION = 2

    def __init__(self, queue_path: Path, canonical_root: Path):
        self.queue_path = queue_path
        self.root = canonical_root / "cache" / "picker_queue_shards"
        self.manifest_path = self.root / "manifest.json"
        self._manifest: dict[str, Any] | None = None
        self._entry_lookup: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def summaries(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            self._manifest = None
            self._entry_lookup = {}
            return []
        self._ensure()
        return [dict(entry) for entry in self._manifest.get("entries", [])]

    def entry_rows(self, entry_id: str) -> list[dict[str, str]]:
        if not self.queue_path.exists():
            return []
        self._ensure()
        entry = self._entry_lookup.get(entry_id)
        if entry is None:
            return []
        shard_path = self.root / self._manifest["build_dir"] / entry["shard"]
        rows: list[dict[str, str]] = []
        with shard_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("entry_id") == entry_id:
                    rows.append(row)
        return rows

    def invalidate(self) -> None:
        self._manifest = None
        self._entry_lookup = {}

    def _ensure(self) -> None:
        signature = self._signature()
        if self._manifest_matches(signature):
            return
        with self._lock:
            if self._manifest_matches(signature):
                return
            self._load_manifest()
            if self._manifest_matches(signature):
                return
            self._build(signature)

    def _signature(self) -> dict[str, int]:
        stat = self.queue_path.stat()
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "summary_version": self.SUMMARY_VERSION,
        }

    def _load_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        self._manifest = payload
        self._entry_lookup = {
            str(entry.get("entry_id", "")): entry
            for entry in payload.get("entries", [])
        }

    def _manifest_matches(self, signature: dict[str, int]) -> bool:
        if self._manifest is None:
            self._load_manifest()
        if self._manifest is None or self._manifest.get("source") != signature:
            return False
        build_dir = self.root / str(self._manifest.get("build_dir", ""))
        matches = all((build_dir / f"shard-{index:02d}.csv").exists() for index in range(self.SHARD_COUNT))
        if matches:
            self._remove_obsolete_builds(build_dir)
        return matches

    def _build(self, signature: dict[str, int]) -> None:
        build_dir_name = f"queue-{signature['size']}-{signature['mtime_ns']}"
        build_dir = self.root / build_dir_name
        build_dir.mkdir(parents=True, exist_ok=True)
        handles = []
        writers = []
        summary_rows_by_entry: dict[str, list[dict[str, str]]] = {}
        shard_by_entry: dict[str, int] = {}
        entry_order: list[str] = []
        try:
            with self.queue_path.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                fieldnames = list(reader.fieldnames or [])
                for index in range(self.SHARD_COUNT):
                    handle = (build_dir / f"shard-{index:02d}.csv").open("w", encoding="utf-8", newline="")
                    handles.append(handle)
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writers.append(writer)
                for row in reader:
                    entry_id = row.get("entry_id", "")
                    if entry_id not in summary_rows_by_entry:
                        entry_order.append(entry_id)
                        summary_rows_by_entry[entry_id] = []
                        shard_by_entry[entry_id] = (
                            int(hashlib.sha256(entry_id.encode("utf-8")).hexdigest()[:8], 16)
                            % self.SHARD_COUNT
                        )
                    shard_index = shard_by_entry[entry_id]
                    writers[shard_index].writerow(row)
                    summary_rows_by_entry[entry_id].append(row)
        finally:
            for handle in handles:
                handle.close()
        entries = [
            _queue_shard_entry_summary(entry_id, summary_rows_by_entry[entry_id], shard_by_entry[entry_id])
            for entry_id in entry_order
        ]
        payload = {
            "version": 1,
            "source": signature,
            "build_dir": build_dir_name,
            "shard_count": self.SHARD_COUNT,
            "entries": entries,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.manifest_path)
        self._manifest = payload
        self._entry_lookup = {entry["entry_id"]: entry for entry in entries}
        self._remove_obsolete_builds(build_dir)

    def _remove_obsolete_builds(self, current_build_dir: Path) -> None:
        for path in self.root.glob("queue-*"):
            if path.is_dir() and path != current_build_dir:
                shutil.rmtree(path, ignore_errors=True)


class PickerState:
    def __init__(self, config: PickerConfig):
        self.config = config
        self._lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._preview_lock = threading.Lock()
        self._crawl_jobs: dict[str, dict[str, Any]] = {}
        self._crop_estimate_jobs: dict[str, dict[str, Any]] = {}
        self._image_paths: dict[str, Path] = {}
        self._video_frame_sets: dict[str, dict[str, Any]] = {}
        self._entry_rows_cache: dict[str, list[dict[str, str]]] = {}
        self._candidate_facts_cache: dict[str, tuple[str, bool]] = {}
        self._dropped_photo_metadata_cache: dict[str, dict[str, str]] = {}
        self._added_candidate_rows: dict[str, list[dict[str, str]]] = {}
        self._replacement_candidate_rows: dict[str, list[dict[str, str]]] = {}
        self._decision_overrides = self._load_decision_overrides()
        self._crop_staging = self._load_crop_staging()
        self._queue_shards = PickerQueueShards(config.queue_path, config.canonical_root)
        self._source_media = self._load_source_media()
        self._completed_external_entry_ids_cache: set[str] | None = None
        self._completed_external_reference_entry_ids_cache: set[str] | None = None
        self._completed_queue_prune_signature: tuple[tuple[int, int] | None, tuple[int, int] | None] | None = None
        self._people_cache_signature: tuple[int, int] | None = None
        self._people_by_entry: dict[str, list[str]] = {}

    def summary(self) -> dict[str, Any]:
        self._prune_completed_queue_entries_if_needed()
        status_counts: dict[str, int] = {}
        entry_count = 0
        pending = {"accepted": 0, "rejected": 0, "associated": 0}
        pending_entries = {"accepted": 0, "rejected": 0, "associated": 0}
        affected_entry_ids = set(self._decision_overrides) | set(self._added_candidate_rows) | set(
            self._replacement_candidate_rows
        )
        seen_entry_ids: set[str] = set()
        queue_exists = self.config.queue_path.exists()
        for record in self._queue_shards.summaries():
            seen_entry_ids.add(record["entry_id"])
            if record["entry_id"] in affected_entry_ids:
                entry_rows = self._entry_rows(record["entry_id"])
                status = _entry_status(entry_rows)
                accepted_count = sum(
                    row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                    for row in entry_rows
                )
                rejected_count = sum(
                    row.get("review_decision", "").strip().lower() in REJECT_DECISIONS
                    for row in entry_rows
                )
                associated_count = sum(
                    row.get("review_decision", "").strip().lower() in ASSOCIATED_PHOTO_DECISIONS
                    for row in entry_rows
                )
            else:
                status = record["status"]
                accepted_count = int(record.get("accepted_count", 0))
                rejected_count = int(record.get("rejected_count", 0))
                associated_count = int(record.get("associated_count", 0))
            status_counts[status] = status_counts.get(status, 0) + 1
            entry_count += 1
            pending["accepted"] += accepted_count
            pending["rejected"] += rejected_count
            pending["associated"] += associated_count
            if accepted_count:
                pending_entries["accepted"] += 1
            if rejected_count:
                pending_entries["rejected"] += 1
            if associated_count:
                pending_entries["associated"] += 1
        if not queue_exists:
            for entry in self._database_entry_summaries(set(), set(), missing_links_only=True):
                entry_id = entry["entry_id"]
                if entry_id in seen_entry_ids:
                    continue
                status = str(entry.get("status", "search_needed"))
                status_counts[status] = status_counts.get(status, 0) + 1
                entry_count += 1
                seen_entry_ids.add(entry_id)
        for entry in self._database_entry_summaries(set(), set(), fallback_only=True):
            entry_id = entry["entry_id"]
            if entry_id in seen_entry_ids:
                continue
            status = str(entry.get("status", "fallback"))
            entry_rows = self._entry_rows(entry_id)
            accepted_count = sum(
                row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                for row in entry_rows
            )
            rejected_count = sum(
                row.get("review_decision", "").strip().lower() in REJECT_DECISIONS
                for row in entry_rows
            )
            associated_count = sum(
                row.get("review_decision", "").strip().lower() in ASSOCIATED_PHOTO_DECISIONS
                for row in entry_rows
            )
            status_counts[status] = status_counts.get(status, 0) + 1
            entry_count += 1
            pending["accepted"] += accepted_count
            pending["rejected"] += rejected_count
            pending["associated"] += associated_count
            if accepted_count:
                pending_entries["accepted"] += 1
            if rejected_count:
                pending_entries["rejected"] += 1
            if associated_count:
                pending_entries["associated"] += 1
            seen_entry_ids.add(entry_id)
        for entry_id in sorted(affected_entry_ids - seen_entry_ids):
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                continue
            status = _entry_status(entry_rows)
            accepted_count = sum(
                row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                for row in entry_rows
            )
            rejected_count = sum(
                row.get("review_decision", "").strip().lower() in REJECT_DECISIONS
                for row in entry_rows
            )
            associated_count = sum(
                row.get("review_decision", "").strip().lower() in ASSOCIATED_PHOTO_DECISIONS
                for row in entry_rows
            )
            status_counts[status] = status_counts.get(status, 0) + 1
            entry_count += 1
            pending["accepted"] += accepted_count
            pending["rejected"] += rejected_count
            pending["associated"] += associated_count
            if accepted_count:
                pending_entries["accepted"] += 1
            if rejected_count:
                pending_entries["rejected"] += 1
            if associated_count:
                pending_entries["associated"] += 1
        return {
            "entry_count": entry_count,
            "status_counts": status_counts,
            "pending_decisions": pending,
            "pending_entry_counts": pending_entries,
            "pending_crop_commits": self.pending_crop_commits(),
            "queue_path": str(self.config.queue_path),
            "queue_exists": queue_exists,
            "default_photo_index_folder": project_originals_source_folder(self.config.canonical_root),
        }

    def batches(self) -> dict[str, Any]:
        self._prune_completed_queue_entries_if_needed()
        batch_path = self._batch_plan_path()
        if not batch_path.exists():
            return {"exists": False, "batches": []}
        with batch_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        batches = []
        active_entry_ids = {str(record.get("entry_id", "")) for record in self._queue_shards.summaries()}
        archived_count = 0
        for row in rows:
            batch_entry_ids = set(_split_semicolon_list(row.get("entry_ids", "")))
            if batch_entry_ids and active_entry_ids and not batch_entry_ids.intersection(active_entry_ids):
                archived_count += 1
                continue
            batches.append(
                {
                    "batch_id": row.get("batch_id", ""),
                    "start_date": row.get("start_date", ""),
                    "end_date": row.get("end_date", ""),
                    "date_count": row.get("date_count", ""),
                    "entry_count": row.get("entry_count", ""),
                    "candidate_count": row.get("candidate_count", ""),
                    "review_date_count": row.get("review_date_count", ""),
                    "folder_needed_date_count": row.get("folder_needed_date_count", ""),
                    "hidden_rejected_candidate_count": row.get("hidden_rejected_candidate_count", ""),
                    "hidden_export_equivalent_candidate_count": row.get("hidden_export_equivalent_candidate_count", ""),
                    "statuses": row.get("statuses", ""),
                    "entry_dates": row.get("entry_dates", ""),
                    "entry_ids": row.get("entry_ids", ""),
                    "recommended_action": row.get("recommended_action", ""),
                }
            )
        batches.sort(key=_batch_sort_key)
        return {
            "exists": True,
            "batches": batches,
            "archived_count": archived_count,
            "total_count": len(rows),
        }

    def entries(
        self,
        status: str = "all",
        entry_ids: set[str] | None = None,
        entry_dates: set[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.entry_page(
            status=status,
            entry_ids=entry_ids,
            entry_dates=entry_dates,
            limit=limit,
            offset=offset,
        )["entries"]

    def entry_page(
        self,
        status: str = "all",
        entry_ids: set[str] | None = None,
        entry_dates: set[str] | None = None,
        limit: int | None = DEFAULT_ENTRY_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._prune_completed_queue_entries_if_needed()
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        entries: list[dict[str, Any]] = []
        matched_count = 0
        has_more = False
        affected_entry_ids = set(self._decision_overrides) | set(self._added_candidate_rows) | set(
            self._replacement_candidate_rows
        )
        reverse_entry_order = status in {"accepted_not_applied", "rejected"}
        if entry_ids or entry_dates:
            seen_entry_ids: set[str] = set()
            for record in sorted(self._queue_shards.summaries(), key=_entry_sort_key, reverse=reverse_entry_order):
                entry_id = str(record.get("entry_id", ""))
                if self._entry_has_completed_external_decision(entry_id):
                    continue
                if entry_ids and entry_id not in entry_ids:
                    continue
                if entry_dates and str(record.get("entry_date", "")) not in entry_dates:
                    continue
                if entry_id in affected_entry_ids:
                    entry = self._entry_summary(entry_id, self._entry_rows(entry_id))
                else:
                    entry = self._entry_summary_from_index(record)
                if not _entry_matches_status(entry, status):
                    continue
                entries.append(entry)
                seen_entry_ids.add(entry_id)
            for entry in self._database_entry_summaries(entry_ids, entry_dates):
                if entry["entry_id"] in seen_entry_ids:
                    continue
                if not _entry_matches_status(entry, status):
                    continue
                entries.append(entry)
                seen_entry_ids.add(entry["entry_id"])
            entries.sort(key=_entry_sort_key, reverse=reverse_entry_order)
            paged_entries = entries[offset:] if limit is None else entries[offset : offset + limit]
            return {
                "entries": paged_entries,
                "offset": offset,
                "limit": limit,
                "returned_count": len(paged_entries),
                "has_more": limit is not None and offset + len(paged_entries) < len(entries),
            }
        queue_exists = self.config.queue_path.exists()
        seen_entry_ids: set[str] = set()
        for record in sorted(self._queue_shards.summaries(), key=_entry_sort_key, reverse=reverse_entry_order):
            entry_id = str(record.get("entry_id", ""))
            if self._entry_has_completed_external_decision(entry_id):
                continue
            seen_entry_ids.add(entry_id)
            if entry_id in affected_entry_ids:
                entry = self._entry_summary(entry_id, self._entry_rows(entry_id))
            else:
                entry = self._entry_summary_from_index(record)
            if entry_ids and entry["entry_id"] not in entry_ids:
                continue
            if entry_dates and entry["entry_date"] not in entry_dates:
                continue
            if not _entry_matches_status(entry, status):
                continue
            if matched_count < offset:
                matched_count += 1
                continue
            if limit is not None and len(entries) >= limit:
                has_more = True
                break
            entries.append(entry)
            matched_count += 1
        if not queue_exists:
            for entry in self._database_entry_summaries(set(), set(), missing_links_only=True):
                if entry["entry_id"] in seen_entry_ids:
                    continue
                if not _entry_matches_status(entry, status):
                    continue
                if matched_count < offset:
                    matched_count += 1
                    continue
                if limit is not None and len(entries) >= limit:
                    has_more = True
                    break
                entries.append(entry)
                matched_count += 1
        if status in {"fallback", "all"}:
            for entry_id in self._database_entry_ids(set(), set(), fallback_only=True):
                if entry_id in seen_entry_ids:
                    continue
                if limit is not None and len(entries) >= limit:
                    has_more = True
                    break
                entry = self._database_entry_summary(entry_id, fallback_only=True)
                if entry is None:
                    continue
                if entry["entry_id"] in seen_entry_ids:
                    continue
                if not _entry_matches_status(entry, status):
                    continue
                if matched_count < offset:
                    matched_count += 1
                    continue
                if limit is not None and len(entries) >= limit:
                    has_more = True
                    break
                entries.append(entry)
                matched_count += 1
                seen_entry_ids.add(entry["entry_id"])
        current_affected_entry_ids = set(self._decision_overrides) | set(self._added_candidate_rows) | set(
            self._replacement_candidate_rows
        )
        for entry_id in sorted(current_affected_entry_ids - seen_entry_ids):
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                continue
            entry = self._entry_summary(entry_id, entry_rows)
            if not _entry_matches_status(entry, status):
                continue
            if matched_count < offset:
                matched_count += 1
                continue
            if limit is not None and len(entries) >= limit:
                has_more = True
                break
            entries.append(entry)
            matched_count += 1
            seen_entry_ids.add(entry_id)
        return {
            "entries": entries,
            "offset": offset,
            "limit": limit,
            "returned_count": len(entries),
            "has_more": has_more,
        }

    def entry_detail(
        self,
        entry_id: str,
        candidate_limit: int | None = None,
        candidate_offset: int = 0,
        rank_if_needed: bool = True,
        include_database: bool = False,
    ) -> dict[str, Any] | None:
        self._prune_completed_queue_entries_if_needed()
        if candidate_limit is not None and candidate_limit <= 0:
            raise ValueError("candidate_limit must be positive")
        if candidate_offset < 0:
            raise ValueError("candidate_offset must be zero or greater")
        has_local_pending_rows = self._entry_has_local_pending_rows(entry_id)
        if self._entry_has_completed_external_decision(entry_id, include_fallback=False) and not has_local_pending_rows:
            if include_database:
                return self._database_entry_detail(entry_id)
            return None
        entry_rows = self._entry_rows(entry_id)
        if not entry_rows:
            self._recover_project_addition_candidates(entry_id)
            entry_rows = self._entry_rows(entry_id)
        if not entry_rows:
            database_detail = self._database_entry_detail(entry_id)
            if (
                database_detail is not None
                and (include_database or not self.config.queue_path.exists() or database_detail.get("status") == "fallback")
            ):
                return database_detail
            return None
        entry_rows = self._ensure_default_filename_index_candidates(entry_id, entry_rows)
        if candidate_limit is None and rank_if_needed:
            entry_rows = self._rank_oversized_entry_if_needed(entry_id, entry_rows)
        entry_rows = self._with_database_confirmed_roles(entry_id, entry_rows)
        entry = self._entry_summary(entry_id, entry_rows)
        candidate_rows = [
            row
            for row in entry_rows
            if row.get("candidate_path", "").strip()
            and not _is_hidden_candidate_row(row)
            and row.get("review_decision", "").strip().lower() not in REJECT_DECISIONS
        ]
        candidate_rows.sort(key=_candidate_page_sort_key)
        candidate_total = len(candidate_rows)
        if candidate_limit is not None:
            candidate_rows = candidate_rows[candidate_offset : candidate_offset + candidate_limit]
        self._hydrate_candidate_rows_with_indexed_geolocation(candidate_rows)
        candidates = [self._candidate_detail(row) for row in candidate_rows]
        entry["candidates"] = candidates
        entry["candidate_total"] = candidate_total
        entry["candidate_offset"] = candidate_offset
        entry["candidate_limit"] = candidate_limit
        entry["candidate_has_more"] = candidate_offset + len(candidates) < candidate_total
        return entry

    def _ensure_default_filename_index_candidates(
        self,
        entry_id: str,
        entry_rows: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        if entry_id in self._replacement_candidate_rows:
            return entry_rows
        if not entry_rows:
            return entry_rows
        entry_date = entry_rows[0].get("entry_date", "")
        try:
            dt.date.fromisoformat(entry_date)
        except ValueError:
            return entry_rows
        target_dates = {entry_date}
        indexed = query_index_candidates(
            default_index_db(self.config.canonical_root),
            target_dates,
            filename_dates_only=True,
        ).get(entry_date, [])
        existing_paths = {
            row.get("candidate_path", "")
            for row in entry_rows
            if row.get("candidate_path", "")
        }
        rejected_hashes = self._rejected_candidate_hashes(entry_id)
        template = entry_rows[0]
        additions: list[dict[str, Any]] = []
        for candidate in indexed:
            path = Path(str(candidate.get("candidate_path", ""))).resolve()
            path_text = str(path)
            if not path_text or path_text in existing_paths or not path.exists():
                continue
            if not _candidate_filename_starts_with_any_date(candidate, target_dates):
                continue
            digest = str(candidate.get("candidate_sha256", "")).strip() or _sha256_path(path)
            if digest in rejected_hashes:
                continue
            byte_size = candidate.get("byte_size", "")
            if byte_size in (None, ""):
                byte_size = path.stat().st_size
            evidence_parts = [part for part in str(candidate.get("evidence", "")).strip(";").split(";") if part]
            evidence_parts.extend(
                [
                    "manual_default_search",
                    "manual_default_scope_filename_index",
                    "manual_default_date_source_filename_only",
                ]
            )
            additions.append(
                {
                    "entry_id": entry_id,
                    "entry_date": entry_date,
                    "project365_media_asset_id": template.get("project365_media_asset_id", ""),
                    "current_match_status": template.get("current_match_status", ""),
                    "current_decision": template.get("current_decision", ""),
                    "candidate_path": path_text,
                    "candidate_filename": candidate.get("candidate_filename", path.name),
                    "candidate_sha256": digest,
                    "byte_size": str(byte_size),
                    "mime_type": candidate.get("mime_type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"),
                    "filename_dates": candidate.get("filename_dates", ""),
                    "media_creation_dates": candidate.get("media_creation_dates", ""),
                    "filesystem_dates": candidate.get("filesystem_dates", ""),
                    "capture_timestamp": candidate.get("capture_timestamp", ""),
                    "capture_timestamp_source": candidate.get("capture_timestamp_source", ""),
                    "gps_latitude": candidate.get("gps_latitude", ""),
                    "gps_longitude": candidate.get("gps_longitude", ""),
                    "gps_source": candidate.get("gps_source", ""),
                    "has_gps": candidate.get("has_gps", ""),
                    "date_distance": candidate.get("date_distance", ""),
                    "evidence": ";".join(evidence_parts),
                    "candidate_filter_reason": candidate.get("candidate_filter_reason", ""),
                    "review_decision": "",
                    "review_notes": "",
                }
            )
            existing_paths.add(path_text)
        if not additions:
            return entry_rows
        self._store_added_candidates(entry_id, template, additions)
        return self._entry_rows(entry_id)

    def crop_entry_detail(self, entry_id: str, mark_estimated_viewed: bool = True) -> dict[str, Any] | None:
        entry_rows = self._entry_rows(entry_id)
        if entry_rows:
            entry = self._entry_summary(entry_id, entry_rows)
            candidates = [
                self._candidate_detail(row)
                for row in entry_rows
                if row.get("candidate_path", "").strip()
                and row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                and _candidate_path_is_available(row.get("candidate_path", ""))
            ]
            if candidates:
                candidates.sort(key=_candidate_sort_key)
                entry["candidates"] = candidates
                entry["crop_source_state"] = "accepted_not_applied"
                entry["crop_has_crop"] = _candidate_has_review_crop(candidates[0])
                return entry
        return self._database_crop_entry_detail(
            entry_id,
            mark_estimated_viewed=mark_estimated_viewed,
        )

    def crop_entries(
        self,
        crop_filter: str = "missing",
        entry_dates: set[str] | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        if crop_filter not in {"all", "missing", "with_crop", "estimated", "confirmed"}:
            raise ValueError("Unsupported crop filter")
        entries: list[dict[str, Any]] = []
        queued_entry_ids: set[str] = set()
        rows = self._read_rows()
        for entry_id, entry_rows in _group_by_entry(rows).items():
            accepted_rows = [
                row
                for row in entry_rows
                if row.get("candidate_path", "").strip()
                and row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                and _candidate_path_is_available(row.get("candidate_path", ""))
            ]
            if not accepted_rows:
                continue
            queued_entry_ids.add(entry_id)
            entry = self._entry_summary(entry_id, entry_rows)
            candidate = self._candidate_detail(accepted_rows[0])
            entries.append(self._crop_summary(entry, candidate, "accepted_not_applied"))
        entries.extend(self._database_crop_entries(exclude_entry_ids=queued_entry_ids))
        entries = [entry for entry in entries if _crop_filter_matches(entry, crop_filter)]
        entries = [
            entry
            for entry in entries
            if _crop_entry_in_date_scope(entry, entry_dates or set(), start_date, end_date)
        ]
        entries.sort(key=lambda entry: (entry.get("entry_date", ""), entry.get("entry_id", "")))
        return entries

    def pending_crop_commits(self) -> dict[str, Any]:
        pending = sum(
            1
            for candidates in self._crop_staging.values()
            for record in candidates.values()
            if self._staged_crop_commit_pending(record)
        )
        return {
            "pending_count": pending,
            "staging_path": str(self._crop_staging_path()),
        }

    def _candidate_detail(self, row: dict[str, str]) -> dict[str, Any]:
        path_text = row.get("candidate_path", "").strip()
        path = Path(path_text)
        metadata_values = self._metadata_values_for_manual_drop(row)
        database_role = row.get("database_confirmed_role", "").strip().lower()
        decision = row.get("review_decision", "").strip().lower()
        return {
            "path": path_text,
            "filename": row.get("candidate_filename", path.name),
            "folder_path": str(path.parent),
            "folder_label": _candidate_folder_label(path_text),
            "token": self._image_token(path),
            "exists": bool(path_text),
            "evidence": row.get("evidence", ""),
            "byte_size": row.get("byte_size", ""),
            "mime_type": row.get("mime_type", ""),
            "dimensions": "",
            "has_embedded_geolocation": _candidate_row_has_indexed_geolocation({**row, **metadata_values}),
            "filename_dates": row.get("filename_dates", ""),
            "media_creation_dates": metadata_values.get("media_creation_dates", row.get("media_creation_dates", "")),
            "filesystem_dates": row.get("filesystem_dates", ""),
            "capture_timestamp": metadata_values.get("capture_timestamp", row.get("capture_timestamp", "")),
            "capture_timestamp_source": metadata_values.get(
                "capture_timestamp_source",
                row.get("capture_timestamp_source", ""),
            ),
            "gps_latitude": metadata_values.get("gps_latitude", row.get("gps_latitude", "")),
            "gps_longitude": metadata_values.get("gps_longitude", row.get("gps_longitude", "")),
            "gps_source": metadata_values.get("gps_source", row.get("gps_source", "")),
            "has_gps": metadata_values.get("has_gps", row.get("has_gps", "")),
            "date_distance": metadata_values.get("date_distance", row.get("date_distance", "")),
            "alignment_score": row.get("alignment_score", ""),
            "alignment_confidence": row.get("alignment_confidence", ""),
            "alignment_crop": row.get("alignment_crop", ""),
            "alignment_error": row.get("alignment_error", ""),
            "review_crop_x": row.get("review_crop_x", ""),
            "review_crop_y": row.get("review_crop_y", ""),
            "review_crop_size": row.get("review_crop_size", ""),
            "review_crop_candidate_width": row.get("review_crop_candidate_width", ""),
            "review_crop_candidate_height": row.get("review_crop_candidate_height", ""),
            "review_crop_source": row.get("review_crop_source", ""),
            "review_crop_fill_color": row.get("review_crop_fill_color", ""),
            "review_crop_rotation_degrees": row.get("review_crop_rotation_degrees", ""),
            "visual_rank": row.get("visual_rank", ""),
            "visual_score": row.get("visual_score", ""),
            "visual_score_gap": row.get("visual_score_gap", ""),
            "visual_likely": row.get("visual_likely", ""),
            "visual_method": row.get("visual_method", ""),
            "visual_best_view": row.get("visual_best_view", ""),
            "visual_error": row.get("visual_error", ""),
            "review_decision": "use_external_original"
            if database_role == "external_original_reference"
            else row.get("review_decision", ""),
            "review_notes": row.get("review_notes", ""),
            "associated_entry_date": row.get("associated_entry_date", ""),
            "associated_date_source": row.get("associated_date_source", ""),
            "project_originals_source": _is_project_originals_source_path(path_text),
            "selected": decision in ACCEPT_DECISIONS or database_role == "external_original_reference",
            "associated": database_role != "external_original_reference"
            and (decision in ASSOCIATED_PHOTO_DECISIONS or database_role == "external_original_associated_photo"),
            "database_confirmed_role": database_role,
        }

    def _metadata_values_for_manual_drop(self, row: dict[str, str]) -> dict[str, str]:
        if "manual_drop_copy" not in row.get("evidence", ""):
            return {}
        if row.get("capture_timestamp") or row.get("gps_latitude") or row.get("gps_longitude"):
            return {}
        path_text = row.get("candidate_path", "").strip()
        if not path_text:
            return {}
        if path_text not in self._dropped_photo_metadata_cache:
            self._dropped_photo_metadata_cache[path_text] = _dropped_photo_metadata_values(
                Path(path_text),
                row.get("entry_date", ""),
            )
        return self._dropped_photo_metadata_cache[path_text]

    def associated_date_choices(self, entry_id: str, candidate_path: str) -> dict[str, Any]:
        rows = self._entry_rows(entry_id)
        for row in rows:
            if row.get("candidate_path") == candidate_path:
                return {
                    "entry_id": entry_id,
                    "candidate_path": candidate_path,
                    "default_date": row.get("entry_date", ""),
                    "choices": _associated_date_choices_from_row(row),
                }
        raise ValueError("Unknown candidate")

    def candidate_facts(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        facts: dict[str, dict[str, Any]] = {}
        paths_by_token = {
            token: path
            for token in tokens[:100]
            if (path := self.image_path(token)) is not None
        }
        indexed_geolocation = self._indexed_geolocation_by_path(paths_by_token.values())
        for token, path in paths_by_token.items():
            dimensions, has_geolocation = self._candidate_file_facts(path)
            has_geolocation = has_geolocation or indexed_geolocation.get(str(path), False)
            facts[token] = {
                "dimensions": dimensions,
                "has_embedded_geolocation": has_geolocation,
                "exists": path.exists(),
            }
        return facts

    def _candidate_file_facts(self, path: Path) -> tuple[str, bool]:
        key = str(path)
        cached = self._candidate_facts_cache.get(key)
        if cached is not None:
            return cached
        facts = (_image_dimensions(path), _has_embedded_geolocation(path))
        self._candidate_facts_cache[key] = facts
        if len(self._candidate_facts_cache) > 2000:
            oldest_key = next(iter(self._candidate_facts_cache))
            self._candidate_facts_cache.pop(oldest_key, None)
        return facts

    def _hydrate_candidate_rows_with_indexed_geolocation(self, rows: list[dict[str, str]]) -> None:
        stale_paths = [
            row.get("candidate_path", "").strip()
            for row in rows
            if row.get("candidate_path", "").strip()
            and not _candidate_row_has_indexed_geolocation(row)
        ]
        indexed_geolocation = self._indexed_geolocation_by_path_text(stale_paths)
        for row in rows:
            path_text = row.get("candidate_path", "").strip()
            if indexed_geolocation.get(path_text, False):
                row["has_gps"] = "1"
                row["has_embedded_geolocation"] = "1"

    def _indexed_geolocation_by_path(self, paths: Any) -> dict[str, bool]:
        return self._indexed_geolocation_by_path_text(str(path) for path in paths)

    def _indexed_geolocation_by_path_text(self, path_texts: Any) -> dict[str, bool]:
        original_to_resolved = {
            path: str(Path(path).resolve())
            for path in (str(path).strip() for path in path_texts)
            if path
        }
        if not original_to_resolved:
            return {}
        resolved_to_originals: dict[str, list[str]] = {}
        for original, resolved in original_to_resolved.items():
            resolved_to_originals.setdefault(resolved, []).append(original)
        resolved_paths = sorted(resolved_to_originals)
        index_db = default_index_db(self.config.canonical_root)
        if not index_db.exists():
            return {}
        try:
            with sqlite3.connect(index_db, timeout=1) as connection:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(photo_library_files)")}
                if not {"path", "gps_latitude", "gps_longitude"}.issubset(columns):
                    return {}
                has_gps_select = "has_gps" if "has_gps" in columns else "0"
                placeholders = ",".join("?" for _ in resolved_paths)
                indexed = {
                    str(path): bool(has_gps or _valid_geolocation(latitude, longitude))
                    for path, has_gps, latitude, longitude in connection.execute(
                        f"""
                        SELECT path, {has_gps_select}, gps_latitude, gps_longitude
                        FROM photo_library_files
                        WHERE path IN ({placeholders})
                        """,
                        resolved_paths,
                    )
                }
                return {
                    original: indexed.get(resolved, False)
                    for resolved, originals in resolved_to_originals.items()
                    for original in originals
                }
        except sqlite3.Error:
            return {}

    def _crop_summary(
        self,
        entry: dict[str, Any],
        candidate: dict[str, Any],
        source_state: str,
    ) -> dict[str, Any]:
        has_crop = _candidate_has_review_crop(candidate)
        crop_source = str(candidate.get("review_crop_source", "")).strip().lower()
        entry_id = str(entry.get("entry_id", ""))
        candidate_path = str(candidate.get("path", ""))
        staged_record = self._staged_crop_record(entry_id, candidate_path)
        crop_commit_pending = bool(
            staged_record is not None and self._staged_crop_commit_pending(staged_record)
        )
        return {
            "entry_id": entry_id,
            "entry_date": entry.get("entry_date", ""),
            "status": entry.get("status", "selected"),
            "source_token": entry.get("source_token", ""),
            "source_exists": entry.get("source_exists", False),
            "source_byte_size": entry.get("source_byte_size", ""),
            "source_file_type": entry.get("source_file_type", ""),
            "source_dimensions": entry.get("source_dimensions", ""),
            "people_names": entry.get("people_names", []),
            "candidate_path": candidate.get("path", ""),
            "candidate_filename": candidate.get("filename", ""),
            "candidate_dimensions": candidate.get("dimensions", ""),
            "candidate_token": candidate.get("token", ""),
            "crop_has_crop": has_crop,
            "crop_source": crop_source,
            "crop_source_state": source_state,
            "crop_commit_pending": crop_commit_pending,
            "crop_status": _crop_status_label(has_crop, crop_source),
        }

    def _database_crop_entries(self, exclude_entry_ids: set[str] | None = None) -> list[dict[str, Any]]:
        entries = []
        for row in self._database_crop_rows():
            if exclude_entry_ids and row["entry_id"] in exclude_entry_ids:
                continue
            summary = self._database_crop_summary_from_row(row)
            if summary is None:
                continue
            entries.append(summary)
        return entries

    def _database_crop_summary_from_row(self, row: sqlite3.Row) -> dict[str, Any] | None:
        entry_id = str(row["entry_id"])
        candidate_path_text = str(row["candidate_path"] or "").strip()
        if not candidate_path_text or not _candidate_path_is_available(candidate_path_text):
            return None
        if self._staged_crop_is_rejection(entry_id, candidate_path_text):
            return None
        source_path = self._source_media.get(entry_id)
        candidate_path = Path(candidate_path_text)
        crop = _review_crop_from_transformation_text(str(row["transformation_json"] or ""))
        candidate = {
            "path": candidate_path_text,
            "filename": str(row["candidate_filename"] or candidate_path.name),
            "dimensions": "",
            "review_crop_x": str(crop.get("x", "")) if crop else "",
            "review_crop_y": str(crop.get("y", "")) if crop else "",
            "review_crop_size": str(crop.get("size", "")) if crop else "",
            "review_crop_candidate_width": str(crop.get("candidate_width", "")) if crop else "",
            "review_crop_candidate_height": str(crop.get("candidate_height", "")) if crop else "",
            "review_crop_source": str(crop.get("source", "")) if crop else "",
            "review_crop_fill_color": str(crop.get("fill_color", "")) if crop else "",
            "review_crop_rotation_degrees": str(crop.get("rotation_degrees", "")) if crop else "",
        }
        self._apply_staged_crop_to_candidate(entry_id, candidate)
        entry = {
            "entry_id": entry_id,
            "entry_date": str(row["entry_date"]),
            "status": "selected",
            "source_token": self._image_token(source_path) if source_path else "",
            "source_exists": bool(source_path and source_path.exists()),
            "people_names": self._entry_people(entry_id),
        }
        return self._crop_summary(
            entry,
            candidate,
            self._database_crop_source_state(entry_id, candidate_path_text),
        )

    def _database_crop_entry_detail(
        self,
        entry_id: str,
        mark_estimated_viewed: bool = True,
    ) -> dict[str, Any] | None:
        for row in self._database_crop_rows(entry_id=entry_id):
            detail = self._database_crop_entry_from_row(
                row,
                mark_estimated_viewed=mark_estimated_viewed,
            )
            if detail is not None:
                return detail
        return None

    def _database_crop_entry_from_row(
        self,
        row: sqlite3.Row,
        mark_estimated_viewed: bool = True,
    ) -> dict[str, Any] | None:
        candidate_path_text = str(row["candidate_path"] or "").strip()
        if not candidate_path_text or not _candidate_path_is_available(candidate_path_text):
            return None
        if self._staged_crop_is_rejection(str(row["entry_id"]), candidate_path_text):
            return None
        source_path = self._source_media.get(str(row["entry_id"]))
        candidate_path = Path(candidate_path_text)
        crop = _review_crop_from_transformation_text(str(row["transformation_json"] or ""))
        candidate = {
            "path": candidate_path_text,
            "filename": str(row["candidate_filename"] or candidate_path.name),
            "folder_path": str(candidate_path.parent),
            "folder_label": _candidate_folder_label(candidate_path_text),
            "token": self._image_token(candidate_path),
            "exists": candidate_path.exists(),
            "evidence": "",
            "byte_size": str(row["candidate_byte_size"] or ""),
            "mime_type": str(row["candidate_mime_type"] or ""),
            "dimensions": _image_dimensions(candidate_path),
            "has_embedded_geolocation": _has_embedded_geolocation(candidate_path),
            "filename_dates": "",
            "media_creation_dates": "",
            "filesystem_dates": "",
            "capture_timestamp": "",
            "capture_timestamp_source": "",
            "date_distance": "",
            "alignment_score": "",
            "alignment_confidence": "",
            "alignment_crop": "",
            "alignment_error": "",
            "review_crop_x": str(crop.get("x", "")) if crop else "",
            "review_crop_y": str(crop.get("y", "")) if crop else "",
            "review_crop_size": str(crop.get("size", "")) if crop else "",
            "review_crop_candidate_width": str(crop.get("candidate_width", "")) if crop else "",
            "review_crop_candidate_height": str(crop.get("candidate_height", "")) if crop else "",
            "review_crop_source": str(crop.get("source", "")) if crop else "",
            "review_crop_fill_color": str(crop.get("fill_color", "")) if crop else "",
            "review_crop_rotation_degrees": str(crop.get("rotation_degrees", "")) if crop else "",
            "visual_rank": "",
            "visual_score": "",
            "visual_score_gap": "",
            "visual_likely": "",
            "visual_method": "",
            "visual_best_view": "",
            "visual_error": "",
            "review_decision": "use_external_original",
            "review_notes": "",
            "selected": True,
            "associated": False,
            "database_confirmed_role": "external_original_reference",
        }
        self._apply_staged_crop_to_candidate(
            str(row["entry_id"]),
            candidate,
            mark_estimated_viewed=mark_estimated_viewed,
        )
        crop_source_state = self._database_crop_source_state(str(row["entry_id"]), candidate_path_text)
        confirmed_candidates = [candidate]
        for database_row in self._database_confirmed_external_candidate_rows(str(row["entry_id"])):
            if database_row.get("candidate_path", "").strip() == candidate_path_text:
                continue
            confirmed_candidates.append(self._candidate_detail(database_row))
        associated_count = sum(1 for item in confirmed_candidates if item.get("associated"))
        entry = {
            "entry_id": str(row["entry_id"]),
            "entry_date": str(row["entry_date"]),
            "status": "selected",
            "candidate_count": len(confirmed_candidates),
            "selected_count": 1,
            "pending_commit_count": 0,
            "commit_ready": False,
            "accepted_count": 1,
            "associated_count": associated_count,
            "rejected_count": 0,
            "fallback_count": 0,
            "source_token": self._image_token(source_path) if source_path else "",
            "source_exists": bool(source_path and source_path.exists()),
            "source_byte_size": _file_byte_size(source_path),
            "source_file_type": _file_type_label(source_path),
            "source_dimensions": _image_dimensions(source_path),
            "people_names": self._entry_people(str(row["entry_id"])),
            "current_match_status": "",
            "current_decision": "",
            "manual_search_message": "",
            "used_range_days": [],
            "crop_source_state": crop_source_state,
            "crop_has_crop": _candidate_has_review_crop(candidate),
            "candidates": confirmed_candidates,
        }
        return entry

    def _database_entry_summaries(
        self,
        entry_ids: set[str],
        entry_dates: set[str],
        missing_links_only: bool = False,
        fallback_only: bool = False,
    ) -> list[dict[str, Any]]:
        summaries = []
        for entry_id in self._database_entry_ids(
            entry_ids,
            entry_dates,
            missing_links_only=missing_links_only,
            fallback_only=fallback_only,
        ):
            summary = self._database_entry_summary(entry_id, fallback_only=fallback_only)
            if summary is None:
                continue
            summaries.append(summary)
        summaries.sort(key=_entry_sort_key)
        return summaries

    def _database_entry_summary(self, entry_id: str, fallback_only: bool = False) -> dict[str, Any] | None:
        if fallback_only:
            self._recover_project_addition_candidates(entry_id)
        if self._entry_has_local_pending_rows(entry_id):
            rows = self._entry_rows_for_manual_candidate(entry_id)
            if not rows:
                return None
            return self._entry_summary(entry_id, rows)
        detail = self._database_entry_detail(entry_id)
        if not detail:
            return None
        return {key: value for key, value in detail.items() if key != "candidates"}

    def _database_entry_ids(
        self,
        entry_ids: set[str],
        entry_dates: set[str],
        missing_links_only: bool = False,
        fallback_only: bool = False,
    ) -> list[str]:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return []
        if missing_links_only and fallback_only:
            raise ValueError("missing_links_only and fallback_only cannot both be true")
        scope_clauses = []
        required_clauses = []
        params: list[str] = []
        if entry_ids:
            scope_clauses.append(f"entries.id IN ({','.join('?' for _ in entry_ids)})")
            params.extend(sorted(entry_ids))
        if entry_dates:
            scope_clauses.append(f"entries.entry_date IN ({','.join('?' for _ in entry_dates)})")
            params.extend(sorted(entry_dates))
        if missing_links_only:
            required_clauses.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM media_assets AS project365_target
                    WHERE project365_target.entry_id = entries.id
                        AND project365_target.role IN ({_sql_in_placeholders(ORIGINAL_PICKER_TARGET_ROLES)})
                )
                """
            )
            params.extend(ORIGINAL_PICKER_TARGET_ROLES)
            required_clauses.append(
                """
                NOT EXISTS (
                    SELECT 1
                    FROM media_assets AS completed_external
                    WHERE completed_external.entry_id = entries.id
                        AND completed_external.role IN ('external_original_reference', 'external_original_fallback')
                        AND completed_external.review_status = 'confirmed'
                )
                """
            )
        if fallback_only:
            required_clauses.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM media_assets AS project365_target
                    WHERE project365_target.entry_id = entries.id
                        AND project365_target.role IN ({_sql_in_placeholders(ORIGINAL_PICKER_TARGET_ROLES)})
                )
                """
            )
            params.extend(ORIGINAL_PICKER_TARGET_ROLES)
            required_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM media_assets AS fallback
                    WHERE fallback.entry_id = entries.id
                        AND fallback.role = 'external_original_fallback'
                        AND fallback.review_status = 'confirmed'
                )
                """
            )
            required_clauses.append(
                """
                NOT EXISTS (
                    SELECT 1
                    FROM media_assets AS external_original
                    WHERE external_original.entry_id = entries.id
                        AND external_original.role = 'external_original_reference'
                        AND external_original.review_status = 'confirmed'
                )
                """
            )
        if not scope_clauses and not required_clauses:
            return []
        clauses = []
        if scope_clauses:
            clauses.append(f"({' OR '.join(scope_clauses)})")
        clauses.extend(required_clauses)
        connection = sqlite3.connect(db_path)
        try:
            rows = connection.execute(
                f"""
                SELECT entries.id
                FROM entries
                WHERE {' AND '.join(clauses)}
                ORDER BY entries.entry_date, entries.id
                """,
                params,
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()
        return [str(row[0]) for row in rows if str(row[0] or "").strip()]

    def _database_entry_detail(self, entry_id: str) -> dict[str, Any] | None:
        selected_detail = self._database_crop_entry_detail(entry_id, mark_estimated_viewed=False)
        if selected_detail is not None:
            return selected_detail
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return None
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                """
                SELECT
                    entries.id,
                    entries.entry_date,
                    SUM(
                        CASE
                            WHEN media_assets.role = 'external_original_fallback'
                                AND media_assets.review_status = 'confirmed'
                            THEN 1 ELSE 0
                        END
                    ) AS fallback_count
                FROM entries
                LEFT JOIN media_assets
                    ON media_assets.entry_id = entries.id
                WHERE entries.id = ?
                GROUP BY entries.id, entries.entry_date
                """,
                (entry_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        if row is None:
            return None
        source_path = self._source_media.get(entry_id)
        fallback_count = int(row[2] or 0)
        return {
            "entry_id": str(row[0]),
            "entry_date": str(row[1]),
            "status": "fallback" if fallback_count else "search_needed",
            "candidate_count": 0,
            "selected_count": 0,
            "pending_commit_count": 0,
            "commit_ready": False,
            "accepted_count": 0,
            "associated_count": 0,
            "rejected_count": 0,
            "fallback_count": fallback_count,
            "source_token": self._image_token(source_path) if source_path else "",
            "source_exists": bool(source_path and source_path.exists()),
            "source_byte_size": _file_byte_size(source_path),
            "source_file_type": _file_type_label(source_path),
            "source_dimensions": _image_dimensions(source_path),
            "people_names": self._entry_people(entry_id),
            "current_match_status": "",
            "current_decision": "",
            "manual_search_message": "",
            "used_range_days": [],
            "candidates": [],
        }

    def _database_manual_candidate_template(self, entry_id: str) -> dict[str, str] | None:
        detail = self._database_entry_detail(entry_id)
        if detail is None:
            return None
        row = {
            "entry_id": entry_id,
            "entry_date": str(detail.get("entry_date", "")),
            "project365_media_asset_id": self._database_project365_media_asset_id(entry_id),
            "current_match_status": "",
            "current_decision": "",
            "review_decision": "search_needed",
        }
        try:
            fieldnames, _rows = self._read_rows_with_fieldnames()
        except FileNotFoundError:
            fieldnames = []
        _queue_fieldnames_with_required_fields(fieldnames, row)
        return row

    def _database_project365_media_asset_id(self, entry_id: str) -> str:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return ""
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                """
                SELECT id
                FROM media_assets
                WHERE entry_id = ?
                    AND role = 'project365_export_png'
                ORDER BY id
                LIMIT 1
                """,
                (entry_id,),
            ).fetchone()
        except sqlite3.Error:
            return ""
        finally:
            connection.close()
        return str(row[0]) if row else ""

    def _database_confirmed_external_roles(self, entry_id: str) -> dict[str, str]:
        return {
            row["candidate_path"]: row["database_confirmed_role"]
            for row in self._database_confirmed_external_candidate_rows(entry_id)
            if row.get("candidate_path", "").strip()
        }

    def _database_confirmed_external_candidate_rows(self, entry_id: str) -> list[dict[str, str]]:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return []
        connection = sqlite3.connect(db_path)
        try:
            rows = connection.execute(
                """
                SELECT
                    entries.entry_date,
                    media_assets.storage_path,
                    media_assets.internal_filename,
                    media_assets.sha256,
                    media_assets.byte_size,
                    media_assets.mime_type,
                    media_assets.role,
                    media_assets.transformation_json
                FROM media_assets
                JOIN entries
                    ON entries.id = media_assets.entry_id
                WHERE media_assets.entry_id = ?
                    AND media_assets.role IN ('external_original_reference', 'external_original_associated_photo')
                    AND media_assets.review_status = 'confirmed'
                    AND COALESCE(media_assets.storage_path, '') != ''
                """,
                (entry_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()
        project365_media_asset_id = self._database_project365_media_asset_id(entry_id)
        result = []
        for row in rows:
            candidate_path = str(row[1] or "").strip()
            if not candidate_path:
                continue
            path = Path(candidate_path)
            transformation = _parse_json_object(str(row[7] or ""))
            candidate_sha256 = str(row[3] or "").strip() or hashlib.sha256(candidate_path.encode("utf-8")).hexdigest()
            result.append(
                {
                    "entry_id": entry_id,
                    "entry_date": str(row[0] or ""),
                    "project365_media_asset_id": project365_media_asset_id,
                    "current_match_status": "",
                    "current_decision": "",
                    "candidate_path": candidate_path,
                    "candidate_filename": str(row[2] or path.name),
                    "candidate_sha256": candidate_sha256,
                    "byte_size": str(row[4] or ""),
                    "mime_type": str(row[5] or ""),
                    "filename_dates": "",
                    "media_creation_dates": "",
                    "filesystem_dates": "",
                    "capture_timestamp": "",
                    "capture_timestamp_source": "",
                    "gps_latitude": "",
                    "gps_longitude": "",
                    "gps_source": "",
                    "has_gps": "",
                    "date_distance": "",
                    "evidence": str(transformation.get("evidence", "database_confirmed")),
                    "candidate_filter_reason": "",
                    "review_decision": "",
                    "review_notes": "",
                    "database_confirmed_role": str(row[6] or ""),
                }
            )
        return result

    def _with_database_confirmed_roles(
        self,
        entry_id: str,
        rows: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        roles_by_path = self._database_confirmed_external_roles(entry_id)
        if not roles_by_path:
            return rows
        existing_paths = {row.get("candidate_path", "").strip() for row in rows}
        overlaid = []
        for row in rows:
            next_row = dict(row)
            role = roles_by_path.get(next_row.get("candidate_path", "").strip(), "")
            if role and not next_row.get("database_confirmed_role"):
                next_row["database_confirmed_role"] = role
            overlaid.append(next_row)
        for database_row in self._database_confirmed_external_candidate_rows(entry_id):
            candidate_path = database_row.get("candidate_path", "").strip()
            if candidate_path and candidate_path not in existing_paths:
                overlaid.append(database_row)
                existing_paths.add(candidate_path)
        return overlaid

    def _project_addition_candidate_paths(self, entry_date: str) -> list[Path]:
        try:
            parsed_date = dt.date.fromisoformat(entry_date)
        except ValueError:
            return []
        source_dir = project_originals_source_path(self.config.canonical_root) / parsed_date.strftime("%Y-%m")
        if not source_dir.exists():
            return []
        prefix = f"{entry_date} {PROJECT_ADDITION_DROP_TIME.strftime('%H%M%S')} ({PROJECT_ADDITION_FILENAME_MARKER})"
        paths = [
            path
            for path in source_dir.glob(f"{prefix}*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        return sorted(paths, key=_project_addition_recovery_sort_key)

    def _recover_project_addition_candidates(self, entry_id: str) -> None:
        if entry_id in self._added_candidate_rows or entry_id in self._replacement_candidate_rows:
            return
        detail = self._database_entry_detail(entry_id)
        if detail is None or detail.get("status") != "fallback":
            return
        template = self._database_manual_candidate_template(entry_id)
        if template is None:
            return
        entry_date = template.get("entry_date", "")
        candidate_paths = self._project_addition_candidate_paths(entry_date)
        if not candidate_paths:
            return
        existing_paths = {
            row.get("candidate_path", "").strip()
            for row in self._entry_rows(entry_id)
            if row.get("candidate_path", "").strip()
        }
        new_paths = [path.resolve() for path in candidate_paths if str(path.resolve()) not in existing_paths]
        if not new_paths:
            return
        selected_path = new_paths[-1]
        values_list: list[dict[str, Any]] = []
        for path in new_paths:
            metadata_values = _dropped_photo_metadata_values(path, entry_date)
            values_list.append(
                {
                    "entry_id": entry_id,
                    "entry_date": entry_date,
                    "project365_media_asset_id": template.get("project365_media_asset_id", ""),
                    "current_match_status": template.get("current_match_status", ""),
                    "current_decision": template.get("current_decision", ""),
                    "candidate_path": str(path),
                    "candidate_filename": path.name,
                    "candidate_sha256": _sha256_path(path),
                    "byte_size": str(path.stat().st_size),
                    "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "filename_dates": entry_date,
                    "media_creation_dates": metadata_values.get("media_creation_dates", ""),
                    "filesystem_dates": entry_date,
                    "capture_timestamp": metadata_values.get("capture_timestamp", ""),
                    "capture_timestamp_source": metadata_values.get("capture_timestamp_source", ""),
                    "gps_latitude": metadata_values.get("gps_latitude", ""),
                    "gps_longitude": metadata_values.get("gps_longitude", ""),
                    "gps_source": metadata_values.get("gps_source", ""),
                    "has_gps": metadata_values.get("has_gps", ""),
                    "date_distance": metadata_values.get("date_distance", ""),
                    "evidence": "manual_drop_copy;recovered_project_addition",
                    "candidate_filter_reason": "",
                    "review_decision": "use_external_original" if path == selected_path else "",
                    "review_notes": "Recovered dropped Project365 project file addition.",
                }
            )
        self._store_added_candidates(entry_id, template, values_list)

    def _database_crop_source_state(self, entry_id: str, candidate_path: str) -> str:
        if self._staged_crop_record(entry_id, candidate_path) is not None:
            return "staged"
        return "applied"

    def _database_crop_rows(self, entry_id: str | None = None) -> list[sqlite3.Row]:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return []
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            where_entry = "AND entries.id = ?" if entry_id else ""
            params = (entry_id,) if entry_id else ()
            return list(
                connection.execute(
                    f"""
                    SELECT
                        entries.id AS entry_id,
                        entries.entry_date AS entry_date,
                        external_refs.storage_path AS candidate_path,
                        external_refs.internal_filename AS candidate_filename,
                        external_refs.byte_size AS candidate_byte_size,
                        external_refs.mime_type AS candidate_mime_type,
                        external_refs.transformation_json AS transformation_json
                    FROM media_assets AS external_refs
                    JOIN entries
                        ON entries.id = external_refs.entry_id
                    WHERE external_refs.role = 'external_original_reference'
                        AND external_refs.review_status = 'confirmed'
                        AND COALESCE(external_refs.storage_path, '') != ''
                        {where_entry}
                    ORDER BY entries.entry_date, entries.id
                    """,
                    params,
                )
            )
        finally:
            connection.close()

    def _set_database_crop(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any],
        source: str,
        commit_pending: bool | None = None,
    ) -> bool:
        path = Path(candidate_path)
        normalized = _normalized_review_crop(crop, path)
        normalized["source"] = source
        normalized["unit"] = "source_pixels"
        normalized["shape"] = "square"
        if not self._database_crop_candidate_exists(entry_id, candidate_path):
            return False
        self._store_crop_staging(
            entry_id,
            candidate_path,
            normalized,
            commit_pending=(source != "estimated" if commit_pending is None else commit_pending),
        )
        return True

    def _clear_database_crop(self, entry_id: str, candidate_path: str) -> bool:
        if not self._database_crop_candidate_exists(entry_id, candidate_path):
            return False
        self._store_crop_staging(entry_id, candidate_path, None, commit_pending=True)
        return True

    def _database_crop_candidate_exists(self, entry_id: str, candidate_path: str) -> bool:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return False
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM media_assets
                WHERE entry_id = ?
                    AND role = 'external_original_reference'
                    AND storage_path = ?
                """,
                (entry_id, candidate_path),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def _update_database_review_crop(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, object] | None,
    ) -> bool:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return False
        connection = sqlite3.connect(db_path)
        try:
            updated = self._update_database_review_crop_with_connection(
                connection,
                entry_id,
                candidate_path,
                crop,
            )
            if not updated:
                return False
            connection.commit()
            return True
        finally:
            connection.close()

    def _update_database_review_crop_with_connection(
        self,
        connection: sqlite3.Connection,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, object] | None,
    ) -> bool:
        row = connection.execute(
            """
            SELECT id, transformation_json
            FROM media_assets
            WHERE entry_id = ?
                AND role = 'external_original_reference'
                AND storage_path = ?
            """,
            (entry_id, candidate_path),
        ).fetchone()
        if row is None:
            return False
        media_id, transformation_text = row
        try:
            transformation = json.loads(transformation_text or "{}")
        except json.JSONDecodeError:
            transformation = {}
        if crop is None:
            transformation.pop("review_crop", None)
        else:
            transformation["review_crop"] = crop
        connection.execute(
            """
            UPDATE media_assets
            SET transformation_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(transformation, sort_keys=True),
                dt.datetime.now(dt.UTC).isoformat(),
                media_id,
            ),
        )
        return True

    def commit_staged_crops(self, start_date: str = "", end_date: str = "") -> dict[str, Any]:
        with self._lock:
            start_date = str(start_date or "").strip()
            end_date = str(end_date or "").strip()
            staged = {
                entry_id: {candidate_path: dict(record) for candidate_path, record in candidates.items()}
                for entry_id, candidates in self._crop_staging.items()
                if _entry_id_in_date_scope(entry_id, start_date, end_date)
            }
            saved_count = 0
            cleared_count = 0
            rejected_count = 0
            missing_count = 0
            missing_staging: dict[str, dict[str, dict[str, Any]]] = {}
            rejected_rows_to_append: list[dict[str, str]] = []
            prune_summary: dict[str, Any] = {}
            db_path = self.config.canonical_root / "canonical.db"
            if not db_path.exists():
                raise FileNotFoundError(f"Missing canonical database: {db_path}")
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            try:
                for entry_id, candidates in staged.items():
                    for candidate_path, record in candidates.items():
                        if not self._staged_crop_commit_pending(record):
                            continue
                        action = str(record.get("action", "")).strip().lower()
                        if action == "reject_original":
                            row = self._database_reference_row(connection, entry_id, candidate_path)
                            if row is None:
                                missing_count += 1
                                missing_staging.setdefault(entry_id, {})[candidate_path] = record
                                continue
                            rejected_row = self._apply_database_crop_original_rejection(
                                connection,
                                entry_id,
                                row,
                                str(record.get("notes", "")),
                            )
                            self._crop_staging.get(entry_id, {}).pop(candidate_path, None)
                            rejected_rows_to_append.append(rejected_row)
                            rejected_count += 1
                            continue
                        crop = record.get("crop")
                        if crop is not None and not isinstance(crop, dict):
                            crop = None
                        applied = self._update_database_review_crop_with_connection(
                            connection,
                            entry_id,
                            candidate_path,
                            crop,
                        )
                        if not applied:
                            missing_count += 1
                            missing_staging.setdefault(entry_id, {})[candidate_path] = record
                            continue
                        if crop is None:
                            cleared_count += 1
                        else:
                            saved_count += 1
                        self._crop_staging.get(entry_id, {}).pop(candidate_path, None)
                    if not self._crop_staging.get(entry_id):
                        self._crop_staging.pop(entry_id, None)
                connection.commit()
            finally:
                connection.close()
            for rejected_row in rejected_rows_to_append:
                self._append_rejected_queue_row(rejected_row)
            if rejected_rows_to_append:
                self._completed_external_entry_ids_cache = None
                self._completed_external_reference_entry_ids_cache = None
                self._completed_queue_prune_signature = None
                prune_summary = prune_applied_review_queue(
                    canonical_root=self.config.canonical_root,
                    queue_path=self.config.queue_path,
                    report_dir=self.config.queue_path.parent,
                )
                self._entry_rows_cache = {}
                self._queue_shards.invalidate()
            missing_archive_path = self._archive_missing_crop_staging(missing_staging)
            for entry_id, candidates in missing_staging.items():
                for candidate_path in candidates:
                    self._crop_staging.get(entry_id, {}).pop(candidate_path, None)
                if not self._crop_staging.get(entry_id):
                    self._crop_staging.pop(entry_id, None)
            self._persist_crop_staging()
        return {
            "saved_count": saved_count,
            "cleared_count": cleared_count,
            "rejected_count": rejected_count,
            "missing_count": missing_count,
            "missing_archive_path": missing_archive_path,
            "remaining_count": self.pending_crop_commits()["pending_count"],
            "remaining_queue_rows": prune_summary.get("queue_rows", ""),
            "remaining_entries": prune_summary.get("entry_count", ""),
            "staging_path": str(self._crop_staging_path()),
        }

    def _rank_oversized_entry_if_needed(
        self,
        entry_id: str,
        entry_rows: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        candidate_rows = [
            row
            for row in entry_rows
            if row.get("candidate_path", "").strip()
            and not _is_hidden_candidate_row(row)
            and row.get("review_decision", "").strip().lower() not in REJECT_DECISIONS
        ]
        if len(candidate_rows) <= DEFAULT_OVERSIZED_THRESHOLD:
            return entry_rows
        if all(
            row.get("visual_method", "") == VISUAL_METHOD_VERSION
            and (row.get("visual_rank", "").strip() or row.get("visual_error", "").strip())
            and not (
                row.get("visual_error", "").strip() == "missing_file"
                and Path(row.get("candidate_path", "")).exists()
            )
            for row in candidate_rows
        ):
            return entry_rows
        source_path = self._source_media.get(entry_id)
        if source_path is None:
            return entry_rows
        cache = VisualDescriptorCache(default_cache_path(self.config.canonical_root))
        try:
            results = rank_candidate_rows(cache, source_path, candidate_rows, likely_limit=DEFAULT_LIKELY_LIMIT)
        finally:
            cache.close()
        with self._lock:
            fieldnames, rows = self._read_rows_with_fieldnames()
            for row in rows:
                if row.get("entry_id") != entry_id:
                    continue
                result = results.get(row.get("candidate_path", ""))
                if result:
                    row.update(result)
            self._write_rows(fieldnames, rows)
        return [row for row in rows if row.get("entry_id") == entry_id]

    def image_path(self, token: str) -> Path | None:
        if token not in self._image_paths:
            self._refresh_image_paths()
        return self._image_paths.get(token)

    def preview_path(self, token: str, max_size: int | None) -> Path | None:
        path = self.image_path(token)
        if path is None or max_size is None:
            return path
        if max_size < MIN_PREVIEW_SIZE or max_size > MAX_PREVIEW_SIZE:
            raise ValueError("Invalid preview size")
        try:
            stat = path.stat()
        except OSError:
            return path
        cache_dir = self.config.canonical_root / "cache" / "picker_previews"
        preview = cache_dir / f"{token}-{max_size}-{stat.st_size}-{stat.st_mtime_ns}.jpg"
        if preview.exists():
            return preview
        with self._preview_lock:
            if preview.exists():
                return preview
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = preview.with_suffix(".tmp.jpg")
            result = subprocess.run(
                [
                    "/opt/homebrew/bin/magick",
                    str(path),
                    "-auto-orient",
                    "-thumbnail",
                    f"{max_size}x{max_size}>",
                    "-strip",
                    "-quality",
                    "82",
                    str(temporary),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if result.returncode != 0 or not temporary.exists():
                temporary.unlink(missing_ok=True)
                return path
            temporary.replace(preview)
        return preview

    def save_decision(
        self,
        entry_id: str,
        candidate_path: str,
        decision: str,
        notes: str,
        crop: dict[str, Any] | None = None,
        include_candidates: bool = True,
        associated_entry_date: str = "",
        associated_date_source: str = "manual",
    ) -> dict[str, Any]:
        normalized_decision = decision.strip().lower()
        if normalized_decision not in {
            "use_external_original",
            "rejected",
            "clear",
            "unlink_associated_photo",
            *ASSOCIATED_PHOTO_DECISIONS,
            *FALLBACK_DECISIONS,
        }:
            raise ValueError("Unsupported decision")
        rows: list[dict[str, str]]
        with self._lock:
            rows = self._entry_rows_for_manual_candidate(entry_id)
            if (
                normalized_decision in ASSOCIATED_PHOTO_DECISIONS
                and self._database_crop_candidate_exists(entry_id, candidate_path)
            ):
                raise ValueError("This photo is already the selected original.")
            if normalized_decision == "clear":
                if not rows:
                    raise ValueError("Unknown entry")
                self._decision_overrides.pop(entry_id, None)
                self._added_candidate_rows.pop(entry_id, None)
                self._replacement_candidate_rows.pop(entry_id, None)
                self._entry_rows_cache.pop(entry_id, None)
                clear_reject_all_range_state(self.config.queue_path.parent, entry_id)
                rows = self._entry_rows(entry_id)
                for row in rows:
                    row["review_decision"] = ""
                    row["review_notes"] = ""
                found_entry = True
                found_candidate = True
            else:
                found_entry = False
                found_candidate = normalized_decision in FALLBACK_DECISIONS
                fallback_applied = False
                for row in rows:
                    found_entry = True
                    if normalized_decision in FALLBACK_DECISIONS:
                        if not fallback_applied:
                            row["review_decision"] = normalized_decision
                            row["review_notes"] = notes
                            fallback_applied = True
                        elif row.get("review_decision", "").strip().lower() in FALLBACK_DECISIONS:
                            row["review_decision"] = ""
                            row["review_notes"] = ""
                        continue
                    if row.get("review_decision", "").strip().lower() in FALLBACK_DECISIONS:
                        row["review_decision"] = ""
                        row["review_notes"] = ""
                    if row.get("candidate_path") == candidate_path:
                        found_candidate = True
                        current_decision = row.get("review_decision", "").strip().lower()
                        database_role = row.get("database_confirmed_role", "").strip().lower()
                        if normalized_decision == "unlink_associated_photo":
                            if current_decision in ASSOCIATED_PHOTO_DECISIONS:
                                row["review_decision"] = ""
                                row["review_notes"] = ""
                                row["associated_entry_date"] = ""
                                row["associated_date_source"] = ""
                            continue
                        row["review_decision"] = normalized_decision
                        row["review_notes"] = notes
                        if normalized_decision in ASSOCIATED_PHOTO_DECISIONS:
                            if (
                                current_decision in ACCEPT_DECISIONS
                                or database_role == "external_original_reference"
                            ):
                                raise ValueError("This photo is already the selected original.")
                            associated_date = _normalized_associated_entry_date(associated_entry_date)
                            row["associated_entry_date"] = associated_date or row.get("entry_date", "")
                            row["associated_date_source"] = associated_date_source.strip() or "manual"
                        if normalized_decision == "use_external_original" and crop is not None:
                            _set_review_crop(row, crop, source="manual")
                    elif normalized_decision == "use_external_original" and row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS:
                        row["review_decision"] = ""
                        row["review_notes"] = ""
            if not found_entry:
                raise ValueError("Unknown entry")
            if not found_candidate:
                raise ValueError("Unknown candidate")
            self._store_entry_overrides(entry_id, rows)
        if include_candidates:
            return self.entry_detail(entry_id) or {}
        return self._entry_summary(entry_id, rows)

    def reject_all_candidates(
        self,
        entry_id: str,
        notes: str,
        include_candidates: bool = True,
        photo_index_folder: str | Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            rows = self._entry_rows(entry_id)
            found_entry = bool(rows)
            rejected_count = 0
            for row in rows:
                if row.get("candidate_path", "").strip() and not _is_hidden_candidate_row(row):
                    row["review_decision"] = "rejected"
                    row["review_notes"] = notes
                    rejected_count += 1
                elif row.get("review_decision", "").strip().lower() in FALLBACK_DECISIONS:
                    row["review_decision"] = ""
                    row["review_notes"] = ""
            if not found_entry:
                raise ValueError("Unknown entry")
            self._store_entry_overrides(entry_id, rows)
            current_range_days = self._current_search_range_days(entry_id, rows)
            record_reject_all_range_state(
                self.config.queue_path.parent,
                entry_id,
                rejected_all_range_days=current_range_days,
                manual_search_required=current_range_days >= 15,
                message=MANUAL_SEARCH_REQUIRED_MESSAGE if current_range_days >= 15 else "",
                photo_index_folder=photo_index_folder,
            )
        return {
            "rejected_count": rejected_count,
            "entry": (
                self.entry_detail(entry_id) or {}
                if include_candidates
                else self._entry_summary(entry_id, rows)
            ),
        }

    def save_crop(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any],
        source: str = "manual",
        commit_pending: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            rows = self._entry_rows(entry_id)
            found = False
            for row in rows:
                if (
                    row.get("entry_id") == entry_id
                    and row.get("candidate_path") == candidate_path
                    and row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                ):
                    _set_review_crop(row, crop, source=source)
                    found = True
                    break
            if found:
                self._store_entry_overrides(entry_id, rows)
                return self.crop_entry_detail(entry_id) or {}
        if self._set_database_crop(entry_id, candidate_path, crop, source, commit_pending=commit_pending):
            return self.crop_entry_detail(entry_id, mark_estimated_viewed=commit_pending) or {}
        raise ValueError("Unknown candidate")

    def reset_crop(
        self,
        entry_id: str,
        candidate_path: str,
        preserve_estimate: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            rows = self._entry_rows(entry_id)
            found = False
            for row in rows:
                if (
                    row.get("entry_id") == entry_id
                    and row.get("candidate_path") == candidate_path
                    and row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                ):
                    if (
                        preserve_estimate
                        and str(row.get("review_crop_source", "")).strip().lower() == "estimated"
                        and _candidate_has_review_crop(row)
                    ):
                        return self.crop_entry_detail(entry_id, mark_estimated_viewed=False) or {}
                    _clear_review_crop(row)
                    found = True
                    break
            if found:
                self._store_entry_overrides(entry_id, rows)
                return self.crop_entry_detail(entry_id) or {}
        if preserve_estimate and self._restore_database_estimated_crop(entry_id, candidate_path):
            return self.crop_entry_detail(entry_id, mark_estimated_viewed=False) or {}
        if self._clear_database_crop(entry_id, candidate_path):
            return self.crop_entry_detail(entry_id) or {}
        raise ValueError("Unknown candidate")

    def _restore_database_estimated_crop(self, entry_id: str, candidate_path: str) -> bool:
        detail = self._database_crop_entry_detail(entry_id, mark_estimated_viewed=False)
        if detail is None:
            return False
        for candidate in detail.get("candidates", []):
            if str(candidate.get("path", "")) != candidate_path:
                continue
            if str(candidate.get("review_crop_source", "")).strip().lower() != "estimated":
                return False
            crop = _review_crop_from_candidate(candidate)
            if crop is None:
                return False
            crop["source"] = "estimated"
            if self._staged_crop_record(entry_id, candidate_path) is not None:
                self._store_crop_staging(entry_id, candidate_path, crop, commit_pending=False)
            return True
        return False

    def reject_crop_original(
        self,
        entry_id: str,
        candidate_path: str,
        notes: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            if self._reject_queued_crop_original(entry_id, candidate_path, notes):
                return self.entry_detail(entry_id) or {
                    "entry_id": entry_id,
                    "status": "rejected",
                    "candidates": [],
                }
            result = self._reject_database_crop_original(entry_id, candidate_path, notes)
        self._source_media = self._load_source_media()
        self._refresh_image_paths()
        return result

    def _reject_queued_crop_original(
        self,
        entry_id: str,
        candidate_path: str,
        notes: str,
    ) -> bool:
        fieldnames, rows = self._read_rows_with_fieldnames()
        found = False
        for row in rows:
            if row.get("entry_id") != entry_id:
                continue
            decision = row.get("review_decision", "").strip().lower()
            if row.get("candidate_path") == candidate_path and decision in ACCEPT_DECISIONS:
                row["review_decision"] = "rejected"
                row["review_notes"] = notes or "Rejected from crop confirmation."
                _clear_review_crop(row)
                found = True
            elif decision in ACCEPT_DECISIONS:
                row["review_decision"] = ""
                row["review_notes"] = ""
                _clear_review_crop(row)
        if not found:
            return False
        self._write_rows(fieldnames, rows)
        refresh_external_original_queue_reports(
            self.config.canonical_root,
            self.config.queue_path.parent,
            rows,
        )
        return True

    def _reject_database_crop_original(
        self,
        entry_id: str,
        candidate_path: str,
        notes: str,
    ) -> dict[str, Any]:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            raise FileNotFoundError(f"Missing canonical database: {db_path}")
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            row = self._database_reference_row(connection, entry_id, candidate_path)
            if row is None:
                raise ValueError("Unknown candidate")
            rejected_row = self._rejected_queue_row_from_database_reference(entry_id, row, notes)
        finally:
            connection.close()
        self._store_crop_rejection_staging(entry_id, candidate_path, rejected_row, notes)
        return {
            "entry": {
                "entry_id": entry_id,
                "entry_date": rejected_row["entry_date"],
                "status": "search_needed",
                "candidates": [],
            },
            "rejected_count": 1,
            "staged": True,
            "pending_crop_commits": self.pending_crop_commits(),
        }

    def _database_reference_row(
        self,
        connection: sqlite3.Connection,
        entry_id: str,
        candidate_path: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT
                entries.entry_date,
                export_media.id AS project365_media_asset_id,
                external_refs.id AS reference_id,
                external_refs.internal_filename,
                external_refs.storage_path,
                external_refs.sha256,
                external_refs.byte_size,
                external_refs.mime_type,
                external_refs.transformation_json,
                external_refs.import_batch_id
            FROM media_assets AS external_refs
            JOIN entries
                ON entries.id = external_refs.entry_id
            JOIN media_assets AS export_media
                ON export_media.entry_id = external_refs.entry_id
                AND export_media.role = 'project365_export_png'
            WHERE external_refs.entry_id = ?
                AND external_refs.role = 'external_original_reference'
                AND external_refs.review_status = 'confirmed'
                AND external_refs.storage_path = ?
            """,
            (entry_id, candidate_path),
        ).fetchone()

    def _apply_database_crop_original_rejection(
        self,
        connection: sqlite3.Connection,
        entry_id: str,
        row: sqlite3.Row,
        notes: str,
    ) -> dict[str, str]:
        rejected_row = self._rejected_queue_row_from_database_reference(entry_id, row, notes)
        now = dt.datetime.now(dt.UTC).isoformat()
        transformation = self._rejected_transformation(row, notes)
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
            VALUES (?, ?, 'external_original_rejected', NULL, ?, ?, ?, ?, ?, 'rejected', 'rejected', 0, ?, ?, ?, ?)
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
                _external_decision_id(entry_id, rejected_row["candidate_sha256"], "external_original_rejected"),
                entry_id,
                rejected_row["candidate_filename"],
                rejected_row["candidate_path"],
                rejected_row["candidate_sha256"],
                int(rejected_row["byte_size"] or 0),
                rejected_row["mime_type"],
                json.dumps(transformation, sort_keys=True),
                row["import_batch_id"],
                now,
                now,
            ),
        )
        connection.execute("DELETE FROM media_assets WHERE id = ?", (row["reference_id"],))
        return rejected_row

    def _rejected_queue_row_from_database_reference(
        self,
        entry_id: str,
        row: sqlite3.Row,
        notes: str,
    ) -> dict[str, str]:
        candidate_path = str(row["storage_path"] or "")
        candidate_sha256 = str(row["sha256"] or "").strip()
        if not candidate_sha256:
            candidate_sha256 = hashlib.sha256(candidate_path.encode("utf-8")).hexdigest()
        transformation = _parse_json_object(str(row["transformation_json"] or ""))
        return {
            "entry_id": entry_id,
            "entry_date": str(row["entry_date"] or ""),
            "project365_media_asset_id": str(row["project365_media_asset_id"] or ""),
            "current_match_status": "",
            "current_decision": "",
            "candidate_path": candidate_path,
            "candidate_filename": str(row["internal_filename"] or Path(candidate_path).name),
            "candidate_sha256": candidate_sha256,
            "byte_size": str(row["byte_size"] or ""),
            "mime_type": str(row["mime_type"] or ""),
            "filename_dates": "",
            "media_creation_dates": "",
            "filesystem_dates": "",
            "capture_timestamp": "",
            "capture_timestamp_source": "",
            "date_distance": "",
            "evidence": str(transformation.get("evidence", "")),
            "candidate_filter_reason": "",
            "review_decision": "rejected",
            "review_notes": notes or "Rejected from crop confirmation.",
        }

    def _rejected_transformation(self, row: sqlite3.Row, notes: str) -> dict[str, object]:
        transformation = _parse_json_object(str(row["transformation_json"] or ""))
        transformation.pop("review_crop", None)
        transformation["source"] = "external_original_rejection"
        transformation["source_path"] = str(row["storage_path"] or "")
        transformation["review_decision"] = "rejected"
        transformation["review_notes"] = notes or "Rejected from crop confirmation."
        transformation["original_is_read_only"] = True
        return transformation

    def _append_rejected_queue_row(self, rejected_row: dict[str, str]) -> None:
        fieldnames, rows = self._read_rows_with_fieldnames()
        fieldnames = _queue_fieldnames_with_required_fields(fieldnames, rejected_row)
        kept_rows = [
            row
            for row in rows
            if not (
                row.get("entry_id") == rejected_row["entry_id"]
                and row.get("candidate_path") == rejected_row["candidate_path"]
            )
        ]
        kept_rows.append({field: str(rejected_row.get(field, "")) for field in fieldnames})
        self._write_rows(fieldnames, kept_rows)

    def suggest_crop_for_candidate(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        estimated_crop = self._estimated_crop_for_candidate(entry_id, candidate_path, crop)
        detail = self.save_crop(entry_id, str(Path(candidate_path)), estimated_crop, source="estimated")
        return {"entry": detail, "crop": estimated_crop}

    def _estimated_crop_for_candidate(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reference_path = self._source_media.get(entry_id)
        if reference_path is None or not reference_path.exists():
            raise ValueError("Missing Project365 target image")
        candidate = Path(candidate_path)
        if not candidate.exists() or not candidate.is_file():
            raise ValueError("Missing candidate original image")
        prior_crop = crop if isinstance(crop, dict) else {}
        rotation_degrees = _normalized_rotation_degrees(prior_crop.get("rotation_degrees"))
        fill_color = _normalized_fill_color(prior_crop.get("fill_color"))
        suggestion = suggest_crop(
            reference_path,
            candidate,
            candidate_rotation_degrees=rotation_degrees,
        )
        estimated_crop = _square_crop_from_alignment(asdict(suggestion))
        estimated_crop["rotation_degrees"] = rotation_degrees
        if fill_color:
            estimated_crop["fill_color"] = fill_color
        return estimated_crop

    def start_crop_estimate_batch(
        self,
        apply_estimates: bool = False,
        refresh_saved_estimates: bool = False,
        target_extensions: list[str] | set[str] | None = None,
    ) -> dict[str, Any]:
        with self._job_lock:
            active_job = self._latest_crop_estimate_job_locked(active_only=True)
            if active_job is not None:
                return dict(active_job)
        normalized_extensions = _normalized_target_extensions(target_extensions)
        crop_filter = "estimated" if refresh_saved_estimates else "missing"
        targets = [
            {
                "entry_id": entry["entry_id"],
                "candidate_path": entry["candidate_path"],
            }
            for entry in self.crop_entries(crop_filter=crop_filter)
            if entry.get("entry_id") and entry.get("candidate_path")
            and (
                not normalized_extensions
                or Path(str(entry.get("candidate_path", ""))).suffix.lower() in normalized_extensions
            )
        ]
        job_id = hashlib.sha256(
            (
                f"crop-estimate:{dt.datetime.now(dt.UTC).isoformat()}:"
                f"{refresh_saved_estimates}:{normalized_extensions}:{targets}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        job = {
            "id": job_id,
            "status": "queued",
            "target_count": len(targets),
            "processed_count": 0,
            "estimated_count": 0,
            "changed_count": 0,
            "unchanged_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "current_entry_id": "",
            "started_at": dt.datetime.now(dt.UTC).isoformat(),
            "finished_at": "",
            "last_update_at": dt.datetime.now(dt.UTC).isoformat(),
            "errors": [],
            "apply_estimates": bool(apply_estimates),
            "refresh_saved_estimates": bool(refresh_saved_estimates),
            "target_extensions": sorted(normalized_extensions),
            "message": "",
        }
        with self._job_lock:
            self._crop_estimate_jobs[job_id] = job
        if not targets:
            message = (
                "No saved crop estimates to refresh."
                if refresh_saved_estimates
                else "No missing crop estimates to run."
            )
            self._update_crop_estimate_job(
                job_id,
                status="pass",
                finished_at=dt.datetime.now(dt.UTC).isoformat(),
                message=message,
            )
            return self.crop_estimate_job(job_id) or dict(job)
        thread = threading.Thread(
            target=self._run_crop_estimate_batch,
            args=(job_id, targets, bool(apply_estimates), bool(refresh_saved_estimates)),
            daemon=True,
        )
        thread.start()
        return dict(job)

    def crop_estimate_job(self, job_id: str) -> dict[str, Any] | None:
        with self._job_lock:
            job = self._crop_estimate_jobs.get(job_id)
            return dict(job) if job else None

    def crop_estimate_jobs(self, active_only: bool = False) -> list[dict[str, Any]]:
        with self._job_lock:
            return [
                dict(job)
                for job in self._crop_estimate_jobs.values()
                if not active_only or job.get("status") in {"queued", "running"}
            ]

    def latest_crop_estimate_job(self, active_only: bool = False) -> dict[str, Any] | None:
        with self._job_lock:
            job = self._latest_crop_estimate_job_locked(active_only=active_only)
            return dict(job) if job else None

    def _latest_crop_estimate_job_locked(self, active_only: bool = False) -> dict[str, Any] | None:
        jobs = [
            job
            for job in self._crop_estimate_jobs.values()
            if not active_only or job.get("status") in {"queued", "running"}
        ]
        return jobs[-1] if jobs else None

    def add_linked_candidate(
        self,
        entry_id: str,
        candidate_path: str,
        associate: bool = False,
    ) -> dict[str, Any]:
        original_path = Path(candidate_path).expanduser()
        if not original_path.is_absolute():
            raise ValueError("The photo must have an absolute path.")
        original_path = original_path.resolve()
        if not original_path.exists() or not original_path.is_file():
            raise ValueError("The selected photo does not exist.")
        if original_path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError("Choose one supported image file.")
        with self._lock:
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                raise ValueError("Unknown entry")
            entry_date = entry_rows[0].get("entry_date", "")
            try:
                dt.date.fromisoformat(entry_date)
            except ValueError as exc:
                raise ValueError("Entry has an invalid date") from exc
            original_path_text = str(original_path)
            if associate and self._database_crop_candidate_exists(entry_id, original_path_text):
                raise ValueError("This photo is already the selected original.")
            if not any(row.get("candidate_path") == original_path_text for row in entry_rows):
                template = entry_rows[0]
                content_type = mimetypes.guess_type(original_path.name)[0] or "application/octet-stream"
                values = {
                    "entry_id": entry_id,
                    "entry_date": entry_date,
                    "project365_media_asset_id": template.get("project365_media_asset_id", ""),
                    "current_match_status": template.get("current_match_status", ""),
                    "current_decision": template.get("current_decision", ""),
                    "candidate_path": original_path_text,
                    "candidate_filename": original_path.name,
                    "candidate_sha256": _sha256_path(original_path),
                    "byte_size": str(original_path.stat().st_size),
                    "mime_type": content_type,
                    "filename_dates": "",
                    "media_creation_dates": "",
                    "filesystem_dates": "",
                    "evidence": "manual_link",
                    "candidate_filter_reason": "",
                    "review_decision": "",
                    "review_notes": "",
                }
                self._store_added_candidate(entry_id, template, values)
        if associate:
            return self.save_decision(
                entry_id=entry_id,
                candidate_path=original_path_text,
                decision="external_original_associated_photo",
                notes="Linked as an associated attachment.",
                associated_entry_date=entry_date,
                associated_date_source="entry",
            )
        return self.entry_detail(entry_id) or {}

    def add_copied_candidate(
        self,
        entry_id: str,
        filename: str,
        content_type: str,
        payload: bytes,
        include_candidates: bool = True,
        associate: bool = False,
    ) -> dict[str, Any]:
        safe_name = Path(filename.replace("\\", "/")).name
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in DROP_IMAGE_EXTENSIONS:
            raise ValueError("Drop one supported image file.")
        if not payload:
            raise ValueError("Dropped image is empty.")
        if len(payload) > MAX_DROP_BYTES:
            raise ValueError("Dropped image exceeds the 250 MB limit.")
        digest = hashlib.sha256(payload).hexdigest()
        stored_suffix = ".heic" if suffix in DROP_CONVERT_TO_HEIC_EXTENSIONS else suffix
        with self._lock:
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                raise ValueError("Unknown entry")
            entry_date = entry_rows[0].get("entry_date", "")
            try:
                parsed_date = dt.date.fromisoformat(entry_date)
            except ValueError as exc:
                raise ValueError("Entry has an invalid date") from exc
            target_dir = (
                self.config.canonical_root.parent
                / "Source Data"
                / "Original Photos matching Project365 Entries"
                / parsed_date.strftime("%Y-%m")
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            timestamp = dt.datetime.combine(parsed_date, PROJECT_ADDITION_DROP_TIME)
            input_temp_path = target_dir / f".{parsed_date.isoformat()} {digest[:12]}{suffix}"
            converted_temp_path = target_dir / f".{parsed_date.isoformat()} {digest[:12]}.heic"
            working_path = input_temp_path
            try:
                input_temp_path.write_bytes(payload)
                if suffix in DROP_CONVERT_TO_HEIC_EXTENSIONS:
                    _convert_dropped_photo_to_heic(input_temp_path, converted_temp_path)
                    input_temp_path.unlink(missing_ok=True)
                    working_path = converted_temp_path
                _set_project_addition_timestamps(working_path, timestamp)
                stored_digest = _sha256_path(working_path)
                destination = _project_addition_destination(target_dir, parsed_date, stored_suffix, stored_digest)
                if destination.exists():
                    if _sha256_path(destination) != stored_digest:
                        destination = _project_addition_destination(
                            target_dir,
                            parsed_date,
                            stored_suffix,
                            stored_digest,
                            force_digest_suffix=True,
                        )
                    if destination.exists():
                        working_path.unlink()
                    else:
                        working_path.replace(destination)
                else:
                    working_path.replace(destination)
            finally:
                input_temp_path.unlink(missing_ok=True)
                converted_temp_path.unlink(missing_ok=True)
            destination = destination.resolve()
            destination_text = str(destination)
            metadata_values = _dropped_photo_metadata_values(destination, entry_date)
            if not any(row.get("candidate_path") == destination_text for row in entry_rows):
                template = entry_rows[0]
                mime_type = "image/heic" if suffix in DROP_CONVERT_TO_HEIC_EXTENSIONS else ""
                mime_type = mime_type or (content_type if content_type.startswith("image/") else "")
                mime_type = mime_type or mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
                values = {
                    "entry_id": entry_id,
                    "entry_date": entry_date,
                    "project365_media_asset_id": template.get("project365_media_asset_id", ""),
                    "current_match_status": template.get("current_match_status", ""),
                    "current_decision": template.get("current_decision", ""),
                    "candidate_path": destination_text,
                    "candidate_filename": destination.name,
                    "candidate_sha256": _sha256_path(destination),
                    "byte_size": str(destination.stat().st_size),
                    "mime_type": mime_type,
                    "filename_dates": entry_date,
                    "media_creation_dates": metadata_values.get("media_creation_dates", ""),
                    "filesystem_dates": entry_date,
                    "capture_timestamp": metadata_values.get("capture_timestamp", ""),
                    "capture_timestamp_source": metadata_values.get("capture_timestamp_source", ""),
                    "gps_latitude": metadata_values.get("gps_latitude", ""),
                    "gps_longitude": metadata_values.get("gps_longitude", ""),
                    "gps_source": metadata_values.get("gps_source", ""),
                    "has_gps": metadata_values.get("has_gps", ""),
                    "date_distance": metadata_values.get("date_distance", ""),
                    "evidence": "manual_drop_copy",
                    "candidate_filter_reason": "",
                    "review_decision": "",
                    "review_notes": "",
                }
                self._store_added_candidate(entry_id, template, values)
        if associate:
            return self.save_decision(
                entry_id=entry_id,
                candidate_path=destination_text,
                decision="external_original_associated_photo",
                notes="Dropped photo linked as an associated attachment.",
                include_candidates=include_candidates,
                associated_entry_date=entry_date,
                associated_date_source="entry",
            )
        return self.save_decision(
            entry_id=entry_id,
            candidate_path=destination_text,
            decision="use_external_original",
            notes="Dropped photo accepted automatically.",
            include_candidates=include_candidates,
        )

    def prepare_video_frame_choices(
        self,
        entry_id: str,
        filename: str,
        upload_path: Path,
        content_type: str = "",
    ) -> dict[str, Any]:
        safe_name = video_drop_filename(filename, content_type)
        suffix = Path(safe_name).suffix.lower()
        if suffix not in VIDEO_DROP_EXTENSIONS:
            raise ValueError("Drop one supported video file.")
        with self._lock:
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                raise ValueError("Unknown entry")
            entry_date = entry_rows[0].get("entry_date", "")
            try:
                dt.date.fromisoformat(entry_date)
            except ValueError as exc:
                raise ValueError("Entry has an invalid date") from exc
        session_id = uuid.uuid4().hex
        frame_dir = self.config.canonical_root / "cache" / "picker_video_frames" / session_id
        frame_dir.mkdir(parents=True, exist_ok=True)
        video_path = frame_dir / safe_name
        try:
            upload_path.replace(video_path)
            try:
                extracted = _extract_video_frames(video_path, frame_dir, VIDEO_FRAME_COUNT)
                capture_timestamp, capture_timestamp_source = _video_capture_timestamp(video_path)
            except Exception:
                shutil.rmtree(frame_dir, ignore_errors=True)
                raise
        finally:
            upload_path.unlink(missing_ok=True)
        if not extracted:
            shutil.rmtree(frame_dir, ignore_errors=True)
            raise ValueError("Could not extract screenshots from the dropped video.")
        frames: list[dict[str, Any]] = []
        with self._lock:
            video_token = self._image_token(video_path)
            for index, (time_seconds, frame_path) in enumerate(extracted, start=1):
                token = self._image_token(frame_path)
                frame_id = f"frame-{index:02d}"
                frames.append(
                    {
                        "frame_id": frame_id,
                        "token": token,
                        "time_seconds": round(time_seconds, 3),
                        "label": _format_frame_time(time_seconds),
                        "path": str(frame_path),
                    }
                )
            self._video_frame_sets[session_id] = {
                "entry_id": entry_id,
                "entry_date": entry_date,
                "filename": safe_name,
                "video_path": str(video_path),
                "video_token": video_token,
                "duration_seconds": _video_duration_seconds(video_path),
                "capture_timestamp": capture_timestamp,
                "capture_timestamp_source": capture_timestamp_source,
                "frames": frames,
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
                "frame_dir": str(frame_dir),
            }
        return {
            "kind": "video_frame_choices",
            "entry_id": entry_id,
            "entry_date": entry_date,
            "filename": safe_name,
            "session_id": session_id,
            "video_token": self._video_frame_sets[session_id]["video_token"],
            "duration_seconds": self._video_frame_sets[session_id]["duration_seconds"],
            "capture_timestamp": self._video_frame_sets[session_id]["capture_timestamp"],
            "capture_timestamp_source": self._video_frame_sets[session_id]["capture_timestamp_source"],
            "frames": [
                {key: frame[key] for key in ("frame_id", "token", "time_seconds", "label")}
                for frame in frames
            ],
        }

    def extract_video_frame_at_time(
        self,
        entry_id: str,
        session_id: str,
        time_seconds: float,
    ) -> dict[str, Any]:
        if time_seconds < 0:
            raise ValueError("Choose a time inside the movie.")
        with self._lock:
            frame_set = self._video_frame_sets.get(session_id)
            if not frame_set or frame_set.get("entry_id") != entry_id:
                raise ValueError("Unknown video screenshot session.")
            frames = list(frame_set.get("frames", []))
            frame_dir = Path(str(frame_set.get("frame_dir", "")))
            video_path = Path(str(frame_set.get("video_path", "")))
            duration = float(frame_set.get("duration_seconds") or 0.0)
        if duration > 0:
            time_seconds = min(time_seconds, duration)
        rounded_time = round(time_seconds, 3)
        if any(round(float(frame.get("time_seconds") or 0.0), 3) == rounded_time for frame in frames):
            raise ValueError(
                f"A screenshot at {_format_frame_time(rounded_time)} is already in the grid. "
                "Move the scrubber slightly to capture a different frame."
            )
        frame_id = f"custom-{len(frames) + 1:02d}"
        frame_path = frame_dir / f"{frame_id}.jpg"
        _extract_video_frame_at_time(video_path, frame_path, rounded_time)
        frame = {
            "frame_id": frame_id,
            "token": self._image_token(frame_path),
            "time_seconds": rounded_time,
            "label": _format_frame_time(rounded_time),
            "path": str(frame_path),
            "custom": True,
        }
        with self._lock:
            current = self._video_frame_sets.get(session_id)
            if not current or current.get("entry_id") != entry_id:
                frame_path.unlink(missing_ok=True)
                raise ValueError("Unknown video screenshot session.")
            current.setdefault("frames", []).append(frame)
        return {
            "frame": {
                key: frame[key]
                for key in ("frame_id", "token", "time_seconds", "label", "custom")
            }
        }

    def import_video_frames(
        self,
        entry_id: str,
        session_id: str,
        main_frame_id: str = "",
        supplemental_frame_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        supplemental_ids = [str(value) for value in (supplemental_frame_ids or [])]
        selected_ids = ([main_frame_id] if main_frame_id else []) + supplemental_ids
        selected_ids = [value for value in selected_ids if value]
        if not selected_ids:
            raise ValueError("Choose at least one screenshot.")
        with self._lock:
            frame_set = self._video_frame_sets.get(session_id)
            if not frame_set or frame_set.get("entry_id") != entry_id:
                raise ValueError("Unknown video screenshot session.")
            frames_by_id = {
                str(frame.get("frame_id", "")): dict(frame)
                for frame in frame_set.get("frames", [])
            }
            video_name = str(frame_set.get("filename", "video"))
        detail: dict[str, Any] = {}
        try:
            used_supplemental_ids = [value for value in supplemental_ids if value != main_frame_id]
            if main_frame_id:
                frame = frames_by_id.get(main_frame_id)
                if not frame:
                    raise ValueError("Unknown main screenshot.")
                frame_path = Path(str(frame.get("path", "")))
                detail = self.add_copied_candidate(
                    entry_id=entry_id,
                    filename=_video_frame_filename(video_name, frame),
                    content_type="image/jpeg",
                    payload=frame_path.read_bytes(),
                    include_candidates=False,
                    associate=False,
                )
            for frame_id in used_supplemental_ids:
                frame = frames_by_id.get(frame_id)
                if not frame:
                    raise ValueError("Unknown supplemental screenshot.")
                frame_path = Path(str(frame.get("path", "")))
                detail = self.add_copied_candidate(
                    entry_id=entry_id,
                    filename=_video_frame_filename(video_name, frame),
                    content_type="image/jpeg",
                    payload=frame_path.read_bytes(),
                    include_candidates=True,
                    associate=True,
                )
        finally:
            self.discard_video_frame_choices(entry_id, session_id, wait=False)
        return self.entry_detail(entry_id) or detail or {}

    def discard_video_frame_choices(self, entry_id: str, session_id: str, wait: bool = True) -> None:
        with self._lock:
            frame_set = self._video_frame_sets.pop(session_id, None)
            if not frame_set or frame_set.get("entry_id") != entry_id:
                return
            frame_dir = Path(str(frame_set.get("frame_dir", "")))
        if frame_dir:
            if wait:
                shutil.rmtree(frame_dir, ignore_errors=True)
            else:
                _remove_tree_in_background(frame_dir)

    def _unused_project_addition_paths_for_entries(self) -> list[Path]:
        paths: list[Path] = []
        for _, rows in self._iter_entry_groups():
            if not any(
                row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS | ASSOCIATED_PHOTO_DECISIONS
                for row in rows
            ):
                continue
            paths.extend(self._unused_project_addition_paths_from_rows(rows))
        return _unique_paths(paths)

    def _unused_project_addition_paths_from_rows(self, rows: list[dict[str, str]]) -> list[Path]:
        paths: list[Path] = []
        project_originals_root = project_originals_source_path(self.config.canonical_root)
        for row in rows:
            decision = row.get("review_decision", "").strip().lower()
            if decision in ACCEPT_DECISIONS or decision in ASSOCIATED_PHOTO_DECISIONS:
                continue
            path_text = row.get("candidate_path", "").strip()
            if not path_text:
                continue
            candidate_path = Path(path_text).expanduser()
            try:
                candidate_path.resolve().relative_to(project_originals_root)
            except (OSError, ValueError):
                continue
            evidence = row.get("evidence", "").strip().lower()
            filename = row.get("candidate_filename", "") or candidate_path.name
            if "manual_drop_copy" not in evidence and PROJECT_ADDITION_FILENAME_MARKER not in filename:
                continue
            paths.append(candidate_path)
        return _unique_paths(paths)

    def _delete_paths(self, paths: list[Path]) -> int:
        deleted = 0
        for path in _unique_paths(paths):
            try:
                if path.exists() and path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
        return deleted

    def apply_decisions(self) -> dict[str, Any]:
        self._prune_completed_queue_entries_if_needed()
        with self._lock:
            unused_project_additions = self._unused_project_addition_paths_for_entries()
            self._materialize_decision_overrides()
            apply_summary = apply_reviewed_external_references(
                canonical_root=self.config.canonical_root,
                reviewed_csv=self.config.queue_path,
            )
            prune_summary = prune_applied_review_queue(
                canonical_root=self.config.canonical_root,
                queue_path=self.config.queue_path,
                report_dir=self.config.queue_path.parent,
            )
            self._entry_rows_cache = {}
            self._queue_shards.invalidate()
            self._completed_external_entry_ids_cache = None
            self._completed_external_reference_entry_ids_cache = None
            self._completed_queue_prune_signature = None
            deleted_unused_project_additions = self._delete_paths(unused_project_additions)
        self._source_media = self._load_source_media()
        self._refresh_image_paths()
        return {
            "selected_count": apply_summary.selected_count,
            "rejected_count": apply_summary.rejected_count,
            "fallback_count": apply_summary.fallback_count,
            "associated_count": apply_summary.associated_count,
            "applied_count": apply_summary.applied_count,
            "skipped_unknown_media_count": apply_summary.skipped_unknown_media_count,
            "remaining_queue_rows": prune_summary["queue_rows"],
            "remaining_entries": prune_summary["entry_count"],
            "removed_completed_entries": prune_summary["removed_completed_entries"],
            "removed_unknown_media_entries": prune_summary["removed_unknown_media_entries"],
            "removed_rejected_candidates": prune_summary["removed_rejected_candidates"],
            "deleted_unused_project_additions": deleted_unused_project_additions,
        }

    def commit_entry_decision(self, entry_id: str) -> dict[str, Any]:
        self._prune_completed_queue_entries_if_needed()
        with self._lock:
            fieldnames, _rows = self._read_rows_with_fieldnames()
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                raise ValueError("Unknown entry")
            if not any(
                row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
                for row in entry_rows
            ):
                raise ValueError("No linked original is pending for this target")
            unused_project_additions = self._unused_project_addition_paths_from_rows(entry_rows)
            temp_path = self.config.queue_path.with_suffix(f".{entry_id.replace(':', '_')}.commit.csv")
            with temp_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(_rows_for_fieldnames(entry_rows, fieldnames))
            try:
                apply_summary = apply_reviewed_external_references(
                    canonical_root=self.config.canonical_root,
                    reviewed_csv=temp_path,
                )
            finally:
                temp_path.unlink(missing_ok=True)
            self._decision_overrides.pop(entry_id, None)
            self._added_candidate_rows.pop(entry_id, None)
            self._replacement_candidate_rows.pop(entry_id, None)
            self._persist_decision_overrides()
            prune_summary = prune_applied_review_queue(
                canonical_root=self.config.canonical_root,
                queue_path=self.config.queue_path,
                report_dir=self.config.queue_path.parent,
            )
            self._entry_rows_cache = {}
            self._queue_shards.invalidate()
            self._completed_external_entry_ids_cache = None
            self._completed_external_reference_entry_ids_cache = None
            self._completed_queue_prune_signature = None
            deleted_unused_project_additions = self._delete_paths(unused_project_additions)
        self._source_media = self._load_source_media()
        self._refresh_image_paths()
        return {
            "selected_count": apply_summary.selected_count,
            "rejected_count": apply_summary.rejected_count,
            "fallback_count": apply_summary.fallback_count,
            "associated_count": apply_summary.associated_count,
            "applied_count": apply_summary.applied_count,
            "skipped_unknown_media_count": apply_summary.skipped_unknown_media_count,
            "remaining_queue_rows": prune_summary["queue_rows"],
            "remaining_entries": prune_summary["entry_count"],
            "removed_completed_entries": prune_summary["removed_completed_entries"],
            "removed_unknown_media_entries": prune_summary["removed_unknown_media_entries"],
            "removed_rejected_candidates": prune_summary["removed_rejected_candidates"],
            "deleted_unused_project_additions": deleted_unused_project_additions,
        }

    def expand_date_range(
        self,
        entry_id: str,
        days: int,
        photo_index_folder: str | Path | None = None,
        search_whole_index: bool = False,
        whole_index_filename_only: bool = False,
        filename_dates_only: bool = False,
        include_modified_dates: bool = False,
    ) -> dict[str, Any]:
        if days not in EXPAND_DATE_RANGE_DAYS:
            raise ValueError("Date range must be 1, 3, 5, 15, or 30 days.")
        effective_search_whole_index = bool(search_whole_index)
        folder_root = None
        effective_filename_only = True if not effective_search_whole_index else bool(whole_index_filename_only or filename_dates_only)
        effective_include_modified_dates = bool(include_modified_dates and effective_search_whole_index and not effective_filename_only)
        with self._lock:
            replace_candidates = bool(search_whole_index)
            if not replace_candidates:
                self._clear_replacement_candidates(entry_id)
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                raise ValueError("Unknown entry")
            entry_date = entry_rows[0].get("entry_date", "")
            dt.date.fromisoformat(entry_date)
            target_dates = _date_window(entry_date, days)
            indexed = query_index_candidates(
                default_index_db(self.config.canonical_root),
                {entry_date},
                max_distance_days=days,
                folder_root=folder_root,
                include_filesystem_dates=not effective_filename_only,
                filename_dates_only=effective_filename_only,
                include_modified_dates=effective_include_modified_dates,
            ).get(entry_date, [])
            existing_paths = set() if replace_candidates else {
                row.get("candidate_path", "")
                for row in entry_rows
                if row.get("candidate_path", "")
            }
            rejected_hashes = self._rejected_candidate_hashes(entry_id)
            template = entry_rows[0]
            additions: list[dict[str, Any]] = []
            for candidate in indexed:
                path = Path(str(candidate.get("candidate_path", ""))).resolve()
                path_text = str(path)
                if not path_text or path_text in existing_paths or not path.exists():
                    continue
                if not effective_search_whole_index and not _candidate_filename_starts_with_any_date(candidate, target_dates):
                    continue
                digest = str(candidate.get("candidate_sha256", "")).strip() or _sha256_path(path)
                if digest in rejected_hashes:
                    continue
                byte_size = candidate.get("byte_size", "")
                if byte_size in (None, ""):
                    byte_size = path.stat().st_size
                evidence_parts = [part for part in str(candidate.get("evidence", "")).strip(";").split(";") if part]
                evidence_parts.append(f"manual_range_{days}_days")
                evidence_parts.append(
                    "manual_range_scope_whole_index"
                    if effective_search_whole_index
                    else "manual_range_scope_filename_index"
                )
                if effective_filename_only:
                    evidence_parts.append("manual_range_date_source_filename_only")
                if effective_include_modified_dates:
                    evidence_parts.append("manual_range_date_source_modified_date")
                evidence = ";".join(evidence_parts)
                values = {
                    "entry_id": entry_id,
                    "entry_date": entry_date,
                    "project365_media_asset_id": template.get("project365_media_asset_id", ""),
                    "current_match_status": template.get("current_match_status", ""),
                    "current_decision": template.get("current_decision", ""),
                    "candidate_path": path_text,
                    "candidate_filename": candidate.get("candidate_filename", path.name),
                    "candidate_sha256": digest,
                    "byte_size": str(byte_size),
                    "mime_type": candidate.get("mime_type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"),
                    "filename_dates": candidate.get("filename_dates", ""),
                    "media_creation_dates": candidate.get("media_creation_dates", ""),
                    "filesystem_dates": candidate.get("filesystem_dates", ""),
                    "capture_timestamp": candidate.get("capture_timestamp", ""),
                    "capture_timestamp_source": candidate.get("capture_timestamp_source", ""),
                    "gps_latitude": candidate.get("gps_latitude", ""),
                    "gps_longitude": candidate.get("gps_longitude", ""),
                    "gps_source": candidate.get("gps_source", ""),
                    "has_gps": candidate.get("has_gps", ""),
                    "date_distance": candidate.get("date_distance", ""),
                    "evidence": evidence,
                    "candidate_filter_reason": candidate.get("candidate_filter_reason", ""),
                    "review_decision": "",
                    "review_notes": "",
                }
                additions.append(values)
                existing_paths.add(path_text)
            if replace_candidates:
                self._store_replacement_candidates(entry_id, template, additions)
            else:
                self._store_added_candidates(entry_id, template, additions)
            record_reject_all_range_state(
                self.config.queue_path.parent,
                entry_id,
                last_search_range_days=days,
                manual_search_required=False,
                message="",
                photo_index_folder=folder_root,
            )
        detail = self.entry_detail(entry_id, rank_if_needed=False) or {}
        return {
            "entry": detail,
            "range_days": days,
            "search_whole_index": effective_search_whole_index,
            "filename_dates_only": effective_filename_only,
            "include_modified_dates": effective_include_modified_dates,
            "whole_index_filename_only": bool(effective_search_whole_index and effective_filename_only),
            "replace_candidates": replace_candidates,
            "photo_index_folder": str(folder_root) if folder_root else "",
            "added_count": len(additions),
            "candidate_count": detail.get("candidate_count", 0),
        }

    def expand_default_date_range(
        self,
        entry_id: str,
        photo_index_folder: str | Path | None = None,
        search_whole_index: bool = False,
        whole_index_filename_only: bool = False,
        filename_dates_only: bool = False,
        include_modified_dates: bool = False,
    ) -> dict[str, Any]:
        effective_search_whole_index = bool(search_whole_index)
        folder_root = None
        effective_filename_only = True if not effective_search_whole_index else bool(whole_index_filename_only or filename_dates_only)
        effective_include_modified_dates = bool(include_modified_dates and effective_search_whole_index and not effective_filename_only)
        with self._lock:
            replace_candidates = bool(search_whole_index)
            if not replace_candidates:
                self._clear_replacement_candidates(entry_id)
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                raise ValueError("Unknown entry")
            entry_date = entry_rows[0].get("entry_date", "")
            dt.date.fromisoformat(entry_date)
            target_dates = {entry_date}
            indexed = query_index_candidates(
                default_index_db(self.config.canonical_root),
                {entry_date},
                folder_root=folder_root,
                include_filesystem_dates=not effective_filename_only,
                filename_dates_only=effective_filename_only,
                include_modified_dates=effective_include_modified_dates,
            ).get(entry_date, [])
            existing_paths = set() if replace_candidates else {
                row.get("candidate_path", "")
                for row in entry_rows
                if row.get("candidate_path", "")
            }
            rejected_hashes = self._rejected_candidate_hashes(entry_id)
            template = entry_rows[0]
            additions: list[dict[str, Any]] = []
            for candidate in indexed:
                path = Path(str(candidate.get("candidate_path", ""))).resolve()
                path_text = str(path)
                if not path_text or path_text in existing_paths or not path.exists():
                    continue
                if not effective_search_whole_index and not _candidate_filename_starts_with_any_date(candidate, target_dates):
                    continue
                digest = str(candidate.get("candidate_sha256", "")).strip() or _sha256_path(path)
                if digest in rejected_hashes:
                    continue
                byte_size = candidate.get("byte_size", "")
                if byte_size in (None, ""):
                    byte_size = path.stat().st_size
                evidence_parts = [part for part in str(candidate.get("evidence", "")).strip(";").split(";") if part]
                evidence_parts.append("manual_default_search")
                evidence_parts.append(
                    "manual_default_scope_whole_index"
                    if effective_search_whole_index
                    else "manual_default_scope_filename_index"
                )
                if effective_filename_only:
                    evidence_parts.append("manual_default_date_source_filename_only")
                if effective_include_modified_dates:
                    evidence_parts.append("manual_default_date_source_modified_date")
                values = {
                    "entry_id": entry_id,
                    "entry_date": entry_date,
                    "project365_media_asset_id": template.get("project365_media_asset_id", ""),
                    "current_match_status": template.get("current_match_status", ""),
                    "current_decision": template.get("current_decision", ""),
                    "candidate_path": path_text,
                    "candidate_filename": candidate.get("candidate_filename", path.name),
                    "candidate_sha256": digest,
                    "byte_size": str(byte_size),
                    "mime_type": candidate.get("mime_type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"),
                    "filename_dates": candidate.get("filename_dates", ""),
                    "media_creation_dates": candidate.get("media_creation_dates", ""),
                    "filesystem_dates": candidate.get("filesystem_dates", ""),
                    "capture_timestamp": candidate.get("capture_timestamp", ""),
                    "capture_timestamp_source": candidate.get("capture_timestamp_source", ""),
                    "gps_latitude": candidate.get("gps_latitude", ""),
                    "gps_longitude": candidate.get("gps_longitude", ""),
                    "gps_source": candidate.get("gps_source", ""),
                    "has_gps": candidate.get("has_gps", ""),
                    "date_distance": candidate.get("date_distance", ""),
                    "evidence": ";".join(evidence_parts),
                    "candidate_filter_reason": candidate.get("candidate_filter_reason", ""),
                    "review_decision": "",
                    "review_notes": "",
                }
                additions.append(values)
                existing_paths.add(path_text)
            if replace_candidates:
                self._store_replacement_candidates(entry_id, template, additions)
            else:
                self._store_added_candidates(entry_id, template, additions)
        detail = self.entry_detail(entry_id, rank_if_needed=False) or {}
        return {
            "entry": detail,
            "search_whole_index": effective_search_whole_index,
            "filename_dates_only": effective_filename_only,
            "include_modified_dates": effective_include_modified_dates,
            "whole_index_filename_only": bool(effective_search_whole_index and effective_filename_only),
            "replace_candidates": replace_candidates,
            "photo_index_folder": str(folder_root) if folder_root else "",
            "added_count": len(additions),
            "candidate_count": detail.get("candidate_count", 0),
        }

    def search_index_date_range(
        self,
        entry_id: str,
        start_date: str,
        end_date: str,
        search_whole_index: bool = False,
        photo_index_folder: str | Path | None = None,
        whole_index_filename_only: bool = False,
        filename_dates_only: bool = False,
        include_modified_dates: bool = False,
    ) -> dict[str, Any]:
        target_dates = _manual_index_search_dates(start_date, end_date)
        folder_root = None
        effective_filename_only = True if not search_whole_index else bool(whole_index_filename_only or filename_dates_only)
        effective_include_modified_dates = bool(include_modified_dates and search_whole_index and not effective_filename_only)
        with self._lock:
            replace_candidates = search_whole_index
            if not replace_candidates:
                self._clear_replacement_candidates(entry_id)
            entry_rows = self._entry_rows_for_manual_candidate(entry_id)
            if not entry_rows:
                raise ValueError("Unknown entry")
            entry_date = entry_rows[0].get("entry_date", "")
            entry_day = dt.date.fromisoformat(entry_date)
            indexed_by_date = query_index_candidates(
                default_index_db(self.config.canonical_root),
                set(target_dates),
                max_distance_days=0,
                folder_root=folder_root,
                include_filesystem_dates=not effective_filename_only,
                filename_dates_only=effective_filename_only,
                include_modified_dates=effective_include_modified_dates,
            )
            existing_paths = set() if replace_candidates else {
                row.get("candidate_path", "")
                for row in entry_rows
                if row.get("candidate_path", "")
            }
            rejected_hashes = self._rejected_candidate_hashes(entry_id)
            template = entry_rows[0]
            additions: list[dict[str, Any]] = []
            matched_dates: set[str] = set()
            for target_date in target_dates:
                for candidate in indexed_by_date.get(target_date, []):
                    path = Path(str(candidate.get("candidate_path", ""))).resolve()
                    path_text = str(path)
                    if not path_text or path_text in existing_paths or not path.exists():
                        continue
                    if not search_whole_index and not _candidate_filename_starts_with_any_date(candidate, set(target_dates)):
                        continue
                    digest = str(candidate.get("candidate_sha256", "")).strip() or _sha256_path(path)
                    if digest in rejected_hashes:
                        continue
                    byte_size = candidate.get("byte_size", "")
                    if byte_size in (None, ""):
                        byte_size = path.stat().st_size
                    evidence = str(candidate.get("evidence", "")).strip(";")
                    evidence = f"{evidence};manual_index_date_search" if evidence else "manual_index_date_search"
                    evidence = (
                        f"{evidence};manual_index_scope_whole_index"
                        if search_whole_index
                        else f"{evidence};manual_index_scope_filename_index"
                    )
                    if effective_filename_only:
                        evidence = f"{evidence};manual_index_date_source_filename_only"
                    if effective_include_modified_dates:
                        evidence = f"{evidence};manual_index_date_source_modified_date"
                    values = {
                        "entry_id": entry_id,
                        "entry_date": entry_date,
                        "project365_media_asset_id": template.get("project365_media_asset_id", ""),
                        "current_match_status": template.get("current_match_status", ""),
                        "current_decision": template.get("current_decision", ""),
                        "candidate_path": path_text,
                        "candidate_filename": candidate.get("candidate_filename", path.name),
                        "candidate_sha256": digest,
                        "byte_size": str(byte_size),
                        "mime_type": candidate.get(
                            "mime_type",
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        ),
                        "filename_dates": candidate.get("filename_dates", ""),
                        "media_creation_dates": candidate.get("media_creation_dates", ""),
                        "filesystem_dates": candidate.get("filesystem_dates", ""),
                        "capture_timestamp": candidate.get("capture_timestamp", ""),
                        "capture_timestamp_source": candidate.get("capture_timestamp_source", ""),
                        "gps_latitude": candidate.get("gps_latitude", ""),
                        "gps_longitude": candidate.get("gps_longitude", ""),
                        "gps_source": candidate.get("gps_source", ""),
                        "has_gps": candidate.get("has_gps", ""),
                        "date_distance": str(abs((dt.date.fromisoformat(target_date) - entry_day).days)),
                        "evidence": evidence,
                        "candidate_filter_reason": candidate.get("candidate_filter_reason", ""),
                        "review_decision": "",
                        "review_notes": "",
                    }
                    additions.append(values)
                    existing_paths.add(path_text)
                    matched_dates.add(target_date)
            if replace_candidates:
                self._store_replacement_candidates(entry_id, template, additions)
            else:
                self._store_added_candidates(entry_id, template, additions)
        detail = self.entry_detail(entry_id, rank_if_needed=False) or {}
        return {
            "entry": detail,
            "start_date": target_dates[0],
            "end_date": target_dates[-1],
            "date_count": len(target_dates),
            "matched_date_count": len(matched_dates),
            "search_whole_index": search_whole_index,
            "filename_dates_only": effective_filename_only,
            "include_modified_dates": effective_include_modified_dates,
            "whole_index_filename_only": bool(search_whole_index and effective_filename_only),
            "replace_candidates": replace_candidates,
            "photo_index_folder": "" if search_whole_index or folder_root is None else str(folder_root),
            "added_count": len(additions),
            "candidate_count": detail.get("candidate_count", 0),
        }

    def _rejected_candidate_hashes(self, entry_id: str) -> set[str]:
        with sqlite3.connect(self.config.canonical_root / "canonical.db") as connection:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(media_assets)")}
            if "sha256" not in columns:
                return set()
            return {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT sha256
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_rejected'
                        AND review_status = 'rejected'
                        AND sha256 != ''
                    """,
                    (entry_id,),
                ).fetchall()
            }

    def _current_search_range_days(self, entry_id: str, rows: list[dict[str, str]]) -> int:
        range_days = 0
        for row in rows:
            for match in BUTTON_RANGE_EVIDENCE_RE.finditer(str(row.get("evidence", ""))):
                range_days = max(range_days, int(match.group(1)))
        record = load_reject_all_range_state(self.config.queue_path.parent).get(entry_id, {})
        try:
            range_days = max(range_days, int(str(record.get("last_search_range_days", "")).strip()))
        except ValueError:
            pass
        return range_days

    def start_crawl(
        self,
        entry_ids: list[str],
        search_roots: list[str],
        scan_metadata_dates: bool = True,
    ) -> dict[str, Any]:
        roots = [Path(root).expanduser() for root in search_roots if str(root).strip()]
        if not roots:
            raise ValueError("At least one search folder is required")
        for root in roots:
            if not root.exists():
                raise ValueError(f"Missing search folder: {root}")
        targets = [entry_id.strip() for entry_id in entry_ids if entry_id.strip()]
        if not targets:
            targets = [entry["entry_id"] for entry in self.entries(status="needs_action")]
        if not targets:
            raise ValueError("No unmatched entries are available to crawl")
        job_id = hashlib.sha256(
            f"{dt.datetime.now(dt.UTC).isoformat()}:{targets}:{roots}".encode("utf-8")
        ).hexdigest()[:16]
        job = {
            "id": job_id,
            "status": "queued",
            "entry_count": len(targets),
            "candidate_count": 0,
            "search_roots": [str(root) for root in roots],
            "started_at": "",
            "finished_at": "",
            "error": "",
        }
        with self._job_lock:
            self._crawl_jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_crawl_job,
            args=(job_id, targets, roots, scan_metadata_dates),
            daemon=True,
        )
        thread.start()
        return job

    def crawl_job(self, job_id: str) -> dict[str, Any] | None:
        with self._job_lock:
            job = self._crawl_jobs.get(job_id)
            return dict(job) if job else None

    def _run_crop_estimate_batch(
        self,
        job_id: str,
        targets: list[dict[str, str]],
        apply_estimates: bool,
        refresh_saved_estimates: bool,
    ) -> None:
        self._update_crop_estimate_job(job_id, status="running", started_at=dt.datetime.now(dt.UTC).isoformat())
        try:
            for target in targets:
                entry_id = target["entry_id"]
                candidate_path = target["candidate_path"]
                self._update_crop_estimate_job(job_id, current_entry_id=entry_id)
                try:
                    if refresh_saved_estimates:
                        prior_crop = self._refreshable_saved_estimate_crop(entry_id, candidate_path)
                        if prior_crop is None:
                            self._increment_crop_estimate_job(job_id, "skipped_count")
                        else:
                            estimated_crop = self._estimated_crop_for_candidate(
                                entry_id,
                                candidate_path,
                                prior_crop,
                            )
                            if not _crop_values_differ(prior_crop, estimated_crop):
                                self._increment_crop_estimate_job(job_id, "unchanged_count")
                            elif not apply_estimates:
                                self._increment_crop_estimate_job(job_id, "changed_count")
                            elif self._refreshable_saved_estimate_crop(entry_id, candidate_path) is None:
                                self._increment_crop_estimate_job(job_id, "skipped_count")
                            else:
                                self.save_crop(
                                    entry_id,
                                    candidate_path,
                                    estimated_crop,
                                    source="estimated",
                                    commit_pending=False,
                                )
                                self._increment_crop_estimate_job(job_id, "changed_count")
                            self._increment_crop_estimate_job(job_id, "estimated_count")
                    elif self._crop_candidate_has_review_crop(entry_id, candidate_path):
                        self._increment_crop_estimate_job(job_id, "skipped_count")
                    else:
                        estimated_crop = self._estimated_crop_for_candidate(entry_id, candidate_path)
                        if apply_estimates:
                            self.save_crop(
                                entry_id,
                                candidate_path,
                                estimated_crop,
                                source="estimated",
                                commit_pending=False,
                            )
                        self._increment_crop_estimate_job(job_id, "estimated_count")
                except Exception as exc:  # noqa: BLE001 - batch should continue and report per-file errors.
                    self._append_crop_estimate_error(job_id, entry_id, candidate_path, str(exc))
                finally:
                    self._increment_crop_estimate_job(job_id, "processed_count")
            with self._job_lock:
                failed_count = int(self._crop_estimate_jobs[job_id].get("failed_count", 0))
            final_status = "fail" if failed_count else "pass"
        except Exception as exc:  # noqa: BLE001 - local batch jobs should surface readable errors.
            self._append_crop_estimate_error(job_id, "", "", str(exc))
            final_status = "fail"
        self._update_crop_estimate_job(
            job_id,
            status=final_status,
            current_entry_id="",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )

    def _crop_candidate_has_review_crop(self, entry_id: str, candidate_path: str) -> bool:
        detail = self.crop_entry_detail(entry_id, mark_estimated_viewed=False)
        if detail is None:
            return False
        for candidate in detail.get("candidates", []):
            if str(candidate.get("path", "")) == candidate_path:
                return _candidate_has_review_crop(candidate)
        return False

    def _refreshable_saved_estimate_crop(
        self,
        entry_id: str,
        candidate_path: str,
    ) -> dict[str, object] | None:
        record = self._staged_crop_record(entry_id, candidate_path)
        if record is not None and self._staged_crop_commit_pending(record):
            return None
        detail = self.crop_entry_detail(entry_id, mark_estimated_viewed=False)
        if detail is None:
            return None
        for candidate in detail.get("candidates", []):
            if str(candidate.get("path", "")) != candidate_path:
                continue
            if str(candidate.get("review_crop_source", "")).strip().lower() != "estimated":
                return None
            crop = _review_crop_from_candidate(candidate)
            if crop is None:
                return None
            crop["source"] = "estimated"
            return crop
        return None

    def _update_crop_estimate_job(self, job_id: str, **updates: Any) -> None:
        with self._job_lock:
            job = self._crop_estimate_jobs[job_id]
            job.update(updates)
            job["last_update_at"] = dt.datetime.now(dt.UTC).isoformat()

    def _increment_crop_estimate_job(self, job_id: str, key: str) -> None:
        with self._job_lock:
            job = self._crop_estimate_jobs[job_id]
            job[key] = int(job.get(key, 0)) + 1
            job["last_update_at"] = dt.datetime.now(dt.UTC).isoformat()

    def _append_crop_estimate_error(
        self,
        job_id: str,
        entry_id: str,
        candidate_path: str,
        error: str,
    ) -> None:
        with self._job_lock:
            job = self._crop_estimate_jobs[job_id]
            job["failed_count"] = int(job.get("failed_count", 0)) + 1
            job["last_update_at"] = dt.datetime.now(dt.UTC).isoformat()
            errors = job.setdefault("errors", [])
            if len(errors) < 20:
                errors.append(
                    {
                        "entry_id": entry_id,
                        "candidate_path": candidate_path,
                        "error": error,
                    }
                )

    def _entry_summary(self, entry_id: str, entry_rows: list[dict[str, str]]) -> dict[str, Any]:
        first = entry_rows[0]
        source_path = self._source_media.get(entry_id)
        status = _entry_status(entry_rows)
        candidate_rows = [
            row
            for row in entry_rows
            if row.get("candidate_path", "").strip() and not _is_hidden_candidate_row(row)
        ]
        selected_rows = [
            row
            for row in candidate_rows
            if row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
        ]
        database_selected_rows = [
            row
            for row in candidate_rows
            if row.get("database_confirmed_role", "").strip().lower() == "external_original_reference"
            and row.get("review_decision", "").strip().lower() not in ACCEPT_DECISIONS
        ]
        associated_rows = [
            row
            for row in candidate_rows
            if row.get("review_decision", "").strip().lower() in ASSOCIATED_PHOTO_DECISIONS
            and row.get("database_confirmed_role", "").strip().lower() != "external_original_reference"
        ]
        fallback_rows = [
            row
            for row in entry_rows
            if row.get("review_decision", "").strip().lower() in FALLBACK_DECISIONS
        ]
        rejected_rows = [
            row
            for row in candidate_rows
            if row.get("review_decision", "").strip().lower() == "rejected"
        ]
        remaining_candidate_count = len(candidate_rows) - len(rejected_rows)
        manual_search_message = _manual_search_message(entry_rows)
        return {
            "entry_id": entry_id,
            "entry_date": first.get("entry_date", ""),
            "status": status,
            "candidate_count": remaining_candidate_count,
            "selected_count": len(selected_rows) + len(database_selected_rows),
            "pending_commit_count": len(selected_rows),
            "commit_ready": bool(selected_rows),
            "source_token": self._image_token(source_path) if source_path else "",
            "source_exists": bool(source_path and source_path.exists()),
            "source_byte_size": _file_byte_size(source_path),
            "source_file_type": _file_type_label(source_path),
            "source_dimensions": _image_dimensions(source_path),
            "people_names": self._entry_people(entry_id),
            "current_match_status": first.get("current_match_status", ""),
            "current_decision": first.get("current_decision", ""),
            "manual_search_message": manual_search_message,
            "accepted_count": len(selected_rows) + len(database_selected_rows),
            "associated_count": len(associated_rows),
            "rejected_count": len(rejected_rows),
            "fallback_count": len(fallback_rows),
            "used_range_days": _used_search_range_days(
                entry_id,
                entry_rows,
                self.config.queue_path.parent,
            ),
        }

    def _entry_summary_from_index(self, record: dict[str, Any]) -> dict[str, Any]:
        entry_id = str(record.get("entry_id", ""))
        source_path = self._source_media.get(entry_id)
        pending_commit_count = int(record.get("pending_commit_count", record.get("selected_count", 0)))
        return {
            "entry_id": entry_id,
            "entry_date": str(record.get("entry_date", "")),
            "status": str(record.get("status", "search_needed")),
            "candidate_count": int(record.get("candidate_count", 0)),
            "selected_count": int(record.get("selected_count", 0)),
            "pending_commit_count": pending_commit_count,
            "commit_ready": bool(record["commit_ready"]) if "commit_ready" in record else pending_commit_count > 0,
            "source_token": self._image_token(source_path) if source_path else "",
            "source_exists": bool(source_path and source_path.exists()),
            "source_byte_size": _file_byte_size(source_path),
            "source_file_type": _file_type_label(source_path),
            "source_dimensions": _image_dimensions(source_path),
            "people_names": self._entry_people(entry_id),
            "current_match_status": str(record.get("current_match_status", "")),
            "current_decision": str(record.get("current_decision", "")),
            "manual_search_message": str(record.get("manual_search_message", "")),
        }

    def _entry_people(self, entry_id: str) -> list[str]:
        db_path = self.config.canonical_root / "canonical.db"
        try:
            stat = db_path.stat()
        except OSError:
            return []
        signature = (stat.st_size, stat.st_mtime_ns)
        if self._people_cache_signature != signature:
            self._people_by_entry = self._load_people_by_entry(db_path)
            self._people_cache_signature = signature
        return list(self._people_by_entry.get(entry_id, []))

    def _load_people_by_entry(self, db_path: Path) -> dict[str, list[str]]:
        people: dict[str, list[str]] = {}
        connection = sqlite3.connect(db_path)
        try:
            rows = connection.execute(
                """
                SELECT entry_id, canonical_name
                FROM people
                WHERE review_status IN ('suggested', 'confirmed', 'reviewed')
                ORDER BY canonical_name
                """
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            connection.close()
        for entry_id, canonical_name in rows:
            name = str(canonical_name or "").strip()
            if name:
                people.setdefault(str(entry_id), []).append(name)
        return people

    def _image_token(self, path: Path | None) -> str:
        if path is None:
            return ""
        token = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        self._image_paths[token] = path
        return token

    def image_token_for_path(self, path: Path | None) -> str:
        return self._image_token(path)

    def _refresh_image_paths(self) -> None:
        self._image_paths = {}
        for source_path in self._source_media.values():
            self._image_token(source_path)
        for row in self._read_rows():
            candidate_path = row.get("candidate_path", "").strip()
            if candidate_path:
                self._image_token(Path(candidate_path))
        for frame_set in self._video_frame_sets.values():
            video_path = Path(str(frame_set.get("video_path", "")))
            if video_path:
                self._image_token(video_path)
            for frame in frame_set.get("frames", []):
                path = Path(str(frame.get("path", "")))
                if path:
                    self._image_token(path)

    def _load_source_media(self) -> dict[str, Path]:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            raise FileNotFoundError(f"Missing canonical database: {db_path}")
        connection = sqlite3.connect(db_path)
        try:
            rows = connection.execute(
                """
                SELECT entry_id, storage_path
                FROM media_assets
                WHERE role = 'project365_export_png'
                """
            ).fetchall()
        finally:
            connection.close()
        return {entry_id: Path(storage_path) for entry_id, storage_path in rows}

    def _entry_has_completed_external_decision(self, entry_id: str, include_fallback: bool = True) -> bool:
        if include_fallback:
            if self._completed_external_entry_ids_cache is None:
                self._completed_external_entry_ids_cache = self._load_completed_external_entry_ids(
                    include_fallback=True
                )
            return entry_id in self._completed_external_entry_ids_cache
        if self._completed_external_reference_entry_ids_cache is None:
            self._completed_external_reference_entry_ids_cache = self._load_completed_external_entry_ids(
                include_fallback=False
            )
        return entry_id in self._completed_external_reference_entry_ids_cache

    def _load_completed_external_entry_ids(self, include_fallback: bool = True) -> set[str]:
        db_path = self.config.canonical_root / "canonical.db"
        if not db_path.exists():
            return set()
        roles = ["external_original_reference"]
        if include_fallback:
            roles.append("external_original_fallback")
        placeholders = ", ".join("?" for _ in roles)
        connection = sqlite3.connect(db_path)
        try:
            rows = connection.execute(
                f"""
                SELECT DISTINCT entry_id
                FROM media_assets
                WHERE role IN ({placeholders})
                    AND review_status = 'confirmed'
                """,
                roles,
            ).fetchall()
        except sqlite3.Error:
            return set()
        finally:
            connection.close()
        return {str(row[0]) for row in rows if str(row[0] or "").strip()}

    def _completed_queue_signature(self) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        return (
            _file_signature(self.config.queue_path),
            _file_signature(self.config.canonical_root / "canonical.db"),
        )

    def _clear_local_review_state(self, entry_ids: set[str]) -> None:
        changed = False
        for entry_id in entry_ids:
            changed = self._decision_overrides.pop(entry_id, None) is not None or changed
            changed = self._added_candidate_rows.pop(entry_id, None) is not None or changed
            changed = self._replacement_candidate_rows.pop(entry_id, None) is not None or changed
        if changed:
            self._persist_decision_overrides()

    def _prune_completed_queue_entries_if_needed(self) -> dict[str, int | str]:
        signature = self._completed_queue_signature()
        if signature == self._completed_queue_prune_signature:
            return {}
        with self._lock:
            signature = self._completed_queue_signature()
            if signature == self._completed_queue_prune_signature:
                return {}
            self._completed_external_entry_ids_cache = None
            self._completed_external_reference_entry_ids_cache = None
            if not self.config.queue_path.exists():
                self._completed_queue_prune_signature = signature
                return {}
            queued_entry_ids = {
                str(record.get("entry_id", "")).strip()
                for record in self._queue_shards.summaries()
                if str(record.get("entry_id", "")).strip()
            }
            completed_queue_entry_ids = queued_entry_ids & self._load_completed_external_entry_ids()
            if not completed_queue_entry_ids:
                self._completed_queue_prune_signature = signature
                return {}
            prune_summary = prune_applied_review_queue(
                canonical_root=self.config.canonical_root,
                queue_path=self.config.queue_path,
                report_dir=self.config.queue_path.parent,
            )
            self._clear_local_review_state(completed_queue_entry_ids)
            self._entry_rows_cache = {}
            self._queue_shards.invalidate()
            self._completed_external_entry_ids_cache = None
            self._completed_external_reference_entry_ids_cache = None
            self._source_media = self._load_source_media()
            self._refresh_image_paths()
            self._completed_queue_prune_signature = self._completed_queue_signature()
            return prune_summary

    def _read_rows(self) -> list[dict[str, str]]:
        _, rows = self._read_rows_with_fieldnames()
        return rows

    def _decision_journal_path(self) -> Path:
        return self.config.queue_path.with_name(f"{self.config.queue_path.stem}_picker_decisions.json")

    def _crop_staging_path(self) -> Path:
        return self.config.queue_path.with_name(f"{self.config.queue_path.stem}_crop_staging.json")

    def _load_crop_staging(self) -> dict[str, dict[str, dict[str, Any]]]:
        path = self._crop_staging_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read crop staging file: {path}") from exc
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        if not isinstance(entries, dict):
            raise ValueError(f"Invalid crop staging file: {path}")
        staged: dict[str, dict[str, dict[str, Any]]] = {}
        for entry_id, candidates in entries.items():
            if not isinstance(candidates, dict):
                continue
            staged[str(entry_id)] = {
                str(candidate_path): dict(record)
                for candidate_path, record in candidates.items()
                if isinstance(record, dict)
            }
        return staged

    def _persist_crop_staging(self) -> None:
        path = self._crop_staging_path()
        if not self._crop_staging:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": self._crop_staging,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _archive_missing_crop_staging(
        self,
        entries: dict[str, dict[str, dict[str, Any]]],
    ) -> str:
        if not entries:
            return ""
        archive_dir = self.config.canonical_root / "exports" / "review_queue_backups"
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S_%f")
        archive_path = archive_dir / (
            f"{self.config.queue_path.stem}_crop_staging_missing_{timestamp}.json"
        )
        archive_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "reason": "canonical crop target no longer exists",
                    "entries": entries,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return str(archive_path)

    def _store_crop_staging(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, object] | None,
        commit_pending: bool = True,
    ) -> None:
        entry = self._crop_staging.setdefault(entry_id, {})
        entry[candidate_path] = {
            "crop": crop,
            "commit_pending": commit_pending,
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        self._persist_crop_staging()

    def _store_crop_rejection_staging(
        self,
        entry_id: str,
        candidate_path: str,
        rejected_row: dict[str, str],
        notes: str,
    ) -> None:
        entry = self._crop_staging.setdefault(entry_id, {})
        entry[candidate_path] = {
            "action": "reject_original",
            "commit_pending": True,
            "notes": notes or "Rejected from crop confirmation.",
            "rejected_row": rejected_row,
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        self._persist_crop_staging()

    def _staged_crop_record(self, entry_id: str, candidate_path: str) -> dict[str, Any] | None:
        return self._crop_staging.get(entry_id, {}).get(candidate_path)

    def _staged_crop_is_rejection(self, entry_id: str, candidate_path: str) -> bool:
        record = self._staged_crop_record(entry_id, candidate_path)
        return bool(record and str(record.get("action", "")).strip().lower() == "reject_original")

    def _staged_crop_commit_pending(self, record: dict[str, Any]) -> bool:
        if "commit_pending" in record:
            return bool(record.get("commit_pending"))
        action = str(record.get("action", "")).strip().lower()
        if action:
            return True
        crop = record.get("crop")
        if isinstance(crop, dict) and str(crop.get("source", "")).strip().lower() == "estimated":
            return False
        return True

    def _mark_staged_crop_commit_pending(self, entry_id: str, candidate_path: str) -> None:
        record = self._staged_crop_record(entry_id, candidate_path)
        if record is None or self._staged_crop_commit_pending(record):
            return
        crop = record.get("crop")
        if not isinstance(crop, dict):
            return
        if str(crop.get("source", "")).strip().lower() != "estimated":
            return
        record["commit_pending"] = True
        record["viewed_at"] = dt.datetime.now(dt.UTC).isoformat()
        self._persist_crop_staging()

    def _apply_staged_crop_to_candidate(
        self,
        entry_id: str,
        candidate: dict[str, Any],
        mark_estimated_viewed: bool = False,
    ) -> None:
        candidate_path = str(candidate.get("path", ""))
        if mark_estimated_viewed:
            self._mark_staged_crop_commit_pending(entry_id, candidate_path)
        record = self._staged_crop_record(entry_id, candidate_path)
        if record is None:
            return
        crop = record.get("crop")
        if crop is None:
            for field in _review_crop_fieldnames():
                candidate[field] = ""
            return
        if not isinstance(crop, dict):
            return
        candidate["review_crop_x"] = str(crop.get("x", ""))
        candidate["review_crop_y"] = str(crop.get("y", ""))
        candidate["review_crop_size"] = str(crop.get("size", ""))
        candidate["review_crop_candidate_width"] = str(crop.get("candidate_width", ""))
        candidate["review_crop_candidate_height"] = str(crop.get("candidate_height", ""))
        candidate["review_crop_source"] = str(crop.get("source", "manual"))
        candidate["review_crop_fill_color"] = str(crop.get("fill_color", ""))
        candidate["review_crop_rotation_degrees"] = _format_float(crop.get("rotation_degrees", 0))

    def _load_decision_overrides(self) -> dict[str, dict[str, dict[str, str]]]:
        path = self._decision_journal_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read picker decision journal: {path}") from exc
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        added_rows = payload.get("added_rows", {}) if isinstance(payload, dict) else {}
        replacement_rows = payload.get("replacement_rows", {}) if isinstance(payload, dict) else {}
        if not isinstance(entries, dict):
            raise ValueError(f"Invalid picker decision journal: {path}")
        if not isinstance(added_rows, dict):
            raise ValueError(f"Invalid picker decision journal: {path}")
        if not isinstance(replacement_rows, dict):
            raise ValueError(f"Invalid picker decision journal: {path}")
        self._added_candidate_rows = {
            str(entry_id): [dict(row) for row in rows if isinstance(row, dict)]
            for entry_id, rows in added_rows.items()
            if isinstance(rows, list)
        }
        self._replacement_candidate_rows = {
            str(entry_id): [dict(row) for row in rows if isinstance(row, dict)]
            for entry_id, rows in replacement_rows.items()
            if isinstance(rows, list)
        }
        return entries

    def _persist_decision_overrides(self) -> None:
        path = self._decision_journal_path()
        if not self._decision_overrides and not self._added_candidate_rows and not self._replacement_candidate_rows:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": self._decision_overrides,
                    "added_rows": self._added_candidate_rows,
                    "replacement_rows": self._replacement_candidate_rows,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _store_entry_overrides(self, entry_id: str, rows: list[dict[str, str]]) -> None:
        fields = [
            "review_decision",
            "review_notes",
            "associated_entry_date",
            "associated_date_source",
            *_review_crop_fieldnames(),
        ]
        self._decision_overrides[entry_id] = {
            _decision_row_key(row): {field: row.get(field, "") for field in fields}
            for row in rows
        }
        self._cache_entry_rows(entry_id, rows)
        self._persist_decision_overrides()

    def _store_added_candidate(
        self,
        entry_id: str,
        template: dict[str, str],
        values: dict[str, Any],
    ) -> None:
        self._store_added_candidates(entry_id, template, [values])

    def _store_added_candidates(
        self,
        entry_id: str,
        template: dict[str, str],
        values_list: list[dict[str, Any]],
    ) -> None:
        if not values_list:
            return
        rows = self._candidate_rows_from_values(template, values_list)
        self._replacement_candidate_rows.pop(entry_id, None)
        additions = self._added_candidate_rows.setdefault(entry_id, [])
        additions.extend(rows)
        self._added_candidate_rows[entry_id] = _deduplicate_candidate_rows(additions)
        cached_rows = self._entry_rows_cache.get(entry_id, [])
        if cached_rows:
            self._cache_entry_rows(entry_id, _deduplicate_candidate_rows([*cached_rows, *rows]))
        self._persist_decision_overrides()

    def _store_replacement_candidates(
        self,
        entry_id: str,
        template: dict[str, str],
        values_list: list[dict[str, Any]],
    ) -> None:
        rows = _deduplicate_candidate_rows(self._candidate_rows_from_values(template, values_list))
        if not rows:
            rows = [self._empty_candidate_row(template)]
        self._replacement_candidate_rows[entry_id] = rows
        self._added_candidate_rows.pop(entry_id, None)
        self._cache_entry_rows(entry_id, rows)
        self._persist_decision_overrides()

    def _clear_replacement_candidates(self, entry_id: str) -> None:
        if entry_id not in self._replacement_candidate_rows:
            return
        self._replacement_candidate_rows.pop(entry_id, None)
        self._entry_rows_cache.pop(entry_id, None)
        self._persist_decision_overrides()

    def _candidate_rows_from_values(
        self,
        template: dict[str, str],
        values_list: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for values in values_list:
            row = {field: str(values.get(field, "")) for field in template}
            for field, value in values.items():
                row[field] = str(value)
            rows.append(row)
        return rows

    def _empty_candidate_row(self, template: dict[str, str]) -> dict[str, str]:
        row = {field: str(value) for field, value in template.items()}
        for field in (
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
            *visual_fieldnames(),
        ):
            row[field] = ""
        return row

    def _apply_entry_overrides(self, entry_id: str, rows: list[dict[str, str]]) -> None:
        overrides = self._decision_overrides.get(entry_id, {})
        for row in rows:
            override = overrides.get(_decision_row_key(row))
            if override:
                row.update(override)

    def _entry_has_local_pending_rows(self, entry_id: str) -> bool:
        return (
            entry_id in self._added_candidate_rows
            or entry_id in self._replacement_candidate_rows
            or entry_id in self._decision_overrides
        )

    def _entry_rows_for_manual_candidate(self, entry_id: str) -> list[dict[str, str]]:
        rows = self._entry_rows(entry_id)
        if rows:
            return rows
        template = self._database_manual_candidate_template(entry_id)
        if template is None:
            return []
        self._recover_project_addition_candidates(entry_id)
        rows = self._entry_rows(entry_id)
        if rows:
            return rows
        self._cache_entry_rows(entry_id, [template])
        return [dict(template)]

    def _materialize_decision_overrides(self) -> None:
        if not self._decision_overrides and not self._added_candidate_rows and not self._replacement_candidate_rows:
            return
        fieldnames, rows = self._read_rows_with_fieldnames()
        self._write_rows(fieldnames, rows)
        self._decision_overrides = {}
        self._added_candidate_rows = {}
        self._replacement_candidate_rows = {}
        self._persist_decision_overrides()

    def _iter_entry_groups(self) -> Any:
        if not self.config.queue_path.exists():
            raise FileNotFoundError(f"Missing search queue: {self.config.queue_path}")
        current_entry_id = ""
        current_rows: list[dict[str, str]] = []
        yielded_entry_ids: set[str] = set()
        with self.config.queue_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                entry_id = row.get("entry_id", "")
                if current_rows and entry_id != current_entry_id:
                    rows = [dict(row) for row in self._replacement_candidate_rows.get(current_entry_id, current_rows)]
                    if current_entry_id not in self._replacement_candidate_rows:
                        rows.extend(dict(row) for row in self._added_candidate_rows.get(current_entry_id, []))
                    self._apply_entry_overrides(current_entry_id, rows)
                    rows = _deduplicate_candidate_rows(rows)
                    yielded_entry_ids.add(current_entry_id)
                    yield current_entry_id, rows
                    current_rows = []
                current_entry_id = entry_id
                current_rows.append(row)
        if current_rows:
            rows = [dict(row) for row in self._replacement_candidate_rows.get(current_entry_id, current_rows)]
            if current_entry_id not in self._replacement_candidate_rows:
                rows.extend(dict(row) for row in self._added_candidate_rows.get(current_entry_id, []))
            self._apply_entry_overrides(current_entry_id, rows)
            rows = _deduplicate_candidate_rows(rows)
            yielded_entry_ids.add(current_entry_id)
            yield current_entry_id, rows
        pending_entry_ids = set(self._replacement_candidate_rows) | set(self._added_candidate_rows)
        for entry_id in sorted(pending_entry_ids - yielded_entry_ids):
            rows = [
                dict(row)
                for row in self._replacement_candidate_rows.get(
                    entry_id,
                    self._added_candidate_rows.get(entry_id, []),
                )
            ]
            if not rows:
                continue
            self._apply_entry_overrides(entry_id, rows)
            rows = _deduplicate_candidate_rows(rows)
            yield entry_id, rows

    def _entry_rows(self, entry_id: str) -> list[dict[str, str]]:
        cached = self._entry_rows_cache.pop(entry_id, None)
        if cached is not None:
            self._entry_rows_cache[entry_id] = cached
            return [dict(row) for row in cached]
        if entry_id in self._replacement_candidate_rows:
            rows = [dict(row) for row in self._replacement_candidate_rows.get(entry_id, [])]
        else:
            rows = self._queue_shards.entry_rows(entry_id)
            rows.extend(dict(row) for row in self._added_candidate_rows.get(entry_id, []))
        self._apply_entry_overrides(entry_id, rows)
        rows = _deduplicate_candidate_rows(rows)
        if rows:
            self._cache_entry_rows(entry_id, rows)
        return rows

    def _cache_entry_rows(self, entry_id: str, rows: list[dict[str, str]]) -> None:
        self._entry_rows_cache.pop(entry_id, None)
        self._entry_rows_cache[entry_id] = [dict(row) for row in rows]
        while len(self._entry_rows_cache) > 2:
            oldest_entry_id = next(iter(self._entry_rows_cache))
            self._entry_rows_cache.pop(oldest_entry_id, None)

    def _pending_decision_counts(self) -> dict[str, int]:
        accepted = 0
        rejected = 0
        associated = 0
        for _entry_id, rows in self._iter_entry_groups():
            for row in rows:
                decision = row.get("review_decision", "").strip().lower()
                if decision in ACCEPT_DECISIONS:
                    accepted += 1
                elif decision in REJECT_DECISIONS:
                    rejected += 1
                elif decision in ASSOCIATED_PHOTO_DECISIONS:
                    associated += 1
        return {"accepted": accepted, "rejected": rejected, "associated": associated}

    def _read_rows_with_fieldnames(self) -> tuple[list[str], list[dict[str, str]]]:
        if not self.config.queue_path.exists():
            raise FileNotFoundError(f"Missing search queue: {self.config.queue_path}")
        with self.config.queue_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        if self._replacement_candidate_rows:
            replacement_entry_ids = set(self._replacement_candidate_rows)
            rows = [row for row in rows if row.get("entry_id", "") not in replacement_entry_ids]
            for replacement_rows in self._replacement_candidate_rows.values():
                rows.extend(dict(row) for row in replacement_rows)
        for added_rows in self._added_candidate_rows.values():
            rows.extend(dict(row) for row in added_rows)
        for field in (
            "review_decision",
            "review_notes",
            "associated_entry_date",
            "associated_date_source",
            "gps_latitude",
            "gps_longitude",
            "gps_source",
            "has_gps",
        ):
            if field not in fieldnames:
                fieldnames.append(field)
                for row in rows:
                    row[field] = ""
        for field in _review_crop_fieldnames():
            if field not in fieldnames:
                fieldnames.append(field)
                for row in rows:
                    row[field] = ""
        for field in visual_fieldnames():
            if field not in fieldnames:
                fieldnames.append(field)
                for row in rows:
                    row[field] = ""
        for row in rows:
            override = self._decision_overrides.get(row.get("entry_id", ""), {}).get(_decision_row_key(row))
            if override:
                row.update(override)
        return fieldnames, rows

    def _write_rows(self, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
        temp_path = self.config.queue_path.with_suffix(self.config.queue_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(_rows_for_fieldnames(rows, fieldnames))
        temp_path.replace(self.config.queue_path)
        self._entry_rows_cache = {}
        self._queue_shards.invalidate()

    def _matcher_review_queue_path(self) -> Path:
        if self.config.matcher_review_queue_path is not None:
            return self.config.matcher_review_queue_path
        return self.config.queue_path.parent / "original_photo_review_queue.csv"

    def _batch_plan_path(self) -> Path:
        if self.config.batch_plan_path is not None:
            return self.config.batch_plan_path
        return self.config.queue_path.parent / "original_photo_search_batch_plan.csv"

    def _run_crawl_job(
        self,
        job_id: str,
        entry_ids: list[str],
        search_roots: list[Path],
        scan_metadata_dates: bool,
    ) -> None:
        self._update_crawl_job(job_id, status="running", started_at=dt.datetime.now(dt.UTC).isoformat())
        try:
            with self._lock:
                summary = build_external_original_search_queue(
                    canonical_root=self.config.canonical_root,
                    search_roots=search_roots,
                    review_queue_path=self._matcher_review_queue_path(),
                    report_dir=self.config.queue_path.parent,
                    scan_metadata_dates=scan_metadata_dates,
                    target_entry_ids=set(entry_ids),
                    merge_existing_queue=True,
                )
            self._update_crawl_job(
                job_id,
                status="pass",
                candidate_count=summary.candidate_count,
                finished_at=dt.datetime.now(dt.UTC).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - local crawl jobs should surface readable errors.
            self._update_crawl_job(
                job_id,
                status="fail",
                error=str(exc),
                finished_at=dt.datetime.now(dt.UTC).isoformat(),
            )

    def _update_crawl_job(self, job_id: str, **updates: Any) -> None:
        with self._job_lock:
            job = self._crawl_jobs[job_id]
            job.update(updates)


def create_handler(state: PickerState) -> type[BaseHTTPRequestHandler]:
    class PickerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/":
                    self._send_html(PICKER_HTML)
                elif parsed.path in {"/link-drop", "/link-drop/"}:
                    self._send_html(link_drop_html())
                elif parsed.path in {"/crop", "/crop/"}:
                    self._send_html(CROP_HTML)
                elif parsed.path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                elif parsed.path == "/api/summary":
                    self._send_json(state.summary())
                elif parsed.path == "/api/batches":
                    self._send_json(state.batches())
                elif parsed.path == "/api/entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    status = query.get("status", ["all"])[0]
                    self._send_json(
                        state.entry_page(
                            status=status,
                            entry_ids=set(_query_values(query, "entry_id")),
                            entry_dates=set(_query_values(query, "entry_date")),
                            limit=_query_int(query, "limit", DEFAULT_ENTRY_PAGE_SIZE),
                            offset=_query_int(query, "offset", 0) or 0,
                        )
                    )
                elif parsed.path == "/api/candidate-facts":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json({"facts": state.candidate_facts(_query_values(query, "token"))})
                elif parsed.path == "/api/crop-entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    crop_filter = query.get("crop_filter", ["missing"])[0]
                    entries = state.crop_entries(
                        crop_filter=crop_filter,
                        entry_dates=set(_query_values(query, "entry_date")),
                        start_date=query.get("start_date", [""])[0],
                        end_date=query.get("end_date", [""])[0],
                    )
                    self._send_json(
                        {
                            "entries": entries,
                            "total_count": len(entries),
                            "pending_crop_commits": state.pending_crop_commits(),
                            "crop_estimate_batch": state.latest_crop_estimate_job() or {},
                        }
                    )
                elif parsed.path.startswith("/api/entry/"):
                    query = urllib.parse.parse_qs(parsed.query)
                    entry_id = urllib.parse.unquote(parsed.path.removeprefix("/api/entry/"))
                    detail = state.entry_detail(
                        entry_id,
                        candidate_limit=None,
                        rank_if_needed=False,
                        include_database=_query_flag(query, "include_database"),
                    )
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown entry")
                    else:
                        self._send_json(detail)
                elif parsed.path.startswith("/api/crop-entry/"):
                    entry_id = urllib.parse.unquote(parsed.path.removeprefix("/api/crop-entry/"))
                    detail = state.crop_entry_detail(entry_id)
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown entry")
                    else:
                        self._send_json(detail)
                elif parsed.path.startswith("/api/crawl/"):
                    job_id = urllib.parse.unquote(parsed.path.removeprefix("/api/crawl/"))
                    job = state.crawl_job(job_id)
                    if job is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown crawl job")
                    else:
                        self._send_json(job)
                elif parsed.path.startswith("/api/crop-estimate-batch/"):
                    job_id = urllib.parse.unquote(parsed.path.removeprefix("/api/crop-estimate-batch/"))
                    job = state.crop_estimate_job(job_id)
                    if job is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown crop estimate batch")
                    else:
                        self._send_json(job)
                elif parsed.path == "/api/choose-folder":
                    self._send_json(_choose_folder_dialog())
                elif parsed.path == "/api/choose-photo":
                    self._send_json(_choose_photo_dialog())
                elif parsed.path.startswith("/image/"):
                    token = urllib.parse.unquote(parsed.path.removeprefix("/image/"))
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_image(token, _query_int(query, "max", None))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            except Exception as exc:  # noqa: BLE001 - local UI should return readable API errors.
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/api/decision":
                    payload = self._read_json()
                    detail = state.save_decision(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        decision=str(payload.get("decision", "")),
                        notes=str(payload.get("notes", "")),
                        crop=payload.get("crop") if isinstance(payload.get("crop"), dict) else None,
                        include_candidates=False,
                        associated_entry_date=str(payload.get("associated_entry_date", "")),
                        associated_date_source=str(payload.get("associated_date_source", "manual")),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/associated-date-choices":
                    payload = self._read_json()
                    self._send_json(
                        state.associated_date_choices(
                            entry_id=str(payload.get("entry_id", "")),
                            candidate_path=str(payload.get("candidate_path", "")),
                        )
                    )
                    return
                if parsed.path == "/api/reject-all":
                    payload = self._read_json()
                    self._send_json(
                        state.reject_all_candidates(
                            entry_id=str(payload.get("entry_id", "")),
                            notes=str(payload.get("notes", "")),
                            include_candidates=False,
                            photo_index_folder=str(payload.get("photo_index_folder", "")),
                        )
                    )
                    return
                if parsed.path == "/api/crop":
                    payload = self._read_json()
                    source = str(payload.get("source", "manual")).strip().lower()
                    if source not in {"manual", "estimated"}:
                        source = "manual"
                    detail = state.save_crop(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        crop=payload.get("crop") if isinstance(payload.get("crop"), dict) else {},
                        source=source,
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/crop-reset":
                    payload = self._read_json()
                    detail = state.reset_crop(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        preserve_estimate=bool(payload.get("preserve_estimate")),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/crop-commit":
                    self._send_json(state.commit_staged_crops())
                    return
                if parsed.path == "/api/crop-reject-original":
                    payload = self._read_json()
                    result = state.reject_crop_original(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        notes=str(payload.get("notes", "")),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/api/crop-suggestion":
                    payload = self._read_json()
                    result = state.suggest_crop_for_candidate(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        crop=payload.get("crop") if isinstance(payload.get("crop"), dict) else None,
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/api/crop-estimate-batch":
                    payload = self._read_json()
                    target_extensions = payload.get("target_extensions")
                    if not isinstance(target_extensions, list):
                        target_extensions = []
                    self._send_json(
                        state.start_crop_estimate_batch(
                            apply_estimates=bool(payload.get("apply_estimates")),
                            refresh_saved_estimates=bool(payload.get("refresh_saved_estimates")),
                            target_extensions=[str(value) for value in target_extensions],
                        )
                    )
                    return
                if parsed.path == "/api/crawl":
                    payload = self._read_json()
                    job = state.start_crawl(
                        entry_ids=[str(value) for value in payload.get("entry_ids", [])],
                        search_roots=[str(value) for value in payload.get("search_roots", [])],
                        scan_metadata_dates=bool(payload.get("scan_metadata_dates", True)),
                    )
                    self._send_json(job)
                    return
                if parsed.path == "/api/expand-date-range":
                    payload = self._read_json()
                    result = state.expand_date_range(
                        entry_id=str(payload.get("entry_id", "")),
                        days=int(payload.get("days", 0)),
                        photo_index_folder=str(payload.get("photo_index_folder", "")),
                        search_whole_index=bool(payload.get("search_whole_index", False)),
                        whole_index_filename_only=bool(payload.get("whole_index_filename_only", False)),
                        filename_dates_only=bool(payload.get("filename_dates_only", False)),
                        include_modified_dates=bool(payload.get("include_modified_dates", False)),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/api/expand-default-date-range":
                    payload = self._read_json()
                    result = state.expand_default_date_range(
                        entry_id=str(payload.get("entry_id", "")),
                        photo_index_folder=str(payload.get("photo_index_folder", "")),
                        search_whole_index=bool(payload.get("search_whole_index", False)),
                        whole_index_filename_only=bool(payload.get("whole_index_filename_only", False)),
                        filename_dates_only=bool(payload.get("filename_dates_only", False)),
                        include_modified_dates=bool(payload.get("include_modified_dates", False)),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/api/search-index-date-range":
                    payload = self._read_json()
                    result = state.search_index_date_range(
                        entry_id=str(payload.get("entry_id", "")),
                        start_date=str(payload.get("start_date", "")),
                        end_date=str(payload.get("end_date", "")),
                        search_whole_index=bool(payload.get("search_whole_index", False)),
                        photo_index_folder=str(payload.get("photo_index_folder", "")),
                        whole_index_filename_only=bool(payload.get("whole_index_filename_only", False)),
                        filename_dates_only=bool(payload.get("filename_dates_only", False)),
                        include_modified_dates=bool(payload.get("include_modified_dates", False)),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/api/link-candidate":
                    payload = self._read_json()
                    detail = state.add_linked_candidate(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        associate=bool(payload.get("associate", False)),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/import-dropped-candidate":
                    entry_id = urllib.parse.unquote(self.headers.get("x-entry-id", ""))
                    filename = urllib.parse.unquote(self.headers.get("x-file-name", ""))
                    content_type = self.headers.get("content-type", "")
                    suffix = video_drop_suffix(filename, content_type)
                    if suffix:
                        upload_path = self._read_upload_to_temp(MAX_VIDEO_DROP_BYTES, suffix)
                        detail = state.prepare_video_frame_choices(
                            entry_id=entry_id,
                            filename=filename,
                            upload_path=upload_path,
                            content_type=content_type,
                        )
                    else:
                        payload = self._read_bytes(MAX_DROP_BYTES)
                        detail = state.add_copied_candidate(
                            entry_id=entry_id,
                            filename=filename,
                            content_type=self.headers.get("content-type", ""),
                            payload=payload,
                            include_candidates=False,
                        )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/import-linked-candidate":
                    payload = self._read_bytes(MAX_DROP_BYTES)
                    detail = state.add_copied_candidate(
                        entry_id=urllib.parse.unquote(self.headers.get("x-entry-id", "")),
                        filename=urllib.parse.unquote(self.headers.get("x-file-name", "")),
                        content_type=self.headers.get("content-type", ""),
                        payload=payload,
                        include_candidates=True,
                        associate=True,
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/import-video-frames":
                    payload = self._read_json()
                    detail = state.import_video_frames(
                        entry_id=str(payload.get("entry_id", "")),
                        session_id=str(payload.get("session_id", "")),
                        main_frame_id=str(payload.get("main_frame_id", "")),
                        supplemental_frame_ids=[
                            str(value) for value in payload.get("supplemental_frame_ids", [])
                        ],
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/discard-video-frames":
                    payload = self._read_json()
                    state.discard_video_frame_choices(
                        entry_id=str(payload.get("entry_id", "")),
                        session_id=str(payload.get("session_id", "")),
                        wait=False,
                    )
                    self._send_json({"ok": True})
                    return
                if parsed.path == "/api/extract-video-frame":
                    payload = self._read_json()
                    detail = state.extract_video_frame_at_time(
                        entry_id=str(payload.get("entry_id", "")),
                        session_id=str(payload.get("session_id", "")),
                        time_seconds=float(payload.get("time_seconds", 0) or 0),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/api/apply-decisions":
                    payload = self._read_json()
                    if payload.get("confirm_apply_decisions") != APPLY_DECISIONS_CONFIRM_TOKEN:
                        self._send_error(HTTPStatus.BAD_REQUEST, "Apply decisions requires explicit confirmation.")
                        return
                    self._send_json(state.apply_decisions())
                    return
                if parsed.path == "/api/commit-entry":
                    payload = self._read_json()
                    self._send_json(state.commit_entry_decision(str(payload.get("entry_id", ""))))
                    return
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Not found")
                    return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:  # noqa: BLE001 - local UI should return readable API errors.
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length", "0"))
            payload = self.rfile.read(length)
            return json.loads(payload.decode("utf-8")) if payload else {}

        def _read_bytes(self, maximum: int) -> bytes:
            length = int(self.headers.get("content-length", "0"))
            if length > maximum:
                raise ValueError("Dropped image exceeds the 250 MB limit.")
            return self.rfile.read(length)

        def _read_upload_to_temp(self, maximum: int, suffix: str) -> Path:
            length = int(self.headers.get("content-length", "0"))
            if length > maximum:
                raise ValueError("Dropped video exceeds the 2 GB limit.")
            temp_dir = state.config.canonical_root / "cache" / "picker_video_uploads"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"{uuid.uuid4().hex}{suffix or '.video'}"
            remaining = length
            with temp_path.open("wb") as handle:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                temp_path.unlink(missing_ok=True)
                raise ValueError("Dropped video upload ended before the full file was received.")
            return temp_path

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, body: dict[str, Any]) -> None:
            payload = json.dumps(body, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_image(self, token: str, max_size: int | None = None) -> None:
            path = state.preview_path(token, max_size)
            if path is None or not path.exists() or not path.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "Image not found")
                return
            payload = path.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(path))
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", mime_type or "application/octet-stream")
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(payload)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            payload = json.dumps({"error": message}).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return PickerHandler


def serve_picker(config: PickerConfig, host: str, port: int) -> ThreadingHTTPServer:
    state = PickerState(config)
    server = ThreadingHTTPServer((host, port), create_handler(state))
    return server


def _group_by_entry(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["entry_id"], []).append(row)
    return grouped


def _entry_matches_status(entry: dict[str, Any], status: str) -> bool:
    return (
        status == "all"
        or entry["status"] == status
        or (
            status == "accepted_not_applied"
            and entry["status"] == "selected"
        )
        or (
            status == "needs_action"
            and entry["status"] in {"needs_review", "search_needed"}
        )
    )


def _entry_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (str(entry.get("entry_date", "")), str(entry.get("entry_id", "")))


def _batch_sort_key(batch: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(batch.get("start_date", "")),
        str(batch.get("end_date", "")),
        str(batch.get("batch_id", "")),
    )


def _project_addition_recovery_sort_key(path: Path) -> tuple[int, int, str]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0, str(path))
    return (int(getattr(stat, "st_ctime_ns", 0)), int(getattr(stat, "st_mtime_ns", 0)), str(path))


def _entry_status(entry_rows: list[dict[str, str]]) -> str:
    entry_rows = _deduplicate_candidate_rows(entry_rows)
    candidate_rows = [
        row
        for row in entry_rows
        if row.get("candidate_path", "").strip() and not _is_hidden_candidate_row(row)
    ]
    selected_rows = [
        row
        for row in candidate_rows
        if row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
    ]
    fallback_rows = [
        row
        for row in entry_rows
        if row.get("review_decision", "").strip().lower() in FALLBACK_DECISIONS
    ]
    rejected_rows = [
        row
        for row in candidate_rows
        if row.get("review_decision", "").strip().lower() == "rejected"
    ]
    if selected_rows:
        return "selected"
    if fallback_rows:
        return "fallback"
    if not candidate_rows:
        return "search_needed"
    if len(rejected_rows) == len(candidate_rows):
        return "rejected"
    return "needs_review"


def _deduplicate_candidate_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for row in rows:
        path_text = str(row.get("candidate_path", "")).strip()
        if path_text:
            normalized = str(Path(path_text).expanduser())
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
        deduped.append(row)
    return deduped


def _queue_shard_entry_summary(
    entry_id: str,
    rows: list[dict[str, str]],
    shard_index: int,
) -> dict[str, Any]:
    rows = _deduplicate_candidate_rows(rows)
    first = rows[0]
    candidate_rows = [
        row
        for row in rows
        if row.get("candidate_path", "").strip() and not _is_hidden_candidate_row(row)
    ]
    accepted_count = sum(
        row.get("review_decision", "").strip().lower() in ACCEPT_DECISIONS
        for row in candidate_rows
    )
    rejected_count = sum(
        row.get("review_decision", "").strip().lower() in REJECT_DECISIONS
        for row in candidate_rows
    )
    associated_count = sum(
        row.get("review_decision", "").strip().lower() in ASSOCIATED_PHOTO_DECISIONS
        for row in candidate_rows
    )
    fallback_count = sum(
        row.get("review_decision", "").strip().lower() in FALLBACK_DECISIONS
        for row in rows
    )
    return {
        "entry_id": entry_id,
        "entry_date": first.get("entry_date", ""),
        "status": _entry_status(rows),
        "candidate_count": len(candidate_rows) - rejected_count,
        "selected_count": accepted_count,
        "pending_commit_count": accepted_count,
        "commit_ready": accepted_count > 0,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "associated_count": associated_count,
        "fallback_count": fallback_count,
        "current_match_status": first.get("current_match_status", ""),
        "current_decision": first.get("current_decision", ""),
        "manual_search_message": _manual_search_message(rows),
        "shard": f"shard-{shard_index:02d}.csv",
    }


def _manual_search_message(entry_rows: list[dict[str, str]]) -> str:
    for row in entry_rows:
        evidence = str(row.get("evidence", ""))
        notes = str(row.get("review_notes", "")).strip()
        if "manual_search_required" in evidence and notes:
            return notes
    return ""


def _used_search_range_days(
    entry_id: str,
    entry_rows: list[dict[str, str]],
    report_dir: Path,
) -> list[int]:
    days: set[int] = set()
    for row in entry_rows:
        for match in BUTTON_RANGE_EVIDENCE_RE.finditer(str(row.get("evidence", ""))):
            days.add(int(match.group(1)))
    record = load_reject_all_range_state(report_dir).get(entry_id, {})
    try:
        recorded_days = int(str(record.get("last_search_range_days", "")).strip())
    except ValueError:
        recorded_days = 0
    if recorded_days:
        days.add(recorded_days)
    return sorted(day for day in days if day in EXPAND_DATE_RANGE_DAYS)


def _is_hidden_candidate_row(row: dict[str, str]) -> bool:
    if _is_project365_export_original_candidate(row):
        return True
    if row.get("review_decision", "").strip().lower():
        return False
    return row.get("candidate_filter_reason", "").strip().lower() in HIDDEN_CANDIDATE_FILTER_REASONS


def _is_project365_export_original_candidate(row: dict[str, str]) -> bool:
    filename = row.get("candidate_filename", "").strip().lower()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.(?:png|jpe?g)", filename):
        return False
    mime_type = row.get("mime_type", "").strip().lower()
    path_text = row.get("candidate_path", "").strip()
    path_lower = path_text.lower()
    if mime_type not in {"image/png", "image/jpeg"} and not re.search(r"\.(?:png|jpe?g)$", path_lower):
        return False
    normalized_path = path_lower.replace("\\", "/")
    export_markers = (
        "/project365canonical/media/project365_exports/",
        "/project365_exports/",
        "project365 pro export zips",
        "project365 export",
        "project 365 export",
    )
    return any(marker in normalized_path for marker in export_markers)


def _decision_row_key(row: dict[str, str]) -> str:
    candidate_path = row.get("candidate_path", "").strip()
    if candidate_path:
        return f"candidate:{candidate_path}"
    media_asset_id = row.get("project365_media_asset_id", "").strip()
    return f"entry:{media_asset_id or row.get('entry_id', '')}"


def _manual_index_search_dates(start_date: str, end_date: str) -> list[str]:
    try:
        start = dt.date.fromisoformat(str(start_date).strip())
        end = dt.date.fromisoformat(str(end_date).strip())
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD.") from exc
    if end < start:
        raise ValueError("End date must be on or after start date.")
    date_count = (end - start).days + 1
    if date_count > MAX_MANUAL_INDEX_DATE_SEARCH_DAYS:
        raise ValueError(
            f"Date search range cannot exceed {MAX_MANUAL_INDEX_DATE_SEARCH_DAYS} days."
        )
    return [(start + dt.timedelta(days=offset)).isoformat() for offset in range(date_count)]


def _associated_date_choices_from_row(row: dict[str, str]) -> list[dict[str, str]]:
    choices: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_choice(date_text: str, source: str) -> None:
        normalized = _normalized_associated_entry_date(date_text)
        normalized_source = source.strip().lower() or "manual"
        key = (normalized_source, normalized)
        if not normalized or key in seen:
            return
        seen.add(key)
        choices.append({"date": normalized, "source": normalized_source})

    capture_source = _associated_capture_source(row.get("capture_timestamp_source", ""))
    add_choice(row.get("capture_timestamp", ""), capture_source)
    return choices


def _normalized_associated_entry_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized_timestamp = _normalized_associated_entry_timestamp(text)
    if normalized_timestamp:
        return normalized_timestamp
    text = text[:10]
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError:
        return ""
    return parsed.isoformat()


def _normalized_associated_entry_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        return ""
    if len(text) == 16 and text[10] == "T":
        text = f"{text}:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.microsecond:
        return parsed.isoformat(timespec="microseconds")
    return parsed.isoformat(timespec="seconds")


def _associated_capture_source(source: str) -> str:
    normalized = source.strip().lower()
    if normalized == "filename_timestamp":
        return "filename_timestamp"
    if normalized:
        return "capture"
    return "capture"


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, str, int, int, str]:
    try:
        distance = int(str(candidate.get("date_distance", "")).strip())
    except ValueError:
        distance = 9999
    timestamp = str(candidate.get("capture_timestamp", "")).strip()
    return (
        0 if candidate.get("selected") else 1,
        distance,
        0 if timestamp else 1,
        timestamp,
        _candidate_quality_rank(candidate),
        -_candidate_byte_size(candidate),
        str(candidate.get("filename", "")),
    )


def _candidate_page_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    decision = str(candidate.get("review_decision", "")).strip().lower()
    database_role = str(candidate.get("database_confirmed_role", "")).strip().lower()
    if database_role == "external_original_reference":
        decision = "use_external_original"
    visual_likely = str(candidate.get("visual_likely", "")).strip().lower() == "true"
    try:
        visual_rank = int(str(candidate.get("visual_rank", "")).strip())
    except ValueError:
        visual_rank = 999999
    normalized = dict(candidate)
    normalized["selected"] = decision in ACCEPT_DECISIONS
    normalized["filename"] = candidate.get("candidate_filename", "")
    normalized["path"] = candidate.get("candidate_path", "")
    return (
        0 if decision in ACCEPT_DECISIONS else 1,
        _candidate_manual_link_rank(candidate),
        0 if _is_project_originals_source_path(str(candidate.get("candidate_path", ""))) else 1,
        0 if visual_likely else 1,
        visual_rank,
        *_candidate_sort_key(normalized)[1:],
    )


def _candidate_manual_link_rank(candidate: dict[str, Any]) -> int:
    text = " ".join(
        str(candidate.get(key, ""))
        for key in ["evidence", "candidate_filename", "candidate_path"]
    )
    return 0 if re.search(r"manual_link|manual_drop_copy", text, re.IGNORECASE) else 1


def _candidate_quality_rank(candidate: dict[str, Any]) -> int:
    text = " ".join(
        str(candidate.get(key, ""))
        for key in ["evidence", "filename", "path"]
    ).lower()
    if "manual_link" in text or "manual_drop_copy" in text:
        return -1
    if any(token in text for token in ["quality_positive", "improved", "enhanced", "edited", "final", "master"]):
        return 0
    if any(token in text for token in ["quality_negative", "web", "small", "thumbnail", "preview", "lowres", "resized", "compressed"]):
        return 2
    return 1


def _candidate_byte_size(candidate: dict[str, Any]) -> int:
    try:
        return int(str(candidate.get("byte_size", "")).strip())
    except ValueError:
        return 0


def _candidate_folder_label(path_text: str) -> str:
    parts = [part for part in Path(path_text).parts if part and part != "/"]
    if len(parts) <= 1:
        return ""
    return " / ".join(parts[:-1][-2:])


def _is_project_originals_source_path(path_text: str) -> bool:
    parts = tuple(Path(str(path_text or "")).parts)
    if len(parts) < len(PROJECT_ORIGINALS_SOURCE_PARTS):
        return False
    for index in range(0, len(parts) - len(PROJECT_ORIGINALS_SOURCE_PARTS) + 1):
        if parts[index : index + len(PROJECT_ORIGINALS_SOURCE_PARTS)] == PROJECT_ORIGINALS_SOURCE_PARTS:
            return True
    return False


def _date_window(entry_date: str, days: int) -> set[str]:
    entry_day = dt.date.fromisoformat(entry_date)
    return {
        (entry_day + dt.timedelta(days=offset)).isoformat()
        for offset in range(-days, days + 1)
    }


def _candidate_filename_starts_with_any_date(candidate: dict[str, Any], target_dates: set[str]) -> bool:
    filename = str(candidate.get("candidate_filename") or candidate.get("filename") or "").strip()
    if not filename:
        filename = Path(str(candidate.get("candidate_path") or candidate.get("path") or "")).name
    return any(filename.startswith(target_date) for target_date in target_dates)


def _candidate_path_is_available(path_text: str) -> bool:
    path = Path(str(path_text or "").strip())
    return bool(str(path)) and path.exists() and path.is_file()


def _file_byte_size(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return str(path.stat().st_size)
    except OSError:
        return ""


def _file_type_label(path: Path | None) -> str:
    if not path:
        return ""
    suffix = path.suffix.strip(".").upper()
    if suffix:
        return "JPG" if suffix == "JPEG" else suffix
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type and "/" in mime_type:
        return mime_type.rsplit("/", 1)[-1].upper()
    return ""


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_addition_destination(
    target_dir: Path,
    entry_date: dt.date,
    suffix: str,
    digest: str,
    force_digest_suffix: bool = False,
) -> Path:
    stem = f"{entry_date.isoformat()} 000001 ({PROJECT_ADDITION_FILENAME_MARKER})"
    if force_digest_suffix:
        stem = f"{stem} - {digest[:12]}"
    return target_dir / f"{stem}{suffix}"


def _magick_command() -> str:
    magick = shutil.which("magick")
    if magick:
        return magick
    homebrew_magick = Path("/opt/homebrew/bin/magick")
    if homebrew_magick.is_file():
        return str(homebrew_magick)
    raise ValueError("ImageMagick is required to convert AVIF drops to HEIC.")


def _convert_dropped_photo_to_heic(source: Path, destination: Path) -> None:
    completed = subprocess.run(
        [
            _magick_command(),
            str(source),
            "-auto-orient",
            f"HEIC:{destination}",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        error = (completed.stderr or completed.stdout).strip()
        detail = f": {error}" if error else "."
        raise ValueError(f"Could not convert dropped photo to HEIC{detail}")


def _ffmpeg_command() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    homebrew_ffmpeg = Path("/opt/homebrew/bin/ffmpeg")
    if homebrew_ffmpeg.is_file():
        return str(homebrew_ffmpeg)
    raise ValueError("FFmpeg is required to extract screenshots from dropped videos.")


def _ffprobe_command() -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    homebrew_ffprobe = Path("/opt/homebrew/bin/ffprobe")
    if homebrew_ffprobe.is_file():
        return str(homebrew_ffprobe)
    raise ValueError("FFprobe is required to inspect dropped videos.")


def _video_duration_seconds(path: Path) -> float:
    completed = subprocess.run(
        [
            _ffprobe_command(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        return 0.0
    try:
        return max(0.0, float(completed.stdout.strip()))
    except ValueError:
        return 0.0


def _video_capture_timestamp(path: Path) -> tuple[str, str]:
    try:
        metadata = _exiftool_capture_metadata([path])
    except Exception:
        return "", ""
    return metadata.get(str(path.resolve()), ("", ""))


def _video_frame_times(duration: float, max_count: int) -> list[float]:
    if duration <= 0:
        return [0.0, 2.0, 5.0, 10.0][:max_count]
    desired = max(1, max_count)
    if desired == 1:
        return [max(0.0, duration / 2)]
    return [duration * (index + 1) / (desired + 1) for index in range(desired)]


def _extract_video_frames(video_path: Path, frame_dir: Path, max_count: int) -> list[tuple[float, Path]]:
    duration = _video_duration_seconds(video_path)
    extracted: list[tuple[float, Path]] = []
    errors: list[str] = []
    for index, time_seconds in enumerate(_video_frame_times(duration, max_count), start=1):
        frame_path = frame_dir / f"frame-{index:02d}.jpg"
        try:
            _extract_video_frame_at_time(video_path, frame_path, time_seconds)
            extracted.append((time_seconds, frame_path))
        except ValueError as exc:
            errors.append(str(exc))
    if not extracted and errors:
        raise ValueError(f"Could not extract screenshots from the dropped video: {errors[0]}")
    return extracted


def _extract_video_frame_at_time(video_path: Path, frame_path: Path, time_seconds: float) -> None:
    completed = subprocess.run(
        [
            _ffmpeg_command(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{time_seconds:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(frame_path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if completed.returncode == 0 and frame_path.exists() and frame_path.stat().st_size > 0:
        return
    error = (completed.stderr or completed.stdout).strip()
    frame_path.unlink(missing_ok=True)
    if error:
        raise ValueError(error)
    raise ValueError("Could not extract that screenshot.")


def _format_frame_time(time_seconds: float) -> str:
    seconds = max(0, int(round(time_seconds)))
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minute:02d}:{second:02d}"
    return f"{minute}:{second:02d}"


def _video_frame_filename(video_name: str, frame: dict[str, Any]) -> str:
    stem = Path(video_name).stem or "video"
    label = str(frame.get("label", "")).replace(":", "-") or "frame"
    frame_id = str(frame.get("frame_id", "frame"))
    return f"{stem} screenshot {label} {frame_id}.jpg"


def _set_project_addition_timestamps(path: Path, timestamp: dt.datetime) -> None:
    exiftool = shutil.which("exiftool")
    if not exiftool:
        raise ValueError("ExifTool is required to timestamp dropped project photos.")
    exif_timestamp = timestamp.strftime("%Y:%m:%d %H:%M:%S")
    completed = subprocess.run(
        [
            exiftool,
            "-overwrite_original",
            "-m",
            f"-FileCreateDate={exif_timestamp}",
            f"-FileModifyDate={exif_timestamp}",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout).strip()
        raise ValueError(f"ExifTool could not timestamp dropped project photo: {error}")
    epoch = timestamp.timestamp()
    os.utime(path, (epoch, epoch))


def _choose_photo_dialog() -> dict[str, str]:
    script = 'POSIX path of (choose file with prompt "Choose the original photo to link")'
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        if "User canceled" in error or "-128" in error:
            return {"path": ""}
        raise ValueError("macOS could not open the photo chooser. Restart the control app and try again.")
    path = result.stdout.strip()
    if path and not Path(path).is_file():
        raise ValueError("The selected photo does not exist.")
    return {"path": path}


def _choose_folder_dialog() -> dict[str, str]:
    script = (
        'POSIX path of (choose folder with prompt "Choose the photo folder to scan")'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        if "User canceled" in error or "-128" in error:
            return {"path": ""}
        raise ValueError("macOS could not open the folder chooser. Restart the control app and try again.")
    path = result.stdout.strip()
    if path and not Path(path).is_dir():
        raise ValueError("The selected folder does not exist.")
    return {"path": path}


def _image_dimensions(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    dimensions: tuple[int, int] | None = None
    try:
        payload = path.read_bytes()[:128 * 1024]
    except OSError:
        payload = b""
    if payload:
        try:
            dimensions = _parse_image_dimensions(payload)
        except (OSError, ValueError, struct.error):
            dimensions = None
    if not dimensions:
        dimensions = _sips_image_dimensions(path)
    if not dimensions:
        return ""
    width, height = dimensions
    if width <= 0 or height <= 0:
        return ""
    return f"{width} x {height}"


def _sips_image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            ["sips", "--getProperty", "pixelWidth", "--getProperty", "pixelHeight", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    width_match = re.search(r"pixelWidth:\s*(\d+)", result.stdout)
    height_match = re.search(r"pixelHeight:\s*(\d+)", result.stdout)
    if not width_match or not height_match:
        return None
    return int(width_match.group(1)), int(height_match.group(1))


def _has_embedded_geolocation(path: Path | None) -> bool:
    if path is None or path.suffix.lower() not in {".jpg", ".jpeg"}:
        return False
    try:
        with path.open("rb") as handle:
            payload = handle.read(256 * 1024)
    except OSError:
        return False
    if not payload.startswith(b"\xff\xd8"):
        return False
    offset = 2
    while offset + 4 <= len(payload):
        if payload[offset] != 0xFF:
            return False
        marker = payload[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            return False
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            return False
        segment = payload[offset + 2 : offset + segment_length]
        offset += segment_length
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return _tiff_has_gps_ifd(segment[6:])
    return False


def _candidate_row_has_indexed_geolocation(row: dict[str, Any]) -> bool:
    gps_flag = str(row.get("has_gps", "")).strip().lower()
    if gps_flag in {"1", "true", "yes"}:
        return True
    flag = str(row.get("has_embedded_geolocation", "")).strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    return _valid_geolocation(row.get("gps_latitude"), row.get("gps_longitude"))


def _valid_geolocation(latitude: Any, longitude: Any) -> bool:
    try:
        lat = float(str(latitude).strip())
        lon = float(str(longitude).strip())
    except (TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _dropped_photo_metadata_values(path: Path, entry_date: str) -> dict[str, str]:
    try:
        metadata = _exiftool_photo_metadata([path]).get(str(path.resolve()))
    except RuntimeError:
        return {}
    if metadata is None:
        return {}
    return _photo_metadata_values(metadata, entry_date)


def _photo_metadata_values(metadata: ExiftoolPhotoMetadata, entry_date: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if metadata.capture_timestamp and metadata.capture_timestamp_source:
        values["capture_timestamp"] = metadata.capture_timestamp
        values["capture_timestamp_source"] = metadata.capture_timestamp_source
        capture_date = metadata.capture_timestamp[:10]
        values["media_creation_dates"] = capture_date
        values["date_distance"] = _capture_date_distance(entry_date, capture_date)
    if metadata.gps_latitude is not None and metadata.gps_longitude is not None:
        values["gps_latitude"] = _format_coordinate(metadata.gps_latitude)
        values["gps_longitude"] = _format_coordinate(metadata.gps_longitude)
        values["gps_source"] = metadata.gps_source
        values["has_gps"] = "1" if _valid_geolocation(metadata.gps_latitude, metadata.gps_longitude) else "0"
    return values


def _format_coordinate(value: float) -> str:
    return f"{float(value):.12f}".rstrip("0").rstrip(".")


def _capture_date_distance(entry_date: str, capture_date: str) -> str:
    try:
        target = dt.date.fromisoformat(entry_date)
        captured = dt.date.fromisoformat(capture_date)
    except ValueError:
        return ""
    return str(abs((captured - target).days))


def _tiff_has_gps_ifd(payload: bytes) -> bool:
    if len(payload) < 8:
        return False
    if payload[:2] == b"II":
        prefix = "<"
    elif payload[:2] == b"MM":
        prefix = ">"
    else:
        return False
    try:
        if struct.unpack_from(prefix + "H", payload, 2)[0] != 42:
            return False
        ifd_offset = struct.unpack_from(prefix + "I", payload, 4)[0]
        if ifd_offset <= 0 or ifd_offset + 2 > len(payload):
            return False
        entry_count = struct.unpack_from(prefix + "H", payload, ifd_offset)[0]
        for index in range(entry_count):
            item_offset = ifd_offset + 2 + index * 12
            if item_offset + 12 > len(payload):
                return False
            tag = struct.unpack_from(prefix + "H", payload, item_offset)[0]
            if tag != 0x8825:
                continue
            gps_offset = struct.unpack_from(prefix + "I", payload, item_offset + 8)[0]
            return gps_offset > 0 and gps_offset + 2 <= len(payload) and struct.unpack_from(
                prefix + "H", payload, gps_offset
            )[0] > 0
    except struct.error:
        return False
    return False


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


def _query_values(query: dict[str, list[str]], key: str) -> list[str]:
    values: list[str] = []
    for raw_value in query.get(key, []):
        values.extend(_split_semicolon_list(raw_value))
    return values


def _split_semicolon_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _query_int(query: dict[str, list[str]], key: str, default: int | None) -> int | None:
    raw_value = query.get(key, [""])[0].strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _query_flag(query: dict[str, list[str]], key: str) -> bool:
    return query.get(key, [""])[0].strip().lower() in {"1", "true", "yes", "on"}


def _parse_json_object(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _queue_fieldnames_with_required_fields(
    fieldnames: list[str],
    row: dict[str, str],
) -> list[str]:
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
        *visual_fieldnames(),
    ]
    merged = list(fieldnames)
    for field in required:
        if field not in merged:
            merged.append(field)
    for field in merged:
        row.setdefault(field, "")
    return merged


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


def _candidate_has_review_crop(candidate: dict[str, Any]) -> bool:
    return all(
        str(candidate.get(field, "")).strip()
        for field in [
            "review_crop_x",
            "review_crop_y",
            "review_crop_size",
            "review_crop_candidate_width",
            "review_crop_candidate_height",
        ]
    )


def _review_crop_from_candidate(candidate: dict[str, Any]) -> dict[str, object] | None:
    if not _candidate_has_review_crop(candidate):
        return None
    return {
        "x": candidate.get("review_crop_x", ""),
        "y": candidate.get("review_crop_y", ""),
        "size": candidate.get("review_crop_size", ""),
        "candidate_width": candidate.get("review_crop_candidate_width", ""),
        "candidate_height": candidate.get("review_crop_candidate_height", ""),
        "fill_color": candidate.get("review_crop_fill_color", ""),
        "rotation_degrees": candidate.get("review_crop_rotation_degrees", ""),
    }


def _crop_filter_matches(entry: dict[str, Any], crop_filter: str) -> bool:
    crop_source = str(entry.get("crop_source", "")).strip().lower()
    if crop_filter == "all":
        return True
    if crop_filter == "missing":
        return not bool(entry.get("crop_has_crop"))
    if crop_filter == "with_crop":
        return bool(entry.get("crop_has_crop"))
    if crop_filter == "estimated":
        return (
            bool(entry.get("crop_has_crop"))
            and crop_source == "estimated"
            and not bool(entry.get("crop_commit_pending"))
        )
    if crop_filter == "confirmed":
        return bool(entry.get("crop_has_crop")) and crop_source != "estimated"
    return False


def _normalized_target_extensions(target_extensions: list[str] | set[str] | None) -> set[str]:
    extensions: set[str] = set()
    for value in target_extensions or []:
        text = str(value).strip().lower()
        if not text:
            continue
        extensions.add(text if text.startswith(".") else f".{text}")
    return extensions


def _crop_values_differ(current: dict[str, object], proposed: dict[str, object]) -> bool:
    for key in (
        "x",
        "y",
        "size",
        "candidate_width",
        "candidate_height",
        "fill_color",
        "rotation_degrees",
    ):
        if str(current.get(key, "")).strip() != str(proposed.get(key, "")).strip():
            return True
    return False


def _crop_entry_in_date_scope(
    entry: dict[str, Any],
    entry_dates: set[str],
    start_date: str,
    end_date: str,
) -> bool:
    entry_date = str(entry.get("entry_date", "")).strip()
    if entry_dates and entry_date not in entry_dates:
        return False
    if start_date and entry_date < start_date:
        return False
    if end_date and entry_date > end_date:
        return False
    return bool(entry_date or not (entry_dates or start_date or end_date))


def _entry_id_in_date_scope(entry_id: str, start_date: str, end_date: str) -> bool:
    if not start_date and not end_date:
        return True
    parts = str(entry_id or "").split(":")
    entry_date = parts[1] if len(parts) >= 2 else ""
    if start_date and entry_date < start_date:
        return False
    if end_date and entry_date > end_date:
        return False
    return bool(entry_date)


def _crop_status_label(has_crop: bool, crop_source: str) -> str:
    if not has_crop:
        return "without crop"
    if crop_source == "estimated":
        return "saved estimate"
    if crop_source == "manual":
        return "user saved"
    return "saved crop"


def _clear_review_crop(row: dict[str, str]) -> None:
    for field in _review_crop_fieldnames():
        row[field] = ""


def _set_review_crop(row: dict[str, str], crop: dict[str, Any], source: str) -> None:
    normalized = _normalized_review_crop(crop, Path(row["candidate_path"]), row.get("alignment_candidate_size", ""))
    row["review_crop_x"] = str(normalized["x"])
    row["review_crop_y"] = str(normalized["y"])
    row["review_crop_size"] = str(normalized["size"])
    row["review_crop_candidate_width"] = str(normalized["candidate_width"])
    row["review_crop_candidate_height"] = str(normalized["candidate_height"])
    row["review_crop_source"] = source
    row["review_crop_fill_color"] = str(normalized.get("fill_color", ""))
    row["review_crop_rotation_degrees"] = _format_float(normalized.get("rotation_degrees", 0))


def _normalized_review_crop(
    crop: dict[str, Any],
    candidate_path: Path,
    dimensions_hint: str = "",
) -> dict[str, Any]:
    candidate_width = _positive_int(crop.get("candidate_width"))
    candidate_height = _positive_int(crop.get("candidate_height"))
    size = _positive_int(crop.get("size"))
    x = _integer(crop.get("x"))
    y = _integer(crop.get("y"))
    if not candidate_width or not candidate_height:
        dimensions = _parse_dimensions_text(dimensions_hint) or _parse_dimensions_text(_image_dimensions(candidate_path))
        if dimensions:
            candidate_width, candidate_height = dimensions
    if not candidate_width or not candidate_height:
        raise ValueError("Crop requires candidate image dimensions")
    max_size = max(candidate_width, candidate_height) * 2
    size = min(size or min(candidate_width, candidate_height), max_size)
    if x >= candidate_width or y >= candidate_height or x + size <= 0 or y + size <= 0:
        raise ValueError("Crop must overlap the candidate image")
    fill_color = _normalized_fill_color(crop.get("fill_color"))
    rotation_degrees = _normalized_rotation_degrees(crop.get("rotation_degrees"))
    return {
        "x": x,
        "y": y,
        "size": size,
        "candidate_width": candidate_width,
        "candidate_height": candidate_height,
        "fill_color": fill_color,
        "rotation_degrees": rotation_degrees,
    }


def _review_crop_from_transformation_text(value: str) -> dict[str, object] | None:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return None
    crop = payload.get("review_crop")
    if not isinstance(crop, dict):
        return None
    values: dict[str, int] = {}
    for key in ["x", "y", "size", "candidate_width", "candidate_height"]:
        try:
            number = int(crop[key])
        except (KeyError, TypeError, ValueError):
            return None
        if key not in {"x", "y"} and number <= 0:
            return None
        values[key] = number
    if (
        values["x"] >= values["candidate_width"]
        or values["y"] >= values["candidate_height"]
        or values["x"] + values["size"] <= 0
        or values["y"] + values["size"] <= 0
    ):
        return None
    result: dict[str, object] = {
        **values,
        "source": str(crop.get("source", "manual")),
        "unit": str(crop.get("unit", "source_pixels")),
        "shape": str(crop.get("shape", "square")),
    }
    fill_color = _normalized_fill_color(crop.get("fill_color"))
    if fill_color:
        result["fill_color"] = fill_color
    rotation_degrees = _normalized_rotation_degrees(crop.get("rotation_degrees"))
    if rotation_degrees:
        result["rotation_degrees"] = rotation_degrees
    return result


def _square_crop_from_alignment(suggestion: dict[str, object]) -> dict[str, int]:
    candidate_width = int(suggestion["candidate_width"])
    candidate_height = int(suggestion["candidate_height"])
    x = int(suggestion["x"])
    y = int(suggestion["y"])
    width = int(suggestion["width"])
    height = int(suggestion["height"])
    size = min(width, height, candidate_width, candidate_height)
    center_x = x + width / 2
    center_y = y + height / 2
    crop_x = round(center_x - size / 2)
    crop_y = round(center_y - size / 2)
    crop_x = max(0, min(crop_x, candidate_width - size))
    crop_y = max(0, min(crop_y, candidate_height - size))
    return {
        "x": crop_x,
        "y": crop_y,
        "size": size,
        "candidate_width": candidate_width,
        "candidate_height": candidate_height,
    }


def _parse_dimensions_text(value: str) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d+)\s*(?:x|,)\s*(\d+)\s*$", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _positive_int(value: object) -> int:
    try:
        number = int(round(float(str(value))))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _non_negative_int(value: object) -> int:
    try:
        number = int(round(float(str(value))))
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _integer(value: object) -> int:
    try:
        return int(round(float(str(value))))
    except (TypeError, ValueError):
        return 0


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
        raise ValueError("Crop rotation must be between -180 and 180 degrees")
    return round(number, 3)


def _format_float(value: object) -> str:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        number = 0.0
    text = f"{number:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Project365 original-photo picker.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--queue", default=DEFAULT_QUEUE)
    parser.add_argument("--batch-plan", default=DEFAULT_BATCH_PLAN)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    config = PickerConfig(
        canonical_root=Path(args.canonical_root),
        queue_path=Path(args.queue),
        batch_plan_path=Path(args.batch_plan),
    )
    server = serve_picker(config, args.host, args.port)
    print(f"Project365 original picker: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PICKER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 Original Picker</title>
<style>
:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #202124;
  --muted: #667085;
  --line: #d9dee5;
  --line-strong: #c5ccd6;
  --accent: #17695d;
  --accent-soft: #e4f2ef;
  --hover: #f0f4f7;
  --warn: #9a5b00;
  --reject: #9f2d2d;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}
button, input, select, textarea {
  font: inherit;
}
button {
  color: var(--ink);
}
.app {
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr);
  height: 100vh;
}
.sidebar {
  border-right: 1px solid var(--line);
  background: var(--panel);
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.topbar {
  padding: 14px 12px 12px;
  border-bottom: 1px solid var(--line);
  display: grid;
  gap: 8px;
}
.title {
  font-size: 16px;
  font-weight: 700;
}
.summary {
  font-size: 12px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.batch-summary {
  min-height: 16px;
}
.filter {
  width: 100%;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 0 8px;
}
.filter-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}
.entry-date-jump {
  display: inline-grid;
  grid-template-columns: 112px 112px auto;
  gap: 8px;
  justify-content: start;
}
.entry-date-jump input {
  width: 100%;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 0 8px;
  font-size: 13px;
}
.entry-list {
  overflow: auto;
  min-height: 0;
}
.entry-paging-actions {
  display: grid;
  grid-template-columns: 7fr 3fr;
  gap: 8px;
}
.entry-paging-actions .action-button {
  width: 100%;
}
.selection-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.entry-item {
  width: 100%;
  border-bottom: 1px solid var(--line);
  background: transparent;
  display: grid;
  grid-template-columns: 28px 56px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  transition: background-color 120ms ease;
}
.entry-item:hover {
  background: var(--hover);
}
.entry-item.active {
  background: var(--accent-soft);
  box-shadow: inset 3px 0 0 var(--accent);
}
.entry-check {
  width: 18px;
  height: 18px;
}
.entry-thumb {
  width: 52px;
  height: 52px;
  object-fit: cover;
  background: #222;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.entry-thumb-button {
  width: 52px;
  height: 52px;
  border: 0;
  background: transparent;
  padding: 0;
  cursor: pointer;
}
.entry-thumb-button .entry-thumb {
  display: block;
}
.entry-button {
  border: 0;
  background: transparent;
  padding: 0;
  text-align: left;
  cursor: pointer;
  display: grid;
  gap: 5px;
  min-width: 0;
}
.entry-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
}
.entry-date {
  font-weight: 650;
  white-space: nowrap;
  font-size: 17px;
  line-height: 1.1;
  overflow: hidden;
  text-overflow: ellipsis;
}
.badge {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 6px;
  font-size: 10px;
  line-height: 1.2;
  color: var(--muted);
  white-space: nowrap;
  background: #fff;
}
.badge.selected {
  color: var(--accent);
  border-color: var(--accent);
}
.badge.search_needed {
  color: var(--warn);
}
.badge.rejected {
  color: var(--reject);
}
.entry-commit {
  grid-column: 3;
  justify-self: start;
  height: 28px;
  padding: 0 9px;
}
.entry-commit[aria-hidden="true"] {
  visibility: hidden;
  pointer-events: none;
}
.main {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}
.main-header {
  padding: 12px 18px;
  border-bottom: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(8px);
  display: flex;
  justify-content: flex-start;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: nowrap;
}
.main-header > div:first-child {
  min-width: 180px;
  flex: 0 1 auto;
}
.control-panel-link {
  display: inline-flex;
  align-items: center;
  text-decoration: none;
  white-space: nowrap;
  width: fit-content;
}
.decision-count {
  margin-left: 7px;
  font-size: 11px;
  font-weight: 600;
}
.entry-heading {
  font-size: 18px;
  font-weight: 700;
}
.entry-title-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.position-pill {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--muted);
  background: #fff;
  white-space: nowrap;
}
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: nowrap;
  justify-content: flex-start;
  min-width: 0;
  flex: 1 1 420px;
}
.navigation-actions {
  display: inline-flex;
  gap: 6px;
}
.icon-button, .action-button {
  border: 1px solid var(--line);
  background: #fff;
  border-radius: 6px;
  height: 34px;
  padding: 0 10px;
  cursor: pointer;
  white-space: nowrap;
  line-height: 1;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background-color 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
}
.icon-button:hover,
.action-button:hover {
  background: var(--hover);
  border-color: var(--line-strong);
}
.icon-button:focus-visible,
.action-button:focus-visible,
.filter:focus-visible,
.entry-date-jump input:focus-visible,
.crawl-panel input:focus-visible,
textarea:focus-visible {
  outline: 2px solid #0b61d8;
  outline-offset: 2px;
}
.icon-button:disabled,
.action-button:disabled {
  color: var(--muted);
  cursor: default;
  opacity: 0.55;
}
.action-button.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.action-button.primary:hover {
  background: #13594f;
  border-color: #13594f;
}
.action-button.flagged:not(:disabled) {
  background: #fff7ed;
  border-color: #c2410c;
  color: #9a3412;
  font-weight: 650;
}
.action-button.flagged:not(:disabled):hover {
  background: #ffedd5;
  border-color: #9a3412;
}
.action-button.linked:not(:disabled) {
  background: #7f1d1d;
  border-color: #7f1d1d;
  color: #fff;
  font-weight: 700;
}
.action-button.linked:not(:disabled):hover {
  background: #651616;
  border-color: #651616;
}
.video-frame-modal {
  position: fixed;
  inset: 0;
  z-index: 70;
  display: none;
  align-items: center;
  justify-content: center;
  background: rgba(15, 23, 42, 0.56);
  padding: 24px;
}
.video-frame-modal.is-open {
  display: flex;
}
.video-frame-dialog {
  width: min(1180px, 100%);
  max-height: min(760px, 94vh);
  overflow: auto;
  background: var(--panel);
  border-radius: 8px;
  border: 1px solid var(--line);
  box-shadow: 0 24px 60px rgba(15, 23, 42, 0.25);
  padding: 16px;
  display: grid;
  gap: 14px;
}
.video-frame-header,
.video-frame-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.video-frame-title {
  display: grid;
  gap: 2px;
}
.video-frame-title strong {
  font-size: 17px;
}
.video-frame-title span,
.video-frame-help {
  color: var(--muted);
  font-size: 12px;
}
.video-frame-scrubber {
  display: grid;
  grid-template-columns: minmax(280px, 1.2fr) minmax(260px, 1fr);
  gap: 12px;
  align-items: stretch;
}
.video-frame-player {
  width: 100%;
  max-height: 360px;
  background: #111827;
  border: 1px solid var(--line);
  border-radius: 8px;
}
.video-frame-custom {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #f8fafc;
  padding: 10px;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  align-items: center;
}
.video-frame-custom label {
  display: grid;
  gap: 4px;
  color: var(--muted);
  font-size: 12px;
}
.video-frame-custom input[type="range"] {
  width: 100%;
}
.video-frame-time {
  font-variant-numeric: tabular-nums;
  color: var(--text);
  font-size: 13px;
  font-weight: 700;
}
.video-frame-step-controls {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 6px;
}
.video-frame-step-controls .action-button {
  min-width: 0;
  padding-left: 6px;
  padding-right: 6px;
}
.video-frame-capture-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.video-frame-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.video-frame-processing {
  grid-column: 1 / -1;
  min-height: 120px;
  display: grid;
  place-items: center;
  border: 1px dashed var(--line);
  border-radius: 8px;
  color: var(--muted);
  background: #f8fafc;
  text-align: center;
  padding: 20px;
}
.video-frame-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
  display: grid;
  position: relative;
}
.video-frame-card.custom-frame {
  border: 2px solid #0f766e;
  box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.24);
}
.video-frame-card.custom-frame::before {
  content: "Captured";
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 1;
  border-radius: 999px;
  background: #0f766e;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  padding: 5px 8px;
}
.video-frame-card .image-preview-trigger {
  display: block;
  height: auto;
  line-height: 0;
}
.video-frame-card img {
  display: block;
  width: 100%;
  aspect-ratio: 4 / 3;
  object-fit: cover;
  background: #111827;
}
.video-frame-controls {
  display: grid;
  gap: 6px;
  padding: 8px;
  font-size: 12px;
}
.video-frame-controls label {
  display: flex;
  align-items: center;
  gap: 6px;
}
.workspace {
  min-height: 0;
  overflow: auto;
  padding: 18px;
  display: grid;
  grid-template-columns: minmax(380px, 520px) minmax(360px, 1fr);
  gap: 18px;
}
.source-pane {
  position: sticky;
  top: 0;
  align-self: start;
  display: grid;
  gap: 8px;
  min-width: 0;
}
.source-frame,
.candidate-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
.source-frame {
  overflow: hidden;
  -webkit-user-select: none;
  user-select: none;
}
.source-frame.is-draggable {
  cursor: grab;
}
.source-frame.is-draggable:active {
  cursor: grabbing;
}
.source-frame.is-draggable .source-image {
  cursor: grab;
}
.source-image {
  display: block;
  width: 100%;
  max-height: calc(100vh - 170px);
  object-fit: contain;
  background: #222;
  -webkit-touch-callout: none;
  -webkit-user-select: none;
  user-select: none;
}
.source-meta,
.candidate-meta {
  padding: 9px 10px;
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.people-script {
  color: var(--muted);
  font-size: 11px;
  font-style: italic;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.candidate-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
  align-content: start;
}
.candidate-group-heading {
  grid-column: 1 / -1;
  margin-top: 2px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.candidate-pane {
  display: grid;
  gap: 10px;
  align-content: start;
}
.compare-pane {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(180px, 1fr);
  gap: 10px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
.compare-frame {
  min-width: 0;
  display: grid;
  gap: 7px;
}
.compare-label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}
.compare-image {
  width: 100%;
  max-height: 320px;
  object-fit: contain;
  background: #222;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.candidate-toolbar {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(150px, auto) minmax(150px, auto) minmax(150px, auto) auto;
  gap: 8px;
  align-items: center;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
.candidate-toolbar input,
.candidate-toolbar select {
  width: 100%;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 0 8px;
  font-size: 13px;
}
.candidate-summary {
  grid-column: 1 / -1;
}
.date-range-controls {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.source-pane .date-range-controls {
  background: var(--panel);
  border-top: 1px solid var(--line);
  padding-top: 8px;
}
.date-range-controls .action-button.used-range {
  background: #e5f4f0;
  border-color: var(--accent);
  color: #0f5f52;
  box-shadow: inset 0 0 0 1px rgba(21, 118, 102, 0.18);
}
.time-filter-controls {
  flex-basis: 100%;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.time-filter-controls .action-button[aria-pressed="true"] {
  background: #e5f4f0;
  border-color: var(--accent);
  color: #0f5f52;
  box-shadow: inset 0 0 0 1px rgba(21, 118, 102, 0.18);
}
.manual-date-search {
  flex-basis: 100%;
  display: grid;
  grid-template-columns: 104px 104px max-content auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
}
.manual-date-search input[type="date"] {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 0 8px;
  font-size: 13px;
}
.index-search-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 20px;
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}
.index-search-options {
  display: grid;
  gap: 2px;
  align-items: center;
}
.index-search-toggle input {
  width: 16px;
  height: 16px;
}
.index-search-scope {
  flex-basis: 100%;
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.candidate-location-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}
.candidate-location-toggle input {
  width: 16px;
  height: 16px;
}
.candidate-card {
  overflow: hidden;
  display: grid;
  grid-template-rows: 210px auto auto;
}
.candidate-card.selected {
  outline: 3px solid var(--accent);
}
.candidate-card.project-originals-source {
  border: 3px solid #b42318;
  box-shadow: 0 0 0 1px rgba(180, 35, 24, 0.18), 0 1px 2px rgba(16, 24, 40, 0.04);
}
.candidate-card.project-originals-source.selected {
  outline-offset: 3px;
}
.candidate-image {
  width: 100%;
  height: 210px;
  object-fit: contain;
  background: #222;
}
.image-preview-trigger {
  border: 0;
  padding: 0;
  background: transparent;
  cursor: zoom-in;
  width: 100%;
  height: 210px;
}
.image-preview-trigger:focus-visible {
  outline: 3px solid rgba(23, 105, 93, 0.45);
  outline-offset: 2px;
}
.image-preview-modal {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: rgba(22, 27, 34, 0.78);
}
.image-preview-modal.is-open {
  display: flex;
}
.image-preview-dialog {
  position: relative;
  width: fit-content;
  max-width: min(96vw, 1600px);
  max-height: 94vh;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.image-preview-dialog img {
  display: block;
  width: auto;
  height: auto;
  max-width: min(96vw, 1600px);
  max-height: 86vh;
  object-fit: contain;
  background: #222;
  border-radius: 8px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.32);
}
.image-preview-close {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 38px;
  height: 38px;
  border: 1px solid rgba(255, 255, 255, 0.7);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.92);
  color: #111827;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
}
.image-preview-caption {
  max-width: min(100%, 96vw);
  color: #fff;
  font-size: 13px;
  overflow-wrap: anywhere;
  text-align: center;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
}
.candidate-filename-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.candidate-filename-row strong {
  min-width: 0;
  overflow-wrap: anywhere;
}
.location-indicator {
  width: 17px;
  height: 17px;
  margin-left: auto;
  flex: 0 0 17px;
  color: #98a2b3;
}
.location-indicator.has-location {
  color: #b42318;
}
.candidate-actions {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  padding: 10px;
}
.candidate-actions.compact {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.candidate-actions.main-only {
  grid-template-columns: minmax(0, 1fr);
}
.candidate-actions .action-button {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  min-height: 34px;
  height: auto;
  padding: 6px 8px;
  white-space: normal;
  line-height: 1.12;
  text-align: center;
  overflow-wrap: anywhere;
}
.associated-date-panel {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: minmax(0, 120px) minmax(0, 98px) auto;
  gap: 8px;
  align-items: center;
}
.associated-date-panel[hidden] {
  display: none;
}
.associated-date-panel input {
  box-sizing: border-box;
  min-width: 0;
  width: 100%;
  height: 30px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0 6px;
  font-size: 12px;
  line-height: 1;
}
.associated-date-panel input[type="date"] {
  font-size: 11px;
}
.associated-date-panel [data-action="save-flag"] {
  box-sizing: border-box;
  height: 30px;
  padding: 0 8px;
  font-size: 12px;
}
.associated-date-choices {
  grid-column: 1 / -1;
  display: grid;
  gap: 6px;
}
.associated-date-choice {
  min-height: 42px;
  width: 100%;
  justify-content: stretch;
  gap: 10px;
  line-height: 1.2;
  white-space: normal;
}
.associated-date-choice strong,
.associated-date-choice span {
  display: block;
  min-width: 0;
}
.associated-date-choice strong {
  flex: 0 0 auto;
  font-size: 12px;
}
.associated-date-choice span {
  flex: 1 1 auto;
  color: var(--muted);
  font-size: 13px;
  text-align: right;
  overflow-wrap: anywhere;
}
.associated-date-choice:hover span {
  color: inherit;
}
.associated-date-choice.is-selected:not(:disabled) {
  border-color: var(--accent);
  background: #ecfdf5;
  color: var(--accent);
}
.empty {
  padding: 18px;
  color: var(--muted);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 15px;
  line-height: 1.35;
  max-width: 560px;
}
.photo-drop-row {
  display: grid;
  grid-template-columns: minmax(0, 4fr) minmax(120px, 1fr);
  gap: 8px;
  align-items: stretch;
}
.photo-drop-target,
.photo-link-drag-lane {
  min-height: 56px;
  border: 2px dashed var(--line-strong);
  border-radius: 8px;
  background: #fff;
  display: grid;
  place-content: center;
  gap: 2px;
  padding: 8px 12px;
  text-align: center;
  color: var(--ink);
  transition: background-color 120ms ease, border-color 120ms ease;
}
.photo-drop-target span {
  color: var(--muted);
  font-size: 11px;
}
.photo-drop-target.active,
.photo-link-drag-lane.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.photo-drop-target.disabled {
  opacity: 0.6;
}
.photo-link-drag-lane {
  border-color: var(--line);
  color: var(--ink);
  font-size: 11px;
  line-height: 1.25;
  background: #f8fafc;
}
.photo-link-drag-lane .action-button {
  margin-top: 6px;
  justify-self: center;
  white-space: normal;
}
.search-panel {
  box-sizing: border-box;
  min-width: 0;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  display: grid;
  gap: 8px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
.search-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.search-panel-title {
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
}
.search-panel-body {
  display: grid;
  gap: 10px;
}
.search-panel-body[hidden] {
  display: none;
}
.crawl-panel {
  background: transparent;
  display: grid;
  gap: 8px;
}
.crawl-panel label {
  color: var(--muted);
  font-size: 12px;
}
.crawl-panel input {
  width: 100%;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0 8px;
  font-size: 13px;
}
.folder-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 8px;
}
.search-actions {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.crawl-status {
  color: var(--muted);
  font-size: 12px;
  font-weight: 400;
  line-height: 1.35;
  max-height: 92px;
  overflow: auto;
  overflow-wrap: anywhere;
  border-top: 1px solid var(--line);
  padding-top: 6px;
}
.crawl-status.is-running {
  color: #7f1d1d;
  font-weight: 700;
}
.entry-paging {
  border-top: 1px solid var(--line);
  padding: 10px;
  display: grid;
  gap: 8px;
  background: var(--panel);
}
@media (max-width: 900px) {
  .app {
    grid-template-columns: 1fr;
    grid-template-rows: 260px minmax(0, 1fr);
  }
  .main-header,
  .actions {
    flex-wrap: wrap;
  }
  .sidebar {
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .workspace {
    grid-template-columns: 1fr;
  }
  .search-actions {
    grid-template-columns: 1fr;
  }
  .manual-date-search {
    grid-template-columns: 1fr;
  }
  .candidate-toolbar {
    grid-template-columns: 1fr;
  }
  .compare-pane {
    grid-template-columns: 1fr;
  }
  .source-pane {
    position: static;
  }
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="topbar">
      <a class="action-button control-panel-link" href="/" title="Return to the Project365 control panel">Project365 Control Panel</a>
      <div class="title">Project365 Original Picker</div>
      <div id="summary" class="summary">Loading</div>
      <select id="filter" class="filter">
        <option value="needs_action">Needs action</option>
        <option value="needs_review">Needs review</option>
        <option value="accepted_not_applied">Accepted not applied</option>
        <option value="selected">Selected</option>
        <option value="fallback">Fallback: needs better original</option>
        <option value="search_needed">Needs broader search</option>
        <option value="rejected">Rejected</option>
        <option value="all">All entries</option>
      </select>
      <div class="filter-row">
        <select id="batchFilter" class="filter">
          <option value="">All batches</option>
        </select>
        <button id="clearBatchFilter" class="action-button" title="Show all batches">All batches</button>
      </div>
      <div id="batchSummary" class="summary batch-summary"></div>
      <div class="entry-date-jump" role="search" aria-label="Open entries by date range">
        <input id="entryDateJump" type="text" inputmode="numeric" pattern="\\d{4}-\\d{2}-\\d{2}" maxlength="10" placeholder="yyyy-mm-dd" aria-label="Entry date yyyy-mm-dd">
        <input id="entryDateJumpEnd" type="text" inputmode="numeric" pattern="\\d{4}-\\d{2}-\\d{2}" maxlength="10" placeholder="yyyy-mm-dd" aria-label="End entry date yyyy-mm-dd">
        <button id="entryDateJumpButton" class="action-button" type="button" title="Open matching Project365 entry dates">Go</button>
      </div>
      <div id="entryDateJumpStatus" class="summary"></div>
      <div class="selection-actions">
        <button id="selectVisible" class="action-button">Check shown entries</button>
        <button id="clearSelection" class="action-button">Uncheck entries</button>
      </div>
      <div id="selectionSummary" class="summary">0 selected</div>
    </div>
    <div id="entryList" class="entry-list"></div>
    <div id="entryPaging" class="entry-paging" hidden>
      <div id="entryPagingSummary" class="summary"></div>
      <div class="entry-paging-actions">
        <button id="loadMoreEntries" class="action-button">Load more</button>
        <button id="loadAllEntries" class="action-button">Load all</button>
      </div>
    </div>
  </aside>
  <main class="main">
    <div class="main-header">
      <div>
        <div class="entry-title-row">
          <div id="entryHeading" class="entry-heading">No entry selected</div>
          <div id="entryPosition" class="position-pill"></div>
        </div>
        <div id="entrySubhead" class="summary"></div>
      </div>
      <div class="actions">
        <div class="navigation-actions" role="group" aria-label="Entry navigation">
          <button id="previousEntryButton" class="action-button" title="Shortcut: Left arrow">Previous entry (Left)</button>
          <button id="nextEntryButton" class="action-button" title="Shortcut: Right arrow">Next entry (Right)</button>
        </div>
        <button id="applyDecisionsButton" class="action-button primary" title="Apply reviewed selections, linked originals, rejections, fallbacks, and associated-photo flags to the database">Apply decisions <span id="acceptedDecisionCount" class="decision-count">0 accepted</span><span id="associatedDecisionCount" class="decision-count">0 flagged</span><span id="rejectedDecisionCount" class="decision-count">0 rejected</span></button>
        <button id="fallbackButton" class="icon-button" title="Shortcut: P. Use the photo already stored in the Project365 entry instead of an external original">Use Project365 photo (P)</button>
        <button id="rejectAllButton" class="icon-button" title="Shortcut: R. Reject every candidate currently available for this target photo">Reject all (R)</button>
        <button id="clearButton" class="icon-button" title="Reset pending selections, rejections, notes, and candidate expansions for this entry">Reset decisions</button>
      </div>
    </div>
    <div class="workspace">
      <section class="source-pane">
        <div class="search-panel">
          <div class="search-panel-header">
            <div class="search-panel-title">Search</div>
            <button id="searchPanelToggle" class="action-button" type="button" aria-expanded="false" aria-controls="searchPanelBody">Expand</button>
          </div>
          <div class="crawl-panel">
            <div id="searchPanelBody" class="search-panel-body" hidden>
              <label>Search folders</label>
              <div class="folder-row">
                <input id="crawlRoots" placeholder="Choose folders or paste paths">
                <button id="chooseFolder" class="action-button">Choose folder</button>
                <button id="choosePhoto" class="action-button" title="Link an original photo without copying it">Choose photo</button>
              </div>
              <div class="search-actions">
                <button id="crawlCurrent" class="action-button primary" title="Search the currently open entry">Current</button>
                <button id="crawlSelected" class="action-button" title="Search checked entries">Selected</button>
                <button id="crawlVisible" class="action-button" title="Search every entry in the current filter">Visible list</button>
              </div>
            </div>
            <div id="crawlStatus" class="crawl-status">Use the date-range buttons to search the existing index. Add a folder only for a targeted search.</div>
          </div>
          <div class="date-range-controls" role="group" aria-label="Expand candidate date range">
            <button id="defaultDateRange" class="action-button" type="button">Default</button>
            <button class="action-button" type="button" data-range-days="1">±1 day</button>
            <button class="action-button" type="button" data-range-days="3">±3 days</button>
            <button class="action-button" type="button" data-range-days="5">±5 days</button>
            <button class="action-button" type="button" data-range-days="15">±15 days</button>
            <button class="action-button" type="button" data-range-days="30">±30 days</button>
            <div class="time-filter-controls" role="group" aria-label="Filter candidates by time of day">
              <button class="action-button" type="button" data-time-filter="morning" aria-pressed="false" title="Morning, 4:00 AM to 7:59 AM">4-8a</button>
              <button class="action-button" type="button" data-time-filter="midday" aria-pressed="false" title="Midday, 8:00 AM to 11:59 AM">8-12p</button>
              <button class="action-button" type="button" data-time-filter="early-afternoon" aria-pressed="false" title="Early afternoon, 12:00 PM to 3:59 PM">12-4p</button>
              <button class="action-button" type="button" data-time-filter="late-afternoon" aria-pressed="false" title="Late afternoon, 4:00 PM to 7:59 PM">4-8p</button>
              <button class="action-button" type="button" data-time-filter="early-evening" aria-pressed="false" title="Early evening, 8:00 PM to 11:59 PM">8-12a</button>
              <button class="action-button" type="button" data-time-filter="late-evening" aria-pressed="false" title="Late evening, 12:00 AM to 3:59 AM">12-4a</button>
            </div>
            <div class="manual-date-search">
              <input id="indexDateStart" type="date" aria-label="Index search start date">
              <input id="indexDateEnd" type="date" aria-label="Index search end date">
              <div class="index-search-options">
                <label class="index-search-toggle"><input id="indexSearchWholeIndex" type="checkbox"> Whole index</label>
                <label class="index-search-toggle"><input id="indexSearchModifiedDate" type="checkbox"> Include modified dates</label>
              </div>
              <button id="searchIndexDateRange" class="action-button" type="button" title="Add indexed photos from the entered date range">Add dates</button>
            </div>
            <div id="indexSearchScope" class="index-search-scope"></div>
          </div>
        </div>
        <div class="photo-drop-row">
          <div id="photoDropTarget" class="photo-drop-target" aria-label="Drop a photo or video for the current entry">
            <strong>Drop photo/video here</strong>
            <span id="photoDropTargetHint">Copies stills into Source Data, or opens movie screenshots</span>
          </div>
          <div id="photoLinkDropTarget" class="photo-link-drag-lane" aria-label="Link an original photo for the current entry">
            <span>Linked photo</span>
            <button id="chooseLinkedPhoto" class="action-button" type="button">Open drop window</button>
          </div>
        </div>
        <div id="sourceFrame" class="source-frame" draggable="false" aria-label="Original Project365 target photo">
          <img id="sourceImage" class="source-image" alt="" draggable="false">
          <div id="sourceMeta" class="source-meta"></div>
        </div>
      </section>
      <section class="candidate-pane">
        <div class="candidate-toolbar">
          <input id="candidateFilter" placeholder="Filter candidates">
          <select id="candidateEvidenceFilter">
            <option value="all">All candidates</option>
            <option value="quality">Improved / final / master</option>
            <option value="entry_date">Date matches this entry</option>
            <option value="filename_entry_date">Filename date matches</option>
            <option value="media_entry_date">Media date matches</option>
            <option value="filesystem_entry_date">Filesystem date matches</option>
            <option value="media_date">Has media date</option>
            <option value="alignment">Has alignment score</option>
            <option value="selected">Selected candidate</option>
          </select>
          <select id="candidateFolderFilter">
            <option value="">All folders</option>
          </select>
          <select id="candidateSort">
            <option value="capture_time">Capture time</option>
            <option value="visual">Visual match</option>
            <option value="best">Best quality</option>
            <option value="dimensions">Largest dimensions</option>
            <option value="largest">Largest file</option>
            <option value="filename">Filename</option>
            <option value="alignment">Closest alignment</option>
          </select>
          <label class="candidate-location-toggle"><input id="candidateLocationOnly" type="checkbox"> Location only</label>
          <div id="candidateSummary" class="summary candidate-summary"></div>
        </div>
        <div id="candidateGrid" class="candidate-grid"></div>
      </section>
  </div>
  </main>
</div>
<div id="videoFrameModal" class="video-frame-modal" aria-hidden="true">
  <div class="video-frame-dialog" role="dialog" aria-modal="true" aria-label="Choose movie screenshots">
    <div class="video-frame-header">
      <div class="video-frame-title">
        <strong>Choose movie screenshots</strong>
        <span id="videoFrameSubtitle"></span>
      </div>
      <button class="icon-button" type="button" onclick="closeVideoFrameChooser()" aria-label="Close screenshot chooser">×</button>
    </div>
    <div id="videoFrameHelp" class="video-frame-help">Pick one main image, and any supplemental attachments. Click a screenshot to inspect it larger.</div>
    <div class="video-frame-scrubber">
      <video id="videoFramePlayer" class="video-frame-player" controls preload="metadata"></video>
      <div class="video-frame-custom">
        <label>
          Scrub movie
          <input id="videoFrameSlider" type="range" min="0" max="0" step="0.05" value="0" oninput="scrubVideoFrameSlider()">
        </label>
        <div class="video-frame-capture-row">
          <span id="videoFrameSliderLabel" class="video-frame-time">0:00</span>
          <button id="videoFrameAddCustom" class="action-button primary" type="button" onclick="captureCurrentVideoFrame()" title="Capture the current frame. Shortcut: Enter">Capture frame</button>
        </div>
        <div class="video-frame-step-controls" aria-label="Micro-adjust video time">
          <button class="action-button" type="button" onclick="stepVideoFrameTime(-1)">-1s</button>
          <button class="action-button" type="button" onclick="stepVideoFrameTime(-0.1)">-0.1s</button>
          <button id="videoFramePlayPause" class="action-button" type="button" onclick="toggleVideoFramePlayback()" title="Play/pause movie. Shortcut: Space">Play</button>
          <button class="action-button" type="button" onclick="stepVideoFrameTime(0.1)">+0.1s</button>
          <button class="action-button" type="button" onclick="stepVideoFrameTime(1)">+1s</button>
        </div>
      </div>
    </div>
    <div id="videoFrameGrid" class="video-frame-grid"></div>
    <div class="video-frame-actions">
      <label><input id="videoFrameNoMain" type="radio" name="videoFrameMain" value="" checked> No main image</label>
      <div>
        <button class="action-button" type="button" onclick="closeVideoFrameChooser()">Cancel</button>
        <button id="videoFrameSave" class="action-button primary" type="button" onclick="saveVideoFrameChoices()">Save screenshots</button>
      </div>
    </div>
  </div>
</div>
<div id="imagePreviewModal" class="image-preview-modal" onclick="handleImagePreviewBackdrop(event)" aria-hidden="true">
  <div class="image-preview-dialog" role="dialog" aria-modal="true" aria-label="Large image preview">
    <button class="image-preview-close" type="button" onclick="closeImagePreview()" aria-label="Close image preview">&times;</button>
    <img id="imagePreviewImage" alt="">
    <div id="imagePreviewCaption" class="image-preview-caption"></div>
  </div>
</div>
<script>
const state = {
  entries: [],
  batches: [],
  selectedEntryId: null,
  currentEntry: null,
  crawlJobId: null,
  selectedEntryIds: new Set(),
  lastCheckedIndex: null,
  urlEntryIds: [],
  urlEntryDates: [],
  batchEntryIds: [],
  batchEntryDates: [],
  initialBatchId: "",
  entryLimit: 20,
  entryOffset: 0,
  entryHasMore: false,
  entryLoadingMore: false,
  entryLoadingAll: false,
  entryLoadAll: false,
  entryRequestId: 0,
  entryDetailCache: new Map(),
  entryDetailRequests: new Map(),
  suppressedCommittedEntryIds: new Set(),
  previousEntryStack: [],
  candidateImageObserver: null,
  candidateRenderLimit: 40,
  candidateRenderObserver: null,
  summaryRefreshTimer: null,
  cropEstimateJobId: "",
  activePhotoIndexFolder: "",
  defaultPhotoIndexFolder: "",
  activeCandidateTimeFilters: new Set(),
  videoFrameReview: null,
  videoFramePendingToken: "",
  filenameDateScopeEntryId: "",
  filenameDateScopeDays: 0,
  filenameDateScopeStart: "",
  filenameDateScopeEnd: "",
  archivedBatchCount: 0,
  lastLinkedPhotoRefreshStamp: "",
  linkedPhotoDropEntryId: "",
  linkedPhotoPopupPoller: null
};
const LINKED_PHOTO_REFRESH_KEY = "project365-linked-photo-refresh";

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Request failed");
  return payload;
}

function setCrawlStatus(message, running = false) {
  const target = document.getElementById("crawlStatus");
  target.textContent = message;
  target.classList.toggle("is-running", running);
}

function updateSourceDragSurface(entry) {
  const frame = document.getElementById("sourceFrame");
  const image = document.getElementById("sourceImage");
  frame.draggable = false;
  image.draggable = Boolean(entry?.source_token);
  frame.classList.toggle("is-draggable", Boolean(entry?.source_token));
}

function statusLabel(status) {
  const labels = {
    needs_action: "needs action",
    needs_review: "review candidates",
    search_needed: "broaden search",
    accepted_not_applied: "accepted not applied",
    selected: "selected",
    fallback: "needs better original",
    rejected: "rejected"
  };
  return labels[status] || status.replace("_", " ");
}

const CANDIDATE_EVIDENCE_FILTERS = [
  {value: "all", label: "All candidates"},
  {value: "likely", label: "Likely matches"},
  {value: "quality", label: "Improved / final / master"},
  {value: "entry_date", label: "Date matches this entry"},
  {value: "filename_entry_date", label: "Filename date matches"},
  {value: "media_entry_date", label: "Media date matches"},
  {value: "filesystem_entry_date", label: "Filesystem date matches"},
  {value: "media_date", label: "Has media date"},
  {value: "alignment", label: "Has alignment score"},
  {value: "visual", label: "Has visual score"},
  {value: "selected", label: "Selected candidate"}
];

function batchStatusLabel(batch) {
  const candidateCount = Number(batch.candidate_count || "0");
  const folderNeeded = Number(batch.folder_needed_date_count || "0");
  const statuses = String(batch.statuses || "");
  if (candidateCount > 0 && folderNeeded > 0) return "review plus broader search";
  if (candidateCount > 0) return "review candidates";
  if (statuses.includes("all_candidates_rejected")) return "all found rejected";
  if (statuses.includes("only_export_equivalent_candidates")) return "only export copies";
  return "broaden search";
}

function formatPriorMatcher(entry) {
  const decision = (entry.current_decision || "").trim();
  const matchStatus = (entry.current_match_status || "").trim();
  if (!decision && !matchStatus) return "";
  const normalizedDecision = decision === "fallback" ? "no clear external original" : decision;
  const parts = [];
  if (matchStatus) parts.push(matchStatus.replaceAll("_", " "));
  if (normalizedDecision) parts.push(normalizedDecision.replaceAll("_", " "));
  return `prior matcher: ${escapeHtml(parts.join(" · "))}`;
}

function peopleScript(names) {
  const values = Array.isArray(names) ? names.filter(Boolean) : [];
  if (!values.length) return "";
  const text = values.join(", ");
  return `<span class="people-script" title="${escapeHtml(text)}">${escapeHtml(text)}</span>`;
}

async function loadSummary() {
  const summary = await fetchJson("/api/summary");
  if (Object.prototype.hasOwnProperty.call(summary, "default_photo_index_folder")) {
    state.defaultPhotoIndexFolder = String(summary.default_photo_index_folder || "").trim();
  }
  if (Object.prototype.hasOwnProperty.call(summary, "active_photo_index_folder")) {
    setActivePhotoIndexFolder(summary.active_photo_index_folder || state.defaultPhotoIndexFolder);
  } else if (state.defaultPhotoIndexFolder && !state.activePhotoIndexFolder) {
    setActivePhotoIndexFolder(state.defaultPhotoIndexFolder);
  }
  const counts = summary.status_counts || {};
  const pending = summary.pending_decisions || {};
  const pendingEntries = summary.pending_entry_counts || {};
  const queueState = summary.queue_exists === false ? " · search queue missing" : "";
  document.getElementById("summary").textContent =
    `${summary.entry_count} entries · ${counts.needs_review || 0} review · ${counts.selected || 0} selected · ${counts.fallback || 0} fallback · ${counts.search_needed || 0} need broader search${queueState}`;
  const acceptedFiles = pending.accepted || 0;
  const acceptedEntries = pendingEntries.accepted ?? acceptedFiles;
  const associatedFiles = pending.associated || 0;
  const associatedEntries = pendingEntries.associated ?? associatedFiles;
  const rejectedFiles = pending.rejected || 0;
  document.getElementById("acceptedDecisionCount").textContent = acceptedFiles === acceptedEntries
    ? `${acceptedEntries} accepted entries`
    : `${acceptedEntries} accepted entries (${acceptedFiles} files)`;
  document.getElementById("associatedDecisionCount").textContent = associatedFiles === associatedEntries
    ? `${associatedEntries} flagged`
    : `${associatedEntries} flagged (${associatedFiles} files)`;
  document.getElementById("rejectedDecisionCount").textContent = `${rejectedFiles} rejected`;
  document.getElementById("applyDecisionsButton").title =
    `Apply ${acceptedEntries} linked, ${associatedEntries} flagged, ${rejectedFiles} rejected, and any fallback decisions to the database`;
}

async function loadBatches() {
  const payload = await fetchJson("/api/batches");
  state.batches = payload.batches || [];
  state.archivedBatchCount = payload.archived_count || 0;
  renderBatchFilter();
}

function renderBatchFilter() {
  const select = document.getElementById("batchFilter");
  const previous = select.value;
  select.innerHTML = "";
  select.appendChild(new Option("All batches", ""));
  for (const batch of state.batches) {
    const label = `${batch.batch_id} · ${batch.start_date} to ${batch.end_date} · ${batch.entry_count || "0"} entries · ${batch.candidate_count || "0"} candidates · ${batchStatusLabel(batch)}`;
    select.appendChild(new Option(label, batch.batch_id));
  }
  const requested = state.initialBatchId || previous;
  if ([...select.options].some(option => option.value === requested)) {
    select.value = requested;
    state.initialBatchId = "";
    syncBatchFilterState();
  } else if ([...select.options].some(option => option.value === previous)) {
    select.value = previous;
  }
  renderBatchSummary();
}

async function loadEntries(preferredEntryId = "", allowScopeFallback = true, preferredEntryDate = "") {
  state.entryOffset = 0;
  state.entryHasMore = false;
  const filter = document.getElementById("filter").value;
  const explicitScope = explicitEntryScopeActive();
  const loadAll = explicitScope || state.entryLoadAll;
  const entries = [];
  const seenEntryIds = new Set();
  let payload = {entries: [], has_more: false};
  let offset = 0;
  do {
    const params = new URLSearchParams();
    params.set("status", filter);
    params.set("limit", String(state.entryLimit));
    params.set("offset", String(offset));
    for (const entryId of activeEntryIds()) params.append("entry_id", entryId);
    for (const entryDate of activeEntryDates()) params.append("entry_date", entryDate);
    payload = await fetchJson(`/api/entries?${params.toString()}`);
    const pageEntries = (payload.entries || []).filter(entry => !state.suppressedCommittedEntryIds.has(entry.entry_id));
    for (const entry of pageEntries) {
      if (seenEntryIds.has(entry.entry_id)) continue;
      seenEntryIds.add(entry.entry_id);
      entries.push(entry);
    }
    offset += (payload.entries || []).length || state.entryLimit;
    if (loadAll) {
      state.entries = entries;
      state.entryOffset = offset;
      state.entryHasMore = Boolean(payload.has_more);
      renderEntries();
      renderEntryPaging();
      renderBatchSummary();
    }
  } while (loadAll && payload.has_more);
  if (explicitScope) {
    entries.sort((left, right) => String(left.entry_date || "").localeCompare(String(right.entry_date || "")) || String(left.entry_id || "").localeCompare(String(right.entry_id || "")));
  }
  state.entries = entries;
  state.entryOffset = loadAll ? state.entries.length : offset;
  state.entryHasMore = loadAll ? false : Boolean(payload.has_more);
  renderEntries();
  renderEntryPaging();
  renderBatchSummary();
  if (!state.entries.length) {
    if (allowScopeFallback && (state.urlEntryIds.length || state.urlEntryDates.length)) {
      const scopedEntryDate = state.urlEntryDates[0] || "";
      clearUrlEntryScope(filter);
      return loadEntries("", false, scopedEntryDate);
    }
    state.selectedEntryId = null;
    renderEmpty();
    return;
  }
  const requestedEntryId = preferredEntryId || state.selectedEntryId;
	  if (state.entries.some(entry => entry.entry_id === requestedEntryId)) {
    state.selectedEntryId = requestedEntryId;
  } else if (preferredEntryDate) {
    const laterEntry = state.entries.find(entry => entry.entry_date > preferredEntryDate);
    const earlierEntries = state.entries.filter(entry => entry.entry_date < preferredEntryDate);
    const adjacentEntry = laterEntry || earlierEntries[earlierEntries.length - 1];
    state.selectedEntryId = adjacentEntry ? adjacentEntry.entry_id : state.entries[0].entry_id;
  } else if (!state.entries.some(entry => entry.entry_id === state.selectedEntryId)) {
    state.selectedEntryId = state.entries[0].entry_id;
  }
  await loadEntry(state.selectedEntryId);
}

function replaceEntryDateScopeUrl(dateTexts, status = "all") {
  const params = new URLSearchParams(window.location.search);
  params.delete("entry_id");
  params.delete("entry_date");
  params.delete("batch");
  params.delete("load_all");
  params.set("status", status);
  for (const dateText of dateTexts) params.append("entry_date", dateText);
  window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
}

function setEntryLoadAllUrl(enabled) {
  state.entryLoadAll = Boolean(enabled);
  const params = new URLSearchParams(window.location.search);
  params.set("status", document.getElementById("filter").value);
  const batchId = document.getElementById("batchFilter").value;
  if (batchId) {
    params.set("batch", batchId);
  } else {
    params.delete("batch");
  }
  if (state.entryLoadAll) {
    params.set("load_all", "1");
  } else {
    params.delete("load_all");
  }
  window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
}

function syncEntryDateJumpControl(message = "") {
  const startInput = document.getElementById("entryDateJump");
  const endInput = document.getElementById("entryDateJumpEnd");
  const status = document.getElementById("entryDateJumpStatus");
  if (startInput) startInput.value = state.urlEntryDates[0] || "";
  if (endInput) endInput.value = state.urlEntryDates.length > 1 ? state.urlEntryDates[state.urlEntryDates.length - 1] : "";
  if (status) status.textContent = message;
}

function entryDateScopeLabel(dateTexts) {
  if (!dateTexts.length) return "";
  const startDate = dateTexts[0];
  const endDate = dateTexts[dateTexts.length - 1];
  return startDate === endDate ? startDate : `${startDate} to ${endDate}`;
}

function isValidEntryDateText(dateText) {
  const match = String(dateText || "").match(/^(\\d{4})-(\\d{2})-(\\d{2})$/);
  if (!match) return false;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return date.toISOString().slice(0, 10) === dateText;
}

async function jumpToEntryDate() {
  const startInput = document.getElementById("entryDateJump");
  const endInput = document.getElementById("entryDateJumpEnd");
  const targetStatus = document.getElementById("entryDateJumpStatus");
  const startDate = String(startInput?.value || "").trim();
  const endDate = String(endInput?.value || "").trim() || startDate;
  if (!isValidEntryDateText(startDate) || !isValidEntryDateText(endDate)) {
    targetStatus.textContent = "Enter dates as yyyy-mm-dd.";
    return;
  }
  if (endDate < startDate) {
    targetStatus.textContent = "End date must be on or after start date.";
    return;
  }
  const dateTexts = dateRangeValues(startDate, endDate);
  if (!dateTexts.length || dateTexts[dateTexts.length - 1] !== endDate) {
    targetStatus.textContent = "Use a date range of 367 days or less.";
    return;
  }
  document.getElementById("filter").value = "all";
  document.getElementById("batchFilter").value = "";
  state.urlEntryIds = [];
  state.urlEntryDates = dateTexts;
  state.batchEntryIds = [];
  state.batchEntryDates = [];
  state.selectedEntryIds.clear();
  state.lastCheckedIndex = null;
  state.initialBatchId = "";
  replaceEntryDateScopeUrl(dateTexts);
  const label = entryDateScopeLabel(dateTexts);
  syncEntryDateJumpControl(`Opening ${label}.`);
  await loadEntries("", false, startDate);
  syncEntryDateJumpControl(state.entries.length ? `Showing ${state.entries.length} entries for ${label}.` : `No entries found for ${label}.`);
}

function entryPageParams(offset) {
  const filter = document.getElementById("filter").value;
  const params = new URLSearchParams();
  params.set("status", filter);
  params.set("limit", String(state.entryLimit));
  params.set("offset", String(offset));
  for (const entryId of activeEntryIds()) params.append("entry_id", entryId);
  for (const entryDate of activeEntryDates()) params.append("entry_date", entryDate);
  return params;
}

function appendEntryPage(payload) {
  state.entries = state.entries.concat((payload.entries || []).filter(entry => !state.suppressedCommittedEntryIds.has(entry.entry_id)));
  state.entryOffset += (payload.entries || []).length || state.entryLimit;
  state.entryHasMore = Boolean(payload.has_more);
}

async function loadMoreEntries(loadAll = false) {
  if (!state.entryHasMore || state.entryLoadingMore) return;
  state.entryLoadingMore = true;
  state.entryLoadingAll = Boolean(loadAll);
  renderEntryPaging();
  try {
    do {
      const payload = await fetchJson(`/api/entries?${entryPageParams(state.entryOffset).toString()}`);
      appendEntryPage(payload);
      if (loadAll) {
        renderEntries();
        renderEntryPaging();
        renderBatchSummary();
      }
    } while (loadAll && state.entryHasMore);
    renderEntries();
  } finally {
    state.entryLoadingMore = false;
    state.entryLoadingAll = false;
    renderEntryPaging();
  }
}

async function maybeLoadMoreEntriesNearEnd() {
  const index = currentEntryIndex();
  if (
    index < 0
    || index < state.entries.length - 2
    || !state.entryHasMore
    || state.entryLoadingMore
  ) {
    return;
  }
  await loadMoreEntries();
}

function renderEntries() {
  const list = document.getElementById("entryList");
  list.innerHTML = "";
	  state.entries.forEach((entry, index) => {
		    const item = document.createElement("div");
		    item.className = `entry-item ${entry.entry_id === state.selectedEntryId ? "active" : ""}`;
		    item.dataset.entryId = entry.entry_id;
		    const thumb = entry.source_token
		      ? `<img class="entry-thumb" src="/image/${encodeURIComponent(entry.source_token)}?max=96" loading="lazy" decoding="async" alt="">`
		      : `<div class="entry-thumb"></div>`;
	    const hasPendingLink = entryHasPendingCommit(entry);
	    const people = peopleScript(entry.people_names);
		    item.innerHTML = `
		      <input class="entry-check" type="checkbox" data-index="${index}" data-entry-id="${escapeHtml(entry.entry_id)}" ${state.selectedEntryIds.has(entry.entry_id) ? "checked" : ""}>
		      <button class="entry-thumb-button" type="button" aria-label="Open ${escapeHtml(entry.entry_date)} target photo">${thumb}</button>
		      <button class="entry-button" type="button" aria-label="Open ${escapeHtml(entry.entry_date)}">
	        <div class="entry-row">
          <span class="entry-date">${escapeHtml(entry.entry_date)}</span>
          <span class="badge ${entry.status}">${escapeHtml(statusLabel(entry.status))}</span>
	        </div>
	        <div class="summary">${entry.candidate_count} candidates</div>
	        ${people}
	      </button>
	      <button class="action-button primary entry-commit" type="button" data-action="commit-entry" title="Commit this target's linked original to the database" ${hasPendingLink ? "" : 'disabled aria-hidden="true" tabindex="-1"'}>Commit</button>
	    `;
    const checkbox = item.querySelector(".entry-check");
    checkbox.onchange = event => toggleEntrySelection(entry.entry_id, index, event.shiftKey, checkbox.checked);
	    checkbox.onclick = event => event.stopPropagation();
	    const thumbButton = item.querySelector(".entry-thumb-button");
	    thumbButton.onclick = () => loadEntry(entry.entry_id);
	    const button = item.querySelector(".entry-button");
	    button.onclick = () => loadEntry(entry.entry_id);
    const commitButton = item.querySelector('[data-action="commit-entry"]');
    if (commitButton) {
      commitButton.onclick = event => {
        event.stopPropagation();
        commitEntryDecision(entry.entry_id, event.currentTarget);
      };
    }
	    list.appendChild(item);
	  });
  renderSelectionSummary();
  renderEntryPaging();
  updateNavigationState();
	}

	function entryListItem(entryId) {
	  return [...document.querySelectorAll(".entry-item")].find(item => item.dataset.entryId === String(entryId)) || null;
	}

	function entryPendingCommitCount(entryOrCount) {
	  if (typeof entryOrCount === "number") return Number(entryOrCount || 0);
	  if (!entryOrCount) return 0;
	  if (Object.prototype.hasOwnProperty.call(entryOrCount, "pending_commit_count")) {
	    return Number(entryOrCount.pending_commit_count || 0);
	  }
	  if (Object.prototype.hasOwnProperty.call(entryOrCount, "commit_ready")) {
	    return entryOrCount.commit_ready ? 1 : 0;
	  }
	  return Number(entryOrCount.selected_count || 0);
	}

	function entryHasPendingCommit(entryOrCount) {
	  return entryPendingCommitCount(entryOrCount) > 0;
	}

	function setEntryCommitButtonState(button, entryOrCount) {
	  if (!button) return;
	  const hasPendingLink = entryHasPendingCommit(entryOrCount);
	  button.disabled = !hasPendingLink;
	  button.setAttribute("aria-hidden", hasPendingLink ? "false" : "true");
	  button.tabIndex = hasPendingLink ? 0 : -1;
	}

	function applyEntryListState(entry) {
	  const item = entryListItem(entry?.entry_id);
	  if (!item) return;
	  item.className = `entry-item ${entry.entry_id === state.selectedEntryId ? "active" : ""}`;
	  const checkbox = item.querySelector(".entry-check");
	  if (checkbox) checkbox.checked = state.selectedEntryIds.has(entry.entry_id);
	  const badge = item.querySelector(".badge");
	  if (badge) {
	    badge.className = `badge ${entry.status}`;
	    badge.textContent = statusLabel(entry.status);
	  }
	  const summary = item.querySelector(".summary");
	  if (summary) summary.textContent = `${entry.candidate_count} candidates`;
	  setEntryCommitButtonState(item.querySelector('[data-action="commit-entry"]'), entry);
	}

function renderEntryPaging() {
  const panel = document.getElementById("entryPaging");
  const summary = document.getElementById("entryPagingSummary");
  const button = document.getElementById("loadMoreEntries");
  const loadAllButton = document.getElementById("loadAllEntries");
  if (!panel || !summary || !button || !loadAllButton) return;
  panel.hidden = !state.entries.length && !state.entryHasMore;
  summary.textContent = state.entryHasMore
    ? `${state.entries.length} matching entries loaded. More entries available.`
    : `All ${state.entries.length} matching entries loaded.`;
  button.hidden = !state.entryHasMore;
  loadAllButton.hidden = !state.entryHasMore;
  button.disabled = state.entryLoadingMore;
  loadAllButton.disabled = state.entryLoadingMore;
  button.textContent = state.entryLoadingMore && !state.entryLoadingAll ? "Loading..." : "Load more";
  loadAllButton.textContent = state.entryLoadingAll ? "Loading..." : "Load all";
}

async function loadEntry(entryId) {
  const requestId = ++state.entryRequestId;
  const entryChanged = state.selectedEntryId !== entryId;
  state.selectedEntryId = entryId;
  renderEntries();
  if (entryChanged) {
    scheduleCandidateScrollReset();
    resetCandidateTimeFilters();
  }
  maybeLoadMoreEntriesNearEnd().catch(error => {
    document.getElementById("entryPagingSummary").textContent = error.message;
  });
  const cached = state.entryDetailCache.get(entryId);
  if (cached) {
    state.currentEntry = cached;
    renderEntryDetail();
    if (entryChanged) scheduleCandidateScrollReset();
  } else {
    document.getElementById("candidateGrid").innerHTML = `<div class="empty">Loading candidates...</div>`;
  }
  const detail = await fetchEntryDetail(entryId, true);
  if (requestId !== state.entryRequestId) return;
  state.currentEntry = detail;
  syncEntrySummaryFromDetail(detail);
  renderEntries();
  renderEntryDetail();
  if (entryChanged) scheduleCandidateScrollReset();
  preloadNextEntry();
}

function syncEntrySummaryFromDetail(detail) {
  const entrySummary = state.entries.find(entry => entry.entry_id === detail.entry_id);
  if (!entrySummary) return;
  const candidates = Array.isArray(detail?.candidates) ? detail.candidates : [];
  entrySummary.candidate_count = candidateScopeCandidates(candidates).length;
  entrySummary.status = detail.status;
  entrySummary.selected_count = detail.selected_count;
  entrySummary.pending_commit_count = detail.pending_commit_count;
  entrySummary.commit_ready = detail.commit_ready;
  entrySummary.accepted_count = detail.accepted_count;
  entrySummary.associated_count = detail.associated_count;
}

function scheduleCandidateScrollReset() {
  resetCandidateScroll();
  window.requestAnimationFrame(() => {
    resetCandidateScroll();
    window.setTimeout(resetCandidateScroll, 0);
  });
}

function resetCandidateScroll() {
  const workspace = document.querySelector(".workspace");
  if (workspace) workspace.scrollTo({top: 0, left: 0});
  const candidatePane = document.querySelector(".candidate-pane");
  if (candidatePane) candidatePane.scrollTo({top: 0, left: 0});
  window.scrollTo(0, 0);
}

function capturePickerScroll() {
  const workspace = document.querySelector(".workspace");
  const candidatePane = document.querySelector(".candidate-pane");
  return {
    windowX: window.scrollX,
    windowY: window.scrollY,
    workspaceLeft: workspace ? workspace.scrollLeft : 0,
    workspaceTop: workspace ? workspace.scrollTop : 0,
    candidatePaneLeft: candidatePane ? candidatePane.scrollLeft : 0,
    candidatePaneTop: candidatePane ? candidatePane.scrollTop : 0
  };
}

function restorePickerScroll(snapshot) {
  if (!snapshot) return;
  const workspace = document.querySelector(".workspace");
  const candidatePane = document.querySelector(".candidate-pane");
  if (workspace) workspace.scrollTo({left: snapshot.workspaceLeft, top: snapshot.workspaceTop});
  if (candidatePane) candidatePane.scrollTo({left: snapshot.candidatePaneLeft, top: snapshot.candidatePaneTop});
  window.scrollTo(snapshot.windowX, snapshot.windowY);
}

async function fetchEntryDetail(entryId, forceRefresh = false) {
  const includeDatabase = Boolean(state.urlEntryIds.length || state.urlEntryDates.length);
  const cacheKey = includeDatabase ? `${entryId}::database` : entryId;
  if (!forceRefresh && state.entryDetailCache.has(cacheKey)) return state.entryDetailCache.get(cacheKey);
  if (state.entryDetailRequests.has(cacheKey)) return state.entryDetailRequests.get(cacheKey);
  const query = includeDatabase ? "?include_database=1" : "";
  const request = fetchJson(`/api/entry/${encodeURIComponent(entryId)}${query}`);
  state.entryDetailRequests.set(cacheKey, request);
  try {
    const detail = await request;
    state.entryDetailCache.set(cacheKey, detail);
    return detail;
  } finally {
    state.entryDetailRequests.delete(cacheKey);
  }
}

function preloadNextEntry() {
  const index = currentEntryIndex();
  const next = index >= 0 ? state.entries[index + 1] : null;
  const keep = new Set([state.selectedEntryId, next?.entry_id].filter(Boolean));
  for (const entryId of state.entryDetailCache.keys()) {
    if (!keep.has(entryId)) state.entryDetailCache.delete(entryId);
  }
  if (!next || state.entryDetailCache.has(next.entry_id)) return;
  fetchEntryDetail(next.entry_id).catch(error => {
    document.getElementById("crawlStatus").textContent = `Next entry preload unavailable: ${error.message}`;
  });
}

function renderEmpty() {
  const filter = document.getElementById("filter").value;
  state.currentEntry = null;
  document.getElementById("entryHeading").textContent = "No entry selected";
  document.getElementById("entrySubhead").textContent = "";
  document.getElementById("entryPosition").textContent = "";
  document.getElementById("sourceImage").removeAttribute("src");
  updateSourceDragSurface(null);
  document.getElementById("sourceMeta").textContent = "";
  document.getElementById("candidateSummary").textContent = "";
  renderCandidateEvidenceFilter([]);
  renderCandidateFolderFilter([]);
  const message = filter === "needs_review"
    ? "No candidate rows need visual review. Check Needs action or Needs broader search for entries that still need more candidates."
    : "No rows";
  document.getElementById("candidateGrid").innerHTML = `<div class="empty">${message}</div>`;
  updateManualDateSearchControls(null);
  updatePhotoDropTarget();
  updateNavigationState();
}

function renderEntryDetail() {
  const entry = state.currentEntry;
  state.candidateRenderLimit = 40;
  if (state.candidateRenderObserver) state.candidateRenderObserver.disconnect();
  updatePhotoDropTarget();
  updateDateRangeButtons(entry);
  updateManualDateSearchControls(entry);
  document.getElementById("entryHeading").textContent = entry.entry_date;
  document.getElementById("entryPosition").textContent = currentEntryPositionLabel();
  document.getElementById("entrySubhead").textContent =
    `${entry.candidate_count} candidates · ${statusLabel(entry.status)}`;
  updateNavigationState();
  const sourceImage = document.getElementById("sourceImage");
  if (entry.source_token) {
    sourceImage.src = `/image/${entry.source_token}?max=1280`;
    updateSourceDragSurface(entry);
  } else {
    sourceImage.removeAttribute("src");
    updateSourceDragSurface(null);
  }
  const priorMatcher = formatPriorMatcher(entry);
  const sourceFacts = formatPhotoFacts(entry.source_file_type, entry.source_byte_size, entry.source_dimensions);
  const sourceParts = [escapeHtml(entry.entry_id)];
  if (entry.status === "fallback") sourceParts.push("Project365 fallback: no external original linked.");
  if (sourceFacts) sourceParts.push(escapeHtml(sourceFacts));
  if (entry.people_names?.length) sourceParts.push(peopleScript(entry.people_names));
  if (priorMatcher) sourceParts.push(priorMatcher);
  document.getElementById("sourceMeta").innerHTML = sourceParts.join("<br>");
  const grid = document.getElementById("candidateGrid");
  grid.innerHTML = "";
  const scopedCandidates = candidateScopeCandidates(entry.candidates);
  renderCandidateFolderFilter(scopedCandidates);
  renderCandidateEvidenceFilter(scopedCandidates);
  defaultVisualControlsForEntry(entry);
  if (!entry.candidates.length) {
    const emptyMessage = entry.manual_search_message
      || "No candidates are currently shown. Expand the date range above to search the existing index, or add a folder for a targeted search.";
    grid.innerHTML = `<div class="empty">${escapeHtml(emptyMessage)}</div>`;
    document.getElementById("candidateSummary").textContent = entry.manual_search_message || "No candidates currently shown.";
    return;
  }
  renderCandidateGrid();
}

function renderCandidateGrid() {
  const entry = state.currentEntry;
  const grid = document.getElementById("candidateGrid");
  const candidates = Array.isArray(entry?.candidates) ? entry.candidates : [];
  if (!entry || !candidates.length) {
    return;
  }
  const scopedCandidates = candidateScopeCandidates(candidates);
  renderCandidateFolderFilter(scopedCandidates);
  renderCandidateEvidenceFilter(scopedCandidates);
  const visibleCandidates = filteredAndSortedCandidates(candidates);
  const renderedCandidates = visibleCandidates.slice(0, state.candidateRenderLimit);
  const folderCount = candidateFolderGroups(scopedCandidates).length;
  const folderFilter = document.getElementById("candidateFolderFilter").value;
  const folderText = folderFilter
    ? " · folder filtered"
    : folderCount
      ? ` · ${folderCount} folders`
      : "";
  const locationText = document.getElementById("candidateLocationOnly").checked ? " · location only" : "";
  const timeText = activeCandidateTimeFilterLabels().length
    ? ` · ${activeCandidateTimeFilterLabels().join(" + ")}`
    : "";
  const scopeText = currentIndexSearchWholeIndex() || scopedCandidates.length === candidates.length
    ? ""
    : ` · ${candidates.length} saved`;
  document.getElementById("candidateSummary").textContent =
    `Showing ${visibleCandidates.length} of ${scopedCandidates.length} candidates${folderText}${locationText}${timeText}${scopeText}`;
  if (state.candidateImageObserver) state.candidateImageObserver.disconnect();
  grid.innerHTML = "";
  if (!visibleCandidates.length) {
    const emptyMessage = currentIndexSearchWholeIndex()
      ? "No candidates match the current filters."
      : "No indexed filename-prefix candidates match the current date window.";
    grid.innerHTML = `<div class="empty">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  let lastCandidateGroup = "";
  const hasMixedSourceGroups = visibleCandidates.some(candidateProjectOriginalsSource) && visibleCandidates.some(candidate => !candidateProjectOriginalsSource(candidate));
  for (const candidate of renderedCandidates) {
    const candidateGroup = candidateProjectOriginalsSource(candidate) ? "project-originals" : "other";
    if (hasMixedSourceGroups && candidateGroup !== lastCandidateGroup) {
      const heading = document.createElement("div");
      heading.className = "candidate-group-heading";
      heading.textContent = candidateGroup === "project-originals"
        ? "Original Photos matching Project365 Entries"
        : "Other candidates";
      grid.appendChild(heading);
      lastCandidateGroup = candidateGroup;
    }
    const card = document.createElement("article");
    card.className = `candidate-card ${candidate.selected ? "selected" : ""} ${candidateProjectOriginalsSource(candidate) ? "project-originals-source" : ""}`;
    card.dataset.candidateToken = candidate.token;
    const candidateImageUrl = `/image/${candidate.token}?max=640`;
    const generatedAddition = candidateIsGeneratedAddition(candidate);
    const showFlagControl = !generatedAddition;
    const showLinkControl = !generatedAddition || !candidate.selected;
    const actionClass = generatedAddition && candidate.selected ? "main-only" : generatedAddition ? "compact" : "";
    card.innerHTML = `
      <button class="image-preview-trigger" type="button" onclick="openImagePreviewFromTrigger(this)" data-preview-url="${escapeHtml(candidateImageUrl)}" data-preview-title="${escapeHtml(candidate.filename || "Candidate")}" data-preview-path="${escapeHtml(candidate.path || "")}" aria-label="Open larger ${escapeHtml(candidate.filename || "candidate")} image">
        <img class="candidate-image" data-src="${escapeHtml(candidateImageUrl)}" decoding="async" alt="">
      </button>
      <div class="candidate-meta">
        <div class="candidate-filename-row">
          <strong>${escapeHtml(candidate.filename)}</strong>
          <svg data-candidate-location="${candidate.token}" class="location-indicator ${candidate.has_embedded_geolocation ? "has-location" : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="${candidate.has_embedded_geolocation ? "Embedded location metadata" : "No embedded location metadata"}">
            <circle cx="12" cy="12" r="10"></circle>
            <path d="M2 12h20M12 2a15.3 15.3 0 0 1 0 20M12 2a15.3 15.3 0 0 0 0 20"></path>
          </svg>
        </div>
        <span data-candidate-facts="${candidate.token}">${escapeHtml(formatPhotoFacts(fileTypeLabel(candidate.filename || candidate.path || "", candidate.mime_type), candidate.byte_size, candidate.dimensions))}</span><br>
        <span title="${escapeHtml(candidate.path || "")}">${escapeHtml(candidate.folder_label || candidateFolderLabel(candidate.path || ""))}</span><br>
        ${formatCandidateCardDetails(candidate)}
        ${formatAlignment(candidate)}
      </div>
      <div class="candidate-actions ${actionClass}">
        <button class="action-button primary" data-action="select">${escapeHtml(selectButtonLabel(candidate))}</button>
        ${showFlagControl ? `<button class="action-button ${candidate.associated ? "flagged" : ""}" data-action="flag" title="${escapeHtml(flagButtonTitle(candidate))}">${escapeHtml(flagButtonLabel(candidate))}</button>` : ""}
        ${showLinkControl ? `<button class="action-button ${candidateLinked(candidate) ? "linked" : ""}" data-action="link" aria-pressed="${candidateLinked(candidate) ? "true" : "false"}" title="${escapeHtml(linkButtonTitle(candidate))}" ${candidate.selected ? "disabled" : ""}>${escapeHtml(linkButtonLabel(candidate))}</button>` : ""}
        <div class="associated-date-panel" data-associated-panel hidden>
          <div class="associated-date-choices" data-associated-choices></div>
          <input type="date" data-associated-date aria-label="Associated date">
          <input type="time" step="1" data-associated-time aria-label="Associated time">
          <button class="action-button" data-action="save-flag">Save manual time</button>
        </div>
      </div>
    `;
    card.querySelector('[data-action="select"]').onclick = () => saveDecision(candidate, "use_external_original", card);
    card.querySelector('[data-action="link"]')?.addEventListener("click", () => linkAssociatedPhoto(candidate, card));
    card.querySelector('[data-action="flag"]')?.addEventListener("click", () => showAssociatedDatePanel(candidate, card));
    card.querySelector('[data-action="save-flag"]').onclick = () => saveAssociatedPhotoFlag(candidate, card);
    card.querySelector('[data-associated-date]').oninput = () => clearAssociatedDateChoiceSelection(card);
    card.querySelector('[data-associated-time]').oninput = () => clearAssociatedDateChoiceSelection(card);
    grid.appendChild(card);
  }
  if (renderedCandidates.length < visibleCandidates.length) {
    const sentinel = document.createElement("div");
    sentinel.className = "candidate-render-sentinel";
    sentinel.setAttribute("aria-hidden", "true");
    grid.appendChild(sentinel);
    observeCandidateRenderSentinel(sentinel);
  }
  observeCandidateImages(grid);
  fetchCandidateFacts(candidateFactPrefetchCandidates(visibleCandidates, renderedCandidates)).catch(error => {
    document.getElementById("crawlStatus").textContent = `Candidate details unavailable: ${error.message}`;
  });
}

function selectButtonLabel(candidate) {
  return candidate.selected ? "Main" : "Select";
}

function applySelectButtonState(card, candidate) {
  const button = card?.querySelector('[data-action="select"]');
  if (!button) return;
  button.textContent = selectButtonLabel(candidate);
  button.title = candidate.selected ? "This is the main image selected for this diary entry" : "";
}

function flagButtonLabel(candidate) {
  return candidate.associated ? "Date" : "Date";
}

function flagButtonTitle(candidate) {
  if (!candidate.associated) return "Set a custom date and link this as a supplemental attachment";
  const date = candidate.associated_entry_date || "saved date";
  const source = associatedDateSourceLabel(candidate.associated_date_source || "manual");
  return `Supplemental attachment saved for ${date} from ${source}. Click to change the date/time.`;
}

function applyFlagButtonState(button, candidate) {
  if (!button) return;
  button.classList.toggle("flagged", Boolean(candidate.associated));
  button.textContent = flagButtonLabel(candidate);
  button.title = flagButtonTitle(candidate);
}

function candidateLinked(candidate) {
  return Boolean(candidate.selected || candidate.associated);
}

function candidateIsGeneratedAddition(candidate) {
  const evidence = String(candidate.evidence || "");
  const filename = String(candidate.filename || candidate.path || "");
  return /manual_drop_copy/i.test(evidence) || /Project365 project file addition/i.test(filename);
}

function linkButtonLabel(candidate) {
  if (candidate.selected) return "Already main";
  return candidate.associated ? "Linked" : "Link";
}

function linkButtonTitle(candidate) {
  if (candidate.selected) return "Already selected as the main image for this diary entry";
  if (candidate.associated) return "This is linked as a supplemental attachment. Click to unlink it.";
  return "Link this as a supplemental attachment for this diary entry";
}

function applyLinkButtonState(card, candidate) {
  if (!card) return;
  card.classList.toggle("selected", candidateLinked(candidate));
  const button = card.querySelector('[data-action="link"]');
  if (!button) return;
  button.classList.toggle("linked", candidateLinked(candidate));
  button.setAttribute("aria-pressed", candidateLinked(candidate) ? "true" : "false");
  button.disabled = Boolean(candidate.selected);
  button.textContent = linkButtonLabel(candidate);
  button.title = linkButtonTitle(candidate);
}

function observeCandidateRenderSentinel(sentinel) {
  if (state.candidateRenderObserver) state.candidateRenderObserver.disconnect();
  state.candidateRenderObserver = new IntersectionObserver(entries => {
    if (!entries.some(entry => entry.isIntersecting)) return;
    state.candidateRenderObserver.disconnect();
    state.candidateRenderLimit += 40;
    renderCandidateGrid();
  }, {rootMargin: "600px"});
  state.candidateRenderObserver.observe(sentinel);
}

async function fetchCandidateFacts(candidates) {
  const pending = candidates.filter(candidate => !candidate.file_facts_loaded);
  if (!pending.length) return;
  let sortFactsChanged = false;
  for (let offset = 0; offset < pending.length; offset += 100) {
    const chunk = pending.slice(offset, offset + 100);
    const params = new URLSearchParams();
    for (const candidate of chunk) params.append("token", candidate.token);
    const payload = await fetchJson(`/api/candidate-facts?${params.toString()}`);
    for (const candidate of chunk) {
      const facts = payload.facts?.[candidate.token];
      if (!facts) continue;
      const previousDimensions = candidate.dimensions || "";
      candidate.dimensions = facts.dimensions || "";
      candidate.has_embedded_geolocation = Boolean(facts.has_embedded_geolocation);
      candidate.exists = Boolean(facts.exists);
      candidate.file_facts_loaded = true;
      if (candidate.dimensions !== previousDimensions) sortFactsChanged = true;
      const factsElement = document.querySelector(`[data-candidate-facts="${candidate.token}"]`);
      if (factsElement) {
        factsElement.textContent = formatPhotoFacts(
          fileTypeLabel(candidate.filename || candidate.path || "", candidate.mime_type),
          candidate.byte_size,
          candidate.dimensions
        );
      }
      const locationElement = document.querySelector(`[data-candidate-location="${candidate.token}"]`);
      if (locationElement) {
        locationElement.classList.toggle("has-location", candidate.has_embedded_geolocation);
        locationElement.setAttribute("aria-label", candidate.has_embedded_geolocation ? "Embedded location metadata" : "No embedded location metadata");
      }
    }
  }
  const sortMode = document.getElementById("candidateSort").value;
  if (sortFactsChanged && (sortMode === "capture_time" || sortMode === "dimensions" || sortMode === "visual")) {
    renderCandidateGrid();
  }
}

function candidateFactPrefetchCandidates(visibleCandidates, renderedCandidates) {
  const sortMode = document.getElementById("candidateSort").value;
  if (sortMode === "dimensions") return visibleCandidates;
  const candidates = new Map();
  for (const candidate of renderedCandidates) candidates.set(candidate.token, candidate);
  if (sortMode === "capture_time" || sortMode === "visual") {
    const renderedTimestampKeys = new Set(renderedCandidates.map(candidateTimestampGroupKey).filter(Boolean));
    if (renderedTimestampKeys.size) {
      for (const candidate of visibleCandidates) {
        if (renderedTimestampKeys.has(candidateTimestampGroupKey(candidate))) {
          candidates.set(candidate.token, candidate);
        }
      }
    }
  }
  return [...candidates.values()];
}

function observeCandidateImages(grid) {
  const images = [...grid.querySelectorAll("img[data-src]")];
  if (!("IntersectionObserver" in window)) {
    for (const image of images) image.src = image.dataset.src;
    return;
  }
  state.candidateImageObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const image = entry.target;
      image.src = image.dataset.src;
      image.removeAttribute("data-src");
      state.candidateImageObserver.unobserve(image);
    }
  }, {rootMargin: "500px"});
  for (const image of images) state.candidateImageObserver.observe(image);
}

function largeImageUrl(url) {
  const parts = String(url || "").split("#");
  const hash = parts.length > 1 ? `#${parts.slice(1).join("#")}` : "";
  const [base, queryText = ""] = parts[0].split("?");
  const params = new URLSearchParams(queryText);
  params.set("max", "2048");
  return `${base}?${params.toString()}${hash}`;
}

function openImagePreviewFromTrigger(trigger) {
  if (!trigger) return;
  openImagePreview(trigger.dataset.previewUrl || "", trigger.dataset.previewTitle || "Image preview", trigger.dataset.previewPath || "");
}

function openImagePreview(url, title, path) {
  const modal = document.getElementById("imagePreviewModal");
  const image = document.getElementById("imagePreviewImage");
  const caption = document.getElementById("imagePreviewCaption");
  const closeButton = modal?.querySelector(".image-preview-close");
  if (!modal || !image || !caption || !url) return;
  caption.textContent = path ? `${title} | ${path}` : title;
  image.alt = title || "Large image preview";
  image.src = largeImageUrl(url);
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
  closeButton?.focus();
}

function closeImagePreview() {
  const modal = document.getElementById("imagePreviewModal");
  const image = document.getElementById("imagePreviewImage");
  if (!modal || !image) return;
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
  image.removeAttribute("src");
}

function handleImagePreviewBackdrop(event) {
  if (event.target?.id === "imagePreviewModal") closeImagePreview();
}

function filteredAndSortedCandidates(candidates) {
  const text = document.getElementById("candidateFilter").value.trim().toLowerCase();
  const evidenceFilter = document.getElementById("candidateEvidenceFilter").value;
  const folderFilter = document.getElementById("candidateFolderFilter").value;
  const sortMode = document.getElementById("candidateSort").value;
  const filtered = candidateScopeCandidates(candidates).filter(candidate => candidateMatchesText(candidate, text) && candidateMatchesEvidence(candidate, evidenceFilter) && candidateMatchesFolder(candidate, folderFilter) && candidateMatchesLocation(candidate) && candidateMatchesActiveTimeFilters(candidate));
  prepareCandidateTimestampGroups(filtered, sortMode);
  return filtered.sort((left, right) => compareCandidates(left, right, sortMode));
}

function candidateScopeCandidates(candidates) {
  if (currentIndexSearchWholeIndex()) return candidates;
  return candidates.filter(candidate => candidateMatchesCurrentIndexDefaultScope(candidate));
}

function candidateMatchesCurrentIndexDefaultScope(candidate) {
  return candidateFilenameStartsWithCurrentScopeDate(candidate);
}

function candidateInProjectOriginalsFolder(candidate) {
  if (candidateProjectOriginalsSource(candidate)) return true;
  const root = normalizedCandidateScopePath(state.defaultPhotoIndexFolder);
  if (!root) return false;
  const folder = normalizedCandidateScopePath(candidate.folder_path || "");
  const path = normalizedCandidateScopePath(candidate.path || "");
  return folder === root || folder.startsWith(`${root}/`) || path.startsWith(`${root}/`);
}

function normalizedCandidateScopePath(value) {
  return String(value || "").trim().replace(/\\/+$/, "");
}

function candidateFilenameStartsWithCurrentScopeDate(candidate) {
  const filename = String(candidate.filename || candidate.path?.split("/").pop() || "").trim();
  if (!filename) return false;
  return currentFilenameDateScopeDates().some(dateText => filename.startsWith(dateText));
}

function currentFilenameDateScopeDates() {
  const entry = state.currentEntry;
  if (!entry?.entry_date) return [];
  if (state.filenameDateScopeEntryId === entry.entry_id && state.filenameDateScopeStart && state.filenameDateScopeEnd) {
    return dateRangeValues(state.filenameDateScopeStart, state.filenameDateScopeEnd);
  }
  const days = state.filenameDateScopeEntryId === entry.entry_id
    ? Number(state.filenameDateScopeDays || 0)
    : 0;
  return dateWindowValues(entry.entry_date, Number.isFinite(days) ? days : 0);
}

function dateWindowValues(dateText, days) {
  const values = [];
  for (let offset = -days; offset <= days; offset += 1) {
    const value = isoDateOffset(dateText, offset);
    if (value) values.push(value);
  }
  return values;
}

function dateRangeValues(startDate, endDate) {
  const values = [];
  let cursor = startDate;
  for (let count = 0; cursor && cursor <= endDate && count <= 366; count += 1) {
    values.push(cursor);
    cursor = isoDateOffset(cursor, 1);
  }
  return values;
}

function isoDateOffset(dateText, offsetDays) {
  const match = String(dateText || "").match(/^(\\d{4})-(\\d{2})-(\\d{2})$/);
  if (!match) return "";
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + offsetDays));
  return date.toISOString().slice(0, 10);
}

function candidateMatchesLocation(candidate) {
  return !document.getElementById("candidateLocationOnly").checked || Boolean(candidate.has_embedded_geolocation);
}

function activeCandidateTimeFilterLabels() {
  const labels = {
    morning: "4-8a",
    midday: "8-12p",
    "early-afternoon": "12-4p",
    "late-afternoon": "4-8p",
    "early-evening": "8-12a",
    "late-evening": "12-4a"
  };
  return [...state.activeCandidateTimeFilters].map(value => labels[value] || value);
}

function candidateMatchesActiveTimeFilters(candidate) {
  if (!state.activeCandidateTimeFilters.size) return true;
  const minutes = candidateTimeOfDayMinutes(candidate);
  if (minutes === null) return false;
  for (const filter of state.activeCandidateTimeFilters) {
    if (candidateTimeMatchesFilter(minutes, filter)) return true;
  }
  return false;
}

function candidateTimeOfDayMinutes(candidate) {
  const timestamp = candidateCaptureSortTime(candidate);
  const match = String(timestamp || "").match(/[T ](\\d{2}):(\\d{2})(?::\\d{2})?/);
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (!Number.isInteger(hour) || !Number.isInteger(minute) || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
    return null;
  }
  return hour * 60 + minute;
}

function candidateTimeMatchesFilter(minutes, filter) {
  if (filter === "morning") return minutes >= 4 * 60 && minutes < 8 * 60;
  if (filter === "midday") return minutes >= 8 * 60 && minutes < 12 * 60;
  if (filter === "early-afternoon") return minutes >= 12 * 60 && minutes < 16 * 60;
  if (filter === "late-afternoon") return minutes >= 16 * 60 && minutes < 20 * 60;
  if (filter === "early-evening") return minutes >= 20 * 60;
  if (filter === "late-evening") return minutes < 4 * 60;
  return false;
}

function toggleCandidateTimeFilter(button) {
  const value = button?.dataset.timeFilter || "";
  if (!value) return;
  if (state.activeCandidateTimeFilters.has(value)) {
    state.activeCandidateTimeFilters.delete(value);
  } else {
    state.activeCandidateTimeFilters.add(value);
  }
  updateCandidateTimeFilterButtons();
  state.candidateRenderLimit = 40;
  if (state.candidateRenderObserver) state.candidateRenderObserver.disconnect();
  renderCandidateGrid();
}

function resetCandidateTimeFilters() {
  if (!state.activeCandidateTimeFilters.size) return;
  state.activeCandidateTimeFilters.clear();
  updateCandidateTimeFilterButtons();
}

function updateCandidateTimeFilterButtons() {
  document.querySelectorAll("[data-time-filter]").forEach(button => {
    button.setAttribute("aria-pressed", state.activeCandidateTimeFilters.has(button.dataset.timeFilter) ? "true" : "false");
  });
}

function candidateFolderLabel(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  if (parts.length <= 1) return "";
  return parts.slice(Math.max(0, parts.length - 3), parts.length - 1).join(" / ");
}

function candidateFolderValue(candidate) {
  return String(candidate.folder_path || "").trim() || candidateFolderLabel(candidate.path || "");
}

function candidateFolderGroups(candidates) {
  const groups = new Map();
  for (const candidate of candidates || []) {
    const value = candidateFolderValue(candidate);
    if (!value) continue;
    const label = candidate.folder_label || candidateFolderLabel(candidate.path || "") || value;
    const group = groups.get(value) || {value, label, count: 0};
    group.count += 1;
    groups.set(value, group);
  }
  return [...groups.values()].sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

function renderCandidateFolderFilter(candidates) {
  const select = document.getElementById("candidateFolderFilter");
  const previous = select.value;
  const groups = candidateFolderGroups(candidates);
  select.innerHTML = "";
  select.appendChild(new Option("All folders", ""));
  for (const group of groups) {
    select.appendChild(new Option(`${group.label} · ${group.count}`, group.value));
  }
  select.value = groups.some(group => group.value === previous) ? previous : "";
  select.disabled = groups.length <= 1;
}

function renderCandidateEvidenceFilter(candidates) {
  const select = document.getElementById("candidateEvidenceFilter");
  const currentEntryId = state.currentEntry?.entry_id || "";
  const previous = select.dataset.entryId === currentEntryId ? select.value : "";
  select.dataset.entryId = currentEntryId;
  const countBase = candidateEvidenceCountBase(candidates);
  select.innerHTML = "";
  for (const filter of CANDIDATE_EVIDENCE_FILTERS) {
    const count = filter.value === "all"
      ? countBase.length
      : countBase.filter(candidate => candidateMatchesEvidence(candidate, filter.value)).length;
    const option = new Option(`${filter.label} · ${count}`, filter.value);
    option.disabled = filter.value !== "all" && count === 0;
    select.appendChild(option);
  }
  const previousOption = [...select.options].find(option => option.value === previous && !option.disabled);
  if (previousOption) {
    select.value = previous;
  } else {
    select.value = "all";
  }
}

function defaultVisualControlsForEntry(entry) {
  const sort = document.getElementById("candidateSort");
  if (sort.dataset.entryId === entry.entry_id) return;
  sort.dataset.entryId = entry.entry_id;
  if (shouldDefaultLikely(entry.candidates)) sort.value = "visual";
}

function updateDateRangeButtons(entry) {
  const usedDays = usedDateRangeDays(entry);
  const defaultButton = document.getElementById("defaultDateRange");
  const defaultUsed = defaultDateRangeUsed(entry);
  defaultButton.classList.toggle("used-range", defaultUsed);
  defaultButton.setAttribute("aria-pressed", defaultUsed ? "true" : "false");
  document.querySelectorAll("[data-range-days]").forEach(button => {
    const used = usedDays.has(Number(button.dataset.rangeDays));
    button.classList.toggle("used-range", used);
    button.setAttribute("aria-pressed", used ? "true" : "false");
  });
}

function updateManualDateSearchControls(entry) {
  const startInput = document.getElementById("indexDateStart");
  const endInput = document.getElementById("indexDateEnd");
  const button = document.getElementById("searchIndexDateRange");
  const defaultButton = document.getElementById("defaultDateRange");
  const wholeIndexCheckbox = document.getElementById("indexSearchWholeIndex");
  const modifiedDateCheckbox = document.getElementById("indexSearchModifiedDate");
  const disabled = !entry;
  startInput.disabled = disabled;
  endInput.disabled = disabled;
  button.disabled = disabled;
  defaultButton.disabled = disabled;
  wholeIndexCheckbox.disabled = disabled;
  modifiedDateCheckbox.disabled = disabled;
  if (entry && startInput.dataset.entryId !== entry.entry_id) {
    wholeIndexCheckbox.checked = false;
    wholeIndexCheckbox.dataset.userChanged = "";
    modifiedDateCheckbox.checked = false;
    state.filenameDateScopeEntryId = entry.entry_id;
    state.filenameDateScopeDays = 0;
    state.filenameDateScopeStart = "";
    state.filenameDateScopeEnd = "";
    startInput.dataset.entryId = entry.entry_id;
    endInput.dataset.entryId = entry.entry_id;
    startInput.value = entry.entry_date;
    endInput.value = entry.entry_date;
  } else if (!entry) {
    state.filenameDateScopeEntryId = "";
    state.filenameDateScopeDays = 0;
    state.filenameDateScopeStart = "";
    state.filenameDateScopeEnd = "";
  }
  updateManualDateSearchScope();
}

function updateManualDateSearchScope() {
  const checkbox = document.getElementById("indexSearchWholeIndex");
  const modifiedDateCheckbox = document.getElementById("indexSearchModifiedDate");
  const target = document.getElementById("indexSearchScope");
  if (!checkbox || !target) return;
  if (checkbox.dataset.userChanged !== "1") {
    checkbox.checked = false;
  }
  const wholeIndex = currentIndexSearchWholeIndex();
  if (modifiedDateCheckbox) {
    if (!wholeIndex) modifiedDateCheckbox.checked = false;
    modifiedDateCheckbox.disabled = !state.currentEntry || !wholeIndex;
  }
  const scope = wholeIndex
    ? `Whole photo index, ${currentIndexDateSourceLabel()}`
    : state.defaultPhotoIndexFolder
      ? `Project originals folder first: ${state.defaultPhotoIndexFolder}, plus filename prefixes across the index`
      : "Filename prefixes across the index";
  target.textContent = scope;
  updateDateRangeButtons(state.currentEntry);
}

function usedDateRangeDays(entry) {
  const includeUnscopedRangeState = currentIndexSearchWholeIndex();
  const days = includeUnscopedRangeState
    ? new Set((entry?.used_range_days || []).map(Number).filter(Number.isFinite))
    : new Set();
  for (const candidate of entry?.candidates || []) {
    const evidence = String(candidate.evidence || "");
    if (!rangeEvidenceMatchesCurrentIndexScope(evidence)) continue;
    for (const match of evidence.matchAll(/(?:manual_range|auto_range)_(\\d+)_days/g)) {
      days.add(Number(match[1]));
    }
  }
  return days;
}

function defaultDateRangeUsed(entry) {
  for (const candidate of entry?.candidates || []) {
    const evidence = String(candidate.evidence || "");
    if (!/manual_default_search/i.test(evidence)) continue;
    if (defaultEvidenceMatchesCurrentIndexScope(evidence)) return true;
  }
  return false;
}

function defaultEvidenceMatchesCurrentIndexScope(evidence) {
  const wholeIndex = currentIndexSearchWholeIndex();
  const hasWholeIndexScope = /manual_default_scope_whole_index/i.test(evidence);
  const hasFolderScope = /manual_default_scope_folder/i.test(evidence);
  const hasProjectOriginalsScope = /manual_default_scope_project_originals/i.test(evidence);
  const hasFilenameIndexScope = /manual_default_scope_filename_index/i.test(evidence);
  const hasFilenameOnlySource = /manual_default_date_source_filename_only/i.test(evidence);
  const hasModifiedDateSource = /manual_default_date_source_modified_date/i.test(evidence);
  if (wholeIndex) {
    if (hasWholeIndexScope) return !hasFilenameOnlySource && hasModifiedDateSource === currentIndexIncludeModifiedDates();
    return !hasFolderScope && !hasProjectOriginalsScope && !hasFilenameIndexScope && !currentIndexIncludeModifiedDates();
  }
  return !hasWholeIndexScope && hasFilenameOnlySource && !hasModifiedDateSource;
}

function rangeEvidenceMatchesCurrentIndexScope(evidence) {
  const wholeIndex = currentIndexSearchWholeIndex();
  const hasWholeIndexScope = /(?:manual_range|auto_range)_scope_whole_index/i.test(evidence);
  const hasFolderScope = /(?:manual_range|auto_range)_scope_folder/i.test(evidence);
  const hasProjectOriginalsScope = /(?:manual_range|auto_range)_scope_project_originals/i.test(evidence);
  const hasFilenameIndexScope = /(?:manual_range|auto_range)_scope_filename_index/i.test(evidence);
  const hasFilenameOnlySource = /manual_range_date_source_filename_only/i.test(evidence);
  const hasModifiedDateSource = /manual_range_date_source_modified_date/i.test(evidence);
  if (wholeIndex) {
    if (hasWholeIndexScope) return !hasFilenameOnlySource && hasModifiedDateSource === currentIndexIncludeModifiedDates();
    return !hasFolderScope && !hasProjectOriginalsScope && !hasFilenameIndexScope && !currentIndexIncludeModifiedDates();
  }
  return !hasWholeIndexScope && hasFilenameOnlySource && !hasModifiedDateSource;
}

function currentIndexSearchWholeIndex() {
  const checkbox = document.getElementById("indexSearchWholeIndex");
  return checkbox ? checkbox.checked : false;
}

function currentIndexIncludeModifiedDates() {
  const checkbox = document.getElementById("indexSearchModifiedDate");
  return currentIndexSearchWholeIndex() && Boolean(checkbox?.checked);
}

function currentIndexRangeDateSourceLabel() {
  if (!currentIndexSearchWholeIndex()) return "filename prefixes";
  return currentIndexIncludeModifiedDates()
    ? "file name, media, filesystem, and modified dates"
    : "file name, media, and filesystem dates";
}

function currentIndexDateSourceLabel() {
  return currentIndexRangeDateSourceLabel();
}

function currentIndexScopeLabel() {
  const scope = currentIndexSearchWholeIndex()
    ? "whole photo index"
    : "project originals folder first plus filename prefixes across the index";
  return `${scope}, ${currentIndexDateSourceLabel()}`;
}

function ensureIndexSearchScopeAvailable() {
  return true;
}

function shouldDefaultLikely(candidates) {
  return (candidates || []).length > 20
    && (candidates || []).some(candidate => candidateVisualRank(candidate) > 0 && candidateVisualRank(candidate) <= 20);
}

function candidateEvidenceCountBase(candidates) {
  const text = document.getElementById("candidateFilter").value.trim().toLowerCase();
  const folderFilter = document.getElementById("candidateFolderFilter").value;
  return candidates.filter(candidate => candidateMatchesText(candidate, text) && candidateMatchesFolder(candidate, folderFilter) && candidateMatchesLocation(candidate) && candidateMatchesActiveTimeFilters(candidate));
}

function fileTypeLabel(path, mimeType = "") {
  const cleanPath = String(path || "").split("?")[0].split("#")[0];
  const filename = cleanPath.split("/").filter(Boolean).pop() || cleanPath;
  const extension = filename.includes(".") ? filename.split(".").pop().trim().toUpperCase() : "";
  if (extension) return extension === "JPEG" ? "JPG" : extension;
  const mimePart = String(mimeType || "").split("/").pop().trim().toUpperCase();
  return mimePart === "JPEG" ? "JPG" : mimePart;
}

function formatPhotoFacts(fileType, byteSize, dimensions = "") {
  const parts = [];
  if (String(fileType || "").trim()) parts.push(String(fileType).trim().toUpperCase());
  const size = formatFileSize(byteSize);
  if (size) parts.push(size);
  if (String(dimensions || "").trim()) parts.push(String(dimensions).trim());
  return parts.join(" · ");
}

function formatCandidateCardDetails(candidate) {
  const lines = [];
  const captureValue = candidateCaptureDisplayValue(candidate);
  if (captureValue) lines.push(escapeHtml(captureValue));
  const evidence = formatCandidateEvidenceLabel(candidate);
  if (evidence) lines.push(escapeHtml(evidence));
  return lines.length ? `${lines.join("<br>")}<br>` : "";
}

function candidateCaptureDisplayValue(candidate) {
  return candidateCaptureTime(candidate)
    || firstDateValue(candidate.media_creation_dates)
    || firstDateValue(candidate.filename_dates)
    || firstDateValue(candidate.filesystem_dates);
}

function firstDateValue(value) {
  return splitDateList(value)[0] || "";
}

function formatCandidateEvidenceLabel(candidate) {
  const evidence = String(candidate.evidence || "");
  const combined = `${evidence} ${candidate.filename || ""} ${candidate.path || ""}`;
  const rangeMatch = evidence.match(/date_within_(\\d+)_days/i);
  if (/manual_drop_copy/i.test(evidence)) return "Dropped photo";
  if (/manual_link/i.test(evidence)) return "Chosen photo";
  if (/manual_default_search/i.test(evidence)) return "Default search";
  if (/manual_index_date_search/i.test(evidence)) return "Custom date search";
  if (rangeMatch) return `Expanded search: ±${rangeMatch[1]} days`;
  if (/quality_positive|improved|enhanced|edited|final|master/i.test(combined)) return "Higher-quality filename hint";
  if (/quality_negative|web|small|thumbnail|preview|lowres|resized|compressed/i.test(combined)) return "Lower-quality filename hint";
  return "";
}

function formatFileSize(byteSize) {
  const bytes = Number(String(byteSize || "").trim());
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${Math.round(bytes).toLocaleString()} bytes`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "";
  for (const nextUnit of units) {
    value = value / 1024;
    unit = nextUnit;
    if (value < 1024) break;
  }
  const rounded = value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2);
  return `${rounded} ${unit}`;
}

function candidateMatchesText(candidate, text) {
  if (!text) return true;
  return [
    candidate.filename,
    candidate.path,
    candidate.mime_type,
    fileTypeLabel(candidate.filename || candidate.path || "", candidate.mime_type),
    formatFileSize(candidate.byte_size),
    candidate.dimensions,
    candidate.evidence,
    candidate.filename_dates,
    candidate.media_creation_dates,
    candidate.filesystem_dates,
    candidate.alignment_confidence,
    candidate.visual_rank,
    candidate.visual_score,
    candidate.visual_best_view,
    candidate.visual_error
  ].some(value => String(value || "").toLowerCase().includes(text));
}

function candidateMatchesEvidence(candidate, evidenceFilter) {
  if (evidenceFilter === "quality") {
    return /quality_positive|improved|enhanced|edited|final|master/i.test(`${candidate.evidence || ""} ${candidate.filename || ""} ${candidate.path || ""}`);
  }
  if (evidenceFilter === "likely") {
    return String(candidate.visual_likely || "").toLowerCase() === "true";
  }
  if (evidenceFilter === "entry_date") {
    return candidateMatchesEntryDate(candidate, ["filename_dates", "media_creation_dates", "filesystem_dates"]);
  }
  if (evidenceFilter === "filename_entry_date") {
    return candidateMatchesEntryDate(candidate, ["filename_dates"]);
  }
  if (evidenceFilter === "media_entry_date") {
    return candidateMatchesEntryDate(candidate, ["media_creation_dates"]);
  }
  if (evidenceFilter === "filesystem_entry_date") {
    return candidateMatchesEntryDate(candidate, ["filesystem_dates"]);
  }
  if (evidenceFilter === "media_date") {
    return Boolean(String(candidate.media_creation_dates || "").trim());
  }
  if (evidenceFilter === "alignment") {
    return Boolean(String(candidate.alignment_score || "").trim());
  }
  if (evidenceFilter === "visual") {
    return Boolean(String(candidate.visual_score || "").trim());
  }
  if (evidenceFilter === "selected") {
    return Boolean(candidate.selected);
  }
  return true;
}

function candidateMatchesEntryDate(candidate, fields) {
  const entryDate = String(state.currentEntry?.entry_date || "").trim();
  if (!entryDate) return false;
  return fields.some(field => splitDateList(candidate[field]).includes(entryDate));
}

function candidateMatchesTargetDate(candidate) {
  return candidateMatchesEntryDate(candidate, ["filename_dates", "media_creation_dates", "filesystem_dates"]);
}

function splitDateList(value) {
  return String(value || "").split(";").map(item => item.trim()).filter(Boolean);
}

function candidateMatchesFolder(candidate, folderFilter) {
  if (!folderFilter) return true;
  return candidateFolderValue(candidate) === folderFilter;
}

function prepareCandidateTimestampGroups(candidates, sortMode) {
  for (const candidate of candidates) {
    delete candidate._timestampGroupKey;
    delete candidate._timestampGroupDateDistance;
    delete candidate._timestampGroupCaptureTime;
    delete candidate._timestampGroupVisualRank;
    delete candidate._timestampGroupVisualScore;
  }
  if (sortMode !== "capture_time" && sortMode !== "visual") return;
  const groups = new Map();
  for (const candidate of candidates) {
    const key = candidateTimestampGroupKey(candidate);
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(candidate);
  }
  for (const [key, group] of groups) {
    const dateDistance = Math.min(...group.map(candidate => candidateDateDistance(candidate)));
    const visualRank = Math.min(...group.map(candidate => candidateVisualRank(candidate)));
    const visualScore = Math.min(...group.map(candidate => numericCandidateValue(candidate.visual_score, Number.POSITIVE_INFINITY)));
    for (const candidate of group) {
      candidate._timestampGroupKey = key;
      candidate._timestampGroupDateDistance = dateDistance;
      candidate._timestampGroupCaptureTime = key;
      candidate._timestampGroupVisualRank = visualRank;
      candidate._timestampGroupVisualScore = visualScore;
    }
  }
}

function compareCandidates(left, right, sortMode) {
  const targetDateCompare = candidateTargetDateRank(left) - candidateTargetDateRank(right);
  if (targetDateCompare) return targetDateCompare;
  const manualLinkCompare = candidateManualLinkRank(left) - candidateManualLinkRank(right);
  if (manualLinkCompare) return manualLinkCompare;
  const sourceCompare = candidateProjectOriginalRank(left) - candidateProjectOriginalRank(right);
  if (sourceCompare) return sourceCompare;
  if (sortMode === "capture_time") {
    const groupCompare = compareTimestampGroups(left, right, sortMode);
    return groupCompare
      || candidateQualityRank(left) - candidateQualityRank(right)
      || numericCandidateValue(right.byte_size) - numericCandidateValue(left.byte_size)
      || compareCandidateNames(left, right);
  }
  if (sortMode === "dimensions") {
    return candidatePixelCount(right) - candidatePixelCount(left)
      || numericCandidateValue(right.byte_size) - numericCandidateValue(left.byte_size)
      || compareCandidateNames(left, right);
  }
  if (sortMode === "largest") {
    return numericCandidateValue(right.byte_size) - numericCandidateValue(left.byte_size) || compareCandidateNames(left, right);
  }
  if (sortMode === "filename") {
    return compareCandidateNames(left, right);
  }
  if (sortMode === "alignment") {
    return numericCandidateValue(left.alignment_score, Number.POSITIVE_INFINITY) - numericCandidateValue(right.alignment_score, Number.POSITIVE_INFINITY) || compareCandidateNames(left, right);
  }
  if (sortMode === "visual") {
    const groupCompare = compareTimestampGroups(left, right, sortMode);
    return groupCompare
      || compareCandidateNames(left, right);
  }
  return 0;
}

function compareTimestampGroups(left, right, sortMode) {
  const leftKey = candidateTimestampGroupKey(left);
  const rightKey = candidateTimestampGroupKey(right);
  if (leftKey && leftKey === rightKey) return compareWithinTimestampGroup(left, right);
  if (sortMode === "capture_time") {
    const leftCaptureTime = timestampGroupCaptureTime(left);
    const rightCaptureTime = timestampGroupCaptureTime(right);
    return timestampGroupDateDistance(left) - timestampGroupDateDistance(right)
      || Number(!leftCaptureTime) - Number(!rightCaptureTime)
      || leftCaptureTime.localeCompare(rightCaptureTime);
  }
  if (sortMode === "visual") {
    return timestampGroupVisualRank(left) - timestampGroupVisualRank(right)
      || timestampGroupVisualScore(left) - timestampGroupVisualScore(right);
  }
  return 0;
}

function compareWithinTimestampGroup(left, right) {
  return candidatePixelCount(right) - candidatePixelCount(left)
    || candidateVisualRank(left) - candidateVisualRank(right)
    || numericCandidateValue(left.visual_score, Number.POSITIVE_INFINITY) - numericCandidateValue(right.visual_score, Number.POSITIVE_INFINITY)
    || candidateQualityRank(left) - candidateQualityRank(right)
    || numericCandidateValue(right.byte_size) - numericCandidateValue(left.byte_size)
    || compareCandidateNames(left, right);
}

function candidateCaptureTime(candidate) {
  return String(candidate.capture_timestamp || "").trim();
}

function candidateCaptureSortTime(candidate) {
  return candidateCaptureTime(candidate) || candidateFilenameCaptureTimestamp(candidate);
}

function candidateTimestampGroupKey(candidate) {
  const captureTime = candidateCaptureTime(candidate);
  if (captureTime.includes("T")) return captureTime;
  return candidateFilenameCaptureTimestamp(candidate);
}

function candidateFilenameCaptureTimestamp(candidate) {
  const text = `${candidate.filename || ""} ${candidate.path || ""}`;
  const compact = text.match(/(\\d{4})-(\\d{2})-(\\d{2})[ _-]+(\\d{2})(\\d{2})(\\d{2})/);
  if (compact) {
    return `${compact[1]}-${compact[2]}-${compact[3]}T${compact[4]}:${compact[5]}:${compact[6]}`;
  }
  const separated = text.match(/(\\d{4})-(\\d{2})-(\\d{2})[ _-]+(\\d{2})-(\\d{2})-(\\d{2})/);
  if (separated) {
    return `${separated[1]}-${separated[2]}-${separated[3]}T${separated[4]}:${separated[5]}:${separated[6]}`;
  }
  return "";
}

function timestampGroupDateDistance(candidate) {
  return candidate._timestampGroupDateDistance ?? candidateDateDistance(candidate);
}

function timestampGroupCaptureTime(candidate) {
  return candidate._timestampGroupCaptureTime ?? candidateCaptureSortTime(candidate);
}

function timestampGroupVisualRank(candidate) {
  return candidate._timestampGroupVisualRank ?? candidateVisualRank(candidate);
}

function timestampGroupVisualScore(candidate) {
  return candidate._timestampGroupVisualScore ?? numericCandidateValue(candidate.visual_score, Number.POSITIVE_INFINITY);
}

function candidateDateDistance(candidate) {
  const value = String(candidate.date_distance || "").trim();
  if (value && Number.isFinite(Number(value))) return Number(value);
  const match = String(candidate.evidence || "").match(/date_within_(\\d+)_days/);
  return match ? Number(match[1]) : 0;
}

function candidateQualityRank(candidate) {
  const text = `${candidate.evidence || ""} ${candidate.filename || ""} ${candidate.path || ""}`;
  if (/manual_link|manual_drop_copy/i.test(text)) return -1;
  if (/quality_positive|improved|enhanced|edited|final|master/i.test(text)) return 0;
  if (/quality_negative|web|small|thumbnail|preview|lowres|resized|compressed/i.test(text)) return 2;
  return 1;
}

function compareCandidateNames(left, right) {
  return String(left.filename || "").localeCompare(String(right.filename || ""));
}

function candidateManualLinkRank(candidate) {
  const text = `${candidate.evidence || ""} ${candidate.filename || ""} ${candidate.path || ""}`;
  return /manual_link|manual_drop_copy/i.test(text) ? 0 : 1;
}

function candidateProjectOriginalRank(candidate) {
  return candidateProjectOriginalsSource(candidate) ? 0 : 1;
}

function candidateProjectOriginalsSource(candidate) {
  return Boolean(candidate.project_originals_source);
}

function candidateTargetDateRank(candidate) {
  return candidateMatchesTargetDate(candidate) ? 0 : 1;
}

function numericCandidateValue(value, fallback = 0) {
  const number = Number(String(value || "").trim());
  return Number.isFinite(number) ? number : fallback;
}

function candidatePixelCount(candidate) {
  const match = String(candidate.dimensions || "").match(/(\\d+)\\s*x\\s*(\\d+)/i);
  if (!match) return 0;
  return Number(match[1]) * Number(match[2]);
}

function candidateVisualRank(candidate) {
  const rank = Number(String(candidate.visual_rank || "").trim());
  return Number.isFinite(rank) && rank > 0 ? rank : Number.POSITIVE_INFINITY;
}

function formatAlignment(candidate) {
  const visual = formatVisualRank(candidate);
  if (candidate.alignment_score) {
    const confidence = candidate.alignment_confidence ? ` · ${candidate.alignment_confidence}` : "";
    const crop = candidate.alignment_crop ? ` · crop ${candidate.alignment_crop}` : "";
    return `${visual}${visual ? "<br>" : ""}alignment ${escapeHtml(candidate.alignment_score)}${escapeHtml(confidence)}${escapeHtml(crop)}`;
  }
  if (candidate.alignment_error) {
    return `${visual}${visual ? "<br>" : ""}alignment unavailable · ${escapeHtml(candidate.alignment_error)}`;
  }
  return visual || "alignment not scored";
}

function formatVisualRank(candidate) {
  if (candidate.visual_score) {
    const view = candidate.visual_best_view ? ` · ${candidate.visual_best_view.replaceAll("_", " ")}` : "";
    return `visual #${escapeHtml(candidate.visual_rank || "?")} · ${escapeHtml(candidate.visual_score)}${escapeHtml(view)}`;
  }
  if (candidate.visual_error) {
    return `visual unavailable · ${escapeHtml(candidate.visual_error)}`;
  }
  return "";
}

async function saveDecision(candidate, decision, card, options = {}) {
  if (!state.currentEntry) return;
  const notes = "";
  const currentEntryId = state.currentEntry.entry_id;
  const currentEntryBeforeSave = state.currentEntry;
  const nextEntryId = nextEntryIdAfterCurrent();
  const buttons = [...card.querySelectorAll("button")];
  buttons.forEach(button => { button.disabled = true; });
  const isLink = options.link === true;
  const linkScrollState = isLink ? capturePickerScroll() : null;
  document.getElementById("crawlStatus").textContent = isLink
    ? (decision === "unlink_associated_photo" ? "Unlinking attachment..." : "Linking attachment...")
    : decision === "rejected" ? "Saving rejection..." : "Saving selection...";
  try {
    const updatedEntry = await fetchJson("/api/decision", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(Object.assign({
        entry_id: currentEntryId,
        candidate_path: candidate.path,
        decision,
        notes
      }, options.extra || {}))
    });
    const entrySummary = state.entries.find(entry => entry.entry_id === currentEntryId);
    if (entrySummary) {
      entrySummary.status = updatedEntry.status;
      entrySummary.candidate_count = updatedEntry.candidate_count;
      entrySummary.selected_count = updatedEntry.selected_count;
      entrySummary.pending_commit_count = updatedEntry.pending_commit_count;
      entrySummary.commit_ready = updatedEntry.commit_ready;
      entrySummary.accepted_count = updatedEntry.accepted_count;
      entrySummary.associated_count = updatedEntry.associated_count;
      entrySummary.rejected_count = updatedEntry.rejected_count;
      entrySummary.fallback_count = updatedEntry.fallback_count;
    }
    const shouldAdvance = options.advance !== false && shouldAdvanceAfterDecision(decision, updatedEntry);
    state.entryDetailCache.delete(currentEntryId);
    if (shouldAdvance) {
      rememberPreviousEntry(currentEntryBeforeSave);
      const index = state.entries.findIndex(entry => entry.entry_id === currentEntryId);
      if (index >= 0) state.entries.splice(index, 1);
      renderEntries();
      document.getElementById("crawlStatus").textContent = decision === "rejected" ? "Rejected." : "Accepted.";
      if (nextEntryId) await loadEntry(nextEntryId);
      else await loadEntries();
    } else {
      const currentCandidates = Array.isArray(currentEntryBeforeSave.candidates)
        ? currentEntryBeforeSave.candidates
        : [];
      state.currentEntry = {
        ...currentEntryBeforeSave,
        ...updatedEntry,
        candidates: Array.isArray(updatedEntry.candidates) ? updatedEntry.candidates : currentCandidates,
        candidate_total: currentEntryBeforeSave.candidate_total ?? updatedEntry.candidate_count
      };
      if (decision === "rejected") {
        state.currentEntry.candidates = state.currentEntry.candidates.filter(item => item.path !== candidate.path);
      } else if (decision === "use_external_original") {
        for (const item of state.currentEntry.candidates) {
          item.selected = item.path === candidate.path;
          if (item.path === candidate.path) {
            item.review_decision = decision;
            item.review_notes = notes;
          }
        }
      } else if (decision === "external_original_associated_photo") {
        const associatedUpdate = {
          associated: true,
          review_decision: decision,
          review_notes: notes,
          associated_entry_date: options.extra?.associated_entry_date || state.currentEntry.entry_date || "",
          associated_date_source: options.extra?.associated_date_source || "entry"
        };
        for (const item of state.currentEntry.candidates) {
          if (item.path === candidate.path) {
            Object.assign(item, associatedUpdate);
          }
        }
        Object.assign(candidate, associatedUpdate);
      } else if (decision === "unlink_associated_photo") {
        const associatedUpdate = {
          associated: false,
          review_decision: "",
          review_notes: "",
          associated_entry_date: "",
          associated_date_source: ""
        };
        for (const item of state.currentEntry.candidates) {
          if (item.path === candidate.path) {
            Object.assign(item, associatedUpdate);
          }
        }
        Object.assign(candidate, associatedUpdate);
      }
      state.currentEntry.candidate_count = updatedEntry.candidate_count;
      state.currentEntry.candidate_total = updatedEntry.candidate_count;
      state.currentEntry.status = updatedEntry.status;
      state.currentEntry.selected_count = updatedEntry.selected_count;
      state.currentEntry.pending_commit_count = updatedEntry.pending_commit_count;
      state.currentEntry.commit_ready = updatedEntry.commit_ready;
      state.currentEntry.accepted_count = updatedEntry.accepted_count;
      state.currentEntry.associated_count = updatedEntry.associated_count;
      state.currentEntry.rejected_count = updatedEntry.rejected_count;
      state.currentEntry.fallback_count = updatedEntry.fallback_count;
      state.entryDetailCache.set(currentEntryId, state.currentEntry);
      if (isLink) {
        applyEntryListState(entrySummary || state.currentEntry);
        for (const item of state.currentEntry.candidates) {
          const itemCard = item.path === candidate.path
            ? card
            : [...document.querySelectorAll(".candidate-card")].find(candidateCard => candidateCard.dataset.candidateToken === item.token);
          applySelectButtonState(itemCard, item);
          applyLinkButtonState(itemCard, item);
          applyFlagButtonState(itemCard?.querySelector('[data-action="flag"]'), item);
        }
        buttons.forEach(button => { button.disabled = false; });
        document.getElementById("entrySubhead").textContent =
          `${state.currentEntry.candidate_count} candidates · ${statusLabel(state.currentEntry.status)}`;
        restorePickerScroll(linkScrollState);
      } else {
        renderEntries();
        renderEntryDetail();
      }
      document.getElementById("crawlStatus").textContent = isLink
        ? (decision === "unlink_associated_photo" ? "Unlinked." : "Linked. Apply decisions when ready.")
        : "Rejected.";
    }
    scheduleSummaryRefresh();
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
    buttons.forEach(button => { button.disabled = false; });
    if (isLink) restorePickerScroll(linkScrollState);
  }
}

async function linkAssociatedPhoto(candidate, card) {
  if (candidate.selected) {
    document.getElementById("crawlStatus").textContent = "This is already the selected original.";
    return;
  }
  if (candidate.associated) {
    await saveDecision(candidate, "unlink_associated_photo", card, {
      advance: false,
      link: true
    });
    return;
  }
  await saveDecision(candidate, "external_original_associated_photo", card, {
    advance: false,
    link: true,
    extra: {
      associated_entry_date: state.currentEntry?.entry_date || "",
      associated_date_source: "entry"
    }
  });
}

async function showAssociatedDatePanel(candidate, card) {
  const panel = card.querySelector("[data-associated-panel]");
  const choicesTarget = card.querySelector("[data-associated-choices]");
  const existingDate = candidate.associated_entry_date || "";
  panel.hidden = false;
  setAssociatedManualInputs(card, existingDate || state.currentEntry?.entry_date || "");
  choicesTarget.innerHTML = `<div class="summary">Loading date choices...</div>`;
  document.getElementById("crawlStatus").textContent = "Loading date choices.";
  try {
    const payload = await fetchJson("/api/associated-date-choices", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: state.currentEntry.entry_id,
        candidate_path: candidate.path
      })
    });
    const choices = associatedCaptureDateChoices(payload.choices || []);
    renderAssociatedDateChoices(choicesTarget, choices, candidate, card);
    const firstChoice = choices[0];
    setAssociatedManualInputs(card, existingDate || firstChoice?.date || payload.default_date || state.currentEntry.entry_date);
    choicesTarget.dataset.loaded = "1";
    if (firstChoice) {
      setAssociatedDateChoiceSelection(card, firstChoice.source, firstChoice.date);
      await saveAssociatedPhotoFlagWithDate(candidate, card, firstChoice.date, firstChoice.source);
    } else {
      clearAssociatedDateChoiceSelection(card);
      document.getElementById("crawlStatus").textContent = "No Date captured found. Use the manual date and time.";
    }
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
    choicesTarget.innerHTML = `<div class="summary">${escapeHtml(error.message)}</div>`;
  }
}

function associatedCaptureDateChoices(choices) {
  const seenDates = new Set();
  return (choices || []).filter(choice => {
    const source = String(choice.source || "").trim().toLowerCase();
    const date = String(choice.date || "").trim();
    if (!(source === "capture" || source === "filename_timestamp") || !date || seenDates.has(date)) return false;
    seenDates.add(date);
    return true;
  }).slice(0, 1);
}

function renderAssociatedDateChoices(target, choices, candidate, card) {
  target.innerHTML = "";
  if (!choices.length) {
    target.innerHTML = `<div class="summary">No source dates found. Use the manual date below.</div>`;
    return;
  }
  for (const choice of choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "action-button associated-date-choice";
    button.dataset.associatedSource = choice.source;
    button.dataset.associatedDate = choice.date;
    button.setAttribute("aria-pressed", "false");
    button.innerHTML = `<strong>${escapeHtml(associatedDateSourceLabel(choice.source))}</strong><span>${escapeHtml(formatAssociatedDateValue(choice.date))}</span>`;
    button.onclick = () => {
      setAssociatedManualInputs(card, choice.date);
      setAssociatedDateChoiceSelection(card, choice.source, choice.date);
      saveAssociatedPhotoFlagWithDate(candidate, card, choice.date, choice.source);
    };
    target.appendChild(button);
  }
}

function associatedDateSourceLabel(source) {
  const labels = {
    capture: "Date captured",
    filename_timestamp: "Date captured",
    manual: "Manual date"
  };
  return labels[String(source || "").trim().toLowerCase()] || String(source || "Date source").replaceAll("_", " ");
}

function associatedDateInputValue(value) {
  const text = String(value || "").trim();
  if (/^\\d{4}-\\d{2}-\\d{2}$/.test(text)) return text;
  const match = text.match(/^(\\d{4}-\\d{2}-\\d{2})[T ](\\d{2}:\\d{2}(?::\\d{2})?)/);
  if (match) return match[1];
  return "";
}

function associatedTimeInputValue(value) {
  const text = String(value || "").trim();
  const match = text.match(/^\\d{4}-\\d{2}-\\d{2}[T ](\\d{2}:\\d{2}(?::\\d{2})?)/);
  if (!match) return "00:00:00";
  return match[1].length === 5 ? `${match[1]}:00` : match[1];
}

function setAssociatedManualInputs(card, value) {
  card.querySelector("[data-associated-date]").value = associatedDateInputValue(value);
  card.querySelector("[data-associated-time]").value = associatedTimeInputValue(value);
}

function normalizeAssociatedManualValue(dateValue, timeValue) {
  const dateText = String(dateValue || "").trim();
  let timeText = String(timeValue || "").trim();
  if (/^\\d{2}:\\d{2}$/.test(timeText)) timeText = `${timeText}:00`;
  if (!/^\\d{4}-\\d{2}-\\d{2}$/.test(dateText) || !/^\\d{2}:\\d{2}:\\d{2}$/.test(timeText)) return "";
  return `${dateText}T${timeText}`;
}

function setAssociatedDateChoiceSelection(card, source, date) {
  const normalizedSource = String(source || "").trim().toLowerCase();
  const normalizedDate = String(date || "").trim();
  card.querySelectorAll(".associated-date-choice").forEach(button => {
    const selected = button.dataset.associatedSource === normalizedSource && button.dataset.associatedDate === normalizedDate;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", selected ? "true" : "false");
  });
}

function clearAssociatedDateChoiceSelection(card) {
  card.querySelectorAll(".associated-date-choice").forEach(button => {
    button.classList.remove("is-selected");
    button.setAttribute("aria-pressed", "false");
  });
}

function syncAssociatedDateChoiceSelection(card, candidate) {
  const source = candidate.associated_date_source || "";
  const date = candidate.associated_entry_date || "";
  if (source === "manual") clearAssociatedDateChoiceSelection(card);
  else setAssociatedDateChoiceSelection(card, source, date);
}

function formatAssociatedDateValue(value) {
  const text = String(value || "").trim();
  return text.replace("T", " ");
}

async function saveAssociatedPhotoFlag(candidate, card) {
  const dateInput = card.querySelector("[data-associated-date]");
  const timeInput = card.querySelector("[data-associated-time]");
  const associatedDate = normalizeAssociatedManualValue(dateInput.value || state.currentEntry?.entry_date || "", timeInput.value || "");
  if (!associatedDate) {
    document.getElementById("crawlStatus").textContent = "Choose a valid associated date and time.";
    return;
  }
  await saveAssociatedPhotoFlagWithDate(candidate, card, associatedDate, "manual");
}

async function saveAssociatedPhotoFlagWithDate(candidate, card, associatedDate, source) {
  await saveDecisionWithExtra(candidate, "external_original_associated_photo", card, {
    associated_entry_date: associatedDate,
    associated_date_source: source || "manual"
  });
}

async function saveDecisionWithExtra(candidate, decision, card, extra) {
  const notes = "";
  const currentEntryId = state.currentEntry.entry_id;
  const buttons = [...card.querySelectorAll("button")];
  buttons.forEach(button => { button.disabled = true; });
  document.getElementById("crawlStatus").textContent = "Saving flag.";
  try {
    const updatedEntry = await fetchJson("/api/decision", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(Object.assign({
        entry_id: currentEntryId,
        candidate_path: candidate.path,
        decision,
        notes
      }, extra || {}))
    });
    Object.assign(candidate, {
      review_decision: decision,
      review_notes: notes,
      associated: decision === "external_original_associated_photo",
      associated_entry_date: extra?.associated_entry_date || "",
      associated_date_source: extra?.associated_date_source || "manual"
    });
    const entrySummary = state.entries.find(entry => entry.entry_id === currentEntryId);
    if (entrySummary) {
      entrySummary.status = updatedEntry.status;
      entrySummary.candidate_count = updatedEntry.candidate_count;
      entrySummary.pending_commit_count = updatedEntry.pending_commit_count;
      entrySummary.commit_ready = updatedEntry.commit_ready;
    }
    state.currentEntry.status = updatedEntry.status;
    state.currentEntry.candidate_count = updatedEntry.candidate_count;
    state.currentEntry.candidate_total = updatedEntry.candidate_count;
    state.currentEntry.pending_commit_count = updatedEntry.pending_commit_count;
    state.currentEntry.commit_ready = updatedEntry.commit_ready;
    state.entryDetailCache.set(currentEntryId, state.currentEntry);
    buttons.forEach(button => { button.disabled = false; });
    applyFlagButtonState(card.querySelector('[data-action="flag"]'), candidate);
    syncAssociatedDateChoiceSelection(card, candidate);
    document.getElementById("crawlStatus").textContent = "";
    scheduleSummaryRefresh();
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
    buttons.forEach(button => { button.disabled = false; });
  }
}

function scheduleSummaryRefresh() {
  if (state.summaryRefreshTimer) clearTimeout(state.summaryRefreshTimer);
  state.summaryRefreshTimer = setTimeout(() => {
    loadSummary().catch(error => {
      document.getElementById("summary").textContent = error.message;
    });
  }, 1500);
}

function navigableEntrySummary(entry) {
  if (!entry?.entry_id) return null;
  return {
    entry_id: entry.entry_id,
    entry_date: entry.entry_date || "",
    status: entry.status || "needs_review",
    candidate_count: Number(entry.candidate_count || entry.candidate_total || 0),
    selected_count: Number(entry.selected_count || entry.accepted_count || 0),
    pending_commit_count: entryPendingCommitCount(entry),
    commit_ready: entryHasPendingCommit(entry),
    accepted_count: Number(entry.accepted_count || entry.selected_count || 0),
    associated_count: Number(entry.associated_count || 0),
    rejected_count: Number(entry.rejected_count || 0),
    fallback_count: Number(entry.fallback_count || 0),
    source_token: entry.source_token || "",
    source_exists: Boolean(entry.source_exists),
    people_names: Array.isArray(entry.people_names) ? entry.people_names : [],
    current_match_status: entry.current_match_status || "",
    current_decision: entry.current_decision || "",
    manual_search_message: entry.manual_search_message || "",
    used_range_days: entry.used_range_days || null
  };
}

function rememberPreviousEntry(entry) {
  const summary = navigableEntrySummary(entry);
  if (!summary || summary.entry_id === state.selectedEntryId && !state.currentEntry) return;
  state.previousEntryStack = state.previousEntryStack.filter(item => item.entry_id !== summary.entry_id);
  state.previousEntryStack.push(summary);
  if (state.previousEntryStack.length > 20) state.previousEntryStack.shift();
}

async function loadRememberedPreviousEntry() {
  while (state.previousEntryStack.length) {
    const previous = state.previousEntryStack.pop();
    if (!previous?.entry_id || previous.entry_id === state.selectedEntryId) continue;
    if (!state.entries.some(entry => entry.entry_id === previous.entry_id)) {
      const index = currentEntryIndex();
      state.entries.splice(index >= 0 ? index : 0, 0, previous);
      state.entryOffset = state.entries.length;
      renderEntries();
      renderEntryPaging();
    }
    await loadEntry(previous.entry_id);
    return true;
  }
  return false;
}

function nextEntryIdAfterCurrent() {
  const index = state.entries.findIndex(entry => entry.entry_id === state.selectedEntryId);
  if (index < 0) return "";
  const next = state.entries[index + 1] || state.entries[index - 1];
  return next ? next.entry_id : "";
}

function currentEntryIndex() {
  return state.entries.findIndex(entry => entry.entry_id === state.selectedEntryId);
}

function currentEntryPositionLabel() {
  const index = currentEntryIndex();
  if (index < 0 || !state.entries.length) return "";
  return `${index + 1} of ${state.entries.length}`;
}

function updateNavigationState() {
  const index = currentEntryIndex();
  if (index < 0) document.getElementById("entryPosition").textContent = "";
  const previousButton = document.getElementById("previousEntryButton");
  const nextButton = document.getElementById("nextEntryButton");
  if (previousButton) previousButton.disabled = index <= 0 && !state.previousEntryStack.length;
  if (nextButton) nextButton.disabled = index < 0 || (index >= state.entries.length - 1 && !state.entryHasMore);
}

async function selectAdjacentEntry(direction) {
  const index = currentEntryIndex();
  if (index < 0) {
    if (direction < 0) await loadRememberedPreviousEntry();
    return;
  }
  const targetIndex = index + direction;
  if (targetIndex >= 0 && targetIndex < state.entries.length) {
    await loadEntry(state.entries[targetIndex].entry_id);
    return;
  }
  if (direction < 0 && await loadRememberedPreviousEntry()) return;
  if (direction > 0 && state.entryHasMore) {
    await loadMoreEntries();
    const refreshedIndex = currentEntryIndex();
    const next = state.entries[refreshedIndex + 1];
    if (next) await loadEntry(next.entry_id);
  }
}

function isPickerShortcutEditableTarget(target) {
  if (!target) return false;
  const focusedControl = target.closest?.("input, select, textarea, button, a, [contenteditable='true']");
  return Boolean(focusedControl);
}

function runPickerShortcutButton(buttonId, action) {
  const button = document.getElementById(buttonId);
  if (!button || button.disabled) return false;
  action();
  return true;
}

function handlePickerKeyboardShortcut(event) {
  const previewModal = document.getElementById("imagePreviewModal");
  if (event.key === "Escape" && previewModal?.classList.contains("is-open")) {
    closeImagePreview();
    event.preventDefault();
    return;
  }
  const videoFrameModal = document.getElementById("videoFrameModal");
  const videoFrameOpen = videoFrameModal?.classList.contains("is-open");
  if (
    (event.key === " " || event.code === "Space")
    && videoFrameOpen
    && !previewModal?.classList.contains("is-open")
    && !event.repeat
    && !event.metaKey
    && !event.ctrlKey
    && !event.altKey
  ) {
    toggleVideoFramePlayback();
    event.preventDefault();
    return;
  }
  if (
    videoFrameOpen
    && !previewModal?.classList.contains("is-open")
    && (event.key === "ArrowLeft" || event.key === "ArrowRight")
    && !event.metaKey
    && !event.ctrlKey
    && !event.altKey
  ) {
    const direction = event.key === "ArrowLeft" ? -1 : 1;
    stepVideoFrameTime(direction * (event.shiftKey ? 1 : 0.1));
    event.preventDefault();
    return;
  }
  if (
    event.key === "Enter"
    && videoFrameOpen
    && !previewModal?.classList.contains("is-open")
    && !event.repeat
    && !event.metaKey
    && !event.ctrlKey
    && !event.altKey
  ) {
    const captureButton = document.getElementById("videoFrameAddCustom");
    if (captureButton && !captureButton.disabled) {
      captureCurrentVideoFrame();
      event.preventDefault();
      return;
    }
  }
  if (event.repeat || event.metaKey || event.ctrlKey || event.altKey || isPickerShortcutEditableTarget(event.target)) return;
  let handled = false;
  if (event.key === "ArrowLeft") {
    handled = runPickerShortcutButton("previousEntryButton", () => selectAdjacentEntry(-1));
  } else if (event.key === "ArrowRight") {
    handled = runPickerShortcutButton("nextEntryButton", () => selectAdjacentEntry(1));
  } else if (event.key.toLowerCase() === "r") {
    handled = runPickerShortcutButton("rejectAllButton", rejectAllCandidates);
  } else if (event.key.toLowerCase() === "p") {
    handled = runPickerShortcutButton("fallbackButton", () => document.getElementById("fallbackButton").click());
  }
  if (handled) event.preventDefault();
}

function shouldAdvanceAfterDecision(decision, updatedEntry) {
  if (decision === "use_external_original") return true;
  if (decision === "keep_project365_export") return true;
  if (decision === "rejected") return updatedEntry.status === "rejected";
  return false;
}

function entryMatchesStatusFilter(entry, status) {
  return status === "all"
    || entry.status === status
    || (status === "accepted_not_applied" && entry.status === "selected")
    || (status === "needs_action" && ["needs_review", "search_needed"].includes(entry.status));
}

async function commitEntryDecision(entryId, button = null) {
  const entry = state.entries.find(item => item.entry_id === entryId);
  const committedEntryDate = entry?.entry_date || state.currentEntry?.entry_date || "";
  if (button) button.disabled = true;
  if (state.summaryRefreshTimer) clearTimeout(state.summaryRefreshTimer);
  state.summaryRefreshTimer = null;
  document.getElementById("crawlStatus").textContent = "Committing linked original.";
  const nextEntryId = entryId === state.selectedEntryId ? nextEntryIdAfterCurrent() : state.selectedEntryId;
  state.entryDetailCache.delete(entryId);
  try {
    const result = await fetchJson("/api/commit-entry", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({entry_id: entryId})
    });
    const skipped = Number(result.skipped_unknown_media_count || 0);
    const removedUnknown = Number(result.removed_unknown_media_entries || 0);
    const skippedText = skipped
      ? ` Skipped ${skipped} decision${skipped === 1 ? "" : "s"} because this target is not in the database.`
      : "";
    const removedUnknownText = removedUnknown
      ? ` Removed ${removedUnknown} missing target entr${removedUnknown === 1 ? "y" : "ies"} from the queue.`
      : "";
    document.getElementById("crawlStatus").textContent =
      `Committed ${result.selected_count || 0} linked original.${skippedText}${removedUnknownText} ${result.remaining_entries || 0} entries remain.`;
    if (!Number(result.applied_count || 0)) {
      await loadSummary();
      await loadBatches();
      await loadEntries(state.selectedEntryId || entryId, true, committedEntryDate);
      return;
    }
    state.suppressedCommittedEntryIds.add(entryId);
    state.selectedEntryIds.delete(entryId);
    state.entries = state.entries.filter(entry => entry.entry_id !== entryId);
    if (state.selectedEntryId === entryId) state.selectedEntryId = nextEntryId || null;
    renderEntries();
    await loadSummary();
    await loadBatches();
    await loadEntries(nextEntryId, true, committedEntryDate);
  } catch (error) {
    state.suppressedCommittedEntryIds.delete(entryId);
    document.getElementById("crawlStatus").textContent = error.message;
    if (button) button.disabled = false;
  }
}

async function applyDecisions() {
  if (!confirm("Apply selected originals, linked originals, flagged associated photos, rejected candidates, and fallback decisions to the database?")) return;
  const button = document.getElementById("applyDecisionsButton");
  button.disabled = true;
  if (state.summaryRefreshTimer) clearTimeout(state.summaryRefreshTimer);
  state.summaryRefreshTimer = null;
  state.entryDetailCache.clear();
  document.getElementById("crawlStatus").textContent = "Applying reviewed decisions.";
  try {
    const result = await fetchJson("/api/apply-decisions", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({confirm_apply_decisions: "apply-reviewed-decisions"})
    });
    const skipped = Number(result.skipped_unknown_media_count || 0);
    const removedUnknown = Number(result.removed_unknown_media_entries || 0);
    const skippedText = skipped
      ? ` Skipped ${skipped} decision${skipped === 1 ? "" : "s"} for test/missing target entries that are not in the database.`
      : "";
    const removedUnknownText = removedUnknown
      ? ` Removed ${removedUnknown} missing target entr${removedUnknown === 1 ? "y" : "ies"} from the queue.`
      : "";
    document.getElementById("crawlStatus").textContent =
      `Applied ${result.applied_count || 0}: ${result.selected_count || 0} selected, ${result.associated_count || 0} linked, ${result.rejected_count || 0} rejected, ${result.fallback_count || 0} fallback.${skippedText}${removedUnknownText} ${result.remaining_entries || 0} entries remain.`;
    await loadSummary();
    await loadBatches();
    state.entryDetailCache.clear();
    await loadEntries();
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function expandDefaultDateRange() {
  if (!state.currentEntry) return;
  if (!ensureIndexSearchScopeAvailable()) return;
  const buttons = [document.getElementById("defaultDateRange"), ...document.querySelectorAll("[data-range-days]")];
  buttons.forEach(button => { button.disabled = true; });
  const entryId = state.currentEntry.entry_id;
  const wholeIndex = currentIndexSearchWholeIndex();
  const filenameOnly = !wholeIndex;
  const includeModifiedDates = wholeIndex && currentIndexIncludeModifiedDates();
  const scopeLabel = currentIndexScopeLabel(false);
    setCrawlStatus(`Searching default ${scopeLabel} candidates.`, true);
  try {
    const payload = {entry_id: entryId, search_whole_index: wholeIndex, filename_dates_only: filenameOnly, include_modified_dates: includeModifiedDates};
    const result = await fetchJson("/api/expand-default-date-range", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(payload)
    });
    state.filenameDateScopeEntryId = entryId;
    state.filenameDateScopeDays = 0;
    state.filenameDateScopeStart = "";
    state.filenameDateScopeEnd = "";
    state.currentEntry = result.entry;
    state.entryDetailCache.set(entryId, result.entry);
    syncEntrySummaryFromDetail(result.entry);
    renderEntries();
    renderEntryDetail();
    await loadBatches();
    const resultScope = result.search_whole_index
      ? "whole photo index"
      : "project originals folder";
    const resultDateSources = result.filename_dates_only
      ? "filename dates"
      : result.include_modified_dates ? "file name, media, filesystem, and modified dates" : "file name, media, and filesystem dates";
    const actionLabel = result.replace_candidates ? "Loaded" : "Added";
    setCrawlStatus(`${actionLabel} ${result.added_count || 0} candidates from the default search in the ${resultScope}, ${resultDateSources}. ${result.candidate_count || 0} candidates are now available.`);
  } catch (error) {
    setCrawlStatus(error.message);
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

async function expandDateRange(days) {
  if (!state.currentEntry) return;
  if (!ensureIndexSearchScopeAvailable()) return;
  const buttons = [document.getElementById("defaultDateRange"), ...document.querySelectorAll("[data-range-days]")];
  buttons.forEach(button => { button.disabled = true; });
  const entryId = state.currentEntry.entry_id;
  const wholeIndex = currentIndexSearchWholeIndex();
  const filenameOnly = !wholeIndex;
  const includeModifiedDates = wholeIndex && currentIndexIncludeModifiedDates();
  const scopeLabel = currentIndexScopeLabel(false);
    setCrawlStatus(`Expanding ${scopeLabel} candidates to ±${days} days.`, true);
  try {
    const payload = {entry_id: entryId, days, search_whole_index: wholeIndex, filename_dates_only: filenameOnly, include_modified_dates: includeModifiedDates};
    const result = await fetchJson("/api/expand-date-range", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(payload)
    });
    state.filenameDateScopeEntryId = entryId;
    state.filenameDateScopeDays = days;
    state.filenameDateScopeStart = "";
    state.filenameDateScopeEnd = "";
    state.currentEntry = result.entry;
    state.entryDetailCache.set(entryId, result.entry);
    syncEntrySummaryFromDetail(result.entry);
    renderEntries();
    renderEntryDetail();
    await loadBatches();
    const resultScope = result.search_whole_index
      ? "whole photo index"
      : "project originals folder";
    const resultDateSources = result.filename_dates_only
      ? "filename dates"
      : result.include_modified_dates ? "file name, media, filesystem, and modified dates" : "file name, media, and filesystem dates";
    const actionLabel = result.replace_candidates ? "Loaded" : "Added";
    setCrawlStatus(`${actionLabel} ${result.added_count || 0} candidates from ±${days} days in the ${resultScope}, ${resultDateSources}. ${result.candidate_count || 0} candidates are now available.`);
  } catch (error) {
    setCrawlStatus(error.message);
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

async function searchIndexDateRange() {
  const entry = state.currentEntry;
  if (!entry) return;
  const startDate = document.getElementById("indexDateStart").value;
  const endDate = document.getElementById("indexDateEnd").value;
  const wholeIndex = currentIndexSearchWholeIndex();
  const filenameOnly = !wholeIndex;
  const includeModifiedDates = wholeIndex && currentIndexIncludeModifiedDates();
  const button = document.getElementById("searchIndexDateRange");
  if (!ensureIndexSearchScopeAvailable()) return;
  if (!startDate || !endDate) {
    setCrawlStatus("Enter a start and end date.");
    return;
  }
  button.disabled = true;
  const scopeLabel = currentIndexScopeLabel();
  setCrawlStatus(`Searching ${scopeLabel} from ${startDate} to ${endDate}.`, true);
  try {
    const payload = {
      entry_id: entry.entry_id,
      start_date: startDate,
      end_date: endDate,
      search_whole_index: wholeIndex,
      filename_dates_only: filenameOnly,
      include_modified_dates: includeModifiedDates
    };
    const result = await fetchJson("/api/search-index-date-range", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(payload)
    });
    state.filenameDateScopeEntryId = entry.entry_id;
    state.filenameDateScopeDays = 0;
    state.filenameDateScopeStart = startDate;
    state.filenameDateScopeEnd = endDate;
    state.currentEntry = result.entry;
    state.entryDetailCache.set(entry.entry_id, result.entry);
    syncEntrySummaryFromDetail(result.entry);
    renderEntries();
    renderEntryDetail();
    await loadBatches();
    const rangeLabel = result.start_date === result.end_date
      ? result.start_date
      : `${result.start_date} to ${result.end_date}`;
    const resultScope = result.search_whole_index
      ? "whole photo index"
      : "project originals folder";
    const resultDateSources = result.filename_dates_only
      ? "filename dates"
      : result.include_modified_dates ? "file name, media, filesystem, and modified dates" : "file name, media, and filesystem dates";
    const actionLabel = result.replace_candidates ? "Loaded" : "Added";
    setCrawlStatus(`${actionLabel} ${result.added_count || 0} candidates from ${rangeLabel} in the ${resultScope}, ${resultDateSources}. ${result.candidate_count || 0} candidates are now available.`);
  } catch (error) {
    setCrawlStatus(error.message);
  } finally {
    button.disabled = false;
  }
}

async function rejectAllCandidates() {
  if (!state.currentEntry || !state.currentEntry.candidates.length) return;
  const button = document.getElementById("rejectAllButton");
  const currentEntryId = state.currentEntry.entry_id;
  const nextEntryId = nextEntryIdAfterCurrent();
  const candidateCount = Number(state.currentEntry.candidate_total || state.currentEntry.candidate_count || 0);
  button.disabled = true;
  document.getElementById("crawlStatus").textContent = `Rejecting ${candidateCount} candidates.`;
  try {
    const result = await fetchJson("/api/reject-all", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: currentEntryId,
        notes: "Rejected with Reject all.",
        photo_index_folder: currentIndexSearchWholeIndex() ? "" : state.activePhotoIndexFolder
      })
    });
    const updatedEntry = result.entry;
    const entrySummary = state.entries.find(entry => entry.entry_id === currentEntryId);
    if (entrySummary) {
      entrySummary.status = updatedEntry.status;
      entrySummary.candidate_count = updatedEntry.candidate_count;
      entrySummary.selected_count = updatedEntry.selected_count;
      entrySummary.pending_commit_count = updatedEntry.pending_commit_count;
      entrySummary.commit_ready = updatedEntry.commit_ready;
    }
    state.currentEntry = updatedEntry;
    state.entryDetailCache.delete(currentEntryId);
    document.getElementById("crawlStatus").textContent = `Rejected ${result.rejected_count || 0} candidates.`;
    await loadSummary();
    await loadBatches();
    const filter = document.getElementById("filter").value;
    if (entryMatchesStatusFilter(updatedEntry, filter)) {
      state.entryDetailCache.set(currentEntryId, updatedEntry);
      renderEntries();
      renderEntryDetail();
    } else {
      rememberPreviousEntry(updatedEntry);
      const index = state.entries.findIndex(entry => entry.entry_id === currentEntryId);
      if (index >= 0) state.entries.splice(index, 1);
      renderEntries();
      if (nextEntryId && state.entries.some(entry => entry.entry_id === nextEntryId)) {
        await loadEntry(nextEntryId);
      } else if (state.entries.length) {
        const targetIndex = index >= 0 ? Math.min(index, state.entries.length - 1) : 0;
        await loadEntry(state.entries[targetIndex].entry_id);
      } else {
        await loadEntries("", true, updatedEntry.entry_date || "");
      }
    }
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

document.getElementById("filter").onchange = changeStatusFilter;
document.getElementById("batchFilter").onchange = changeBatchFilter;
document.getElementById("clearBatchFilter").onclick = clearBatchFilter;
document.getElementById("candidateFilter").oninput = renderCandidateGrid;
document.getElementById("candidateEvidenceFilter").onchange = renderCandidateGrid;
document.getElementById("candidateFolderFilter").onchange = renderCandidateGrid;
document.getElementById("candidateSort").onchange = renderCandidateGrid;
document.getElementById("candidateLocationOnly").onchange = renderCandidateGrid;
document.querySelectorAll("[data-time-filter]").forEach(button => {
  button.onclick = () => toggleCandidateTimeFilter(button);
});
document.getElementById("chooseFolder").onclick = chooseFolder;
document.getElementById("choosePhoto").onclick = choosePhoto;
document.getElementById("chooseLinkedPhoto").onclick = chooseLinkedPhoto;
document.getElementById("searchPanelToggle").onclick = () => {
  const button = document.getElementById("searchPanelToggle");
  setSearchPanelExpanded(button.getAttribute("aria-expanded") !== "true");
};
document.getElementById("crawlCurrent").onclick = () => startCrawl("current");
document.getElementById("crawlSelected").onclick = () => startCrawl("selected");
document.getElementById("crawlVisible").onclick = () => startCrawl("visible");
document.getElementById("selectVisible").onclick = selectVisibleEntries;
document.getElementById("clearSelection").onclick = clearSelectedEntries;
document.getElementById("entryDateJumpButton").onclick = () => {
  jumpToEntryDate().catch(error => {
    document.getElementById("entryDateJumpStatus").textContent = error.message;
  });
};
for (const inputId of ["entryDateJump", "entryDateJumpEnd"]) {
  document.getElementById(inputId).onkeydown = event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    jumpToEntryDate().catch(error => {
      document.getElementById("entryDateJumpStatus").textContent = error.message;
    });
  };
}
document.getElementById("previousEntryButton").onclick = () => selectAdjacentEntry(-1);
document.getElementById("nextEntryButton").onclick = () => selectAdjacentEntry(1);
document.getElementById("applyDecisionsButton").onclick = applyDecisions;
document.getElementById("rejectAllButton").onclick = rejectAllCandidates;
document.getElementById("defaultDateRange").onclick = expandDefaultDateRange;
document.querySelectorAll("[data-range-days]").forEach(button => {
  button.onclick = () => expandDateRange(Number(button.dataset.rangeDays));
});
document.getElementById("searchIndexDateRange").onclick = searchIndexDateRange;
document.getElementById("indexSearchWholeIndex").onchange = event => {
  event.currentTarget.dataset.userChanged = "1";
  state.candidateRenderLimit = 40;
  updateManualDateSearchScope();
  renderCandidateGrid();
};
document.getElementById("indexSearchModifiedDate").onchange = () => {
  updateManualDateSearchScope();
  renderCandidateGrid();
};
document.getElementById("fallbackButton").onclick = async () => {
  if (!state.currentEntry) return;
  if (!confirm("Keep the Project365 export for this entry and remove it from the active original-photo search list?")) return;
  const currentEntryId = state.currentEntry.entry_id;
  const nextEntryId = nextEntryIdAfterCurrent();
  rememberPreviousEntry(state.currentEntry);
  await fetchJson("/api/decision", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      entry_id: currentEntryId,
      candidate_path: "",
      decision: "keep_project365_export",
      notes: "No external original selected; keep Project365 export as fallback."
    })
  });
  await loadSummary();
  await loadEntries(nextEntryId);
};
document.getElementById("clearButton").onclick = async () => {
  if (!state.currentEntry) return;
  if (!confirm(`Reset pending selections, rejections, notes, and candidate expansions for ${state.currentEntry.entry_date}?`)) return;
  const currentEntryId = state.currentEntry.entry_id;
  await fetchJson("/api/decision", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      entry_id: currentEntryId,
      candidate_path: "",
      decision: "clear",
      notes: ""
    })
  });
  await loadSummary();
  await loadEntries(currentEntryId);
};

function toggleEntrySelection(entryId, index, shiftKey, checked) {
  if (shiftKey && state.lastCheckedIndex !== null) {
    const start = Math.min(state.lastCheckedIndex, index);
    const end = Math.max(state.lastCheckedIndex, index);
    for (let i = start; i <= end; i += 1) {
      const rangeEntry = state.entries[i];
      if (!rangeEntry) continue;
      if (checked) {
        state.selectedEntryIds.add(rangeEntry.entry_id);
      } else {
        state.selectedEntryIds.delete(rangeEntry.entry_id);
      }
    }
  } else if (checked) {
    state.selectedEntryIds.add(entryId);
  } else {
    state.selectedEntryIds.delete(entryId);
  }
  state.lastCheckedIndex = index;
  renderEntries();
}

function selectVisibleEntries() {
  for (const entry of state.entries) {
    state.selectedEntryIds.add(entry.entry_id);
  }
  renderEntries();
}

function clearSelectedEntries() {
  state.selectedEntryIds.clear();
  state.lastCheckedIndex = null;
  renderEntries();
}

function renderSelectionSummary() {
  const scope = activeEntryIds().length || activeEntryDates().length
    ? ` · filtered`
    : "";
  document.getElementById("selectionSummary").textContent =
    `${state.selectedEntryIds.size} selected${scope}`;
}

function activeEntryIds() {
  return [...state.urlEntryIds, ...state.batchEntryIds];
}

function activeEntryDates() {
  return [...state.urlEntryDates, ...state.batchEntryDates];
}

function explicitEntryScopeActive() {
  return Boolean(state.urlEntryIds.length || state.urlEntryDates.length);
}

function changeBatchFilter() {
  setEntryLoadAllUrl(false);
  syncBatchFilterState();
  state.selectedEntryIds.clear();
  state.lastCheckedIndex = null;
  loadEntries();
}

function changeStatusFilter() {
  const status = document.getElementById("filter").value;
  document.getElementById("batchFilter").value = "";
  state.batchEntryIds = [];
  state.batchEntryDates = [];
  state.selectedEntryIds.clear();
  state.lastCheckedIndex = null;
  state.initialBatchId = "";
  clearUrlEntryScope(status);
  loadEntries();
}

function syncBatchFilterState() {
  const selectedId = document.getElementById("batchFilter").value;
  const batch = state.batches.find(item => item.batch_id === selectedId);
  if (!batch) {
    state.batchEntryIds = [];
    state.batchEntryDates = [];
  } else {
    state.batchEntryIds = splitFilterValue(batch.entry_ids || "");
    state.batchEntryDates = splitFilterValue(batch.entry_dates || "");
  }
  renderBatchSummary();
}

function clearBatchFilter() {
  document.getElementById("batchFilter").value = "";
  clearUrlEntryScope();
  document.getElementById("filter").value = "needs_action";
  changeBatchFilter();
}

function clearUrlEntryScope(status = "needs_action") {
  state.urlEntryIds = [];
  state.urlEntryDates = [];
  state.entryLoadAll = false;
  replaceEntryDateScopeUrl([], status);
  syncEntryDateJumpControl();
}

function setActivePhotoIndexFolder(folder) {
  state.activePhotoIndexFolder = String(folder || "").trim();
  try {
    if (state.activePhotoIndexFolder) {
      window.localStorage.setItem("project365.activePhotoIndexFolder", state.activePhotoIndexFolder);
    } else {
      window.localStorage.removeItem("project365.activePhotoIndexFolder");
    }
  } catch (_) {
  }
  updateManualDateSearchScope();
}

function renderBatchSummary() {
  const selectedId = document.getElementById("batchFilter").value;
  const batch = state.batches.find(item => item.batch_id === selectedId);
  const target = document.getElementById("batchSummary");
  if (!batch) {
    const archived = state.archivedBatchCount ? ` ${state.archivedBatchCount} completed batches archived.` : "";
    target.textContent = `Showing ${state.entries.length} entries from all active batches.${archived}`;
    return;
  }
  target.textContent =
    `${batch.batch_id}: ${batch.start_date} to ${batch.end_date} · ${batch.entry_count || "0"} entries · ${batch.candidate_count || "0"} candidates · ${batchStatusLabel(batch)}`;
}

function setSearchPanelExpanded(expanded) {
  const button = document.getElementById("searchPanelToggle");
  const body = document.getElementById("searchPanelBody");
  body.hidden = !expanded;
  button.setAttribute("aria-expanded", expanded ? "true" : "false");
  button.textContent = expanded ? "Collapse" : "Expand";
}

function crawlRoots() {
  return document.getElementById("crawlRoots").value
    .split(";")
    .map(value => value.trim())
    .filter(Boolean);
}

async function chooseFolder() {
  try {
    document.getElementById("crawlStatus").textContent = "Opening folder picker...";
    const payload = await fetchJson("/api/choose-folder");
    if (payload.path) {
      appendCrawlRoot(payload.path);
      document.getElementById("crawlStatus").textContent = "Folder added. Search current, selected, or visible entries.";
    } else {
      document.getElementById("crawlStatus").textContent = "Folder selection canceled.";
    }
  } catch (error) {
    if (isVideoDrop) {
      state.videoFramePendingToken = "";
      document.getElementById("videoFrameHelp").textContent = error.message;
      document.getElementById("videoFrameGrid").innerHTML =
        `<div class="video-frame-processing">${escapeHtml(error.message)}</div>`;
    }
    document.getElementById("crawlStatus").textContent = error.message;
  }
}

async function choosePhoto() {
  if (!state.currentEntry) {
    document.getElementById("crawlStatus").textContent = "Open an entry before choosing a photo.";
    return;
  }
  try {
    const payload = await fetchJson("/api/choose-photo");
    if (payload.path) await linkCandidatePath(payload.path);
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
  }
}

function chooseLinkedPhoto() {
  if (!state.currentEntry) {
    document.getElementById("crawlStatus").textContent = "Open an entry before linking a photo.";
    return;
  }
  const params = new URLSearchParams({
    entry_id: state.currentEntry.entry_id,
    entry_date: state.currentEntry.entry_date || ""
  });
  const basePath = window.location.pathname.startsWith("/picker")
    ? "/picker/link-drop"
    : "/link-drop";
  const popup = window.open(
    `${basePath}?${params.toString()}`,
    "project365-linked-photo-drop",
    "width=640,height=460"
  );
  if (!popup) {
    document.getElementById("crawlStatus").textContent =
      "Allow pop-ups for this page to open the linked-photo drop window.";
    return;
  }
  state.linkedPhotoDropEntryId = state.currentEntry.entry_id;
  monitorLinkedPhotoPopup(popup, state.currentEntry.entry_id);
  popup.focus();
  document.getElementById("crawlStatus").textContent =
    `Opened linked-photo drop window for ${state.currentEntry.entry_date}.`;
}

function monitorLinkedPhotoPopup(popup, entryId) {
  if (state.linkedPhotoPopupPoller) window.clearInterval(state.linkedPhotoPopupPoller);
  state.linkedPhotoPopupPoller = window.setInterval(() => {
    let closed = false;
    try {
      closed = Boolean(popup.closed);
    } catch (_) {
      closed = true;
    }
    if (!closed) return;
    window.clearInterval(state.linkedPhotoPopupPoller);
    state.linkedPhotoPopupPoller = null;
    if (state.linkedPhotoDropEntryId !== entryId) return;
    refreshLinkedPhotoEntry({
      entry_id: entryId,
      status: "Linked-photo window closed. Refreshing the entry.",
      stamp: `closed-${Date.now()}`
    }).catch(error => {
      document.getElementById("crawlStatus").textContent = error.message;
    });
  }, 500);
}

async function refreshLinkedPhotoEntry(message) {
  if (!message?.entry_id) return;
  const activeEntryId = state.currentEntry?.entry_id || state.selectedEntryId || "";
  if (!activeEntryId || activeEntryId !== message.entry_id) return;
  if (message.stamp) state.lastLinkedPhotoRefreshStamp = String(message.stamp);
  if (state.linkedPhotoDropEntryId === message.entry_id) state.linkedPhotoDropEntryId = "";
  if (state.linkedPhotoPopupPoller) {
    window.clearInterval(state.linkedPhotoPopupPoller);
    state.linkedPhotoPopupPoller = null;
  }
  state.entryDetailCache.delete(message.entry_id);
  document.getElementById("crawlStatus").textContent =
    message.status || "Linked photo received. Refreshing the entry.";
  try {
    await loadSummary();
    await loadEntry(message.entry_id);
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
  }
}

async function refreshLinkedPhotoEntryFromStorage() {
  let message = null;
  try {
    message = JSON.parse(localStorage.getItem(LINKED_PHOTO_REFRESH_KEY) || "null");
  } catch (_) {
    return;
  }
  if (!message?.entry_id || !message.stamp) return;
  if (String(message.stamp) === state.lastLinkedPhotoRefreshStamp) return;
  await refreshLinkedPhotoEntry(message);
}

async function handleLinkedDropWindowMessage(event) {
  if (event.origin !== window.location.origin) return;
  const message = event.data || {};
  if (message.type !== "project365-linked-photo") return;
  await refreshLinkedPhotoEntry(message);
}

function appendCrawlRoot(path) {
  const input = document.getElementById("crawlRoots");
  const roots = crawlRoots();
  if (!roots.includes(path)) {
    roots.push(path);
  }
  input.value = roots.join("; ");
}

async function startCrawl(scope) {
  const roots = crawlRoots();
  const entryIds = scope === "current" && state.currentEntry
    ? [state.currentEntry.entry_id]
    : scope === "selected"
    ? Array.from(state.selectedEntryIds)
    : state.entries.map(entry => entry.entry_id);
  if (!roots.length) {
    document.getElementById("crawlStatus").textContent = "Choose at least one search folder.";
    return;
  }
  if (!entryIds.length) {
    document.getElementById("crawlStatus").textContent = "No entries selected for crawl.";
    return;
  }
  const job = await fetchJson("/api/crawl", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      entry_ids: entryIds,
      search_roots: roots,
      scan_metadata_dates: true
    })
  });
  state.crawlJobId = job.id;
  renderCrawlJob(job);
  pollCrawlJob(job.id);
}

async function pollCrawlJob(jobId) {
  const job = await fetchJson(`/api/crawl/${encodeURIComponent(jobId)}`);
  renderCrawlJob(job);
  if (job.status === "queued" || job.status === "running") {
    setTimeout(() => pollCrawlJob(jobId), 1500);
    return;
  }
  await loadSummary();
  await loadEntries();
}

function renderCrawlJob(job) {
  const detail = job.error
    ? ` · ${job.error}`
    : ` · ${job.entry_count} entries · ${job.candidate_count || 0} candidates`;
  document.getElementById("crawlStatus").textContent = `${job.status}${detail}`;
}

function isFileDrag(event) {
  const transfer = event.dataTransfer;
  const types = Array.from(transfer?.types || []);
  const files = Array.from(transfer?.files || []);
  const items = Array.from(transfer?.items || []);
  return types.includes("Files") || files.length > 0 || items.length > 0;
}

function describePhotoDropTransfer(event) {
  const transfer = event.dataTransfer;
  const types = Array.from(transfer?.types || []).join(", ") || "none";
  const items = Array.from(transfer?.items || [])
    .map(item => `${item.kind}:${item.type || "unknown"}`)
    .join(", ") || "none";
  const files = Array.from(transfer?.files || [])
    .map(file => `${file.name || "unnamed"} (${file.type || "unknown"}, ${file.size || 0} bytes)`)
    .join(", ") || "none";
  return `This drag exposed: types=${types}; items=${items}; files=${files}.`;
}

function droppedFileIsVideo(file) {
  const type = String(file?.type || "").split(";", 1)[0].trim().toLowerCase();
  return /\\.(mov|mp4|m4v)$/i.test(file?.name || "") ||
    ["video/quicktime", "video/mp4", "video/x-m4v"].includes(type);
}

function droppedFileIsImage(file) {
  const type = String(file?.type || "").split(";", 1)[0].trim().toLowerCase();
  return type.startsWith("image/") ||
    /\\.(avif|avifs|bmp|gif|heic|heif|jpe?g|png|tiff?|webp)$/i.test(file?.name || "");
}

function droppedLivePhotoStillFile(files) {
  const images = files.filter(droppedFileIsImage);
  const videos = files.filter(droppedFileIsVideo);
  if (images.length === 1 && videos.length > 0) return images[0];
  return null;
}

function linkedPhotoDropFiles(files) {
  const livePhotoStill = droppedLivePhotoStillFile(files);
  if (livePhotoStill) return [livePhotoStill];
  const images = files.filter(droppedFileIsImage);
  if (images.length) return images;
  return files.length === 1 ? [files[0]] : [];
}

function isLinkDrag(event) {
  const transfer = event.dataTransfer;
  if (!transfer) return false;
  const types = Array.from(transfer.types || []);
  const files = Array.from(transfer.files || []);
  const items = Array.from(transfer.items || []);
  return types.length > 0 || files.length > 0 || items.length > 0;
}

function droppedLinkPath(event) {
  const transfer = event.dataTransfer;
  const text = [
    "text/uri-list",
    "text/plain",
    "text/x-moz-url",
    "URL"
  ].map(type => {
    try {
      return transfer?.getData(type) || "";
    } catch (_) {
      return "";
    }
  }).find(Boolean) || "";
  const line = text.split(/\\r?\\n/).map(value => value.trim()).find(value => value && !value.startsWith("#")) || "";
  if (line.startsWith("file://")) {
    try {
      return decodeURIComponent(new URL(line).pathname);
    } catch (_) {
      return "";
    }
  }
  if (line.startsWith("/")) return line;
  return transfer?.files?.[0]?.path || "";
}

function updatePhotoDropTarget() {
  const target = document.getElementById("photoDropTarget");
  const hint = document.getElementById("photoDropTargetHint");
  target.classList.toggle("disabled", !state.currentEntry);
  hint.textContent = state.currentEntry
    ? `Copies stills for ${state.currentEntry.entry_date}, or opens movie screenshots`
    : "Open an entry before dropping a photo";
}

async function linkCandidatePath(candidatePath, associate = false) {
  const entry = state.currentEntry;
  if (!entry) return;
  document.getElementById("crawlStatus").textContent =
    `Linking original photo to ${entry.entry_date}.`;
  const detail = await fetchJson("/api/link-candidate", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({entry_id: entry.entry_id, candidate_path: candidatePath, associate})
  });
  state.currentEntry = detail;
  const entrySummary = state.entries.find(item => item.entry_id === detail.entry_id);
  if (entrySummary) {
    entrySummary.candidate_count = detail.candidate_count;
    entrySummary.status = detail.status;
    entrySummary.associated_count = detail.associated_count;
    entrySummary.pending_commit_count = detail.pending_commit_count;
    entrySummary.commit_ready = detail.commit_ready;
  }
  renderEntries();
  renderEntryDetail();
  document.getElementById("crawlStatus").textContent =
    associate
      ? `Original photo linked as an associated attachment for ${entry.entry_date}.`
      : `Original photo linked for ${entry.entry_date}. Review it, then select it.`;
  scheduleSummaryRefresh();
}

async function importLinkedFile(file, entry) {
  document.getElementById("crawlStatus").textContent =
    `Copying dropped photo as an associated attachment for ${entry.entry_date}.`;
  const detail = await fetchJson("/api/import-linked-candidate", {
    method: "POST",
    headers: {
      "content-type": file.type || "application/octet-stream",
      "x-entry-id": encodeURIComponent(entry.entry_id),
      "x-file-name": encodeURIComponent(file.name)
    },
    body: file
  });
  state.entryDetailCache.delete(entry.entry_id);
  state.currentEntry = detail;
  const entrySummary = state.entries.find(item => item.entry_id === detail.entry_id);
  if (entrySummary) {
    entrySummary.candidate_count = detail.candidate_count;
    entrySummary.status = detail.status;
    entrySummary.associated_count = detail.associated_count;
    entrySummary.pending_commit_count = detail.pending_commit_count;
    entrySummary.commit_ready = detail.commit_ready;
  }
  renderEntries();
  renderEntryDetail();
  document.getElementById("crawlStatus").textContent =
    `Dropped photo linked as an associated attachment for ${entry.entry_date}.`;
  scheduleSummaryRefresh();
}

function showVideoFrameChooser(payload, entry) {
  const frames = Array.isArray(payload.frames) ? payload.frames : [];
  if (!frames.length) {
    document.getElementById("crawlStatus").textContent = "No screenshots were extracted from that movie.";
    return;
  }
  state.videoFramePendingToken = "";
  state.videoFrameReview = {
    payload,
    entry,
    currentEntryId: entry.entry_id,
    nextEntryId: nextEntryIdAfterCurrent()
  };
  const modal = document.getElementById("videoFrameModal");
  const grid = document.getElementById("videoFrameGrid");
  const player = document.getElementById("videoFramePlayer");
  document.getElementById("videoFrameSubtitle").textContent =
    `${payload.filename || "Dropped movie"} · ${videoFrameShotDateLabel(entry.entry_date, payload.capture_timestamp)}`;
  document.getElementById("videoFrameHelp").textContent =
    "Pick one main image, and any supplemental attachments. Click a screenshot to inspect it larger.";
  document.getElementById("videoFrameNoMain").checked = true;
  const duration = Math.max(0, Number(payload.duration_seconds || 0));
  const slider = document.getElementById("videoFrameSlider");
  const addButton = document.getElementById("videoFrameAddCustom");
  const saveButton = document.getElementById("videoFrameSave");
  const fallbackMax = Math.max(0, ...frames.map(frame => Number(frame.time_seconds || 0)));
  slider.max = String(duration || fallbackMax || 0);
  slider.value = "0";
  slider.disabled = Number(slider.max || 0) <= 0;
  addButton.disabled = slider.disabled;
  saveButton.disabled = false;
  player.src = payload.video_token ? `/image/${encodeURIComponent(payload.video_token)}` : "";
  player.onloadedmetadata = syncVideoFrameDurationFromPlayer;
  player.ontimeupdate = syncVideoFrameSliderToPlayer;
  player.onplay = updateVideoFramePlayPauseButton;
  player.onpause = updateVideoFramePlayPauseButton;
  player.onended = updateVideoFramePlayPauseButton;
  if (player.src && !slider.disabled) player.currentTime = Number(slider.value || 0);
  updateVideoFrameSliderLabel();
  updateVideoFramePlayPauseButton();
  reviewSortVideoFrames(state.videoFrameReview);
  renderVideoFrameGrid();
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
  document.getElementById("crawlStatus").textContent =
    `Choose screenshots from ${payload.filename || "the dropped movie"}.`;
}

function showVideoFrameProcessing(entry, file) {
  const token = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  state.videoFramePendingToken = token;
  state.videoFrameReview = null;
  const modal = document.getElementById("videoFrameModal");
  const grid = document.getElementById("videoFrameGrid");
  const player = document.getElementById("videoFramePlayer");
  const slider = document.getElementById("videoFrameSlider");
  document.getElementById("videoFrameSubtitle").textContent =
    `${file?.name || "Dropped movie"} · ${entry.entry_date}`;
  document.getElementById("videoFrameHelp").textContent =
    "Preparing movie screenshots. Large videos can take a while; this window will fill in when frames are ready.";
  document.getElementById("videoFrameNoMain").checked = true;
  player.pause();
  player.removeAttribute("src");
  player.load();
  slider.max = "0";
  slider.value = "0";
  slider.disabled = true;
  updateVideoFrameSliderLabel();
  document.getElementById("videoFrameAddCustom").disabled = true;
  document.getElementById("videoFrameSave").disabled = true;
  updateVideoFramePlayPauseButton();
  grid.innerHTML = '<div class="video-frame-processing">Preparing movie screenshots…</div>';
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
  document.getElementById("crawlStatus").textContent =
    `Preparing screenshots from ${file?.name || "the dropped movie"}.`;
  return token;
}

function videoFrameCardHtml(frame) {
  const imageUrl = `/image/${encodeURIComponent(frame.token)}?max=640`;
  const fullUrl = `/image/${encodeURIComponent(frame.token)}`;
  const customClass = frame.custom ? " custom-frame" : "";
  const title = frame.custom ? "Captured frame" : "";
  return `
    <article class="video-frame-card${customClass}" data-preview-url="${escapeHtml(fullUrl)}" data-preview-title="${escapeHtml(frame.label || "")}" title="${escapeHtml(title)}">
      <button class="image-preview-trigger" type="button" onclick="openVideoFramePreview(this)" data-preview-url="${escapeHtml(fullUrl)}" data-preview-title="${escapeHtml(frame.label || "")}" aria-label="Open larger screenshot ${escapeHtml(frame.label || "")}">
        <img src="${escapeHtml(imageUrl)}" alt="">
      </button>
      <div class="video-frame-controls">
        <strong>${escapeHtml(frame.label || "")}</strong>
        <label><input type="radio" name="videoFrameMain" value="${escapeHtml(frame.frame_id)}"> Main</label>
        <label><input type="checkbox" data-video-supplemental-frame="${escapeHtml(frame.frame_id)}"> Link</label>
      </div>
    </article>
  `;
}

function reviewSortVideoFrames(review) {
  if (!review?.payload) return;
  review.payload.frames = [...(review.payload.frames || [])].sort((left, right) => {
    const timeCompare = Number(left.time_seconds || 0) - Number(right.time_seconds || 0);
    if (timeCompare) return timeCompare;
    const leftCustom = left.custom || String(left.frame_id || "").startsWith("custom-") ? 1 : 0;
    const rightCustom = right.custom || String(right.frame_id || "").startsWith("custom-") ? 1 : 0;
    if (leftCustom !== rightCustom) return leftCustom - rightCustom;
    return String(left.frame_id || "").localeCompare(String(right.frame_id || ""));
  });
}

function currentVideoFrameSelection() {
  return {
    mainFrame: document.querySelector('input[name="videoFrameMain"]:checked')?.value || "",
    supplementalFrames: new Set(
      Array.from(document.querySelectorAll("[data-video-supplemental-frame]:checked"))
        .map(input => input.dataset.videoSupplementalFrame)
        .filter(Boolean)
    )
  };
}

function restoreVideoFrameSelection(selection) {
  const mainInput = document.querySelector(`input[name="videoFrameMain"][value="${CSS.escape(selection.mainFrame || "")}"]`);
  if (mainInput) {
    mainInput.checked = true;
  } else {
    document.getElementById("videoFrameNoMain").checked = true;
  }
  document.querySelectorAll("[data-video-supplemental-frame]").forEach(input => {
    input.checked = selection.supplementalFrames.has(input.dataset.videoSupplementalFrame || "");
  });
}

function renderVideoFrameGrid(selection = currentVideoFrameSelection()) {
  const review = state.videoFrameReview;
  const grid = document.getElementById("videoFrameGrid");
  reviewSortVideoFrames(review);
  grid.innerHTML = (review?.payload?.frames || []).map(videoFrameCardHtml).join("");
  restoreVideoFrameSelection(selection);
}

function videoFrameTimeKey(timeSeconds) {
  const value = Number(timeSeconds || 0);
  return Number.isFinite(value) ? value.toFixed(3) : "0.000";
}

function videoFrameShotDateLabel(entryDate, captureTimestamp) {
  const entryDateText = String(entryDate || "").trim();
  const timestamp = String(captureTimestamp || "").trim();
  if (!timestamp) return entryDateText;
  const normalized = timestamp.replace("T", " ");
  if (entryDateText && normalized.startsWith(`${entryDateText} `)) return normalized.slice(0, 19);
  return normalized.slice(0, 19);
}

function formatVideoFrameTime(timeSeconds) {
  const total = Math.max(0, Math.round(Number(timeSeconds || 0)));
  const seconds = total % 60;
  const minutesTotal = Math.floor(total / 60);
  const minutes = minutesTotal % 60;
  const hours = Math.floor(minutesTotal / 60);
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function updateVideoFrameSliderLabel() {
  const slider = document.getElementById("videoFrameSlider");
  document.getElementById("videoFrameSliderLabel").textContent = formatVideoFrameTime(slider.value);
}

function syncVideoFrameDurationFromPlayer() {
  const player = document.getElementById("videoFramePlayer");
  const slider = document.getElementById("videoFrameSlider");
  const duration = Number.isFinite(player.duration) ? Math.max(0, player.duration) : 0;
  if (duration > 0 && (!Number(slider.max) || Number(slider.max) < duration)) {
    slider.max = String(duration);
    slider.disabled = false;
    document.getElementById("videoFrameAddCustom").disabled = false;
  }
  syncVideoFrameSliderToPlayer();
}

function syncVideoFrameSliderToPlayer() {
  const player = document.getElementById("videoFramePlayer");
  const slider = document.getElementById("videoFrameSlider");
  if (!slider.matches(":active") && Number.isFinite(player.currentTime)) {
    slider.value = String(Math.min(Number(slider.max || player.currentTime), player.currentTime));
  }
  updateVideoFrameSliderLabel();
}

function updateVideoFramePlayPauseButton() {
  const player = document.getElementById("videoFramePlayer");
  const button = document.getElementById("videoFramePlayPause");
  if (!player || !button) return;
  const canPlay = Boolean(player.src);
  button.disabled = !canPlay;
  button.textContent = player.paused || player.ended ? "Play" : "Pause";
  button.setAttribute("aria-pressed", player.paused || player.ended ? "false" : "true");
}

function toggleVideoFramePlayback() {
  const player = document.getElementById("videoFramePlayer");
  if (!player?.src) return;
  if (player.paused || player.ended) {
    player.play().catch(() => {});
  } else {
    player.pause();
  }
  updateVideoFramePlayPauseButton();
}

function scrubVideoFrameSlider() {
  const player = document.getElementById("videoFramePlayer");
  const slider = document.getElementById("videoFrameSlider");
  const time = Number(slider.value || 0);
  if (player.src && Number.isFinite(time)) player.currentTime = time;
  updateVideoFrameSliderLabel();
}

function stepVideoFrameTime(deltaSeconds) {
  const player = document.getElementById("videoFramePlayer");
  const slider = document.getElementById("videoFrameSlider");
  const max = Number(slider.max || 0);
  const current = player.src && Number.isFinite(player.currentTime) ? player.currentTime : Number(slider.value || 0);
  const next = Math.max(0, Math.min(max || current + deltaSeconds, current + deltaSeconds));
  slider.value = String(next);
  if (player.src) player.currentTime = next;
  updateVideoFrameSliderLabel();
}

function captureCurrentVideoFrame() {
  const player = document.getElementById("videoFramePlayer");
  const slider = document.getElementById("videoFrameSlider");
  const time = player.src && Number.isFinite(player.currentTime) ? player.currentTime : Number(slider.value || 0);
  return addCustomVideoFrameAtTime(time);
}

function openVideoFramePreview(trigger) {
  openImagePreview(
    trigger.dataset.previewUrl || "",
    trigger.dataset.previewTitle || "Movie screenshot",
    ""
  );
}

async function addCustomVideoFrameAtTime(timeSeconds) {
  const review = state.videoFrameReview;
  if (!review) return;
  const requestedKey = videoFrameTimeKey(timeSeconds);
  const duplicateFrame = (review.payload.frames || []).find(frame => videoFrameTimeKey(frame.time_seconds) === requestedKey);
  if (duplicateFrame) {
    const message = `Screenshot ${duplicateFrame.label || formatVideoFrameTime(timeSeconds)} is already in the grid. Move slightly before capturing again.`;
    document.getElementById("videoFrameHelp").textContent = message;
    document.getElementById("crawlStatus").textContent = message;
    return;
  }
  const button = document.getElementById("videoFrameAddCustom");
  button.disabled = true;
  document.getElementById("crawlStatus").textContent = "Adding movie screenshot.";
  try {
    const payload = await fetchJson("/api/extract-video-frame", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: review.currentEntryId,
        session_id: review.payload.session_id,
        time_seconds: Number(timeSeconds || 0)
      })
    });
    const frame = payload.frame;
    const selection = currentVideoFrameSelection();
    review.payload.frames = [...(review.payload.frames || []), frame];
    renderVideoFrameGrid(selection);
    document.getElementById("videoFrameHelp").textContent =
      "Pick one main image, and any supplemental attachments. Click a screenshot to inspect it larger.";
    document.getElementById("crawlStatus").textContent = `Added screenshot at ${frame.label || formatVideoFrameTime(timeSeconds)}.`;
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
  } finally {
    button.disabled = document.getElementById("videoFrameSlider").disabled;
  }
}

async function discardVideoFrameSession(review) {
  const payload = review?.payload || {};
  if (!review?.currentEntryId || !payload.session_id) return;
  try {
    await fetchJson("/api/discard-video-frames", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: review.currentEntryId,
        session_id: payload.session_id
      })
    });
  } catch (_) {
  }
}

function closeVideoFrameChooser({discard = true} = {}) {
  const review = state.videoFrameReview;
  state.videoFramePendingToken = "";
  const modal = document.getElementById("videoFrameModal");
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
  const player = document.getElementById("videoFramePlayer");
  player.pause();
  player.removeAttribute("src");
  player.load();
  state.videoFrameReview = null;
  if (discard) void discardVideoFrameSession(review);
}

async function saveVideoFrameChoices() {
  const review = state.videoFrameReview;
  if (!review) return;
  const mainFrame = document.querySelector('input[name="videoFrameMain"]:checked')?.value || "";
  const supplementalFrames = Array.from(document.querySelectorAll("[data-video-supplemental-frame]:checked"))
    .map(input => input.dataset.videoSupplementalFrame)
    .filter(Boolean);
  if (!mainFrame && !supplementalFrames.length) {
    document.getElementById("crawlStatus").textContent = "Choose at least one screenshot.";
    return;
  }
  const saveButton = document.getElementById("videoFrameSave");
  saveButton.disabled = true;
  const originalSaveLabel = saveButton.textContent;
  saveButton.textContent = "Saving...";
  document.getElementById("crawlStatus").textContent = "Saving selected movie screenshots.";
  document.getElementById("videoFrameHelp").textContent = "Saving selected movie screenshots.";
  try {
    const detail = await fetchJson("/api/import-video-frames", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: review.currentEntryId,
        session_id: review.payload.session_id,
        main_frame_id: mainFrame,
        supplemental_frame_ids: supplementalFrames
      })
    });
    closeVideoFrameChooser({discard: false});
    state.entryDetailCache.delete(review.currentEntryId);
    state.currentEntry = detail;
    const entrySummary = state.entries.find(item => item.entry_id === detail.entry_id);
    if (entrySummary) {
      entrySummary.candidate_count = detail.candidate_count;
      entrySummary.status = detail.status;
      entrySummary.selected_count = detail.selected_count;
      entrySummary.pending_commit_count = detail.pending_commit_count;
      entrySummary.commit_ready = detail.commit_ready;
      entrySummary.accepted_count = detail.accepted_count;
      entrySummary.associated_count = detail.associated_count;
      entrySummary.rejected_count = detail.rejected_count;
      entrySummary.fallback_count = detail.fallback_count;
    }
    if (mainFrame) {
      renderEntries();
      renderEntryDetail();
      document.getElementById("crawlStatus").textContent =
        `Movie screenshot accepted for ${review.entry.entry_date}.`;
    } else {
      renderEntries();
      renderEntryDetail();
      document.getElementById("crawlStatus").textContent =
        `Movie screenshots flagged as supplemental for ${review.entry.entry_date}.`;
    }
    scheduleSummaryRefresh();
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
    document.getElementById("videoFrameHelp").textContent = error.message;
  } finally {
    saveButton.textContent = originalSaveLabel;
    saveButton.disabled = false;
  }
}

async function handlePhotoDrop(event) {
  event.preventDefault();
  photoDropDragDepth = 0;
  event.currentTarget.classList.remove("active");
  const entry = state.currentEntry;
  const files = Array.from(event.dataTransfer?.files || []);
  if (!entry) {
    document.getElementById("crawlStatus").textContent = "Open an entry before dropping a photo.";
    return;
  }
  if (files.length !== 1) {
    const livePhotoStill = droppedLivePhotoStillFile(files);
    if (livePhotoStill) {
      await importDroppedPhotoFile(livePhotoStill, entry);
      return;
    }
    document.getElementById("crawlStatus").textContent =
      `Safari did not expose one usable photo or movie file for this drop. ` +
      `Try Open drop window, or export from Photos to Finder first. ${describePhotoDropTransfer(event)}`;
    return;
  }
  await importDroppedPhotoFile(files[0], entry);
}

async function importDroppedPhotoFile(file, entry) {
  const currentEntryId = entry.entry_id;
  const nextEntryId = nextEntryIdAfterCurrent();
  const isVideoDrop = droppedFileIsVideo(file);
  const videoDropToken = isVideoDrop ? showVideoFrameProcessing(entry, file) : "";
  document.getElementById("crawlStatus").textContent =
    isVideoDrop ? `Preparing screenshots from ${file.name || "the dropped movie"}.` : `Copying dropped photo for ${entry.entry_date}.`;
  try {
    const detail = await fetchJson("/api/import-dropped-candidate", {
      method: "POST",
      headers: {
        "content-type": file.type || "application/octet-stream",
        "x-entry-id": encodeURIComponent(entry.entry_id),
        "x-file-name": encodeURIComponent(file.name)
      },
      body: file
    });
    if (detail.kind === "video_frame_choices") {
      if (state.videoFramePendingToken !== videoDropToken) {
        await discardVideoFrameSession({
          currentEntryId,
          payload: {session_id: detail.session_id}
        });
        return;
      }
      showVideoFrameChooser(detail, entry);
      return;
    }
    state.entryDetailCache.delete(currentEntryId);
    rememberPreviousEntry(entry);
    const index = state.entries.findIndex(item => item.entry_id === currentEntryId);
    if (index >= 0) state.entries.splice(index, 1);
    renderEntries();
    document.getElementById("crawlStatus").textContent =
      `Photo copied and accepted for ${entry.entry_date}.`;
    if (nextEntryId) await loadEntry(nextEntryId);
    else await loadEntries();
    scheduleSummaryRefresh();
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
  }
}

async function handleLinkDrop(event) {
  event.preventDefault();
  photoLinkDragDepth = 0;
  linkDropTarget.classList.remove("active");
  if (!state.currentEntry) {
    document.getElementById("crawlStatus").textContent = "Open an entry before linking a photo.";
    return;
  }
  const entry = state.currentEntry;
  const files = Array.from(event.dataTransfer?.files || []);
  const filesToImport = linkedPhotoDropFiles(files);
  const candidatePath = droppedLinkPath(event);
  if (!candidatePath && !filesToImport.length) {
    const types = Array.from(event.dataTransfer?.types || []).join(", ") || "none";
    document.getElementById("crawlStatus").textContent =
      `Drop one or more image files, or a local file path. This drag exposed: ${types}.`;
    return;
  }
  try {
    if (filesToImport.length > 1) {
      document.getElementById("crawlStatus").textContent =
        `Copying ${filesToImport.length} dropped photos as associated attachments for ${entry.entry_date}.`;
      for (const file of filesToImport) await importLinkedFile(file, entry);
      document.getElementById("crawlStatus").textContent =
        `Dropped ${filesToImport.length} photos linked as associated attachments for ${entry.entry_date}.`;
    } else if (candidatePath) {
      await linkCandidatePath(candidatePath, true);
    } else {
      await importLinkedFile(filesToImport[0], entry);
    }
  } catch (error) {
    document.getElementById("crawlStatus").textContent = error.message;
  }
}

function shouldHandleFallbackLinkDrop(event) {
  if (!state.currentEntry) return false;
  const target = event.target;
  if (target?.closest?.("#photoDropTarget")) return false;
  if (target?.closest?.("#sourceFrame")) return false;
  if (target?.closest?.("input, textarea, select, button, a")) return false;
  return Boolean(target?.closest?.(".workspace, .main, #photoLinkDropTarget"));
}

function activateFallbackLinkDrop(event) {
  if (!shouldHandleFallbackLinkDrop(event)) return;
  event.preventDefault();
  linkDropTarget.classList.add("active");
  if (event.dataTransfer) event.dataTransfer.dropEffect = "link";
}

function clearFallbackLinkDrop(event) {
  if (!shouldHandleFallbackLinkDrop(event)) return;
  linkDropTarget.classList.remove("active");
}

async function handleFallbackLinkDrop(event) {
  if (!shouldHandleFallbackLinkDrop(event)) return;
  await handleLinkDrop(event);
}

const dropTarget = document.getElementById("photoDropTarget");
const linkDropTarget = document.getElementById("photoLinkDropTarget");
let photoDropDragDepth = 0;
let photoLinkDragDepth = 0;
document.getElementById("loadMoreEntries").onclick = () => {
  loadMoreEntries().catch(error => {
    document.getElementById("summary").textContent = error.message;
  });
};
document.getElementById("loadAllEntries").onclick = () => {
  setEntryLoadAllUrl(true);
  loadMoreEntries(true).catch(error => {
    document.getElementById("summary").textContent = error.message;
  });
};
dropTarget.addEventListener("dragenter", event => {
  event.preventDefault();
  photoDropDragDepth += 1;
  dropTarget.classList.add("active");
});
dropTarget.addEventListener("dragover", event => {
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
});
dropTarget.addEventListener("dragleave", event => {
  photoDropDragDepth = Math.max(0, photoDropDragDepth - 1);
  if (photoDropDragDepth === 0) dropTarget.classList.remove("active");
});
dropTarget.addEventListener("dragend", () => {
  photoDropDragDepth = 0;
  dropTarget.classList.remove("active");
});
dropTarget.addEventListener("drop", handlePhotoDrop);
linkDropTarget.addEventListener("dragenter", event => {
  event.preventDefault();
  photoLinkDragDepth += 1;
  linkDropTarget.classList.add("active");
});
linkDropTarget.addEventListener("dragover", event => {
  event.preventDefault();
  event.dataTransfer.dropEffect = "link";
});
linkDropTarget.addEventListener("dragleave", event => {
  photoLinkDragDepth = Math.max(0, photoLinkDragDepth - 1);
  if (photoLinkDragDepth === 0) linkDropTarget.classList.remove("active");
});
linkDropTarget.addEventListener("dragend", () => {
  photoLinkDragDepth = 0;
  linkDropTarget.classList.remove("active");
});
linkDropTarget.addEventListener("drop", handleLinkDrop);
document.addEventListener("dragover", activateFallbackLinkDrop, true);
document.addEventListener("dragleave", clearFallbackLinkDrop, true);
document.addEventListener("drop", handleFallbackLinkDrop, true);
window.addEventListener("message", handleLinkedDropWindowMessage);
window.addEventListener("storage", event => {
  if (event.key === LINKED_PHOTO_REFRESH_KEY) {
    refreshLinkedPhotoEntryFromStorage().catch(error => {
      document.getElementById("crawlStatus").textContent = error.message;
    });
  }
});
window.addEventListener("focus", () => {
  refreshLinkedPhotoEntryFromStorage().catch(error => {
    document.getElementById("crawlStatus").textContent = error.message;
  });
});
document.addEventListener("keydown", handlePickerKeyboardShortcut);

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[char]));
}

function applyUrlFilters() {
  const params = new URLSearchParams(window.location.search);
  const status = params.get("status");
  if (status) {
    const filter = document.getElementById("filter");
    if ([...filter.options].some(option => option.value === status)) {
      filter.value = status;
    }
  }
  state.urlEntryIds = params.getAll("entry_id").flatMap(splitFilterValue);
  state.urlEntryDates = params.getAll("entry_date").flatMap(splitFilterValue);
  state.initialBatchId = params.get("batch") || "";
  state.entryLoadAll = params.get("load_all") === "1";
  const scopedDateLabel = entryDateScopeLabel(state.urlEntryDates);
  syncEntryDateJumpControl(scopedDateLabel ? `Showing ${scopedDateLabel}.` : "");
  if (params.has("photo_index_folder")) {
    setActivePhotoIndexFolder(params.get("photo_index_folder") || "");
  }
}

function splitFilterValue(value) {
  return String(value)
    .split(";")
    .map(item => item.trim())
    .filter(Boolean);
}

applyUrlFilters();
loadBatches().then(loadEntries).then(loadSummary).catch(error => {
  document.getElementById("summary").textContent = error.message;
});
</script>
</body>
</html>
"""


def link_drop_html(api_prefix: str = "/api") -> str:
    return LINK_DROP_HTML.replace("__API_PREFIX__", api_prefix.rstrip("/"))


LINK_DROP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 linked photo drop</title>
<style>
:root {
  color-scheme: light;
  --bg: #f8fafc;
  --ink: #111827;
  --muted: #475569;
  --line: #cbd5e1;
  --accent: #2563eb;
  --good: #166534;
  --bad: #991b1b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  padding: 18px;
}
main {
  display: grid;
  grid-template-rows: auto minmax(190px, 1fr) auto;
  gap: 12px;
  width: min(100%, 620px);
  max-height: min(92vh, 460px);
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  box-shadow: 0 14px 36px rgba(15, 23, 42, 0.14);
}
.title {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: baseline;
}
h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
}
.entry {
  color: var(--muted);
  font-size: 13px;
}
.drop-zone {
  border: 2px dashed var(--line);
  border-radius: 8px;
  display: grid;
  place-items: center;
  min-height: 190px;
  background: #f8fafc;
  text-align: center;
  padding: 20px;
}
.drop-zone.active {
  border-color: var(--accent);
  background: #eff6ff;
}
.drop-copy {
  display: grid;
  gap: 8px;
  max-width: 480px;
}
.drop-copy strong {
  font-size: 20px;
}
.drop-copy span {
  color: var(--muted);
}
.status {
  min-height: 44px;
  color: var(--muted);
  white-space: pre-wrap;
}
.status.good { color: var(--good); }
.status.bad { color: var(--bad); }
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
button {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: white;
  color: var(--ink);
  padding: 8px 12px;
  font: inherit;
}
</style>
</head>
<body>
<main>
  <div class="title">
    <h1>Linked photo drop</h1>
    <div id="entryLabel" class="entry"></div>
  </div>
  <div id="dropZone" class="drop-zone">
    <div class="drop-copy">
      <strong>Drop the linked photo here</strong>
      <span>This window links the dropped item as an associated attachment for the current Project365 entry.</span>
    </div>
  </div>
  <div id="status" class="status">Waiting for a Photos drag.</div>
  <div class="actions">
    <button id="closeWindow" type="button">Close</button>
  </div>
</main>
<script>
const API_PREFIX = "__API_PREFIX__";
const params = new URLSearchParams(window.location.search);
const entryId = params.get("entry_id") || "";
const entryDate = params.get("entry_date") || "";
const dropZone = document.getElementById("dropZone");
const status = document.getElementById("status");
document.getElementById("entryLabel").textContent = entryDate || entryId || "No entry selected";
document.getElementById("closeWindow").onclick = () => window.close();

function setStatus(message, kind = "") {
  status.textContent = message;
  status.className = `status ${kind}`.trim();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (_) {
      payload = {error: text};
    }
  }
  if (!response.ok) {
    throw new Error(payload.error || payload.message || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function transferTypes(transfer) {
  return Array.from(transfer?.types || []);
}

function transferItems(transfer) {
  return Array.from(transfer?.items || []).map(item => `${item.kind}:${item.type || "unknown"}`);
}

function describeTransfer(transfer) {
  const files = Array.from(transfer?.files || []);
  const names = files.map(file => `${file.name || "unnamed"} (${file.type || "unknown"}, ${file.size || 0} bytes)`);
  return [
    `Types: ${transferTypes(transfer).join(", ") || "none"}`,
    `Items: ${transferItems(transfer).join(", ") || "none"}`,
    `Files: ${names.join(", ") || "none"}`
  ].join("\\n");
}

function droppedFileIsVideo(file) {
  const type = String(file?.type || "").split(";", 1)[0].trim().toLowerCase();
  return /\\.(mov|mp4|m4v)$/i.test(file?.name || "") ||
    ["video/quicktime", "video/mp4", "video/x-m4v"].includes(type);
}

function droppedFileIsImage(file) {
  const type = String(file?.type || "").split(";", 1)[0].trim().toLowerCase();
  return type.startsWith("image/") ||
    /\\.(avif|avifs|bmp|gif|heic|heif|jpe?g|png|tiff?|webp)$/i.test(file?.name || "");
}

function droppedLivePhotoStillFile(files) {
  const images = files.filter(droppedFileIsImage);
  const videos = files.filter(droppedFileIsVideo);
  if (images.length === 1 && videos.length > 0) return images[0];
  return null;
}

function linkedPhotoDropFiles(files) {
  const livePhotoStill = droppedLivePhotoStillFile(files);
  if (livePhotoStill) return [livePhotoStill];
  const images = files.filter(droppedFileIsImage);
  if (images.length) return images;
  return files.length === 1 ? [files[0]] : [];
}

function droppedLinkPath(transfer) {
  const text = [
    "text/uri-list",
    "text/plain",
    "text/x-moz-url",
    "URL"
  ].map(type => {
    try {
      return transfer?.getData(type) || "";
    } catch (_) {
      return "";
    }
  }).find(Boolean) || "";
  const line = text.split(/\\r?\\n/).map(value => value.trim()).find(value => value && !value.startsWith("#")) || "";
  if (line.startsWith("file://")) {
    try {
      return decodeURIComponent(new URL(line).pathname);
    } catch (_) {
      return "";
    }
  }
  if (line.startsWith("/")) return line;
  return "";
}

function notifyParent(detail, message) {
  const payload = {
    type: "project365-linked-photo",
    entry_id: detail.entry_id || entryId,
    status: message,
    stamp: String(Date.now())
  };
  try {
    localStorage.setItem("project365-linked-photo-refresh", JSON.stringify(payload));
  } catch (_) {
  }
  if (!window.opener) return;
  try {
    window.opener.postMessage(payload, window.location.origin);
  } catch (_) {
  }
}

async function importLinkedFile(file) {
  return fetchJson(`${API_PREFIX}/import-linked-candidate`, {
    method: "POST",
    headers: {
      "content-type": file.type || "application/octet-stream",
      "x-entry-id": encodeURIComponent(entryId),
      "x-file-name": encodeURIComponent(file.name || "linked-photo")
    },
    body: file
  });
}

async function linkCandidatePath(candidatePath) {
  return fetchJson(`${API_PREFIX}/link-candidate`, {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({
      entry_id: entryId,
      candidate_path: candidatePath,
      associate: true
    })
  });
}

async function handleDrop(event) {
  event.preventDefault();
  event.stopPropagation();
  dropZone.classList.remove("active");
  if (!entryId) {
    setStatus("No Project365 entry was provided to this drop window.", "bad");
    return;
  }
  const transfer = event.dataTransfer;
  const files = Array.from(transfer?.files || []);
  const filesToImport = linkedPhotoDropFiles(files);
  const candidatePath = droppedLinkPath(transfer);
  setStatus(`Received drop.\\n${describeTransfer(transfer)}`);
  try {
    let detail = null;
    if (filesToImport.length) {
      setStatus(
        `Copying ${filesToImport.length} linked photo${filesToImport.length === 1 ? "" : "s"}.\\n${describeTransfer(transfer)}`
      );
      for (const file of filesToImport) detail = await importLinkedFile(file);
    } else if (candidatePath) {
      detail = await linkCandidatePath(candidatePath);
    } else {
      setStatus(
        `Safari did not expose a usable file or local path for this Photos drag.\\n${describeTransfer(transfer)}`,
        "bad"
      );
      return;
    }
    const count = filesToImport.length || 1;
    const message = count === 1
      ? `Linked as associated attachment for ${detail.entry_date || entryDate || entryId}.`
      : `Linked ${count} associated attachments for ${detail.entry_date || entryDate || entryId}.`;
    setStatus(message, "good");
    notifyParent(detail, message);
  } catch (error) {
    setStatus(error.message, "bad");
  }
}

function activateDrop(event) {
  event.preventDefault();
  event.stopPropagation();
  dropZone.classList.add("active");
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
}

function clearDrop() {
  dropZone.classList.remove("active");
}

dropZone.addEventListener("dragenter", activateDrop);
dropZone.addEventListener("dragover", activateDrop);
dropZone.addEventListener("dragleave", clearDrop);
dropZone.addEventListener("drop", handleDrop);
window.addEventListener("dragenter", activateDrop, true);
window.addEventListener("dragover", activateDrop, true);
window.addEventListener("drop", handleDrop, true);
document.addEventListener("dragover", activateDrop);
document.addEventListener("drop", handleDrop);
</script>
</body>
</html>
"""


CROP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 crop confirmation</title>
<style>
:root {
  color-scheme: light;
  --bg: #f4f6f8;
  --panel: #ffffff;
  --line: #d0d5dd;
  --text: #101828;
  --subtle: #667085;
  --accent: #2563eb;
  --danger: #b42318;
}
html {
  height: 100vh;
  overflow: hidden;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  overflow: hidden;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}
header {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: flex-start;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
h1 {
  margin: 0;
  font-size: 20px;
  font-weight: 650;
}
.control-panel-link {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 6px;
  min-height: 34px;
  padding: 0 10px;
  background: #fff;
  color: var(--accent);
  text-decoration: none;
  font-weight: 600;
  white-space: nowrap;
}
main {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 12px;
  padding: 12px;
  min-height: 0;
  overflow: hidden;
}
.sidebar,
.workspace {
  min-height: 0;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.sidebar {
  display: flex;
  flex-direction: column;
}
.summary {
  padding: 12px;
  border-bottom: 1px solid var(--line);
  color: var(--subtle);
  font-size: 13px;
}
.sidebar-controls {
  display: grid;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--line);
}
.sidebar-controls label {
  display: grid;
  gap: 4px;
  color: var(--subtle);
  font-size: 12px;
}
.sidebar-controls select,
.sidebar-controls input {
  width: 100%;
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  padding: 0 8px;
  font: inherit;
}
.date-scope-controls {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.date-scope-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.date-scope-actions button {
  min-height: 32px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  font: inherit;
  cursor: pointer;
}
.entry-list {
  overflow: auto;
}
.tree-group {
  border-bottom: 1px solid #eaecf0;
}
.tree-group summary {
  cursor: pointer;
  padding: 10px 12px;
  font-weight: 650;
  user-select: none;
}
.tree-group.month summary {
  padding-left: 24px;
  color: var(--subtle);
  font-size: 13px;
}
.tree-count {
  color: var(--subtle);
  font-weight: 500;
}
.entry-item {
  width: 100%;
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  padding-left: 36px;
  border: 0;
  border-bottom: 1px solid #eaecf0;
  background: #fff;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.entry-item.active {
  background: #eff6ff;
}
.entry-item img {
  width: 52px;
  height: 52px;
  object-fit: cover;
  border-radius: 6px;
  background: #eaecf0;
}
.entry-date {
  font-weight: 650;
}
.entry-meta {
  margin-top: 3px;
  color: var(--subtle);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.people-script {
  display: block;
  margin-top: 2px;
  color: var(--subtle);
  font-size: 11px;
  font-style: italic;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.entry-crop-status {
  display: inline-block;
  margin-top: 4px;
  color: var(--subtle);
  font-size: 11px;
}
.entry-crop-status.missing {
  color: var(--danger);
}
.workspace {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
}
.toolbar {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
  align-items: center;
  padding: 12px;
  border-bottom: 1px solid var(--line);
}
.heading {
  min-width: 0;
}
.heading strong {
  display: block;
  font-size: 16px;
}
.heading span {
  display: block;
  color: var(--subtle);
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.controls {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: start;
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
}
.control-group {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  min-height: 42px;
  white-space: nowrap;
}
.control-group + .control-group {
  padding-left: 8px;
  border-left: 1px solid var(--line);
}
.item-actions button {
  min-height: 38px;
  white-space: nowrap;
}
#suggestCropButton,
#resetCropButton,
#saveCropButton {
  min-width: 104px;
}
#commitCropButton {
  min-width: 236px;
}
button {
  appearance: none;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--text);
  border-radius: 6px;
  padding: 8px 10px;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
button.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.pending-count {
  display: inline-block;
  margin-left: 6px;
  min-width: 62px;
  font-size: 11px;
  font-weight: 600;
  opacity: .8;
}
button:disabled {
  cursor: not-allowed;
  opacity: .55;
}
button.subtle-danger {
  justify-self: end;
  border-color: transparent;
  background: transparent;
  color: #9b4a42;
  padding: 4px 2px;
  font-size: 11px;
  font-weight: 500;
}
button.subtle-danger:hover {
  border-color: #f1b8b8;
  background: #fff7f7;
  color: var(--danger);
}
.icon-control {
  width: 34px;
  height: 34px;
  padding: 0;
  font-size: 16px;
  line-height: 1;
}
.minimal-crop-control {
  min-height: 34px;
  padding: 6px 7px;
  color: var(--subtle);
  font-size: 12px;
  font-weight: 600;
}
.minimal-crop-control:not(:disabled) {
  color: var(--text);
}
.crop-size-control,
.crop-rotation-control {
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--subtle);
  font-size: 13px;
}
.crop-size-control input,
.crop-rotation-control input {
  width: 165px;
}
.crop-size-slider-frame {
  position: relative;
  display: inline-flex;
  align-items: center;
  width: 165px;
  padding-bottom: 8px;
  margin-bottom: -8px;
}
.crop-size-slider-frame input {
  width: 100%;
}
.crop-size-slider-frame.fill-active input {
  accent-color: var(--danger);
}
.crop-size-slider-frame::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 5px;
  border-radius: 999px;
  background: var(--danger);
  box-shadow: 0 0 0 1px #fff, 0 1px 4px rgba(180, 35, 24, .45);
  opacity: 0;
  pointer-events: none;
}
.crop-size-slider-frame.fill-active::after {
  opacity: 1;
}
.crop-fill-indicator {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 5px;
  border-radius: 999px;
  background: var(--danger);
  opacity: 0;
  pointer-events: none;
}
.crop-fill-indicator.active {
  opacity: 1;
}
.crop-rotation-control output {
  min-width: 42px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.compare {
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(220px, 320px) minmax(0, 1fr);
  gap: 14px;
  padding: 14px;
  overflow: hidden;
}
.photo-pane {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 8px;
}
.photo-title {
  min-width: 0;
}
.photo-title strong {
  display: block;
}
.photo-title span {
  display: block;
  color: var(--subtle);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.photo-title .people-script {
  font-size: 12px;
}
.photo-frame {
  min-height: 0;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  border: 1px solid #d0d5dd;
  border-radius: 8px;
  background: #0f172a;
  overflow: hidden;
}
.photo-frame img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  display: block;
}
.target-pane {
  align-content: start;
  grid-template-rows: auto auto auto;
}
.target-pane .photo-frame {
  min-height: 0;
  aspect-ratio: 1 / 1;
}
.crop-preview {
  display: grid;
  gap: 6px;
  justify-items: center;
}
.crop-preview strong {
  justify-self: start;
  font-size: 13px;
}
.crop-preview canvas {
  width: 100%;
  aspect-ratio: 1 / 1;
  border: 1px solid #d0d5dd;
  border-radius: 8px;
  background: #0f172a;
}
.original-pane .photo-frame {
  min-height: 0;
}
.crop-stage {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: visible;
}
.crop-stage img {
  position: absolute;
  max-width: none;
  max-height: none;
  object-fit: fill;
  transform-origin: center center;
}
.crop-box {
  position: absolute;
  border: 3px solid #22c55e;
  box-shadow: 0 0 0 9999px rgba(0, 0, 0, .45);
  cursor: move;
  touch-action: none;
}
.crop-resize-handle {
  position: absolute;
  right: -8px;
  bottom: -8px;
  width: 18px;
  height: 18px;
  border: 2px solid #fff;
  border-radius: 4px;
  background: #22c55e;
  cursor: nwse-resize;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .35);
}
.fill-color-control {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--subtle);
  font-size: 12px;
}
.fill-color-control.active {
  color: var(--text);
}
.fill-color-control input {
  width: 34px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: #fff;
}
.color-sample-control {
  min-height: 34px;
  padding: 6px 9px;
  color: var(--subtle);
  font-size: 12px;
  font-weight: 600;
}
.color-sample-control.is-active {
  border-color: var(--accent);
  background: #eff6ff;
  color: var(--accent);
}
.crop-box::before,
.crop-box::after {
  content: "";
  position: absolute;
  background: rgba(34, 197, 94, .9);
}
.crop-box::before {
  left: 33%;
  right: 33%;
  top: 0;
  bottom: 0;
  width: 1px;
  margin: auto;
}
.crop-box::after {
  top: 33%;
  bottom: 33%;
  left: 0;
  right: 0;
  height: 1px;
  margin: auto;
}
.status-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-top: 1px solid var(--line);
  color: var(--subtle);
  font-size: 13px;
}
.status-bar strong {
  color: var(--text);
}
.empty {
  padding: 24px;
  color: var(--subtle);
}
.crop-list-hint {
  padding: 8px 12px 12px;
  border-bottom: 1px solid var(--line);
}
.crop-list-hint[hidden] {
  display: none;
}
@media (max-width: 1680px) {
  .toolbar {
    grid-template-columns: 1fr;
  }
  .controls {
    justify-content: start;
  }
  .control-group {
    flex-wrap: nowrap;
    max-width: 100%;
    overflow-x: auto;
  }
  .control-group + .control-group {
    padding-left: 0;
    padding-top: 8px;
    border-left: 0;
    border-top: 1px solid var(--line);
  }
  .crop-size-control,
  .crop-rotation-control {
    flex: 1 1 260px;
  }
  .crop-size-control input,
  .crop-rotation-control input {
    width: 100%;
  }
  .crop-size-slider-frame {
    width: 100%;
  }
}
@media (max-width: 900px) {
  body {
    overflow: auto;
  }
  main {
    grid-template-columns: 1fr;
    overflow: visible;
  }
  .sidebar {
    max-height: 260px;
  }
  .workspace {
    min-height: 640px;
  }
  .compare {
    grid-template-columns: 1fr;
  }
}
</style>
</head>
<body>
<header>
  <a class="control-panel-link" href="/?step=crop_confirmation" title="Return to the Project365 control panel">Project365 Control Panel</a>
  <h1>Crop confirmation</h1>
</header>
<main>
	  <aside class="sidebar">
	    <div class="sidebar-controls">
	      <label>
	        Crop list
	        <select id="cropFilter">
	          <option value="missing">No saved crop data</option>
	          <option value="estimated">Saved estimates</option>
	          <option value="confirmed">User saved crops</option>
	          <option value="all">All linked originals</option>
	        </select>
	      </label>
        <div class="date-scope-controls">
          <label>
            From
            <input id="cropStartDate" type="date">
          </label>
          <label>
            To
            <input id="cropEndDate" type="date">
          </label>
        </div>
        <div class="date-scope-actions">
          <button id="applyCropDateScopeButton" type="button">Apply</button>
          <button id="clearCropDateScopeButton" type="button">Clear</button>
        </div>
	    </div>
    <div id="summary" class="summary">Loading linked originals...</div>
    <div id="cropListHint" class="summary crop-list-hint" hidden>Expand a year and month to choose a linked original for crop confirmation.</div>
    <div id="entryList" class="entry-list"></div>
  </aside>
  <section class="workspace">
    <div class="toolbar">
      <div class="heading">
        <strong id="entryHeading">No original selected</strong>
        <span id="candidatePath"></span>
      </div>
      <div class="controls">
        <div class="control-group adjustment-controls">
          <label id="fillColorControl" class="fill-color-control">
            Fill
            <input id="cropFillColor" type="color" value="#000000">
          </label>
          <button id="fillColorSampleButton" class="color-sample-control" type="button" title="Sample a 3x3 average fill color from the actual original image pixels" disabled>Sample</button>
          <button id="minimalFitButton" class="minimal-crop-control" type="button" title="Shortcut: f. Shrink the crop around its current center until it fits inside the original photo" disabled>Minimal fit (f)</button>
          <button id="minimalMoveButton" class="minimal-crop-control" type="button" title="Shortcut: m. Move the crop the shortest distance that fits it inside the original photo without resizing" disabled>Minimal move (m)</button>
          <button id="rotateQuarterTurnButton" class="icon-control" type="button" title="Rotate 90 degrees clockwise" aria-label="Rotate 90 degrees clockwise">↻</button>
          <label class="crop-size-control">
            Crop size
            <span id="cropSizeSliderFrame" class="crop-size-slider-frame">
              <input id="cropSizeSlider" type="range" min="1" max="100" value="100">
              <span id="cropFillIndicator" class="crop-fill-indicator" aria-hidden="true"></span>
            </span>
          </label>
          <label class="crop-rotation-control">
            Rotate
            <input id="cropRotationSlider" type="range" min="-15" max="15" step="0.1" value="0">
            <output id="cropRotationValue">0°</output>
          </label>
        </div>
        <div class="control-group item-actions">
          <button id="previousCropEntryButton" title="Shortcut: Left arrow">Previous entry (Left)</button>
          <button id="nextCropEntryButton" title="Shortcut: Right arrow">Next entry (Right)</button>
          <button id="suggestCropButton">Estimate crop</button>
          <button id="resetCropButton">Reset crop</button>
          <button id="saveCropButton" class="primary" title="Shortcut: Return. Save crop offsets to staging">Save crop (Return)</button>
          <button id="commitCropButton" title="Commit staged crop-window changes to the canonical database">Commit crop changes <span id="pendingCropCommitCount" class="pending-count">0 pending</span></button>
        </div>
      </div>
    </div>
    <div id="compare" class="compare">
      <div class="photo-pane target-pane">
        <div class="photo-title">
          <strong>Original Project365 target</strong>
          <span id="targetFacts"></span>
          <span id="targetPeople"></span>
        </div>
        <div class="photo-frame"><img id="targetImage" alt=""></div>
        <div class="crop-preview">
          <strong>Selected crop</strong>
          <canvas id="cropPreviewCanvas" width="320" height="320"></canvas>
          <button id="rejectOriginalButton" class="subtle-danger" title="Reject this original and return the entry to original-photo matching">Reject original</button>
        </div>
      </div>
      <div class="photo-pane original-pane">
        <div class="photo-title">
          <strong>Identified original</strong>
          <span id="originalFacts"></span>
        </div>
        <div class="photo-frame">
          <div class="crop-stage">
            <img id="originalImage" alt="">
            <div id="cropBox" class="crop-box" hidden><div id="cropResizeHandle" class="crop-resize-handle"></div></div>
          </div>
        </div>
      </div>
    </div>
    <div class="status-bar">
      <span id="cropStatus">Select an entry to confirm its square crop.</span>
      <span id="savedCropLabel"></span>
    </div>
  </section>
</main>
<script>
const state = {
  entries: [],
  selectedEntryId: "",
  currentEntry: null,
  selectedCandidate: null,
	  cropDraft: null,
	  cropDrag: null,
	  fillColorSampling: false,
	  pendingCropCommits: {pending_count: 0},
	  cropEstimateBatch: {},
	  cropFilterTouched: false,
	  cropEntryDates: [],
    cropStartDate: "",
    cropEndDate: "",
    cropScopeFallbackMessage: "",
	  openYears: new Set(),
	  openMonths: new Set()
	};

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

async function loadEntries(preferredEntryId = "") {
  const filterControl = document.getElementById("cropFilter");
  let cropFilter = filterControl.value;
  let body = await fetchJson(cropEntriesUrl(cropFilter));
  state.cropEstimateBatch = body.crop_estimate_batch || {};
  if (shouldPreferSavedEstimateFilter(cropFilter, state.cropEstimateBatch)) {
    filterControl.value = "estimated";
    cropFilter = "estimated";
    return loadEntries(preferredEntryId);
  }
  if (shouldProbePersistedSavedEstimateFilter(cropFilter, body)) {
    const estimatedBody = await fetchJson(cropEntriesUrl("estimated"));
    if ((estimatedBody.entries || []).length) {
      filterControl.value = "estimated";
      cropFilter = "estimated";
      body = estimatedBody;
      state.cropEstimateBatch = body.crop_estimate_batch || {};
    }
  }
  state.cropScopeFallbackMessage = "";
  if (shouldFallBackToAllForDirectCropScope(cropFilter, body)) {
    const allBody = await fetchJson(cropEntriesUrl("all"));
    if ((allBody.entries || []).length) {
      filterControl.value = "all";
      cropFilter = "all";
      body = allBody;
      state.cropEstimateBatch = body.crop_estimate_batch || {};
      state.cropScopeFallbackMessage = directCropScopeFallbackMessage(allBody.entries);
    }
  }
  state.entries = body.entries || [];
  state.pendingCropCommits = body.pending_crop_commits || {pending_count: 0};
  updatePendingCropCommitControl();
  const missingCount = state.entries.filter(entry => !entry.crop_has_crop).length;
  document.getElementById("summary").textContent =
    cropSummaryText(cropFilter, state.entries, missingCount) + cropDateScopeSummary() + cropScopeFallbackSummary() + cropEstimateBatchSummary(state.cropEstimateBatch);
  if (!state.entries.length) {
    state.selectedEntryId = "";
    state.currentEntry = null;
    state.selectedCandidate = null;
    renderEntries();
    renderEmpty();
    return;
  }
  const lastEntryId = window.localStorage.getItem("project365CropLastEntryId") || "";
  const requested = preferredEntryId || state.selectedEntryId || lastEntryId;
  const selected = cropEntryById(requested)
    || cropEntryAfter(preferredEntryId || state.selectedEntryId || lastEntryId)
    || state.entries[0];
  state.selectedEntryId = selected.entry_id;
  rememberOpenGroups(selected);
  renderEntries();
  await loadEntry(state.selectedEntryId);
}

function cropEntriesUrl(cropFilter) {
  const params = new URLSearchParams({crop_filter: cropFilter});
  for (const entryDate of state.cropEntryDates) params.append("entry_date", entryDate);
  if (state.cropStartDate) params.set("start_date", state.cropStartDate);
  if (state.cropEndDate) params.set("end_date", state.cropEndDate);
  return `/api/crop-entries?${params.toString()}`;
}

function shouldPreferSavedEstimateFilter(cropFilter, job) {
  if (state.cropFilterTouched || cropFilter !== "missing") return false;
  if (!job || !job.apply_estimates) return false;
  if (job.refresh_saved_estimates) return false;
  if (!["queued", "running"].includes(job.status || "")) return false;
  return Number(job.estimated_count || 0) > 0;
}

function shouldProbePersistedSavedEstimateFilter(cropFilter, body) {
  if (state.cropFilterTouched || cropFilter !== "missing") return false;
  return Number(body?.total_count || 0) === 0 && !(body?.entries || []).length;
}

function shouldFallBackToAllForDirectCropScope(cropFilter, body) {
  if (cropFilter === "all") return false;
  if (!hasDirectCropDateScope()) return false;
  return Number(body?.total_count || 0) === 0 && !(body?.entries || []).length;
}

function hasDirectCropDateScope() {
  if (state.cropEntryDates.length === 1) return true;
  return Boolean(state.cropStartDate && state.cropEndDate && state.cropStartDate === state.cropEndDate);
}

function directCropScopeFallbackMessage(entries) {
  const entry = entries[0] || {};
  return ` Current state: ${cropEntryStateText(entry)}.`;
}

function cropEntryStateText(entry) {
  if (!entry.crop_has_crop) return "no saved crop information";
  const source = String(entry.crop_source || "").trim().toLowerCase();
  const stateText = String(entry.crop_source_state || "").trim().toLowerCase();
  const pending = Boolean(entry.crop_commit_pending);
  if (source === "estimated" && pending) return "saved estimate already opened and pending commit";
  if (source === "estimated" && stateText === "staged") return "saved estimate";
  if (source === "manual" && pending) return "staged manual crop pending commit";
  if (source === "manual") return stateText === "applied" ? "committed manual crop" : "saved manual crop";
  return stateText === "applied" ? "committed saved crop" : "saved crop";
}

function cropSummaryText(cropFilter, entries, missingCount) {
  const countText = `${entries.length} linked original${entries.length === 1 ? "" : "s"}`;
  if (cropFilter === "missing") return `${countText} without saved crop data.`;
  if (cropFilter === "estimated") return `${countText} with saved estimate crop data.`;
  if (cropFilter === "confirmed") return `${countText} with user saved crop data.`;
  return `${countText} · ${missingCount} without saved crop data.`;
}

function cropScopeFallbackSummary() {
  return state.cropScopeFallbackMessage || "";
}

function cropEstimateBatchSummary(job) {
  if (!job || !job.id || !["queued", "running"].includes(job.status || "")) return "";
  const processed = Number(job.processed_count || 0);
  const total = Number(job.target_count || 0);
  const estimated = Number(job.estimated_count || 0);
  const mode = job.apply_estimates ? "saved" : "previewed";
  if (job.refresh_saved_estimates) {
    const changed = Number(job.changed_count || 0);
    const unchanged = Number(job.unchanged_count || 0);
    return ` Saved-estimate refresh running: ${processed}/${total} checked, ${changed} changed, ${unchanged} unchanged.`;
  }
  return ` Batch estimate running: ${processed}/${total} checked, ${estimated} ${mode}.`;
}

function cropDateScopeSummary() {
  if (state.cropEntryDates.length === 1) return ` Date ${state.cropEntryDates[0]}.`;
  if (state.cropEntryDates.length > 1) return ` ${state.cropEntryDates.length} dates.`;
  if (state.cropStartDate && state.cropEndDate && state.cropStartDate === state.cropEndDate) return ` Date ${state.cropStartDate}.`;
  if (state.cropStartDate && state.cropEndDate) return ` ${state.cropStartDate} to ${state.cropEndDate}.`;
  if (state.cropStartDate) return ` From ${state.cropStartDate}.`;
  if (state.cropEndDate) return ` Through ${state.cropEndDate}.`;
  return "";
}

async function loadEntry(entryId) {
  state.selectedEntryId = entryId;
  window.localStorage.setItem("project365CropLastEntryId", entryId);
  state.currentEntry = await fetchJson(`/api/crop-entry/${encodeURIComponent(entryId)}`);
  state.selectedCandidate = (state.currentEntry.candidates || []).find(candidate => candidate.selected) || null;
  state.cropDraft = initialCropForCandidate(state.selectedCandidate);
  state.fillColorSampling = false;
  rememberOpenGroups(state.currentEntry);
  renderEntries();
  renderCropEditor();
}

function renderEntries() {
  ensureOpenCropGroup();
  const list = document.getElementById("entryList");
  list.innerHTML = "";
  for (const [year, months] of groupedEntriesByYearMonth(state.entries)) {
    const yearDetails = document.createElement("details");
    yearDetails.className = "tree-group year";
    yearDetails.open = state.openYears.has(year);
    yearDetails.ontoggle = () => updateOpenGroup(state.openYears, year, yearDetails.open, yearDetails);
    yearDetails.innerHTML = `<summary>${escapeHtml(year)} <span class="tree-count">${entryCount(months)} dates</span></summary>`;
    for (const [month, entries] of months) {
      const monthKey = `${year}-${month}`;
      const monthDetails = document.createElement("details");
      monthDetails.className = "tree-group month";
      monthDetails.open = state.openMonths.has(monthKey);
      monthDetails.ontoggle = () => updateOpenGroup(state.openMonths, monthKey, monthDetails.open, monthDetails);
      monthDetails.innerHTML = `<summary>${escapeHtml(monthName(year, month))} <span class="tree-count">${entries.length}</span></summary>`;
      for (const entry of entries) {
        const button = document.createElement("button");
        button.className = `entry-item ${entry.entry_id === state.selectedEntryId ? "active" : ""}`;
        button.type = "button";
        button.onclick = () => loadEntry(entry.entry_id);
        const image = entry.source_token
          ? `<img src="/image/${encodeURIComponent(entry.source_token)}" alt="">`
          : `<span></span>`;
        const statusClass = entry.crop_has_crop ? "" : "missing";
        const people = peopleScript(entry.people_names);
        button.innerHTML = `
          ${image}
          <span>
            <span class="entry-date">${escapeHtml(entry.entry_date || entry.entry_id)}</span>
            <span class="entry-meta">${escapeHtml(entry.candidate_filename || entry.candidate_path || "linked original")}</span>
            ${people}
            <span class="entry-crop-status ${statusClass}">${escapeHtml(entry.crop_status || "")} · ${escapeHtml(entry.crop_source_state || "")}</span>
          </span>
        `;
        monthDetails.appendChild(button);
      }
      yearDetails.appendChild(monthDetails);
    }
    list.appendChild(yearDetails);
  }
  updateCropNavigationState();
}

function groupedEntriesByYearMonth(entries) {
  const years = new Map();
  for (const entry of entries) {
    const date = String(entry.entry_date || "");
    const year = date.slice(0, 4) || "Unknown";
    const month = date.slice(5, 7) || "00";
    if (!years.has(year)) years.set(year, new Map());
    if (!years.get(year).has(month)) years.get(year).set(month, []);
    years.get(year).get(month).push(entry);
  }
  return [...years.entries()].map(([year, months]) => [
    year,
    [...months.entries()].sort(([left], [right]) => left.localeCompare(right))
  ]);
}

function entryCount(months) {
  return months.reduce((total, [, entries]) => total + entries.length, 0);
}

function monthName(year, month) {
  const date = new Date(`${year}-${month}-01T00:00:00`);
  if (Number.isNaN(date.getTime())) return month;
  return date.toLocaleString(undefined, {month: "long"});
}

function rememberOpenGroups(entry) {
  if (!entry) return;
  const year = String(entry.entry_date || "").slice(0, 4);
  const month = String(entry.entry_date || "").slice(5, 7);
  if (year) state.openYears.add(year);
  if (year && month) state.openMonths.add(`${year}-${month}`);
}

function cropEntryById(entryId) {
  if (!entryId) return null;
  return state.entries.find(entry => entry.entry_id === entryId) || null;
}

function cropEntryAfter(entryId) {
  if (!entryId) return null;
  const reference = cropEntryById(entryId);
  const referenceDate = reference?.entry_date || String(entryId).replace(/^project365:/, "");
  if (!referenceDate) return null;
  return state.entries.find(entry => String(entry.entry_date || "") > referenceDate) || null;
}

function ensureOpenCropGroup() {
  if (!state.entries.length || hasOpenVisibleCropMonth()) return;
  rememberOpenGroups(cropEntryById(state.selectedEntryId) || state.entries[0]);
}

function hasOpenVisibleCropMonth() {
  return state.entries.some(entry => {
    const date = String(entry.entry_date || "");
    const year = date.slice(0, 4);
    const month = date.slice(5, 7);
    return year && month && state.openYears.has(year) && state.openMonths.has(`${year}-${month}`);
  });
}

function updateOpenGroup(collection, key, open, element) {
  if (open) {
    collection.add(key);
  } else {
    collection.delete(key);
    ensureOpenCropGroup();
    if (collection.has(key) && element) element.open = true;
  }
}

function renderEmpty() {
  document.getElementById("entryHeading").textContent = "No selected originals";
  const candidatePath = document.getElementById("candidatePath");
  candidatePath.textContent = "";
  candidatePath.removeAttribute("title");
  document.getElementById("targetFacts").textContent = "";
  document.getElementById("targetPeople").innerHTML = "";
  document.getElementById("originalFacts").textContent = "";
  document.getElementById("targetImage").removeAttribute("src");
  const originalImage = document.getElementById("originalImage");
  originalImage.removeAttribute("src");
  originalImage.style.transform = "";
  originalImage.style.left = "";
  originalImage.style.top = "";
  originalImage.style.width = "";
  originalImage.style.height = "";
  document.getElementById("cropBox").hidden = true;
  state.fillColorSampling = false;
  updateCropRotationControl();
  updateFillColorControl();
  clearCropPreview();
  document.getElementById("cropStatus").textContent = "";
  document.getElementById("cropListHint").hidden = false;
  document.getElementById("savedCropLabel").textContent = "";
  updateCropNavigationState();
}

function renderCropEditor() {
  const entry = state.currentEntry;
  const candidate = state.selectedCandidate;
  if (!entry || !candidate) {
    renderEmpty();
    return;
  }
  document.getElementById("entryHeading").textContent = entry.entry_date || entry.entry_id;
  document.getElementById("cropListHint").hidden = true;
  const candidatePath = document.getElementById("candidatePath");
  candidatePath.textContent = candidateFileLabel(candidate);
  candidatePath.title = candidate.path || candidate.filename || "";
  document.getElementById("targetFacts").textContent = formatPhotoFacts(entry.source_file_type, entry.source_byte_size, entry.source_dimensions);
  document.getElementById("targetPeople").innerHTML = peopleScript(entry.people_names);
  document.getElementById("originalFacts").textContent = formatPhotoFacts(
    fileTypeLabel(candidate.filename || candidate.path || "", candidate.mime_type),
    candidate.byte_size,
    candidate.dimensions
  );
  document.getElementById("targetImage").src = entry.source_token ? `/image/${entry.source_token}` : "";
  const originalImage = document.getElementById("originalImage");
  originalImage.onload = () => {
    ensureCropDraftFromImage();
    positionCropBox();
    updateCropPreview();
  };
  originalImage.src = candidate.token ? `/image/${candidate.token}` : "";
  updateCropSizeControl();
  updateCropRotationControl();
  positionCropBox();
  updateCropPreview();
  const cropSource = savedCropSourceLabel(candidate);
  document.getElementById("cropStatus").textContent = hasSavedCropForCandidate(candidate)
    ? `${cropSource} loaded. Adjust it if needed.`
    : "Adjust the default crop, estimate this crop, or run the batch estimate.";
  document.getElementById("savedCropLabel").textContent = cropLabel(candidate);
  updateCropNavigationState();
}

function initialCropForCandidate(candidate) {
  if (!candidate) return null;
  const saved = savedCropForCandidate(candidate);
  if (saved) return saved;
  return null;
}

function savedCropForCandidate(candidate) {
  const crop = {
    x: Number(candidate.review_crop_x),
    y: Number(candidate.review_crop_y),
    size: Number(candidate.review_crop_size),
    candidate_width: Number(candidate.review_crop_candidate_width),
    candidate_height: Number(candidate.review_crop_candidate_height),
    fill_color: normalizeFillColor(candidate.review_crop_fill_color),
    rotation_degrees: normalizeRotationDegrees(candidate.review_crop_rotation_degrees)
  };
  if ([crop.x, crop.y, crop.size, crop.candidate_width, crop.candidate_height].every(Number.isFinite) && crop.size > 0) return crop;
  return null;
}

function sourceForCropSave() {
  const source = String(state.selectedCandidate?.review_crop_source || "").trim().toLowerCase();
  if (source !== "estimated") return "manual";
  const saved = savedCropForCandidate(state.selectedCandidate);
  if (!saved || !state.cropDraft) return "manual";
  return cropsMatchForSourcePreservation(saved, state.cropDraft) ? "estimated" : "manual";
}

function cropsMatchForSourcePreservation(saved, draft) {
  const numericFields = ["x", "y", "size", "candidate_width", "candidate_height", "rotation_degrees"];
  const sameNumbers = numericFields.every(field => Number(saved[field]) === Number(draft[field]));
  const savedFill = normalizeFillColor(saved.fill_color) || defaultFillColor();
  const draftFill = normalizeFillColor(draft.fill_color) || defaultFillColor();
  return sameNumbers && savedFill === draftFill;
}

function hasSavedCropForCandidate(candidate) {
  return Boolean(savedCropForCandidate(candidate));
}

function shouldPreserveEstimatedCropOnReset() {
  const cropFilter = document.getElementById("cropFilter").value;
  const source = String(state.selectedCandidate?.review_crop_source || "").trim().toLowerCase();
  return cropFilter === "estimated" && source === "estimated" && hasSavedCropForCandidate(state.selectedCandidate);
}

function ensureCropDraftFromImage() {
  const image = document.getElementById("originalImage");
  if (!image.naturalWidth || !image.naturalHeight) return;
  const existing = state.cropDraft || {};
  const hasExistingGeometry = [existing.x, existing.y, existing.size, existing.candidate_width, existing.candidate_height]
    .map(value => Number(value))
    .every(value => Number.isFinite(value))
    && Number(existing.size) > 0;
  const width = numberOrDefault(existing.candidate_width, image.naturalWidth);
  const height = numberOrDefault(existing.candidate_height, image.naturalHeight);
  const size = numberOrDefault(existing.size, Math.min(width, height));
  state.cropDraft = clampCrop({
    x: numberOrDefault(existing.x, Math.round((width - size) / 2)),
    y: numberOrDefault(existing.y, Math.round((height - size) / 2)),
    size,
    candidate_width: width,
    candidate_height: height,
    fill_color: normalizeFillColor(existing.fill_color) || defaultFillColor(),
    rotation_degrees: normalizeRotationDegrees(existing.rotation_degrees)
  }, {allowOverflow: hasExistingGeometry});
  updateCropSizeControl();
  updateCropRotationControl();
}

function numberOrDefault(value, defaultValue) {
  const number = Number(value);
  return Number.isFinite(number) ? number : defaultValue;
}

function clampCrop(crop, options = {}) {
  const candidateWidth = Math.max(1, Math.round(Number(crop.candidate_width) || 1));
  const candidateHeight = Math.max(1, Math.round(Number(crop.candidate_height) || 1));
  const inBoundsMaxSize = Math.min(candidateWidth, candidateHeight);
  const maxSize = options.allowOverflow ? Math.max(candidateWidth, candidateHeight) * 2 : inBoundsMaxSize;
  const size = Math.max(1, Math.min(Math.round(Number(crop.size) || inBoundsMaxSize), maxSize));
  const minX = options.allowOverflow ? 1 - size : Math.min(0, candidateWidth - size);
  const minY = options.allowOverflow ? 1 - size : Math.min(0, candidateHeight - size);
  const maxX = options.allowOverflow ? candidateWidth - 1 : Math.max(0, candidateWidth - size);
  const maxY = options.allowOverflow ? candidateHeight - 1 : Math.max(0, candidateHeight - size);
  const rawX = Number(crop.x);
  const rawY = Number(crop.y);
  const roundedX = Number.isFinite(rawX) ? Math.round(rawX) : 0;
  const roundedY = Number.isFinite(rawY) ? Math.round(rawY) : 0;
  return {
    x: options.freePosition ? roundedX : Math.max(minX, Math.min(roundedX, maxX)),
    y: options.freePosition ? roundedY : Math.max(minY, Math.min(roundedY, maxY)),
    size,
    candidate_width: candidateWidth,
    candidate_height: candidateHeight,
    fill_color: normalizeFillColor(crop.fill_color) || defaultFillColor(),
    rotation_degrees: normalizeRotationDegrees(crop.rotation_degrees)
  };
}

function cropExtendsBeyondImage(crop) {
  if (!crop) return false;
  return cropSourceCorners(crop).some(point => !pointIsInsideImage(point, crop));
}

function cropSourceCorners(crop) {
  const centerX = crop.candidate_width / 2;
  const centerY = crop.candidate_height / 2;
  return cropCorners(crop).map(point => (
    rotatePoint(point, centerX, centerY, -normalizeRotationDegrees(crop.rotation_degrees))
  ));
}

function minimalFitSizeForCrop(crop) {
  if (!crop) return 0;
  const cropCenter = {
    x: crop.x + crop.size / 2,
    y: crop.y + crop.size / 2
  };
  const imageCenterX = crop.candidate_width / 2;
  const imageCenterY = crop.candidate_height / 2;
  const sourceCenter = rotatePoint(
    cropCenter,
    imageCenterX,
    imageCenterY,
    -normalizeRotationDegrees(crop.rotation_degrees)
  );
  const nearestEdgeDistance = Math.min(
    sourceCenter.x,
    crop.candidate_width - sourceCenter.x,
    sourceCenter.y,
    crop.candidate_height - sourceCenter.y
  );
  if (nearestEdgeDistance <= 0) return 0;
  const radians = normalizeRotationDegrees(crop.rotation_degrees) * Math.PI / 180;
  const halfSizeExpansion = Math.abs(Math.cos(radians)) + Math.abs(Math.sin(radians));
  const maximumSize = Math.floor((nearestEdgeDistance * 2) / Math.max(halfSizeExpansion, 0.0001) + 0.0001);
  const currentParity = Math.abs(Math.round(crop.size)) % 2;
  const parityMatchedSize = maximumSize % 2 === currentParity ? maximumSize : maximumSize - 1;
  return Math.max(0, parityMatchedSize);
}

function minimalFitCrop(crop) {
  if (!crop) return null;
  const size = minimalFitSizeForCrop(crop);
  if (size < 1 || size >= crop.size) return null;
  const centerX = crop.x + crop.size / 2;
  const centerY = crop.y + crop.size / 2;
  return clampCrop({
    ...crop,
    x: centerX - size / 2,
    y: centerY - size / 2,
    size
  }, {allowOverflow: true, freePosition: true});
}

function minimalMoveCrop(crop) {
  if (!crop) return null;
  const sourceBounds = boundsForPoints(cropSourceCorners(crop));
  const interval = {
    left: -sourceBounds.left,
    right: crop.candidate_width - sourceBounds.right,
    top: -sourceBounds.top,
    bottom: crop.candidate_height - sourceBounds.bottom
  };
  if (interval.left > interval.right || interval.top > interval.bottom) return null;
  const move = closestIntegerCropMove(interval, normalizeRotationDegrees(crop.rotation_degrees));
  if (!move || (move.dx === 0 && move.dy === 0)) return null;
  const moved = clampCrop({
    ...crop,
    x: crop.x + move.dx,
    y: crop.y + move.dy
  }, {allowOverflow: true, freePosition: true});
  return cropExtendsBeyondImage(moved) ? null : moved;
}

function closestIntegerCropMove(sourceInterval, rotationDegrees) {
  const corners = [
    {x: sourceInterval.left, y: sourceInterval.top},
    {x: sourceInterval.right, y: sourceInterval.top},
    {x: sourceInterval.right, y: sourceInterval.bottom},
    {x: sourceInterval.left, y: sourceInterval.bottom}
  ].map(point => rotateVector(point, rotationDegrees));
  const moveBounds = boundsForPoints(corners);
  const minDx = Math.ceil(moveBounds.left - 0.0001);
  const maxDx = Math.floor(moveBounds.right + 0.0001);
  const cos = Math.cos(rotationDegrees * Math.PI / 180);
  const sin = Math.sin(rotationDegrees * Math.PI / 180);
  let best = null;
  for (let dx = minDx; dx <= maxDx; dx += 1) {
    let minDy = -Infinity;
    let maxDy = Infinity;
    const xValueWithoutDy = dx * cos;
    const yValueWithoutDy = -dx * sin;
    if (!intersectLinearInterval(sourceInterval.left, sourceInterval.right, xValueWithoutDy, sin, value => { minDy = Math.max(minDy, value.min); maxDy = Math.min(maxDy, value.max); })) continue;
    if (!intersectLinearInterval(sourceInterval.top, sourceInterval.bottom, yValueWithoutDy, cos, value => { minDy = Math.max(minDy, value.min); maxDy = Math.min(maxDy, value.max); })) continue;
    if (minDy > maxDy) continue;
    const dy = nearestIntegerInInterval(minDy, maxDy);
    if (dy === null) continue;
    const distanceSquared = dx * dx + dy * dy;
    if (!best || distanceSquared < best.distanceSquared) best = {dx, dy, distanceSquared};
  }
  return best;
}

function intersectLinearInterval(minAllowed, maxAllowed, base, coefficient, apply) {
  const epsilon = 0.000001;
  if (Math.abs(coefficient) < epsilon) return base >= minAllowed - epsilon && base <= maxAllowed + epsilon;
  const values = [(minAllowed - base) / coefficient, (maxAllowed - base) / coefficient];
  apply({min: Math.min(...values), max: Math.max(...values)});
  return true;
}

function nearestIntegerInInterval(minValue, maxValue) {
  const minInteger = Math.ceil(minValue - 0.0001);
  const maxInteger = Math.floor(maxValue + 0.0001);
  if (minInteger > maxInteger) return null;
  if (minInteger <= 0 && maxInteger >= 0) return 0;
  return Math.abs(minInteger) < Math.abs(maxInteger) ? minInteger : maxInteger;
}

function rotateVector(vector, degrees) {
  const radians = degrees * Math.PI / 180;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  return {
    x: vector.x * cos - vector.y * sin,
    y: vector.x * sin + vector.y * cos
  };
}

function cropCorners(crop) {
  return [
    {x: crop.x, y: crop.y},
    {x: crop.x + crop.size, y: crop.y},
    {x: crop.x + crop.size, y: crop.y + crop.size},
    {x: crop.x, y: crop.y + crop.size}
  ];
}

function rotatedImageCorners(crop) {
  const centerX = crop.candidate_width / 2;
  const centerY = crop.candidate_height / 2;
  const rotation = normalizeRotationDegrees(crop.rotation_degrees);
  return [
    {x: 0, y: 0},
    {x: crop.candidate_width, y: 0},
    {x: crop.candidate_width, y: crop.candidate_height},
    {x: 0, y: crop.candidate_height}
  ].map(point => rotatePoint(point, centerX, centerY, rotation));
}

function rotatePoint(point, centerX, centerY, degrees) {
  const radians = degrees * Math.PI / 180;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  const dx = point.x - centerX;
  const dy = point.y - centerY;
  return {
    x: centerX + dx * cos - dy * sin,
    y: centerY + dx * sin + dy * cos
  };
}

function imagePointFromPointer(event) {
  const image = document.getElementById("originalImage");
  const crop = state.cropDraft;
  if (!crop || !image.naturalWidth || !image.naturalHeight) return null;
  const stageRect = image.parentElement.getBoundingClientRect();
  const stagePoint = {
    x: event.clientX - stageRect.left,
    y: event.clientY - stageRect.top
  };
  const displayWidth = parseFloat(image.style.width) || image.clientWidth;
  const displayHeight = parseFloat(image.style.height) || image.clientHeight;
  const originX = parseFloat(image.style.left) || 0;
  const originY = parseFloat(image.style.top) || 0;
  if (!displayWidth || !displayHeight) return null;
  const unrotated = rotatePoint(
    stagePoint,
    originX + displayWidth / 2,
    originY + displayHeight / 2,
    -normalizeRotationDegrees(crop.rotation_degrees)
  );
  const imageX = (unrotated.x - originX) / displayWidth * image.naturalWidth;
  const imageY = (unrotated.y - originY) / displayHeight * image.naturalHeight;
  const epsilon = 0.01;
  if (
    imageX < -epsilon
    || imageY < -epsilon
    || imageX > image.naturalWidth + epsilon
    || imageY > image.naturalHeight + epsilon
  ) {
    return null;
  }
  return {
    x: Math.max(0, Math.min(image.naturalWidth - 1, imageX)),
    y: Math.max(0, Math.min(image.naturalHeight - 1, imageY))
  };
}

function pointIsInsideImage(point, crop) {
  const epsilon = 0.01;
  return point.x >= -epsilon
    && point.y >= -epsilon
    && point.x <= crop.candidate_width + epsilon
    && point.y <= crop.candidate_height + epsilon;
}

function boundsForPoints(points) {
  const xs = points.map(point => point.x);
  const ys = points.map(point => point.y);
  return {
    left: Math.min(...xs),
    top: Math.min(...ys),
    right: Math.max(...xs),
    bottom: Math.max(...ys)
  };
}

function defaultFillColor() {
  return "#000000";
}

function normalizeFillColor(value) {
  const text = String(value || "").trim();
  return /^#[0-9a-fA-F]{6}$/.test(text) ? text.toLowerCase() : "";
}

function colorToHex(red, green, blue) {
  return `#${[red, green, blue].map(value => Math.max(0, Math.min(255, Math.round(value))).toString(16).padStart(2, "0")).join("")}`;
}

function normalizeRotationDegrees(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return 0;
  let normalized = Math.round(number * 10) / 10;
  while (normalized > 180) normalized -= 360;
  while (normalized <= -180) normalized += 360;
  return normalized;
}

function formatRotationDegrees(value) {
  const number = normalizeRotationDegrees(value);
  return `${Number.isInteger(number) ? number.toFixed(0) : number.toFixed(1)}°`;
}

function positionCropBox() {
  const box = document.getElementById("cropBox");
  const image = document.getElementById("originalImage");
  const crop = state.cropDraft;
  if (!crop || !image.naturalWidth || !image.naturalHeight) {
    box.hidden = true;
    image.style.transform = "";
    return;
  }
  const stage = image.parentElement;
  const stageWidth = stage.clientWidth;
  const stageHeight = stage.clientHeight;
  const screenMargin = 32;
  const availableWidth = Math.max(1, stageWidth - screenMargin * 2);
  const availableHeight = Math.max(1, stageHeight - screenMargin * 2);
  const imageBounds = boundsForPoints(rotatedImageCorners(crop));
  const boundsWidth = Math.max(1, imageBounds.right - imageBounds.left);
  const boundsHeight = Math.max(1, imageBounds.bottom - imageBounds.top);
  const scale = Math.min(availableWidth / boundsWidth, availableHeight / boundsHeight);
  const originX = (stageWidth - boundsWidth * scale) / 2 - imageBounds.left * scale;
  const originY = (stageHeight - boundsHeight * scale) / 2 - imageBounds.top * scale;
  image.style.left = `${originX}px`;
  image.style.top = `${originY}px`;
  image.style.width = `${crop.candidate_width * scale}px`;
  image.style.height = `${crop.candidate_height * scale}px`;
  image.style.transform = `rotate(${normalizeRotationDegrees(crop.rotation_degrees)}deg)`;
  box.hidden = false;
  box.style.left = `${originX + crop.x * scale}px`;
  box.style.top = `${originY + crop.y * scale}px`;
  box.style.width = `${crop.size * scale}px`;
  box.style.height = `${crop.size * scale}px`;
  updateFillColorControl();
  updateCropPreview();
}

function updateCropPreview() {
  const canvas = document.getElementById("cropPreviewCanvas");
  const image = document.getElementById("originalImage");
  const crop = state.cropDraft;
  if (!canvas) return;
  const context = canvas.getContext("2d");
  const previewSize = canvas.width;
  context.clearRect(0, 0, previewSize, previewSize);
  if (!crop || !image.naturalWidth || !image.naturalHeight) return;
  context.fillStyle = normalizeFillColor(crop.fill_color) || defaultFillColor();
  context.fillRect(0, 0, previewSize, previewSize);
  const scale = previewSize / crop.size;
  context.save();
  context.setTransform(scale, 0, 0, scale, -crop.x * scale, -crop.y * scale);
  context.translate(crop.candidate_width / 2, crop.candidate_height / 2);
  context.rotate(normalizeRotationDegrees(crop.rotation_degrees) * Math.PI / 180);
  context.drawImage(
    image,
    -crop.candidate_width / 2,
    -crop.candidate_height / 2,
    crop.candidate_width,
    crop.candidate_height
  );
  context.restore();
}

function clearCropPreview() {
  const canvas = document.getElementById("cropPreviewCanvas");
  if (!canvas) return;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
}

function updateCropSizeControl() {
  const slider = document.getElementById("cropSizeSlider");
  if (!state.cropDraft) {
    slider.disabled = true;
    return;
  }
  slider.disabled = false;
  const maxSize = Math.max(state.cropDraft.candidate_width, state.cropDraft.candidate_height) * 2;
  const adjustmentWindow = Math.max(20, Math.round(maxSize * 0.03));
  slider.min = Math.max(1, state.cropDraft.size - adjustmentWindow);
  slider.max = Math.min(maxSize, state.cropDraft.size + adjustmentWindow);
  slider.step = "1";
  slider.value = state.cropDraft.size;
  updateFillColorControl();
}

function updateCropRotationControl() {
  const slider = document.getElementById("cropRotationSlider");
  const output = document.getElementById("cropRotationValue");
  if (!state.cropDraft) {
    slider.disabled = true;
    output.textContent = "0°";
    return;
  }
  const rotation = normalizeRotationDegrees(state.cropDraft.rotation_degrees);
  slider.disabled = false;
  slider.value = rotation - quarterTurnBase(rotation);
  output.textContent = formatRotationDegrees(rotation);
}

function resizeCropDraft(size) {
  if (!state.cropDraft) return;
  const centerX = state.cropDraft.x + state.cropDraft.size / 2;
  const centerY = state.cropDraft.y + state.cropDraft.size / 2;
  state.cropDraft = clampCrop({
    ...state.cropDraft,
    x: Math.round(centerX - size / 2),
    y: Math.round(centerY - size / 2),
    size: Number(size)
  }, {allowOverflow: true});
  positionCropBox();
}

function rotateCropDraft(rotationDegrees) {
  if (!state.cropDraft) return;
  state.cropDraft = clampCrop({
    ...state.cropDraft,
    rotation_degrees: normalizeRotationDegrees(rotationDegrees)
  }, {allowOverflow: true});
  updateCropRotationControl();
  positionCropBox();
}

function fineRotateCropDraft(offsetDegrees) {
  if (!state.cropDraft) return;
  const currentRotation = normalizeRotationDegrees(state.cropDraft.rotation_degrees);
  rotateCropDraft(quarterTurnBase(currentRotation) + Number(offsetDegrees || 0));
}

function rotateCropDraftByQuarterTurn() {
  if (!state.cropDraft) return;
  rotateCropDraft(normalizeRotationDegrees(state.cropDraft.rotation_degrees + 90));
}

function quarterTurnBase(rotationDegrees) {
  return Math.round(normalizeRotationDegrees(rotationDegrees) / 90) * 90;
}

function updateFillColorControl() {
  const control = document.getElementById("fillColorControl");
  const input = document.getElementById("cropFillColor");
  const indicator = document.getElementById("cropFillIndicator");
  const sizeFrame = document.getElementById("cropSizeSliderFrame");
  const sampleButton = document.getElementById("fillColorSampleButton");
  const minimalFitButton = document.getElementById("minimalFitButton");
  const minimalMoveButton = document.getElementById("minimalMoveButton");
  if (!state.cropDraft) {
    state.fillColorSampling = false;
    control.classList.remove("active");
    indicator.classList.remove("active");
    sizeFrame.classList.remove("fill-active");
    sizeFrame.title = "";
    input.disabled = true;
    sampleButton.disabled = true;
    sampleButton.classList.remove("is-active");
    minimalFitButton.disabled = true;
    minimalMoveButton.disabled = true;
    return;
  }
  const hasFill = cropExtendsBeyondImage(state.cropDraft);
  const image = document.getElementById("originalImage");
  if (!image.naturalWidth || !image.naturalHeight) state.fillColorSampling = false;
  input.disabled = false;
  input.value = normalizeFillColor(state.cropDraft.fill_color) || defaultFillColor();
  sampleButton.disabled = !image.naturalWidth || !image.naturalHeight;
  sampleButton.classList.toggle("is-active", state.fillColorSampling);
  minimalFitButton.disabled = !hasFill;
  minimalMoveButton.disabled = !hasFill;
  control.classList.toggle("active", hasFill);
  indicator.classList.toggle("active", hasFill);
  sizeFrame.classList.toggle("fill-active", hasFill);
  sizeFrame.title = hasFill ? "Crop includes fill outside the original image." : "";
}

function minimalFitCurrentCrop() {
  if (!state.cropDraft || !cropExtendsBeyondImage(state.cropDraft)) return;
  const fitted = minimalFitCrop(state.cropDraft);
  if (!fitted) {
    document.getElementById("cropStatus").textContent = "Crop cannot fit without moving its center.";
    return;
  }
  state.cropDraft = fitted;
  updateCropSizeControl();
  positionCropBox();
  document.getElementById("cropStatus").textContent = "Crop resized to fit inside the original photo.";
}

function minimalMoveCurrentCrop() {
  if (!state.cropDraft || !cropExtendsBeyondImage(state.cropDraft)) return;
  const moved = minimalMoveCrop(state.cropDraft);
  if (!moved) {
    document.getElementById("cropStatus").textContent = "Crop cannot fit at this size; use Minimal fit or reduce the crop size.";
    return;
  }
  state.cropDraft = moved;
  positionCropBox();
  document.getElementById("cropStatus").textContent = "Crop moved to fit inside the original photo.";
}

function toggleFillColorSampler() {
  if (!state.cropDraft) return;
  const image = document.getElementById("originalImage");
  if (!image.naturalWidth || !image.naturalHeight) return;
  state.fillColorSampling = !state.fillColorSampling;
  updateFillColorControl();
  document.getElementById("cropStatus").textContent = state.fillColorSampling
    ? "Click the original photo to sample a 3x3 average fill color."
    : "Fill color sampler canceled.";
}

function sampleFillColorFromPointer(event) {
  if (!state.fillColorSampling || !state.cropDraft) return false;
  event.preventDefault();
  const point = imagePointFromPointer(event);
  if (!point) {
    document.getElementById("cropStatus").textContent = "Click directly on the original photo to sample its color.";
    return true;
  }
  const color = sampledImageColor(point);
  state.fillColorSampling = false;
  if (!color) {
    document.getElementById("cropStatus").textContent = "Could not read the selected image color.";
    updateFillColorControl();
    return true;
  }
  state.cropDraft.fill_color = color;
  updateFillColorControl();
  updateCropPreview();
  document.getElementById("cropStatus").textContent = `Fill color sampled from original pixels: ${color}.`;
  return true;
}

function sampledImageColor(point) {
  const image = document.getElementById("originalImage");
  if (!image.naturalWidth || !image.naturalHeight) return "";
  const centerX = Math.round(point.x);
  const centerY = Math.round(point.y);
  const left = Math.max(0, centerX - 1);
  const top = Math.max(0, centerY - 1);
  const right = Math.min(image.naturalWidth - 1, centerX + 1);
  const bottom = Math.min(image.naturalHeight - 1, centerY + 1);
  const width = right - left + 1;
  const height = bottom - top + 1;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", {willReadFrequently: true});
  context.drawImage(image, left, top, width, height, 0, 0, width, height);
  const pixels = context.getImageData(0, 0, width, height).data;
  let red = 0;
  let green = 0;
  let blue = 0;
  let alpha = 0;
  for (let index = 0; index < pixels.length; index += 4) {
    const weight = pixels[index + 3] / 255;
    red += pixels[index] * weight;
    green += pixels[index + 1] * weight;
    blue += pixels[index + 2] * weight;
    alpha += weight;
  }
  if (alpha <= 0) return "";
  return colorToHex(red / alpha, green / alpha, blue / alpha);
}

async function estimateCropForCurrentCandidate() {
  if (!state.currentEntry || !state.selectedCandidate) return;
  const entryId = state.currentEntry.entry_id;
  const candidatePath = state.selectedCandidate.path;
  const button = document.getElementById("suggestCropButton");
  const existingCrop = state.cropDraft ? {...state.cropDraft} : {};
  button.disabled = true;
  document.getElementById("cropStatus").textContent = "Estimating crop with current rotation.";
  try {
    const result = await fetchJson("/api/crop-suggestion", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: entryId,
        candidate_path: candidatePath,
        crop: existingCrop
      })
    });
    state.currentEntry = result.entry;
    state.selectedCandidate = (state.currentEntry.candidates || []).find(candidate => candidate.selected) || state.selectedCandidate;
    state.cropDraft = result.crop;
    renderCropEditor();
    document.getElementById("cropStatus").textContent = "Estimated crop staged with current rotation. Adjust it if needed.";
  } catch (error) {
    document.getElementById("cropStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function saveCropForCurrentCandidate() {
  if (!state.currentEntry || !state.selectedCandidate || !state.cropDraft) return;
  const button = document.getElementById("saveCropButton");
  const nextEntryId = nextEntryIdAfterCurrent();
  button.disabled = true;
  try {
    state.currentEntry = await fetchJson("/api/crop", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: state.currentEntry.entry_id,
        candidate_path: state.selectedCandidate.path,
        crop: state.cropDraft,
        source: sourceForCropSave()
      })
    });
    state.selectedCandidate = (state.currentEntry.candidates || []).find(candidate => candidate.selected) || state.selectedCandidate;
    state.cropDraft = savedCropForCandidate(state.selectedCandidate) || state.cropDraft;
    renderCropEditor();
    document.getElementById("cropStatus").textContent = "Crop offsets staged.";
    const cropFilter = document.getElementById("cropFilter").value;
    await loadEntries(["missing", "estimated"].includes(cropFilter) ? nextEntryId : state.currentEntry.entry_id);
  } catch (error) {
    document.getElementById("cropStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function resetCropForCurrentCandidate() {
  if (!state.currentEntry || !state.selectedCandidate) return;
  const button = document.getElementById("resetCropButton");
  const preserveEstimate = shouldPreserveEstimatedCropOnReset();
  button.disabled = true;
  try {
    state.currentEntry = await fetchJson("/api/crop-reset", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: state.currentEntry.entry_id,
        candidate_path: state.selectedCandidate.path,
        preserve_estimate: preserveEstimate
      })
    });
    state.selectedCandidate = (state.currentEntry.candidates || []).find(candidate => candidate.selected) || state.selectedCandidate;
    state.cropDraft = preserveEstimate ? savedCropForCandidate(state.selectedCandidate) : null;
    renderCropEditor();
    document.getElementById("cropStatus").textContent = preserveEstimate
      ? "Saved estimate restored. Adjust it if needed."
      : "Crop reset staged.";
    await loadEntries(state.currentEntry.entry_id);
  } catch (error) {
    document.getElementById("cropStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function updatePendingCropCommitControl() {
  const button = document.getElementById("commitCropButton");
  const count = document.getElementById("pendingCropCommitCount");
  const pending = Number(state.pendingCropCommits?.pending_count || 0);
  count.textContent = `${pending} pending`;
  button.disabled = pending <= 0;
}

async function commitStagedCrops() {
  const button = document.getElementById("commitCropButton");
  button.disabled = true;
  document.getElementById("cropStatus").textContent = "Committing staged crops.";
  try {
    const result = await fetchJson("/api/crop-commit", {method: "POST"});
    state.pendingCropCommits = {pending_count: result.remaining_count || 0};
    updatePendingCropCommitControl();
    const changed = Number(result.saved_count || 0) + Number(result.cleared_count || 0) + Number(result.rejected_count || 0);
    document.getElementById("cropStatus").textContent =
      `Committed ${changed} crop-window change${changed === 1 ? "" : "s"}.`;
    await loadEntries(state.selectedEntryId);
  } catch (error) {
    document.getElementById("cropStatus").textContent = error.message;
  } finally {
    updatePendingCropCommitControl();
  }
}

async function rejectOriginalForCurrentCrop() {
  if (!state.currentEntry || !state.selectedCandidate) return;
  if (!confirm(`Reject this original for ${state.currentEntry.entry_date || state.currentEntry.entry_id} and return the entry to original-photo matching?`)) return;
  const button = document.getElementById("rejectOriginalButton");
  const entryId = state.currentEntry.entry_id;
  button.disabled = true;
  document.getElementById("cropStatus").textContent = "Rejecting original and requeuing entry.";
  try {
    await fetchJson("/api/crop-reject-original", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        entry_id: entryId,
        candidate_path: state.selectedCandidate.path,
        notes: "Rejected from crop confirmation."
      })
    });
    state.currentEntry = null;
    state.selectedCandidate = null;
    state.cropDraft = null;
    document.getElementById("cropStatus").textContent = "Original rejected. Entry returned to original-photo matching.";
    await loadEntries();
  } catch (error) {
    document.getElementById("cropStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function nextEntryIdAfterCurrent() {
  const index = state.entries.findIndex(entry => entry.entry_id === state.selectedEntryId);
  if (index < 0) return "";
  const next = state.entries[index + 1] || state.entries[index - 1];
  return next ? next.entry_id : "";
}

function currentCropEntryIndex() {
  return state.entries.findIndex(entry => entry.entry_id === state.selectedEntryId);
}

function updateCropNavigationState() {
  const index = currentCropEntryIndex();
  const previousButton = document.getElementById("previousCropEntryButton");
  const nextButton = document.getElementById("nextCropEntryButton");
  if (previousButton) previousButton.disabled = index <= 0;
  if (nextButton) nextButton.disabled = index < 0 || index >= state.entries.length - 1;
}

async function selectAdjacentCropEntry(direction) {
  const index = currentCropEntryIndex();
  const target = state.entries[index + direction];
  if (!target) return;
  await loadEntry(target.entry_id);
}

function cropLabel(candidate) {
  const saved = savedCropForCandidate(candidate);
  if (!saved) return "No saved crop";
  return `${savedCropSourceLabel(candidate)}: x ${saved.x}, y ${saved.y}, size ${saved.size}`;
}

function savedCropSourceLabel(candidate) {
  const source = String(candidate?.review_crop_source || "").trim().toLowerCase();
  if (source === "estimated") return "Saved estimate";
  if (source === "manual") return "Manual crop";
  return "Saved crop";
}

function candidateFileLabel(candidate) {
  if (!candidate) return "";
  return candidate.filename || filenameFromPath(candidate.path) || "";
}

function filenameFromPath(path) {
  return String(path || "").split(/[\\/]/).filter(Boolean).pop() || "";
}

function peopleScript(names) {
  const values = Array.isArray(names) ? names.filter(Boolean) : [];
  if (!values.length) return "";
  const text = values.join(", ");
  return `<span class="people-script" title="${escapeHtml(text)}">${escapeHtml(text)}</span>`;
}

function fileTypeLabel(path, mimeType = "") {
  const lower = String(path || "").toLowerCase();
  if (mimeType) return mimeType;
  if (lower.endsWith(".png")) return "PNG";
  if (lower.endsWith(".heic")) return "HEIC";
  if (lower.endsWith(".tif") || lower.endsWith(".tiff")) return "TIFF";
  if (lower.endsWith(".gif")) return "GIF";
  if (lower.endsWith(".webp")) return "WEBP";
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "JPEG";
  return "";
}

function formatPhotoFacts(fileType, byteSize, dimensions = "") {
  return [fileType, formatFileSize(byteSize), dimensions].filter(Boolean).join(" | ");
}

function formatFileSize(byteSize) {
  const bytes = Number(String(byteSize || "").trim());
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${Math.round(bytes).toLocaleString()} bytes`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "";
  for (const nextUnit of units) {
    value = value / 1024;
    unit = nextUnit;
    if (value < 1024) break;
  }
  const rounded = value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2);
  return `${rounded} ${unit}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[char]));
}

function isCropShortcutEditableTarget(target) {
  if (!target) return false;
  const focusedControl = target.closest?.("input, select, textarea, button, a, [contenteditable='true']");
  return Boolean(focusedControl);
}

function runCropShortcutButton(buttonId, action) {
  const button = document.getElementById(buttonId);
  if (!button || button.disabled) return false;
  action();
  return true;
}

function handleCropKeyboardShortcut(event) {
  if (event.repeat || event.metaKey || event.ctrlKey || event.altKey || isCropShortcutEditableTarget(event.target)) return;
  const key = event.key.toLowerCase();
  let handled = false;
  if (key === "f") {
    handled = runCropShortcutButton("minimalFitButton", minimalFitCurrentCrop);
  } else if (key === "m") {
    handled = runCropShortcutButton("minimalMoveButton", minimalMoveCurrentCrop);
  } else if (event.key === "ArrowLeft") {
    handled = runCropShortcutButton("previousCropEntryButton", () => selectAdjacentCropEntry(-1));
  } else if (event.key === "ArrowRight") {
    handled = runCropShortcutButton("nextCropEntryButton", () => selectAdjacentCropEntry(1));
  } else if (event.key === "Enter") {
    handled = runCropShortcutButton("saveCropButton", saveCropForCurrentCandidate);
  }
  if (handled) event.preventDefault();
}

document.getElementById("cropSizeSlider").oninput = event => resizeCropDraft(event.target.value);
document.getElementById("cropRotationSlider").oninput = event => fineRotateCropDraft(event.target.value);
document.getElementById("rotateQuarterTurnButton").onclick = rotateCropDraftByQuarterTurn;
document.getElementById("fillColorSampleButton").onclick = toggleFillColorSampler;
document.getElementById("minimalFitButton").onclick = minimalFitCurrentCrop;
document.getElementById("minimalMoveButton").onclick = minimalMoveCurrentCrop;
document.getElementById("cropFilter").onchange = () => {
  state.cropFilterTouched = true;
  loadEntries();
};
document.getElementById("applyCropDateScopeButton").onclick = () => {
  applyCropDateScopeControls();
  loadEntries();
};
document.getElementById("clearCropDateScopeButton").onclick = () => {
  state.cropEntryDates = [];
  state.cropStartDate = "";
  state.cropEndDate = "";
  state.cropScopeFallbackMessage = "";
  document.getElementById("cropStartDate").value = "";
  document.getElementById("cropEndDate").value = "";
  loadEntries();
};
document.getElementById("suggestCropButton").onclick = estimateCropForCurrentCandidate;
document.getElementById("resetCropButton").onclick = resetCropForCurrentCandidate;
document.getElementById("rejectOriginalButton").onclick = rejectOriginalForCurrentCrop;
document.getElementById("previousCropEntryButton").onclick = () => selectAdjacentCropEntry(-1);
document.getElementById("nextCropEntryButton").onclick = () => selectAdjacentCropEntry(1);
document.getElementById("saveCropButton").onclick = saveCropForCurrentCandidate;
document.getElementById("commitCropButton").onclick = commitStagedCrops;
document.getElementById("originalImage").onpointerdown = event => {
  sampleFillColorFromPointer(event);
};
document.getElementById("cropBox").onpointerdown = event => {
  if (!state.cropDraft) return;
  if (sampleFillColorFromPointer(event)) return;
  event.preventDefault();
  const image = document.getElementById("originalImage");
  const scale = Math.min(
    image.clientWidth / state.cropDraft.candidate_width,
    image.clientHeight / state.cropDraft.candidate_height
  );
  document.getElementById("cropBox").setPointerCapture(event.pointerId);
  state.cropDrag = {
    mode: event.target?.id === "cropResizeHandle" ? "resize" : "move",
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    scale: Number.isFinite(scale) && scale > 0 ? scale : 1,
    crop: {...state.cropDraft}
  };
};
document.getElementById("cropBox").onpointermove = event => {
  if (!state.cropDrag || state.cropDrag.pointerId !== event.pointerId) return;
  const scale = state.cropDrag.scale || 1;
  if (state.cropDrag.mode === "resize") {
    const delta = Math.max(
      Math.round((event.clientX - state.cropDrag.startX) / scale),
      Math.round((event.clientY - state.cropDrag.startY) / scale)
    );
    state.cropDraft = clampCrop({
      ...state.cropDrag.crop,
      size: state.cropDrag.crop.size + delta
    }, {allowOverflow: true});
    updateCropSizeControl();
  } else {
    state.cropDraft = clampCrop({
      ...state.cropDrag.crop,
      x: state.cropDrag.crop.x + Math.round((event.clientX - state.cropDrag.startX) / scale),
      y: state.cropDrag.crop.y + Math.round((event.clientY - state.cropDrag.startY) / scale)
    }, {allowOverflow: true, freePosition: true});
  }
  positionCropBox();
};
document.getElementById("cropBox").onpointerup = event => {
  if (state.cropDrag?.pointerId === event.pointerId) state.cropDrag = null;
};
document.getElementById("cropFillColor").oninput = event => {
  if (!state.cropDraft) return;
  state.cropDraft.fill_color = normalizeFillColor(event.target.value) || defaultFillColor();
  updateFillColorControl();
  updateCropPreview();
};
document.addEventListener("keydown", handleCropKeyboardShortcut);
window.addEventListener("resize", positionCropBox);

function applyInitialCropFilterFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("crop_filter") || "";
  if (!["missing", "estimated", "confirmed", "all"].includes(requested)) return;
  document.getElementById("cropFilter").value = requested;
  state.cropFilterTouched = true;
}

function applyInitialCropDateScopeFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const entryDates = params.getAll("entry_date").flatMap(splitCropDateValues).filter(Boolean);
  const directDate = normalizeCropDate(params.get("date") || (entryDates.length === 1 ? entryDates[0] : ""));
  const startDate = normalizeCropDate(params.get("start_date") || directDate);
  const endDate = normalizeCropDate(params.get("end_date") || directDate);
  state.cropEntryDates = entryDates.map(normalizeCropDate).filter(Boolean);
  state.cropStartDate = startDate;
  state.cropEndDate = endDate;
  document.getElementById("cropStartDate").value = startDate;
  document.getElementById("cropEndDate").value = endDate;
}

function applyCropDateScopeControls() {
  const startDate = normalizeCropDate(document.getElementById("cropStartDate").value);
  const endDate = normalizeCropDate(document.getElementById("cropEndDate").value || startDate);
  state.cropEntryDates = [];
  state.cropStartDate = startDate;
  state.cropEndDate = endDate;
  document.getElementById("cropStartDate").value = startDate;
  document.getElementById("cropEndDate").value = endDate;
}

function splitCropDateValues(value) {
  return String(value || "").split(";").map(item => item.trim()).filter(Boolean);
}

function normalizeCropDate(value) {
  const text = String(value || "").trim().replace(/^project365:/, "");
  return /^\\d{4}-\\d{2}-\\d{2}$/.test(text) ? text : "";
}

applyInitialCropFilterFromUrl();
applyInitialCropDateScopeFromUrl();
loadEntries().catch(error => {
  document.getElementById("summary").textContent = error.message;
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
