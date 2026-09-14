#!/usr/bin/env python3
"""Read-only media dedupe candidate review and decision storage."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from project365_original_matcher import IMAGE_EXTENSIONS
from project365_photo_library_index import (
    VIDEO_EXTENSIONS,
    default_index_db,
    default_legacy_video_sidecar_index_db,
    default_reexport_sidecar_index_db,
)


DECISION_VALUES = {
    "confirm_duplicate_delete_legacy",
    "confirm_duplicate_delete_reexport",
    "reject_duplicate",
}
DEFAULT_LIMIT = 1


@dataclass(frozen=True)
class MediaIndexRow:
    path: str
    root: str
    filename: str
    extension: str
    byte_size: int
    sha256: str
    normalized_stem: str
    capture_timestamp: str
    capture_datetime: dt.datetime | None
    dates: tuple[str, ...]
    has_gps: bool
    media_width: int | None
    media_height: int | None
    media_duration_seconds: float | None


@dataclass(frozen=True)
class MediaDedupeCandidate:
    media_type: str
    candidate_key: str
    reexport: MediaIndexRow
    legacy: MediaIndexRow
    score: int
    reasons: tuple[str, ...]
    timestamp_delta_seconds: float | None
    duration_delta_seconds: float | None


def decision_db_path(canonical_root: Path) -> Path:
    return canonical_root / "media_dedupe_decisions.sqlite"


def index_paths_for_media_type(canonical_root: Path, media_type: str) -> tuple[Path, Path]:
    if media_type == "video":
        return (
            default_reexport_sidecar_index_db(canonical_root),
            default_legacy_video_sidecar_index_db(canonical_root),
        )
    if media_type == "photo":
        return (
            default_reexport_sidecar_index_db(canonical_root),
            default_index_db(canonical_root),
        )
    raise ValueError("media_type must be photo or video")


def candidate_page(
    canonical_root: Path,
    media_type: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    status: str = "unreviewed",
    refresh: bool = False,
) -> dict[str, object]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must not be negative")
    candidates = cached_candidate_payloads(canonical_root, media_type, refresh=refresh)
    decisions = _decision_map(decision_db_path(canonical_root), media_type)
    filtered = [
        candidate
        for candidate in candidates
        if status == "all" or str(candidate.get("candidate_key", "")) not in decisions
    ]
    page = filtered[offset : offset + limit]
    return {
        "media_type": media_type,
        "status": status,
        "offset": offset,
        "limit": limit,
        "total_count": len(filtered),
        "candidate_count": len(candidates),
        "returned_count": len(page),
        "has_more": offset + limit < len(filtered),
        "reviewed_count": len(decisions),
        "candidates": [
            {**candidate, "decision": decisions.get(str(candidate.get("candidate_key", ""))) or {}}
            for candidate in page
        ],
    }


def cached_candidate_payloads(
    canonical_root: Path,
    media_type: str,
    refresh: bool = False,
) -> list[dict[str, object]]:
    db_path = decision_db_path(canonical_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        connection.row_factory = sqlite3.Row
        _initialize_decision_schema(connection)
        if refresh:
            connection.execute(
                "DELETE FROM media_dedupe_candidates WHERE media_type = ?",
                (media_type,),
            )
            connection.commit()
        cached = _load_cached_candidate_payloads(connection, media_type)
        if cached:
            return cached
        generated = [candidate_payload(candidate) for candidate in dedupe_candidates(canonical_root, media_type)]
        _store_candidate_payloads(connection, media_type, generated)
        connection.commit()
        return generated
    finally:
        connection.close()


def candidate_payload(
    candidate: MediaDedupeCandidate,
    decision: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = {
        "media_type": candidate.media_type,
        "candidate_key": candidate.candidate_key,
        "classification": "candidate_same_media",
        "deletion_safety": "decision_only_no_file_action",
        "score": candidate.score,
        "reasons": list(candidate.reasons),
        "timestamp_delta_seconds": candidate.timestamp_delta_seconds,
        "duration_delta_seconds": candidate.duration_delta_seconds,
        "reexport": media_row_payload(candidate.reexport),
        "legacy": media_row_payload(candidate.legacy),
        "decision": decision or {},
    }
    return payload


def media_row_payload(row: MediaIndexRow) -> dict[str, object]:
    return {
        "path": row.path,
        "root": row.root,
        "filename": row.filename,
        "extension": row.extension,
        "byte_size": row.byte_size,
        "sha256": row.sha256,
        "capture_timestamp": row.capture_timestamp,
        "dates": list(row.dates),
        "has_gps": row.has_gps,
        "media_width": row.media_width,
        "media_height": row.media_height,
        "media_duration_seconds": row.media_duration_seconds,
        "resolution_pixels": _resolution_pixels(row),
    }


def dedupe_candidates(canonical_root: Path, media_type: str) -> list[MediaDedupeCandidate]:
    reexport_db, legacy_db = index_paths_for_media_type(canonical_root, media_type)
    if not reexport_db.exists():
        raise FileNotFoundError(f"Missing re-export sidecar index: {reexport_db}")
    if not legacy_db.exists():
        raise FileNotFoundError(f"Missing legacy index: {legacy_db}")
    extensions = VIDEO_EXTENSIONS if media_type == "video" else IMAGE_EXTENSIONS
    reexport_rows = _load_rows(reexport_db, extensions)
    legacy_rows = _load_rows(legacy_db, extensions)
    return _find_candidates(media_type, reexport_rows, legacy_rows)


def record_decision(
    canonical_root: Path,
    media_type: str,
    candidate_key: str,
    reexport_path: str,
    legacy_path: str,
    decision: str,
    notes: str = "",
) -> dict[str, object]:
    if decision not in DECISION_VALUES:
        raise ValueError("decision must be confirm_duplicate_delete_legacy, confirm_duplicate_delete_reexport, or reject_duplicate")
    db_path = decision_db_path(canonical_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        _initialize_decision_schema(connection)
        decided_at = dt.datetime.now(dt.UTC).isoformat()
        connection.execute(
            """
            INSERT INTO media_dedupe_decisions (
                media_type,
                candidate_key,
                reexport_path,
                legacy_path,
                decision,
                notes,
                decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_type, candidate_key)
            DO UPDATE SET
                reexport_path = excluded.reexport_path,
                legacy_path = excluded.legacy_path,
                decision = excluded.decision,
                notes = excluded.notes,
                decided_at = excluded.decided_at
            """,
            (
                media_type,
                candidate_key,
                reexport_path,
                legacy_path,
                decision,
                notes,
                decided_at,
            ),
        )
        connection.commit()
        return {
            "media_type": media_type,
            "candidate_key": candidate_key,
            "reexport_path": reexport_path,
            "legacy_path": legacy_path,
            "decision": decision,
            "notes": notes,
            "decided_at": decided_at,
            "file_action_taken": False,
        }
    finally:
        connection.close()


def _load_rows(index_db: Path, extensions: set[str]) -> list[MediaIndexRow]:
    connection = sqlite3.connect(f"{index_db.resolve().as_uri()}?mode=ro", uri=True, timeout=60)
    try:
        connection.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in extensions)
        rows = connection.execute(
            f"""
            SELECT
                path,
                root,
                filename,
                extension,
                byte_size,
                sha256,
                capture_timestamp,
                has_gps,
                media_width,
                media_height,
                media_duration_seconds
            FROM photo_library_files
            WHERE lower(extension) IN ({placeholders})
            ORDER BY path
            """,
            tuple(sorted(extensions)),
        ).fetchall()
        dates_by_path = _load_dates_by_path(connection)
        return [_row_from_sqlite(row, dates_by_path.get(str(row["path"]), ())) for row in rows]
    finally:
        connection.close()


def _load_dates_by_path(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_library_dates'"
    ).fetchone()
    if row is None:
        return {}
    dates_by_path: dict[str, set[str]] = defaultdict(set)
    for file_path, date_value in connection.execute(
        "SELECT file_path, date FROM photo_library_dates WHERE date != ''"
    ):
        dates_by_path[str(file_path)].add(str(date_value))
    return {path: tuple(sorted(values)) for path, values in dates_by_path.items()}


def _row_from_sqlite(row: sqlite3.Row, dates: tuple[str, ...]) -> MediaIndexRow:
    filename = str(row["filename"] or Path(str(row["path"])).name)
    capture_timestamp = str(row["capture_timestamp"] or "")
    capture_datetime = _parse_capture_datetime(capture_timestamp)
    date_values = set(dates)
    if capture_datetime is not None:
        date_values.add(capture_datetime.date().isoformat())
    return MediaIndexRow(
        path=str(row["path"]),
        root=str(row["root"] or ""),
        filename=filename,
        extension=str(row["extension"] or Path(filename).suffix).lower(),
        byte_size=int(row["byte_size"] or 0),
        sha256=str(row["sha256"] or ""),
        normalized_stem=_normalized_stem(filename),
        capture_timestamp=capture_timestamp,
        capture_datetime=capture_datetime,
        dates=tuple(sorted(date_values)),
        has_gps=bool(row["has_gps"]),
        media_width=_optional_int(row["media_width"]),
        media_height=_optional_int(row["media_height"]),
        media_duration_seconds=_optional_float(row["media_duration_seconds"]),
    )


def _find_candidates(
    media_type: str,
    reexport_rows: list[MediaIndexRow],
    legacy_rows: list[MediaIndexRow],
) -> list[MediaDedupeCandidate]:
    legacy_by_hash_size: dict[tuple[str, int], list[MediaIndexRow]] = defaultdict(list)
    legacy_by_stem: dict[str, list[MediaIndexRow]] = defaultdict(list)
    legacy_by_date: dict[str, list[MediaIndexRow]] = defaultdict(list)
    legacy_by_date_dimensions: dict[tuple[str, tuple[int, int]], list[MediaIndexRow]] = defaultdict(list)
    for legacy in legacy_rows:
        if legacy.sha256:
            legacy_by_hash_size[(legacy.sha256, legacy.byte_size)].append(legacy)
        if legacy.normalized_stem:
            legacy_by_stem[legacy.normalized_stem].append(legacy)
        dimension_pair = _dimension_pair(legacy)
        for date_value in legacy.dates:
            legacy_by_date[date_value].append(legacy)
            if dimension_pair is not None:
                legacy_by_date_dimensions[(date_value, dimension_pair)].append(legacy)

    candidates: list[MediaDedupeCandidate] = []
    for reexport in reexport_rows:
        possible: dict[str, MediaIndexRow] = {}
        if reexport.sha256:
            for legacy in legacy_by_hash_size.get((reexport.sha256, reexport.byte_size), []):
                possible[legacy.path] = legacy
        if reexport.normalized_stem:
            for legacy in legacy_by_stem.get(reexport.normalized_stem, []):
                possible[legacy.path] = legacy
        if media_type == "photo":
            dimension_pair = _dimension_pair(reexport)
            if dimension_pair is not None:
                for date_value in _date_neighborhood(reexport.dates, tolerance_days=1):
                    for legacy in legacy_by_date_dimensions.get((date_value, dimension_pair), []):
                        possible[legacy.path] = legacy
        else:
            for date_value in _date_neighborhood(reexport.dates, tolerance_days=1):
                for legacy in legacy_by_date.get(date_value, []):
                    possible[legacy.path] = legacy
        for legacy in possible.values():
            candidate = _score_candidate(media_type, reexport, legacy)
            if candidate.score >= _minimum_score(media_type) and _has_identity_anchor(candidate):
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item.reexport.path, -item.score, item.legacy.path))


def _score_candidate(media_type: str, reexport: MediaIndexRow, legacy: MediaIndexRow) -> MediaDedupeCandidate:
    score = 0
    reasons: list[str] = []
    timestamp_delta = _timestamp_delta_seconds(reexport, legacy)
    duration_delta = _duration_delta_seconds(reexport, legacy)
    if reexport.sha256 and reexport.sha256 == legacy.sha256 and reexport.byte_size == legacy.byte_size:
        score += 100
        reasons.append("exact_hash_size_match")
    if reexport.normalized_stem and reexport.normalized_stem == legacy.normalized_stem:
        score += 35
        reasons.append("normalized_stem_match")
    if timestamp_delta is not None and timestamp_delta <= 2:
        score += 30
        reasons.append("capture_timestamp_within_tolerance")
    elif set(reexport.dates) & set(legacy.dates):
        score += 15
        reasons.append("capture_date_match")
    if media_type == "video" and duration_delta is not None and duration_delta <= 1:
        score += 25
        reasons.append("duration_within_tolerance")
    if _same_dimensions(reexport, legacy):
        score += 20
        reasons.append("dimensions_match")
    elif _same_orientation(reexport, legacy):
        score += 8
        reasons.append("orientation_match")
    if reexport.extension and reexport.extension == legacy.extension:
        score += 5
        reasons.append("extension_match")
    if reexport.has_gps == legacy.has_gps:
        score += 3
        reasons.append("gps_presence_match")
    return MediaDedupeCandidate(
        media_type=media_type,
        candidate_key=_candidate_key(media_type, reexport.path, legacy.path),
        reexport=reexport,
        legacy=legacy,
        score=score,
        reasons=tuple(reasons),
        timestamp_delta_seconds=timestamp_delta,
        duration_delta_seconds=duration_delta,
    )


def _minimum_score(media_type: str) -> int:
    return 45 if media_type == "video" else 55


def _has_identity_anchor(candidate: MediaDedupeCandidate) -> bool:
    reasons = set(candidate.reasons)
    if "exact_hash_size_match" in reasons:
        return True
    if "normalized_stem_match" in reasons and ("capture_date_match" in reasons or "dimensions_match" in reasons):
        return True
    if "capture_timestamp_within_tolerance" in reasons and "dimensions_match" in reasons:
        return True
    if candidate.media_type == "video":
        return {
            "capture_date_match",
            "duration_within_tolerance",
            "dimensions_match",
        }.issubset(reasons)
    return {
        "capture_date_match",
        "dimensions_match",
        "extension_match",
    }.issubset(reasons)


def _candidate_key(media_type: str, reexport_path: str, legacy_path: str) -> str:
    text = f"{media_type}\0{reexport_path}\0{legacy_path}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decision_map(db_path: Path, media_type: str) -> dict[str, dict[str, object]]:
    if not db_path.exists():
        return {}
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        connection.row_factory = sqlite3.Row
        _initialize_decision_schema(connection)
        rows = connection.execute(
            """
            SELECT *
            FROM media_dedupe_decisions
            WHERE media_type = ?
            """,
            (media_type,),
        ).fetchall()
        return {str(row["candidate_key"]): dict(row) for row in rows}
    finally:
        connection.close()


def _initialize_decision_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS media_dedupe_decisions (
            media_type TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            reexport_path TEXT NOT NULL,
            legacy_path TEXT NOT NULL,
            decision TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL,
            PRIMARY KEY (media_type, candidate_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS media_dedupe_candidates (
            media_type TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (media_type, candidate_key)
        )
        """
    )


def _load_cached_candidate_payloads(connection: sqlite3.Connection, media_type: str) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT payload_json
        FROM media_dedupe_candidates
        WHERE media_type = ?
        """,
        (media_type,),
    ).fetchall()
    payloads: list[dict[str, object]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    payloads.sort(
        key=lambda payload: (
            str((payload.get("reexport") or {}).get("path") or ""),
            -int(payload.get("score") or 0),
            str((payload.get("legacy") or {}).get("path") or ""),
        )
    )
    return payloads


def _store_candidate_payloads(
    connection: sqlite3.Connection,
    media_type: str,
    payloads: list[dict[str, object]],
) -> None:
    generated_at = dt.datetime.now(dt.UTC).isoformat()
    connection.executemany(
        """
        INSERT OR REPLACE INTO media_dedupe_candidates (
            media_type,
            candidate_key,
            payload_json,
            generated_at
        )
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                media_type,
                str(payload.get("candidate_key", "")),
                json.dumps(payload, sort_keys=True),
                generated_at,
            )
            for payload in payloads
        ],
    )


def _parse_capture_datetime(value: str) -> dt.datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _timestamp_delta_seconds(reexport: MediaIndexRow, legacy: MediaIndexRow) -> float | None:
    if reexport.capture_datetime is None or legacy.capture_datetime is None:
        return None
    first = reexport.capture_datetime
    second = legacy.capture_datetime
    if first.tzinfo is None and second.tzinfo is not None:
        second = second.replace(tzinfo=None)
    elif first.tzinfo is not None and second.tzinfo is None:
        first = first.replace(tzinfo=None)
    return abs((first - second).total_seconds())


def _duration_delta_seconds(reexport: MediaIndexRow, legacy: MediaIndexRow) -> float | None:
    if reexport.media_duration_seconds is None or legacy.media_duration_seconds is None:
        return None
    return abs(reexport.media_duration_seconds - legacy.media_duration_seconds)


def _same_dimensions(reexport: MediaIndexRow, legacy: MediaIndexRow) -> bool:
    first = _dimension_pair(reexport)
    second = _dimension_pair(legacy)
    return bool(first and second and first == second)


def _same_orientation(reexport: MediaIndexRow, legacy: MediaIndexRow) -> bool:
    first = _dimension_pair(reexport)
    second = _dimension_pair(legacy)
    if not first or not second:
        return False
    return (first[0] >= first[1]) == (second[0] >= second[1])


def _dimension_pair(row: MediaIndexRow) -> tuple[int, int] | None:
    if row.media_width is None or row.media_height is None:
        return None
    return tuple(sorted((row.media_width, row.media_height), reverse=True))


def _resolution_pixels(row: MediaIndexRow) -> int:
    if row.media_width is None or row.media_height is None:
        return 0
    return row.media_width * row.media_height


def _date_neighborhood(dates: tuple[str, ...], tolerance_days: int) -> set[str]:
    values: set[str] = set()
    for value in dates:
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError:
            continue
        for offset in range(-tolerance_days, tolerance_days + 1):
            values.add((parsed + dt.timedelta(days=offset)).isoformat())
    return values


def _normalized_stem(filename: str) -> str:
    normalized = unicodedata.normalize("NFKD", Path(filename).stem).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower().strip()
    previous = ""
    suffix_pattern = re.compile(
        r"(?:\s+copy|\s+duplicate|\s+dup|\s+alternate|\s+edited|\s+trimmed|\s*\(\d+\)|\s+-\s*\d+)$"
    )
    while normalized and normalized != previous:
        previous = normalized
        normalized = suffix_pattern.sub("", normalized).strip()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
