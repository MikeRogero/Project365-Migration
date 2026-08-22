from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as importer


class Project365CanonicalImporterTests(unittest.TestCase):
    def test_import_sample_zip_creates_canonical_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            report_dir = base / "Reports"
            import_dir.mkdir()
            secret = "private diary text"
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.txt": secret.encode(),
                    "1998-04-12.png": b"png bytes",
                    "1998-04-13.png": b"more png bytes",
                },
            )

            summary = importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
                report_dir=report_dir,
            )

            self.assertEqual(summary.source_files, 1)
            self.assertEqual(summary.entries, 2)
            self.assertEqual(summary.media_assets, 2)
            self.assertEqual(summary.text_entries, 1)
            self.assertTrue((canonical_root / "canonical.db").exists())
            self.assertTrue(
                (
                    canonical_root
                    / "media"
                    / "project365_exports"
                    / "1998-04"
                    / "1998-04-12.png"
                ).exists()
            )
            self.assertNotIn(secret, (report_dir / "source_files.csv").read_text())

            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                rows = connection.execute(
                    "SELECT id, entry_date, original_text FROM entries ORDER BY id"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("project365:1998-04-12", "1998-04-12", secret),
                        ("project365:1998-04-13", "1998-04-13", None),
                    ],
                )
                media_roles = connection.execute(
                    "SELECT role, selected_default FROM media_assets ORDER BY entry_id"
                ).fetchall()
                self.assertEqual(
                    media_roles,
                    [
                        ("project365_export_png", 1),
                        ("project365_export_png", 1),
                    ],
                )
                source_roles = connection.execute(
                    "SELECT source_role FROM entry_sources ORDER BY source_role"
                ).fetchall()
                self.assertEqual(
                    source_roles,
                    [
                        ("project365_png",),
                        ("project365_png",),
                        ("project365_text",),
                    ],
                )

    def test_invalid_exports_are_not_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"nested/1998-04-12.png": b"invalid"},
            )

            with self.assertRaises(ValueError):
                importer.import_project365_exports(
                    import_dir=import_dir,
                    canonical_root=canonical_root,
                )

            self.assertFalse((canonical_root / "canonical.db").exists())

    def test_reimport_from_moved_source_folder_updates_stable_media_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first_dir = base / "Import"
            second_dir = base / "Source Data" / "Project365 Pro Export Zips"
            canonical_root = base / "Project365Canonical"
            first_dir.mkdir()
            second_dir.mkdir(parents=True)
            members = {"1998-04-12.png": b"png bytes"}
            _write_zip(first_dir / "1998-04.zip", members)
            _write_zip(second_dir / "1998-04.zip", members)

            importer.import_project365_exports(
                import_dir=first_dir,
                canonical_root=canonical_root,
            )
            importer.import_project365_exports(
                import_dir=second_dir,
                canonical_root=canonical_root,
            )

            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                media_rows = connection.execute(
                    """
                    SELECT id, role
                    FROM media_assets
                    WHERE role = 'project365_export_png'
                    """
                ).fetchall()
                source_paths = connection.execute(
                    """
                    SELECT source_path
                    FROM source_files
                    ORDER BY source_path
                    """
                ).fetchall()
            self.assertEqual(media_rows, [("project365:1998-04-12:project365_export_png", "project365_export_png")])
            self.assertEqual(len(source_paths), 2)


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


if __name__ == "__main__":
    unittest.main()
