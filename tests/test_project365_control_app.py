from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import project365_control_app as control


class Project365ControlAppTests(unittest.TestCase):
    def test_import_step_uses_project365_pro_zip_source_folder(self) -> None:
        commands = control._commands_for_step("import_zips", {})

        self.assertEqual(len(commands), 1)
        command_text = " ".join(commands[0])
        self.assertIn("project365_canonical_importer.py", command_text)
        self.assertIn("Source Data/Project365 Pro Export Zips", command_text)

    def test_initial_sqlite_count_reads_aggregate_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "counts.sqlite"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("CREATE TABLE entries (entry_date TEXT)")
                connection.executemany(
                    "INSERT INTO entries (entry_date) VALUES (?)",
                    [("2020-01-01",), ("2020-01-01",), ("2020-01-02",)],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(control._initial_entry_count(db_path), 3)
            self.assertEqual(control._initial_unique_day_count(db_path), 2)

    def test_search_step_includes_default_original_folder_and_extra_roots(self) -> None:
        commands = control._commands_for_step(
            "search_originals",
            {"search_roots": ["/Volumes/Archive Photos"]},
        )

        command = commands[0]
        self.assertIn("Source Data/Original Photos matching Project365 Entries", command)
        self.assertIn("/Volumes/Archive Photos", command)

    def test_search_step_passes_target_filters(self) -> None:
        commands = control._commands_for_step(
            "search_originals",
            {
                "entry_ids": ["project365:1998-04-12"],
                "entry_dates": ["1998-04-13"],
                "crawl_start_date": "1998-04-01",
                "crawl_end_date": "1998-04-30",
                "max_targets": "25",
            },
        )

        command = commands[0]
        self.assertIn("--entry-id", command)
        self.assertIn("project365:1998-04-12", command)
        self.assertIn("--entry-date", command)
        self.assertIn("1998-04-13", command)
        self.assertIn("--start-date", command)
        self.assertIn("--end-date", command)
        self.assertIn("--max-targets", command)
        self.assertIn("25", command)
        self.assertIn("--merge-existing-queue", command)

    def test_unscoped_search_step_replaces_queue(self) -> None:
        command = control._commands_for_step("search_originals", {})[0]

        self.assertNotIn("--merge-existing-queue", command)

    def test_easy_match_step_uses_source_data_and_photo_index_fallback_without_target_filters(self) -> None:
        commands = control._commands_for_step("match_easy_originals", {})

        command = commands[0]
        self.assertIn("project365_original_reference_pipeline.py", command)
        self.assertIn("Source Data/Original Photos matching Project365 Entries", command)
        self.assertNotIn("--entry-id", command)
        self.assertNotIn("--entry-date", command)
        self.assertNotIn("--disable-photo-index-fallback", command)
        self.assertNotIn("--merge-existing-queue", command)
        self.assertIn("--replace-existing-queue", command)

    def test_easy_match_step_can_filter_photo_index_to_one_folder(self) -> None:
        commands = control._commands_for_step(
            "match_easy_originals",
            {
                "limit_to_photo_index_folder": True,
                "photo_index_folder": "/Volumes/Archive Photos/Project365",
            },
        )

        command = commands[0]
        self.assertIn("--photo-index-folder", command)
        self.assertIn("/Volumes/Archive Photos/Project365", command)
        self.assertNotIn("--merge-existing-queue", command)
        self.assertIn("--replace-existing-queue", command)

    def test_easy_match_filtered_folder_requires_folder(self) -> None:
        with self.assertRaisesRegex(ValueError, "Choose a photo-index folder"):
            control._commands_for_step(
                "match_easy_originals",
                {"limit_to_photo_index_folder": True},
            )

    def test_easy_match_step_ignores_folder_when_filter_is_off(self) -> None:
        command = control._commands_for_step(
            "match_easy_originals",
            {
                "limit_to_photo_index_folder": False,
                "photo_index_folder": "/Volumes/Archive Photos/Project365",
            },
        )[0]

        self.assertNotIn("--photo-index-folder", command)
        self.assertIn("--replace-existing-queue", command)

    def test_easy_match_step_can_include_low_quality_matches(self) -> None:
        command = control._commands_for_step(
            "match_easy_originals",
            {"include_low_quality_matches": True},
        )[0]

        self.assertIn("--include-low-quality-matches", command)
        self.assertIn("--replace-existing-queue", command)

    def test_replace_queue_run_clears_outputs_and_picker_state_before_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            queue = base / "queue.csv"
            groups = base / "groups.csv"
            batch_plan = base / "batch.csv"
            for path in (queue, groups, batch_plan):
                path.write_text("old\n", encoding="utf-8")
            state = control.ControlState()
            state._picker_state = object()

            def fake_run_command(*args: object, **kwargs: object) -> dict[str, object]:
                self.assertFalse(queue.exists())
                self.assertFalse(groups.exists())
                self.assertFalse(batch_plan.exists())
                self.assertIsNone(state._picker_state)
                return {"command": list(args[0]), "returncode": 0, "stdout": "", "stderr": ""}

            with (
                mock.patch.object(control, "ORIGINAL_QUEUE", queue),
                mock.patch.object(control, "ORIGINAL_UNCLEAR_GROUPS", groups),
                mock.patch.object(control, "ORIGINAL_BATCH_PLAN", batch_plan),
                mock.patch.object(control, "_workflow_snapshot", return_value={}),
                mock.patch.object(control, "_workflow_run_summary", return_value={}),
                mock.patch.object(control, "_run_command", side_effect=fake_run_command),
            ):
                record = state._execute_commands(
                    "match_easy_originals",
                    [["python", "project365_original_reference_pipeline.py"]],
                    "2026-08-21T00:00:00+00:00",
                    payload={"replace_existing_queue": True},
                )

        self.assertEqual(record["status"], "pass")

    def test_working_copy_generation_does_not_commit_staged_crops_before_command(self) -> None:
        state = control.ControlState()
        picker_state = mock.Mock()
        state._picker_state = picker_state

        def fake_run_command(*args: object, **kwargs: object) -> dict[str, object]:
            return {"command": " ".join(args[0]), "returncode": 0, "output": "Generated: 3\n"}

        with (
            mock.patch.object(control, "_workflow_snapshot", return_value={}),
            mock.patch.object(control, "_workflow_run_summary", return_value={}),
            mock.patch.object(control, "_run_command", side_effect=fake_run_command) as run_command,
        ):
            record = state._execute_commands(
                "generate_derivatives",
                [["python", "project365_media_derivatives.py"]],
                "2026-08-21T00:00:00+00:00",
                payload={"start_date": "1998-04-12", "end_date": "1998-04-13"},
            )

        self.assertEqual(record["status"], "pass")
        picker_state.commit_staged_crops.assert_not_called()
        run_command.assert_called_once()
        self.assertIn("Generated: 3", record["outputs"][0]["output"])

    def test_run_command_does_not_inherit_stdin(self) -> None:
        stdout = mock.Mock()
        stdout.readline.return_value = ""
        stdout.read.return_value = ""
        process = mock.Mock()
        process.stdout = stdout
        process.poll.return_value = 0
        process.wait.return_value = 0

        with (
            mock.patch.object(control.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(control.select, "select", return_value=([stdout], [], [])),
        ):
            result = control._run_command(["python3", "project365_media_derivatives.py"], 5)

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(popen.call_args.kwargs["stdin"], control.subprocess.DEVNULL)

    def test_build_photo_index_step_requires_and_passes_selected_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_index = Path(temp_dir) / "missing.sqlite"
            with mock.patch.object(control, "PHOTO_LIBRARY_INDEX", missing_index):
                default_command = control._commands_for_step("build_photo_index", {})[0]
        self.assertIn("--index-root", default_command)
        self.assertIn("Source Data", default_command)

        commands = control._commands_for_step(
            "build_photo_index",
            {
                "search_roots": ["/Volumes/Archive Photos", "/Volumes/Trip Photos"],
                "reset_photo_index": True,
                "reset_confirmation": "replace-photo-index",
            },
        )

        command = commands[0]
        self.assertIn("project365_photo_library_index.py", command)
        self.assertIn("--canonical-root", command)
        self.assertIn("Project365Canonical", command)
        self.assertIn("--reset", command)
        self.assertEqual(command.count("--index-root"), 2)
        self.assertIn("/Volumes/Archive Photos", command)
        self.assertIn("/Volumes/Trip Photos", command)

    def test_build_photo_index_blank_roots_uses_existing_index_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    ("/Volumes/Photos/a.jpg", "/Volumes/Photos"),
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    ("/Volumes/Trips/b.jpg", "/Volumes/Trips"),
                )
                connection.commit()

            with mock.patch.object(control, "PHOTO_LIBRARY_INDEX", index_path):
                command = control._commands_for_step("build_photo_index", {})[0]

        self.assertEqual(command.count("--index-root"), 2)
        self.assertIn("/Volumes/Photos", command)
        self.assertIn("/Volumes/Trips", command)
        self.assertNotIn("Source Data", command)

    def test_build_photo_index_blank_reset_uses_existing_index_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    ("/Volumes/Photos/a.jpg", "/Volumes/Photos"),
                )
                connection.commit()

            with mock.patch.object(control, "PHOTO_LIBRARY_INDEX", index_path):
                command = control._commands_for_step(
                    "build_photo_index",
                    {
                        "reset_photo_index": True,
                        "reset_confirmation": "replace-photo-index",
                    },
                )[0]

        self.assertIn("--reset", command)
        self.assertIn("/Volumes/Photos", command)
        self.assertNotIn("Source Data", command)

    def test_build_photo_index_reset_requires_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires confirmation"):
            control._commands_for_step(
                "build_photo_index",
                {"search_roots": ["/Volumes/Test Photos"], "reset_photo_index": True},
            )

    def test_build_photo_index_step_dedupes_selected_roots_without_reset_by_default(self) -> None:
        commands = control._commands_for_step(
            "build_photo_index",
            {"search_roots": ["/Volumes/Test Photos", "/Volumes/Test Photos"]},
        )

        command = commands[0]
        self.assertEqual(command.count("/Volumes/Test Photos"), 1)
        self.assertNotIn("--reset", command)

    def test_build_photo_index_uses_parent_folder_for_pasted_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "iCloud Shared 2025-09 download"
            folder.mkdir()
            photo = folder / "2026-08-22 Placeholder iCloud Shared 2025-09 download.jpg"
            photo.write_bytes(b"placeholder")

            command = control._commands_for_step(
                "build_photo_index",
                {"search_roots": [f"'{photo}'"]},
            )[0]

        self.assertIn(str(folder), command)
        self.assertNotIn(str(photo), command)
        self.assertNotIn(f"'{photo}'", command)

    def test_refresh_photo_index_metadata_uses_existing_index_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    ("/Volumes/Photos/a.jpg", "/Volumes/Photos"),
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    ("/Volumes/Photos/b.jpg", "/Volumes/Photos"),
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    ("/Volumes/Trips/c.jpg", "/Volumes/Trips"),
                )

            with mock.patch.object(control, "PHOTO_LIBRARY_INDEX", index_path):
                command = control._commands_for_step("refresh_photo_index_metadata", {})[0]

        self.assertIn("project365_photo_library_index.py", command)
        self.assertEqual(command.count("--index-root"), 2)
        self.assertIn("/Volumes/Photos", command)
        self.assertIn("/Volumes/Trips", command)
        self.assertNotIn("--reset", command)

    def test_refresh_photo_index_metadata_uses_selected_roots_when_provided(self) -> None:
        commands = control._commands_for_step(
            "refresh_photo_index_metadata",
            {"search_roots": ["/Volumes/Selected Photos", "/Volumes/Selected Photos"]},
        )

        command = commands[0]
        self.assertEqual(command.count("--index-root"), 1)
        self.assertIn("/Volumes/Selected Photos", command)
        self.assertNotIn("--reset", command)

    def test_refresh_photo_index_metadata_can_reconcile_moves_only(self) -> None:
        commands = control._commands_for_step(
            "refresh_photo_index_metadata",
            {
                "search_roots": ["/Volumes/Selected Photos"],
                "reconcile_moves_only": True,
            },
        )

        command = commands[0]
        self.assertIn("--reconcile-moves-only", command)
        self.assertIn("/Volumes/Selected Photos", command)
        self.assertNotIn("--reset", command)

    def test_refresh_photo_index_metadata_uses_parent_folder_for_pasted_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "iCloud Shared 2025-09 download"
            folder.mkdir()
            photo = folder / "2026-08-22 Placeholder iCloud Shared 2025-09 download.jpg"
            photo.write_bytes(b"placeholder")

            command = control._commands_for_step(
                "refresh_photo_index_metadata",
                {"search_roots": [f"'{photo}'"]},
            )[0]

        self.assertEqual(command.count("--index-root"), 1)
        self.assertIn(str(folder), command)
        self.assertNotIn(str(photo), command)

    def test_photo_library_index_roots_omits_child_roots_covered_by_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            parent = base / "Photos - Travels Places Events & Homes"
            child = parent / "India - 2003 The Year on A Bike"
            child.mkdir(parents=True)
            index_path = base / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    (str(parent / "a.jpg"), str(parent)),
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    (str(child / "b.jpg"), str(child)),
                )
                connection.commit()

            roots = control._photo_library_index_roots(index_path)

        self.assertEqual(roots, [str(parent)])

    def test_refresh_photo_index_metadata_requires_existing_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(control, "PHOTO_LIBRARY_INDEX", Path(temp_dir) / "missing.sqlite"):
                with self.assertRaisesRegex(ValueError, "No existing photo index roots"):
                    control._commands_for_step("refresh_photo_index_metadata", {})

    def test_import_run_summary_classifies_archives_and_deltas_without_private_content(self) -> None:
        before = {
            "database": {
                "entries": 1,
                "unique_days": 1,
                "entry_sources": 2,
                "media_assets": 1,
                "source_file_rows": [
                    {
                        "source_path": "/exports/1998-04.zip",
                        "zip_filename": "1998-04.zip",
                        "month": "1998-04",
                        "zip_sha256": "old",
                        "zip_bytes": 10,
                        "validation_status": "pass",
                    },
                ],
            }
        }
        after = {
            "database": {
                "entries": 3,
                "unique_days": 3,
                "entry_sources": 5,
                "media_assets": 3,
                "source_file_rows": [
                    {
                        "source_path": "/exports/1998-04.zip",
                        "zip_filename": "1998-04.zip",
                        "month": "1998-04",
                        "zip_sha256": "old",
                        "zip_bytes": 10,
                        "validation_status": "pass",
                    },
                    {
                        "source_path": "/exports/1998-05.zip",
                        "zip_filename": "1998-05.zip",
                        "month": "1998-05",
                        "zip_sha256": "new",
                        "zip_bytes": 20,
                        "validation_status": "pass",
                    },
                    {
                        "source_path": "/exports/1998-06.zip",
                        "zip_filename": "1998-06.zip",
                        "month": "1998-06",
                        "zip_sha256": "same",
                        "zip_bytes": 30,
                        "validation_status": "invalid",
                    },
                ],
            }
        }

        summary = control._workflow_run_summary(
            step="import_zips",
            status="pass",
            payload={},
            before=before,
            after=after,
            outputs=[],
            error="",
        )

        self.assertEqual(summary["deltas"]["diary_entries"], 2)
        self.assertEqual(summary["deltas"]["unique_diary_days"], 2)
        self.assertEqual(summary["deltas"]["source_links"], 3)
        self.assertEqual(summary["deltas"]["media_records"], 2)
        self.assertEqual(summary["archive_result_count"], 3)
        self.assertEqual(summary["archive_changed_count"], 2)
        self.assertEqual(summary["archive_anomaly_count"], 1)
        self.assertEqual([item["filename"] for item in summary["archive_results"]], ["1998-06.zip"])
        self.assertNotIn("private diary text", json.dumps(summary))

    def test_match_run_summary_does_not_include_zip_archive_results(self) -> None:
        database_snapshot = {
            "entries": 3,
            "unique_days": 3,
            "entry_sources": 5,
            "media_assets": 3,
            "source_file_rows": [
                {
                    "source_path": "/exports/1998-04.zip",
                    "zip_filename": "1998-04.zip",
                    "month": "1998-04",
                    "zip_sha256": "same",
                    "zip_bytes": 10,
                    "validation_status": "pass",
                }
            ],
        }
        summary = control._workflow_run_summary(
            step="match_easy_originals",
            status="pass",
            payload={"photo_index_folder": "/Volumes/Photos/Project365"},
            before={"database": database_snapshot},
            after={
                "database": database_snapshot,
                "queue": {"rows": 12, "pending_apply": {"decision_count": 2}},
                "original_remainder_overview": {"review_ready_entry_count": 5},
                "batch_plan": {"rows": 1},
            },
            outputs=[],
            error="",
        )

        self.assertEqual(summary["archive_result_count"], 0)
        self.assertEqual(summary["archive_anomaly_count"], 0)
        self.assertEqual(summary["archive_results"], [])
        self.assertFalse(summary["zero_change"])
        self.assertEqual(
            [(item["label"], item["value"]) for item in summary["metrics"]],
            [
                ("Queue rows", "12"),
                ("Pending picker decisions", "2"),
                ("Review-ready entries", "5"),
                ("Search batches", "1"),
            ],
        )

    def test_broad_visual_match_summary_uses_search_metrics_not_index_metrics(self) -> None:
        broad_snapshot = {
            "descriptor_count": 222384,
            "result_count": 40,
            "latest_index_run": {
                "indexed_descriptor_count": 215161,
                "reused_descriptor_count": 7223,
                "error_count": 0,
            },
            "latest_run": {
                "target_count": 2,
                "processed_target_count": 2,
                "scanned_count": 1492,
                "matched_entries": 2,
                "result_count": 40,
                "error_count": 0,
            },
        }

        summary = control._workflow_run_summary(
            step="broad_visual_match",
            status="pass",
            payload={"candidate_scope": "date_window_limited", "date_window_days": 30},
            before={"broad_visual_match": {"descriptor_count": 222384, "result_count": 0}},
            after={"broad_visual_match": broad_snapshot},
            outputs=[],
            error="",
        )

        metrics = [(item["label"], item["value"]) for item in summary["metrics"]]
        self.assertEqual(
            metrics,
            [
                ("Entries searched", "2/2"),
                ("Candidate comparisons", "1492"),
                ("Matched entries", "2"),
                ("Saved candidate rows", "40"),
                ("Search errors", "0"),
            ],
        )
        metric_labels = [label for label, _value in metrics]
        self.assertNotIn("New/rebuilt fingerprints", metric_labels)
        self.assertEqual(summary["deltas"], {"broad_results": 40})

    def test_broad_visual_index_summary_uses_fingerprint_metrics(self) -> None:
        summary = control._workflow_run_summary(
            step="broad_visual_index",
            status="pass",
            payload={},
            before={"broad_visual_match": {"descriptor_count": 100, "result_count": 40}},
            after={
                "broad_visual_match": {
                    "descriptor_count": 222384,
                    "result_count": 40,
                    "latest_index_run": {
                        "indexed_descriptor_count": 215161,
                        "reused_descriptor_count": 7223,
                        "error_count": 0,
                    },
                    "latest_run": {"result_count": 40},
                }
            },
            outputs=[],
            error="",
        )

        self.assertEqual(
            [(item["label"], item["value"]) for item in summary["metrics"]],
            [
                ("Stored fingerprints", "222384"),
                ("New/rebuilt fingerprints", "215161"),
                ("Reused fingerprints", "7223"),
                ("Fingerprint errors", "0"),
            ],
        )
        self.assertEqual(summary["deltas"], {"broad_descriptors": 222284})

    def test_control_run_history_round_trips_step_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "control_run_history.jsonl"
            record = {
                "step": "import_zips",
                "status": "pass",
                "started_at": "2026-08-20T00:00:00+00:00",
                "finished_at": "2026-08-20T00:00:01+00:00",
                "outputs": [{"command": "import", "returncode": 0, "output": "PASS"}],
                "summary": {"metrics": [{"label": "Diary entries", "value": "2 (+1)"}]},
            }

            control._save_control_run_history(path, [record])
            loaded = control._load_control_run_history(path)

        self.assertEqual(loaded[0]["step"], "import_zips")
        self.assertEqual(loaded[0]["summary"]["metrics"][0]["value"], "2 (+1)")

    def test_apply_original_decisions_applies_then_prunes_review_queue(self) -> None:
        commands = control._commands_for_step("apply_original_decisions", {})

        self.assertEqual(len(commands), 2)
        self.assertIn("--apply-reviewed", commands[0])
        self.assertIn("--prune-applied-reviewed", commands[1])
        self.assertIn("original_photo_external_search_queue.csv", " ".join(commands[0]))
        self.assertIn("original_photo_external_search_queue.csv", " ".join(commands[1]))

    def test_generate_derivatives_command_requests_progress_output(self) -> None:
        command = control._commands_for_step("generate_derivatives", {})[0]

        self.assertIn("--progress-interval", command)
        self.assertIn("25", command)

    def test_generate_derivatives_uses_long_running_timeout(self) -> None:
        self.assertEqual(control._step_timeout_seconds("generate_derivatives"), 12 * 60 * 60)

    def test_generate_derivatives_timeout_summary_uses_latest_progress(self) -> None:
        outputs = [
            {
                "command": "generate",
                "returncode": -9,
                "output": (
                    "Progress: 1825/8361 sources · generated 1811 · skipped 0 · not ready 14 · current project365:2007-09-11\n"
                    "Progress: 1850/8361 sources · generated 1836 · skipped 0 · not ready 14 · current project365:2007-10-13\n"
                    "\nTimed out after 3600 seconds.\n"
                ),
            }
        ]

        summary = control._workflow_run_summary(
            step="generate_derivatives",
            status="fail",
            payload={},
            before={"database": {"diarium_derivatives": 868}},
            after={"database": {"diarium_derivatives": 868}},
            outputs=outputs,
            error="",
        )

        self.assertEqual(
            [(item["label"], item["value"]) for item in summary["metrics"][:4]],
            [
                ("Processed working-copy sources", "1850"),
                ("Generated working copies", "1836"),
                ("Skipped unchanged copies", "0"),
                ("Not ready for export", "14"),
            ],
        )
        self.assertEqual(
            summary["error"],
            "Timed out after 3600 seconds. Last progress: 1850/8361 sources, generated 1836, skipped 0, not ready 14, current project365:2007-10-13.",
        )

    def test_mark_fallback_step_requires_and_passes_batch_filters(self) -> None:
        with self.assertRaises(ValueError):
            control._commands_for_step("mark_original_fallback", {})

        commands = control._commands_for_step(
            "mark_original_fallback",
            {
                "entry_ids": ["project365:1998-04-12"],
                "entry_dates": ["1998-04-12"],
                "crawl_start_date": "1998-04-12",
                "crawl_end_date": "1998-04-12",
                "max_targets": "1",
            },
        )

        command = commands[0]
        self.assertIn("--mark-fallback", command)
        self.assertIn("--entry-id", command)
        self.assertIn("project365:1998-04-12", command)
        self.assertIn("--entry-date", command)
        self.assertIn("1998-04-12", command)
        self.assertIn("--start-date", command)
        self.assertIn("--end-date", command)
        self.assertIn("--max-targets", command)

    def test_score_alignment_step_requires_and_passes_batch_filters(self) -> None:
        with self.assertRaises(ValueError):
            control._commands_for_step("score_alignment", {})

        commands = control._commands_for_step(
            "score_alignment",
            {
                "entry_ids": ["project365:1998-04-12"],
                "entry_dates": ["1998-04-12"],
                "max_candidates": "12",
            },
        )

        command = commands[0]
        command_text = " ".join(command)
        self.assertIn("--score-queue-alignment", command)
        self.assertIn("original_photo_external_search_queue.csv", command_text)
        self.assertIn("--entry-id", command)
        self.assertIn("project365:1998-04-12", command)
        self.assertIn("--entry-date", command)
        self.assertIn("1998-04-12", command)
        self.assertIn("--score-max-candidates", command)
        self.assertIn("12", command)
        self.assertIn("--progress-interval", command)
        self.assertIn("10", command)

    def test_broad_visual_index_command_uses_separate_tool_and_database_defaults(self) -> None:
        command = control._commands_for_step(
            "broad_visual_index",
            {
                "candidate_roots": ["/Volumes/Archive Photos"],
                "density": "11",
                "start_date": "2021-01-01",
                "end_date": "2021-03-31",
                "date_window_days": "30",
                "include_low_quality_candidates": True,
                "overwrite_existing_fingerprints": True,
                "dry_run": True,
            },
        )[0]

        self.assertIn("project365_broad_visual_match.py", command)
        self.assertIn("index", command)
        self.assertIn("--candidate-root", command)
        self.assertIn("/Volumes/Archive Photos", command)
        self.assertIn("--density", command)
        self.assertIn("11", command)
        self.assertIn("--start-date", command)
        self.assertIn("2021-01-01", command)
        self.assertIn("--end-date", command)
        self.assertIn("2021-03-31", command)
        self.assertIn("--date-window-days", command)
        self.assertIn("30", command)
        self.assertIn("--include-low-quality", command)
        self.assertIn("--overwrite-existing", command)
        self.assertIn("--dry-run", command)

    def test_broad_visual_index_can_use_confirmed_originals_for_date_scope(self) -> None:
        command = control._commands_for_step(
            "broad_visual_index",
            {
                "confirmed_only": True,
                "start_date": "2003-01-01",
                "end_date": "2003-12-31",
                "density": "9",
            },
        )[0]

        self.assertIn("project365_broad_visual_match.py", command)
        self.assertIn("index-confirmed", command)
        self.assertIn("--start-date", command)
        self.assertIn("2003-01-01", command)
        self.assertIn("--end-date", command)
        self.assertIn("2003-12-31", command)
        self.assertNotIn("--candidate-root", command)

    def test_rough_prefilter_build_command_is_separate_from_broad_index(self) -> None:
        command = control._commands_for_step(
            "rough_prefilter_build",
            {
                "density": "11",
                "commit_interval": "250",
                "overwrite_existing_prefilter": True,
                "dry_run": True,
            },
        )[0]

        self.assertIn("project365_broad_visual_match.py", command)
        self.assertIn("prefilter", command)
        self.assertIn("--density", command)
        self.assertIn("11", command)
        self.assertIn("--commit-interval", command)
        self.assertIn("250", command)
        self.assertIn("--overwrite-existing", command)
        self.assertIn("--dry-run", command)
        self.assertNotIn("index-confirmed", command)

    def test_rough_visual_match_command_uses_no_date_shortlist_caps(self) -> None:
        command = control._commands_for_step(
            "rough_visual_match",
            {
                "entry_ids": ["project365:1998-04-12"],
                "start_date": "1998-04-01",
                "end_date": "1998-04-30",
                "shortlist_size": "1000",
                "per_band_hit_limit": "50000",
                "max_results": "20",
                "density": "9",
                "include_low_quality_candidates": True,
            },
        )[0]

        self.assertIn("project365_broad_visual_match.py", command)
        self.assertIn("match-no-date", command)
        self.assertIn("--entry-id", command)
        self.assertIn("project365:1998-04-12", command)
        self.assertIn("--start-date", command)
        self.assertIn("1998-04-01", command)
        self.assertIn("--end-date", command)
        self.assertIn("1998-04-30", command)
        self.assertIn("--shortlist-size", command)
        self.assertIn("1000", command)
        self.assertIn("--per-band-hit-limit", command)
        self.assertIn("50000", command)
        self.assertIn("--include-low-quality", command)
        self.assertNotIn("--date-window-days", command)
        self.assertNotIn("--candidate-scope", command)

    def test_rough_visual_match_command_can_target_fallback_originals(self) -> None:
        command = control._commands_for_step(
            "rough_visual_match",
            {
                "target_scope": "fallback_originals",
                "shortlist_size": "1000",
                "per_band_hit_limit": "50000",
                "max_results": "20",
            },
        )[0]

        self.assertIn("match-no-date", command)
        self.assertIn("--include-fallback-targets", command)

    def test_rough_visual_benchmark_command_reports_prefilter_recall(self) -> None:
        command = control._commands_for_step(
            "rough_visual_benchmark",
            {
                "shortlist_size": "1000",
                "per_band_hit_limit": "50000",
                "shortlist_sizes": "100,500,1000",
                "max_results": "50",
            },
        )[0]

        self.assertIn("benchmark", command)
        self.assertIn("--use-prefilter", command)
        self.assertIn("--shortlist-size", command)
        self.assertIn("1000", command)
        self.assertIn("--shortlist-sizes", command)
        self.assertIn("100,500,1000", command)

    def test_control_panel_includes_rough_visual_workflow_controls(self) -> None:
        self.assertIn('data-step="rough_visual_match"', control.CONTROL_HTML)
        self.assertIn("No-Date Visual Match", control.CONTROL_HTML)
        self.assertIn("Build rough prefilter", control.CONTROL_HTML)
        self.assertIn("Search no-date matches", control.CONTROL_HTML)
        self.assertIn("Measure no-date accuracy", control.CONTROL_HTML)
        self.assertIn('id="roughShortlistSize"', control.CONTROL_HTML)
        self.assertIn('id="roughPerBandHitLimit"', control.CONTROL_HTML)
        self.assertIn("Accuracy benchmark depth", control.CONTROL_HTML)
        self.assertIn('id="roughBenchmarkShortlistSizes"', control.CONTROL_HTML)
        self.assertIn("Standard: 100 / 500 / 1,000 / 5,000", control.CONTROL_HTML)
        self.assertIn("Quick: 100 / 500 / 1,000", control.CONTROL_HTML)
        self.assertIn("Deep: 500 / 1,000 / 5,000 / 10,000", control.CONTROL_HTML)
        self.assertIn("rough_prefilter_build", control.CONTROL_HTML)
        self.assertIn("rough_visual_benchmark", control.CONTROL_HTML)
        self.assertLess(
            control.CONTROL_HTML.index("Broad Visual Match"),
            control.CONTROL_HTML.index("No-Date Visual Match"),
        )

    def test_broad_visual_match_command_passes_target_candidate_and_resume_settings(self) -> None:
        with mock.patch.object(
            control.broad_visual_match,
            "current_match_run_id",
            return_value="broad-match:current",
        ) as current_run:
            command = control._commands_for_step(
                "broad_visual_match",
                {
                    "entry_ids": ["project365:1998-04-12"],
                    "start_date": "1998-04-01",
                    "end_date": "1998-04-30",
                    "broad_search_needed_list": "/tmp/needed.csv",
                    "candidate_scope": "date_window_limited",
                    "candidate_roots": ["/Volumes/Archive Photos"],
                    "date_window_days": "5",
                    "max_results": "25",
                    "density": "13",
                    "include_low_quality_candidates": True,
                    "resume_existing_run": True,
                    "dry_run": True,
                },
            )[0]

        current_run.assert_called_once_with(control.BROAD_VISUAL_DB, control.broad_visual_match.CURRENT_BROAD_MATCH_SLOT)
        self.assertIn("project365_broad_visual_match.py", command)
        self.assertIn("match", command)
        self.assertIn("--entry-id", command)
        self.assertIn("project365:1998-04-12", command)
        self.assertIn("--start-date", command)
        self.assertIn("--end-date", command)
        self.assertIn("--broad-search-needed-list", command)
        self.assertIn("/tmp/needed.csv", command)
        self.assertIn("--candidate-scope", command)
        self.assertIn("date_window_limited", command)
        self.assertIn("--candidate-root", command)
        self.assertIn("/Volumes/Archive Photos", command)
        self.assertIn("--date-window-days", command)
        self.assertIn("5", command)
        self.assertIn("--max-results", command)
        self.assertIn("25", command)
        self.assertIn("--density", command)
        self.assertIn("13", command)
        self.assertIn("--resume-run", command)
        self.assertIn("broad-match:current", command)
        self.assertIn("--include-low-quality", command)
        self.assertIn("--dry-run", command)

    def test_broad_visual_match_command_can_target_fallback_originals(self) -> None:
        command = control._commands_for_step(
            "broad_visual_match",
            {
                "target_scope": "fallback_originals",
                "candidate_scope": "date_window_limited",
                "date_window_days": "30",
            },
        )[0]

        self.assertIn("match", command)
        self.assertIn("--include-fallback-targets", command)

    def test_broad_visual_match_refuses_confirmed_only_accuracy_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "not an unresolved search"):
            control._commands_for_step(
                "broad_visual_match",
                {
                    "confirmed_only": True,
                    "start_date": "2003-01-01",
                    "end_date": "2003-12-31",
                },
            )

    def test_broad_visual_match_refuses_date_range_whole_library(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use a candidate date window"):
            control._commands_for_step(
                "broad_visual_match",
                {
                    "start_date": "1998-01-01",
                    "end_date": "2000-01-01",
                    "candidate_scope": "whole_indexed_library",
                    "date_window_days": "30",
                },
            )

    def test_broad_visual_match_requires_positive_candidate_date_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than 0"):
            control._commands_for_step(
                "broad_visual_match",
                {
                    "start_date": "1998-01-01",
                    "end_date": "2000-01-01",
                    "candidate_scope": "date_window_limited",
                    "date_window_days": "0",
                },
            )

    def test_broad_visual_benchmark_command_exports_metadata_report(self) -> None:
        command = control._commands_for_step(
            "broad_visual_benchmark",
            {
                "max_results": "10",
                "start_date": "2003-01-01",
                "end_date": "2003-12-31",
            },
        )[0]

        self.assertIn("project365_broad_visual_match.py", command)
        self.assertIn("benchmark", command)
        self.assertIn("--report-dir", command)
        self.assertIn("Project365Canonical/exports/verification_reports", command)
        self.assertIn("--max-results", command)
        self.assertIn("10", command)
        self.assertIn("--start-date", command)
        self.assertIn("2003-01-01", command)
        self.assertIn("--end-date", command)
        self.assertIn("2003-12-31", command)

    def test_broad_visual_control_html_exposes_separate_workflow_controls(self) -> None:
        self.assertIn("Broad Visual Match", control.CONTROL_HTML)
        self.assertIn('data-step="broad_visual_match"', control.CONTROL_HTML)
        self.assertIn('id="broadCandidateRoots"', control.CONTROL_HTML)
        self.assertIn('id="broadTargetScope"', control.CONTROL_HTML)
        self.assertIn('value="all_unresolved"', control.CONTROL_HTML)
        self.assertIn('value="fallback_originals"', control.CONTROL_HTML)
        self.assertIn('value="entry_ids"', control.CONTROL_HTML)
        self.assertIn('value="date_range"', control.CONTROL_HTML)
        self.assertIn('value="broad_search_needed_list"', control.CONTROL_HTML)
        self.assertIn('id="broadCandidateScope"', control.CONTROL_HTML)
        self.assertIn('<option value="date_window_limited">Only candidates dated near each entry</option>', control.CONTROL_HTML)
        self.assertIn('value="whole_indexed_library"', control.CONTROL_HTML)
        self.assertIn('value="folder_limited"', control.CONTROL_HTML)
        self.assertIn('value="date_window_limited"', control.CONTROL_HTML)
        self.assertIn('value="same_setting_folder_limited"', control.CONTROL_HTML)
        self.assertIn('id="broadMaxResults"', control.CONTROL_HTML)
        self.assertIn('id="broadDensity"', control.CONTROL_HTML)
        self.assertIn('id="broadIncludeLowQuality"', control.CONTROL_HTML)
        self.assertIn('id="broadConfirmedOnly"', control.CONTROL_HTML)
        self.assertIn('id="broadResumeExisting"', control.CONTROL_HTML)
        self.assertIn('id="broadOverwriteFingerprints"', control.CONTROL_HTML)
        self.assertIn('id="broadDryRun"', control.CONTROL_HTML)
        self.assertIn("Project365 entries to search", control.CONTROL_HTML)
        self.assertIn("Original candidates to compare", control.CONTROL_HTML)
        self.assertIn("Candidate date window, +/- days", control.CONTROL_HTML)
        self.assertIn('id="broadDateWindowDays" type="number" min="0" value="30"', control.CONTROL_HTML)
        self.assertIn("Date-range unresolved search must use a candidate date window", control.CONTROL_HTML)
        self.assertIn("Fingerprint already confirmed originals", control.CONTROL_HTML)
        self.assertIn("for accuracy benchmark", control.CONTROL_HTML)
        self.assertIn("does not rebuild monthly candidate coverage", control.CONTROL_HTML)
        self.assertIn("Overwrite existing fingerprints", control.CONTROL_HTML)
        self.assertIn("already-current fingerprints are skipped for speed", control.CONTROL_HTML)
        self.assertIn("1. Build fingerprints", control.CONTROL_HTML)
        self.assertIn("2. Search unresolved photos", control.CONTROL_HTML)
        self.assertIn("3. Review unresolved results (loading)", control.CONTROL_HTML)
        self.assertIn('id="broadVisualActionMessage"', control.CONTROL_HTML)
        self.assertIn("Accuracy diagnostics", control.CONTROL_HTML)
        self.assertIn(">Measure accuracy</button>", control.CONTROL_HTML)
        self.assertIn("Open accuracy benchmark review", control.CONTROL_HTML)
        self.assertIn('id="broadVisualBenchmarkMessage"', control.CONTROL_HTML)
        self.assertNotIn('id="broadVisualMessage"', control.CONTROL_HTML)
        self.assertNotIn("2. Measure accuracy", control.CONTROL_HTML)
        self.assertNotIn("4. Review unresolved results (loading)", control.CONTROL_HTML)
        self.assertIn("Review options", control.CONTROL_HTML)
        self.assertNotIn('id="broadReviewMode"', control.CONTROL_HTML)
        self.assertNotIn("Ready unresolved results", control.CONTROL_HTML)
        self.assertNotIn("Accuracy benchmark (known originals)", control.CONTROL_HTML)
        self.assertIn('id="broadReviewEntryId"', control.CONTROL_HTML)
        self.assertIn("Jump to entry date", control.CONTROL_HTML)
        self.assertIn("YYYY-MM-DD or project365:YYYY-MM-DD", control.CONTROL_HTML)
        self.assertNotIn('id="broadReviewLimit"', control.CONTROL_HTML)
        self.assertIn("Opens one target and its candidates at a time.", control.CONTROL_HTML)
        self.assertIn('id="broadReviewResultsButton" class="button primary"', control.CONTROL_HTML)
        self.assertIn('data-review-mode="search"', control.CONTROL_HTML)
        self.assertIn("3. Review unresolved results (loading)", control.CONTROL_HTML)
        self.assertIn("function updateBroadReviewButton", control.CONTROL_HTML)
        self.assertIn('broad_visual_match: "broadVisualActionMessage"', control.CONTROL_HTML)
        self.assertIn('broad_visual_index: "broadVisualActionMessage"', control.CONTROL_HTML)
        self.assertIn('broad_visual_benchmark: "broadVisualBenchmarkMessage"', control.CONTROL_HTML)
        self.assertIn("button.dataset.reviewMode", control.CONTROL_HTML)
        self.assertIn("button.dataset.reviewSet", control.CONTROL_HTML)
        self.assertIn("button.dataset.unresolvedReadyCount", control.CONTROL_HTML)
        self.assertIn("latestSearchRunId", control.CONTROL_HTML)
        self.assertIn("No unresolved entries are currently review-ready.", control.CONTROL_HTML)
        self.assertIn("Review unresolved results", control.CONTROL_HTML)
        self.assertIn("Accuracy benchmark (diagnostic)", control.CONTROL_HTML)
        self.assertNotIn("benchmark entries", control.CONTROL_HTML)
        self.assertIn("/broad-review?${params.toString()}", control.CONTROL_HTML)
        self.assertIn("function normalizeProject365EntryJump(value)", control.CONTROL_HTML)
        self.assertIn("`project365:${text}`", control.CONTROL_HTML)
        self.assertIn("unresolvedReadyCount", control.CONTROL_HTML)
        self.assertIn("No unresolved Broad Visual results are ready yet.", control.CONTROL_HTML)
        self.assertIn("searches entries without a confirmed original", control.CONTROL_HTML)
        self.assertIn("Fingerprint already confirmed originals is for Build fingerprints plus Measure accuracy", control.CONTROL_HTML)
        self.assertLess(
            control.CONTROL_HTML.index("2. Search unresolved photos"),
            control.CONTROL_HTML.index("3. Review unresolved results (loading)"),
        )
        self.assertLess(
            control.CONTROL_HTML.index("3. Review unresolved results (loading)"),
            control.CONTROL_HTML.index("Review options"),
        )

    def test_broad_review_page_is_precomputed_only_and_has_direct_match_actions(self) -> None:
        self.assertIn("Visual review for stored broad-search results", control.BROAD_REVIEW_HTML)
        self.assertIn('href="/?step=broad_visual_match"', control.BROAD_REVIEW_HTML)
        self.assertIn("Project365 Control Panel", control.BROAD_REVIEW_HTML)
        self.assertNotIn('href="/picker?status=needs_review"', control.BROAD_REVIEW_HTML)
        self.assertIn("/broad/api/review-entries", control.BROAD_REVIEW_HTML)
        self.assertIn("/broad/api/benchmark-entries", control.BROAD_REVIEW_HTML)
        self.assertIn("/broad/api/confirm-match", control.BROAD_REVIEW_HTML)
        self.assertIn("/broad/api/keep-project365", control.BROAD_REVIEW_HTML)
        self.assertIn("/broad/api/reject-entry", control.BROAD_REVIEW_HTML)
        self.assertIn("/broad/api/undo-entry-decision", control.BROAD_REVIEW_HTML)
        self.assertNotIn("/broad/api/review-runs", control.BROAD_REVIEW_HTML)
        self.assertNotIn("/broad/api/clear-review-run", control.BROAD_REVIEW_HTML)
        self.assertNotIn("/broad/api/clear-all-review-runs", control.BROAD_REVIEW_HTML)
        self.assertNotIn("Previous reviews", control.BROAD_REVIEW_HTML)
        self.assertNotIn("Review type", control.BROAD_REVIEW_HTML)
        self.assertNotIn("Run ID", control.BROAD_REVIEW_HTML)
        self.assertNotIn("Load entries", control.BROAD_REVIEW_HTML)
        self.assertNotIn("function clearBroadReviewRun(runId)", control.BROAD_REVIEW_HTML)
        self.assertNotIn("function clearAllBroadReviewRuns()", control.BROAD_REVIEW_HTML)
        self.assertIn("Use Project365 photo", control.BROAD_REVIEW_HTML)
        self.assertIn("function keepProject365Photo", control.BROAD_REVIEW_HTML)
        self.assertIn("Keep the Project365 export for this entry", control.BROAD_REVIEW_HTML)
        self.assertIn("Reject all (R)", control.BROAD_REVIEW_HTML)
        self.assertIn("Previous entry (Left)", control.BROAD_REVIEW_HTML)
        self.assertIn("Next entry (Right)", control.BROAD_REVIEW_HTML)
        self.assertIn("function handleBroadReviewKeyboardShortcut(event)", control.BROAD_REVIEW_HTML)
        self.assertIn('event.key === "ArrowLeft"', control.BROAD_REVIEW_HTML)
        self.assertIn('event.key === "ArrowRight"', control.BROAD_REVIEW_HTML)
        self.assertIn('event.key.toLowerCase() === "r"', control.BROAD_REVIEW_HTML)
        self.assertIn('document.addEventListener("keydown", handleBroadReviewKeyboardShortcut);', control.BROAD_REVIEW_HTML)
        self.assertNotIn(">Clear all<", control.BROAD_REVIEW_HTML)
        self.assertIn(">Match<", control.BROAD_REVIEW_HTML)
        self.assertIn("candidate_url", control.BROAD_REVIEW_HTML)
        self.assertIn("<img", control.BROAD_REVIEW_HTML)
        self.assertIn('id="imagePreviewModal"', control.BROAD_REVIEW_HTML)
        self.assertIn('class="image-preview-modal"', control.BROAD_REVIEW_HTML)
        self.assertIn("function openBroadImagePreviewFromTrigger(trigger)", control.BROAD_REVIEW_HTML)
        self.assertIn("function closeBroadImagePreview()", control.BROAD_REVIEW_HTML)
        self.assertIn("function handleImagePreviewBackdrop(event)", control.BROAD_REVIEW_HTML)
        self.assertIn('params.set("max", "2048");', control.BROAD_REVIEW_HTML)
        self.assertIn("image.src = largeBroadImageUrl(url);", control.BROAD_REVIEW_HTML)
        self.assertIn('image.removeAttribute("src");', control.BROAD_REVIEW_HTML)
        self.assertIn('event.key === "Escape"', control.BROAD_REVIEW_HTML)
        self.assertIn("data-preview-url", control.BROAD_REVIEW_HTML)
        self.assertIn('onclick="openBroadImagePreviewFromTrigger(this)"', control.BROAD_REVIEW_HTML)
        self.assertIn(".image-preview-modal.is-open", control.BROAD_REVIEW_HTML)
        self.assertIn("cursor: zoom-in", control.BROAD_REVIEW_HTML)
        self.assertIn('query.get("mode")', control.BROAD_REVIEW_HTML)
        self.assertIn('query.get("run_id")', control.BROAD_REVIEW_HTML)
        self.assertIn('query.get("entry_id")', control.BROAD_REVIEW_HTML)
        self.assertIn("const currentLimit = 1", control.BROAD_REVIEW_HTML)
        self.assertNotIn('query.get("limit")', control.BROAD_REVIEW_HTML)
        self.assertNotIn("Math.min(10", control.BROAD_REVIEW_HTML)
        self.assertIn("let currentEntryDate", control.BROAD_REVIEW_HTML)
        self.assertIn("function loadEntriesByDate(direction, entryDate)", control.BROAD_REVIEW_HTML)
        self.assertIn('params.set(direction === "before" ? "before_date" : "after_date", entryDate);', control.BROAD_REVIEW_HTML)
        self.assertIn('await loadEntriesAfterRemoval(payload.entry_date || "");', control.BROAD_REVIEW_HTML)
        self.assertIn("function resetBroadReviewScrollToCandidateList()", control.BROAD_REVIEW_HTML)
        self.assertIn('document.querySelector(".candidate-pane")', control.BROAD_REVIEW_HTML)
        self.assertIn('target.scrollIntoView({block: "start", inline: "nearest"});', control.BROAD_REVIEW_HTML)
        self.assertIn("Undo last action", control.BROAD_REVIEW_HTML)
        self.assertIn("function undoLastBroadDecision()", control.BROAD_REVIEW_HTML)
        self.assertIn("function setLastBroadDecision(payload)", control.BROAD_REVIEW_HTML)
        self.assertIn("picker_queue_path=ORIGINAL_QUEUE", control.__loader__.get_source(control.__name__))
        self.assertIn("Known confirmed original", control.BROAD_REVIEW_HTML)
        self.assertNotIn("Review match in Picker", control.BROAD_REVIEW_HTML)
        self.assertNotIn("sendCandidateToPicker", control.BROAD_REVIEW_HTML)
        self.assertIn("confirmBroadMatch", control.BROAD_REVIEW_HTML)
        self.assertIn("rejectBroadEntry", control.BROAD_REVIEW_HTML)
        self.assertIn("function formatBroadPhotoFacts", control.BROAD_REVIEW_HTML)
        self.assertNotIn("function syncCandidateImageFacts", control.BROAD_REVIEW_HTML)
        self.assertNotIn('data-facts-target=', control.BROAD_REVIEW_HTML)
        self.assertNotIn("naturalWidth", control.BROAD_REVIEW_HTML)
        self.assertIn("function locationIndicator(hasLocation)", control.BROAD_REVIEW_HTML)
        self.assertIn(".entry-head { position: sticky; top: 0;", control.BROAD_REVIEW_HTML)
        self.assertIn(".review-layout { display: grid; grid-template-columns: minmax(380px, 520px) minmax(360px, 1fr);", control.BROAD_REVIEW_HTML)
        self.assertIn(".source-pane { position: sticky; top: 64px;", control.BROAD_REVIEW_HTML)
        self.assertIn(".entry-head { position: static; }", control.BROAD_REVIEW_HTML)
        self.assertIn(".candidate-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));", control.BROAD_REVIEW_HTML)
        self.assertIn(".candidate-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }", control.BROAD_REVIEW_HTML)
        self.assertIn(".candidate-grid { grid-template-columns: 1fr; }", control.BROAD_REVIEW_HTML)
        self.assertIn(".candidate-card { border: 1px solid var(--line); border-radius: 8px; padding: 8px; overflow: hidden; display: grid; grid-template-rows: 210px auto auto;", control.BROAD_REVIEW_HTML)
        self.assertIn(".candidate-image { width: 100%; height: 210px;", control.BROAD_REVIEW_HTML)
        self.assertIn("function sourceBox(title, url, path, facts = {})", control.BROAD_REVIEW_HTML)
        self.assertNotIn("function imageBox(", control.BROAD_REVIEW_HTML)
        self.assertNotIn(".compare-grid", control.BROAD_REVIEW_HTML)
        self.assertIn(".location-indicator.has-location { color: #b42318; }", control.BROAD_REVIEW_HTML)
        self.assertIn('aria-label="${hasLocation ? "Embedded location metadata" : "No embedded location metadata"}"', control.BROAD_REVIEW_HTML)
        self.assertIn("has_geolocation", control.BROAD_REVIEW_HTML)
        self.assertIn("dimensions", control.BROAD_REVIEW_HTML)
        self.assertIn("byte_size", control.BROAD_REVIEW_HTML)
        self.assertIn("row.result_id && row.candidate_url", control.BROAD_REVIEW_HTML)
        self.assertIn("max_size=640", control.__loader__.get_source(control.__name__))
        date_navigation_body = control.BROAD_REVIEW_HTML.split(
            "async function loadEntriesByDate(direction, entryDate)"
        )[1].split("async function loadEntriesAfterRemoval")[0]
        self.assertNotIn('params.set("entry_id"', date_navigation_body)

    def test_broad_review_entries_omit_missing_candidate_image_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            existing = Path(temp_dir) / "existing.jpg"
            missing = Path(temp_dir) / "missing.jpg"
            existing.write_bytes(b"image")
            state = control.ControlState()
            server = control.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            payload = {
                "run_id": "run-1",
                "entries": [
                    {
                        "entry_id": "project365:1998-04-11",
                        "entry_date": "1998-04-11",
                        "source_path": "",
                        "confirmed_path": "",
                        "results": [
                            {
                                "result_id": 1,
                                "candidate_path": str(missing),
                                "candidate_filename": missing.name,
                            },
                            {
                                "result_id": 2,
                                "candidate_path": str(existing),
                                "candidate_filename": existing.name,
                            },
                        ],
                    },
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "source_path": "",
                        "confirmed_path": "",
                        "results": [
                            {
                                "result_id": 3,
                                "candidate_path": str(existing),
                                "candidate_filename": existing.name,
                            },
                        ],
                    },
                ],
                "returned_count": 1,
                "has_more": False,
                "offset": 0,
                "limit": 1,
            }
            thread.start()
            try:
                with (
                    mock.patch.object(control.broad_visual_match, "review_entries", return_value=payload) as review_entries,
                    mock.patch.object(
                        control,
                        "_broad_photo_facts",
                        return_value={
                            "mime_type": "image/jpeg",
                            "byte_size": 5,
                            "dimensions": "",
                            "has_geolocation": False,
                        },
                    ),
                ):
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/broad/api/review-entries?limit=10",
                        timeout=5,
                    ) as response:
                        result = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        rows = result["entries"][0]["results"]
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["limit"], control.BROAD_REVIEW_ENTRY_LIMIT)
        self.assertEqual(review_entries.call_args.kwargs["limit"], control.BROAD_REVIEW_ENTRY_LIMIT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_path"], str(existing))
        self.assertRegex(rows[0]["candidate_url"], r"^/broad/image/[0-9a-f]{24}\?max=640$")
        self.assertNotIn('parts.push(facts.has_geolocation ? "GPS" : "No GPS")', control.BROAD_REVIEW_HTML)
        self.assertIn("fileName(path)", control.BROAD_REVIEW_HTML)
        self.assertIn('replace(/\\\\/g, "\\\\\\\\")', control.BROAD_REVIEW_HTML)
        self.assertNotIn("replace(/\\/g", control.BROAD_REVIEW_HTML)
        self.assertNotIn("Visual distance", control.BROAD_REVIEW_HTML)
        self.assertNotIn("square_y", control.BROAD_REVIEW_HTML)
        self.assertNotIn("square_x", control.BROAD_REVIEW_HTML)
        self.assertNotIn("best_view", control.BROAD_REVIEW_HTML)
        self.assertNotIn("score_gap", control.BROAD_REVIEW_HTML)
        self.assertNotIn("sendSelectedToPicker", control.BROAD_REVIEW_HTML)
        self.assertNotIn("data-result-id", control.BROAD_REVIEW_HTML)
        self.assertNotIn("/picker/image/", control.BROAD_REVIEW_HTML)

    def test_broad_image_route_uses_preview_size_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original = Path(temp_dir) / "original.heic"
            preview = Path(temp_dir) / "preview.jpg"
            original.write_bytes(b"raw-original")
            preview.write_bytes(b"jpeg-preview")
            state = control.ControlState()
            token = state.broad_image_token(str(original))
            server = control.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(state, "broad_preview_path", return_value=preview) as broad_preview_path:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/broad/image/{token}?max=640",
                        timeout=5,
                    ) as response:
                        payload = response.read()
                        content_type = response.headers.get("content-type")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        broad_preview_path.assert_called_once_with(token, 640)
        self.assertEqual(payload, b"jpeg-preview")
        self.assertEqual(content_type, "image/jpeg")

    def test_media_dedupe_review_page_and_control_link_are_available(self) -> None:
        self.assertIn("media_dedupe_review", control.WORKFLOW_STEPS)
        self.assertIn('data-step="media_dedupe_review"', control.CONTROL_HTML)
        self.assertIn('href="/dedupe"', control.CONTROL_HTML)
        self.assertIn("Open dedupe review", control.CONTROL_HTML)
        self.assertIn("Media Dedupe Review", control.MEDIA_DEDUPE_HTML)
        self.assertIn("/dedupe/api/candidates", control.MEDIA_DEDUPE_HTML)
        self.assertIn("/dedupe/api/decision", control.MEDIA_DEDUPE_HTML)
        self.assertIn("<video controls", control.MEDIA_DEDUPE_HTML)
        self.assertIn("zoom-button", control.MEDIA_DEDUPE_HTML)
        self.assertIn("facts-grid", control.MEDIA_DEDUPE_HTML)
        self.assertIn("openDedupePreview", control.MEDIA_DEDUPE_HTML)
        self.assertIn("confirm_duplicate_delete_legacy", control.MEDIA_DEDUPE_HTML)
        self.assertIn("confirm_duplicate_delete_reexport", control.MEDIA_DEDUPE_HTML)
        self.assertIn("reject_duplicate", control.MEDIA_DEDUPE_HTML)
        self.assertIn('event.key === "ArrowRight"', control.MEDIA_DEDUPE_HTML)

    def test_media_dedupe_api_adds_media_urls_and_records_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            new_video = base / "new" / "IMG_0001.MOV"
            old_video = base / "old" / "IMG_0001.MOV"
            new_video.parent.mkdir()
            old_video.parent.mkdir()
            new_video.write_bytes(b"new-video")
            old_video.write_bytes(b"old-video")
            _write_dedupe_index(
                canonical_root / "photo_library_reexport_index.sqlite",
                [
                    _dedupe_row(
                        new_video,
                        root=new_video.parent,
                        extension=".mov",
                        capture_timestamp="2021-05-04T12:00:00",
                        date_value="2021-05-04",
                        duration=10.0,
                    )
                ],
            )
            _write_dedupe_index(
                canonical_root / "video_library_legacy_index.sqlite",
                [
                    _dedupe_row(
                        old_video,
                        root=old_video.parent,
                        extension=".mov",
                        capture_timestamp="2021-05-04T12:00:01",
                        date_value="2021-05-04",
                        duration=10.4,
                    )
                ],
            )
            state = control.ControlState()
            server = control.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(control, "CANONICAL_ROOT", canonical_root):
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/dedupe/api/candidates?media_type=video",
                        timeout=5,
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    candidate = payload["candidates"][0]
                    decision_request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/dedupe/api/decision",
                        data=json.dumps(
                            {
                                "media_type": "video",
                                "candidate_key": candidate["candidate_key"],
                                "reexport_path": candidate["reexport"]["path"],
                                "legacy_path": candidate["legacy"]["path"],
                                "decision": "confirm_duplicate_delete_legacy",
                                "notes": "reviewed",
                            }
                        ).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(decision_request, timeout=5) as response:
                        decision = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(payload["media_type"], "video")
        self.assertRegex(candidate["reexport"]["media_url"], r"^/dedupe/media/[0-9a-f]{24}$")
        self.assertRegex(candidate["legacy"]["media_url"], r"^/dedupe/media/[0-9a-f]{24}$")
        self.assertEqual(decision["decision"], "confirm_duplicate_delete_legacy")
        self.assertFalse(decision["file_action_taken"])

    def test_photo_dedupe_api_uses_local_preview_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            new_photo = base / "new" / "IMG_1000.HEIC"
            old_photo = base / "old" / "IMG_1000.HEIC"
            new_photo.parent.mkdir()
            old_photo.parent.mkdir()
            new_photo.write_bytes(b"same-photo")
            old_photo.write_bytes(b"same-photo")
            _write_dedupe_index(
                canonical_root / "photo_library_reexport_index.sqlite",
                [
                    _dedupe_row(
                        new_photo,
                        root=new_photo.parent,
                        extension=".heic",
                        capture_timestamp="2021-06-01T09:00:00",
                        date_value="2021-06-01",
                    )
                ],
            )
            _write_dedupe_index(
                canonical_root / "photo_library_index.sqlite",
                [
                    _dedupe_row(
                        old_photo,
                        root=old_photo.parent,
                        extension=".heic",
                        capture_timestamp="2021-06-01T09:00:00",
                        date_value="2021-06-01",
                    )
                ],
            )
            state = control.ControlState()
            server = control.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(control, "CANONICAL_ROOT", canonical_root):
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/dedupe/api/candidates?media_type=photo",
                        timeout=5,
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        candidate = payload["candidates"][0]
        self.assertEqual(payload["media_type"], "photo")
        self.assertRegex(candidate["reexport"]["media_url"], r"^/broad/image/[0-9a-f]{24}\?max=1280$")
        self.assertRegex(candidate["legacy"]["media_url"], r"^/broad/image/[0-9a-f]{24}\?max=1280$")

    def test_dedupe_media_route_supports_range_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "clip.mov"
            media_path.write_bytes(b"0123456789")
            state = control.ControlState()
            token = state.broad_image_token(str(media_path))
            server = control.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/dedupe/media/{token}",
                    headers={"Range": "bytes=2-5"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = response.read()
                    status = response.status
                    content_range = response.headers.get("content-range")
                    accept_ranges = response.headers.get("accept-ranges")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(status, 206)
        self.assertEqual(payload, b"2345")
        self.assertEqual(content_range, "bytes 2-5/10")
        self.assertEqual(accept_ranges, "bytes")

    def test_control_html_delegates_original_review_to_picker(self) -> None:
        self.assertIn("Original-photo review", control.CONTROL_HTML)
        self.assertIn("Review batches, search folders, and choose the identified original in the visual picker.", control.CONTROL_HTML)
        self.assertIn("Crop confirmation", control.CONTROL_HTML)
        self.assertIn("Open crop confirmation", control.CONTROL_HTML)
        self.assertIn("Batch estimate crop", control.CONTROL_HTML)
        self.assertIn('id="applyCropEstimates"', control.CONTROL_HTML)
        self.assertIn('id="applyCropEstimates" type="checkbox" checked', control.CONTROL_HTML)
        self.assertIn("Save estimates as calculated", control.CONTROL_HTML)
        self.assertIn('id="estimateCropBatchButton"', control.CONTROL_HTML)
        self.assertIn('id="openCropConfirmationButton"', control.CONTROL_HTML)
        self.assertIn('url.searchParams.set("crop_filter", "estimated")', control.CONTROL_HTML)
        self.assertIn("function attachCropEstimateBatchJob(payload)", control.CONTROL_HTML)
        self.assertIn("function cropEstimateProgressDetail(job)", control.CONTROL_HTML)
        self.assertIn("const canCancel = job.cancellable !== false", control.CONTROL_HTML)
        self.assertLess(
            control.CONTROL_HTML.index('id="estimateCropBatchButton"'),
            control.CONTROL_HTML.index('id="applyCropEstimates"'),
        )
        self.assertLess(
            control.CONTROL_HTML.index('id="applyCropEstimates"'),
            control.CONTROL_HTML.index('id="openCropConfirmationButton"'),
        )
        self.assertIn("function startCropEstimateBatch()", control.CONTROL_HTML)
        self.assertIn('const applyEstimates = Boolean(document.getElementById("applyCropEstimates")?.checked);', control.CONTROL_HTML)
        self.assertIn("body: JSON.stringify({apply_estimates: applyEstimates})", control.CONTROL_HTML)
        self.assertIn('fetchJson("/api/crop-estimate-batch"', control.CONTROL_HTML)
        self.assertIn('fetchJson(`/api/crop-estimate-batch/', control.CONTROL_HTML)
        self.assertIn("No missing crop estimates to run.", control.CONTROL_HTML)
        self.assertIn('onclick="openCropConfirmation()"', control.CONTROL_HTML)
        self.assertIn('const url = new URL("/crop", window.location.href);', control.CONTROL_HTML)
        self.assertIn("window.location.assign(url.toString())", control.CONTROL_HTML)
        self.assertNotIn('<a href="/crop">Open crop confirmation</a>', control.CONTROL_HTML)
        self.assertIn("Review easy matches (0)", control.CONTROL_HTML)
        self.assertIn("Open visual picker", control.CONTROL_HTML)
        self.assertIn("Diary enrichment", control.CONTROL_HTML)
        self.assertIn('data-step="diary_enrichment"', control.CONTROL_HTML)
        self.assertIn('id="diaryEnrichmentStartDate"', control.CONTROL_HTML)
        self.assertIn('id="diaryEnrichmentEndDate"', control.CONTROL_HTML)
        self.assertIn('id="diaryEnrichmentLimit"', control.CONTROL_HTML)
        self.assertIn("function openDiaryEnrichment()", control.CONTROL_HTML)
        self.assertIn("function dateScopeFromInputValues(startValue, endValue = \"\", options = {})", control.CONTROL_HTML)
        self.assertIn("function parseDateScopeValue(value)", control.CONTROL_HTML)
        self.assertIn('url.searchParams.set("start_date", scope.startDate)', control.CONTROL_HTML)
        self.assertIn('url.searchParams.set("end_date", scope.endDate)', control.CONTROL_HTML)
        self.assertIn('url.searchParams.set("limit", String(boundedLimit))', control.CONTROL_HTML)
        self.assertIn('id="limitEasyMatchToIndexFolder"', control.CONTROL_HTML)
        self.assertIn('id="easyMatchIndexFolder"', control.CONTROL_HTML)
        self.assertIn("Limit easy matches to one indexed folder", control.CONTROL_HTML)
        self.assertIn("Choose file in folder", control.CONTROL_HTML)
        self.assertIn("chooseFolderFromIndexedFile('easyMatchIndexFolder')", control.CONTROL_HTML)
        self.assertIn('id="easyMatchIndexFile"', control.CONTROL_HTML)
        self.assertIn("function sha256File(file)", control.CONTROL_HTML)
        self.assertIn('fetchJson("/api/photo-index-folder-from-file"', control.CONTROL_HTML)
        self.assertIn("limit_to_photo_index_folder: limitToFolder", control.CONTROL_HTML)
        self.assertIn("photo_index_folder: limitToFolder ? folder : \"\"", control.CONTROL_HTML)
        self.assertNotIn('id="controlFolderBrowser"', control.CONTROL_HTML)
        self.assertNotIn("function showControlFolderBrowser", control.CONTROL_HTML)
        self.assertNotIn("function openControlFolder", control.CONTROL_HTML)
        self.assertNotIn("function useControlFolder", control.CONTROL_HTML)
        self.assertNotIn("Targeted remainder search", control.CONTROL_HTML)
        self.assertNotIn("Run targeted search", control.CONTROL_HTML)
        self.assertNotIn("Use selected batch", control.CONTROL_HTML)
        self.assertNotIn("Use next search batch", control.CONTROL_HTML)
        self.assertNotIn("Keep fallback for selected batch", control.CONTROL_HTML)
        self.assertNotIn("Score selected batch", control.CONTROL_HTML)
        self.assertNotIn('id="batchSelect"', control.CONTROL_HTML)
        self.assertNotIn('id="targetedSearchRoots"', control.CONTROL_HTML)
        self.assertNotIn('id="crawlEntryIds"', control.CONTROL_HTML)
        self.assertNotIn('id="crawlEntryDates"', control.CONTROL_HTML)
        self.assertNotIn('id="batchPreview"', control.CONTROL_HTML)
        self.assertIn('id="batchOverview"', control.CONTROL_HTML)
        self.assertIn("function renderBatchOverview(", control.CONTROL_HTML)
        self.assertIn("Search attempts", control.CONTROL_HTML)
        self.assertIn("Latest search attempt", control.CONTROL_HTML)
        self.assertIn("function formatSearchAttempt(", control.CONTROL_HTML)
        self.assertIn("Current original review", control.CONTROL_HTML)
        self.assertIn("Review-ready", control.CONTROL_HTML)
        self.assertIn("Need folder", control.CONTROL_HTML)
        self.assertIn("Hidden export copies", control.CONTROL_HTML)
        self.assertIn("hidden_export_equivalent_candidate_count", control.CONTROL_HTML)
        self.assertNotIn('id="attemptOverview"', control.CONTROL_HTML)
        self.assertNotIn("function renderAttemptOverview(", control.CONTROL_HTML)
        self.assertNotIn("<th>Batch</th>", control.CONTROL_HTML)
        self.assertIn("function formatAttemptScope(", control.CONTROL_HTML)
        self.assertIn("Pending picker decisions", control.CONTROL_HTML)
        self.assertIn("function formatPendingApply(pending)", control.CONTROL_HTML)
        self.assertIn("selected ·", control.CONTROL_HTML)
        self.assertIn("Remainder overview", control.CONTROL_HTML)
        self.assertIn("function formatRemainderOverview(summary)", control.CONTROL_HTML)
        self.assertIn("review-ready", control.CONTROL_HTML)
        self.assertIn("export-copy only", control.CONTROL_HTML)
        self.assertNotIn("function formatBatchStatuses(statuses)", control.CONTROL_HTML)
        self.assertIn("Current batches", control.CONTROL_HTML)
        self.assertIn("Next batch", control.CONTROL_HTML)
        self.assertIn("Last search", control.CONTROL_HTML)
        self.assertIn("Project365 entries", control.CONTROL_HTML)
        self.assertIn("Canonical entries", control.CONTROL_HTML)
        self.assertIn("Refresh existing index metadata", control.CONTROL_HTML)
        self.assertIn("refreshPhotoIndexMetadata", control.CONTROL_HTML)
        self.assertIn('refresh_photo_index_metadata: "photoIndexMessage"', control.CONTROL_HTML)

        self.assertIn("Diarium import check", control.CONTROL_HTML)
        self.assertIn("Diarium photo import", control.CONTROL_HTML)
        self.assertIn("Migrate from other app", control.CONTROL_HTML)
        self.assertIn("Do not use Import diary", control.CONTROL_HTML)
        self.assertIn("the ZIP is not a database", control.CONTROL_HTML)
        self.assertIn("function formatDiariumImportInstruction(status)", control.CONTROL_HTML)
        self.assertIn("not a Diarium database backup", control.CONTROL_HTML)
        self.assertIn("function formatCanonicalEntries(db)", control.CONTROL_HTML)
        self.assertIn("function formatDiariumImportCheck(packageStatus, diariumLocal)", control.CONTROL_HTML)
        self.assertIn("function formatMissingMediaDates(packageStatus, localNames)", control.CONTROL_HTML)
        self.assertIn("function formatDiariumImportVerification(verification)", control.CONTROL_HTML)
        self.assertIn("function formatDiariumAttachmentNote(verification)", control.CONTROL_HTML)
        self.assertIn("diarium_import_verification", control.CONTROL_HTML)
        self.assertIn("Photos are imported.", control.CONTROL_HTML)
        self.assertIn("<span>Next batch</span>", control.CONTROL_HTML)
        self.assertIn("Build photo index", control.CONTROL_HTML)
        self.assertIn('id="photoIndexBox"', control.CONTROL_HTML)
        self.assertIn('id="indexRoots"', control.CONTROL_HTML)
        self.assertIn("function renderPhotoIndexBox(payload)", control.CONTROL_HTML)
        self.assertIn("_current_workflow_history(self.history, status)", control.__loader__.get_source(control.__name__))
        self.assertNotIn("function latestPhotoIndexWorkflowRecord(photoIndex)", control.CONTROL_HTML)
        self.assertIn('loadStatus(["workflow_overview"])', control.CONTROL_HTML)
        self.assertIn("if (initialOwnerStep) return loadStatus([initialOwnerStep]);", control.CONTROL_HTML)
        self.assertIn("recent_runs", control.CONTROL_HTML)
        self.assertIn("Index run", control.CONTROL_HTML)
        self.assertIn("indexed ·", control.CONTROL_HTML)
        self.assertIn("function buildPhotoIndex()", control.CONTROL_HTML)
        self.assertNotIn('document.getElementById("indexRoots").value = ""', control.CONTROL_HTML)
        self.assertIn('id="resetPhotoIndex"', control.CONTROL_HTML)
        self.assertIn('id="photoIndexMessage"', control.CONTROL_HTML)
        self.assertIn('role="status"', control.CONTROL_HTML)
        self.assertIn("Leave blank to refresh existing indexed folders", control.CONTROL_HTML)
        self.assertIn("function confirmPhotoIndexReplacement()", control.CONTROL_HTML)
        self.assertIn("Replace the existing photo index?", control.CONTROL_HTML)
        self.assertIn("Are you sure? This is the final confirmation.", control.CONTROL_HTML)
        self.assertIn('reset_confirmation: resetPhotoIndex ? "replace-photo-index" : ""', control.CONTROL_HTML)
        self.assertIn("function setStepMessage(step, message, state", control.CONTROL_HTML)
        self.assertIn('id="reconcileMovedPhotoIndex"', control.CONTROL_HTML)
        self.assertIn('reconcile_moves_only: reconcileMovesOnly', control.CONTROL_HTML)
        self.assertIn("Find easy matches", control.CONTROL_HTML)
        self.assertIn("function runEasyMatch()", control.CONTROL_HTML)
        self.assertIn('await runStep("match_easy_originals", {', control.CONTROL_HTML)
        self.assertIn('id="includeLowQualityMatches"', control.CONTROL_HTML)
        self.assertIn("include_low_quality_matches: includeLowQualityMatches", control.CONTROL_HTML)
        self.assertIn('id="reviewEasyMatchesButton"', control.CONTROL_HTML)
        self.assertIn('id="easyMatchMessage"', control.CONTROL_HTML)
        self.assertIn("function renderEasyMatchBox(payload)", control.CONTROL_HTML)
        self.assertIn("review_ready_entry_count", control.CONTROL_HTML)
        self.assertIn("Next: review easy matches.", control.CONTROL_HTML)
        self.assertNotIn("Search or index roots", control.CONTROL_HTML)
        self.assertIn("Indexed photos", control.CONTROL_HTML)
        self.assertIn("photo attachments", control.CONTROL_HTML)
        self.assertIn("photo files", control.CONTROL_HTML)
        self.assertIn("function openEasyMatchesInPicker()", control.CONTROL_HTML)
        self.assertIn('url.searchParams.set("status", "needs_review")', control.CONTROL_HTML)
        self.assertIn("function activePhotoIndexFolderForPicker()", control.CONTROL_HTML)
        self.assertIn('url.searchParams.set("photo_index_folder", folder)', control.CONTROL_HTML)
        self.assertIn("window.location.assign(url.toString())", control.CONTROL_HTML)
        self.assertNotIn("window.open(", control.CONTROL_HTML)
        self.assertNotIn('target="_blank"', control.CONTROL_HTML)
        self.assertIn("function pickerUrlForBatch(batch)", control.CONTROL_HTML)
        self.assertEqual(control.ControlConfig("127.0.0.1", 8766, "/picker").picker_url, "/picker")
        self.assertIn('url.searchParams.set("status", "needs_action")', control.CONTROL_HTML)
        self.assertIn('url.searchParams.append("entry_id", entryId)', control.CONTROL_HTML)
        self.assertIn('url.searchParams.append("entry_date", entryDate)', control.CONTROL_HTML)
        self.assertIn('data-step-result="match_easy_originals"', control.CONTROL_HTML)
        self.assertIn("function renderWorkflowCard(", control.CONTROL_HTML)
        self.assertIn("function applyInitialWorkflowExpansion()", control.CONTROL_HTML)
        self.assertIn("applyInitialWorkflowExpansion();", control.CONTROL_HTML)
        self.assertIn('data-step-toggle="match_easy_originals"', control.CONTROL_HTML)
        self.assertIn("function toggleWorkflowStep(step)", control.CONTROL_HTML)
        self.assertIn('const initialStep = new URLSearchParams(window.location.search).get("step") || ""', control.CONTROL_HTML)
        self.assertIn('const manuallyExpandedSteps = new Set(initialOwnerStep ? [initialOwnerStep] : [])', control.CONTROL_HTML)
        self.assertIn("function statusUrlFor(steps, options = {})", control.CONTROL_HTML)
        self.assertIn('params.set("include_broad_monthly_coverage", "1")', control.CONTROL_HTML)
        self.assertIn('loadStatus(["workflow_overview"])', control.CONTROL_HTML)
        self.assertIn("if (initialOwnerStep) return loadStatus([initialOwnerStep]);", control.CONTROL_HTML)
        self.assertIn("manuallyExpandedSteps.add(ownerStep(step))", control.CONTROL_HTML)
        self.assertIn("loadStatus(expandedStatusSteps()).catch", control.CONTROL_HTML)
        self.assertNotIn('Showing priority archive results', control.CONTROL_HTML)
        self.assertIn("No archive anomalies.", control.CONTROL_HTML)
        self.assertIn('const showArchives = ownerStep(record.step || "") === "import_zips"', control.CONTROL_HTML)
        self.assertIn("zero_change_message", control.CONTROL_HTML)
        self.assertIn(".workflow-step:not(.is-expanded) .workflow-step-body", control.CONTROL_HTML)
        self.assertIn('id="statusLoadError"', control.CONTROL_HTML)
        self.assertIn("function setStatusLoadError(message)", control.CONTROL_HTML)
        self.assertIn("Object.assign({step: step}, extra || {})", control.CONTROL_HTML)
        self.assertIn("function replaceAllText(value, search, replacement)", control.CONTROL_HTML)
        self.assertNotIn("CSS.escape", control.CONTROL_HTML)
        self.assertNotIn(".replaceAll", control.CONTROL_HTML)
        self.assertNotIn("...extra", control.CONTROL_HTML)
        self.assertIn("function waitForJob(jobId)", control.CONTROL_HTML)
        self.assertIn("function updateProgress(job)", control.CONTROL_HTML)
        self.assertIn("formatElapsed", control.CONTROL_HTML)
        self.assertIn(".button.is-running", control.CONTROL_HTML)
        self.assertIn("function markButtonRunning(button, runningNow)", control.CONTROL_HTML)
        self.assertIn("markButtonRunning(trigger, true)", control.CONTROL_HTML)
        self.assertIn('document.querySelectorAll(".button.is-running")', control.CONTROL_HTML)
        self.assertIn('other.removeAttribute("aria-busy")', control.CONTROL_HTML)
        self.assertIn("aria-busy", control.CONTROL_HTML)
        self.assertIn('id="cropConfirmationOverview"', control.CONTROL_HTML)
        self.assertIn("function renderCropConfirmationBox(payload)", control.CONTROL_HTML)
        self.assertIn("formatCropConfirmationStatus", control.CONTROL_HTML)
        self.assertIn("With crop information", control.CONTROL_HTML)
        self.assertIn("Saved estimates", control.CONTROL_HTML)
        self.assertIn("User saved", control.CONTROL_HTML)
        self.assertIn("Queued without crop", control.CONTROL_HTML)

    def test_control_html_expands_only_requested_crop_confirmation_step(self) -> None:
        html = control._control_html("/picker", initial_step="crop_confirmation")

        self.assertIn('<section class="panel workflow-step" data-step="import_zips">', html)
        self.assertIn(
            'data-step-toggle="import_zips" onclick="toggleWorkflowStep(\'import_zips\')" aria-expanded="false">Open</button>',
            html,
        )
        self.assertIn('<section class="panel workflow-step is-expanded" data-step="crop_confirmation">', html)
        self.assertIn(
            'data-step-toggle="crop_confirmation" onclick="toggleWorkflowStep(\'crop_confirmation\')" aria-expanded="true">Collapse</button>',
            html,
        )

    def test_control_html_defaults_to_all_workflow_steps_collapsed(self) -> None:
        html = control._control_html("/picker")

        self.assertNotIn("workflow-step is-expanded", html)
        self.assertIn(
            'data-step-toggle="import_zips" onclick="toggleWorkflowStep(\'import_zips\')" aria-expanded="false">Open</button>',
            html,
        )

    def test_diary_enrichment_follows_working_copies_and_face_tagging(self) -> None:
        original_review = control.CONTROL_HTML.index("Original-photo review")
        crop_confirmation = control.CONTROL_HTML.index("Crop confirmation")
        diary_enrichment = control.CONTROL_HTML.index("Diary enrichment")
        working_copies = control.CONTROL_HTML.index("Working photo copies")
        face_tagging = control.CONTROL_HTML.index("People / face tagging")

        self.assertLess(original_review, crop_confirmation)
        self.assertLess(crop_confirmation, working_copies)
        self.assertLess(working_copies, face_tagging)
        self.assertLess(face_tagging, diary_enrichment)
        self.assertIn("Open crop confirmation", control.CONTROL_HTML)
        self.assertIn("workingCopyStartDate", control.CONTROL_HTML)
        self.assertIn("workingCopyEndDate", control.CONTROL_HTML)
        self.assertIn("forceWorkingCopies", control.CONTROL_HTML)
        self.assertIn("function runWorkingCopies()", control.CONTROL_HTML)
        self.assertIn("function refreshWorkingCopyReadiness()", control.CONTROL_HTML)
        self.assertIn("/api/working-copy-readiness", control.CONTROL_HTML)
        self.assertIn("Unchanged copies are skipped unless forced", control.CONTROL_HTML)
        self.assertIn("Import digiKam suggestions", control.CONTROL_HTML)
        self.assertIn("digikamXmpRoots", control.CONTROL_HTML)
        self.assertIn("digikamSuggestionsCsv", control.CONTROL_HTML)
        self.assertIn("function importDigiKamPeople()", control.CONTROL_HTML)

    def test_control_scopes_working_photo_copy_command(self) -> None:
        commands = control._commands_for_step(
            "generate_derivatives",
            {
                "start_date": "1998-04-12",
                "end_date": "1998-04-13",
                "force": True,
            },
        )

        command = commands[0]
        self.assertIn("project365_media_derivatives.py", command)
        self.assertIn("--start-date", command)
        self.assertIn("1998-04-12", command)
        self.assertIn("--end-date", command)
        self.assertIn("1998-04-13", command)
        self.assertIn("--force", command)

    def test_working_copy_readiness_status_accepts_date_scope(self) -> None:
        summary = SimpleNamespace(
            source_count=2,
            ready_count=1,
            not_ready_count=1,
            current_count=1,
            needs_update_count=0,
        )
        with mock.patch.object(control, "derivative_readiness_summary", return_value=summary) as readiness:
            payload = control._working_copy_readiness_status(
                start_date="1998-04-12",
                end_date="1998-04-13",
            )

        readiness.assert_called_once_with(
            control.CANONICAL_ROOT,
            start_date="1998-04-12",
            end_date="1998-04-13",
        )
        self.assertEqual(payload["database"]["working_copy_current_count"], 1)
        self.assertEqual(payload["scope"], {"start_date": "1998-04-12", "end_date": "1998-04-13"})

    def test_control_builds_digikam_people_import_command(self) -> None:
        commands = control._commands_for_step(
            "import_digikam_people",
            {
                "xmp_roots": ["/tmp/xmp-one", "/tmp/xmp-two"],
                "suggestions_csv": "/tmp/people.csv",
            },
        )

        command = commands[0]
        self.assertIn("project365_digikam_people_importer.py", command)
        self.assertIn("--xmp-root", command)
        self.assertIn("/tmp/xmp-one", command)
        self.assertIn("/tmp/xmp-two", command)
        self.assertIn("--suggestions-csv", command)
        self.assertIn("/tmp/people.csv", command)

    def test_control_embeds_picker_under_same_port(self) -> None:
        html = control._embedded_picker_html()

        self.assertIn('fetchJson("/picker/api/summary")', html)
        self.assertIn('fetchJson("/picker/api/batches")', html)
        self.assertIn('fetchJson("/picker/api/apply-decisions"', html)
        self.assertIn('confirm_apply_decisions: "apply-reviewed-decisions"', html)
        self.assertIn('fetchJson(`/picker/api/entries?', html)
        self.assertIn('fetchJson(`/picker/api/entry/', html)
        self.assertIn('"?include_database=1"', html)
        self.assertIn('fetchJson("/picker/api/choose-folder")', html)
        self.assertIn('fetchJson(`/picker/api/crawl/', html)
        self.assertIn('fetchJson("/picker/api/associated-date-choices"', html)
        self.assertIn('fetchJson("/picker/api/expand-date-range"', html)
        self.assertIn('fetchJson("/picker/api/expand-default-date-range"', html)
        self.assertIn('fetchJson("/picker/api/search-index-date-range"', html)
        self.assertNotIn('fetchJson("/picker/api/crop-estimate-batch"', html)
        self.assertNotIn('fetchJson(`/picker/api/crop-estimate-batch/', html)
        self.assertIn('src="/picker/image/${encodeURIComponent(entry.source_token)}?max=96"', html)
        self.assertIn("`/picker/image/${entry.source_token}?max=1280`", html)
        self.assertNotIn('fetchJson("/api/', html)
        self.assertNotIn('fetchJson(`/api/', html)
        self.assertNotIn('src="/image/${', html)
        self.assertNotIn("`/image/${", html)

    def test_control_rejects_picker_apply_decisions_without_confirmation_token(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/apply-decisions",
                data=json.dumps({}).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        picker_state.apply_decisions.assert_not_called()

    def test_control_status_accepts_plural_steps_query(self) -> None:
        state = mock.Mock()
        state.status.return_value = {"ok": True}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/status?steps=broad_visual_match&steps=rough_visual_match",
                timeout=5,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result, {"ok": True})
        state.status.assert_called_once_with(
            ["broad_visual_match", "rough_visual_match"],
            include_broad_monthly_coverage=False,
        )

    def test_control_routes_picker_apply_decisions_with_confirmation_token(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.apply_decisions.return_value = {"applied_count": 3}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/apply-decisions",
                data=json.dumps(
                    {"confirm_apply_decisions": control.original_picker.APPLY_DECISIONS_CONFIRM_TOKEN}
                ).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["applied_count"], 3)
        picker_state.apply_decisions.assert_called_once_with()

    def test_control_routes_picker_commit_entry(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.commit_entry_decision.return_value = {"applied_count": 1}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/commit-entry",
                data=json.dumps({"entry_id": "project365:1998-04-12"}).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["applied_count"], 1)
        picker_state.commit_entry_decision.assert_called_once_with("project365:1998-04-12")

    def test_control_routes_picker_associated_photo_decision_and_date_choices(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.associated_date_choices.return_value = {
            "choices": [{"date": "2015-01-02", "source": "filename"}]
        }
        picker_state.save_decision.return_value = {"entry_id": "project365:2015-01-02"}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            choices_payload = {
                "entry_id": "project365:2015-01-02",
                "candidate_path": "/tmp/original.jpg",
            }
            choices_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/associated-date-choices",
                data=json.dumps(choices_payload).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(choices_request, timeout=5) as response:
                choices_result = json.loads(response.read().decode("utf-8"))

            decision_payload = {
                "entry_id": "project365:2015-01-02",
                "candidate_path": "/tmp/original.jpg",
                "decision": "external_original_associated_photo",
                "notes": "associated only",
                "associated_entry_date": "2015-01-03",
                "associated_date_source": "filename",
            }
            decision_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/decision",
                data=json.dumps(decision_payload).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(decision_request, timeout=5) as response:
                decision_result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(
            choices_result,
            {"choices": [{"date": "2015-01-02", "source": "filename"}]},
        )
        self.assertEqual(decision_result, {"entry_id": "project365:2015-01-02"})
        picker_state.associated_date_choices.assert_called_once_with(
            entry_id="project365:2015-01-02",
            candidate_path="/tmp/original.jpg",
        )
        picker_state.save_decision.assert_called_once_with(
            entry_id="project365:2015-01-02",
            candidate_path="/tmp/original.jpg",
            decision="external_original_associated_photo",
            notes="associated only",
            include_candidates=False,
            associated_entry_date="2015-01-03",
            associated_date_source="filename",
        )

    def test_control_embeds_crop_under_same_port(self) -> None:
        html = control._embedded_crop_html()

        self.assertIn('href="/?step=crop_confirmation"', html)
        self.assertIn("fetchJson(cropEntriesUrl(cropFilter))", html)
        self.assertIn("`/crop/api/crop-entries?${params.toString()}`", html)
        self.assertIn('params.set("start_date", state.cropStartDate);', html)
        self.assertIn('fetchJson(`/crop/api/crop-entry/', html)
        self.assertIn('fetchJson("/crop/api/crop"', html)
        self.assertIn('fetchJson("/crop/api/crop-reset"', html)
        self.assertIn('fetchJson("/crop/api/crop-commit"', html)
        self.assertIn('fetchJson("/crop/api/crop-reject-original"', html)
        self.assertIn('fetchJson("/crop/api/crop-suggestion"', html)
        self.assertNotIn('fetchJson("/crop/api/crop-estimate-batch"', html)
        self.assertNotIn('fetchJson(`/crop/api/crop-estimate-batch/', html)
        self.assertNotIn('id="estimateCropBatchButton"', html)
        self.assertIn('src="/picker/image/${encodeURIComponent(entry.source_token)}"', html)
        self.assertIn("`/picker/image/${entry.source_token}`", html)
        self.assertIn("`/picker/image/${candidate.token}`", html)
        self.assertNotIn('fetchJson("/api/', html)
        self.assertNotIn('fetchJson(`/api/', html)
        self.assertNotIn('src="/image/${', html)
        self.assertNotIn("`/image/${", html)

    def test_control_routes_crop_save(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.save_crop.return_value = {"entry_id": "project365:1998-04-11"}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = {
                "entry_id": "project365:1998-04-11",
                "candidate_path": "/tmp/original.jpg",
                "crop": {
                    "x": 1,
                    "y": 2,
                    "size": 3,
                    "candidate_width": 10,
                    "candidate_height": 12,
                },
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/crop/api/crop",
                data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["entry_id"], "project365:1998-04-11")
        picker_state.save_crop.assert_called_once_with(
            entry_id="project365:1998-04-11",
            candidate_path="/tmp/original.jpg",
            crop={
                "x": 1,
                "y": 2,
                "size": 3,
                "candidate_width": 10,
                "candidate_height": 12,
            },
            source="manual",
        )

    def test_control_routes_crop_reset(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.reset_crop.return_value = {"entry_id": "project365:1998-04-11"}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = {
                "entry_id": "project365:1998-04-11",
                "candidate_path": "/tmp/original.jpg",
                "preserve_estimate": True,
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/crop/api/crop-reset",
                data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["entry_id"], "project365:1998-04-11")
        picker_state.reset_crop.assert_called_once_with(
            entry_id="project365:1998-04-11",
            candidate_path="/tmp/original.jpg",
            preserve_estimate=True,
        )

    def test_control_routes_crop_reject_original(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.reject_crop_original.return_value = {
            "entry": {"entry_id": "project365:1998-04-11"},
            "rejected_count": 1,
        }
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = {
                "entry_id": "project365:1998-04-11",
                "candidate_path": "/tmp/original.jpg",
                "notes": "wrong original",
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/crop/api/crop-reject-original",
                data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["rejected_count"], 1)
        picker_state.reject_crop_original.assert_called_once_with(
            entry_id="project365:1998-04-11",
            candidate_path="/tmp/original.jpg",
            notes="wrong original",
        )

    def test_control_routes_crop_entries(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.crop_entries.return_value = [
            {"entry_id": "project365:1998-04-11"},
            {"entry_id": "project365:1998-04-12"},
        ]
        picker_state.pending_crop_commits.return_value = {"pending_count": 2}
        picker_state.latest_crop_estimate_job.return_value = {
            "id": "job-1",
            "status": "running",
            "errors": [
                {
                    "entry_id": "project365:1998-04-11",
                    "candidate_path": "/tmp/example.heic",
                    "error": "Cannot load image for crop estimation: /tmp/example.heic; raw command detail",
                }
            ],
        }
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/crop/api/crop-entries?crop_filter=missing&limit=1&entry_date=1998-04-12&start_date=1998-04-01&end_date=1998-04-30",
                timeout=5,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["entries"], [{"entry_id": "project365:1998-04-11"}])
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["pending_crop_commits"], {"pending_count": 2})
        self.assertEqual(result["crop_estimate_batch"]["id"], "job-1")
        self.assertEqual(result["crop_estimate_batch"]["error_count"], 1)
        self.assertNotIn("errors", result["crop_estimate_batch"])
        self.assertEqual(
            result["crop_estimate_batch"]["first_error"],
            {
                "entry_id": "project365:1998-04-11",
                "candidate_filename": "example.heic",
                "message": "could not load image for crop estimation",
            },
        )
        picker_state.crop_entries.assert_called_once_with(
            crop_filter="missing",
            entry_dates={"1998-04-12"},
            start_date="1998-04-01",
            end_date="1998-04-30",
        )
        picker_state.pending_crop_commits.assert_called_once_with()
        picker_state.latest_crop_estimate_job.assert_called_once_with()

    def test_control_routes_crop_estimate_batch_from_control_picker_and_crop_pages(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.start_crop_estimate_batch.return_value = {"id": "job-1", "status": "queued"}
        picker_state.crop_estimate_job.return_value = {"id": "job-1", "status": "pass"}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in (
                "api/crop-estimate-batch",
                "picker/api/crop-estimate-batch",
                "crop/api/crop-estimate-batch",
            ):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/{path}",
                    data=json.dumps({"apply_estimates": True}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    start_result = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/{path}/job-1",
                    timeout=5,
                ) as response:
                    poll_result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(start_result, {"id": "job-1", "status": "queued"})
                self.assertEqual(poll_result, {"id": "job-1", "status": "pass"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(picker_state.start_crop_estimate_batch.call_count, 3)
        picker_state.start_crop_estimate_batch.assert_has_calls(
            [
                mock.call(apply_estimates=True, refresh_saved_estimates=False, target_extensions=[]),
                mock.call(apply_estimates=True, refresh_saved_estimates=False, target_extensions=[]),
                mock.call(apply_estimates=True, refresh_saved_estimates=False, target_extensions=[]),
            ]
        )
        self.assertEqual(picker_state.crop_estimate_job.call_count, 3)
        picker_state.crop_estimate_job.assert_called_with("job-1")

    def test_control_status_includes_crop_confirmation_counts(self) -> None:
        state = control.ControlState()
        picker_state = mock.Mock()
        picker_state.crop_entries.return_value = [
            {"entry_id": "project365:1998-04-11", "crop_has_crop": True, "crop_source": "estimated"},
            {"entry_id": "project365:1998-04-12", "crop_has_crop": True, "crop_source": "manual"},
            {"entry_id": "project365:1998-04-12", "crop_has_crop": False},
            {"entry_id": "project365:1998-04-13", "crop_has_crop": False},
        ]
        picker_state.pending_crop_commits.return_value = {"pending_count": 5}
        picker_state.latest_crop_estimate_job.return_value = {}
        state._picker_state = picker_state

        status = state.crop_confirmation_status()

        self.assertEqual(
            status,
            {
                "exists": True,
                "total_count": 4,
                "with_crop_count": 2,
                "estimated_crop_count": 1,
                "confirmed_crop_count": 1,
                "queued_count": 2,
                "pending_commit_count": 5,
                "crop_estimate_batch": {},
            },
        )
        picker_state.crop_entries.assert_called_once_with(crop_filter="all")
        picker_state.pending_crop_commits.assert_called_once_with()
        picker_state.latest_crop_estimate_job.assert_called_once_with()

    def test_scoped_status_includes_active_crop_estimate_batch(self) -> None:
        state = control.ControlState()
        picker_state = mock.Mock()
        picker_state.crop_estimate_jobs.return_value = [
            {
                "id": "crop-job",
                "status": "running",
                "target_count": 1059,
                "processed_count": 50,
                "estimated_count": 50,
                "apply_estimates": True,
                "started_at": "2026-09-10T00:00:00+00:00",
                "errors": [
                    {
                        "entry_id": "project365:1998-04-11",
                        "candidate_path": "/tmp/example.heic",
                        "error": "Cannot load image for crop estimation: /tmp/example.heic; raw command detail",
                    }
                ],
            }
        ]
        picker_state.crop_entries.return_value = []
        picker_state.pending_crop_commits.return_value = {"pending_count": 0}
        picker_state.latest_crop_estimate_job.return_value = picker_state.crop_estimate_jobs.return_value[0]
        state._picker_state = picker_state

        status = state.status(["crop_confirmation"])

        self.assertEqual(len(status["active_jobs"]), 1)
        self.assertEqual(status["active_jobs"][0]["step"], "crop_confirmation")
        self.assertEqual(status["active_jobs"][0]["kind"], "crop_estimate_batch")
        self.assertFalse(status["active_jobs"][0]["cancellable"])
        self.assertNotIn("errors", status["active_jobs"][0])
        self.assertEqual(status["active_jobs"][0]["error_count"], 1)
        self.assertEqual(status["crop_confirmation"]["crop_estimate_batch"]["id"], "crop-job")
        self.assertNotIn("errors", status["crop_confirmation"]["crop_estimate_batch"])
        picker_state.crop_estimate_jobs.assert_called_once_with(active_only=True)

    def test_control_routes_crop_commit(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.commit_staged_crops.return_value = {"saved_count": 2, "remaining_count": 0}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/crop/api/crop-commit",
                data=b"{}",
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["saved_count"], 2)
        picker_state.commit_staged_crops.assert_called_once_with()

    def test_control_routes_crop_entry_detail_to_crop_endpoint(self) -> None:
        state = mock.Mock()
        picker_state = state.picker_state.return_value
        picker_state.crop_entry_detail.return_value = {
            "entry_id": "project365:1998-04-11",
            "candidates": [{"selected": True}],
        }
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/crop/api/entry/project365%3A1998-04-11",
                timeout=5,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["entry_id"], "project365:1998-04-11")
        picker_state.crop_entry_detail.assert_called_once_with(
            "project365:1998-04-11",
            mark_estimated_viewed=False,
        )
        picker_state.entry_detail.assert_not_called()

    def test_control_routes_picker_date_range_expansion(self) -> None:
        state = mock.Mock()
        state.active_photo_index_folder.return_value = "/Volumes/Archive Photos/Project 365"
        picker_state = state.picker_state.return_value
        picker_state.expand_date_range.return_value = {
            "entry_id": "project365:1998-04-11",
            "days": 15,
            "added_count": 5,
        }
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/expand-date-range",
                data=json.dumps({"entry_id": "project365:1998-04-11", "days": 15}).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["added_count"], 5)
        picker_state.expand_date_range.assert_called_once_with(
            entry_id="project365:1998-04-11",
            days=15,
            photo_index_folder="/Volumes/Archive Photos/Project 365",
            search_whole_index=False,
            whole_index_filename_only=False,
            filename_dates_only=False,
            include_modified_dates=False,
        )

    def test_control_state_defaults_active_photo_index_folder_to_project_originals(self) -> None:
        state = control.ControlState()

        with mock.patch.object(
            control.original_picker,
            "project_originals_source_folder",
            return_value="/Archive/Source Data/Original Photos matching Project365 Entries",
        ):
            folder = state.active_photo_index_folder()

        self.assertEqual(folder, "/Archive/Source Data/Original Photos matching Project365 Entries")

    def test_control_routes_picker_reject_all_with_active_photo_index_folder(self) -> None:
        state = mock.Mock()
        state.active_photo_index_folder.return_value = "/Volumes/Archive Photos/Project 365"
        picker_state = state.picker_state.return_value
        picker_state.reject_all_candidates.return_value = {
            "rejected_count": 3,
            "entry": {"entry_id": "project365:1998-04-11"},
        }
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/reject-all",
                data=json.dumps(
                    {
                        "entry_id": "project365:1998-04-11",
                        "notes": "Rejected with Reject all.",
                    }
                ).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["rejected_count"], 3)
        picker_state.reject_all_candidates.assert_called_once_with(
            entry_id="project365:1998-04-11",
            notes="Rejected with Reject all.",
            include_candidates=False,
            photo_index_folder="/Volumes/Archive Photos/Project 365",
        )

    def test_control_routes_picker_default_date_expansion_with_whole_index_scope(self) -> None:
        state = mock.Mock()
        state.active_photo_index_folder.return_value = "/Volumes/Archive Photos/Project 365"
        picker_state = state.picker_state.return_value
        picker_state.expand_default_date_range.return_value = {
            "entry_id": "project365:1998-04-11",
            "search_whole_index": True,
            "added_count": 7,
        }
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/expand-default-date-range",
                data=json.dumps(
                    {
                        "entry_id": "project365:1998-04-11",
                        "search_whole_index": True,
                        "filename_dates_only": True,
                        "include_modified_dates": True,
                    }
                ).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["added_count"], 7)
        picker_state.expand_default_date_range.assert_called_once_with(
            entry_id="project365:1998-04-11",
            photo_index_folder="/Volumes/Archive Photos/Project 365",
            search_whole_index=True,
            whole_index_filename_only=False,
            filename_dates_only=True,
            include_modified_dates=True,
        )

    def test_control_routes_picker_custom_date_search_with_whole_index_scope(self) -> None:
        state = mock.Mock()
        state.active_photo_index_folder.return_value = "/Volumes/Archive Photos/Project 365"
        picker_state = state.picker_state.return_value
        picker_state.search_index_date_range.return_value = {
            "entry_id": "project365:1998-04-11",
            "search_whole_index": True,
            "added_count": 9,
        }
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/picker/api/search-index-date-range",
                data=json.dumps(
                    {
                        "entry_id": "project365:1998-04-11",
                        "start_date": "2010-08-15",
                        "end_date": "2010-08-18",
                        "search_whole_index": True,
                        "filename_dates_only": True,
                        "include_modified_dates": True,
                    }
                ).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["added_count"], 9)
        picker_state.search_index_date_range.assert_called_once_with(
            entry_id="project365:1998-04-11",
            start_date="2010-08-15",
            end_date="2010-08-18",
            search_whole_index=True,
            photo_index_folder="/Volumes/Archive Photos/Project 365",
            whole_index_filename_only=False,
            filename_dates_only=True,
            include_modified_dates=True,
        )

    def test_control_state_runs_step_as_pollable_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(control, "CONTROL_RUN_HISTORY", Path(temp_dir) / "history.jsonl"):
                state = control.ControlState()
                with mock.patch(
                    "project365_control_app._commands_for_step",
                    return_value=[[sys.executable, "-c", "print('job complete')"]],
                ):
                    job = state.start_step("import_zips", {})
                    for _ in range(50):
                        job = state.job_status(job["job_id"])
                        if job["status"] in {"pass", "fail"}:
                            break
                        time.sleep(0.1)

        self.assertEqual(job["status"], "pass")
        self.assertIn("job complete", job["outputs"][0]["output"])
        self.assertEqual(state.history[-1]["status"], "pass")

    def test_control_state_cancels_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(control, "CONTROL_RUN_HISTORY", Path(temp_dir) / "history.jsonl"):
                state = control.ControlState()
                with mock.patch(
                    "project365_control_app._commands_for_step",
                    return_value=[[sys.executable, "-c", "import time; time.sleep(30)"]],
                ):
                    job = state.start_step("match_easy_originals", {})
                    for _ in range(50):
                        job = state.job_status(job["job_id"])
                        if job["status"] == "running":
                            break
                        time.sleep(0.1)
                    cancelled = state.cancel_job(job["job_id"])
                    self.assertIn(cancelled["status"], {"cancelling", "cancelled"})
                    for _ in range(50):
                        job = state.job_status(job["job_id"])
                        if job["status"] == "cancelled":
                            break
                        time.sleep(0.1)

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(state.history[-1]["status"], "cancelled")

    def test_control_html_uses_native_finder_folder_picker(self) -> None:
        self.assertIn("Choose folder", control.CONTROL_HTML)
        self.assertIn('chooseFolderInFinder(\'indexRoots\')', control.CONTROL_HTML)
        self.assertNotIn('chooseFolderInFinder(\'targetedSearchRoots\')', control.CONTROL_HTML)
        self.assertIn('function chooseFolderInFinder(targetInputId = "indexRoots", mode = "append")', control.CONTROL_HTML)
        self.assertIn('function setFolderChooserMessage(targetInputId, message, state = "")', control.CONTROL_HTML)
        self.assertIn("function promptForFolderPath(targetInputId, mode)", control.CONTROL_HTML)
        self.assertIn('fetchJson("/api/validate-folder-path"', control.CONTROL_HTML)
        self.assertIn("Paste the folder path here", control.CONTROL_HTML)
        self.assertIn("paste a folder path directly into the field", control.CONTROL_HTML)
        self.assertIn('targetInputId === "indexRoots"', control.CONTROL_HTML)
        self.assertIn('"photoIndexMessage"', control.CONTROL_HTML)
        self.assertNotIn('fetchJson("/api/choose-folder")', control.CONTROL_HTML)
        self.assertNotIn("Browse here", control.CONTROL_HTML)
        self.assertNotIn('id="folderBrowser"', control.CONTROL_HTML)
        self.assertNotIn("function showFolderBrowser()", control.CONTROL_HTML)

    def test_completed_run_progress_is_static(self) -> None:
        self.assertIn(".run-progress.status-pass .spinner", control.CONTROL_HTML)
        self.assertIn(".run-progress.status-pass .progress-bar", control.CONTROL_HTML)
        self.assertIn("function formatActiveWorkflowResult(job)", control.CONTROL_HTML)
        self.assertIn("formatWorkflowResult(latest, Boolean(active))", control.CONTROL_HTML)

    def test_active_run_progress_keeps_detail_separate_from_working_indicator(self) -> None:
        self.assertIn("run-progress-detail", control.CONTROL_HTML)
        self.assertIn("run-progress-text", control.CONTROL_HTML)
        self.assertIn("run-progress-live", control.CONTROL_HTML)
        self.assertIn("if (activeJobId) return;", control.CONTROL_HTML)
        self.assertIn("const liveLabel =", control.CONTROL_HTML)
        self.assertIn("@keyframes live-pulse", control.CONTROL_HTML)
        self.assertIn("function genericRunningProgressDetail(job)", control.CONTROL_HTML)
        self.assertIn("Still running · no detailed progress output yet", control.CONTROL_HTML)

    def test_working_copy_progress_parses_live_totals(self) -> None:
        self.assertIn("function latestWorkingCopyProgress(job)", control.CONTROL_HTML)
        self.assertIn('line.match(/^Progress:', control.CONTROL_HTML)
        self.assertIn('`${progress.done}/${progress.total} sources`', control.CONTROL_HTML)
        self.assertIn("return percentComplete(progress.done, progress.total)", control.CONTROL_HTML)

    def test_broad_visual_status_shows_date_coverage(self) -> None:
        self.assertIn("<span>Date coverage</span>", control.CONTROL_HTML)
        self.assertIn("function formatBroadDateCoverage(latestIndex)", control.CONTROL_HTML)
        self.assertIn("date_coverage_json", control.CONTROL_HTML)
        self.assertIn("Monthly fingerprint coverage", control.CONTROL_HTML)
        self.assertIn("function renderBroadMonthlyCoverage(rows)", control.CONTROL_HTML)
        self.assertIn("coverage-table-head", control.CONTROL_HTML)
        self.assertIn('class="button primary small"', control.CONTROL_HTML)
        self.assertIn('id="broadCoverageCheckStatus"', control.CONTROL_HTML)
        self.assertIn("coverage-check-status", control.CONTROL_HTML)
        self.assertIn("onclick=\"checkBroadVisualCoverage()\"", control.CONTROL_HTML)
        self.assertIn("missing_original_targets", control.CONTROL_HTML)
        self.assertIn("low-coverage", control.CONTROL_HTML)
        self.assertIn("function parseJsonObject(value)", control.CONTROL_HTML)
        self.assertIn("complete_start_date", control.CONTROL_HTML)
        self.assertIn("current_checked", control.CONTROL_HTML)

    def test_broad_visual_card_hides_previous_review_controls(self) -> None:
        self.assertIn('status["review_runs"] = []', control.__loader__.get_source(control.__name__))
        self.assertIn('status["review_ready_run"] =', control.__loader__.get_source(control.__name__))
        self.assertIn("review_ready_summary", control.__loader__.get_source(control.__name__))
        self.assertIn("Review readiness", control.CONTROL_HTML)
        self.assertIn("function formatBroadReviewReadiness", control.CONTROL_HTML)
        self.assertNotIn("already in Original-photo review", control.CONTROL_HTML)
        self.assertNotIn("function renderBroadReviewRunControls(runs)", control.CONTROL_HTML)
        self.assertNotIn("${renderBroadReviewRunControls(broad.review_runs || [])}", control.CONTROL_HTML)
        self.assertNotIn("Previous reviews", control.CONTROL_HTML)
        self.assertNotIn("clearBroadReviewRunFromControl", control.CONTROL_HTML)
        self.assertNotIn("clearAllBroadReviewRunsFromControl", control.CONTROL_HTML)
        self.assertNotIn("/broad/api/clear-review-run", control.CONTROL_HTML)
        self.assertNotIn("/broad/api/clear-all-review-runs", control.CONTROL_HTML)

    def test_broad_visual_search_preflights_selected_coverage(self) -> None:
        self.assertIn("onclick=\"checkBroadVisualCoverage()\"", control.CONTROL_HTML)
        self.assertIn("async function checkBroadVisualCoverage()", control.CONTROL_HTML)
        self.assertIn('setBroadCoverageStatus("Checking fingerprint coverage...", "running")', control.CONTROL_HTML)
        self.assertIn("function setBroadCoverageStatus(message, state = \"\")", control.CONTROL_HTML)
        self.assertIn("Checking fingerprint coverage before search", control.CONTROL_HTML)
        self.assertIn("includeBroadMonthlyCoverage: true", control.CONTROL_HTML)
        self.assertIn("function broadCoveragePreview(settings, status)", control.CONTROL_HTML)
        self.assertIn("const coverage = await checkBroadVisualCoverage();", control.CONTROL_HTML)
        self.assertIn('await runStep("broad_visual_match", settings, trigger)', control.CONTROL_HTML)
        self.assertIn("Could not start broad visual search", control.CONTROL_HTML)
        self.assertIn("if (coverage.blocking) return;", control.CONTROL_HTML)
        self.assertIn("Fingerprint coverage too low", control.CONTROL_HTML)
        self.assertIn("run Build fingerprints with Fingerprint already confirmed originals unchecked", control.CONTROL_HTML)
        self.assertIn("missing-original target(s)", control.CONTROL_HTML)
        self.assertIn("function broadCandidateMonths(settings)", control.CONTROL_HTML)
        self.assertIn("coverage_warning", control.__loader__.get_source(control.__name__))

    def test_broad_visual_status_shows_current_candidate_heartbeat(self) -> None:
        self.assertIn("<span>Current item</span>", control.CONTROL_HTML)
        self.assertIn("function formatBroadCurrentItem(latestIndex)", control.CONTROL_HTML)
        self.assertIn("current_candidate_index", control.CONTROL_HTML)
        self.assertIn("current_candidate_byte_size", control.CONTROL_HTML)
        self.assertIn("current_phase", control.CONTROL_HTML)
        self.assertIn("function formatBytes(value)", control.CONTROL_HTML)

    def test_broad_visual_status_shows_match_progress_heartbeat(self) -> None:
        self.assertIn("<span>Search heartbeat</span>", control.CONTROL_HTML)
        self.assertIn("function formatBroadSearchHeartbeat(latestRun, active)", control.CONTROL_HTML)
        self.assertIn("function broadVisualRunCompleteMessage(latestRun, reviewReadyRun = {})", control.CONTROL_HTML)
        self.assertIn("Broad visual search found no entries in the selected scope", control.CONTROL_HTML)
        self.assertIn('setStepMessage("broad_visual_match", broadVisualRunCompleteMessage(latestRun, broad.review_ready_run || {}))', control.CONTROL_HTML)
        self.assertIn("function broadVisualIndexProgressDetail(job)", control.CONTROL_HTML)
        self.assertIn("function broadVisualMatchProgressDetail(job)", control.CONTROL_HTML)
        self.assertIn("function roughPrefilterProgressDetail(job)", control.CONTROL_HTML)
        self.assertIn("function roughVisualProgressDetail(job)", control.CONTROL_HTML)
        self.assertIn("job.broad_visual_index_run", control.CONTROL_HTML)
        self.assertIn("job.broad_visual_run", control.CONTROL_HTML)
        self.assertIn("job.rough_prefilter_run", control.CONTROL_HTML)
        self.assertIn("job.rough_visual_run", control.CONTROL_HTML)
        self.assertIn("Stop now:", control.CONTROL_HTML)
        self.assertIn("function jobProgressPercent(job)", control.CONTROL_HTML)
        self.assertIn("function formatHeartbeatAge(value)", control.CONTROL_HTML)
        self.assertIn("return payload;", control.CONTROL_HTML)
        self.assertIn("processed_target_count", control.CONTROL_HTML)
        self.assertIn("current_target_index", control.CONTROL_HTML)
        self.assertIn("current_candidate_count", control.CONTROL_HTML)
        self.assertIn("latestRun.heartbeat_at", control.CONTROL_HTML)

    def test_control_panel_timestamps_use_project365_datetime_format(self) -> None:
        self.assertIn("function formatShortDateTime(value)", control.CONTROL_HTML)
        self.assertIn("function pad2(value)", control.CONTROL_HTML)
        self.assertIn('].join("-") + " " + [', control.CONTROL_HTML)
        self.assertNotIn("return parsed.toLocaleString();", control.CONTROL_HTML)
        self.assertIn("formatShortDateTime(attempt.finished_at)", control.CONTROL_HTML)

    def test_broad_visual_history_filters_stale_index_metrics_from_match_rows(self) -> None:
        self.assertIn("function workflowHistoryMetrics(record)", control.CONTROL_HTML)
        self.assertIn('if (step === "broad_visual_match")', control.CONTROL_HTML)
        self.assertIn('"Unresolved candidates saved"', control.CONTROL_HTML)
        self.assertIn('{ label: "Saved candidate rows", value: metric.value }', control.CONTROL_HTML)

    def test_broad_visual_status_shows_speed_and_error_details(self) -> None:
        self.assertIn("<span>Speed</span>", control.CONTROL_HTML)
        self.assertIn("function formatBroadIndexThroughput(latestIndex, active)", control.CONTROL_HTML)
        self.assertIn("Boolean(activeIndex)", control.CONTROL_HTML)
        self.assertIn("<span>Latest fingerprint errors</span>", control.CONTROL_HTML)
        self.assertIn("function formatBroadIndexErrors(errors, errorCount)", control.CONTROL_HTML)
        self.assertIn("latest_index_errors", control.CONTROL_HTML)
        self.assertIn("skipped_candidate_count", control.CONTROL_HTML)

    def test_control_html_refreshes_attached_active_jobs(self) -> None:
        self.assertIn("const ACTIVE_STATUS_REFRESH_MS = 1500", control.CONTROL_HTML)
        self.assertIn("scheduleActiveStatusRefresh(payload)", control.CONTROL_HTML)
        self.assertIn("function scheduleActiveStatusRefresh(payload)", control.CONTROL_HTML)
        self.assertIn("const activeJobs = (payload.active_jobs || []).filter", control.CONTROL_HTML)
        self.assertIn("loadStatus(Array.from(steps))", control.CONTROL_HTML)
        self.assertIn("if (cancellableJob) activeJobId = cancellableJob.job_id", control.CONTROL_HTML)

    def test_control_html_exposes_stop_button_for_running_jobs(self) -> None:
        self.assertIn('document.querySelector(".run-progress button")', control.CONTROL_HTML)
        self.assertIn("cancelActiveJob()", control.CONTROL_HTML)
        self.assertIn('/cancel`, {method: "POST"}', control.CONTROL_HTML)
        self.assertIn(".run-progress.status-cancelled", control.CONTROL_HTML)

    def test_control_html_distinguishes_start_cancel_from_job_stop(self) -> None:
        self.assertIn("let startRequestController = null", control.CONTROL_HTML)
        self.assertIn("new AbortController()", control.CONTROL_HTML)
        self.assertIn("signal: startRequestController.signal", control.CONTROL_HTML)
        self.assertIn('const actionLabel = jobId ? "Stop" : "Cancel"', control.CONTROL_HTML)
        self.assertIn("Waiting for the server to create a job", control.CONTROL_HTML)
        self.assertIn("Start cancelled before a server job was created.", control.CONTROL_HTML)
        self.assertIn('status: "starting"', control.CONTROL_HTML)

    def test_control_html_does_not_use_in_page_folder_browser(self) -> None:
        self.assertNotIn('id="folderBrowser"', control.CONTROL_HTML)
        self.assertNotIn('id="controlFolderBrowser"', control.CONTROL_HTML)
        self.assertNotIn("function showFolderBrowser", control.CONTROL_HTML)
        self.assertNotIn("function showControlFolderBrowser", control.CONTROL_HTML)
        self.assertNotIn("folder-option", control.CONTROL_HTML)

    def test_photo_index_folder_from_file_returns_indexed_root_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            folder = base / "Indexed Folder"
            nested_folder = folder / "Nested" / "Month"
            nested_folder.mkdir(parents=True)
            photo = nested_folder / "sample.jpg"
            photo.write_bytes(b"sample photo")
            index_path = base / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        byte_size INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root, filename, byte_size) VALUES (?, ?, ?, ?)",
                    (str(photo), str(folder), photo.name, photo.stat().st_size),
                )
                connection.commit()

            payload = control._photo_index_folder_from_file(
                filename=photo.name,
                byte_size=photo.stat().st_size,
                sha256=control._sha256_file(photo),
                index_path=index_path,
            )

        self.assertEqual(payload["path"], str(folder))
        self.assertEqual(payload["matched_file"], str(photo))

    def test_photo_index_folder_from_file_uses_indexed_hash_without_rereading_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            folder = base / "Indexed Folder"
            folder.mkdir()
            missing_photo = folder / "placeholder.jpg"
            index_path = base / "photo_library_index.sqlite"
            sha256 = hashlib.sha256(b"placeholder").hexdigest()
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        byte_size INTEGER NOT NULL,
                        sha256 TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root, filename, byte_size, sha256) VALUES (?, ?, ?, ?, ?)",
                    (str(missing_photo), str(folder), missing_photo.name, len(b"placeholder"), sha256),
                )
                connection.commit()

            payload = control._photo_index_folder_from_file(
                filename=missing_photo.name,
                byte_size=len(b"placeholder"),
                sha256=sha256,
                index_path=index_path,
            )

        self.assertEqual(payload["path"], str(folder))
        self.assertEqual(payload["matched_file"], str(missing_photo))

    def test_photo_index_folder_from_file_rejects_nonindexed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        byte_size INTEGER NOT NULL
                    )
                    """
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "not found in the photo index"):
                control._photo_index_folder_from_file(
                    filename="missing.jpg",
                    byte_size=123,
                    sha256="0" * 64,
                    index_path=index_path,
                )

    def test_photo_index_folder_from_file_reports_latest_index_run_for_missing_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        byte_size INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE photo_library_index_runs (
                        run_id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        roots TEXT NOT NULL,
                        reset INTEGER NOT NULL,
                        scanned_file_count INTEGER NOT NULL,
                        indexed_file_count INTEGER NOT NULL,
                        skipped_file_count INTEGER NOT NULL,
                        file_count_before INTEGER NOT NULL,
                        file_count_after INTEGER NOT NULL,
                        new_file_count INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO photo_library_index_runs (
                        run_id,
                        started_at,
                        finished_at,
                        roots,
                        reset,
                        scanned_file_count,
                        indexed_file_count,
                        skipped_file_count,
                        file_count_before,
                        file_count_after,
                        new_file_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "run-1",
                        "2026-08-22T02:19:16+00:00",
                        "2026-08-22T02:19:19+00:00",
                        "Source Data",
                        0,
                        369,
                        369,
                        0,
                        653550,
                        653715,
                        165,
                    ),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ValueError,
                "Latest index run 2026-08-22 scanned Source Data and added 165 new files",
            ):
                control._photo_index_folder_from_file(
                    filename="2026-08-22 Placeholder iCloud Photos 2006-2019.jpg",
                    byte_size=123,
                    sha256="0" * 64,
                    index_path=index_path,
                )

    def test_choose_folder_dialog_returns_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = mock.Mock(returncode=0, stdout=f"{temp_dir}\n", stderr="")
            with mock.patch("project365_control_app.subprocess.run", return_value=result) as run:
                payload = control._choose_folder_dialog()

        self.assertEqual(payload, {"path": temp_dir})
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertIn("choose folder with prompt", run.call_args.args[0][2])
        self.assertNotIn("activate\n", run.call_args.args[0][2])

    def test_validate_folder_path_returns_resolved_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = control._validate_folder_path(temp_dir)

        self.assertEqual(payload, {"path": str(Path(temp_dir).resolve())})

    def test_validate_folder_path_accepts_pasted_file_path_as_parent_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Photos"
            folder.mkdir()
            photo = folder / "example.jpg"
            photo.write_bytes(b"placeholder")
            payload = control._validate_folder_path(f"'{photo}'")

        self.assertEqual(payload, {"path": str(folder.resolve())})

    def test_validate_folder_path_rejects_missing_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with self.assertRaisesRegex(ValueError, "Folder does not exist"):
                control._validate_folder_path(str(missing))

    def test_choose_folder_dialog_treats_user_cancel_as_empty_path(self) -> None:
        result = mock.Mock(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")
        with mock.patch("project365_control_app.subprocess.run", return_value=result):
            payload = control._choose_folder_dialog()

        self.assertEqual(payload, {"path": ""})

    def test_status_reports_missing_paths_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(control, "CANONICAL_ROOT", Path(temp_dir) / "missing"),
                mock.patch.object(control, "ORIGINAL_UNCLEAR_GROUPS", Path(temp_dir) / "missing_groups.csv"),
            ):
                status = control.ControlState().status()

        self.assertIn("paths", status)
        self.assertEqual(status["database"], {})

    def test_scoped_status_skips_photo_index_database_read_while_index_job_runs(self) -> None:
        state = control.ControlState()
        with state._job_lock:
            state.jobs["index-job"] = {
                "job_id": "index-job",
                "step": "refresh_photo_index_metadata",
                "status": "running",
                "started_at": "2026-08-22T00:00:00+00:00",
            }

        with (
            mock.patch.object(
                control,
                "_photo_library_index_status",
                side_effect=AssertionError("should not read busy index"),
            ),
            mock.patch.object(
                control,
                "_initial_photo_index_file_count",
                side_effect=AssertionError("should not count busy index"),
            ),
        ):
            status = state.status(["build_photo_index"])

        self.assertEqual(status["active_jobs"][0]["job_id"], "index-job")
        self.assertTrue(status["photo_library_index"]["busy"])
        self.assertNotIn("photo_index_files", status["top_metrics"])

    def test_broad_scoped_status_skips_raw_history_and_photo_index_count(self) -> None:
        state = control.ControlState()
        state.history = [
            {
                "step": "broad_visual_match",
                "status": "pass",
                "outputs": [{"output": "x" * 10000}],
                "summary": {},
            },
            {
                "step": "import_zips",
                "status": "pass",
                "outputs": [{"output": "unrelated"}],
                "summary": {},
            },
        ]

        with (
            mock.patch.object(control, "_broad_visual_status", return_value={}),
            mock.patch.object(
                control,
                "_initial_photo_index_file_count",
                side_effect=AssertionError("should not count unrelated photo index"),
            ),
        ):
            status = state.status(["broad_visual_match"])

        self.assertNotIn("history", status)
        self.assertIn("workflow_history", status)
        self.assertIn("broad_visual_match", status["workflow_history"])
        self.assertNotIn("import_zips", status["workflow_history"])
        self.assertNotIn("photo_index_files", status["top_metrics"])

    def test_current_workflow_history_uses_persisted_no_date_status_without_control_history(self) -> None:
        status = {
            "broad_visual_match": {
                "rough_review_ready_run": {
                    "run_id": "rough-no-date-match:current",
                    "status": "pass",
                    "started_at": "2026-09-12T01:00:00+00:00",
                    "finished_at": "2026-09-12T01:05:00+00:00",
                    "processed_target_count": 12,
                    "target_count": 12,
                    "scanned_count": 240,
                    "matched_entries": 8,
                    "result_count": 32,
                    "error_count": 0,
                    "prefilter_metrics": {"shortlist_size": 1000, "capped_band_count": 4},
                }
            }
        }

        history = control._current_workflow_history([], status, {"rough_visual_match"})

        record = history["rough_visual_match"][0]
        self.assertEqual(record["step"], "rough_visual_match")
        self.assertEqual(record["started_at"], "2026-09-12T01:00:00+00:00")
        self.assertIn(
            {"label": "Entries searched", "value": "12/12"},
            record["summary"]["metrics"],
        )
        self.assertIn(
            {"label": "Saved candidate rows", "value": "32"},
            record["summary"]["metrics"],
        )

    def test_current_workflow_history_includes_persisted_status_with_recorded_history(self) -> None:
        recorded = {
            "step": "rough_visual_match",
            "status": "fail",
            "started_at": "2026-09-12T02:00:00+00:00",
            "finished_at": "2026-09-12T02:01:00+00:00",
            "error": "kept",
            "outputs": [],
            "summary": {"metrics": [{"label": "Saved candidate rows", "value": "1"}]},
        }
        status = {
            "broad_visual_match": {
                "rough_review_ready_run": {
                    "run_id": "rough-no-date-match:current",
                    "started_at": "2026-09-12T03:00:00+00:00",
                    "result_count": 99,
                }
            }
        }

        history = control._current_workflow_history([recorded], status, {"rough_visual_match"})

        self.assertEqual(history["rough_visual_match"][0]["started_at"], "2026-09-12T03:00:00+00:00")
        self.assertEqual(history["rough_visual_match"][1], recorded)

    def test_current_workflow_history_synthesizes_persisted_tool_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            report_dir = canonical_root / "exports" / "verification_reports"
            package_dir = canonical_root / "exports" / "diarium_import_batches"
            report_dir.mkdir(parents=True)
            package_dir.mkdir(parents=True)
            db_path = canonical_root / "canonical.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE import_batches (
                        id TEXT PRIMARY KEY,
                        source_type TEXT NOT NULL,
                        import_dir TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        source_file_count INTEGER NOT NULL DEFAULT 0,
                        entry_count INTEGER NOT NULL DEFAULT 0,
                        entry_source_count INTEGER NOT NULL DEFAULT 0,
                        media_asset_count INTEGER NOT NULL DEFAULT 0,
                        text_entry_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO import_batches (
                        id, source_type, import_dir, started_at, finished_at,
                        source_file_count, entry_count, entry_source_count,
                        media_asset_count, text_entry_count, status
                    )
                    VALUES ('batch-1', 'project365_zip', 'zips',
                        '2026-09-12T00:00:00+00:00',
                        '2026-09-12T00:10:00+00:00',
                        4, 40, 40, 40, 0, 'pass')
                    """
                )
                connection.commit()
            derivatives_report = report_dir / f"media_derivatives_{control.DERIVATIVE_POLICY}.csv"
            derivatives_report.write_text("entry_id,path\nproject365:1998-04-12,out.jpg\n", encoding="utf-8")
            tag_queue = report_dir / "tag_review_queue.csv"
            tag_queue.write_text("entry_id,person\nproject365:1998-04-12,Mike\n", encoding="utf-8")
            digikam_report = report_dir / "digikam_people_import_report.csv"
            digikam_report.write_text("status\nsuggested\n", encoding="utf-8")
            package_path = package_dir / "package.zip"
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr("Journal.json", json.dumps({"entries": []}))
            status = {
                "photo_library_index": {
                    "date_count": 20,
                    "recent_runs": [
                        {
                            "started_at": "2026-09-12T00:20:00+00:00",
                            "finished_at": "2026-09-12T00:30:00+00:00",
                            "roots": "Photos",
                            "file_count_after": 500,
                            "new_file_count": 5,
                            "skipped_file_count": 2,
                        }
                    ],
                },
                "original_remainder_overview": {
                    "exists": True,
                    "review_ready_entry_count": 7,
                },
                "original_batch_plan": {"rows": 3},
                "original_search_attempts": {
                    "latest": {
                        "finished_at": "2026-09-12T00:40:00+00:00",
                        "search_roots": "Photos",
                    }
                },
                "original_queue": {
                    "exists": True,
                    "rows": 70,
                    "pending_apply": {"decision_count": 6},
                },
                "broad_visual_match": {
                    "review_ready_run": {
                        "run_id": "broad-match:current",
                        "status": "pass",
                        "started_at": "2026-09-12T00:50:00+00:00",
                        "finished_at": "2026-09-12T00:55:00+00:00",
                        "processed_target_count": 2,
                        "target_count": 2,
                        "scanned_count": 20,
                        "matched_entries": 1,
                        "result_count": 5,
                        "error_count": 0,
                    },
                    "rough_review_ready_run": {
                        "run_id": "rough-no-date-match:current",
                        "status": "pass",
                        "started_at": "2026-09-12T01:00:00+00:00",
                        "finished_at": "2026-09-12T01:05:00+00:00",
                        "processed_target_count": 3,
                        "target_count": 3,
                        "scanned_count": 30,
                        "matched_entries": 2,
                        "result_count": 6,
                        "error_count": 0,
                        "prefilter_metrics": {"shortlist_size": 1000},
                    },
                },
                "crop_confirmation": {
                    "queued_count": 8,
                    "estimated_crop_count": 9,
                    "confirmed_crop_count": 10,
                    "pending_commit_count": 0,
                },
                "database": {
                    "working_copy_source_count": 11,
                    "working_copy_ready_count": 12,
                    "working_copy_current_count": 13,
                    "working_copy_not_ready_count": 14,
                    "working_copy_needs_update_count": 15,
                },
                "diarium_package": {
                    "path": str(package_path),
                    "filename": package_path.name,
                    "package_count": 1,
                    "journal_entries": 16,
                    "photo_files": 17,
                    "manifest_rows": 18,
                    "photo_ready": True,
                },
            }

            with (
                mock.patch.object(control, "CANONICAL_ROOT", canonical_root),
                mock.patch.object(control, "VERIFY_REPORT_DIR", report_dir),
                mock.patch.object(control, "ORIGINAL_UNCLEAR_GROUPS", report_dir / "original_photo_unclear_groups.csv"),
                mock.patch.object(control, "ORIGINAL_QUEUE", report_dir / "original_photo_external_search_queue.csv"),
                mock.patch.object(control, "TAG_QUEUE", tag_queue),
                mock.patch.object(control, "DIGIKAM_PEOPLE_REPORT", digikam_report),
                mock.patch.object(control, "DIARIUM_IMPORT_BATCH_DIR", package_dir),
            ):
                history = control._current_workflow_history([], status)

        self.assertIn("import_zips", history)
        self.assertIn("build_photo_index", history)
        self.assertIn("search_originals", history)
        self.assertIn("broad_visual_match", history)
        self.assertIn("rough_visual_match", history)
        self.assertIn("crop_confirmation", history)
        self.assertIn("generate_derivatives", history)
        self.assertIn("face_tagging", history)
        self.assertIn("generate_diarium_package", history)

    def test_workflow_overview_status_uses_lightweight_photo_index_summary(self) -> None:
        state = control.ControlState()
        with (
            mock.patch.object(control, "_initial_top_metrics", return_value={}) as top_metrics,
            mock.patch.object(control, "_photo_library_index_latest_run_status", return_value={}) as latest_index,
            mock.patch.object(
                control,
                "_photo_library_index_status",
                side_effect=AssertionError("overview should not read full photo index status"),
            ),
            mock.patch.object(control, "_queue_status", return_value={}),
            mock.patch.object(control, "_remainder_overview", return_value={}),
            mock.patch.object(control, "_batch_plan_status", return_value={}),
            mock.patch.object(control, "_search_attempt_status", return_value={}),
            mock.patch.object(control, "_broad_visual_overview_status", return_value={}),
            mock.patch.object(control.ControlState, "crop_confirmation_status", return_value={}),
            mock.patch.object(control, "_diarium_package_status", return_value={}),
        ):
            status = state.status(["workflow_overview"])

        top_metrics.assert_called_once_with(include_photo_index=False)
        latest_index.assert_called_once_with(control.PHOTO_LIBRARY_INDEX)
        self.assertIn("workflow_history", status)

    def test_rough_scoped_status_uses_lightweight_broad_visual_status(self) -> None:
        state = control.ControlState()
        with state._job_lock:
            state.jobs["prefilter-job"] = {
                "job_id": "prefilter-job",
                "step": "rough_prefilter_build",
                "status": "running",
                "started_at": "2026-08-22T00:00:00+00:00",
            }
            state.jobs["rough-job"] = {
                "job_id": "rough-job",
                "step": "rough_visual_match",
                "status": "running",
                "started_at": "2026-08-22T00:00:00+00:00",
            }

        with mock.patch.object(
            control,
            "_broad_visual_status",
            return_value={
                "rough_prefilter": {
                    "latest_run": {
                        "run_id": "prefilter-run",
                        "status": "running",
                        "finished_at": "",
                        "total_descriptor_count": 20,
                        "scanned_count": 5,
                    },
                },
                "latest_no_date_run": {
                    "run_id": "run-1",
                    "status": "running",
                    "finished_at": "",
                    "target_count": 10,
                    "processed_target_count": 4,
                    "result_count": 7,
                    "heartbeat_at": "2026-08-22T00:01:00+00:00",
                }
            },
        ) as broad_status:
            status = state.status(["rough_visual_match"])

        broad_status.assert_called_once_with(
            include_monthly_coverage=False,
            include_prefilter_stale_count=False,
            include_review_runs=False,
        )
        self.assertIn("broad_visual_match", status)
        by_step = {job["step"]: job for job in status["active_jobs"]}
        self.assertEqual(by_step["rough_prefilter_build"]["rough_prefilter_run"]["scanned_count"], 5)
        self.assertEqual(by_step["rough_visual_match"]["rough_visual_run"]["processed_target_count"], 4)

    def test_broad_scoped_status_uses_lightweight_prefilter_counts(self) -> None:
        state = control.ControlState()
        with state._job_lock:
            state.jobs["broad-index-job"] = {
                "job_id": "broad-index-job",
                "step": "broad_visual_index",
                "status": "running",
                "started_at": "2026-08-22T00:00:00+00:00",
            }
            state.jobs["broad-search-job"] = {
                "job_id": "broad-search-job",
                "step": "broad_visual_match",
                "status": "running",
                "started_at": "2026-08-22T00:00:00+00:00",
            }

        with mock.patch.object(
            control,
            "_broad_visual_status",
            return_value={
                "latest_index_run": {
                    "run_id": "index-run",
                    "status": "running",
                    "finished_at": "",
                    "scanned_count": 50,
                    "total_candidate_count": 100,
                },
                "latest_run": {
                    "run_id": "search-run",
                    "status": "running",
                    "finished_at": "",
                    "processed_target_count": 3,
                    "target_count": 8,
                },
            },
        ) as broad_status:
            status = state.status(["broad_visual_match"])

        broad_status.assert_called_once_with(
            include_monthly_coverage=False,
            include_prefilter_stale_count=False,
            include_review_runs=True,
        )
        self.assertIn("broad_visual_match", status)
        by_step = {job["step"]: job for job in status["active_jobs"]}
        self.assertEqual(by_step["broad_visual_index"]["broad_visual_index_run"]["scanned_count"], 50)
        self.assertEqual(by_step["broad_visual_match"]["broad_visual_run"]["processed_target_count"], 3)

    def test_broad_scoped_status_loads_monthly_coverage_when_requested(self) -> None:
        state = control.ControlState()
        with mock.patch.object(
            control,
            "_broad_visual_status",
            return_value={"latest_run": {}},
        ) as broad_status:
            status = state.status(["broad_visual_match"], include_broad_monthly_coverage=True)

        broad_status.assert_called_once_with(
            include_monthly_coverage=True,
            include_prefilter_stale_count=False,
            include_review_runs=True,
        )
        self.assertIn("broad_visual_match", status)

    def test_lightweight_rough_status_avoids_full_broad_status(self) -> None:
        with (
            mock.patch.object(control.broad_visual_match, "rough_visual_status", return_value={}) as rough_status,
            mock.patch.object(
                control.broad_visual_match,
                "broad_status",
                side_effect=AssertionError("should not use full broad status"),
            ),
        ):
            status = control._broad_visual_status(
                include_monthly_coverage=False,
                include_prefilter_stale_count=False,
                include_review_runs=False,
            )

        rough_status.assert_called_once_with(control.BROAD_VISUAL_DB)
        self.assertEqual(status["review_runs"], [])

    def test_status_reports_original_search_batch_plan_with_attempt_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            batch_plan = base / "original_photo_search_batch_plan.csv"
            attempt_log = base / "original_photo_search_attempts.csv"
            _write_csv(
                batch_plan,
                [
                    {
                        "batch_id": "batch-0001",
                        "start_date": "1998-04-10",
                        "end_date": "1998-04-13",
                        "date_count": "4",
                        "entry_count": "4",
                        "candidate_count": "0",
                        "review_date_count": "0",
                        "folder_needed_date_count": "4",
                        "hidden_rejected_candidate_count": "0",
                        "statuses": "no_external_candidates",
                        "entry_dates": "1998-04-10;1998-04-11;1998-04-12;1998-04-13",
                        "entry_ids": "project365:1998-04-10",
                        "control_app_start_date": "1998-04-10",
                        "control_app_end_date": "1998-04-13",
                        "recommended_action": "choose_search_folder",
                    },
                    {
                        "batch_id": "batch-0002",
                        "start_date": "1998-04-20",
                        "end_date": "1998-04-20",
                        "date_count": "1",
                        "entry_count": "1",
                        "candidate_count": "0",
                        "review_date_count": "0",
                        "folder_needed_date_count": "1",
                        "hidden_rejected_candidate_count": "1",
                        "statuses": "all_candidates_rejected",
                        "entry_dates": "1998-04-20",
                        "entry_ids": "project365:1998-04-20",
                        "control_app_start_date": "1998-04-20",
                        "control_app_end_date": "1998-04-20",
                        "recommended_action": "choose_search_folder",
                    }
                ],
            )
            _write_csv(
                attempt_log,
                [
                    {
                        "attempt_id": "default",
                        "started_at": "2026-08-17T00:00:00+00:00",
                        "finished_at": "2026-08-17T00:00:01+00:00",
                        "search_roots": "/Volumes/Default",
                        "target_entry_ids": "",
                        "target_entry_dates": "",
                        "start_date": "",
                        "end_date": "",
                        "max_targets": "",
                        "scan_metadata_dates": "true",
                        "merge_existing_queue": "false",
                        "unclear_entry_count": "4",
                        "candidate_count": "0",
                        "hidden_rejected_candidate_count": "3",
                        "hidden_export_equivalent_candidate_count": "0",
                        "search_queue_path": "queue.csv",
                        "group_report_path": "groups.csv",
                        "batch_plan_path": "batches.csv",
                    },
                    {
                        "attempt_id": "targeted",
                        "started_at": "2026-08-17T00:01:00+00:00",
                        "finished_at": "2026-08-17T00:01:02+00:00",
                        "search_roots": "/Volumes/Trip",
                        "target_entry_ids": "project365:1998-04-10",
                        "target_entry_dates": "1998-04-10;1998-04-11;1998-04-12;1998-04-13",
                        "start_date": "1998-04-10",
                        "end_date": "1998-04-13",
                        "max_targets": "4",
                        "scan_metadata_dates": "true",
                        "merge_existing_queue": "true",
                        "unclear_entry_count": "4",
                        "candidate_count": "0",
                        "hidden_rejected_candidate_count": "0",
                        "hidden_export_equivalent_candidate_count": "0",
                        "search_queue_path": "queue.csv",
                        "group_report_path": "groups.csv",
                        "batch_plan_path": "batches.csv",
                    },
                ],
            )

            with (
                mock.patch.object(control, "CANONICAL_ROOT", base / "missing"),
                mock.patch.object(control, "ORIGINAL_UNCLEAR_GROUPS", base / "missing_groups.csv"),
                mock.patch.object(control, "ORIGINAL_BATCH_PLAN", batch_plan),
                mock.patch.object(control, "ORIGINAL_SEARCH_ATTEMPTS", attempt_log),
                mock.patch.object(control, "DIARIUM_IMPORT_BATCH_DIR", base / "missing_packages"),
            ):
                status = control.ControlState().status()

        self.assertEqual(status["original_batch_plan"]["rows"], 2)
        self.assertEqual(status["original_batch_plan"]["hidden_rejected_candidate_count"], 1)
        self.assertEqual(
            status["original_batch_plan"]["status_counts"],
            {"no_external_candidates": 1, "all_candidates_rejected": 1},
        )
        self.assertNotIn("batches", status["original_batch_plan"])
        self.assertNotIn("batch_limit", status["original_batch_plan"])
        self.assertEqual(
            status["original_batch_plan"]["next_batch"]["start_date"],
            "1998-04-10",
        )
        self.assertEqual(
            status["original_batch_plan"]["next_batch"]["recommended_action"],
            "choose_search_folder",
        )
        self.assertEqual(
            status["original_batch_plan"]["next_batch"]["hidden_rejected_candidate_count"],
            "0",
        )
        self.assertEqual(status["original_batch_plan"]["next_batch"]["review_date_count"], "0")
        self.assertEqual(status["original_batch_plan"]["next_batch"]["folder_needed_date_count"], "4")
        self.assertEqual(status["original_batch_plan"]["next_batch"]["search_attempt_count"], "1")
        self.assertEqual(
            status["original_batch_plan"]["next_batch"]["latest_search_roots"],
            "/Volumes/Trip",
        )
        self.assertEqual(
            status["original_batch_plan"]["next_batch"]["latest_search_candidate_count"],
            "0",
        )
        self.assertEqual(status["original_batch_plan"]["next_batch"]["next_step"], "Try another folder")

    def test_remainder_overview_reports_actionable_folder_and_hidden_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            group_path = Path(temp_dir) / "original_photo_unclear_groups.csv"
            _write_csv(
                group_path,
                [
                    {
                        "entry_date": "1998-04-08",
                        "entry_count": "1",
                        "candidate_count": "7",
                        "hidden_rejected_candidate_count": "0",
                        "hidden_export_equivalent_candidate_count": "1",
                        "status": "needs_choice",
                    },
                    {
                        "entry_date": "1998-04-10",
                        "entry_count": "1",
                        "candidate_count": "0",
                        "hidden_rejected_candidate_count": "0",
                        "hidden_export_equivalent_candidate_count": "1",
                        "status": "only_export_equivalent_candidates",
                    },
                    {
                        "entry_date": "1998-05-01",
                        "entry_count": "1",
                        "candidate_count": "0",
                        "hidden_rejected_candidate_count": "0",
                        "hidden_export_equivalent_candidate_count": "0",
                        "status": "no_external_candidates",
                    },
                    {
                        "entry_date": "2003-12-01",
                        "entry_count": "1",
                        "candidate_count": "0",
                        "hidden_rejected_candidate_count": "3",
                        "hidden_export_equivalent_candidate_count": "0",
                        "status": "all_candidates_rejected",
                    },
                ],
            )

            overview = control._remainder_overview(group_path)

        self.assertTrue(overview["exists"])
        self.assertEqual(overview["date_count"], 4)
        self.assertEqual(overview["entry_count"], 4)
        self.assertEqual(overview["review_ready_date_count"], 1)
        self.assertEqual(overview["review_ready_entry_count"], 1)
        self.assertEqual(overview["folder_needed_date_count"], 1)
        self.assertEqual(overview["only_export_equivalent_date_count"], 1)
        self.assertEqual(overview["all_rejected_date_count"], 1)
        self.assertEqual(overview["actionable_candidate_count"], 7)
        self.assertEqual(overview["hidden_rejected_candidate_count"], 3)
        self.assertEqual(overview["hidden_export_equivalent_candidate_count"], 2)

    def test_partial_entry_attempt_does_not_count_as_batch_search(self) -> None:
        batch = {
            "batch_id": "batch-0001",
            "start_date": "1998-04-10",
            "end_date": "1998-04-13",
            "entry_count": "4",
            "candidate_count": "0",
            "review_date_count": "0",
            "folder_needed_date_count": "4",
            "hidden_rejected_candidate_count": "0",
            "statuses": "no_external_candidates",
            "entry_dates": "1998-04-10;1998-04-11;1998-04-12;1998-04-13",
            "entry_ids": "project365:1998-04-10;project365:1998-04-11;project365:1998-04-12;project365:1998-04-13",
            "recommended_action": "choose_search_folder",
        }
        partial_attempt = {
            "target_entry_ids": "project365:1998-04-10",
            "target_entry_dates": "1998-04-10",
            "start_date": "1998-04-10",
            "end_date": "1998-04-13",
            "max_targets": "4",
            "finished_at": "2026-08-17T00:01:02+00:00",
            "search_roots": "/Volumes/Trip",
            "candidate_count": "0",
        }
        full_attempt = {
            **partial_attempt,
            "target_entry_ids": "",
            "target_entry_dates": "1998-04-10;1998-04-11;1998-04-12;1998-04-13",
        }

        partial_summary = control._batch_summary(batch, [partial_attempt])
        full_summary = control._batch_summary(batch, [full_attempt])

        self.assertEqual(partial_summary["search_attempt_count"], "0")
        self.assertEqual(partial_summary["next_step"], "Choose folder")
        self.assertEqual(full_summary["search_attempt_count"], "1")
        self.assertEqual(full_summary["next_step"], "Try another folder")
        self.assertEqual(full_summary["review_date_count"], "0")
        self.assertEqual(full_summary["folder_needed_date_count"], "4")

    def test_status_prioritizes_candidate_batch_as_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            batch_plan = base / "original_photo_search_batch_plan.csv"
            _write_csv(
                batch_plan,
                [
                    {
                        "batch_id": "batch-0001",
                        "start_date": "1998-04-10",
                        "end_date": "1998-04-13",
                        "date_count": "4",
                        "entry_count": "4",
                        "candidate_count": "0",
                        "hidden_rejected_candidate_count": "0",
                        "statuses": "no_external_candidates",
                        "entry_dates": "1998-04-10",
                        "entry_ids": "project365:1998-04-10",
                        "control_app_start_date": "1998-04-10",
                        "control_app_end_date": "1998-04-13",
                        "recommended_action": "choose_search_folder",
                    },
                    {
                        "batch_id": "batch-0002",
                        "start_date": "2026-08-09",
                        "end_date": "2026-08-09",
                        "date_count": "1",
                        "entry_count": "1",
                        "candidate_count": "2",
                        "hidden_rejected_candidate_count": "0",
                        "statuses": "needs_choice",
                        "entry_dates": "2026-08-09",
                        "entry_ids": "project365:2026-08-09",
                        "control_app_start_date": "2026-08-09",
                        "control_app_end_date": "2026-08-09",
                        "recommended_action": "review_candidates",
                    },
                ],
            )

            status = control._batch_plan_status(batch_plan)

        self.assertEqual(status["next_batch"]["batch_id"], "batch-0002")
        self.assertEqual(status["next_batch"]["next_step"], "Review candidates")

    def test_status_reports_original_search_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            attempt_log = base / "original_photo_search_attempts.csv"
            _write_csv(
                attempt_log,
                [
                    {
                        "attempt_id": "first",
                        "started_at": "2026-08-17T00:00:00+00:00",
                        "finished_at": "2026-08-17T00:00:01+00:00",
                        "search_roots": "/Volumes/First",
                        "target_entry_ids": "",
                        "target_entry_dates": "",
                        "start_date": "",
                        "end_date": "",
                        "max_targets": "",
                        "scan_metadata_dates": "false",
                        "merge_existing_queue": "false",
                        "unclear_entry_count": "6",
                        "candidate_count": "0",
                        "hidden_rejected_candidate_count": "0",
                        "search_queue_path": "queue.csv",
                        "group_report_path": "groups.csv",
                        "batch_plan_path": "batches.csv",
                    },
                    {
                        "attempt_id": "second",
                        "started_at": "2026-08-17T00:01:00+00:00",
                        "finished_at": "2026-08-17T00:01:02+00:00",
                        "search_roots": "/Volumes/Second",
                        "target_entry_ids": "project365:1998-04-13",
                        "target_entry_dates": "1998-04-13",
                        "start_date": "1998-04-13",
                        "end_date": "1998-04-13",
                        "max_targets": "1",
                        "scan_metadata_dates": "true",
                        "merge_existing_queue": "true",
                        "unclear_entry_count": "1",
                        "candidate_count": "2",
                        "hidden_rejected_candidate_count": "3",
                        "hidden_export_equivalent_candidate_count": "4",
                        "search_queue_path": "queue.csv",
                        "group_report_path": "groups.csv",
                        "batch_plan_path": "batches.csv",
                    },
                ],
            )

            status = control._search_attempt_status(attempt_log)

        self.assertEqual(status["rows"], 2)
        self.assertEqual(status["latest"]["search_roots"], "/Volumes/Second")
        self.assertEqual(status["latest"]["target_entry_dates"], "1998-04-13")
        self.assertEqual(status["latest"]["candidate_count"], "2")
        self.assertEqual(status["latest"]["hidden_rejected_candidate_count"], "3")
        self.assertEqual(status["latest"]["hidden_export_equivalent_candidate_count"], "4")
        self.assertNotIn("recent", status)

    def test_status_reports_latest_diarium_package_photo_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = Path(temp_dir)
            package_path = package_dir / "project365_full_dayone.zip"
            with zipfile.ZipFile(package_path, "w") as archive:
                photos = [
                    {
                        "identifier": "photo-a",
                        "md5": "aaa",
                        "type": "jpeg",
                        "width": 12,
                        "height": 9,
                    },
                    {
                        "identifier": "photo-b",
                        "md5": "bbb",
                        "type": "jpeg",
                        "width": 10,
                        "height": 8,
                    },
                ]
                archive.writestr(
                    "Journal.json",
                    json.dumps(
                        {
                            "metadata": {"version": "1.0"},
                            "entries": [
                                {
                                    "text": "![](dayone-moment://photo-a)",
                                    "photos": [photos[0]],
                                },
                                {
                                    "text": "![](dayone-moment://photo-b)",
                                    "photos": [photos[1]],
                                },
                            ],
                        }
                    ),
                )
                archive.writestr("photos/aaa.jpeg", b"photo-a")
                archive.writestr("photos/bbb.jpeg", b"photo-b")
            _write_csv(
                package_dir / "project365_full_dayone_manifest.csv",
                [
                    {"entry_id": "project365:1998-04-10", "photo_zip_path": "photos/aaa.jpeg"},
                    {"entry_id": "project365:1998-04-11", "photo_zip_path": "photos/bbb.jpeg"},
                ],
            )

            status = control._diarium_package_status(package_dir)

        self.assertEqual(status["journal_entries"], 2)
        self.assertEqual(status["photo_files"], 2)
        self.assertEqual(status["journal_photo_refs"], 2)
        self.assertEqual(status["missing_photo_refs"], 0)
        self.assertEqual(status["missing_photo_markers"], 0)
        self.assertEqual(status["zero_dimension_photo_refs"], 0)
        self.assertTrue(status["photo_ready"])
        self.assertEqual(status["manifest_rows"], 2)
        self.assertEqual(status["manifest_photo_rows"], 2)
        self.assertEqual(status["photo_hashes"], ["aaa", "bbb"])
        self.assertEqual(
            status["photo_hash_entries"],
            [
                {
                    "entry_id": "project365:1998-04-10",
                    "entry_date": "1998-04-10",
                    "photo_hash": "aaa",
                    "photo_zip_path": "photos/aaa.jpeg",
                },
                {
                    "entry_id": "project365:1998-04-11",
                    "entry_date": "1998-04-11",
                    "photo_hash": "bbb",
                    "photo_zip_path": "photos/bbb.jpeg",
                },
            ],
        )

    def test_diarium_package_status_flags_photo_metadata_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = Path(temp_dir)
            package_path = package_dir / "project365_full_dayone.zip"
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr(
                    "Journal.json",
                    json.dumps(
                        {
                            "metadata": {"version": "1.0"},
                            "entries": [
                                {
                                    "text": "photo marker missing",
                                    "photos": [
                                        {
                                            "identifier": "photo-a",
                                            "md5": "aaa",
                                            "type": "jpeg",
                                            "width": 0,
                                            "height": 0,
                                        },
                                        {
                                            "identifier": "photo-b",
                                            "md5": "bbb",
                                            "type": "jpeg",
                                            "width": 10,
                                            "height": 8,
                                        },
                                    ],
                                }
                            ],
                        }
                    ),
                )
                archive.writestr("photos/aaa.jpeg", b"photo-a")
            _write_csv(
                package_dir / "project365_full_dayone_manifest.csv",
                [
                    {"entry_id": "project365:1998-04-10", "photo_zip_path": "photos/aaa.jpeg"},
                ],
            )

            status = control._diarium_package_status(package_dir)

        self.assertEqual(status["journal_photo_refs"], 2)
        self.assertEqual(status["missing_photo_refs"], 1)
        self.assertEqual(status["missing_photo_markers"], 2)
        self.assertEqual(status["zero_dimension_photo_refs"], 1)
        self.assertFalse(status["photo_ready"])

    def test_diarium_import_verification_passes_when_local_attachments_match(self) -> None:
        verification = control._diarium_import_verification(
            {
                "exists": True,
                "photo_ready": True,
                "journal_entries": 67,
                "photo_files": 67,
                "photo_hashes": ["photo-a", "photo-b"],
            },
            {
                "exists": True,
                "readable": True,
                "entry_count": 67,
                "media_count": 67,
                "entries_without_media": 0,
                "empty_media_count": 0,
                "media_names": ["photo-a", "photo-b"],
            },
        )

        self.assertEqual(verification["status"], "pass")
        self.assertTrue(verification["photo_imported"])
        self.assertEqual(verification["actual_photos"], 67)
        self.assertIn("Attachments > Photos", verification["review_hint"])
        self.assertIn("separate media files under media/", verification["review_hint"])

    def test_diarium_import_verification_flags_stale_local_media_names(self) -> None:
        verification = control._diarium_import_verification(
            {
                "exists": True,
                "photo_ready": True,
                "journal_entries": 2,
                "photo_files": 2,
                "photo_hashes": ["new-a", "new-b"],
                "photo_hash_entries": [
                    {"entry_id": "project365:1998-04-10", "entry_date": "1998-04-10", "photo_hash": "new-a"},
                    {"entry_id": "project365:1998-04-11", "entry_date": "1998-04-11", "photo_hash": "new-b"},
                ],
            },
            {
                "exists": True,
                "readable": True,
                "entry_count": 2,
                "media_count": 2,
                "entries_without_media": 0,
                "empty_media_count": 0,
                "media_names": ["old-a", "new-b"],
            },
        )

        self.assertEqual(verification["status"], "attention")
        self.assertFalse(verification["photo_imported"])
        self.assertEqual(verification["media_name_match_count"], 1)
        self.assertEqual(
            verification["missing_media_name_entries"],
            [{"entry_id": "project365:1998-04-10", "entry_date": "1998-04-10", "photo_hash": "new-a"}],
        )
        self.assertIn("media names 1/2 match latest package (1998-04-10)", verification["message"])

    def test_diarium_import_verification_flags_missing_local_photos(self) -> None:
        verification = control._diarium_import_verification(
            {
                "exists": True,
                "photo_ready": True,
                "journal_entries": 67,
                "photo_files": 67,
            },
            {
                "exists": True,
                "readable": True,
                "entry_count": 67,
                "media_count": 0,
                "entries_without_media": 67,
                "empty_media_count": 0,
            },
        )

        self.assertEqual(verification["status"], "attention")
        self.assertFalse(verification["photo_imported"])
        self.assertIn("photos 0/67", verification["message"])
        self.assertIn("67 entries without attachments", verification["message"])

    def test_queue_status_reports_pending_picker_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "queue.csv"
            _write_csv(
                queue_path,
                [
                    {
                        "entry_id": "project365:1998-04-10",
                        "candidate_path": "/photos/a.jpg",
                        "review_decision": "use_external_original",
                    },
                    {
                        "entry_id": "project365:1998-04-11",
                        "candidate_path": "/photos/b.jpg",
                        "review_decision": "rejected",
                    },
                    {
                        "entry_id": "project365:1998-04-12",
                        "candidate_path": "/photos/c.jpg",
                        "review_decision": "keep_project365_export",
                    },
                    {
                        "entry_id": "project365:1998-04-12",
                        "candidate_path": "/photos/d.jpg",
                        "review_decision": "keep_project365_export",
                    },
                    {
                        "entry_id": "project365:1998-04-13",
                        "candidate_path": "",
                        "review_decision": "search_needed",
                    },
                ],
            )

            status = control._queue_status(queue_path)

        self.assertEqual(status["decisions"]["keep_project365_export"], 2)
        self.assertEqual(
            status["pending_apply"],
            {
                "entry_count": 3,
                "selected_count": 1,
                "rejected_count": 1,
                "fallback_count": 1,
                "decision_count": 3,
            },
        )

    def test_database_status_reports_project365_and_staged_source_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "canonical.db"
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
                        role TEXT NOT NULL,
                        review_status TEXT NOT NULL
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO entries VALUES (?, ?, ?)",
                    [
                        ("project365:1998-04-10", "1998-04-10", "project365"),
                        ("project365:1998-04-11", "1998-04-11", "project365"),
                        ("social:x_archive:1", "1998-04-10", "x_archive"),
                    ],
                )
                connection.execute(
                    "INSERT INTO media_assets VALUES ('media-1', 'project365_export_png', 'unreviewed')"
                )

            status = control._database_status(db_path)

        self.assertEqual(status["entry_count"], 3)
        self.assertEqual(status["project365_entry_count"], 2)
        self.assertEqual(status["non_project365_entry_count"], 1)
        self.assertEqual(status["project365_date_start"], "1998-04-10")
        self.assertEqual(status["project365_date_end"], "1998-04-11")
        self.assertEqual(
            status["source_counts"],
            [{"source_app": "project365", "count": 2}, {"source_app": "x_archive", "count": 1}],
        )

    def test_database_status_reports_missing_photos_as_unidentified_originals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "canonical.db"
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
                        entry_id TEXT,
                        role TEXT NOT NULL,
                        review_status TEXT NOT NULL
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO entries VALUES (?, ?, ?)",
                    [
                        ("project365:1998-04-10", "1998-04-10", "project365"),
                        ("project365:1998-04-11", "1998-04-11", "project365"),
                        ("project365:1998-04-12", "1998-04-12", "project365"),
                        ("social:x_archive:1", "1998-04-13", "x_archive"),
                    ],
                )
                connection.executemany(
                    "INSERT INTO media_assets VALUES (?, ?, ?, ?)",
                    [
                        ("export-1", "project365:1998-04-10", "project365_export_png", "unreviewed"),
                        ("export-2", "project365:1998-04-11", "project365_export_png", "unreviewed"),
                        ("original-1", "project365:1998-04-10", "external_original_reference", "confirmed"),
                        ("associated-1", "project365:1998-04-10", "external_original_associated_photo", "confirmed"),
                        ("rejected-1", "project365:1998-04-11", "external_original_rejected", "rejected"),
                    ],
                )

            status = control._database_status(db_path)

        self.assertEqual(status["project365_missing_photo_count"], 2)
        self.assertEqual(status["identified_original_count"], 1)
        self.assertEqual(status["associated_photo_count"], 1)
        self.assertEqual(status["without_identified_original_count"], 2)
        self.assertEqual(status["without_identified_original_percent"], 66.7)

    def test_control_header_includes_missing_photo_status_number(self) -> None:
        self.assertIn('<span>Missing photos</span>', control.CONTROL_HTML)
        self.assertIn('id="metricMissingPhotos"', control.CONTROL_HTML)
        self.assertIn('<span>Associated photos</span>', control.CONTROL_HTML)
        self.assertIn('id="metricAssociatedPhotos"', control.CONTROL_HTML)
        self.assertIn("metrics.associated_photos", control.CONTROL_HTML)
        self.assertIn('hasOwnProperty.call(metrics, "photo_index_files")', control.CONTROL_HTML)
        self.assertNotIn('id="metricWithoutIdentifiedOriginal"', control.CONTROL_HTML)
        self.assertIn("function formatCountPercent(count, total)", control.CONTROL_HTML)

    def test_database_status_reports_working_copy_readiness_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "canonical.db"
            ready_path = Path(temp_dir) / "ready.png"
            needs_crop_path = Path(temp_dir) / "needs-crop.png"
            ready_path.write_bytes(b"ready")
            needs_crop_path.write_bytes(b"needs-crop")
            ready_crop = {
                "review_crop": {
                    "x": 0,
                    "y": 0,
                    "size": 10,
                    "candidate_width": 10,
                    "candidate_height": 10,
                    "source": "manual",
                }
            }
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
                        entry_id TEXT,
                        role TEXT NOT NULL,
                        source_file_id TEXT,
                        storage_path TEXT,
                        sha256 TEXT,
                        byte_size INTEGER,
                        status TEXT,
                        review_status TEXT NOT NULL,
                        selected_default INTEGER NOT NULL DEFAULT 0,
                        transformation_json TEXT,
                        import_batch_id TEXT,
                        updated_at TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO entries VALUES (?, ?, ?)",
                    [
                        ("project365:1998-04-10", "1998-04-10", "project365"),
                        ("project365:1998-04-11", "1998-04-11", "project365"),
                        ("project365:1998-04-12", "1998-04-12", "project365"),
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO media_assets (
                        id, entry_id, role, storage_path, sha256, byte_size,
                        status, review_status, selected_default, transformation_json,
                        import_batch_id, updated_at
                    )
                    VALUES (?, ?, 'project365_export_png', ?, ?, ?, 'available',
                        'unreviewed', 1, ?, 'batch-1', ?)
                    """,
                    [
                        (
                            "media-ready",
                            "project365:1998-04-10",
                            str(ready_path),
                            "sha-ready",
                            ready_path.stat().st_size,
                            json.dumps(ready_crop, sort_keys=True),
                            "2026-08-20T00:00:00Z",
                        ),
                        (
                            "media-needs-crop",
                            "project365:1998-04-11",
                            str(needs_crop_path),
                            "sha-needs-crop",
                            needs_crop_path.stat().st_size,
                            "{}",
                            "2026-08-20T00:00:00Z",
                        ),
                    ],
                )

            status = control._database_status(db_path)

        self.assertEqual(status["working_copy_source_count"], 2)
        self.assertEqual(status["working_copy_ready_count"], 1)
        self.assertEqual(status["working_copy_not_ready_count"], 1)

    def test_status_reports_local_diarium_photo_counts_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "data.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE Entries(
                        DiaryEntryId bigint primary key not null,
                        Heading varchar not null,
                        Text varchar not null,
                        Rating integer not null,
                        Latitude float not null,
                        Longitude float not null
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE Media(
                        DiaryMediaId varchar(36) primary key not null,
                        Type integer not null,
                        Data blob,
                        Name varchar not null,
                        FileEnding varchar not null,
                        "Index" integer not null,
                        DiaryEntryId bigint not null
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO Entries VALUES (?, '', ?, 0, 0, 0)",
                    [
                        (630278064000000000, "private text should not be returned"),
                        (630278928000000000, "more private text"),
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO Media
                    VALUES ('media-id', 0, ?, 'photo', '.jpg', 0, 630278064000000000)
                    """,
                    (b"\xff\xd8photo",),
                )

            status = control._diarium_local_status(db_path)

        self.assertTrue(status["readable"])
        self.assertEqual(status["entry_count"], 2)
        self.assertEqual(status["media_count"], 1)
        self.assertEqual(status["empty_media_count"], 0)
        self.assertEqual(status["entries_without_media"], 1)
        self.assertEqual(status["date_start"], "1998-04-10")
        self.assertEqual(status["date_end"], "1998-04-11")
        self.assertEqual(status["media_types"], [{"type": 0, "file_ending": ".jpg", "count": 1}])
        self.assertNotIn("private text", json.dumps(status))

    def test_status_reports_photo_library_index_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL,
                        gps_latitude REAL,
                        gps_longitude REAL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE photo_library_dates (
                        file_path TEXT NOT NULL,
                        date TEXT NOT NULL,
                        source TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root, gps_latitude, gps_longitude) VALUES (?, ?, ?, ?)",
                    ("/Volumes/Photos/a.jpg", "/Volumes/Photos", 25.033, 121.565),
                )
                connection.execute(
                    "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, ?)",
                    ("/Volumes/Photos/a.jpg", "1998-04-10", "filename_date"),
                )
                connection.execute(
                    "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, ?)",
                    ("/Volumes/Photos/a.jpg", "1998-04-10", "filesystem_date"),
                )
                control._ensure_photo_library_index_run_table(connection)
                run_values = (
                    "2026-08-17T10:00:00+00:00",
                    "2026-08-17T10:00:01+00:00",
                    "Source Data",
                    0,
                    1,
                    1,
                    0,
                    1,
                    1,
                )
                connection.execute(
                    """
                    INSERT INTO photo_library_index_runs (
                        run_id, started_at, finished_at, roots, reset,
                        scanned_file_count, indexed_file_count, skipped_file_count,
                        file_count_before, file_count_after, new_file_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("unchanged", *run_values, 0),
                )
                connection.execute(
                    """
                    INSERT INTO photo_library_index_runs (
                        run_id, started_at, finished_at, roots, reset,
                        scanned_file_count, indexed_file_count, skipped_file_count,
                        file_count_before, file_count_after, new_file_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("changed", *run_values, 1),
                )

            status = control._photo_library_index_status(index_path)

        self.assertEqual(status["file_count"], 1)
        self.assertEqual(status["date_count"], 1)
        self.assertEqual(status["capture_timestamp_count"], 0)
        self.assertEqual(status["gps_coordinate_count"], 1)
        self.assertEqual(status["roots"], ["/Volumes/Photos"])
        self.assertEqual(len(status["recent_runs"]), 2)
        self.assertEqual({run["new_file_count"] for run in status["recent_runs"]}, {0, 1})

    def test_status_prefers_photo_library_index_has_gps_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL,
                        has_gps INTEGER NOT NULL DEFAULT 0,
                        gps_latitude REAL,
                        gps_longitude REAL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE photo_library_dates (
                        file_path TEXT NOT NULL,
                        date TEXT NOT NULL,
                        source TEXT NOT NULL
                    )
                    """
                )
                indexed_path = "/Volumes/Photos/a.heic"
                indexed_path_with_stale_flag = "/Volumes/Photos/b.heic"
                connection.execute(
                    "INSERT INTO photo_library_files (path, root, has_gps) VALUES (?, ?, ?)",
                    (indexed_path, "/Volumes/Photos", 1),
                )
                connection.execute(
                    """
                    INSERT INTO photo_library_files
                        (path, root, has_gps, gps_latitude, gps_longitude)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (indexed_path_with_stale_flag, "/Volumes/Photos", 0, 25.0833694444444, 121.593819444444),
                )

            status = control._photo_library_index_status(index_path)
            geolocation_by_path = control._photo_index_geolocation_by_path(
                index_path, [indexed_path, indexed_path_with_stale_flag]
            )

        self.assertEqual(status["gps_coordinate_count"], 2)
        self.assertTrue(geolocation_by_path[indexed_path])
        self.assertTrue(geolocation_by_path[indexed_path_with_stale_flag])

    def test_photo_library_index_status_omits_child_roots_covered_by_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            parent = base / "Photos - Travels Places Events & Homes"
            child = parent / "India - 2003 The Year on A Bike"
            child.mkdir(parents=True)
            index_path = base / "photo_library_index.sqlite"
            with sqlite3.connect(index_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE photo_library_files (
                        path TEXT PRIMARY KEY,
                        root TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE photo_library_dates (
                        file_path TEXT NOT NULL,
                        date TEXT NOT NULL,
                        source TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    (str(parent / "a.jpg"), str(parent)),
                )
                connection.execute(
                    "INSERT INTO photo_library_files (path, root) VALUES (?, ?)",
                    (str(child / "b.jpg"), str(child)),
                )
                control._ensure_photo_library_index_run_table(connection)
                connection.commit()

            status = control._photo_library_index_status(index_path)

        self.assertEqual(status["roots"], [str(parent)])

    def test_photo_library_index_status_reports_busy_on_sqlite_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "photo_index.sqlite"
            index_path.touch()
            with mock.patch.object(
                control,
                "_ensure_photo_library_index_run_table",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                status = control._photo_library_index_status(index_path)

        self.assertTrue(status["busy"])
        self.assertIn("database is locked", status["error"])


def _write_dedupe_index(index_db: Path, rows: list[dict[str, object]]) -> None:
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
            connection.execute(
                "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, ?)",
                (row["path"], row["date_value"], "media_creation_date"),
            )
        connection.commit()
    finally:
        connection.close()


def _dedupe_row(
    path: Path,
    root: Path,
    extension: str,
    capture_timestamp: str,
    date_value: str,
    duration: float | None = None,
) -> dict[str, object]:
    return {
        "path": str(path),
        "root": str(root),
        "filename": path.name,
        "extension": extension,
        "byte_size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "capture_timestamp": capture_timestamp,
        "date_value": date_value,
        "has_gps": 1,
        "media_width": 1920,
        "media_height": 1080,
        "media_duration_seconds": duration,
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for field in row:
            if field not in fieldnames:
                fieldnames.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
