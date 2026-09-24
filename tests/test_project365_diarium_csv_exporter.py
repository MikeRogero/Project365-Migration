from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from project365_diarium_csv_exporter import generate_diarium_csv_package


class DiariumCsvExporterTests(unittest.TestCase):
    def test_people_locations_and_photos_use_diarium_csv_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "canonical.db"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE people(entry_id TEXT, canonical_name TEXT, review_status TEXT);
                    CREATE TABLE locations(entry_id TEXT, latitude REAL, longitude REAL,
                        location_status TEXT, source TEXT, review_status TEXT);
                    INSERT INTO people VALUES ('project365:2001-01-02', 'Alex Example', 'suggested');
                    INSERT INTO people VALUES ('project365:2001-01-02', 'Sam Example', 'reviewed');
                    INSERT INTO locations VALUES ('project365:2001-01-02', 25.033, 121.565,
                        'reviewed', 'manual', 'reviewed');
                """)
            dayone = root / "source.zip"
            with zipfile.ZipFile(dayone, "w") as archive:
                archive.writestr("Journal.json", json.dumps({"entries": [{
                    "uuid": "TEST-UUID", "creationDate": "2001-01-02T04:00:00Z",
                    "timeZone": "Asia/Taipei",
                    "text": "Private test entry.\n![](dayone-moment://PHOTO)",
                    "tags": ["source:project365", "person:Alex Example", "other"],
                    "photos": [{"md5": "abc", "type": "jpeg"}],
                }]}))
                archive.writestr("photos/abc.jpeg", b"image bytes")
            manifest = root / "manifest.csv"
            manifest.write_text("entry_id,dayone_uuid\nproject365:2001-01-02,TEST-UUID\n")

            summary = generate_diarium_csv_package(
                canonical_root=root, dayone_zip=dayone, manifest_path=manifest,
                output_path=root / "diarium.csv.zip", include_suggested=True,
            )

            self.assertEqual((summary.entry_count, summary.people_entry_count, summary.location_count), (1, 1, 1))
            with zipfile.ZipFile(summary.package_path) as archive:
                self.assertEqual(sorted(archive.namelist()), ["entries.csv", "photos/abc.jpeg"])
                rows = list(csv.DictReader(io.StringIO(archive.read("entries.csv").decode("utf-8"))))
                self.assertEqual(archive.read("photos/abc.jpeg"), b"image bytes")
            self.assertEqual(rows[0]["date"], "2001-01-02T12:00:00")
            self.assertEqual(rows[0]["people"], "Alex Example|Sam Example")
            self.assertEqual(rows[0]["tags"], "source:project365|other")
            self.assertEqual(rows[0]["attachments"], "photos/abc.jpeg")
            self.assertEqual((rows[0]["latitude"], rows[0]["longitude"]), ("25.033", "121.565"))
            self.assertEqual(rows[0]["text"], "Private test entry.")

            reviewed = generate_diarium_csv_package(
                canonical_root=root, dayone_zip=dayone, manifest_path=manifest,
                output_path=root / "reviewed.csv.zip", include_suggested=False,
            )
            with zipfile.ZipFile(reviewed.package_path) as archive:
                row = next(csv.DictReader(io.StringIO(archive.read("entries.csv").decode("utf-8"))))
            self.assertEqual(row["people"], "Sam Example")

    def test_missing_attachment_rejects_package_without_writing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with sqlite3.connect(root / "canonical.db") as connection:
                connection.executescript("""
                    CREATE TABLE people(entry_id TEXT, canonical_name TEXT, review_status TEXT);
                    CREATE TABLE locations(entry_id TEXT, latitude REAL, longitude REAL,
                        location_status TEXT, source TEXT, review_status TEXT);
                """)
            dayone = root / "source.zip"
            with zipfile.ZipFile(dayone, "w") as archive:
                archive.writestr("Journal.json", json.dumps({"entries": [{
                    "uuid": "TEST-UUID", "creationDate": "2001-01-02T04:00:00Z",
                    "timeZone": "Asia/Taipei", "text": "test", "photos": [
                        {"md5": "missing", "type": "jpeg"}],
                }]}))
            manifest = root / "manifest.csv"
            manifest.write_text("entry_id,dayone_uuid\nproject365:2001-01-02,TEST-UUID\n")
            output = root / "diarium.csv.zip"

            with self.assertRaisesRegex(ValueError, "Missing Day One photo"):
                generate_diarium_csv_package(root, dayone, manifest, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
