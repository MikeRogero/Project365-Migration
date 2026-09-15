from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as canonical_importer
import project365_diary_intake as intake


class Project365DiaryIntakeTests(unittest.TestCase):
    def test_stage_drop_uses_filename_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))

            result = intake.stage_dropped_photo(
                canonical_root,
                filename="IMG_19980412_093015.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(12, 12),
            )

            self.assertEqual(result["resolved_timestamp"], "1998-04-12T09:30:15")
            self.assertEqual(result["resolved_source"], "filename_timestamp")
            self.assertFalse(result["requires_manual_datetime"])
            self.assertTrue(Path(result["path"]).exists())

    def test_stage_drop_requires_manual_datetime_without_metadata_or_filename_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))

            result = intake.stage_dropped_photo(
                canonical_root,
                filename="vacation.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(12, 12),
            )

            self.assertEqual(result["resolved_timestamp"], "")
            self.assertTrue(result["requires_manual_datetime"])
            self.assertEqual(result["date_choices"], [])

    def test_commit_secondary_entry_creates_exportable_source_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))
            staged = intake.stage_dropped_photo(
                canonical_root,
                filename="IMG_19980412_093015.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(12, 12),
            )

            result = intake.commit_intake_photo(
                canonical_root,
                staged_path=staged["path"],
                mode="secondary",
                timestamp="1998-04-12T09:30:15",
                timestamp_source="filename_timestamp",
                original_timestamp=staged["resolved_timestamp"],
                original_timestamp_source=staged["resolved_source"],
                text="A small note.",
            )

            self.assertTrue(result["entry_id"].startswith("project365:1998-04-12:sub:"))
            self.assertEqual(result["entry"]["source_app"], intake.INTAKE_SOURCE_APP)
            self.assertTrue(result["entry"]["text_present"])
            self.assertEqual(result["entry"]["media"][0]["role"], "external_original_reference")
            self.assertEqual(result["entry"]["media"][0]["capture_timestamp"], "1998-04-12T09:30:15")

    def test_commit_additional_photo_preserves_main_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))
            first = intake.stage_dropped_photo(
                canonical_root,
                filename="IMG_19980412_093015.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(12, 12),
            )
            second = intake.stage_dropped_photo(
                canonical_root,
                filename="IMG_19980412_100000.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(13, 13),
            )
            main = intake.commit_intake_photo(
                canonical_root,
                staged_path=first["path"],
                mode="main",
                timestamp="1998-04-12T09:30:15",
                timestamp_source="filename_timestamp",
            )

            additional = intake.commit_intake_photo(
                canonical_root,
                staged_path=second["path"],
                mode="additional",
                timestamp="1998-04-12T10:00:00",
                timestamp_source="filename_timestamp",
            )

            self.assertEqual(main["entry_id"], "project365:1998-04-12")
            roles = [media["role"] for media in additional["entry"]["media"]]
            self.assertEqual(roles, ["external_original_reference", "external_original_associated_photo"])

    def test_commit_main_photo_replaces_prior_external_reference_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))
            first = intake.stage_dropped_photo(
                canonical_root,
                filename="IMG_19980412_093015.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(12, 12),
            )
            second = intake.stage_dropped_photo(
                canonical_root,
                filename="IMG_19980412_100000.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(13, 13),
            )

            intake.commit_intake_photo(
                canonical_root,
                staged_path=first["path"],
                mode="main",
                timestamp="1998-04-12T09:30:15",
                timestamp_source="filename_timestamp",
            )
            result = intake.commit_intake_photo(
                canonical_root,
                staged_path=second["path"],
                mode="main",
                timestamp="1998-04-12T10:00:00",
                timestamp_source="filename_timestamp",
            )

            self.assertEqual([media["role"] for media in result["entry"]["media"]], ["external_original_reference"])
            self.assertEqual(result["entry"]["media"][0]["capture_timestamp"], "1998-04-12T10:00:00")

    def test_nearby_photos_reads_photo_library_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _sample_canonical_root(base)
            source = base / "indexed.jpg"
            payload = _jpeg_with_dimensions(12, 12)
            source.write_bytes(payload)
            _write_photo_index(canonical_root, source, "1998-04-12", hashlib.sha256(payload).hexdigest())

            result = intake.nearby_photos(canonical_root, "1998-04-12")

            self.assertEqual(result["photos"][0]["path"], str(source))
            self.assertEqual(result["photos"][0]["capture_timestamp"], "1998-04-12T09:30:15")


def _sample_canonical_root(base: Path) -> Path:
    import_dir = base / "Import"
    canonical_root = base / "Project365Canonical"
    import_dir.mkdir()
    _write_zip(
        import_dir / "1998-04.zip",
        {
            "1998-04-12.txt": b"private",
            "1998-04-12.png": _tiny_png(),
        },
    )
    canonical_importer.import_project365_exports(
        import_dir=import_dir,
        canonical_root=canonical_root,
    )
    return canonical_root


def _write_photo_index(canonical_root: Path, source: Path, entry_date: str, digest: str) -> None:
    with sqlite3.connect(canonical_root / "photo_library_index.sqlite") as connection:
        connection.execute(
            """
            CREATE TABLE photo_library_files (
                path TEXT PRIMARY KEY,
                root TEXT NOT NULL,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                filesystem_mtime_utc TEXT NOT NULL,
                filesystem_mtime_ns INTEGER NOT NULL DEFAULT 0,
                filename_dates TEXT NOT NULL,
                media_creation_dates TEXT NOT NULL,
                filesystem_dates TEXT NOT NULL,
                capture_timestamp TEXT NOT NULL DEFAULT '',
                capture_timestamp_source TEXT NOT NULL DEFAULT '',
                gps_latitude REAL,
                gps_longitude REAL,
                gps_source TEXT NOT NULL DEFAULT '',
                has_gps INTEGER NOT NULL DEFAULT 0,
                media_width INTEGER,
                media_height INTEGER,
                media_duration_seconds REAL,
                sha256 TEXT NOT NULL DEFAULT '',
                metadata_version TEXT NOT NULL DEFAULT '',
                quality_score INTEGER NOT NULL DEFAULT 0,
                quality_evidence TEXT NOT NULL DEFAULT '',
                indexed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE photo_library_dates (
                file_path TEXT NOT NULL,
                date TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (file_path, date, source)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO photo_library_files (
                path, root, filename, extension, byte_size, filesystem_mtime_utc,
                filename_dates, media_creation_dates, filesystem_dates, capture_timestamp,
                capture_timestamp_source, sha256, indexed_at
            )
            VALUES (?, ?, ?, '.jpg', ?, '2026-09-14T00:00:00+00:00',
                    ?, ?, '', '1998-04-12T09:30:15', 'test', ?, '2026-09-14T00:00:00+00:00')
            """,
            (str(source), str(source.parent), source.name, source.stat().st_size, entry_date, entry_date, digest),
        )
        connection.execute(
            "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, 'media_creation_date')",
            (str(source), entry_date),
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


def _jpeg_with_dimensions(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"
        b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        + b"\xff\xd9"
    )


if __name__ == "__main__":
    unittest.main()
