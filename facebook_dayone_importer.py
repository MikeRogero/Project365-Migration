#!/usr/bin/env python3
"""Convert a Facebook JSON data export to a Day One JSON ZIP.

Facebook posts, stories, albums, Marketplace listings, comments, and uploaded
media are read. Source files are never changed. A technical manifest accompanies
the import ZIP.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from project365_diarium_exporter import _image_dimensions


NAMESPACE = uuid.UUID("166cc6f6-5755-45a8-9f91-3982cba5eb0a")
PHOTO_TYPES = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".heic": "heic", ".webp": "webp"}
VIDEO_TYPES = {".mp4": "mp4", ".mov": "mov", ".m4v": "m4v"}
DAYONE_MEDIA_LIMIT = 30


@dataclass
class SourceEntry:
    source_id: str
    timestamp: int
    text: str
    media: list[str] = field(default_factory=list)
    location: dict[str, float | str] | None = None
    kind: str = "post"
    date_source: str = "source_timestamp"
    modified_timestamp: int | None = None
    source_url: str = ""
    shared_content_without_original_url: bool = False
    media_captions: dict[str, str] = field(default_factory=dict)
    media_timestamps: dict[str, int] = field(default_factory=dict)


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _web_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    return value if parsed.scheme in {"http", "https"} and parsed.netloc and not any(
        character.isspace() or character in "<>" for character in value
    ) else ""


def _facebook_url(value: str) -> str:
    url = _web_url(value)
    return url if urlparse(url).hostname in {"facebook.com", "www.facebook.com", "m.facebook.com"} else ""


def _resolve_facebook_link(value: str, overrides: dict[str, str]) -> str:
    """Use a verified destination for opaque Facebook download-your-information links."""
    url = _facebook_url(value)
    return overrides.get(url, url)


def _load_post_link_overrides(source: Path) -> dict[str, str]:
    path = source.parent / "facebook_post_link_overrides.json"
    if not path.is_file():
        return {}
    mappings = _load_json(path)
    if not isinstance(mappings, dict):
        raise ValueError(f"Expected a Facebook post link mapping in {path.name}")
    for intermediate, destination in mappings.items():
        if (not isinstance(intermediate, str) or not isinstance(destination, str)
                or not _facebook_url(intermediate) or "/dyi/l/" not in urlparse(intermediate).path
                or not _facebook_url(destination) or "/posts/" not in urlparse(destination).path):
            raise ValueError(f"Invalid Facebook post link mapping in {path.name}")
    return mappings


def _markdown_link(label: str, url: str) -> str:
    safe_label = label.replace("\\", "\\\\").replace("]", "\\]")
    safe_url = url.replace("(", "%28").replace(")", "%29")
    return f"[{safe_label}]({safe_url})"


def _readable_mentions(value: str) -> str:
    # Facebook's @[id:size:name] token identifies a tagged profile, not a post.
    return re.sub(
        r"@\[([1-9]\d*):\d+:([^\]]+)\]",
        lambda match: _markdown_link(
            match.group(2), f"https://www.facebook.com/profile.php?id={match.group(1)}"
        ),
        value,
    )


def _timestamp(value: Any, source_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 4102444800:
        raise ValueError(f"Invalid timestamp in {source_id}")
    return int(value)


def _coordinate(place: Any) -> dict[str, float | str] | None:
    if not isinstance(place, dict):
        return None
    value = place.get("coordinate") or place
    if not isinstance(value, dict):
        return None
    lat, lon = value.get("latitude"), value.get("longitude")
    if isinstance(lat, (int, float)) and not isinstance(lat, bool) and isinstance(lon, (int, float)) and not isinstance(lon, bool):
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            location: dict[str, float | str] = {"latitude": float(lat), "longitude": float(lon)}
            name = _text(place.get("name"))
            if name:
                location["placeName"] = name
            return location
    return None


def _post_entry(record: dict[str, Any], source_id: str, kind: str = "post") -> SourceEntry:
    timestamp = _timestamp(record.get("timestamp"), source_id)
    body = "\n\n".join(dict.fromkeys(
        value for item in _objects(record.get("data"))
        if (value := _text(item.get("post")))
    ))
    title = _text(record.get("title"))
    if title and title not in body:
        body = f"{title}\n\n{body}" if body else title
    tagged_people = [name for item in _objects(record.get("tags")) if (name := _text(item.get("name")))]
    modified_dates = [item.get("update_timestamp") for item in _objects(record.get("data"))
                      if isinstance(item.get("update_timestamp"), (int, float))]
    backdated_dates = [item.get("backdated_timestamp") for item in _objects(record.get("data"))
                       if isinstance(item.get("backdated_timestamp"), (int, float))]
    media: list[str] = []
    media_captions: dict[str, str] = {}
    location = _coordinate(record.get("place"))
    notes: list[str] = []
    missing_shared_url = False
    shared_url_seen = False
    for attachment in _objects(record.get("attachments")):
        for item in _objects(attachment.get("data")):
            media_item = item.get("media")
            if isinstance(media_item, dict):
                uri = _text(media_item.get("uri"))
                if uri and uri not in media:
                    media.append(uri)
                description = _text(media_item.get("description"))
                if uri and description:
                    media_captions[uri] = description
                elif description and description not in body and description not in notes:
                    notes.append(description)
            external = item.get("external_context")
            if isinstance(external, dict):
                name = _text(external.get("name"))
                if name and name not in body and name not in notes:
                    notes.append(name)
                url = _text(external.get("url"))
                shared_url_seen = shared_url_seen or bool(_web_url(url))
                if url and url not in body and url not in notes:
                    notes.append(_markdown_link("Open shared link", url) if _web_url(url) else url)
                elif not url:
                    missing_shared_url = True
            attachment_text = _text(item.get("text"))
            if attachment_text and attachment_text not in body and attachment_text not in notes:
                notes.append(attachment_text)
            location = location or _coordinate(item.get("place"))
            life_event = item.get("life_event")
            if isinstance(life_event, dict):
                life_title = _text(life_event.get("title"))
                if life_title and life_title not in body and life_title not in notes:
                    notes.append(life_title)
                for photo in _objects(life_event.get("photos")):
                    uri = _text(photo.get("uri"))
                    if uri and uri not in media:
                        media.append(uri)
    if notes:
        body = "\n\n".join(part for part in (body, *notes) if part)
    shared_content_without_original_url = missing_shared_url and not shared_url_seen and not media
    if shared_content_without_original_url:
        body += ("\n\n" if body else "") + "Shared content: Facebook export did not include the original link or media."
    if tagged_people:
        body += ("\n\n" if body else "") + "Tagged: " + ", ".join(dict.fromkeys(tagged_people))
    for raw_date in backdated_dates:
        original_date = _date(_timestamp(raw_date, source_id))
        if original_date != _date(timestamp):
            body += ("\n\n" if body else "") + f"Facebook backdated date: {original_date}"
    modified_timestamp = max((_timestamp(value, source_id) for value in modified_dates), default=None)
    return SourceEntry(source_id, timestamp, _readable_mentions(body), media, location, kind,
                       modified_timestamp=modified_timestamp,
                       source_url=_facebook_url(_text(record.get("url"))),
                       shared_content_without_original_url=shared_content_without_original_url,
                       media_captions=media_captions)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


@dataclass
class _HtmlNode:
    tag: str
    classes: frozenset[str] = frozenset()
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_HtmlNode | str] = field(default_factory=list)


class _HtmlTree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = frozenset((dict(attrs).get("class") or "").split())
        node = _HtmlNode(tag, classes, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "hr", "img", "input", "link", "meta", "source"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _html_nodes(node: _HtmlNode, classes: set[str]) -> list[_HtmlNode]:
    found: list[_HtmlNode] = []
    for child in node.children:
        if isinstance(child, _HtmlNode):
            if classes <= child.classes:
                found.append(child)
            found.extend(_html_nodes(child, classes))
    return found


def _html_text(node: _HtmlNode) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        else:
            if child.tag in {"div", "p", "li"} and parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            parts.append(_html_text(child))
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def _html_post_url(node: _HtmlNode) -> str:
    """Use only the date's Facebook link, not unrelated links inside the post body."""
    found: set[str] = set()

    def visit(current: _HtmlNode) -> None:
        if current.tag == "a" and _html_nodes(current, {"_a72d"}):
            url = _facebook_url(current.attrs.get("href", ""))
            if url:
                found.add(url)
        for child in current.children:
            if isinstance(child, _HtmlNode):
                visit(child)

    visit(node)
    return next(iter(found)) if len(found) == 1 else ""


def _supplemental_roots(additional_exports: Path) -> list[Path]:
    if not additional_exports.is_dir():
        raise ValueError(f"Additional exports folder does not exist: {additional_exports}")
    if (additional_exports / "your_activity_across_facebook" / "posts").is_dir() or (additional_exports / "posts").is_dir():
        return [additional_exports]
    return sorted(path for path in additional_exports.iterdir() if path.is_dir())


def _supplemental_posts(roots: list[Path], entries: list[SourceEntry]) -> tuple[list[SourceEntry], int, int]:
    json_dates = {entry.timestamp for entry in entries if entry.kind == "post"}
    added: list[SourceEntry] = []
    inferred_offsets: set[int] = set()
    html_links: dict[int, set[str]] = {}
    for root in roots:
        for path in sorted(root.glob("**/posts/your_posts*.html")):
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"HTML post file outside additional export: {path.name}")
            parser = _HtmlTree()
            parser.feed(path.read_text(encoding="utf-8-sig"))
            cards: list[tuple[int, str, str]] = []
            for node in _html_nodes(parser.root, {"_3-95", "_a6-g"}):
                if not _html_nodes(node, {"_2ph_", "_a6-p"}):
                    continue
                dates = _html_nodes(node, {"_a72d"})
                if len(dates) != 1:
                    raise ValueError(f"HTML post has no unambiguous date: {path.name}")
                try:
                    local = dt.datetime.strptime(_html_text(dates[0]), "%b %d, %Y %I:%M:%S%p")
                except ValueError as error:
                    raise ValueError(f"Unrecognized HTML post date in {path.name}") from error
                headers = _html_nodes(node, {"_2pin"})
                bodies = _html_nodes(node, {"_2ph_", "_a6-h", "_a6-i"})
                parts = [text for text in (_html_text(headers[0]) if headers else "",
                                           _html_text(bodies[0]) if bodies else "") if text]
                cards.append((calendar.timegm(local.timetuple()), "\n\n".join(dict.fromkeys(parts)),
                              _html_post_url(node)))
            if not cards:
                continue
            # Facebook's HTML dates omit a timezone. Infer one fixed offset from
            # posts that also exist in the timestamped JSON, or fail closed.
            matches = {hours: sum(local - hours * 3600 in json_dates for local, _, _ in cards)
                       for hours in range(-14, 15)}
            offset = max(matches, key=matches.get)
            if matches[offset] < max(2, int(0.8 * min(len(cards), len(json_dates)))):
                raise ValueError(f"Cannot infer HTML post timezone from JSON overlap: {path.name}")
            inferred_offsets.add(offset)
            for index, (local, body, url) in enumerate(cards):
                timestamp = local - offset * 3600
                if url:
                    html_links.setdefault(timestamp, set()).add(url)
                if timestamp not in json_dates:
                    added.append(SourceEntry(f"html:{path.relative_to(root)}:{index}", timestamp,
                                             _readable_mentions(body), kind="post", date_source="html_timezone_inferred"))
                    json_dates.add(timestamp)
    if len(inferred_offsets) > 1:
        raise ValueError("Additional HTML exports disagree on timezone")
    entries_by_time: dict[int, list[SourceEntry]] = {}
    for entry in (*entries, *added):
        if entry.kind == "post":
            entries_by_time.setdefault(entry.timestamp, []).append(entry)
    ambiguous = 0
    for timestamp, urls in html_links.items():
        matches = entries_by_time.get(timestamp, [])
        if len(matches) != 1 or len(urls) != 1:
            ambiguous += 1
        elif not matches[0].source_url:
            matches[0].source_url = next(iter(urls))
    return added, next(iter(inferred_offsets), 0), ambiguous


def _recover_external_media(roots: list[Path], external_uris: set[str]) -> dict[str, Path]:
    videos = [path for root in roots for path in root.glob("**/posts/media/**/*")
              if path.is_file() and path.resolve().is_relative_to(root.resolve())
              and path.suffix.lower() in VIDEO_TYPES]
    recovered: dict[str, Path] = {}
    for uri in external_uris:
        basename = Path(urlparse(uri).path).name
        stem, extension = Path(basename).stem, Path(basename).suffix.lower()
        if extension not in VIDEO_TYPES or not stem.endswith("_n"):
            continue
        matches = {path.resolve() for path in videos
                   if path.suffix.lower() == extension and path.stem.startswith(stem + "_")}
        if len(matches) > 1:
            raise ValueError(f"Ambiguous supplemental video for {basename}")
        if matches:
            recovered[uri] = matches.pop()
    return recovered


def _find_posts_dir(source: Path) -> Path:
    choices = (source / "your_activity_across_facebook" / "posts", source / "posts")
    for choice in choices:
        if choice.is_dir() and list(choice.glob("your_posts*.json")):
            return choice
    raise ValueError("No Facebook JSON post files found; choose the extracted JSON export root")


def _source_entries(source: Path) -> tuple[list[SourceEntry], dict[str, int], set[str]]:
    posts_dir = _find_posts_dir(source)
    entries: list[SourceEntry] = []
    counts = {"posts": 0, "stories": 0, "orphan_photos": 0, "orphan_videos": 0,
              "unindexed_photos": 0, "marketplace_listings": 0, "unlinked_comments": 0,
              "album_photos": 0}
    for path in sorted(posts_dir.glob("your_posts*.json")):
        records = _load_json(path)
        if not isinstance(records, list):
            raise ValueError(f"Expected a JSON array in {path.name}")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"Invalid post record in {path.name}: {index}")
            source_id = f"{path.relative_to(source)}:{index}"
            entries.append(_post_entry(record, source_id))
            counts["posts"] += 1

    story_path = source / "your_activity_across_facebook" / "stories" / "archived_stories.json"
    if story_path.is_file():
        stories = _load_json(story_path).get("archived_stories_v2", [])
        for index, record in enumerate(_objects(stories)):
            entries.append(_post_entry(record, f"{story_path.relative_to(source)}:{index}", "story"))
            counts["stories"] += 1

    marketplace_path = source / "your_activity_across_facebook" / "facebook_marketplace" / "items_sold.json"
    if marketplace_path.is_file():
        payload = _load_json(marketplace_path)
        if not isinstance(payload, dict) or not isinstance(payload.get("items_selling_v2"), list):
            raise ValueError(f"Expected Marketplace listings in {marketplace_path.name}")
        for index, record in enumerate(_objects(payload["items_selling_v2"])):
            source_id = f"{marketplace_path.relative_to(source)}:{index}"
            timestamp = _timestamp(record.get("created_timestamp"), source_id)
            details = ["Facebook Marketplace listing"]
            for field, label in (("title", "Item"), ("description", "Description"),
                                 ("price", "Price"), ("category", "Category")):
                value = _text(record.get(field))
                if value:
                    details.append(f"{label}: {value}")
            updated = record.get("updated_timestamp")
            entries.append(SourceEntry(
                source_id, timestamp, "\n\n".join(details),
                location=_coordinate(record.get("location")), kind="marketplace",
                modified_timestamp=_timestamp(updated, source_id) if updated is not None else None,
            ))
            counts["marketplace_listings"] += 1

    comments_path = source / "your_activity_across_facebook" / "comments_and_reactions" / "comments.json"
    if comments_path.is_file():
        payload = _load_json(comments_path)
        if not isinstance(payload, dict) or not isinstance(payload.get("comments_v2"), list):
            raise ValueError(f"Expected Facebook comments in {comments_path.name}")
        for record in _objects(payload["comments_v2"]):
            for data in _objects(record.get("data")):
                comment = data.get("comment")
                if isinstance(comment, dict) and _text(comment.get("comment")):
                    # No parent post ID, URL, or thread ID: do not create
                    # contextless Day One entries or guess a parent by date.
                    counts["unlinked_comments"] += 1

    attached_entries: dict[str, list[SourceEntry]] = {}
    for entry in entries:
        for uri in entry.media:
            attached_entries.setdefault(uri, []).append(entry)
    attached_uris = set(attached_entries)
    seen_media: set[str] = set()
    for path in sorted((posts_dir / "album").glob("*.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object in {path.name}")
        album_name = _text(payload.get("name"))
        album_description = _text(payload.get("description"))
        photos = _objects(payload.get("photos"))
        if isinstance(payload.get("cover_photo"), dict):
            photos.append(payload["cover_photo"])
        media: list[str] = []
        captions: dict[str, str] = {}
        media_dates: dict[str, int] = {}
        date_source = "album_first_photo"
        for index, photo in enumerate(photos):
            uri = _text(photo.get("uri"))
            if not uri:
                continue
            photo_id = f"{path.relative_to(source)}:{index}"
            raw_date = photo.get("creation_timestamp")
            if raw_date is None:
                raw_date = payload.get("last_modified_timestamp")
                date_source = "album_last_modified"
            photo_date = _timestamp(raw_date, photo_id)
            description = _text(photo.get("description"))
            title = _text(photo.get("title"))
            caption = description or (title if title != album_name else "")
            if uri in attached_uris:
                for attached in attached_entries[uri]:
                    if caption:
                        attached.media_captions.setdefault(uri, caption)
                    attached.media_timestamps.setdefault(uri, photo_date)
                continue
            if uri in seen_media:
                continue
            media.append(uri)
            if caption:
                captions[uri] = caption
            media_dates[uri] = photo_date
            seen_media.add(uri)
        if media:
            header = f"Album: {album_name}" if album_name else "Facebook photo album"
            body = "\n\n".join(part for part in (header, album_description) if part)
            entries.append(SourceEntry(
                str(path.relative_to(source)), min(media_dates.values()), body, media,
                kind="album", date_source=date_source, media_captions=captions,
                media_timestamps=media_dates,
            ))
            counts["album_photos"] += len(media)

    media_sources: list[tuple[Path, str, str]] = [
        (posts_dir / "your_uncategorized_photos.json", "other_photos_v2", "photo"),
        (posts_dir / "your_videos.json", "videos_v2", "video"),
    ]
    for path, key, kind in media_sources:
        if not path.is_file():
            continue
        payload = _load_json(path)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object in {path.name}")
        records = _objects(payload.get(key))
        for index, item in enumerate(records):
            uri = _text(item.get("uri"))
            if not uri or uri in attached_uris or uri in seen_media:
                continue
            source_id = f"{path.relative_to(source)}:{index}"
            text = _text(item.get("description")) or _text(item.get("title"))
            if not text:
                text = "Facebook photo" if kind == "photo" else "Facebook video"
            raw_timestamp = item.get("creation_timestamp")
            timestamp = _timestamp(raw_timestamp, source_id)
            entries.append(SourceEntry(source_id, timestamp, text, [uri], kind=kind))
            counts[f"orphan_{kind}s"] += 1
            seen_media.add(uri)
    entries, suppressed_uris, grouped_listings, linked_photos, visual_duplicates = _group_marketplace(entries, source)
    counts["marketplace_grouped_listings"] = grouped_listings
    counts["marketplace_linked_photos"] = linked_photos
    counts["marketplace_duplicate_photos"] = len(suppressed_uris)
    counts["marketplace_visual_duplicate_photos"] = visual_duplicates
    entries.sort(key=lambda entry: (entry.timestamp, entry.source_id))
    return entries, counts, suppressed_uris


def _visual_signature(path: Path) -> tuple[tuple[int, int], bytes] | None:
    """Decode a small grayscale preview when ImageMagick is available."""
    if not shutil.which("magick"):
        return None
    with path.open("rb") as handle:
        dimensions = _image_dimensions(handle.read(2 * 1024 * 1024), path.suffix.lower().lstrip("."))
    if not all(dimensions):
        return None
    try:
        result = subprocess.run(
            ["magick", str(path), "-auto-orient", "-resize", "32x32!", "-colorspace", "Gray",
             "-depth", "8", "gray:-"], capture_output=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (dimensions, result.stdout) if len(result.stdout) == 1024 else None


def _same_visual_photo(first: tuple[tuple[int, int], bytes], second: tuple[tuple[int, int], bytes]) -> bool:
    if first[0] != second[0]:
        return False
    differences = [abs(a - b) for a, b in zip(first[1], second[1])]
    return sum(differences) <= 4 * 1024 and sum(value > 32 for value in differences) <= 10


def _group_marketplace(entries: list[SourceEntry], source: Path) -> tuple[list[SourceEntry], set[str], int, int, int]:
    """Merge repeated listing records and attach uniquely timed uncategorized photos."""
    groups: list[tuple[SourceEntry, list[int]]] = []
    grouped_ids: set[str] = set()
    for entry in sorted((item for item in entries if item.kind == "marketplace"),
                        key=lambda item: (item.timestamp, item.source_id)):
        match = next(((listing, dates) for listing, dates in groups
                      if listing.text == entry.text and entry.timestamp - dates[-1] <= 3 * 86400), None)
        if match is None:
            groups.append((entry, [entry.timestamp]))
        else:
            listing, dates = match
            dates.append(entry.timestamp)
            listing.modified_timestamp = max(filter(None, (listing.modified_timestamp,
                                                           entry.modified_timestamp)), default=None)
            grouped_ids.add(entry.source_id)

    removed: set[str] = set(grouped_ids)
    suppressed_uris: set[str] = set()
    digests: dict[str, set[str]] = {listing.source_id: set() for listing, _ in groups}
    signatures: dict[str, list[tuple[tuple[int, int], bytes]]] = {listing.source_id: [] for listing, _ in groups}
    linked_photos = 0
    visual_duplicates = 0
    for entry in entries:
        if entry.kind != "photo" or len(entry.media) != 1 or entry.text != "Facebook photo":
            continue
        matches = [(listing, min(abs(entry.timestamp - timestamp) for timestamp in dates))
                   for listing, dates in groups
                   if min(abs(entry.timestamp - timestamp) for timestamp in dates) <= 900]
        if len(matches) != 1:
            continue
        listing = matches[0][0]
        uri = entry.media[0]
        path = _media_path(source, uri)
        if path is None or not path.is_file():
            continue
        digest = _md5(path)
        if digest in digests[listing.source_id]:
            suppressed_uris.add(uri)
        else:
            digests[listing.source_id].add(digest)
            signature = _visual_signature(path)
            if signature is not None and any(_same_visual_photo(signature, old)
                                             for old in signatures[listing.source_id]):
                suppressed_uris.add(uri)
                visual_duplicates += 1
            else:
                listing.media.append(uri)
                listing.media_timestamps[uri] = entry.timestamp
                linked_photos += 1
                if signature is not None:
                    signatures[listing.source_id].append(signature)
        removed.add(entry.source_id)
    return ([entry for entry in entries if entry.source_id not in removed], suppressed_uris,
            len(grouped_ids), linked_photos, visual_duplicates)


def _group_album_days(entries: list[SourceEntry], offset_hours: int) -> list[SourceEntry]:
    """Keep long-running albums on the correct calendar days in Day One."""
    timezone = dt.timezone(dt.timedelta(hours=offset_hours))
    grouped: list[SourceEntry] = []
    for entry in entries:
        if entry.kind != "album":
            grouped.append(entry)
            continue
        days: dict[str, list[str]] = {}
        for uri in sorted(entry.media, key=lambda item: entry.media_timestamps[item]):
            day = dt.datetime.fromtimestamp(entry.media_timestamps[uri], timezone).strftime("%Y-%m-%d")
            days.setdefault(day, []).append(uri)
        for day, media in sorted(days.items()):
            grouped.append(SourceEntry(
                f"{entry.source_id}:{day}", min(entry.media_timestamps[uri] for uri in media),
                entry.text, media, entry.location, entry.kind, entry.date_source,
                media_captions={uri: entry.media_captions[uri] for uri in media if uri in entry.media_captions},
                media_timestamps={uri: entry.media_timestamps[uri] for uri in media},
            ))
    return grouped


def _limit_entry_media(entries: list[SourceEntry]) -> tuple[list[SourceEntry], int]:
    """Day One personal journals support at most 30 attachments per entry."""
    limited: list[SourceEntry] = []
    split_groups = 0
    for entry in entries:
        if len(entry.media) <= DAYONE_MEDIA_LIMIT:
            limited.append(entry)
            continue
        split_groups += 1
        part_count = (len(entry.media) + DAYONE_MEDIA_LIMIT - 1) // DAYONE_MEDIA_LIMIT
        for part in range(part_count):
            media = entry.media[part * DAYONE_MEDIA_LIMIT:(part + 1) * DAYONE_MEDIA_LIMIT]
            label = f"Part {part + 1} of {part_count}"
            body = entry.text if part == 0 or entry.kind == "album" else "Facebook post continued"
            limited.append(SourceEntry(
                entry.source_id if part == 0 else f"{entry.source_id}:part:{part + 1}",
                entry.timestamp, f"{body}\n\n{label}", media, entry.location, entry.kind,
                entry.date_source, entry.modified_timestamp, entry.source_url,
                entry.shared_content_without_original_url,
                {uri: entry.media_captions[uri] for uri in media if uri in entry.media_captions},
                {uri: entry.media_timestamps[uri] for uri in media if uri in entry.media_timestamps},
            ))
    return limited, split_groups


def _media_path(source: Path, uri: str) -> Path | None:
    if re.match(r"^https?://", uri, re.I):
        return None
    path = (source / uri).resolve()
    if not path.is_relative_to(source.resolve()):
        raise ValueError(f"Media path outside the export: {uri}")
    return path


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_type(path: Path) -> tuple[str, str, str]:
    extension = path.suffix.lower()
    if extension in PHOTO_TYPES:
        return "photos", "photos", PHOTO_TYPES[extension]
    if extension in VIDEO_TYPES:
        return "videos", "videos", VIDEO_TYPES[extension]
    if not extension:
        with path.open("rb") as handle:
            header = handle.read(12)
        if len(header) >= 8 and header[4:8] == b"ftyp":
            return "videos", "videos", "mp4"
    raise ValueError(f"Unsupported Facebook media type: {path.relative_to(path.anchor)}")


def _date(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dayone_timezone(offset_hours: int) -> str:
    if not offset_hours:
        return "Etc/UTC"
    # IANA Etc/GMT signs are reversed relative to the UTC offset.
    return f"Etc/GMT{'-' if offset_hours > 0 else '+'}{abs(offset_hours)}"


def _looks_mojibake(value: str) -> bool:
    return _repair_text(value) != value or bool(re.search(r"Ã[\u0080-\u00bf]|â[\u0080-\u00bf€]|ðŸ", value))


_WINDOWS_1252_BYTES = {
    character: byte for byte in range(128, 160)
    if (character := bytes([byte]).decode("cp1252", errors="ignore"))
}


def _repair_text(value: str) -> str:
    """Reverse valid UTF-8 sequences misread as Latin-1/Windows-1252.

    Work on byte-shaped spans, preserving correct Unicode and malformed text
    verbatim. Facebook source files are never rewritten.
    """
    def source_byte(character: str) -> int | None:
        return ord(character) if ord(character) <= 255 else _WINDOWS_1252_BYTES.get(character)

    for _ in range(2):  # Facebook exports can contain twice-decoded text.
        repaired: list[str] = []
        index = 0
        changed = False
        while index < len(value):
            lead = source_byte(value[index])
            length = 2 if lead is not None and 0xC2 <= lead <= 0xDF else (
                3 if lead is not None and 0xE0 <= lead <= 0xEF else (
                    4 if lead is not None and 0xF0 <= lead <= 0xF4 else 0
                )
            )
            if length and index + length <= len(value):
                encoded = [source_byte(character) for character in value[index:index + length]]
                if all(byte is not None for byte in encoded):
                    try:
                        decoded = bytes(encoded).decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                    else:
                        if decoded.isprintable():
                            repaired.append(decoded)
                            index += length
                            changed = True
                            continue
            repaired.append(value[index])
            index += 1
        if not changed:
            break
        value = "".join(repaired)
    return value


def verify_media_hashes(package_path: Path) -> int:
    """Read every packaged asset and verify its content-addressed filename."""
    checked = 0
    with zipfile.ZipFile(package_path) as archive:
        for name in archive.namelist():
            if not name.startswith(("photos/", "videos/")):
                continue
            digest = hashlib.md5()
            with archive.open(name) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != Path(name).stem:
                raise ValueError(f"Packaged media hash does not match: {name}")
            checked += 1
    return checked


def build_package(source: Path, output_dir: Path, *, additional_exports: Path | None = None) -> dict[str, Any]:
    """Build a new package; raise before publication if any local media is missing."""
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Facebook export folder does not exist: {source}")
    entries, counts, suppressed_uris = _source_entries(source)
    if not entries:
        raise ValueError("No Facebook entries found")
    roots = _supplemental_roots(additional_exports.expanduser().resolve()) if additional_exports else []
    if additional_exports and not roots:
        raise ValueError("Additional exports folder contains no export folders")
    supplemental_posts, html_offset, ambiguous_html_links = _supplemental_posts(roots, entries) if roots else ([], 0, 0)
    entries.extend(supplemental_posts)
    counts["supplemental_posts"] = len(supplemental_posts)
    post_link_overrides = _load_post_link_overrides(source)
    resolved_post_links = 0
    for entry in entries:
        resolved = _resolve_facebook_link(entry.source_url, post_link_overrides)
        resolved_post_links += bool(resolved and resolved != entry.source_url)
        entry.source_url = resolved
    linked_post_count = sum(bool(entry.source_url) for entry in entries)
    unresolved_intermediate_links = sum("/dyi/l/" in urlparse(entry.source_url).path
                                        for entry in entries if entry.source_url)
    entries = _group_album_days(entries, html_offset)
    entries, split_groups = _limit_entry_media(entries)
    counts["album_entries"] = sum(entry.kind == "album" for entry in entries)
    counts["media_split_groups"] = split_groups

    media_paths: dict[str, Path] = {}
    external_uris: set[str] = set()
    all_uris = {uri for entry in entries for uri in entry.media}
    external_candidates = {uri for uri in all_uris if re.match(r"^https?://", uri, re.I)}
    recovered = _recover_external_media(roots, external_candidates) if roots else {}
    variants: set[str] = set()
    for root in roots:
        for media in root.glob("**/posts/media/**/*"):
            if media.is_file() and not media.name.startswith("."):
                relative = media.relative_to(root)
                original = source / relative
                if original.is_file() and media.stat().st_size != original.stat().st_size:
                    variants.add(str(relative))
    for entry in entries:
        for uri in entry.media:
            path = _media_path(source, uri)
            if path is None:
                if uri in recovered:
                    media_paths[uri] = recovered[uri]
                else:
                    external_uris.add(uri)
            elif not path.is_file():
                raise ValueError(f"Facebook export has missing media: {uri}")
            else:
                media_paths[uri] = path
    media_root = _find_posts_dir(source) / "media"
    referenced_paths = {path for path in media_paths.values() if path.is_relative_to(source)}
    referenced_paths.update(path for uri in suppressed_uris
                            if (path := _media_path(source, uri)) is not None)
    unindexed_paths = sorted(
        path.resolve()
        for path in media_root.rglob("*")
        if path.is_file() and not path.name.startswith(".") and path.resolve() not in referenced_paths
    ) if media_root.is_dir() else []
    for path in unindexed_paths:
        if path.suffix.lower() not in PHOTO_TYPES:
            raise ValueError(f"Unindexed Facebook media has an unsupported type: {path.name}")
        uri = str(path.relative_to(source))
        entries.append(SourceEntry(
            source_id=f"unindexed:{uri}",
            timestamp=_timestamp(path.stat().st_mtime, uri),
            text="Facebook media without a matching JSON record. Date is file modification time, not a confirmed capture or post date.",
            media=[uri], kind="photo", date_source="file_modified",
        ))
        media_paths[uri] = path
        counts["unindexed_photos"] += 1
    entries.sort(key=lambda entry: (entry.timestamp, entry.source_id))
    timezone = _dayone_timezone(html_offset)

    repaired_text_entries = 0
    for entry in entries:
        repaired = _readable_mentions(_repair_text(entry.text))
        changed = repaired != entry.text
        entry.text = repaired
        for uri, caption in entry.media_captions.items():
            fixed = _readable_mentions(_repair_text(caption))
            changed |= fixed != caption
            entry.media_captions[uri] = fixed
        repaired_text_entries += changed
        if entry.location and isinstance(entry.location.get("placeName"), str):
            entry.location["placeName"] = _repair_text(entry.location["placeName"])

    journal_entries: list[dict[str, Any]] = []
    zip_media: dict[str, Path] = {}
    manifest: list[dict[str, Any]] = []
    attachment_count = 0
    for entry in entries:
        entry_uuid = uuid.uuid5(NAMESPACE, entry.source_id).hex.upper()
        item: dict[str, Any] = {
            "uuid": entry_uuid,
            "creationDate": _date(entry.timestamp),
            "timeZone": timezone,
            "text": entry.text,
            "tags": ["source:facebook", f"facebook:{entry.kind}"],
        }
        if entry.date_source != "source_timestamp":
            item["tags"].append("facebook:date-estimated")
        if entry.location:
            item["location"] = entry.location
        if entry.modified_timestamp and entry.modified_timestamp >= entry.timestamp:
            item["modifiedDate"] = _date(entry.modified_timestamp)
        if entry.source_url:
            item["text"] += ("\n\n" if item["text"] else "") + _markdown_link("View this Facebook post", entry.source_url)
        for order, uri in enumerate(entry.media):
            if uri in external_uris:
                if uri not in item["text"]:
                    item["text"] += ("\n\n" if item["text"] else "") + uri
                continue
            path = media_paths[uri]
            field, folder, media_type = _media_type(path)
            md5 = _md5(path)
            name = f"{folder}/{md5}.{media_type}"
            zip_media[name] = path
            identifier = uuid.uuid5(NAMESPACE, f"{entry.source_id}:{uri}").hex.upper()
            media_item: dict[str, Any] = {
                "identifier": identifier, "md5": md5, "orderInEntry": order,
                "date": _date(entry.media_timestamps.get(uri, entry.timestamp)), "fileSize": path.stat().st_size,
            }
            if field == "photos":
                media_item["type"] = media_type
                with path.open("rb") as handle:
                    width, height = _image_dimensions(handle.read(2 * 1024 * 1024), media_type)
                if width and height:
                    media_item.update(width=width, height=height)
            else:
                media_item["format"] = media_type
            item.setdefault(field, []).append(media_item)
            caption = entry.media_captions.get(uri)
            if caption:
                item["text"] += ("\n\n" if item["text"] else "") + caption
            marker = f"![](dayone-moment://{identifier})"
            item["text"] += ("\n" if item["text"] else "") + marker
            manifest.append({"source_id": entry.source_id, "source_uri": uri, "date_source": entry.date_source, "zip_path": name,
                             "md5": md5, "bytes": path.stat().st_size,
                             "media_date": _date(entry.media_timestamps.get(uri, entry.timestamp))})
            attachment_count += 1
        journal_entries.append(item)

    output_dir = output_dir.expanduser().resolve()
    if output_dir.is_relative_to(source):
        raise ValueError("Output folder must be outside the Facebook export")
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    package_path = output_dir / f"facebook_dayone_{suffix}.zip"
    manifest_path = output_dir / f"facebook_dayone_{suffix}_manifest.json"
    if package_path.exists() or manifest_path.exists():
        raise FileExistsError("A Facebook package with this timestamp already exists")
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".zip", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", allowZip64=True) as archive:
            archive.writestr("Journal.json", json.dumps(
                {"metadata": {"version": "1.0"}, "entries": journal_entries}, ensure_ascii=False
            ), compress_type=zipfile.ZIP_DEFLATED)
            for name, path in sorted(zip_media.items()):
                archive.write(path, name, compress_type=zipfile.ZIP_STORED)
        verify_media_hashes(temporary_path)
        temporary_path.replace(package_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    summary: dict[str, Any] = {
        "package_path": str(package_path), "manifest_path": str(manifest_path),
        "entry_count": len(journal_entries), "attachment_count": attachment_count,
        "unique_media_count": len(zip_media), "entries_with_location": sum(bool(item.get("location")) for item in journal_entries),
        "external_media_links": len(external_uris), "source_counts": counts,
        "album_entries": counts["album_entries"], "unlinked_comments": counts["unlinked_comments"],
        "media_split_groups": counts["media_split_groups"],
        "recovered_external_media_links": len(recovered),
        "html_timezone_inferred_hours": html_offset if roots else None,
        "latest_post_date": max(
            (dt.datetime.fromtimestamp(entry.timestamp, dt.timezone(dt.timedelta(hours=html_offset)))
             .strftime("%Y-%m-%d") for entry in entries if entry.kind == "post"), default=""
        ),
        "additional_exports": str(additional_exports) if additional_exports else None,
        "supplemental_media_variants": len(variants),
        "supplemental_variant_paths": sorted(variants),
        "estimated_date_entries": sum(entry.date_source != "source_timestamp" for entry in entries),
        "text_encoding_review_entries": sum(_looks_mojibake(item["text"]) for item in journal_entries),
        "text_encoding_repaired_entries": repaired_text_entries,
        "unindexed_media_files": len(unindexed_paths),
        "facebook_post_links": linked_post_count,
        "resolved_post_links": resolved_post_links,
        "unresolved_intermediate_links": unresolved_intermediate_links,
        "ambiguous_html_post_links": ambiguous_html_links,
        "shared_content_without_original_url": sum(entry.shared_content_without_original_url for entry in entries),
        "unindexed_media_paths": [str(path.relative_to(source)) for path in unindexed_paths],
        "external_media_uris": sorted(external_uris),
        "manifest": manifest,
    }
    manifest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {key: value for key, value in summary.items() if key not in {"manifest", "external_media_uris", "unindexed_media_paths", "supplemental_variant_paths"}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Day One import ZIP from a Facebook JSON export")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--additional-exports", type=Path, help="Optional folder of other Facebook exports")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = build_package(args.source, args.output_dir, additional_exports=args.additional_exports)
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
