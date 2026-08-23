from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_broad_visual_match as broad
import project365_original_picker as picker


class Project365BroadVisualMatchTests(unittest.TestCase):
    def test_schema_initializer_creates_isolated_broad_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "broad_visual_match.sqlite"
            connection = broad.connect_broad_db(db_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                run_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(broad_descriptor_runs)")
                }
            finally:
                connection.close()

        self.assertIn("broad_descriptors", tables)
        self.assertIn("broad_descriptor_errors", tables)
        self.assertIn("broad_match_runs", tables)
        self.assertIn("broad_match_results", tables)
        self.assertIn("current_candidate_index", run_columns)
        self.assertIn("current_phase", run_columns)
        self.assertIn("heartbeat_at", run_columns)
        self.assertIn("skipped_candidate_count", run_columns)
        self.assertNotIn("media_assets", tables)

    def test_dense_square_index_and_match_rank_off_center_landscape_crop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            landscape_pixels = _make_gradient_pixels(100, 40)
            source_pixels = _crop(landscape_pixels, 100, 55, 0, 40, 40)
            wrong_pixels = _make_solid_pixels(40, 40, (240, 15, 15))
            source_path = base / "project365.bmp"
            match_path = candidate_root / "landscape-original.bmp"
            wrong_path = candidate_root / "wrong.bmp"
            _write_bmp(source_path, 40, 40, source_pixels)
            _write_bmp(match_path, 100, 40, landscape_pixels)
            _write_bmp(wrong_path, 40, 40, wrong_pixels)
            _write_canonical_db(canonical_root / "canonical.db", source_path)

            index_summary = broad.build_descriptor_index(
                canonical_root=canonical_root,
                db_path=db_path,
                candidate_roots=[candidate_root],
                density=11,
            )
            match_summary = broad.run_match_batch(
                canonical_root=canonical_root,
                db_path=db_path,
                target_scope={"entry_ids": ["project365:1998-04-12"]},
                max_results=2,
                density=11,
            )
            page = broad.review_results(db_path, run_id=match_summary.run_id, limit=10)

        self.assertEqual(index_summary.error_count, 0)
        self.assertEqual(match_summary.matched_entries, 1)
        self.assertEqual(page["results"][0]["candidate_path"], str(match_path.resolve()))
        self.assertIn("square_x", page["results"][0]["best_view"])

    def test_match_excludes_project365_target_export_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            landscape_pixels = _make_gradient_pixels(100, 40)
            source_pixels = _crop(landscape_pixels, 100, 55, 0, 40, 40)
            source_path = base / "project365.bmp"
            target_copy_path = candidate_root / "project365-copy.bmp"
            match_path = candidate_root / "landscape-original.bmp"
            _write_bmp(source_path, 40, 40, source_pixels)
            _write_bmp(target_copy_path, 40, 40, source_pixels)
            _write_bmp(match_path, 100, 40, landscape_pixels)
            _write_canonical_db(canonical_root / "canonical.db", source_path)

            broad.build_descriptor_index(canonical_root, db_path, [candidate_root], density=11)
            match_summary = broad.run_match_batch(
                canonical_root=canonical_root,
                db_path=db_path,
                target_scope={"entry_ids": ["project365:1998-04-12"]},
                max_results=5,
                density=11,
            )
            page = broad.review_results(db_path, run_id=match_summary.run_id, limit=10)
            result_paths = [row["candidate_path"] for row in page["results"]]

        self.assertEqual(match_summary.matched_entries, 1)
        self.assertEqual(result_paths, [str(match_path.resolve())])
        self.assertNotIn(str(target_copy_path.resolve()), result_paths)

    def test_match_excludes_previously_rejected_candidates_for_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            landscape_pixels = _make_gradient_pixels(100, 40)
            source_pixels = _crop(landscape_pixels, 100, 55, 0, 40, 40)
            source_path = base / "project365.bmp"
            rejected_path = candidate_root / "rejected-original.bmp"
            fallback_path = candidate_root / "fallback.bmp"
            _write_bmp(source_path, 40, 40, source_pixels)
            _write_bmp(rejected_path, 100, 40, landscape_pixels)
            _write_bmp(fallback_path, 40, 40, _make_solid_pixels(40, 40, (15, 15, 240)))
            _write_canonical_db(canonical_root / "canonical.db", source_path)
            _insert_rejected_original(canonical_root / "canonical.db", rejected_path)

            broad.build_descriptor_index(canonical_root, db_path, [candidate_root], density=11)
            match_summary = broad.run_match_batch(
                canonical_root=canonical_root,
                db_path=db_path,
                target_scope={"entry_ids": ["project365:1998-04-12"]},
                max_results=5,
                density=11,
            )
            page = broad.review_results(db_path, run_id=match_summary.run_id, limit=10)
            result_paths = [row["candidate_path"] for row in page["results"]]

        self.assertEqual(match_summary.matched_entries, 1)
        self.assertEqual(result_paths, [str(fallback_path.resolve())])
        self.assertNotIn(str(rejected_path.resolve()), result_paths)

    def test_review_entries_hides_preexisting_project365_target_export_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            source_path = base / "project365.bmp"
            candidate_path = base / "candidate.bmp"
            _write_bmp(source_path, 40, 40, _make_gradient_pixels(40, 40))
            _write_bmp(candidate_path, 40, 40, _make_solid_pixels(40, 40, (240, 15, 15)))
            _write_canonical_db(canonical_root / "canonical.db", source_path)
            connection = broad.connect_broad_db(db_path)
            try:
                _insert_run(connection, "run-1")
                for rank, path in enumerate((source_path, candidate_path), start=1):
                    connection.execute(
                        """
                        INSERT INTO broad_match_results (
                            run_id, entry_id, entry_date, project365_media_asset_id, candidate_path,
                            candidate_filename, candidate_sha256, byte_size, mime_type, date_distance,
                            score, score_gap, rank, best_view, method_version, evidence, created_at
                        )
                        VALUES ('run-1', 'project365:1998-04-12', '1998-04-12',
                            'project365:1998-04-12:project365_export_png', ?, ?, ?, ?,
                            'image/bmp', 0, ?, 0, ?, 'full', ?, 'test', '2026-08-23T00:00:01+00:00')
                        """,
                        (
                            str(path.resolve()),
                            path.name,
                            _sha256(path),
                            path.stat().st_size,
                            float(rank - 1),
                            rank,
                            broad.METHOD_VERSION,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

            page = broad.review_entries(canonical_root, db_path, run_id="run-1")
            result_paths = [row["candidate_path"] for row in page["entries"][0]["results"]]

        self.assertEqual(result_paths, [str(candidate_path.resolve())])

    def test_review_entries_hides_previously_rejected_stored_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            source_path = base / "project365.bmp"
            rejected_path = base / "rejected.bmp"
            candidate_path = base / "candidate.bmp"
            _write_bmp(source_path, 40, 40, _make_gradient_pixels(40, 40))
            _write_bmp(rejected_path, 40, 40, _make_solid_pixels(40, 40, (240, 15, 15)))
            _write_bmp(candidate_path, 40, 40, _make_solid_pixels(40, 40, (15, 15, 240)))
            _write_canonical_db(canonical_root / "canonical.db", source_path)
            _insert_rejected_original(canonical_root / "canonical.db", rejected_path)
            connection = broad.connect_broad_db(db_path)
            try:
                _insert_run(connection, "run-1")
                for rank, path in enumerate((rejected_path, candidate_path), start=1):
                    connection.execute(
                        """
                        INSERT INTO broad_match_results (
                            run_id, entry_id, entry_date, project365_media_asset_id, candidate_path,
                            candidate_filename, candidate_sha256, byte_size, mime_type, date_distance,
                            score, score_gap, rank, best_view, method_version, evidence, created_at
                        )
                        VALUES ('run-1', 'project365:1998-04-12', '1998-04-12',
                            'project365:1998-04-12:project365_export_png', ?, ?, ?, ?,
                            'image/bmp', 0, ?, 0, ?, 'full', ?, 'test', '2026-08-23T00:00:01+00:00')
                        """,
                        (
                            str(path.resolve()),
                            path.name,
                            _sha256(path),
                            path.stat().st_size,
                            float(rank - 1),
                            rank,
                            broad.METHOD_VERSION,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

            page = broad.review_entries(canonical_root, db_path, run_id="run-1")
            result_paths = [row["candidate_path"] for row in page["entries"][0]["results"]]

        self.assertEqual(result_paths, [str(candidate_path.resolve())])
        self.assertNotIn(str(rejected_path.resolve()), result_paths)

    def test_review_entries_skips_entry_when_all_stored_results_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            source_path = base / "project365.bmp"
            first_path = base / "first.bmp"
            second_path = base / "second.bmp"
            _write_bmp(source_path, 40, 40, _make_gradient_pixels(40, 40))
            _write_bmp(first_path, 40, 40, _make_solid_pixels(40, 40, (240, 15, 15)))
            _write_bmp(second_path, 40, 40, _make_solid_pixels(40, 40, (15, 15, 240)))
            _write_canonical_db(canonical_root / "canonical.db", source_path)
            _insert_rejected_original(canonical_root / "canonical.db", first_path)
            _insert_rejected_original(canonical_root / "canonical.db", second_path)
            connection = broad.connect_broad_db(db_path)
            try:
                _insert_run(connection, "run-1")
                for rank, path in enumerate((first_path, second_path), start=1):
                    connection.execute(
                        """
                        INSERT INTO broad_match_results (
                            run_id, entry_id, entry_date, project365_media_asset_id, candidate_path,
                            candidate_filename, candidate_sha256, byte_size, mime_type, date_distance,
                            score, score_gap, rank, best_view, method_version, evidence, created_at
                        )
                        VALUES ('run-1', 'project365:1998-04-12', '1998-04-12',
                            'project365:1998-04-12:project365_export_png', ?, ?, ?, ?,
                            'image/bmp', 0, ?, 0, ?, 'full', ?, 'test', '2026-08-23T00:00:01+00:00')
                        """,
                        (
                            str(path.resolve()),
                            path.name,
                            _sha256(path),
                            path.stat().st_size,
                            float(rank - 1),
                            rank,
                            broad.METHOD_VERSION,
                        ),
                    )
                connection.commit()
            finally:
                connection.close()

            page = broad.review_entries(canonical_root, db_path, run_id="run-1")

        self.assertEqual(page["returned_count"], 0)
        self.assertEqual(page["entries"], [])

    def test_review_results_are_paginated_precomputed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "broad.sqlite"
            connection = broad.connect_broad_db(db_path)
            try:
                _insert_run(connection, "run-1")
                for index in range(3):
                    _insert_result(connection, "run-1", index)
                connection.commit()
            finally:
                connection.close()

            first = broad.review_results(db_path, run_id="run-1", limit=2, offset=0)
            second = broad.review_results(db_path, run_id="run-1", limit=2, offset=2)

        self.assertEqual(first["returned_count"], 2)
        self.assertTrue(first["has_more"])
        self.assertEqual(second["returned_count"], 1)
        self.assertFalse(second["has_more"])

    def test_review_entries_include_target_confirmed_original_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            source_path = base / "project365.bmp"
            confirmed_path = base / "confirmed.bmp"
            _write_bmp(source_path, 40, 40, _make_gradient_pixels(40, 40))
            _write_bmp(confirmed_path, 40, 40, _make_gradient_pixels(40, 40))
            _write_canonical_db(
                canonical_root / "canonical.db",
                source_path,
                confirmed_path=confirmed_path,
            )
            db_path = canonical_root / "broad.sqlite"
            connection = broad.connect_broad_db(db_path)
            try:
                _insert_run(connection, "run-1")
                _insert_result(connection, "run-1", 0)
                connection.commit()
            finally:
                connection.close()

            page = broad.review_entries(canonical_root, db_path, run_id="run-1")

        self.assertEqual(page["returned_count"], 1)
        self.assertEqual(page["entries"][0]["entry_id"], "project365:1998-04-12")
        self.assertEqual(page["entries"][0]["source_path"], str(source_path))
        self.assertEqual(page["entries"][0]["confirmed_path"], str(confirmed_path))
        self.assertEqual(page["entries"][0]["results"][0]["rank"], 1)

    def test_folder_limited_match_requires_candidate_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = Path(temp_dir) / "Project365Canonical"
            canonical_root.mkdir()
            source_path = Path(temp_dir) / "source.bmp"
            _write_bmp(source_path, 40, 40, _make_gradient_pixels(40, 40))
            _write_canonical_db(canonical_root / "canonical.db", source_path)

            with self.assertRaisesRegex(ValueError, "requires at least one candidate root"):
                broad.run_match_batch(
                    canonical_root=canonical_root,
                    db_path=canonical_root / "broad.sqlite",
                    candidate_scope="folder_limited",
                )

    def test_benchmark_exports_metadata_only_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            pixels = _make_gradient_pixels(40, 40)
            source_path = base / "project365.bmp"
            confirmed_path = candidate_root / "confirmed-original.bmp"
            _write_bmp(source_path, 40, 40, pixels)
            _write_bmp(confirmed_path, 40, 40, pixels)
            _write_canonical_db(
                canonical_root / "canonical.db",
                source_path,
                confirmed_path=confirmed_path,
            )
            broad.build_descriptor_index(canonical_root, db_path, [candidate_root])

            summary = broad.benchmark_confirmed_originals(
                canonical_root=canonical_root,
                db_path=db_path,
                report_dir=canonical_root / "reports",
            )
            report_text = Path(summary["json_path"]).read_text(encoding="utf-8")

            self.assertEqual(summary["confirmed_count"], 1)
            self.assertEqual(summary["hit_count"], 1)
            self.assertTrue(Path(summary["csv_path"]).exists())
            self.assertIn("project365:1998-04-12", report_text)
            self.assertNotIn("private diary text", report_text)
            visual = broad.benchmark_review_entries(
                canonical_root=canonical_root,
                report_dir=canonical_root / "reports",
            )

            self.assertEqual(visual["returned_count"], 1)
            self.assertEqual(visual["entries"][0]["source_path"], str(source_path))
            self.assertEqual(visual["entries"][0]["confirmed_path"], str(confirmed_path))
            self.assertEqual(visual["entries"][0]["expected_rank"], 1)
            self.assertEqual(visual["entries"][0]["results"][0]["candidate_path"], str(confirmed_path.resolve()))

    def test_descriptor_index_reuses_current_fingerprints_unless_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            candidate_path = candidate_root / "candidate.bmp"
            _write_bmp(candidate_path, 40, 40, _make_gradient_pixels(40, 40))

            first = broad.build_descriptor_index(canonical_root, db_path, [candidate_root])
            second = broad.build_descriptor_index(canonical_root, db_path, [candidate_root])
            third = broad.build_descriptor_index(
                canonical_root,
                db_path,
                [candidate_root],
                rebuild_stale=True,
            )

        self.assertEqual(first.indexed_descriptor_count, 1)
        self.assertEqual(first.reused_descriptor_count, 0)
        self.assertEqual(second.indexed_descriptor_count, 0)
        self.assertEqual(second.reused_descriptor_count, 1)
        self.assertEqual(third.indexed_descriptor_count, 1)
        self.assertEqual(third.reused_descriptor_count, 0)

    def test_descriptor_index_retries_error_rows_and_records_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            candidate_path = candidate_root / "candidate.bmp"
            _write_bmp(candidate_path, 40, 40, _make_gradient_pixels(40, 40))
            calls = 0

            def flaky_builder(path: Path, _density: int, *_args: object) -> tuple[dict[str, object], str]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {}, "RuntimeError"
                return {"width": 40, "height": 40, "views": []}, ""

            with mock.patch.object(broad, "_build_descriptor_for_path", side_effect=flaky_builder):
                first = broad.build_descriptor_index(canonical_root, db_path, [candidate_root], workers=1)
                second = broad.build_descriptor_index(canonical_root, db_path, [candidate_root], workers=1)
            status = broad.broad_status(db_path)
            connection = broad.connect_broad_db(db_path)
            try:
                errors = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT run_id, candidate_index, filename, phase, error FROM broad_descriptor_errors"
                    )
                ]
                descriptor_error = connection.execute(
                    "SELECT error FROM broad_descriptors WHERE path = ?",
                    (str(candidate_path.resolve()),),
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(first.indexed_descriptor_count, 0)
        self.assertEqual(first.error_count, 1)
        self.assertEqual(second.indexed_descriptor_count, 1)
        self.assertEqual(second.reused_descriptor_count, 0)
        self.assertEqual(descriptor_error, "")
        self.assertEqual(status["descriptor_count"], 1)
        self.assertEqual(status["descriptor_error_count"], 0)
        self.assertEqual(errors[0]["filename"], candidate_path.name)
        self.assertEqual(errors[0]["phase"], "fingerprinting")
        self.assertEqual(errors[0]["error"], "RuntimeError")

    def test_hash_failures_are_recorded_as_file_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            candidate_path = candidate_root / "candidate.bmp"
            _write_bmp(candidate_path, 40, 40, _make_gradient_pixels(40, 40))

            with mock.patch.object(broad, "_sha256_file", side_effect=PermissionError("denied")):
                summary = broad.build_descriptor_index(canonical_root, db_path, [candidate_root], workers=1)
            status = broad.broad_status(db_path)

        self.assertEqual(summary.indexed_descriptor_count, 0)
        self.assertEqual(summary.error_count, 1)
        self.assertEqual(status["descriptor_count"], 0)
        self.assertEqual(status["descriptor_error_count"], 1)
        self.assertEqual(status["latest_index_errors"][0]["phase"], "hashing")
        self.assertEqual(status["latest_index_errors"][0]["error"], "PermissionError")

    def test_descriptor_index_skips_hash_when_fingerprint_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            candidate_path = candidate_root / "candidate.bmp"
            _write_bmp(candidate_path, 40, 40, _make_gradient_pixels(40, 40))
            broad.build_descriptor_index(canonical_root, db_path, [candidate_root])

            with mock.patch.object(broad, "_sha256_file", side_effect=AssertionError("should not hash reused file")):
                summary = broad.build_descriptor_index(canonical_root, db_path, [candidate_root])

        self.assertEqual(summary.indexed_descriptor_count, 0)
        self.assertEqual(summary.reused_descriptor_count, 1)

    def test_descriptor_index_records_default_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            candidate_path = candidate_root / "candidate.bmp"
            _write_bmp(candidate_path, 40, 40, _make_gradient_pixels(40, 40))

            broad.build_descriptor_index(canonical_root, db_path, [candidate_root])
            connection = broad.connect_broad_db(db_path)
            try:
                latest_run = dict(connection.execute("SELECT * FROM broad_descriptor_runs").fetchone())
            finally:
                connection.close()

        settings = json.loads(latest_run["settings_json"])
        self.assertEqual(settings["workers"], broad.DEFAULT_INDEX_WORKERS)

    def test_descriptor_index_respects_expanded_date_range_before_fingerprinting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            pixels = _make_gradient_pixels(40, 40)
            in_range = candidate_root / "IMG_20210115_in.bmp"
            window_range = candidate_root / "IMG_20201215_window.bmp"
            too_early = candidate_root / "IMG_20201130_out.bmp"
            too_late = candidate_root / "IMG_20210501_out.bmp"
            for path in (in_range, window_range, too_early, too_late):
                _write_bmp(path, 40, 40, pixels)

            summary = broad.build_descriptor_index(
                canonical_root=canonical_root,
                db_path=db_path,
                candidate_roots=[candidate_root],
                start_date="2021-01-01",
                end_date="2021-03-31",
                date_window_days=30,
                commit_interval=1,
            )
            connection = broad.connect_broad_db(db_path)
            try:
                descriptor_paths = [
                    row[0]
                    for row in connection.execute("SELECT path FROM broad_descriptors ORDER BY path")
                ]
                latest_run = dict(
                    connection.execute("SELECT * FROM broad_descriptor_runs").fetchone()
                )
            finally:
                connection.close()

        self.assertEqual(summary.scanned_count, 4)
        self.assertEqual(summary.indexed_descriptor_count, 2)
        self.assertEqual(summary.skipped_candidate_count, 2)
        self.assertEqual(descriptor_paths, sorted([str(in_range.resolve()), str(window_range.resolve())]))
        self.assertEqual(latest_run["total_candidate_count"], 4)
        self.assertEqual(latest_run["scanned_count"], 4)
        self.assertEqual(latest_run["skipped_candidate_count"], 2)

    def test_date_limited_index_reuses_photo_index_sha_without_rehashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            candidate_path = candidate_root / "IMG_20210115_in.bmp"
            _write_bmp(candidate_path, 40, 40, _make_gradient_pixels(40, 40))
            expected_sha = _sha256(candidate_path)
            _write_photo_index(
                canonical_root / "photo_library_index.sqlite",
                [(candidate_path, candidate_root, "2021-01-15")],
            )

            with mock.patch.object(broad, "_sha256_file", side_effect=AssertionError("should use photo index hash")):
                summary = broad.build_descriptor_index(
                    canonical_root=canonical_root,
                    db_path=db_path,
                    candidate_roots=[candidate_root],
                    start_date="2021-01-01",
                    end_date="2021-01-31",
                    workers=1,
                )
            connection = broad.connect_broad_db(db_path)
            try:
                stored_sha = connection.execute("SELECT sha256 FROM broad_descriptors").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(summary.indexed_descriptor_count, 1)
        self.assertEqual(stored_sha, expected_sha)

    def test_date_limited_descriptor_index_uses_photo_index_date_order_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            pixels = _make_gradient_pixels(40, 40)
            late_path = candidate_root / "a-late.bmp"
            early_path = candidate_root / "z-early.bmp"
            _write_bmp(late_path, 40, 40, pixels)
            _write_bmp(early_path, 40, 40, pixels)
            _write_photo_index(
                canonical_root / "photo_library_index.sqlite",
                [
                    (late_path, candidate_root, "2021-01-03"),
                    (early_path, candidate_root, "2021-01-01"),
                ],
            )
            build_order: list[Path] = []

            def capture_order(path: Path, _density: int, *_args: object) -> tuple[dict[str, object], str]:
                build_order.append(path)
                return {"width": 40, "height": 40, "views": {}}, ""

            with mock.patch.object(broad, "_build_descriptor_for_path", side_effect=capture_order):
                summary = broad.build_descriptor_index(
                    canonical_root=canonical_root,
                    db_path=db_path,
                    candidate_roots=[candidate_root],
                    start_date="2021-01-01",
                    end_date="2021-01-31",
                    commit_interval=1,
                    workers=1,
                )
            connection = broad.connect_broad_db(db_path)
            try:
                latest_run = dict(connection.execute("SELECT * FROM broad_descriptor_runs").fetchone())
                coverage = json.loads(latest_run["date_coverage_json"])
            finally:
                connection.close()

        self.assertEqual(summary.scanned_count, 2)
        self.assertEqual(build_order, [early_path.resolve(), late_path.resolve()])
        self.assertEqual(latest_run["total_candidate_count"], 2)
        self.assertEqual(coverage["total_date_count"], 2)
        self.assertEqual(coverage["complete_date_count"], 2)
        self.assertEqual(coverage["complete_start_date"], "2021-01-01")
        self.assertEqual(coverage["complete_end_date"], "2021-01-03")
        self.assertEqual(
            [(row["date"], row["checked"], row["total"]) for row in coverage["dates"]],
            [("2021-01-01", 1, 1), ("2021-01-03", 1, 1)],
        )

    def test_descriptor_index_commits_completed_batches_when_stop_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            pixels = _make_gradient_pixels(40, 40)
            first_path = candidate_root / "IMG_20210101_first.bmp"
            second_path = candidate_root / "IMG_20210102_second.bmp"
            _write_bmp(first_path, 40, 40, pixels)
            _write_bmp(second_path, 40, 40, pixels)
            original_builder = broad._build_descriptor_for_path

            def stop_after_first(path: Path, density: int, *args: object) -> tuple[dict[str, object], str]:
                descriptor, error = original_builder(path, density, *args)
                broad._STOP_REQUESTED = True
                return descriptor, error

            try:
                broad._STOP_REQUESTED = False
                with mock.patch.object(broad, "_build_descriptor_for_path", side_effect=stop_after_first):
                    summary = broad.build_descriptor_index(
                        canonical_root=canonical_root,
                        db_path=db_path,
                        candidate_roots=[candidate_root],
                        commit_interval=1,
                        workers=1,
                    )
                connection = broad.connect_broad_db(db_path)
                try:
                    descriptor_count = connection.execute("SELECT COUNT(*) FROM broad_descriptors").fetchone()[0]
                    latest_run = dict(
                        connection.execute("SELECT * FROM broad_descriptor_runs").fetchone()
                    )
                finally:
                    connection.close()
            finally:
                broad._STOP_REQUESTED = False

        self.assertEqual(summary.indexed_descriptor_count, 1)
        self.assertEqual(descriptor_count, 1)
        self.assertEqual(latest_run["status"], "cancelled")
        self.assertEqual(latest_run["total_candidate_count"], 2)
        self.assertEqual(latest_run["indexed_descriptor_count"], 1)

    def test_descriptor_index_records_current_candidate_before_fingerprinting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            candidate_path = candidate_root / "IMG_20210101_first.bmp"
            _write_bmp(candidate_path, 40, 40, _make_gradient_pixels(40, 40))
            captured: dict[str, object] = {}

            def capture_current(_path: Path, _density: int, *_args: object) -> tuple[dict[str, object], str]:
                connection = broad.connect_broad_db(db_path)
                try:
                    captured.update(dict(connection.execute("SELECT * FROM broad_descriptor_runs").fetchone()))
                finally:
                    connection.close()
                return {"width": 40, "height": 40, "views": []}, ""

            with mock.patch.object(broad, "_build_descriptor_for_path", side_effect=capture_current):
                broad.build_descriptor_index(
                    canonical_root=canonical_root,
                    db_path=db_path,
                    candidate_roots=[candidate_root],
                    commit_interval=100,
                    workers=1,
                )

        self.assertEqual(captured["current_candidate_index"], 1)
        self.assertEqual(captured["current_candidate_name"], candidate_path.name)
        self.assertEqual(captured["current_candidate_extension"], ".bmp")
        self.assertEqual(captured["current_phase"], "thumbnailing batch (1 files, 1 workers)")
        self.assertTrue(captured["heartbeat_at"])

    def test_confirmed_only_index_and_benchmark_can_be_constrained_to_year(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            pixels_2003 = _make_gradient_pixels(40, 40)
            pixels_2004 = _make_solid_pixels(40, 40, (20, 160, 200))
            source_2003 = base / "project365-2003.bmp"
            confirmed_2003 = base / "confirmed-2003.bmp"
            source_2004 = base / "project365-2004.bmp"
            confirmed_2004 = base / "confirmed-2004.bmp"
            _write_bmp(source_2003, 40, 40, pixels_2003)
            _write_bmp(confirmed_2003, 40, 40, pixels_2003)
            _write_bmp(source_2004, 40, 40, pixels_2004)
            _write_bmp(confirmed_2004, 40, 40, pixels_2004)
            _write_canonical_db(
                canonical_root / "canonical.db",
                source_2003,
                confirmed_path=confirmed_2003,
                entry_id="project365:2003-01-02",
                entry_date="2003-01-02",
            )
            _insert_confirmed_entry(
                canonical_root / "canonical.db",
                entry_id="project365:2004-02-03",
                entry_date="2004-02-03",
                source_path=source_2004,
                confirmed_path=confirmed_2004,
            )

            index_summary = broad.build_confirmed_descriptor_index(
                canonical_root=canonical_root,
                db_path=db_path,
                target_scope={"start_date": "2003-01-01", "end_date": "2003-12-31"},
            )
            benchmark = broad.benchmark_confirmed_originals(
                canonical_root=canonical_root,
                db_path=db_path,
                target_scope={"start_date": "2003-01-01", "end_date": "2003-12-31"},
                report_dir=canonical_root / "reports",
            )
            connection = broad.connect_broad_db(db_path)
            try:
                descriptor_paths = [
                    row[0]
                    for row in connection.execute("SELECT path FROM broad_descriptors ORDER BY path")
                ]
            finally:
                connection.close()

        self.assertEqual(index_summary.scanned_count, 1)
        self.assertEqual(descriptor_paths, [str(confirmed_2003.resolve())])
        self.assertEqual(benchmark["confirmed_count"], 1)
        self.assertEqual(benchmark["top_1_count"], 1)
        self.assertEqual(benchmark["recall_at_1"], 1.0)

    def test_confirm_broad_match_records_canonical_original_and_hides_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            pixels = _make_gradient_pixels(100, 40)
            source_pixels = _crop(pixels, 100, 55, 0, 40, 40)
            source_path = base / "project365.bmp"
            candidate_path = candidate_root / "candidate.bmp"
            _write_bmp(source_path, 40, 40, source_pixels)
            _write_bmp(candidate_path, 100, 40, pixels)
            _write_canonical_db(canonical_root / "canonical.db", source_path)
            broad.build_descriptor_index(canonical_root, db_path, [candidate_root], density=11)
            match_summary = broad.run_match_batch(
                canonical_root,
                db_path,
                {"entry_ids": ["project365:1998-04-12"]},
                density=11,
            )
            result = broad.review_results(db_path, match_summary.run_id)["results"][0]

            confirm_summary = broad.confirm_broad_match(
                canonical_root=canonical_root,
                db_path=db_path,
                result_id=int(result["result_id"]),
            )
            remaining = broad.review_entries(canonical_root, db_path, run_id=match_summary.run_id)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                row = connection.execute(
                    """
                    SELECT role, storage_path, review_status
                    FROM media_assets
                    WHERE role = 'external_original_reference'
                        AND review_status = 'confirmed'
                    """
                ).fetchone()

        self.assertEqual(confirm_summary["decision"], "matched")
        self.assertEqual(row, ("external_original_reference", str(candidate_path.resolve()), "confirmed"))
        self.assertEqual(remaining["returned_count"], 0)

    def test_reject_broad_entry_records_rejections_and_hides_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            candidate_root = base / "candidates"
            candidate_root.mkdir()
            db_path = canonical_root / "broad_visual_match.sqlite"
            source_path = base / "project365.bmp"
            first_path = candidate_root / "first.bmp"
            second_path = candidate_root / "second.bmp"
            _write_bmp(source_path, 40, 40, _make_gradient_pixels(40, 40))
            _write_bmp(first_path, 40, 40, _make_solid_pixels(40, 40, (240, 15, 15)))
            _write_bmp(second_path, 40, 40, _make_solid_pixels(40, 40, (15, 15, 240)))
            _write_canonical_db(canonical_root / "canonical.db", source_path)
            broad.build_descriptor_index(canonical_root, db_path, [candidate_root])
            match_summary = broad.run_match_batch(
                canonical_root,
                db_path,
                {"entry_ids": ["project365:1998-04-12"]},
                max_results=2,
            )

            reject_summary = broad.reject_broad_entry(
                canonical_root=canonical_root,
                db_path=db_path,
                run_id=match_summary.run_id,
                entry_id="project365:1998-04-12",
            )
            remaining = broad.review_entries(canonical_root, db_path, run_id=match_summary.run_id)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                rejected_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media_assets
                    WHERE role = 'external_original_rejected'
                        AND review_status = 'rejected'
                    """
                ).fetchone()[0]

        self.assertEqual(reject_summary["decision"], "rejected_all")
        self.assertEqual(reject_summary["rejected_count"], 2)
        self.assertEqual(rejected_count, 2)
        self.assertEqual(remaining["returned_count"], 0)

    def test_normal_picker_entry_detail_does_not_initialize_broad_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            source_path = base / "project365.bmp"
            candidate_path = base / "candidate.bmp"
            pixels = _make_gradient_pixels(40, 40)
            _write_bmp(source_path, 40, 40, pixels)
            _write_bmp(candidate_path, 40, 40, pixels)
            _write_canonical_db(canonical_root / "canonical.db", source_path)
            queue_path = base / "queue.csv"
            _write_picker_queue(queue_path, candidate_path)
            state = picker.PickerState(picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path))

            detail = state.entry_detail("project365:1998-04-12", rank_if_needed=False)

        self.assertIsNotNone(detail)
        self.assertFalse((canonical_root / "broad_visual_match.sqlite").exists())


def _write_canonical_db(
    db_path: Path,
    source_path: Path,
    confirmed_path: Path | None = None,
    entry_id: str = "project365:1998-04-12",
    entry_date: str = "1998-04-12",
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE entries (
                id TEXT PRIMARY KEY,
                entry_date TEXT NOT NULL,
                source_app TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE media_assets (
                id TEXT PRIMARY KEY,
                entry_id TEXT NOT NULL,
                role TEXT NOT NULL,
                internal_filename TEXT,
                storage_path TEXT,
                sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                review_status TEXT NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO entries VALUES (?, ?, 'project365')", (entry_id, entry_date))
        connection.execute(
            """
            INSERT INTO media_assets
                (id, entry_id, role, internal_filename, storage_path, sha256, byte_size, mime_type, review_status)
            VALUES (?, ?, 'project365_export_png', ?, ?, ?, ?, 'image/bmp', 'unreviewed')
            """,
            (
                f"{entry_id}:project365_export_png",
                entry_id,
                source_path.name,
                str(source_path),
                _sha256(source_path),
                source_path.stat().st_size,
            ),
        )
        if confirmed_path is not None:
            connection.execute(
                """
                INSERT INTO media_assets
                    (id, entry_id, role, internal_filename, storage_path, sha256, byte_size, mime_type, review_status)
                VALUES (?, ?, 'external_original_reference', ?, ?, ?, ?, 'image/bmp', 'confirmed')
                """,
                (
                    f"{entry_id}:external_original_reference",
                    entry_id,
                    confirmed_path.name,
                    str(confirmed_path),
                    _sha256(confirmed_path),
                    confirmed_path.stat().st_size,
                ),
            )
        connection.commit()


def _insert_confirmed_entry(
    db_path: Path,
    entry_id: str,
    entry_date: str,
    source_path: Path,
    confirmed_path: Path,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO entries VALUES (?, ?, 'project365')", (entry_id, entry_date))
        connection.execute(
            """
            INSERT INTO media_assets
                (id, entry_id, role, internal_filename, storage_path, sha256, byte_size, mime_type, review_status)
            VALUES (?, ?, 'project365_export_png', ?, ?, ?, ?, 'image/bmp', 'unreviewed')
            """,
            (
                f"{entry_id}:project365_export_png",
                entry_id,
                source_path.name,
                str(source_path),
                _sha256(source_path),
                source_path.stat().st_size,
            ),
        )
        connection.execute(
            """
            INSERT INTO media_assets
                (id, entry_id, role, internal_filename, storage_path, sha256, byte_size, mime_type, review_status)
            VALUES (?, ?, 'external_original_reference', ?, ?, ?, ?, 'image/bmp', 'confirmed')
            """,
            (
                f"{entry_id}:external_original_reference",
                entry_id,
                confirmed_path.name,
                str(confirmed_path),
                _sha256(confirmed_path),
                confirmed_path.stat().st_size,
            ),
        )
        connection.commit()


def _insert_rejected_original(
    db_path: Path,
    candidate_path: Path,
    entry_id: str = "project365:1998-04-12",
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO media_assets
                (id, entry_id, role, internal_filename, storage_path, sha256, byte_size, mime_type, review_status)
            VALUES (?, ?, 'external_original_rejected', ?, ?, ?, ?, 'image/bmp', 'rejected')
            """,
            (
                f"{entry_id}:external_original_rejected:{candidate_path.name}",
                entry_id,
                candidate_path.name,
                str(candidate_path.resolve()),
                _sha256(candidate_path),
                candidate_path.stat().st_size,
            ),
        )
        connection.commit()


def _insert_run(connection: sqlite3.Connection, run_id: str) -> None:
    connection.execute(
        """
        INSERT INTO broad_match_runs (
            run_id, started_at, finished_at, status, phase, target_scope_json,
            candidate_scope_json, settings_json
        )
        VALUES (?, '2026-08-23T00:00:00+00:00', '2026-08-23T00:00:01+00:00',
            'pass', 'finished', '{}', '{}', '{}')
        """,
        (run_id,),
    )


def _insert_result(connection: sqlite3.Connection, run_id: str, index: int) -> None:
    connection.execute(
        """
        INSERT INTO broad_match_results (
            run_id, entry_id, entry_date, project365_media_asset_id, candidate_path,
            candidate_filename, candidate_sha256, byte_size, mime_type, date_distance,
            score, score_gap, rank, best_view, method_version, evidence, created_at
        )
        VALUES (?, 'project365:1998-04-12', '1998-04-12',
            'project365:1998-04-12:project365_export_png', ?, ?, 'sha', 1,
            'image/bmp', 0, ?, 0, ?, 'square_x_01', ?, 'test', '2026-08-23T00:00:01+00:00')
        """,
        (run_id, f"/tmp/candidate-{index}.bmp", f"candidate-{index}.bmp", float(index), index + 1, broad.METHOD_VERSION),
    )


def _write_picker_queue(path: Path, candidate_path: Path) -> None:
    fieldnames = broad._queue_fieldnames([])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        row = {field: "" for field in fieldnames}
        row.update(
            {
                "entry_id": "project365:1998-04-12",
                "entry_date": "1998-04-12",
                "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                "candidate_path": str(candidate_path),
                "candidate_filename": candidate_path.name,
                "candidate_sha256": _sha256(candidate_path),
                "byte_size": str(candidate_path.stat().st_size),
                "mime_type": "image/bmp",
                "evidence": "test",
            }
        )
        writer.writerow(row)


def _write_photo_index(index_path: Path, rows: list[tuple[Path, Path, str]]) -> None:
    with sqlite3.connect(index_path) as connection:
        connection.execute(
            """
            CREATE TABLE photo_library_files (
                path TEXT PRIMARY KEY,
                root TEXT NOT NULL,
                sha256 TEXT NOT NULL DEFAULT ''
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
        for path, root, date_value in rows:
            connection.execute(
                "INSERT INTO photo_library_files (path, root, sha256) VALUES (?, ?, ?)",
                (str(path.resolve()), str(root.resolve()), _sha256(path)),
            )
            connection.execute(
                "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, ?)",
                (str(path.resolve()), date_value, "test"),
            )
        connection.commit()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha256(path: Path) -> str:
    return __import__("hashlib").sha256(path.read_bytes()).hexdigest()


def _make_gradient_pixels(width: int, height: int) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            red = (x * 5 + y * 3) % 256
            green = (x * 11 + y * 7) % 256
            blue = (x * 17 + y * 13) % 256
            pixels.append((red, green, blue))
    return pixels


def _make_solid_pixels(width: int, height: int, color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    return [color] * (width * height)


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


if __name__ == "__main__":
    unittest.main()
