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


FRAME_COUNT = 16
PREVIEW_MAX_PIXELS = 640
FINAL_FRAME_SAFETY_MARGIN_SECONDS = 0.1
DEFAULT_PAGE_SIZE = 1
MAX_PAGE_SIZE = 8
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
PROMPT_VERSION = 1


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


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
                error TEXT NOT NULL DEFAULT '',
                review_version INTEGER NOT NULL DEFAULT 0,
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
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                SET review_version = review_version + 1, updated_at = ?
                WHERE video_id = ?
                """,
                (now, row["video_id"]),
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
                SET review_version = review_version + 1, updated_at = ?
                WHERE video_id = ?
                """,
                (now, video_id),
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
                "UPDATE video_review_items SET review_version = review_version + 1, updated_at = ? WHERE video_id = ?",
                (now, row["video_id"]),
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
                   SUM(CASE WHEN private = 1 THEN 1 ELSE 0 END) AS private_count
            FROM video_review_frames
            WHERE video_id = ?
            """,
            (video_id,),
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
        payload["private"] = private
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
                    updated_at = ?
                WHERE video_id = ?
                """,
                (1 if private else 0, now, video_id),
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
                    updated_at = ?
                WHERE video_id = ?
                """,
                (1 if summary["all_private"] else 0, now, video_id),
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
    ) -> dict[str, Any]:
        normalized_datetime, normalized_title, normalized_description = self._metadata_values(
            memory_datetime, title, description
        )
        memory_date = normalized_datetime[:10]
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE video_review_items
                SET memory_date = ?, memory_datetime = ?, title = ?, description = ?,
                    memory_date_source = 'user_adjusted',
                    review_version = review_version + 1, updated_at = ?
                WHERE video_id = ? AND status = 'ready'
                """,
                (
                    memory_date,
                    normalized_datetime,
                    normalized_title,
                    normalized_description,
                    utc_now(),
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
                "SELECT 1 FROM video_review_items WHERE video_id = ? AND status = 'ready'",
                (video_id,),
            ).fetchone()
            if video is None:
                raise ValueError("Unknown or unavailable video")
            selected_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM video_review_frames WHERE video_id = ? AND selected = 1",
                    (video_id,),
                ).fetchone()[0]
            )
            if selected_count == 0:
                raise ValueError("Select at least one screenshot before accepting")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE video_review_items
                SET review_status = 'accepted', memory_date = ?, memory_datetime = ?,
                    title = ?, description = ?,
                    memory_date_source = CASE WHEN ? = capture_date THEN date_source ELSE 'user_adjusted' END,
                    review_version = review_version + 1, updated_at = ?
                WHERE video_id = ?
                """,
                (
                    memory_date,
                    normalized_datetime,
                    normalized_title,
                    normalized_description,
                    memory_date,
                    now,
                    video_id,
                ),
            )
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
        return {
            "video_id": video_id,
            "review_status": "accepted",
            "memory_date": memory_date,
            "memory_datetime": normalized_datetime,
            "title": normalized_title,
            "description": normalized_description,
            "queued": int(cursor.rowcount),
        }

    def reject_video(self, video_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE video_review_items
                SET review_status = 'rejected', review_version = review_version + 1,
                    updated_at = ?
                WHERE video_id = ? AND status = 'ready'
                """,
                (now, video_id),
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
        return {"video_id": video_id, "review_status": "rejected"}

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
                "UPDATE video_review_items SET review_version = review_version + 1, updated_at = ? WHERE video_id = ?",
                (now, video_id),
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
                SET rotation_override = ?, review_version = review_version + 1, updated_at = ?
                WHERE video_id = ? AND status = 'ready'
                """,
                (rotation, now, video_id),
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

    def claim_render_items(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT f.*, v.source_path, v.source_sha256, v.source_byte_size,
                       v.source_mtime_ns, v.memory_date AS capture_date,
                       v.memory_date_source AS date_source, v.capture_timestamp,
                       v.memory_datetime, v.title, v.description,
                       v.rotation_override
                FROM video_review_frames f
                JOIN video_review_items v ON v.video_id = f.video_id
                WHERE f.render_status = 'queued'
                ORDER BY v.capture_date, f.video_id, f.frame_index
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
            "rotation_override": int(row["rotation_override"]),
            "duration_seconds": row["duration_seconds"],
            "ai_status": row["ai_status"],
            "ai_method": row["ai_method"],
            "ai_model": row["ai_model"],
            "review_version": row["review_version"],
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

    def video_queue_month(
        self,
        review_status: str = "pending",
        selected_month: str = "",
    ) -> dict[str, Any]:
        if review_status not in {"pending", "accepted", "rejected", "all"}:
            raise ValueError("Unknown review status filter")
        selected_month = str(selected_month or "").strip()
        if selected_month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", selected_month):
            raise ValueError("month must be yyyy-mm")
        status_clause = "" if review_status == "all" else " AND v.review_status = ?"
        status_parameters: tuple[Any, ...] = () if review_status == "all" else (review_status,)
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
        if review_status not in {"pending", "accepted", "rejected", "all"}:
            raise ValueError("Unknown review status filter")
        status_clause = "" if review_status == "all" else " AND review_status = ?"
        status_parameters: tuple[Any, ...] = () if review_status == "all" else (review_status,)
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
                SELECT render_status, COUNT(*) count
                FROM video_review_frames
                GROUP BY render_status
                """
            ).fetchall()
            renders = {str(item["render_status"]): int(item["count"]) for item in render_rows}
            return {
                "videos": int(row["videos"] or 0),
                "ready": int(row["ready"] or 0),
                "errors": int(row["errors"] or 0),
                "pending": int(row["pending"] or 0),
                "accepted": int(row["accepted"] or 0),
                "rejected": int(row["rejected"] or 0),
                "queued_renders": renders.get("queued", 0),
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
        rows = connection.execute(
            """
            SELECT path, filename, extension, byte_size, filesystem_mtime_ns,
                   capture_timestamp, capture_timestamp_source, sha256,
                   media_width, media_height, media_duration_seconds
            FROM photo_library_files
            WHERE lower(extension) IN ('.mov', '.mp4', '.m4v')
            ORDER BY path
            """
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


def scrub_optimized_video(source: Path, cache_root: Path, video_id: str) -> Path:
    """Lazily build a compact, short-GOP MP4 for responsive browser seeking."""
    destination = Path(cache_root) / "playback-scrub-v1" / f"{video_id}.mp4"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.partial-{uuid.uuid4().hex}.mp4")
    completed = _run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
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
        ],
        timeout=1200,
    )
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        raise ValueError((completed.stderr or "Could not prepare scrub-optimized video").strip())
    temporary.replace(destination)
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
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{time_seconds:.3f}",
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
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        raise ValueError((completed.stderr or "Could not extract video frame").strip())


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
    with store._connect() as connection:
        duplicate = connection.execute(
            "SELECT 1 FROM video_review_frames WHERE video_id = ? AND ABS(time_seconds - ?) < 0.05",
            (video_id, requested_time),
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
        extract_frame(
            Path(str(record["source_path"])),
            temporary,
            min(requested_time, max(0.0, duration - FINAL_FRAME_SAFETY_MARGIN_SECONDS)),
            max_pixels=PREVIEW_MAX_PIXELS,
            quality=5,
            rotation=int(record["rotation_override"]),
        )
        score = _vision_scores([temporary])[0]
        temporary.replace(preview)
        return store.add_captured_frame(
            video_id,
            time_seconds=requested_time,
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
        times = frame_times(duration)
        preview_paths: list[Path] = []
        frames = []
        for index, time_seconds in enumerate(times, start=1):
            preview = temporary / f"frame-{index:02d}.jpg"
            extract_frame(
                source,
                preview,
                time_seconds,
                max_pixels=PREVIEW_MAX_PIXELS,
                quality=5,
                rotation=rotation,
            )
            preview_paths.append(preview)
        scores = _vision_scores(preview_paths)
        for index, (time_seconds, preview, score) in enumerate(zip(times, preview_paths, scores, strict=True), start=1):
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
            "frame_times": times,
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
        return {
            "video_id": video_id,
            "frames": len(frames),
            "selected": len(selection["selected_frame_ids"]),
            "ai_method": method,
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


def prepare_batch(
    canonical_root: Path,
    *,
    count: int,
    index_paths: list[Path] | None = None,
) -> dict[str, int]:
    canonical_root = Path(canonical_root)
    index_paths = index_paths or [
        canonical_root / "video_library_legacy_index.sqlite",
        canonical_root / "photo_library_reexport_index.sqlite",
    ]
    store = VideoMemoryStore(canonical_root / "video_memory_review.sqlite")
    indexed = select_pilot_videos(index_paths, count=1_000_000)
    completed_ids = store.completed_ai_video_ids()
    retry_ids = store.incomplete_ai_video_ids()
    candidates = select_batch_candidates(
        indexed,
        excluded_video_ids=completed_ids,
        count=count,
    )
    candidates = (
        [item for item in candidates if str(item["video_id"]) in retry_ids]
        + [item for item in candidates if str(item["video_id"]) not in retry_ids]
    )
    cache_root = canonical_root / "cache" / "video_memory_review"
    prepared = 0
    failed = 0
    attempted = 0
    for video in candidates:
        if prepared >= count:
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
                f"failed attempt={attempted} prepared={prepared}/{count} "
                f"error={type(exc).__name__}",
                flush=True,
            )
            continue
        prepared += 1
        print(
            f"prepared {prepared}/{count} frames={result['frames']} "
            f"selected={result['selected']} ai={result['ai_method']}",
            flush=True,
        )
    return {
        "requested": count,
        "prepared": prepared,
        "failed": failed,
        "attempted": attempted,
    }


def final_renderer(output_root: Path) -> Callable[[dict[str, Any]], Path]:
    output_root = Path(output_root)

    def render(item: dict[str, Any]) -> Path:
        source = Path(str(item["source_path"]))
        if not source.is_file():
            raise ValueError("Source video is offline")
        stat = source.stat()
        if int(item["source_byte_size"]) != stat.st_size:
            raise ValueError("Source video byte size changed after review")
        video_id = str(item["video_id"])
        frame_index = int(item["frame_index"])
        time_ms = int(round(float(item["time_seconds"]) * 1000))
        rotation = int(item.get("rotation_override", 0)) % 360
        folder = output_root / video_id[:2] / video_id
        folder.mkdir(parents=True, exist_ok=True)
        role = "main" if bool(item["is_main"]) else "linked"
        output = folder / f"frame-{frame_index:02d}-{time_ms:09d}ms-rot{rotation:03d}-{role}.jpg"
        sidecar = output.with_suffix(".json")
        if output.is_file() and output.stat().st_size > 0 and sidecar.is_file():
            return output
        temporary = output.with_name(f".{output.stem}.partial-{uuid.uuid4().hex}.jpg")
        extract_frame(
            source, temporary, float(item["time_seconds"]),
            max_pixels=None, quality=2, rotation=rotation,
        )
        temporary.replace(output)
        metadata = {
            "schema_version": 1,
            "status": "rendered_not_canonical",
            "video_id": video_id,
            "source_sha256": item["source_sha256"],
            "source_path": str(source),
            "frame_index": frame_index,
            "time_seconds": float(item["time_seconds"]),
            "is_main": bool(item["is_main"]),
            "private": bool(item["private"]),
            "user_captured": bool(item.get("user_captured", 0)),
            "rotation_override": rotation,
            "capture_date": item["capture_date"],
            "date_source": item["date_source"],
            "capture_timestamp": item["capture_timestamp"],
            "memory_datetime": item.get("memory_datetime", ""),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
        }
        sidecar_temporary = sidecar.with_name(f".{sidecar.name}.partial-{uuid.uuid4().hex}")
        sidecar_temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True))
        sidecar_temporary.replace(sidecar)
        return output

    return render


def process_render_queue(
    store: VideoMemoryStore,
    renderer: Callable[[dict[str, Any]], Path],
    limit: int = 100,
) -> dict[str, int]:
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
 function step(delta){return selectExact(selectedTime+Number(delta))}
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
"""


# The queue review is intentionally a single-item surface.  Keeping the document
# embedded lets the standalone loopback server remain dependency-free.
REVIEW_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project365 Video Memory Review</title>
<style>
:root{color-scheme:light;--bg:#f6f7f9;--panel:#ffffff;--ink:#202124;--muted:#667085;--line:#d9dee5;--line-strong:#c5ccd6;--accent:#17695d;--accent-soft:#e4f2ef;--hover:#f0f4f7;--gold:#f0a000;--reject:#9f2d2d;--purple:#8f3f7d}
*{box-sizing:border-box}html,body{height:100%;overflow:hidden}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:grid;grid-template-rows:auto minmax(0,1fr);-webkit-font-smoothing:antialiased}button,input,select,textarea{font:inherit}button{color:var(--ink);background:#fff;border:1px solid var(--line-strong);border-radius:6px;padding:.48rem .72rem;cursor:pointer}button:hover{background:var(--hover)}button:disabled{opacity:.45;cursor:default}.primary{color:#fff;background:var(--accent);border-color:var(--accent)}.primary:hover{background:#10594f}.danger{color:var(--reject);background:#fff;border-color:#dca5a5}.icon{font-size:0;line-height:1;padding:.42rem .58rem}.icon:before{font-size:1.35rem}.icon[data-rotate="left"]:before{content:"↺"}.icon[data-rotate="right"]:before{content:"↻"}
header{display:flex;align-items:center;gap:.75rem;padding:.65rem 1rem;background:var(--panel);border-bottom:1px solid var(--line)}header a{color:var(--ink);text-decoration:none;border:1px solid var(--line-strong);border-radius:6px;padding:.42rem .62rem}h1{font-size:1.08rem;margin:0}.spacer{flex:1}#summary{color:var(--muted);white-space:nowrap}
.queue-sidebar{min-height:0;display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}.queue-controls{display:grid;gap:7px;padding:10px;border-bottom:1px solid var(--line)}.queue-controls label{color:var(--muted);font-size:.8rem;font-weight:700}.queue-controls select{width:100%;margin-top:3px;color:var(--ink);background:#fff;border:1px solid var(--line-strong);border-radius:6px;padding:.45rem}.queue-summary{color:var(--muted);font-size:.78rem}.queue-list{min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding:7px}.queue-list details{margin:0}.queue-list summary{cursor:pointer;color:var(--muted);font-size:.78rem;font-weight:750;padding:5px 2px;list-style-position:inside}.queue-list .queue-month{margin-left:5px}.queue-list .queue-month[open]>summary{color:var(--accent)}.month-count{float:right;color:var(--muted);font-weight:600;font-variant-numeric:tabular-nums}.queue-loading{padding:5px 18px;color:var(--muted);font-size:.74rem}.queue-item{width:100%;display:grid;grid-template-columns:68px minmax(0,1fr);gap:7px;align-items:center;margin:4px 0;padding:5px;text-align:left;border-color:transparent;background:transparent}.queue-item:hover{background:var(--hover)}.queue-item.active{background:var(--accent-soft);border-color:var(--accent)}.queue-thumb{display:block;width:68px;height:48px;object-fit:cover;background:#222;border-radius:4px}.queue-thumb.empty{height:48px}.queue-copy{min-width:0}.queue-date,.queue-title,.queue-meta{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.queue-date{font-size:.76rem;font-variant-numeric:tabular-nums}.queue-title{font-size:.8rem;font-weight:700}.queue-meta{font-size:.69rem;color:var(--muted)}
main{min-height:0;overflow:hidden;padding:12px;max-width:2100px;width:100%;margin:0 auto;display:grid;grid-template-columns:260px minmax(0,1fr);gap:12px}#review{height:100%;min-height:0;min-width:0}.empty{height:100%;display:grid;place-items:center;color:var(--muted)}.review{height:100%;min-height:0;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr);box-shadow:0 1px 2px rgba(16,24,40,.04)}.review-head{display:flex;align-items:center;gap:.8rem;padding:.65rem .85rem;border-bottom:1px solid var(--line);flex-wrap:wrap}.review-head h2{font-size:1rem;margin:0}.meta{color:var(--muted);font-size:.85rem}.review-actions{margin-left:auto;display:flex;gap:.4rem;align-items:center;flex-wrap:wrap}.privacy-all.private{color:#fff;background:var(--purple);border-color:var(--purple)}.privacy-all.mixed{color:var(--purple);border-color:var(--purple)}
.workspace{min-height:0;overflow:hidden;display:grid;grid-template-columns:minmax(390px,520px) minmax(520px,1fr);gap:0}.viewer{min-height:0;overflow-x:hidden;overflow-y:auto;scrollbar-gutter:stable;padding:12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:8px;background:var(--panel)}.section-title{display:flex;align-items:center;justify-content:space-between;gap:.5rem}.section-title h3{font-size:.9rem;margin:0}.legend{color:var(--muted);font-size:.78rem}
.player-stage{position:relative;height:clamp(250px,42vh,500px);min-height:220px;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#222;border:1px solid var(--line);border-radius:8px}.player-stage video{display:block;width:100%;height:100%;object-fit:contain;transform-origin:center}.player-seek{display:grid;gap:3px}.player-time{color:var(--muted);font-variant-numeric:tabular-nums;text-align:center;font-size:.82rem}.player-timeline{width:100%;margin:0;accent-color:var(--accent);cursor:ew-resize}.player-tools{display:grid;justify-items:center;gap:5px}.player-navigation,.capture-tools{display:flex;align-items:center;justify-content:center;gap:5px;flex-wrap:wrap}.player-tools button{padding:.35rem .52rem}#playPauseButton{min-width:96px}.capture-status,.status{color:var(--muted);font-size:.82rem}
.key-summary{display:grid;grid-template-columns:160px minmax(0,1fr);gap:10px;margin-top:2px;padding:9px;background:#f8fafc;border:1px solid var(--line);border-radius:8px;min-height:0}.key-column{display:grid;gap:5px;align-content:start}.key-label{font-size:.82rem;font-weight:700}.key-view{height:112px;width:100%;padding:0;overflow:hidden;display:flex;align-items:center;justify-content:center;background:#222;border:1px solid var(--line);border-radius:6px;color:#fff}.key-view img{display:block;width:100%;height:100%;object-fit:contain}.metadata-controls{display:grid;grid-template-columns:minmax(0,1fr) minmax(180px,.55fr);gap:7px;align-content:start}.metadata-controls label{display:grid;gap:3px;color:var(--muted);font-size:.76rem}.metadata-controls .description-field{grid-column:1/-1}.metadata-controls input,.metadata-controls textarea{width:100%;color:var(--ink);background:#fff;border:1px solid var(--line-strong);border-radius:6px;padding:7px 8px}.metadata-controls textarea{height:54px;resize:vertical}.metadata-actions{grid-column:1/-1;display:flex;align-items:center;gap:7px}.metadata-actions .status{flex:1}.selected-strip-wrap{grid-column:1/-1;display:grid;gap:5px}.selected-strip-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.selected-strip-head button{padding:.25rem .5rem;font-size:.74rem}.selected-strip{display:grid;grid-template-columns:repeat(auto-fill,80px);grid-auto-rows:80px;gap:8px;height:168px;overflow-x:hidden;overflow-y:auto;align-content:start;scrollbar-gutter:stable}.selected-mini{width:80px;height:80px;padding:0;border-radius:6px;overflow:hidden;background:#222}.selected-mini.main{outline:4px solid var(--gold);outline-offset:-4px}.selected-mini img{display:block;width:100%;height:100%;object-fit:cover}
.candidates{min-height:0;min-width:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;-webkit-overflow-scrolling:touch;touch-action:pan-y;padding:12px;background:var(--bg)}.candidate-heading{position:sticky;top:-12px;z-index:5;margin:-12px -12px 10px;padding:12px;background:#f6f7f9ed;border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}.candidate-heading .legend:after{content:" · red outer = captured";color:#a51f1f;font-weight:700}.frame-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:34px 24px;align-content:start;padding:20px}.frame{position:relative;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:visible;min-width:0;box-shadow:0 1px 2px rgba(16,24,40,.04)}.frame.ai-selected{box-shadow:0 0 0 4px #1769a4}.frame.ai-selected.selected,.frame.ai-selected.main{box-shadow:0 0 0 15px #1769a4}.frame.user-captured:before{content:"";position:absolute;inset:-4px;border:4px solid #c62828;border-radius:10px;pointer-events:none;z-index:3}.frame.user-captured.selected:before,.frame.user-captured.main:before{inset:-19px}.frame.selected{outline:8px solid var(--accent);outline-offset:3px}.frame.selected .frame-tools{background:#9fe3bd;border-top:2px solid #2f8f65}.frame.main{outline:8px solid var(--gold);outline-offset:3px}.frame.main .frame-tools{background:#fff0c7;border-top-color:#e4bc58}.frame-image{display:block;width:100%;height:210px;padding:0;border:0;border-radius:7px 7px 0 0;overflow:hidden;background:#222}.frame-image img{width:100%;height:100%;object-fit:contain;display:block}.badge{position:absolute;top:.35rem;border-radius:999px;padding:.16rem .38rem;font-size:.68rem;font-weight:750;pointer-events:none;color:#fff;z-index:4}.ai-badge{left:.35rem;background:#1769a4}.key-badge{right:.35rem;background:var(--gold);color:#2b1b00;font-size:.82rem;padding:.24rem .5rem;font-weight:850;border:1px solid #fff9;box-shadow:0 1px 4px #0005}.frame-tools{display:flex;align-items:center;gap:.28rem;padding:.3rem .38rem;border-radius:0 0 7px 7px}.frame-tools button{font-size:.72rem;line-height:1;padding:.22rem .32rem}.frame-time{margin-right:auto}.privacy.private{color:#fff;background:var(--purple);border-color:var(--purple)}
.zoom-overlay[hidden]{display:none}.zoom-overlay{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:3vh 3vw;background:transparent;pointer-events:none}.zoom-overlay img{display:block;max-width:min(82vw,1100px);max-height:82vh;object-fit:contain;background:#111;border:2px solid #fff;border-radius:8px;box-shadow:0 18px 60px rgba(0,0,0,.45)}
@media(max-width:1150px){main{grid-template-columns:220px minmax(0,1fr)}.workspace{grid-template-columns:minmax(330px,44%) minmax(430px,56%)}.review-actions{margin-left:0}.player-stage{height:38vh}.key-summary{grid-template-columns:130px minmax(0,1fr)}.key-view{height:96px}.metadata-controls{grid-template-columns:1fr}}@media(max-height:760px){header{padding-top:.42rem;padding-bottom:.42rem}.review-head{padding-top:.45rem;padding-bottom:.45rem}.player-stage{height:34vh;min-height:190px}.key-view{height:88px}}
</style></head><body>
<header><a href="http://127.0.0.1:8766/">Control Panel</a><h1>Video Memory Review</h1><span id="summary">Loading…</span><span class="spacer"></span><button id="renderButton" type="button">Render queued JPGs</button></header>
<main><aside class="queue-sidebar" aria-label="Video review queue"><div class="queue-controls"><label for="queueFilter">Video list<select id="queueFilter"><option value="pending">Queued</option><option value="accepted">Accepted</option><option value="all">All</option></select></label><div id="queueSummary" class="queue-summary">Loading…</div></div><div id="videoQueue" class="queue-list"></div></aside><div id="review"></div></main><div id="frameZoom" class="zoom-overlay" hidden aria-hidden="true"><img id="zoomPreview" alt="Enlarged screenshot"></div>
<script>
""" + VIDEO_SCRUB_CONTROLLER_JS + r"""
let reviewStatus='pending';let queueMonths=[];let queueTotal=0;let queueMonthCache=new Map();let openQueueMonths=new Set();let currentVideo=null;let selectedPlayerTime=0;let scrubController=null;let timelineScrubbing=false;let playbackGeneration=0;let videoLoadSerial=0;let zoomTimer=null;const videoScrubDiagnostics=[];const PLAYBACK_SESSION_TOKEN=Date.now().toString(36);const ZOOM_HOVER_DELAY_MS=750;const AUTO_OPEN_MONTHS=4;const LAST_VIDEO_KEY='project365VideoMemoryLastVideoId';window.__videoScrubDiagnostics=videoScrubDiagnostics;window.videoScrubSnapshot=function(){return scrubController?.snapshot()||null};
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
function videoHtml(video){const privacyClass=video.privacy_state==='all'?'private':video.privacy_state==='mixed'?'mixed':'';const privacyLabel=video.privacy_state==='all'?'P All ✓':video.privacy_state==='mixed'?'P Mixed':'P All';return '<article class="review" data-video-id="'+escapeHtml(video.video_id)+'"><div class="review-head"><div><h2>'+escapeHtml(video.memory_datetime)+' · '+formatTime(video.duration_seconds)+'</h2><div class="meta">'+escapeHtml(video.review_status)+' · AI: '+escapeHtml(video.ai_method||video.ai_status)+'</div></div><div class="review-actions"><button data-open-in-finder="1" type="button">Open in Finder</button><button data-rotate="left" class="icon" type="button" title="Rotate left" aria-label="Rotate video left">↶</button><button data-rotate="right" class="icon" type="button" title="Rotate right" aria-label="Rotate video right">↷</button><button data-video-private="1" class="privacy-all '+privacyClass+'" type="button" aria-pressed="'+(video.all_private?'true':'false')+'">'+privacyLabel+'</button><button data-reject="1" class="danger" type="button">Reject &amp; Next</button><button data-accept="1" class="primary" type="button">Accept &amp; Next</button></div></div><div class="workspace"><section class="viewer"><div class="section-title"><h3>Source video</h3><span class="legend">Timeline previews the video · capture uses the selected time</span></div><div class="player-stage"><video id="sharedPlayer" preload="auto" playsinline src="'+escapeHtml(video.playback_url)+'"></video></div><div class="player-seek"><span id="playerTime" class="player-time">Selected: 0:00.000</span><span id="playerSeekStatus" class="capture-status" aria-live="polite"></span><input id="playerTimeline" type="range" class="player-timeline" min="0" max="'+video.duration_seconds+'" step="0.05" value="0" aria-label="Selected timestamp"></div><div class="player-tools"><div class="player-navigation"><button data-adjust="-1" type="button">−1s</button><button data-adjust="-0.1" type="button">−0.1s</button><button id="playPauseButton" data-play-pause="1" type="button" aria-pressed="false">Play</button><button data-adjust="0.1" type="button">+0.1s</button><button data-adjust="1" type="button">+1s</button></div><div class="capture-tools"><button data-capture="1" class="primary" type="button" title="Capture selected timestamp (.)">Capture selected frame</button><button data-make-key="1" type="button">Make key</button><span id="captureStatus" class="capture-status"></span></div></div><div class="key-summary"><div class="key-column"><span class="key-label">Current key frame</span><div id="keyFramePreview">'+keyFrameHtml(video.frames.find(function(frame){return frame.is_main}))+'</div></div><div class="metadata-controls"><label>Title<input id="memoryTitle" maxlength="500" value="'+escapeHtml(video.title)+'"></label><label>Memory Date<input id="memoryDateTime" type="text" maxlength="17" placeholder="yyyy-mm-dd hhmmss" pattern="\\d{4}-\\d{2}-\\d{2} \\d{6}" value="'+escapeHtml(video.memory_datetime)+'"></label><label class="description-field">Description<textarea id="memoryDescription" maxlength="10000">'+escapeHtml(video.description)+'</textarea></label><div class="metadata-actions"><span class="status"></span></div></div><div class="selected-strip-wrap"><div class="selected-strip-head"><span class="key-label">Selected frames</span><button data-deselect-all="1" type="button">Deselect all</button></div><div id="selectedFrameStrip" class="selected-strip">'+selectedFramesHtml(video.frames)+'</div></div></div></section><section class="candidates"><div class="candidate-heading section-title"><h3>Screenshot choices</h3><span class="legend">Click image to keep · blue = AI · green = kept · gold = key · P = private</span></div><div class="frame-grid">'+video.frames.map(frameHtml).join('')+'</div></section></div></article>'}
function playerElement(){return document.getElementById('sharedPlayer')}function candidatePane(){return reviewElement.querySelector('.candidates')}
function updatePlayPauseButton(){const player=playerElement();const button=document.getElementById('playPauseButton');if(!player||!button)return;const playing=scrubController?.snapshot().phase==='playing'&&!player.paused&&!player.ended;button.textContent=playing?'Pause':'Play';button.setAttribute('aria-pressed',playing?'true':'false')}
function updateSelectedTime(value){const player=playerElement();const label=document.getElementById('playerTime');const timeline=document.getElementById('playerTimeline');if(!player)return;selectedPlayerTime=clampPlayerTime(value);if(label)label.textContent='Selected: '+formatTime(selectedPlayerTime,true)+' / '+formatTime(player.duration||currentVideo?.duration_seconds||0,true);if(timeline&&!timeline.matches(':active'))timeline.value=String(selectedPlayerTime)}
function clampPlayerTime(time){const maximum=Number(playerElement()?.duration)||Number(currentVideo?.duration_seconds)||0;return Math.max(0,Math.min(maximum,Number(time)||0))}
function playerSeekStatus(value){const node=document.getElementById('playerSeekStatus');if(node)node.textContent=value}
function selectPlayerTime(time){scrubController?.selectExact(time)}
function bindPlayer(startTime=0){const player=playerElement();const timeline=document.getElementById('playerTimeline');if(!player)return;if(scrubController)scrubController.destroy();scrubController=null;timelineScrubbing=false;selectedPlayerTime=clampPlayerTime(startTime);player.style.transform='rotate('+(currentVideo?.rotation_override||0)+'deg)';player.addEventListener('timeupdate',function(){if(scrubController?.snapshot().phase!=='playing'||player.seeking)return;updateSelectedTime(player.currentTime)});player.addEventListener('play',updatePlayPauseButton);player.addEventListener('playing',function(){playerSeekStatus('');updatePlayPauseButton()});player.addEventListener('pause',updatePlayPauseButton);player.addEventListener('ended',function(){updateSelectedTime(player.currentTime);updatePlayPauseButton()});player.addEventListener('error',function(){playerSeekStatus('Video playback failed')});const initialize=function(){player.pause();if(timeline)timeline.max=String(player.duration||currentVideo?.duration_seconds||0);scrubController=createVideoScrubController(player,{duration:function(){return Number(currentVideo?.duration_seconds)||0},onSelectedTime:updateSelectedTime,onStateChange:updatePlayPauseButton,onStatus:playerSeekStatus,onDiagnostic:function(item){videoScrubDiagnostics.push(item);if(videoScrubDiagnostics.length>500)videoScrubDiagnostics.shift()}});scrubController.reset(selectedPlayerTime);if(Math.abs(Number(player.currentTime)-selectedPlayerTime)>=0.0005)scrubController.selectExact(selectedPlayerTime);updatePlayPauseButton()};if(player.readyState>=1)initialize();else player.addEventListener('loadedmetadata',initialize,{once:true})}
function adjustPlayer(delta){scrubController?.step(Number(delta))}
function togglePlayback(){scrubController?.togglePlayback()}
async function loadSummary(){const summary=await jsonRequest('/api/summary');document.getElementById('summary').textContent=summary.pending+' pending · '+summary.accepted+' accepted · '+summary.rejected+' rejected · '+summary.queued_renders+' JPGs queued'}
async function loadVideo(videoId,options={}){const serial=++videoLoadSerial;if(scrubController)scrubController.destroy();scrubController=null;timelineScrubbing=false;if(!videoId){currentVideo=null;reviewElement.innerHTML='<div class="empty">No videos in this list.</div>';renderQueue();return}reviewElement.innerHTML='<div class="empty">Loading video…</div>';const video=await jsonRequest('/api/videos/'+encodeURIComponent(videoId));if(serial!==videoLoadSerial)return;video.playback_url=playerVideoUrl(video.video_url);currentVideo=video;localStorage.setItem(LAST_VIDEO_KEY,currentVideo.video_id);reviewElement.innerHTML=videoHtml(currentVideo);renderQueue();const startTime=Object.prototype.hasOwnProperty.call(options,'playerTime')?options.playerTime:0;bindPlayer(startTime);requestAnimationFrame(function(){const pane=candidatePane();if(pane)pane.scrollTop=Number(options.candidateScroll)||0;queueElement.querySelector('.queue-item.active')?.scrollIntoView({block:'nearest'})})}
async function fetchQueueMonth(month=''){const payload=await jsonRequest('/api/video-queue?review_status='+encodeURIComponent(reviewStatus)+'&month='+encodeURIComponent(month));queueMonths=payload.month_counts||[];queueTotal=payload.total_count||0;if(payload.selected_month)queueMonthCache.set(payload.selected_month,payload.entries||[]);return payload}
async function openUpcomingMonths(startMonth){const startIndex=Math.max(0,queueMonths.findIndex(function(item){return item.month===startMonth}));const upcoming=queueMonths.slice(startIndex,startIndex+AUTO_OPEN_MONTHS).map(function(item){return item.month});upcoming.forEach(function(month){openQueueMonths.add(month)});await Promise.all(upcoming.filter(function(month){return !queueMonthCache.has(month)}).map(function(month){return fetchQueueMonth(month)}));renderQueue()}
function currentMonthEntries(){const month=currentVideo?.memory_datetime?.slice(0,7)||'';return queueMonthCache.get(month)||[]}
async function loadQueue(options={}){if(options.reset!==false){queueMonthCache=new Map();openQueueMonths=new Set()}const payload=await fetchQueueMonth(options.month||'');const selectedMonth=payload.selected_month;await openUpcomingMonths(selectedMonth);const preferred=options.preferredVideoId||localStorage.getItem(LAST_VIDEO_KEY)||'';const loadedEntries=[...queueMonthCache.values()].flat();let entry=loadedEntries.find(function(item){return item.video_id===preferred});const primary=queueMonthCache.get(selectedMonth)||[];if(!entry&&primary.length)entry=primary[Math.max(0,Math.min(primary.length-1,Number(options.preferredIndex)||0))];renderQueue();await loadVideo(entry?.video_id||'');await loadSummary()}
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
async function advanceAfterDecision(newStatus){const month=currentVideo?.memory_datetime.slice(0,7)||'';const entries=queueMonthCache.get(month)||[];const currentIndex=Math.max(0,entries.findIndex(function(entry){return entry.video_id===currentVideo?.video_id}));const preferred=reviewStatus==='all'||reviewStatus===newStatus?currentVideo?.video_id:'';return loadQueue({month:month,preferredVideoId:preferred,preferredIndex:currentIndex})}
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
reviewElement.addEventListener('pointerup',finishTimelineScrub);reviewElement.addEventListener('pointercancel',finishTimelineScrub);reviewElement.addEventListener('change',finishTimelineScrub)
queueElement.addEventListener('click',function(event){const item=event.target.closest('[data-video-id]');if(!item)return;const entry=[...queueMonthCache.values()].flat().find(function(value){return value.video_id===item.dataset.videoId});const month=entry?.memory_datetime.slice(0,7)||'';openUpcomingMonths(month).then(function(){return loadVideo(item.dataset.videoId)}).catch(function(error){reviewElement.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'})});queueElement.addEventListener('toggle',function(event){const details=event.target.closest('.queue-month');if(!details)return;const month=details.dataset.month;if(!details.open){openQueueMonths.delete(month);return}const wasOpen=openQueueMonths.has(month);openQueueMonths.add(month);if(!wasOpen){openUpcomingMonths(month).catch(function(error){document.getElementById('queueSummary').textContent=error.message});return}if(queueMonthCache.has(month))return;fetchQueueMonth(month).then(renderQueue).catch(function(error){queueMonthCache.set(month,[]);renderQueue();document.getElementById('queueSummary').textContent=error.message})},true);document.getElementById('queueFilter').addEventListener('change',function(event){reviewStatus=event.target.value;loadQueue({month:''}).catch(function(error){queueElement.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'})});
document.getElementById('renderButton').addEventListener('click',async function(event){event.target.disabled=true;try{await jsonRequest('/api/render/start',{method:'POST',body:'{}'});await loadSummary()}catch(error){document.getElementById('summary').textContent=error.message}finally{event.target.disabled=false}});document.addEventListener('keydown',function(event){if(event.metaKey||event.ctrlKey||event.altKey)return;const editable=event.target?.matches('textarea,select,[contenteditable="true"],input:not(#playerTimeline)');if(editable)return;if((event.key===' '||event.code==='Space')&&!event.repeat){event.preventDefault();togglePlayback();return}if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();if(event.repeat)return;adjustPlayer((event.key==='ArrowLeft'?-1:1)*(event.shiftKey?1:0.1));return}if(event.key==='.'&&!event.repeat){const capture=reviewElement.querySelector('[data-capture]');if(capture&&!capture.disabled){event.preventDefault();capture.click()}}});setInterval(loadSummary,3000);loadQueue({month:''}).catch(function(error){reviewElement.innerHTML='<div class="empty">'+escapeHtml(error.message)+'</div>'});
</script></body></html>"""

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
        self._playback_lock = threading.Lock()

    def playback_path(self, source: Path, video_id: str) -> Path:
        with self._playback_lock:
            try:
                return scrub_optimized_video(source, self.cache_root, video_id)
            except ValueError:
                return source

    def start_render(self) -> bool:
        with self._render_lock:
            if self._render_thread and self._render_thread.is_alive():
                return False
            thread = threading.Thread(target=self._render_worker, daemon=True, name="video-memory-render")
            self._render_thread = thread
            thread.start()
            return True

    def _render_worker(self) -> None:
        renderer = final_renderer(self.output_root)
        while self.store.queued_render_count():
            result = process_render_queue(self.store, renderer, limit=8)
            if not result["processed"]:
                break


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
                if parsed.path.startswith("/api/videos/"):
                    video_id = urllib.parse.unquote(parsed.path.removeprefix("/api/videos/"))
                    detail = state.store.video_detail(video_id)
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Video not found")
                    else:
                        self._send_json(detail)
                    return
                if parsed.path == "/api/summary":
                    self._send_json(state.store.summary())
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
                    path = state.playback_path(path, video_id)
                    self._send_file(path, allow_ranges=True, immutable=True)
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            except (TypeError, ValueError, OSError) as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            try:
                payload = self._read_json()
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
                    self._send_json({"started": started, **state.store.summary()})
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
            payload = value.encode("utf-8")
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
    server = ThreadingHTTPServer((host, port), create_handler(state))
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
    batch_parser.add_argument("--count", type=int, required=True)
    serve_parser = subparsers.add_parser("serve", help="Serve the standalone review page")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8767)
    render_parser = subparsers.add_parser("render", help="Explicitly process queued final JPGs")
    render_parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.command == "prepare-pilot":
        results = prepare_pilot(args.canonical_root)
        print(json.dumps({"prepared": len(results), "frames": sum(item["frames"] for item in results)}, sort_keys=True))
        return 0
    if args.command == "prepare-batch":
        print(json.dumps(prepare_batch(args.canonical_root, count=args.count), sort_keys=True))
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
