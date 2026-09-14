from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_photo_library_index as photo_index


class Project365PhotoLibraryIndexTests(unittest.TestCase):
    def test_default_reexport_sidecar_index_db_uses_canonical_root(self) -> None:
        canonical_root = Path("/tmp/Project365Canonical")

        index_db = photo_index.default_reexport_sidecar_index_db(canonical_root)

        self.assertEqual(index_db, canonical_root / "photo_library_reexport_index.sqlite")

    def test_default_legacy_video_sidecar_index_db_uses_canonical_root(self) -> None:
        canonical_root = Path("/tmp/Project365Canonical")

        index_db = photo_index.default_legacy_video_sidecar_index_db(canonical_root)

        self.assertEqual(index_db, canonical_root / "video_library_legacy_index.sqlite")

    def test_reexport_sidecar_cli_writes_sidecar_not_main_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            reexport_root = base / "2000-2023 iCloud Photos (Re-exported)"
            reexport_root.mkdir()
            photo = reexport_root / "2022-03-14 123456.jpg"
            video = reexport_root / "2022-03-14 123457.mov"
            photo.write_bytes(b"fake image bytes")
            video.write_bytes(b"fake video bytes")

            argv = [
                "project365_photo_library_index.py",
                "--canonical-root",
                str(canonical_root),
                "--reexport-sidecar",
                "--index-root",
                str(reexport_root),
                "--reset",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                photo_index, "_exiftool_photo_metadata", return_value={}
            ), mock.patch("sys.stdout", new_callable=io.StringIO):
                exit_code = photo_index.main()

            sidecar_db = canonical_root / "photo_library_reexport_index.sqlite"
            main_db = canonical_root / "photo_library_index.sqlite"

            self.assertEqual(exit_code, 0)
            self.assertTrue(sidecar_db.exists())
            self.assertFalse(main_db.exists())
            with sqlite3.connect(sidecar_db) as connection:
                rows = connection.execute(
                    "SELECT path, root, filename FROM photo_library_files ORDER BY filename"
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    (str(photo.resolve()), str(reexport_root), photo.name),
                    (str(video.resolve()), str(reexport_root), video.name),
                ],
            )

    def test_default_index_scan_keeps_existing_image_only_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            photo = library_root / "2022-03-14 123456.jpg"
            video = library_root / "2022-03-14 123457.mov"
            photo.write_bytes(b"fake image bytes")
            video.write_bytes(b"fake video bytes")
            index_db = base / "index.sqlite"

            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            with sqlite3.connect(index_db) as connection:
                rows = connection.execute(
                    "SELECT filename FROM photo_library_files ORDER BY filename"
                ).fetchall()

            self.assertEqual(rows, [(photo.name,)])

    def test_reexport_sidecar_rejects_explicit_index_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            argv = [
                "project365_photo_library_index.py",
                "--canonical-root",
                temp_dir,
                "--reexport-sidecar",
                "--index-db",
                str(Path(temp_dir) / "custom.sqlite"),
            ]

            with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    photo_index.main()

        self.assertNotEqual(raised.exception.code, 0)

    def test_legacy_video_sidecar_derives_roots_from_main_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            old_root = base / "old iCloud Photos"
            old_root.mkdir()
            photo = old_root / "2022-03-14 123456.jpg"
            video = old_root / "2022-03-14 123457.mov"
            photo.write_bytes(b"fake image bytes")
            video.write_bytes(b"fake video bytes")
            main_db = canonical_root / "photo_library_index.sqlite"
            with sqlite3.connect(main_db) as connection:
                connection.execute("CREATE TABLE photo_library_files (path TEXT PRIMARY KEY, root TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    (str(photo.resolve()), str(old_root)),
                )
                connection.commit()
            argv = [
                "project365_photo_library_index.py",
                "--canonical-root",
                str(canonical_root),
                "--legacy-video-sidecar",
                "--reset",
            ]

            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                photo_index, "_exiftool_photo_metadata", return_value={}
            ), mock.patch("sys.stdout", new_callable=io.StringIO):
                exit_code = photo_index.main()

            legacy_db = canonical_root / "video_library_legacy_index.sqlite"

            self.assertEqual(exit_code, 0)
            self.assertTrue(legacy_db.exists())
            with sqlite3.connect(legacy_db) as connection:
                rows = connection.execute(
                    "SELECT filename FROM photo_library_files ORDER BY filename"
                ).fetchall()

            self.assertEqual(rows, [(video.name,)])

    def test_index_stores_media_dimensions_and_duration_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            video = library_root / "2022-03-14 123457.mov"
            video.write_bytes(b"fake video bytes")
            index_db = base / "video.sqlite"
            with mock.patch.object(
                photo_index,
                "_exiftool_photo_metadata",
                return_value={
                    str(video.resolve()): photo_index.ExiftoolPhotoMetadata(
                        capture_timestamp="2022-03-14T12:34:57",
                        capture_timestamp_source="quicktime_create_date",
                        media_width=3840,
                        media_height=2160,
                        media_duration_seconds=12.5,
                    )
                },
            ):
                photo_index.build_photo_library_index(
                    index_db,
                    [library_root],
                    reset=True,
                    media_extensions=photo_index.VIDEO_EXTENSIONS,
                )

            with sqlite3.connect(index_db) as connection:
                row = connection.execute(
                    """
                    SELECT capture_timestamp, media_width, media_height, media_duration_seconds
                    FROM photo_library_files
                    WHERE path = ?
                    """,
                    (str(video.resolve()),),
                ).fetchone()

            self.assertEqual(row, ("2022-03-14T12:34:57", 3840, 2160, 12.5))

    def test_media_helpers_parse_numeric_video_facts(self) -> None:
        record = {
            "Track1:ImageWidth": "3840",
            "Track1:ImageHeight": "2160",
            "QuickTime:Duration": "12.5",
        }

        self.assertEqual(photo_index._media_dimensions_from_exiftool_record(record), (3840, 2160))
        self.assertEqual(photo_index._media_duration_from_exiftool_record(record), 12.5)

    def test_media_helpers_parse_composite_image_size(self) -> None:
        record = {"Composite:ImageSize": "3840 2160"}

        self.assertEqual(photo_index._media_dimensions_from_exiftool_record(record), (3840, 2160))


if __name__ == "__main__":
    unittest.main()
