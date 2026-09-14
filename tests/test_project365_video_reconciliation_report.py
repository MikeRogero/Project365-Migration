from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import project365_video_reconciliation_report as report


class Project365VideoReconciliationReportTests(unittest.TestCase):
    def test_report_classifies_metadata_candidates_without_deletion_safe_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            reexport_db = base / "reexport.sqlite"
            legacy_db = base / "legacy.sqlite"
            _write_index(
                reexport_db,
                [
                    _video_row(
                        "/new/IMG_0001.MOV",
                        root="/new",
                        capture_timestamp="2021-05-04T12:00:00",
                        dates=("2021-05-04",),
                        width=1920,
                        height=1080,
                        duration=10.0,
                        has_gps=1,
                    )
                ],
            )
            _write_index(
                legacy_db,
                [
                    _video_row(
                        "/old/IMG_0001.MOV",
                        root="/old",
                        capture_timestamp="2021-05-04T12:00:01",
                        dates=("2021-05-04",),
                        width=3840,
                        height=2160,
                        duration=10.4,
                        has_gps=0,
                    )
                ],
            )

            summary = report.generate_video_reconciliation_report(
                reexport_db=reexport_db,
                legacy_db=legacy_db,
                report_dir=base / "reports",
            )

            self.assertEqual(summary.new_videos_with_candidate_legacy_matches, 1)
            self.assertEqual(summary.candidate_legacy_matches_4k_or_higher, 1)
            self.assertEqual(summary.legacy_higher_resolution_missing_gps_new_has_gps, 1)
            self.assertEqual(summary.ambiguous_cluster_count, 0)
            self.assertEqual(summary.candidate_row_count, 1)

            csv_rows = _read_csv(Path(summary.candidate_report_path))
            self.assertEqual(csv_rows[0]["classification"], "candidate_same_video")
            self.assertEqual(csv_rows[0]["deletion_safety"], "not_deletion_safe_metadata_only")
            self.assertEqual(csv_rows[0]["legacy_is_4k_or_higher"], "1")
            self.assertEqual(csv_rows[0]["legacy_higher_resolution_missing_gps_new_has_gps"], "1")
            self.assertIn("duration_within_tolerance", csv_rows[0]["match_reasons"])

            summary_payload = json.loads(Path(summary.summary_report_path).read_text())
            self.assertEqual(summary_payload["new_videos_with_candidate_legacy_matches"], 1)
            self.assertEqual(summary_payload["candidate_legacy_matches_4k_or_higher"], 1)
            self.assertEqual(summary_payload["classification_note"], "metadata-only candidate same-video; not deletion-safe")
            self.assertEqual(summary_payload["html_report_path"], summary.html_report_path)

            html = Path(summary.html_report_path).read_text()
            self.assertIn("Project365 Video Candidate Review", html)
            self.assertIn('id="reportData"', html)
            self.assertIn("Metadata does not establish a deletion-safe duplicate.", html)
            self.assertIn("Legacy 4K+", html)

    def test_ambiguous_many_to_many_candidates_are_counted_as_review_clusters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            reexport_db = base / "reexport.sqlite"
            legacy_db = base / "legacy.sqlite"
            _write_index(
                reexport_db,
                [
                    _video_row(
                        "/new/IMG_1000.MOV",
                        root="/new",
                        capture_timestamp="2021-06-01T09:00:00",
                        dates=("2021-06-01",),
                        duration=20.0,
                    ),
                    _video_row(
                        "/new/IMG_1000 copy.MOV",
                        root="/new",
                        capture_timestamp="2021-06-01T09:00:01",
                        dates=("2021-06-01",),
                        duration=20.0,
                    ),
                ],
            )
            _write_index(
                legacy_db,
                [
                    _video_row(
                        "/old/IMG_1000.MOV",
                        root="/old",
                        capture_timestamp="2021-06-01T09:00:00",
                        dates=("2021-06-01",),
                        duration=20.0,
                    ),
                    _video_row(
                        "/old/IMG_1000 alternate.MOV",
                        root="/old",
                        capture_timestamp="2021-06-01T09:00:01",
                        dates=("2021-06-01",),
                        duration=20.0,
                    ),
                ],
            )

            summary = report.generate_video_reconciliation_report(
                reexport_db=reexport_db,
                legacy_db=legacy_db,
                report_dir=base / "reports",
            )

            csv_rows = _read_csv(Path(summary.candidate_report_path))
            self.assertEqual(summary.ambiguous_cluster_count, 1)
            self.assertEqual({row["ambiguous_cluster"] for row in csv_rows}, {"1"})
            self.assertEqual({row["cluster_new_count"] for row in csv_rows}, {"2"})
            self.assertEqual({row["cluster_legacy_count"] for row in csv_rows}, {"2"})

    def test_same_date_and_duration_without_identity_anchor_is_not_enough(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            reexport_db = base / "reexport.sqlite"
            legacy_db = base / "legacy.sqlite"
            _write_index(
                reexport_db,
                [
                    _video_row(
                        "/new/IMG_2000.MOV",
                        root="/new",
                        capture_timestamp="2021-07-01T09:00:00",
                        dates=("2021-07-01",),
                        width=1920,
                        height=1080,
                        duration=15.0,
                    )
                ],
            )
            _write_index(
                legacy_db,
                [
                    _video_row(
                        "/old/VID_9999.MOV",
                        root="/old",
                        capture_timestamp="2021-07-01T12:00:00",
                        dates=("2021-07-01",),
                        width=1280,
                        height=720,
                        duration=15.2,
                    )
                ],
            )

            summary = report.generate_video_reconciliation_report(
                reexport_db=reexport_db,
                legacy_db=legacy_db,
                report_dir=base / "reports",
            )

            self.assertEqual(summary.candidate_row_count, 0)
            self.assertEqual(summary.new_videos_with_candidate_legacy_matches, 0)


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
                capture_timestamp TEXT NOT NULL DEFAULT '',
                capture_timestamp_source TEXT NOT NULL DEFAULT '',
                has_gps INTEGER NOT NULL DEFAULT 0,
                media_width INTEGER,
                media_height INTEGER,
                media_duration_seconds REAL,
                indexed_at TEXT NOT NULL DEFAULT ''
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
                    capture_timestamp,
                    capture_timestamp_source,
                    has_gps,
                    media_width,
                    media_height,
                    media_duration_seconds,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["path"],
                    row["root"],
                    row["filename"],
                    row["extension"],
                    row["byte_size"],
                    row["capture_timestamp"],
                    "test",
                    row["has_gps"],
                    row["media_width"],
                    row["media_height"],
                    row["media_duration_seconds"],
                    "2026-09-13T00:00:00+00:00",
                ),
            )
            for value in row["dates"]:
                connection.execute(
                    "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, ?)",
                    (row["path"], value, "media_creation_date"),
                )
        connection.commit()
    finally:
        connection.close()


def _video_row(
    path: str,
    root: str,
    capture_timestamp: str,
    dates: tuple[str, ...],
    width: int | None = 1920,
    height: int | None = 1080,
    duration: float | None = 10.0,
    has_gps: int = 0,
) -> dict[str, object]:
    video_path = Path(path)
    return {
        "path": path,
        "root": root,
        "filename": video_path.name,
        "extension": video_path.suffix.lower(),
        "byte_size": 1234,
        "capture_timestamp": capture_timestamp,
        "dates": dates,
        "has_gps": has_gps,
        "media_width": width,
        "media_height": height,
        "media_duration_seconds": duration,
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()
