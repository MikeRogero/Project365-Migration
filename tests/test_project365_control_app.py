from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
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

    def test_build_photo_index_step_requires_and_passes_selected_roots(self) -> None:
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
                    }
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

    def test_control_html_delegates_original_review_to_picker(self) -> None:
        self.assertIn("Original-photo review", control.CONTROL_HTML)
        self.assertIn("Review batches, search folders, and choose the identified original in the visual picker.", control.CONTROL_HTML)
        self.assertIn("Crop confirmation", control.CONTROL_HTML)
        self.assertIn("Open crop confirmation", control.CONTROL_HTML)
        self.assertIn("Batch estimate crop", control.CONTROL_HTML)
        self.assertIn('id="estimateCropBatchButton"', control.CONTROL_HTML)
        self.assertIn('id="openCropConfirmationButton"', control.CONTROL_HTML)
        self.assertLess(
            control.CONTROL_HTML.index('id="estimateCropBatchButton"'),
            control.CONTROL_HTML.index('id="openCropConfirmationButton"'),
        )
        self.assertIn("function startCropEstimateBatch()", control.CONTROL_HTML)
        self.assertIn('fetchJson("/api/crop-estimate-batch"', control.CONTROL_HTML)
        self.assertIn('fetchJson(`/api/crop-estimate-batch/', control.CONTROL_HTML)
        self.assertIn("No missing crop estimates to run.", control.CONTROL_HTML)
        self.assertIn('onclick="openCropConfirmation()"', control.CONTROL_HTML)
        self.assertIn('window.location.assign("/crop")', control.CONTROL_HTML)
        self.assertNotIn('<a href="/crop">Open crop confirmation</a>', control.CONTROL_HTML)
        self.assertIn("Review easy matches (0)", control.CONTROL_HTML)
        self.assertIn("Open visual picker", control.CONTROL_HTML)
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
        self.assertIn('id="attemptOverview"', control.CONTROL_HTML)
        self.assertIn("function formatSearchAttempt(", control.CONTROL_HTML)
        self.assertIn("function renderAttemptOverview(", control.CONTROL_HTML)
        self.assertIn("Review dates", control.CONTROL_HTML)
        self.assertIn("Need folders", control.CONTROL_HTML)
        self.assertIn("Hidden export copies", control.CONTROL_HTML)
        self.assertIn("Export copies", control.CONTROL_HTML)
        self.assertIn("hidden_export_equivalent_candidate_count", control.CONTROL_HTML)
        self.assertIn("Review in picker", control.CONTROL_HTML)
        self.assertIn("function formatAttemptScope(", control.CONTROL_HTML)
        self.assertIn("function formatBatchAttempt(batch)", control.CONTROL_HTML)
        self.assertIn("Pending picker decisions", control.CONTROL_HTML)
        self.assertIn("function formatPendingApply(pending)", control.CONTROL_HTML)
        self.assertIn("selected ·", control.CONTROL_HTML)
        self.assertIn("Remainder overview", control.CONTROL_HTML)
        self.assertIn("function formatRemainderOverview(summary)", control.CONTROL_HTML)
        self.assertIn("review-ready", control.CONTROL_HTML)
        self.assertIn("export-copy only", control.CONTROL_HTML)
        self.assertIn("function formatBatchStatuses(statuses)", control.CONTROL_HTML)
        self.assertIn("Choose another folder", control.CONTROL_HTML)
        self.assertIn("Only export-copy candidates", control.CONTROL_HTML)
        self.assertIn("All found candidates rejected", control.CONTROL_HTML)
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
        self.assertIn("<th>Next step</th>", control.CONTROL_HTML)
        self.assertIn("Build photo index", control.CONTROL_HTML)
        self.assertIn('id="photoIndexBox"', control.CONTROL_HTML)
        self.assertIn('id="indexRoots"', control.CONTROL_HTML)
        self.assertIn("function renderPhotoIndexBox(payload)", control.CONTROL_HTML)
        self.assertIn("recent_runs", control.CONTROL_HTML)
        self.assertIn("Index run", control.CONTROL_HTML)
        self.assertIn("indexed ·", control.CONTROL_HTML)
        self.assertIn("function buildPhotoIndex()", control.CONTROL_HTML)
        self.assertIn('result.status === "pass" && step === "build_photo_index"', control.CONTROL_HTML)
        self.assertIn('document.getElementById("indexRoots").value = ""', control.CONTROL_HTML)
        self.assertIn('id="resetPhotoIndex"', control.CONTROL_HTML)
        self.assertIn('id="photoIndexMessage"', control.CONTROL_HTML)
        self.assertIn('role="status"', control.CONTROL_HTML)
        self.assertIn("Leave blank to scan Source Data", control.CONTROL_HTML)
        self.assertIn("function confirmPhotoIndexReplacement()", control.CONTROL_HTML)
        self.assertEqual(control.CONTROL_HTML.count("window.confirm("), 2)
        self.assertIn('reset_confirmation: resetPhotoIndex ? "replace-photo-index" : ""', control.CONTROL_HTML)
        self.assertIn("function setStepMessage(step, message, state", control.CONTROL_HTML)
        self.assertIn("Find easy matches", control.CONTROL_HTML)
        self.assertIn("function runEasyMatch()", control.CONTROL_HTML)
        self.assertIn('await runStep("match_easy_originals", {', control.CONTROL_HTML)
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
        self.assertIn("function statusUrlFor(steps)", control.CONTROL_HTML)
        self.assertIn('if (initialOwnerStep) {', control.CONTROL_HTML)
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
        self.assertIn("aria-busy", control.CONTROL_HTML)
        self.assertIn('id="cropConfirmationOverview"', control.CONTROL_HTML)
        self.assertIn("function renderCropConfirmationBox(payload)", control.CONTROL_HTML)
        self.assertIn("formatCropConfirmationStatus", control.CONTROL_HTML)
        self.assertIn("With crop information", control.CONTROL_HTML)
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

    def test_working_photo_copies_precede_face_tagging(self) -> None:
        original_review = control.CONTROL_HTML.index("Original-photo review")
        crop_confirmation = control.CONTROL_HTML.index("Crop confirmation")
        working_copies = control.CONTROL_HTML.index("Working photo copies")
        face_tagging = control.CONTROL_HTML.index("People / face tagging")

        self.assertLess(original_review, crop_confirmation)
        self.assertLess(crop_confirmation, working_copies)
        self.assertLess(working_copies, face_tagging)
        self.assertIn("Open crop confirmation", control.CONTROL_HTML)
        self.assertIn("Local face-recognition tools such as digiKam", control.CONTROL_HTML)
        self.assertIn("Import digiKam suggestions", control.CONTROL_HTML)
        self.assertIn("digikamXmpRoots", control.CONTROL_HTML)
        self.assertIn("digikamSuggestionsCsv", control.CONTROL_HTML)
        self.assertIn("function importDigiKamPeople()", control.CONTROL_HTML)

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
        self.assertIn('fetchJson(`/picker/api/entries?', html)
        self.assertIn('fetchJson(`/picker/api/entry/', html)
        self.assertIn('fetchJson("/picker/api/choose-folder")', html)
        self.assertIn('fetchJson(`/picker/api/crawl/', html)
        self.assertIn('fetchJson("/picker/api/expand-date-range"', html)
        self.assertIn('fetchJson("/picker/api/expand-default-date-range"', html)
        self.assertIn('fetchJson("/picker/api/search-index-date-range"', html)
        self.assertIn('fetchJson("/picker/api/crop-estimate-batch"', html)
        self.assertIn('fetchJson(`/picker/api/crop-estimate-batch/', html)
        self.assertIn('src="/picker/image/${encodeURIComponent(entry.source_token)}?max=96"', html)
        self.assertIn("`/picker/image/${entry.source_token}?max=1280`", html)
        self.assertNotIn('fetchJson("/api/', html)
        self.assertNotIn('fetchJson(`/api/', html)
        self.assertNotIn('src="/image/${', html)
        self.assertNotIn("`/image/${", html)

    def test_control_embeds_crop_under_same_port(self) -> None:
        html = control._embedded_crop_html()

        self.assertIn('href="/?step=crop_confirmation"', html)
        self.assertIn('fetchJson(`/crop/api/crop-entries?crop_filter=${encodeURIComponent(cropFilter)}`)', html)
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
        picker_state.crop_entries.return_value = [{"entry_id": "project365:1998-04-11"}]
        picker_state.pending_crop_commits.return_value = {"pending_count": 2}
        server = control.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            control.create_handler(state, control.ControlConfig("127.0.0.1", 0, "/picker")),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/crop/api/crop-entries?crop_filter=missing",
                timeout=5,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["entries"], [{"entry_id": "project365:1998-04-11"}])
        self.assertEqual(result["pending_crop_commits"], {"pending_count": 2})
        picker_state.crop_entries.assert_called_once_with(crop_filter="missing")
        picker_state.pending_crop_commits.assert_called_once_with()

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
                    data=b"{}",
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
        self.assertEqual(picker_state.crop_estimate_job.call_count, 3)
        picker_state.crop_estimate_job.assert_called_with("job-1")

    def test_control_status_includes_crop_confirmation_counts(self) -> None:
        state = control.ControlState()
        picker_state = mock.Mock()
        picker_state.crop_entries.return_value = [
            {"entry_id": "project365:1998-04-11", "crop_has_crop": True},
            {"entry_id": "project365:1998-04-12", "crop_has_crop": False},
            {"entry_id": "project365:1998-04-13", "crop_has_crop": False},
        ]
        picker_state.pending_crop_commits.return_value = {"pending_count": 5}
        state._picker_state = picker_state

        status = state.crop_confirmation_status()

        self.assertEqual(
            status,
            {
                "exists": True,
                "total_count": 3,
                "with_crop_count": 1,
                "queued_count": 2,
                "pending_commit_count": 5,
            },
        )
        picker_state.crop_entries.assert_called_once_with(crop_filter="all")
        picker_state.pending_crop_commits.assert_called_once_with()

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
        picker_state.crop_entry_detail.assert_called_once_with("project365:1998-04-11")
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
        )

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
                        "whole_index_filename_only": True,
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
            whole_index_filename_only=True,
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
                        "whole_index_filename_only": True,
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
            whole_index_filename_only=True,
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
        self.assertIn('fetchJson("/api/choose-folder")', control.CONTROL_HTML)
        self.assertNotIn("Browse here", control.CONTROL_HTML)
        self.assertNotIn('id="folderBrowser"', control.CONTROL_HTML)
        self.assertNotIn("function showFolderBrowser()", control.CONTROL_HTML)

    def test_completed_run_progress_is_static(self) -> None:
        self.assertIn(".run-progress.status-pass .spinner", control.CONTROL_HTML)
        self.assertIn(".run-progress.status-pass .progress-bar", control.CONTROL_HTML)
        self.assertIn("function formatActiveWorkflowResult(job)", control.CONTROL_HTML)
        self.assertIn("formatWorkflowResult(latest, Boolean(active))", control.CONTROL_HTML)

    def test_control_html_exposes_stop_button_for_running_jobs(self) -> None:
        self.assertIn('document.querySelector(".run-progress button")', control.CONTROL_HTML)
        self.assertIn("cancelActiveJob()", control.CONTROL_HTML)
        self.assertIn('/cancel`, {method: "POST"}', control.CONTROL_HTML)
        self.assertIn(".run-progress.status-cancelled", control.CONTROL_HTML)

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

    def test_choose_folder_dialog_returns_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = mock.Mock(returncode=0, stdout=f"{temp_dir}\n", stderr="")
            with mock.patch("project365_control_app.subprocess.run", return_value=result) as run:
                payload = control._choose_folder_dialog()

        self.assertEqual(payload, {"path": temp_dir})
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertIn('tell application "Finder" to activate', run.call_args.args[0][2])

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
        self.assertEqual(len(status["original_batch_plan"]["batches"]), 2)
        self.assertEqual(status["original_batch_plan"]["batches"][0]["batch_id"], "batch-0001")
        self.assertEqual(
            status["original_batch_plan"]["batches"][0]["entry_ids"],
            "project365:1998-04-10",
        )
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
        self.assertEqual(
            status["original_batch_plan"]["batches"][1]["next_step"],
            "Try another folder or keep fallback",
        )
        self.assertEqual(status["original_batch_plan"]["batches"][1]["search_attempt_count"], "0")

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
        self.assertEqual(len(status["recent"]), 2)
        self.assertEqual(status["recent"][0]["attempt_id"], "second")
        self.assertEqual(status["recent"][1]["attempt_id"], "first")

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

    def test_database_status_reports_missing_photo_and_original_counts(self) -> None:
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
                        ("rejected-1", "project365:1998-04-11", "external_original_rejected", "rejected"),
                    ],
                )

            status = control._database_status(db_path)

        self.assertEqual(status["project365_missing_photo_count"], 1)
        self.assertEqual(status["identified_original_count"], 1)
        self.assertEqual(status["without_identified_original_count"], 2)
        self.assertEqual(status["without_identified_original_percent"], 66.7)

    def test_control_header_includes_photo_and_original_status_numbers(self) -> None:
        self.assertIn('<span>Missing photos</span>', control.CONTROL_HTML)
        self.assertIn('id="metricMissingPhotos"', control.CONTROL_HTML)
        self.assertIn('<span>Without identified original</span>', control.CONTROL_HTML)
        self.assertIn('id="metricWithoutIdentifiedOriginal"', control.CONTROL_HTML)
        self.assertIn("function formatCountPercent(count, total)", control.CONTROL_HTML)

    def test_database_status_reports_working_copy_readiness_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "canonical.db"
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
                            "/tmp/ready.png",
                            "sha-ready",
                            10,
                            json.dumps(ready_crop, sort_keys=True),
                            "2026-08-20T00:00:00Z",
                        ),
                        (
                            "media-needs-crop",
                            "project365:1998-04-11",
                            "/tmp/needs-crop.png",
                            "sha-needs-crop",
                            20,
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
