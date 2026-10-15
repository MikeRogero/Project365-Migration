#!/usr/bin/env python3
"""Local review page for representative photos on photo-less Swarm entries."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import ipaddress
import json
import mimetypes
import re
import secrets
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from swarm_dayone_importer import Entry, Photo, _build_entries, _export_root


MAX_CANDIDATES = 4
MAX_DROP_BYTES = 50 * 1024 * 1024
SAME_VISIT_WINDOW_SECONDS = 7 * 24 * 60 * 60
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WIKIMEDIA_USER_AGENT = (
    "Project365SwarmReviewBot/1.0 "
    "(https://github.com/MikeRogero/Project365-Migration)"
)
ALLOWED_COMMONS_LICENSES = ("CC BY", "CC0", "PUBLIC DOMAIN", "PDM")
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = "Project365-Swarm-Representative-Photo-Review/1.0"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
OPENSTREETMAP_ATTRIBUTION = "© OpenStreetMap contributors"
OPENSTREETMAP_ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"
GOOGLE_IMAGES_SEARCH_URL = "https://www.google.com/search?tbm=isch"


@dataclass(frozen=True)
class BackfillCandidate:
    photo: Photo
    source_entry: Entry
    rationale: str


@dataclass(frozen=True)
class ReviewTarget:
    entry: Entry
    candidates: tuple[BackfillCandidate, ...]


def build_review_targets(entries: list[Entry], max_candidates: int = MAX_CANDIDATES) -> list[ReviewTarget]:
    if max_candidates <= 0:
        raise ValueError("Candidate limit must be positive")
    photos_by_venue: dict[str, list[tuple[Entry, Photo]]] = defaultdict(list)
    for source_entry in entries:
        if not source_entry.venue_id:
            continue
        for photo in source_entry.photos:
            photos_by_venue[source_entry.venue_id].append((source_entry, photo))

    targets: list[ReviewTarget] = []
    for entry in entries:
        if entry.kind not in {"checkin", "tip"} or entry.photos:
            continue
        ranked: list[tuple[tuple[Any, ...], BackfillCandidate]] = []
        seen_photo_ids: set[str] = set()
        for source_entry, photo in photos_by_venue.get(entry.venue_id, []):
            if photo.source_id in seen_photo_ids:
                continue
            seen_photo_ids.add(photo.source_id)
            relation_priority = {"v": 0, "checkin": 1, "item": 2}.get(photo.related_kind, 3)
            time_distance = abs((photo.created_utc - entry.created_utc).total_seconds())
            pixels = (photo.width or 0) * (photo.height or 0)
            rank = (
                relation_priority,
                time_distance,
                -pixels,
                photo.created_utc,
                photo.source_id,
            )
            relation = {
                "v": "Venue photo",
                "checkin": "Photo from another check-in",
                "item": "Photo from another tip",
            }.get(photo.related_kind, "Exact-venue photo")
            ranked.append((rank, BackfillCandidate(
                photo=photo,
                source_entry=source_entry,
                rationale=f"{relation} at the exact same venue",
            )))
        ranked.sort(key=lambda item: item[0])
        targets.append(ReviewTarget(
            entry=entry,
            candidates=tuple(candidate for _, candidate in ranked[:max_candidates]),
        ))
    return sorted(targets, key=lambda target: (target.entry.created_utc, target.entry.source_key))


def _search_title(title: str) -> str:
    return str(title or "").strip().removeprefix("Tip for ").strip()


def _google_maps_url(entry: Entry) -> str:
    if entry.latitude is not None and entry.longitude is not None:
        query = f"{entry.latitude:.8f},{entry.longitude:.8f}"
    elif (title := _search_title(entry.title)) not in {"", "Swarm check-in", "Foursquare tip"}:
        query = title
    else:
        return ""
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({
        "api": "1",
        "query": query,
    })


def _google_maps_embed_url(entry: Entry) -> str:
    if entry.latitude is not None and entry.longitude is not None:
        query = f"{entry.latitude:.8f},{entry.longitude:.8f}"
    elif (title := _search_title(entry.title)) not in {"", "Swarm check-in", "Foursquare tip"}:
        query = title
    else:
        return ""
    return "https://maps.google.com/maps?" + urllib.parse.urlencode({
        "q": query,
        "output": "embed",
    })


def _foursquare_url(entry: Entry) -> str:
    for label, url in entry.links:
        if label == "Foursquare venue" and url:
            parsed = urllib.parse.urlsplit(url)
            hostname = (parsed.hostname or "").lower()
            if (parsed.scheme == "https"
                    and (hostname == "foursquare.com" or hostname.endswith(".foursquare.com"))):
                path = parsed.path.rstrip("/")
                if not path.endswith("/photos"):
                    path += "/photos"
                return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    if entry.venue_id:
        venue_id = urllib.parse.quote(entry.venue_id, safe="")
        return f"https://app.foursquare.com/v/{venue_id}/photos"
    return ""


def _normalize_nominatim_address(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("address") if isinstance(payload, dict) else {}
    address = raw if isinstance(raw, dict) else {}

    def first(*keys: str) -> str:
        for key in keys:
            value = str(address.get(key) or "").strip()
            if value:
                return value
        return ""

    house_number = first("house_number")
    road = first("road", "pedestrian", "residential", "street", "footway", "path")
    street_address = " ".join(part for part in (house_number, road) if part)
    city = first("city", "town", "village", "municipality", "borough")
    state = first("state", "region", "state_district")
    country = first("country")
    display_parts: list[str] = []
    seen: set[str] = set()
    for value in (street_address, city, state, country):
        normalized = value.casefold()
        if value and normalized not in seen:
            display_parts.append(value)
            seen.add(normalized)
    return {
        "address": street_address,
        "city": city,
        "state": state,
        "country": country,
        "display": ", ".join(display_parts),
    }


def _google_images_query(title: str, location: dict[str, Any] | None) -> str:
    clean_title = _search_title(title)
    display = str((location or {}).get("display") or "").strip()
    if not clean_title:
        return display
    components = {part.strip().casefold() for part in display.split(",") if part.strip()}
    if not display or clean_title.casefold() in components:
        return display or clean_title
    return f"{clean_title}, {display}"


def _google_images_url(query: str) -> str:
    clean_query = str(query or "").strip()
    if not clean_query:
        return GOOGLE_IMAGES_SEARCH_URL
    return f"{GOOGLE_IMAGES_SEARCH_URL}&{urllib.parse.urlencode({'q': clean_query})}"


def _is_allowed_remote_image_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return False
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        return literal_address.is_global
    try:
        resolved = socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (OSError, UnicodeError):
        return False
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for result in resolved:
        try:
            addresses.add(ipaddress.ip_address(result[4][0].split("%", 1)[0]))
        except (ValueError, IndexError, TypeError):
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


class _RemoteImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        if not _is_allowed_remote_image_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "Remote image redirected to a blocked host", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BackfillDecisionStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "decisions": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                or not isinstance(payload.get("decisions"), dict)):
            raise ValueError("Invalid Swarm backfill decision file")
        return payload

    def get(self, source_key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._payload()["decisions"].get(source_key)
        return value if isinstance(value, dict) else None

    def decisions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                key: value for key, value in self._payload()["decisions"].items()
                if isinstance(key, str) and isinstance(value, dict)
            }

    def select(self, source_key: str, candidate: dict[str, Any]) -> None:
        allowed = {
            "kind", "photo_source_id", "source_entry_key", "source_url", "label",
            "relative_path", "title", "author", "license", "license_url",
            "original_url", "width", "height",
        }
        clean = {key: value for key, value in candidate.items() if key in allowed}
        if clean.get("kind") not in {"export", "manual", "wikimedia"}:
            raise ValueError("Invalid representative-photo candidate")
        with self._lock:
            payload = self._payload()
            payload["decisions"][source_key] = {
                "status": "selected",
                "updated_at": dt.datetime.now(dt.UTC).isoformat(),
                "candidate": clean,
            }
            self._write(payload)

    def skip(self, source_key: str) -> None:
        with self._lock:
            payload = self._payload()
            payload["decisions"][source_key] = {
                "status": "skipped",
                "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            self._write(payload)

    def clear(self, source_key: str) -> None:
        with self._lock:
            payload = self._payload()
            payload["decisions"].pop(source_key, None)
            self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(self.path)


def validate_dropped_image(filename: str, payload: bytes) -> str:
    if not payload or len(payload) > MAX_DROP_BYTES:
        raise ValueError("Dropped image is empty or exceeds 50 MB")
    suffix = Path(str(filename or "").replace("\\", "/")).suffix.lower()
    detected = ""
    if payload.startswith(b"\xff\xd8\xff"):
        detected = ".jpg"
    elif payload.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = ".png"
    elif len(payload) >= 12 and payload[4:8] == b"ftyp" and payload[8:12] in {
        b"heic", b"heix", b"hevc", b"hevx", b"mif1",
    }:
        detected = ".heic"
    if not detected:
        raise ValueError("Drop one valid JPEG, PNG, or HEIC image")
    normalized = ".jpg" if suffix in {".jpg", ".jpeg"} else suffix
    if normalized and normalized != detected:
        raise ValueError("Dropped image extension does not match its contents")
    return detected


def _plain_metadata(value: Any) -> str:
    raw = value.get("value", "") if isinstance(value, dict) else ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(raw)))).strip()


def _https_url(value: Any) -> str:
    text = str(value or "").strip()
    return f"https:{text}" if text.startswith("//") else text


def _commons_page_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
    rows: list[dict[str, Any]] = []
    page_rows = pages.values() if isinstance(pages, dict) else pages if isinstance(pages, list) else []
    for page in page_rows:
        if not isinstance(page, dict) or not isinstance(page.get("imageinfo"), list):
            continue
        info = page["imageinfo"][0] if page["imageinfo"] else {}
        if not isinstance(info, dict):
            continue
        metadata = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
        license_name = _plain_metadata(metadata.get("LicenseShortName"))
        if not any(license_name.upper().startswith(prefix) for prefix in ALLOWED_COMMONS_LICENSES):
            continue
        image_url = _https_url(info.get("thumburl") or info.get("url"))
        source_url = _https_url(info.get("descriptionurl"))
        if (urllib.parse.urlparse(image_url).hostname or "").lower() != "upload.wikimedia.org":
            continue
        if (urllib.parse.urlparse(source_url).hostname or "").lower() != "commons.wikimedia.org":
            continue
        title = _plain_metadata(metadata.get("ObjectName")) or str(page.get("title") or "")
        candidate_id = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:24]
        rows.append({
            "candidate_id": candidate_id,
            "kind": "wikimedia",
            "title": title,
            "author": _plain_metadata(metadata.get("Artist")) or "Unknown creator",
            "license": license_name,
            "license_url": _https_url(_plain_metadata(metadata.get("LicenseUrl"))),
            "source_url": source_url,
            "image_url": image_url,
            "original_url": _https_url(info.get("url") or image_url),
            "width": int(info.get("thumbwidth") or info.get("width") or 0),
            "height": int(info.get("thumbheight") or info.get("height") or 0),
        })
    return rows


class BackfillReviewState:
    def __init__(self, entries: list[Entry], state_dir: Path,
                 geocoder_url: str = NOMINATIM_REVERSE_URL):
        self.targets = build_review_targets(entries)
        self.state_dir = state_dir.resolve()
        self.media_dir = self.state_dir / "media"
        self.store = BackfillDecisionStore(self.state_dir / "decisions.json")
        self.location_cache_path = self.state_dir / "location_cache.json"
        self.geocoder_url = str(geocoder_url).strip()
        parsed_geocoder = urllib.parse.urlsplit(self.geocoder_url)
        if parsed_geocoder.scheme not in {"http", "https"} or not parsed_geocoder.hostname:
            raise ValueError("Geocoder URL must be an HTTP or HTTPS endpoint")
        self.csrf_token = secrets.token_urlsafe(24)
        self._location_cache_lock = threading.RLock()
        self._geocoder_lock = threading.Lock()
        self._commons_lock = threading.Lock()
        self._last_geocoder_request = 0.0
        self.targets_by_token: dict[str, ReviewTarget] = {}
        self.targets_by_source_key = {
            target.entry.source_key: target for target in self.targets
        }
        self.photos_by_source: dict[tuple[str, str], Photo] = {
            (entry.source_key, photo.source_id): photo
            for entry in entries
            for photo in entry.photos
        }
        self.entry_tokens: dict[str, str] = {}
        self.media_by_token: dict[str, Path] = {}
        self.candidates_by_token: dict[tuple[str, str], BackfillCandidate] = {}
        self.commons_cache: dict[str, list[dict[str, Any]]] = {}
        for target in self.targets:
            entry_token = hashlib.sha256(target.entry.source_key.encode("utf-8")).hexdigest()[:24]
            self.targets_by_token[entry_token] = target
            self.entry_tokens[target.entry.source_key] = entry_token
            for candidate in target.candidates:
                candidate_token = hashlib.sha256(
                    f"{target.entry.source_key}:{candidate.source_entry.source_key}:"
                    f"{candidate.photo.source_id}".encode("utf-8")
                ).hexdigest()[:24]
                self.candidates_by_token[(entry_token, candidate_token)] = candidate
                self._media_token(candidate.photo.path)

    def _media_token(self, path: Path) -> str:
        resolved = path.resolve()
        for token, existing in self.media_by_token.items():
            if existing == resolved:
                return token
        token = secrets.token_urlsafe(18)
        self.media_by_token[token] = resolved
        return token

    @staticmethod
    def _location_cache_key(entry: Entry) -> str:
        if entry.latitude is None or entry.longitude is None:
            return ""
        return f"{entry.latitude:.6f},{entry.longitude:.6f}"

    def _read_location_cache(self) -> dict[str, Any]:
        if not self.location_cache_path.is_file():
            return {"schema_version": 1, "locations": {}}
        payload = json.loads(self.location_cache_path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                or not isinstance(payload.get("locations"), dict)):
            raise ValueError("Invalid reverse-geocoding cache")
        return payload

    def _cached_location(self, entry: Entry) -> dict[str, str] | None:
        key = self._location_cache_key(entry)
        if not key:
            return None
        with self._location_cache_lock:
            value = self._read_location_cache()["locations"].get(key)
        if not isinstance(value, dict):
            return None
        normalized = {
            name: str(value.get(name) or "")
            for name in ("address", "city", "state", "country", "display")
        }
        return normalized

    def _write_cached_location(self, entry: Entry, location: dict[str, str]) -> None:
        key = self._location_cache_key(entry)
        if not key:
            return
        with self._location_cache_lock:
            payload = self._read_location_cache()
            payload["locations"][key] = {
                name: str(location.get(name) or "")
                for name in ("address", "city", "state", "country", "display")
            }
            self.location_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.location_cache_path.parent,
                suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            temporary.replace(self.location_cache_path)

    @staticmethod
    def _location_result(entry: Entry, location: dict[str, str] | None) -> dict[str, Any]:
        fields = location or {
            "address": "", "city": "", "state": "", "country": "", "display": "",
        }
        google_images_query = _google_images_query(entry.title, fields)
        result: dict[str, Any] = dict(fields)
        result.update({
            "available": bool(fields.get("display")),
            "attribution": OPENSTREETMAP_ATTRIBUTION if location is not None else "",
            "attribution_url": OPENSTREETMAP_ATTRIBUTION_URL if location is not None else "",
            "google_images_query": google_images_query,
            "google_images_url": _google_images_url(google_images_query),
        })
        return result

    def location_for(self, entry_token: str) -> dict[str, Any]:
        target = self._target(entry_token)
        entry = target.entry
        if entry.latitude is None or entry.longitude is None:
            return self._location_result(entry, None)
        cached = self._cached_location(entry)
        if cached is not None:
            return self._location_result(entry, cached)

        with self._geocoder_lock:
            cached = self._cached_location(entry)
            if cached is not None:
                return self._location_result(entry, cached)
            wait_seconds = (
                NOMINATIM_MIN_INTERVAL_SECONDS
                - (time.monotonic() - self._last_geocoder_request)
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            query = urllib.parse.urlencode({
                "format": "jsonv2",
                "lat": f"{entry.latitude:.8f}",
                "lon": f"{entry.longitude:.8f}",
                "addressdetails": "1",
                "zoom": "18",
                "layer": "address",
                "accept-language": "en",
            })
            separator = "&" if urllib.parse.urlsplit(self.geocoder_url).query else "?"
            request = urllib.request.Request(
                f"{self.geocoder_url}{separator}{query}",
                headers={
                    "User-Agent": NOMINATIM_USER_AGENT,
                    "Accept": "application/json",
                },
            )
            self._last_geocoder_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as error:
                raise ValueError(f"Address lookup failed (HTTP {error.code})") from error
            except (urllib.error.URLError, TimeoutError):
                raise ValueError("Address lookup failed. Try this venue again later.") from None
            except json.JSONDecodeError:
                raise ValueError("Address lookup returned invalid data") from None
            if not isinstance(payload, dict):
                raise ValueError("Address lookup returned invalid data")
            location = _normalize_nominatim_address(payload)
            self._write_cached_location(entry, location)
            return self._location_result(entry, location)

    def summary(self) -> dict[str, int]:
        decisions = self.store.decisions()
        counts = Counter(
            decision.get("status") for decision in decisions.values()
            if decision.get("status") in {"selected", "skipped"}
        )
        return {
            "total": len(self.targets),
            "pending": len(self.targets) - counts["selected"] - counts["skipped"],
            "selected": counts["selected"],
            "skipped": counts["skipped"],
            "with_local_candidates": sum(bool(target.candidates) for target in self.targets),
            "without_local_candidates": sum(not target.candidates for target in self.targets),
        }

    def filtered(self, status: str) -> list[ReviewTarget]:
        decisions = self.store.decisions()
        if status == "all":
            return self.targets
        return [
            target for target in self.targets
            if (decisions.get(target.entry.source_key, {}).get("status") or "pending") == status
        ]

    def _selected_candidate_path(self, candidate: dict[str, Any]) -> Path | None:
        kind = candidate.get("kind")
        if kind == "export":
            photo = self.photos_by_source.get((
                str(candidate.get("source_entry_key") or ""),
                str(candidate.get("photo_source_id") or ""),
            ))
            return photo.path.resolve() if photo is not None and photo.path.is_file() else None
        if kind not in {"manual", "wikimedia"}:
            return None
        relative_path = Path(str(candidate.get("relative_path") or ""))
        if not relative_path.parts or relative_path.is_absolute():
            return None
        resolved = (self.state_dir / relative_path).resolve()
        try:
            resolved.relative_to(self.state_dir)
        except ValueError:
            return None
        return resolved if resolved.is_file() else None

    def _previous_candidate_choices(
            self, target: ReviewTarget) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        venue_id = target.entry.venue_id
        if not venue_id:
            return []
        decisions = self.store.decisions()
        matching_targets = sorted(
            (
                other for other in self.targets
                if other.entry.source_key != target.entry.source_key
                and other.entry.venue_id == venue_id
            ),
            key=lambda other: (other.entry.created_utc, other.entry.source_key),
            reverse=True,
        )
        choices: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen_candidates: set[str] = set()
        for other in matching_targets:
            decision = decisions.get(other.entry.source_key, {})
            candidate = decision.get("candidate")
            if decision.get("status") != "selected" or not isinstance(candidate, dict):
                continue
            candidate_identity = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            if candidate_identity in seen_candidates:
                continue
            path = self._selected_candidate_path(candidate)
            if path is None:
                continue
            seen_candidates.add(candidate_identity)
            repeat_token = hashlib.sha256(
                f"{target.entry.source_key}:previous:{candidate_identity}".encode("utf-8")
            ).hexdigest()[:24]
            selected_local_time = other.entry.created_utc + dt.timedelta(
                minutes=other.entry.offset_minutes
            )
            choices.append(({
                "repeat_token": repeat_token,
                "preview_url": f"/media/{self._media_token(path)}",
                "label": str(candidate.get("label") or "Previously selected venue image"),
                "selected_for_date": selected_local_time.date().isoformat(),
                "source_url": str(
                    candidate.get("source_url") or candidate.get("original_url") or ""
                ),
            }, dict(candidate)))
        return choices

    def entry_payload(self, status: str, index: int) -> dict[str, Any]:
        filtered = self.filtered(status)
        if not filtered:
            return {"entry": None, "index": 0, "count": 0, "summary": self.summary()}
        bounded = max(0, min(index, len(filtered) - 1))
        target = filtered[bounded]
        entry = target.entry
        entry_token = self.entry_tokens[entry.source_key]
        decision = self.store.get(entry.source_key)
        candidates = []
        for candidate in target.candidates:
            candidate_token = hashlib.sha256(
                f"{entry.source_key}:{candidate.source_entry.source_key}:"
                f"{candidate.photo.source_id}".encode("utf-8")
            ).hexdigest()[:24]
            media_token = next(
                token for token, path in self.media_by_token.items()
                if path == candidate.photo.path.resolve()
            )
            local_date = (candidate.photo.created_utc + dt.timedelta(
                minutes=candidate.source_entry.offset_minutes
            )).date().isoformat()
            candidates.append({
                "candidate_token": candidate_token,
                "preview_url": f"/media/{media_token}",
                "label": candidate.rationale,
                "source_date": local_date,
                "source_url": candidate.photo.canonical_url,
                "width": candidate.photo.width,
                "height": candidate.photo.height,
                "same_visit": abs(
                    (candidate.photo.created_utc - entry.created_utc).total_seconds()
                ) <= SAME_VISIT_WINDOW_SECONDS,
            })
        local_time = entry.created_utc + dt.timedelta(minutes=entry.offset_minutes)
        cached_location = self._cached_location(entry)
        location = (
            self._location_result(entry, cached_location)
            if cached_location is not None
            else None
        )
        google_images_query = _google_images_query(entry.title, cached_location)
        return {
            "entry": {
                "entry_token": entry_token,
                "title": entry.title,
                "kind": entry.kind,
                "local_time": local_time.strftime("%Y-%m-%d %H:%M"),
                "body": entry.body,
                "has_location": entry.latitude is not None and entry.longitude is not None,
                "google_maps_url": _google_maps_url(entry),
                "google_maps_embed_url": _google_maps_embed_url(entry),
                "foursquare_url": _foursquare_url(entry),
                "location": location,
                "google_images_query": google_images_query,
                "google_images_url": _google_images_url(google_images_query),
                "local_candidates": candidates,
                "previous_candidates": [
                    payload for payload, _ in self._previous_candidate_choices(target)
                ],
                "decision": decision,
            },
            "index": bounded,
            "count": len(filtered),
            "summary": self.summary(),
        }

    def select_local(self, entry_token: str, candidate_token: str) -> None:
        target = self._target(entry_token)
        candidate = self.candidates_by_token.get((entry_token, candidate_token))
        if candidate is None:
            raise ValueError("Unknown exact-venue candidate")
        self.store.select(target.entry.source_key, {
            "kind": "export",
            "photo_source_id": candidate.photo.source_id,
            "source_entry_key": candidate.source_entry.source_key,
            "source_url": candidate.photo.canonical_url,
            "label": candidate.rationale,
            "width": candidate.photo.width,
            "height": candidate.photo.height,
        })

    def select_previous(self, entry_token: str, repeat_token: str) -> None:
        target = self._target(entry_token)
        candidate = next(
            (
                stored for payload, stored in self._previous_candidate_choices(target)
                if payload["repeat_token"] == repeat_token
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Unknown previously selected candidate")
        self.store.select(target.entry.source_key, candidate)

    def skip(self, entry_token: str) -> None:
        self.store.skip(self._target(entry_token).entry.source_key)

    def clear(self, entry_token: str) -> None:
        self.store.clear(self._target(entry_token).entry.source_key)

    def save_manual(self, entry_token: str, filename: str, payload: bytes) -> None:
        target = self._target(entry_token)
        suffix = validate_dropped_image(urllib.parse.unquote(filename), payload)
        self._save_manual_candidate(target, suffix, payload)

    def save_remote(self, entry_token: str, source_url: str) -> None:
        target = self._target(entry_token)
        source_url = str(source_url or "").strip()
        if not _is_allowed_remote_image_url(source_url):
            raise ValueError("Drag a public HTTPS image, or a local image file")
        request = urllib.request.Request(source_url, headers={"User-Agent": WIKIMEDIA_USER_AGENT})
        opener = urllib.request.build_opener(_RemoteImageRedirectHandler())
        with opener.open(request, timeout=30) as response:
            final_url = response.geturl()
            if not _is_allowed_remote_image_url(final_url):
                raise ValueError("Remote image resolved to a blocked host")
            payload = response.read(MAX_DROP_BYTES + 1)
            content_type = response.headers.get_content_type()
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/heic": ".heic",
            "image/heif": ".heic",
        }.get(content_type, "")
        if not suffix:
            suffix = Path(urllib.parse.urlsplit(final_url).path).suffix.lower()
            suffix = ".jpg" if suffix in {".jpg", ".jpeg"} else suffix
        suffix = validate_dropped_image(f"download{suffix}", payload)
        self._save_manual_candidate(target, suffix, payload, source_url=final_url)

    def _save_manual_candidate(self, target: ReviewTarget, suffix: str, payload: bytes,
                               source_url: str = "") -> None:
        digest = hashlib.sha256(payload).hexdigest()
        self.media_dir.mkdir(parents=True, exist_ok=True)
        path = self.media_dir / f"{digest}{suffix}"
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
        self._media_token(path)
        candidate = {
            "kind": "manual",
            "relative_path": str(path.relative_to(self.state_dir)),
            "label": "Manually supplied representative venue image",
        }
        if source_url:
            candidate["source_url"] = source_url
        self.store.select(target.entry.source_key, candidate)

    def commons_candidates(self, entry_token: str) -> list[dict[str, Any]]:
        if entry_token in self.commons_cache:
            return self.commons_cache[entry_token]
        with self._commons_lock:
            if entry_token in self.commons_cache:
                return self.commons_cache[entry_token]
            target = self._target(entry_token)
            entry = target.entry
            search_title = _search_title(entry.title)
            if not search_title or search_title == "Swarm check-in":
                self.commons_cache[entry_token] = []
                return []
            rows = self._commons_query({
                "generator": "search",
                "gsrsearch": f'intitle:"{search_title}"',
                "gsrnamespace": "6",
                "gsrlimit": "8",
            })
            if (len(rows) < MAX_CANDIDATES
                    and entry.latitude is not None and entry.longitude is not None):
                geo_rows = self._commons_query({
                    "generator": "geosearch",
                    "ggsprimary": "all",
                    "ggsnamespace": "6",
                    "ggsradius": "2000",
                    "ggslimit": "10",
                    "ggscoord": f"{entry.latitude:.2f}|{entry.longitude:.2f}",
                })
                existing = {row["candidate_id"] for row in rows}
                rows.extend(
                    row for row in geo_rows if row["candidate_id"] not in existing
                )
            self.commons_cache[entry_token] = rows[:MAX_CANDIDATES]
            return self.commons_cache[entry_token]

    def select_commons(self, entry_token: str, candidate_id: str) -> None:
        target = self._target(entry_token)
        candidate = next(
            (row for row in self.commons_candidates(entry_token)
             if row["candidate_id"] == candidate_id),
            None,
        )
        if candidate is None:
            raise ValueError("Unknown Wikimedia candidate")
        request = urllib.request.Request(
            candidate["image_url"], headers={"User-Agent": WIKIMEDIA_USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(MAX_DROP_BYTES + 1)
            content_type = response.headers.get_content_type()
        suffix = {"image/jpeg": ".jpg", "image/png": ".png"}.get(content_type, "")
        if not suffix:
            suffix = Path(urllib.parse.urlparse(candidate["image_url"]).path).suffix.lower()
            suffix = ".jpg" if suffix in {".jpg", ".jpeg"} else suffix
        validate_dropped_image(f"download{suffix}", payload)
        digest = hashlib.sha256(payload).hexdigest()
        self.media_dir.mkdir(parents=True, exist_ok=True)
        path = self.media_dir / f"{digest}{suffix}"
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
        self._media_token(path)
        self.store.select(target.entry.source_key, {
            "kind": "wikimedia",
            "relative_path": str(path.relative_to(self.state_dir)),
            "title": candidate["title"],
            "author": candidate["author"],
            "license": candidate["license"],
            "license_url": candidate["license_url"],
            "source_url": candidate["source_url"],
            "original_url": candidate["original_url"],
            "width": candidate["width"],
            "height": candidate["height"],
            "label": "Wikimedia Commons representative venue image",
        })

    def _commons_query(self, generator: dict[str, str]) -> list[dict[str, Any]]:
        parameters = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "1200",
            **generator,
        }
        url = f"{WIKIMEDIA_API}?{urllib.parse.urlencode(parameters)}"
        request = urllib.request.Request(url, headers={"User-Agent": WIKIMEDIA_USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ValueError(f"Wikimedia lookup failed: {error}") from error
        return _commons_page_candidates(payload)

    def _target(self, entry_token: str) -> ReviewTarget:
        target = self.targets_by_token.get(entry_token)
        if target is None:
            raise ValueError("Unknown review entry")
        return target


def _review_html(csrf_token: str) -> str:
    token_json = json.dumps(csrf_token)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swarm representative-photo review</title>
<style>
:root{{--bg:#f6f6f2;--panel:#fff;--ink:#202124;--muted:#667085;--line:#d7d9d2;--accent:#17695d;--accent-soft:#dceeea;--warn:#9a6700}}
*{{box-sizing:border-box}} html,body{{height:100%}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow:hidden}}
header{{background:#ffffffee;border-bottom:1px solid var(--line);backdrop-filter:blur(10px);padding:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}} h1{{font-size:16px;line-height:1.2;margin:0;flex:1 1 130px}} a{{color:var(--accent)}} .control-panel-link{{border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink);padding:5px 7px;text-decoration:none;font-size:12px;font-weight:650;white-space:nowrap;flex:0 0 auto}} #summary{{flex:1 1 100%;font-size:12px;color:var(--muted)}} button,select,textarea{{background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:8px 12px;font:inherit}} button{{cursor:pointer}} button:hover,.control-panel-link:hover{{border-color:var(--accent)}} button.primary,button.good{{background:var(--accent);border-color:var(--accent);color:#fff}} button.warn{{background:#fff8e1;border-color:#d8b45b;color:#614700}} button:disabled{{opacity:.5;cursor:default}}
.workspace{{display:grid;grid-template-columns:25% 75%;height:100%}} .review-column{{min-width:0;min-height:0;display:flex;flex-direction:column;border-right:1px solid var(--line)}} .review-pane{{min-height:0;flex:1;padding:10px;overflow:auto}} .review-pane.drop-active{{background:var(--accent-soft)}} .local-image-input{{display:block;width:100%;min-height:46px;margin-top:10px;padding:8px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}} .entry{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}} .eyebrow{{color:var(--accent);font-weight:650}} h2{{margin:5px 0 4px;font-size:21px;line-height:1.2}} .location-address{{margin:0 0 5px;color:#344054;overflow-wrap:anywhere}} .location-address .attribution{{display:block;margin-top:2px;font-size:11px;color:var(--muted)}} .meta,.status,.source{{color:var(--muted)}} .note{{white-space:pre-wrap;margin-top:12px}}
.notice{{border-left:4px solid var(--warn);padding:9px 10px;background:#fff8e1;border-radius:6px;margin:12px 0}} .candidate-heading{{margin:14px 0 7px;font-size:14px}} .grid{{display:grid;grid-template-columns:1fr;gap:10px}} .candidate{{background:#fff;border:2px solid var(--line);border-radius:8px;overflow:hidden;cursor:pointer;position:relative}} .candidate:hover{{border-color:var(--accent)}} .candidate.same-visit,.candidate.same-visit:hover{{border-color:#b42318;box-shadow:0 0 0 1px #b42318}} .candidate img{{display:block;width:100%;height:180px;object-fit:cover;background:#eef0ed}} .candidate .copy{{padding:9px}} .number{{position:absolute;top:8px;left:8px;background:#202124dd;color:#fff;border-radius:20px;padding:4px 9px;font-weight:700}} .visit-window{{display:block;color:#b42318;font-weight:650}}
.drop{{margin:14px 0;border:2px dashed var(--line);border-radius:8px;padding:18px 10px;text-align:center;color:var(--muted);background:#f9faf8}} .drop button{{display:block;margin:10px auto 0}} .drop.error{{border-color:#b42318;color:#b42318;background:#fff3f1}} .controls{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px}} .spacer{{flex:1}} .empty{{padding:28px 12px;text-align:center;color:var(--muted)}}
.reference-pane{{min-width:0;background:#fff;display:flex;flex-direction:column}} .reference-toolbar{{height:50px;display:flex;align-items:end;border-bottom:1px solid var(--line);background:var(--bg)}} .reference-tabs{{display:flex;align-items:end;gap:4px;padding:8px 0 0 12px;flex:1}} .reference-tab{{display:inline-block;background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:6px 6px 0 0;padding:8px 12px;font:inherit;font-weight:650;text-decoration:none;cursor:pointer}} .reference-tab[aria-selected="true"]{{color:var(--accent);border-color:var(--accent);background:#fff}} .auto-submit-toggle{{display:flex;align-items:center;gap:6px;padding:0 12px 9px;font-size:13px;font-weight:650;white-space:nowrap}} .auto-submit-toggle input{{margin:0}} .reference-panel{{flex:1;min-height:0;position:relative}} .reference-message{{max-width:620px;margin:11vh auto;padding:28px;border:1px solid var(--line);border-radius:10px;background:var(--bg);text-align:center}} .reference-message h2{{font-size:24px}} .reference-message p{{color:var(--muted)}} .reference-panel iframe{{display:block;width:100%;height:100%;border:0}} .query-field{{display:block;width:100%;min-height:88px;margin:16px 0 10px;resize:vertical;text-align:left}} .reference-actions{{display:flex;justify-content:center;gap:8px;flex-wrap:wrap}} [hidden]{{display:none!important}}
@media(max-width:560px){{body{{overflow:auto}} .workspace{{display:block;height:auto}} .review-column{{border-right:0}} .reference-pane{{min-height:70vh;border-top:1px solid var(--line)}}}}
</style></head><body>
<div class="workspace"><div class="review-column">
<header><a class="control-panel-link" href="http://127.0.0.1:8766/?step=import_hub">← Project365 Control Panel</a><h1>Swarm representative-photo review</h1><span id="summary"></span><select id="filter"><option value="pending">Pending</option><option value="selected">Selected</option><option value="skipped">Skipped</option><option value="all">All</option></select></header>
<main id="reviewPane" class="review-pane"><section id="entry" class="entry"><div class="empty">Loading…</div></section></main></div>
<aside class="reference-pane" aria-label="Venue references">
<div class="reference-toolbar"><div class="reference-tabs" role="tablist" aria-label="Venue reference">
<button class="reference-tab" data-reference="foursquare" role="tab" aria-selected="true" onclick="selectReference('foursquare')">Foursquare</button>
<button class="reference-tab" data-reference="google" role="tab" aria-selected="false" onclick="selectReference('google')">Google Maps</button>
<a class="reference-tab" data-reference="google-images" role="tab" aria-selected="false" href="https://www.google.com/search?tbm=isch" target="swarm-google-images-window" rel="noopener" onclick="selectReference('google-images');return false">Google Images</a>
</div><label class="auto-submit-toggle" title="Submit the selected provider automatically after target changes"><input id="autoSubmit" type="checkbox" onchange="setAutoSubmit(this.checked)"> Auto-submit</label></div>
<section id="foursquarePanel" class="reference-panel" role="tabpanel">
<div class="reference-message"><h2 id="referenceTitle">Foursquare photos</h2><p>Submit this venue to Foursquare, then drag a photo anywhere onto the review pane. Foursquare prevents its signed-in pages from being embedded here.</p><button id="foursquareButton" class="primary" onclick="openFoursquare()">Submit to Foursquare</button><p><a id="foursquareFallback" href="#" target="swarm-location-window" rel="noopener">Open as a browser tab if the side window is blocked</a></p></div>
</section>
<section id="googlePanel" class="reference-panel" role="tabpanel" hidden><div id="googleMapsPrompt" class="reference-message"><h2>Open Google Maps</h2><p>Google Maps waits until you submit the current venue.</p><button id="submitGoogleMaps" class="primary" onclick="submitGoogleMaps()">Submit to Google Maps</button></div><iframe id="googleFrame" title="Google Maps venue" referrerpolicy="no-referrer-when-downgrade" allowfullscreen hidden></iframe></section>
<section id="googleImagesPanel" class="reference-panel" role="tabpanel" hidden>
<div class="reference-message"><h2>Search Google Images</h2><p>Google Images waits until you submit the current venue and address. Copy the query if you need it manually.</p><textarea id="googleImagesQuery" class="query-field" readonly aria-label="Google Images search query"></textarea><div class="reference-actions"><button id="copyGoogleImages" onclick="copyGoogleImagesQuery()">Copy search</button><button id="openGoogleImages" class="primary" onclick="openGoogleImages()">Submit to Google Images</button></div><p id="googleImagesStatus" class="status">Press Submit to look up the address and search.</p></div>
</section>
</aside></div>
<script>
const csrf={token_json};const autoSubmitKey='swarm-review-auto-submit';let index=0;let current=null;let commons=[];let currentCount=0;let activeReference='foursquare';let autoSubmit=localStorage.getItem(autoSubmitKey)==='true';let googleMapsEntryToken='';let referenceWindow=null;let googleImagesWindow=null;let commonsRequestToken='';
const entryEl=document.getElementById('entry'),filterEl=document.getElementById('filter'),reviewPane=document.getElementById('reviewPane'),autoSubmitEl=document.getElementById('autoSubmit');autoSubmitEl.checked=autoSubmit;
async function jsonFetch(url,options={{}}){{const response=await fetch(url,options);let body;try{{body=await response.json()}}catch{{body={{error:`HTTP ${{response.status}}`}}}}if(!response.ok)throw new Error(body.error||`HTTP ${{response.status}}`);return body}}
function esc(value){{return String(value??'').replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]))}}
function summaryText(s){{return `${{s.pending}} pending · ${{s.selected}} selected · ${{s.skipped}} skipped · ${{s.with_local_candidates}} with local candidates`}}
async function load(next=index){{index=Math.max(0,next);commons=[];const data=await jsonFetch(`/api/entry?status=${{encodeURIComponent(filterEl.value)}}&index=${{index}}`);document.getElementById('summary').textContent=summaryText(data.summary);index=data.index;current=data.entry;currentCount=data.count;render(currentCount);selectReference(activeReference,false);if(autoSubmit)await submitSelectedReference()}}
function candidateCard(candidate,number,kind='local'){{const action=kind==='commons'?`chooseCommons('${{candidate.candidate_id}}')`:kind==='previous'?`choosePrevious('${{candidate.repeat_token}}')`:`chooseLocal('${{candidate.candidate_token}}')`;const source=candidate.source_url?`<a href="${{esc(candidate.source_url)}}" target="_blank" rel="noopener" onclick="event.stopPropagation()">source</a>`:'';const detail=kind==='commons'?`${{esc(candidate.author)}} · ${{esc(candidate.license)}}`:kind==='previous'?`Previously used for ${{esc(candidate.selected_for_date)}}`:esc(candidate.source_date);const sameVisitClass=candidate.same_visit?' same-visit':'';const sameVisitNote=candidate.same_visit?'<span class="visit-window">Within 7 days of this visit</span>':'';return `<article class="candidate${{sameVisitClass}}" onclick="${{action}}"><span class="number">${{number}}</span><img src="${{esc(candidate.preview_url||candidate.image_url)}}" alt="Representative candidate" loading="lazy"><div class="copy"><strong>${{esc(candidate.label||candidate.title)}}</strong><div class="source">${{detail}} ${{source}}</div>${{sameVisitNote}}</div></article>`}}
function render(count){{if(!current){{entryEl.innerHTML='<div class="empty">No entries in this filter.</div>';updateReferencePane();return}}const localCards=current.local_candidates.map((candidate,i)=>candidateCard(candidate,i+1)).join('');const previousStart=current.local_candidates.length;const previousCards=current.previous_candidates.map((candidate,i)=>candidateCard(candidate,previousStart+i+1,'previous')).join('');const commonsStart=previousStart+current.previous_candidates.length;const commonsCards=commons.map((candidate,i)=>candidateCard(candidate,commonsStart+i+1,'commons')).join('');const archiveCards=localCards+commonsCards;const previousSection=previousCards?`<h3 class="candidate-heading">Previously selected for this venue</h3><div class="grid">${{previousCards}}</div>`:'';entryEl.innerHTML=`<div class="eyebrow">${{esc(current.kind)}} · ${{index+1}} of ${{count}}</div><h2>${{esc(current.title)}}</h2><div id="locationAddress" class="location-address"></div><div class="meta">${{esc(current.local_time)}} · ${{current.has_location?'Mapped location':'Location unresolved'}}</div>${{current.body?`<div class="note">${{esc(current.body)}}</div>`:''}}<div class="notice">Selected images are representative venue references—not photos captured during this visit.</div><h3 class="candidate-heading">Photos from this venue</h3><div class="grid">${{archiveCards||'<div class="empty">No exact-venue photos are available in the export.</div>'}}</div>${{previousSection}}<div class="controls"><button id="commonsButton" onclick="findCommons()">Find Wikimedia photos</button><span class="status" id="webStatus">On demand · 1–2 serial searches.</span></div><div id="drop" class="drop"><span id="dropStatus">Drop the image file directly on the native control below, or click Choose File.</span><input id="localImageInput" class="local-image-input" type="file" accept="image/jpeg,image/png,image/heic,image/heif,.jpg,.jpeg,.png,.heic,.heif" onchange="handleChosenFile(this)"></div><div class="controls"><button onclick="load(index-1)">← Previous</button><button class="warn" onclick="decide('skip')">Skip (S)</button><button onclick="decide('clear')">Clear</button><span class="spacer"></span><button onclick="load(index+1)">Next →</button></div>`;updateLocationDisplay();updateReferencePane()}}
function updateLocationDisplay(){{const element=document.getElementById('locationAddress');if(!element||!current)return;if(current.location?.available){{element.innerHTML=`${{esc(current.location.display)}}<a class="attribution" href="${{esc(current.location.attribution_url)}}" target="_blank" rel="noopener">${{esc(current.location.attribution)}}</a>`}}else if(current.location?.lookup_error)element.textContent=current.location.lookup_error;else if(!current.has_location)element.textContent='Address unavailable — this entry has no coordinates.';else if(current.location)element.textContent='No street address was found for these coordinates.';else element.textContent='Submit to Google Images to look up the address.'}}
function updateReferencePane(){{document.getElementById('referenceTitle').textContent=current?`${{current.title}} photos`:'Foursquare photos';const link=document.getElementById('foursquareFallback');link.href=current?.foursquare_url||'#';document.getElementById('foursquareButton').disabled=!current?.foursquare_url;const frame=document.getElementById('googleFrame');const mapsPrompt=document.getElementById('googleMapsPrompt');const mapsSubmitted=Boolean(current&&googleMapsEntryToken===current?.entry_token);frame.title=current?`Google Maps for ${{current.title}}`:'Google Maps venue';frame.hidden=!mapsSubmitted;mapsPrompt.hidden=mapsSubmitted;document.getElementById('submitGoogleMaps').disabled=!current?.google_maps_embed_url;if(mapsSubmitted&&current?.google_maps_embed_url&&frame.src!==current.google_maps_embed_url)frame.src=current.google_maps_embed_url;else if(!mapsSubmitted||!current?.google_maps_embed_url)frame.removeAttribute('src');const query=document.getElementById('googleImagesQuery');query.value=current?.google_images_query||'';const waiting=Boolean(current?.has_location&&!current?.location);document.getElementById('copyGoogleImages').disabled=!current||waiting;document.getElementById('openGoogleImages').disabled=!current;const status=document.getElementById('googleImagesStatus');if(current?.location?.lookup_error)status.textContent=current.location.lookup_error;else if(current?.location?.available)status.textContent='Address found. The exact image-search query is ready.';else if(waiting&&locationRequestToken===current?.entry_token)status.textContent='Looking up the address…';else if(waiting)status.textContent='Press Submit to look up the address and search.';else if(current)status.textContent='No address is available; the image search will use the venue name only.';else status.textContent='No venue is selected.'}}
async function selectReference(name,submit=true){{activeReference=name;for(const tab of document.querySelectorAll('.reference-tab'))tab.setAttribute('aria-selected',String(tab.dataset.reference===name));document.getElementById('foursquarePanel').hidden=name!=='foursquare';document.getElementById('googlePanel').hidden=name!=='google';document.getElementById('googleImagesPanel').hidden=name!=='google-images';updateReferencePane();if(submit&&autoSubmit){{if(prepareSelectedReferenceWindow())await submitSelectedReference()}}}}
function setAutoSubmit(enabled){{autoSubmit=Boolean(enabled);localStorage.setItem(autoSubmitKey,String(autoSubmit))}}
function windowIsOpen(value){{try{{return Boolean(value&&!value.closed)}}catch{{return false}}}}
function captureFoursquareWindow(){{if(windowIsOpen(referenceWindow))return true;const width=Math.max(640,Math.floor(screen.availWidth*.75));const height=screen.availHeight;const left=(screen.availLeft||0)+screen.availWidth-width;referenceWindow=window.open('about:blank','swarm-location-window',`popup=yes,width=${{width}},height=${{height}},left=${{left}},top=${{screen.availTop||0}}`);return windowIsOpen(referenceWindow)}}
function openFoursquare(){{if(!current?.foursquare_url||!captureFoursquareWindow())return;syncFoursquareWindow();referenceWindow.focus()}}
function submitGoogleMaps(){{googleMapsEntryToken=current?.google_maps_embed_url?current.entry_token:'';updateReferencePane()}}
function prepareSelectedReferenceWindow(){{if(activeReference==='foursquare')return captureFoursquareWindow();if(activeReference==='google-images')return captureGoogleImagesWindow();return true}}
async function submitSelectedReference(){{if(activeReference==='foursquare')syncFoursquareWindow();else if(activeReference==='google')submitGoogleMaps();else if(activeReference==='google-images'){{if(!windowIsOpen(googleImagesWindow))return;await loadGoogleImagesLocation();syncGoogleImagesWindow()}}}}
function syncFoursquareWindow(){{if(!current?.foursquare_url||!referenceWindow)return;try{{if(!referenceWindow.closed)referenceWindow.location.href=current.foursquare_url}}catch{{referenceWindow=null}}}}
function syncGoogleImagesWindow(){{if(!current?.google_images_url||!googleImagesWindow)return;try{{if(!googleImagesWindow.closed)googleImagesWindow.location.href=current.google_images_url}}catch{{googleImagesWindow=null}}}}
function captureGoogleImagesWindow(){{if(windowIsOpen(googleImagesWindow))return true;const width=Math.max(640,Math.floor(screen.availWidth*.75));const height=screen.availHeight;const left=(screen.availLeft||0)+screen.availWidth-width;googleImagesWindow=window.open('about:blank','swarm-google-images-window',`popup=yes,width=${{width}},height=${{height}},left=${{left}},top=${{screen.availTop||0}}`);return windowIsOpen(googleImagesWindow)}}
let locationRequestToken='';
async function loadGoogleImagesLocation(){{if(!current?.has_location||current.location)return;const token=current.entry_token;if(locationRequestToken===token)return;locationRequestToken=token;updateReferencePane();try{{const location=await jsonFetch(`/api/location?entry=${{encodeURIComponent(token)}}`);if(current?.entry_token!==token)return;current.location=location;current.google_images_query=location.google_images_query;current.google_images_url=location.google_images_url;updateLocationDisplay()}}catch(error){{if(current?.entry_token!==token)return;const fallbackQuery=current.google_images_query||current.title;current.location={{available:false,display:'',lookup_error:error.message,google_images_query:fallbackQuery}};current.google_images_query=fallbackQuery;current.google_images_url=`https://www.google.com/search?tbm=isch&q=${{encodeURIComponent(fallbackQuery)}}`;updateLocationDisplay()}}finally{{if(locationRequestToken===token)locationRequestToken='';if(current?.entry_token===token)updateReferencePane()}}}}
async function copyGoogleImagesQuery(){{const field=document.getElementById('googleImagesQuery');if(!field.value)return;try{{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(field.value);else throw new Error('Clipboard API unavailable');document.getElementById('googleImagesStatus').textContent='Image-search query copied.'}}catch{{field.focus();field.select();document.execCommand('copy');document.getElementById('googleImagesStatus').textContent='Image-search query selected and copied.'}}}}
async function openGoogleImages(){{if(!current)return;if(!captureGoogleImagesWindow()){{document.getElementById('googleImagesStatus').textContent='The browser blocked the Google Images window. Press Submit to try again.';return}}await loadGoogleImagesLocation();syncGoogleImagesWindow();googleImagesWindow.focus()}}
async function chooseLocal(candidate){{await jsonFetch('/api/decision',{{method:'POST',headers:{{'Content-Type':'application/json','X-Backfill-Token':csrf}},body:JSON.stringify({{entry_token:current.entry_token,action:'select',candidate_token:candidate}})}});await load(index)}}
async function choosePrevious(candidate){{await jsonFetch('/api/decision',{{method:'POST',headers:{{'Content-Type':'application/json','X-Backfill-Token':csrf}},body:JSON.stringify({{entry_token:current.entry_token,action:'select_previous',repeat_token:candidate}})}});await load(index)}}
async function decide(action){{await jsonFetch('/api/decision',{{method:'POST',headers:{{'Content-Type':'application/json','X-Backfill-Token':csrf}},body:JSON.stringify({{entry_token:current.entry_token,action}})}});await load(index)}}
async function findCommons(){{const token=current?.entry_token;if(!token||commonsRequestToken===token)return;commonsRequestToken=token;let status=document.getElementById('webStatus');const button=document.getElementById('commonsButton');if(button)button.disabled=true;status.textContent='Searching Wikimedia Commons…';try{{const results=await jsonFetch(`/api/commons?entry=${{encodeURIComponent(token)}}`);if(current?.entry_token!==token)return;commons=results;render(currentCount);status=document.getElementById('webStatus');status.textContent=commons.length?`${{commons.length}} licensed Commons candidates found.`:'No suitable Commons candidates found.'}}catch(error){{if(current?.entry_token===token){{status=document.getElementById('webStatus');if(status)status.textContent=error.message}}}}finally{{if(commonsRequestToken===token)commonsRequestToken='';if(current?.entry_token===token){{const currentButton=document.getElementById('commonsButton');if(currentButton)currentButton.disabled=false}}}}}}
async function chooseCommons(candidate_id){{await jsonFetch('/api/select-commons',{{method:'POST',headers:{{'Content-Type':'application/json','X-Backfill-Token':csrf}},body:JSON.stringify({{entry_token:current.entry_token,candidate_id}})}});await load(index)}}
function droppedFileLooksSupported(file){{const type=String(file?.type||'').split(';',1)[0].trim().toLowerCase();return ['image/jpeg','image/png','image/heic','image/heif'].includes(type)||/\\.(jpe?g|png|heic|heif)$/i.test(file?.name||'')}}
function droppedImageFile(transfer){{const files=Array.from(transfer?.files||[]);for(const item of Array.from(transfer?.items||[])){{if(item.kind==='file'){{const file=item.getAsFile();if(file&&!files.includes(file))files.push(file)}}}}return files.find(droppedFileLooksSupported)||(files.length===1?files[0]:null)}}
function describeDropTransfer(transfer){{const types=Array.from(transfer?.types||[]).join(', ')||'none';const items=Array.from(transfer?.items||[]).map(item=>`${{item.kind||'unknown'}}:${{item.type||'unknown'}}`).join(', ')||'none';const files=Array.from(transfer?.files||[]).map(file=>`${{file.name||'unnamed'}}:${{file.type||'unknown'}}:${{file.size||0}}`).join(', ')||'none';return `Safari exposed types=[${{types}}]; items=[${{items}}]; files=[${{files}}].`}}
function droppedImageUrl(transfer){{const htmlValue=transfer.getData('text/html');if(htmlValue){{const documentValue=new DOMParser().parseFromString(htmlValue,'text/html');const image=documentValue.querySelector('img[src]');if(image?.src)return image.src}}const uri=transfer.getData('text/uri-list').split(String.fromCharCode(10)).map(value=>value.trim()).find(value=>value&&!value.startsWith('#'));if(uri?.toLowerCase().startsWith('https://'))return uri;const plain=transfer.getData('text/plain').trim();return plain.toLowerCase().startsWith('https://')?plain:''}}
function dropStatus(message,error=false){{const zone=document.getElementById('drop'),status=document.getElementById('dropStatus');if(status)status.textContent=message;if(zone)zone.classList.toggle('error',error)}}
async function uploadLocalFile(file){{if(!current||!file)return;const token=current.entry_token;const contentType=file.type||'application/octet-stream';dropStatus(`Uploading ${{file.name||'dropped image'}}…`);const bytes=await file.arrayBuffer();const payload=new Blob([bytes],{{type:contentType}});await jsonFetch(`/api/drop?entry=${{encodeURIComponent(token)}}&filename=${{encodeURIComponent(file.name||'')}}`,{{method:'POST',headers:{{'Content-Type':contentType,'X-Backfill-Token':csrf}},body:payload}});if(current?.entry_token===token)await load(index)}}
async function handleChosenFile(input){{const file=input.files?.[0];if(!file)return;try{{await uploadLocalFile(file)}}catch(error){{dropStatus(error.message,true)}}finally{{input.value=''}}}}
async function handleDrop(event){{event.preventDefault();reviewPane.classList.remove('drop-active');if(!current)return;const token=current.entry_token;const file=droppedImageFile(event.dataTransfer);const sourceUrl=file?'':droppedImageUrl(event.dataTransfer);if(!file&&!sourceUrl){{console.warn(describeDropTransfer(event.dataTransfer));dropStatus('Safari did not provide the dragged file. Drop it directly on Choose File below.',true);const input=document.getElementById('localImageInput');if(input)input.focus();return}}try{{if(file)await uploadLocalFile(file);else{{dropStatus('Fetching dragged image…');await jsonFetch('/api/drop-url',{{method:'POST',headers:{{'Content-Type':'application/json','X-Backfill-Token':csrf}},body:JSON.stringify({{entry_token:token,source_url:sourceUrl}})}});if(current?.entry_token===token)await load(index)}}}}catch(error){{dropStatus(error.message,true)}}}}
function dropTargetsReviewPane(event){{if(reviewPane.contains(event.target))return true;const rect=reviewPane.getBoundingClientRect();return event.clientX>=rect.left&&event.clientX<=rect.right&&event.clientY>=rect.top&&event.clientY<=rect.bottom}}
function wireDrop(){{for(const name of ['dragenter','dragover'])window.addEventListener(name,event=>{{if(event.target?.id==='localImageInput')return;if(!dropTargetsReviewPane(event))return;event.preventDefault();event.dataTransfer.dropEffect='copy';reviewPane.classList.add('drop-active')}});reviewPane.addEventListener('dragleave',event=>{{if(!reviewPane.contains(event.relatedTarget))reviewPane.classList.remove('drop-active')}});window.addEventListener('drop',event=>{{if(event.target?.id==='localImageInput')return;if(!dropTargetsReviewPane(event))return;handleDrop(event)}})}}
filterEl.addEventListener('change',()=>{{index=0;load(0)}});document.addEventListener('keydown',event=>{{if(event.metaKey||event.ctrlKey||event.altKey)return;const n=Number(event.key);const candidates=[...current?.local_candidates||[],...current?.previous_candidates||[],...commons];if(n>=1&&n<=candidates.length){{event.preventDefault();const candidate=candidates[n-1];if(n<=current.local_candidates.length)chooseLocal(candidate.candidate_token);else if(n<=current.local_candidates.length+current.previous_candidates.length)choosePrevious(candidate.repeat_token);else chooseCommons(candidate.candidate_id)}}else if(event.key.toLowerCase()==='s'){{event.preventDefault();decide('skip')}}else if(event.key==='ArrowLeft'){{event.preventDefault();load(index-1)}}else if(event.key==='ArrowRight'){{event.preventDefault();load(index+1)}}}});wireDrop();load();
</script></body></html>"""


def create_handler(state: BackfillReviewState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SwarmBackfill/1.0"

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/":
                    self._send_bytes(_review_html(state.csrf_token).encode("utf-8"), "text/html; charset=utf-8")
                elif parsed.path == "/api/entry":
                    status = query.get("status", ["pending"])[0]
                    if status not in {"pending", "selected", "skipped", "all"}:
                        raise ValueError("Invalid review filter")
                    index = int(query.get("index", ["0"])[0])
                    self._send_json(state.entry_payload(status, index))
                elif parsed.path == "/api/summary":
                    self._send_json(state.summary())
                elif parsed.path == "/api/location":
                    self._send_json(state.location_for(query.get("entry", [""])[0]))
                elif parsed.path == "/api/commons":
                    self._send_json(state.commons_candidates(query.get("entry", [""])[0]))
                elif parsed.path.startswith("/media/"):
                    token = parsed.path.removeprefix("/media/")
                    path = state.media_by_token.get(token)
                    if path is None or not path.is_file():
                        self.send_error(404)
                        return
                    self._send_bytes(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                else:
                    self.send_error(404)
            except (ValueError, OSError) as error:
                self._send_json({"error": str(error)}, status=400)

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-Backfill-Token") != state.csrf_token:
                self._send_json({"error": "Invalid local review token"}, status=403)
                return
            try:
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_DROP_BYTES:
                    raise ValueError("Request body exceeds 50 MB")
                body = self.rfile.read(length)
                if parsed.path == "/api/drop":
                    state.save_manual(
                        query.get("entry", [""])[0],
                        query.get("filename", [""])[0],
                        body,
                    )
                else:
                    payload = json.loads(body or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid request")
                    if parsed.path == "/api/drop-url":
                        state.save_remote(
                            str(payload.get("entry_token", "")),
                            str(payload.get("source_url", "")),
                        )
                    elif parsed.path == "/api/decision":
                        action = payload.get("action")
                        if action == "select":
                            state.select_local(str(payload.get("entry_token", "")),
                                               str(payload.get("candidate_token", "")))
                        elif action == "select_previous":
                            state.select_previous(
                                str(payload.get("entry_token", "")),
                                str(payload.get("repeat_token", "")),
                            )
                        elif action == "skip":
                            state.skip(str(payload.get("entry_token", "")))
                        elif action == "clear":
                            state.clear(str(payload.get("entry_token", "")))
                        else:
                            raise ValueError("Invalid review decision")
                    elif parsed.path == "/api/select-commons":
                        state.select_commons(str(payload.get("entry_token", "")),
                                             str(payload.get("candidate_id", "")))
                    else:
                        self.send_error(404)
                        return
                self._send_json({"ok": True, "summary": state.summary()})
            except (ValueError, OSError, json.JSONDecodeError, urllib.error.URLError) as error:
                self._send_json({"error": str(error)}, status=400)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            self._send_bytes(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' https://upload.wikimedia.org data:; "
                "style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
                "frame-src https://maps.google.com https://www.google.com",
            )
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Swarm export folder or parent")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("Project365Canonical/swarm_dayone_backfill"),
        help="Persistent decisions and selected external media",
    )
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument(
        "--geocoder-url",
        default=NOMINATIM_REVERSE_URL,
        help="Replaceable reverse-geocoding endpoint (default: public OpenStreetMap Nominatim)",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    root = _export_root(args.source)
    entries, _, _, _ = _build_entries(root)
    state = BackfillReviewState(entries, args.state_dir, geocoder_url=args.geocoder_url)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), create_handler(state))
    print(json.dumps({
        "url": f"http://127.0.0.1:{args.port}/",
        "targets": len(state.targets),
        "summary": state.summary(),
        "decisions": str(state.store.path),
    }, ensure_ascii=False, indent=2))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
