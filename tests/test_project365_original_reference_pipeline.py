from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

import project365_canonical_importer as canonical_importer
import project365_media_derivatives as derivatives
import project365_photo_library_index as photo_index
import project365_original_reference_pipeline as pipeline


class Project365OriginalReferencePipelineTests(unittest.TestCase):
    def test_review_crop_from_row_preserves_rotation(self) -> None:
        crop = pipeline._review_crop_from_row(
            {
                "review_crop_x": "1",
                "review_crop_y": "2",
                "review_crop_size": "3",
                "review_crop_candidate_width": "10",
                "review_crop_candidate_height": "12",
                "review_crop_source": "manual",
                "review_crop_fill_color": "#fefefe",
                "review_crop_rotation_degrees": "12.5",
            }
        )

        self.assertEqual(crop["rotation_degrees"], 12.5)
        self.assertEqual(crop["fill_color"], "#fefefe")

    def test_photo_index_extracts_full_filename_timestamp(self) -> None:
        path = Path("2003-12-01 115115 - Improved.jpg")

        self.assertEqual(
            photo_index._filename_timestamps(path),
            {"2003-12-01T11:51:15"},
        )

    def test_photo_index_prefers_embedded_capture_time_and_rejects_modification_time(self) -> None:
        timestamp, source = photo_index._best_exiftool_capture_timestamp(
            {
                "EXIF:DateTimeOriginal": "2003:12:01 10:11:12+08:00",
                "XMP-xmp:ModifyDate": "2026:08:17 09:00:00+08:00",
                "File:FileModifyDate": "2026:08:17 09:00:00+08:00",
            }
        )

        self.assertEqual(timestamp, "2003-12-01T10:11:12+08:00")
        self.assertEqual(source, "exif_datetime_original")

    def test_photo_index_accepts_xmp_capture_fields_and_rejects_xmp_modification_fields(self) -> None:
        timestamp, source = photo_index._best_exiftool_capture_timestamp(
            {
                "XMP-xmp:CreateDate": "2003:12:01 10:11:12-0500",
                "XMP-xmp:ModifyDate": "2026:08:17 09:00:00+08:00",
                "XMP-xmp:MetadataDate": "2026:08:17 09:00:00+08:00",
            }
        )

        self.assertEqual(timestamp, "2003-12-01T10:11:12-05:00")
        self.assertEqual(source, "xmp_create_date")
        self.assertEqual(
            photo_index._best_exiftool_capture_timestamp(
                {
                    "XMP-xmp:ModifyDate": "2026:08:17 09:00:00+08:00",
                    "XMP-xmp:MetadataDate": "2026:08:17 09:00:00+08:00",
                }
            ),
            ("", ""),
        )

    def test_photo_index_accepts_heif_exif_capture_field(self) -> None:
        timestamp, source = photo_index._best_exiftool_capture_timestamp(
            {
                "SourceFile": "photo.heif",
                "EXIF:DateTimeOriginal": "2003:12:01 10:11:12+08:00",
            }
        )

        self.assertEqual(timestamp, "2003-12-01T10:11:12+08:00")
        self.assertEqual(source, "exif_datetime_original")
        self.assertEqual(photo_index._mime_type(Path("photo.heif")), "image/heic")
        self.assertEqual(pipeline._mime_type(Path("photo.heif")), "image/heic")

    def test_photo_index_uses_gps_datetime_only_when_date_and_time_are_paired(self) -> None:
        self.assertEqual(
            photo_index._best_exiftool_capture_timestamp(
                {
                    "EXIF:GPSDateStamp": "2003:12:01",
                    "EXIF:GPSTimeStamp": "10:11:12",
                }
            ),
            ("", ""),
        )

        timestamp, source = photo_index._best_exiftool_capture_timestamp(
            {"Composite:GPSDateTime": "2003:12:01 10:11:12Z"}
        )

        self.assertEqual(timestamp, "2003-12-01T10:11:12+00:00")
        self.assertEqual(source, "gps_datetime_utc")

    def test_photo_index_accepts_quicktime_creation_and_rejects_quicktime_modification(self) -> None:
        timestamp, source = photo_index._best_exiftool_capture_timestamp(
            {
                "QuickTime:ContentCreateDate": "2003:12:01 10:11:12+08:00",
                "QuickTime:ModifyDate": "2026:08:17 09:00:00+08:00",
                "QuickTime:TrackModifyDate": "2026:08:17 09:00:00+08:00",
            }
        )

        self.assertEqual(timestamp, "2003-12-01T10:11:12+08:00")
        self.assertEqual(source, "quicktime_content_create_date")
        self.assertEqual(
            photo_index._best_exiftool_capture_timestamp(
                {
                    "QuickTime:ModifyDate": "2026:08:17 09:00:00+08:00",
                    "QuickTime:TrackModifyDate": "2026:08:17 09:00:00+08:00",
                    "QuickTime:MediaModifyDate": "2026:08:17 09:00:00+08:00",
                }
            ),
            ("", ""),
        )

    def test_photo_index_reads_embedded_capture_time_through_exiftool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "photo.jpg"
            path.write_bytes(_jpeg_with_exif_date("2003:12:01 10:11:12"))

            metadata = photo_index._exiftool_capture_metadata([path])

            self.assertEqual(
                metadata[str(path.resolve())],
                ("2003-12-01T10:11:12", "exif_datetime_original"),
            )

    def test_photo_index_parses_numeric_gps_coordinates_from_exiftool_record(self) -> None:
        latitude, longitude, source = photo_index._gps_coordinates_from_exiftool_record(
            {
                "Composite:GPSLatitude": 25.033,
                "Composite:GPSLongitude": 121.565,
            }
        )

        self.assertEqual(latitude, 25.033)
        self.assertEqual(longitude, 121.565)
        self.assertEqual(source, "composite_gps")

        latitude, longitude, source = photo_index._gps_coordinates_from_exiftool_record(
            {
                "GPS:GPSLatitude": "25.033",
                "GPS:GPSLatitudeRef": "S",
                "GPS:GPSLongitude": "121.565",
                "GPS:GPSLongitudeRef": "W",
            }
        )

        self.assertEqual(latitude, -25.033)
        self.assertEqual(longitude, -121.565)
        self.assertEqual(source, "embedded_gps")

    def test_photo_index_stores_embedded_gps_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            geotagged = library_root / "2004-01-01 geotagged.jpg"
            geotagged.write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            with mock.patch.object(
                photo_index,
                "_exiftool_photo_metadata",
                return_value={
                    str(geotagged.resolve()): photo_index.ExiftoolPhotoMetadata(
                        capture_timestamp="2004-01-01T08:09:10+08:00",
                        capture_timestamp_source="exif_datetime_original",
                        gps_latitude=25.033,
                        gps_longitude=121.565,
                        gps_source="composite_gps",
                    )
                },
            ):
                photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            with sqlite3.connect(index_db) as connection:
                row = connection.execute(
                    """
                    SELECT gps_latitude, gps_longitude, gps_source
                    FROM photo_library_files
                    WHERE path = ?
                    """,
                    (str(geotagged.resolve()),),
                ).fetchone()

            self.assertEqual(row, (25.033, 121.565, "composite_gps"))
            candidate = photo_index.query_index_candidates(index_db, {"2004-01-01"})["2004-01-01"][0]
            self.assertEqual(candidate["gps_latitude"], 25.033)
            self.assertEqual(candidate["gps_longitude"], 121.565)
            self.assertEqual(candidate["gps_source"], "composite_gps")

    def test_photo_index_skips_unchanged_files_at_current_metadata_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            photo = library_root / "2004-01-01 unchanged.jpg"
            payload = _tiny_png()
            photo.write_bytes(payload)
            index_db = base / "index.sqlite"
            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            with mock.patch.object(photo_index, "_exiftool_photo_metadata") as metadata:
                summary = photo_index.build_photo_library_index(index_db, [library_root], reset=False)

            metadata.assert_not_called()
            self.assertEqual(summary.scanned_file_count, 1)
            self.assertEqual(summary.indexed_file_count, 0)
            self.assertEqual(summary.skipped_file_count, 1)
            candidate = photo_index.query_index_candidates(index_db, {"2004-01-01"})["2004-01-01"][0]
            self.assertEqual(candidate["candidate_sha256"], hashlib.sha256(payload).hexdigest())

    def test_photo_index_refreshes_unchanged_files_from_old_metadata_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            photo = library_root / "2004-01-01 old-version.jpg"
            photo.write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                photo_index.build_photo_library_index(index_db, [library_root], reset=True)
            with sqlite3.connect(index_db) as connection:
                connection.execute("UPDATE photo_library_files SET metadata_version = ''")

            with mock.patch.object(
                photo_index,
                "_exiftool_photo_metadata",
                return_value={
                    str(photo.resolve()): photo_index.ExiftoolPhotoMetadata(
                        gps_latitude=25.033,
                        gps_longitude=121.565,
                        gps_source="composite_gps",
                    )
                },
            ):
                summary = photo_index.build_photo_library_index(index_db, [library_root], reset=False)

            with sqlite3.connect(index_db) as connection:
                row = connection.execute(
                    "SELECT gps_latitude, gps_longitude, metadata_version FROM photo_library_files WHERE path = ?",
                    (str(photo.resolve()),),
                ).fetchone()

            self.assertEqual(summary.indexed_file_count, 1)
            self.assertEqual(summary.skipped_file_count, 0)
            self.assertEqual(row, (25.033, 121.565, photo_index.PHOTO_INDEX_METADATA_VERSION))

    def test_photo_index_refreshes_file_when_mtime_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            photo = library_root / "2004-01-01 resized.jpg"
            photo.write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                photo_index.build_photo_library_index(index_db, [library_root], reset=True)
            photo.write_bytes(_tiny_png() + b"updated")
            os.utime(photo, (2000000000, 2000000000))

            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                summary = photo_index.build_photo_library_index(index_db, [library_root], reset=False)

            self.assertEqual(summary.scanned_file_count, 1)
            self.assertEqual(summary.indexed_file_count, 1)
            self.assertEqual(summary.skipped_file_count, 0)

    def test_photo_index_orders_same_date_candidates_by_capture_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            later = library_root / "2004-01-01 120000 later.jpg"
            earlier = library_root / "2004-01-01 090000 earlier.jpg"
            later.write_bytes(_tiny_png() + b"larger")
            earlier.write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            candidates = photo_index.query_index_candidates(index_db, {"2004-01-01"})["2004-01-01"]

            self.assertEqual(
                [row["candidate_filename"] for row in candidates],
                [earlier.name, later.name],
            )
            self.assertEqual(candidates[0]["capture_timestamp"], "2004-01-01T09:00:00")
            self.assertEqual(candidates[0]["capture_timestamp_source"], "filename_timestamp")

    def test_photo_index_reports_capture_source_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            timed = library_root / "2004-01-01 090000 timed.jpg"
            timeless = library_root / "2004-01-01 timeless.jpg"
            timed.write_bytes(_tiny_png())
            timeless.write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                summary = photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            self.assertEqual(summary.capture_source_counts, {"filename_timestamp": 1})

    def test_queue_enrichment_counts_only_candidates_with_capture_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            timed = library_root / "2004-01-01 090000 timed.jpg"
            date_only = library_root / "2004-01-01 date-only.jpg"
            timed.write_bytes(_tiny_png())
            date_only.write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            with mock.patch.object(photo_index, "_exiftool_photo_metadata", return_value={}):
                photo_index.build_photo_library_index(index_db, [library_root], reset=True)
            queue_path = base / "queue.csv"
            _write_csv(
                queue_path,
                [
                    {"entry_date": "2004-01-01", "candidate_path": str(timed.resolve())},
                    {"entry_date": "2004-01-01", "candidate_path": str(date_only.resolve())},
                ],
                ["entry_date", "candidate_path"],
            )

            enriched = photo_index.enrich_candidate_queue(index_db, queue_path)

            self.assertEqual(enriched, 1)

    def test_source_scan_exposes_embedded_capture_timestamp_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = root / "untimed.jpg"
            candidate.write_bytes(_tiny_png())
            with mock.patch.object(
                pipeline,
                "_embedded_capture_timestamp",
                return_value=("2004-01-01T08:09:10+08:00", "xmp_create_date"),
            ):
                candidates = pipeline._scan_search_roots([root], {"2004-01-01"}, True)["2004-01-01"]

            self.assertEqual(candidates[0]["candidate_filename"], candidate.name)
            self.assertEqual(candidates[0]["capture_timestamp"], "2004-01-01T08:09:10+08:00")
            self.assertEqual(candidates[0]["capture_timestamp_source"], "xmp_create_date")
            self.assertIn("media_creation_date", candidates[0]["evidence"])

    def test_photo_index_explicit_range_returns_every_candidate_within_requested_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            for filename in [
                "2004-01-01 exact.jpg",
                "2003-12-29 minus-three.jpg",
                "2004-01-04 plus-three.jpg",
                "2004-01-06 plus-five.jpg",
            ]:
                library_root.joinpath(filename).write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            candidates = photo_index.query_index_candidates(
                index_db,
                {"2004-01-01"},
                max_distance_days=3,
            )["2004-01-01"]

            self.assertEqual(
                {row["candidate_filename"] for row in candidates},
                {
                    "2004-01-01 exact.jpg",
                    "2003-12-29 minus-three.jpg",
                    "2004-01-04 plus-three.jpg",
                },
            )

    def test_photo_index_expands_past_png_only_exact_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            exact_png = library_root / "2004-01-01 export.png"
            nearby_jpg = library_root / "2003-12-31 improved.jpg"
            exact_png.write_bytes(_tiny_png())
            nearby_jpg.write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            candidates = photo_index.query_index_candidates(index_db, {"2004-01-01"})["2004-01-01"]

            self.assertEqual(
                {row["candidate_filename"] for row in candidates},
                {exact_png.name, nearby_jpg.name},
            )
            nearby = next(row for row in candidates if row["candidate_filename"] == nearby_jpg.name)
            self.assertIn("date_within_1_days", nearby["evidence"])

    def test_source_scan_expands_past_png_only_exact_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exact_png = root / "2004-01-01 export.png"
            nearby_jpg = root / "2003-12-31 improved.jpg"
            exact_png.write_bytes(_tiny_png())
            nearby_jpg.write_bytes(_tiny_png())

            candidates = pipeline._scan_search_roots([root], {"2004-01-01"}, False)["2004-01-01"]

            self.assertEqual(
                {row["candidate_filename"] for row in candidates},
                {exact_png.name, nearby_jpg.name},
            )
            nearby = next(row for row in candidates if row["candidate_filename"] == nearby_jpg.name)
            self.assertIn("date_within_1_days", nearby["evidence"])

    def test_photo_index_returns_every_candidate_in_the_first_matching_date_tier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            for index in range(205):
                library_root.joinpath(f"2004-01-01 candidate {index:03d}.jpg").write_bytes(_tiny_png())
            index_db = base / "index.sqlite"
            photo_index.build_photo_library_index(index_db, [library_root], reset=True)

            candidates = photo_index.query_index_candidates(index_db, {"2004-01-01"})["2004-01-01"]

            self.assertEqual(len(candidates), 205)

    def test_photo_index_uses_staged_capture_date_fallback_and_ignores_filesystem_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            library_root = base / "library"
            library_root.mkdir()
            exact = library_root / "2004-01-01 exact.jpg"
            one_day = library_root / "2004-01-02 nearby.jpg"
            three_days = library_root / "2004-01-04 wider.jpg"
            filesystem_only = library_root / "unrelated.jpg"
            for path in [exact, one_day, three_days, filesystem_only]:
                path.write_bytes(_tiny_png())
            filesystem_timestamp = 1072915200  # 2004-01-01 UTC
            os.utime(filesystem_only, (filesystem_timestamp, filesystem_timestamp))
            index_db = base / "index.sqlite"
            photo_index.build_photo_library_index(index_db, [library_root], reset=True)
            with sqlite3.connect(index_db) as connection:
                self.assertIsNotNone(
                    connection.execute(
                        """
                        SELECT 1
                        FROM sqlite_master
                        WHERE type = 'index'
                            AND name = 'idx_photo_library_dates_source_date_path'
                        """
                    ).fetchone()
                )

            exact_rows = photo_index.query_index_candidates(index_db, {"2004-01-01"})["2004-01-01"]
            self.assertEqual([row["candidate_filename"] for row in exact_rows], [exact.name])

            one_day_rows = photo_index.query_index_candidates(index_db, {"2004-01-03"})["2004-01-03"]
            self.assertEqual(
                [row["candidate_filename"] for row in one_day_rows],
                [one_day.name, three_days.name],
            )

            three_day_rows = photo_index.query_index_candidates(index_db, {"2004-01-07"})["2004-01-07"]
            self.assertEqual([row["candidate_filename"] for row in three_day_rows], [three_days.name])

            filesystem_rows = photo_index.query_index_candidates(
                index_db,
                {"2004-01-01"},
                include_filesystem_dates=True,
            )["2004-01-01"]
            self.assertEqual(
                {row["candidate_filename"] for row in filesystem_rows},
                {exact.name, filesystem_only.name},
            )
            filesystem_candidate = next(
                row for row in filesystem_rows if row["candidate_filename"] == filesystem_only.name
            )
            self.assertIn("filesystem_date", filesystem_candidate["evidence"])

            filename_only_rows = photo_index.query_index_candidates(
                index_db,
                {"2004-01-01"},
                include_filesystem_dates=True,
                filename_dates_only=True,
            )["2004-01-01"]
            self.assertEqual([row["candidate_filename"] for row in filename_only_rows], [exact.name])
            self.assertIn("filename_date", filename_only_rows[0]["evidence"])
            self.assertNotIn("filesystem_date", filename_only_rows[0]["evidence"])

    def test_source_scan_uses_first_nonempty_date_tier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exact = root / "2004-01-01 exact.jpg"
            nearby = root / "2004-01-02 nearby.jpg"
            wider = root / "2004-01-04 wider.jpg"
            for path in [exact, nearby, wider]:
                path.write_bytes(_tiny_png())

            candidates = pipeline._scan_search_roots([root], {"2004-01-01"}, False)

            self.assertEqual(
                [row["candidate_filename"] for row in candidates["2004-01-01"]],
                [exact.name],
            )
            nearby_candidates = pipeline._scan_search_roots([root], {"2004-01-03"}, False)
            self.assertEqual(
                [row["candidate_filename"] for row in nearby_candidates["2004-01-03"]],
                [nearby.name, wider.name],
            )
            wider_candidates = pipeline._scan_search_roots([root], {"2004-01-07"}, False)
            self.assertEqual(
                [row["candidate_filename"] for row in wider_candidates["2004-01-07"]],
                [wider.name],
            )

    def test_exif_modification_date_is_not_a_capture_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "photo.jpg"
            path.write_bytes(_jpeg_with_exif_date("2003:12:01 10:11:12", tag=0x0132))

            self.assertEqual(photo_index._jpeg_exif_dates(path), set())
            self.assertEqual(pipeline._jpeg_exif_dates(path), set())

    def test_spotlight_capture_scan_excludes_filesystem_creation_date(self) -> None:
        completed = mock.Mock(stdout="2003-11-30 10:11:12 +0000\n")
        with mock.patch.object(pipeline.subprocess, "run", return_value=completed) as run:
            dates = pipeline._spotlight_content_creation_dates(Path("photo.heic"))

        self.assertEqual(dates, {"2003-11-30"})
        command = run.call_args.args[0]
        self.assertIn("kMDItemContentCreationDate", command)
        self.assertNotIn("kMDItemFSCreationDate", command)

    def test_parent_folder_date_does_not_count_as_filename_evidence(self) -> None:
        path = Path("2003-12-09 trip") / "IMG_1234.jpg"

        self.assertEqual(photo_index._filename_dates(path), set())
        self.assertEqual(pipeline._filename_dates(path), set())

    def test_write_csv_failure_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.csv"
            pipeline._write_csv(path, [{"entry_id": "project365:1998-04-12"}], ["entry_id"])
            before = path.read_text()

            with self.assertRaises(ValueError):
                pipeline._write_csv(
                    path,
                    [{"entry_id": "project365:1998-04-13", "unexpected": "value"}],
                    ["entry_id"],
                )

            self.assertEqual(path.read_text(), before)

    def test_append_csv_row_rewrites_when_schema_grows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "attempts.csv"
            pipeline._append_csv_row(path, {"first": "a"}, ["first"])

            pipeline._append_csv_row(
                path,
                {"first": "b", "second": "new"},
                ["first", "second"],
            )

            rows = _read_csv(path)
            self.assertEqual(rows[0], {"first": "a", "second": ""})
            self.assertEqual(rows[1], {"first": "b", "second": "new"})

    def test_search_apply_and_derivative_use_external_reference_without_copying_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            original_path = source_root / "1998-04-12 camera-original.png"
            original_path.write_bytes(_tiny_png())

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["evidence"], "filename_date")
            rows[0]["review_decision"] = "use_external_original"
            rows[0]["review_crop_x"] = "1"
            rows[0]["review_crop_y"] = "2"
            rows[0]["review_crop_size"] = "3"
            rows[0]["review_crop_candidate_width"] = "10"
            rows[0]["review_crop_candidate_height"] = "12"
            rows[0]["review_crop_source"] = "manual"
            rows[0]["review_crop_fill_color"] = "#fefefe"
            reviewed_path = base / "reviewed.csv"
            _write_csv(reviewed_path, rows, list(rows[0]))

            apply_summary = pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            self.assertEqual(apply_summary.applied_count, 1)
            self.assertEqual(apply_summary.selected_count, 1)
            self.assertEqual(apply_summary.rejected_count, 0)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                db_rows = connection.execute(
                    """
                    SELECT role, storage_path, selected_default, transformation_json
                    FROM media_assets
                    WHERE role = 'external_original_reference'
                    """
                ).fetchall()
            self.assertEqual(db_rows[0][:3], ("external_original_reference", str(original_path), 0))
            self.assertEqual(
                json.loads(db_rows[0][3])["review_crop"],
                {
                    "x": 1,
                    "y": 2,
                    "size": 3,
                    "candidate_width": 10,
                    "candidate_height": 12,
                    "source": "manual",
                    "unit": "source_pixels",
                    "shape": "square",
                    "fill_color": "#fefefe",
                },
            )
            self.assertTrue(original_path.exists())

            derivative_summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )
            derivative_rows = _read_csv(Path(derivative_summary.report_path))
            self.assertEqual(derivative_rows[0]["source_path"], str(original_path))

    def test_rejected_candidate_is_persisted_and_not_relisted_for_same_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            original_path = source_root / "1998-04-12 wrong-original.png"
            original_path.write_bytes(_tiny_png())

            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0]["review_decision"] = "rejected"
            reviewed_path = base / "reviewed.csv"
            _write_csv(reviewed_path, rows, list(rows[0]))

            apply_summary = pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            self.assertEqual(apply_summary.selected_count, 0)
            self.assertEqual(apply_summary.rejected_count, 1)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                db_rows = connection.execute(
                    """
                    SELECT role, storage_path, review_status
                    FROM media_assets
                    WHERE role = 'external_original_rejected'
                    """
                ).fetchall()
            self.assertEqual(db_rows, [("external_original_rejected", str(original_path), "rejected")])

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports_second",
                scan_metadata_dates=False,
            )

            second_rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(second_summary.candidate_count, 0)
            self.assertEqual(second_rows[0]["review_decision"], "search_needed")
            self.assertEqual(second_rows[0]["candidate_path"], "")
            group_rows = _read_csv(Path(second_summary.group_report_path))
            self.assertEqual(group_rows[0]["candidate_count"], "0")
            self.assertEqual(group_rows[0]["hidden_rejected_candidate_count"], "1")
            self.assertEqual(group_rows[0]["status"], "all_candidates_rejected")
            batch_rows = _read_csv(Path(second_summary.batch_plan_path))
            self.assertEqual(batch_rows[0]["candidate_count"], "0")
            self.assertEqual(batch_rows[0]["hidden_rejected_candidate_count"], "1")
            self.assertEqual(batch_rows[0]["statuses"], "all_candidates_rejected")

    def test_accepting_original_prunes_rejected_candidates_for_same_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            accepted_path = source_root / "1998-04-12 accepted.png"
            rejected_path = source_root / "1998-04-12 rejected.png"
            accepted_path.write_bytes(_tiny_png())
            rejected_path.write_bytes(_tiny_png() + b"rejected")

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(summary.search_queue_path))
            for row in rows:
                row["review_decision"] = (
                    "use_external_original"
                    if row["candidate_path"] == str(accepted_path)
                    else "rejected"
                )
            reviewed_path = base / "reviewed.csv"
            _write_csv(reviewed_path, rows, list(rows[0]))

            apply_summary = pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            self.assertEqual(apply_summary.selected_count, 1)
            self.assertEqual(apply_summary.rejected_count, 0)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                accepted_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
                rejected_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_rejected'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
            self.assertEqual(accepted_count, 1)
            self.assertEqual(rejected_count, 0)

    def test_fallback_decision_is_persisted_and_removed_from_unresolved_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            source_root = base / "external"
            source_root.mkdir()

            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0]["review_decision"] = "keep_project365_export"
            rows[0]["review_notes"] = "searched family archive"
            reviewed_path = base / "reviewed.csv"
            _write_csv(reviewed_path, rows, list(rows[0]))

            apply_summary = pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            self.assertEqual(apply_summary.selected_count, 0)
            self.assertEqual(apply_summary.rejected_count, 0)
            self.assertEqual(apply_summary.fallback_count, 1)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                db_rows = connection.execute(
                    """
                    SELECT role, storage_path, status, review_status, byte_size
                    FROM media_assets
                    WHERE role = 'external_original_fallback'
                    """
                ).fetchall()
            self.assertEqual(db_rows, [("external_original_fallback", "", "fallback", "confirmed", 0)])

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports_second",
                scan_metadata_dates=False,
            )

            second_rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(second_summary.unclear_entry_count, 1)
            self.assertEqual([row["entry_id"] for row in second_rows], ["project365:1998-04-13"])

    def test_apply_reviewed_deduplicates_duplicate_fallback_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 first.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-12 second.png").write_bytes(_different_tiny_png())

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(len(rows), 2)
            for row in rows:
                row["review_decision"] = "keep_project365_export"
                row["review_notes"] = "duplicate fallback rows"
            reviewed_path = base / "reviewed.csv"
            _write_csv(reviewed_path, rows, list(rows[0]))

            apply_summary = pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            self.assertEqual(apply_summary.fallback_count, 1)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM media_assets WHERE role = 'external_original_fallback'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_prune_applied_review_queue_removes_completed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            report_dir = canonical_root / "exports" / "verification_reports"
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(summary.search_queue_path))
            rows[0]["review_decision"] = "use_external_original"
            _write_csv(Path(summary.search_queue_path), rows, list(rows[0]))
            pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=Path(summary.search_queue_path),
            )

            prune_summary = pipeline.prune_applied_review_queue(
                canonical_root=canonical_root,
                queue_path=Path(summary.search_queue_path),
                report_dir=report_dir,
            )

            pruned_rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(prune_summary["removed_completed_entries"], 1)
            self.assertEqual(prune_summary["queue_rows"], 1)
            self.assertEqual([row["entry_id"] for row in pruned_rows], ["project365:1998-04-13"])

    def test_prune_applied_review_queue_hides_rejected_candidate_and_keeps_search_needed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 wrong.png").write_bytes(_tiny_png())
            report_dir = canonical_root / "exports" / "verification_reports"
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(summary.search_queue_path))
            rows[0]["review_decision"] = "rejected"
            _write_csv(Path(summary.search_queue_path), rows, list(rows[0]))
            pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=Path(summary.search_queue_path),
            )

            prune_summary = pipeline.prune_applied_review_queue(
                canonical_root=canonical_root,
                queue_path=Path(summary.search_queue_path),
                report_dir=report_dir,
            )

            pruned_rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(prune_summary["removed_rejected_candidates"], 1)
            self.assertEqual(len(pruned_rows), 1)
            self.assertEqual(pruned_rows[0]["entry_id"], "project365:1998-04-12")
            self.assertEqual(pruned_rows[0]["candidate_path"], "")
            self.assertEqual(pruned_rows[0]["review_decision"], "search_needed")
            group_rows = _read_csv(report_dir / "original_photo_unclear_groups.csv")
            self.assertEqual(group_rows[0]["candidate_count"], "0")
            self.assertEqual(group_rows[0]["hidden_rejected_candidate_count"], "1")
            self.assertEqual(group_rows[0]["status"], "all_candidates_rejected")
            batch_rows = _read_csv(report_dir / "original_photo_search_batch_plan.csv")
            self.assertEqual(batch_rows[0]["hidden_rejected_candidate_count"], "1")
            self.assertEqual(batch_rows[0]["statuses"], "all_candidates_rejected")

    def test_pending_rejected_queue_rows_do_not_count_as_review_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 wrong.png").write_bytes(_tiny_png())
            report_dir = canonical_root / "exports" / "verification_reports"
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            fieldnames, rows = pipeline._read_csv_with_fieldnames(Path(summary.search_queue_path))
            rows[0]["review_decision"] = "rejected"
            rows[0]["review_notes"] = "wrong original"

            pipeline.refresh_external_original_queue_reports(canonical_root, report_dir, rows)

            group_rows = _read_csv(report_dir / "original_photo_unclear_groups.csv")
            self.assertEqual(group_rows[0]["candidate_count"], "0")
            self.assertEqual(group_rows[0]["hidden_rejected_candidate_count"], "1")
            self.assertEqual(group_rows[0]["status"], "all_candidates_rejected")
            batch_rows = _read_csv(report_dir / "original_photo_search_batch_plan.csv")
            self.assertEqual(batch_rows[0]["candidate_count"], "0")
            self.assertEqual(batch_rows[0]["review_date_count"], "0")
            self.assertEqual(batch_rows[0]["recommended_action"], "choose_search_folder")
            self.assertEqual(batch_rows[0]["statuses"], "all_candidates_rejected")
            self.assertEqual(fieldnames[0], "entry_id")

    def test_score_review_queue_alignment_adds_local_crop_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            candidate_pixels = _make_gradient_pixels(80, 40)
            reference_pixels = _crop(candidate_pixels, 80, 20, 0, 40, 40)
            reference_path = base / "reference.bmp"
            candidate_path = base / "candidate.bmp"
            _write_bmp(reference_path, 40, 40, reference_pixels)
            _write_bmp(candidate_path, 80, 40, candidate_pixels)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                connection.execute(
                    """
                    UPDATE media_assets
                    SET storage_path = ?, mime_type = 'image/bmp'
                    WHERE id = 'project365:1998-04-12:project365_export_png'
                    """,
                    (str(reference_path),),
                )
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            queue_path.parent.mkdir(parents=True)
            _write_csv(
                queue_path,
                [
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(candidate_path),
                        "candidate_filename": candidate_path.name,
                        "candidate_sha256": "sha",
                        "byte_size": str(candidate_path.stat().st_size),
                        "mime_type": "image/bmp",
                        "review_decision": "",
                        "review_notes": "",
                    }
                ],
                pipeline._search_queue_fieldnames(),
            )

            progress_messages: list[str] = []
            summary = pipeline.score_review_queue_alignment(
                canonical_root=canonical_root,
                queue_path=queue_path,
                progress_interval=1,
                progress_sink=progress_messages.append,
            )

            rows = _read_csv(queue_path)
            self.assertEqual(summary["scored_rows"], 1)
            self.assertEqual(summary["error_rows"], 0)
            self.assertEqual(rows[0]["alignment_confidence"], "high")
            self.assertTrue(rows[0]["alignment_score"])
            self.assertTrue(rows[0]["alignment_crop"])
            self.assertEqual(rows[0]["alignment_error"], "")
            self.assertEqual(len(progress_messages), 2)
            self.assertIn("Alignment progress: 0/1 candidates", progress_messages[0])
            self.assertIn("Alignment progress: 1/1 candidates", progress_messages[1])

    def test_score_review_queue_alignment_hides_same_size_export_equivalent_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            candidate_pixels = _make_gradient_pixels(80, 40)
            reference_pixels = _crop(candidate_pixels, 80, 20, 0, 40, 40)
            reference_path = base / "reference.bmp"
            export_equivalent_path = base / "1998-04-12 export-copy.bmp"
            larger_candidate_path = base / "1998-04-12 larger-original.bmp"
            _write_bmp(reference_path, 40, 40, reference_pixels)
            _write_bmp(export_equivalent_path, 40, 40, reference_pixels)
            _write_bmp(larger_candidate_path, 80, 40, candidate_pixels)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                connection.execute(
                    """
                    UPDATE media_assets
                    SET storage_path = ?, mime_type = 'image/bmp'
                    WHERE id = 'project365:1998-04-12:project365_export_png'
                    """,
                    (str(reference_path),),
                )
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            queue_path.parent.mkdir(parents=True)
            _write_csv(
                queue_path,
                [
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(export_equivalent_path),
                        "candidate_filename": export_equivalent_path.name,
                        "candidate_sha256": "sha-copy",
                        "byte_size": str(export_equivalent_path.stat().st_size),
                        "mime_type": "image/bmp",
                    },
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(larger_candidate_path),
                        "candidate_filename": larger_candidate_path.name,
                        "candidate_sha256": "sha-larger",
                        "byte_size": str(larger_candidate_path.stat().st_size),
                        "mime_type": "image/bmp",
                    },
                ],
                pipeline._search_queue_fieldnames(),
            )

            summary = pipeline.score_review_queue_alignment(
                canonical_root=canonical_root,
                queue_path=queue_path,
            )

            rows = _read_csv(queue_path)
            self.assertEqual(summary["scored_rows"], 2)
            self.assertEqual(rows[0]["candidate_filter_reason"], "export_equivalent")
            self.assertEqual(rows[1]["candidate_filter_reason"], "")
            group_rows = _read_csv(queue_path.parent / "original_photo_unclear_groups.csv")
            self.assertEqual(group_rows[0]["candidate_count"], "1")
            self.assertEqual(group_rows[0]["hidden_export_equivalent_candidate_count"], "1")
            self.assertEqual(group_rows[0]["status"], "needs_choice")
            batch_rows = _read_csv(queue_path.parent / "original_photo_search_batch_plan.csv")
            self.assertEqual(batch_rows[0]["candidate_count"], "1")
            self.assertEqual(batch_rows[0]["hidden_export_equivalent_candidate_count"], "1")

    def test_score_review_queue_alignment_can_target_entry_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            candidate_pixels = _make_gradient_pixels(80, 40)
            reference_pixels = _crop(candidate_pixels, 80, 20, 0, 40, 40)
            reference_path = base / "reference.bmp"
            first_candidate_path = base / "candidate-first.bmp"
            second_candidate_path = base / "candidate-second.bmp"
            _write_bmp(reference_path, 40, 40, reference_pixels)
            _write_bmp(first_candidate_path, 80, 40, candidate_pixels)
            _write_bmp(second_candidate_path, 80, 40, candidate_pixels)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                for entry_date in ("1998-04-12", "1998-04-13"):
                    connection.execute(
                        """
                        UPDATE media_assets
                        SET storage_path = ?, mime_type = 'image/bmp'
                        WHERE id = ?
                        """,
                        (
                            str(reference_path),
                            f"project365:{entry_date}:project365_export_png",
                        ),
                    )
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            queue_path.parent.mkdir(parents=True)
            _write_csv(
                queue_path,
                [
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(first_candidate_path),
                        "candidate_filename": first_candidate_path.name,
                        "candidate_sha256": "sha-first",
                        "byte_size": str(first_candidate_path.stat().st_size),
                        "mime_type": "image/bmp",
                    },
                    {
                        "entry_id": "project365:1998-04-13",
                        "entry_date": "1998-04-13",
                        "project365_media_asset_id": "project365:1998-04-13:project365_export_png",
                        "candidate_path": str(second_candidate_path),
                        "candidate_filename": second_candidate_path.name,
                        "candidate_sha256": "sha-second",
                        "byte_size": str(second_candidate_path.stat().st_size),
                        "mime_type": "image/bmp",
                    },
                ],
                pipeline._search_queue_fieldnames(),
            )

            summary = pipeline.score_review_queue_alignment(
                canonical_root=canonical_root,
                queue_path=queue_path,
                target_entry_dates={"1998-04-13"},
                max_candidates=1,
            )

            rows = _read_csv(queue_path)
            self.assertEqual(summary["candidate_rows"], 1)
            self.assertEqual(summary["scored_rows"], 1)
            self.assertEqual(rows[0]["alignment_score"], "")
            self.assertTrue(rows[1]["alignment_score"])

    def test_mark_fallback_can_target_batch_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                    "1998-04-14.png": _tiny_png(),
                },
            )

            summary = pipeline.mark_fallback_external_references(
                canonical_root=canonical_root,
                review_queue_path=base / "missing_review_queue.csv",
                target_entry_ids={"project365:1998-04-12", "project365:1998-04-13"},
                start_date="1998-04-12",
                end_date="1998-04-13",
                max_targets=2,
            )

            self.assertEqual(summary.fallback_count, 2)
            with _sqlite_connection(canonical_root / "canonical.db") as connection:
                rows = connection.execute(
                    """
                    SELECT entry_id
                    FROM media_assets
                    WHERE role = 'external_original_fallback'
                    ORDER BY entry_id
                    """
                ).fetchall()
            self.assertEqual(
                rows,
                [("project365:1998-04-12",), ("project365:1998-04-13",)],
            )

    def test_search_finds_jpeg_media_creation_date_without_filename_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-13.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("IMG_9999.jpg").write_bytes(_jpeg_with_exif_date("1998:04:13 10:11:12"))

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=True,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["media_creation_dates"], "1998-04-13")
            self.assertIn("media_creation_date", rows[0]["evidence"])

    def test_search_can_target_single_unmatched_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                    "1998-04-14.png": _tiny_png(),
                },
            )
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-13 original.jpg").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-14 original.jpg").write_bytes(_tiny_png())

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
                target_entry_ids={"project365:1998-04-13"},
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.unclear_entry_count, 1)
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual([row["entry_id"] for row in rows], ["project365:1998-04-13"])
            self.assertEqual(rows[0]["candidate_filename"], "1998-04-13 original.jpg")

    def test_search_can_limit_unmatched_batch_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                    "1998-04-14.png": _tiny_png(),
                },
            )
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-13 original.jpg").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-14 original.jpg").write_bytes(_tiny_png())

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
                max_targets=2,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.unclear_entry_count, 2)
            self.assertEqual(
                [row["entry_id"] for row in rows],
                ["project365:1998-04-12", "project365:1998-04-13"],
            )

    def test_targeted_search_can_merge_with_existing_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                    "1998-04-14.png": _tiny_png(),
                },
            )
            source_root = base / "external"
            source_root.mkdir()
            pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            source_root.joinpath("1998-04-13 original.png").write_bytes(_tiny_png())

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
                target_entry_ids={"project365:1998-04-13"},
                merge_existing_queue=True,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            rows_by_entry = {row["entry_id"]: row for row in rows}
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows_by_entry["project365:1998-04-12"]["review_decision"], "search_needed")
            self.assertEqual(rows_by_entry["project365:1998-04-13"]["candidate_filename"], "1998-04-13 original.png")
            self.assertEqual(rows_by_entry["project365:1998-04-14"]["review_decision"], "search_needed")

    def test_full_search_preserves_pending_candidate_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            first_root = base / "first"
            empty_root = base / "empty"
            first_root.mkdir()
            empty_root.mkdir()
            first_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[first_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0]["review_decision"] = "use_external_original"
            _write_csv(Path(first_summary.search_queue_path), rows, list(rows[0]))

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[empty_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["candidate_filename"], "1998-04-12 original.png")
            self.assertEqual(rows[0]["review_decision"], "use_external_original")

    def test_replace_existing_queue_discards_pending_candidate_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            first_root = base / "first"
            empty_root = base / "empty"
            first_root.mkdir()
            empty_root.mkdir()
            first_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[first_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0]["review_decision"] = "use_external_original"
            _write_csv(Path(first_summary.search_queue_path), rows, list(rows[0]))

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[empty_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
                replace_existing_queue=True,
            )

            rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["candidate_filename"], "")
            self.assertEqual(rows[0]["review_decision"], "search_needed")

    def test_full_search_preserves_manual_drop_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            empty_root = base / "empty"
            empty_root.mkdir()
            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[empty_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            manual_path = base / "manual.png"
            manual_path.write_bytes(_tiny_png())
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0].update(
                {
                    "candidate_path": str(manual_path),
                    "candidate_filename": manual_path.name,
                    "candidate_sha256": "manual-sha256",
                    "evidence": "manual_drop_copy",
                    "review_decision": "",
                }
            )
            _write_csv(Path(first_summary.search_queue_path), rows, list(rows[0]))

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[empty_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["candidate_path"], str(manual_path))
            self.assertEqual(rows[0]["evidence"], "manual_drop_copy")

    def test_full_search_discards_stale_unreviewed_search_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            first_root = base / "first"
            empty_root = base / "empty"
            first_root.mkdir()
            empty_root.mkdir()
            first_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[first_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[empty_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["candidate_path"], "")
            self.assertEqual(rows[0]["review_decision"], "search_needed")

    def test_repeated_targeted_search_accumulates_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            first_root.joinpath("1998-04-12 first.png").write_bytes(_tiny_png())
            second_root.joinpath("1998-04-12 second.png").write_bytes(_different_tiny_png())

            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[first_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0]["review_notes"] = "keep this candidate"
            _write_csv(Path(first_summary.search_queue_path), rows, list(rows[0]))

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[second_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
                target_entry_ids={"project365:1998-04-12"},
                merge_existing_queue=True,
            )

            rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(
                [row["candidate_filename"] for row in rows],
                ["1998-04-12 first.png", "1998-04-12 second.png"],
            )
            self.assertEqual(rows[0]["review_notes"], "keep this candidate")

    def test_targeted_search_merge_preserves_alignment_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            first_root.joinpath("1998-04-12 first.png").write_bytes(_tiny_png())
            second_root.joinpath("1998-04-12 second.png").write_bytes(_different_tiny_png())

            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[first_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0]["alignment_score"] = "1.00"
            rows[0]["alignment_confidence"] = "high"
            _write_csv(
                Path(first_summary.search_queue_path),
                rows,
                pipeline._alignment_fieldnames(pipeline._search_queue_fieldnames()),
            )

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[second_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
                target_entry_ids={"project365:1998-04-12"},
                merge_existing_queue=True,
            )

            rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(
                [row["candidate_filename"] for row in rows],
                ["1998-04-12 first.png", "1998-04-12 second.png"],
            )
            self.assertEqual(rows[0]["alignment_score"], "1.00")
            self.assertIn("alignment_confidence", rows[1])

    def test_search_attempt_log_appends_metadata_only_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            second_root.joinpath("1998-04-13 original.png").write_bytes(_tiny_png())
            report_dir = canonical_root / "exports" / "verification_reports"

            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[first_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
                max_targets=1,
            )
            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[second_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
                target_entry_ids={"project365:1998-04-13"},
                target_entry_dates={"1998-04-13"},
                start_date="1998-04-13",
                end_date="1998-04-13",
                max_targets=1,
                merge_existing_queue=True,
            )

            self.assertEqual(first_summary.attempt_log_path, second_summary.attempt_log_path)
            rows = _read_csv(Path(second_summary.attempt_log_path))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["search_roots"], str(first_root))
            self.assertEqual(rows[0]["max_targets"], "1")
            self.assertEqual(rows[0]["unclear_entry_count"], "1")
            self.assertEqual(rows[0]["candidate_count"], "0")
            self.assertEqual(rows[0]["hidden_export_equivalent_candidate_count"], "0")
            self.assertEqual(rows[0]["scan_metadata_dates"], "false")
            self.assertEqual(rows[1]["search_roots"], str(second_root))
            self.assertEqual(rows[1]["target_entry_ids"], "project365:1998-04-13")
            self.assertEqual(rows[1]["target_entry_dates"], "1998-04-13")
            self.assertEqual(rows[1]["start_date"], "1998-04-13")
            self.assertEqual(rows[1]["end_date"], "1998-04-13")
            self.assertEqual(rows[1]["merge_existing_queue"], "true")
            self.assertEqual(rows[1]["candidate_count"], "1")
            self.assertEqual(rows[1]["hidden_export_equivalent_candidate_count"], "0")
            self.assertTrue(rows[1]["search_queue_path"].endswith("original_photo_external_search_queue.csv"))

    def test_search_writes_consecutive_unmatched_batch_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                    "1998-04-20.png": _tiny_png(),
                },
            )
            source_root = base / "external"
            source_root.mkdir()

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.batch_plan_path))
            self.assertEqual(
                [(row["start_date"], row["end_date"], row["date_count"]) for row in rows],
                [("1998-04-12", "1998-04-13", "2"), ("1998-04-20", "1998-04-20", "1")],
            )
            self.assertEqual(rows[0]["hidden_rejected_candidate_count"], "0")
            self.assertEqual(rows[0]["recommended_action"], "choose_search_folder")
            self.assertEqual(rows[0]["review_date_count"], "0")
            self.assertEqual(rows[0]["folder_needed_date_count"], "2")
            self.assertEqual(
                rows[0]["entry_ids"],
                "project365:1998-04-12;project365:1998-04-13",
            )
            self.assertEqual(rows[0]["control_app_start_date"], "1998-04-12")
            self.assertEqual(rows[0]["control_app_end_date"], "1998-04-13")

    def test_photo_library_index_supplies_default_candidate_when_source_folder_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            library_root = base / "external_drive"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 indexed-original.png").write_bytes(_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(rows[0]["candidate_filename"], "1998-04-12 indexed-original.png")
            self.assertIn("photo_library_index", rows[0]["evidence"])
            self.assertIn("filename_date", rows[0]["evidence"])

    def test_photo_library_index_assigns_duplicate_candidate_to_closest_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-14.png": _tiny_png(),
                    "1998-04-17.png": _tiny_png(),
                },
            )
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            library_root = base / "external_drive"
            library_root.mkdir()
            library_root.joinpath("1998-04-17 indexed-original.jpg").write_bytes(_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            rows_by_entry = {row["entry_id"]: row for row in rows}
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(rows_by_entry["project365:1998-04-14"]["candidate_filename"], "")
            self.assertEqual(rows_by_entry["project365:1998-04-14"]["review_decision"], "search_needed")
            self.assertEqual(
                rows_by_entry["project365:1998-04-17"]["candidate_filename"],
                "1998-04-17 indexed-original.jpg",
            )
            self.assertEqual(rows_by_entry["project365:1998-04-17"]["date_distance"], "0")

    def test_photo_library_index_folder_filter_is_recursive_and_excludes_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            library_root = base / "external_drive"
            selected_root = library_root / "Selected"
            nested_root = selected_root / "Nested"
            sibling_prefix_root = library_root / "Selected Similar"
            nested_root.mkdir(parents=True)
            sibling_prefix_root.mkdir(parents=True)
            nested_root.joinpath("1998-04-12 selected-indexed.png").write_bytes(_tiny_png())
            sibling_prefix_root.joinpath("1998-04-12 sibling-indexed.png").write_bytes(_different_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
                photo_index_folder=selected_root,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(rows[0]["candidate_filename"], "1998-04-12 selected-indexed.png")
            self.assertIn("photo_library_index", rows[0]["evidence"])

    def test_search_queue_retains_nearby_jpeg_when_exact_candidates_are_png_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            library_root = base / "library"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 export.png").write_bytes(_tiny_png())
            library_root.joinpath("1998-04-11 improved.jpg").write_bytes(_different_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.candidate_count, 2)
            self.assertEqual(
                {row["candidate_filename"] for row in rows},
                {"1998-04-12 export.png", "1998-04-11 improved.jpg"},
            )

    def test_source_folder_and_photo_library_index_candidates_are_combined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            source_root.joinpath("1998-04-12 source-original.png").write_bytes(_tiny_png())
            library_root = base / "external_drive"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 indexed-original.png").write_bytes(_different_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.candidate_count, 2)
            self.assertEqual(
                {row["candidate_filename"] for row in rows},
                {"1998-04-12 indexed-original.png", "1998-04-12 source-original.png"},
            )
            self.assertTrue(any("photo_library_index" in row["evidence"] for row in rows))

    def test_exact_index_candidate_suppresses_nearby_source_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            source_root.joinpath("1998-04-15 nearby.png").write_bytes(_tiny_png())
            library_root = base / "external_drive"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 exact.jpg").write_bytes(_different_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(rows[0]["candidate_filename"], "1998-04-12 exact.jpg")
            self.assertIn("photo_library_index", rows[0]["evidence"])

    def test_export_equivalent_source_candidate_does_not_block_photo_library_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            report_dir = canonical_root / "exports" / "verification_reports"
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            source_candidate = source_root / "1998-04-12 export-copy.png"
            source_candidate.write_bytes(_tiny_png())
            library_root = base / "external_drive"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 indexed-original.png").write_bytes(_different_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )
            report_dir.mkdir(parents=True, exist_ok=True)
            _write_csv(
                report_dir / "original_photo_external_search_queue.csv",
                [
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(source_candidate),
                        "candidate_filename": source_candidate.name,
                        "candidate_sha256": pipeline._sha256_file(source_candidate),
                        "byte_size": str(source_candidate.stat().st_size),
                        "mime_type": "image/png",
                        "evidence": "filename_date",
                        "candidate_filter_reason": "export_equivalent",
                    }
                ],
                pipeline._search_queue_fieldnames(),
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(
                [row["candidate_filename"] for row in rows],
                ["1998-04-12 export-copy.png", "1998-04-12 indexed-original.png"],
            )
            self.assertEqual(rows[0]["candidate_filter_reason"], "export_equivalent")
            self.assertIn("photo_library_index", rows[1]["evidence"])
            group_rows = _read_csv(Path(summary.group_report_path))
            self.assertEqual(group_rows[0]["candidate_count"], "1")
            self.assertEqual(group_rows[0]["hidden_export_equivalent_candidate_count"], "1")
            self.assertEqual(group_rows[0]["status"], "needs_choice")

    def test_rejected_photo_library_index_candidate_is_not_relisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            library_root = base / "external_drive"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 wrong-indexed.png").write_bytes(_tiny_png())
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )
            first_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            rows = _read_csv(Path(first_summary.search_queue_path))
            rows[0]["review_decision"] = "rejected"
            reviewed_path = base / "reviewed.csv"
            _write_csv(reviewed_path, rows, list(rows[0]))
            pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            second_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports_second",
                scan_metadata_dates=False,
            )

            second_rows = _read_csv(Path(second_summary.search_queue_path))
            self.assertEqual(second_summary.candidate_count, 0)
            self.assertEqual(second_rows[0]["review_decision"], "search_needed")
            self.assertEqual(second_rows[0]["candidate_path"], "")
            group_rows = _read_csv(Path(second_summary.group_report_path))
            self.assertEqual(group_rows[0]["hidden_rejected_candidate_count"], "1")
            self.assertEqual(group_rows[0]["status"], "all_candidates_rejected")

    def test_photo_library_index_ranks_likely_higher_quality_variants_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            source_root.mkdir(parents=True)
            library_root = base / "external_drive"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 web.jpg").write_bytes(_tiny_png())
            library_root.joinpath("1998-04-12 original.jpg").write_bytes(_tiny_png() + b"x" * 300)
            library_root.joinpath("1998-04-12 improved.jpg").write_bytes(_tiny_png() + b"x" * 100)
            photo_index.build_photo_library_index(
                index_db=photo_index.default_index_db(canonical_root),
                index_roots=[library_root],
                reset=True,
            )

            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )

            rows = _read_csv(Path(summary.search_queue_path))
            self.assertEqual(
                [row["candidate_filename"] for row in rows],
                ["1998-04-12 improved.jpg", "1998-04-12 original.jpg", "1998-04-12 web.jpg"],
            )
            self.assertIn("quality_positive_improved", rows[0]["evidence"])
            self.assertIn("quality_negative_web", rows[2]["evidence"])


def _import_sample(base: Path, members: dict[str, bytes]) -> Path:
    import_dir = base / "Import"
    canonical_root = base / "Project365Canonical"
    import_dir.mkdir()
    with zipfile.ZipFile(import_dir / "1998-04.zip", "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    canonical_importer.import_project365_exports(
        import_dir=import_dir,
        canonical_root=canonical_root,
    )
    return canonical_root


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _jpeg_with_exif_date(value: str, tag: int = 0x9003) -> bytes:
    payload = value.encode("ascii") + b"\x00"
    value_offset = 8 + 2 + 12 + 4
    tiff = bytearray()
    tiff.extend(b"II")
    tiff.extend((42).to_bytes(2, "little"))
    tiff.extend((8).to_bytes(4, "little"))
    tiff.extend((1).to_bytes(2, "little"))
    tiff.extend(tag.to_bytes(2, "little"))
    tiff.extend((2).to_bytes(2, "little"))
    tiff.extend(len(payload).to_bytes(4, "little"))
    tiff.extend(value_offset.to_bytes(4, "little"))
    tiff.extend((0).to_bytes(4, "little"))
    tiff.extend(payload)
    app1 = b"Exif\x00\x00" + bytes(tiff)
    return b"\xff\xd8" + b"\xff\xe1" + (len(app1) + 2).to_bytes(2, "big") + app1 + b"\xff\xd9"


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
        b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f"
        b"\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _different_tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00"
        b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _make_gradient_pixels(width: int, height: int) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            pixels.append(((x * 3 + y * 5) % 256, (x * 7 + y * 11) % 256, (x * 13 + y * 17) % 256))
    return pixels


def _crop(
    pixels: list[tuple[int, int, int]],
    source_width: int,
    x: int,
    y: int,
    width: int,
    height: int,
) -> list[tuple[int, int, int]]:
    cropped = []
    for row in range(y, y + height):
        offset = row * source_width
        cropped.extend(pixels[offset + x : offset + x + width])
    return cropped


def _write_bmp(
    path: Path,
    width: int,
    height: int,
    pixels: list[tuple[int, int, int]],
) -> None:
    row_stride = ((width * 3 + 3) // 4) * 4
    pixel_bytes = bytearray()
    for row in range(height - 1, -1, -1):
        row_bytes = bytearray()
        for col in range(width):
            red, green, blue = pixels[row * width + col]
            row_bytes.extend([blue, green, red])
        row_bytes.extend(b"\x00" * (row_stride - width * 3))
        pixel_bytes.extend(row_bytes)
    file_size = 14 + 40 + len(pixel_bytes)
    header = bytearray()
    header.extend(b"BM")
    header.extend(file_size.to_bytes(4, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend((54).to_bytes(4, "little"))
    header.extend((40).to_bytes(4, "little"))
    header.extend(width.to_bytes(4, "little", signed=True))
    header.extend(height.to_bytes(4, "little", signed=True))
    header.extend((1).to_bytes(2, "little"))
    header.extend((24).to_bytes(2, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend(len(pixel_bytes).to_bytes(4, "little"))
    header.extend((2835).to_bytes(4, "little", signed=True))
    header.extend((2835).to_bytes(4, "little", signed=True))
    header.extend((0).to_bytes(4, "little"))
    header.extend((0).to_bytes(4, "little"))
    path.write_bytes(bytes(header) + bytes(pixel_bytes))


@contextmanager
def _sqlite_connection(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


if __name__ == "__main__":
    unittest.main()
