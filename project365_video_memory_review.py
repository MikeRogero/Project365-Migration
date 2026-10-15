#!/usr/bin/env python3
"""Local, durable review queue for memory-triggering video screenshots.

The tool is deliberately separate from the Original Picker.  Preview generation,
AI suggestions, manual choices, and final-JPEG rendering are distinct stages.
Nothing in this module writes canonical.db or modifies a source video.

Automated frame rating is restricted to Apple's on-device Vision framework.
This module has no network-model client and never serializes previews for AI.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from project365_photo_library_index import VIDEO_EXTENSIONS
from project365_service_activity import activity_handler, inject_activity, start_idle_shutdown


FRAME_COUNT = 16
PREVIEW_MAX_PIXELS = 640
FINAL_FRAME_SAFETY_MARGIN_SECONDS = 0.1
DEFAULT_PAGE_SIZE = 1
MAX_PAGE_SIZE = 8
TRIAGE_DEFAULT_PAGE_SIZE = 48
TRIAGE_MAX_PAGE_SIZE = 96
CONTENT_FINGERPRINT_VERSION = 1
CONTENT_FINGERPRINT_FRAME_BYTES = 64
DUPLICATE_DURATION_TOLERANCE_SECONDS = 0.25
DUPLICATE_MAX_AVERAGE_PIXEL_DELTA = 2.0
DUPLICATE_MAX_PIXEL_DELTA = 32
DUPLICATE_MAX_AVERAGE_AHASH_DISTANCE = 1.0
DUPLICATE_MAX_FRAME_AHASH_DISTANCE = 8
TRIAGE_STATES = (
    "not_triaged",
    "no_detail",
    "rejected",
    "summary_wanted",
    "detail_wanted",
    "detail_completed",
)
PROMPT_VERSION = 1
PLAYBACK_TIMEOUT_MIN_SECONDS = 20 * 60
PLAYBACK_TIMEOUT_DURATION_MULTIPLIER = 6
PLAYBACK_TIMEOUT_MAX_SECONDS = 12 * 60 * 60
SYNOPSIS_SCHEMA_VERSION = 3
SYNOPSIS_SEGMENT_SECONDS = 2.0
SYNOPSIS_TRANSITION_SECONDS = 0.2


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def inferred_video_title(filename: str) -> str:
    """Return the descriptive filename suffix after an ISO date, if present."""
    stem = Path(str(filename or "")).stem.strip()
    match = re.match(
        r"^\d{4}-\d{2}-\d{2}(?:[ _-]+\d{6})?[ _-]+(.+)$",
        stem,
    )
    if match is None:
        return ""
    inferred = match.group(1).rstrip(" .\t_-\\")
    if re.fullmatch(r"(?:\d{6}|\d{1,2}([:._-])\d{2}\1\d{2})", inferred):
        return ""
    return inferred


def encode_content_fingerprint(samples: Iterable[tuple[int, bytes]]) -> bytes:
    """Pack indexed 8x8 grayscale samples into a deterministic binary fingerprint."""
    payload = bytearray()
    seen: set[int] = set()
    for frame_index, pixels in sorted(samples, key=lambda item: int(item[0])):
        index = int(frame_index)
        values = bytes(pixels)
        if not 0 <= index <= 65535 or index in seen:
            raise ValueError("Fingerprint frame indices must be unique 16-bit integers")
        if len(values) != CONTENT_FINGERPRINT_FRAME_BYTES:
            raise ValueError("Each fingerprint frame must contain exactly 64 grayscale pixels")
        seen.add(index)
        payload.extend(index.to_bytes(2, "big"))
        payload.extend(values)
    if not payload:
        raise ValueError("Content fingerprint requires at least one frame")
    return bytes(payload)


def decode_content_fingerprint(payload: bytes) -> dict[int, bytes]:
    record_size = 2 + CONTENT_FINGERPRINT_FRAME_BYTES
    values = bytes(payload or b"")
    if not values or len(values) % record_size:
        return {}
    frames: dict[int, bytes] = {}
    for offset in range(0, len(values), record_size):
        frame_index = int.from_bytes(values[offset : offset + 2], "big")
        if frame_index in frames:
            return {}
        frames[frame_index] = values[offset + 2 : offset + record_size]
    return frames


def _average_hash(pixels: bytes) -> int:
    mean = sum(pixels) / len(pixels)
    return sum((value >= mean) << index for index, value in enumerate(pixels))


def content_fingerprints_match(first: bytes, second: bytes) -> bool:
    """Return true only for strongly matching, aligned perceptual frame samples."""
    left = decode_content_fingerprint(first)
    right = decode_content_fingerprint(second)
    common = sorted(left.keys() & right.keys())
    required = max(8, math.ceil(max(len(left), len(right)) * 0.75))
    if len(common) < required:
        return False
    pixel_differences = [
        abs(left[index][position] - right[index][position])
        for index in common
        for position in range(CONTENT_FINGERPRINT_FRAME_BYTES)
    ]
    if (
        sum(pixel_differences) / len(pixel_differences)
        > DUPLICATE_MAX_AVERAGE_PIXEL_DELTA
        or max(pixel_differences) > DUPLICATE_MAX_PIXEL_DELTA
    ):
        return False
    hash_distances = [
        (_average_hash(left[index]) ^ _average_hash(right[index])).bit_count()
        for index in common
    ]
    return (
        sum(hash_distances) / len(hash_distances)
        <= DUPLICATE_MAX_AVERAGE_AHASH_DISTANCE
        and max(hash_distances) <= DUPLICATE_MAX_FRAME_AHASH_DISTANCE
    )


def frame_times(duration_seconds: float, count: int = FRAME_COUNT) -> list[float]:
    """Return deterministic interior samples, excluding fragile first/last frames."""
    if count <= 0:
        raise ValueError("count must be positive")
    duration = float(duration_seconds or 0.0)
    if duration <= 0:
        return [float(index) for index in range(count)]
    return [round(duration * index / (count + 1), 3) for index in range(1, count + 1)]


def normalize_memory_datetime(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{6}", text):
        raise ValueError("Memory Date must use yyyy-mm-dd hhmmss")
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m-%d %H%M%S")
    except ValueError as exc:
        raise ValueError("Memory Date must be a valid yyyy-mm-dd hhmmss value") from exc
    if parsed.date() < dt.date(1900, 1, 1) or parsed.date() > dt.date.today():
        raise ValueError("Memory Date is outside the supported range")
    return parsed.strftime("%Y-%m-%d %H%M%S")


def initial_memory_datetime(capture_timestamp: str, memory_date: str) -> str:
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):?(\d{2}):?(\d{2})",
        str(capture_timestamp or "").strip(),
    )
    if match:
        candidate = (
            f"{str(memory_date or '').strip()} "
            f"{match.group(2)}{match.group(3)}{match.group(4)}"
        )
        try:
            return normalize_memory_datetime(candidate)
        except ValueError:
            pass
    return normalize_memory_datetime(f"{str(memory_date or '').strip()} 000000")


def video_month_index(
    entries: list[dict[str, Any]], selected_month: str = ""
) -> dict[str, Any]:
    """Return all lightweight month counts and entries for one expanded month."""
    selected_month = str(selected_month or "").strip()
    if selected_month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", selected_month):
        raise ValueError("month must be yyyy-mm")
    counts: dict[str, int] = {}
    for entry in entries:
        value = str(entry.get("memory_datetime", ""))
        if re.match(r"^\d{4}-(0[1-9]|1[0-2])-\d{2} \d{6}$", value):
            month = value[:7]
            counts[month] = counts.get(month, 0) + 1
    months = sorted(counts)
    if not months:
        return {
            "entries": [], "month_counts": [], "selected_month": "",
            "previous_month": "", "next_month": "",
        }
    if selected_month not in counts:
        selected_month = months[0]
    selected_index = months.index(selected_month)
    return {
        "entries": [
            entry for entry in entries
            if str(entry.get("memory_datetime", ""))[:7] == selected_month
        ],
        "month_counts": [
            {"month": month, "count": counts[month]} for month in months
        ],
        "selected_month": selected_month,
        "previous_month": months[selected_index - 1] if selected_index > 0 else "",
        "next_month": months[selected_index + 1]
        if selected_index + 1 < len(months) else "",
    }


def choose_vision_selection(
    frames: list[dict[str, Any]],
    max_keep: int = 3,
    min_index_gap: int = 2,
) -> dict[str, Any]:
    """Choose a small, time-diverse default set from Apple Vision scores."""
    if not frames:
        raise ValueError("frames must not be empty")
    scored = []
    for frame in frames:
        score = frame.get("vision_score")
        if isinstance(score, (int, float)):
            scored.append(frame)
    if not scored:
        ordered = sorted(frames, key=lambda item: (int(item["frame_index"]), str(item["frame_id"])))
        fallback = ordered[(len(ordered) - 1) // 2]
        return {
            "selected_frame_ids": [str(fallback["frame_id"])],
            "main_frame_id": str(fallback["frame_id"]),
            "method": "temporal_fallback",
        }
    ranked = sorted(
        scored,
        key=lambda item: (
            -float(item["vision_score"]),
            int(item["frame_index"]),
            str(item["frame_id"]),
        ),
    )
    selected: list[dict[str, Any]] = []
    for frame in ranked:
        index = int(frame["frame_index"])
        if all(abs(index - int(existing["frame_index"])) >= min_index_gap for existing in selected):
            selected.append(frame)
        if len(selected) >= max_keep:
            break
    if not selected:
        selected = [ranked[0]]
    return {
        "selected_frame_ids": [str(item["frame_id"]) for item in selected],
        "main_frame_id": str(selected[0]["frame_id"]),
        "method": "apple_vision",
    }


class VideoMemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._initialize(connection)

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS video_review_items (
                video_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_byte_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                capture_date TEXT NOT NULL,
                date_source TEXT NOT NULL,
                capture_timestamp TEXT NOT NULL DEFAULT '',
                duration_seconds REAL NOT NULL,
                status TEXT NOT NULL,
                preview_format TEXT NOT NULL,
                ai_status TEXT NOT NULL DEFAULT 'pending',
                ai_method TEXT NOT NULL DEFAULT '',
                ai_model TEXT NOT NULL DEFAULT '',
                ai_prompt_version INTEGER NOT NULL DEFAULT 0,
                privacy_default INTEGER NOT NULL DEFAULT 0,
                review_status TEXT NOT NULL DEFAULT 'pending',
                memory_date TEXT NOT NULL DEFAULT '',
                memory_date_source TEXT NOT NULL DEFAULT '',
                memory_datetime TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                rotation_override INTEGER NOT NULL DEFAULT 0,
                output_folder_name TEXT NOT NULL DEFAULT '',
                rating INTEGER NOT NULL DEFAULT 0,
                triage_status TEXT NOT NULL DEFAULT 'not_triaged',
                last_triaged_at TEXT NOT NULL DEFAULT '',
                output_screenshots INTEGER NOT NULL DEFAULT 1,
                output_embedded INTEGER NOT NULL DEFAULT 0,
                output_summary INTEGER NOT NULL DEFAULT 0,
                synopsis_status TEXT NOT NULL DEFAULT 'not_requested',
                synopsis_path TEXT NOT NULL DEFAULT '',
                synopsis_error TEXT NOT NULL DEFAULT '',
                synopsis_updated_at TEXT NOT NULL DEFAULT '',
                content_fingerprint BLOB NOT NULL DEFAULT X'',
                content_fingerprint_version INTEGER NOT NULL DEFAULT 0,
                human_edit_version INTEGER NOT NULL DEFAULT 0,
                duplicate_group_id TEXT NOT NULL DEFAULT '',
                duplicate_state TEXT NOT NULL DEFAULT '',
                duplicate_of_video_id TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                review_version INTEGER NOT NULL DEFAULT 0,
                last_reviewed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS video_review_frames (
                frame_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                frame_index INTEGER NOT NULL,
                time_seconds REAL NOT NULL,
                preview_path TEXT NOT NULL,
                preview_bytes INTEGER NOT NULL,
                vision_score REAL,
                ai_selected INTEGER NOT NULL DEFAULT 0,
                selected INTEGER NOT NULL DEFAULT 0,
                is_main INTEGER NOT NULL DEFAULT 0,
                private INTEGER NOT NULL DEFAULT 0,
                user_captured INTEGER NOT NULL DEFAULT 0,
                render_status TEXT NOT NULL DEFAULT 'not_queued',
                final_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(video_id, frame_index),
                FOREIGN KEY(video_id) REFERENCES video_review_items(video_id) ON DELETE CASCADE
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_video_review_one_main
            ON video_review_frames(video_id)
            WHERE is_main = 1;
            CREATE INDEX IF NOT EXISTS idx_video_review_items_page
            ON video_review_items(status, capture_date, video_id);
            CREATE INDEX IF NOT EXISTS idx_video_review_frames_video
            ON video_review_frames(video_id, frame_index);
            CREATE INDEX IF NOT EXISTS idx_video_review_render_queue
            ON video_review_frames(render_status, video_id, frame_index);
            """
        )
        item_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(video_review_items)")
        }
        legacy_output_options = not any(
            name in item_columns
            for name in ("output_screenshots", "output_embedded", "output_summary")
        )
        if "privacy_default" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN privacy_default INTEGER NOT NULL DEFAULT 0"
            )
        if "review_status" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "memory_date" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN memory_date TEXT NOT NULL DEFAULT ''"
            )
        if "memory_date_source" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN memory_date_source TEXT NOT NULL DEFAULT ''"
            )
        if "memory_datetime" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN memory_datetime TEXT NOT NULL DEFAULT ''"
            )
        if "title" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )
        if "description" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )
        if "rotation_override" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN rotation_override INTEGER NOT NULL DEFAULT 0"
            )
        if "output_folder_name" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN output_folder_name TEXT NOT NULL DEFAULT ''"
            )
        if "last_reviewed_at" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN last_reviewed_at TEXT NOT NULL DEFAULT ''"
            )
        if "rating" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN rating INTEGER NOT NULL DEFAULT 0"
            )
        if "triage_status" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN triage_status TEXT NOT NULL DEFAULT 'not_triaged'"
            )
        if "last_triaged_at" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN last_triaged_at TEXT NOT NULL DEFAULT ''"
            )
        if "output_screenshots" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN output_screenshots INTEGER NOT NULL DEFAULT 1"
            )
        if "output_embedded" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN output_embedded INTEGER NOT NULL DEFAULT 0"
            )
        if "output_summary" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN output_summary INTEGER NOT NULL DEFAULT 0"
            )
        if legacy_output_options:
            connection.execute(
                """
                UPDATE video_review_items
                SET output_screenshots = CASE
                        WHEN review_status = 'rejected'
                          OR triage_status IN ('rejected', 'summary_wanted', 'no_detail')
                        THEN 0 ELSE 1 END,
                    output_embedded = CASE WHEN triage_status = 'no_detail' THEN 1 ELSE 0 END,
                    output_summary = CASE WHEN triage_status = 'summary_wanted' THEN 1 ELSE 0 END
                """
            )
        if "synopsis_status" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN synopsis_status TEXT NOT NULL DEFAULT 'not_requested'"
            )
        if "synopsis_path" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN synopsis_path TEXT NOT NULL DEFAULT ''"
            )
        if "synopsis_error" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN synopsis_error TEXT NOT NULL DEFAULT ''"
            )
        if "synopsis_updated_at" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN synopsis_updated_at TEXT NOT NULL DEFAULT ''"
            )
        if "content_fingerprint" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN content_fingerprint BLOB NOT NULL DEFAULT X''"
            )
        if "content_fingerprint_version" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN content_fingerprint_version INTEGER NOT NULL DEFAULT 0"
            )
        added_human_edit_version = "human_edit_version" not in item_columns
        if added_human_edit_version:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN human_edit_version INTEGER NOT NULL DEFAULT 0"
            )
        if "duplicate_group_id" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN duplicate_group_id TEXT NOT NULL DEFAULT ''"
            )
        if "duplicate_state" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN duplicate_state TEXT NOT NULL DEFAULT ''"
            )
        if "duplicate_of_video_id" not in item_columns:
            connection.execute(
                "ALTER TABLE video_review_items ADD COLUMN duplicate_of_video_id TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "UPDATE video_review_items SET triage_status = 'detail_completed' "
            "WHERE review_status = 'accepted' AND triage_status IN ('unreviewed', 'not_triaged')"
        )
        connection.execute(
            "UPDATE video_review_items SET triage_status = 'rejected' "
            "WHERE review_status = 'rejected' AND triage_status IN ('unreviewed', 'not_triaged', 'no_detail')"
        )
        connection.execute(
            "UPDATE video_review_items SET triage_status = 'not_triaged' "
            "WHERE triage_status = 'unreviewed'"
        )
        connection.execute(
            "UPDATE video_review_items SET last_reviewed_at = updated_at "
            "WHERE review_version > 0 AND last_reviewed_at = ''"
        )
        if added_human_edit_version:
            connection.execute(
                """
                UPDATE video_review_items
                SET human_edit_version = 1
                WHERE rating > 0
                   OR triage_status NOT IN ('unreviewed', 'not_triaged', 'rejected')
                   OR memory_date_source = 'user_adjusted'
                   OR title <> '' OR description <> ''
                   OR rotation_override <> 0 OR privacy_default <> 0
                   OR (review_status <> 'rejected' AND review_version > 0)
                   OR (review_status = 'rejected' AND review_version > 1)
                """
            )
        connection.execute(
            "UPDATE video_review_items SET memory_date = capture_date, memory_date_source = date_source WHERE memory_date = ''"
        )
        for row in connection.execute(
            "SELECT video_id, capture_timestamp, memory_date FROM video_review_items WHERE memory_datetime = ''"
        ).fetchall():
            connection.execute(
                "UPDATE video_review_items SET memory_datetime = ? WHERE video_id = ?",
                (
                    initial_memory_datetime(str(row["capture_timestamp"]), str(row["memory_date"])),
                    str(row["video_id"]),
                ),
            )
        for row in connection.execute(
            "SELECT video_id, memory_date, memory_datetime FROM video_review_items "
            "WHERE memory_datetime <> '' AND substr(memory_datetime, 1, 10) <> memory_date"
        ).fetchall():
            connection.execute(
                "UPDATE video_review_items SET memory_datetime = ? WHERE video_id = ?",
                (
                    normalize_memory_datetime(
                        f"{str(row['memory_date'])} {str(row['memory_datetime'])[11:17]}"
                    ),
                    str(row["video_id"]),
                ),
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_review_queue ON video_review_items(status, review_status, capture_date, video_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_triage_status_date "
            "ON video_review_items(status, triage_status, memory_datetime, video_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_triage_rating "
            "ON video_review_items(status, rating, memory_datetime, video_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_duplicate_group "
            "ON video_review_items(duplicate_state, duplicate_group_id, video_id)"
        )
        frame_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(video_review_frames)")
        }
        if "private" not in frame_columns:
            connection.execute(
                "ALTER TABLE video_review_frames ADD COLUMN private INTEGER NOT NULL DEFAULT 0"
            )
        if "user_captured" not in frame_columns:
            connection.execute(
                "ALTER TABLE video_review_frames ADD COLUMN user_captured INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                "UPDATE video_review_frames SET user_captured = 1 WHERE frame_index > ?",
                (FRAME_COUNT,),
            )

    def upsert_video(self, values: dict[str, Any]) -> None:
        now = utc_now()
        memory_date = str(values.get("memory_date", values["capture_date"]))
        memory_datetime = values.get("memory_datetime") or initial_memory_datetime(
            str(values.get("capture_timestamp", "")), memory_date
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO video_review_items (
                    video_id, source_path, source_sha256, source_byte_size,
                    source_mtime_ns, capture_date, date_source, capture_timestamp,
                    duration_seconds, status, preview_format, memory_date,
                    memory_date_source, memory_datetime, title, description,
                    triage_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    source_path = excluded.source_path,
                    source_sha256 = excluded.source_sha256,
                    source_byte_size = excluded.source_byte_size,
                    source_mtime_ns = excluded.source_mtime_ns,
                    capture_date = excluded.capture_date,
                    date_source = excluded.date_source,
                    capture_timestamp = excluded.capture_timestamp,
                    duration_seconds = excluded.duration_seconds,
                    status = excluded.status,
                    preview_format = excluded.preview_format,
                    updated_at = excluded.updated_at
                """,
                (
                    values["video_id"],
                    values["source_path"],
                    values["source_sha256"],
                    int(values["source_byte_size"]),
                    int(values["source_mtime_ns"]),
                    values["capture_date"],
                    values["date_source"],
                    values.get("capture_timestamp", ""),
                    float(values.get("duration_seconds") or 0.0),
                    values.get("status", "queued"),
                    values.get("preview_format", "jpeg"),
                    memory_date,
                    values.get("memory_date_source", values["date_source"]),
                    memory_datetime,
                    str(values.get("title", "")),
                    str(values.get("description", "")),
                    "not_triaged",
                    now,
                    now,
                ),
            )
            connection.commit()

    def replace_frames(self, video_id: str, frames: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_frames = {
                str(row["frame_id"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM video_review_frames WHERE video_id = ?",
                    (video_id,),
                )
            }
            video_row = connection.execute(
                "SELECT privacy_default FROM video_review_items WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            privacy_default = bool(video_row["privacy_default"]) if video_row else False
            connection.execute("DELETE FROM video_review_frames WHERE video_id = ?", (video_id,))
            connection.executemany(
                """
                INSERT INTO video_review_frames (
                    frame_id, video_id, frame_index, time_seconds, preview_path,
                    preview_bytes, vision_score, ai_selected, selected, is_main,
                    private, user_captured, render_status, final_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        frame["frame_id"],
                        video_id,
                        int(frame["frame_index"]),
                        float(frame["time_seconds"]),
                        frame["preview_path"],
                        int(frame.get("preview_bytes") or 0),
                        frame.get("vision_score"),
                        int(bool(existing_frames.get(str(frame["frame_id"]), {}).get("ai_selected", 0))),
                        int(bool(existing_frames.get(str(frame["frame_id"]), {}).get("selected", 0))),
                        int(bool(existing_frames.get(str(frame["frame_id"]), {}).get("is_main", 0))),
                        int(bool(existing_frames.get(str(frame["frame_id"]), {}).get("private", privacy_default))),
                        int(bool(existing_frames.get(str(frame["frame_id"]), {}).get("user_captured", 0))),
                        str(existing_frames.get(str(frame["frame_id"]), {}).get("render_status", "not_queued")),
                        str(existing_frames.get(str(frame["frame_id"]), {}).get("final_path", "")),
                        now,
                        now,
                    )
                    for frame in frames
                ],
            )
            connection.execute(
                "UPDATE video_review_items SET status = 'ready', updated_at = ? WHERE video_id = ?",
                (now, video_id),
            )
            connection.commit()

    def save_ai_selection(
        self,
        video_id: str,
        selected_frame_ids: Iterable[str],
        main_frame_id: str,
        method: str,
        model: str,
    ) -> None:
        selected = list(dict.fromkeys(str(value) for value in selected_frame_ids))
        if main_frame_id not in selected:
            selected.append(main_frame_id)
        now = utc_now()
        with self._connect() as connection:
            valid = {
                str(row[0])
                for row in connection.execute(
                    "SELECT frame_id FROM video_review_frames WHERE video_id = ?", (video_id,)
                )
            }
            if not selected or main_frame_id not in valid or any(value not in valid for value in selected):
                raise ValueError("AI selection contains an unknown frame")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE video_review_frames
                SET ai_selected = 0, selected = 0, is_main = 0, updated_at = ?
                WHERE video_id = ?
                """,
                (now, video_id),
            )
            placeholders = ",".join("?" for _ in selected)
            connection.execute(
                f"""
                UPDATE video_review_frames
                SET ai_selected = 1, selected = 1, updated_at = ?
                WHERE video_id = ? AND frame_id IN ({placeholders})
                """,
                (now, video_id, *selected),
            )
            connection.execute(
                "UPDATE video_review_frames SET is_main = 1, selected = 1, updated_at = ? WHERE frame_id = ?",
                (now, main_frame_id),
            )
            connection.execute(
                """
                UPDATE video_review_items
                SET ai_status = 'complete', ai_method = ?, ai_model = ?,
                    ai_prompt_version = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (method, model, PROMPT_VERSION, now, video_id),
            )
            connection.commit()

    def save_content_fingerprint(self, video_id: str, fingerprint: bytes) -> None:
        if not decode_content_fingerprint(fingerprint):
            raise ValueError("Content fingerprint is invalid")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE video_review_items
                SET content_fingerprint = ?, content_fingerprint_version = ?, updated_at = ?
                WHERE video_id = ? AND status = 'ready'
                """,
                (sqlite3.Binary(fingerprint), CONTENT_FINGERPRINT_VERSION, utc_now(), video_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown or unavailable video")

    def duplicate_fingerprint_candidate_ids(self, video_id: str | None = None) -> list[str]:
        with self._connect() as connection:
            parameters: tuple[object, ...] = ()
            timestamp_clause = ""
            if video_id is not None:
                target = connection.execute(
                    """
                    SELECT capture_timestamp
                    FROM video_review_items
                    WHERE video_id = ? AND status = 'ready'
                      AND review_status IN ('pending', 'rejected', 'accepted')
                    """,
                    (video_id,),
                ).fetchone()
                if target is None or not str(target["capture_timestamp"]):
                    return []
                timestamp_clause = "AND capture_timestamp = ?"
                parameters = (str(target["capture_timestamp"]),)
            rows = connection.execute(
                f"""
                SELECT video_id, capture_timestamp, duration_seconds, review_status
                FROM video_review_items
                WHERE status = 'ready' AND review_status IN ('pending', 'rejected', 'accepted')
                  AND capture_timestamp <> '' {timestamp_clause}
                ORDER BY capture_timestamp, created_at, video_id
                """,
                parameters,
            ).fetchall()
        by_timestamp: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_timestamp.setdefault(str(row["capture_timestamp"]), []).append(row)
        candidates: set[str] = set()
        for values in by_timestamp.values():
            if len(values) < 2 or not any(str(row["review_status"]) == "pending" for row in values):
                continue
            for index, first in enumerate(values):
                for second in values[index + 1 :]:
                    if abs(float(first["duration_seconds"]) - float(second["duration_seconds"])) <= (
                        DUPLICATE_DURATION_TOLERANCE_SECONDS
                    ):
                        candidates.update((str(first["video_id"]), str(second["video_id"])))
        return sorted(candidates)

    def reconcile_duplicate_groups(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT video_id, capture_timestamp, duration_seconds, review_status,
                       created_at, content_fingerprint, content_fingerprint_version,
                       human_edit_version, duplicate_group_id, duplicate_state
                FROM video_review_items
                WHERE status = 'ready' AND review_status IN ('pending', 'rejected', 'accepted')
                  AND capture_timestamp <> '' AND content_fingerprint_version = ?
                  AND length(content_fingerprint) > 0
                ORDER BY capture_timestamp, created_at, video_id
                """,
                (CONTENT_FINGERPRINT_VERSION,),
            ).fetchall()
            by_timestamp: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_timestamp.setdefault(str(row["capture_timestamp"]), []).append(row)

            components: list[list[sqlite3.Row]] = []
            for values in by_timestamp.values():
                if len(values) < 2:
                    continue
                parents = list(range(len(values)))

                def find(index: int) -> int:
                    while parents[index] != index:
                        parents[index] = parents[parents[index]]
                        index = parents[index]
                    return index

                def union(first_index: int, second_index: int) -> None:
                    first_root = find(first_index)
                    second_root = find(second_index)
                    if first_root != second_root:
                        parents[second_root] = first_root

                for first_index, first in enumerate(values):
                    for second_index in range(first_index + 1, len(values)):
                        second = values[second_index]
                        if abs(float(first["duration_seconds"]) - float(second["duration_seconds"])) > (
                            DUPLICATE_DURATION_TOLERANCE_SECONDS
                        ):
                            continue
                        if content_fingerprints_match(
                            bytes(first["content_fingerprint"]),
                            bytes(second["content_fingerprint"]),
                        ):
                            union(first_index, second_index)
                grouped: dict[int, list[sqlite3.Row]] = {}
                for index, row in enumerate(values):
                    grouped.setdefault(find(index), []).append(row)
                components.extend(
                    component
                    for component in grouped.values()
                    if len(component) > 1
                    and any(str(row["review_status"]) == "pending" for row in component)
                )

            now = utc_now()
            connection.execute("BEGIN IMMEDIATE")
            for component in components:
                existing_group_ids = {
                    str(row["duplicate_group_id"])
                    for row in component
                    if str(row["duplicate_group_id"])
                }
                group_id = (
                    next(iter(existing_group_ids))
                    if len(existing_group_ids) == 1
                    else min(str(row["video_id"]) for row in component)
                )
                keepers = [row for row in component if str(row["duplicate_state"]) == "keeper"]
                edited_unresolved = any(
                    int(row["human_edit_version"] or 0) > 0
                    and str(row["duplicate_state"]) != "keeper"
                    for row in component
                )
                is_problem = len(keepers) > 1 or edited_unresolved or len(existing_group_ids) > 1
                if not keepers and any(int(row["human_edit_version"] or 0) > 0 for row in component):
                    is_problem = True
                if is_problem:
                    placeholders = ",".join("?" for _ in component)
                    connection.execute(
                        f"""
                        UPDATE video_review_items
                        SET duplicate_group_id = ?, duplicate_state = 'problem',
                            duplicate_of_video_id = '', updated_at = ?
                        WHERE video_id IN ({placeholders})
                        """,
                        (group_id, now, *(str(row["video_id"]) for row in component)),
                    )
                    continue
                keeper = keepers[0] if keepers else min(
                    (row for row in component if str(row["review_status"]) == "pending"),
                    key=lambda row: (str(row["created_at"]), str(row["video_id"])),
                )
                keeper_id = str(keeper["video_id"])
                connection.execute(
                    """
                    UPDATE video_review_items
                    SET duplicate_group_id = ?, duplicate_state = 'keeper',
                        duplicate_of_video_id = '', updated_at = ?
                    WHERE video_id = ?
                    """,
                    (group_id, now, keeper_id),
                )
                sibling_ids = [
                    str(row["video_id"]) for row in component if str(row["video_id"]) != keeper_id
                ]
                placeholders = ",".join("?" for _ in sibling_ids)
                connection.execute(
                    f"""
                    UPDATE video_review_items
                    SET review_status = 'rejected', triage_status = 'rejected',
                        output_screenshots = 0, output_embedded = 0, output_summary = 0,
                        duplicate_group_id = ?, duplicate_state = 'auto_rejected',
                        duplicate_of_video_id = ?, updated_at = ?
                    WHERE video_id IN ({placeholders})
                    """,
                    (group_id, keeper_id, now, *sibling_ids),
                )
                connection.execute(
                    f"""
                    UPDATE video_review_frames
                    SET render_status = 'not_queued', updated_at = ?
                    WHERE video_id IN ({placeholders}) AND render_status IN ('queued', 'error')
                    """,
                    (now, *sibling_ids),
                )
            connection.commit()
            summary = connection.execute(
                """
                SELECT COUNT(DISTINCT CASE WHEN duplicate_state <> '' THEN duplicate_group_id END),
                       COUNT(DISTINCT CASE WHEN duplicate_state = 'problem' THEN duplicate_group_id END),
                       SUM(CASE WHEN duplicate_state = 'problem' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN duplicate_state = 'auto_rejected' THEN 1 ELSE 0 END)
                FROM video_review_items
                """
            ).fetchone()
        return {
            "duplicate_groups": int(summary[0] or 0),
            "problem_groups": int(summary[1] or 0),
            "problem_items": int(summary[2] or 0),
            "auto_rejected": int(summary[3] or 0),
        }

    def resolve_duplicate_group(self, group_id: str, keeper_video_id: str) -> dict[str, Any]:
        group_id = str(group_id or "").strip()
        keeper_video_id = str(keeper_video_id or "").strip()
        if not group_id or not keeper_video_id:
            raise ValueError("Duplicate group and keeper are required")
        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT video_id, review_status
                FROM video_review_items
                WHERE duplicate_group_id = ? AND duplicate_state = 'problem'
                ORDER BY video_id
                """,
                (group_id,),
            ).fetchall()
            identifiers = [str(row["video_id"]) for row in rows]
            if len(identifiers) < 2 or keeper_video_id not in identifiers:
                raise ValueError("Keeper is not a member of an unresolved duplicate group")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE video_review_items
                SET review_status = CASE WHEN review_status = 'rejected' THEN 'pending' ELSE review_status END,
                    triage_status = CASE WHEN triage_status = 'rejected' THEN 'not_triaged' ELSE triage_status END,
                    output_screenshots = CASE
                        WHEN review_status = 'rejected' AND output_screenshots = 0
                         AND output_embedded = 0 AND output_summary = 0 THEN 1
                        ELSE output_screenshots END,
                    duplicate_state = 'keeper', duplicate_of_video_id = '', updated_at = ?
                WHERE video_id = ?
                """,
                (now, keeper_video_id),
            )
            sibling_ids = [value for value in identifiers if value != keeper_video_id]
            placeholders = ",".join("?" for _ in sibling_ids)
            connection.execute(
                f"""
                UPDATE video_review_items
                SET review_status = 'rejected', triage_status = 'rejected',
                    output_screenshots = 0, output_embedded = 0, output_summary = 0,
                    duplicate_state = 'auto_rejected', duplicate_of_video_id = ?, updated_at = ?
                WHERE video_id IN ({placeholders})
                """,
                (keeper_video_id, now, *sibling_ids),
            )
            connection.execute(
                f"""
                UPDATE video_review_frames
                SET render_status = 'not_queued', updated_at = ?
                WHERE video_id IN ({placeholders}) AND render_status IN ('queued', 'error')
                """,
                (now, *sibling_ids),
            )
            connection.commit()
        return {
            "duplicate_group_id": group_id,
            "keeper_video_id": keeper_video_id,
            "rejected_video_ids": sibling_ids,
        }

    def set_frame_selected(self, frame_id: str, selected: bool) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT video_id, is_main FROM video_review_frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown frame")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE video_review_frames
                SET selected = ?, is_main = CASE WHEN ? = 0 THEN 0 ELSE is_main END,
                    render_status = CASE
                        WHEN ? = 0 AND render_status = 'queued' THEN 'not_queued'
                        ELSE render_status
                    END,
                    updated_at = ?
                WHERE frame_id = ?
                """,
                (1 if selected else 0, 1 if selected else 0, 1 if selected else 0, now, frame_id),
            )
            if selected:
                main = connection.execute(
                    "SELECT 1 FROM video_review_frames WHERE video_id = ? AND is_main = 1",
                    (row["video_id"],),
                ).fetchone()
                if main is None:
                    connection.execute(
                        "UPDATE video_review_frames SET is_main = 1 WHERE frame_id = ?", (frame_id,)
                    )
            connection.execute(
                """
                UPDATE video_review_items
                SET review_version = review_version + 1,
                    human_edit_version = human_edit_version + 1,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (now, now, row["video_id"]),
            )
            connection.commit()
            result = connection.execute(
                "SELECT * FROM video_review_frames WHERE frame_id = ?",
                (frame_id,),
            ).fetchone()
            return self._frame_payload(result, include_media=True)

    def deselect_non_key_frames(self, video_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            video = connection.execute(
                "SELECT 1 FROM video_review_items WHERE video_id = ? AND status = 'ready'",
                (video_id,),
            ).fetchone()
            if video is None:
                raise ValueError("Unknown or unavailable video")
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE video_review_frames
                SET selected = 0,
                    render_status = CASE
                        WHEN render_status = 'queued' THEN 'not_queued'
                        ELSE render_status
                    END,
                    updated_at = ?
                WHERE video_id = ? AND is_main = 0 AND selected = 1
                """,
                (now, video_id),
            )
            connection.execute(
                """
                UPDATE video_review_items
                SET review_version = review_version + 1,
                    human_edit_version = human_edit_version + 1,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (now, now, video_id),
            )
            connection.commit()
        return {"video_id": video_id, "deselected": int(cursor.rowcount)}

    def set_main_frame(self, frame_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT video_id FROM video_review_frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown frame")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE video_review_frames SET is_main = 0, updated_at = ? WHERE video_id = ?",
                (now, row["video_id"]),
            )
            connection.execute(
                "UPDATE video_review_frames SET selected = 1, is_main = 1, updated_at = ? WHERE frame_id = ?",
                (now, frame_id),
            )
            connection.execute(
                "UPDATE video_review_items SET review_version = review_version + 1, "
                "human_edit_version = human_edit_version + 1, "
                "last_reviewed_at = ?, updated_at = ? WHERE video_id = ?",
                (now, now, row["video_id"]),
            )
            connection.commit()
            result = connection.execute(
                "SELECT * FROM video_review_frames WHERE frame_id = ?",
                (frame_id,),
            ).fetchone()
            return self._frame_payload(result, include_media=True)

    @staticmethod
    def _privacy_summary(connection: sqlite3.Connection, video_id: str) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT COUNT(*) AS frame_count,
                   SUM(CASE WHEN private = 1 THEN 1 ELSE 0 END) AS private_count,
                   (SELECT privacy_default FROM video_review_items WHERE video_id = ?) AS video_private
            FROM video_review_frames
            WHERE video_id = ?
            """,
            (video_id, video_id),
        ).fetchone()
        frame_count = int(row["frame_count"] or 0)
        private_count = int(row["private_count"] or 0)
        privacy_state = (
            "all"
            if frame_count and private_count == frame_count
            else "mixed"
            if private_count
            else "none"
        )
        return {
            "video_id": video_id,
            "frame_count": frame_count,
            "private_count": private_count,
            "all_private": privacy_state == "all",
            "privacy_state": privacy_state,
            "video_private": bool(row["video_private"]),
        }

    @staticmethod
    def _sync_rendered_privacy(final_path: str, private: bool) -> None:
        if not final_path:
            return
        sidecar = Path(final_path).with_suffix(".json")
        if not sidecar.is_file():
            raise ValueError("Rendered screenshot privacy sidecar is missing")
        try:
            payload = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Rendered screenshot privacy sidecar is unreadable") from exc
        if not isinstance(payload, dict):
            raise ValueError("Rendered screenshot privacy sidecar is invalid")
        if private:
            payload["private"] = True
        else:
            payload.pop("private", None)
        temporary = sidecar.with_name(f".{sidecar.name}.partial-{uuid.uuid4().hex}")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True))
            temporary.replace(sidecar)
        finally:
            temporary.unlink(missing_ok=True)

    def set_video_private(self, video_id: str, private: bool) -> dict[str, Any]:
        if type(private) is not bool:
            raise ValueError("private must be a boolean")
        now = utc_now()
        with self._connect() as connection:
            video = connection.execute(
                "SELECT 1 FROM video_review_items WHERE video_id = ?", (video_id,)
            ).fetchone()
            if video is None:
                raise ValueError("Unknown video")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE video_review_frames SET private = ?, updated_at = ? WHERE video_id = ?",
                (1 if private else 0, now, video_id),
            )
            rendered = connection.execute(
                "SELECT final_path FROM video_review_frames WHERE video_id = ? AND final_path <> ''",
                (video_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE video_review_items
                SET privacy_default = ?, review_version = review_version + 1,
                    human_edit_version = human_edit_version + 1,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (1 if private else 0, now, now, video_id),
            )
            summary = self._privacy_summary(connection, video_id)
            connection.commit()
            for row in rendered:
                self._sync_rendered_privacy(str(row["final_path"]), private)
            return summary

    def set_frame_private(self, frame_id: str, private: bool) -> dict[str, Any]:
        if type(private) is not bool:
            raise ValueError("private must be a boolean")
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT video_id, final_path FROM video_review_frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown frame")
            video_id = str(row["video_id"])
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE video_review_frames SET private = ?, updated_at = ? WHERE frame_id = ?",
                (1 if private else 0, now, frame_id),
            )
            summary = self._privacy_summary(connection, video_id)
            connection.execute(
                """
                UPDATE video_review_items
                SET privacy_default = ?, review_version = review_version + 1,
                    human_edit_version = human_edit_version + 1,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (1 if summary["all_private"] else 0, now, now, video_id),
            )
            result = connection.execute(
                "SELECT * FROM video_review_frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            connection.commit()
            self._sync_rendered_privacy(str(row["final_path"]), private)
            payload = self._frame_payload(result)
            payload["video_privacy"] = summary
            return payload

    def save_memory_date(self, video_id: str, memory_date: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT memory_datetime, title, description FROM video_review_items "
                "WHERE video_id = ? AND status = 'ready'",
                (video_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown or unavailable video")
        current_datetime = str(row["memory_datetime"] or "")
        time_text = current_datetime[11:17] if len(current_datetime) >= 17 else "000000"
        result = self.save_video_metadata(
            video_id,
            f"{str(memory_date).strip()} {time_text}",
            str(row["title"]),
            str(row["description"]),
        )
        return {"video_id": video_id, "memory_date": result["memory_date"]}

    @staticmethod
    def _metadata_values(
        memory_datetime: str, title: str, description: str
    ) -> tuple[str, str, str]:
        normalized_datetime = normalize_memory_datetime(memory_datetime)
        normalized_title = str(title or "").strip()
        normalized_description = str(description or "").strip()
        if len(normalized_title) > 500:
            raise ValueError("Title must be 500 characters or fewer")
        if len(normalized_description) > 10_000:
            raise ValueError("Description must be 10000 characters or fewer")
        return normalized_datetime, normalized_title, normalized_description

    def save_video_metadata(
        self,
        video_id: str,
        memory_datetime: str,
        title: str,
        description: str,
        *,
        public_only: bool = False,
    ) -> dict[str, Any]:
        normalized_datetime, normalized_title, normalized_description = self._metadata_values(
            memory_datetime, title, description
        )
        memory_date = normalized_datetime[:10]
        now = utc_now()
        privacy_clause = ""
        if public_only:
            privacy_clause = (
                " AND privacy_default = 0 AND NOT EXISTS ("
                "SELECT 1 FROM video_review_frames private_frame "
                "WHERE private_frame.video_id = video_review_items.video_id "
                "AND private_frame.private = 1)"
            )
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE video_review_items
                SET memory_date = ?, memory_datetime = ?, title = ?, description = ?,
                    memory_date_source = 'user_adjusted',
                    review_version = review_version + 1,
                    human_edit_version = human_edit_version + 1,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ? AND status = 'ready'{privacy_clause}
                """,
                (
                    memory_date,
                    normalized_datetime,
                    normalized_title,
                    normalized_description,
                    now,
                    now,
                    video_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown or unavailable video")
            connection.commit()
        return {
            "video_id": video_id,
            "memory_date": memory_date,
            "memory_datetime": normalized_datetime,
            "title": normalized_title,
            "description": normalized_description,
        }

    def accept_video(
        self,
        video_id: str,
        memory_datetime: str,
        title: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        normalized_datetime, normalized_title, normalized_description = self._metadata_values(
            memory_datetime, title, description
        )
        memory_date = normalized_datetime[:10]
        now = utc_now()
        with self._connect() as connection:
            video = connection.execute(
                """
                SELECT output_screenshots, output_embedded, output_summary
                FROM video_review_items
                WHERE video_id = ? AND status = 'ready'
                """,
                (video_id,),
            ).fetchone()
            if video is None:
                raise ValueError("Unknown or unavailable video")
            output_screenshots = bool(video["output_screenshots"])
            if not any(
                (
                    output_screenshots,
                    bool(video["output_embedded"]),
                    bool(video["output_summary"]),
                )
            ):
                raise ValueError("Select at least one diary output before accepting")
            selected_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM video_review_frames WHERE video_id = ? AND selected = 1",
                    (video_id,),
                ).fetchone()[0]
            )
            if output_screenshots and selected_count == 0:
                raise ValueError("Select at least one screenshot before accepting")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE video_review_items
                SET review_status = 'accepted', memory_date = ?, memory_datetime = ?,
                    title = ?, description = ?,
                    memory_date_source = CASE WHEN ? = capture_date THEN date_source ELSE 'user_adjusted' END,
                    triage_status = 'detail_completed', last_triaged_at = ?,
                    review_version = review_version + 1,
                    human_edit_version = human_edit_version + 1,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (
                    memory_date,
                    normalized_datetime,
                    normalized_title,
                    normalized_description,
                    memory_date,
                    now,
                    now,
                    now,
                    video_id,
                ),
            )
            connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'not_queued', updated_at = ?
                WHERE video_id = ? AND (selected = 0 OR ? = 0) AND render_status = 'queued'
                """,
                (now, video_id, int(output_screenshots)),
            )
            cursor = connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'queued', error = '', updated_at = ?
                WHERE video_id = ? AND selected = 1 AND ? = 1
                  AND render_status IN ('not_queued', 'error')
                """,
                (now, video_id, int(output_screenshots)),
            )
            connection.commit()
        return {
            "video_id": video_id,
            "review_status": "accepted",
            "memory_date": memory_date,
            "memory_datetime": normalized_datetime,
            "title": normalized_title,
            "description": normalized_description,
            "queued": int(cursor.rowcount),
        }

    def reject_video(self, video_id: str, *, public_only: bool = False) -> dict[str, Any]:
        now = utc_now()
        privacy_clause = ""
        if public_only:
            privacy_clause = (
                " AND privacy_default = 0 AND NOT EXISTS ("
                "SELECT 1 FROM video_review_frames private_frame "
                "WHERE private_frame.video_id = video_review_items.video_id "
                "AND private_frame.private = 1)"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE video_review_items
                SET review_status = 'rejected', review_version = review_version + 1,
                    triage_status = 'rejected', last_triaged_at = ?,
                    output_screenshots = 0, output_embedded = 0, output_summary = 0,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ? AND status = 'ready'{privacy_clause}
                """,
                (now, now, now, video_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown or unavailable video")
            connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'not_queued', updated_at = ?
                WHERE video_id = ? AND render_status IN ('queued', 'error')
                """,
                (now, video_id),
            )
            connection.commit()
        return {
            "video_id": video_id,
            "review_status": "rejected",
            "triage_status": "rejected",
            "output_screenshots": False,
            "output_embedded": False,
            "output_summary": False,
            "output_rejected": True,
        }

    def add_captured_frame(
        self,
        video_id: str,
        *,
        time_seconds: float,
        preview_path: Path,
        preview_bytes: int,
        vision_score: float | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            video = connection.execute(
                "SELECT privacy_default FROM video_review_items WHERE video_id = ? AND status = 'ready'",
                (video_id,),
            ).fetchone()
            if video is None:
                raise ValueError("Unknown or unavailable video")
            duplicate = connection.execute(
                "SELECT 1 FROM video_review_frames WHERE video_id = ? AND ABS(time_seconds - ?) < 0.05",
                (video_id, time_seconds),
            ).fetchone()
            if duplicate:
                raise ValueError("A screenshot already exists at nearly this time")
            frame_index = int(
                connection.execute(
                    "SELECT COALESCE(MAX(frame_index), 0) + 1 FROM video_review_frames WHERE video_id = ?",
                    (video_id,),
                ).fetchone()[0]
            )
            frame_id = f"{video_id}:{frame_index:02d}"
            has_main = connection.execute(
                "SELECT 1 FROM video_review_frames WHERE video_id = ? AND is_main = 1",
                (video_id,),
            ).fetchone() is not None
            connection.execute(
                """
                INSERT INTO video_review_frames (
                    frame_id, video_id, frame_index, time_seconds, preview_path,
                    preview_bytes, vision_score, selected, is_main, private, user_captured,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?, ?)
                """,
                (
                    frame_id, video_id, frame_index, time_seconds, str(preview_path),
                    preview_bytes, vision_score, 0 if has_main else 1,
                    int(bool(video["privacy_default"])), now, now,
                ),
            )
            connection.execute(
                "UPDATE video_review_items SET review_version = review_version + 1, "
                "human_edit_version = human_edit_version + 1, "
                "last_reviewed_at = ?, updated_at = ? WHERE video_id = ?",
                (now, now, video_id),
            )
            result = connection.execute(
                "SELECT * FROM video_review_frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            connection.commit()
            return self._frame_payload(result, include_media=True)

    def save_rotation(self, video_id: str, rotation: int, frame_updates: list[dict[str, Any]]) -> None:
        if rotation not in {0, 90, 180, 270}:
            raise ValueError("Rotation must be 0, 90, 180, or 270 degrees")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE video_review_items
                SET rotation_override = ?, review_version = review_version + 1,
                    human_edit_version = human_edit_version + 1,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ? AND status = 'ready'
                """,
                (rotation, now, now, video_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown or unavailable video")
            for frame in frame_updates:
                connection.execute(
                    """
                    UPDATE video_review_frames
                    SET preview_bytes = ?, vision_score = ?, render_status = 'not_queued',
                        final_path = '', error = '', updated_at = ?
                    WHERE frame_id = ? AND video_id = ?
                    """,
                    (frame["preview_bytes"], frame.get("vision_score"), now, frame["frame_id"], video_id),
                )
            connection.commit()

    def queue_selected_frames(self, video_id: str) -> int:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'not_queued', updated_at = ?
                WHERE video_id = ? AND selected = 0 AND render_status = 'queued'
                """,
                (now, video_id),
            )
            cursor = connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'queued', error = '', updated_at = ?
                WHERE video_id = ? AND selected = 1
                  AND render_status IN ('not_queued', 'error')
                """,
                (now, video_id),
            )
            connection.commit()
            return int(cursor.rowcount)

    def queued_render_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM video_review_frames WHERE render_status = 'queued'"
                ).fetchone()[0]
            )

    def completed_ai_video_ids(self) -> set[str]:
        with self._connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT video_id FROM video_review_items "
                    "WHERE status = 'ready' AND ai_method = 'apple_vision'"
                )
            }

    def incomplete_ai_video_ids(self) -> set[str]:
        with self._connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT video_id FROM video_review_items "
                    "WHERE status != 'ready' OR ai_method != 'apple_vision'"
                )
            }

    def assign_output_folder_names(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT video_id, source_path, output_folder_name
                FROM video_review_items
                ORDER BY video_id
                """
            ).fetchall()
            assignments: dict[str, str] = {}
            reserved: dict[str, str] = {}
            pending: list[tuple[str, Path]] = []
            for row in rows:
                video_id = str(row["video_id"])
                source = Path(str(row["source_path"]))
                assigned = str(row["output_folder_name"] or "")
                if not assigned:
                    pending.append((video_id, source))
                    continue
                if Path(assigned).name != assigned or assigned in {".", ".."}:
                    raise ValueError(f"Invalid output folder name for video: {video_id}")
                owner = reserved.setdefault(assigned.casefold(), video_id)
                if owner != video_id:
                    raise ValueError(f"Duplicate persisted output folder name: {assigned}")
                assignments[video_id] = assigned

            stem_groups: dict[str, list[tuple[str, Path]]] = {}
            for video_id, source in pending:
                stem = source.stem.strip()
                if not stem or stem in {".", ".."}:
                    raise ValueError(f"Source video has no usable filename: {source}")
                stem_groups.setdefault(stem.casefold(), []).append((video_id, source))

            def reserve(video_id: str, candidate: str) -> bool:
                owner = reserved.setdefault(candidate.casefold(), video_id)
                if owner != video_id:
                    return False
                assignments[video_id] = candidate
                return True

            for group in stem_groups.values():
                if len(group) == 1:
                    video_id, source = group[0]
                    if reserve(video_id, source.stem.strip()):
                        continue
                name_counts: dict[str, int] = {}
                for _, source in group:
                    name_counts[source.name.casefold()] = name_counts.get(source.name.casefold(), 0) + 1
                for video_id, source in group:
                    if video_id in assignments:
                        continue
                    if name_counts[source.name.casefold()] == 1 and reserve(video_id, source.name):
                        continue
                    parent_label = source.parent.name.strip() or "source"
                    parent_candidate = f"{source.name} — {parent_label}"
                    if reserve(video_id, parent_candidate):
                        continue
                    reserve(video_id, f"{parent_candidate} — {video_id[:8]}")

            connection.execute("BEGIN IMMEDIATE")
            for video_id, folder_name in assignments.items():
                connection.execute(
                    """
                    UPDATE video_review_items
                    SET output_folder_name = ?
                    WHERE video_id = ? AND output_folder_name = ''
                    """,
                    (folder_name, video_id),
                )
            connection.commit()
        return assignments

    def claim_render_items(self, limit: int) -> list[dict[str, Any]]:
        self.assign_output_folder_names()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                WITH selected_frames AS (
                    SELECT frame_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY video_id
                               ORDER BY time_seconds, frame_index
                           ) AS export_sequence,
                           COUNT(*) OVER (PARTITION BY video_id) AS export_count
                    FROM video_review_frames
                    WHERE selected = 1
                )
                SELECT f.*, v.source_path, v.source_sha256, v.source_byte_size,
                       v.source_mtime_ns, v.memory_date AS capture_date,
                       v.memory_date_source AS date_source, v.capture_timestamp,
                       v.memory_datetime, v.title, v.description,
                       v.rotation_override, v.duration_seconds,
                       v.output_folder_name,
                       selected_frames.export_sequence,
                       selected_frames.export_count
                FROM video_review_frames f
                JOIN video_review_items v ON v.video_id = f.video_id
                JOIN selected_frames ON selected_frames.frame_id = f.frame_id
                WHERE f.render_status = 'queued'
                  AND v.output_screenshots = 1
                ORDER BY v.capture_date, f.video_id, selected_frames.export_sequence
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows)
                connection.execute(
                    f"UPDATE video_review_frames SET render_status = 'rendering', updated_at = ? WHERE frame_id IN ({placeholders})",
                    (utc_now(), *(str(row["frame_id"]) for row in rows)),
                )
            connection.commit()
            return [dict(row) for row in rows]

    def rendered_output_items(self) -> list[dict[str, Any]]:
        self.assign_output_folder_names()
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH selected_frames AS (
                    SELECT frame_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY video_id
                               ORDER BY time_seconds, frame_index
                           ) AS export_sequence,
                           COUNT(*) OVER (PARTITION BY video_id) AS export_count
                    FROM video_review_frames
                    WHERE selected = 1
                )
                SELECT f.*, v.source_path, v.rotation_override,
                       v.output_folder_name,
                       selected_frames.export_sequence,
                       selected_frames.export_count
                FROM video_review_frames f
                JOIN video_review_items v ON v.video_id = f.video_id
                LEFT JOIN selected_frames ON selected_frames.frame_id = f.frame_id
                WHERE f.render_status = 'rendered' AND f.final_path <> ''
                ORDER BY v.source_path, selected_frames.export_sequence
                """
            ).fetchall()
        items = [dict(row) for row in rows]
        if any(item["export_sequence"] is None for item in items):
            raise ValueError("A rendered screenshot is no longer selected")
        return items

    def update_rendered_output_paths(
        self, updates: list[tuple[str, str, str]]
    ) -> None:
        if not updates:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for frame_id, old_path, new_path in updates:
                cursor = connection.execute(
                    """
                    UPDATE video_review_frames
                    SET final_path = ?, updated_at = ?
                    WHERE frame_id = ? AND render_status = 'rendered' AND final_path = ?
                    """,
                    (new_path, utc_now(), frame_id, old_path),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Rendered path changed during migration: {frame_id}")
            connection.commit()

    def mark_rendered(self, frame_id: str, final_path: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'rendered', final_path = ?, error = '', updated_at = ?
                WHERE frame_id = ?
                """,
                (str(final_path), utc_now(), frame_id),
            )
            connection.commit()

    def mark_render_failed(self, frame_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'error', error = ?, updated_at = ?
                WHERE frame_id = ?
                """,
                (str(error)[:500], utc_now(), frame_id),
            )
            connection.commit()

    def render_error_details(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT f.frame_id, f.video_id, f.frame_index, f.time_seconds,
                       f.is_main, f.private, v.memory_date, v.memory_datetime,
                       v.duration_seconds
                FROM video_review_frames f
                JOIN video_review_items v ON v.video_id = f.video_id
                WHERE f.render_status = 'error'
                ORDER BY v.memory_datetime, f.video_id, f.frame_index
                """
            ).fetchall()
        errors = []
        for row in rows:
            time_seconds = float(row["time_seconds"])
            duration_seconds = float(row["duration_seconds"] or 0.0)
            near_end = duration_seconds > 0 and duration_seconds - time_seconds <= 0.15
            errors.append(
                {
                    "frame_id": str(row["frame_id"]),
                    "video_id": str(row["video_id"]),
                    "frame_index": int(row["frame_index"]),
                    "time_seconds": time_seconds,
                    "memory_date": str(row["memory_date"]),
                    "memory_datetime": str(row["memory_datetime"]),
                    "role": "key" if bool(row["is_main"]) else "linked",
                    "private": bool(row["private"]),
                    "message": (
                        "The selected time was too close to the end of the video."
                        if near_end
                        else "The JPG renderer could not extract this frame."
                    ),
                }
            )
        return {
            "count": len(errors),
            "video_count": len({item["video_id"] for item in errors}),
            "errors": errors,
        }

    def requeue_render_errors(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE video_review_frames
                SET render_status = 'queued', error = '', updated_at = ?
                WHERE render_status = 'error' AND selected = 1
                  AND EXISTS (
                      SELECT 1 FROM video_review_items v
                      WHERE v.video_id = video_review_frames.video_id
                        AND v.review_status = 'accepted'
                  )
                """,
                (utc_now(),),
            )
            connection.commit()
            return int(cursor.rowcount)

    def _video_payload(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        frames = connection.execute(
            """
            SELECT * FROM video_review_frames
            WHERE video_id = ?
            ORDER BY time_seconds, frame_index
            """,
            (row["video_id"],),
        ).fetchall()
        privacy = self._privacy_summary(connection, str(row["video_id"]))
        return {
            "video_id": row["video_id"],
            "capture_date": row["capture_date"],
            "date_source": row["date_source"],
            "memory_date": row["memory_date"] or row["capture_date"],
            "memory_datetime": row["memory_datetime"]
            or initial_memory_datetime(str(row["capture_timestamp"]), str(row["capture_date"])),
            "memory_date_source": row["memory_date_source"] or row["date_source"],
            "title": str(row["title"] or ""),
            "description": str(row["description"] or ""),
            "review_status": row["review_status"],
            "rating": int(row["rating"]),
            "triage_status": str(row["triage_status"]),
            "last_triaged_at": str(row["last_triaged_at"]),
            "output_screenshots": bool(row["output_screenshots"]),
            "output_embedded": bool(row["output_embedded"]),
            "output_summary": bool(row["output_summary"]),
            "output_rejected": str(row["review_status"]) == "rejected",
            "synopsis_status": str(row["synopsis_status"]),
            "synopsis_url": (
                synopsis_media_url(
                    str(row["video_id"]), str(row["synopsis_updated_at"])
                )
                if str(row["synopsis_status"]) == "ready" and str(row["synopsis_path"])
                else ""
            ),
            "synopsis_error": str(row["synopsis_error"]),
            "rotation_override": int(row["rotation_override"]),
            "duration_seconds": row["duration_seconds"],
            "ai_status": row["ai_status"],
            "ai_method": row["ai_method"],
            "ai_model": row["ai_model"],
            "review_version": row["review_version"],
            "human_edit_version": int(row["human_edit_version"]),
            "duplicate_group_id": str(row["duplicate_group_id"]),
            "duplicate_state": str(row["duplicate_state"]),
            "duplicate_of_video_id": str(row["duplicate_of_video_id"]),
            **privacy,
            "video_url": f"/media/video/{urllib.parse.quote(str(row['video_id']))}",
            "frames": [self._frame_payload(frame, include_media=True) for frame in frames],
        }

    def video_detail(self, video_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM video_review_items WHERE video_id = ? AND status = 'ready'",
                (video_id,),
            ).fetchone()
            return self._video_payload(connection, row) if row is not None else None

    def require_triage_eligible(self, video_id: str, *, allow_private: bool = False) -> None:
        privacy_clause = ""
        if not allow_private:
            privacy_clause = (
                " AND v.privacy_default = 0 AND NOT EXISTS ("
                "SELECT 1 FROM video_review_frames private_frame "
                "WHERE private_frame.video_id = v.video_id "
                "AND private_frame.private = 1)"
            )
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT 1
                FROM video_review_items v
                WHERE v.video_id = ? AND v.status = 'ready'{privacy_clause}
                """,
                (video_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Video is unknown or unavailable for rapid triage")

    def update_triage(
        self,
        video_ids: Iterable[str],
        *,
        rating: int | None = None,
        triage_status: str | None = None,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        identifiers = list(dict.fromkeys(str(value).strip() for value in video_ids if str(value).strip()))
        if not identifiers:
            raise ValueError("Select at least one video")
        if len(identifiers) > 500:
            raise ValueError("At most 500 videos can be updated at once")
        if rating is None and triage_status is None:
            raise ValueError("Provide a rating or triage status")
        if rating is not None and (isinstance(rating, bool) or not 0 <= int(rating) <= 5):
            raise ValueError("Rating must be from 0 to 5")
        if triage_status is not None and triage_status not in TRIAGE_STATES:
            raise ValueError("Unknown triage status")
        assignments: list[str] = []
        values: list[Any] = []
        if rating is not None:
            assignments.append("rating = ?")
            values.append(int(rating))
        if triage_status is not None:
            assignments.append("triage_status = ?")
            values.append(triage_status)
            if triage_status == "rejected":
                assignments.extend(
                    (
                        "review_status = 'rejected'",
                        "output_screenshots = 0",
                        "output_embedded = 0",
                        "output_summary = 0",
                    )
                )
            else:
                assignments.append(
                    "review_status = CASE WHEN review_status = 'rejected' "
                    "THEN 'pending' ELSE review_status END"
                )
                assignments.extend(
                    (
                        "output_screenshots = ?",
                        "output_embedded = ?",
                        "output_summary = ?",
                    )
                )
                values.extend(
                    (
                        int(triage_status in {"detail_wanted", "detail_completed"}),
                        int(triage_status == "no_detail"),
                        int(triage_status == "summary_wanted"),
                    )
                )
        marks_human_edit = rating is not None or triage_status not in {None, "rejected"}
        assignments.append("human_edit_version = human_edit_version + ?")
        values.append(int(marks_human_edit))
        now = utc_now()
        assignments.extend(("last_triaged_at = ?", "updated_at = ?"))
        values.extend((now, now))
        placeholders = ",".join("?" for _ in identifiers)
        availability_clause = (
            " AND review_status != 'accepted'" if triage_status == "not_triaged" else ""
        )
        privacy_clause = ""
        if not allow_private:
            privacy_clause = (
                " AND privacy_default = 0 AND NOT EXISTS ("
                "SELECT 1 FROM video_review_frames private_frame "
                "WHERE private_frame.video_id = video_review_items.video_id "
                "AND private_frame.private = 1)"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE video_review_items SET {', '.join(assignments)} "
                f"WHERE status = 'ready'{privacy_clause}{availability_clause} "
                f"AND video_id IN ({placeholders})",
                (*values, *identifiers),
            )
            if cursor.rowcount != len(identifiers):
                raise ValueError("One or more videos are unknown or unavailable")
            if triage_status is not None and triage_status not in {
                "detail_wanted",
                "detail_completed",
            }:
                connection.execute(
                    f"UPDATE video_review_frames SET render_status = 'not_queued', updated_at = ? "
                    f"WHERE video_id IN ({placeholders}) AND render_status IN ('queued', 'error')",
                    (now, *identifiers),
                )
            connection.commit()
        return {
            "updated_count": int(cursor.rowcount),
            "video_ids": identifiers,
            "rating": int(rating) if rating is not None else None,
            "triage_status": triage_status,
            "updated_at": now,
        }

    def update_output_options(
        self,
        video_id: str,
        *,
        screenshots: bool | None = None,
        embedded: bool | None = None,
        summary: bool | None = None,
        rejected: bool | None = None,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        changes = {
            "screenshots": screenshots,
            "embedded": embedded,
            "summary": summary,
            "rejected": rejected,
        }
        if all(value is None for value in changes.values()):
            raise ValueError("Provide at least one diary output option")
        for name, value in changes.items():
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")

        privacy_clause = ""
        if not allow_private:
            privacy_clause = (
                " AND privacy_default = 0 AND NOT EXISTS ("
                "SELECT 1 FROM video_review_frames private_frame "
                "WHERE private_frame.video_id = video_review_items.video_id "
                "AND private_frame.private = 1)"
            )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT review_status, output_screenshots, output_embedded, output_summary
                FROM video_review_items
                WHERE video_id = ? AND status = 'ready'{privacy_clause}
                """,
                (video_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Video is unknown or unavailable for diary output selection")

            selected = {
                "screenshots": bool(row["output_screenshots"]),
                "embedded": bool(row["output_embedded"]),
                "summary": bool(row["output_summary"]),
            }
            for name in selected:
                if changes[name] is not None:
                    selected[name] = bool(changes[name])

            review_status = str(row["review_status"])
            if rejected is True:
                selected = {name: False for name in selected}
                review_status = "rejected"
                triage_status = "rejected"
            else:
                if any(selected.values()) and review_status == "rejected":
                    review_status = "pending"
                elif rejected is False and review_status == "rejected":
                    review_status = "pending"
                if review_status == "accepted":
                    if not any(selected.values()):
                        raise ValueError("An accepted video requires at least one diary output")
                    triage_status = "detail_completed"
                elif screenshots is True:
                    triage_status = "detail_wanted"
                elif summary is True:
                    triage_status = "summary_wanted"
                elif embedded is True:
                    triage_status = "no_detail"
                elif selected["screenshots"]:
                    triage_status = "detail_wanted"
                elif selected["summary"]:
                    triage_status = "summary_wanted"
                elif selected["embedded"]:
                    triage_status = "no_detail"
                else:
                    triage_status = "not_triaged"

            connection.execute(
                """
                UPDATE video_review_items
                SET output_screenshots = ?, output_embedded = ?, output_summary = ?,
                    review_status = ?, triage_status = ?, last_triaged_at = ?,
                    review_version = review_version + 1,
                    human_edit_version = human_edit_version + ?,
                    last_reviewed_at = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (
                    int(selected["screenshots"]),
                    int(selected["embedded"]),
                    int(selected["summary"]),
                    review_status,
                    triage_status,
                    now,
                    int(not (rejected is True and screenshots is None and embedded is None and summary is None)),
                    now,
                    now,
                    video_id,
                ),
            )
            if not selected["screenshots"]:
                connection.execute(
                    """
                    UPDATE video_review_frames
                    SET render_status = 'not_queued', updated_at = ?
                    WHERE video_id = ? AND render_status IN ('queued', 'error')
                    """,
                    (now, video_id),
                )
            elif review_status == "accepted":
                connection.execute(
                    """
                    UPDATE video_review_frames
                    SET render_status = 'queued', error = '', updated_at = ?
                    WHERE video_id = ? AND selected = 1
                      AND render_status IN ('not_queued', 'error')
                    """,
                    (now, video_id),
                )
            connection.commit()

        return {
            "video_id": video_id,
            "review_status": review_status,
            "triage_status": triage_status,
            "output_screenshots": selected["screenshots"],
            "output_embedded": selected["embedded"],
            "output_summary": selected["summary"],
            "output_rejected": review_status == "rejected",
            "updated_at": now,
        }

    def set_synopsis_state(
        self,
        video_id: str,
        status: str,
        *,
        path: Path | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        if status not in {"not_requested", "queued", "generating", "ready", "source_preferred", "error"}:
            raise ValueError("Unknown synopsis status")
        synopsis_path = str(path) if path is not None else ""
        if status == "ready" and not synopsis_path:
            raise ValueError("Ready synopsis requires a path")
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE video_review_items
                SET synopsis_status = ?, synopsis_path = ?, synopsis_error = ?,
                    synopsis_updated_at = ?, updated_at = ?
                WHERE video_id = ? AND status = 'ready'
                """,
                (status, synopsis_path, str(error)[:1000], now, now, video_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown or unavailable video")
        return {
            "video_id": video_id,
            "status": status,
            "url": synopsis_media_url(video_id, now) if status == "ready" else "",
            "error": str(error)[:1000],
            "updated_at": now,
        }

    def synopsis_record(self, video_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT video_id, synopsis_status, synopsis_path, synopsis_error,
                       synopsis_updated_at
                FROM video_review_items
                WHERE video_id = ? AND status = 'ready'
                """,
                (video_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def triage_page(
        self,
        *,
        limit: int = TRIAGE_DEFAULT_PAGE_SIZE,
        offset: int = 0,
        start_date: str = "",
        end_date: str = "",
        minimum_rating: int = 0,
        triage_status: str = "not_triaged",
        minimum_duration: float = 0.0,
        maximum_duration: float = 0.0,
        search: str = "",
        sort: str = "chronological",
        show_private: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), TRIAGE_MAX_PAGE_SIZE))
        offset = max(0, int(offset))
        minimum_rating = int(minimum_rating)
        if not 0 <= minimum_rating <= 5:
            raise ValueError("Minimum rating must be from 0 to 5")
        if triage_status not in {"all", "problems", *TRIAGE_STATES}:
            raise ValueError("Unknown triage status filter")
        minimum_duration = max(0.0, float(minimum_duration or 0.0))
        maximum_duration = max(0.0, float(maximum_duration or 0.0))
        if maximum_duration and maximum_duration < minimum_duration:
            raise ValueError("Maximum duration must not be less than minimum duration")
        search = str(search or "").strip()[:200]
        for label, value in (("start", start_date), ("end", end_date)):
            if value:
                try:
                    dt.date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(f"Invalid {label} date") from exc
        if start_date and end_date and start_date > end_date:
            raise ValueError("End date must not be before start date")
        sort_sql = {
            "chronological": "v.memory_datetime ASC, v.video_id ASC",
            "reverse_chronological": "v.memory_datetime DESC, v.video_id DESC",
            "rating_desc": "v.rating DESC, v.memory_datetime ASC, v.video_id ASC",
            "duration_asc": "v.duration_seconds ASC, v.memory_datetime ASC, v.video_id ASC",
            "duration_desc": "v.duration_seconds DESC, v.memory_datetime ASC, v.video_id ASC",
            "review_status": (
                "CASE v.triage_status WHEN 'not_triaged' THEN 0 WHEN 'summary_wanted' THEN 1 "
                "WHEN 'detail_wanted' THEN 2 WHEN 'no_detail' THEN 3 "
                "WHEN 'rejected' THEN 4 WHEN 'detail_completed' THEN 5 ELSE 6 END, "
                "v.memory_datetime ASC, v.video_id ASC"
            ),
        }.get(sort)
        if sort_sql is None:
            raise ValueError("Unknown triage sort")
        if triage_status == "problems":
            sort_sql = "v.duplicate_group_id ASC, v.created_at ASC, v.video_id ASC"
        clauses = ["v.status = 'ready'"]
        if not show_private:
            clauses.extend(
                (
                    "v.privacy_default = 0",
                    "NOT EXISTS (SELECT 1 FROM video_review_frames private_frame "
                    "WHERE private_frame.video_id = v.video_id AND private_frame.private = 1)",
                )
            )
        parameters: list[Any] = []
        if triage_status == "problems":
            clauses.append("v.duplicate_state = 'problem'")
        elif triage_status != "all":
            clauses.append("v.triage_status = ?")
            parameters.append(triage_status)
        if start_date:
            clauses.append("v.capture_date >= ?")
            parameters.append(start_date)
        if end_date:
            clauses.append("v.capture_date <= ?")
            parameters.append(end_date)
        if minimum_rating:
            clauses.append("v.rating >= ?")
            parameters.append(minimum_rating)
        if minimum_duration:
            clauses.append("v.duration_seconds >= ?")
            parameters.append(minimum_duration)
        if maximum_duration:
            clauses.append("v.duration_seconds <= ?")
            parameters.append(maximum_duration)
        if search:
            clauses.append("(LOWER(v.title) LIKE ? OR LOWER(v.description) LIKE ? OR LOWER(v.source_path) LIKE ?)")
            pattern = f"%{search.lower()}%"
            parameters.extend((pattern, pattern, pattern))
        where_sql = " AND ".join(clauses)
        with self._connect() as connection:
            total_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM video_review_items v WHERE {where_sql}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT v.video_id, v.source_path, v.capture_date, v.capture_timestamp,
                       v.memory_datetime, v.duration_seconds, v.review_status,
                       v.rating, v.triage_status, v.last_triaged_at,
                       v.output_screenshots, v.output_embedded, v.output_summary,
                       v.title, v.description, v.rotation_override,
                       v.privacy_default,
                       v.ai_status, v.ai_method,
                       v.synopsis_status, v.synopsis_path, v.synopsis_error,
                       v.synopsis_updated_at
                       , v.duplicate_group_id, v.duplicate_state, v.duplicate_of_video_id
                FROM video_review_items v
                WHERE {where_sql}
                ORDER BY {sort_sql}
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            identifiers = [str(row["video_id"]) for row in rows]
            frames_by_video: dict[str, list[dict[str, Any]]] = {value: [] for value in identifiers}
            if identifiers:
                placeholders = ",".join("?" for _ in identifiers)
                frame_rows = connection.execute(
                    f"""
                    SELECT frame_id, video_id, frame_index, time_seconds, updated_at,
                           is_main, private
                    FROM video_review_frames
                    WHERE video_id IN ({placeholders})
                    ORDER BY video_id, time_seconds, frame_index
                    """,
                    identifiers,
                ).fetchall()
                for frame in frame_rows:
                    version = urllib.parse.quote(str(frame["updated_at"] or "1"))
                    frames_by_video[str(frame["video_id"])].append(
                        {
                            "frame_id": str(frame["frame_id"]),
                            "frame_index": int(frame["frame_index"]),
                            "time_seconds": float(frame["time_seconds"]),
                            "is_main": bool(frame["is_main"]),
                            "private": bool(frame["private"]),
                            "image_url": (
                                f"/media/frame/{urllib.parse.quote(str(frame['frame_id']))}?v={version}"
                            ),
                        }
                    )
            summary_privacy_clause = ""
            if not show_private:
                summary_privacy_clause = (
                    " AND v.privacy_default = 0 AND NOT EXISTS ("
                    "SELECT 1 FROM video_review_frames private_frame "
                    "WHERE private_frame.video_id = v.video_id "
                    "AND private_frame.private = 1)"
                )
            summary_row = connection.execute(
                f"""
                SELECT COUNT(*) AS ready,
                       SUM(CASE WHEN triage_status = 'not_triaged' THEN 1 ELSE 0 END) AS not_triaged,
                       SUM(CASE WHEN triage_status = 'no_detail' THEN 1 ELSE 0 END) AS no_detail,
                       SUM(CASE WHEN triage_status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                       SUM(CASE WHEN triage_status = 'summary_wanted' THEN 1 ELSE 0 END) AS summary_wanted,
                       SUM(CASE WHEN triage_status = 'detail_wanted' THEN 1 ELSE 0 END) AS detail_wanted,
                       SUM(CASE WHEN triage_status = 'detail_completed' THEN 1 ELSE 0 END) AS detail_completed,
                       SUM(CASE WHEN duplicate_state = 'problem' THEN 1 ELSE 0 END) AS problems
                FROM video_review_items v
                WHERE v.status = 'ready'{summary_privacy_clause}
                """
            ).fetchone()
        items = []
        for row in rows:
            frames = frames_by_video[str(row["video_id"])]
            private_count = sum(1 for frame in frames if frame["private"])
            frame_count = len(frames)
            video_private = bool(row["privacy_default"])
            all_private = video_private and private_count == frame_count
            privacy_state = "all" if all_private else "mixed" if video_private or private_count else "none"
            items.append({
                "video_id": str(row["video_id"]),
                "filename": Path(str(row["source_path"])).name,
                "inferred_title": inferred_video_title(
                    Path(str(row["source_path"])).name
                ),
                "capture_date": str(row["capture_date"]),
                "capture_timestamp": str(row["capture_timestamp"]),
                "memory_datetime": str(row["memory_datetime"]),
                "duration_seconds": float(row["duration_seconds"]),
                "review_status": str(row["review_status"]),
                "rating": int(row["rating"]),
                "triage_status": str(row["triage_status"]),
                "last_triaged_at": str(row["last_triaged_at"]),
                "output_screenshots": bool(row["output_screenshots"]),
                "output_embedded": bool(row["output_embedded"]),
                "output_summary": bool(row["output_summary"]),
                "output_rejected": str(row["review_status"]) == "rejected",
                "title": str(row["title"] or ""),
                "description": str(row["description"] or ""),
                "rotation_override": int(row["rotation_override"]),
                "ai_status": str(row["ai_status"]),
                "ai_method": str(row["ai_method"]),
                "synopsis_status": str(row["synopsis_status"]),
                "synopsis_url": (
                    synopsis_media_url(
                        str(row["video_id"]), str(row["synopsis_updated_at"])
                    )
                    if str(row["synopsis_status"]) == "ready" and str(row["synopsis_path"])
                    else ""
                ),
                "synopsis_error": str(row["synopsis_error"]),
                "duplicate_group_id": str(row["duplicate_group_id"]),
                "duplicate_state": str(row["duplicate_state"]),
                "duplicate_of_video_id": str(row["duplicate_of_video_id"]),
                "video_private": video_private,
                "frame_count": frame_count,
                "private_count": private_count,
                "all_private": all_private,
                "privacy_state": privacy_state,
                "frames": frames,
            })
        summary = {
            key: int(summary_row[key] or 0)
            for key in ("ready", *TRIAGE_STATES, "problems")
        }
        return {
            "offset": offset,
            "limit": limit,
            "total_count": total_count,
            "returned_count": len(items),
            "has_more": offset + len(items) < total_count,
            "items": items,
            "summary": summary,
        }

    def video_queue_month(
        self,
        review_status: str = "pending",
        selected_month: str = "",
    ) -> dict[str, Any]:
        if review_status not in {"pending", "reviewed", "accepted", "rejected", "all"}:
            raise ValueError("Unknown review status filter")
        selected_month = str(selected_month or "").strip()
        if selected_month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", selected_month):
            raise ValueError("month must be yyyy-mm")
        if review_status == "all":
            status_clause = ""
            status_parameters: tuple[Any, ...] = ()
        elif review_status == "reviewed":
            status_clause = " AND v.review_version > 0"
            status_parameters = ()
        else:
            status_clause = " AND v.review_status = ?"
            status_parameters = (review_status,)
        with self._connect() as connection:
            month_rows = connection.execute(
                f"""
                SELECT substr(v.memory_datetime, 1, 7) AS month, COUNT(*) AS count
                FROM video_review_items v
                WHERE v.status = 'ready'{status_clause}
                GROUP BY substr(v.memory_datetime, 1, 7)
                ORDER BY month
                """,
                status_parameters,
            ).fetchall()
            month_counts = [
                {"month": str(row["month"]), "count": int(row["count"])}
                for row in month_rows
            ]
            months = [item["month"] for item in month_counts]
            if not selected_month and review_status == "accepted":
                recent_month_row = connection.execute(
                    """
                    SELECT substr(memory_datetime, 1, 7) AS month
                    FROM video_review_items
                    WHERE status = 'ready' AND review_status = 'accepted'
                    ORDER BY updated_at DESC, video_id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if recent_month_row is not None:
                    selected_month = str(recent_month_row["month"])
            if not selected_month and review_status == "reviewed":
                recent_month_row = connection.execute(
                    """
                    SELECT substr(memory_datetime, 1, 7) AS month
                    FROM video_review_items
                    WHERE status = 'ready' AND review_version > 0
                    ORDER BY last_reviewed_at DESC, video_id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if recent_month_row is not None:
                    selected_month = str(recent_month_row["month"])
            if selected_month not in months:
                selected_month = months[0] if months else ""
            selected_parameters = (*status_parameters, selected_month)
            rows = connection.execute(
                f"""
                SELECT v.video_id, v.memory_datetime, v.capture_date, v.duration_seconds,
                       v.review_status, v.title, v.description,
                       f.frame_id AS key_frame_id, f.updated_at AS key_frame_updated_at
                FROM video_review_items v
                LEFT JOIN video_review_frames f
                  ON f.video_id = v.video_id AND f.is_main = 1
                WHERE v.status = 'ready'{status_clause}
                  AND substr(v.memory_datetime, 1, 7) = ?
                ORDER BY v.memory_datetime, v.capture_date, v.video_id
                """,
                selected_parameters,
            ).fetchall()
        entries = []
        for row in rows:
            frame_id = str(row["key_frame_id"] or "")
            version = urllib.parse.quote(str(row["key_frame_updated_at"] or "1"))
            entries.append(
                {
                    "video_id": str(row["video_id"]),
                    "memory_datetime": str(row["memory_datetime"]),
                    "capture_date": str(row["capture_date"]),
                    "duration_seconds": float(row["duration_seconds"]),
                    "review_status": str(row["review_status"]),
                    "title": str(row["title"] or ""),
                    "description": str(row["description"] or ""),
                    "key_frame_url": (
                        f"/media/frame/{urllib.parse.quote(frame_id)}?v={version}" if frame_id else ""
                    ),
                }
            )
        selected_index = months.index(selected_month) if selected_month else -1
        return {
            "review_status": review_status,
            "total_count": sum(item["count"] for item in month_counts),
            "loaded_count": len(entries),
            "month_counts": month_counts,
            "selected_month": selected_month,
            "previous_month": months[selected_index - 1] if selected_index > 0 else "",
            "next_month": months[selected_index + 1]
            if 0 <= selected_index + 1 < len(months) else "",
            "entries": entries,
        }

    def video_page(
        self,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        review_status: str = "pending",
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        offset = max(0, int(offset))
        if review_status not in {"pending", "reviewed", "accepted", "rejected", "all"}:
            raise ValueError("Unknown review status filter")
        if review_status == "all":
            status_clause = ""
            status_parameters: tuple[Any, ...] = ()
        elif review_status == "reviewed":
            status_clause = " AND review_version > 0"
            status_parameters = ()
        else:
            status_clause = " AND review_status = ?"
            status_parameters = (review_status,)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM video_review_items WHERE status = 'ready'{status_clause}",
                    status_parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM video_review_items
                WHERE status = 'ready'{status_clause}
                ORDER BY capture_date, video_id
                LIMIT ? OFFSET ?
                """,
                (*status_parameters, limit, offset),
            ).fetchall()
            videos = [self._video_payload(connection, row) for row in rows]
            return {
                "offset": offset,
                "limit": limit,
                "review_status": review_status,
                "total_count": total,
                "returned_count": len(videos),
                "has_more": offset + len(videos) < total,
                "videos": videos,
            }

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS videos,
                    SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS ready,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                    SUM(CASE WHEN status = 'ready' AND review_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'ready' AND review_status = 'accepted' THEN 1 ELSE 0 END) AS accepted,
                    SUM(CASE WHEN status = 'ready' AND review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected
                FROM video_review_items
                """
            ).fetchone()
            render_rows = connection.execute(
                """
                SELECT render_status, COUNT(*) count,
                       COUNT(DISTINCT video_id) video_count
                FROM video_review_frames
                GROUP BY render_status
                """
            ).fetchall()
            renders = {str(item["render_status"]): int(item["count"]) for item in render_rows}
            render_videos = {
                str(item["render_status"]): int(item["video_count"])
                for item in render_rows
            }
            return {
                "videos": int(row["videos"] or 0),
                "ready": int(row["ready"] or 0),
                "errors": int(row["errors"] or 0),
                "pending": int(row["pending"] or 0),
                "accepted": int(row["accepted"] or 0),
                "rejected": int(row["rejected"] or 0),
                "queued_renders": renders.get("queued", 0),
                "queued_render_videos": render_videos.get("queued", 0),
                "rendering": renders.get("rendering", 0),
                "rendered": renders.get("rendered", 0),
                "render_errors": renders.get("error", 0),
            }

    def frame_record(self, frame_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM video_review_frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            return dict(row) if row else None

    def video_record(self, video_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM video_review_items WHERE video_id = ?", (video_id,)
            ).fetchone()
            return dict(row) if row else None

    def mark_video_error(self, video_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE video_review_items SET status = 'error', error = ?, updated_at = ? WHERE video_id = ?",
                (str(error)[:500], utc_now(), video_id),
            )
            connection.commit()

    @staticmethod
    def _frame_payload(row: sqlite3.Row, include_media: bool = False) -> dict[str, Any]:
        payload = {
            "frame_id": str(row["frame_id"]),
            "video_id": str(row["video_id"]),
            "frame_index": int(row["frame_index"]) if "frame_index" in row.keys() else 0,
            "time_seconds": float(row["time_seconds"]) if "time_seconds" in row.keys() else 0.0,
            "vision_score": row["vision_score"] if "vision_score" in row.keys() else None,
            "ai_selected": bool(row["ai_selected"]),
            "selected": bool(row["selected"]),
            "is_main": bool(row["is_main"]),
            "private": bool(row["private"]) if "private" in row.keys() else False,
            "user_captured": bool(row["user_captured"]) if "user_captured" in row.keys() else False,
            "render_status": str(row["render_status"]) if "render_status" in row.keys() else "not_queued",
        }
        if include_media:
            version = urllib.parse.quote(str(row["updated_at"])) if "updated_at" in row.keys() else "1"
            payload["image_url"] = (
                f"/media/frame/{urllib.parse.quote(str(row['frame_id']))}?v={version}"
            )
        return payload


def _valid_date(value: str, today: dt.date) -> dt.date | None:
    try:
        parsed = dt.date.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed < dt.date(1980, 1, 1) or parsed > today:
        return None
    return parsed


def _preferred_date(values: list[tuple[str, str]], today: dt.date) -> tuple[str, str] | None:
    priority = {
        "filename_date": 0,
        "media_creation_date": 1,
        "filesystem_creation_date": 2,
        "filesystem_modified_date": 3,
    }
    candidates = []
    for value, source in values:
        parsed = _valid_date(value, today)
        if parsed is not None:
            candidates.append((priority.get(source, 99), parsed, source))
    if not candidates:
        return None
    _, parsed, source = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    return parsed.isoformat(), source


def _read_index_videos(index_path: Path, today: dt.date) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"{index_path.resolve().as_uri()}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        dates: dict[str, list[tuple[str, str]]] = {}
        for row in connection.execute(
            "SELECT file_path, date, source FROM photo_library_dates ORDER BY file_path, source, date"
        ):
            dates.setdefault(str(row["file_path"]), []).append((str(row["date"]), str(row["source"])))
        extension_placeholders = ", ".join("?" for _ in VIDEO_EXTENSIONS)
        rows = connection.execute(
            f"""
            SELECT path, filename, extension, byte_size, filesystem_mtime_ns,
                   capture_timestamp, capture_timestamp_source, sha256,
                   media_width, media_height, media_duration_seconds
            FROM photo_library_files
            WHERE lower(extension) IN ({extension_placeholders})
            ORDER BY path
            """,
            tuple(sorted(VIDEO_EXTENSIONS)),
        ).fetchall()
    finally:
        connection.close()
    videos = []
    for row in rows:
        path = Path(str(row["path"]))
        if not path.is_file():
            continue
        chosen = _preferred_date(dates.get(str(path), []), today)
        if chosen is None:
            capture_text = str(row["capture_timestamp"] or "")[:10]
            parsed = _valid_date(capture_text, today)
            if parsed is None:
                continue
            chosen = (parsed.isoformat(), str(row["capture_timestamp_source"] or "capture_timestamp"))
        sha256 = str(row["sha256"] or "").lower()
        if len(sha256) != 64:
            identity = f"{path}\0{row['byte_size']}\0{row['filesystem_mtime_ns']}"
            sha256 = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        videos.append(
            {
                "video_id": sha256,
                "source_path": str(path),
                "source_sha256": sha256,
                "source_byte_size": int(row["byte_size"] or path.stat().st_size),
                "source_mtime_ns": int(row["filesystem_mtime_ns"] or path.stat().st_mtime_ns),
                "capture_date": chosen[0],
                "date_source": chosen[1],
                "capture_timestamp": str(row["capture_timestamp"] or ""),
                "duration_seconds": float(row["media_duration_seconds"] or 0.0),
                "media_width": int(row["media_width"] or 0),
                "media_height": int(row["media_height"] or 0),
            }
        )
    return videos


def select_pilot_videos(
    index_paths: list[Path],
    count: int = 8,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    if count <= 0:
        raise ValueError("count must be positive")
    today = today or dt.date.today()
    by_hash: dict[str, dict[str, Any]] = {}
    for index_path in index_paths:
        for video in _read_index_videos(Path(index_path), today):
            existing = by_hash.get(str(video["source_sha256"]))
            if existing is None:
                by_hash[str(video["source_sha256"])] = video
                continue
            existing_area = int(existing.get("media_width") or 0) * int(existing.get("media_height") or 0)
            video_area = int(video.get("media_width") or 0) * int(video.get("media_height") or 0)
            if video_area > existing_area:
                by_hash[str(video["source_sha256"])] = video
    ordered = sorted(by_hash.values(), key=lambda item: (str(item["capture_date"]), str(item["video_id"])))
    if len(ordered) <= count:
        return ordered
    indices = [round(position * (len(ordered) - 1) / (count - 1)) for position in range(count)] if count > 1 else [len(ordered) // 2]
    return [ordered[index] for index in indices]


def select_batch_candidates(
    candidates: list[dict[str, Any]],
    *,
    excluded_video_ids: set[str],
    count: int,
) -> list[dict[str, Any]]:
    """Put a time-spread batch first, followed by deterministic failure fallbacks."""
    if count <= 0:
        raise ValueError("count must be positive")
    available = [
        item for item in candidates
        if str(item["video_id"]) not in excluded_video_ids
    ]
    if len(available) < count:
        raise ValueError(f"Only {len(available)} unprepared videos are available")
    if len(available) == count:
        return available
    indices = {
        round(position * (len(available) - 1) / (count - 1))
        for position in range(count)
    } if count > 1 else {len(available) // 2}
    preferred = [item for index, item in enumerate(available) if index in indices]
    fallbacks = [item for index, item in enumerate(available) if index not in indices]
    return preferred + fallbacks


def _batch_date_bound(value: str, *, end: bool) -> dt.date:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return dt.date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("Date scope must use a valid yyyy-mm-dd or yyyy-mm value") from exc
    if re.fullmatch(r"\d{4}-\d{2}", text):
        try:
            first = dt.date.fromisoformat(f"{text}-01")
        except ValueError as exc:
            raise ValueError("Date scope must use a valid yyyy-mm-dd or yyyy-mm value") from exc
        if not end:
            return first
        next_month = (
            dt.date(first.year + 1, 1, 1)
            if first.month == 12
            else dt.date(first.year, first.month + 1, 1)
        )
        return next_month - dt.timedelta(days=1)
    raise ValueError("Date scope must use yyyy-mm-dd or yyyy-mm")


def normalize_batch_date_scope(start_date: str = "", end_date: str = "") -> tuple[str, str]:
    """Normalize optional inclusive capture-date bounds for batch preparation."""
    start_text = str(start_date or "").strip()
    end_text = str(end_date or "").strip()
    if not start_text and not end_text:
        return "", ""
    if not start_text:
        raise ValueError("Enter a start date before an end date")
    normalized_start = _batch_date_bound(start_text, end=False)
    normalized_end = (
        _batch_date_bound(end_text, end=True)
        if end_text
        else _batch_date_bound(start_text, end=True)
    )
    if normalized_start > normalized_end:
        raise ValueError("Start date must be on or before end date")
    return normalized_start.isoformat(), normalized_end.isoformat()


def filter_batch_candidates_by_date(
    candidates: list[dict[str, Any]],
    *,
    start_date: str = "",
    end_date: str = "",
) -> list[dict[str, Any]]:
    normalized_start, normalized_end = normalize_batch_date_scope(start_date, end_date)
    if not normalized_start:
        return list(candidates)
    return [
        item
        for item in candidates
        if normalized_start <= str(item.get("capture_date", "")) <= normalized_end
    ]


PILOT_STRATA = (
    (1980, 2004, 0.0, 30.0, 0, 640),
    (2005, 2009, 30.0, 180.0, 0, 0),
    (2010, 2012, 180.0, 600.0, 0, 0),
    (2013, 2014, 600.0, 3600.0, 0, 0),
    (2015, 2017, 0.0, 30.0, 0, 0),
    (2018, 2019, 30.0, 180.0, 0, 0),
    (2020, 2100, 180.0, 600.0, 1921, 0),
    (2020, 2100, 3600.0, float("inf"), 0, 0),
)


def select_stratified_pilot_videos(
    index_paths: list[Path],
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Choose eight deterministic era/duration/resolution strata for the pilot."""
    today = today or dt.date.today()
    candidates = select_pilot_videos(index_paths, count=1_000_000, today=today)
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for start_year, end_year, min_duration, max_duration, min_edge, max_edge in PILOT_STRATA:
        eligible = []
        for item in candidates:
            video_id = str(item["video_id"])
            if video_id in used:
                continue
            year = int(str(item["capture_date"])[:4])
            duration = float(item.get("duration_seconds") or 0.0)
            long_edge = max(int(item.get("media_width") or 0), int(item.get("media_height") or 0))
            if not (start_year <= year <= min(end_year, today.year)):
                continue
            if not (min_duration < duration <= max_duration):
                continue
            if min_edge and long_edge < min_edge:
                continue
            if max_edge and long_edge > max_edge:
                continue
            eligible.append(item)
        if not eligible:
            continue
        middle_year = (start_year + min(end_year, today.year)) / 2
        chosen = min(
            eligible,
            key=lambda item: (
                abs(int(str(item["capture_date"])[:4]) - middle_year),
                abs(float(item.get("duration_seconds") or 0.0) - min(max_duration, 600.0) / 2),
                str(item["video_id"]),
            ),
        )
        selected.append(chosen)
        used.add(str(chosen["video_id"]))
    if len(selected) < 8:
        for item in select_pilot_videos(index_paths, count=8, today=today):
            if str(item["video_id"]) in used:
                continue
            selected.append(item)
            used.add(str(item["video_id"]))
            if len(selected) == 8:
                break
    if len(selected) != 8:
        raise ValueError(f"Could only select {len(selected)} available pilot videos")
    return selected


def _run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _magick() -> str:
    command = shutil.which("magick")
    if command:
        return command
    homebrew = Path("/opt/homebrew/bin/magick")
    if homebrew.is_file():
        return str(homebrew)
    raise ValueError("ImageMagick is required for duplicate video fingerprints")


def content_fingerprint_for_previews(samples: list[tuple[int, Path]]) -> bytes:
    ordered = sorted((int(index), Path(path)) for index, path in samples)
    if len(ordered) < 8:
        raise ValueError("At least eight prepared screenshots are required for fingerprinting")
    if any(not path.is_file() for _, path in ordered):
        raise ValueError("A prepared screenshot is unavailable for fingerprinting")
    try:
        completed = subprocess.run(
            [
                _magick(),
                *(str(path) for _, path in ordered),
                "-auto-orient",
                "-resize",
                "8x8!",
                "-colorspace",
                "Gray",
                "-depth",
                "8",
                "gray:-",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(30, len(ordered) * 3),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Could not calculate the duplicate video fingerprint") from exc
    expected = len(ordered) * CONTENT_FINGERPRINT_FRAME_BYTES
    if completed.returncode != 0 or len(completed.stdout) != expected:
        raise ValueError("Could not calculate the duplicate video fingerprint")
    return encode_content_fingerprint(
        [
            (
                frame_index,
                completed.stdout[
                    position * CONTENT_FINGERPRINT_FRAME_BYTES :
                    (position + 1) * CONTENT_FINGERPRINT_FRAME_BYTES
                ],
            )
            for position, (frame_index, _) in enumerate(ordered)
        ]
    )


def fingerprint_prepared_video(store: VideoMemoryStore, video_id: str) -> bytes:
    with store._connect() as connection:
        rows = connection.execute(
            """
            SELECT frame_index, preview_path
            FROM video_review_frames
            WHERE video_id = ? AND user_captured = 0
            ORDER BY frame_index
            LIMIT ?
            """,
            (video_id, FRAME_COUNT),
        ).fetchall()
    fingerprint = content_fingerprint_for_previews(
        [(int(row["frame_index"]), Path(str(row["preview_path"]))) for row in rows]
    )
    store.save_content_fingerprint(video_id, fingerprint)
    return fingerprint


def backfill_duplicate_candidate_fingerprints(
    store: VideoMemoryStore, video_id: str
) -> int:
    failures = 0
    for candidate_id in store.duplicate_fingerprint_candidate_ids(video_id):
        record = store.video_record(candidate_id)
        if (
            record is not None
            and int(record.get("content_fingerprint_version") or 0)
            == CONTENT_FINGERPRINT_VERSION
            and bytes(record.get("content_fingerprint") or b"")
        ):
            continue
        try:
            fingerprint_prepared_video(store, candidate_id)
        except ValueError:
            failures += 1
    return failures


def prune_duplicate_queue(canonical_root: Path) -> dict[str, int]:
    store = VideoMemoryStore(Path(canonical_root) / "video_memory_review.sqlite")
    identifiers = store.duplicate_fingerprint_candidate_ids()
    fingerprinted = 0
    reused = 0
    failures = 0
    for position, video_id in enumerate(identifiers, start=1):
        record = store.video_record(video_id)
        if (
            record is not None
            and int(record.get("content_fingerprint_version") or 0)
            == CONTENT_FINGERPRINT_VERSION
            and bytes(record.get("content_fingerprint") or b"")
        ):
            reused += 1
        else:
            try:
                fingerprint_prepared_video(store, video_id)
                fingerprinted += 1
            except ValueError:
                failures += 1
        if position % 25 == 0 or position == len(identifiers):
            print(
                f"Duplicate fingerprint progress: {position}/{len(identifiers)} "
                f"ready={fingerprinted + reused} failed={failures}",
                flush=True,
            )
    return {
        "candidate_videos": len(identifiers),
        "fingerprinted": fingerprinted,
        "reused": reused,
        "fingerprint_failures": failures,
        **store.reconcile_duplicate_groups(),
    }


def playback_conversion_timeout_seconds(duration_seconds: float) -> int:
    """Return a bounded transcode limit that grows with source duration."""
    duration = max(0.0, float(duration_seconds or 0.0))
    return min(
        PLAYBACK_TIMEOUT_MAX_SECONDS,
        max(
            PLAYBACK_TIMEOUT_MIN_SECONDS,
            int(duration * PLAYBACK_TIMEOUT_DURATION_MULTIPLIER + 0.999),
        ),
    )


def _ffmpeg_progress_seconds(line: str) -> float | None:
    if not line.startswith("out_time="):
        return None
    value = line.partition("=")[2].strip()
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", value)
    if not match:
        return None
    return (
        int(match.group(1)) * 3600
        + int(match.group(2)) * 60
        + float(match.group(3))
    )


def _run_ffmpeg_with_progress(
    command: list[str],
    *,
    timeout: int,
    progress_callback: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg while consuming its machine-readable progress stream."""
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output: list[str] = []

    def read_output() -> None:
        if process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            output.append(line)
            seconds = _ffmpeg_progress_seconds(line)
            if seconds is not None and progress_callback is not None:
                try:
                    progress_callback(seconds)
                except Exception:
                    continue

    reader = threading.Thread(
        target=read_output,
        daemon=True,
        name="video-memory-ffmpeg-progress",
    )
    reader.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join(timeout=5)
    combined = "\n".join(output)
    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout=combined,
        stderr=combined if return_code else "",
    )


def reveal_source_in_finder(store: VideoMemoryStore, video_id: str) -> dict[str, bool]:
    """Reveal the registered source video in Finder without accepting a client path."""
    record = store.video_record(video_id)
    if record is None:
        raise LookupError("Video not found")
    source = Path(str(record["source_path"])).resolve()
    if not source.is_file():
        raise FileNotFoundError("Source video is offline")
    if sys.platform != "darwin":
        raise NotImplementedError("Open in Finder is only available on macOS")
    try:
        completed = _run(["open", "-R", str(source)], timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Finder could not reveal the source video") from exc
    if completed.returncode != 0:
        raise RuntimeError("Finder could not reveal the source video")
    return {"revealed": True}


def _ffmpeg() -> str:
    command = shutil.which("ffmpeg")
    if not command:
        raise ValueError("FFmpeg is required")
    return command


def _ffprobe() -> str:
    command = shutil.which("ffprobe")
    if not command:
        raise ValueError("FFprobe is required")
    return command


def synopsis_segment_plan(
    duration_seconds: float,
    representative_times: Iterable[float],
) -> list[tuple[float, float]]:
    """Plan a deterministic short summary without decoding the whole source."""
    duration = max(0.0, float(duration_seconds or 0.0))
    if duration < 45.0:
        return []
    desired_count = min(16, max(12, int(round(duration / 45.0)) + 10))
    candidates = sorted(
        {
            round(float(value), 3)
            for value in representative_times
            if 0.0 < float(value) < duration
        }
    )
    if len(candidates) < desired_count:
        candidates = frame_times(duration, desired_count)
    elif len(candidates) > desired_count:
        candidates = [
            candidates[round(index * (len(candidates) - 1) / (desired_count - 1))]
            for index in range(desired_count)
        ]
    segment_duration = SYNOPSIS_SEGMENT_SECONDS
    return [
        (
            round(min(max(0.0, center - segment_duration / 2), duration - segment_duration), 3),
            round(segment_duration, 3),
        )
        for center in candidates
    ]


def synopsis_ffmpeg_command(
    source: Path,
    destination: Path,
    segments: list[tuple[float, float]],
    *,
    include_audio: bool,
) -> list[str]:
    if not segments:
        raise ValueError("Synopsis requires at least one segment")
    transition = min(
        SYNOPSIS_TRANSITION_SECONDS,
        min(float(duration) for _start, duration in segments) / 2,
    )
    if transition <= 0:
        raise ValueError("Synopsis segments must have positive durations")
    command = [_ffmpeg(), "-hide_banner", "-loglevel", "error"]
    for start, duration in segments:
        command.extend(("-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)))
    filters: list[str] = []
    single_segment = len(segments) == 1
    for index, (_start, duration) in enumerate(segments):
        video_label = "outv" if single_segment else f"v{index}"
        filters.append(
            f"[{index}:v:0]setpts=PTS-STARTPTS,"
            "scale='min(1280,iw)':-2:flags=lanczos,fps=30,"
            f"format=yuv420p,settb=AVTB[{video_label}]"
        )
        if include_audio:
            audio_label = "outa" if single_segment else f"a{index}"
            filters.append(
                f"[{index}:a:0]asetpts=PTS-STARTPTS,"
                "aresample=48000:async=1:first_pts=0,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"apad=whole_dur={duration:.3f},atrim=duration={duration:.3f}"
                f"[{audio_label}]"
            )
    if not single_segment:
        elapsed = float(segments[0][1])
        previous_video = "v0"
        previous_audio = "a0"
        for index in range(1, len(segments)):
            final_transition = index == len(segments) - 1
            video_output = "outv" if final_transition else f"vx{index}"
            offset = elapsed - transition
            filters.append(
                f"[{previous_video}][v{index}]"
                f"xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}"
                f"[{video_output}]"
            )
            previous_video = video_output
            if include_audio:
                audio_output = "outa" if final_transition else f"ax{index}"
                filters.append(
                    f"[{previous_audio}][a{index}]"
                    f"acrossfade=d={transition:.3f}:c1=qsin:c2=qsin"
                    f"[{audio_output}]"
                )
                previous_audio = audio_output
            elapsed += float(segments[index][1]) - transition
    command.extend(
        (
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
        )
    )
    if include_audio:
        command.extend(("-map", "[outa]", "-c:a", "aac", "-b:a", "160k"))
    else:
        command.append("-an")
    command.extend(("-movflags", "+faststart", "-y", str(destination)))
    return command


def synopsis_output_path(output_root: Path, video_id: str, source: Path) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-._") or "video"
    return Path(output_root) / video_id[:2] / video_id / f"{stem}-synopsis.mp4"


def synopsis_media_url(video_id: str, updated_at: str) -> str:
    return (
        f"/media/synopsis/{urllib.parse.quote(str(video_id), safe='')}"
        f"?v={urllib.parse.quote(str(updated_at), safe='')}"
    )


def synopsis_artifact_is_current(path: Path) -> bool:
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        sidecar = json.loads(path.with_suffix(".json").read_text())
    except (OSError, ValueError, TypeError):
        return False
    return sidecar.get("schema_version") == SYNOPSIS_SCHEMA_VERSION


def source_has_audio(path: Path) -> bool:
    completed = _run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60,
    )
    if completed.returncode != 0:
        raise ValueError((completed.stderr or "Could not inspect source audio").strip())
    return bool(completed.stdout.strip())


def generate_synopsis(
    item: dict[str, Any],
    output_root: Path,
) -> tuple[str, Path | None]:
    source = Path(str(item["source_path"]))
    if not source.is_file():
        raise ValueError("Source video is offline")
    if source.stat().st_size != int(item["source_byte_size"]):
        raise ValueError("Source video changed after review")
    duration = float(item["duration_seconds"] or 0.0)
    segments = synopsis_segment_plan(
        duration,
        [float(frame["time_seconds"]) for frame in item.get("frames", ())],
    )
    if not segments:
        return "source_preferred", None
    destination = synopsis_output_path(
        output_root, str(item["video_id"]), source
    )
    if synopsis_artifact_is_current(destination):
        return "ready", destination
    include_audio = source_has_audio(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.partial-{uuid.uuid4().hex}.mp4")
    try:
        completed = subprocess.run(
            synopsis_ffmpeg_command(
                source, temporary, segments, include_audio=include_audio
            ),
            check=False,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=playback_conversion_timeout_seconds(duration),
        )
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError((completed.stderr or "Synopsis generation failed").strip()[-1000:])
        temporary.replace(destination)
        provenance = {
            "schema_version": SYNOPSIS_SCHEMA_VERSION,
            "video_id": str(item["video_id"]),
            "source_path": str(source),
            "source_sha256": str(item["source_sha256"]),
            "source_byte_size": int(item["source_byte_size"]),
            "duration_seconds": duration,
            "segments": [
                {"start_seconds": start, "duration_seconds": length}
                for start, length in segments
            ],
            "audio": "source" if include_audio else "none",
            "transition": {
                "type": "crossfade",
                "duration_seconds": SYNOPSIS_TRANSITION_SECONDS,
            },
            "container": "mp4",
            "video_codec": "h264",
            "created_at": utc_now(),
        }
        sidecar = destination.with_suffix(".json")
        sidecar_temporary = sidecar.with_name(f".{sidecar.name}.partial-{uuid.uuid4().hex}")
        sidecar_temporary.write_text(json.dumps(provenance, indent=2, sort_keys=True))
        sidecar_temporary.replace(sidecar)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return "ready", destination


def probe_duration(path: Path) -> float:
    completed = _run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60,
    )
    if completed.returncode != 0:
        raise ValueError((completed.stderr or "Could not read video duration").strip())
    try:
        duration = float(completed.stdout.strip())
    except ValueError as exc:
        raise ValueError("Video duration is invalid") from exc
    if duration <= 0:
        raise ValueError("Video duration is unavailable")
    return duration


def playback_proxy_path(cache_root: Path, video_id: str) -> Path:
    return Path(cache_root) / "playback-scrub-v1" / f"{video_id}.mp4"


def scrub_optimized_video(
    source: Path,
    cache_root: Path,
    video_id: str,
    *,
    duration_seconds: float = 0.0,
    progress_callback: Callable[[float], None] | None = None,
) -> Path:
    """Lazily build a compact, short-GOP MP4 for responsive browser seeking."""
    destination = playback_proxy_path(cache_root, video_id)
    if destination.is_file() and destination.stat().st_size > 0:
        if progress_callback is not None:
            progress_callback(max(0.0, float(duration_seconds or 0.0)))
        return destination
    duration = max(0.0, float(duration_seconds or 0.0)) or probe_duration(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.partial-{uuid.uuid4().hex}.mp4")
    command = [
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        "scale='min(960,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "25",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "6",
        "-keyint_min",
        "6",
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-y",
        str(temporary),
    ]
    try:
        completed = _run_ffmpeg_with_progress(
            command,
            timeout=playback_conversion_timeout_seconds(duration),
            progress_callback=progress_callback,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        raise ValueError((completed.stderr or "Could not prepare scrub-optimized video").strip())
    temporary.replace(destination)
    if progress_callback is not None:
        progress_callback(duration)
    return destination


def video_filters(rotation: int, max_pixels: int | None) -> list[str]:
    normalized = int(rotation) % 360
    if normalized not in {0, 90, 180, 270}:
        raise ValueError("Rotation must be a multiple of 90 degrees")
    filters: list[str] = []
    if normalized == 90:
        filters.append("transpose=clock")
    elif normalized == 180:
        filters.extend(["hflip", "vflip"])
    elif normalized == 270:
        filters.append("transpose=cclock")
    if max_pixels:
        filters.append(f"scale={max_pixels}:{max_pixels}:force_original_aspect_ratio=decrease")
    return filters


def extract_frame(
    source: Path,
    output: Path,
    time_seconds: float,
    *,
    max_pixels: int | None,
    quality: int = 5,
    rotation: int = 0,
    retry_earlier: bool = False,
    retry_earlier_limit_seconds: float | None = None,
) -> float:
    output.parent.mkdir(parents=True, exist_ok=True)
    offsets = (0.0, 0.05, 0.1, 0.25, 0.5, 1.0) if retry_earlier else (0.0,)
    if retry_earlier_limit_seconds is not None:
        limit = max(0.0, float(retry_earlier_limit_seconds))
        offsets = tuple(offset for offset in offsets if offset <= limit)
    attempted_times: set[float] = set()
    last_error = "Could not extract video frame"
    for offset in offsets:
        seek_time = round(max(0.0, float(time_seconds) - offset), 3)
        if seek_time in attempted_times:
            continue
        attempted_times.add(seek_time)
        command = [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{seek_time:.3f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
        ]
        filters = video_filters(rotation, max_pixels)
        if filters:
            command += ["-vf", ",".join(filters)]
        command += ["-q:v", str(quality), "-y", str(output)]
        completed = _run(command, timeout=180)
        if completed.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return seek_time
        output.unlink(missing_ok=True)
        last_error = (completed.stderr or "Could not extract video frame").strip()
        if completed.returncode != 0:
            break
    raise ValueError(last_error)


def _vision_scores(paths: list[Path]) -> list[float | None]:
    try:
        from project365_crop_trial import VISION_BINARY, ensure_vision_binary

        ensure_vision_binary()
        completed = _run([str(VISION_BINARY), "score", *(str(path) for path in paths)], timeout=180)
        if completed.returncode != 0:
            return [None] * len(paths)
        payload = json.loads(completed.stdout)
    except (ImportError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return [None] * len(paths)
    scores: list[float | None] = []
    for item in payload:
        value = item.get("score") if isinstance(item, dict) else None
        scores.append(float(value) if isinstance(value, (int, float)) else None)
    if len(scores) != len(paths):
        return [None] * len(paths)
    return scores


def _cache_folder(cache_root: Path, video_id: str) -> Path:
    return cache_root / video_id[:2] / video_id


def capture_video_frame(
    store: VideoMemoryStore,
    cache_root: Path,
    video_id: str,
    time_seconds: float,
) -> dict[str, Any]:
    record = store.video_record(video_id)
    if record is None or record["status"] != "ready":
        raise ValueError("Unknown or unavailable video")
    duration = float(record["duration_seconds"])
    requested_time = float(time_seconds)
    if requested_time < 0 or requested_time > duration:
        raise ValueError("Capture time is outside the video")
    capture_time = min(
        requested_time,
        max(0.0, duration - FINAL_FRAME_SAFETY_MARGIN_SECONDS),
    )
    with store._connect() as connection:
        duplicate = connection.execute(
            "SELECT 1 FROM video_review_frames WHERE video_id = ? AND ABS(time_seconds - ?) < 0.05",
            (video_id, capture_time),
        ).fetchone()
        if duplicate:
            raise ValueError("A screenshot already exists at nearly this time")
        next_index = int(
            connection.execute(
                "SELECT COALESCE(MAX(frame_index), 0) + 1 FROM video_review_frames WHERE video_id = ?",
                (video_id,),
            ).fetchone()[0]
        )
    destination = _cache_folder(Path(cache_root), video_id)
    destination.mkdir(parents=True, exist_ok=True)
    preview = destination / f"frame-{next_index:02d}.jpg"
    temporary = preview.with_name(f".{preview.stem}.partial-{uuid.uuid4().hex}.jpg")
    try:
        resolved_time = extract_frame(
            Path(str(record["source_path"])),
            temporary,
            capture_time,
            max_pixels=PREVIEW_MAX_PIXELS,
            quality=5,
            rotation=int(record["rotation_override"]),
        )
        score = _vision_scores([temporary])[0]
        temporary.replace(preview)
        return store.add_captured_frame(
            video_id,
            time_seconds=resolved_time,
            preview_path=preview,
            preview_bytes=preview.stat().st_size,
            vision_score=score,
        )
    finally:
        temporary.unlink(missing_ok=True)


def rotate_video_previews(
    store: VideoMemoryStore,
    cache_root: Path,
    video_id: str,
    direction: str,
) -> dict[str, Any]:
    if direction not in {"left", "right"}:
        raise ValueError("Rotation direction must be left or right")
    record = store.video_record(video_id)
    if record is None or record["status"] != "ready":
        raise ValueError("Unknown or unavailable video")
    delta = -90 if direction == "left" else 90
    rotation = (int(record["rotation_override"]) + delta) % 360
    with store._connect() as connection:
        frames = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM video_review_frames WHERE video_id = ? ORDER BY frame_index",
                (video_id,),
            )
        ]
    if not frames:
        raise ValueError("Video has no prepared screenshots")
    temporary_paths: list[Path] = []
    try:
        for frame in frames:
            preview = Path(str(frame["preview_path"]))
            if not preview.resolve().is_relative_to(Path(cache_root).resolve()):
                raise ValueError("Preview path is outside the review cache")
            temporary = preview.with_name(f".{preview.stem}.rotation-{uuid.uuid4().hex}.jpg")
            extract_frame(
                Path(str(record["source_path"])), temporary, float(frame["time_seconds"]),
                max_pixels=PREVIEW_MAX_PIXELS, quality=5, rotation=rotation,
            )
            temporary_paths.append(temporary)
        scores = _vision_scores(temporary_paths)
        updates = []
        for frame, temporary, score in zip(frames, temporary_paths, scores, strict=True):
            preview = Path(str(frame["preview_path"]))
            temporary.replace(preview)
            updates.append(
                {
                    "frame_id": frame["frame_id"],
                    "preview_bytes": preview.stat().st_size,
                    "vision_score": score if score is not None else frame.get("vision_score"),
                }
            )
        store.save_rotation(video_id, rotation, updates)
        return {"video_id": video_id, "rotation_override": rotation, "frames": len(updates)}
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)


def prepare_video(
    store: VideoMemoryStore,
    video: dict[str, Any],
    cache_root: Path,
) -> dict[str, Any]:
    video_id = str(video["video_id"])
    source = Path(str(video["source_path"]))
    if not source.is_file():
        raise ValueError("Source video is offline")
    duration = float(video.get("duration_seconds") or 0.0) or probe_duration(source)
    record = {
        **video,
        "duration_seconds": duration,
        "status": "generating",
        "preview_format": "jpeg",
    }
    store.upsert_video(record)
    persisted = store.video_record(video_id) or {}
    rotation = int(persisted.get("rotation_override", 0))
    preserve_user_selection = int(persisted.get("review_version", 0)) > 0
    destination = _cache_folder(cache_root, video_id)
    temporary = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        requested_times = frame_times(duration)
        extracted_samples: list[tuple[int, float, Path]] = []
        frames = []
        for index, time_seconds in enumerate(requested_times, start=1):
            preview = temporary / f"frame-{index:02d}.jpg"
            try:
                resolved_time = extract_frame(
                    source,
                    preview,
                    time_seconds,
                    max_pixels=PREVIEW_MAX_PIXELS,
                    quality=5,
                    rotation=rotation,
                    retry_earlier=True,
                )
            except ValueError:
                continue
            extracted_samples.append((index, resolved_time, preview))
        if not extracted_samples:
            raise ValueError("Could not extract any video frames")
        preview_paths = [preview for _, _, preview in extracted_samples]
        scores = _vision_scores(preview_paths)
        for (index, time_seconds, preview), score in zip(extracted_samples, scores, strict=True):
            frames.append(
                {
                    "frame_id": f"{video_id}:{index:02d}",
                    "frame_index": index,
                    "time_seconds": time_seconds,
                    "preview_path": str(destination / preview.name),
                    "preview_bytes": preview.stat().st_size,
                    "vision_score": score,
                }
            )
        symlink = temporary / f"Source Video{source.suffix.lower()}"
        try:
            symlink.symlink_to(source)
        except OSError:
            pass
        manifest = {
            "schema_version": 1,
            "video_id": video_id,
            "source_sha256": video["source_sha256"],
            "source_path": str(source),
            "source_byte_size": int(video["source_byte_size"]),
            "source_mtime_ns": int(video["source_mtime_ns"]),
            "capture_date": video["capture_date"],
            "date_source": video["date_source"],
            "capture_timestamp": video.get("capture_timestamp", ""),
            "duration_seconds": duration,
            "preview_format": "jpeg",
            "preview_max_pixels": PREVIEW_MAX_PIXELS,
            "rotation_override": rotation,
            "requested_frame_count": len(requested_times),
            "extracted_frame_count": len(extracted_samples),
            "frame_times": [time_seconds for _, time_seconds, _ in extracted_samples],
        }
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing_manifest = destination / "manifest.json"
            if not existing_manifest.is_file() or json.loads(existing_manifest.read_text()).get("source_sha256") != video["source_sha256"]:
                raise ValueError("Existing preview folder has a different source identity")
            for preview in preview_paths:
                preview.replace(destination / preview.name)
            (temporary / "manifest.json").replace(destination / "manifest.json")
            if symlink.is_symlink() and not (destination / symlink.name).exists():
                symlink.replace(destination / symlink.name)
            shutil.rmtree(temporary, ignore_errors=True)
        else:
            temporary.replace(destination)
        store.replace_frames(video_id, frames)
        selection = choose_vision_selection(frames)
        method = str(selection["method"])
        model = "VNCalculateImageAestheticsScoresRequest" if method == "apple_vision" else ""
        if not preserve_user_selection:
            store.save_ai_selection(
                video_id,
                selection["selected_frame_ids"],
                selection["main_frame_id"],
                method=method,
                model=model,
            )
        fingerprint_status = "ready"
        try:
            fingerprint_prepared_video(store, video_id)
            if backfill_duplicate_candidate_fingerprints(store, video_id):
                fingerprint_status = "partial"
            store.reconcile_duplicate_groups()
        except ValueError:
            fingerprint_status = "error"
        return {
            "video_id": video_id,
            "frames": len(frames),
            "selected": len(selection["selected_frame_ids"]),
            "ai_method": method,
            "duplicate_fingerprint": fingerprint_status,
        }
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        store.mark_video_error(video_id, "Preview or AI preparation failed")
        raise


def prepare_pilot(
    canonical_root: Path,
    *,
    index_paths: list[Path] | None = None,
) -> list[dict[str, Any]]:
    canonical_root = Path(canonical_root)
    index_paths = index_paths or [
        canonical_root / "video_library_legacy_index.sqlite",
        canonical_root / "photo_library_reexport_index.sqlite",
    ]
    videos = select_stratified_pilot_videos(index_paths)
    store = VideoMemoryStore(canonical_root / "video_memory_review.sqlite")
    cache_root = canonical_root / "cache" / "video_memory_review"
    results = []
    for position, video in enumerate(videos, start=1):
        result = prepare_video(store, video, cache_root)
        results.append(result)
        print(
            f"prepared {position}/{len(videos)} frames={result['frames']} "
            f"selected={result['selected']} ai={result['ai_method']}",
            flush=True,
        )
    return results


def video_batch_progress_line(
    *,
    completed: int,
    total: int,
    prepared: int,
    failed: int,
    latest: str,
) -> str:
    return (
        f"Progress: {completed}/{total} videos · prepared {prepared} · "
        f"failed {failed} · latest {latest}"
    )


def prepare_batch(
    canonical_root: Path,
    *,
    count: int | None,
    start_date: str = "",
    end_date: str = "",
    index_paths: list[Path] | None = None,
) -> dict[str, int]:
    canonical_root = Path(canonical_root)
    index_paths = index_paths or [
        canonical_root / "video_library_legacy_index.sqlite",
        canonical_root / "photo_library_reexport_index.sqlite",
    ]
    store = VideoMemoryStore(canonical_root / "video_memory_review.sqlite")
    indexed = filter_batch_candidates_by_date(
        select_pilot_videos(index_paths, count=1_000_000),
        start_date=start_date,
        end_date=end_date,
    )
    completed_ids = store.completed_ai_video_ids()
    retry_ids = store.incomplete_ai_video_ids()
    available_count = sum(
        str(item["video_id"]) not in completed_ids
        for item in indexed
    )
    all_missing = count is None
    requested = available_count if all_missing else count
    if requested == 0:
        return {"requested": 0, "prepared": 0, "failed": 0, "attempted": 0}
    candidates = select_batch_candidates(
        indexed,
        excluded_video_ids=completed_ids,
        count=requested,
    )
    candidates = (
        [item for item in candidates if str(item["video_id"]) in retry_ids]
        + [item for item in candidates if str(item["video_id"]) not in retry_ids]
    )
    cache_root = canonical_root / "cache" / "video_memory_review"
    prepared = 0
    failed = 0
    attempted = 0
    progress_total = len(candidates) if all_missing else requested
    for video in candidates:
        if prepared >= requested:
            break
        attempted += 1
        try:
            result = prepare_video(store, video, cache_root)
            if result["ai_method"] != "apple_vision":
                store.mark_video_error(
                    str(video["video_id"]),
                    "On-device Apple Vision did not return a usable score",
                )
                raise RuntimeError("On-device Apple Vision scoring was unavailable")
        except Exception as exc:
            failed += 1
            print(
                video_batch_progress_line(
                    completed=attempted if all_missing else prepared,
                    total=progress_total,
                    prepared=prepared,
                    failed=failed,
                    latest=f"failed {type(exc).__name__}",
                ),
                flush=True,
            )
            continue
        prepared += 1
        print(
            video_batch_progress_line(
                completed=attempted if all_missing else prepared,
                total=progress_total,
                prepared=prepared,
                failed=failed,
                latest=str(result["ai_method"]),
            ),
            flush=True,
        )
    return {
        "requested": requested,
        "prepared": prepared,
        "failed": failed,
        "attempted": attempted,
    }


def final_output_path(output_root: Path, item: dict[str, Any]) -> Path:
    source_stem = Path(str(item["source_path"])).stem.strip()
    if not source_stem or source_stem in {".", ".."}:
        raise ValueError("Source video has no usable filename")
    folder_name = str(item.get("output_folder_name") or source_stem)
    if Path(folder_name).name != folder_name or folder_name in {".", ".."}:
        raise ValueError("Invalid screenshot output folder name")
    export_sequence = int(item["export_sequence"])
    export_count = int(item["export_count"])
    if export_count < 1 or not 1 <= export_sequence <= export_count:
        raise ValueError("Invalid export sequence")
    sequence_width = len(str(export_count))
    time_ms = int(round(float(item["time_seconds"]) * 1000))
    rotation = int(item.get("rotation_override", 0)) % 360
    role = "main" if bool(item["is_main"]) else "linked"
    filename = (
        f"frame-{export_sequence:0{sequence_width}d}-{time_ms:09d}ms-"
        f"rot{rotation:03d}-{role}.jpg"
    )
    return Path(output_root) / folder_name / filename


def _verify_export_folder(folder: Path, video_id: str) -> None:
    if not folder.exists():
        return
    if not folder.is_dir():
        raise ValueError(f"Screenshot output folder is not a directory: {folder}")
    for sidecar in folder.glob("frame-*.json"):
        try:
            payload = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unreadable screenshot sidecar: {sidecar}") from exc
        if not isinstance(payload, dict) or str(payload.get("video_id", "")) != video_id:
            raise ValueError(
                f"Two source videos share the output folder name: {folder.name}"
            )


def migrate_rendered_output_names(
    store: VideoMemoryStore, output_root: Path
) -> dict[str, int]:
    output_root = Path(output_root).resolve()
    output_root_resolved = output_root
    items = store.rendered_output_items()
    plans: list[tuple[dict[str, Any], Path, Path]] = []
    folder_owners: dict[Path, str] = {}
    destination_owners: dict[Path, str] = {}
    for item in items:
        stored_path = str(item["final_path"])
        old_path = Path(stored_path).resolve()
        item["stored_final_path"] = stored_path
        try:
            old_path.resolve().relative_to(output_root_resolved)
        except ValueError as exc:
            raise ValueError(f"Rendered screenshot is outside the output root: {old_path}") from exc
        new_path = final_output_path(output_root, item)
        video_id = str(item["video_id"])
        previous_video = folder_owners.setdefault(new_path.parent, video_id)
        if previous_video != video_id:
            raise ValueError(
                f"Two source videos share the output folder name: {new_path.parent.name}"
            )
        previous_frame = destination_owners.setdefault(new_path, str(item["frame_id"]))
        if previous_frame != str(item["frame_id"]):
            raise ValueError(f"Two screenshots resolve to the same output path: {new_path}")
        old_sidecar = old_path.with_suffix(".json")
        if not old_path.is_file() or not old_sidecar.is_file():
            raise ValueError(f"Rendered screenshot pair is incomplete: {old_path}")
        plans.append((item, old_path, new_path))

    for folder, video_id in folder_owners.items():
        _verify_export_folder(folder, video_id)

    changed_plans = [plan for plan in plans if plan[1] != plan[2]]
    old_artifacts = {
        path
        for _, old_path, _ in changed_plans
        for path in (old_path, old_path.with_suffix(".json"))
    }
    for _, _, new_path in changed_plans:
        for destination in (new_path, new_path.with_suffix(".json")):
            if destination.exists() and destination not in old_artifacts:
                raise ValueError(f"Human-readable output path already exists: {destination}")

    staging_root = output_root / f".name-migration-{uuid.uuid4().hex}"
    staged: list[tuple[Path, Path, Path]] = []
    updates: list[tuple[str, str, str]] = []
    try:
        if changed_plans:
            staging_root.mkdir(parents=True)
        for item, old_path, new_path in changed_plans:
            token = uuid.uuid4().hex
            for old_artifact, new_artifact in (
                (old_path, new_path),
                (old_path.with_suffix(".json"), new_path.with_suffix(".json")),
            ):
                temporary = staging_root / f"{token}{old_artifact.suffix}"
                old_artifact.replace(temporary)
                staged.append((temporary, old_artifact, new_artifact))
        for temporary, _, new_artifact in staged:
            new_artifact.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(new_artifact)
        updates = [
            (
                str(item["frame_id"]),
                str(item["stored_final_path"]),
                str(new_path),
            )
            for item, _, new_path in plans
            if str(item["stored_final_path"]) != str(new_path)
        ]
        store.update_rendered_output_paths(updates)
    except Exception:
        for temporary, _, new_artifact in staged:
            if new_artifact.exists() and not temporary.exists():
                new_artifact.replace(temporary)
        for temporary, original_path, _ in staged:
            if temporary.exists() and not original_path.exists():
                original_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(original_path)
        raise
    finally:
        try:
            staging_root.rmdir()
        except OSError:
            pass

    old_folders = sorted(
        {old_path.parent for _, old_path, new_path in plans if old_path != new_path},
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for folder in old_folders:
        current = folder
        while current != output_root and output_root in current.parents:
            try:
                finder_metadata = current / ".DS_Store"
                if finder_metadata.is_file():
                    finder_metadata.unlink()
                current.rmdir()
            except OSError:
                break
            current = current.parent
    for shard in output_root.iterdir() if output_root.is_dir() else ():
        if not shard.is_dir() or re.fullmatch(r"[0-9a-fA-F]{2}", shard.name) is None:
            continue
        for legacy_folder in tuple(shard.iterdir()):
            if (
                not legacy_folder.is_dir()
                or re.fullmatch(r"[0-9a-fA-F]{64}", legacy_folder.name) is None
            ):
                continue
            children = tuple(legacy_folder.iterdir())
            if any(child.name != ".DS_Store" for child in children):
                continue
            for child in children:
                child.unlink()
            legacy_folder.rmdir()
        shard_children = tuple(shard.iterdir())
        if all(child.name == ".DS_Store" for child in shard_children):
            for child in shard_children:
                child.unlink()
            shard.rmdir()
    return {
        "videos": len({str(item["video_id"]) for item, _, _ in plans}),
        "frames": len(plans),
        "files_moved": len(changed_plans) * 2,
    }


def final_renderer(output_root: Path) -> Callable[[dict[str, Any]], Path]:
    output_root = Path(output_root).resolve()

    def render(item: dict[str, Any]) -> Path:
        source = Path(str(item["source_path"]))
        if not source.is_file():
            raise ValueError("Source video is offline")
        stat = source.stat()
        if int(item["source_byte_size"]) != stat.st_size:
            raise ValueError("Source video byte size changed after review")
        video_id = str(item["video_id"])
        frame_index = int(item["frame_index"])
        requested_time = float(item["time_seconds"])
        duration = float(item.get("duration_seconds") or 0.0)
        render_time = (
            min(requested_time, max(0.0, duration - FINAL_FRAME_SAFETY_MARGIN_SECONDS))
            if duration > 0
            else requested_time
        )
        rotation = int(item.get("rotation_override", 0)) % 360
        output = final_output_path(output_root, item)
        folder = output.parent
        _verify_export_folder(folder, video_id)
        folder.mkdir(parents=True, exist_ok=True)
        sidecar = output.with_suffix(".json")
        if output.exists() != sidecar.exists():
            raise ValueError("Rendered screenshot pair is incomplete")
        if output.is_file() and output.stat().st_size > 0 and sidecar.is_file():
            return output
        temporary = output.with_name(f".{output.stem}.partial-{uuid.uuid4().hex}.jpg")
        resolved_time = extract_frame(
            source, temporary, render_time,
            max_pixels=None, quality=2, rotation=rotation,
            retry_earlier=True, retry_earlier_limit_seconds=0.5,
        )
        temporary.replace(output)
        metadata = {
            "schema_version": 1,
            "status": "rendered_not_canonical",
            "video_id": video_id,
            "source_sha256": item["source_sha256"],
            "source_path": str(source),
            "frame_index": frame_index,
            "export_sequence": int(item["export_sequence"]),
            "export_count": int(item["export_count"]),
            "time_seconds": resolved_time,
            "requested_time_seconds": requested_time,
            "is_main": bool(item["is_main"]),
            "user_captured": bool(item.get("user_captured", 0)),
            "rotation_override": rotation,
            "capture_date": item["capture_date"],
            "date_source": item["date_source"],
            "capture_timestamp": item["capture_timestamp"],
            "memory_datetime": item.get("memory_datetime", ""),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
        }
        if bool(item["private"]):
            metadata["private"] = True
        sidecar_temporary = sidecar.with_name(f".{sidecar.name}.partial-{uuid.uuid4().hex}")
        sidecar_temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True))
        sidecar_temporary.replace(sidecar)
        return output

    setattr(render, "output_root", output_root)
    return render


def process_render_queue(
    store: VideoMemoryStore,
    renderer: Callable[[dict[str, Any]], Path],
    limit: int = 100,
) -> dict[str, int]:
    renderer_output_root = getattr(renderer, "output_root", None)
    if renderer_output_root is not None:
        migrate_rendered_output_names(store, Path(renderer_output_root))
    items = store.claim_render_items(limit)
    rendered = 0
    failed = 0
    for item in items:
        try:
            output = renderer(item)
            store.mark_rendered(str(item["frame_id"]), output)
            rendered += 1
        except Exception as exc:
            store.mark_render_failed(str(item["frame_id"]), str(exc))
            failed += 1
    return {"processed": len(items), "rendered": rendered, "failed": failed}


# The scrub controller is kept as a standalone JavaScript unit so its transition
# rules can be executed against a deterministic mock player.  Safari receives the
# same source verbatim in the embedded review page.
VIDEO_SCRUB_CONTROLLER_JS = r"""
function createVideoScrubController(player,hooks={}){
 const onSelectedTime=hooks.onSelectedTime||function(){};
 const onStateChange=hooks.onStateChange||function(){};
 const onStatus=hooks.onStatus||function(){};
 const onDiagnostic=hooks.onDiagnostic||function(){};
 const setTimer=hooks.setTimer||setTimeout;
 const clearTimer=hooks.clearTimer||clearTimeout;
 const frameTolerance=Number(hooks.frameTolerance)||0.12;
 const watchdogMilliseconds=Number(hooks.watchdogMilliseconds)||1500;
 let phase='idle';
 let dragging=false;
 let selectedTime=0;
 let activeRequest=null;
 let pendingRequest=null;
 let requestSerial=0;
 let frameCallbackId=null;
 let watchdogId=null;
 let playWhenSettled=false;
 let decoderAssistActive=false;
 let destroyed=false;
 const diagnostics=[];
 function duration(){return Math.max(0,Number(player.duration)||Number(hooks.duration?.())||0)}
 function clamp(value){return Math.max(0,Math.min(duration(),Number(value)||0))}
 function playable(value){const total=duration();return Math.min(clamp(value),Math.max(0,total-0.1))}
 function diagnostic(kind,request,extra={}){
  const item={
   at:typeof performance!=='undefined'?performance.now():Date.now(),kind,
   phase,target:request?.target??null,requestId:request?.id??null,
   selectedTime,currentTime:Number(player.currentTime)||0,seeking:Boolean(player.seeking),
   paused:Boolean(player.paused),pendingTarget:pendingRequest?.target??null,...extra,
  };
  diagnostics.push(item);if(diagnostics.length>300)diagnostics.shift();onDiagnostic(item)
 }
 function publish(){onStateChange(snapshot())}
 function setPhase(value){if(phase===value)return;phase=value;publish()}
 function cancelObservation(){
  if(frameCallbackId!==null&&typeof player.cancelVideoFrameCallback==='function'){
   player.cancelVideoFrameCallback(frameCallbackId)
  }
  frameCallbackId=null;
  if(watchdogId!==null)clearTimer(watchdogId);
  watchdogId=null
 }
 function finishActive(request){
  if(!activeRequest||request.id!==activeRequest.id)return;
  diagnostic('presented',request);
  const continueDecoder=decoderAssistActive&&Boolean(pendingRequest||(playWhenSettled&&!dragging));
  if(decoderAssistActive&&!continueDecoder&&!player.paused)player.pause();
  decoderAssistActive=continueDecoder;
  onStatus('');
  cancelObservation();
  activeRequest=null;
  if(pendingRequest){pump();return}
  if(playWhenSettled){
   playWhenSettled=false;
   setPhase('playing');
   if(continueDecoder)return;
   player.play().catch(function(error){onStatus(error.message);setPhase('idle')});
   return
  }
  setPhase(dragging?'scrubbing':'idle')
 }
 function observeFrame(request){
  if(!activeRequest||request.id!==activeRequest.id)return;
  if(typeof player.requestVideoFrameCallback!=='function'){
   if(!player.seeking)finishActive(request);
   return
  }
  frameCallbackId=player.requestVideoFrameCallback(function(_now,metadata){
   frameCallbackId=null;
   if(!activeRequest||request.id!==activeRequest.id)return;
   const mediaTime=Number(metadata?.mediaTime);
   diagnostic('video-frame',request,{mediaTime:Number.isFinite(mediaTime)?mediaTime:null});
   if(Number.isFinite(mediaTime)&&Math.abs(mediaTime-request.target)<=frameTolerance){
    finishActive(request);return
   }
   observeFrame(request)
  })
 }
 function startWatchdog(request){
  if(watchdogId!==null)clearTimer(watchdogId);
  watchdogId=setTimer(function(){
   watchdogId=null;
   if(!activeRequest||request.id!==activeRequest.id)return;
   diagnostic('frame-timeout',request);
   const retry=pendingRequest;
   pendingRequest=null;
   abandonActive();
   playWhenSettled=false;
   onStatus('Safari did not present the selected frame; try the timeline again.');
   setPhase(dragging?'scrubbing':'idle');
   if(retry&&Math.abs(retry.target-request.target)>=0.0005){pendingRequest=retry;pump()}
  },watchdogMilliseconds)
 }
 function abandonActive(){
  if(activeRequest)diagnostic('superseded',activeRequest);
  if(decoderAssistActive&&!player.paused)player.pause();
  decoderAssistActive=false;
  cancelObservation();
  activeRequest=null
 }
 function startDecoderAssist(request){
  if(decoderAssistActive&&!player.paused){
   diagnostic('decoder-assist-reuse',request);
   return
  }
  decoderAssistActive=true;
  diagnostic('decoder-assist',request);
  player.play().catch(function(error){
   if(!activeRequest||request.id!==activeRequest.id)return;
   decoderAssistActive=false;
   diagnostic('decoder-assist-error',request,{message:error.message});
   onStatus('Safari could not decode the selected frame: '+error.message)
  })
 }
 function pump(){
  if(destroyed||activeRequest||!pendingRequest)return;
  const request=pendingRequest;
  pendingRequest=null;
  activeRequest=request;
  diagnostic('seek-start',request,{mode:request.exact?'exact':'fast'});
  if(Math.abs((Number(player.currentTime)||0)-request.target)<0.0005&&!player.seeking){
   finishActive(request);return
  }
  observeFrame(request);
  startWatchdog(request);
  try{
   if(!request.exact&&typeof player.fastSeek==='function')player.fastSeek(request.target);
   else player.currentTime=request.target;
   startDecoderAssist(request)
  }catch(error){
   diagnostic('seek-error',request,{message:error.message});
   abandonActive();
   onStatus(error.message);
   if(pendingRequest)pump();else setPhase(dragging?'scrubbing':'idle')
  }
 }
 function enqueue(value,exact,reason){
  const request={id:++requestSerial,target:playable(value),exact:Boolean(exact),reason};
  pendingRequest=request;
  diagnostic('requested',request,{mode:request.exact?'exact':'fast'});
  pump();
  return request
 }
 function setSelection(value){selectedTime=clamp(value);onSelectedTime(selectedTime);return selectedTime}
 function pauseForSelection(){
  playWhenSettled=false;
  if(!decoderAssistActive&&!player.paused&&!player.ended)player.pause()
 }
 function beginScrub(){
  pauseForSelection();
  dragging=true;
  setPhase('scrubbing');
  diagnostic('scrub-begin',null)
 }
 function preview(value){
  if(!dragging)beginScrub();
  const selected=setSelection(value);
  enqueue(selected,false,'drag');
  return selected
 }
 function finishScrub(value=selectedTime){
  pauseForSelection();
  dragging=false;
  const selected=setSelection(value);
  setPhase('settling');
  const target=playable(selected);
  if(activeRequest&&Math.abs(activeRequest.target-target)<0.0005&&!activeRequest.exact){
   activeRequest.exact=true;
   activeRequest.reason='release';
   pendingRequest=null;
   diagnostic('seek-upgrade',activeRequest,{mode:'exact'});
   try{player.currentTime=target}
   catch(error){
    diagnostic('seek-error',activeRequest,{message:error.message});
    abandonActive();
    onStatus(error.message);
    setPhase('idle')
   }
   diagnostic('scrub-end',activeRequest);
   return selected
  }
  enqueue(selected,true,'release');
  diagnostic('scrub-end',pendingRequest||activeRequest);
  return selected
 }
 function selectExact(value){
  pauseForSelection();
  dragging=false;
  const selected=setSelection(value);
  setPhase('settling');
  enqueue(selected,true,'exact');
  return selected
 }
 function step(delta){
  const mediaTime=Number(player.currentTime);
  const base=!player.paused&&!player.ended&&Number.isFinite(mediaTime)?mediaTime:selectedTime;
  return selectExact(base+Number(delta))
 }
 function startPlayback(){
  onStatus('');
  setPhase('playing');
  player.play().catch(function(error){onStatus(error.message);setPhase('idle')})
 }
 function togglePlayback(){
  if(phase==='playing'&&!player.paused&&!player.ended){
   playWhenSettled=false;
   player.pause();
   setSelection(player.currentTime);
   setPhase('idle');
   return
  }
  const target=playable(selectedTime);
  playWhenSettled=true;
  setPhase('settling');
  if(activeRequest&&Math.abs(activeRequest.target-target)<0.0005&&activeRequest.exact&&!pendingRequest)return;
  if(pendingRequest&&Math.abs(pendingRequest.target-target)<0.0005){
   pendingRequest.exact=true;
   pendingRequest.reason='play';
   return
  }
  if(!activeRequest&&!pendingRequest&&Math.abs((Number(player.currentTime)||0)-target)<0.0005&&!player.seeking){
   playWhenSettled=false;
   startPlayback();
   return
  }
  enqueue(selectedTime,true,'play')
 }
 function reset(value=0){
  requestSerial+=1;
  pendingRequest=null;
  abandonActive();
  playWhenSettled=false;
  dragging=false;
  selectedTime=clamp(value);
  onSelectedTime(selectedTime);
  setPhase('idle')
 }
 function handleSeeked(){
  const request=activeRequest;
  if(!request)return;
  diagnostic('seeked',request);
  if(typeof player.requestVideoFrameCallback!=='function')finishActive(request)
 }
 function handlePlaying(){if(!decoderAssistActive)setPhase('playing')}
 function snapshot(){return {
  phase,dragging,selectedTime,activeTarget:activeRequest?.target??null,
  pendingTarget:pendingRequest?.target??null,playWhenSettled,decoderAssistActive,requestSerial,
  diagnostics:[...diagnostics],
 }}
 function destroy(){
  destroyed=true;
  requestSerial+=1;
  pendingRequest=null;
  abandonActive();
  player.removeEventListener('seeked',handleSeeked);
  player.removeEventListener('playing',handlePlaying)
 }
 player.addEventListener('seeked',handleSeeked);
 player.addEventListener('playing',handlePlaying);
 return {beginScrub,preview,finishScrub,selectExact,step,togglePlayback,reset,snapshot,destroy}
}

function createPlaybackTimelineFollower(player,onTime,hooks={}){
 const requestAnimation=hooks.requestAnimationFrame||(typeof requestAnimationFrame==='function'?requestAnimationFrame:null);
 const cancelAnimation=hooks.cancelAnimationFrame||(typeof cancelAnimationFrame==='function'?cancelAnimationFrame:null);
 let running=false;
 let videoFrameCallbackId=null;
 let animationFrameId=null;
 function cancelScheduled(){
  if(videoFrameCallbackId!==null&&typeof player.cancelVideoFrameCallback==='function')player.cancelVideoFrameCallback(videoFrameCallbackId);
  if(animationFrameId!==null&&cancelAnimation)cancelAnimation(animationFrameId);
  videoFrameCallbackId=null;
  animationFrameId=null
 }
 function publish(value){const time=Number(value);onTime(Number.isFinite(time)?time:Number(player.currentTime)||0)}
 function schedule(){
  if(!running)return;
  if(typeof player.requestVideoFrameCallback==='function'){
   videoFrameCallbackId=player.requestVideoFrameCallback(function(_now,metadata){
    videoFrameCallbackId=null;
    if(!running)return;
    publish(metadata?.mediaTime);
    schedule()
   });
   return
  }
  if(requestAnimation){
   animationFrameId=requestAnimation(function(){
    animationFrameId=null;
    if(!running)return;
    publish(player.currentTime);
    schedule()
   })
  }
 }
 function start(){if(running)return;running=true;publish(player.currentTime);schedule()}
 function stop(sync=true){const wasRunning=running;running=false;cancelScheduled();if(wasRunning&&sync)publish(player.currentTime)}
 function destroy(){stop(false);player.removeEventListener('playing',start);player.removeEventListener('pause',stop);player.removeEventListener('ended',stop)}
 player.addEventListener('playing',start);
 player.addEventListener('pause',stop);
 player.addEventListener('ended',stop);
 return {start,stop,destroy,snapshot:function(){return {running,videoFrameCallbackId,animationFrameId}}}
}
"""


# Rapid triage keeps a bounded card page beside one fixed, explicitly selected player.
TRIAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project365 Video Rapid Triage</title>
<style>
:root{color-scheme:light;--bg:#f4f6f8;--panel:#fff;--ink:#202124;--muted:#667085;--line:#d5dae2;--accent:#17695d;--wanted:#0d5f9a;--done:#5d3a88;--reject:#913636;--gold:#d39b1b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select,textarea{font:inherit}button,.button{border:1px solid #bcc5d0;border-radius:6px;background:#fff;color:var(--ink);padding:.42rem .62rem;cursor:pointer;text-decoration:none;white-space:nowrap}button:hover,.button:hover{background:#edf2f5}button:disabled{opacity:.45;cursor:default}header{display:flex;align-items:center;gap:9px;padding:9px 13px;background:#fff;border-bottom:1px solid var(--line)}h1{font-size:1.05rem;margin:0}.spacer{flex:1}.stats{color:var(--muted);font-variant-numeric:tabular-nums}.filters{position:sticky;top:0;z-index:15;display:flex;align-items:end;gap:8px;flex-wrap:wrap;padding:9px 13px;background:#f9fafb;border-bottom:1px solid var(--line)}label{display:grid;gap:3px;color:var(--muted);font-size:.72rem;font-weight:700}input,select,textarea{min-height:34px;border:1px solid #bcc5d0;border-radius:6px;background:#fff;padding:5px 7px;color:var(--ink)}.compact{width:92px}.search{width:190px}.checkbox-filter{display:flex;align-items:center;gap:5px;min-height:34px;padding:0 4px}.checkbox-filter input{min-height:auto;margin:0}.bulk{display:flex;align-items:center;gap:6px;margin-left:auto}.bulk-count{color:var(--muted)}main{padding:12px;max-width:2100px;margin:0 auto}.triage-layout{display:grid;grid-template-columns:minmax(330px,410px) minmax(0,1fr);gap:12px;align-items:start}.selected-panel{position:sticky;top:76px;max-height:calc(100vh - 88px);overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px;display:grid;gap:8px}.selected-panel h2{font-size:.95rem;margin:0;overflow-wrap:anywhere}.selected-file{color:var(--muted);font-size:.75rem;overflow-wrap:anywhere}.player-stage{position:relative;height:260px;background:#1e2228;border-radius:7px;overflow:hidden;display:grid;place-items:center;cursor:pointer}.player-stage video{width:100%;height:100%;object-fit:contain;transform-origin:center}.playback-status{position:absolute;inset:0;display:grid;place-content:center;padding:18px;text-align:center;color:#fff;background:#1e2228dd}.playback-status[hidden]{display:none}.player-time{text-align:center;color:var(--muted);font-variant-numeric:tabular-nums}.player-timeline{width:100%;min-height:24px;margin:0;padding:0;border:0;border-radius:0;background:transparent;accent-color:var(--accent);cursor:ew-resize}.player-buttons,.reference-buttons,.metadata-actions{display:flex;align-items:center;justify-content:center;gap:5px;flex-wrap:wrap}.player-metadata{display:grid;gap:7px}.player-metadata textarea{min-height:74px;resize:vertical}.metadata-actions{justify-content:flex-start}.panel-message{min-height:1.35em;color:var(--muted);font-size:.78rem}.queue-pane{min-width:0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:12px}.card{min-width:0;background:var(--panel);border:2px solid transparent;border-radius:9px;box-shadow:0 1px 3px #0001;overflow:hidden}.card.current{border-color:var(--accent);box-shadow:0 0 0 3px #17695d2c}.card-head{display:flex;align-items:center;gap:7px;padding:7px 8px;border-bottom:1px solid var(--line)}.card-head input{min-height:auto}.title{min-width:0;flex:1;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.date{color:var(--muted);font-size:.75rem}.scrubber{position:relative;height:172px;background:#1e2228;overflow:hidden;cursor:ew-resize}.scrubber img{width:100%;height:100%;object-fit:contain;display:block}.scrub-time{position:absolute;right:6px;bottom:6px;padding:2px 5px;border-radius:4px;background:#000b;color:#fff;font-variant-numeric:tabular-nums}.meta{padding:7px 8px;color:var(--muted);font-size:.78rem;display:grid;gap:4px}.description{height:2.7em;overflow:hidden;color:var(--ink)}.rating,.decisions,.actions{display:flex;gap:5px;align-items:center;flex-wrap:wrap;padding:0 8px 8px}.rating button{padding:.24rem .38rem;color:#8d96a3;background:#f3f5f7;border-color:#d5dae2}.rating button.active{color:#7a5200;background:#ffe5a3;border-color:var(--gold)}.rating .privacy-button{margin-left:12px;padding-left:.55rem;padding-right:.55rem;font-weight:800}.rating .privacy-button.active{color:#fff;background:#68406f;border-color:#68406f}.decisions button{font-size:.76rem}.decisions .active{color:#fff;background:var(--accent);border-color:var(--accent)}.actions{border-top:1px solid var(--line);padding-top:8px}.actions .detail{margin-left:auto}.status-chip{font-size:.69rem;font-weight:750;border-radius:999px;padding:2px 6px;background:#e9edf2}.status-chip.detail_wanted{color:#fff;background:var(--wanted)}.status-chip.detail_completed{color:#fff;background:var(--done)}.status-chip.no_detail{color:#fff;background:var(--reject)}.contact{border-top:1px solid var(--line);padding:7px 8px}.contact summary{cursor:pointer;font-weight:700}.sheet{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:7px}.sheet img{width:100%;aspect-ratio:16/10;object-fit:cover;background:#222;border-radius:3px}.synopsis{padding:0 8px 8px}.synopsis video{display:block;width:100%;max-height:220px;background:#111}.pager{display:flex;justify-content:center;align-items:center;gap:9px;padding:15px}.empty{padding:60px;text-align:center;color:var(--muted)}.help{max-width:900px;margin:8px auto;padding:10px 13px;background:#fff;border:1px solid var(--line);border-radius:8px;color:var(--muted)}.shortcut-warning{position:fixed;right:18px;bottom:18px;z-index:30;padding:8px 11px;border:1px solid #d4a72c;border-radius:7px;background:#fff5cc;color:#624a00;box-shadow:0 4px 16px #0002;font-size:.8rem;font-weight:700}.shortcut-warning[hidden]{display:none}kbd{padding:1px 4px;border:1px solid #c8ced7;border-radius:4px;background:#f5f6f8;color:var(--ink)}@media(max-width:1050px){.triage-layout{grid-template-columns:1fr}.selected-panel{position:relative;top:auto;max-height:none}.player-stage{height:340px}}@media(max-width:700px){.filters{position:static}.bulk{margin-left:0}.grid{grid-template-columns:1fr}.scrubber{height:210px}.player-stage{height:250px}}
.filters{display:grid;gap:8px;padding:10px 13px 8px}.filter-main{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap}.filter-group{display:flex;align-items:flex-end;gap:8px;padding-right:12px;border-right:1px solid var(--line)}.filter-group label{margin:0}.date-filters input{width:136px}.review-filters #minimumRating{width:92px}.review-filters #triageStatus{width:205px}.duration-filters input{width:98px}.discovery-filters{flex:1;min-width:330px}.discovery-filters .search{width:min(240px,32vw)}.discovery-filters #sort{width:155px}.filter-actions{display:flex;align-items:center;gap:8px;margin-left:auto}.filter-actions .checkbox-filter{min-height:34px}.filter-actions #applyFilters{min-height:34px;padding-inline:16px;color:#fff;background:var(--accent);border-color:var(--accent);font-weight:700}.filter-actions #applyFilters:hover{background:#12574d}.bulk{justify-content:flex-end;margin-left:0;padding-top:8px;border-top:1px solid #e6e9ee}.card .scrubber{border-radius:7px 7px 0 0}.meta{height:86px;padding:8px 9px 7px;grid-template-rows:16px 24px 24px;gap:4px;border-bottom:1px solid #edf0f3}.card-title-row,.card-status-row{min-width:0;display:flex;align-items:center;gap:6px}.card-title-row .title{color:var(--ink);font-size:.86rem;line-height:24px}.inline-title-trigger{min-width:0;flex:1;height:24px;padding:0;border:0;border-radius:3px;background:transparent;text-align:left;font-weight:750;overflow:hidden;text-overflow:ellipsis}.inline-title-trigger:hover{background:#edf2f5}.inline-title-input{min-width:0;width:100%;height:24px;min-height:24px;padding:1px 5px;font-size:.86rem;font-weight:750}.description-indicator{flex:none;display:inline-flex;align-items:center;height:20px;padding:0 6px;border-radius:999px;color:#395c72;background:#e8f1f6;font-size:.66rem;font-weight:750;letter-spacing:.02em}.card-status-row input{width:16px;height:16px;min-height:auto;margin:0}.review-state{color:var(--muted);font-size:.7rem;text-transform:capitalize}.card-status-row .status-chip{margin-left:auto}.status-chip.summary_wanted{color:#fff;background:#8a5a12}.rating{padding-top:8px}.summary-action{display:inline-flex;align-items:center;min-width:112px;min-height:34px}.summary-action button{width:100%}@media(max-width:1500px){.filter-actions{margin-left:0}.discovery-filters{flex:0 1 auto}}@media(max-width:900px){.filter-main{align-items:stretch}.filter-group{padding-right:0;border-right:0}.discovery-filters{min-width:100%}.filter-actions{width:100%;justify-content:flex-end}}
.status-chip.no_detail{background:var(--accent)}.status-chip.rejected{color:#fff;background:var(--reject)}.card.duplicate-problem{border-color:#c47b19;background:#fffaf0}.duplicate-warning{color:#8a5700;background:#ffedc2}.keep-copy{color:#fff;background:#a45f00;border-color:#a45f00;font-weight:750}
</style></head><body>
<header><a class="button" href="http://127.0.0.1:8766/">Control Panel</a><a class="button" href="/">Detailed Review</a><h1>Video Rapid Triage</h1><span id="stats" class="stats">Loading…</span><span class="spacer"></span><button id="helpButton" type="button">Shortcuts</button></header>
<section class="filters" aria-label="Triage filters"><div class="filter-main"><div class="filter-group date-filters" aria-label="Date range"><label>From<input id="startDate" type="date"></label><label>To<input id="endDate" type="date"></label></div><div class="filter-group review-filters" aria-label="Review filters"><label>Rating<select id="minimumRating"><option value="0">Any</option><option value="1">1+</option><option value="2">2+</option><option value="3">3+</option><option value="4">4+</option><option value="5">5</option></select></label><label>Status<select id="triageStatus"><option value="not_triaged">Not triaged</option><option value="problems">Problems to resolve</option><option value="summary_wanted">Summary requested</option><option value="detail_wanted">Detailed review wanted</option><option value="no_detail">Embed</option><option value="rejected">Rejected</option><option value="detail_completed">Detailed review completed</option><option value="all">All</option></select></label></div><div class="filter-group duration-filters" aria-label="Duration filters"><label>Min seconds<input id="minimumDuration" type="number" min="0" step="1"></label><label>Max seconds<input id="maximumDuration" type="number" min="0" step="1"></label></div><div class="filter-group discovery-filters" aria-label="Search and sort"><label>Search<input id="search" class="search" type="search" placeholder="filename, title, description"></label><label>Sort<select id="sort"><option value="chronological">Oldest first</option><option value="reverse_chronological">Newest first</option><option value="rating_desc">Rating</option><option value="duration_asc">Duration ↑</option><option value="duration_desc">Duration ↓</option><option value="review_status">Review status</option></select></label></div><div class="filter-actions"><label class="checkbox-filter"><input id="showPrivate" type="checkbox">Show private</label><button id="applyFilters" type="button">Apply filters</button></div></div><div class="bulk"><span id="bulkCount" class="bulk-count">0 selected</span><select id="bulkStatus" aria-label="Bulk status"><option value="">Set status…</option><option value="detail_wanted">Detailed review wanted</option><option value="no_detail">Embed</option></select><button id="applyBulk" type="button">Apply to selected</button></div></section>
<section id="help" class="help" hidden><kbd>J</kbd>/<kbd>K</kbd> or <kbd>↓</kbd>/<kbd>↑</kbd> next/previous · <kbd>1</kbd>–<kbd>5</kbd> rate · <kbd>D</kbd> toggle detailed review · <kbd>S</kbd> toggle summary · <kbd>E</kbd> toggle embed · <kbd>R</kbd> toggle reject · <kbd>P</kbd> toggle private · <kbd>Space</kbd> play/pause · <kbd>←</kbd>/<kbd>→</kbd> step 0.1s · <kbd>Shift</kbd>+<kbd>←</kbd>/<kbd>→</kbd> step 1s · <kbd>.</kbd> screenshot · <kbd>Enter</kbd> detailed review.</section>
<main><div class="triage-layout"><aside id="selectedPanel" class="selected-panel"><div class="empty">Select a video to play and edit.</div></aside><section class="queue-pane"><div id="grid" class="grid" aria-live="polite"></div><nav class="pager"><button id="previousPage" type="button">Previous 48</button><span id="pageStatus" class="stats"></span><button id="nextPage" type="button">Next 48</button></nav></section></div></main>
<div id="shortcutWarning" class="shortcut-warning" role="status" aria-live="polite" hidden>Select or hover over a video first.</div>
<script>
""" + VIDEO_SCRUB_CONTROLLER_JS + r"""
const TRIAGE_PAGE_SIZE=48;const STATE_KEY='project365VideoTriageStateV3';const PLAYER_RATES=[1,1.25,1.5,1.75,2];const PLAYER_SESSION=Date.now().toString(36);let page={items:[],offset:0,total_count:0,summary:{}};let currentIndex=-1;let selectedIds=new Set();let sessionReviewed=0;let activePlayerVideoId='';let playerSerial=0;let playerController=null;let playerTimelineFollower=null;let playerPreparing=false;let playerSelectedTime=0;let playerRate=1;let timelineScrubbing=false;let shortcutWarningTimer=0;const metadataDrafts=new Map();const metadataSavePromises=new Map();const sessionStarted=Date.now();
const grid=document.getElementById('grid');const selectedPanel=document.getElementById('selectedPanel');const startDate=document.getElementById('startDate');const endDate=document.getElementById('endDate');const minimumRating=document.getElementById('minimumRating');const triageStatus=document.getElementById('triageStatus');const minimumDuration=document.getElementById('minimumDuration');const maximumDuration=document.getElementById('maximumDuration');const search=document.getElementById('search');const sort=document.getElementById('sort');const showPrivate=document.getElementById('showPrivate');const bulkStatus=document.getElementById('bulkStatus');const help=document.getElementById('help');
function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]})}
function formatTime(value,precise=false){const number=Math.max(0,Number(value)||0);const total=Math.floor(number);const h=Math.floor(total/3600),m=Math.floor(total%3600/60),s=total%60;const base=h?h+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'):m+':'+String(s).padStart(2,'0');return precise?base+'.'+String(Math.round((number-total)*1000)).padStart(3,'0'):base}
function showShortcutWarning(){const warning=document.getElementById('shortcutWarning');warning.hidden=false;clearTimeout(shortcutWarningTimer);shortcutWarningTimer=setTimeout(function(){warning.hidden=true},2200)}
async function jsonRequest(url,options={}){const response=await fetch(url,{...options,headers:{'content-type':'application/json',...(options.headers||{})}});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'HTTP '+response.status);return payload}
function itemById(id){return page.items.find(function(item){return item.video_id===id})}function primaryFrame(item){return item.frames.find(function(frame){return frame.is_main})||item.frames[0]||null}function displayedTitle(item){return item.title||item.inferred_title||item.filename}
function cardTitleHtml(item){const title=displayedTitle(item);return '<button type="button" class="title inline-title-trigger" data-edit-title title="Edit title: '+escapeHtml(title)+'">'+escapeHtml(title)+'</button>'}
function privacyButtonTitle(item){return item.all_private?'Remove Private from this video and its screenshot sidecars':'Make video and all screenshots private'}
function ratingHtml(item){const stars=[1,2,3,4,5].map(function(value){const active=value<=item.rating;return '<button type="button" data-rating="'+value+'" class="'+(active?'active':'')+'" aria-pressed="'+(active?'true':'false')+'" aria-label="Rate '+value+'">★</button>'}).join('');return stars+'<button type="button" data-private class="privacy-button '+(item.all_private?'active':'')+'" aria-pressed="'+(item.all_private?'true':'false')+'" title="'+privacyButtonTitle(item)+'">P</button>'}
function summaryPlaybackHtml(item){return item.synopsis_status==='ready'?'<button type="button" data-play-synopsis>Play Summary</button>':item.synopsis_status==='source_preferred'?'<button type="button" disabled title="The source is under 45 seconds, so no shorter summary is generated">Use original — already short</button>':''}
function cardHtml(item,index){
 const frame=primaryFrame(item);const image=frame?'<img data-src="'+escapeHtml(frame.image_url)+'" loading="lazy" decoding="async" alt="">':'';const title=displayedTitle(item);
 const summaryTitle=item.synopsis_error||'Queue a short silent summary video';
 const decisions='<button type="button" data-triage="detail_wanted" class="'+(item.triage_status==='detail_wanted'?'active':'')+'" aria-pressed="'+(item.triage_status==='detail_wanted'?'true':'false')+'">Detailed</button><button type="button" data-summary class="'+(item.triage_status==='summary_wanted'?'active':'')+'" aria-pressed="'+(item.triage_status==='summary_wanted'?'true':'false')+'" title="'+escapeHtml(summaryTitle)+'">Summary</button><button type="button" data-triage="no_detail" class="'+(item.triage_status==='no_detail'?'active':'')+'" aria-pressed="'+(item.triage_status==='no_detail'?'true':'false')+'">Embed</button><button type="button" data-reject class="'+(item.triage_status==='rejected'?'active':'')+'" aria-pressed="'+(item.triage_status==='rejected'?'true':'false')+'">Reject</button>';
 const descriptionIndicator=item.description?'<span class="description-indicator" title="Description saved — select this video to view it" aria-label="Description saved">Note</span>':'';
 const triageLabel={summary_wanted:'summary requested',no_detail:'embed'}[item.triage_status]||item.triage_status.replaceAll('_',' ');
 const problem=item.duplicate_state==='problem';const problemChip=problem?'<span class="status-chip duplicate-warning">duplicate problem</span>':'';const keepCopy=problem?'<button type="button" data-keep-copy class="keep-copy">Keep this copy</button>':'';
 return '<article class="card '+(problem?'duplicate-problem ':'')+(index===currentIndex?'current':'')+'" data-video-id="'+escapeHtml(item.video_id)+'"><div class="scrubber" data-scrubber>'+image+'<span class="scrub-time">'+(frame?formatTime(frame.time_seconds):'No frames')+'</span></div><div class="meta"><span class="date">'+escapeHtml(item.capture_timestamp||item.capture_date)+' · '+formatTime(item.duration_seconds)+'</span><div class="card-title-row">'+cardTitleHtml(item)+descriptionIndicator+'</div><div class="card-status-row"><input type="checkbox" data-select '+(selectedIds.has(item.video_id)?'checked':'')+' aria-label="Select '+escapeHtml(title)+'"><span class="review-state">'+escapeHtml(item.review_status)+'</span>'+problemChip+'<span class="status-chip '+escapeHtml(item.triage_status)+'">'+escapeHtml(triageLabel)+'</span></div></div><div class="rating" aria-label="Rating">'+ratingHtml(item)+'</div><div class="decisions" aria-label="Triage decision">'+decisions+'</div><div class="actions"><button type="button" data-play-video>Play</button><span class="summary-action">'+summaryPlaybackHtml(item)+'</span>'+keepCopy+'<button type="button" data-detail class="detail">Detailed Review</button></div><div class="synopsis"></div></article>'
}
function updateStats(){const rate=sessionReviewed/Math.max(1,(Date.now()-sessionStarted)/60000);document.getElementById('stats').textContent=(page.summary.not_triaged||0)+' not triaged · '+(page.summary.problems||0)+' problems · '+(page.summary.summary_wanted||0)+' summaries · '+(page.summary.detail_wanted||0)+' detailed · '+sessionReviewed+' this session · '+rate.toFixed(1)+'/min'}
function render(){grid.innerHTML=page.items.length?page.items.map(cardHtml).join(''):'<div class="empty">No videos match these filters.</div>';document.getElementById('previousPage').disabled=page.offset<=0;document.getElementById('nextPage').disabled=!page.has_more;document.getElementById('pageStatus').textContent=page.total_count?(page.offset+1)+'–'+(page.offset+page.items.length)+' of '+page.total_count:'0 videos';document.getElementById('bulkCount').textContent=selectedIds.size+' selected';updateStats();observeImages();saveState()}
function preloadFrameUrls(frames){for(const frame of frames){const image=new Image();image.decoding='async';image.src=frame.image_url}}
const imageObserver=new IntersectionObserver(function(entries){for(const entry of entries){if(!entry.isIntersecting)continue;const image=entry.target;if(image.dataset.src&&!image.src)image.src=image.dataset.src;const card=image.closest('.card');const item=card&&itemById(card.dataset.videoId);if(item)preloadFrameUrls(item.frames.slice(0,2));imageObserver.unobserve(image)}},{rootMargin:'300px'});
function observeImages(){grid.querySelectorAll('img[data-src]').forEach(function(image){imageObserver.observe(image)})}
function nearestFrame(item,relative){const target=Math.max(0,Math.min(1,relative))*item.duration_seconds;return item.frames.reduce(function(best,frame){return !best||Math.abs(frame.time_seconds-target)<Math.abs(best.time_seconds-target)?frame:best},null)}
function playerElement(){return document.getElementById('triagePlayer')}function panelMessage(value){const node=document.getElementById('panelMessage');if(node)node.textContent=value}
function selectedPanelHtml(item){const frame=primaryFrame(item);const poster=frame?frame.image_url:'';return '<h2>'+escapeHtml(displayedTitle(item))+'</h2><div class="selected-file">'+escapeHtml(item.filename)+'</div><div class="player-stage" data-player-stage title="Play or pause"><video id="triagePlayer" preload="none" playsinline poster="'+escapeHtml(poster)+'"></video><div id="playbackStatus" class="playback-status">Press Play to prepare this video.</div></div><div id="playerTime" class="player-time">Selected: '+formatTime(frame?.time_seconds||0,true)+' / '+formatTime(item.duration_seconds,true)+'</div><input id="playerTimeline" class="player-timeline" type="range" min="0" max="'+item.duration_seconds+'" step="0.05" value="'+(frame?.time_seconds||0)+'" aria-label="Selected timestamp"><div class="player-buttons"><button type="button" data-adjust="-1">−1s</button><button type="button" data-adjust="-0.1">−0.1s</button><button id="playPauseButton" type="button" data-play-pause>Play</button><button type="button" data-adjust="0.1">+0.1s</button><button type="button" data-adjust="1">+1s</button><label>Speed<select id="playbackRate">'+PLAYER_RATES.map(function(rate){return '<option value="'+rate+'"'+(rate===playerRate?' selected':'')+'>'+rate+'×</option>'}).join('')+'</select></label></div><div class="reference-buttons"><button type="button" data-capture class="button">Get screenshot</button><button type="button" data-make-key>Make key</button></div><div class="player-metadata"><label>Title<input id="triageTitle" maxlength="500" value="'+escapeHtml(item.title||item.inferred_title||'')+'"></label><label>Description<textarea id="triageDescription" maxlength="10000">'+escapeHtml(item.description||'')+'</textarea></label><div class="metadata-actions"><button type="button" data-save-metadata>Save title &amp; description</button></div></div><div id="panelMessage" class="panel-message" aria-live="polite"></div>'}
function updatePlayerTime(value){const item=itemById(activePlayerVideoId);playerSelectedTime=Math.max(0,Math.min(Number(item?.duration_seconds)||0,Number(value)||0));const label=document.getElementById('playerTime');const timeline=document.getElementById('playerTimeline');if(label)label.textContent='Selected: '+formatTime(playerSelectedTime,true)+' / '+formatTime(item?.duration_seconds||0,true);if(timeline&&!timeline.matches(':active'))timeline.value=String(playerSelectedTime)}
function adjustSelectedPlayer(delta){if(playerController)playerController.step(Number(delta));else updatePlayerTime(playerSelectedTime+Number(delta))}
function updatePlayButton(){const player=playerElement();const button=document.getElementById('playPauseButton');if(!player||!button)return;const playing=playerController?.snapshot().phase==='playing'&&!player.paused&&!player.ended;button.textContent=playing?'Pause':'Play';button.setAttribute('aria-pressed',playing?'true':'false')}
function bindSelectedPlayer(item,startTime){const player=playerElement();if(!player)return Promise.resolve();if(playerController)playerController.destroy();if(playerTimelineFollower)playerTimelineFollower.destroy();playerController=null;playerTimelineFollower=null;playerSelectedTime=Math.max(0,Math.min(item.duration_seconds,Number(startTime)||0));player.playbackRate=playerRate;player.style.transform='rotate('+(item.rotation_override||0)+'deg)';playerTimelineFollower=createPlaybackTimelineFollower(player,function(value){if(playerController?.snapshot().phase==='playing'&&!player.seeking)updatePlayerTime(value)});player.addEventListener('play',updatePlayButton);player.addEventListener('pause',updatePlayButton);player.addEventListener('ended',updatePlayButton);return new Promise(function(resolve){const initialize=function(){player.pause();playerController=createVideoScrubController(player,{duration:function(){return item.duration_seconds},onSelectedTime:updatePlayerTime,onStateChange:updatePlayButton,onStatus:panelMessage});playerController.reset(playerSelectedTime);playerController.selectExact(playerSelectedTime);updatePlayButton();resolve()};if(player.readyState>=1)initialize();else player.addEventListener('loadedmetadata',initialize,{once:true})})}
async function prepareSelectedPlayback(item,serial,startTime){const statusNode=document.getElementById('playbackStatus');const privateQuery=showPrivate.checked?'?show_private=1':'';let status=await jsonRequest('/api/triage/'+encodeURIComponent(item.video_id)+'/playback',{method:'POST',body:JSON.stringify({show_private:showPrivate.checked})});while(serial===playerSerial&&!['ready','error'].includes(status.status)){if(statusNode)statusNode.textContent=status.message||'Preparing playback…';await new Promise(function(resolve){setTimeout(resolve,500)});status=await jsonRequest('/api/triage/'+encodeURIComponent(item.video_id)+'/playback'+privateQuery)}if(serial!==playerSerial)return;if(status.status==='error')throw new Error(status.error||'Playback preparation failed');if(statusNode)statusNode.hidden=true;const player=playerElement();if(!player)return;player.src='/media/video/'+encodeURIComponent(item.video_id)+'?session='+PLAYER_SESSION;player.load();await bindSelectedPlayer(item,startTime)}
async function playSelected(){const item=itemById(activePlayerVideoId);if(!item||playerPreparing)return;if(!playerController){const serial=playerSerial;playerPreparing=true;const button=document.getElementById('playPauseButton');const statusNode=document.getElementById('playbackStatus');if(button)button.disabled=true;if(statusNode){statusNode.hidden=false;statusNode.textContent='Preparing playback…'}try{await prepareSelectedPlayback(item,serial,playerSelectedTime)}finally{if(serial===playerSerial){playerPreparing=false;if(button)button.disabled=false}}}if(playerController)playerController.togglePlayback()}
async function playFromBeginning(item){setCurrent(page.items.indexOf(item),0);if(playerController){playerController.reset(0);playerController.togglePlayback();return}if(playerPreparing){playerPreparing=false;++playerSerial}await playSelected()}
function activateItem(item,startTime){if(!item)return;const same=item.video_id===activePlayerVideoId;activePlayerVideoId=item.video_id;if(!same){if(playerController)playerController.destroy();if(playerTimelineFollower)playerTimelineFollower.destroy();playerController=null;playerTimelineFollower=null;playerPreparing=false;++playerSerial;selectedPanel.innerHTML=selectedPanelHtml(item);const draft=metadataDrafts.get(item.video_id);if(draft){document.getElementById('triageTitle').value=draft.title;document.getElementById('triageDescription').value=draft.description}updatePlayerTime(Number.isFinite(Number(startTime))?Number(startTime):(primaryFrame(item)?.time_seconds||0))}else if(Number.isFinite(Number(startTime))){playerController?.selectExact(Number(startTime));updatePlayerTime(startTime)}}
function setCurrent(index,startTime){if(!page.items.length||index<0||index>=page.items.length)return;const previousVideoId=activePlayerVideoId;currentIndex=index;grid.querySelectorAll('.card').forEach(function(card,i){card.classList.toggle('current',i===currentIndex)});grid.querySelectorAll('.card')[currentIndex]?.scrollIntoView({block:'nearest'});const item=page.items[currentIndex];if(previousVideoId&&previousVideoId!==item.video_id)saveSelectedMetadata(false).catch(function(error){panelMessage(error.message)});const target=Number.isFinite(Number(startTime))?Number(startTime):(item.video_id===activePlayerVideoId?undefined:primaryFrame(item)?.time_seconds??0);activateItem(item,target);saveState()}
function filters(){return {start_date:startDate.value,end_date:endDate.value,minimum_rating:minimumRating.value,triage_status:triageStatus.value,minimum_duration:minimumDuration.value,maximum_duration:maximumDuration.value,search:search.value.trim(),sort:sort.value,show_private:showPrivate.checked?'1':'0'}}
function saveState(){localStorage.setItem(STATE_KEY,JSON.stringify({filters:filters(),offset:page.offset,currentVideoId:page.items[currentIndex]?.video_id||''}))}
function restoreState(){try{const saved=JSON.parse(localStorage.getItem(STATE_KEY)||'{}');for(const [key,value] of Object.entries(saved.filters||{})){const map={start_date:'startDate',end_date:'endDate',minimum_rating:'minimumRating',triage_status:'triageStatus',minimum_duration:'minimumDuration',maximum_duration:'maximumDuration',search:'search',sort:'sort',show_private:'showPrivate'};const element=document.getElementById(map[key]);if(!element)continue;const restoredValue=key==='triage_status'&&value==='unreviewed'?'not_triaged':value;if(key==='show_private')element.checked=restoredValue==='1';else element.value=restoredValue}return saved}catch(_error){return {}}}
async function load(offset=0,preferred=''){await saveAllMetadataDrafts();grid.innerHTML='<div class="empty">Loading videos…</div>';const query=new URLSearchParams({...filters(),limit:String(TRIAGE_PAGE_SIZE),offset:String(Math.max(0,offset))});page=await jsonRequest('/api/triage?'+query);const found=page.items.findIndex(function(item){return item.video_id===preferred});if(playerController)playerController.destroy();if(playerTimelineFollower)playerTimelineFollower.destroy();playerController=null;playerTimelineFollower=null;playerPreparing=false;++playerSerial;currentIndex=-1;activePlayerVideoId='';render();if(found>=0)setCurrent(found);else selectedPanel.innerHTML='<div class="empty">Select or hover over a video to play and edit.</div>'}
function applyItemChanges(item,changes){const previousStatus=item.triage_status;Object.assign(item,changes);if(changes.triage_status&&changes.triage_status!==previousStatus){page.summary[previousStatus]=Math.max(0,(page.summary[previousStatus]||0)-1);page.summary[changes.triage_status]=(page.summary[changes.triage_status]||0)+1}}
function updateCardState(item){const card=grid.querySelector('[data-video-id="'+CSS.escape(item.video_id)+'"]');if(!card)return;card.querySelectorAll('[data-rating]').forEach(function(button){const active=Number(button.dataset.rating)<=item.rating;button.classList.toggle('active',active);button.setAttribute('aria-pressed',active?'true':'false')});const privacy=card.querySelector('[data-private]');if(privacy){privacy.classList.toggle('active',item.all_private);privacy.setAttribute('aria-pressed',item.all_private?'true':'false');privacy.title=privacyButtonTitle(item)}card.querySelectorAll('[data-triage]').forEach(function(button){const active=button.dataset.triage===item.triage_status;button.classList.toggle('active',active);button.setAttribute('aria-pressed',active?'true':'false')});const summary=card.querySelector('[data-summary]');if(summary){const active=item.triage_status==='summary_wanted';summary.classList.toggle('active',active);summary.setAttribute('aria-pressed',active?'true':'false')}const reject=card.querySelector('[data-reject]');if(reject){const active=item.triage_status==='rejected';reject.classList.toggle('active',active);reject.setAttribute('aria-pressed',active?'true':'false')}const reviewState=card.querySelector('.review-state');if(reviewState)reviewState.textContent=item.review_status;const chip=card.querySelector('.status-chip');if(chip){chip.className='status-chip '+item.triage_status;chip.textContent=({summary_wanted:'summary requested',no_detail:'embedded'}[item.triage_status]||item.triage_status.replaceAll('_',' '))}const summaryAction=card.querySelector('.summary-action');if(summaryAction)summaryAction.innerHTML=summaryPlaybackHtml(item);updateStats();saveState()}
function toggledTriageStatus(item,status){return item.triage_status===status?'not_triaged':status}
async function updateItem(item,changes){await jsonRequest('/api/triage/'+encodeURIComponent(item.video_id),{method:'POST',body:JSON.stringify({...changes,show_private:showPrivate.checked})});sessionReviewed+=1;const applied={...changes};if(changes.triage_status&&changes.triage_status!=='rejected'&&item.review_status==='rejected')applied.review_status='pending';applyItemChanges(item,applied);updateCardState(item)}
async function requestSummary(item){const result=await jsonRequest('/api/triage/'+encodeURIComponent(item.video_id)+'/summary',{method:'POST',body:JSON.stringify({show_private:showPrivate.checked})});sessionReviewed+=1;applyItemChanges(item,result);updateCardState(item);if(['queued','generating'].includes(item.synopsis_status))pollSynopsis(item)}
async function toggleSummary(item){const status=toggledTriageStatus(item,'summary_wanted');return status==='not_triaged'?updateItem(item,{triage_status:status}):requestSummary(item)}
async function rejectItem(item){const result=await jsonRequest('/api/triage/'+encodeURIComponent(item.video_id)+'/reject',{method:'POST',body:JSON.stringify({show_private:showPrivate.checked})});sessionReviewed+=1;applyItemChanges(item,result);updateCardState(item)}
async function toggleReject(item){return item.triage_status==='rejected'?updateItem(item,{triage_status:'not_triaged'}):rejectItem(item)}
async function togglePrivate(item){const requested=!item.all_private;const result=await jsonRequest('/api/videos/'+encodeURIComponent(item.video_id)+'/privacy',{method:'POST',body:JSON.stringify({private:requested})});item.video_private=result.video_private;item.all_private=result.all_private;item.private_count=result.private_count;item.privacy_state=result.privacy_state;item.frames.forEach(function(frame){frame.private=requested});if(requested&&!showPrivate.checked){const preferred=page.items[currentIndex+1]?.video_id||page.items[currentIndex-1]?.video_id||'';await load(page.offset,preferred)}else updateCardState(item)}
async function keepDuplicateCopy(item){await saveAllMetadataDrafts();const result=await jsonRequest('/api/duplicates/'+encodeURIComponent(item.duplicate_group_id)+'/keep',{method:'POST',body:JSON.stringify({video_id:item.video_id})});sessionReviewed+=1;await load(page.offset);return result}
async function saveMetadataForItem(item,force=false){
 if(!item)return;
 const videoId=item.video_id,existing=metadataSavePromises.get(videoId);
 if(existing){await existing;if(metadataDrafts.has(videoId))return saveMetadataForItem(item,false);return}
 if(force){
  const title=document.getElementById('triageTitle'),description=document.getElementById('triageDescription');
  if(title&&description&&activePlayerVideoId===videoId)metadataDrafts.set(videoId,{title:title.value,description:description.value})
 }
 const draft=metadataDrafts.get(videoId);if(!draft)return;
 if(activePlayerVideoId===videoId)panelMessage('Saving title and description…');
 const request=jsonRequest('/api/triage/'+encodeURIComponent(videoId)+'/metadata',{method:'POST',body:JSON.stringify({memory_datetime:item.memory_datetime,title:draft.title,description:draft.description,show_private:showPrivate.checked})});
 metadataSavePromises.set(videoId,request);
 try{
  const result=await request;
  if(metadataDrafts.get(videoId)===draft)metadataDrafts.delete(videoId);
  Object.assign(item,result);render();
  if(activePlayerVideoId===videoId){const heading=selectedPanel.querySelector('h2'),title=document.getElementById('triageTitle'),description=document.getElementById('triageDescription');if(heading)heading.textContent=displayedTitle(item);if(title&&document.activeElement!==title)title.value=item.title||item.inferred_title||'';if(description&&document.activeElement!==description)description.value=item.description||'';panelMessage('Title and description saved')}
 }catch(error){
  if(activePlayerVideoId===videoId)panelMessage('Could not save title and description: '+error.message);
  throw error
 }finally{
  if(metadataSavePromises.get(videoId)===request)metadataSavePromises.delete(videoId)
 }
 if(metadataDrafts.has(videoId))return saveMetadataForItem(item,false)
}
async function saveSelectedMetadata(force=false){return saveMetadataForItem(itemById(activePlayerVideoId),force)}
async function saveAllMetadataDrafts(){for(const videoId of [...metadataDrafts.keys()]){const item=itemById(videoId);if(item)await saveMetadataForItem(item,false)}}
async function captureSelectedFrame(item){panelMessage('Capturing screenshot…');const created=await jsonRequest('/api/triage/'+encodeURIComponent(item.video_id)+'/capture-frame',{method:'POST',body:JSON.stringify({time_seconds:playerSelectedTime,show_private:showPrivate.checked})});item.frames.push(created);item.frames.sort(function(a,b){return a.time_seconds-b.time_seconds||a.frame_index-b.frame_index});render();panelMessage('Screenshot added to this video’s references');return created}
async function makeSelectedKey(){const item=itemById(activePlayerVideoId);if(!item)return;let frame=item.frames.reduce(function(best,value){return !best||Math.abs(value.time_seconds-playerSelectedTime)<Math.abs(best.time_seconds-playerSelectedTime)?value:best},null);if(!frame||Math.abs(frame.time_seconds-playerSelectedTime)>=0.05)frame=await captureSelectedFrame(item);const updated=await jsonRequest('/api/triage/frames/'+encodeURIComponent(frame.frame_id)+'/main',{method:'POST',body:JSON.stringify({show_private:showPrivate.checked})});item.frames.forEach(function(value){value.is_main=value.frame_id===updated.frame_id;if(value.is_main)Object.assign(value,updated)});render();panelMessage('Key frame updated')}
async function openDetail(item){if(!item)return;await saveAllMetadataDrafts();location.href='/?video='+encodeURIComponent(item.video_id)}
function beginInlineTitle(item,button){if(!item||!button)return;const input=document.createElement('input');input.type='text';input.className="inline-title-input";input.dataset.inlineTitle='';input.dataset.initialValue=displayedTitle(item);input.maxLength=500;input.value=displayedTitle(item);input.setAttribute('aria-label','Edit title for '+displayedTitle(item));button.replaceWith(input);input.focus();input.select()}
async function commitInlineTitle(input){if(!input||input.dataset.committing||input.dataset.cancelled)return;const item=itemById(input.closest('.card')?.dataset.videoId);if(!item)return;const title=input.value.trim();if(title===input.dataset.initialValue){render();return}input.dataset.committing='1';const draft=metadataDrafts.get(item.video_id);const panelDescription=activePlayerVideoId===item.video_id?document.getElementById('triageDescription'):null;metadataDrafts.set(item.video_id,{title:title,description:draft?.description??panelDescription?.value??item.description??''});const panelTitle=activePlayerVideoId===item.video_id?document.getElementById('triageTitle'):null;if(panelTitle)panelTitle.value=title;try{await saveMetadataForItem(item,false)}catch(error){delete input.dataset.committing;input.disabled=false;panelMessage(error.message)}}
grid.addEventListener('pointerdown',function(event){const button=event.target.closest('[data-edit-title]');if(!button)return;event.preventDefault();event.stopPropagation();const item=itemById(button.closest('.card')?.dataset.videoId);if(!item)return;setCurrent(page.items.indexOf(item));beginInlineTitle(item,button)});
grid.addEventListener('click',async function(event){const card=event.target.closest('.card');if(!card)return;const item=itemById(card.dataset.videoId);try{if(event.target.closest('[data-play-video]')){await playFromBeginning(item);return}const scrubber=event.target.closest('[data-scrubber]');let startTime;if(scrubber){const rect=scrubber.getBoundingClientRect();startTime=nearestFrame(item,(event.clientX-rect.left)/rect.width)?.time_seconds}setCurrent(page.items.indexOf(item),startTime);const editTitle=event.target.closest('[data-edit-title]');if(editTitle){beginInlineTitle(item,editTitle);return}if(event.target.matches('[data-select]')){event.target.checked?selectedIds.add(item.video_id):selectedIds.delete(item.video_id);render();return}if(event.target.closest('[data-keep-copy]')){await keepDuplicateCopy(item);return}const triage=event.target.closest('[data-triage]');if(triage){await updateItem(item,{triage_status:toggledTriageStatus(item,triage.dataset.triage)});return}if(event.target.closest('[data-summary]')){await toggleSummary(item);return}const rating=event.target.closest('[data-rating]');if(rating){await updateItem(item,{rating:Number(rating.dataset.rating)});return}if(event.target.closest('[data-private]')){await togglePrivate(item);return}if(event.target.closest('[data-reject]')){await toggleReject(item);return}if(event.target.closest('[data-detail]')){await openDetail(item);return}if(event.target.closest('[data-play-synopsis]'))await playSynopsis(item,card)}catch(error){updateCardState(item);alert(error.message)}});
grid.addEventListener('pointermove',function(event){const scrubber=event.target.closest('[data-scrubber]');if(!scrubber)return;const item=itemById(scrubber.closest('.card').dataset.videoId);const rect=scrubber.getBoundingClientRect();const frame=nearestFrame(item,(event.clientX-rect.left)/rect.width);const image=scrubber.querySelector('img');if(frame&&image&&image.dataset.frameId!==frame.frame_id){image.src=frame.image_url;image.dataset.frameId=frame.frame_id;scrubber.querySelector('.scrub-time').textContent=formatTime(frame.time_seconds)}});
grid.addEventListener('pointerover',function(event){const scrubber=event.target.closest('[data-scrubber]');if(!scrubber||scrubber.contains(event.relatedTarget))return;const item=itemById(scrubber.closest('.card').dataset.videoId);setCurrent(page.items.indexOf(item));preloadFrameUrls(item.frames)});grid.addEventListener('pointerout',function(event){const scrubber=event.target.closest('[data-scrubber]');if(!scrubber||scrubber.contains(event.relatedTarget))return;const item=itemById(scrubber.closest('.card').dataset.videoId),frame=primaryFrame(item),image=scrubber.querySelector('img');if(frame&&image){image.src=frame.image_url;image.dataset.frameId=frame.frame_id;scrubber.querySelector('.scrub-time').textContent=formatTime(frame.time_seconds)}});
grid.addEventListener('keydown',function(event){const button=event.target.closest('[data-edit-title]');if(button&&(event.key==='Enter'||event.key===' ')){event.preventDefault();event.stopPropagation();const item=itemById(button.closest('.card')?.dataset.videoId);if(item){setCurrent(page.items.indexOf(item));beginInlineTitle(item,button)}return}const input=event.target.closest('[data-inline-title]');if(!input)return;if(event.key==='Enter'){event.preventDefault();event.stopPropagation();input.blur()}else if(event.key==='Escape'){event.preventDefault();event.stopPropagation();input.dataset.cancelled='1';render()}});
grid.addEventListener('focusout',function(event){const input=event.target.closest('[data-inline-title]');if(input&&!input.dataset.cancelled)commitInlineTitle(input)});
selectedPanel.addEventListener('click',function(event){const item=itemById(activePlayerVideoId);if(!item)return;(async function(){if(event.target.closest('[data-player-stage]')){await playSelected();return}if(event.target.closest('[data-adjust]')){adjustSelectedPlayer(event.target.closest('[data-adjust]').dataset.adjust);return}if(event.target.closest('[data-play-pause]')){await playSelected();return}if(event.target.closest('[data-capture]')){await captureSelectedFrame(item);return}if(event.target.closest('[data-make-key]')){await makeSelectedKey();return}if(event.target.closest('[data-save-metadata]'))await saveSelectedMetadata(true)})().catch(function(error){panelMessage(error.message)})});
selectedPanel.addEventListener('input',function(event){if(!event.target.matches('#triageTitle,#triageDescription'))return;const title=document.getElementById('triageTitle'),description=document.getElementById('triageDescription');if(activePlayerVideoId&&title&&description)metadataDrafts.set(activePlayerVideoId,{title:title.value,description:description.value})});
selectedPanel.addEventListener('focusout',function(event){if(event.target.matches('#triageTitle,#triageDescription'))saveSelectedMetadata(false).catch(function(error){panelMessage(error.message)})});
selectedPanel.addEventListener('pointerdown',function(event){if(event.target.matches('#playerTimeline')&&playerController){timelineScrubbing=true;playerController.beginScrub()}});selectedPanel.addEventListener('input',function(event){if(event.target.matches('#playerTimeline')&&playerController){if(timelineScrubbing)playerController.preview(event.target.value);else playerController.selectExact(event.target.value)}});function finishTimeline(event){if(event.target.matches('#playerTimeline')&&timelineScrubbing&&playerController){timelineScrubbing=false;playerController.finishScrub(event.target.value)}}selectedPanel.addEventListener('pointerup',finishTimeline);selectedPanel.addEventListener('pointercancel',finishTimeline);selectedPanel.addEventListener('change',finishTimeline);selectedPanel.addEventListener('change',function(event){if(event.target.matches('#playbackRate')){const value=Number(event.target.value);if(PLAYER_RATES.includes(value)){playerRate=value;const player=playerElement();if(player)player.playbackRate=value}}});
function showSynopsis(item,card){card.querySelector('.synopsis').innerHTML='<video controls autoplay preload="metadata" src="'+escapeHtml(item.synopsis_url)+'"></video>'}
async function playSynopsis(item,card){const result=await jsonRequest('/api/synopsis/'+encodeURIComponent(item.video_id),{method:'POST',body:'{}'});item.synopsis_status=result.status;item.synopsis_url=result.url;item.synopsis_error=result.error;if(result.status==='ready'){showSynopsis(item,card);return}updateCardState(item);if(['queued','generating'].includes(result.status))pollSynopsis(item,function(){showSynopsis(item,card)})}
async function pollSynopsis(item,onReady=null){for(let count=0;count<600;count++){await new Promise(function(resolve){setTimeout(resolve,1000)});const result=await jsonRequest('/api/synopsis/'+encodeURIComponent(item.video_id));item.synopsis_status=result.status;item.synopsis_url=result.url;item.synopsis_error=result.error;if(!['queued','generating'].includes(result.status)){updateCardState(item);if(result.status==='ready'&&onReady)onReady();return}}}
function currentPageMatchCount(){const status=triageStatus.value;const rating=Number(minimumRating.value)||0;return page.items.filter(function(item){return (status==='all'||(status==='problems'?item.duplicate_state==='problem':item.triage_status===status))&&item.rating>=rating}).length}
document.getElementById('applyFilters').addEventListener('click',function(){load(0).catch(function(error){alert(error.message)})});document.getElementById('previousPage').addEventListener('click',function(){load(page.offset-TRIAGE_PAGE_SIZE).catch(function(error){alert(error.message)})});document.getElementById('nextPage').addEventListener('click',function(){load(page.offset+currentPageMatchCount()).catch(function(error){alert(error.message)})});document.getElementById('helpButton').addEventListener('click',function(){help.hidden=!help.hidden});document.getElementById('applyBulk').addEventListener('click',async function(){const status=bulkStatus.value;if(!status||!selectedIds.size)return;try{await jsonRequest('/api/triage/bulk',{method:'POST',body:JSON.stringify({video_ids:[...selectedIds],triage_status:status,show_private:showPrivate.checked})});sessionReviewed+=selectedIds.size;selectedIds.clear();await load(page.offset)}catch(error){alert(error.message)}});
document.querySelectorAll('header a').forEach(function(link){link.addEventListener('click',async function(event){if(!metadataDrafts.size)return;event.preventDefault();try{await saveAllMetadataDrafts();location.href=link.href}catch(error){panelMessage(error.message)}})});
document.addEventListener('keydown',function(event){if(event.metaKey||event.ctrlKey||event.altKey)return;const editable=event.target?.matches('textarea,select,[contenteditable="true"],input:not(#playerTimeline)');if(editable)return;const key=event.key.toLowerCase();if(event.key==='j'||event.key==='ArrowDown'){event.preventDefault();setCurrent(currentIndex<0?0:currentIndex+1);return}if(event.key==='k'||event.key==='ArrowUp'){event.preventDefault();setCurrent(currentIndex<0?page.items.length-1:currentIndex-1);return}const videoShortcut=(event.key>='1'&&event.key<='5')||['d','s','e','x','r','p','.',' ','arrowleft','arrowright','enter'].includes(key);if(!videoShortcut)return;event.preventDefault();const item=itemById(activePlayerVideoId);if(!item){showShortcutWarning();return}if(event.repeat)return;if(event.key===' '||event.code==='Space'){playSelected().catch(function(error){panelMessage(error.message)});return}if(event.key==='ArrowLeft'||event.key==='ArrowRight'){adjustSelectedPlayer((event.key==='ArrowLeft'?-1:1)*(event.shiftKey?1:0.1));return}if(event.key==='.'){const capture=selectedPanel.querySelector('[data-capture]');if(capture&&!capture.disabled)capture.click();return}if(event.key>='1'&&event.key<='5')updateItem(item,{rating:Number(event.key)}).catch(function(error){alert(error.message)});else if(event.key.toLowerCase()==='d')updateItem(item,{triage_status:toggledTriageStatus(item,'detail_wanted')}).catch(function(error){alert(error.message)});else if(event.key.toLowerCase()==='s')toggleSummary(item).catch(function(error){alert(error.message)});else if(event.key.toLowerCase()==='e'||event.key.toLowerCase()==='x')updateItem(item,{triage_status:toggledTriageStatus(item,'no_detail')}).catch(function(error){alert(error.message)});else if(event.key.toLowerCase()==='r')toggleReject(item).catch(function(error){alert(error.message)});else if(event.key.toLowerCase()==='p')togglePrivate(item).catch(function(error){alert(error.message)});else if(event.key==='Enter')openDetail(item).catch(function(error){alert(error.message)})});
const saved=restoreState();load(Number(saved.offset)||0,saved.currentVideoId||'').catch(function(error){grid.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'});
</script></body></html>"""
# The queue review is intentionally a single-item surface.  Keeping the document
# embedded lets the standalone loopback server remain dependency-free.
REVIEW_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project365 Video Memory Review</title>
<style>
:root{color-scheme:light;--bg:#f6f7f9;--panel:#ffffff;--ink:#202124;--muted:#667085;--line:#d9dee5;--line-strong:#c5ccd6;--accent:#17695d;--accent-soft:#e4f2ef;--hover:#f0f4f7;--gold:#f0a000;--reject:#9f2d2d;--purple:#8f3f7d}
*{box-sizing:border-box}html,body{height:100%;overflow:hidden}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:grid;grid-template-rows:auto minmax(0,1fr);-webkit-font-smoothing:antialiased}button,input,select,textarea{font:inherit}button{color:var(--ink);background:#fff;border:1px solid var(--line-strong);border-radius:6px;padding:.48rem .72rem;cursor:pointer}button:hover{background:var(--hover)}button:disabled{opacity:.45;cursor:default}.primary{color:#fff;background:var(--accent);border-color:var(--accent)}.primary:hover{background:#10594f}.danger{color:var(--reject);background:#fff;border-color:#dca5a5}.icon{font-size:0;line-height:1;padding:.42rem .58rem}.icon:before{font-size:1.35rem}.icon[data-rotate="left"]:before{content:"↺"}.icon[data-rotate="right"]:before{content:"↻"}
header{display:flex;align-items:center;gap:.75rem;padding:.65rem 1rem;background:var(--panel);border-bottom:1px solid var(--line)}header a{color:var(--ink);text-decoration:none;border:1px solid var(--line-strong);border-radius:6px;padding:.42rem .62rem}h1{font-size:1.08rem;margin:0}.spacer{flex:1}#summary{color:var(--muted);white-space:nowrap}.render-errors-button{color:var(--reject);border-color:#dca5a5}.render-errors-button[hidden]{display:none}
.queue-sidebar{min-height:0;display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}.queue-controls{display:grid;gap:7px;padding:10px;border-bottom:1px solid var(--line)}.queue-controls label{color:var(--muted);font-size:.8rem;font-weight:700}.queue-controls select{width:100%;margin-top:3px;color:var(--ink);background:#fff;border:1px solid var(--line-strong);border-radius:6px;padding:.45rem}.queue-summary{color:var(--muted);font-size:.78rem}.queue-list{min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding:7px}.queue-list details{margin:0}.queue-list summary{cursor:pointer;color:var(--muted);font-size:.78rem;font-weight:750;padding:5px 2px;list-style-position:inside}.queue-list .queue-month{margin-left:5px}.queue-list .queue-month[open]>summary{color:var(--accent)}.month-count{float:right;color:var(--muted);font-weight:600;font-variant-numeric:tabular-nums}.queue-loading{padding:5px 18px;color:var(--muted);font-size:.74rem}.queue-item{width:100%;display:grid;grid-template-columns:68px minmax(0,1fr);gap:7px;align-items:center;margin:4px 0;padding:5px;text-align:left;border-color:transparent;background:transparent}.queue-item:hover{background:var(--hover)}.queue-item.active{background:var(--accent-soft);border-color:var(--accent)}.queue-thumb{display:block;width:68px;height:48px;object-fit:cover;background:#222;border-radius:4px}.queue-thumb.empty{height:48px}.queue-copy{min-width:0}.queue-date,.queue-title,.queue-meta{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.queue-date{font-size:.76rem;font-variant-numeric:tabular-nums}.queue-title{font-size:.8rem;font-weight:700}.queue-meta{font-size:.69rem;color:var(--muted)}
main{min-height:0;overflow:hidden;padding:12px;max-width:2100px;width:100%;margin:0 auto;display:grid;grid-template-columns:260px minmax(0,1fr);gap:12px}#review{height:100%;min-height:0;min-width:0}.empty{height:100%;display:grid;place-items:center;color:var(--muted)}.review{height:100%;min-height:0;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr);box-shadow:0 1px 2px rgba(16,24,40,.04)}.review-head{display:flex;align-items:center;gap:.8rem;padding:.65rem .85rem;border-bottom:1px solid var(--line);flex-wrap:wrap}.review-head h2{font-size:1rem;margin:0}.meta{color:var(--muted);font-size:.85rem}.review-actions{margin-left:auto;display:flex;gap:.4rem;align-items:center;flex-wrap:wrap}.privacy-all.private{color:#fff;background:var(--purple);border-color:var(--purple)}.privacy-all.mixed{color:var(--purple);border-color:var(--purple)}
.workspace{min-height:0;overflow:hidden;display:grid;grid-template-columns:minmax(390px,520px) minmax(520px,1fr);gap:0}.viewer{min-height:0;overflow-x:hidden;overflow-y:auto;scrollbar-gutter:stable;padding:12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:8px;background:var(--panel)}.section-title{display:flex;align-items:center;justify-content:space-between;gap:.5rem}.section-title h3{font-size:.9rem;margin:0}.legend{color:var(--muted);font-size:.78rem}
.player-stage{position:relative;height:clamp(250px,42vh,500px);min-height:220px;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#222;border:1px solid var(--line);border-radius:8px}.player-stage video{display:block;width:100%;height:100%;object-fit:contain;transform-origin:center}.playback-preparation{position:absolute;inset:0;display:grid;place-content:center;justify-items:center;gap:12px;padding:28px;color:#fff;background:#222;text-align:center;z-index:2}.playback-preparation[hidden]{display:none}.playback-preparation progress{width:min(360px,75vw);height:14px;accent-color:#56b8a8}.playback-preparation button{margin-top:2px}.player-seek{display:grid;gap:3px}.player-time{color:var(--muted);font-variant-numeric:tabular-nums;text-align:center;font-size:.82rem}.player-timeline{width:100%;min-height:24px;margin:0;padding:0;border:0;border-radius:0;background:transparent;accent-color:var(--accent);cursor:ew-resize}.player-tools{display:grid;justify-items:center;gap:5px}.player-navigation,.capture-tools{display:flex;align-items:center;justify-content:center;gap:5px;flex-wrap:wrap}.player-tools button{padding:.35rem .52rem}#playPauseButton{min-width:96px}.playback-rate{display:flex;align-items:center;gap:4px;color:var(--muted);font-size:.8rem;font-weight:700}.playback-rate select{color:var(--ink);background:#fff;border:1px solid var(--line-strong);border-radius:6px;padding:.3rem .4rem}.capture-status,.status{color:var(--muted);font-size:.82rem}
.key-summary{display:grid;grid-template-columns:160px minmax(0,1fr);gap:10px;margin-top:2px;padding:9px;background:#f8fafc;border:1px solid var(--line);border-radius:8px;min-height:0}.key-column{display:grid;gap:5px;align-content:start}.key-label{font-size:.82rem;font-weight:700}.key-view{height:112px;width:100%;padding:0;overflow:hidden;display:flex;align-items:center;justify-content:center;background:#222;border:1px solid var(--line);border-radius:6px;color:#fff}.key-view img{display:block;width:100%;height:100%;object-fit:contain}.metadata-controls{display:grid;grid-template-columns:minmax(0,1fr) minmax(180px,.55fr);gap:7px;align-content:start}.metadata-controls label{display:grid;gap:3px;color:var(--muted);font-size:.76rem}.metadata-controls .description-field{grid-column:1/-1}.metadata-controls input,.metadata-controls textarea{width:100%;color:var(--ink);background:#fff;border:1px solid var(--line-strong);border-radius:6px;padding:7px 8px}.metadata-controls textarea{height:54px;resize:vertical}.metadata-actions{grid-column:1/-1;display:flex;align-items:center;gap:7px}.metadata-actions .status{flex:1}.selected-strip-wrap{grid-column:1/-1;display:grid;gap:5px}.selected-strip-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.selected-strip-head button{padding:.25rem .5rem;font-size:.74rem}.selected-strip{display:grid;grid-template-columns:repeat(auto-fill,80px);grid-auto-rows:80px;gap:8px;height:168px;overflow-x:hidden;overflow-y:auto;align-content:start;scrollbar-gutter:stable}.selected-mini{width:80px;height:80px;padding:0;border-radius:6px;overflow:hidden;background:#222}.selected-mini.main{outline:4px solid var(--gold);outline-offset:-4px}.selected-mini img{display:block;width:100%;height:100%;object-fit:cover}
.candidates{min-height:0;min-width:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;-webkit-overflow-scrolling:touch;touch-action:pan-y;padding:12px;background:var(--bg)}.candidate-heading{position:sticky;top:-12px;z-index:5;margin:-12px -12px 10px;padding:12px;background:#f6f7f9ed;border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}.candidate-heading .legend:after{content:" · red outer = captured";color:#a51f1f;font-weight:700}.frame-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:34px 24px;align-content:start;padding:20px}.frame{position:relative;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:visible;min-width:0;box-shadow:0 1px 2px rgba(16,24,40,.04)}.frame.ai-selected{box-shadow:0 0 0 4px #1769a4}.frame.ai-selected.selected,.frame.ai-selected.main{box-shadow:0 0 0 15px #1769a4}.frame.user-captured:before{content:"";position:absolute;inset:-4px;border:4px solid #c62828;border-radius:10px;pointer-events:none;z-index:3}.frame.user-captured.selected:before,.frame.user-captured.main:before{inset:-19px}.frame.selected{outline:8px solid var(--accent);outline-offset:3px}.frame.selected .frame-tools{background:#9fe3bd;border-top:2px solid #2f8f65}.frame.main{outline:8px solid var(--gold);outline-offset:3px}.frame.main .frame-tools{background:#fff0c7;border-top-color:#e4bc58}.frame-image{display:block;width:100%;height:210px;padding:0;border:0;border-radius:7px 7px 0 0;overflow:hidden;background:#222}.frame-image img{width:100%;height:100%;object-fit:contain;display:block}.badge{position:absolute;top:.35rem;border-radius:999px;padding:.16rem .38rem;font-size:.68rem;font-weight:750;pointer-events:none;color:#fff;z-index:4}.ai-badge{left:.35rem;background:#1769a4}.key-badge{right:.35rem;background:var(--gold);color:#2b1b00;font-size:.82rem;padding:.24rem .5rem;font-weight:850;border:1px solid #fff9;box-shadow:0 1px 4px #0005}.frame-tools{display:flex;align-items:center;gap:.28rem;padding:.3rem .38rem;border-radius:0 0 7px 7px}.frame-tools button{font-size:.72rem;line-height:1;padding:.22rem .32rem}.frame-time{margin-right:auto}.privacy.private{color:#fff;background:var(--purple);border-color:var(--purple)}
.zoom-overlay[hidden]{display:none}.zoom-overlay{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:3vh 3vw;background:transparent;pointer-events:none}.zoom-overlay img{display:block;max-width:min(82vw,1100px);max-height:82vh;object-fit:contain;background:#111;border:2px solid #fff;border-radius:8px;box-shadow:0 18px 60px rgba(0,0,0,.45)}
.render-errors-dialog[hidden]{display:none}.render-errors-dialog{position:fixed;inset:0;z-index:30;display:grid;place-items:center;padding:24px;background:rgba(16,24,40,.42)}.render-errors-card{width:min(720px,96vw);max-height:min(680px,90vh);display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;gap:12px;padding:18px;background:var(--panel);border-radius:10px;box-shadow:0 20px 70px rgba(0,0,0,.35)}.render-errors-head,.render-errors-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.render-errors-head h2{margin:0;font-size:1.05rem}.render-errors-copy{margin:0;color:var(--muted)}.render-errors-list{overflow:auto;display:grid;gap:8px}.render-error-item{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;padding:10px;border:1px solid var(--line);border-radius:7px}.render-error-meta{font-weight:700}.render-error-message{color:var(--muted);font-size:.82rem}.private-marker{color:var(--purple)}
@media(max-width:1150px){main{grid-template-columns:220px minmax(0,1fr)}.workspace{grid-template-columns:minmax(330px,44%) minmax(430px,56%)}.review-actions{margin-left:0}.player-stage{height:38vh}.key-summary{grid-template-columns:130px minmax(0,1fr)}.key-view{height:96px}.metadata-controls{grid-template-columns:1fr}}@media(max-height:760px){header{padding-top:.42rem;padding-bottom:.42rem}.review-head{padding-top:.45rem;padding-bottom:.45rem}.player-stage{height:34vh;min-height:190px}.key-view{height:88px}}
</style></head><body>
<header><a href="http://127.0.0.1:8766/">Control Panel</a><a href="/triage">Rapid Triage</a><h1>Video Memory Review</h1><span id="summary">Loading…</span><button id="renderErrorsButton" class="render-errors-button" type="button" hidden>View JPG errors</button><span class="spacer"></span><span id="renderStatus" class="status" role="status" aria-live="polite"></span><button id="renderButton" type="button">Render queued JPGs</button></header>
<main><aside class="queue-sidebar" aria-label="Video review queue"><div class="queue-controls"><label for="queueFilter">Video list<select id="queueFilter"><option value="pending">Queued</option><option value="reviewed">Reviewed</option><option value="accepted">Accepted</option><option value="all">All</option></select></label><div id="queueSummary" class="queue-summary">Loading…</div></div><div id="videoQueue" class="queue-list"></div></aside><div id="review"></div></main><div id="frameZoom" class="zoom-overlay" hidden aria-hidden="true"><img id="zoomPreview" alt="Enlarged screenshot"></div><div id="renderErrorsDialog" class="render-errors-dialog" role="dialog" aria-modal="true" aria-labelledby="renderErrorsTitle" hidden><section class="render-errors-card"><div class="render-errors-head"><h2 id="renderErrorsTitle">JPG render errors</h2><button id="renderErrorsClose" type="button">Close</button></div><p id="renderErrorsCopy" class="render-errors-copy"></p><div id="renderErrorsList" class="render-errors-list"></div><div class="render-errors-actions"><span id="renderErrorsStatus" class="status" role="status" aria-live="polite"></span><button id="retryRenderErrors" class="primary" type="button">Retry failed JPGs</button></div></section></div>
<script>
""" + VIDEO_SCRUB_CONTROLLER_JS + r"""
let reviewStatus='pending';let queueMonths=[];let queueTotal=0;let queueMonthCache=new Map();let openQueueMonths=new Set();let currentVideo=null;let selectedPlayerTime=0;let currentPlaybackRate=1;let scrubController=null;let playerTimelineFollower=null;let timelineScrubbing=false;let playbackGeneration=0;let videoLoadSerial=0;let zoomTimer=null;const videoScrubDiagnostics=[];const PLAYBACK_RATES=[1,1.25,1.5,1.75,2];const PLAYBACK_SESSION_TOKEN=Date.now().toString(36);const PLAYBACK_PROGRESS_DELAY_MS=2000;const PLAYBACK_POLL_INTERVAL_MS=500;const ZOOM_HOVER_DELAY_MS=750;const AUTO_OPEN_MONTHS=4;const LAST_VIDEO_KEY='project365VideoMemoryLastVideoId';window.__videoScrubDiagnostics=videoScrubDiagnostics;window.videoScrubSnapshot=function(){return scrubController?.snapshot()||null};
const reviewElement=document.getElementById('review');const queueElement=document.getElementById('videoQueue');
function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,function(ch){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]})}
function formatTime(value,precise=false){const total=Math.max(0,Number(value)||0);const whole=Math.floor(total);const s=whole%60;const mt=Math.floor(whole/60);const m=mt%60;const h=Math.floor(mt/60);const base=h?h+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'):m+':'+String(s).padStart(2,'0');return precise?base+'.'+String(Math.round((total-whole)*1000)).padStart(3,'0'):base}
function openFrameZoom(source){const overlay=document.getElementById('frameZoom');const image=document.getElementById('zoomPreview');if(!overlay||!image||!source)return;clearTimeout(zoomTimer);image.src=source;overlay.hidden=false;overlay.setAttribute('aria-hidden','false')}
function closeFrameZoom(){clearTimeout(zoomTimer);zoomTimer=null;const overlay=document.getElementById('frameZoom');const image=document.getElementById('zoomPreview');if(overlay){overlay.hidden=true;overlay.setAttribute('aria-hidden','true')}if(image)image.removeAttribute('src')}
function scheduleFrameZoom(source){clearTimeout(zoomTimer);zoomTimer=setTimeout(function(){openFrameZoom(source)},ZOOM_HOVER_DELAY_MS)}
function cancelFrameZoom(){closeFrameZoom()}
async function jsonRequest(url,options={}){const response=await fetch(url,{...options,headers:{'content-type':'application/json',...(options.headers||{})}});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'HTTP '+response.status);return payload}
function monthLabel(value){const parsed=new Date(value+'-01T00:00:00');return Number.isNaN(parsed.valueOf())?value:parsed.toLocaleDateString(undefined,{month:'long',year:'numeric'})}
function groupedQueueEntries(months){const years=new Map();months.forEach(function(month){const year=month.month.slice(0,4);if(!years.has(year))years.set(year,[]);years.get(year).push(month)});return years}
function queueItemHtml(entry){const image=entry.key_frame_url?'<img class="queue-thumb" src="'+escapeHtml(entry.key_frame_url)+'" loading="lazy" decoding="async" alt="">':'<span class="queue-thumb empty"></span>';const title=entry.title||'Untitled video';return '<button class="queue-item '+(currentVideo?.video_id===entry.video_id?'active':'')+'" data-video-id="'+escapeHtml(entry.video_id)+'" type="button">'+image+'<span class="queue-copy"><span class="queue-date">'+escapeHtml(entry.memory_datetime)+'</span><span class="queue-title">'+escapeHtml(title)+'</span><span class="queue-meta">'+formatTime(entry.duration_seconds)+' · '+escapeHtml(entry.review_status)+'</span></span></button>'}
function queueMonthHtml(month){const entries=queueMonthCache.get(month.month);const open=openQueueMonths.has(month.month);const body=entries?entries.map(queueItemHtml).join(''):open?'<div class="queue-loading">Loading videos…</div>':'';return '<details class="queue-month" data-month="'+escapeHtml(month.month)+'"'+(open?' open':'')+'><summary>'+escapeHtml(monthLabel(month.month))+'<span class="month-count">'+month.count+'</span></summary>'+body+'</details>'}
function renderQueue(){const scrollTop=queueElement.scrollTop;const groups=groupedQueueEntries(queueMonths);queueElement.innerHTML=groups.size?[...groups].map(function(yearGroup){return '<details open><summary>'+yearGroup[0]+'</summary>'+yearGroup[1].map(queueMonthHtml).join('')+'</details>'}).join(''):'<div class="empty">No videos in this list.</div>';document.getElementById('queueSummary').textContent=queueTotal+' videos · '+queueMonths.length+' populated months';queueElement.scrollTop=scrollTop}
function framesByTime(frames){return [...frames].sort(function(left,right){return Number(left.time_seconds)-Number(right.time_seconds)||Number(left.frame_index)-Number(right.frame_index)})}
function playerVideoUrl(value){playbackGeneration+=1;return String(value)+(String(value).includes('?')?'&':'?')+'session='+encodeURIComponent(PLAYBACK_SESSION_TOKEN+'-'+playbackGeneration)}
function frameHtml(frame){const classes=['frame'];if(frame.ai_selected)classes.push('ai-selected');if(frame.user_captured)classes.push('user-captured');if(frame.selected)classes.push('selected');if(frame.is_main)classes.push('main');return '<article class="'+classes.join(' ')+'" data-frame-id="'+escapeHtml(frame.frame_id)+'"><button class="frame-image" data-toggle="1" type="button" aria-pressed="'+(frame.selected?'true':'false')+'" title="Keep or remove this screenshot"><img src="'+escapeHtml(frame.image_url)+'" loading="lazy" decoding="async" width="640" height="360" alt="Screenshot at '+formatTime(frame.time_seconds)+'"></button>'+(frame.ai_selected?'<span class="badge ai-badge">AI PICK</span>':'')+(frame.is_main?'<span class="badge key-badge">KEY</span>':'')+'<div class="frame-tools"><button class="frame-time" data-seek="'+frame.time_seconds+'" type="button">▶ '+formatTime(frame.time_seconds)+'</button><button data-zoom="1" type="button" title="Enlarge screenshot" aria-label="Enlarge screenshot">🔍</button><button class="privacy '+(frame.private?'private':'')+'" data-private="1" type="button" aria-pressed="'+(frame.private?'true':'false')+'" title="Private screenshot">P</button></div></article>'}
function keyFrameHtml(frame){if(!frame)return '<div class="key-view"><span>No key frame selected</span></div>';return '<button class="key-view" data-seek="'+frame.time_seconds+'" type="button" title="Select key-frame timestamp"><img src="'+escapeHtml(frame.image_url)+'" decoding="async" alt="Current key frame"></button>'}
function selectedFramesHtml(frames){const selected=framesByTime(frames.filter(function(frame){return frame.selected&&!frame.is_main}));if(!selected.length)return '<span class="legend">No additional frames selected</span>';return selected.map(function(frame){return '<button class="selected-mini" data-seek="'+frame.time_seconds+'" type="button" title="Select screenshot timestamp"><img src="'+escapeHtml(frame.image_url)+'" loading="lazy" decoding="async" alt=""></button>'}).join('')}
function playbackRateOptions(){return PLAYBACK_RATES.map(function(rate){return '<option value="'+rate+'"'+(rate===currentPlaybackRate?' selected':'')+'>'+rate+'×</option>'}).join('')}
function videoHtml(video){const privacyClass=video.privacy_state==='all'?'private':video.privacy_state==='mixed'?'mixed':'';const privacyLabel=video.privacy_state==='all'?'P All ✓':video.privacy_state==='mixed'?'P Mixed':'P All';return '<article class="review" data-video-id="'+escapeHtml(video.video_id)+'"><div class="review-head"><div><h2>'+escapeHtml(video.memory_datetime)+' · '+formatTime(video.duration_seconds)+'</h2><div class="meta">'+escapeHtml(video.review_status)+' · AI: '+escapeHtml(video.ai_method||video.ai_status)+'</div></div><div class="review-actions"><button data-open-in-finder="1" type="button">Open in Finder</button><button data-rotate="left" class="icon" type="button" title="Rotate left" aria-label="Rotate video left">↶</button><button data-rotate="right" class="icon" type="button" title="Rotate right" aria-label="Rotate video right">↷</button><button data-video-private="1" class="privacy-all '+privacyClass+'" type="button" aria-pressed="'+(video.all_private?'true':'false')+'">'+privacyLabel+'</button><button data-reject="1" class="danger" type="button">Reject &amp; Next</button><button data-accept="1" class="primary" type="button">Accept &amp; Next</button></div></div><div class="workspace"><section class="viewer"><div class="section-title"><h3>Source video</h3><span class="legend">Timeline previews the video · capture uses the selected time</span></div><div class="player-stage"><video id="sharedPlayer" preload="auto" playsinline></video><div id="playbackPreparation" class="playback-preparation"><span id="playbackPreparationMessage">Checking playback…</span><progress id="playbackProgress" max="100" value="0" hidden></progress><button id="playbackRetry" type="button" onclick="retryVideoPlayback()" hidden>Retry preparation</button></div></div><div class="player-seek"><span id="playerTime" class="player-time">Selected: 0:00.000</span><span id="playerSeekStatus" class="capture-status" aria-live="polite"></span><input id="playerTimeline" type="range" class="player-timeline" min="0" max="'+video.duration_seconds+'" step="0.05" value="0" aria-label="Selected timestamp"></div><div class="player-tools"><div class="player-navigation"><button data-adjust="-1" type="button">−1s</button><button data-adjust="-0.1" type="button">−0.1s</button><button id="playPauseButton" data-play-pause="1" type="button" aria-pressed="false">Play</button><button data-adjust="0.1" type="button">+0.1s</button><button data-adjust="1" type="button">+1s</button><label class="playback-rate">Speed<select id="playbackRate" aria-label="Playback speed">'+playbackRateOptions()+'</select></label></div><div class="capture-tools"><button data-capture="1" class="primary" type="button" title="Capture selected timestamp (.)">Capture selected frame</button><button data-make-key="1" type="button">Make key</button><span id="captureStatus" class="capture-status"></span></div></div><div class="key-summary"><div class="key-column"><span class="key-label">Current key frame</span><div id="keyFramePreview">'+keyFrameHtml(video.frames.find(function(frame){return frame.is_main}))+'</div></div><div class="metadata-controls"><label>Title<input id="memoryTitle" maxlength="500" value="'+escapeHtml(video.title)+'"></label><label>Memory Date<input id="memoryDateTime" type="text" maxlength="17" placeholder="yyyy-mm-dd hhmmss" pattern="\\d{4}-\\d{2}-\\d{2} \\d{6}" value="'+escapeHtml(video.memory_datetime)+'"></label><label class="description-field">Description<textarea id="memoryDescription" maxlength="10000">'+escapeHtml(video.description)+'</textarea></label><div class="metadata-actions"><span class="status"></span></div></div><div class="selected-strip-wrap"><div class="selected-strip-head"><span class="key-label">Selected frames</span><button data-deselect-all="1" type="button">Deselect all</button></div><div id="selectedFrameStrip" class="selected-strip">'+selectedFramesHtml(video.frames)+'</div></div></div></section><section class="candidates"><div class="candidate-heading section-title"><h3>Screenshot choices</h3><span class="legend">Click image to keep · blue = AI · green = kept · gold = key · P = private</span></div><div class="frame-grid">'+video.frames.map(frameHtml).join('')+'</div></section></div></article>'}
function playerElement(){return document.getElementById('sharedPlayer')}function candidatePane(){return reviewElement.querySelector('.candidates')}
function updatePlayPauseButton(){const player=playerElement();const button=document.getElementById('playPauseButton');if(!player||!button)return;const playing=scrubController?.snapshot().phase==='playing'&&!player.paused&&!player.ended;button.textContent=playing?'Pause':'Play';button.setAttribute('aria-pressed',playing?'true':'false')}
function updateSelectedTime(value){const player=playerElement();const label=document.getElementById('playerTime');const timeline=document.getElementById('playerTimeline');if(!player)return;selectedPlayerTime=clampPlayerTime(value);if(label)label.textContent='Selected: '+formatTime(selectedPlayerTime,true)+' / '+formatTime(player.duration||currentVideo?.duration_seconds||0,true);if(timeline&&!timeline.matches(':active'))timeline.value=String(selectedPlayerTime)}
function clampPlayerTime(time){const maximum=Number(playerElement()?.duration)||Number(currentVideo?.duration_seconds)||0;return Math.max(0,Math.min(maximum,Number(time)||0))}
function playerSeekStatus(value){const node=document.getElementById('playerSeekStatus');if(node)node.textContent=value}
function selectPlayerTime(time){scrubController?.selectExact(time)}
function bindPlayer(startTime=0){const player=playerElement();const timeline=document.getElementById('playerTimeline');if(!player)return;if(scrubController)scrubController.destroy();if(playerTimelineFollower)playerTimelineFollower.destroy();scrubController=null;playerTimelineFollower=null;timelineScrubbing=false;selectedPlayerTime=clampPlayerTime(startTime);player.playbackRate=currentPlaybackRate;player.style.transform='rotate('+(currentVideo?.rotation_override||0)+'deg)';playerTimelineFollower=createPlaybackTimelineFollower(player,function(value){if(scrubController?.snapshot().phase==='playing'&&!player.seeking)updateSelectedTime(value)});player.addEventListener('play',updatePlayPauseButton);player.addEventListener('playing',function(){playerSeekStatus('');updatePlayPauseButton()});player.addEventListener('pause',updatePlayPauseButton);player.addEventListener('ended',function(){updateSelectedTime(player.currentTime);updatePlayPauseButton()});player.addEventListener('error',function(){playerSeekStatus('Video playback failed')});const initialize=function(){player.pause();if(timeline)timeline.max=String(player.duration||currentVideo?.duration_seconds||0);scrubController=createVideoScrubController(player,{duration:function(){return Number(currentVideo?.duration_seconds)||0},onSelectedTime:updateSelectedTime,onStateChange:updatePlayPauseButton,onStatus:playerSeekStatus,onDiagnostic:function(item){videoScrubDiagnostics.push(item);if(videoScrubDiagnostics.length>500)videoScrubDiagnostics.shift()}});scrubController.reset(selectedPlayerTime);if(Math.abs(Number(player.currentTime)-selectedPlayerTime)>=0.0005)scrubController.selectExact(selectedPlayerTime);updatePlayPauseButton()};if(player.readyState>=1)initialize();else player.addEventListener('loadedmetadata',initialize,{once:true})}
function adjustPlayer(delta){scrubController?.step(Number(delta))}
function togglePlayback(){scrubController?.togglePlayback()}
function setPlaybackRate(value){const rate=Number(value);if(!PLAYBACK_RATES.includes(rate))return;currentPlaybackRate=rate;const player=playerElement();if(player)player.playbackRate=currentPlaybackRate}
function waitMilliseconds(milliseconds){return new Promise(function(resolve){setTimeout(resolve,milliseconds)})}
function updatePlaybackPreparation(status,startedAt){const host=document.getElementById('playbackPreparation');const message=document.getElementById('playbackPreparationMessage');const progress=document.getElementById('playbackProgress');const retry=document.getElementById('playbackRetry');if(!host||!message||!progress||!retry)return;if(status.status==='ready'){host.hidden=true;return}host.hidden=false;const showProgress=Date.now()-startedAt>=PLAYBACK_PROGRESS_DELAY_MS&&(status.status==='queued'||status.status==='preparing');progress.hidden=!showProgress;progress.value=Math.max(0,Math.min(100,Number(status.progress_percent)||0));retry.hidden=status.status!=='error';if(status.status==='error'){message.textContent=status.error||status.message||'Playback preparation failed';return}const processed=Number(status.processed_seconds)||0;const duration=Number(status.duration_seconds)||0;message.textContent=showProgress?'Preparing playback · '+progress.value+'% · '+formatTime(processed)+' / '+formatTime(duration):(status.message||'Preparing playback…')}
async function prepareVideoPlayback(video,serial,startTime=0){const startedAt=Date.now();let status=await jsonRequest('/api/playback/'+encodeURIComponent(video.video_id),{method:'POST',body:'{}'});while(serial===videoLoadSerial&&status.status!=='ready'&&status.status!=='error'){updatePlaybackPreparation(status,startedAt);await waitMilliseconds(PLAYBACK_POLL_INTERVAL_MS);if(serial!==videoLoadSerial)return;status=await jsonRequest('/api/playback/'+encodeURIComponent(video.video_id))}if(serial!==videoLoadSerial)return;updatePlaybackPreparation(status,startedAt);if(status.status==='error')return;video.playback_url=playerVideoUrl(video.video_url);const player=playerElement();if(!player)return;player.src=video.playback_url;player.load();bindPlayer(startTime)}
function retryVideoPlayback(){if(!currentVideo)return;const retry=document.getElementById('playbackRetry');if(retry)retry.hidden=true;prepareVideoPlayback(currentVideo,videoLoadSerial,selectedPlayerTime).catch(function(error){updatePlaybackPreparation({status:'error',error:error.message},Date.now())})}
let renderRequested=false;
document.getElementById('retryRenderErrors').textContent='Requeue failed JPGs';
function applyRenderSummary(summary){const rendering=Number(summary.rendering)||0;const rendered=Number(summary.rendered)||0;const errors=Number(summary.render_errors)||0;const queuedVideos=Number(summary.queued_render_videos)||0;const videoLabel=queuedVideos+' '+(queuedVideos===1?'video':'videos');const running=Boolean(summary.render_running)||rendering>0;const parts=[summary.pending+' pending',summary.accepted+' accepted',summary.rejected+' rejected',summary.queued_renders+' JPGs queued'];if(rendering)parts.push(rendering+' JPGs rendering');if(rendered)parts.push(rendered+' JPGs rendered');if(errors)parts.push(errors+' JPG render errors');document.getElementById('summary').textContent=parts.join(' · ');const errorsButton=document.getElementById('renderErrorsButton');errorsButton.hidden=!errors;errorsButton.textContent='View '+errors+' JPG '+(errors===1?'error':'errors');const button=document.getElementById('renderButton');button.disabled=running;button.textContent=running?'Rendering JPGs… · '+videoLabel+' queued':'Render queued JPGs · '+videoLabel;const status=document.getElementById('renderStatus');if(summary.render_worker_error){status.textContent='Render stopped: '+summary.render_worker_error;renderRequested=false}else if(running){status.textContent='Rendering queued JPGs…';renderRequested=true}else if(renderRequested){status.textContent=errors?'Rendering finished with '+errors+' errors':'Rendering complete · '+rendered+' JPGs rendered';renderRequested=false}}
async function loadSummary(){const summary=await jsonRequest('/api/summary');applyRenderSummary(summary);return summary}
function renderErrorItemHtml(item){const privacy=item.private?'<span class="private-marker"> · Private</span>':'';return '<article class="render-error-item"><div><div class="render-error-meta">'+escapeHtml(item.memory_datetime)+' · '+formatTime(item.time_seconds,true)+' · '+escapeHtml(item.role)+' JPG'+privacy+'</div><div class="render-error-message">'+escapeHtml(item.message)+'</div></div><button type="button" data-error-video="'+escapeHtml(item.video_id)+'" data-error-month="'+escapeHtml(item.memory_date.slice(0,7))+'" data-error-time="'+escapeHtml(item.time_seconds)+'">Show video</button></article>'}
async function openRenderErrors(){const dialog=document.getElementById('renderErrorsDialog');const list=document.getElementById('renderErrorsList');const copy=document.getElementById('renderErrorsCopy');dialog.hidden=false;list.innerHTML='<div class="queue-loading">Loading errors…</div>';const payload=await jsonRequest('/api/render/errors');copy.textContent=payload.count+' failed JPG'+(payload.count===1?'':'s')+' across '+payload.video_count+' video'+(payload.video_count===1?'':'s')+'. Each row is one JPG, not a whole-video failure.';list.innerHTML=payload.errors.length?payload.errors.map(renderErrorItemHtml).join(''):'<div class="empty">No JPG render errors.</div>';document.getElementById('retryRenderErrors').disabled=!payload.count}
function closeRenderErrors(){document.getElementById('renderErrorsDialog').hidden=true}
async function retryRenderErrors(){const button=document.getElementById('retryRenderErrors');const status=document.getElementById('renderErrorsStatus');button.disabled=true;status.textContent='Requeueing…';try{const summary=await jsonRequest('/api/render/requeue-errors',{method:'POST',body:'{}'});const message=summary.requeued?'Requeued '+summary.requeued+' JPG'+(summary.requeued===1?'':'s')+'. Use Render queued JPGs when ready.':'No selected accepted JPG errors to requeue';status.textContent=message;document.getElementById('renderStatus').textContent=message;applyRenderSummary(summary);if(summary.requeued)closeRenderErrors();else button.disabled=false}catch(error){status.textContent=error.message;button.disabled=false}}
async function loadVideo(videoId,options={}){const serial=++videoLoadSerial;if(scrubController)scrubController.destroy();if(playerTimelineFollower)playerTimelineFollower.destroy();scrubController=null;playerTimelineFollower=null;timelineScrubbing=false;if(!videoId){currentVideo=null;reviewElement.innerHTML='<div class="empty">No videos in this list.</div>';renderQueue();return}reviewElement.innerHTML='<div class="empty">Loading video…</div>';const video=await jsonRequest('/api/videos/'+encodeURIComponent(videoId));if(serial!==videoLoadSerial)return;currentVideo=video;localStorage.setItem(LAST_VIDEO_KEY,currentVideo.video_id);reviewElement.innerHTML=videoHtml(currentVideo);renderQueue();const startTime=Object.prototype.hasOwnProperty.call(options,'playerTime')?options.playerTime:0;prepareVideoPlayback(video,serial,startTime).catch(function(error){if(serial===videoLoadSerial)updatePlaybackPreparation({status:'error',error:error.message},Date.now())});requestAnimationFrame(function(){const pane=candidatePane();if(pane)pane.scrollTop=Number(options.candidateScroll)||0;queueElement.querySelector('.queue-item.active')?.scrollIntoView({block:'nearest'})})}
async function fetchQueueMonth(month=''){const payload=await jsonRequest('/api/video-queue?review_status='+encodeURIComponent(reviewStatus)+'&month='+encodeURIComponent(month));queueMonths=payload.month_counts||[];queueTotal=payload.total_count||0;if(payload.selected_month)queueMonthCache.set(payload.selected_month,payload.entries||[]);return payload}
async function openUpcomingMonths(startMonth){const startIndex=Math.max(0,queueMonths.findIndex(function(item){return item.month===startMonth}));const openCount=reviewStatus==='accepted'||reviewStatus==='reviewed'?1:AUTO_OPEN_MONTHS;const upcoming=queueMonths.slice(startIndex,startIndex+openCount).map(function(item){return item.month});upcoming.forEach(function(month){openQueueMonths.add(month)});await Promise.all(upcoming.filter(function(month){return !queueMonthCache.has(month)}).map(function(month){return fetchQueueMonth(month)}));renderQueue()}
function currentMonthEntries(){const month=currentVideo?.memory_datetime?.slice(0,7)||'';return queueMonthCache.get(month)||[]}
async function loadQueue(options={}){if(options.reset!==false){queueMonthCache=new Map();openQueueMonths=new Set()}const payload=await fetchQueueMonth(options.month||'');const selectedMonth=payload.selected_month;await openUpcomingMonths(selectedMonth);const remembered=options.useRememberedVideo===false?'':localStorage.getItem(LAST_VIDEO_KEY)||'';const preferred=options.preferredVideoId||remembered;const loadedEntries=[...queueMonthCache.values()].flat();let entry=loadedEntries.find(function(item){return item.video_id===preferred});const matchedPreferred=entry?.video_id===preferred;const primary=queueMonthCache.get(selectedMonth)||[];if(!entry&&primary.length)entry=primary[Math.max(0,Math.min(primary.length-1,Number(options.preferredIndex)||0))];const videoOptions=matchedPreferred&&Object.prototype.hasOwnProperty.call(options,'playerTime')?{playerTime:options.playerTime}:{};renderQueue();await loadVideo(entry?.video_id||'',videoOptions);await loadSummary()}
async function navigateQueue(delta){if(!currentVideo)return;const month=currentVideo.memory_datetime.slice(0,7);const entries=queueMonthCache.get(month)||[];const index=entries.findIndex(function(entry){return entry.video_id===currentVideo.video_id});const target=entries[index+delta];if(target)return loadVideo(target.video_id);const monthIndex=queueMonths.findIndex(function(item){return item.month===month});const nextMonth=queueMonths[monthIndex+delta]?.month;if(!nextMonth)return;openQueueMonths.add(nextMonth);if(!queueMonthCache.has(nextMonth))await fetchQueueMonth(nextMonth);if(delta>0)await openUpcomingMonths(nextMonth);else renderQueue();const adjacent=queueMonthCache.get(nextMonth)||[];return loadVideo(adjacent[delta<0?adjacent.length-1:0]?.video_id||'')}
function statusMessage(value){const node=reviewElement.querySelector('.status');if(node)node.textContent=value}
async function postVideo(action,payload={}){if(!currentVideo)return null;return jsonRequest('/api/videos/'+encodeURIComponent(currentVideo.video_id)+'/'+action,{method:'POST',body:JSON.stringify(payload)})}
function frameModel(frameId){return currentVideo?.frames.find(function(frame){return frame.frame_id===frameId})}
function updateChosenFrames(){const keyHost=document.getElementById('keyFramePreview');if(keyHost)keyHost.innerHTML=keyFrameHtml(currentVideo?.frames.find(function(frame){return frame.is_main}));const strip=document.getElementById('selectedFrameStrip');if(strip)strip.innerHTML=selectedFramesHtml(currentVideo?.frames||[])}
function addCapturedFrameToView(created){if(!currentVideo||!created)return;const index=currentVideo.frames.findIndex(function(frame){return frame.frame_id===created.frame_id});if(index>=0)currentVideo.frames[index]=created;else currentVideo.frames.push(created);currentVideo.frames=framesByTime(currentVideo.frames);const grid=reviewElement.querySelector('.frame-grid');if(grid)grid.innerHTML=currentVideo.frames.map(frameHtml).join('');updateChosenFrames();const status=document.getElementById('captureStatus');if(status)status.textContent='Captured'}
function updateFrameDom(frame){const card=reviewElement.querySelector('[data-frame-id="'+CSS.escape(frame.frame_id)+'"]');if(!card)return;card.classList.toggle('ai-selected',Boolean(frame.ai_selected));card.classList.toggle('user-captured',Boolean(frame.user_captured));card.classList.toggle('selected',Boolean(frame.selected));card.classList.toggle('main',Boolean(frame.is_main));const toggle=card.querySelector('[data-toggle]');if(toggle)toggle.setAttribute('aria-pressed',frame.selected?'true':'false');const privacy=card.querySelector('[data-private]');if(privacy){privacy.classList.toggle('private',Boolean(frame.private));privacy.setAttribute('aria-pressed',frame.private?'true':'false')}let badge=card.querySelector('.key-badge');if(frame.is_main&&!badge){badge=document.createElement('span');badge.className='badge key-badge';badge.textContent='KEY';card.appendChild(badge)}else if(!frame.is_main&&badge)badge.remove()}
function updateFrameFromResponse(response){const frame=frameModel(response.frame_id);if(!frame)return;Object.assign(frame,response);updateFrameDom(frame);updateChosenFrames()}
function updateVideoPrivacyButton(privacy){const button=reviewElement.querySelector('[data-video-private]');if(!button)return;button.classList.toggle('private',privacy.privacy_state==='all');button.classList.toggle('mixed',privacy.privacy_state==='mixed');button.setAttribute('aria-pressed',privacy.all_private?'true':'false');button.textContent=privacy.privacy_state==='all'?'P All ✓':privacy.privacy_state==='mixed'?'P Mixed':'P All'}
function metadataPayload(){return {memory_datetime:document.getElementById('memoryDateTime').value,title:document.getElementById('memoryTitle').value,description:document.getElementById('memoryDescription').value}}
async function saveCurrentMetadata(){if(!currentVideo)return;statusMessage('Saving…');const result=await postVideo('metadata',metadataPayload());Object.assign(currentVideo,result);statusMessage('Saved '+result.memory_datetime)}
async function reloadCurrentPreservingView(){const pane=candidatePane();return loadVideo(currentVideo.video_id,{candidateScroll:pane?.scrollTop||0,playerTime:selectedPlayerTime})}
async function advanceAfterDecision(newStatus){const month=currentVideo?.memory_datetime.slice(0,7)||'';const entries=queueMonthCache.get(month)||[];const currentIndex=Math.max(0,entries.findIndex(function(entry){return entry.video_id===currentVideo?.video_id}));const preferred=reviewStatus==='all'||reviewStatus==='reviewed'||reviewStatus===newStatus?currentVideo?.video_id:'';return loadQueue({month:month,preferredVideoId:preferred,preferredIndex:currentIndex})}
async function acceptCurrent(){if(!currentVideo)return;statusMessage('Accepting…');await postVideo('accept',metadataPayload());return advanceAfterDecision('accepted')}
async function rejectCurrent(){if(!currentVideo)return;statusMessage('Rejecting…');await postVideo('reject');return advanceAfterDecision('rejected')}
async function makeCurrentFrameKey(){if(!currentVideo)return;const keyTime=selectedPlayerTime;let target=currentVideo.frames.reduce(function(best,frame){return !best||Math.abs(frame.time_seconds-keyTime)<Math.abs(best.time_seconds-keyTime)?frame:best},null);if(!target||Math.abs(Number(target.time_seconds)-keyTime)>=0.05){statusMessage('Capturing key frame…');const created=await postVideo('capture-frame',{time_seconds:keyTime});addCapturedFrameToView(created);target=frameModel(created.frame_id)}const response=await jsonRequest('/api/frames/'+encodeURIComponent(target.frame_id)+'/main',{method:'POST',body:'{}'});currentVideo.frames.forEach(function(frame){frame.is_main=frame.frame_id===response.frame_id;if(frame.is_main){frame.selected=true;Object.assign(frame,response)}updateFrameDom(frame)});const queueEntry=[...queueMonthCache.values()].flat().find(function(entry){return entry.video_id===currentVideo.video_id});if(queueEntry)queueEntry.key_frame_url=response.image_url;updateChosenFrames();renderQueue();statusMessage('Key frame updated')}
reviewElement.addEventListener('click',async function(event){const button=event.target.closest('[data-deselect-all]');if(!button||!currentVideo)return;event.stopImmediatePropagation();button.disabled=true;statusMessage('Deselecting…');try{await postVideo('deselect-all');currentVideo.frames.forEach(function(frame){if(!frame.is_main){frame.selected=false;updateFrameDom(frame)}});updateChosenFrames();statusMessage('Additional frames deselected')}catch(error){statusMessage(error.message)}finally{button.disabled=false}})
reviewElement.addEventListener('click',async function(event){
 if(!currentVideo)return;
 const card=event.target.closest('.frame');
 try{
  if(event.target.closest('[data-toggle]')){const response=await jsonRequest('/api/frames/'+encodeURIComponent(card.dataset.frameId)+'/selection',{method:'POST',body:JSON.stringify({selected:!card.classList.contains('selected')})});updateFrameFromResponse(response);return}
  if(event.target.closest('[data-private]')){const response=await jsonRequest('/api/frames/'+encodeURIComponent(card.dataset.frameId)+'/privacy',{method:'POST',body:JSON.stringify({private:!event.target.closest('[data-private]').classList.contains('private')})});updateFrameFromResponse(response);updateVideoPrivacyButton(response.video_privacy);return}
  if(event.target.closest('[data-seek]')){selectPlayerTime(event.target.closest('[data-seek]').dataset.seek);return}
  if(event.target.closest('[data-adjust]')){adjustPlayer(event.target.closest('[data-adjust]').dataset.adjust);return}
  if(event.target.closest('[data-play-pause]')){togglePlayback();return}
  if(event.target.closest('[data-capture]')){const button=event.target.closest('[data-capture]');const captureTime=selectedPlayerTime;button.disabled=true;document.getElementById('captureStatus').textContent='Capturing…';try{const created=await postVideo('capture-frame',{time_seconds:captureTime});addCapturedFrameToView(created)}finally{button.disabled=false}return}
  if(event.target.closest('[data-make-key]')){await makeCurrentFrameKey();return}
  if(event.target.closest('[data-video-private]')){const privacy=await postVideo('privacy',{private:event.target.closest('[data-video-private]').getAttribute('aria-pressed')!=='true'});currentVideo.frames.forEach(function(frame){frame.private=privacy.all_private;updateFrameDom(frame)});updateVideoPrivacyButton(privacy);return}
  if(event.target.closest('[data-open-in-finder]')){const button=event.target.closest('[data-open-in-finder]');button.disabled=true;statusMessage('Opening Finder…');try{await postVideo('open-in-finder');statusMessage('Selected source video in Finder')}finally{button.disabled=false}return}
  if(event.target.closest('[data-save-metadata]')){await saveCurrentMetadata();return}
  if(event.target.closest('[data-rotate]')){statusMessage('Reprocessing screenshots…');await postVideo('rotate',{direction:event.target.closest('[data-rotate]').dataset.rotate});return reloadCurrentPreservingView()}
  if(event.target.closest('[data-accept]'))return acceptCurrent();
  if(event.target.closest('[data-reject]'))return rejectCurrent()
 }catch(error){statusMessage(error.message);const capture=document.getElementById('captureStatus');if(capture)capture.textContent=error.message}
})
reviewElement.addEventListener('pointerover',function(event){const target=event.target.closest('.frame-image,[data-zoom]');if(!target||target.contains(event.relatedTarget))return;const image=target.closest('.frame')?.querySelector('.frame-image img');scheduleFrameZoom(image?.currentSrc||image?.src)})
reviewElement.addEventListener('pointerout',function(event){const target=event.target.closest('.frame-image,[data-zoom]');if(!target||target.contains(event.relatedTarget))return;cancelFrameZoom()})
document.addEventListener('keydown',function(event){const overlay=document.getElementById('frameZoom');if(overlay.hidden||event.key!=='Escape')return;closeFrameZoom();event.preventDefault();event.stopImmediatePropagation()},{capture:true})
reviewElement.addEventListener('pointerdown',function(event){if(!event.target.matches('#playerTimeline')||!scrubController)return;timelineScrubbing=true;scrubController.beginScrub()})
reviewElement.addEventListener('input',function(event){if(!event.target.matches('#playerTimeline')||!scrubController)return;if(timelineScrubbing)scrubController.preview(event.target.value);else scrubController.selectExact(event.target.value)})
function finishTimelineScrub(event){if(!event.target.matches('#playerTimeline')||!timelineScrubbing||!scrubController)return;timelineScrubbing=false;scrubController.finishScrub(event.target.value)}
reviewElement.addEventListener('pointerup',finishTimelineScrub);reviewElement.addEventListener('pointercancel',finishTimelineScrub);reviewElement.addEventListener('change',finishTimelineScrub);reviewElement.addEventListener('change',function(event){if(event.target.matches('#playbackRate'))setPlaybackRate(event.target.value)})
queueElement.addEventListener('click',function(event){const item=event.target.closest('[data-video-id]');if(!item)return;const entry=[...queueMonthCache.values()].flat().find(function(value){return value.video_id===item.dataset.videoId});const month=entry?.memory_datetime.slice(0,7)||'';openUpcomingMonths(month).then(function(){return loadVideo(item.dataset.videoId)}).catch(function(error){reviewElement.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'})});queueElement.addEventListener('toggle',function(event){const details=event.target.closest('.queue-month');if(!details)return;const month=details.dataset.month;if(!details.open){openQueueMonths.delete(month);return}const wasOpen=openQueueMonths.has(month);openQueueMonths.add(month);if(!wasOpen){openUpcomingMonths(month).catch(function(error){document.getElementById('queueSummary').textContent=error.message});return}if(queueMonthCache.has(month))return;fetchQueueMonth(month).then(renderQueue).catch(function(error){queueMonthCache.set(month,[]);renderQueue();document.getElementById('queueSummary').textContent=error.message})},true);document.getElementById('queueFilter').addEventListener('change',function(event){reviewStatus=event.target.value;loadQueue({month:''}).catch(function(error){queueElement.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'})});
document.getElementById('renderErrorsButton').addEventListener('click',function(){openRenderErrors().catch(function(error){document.getElementById('renderStatus').textContent=error.message})});document.getElementById('renderErrorsClose').addEventListener('click',closeRenderErrors);document.getElementById('renderErrorsDialog').addEventListener('click',function(event){if(event.target===event.currentTarget)closeRenderErrors()});document.getElementById('retryRenderErrors').addEventListener('click',retryRenderErrors);document.getElementById('renderErrorsList').addEventListener('click',function(event){const button=event.target.closest('[data-error-video]');if(!button)return;reviewStatus='accepted';document.getElementById('queueFilter').value='accepted';closeRenderErrors();loadQueue({month:button.dataset.errorMonth,preferredVideoId:button.dataset.errorVideo,playerTime:Number(button.dataset.errorTime)}).catch(function(error){document.getElementById('renderStatus').textContent=error.message})});document.addEventListener('keydown',function(event){if(event.key==='Escape'&&!document.getElementById('renderErrorsDialog').hidden){closeRenderErrors();event.preventDefault()}});
document.getElementById('renderButton').addEventListener('click',async function(event){event.target.disabled=true;document.getElementById('renderStatus').textContent='Starting render…';try{const summary=await jsonRequest('/api/render/start',{method:'POST',body:'{}'});renderRequested=Boolean(summary.started);document.getElementById('renderStatus').textContent=summary.started?'Render started':'No queued JPGs to render';applyRenderSummary(summary)}catch(error){document.getElementById('renderStatus').textContent=error.message;event.target.disabled=false}});document.addEventListener('keydown',function(event){if(event.metaKey||event.ctrlKey||event.altKey)return;const editable=event.target?.matches('textarea,select,[contenteditable="true"],input:not(#playerTimeline)');if(editable)return;if(event.key==='Enter'&&!event.repeat){event.preventDefault();acceptCurrent();return}if((event.key===' '||event.code==='Space')&&!event.repeat){event.preventDefault();togglePlayback();return}if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();if(event.repeat)return;adjustPlayer((event.key==='ArrowLeft'?-1:1)*(event.shiftKey?1:0.1));return}if(event.key==='.'&&!event.repeat){const capture=reviewElement.querySelector('[data-capture]');if(capture&&!capture.disabled){event.preventDefault();capture.click()}}});setInterval(loadSummary,3000);reviewStatus='pending';document.getElementById('queueFilter').value='pending';const initialVideoId=new URLSearchParams(location.search).get('video')||'';if(initialVideoId)jsonRequest('/api/videos/'+encodeURIComponent(initialVideoId)).then(function(video){return loadQueue({month:video.memory_datetime.slice(0,7),preferredVideoId:initialVideoId,useRememberedVideo:false})}).catch(function(error){reviewElement.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'});else loadQueue({month:'',useRememberedVideo:false}).catch(function(error){reviewElement.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'});
</script></body></html>"""
REVIEW_HTML = REVIEW_HTML.replace(
    "+' · AI: '+escapeHtml(video.ai_method||video.ai_status)",
    "",
)
REVIEW_HTML = REVIEW_HTML.replace(
    "</style></head><body>",
    ".diary-output{grid-column:1/-1;display:flex;align-items:center;gap:10px;margin:0;padding:7px 9px;border:1px solid var(--line);border-radius:7px}.diary-output legend{padding:0 4px;color:var(--muted);font-size:.74rem;font-weight:750}.diary-output label{display:flex;align-items:center;gap:4px;color:var(--ink);font-size:.78rem}.diary-output input{width:16px;height:16px;margin:0;accent-color:var(--accent)}</style></head><body>",
)
REVIEW_HTML = REVIEW_HTML.replace(
    '<div class="metadata-actions"><span class="status"></span></div>',
    r'''<fieldset class="diary-output"><legend>Diary output</legend><label><input type="checkbox" data-detail-output="screenshots" '+(video.output_screenshots?'checked':'')+'>Screenshots</label><label><input type="checkbox" data-detail-output="embedded" '+(video.output_embedded?'checked':'')+'>Embedded</label><label><input type="checkbox" data-detail-output="summary" '+(video.output_summary?'checked':'')+'>Summary</label></fieldset><div class="metadata-actions"><span class="status"></span></div>''',
)
REVIEW_HTML = REVIEW_HTML.replace(
    "reviewElement.addEventListener('change',function(event){if(event.target.matches('#playbackRate'))setPlaybackRate(event.target.value)})",
    r'''reviewElement.addEventListener('change',function(event){if(event.target.matches('#playbackRate'))setPlaybackRate(event.target.value)})
reviewElement.addEventListener('change',async function(event){const input=event.target.closest('[data-detail-output]');if(!input||!currentVideo)return;const name=input.dataset.detailOutput;input.disabled=true;statusMessage('Saving diary output…');try{const result=await postVideo('outputs',{[name]:input.checked});Object.assign(currentVideo,result);if(name==='summary'&&input.checked){const synopsis=await jsonRequest('/api/synopsis/'+encodeURIComponent(currentVideo.video_id),{method:'POST',body:'{}'});currentVideo.synopsis_status=synopsis.status;currentVideo.synopsis_url=synopsis.url||'';currentVideo.synopsis_error=synopsis.error||''}statusMessage('Diary output saved')}catch(error){input.checked=Boolean(currentVideo['output_'+name]);statusMessage(error.message)}finally{input.disabled=false}})''',
)

def parse_byte_range(header: str, file_size: int) -> tuple[int, int] | None:
    if not header:
        return None
    if file_size <= 0 or not header.startswith("bytes=") or "," in header:
        raise ValueError("Invalid byte range")
    start_text, separator, end_text = header.removeprefix("bytes=").partition("-")
    if not separator:
        raise ValueError("Invalid byte range")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
        else:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start = max(0, file_size - suffix)
            end = file_size - 1
    except ValueError as exc:
        raise ValueError("Invalid byte range") from exc
    if start < 0 or start >= file_size or end < start:
        raise ValueError("Range outside file")
    return start, min(end, file_size - 1)


class VideoMemoryServerState:
    def __init__(self, canonical_root: Path):
        self.canonical_root = Path(canonical_root)
        self.store = VideoMemoryStore(self.canonical_root / "video_memory_review.sqlite")
        self.cache_root = (self.canonical_root / "cache" / "video_memory_review").resolve()
        self.output_root = self.canonical_root / "media" / "video_memory_frames"
        self._render_lock = threading.Lock()
        self._render_thread: threading.Thread | None = None
        self._render_error = ""
        self._playback_job_lock = threading.Lock()
        self._playback_encode_lock = threading.Lock()
        self._playback_jobs: dict[str, dict[str, Any]] = {}
        self._playback_threads: dict[str, threading.Thread] = {}
        self._synopsis_job_lock = threading.Lock()
        self._synopsis_encode_lock = threading.Lock()
        self._synopsis_threads: dict[str, threading.Thread] = {}
        self.synopsis_root = (self.canonical_root / "media" / "video_synopses").resolve()

    def has_background_work(self) -> bool:
        with self._render_lock:
            if self._render_thread is not None and self._render_thread.is_alive():
                return True
        with self._playback_job_lock:
            if any(thread.is_alive() for thread in self._playback_threads.values()):
                return True
        with self._synopsis_job_lock:
            return any(thread.is_alive() for thread in self._synopsis_threads.values())

    def ready_playback_path(self, video_id: str) -> Path | None:
        path = playback_proxy_path(self.cache_root, video_id)
        return path if path.is_file() and path.stat().st_size > 0 else None

    def playback_status(self, video_id: str, duration_seconds: float) -> dict[str, Any]:
        duration = max(0.0, float(duration_seconds or 0.0))
        if self.ready_playback_path(video_id) is not None:
            return {
                "video_id": video_id,
                "status": "ready",
                "progress_percent": 100,
                "processed_seconds": duration,
                "duration_seconds": duration,
                "message": "Playback ready",
                "error": "",
            }
        with self._playback_job_lock:
            job = dict(self._playback_jobs.get(video_id, {}))
        if job:
            return job
        return {
            "video_id": video_id,
            "status": "idle",
            "progress_percent": 0,
            "processed_seconds": 0.0,
            "duration_seconds": duration,
            "message": "Playback preparation has not started",
            "error": "",
        }

    def start_playback_preparation(
        self,
        source: Path,
        video_id: str,
        duration_seconds: float,
    ) -> dict[str, Any]:
        duration = max(0.0, float(duration_seconds or 0.0))
        ready = self.ready_playback_path(video_id)
        if ready is not None:
            return self.playback_status(video_id, duration)
        with self._playback_job_lock:
            existing = self._playback_threads.get(video_id)
            if existing is not None and existing.is_alive():
                return dict(self._playback_jobs[video_id])
            self._playback_jobs[video_id] = {
                "video_id": video_id,
                "status": "queued",
                "progress_percent": 0,
                "processed_seconds": 0.0,
                "duration_seconds": duration,
                "message": "Queued for playback preparation",
                "error": "",
            }
            thread = threading.Thread(
                target=self._playback_worker,
                args=(Path(source), video_id, duration),
                daemon=True,
                name=f"video-memory-playback-{video_id[:12]}",
            )
            self._playback_threads[video_id] = thread
            thread.start()
        return self.playback_status(video_id, duration)

    def _playback_worker(self, source: Path, video_id: str, duration: float) -> None:
        def update_progress(processed_seconds: float) -> None:
            processed = max(0.0, min(duration, float(processed_seconds or 0.0)))
            percent = min(99, int(processed * 100 / duration)) if duration > 0 else 0
            with self._playback_job_lock:
                job = self._playback_jobs.get(video_id)
                if job is None:
                    return
                job.update(
                    {
                        "status": "preparing",
                        "progress_percent": percent,
                        "processed_seconds": round(processed, 3),
                        "message": f"Preparing playback · {percent}%",
                    }
                )

        with self._playback_encode_lock:
            with self._playback_job_lock:
                self._playback_jobs[video_id].update(
                    {"status": "preparing", "message": "Preparing playback"}
                )
            try:
                scrub_optimized_video(
                    source,
                    self.cache_root,
                    video_id,
                    duration_seconds=duration,
                    progress_callback=update_progress,
                )
            except subprocess.TimeoutExpired:
                error = (
                    "Playback preparation timed out after "
                    f"{playback_conversion_timeout_seconds(duration)} seconds"
                )
                with self._playback_job_lock:
                    self._playback_jobs[video_id].update(
                        {"status": "error", "message": error, "error": error}
                    )
                return
            except Exception as exc:
                error = str(exc).strip() or "Playback preparation failed"
                with self._playback_job_lock:
                    self._playback_jobs[video_id].update(
                        {
                            "status": "error",
                            "message": "Playback preparation failed",
                            "error": error[:1000],
                        }
                    )
                return
            with self._playback_job_lock:
                self._playback_jobs[video_id].update(
                    {
                        "status": "ready",
                        "progress_percent": 100,
                        "processed_seconds": duration,
                        "message": "Playback ready",
                        "error": "",
                    }
                )

    def synopsis_status(self, video_id: str) -> dict[str, Any]:
        record = self.store.synopsis_record(video_id)
        if record is None:
            raise ValueError("Unknown or unavailable video")
        if str(record["synopsis_status"]) == "ready" and not synopsis_artifact_is_current(
            Path(str(record["synopsis_path"]))
        ):
            self.store.set_synopsis_state(video_id, "not_requested")
            record = self.store.synopsis_record(video_id)
            if record is None:
                raise ValueError("Unknown or unavailable video")
        return {
            "video_id": video_id,
            "status": str(record["synopsis_status"]),
            "url": (
                synopsis_media_url(video_id, str(record["synopsis_updated_at"] or ""))
                if str(record["synopsis_status"]) == "ready"
                else ""
            ),
            "error": str(record["synopsis_error"] or ""),
            "updated_at": str(record["synopsis_updated_at"] or ""),
        }

    def start_synopsis(self, video_id: str) -> dict[str, Any]:
        record = self.store.video_record(video_id)
        if record is None or str(record.get("status")) != "ready":
            raise ValueError("Unknown or unavailable video")
        current = self.synopsis_status(video_id)
        if current["status"] in {"ready", "source_preferred"}:
            return current
        with self._synopsis_job_lock:
            existing = self._synopsis_threads.get(video_id)
            if existing is not None and existing.is_alive():
                return self.synopsis_status(video_id)
            self.store.set_synopsis_state(video_id, "queued")
            thread = threading.Thread(
                target=self._synopsis_worker,
                args=(video_id,),
                daemon=True,
                name=f"video-synopsis-{video_id[:12]}",
            )
            self._synopsis_threads[video_id] = thread
            thread.start()
        return self.synopsis_status(video_id)

    def _synopsis_worker(self, video_id: str) -> None:
        with self._synopsis_encode_lock:
            try:
                record = self.store.video_record(video_id)
                detail = self.store.video_detail(video_id)
                if record is None or detail is None:
                    raise ValueError("Unknown or unavailable video")
                self.store.set_synopsis_state(video_id, "generating")
                record["frames"] = detail["frames"]
                status, path = generate_synopsis(record, self.synopsis_root)
                self.store.set_synopsis_state(video_id, status, path=path)
            except Exception as exc:
                self.store.set_synopsis_state(video_id, "error", error=str(exc))

    def start_render(self) -> bool:
        with self._render_lock:
            if self._render_thread and self._render_thread.is_alive():
                return False
            if not self.store.queued_render_count():
                return False
            self._render_error = ""
            thread = threading.Thread(target=self._render_worker, daemon=True, name="video-memory-render")
            self._render_thread = thread
            thread.start()
            return True

    def requeue_render_errors(self) -> dict[str, Any]:
        with self._render_lock:
            if self._render_thread and self._render_thread.is_alive():
                raise ValueError("Wait for the current JPG render to finish before requeueing errors")
            requeued = self.store.requeue_render_errors()
        return {"requeued": requeued, **self.render_summary()}

    def _render_worker(self) -> None:
        try:
            renderer = final_renderer(self.output_root)
            while self.store.queued_render_count():
                result = process_render_queue(self.store, renderer, limit=8)
                if not result["processed"]:
                    break
        except Exception as exc:
            with self._render_lock:
                self._render_error = str(exc).strip()[:1000] or "Render worker failed"

    def render_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = self.store.summary()
        with self._render_lock:
            summary["render_running"] = bool(
                self._render_thread and self._render_thread.is_alive()
            )
            summary["render_worker_error"] = self._render_error
        return summary


def create_handler(state: VideoMemoryServerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Project365VideoMemory/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            try:
                if parsed.path in {"/", "/video-memory"}:
                    self._send_html(REVIEW_HTML)
                    return
                if parsed.path == "/triage":
                    self._send_html(TRIAGE_HTML)
                    return
                if parsed.path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.send_header("cache-control", "private, max-age=86400")
                    self.end_headers()
                    return
                if parsed.path == "/api/video-queue":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        state.store.video_queue_month(
                            review_status=str(query.get("review_status", ["pending"])[0]),
                            selected_month=str(query.get("month", [""])[0]),
                        )
                    )
                    return
                if parsed.path == "/api/videos":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        state.store.video_page(
                            limit=int(query.get("limit", [DEFAULT_PAGE_SIZE])[0]),
                            offset=int(query.get("offset", [0])[0]),
                            review_status=str(query.get("review_status", ["pending"])[0]),
                        )
                    )
                    return
                if parsed.path == "/api/triage":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        state.store.triage_page(
                            limit=int(query.get("limit", [TRIAGE_DEFAULT_PAGE_SIZE])[0]),
                            offset=int(query.get("offset", [0])[0]),
                            start_date=str(query.get("start_date", [""])[0]),
                            end_date=str(query.get("end_date", [""])[0]),
                            minimum_rating=int(query.get("minimum_rating", [0])[0]),
                            triage_status=str(query.get("triage_status", ["not_triaged"])[0]),
                            minimum_duration=float(query.get("minimum_duration", [0])[0]),
                            maximum_duration=float(query.get("maximum_duration", [0])[0]),
                            search=str(query.get("search", [""])[0]),
                            sort=str(query.get("sort", ["chronological"])[0]),
                            show_private=str(query.get("show_private", ["0"])[0]).lower()
                            in {"1", "true", "yes", "on"},
                        )
                    )
                    return
                if parsed.path.startswith("/api/synopsis/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/api/synopsis/"))
                    self._send_json(state.synopsis_status(video_id))
                    return
                if parsed.path.startswith("/api/triage/") and parsed.path.endswith("/playback"):
                    encoded = parsed.path.removeprefix("/api/triage/").removesuffix("/playback")
                    video_id = urllib.parse.unquote(encoded)
                    query = urllib.parse.parse_qs(parsed.query)
                    state.store.require_triage_eligible(
                        video_id,
                        allow_private=str(query.get("show_private", ["0"])[0]).lower()
                        in {"1", "true", "yes", "on"},
                    )
                    record = state.store.video_record(video_id)
                    if record is None:
                        raise ValueError("Unknown or unavailable video")
                    self._send_json(
                        state.playback_status(
                            video_id, float(record["duration_seconds"] or 0.0)
                        )
                    )
                    return
                if parsed.path.startswith("/api/playback/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/api/playback/"))
                    record = state.store.video_record(video_id)
                    if not record:
                        self._send_error(HTTPStatus.NOT_FOUND, "Video not found")
                        return
                    self._send_json(
                        state.playback_status(video_id, float(record["duration_seconds"] or 0.0))
                    )
                    return
                if parsed.path.startswith("/api/videos/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/api/videos/"))
                    detail = state.store.video_detail(video_id)
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Video not found")
                    else:
                        self._send_json(detail)
                    return
                if parsed.path == "/api/summary":
                    self._send_json(state.render_summary())
                    return
                if parsed.path == "/api/render/errors":
                    self._send_json(state.store.render_error_details())
                    return
                if parsed.path.startswith("/media/frame/"):
                    frame_id = urllib.parse.unquote(parsed.path.removeprefix("/media/frame/"))
                    record = state.store.frame_record(frame_id)
                    if not record:
                        self._send_error(HTTPStatus.NOT_FOUND, "Frame not found")
                        return
                    path = Path(str(record["preview_path"])).resolve()
                    if not path.is_relative_to(state.cache_root):
                        self._send_error(HTTPStatus.FORBIDDEN, "Frame path is outside the review cache")
                        return
                    self._send_file(path, allow_ranges=False, immutable=True)
                    return
                if parsed.path.startswith("/media/synopsis/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/media/synopsis/"))
                    record = state.store.synopsis_record(video_id)
                    if record is None or str(record["synopsis_status"]) != "ready":
                        self._send_error(HTTPStatus.NOT_FOUND, "Synopsis not found")
                        return
                    path = Path(str(record["synopsis_path"])).resolve()
                    if not path.is_relative_to(state.synopsis_root):
                        self._send_error(HTTPStatus.FORBIDDEN, "Synopsis path is outside the derivative root")
                        return
                    self._send_file(path, allow_ranges=True, immutable=True)
                    return
                if parsed.path.startswith("/media/video/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/media/video/"))
                    record = state.store.video_record(video_id)
                    if not record:
                        self._send_error(HTTPStatus.NOT_FOUND, "Video not found")
                        return
                    path = Path(str(record["source_path"]))
                    if not path.is_file():
                        self._send_error(HTTPStatus.NOT_FOUND, "Source video is offline")
                        return
                    if path.stat().st_size != int(record["source_byte_size"]):
                        self._send_error(HTTPStatus.CONFLICT, "Source video changed after review")
                        return
                    path = state.ready_playback_path(video_id)
                    if path is None:
                        self._send_error(
                            HTTPStatus.CONFLICT,
                            "Playback is still being prepared",
                        )
                        return
                    self._send_file(path, allow_ranges=True, immutable=True)
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            except (TypeError, ValueError, OSError) as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            try:
                payload = self._read_json()
                show_private = payload.get("show_private") is True
                if parsed.path == "/api/triage/bulk":
                    identifiers = payload.get("video_ids")
                    if not isinstance(identifiers, list):
                        raise ValueError("video_ids must be a list")
                    self._send_json(
                        state.store.update_triage(
                            identifiers,
                            rating=(int(payload["rating"]) if "rating" in payload else None),
                            triage_status=(
                                str(payload["triage_status"])
                                if "triage_status" in payload
                                else None
                            ),
                            allow_private=show_private,
                        )
                    )
                    return
                if parsed.path.startswith("/api/duplicates/") and parsed.path.endswith("/keep"):
                    encoded = parsed.path.removeprefix("/api/duplicates/").removesuffix("/keep")
                    group_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        state.store.resolve_duplicate_group(
                            group_id,
                            str(payload.get("video_id", "")),
                        )
                    )
                    return
                if parsed.path.startswith("/api/triage/") and parsed.path.endswith("/playback"):
                    encoded = parsed.path.removeprefix("/api/triage/").removesuffix("/playback")
                    video_id = urllib.parse.unquote(encoded)
                    state.store.require_triage_eligible(
                        video_id, allow_private=show_private
                    )
                    record = state.store.video_record(video_id)
                    if record is None:
                        raise ValueError("Unknown or unavailable video")
                    source = Path(str(record["source_path"]))
                    if not source.is_file():
                        self._send_error(HTTPStatus.NOT_FOUND, "Source video is offline")
                        return
                    if source.stat().st_size != int(record["source_byte_size"]):
                        self._send_error(HTTPStatus.CONFLICT, "Source video changed after review")
                        return
                    self._send_json(
                        state.start_playback_preparation(
                            source,
                            video_id,
                            float(record["duration_seconds"] or 0.0),
                        )
                    )
                    return
                if parsed.path.startswith("/api/triage/frames/") and parsed.path.endswith("/main"):
                    encoded = parsed.path.removeprefix("/api/triage/frames/").removesuffix("/main")
                    frame_id = urllib.parse.unquote(encoded)
                    frame = state.store.frame_record(frame_id)
                    if frame is None:
                        raise ValueError("Unknown frame")
                    state.store.require_triage_eligible(
                        str(frame["video_id"]), allow_private=show_private
                    )
                    self._send_json(state.store.set_main_frame(frame_id))
                    return
                if parsed.path.startswith("/api/triage/") and parsed.path.endswith("/metadata"):
                    encoded = parsed.path.removeprefix("/api/triage/").removesuffix("/metadata")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        state.store.save_video_metadata(
                            video_id,
                            str(payload.get("memory_datetime", "")),
                            str(payload.get("title", "")),
                            str(payload.get("description", "")),
                            public_only=not show_private,
                        )
                    )
                    return
                if parsed.path.startswith("/api/triage/") and parsed.path.endswith("/outputs"):
                    encoded = parsed.path.removeprefix("/api/triage/").removesuffix("/outputs")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        state.store.update_output_options(
                            video_id,
                            screenshots=(
                                payload["screenshots"] if "screenshots" in payload else None
                            ),
                            embedded=(
                                payload["embedded"] if "embedded" in payload else None
                            ),
                            summary=(payload["summary"] if "summary" in payload else None),
                            rejected=(
                                payload["rejected"] if "rejected" in payload else None
                            ),
                            allow_private=show_private,
                        )
                    )
                    return
                if parsed.path.startswith("/api/triage/") and parsed.path.endswith("/reject"):
                    encoded = parsed.path.removeprefix("/api/triage/").removesuffix("/reject")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        state.store.reject_video(video_id, public_only=not show_private)
                    )
                    return
                if parsed.path.startswith("/api/triage/") and parsed.path.endswith("/summary"):
                    encoded = parsed.path.removeprefix("/api/triage/").removesuffix("/summary")
                    video_id = urllib.parse.unquote(encoded)
                    state.store.require_triage_eligible(
                        video_id, allow_private=show_private
                    )
                    triage_update = state.store.update_triage(
                        [video_id],
                        triage_status="summary_wanted",
                        allow_private=show_private,
                    )
                    triage = state.store.video_detail(video_id)
                    synopsis = state.start_synopsis(video_id)
                    self._send_json(
                        {
                            "video_id": video_id,
                            "review_status": str(triage["review_status"]),
                            "triage_status": str(triage["triage_status"]),
                            "output_screenshots": bool(triage["output_screenshots"]),
                            "output_embedded": bool(triage["output_embedded"]),
                            "output_summary": bool(triage["output_summary"]),
                            "output_rejected": bool(triage["output_rejected"]),
                            "synopsis_status": str(synopsis["status"]),
                            "synopsis_url": str(synopsis.get("url", "")),
                            "synopsis_error": str(synopsis.get("error", "")),
                            "updated_at": str(triage_update["updated_at"]),
                        }
                    )
                    return
                if parsed.path.startswith("/api/triage/") and parsed.path.endswith("/capture-frame"):
                    encoded = parsed.path.removeprefix("/api/triage/").removesuffix("/capture-frame")
                    video_id = urllib.parse.unquote(encoded)
                    state.store.require_triage_eligible(
                        video_id, allow_private=show_private
                    )
                    self._send_json(
                        capture_video_frame(
                            state.store,
                            state.cache_root,
                            video_id,
                            float(payload.get("time_seconds", -1)),
                        )
                    )
                    return
                if parsed.path.startswith("/api/triage/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/api/triage/"))
                    self._send_json(
                        state.store.update_triage(
                            [video_id],
                            rating=(int(payload["rating"]) if "rating" in payload else None),
                            triage_status=(
                                str(payload["triage_status"])
                                if "triage_status" in payload
                                else None
                            ),
                            allow_private=show_private,
                        )
                    )
                    return
                if parsed.path.startswith("/api/synopsis/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/api/synopsis/"))
                    self._send_json(state.start_synopsis(video_id))
                    return
                if parsed.path.startswith("/api/playback/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/api/playback/"))
                    record = state.store.video_record(video_id)
                    if not record:
                        self._send_error(HTTPStatus.NOT_FOUND, "Video not found")
                        return
                    source = Path(str(record["source_path"]))
                    if not source.is_file():
                        self._send_error(HTTPStatus.NOT_FOUND, "Source video is offline")
                        return
                    if source.stat().st_size != int(record["source_byte_size"]):
                        self._send_error(HTTPStatus.CONFLICT, "Source video changed after review")
                        return
                    self._send_json(
                        state.start_playback_preparation(
                            source,
                            video_id,
                            float(record["duration_seconds"] or 0.0),
                        )
                    )
                    return
                if parsed.path.startswith("/api/frames/") and parsed.path.endswith("/selection"):
                    encoded = parsed.path.removeprefix("/api/frames/").removesuffix("/selection")
                    frame_id = urllib.parse.unquote(encoded)
                    self._send_json(state.store.set_frame_selected(frame_id, bool(payload.get("selected"))))
                    return
                if parsed.path.startswith("/api/frames/") and parsed.path.endswith("/main"):
                    encoded = parsed.path.removeprefix("/api/frames/").removesuffix("/main")
                    frame_id = urllib.parse.unquote(encoded)
                    self._send_json(state.store.set_main_frame(frame_id))
                    return
                if parsed.path.startswith("/api/frames/") and parsed.path.endswith("/privacy"):
                    encoded = parsed.path.removeprefix("/api/frames/").removesuffix("/privacy")
                    frame_id = urllib.parse.unquote(encoded)
                    self._send_json(state.store.set_frame_private(frame_id, payload.get("private")))
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/privacy"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/privacy")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(state.store.set_video_private(video_id, payload.get("private")))
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/open-in-finder"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/open-in-finder")
                    video_id = urllib.parse.unquote(encoded)
                    try:
                        result = reveal_source_in_finder(state.store, video_id)
                    except LookupError as exc:
                        self._send_error(HTTPStatus.NOT_FOUND, str(exc))
                    except FileNotFoundError as exc:
                        self._send_error(HTTPStatus.NOT_FOUND, str(exc))
                    except NotImplementedError as exc:
                        self._send_error(HTTPStatus.NOT_IMPLEMENTED, str(exc))
                    except RuntimeError as exc:
                        self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                    else:
                        self._send_json(result)
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/deselect-all"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/deselect-all")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(state.store.deselect_non_key_frames(video_id))
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/date"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/date")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(state.store.save_memory_date(video_id, str(payload.get("memory_date", ""))))
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/metadata"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/metadata")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        state.store.save_video_metadata(
                            video_id,
                            str(payload.get("memory_datetime", "")),
                            str(payload.get("title", "")),
                            str(payload.get("description", "")),
                        )
                    )
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/outputs"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/outputs")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        state.store.update_output_options(
                            video_id,
                            screenshots=(
                                payload["screenshots"] if "screenshots" in payload else None
                            ),
                            embedded=(
                                payload["embedded"] if "embedded" in payload else None
                            ),
                            summary=(payload["summary"] if "summary" in payload else None),
                            rejected=(
                                payload["rejected"] if "rejected" in payload else None
                            ),
                            allow_private=True,
                        )
                    )
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/accept"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/accept")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        state.store.accept_video(
                            video_id,
                            str(payload.get("memory_datetime", "")),
                            str(payload.get("title", "")),
                            str(payload.get("description", "")),
                        )
                    )
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/reject"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/reject")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(state.store.reject_video(video_id))
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/capture-frame"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/capture-frame")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        capture_video_frame(
                            state.store, state.cache_root, video_id,
                            float(payload.get("time_seconds", -1)),
                        )
                    )
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/rotate"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/rotate")
                    video_id = urllib.parse.unquote(encoded)
                    self._send_json(
                        rotate_video_previews(
                            state.store, state.cache_root, video_id,
                            str(payload.get("direction", "")),
                        )
                    )
                    return
                if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/queue-render"):
                    encoded = parsed.path.removeprefix("/api/videos/").removesuffix("/queue-render")
                    video_id = urllib.parse.unquote(encoded)
                    if state.store.video_record(video_id) is None:
                        raise ValueError("Unknown video")
                    self._send_json({"queued": state.store.queue_selected_frames(video_id)})
                    return
                if parsed.path == "/api/render/start":
                    started = state.start_render()
                    self._send_json({"started": started, **state.render_summary()})
                    return
                if parsed.path == "/api/render/requeue-errors":
                    self._send_json(state.requeue_render_errors())
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length", "0") or 0)
            if length > 1_000_000:
                raise ValueError("Request body is too large")
            if length == 0:
                return {}
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _send_html(self, value: str) -> None:
            payload = inject_activity(value).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-security-policy", "default-src 'self'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, value: dict[str, Any]) -> None:
            payload = json.dumps(value, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            payload = json.dumps({"error": message}).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, path: Path, *, allow_ranges: bool, immutable: bool) -> None:
            if not path.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "Media not found")
                return
            file_stat = path.stat()
            file_size = file_stat.st_size
            byte_range = None
            if allow_ranges:
                try:
                    byte_range = parse_byte_range(self.headers.get("Range", ""), file_size)
                except ValueError as exc:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("content-range", f"bytes */{file_size}")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
            start, end = byte_range if byte_range else (0, file_size - 1)
            length = max(0, end - start + 1)
            self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK)
            mime_type, _ = mimetypes.guess_type(str(path))
            self.send_header("content-type", mime_type or "application/octet-stream")
            self.send_header("content-length", str(length))
            if allow_ranges:
                self.send_header("accept-ranges", "bytes")
            if byte_range:
                self.send_header("content-range", f"bytes {start}-{end}/{file_size}")
            self.send_header("etag", f'"{file_stat.st_mtime_ns:x}-{file_size:x}"')
            self.send_header("cache-control", "private, max-age=31536000, immutable" if immutable else "private, no-store")
            self.end_headers()
            try:
                with path.open("rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(canonical_root: Path, host: str = "127.0.0.1", port: int = 8767) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Video Memory Review must bind to loopback")
    state = VideoMemoryServerState(canonical_root)
    server = ThreadingHTTPServer((host, port), activity_handler(create_handler(state)))
    start_idle_shutdown(server, busy=state.has_background_work)
    print(f"Video Memory Review: http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-root", type=Path, default=Path("Project365Canonical"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare-pilot", help="Prepare the on-device eight-video pilot")
    batch_parser = subparsers.add_parser("prepare-batch", help="Prepare new videos for review")
    batch_target = batch_parser.add_mutually_exclusive_group(required=True)
    batch_target.add_argument("--count", type=int)
    batch_target.add_argument(
        "--all-missing",
        action="store_true",
        help="Prepare every indexed video not already ready with Apple Vision",
    )
    batch_parser.add_argument("--start-date", default="")
    batch_parser.add_argument("--end-date", default="")
    serve_parser = subparsers.add_parser("serve", help="Serve the standalone review page")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8767)
    render_parser = subparsers.add_parser("render", help="Explicitly process queued final JPGs")
    render_parser.add_argument("--limit", type=int, default=100)
    subparsers.add_parser(
        "migrate-output-names",
        help="Move rendered JPG/JSON pairs to human-readable names",
    )
    subparsers.add_parser(
        "prune-duplicates",
        help="Fingerprint duplicate candidates and prune the current review queue",
    )
    args = parser.parse_args()
    if args.command == "prepare-pilot":
        results = prepare_pilot(args.canonical_root)
        print(json.dumps({"prepared": len(results), "frames": sum(item["frames"] for item in results)}, sort_keys=True))
        return 0
    if args.command == "prepare-batch":
        print(json.dumps(prepare_batch(
            args.canonical_root,
            count=None if args.all_missing else args.count,
            start_date=args.start_date,
            end_date=args.end_date,
        ), sort_keys=True))
        return 0
    if args.command == "serve":
        serve(args.canonical_root, args.host, args.port)
        return 0
    if args.command == "render":
        store = VideoMemoryStore(args.canonical_root / "video_memory_review.sqlite")
        summary = process_render_queue(
            store,
            final_renderer(args.canonical_root / "media" / "video_memory_frames"),
            limit=args.limit,
        )
        print(json.dumps(summary, sort_keys=True))
        return 0
    if args.command == "migrate-output-names":
        store = VideoMemoryStore(args.canonical_root / "video_memory_review.sqlite")
        summary = migrate_rendered_output_names(
            store,
            args.canonical_root / "media" / "video_memory_frames",
        )
        print(json.dumps(summary, sort_keys=True))
        return 0
    if args.command == "prune-duplicates":
        print(json.dumps(prune_duplicate_queue(args.canonical_root), sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
