#!/usr/bin/env python3
"""Generate Diarium import packages from the Project365 canonical archive."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import mimetypes
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from project365_media_derivatives import DEFAULT_DERIVATIVE_POLICY
from project365_location_enrichment import load_exportable_locations
from project365_tag_enrichment import load_exportable_diarium_tags


NAMESPACE = uuid.UUID("c4d9f898-190f-4c0c-b87e-000000a36500")
DEFAULT_TIME_ZONE = "Asia/Taipei"
EXPORTABLE_SOURCE_APPS = ("project365", "project365_enrichment")


@dataclass(frozen=True)
class DiariumExportSummary:
    package_path: str
    manifest_path: str
    entry_count: int
    media_count: int
    skipped_entry_count: int = 0
    skipped_entry_dates: tuple[str, ...] = ()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Day One ZIP package for Diarium import."
    )
    parser.add_argument(
        "--canonical-root",
        default="Project365Canonical",
        help="Canonical archive root containing canonical.db.",
    )
    parser.add_argument(
        "--output-dir",
        default="Project365Canonical/exports/diarium_import_batches",
        help="Output folder for the Diarium ZIP and manifest.",
    )
    parser.add_argument(
        "--package-name",
        default="project365_pilot_dayone.zip",
        help="Output ZIP filename.",
    )
    parser.add_argument(
        "--start-date",
        default="1998-04-01",
        help="First canonical entry date to include.",
    )
    parser.add_argument(
        "--end-date",
        default="1998-05-31",
        help="Last canonical entry date to include.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum entries to include.",
    )
    parser.add_argument(
        "--time-zone",
        default=DEFAULT_TIME_ZONE,
        help="IANA time zone for date-only Project365 entries.",
    )
    parser.add_argument(
        "--derivative-policy",
        default=DEFAULT_DERIVATIVE_POLICY,
        help="Preferred diarium_derivative policy suffix.",
    )
    args = parser.parse_args()

    summary = generate_diarium_dayone_package(
        canonical_root=Path(args.canonical_root),
        output_dir=Path(args.output_dir),
        package_name=args.package_name,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
        time_zone=args.time_zone,
        derivative_policy=args.derivative_policy,
    )
    print("Project365 Diarium package generation: PASS")
    print(f"Package: {summary.package_path}")
    print(f"Manifest: {summary.manifest_path}")
    print(f"Entries: {summary.entry_count}")
    print(f"Media assets: {summary.media_count}")
    print(f"Skipped entries: {summary.skipped_entry_count}")
    if summary.skipped_entry_dates:
        print(f"Skipped dates: {', '.join(summary.skipped_entry_dates)}")
    return 0


def generate_diarium_dayone_package(
    canonical_root: Path,
    output_dir: Path,
    package_name: str,
    start_date: str,
    end_date: str,
    limit: int,
    time_zone: str = DEFAULT_TIME_ZONE,
    derivative_policy: str = DEFAULT_DERIVATIVE_POLICY,
) -> DiariumExportSummary:
    if limit <= 0:
        raise ValueError("limit must be positive")
    _validate_date(start_date, "start-date")
    _validate_date(end_date, "end-date")
    if start_date > end_date:
        raise ValueError("start-date must be before or equal to end-date")

    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    package_path = output_dir / package_name
    manifest_path = output_dir / f"{Path(package_name).stem}_manifest.csv"

    entries = _load_entries(db_path, start_date, end_date, limit, derivative_policy)
    if not entries:
        raise ValueError("No canonical entries matched the requested date range")

    exportable_entries = []
    skipped_dates = []
    for entry in entries:
        skip_reason = _entry_skip_reason(entry)
        if skip_reason:
            skipped_dates.append(str(entry["entry_date"]))
            continue
        exportable_entries.append(entry)
    if not exportable_entries:
        preview = ", ".join(skipped_dates[:10])
        suffix = "" if len(skipped_dates) <= 10 else f", and {len(skipped_dates) - 10} more"
        raise ValueError(f"No exportable Project365 entries in requested range; skipped dates: {preview}{suffix}")

    tags_by_entry = load_exportable_diarium_tags(
        db_path,
        [entry["entry_id"] for entry in exportable_entries],
    )
    locations_by_entry = load_exportable_locations(
        db_path,
        [entry["entry_id"] for entry in exportable_entries],
    )

    entries_json: list[dict[str, object]] = []
    manifest_rows: list[dict[str, object]] = []
    media_count = 0
    zip_payloads: dict[str, bytes] = {}

    for entry in exportable_entries:
        entry_id = entry["entry_id"]
        entry_date = entry["entry_date"]
        creation_date = _creation_date_utc(entry_date, time_zone)
        entry_uuid = str(uuid.uuid5(NAMESPACE, entry_id)).upper()
        text = entry["original_text"] or ""
        tags = tags_by_entry.get(entry_id, ["source:project365"])
        photos: list[dict[str, object]] = []

        for order, media in enumerate(entry["media"]):
            media_payload = Path(media["storage_path"]).read_bytes()
            media_md5 = hashlib.md5(media_payload).hexdigest()
            media_ext = _dayone_extension(media["mime_type"], media["storage_path"])
            media_type = _dayone_photo_type(media_ext)
            media_identifier = uuid.uuid5(
                NAMESPACE, f"{entry_id}:{media['media_asset_id']}:{order}"
            ).hex.upper()
            photo_zip_name = f"photos/{media_md5}.{media_ext}"
            zip_payloads[photo_zip_name] = media_payload
            text = _append_photo_marker(text, media_identifier)
            width, height = _image_dimensions(media_payload, media_ext)
            if width != height:
                raise ValueError(f"Diarium media is not square for {entry_id}: {width} x {height}")
            photos.append(
                {
                    "type": media_type,
                    "identifier": media_identifier,
                    "md5": media_md5,
                    "orderInEntry": order,
                    "fileSize": len(media_payload),
                    "width": width,
                    "height": height,
                    "date": creation_date,
                }
            )
            media_count += 1
            manifest_rows.append(
                {
                    "entry_id": entry_id,
                    "entry_date": entry_date,
                    "creation_date_utc": creation_date,
                    "dayone_uuid": entry_uuid,
                    "photo_order": order,
                    "media_asset_id": media["media_asset_id"] or "",
                    "media_role": media["media_role"] or "",
                    "media_sha256": media["media_sha256"] or "",
                    "source_media_asset_id": media["source_media_asset_id"] or "",
                    "source_media_role": media["source_media_role"] or "",
                    "associated_entry_date": media["associated_entry_date"] or "",
                    "photo_zip_path": photo_zip_name,
                    "text_present": str(entry["original_text"] is not None).lower(),
                }
            )

        entry_json: dict[str, object] = {
            "uuid": entry_uuid,
            "creationDate": creation_date,
            "timeZone": time_zone,
            "text": text,
            "tags": tags,
        }
        if entry_id in locations_by_entry:
            entry_json["location"] = locations_by_entry[entry_id]
        if photos:
            entry_json["photos"] = photos
        entries_json.append(entry_json)

    dayone_payload = {
        "metadata": {"version": "1.0"},
        "entries": entries_json,
    }
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Journal.json",
            json.dumps(dayone_payload, ensure_ascii=False, indent=2) + "\n",
        )
        for name, payload in sorted(zip_payloads.items()):
            archive.writestr(name, payload)

    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "entry_id",
            "entry_date",
            "creation_date_utc",
            "dayone_uuid",
            "photo_order",
            "media_asset_id",
            "media_role",
            "media_sha256",
            "source_media_asset_id",
            "source_media_role",
            "associated_entry_date",
            "photo_zip_path",
            "text_present",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(manifest_rows)

    return DiariumExportSummary(
        package_path=str(package_path),
        manifest_path=str(manifest_path),
        entry_count=len(entries_json),
        media_count=media_count,
        skipped_entry_count=len(skipped_dates),
        skipped_entry_dates=tuple(skipped_dates),
    )


def _entry_skip_reason(entry: dict[str, object]) -> str:
    media_rows = entry.get("media", [])
    if not isinstance(media_rows, list):
        return "missing available square derivative"
    if not any(media.get("media_role") == "diarium_derivative" for media in media_rows):
        return "missing available square derivative"
    if any(
        not _has_derivative_crop_metadata(media.get("media_transformation_json"))
        for media in media_rows
    ):
        return "missing derivative crop metadata"
    return ""


def _load_entries(
    db_path: Path,
    start_date: str,
    end_date: str,
    limit: int,
    derivative_policy: str,
) -> list[dict[str, object]]:
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        entry_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    entries.id AS entry_id,
                    entries.entry_date,
                    entries.original_text
                FROM entries
                WHERE entries.source_app IN ('project365', 'project365_enrichment')
                    AND entries.entry_date BETWEEN ? AND ?
                ORDER BY entries.entry_date, entries.id
                LIMIT ?
                """,
                (start_date, end_date, limit),
            ).fetchall()
        ]
        if not entry_rows:
            return []
        media_by_entry = _load_entry_media(connection, [row["entry_id"] for row in entry_rows], derivative_policy)
        return [
            {
                **entry,
                "media": media_by_entry.get(str(entry["entry_id"]), []),
            }
            for entry in entry_rows
        ]
    finally:
        connection.close()


def _load_entry_media(
    connection: sqlite3.Connection,
    entry_ids: list[str],
    derivative_policy: str,
) -> dict[str, list[dict[str, object]]]:
    placeholders = ",".join("?" for _ in entry_ids)
    rows = connection.execute(
        f"""
            WITH derivative_rows AS (
                SELECT
                    media_assets.entry_id,
                    media_assets.id AS media_asset_id,
                    media_assets.role AS media_role,
                    media_assets.storage_path,
                    media_assets.sha256 AS media_sha256,
                    media_assets.mime_type,
                    media_assets.transformation_json AS media_transformation_json,
                    json_extract(media_assets.transformation_json, '$.source_media_asset_id') AS source_media_asset_id,
                    json_extract(media_assets.transformation_json, '$.source_role') AS source_media_role,
                    '' AS associated_entry_date,
                    0 AS media_order
                FROM media_assets
                WHERE media_assets.entry_id IN ({placeholders})
                    AND media_assets.role = 'diarium_derivative'
                    AND media_assets.id = media_assets.entry_id || ':diarium_derivative:' || ?
                    AND media_assets.status = 'available'
                UNION ALL
                SELECT
                    media_assets.entry_id,
                    media_assets.id AS media_asset_id,
                    media_assets.role AS media_role,
                    media_assets.storage_path,
                    media_assets.sha256 AS media_sha256,
                    media_assets.mime_type,
                    media_assets.transformation_json AS media_transformation_json,
                    json_extract(media_assets.transformation_json, '$.source_media_asset_id') AS source_media_asset_id,
                    json_extract(media_assets.transformation_json, '$.source_role') AS source_media_role,
                    COALESCE(json_extract(source_media.transformation_json, '$.associated_entry_date'), '') AS associated_entry_date,
                    1 AS media_order
                FROM media_assets
                LEFT JOIN media_assets AS source_media
                    ON source_media.id = json_extract(media_assets.transformation_json, '$.source_media_asset_id')
                WHERE media_assets.entry_id IN ({placeholders})
                    AND media_assets.role = 'diarium_associated_derivative'
                    AND media_assets.id LIKE '%:diarium_associated_derivative:' || ?
                    AND media_assets.status = 'available'
            )
            SELECT
                *
            FROM derivative_rows
            ORDER BY entry_id, media_order, associated_entry_date, media_asset_id
            """,
        [*entry_ids, derivative_policy, *entry_ids, derivative_policy],
    ).fetchall()
    media_by_entry: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        media_by_entry.setdefault(str(row["entry_id"]), []).append(dict(row))
    return media_by_entry


def _creation_date_utc(entry_date: str, time_zone: str) -> str:
    local_zone = ZoneInfo(time_zone)
    date = dt.date.fromisoformat(entry_date)
    local_noon = dt.datetime.combine(date, dt.time(12, 0), local_zone)
    return local_noon.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _has_derivative_crop_metadata(value: object) -> bool:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    crop = payload.get("crop")
    if not isinstance(crop, dict):
        return False
    try:
        size = int(crop.get("size") or 0)
    except (TypeError, ValueError):
        return False
    return crop.get("shape") == "square" and size > 0


def _append_photo_marker(text: str, identifier: str) -> str:
    marker = f"![](dayone-moment://{identifier})"
    if text:
        return f"{text}\n{marker}"
    return marker


def _dayone_extension(mime_type: str | None, storage_path: str) -> str:
    if mime_type == "image/png":
        return "png"
    if mime_type in {"image/jpeg", "image/jpg"}:
        return "jpeg"
    guessed, _ = mimetypes.guess_type(storage_path)
    if guessed == "image/png":
        return "png"
    if guessed in {"image/jpeg", "image/jpg"}:
        return "jpeg"
    return Path(storage_path).suffix.lstrip(".").lower() or "bin"


def _dayone_photo_type(extension: str) -> str:
    if extension in {"jpg", "jpeg"}:
        return "jpeg"
    return extension


def _image_dimensions(payload: bytes, extension: str) -> tuple[int, int]:
    if extension == "png":
        return _png_dimensions(payload)
    if extension in {"jpg", "jpeg"}:
        return _jpeg_dimensions(payload)
    return (0, 0)


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return (
        int.from_bytes(payload[16:20], "big"),
        int.from_bytes(payload[20:24], "big"),
    )


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 4 or payload[:2] != b"\xff\xd8":
        return (0, 0)
    offset = 2
    while offset + 4 <= len(payload):
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return (0, 0)
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(payload):
            return (0, 0)
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            return (0, 0)
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if segment_length < 7:
                return (0, 0)
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            return (width, height)
        offset += segment_length
    return (0, 0)


def _validate_date(value: str, name: str) -> None:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be YYYY-MM-DD")


if __name__ == "__main__":
    raise SystemExit(main())
