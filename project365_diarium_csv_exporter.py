#!/usr/bin/env python3
"""Convert a Project365 Day One package into Diarium's People-capable CSV ZIP."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from project365_location_enrichment import load_exportable_locations


PHOTO_MARKER = re.compile(r"\n?!\[\]\(dayone-moment://[A-Za-z0-9]+\)")
CSV_FIELDS = ("date", "text", "tags", "people", "latitude", "longitude", "attachments")


@dataclass(frozen=True)
class DiariumCsvSummary:
    package_path: str
    entry_count: int
    people_entry_count: int
    location_count: int
    attachment_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Diarium CSV ZIP with dedicated People fields.")
    parser.add_argument("--canonical-root", type=Path, default=Path("Project365Canonical"))
    parser.add_argument("--dayone-zip", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-suggested", action="store_true",
                        help="Include digiKam people suggestions without changing their review status.")
    args = parser.parse_args()
    summary = generate_diarium_csv_package(
        args.canonical_root, args.dayone_zip, args.manifest, args.output, args.include_suggested,
    )
    print(f"Package: {summary.package_path}")
    print(f"Entries: {summary.entry_count}")
    print(f"Entries with People: {summary.people_entry_count}")
    print(f"Entries with location: {summary.location_count}")
    print(f"Attachments: {summary.attachment_count}")
    return 0


def generate_diarium_csv_package(
    canonical_root: Path,
    dayone_zip: Path,
    manifest_path: Path,
    output_path: Path,
    include_suggested: bool = False,
) -> DiariumCsvSummary:
    """Build a fresh-import ZIP; Diarium skips rows matching existing timestamps."""
    if output_path.resolve() == dayone_zip.resolve():
        raise ValueError("Output must differ from the source Day One ZIP")
    uuid_to_entry = _read_manifest(manifest_path)
    people_by_entry = _load_people(canonical_root / "canonical.db", include_suggested)
    locations = load_exportable_locations(canonical_root / "canonical.db", list(uuid_to_entry.values()))
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=CSV_FIELDS)
    writer.writeheader()
    people_entries = 0
    location_entries = 0
    attachments: set[str] = set()

    with zipfile.ZipFile(dayone_zip) as source:
        payload = json.loads(source.read("Journal.json"))
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("Journal.json must contain an entries array")
        names = set(source.namelist())
        seen_uuids: set[str] = set()
        for entry in entries:
            uuid = entry["uuid"]
            if uuid in seen_uuids or uuid not in uuid_to_entry:
                raise ValueError(f"Missing or repeated manifest mapping for Day One UUID: {uuid}")
            seen_uuids.add(uuid)
            entry_id = uuid_to_entry[uuid]
            people = people_by_entry.get(entry_id, [])
            if any("|" in name or "\n" in name or "\r" in name for name in people):
                raise ValueError(f"Person name cannot be represented in Diarium CSV: {entry_id}")
            people_entries += bool(people)
            person_labels = {name.casefold() for name in people}
            tags = [
                tag for tag in entry.get("tags", [])
                if str(tag).casefold() not in person_labels
                and str(tag).removeprefix("person:").casefold() not in person_labels
            ]
            if any("|" in str(tag) for tag in tags):
                raise ValueError(f"Tag cannot be represented in Diarium CSV: {entry_id}")
            location = locations.get(entry_id, entry.get("location") or {})
            location_entries += bool(location)
            photo_paths = []
            for photo in entry.get("photos", []):
                path = f"photos/{photo['md5']}.{photo['type']}"
                if path not in names:
                    raise ValueError(f"Missing Day One photo in ZIP: {path}")
                photo_paths.append(path)
                attachments.add(path)
            created = dt.datetime.fromisoformat(entry["creationDate"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise ValueError(f"Day One creationDate needs a timezone: {entry_id}")
            local_date = created.astimezone(ZoneInfo(entry["timeZone"])).strftime("%Y-%m-%dT%H:%M:%S")
            writer.writerow({
                "date": local_date,
                "text": PHOTO_MARKER.sub("", entry.get("text", "")),
                "tags": "|".join(str(tag) for tag in tags),
                "people": "|".join(people),
                "latitude": location.get("latitude", ""),
                "longitude": location.get("longitude", ""),
                "attachments": "|".join(photo_paths),
            })
        if set(uuid_to_entry) != seen_uuids:
            raise ValueError("Manifest and Day One ZIP have different entry sets")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_path.parent, suffix=".zip", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_STORED) as output:
                output.writestr("entries.csv", csv_buffer.getvalue())
                for name in sorted(attachments):
                    with source.open(name) as incoming, output.open(name, "w") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            os.replace(temporary_path, output_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    return DiariumCsvSummary(str(output_path), len(entries), people_entries,
                             location_entries, len(attachments))


def _read_manifest(path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            uuid, entry_id = row["dayone_uuid"], row["entry_id"]
            if uuid in mapping and mapping[uuid] != entry_id:
                raise ValueError(f"Conflicting manifest UUID: {uuid}")
            mapping[uuid] = entry_id
    return mapping


def _load_people(db_path: Path, include_suggested: bool) -> dict[str, list[str]]:
    statuses = {"confirmed", "reviewed"}
    if include_suggested:
        statuses.add("suggested")
    result: dict[str, set[str]] = {}
    with closing(sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)) as connection:
        for entry_id, name, status in connection.execute(
            "SELECT entry_id, canonical_name, review_status FROM people"
        ):
            if status in statuses and name.strip():
                result.setdefault(entry_id, set()).add(name.strip())
    return {entry_id: sorted(names, key=str.casefold) for entry_id, names in result.items()}


if __name__ == "__main__":
    raise SystemExit(main())
