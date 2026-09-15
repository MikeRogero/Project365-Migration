#!/usr/bin/env python3
"""Fast local intake workflow for adding photos to Project365 diary entries."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Protocol

from project365_original_matcher import IMAGE_EXTENSIONS
from project365_original_picker import MAX_DROP_BYTES
from project365_original_reference_pipeline import _external_decision_id
from project365_photo_library_index import (
    _exiftool_photo_metadata,
    _filename_dates,
    _filename_timestamps,
    default_index_db,
    query_index_candidates,
)


INTAKE_SOURCE_APP = "project365_enrichment"
INTAKE_SOURCE_NAME = "diary_intake"
MAIN_ENTRY_PREFIX = "project365"
SUBENTRY_PREFIX = "sub"


class ImageTokenProvider(Protocol):
    def image_token_for_path(self, path: Path | None) -> str:
        ...


def stage_dropped_photo(
    canonical_root: Path,
    filename: str,
    content_type: str,
    payload: bytes,
    image_tokens: ImageTokenProvider | None = None,
) -> dict[str, Any]:
    safe_name, suffix = _validated_drop(filename, payload)
    digest = hashlib.sha256(payload).hexdigest()
    analysis = _analyze_payload(safe_name, suffix, payload)
    date_folder = (analysis.get("resolved_date") or "_undated")[:7]
    target_dir = canonical_root.parent / "Source Data" / "Diary Intake Photos" / date_folder
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / safe_name
    if destination.exists() and _sha256_file(destination) != digest:
        destination = target_dir / f"{destination.stem} - {digest[:12]}{suffix}"
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(destination)

    path = destination.resolve()
    return {
        **analysis,
        "intake_id": digest[:16],
        "filename": safe_name,
        "path": str(path),
        "sha256": digest,
        "byte_size": len(payload),
        "mime_type": _safe_mime_type(content_type, safe_name),
        "token": image_tokens.image_token_for_path(path) if image_tokens else "",
    }


def commit_intake_photo(
    canonical_root: Path,
    staged_path: str,
    mode: str,
    timestamp: str,
    timestamp_source: str,
    original_timestamp: str = "",
    original_timestamp_source: str = "",
    text: str = "",
    image_tokens: ImageTokenProvider | None = None,
) -> dict[str, Any]:
    mode = str(mode or "").strip()
    if mode not in {"main", "additional", "secondary"}:
        raise ValueError("Choose main, additional, or secondary.")
    applied_timestamp = _normalized_timestamp(timestamp)
    if not applied_timestamp:
        raise ValueError("Choose a valid date and time before saving.")
    entry_date = applied_timestamp[:10]
    source_path = Path(staged_path)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Missing staged photo: {source_path}")

    db_path = canonical_root / "canonical.db"
    connection = _connect(db_path)
    try:
        import_batch_id = _latest_or_create_intake_batch(connection, canonical_root)
        digest = _sha256_file(source_path)
        now = dt.datetime.now(dt.UTC).isoformat()
        if mode == "secondary":
            entry_id = f"{MAIN_ENTRY_PREFIX}:{entry_date}:{SUBENTRY_PREFIX}:{digest[:12]}"
            _upsert_entry(
                connection,
                entry_id=entry_id,
                entry_date=entry_date,
                text=text,
                now=now,
                source_app=INTAKE_SOURCE_APP,
                update_existing_text=True,
            )
            role = "external_original_reference"
            review_decision = "intake_secondary_entry"
        else:
            entry_id = f"{MAIN_ENTRY_PREFIX}:{entry_date}"
            existed = _entry_exists(connection, entry_id)
            _upsert_entry(
                connection,
                entry_id=entry_id,
                entry_date=entry_date,
                text=text,
                now=now,
                source_app=INTAKE_SOURCE_APP,
                update_existing_text=not existed,
            )
            role = "external_original_reference" if mode == "main" else "external_original_associated_photo"
            review_decision = "intake_main_photo" if mode == "main" else "intake_additional_photo"
        media_asset_id = _upsert_intake_media(
            connection=connection,
            entry_id=entry_id,
            role=role,
            source_path=source_path,
            import_batch_id=import_batch_id,
            review_decision=review_decision,
            applied_timestamp=applied_timestamp,
            timestamp_source=timestamp_source,
            original_timestamp=original_timestamp,
            original_timestamp_source=original_timestamp_source,
        )
        connection.commit()
        return {
            "entry": _entry_payload(connection, entry_id, image_tokens),
            "entry_id": entry_id,
            "entry_date": entry_date,
            "media_asset_id": media_asset_id,
            "mode": mode,
            "next_action": "Generate Diarium derivatives for this date before export.",
        }
    finally:
        connection.close()


def nearby_photos(
    canonical_root: Path,
    date: str,
    days: int = 0,
    limit: int = 80,
    image_tokens: ImageTokenProvider | None = None,
) -> dict[str, Any]:
    entry_date = _normalized_date(date)
    bounded_days = max(0, min(int(days or 0), 30))
    bounded_limit = max(1, min(int(limit or 80), 250))
    index_db = default_index_db(canonical_root)
    candidates = query_index_candidates(
        index_db,
        {entry_date},
        max_distance_days=bounded_days,
        include_filesystem_dates=True,
    ).get(entry_date, [])
    rows = []
    seen_paths: set[str] = set()
    for candidate in candidates:
        path = Path(str(candidate.get("candidate_path") or candidate.get("path") or ""))
        path_text = str(path)
        if not path_text or path_text in seen_paths:
            continue
        seen_paths.add(path_text)
        rows.append(_candidate_payload(candidate, image_tokens))
        if len(rows) >= bounded_limit:
            break
    return {"date": entry_date, "days": bounded_days, "photos": rows}


def _validated_drop(filename: str, payload: bytes) -> tuple[str, str]:
    safe_name = Path(str(filename or "").replace("\\", "/")).name
    suffix = Path(safe_name).suffix.lower()
    if not safe_name or suffix not in IMAGE_EXTENSIONS:
        raise ValueError("Drop one supported image file.")
    if not payload:
        raise ValueError("Dropped image is empty.")
    if len(payload) > MAX_DROP_BYTES:
        raise ValueError("Dropped image exceeds the 250 MB limit.")
    return safe_name, suffix


def _analyze_payload(filename: str, suffix: str, payload: bytes) -> dict[str, Any]:
    choices = []
    metadata_timestamp = ""
    metadata_source = ""
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        handle.write(payload)
        handle.flush()
        metadata = _exiftool_photo_metadata([Path(handle.name)])
        photo_metadata = metadata.get(str(Path(handle.name).resolve()))
        if photo_metadata:
            metadata_timestamp = photo_metadata.capture_timestamp
            metadata_source = photo_metadata.capture_timestamp_source
    if metadata_timestamp:
        choices.append(_timestamp_choice(metadata_timestamp, metadata_source, "Photo metadata"))

    filename_path = Path(filename)
    for value in sorted(_filename_timestamps(filename_path)):
        choices.append(_timestamp_choice(value, "filename_timestamp", "Filename time"))
    for value in sorted(_filename_dates(filename_path)):
        choices.append(_timestamp_choice(f"{value}T12:00:00", "filename_date", "Filename date at noon"))

    deduped: list[dict[str, str]] = []
    seen = set()
    for choice in choices:
        key = (choice["timestamp"], choice["source"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(choice)
    resolved = deduped[0] if deduped else {}
    return {
        "resolved_timestamp": resolved.get("timestamp", ""),
        "resolved_date": resolved.get("timestamp", "")[:10],
        "resolved_source": resolved.get("source", ""),
        "requires_manual_datetime": not bool(resolved),
        "date_choices": deduped,
    }


def _timestamp_choice(timestamp: str, source: str, label: str) -> dict[str, str]:
    return {
        "timestamp": _normalized_timestamp(timestamp),
        "source": source,
        "label": label,
    }


def _candidate_payload(candidate: dict[str, Any], image_tokens: ImageTokenProvider | None) -> dict[str, Any]:
    path = Path(str(candidate.get("candidate_path") or candidate.get("path") or ""))
    token = image_tokens.image_token_for_path(path) if image_tokens and path.exists() else ""
    return {
        "path": str(path),
        "filename": str(candidate.get("candidate_filename") or candidate.get("filename") or path.name),
        "sha256": str(candidate.get("candidate_sha256") or candidate.get("sha256") or ""),
        "byte_size": int(candidate.get("byte_size") or 0),
        "capture_timestamp": str(candidate.get("capture_timestamp") or ""),
        "capture_timestamp_source": str(candidate.get("capture_timestamp_source") or ""),
        "evidence_sources": str(candidate.get("evidence_sources") or ""),
        "date_distance": candidate.get("date_distance"),
        "token": token,
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _latest_or_create_intake_batch(connection: sqlite3.Connection, canonical_root: Path) -> str:
    row = connection.execute(
        "SELECT id FROM import_batches ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is not None and row["id"]:
        return str(row["id"])
    now = dt.datetime.now(dt.UTC).isoformat()
    batch_id = f"diary-intake:{now}"
    connection.execute(
        """
        INSERT INTO import_batches (
            id, source_type, import_dir, started_at, finished_at, status
        )
        VALUES (?, 'diary_intake', ?, ?, ?, 'completed')
        """,
        (batch_id, str(canonical_root.parent / "Source Data" / "Diary Intake Photos"), now, now),
    )
    return batch_id


def _entry_exists(connection: sqlite3.Connection, entry_id: str) -> bool:
    return connection.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone() is not None


def _upsert_entry(
    connection: sqlite3.Connection,
    entry_id: str,
    entry_date: str,
    text: str,
    now: str,
    source_app: str,
    update_existing_text: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO entries (
            id, entry_date, source_app, original_text, corrected_text, correction_status,
            import_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, NULL, 'uncorrected', 'not_exported', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            updated_at = excluded.updated_at,
            original_text = CASE
                WHEN ? THEN excluded.original_text
                ELSE entries.original_text
            END
        """,
        (entry_id, entry_date, source_app, text, now, now, int(update_existing_text)),
    )


def _upsert_intake_media(
    connection: sqlite3.Connection,
    entry_id: str,
    role: str,
    source_path: Path,
    import_batch_id: str,
    review_decision: str,
    applied_timestamp: str,
    timestamp_source: str,
    original_timestamp: str,
    original_timestamp_source: str,
) -> str:
    if role not in {"external_original_reference", "external_original_associated_photo"}:
        raise ValueError("Unsupported intake role")
    digest = _sha256_file(source_path)
    media_asset_id = _external_decision_id(entry_id, digest, role)
    transformation = {
        "source": INTAKE_SOURCE_NAME,
        "source_path": str(source_path),
        "review_decision": review_decision,
        "original_is_read_only": True,
        "capture_timestamp": applied_timestamp,
        "capture_timestamp_source": timestamp_source or "manual",
    }
    if original_timestamp:
        transformation["original_capture_timestamp"] = original_timestamp
    if original_timestamp_source:
        transformation["original_capture_timestamp_source"] = original_timestamp_source
    if role == "external_original_associated_photo":
        transformation["associated_entry_date"] = applied_timestamp[:10]
        transformation["associated_date_source"] = timestamp_source or "manual"
    now = dt.datetime.now(dt.UTC).isoformat()
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
            import_batch_id = excluded.import_batch_id,
            updated_at = excluded.updated_at
        """,
        (
            media_asset_id,
            entry_id,
            role,
            source_path.name,
            str(source_path),
            digest,
            source_path.stat().st_size,
            mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
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
            (entry_id, media_asset_id),
        )
    return media_asset_id


def _entry_payload(
    connection: sqlite3.Connection,
    entry_id: str,
    image_tokens: ImageTokenProvider | None,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT id, entry_date, source_app, original_text FROM entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    media_rows = connection.execute(
        """
        SELECT *
        FROM media_assets
        WHERE entry_id = ?
            AND role IN ('external_original_reference', 'external_original_associated_photo')
            AND status = 'available'
        ORDER BY role != 'external_original_reference', updated_at, id
        """,
        (entry_id,),
    ).fetchall()
    return {
        "entry_id": str(row["id"]),
        "entry_date": str(row["entry_date"]),
        "source_app": str(row["source_app"]),
        "text_present": bool(str(row["original_text"] or "")),
        "media": [_media_payload(media, image_tokens) for media in media_rows],
    }


def _media_payload(row: sqlite3.Row, image_tokens: ImageTokenProvider | None) -> dict[str, Any]:
    path = Path(str(row["storage_path"] or ""))
    token = image_tokens.image_token_for_path(path) if image_tokens and path.exists() else ""
    transformation = _json_object(str(row["transformation_json"] or ""))
    return {
        "media_asset_id": str(row["id"]),
        "role": str(row["role"]),
        "filename": str(row["internal_filename"] or path.name),
        "path": str(path),
        "sha256": str(row["sha256"] or ""),
        "token": token,
        "capture_timestamp": str(transformation.get("capture_timestamp") or ""),
        "capture_timestamp_source": str(transformation.get("capture_timestamp_source") or ""),
    }


def _normalized_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 10:
        text = f"{text}T12:00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.second == 0 and "." not in text and len(text) <= 16:
        return parsed.replace(second=0).isoformat(timespec="seconds")
    return parsed.isoformat()


def _normalized_date(value: str) -> str:
    try:
        return dt.date.fromisoformat(str(value or "").strip()).isoformat()
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def _safe_mime_type(content_type: str, filename: str) -> str:
    if str(content_type or "").startswith("image/"):
        return str(content_type)
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


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


INTAKE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 Diary Intake</title>
<style>
:root {
  --bg: #f5f6f8;
  --panel: #ffffff;
  --ink: #202124;
  --muted: #667085;
  --line: #d7dde5;
  --accent: #17695d;
  --accent-soft: #e5f2ef;
  --warn: #9a5b00;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--ink);
}
button, input, textarea, select { font: inherit; }
button {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 8px 10px;
  cursor: pointer;
}
button.primary { border-color: var(--accent); background: var(--accent); color: #fff; }
button:disabled { opacity: .55; cursor: default; }
.app { display: grid; grid-template-columns: 300px minmax(0, 1fr) 320px; height: 100vh; }
.rail, .side { background: var(--panel); border-right: 1px solid var(--line); min-height: 0; display: flex; flex-direction: column; }
.side { border-right: 0; border-left: 1px solid var(--line); }
.top { padding: 14px; border-bottom: 1px solid var(--line); display: grid; gap: 8px; }
.back { color: var(--accent); text-decoration: none; font-size: 13px; }
.title { font-size: 17px; font-weight: 700; }
.hint, .meta, .status { color: var(--muted); font-size: 12px; line-height: 1.35; }
.drop {
  margin: 12px;
  min-height: 100px;
  border: 1px dashed #aab4c2;
  border-radius: 6px;
  background: #fff;
  display: grid;
  place-content: center;
  text-align: center;
  padding: 14px;
  gap: 5px;
}
.drop.active { border-color: var(--accent); background: var(--accent-soft); }
.list { overflow: auto; padding: 8px; display: grid; gap: 7px; }
.item {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  gap: 8px;
  width: 100%;
  text-align: left;
  align-items: center;
}
.item.active { border-color: var(--accent); background: var(--accent-soft); }
.thumb { width: 52px; height: 52px; object-fit: cover; background: #edf1f4; border: 1px solid var(--line); border-radius: 4px; }
.main { min-width: 0; min-height: 0; display: grid; grid-template-rows: auto minmax(0, 1fr); }
.bar { background: var(--panel); border-bottom: 1px solid var(--line); padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.workspace { overflow: auto; padding: 16px; display: grid; gap: 14px; align-content: start; }
.preview { max-width: min(720px, 100%); background: #fff; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.preview img { width: 100%; max-height: 52vh; object-fit: contain; display: block; background: #eef1f4; }
.panel { display: grid; gap: 10px; max-width: 720px; }
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.field { display: grid; gap: 5px; }
.field label { color: var(--muted); font-size: 12px; }
.field input, .field select, textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 8px 10px;
}
textarea { min-height: 150px; resize: vertical; }
.choices, .actions { display: flex; gap: 8px; flex-wrap: wrap; }
.choice { font-size: 12px; }
.choice.warn { border-color: var(--warn); color: var(--warn); }
.photo-grid { overflow: auto; padding: 10px; display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(126px, 1fr)); }
.nearby img { width: 100%; aspect-ratio: 1 / 1; object-fit: cover; background: #edf1f4; border: 1px solid var(--line); border-radius: 4px; }
.nearby .meta { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
@media (max-width: 980px) {
  .app { grid-template-columns: 1fr; grid-template-rows: 220px minmax(0, 1fr) 260px; }
  .rail, .side { border: 0; border-bottom: 1px solid var(--line); }
  .fields { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="top">
      <a class="back" href="/">Project365 Control Panel</a>
      <div class="title">Diary Intake</div>
      <div id="summary" class="hint">Drop photos from Finder or Apple Photos.</div>
    </div>
    <div id="dropZone" class="drop">
      <strong>Drop photos here</strong>
      <span class="hint">Single photos or batches</span>
    </div>
    <div id="intakeList" class="list"></div>
  </aside>
  <main class="main">
    <header class="bar">
      <div>
        <strong id="heading">No photo selected</strong>
        <div id="subheading" class="meta"></div>
      </div>
      <button id="nearbyButton">Find Nearby</button>
    </header>
    <section class="workspace">
      <div id="preview" class="preview"></div>
      <div class="panel">
        <div id="choices" class="choices"></div>
        <div class="fields">
          <div class="field">
            <label>Date and time</label>
            <input id="timestampInput" type="datetime-local">
          </div>
          <div class="field">
            <label>Save as</label>
            <select id="modeInput">
              <option value="secondary">New secondary entry</option>
              <option value="additional">Additional photo on day</option>
              <option value="main">Main day photo</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>Entry text</label>
          <textarea id="textInput" placeholder="Optional text for a new secondary entry"></textarea>
        </div>
        <div class="actions">
          <button id="saveButton" class="primary">Save to Diary</button>
          <span id="saveStatus" class="status"></span>
        </div>
      </div>
    </section>
  </main>
  <aside class="side">
    <div class="top">
      <div class="title">Nearby Photos</div>
      <div id="nearbySummary" class="hint">Use a selected photo date, then search.</div>
      <div class="choices">
        <button data-days="0">Same day</button>
        <button data-days="1">+/- 1</button>
        <button data-days="5">+/- 5</button>
      </div>
    </div>
    <div id="nearbyGrid" class="photo-grid"></div>
  </aside>
</div>
<script>
const state = {items: [], selected: -1, nearbyDays: 0};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}
function imageSrc(item) {
  return item?.token ? `/picker/image/${encodeURIComponent(item.token)}?max=768` : "";
}
function localInputValue(timestamp) {
  if (!timestamp) return "";
  return timestamp.slice(0, 16);
}
function selectedItem() {
  return state.items[state.selected] || null;
}
async function fetchJson(url, options = {}) {
  const response = await fetch(url, {headers: {"content-type": "application/json", ...(options.headers || {})}, ...options});
  if (!response.ok) throw new Error(await response.text() || response.statusText);
  return response.json();
}
async function stageFile(file) {
  const response = await fetch("/intake/api/stage", {
    method: "POST",
    headers: {
      "content-type": file.type || "application/octet-stream",
      "x-file-name": encodeURIComponent(file.name)
    },
    body: file
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
function renderList() {
  document.getElementById("summary").textContent = `${state.items.length} staged photo${state.items.length === 1 ? "" : "s"}`;
  const target = document.getElementById("intakeList");
  if (!state.items.length) {
    target.innerHTML = "";
    return;
  }
  target.innerHTML = state.items.map((item, index) => `
    <button class="item ${index === state.selected ? "active" : ""}" data-index="${index}">
      ${imageSrc(item) ? `<img class="thumb" src="${imageSrc(item)}" alt="">` : '<div class="thumb"></div>'}
      <span>
        <strong>${escapeHtml(item.filename)}</strong>
        <span class="meta">${escapeHtml(item.resolved_timestamp || "Needs date")}</span>
      </span>
    </button>
  `).join("");
  target.querySelectorAll("[data-index]").forEach(button => {
    button.onclick = () => selectItem(Number(button.dataset.index));
  });
}
function selectItem(index) {
  state.selected = index;
  renderList();
  renderDetail();
}
function renderDetail() {
  const item = selectedItem();
  document.getElementById("heading").textContent = item ? item.filename : "No photo selected";
  document.getElementById("subheading").textContent = item ? item.path : "";
  document.getElementById("preview").innerHTML = item && imageSrc(item) ? `<img src="${imageSrc(item)}" alt="">` : "";
  document.getElementById("timestampInput").value = item ? localInputValue(item.resolved_timestamp) : "";
  document.getElementById("choices").innerHTML = item ? item.date_choices.map(choice => `
    <button class="choice" data-timestamp="${escapeHtml(choice.timestamp)}" data-source="${escapeHtml(choice.source)}">${escapeHtml(choice.label)} · ${escapeHtml(choice.timestamp)}</button>
  `).join("") || '<button class="choice warn" disabled>Manual date needed</button>' : "";
  document.querySelectorAll("#choices [data-timestamp]").forEach(button => {
    button.onclick = () => {
      document.getElementById("timestampInput").value = localInputValue(button.dataset.timestamp);
      item.resolved_source = button.dataset.source;
    };
  });
}
async function handleDrop(event) {
  event.preventDefault();
  dropZone.classList.remove("active");
  const files = [...event.dataTransfer.files].filter(file => file.type.startsWith("image/") || /\\.(jpe?g|png|heic|heif|tiff?)$/i.test(file.name));
  for (const file of files) {
    document.getElementById("summary").textContent = `Reading ${file.name}`;
    state.items.push(await stageFile(file));
    if (state.selected < 0) state.selected = 0;
    renderList();
    renderDetail();
  }
}
async function saveSelected() {
  const item = selectedItem();
  if (!item) return;
  const timestamp = document.getElementById("timestampInput").value;
  const status = document.getElementById("saveStatus");
  status.textContent = "Saving...";
  const result = await fetchJson("/intake/api/commit", {
    method: "POST",
    body: JSON.stringify({
      staged_path: item.path,
      mode: document.getElementById("modeInput").value,
      timestamp,
      timestamp_source: item.resolved_source || "manual",
      original_timestamp: item.resolved_timestamp || "",
      original_timestamp_source: item.resolved_source || "",
      text: document.getElementById("textInput").value
    })
  });
  status.textContent = `Saved ${result.entry_id}`;
}
async function findNearby() {
  const item = selectedItem();
  const value = document.getElementById("timestampInput").value || item?.resolved_timestamp || "";
  const date = value.slice(0, 10);
  if (!date) return;
  const body = await fetchJson(`/intake/api/nearby?date=${encodeURIComponent(date)}&days=${state.nearbyDays}`);
  document.getElementById("nearbySummary").textContent = `${body.photos.length} nearby photo${body.photos.length === 1 ? "" : "s"} for ${body.date}`;
  document.getElementById("nearbyGrid").innerHTML = body.photos.map(photo => `
    <div class="nearby" title="${escapeHtml(photo.path)}">
      ${photo.token ? `<img src="/picker/image/${encodeURIComponent(photo.token)}?max=384" alt="">` : ""}
      <div class="meta">${escapeHtml(photo.filename)}</div>
      <div class="meta">${escapeHtml(photo.capture_timestamp || photo.evidence_sources || "")}</div>
    </div>
  `).join("") || '<div class="hint">No nearby indexed photos.</div>';
}
const dropZone = document.getElementById("dropZone");
dropZone.addEventListener("dragenter", event => { event.preventDefault(); dropZone.classList.add("active"); });
dropZone.addEventListener("dragover", event => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("active"));
dropZone.addEventListener("drop", event => handleDrop(event).catch(error => {
  document.getElementById("summary").textContent = error.message || "Drop failed.";
}));
document.getElementById("saveButton").onclick = () => saveSelected().catch(error => {
  document.getElementById("saveStatus").textContent = error.message || "Save failed.";
});
document.getElementById("nearbyButton").onclick = () => findNearby().catch(error => {
  document.getElementById("nearbySummary").textContent = error.message || "Search failed.";
});
document.querySelectorAll("[data-days]").forEach(button => {
  button.onclick = () => { state.nearbyDays = Number(button.dataset.days); findNearby(); };
});
renderList();
renderDetail();
</script>
</body>
</html>
"""
