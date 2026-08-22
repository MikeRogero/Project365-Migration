from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as canonical_importer
import project365_diarium_exporter as diarium_exporter
import project365_location_enrichment as location_enrichment


class Project365LocationEnrichmentTests(unittest.TestCase):
    def test_reviewed_location_is_exported_to_diarium_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            import_dir.mkdir()
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": _tiny_png()})
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            review_csv = base / "location_review.csv"
            review_csv.write_text(
                "\n".join(
                    [
                        "entry_id,location_status,source,latitude,longitude,label,confidence,review_status",
                        "project365:1998-04-12,reviewed,manual,25.033,121.565,Taipei,manual,reviewed",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = location_enrichment.run_location_enrichment(
                canonical_root=canonical_root,
                queue_path=canonical_root / "exports" / "verification_reports" / "location_queue.csv",
                review_csv=review_csv,
            )

            self.assertEqual(summary.applied_count, 1)
            locations = location_enrichment.load_exportable_locations(
                canonical_root / "canonical.db",
                ["project365:1998-04-12"],
            )
            self.assertEqual(
                locations,
                {"project365:1998-04-12": {"latitude": 25.033, "longitude": 121.565}},
            )
            package_summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=output_dir,
                package_name="pilot.zip",
                start_date="1998-04-01",
                end_date="1998-04-30",
                limit=20,
            )
            with zipfile.ZipFile(package_summary.package_path) as archive:
                payload = json.loads(archive.read("Journal.json").decode("utf-8"))
            self.assertEqual(
                payload["entries"][0]["location"],
                {"latitude": 25.033, "longitude": 121.565},
            )

    def test_conflicting_confirmed_locations_are_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": _tiny_png()})
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                location_enrichment.upsert_location(
                    connection,
                    "project365:1998-04-12",
                    "confirmed",
                    "exif",
                    25.033,
                    121.565,
                    "exif",
                    "gps",
                    "confirmed",
                )
                location_enrichment.upsert_location(
                    connection,
                    "project365:1998-04-12",
                    "reviewed",
                    "manual",
                    35.0,
                    139.0,
                    "manual",
                    "manual",
                    "reviewed",
                )

            locations = location_enrichment.load_exportable_locations(
                canonical_root / "canonical.db",
                ["project365:1998-04-12"],
            )

            self.assertEqual(locations, {})

    def test_review_queue_lists_missing_location_without_private_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            private_text = "private location queue text"
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.txt": private_text.encode(), "1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )

            summary = location_enrichment.run_location_enrichment(
                canonical_root=canonical_root,
                queue_path=canonical_root / "exports" / "verification_reports" / "location_queue.csv",
            )

            with Path(summary.queue_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["entry_id"], "project365:1998-04-12")
            self.assertEqual(rows[0]["has_text"], "1")
            self.assertNotIn(private_text, Path(summary.queue_path).read_text())

    def test_confirmed_location_requires_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": _tiny_png()})
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            review_csv = base / "location_review.csv"
            review_csv.write_text(
                "\n".join(
                    [
                        "entry_id,location_status,source,latitude,longitude,label,confidence,review_status",
                        "project365:1998-04-12,confirmed,exif,,,missing,gps,confirmed",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "require latitude and longitude"):
                location_enrichment.run_location_enrichment(
                    canonical_root=canonical_root,
                    queue_path=canonical_root / "exports" / "verification_reports" / "location_queue.csv",
                    review_csv=review_csv,
                )


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
        b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f"
        b"\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


if __name__ == "__main__":
    unittest.main()
