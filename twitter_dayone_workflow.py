#!/usr/bin/env python3
"""Review an X/Twitter archive and export explicitly approved Day One entries.

The source archive is read-only. Review decisions live in a separate SQLite
database, and only entries with a reviewed ``include`` decision can be exported.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import mimetypes
import re
import secrets
import sqlite3
import tempfile
import threading
import urllib.parse
import uuid
import zipfile
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from project365_service_activity import (
    activity_handler,
    inject_activity,
    start_idle_shutdown,
)


SCHEMA_VERSION = "1"
DAYONE_MEDIA_LIMIT = 30
MAX_BODY_BYTES = 1024 * 1024
NAMESPACE = uuid.UUID("dadb8855-90d6-471a-ab49-f2d86db2451f")
PHOTO_TYPES = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".heic": "heic"}
VIDEO_TYPES = {".mp4": "mp4", ".mov": "mov", ".m4v": "m4v"}
DECISIONS = {"pending", "include", "exclude", "needs_context"}
VIEWS = {"recommended", "needs_context", "included", "excluded", "all"}
TIMEZONE_MAP = {
    "Pacific Time (US & Canada)": "America/Los_Angeles",
    "Eastern Time (US & Canada)": "America/New_York",
    "Central Time (US & Canada)": "America/Chicago",
    "Mountain Time (US & Canada)": "America/Denver",
    "Taipei": "Asia/Taipei",
    "UTC": "Etc/UTC",
}


@dataclass(frozen=True)
class TwitterMedia:
    media_id: str
    post_id: str
    path: Path
    kind: str
    created_utc: dt.datetime
    source_sha256: str


@dataclass(frozen=True)
class TwitterCandidate:
    entry_key: str
    root_post_id: str
    post_ids: tuple[str, ...]
    created_utc: dt.datetime
    time_zone: str
    username: str
    source_url: str
    body: str
    classification: str
    context_status: str
    default_decision: str
    default_rating: int
    auto_excluded: bool
    media: tuple[TwitterMedia, ...]
    source_fingerprint: str


@dataclass(frozen=True)
class CandidateBuild:
    candidates: list[TwitterCandidate]
    summary: dict[str, int | str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_root(source: Path) -> Path:
    source = source.expanduser().resolve()
    if (source / "data" / "tweets.js").is_file():
        return source
    if not source.is_dir():
        raise ValueError(f"Twitter archive folder does not exist: {source}")
    candidates = [
        child.resolve() for child in source.iterdir()
        if child.is_dir() and (child / "data" / "tweets.js").is_file()
    ]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one extracted Twitter archive containing data/tweets.js")
    return candidates[0]


def _load_js_array(path: Path, assignment: str, *, required: bool = True) -> list[dict[str, Any]]:
    if not path.is_file():
        if required:
            raise ValueError(f"Twitter archive is missing {path.name}")
        return []
    text = path.read_text(encoding="utf-8-sig")
    prefix = assignment + " = "
    if not text.startswith(prefix):
        raise ValueError(f"Unexpected Twitter archive assignment in {path.name}")
    payload_text = text[len(prefix):].strip()
    if payload_text.endswith(";"):
        payload_text = payload_text[:-1].rstrip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid Twitter archive JSON in {path.name}") from error
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError(f"Invalid Twitter archive collection in {path.name}")
    return payload


def _parse_tweet_time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Twitter post has no creation timestamp")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Twitter post has an invalid creation timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Twitter post timestamp has no timezone")
    return parsed.astimezone(dt.UTC)


def _parse_iso_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _wrapped_value(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if not rows:
        return {}
    value = rows[0].get(key)
    return value if isinstance(value, dict) else {}


def _account_details(root: Path) -> tuple[str, str]:
    data = root / "data"
    account = _wrapped_value(
        _load_js_array(data / "account.js", "window.YTD.account.part0", required=False),
        "account",
    )
    timezone = _wrapped_value(
        _load_js_array(
            data / "account-timezone.js",
            "window.YTD.account_timezone.part0",
            required=False,
        ),
        "accountTimezone",
    )
    username = str(account.get("username") or "").strip().lstrip("@")
    source_timezone = str(timezone.get("timeZone") or "UTC").strip()
    return username, TIMEZONE_MAP.get(source_timezone, "Etc/UTC")


def _note_text_by_timestamp(root: Path, tweet_times: Counter[str]) -> tuple[dict[str, str], int]:
    rows = _load_js_array(
        root / "data" / "note-tweet.js",
        "window.YTD.note_tweet.part0",
        required=False,
    )
    notes: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        record = row.get("noteTweet")
        if not isinstance(record, dict):
            continue
        created = _parse_iso_time(record.get("createdAt"))
        core = record.get("core")
        text = core.get("text") if isinstance(core, dict) else None
        if created and isinstance(text, str) and text.strip():
            notes[created.isoformat()].append(text.strip())
    result = {
        timestamp: values[0]
        for timestamp, values in notes.items()
        if len(values) == 1 and tweet_times[timestamp] == 1
    }
    ambiguous = sum(len(values) != 1 or tweet_times[timestamp] != 1 for timestamp, values in notes.items())
    return result, ambiguous


def _clean_text(tweet: dict[str, Any], note_text: str = "") -> str:
    text = note_text or str(tweet.get("full_text") or "")
    entities = tweet.get("entities")
    if not isinstance(entities, dict):
        return text.strip()
    replacements: list[tuple[str, str]] = []
    for row in entities.get("urls") or []:
        if not isinstance(row, dict):
            continue
        short = row.get("url")
        expanded = row.get("expanded_url")
        if isinstance(short, str) and isinstance(expanded, str) and short:
            replacements.append((short, expanded))
    for row in entities.get("media") or []:
        if isinstance(row, dict) and isinstance(row.get("url"), str):
            replacements.append((str(row["url"]), ""))
    for short, expanded in replacements:
        text = text.replace(short, expanded)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _source_id(tweet: dict[str, Any]) -> str:
    value = tweet.get("id_str")
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("Twitter post has an invalid source id")
    return value


def _media_url_names(entity: dict[str, Any]) -> set[str]:
    urls: list[str] = []
    for key in ("media_url_https", "media_url"):
        value = entity.get(key)
        if isinstance(value, str) and value:
            urls.append(value)
    video_info = entity.get("video_info")
    variants = video_info.get("variants") if isinstance(video_info, dict) else []
    if isinstance(variants, list):
        urls.extend(
            str(variant["url"])
            for variant in variants
            if isinstance(variant, dict) and isinstance(variant.get("url"), str)
        )
    return {
        Path(urllib.parse.urlparse(url).path).name
        for url in urls
        if Path(urllib.parse.urlparse(url).path).name
    }


def _unique_media_assignment(
    post_id: str,
    entities: list[dict[str, Any]],
    paths: list[Path],
) -> list[Path]:
    archive_names = [path.name.removeprefix(f"{post_id}-") for path in paths]
    entity_kinds = [str(entity.get("type") or "") for entity in entities]
    aliases = [_media_url_names(entity) for entity in entities]
    entity_ids = [str(entity.get("id_str") or "") for entity in entities]
    compatible: list[list[int]] = []
    for index, entity in enumerate(entities):
        entity_kind = entity_kinds[index]
        matches = []
        by_kind = []
        for path_index, path in enumerate(paths):
            suffix = path.suffix.lower()
            kind = "photo" if suffix in PHOTO_TYPES else "video" if suffix in VIDEO_TYPES else ""
            if entity_kind == "photo" and kind != "photo":
                continue
            if entity_kind in {"video", "animated_gif"} and kind != "video":
                continue
            by_kind.append(path_index)
            archive_name = archive_names[path_index]
            if archive_name in aliases[index] or (
                entity_ids[index] and path.stem.removeprefix(f"{post_id}-") == entity_ids[index]
            ):
                matches.append(path_index)
        compatible.append(matches or by_kind)

    # Find one perfect matching, then reject it if an alternating cycle proves
    # that another complete entity-to-file assignment is also possible.
    path_to_entity: dict[int, int] = {}

    def assign(entity_index: int, visited_paths: set[int]) -> bool:
        for path_index in compatible[entity_index]:
            if path_index in visited_paths:
                continue
            visited_paths.add(path_index)
            previous_entity = path_to_entity.get(path_index)
            if previous_entity is None or assign(previous_entity, visited_paths):
                path_to_entity[path_index] = entity_index
                return True
        return False

    for entity_index in sorted(range(len(entities)), key=lambda index: len(compatible[index])):
        if not assign(entity_index, set()):
            raise ValueError(
                f"Twitter post {post_id} media files cannot be uniquely associated "
                "with archive media entities"
            )

    entity_to_path = {entity: path for path, entity in path_to_entity.items()}
    alternating: list[list[int]] = [[] for _ in entities]
    for entity_index, options in enumerate(compatible):
        for path_index in options:
            if path_index != entity_to_path[entity_index]:
                alternating[entity_index].append(path_to_entity[path_index])

    visiting: set[int] = set()
    visited: set[int] = set()

    def has_cycle(entity_index: int) -> bool:
        if entity_index in visiting:
            return True
        if entity_index in visited:
            return False
        visiting.add(entity_index)
        if any(has_cycle(next_entity) for next_entity in alternating[entity_index]):
            return True
        visiting.remove(entity_index)
        visited.add(entity_index)
        return False

    if any(has_cycle(entity_index) for entity_index in range(len(entities))):
        raise ValueError(
            f"Twitter post {post_id} media files cannot be uniquely associated "
            "with archive media entities"
        )
    return [paths[entity_to_path[index]] for index in range(len(entities))]


def _media_for_post(root: Path, tweet: dict[str, Any], created: dt.datetime) -> list[TwitterMedia]:
    post_id = _source_id(tweet)
    extended = tweet.get("extended_entities")
    entities = extended.get("media") if isinstance(extended, dict) else []
    if not isinstance(entities, list):
        raise ValueError(f"Twitter post {post_id} has invalid media entities")
    folder = root / "data" / "tweets_media"
    local_paths = sorted(
        (path for path in folder.glob(f"{post_id}-*") if path.is_file()),
        key=lambda path: path.name,
    ) if folder.is_dir() else []
    if len(local_paths) != len(entities):
        raise ValueError(
            f"Twitter post {post_id} declares {len(entities)} media items but has "
            f"{len(local_paths)} local files"
        )
    if not all(isinstance(entity, dict) for entity in entities):
        raise ValueError(f"Twitter post {post_id} has an invalid media entity")
    ordered_paths = _unique_media_assignment(post_id, entities, local_paths)
    media: list[TwitterMedia] = []
    for index, (entity, path) in enumerate(zip(entities, ordered_paths, strict=True)):
        if not isinstance(entity, dict):
            raise ValueError(f"Twitter post {post_id} has an invalid media entity")
        suffix = path.suffix.lower()
        if suffix in PHOTO_TYPES:
            kind = "photo"
        elif suffix in VIDEO_TYPES:
            kind = "video"
        else:
            raise ValueError(f"Twitter post {post_id} has unsupported media type: {suffix or '(none)'}")
        entity_kind = str(entity.get("type") or "")
        if entity_kind == "photo" and kind != "photo":
            raise ValueError(f"Twitter post {post_id} media type does not match its local file")
        if entity_kind in {"video", "animated_gif"} and kind != "video":
            raise ValueError(f"Twitter post {post_id} media type does not match its local file")
        entity_id = str(entity.get("id_str") or index)
        media.append(TwitterMedia(
            media_id=f"{post_id}:{entity_id}",
            post_id=post_id,
            path=path.resolve(),
            kind=kind,
            created_utc=created,
            source_sha256=_sha256(path),
        ))
    return media


def _build_candidates(source: Path) -> CandidateBuild:
    root = _archive_root(source)
    username, timezone = _account_details(root)
    rows = _load_js_array(root / "data" / "tweets.js", "window.YTD.tweets.part0")
    posts: dict[str, dict[str, Any]] = {}
    times: dict[str, dt.datetime] = {}
    for row in rows:
        tweet = row.get("tweet")
        if not isinstance(tweet, dict):
            raise ValueError("Twitter archive contains an invalid tweet wrapper")
        source_id = _source_id(tweet)
        previous = posts.get(source_id)
        if previous is not None:
            if previous != tweet:
                raise ValueError(f"Twitter archive contains conflicting duplicate post id {source_id}")
            continue
        posts[source_id] = tweet
        times[source_id] = _parse_tweet_time(tweet.get("created_at"))
    if not posts:
        raise ValueError("Twitter archive contains no posts")

    timestamp_counts = Counter(value.isoformat() for value in times.values())
    note_texts, ambiguous_notes = _note_text_by_timestamp(root, timestamp_counts)
    root_by_post: dict[str, str] = {}
    for post_id in posts:
        if post_id in root_by_post:
            continue
        current = post_id
        path: list[str] = []
        visited: set[str] = set()
        while current not in root_by_post:
            if current in visited:
                raise ValueError(f"Twitter archive contains a reply cycle at post {post_id}")
            visited.add(current)
            path.append(current)
            parent = str(posts[current].get("in_reply_to_status_id_str") or "")
            if not parent or parent not in posts:
                group_root = current
                break
            current = parent
        else:
            group_root = root_by_post[current]
        for member in path:
            root_by_post[member] = group_root

    groups: defaultdict[str, list[str]] = defaultdict(list)
    for post_id, root_id in root_by_post.items():
        groups[root_id].append(post_id)

    candidates: list[TwitterCandidate] = []
    missing_context_posts = 0
    retweet_posts = 0
    total_media = 0
    for root_id, post_ids in groups.items():
        ordered_ids = sorted(post_ids, key=lambda value: (times[value], int(value)))
        root_tweet = posts[root_id]
        root_parent = str(root_tweet.get("in_reply_to_status_id_str") or "")
        root_text = str(root_tweet.get("full_text") or "")
        if root_text.startswith("RT @"):
            classification = "retweet"
            context_status = "reshared"
            retweet_posts += len(ordered_ids)
        elif root_parent:
            classification = "missing_context"
            context_status = "parent_not_in_archive"
            missing_context_posts += len(ordered_ids)
        elif len(ordered_ids) > 1:
            classification = "complete_thread"
            context_status = "complete"
        else:
            classification = "standalone"
            context_status = "complete"
        auto_excluded = classification in {"missing_context", "retweet"}
        media: list[TwitterMedia] = []
        body_parts: list[str] = []
        fingerprint_posts: list[dict[str, Any]] = []
        for post_id in ordered_ids:
            tweet = posts[post_id]
            created = times[post_id]
            post_media = _media_for_post(root, tweet, created)
            media.extend(post_media)
            note_text = note_texts.get(created.isoformat(), "")
            cleaned = _clean_text(tweet, note_text)
            if cleaned:
                body_parts.append(cleaned)
            fingerprint_posts.append({
                "id": post_id,
                "created": created.isoformat(),
                "parent": str(tweet.get("in_reply_to_status_id_str") or ""),
                "text": cleaned,
                "media": [(item.media_id, item.source_sha256) for item in post_media],
            })
        if len(media) > DAYONE_MEDIA_LIMIT:
            auto_excluded = True
            classification = "too_many_media"
            context_status = "manual_split_required"
        body = "\n\n---\n\n".join(body_parts)
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_posts, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        source_url = f"https://x.com/{username}/status/{root_id}" if username else ""
        rating = 4 if media else 3
        if len(ordered_ids) > 1 and not auto_excluded:
            rating = max(rating, 4)
        if auto_excluded:
            rating = 1
        total_media += len(media)
        candidates.append(TwitterCandidate(
            entry_key=f"twitter:{root_id}",
            root_post_id=root_id,
            post_ids=tuple(ordered_ids),
            created_utc=times[ordered_ids[0]],
            time_zone=timezone,
            username=username,
            source_url=source_url,
            body=body,
            classification=classification,
            context_status=context_status,
            default_decision="exclude" if auto_excluded else "pending",
            default_rating=rating,
            auto_excluded=auto_excluded,
            media=tuple(media),
            source_fingerprint=fingerprint,
        ))
    candidates.sort(key=lambda item: (item.created_utc, int(item.root_post_id)))
    summary: dict[str, int | str] = {
        "source_post_count": len(posts),
        "candidate_entry_count": len(candidates),
        "recommended_entry_count": sum(not item.auto_excluded for item in candidates),
        "auto_excluded_entry_count": sum(item.auto_excluded for item in candidates),
        "missing_context_posts": missing_context_posts,
        "retweet_posts": retweet_posts,
        "media_count": total_media,
        "ambiguous_note_tweets": ambiguous_notes,
        "time_zone": timezone,
    }
    return CandidateBuild(candidates, summary)


class TwitterReviewStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
            CREATE TABLE IF NOT EXISTS twitter_review_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS twitter_entries (
                entry_key TEXT PRIMARY KEY,
                root_post_id TEXT NOT NULL,
                post_ids_json TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                time_zone TEXT NOT NULL,
                username TEXT NOT NULL,
                source_url TEXT NOT NULL,
                generated_body TEXT NOT NULL,
                classification TEXT NOT NULL,
                context_status TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                post_count INTEGER NOT NULL CHECK(post_count > 0),
                media_count INTEGER NOT NULL CHECK(media_count >= 0),
                auto_excluded INTEGER NOT NULL CHECK(auto_excluded IN (0, 1)),
                decision TEXT NOT NULL CHECK(decision IN ('pending', 'include', 'exclude', 'needs_context')),
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                edited_title TEXT,
                edited_body TEXT,
                context_note TEXT NOT NULL DEFAULT '',
                reviewed INTEGER NOT NULL DEFAULT 0 CHECK(reviewed IN (0, 1)),
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS twitter_media (
                media_id TEXT PRIMARY KEY,
                entry_key TEXT NOT NULL,
                post_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('photo', 'video')),
                created_utc TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                default_position INTEGER NOT NULL CHECK(default_position >= 0),
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY(entry_key) REFERENCES twitter_entries(entry_key) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_twitter_entries_queue
            ON twitter_entries(active, auto_excluded, reviewed, created_utc);
            CREATE INDEX IF NOT EXISTS idx_twitter_media_entry
            ON twitter_media(entry_key, active, default_position);
            """
        )
        existing = connection.execute(
            "SELECT value FROM twitter_review_meta WHERE key = 'schema_version'"
        ).fetchone()
        if existing is not None and str(existing[0]) != SCHEMA_VERSION:
            raise ValueError("Unsupported Twitter review schema")
        connection.execute(
            "INSERT OR REPLACE INTO twitter_review_meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )

    def sync(self, candidates: list[TwitterCandidate]) -> dict[str, int]:
        now = dt.datetime.now(dt.UTC).isoformat()
        changed = 0
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE twitter_entries SET active = 0")
            connection.execute("UPDATE twitter_media SET active = 0")
            for candidate in candidates:
                previous = connection.execute(
                    "SELECT source_fingerprint FROM twitter_entries WHERE entry_key = ?",
                    (candidate.entry_key,),
                ).fetchone()
                fingerprint_changed = previous is not None and previous[0] != candidate.source_fingerprint
                changed += int(fingerprint_changed)
                connection.execute(
                    """
                    INSERT INTO twitter_entries (
                        entry_key, root_post_id, post_ids_json, created_utc, time_zone,
                        username, source_url, generated_body, classification, context_status,
                        source_fingerprint, post_count, media_count, auto_excluded,
                        decision, rating, reviewed, active, last_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                    ON CONFLICT(entry_key) DO UPDATE SET
                        root_post_id = excluded.root_post_id,
                        post_ids_json = excluded.post_ids_json,
                        created_utc = excluded.created_utc,
                        time_zone = excluded.time_zone,
                        username = excluded.username,
                        source_url = excluded.source_url,
                        generated_body = excluded.generated_body,
                        classification = excluded.classification,
                        context_status = excluded.context_status,
                        source_fingerprint = excluded.source_fingerprint,
                        post_count = excluded.post_count,
                        media_count = excluded.media_count,
                        auto_excluded = excluded.auto_excluded,
                        reviewed = CASE
                            WHEN twitter_entries.source_fingerprint != excluded.source_fingerprint THEN 0
                            ELSE twitter_entries.reviewed
                        END,
                        active = 1,
                        last_seen_at = excluded.last_seen_at,
                        updated_at = CASE
                            WHEN twitter_entries.source_fingerprint != excluded.source_fingerprint THEN excluded.updated_at
                            ELSE twitter_entries.updated_at
                        END
                    """,
                    (
                        candidate.entry_key, candidate.root_post_id,
                        json.dumps(candidate.post_ids), candidate.created_utc.isoformat(),
                        candidate.time_zone, candidate.username, candidate.source_url,
                        candidate.body, candidate.classification, candidate.context_status,
                        candidate.source_fingerprint, len(candidate.post_ids), len(candidate.media),
                        int(candidate.auto_excluded), candidate.default_decision,
                        candidate.default_rating, now, now,
                    ),
                )
                for position, media in enumerate(candidate.media):
                    connection.execute(
                        """
                        INSERT INTO twitter_media (
                            media_id, entry_key, post_id, source_path, kind, created_utc,
                            source_sha256, default_position, active, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        ON CONFLICT(media_id) DO UPDATE SET
                            entry_key = excluded.entry_key,
                            post_id = excluded.post_id,
                            source_path = excluded.source_path,
                            kind = excluded.kind,
                            created_utc = excluded.created_utc,
                            source_sha256 = excluded.source_sha256,
                            default_position = excluded.default_position,
                            active = 1,
                            last_seen_at = excluded.last_seen_at
                        """,
                        (
                            media.media_id, candidate.entry_key, media.post_id,
                            str(media.path), media.kind, media.created_utc.isoformat(),
                            media.source_sha256, position, now,
                        ),
                    )
        return {"candidate_count": len(candidates), "reopened_count": changed}

    def review(
        self,
        entry_key: str,
        *,
        decision: str,
        rating: int,
        title: str,
        body: str,
        context_note: str,
        reviewed: bool,
    ) -> None:
        if decision not in DECISIONS:
            raise ValueError("Invalid Twitter review decision")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValueError("Rating must be an integer from 1 to 5")
        if not all(isinstance(value, str) for value in (entry_key, title, body, context_note)):
            raise ValueError("Twitter review text fields must be strings")
        if len(title) > 200 or len(body) > 100_000 or len(context_note) > 5_000:
            raise ValueError("Twitter review text is too long")
        now = dt.datetime.now(dt.UTC).isoformat()
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE twitter_entries
                SET decision = ?, rating = ?, edited_title = ?, edited_body = ?,
                    context_note = ?, reviewed = ?, updated_at = ?
                WHERE entry_key = ? AND active = 1
                """,
                (decision, rating, title.strip(), body.strip(), context_note.strip(), int(reviewed), now, entry_key),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown or inactive Twitter review entry")

    @staticmethod
    def _view_where(view: str) -> tuple[str, tuple[Any, ...]]:
        if view not in VIEWS:
            raise ValueError("Invalid Twitter review view")
        if view == "recommended":
            return "active = 1 AND auto_excluded = 0 AND reviewed = 0", ()
        if view == "needs_context":
            return "active = 1 AND classification = 'missing_context' AND reviewed = 0", ()
        if view == "included":
            return "active = 1 AND decision = 'include' AND reviewed = 1", ()
        if view == "excluded":
            return "active = 1 AND decision = 'exclude'", ()
        return "active = 1", ()

    def entry_keys(self, view: str = "recommended") -> list[str]:
        where, parameters = self._view_where(view)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT entry_key FROM twitter_entries WHERE {where}
                ORDER BY media_count DESC, rating DESC, created_utc, root_post_id
                """,
                parameters,
            ).fetchall()
        return [str(row[0]) for row in rows]

    def entry(self, entry_key: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM twitter_entries WHERE entry_key = ? AND active = 1",
                (entry_key,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown or inactive Twitter review entry")
            media = connection.execute(
                """
                SELECT media_id, post_id, kind, created_utc, default_position
                FROM twitter_media WHERE entry_key = ? AND active = 1
                ORDER BY default_position, media_id
                """,
                (entry_key,),
            ).fetchall()
        payload = dict(row)
        payload.update({
            "post_ids": json.loads(payload.pop("post_ids_json")),
            "title": payload.pop("edited_title") or "",
            "body": payload.pop("edited_body") if payload["edited_body"] is not None else payload["generated_body"],
            "reviewed": bool(payload["reviewed"]),
            "auto_excluded": bool(payload["auto_excluded"]),
            "media": [dict(item) for item in media],
        })
        return payload

    def entry_payload(self, index: int, view: str = "recommended") -> dict[str, Any]:
        keys = self.entry_keys(view)
        if not keys:
            return {"entry": None, "index": 0, "count": 0, "view": view, "summary": self.summary()}
        safe_index = min(max(index, 0), len(keys) - 1)
        return {
            "entry": self.entry(keys[safe_index]),
            "index": safe_index,
            "count": len(keys),
            "view": view,
            "summary": self.summary(),
        }

    def media_path(self, entry_key: str, media_id: str) -> tuple[Path, str]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT source_path, kind FROM twitter_media
                WHERE entry_key = ? AND media_id = ? AND active = 1
                """,
                (entry_key, media_id),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown Twitter media")
        path = Path(str(row[0]))
        if not path.is_file():
            raise ValueError("Twitter media is no longer available")
        return path, str(row[1])

    def summary(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN auto_excluded = 0 THEN 1 ELSE 0 END) AS recommended_total,
                    SUM(CASE WHEN auto_excluded = 0 AND reviewed = 0 THEN 1 ELSE 0 END) AS recommended_pending,
                    SUM(CASE WHEN classification = 'missing_context' THEN 1 ELSE 0 END) AS missing_context,
                    SUM(CASE WHEN auto_excluded = 1 THEN 1 ELSE 0 END) AS auto_excluded,
                    SUM(CASE WHEN decision = 'include' AND reviewed = 1 THEN 1 ELSE 0 END) AS reviewed_included,
                    SUM(CASE WHEN decision = 'exclude' AND reviewed = 1 THEN 1 ELSE 0 END) AS reviewed_excluded,
                    SUM(media_count) AS media_count
                FROM twitter_entries WHERE active = 1
                """
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def approved_entries(self, limit: int) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Test export limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT entry_key FROM twitter_entries
                WHERE active = 1 AND reviewed = 1 AND decision = 'include'
                ORDER BY rating DESC, media_count DESC, created_utc, root_post_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self.entry(str(row[0])) for row in rows]

    def export_media(self, entry_key: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM twitter_media WHERE entry_key = ? AND active = 1
                ORDER BY default_position, media_id
                """,
                (entry_key,),
            ).fetchall()
        return [dict(row) for row in rows]


def _dayone_date(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _markdown_link(label: str, url: str) -> str:
    safe_label = label.replace("[", "\\[").replace("]", "\\]")
    safe_url = url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    return f"[{safe_label}]({safe_url})"


def _parse_byte_range(value: str, file_size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or file_size <= 0:
        raise ValueError("Invalid media byte range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("Invalid media byte range")
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("Invalid media byte range")
        start, end = max(0, file_size - length), file_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    if start >= file_size or end < start:
        raise ValueError("Invalid media byte range")
    return start, min(end, file_size - 1)


def _entry_text(entry: dict[str, Any], markers: list[str]) -> str:
    sections: list[str] = []
    if markers:
        sections.append("\n".join(markers))
    if entry["title"]:
        sections.append(f"# {entry['title']}")
    if entry["context_note"]:
        sections.append(f"> **Context:** {entry['context_note']}")
    if entry["body"]:
        sections.append(str(entry["body"]))
    if entry["source_url"]:
        sections.append(_markdown_link("View original thread on X", str(entry["source_url"])))
    return "\n\n".join(sections)


def build_reviewed_package(store: TwitterReviewStore, output_dir: Path, *, limit: int = 20) -> dict[str, Any]:
    entries = store.approved_entries(limit)
    if not entries:
        raise ValueError("No reviewed Twitter entries are approved for export")
    journal_entries: list[dict[str, Any]] = []
    zip_media: dict[str, Path] = {}
    manifest_entries: list[dict[str, Any]] = []
    photo_count = 0
    video_count = 0
    for entry in entries:
        media_rows = store.export_media(str(entry["entry_key"]))
        if len(media_rows) > DAYONE_MEDIA_LIMIT:
            raise ValueError(f"Twitter entry {entry['entry_key']} exceeds Day One's media limit")
        item: dict[str, Any] = {
            "uuid": uuid.uuid5(NAMESPACE, str(entry["entry_key"])).hex.upper(),
            "creationDate": _dayone_date(str(entry["created_utc"])),
            "timeZone": str(entry["time_zone"]),
            "tags": ["source:twitter", "twitter:reviewed", f"twitter:{entry['classification']}"],
        }
        markers: list[str] = []
        attachment_manifest: list[dict[str, Any]] = []
        for order, media in enumerate(media_rows):
            path = Path(str(media["source_path"]))
            if not path.is_file() or _sha256(path) != str(media["source_sha256"]):
                raise ValueError(f"Reviewed Twitter media changed or is missing: {media['media_id']}")
            identifier = uuid.uuid5(NAMESPACE, f"{entry['entry_key']}:{media['media_id']}").hex.upper()
            checksum = _md5(path)
            if media["kind"] == "photo":
                media_type = PHOTO_TYPES[path.suffix.lower()]
                folder = "photos"
                descriptor = {
                    "identifier": identifier,
                    "md5": checksum,
                    "type": media_type,
                    "orderInEntry": order,
                    "date": _dayone_date(str(media["created_utc"])),
                    "fileSize": path.stat().st_size,
                }
                item.setdefault("photos", []).append(descriptor)
                photo_count += 1
            else:
                media_type = VIDEO_TYPES[path.suffix.lower()]
                folder = "videos"
                descriptor = {
                    "identifier": identifier,
                    "md5": checksum,
                    "format": media_type,
                    "orderInEntry": order,
                    "date": _dayone_date(str(media["created_utc"])),
                    "fileSize": path.stat().st_size,
                }
                item.setdefault("videos", []).append(descriptor)
                video_count += 1
            zip_name = f"{folder}/{checksum}.{media_type}"
            zip_media[zip_name] = path
            markers.append(f"![](dayone-moment://{identifier})")
            attachment_manifest.append({
                "media_id": media["media_id"],
                "source_sha256": media["source_sha256"],
                "zip_path": zip_name,
                "md5": checksum,
            })
        item["text"] = _entry_text(entry, markers)
        journal_entries.append(item)
        manifest_entries.append({
            "entry_key": entry["entry_key"],
            "root_post_id": entry["root_post_id"],
            "post_ids": entry["post_ids"],
            "classification": entry["classification"],
            "rating": entry["rating"],
            "source_fingerprint": entry["source_fingerprint"],
            "attachments": attachment_manifest,
        })

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    package_path = output_dir / f"twitter_dayone_test_{suffix}.zip"
    manifest_path = output_dir / f"twitter_dayone_test_{suffix}_manifest.json"
    manifest = {
        "schema_version": 1,
        "package_path": str(package_path),
        "entry_count": len(journal_entries),
        "photo_count": photo_count,
        "video_count": video_count,
        "review_summary": store.summary(),
        "entries": manifest_entries,
        "warnings": [
            "Only explicitly reviewed Include decisions are exported.",
            "Structural validation does not prove the visual result in the Day One app.",
        ],
    }
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".zip", delete=False) as temporary:
        temporary_zip = Path(temporary.name)
    with tempfile.NamedTemporaryFile("w", dir=output_dir, suffix=".json", encoding="utf-8", delete=False) as temporary:
        temporary_manifest = Path(temporary.name)
        json.dump(manifest, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
    try:
        with zipfile.ZipFile(temporary_zip, "w", allowZip64=True) as archive:
            archive.writestr(
                "Journal.json",
                json.dumps({"metadata": {"version": "1.0"}, "entries": journal_entries}, ensure_ascii=False),
                compress_type=zipfile.ZIP_DEFLATED,
            )
            for name, path in sorted(zip_media.items()):
                archive.write(path, name, compress_type=zipfile.ZIP_STORED)
        verify_package(temporary_zip)
        temporary_zip.replace(package_path)
        try:
            temporary_manifest.replace(manifest_path)
        except Exception:
            package_path.unlink(missing_ok=True)
            raise
    finally:
        temporary_zip.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
    return {
        "package_path": str(package_path),
        "manifest_path": str(manifest_path),
        "entry_count": len(journal_entries),
        "photo_count": photo_count,
        "video_count": video_count,
        "unique_media_count": len(zip_media),
    }


def verify_package(package_path: Path) -> None:
    with zipfile.ZipFile(package_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Twitter Day One package has a corrupt ZIP member")
        names = archive.namelist()
        if names.count("Journal.json") != 1:
            raise ValueError("Twitter Day One package must contain exactly one Journal.json")
        payload = json.loads(archive.read("Journal.json"))
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError("Twitter Day One package has no entries")
        uuids: set[str] = set()
        identifiers: set[str] = set()
        referenced_members: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("uuid"), str):
                raise ValueError("Twitter Day One package has an invalid entry")
            if entry["uuid"] in uuids:
                raise ValueError("Twitter Day One package has duplicate entry UUIDs")
            uuids.add(entry["uuid"])
            text = str(entry.get("text") or "")
            for field, folder, format_key in (("photos", "photos", "type"), ("videos", "videos", "format")):
                for media in entry.get(field, []) or []:
                    if not isinstance(media, dict):
                        raise ValueError("Twitter Day One package has invalid media metadata")
                    identifier = media.get("identifier")
                    checksum = media.get("md5")
                    media_type = media.get(format_key)
                    if not all(isinstance(value, str) and value for value in (identifier, checksum, media_type)):
                        raise ValueError("Twitter Day One package has incomplete media metadata")
                    if identifier in identifiers:
                        raise ValueError("Twitter Day One package has duplicate media identifiers")
                    identifiers.add(identifier)
                    marker = f"![](dayone-moment://{identifier})"
                    if text.count(marker) != 1:
                        raise ValueError("Twitter Day One media marker does not match metadata")
                    member = f"{folder}/{checksum}.{media_type}"
                    if member not in names:
                        raise ValueError("Twitter Day One package is missing referenced media")
                    if hashlib.md5(archive.read(member), usedforsecurity=False).hexdigest() != checksum:
                        raise ValueError("Twitter Day One package media hash does not match metadata")
                    referenced_members.add(member)
        packaged_media = {name for name in names if name.startswith(("photos/", "videos/"))}
        if packaged_media != referenced_members:
            raise ValueError("Twitter Day One package contains unreferenced media")


@dataclass
class TwitterReviewState:
    store: TwitterReviewStore
    output_dir: Path
    csrf_token: str


def _review_html(csrf_token: str) -> str:
    token = html.escape(csrf_token, quote=True)
    return inject_activity(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Twitter diary review</title>
<style>
:root{{--bg:#f6f6f2;--paper:#fffefa;--ink:#282721;--muted:#77736a;--line:#dedbd0;--accent:#2f6f61;--warn:#a66524;--danger:#9b3f3f}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.48 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{position:sticky;top:0;z-index:4;background:rgba(246,246,242,.96);border-bottom:1px solid var(--line);padding:14px 22px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}}
header h1{{font:600 20px/1.2 Georgia,serif;margin:0}} a{{color:var(--accent)}} .grow{{flex:1}} .summary{{color:var(--muted);font-size:13px}}
.tabs{{display:flex;gap:7px;flex-wrap:wrap}} button,.button{{border:1px solid var(--line);background:white;color:var(--ink);border-radius:9px;padding:9px 13px;font:inherit;cursor:pointer;text-decoration:none}} button.primary{{background:var(--accent);color:white;border-color:var(--accent)}} button.danger{{color:var(--danger)}} button.active{{border-color:var(--accent);box-shadow:0 0 0 2px #d9ebe5}}
main{{max-width:1320px;margin:22px auto;padding:0 18px;display:grid;grid-template-columns:minmax(320px,1.1fr) minmax(340px,.9fr);gap:20px}}
.panel{{background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:0 7px 30px rgba(40,39,33,.06);overflow:hidden}} .panel-body{{padding:20px}}
.media{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;background:#eceae2;min-height:180px}} .media.one{{grid-template-columns:1fr}} .media img,.media video{{display:block;width:100%;height:min(54vh,620px);object-fit:contain;background:#171717}}
.eyebrow{{text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-size:11px;font-weight:700}} .context-warning{{border-left:4px solid var(--warn);background:#fff4df;padding:12px;margin:12px 0;border-radius:6px}}
label{{display:block;margin:13px 0 5px;font-size:12px;font-weight:700;color:var(--muted)}} input,textarea{{width:100%;border:1px solid var(--line);border-radius:9px;padding:10px;background:#fff;font:inherit}} textarea{{min-height:190px;resize:vertical}}
.rating{{display:flex;gap:5px}} .rating button{{font-size:20px;padding:4px 9px;color:#a98325}} .actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}} .nav{{display:flex;align-items:center;gap:8px;margin-top:14px}} .nav span{{flex:1;text-align:center;color:var(--muted)}}
.preview{{white-space:pre-wrap;font-family:Georgia,serif;font-size:17px}} .empty{{padding:60px 25px;text-align:center;color:var(--muted)}} .export{{margin-top:15px;border-top:1px solid var(--line);padding-top:15px;display:flex;gap:8px;align-items:end}} .export input{{width:90px}} .message{{color:var(--muted);font-size:13px;margin-top:8px}}
@media(max-width:820px){{main{{grid-template-columns:1fr}} header{{position:static}}}}
</style></head><body>
<header><a href="http://127.0.0.1:8766/?step=import_hub">← Project365 Control Panel</a><h1>Twitter diary review</h1><div class="grow"></div><div id="summary" class="summary">Loading…</div></header>
<header class="tabs"><button data-view="recommended" class="active">Recommended</button><button data-view="needs_context">Needs context</button><button data-view="included">Included</button><button data-view="excluded">Excluded</button><button data-view="all">All items</button></header>
<main><section class="panel"><div id="media" class="media"></div><div class="panel-body"><div class="eyebrow" id="classification"></div><div id="contextWarning"></div><div class="preview" id="preview">Loading…</div></div></section>
<section class="panel"><div class="panel-body"><div class="eyebrow">Day One preview</div><label for="entryTitle">Optional title</label><input id="entryTitle" maxlength="200"><label for="entryBody">Diary text</label><textarea id="entryBody"></textarea><label for="contextNote">Context note</label><textarea id="contextNote" style="min-height:85px" placeholder="Add the missing context in your own words when needed."></textarea><label>Diary value</label><div id="ratingControls" class="rating"></div><div class="actions"><button class="primary" onclick="save('include',true)">Approve &amp; next</button><button onclick="save('needs_context',true)">Needs context &amp; next</button><button class="danger" onclick="save('exclude',true)">Exclude &amp; next</button><button onclick="save(current?.decision||'pending',false)">Save draft</button></div><div class="nav"><button onclick="move(-1)">Previous</button><span id="position">—</span><button onclick="move(1)">Next</button></div><div class="export"><label>Test set size<input id="exportLimit" type="number" min="1" max="100" value="20"></label><button onclick="buildExport()">Build reviewed test ZIP</button></div><div id="message" class="message"></div></div></section></main>
<script>
const CSRF={json.dumps(token)};let current=null,index=0,view='recommended',rating=3;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
async function request(url,options={{}}){{const response=await fetch(url,options);const payload=await response.json();if(!response.ok)throw new Error(payload.error||'Request failed');return payload}}
function renderRating(){{document.getElementById('ratingControls').innerHTML=[1,2,3,4,5].map(n=>`<button class="${{n===rating?'active':''}}" onclick="rating=${{n}};renderRating()" aria-label="${{n}} stars">★</button>`).join('')}}
function render(payload){{current=payload.entry;index=payload.index;const s=payload.summary;document.getElementById('summary').textContent=`${{s.recommended_pending}} recommended to review · ${{s.reviewed_included}} included · ${{s.media_count}} media`;document.getElementById('position').textContent=payload.count?`${{index+1}} of ${{payload.count}}`:'0 items';if(!current){{document.getElementById('media').innerHTML='';document.getElementById('preview').innerHTML='<div class="empty">This queue is complete.</div>';document.getElementById('classification').textContent='';return}}rating=current.rating;renderRating();document.getElementById('entryTitle').value=current.title;document.getElementById('entryBody').value=current.body;document.getElementById('contextNote').value=current.context_note;document.getElementById('classification').textContent=`${{current.classification.replaceAll('_',' ')}} · ${{current.post_count}} post${{current.post_count===1?'':'s'}} · ${{current.media_count}} media`;document.getElementById('contextWarning').innerHTML=current.classification==='missing_context'?'<div class="context-warning"><strong>Parent post is not in this archive.</strong> Keep excluded unless you add enough context for this to read as a coherent diary entry.</div>':'';document.getElementById('preview').textContent=current.body;const media=document.getElementById('media');media.className='media '+(current.media.length===1?'one':'');media.innerHTML=current.media.map(m=>{{const url=`/media?entry_key=${{encodeURIComponent(current.entry_key)}}&media_id=${{encodeURIComponent(m.media_id)}}`;return m.kind==='video'?`<video controls preload="metadata" src="${{url}}"></video>`:`<img loading="lazy" src="${{url}}" alt="Twitter attachment">`}}).join('')||'<div class="empty">Text-only entry</div>'}}
async function load(){{try{{render(await request(`/api/entry?view=${{encodeURIComponent(view)}}&index=${{index}}`))}}catch(error){{message(error.message)}}}}
function move(delta){{index=Math.max(0,index+delta);load()}}
function message(value){{document.getElementById('message').textContent=value}}
async function save(decision,reviewed){{if(!current)return;try{{await request('/api/review',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{entry_key:current.entry_key,decision,rating,title:document.getElementById('entryTitle').value,body:document.getElementById('entryBody').value,context_note:document.getElementById('contextNote').value,reviewed}})}});message(reviewed?'Decision saved.':'Draft saved.');await load()}}catch(error){{message(error.message)}}}}
async function buildExport(){{try{{const payload=await request('/api/export',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{limit:Number(document.getElementById('exportLimit').value)}})}});message(`Created ${{payload.entry_count}}-entry ZIP: ${{payload.package_path}}`)}}catch(error){{message(error.message)}}}}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{{document.querySelectorAll('[data-view]').forEach(x=>x.classList.remove('active'));button.classList.add('active');view=button.dataset.view;index=0;load()}}));renderRating();load();
</script></body></html>""")


def create_handler(state: TwitterReviewState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TwitterDiaryReview/1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _headers(
            self,
            status: int,
            content_type: str,
            length: int,
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _json(self, payload: object, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(data))
            self.wfile.write(data)

        def _error(self, error: Exception, status: int = 400) -> None:
            self._json({"error": str(error)}, status)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Invalid request length") from error
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("Invalid request body size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON request must be an object")
            return payload

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    data = _review_html(state.csrf_token).encode("utf-8")
                    self._headers(200, "text/html; charset=utf-8", len(data))
                    self.wfile.write(data)
                    return
                if parsed.path == "/api/summary":
                    self._json(state.store.summary())
                    return
                if parsed.path == "/api/entry":
                    view = query.get("view", ["recommended"])[0]
                    index = int(query.get("index", ["0"])[0])
                    self._json(state.store.entry_payload(index, view))
                    return
                if parsed.path == "/media":
                    entry_key = query.get("entry_key", [""])[0]
                    media_id = query.get("media_id", [""])[0]
                    path, _ = state.store.media_path(entry_key, media_id)
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    file_size = path.stat().st_size
                    byte_range = _parse_byte_range(self.headers.get("Range", ""), file_size)
                    if byte_range is None:
                        start, end, status = 0, file_size - 1, HTTPStatus.OK
                        extra = {"Accept-Ranges": "bytes"}
                    else:
                        start, end = byte_range
                        status = HTTPStatus.PARTIAL_CONTENT
                        extra = {
                            "Accept-Ranges": "bytes",
                            "Content-Range": f"bytes {start}-{end}/{file_size}",
                        }
                    with path.open("rb") as handle:
                        handle.seek(start)
                        data = handle.read(end - start + 1)
                    self._headers(status, mime, len(data), extra)
                    self.wfile.write(data)
                    return
                self._error(ValueError("Not found"), HTTPStatus.NOT_FOUND)
            except (ValueError, OSError) as error:
                self._error(error)

        def do_POST(self) -> None:
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), state.csrf_token):
                self._error(ValueError("Invalid CSRF token"), HTTPStatus.FORBIDDEN)
                return
            parsed = urllib.parse.urlparse(self.path)
            try:
                payload = self._body()
                if parsed.path == "/api/review":
                    state.store.review(
                        str(payload.get("entry_key") or ""),
                        decision=str(payload.get("decision") or ""),
                        rating=payload.get("rating"),
                        title=payload.get("title"),
                        body=payload.get("body"),
                        context_note=payload.get("context_note"),
                        reviewed=payload.get("reviewed") is True,
                    )
                    self._json({"ok": True, "summary": state.store.summary()})
                    return
                if parsed.path == "/api/export":
                    limit = payload.get("limit")
                    if isinstance(limit, bool) or not isinstance(limit, int):
                        raise ValueError("Test export limit must be an integer")
                    self._json(build_reviewed_package(state.store, state.output_dir, limit=limit))
                    return
                self._error(ValueError("Not found"), HTTPStatus.NOT_FOUND)
            except (ValueError, OSError, json.JSONDecodeError) as error:
                self._error(error)

    return Handler


def _sync(source: Path, database: Path) -> tuple[TwitterReviewStore, dict[str, Any]]:
    build = _build_candidates(source)
    store = TwitterReviewStore(database)
    sync_summary = store.sync(build.candidates)
    return store, {**build.summary, **sync_summary, **store.summary()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Review a Twitter archive for Day One export")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--database", type=Path, default=Path("Project365Canonical/twitter_dayone_review/review.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("Project365Canonical/exports/twitter_dayone"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8773)
    subparsers.add_parser("sync")
    export = subparsers.add_parser("export")
    export.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    store, summary = _sync(args.source, args.database)
    if args.command == "sync":
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if args.command == "export":
        print(json.dumps(build_reviewed_package(store, args.output_dir, limit=args.limit), indent=2))
        return 0
    state = TwitterReviewState(store, args.output_dir.expanduser().resolve(), secrets.token_urlsafe(32))
    server = ThreadingHTTPServer(
        (args.host, args.port), activity_handler(create_handler(state))
    )
    start_idle_shutdown(server)
    print(f"Twitter diary review: http://{args.host}:{server.server_port}/", flush=True)
    print(json.dumps(summary, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
