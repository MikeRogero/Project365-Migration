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
import project365_diarium_reconciler as reconciler


class Project365DiariumReconcilerTests(unittest.TestCase):
    def test_text_status_allows_diarium_whitespace_normalization(self) -> None:
        self.assertEqual(
            reconciler._text_status("line one\n\nline two", "line one line two"),
            "normalized_unchanged",
        )

    def test_text_status_flags_real_text_change(self) -> None:
        self.assertEqual(
            reconciler._text_status("line one line two", "line one changed two"),
            "changed",
        )

    def test_reconcile_export_reports_and_applies_reviewed_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            report_dir = canonical_root / "exports" / "verification_reports"
            import_dir.mkdir()
            private_text = "private canonical text"
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.txt": private_text.encode(), "1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            package_summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=output_dir,
                package_name="pilot.zip",
                start_date="1998-04-01",
                end_date="1998-04-30",
                limit=20,
            )
            diarium_export = base / "diarium_export.json"
            diarium_export.write_text(
                json.dumps(
                    [
                        {
                            "date": "1998-04-12T12:00:00",
                            "heading": "",
                            "html": f"<p>{private_text}</p>",
                            "location": [25.033, 121.565],
                            "tags": ["source:project365", "person:Alex", "place:Taipei"],
                            "people": ["Sam"],
                            "tracker": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            summary = reconciler.reconcile_diarium_export(
                canonical_root=canonical_root,
                diarium_export=diarium_export,
                pilot_manifest=Path(package_summary.manifest_path),
                report_dir=report_dir,
                apply_reviewed=False,
            )

            self.assertEqual(summary.export_entries, 1)
            self.assertEqual(summary.matched_entries, 1)
            self.assertEqual(summary.unmatched_entries, 0)
            self.assertEqual(summary.missing_expected_entries, 0)
            self.assertTrue(summary.passed)
            self.assertEqual(summary.applied_updates, 0)
            report_text = Path(summary.report_path).read_text()
            self.assertNotIn(private_text, report_text)
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["entry_id"], "project365:1998-04-12")
            self.assertEqual(rows[0]["match_status"], "matched")
            self.assertEqual(rows[0]["text_status"], "unchanged")
            self.assertEqual(rows[0]["reviewable_tag_count"], "2")
            self.assertEqual(rows[0]["people_count"], "1")
            self.assertEqual(rows[0]["location_status"], "present")
            self.assertEqual(rows[0]["has_reviewable_updates"], "true")

            applied = reconciler.reconcile_diarium_export(
                canonical_root=canonical_root,
                diarium_export=diarium_export,
                pilot_manifest=Path(package_summary.manifest_path),
                report_dir=report_dir,
                apply_reviewed=True,
            )

            self.assertEqual(applied.applied_updates, 4)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                tag_rows = connection.execute(
                    "SELECT tag_type, canonical_name, review_status, source FROM tags"
                ).fetchall()
                people_rows = connection.execute(
                    "SELECT canonical_name, diarium_tag, review_status, source FROM people"
                ).fetchall()
                location_rows = connection.execute(
                    """
                    SELECT latitude, longitude, location_status, review_status, source
                    FROM locations
                    """
                ).fetchall()
            self.assertEqual(
                tag_rows,
                [
                    ("place", "place:Taipei", "reviewed", "diarium_export"),
                ],
            )
            self.assertEqual(
                sorted(people_rows),
                [
                    ("Alex", "person:Alex", "reviewed", "diarium_export"),
                    ("Sam", "Sam", "reviewed", "diarium_export"),
                ],
            )
            self.assertEqual(
                location_rows,
                [(25.033, 121.565, "confirmed", "reviewed", "diarium_export")],
            )

    def test_reconcile_zip_export_counts_media_from_diarium_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            report_dir = canonical_root / "exports" / "verification_reports"
            import_dir.mkdir()
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": _tiny_png()})
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            package_summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=output_dir,
                package_name="pilot.zip",
                start_date="1998-04-01",
                end_date="1998-04-30",
                limit=20,
            )
            diarium_zip = base / "diarium_export.zip"
            with zipfile.ZipFile(diarium_zip, "w") as archive:
                archive.writestr(
                    "diarium_export.json",
                    json.dumps(
                        [
                            {
                                "date": "1998-04-12T12:00:00",
                                "heading": "",
                                "html": "",
                                "tags": ["source-project365"],
                                "people": [],
                                "tracker": [],
                            }
                        ]
                    ),
                )
                archive.writestr("media/1998-04-12_120000000/example.jpg", b"image")

            summary = reconciler.reconcile_diarium_export(
                canonical_root=canonical_root,
                diarium_export=diarium_zip,
                pilot_manifest=Path(package_summary.manifest_path),
                report_dir=report_dir,
            )

            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["media_file_count"], "1")

    def test_reconcile_reports_manifest_entries_missing_from_diarium_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            report_dir = canonical_root / "exports" / "verification_reports"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            package_summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=output_dir,
                package_name="pilot.zip",
                start_date="1998-04-01",
                end_date="1998-04-30",
                limit=20,
            )
            diarium_export = base / "partial_diarium_export.json"
            diarium_export.write_text(
                json.dumps(
                    [
                        {
                            "date": "1998-04-12T12:00:00",
                            "heading": "",
                            "html": "",
                            "tags": ["source:project365"],
                            "people": [],
                            "tracker": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            summary = reconciler.reconcile_diarium_export(
                canonical_root=canonical_root,
                diarium_export=diarium_export,
                pilot_manifest=Path(package_summary.manifest_path),
                report_dir=report_dir,
            )

            self.assertEqual(summary.export_entries, 1)
            self.assertEqual(summary.matched_entries, 1)
            self.assertEqual(summary.unmatched_entries, 0)
            self.assertEqual(summary.missing_expected_entries, 1)
            self.assertFalse(summary.passed)
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["entry_id"], "project365:1998-04-13")
            self.assertEqual(rows[-1]["match_status"], "missing_expected")
            self.assertEqual(rows[-1]["diarium_date"], "")


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
