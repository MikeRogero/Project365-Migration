from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

import project365_digikam_location_importer as importer


class DigiKamLocationImporterTests(unittest.TestCase):
    def test_import_applies_reviewed_locations_from_xmp_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Project365Canonical"
            xmp_root = root / "media" / "diarium_derivatives" / "Project365_square_2560_q88" / "2026-06"
            xmp_root.mkdir(parents=True)
            _create_db(root / "canonical.db", ["project365:2026-06-19"])
            xmp_path = xmp_root / "project365_2026-06-19.jpg.xmp"
            xmp_path.write_text(_xmp("25,5.0551667N", "121,35.2863333E"), encoding="utf-8")

            summary = importer.import_digikam_locations(
                canonical_root=root,
                xmp_roots=[root / "media" / "diarium_derivatives" / "Project365_square_2560_q88"],
                report_path=root / "exports" / "verification_reports" / "digikam_location_import_report.csv",
                queue_path=root / "exports" / "verification_reports" / "location_review_queue.csv",
            )

            self.assertEqual(summary.scanned_count, 1)
            self.assertEqual(summary.suggested_count, 1)
            self.assertEqual(summary.applied_count, 1)
            with sqlite3.connect(root / "canonical.db") as connection:
                row = connection.execute(
                    """
                    SELECT entry_id, location_status, source, latitude, longitude, label, confidence, review_status
                    FROM locations
                    """
                ).fetchone()
            self.assertEqual(
                row,
                (
                    "project365:2026-06-19",
                    "reviewed",
                    "digikam_xmp",
                    25.08425278,
                    121.58810555,
                    "digiKam GPS",
                    "metadata",
                    "reviewed",
                ),
            )

    def test_import_reports_unknown_working_copy_without_applying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Project365Canonical"
            xmp_root = root / "media" / "diarium_derivatives" / "Project365_square_2560_q88" / "2026-06"
            xmp_root.mkdir(parents=True)
            _create_db(root / "canonical.db", ["project365:2026-06-19"])
            xmp_path = xmp_root / "project365_2026-06-20.jpg.xmp"
            xmp_path.write_text(_xmp("25,0.0N", "121,0.0E"), encoding="utf-8")
            report_path = root / "exports" / "verification_reports" / "digikam_location_import_report.csv"

            summary = importer.import_digikam_locations(
                canonical_root=root,
                xmp_roots=[root / "media" / "diarium_derivatives" / "Project365_square_2560_q88"],
                report_path=report_path,
                queue_path=root / "exports" / "verification_reports" / "location_review_queue.csv",
            )

            self.assertEqual(summary.error_count, 1)
            with sqlite3.connect(root / "canonical.db") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 0)
            with report_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["reason"], "unknown_working_copy")


def _create_db(path: Path, entry_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE entries (id TEXT PRIMARY KEY, entry_date TEXT, original_text TEXT)")
        connection.execute(
            """
            CREATE TABLE media_assets (
                id TEXT PRIMARY KEY,
                entry_id TEXT NOT NULL,
                selected_default INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL,
                location_status TEXT NOT NULL,
                source TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                label TEXT,
                confidence TEXT,
                review_status TEXT NOT NULL,
                UNIQUE (entry_id, location_status, source, label)
            )
            """
        )
        for entry_id in entry_ids:
            connection.execute(
                "INSERT INTO entries (id, entry_date, original_text) VALUES (?, ?, ?)",
                (entry_id, entry_id.removeprefix("project365:"), ""),
            )


def _xmp(latitude: str, longitude: str) -> str:
    return f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    exif:GPSLatitude="{latitude}"
    exif:GPSLongitude="{longitude}" />
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


if __name__ == "__main__":
    unittest.main()
