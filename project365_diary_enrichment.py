#!/usr/bin/env python3
"""Local diary-entry enrichment workflow for Project365 canonical data."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from project365_original_matcher import IMAGE_EXTENSIONS
from project365_original_reference_pipeline import _external_decision_id
from project365_original_picker import MAX_DROP_BYTES


ENRICHMENT_SOURCE_APP = "project365_enrichment"
SUBENTRY_PREFIX = "sub"


class ImageTokenProvider(Protocol):
    def image_token_for_path(self, path: Path | None) -> str:
        ...


def entry_list(
    canonical_root: Path,
    image_tokens: ImageTokenProvider | None = None,
    start_date: str = "",
    end_date: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    start_date = _normalized_optional_date(start_date, "start_date")
    end_date = _normalized_optional_date(end_date, "end_date")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")
    bounded_limit = max(1, min(int(limit or 500), 1000))
    if not start_date and not end_date:
        return {
            "entries": [],
            "requires_date_filter": True,
            "message": "Choose a date range from the Control page before opening Diary enrichment.",
        }
    db_path = canonical_root / "canonical.db"
    connection = _connect(db_path)
    try:
        predicates = ["source_app IN ('project365', ?)"]
        params: list[object] = [ENRICHMENT_SOURCE_APP]
        if start_date:
            predicates.append("entry_date >= ?")
            params.append(start_date)
        if end_date:
            predicates.append("entry_date <= ?")
            params.append(end_date)
        params.append(bounded_limit)
        rows = connection.execute(
            f"""
            SELECT id, entry_date, source_app, original_text
            FROM entries
            WHERE {' AND '.join(predicates)}
            ORDER BY entry_date, source_app != 'project365', id
            LIMIT ?
            """,
            params,
        ).fetchall()
        entries = [_entry_summary(connection, row, image_tokens) for row in rows]
        return {
            "entries": entries,
            "start_date": start_date,
            "end_date": end_date,
            "limit": bounded_limit,
            "has_more": len(entries) >= bounded_limit,
        }
    finally:
        connection.close()


def entry_detail(
    canonical_root: Path,
    entry_id: str,
    image_tokens: ImageTokenProvider | None = None,
    candidate_days: int = 0,
) -> dict[str, Any] | None:
    if candidate_days not in {0, 1, 5, 15, 30}:
        raise ValueError("Unsupported candidate date range")
    db_path = canonical_root / "canonical.db"
    connection = _connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT id, entry_date, source_app, original_text
            FROM entries
            WHERE id = ?
                AND source_app IN ('project365', ?)
            """,
            (entry_id, ENRICHMENT_SOURCE_APP),
        ).fetchone()
        if row is None:
            return None
        entry = _entry_summary(connection, row, image_tokens)
        entry["attached_photos"] = _attached_associated_photos(connection, entry_id, image_tokens)
        entry["candidates"] = _flagged_candidates(connection, str(row["entry_date"]), candidate_days, image_tokens)
        entry["candidate_days"] = candidate_days
        return entry
    finally:
        connection.close()


def add_associated_photo(
    canonical_root: Path,
    target_entry_id: str,
    source_media_asset_id: str,
    image_tokens: ImageTokenProvider | None = None,
) -> dict[str, Any]:
    db_path = canonical_root / "canonical.db"
    connection = _connect(db_path)
    try:
        target = _require_entry(connection, target_entry_id)
        source = _require_media(connection, source_media_asset_id)
        associated_date = _candidate_entry_date(source) or str(target["entry_date"])
        _upsert_external_media(
            connection=connection,
            target_entry_id=target_entry_id,
            role="external_original_associated_photo",
            source_path=Path(str(source["storage_path"])),
            import_batch_id=_entry_import_batch_id(connection, target_entry_id),
            source_name="diarium_enrichment_associated_photo",
            review_decision="add_associated_photo",
            associated_entry_date=associated_date,
            associated_date_source=_candidate_date_source(source),
        )
        connection.commit()
        return entry_detail(canonical_root, target_entry_id, image_tokens=image_tokens) or {}
    finally:
        connection.close()


def add_dropped_associated_photo(
    canonical_root: Path,
    target_entry_id: str,
    filename: str,
    content_type: str,
    payload: bytes,
    image_tokens: ImageTokenProvider | None = None,
) -> dict[str, Any]:
    safe_name = Path(filename.replace("\\", "/")).name
    suffix = Path(safe_name).suffix.lower()
    if not safe_name or suffix not in IMAGE_EXTENSIONS:
        raise ValueError("Drop one supported image file.")
    if not payload:
        raise ValueError("Dropped image is empty.")
    if len(payload) > MAX_DROP_BYTES:
        raise ValueError("Dropped image exceeds the 250 MB limit.")
    db_path = canonical_root / "canonical.db"
    connection = _connect(db_path)
    try:
        target = _require_entry(connection, target_entry_id)
        entry_date = str(target["entry_date"])
        digest = hashlib.sha256(payload).hexdigest()
        target_dir = canonical_root.parent / "Source Data" / "Diary Enrichment Photos" / entry_date[:7]
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / safe_name
        if destination.exists() and _sha256_file(destination) != digest:
            destination = target_dir / f"{destination.stem} - {digest[:12]}{suffix}"
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(destination)
        mime_type = content_type if content_type.startswith("image/") else ""
        _upsert_external_media(
            connection=connection,
            target_entry_id=target_entry_id,
            role="external_original_associated_photo",
            source_path=destination.resolve(),
            import_batch_id=_entry_import_batch_id(connection, target_entry_id),
            source_name="diarium_enrichment_drop",
            review_decision="drop_associated_photo",
            associated_entry_date=entry_date,
            associated_date_source="manual_drop",
            mime_type_override=mime_type or None,
        )
        connection.commit()
        return entry_detail(canonical_root, target_entry_id, image_tokens=image_tokens) or {}
    finally:
        connection.close()


def create_subentry_from_candidate(
    canonical_root: Path,
    parent_entry_id: str,
    source_media_asset_id: str,
    image_tokens: ImageTokenProvider | None = None,
) -> dict[str, Any]:
    db_path = canonical_root / "canonical.db"
    connection = _connect(db_path)
    try:
        _require_entry(connection, parent_entry_id)
        source = _require_media(connection, source_media_asset_id)
        source_path = Path(str(source["storage_path"]))
        if not source_path.exists():
            raise FileNotFoundError(f"Missing source photo: {source_path}")
        entry_date = _candidate_entry_date(source)
        if not entry_date:
            raise ValueError("Candidate does not have a usable photo date.")
        source_hash = str(source["sha256"] or "") or _sha256_file(source_path)
        subentry_id = f"project365:{entry_date}:{SUBENTRY_PREFIX}:{source_hash[:12]}"
        now = dt.datetime.now(dt.UTC).isoformat()
        connection.execute(
            """
            INSERT INTO entries (
                id, entry_date, source_app, original_text, corrected_text, correction_status,
                import_status, created_at, updated_at
            )
            VALUES (?, ?, ?, '', NULL, 'uncorrected', 'not_exported', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (subentry_id, entry_date, ENRICHMENT_SOURCE_APP, now, now),
        )
        _upsert_external_media(
            connection=connection,
            target_entry_id=subentry_id,
            role="external_original_reference",
            source_path=source_path,
            import_batch_id=_entry_import_batch_id(connection, parent_entry_id),
            source_name="diarium_enrichment_subentry_primary",
            review_decision="create_subentry",
            associated_entry_date=entry_date,
            associated_date_source=_candidate_date_source(source),
        )
        connection.commit()
        return {
            "entry": entry_detail(canonical_root, subentry_id, image_tokens=image_tokens) or {},
            "subentry_id": subentry_id,
        }
    finally:
        connection.close()


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _normalized_optional_date(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def _entry_summary(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    image_tokens: ImageTokenProvider | None,
) -> dict[str, Any]:
    entry_id = str(row["id"])
    primary = _primary_media(connection, entry_id)
    associated_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM media_assets
        WHERE entry_id = ?
            AND role = 'external_original_associated_photo'
            AND review_status = 'confirmed'
            AND status = 'available'
        """,
        (entry_id,),
    ).fetchone()[0]
    source_app = str(row["source_app"])
    people_names = _entry_people(connection, entry_id)
    primary_payload = _media_payload(primary, image_tokens)
    if primary_payload:
        primary_payload["people_names"] = people_names
    return {
        "entry_id": entry_id,
        "entry_date": str(row["entry_date"]),
        "source_app": source_app,
        "is_subentry": source_app == ENRICHMENT_SOURCE_APP,
        "text_present": row["original_text"] is not None and str(row["original_text"]) != "",
        "primary_photo": primary_payload,
        "primary_photo_ready": primary is not None,
        "primary_photo_status": "working_copy_ready" if primary else "working_copy_missing",
        "associated_count": int(associated_count or 0),
        "people_names": people_names,
    }


def _entry_people(connection: sqlite3.Connection, entry_id: str) -> list[str]:
    rows = connection.execute(
        """
        SELECT canonical_name
        FROM people
        WHERE entry_id = ?
            AND review_status IN ('suggested', 'confirmed', 'reviewed')
        ORDER BY canonical_name
        """,
        (entry_id,),
    ).fetchall()
    return [str(row["canonical_name"]) for row in rows if str(row["canonical_name"] or "").strip()]


def _primary_media(connection: sqlite3.Connection, entry_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM media_assets
        WHERE entry_id = ?
            AND status = 'available'
            AND role = 'diarium_derivative'
            AND COALESCE(storage_path, '') != ''
        ORDER BY updated_at DESC, id
        LIMIT 1
        """,
        (entry_id,),
    ).fetchone()


def _attached_associated_photos(
    connection: sqlite3.Connection,
    entry_id: str,
    image_tokens: ImageTokenProvider | None,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM media_assets
        WHERE entry_id = ?
            AND role = 'external_original_associated_photo'
            AND review_status = 'confirmed'
            AND status = 'available'
            AND COALESCE(storage_path, '') != ''
        ORDER BY json_extract(transformation_json, '$.associated_entry_date'), updated_at, id
        """,
        (entry_id,),
    ).fetchall()
    return [_media_payload(row, image_tokens) for row in rows]


def _flagged_candidates(
    connection: sqlite3.Connection,
    entry_date: str,
    days: int,
    image_tokens: ImageTokenProvider | None,
) -> list[dict[str, Any]]:
    target = dt.date.fromisoformat(entry_date)
    start = (target - dt.timedelta(days=days)).isoformat()
    end = (target + dt.timedelta(days=days)).isoformat()
    rows = connection.execute(
        """
        SELECT media_assets.*, entries.entry_date AS source_entry_date
        FROM media_assets
        JOIN entries
            ON entries.id = media_assets.entry_id
        WHERE media_assets.role = 'external_original_associated_photo'
            AND media_assets.review_status = 'confirmed'
            AND media_assets.status = 'available'
            AND COALESCE(media_assets.storage_path, '') != ''
            AND substr(
                COALESCE(
                    json_extract(media_assets.transformation_json, '$.associated_entry_date'),
                    entries.entry_date
                ),
                1,
                10
            ) BETWEEN ? AND ?
        ORDER BY
            ABS(julianday(substr(COALESCE(json_extract(media_assets.transformation_json, '$.associated_entry_date'), entries.entry_date), 1, 10)) - julianday(?)),
            substr(COALESCE(json_extract(media_assets.transformation_json, '$.associated_entry_date'), entries.entry_date), 1, 10),
            media_assets.updated_at,
            media_assets.id
        """,
        (start, end, entry_date),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for row in rows:
        path = str(row["storage_path"] or "")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        payload = _media_payload(row, image_tokens)
        payload["candidate_date"] = _candidate_entry_date(row)
        payload["candidate_date_source"] = _candidate_date_source(row)
        payload["source_entry_id"] = str(row["entry_id"])
        payload["source_entry_date"] = str(row["source_entry_date"])
        candidates.append(payload)
    return candidates


def _media_payload(row: sqlite3.Row | None, image_tokens: ImageTokenProvider | None) -> dict[str, Any]:
    if row is None:
        return {}
    path_text = str(row["storage_path"] or "")
    path = Path(path_text) if path_text else None
    token = image_tokens.image_token_for_path(path) if image_tokens and path else ""
    transformation = _json_object(str(row["transformation_json"] or ""))
    return {
        "media_asset_id": str(row["id"]),
        "role": str(row["role"]),
        "filename": str(row["internal_filename"] or (path.name if path else "")),
        "path": path_text,
        "sha256": str(row["sha256"] or ""),
        "byte_size": int(row["byte_size"] or 0),
        "mime_type": str(row["mime_type"] or ""),
        "token": token,
        "associated_entry_date": str(transformation.get("associated_entry_date") or ""),
        "associated_date_source": str(transformation.get("associated_date_source") or ""),
    }


def _require_entry(connection: sqlite3.Connection, entry_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM entries
        WHERE id = ?
            AND source_app IN ('project365', ?)
        """,
        (entry_id, ENRICHMENT_SOURCE_APP),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown entry")
    return row


def _require_media(connection: sqlite3.Connection, media_asset_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM media_assets
        WHERE id = ?
            AND status = 'available'
            AND COALESCE(storage_path, '') != ''
        """,
        (media_asset_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown source photo")
    return row


def _entry_import_batch_id(connection: sqlite3.Connection, entry_id: str) -> str:
    row = connection.execute(
        """
        SELECT import_batch_id
        FROM media_assets
        WHERE entry_id = ?
        ORDER BY selected_default DESC, updated_at DESC, id
        LIMIT 1
        """,
        (entry_id,),
    ).fetchone()
    if row is not None and row["import_batch_id"]:
        return str(row["import_batch_id"])
    fallback = connection.execute(
        """
        SELECT id
        FROM import_batches
        ORDER BY started_at DESC, id
        LIMIT 1
        """
    ).fetchone()
    if fallback is None:
        raise ValueError("No import batch is available for enrichment media")
    return str(fallback["id"])


def _upsert_external_media(
    connection: sqlite3.Connection,
    target_entry_id: str,
    role: str,
    source_path: Path,
    import_batch_id: str,
    source_name: str,
    review_decision: str,
    associated_entry_date: str,
    associated_date_source: str,
    mime_type_override: str | None = None,
) -> str:
    if role not in {"external_original_reference", "external_original_associated_photo"}:
        raise ValueError("Unsupported enrichment media role")
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Missing source photo: {source_path}")
    sha256 = _sha256_file(source_path)
    asset_id = _external_decision_id(target_entry_id, sha256, role)
    now = dt.datetime.now(dt.UTC).isoformat()
    transformation = {
        "source": source_name,
        "source_path": str(source_path),
        "review_decision": review_decision,
        "original_is_read_only": True,
    }
    if role == "external_original_associated_photo":
        transformation["associated_entry_date"] = associated_entry_date
        transformation["associated_date_source"] = associated_date_source or "manual"
    connection.execute(
        """
        INSERT INTO media_assets (
            id, entry_id, role, source_file_id, internal_filename, storage_path,
            sha256, byte_size, mime_type, status, review_status, selected_default,
            transformation_json, import_batch_id, created_at, updated_at
        )
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'available', 'confirmed', 0, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            storage_path = excluded.storage_path,
            sha256 = excluded.sha256,
            byte_size = excluded.byte_size,
            mime_type = excluded.mime_type,
            status = excluded.status,
            review_status = excluded.review_status,
            transformation_json = excluded.transformation_json,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            target_entry_id,
            role,
            source_path.name,
            str(source_path),
            sha256,
            source_path.stat().st_size,
            mime_type_override or mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
            json.dumps(transformation, sort_keys=True),
            import_batch_id,
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
            (target_entry_id, asset_id),
        )
    return asset_id


def _candidate_entry_date(row: sqlite3.Row) -> str:
    transformation = _json_object(str(row["transformation_json"] or ""))
    for key in ("associated_entry_date", "capture_timestamp"):
        value = str(transformation.get(key) or "").strip()
        if _valid_date_text(value[:10]):
            return value[:10]
    if "source_entry_date" in row.keys() and _valid_date_text(str(row["source_entry_date"])[:10]):
        return str(row["source_entry_date"])[:10]
    if _valid_date_text(str(row["entry_id"]).replace("project365:", "")[:10]):
        return str(row["entry_id"]).replace("project365:", "")[:10]
    return ""


def _candidate_date_source(row: sqlite3.Row) -> str:
    transformation = _json_object(str(row["transformation_json"] or ""))
    return str(transformation.get("associated_date_source") or "associated_flag").strip() or "associated_flag"


def _valid_date_text(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _json_object(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


ENRICHMENT_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 Diary Enrichment</title>
<style>
:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #202124;
  --muted: #667085;
  --line: #d9dee5;
  --accent: #17695d;
  --accent-soft: #e4f2ef;
  --warn: #9a5b00;
  --ok: #17695d;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background: var(--bg);
}
button, input { font: inherit; }
button { color: var(--ink); }
.app {
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  height: 100vh;
}
.sidebar {
  min-height: 0;
  background: var(--panel);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
}
.topbar {
  padding: 14px 12px;
  border-bottom: 1px solid var(--line);
  display: grid;
  gap: 8px;
}
.title { font-size: 16px; font-weight: 700; }
.summary { color: var(--muted); font-size: 12px; min-height: 16px; }
.back-link {
  color: var(--accent);
  font-size: 13px;
  text-decoration: none;
}
.back-link:hover { text-decoration: underline; }
.filter input {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  background: #fff;
}
.entry-list {
  overflow: auto;
  padding: 8px;
}
.entry-item {
  width: 100%;
  min-height: 58px;
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  gap: 8px;
  align-items: center;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  padding: 6px;
  text-align: left;
  cursor: pointer;
}
.entry-item:hover { background: #f0f4f7; }
.entry-item.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.entry-thumb {
  width: 48px;
  height: 48px;
  border-radius: 4px;
  border: 1px solid var(--line);
  object-fit: cover;
  background: #eef1f4;
}
.entry-date { font-weight: 650; font-size: 13px; }
.entry-sub .entry-date::before {
  content: "Sub ";
  color: var(--warn);
  font-weight: 700;
}
.entry-meta {
  color: var(--muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.people-script {
  color: var(--muted);
  font-size: 11px;
  font-style: italic;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.main {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}
.detail-header {
  background: var(--panel);
  border-bottom: 1px solid var(--line);
  padding: 14px 16px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
}
.heading {
  display: grid;
  gap: 3px;
}
.heading strong { font-size: 18px; }
.heading span { color: var(--muted); font-size: 12px; }
.heading .people-script { font-size: 12px; }
.range-controls {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.range-controls button, .candidate-actions button, .drop-zone button {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 7px 10px;
  cursor: pointer;
}
.range-controls button.active, .candidate-actions button.primary {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}
.workspace {
  min-height: 0;
  overflow: auto;
  padding: 14px 16px 24px;
  display: grid;
  gap: 14px;
}
.drop-zone {
  border: 1px dashed #aeb7c4;
  border-radius: 6px;
  background: #fff;
  min-height: 88px;
  padding: 14px;
  display: grid;
  gap: 8px;
  align-content: center;
}
.drop-zone.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.drop-zone strong { font-size: 14px; }
.drop-zone span, .section-title span { color: var(--muted); font-size: 12px; }
.section-title {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
}
.section-title strong { font-size: 14px; }
.photo-strip {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: 10px;
}
.attached-photo, .candidate-card {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  overflow: hidden;
}
.attached-photo img, .candidate-card img {
  width: 100%;
  aspect-ratio: 1 / 1;
  object-fit: cover;
  background: #eef1f4;
  display: block;
}
.photo-caption {
  padding: 8px;
  color: var(--muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.candidate-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}
.candidate-body {
  padding: 10px;
  display: grid;
  gap: 8px;
}
.candidate-title {
  font-weight: 650;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.candidate-meta { color: var(--muted); font-size: 12px; line-height: 1.35; }
.candidate-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.candidate-actions button.added {
  border-color: var(--ok);
  color: var(--ok);
}
.empty {
  color: var(--muted);
  font-size: 13px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
}
@media (max-width: 820px) {
  .app { grid-template-columns: 1fr; grid-template-rows: 240px minmax(0, 1fr); }
  .sidebar { border-right: 0; border-bottom: 1px solid var(--line); }
  .detail-header { grid-template-columns: 1fr; }
  .range-controls { justify-content: flex-start; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="topbar">
      <a class="back-link" href="/?step=diary_enrichment">Project365 Control Panel</a>
      <div class="title">Diary Enrichment</div>
      <div id="summary" class="summary"></div>
      <label class="filter"><input id="entryFilter" placeholder="Filter loaded entries"></label>
    </div>
    <div id="entryList" class="entry-list"></div>
  </aside>
  <main class="main">
    <header class="detail-header">
      <div class="heading">
        <strong id="entryHeading">No entry selected</strong>
        <span id="entryMeta"></span>
        <span id="entryPeople"></span>
      </div>
      <div id="rangeControls" class="range-controls">
        <button data-days="0" class="active">Same day</button>
        <button data-days="1">+/- 1</button>
        <button data-days="5">+/- 5</button>
        <button data-days="15">+/- 15</button>
        <button data-days="30">+/- 30</button>
      </div>
    </header>
    <section class="workspace">
      <div id="dropZone" class="drop-zone">
        <strong>Drop photos to add them to this entry</strong>
        <span id="dropStatus">Open an entry, then drop supported image files here.</span>
      </div>
      <div class="section-title">
        <strong>Attached Additional Photos</strong>
        <span id="attachedCount"></span>
      </div>
      <div id="attachedPhotos" class="photo-strip"></div>
      <div class="section-title">
        <strong>Flagged Candidates</strong>
        <span id="candidateCount"></span>
      </div>
      <div id="candidateGrid" class="candidate-grid"></div>
    </section>
  </main>
</div>
<script>
const state = {
  entries: [],
  selectedEntryId: "",
  currentEntry: null,
  candidateDays: 0,
  startDate: "",
  endDate: "",
  entryLimit: 500
};

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {"content-type": "application/json", ...(options.headers || {})},
    ...options
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

function imageSrc(item) {
  return item?.token ? `/picker/image/${encodeURIComponent(item.token)}?max=512` : "";
}

function filename(item) {
  return item?.filename || String(item?.path || "").split("/").filter(Boolean).pop() || "";
}

function peopleScript(names) {
  const values = Array.isArray(names) ? names.filter(Boolean) : [];
  if (!values.length) return "";
  const text = values.join(", ");
  return `<span class="people-script" title="${escapeHtml(text)}">${escapeHtml(text)}</span>`;
}

function applyInitialQuery() {
  const query = new URLSearchParams(window.location.search);
  state.startDate = query.get("start_date") || "";
  state.endDate = query.get("end_date") || "";
  const limit = Number(query.get("limit") || "500");
  state.entryLimit = Number.isFinite(limit) && limit > 0 ? Math.min(Math.round(limit), 1000) : 500;
}

function entriesApiUrl() {
  const params = new URLSearchParams();
  if (state.startDate) params.set("start_date", state.startDate);
  if (state.endDate) params.set("end_date", state.endDate);
  params.set("limit", String(state.entryLimit));
  return `/enrich/api/entries?${params.toString()}`;
}

async function loadEntries(preferredEntryId = "") {
  const body = await fetchJson(entriesApiUrl());
  state.entries = body.entries || [];
  const range = state.startDate || state.endDate
    ? `${state.startDate || "start"} to ${state.endDate || "end"}`
    : "No date range";
  document.getElementById("summary").textContent = body.message || `${state.entries.length} diary targets · ${range}${body.has_more ? " · limited" : ""}`;
  const selected = state.entries.find(entry => entry.entry_id === (preferredEntryId || state.selectedEntryId))
    || state.entries[0];
  state.selectedEntryId = selected?.entry_id || "";
  renderEntries();
  if (state.selectedEntryId) {
    await loadEntry(state.selectedEntryId);
  } else {
    state.currentEntry = null;
    renderEntryDetail();
  }
}

function renderEntries() {
  const list = document.getElementById("entryList");
  const filter = document.getElementById("entryFilter").value.trim().toLowerCase();
  const visible = state.entries.filter(entry => !filter || `${entry.entry_date} ${entry.entry_id}`.toLowerCase().includes(filter));
  list.innerHTML = "";
  if (!visible.length) {
    list.innerHTML = '<div class="empty">No matching entries.</div>';
    return;
  }
  for (const entry of visible) {
    const button = document.createElement("button");
    button.className = `entry-item ${entry.entry_id === state.selectedEntryId ? "active" : ""} ${entry.is_subentry ? "entry-sub" : ""}`;
    button.onclick = () => loadEntry(entry.entry_id);
    const primarySrc = imageSrc(entry.primary_photo);
    const workingCopyText = entry.primary_photo_ready ? "" : "working copy missing · ";
    const people = peopleScript(entry.people_names);
    button.innerHTML = `
      ${primarySrc ? `<img class="entry-thumb" src="${primarySrc}" alt="">` : '<div class="entry-thumb"></div>'}
      <span>
        <span class="entry-date">${escapeHtml(entry.entry_date)}</span>
        <span class="entry-meta">${workingCopyText}${entry.associated_count || 0} additional photo${entry.associated_count === 1 ? "" : "s"}</span>
        ${people}
      </span>`;
    list.appendChild(button);
  }
}

async function loadEntry(entryId) {
  state.selectedEntryId = entryId;
  renderEntries();
  state.currentEntry = await fetchJson(`/enrich/api/entry/${encodeURIComponent(entryId)}?days=${state.candidateDays}`);
  renderEntryDetail();
}

function renderEntryDetail() {
  const entry = state.currentEntry;
  document.getElementById("entryHeading").textContent = entry ? `${entry.entry_date}${entry.is_subentry ? " sub-entry" : ""}` : "No entry selected";
  document.getElementById("entryMeta").textContent = entry ? entry.entry_id : "";
  document.getElementById("entryPeople").innerHTML = entry ? peopleScript(entry.people_names) : "";
  document.querySelectorAll("[data-days]").forEach(button => {
    button.classList.toggle("active", Number(button.dataset.days) === state.candidateDays);
  });
  renderAttached(entry?.attached_photos || []);
  renderCandidates(entry?.candidates || []);
}

function renderAttached(photos) {
  document.getElementById("attachedCount").textContent = `${photos.length} attached`;
  const target = document.getElementById("attachedPhotos");
  if (!photos.length) {
    target.innerHTML = '<div class="empty">No additional photos attached yet.</div>';
    return;
  }
  target.innerHTML = photos.map(photo => `
    <div class="attached-photo">
      ${imageSrc(photo) ? `<img src="${imageSrc(photo)}" alt="">` : ""}
      <div class="photo-caption" title="${escapeHtml(photo.path)}">${escapeHtml(filename(photo))}</div>
    </div>
  `).join("");
}

function renderCandidates(candidates) {
  document.getElementById("candidateCount").textContent = `${candidates.length} candidate${candidates.length === 1 ? "" : "s"}`;
  const target = document.getElementById("candidateGrid");
  if (!candidates.length) {
    target.innerHTML = '<div class="empty">No flagged photos in this date range.</div>';
    return;
  }
  const attachedIds = new Set((state.currentEntry?.attached_photos || []).map(photo => photo.sha256));
  target.innerHTML = candidates.map(candidate => {
    const added = attachedIds.has(candidate.sha256);
    return `
      <article class="candidate-card" data-media-id="${escapeHtml(candidate.media_asset_id)}">
        ${imageSrc(candidate) ? `<img src="${imageSrc(candidate)}" alt="">` : ""}
        <div class="candidate-body">
          <div class="candidate-title" title="${escapeHtml(candidate.path)}">${escapeHtml(filename(candidate))}</div>
          <div class="candidate-meta">Photo date ${escapeHtml(candidate.candidate_date || "")}<br>Flagged from ${escapeHtml(candidate.source_entry_date || "")}</div>
          <div class="candidate-actions">
            <button class="primary ${added ? "added" : ""}" data-action="add">${added ? "Added" : "Add"}</button>
            <button data-action="subentry">New Entry</button>
          </div>
        </div>
      </article>`;
  }).join("");
  target.querySelectorAll("[data-action='add']").forEach(button => {
    button.onclick = () => addCandidate(button.closest(".candidate-card").dataset.mediaId, button);
  });
  target.querySelectorAll("[data-action='subentry']").forEach(button => {
    button.onclick = () => createSubentry(button.closest(".candidate-card").dataset.mediaId);
  });
}

async function addCandidate(mediaAssetId, button) {
  if (!state.currentEntry) return;
  button.disabled = true;
  button.textContent = "Adding...";
  state.currentEntry = await fetchJson("/enrich/api/add-associated", {
    method: "POST",
    body: JSON.stringify({
      target_entry_id: state.currentEntry.entry_id,
      source_media_asset_id: mediaAssetId
    })
  });
  renderEntryDetail();
  renderEntries();
}

async function createSubentry(mediaAssetId) {
  if (!state.currentEntry) return;
  const result = await fetchJson("/enrich/api/create-subentry", {
    method: "POST",
    body: JSON.stringify({
      parent_entry_id: state.currentEntry.entry_id,
      source_media_asset_id: mediaAssetId
    })
  });
  state.selectedEntryId = result.subentry_id;
  await loadEntries(result.subentry_id);
}

async function handleDrop(event) {
  event.preventDefault();
  document.getElementById("dropZone").classList.remove("active");
  if (!state.currentEntry) return;
  const files = [...event.dataTransfer.files].filter(file => file.type.startsWith("image/") || /\\.(jpe?g|png|gif|tiff?|heic|heif|webp|bmp|psd)$/i.test(file.name));
  if (!files.length) {
    document.getElementById("dropStatus").textContent = "Drop one or more supported image files.";
    return;
  }
  for (const file of files) {
    document.getElementById("dropStatus").textContent = `Adding ${file.name}.`;
    const response = await fetch("/enrich/api/import-dropped-associated", {
      method: "POST",
      headers: {
        "content-type": file.type || "application/octet-stream",
        "x-entry-id": encodeURIComponent(state.currentEntry.entry_id),
        "x-file-name": encodeURIComponent(file.name)
      },
      body: file
    });
    if (!response.ok) throw new Error(await response.text());
    state.currentEntry = await response.json();
  }
  document.getElementById("dropStatus").textContent = `Added ${files.length} dropped photo${files.length === 1 ? "" : "s"}.`;
  renderEntryDetail();
  await loadEntries(state.currentEntry.entry_id);
}

document.getElementById("entryFilter").oninput = renderEntries;
document.querySelectorAll("[data-days]").forEach(button => {
  button.onclick = async () => {
    state.candidateDays = Number(button.dataset.days);
    if (state.selectedEntryId) await loadEntry(state.selectedEntryId);
  };
});
const dropZone = document.getElementById("dropZone");
dropZone.addEventListener("dragenter", event => {
  event.preventDefault();
  dropZone.classList.add("active");
});
dropZone.addEventListener("dragover", event => {
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("active"));
dropZone.addEventListener("drop", event => {
  handleDrop(event).catch(error => {
    document.getElementById("dropStatus").textContent = error.message || "Drop failed.";
  });
});
applyInitialQuery();
loadEntries().catch(error => {
  document.getElementById("summary").textContent = error.message || "Failed to load.";
});
</script>
</body>
</html>
"""
