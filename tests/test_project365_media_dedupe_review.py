from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import project365_media_dedupe_review as dedupe


class Project365MediaDedupeReviewTests(unittest.TestCase):
    def test_video_candidate_page_excludes_reviewed_decisions_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = Path(temp_dir) / "Project365Canonical"
            canonical_root.mkdir()
            _write_index(
                canonical_root / "photo_library_reexport_index.sqlite",
                [
                    _media_row(
                        "/new/IMG_0001.MOV",
                        root="/new",
                        extension=".mov",
                        capture_timestamp="2021-05-04T12:00:00",
                        dates=("2021-05-04",),
                        width=1920,
                        height=1080,
                        duration=10.0,
                    )
                ],
            )
            _write_index(
                canonical_root / "video_library_legacy_index.sqlite",
                [
                    _media_row(
                        "/old/IMG_0001.MOV",
                        root="/old",
                        extension=".mov",
                        capture_timestamp="2021-05-04T12:00:01",
                        dates=("2021-05-04",),
                        width=1920,
                        height=1080,
                        duration=10.4,
                    )
                ],
            )

            page = dedupe.candidate_page(canonical_root, "video")
            candidate = page["candidates"][0]
            decision = dedupe.record_decision(
                canonical_root,
                media_type="video",
                candidate_key=str(candidate["candidate_key"]),
                reexport_path=str(candidate["reexport"]["path"]),
                legacy_path=str(candidate["legacy"]["path"]),
                decision="confirm_duplicate_delete_legacy",
                notes="verified in review",
            )
            unreviewed = dedupe.candidate_page(canonical_root, "video")
            all_rows = dedupe.candidate_page(canonical_root, "video", status="all")

            self.assertEqual(page["total_count"], 1)
            self.assertEqual(candidate["classification"], "candidate_same_media")
            self.assertEqual(candidate["deletion_safety"], "decision_only_no_file_action")
            self.assertEqual(decision["file_action_taken"], False)
            self.assertEqual(unreviewed["total_count"], 0)
            self.assertEqual(all_rows["total_count"], 1)
            self.assertEqual(all_rows["candidates"][0]["decision"]["decision"], "confirm_duplicate_delete_legacy")

    def test_photo_candidate_uses_reexport_sidecar_against_main_photo_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = Path(temp_dir) / "Project365Canonical"
            canonical_root.mkdir()
            payload_hash = "a" * 64
            _write_index(
                canonical_root / "photo_library_reexport_index.sqlite",
                [
                    _media_row(
                        "/new/IMG_1000.JPG",
                        root="/new",
                        extension=".jpg",
                        sha256=payload_hash,
                        capture_timestamp="2021-06-01T09:00:00",
                        dates=("2021-06-01",),
                        width=4032,
                        height=3024,
                    )
                ],
            )
            _write_index(
                canonical_root / "photo_library_index.sqlite",
                [
                    _media_row(
                        "/old/IMG_1000.JPG",
                        root="/old",
                        extension=".jpg",
                        sha256=payload_hash,
                        capture_timestamp="2021-06-01T09:00:00",
                        dates=("2021-06-01",),
                        width=4032,
                        height=3024,
                    )
                ],
            )

            page = dedupe.candidate_page(canonical_root, "photo")

            self.assertEqual(page["media_type"], "photo")
            self.assertEqual(page["total_count"], 1)
            self.assertIn("exact_hash_size_match", page["candidates"][0]["reasons"])
            self.assertEqual(page["candidates"][0]["reexport"]["media_width"], 4032)


def _write_index(index_db: Path, rows: list[dict[str, object]]) -> None:
    connection = sqlite3.connect(index_db)
    try:
        connection.execute(
            """
            CREATE TABLE photo_library_files (
                path TEXT PRIMARY KEY,
                root TEXT NOT NULL,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                capture_timestamp TEXT NOT NULL DEFAULT '',
                has_gps INTEGER NOT NULL DEFAULT 0,
                media_width INTEGER,
                media_height INTEGER,
                media_duration_seconds REAL
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
        for row in rows:
            connection.execute(
                """
                INSERT INTO photo_library_files (
                    path,
                    root,
                    filename,
                    extension,
                    byte_size,
                    sha256,
                    capture_timestamp,
                    has_gps,
                    media_width,
                    media_height,
                    media_duration_seconds
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["path"],
                    row["root"],
                    row["filename"],
                    row["extension"],
                    row["byte_size"],
                    row["sha256"],
                    row["capture_timestamp"],
                    row["has_gps"],
                    row["media_width"],
                    row["media_height"],
                    row["media_duration_seconds"],
                ),
            )
            for date_value in row["dates"]:
                connection.execute(
                    "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, ?)",
                    (row["path"], date_value, "media_creation_date"),
                )
        connection.commit()
    finally:
        connection.close()


def _media_row(
    path: str,
    root: str,
    extension: str,
    capture_timestamp: str,
    dates: tuple[str, ...],
    width: int,
    height: int,
    duration: float | None = None,
    sha256: str = "",
    has_gps: int = 1,
) -> dict[str, object]:
    return {
        "path": path,
        "root": root,
        "filename": Path(path).name,
        "extension": extension,
        "byte_size": 1234,
        "sha256": sha256,
        "capture_timestamp": capture_timestamp,
        "dates": dates,
        "has_gps": has_gps,
        "media_width": width,
        "media_height": height,
        "media_duration_seconds": duration,
    }


if __name__ == "__main__":
    unittest.main()
