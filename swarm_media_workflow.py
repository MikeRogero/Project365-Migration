#!/usr/bin/env python3
"""Persistent local review for Swarm photo order, crops, and digiKam People."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import secrets
import shutil
import sqlite3
import tempfile
import threading
import urllib.parse
from collections import Counter
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from project365_digikam_people_importer import _extract_people_from_xmp
from project365_media_derivatives import _convert_image_atomically
from project365_original_picker import _normalized_review_crop
from swarm_dayone_importer import Entry, Photo, _build_entries, _export_root


SCHEMA_VERSION = "4"
MAX_BODY_BYTES = 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _photo_key(entry_key: str, photo_source_id: str) -> str:
    return f"{entry_key}\0{photo_source_id}"


class SwarmMediaStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            self._create_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS swarm_review_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS swarm_entries (
                entry_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                local_date TEXT NOT NULL,
                order_reviewed INTEGER NOT NULL DEFAULT 0 CHECK(order_reviewed IN (0, 1)),
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS swarm_photos (
                photo_key TEXT PRIMARY KEY,
                entry_key TEXT NOT NULL,
                photo_source_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                default_position INTEGER NOT NULL CHECK(default_position >= 0),
                custom_position INTEGER CHECK(custom_position IS NULL OR custom_position >= 0),
                crop_queued INTEGER NOT NULL DEFAULT 0 CHECK(crop_queued IN (0, 1)),
                crop_json TEXT NOT NULL DEFAULT '',
                crop_derivative_path TEXT NOT NULL DEFAULT '',
                crop_source_sha256 TEXT NOT NULL DEFAULT '',
                digikam_working_path TEXT NOT NULL DEFAULT '',
                digikam_source_sha256 TEXT NOT NULL DEFAULT '',
                rejected INTEGER NOT NULL DEFAULT 0 CHECK(rejected IN (0, 1)),
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                last_seen_at TEXT NOT NULL,
                UNIQUE(entry_key, photo_source_id),
                FOREIGN KEY(entry_key) REFERENCES swarm_entries(entry_key)
            );
            CREATE TABLE IF NOT EXISTS swarm_photo_people (
                photo_key TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY(photo_key, canonical_name),
                FOREIGN KEY(photo_key) REFERENCES swarm_photos(photo_key) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS swarm_order_staging (
                entry_key TEXT PRIMARY KEY,
                photo_order_json TEXT NOT NULL,
                staged_at TEXT NOT NULL,
                FOREIGN KEY(entry_key) REFERENCES swarm_entries(entry_key) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_swarm_photos_entry_active
            ON swarm_photos(entry_key, active, custom_position, default_position);
            """
        )
        existing = connection.execute(
            "SELECT value FROM swarm_review_meta WHERE key = 'schema_version'"
        ).fetchone()
        if existing is not None and str(existing[0]) not in {"1", "2", "3", SCHEMA_VERSION}:
            raise ValueError("Unsupported Swarm media review schema")
        entry_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(swarm_entries)")
        }
        if "order_reviewed" not in entry_columns:
            connection.execute(
                "ALTER TABLE swarm_entries ADD COLUMN order_reviewed "
                "INTEGER NOT NULL DEFAULT 0 CHECK(order_reviewed IN (0, 1))"
            )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(swarm_photos)")
        }
        if "rejected" not in columns:
            connection.execute(
                "ALTER TABLE swarm_photos ADD COLUMN rejected "
                "INTEGER NOT NULL DEFAULT 0 CHECK(rejected IN (0, 1))"
            )
        connection.execute(
            "INSERT OR REPLACE INTO swarm_review_meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )

    def sync(self, entries: list[Entry]) -> dict[str, int]:
        now = dt.datetime.now(dt.UTC).isoformat()
        seen_entries = 0
        seen_photos = 0
        changed_photos = 0
        with self._lock, closing(self._connect()) as connection, connection:
            previous_active_photos: dict[str, set[str]] = {}
            for row in connection.execute(
                "SELECT entry_key, photo_source_id FROM swarm_photos WHERE active = 1"
            ):
                previous_active_photos.setdefault(str(row["entry_key"]), set()).add(
                    str(row["photo_source_id"])
                )
            connection.execute("UPDATE swarm_entries SET active = 0")
            connection.execute("UPDATE swarm_photos SET active = 0")
            for entry in entries:
                if not entry.photos:
                    continue
                local_date = (entry.created_utc + dt.timedelta(minutes=entry.offset_minutes)).date().isoformat()
                connection.execute(
                    """
                    INSERT INTO swarm_entries(entry_key, kind, title, created_utc, local_date, active, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(entry_key) DO UPDATE SET
                        kind = excluded.kind, title = excluded.title,
                        created_utc = excluded.created_utc, local_date = excluded.local_date,
                        active = 1, last_seen_at = excluded.last_seen_at
                    """,
                    (entry.source_key, entry.kind, entry.title, entry.created_utc.isoformat(), local_date, now),
                )
                seen_entries += 1
                custom_row = connection.execute(
                    "SELECT MAX(custom_position) FROM swarm_photos WHERE entry_key = ?",
                    (entry.source_key,),
                ).fetchone()
                next_custom_position = (
                    int(custom_row[0]) + 1 if custom_row is not None and custom_row[0] is not None
                    else None
                )
                current_photo_ids: set[str] = set()
                for position, photo in enumerate(entry.photos):
                    current_photo_ids.add(photo.source_id)
                    key = _photo_key(entry.source_key, photo.source_id)
                    digest = _sha256(photo.path)
                    existing = connection.execute(
                        "SELECT source_sha256, custom_position FROM swarm_photos WHERE photo_key = ?",
                        (key,),
                    ).fetchone()
                    changed = existing is not None and str(existing[0]) != digest
                    inserted_custom_position = next_custom_position if existing is None else None
                    if inserted_custom_position is not None:
                        next_custom_position += 1
                    connection.execute(
                        """
                        INSERT INTO swarm_photos(
                            photo_key, entry_key, photo_source_id, source_path, source_sha256,
                            width, height, default_position, custom_position, active, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        ON CONFLICT(photo_key) DO UPDATE SET
                            entry_key = excluded.entry_key,
                            photo_source_id = excluded.photo_source_id,
                            source_path = excluded.source_path,
                            source_sha256 = excluded.source_sha256,
                            width = excluded.width,
                            height = excluded.height,
                            default_position = excluded.default_position,
                            active = 1,
                            last_seen_at = excluded.last_seen_at,
                            crop_json = CASE WHEN swarm_photos.source_sha256 != excluded.source_sha256 THEN '' ELSE swarm_photos.crop_json END,
                            crop_derivative_path = CASE WHEN swarm_photos.source_sha256 != excluded.source_sha256 THEN '' ELSE swarm_photos.crop_derivative_path END,
                            crop_source_sha256 = CASE WHEN swarm_photos.source_sha256 != excluded.source_sha256 THEN '' ELSE swarm_photos.crop_source_sha256 END,
                            digikam_working_path = CASE WHEN swarm_photos.source_sha256 != excluded.source_sha256 THEN '' ELSE swarm_photos.digikam_working_path END,
                            digikam_source_sha256 = CASE WHEN swarm_photos.source_sha256 != excluded.source_sha256 THEN '' ELSE swarm_photos.digikam_source_sha256 END
                        """,
                        (
                            key, entry.source_key, photo.source_id, str(photo.path.resolve()), digest,
                            photo.width, photo.height, position, inserted_custom_position, now,
                        ),
                    )
                    if changed:
                        connection.execute("DELETE FROM swarm_photo_people WHERE photo_key = ?", (key,))
                        changed_photos += 1
                    seen_photos += 1
                previous_photo_ids = previous_active_photos.get(entry.source_key)
                if previous_photo_ids is not None and previous_photo_ids != current_photo_ids:
                    connection.execute(
                        "UPDATE swarm_entries SET order_reviewed = 0 WHERE entry_key = ?",
                        (entry.source_key,),
                    )
                    connection.execute(
                        "DELETE FROM swarm_order_staging WHERE entry_key = ?",
                        (entry.source_key,),
                    )
            connection.execute(
                """
                DELETE FROM swarm_order_staging
                WHERE NOT EXISTS (
                    SELECT 1 FROM swarm_entries
                    WHERE swarm_entries.entry_key = swarm_order_staging.entry_key
                      AND swarm_entries.active = 1
                )
                """
            )
            connection.commit()
        return {
            "active_entry_count": seen_entries,
            "active_photo_count": seen_photos,
            "changed_photo_count": changed_photos,
        }

    def summary(self) -> dict[str, int]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS photo_count,
                    SUM(CASE WHEN rejected = 0 THEN crop_queued ELSE 0 END) AS crop_queued_count,
                    SUM(CASE WHEN crop_queued = 1 AND crop_derivative_path != ''
                                  AND rejected = 0
                                  AND crop_source_sha256 = source_sha256 THEN 1 ELSE 0 END) AS crop_ready_count,
                    SUM(CASE WHEN digikam_working_path != ''
                                  AND rejected = 0
                                  AND digikam_source_sha256 = source_sha256 THEN 1 ELSE 0 END) AS digikam_ready_count,
                    SUM(rejected) AS rejected_photo_count
                FROM swarm_photos WHERE active = 1
                """
            ).fetchone()
            order_counts = connection.execute(
                """
                SELECT COUNT(*) AS total_count,
                       SUM(CASE WHEN order_reviewed = 0 THEN 1 ELSE 0 END) AS pending_count,
                       SUM(CASE WHEN order_reviewed = 1 THEN 1 ELSE 0 END) AS reviewed_count
                FROM (
                    SELECT entries.entry_key, entries.order_reviewed
                    FROM swarm_entries AS entries
                    JOIN swarm_photos AS photos
                      ON photos.entry_key = entries.entry_key AND photos.active = 1
                    WHERE entries.active = 1
                    GROUP BY entries.entry_key
                    HAVING COUNT(photos.photo_key) > 1
                )
                """
            ).fetchone()
            people = connection.execute(
                """
                SELECT COUNT(*) FROM swarm_photo_people AS people
                JOIN swarm_photos AS photos ON photos.photo_key = people.photo_key
                WHERE photos.active = 1 AND photos.rejected = 0
                """
            ).fetchone()[0]
            staged_orders = connection.execute(
                """
                SELECT COUNT(*)
                FROM swarm_order_staging AS staging
                JOIN swarm_entries AS entries ON entries.entry_key = staging.entry_key
                WHERE entries.active = 1
                """
            ).fetchone()[0]
        return {
            "photo_count": int(row["photo_count"] or 0),
            "multi_photo_entry_count": int(order_counts["total_count"] or 0),
            "pending_order_entry_count": int(order_counts["pending_count"] or 0),
            "reviewed_order_entry_count": int(order_counts["reviewed_count"] or 0),
            "pending_order_commit_count": int(staged_orders or 0),
            "crop_queued_count": int(row["crop_queued_count"] or 0),
            "crop_ready_count": int(row["crop_ready_count"] or 0),
            "digikam_ready_count": int(row["digikam_ready_count"] or 0),
            "people_assignment_count": int(people or 0),
            "rejected_photo_count": int(row["rejected_photo_count"] or 0),
        }

    def entry_keys(
        self, *, multiple_only: bool = True, include_reviewed: bool = True
    ) -> list[str]:
        having = "HAVING COUNT(photos.photo_key) > 1" if multiple_only else "HAVING COUNT(photos.photo_key) > 0"
        review_filter = "" if include_reviewed else "AND entries.order_reviewed = 0"
        with closing(self._connect()) as connection, connection:
            return [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT entries.entry_key
                    FROM swarm_entries AS entries
                    JOIN swarm_photos AS photos ON photos.entry_key = entries.entry_key AND photos.active = 1
                    WHERE entries.active = 1
                    {review_filter}
                    GROUP BY entries.entry_key
                    {having}
                    ORDER BY entries.created_utc, entries.entry_key
                    """
                )
            ]

    def entry_state(self, entry_key: str) -> dict[str, Any]:
        with closing(self._connect()) as connection, connection:
            entry = connection.execute(
                "SELECT * FROM swarm_entries WHERE entry_key = ? AND active = 1", (entry_key,)
            ).fetchone()
            if entry is None:
                raise ValueError("Unknown active Swarm entry")
            photos = connection.execute(
                """
                SELECT photos.*,
                       GROUP_CONCAT(people.canonical_name, char(31)) AS people_names
                FROM swarm_photos AS photos
                LEFT JOIN swarm_photo_people AS people ON people.photo_key = photos.photo_key
                WHERE photos.entry_key = ? AND photos.active = 1
                GROUP BY photos.photo_key
                ORDER BY COALESCE(photos.custom_position, photos.default_position),
                         photos.default_position, photos.photo_source_id
                """,
                (entry_key,),
            ).fetchall()
            staged = connection.execute(
                "SELECT photo_order_json FROM swarm_order_staging WHERE entry_key = ?",
                (entry_key,),
            ).fetchone()
        photo_rows = list(photos)
        if staged is not None:
            try:
                staged_order = json.loads(str(staged["photo_order_json"]))
            except json.JSONDecodeError as error:
                raise ValueError("Invalid staged photo order") from error
            by_source_id = {str(row["photo_source_id"]): row for row in photo_rows}
            if (
                not isinstance(staged_order, list)
                or not all(isinstance(source_id, str) for source_id in staged_order)
                or set(staged_order) != set(by_source_id)
                or len(staged_order) != len(by_source_id)
            ):
                raise ValueError("Invalid staged photo order")
            photo_rows = [by_source_id[source_id] for source_id in staged_order]
        return {
            "entry_key": entry_key,
            "title": str(entry["title"]),
            "kind": str(entry["kind"]),
            "local_date": str(entry["local_date"]),
            "order_reviewed": bool(entry["order_reviewed"]),
            "order_staged": staged is not None,
            "photos": [
                {
                    **dict(row),
                    "crop_queued": bool(row["crop_queued"]),
                    "rejected": bool(row["rejected"]),
                    "crop_ready": bool(
                        row["crop_queued"] and row["crop_derivative_path"]
                        and row["crop_source_sha256"] == row["source_sha256"]
                    ),
                    "people": str(row["people_names"] or "").split(chr(31))
                    if row["people_names"] else [],
                }
                for row in photo_rows
            ],
        }

    @staticmethod
    def _validate_photo_order(
        connection: sqlite3.Connection,
        entry_key: str,
        photo_source_ids: list[str],
        *,
        require_multiple: bool = False,
    ) -> None:
        if (
            not photo_source_ids
            or not all(isinstance(source_id, str) for source_id in photo_source_ids)
            or len(photo_source_ids) != len(set(photo_source_ids))
        ):
            raise ValueError("Photo order must contain unique photo identities")
        known = {
            str(row[0]) for row in connection.execute(
                "SELECT photo_source_id FROM swarm_photos WHERE entry_key = ? AND active = 1",
                (entry_key,),
            )
        }
        if require_multiple and len(known) <= 1:
            raise ValueError("Photo order review requires an active multi-photo entry")
        if known != set(photo_source_ids):
            raise ValueError("Photo order must contain every current entry photo exactly once")

    def stage_order(self, entry_key: str, photo_source_ids: list[str]) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            self._validate_photo_order(
                connection, entry_key, photo_source_ids, require_multiple=True
            )
            connection.execute(
                """
                INSERT INTO swarm_order_staging(entry_key, photo_order_json, staged_at)
                VALUES (?, ?, ?)
                ON CONFLICT(entry_key) DO UPDATE SET
                    photo_order_json = excluded.photo_order_json,
                    staged_at = excluded.staged_at
                """,
                (
                    entry_key,
                    json.dumps(photo_source_ids, ensure_ascii=False),
                    dt.datetime.now(dt.UTC).isoformat(),
                ),
            )
            connection.commit()

    def staged_entry_keys(self) -> set[str]:
        with closing(self._connect()) as connection, connection:
            return {
                str(row[0]) for row in connection.execute(
                    """
                    SELECT staging.entry_key
                    FROM swarm_order_staging AS staging
                    JOIN swarm_entries AS entries ON entries.entry_key = staging.entry_key
                    WHERE entries.active = 1
                    """
                )
            }

    def commit_staged_orders(self) -> dict[str, int]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT staging.entry_key, staging.photo_order_json, entries.order_reviewed
                FROM swarm_order_staging AS staging
                JOIN swarm_entries AS entries ON entries.entry_key = staging.entry_key
                WHERE entries.active = 1
                ORDER BY staging.staged_at, staging.entry_key
                """
            ).fetchall()
            validated: list[tuple[str, list[str], bool]] = []
            for row in rows:
                try:
                    photo_source_ids = json.loads(str(row["photo_order_json"]))
                except json.JSONDecodeError as error:
                    raise ValueError("Invalid staged photo order") from error
                if not isinstance(photo_source_ids, list):
                    raise ValueError("Invalid staged photo order")
                entry_key = str(row["entry_key"])
                self._validate_photo_order(
                    connection, entry_key, photo_source_ids, require_multiple=True
                )
                validated.append((entry_key, photo_source_ids, bool(row["order_reviewed"])))
            for entry_key, photo_source_ids, _ in validated:
                connection.executemany(
                    "UPDATE swarm_photos SET custom_position = ? WHERE entry_key = ? AND photo_source_id = ?",
                    (
                        (position, entry_key, source_id)
                        for position, source_id in enumerate(photo_source_ids)
                    ),
                )
                connection.execute(
                    "UPDATE swarm_entries SET order_reviewed = 1 WHERE entry_key = ?",
                    (entry_key,),
                )
            if validated:
                connection.executemany(
                    "DELETE FROM swarm_order_staging WHERE entry_key = ?",
                    ((entry_key,) for entry_key, _, _ in validated),
                )
            connection.commit()
        return {
            "submitted_count": len(validated),
            "newly_reviewed_count": sum(not reviewed for _, _, reviewed in validated),
        }

    def save_order(self, entry_key: str, photo_source_ids: list[str]) -> None:
        if not photo_source_ids or len(photo_source_ids) != len(set(photo_source_ids)):
            raise ValueError("Photo order must contain unique photo identities")
        with self._lock, closing(self._connect()) as connection, connection:
            known = {
                str(row[0]) for row in connection.execute(
                    "SELECT photo_source_id FROM swarm_photos WHERE entry_key = ? AND active = 1",
                    (entry_key,),
                )
            }
            if known != set(photo_source_ids):
                raise ValueError("Photo order must contain every current entry photo exactly once")
            connection.executemany(
                "UPDATE swarm_photos SET custom_position = ? WHERE entry_key = ? AND photo_source_id = ?",
                ((position, entry_key, source_id) for position, source_id in enumerate(photo_source_ids)),
            )
            connection.commit()

    def complete_order_review(self, entry_key: str, photo_source_ids: list[str]) -> None:
        if not photo_source_ids or len(photo_source_ids) != len(set(photo_source_ids)):
            raise ValueError("Photo order must contain unique photo identities")
        with self._lock, closing(self._connect()) as connection, connection:
            known = {
                str(row[0]) for row in connection.execute(
                    "SELECT photo_source_id FROM swarm_photos WHERE entry_key = ? AND active = 1",
                    (entry_key,),
                )
            }
            if known != set(photo_source_ids):
                raise ValueError("Photo order must contain every current entry photo exactly once")
            connection.executemany(
                "UPDATE swarm_photos SET custom_position = ? WHERE entry_key = ? AND photo_source_id = ?",
                ((position, entry_key, source_id) for position, source_id in enumerate(photo_source_ids)),
            )
            cursor = connection.execute(
                "UPDATE swarm_entries SET order_reviewed = 1 WHERE entry_key = ? AND active = 1",
                (entry_key,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown active Swarm entry")
            connection.execute(
                "DELETE FROM swarm_order_staging WHERE entry_key = ?", (entry_key,)
            )
            connection.commit()

    def set_crop_queued(self, entry_key: str, photo_source_id: str, selected: bool) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE swarm_photos SET crop_queued = ?
                WHERE entry_key = ? AND photo_source_id = ? AND active = 1
                """,
                (int(bool(selected)), entry_key, photo_source_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown active Swarm photo")
            connection.commit()

    def set_rejected(self, entry_key: str, photo_source_id: str, rejected: bool) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE swarm_photos SET rejected = ?
                WHERE entry_key = ? AND photo_source_id = ? AND active = 1
                """,
                (int(bool(rejected)), entry_key, photo_source_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown active Swarm photo")
            connection.commit()

    def queued_photos(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            return [
                dict(row) for row in connection.execute(
                    """
                    SELECT photos.*, entries.title, entries.local_date
                    FROM swarm_photos AS photos
                    JOIN swarm_entries AS entries ON entries.entry_key = photos.entry_key
                    WHERE photos.active = 1 AND photos.rejected = 0 AND photos.crop_queued = 1
                    ORDER BY entries.created_utc,
                             COALESCE(photos.custom_position, photos.default_position),
                             photos.photo_source_id
                    """
                )
            ]

    def queued_photo(self, photo_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT photos.*, entries.title, entries.local_date
                FROM swarm_photos AS photos
                JOIN swarm_entries AS entries ON entries.entry_key = photos.entry_key
                WHERE photos.photo_key = ?
                  AND photos.active = 1
                  AND photos.rejected = 0
                  AND photos.crop_queued = 1
                """,
                (photo_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def render_crop(self, photo_key: str, crop: dict[str, Any]) -> Path:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT * FROM swarm_photos
                WHERE photo_key = ? AND active = 1 AND rejected = 0 AND crop_queued = 1
                """,
                (photo_key,),
            ).fetchone()
            if row is None:
                raise ValueError("Photo is not in the active crop queue")
            source_path = Path(str(row["source_path"])).resolve()
            if not source_path.is_file() or _sha256(source_path) != str(row["source_sha256"]):
                raise ValueError("Swarm crop source is missing or changed")
            normalized = _normalized_review_crop(crop, source_path)
            normalized.update(source="manual", unit="source_pixels", shape="square")
            derivative_root = self.path.parent / "crop_derivatives"
            derivative_root.mkdir(parents=True, exist_ok=True)
            fingerprint = hashlib.sha256(
                (str(row["photo_key"]) + str(row["source_sha256"]) + json.dumps(
                    normalized, sort_keys=True
                )).encode("utf-8")
            ).hexdigest()
            output_path = derivative_root / f"{fingerprint}.jpg"
            if not output_path.is_file():
                _convert_image_atomically(source_path, output_path, "jpeg", 2560, 88, normalized)
            connection.execute(
                """
                UPDATE swarm_photos
                SET crop_json = ?, crop_derivative_path = ?, crop_source_sha256 = source_sha256
                WHERE photo_key = ?
                """,
                (json.dumps(normalized, sort_keys=True), str(output_path), photo_key),
            )
            connection.commit()
            return output_path

    def clear_crop(self, photo_key: str) -> None:
        """Clear the applied crop while leaving the photo in the review queue."""
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE swarm_photos
                SET crop_json = '', crop_derivative_path = '', crop_source_sha256 = ''
                WHERE photo_key = ? AND active = 1 AND rejected = 0 AND crop_queued = 1
                """,
                (photo_key,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Photo is not in the active crop queue")
            connection.commit()

    def prepare_digikam(self) -> dict[str, Any]:
        root = self.path.parent / "digikam_working_copies"
        copied = 0
        existing = 0
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT photos.*, entries.local_date
                FROM swarm_photos AS photos
                JOIN swarm_entries AS entries ON entries.entry_key = photos.entry_key
                WHERE photos.active = 1 AND photos.rejected = 0
                ORDER BY entries.local_date, photos.photo_key
                """
            ).fetchall()
            for row in rows:
                source = Path(str(row["source_path"]))
                digest = str(row["source_sha256"])
                if not source.is_file() or _sha256(source) != digest:
                    raise ValueError("A Swarm photo changed while preparing digiKam copies")
                month = str(row["local_date"])[:7]
                safe_id = hashlib.sha256(str(row["photo_key"]).encode("utf-8")).hexdigest()[:16]
                suffix = source.suffix.lower()
                destination = root / month / (
                    f"Swarm - {row['local_date']} - {safe_id}-{digest[:12]}{suffix}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_file() and _sha256(destination) == digest:
                    existing += 1
                else:
                    with tempfile.NamedTemporaryFile(
                        dir=destination.parent, suffix=destination.suffix, delete=False
                    ) as handle:
                        temporary = Path(handle.name)
                    try:
                        shutil.copy2(source, temporary)
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                    copied += 1
                connection.execute(
                    """
                    UPDATE swarm_photos
                    SET digikam_working_path = ?, digikam_source_sha256 = source_sha256
                    WHERE photo_key = ?
                    """,
                    (str(destination), str(row["photo_key"])),
                )
            connection.commit()
        return {"working_root": str(root), "copied_count": copied, "existing_count": existing}

    def import_digikam_people(self) -> dict[str, int]:
        scanned = 0
        updated = 0
        assignments = 0
        errors = 0
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT photo_key, digikam_working_path, source_sha256, digikam_source_sha256
                FROM swarm_photos
                WHERE active = 1 AND rejected = 0 AND digikam_working_path != ''
                """
            ).fetchall()
            for row in rows:
                if str(row["source_sha256"]) != str(row["digikam_source_sha256"]):
                    continue
                working = Path(str(row["digikam_working_path"]))
                sidecar = working.with_suffix(working.suffix + ".xmp")
                if not sidecar.is_file():
                    continue
                scanned += 1
                try:
                    names = _extract_people_from_xmp(sidecar)
                except Exception:  # malformed sidecars retain the last known good mapping
                    errors += 1
                    continue
                connection.execute(
                    "DELETE FROM swarm_photo_people WHERE photo_key = ? AND source = 'digikam_xmp'",
                    (str(row["photo_key"]),),
                )
                connection.executemany(
                    """
                    INSERT INTO swarm_photo_people(photo_key, canonical_name, source)
                    VALUES (?, ?, 'digikam_xmp')
                    """,
                    ((str(row["photo_key"]), name) for name in names),
                )
                assignments += len(names)
                updated += 1
            connection.commit()
        return {
            "scanned_sidecar_count": scanned,
            "updated_photo_count": updated,
            "people_assignment_count": assignments,
            "error_count": errors,
        }


class SwarmMediaReviewState:
    def __init__(self, store: SwarmMediaStore):
        self.store = store
        self.csrf = secrets.token_urlsafe(24)
        self.media_by_token: dict[str, Path] = {}
        self.token_by_photo_key: dict[str, str] = {}
        self.photo_key_by_token: dict[str, str] = {}
        self.refresh_tokens()

    def refresh_tokens(self) -> None:
        self.media_by_token.clear()
        self.token_by_photo_key.clear()
        self.photo_key_by_token.clear()
        with closing(self.store._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT photo_key, source_path FROM swarm_photos WHERE active = 1"
            ).fetchall()
        for row in rows:
            photo_key = str(row["photo_key"])
            token = hashlib.sha256(photo_key.encode("utf-8")).hexdigest()[:24]
            self.token_by_photo_key[photo_key] = token
            self.photo_key_by_token[token] = photo_key
            self.media_by_token[token] = Path(str(row["source_path"])).resolve()

    def entry_payload(
        self,
        index: int,
        *,
        include_reviewed: bool = False,
        skip_staged: bool = False,
    ) -> dict[str, Any]:
        keys = self.store.entry_keys(multiple_only=True, include_reviewed=include_reviewed)
        if not keys:
            return {"entry": None, "index": 0, "count": 0, "summary": self.store.summary()}
        bounded = max(0, min(index, len(keys) - 1))
        if skip_staged:
            staged_keys = self.store.staged_entry_keys()
            remaining_indexes = [
                candidate_index
                for candidate_index in range(bounded, len(keys))
                if keys[candidate_index] not in staged_keys
            ]
            if not remaining_indexes:
                remaining_indexes = [
                    candidate_index
                    for candidate_index in range(0, bounded)
                    if keys[candidate_index] not in staged_keys
                ]
            if not remaining_indexes:
                return {
                    "entry": None,
                    "index": len(keys),
                    "count": len(keys),
                    "summary": self.store.summary(),
                }
            bounded = remaining_indexes[0]
        entry = self.store.entry_state(keys[bounded])
        entry_token = hashlib.sha256(entry["entry_key"].encode("utf-8")).hexdigest()[:24]
        for photo in entry["photos"]:
            token = self.token_by_photo_key[str(photo["photo_key"])]
            photo["photo_token"] = token
            photo["preview_url"] = f"/media/{token}"
            photo.pop("source_path", None)
        entry["entry_token"] = entry_token
        return {"entry": entry, "index": bounded, "count": len(keys), "summary": self.store.summary()}

    def entry_key(self, token: str) -> str:
        for key in self.store.entry_keys(multiple_only=False):
            if hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] == token:
                return key
        raise ValueError("Unknown Swarm entry token")

    def photo_identity(self, token: str) -> tuple[str, str, str]:
        photo_key = self.photo_key_by_token.get(token)
        if photo_key is None:
            raise ValueError("Unknown Swarm photo token")
        with closing(self.store._connect()) as connection, connection:
            row = connection.execute(
                "SELECT entry_key, photo_source_id FROM swarm_photos WHERE photo_key = ? AND active = 1",
                (photo_key,),
            ).fetchone()
        if row is None:
            raise ValueError("Inactive Swarm photo token")
        return photo_key, str(row["entry_key"]), str(row["photo_source_id"])

    def crop_payload(self, index: int) -> dict[str, Any]:
        rows = self.store.queued_photos()
        if not rows:
            return {"photo": None, "index": 0, "count": 0, "summary": self.store.summary()}
        bounded = max(0, min(index, len(rows) - 1))
        row = rows[bounded]
        token = self.token_by_photo_key[str(row["photo_key"])]
        crop = json.loads(str(row["crop_json"] or "{}"))
        return {
            "photo": {
                "photo_token": token,
                "preview_url": f"/media/{token}",
                "title": str(row["title"]),
                "local_date": str(row["local_date"]),
                "width": int(row["width"] or 0),
                "height": int(row["height"] or 0),
                "crop": crop,
                "crop_ready": bool(
                    row["crop_derivative_path"]
                    and row["crop_source_sha256"] == row["source_sha256"]
                    and Path(str(row["crop_derivative_path"])).is_file()
                ),
            },
            "index": bounded,
            "count": len(rows),
            "summary": self.store.summary(),
        }


def _order_html(csrf: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swarm photo order</title><style>{_COMMON_CSS}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}.photo-slot{{min-width:0;padding:8px;border:3px dashed #aeb5ad;border-radius:12px;background:#f7f9f7;transition:border-color .12s,background .12s,box-shadow .12s}}.slot-label{{display:flex;justify-content:space-between;gap:8px;align-items:center;min-height:30px;padding:0 3px 7px;color:var(--muted);font-size:13px;font-weight:700}}.slot-position{{color:var(--ink)}}.slot-hint{{white-space:nowrap}}.slot-content{{min-height:304px}}.photo-slot.cover-slot{{border-color:var(--accent)}}.photo-slot.rejected-slot{{border-style:dotted;background:#f3f3f0}}.grid.drag-active .photo-slot:not(.rejected-slot){{border-color:#6c9c94;background:#f1f8f6}}.photo-slot.drop-target{{border-color:var(--accent)!important;border-style:solid!important;background:var(--accent-soft)!important;box-shadow:0 0 0 4px #17695d2e}}.photo-slot.drop-target .slot-hint{{color:var(--accent)}}.photo-slot.drag-source{{opacity:.6}}.photo{{height:100%;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden;cursor:grab}}.photo.dragging{{opacity:.35}}.photo.rejected{{cursor:default;border-style:dashed;opacity:.72}}.photo.rejected img{{filter:grayscale(1)}}.photo img{{width:100%;height:220px;object-fit:cover;display:block;background:#eef0ed}}.copy{{padding:10px}}.item-actions{{display:flex;gap:8px;align-items:center;margin-top:10px}}.item-actions button{{flex:1 1 0}}.item-actions button[aria-pressed="true"]{{background:var(--accent);border-color:var(--accent);color:#fff}}.item-actions .reject[aria-pressed="true"]{{background:#8b2c2c;border-color:#8b2c2c}}.submit-count{{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;margin-left:5px;padding:0 6px;border-radius:11px;background:#fff;color:var(--accent);font-size:12px;font-weight:800}}#orderStatus{{color:var(--muted)}}button:disabled{{opacity:.45;cursor:default}}</style></head><body>
<header><a class="control-panel-link" href="http://127.0.0.1:8766/?step=import_hub">← Project365 Control Panel</a><h1>Swarm photo order</h1><span id="summary"></span><span id="orderStatus" role="status"></span><button class="tool-link" id="scopeButton" onclick="toggleScope()">All items</button><a class="tool-link" href="http://127.0.0.1:8766/crop?provider=swarm">Open shared crop workbench</a><button class="tool-link primary" id="submitOrdersButton" onclick="submitOrders()" disabled>Submit <span class="submit-count" id="stagedOrderCount">0</span></button></header><main><section class="panel" id="entry">Loading…</section></main>
<script>const csrf={json.dumps(csrf)};let index=0,current=null,allItems=false,entryCount=0,summary=null,submitting=false,revisitFrontier=null;const box=document.getElementById('entry');
async function api(url,opt={{}}){{const r=await fetch(url,opt);const b=await r.json();if(!r.ok)throw Error(b.error||`HTTP ${{r.status}}`);return b}}
function esc(v){{return String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
function sum(s){{return `${{s.pending_order_entry_count}} to review · ${{s.reviewed_order_entry_count}} reviewed · ${{s.rejected_photo_count}} rejected · ${{s.crop_queued_count}} queued crops · ${{s.crop_ready_count}} ready`}}
function updateHeader(s){{summary=s;document.getElementById('summary').textContent=sum(s);const pending=Number(s.pending_order_commit_count||0);document.getElementById('stagedOrderCount').textContent=pending;document.getElementById('submitOrdersButton').disabled=submitting||pending===0;document.getElementById('scopeButton').textContent=allItems?'Review queue':'All items'}}
async function load(n=0,skipStaged=false){{const d=await api(`/api/entry?index=${{Math.max(0,n)}}&all=${{allItems?1:0}}&skip_staged=${{skipStaged?1:0}}`);index=d.index;current=d.entry;entryCount=d.count;updateHeader(d.summary);render(d.count)}}
function render(count){{if(!current){{const pending=Number(summary?.pending_order_commit_count||0);box.innerHTML=pending?`All remaining items in this pass are staged. Submit ${{pending}} change${{pending===1?'':'s'}} to commit them.`:allItems?'No multi-photo entries.':'Review queue complete. Open All items to revisit completed entries.';return}}let keptPosition=0;const cards=current.photos.map(p=>{{const position=p.rejected?0:++keptPosition;const positionLabel=p.rejected?'Rejected':position===1?'Position 1 · Calendar / cover':`Position ${{position}}`;const hint=p.rejected?'Omitted from order':'Drop photo here';return `<section class="photo-slot${{p.rejected?' rejected-slot':position===1?' cover-slot':''}}" data-position="${{position}}" aria-label="${{esc(positionLabel)}}"><div class="slot-label"><span class="slot-position">${{positionLabel}}</span><span class="slot-hint">${{hint}}</span></div><div class="slot-content"><article class="photo${{p.rejected?' rejected':''}}" draggable="${{!p.rejected}}" data-token="${{p.photo_token}}"><img src="${{p.preview_url}}" alt=""><div class="copy"><div>${{p.rejected?'Omitted from export':p.crop_ready?'Crop ready':p.crop_queued?'Crop queued':'Original photo'}}${{p.people.length?' · '+esc(p.people.join(', ')):''}}</div><div class="item-actions"><button aria-pressed="${{p.crop_queued}}" onclick="queueCrop('${{p.photo_token}}',${{!p.crop_queued}})">Crop queue</button><button class="reject" aria-pressed="${{p.rejected}}" onclick="setRejected('${{p.photo_token}}',${{!p.rejected}})">Reject</button></div></div></article></div></section>`}}).join('');box.innerHTML=`<div>${{esc(current.kind)}} · ${{index+1}} of ${{count}}${{current.order_reviewed?' · Reviewed':''}}${{current.order_staged?' · Staged for Submit':''}}</div><h2>${{esc(current.title)}}</h2><div class="muted">${{esc(current.local_date)}} · Drag each retained photo into a numbered position box. Next stages this decision; Submit commits the batch.</div><div class="grid" id="grid">${{cards}}</div><div class="controls"><button onclick="goPrevious()" ${{index===0?'disabled':''}}>← Previous</button><button class="primary" onclick="stageAndNext()">Next →</button></div>`;wire()}}
function syncCurrentOrder(){{const tokens=[...document.querySelectorAll('.photo')].map(card=>card.dataset.token);if(!tokens.length||!current)return;const byToken=new Map(current.photos.map(photo=>[photo.photo_token,photo]));current.photos=tokens.map(token=>byToken.get(token))}}
function clearDropTargets(grid){{grid.querySelectorAll('.photo-slot.drop-target').forEach(slot=>slot.classList.remove('drop-target'));grid.querySelectorAll('.photo-slot:not(.rejected-slot) .slot-hint').forEach(hint=>hint.textContent='Drop photo here')}}
function movePhotoToSlot(grid,dragged,targetSlot){{const slots=[...grid.querySelectorAll('.photo-slot:not(.rejected-slot)')];const cards=slots.map(slot=>slot.querySelector('.photo'));const from=cards.indexOf(dragged),to=slots.indexOf(targetSlot);if(from<0||to<0||from===to)return;cards.splice(to,0,cards.splice(from,1)[0]);slots.forEach((slot,position)=>slot.querySelector('.slot-content').replaceChildren(cards[position]));syncCurrentOrder()}}
function wire(){{const grid=document.getElementById('grid');let dragged=null;function finishDrag(){{clearDropTargets(grid);grid.classList.remove('drag-active');grid.querySelectorAll('.drag-source').forEach(slot=>slot.classList.remove('drag-source'));if(dragged)dragged.classList.remove('dragging');dragged=null}}grid.querySelectorAll('.photo[draggable="true"]').forEach(card=>{{card.ondragstart=e=>{{dragged=card;e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',card.dataset.token);card.classList.add('dragging');card.closest('.photo-slot').classList.add('drag-source');grid.classList.add('drag-active')}};card.ondragend=finishDrag}});grid.querySelectorAll('.photo-slot:not(.rejected-slot)').forEach(slot=>{{slot.ondragenter=e=>{{e.preventDefault();if(!dragged||slot.contains(dragged))return;clearDropTargets(grid);slot.classList.add('drop-target');slot.querySelector('.slot-hint').textContent='Release to place here'}};slot.ondragover=e=>{{e.preventDefault();if(e.dataTransfer)e.dataTransfer.dropEffect='move'}};slot.ondragleave=e=>{{if(!slot.contains(e.relatedTarget)){{slot.classList.remove('drop-target');slot.querySelector('.slot-hint').textContent='Drop photo here'}}}};slot.ondrop=e=>{{e.preventDefault();e.stopPropagation();if(dragged)movePhotoToSlot(grid,dragged,slot);finishDrag()}}}});grid.ondragleave=e=>{{if(!grid.contains(e.relatedTarget))clearDropTargets(grid)}}}}
async function goPrevious(){{if(!current||index===0)return;if(revisitFrontier===null)revisitFrontier=index;await load(index-1,false)}}
async function stageAndNext(){{if(!current)return;syncCurrentOrder();const photo_tokens=current.photos.map(photo=>photo.photo_token);const d=await api('/api/stage-order',{{method:'POST',headers:{{'Content-Type':'application/json','X-Swarm-Token':csrf}},body:JSON.stringify({{entry_token:current.entry_token,photo_tokens}})}});updateHeader(d.summary);const revisiting=revisitFrontier!==null&&index<revisitFrontier;if(revisiting){{await load(index+1,false);return}}revisitFrontier=null;await load(index+1,true)}}
async function queueCrop(photo_token,selected){{syncCurrentOrder();const d=await api('/api/crop-queue',{{method:'POST',headers:{{'Content-Type':'application/json','X-Swarm-Token':csrf}},body:JSON.stringify({{photo_token,selected}})}});const photo=current.photos.find(item=>item.photo_token===photo_token);photo.crop_queued=selected;if(!selected)photo.crop_ready=false;updateHeader(d.summary);render(entryCount)}}
async function setRejected(photo_token,rejected){{syncCurrentOrder();const d=await api('/api/reject',{{method:'POST',headers:{{'Content-Type':'application/json','X-Swarm-Token':csrf}},body:JSON.stringify({{photo_token,rejected}})}});current.photos.find(photo=>photo.photo_token===photo_token).rejected=rejected;updateHeader(d.summary);render(entryCount)}}
async function submitOrders(){{if(submitting||!summary?.pending_order_commit_count)return;const status=document.getElementById('orderStatus');submitting=true;updateHeader(summary);status.textContent='Submitting…';try{{const d=await api('/api/submit-orders',{{method:'POST',headers:{{'Content-Type':'application/json','X-Swarm-Token':csrf}},body:'{{}}'}});status.textContent=`Submitted ${{d.submitted_count}} order${{d.submitted_count===1?'':'s'}}.`;updateHeader(d.summary);revisitFrontier=null;await load(allItems?Math.min(index,Math.max(0,d.summary.multi_photo_entry_count-1)):0,true)}}catch(error){{status.textContent=error.message}}finally{{submitting=false;updateHeader(summary)}}}}
function toggleScope(){{allItems=!allItems;index=0;revisitFrontier=null;load(0,true)}}
document.onkeydown=e=>{{if(e.key==='ArrowLeft')goPrevious();if(e.key==='ArrowRight')stageAndNext()}};load(0,true);</script></body></html>"""


_COMMON_CSS = """
:root{--bg:#f6f6f2;--panel:#fff;--ink:#202124;--muted:#667085;--line:#d7d9d2;--accent:#17695d;--accent-soft:#dceeea}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;z-index:5;background:#ffffffee;padding:12px 18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);backdrop-filter:blur(10px)}h1{font-size:18px;margin:0;flex:1 1 260px}a{color:var(--accent)}.control-panel-link,.tool-link{border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink);padding:7px 10px;text-decoration:none;font-weight:650;white-space:nowrap;flex:0 0 auto}.control-panel-link:hover,.tool-link:hover{border-color:var(--accent)}#summary{color:var(--muted);flex:1 1 320px}.muted{color:var(--muted)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px}main{max-width:1280px;margin:auto;padding:20px}.muted{margin-bottom:15px}.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:18px}button{background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:9px 13px;font:inherit;cursor:pointer}button:hover{border-color:var(--accent)}.primary{background:var(--accent);border-color:var(--accent);color:#fff}
"""


def create_handler(state: SwarmMediaReviewState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/":
                    self._send(_order_html(state.csrf).encode(), "text/html; charset=utf-8")
                elif parsed.path == "/crop":
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "http://127.0.0.1:8766/crop?provider=swarm")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif parsed.path == "/api/entry":
                    self._json(state.entry_payload(
                        int(query.get("index", ["0"])[0]),
                        include_reviewed=query.get("all", ["0"])[0] == "1",
                        skip_staged=query.get("skip_staged", ["0"])[0] == "1",
                    ))
                elif parsed.path == "/api/crop-entry":
                    self._json(state.crop_payload(int(query.get("index", ["0"])[0])))
                elif parsed.path == "/api/summary":
                    self._json(state.store.summary())
                elif parsed.path.startswith("/media/"):
                    token = parsed.path.removeprefix("/media/")
                    path = state.media_by_token.get(token)
                    if path is None or not path.is_file():
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._send(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (ValueError, OSError) as error:
                self._json({"error": str(error)}, 400)

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-Swarm-Token") != state.csrf:
                self._json({"error": "Invalid local review token"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_BODY_BYTES:
                    raise ValueError("Request body is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("Invalid request")
                if self.path == "/api/order":
                    entry_key = state.entry_key(str(payload.get("entry_token", "")))
                    photo_tokens = payload.get("photo_tokens")
                    if not isinstance(photo_tokens, list) or not all(isinstance(v, str) for v in photo_tokens):
                        raise ValueError("Invalid photo order")
                    identities = [state.photo_identity(token) for token in photo_tokens]
                    if any(identity[1] != entry_key for identity in identities):
                        raise ValueError("Photo order crosses Swarm entries")
                    state.store.save_order(entry_key, [identity[2] for identity in identities])
                elif self.path == "/api/stage-order":
                    entry_key = state.entry_key(str(payload.get("entry_token", "")))
                    photo_tokens = payload.get("photo_tokens")
                    if not isinstance(photo_tokens, list) or not all(isinstance(v, str) for v in photo_tokens):
                        raise ValueError("Invalid photo order")
                    identities = [state.photo_identity(token) for token in photo_tokens]
                    if any(identity[1] != entry_key for identity in identities):
                        raise ValueError("Photo order crosses Swarm entries")
                    state.store.stage_order(entry_key, [identity[2] for identity in identities])
                elif self.path == "/api/complete-order":
                    entry_key = state.entry_key(str(payload.get("entry_token", "")))
                    photo_tokens = payload.get("photo_tokens")
                    if not isinstance(photo_tokens, list) or not all(isinstance(v, str) for v in photo_tokens):
                        raise ValueError("Invalid photo order")
                    identities = [state.photo_identity(token) for token in photo_tokens]
                    if any(identity[1] != entry_key for identity in identities):
                        raise ValueError("Photo order crosses Swarm entries")
                    state.store.complete_order_review(
                        entry_key, [identity[2] for identity in identities]
                    )
                elif self.path == "/api/submit-orders":
                    result = state.store.commit_staged_orders()
                    self._json({**result, "summary": state.store.summary()})
                    return
                elif self.path == "/api/crop-queue":
                    _, entry_key, source_id = state.photo_identity(str(payload.get("photo_token", "")))
                    selected = payload.get("selected")
                    if not isinstance(selected, bool):
                        raise ValueError("Invalid crop queue decision")
                    state.store.set_crop_queued(entry_key, source_id, selected)
                elif self.path == "/api/reject":
                    _, entry_key, source_id = state.photo_identity(str(payload.get("photo_token", "")))
                    rejected = payload.get("rejected")
                    if not isinstance(rejected, bool):
                        raise ValueError("Invalid rejection decision")
                    state.store.set_rejected(entry_key, source_id, rejected)
                elif self.path == "/api/crop":
                    photo_key, _, _ = state.photo_identity(str(payload.get("photo_token", "")))
                    crop = payload.get("crop")
                    if not isinstance(crop, dict):
                        raise ValueError("Invalid crop")
                    state.store.render_crop(photo_key, crop)
                elif self.path == "/api/prepare-digikam":
                    self._json(state.store.prepare_digikam())
                    return
                elif self.path == "/api/import-digikam-people":
                    self._json(state.store.import_digikam_people())
                    return
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._json({"ok": True, "summary": state.store.summary()})
            except (ValueError, OSError, json.JSONDecodeError, sqlite3.Error) as error:
                self._json({"error": str(error)}, 400)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

        def _send(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("Project365Canonical/swarm_dayone_media/review.sqlite3"),
    )
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--prepare-digikam", action="store_true")
    parser.add_argument("--import-digikam-people", action="store_true")
    args = parser.parse_args()
    root = _export_root(args.source)
    entries, _, _, _ = _build_entries(root)
    store = SwarmMediaStore(args.database)
    sync = store.sync(entries)
    if args.prepare_digikam:
        print(json.dumps({"sync": sync, "digikam": store.prepare_digikam()}, indent=2))
        return 0
    if args.import_digikam_people:
        print(json.dumps({"sync": sync, "people": store.import_digikam_people()}, indent=2))
        return 0
    if not 1024 <= args.port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    state = SwarmMediaReviewState(store)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), create_handler(state))
    print(json.dumps({"url": f"http://127.0.0.1:{args.port}/", "sync": sync, "summary": store.summary()}, indent=2))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
