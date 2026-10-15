#!/usr/bin/env python3
"""Build a privacy-preserving Day One JSON ZIP from a Swarm data export.

Confirmed check-ins, tips, owner-authored comments, and locally exported photos
are converted. Source files are read-only. Automatic/unconfirmed visits are
counted in the manifest but are not turned into diary entries.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote, urlparse


# Version 4+ deliberately uses new deterministic identifiers so entries with
# recovered check-in People tags cannot collide with earlier imports.
NAMESPACE = uuid.UUID("44d500b5-978d-4177-bea6-68b223b01e0a")
BACKFILL_NAMESPACE = uuid.UUID("79dda6de-dbc6-441b-9e72-54caf663e4a6")
WORKFLOW_NAMESPACE = uuid.UUID("e50d34b2-247c-49fd-a76d-b1b47502c571")
DAYONE_MEDIA_LIMIT = 30
PHOTO_EXTENSIONS = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".heic": "heic"}
VISUAL_DUPLICATE_WINDOW_SECONDS = 120
VISUAL_DUPLICATE_PHASH_MAX = 2.0
REVIEW_SAMPLE_WEIGHTS = (
    ("checkin-visual-duplicate", 4),
    ("checkin-missing-venue", 3),
    ("checkin-multi-photo", 18),
    ("checkin-single-photo", 18),
    ("checkin-no-photo-comment-count", 10),
    ("checkin-no-photo-other", 12),
    ("tip-photo-location", 4),
    ("tip-no-photo-location", 2),
    ("tip-unresolved-location", 9),
    ("venue-photo", 8),
    ("comment", 12),
)
PEOPLE_MAPPING_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Photo:
    source_id: str
    created_utc: dt.datetime
    canonical_url: str
    related_kind: str
    related_id: str
    path: Path
    width: int | None
    height: int | None


@dataclass
class Entry:
    source_key: str
    kind: str
    created_utc: dt.datetime
    offset_minutes: int
    title: str
    body: str
    latitude: float | None
    longitude: float | None
    venue_id: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    photos: list[Photo] = field(default_factory=list)
    location_source: str = "none"
    timezone_source: str = "source"
    visual_duplicates_merged: int = 0
    reported_comment_count: int = 0
    representative_photo_source: str = ""
    representative_photo_note: str = ""
    representative_photo_links: list[tuple[str, str]] = field(default_factory=list)
    people_tags: list[str] = field(default_factory=list)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _export_root(source: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Swarm source folder does not exist: {source}")
    if list(source.glob("checkins*.json")) and (source / "pix").is_dir():
        return source
    candidates = [
        child for child in source.iterdir()
        if child.is_dir() and list(child.glob("checkins*.json")) and (child / "pix").is_dir()
    ]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one Swarm export folder containing checkins JSON and pix/")
    return candidates[0].resolve()


def _chunk_number(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)$", path.stem)
    return (int(match.group(1)) if match else 0, path.name)


def _records(root: Path, pattern: str, *, required: bool = False) -> list[dict[str, Any]]:
    paths = sorted(root.glob(pattern), key=_chunk_number)
    if required and not paths:
        raise ValueError(f"Swarm export has no {pattern}")
    records: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = _load_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError(f"Invalid Swarm record collection: {path.name}")
        for record in payload["items"]:
            if not isinstance(record, dict):
                raise ValueError(f"Invalid Swarm item in {path.name}")
            source_id = _required_text(record.get("id"), f"{path.name} item id")
            previous = seen.get(source_id)
            if previous is not None:
                if previous != record:
                    raise ValueError(f"Conflicting duplicate Swarm id in {path.name}")
                continue
            seen[source_id] = record
            records.append(record)
    return records


def _single_items(root: Path, name: str) -> list[dict[str, Any]]:
    path = root / name
    if not path.is_file():
        return []
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError(f"Invalid Swarm record collection: {name}")
    if not all(isinstance(item, dict) for item in payload["items"]):
        raise ValueError(f"Invalid Swarm item in {name}")
    return payload["items"]


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {field}")
    return value.strip()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _checkin_person_entities(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Return stable identity and display name for explicit ``with Person`` entities."""
    shout = record.get("shout")
    entities = record.get("entities")
    if not isinstance(shout, str) or not isinstance(entities, list):
        return []
    people: list[tuple[str, str]] = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("type") not in {"user", "facebookUser"}:
            continue
        indices = entity.get("indices")
        if (not isinstance(indices, list) or len(indices) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in indices)):
            continue
        start, end = indices
        if not 0 <= start < end <= len(shout):
            continue
        if re.search(r"(?:^|\s)(?:with|and)\s*$", shout[:start], re.IGNORECASE) is None:
            continue
        source_name = re.sub(r"\s+", " ", shout[start:end]).strip(" @")
        source_id = _text(entity.get("id"))
        if source_id and source_name:
            people.append((f"{entity['type']}:{source_id}", source_name))
    return list(dict.fromkeys(people))


def _normalize_people_name(value: str) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    if clean.lower().startswith("person:"):
        clean = clean.split(":", 1)[1].strip()
    parts = [part.strip() for part in re.split(r"[/|\\]", clean) if part.strip()]
    if len(parts) >= 2 and parts[0].lower() in {"people", "person", "persons"}:
        clean = parts[-1]
    return clean


def _load_people_mapping_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = _load_json(path)
    if (not isinstance(payload, dict)
            or payload.get("schema_version") != PEOPLE_MAPPING_SCHEMA_VERSION
            or not isinstance(payload.get("mappings"), dict)):
        raise ValueError("Invalid Swarm people mapping file")
    records: dict[str, dict[str, Any]] = {}
    for identity, record in payload["mappings"].items():
        if (not isinstance(identity, str) or not identity.strip() or ":" not in identity
                or not isinstance(record, dict)):
            raise ValueError("Invalid Swarm people mapping record")
        source_names = record.get("swarm_names")
        canonical_name = record.get("digikam_name")
        if (not isinstance(source_names, list) or not source_names
                or not all(isinstance(name, str) and name.strip() for name in source_names)
                or not isinstance(canonical_name, str) or not canonical_name.strip()):
            raise ValueError("Invalid Swarm people mapping record")
        normalized_name = _normalize_people_name(canonical_name)
        if not normalized_name:
            raise ValueError("Invalid Swarm people mapping record")
        records[identity] = {
            "swarm_names": sorted(set(name.strip() for name in source_names)),
            "digikam_name": normalized_name,
        }
    return records


def _save_people_mapping_records(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PEOPLE_MAPPING_SCHEMA_VERSION,
        "mappings": {identity: records[identity] for identity in sorted(records)},
    }
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, encoding="utf-8", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        json.dump(payload, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _resolve_people_mappings(
    root: Path,
    mapping_path: Path,
    prompt: Callable[[str], str] | None,
) -> dict[str, str]:
    root = _export_root(root)
    mapping_path = mapping_path.expanduser().resolve()
    if mapping_path == root or mapping_path.is_relative_to(root):
        raise ValueError("People mapping file must be outside the Swarm export")
    records = _load_people_mapping_records(mapping_path)
    for record in _records(root, "checkins*.json", required=True):
        for identity, source_name in _checkin_person_entities(record):
            existing = records.get(identity)
            if existing is not None:
                if source_name not in existing["swarm_names"]:
                    existing["swarm_names"] = sorted({*existing["swarm_names"], source_name})
                    _save_people_mapping_records(mapping_path, records)
                continue
            canonical_name = ""
            if not canonical_name:
                if prompt is None:
                    raise ValueError(
                        f'Swarm companion "{source_name}" requires interactive name mapping; '
                        "run swarm_dayone_importer.py in a terminal"
                    )
                message = (
                    f'Swarm companion "{source_name}" is not mapped. '
                    "Enter the matching digiKam People name: "
                )
                while not canonical_name:
                    try:
                        canonical_name = _normalize_people_name(prompt(message))
                    except EOFError as error:
                        raise ValueError(
                            f'Swarm companion "{source_name}" requires interactive name mapping'
                        ) from error
            records[identity] = {
                "swarm_names": [source_name],
                "digikam_name": canonical_name,
            }
            _save_people_mapping_records(mapping_path, records)
    return {
        identity: str(record["digikam_name"])
        for identity, record in records.items()
    }


def _checkin_people_tags(
    record: dict[str, Any],
    people_mappings: dict[str, str] | None = None,
) -> list[str]:
    """Map explicit companions only when their canonical People identity is known."""
    mappings = people_mappings or {}
    tags: list[str] = []
    for identity, source_name in _checkin_person_entities(record):
        canonical_name = _canonical_person_name(identity, mappings)
        if canonical_name:
            tags.append(f"person:{canonical_name}")
    return list(dict.fromkeys(tags))


def _canonical_person_name(
    identity: str,
    people_mappings: dict[str, str],
) -> str:
    return people_mappings.get(identity, "")


def _source_utc_datetime(value: Any, field: str) -> dt.datetime:
    text = _required_text(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid {field}") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _offset_minutes(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValueError(f"Invalid {field}")
    offset = int(value)
    if not -14 * 60 <= offset <= 14 * 60:
        raise ValueError(f"Invalid {field}")
    return offset


def _coordinate(value: Any, minimum: float, maximum: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid {field}")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"Invalid {field}")
    return number


def _web_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if any(character.isspace() or character in "<>" for character in value):
        return ""
    return value


def _related_target(value: Any) -> tuple[str, str]:
    url = _web_url(value)
    if not url:
        raise ValueError("Invalid photo relatedItemUrl")
    parts = [part for part in urlparse(url).path.split("/") if part]
    for kind in ("checkin", "item", "v"):
        if kind in parts:
            index = parts.index(kind)
            if index + 1 < len(parts) and parts[index + 1]:
                return kind, unquote(parts[index + 1])
    raise ValueError("Unsupported photo relatedItemUrl")


def _photo_path(root: Path, full_url: Any) -> Path:
    url = _web_url(full_url)
    if not url:
        raise ValueError("Invalid photo fullUrl")
    filename = unquote(PurePosixPath(urlparse(url).path).name)
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError("Unsafe photo filename")
    path = (root / "pix" / filename).resolve()
    pix = (root / "pix").resolve()
    if path.parent != pix:
        raise ValueError("Unsafe photo path")
    if not path.is_file():
        raise ValueError(f"Missing local photo for Swarm photo record: {filename}")
    if path.suffix.lower() not in PHOTO_EXTENSIONS:
        raise ValueError(f"Unsupported Swarm photo type: {path.suffix.lower() or '(none)'}")
    return path


def _dimension(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) <= 0:
        return None
    return int(value)


def _utc_date(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamp is missing timezone information")
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_date(value: dt.datetime, offset_minutes: int) -> dt.date:
    return (value.astimezone(dt.UTC) + dt.timedelta(minutes=offset_minutes)).date()


def _timezone_name(offset_minutes: int) -> str:
    if offset_minutes == 0:
        return "Etc/UTC"
    if offset_minutes % 60 == 0:
        hours = abs(offset_minutes) // 60
        return f"Etc/GMT{'-' if offset_minutes > 0 else '+'}{hours}"
    if offset_minutes == 330:
        return "Asia/Kolkata"
    raise ValueError(f"No safe fixed Day One timezone mapping for UTC offset {offset_minutes} minutes")


def _nearest_entry(entries: list[Entry], created: dt.datetime) -> Entry | None:
    return min(entries, key=lambda entry: abs((entry.created_utc - created).total_seconds()), default=None)


def _visual_distance(first: Photo, second: Photo) -> float | None:
    if first.path.stat().st_size == second.path.stat().st_size:
        if _md5(first.path) == _md5(second.path):
            return 0.0
    executable = shutil.which("magick")
    if executable is None and Path("/opt/homebrew/bin/magick").is_file():
        executable = "/opt/homebrew/bin/magick"
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "compare", "-metric", "PHASH", str(first.path), str(second.path), "null:"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in {0, 1}:
        return None
    match = re.search(r"(?:^|\s)(\d+(?:\.\d+)?)", result.stderr or result.stdout)
    return float(match.group(1)) if match else None


def _preferred_photo(first: Photo, second: Photo) -> Photo:
    def quality(photo: Photo) -> tuple[int, int, int, str]:
        pixels = (photo.width or 0) * (photo.height or 0)
        return (-pixels, -(photo.related_kind == "checkin"), -photo.path.stat().st_size,
                photo.source_id)

    return min((first, second), key=quality)


def _markdown_title(value: str) -> str:
    return re.sub(r"([\\*_\[\]<>])", r"\\\1", value)


def _markdown_link(label: str, url: str) -> str:
    safe_label = label.replace("\\", "\\\\").replace("]", "\\]")
    safe_url = url.replace("(", "%28").replace(")", "%29")
    return f"[{safe_label}]({safe_url})"


def _entry_text(entry: Entry, identifiers: list[str]) -> str:
    parts = [f"**{_markdown_title(entry.title)}**"]
    if entry.body:
        parts.append(entry.body)
    if identifiers:
        parts.append("\n".join(f"![](dayone-moment://{identifier})" for identifier in identifiers))
    if entry.representative_photo_note:
        note = entry.representative_photo_note
        if entry.representative_photo_links:
            credits = " · ".join(
                _markdown_link(label, url) for label, url in entry.representative_photo_links
            )
            note = f"{note}\n\n{credits}"
        parts.append(f"_{note}_")
    if entry.links:
        links = " · ".join(_markdown_link(label, url) for label, url in entry.links)
        parts.append(f"---\n_Source: {links}_")
    return "\n\n".join(parts)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reported_comment_count(record: dict[str, Any]) -> int:
    comments = record.get("comments")
    value = comments.get("count") if isinstance(comments, dict) else 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < 0:
        raise ValueError("Invalid check-in comment count")
    return int(value)


def _self_user_id(root: Path) -> str:
    path = root / "users.json"
    if not path.is_file():
        raise ValueError("Swarm comments require users.json to identify their author")
    payload = _load_json(path)
    self_record = payload.get("self") if isinstance(payload, dict) else None
    if not isinstance(self_record, dict):
        raise ValueError("Swarm users.json has no self record")
    value = self_record.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError("Swarm users.json has no valid self id")
    return str(value).strip()


def _source_counts(root: Path, checkins: list[dict[str, Any]], photos: list[Photo],
                   tips: list[dict[str, Any]], comments: list[dict[str, Any]]) -> dict[str, int]:
    ratings = _load_json(root / "venueRatings.json") if (root / "venueRatings.json").is_file() else {}
    rating_count = sum(len(ratings.get(key, [])) for key in ("venueLikes", "venueOkays", "venueDislikes")) if isinstance(ratings, dict) else 0
    return {
        "checkins": len(checkins),
        "photos": len(photos),
        "tips": len(tips),
        "comment_records": len(comments),
        "nonempty_comment_records": sum(bool(_text(record.get("comment"))) for record in comments),
        "empty_comment_records": sum(not bool(_text(record.get("comment"))) for record in comments),
        "checkins_with_comment_count": sum(
            _reported_comment_count(record) > 0
            for record in checkins
        ),
        "comment_bodies_not_exported": sum(
            _reported_comment_count(record)
            for record in checkins
        ),
        "unconfirmed_visits": len(_single_items(root, "unconfirmed_visits.json")),
        "automatic_visits": len(_single_items(root, "visits.json")),
        "venue_ratings_without_dates": rating_count,
        "lists": len(_single_items(root, "lists.json")),
        "plans": len(_single_items(root, "plans.json")),
        "shares": len(_single_items(root, "shares.json")),
        "expertise_records": len(_single_items(root, "expertise.json")),
    }


def _build_entries(
    root: Path,
    people_mappings: dict[str, str] | None = None,
) -> tuple[list[Entry], list[Photo], dict[str, int], dict[str, int]]:
    checkins = _records(root, "checkins*.json", required=True)
    photo_records = _records(root, "photos*.json")
    tips = _single_items(root, "tips.json")
    comments = _single_items(root, "comments.json")

    photos: list[Photo] = []
    for record in photo_records:
        kind, related_id = _related_target(record.get("relatedItemUrl"))
        photos.append(Photo(
            source_id=_required_text(record.get("id"), "photo id"),
            created_utc=_source_utc_datetime(record.get("createdAt"), "photo createdAt"),
            canonical_url=_web_url(record.get("canonicalUrl")),
            related_kind=kind,
            related_id=related_id,
            path=_photo_path(root, record.get("fullUrl")),
            width=_dimension(record.get("width")),
            height=_dimension(record.get("height")),
        ))

    entries: list[Entry] = []
    checkin_by_id: dict[str, Entry] = {}
    checkins_by_venue: dict[str, list[Entry]] = defaultdict(list)
    unresolved_people_identities: set[str] = set()
    missing_venue = 0
    for record in checkins:
        source_id = _required_text(record.get("id"), "check-in id")
        venue = record.get("venue") if isinstance(record.get("venue"), dict) else {}
        venue_id = _text(venue.get("id"))
        venue_name = _text(venue.get("name"))
        missing_venue += not bool(venue_name)
        links: list[tuple[str, str]] = []
        if url := _web_url(record.get("canonicalUrl")):
            links.append(("Swarm check-in", url))
        if url := _web_url(venue.get("url")):
            links.append(("Foursquare venue", url))
        body = _text(record.get("shout"))
        for identity, source_name in _checkin_person_entities(record):
            if not _canonical_person_name(identity, people_mappings or {}):
                unresolved_people_identities.add(identity)
        reported_comments = _reported_comment_count(record)
        if reported_comments:
            missing = (
                f"_Swarm reports {reported_comments} comment"
                f"{'s' if reported_comments != 1 else ''} on this check-in, but did not include "
                "their authors or text in the export._"
            )
            body = f"{body}\n\n{missing}" if body else missing
        entry = Entry(
            source_key=f"checkin:{source_id}",
            kind="checkin",
            created_utc=_source_utc_datetime(record.get("createdAt"), "check-in createdAt"),
            offset_minutes=_offset_minutes(record.get("timeZoneOffset"), "check-in timeZoneOffset"),
            title=venue_name or "Swarm check-in",
            body=body,
            latitude=_coordinate(record.get("lat"), -90, 90, "check-in latitude"),
            longitude=_coordinate(record.get("lng"), -180, 180, "check-in longitude"),
            venue_id=venue_id,
            links=links,
            location_source="checkin",
            reported_comment_count=reported_comments,
            people_tags=_checkin_people_tags(record, people_mappings),
        )
        entries.append(entry)
        checkin_by_id[source_id] = entry
        if venue_id:
            checkins_by_venue[venue_id].append(entry)

    tip_by_id: dict[str, Entry] = {}
    tip_locations_from_checkins = 0
    unresolved_tip_locations = 0
    estimated_tip_timezones = 0
    for record in tips:
        source_id = _required_text(record.get("id"), "tip id")
        created = _source_utc_datetime(record.get("createdAt"), "tip createdAt")
        venue = record.get("venue") if isinstance(record.get("venue"), dict) else {}
        venue_id = _text(venue.get("id"))
        venue_name = _text(venue.get("name")) or "Foursquare tip"
        evidence = _nearest_entry(checkins_by_venue.get(venue_id, []), created)
        timezone_evidence = evidence or _nearest_entry(list(checkin_by_id.values()), created)
        if timezone_evidence is None:
            raise ValueError("Cannot recover a timestamp offset for a Swarm tip")
        links: list[tuple[str, str]] = []
        if url := _web_url(record.get("canonicalUrl")):
            links.append(("Foursquare tip", url))
        if url := _web_url(venue.get("url")):
            links.append(("Foursquare venue", url))
        entry = Entry(
            source_key=f"tip:{source_id}",
            kind="tip",
            created_utc=created,
            offset_minutes=timezone_evidence.offset_minutes,
            title=f"Tip for {venue_name}",
            body=_text(record.get("text")),
            latitude=evidence.latitude if evidence else None,
            longitude=evidence.longitude if evidence else None,
            venue_id=venue_id,
            links=links,
            location_source="nearest-checkin-same-venue" if evidence else "none",
            timezone_source="nearest-checkin-same-venue" if evidence else "nearest-checkin-by-time",
        )
        tip_locations_from_checkins += evidence is not None
        unresolved_tip_locations += evidence is None
        estimated_tip_timezones += timezone_evidence is not evidence
        entries.append(entry)
        tip_by_id[source_id] = entry

    exported_comment_entries = 0
    empty_comment_records = 0
    foreign_comment_records = 0
    estimated_comment_timezones = 0
    if comments:
        self_id = _self_user_id(root)
        occurrence: dict[str, int] = defaultdict(int)
        sorted_comments = sorted(
            comments,
            key=lambda record: (
                _required_text(record.get("time"), "comment time"),
                str(record.get("userId", "")),
                _text(record.get("comment")),
            ),
        )
        for record in sorted_comments:
            body = _text(record.get("comment"))
            if not body:
                empty_comment_records += 1
                continue
            created = _source_utc_datetime(record.get("time"), "comment time")
            timezone_evidence = _nearest_entry(list(checkin_by_id.values()), created)
            if timezone_evidence is None:
                raise ValueError("Cannot recover a timestamp offset for a Swarm comment")
            author_is_self = str(record.get("userId", "")).strip() == self_id
            foreign_comment_records += not author_is_self
            canonical = json.dumps({
                "userId": str(record.get("userId", "")),
                "time": _required_text(record.get("time"), "comment time"),
                "comment": body,
            }, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            ordinal = occurrence[digest]
            occurrence[digest] += 1
            entries.append(Entry(
                source_key=f"comment:{digest}:{ordinal}",
                kind="comment",
                created_utc=created,
                offset_minutes=timezone_evidence.offset_minutes,
                title="My Swarm comment" if author_is_self else "Swarm comment",
                body=(f"{body}\n\n"
                      "_Original check-in was not identified by the Swarm export._"),
                latitude=None,
                longitude=None,
                location_source="none",
                timezone_source="nearest-checkin-by-time",
            ))
            exported_comment_entries += 1
            estimated_comment_timezones += 1

    assigned: set[str] = set()
    suppressed: set[str] = set()
    venue_photo_candidates: list[Photo] = []
    standalone: dict[tuple[str, dt.date], list[Photo]] = defaultdict(list)
    explicit_checkin_photos = 0
    explicit_tip_photos = 0
    venue_photos = 0
    for photo in photos:
        target: Entry | None = None
        if photo.related_kind == "checkin":
            target = checkin_by_id.get(photo.related_id)
            if target is None:
                raise ValueError("Photo references a check-in absent from the export")
            explicit_checkin_photos += 1
        elif photo.related_kind == "item":
            target = tip_by_id.get(photo.related_id)
            if target is None:
                raise ValueError("Photo references a tip absent from the export")
            explicit_tip_photos += 1
        elif photo.related_kind == "v":
            venue_photos += 1
            venue_photo_candidates.append(photo)
        if target is not None:
            target.photos.append(photo)
            assigned.add(photo.source_id)

    visually_merged_venue_photos = 0
    visual_compare_failures = 0
    for photo in venue_photo_candidates:
        candidates: list[tuple[float, float, Entry, Photo]] = []
        for checkin in checkins_by_venue.get(photo.related_id, []):
            checkin_delta = abs((checkin.created_utc - photo.created_utc).total_seconds())
            if checkin_delta > VISUAL_DUPLICATE_WINDOW_SECONDS:
                continue
            for explicit in checkin.photos:
                if explicit.related_kind != "checkin":
                    continue
                photo_delta = abs((explicit.created_utc - photo.created_utc).total_seconds())
                if photo_delta > VISUAL_DUPLICATE_WINDOW_SECONDS:
                    continue
                distance = _visual_distance(explicit, photo)
                if distance is None:
                    visual_compare_failures += 1
                elif distance <= VISUAL_DUPLICATE_PHASH_MAX:
                    candidates.append((distance, photo_delta, checkin, explicit))
        if candidates:
            _, _, checkin, explicit = min(
                candidates,
                key=lambda candidate: (
                    candidate[0], candidate[1], candidate[2].source_key, candidate[3].source_id,
                ),
            )
            winner = _preferred_photo(explicit, photo)
            loser = photo if winner is explicit else explicit
            if winner is photo:
                checkin.photos.remove(explicit)
                checkin.photos.append(photo)
                assigned.remove(explicit.source_id)
                assigned.add(photo.source_id)
            suppressed.add(loser.source_id)
            checkin.visual_duplicates_merged += 1
            visually_merged_venue_photos += 1
            continue

        evidence = _nearest_entry(checkins_by_venue.get(photo.related_id, []), photo.created_utc)
        if evidence is None:
            raise ValueError("Venue photo has no location evidence in exported check-ins")
        standalone[(photo.related_id, _local_date(photo.created_utc, evidence.offset_minutes))].append(photo)

    for (venue_id, day), grouped_photos in sorted(standalone.items(), key=lambda item: item[0]):
        evidence = _nearest_entry(checkins_by_venue.get(venue_id, []), grouped_photos[0].created_utc)
        if evidence is None:
            raise ValueError("Venue photo has no location evidence in exported check-ins")
        photo_links = [("Foursquare photo", photo.canonical_url) for photo in grouped_photos if photo.canonical_url]
        links = photo_links[:1] + [link for link in evidence.links if link[0] == "Foursquare venue"]
        entry = Entry(
            source_key=f"venue-photo:{venue_id}:{day.isoformat()}",
            kind="venue-photo",
            created_utc=min(photo.created_utc for photo in grouped_photos),
            offset_minutes=evidence.offset_minutes,
            title=evidence.title,
            body="",
            latitude=evidence.latitude,
            longitude=evidence.longitude,
            venue_id=venue_id,
            links=links,
            photos=list(grouped_photos),
            location_source="nearest-checkin-same-venue",
            timezone_source="nearest-checkin-same-venue",
        )
        entries.append(entry)
        assigned.update(photo.source_id for photo in grouped_photos)

    all_photo_ids = {photo.source_id for photo in photos}
    if assigned & suppressed or assigned | suppressed != all_photo_ids:
        raise ValueError("Not every Swarm photo was assigned exactly once")
    for entry in entries:
        entry.photos.sort(key=lambda photo: (photo.created_utc, photo.source_id))
        if len(entry.photos) > DAYONE_MEDIA_LIMIT:
            raise ValueError(
                f"{entry.source_key} has {len(entry.photos)} photos; refusing to split one diary entry"
            )
    entries.sort(key=lambda entry: (_utc_date(entry.created_utc), entry.source_key))
    metrics = {
        "full_entry_count": len(entries),
        "full_attachment_count": sum(len(entry.photos) for entry in entries),
        "source_max_attachments_per_entry": max((len(entry.photos) for entry in entries), default=0),
        "explicit_checkin_photos": explicit_checkin_photos,
        "explicit_tip_photos": explicit_tip_photos,
        "venue_photos": venue_photos,
        "visually_merged_venue_photos": visually_merged_venue_photos,
        "visual_compare_failures": visual_compare_failures,
        "exported_comment_entries": exported_comment_entries,
        "empty_comment_records": empty_comment_records,
        "foreign_comment_records": foreign_comment_records,
        "estimated_comment_timezones": estimated_comment_timezones,
        "checkins_missing_venue": missing_venue,
        "checkin_people_tagged_entries": sum(
            entry.kind == "checkin" and bool(entry.people_tags) for entry in entries
        ),
        "checkin_people_tag_count": sum(
            len(entry.people_tags) for entry in entries if entry.kind == "checkin"
        ),
        "checkin_people_unresolved_identity_count": len(unresolved_people_identities),
        "tip_locations_from_checkins": tip_locations_from_checkins,
        "unresolved_tip_locations": unresolved_tip_locations,
        "estimated_tip_timezones": estimated_tip_timezones,
        "standalone_venue_photo_entries": len(standalone),
    }
    return entries, photos, _source_counts(root, checkins, photos, tips, comments), metrics


def _entry_review_class(entry: Entry) -> str:
    if entry.kind == "comment":
        return "comment"
    if entry.kind == "venue-photo":
        return "venue-photo"
    if entry.kind == "tip":
        if entry.latitude is None or entry.longitude is None:
            return "tip-unresolved-location"
        return "tip-photo-location" if entry.photos else "tip-no-photo-location"
    if entry.title == "Swarm check-in":
        return "checkin-missing-venue"
    if entry.visual_duplicates_merged:
        return "checkin-visual-duplicate"
    if len(entry.photos) >= 2:
        return "checkin-multi-photo"
    if len(entry.photos) == 1:
        return "checkin-single-photo"
    if entry.reported_comment_count:
        return "checkin-no-photo-comment-count"
    return "checkin-no-photo-other"


def _spread_entries(entries: list[Entry], count: int) -> list[Entry]:
    ordered = sorted(entries, key=lambda entry: (entry.created_utc, entry.source_key))
    if count <= 0:
        return []
    if count >= len(ordered):
        return ordered
    if count == 1:
        return [ordered[len(ordered) // 2]]
    indices = [round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)]
    return [ordered[index] for index in indices]


def _review_sample_entries(entries: list[Entry], size: int) -> list[Entry]:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("Review sample size must be a positive integer")
    if size > len(entries):
        raise ValueError("Review sample size exceeds the available entry count")

    total_weight = sum(weight for _, weight in REVIEW_SAMPLE_WEIGHTS)
    raw_quotas = [size * weight / total_weight for _, weight in REVIEW_SAMPLE_WEIGHTS]
    quotas = [int(value) for value in raw_quotas]
    remaining_slots = size - sum(quotas)
    remainder_order = sorted(
        range(len(quotas)),
        key=lambda index: (-(raw_quotas[index] - quotas[index]), index),
    )
    for index in remainder_order[:remaining_slots]:
        quotas[index] += 1

    buckets: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        buckets[_entry_review_class(entry)].append(entry)

    selected: list[Entry] = []
    selected_keys: set[str] = set()
    for (review_class, _), quota in zip(REVIEW_SAMPLE_WEIGHTS, quotas, strict=True):
        for entry in _spread_entries(buckets.get(review_class, []), quota):
            selected.append(entry)
            selected_keys.add(entry.source_key)

    if len(selected) < size:
        unused = [entry for entry in entries if entry.source_key not in selected_keys]
        selected.extend(_spread_entries(unused, size - len(selected)))
    if len(selected) != size or len({entry.source_key for entry in selected}) != size:
        raise ValueError("Could not construct a unique review sample of the requested size")
    return sorted(selected, key=lambda entry: (entry.created_utc, entry.source_key))


def _pilot_entries(entries: list[Entry]) -> list[Entry]:
    chosen: list[Entry] = []
    venue_entries = [entry for entry in entries if entry.kind == "venue-photo"]
    checkin = next((entry for entry in entries if entry.kind == "checkin" and entry.body
                    and entry.visual_duplicates_merged), None)
    if checkin is None:
        checkin = next((entry for entry in entries if entry.kind == "checkin"
                        and entry.visual_duplicates_merged), None)
    if checkin is None:
        checkin = next((entry for entry in entries if entry.kind == "checkin" and entry.body
                        and entry.title != "Swarm check-in" and entry.photos
                        and any(venue.venue_id == entry.venue_id
                                and _local_date(venue.created_utc, venue.offset_minutes)
                                == _local_date(entry.created_utc, entry.offset_minutes)
                                for venue in venue_entries)), None)
    venue_peer = next((venue for venue in venue_entries if checkin is not None
                       and venue.venue_id == checkin.venue_id
                       and _local_date(venue.created_utc, venue.offset_minutes)
                       == _local_date(checkin.created_utc, checkin.offset_minutes)), None)
    if checkin is None:
        checkin = next((entry for entry in entries if entry.kind == "checkin" and entry.body
                        and entry.title != "Swarm check-in" and len(entry.photos) >= 2), None)
    tip = next((entry for entry in entries if entry.kind == "tip" and entry.photos
                and entry.latitude is not None), None)
    comment = next((entry for entry in entries if entry.kind == "comment"), None)
    for entry in (checkin, venue_peer, tip, comment):
        if entry is not None and entry not in chosen:
            chosen.append(entry)
    if not chosen:
        fallback = next((entry for entry in entries if entry.photos and entry.latitude is not None), None)
        if fallback is None:
            raise ValueError("No representative location-plus-photo entry is available for a pilot")
        chosen.append(fallback)
    return sorted(chosen, key=lambda entry: (_utc_date(entry.created_utc), entry.source_key))


def verify_package(package_path: Path) -> None:
    with zipfile.ZipFile(package_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Day One ZIP failed its CRC check")
        names = archive.namelist()
        if len(names) != len(set(names)) or "Journal.json" not in names:
            raise ValueError("Day One ZIP has duplicate members or no Journal.json")
        journal = json.loads(archive.read("Journal.json"))
        if not isinstance(journal, dict) or not isinstance(journal.get("entries"), list):
            raise ValueError("Invalid Day One Journal.json")
        uuids: set[str] = set()
        identifiers: set[str] = set()
        referenced_media: set[str] = set()
        for entry in journal["entries"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                raise ValueError("Invalid Day One entry")
            entry_uuid = entry.get("uuid")
            if not isinstance(entry_uuid, str) or entry_uuid in uuids:
                raise ValueError("Missing or duplicate Day One entry UUID")
            uuids.add(entry_uuid)
            location = entry.get("location")
            if location is not None:
                if not isinstance(location, dict):
                    raise ValueError("Invalid Day One location")
                latitude = _coordinate(location.get("latitude"), -90, 90, "Day One latitude")
                longitude = _coordinate(location.get("longitude"), -180, 180, "Day One longitude")
                region = location.get("region")
                center = region.get("center") if isinstance(region, dict) else None
                if (not isinstance(center, dict) or center.get("latitude") != latitude
                        or center.get("longitude") != longitude
                        or isinstance(region.get("radius"), bool)
                        or not isinstance(region.get("radius"), (int, float))
                        or region["radius"] <= 0):
                    raise ValueError("Day One location region does not match its coordinates")
            for photo in entry.get("photos", []):
                if not isinstance(photo, dict):
                    raise ValueError("Invalid Day One photo")
                identifier = photo.get("identifier")
                digest = photo.get("md5")
                photo_type = photo.get("type")
                if (not isinstance(identifier, str) or identifier in identifiers
                        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{32}", digest)
                        or not isinstance(photo_type, str)):
                    raise ValueError("Invalid Day One photo metadata")
                identifiers.add(identifier)
                if entry["text"].count(f"![](dayone-moment://{identifier})") != 1:
                    raise ValueError("Day One photo marker does not match its identifier")
                member = f"photos/{digest}.{photo_type}"
                if member not in names or hashlib.md5(archive.read(member)).hexdigest() != digest:
                    raise ValueError("Day One photo filename or MD5 is invalid")
                referenced_media.add(member)
        packaged_media = {name for name in names if name.startswith("photos/")}
        if packaged_media != referenced_media:
            raise ValueError("Day One ZIP contains missing or unreferenced photos")


def _apply_backfill_decisions(entries: list[Entry], decision_path: Path) -> dict[str, int]:
    decision_path = decision_path.expanduser().resolve()
    payload = _load_json(decision_path)
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or not isinstance(decisions, dict)):
        raise ValueError("Invalid Swarm backfill decision file")

    entries_by_key = {entry.source_key: entry for entry in entries}
    applied = 0
    skipped = 0
    source_counts: Counter[str] = Counter()
    for source_key, decision in sorted(decisions.items()):
        if not isinstance(source_key, str) or not isinstance(decision, dict):
            raise ValueError("Invalid Swarm backfill decision")
        status = decision.get("status")
        if status == "skipped":
            skipped += 1
            continue
        if status != "selected" or not isinstance(decision.get("candidate"), dict):
            raise ValueError("Invalid selected Swarm backfill decision")
        entry = entries_by_key.get(source_key)
        if entry is None:
            raise ValueError("Backfill decision references an entry absent from the export")
        if entry.kind not in {"checkin", "tip"} or entry.photos:
            raise ValueError("Backfill decisions may target only photo-less check-ins or tips")
        candidate = decision["candidate"]
        kind = _required_text(candidate.get("kind"), "backfill candidate kind")
        links: list[tuple[str, str]] = []

        if kind == "export":
            source_entry_key = _required_text(
                candidate.get("source_entry_key"), "backfill source entry key"
            )
            photo_source_id = _required_text(
                candidate.get("photo_source_id"), "backfill photo source id"
            )
            source_entry = entries_by_key.get(source_entry_key)
            if (source_entry is None or not entry.venue_id
                    or source_entry.venue_id != entry.venue_id):
                raise ValueError("Export backfill candidate is not from the exact same venue")
            photo = next(
                (item for item in source_entry.photos if item.source_id == photo_source_id),
                None,
            )
            if photo is None:
                raise ValueError("Export backfill candidate photo is absent")
            if url := _web_url(candidate.get("source_url") or photo.canonical_url):
                links.append(("Representative photo source", url))
            note = (
                "Representative venue image selected from another exact-venue Swarm record; "
                "not captured during this visit."
            )
        elif kind in {"manual", "wikimedia"}:
            relative = Path(_required_text(candidate.get("relative_path"), "backfill media path"))
            if relative.is_absolute():
                raise ValueError("Backfill media path must be relative")
            state_root = decision_path.parent.resolve()
            path = (state_root / relative).resolve()
            if not path.is_relative_to(state_root) or not path.is_file():
                raise ValueError("Backfill media path escapes its managed folder or is missing")
            photo_type = PHOTO_EXTENSIONS.get(path.suffix.lower())
            if photo_type is None:
                raise ValueError("Unsupported managed backfill image type")
            photo = Photo(
                source_id=f"backfill:{hashlib.sha256(path.read_bytes()).hexdigest()}",
                created_utc=entry.created_utc,
                canonical_url=_web_url(candidate.get("source_url")),
                related_kind="representative",
                related_id=entry.venue_id,
                path=path,
                width=_dimension(candidate.get("width")),
                height=_dimension(candidate.get("height")),
            )
            if kind == "wikimedia":
                title = _text(candidate.get("title")) or "Wikimedia Commons image"
                author = _text(candidate.get("author")) or "Unknown creator"
                license_name = _text(candidate.get("license")) or "License shown at source"
                note = (
                    f"Representative venue image from Wikimedia Commons; not captured during "
                    f"this visit. {title} by {author}, {license_name}."
                )
                if url := _web_url(candidate.get("source_url")):
                    links.append(("Wikimedia source", url))
                if url := _web_url(candidate.get("license_url")):
                    links.append(("Image license", url))
            else:
                note = (
                    "Representative venue image supplied manually during review; not captured "
                    "during this visit."
                )
        else:
            raise ValueError("Unsupported representative-photo source")

        entry.photos.append(photo)
        entry.photos.sort(key=lambda item: (item.created_utc, item.source_id))
        entry.representative_photo_source = kind
        entry.representative_photo_note = note
        entry.representative_photo_links = links
        applied += 1
        source_counts[kind] += 1

    return {
        "representative_backfills": applied,
        "backfill_decisions_skipped": skipped,
        "backfills_from_export": source_counts["export"],
        "backfills_from_manual_drop": source_counts["manual"],
        "backfills_from_wikimedia": source_counts["wikimedia"],
    }


def _apply_media_workflow(entries: list[Entry], database_path: Path) -> dict[str, int]:
    database_path = database_path.expanduser().resolve()
    if not database_path.is_file():
        raise ValueError(f"Swarm media workflow database does not exist: {database_path}")
    entry_by_key = {entry.source_key: entry for entry in entries}
    reordered_entries = 0
    cropped_photos = 0
    rejected_photos = 0
    tagged_entries = 0
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        schema = connection.execute(
            "SELECT value FROM swarm_review_meta WHERE key = 'schema_version'"
        ).fetchone()
        if schema is None or str(schema[0]) not in {"1", "2", "3", "4"}:
            raise ValueError("Unsupported Swarm media workflow database")
        photo_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(swarm_photos)")
        }
        rejected_expression = "rejected" if "rejected" in photo_columns else "0 AS rejected"
        rows = connection.execute(
            f"""
            SELECT
                photo_key, entry_key, photo_source_id, custom_position,
                crop_queued, crop_derivative_path, crop_source_sha256, source_sha256,
                {rejected_expression}
            FROM swarm_photos
            WHERE active = 1
            ORDER BY entry_key, COALESCE(custom_position, default_position), default_position,
                     photo_source_id
            """
        ).fetchall()
        rows_by_entry: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            rows_by_entry[str(row["entry_key"])].append(row)
        people_by_photo: dict[str, list[str]] = defaultdict(list)
        for photo_key, name in connection.execute(
            """
            SELECT people.photo_key, people.canonical_name
            FROM swarm_photo_people AS people
            JOIN swarm_photos AS photos ON photos.photo_key = people.photo_key
            WHERE photos.active = 1
            ORDER BY people.photo_key, people.canonical_name
            """
        ):
            people_by_photo[str(photo_key)].append(str(name))

    for entry_key, rows_for_entry in rows_by_entry.items():
        entry = entry_by_key.get(entry_key)
        if entry is None:
            continue
        photos_by_id = {photo.source_id: photo for photo in entry.photos}
        if len(photos_by_id) != len(entry.photos):
            raise ValueError(f"Duplicate photo identity in Swarm entry: {entry_key}")
        known_rows = [row for row in rows_for_entry if str(row["photo_source_id"]) in photos_by_id]
        ordered_ids = [str(row["photo_source_id"]) for row in known_rows]
        ordered_ids.extend(photo.source_id for photo in entry.photos if photo.source_id not in ordered_ids)
        if ordered_ids != [photo.source_id for photo in entry.photos]:
            reordered_entries += 1
        output_photos: list[Photo] = []
        names: set[str] = set()
        row_by_id = {str(row["photo_source_id"]): row for row in known_rows}
        for source_id in ordered_ids:
            photo = photos_by_id[source_id]
            row = row_by_id.get(source_id)
            if row is not None:
                if bool(row["rejected"]):
                    rejected_photos += 1
                    continue
                if bool(row["crop_queued"]):
                    derivative_text = str(row["crop_derivative_path"] or "").strip()
                    derivative = Path(derivative_text).resolve() if derivative_text else None
                    if (derivative is None or not derivative.is_file()
                            or str(row["crop_source_sha256"] or "") != str(row["source_sha256"] or "")):
                        raise ValueError(
                            f"Swarm photo has an unfinished crop: {entry_key} / {source_id}"
                        )
                    if derivative.suffix.lower() not in PHOTO_EXTENSIONS:
                        raise ValueError("Swarm crop derivative has an unsupported image type")
                    photo = replace(photo, path=derivative, width=None, height=None)
                    cropped_photos += 1
                names.update(people_by_photo.get(str(row["photo_key"]), []))
            output_photos.append(photo)
        entry.photos = output_photos
        entry.people_tags = sorted({*entry.people_tags, *(f"person:{name}" for name in names)})
        if entry.people_tags:
            tagged_entries += 1
    return {
        "workflow_reordered_entry_count": reordered_entries,
        "workflow_cropped_photo_count": cropped_photos,
        "workflow_rejected_photo_count": rejected_photos,
        "workflow_people_tagged_entry_count": tagged_entries,
    }


def build_package(source: Path, output_dir: Path, *, pilot: bool = False,
                  sample_size: int | None = None,
                  backfill_decisions: Path | None = None,
                  media_workflow_database: Path | None = None,
                  people_mappings: dict[str, str] | None = None) -> dict[str, Any]:
    if pilot and sample_size is not None:
        raise ValueError("Pilot and review sample modes are mutually exclusive")
    root = _export_root(source)
    entries, photos, source_counts, metrics = _build_entries(root, people_mappings)
    version = 6 if media_workflow_database is not None else 5 if backfill_decisions is not None else 4
    namespace = (
        WORKFLOW_NAMESPACE if media_workflow_database is not None
        else BACKFILL_NAMESPACE if backfill_decisions is not None
        else NAMESPACE
    )
    if backfill_decisions is not None:
        metrics.update(_apply_backfill_decisions(entries, backfill_decisions))
    else:
        metrics.update({
            "representative_backfills": 0,
            "backfill_decisions_skipped": 0,
            "backfills_from_export": 0,
            "backfills_from_manual_drop": 0,
            "backfills_from_wikimedia": 0,
        })
    if media_workflow_database is not None:
        metrics.update(_apply_media_workflow(entries, media_workflow_database))
    else:
        metrics.update({
            "workflow_reordered_entry_count": 0,
            "workflow_cropped_photo_count": 0,
            "workflow_rejected_photo_count": 0,
            "workflow_people_tagged_entry_count": 0,
        })
    metrics["full_attachment_count"] = sum(len(entry.photos) for entry in entries)
    metrics["source_max_attachments_per_entry"] = max(
        (len(entry.photos) for entry in entries), default=0
    )
    if pilot:
        selected_entries = _pilot_entries(entries)
        mode = "pilot"
    elif sample_size is not None:
        selected_entries = _review_sample_entries(entries, sample_size)
        mode = f"sample_{sample_size}"
    else:
        selected_entries = entries
        mode = "full"

    output_dir = output_dir.expanduser().resolve()
    if output_dir == root or output_dir.is_relative_to(root):
        raise ValueError("Output folder must be outside the Swarm export")
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    package_path = output_dir / f"swarm_dayone_{mode}_v{version}_{suffix}.zip"
    manifest_path = output_dir / f"swarm_dayone_{mode}_v{version}_{suffix}_manifest.json"
    if package_path.exists() or manifest_path.exists():
        raise FileExistsError("A Swarm package with this timestamp already exists")

    journal_entries: list[dict[str, Any]] = []
    zip_media: dict[str, Path] = {}
    manifest_entries: list[dict[str, Any]] = []
    for entry in selected_entries:
        entry_uuid = uuid.uuid5(namespace, entry.source_key).hex.upper()
        photo_objects: list[dict[str, Any]] = []
        identifiers: list[str] = []
        for order, photo in enumerate(entry.photos):
            identifier = uuid.uuid5(
                namespace, f"{entry.source_key}:{photo.source_id}"
            ).hex.upper()
            digest = _md5(photo.path)
            photo_type = PHOTO_EXTENSIONS[photo.path.suffix.lower()]
            member = f"photos/{digest}.{photo_type}"
            zip_media[member] = photo.path
            item: dict[str, Any] = {
                "identifier": identifier,
                "md5": digest,
                "type": photo_type,
                "date": _utc_date(photo.created_utc),
                "orderInEntry": order,
                "fileSize": photo.path.stat().st_size,
            }
            if photo.width and photo.height:
                item.update(width=photo.width, height=photo.height)
            photo_objects.append(item)
            identifiers.append(identifier)

        item = {
            "uuid": entry_uuid,
            "creationDate": _utc_date(entry.created_utc),
            "timeZone": _timezone_name(entry.offset_minutes),
            "text": _entry_text(entry, identifiers),
            "tags": ["source:swarm", f"swarm:{entry.kind}"],
        }
        if entry.timezone_source != "source":
            item["tags"].append("swarm:timezone-derived")
        if entry.kind == "comment":
            item["tags"].append("swarm:comment-parent-unresolved")
        if entry.representative_photo_source:
            item["tags"].append("swarm:representative-photo")
        item["tags"].extend(entry.people_tags)
        item["tags"] = list(dict.fromkeys(item["tags"]))
        if entry.latitude is not None and entry.longitude is not None:
            location: dict[str, Any] = {
                "latitude": entry.latitude,
                "longitude": entry.longitude,
                "region": {
                    "center": {"latitude": entry.latitude, "longitude": entry.longitude},
                    "radius": 75,
                },
            }
            if entry.title and entry.title not in {"Swarm check-in", "Foursquare tip"}:
                location["placeName"] = entry.title.removeprefix("Tip for ")
            item["location"] = location
        else:
            item["tags"].append("swarm:location-unresolved")
        if photo_objects:
            item["photos"] = photo_objects
        journal_entries.append(item)
        manifest_entries.append({
            "entry_uuid": entry_uuid,
            "kind": entry.kind,
            "photo_count": len(entry.photos),
            "has_location": entry.latitude is not None and entry.longitude is not None,
            "has_venue_name": entry.title not in {"Swarm check-in", "Foursquare tip"},
            "location_source": entry.location_source,
            "timezone_source": entry.timezone_source,
            "source_link_count": len(entry.links),
            "visual_duplicates_merged": entry.visual_duplicates_merged,
            "representative_photo_source": entry.representative_photo_source or None,
            "people_tags": entry.people_tags,
            "review_class": _entry_review_class(entry),
        })

    warnings = [
        "Owner-authored comments were imported as standalone entries because the export has no parent key.",
        "Check-in comment threads cannot be reconstructed because check-ins expose only counts and comment records have no parent key.",
        "Automatic and unconfirmed visits were not imported as confirmed diary events.",
        "Tip coordinates are derived only from exported check-ins with the same venue id.",
        "Swarm createdAt values are UTC instants; Day One applies each entry's recorded or derived local offset for display.",
        "Venue-linked photos remain separate unless a same-venue, two-minute visual comparison confirms a duplicate of an explicit check-in photo.",
    ]
    if source_counts["comment_bodies_not_exported"]:
        warnings.append(
            "Some check-ins report comments, but the export supplies only counts and not those comment bodies."
        )
    if metrics["checkin_people_tagged_entries"]:
        warnings.append(
            "Person tags were recovered only from explicit user entities in check-in text; "
            "the Swarm export has no separate historical check-in-with field."
        )
    if metrics["checkin_people_unresolved_identity_count"]:
        warnings.append(
            f"{metrics['checkin_people_unresolved_identity_count']} explicit companion "
            "identities were not tagged because no canonical People mapping was supplied."
        )
    if metrics["visual_compare_failures"]:
        warnings.append(
            f"{metrics['visual_compare_failures']} candidate visual comparisons could not be completed; those photos were preserved separately."
        )
    if metrics["representative_backfills"]:
        warnings.append(
            "Selected representative venue images are explicitly labeled as not captured "
            "during the visit; Wikimedia selections retain source and license attribution."
        )
    if media_workflow_database is not None:
        warnings.append(
            "Photo order, completed crop derivatives, and photo-level People tags were applied "
            "from the persistent Swarm media workflow database."
        )
    manifest = {
        "schema_version": version,
        "converter_version": version,
        "mode": mode,
        "source_counts": source_counts,
        "exported_entry_count": len(journal_entries),
        "exported_attachment_count": sum(len(entry.photos) for entry in selected_entries),
        "exported_unique_media_count": len(zip_media),
        "max_attachments_per_entry": max((len(entry.photos) for entry in selected_entries), default=0),
        "sample_class_counts": dict(sorted(Counter(
            _entry_review_class(entry) for entry in selected_entries
        ).items())) if sample_size is not None else {},
        **metrics,
        "entries": manifest_entries,
        "warnings": warnings,
    }

    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".zip", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".json", delete=False) as temporary_manifest:
        temporary_manifest_path = Path(temporary_manifest.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", allowZip64=True) as archive:
            archive.writestr(
                "Journal.json",
                json.dumps({"metadata": {"version": "1.0"}, "entries": journal_entries}, ensure_ascii=False),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            for member, path in sorted(zip_media.items()):
                archive.write(path, member, compress_type=zipfile.ZIP_STORED)
        verify_package(temporary_path)
        temporary_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary_path.replace(package_path)
        try:
            temporary_manifest_path.replace(manifest_path)
        except Exception:
            package_path.unlink(missing_ok=True)
            raise
    finally:
        temporary_path.unlink(missing_ok=True)
        temporary_manifest_path.unlink(missing_ok=True)

    return {
        "package_path": str(package_path),
        "manifest_path": str(manifest_path),
        "mode": mode,
        "entry_count": len(journal_entries),
        "attachment_count": manifest["exported_attachment_count"],
        "unique_media_count": len(zip_media),
        "entries_with_location": sum("location" in entry for entry in journal_entries),
        **metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Swarm export folder or its parent")
    parser.add_argument("--output-dir", type=Path, required=True, help="Folder for ZIP and manifest")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--pilot", action="store_true", help="Export a small structural pilot")
    modes.add_argument("--sample-size", type=int,
                       help="Export a deterministic stratified review sample")
    parser.add_argument(
        "--backfill-decisions",
        type=Path,
        help="Apply reviewed representative-photo decisions and emit a version 5 package",
    )
    parser.add_argument(
        "--media-workflow-db",
        type=Path,
        help="Apply persistent Swarm photo order, completed crops, and People tags",
    )
    parser.add_argument(
        "--people-map",
        type=Path,
        help="Persistent Swarm-to-digiKam People mapping JSON (defaults beside output packages)",
    )
    args = parser.parse_args()
    root = _export_root(args.source)
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir == root or output_dir.is_relative_to(root):
        raise ValueError("Output folder must be outside the Swarm export")
    mapping_path = args.people_map or args.output_dir / "swarm_people_mappings.json"
    people_mappings = _resolve_people_mappings(
        root,
        mapping_path,
        input if sys.stdin.isatty() else None,
    )
    summary = build_package(
        args.source,
        args.output_dir,
        pilot=args.pilot,
        sample_size=args.sample_size,
        backfill_decisions=args.backfill_decisions,
        media_workflow_database=args.media_workflow_db,
        people_mappings=people_mappings,
    )
    summary["people_mapping_path"] = str(mapping_path.expanduser().resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
