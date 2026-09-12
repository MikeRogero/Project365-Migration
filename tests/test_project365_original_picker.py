from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import project365_canonical_importer as canonical_importer
import project365_crop_align as crop_align
import project365_original_picker as picker
import project365_original_reference_pipeline as pipeline


class Project365OriginalPickerTests(unittest.TestCase):
    def test_picker_lists_images_and_persists_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            server = picker.serve_picker(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path)),
                "127.0.0.1",
                0,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                entries = _get_json(f"{base_url}/api/entries?status=needs_review")["entries"]
                self.assertEqual(len(entries), 1)
                entry = _get_json(f"{base_url}/api/entry/{entries[0]['entry_id']}")
                self.assertEqual(len(entry["candidates"]), 1)
                self.assertEqual(entry["source_file_type"], "PNG")
                self.assertTrue(int(entry["source_byte_size"]) > 0)
                self.assertEqual(entry["source_dimensions"], "1 x 1")
                self.assertEqual(entry["candidates"][0]["mime_type"], "image/png")
                self.assertTrue(int(entry["candidates"][0]["byte_size"]) > 0)
                self.assertEqual(entry["candidates"][0]["dimensions"], "")
                self.assertEqual(entry["candidates"][0]["folder_path"], str(source_root))
                self.assertEqual(entry["candidates"][0]["folder_label"], f"{base.name} / {source_root.name}")
                facts = _get_json(
                    f"{base_url}/api/candidate-facts?token={entry['candidates'][0]['token']}"
                )
                self.assertEqual(facts["facts"][entry["candidates"][0]["token"]]["dimensions"], "1 x 1")

                source_payload = urllib.request.urlopen(f"{base_url}/image/{entry['source_token']}", timeout=5).read()
                candidate_payload = urllib.request.urlopen(
                    f"{base_url}/image/{entry['candidates'][0]['token']}",
                    timeout=5,
                ).read()
                self.assertTrue(source_payload.startswith(b"\x89PNG"))
                self.assertTrue(candidate_payload.startswith(b"\x89PNG"))

                selected = _post_json(
                    f"{base_url}/api/decision",
                    {
                        "entry_id": entry["entry_id"],
                        "candidate_path": entry["candidates"][0]["path"],
                        "decision": "use_external_original",
                        "notes": "picked in test",
                    },
                )
                self.assertEqual(selected["status"], "selected")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            rows = restarted._entry_rows(entry["entry_id"])
            self.assertEqual(rows[0]["review_decision"], "use_external_original")
            self.assertEqual(rows[0]["review_notes"], "picked in test")

    def test_picker_target_payload_refreshes_imported_people_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )

            before = state.entry_detail("project365:1998-04-12")
            _insert_people(canonical_root, "project365:1998-04-12", ["Alex Example", "Bea Example"])
            listed = state.entry_page(status="needs_review", limit=None)["entries"][0]
            after = state.entry_detail("project365:1998-04-12")

            self.assertEqual(before["people_names"], [])
            self.assertEqual(listed["people_names"], ["Alex Example", "Bea Example"])
            self.assertEqual(after["people_names"], ["Alex Example", "Bea Example"])
            self.assertIn("peopleScript(entry.people_names)", picker.PICKER_HTML)
            self.assertIn('id="targetPeople"', picker.CROP_HTML)

    def test_picker_summary_counts_pending_accepts_and_rejects(self) -> None:
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
            source_root.joinpath("1998-04-13 original.png").write_bytes(_tiny_png())
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            details = {
                entry["entry_date"]: state.entry_detail(entry["entry_id"])
                for entry in state.entries()
            }
            accepted = details["1998-04-12"]
            rejected = details["1998-04-13"]
            state.save_decision(
                accepted["entry_id"],
                accepted["candidates"][0]["path"],
                "use_external_original",
                "",
            )
            state.save_decision(
                rejected["entry_id"],
                rejected["candidates"][0]["path"],
                "rejected",
                "",
            )

            self.assertEqual(
                state.summary()["pending_decisions"],
                {"accepted": 1, "rejected": 1, "associated": 0},
            )

    def test_picker_flags_associated_photo_with_manual_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 associated.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate = detail["candidates"][0]

            choices = state.associated_date_choices(detail["entry_id"], candidate["path"])
            updated = state.save_decision(
                entry_id=detail["entry_id"],
                candidate_path=candidate["path"],
                decision="external_original_associated_photo",
                notes="associated only",
                associated_entry_date="1998-04-13T08:09:10",
                associated_date_source="manual",
            )

            self.assertEqual(choices["choices"], [])
            self.assertEqual(updated["status"], "needs_review")
            rows = state._entry_rows(detail["entry_id"])
            self.assertEqual(rows[0]["review_decision"], "external_original_associated_photo")
            self.assertEqual(rows[0]["associated_entry_date"], "1998-04-13T08:09:10")
            self.assertEqual(state.summary()["pending_decisions"]["associated"], 1)

    def test_associated_date_choices_only_offer_capture_date(self) -> None:
        choices = picker._associated_date_choices_from_row(
            {
                "entry_date": "2015-01-03",
                "capture_timestamp": "2015-01-02T18:16:07",
                "capture_timestamp_source": "exif_datetime_original",
                "filename_dates": "2015-01-02",
                "media_creation_dates": "2015-01-04",
                "filesystem_dates": "2015-01-05",
            }
        )

        self.assertEqual(
            choices,
            [
                {"date": "2015-01-02T18:16:07", "source": "capture"},
            ],
        )

    def test_associated_date_choices_keep_filename_timestamp_provenance(self) -> None:
        choices = picker._associated_date_choices_from_row(
            {
                "entry_date": "2015-01-03",
                "capture_timestamp": "2015-01-02T18:16:07",
                "capture_timestamp_source": "filename_timestamp",
                "filename_dates": "2015-01-02",
                "media_creation_dates": "",
                "filesystem_dates": "",
            }
        )

        self.assertEqual(
            choices,
            [
                {"date": "2015-01-02T18:16:07", "source": "filename_timestamp"},
            ],
        )

    def test_accepted_not_applied_entries_sort_newest_first(self) -> None:
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
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-13 original.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-14 original.png").write_bytes(_tiny_png())
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            self.assertEqual(
                [entry["entry_date"] for entry in state.entries()],
                ["1998-04-12", "1998-04-13", "1998-04-14"],
            )
            for entry in state.entries():
                detail = state.entry_detail(entry["entry_id"])
                state.save_decision(
                    entry["entry_id"],
                    detail["candidates"][0]["path"],
                    "use_external_original",
                    "",
                )

            accepted_dates = [
                entry["entry_date"]
                for entry in state.entry_page(status="accepted_not_applied", limit=None)["entries"]
            ]

            self.assertEqual(accepted_dates, ["1998-04-14", "1998-04-13", "1998-04-12"])

    def test_summary_reports_accepted_entries_separately_from_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 first.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-12 second.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            fieldnames, rows = state._read_rows_with_fieldnames()
            candidate_rows = [row for row in rows if row.get("candidate_path", "").strip()]
            self.assertEqual(len(candidate_rows), 2)
            for row in candidate_rows:
                row["review_decision"] = "use_external_original"
            state._write_rows(fieldnames, rows)

            summary = state.summary()
            accepted_page = state.entry_page(status="accepted_not_applied", limit=None)

            self.assertEqual(summary["pending_decisions"]["accepted"], 2)
            self.assertEqual(summary["pending_entry_counts"]["accepted"], 1)
            self.assertEqual(accepted_page["returned_count"], 1)

    def test_picker_persists_review_crop_offsets_with_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            original = source_root / "1998-04-12 original.jpg"
            original.write_bytes(_jpeg_with_dimensions(80, 60))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            detail = state.entry_detail("project365:1998-04-12")

            updated = state.save_decision(
                "project365:1998-04-12",
                detail["candidates"][0]["path"],
                "use_external_original",
                "crop saved",
                crop={
                    "x": -5,
                    "y": 8,
                    "size": 90,
                    "candidate_width": 80,
                    "candidate_height": 60,
                    "fill_color": "#fefefe",
                    "rotation_degrees": 12.5,
                },
            )

            self.assertEqual(updated["status"], "selected")
            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            rows = restarted._entry_rows("project365:1998-04-12")
            self.assertEqual(rows[0]["review_crop_x"], "-5")
            self.assertEqual(rows[0]["review_crop_y"], "8")
            self.assertEqual(rows[0]["review_crop_size"], "90")
            self.assertEqual(rows[0]["review_crop_candidate_width"], "80")
            self.assertEqual(rows[0]["review_crop_candidate_height"], "60")
            self.assertEqual(rows[0]["review_crop_source"], "manual")
            self.assertEqual(rows[0]["review_crop_fill_color"], "#fefefe")
            self.assertEqual(rows[0]["review_crop_rotation_degrees"], "12.5")

    def test_crop_entry_detail_returns_only_selected_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            selected_original = source_root / "1998-04-12 selected.jpg"
            rejected_original = source_root / "1998-04-12 rejected.jpg"
            selected_original.write_bytes(_jpeg_with_dimensions(80, 60))
            rejected_original.write_bytes(_jpeg_with_dimensions(80, 60))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            entry = state.entry_detail("project365:1998-04-12")
            selected_candidate = next(
                candidate
                for candidate in entry["candidates"]
                if candidate["path"] == str(selected_original)
            )

            state.save_decision(
                "project365:1998-04-12",
                selected_candidate["path"],
                "use_external_original",
                "",
            )
            crop_entry = state.crop_entry_detail("project365:1998-04-12")

            self.assertEqual(crop_entry["status"], "selected")
            self.assertEqual(len(crop_entry["candidates"]), 1)
            self.assertEqual(crop_entry["candidates"][0]["path"], str(selected_original))
            self.assertTrue(crop_entry["candidates"][0]["selected"])

    def test_rejected_candidates_disappear_from_the_visible_picker_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 first.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-12 second.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            entry_id = "project365:1998-04-12"
            before = state.entry_detail(entry_id)

            after_first = state.save_decision(
                entry_id,
                before["candidates"][0]["path"],
                "rejected",
                "",
            )

            self.assertEqual(after_first["status"], "needs_review")
            self.assertEqual(after_first["candidate_count"], 1)
            self.assertEqual(len(after_first["candidates"]), 1)
            self.assertEqual(state.summary()["pending_decisions"]["rejected"], 1)

            after_second = state.save_decision(
                entry_id,
                after_first["candidates"][0]["path"],
                "rejected",
                "",
            )

            self.assertEqual(after_second["status"], "rejected")
            self.assertEqual(after_second["candidate_count"], 0)
            self.assertEqual(after_second["candidates"], [])
            self.assertEqual(state.summary()["pending_decisions"]["rejected"], 2)

    def test_picker_decisions_are_journaled_without_rewriting_the_large_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(search_summary.search_queue_path)
            before = queue_path.read_bytes()
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            entry = state.entry_detail("project365:1998-04-12")

            state.save_decision(
                entry["entry_id"],
                entry["candidates"][0]["path"],
                "use_external_original",
                "journal test",
            )

            self.assertEqual(queue_path.read_bytes(), before)
            self.assertTrue(state._decision_journal_path().exists())
            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            restarted_entry = restarted.entry_detail("project365:1998-04-12")
            self.assertEqual(restarted_entry["status"], "selected")
            self.assertEqual(restarted_entry["candidates"][0]["review_notes"], "journal test")

            result = restarted.apply_decisions()

            self.assertEqual(result["selected_count"], 1)
            self.assertFalse(restarted._decision_journal_path().exists())

    def test_reject_all_candidates_covers_unloaded_candidate_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            for index in range(3):
                source_root.joinpath(f"1998-04-12 candidate-{index}.png").write_bytes(_tiny_png())
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.reject_all_candidates("project365:1998-04-12", "Rejected together.")

            self.assertEqual(result["rejected_count"], 3)
            self.assertEqual(result["entry"]["status"], "rejected")
            self.assertEqual(result["entry"]["candidates"], [])
            self.assertEqual(state.summary()["pending_decisions"]["rejected"], 3)

    def test_reject_all_preserves_other_uncommitted_linked_targets(self) -> None:
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
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-13 original.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-14 original.png").write_bytes(_tiny_png())
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            for entry in state.entries():
                detail = state.entry_detail(entry["entry_id"])
                state.save_decision(
                    entry_id=entry["entry_id"],
                    candidate_path=detail["candidates"][0]["path"],
                    decision="use_external_original",
                    notes="linked pending",
                )

            result = state.reject_all_candidates("project365:1998-04-13", "Rejected current target.")

            self.assertEqual(result["entry"]["status"], "rejected")
            self.assertEqual(
                [entry["entry_id"] for entry in state.entries(status="accepted_not_applied")],
                ["project365:1998-04-14", "project365:1998-04-12"],
            )
            self.assertEqual(state.summary()["pending_decisions"]["accepted"], 2)
            self.assertEqual(state.summary()["pending_decisions"]["rejected"], 1)

    def test_picker_can_manually_expand_indexed_candidate_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            nearby_payload = _jpeg_with_dimensions(18, 12)
            library_root.joinpath("1998-04-15 nearby.jpg").write_bytes(nearby_payload)
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            queue_before = Path(search_summary.search_queue_path).read_bytes()

            with mock.patch.object(picker, "_sha256_path", side_effect=AssertionError("candidate file was hashed")):
                result = state.expand_date_range("project365:1998-04-12", 3)

            self.assertEqual(result["added_count"], 1)
            self.assertEqual(Path(search_summary.search_queue_path).read_bytes(), queue_before)
            self.assertEqual(result["entry"]["candidate_count"], 2)
            self.assertEqual(
                {candidate["filename"] for candidate in result["entry"]["candidates"]},
                {"1998-04-12 exact.jpg", "1998-04-15 nearby.jpg"},
            )
            nearby = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == "1998-04-15 nearby.jpg"
            )
            self.assertIn("manual_range_3_days", nearby["evidence"])
            self.assertIn("manual_range_scope_whole_index", nearby["evidence"])
            self.assertEqual(nearby["date_distance"], "3")
            nearby_row = next(
                row
                for row in state._entry_rows("project365:1998-04-12")
                if row["candidate_filename"] == "1998-04-15 nearby.jpg"
            )
            self.assertEqual(nearby_row["candidate_sha256"], hashlib.sha256(nearby_payload).hexdigest())
            self.assertEqual(result["entry"]["used_range_days"], [3])
            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            restarted_detail = restarted.entry_detail("project365:1998-04-12")
            self.assertEqual(restarted_detail["candidate_count"], 2)
            self.assertEqual(restarted_detail["used_range_days"], [3])

    def test_reject_all_records_current_expanded_range_for_next_easy_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            library_root = base / "library"
            library_root.mkdir()
            library_root.joinpath("1998-04-15 nearby.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            library_root.joinpath("1998-04-17 wider.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            report_dir = canonical_root / "exports" / "verification_reports"
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            state.expand_date_range("project365:1998-04-12", 3)
            result = state.reject_all_candidates("project365:1998-04-12", "Rejected expanded candidates.")

            self.assertEqual(result["rejected_count"], 2)
            range_state = pipeline.load_reject_all_range_state(report_dir)
            self.assertEqual(range_state["project365:1998-04-12"]["rejected_all_range_days"], 3)

    def test_easy_match_auto_expands_rejected_target_to_next_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            library_root = base / "library"
            library_root.mkdir()
            library_root.joinpath("1998-04-15 rejected-nearby.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            library_root.joinpath("1998-04-17 next-range.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            report_dir = canonical_root / "exports" / "verification_reports"
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            state.expand_date_range("project365:1998-04-12", 3)
            state.reject_all_candidates("project365:1998-04-12", "Rejected expanded candidates.")
            state.apply_decisions()

            updated_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            updated_state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(updated_summary.search_queue_path))
            )
            detail = updated_state.entry_detail("project365:1998-04-12")

            self.assertEqual(updated_summary.candidate_count, 1)
            self.assertEqual([candidate["filename"] for candidate in detail["candidates"]], ["1998-04-17 next-range.jpg"])
            self.assertIn("auto_range_5_days", detail["candidates"][0]["evidence"])

    def test_easy_match_auto_expansion_does_not_cross_photo_index_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            first_library = base / "first-library"
            second_library = base / "second-library"
            first_library.mkdir()
            second_library.mkdir()
            first_library.joinpath("1998-04-15 rejected-nearby.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            second_library.joinpath("1998-04-17 different-album.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [first_library, second_library], reset=True)
            report_dir = canonical_root / "exports" / "verification_reports"
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            state.expand_date_range("project365:1998-04-12", 3, photo_index_folder=first_library)
            state.reject_all_candidates(
                "project365:1998-04-12",
                "Rejected expanded candidates.",
                photo_index_folder=first_library,
            )
            state.apply_decisions()

            updated_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
                photo_index_folder=second_library,
                replace_existing_queue=True,
            )
            updated_state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(updated_summary.search_queue_path))
            )
            detail = updated_state.entry_detail("project365:1998-04-12")

            self.assertEqual(updated_summary.candidate_count, 0)
            self.assertEqual(detail["candidates"], [])

    def test_easy_match_stops_auto_expansion_after_rejected_fifteen_day_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            library_root = base / "library"
            library_root.mkdir()
            library_root.joinpath("1998-04-27 rejected-fifteen.jpg").write_bytes(_jpeg_with_dimensions(24, 18))
            library_root.joinpath("1998-05-12 should-stay-manual.jpg").write_bytes(_jpeg_with_dimensions(28, 20))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            report_dir = canonical_root / "exports" / "verification_reports"
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            state.expand_date_range("project365:1998-04-12", 15)
            state.reject_all_candidates("project365:1998-04-12", "Rejected fifteen-day candidates.")
            state.apply_decisions()

            updated_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=report_dir,
                scan_metadata_dates=False,
            )
            updated_state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(updated_summary.search_queue_path))
            )
            detail = updated_state.entry_detail("project365:1998-04-12")

            self.assertEqual(updated_summary.candidate_count, 0)
            self.assertEqual(detail["candidates"], [])
            self.assertEqual(detail["manual_search_message"], pipeline.MANUAL_SEARCH_REQUIRED_MESSAGE)

    def test_picker_expands_indexed_candidate_date_range_with_folder_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            selected_root = base / "Project 365"
            selected_nested = selected_root / "1998" / "April"
            selected_nested.mkdir(parents=True)
            selected_nested.joinpath("1998-04-27 selected.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            other_root = base / "Other Photos"
            other_root.mkdir()
            other_root.joinpath("1998-04-27 outside.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [selected_root, other_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.expand_date_range("project365:1998-04-12", 15, photo_index_folder=selected_root)

            self.assertEqual(result["added_count"], 1)
            self.assertEqual(result["photo_index_folder"], str(selected_root))
            self.assertEqual(
                {candidate["filename"] for candidate in result["entry"]["candidates"]},
                {"1998-04-12 exact.jpg", "1998-04-27 selected.jpg"},
            )
            nearby = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == "1998-04-27 selected.jpg"
            )
            self.assertIn("manual_range_15_days", nearby["evidence"])
            self.assertIn("manual_range_scope_folder", nearby["evidence"])

            whole_index = state.expand_date_range(
                "project365:1998-04-12",
                15,
                photo_index_folder=selected_root,
                search_whole_index=True,
            )

            self.assertEqual(whole_index["added_count"], 2)
            self.assertTrue(whole_index["search_whole_index"])
            self.assertTrue(whole_index["replace_candidates"])
            self.assertEqual(whole_index["photo_index_folder"], "")
            self.assertEqual(
                {candidate["filename"] for candidate in whole_index["entry"]["candidates"]},
                {"1998-04-27 selected.jpg", "1998-04-27 outside.jpg"},
            )
            outside = next(
                candidate
                for candidate in whole_index["entry"]["candidates"]
                if candidate["filename"] == "1998-04-27 outside.jpg"
            )
            self.assertIn("manual_range_15_days", outside["evidence"])
            self.assertIn("manual_range_scope_whole_index", outside["evidence"])

            constrained_again = state.expand_date_range(
                "project365:1998-04-12",
                15,
                photo_index_folder=selected_root,
            )
            self.assertFalse(constrained_again["search_whole_index"])
            self.assertFalse(constrained_again["replace_candidates"])
            self.assertEqual(
                {candidate["filename"] for candidate in constrained_again["entry"]["candidates"]},
                {"1998-04-12 exact.jpg", "1998-04-27 selected.jpg"},
            )

    def test_picker_default_index_search_respects_folder_and_whole_index_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            selected_root = base / "Project 365"
            selected_root.mkdir()
            selected_root.joinpath("1998-04-12 selected.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            other_root = base / "Other Photos"
            other_root.mkdir()
            other_root.joinpath("1998-04-12 outside.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [selected_root, other_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            constrained = state.expand_default_date_range(
                "project365:1998-04-12",
                photo_index_folder=selected_root,
            )
            whole_index = state.expand_default_date_range(
                "project365:1998-04-12",
                photo_index_folder=selected_root,
                search_whole_index=True,
            )

            self.assertEqual(constrained["added_count"], 1)
            self.assertFalse(constrained["search_whole_index"])
            self.assertEqual(constrained["photo_index_folder"], str(selected_root))
            self.assertEqual(whole_index["added_count"], 2)
            self.assertTrue(whole_index["search_whole_index"])
            self.assertTrue(whole_index["replace_candidates"])
            self.assertEqual(whole_index["photo_index_folder"], "")
            self.assertEqual(
                {candidate["filename"] for candidate in whole_index["entry"]["candidates"]},
                {"1998-04-12 selected.jpg", "1998-04-12 outside.jpg"},
            )
            selected = next(
                candidate
                for candidate in whole_index["entry"]["candidates"]
                if candidate["filename"] == "1998-04-12 selected.jpg"
            )
            outside = next(
                candidate
                for candidate in whole_index["entry"]["candidates"]
                if candidate["filename"] == "1998-04-12 outside.jpg"
            )
            self.assertNotIn("manual_default_scope_folder", selected["evidence"])
            self.assertIn("manual_default_scope_whole_index", selected["evidence"])
            self.assertIn("manual_default_scope_whole_index", outside["evidence"])

            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )
            restarted_detail = restarted.entry_detail("project365:1998-04-12", rank_if_needed=False)
            self.assertIsNotNone(restarted_detail)
            self.assertEqual(
                {candidate["filename"] for candidate in restarted_detail["candidates"]},
                {"1998-04-12 selected.jpg", "1998-04-12 outside.jpg"},
            )

    def test_picker_index_expansions_do_not_block_on_visual_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            library_root.joinpath("1998-04-12 default.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            library_root.joinpath("1998-04-13 range.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            library_root.joinpath("1998-05-01 manual.jpg").write_bytes(_jpeg_with_dimensions(22, 16))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            with mock.patch.object(
                state,
                "_rank_oversized_entry_if_needed",
                side_effect=AssertionError("date expansion should not run visual ranking"),
            ):
                state.expand_default_date_range("project365:1998-04-12", search_whole_index=True)
                state.expand_date_range("project365:1998-04-12", 1, search_whole_index=True)
                state.search_index_date_range(
                    "project365:1998-04-12",
                    "1998-05-01",
                    "1998-05-01",
                    search_whole_index=True,
                )

    def test_picker_whole_index_range_replaces_prior_whole_index_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            library_root.joinpath("1998-04-13 plus-one.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            library_root.joinpath("1998-04-15 plus-three.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            plus_one = state.expand_date_range("project365:1998-04-12", 1, search_whole_index=True)
            plus_three = state.expand_date_range("project365:1998-04-12", 3, search_whole_index=True)

            self.assertTrue(plus_one["replace_candidates"])
            self.assertTrue(plus_three["replace_candidates"])
            self.assertEqual(
                [candidate["filename"] for candidate in plus_one["entry"]["candidates"]],
                ["1998-04-13 plus-one.jpg"],
            )
            self.assertEqual(
                {candidate["filename"] for candidate in plus_three["entry"]["candidates"]},
                {"1998-04-13 plus-one.jpg", "1998-04-15 plus-three.jpg"},
            )
            paths = [candidate["path"] for candidate in plus_three["entry"]["candidates"]]
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(plus_three["candidate_count"], 2)

    def test_picker_default_whole_index_filename_only_uses_filename_dates_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            filename_date = library_root / "1998-04-12 filename.jpg"
            filename_date.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_only = library_root / "unrelated.jpg"
            filesystem_only.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_timestamp = dt.datetime(1998, 4, 12, 12, 0, 0).timestamp()
            os.utime(filesystem_only, (filesystem_timestamp, filesystem_timestamp))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.expand_default_date_range(
                "project365:1998-04-12",
                search_whole_index=True,
                whole_index_filename_only=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertTrue(result["search_whole_index"])
            self.assertTrue(result["whole_index_filename_only"])
            self.assertNotIn(
                filesystem_only.name,
                {candidate["filename"] for candidate in result["entry"]["candidates"]},
            )
            candidate = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == filename_date.name
            )
            self.assertIn("filename_date", candidate["evidence"])
            self.assertIn("manual_default_date_source_filename_only", candidate["evidence"])

    def test_picker_whole_index_date_range_includes_filesystem_only_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            filesystem_only = library_root / "unrelated.jpg"
            filesystem_only.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_timestamp = dt.datetime(1998, 4, 15, 12, 0, 0).timestamp()
            os.utime(filesystem_only, (filesystem_timestamp, filesystem_timestamp))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.expand_date_range(
                "project365:1998-04-12",
                3,
                search_whole_index=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertTrue(result["search_whole_index"])
            candidate = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == filesystem_only.name
            )
            self.assertIn("filesystem_date", candidate["evidence"])
            self.assertIn("manual_range_scope_whole_index", candidate["evidence"])

    def test_picker_whole_index_date_range_filename_only_excludes_filesystem_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            filename_date = library_root / "1998-04-15 filename.jpg"
            filename_date.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_only = library_root / "unrelated.jpg"
            filesystem_only.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_timestamp = dt.datetime(1998, 4, 15, 12, 0, 0).timestamp()
            os.utime(filesystem_only, (filesystem_timestamp, filesystem_timestamp))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.expand_date_range(
                "project365:1998-04-12",
                3,
                search_whole_index=True,
                whole_index_filename_only=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertTrue(result["whole_index_filename_only"])
            self.assertNotIn(
                filesystem_only.name,
                {candidate["filename"] for candidate in result["entry"]["candidates"]},
            )
            candidate = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == filename_date.name
            )
            self.assertIn("manual_range_date_source_filename_only", candidate["evidence"])

    def test_picker_searches_custom_index_dates_with_constrained_default_and_whole_index_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            selected_root = base / "Project 365"
            selected_root.mkdir()
            selected_root.joinpath("1998-05-01 selected.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            other_root = base / "Other Photos"
            other_root.mkdir()
            other_root.joinpath("1998-05-01 outside.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [selected_root, other_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            constrained = state.search_index_date_range(
                "project365:1998-04-12",
                "1998-05-01",
                "1998-05-01",
                photo_index_folder=selected_root,
            )
            whole_index = state.search_index_date_range(
                "project365:1998-04-12",
                "1998-05-01",
                "1998-05-01",
                search_whole_index=True,
                photo_index_folder=selected_root,
            )

            self.assertEqual(constrained["added_count"], 1)
            self.assertEqual(constrained["photo_index_folder"], str(selected_root))
            self.assertFalse(constrained["search_whole_index"])
            self.assertEqual(whole_index["added_count"], 2)
            self.assertEqual(whole_index["photo_index_folder"], "")
            self.assertTrue(whole_index["search_whole_index"])
            self.assertTrue(whole_index["replace_candidates"])
            self.assertEqual(
                {candidate["filename"] for candidate in whole_index["entry"]["candidates"]},
                {"1998-05-01 selected.jpg", "1998-05-01 outside.jpg"},
            )
            outside = next(
                candidate
                for candidate in whole_index["entry"]["candidates"]
                if candidate["filename"] == "1998-05-01 outside.jpg"
            )
            self.assertIn("manual_index_date_search", outside["evidence"])
            self.assertEqual(outside["date_distance"], "19")

    def test_picker_custom_index_date_search_includes_filesystem_only_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            filesystem_only = library_root / "unrelated.jpg"
            filesystem_only.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_timestamp = dt.datetime(1998, 5, 1, 12, 0, 0).timestamp()
            os.utime(filesystem_only, (filesystem_timestamp, filesystem_timestamp))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.search_index_date_range(
                "project365:1998-04-12",
                "1998-05-01",
                "1998-05-01",
                search_whole_index=True,
            )

            self.assertEqual(result["added_count"], 1)
            candidate = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == filesystem_only.name
            )
            self.assertIn("filesystem_date", candidate["evidence"])
            self.assertIn("manual_index_date_search", candidate["evidence"])

    def test_picker_custom_index_date_search_filename_only_excludes_filesystem_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            filename_date = library_root / "1998-05-01 filename.jpg"
            filename_date.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_only = library_root / "unrelated.jpg"
            filesystem_only.write_bytes(_jpeg_with_dimensions(18, 12))
            filesystem_timestamp = dt.datetime(1998, 5, 1, 12, 0, 0).timestamp()
            os.utime(filesystem_only, (filesystem_timestamp, filesystem_timestamp))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.search_index_date_range(
                "project365:1998-04-12",
                "1998-05-01",
                "1998-05-01",
                search_whole_index=True,
                whole_index_filename_only=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertTrue(result["whole_index_filename_only"])
            self.assertNotIn(
                filesystem_only.name,
                {candidate["filename"] for candidate in result["entry"]["candidates"]},
            )
            candidate = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == filename_date.name
            )
            self.assertIn("filename_date", candidate["evidence"])
            self.assertIn("manual_index_date_source_filename_only", candidate["evidence"])

    def test_picker_rejects_invalid_custom_index_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "source"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 exact.jpg").write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            with self.assertRaisesRegex(ValueError, "End date"):
                state.search_index_date_range("project365:1998-04-12", "1998-05-02", "1998-05-01")

            detail = state.entry_detail("project365:1998-04-12")
            self.assertEqual([candidate["filename"] for candidate in detail["candidates"]], ["1998-04-12 exact.jpg"])

    def test_picker_exposes_batches_for_visual_filtering(self) -> None:
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
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )

            batches = state.batches()

        self.assertTrue(batches["exists"])
        self.assertEqual(len(batches["batches"]), 1)
        self.assertEqual(batches["batches"][0]["batch_id"], "B001")
        self.assertIn("project365:1998-04-12", batches["batches"][0]["entry_ids"])

    def test_picker_archives_completed_batches_from_active_batch_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            batch_path = canonical_root / "exports" / "verification_reports" / "batches.csv"
            with batch_path.open("w", newline="") as handle:
                fieldnames = [
                    "batch_id",
                    "start_date",
                    "end_date",
                    "date_count",
                    "entry_count",
                    "candidate_count",
                    "entry_ids",
                    "entry_dates",
                ]
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {
                        "batch_id": "B001",
                        "start_date": "1998-04-12",
                        "end_date": "1998-04-12",
                        "date_count": "1",
                        "entry_count": "1",
                        "candidate_count": "0",
                        "entry_ids": "project365:1998-04-12",
                        "entry_dates": "1998-04-12",
                    }
                )
                writer.writerow(
                    {
                        "batch_id": "B002",
                        "start_date": "1998-04-13",
                        "end_date": "1998-04-13",
                        "date_count": "1",
                        "entry_count": "1",
                        "candidate_count": "0",
                        "entry_ids": "project365:1998-04-13",
                        "entry_dates": "1998-04-13",
                    }
                )
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=queue_path,
                    batch_plan_path=batch_path,
                )
            )

            batches = state.batches()

            self.assertEqual([batch["batch_id"] for batch in batches["batches"]], ["B001"])
            self.assertEqual(batches["archived_count"], 1)
            self.assertEqual(batches["total_count"], 2)

    def test_picker_apply_decisions_persists_and_prunes_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            detail = state.entry_detail("project365:1998-04-12")
            self.assertIsNotNone(detail)
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=detail["candidates"][0]["path"],
                decision="use_external_original",
                notes="apply test",
            )
            accepted_not_applied = state.entries(status="accepted_not_applied")

            self.assertEqual([entry["entry_id"] for entry in accepted_not_applied], ["project365:1998-04-12"])

            result = state.apply_decisions()

            self.assertEqual(result["selected_count"], 1)
            self.assertEqual(result["applied_count"], 1)
            self.assertEqual(result["removed_completed_entries"], 1)
            self.assertIsNone(state.entry_detail("project365:1998-04-12"))
            self.assertEqual(state.entries(status="needs_action"), [])
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                row = connection.execute(
                    """
                    SELECT role, review_status
                    FROM media_assets
                    WHERE entry_id = ?
                    AND role = 'external_original_reference'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()
            self.assertEqual(row, ("external_original_reference", "confirmed"))
            fieldnames, _rows = state._read_rows_with_fieldnames()
            stale_row = {field: "" for field in fieldnames}
            stale_row.update(
                {
                    "entry_id": "project365:1998-04-12",
                    "entry_date": "1998-04-12",
                    "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                    "candidate_path": detail["candidates"][0]["path"],
                    "candidate_filename": Path(detail["candidates"][0]["path"]).name,
                }
            )
            state._write_rows(fieldnames, [stale_row])
            self.assertEqual(state.entries(status="needs_action"), [])
            self.assertIsNone(state.entry_detail("project365:1998-04-12"))
            missing_crop_entries = state.crop_entries(crop_filter="missing")
            self.assertEqual([entry["entry_id"] for entry in missing_crop_entries], ["project365:1998-04-12"])
            self.assertEqual(missing_crop_entries[0]["crop_source_state"], "applied")
            candidate_path = missing_crop_entries[0]["candidate_path"]

            cropped = state.save_crop(
                "project365:1998-04-12",
                candidate_path,
                {"x": 0, "y": 0, "size": 1, "candidate_width": 1, "candidate_height": 1},
            )

            self.assertTrue(cropped["crop_has_crop"])
            self.assertEqual(state.crop_entries(crop_filter="missing"), [])
            self.assertEqual([entry["entry_id"] for entry in state.crop_entries(crop_filter="with_crop")], ["project365:1998-04-12"])
            self.assertEqual(state.crop_entries(crop_filter="estimated"), [])
            self.assertEqual([entry["entry_id"] for entry in state.crop_entries(crop_filter="confirmed")], ["project365:1998-04-12"])
            self.assertEqual(state.pending_crop_commits()["pending_count"], 1)

            reset = state.reset_crop("project365:1998-04-12", candidate_path)
            self.assertFalse(reset["crop_has_crop"])

    def test_picker_commit_entry_decision_applies_only_that_linked_target(self) -> None:
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
            source_root.joinpath("1998-04-13 original.png").write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            details = {
                entry["entry_id"]: state.entry_detail(entry["entry_id"])
                for entry in state.entries()
            }
            for entry_id, detail in details.items():
                state.save_decision(
                    entry_id=entry_id,
                    candidate_path=detail["candidates"][0]["path"],
                    decision="use_external_original",
                    notes="linked pending",
                )

            result = state.commit_entry_decision("project365:1998-04-12")

            self.assertEqual(result["selected_count"], 1)
            self.assertIsNone(state.entry_detail("project365:1998-04-12"))
            self.assertEqual(
                [entry["entry_id"] for entry in state.entries(status="accepted_not_applied")],
                ["project365:1998-04-13"],
            )
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                confirmed_entries = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT entry_id
                        FROM media_assets
                        WHERE role = 'external_original_reference'
                        AND review_status = 'confirmed'
                        ORDER BY entry_id
                        """
                    ).fetchall()
                ]
            self.assertEqual(confirmed_entries, ["project365:1998-04-12"])

    def test_applied_crop_edits_are_staged_until_explicit_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate_path = detail["candidates"][0]["path"]
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=candidate_path,
                decision="use_external_original",
                notes="apply first",
            )
            state.apply_decisions()

            staged = state.save_crop(
                "project365:1998-04-12",
                candidate_path,
                {
                    "x": 2,
                    "y": 3,
                    "size": 40,
                    "candidate_width": 80,
                    "candidate_height": 60,
                    "fill_color": "#112233",
                    "rotation_degrees": 1.5,
                },
            )

            self.assertTrue(staged["crop_has_crop"])
            self.assertEqual(state.pending_crop_commits()["pending_count"], 1)
            self.assertEqual(state.crop_entries(crop_filter="missing"), [])
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                transformation_text = connection.execute(
                    """
                    SELECT transformation_json
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
            self.assertIsNone(picker._review_crop_from_transformation_text(transformation_text))

            with mock.patch("project365_original_picker.suggest_crop") as suggest_again:
                no_work = state.start_crop_estimate_batch(apply_estimates=True)

            self.assertEqual(no_work["status"], "pass")
            self.assertEqual(no_work["target_count"], 0)
            self.assertEqual(no_work["message"], "No missing crop estimates to run.")
            suggest_again.assert_not_called()

            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            self.assertEqual(restarted.pending_crop_commits()["pending_count"], 1)
            self.assertEqual(restarted.crop_entries(crop_filter="missing"), [])

            result = restarted.commit_staged_crops()

            self.assertEqual(result["saved_count"], 1)
            self.assertEqual(result["remaining_count"], 0)
            self.assertFalse(restarted._crop_staging_path().exists())
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                transformation_text = connection.execute(
                    """
                    SELECT transformation_json
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
            crop = picker._review_crop_from_transformation_text(transformation_text)
            self.assertEqual(crop["x"], 2)
            self.assertEqual(crop["y"], 3)
            self.assertEqual(crop["size"], 40)
            self.assertEqual(crop["fill_color"], "#112233")
            self.assertEqual(crop["rotation_degrees"], 1.5)

    def test_crop_entries_uses_lightweight_database_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            original = source_root / "1998-04-12 original.jpg"
            original.write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate_path = detail["candidates"][0]["path"]
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=candidate_path,
                decision="use_external_original",
                notes="apply first",
            )
            state.apply_decisions()

            with (
                mock.patch.object(picker, "_image_dimensions", side_effect=AssertionError("full image facts loaded")),
                mock.patch.object(picker, "_has_embedded_geolocation", side_effect=AssertionError("full image facts loaded")),
            ):
                entries = state.crop_entries(crop_filter="missing")

            self.assertEqual([entry["entry_id"] for entry in entries], ["project365:1998-04-12"])
            self.assertEqual(entries[0]["candidate_filename"], original.name)
            self.assertTrue(entries[0]["source_token"])

    def test_crop_estimate_batch_previews_missing_database_crops_without_applying(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate_path = detail["candidates"][0]["path"]
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=candidate_path,
                decision="use_external_original",
                notes="apply first",
            )
            state.apply_decisions()

            suggestion = crop_align.CropSuggestion(
                x=10,
                y=5,
                width=40,
                height=40,
                score=1.0,
                confidence="high",
                reference_width=1,
                reference_height=1,
                candidate_width=80,
                candidate_height=60,
            )
            with mock.patch("project365_original_picker.suggest_crop", return_value=suggestion) as suggest:
                job = state.start_crop_estimate_batch()
                finished = _wait_for_crop_estimate_job(state, job["id"])

            self.assertEqual(finished["status"], "pass")
            self.assertFalse(finished["apply_estimates"])
            self.assertEqual(finished["target_count"], 1)
            self.assertEqual(finished["estimated_count"], 1)
            self.assertEqual(finished["failed_count"], 0)
            self.assertEqual(state.pending_crop_commits()["pending_count"], 0)
            suggest.assert_called_once()
            self.assertFalse(state.crop_entry_detail("project365:1998-04-12")["crop_has_crop"])
            self.assertEqual([entry["entry_id"] for entry in state.crop_entries(crop_filter="missing")], ["project365:1998-04-12"])
            self.assertEqual(state.crop_entries(crop_filter="estimated"), [])

    def test_crop_estimate_batch_reuses_existing_active_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=base / "missing_review_queue.csv",
                )
            )
            active_job = {
                "id": "active-crop-job",
                "status": "running",
                "target_count": 1059,
                "processed_count": 50,
                "estimated_count": 50,
                "apply_estimates": True,
            }
            with state._job_lock:
                state._crop_estimate_jobs["active-crop-job"] = dict(active_job)

            with mock.patch.object(state, "crop_entries", side_effect=AssertionError("should reuse active job")):
                result = state.start_crop_estimate_batch(apply_estimates=True)

            self.assertEqual(result, active_job)
            self.assertEqual(state.crop_estimate_jobs(active_only=True), [active_job])

    def test_crop_estimate_batch_stages_saved_estimate_database_crops_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate_path = detail["candidates"][0]["path"]
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=candidate_path,
                decision="use_external_original",
                notes="apply first",
            )
            state.apply_decisions()

            suggestion = crop_align.CropSuggestion(
                x=10,
                y=5,
                width=40,
                height=40,
                score=1.0,
                confidence="high",
                reference_width=1,
                reference_height=1,
                candidate_width=80,
                candidate_height=60,
            )
            with mock.patch("project365_original_picker.suggest_crop", return_value=suggestion) as suggest:
                job = state.start_crop_estimate_batch(apply_estimates=True)
                finished = _wait_for_crop_estimate_job(state, job["id"])

            self.assertEqual(finished["status"], "pass")
            self.assertTrue(finished["apply_estimates"])
            self.assertEqual(finished["target_count"], 1)
            self.assertEqual(finished["estimated_count"], 1)
            self.assertEqual(finished["failed_count"], 0)
            self.assertEqual(state.pending_crop_commits()["pending_count"], 0)
            suggest.assert_called_once()

            estimated_entries = state.crop_entries(crop_filter="estimated")
            self.assertEqual([entry["entry_id"] for entry in estimated_entries], ["project365:1998-04-12"])
            self.assertEqual(estimated_entries[0]["crop_status"], "saved estimate")

            self.assertEqual(state.pending_crop_commits()["pending_count"], 0)

            crop_entry = state.crop_entry_detail("project365:1998-04-12")
            candidate = crop_entry["candidates"][0]
            self.assertEqual(candidate["review_crop_source"], "estimated")
            self.assertEqual(candidate["review_crop_x"], "10")
            self.assertEqual(candidate["review_crop_y"], "5")
            self.assertEqual(candidate["review_crop_size"], "40")
            self.assertEqual(candidate["review_crop_candidate_width"], "80")
            self.assertEqual(candidate["review_crop_candidate_height"], "60")
            self.assertEqual(state.pending_crop_commits()["pending_count"], 1)
            self.assertEqual(state.crop_entries(crop_filter="missing"), [])
            self.assertEqual(state.crop_entries(crop_filter="confirmed"), [])

            reset = state.reset_crop("project365:1998-04-12", candidate_path, preserve_estimate=True)
            self.assertTrue(reset["crop_has_crop"])
            self.assertEqual(reset["candidates"][0]["review_crop_source"], "estimated")
            self.assertEqual(state.pending_crop_commits()["pending_count"], 0)
            estimated_entries = state.crop_entries(crop_filter="estimated")
            self.assertEqual([entry["entry_id"] for entry in estimated_entries], ["project365:1998-04-12"])
            suggest.assert_called_once_with(
                mock.ANY,
                Path(candidate_path),
                candidate_rotation_degrees=0.0,
            )

            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                transformation_text = connection.execute(
                    """
                    SELECT transformation_json
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
            self.assertIsNone(picker._review_crop_from_transformation_text(transformation_text))

    def test_applied_crop_save_ignores_unaccepted_queue_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate_path = detail["candidates"][0]["path"]
            state.save_decision(
                "project365:1998-04-12",
                candidate_path,
                "use_external_original",
                "apply first",
            )
            state.apply_decisions()

            fieldnames, _ = state._read_rows_with_fieldnames()
            duplicate = {field: "" for field in fieldnames}
            duplicate.update(
                {
                    "entry_id": "project365:1998-04-12",
                    "candidate_path": candidate_path,
                    "candidate_filename": Path(candidate_path).name,
                }
            )
            state._write_rows(fieldnames, [duplicate])

            staged = state.save_crop(
                "project365:1998-04-12",
                candidate_path,
                {
                    "x": 2,
                    "y": 3,
                    "size": 40,
                    "candidate_width": 80,
                    "candidate_height": 60,
                },
            )

            self.assertEqual(staged["crop_source_state"], "staged")
            self.assertEqual(state.pending_crop_commits()["pending_count"], 1)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                transformation_text = connection.execute(
                    """
                    SELECT transformation_json
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
            self.assertIsNone(picker._review_crop_from_transformation_text(transformation_text))

            result = state.commit_staged_crops()

            self.assertEqual(result["saved_count"], 1)
            self.assertEqual(result["remaining_count"], 0)

    def test_commit_archives_and_clears_orphan_crop_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=base / "missing_review_queue.csv",
                )
            )
            candidate_path = base / "orphan.jpg"
            crop = {
                "x": 0,
                "y": 0,
                "size": 40,
                "candidate_width": 40,
                "candidate_height": 40,
            }
            state._store_crop_staging(
                "project365:1998-04-12",
                str(candidate_path),
                crop,
            )

            result = state.commit_staged_crops()

            self.assertEqual(result["missing_count"], 1)
            self.assertEqual(result["remaining_count"], 0)
            archive_path = Path(result["missing_archive_path"])
            self.assertTrue(archive_path.exists())
            archived = json.loads(archive_path.read_text())
            self.assertIn("project365:1998-04-12", archived["entries"])
            self.assertFalse(state._crop_staging_path().exists())

    def test_commit_staged_crops_can_be_limited_to_date_scope(self) -> None:
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
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            source_root.joinpath("1998-04-13 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            for entry_id in ("project365:1998-04-12", "project365:1998-04-13"):
                detail = state.entry_detail(entry_id)
                state.save_decision(
                    entry_id=entry_id,
                    candidate_path=detail["candidates"][0]["path"],
                    decision="use_external_original",
                    notes="apply first",
                )
            state.apply_decisions()

            suggestion = crop_align.CropSuggestion(
                x=10,
                y=5,
                width=40,
                height=40,
                score=1.0,
                confidence="high",
                reference_width=1,
                reference_height=1,
                candidate_width=80,
                candidate_height=60,
            )
            with mock.patch("project365_original_picker.suggest_crop", return_value=suggestion):
                job = state.start_crop_estimate_batch(apply_estimates=True)
                finished = _wait_for_crop_estimate_job(state, job["id"])

            self.assertEqual(finished["estimated_count"], 2)
            self.assertEqual(state.pending_crop_commits()["pending_count"], 0)

            state.crop_entry_detail("project365:1998-04-12")
            self.assertEqual(state.pending_crop_commits()["pending_count"], 1)

            committed = state.commit_staged_crops(
                start_date="1998-04-12",
                end_date="1998-04-12",
            )

            self.assertEqual(committed["saved_count"], 1)
            self.assertEqual(committed["remaining_count"], 0)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                rows = connection.execute(
                    """
                    SELECT entry_id, transformation_json
                    FROM media_assets
                    WHERE role = 'external_original_reference'
                    ORDER BY entry_id
                    """
                ).fetchall()
            crops_by_entry = {
                entry_id: picker._review_crop_from_transformation_text(transformation)
                for entry_id, transformation in rows
            }
            self.assertIsNotNone(crops_by_entry["project365:1998-04-12"])
            self.assertIsNone(crops_by_entry["project365:1998-04-13"])

    def test_crop_estimate_batch_reports_partial_failures(self) -> None:
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
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            source_root.joinpath("1998-04-13 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            for entry_id in ("project365:1998-04-12", "project365:1998-04-13"):
                detail = state.entry_detail(entry_id)
                state.save_decision(
                    entry_id=entry_id,
                    candidate_path=detail["candidates"][0]["path"],
                    decision="use_external_original",
                    notes="apply first",
                )
            state.apply_decisions()

            suggestion = crop_align.CropSuggestion(
                x=10,
                y=5,
                width=40,
                height=40,
                score=1.0,
                confidence="high",
                reference_width=1,
                reference_height=1,
                candidate_width=80,
                candidate_height=60,
            )

            def suggest_or_fail(
                reference_path: Path,
                candidate_path: Path,
                **_: object,
            ) -> crop_align.CropSuggestion:
                if "1998-04-13" in candidate_path.name:
                    raise ValueError("cannot align")
                return suggestion

            with mock.patch("project365_original_picker.suggest_crop", side_effect=suggest_or_fail):
                job = state.start_crop_estimate_batch(apply_estimates=True)
                finished = _wait_for_crop_estimate_job(state, job["id"])

            self.assertEqual(finished["status"], "fail")
            self.assertEqual(finished["target_count"], 2)
            self.assertEqual(finished["processed_count"], 2)
            self.assertEqual(finished["estimated_count"], 1)
            self.assertEqual(finished["failed_count"], 1)
            self.assertEqual(finished["errors"][0]["entry_id"], "project365:1998-04-13")
            self.assertIn("cannot align", finished["errors"][0]["error"])
            self.assertEqual(state.pending_crop_commits()["pending_count"], 0)
            self.assertTrue(state.crop_entry_detail("project365:1998-04-12")["crop_has_crop"])
            self.assertEqual(state.pending_crop_commits()["pending_count"], 1)
            self.assertFalse(state.crop_entry_detail("project365:1998-04-13")["crop_has_crop"])

    def test_single_crop_estimate_preserves_current_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate_path = detail["candidates"][0]["path"]
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=candidate_path,
                decision="use_external_original",
                notes="apply first",
            )
            state.apply_decisions()
            suggestion = crop_align.CropSuggestion(
                x=10,
                y=5,
                width=40,
                height=40,
                score=1.0,
                confidence="high",
                reference_width=1,
                reference_height=1,
                candidate_width=80,
                candidate_height=60,
            )

            with mock.patch("project365_original_picker.suggest_crop", return_value=suggestion) as suggest:
                result = state.suggest_crop_for_candidate(
                    "project365:1998-04-12",
                    candidate_path,
                    crop={
                        "rotation_degrees": 90,
                        "fill_color": "#112233",
                    },
                )

            suggest.assert_called_once_with(
                mock.ANY,
                Path(candidate_path),
                candidate_rotation_degrees=90.0,
            )
            self.assertEqual(result["crop"]["rotation_degrees"], 90.0)
            self.assertEqual(result["crop"]["fill_color"], "#112233")
            crop_entry = state.crop_entry_detail("project365:1998-04-12")
            candidate = crop_entry["candidates"][0]
            self.assertEqual(candidate["review_crop_source"], "estimated")
            self.assertEqual(candidate["review_crop_rotation_degrees"], "90")
            self.assertEqual(candidate["review_crop_fill_color"], "#112233")

    def test_crop_entries_exclude_missing_pending_original_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            original = source_root / "1998-04-12 original.png"
            original.write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            detail = state.entry_detail("project365:1998-04-12")
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=detail["candidates"][0]["path"],
                decision="use_external_original",
                notes="pending missing test",
            )
            original.unlink()

            self.assertEqual(state.crop_entries(crop_filter="missing"), [])
            self.assertEqual(state.crop_entries(crop_filter="all"), [])
            self.assertIsNone(state.crop_entry_detail("project365:1998-04-12"))

    def test_crop_entries_exclude_missing_applied_original_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            original = source_root / "1998-04-12 original.png"
            original.write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            detail = state.entry_detail("project365:1998-04-12")
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=detail["candidates"][0]["path"],
                decision="use_external_original",
                notes="applied missing test",
            )
            state.apply_decisions()
            original.unlink()

            self.assertEqual(state.crop_entries(crop_filter="missing"), [])
            self.assertEqual(state.crop_entries(crop_filter="all"), [])
            self.assertIsNone(state.crop_entry_detail("project365:1998-04-12"))

    def test_crop_reject_applied_original_requeues_entry_and_removes_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            detail = state.entry_detail("project365:1998-04-12")
            candidate_path = detail["candidates"][0]["path"]
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=candidate_path,
                decision="use_external_original",
                notes="apply test",
            )
            state.apply_decisions()
            fieldnames, _ = state._read_rows_with_fieldnames()
            stale_row = {field: "" for field in fieldnames}
            stale_row.update(
                {
                    "entry_id": "project365:1998-04-12",
                    "entry_date": "1998-04-12",
                    "candidate_path": candidate_path,
                    "candidate_filename": Path(candidate_path).name,
                }
            )
            state._write_rows(fieldnames, [stale_row])

            result = state.reject_crop_original(
                "project365:1998-04-12",
                candidate_path,
                "wrong original",
            )

            self.assertEqual(result["rejected_count"], 1)
            self.assertTrue(result["staged"])
            self.assertEqual(state.pending_crop_commits()["pending_count"], 1)
            self.assertIsNone(state.crop_entry_detail("project365:1998-04-12"))
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                reference_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                        AND review_status = 'confirmed'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
                rejected_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_rejected'
                        AND review_status = 'rejected'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
            self.assertEqual(reference_count, 1)
            self.assertEqual(rejected_count, 0)
            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            self.assertEqual(restarted.pending_crop_commits()["pending_count"], 1)
            self.assertIsNone(restarted.crop_entry_detail("project365:1998-04-12"))

            committed = restarted.commit_staged_crops()

            self.assertEqual(committed["rejected_count"], 1)
            self.assertEqual(committed["remaining_count"], 0)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                reference_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                        AND review_status = 'confirmed'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
                rejected_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_rejected'
                        AND review_status = 'rejected'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()[0]
            self.assertEqual(reference_count, 0)
            self.assertEqual(rejected_count, 1)
            requeued = restarted.entries(status="needs_action")
            self.assertEqual([entry["entry_id"] for entry in requeued], ["project365:1998-04-12"])
            self.assertEqual(requeued[0]["status"], "search_needed")
            rows = restarted._entry_rows("project365:1998-04-12")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["entry_id"], "project365:1998-04-12")
            self.assertEqual(rows[0]["candidate_path"], "")
            self.assertEqual(rows[0]["review_decision"], "search_needed")

    def test_picker_needs_action_includes_search_needed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=Path(summary.search_queue_path),
                )
            )

            entries = state.entries(status="needs_action")

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["status"], "search_needed")
            self.assertEqual(entries[0]["candidate_count"], 0)

    def test_picker_entries_support_limit_and_offset(self) -> None:
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
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                for date in ("1998-04-14", "1998-04-12", "1998-04-13"):
                    writer.writerow(
                        {
                            "entry_id": f"project365:{date}",
                            "entry_date": date,
                            "project365_media_asset_id": f"project365:{date}:project365_export_png",
                            "review_decision": "search_needed",
                        }
                    )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            first_page = state.entry_page(status="needs_action", limit=2, offset=0)
            second_page = state.entry_page(status="needs_action", limit=2, offset=2)

            self.assertEqual([entry["entry_date"] for entry in first_page["entries"]], ["1998-04-12", "1998-04-13"])
            self.assertTrue(first_page["has_more"])
            self.assertEqual([entry["entry_date"] for entry in second_page["entries"]], ["1998-04-14"])
            self.assertFalse(second_page["has_more"])

    def test_picker_batches_are_sorted_by_oldest_start_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                for date in ("1998-04-13", "1998-04-12"):
                    writer.writerow(
                        {
                            "entry_id": f"project365:{date}",
                            "entry_date": date,
                            "project365_media_asset_id": f"project365:{date}:project365_export_png",
                            "review_decision": "search_needed",
                        }
                    )
            batch_path = canonical_root / "exports" / "verification_reports" / "batches.csv"
            with batch_path.open("w", newline="") as handle:
                fieldnames = [
                    "batch_id",
                    "start_date",
                    "end_date",
                    "date_count",
                    "entry_count",
                    "candidate_count",
                    "entry_ids",
                    "entry_dates",
                ]
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for batch_id, date in (("B002", "1998-04-13"), ("B001", "1998-04-12")):
                    writer.writerow(
                        {
                            "batch_id": batch_id,
                            "start_date": date,
                            "end_date": date,
                            "date_count": "1",
                            "entry_count": "1",
                            "candidate_count": "0",
                            "entry_ids": f"project365:{date}",
                            "entry_dates": date,
                        }
                    )
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=queue_path,
                    batch_plan_path=batch_path,
                )
            )

            batches = state.batches()

            self.assertEqual([batch["batch_id"] for batch in batches["batches"]], ["B001", "B002"])

    def test_picker_builds_twenty_independent_candidate_shards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                for date in ("1998-04-12", "1998-04-13"):
                    writer.writerow(
                        {
                            "entry_id": f"project365:{date}",
                            "entry_date": date,
                            "project365_media_asset_id": f"project365:{date}:project365_export_png",
                            "review_decision": "search_needed",
                        }
                    )
            before = queue_path.read_bytes()
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            entries = state.entries(status="needs_action")

            self.assertEqual(len(entries), 2)
            self.assertEqual(queue_path.read_bytes(), before)
            shard_root = canonical_root / "cache" / "picker_queue_shards"
            manifest = json.loads((shard_root / "manifest.json").read_text())
            shard_dir = shard_root / manifest["build_dir"]
            self.assertEqual(len(list(shard_dir.glob("shard-*.csv"))), 20)

            with queue_path.open("a", newline="") as handle:
                csv.DictWriter(handle, fieldnames=fieldnames).writerow(
                    {
                        "entry_id": "project365:1998-04-14",
                        "entry_date": "1998-04-14",
                        "review_decision": "search_needed",
                    }
                )
            state._queue_shards.invalidate()
            state.entries(status="needs_action")
            self.assertEqual(len([path for path in shard_root.iterdir() if path.is_dir()]), 1)

    def test_picker_shard_manifest_deduplicates_non_contiguous_entry_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(
                base,
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                for entry_id, entry_date, candidate_name in [
                    ("project365:1998-04-12", "1998-04-12", "first.jpg"),
                    ("project365:1998-04-13", "1998-04-13", "other.jpg"),
                    ("project365:1998-04-12", "1998-04-12", "second.jpg"),
                ]:
                    candidate = base / candidate_name
                    candidate.write_bytes(_jpeg_with_dimensions(12, 9))
                    writer.writerow(
                        {
                            "entry_id": entry_id,
                            "entry_date": entry_date,
                            "project365_media_asset_id": f"{entry_id}:project365_export_png",
                            "candidate_path": str(candidate),
                            "candidate_filename": candidate.name,
                            "candidate_sha256": candidate_name,
                            "byte_size": str(candidate.stat().st_size),
                            "mime_type": "image/jpeg",
                            "review_decision": "rejected",
                        }
                    )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            rejected_entries = state.entries(status="rejected")
            rejected_dates = [entry["entry_date"] for entry in rejected_entries]

            self.assertEqual(rejected_dates.count("1998-04-12"), 1)
            self.assertEqual(rejected_dates, ["1998-04-13", "1998-04-12"])

    def test_picker_entry_detail_supports_bounded_candidate_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                for index in range(3):
                    candidate = base / f"candidate-{index}.png"
                    candidate.write_bytes(_tiny_png())
                    writer.writerow(
                        {
                            "entry_id": "project365:1998-04-12",
                            "entry_date": "1998-04-12",
                            "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                            "candidate_path": str(candidate),
                            "candidate_filename": candidate.name,
                            "byte_size": str(candidate.stat().st_size),
                            "mime_type": "image/png",
                        }
                    )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            first_page = state.entry_detail("project365:1998-04-12", candidate_limit=2, candidate_offset=0)
            second_page = state.entry_detail("project365:1998-04-12", candidate_limit=2, candidate_offset=2)

            self.assertEqual(len(first_page["candidates"]), 2)
            self.assertEqual(first_page["candidate_total"], 3)
            self.assertTrue(first_page["candidate_has_more"])
            self.assertEqual(len(second_page["candidates"]), 1)
            self.assertFalse(second_page["candidate_has_more"])

    def test_picker_sorts_and_reports_alignment_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            candidate_a = base / "candidate-a.png"
            candidate_b = base / "candidate-b.png"
            candidate_a.write_bytes(_tiny_png())
            candidate_b.write_bytes(_tiny_png())
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = [
                "entry_id",
                "entry_date",
                "project365_media_asset_id",
                "current_match_status",
                "current_decision",
                "candidate_path",
                "candidate_filename",
                "candidate_sha256",
                "byte_size",
                "mime_type",
                "filename_dates",
                "media_creation_dates",
                "filesystem_dates",
                "evidence",
                "review_decision",
                "review_notes",
                "alignment_score",
                "alignment_confidence",
                "alignment_crop",
                "alignment_reference_size",
                "alignment_candidate_size",
                "alignment_error",
            ]
            with queue_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(candidate_a),
                        "candidate_filename": "candidate-a.png",
                        "candidate_sha256": "a",
                        "byte_size": "1",
                        "mime_type": "image/png",
                        "evidence": "filename_date",
                        "alignment_score": "20.00",
                        "alignment_confidence": "medium",
                        "alignment_crop": "0,0,10,10",
                    }
                )
                writer.writerow(
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(candidate_b),
                        "candidate_filename": "candidate-b.png",
                        "candidate_sha256": "b",
                        "byte_size": "1",
                        "mime_type": "image/png",
                        "evidence": "filename_date",
                        "alignment_score": "10.00",
                        "alignment_confidence": "high",
                        "alignment_crop": "1,2,10,10",
                    }
                )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            entry = state.entry_detail("project365:1998-04-12")

        self.assertIsNotNone(entry)
        candidates = entry["candidates"]
        self.assertEqual([candidate["filename"] for candidate in candidates], ["candidate-a.png", "candidate-b.png"])
        self.assertEqual(candidates[1]["alignment_score"], "10.00")
        self.assertEqual(candidates[1]["alignment_confidence"], "high")
        self.assertEqual(candidates[1]["alignment_crop"], "1,2,10,10")
        self.assertEqual(candidates[1]["mime_type"], "image/png")
        self.assertIn("function formatAlignment(candidate)", picker.PICKER_HTML)

    def test_picker_best_sort_uses_quality_hints_and_file_size_without_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            improved = base / "1998-04-12 improved.jpg"
            original = base / "1998-04-12 original.jpg"
            web = base / "1998-04-12 web.jpg"
            improved.write_bytes(_tiny_png() + b"x" * 10)
            original.write_bytes(_tiny_png() + b"x" * 50)
            web.write_bytes(_tiny_png() + b"x" * 500)
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = [
                "entry_id",
                "entry_date",
                "project365_media_asset_id",
                "current_match_status",
                "current_decision",
                "candidate_path",
                "candidate_filename",
                "candidate_sha256",
                "byte_size",
                "mime_type",
                "filename_dates",
                "media_creation_dates",
                "filesystem_dates",
                "evidence",
                "review_decision",
                "review_notes",
            ]
            with queue_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for path, sha256, evidence in [
                    (web, "web", "filename_date;quality_negative_web"),
                    (original, "original", "filename_date"),
                    (improved, "improved", "filename_date;quality_positive_improved"),
                ]:
                    writer.writerow(
                        {
                            "entry_id": "project365:1998-04-12",
                            "entry_date": "1998-04-12",
                            "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                            "candidate_path": str(path),
                            "candidate_filename": path.name,
                            "candidate_sha256": sha256,
                            "byte_size": str(path.stat().st_size),
                            "mime_type": "image/jpeg",
                            "evidence": evidence,
                        }
                    )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            entry = state.entry_detail("project365:1998-04-12")

        self.assertIsNotNone(entry)
        self.assertEqual(
            [candidate["filename"] for candidate in entry["candidates"]],
            [
                "1998-04-12 improved.jpg",
                "1998-04-12 original.jpg",
                "1998-04-12 web.jpg",
            ],
        )

    def test_picker_hides_export_equivalent_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            hidden_candidate = base / "candidate-copy.png"
            visible_candidate = base / "candidate-original.png"
            hidden_candidate.write_bytes(_tiny_png())
            visible_candidate.write_bytes(_tiny_png())
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = pipeline._alignment_fieldnames(pipeline._search_queue_fieldnames())
            with queue_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(hidden_candidate),
                        "candidate_filename": "candidate-copy.png",
                        "candidate_sha256": "copy",
                        "byte_size": "1",
                        "mime_type": "image/png",
                        "evidence": "filename_date",
                        "candidate_filter_reason": "export_equivalent",
                    }
                )
                writer.writerow(
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(visible_candidate),
                        "candidate_filename": "candidate-original.png",
                        "candidate_sha256": "original",
                        "byte_size": "1",
                        "mime_type": "image/png",
                        "evidence": "filename_date",
                    }
                )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            entries = state.entries(status="needs_review")
            detail = state.entry_detail("project365:1998-04-12")

        self.assertEqual(entries[0]["candidate_count"], 1)
        self.assertEqual(
            [candidate["filename"] for candidate in detail["candidates"]],
            ["candidate-original.png"],
        )

    def test_picker_hides_project365_export_original_image_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            export_dir = base / "Project 365 Export - 2023-09-21 Last use" / "1998-04"
            tiny_export_dir = base / "Project 365 Export - Squared (TINY)"
            external_dir = base / "external"
            export_dir.mkdir(parents=True)
            tiny_export_dir.mkdir(parents=True)
            external_dir.mkdir()
            export_candidate = export_dir / "1998-04-12.png"
            expanded_export_candidate = export_dir / "1998-04-15.png"
            tiny_export_candidate = tiny_export_dir / "1998-04-12.jpg"
            external_candidate = external_dir / "1998-04-12.png"
            export_candidate.write_bytes(_tiny_png())
            expanded_export_candidate.write_bytes(_tiny_png())
            tiny_export_candidate.write_bytes(_jpeg_with_dimensions(12, 9))
            external_candidate.write_bytes(_tiny_png())
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = pipeline._alignment_fieldnames(pipeline._search_queue_fieldnames())
            with queue_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for path, sha256, mime_type in [
                    (export_candidate, "export", "image/png"),
                    (expanded_export_candidate, "expanded-export", "image/png"),
                    (tiny_export_candidate, "tiny-export", "image/jpeg"),
                    (external_candidate, "external", "image/png"),
                ]:
                    writer.writerow(
                        {
                            "entry_id": "project365:1998-04-12",
                            "entry_date": "1998-04-12",
                            "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                            "candidate_path": str(path),
                            "candidate_filename": path.name,
                            "candidate_sha256": sha256,
                            "byte_size": "1",
                            "mime_type": mime_type,
                            "evidence": "filename_date",
                        }
                    )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            entries = state.entries(status="needs_review")
            detail = state.entry_detail("project365:1998-04-12")

        self.assertEqual(entries[0]["candidate_count"], 1)
        self.assertEqual(
            [candidate["path"] for candidate in detail["candidates"]],
            [str(external_candidate)],
        )

    def test_picker_can_mark_entry_as_project365_export_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=Path(summary.search_queue_path),
                )
            )

            detail = state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path="",
                decision="keep_project365_export",
                notes="searched enough",
            )

            self.assertEqual(detail["status"], "fallback")
            self.assertEqual(state.entries(status="needs_action"), [])
            self.assertEqual(
                [entry["entry_id"] for entry in state.entries(status="fallback")],
                ["project365:1998-04-12"],
            )
            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            rows = restarted._entry_rows("project365:1998-04-12")
            self.assertEqual(rows[0]["review_decision"], "keep_project365_export")
            self.assertEqual(rows[0]["review_notes"], "searched enough")

    def test_picker_writes_one_fallback_decision_for_multi_candidate_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 first.png").write_bytes(_tiny_png())
            source_root.joinpath("1998-04-12 second.png").write_bytes(_tiny_png())
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=Path(summary.search_queue_path),
                )
            )

            detail = state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path="",
                decision="keep_project365_export",
                notes="searched enough",
            )

            self.assertEqual(detail["status"], "fallback")
            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path))
            )
            rows = restarted._entry_rows("project365:1998-04-12")
            fallback_rows = [
                row for row in rows if row["review_decision"] == "keep_project365_export"
            ]
            self.assertEqual(len(fallback_rows), 1)

    def test_picker_filters_entries_by_url_entry_ids_and_dates(self) -> None:
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
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            server = picker.serve_picker(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path)),
                "127.0.0.1",
                0,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                by_id = _get_json(
                    f"{base_url}/api/entries?status=needs_review&entry_id=project365%3A1998-04-12"
                )["entries"]
                by_date = _get_json(
                    f"{base_url}/api/entries?status=needs_action&entry_date=1998-04-13"
                )["entries"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual([entry["entry_id"] for entry in by_id], ["project365:1998-04-12"])
        self.assertEqual([entry["entry_id"] for entry in by_date], ["project365:1998-04-13"])
        self.assertEqual(by_date[0]["status"], "search_needed")

    def test_picker_custom_index_date_search_api_adds_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            library_root = base / "library"
            library_root.mkdir()
            library_root.joinpath("1998-05-01 indexed.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            server = picker.serve_picker(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(summary.search_queue_path)),
                "127.0.0.1",
                0,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                result = _post_json(
                    f"{base_url}/api/search-index-date-range",
                    {
                        "entry_id": "project365:1998-04-12",
                        "start_date": "1998-05-01",
                        "end_date": "1998-05-01",
                        "search_whole_index": True,
                    },
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["candidate_count"], 1)
        self.assertTrue(result["replace_candidates"])
        self.assertEqual(
            {candidate["filename"] for candidate in result["entry"]["candidates"]},
            {"1998-05-01 indexed.jpg"},
        )

    def test_picker_crawl_job_updates_search_needed_entry_with_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[source_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            source_root.joinpath("1998-04-12 original.png").write_bytes(_tiny_png())
            state = picker.PickerState(
                picker.PickerConfig(
                    canonical_root=canonical_root,
                    queue_path=Path(summary.search_queue_path),
                    matcher_review_queue_path=base / "missing_review_queue.csv",
                )
            )

            job = state.start_crawl(
                entry_ids=["project365:1998-04-12"],
                search_roots=[str(source_root)],
                scan_metadata_dates=False,
            )
            finished = _wait_for_job(state, job["id"])

            self.assertEqual(finished["status"], "pass")
            self.assertEqual(finished["candidate_count"], 1)
            entry = state.entry_detail("project365:1998-04-12")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["status"], "needs_review")
            self.assertEqual(entry["candidates"][0]["filename"], "1998-04-12 original.png")

    def test_picker_choose_folder_dialog_returns_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = mock.Mock(returncode=0, stdout=f"{temp_dir}\n", stderr="")
            with mock.patch("project365_original_picker.subprocess.run", return_value=result) as run:
                payload = picker._choose_folder_dialog()

        self.assertEqual(payload, {"path": temp_dir})
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertNotIn("activate\n", run.call_args.args[0][2])

    def test_picker_choose_folder_dialog_treats_user_cancel_as_empty_path(self) -> None:
        result = mock.Mock(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")
        with mock.patch("project365_original_picker.subprocess.run", return_value=result):
            payload = picker._choose_folder_dialog()

        self.assertEqual(payload, {"path": ""})

    def test_picker_html_exposes_group_selection_controls_and_thumbnails(self) -> None:
        self.assertIn("Check shown entries", picker.PICKER_HTML)
        self.assertIn("Uncheck entries", picker.PICKER_HTML)
        self.assertIn('<option value="all">All entries</option>', picker.PICKER_HTML)
        self.assertIn('<option value="accepted_not_applied">Accepted not applied</option>', picker.PICKER_HTML)
        self.assertIn('title="Show all batches">All batches</button>', picker.PICKER_HTML)
        self.assertIn('title="Search checked entries">Selected</button>', picker.PICKER_HTML)
        self.assertIn("entry-thumb", picker.PICKER_HTML)
        self.assertIn("shiftKey", picker.PICKER_HTML)
        self.assertIn('aria-label="Open ${escapeHtml(entry.entry_date)}"', picker.PICKER_HTML)
        self.assertIn("grid-template-columns: 360px minmax(0, 1fr)", picker.PICKER_HTML)
        self.assertIn("white-space: nowrap", picker.PICKER_HTML)
        self.assertIn('id="searchPanelToggle"', picker.PICKER_HTML)
        self.assertIn('aria-expanded="false"', picker.PICKER_HTML)
        self.assertIn('id="searchPanelBody" class="search-panel-body" hidden', picker.PICKER_HTML)
        self.assertIn("function setSearchPanelExpanded(expanded)", picker.PICKER_HTML)
        self.assertLess(
            picker.PICKER_HTML.index('id="searchPanelBody" class="search-panel-body" hidden'),
            picker.PICKER_HTML.index('id="crawlStatus" class="crawl-status"'),
        )
        self.assertLess(
            picker.PICKER_HTML.index('id="crawlStatus" class="crawl-status"'),
            picker.PICKER_HTML.index('class="date-range-controls"'),
        )
        self.assertIn("<label>Search folders</label>", picker.PICKER_HTML)
        self.assertIn("font-size: 13px", picker.PICKER_HTML)
        self.assertNotIn('id="folderBrowser"', picker.PICKER_HTML)
        self.assertNotIn("function renderFolderRoots", picker.PICKER_HTML)
        self.assertNotIn("function openFolder", picker.PICKER_HTML)
        self.assertNotIn("function addCurrentFolder", picker.PICKER_HTML)
        self.assertNotIn("folder-option", picker.PICKER_HTML)
        self.assertIn('search_needed: "broaden search"', picker.PICKER_HTML)
        self.assertIn('Needs broader search', picker.PICKER_HTML)
        self.assertNotIn('Needs folder search', picker.PICKER_HTML)
        self.assertIn('id="crawlSelected" class="action-button" title="Search checked entries">Selected</button>', picker.PICKER_HTML)
        self.assertIn("function applyUrlFilters()", picker.PICKER_HTML)
        self.assertIn('params.append("entry_id", entryId)', picker.PICKER_HTML)
        self.assertIn('params.append("entry_date", entryDate)', picker.PICKER_HTML)
        self.assertIn('id="fallbackButton"', picker.PICKER_HTML)
        self.assertIn('id="rejectAllButton"', picker.PICKER_HTML)
        self.assertNotIn('id="estimateCropBatchButton"', picker.PICKER_HTML)
        self.assertNotIn("Estimate crop locations", picker.PICKER_HTML)
        self.assertNotIn("function startCropEstimateBatch()", picker.PICKER_HTML)
        self.assertNotIn("function pollCropEstimateBatch(jobId)", picker.PICKER_HTML)
        self.assertNotIn('fetchJson("/api/crop-estimate-batch"', picker.PICKER_HTML)
        self.assertIn("async function rejectAllCandidates()", picker.PICKER_HTML)
        self.assertIn('>Use Project365 photo</button>', picker.PICKER_HTML)
        self.assertIn('>Reset decisions</button>', picker.PICKER_HTML)
        self.assertIn('data-action="link"', picker.PICKER_HTML)
        self.assertIn('Matches the origional to the target, but does NOT refresh the page', picker.PICKER_HTML)
        self.assertIn('${candidate.selected ? "linked" : ""}', picker.PICKER_HTML)
        self.assertIn('aria-pressed="${candidate.selected ? "true" : "false"}"', picker.PICKER_HTML)
        self.assertIn('${candidate.selected ? "Linked" : "Link"}', picker.PICKER_HTML)
        self.assertIn("background: #7f1d1d;", picker.PICKER_HTML)
        self.assertNotIn('data-action="reject">Reject</button>', picker.PICKER_HTML)
        self.assertIn('data-action="commit-entry"', picker.PICKER_HTML)
        self.assertIn("function commitEntryDecision(entryId, button = null)", picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/commit-entry"', picker.PICKER_HTML)
        self.assertIn('commitEntryDecision(entry.entry_id, event.currentTarget);', picker.PICKER_HTML)
        self.assertIn("if (button) button.disabled = true;", picker.PICKER_HTML)
        self.assertIn("if (state.summaryRefreshTimer) clearTimeout(state.summaryRefreshTimer);", picker.PICKER_HTML)
        self.assertIn("suppressedCommittedEntryIds: new Set()", picker.PICKER_HTML)
        self.assertIn("state.entries = state.entries.filter(entry => entry.entry_id !== entryId);", picker.PICKER_HTML)
        self.assertIn("await loadEntries(nextEntryId, true, committedEntryDate);", picker.PICKER_HTML)
        self.assertNotIn("Commit the linked original for", picker.PICKER_HTML)
        self.assertIn('confirm(`Reset all pending selections, rejections, and notes for ${state.currentEntry.entry_date}?`)', picker.PICKER_HTML)
        self.assertIn('decision: "keep_project365_export"', picker.PICKER_HTML)
        self.assertIn('fallback: "fallback"', picker.PICKER_HTML)
        self.assertIn("function formatPriorMatcher(entry)", picker.PICKER_HTML)
        self.assertIn('decision === "fallback" ? "no clear external original"', picker.PICKER_HTML)
        self.assertIn("prior matcher:", picker.PICKER_HTML)
        self.assertIn('id="batchFilter"', picker.PICKER_HTML)
        self.assertIn('id="batchSummary"', picker.PICKER_HTML)
        self.assertIn("function renderBatchFilter()", picker.PICKER_HTML)
        self.assertIn("function changeBatchFilter()", picker.PICKER_HTML)
        self.assertIn("function renderBatchSummary()", picker.PICKER_HTML)
        self.assertIn('id="applyDecisionsButton"', picker.PICKER_HTML)
        self.assertIn('id="acceptedDecisionCount"', picker.PICKER_HTML)
        self.assertIn('id="associatedDecisionCount"', picker.PICKER_HTML)
        self.assertIn('id="rejectedDecisionCount"', picker.PICKER_HTML)
        self.assertNotIn('id="pendingDecisionCount"', picker.PICKER_HTML)
        self.assertNotIn("#applyDecisionsButton {\n  max-width:", picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/apply-decisions"', picker.PICKER_HTML)
        self.assertNotIn('id="previousEntryButton"', picker.PICKER_HTML)
        self.assertNotIn('id="nextEntryButton"', picker.PICKER_HTML)
        self.assertIn('id="entryPosition"', picker.PICKER_HTML)
        self.assertIn("function currentEntryPositionLabel()", picker.PICKER_HTML)
        self.assertNotIn("function selectAdjacentEntry(direction)", picker.PICKER_HTML)
        self.assertIn('id="candidateFilter"', picker.PICKER_HTML)
        self.assertIn('id="candidateEvidenceFilter"', picker.PICKER_HTML)
        self.assertIn('id="candidateFolderFilter"', picker.PICKER_HTML)
        self.assertIn('id="candidateSort"', picker.PICKER_HTML)
        self.assertIn('id="candidateLocationOnly"', picker.PICKER_HTML)
        self.assertIn("const candidates = Array.isArray(entry?.candidates) ? entry.candidates : [];", picker.PICKER_HTML)
        self.assertIn("function candidateMatchesLocation(candidate)", picker.PICKER_HTML)
        self.assertIn("candidate.has_embedded_geolocation", picker.PICKER_HTML)
        self.assertIn('<option value="capture_time">Capture time</option>', picker.PICKER_HTML)
        self.assertIn('function candidateCaptureTime(candidate)', picker.PICKER_HTML)
        self.assertIn('function candidateCaptureSortTime(candidate)', picker.PICKER_HTML)
        self.assertIn('function candidateFilenameCaptureTimestamp(candidate)', picker.PICKER_HTML)
        self.assertIn('const compact = text.match(/(\\d{4})-(\\d{2})-(\\d{2})[ _-]+(\\d{2})(\\d{2})(\\d{2})/)', picker.PICKER_HTML)
        self.assertIn('const separated = text.match(/(\\d{4})-(\\d{2})-(\\d{2})[ _-]+(\\d{2})-(\\d{2})-(\\d{2})/)', picker.PICKER_HTML)
        self.assertIn("function prepareCandidateTimestampGroups(candidates, sortMode)", picker.PICKER_HTML)
        self.assertIn("function compareTimestampGroups(left, right, sortMode)", picker.PICKER_HTML)
        self.assertIn("function compareWithinTimestampGroup(left, right)", picker.PICKER_HTML)
        self.assertIn("return candidatePixelCount(right) - candidatePixelCount(left)", picker.PICKER_HTML)
        self.assertNotIn("timestampGroupSelected", picker.PICKER_HTML)
        self.assertNotIn("Number(Boolean(right.selected)) - Number(Boolean(left.selected))", picker.PICKER_HTML)
        self.assertIn("prepareCandidateTimestampGroups(filtered, sortMode);", picker.PICKER_HTML)
        self.assertIn('if (sortMode === "capture_time")', picker.PICKER_HTML)
        self.assertNotIn('id="cropSizeSlider"', picker.PICKER_HTML)
        self.assertNotIn('id="cropBox"', picker.PICKER_HTML)
        self.assertNotIn('id="suggestCropButton"', picker.PICKER_HTML)
        self.assertNotIn("Save crop", picker.PICKER_HTML)
        self.assertIn('id="candidateSummary"', picker.PICKER_HTML)
        self.assertIn("const CANDIDATE_EVIDENCE_FILTERS", picker.PICKER_HTML)
        self.assertIn("function filteredAndSortedCandidates(candidates)", picker.PICKER_HTML)
        self.assertIn("function candidateFolderLabel(path)", picker.PICKER_HTML)
        self.assertIn("function candidateFolderGroups(candidates)", picker.PICKER_HTML)
        self.assertIn("function renderCandidateFolderFilter(candidates)", picker.PICKER_HTML)
        self.assertIn("function renderCandidateEvidenceFilter(candidates)", picker.PICKER_HTML)
        self.assertIn("function candidateEvidenceCountBase(candidates)", picker.PICKER_HTML)
        self.assertIn("countBase.filter(candidate => candidateMatchesEvidence(candidate, filter.value)).length", picker.PICKER_HTML)
        self.assertIn("function candidateMatchesFolder(candidate, folderFilter)", picker.PICKER_HTML)
        self.assertIn('document.getElementById("candidateFolderFilter").onchange = renderCandidateGrid', picker.PICKER_HTML)
        self.assertIn('value="entry_date"', picker.PICKER_HTML)
        self.assertIn('value="filename_entry_date"', picker.PICKER_HTML)
        self.assertIn('value="media_entry_date"', picker.PICKER_HTML)
        self.assertIn('value="filesystem_entry_date"', picker.PICKER_HTML)
        self.assertIn("function candidateMatchesEntryDate(candidate, fields)", picker.PICKER_HTML)
        self.assertIn("function splitDateList(value)", picker.PICKER_HTML)
        self.assertIn("function fileTypeLabel(path, mimeType = \"\")", picker.PICKER_HTML)
        self.assertIn("function formatPhotoFacts(fileType, byteSize, dimensions = \"\")", picker.PICKER_HTML)
        self.assertIn("function formatCandidateCardDetails(candidate)", picker.PICKER_HTML)
        self.assertIn("function formatCandidateEvidenceLabel(candidate)", picker.PICKER_HTML)
        self.assertIn("function formatFileSize(byteSize)", picker.PICKER_HTML)
        self.assertIn("formatPhotoFacts(fileTypeLabel(candidate.filename || candidate.path || \"\", candidate.mime_type), candidate.byte_size, candidate.dimensions)", picker.PICKER_HTML)
        self.assertNotIn('${escapeHtml(candidate.evidence || "date evidence")}<br>', picker.PICKER_HTML)
        self.assertNotIn('candidate.capture_timestamp_source ? `${escapeHtml(candidate.capture_timestamp_source.replaceAll("_", " "))}<br>`', picker.PICKER_HTML)
        self.assertIn("formatPhotoFacts(entry.source_file_type, entry.source_byte_size, entry.source_dimensions)", picker.PICKER_HTML)
        self.assertIn("function candidateMatchesEvidence(candidate, evidenceFilter)", picker.PICKER_HTML)
        self.assertIn('value="dimensions"', picker.PICKER_HTML)
        self.assertIn("Largest dimensions", picker.PICKER_HTML)
        self.assertIn("function candidatePixelCount(candidate)", picker.PICKER_HTML)
        self.assertIn("candidatePixelCount(right) - candidatePixelCount(left)", picker.PICKER_HTML)
        self.assertIn('value="quality"', picker.PICKER_HTML)
        self.assertIn('class="candidate-filename-row"', picker.PICKER_HTML)
        self.assertIn('candidate.has_embedded_geolocation ? "has-location" : ""', picker.PICKER_HTML)
        self.assertIn('aria-label="${candidate.has_embedded_geolocation ? "Embedded location metadata" : "No embedded location metadata"}"', picker.PICKER_HTML)
        self.assertIn("color: #98a2b3;", picker.PICKER_HTML)
        self.assertIn("color: #b42318;", picker.PICKER_HTML)
        self.assertIn('id="photoDropTarget"', picker.PICKER_HTML)
        self.assertIn("min-height: 56px;", picker.PICKER_HTML)
        self.assertIn('id="choosePhoto"', picker.PICKER_HTML)
        self.assertIn('href="/"', picker.PICKER_HTML)
        self.assertIn('<div class="topbar">\n      <a class="action-button control-panel-link" href="/" title="Return to the Project365 control panel">Project365 Control Panel</a>', picker.PICKER_HTML)
        self.assertIn('>Project365 Control Panel</a>', picker.PICKER_HTML)
        self.assertIn('id="acceptedDecisionCount"', picker.PICKER_HTML)
        self.assertIn('id="rejectedDecisionCount"', picker.PICKER_HTML)
        self.assertIn('id="defaultDateRange"', picker.PICKER_HTML)
        self.assertIn('data-range-days="1"', picker.PICKER_HTML)
        self.assertIn('data-range-days="3"', picker.PICKER_HTML)
        self.assertIn('data-range-days="5"', picker.PICKER_HTML)
        self.assertIn('data-range-days="15"', picker.PICKER_HTML)
        self.assertIn('data-range-days="30"', picker.PICKER_HTML)
        self.assertLess(
            picker.PICKER_HTML.index('id="defaultDateRange"'),
            picker.PICKER_HTML.index('data-range-days="1"'),
        )
        self.assertIn(".date-range-controls .action-button.used-range", picker.PICKER_HTML)
        self.assertIn(".crawl-status.is-running", picker.PICKER_HTML)
        self.assertIn("async function expandDefaultDateRange()", picker.PICKER_HTML)
        self.assertIn('id="indexSearchFilenameOnly"', picker.PICKER_HTML)
        self.assertIn('class="index-search-options"', picker.PICKER_HTML)
        self.assertIn("> file name only</label>", picker.PICKER_HTML)
        self.assertIn("grid-template-columns: minmax(120px, 1fr) minmax(120px, 1fr) max-content auto", picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/expand-default-date-range"', picker.PICKER_HTML)
        self.assertIn('document.getElementById("defaultDateRange").onclick = expandDefaultDateRange', picker.PICKER_HTML)
        self.assertIn('document.getElementById("indexSearchFilenameOnly").onchange = event => {', picker.PICKER_HTML)
        self.assertIn('if (event.currentTarget.checked) {', picker.PICKER_HTML)
        self.assertIn('wholeIndex.checked = true;', picker.PICKER_HTML)
        self.assertIn('wholeIndex.dataset.userChanged = "1";', picker.PICKER_HTML)
        self.assertIn('setCrawlStatus(`Searching ${scopeLabel} from ${startDate} to ${endDate}.`, true);', picker.PICKER_HTML)
        self.assertIn('setCrawlStatus(`Expanding ${scopeLabel} candidates to ±${days} days.`, true);', picker.PICKER_HTML)
        self.assertIn("function updateDateRangeButtons(entry)", picker.PICKER_HTML)
        self.assertIn("function usedDateRangeDays(entry)", picker.PICKER_HTML)
        self.assertIn("function defaultDateRangeUsed(entry)", picker.PICKER_HTML)
        self.assertIn("function defaultEvidenceMatchesCurrentIndexScope(evidence)", picker.PICKER_HTML)
        self.assertIn("function rangeEvidenceMatchesCurrentIndexScope(evidence)", picker.PICKER_HTML)
        self.assertIn("function currentIndexSearchWholeIndex()", picker.PICKER_HTML)
        self.assertIn("function currentIndexFilenameOnly()", picker.PICKER_HTML)
        self.assertIn("wholeIndexCheckbox.checked = false;", picker.PICKER_HTML)
        self.assertIn("filenameOnlyCheckbox.checked = false;", picker.PICKER_HTML)
        self.assertIn("filenameOnlyCheckbox.disabled = !state.currentEntry;", picker.PICKER_HTML)
        self.assertNotIn("filenameOnlyCheckbox.disabled = !checkbox.checked || !state.currentEntry;", picker.PICKER_HTML)
        self.assertIn("const includeUnscopedRangeState = !state.activePhotoIndexFolder && currentIndexSearchWholeIndex();", picker.PICKER_HTML)
        self.assertIn("if (!rangeEvidenceMatchesCurrentIndexScope(evidence)) continue;", picker.PICKER_HTML)
        self.assertIn("updateDateRangeButtons(state.currentEntry);", picker.PICKER_HTML)
        self.assertIn('button.classList.toggle("used-range", used);', picker.PICKER_HTML)
        self.assertLess(
            picker.PICKER_HTML.index('class="date-range-controls"'),
            picker.PICKER_HTML.index('id="photoDropTarget"'),
        )
        self.assertIn('fetchJson("/api/expand-date-range"', picker.PICKER_HTML)
        self.assertIn("activePhotoIndexFolder", picker.PICKER_HTML)
        self.assertIn('params.get("photo_index_folder")', picker.PICKER_HTML)
        self.assertIn('window.localStorage.setItem("project365.activePhotoIndexFolder"', picker.PICKER_HTML)
        self.assertIn('window.localStorage.getItem("project365.activePhotoIndexFolder")', picker.PICKER_HTML)
        self.assertIn(
            "const payload = {entry_id: entryId, days, search_whole_index: wholeIndex, whole_index_filename_only: filenameOnly};",
            picker.PICKER_HTML,
        )
        self.assertIn("whole_index_filename_only: filenameOnly", picker.PICKER_HTML)
        self.assertIn("if (state.activePhotoIndexFolder && !wholeIndex) payload.photo_index_folder = state.activePhotoIndexFolder;", picker.PICKER_HTML)
        self.assertIn("state.entryDetailCache.set(entryId, result.entry);", picker.PICKER_HTML)
        self.assertNotIn('png-candidate', picker.PICKER_HTML)
        self.assertIn('className = `candidate-card ${candidate.selected ? "selected" : ""}`', picker.PICKER_HTML)
        self.assertIn('<button class="action-button primary" data-action="select">Select</button>', picker.PICKER_HTML)
        self.assertIn('class="action-button ${candidate.associated ? "flagged" : ""}" data-action="flag"', picker.PICKER_HTML)
        self.assertNotIn('data-action="toggle-notes"', picker.PICKER_HTML)
        self.assertNotIn('data-notes-editor hidden', picker.PICKER_HTML)
        self.assertNotIn('function toggleCandidateNotes', picker.PICKER_HTML)
        self.assertNotIn('document.getElementById(notesId).focus();', picker.PICKER_HTML)
        self.assertIn('${escapeHtml(flagButtonLabel(candidate))}', picker.PICKER_HTML)
        self.assertIn('function flagButtonLabel(candidate)', picker.PICKER_HTML)
        self.assertIn('function flagButtonTitle(candidate)', picker.PICKER_HTML)
        self.assertIn('function applyFlagButtonState(button, candidate)', picker.PICKER_HTML)
        self.assertIn('function applyLinkButtonState(card, candidate)', picker.PICKER_HTML)
        self.assertIn('function applyEntryListState(entry)', picker.PICKER_HTML)
        self.assertIn('function capturePickerScroll()', picker.PICKER_HTML)
        self.assertIn('function restorePickerScroll(snapshot)', picker.PICKER_HTML)
        self.assertIn('if (isLink) {', picker.PICKER_HTML)
        self.assertIn('applyEntryListState(entrySummary || state.currentEntry);', picker.PICKER_HTML)
        self.assertIn('applyLinkButtonState(itemCard, item);', picker.PICKER_HTML)
        self.assertIn('restorePickerScroll(linkScrollState);', picker.PICKER_HTML)
        self.assertIn('.entry-commit[aria-hidden="true"]', picker.PICKER_HTML)
        self.assertIn('.action-button.flagged:not(:disabled)', picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/link-candidate"', picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/import-dropped-candidate"', picker.PICKER_HTML)
        self.assertIn("Photo copied and accepted", picker.PICKER_HTML)
        self.assertIn("candidateRenderLimit: 40", picker.PICKER_HTML)
        self.assertIn("candidateRenderObserver: null", picker.PICKER_HTML)
        self.assertIn("function observeCandidateRenderSentinel", picker.PICKER_HTML)
        self.assertIn("visibleCandidates.slice(0, state.candidateRenderLimit)", picker.PICKER_HTML)
        self.assertIn("fetchCandidateFacts(candidateFactPrefetchCandidates(visibleCandidates, renderedCandidates))", picker.PICKER_HTML)
        self.assertIn("function candidateFactPrefetchCandidates(visibleCandidates, renderedCandidates)", picker.PICKER_HTML)
        self.assertIn('if (sortMode === "dimensions") return visibleCandidates;', picker.PICKER_HTML)
        self.assertIn("renderedTimestampKeys.has(candidateTimestampGroupKey(candidate))", picker.PICKER_HTML)
        self.assertNotIn("Load More Candidates", picker.PICKER_HTML)
        self.assertIn('dropTarget.addEventListener("drop", handlePhotoDrop)', picker.PICKER_HTML)
        self.assertNotIn('document.addEventListener("drop", handlePhotoDrop)', picker.PICKER_HTML)
        self.assertIn('function clearUrlEntryScope(status = "needs_action")', picker.PICKER_HTML)
        self.assertIn("function changeStatusFilter()", picker.PICKER_HTML)
        self.assertIn('document.getElementById("filter").onchange = changeStatusFilter', picker.PICKER_HTML)
        self.assertIn('const status = document.getElementById("filter").value;', picker.PICKER_HTML)
        self.assertIn('document.getElementById("batchFilter").value = "";', picker.PICKER_HTML)
        self.assertIn('state.batchEntryIds = [];', picker.PICKER_HTML)
        self.assertIn('state.initialBatchId = "";', picker.PICKER_HTML)
        self.assertIn('clearUrlEntryScope(status);', picker.PICKER_HTML)
        self.assertIn('const scopedEntryDate = state.urlEntryDates[0] || "";', picker.PICKER_HTML)
        self.assertIn("clearUrlEntryScope(filter);", picker.PICKER_HTML)
        self.assertIn("return loadEntries(\"\", false, scopedEntryDate);", picker.PICKER_HTML)
        self.assertIn("const laterEntry = state.entries.find(entry => entry.entry_date > preferredEntryDate);", picker.PICKER_HTML)
        self.assertIn("function resetCandidateScroll()", picker.PICKER_HTML)
        self.assertIn("if (entryChanged) resetCandidateScroll();", picker.PICKER_HTML)
        self.assertIn('document.querySelector(".workspace")', picker.PICKER_HTML)
        self.assertIn('id="commitCropButton"', picker.CROP_HTML)
        self.assertIn('id="pendingCropCommitCount"', picker.CROP_HTML)
        self.assertIn('fetchJson("/api/crop-commit"', picker.CROP_HTML)
        self.assertIn("function commitStagedCrops()", picker.CROP_HTML)

    def test_picker_extracts_common_image_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            png_path = base / "one.png"
            jpg_path = base / "photo.jpg"
            gif_path = base / "animation.gif"
            psd_path = base / "design.psd"
            png_path.write_bytes(_tiny_png())
            jpg_path.write_bytes(_jpeg_with_dimensions(12, 9))
            gif_path.write_bytes(_gif_with_dimensions(320, 240))
            psd_path.write_bytes(_psd_with_dimensions(1024, 768))

            self.assertEqual(picker._image_dimensions(png_path), "1 x 1")
            self.assertEqual(picker._image_dimensions(jpg_path), "12 x 9")
            self.assertEqual(picker._image_dimensions(gif_path), "320 x 240")
            self.assertEqual(picker._image_dimensions(psd_path), "1024 x 768")

    def test_picker_uses_sips_for_unparsed_image_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "photo.heic"
            image_path.write_bytes(b"not a parsed header")
            with mock.patch.object(picker.subprocess, "run") as run:
                run.return_value = mock.Mock(
                    returncode=0,
                    stdout=f"{image_path}\n  pixelWidth: 4032\n  pixelHeight: 3024\n",
                    stderr="",
                )

                self.assertEqual(picker._image_dimensions(image_path), "4032 x 3024")
                run.assert_called_once_with(
                    ["sips", "--getProperty", "pixelWidth", "--getProperty", "pixelHeight", str(image_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )

    def test_picker_detects_embedded_jpeg_geolocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            located_path = base / "located.jpg"
            plain_path = base / "plain.jpg"
            located_path.write_bytes(_jpeg_with_gps_metadata())
            plain_path.write_bytes(_jpeg_with_dimensions(12, 9))

            self.assertTrue(picker._has_embedded_geolocation(located_path))
            self.assertFalse(picker._has_embedded_geolocation(plain_path))
            self.assertFalse(picker._has_embedded_geolocation(base / "missing.jpg"))

    def test_picker_links_original_image_without_copying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            original_path = base / "known-original.png"
            original_path.write_bytes(_tiny_png())
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writerow(
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "review_decision": "search_needed",
                    }
                )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            detail = state.add_linked_candidate("project365:1998-04-12", str(original_path))

            self.assertEqual(detail["candidate_count"], 1)
            self.assertEqual(detail["candidates"][0]["evidence"], "manual_link")
            self.assertFalse(detail["candidates"][0]["selected"])
            self.assertEqual(Path(detail["candidates"][0]["path"]), original_path.resolve())
            self.assertFalse((canonical_root / "picker_drops").exists())

            repeated = state.add_linked_candidate("project365:1998-04-12", str(original_path))
            self.assertEqual(repeated["candidate_count"], 1)

    def test_picker_copies_dropped_image_into_managed_source_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            before_queue = queue_path.read_bytes()
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            detail = state.add_copied_candidate(
                "project365:1998-04-12",
                "known-original.png",
                "image/png",
                _tiny_png(),
            )

            copied_path = (
                base
                / "Source Data"
                / "Original Photos matching Project365 Entries"
                / "1998-04"
                / "known-original.png"
            )
            self.assertEqual(detail["candidate_count"], 1)
            self.assertEqual(detail["status"], "selected")
            self.assertTrue(detail["candidates"][0]["selected"])
            self.assertEqual(detail["candidates"][0]["evidence"], "manual_drop_copy")
            self.assertEqual(Path(detail["candidates"][0]["path"]), copied_path.resolve())
            self.assertEqual(copied_path.read_bytes(), _tiny_png())
            self.assertEqual(queue_path.read_bytes(), before_queue)

            repeated = state.add_copied_candidate(
                "project365:1998-04-12",
                "known-original.png",
                "image/png",
                _tiny_png(),
            )
            self.assertEqual(repeated["candidate_count"], 1)

    def test_picker_preserves_both_dropped_files_with_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            state.add_copied_candidate(
                "project365:1998-04-12", "photo.jpg", "image/jpeg", _jpeg_with_dimensions(12, 9)
            )
            detail = state.add_copied_candidate(
                "project365:1998-04-12", "photo.jpg", "image/jpeg", _jpeg_with_dimensions(20, 15)
            )

            self.assertEqual(detail["candidate_count"], 2)
            self.assertEqual(len({candidate["path"] for candidate in detail["candidates"]}), 2)

    def test_picker_rejects_non_image_drop_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            with self.assertRaisesRegex(ValueError, "supported image"):
                state.add_copied_candidate(
                    "project365:1998-04-12", "notes.txt", "text/plain", b"text"
                )

    def test_picker_rejects_non_image_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            text_path = base / "notes.txt"
            text_path.write_text("text", encoding="utf-8")
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            with self.assertRaisesRegex(ValueError, "supported image"):
                state.add_linked_candidate("project365:1998-04-12", str(text_path))

    def test_picker_rejects_relative_or_missing_image_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            with self.assertRaisesRegex(ValueError, "absolute path"):
                state.add_linked_candidate("project365:1998-04-12", "photo.jpg")
            with self.assertRaisesRegex(ValueError, "does not exist"):
                state.add_linked_candidate("project365:1998-04-12", str(base / "missing.jpg"))

    def test_photo_chooser_hides_raw_macos_errors(self) -> None:
        result = mock.Mock(returncode=1, stderr="Connection Invalid internal details", stdout="")
        with mock.patch.object(picker.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "macOS could not open the photo chooser") as raised:
                picker._choose_photo_dialog()
        self.assertNotIn("Connection Invalid", str(raised.exception))

    def test_picker_formats_candidate_folder_labels(self) -> None:
        self.assertEqual(
            picker._candidate_folder_label("/Volumes/External/Photos/Trip/IMG_0001.jpg"),
            "Photos / Trip",
        )
        self.assertIn('value="alignment"', picker.PICKER_HTML)
        self.assertIn('params.get("batch")', picker.PICKER_HTML)
        self.assertIn('async function loadEntries(preferredEntryId = "", allowScopeFallback = true, preferredEntryDate = "")', picker.PICKER_HTML)
        self.assertIn("payload.entries.filter(entry => !state.suppressedCommittedEntryIds.has(entry.entry_id))", picker.PICKER_HTML)
        self.assertIn('id="loadMoreEntries"', picker.PICKER_HTML)
        self.assertIn("function loadMoreEntries()", picker.PICKER_HTML)
        self.assertIn("function maybeLoadMoreEntriesNearEnd()", picker.PICKER_HTML)
        self.assertIn("index < state.entries.length - 2", picker.PICKER_HTML)
        self.assertIn('params.set("limit", String(state.entryLimit))', picker.PICKER_HTML)
        self.assertIn('confirm_apply_decisions: "apply-reviewed-decisions"', picker.PICKER_HTML)
        self.assertIn("function entryMatchesStatusFilter(entry, status)", picker.PICKER_HTML)
        self.assertNotIn('id="loadMoreCandidates"', picker.PICKER_HTML)
        self.assertNotIn("function loadMoreCandidates()", picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/reject-all"', picker.PICKER_HTML)
        self.assertIn('data-action="flag"', picker.PICKER_HTML)
        self.assertIn('data-associated-panel hidden', picker.PICKER_HTML)
        self.assertIn('data-associated-choices', picker.PICKER_HTML)
        self.assertNotIn('data-associated-save-status', picker.PICKER_HTML)
        self.assertNotIn('.associated-save-status', picker.PICKER_HTML)
        self.assertIn('type="date" data-associated-date', picker.PICKER_HTML)
        self.assertIn('type="time" step="1" data-associated-time', picker.PICKER_HTML)
        self.assertIn('async function showAssociatedDatePanel(candidate, card)', picker.PICKER_HTML)
        self.assertIn('async function saveAssociatedPhotoFlag(candidate, card)', picker.PICKER_HTML)
        self.assertIn('function associatedCaptureDateChoices(choices)', picker.PICKER_HTML)
        self.assertIn('}).slice(0, 1);', picker.PICKER_HTML)
        self.assertIn('function renderAssociatedDateChoices(target, choices, candidate, card)', picker.PICKER_HTML)
        self.assertIn('function associatedDateSourceLabel(source)', picker.PICKER_HTML)
        self.assertIn('function associatedDateInputValue(value)', picker.PICKER_HTML)
        self.assertIn('function associatedTimeInputValue(value)', picker.PICKER_HTML)
        self.assertIn('function normalizeAssociatedManualValue(dateValue, timeValue)', picker.PICKER_HTML)
        self.assertIn('function setAssociatedDateChoiceSelection(card, source, date)', picker.PICKER_HTML)
        self.assertIn('function clearAssociatedDateChoiceSelection(card)', picker.PICKER_HTML)
        self.assertIn('function syncAssociatedDateChoiceSelection(card, candidate)', picker.PICKER_HTML)
        self.assertIn('.associated-date-choice.is-selected:not(:disabled)', picker.PICKER_HTML)
        self.assertIn('button.setAttribute("aria-pressed", "false");', picker.PICKER_HTML)
        self.assertIn('await saveAssociatedPhotoFlagWithDate(candidate, card, firstChoice.date, firstChoice.source);', picker.PICKER_HTML)
        self.assertIn('card.querySelector(\'[data-associated-time]\').oninput = () => clearAssociatedDateChoiceSelection(card);', picker.PICKER_HTML)
        self.assertIn('function formatAssociatedDateValue(value)', picker.PICKER_HTML)
        self.assertIn('Date captured', picker.PICKER_HTML)
        self.assertNotIn('Filename timestamp', picker.PICKER_HTML)
        self.assertNotIn('media: "Media metadata date"', picker.PICKER_HTML)
        self.assertNotIn('filename: "Filename date"', picker.PICKER_HTML)
        self.assertNotIn('filesystem: "Filesystem date"', picker.PICKER_HTML)
        self.assertNotIn('entry: "Entry date"', picker.PICKER_HTML)
        self.assertIn('Save manual time', picker.PICKER_HTML)
        self.assertIn('saveAssociatedPhotoFlagWithDate(candidate, card, choice.date, choice.source)', picker.PICKER_HTML)
        self.assertIn('applyFlagButtonState(card.querySelector(\'[data-action="flag"]\'), candidate);', picker.PICKER_HTML)
        self.assertNotIn('Flag saved.', picker.PICKER_HTML)
        self.assertNotIn('await loadEntry(currentEntryId);', picker.PICKER_HTML)
        self.assertIn('"/api/associated-date-choices"', picker.PICKER_HTML)
        self.assertIn('"external_original_associated_photo"', picker.PICKER_HTML)
        self.assertIn('id="associatedDecisionCount"', picker.PICKER_HTML)
        self.assertIn("entryDetailCache: new Map()", picker.PICKER_HTML)
        self.assertIn("function preloadNextEntry()", picker.PICKER_HTML)
        self.assertIn("new IntersectionObserver", picker.PICKER_HTML)
        self.assertIn('data-src="/image/${candidate.token}?max=640"', picker.PICKER_HTML)
        self.assertIn('?max=96', picker.PICKER_HTML)
        self.assertIn('?max=640', picker.PICKER_HTML)
        self.assertIn("function nextEntryIdAfterCurrent()", picker.PICKER_HTML)
        self.assertIn("function shouldAdvanceAfterDecision(decision, updatedEntry)", picker.PICKER_HTML)
        self.assertIn('decision === "use_external_original"', picker.PICKER_HTML)
        self.assertIn("const currentEntryBeforeSave = state.currentEntry;", picker.PICKER_HTML)
        self.assertIn("Array.isArray(updatedEntry.candidates) ? updatedEntry.candidates : currentCandidates", picker.PICKER_HTML)
        self.assertIn("item.selected = item.path === candidate.path;", picker.PICKER_HTML)
        self.assertIn('decision === "keep_project365_export"', picker.PICKER_HTML)
        self.assertIn('updatedEntry.status === "rejected"', picker.PICKER_HTML)

    def test_crop_html_exposes_separate_crop_confirmation_controls(self) -> None:
        self.assertIn("Crop confirmation", picker.CROP_HTML)
        self.assertIn('<header>\n  <a class="control-panel-link" href="/?step=crop_confirmation" title="Return to the Project365 control panel">Project365 Control Panel</a>\n  <h1>Crop confirmation</h1>', picker.CROP_HTML)
        self.assertIn("Original Project365 target", picker.CROP_HTML)
        self.assertIn("Identified original", picker.CROP_HTML)
        self.assertIn("grid-template-columns: minmax(220px, 320px) minmax(0, 1fr)", picker.CROP_HTML)
        self.assertIn("height: 100vh", picker.CROP_HTML)
        self.assertIn("overflow: hidden", picker.CROP_HTML)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr)", picker.CROP_HTML)
        self.assertIn('class="photo-pane target-pane"', picker.CROP_HTML)
        self.assertIn('class="photo-pane original-pane"', picker.CROP_HTML)
        self.assertIn('id="cropSizeSlider"', picker.CROP_HTML)
        self.assertIn("width: 165px;", picker.CROP_HTML)
        self.assertIn(".controls {\n  display: flex;", picker.CROP_HTML)
        self.assertNotIn(".controls {\n  display: grid;", picker.CROP_HTML)
        self.assertIn("crop_estimate_batch", picker.CROP_HTML)
        self.assertIn("function shouldPreferSavedEstimateFilter(cropFilter, job)", picker.CROP_HTML)
        self.assertIn("function cropEstimateBatchSummary(job)", picker.CROP_HTML)
        self.assertIn("function applyInitialCropFilterFromUrl()", picker.CROP_HTML)
        self.assertIn('state.cropFilterTouched = true;', picker.CROP_HTML)
        self.assertIn("grid-template-columns: 1fr", picker.CROP_HTML)
        self.assertIn("justify-content: start;", picker.CROP_HTML)
        self.assertIn("flex-wrap: nowrap;", picker.CROP_HTML)
        self.assertIn("overflow-x: auto;", picker.CROP_HTML)
        self.assertIn('class="control-group adjustment-controls"', picker.CROP_HTML)
        self.assertIn('class="control-group item-actions"', picker.CROP_HTML)
        self.assertNotIn('class="control-group batch-actions"', picker.CROP_HTML)
        self.assertIn('id="cropRotationSlider"', picker.CROP_HTML)
        self.assertIn('min="-15" max="15" step="0.1"', picker.CROP_HTML)
        self.assertIn('id="cropRotationValue"', picker.CROP_HTML)
        self.assertIn('id="rotateQuarterTurnButton"', picker.CROP_HTML)
        self.assertIn('id="cropBox"', picker.CROP_HTML)
        self.assertIn('id="cropResizeHandle"', picker.CROP_HTML)
        self.assertIn('id="cropPreviewCanvas"', picker.CROP_HTML)
        self.assertIn('id="rejectOriginalButton" class="subtle-danger"', picker.CROP_HTML)
        self.assertNotIn('id="rejectOriginalButton" class="danger"', picker.CROP_HTML)
        self.assertLess(
            picker.CROP_HTML.index('id="cropPreviewCanvas"'),
            picker.CROP_HTML.index('id="rejectOriginalButton"'),
        )
        self.assertIn(".crop-stage img", picker.CROP_HTML)
        self.assertIn("overflow: visible", picker.CROP_HTML)
        self.assertIn("const screenMargin = 32;", picker.CROP_HTML)
        self.assertIn("function rotatedImageCorners(crop)", picker.CROP_HTML)
        self.assertIn("function rotatePoint(point, centerX, centerY, degrees)", picker.CROP_HTML)
        self.assertIn("function boundsForPoints(points)", picker.CROP_HTML)
        self.assertIn("const imageBounds = boundsForPoints(rotatedImageCorners(crop));", picker.CROP_HTML)
        self.assertNotIn("boundsForPoints([...rotatedImageCorners(crop), ...cropCorners(crop)])", picker.CROP_HTML)
        self.assertIn("image.style.width = `${crop.candidate_width * scale}px`;", picker.CROP_HTML)
        self.assertIn("const adjustmentWindow = Math.max(20, Math.round(maxSize * 0.03));", picker.CROP_HTML)
        self.assertIn("slider.min = Math.max(1, state.cropDraft.size - adjustmentWindow);", picker.CROP_HTML)
        self.assertIn("slider.max = Math.min(maxSize, state.cropDraft.size + adjustmentWindow);", picker.CROP_HTML)
        self.assertIn('slider.step = "1";', picker.CROP_HTML)
        self.assertIn("function numberOrDefault(value, defaultValue)", picker.CROP_HTML)
        self.assertIn("const hasExistingGeometry = [existing.x, existing.y, existing.size, existing.candidate_width, existing.candidate_height]", picker.CROP_HTML)
        self.assertIn("x: numberOrDefault(existing.x, Math.round((width - size) / 2))", picker.CROP_HTML)
        self.assertIn("}, {allowOverflow: hasExistingGeometry});", picker.CROP_HTML)
        self.assertNotIn("snapCropToBounds", picker.CROP_HTML)
        self.assertNotIn("snapDistance", picker.CROP_HTML)
        self.assertNotIn("snap:", picker.CROP_HTML)
        self.assertIn("options.freePosition ? roundedX", picker.CROP_HTML)
        self.assertIn("options.freePosition ? roundedY", picker.CROP_HTML)
        self.assertIn("scale: Number.isFinite(scale) && scale > 0 ? scale : 1,", picker.CROP_HTML)
        self.assertIn("}, {allowOverflow: true, freePosition: true});", picker.CROP_HTML)
        self.assertIn('width: 100%;', picker.CROP_HTML)
        self.assertIn('id="cropListHint"', picker.CROP_HTML)
        self.assertIn('document.getElementById("cropStatus").textContent = "";', picker.CROP_HTML)
        self.assertIn('id="fillColorControl"', picker.CROP_HTML)
        self.assertIn('id="cropFillColor"', picker.CROP_HTML)
        self.assertIn('id="cropFillIndicator"', picker.CROP_HTML)
        self.assertIn('id="fillColorSampleButton" class="color-sample-control"', picker.CROP_HTML)
        self.assertIn('disabled>Sample</button>', picker.CROP_HTML)
        self.assertIn("fillColorSampling: false", picker.CROP_HTML)
        self.assertIn("function imagePointFromPointer(event)", picker.CROP_HTML)
        self.assertIn("function sampledImageColor(point)", picker.CROP_HTML)
        self.assertIn("function toggleFillColorSampler()", picker.CROP_HTML)
        self.assertIn("function sampleFillColorFromPointer(event)", picker.CROP_HTML)
        self.assertIn("context.getImageData(0, 0, width, height).data", picker.CROP_HTML)
        self.assertIn("state.cropDraft.fill_color = color;", picker.CROP_HTML)
        self.assertIn('document.getElementById("fillColorSampleButton").onclick = toggleFillColorSampler;', picker.CROP_HTML)
        self.assertIn('document.getElementById("originalImage").onpointerdown = event => {', picker.CROP_HTML)
        self.assertIn('id="minimalFitButton" class="minimal-crop-control"', picker.CROP_HTML)
        self.assertIn("Shortcut: f. Shrink the crop", picker.CROP_HTML)
        self.assertIn('disabled>Minimal fit (f)</button>', picker.CROP_HTML)
        self.assertIn('id="minimalMoveButton" class="minimal-crop-control"', picker.CROP_HTML)
        self.assertIn("Shortcut: m. Move the crop", picker.CROP_HTML)
        self.assertIn('disabled>Minimal move (m)</button>', picker.CROP_HTML)
        self.assertIn("function minimalFitSizeForCrop(crop)", picker.CROP_HTML)
        self.assertIn("function minimalFitCrop(crop)", picker.CROP_HTML)
        self.assertIn("function minimalFitCurrentCrop()", picker.CROP_HTML)
        self.assertIn("function minimalMoveCrop(crop)", picker.CROP_HTML)
        self.assertIn("function closestIntegerCropMove(sourceInterval, rotationDegrees)", picker.CROP_HTML)
        self.assertIn("function minimalMoveCurrentCrop()", picker.CROP_HTML)
        self.assertIn("minimalFitButton.disabled = !hasFill;", picker.CROP_HTML)
        self.assertIn("minimalMoveButton.disabled = !hasFill;", picker.CROP_HTML)
        self.assertIn('document.getElementById("minimalFitButton").onclick = minimalFitCurrentCrop;', picker.CROP_HTML)
        self.assertIn('document.getElementById("minimalMoveButton").onclick = minimalMoveCurrentCrop;', picker.CROP_HTML)
        self.assertIn("function handleCropKeyboardShortcut(event)", picker.CROP_HTML)
        self.assertIn('focusedControl = target.closest?.("input, select, textarea, button, a, [contenteditable=', picker.CROP_HTML)
        self.assertIn('if (key === "f") {', picker.CROP_HTML)
        self.assertIn('handled = runCropShortcutButton("minimalFitButton", minimalFitCurrentCrop);', picker.CROP_HTML)
        self.assertIn('} else if (key === "m") {', picker.CROP_HTML)
        self.assertIn('handled = runCropShortcutButton("minimalMoveButton", minimalMoveCurrentCrop);', picker.CROP_HTML)
        self.assertIn('} else if (event.key === "Enter") {', picker.CROP_HTML)
        self.assertIn('handled = runCropShortcutButton("saveCropButton", saveCropForCurrentCandidate);', picker.CROP_HTML)
        self.assertIn('document.addEventListener("keydown", handleCropKeyboardShortcut);', picker.CROP_HTML)
        self.assertIn('id="cropSizeSliderFrame"', picker.CROP_HTML)
        self.assertIn('class="crop-size-slider-frame"', picker.CROP_HTML)
        self.assertIn(".crop-size-slider-frame.fill-active input", picker.CROP_HTML)
        self.assertIn(".crop-size-slider-frame::after", picker.CROP_HTML)
        self.assertIn(".crop-size-slider-frame.fill-active::after {\n  opacity: 1;", picker.CROP_HTML)
        self.assertIn(".crop-fill-indicator {\n  position: absolute;", picker.CROP_HTML)
        self.assertIn("bottom: 0;", picker.CROP_HTML)
        self.assertIn("height: 5px;", picker.CROP_HTML)
        self.assertIn("background: var(--danger);", picker.CROP_HTML)
        self.assertIn(".crop-fill-indicator.active {\n  opacity: 1;", picker.CROP_HTML)
        self.assertIn('sizeFrame.classList.toggle("fill-active", hasFill);', picker.CROP_HTML)
        self.assertIn('sizeFrame.title = hasFill ? "Crop includes fill outside the original image." : "";', picker.CROP_HTML)
        self.assertIn(".fill-color-control {\n  display: inline-flex;", picker.CROP_HTML)
        self.assertNotIn(".fill-color-control {\n  display: none;", picker.CROP_HTML)
        self.assertLess(
            picker.CROP_HTML.index('id="fillColorControl"'),
            picker.CROP_HTML.index('id="fillColorSampleButton"'),
        )
        self.assertLess(
            picker.CROP_HTML.index('id="fillColorSampleButton"'),
            picker.CROP_HTML.index('id="minimalFitButton"'),
        )
        self.assertLess(
            picker.CROP_HTML.index('id="minimalFitButton"'),
            picker.CROP_HTML.index('id="minimalMoveButton"'),
        )
        self.assertLess(
            picker.CROP_HTML.index('id="minimalMoveButton"'),
            picker.CROP_HTML.index('id="rotateQuarterTurnButton"'),
        )
        self.assertLess(
            picker.CROP_HTML.index('id="rotateQuarterTurnButton"'),
            picker.CROP_HTML.index('id="cropSizeSlider"'),
        )
        self.assertLess(
            picker.CROP_HTML.index('id="saveCropButton"'),
            picker.CROP_HTML.index('id="commitCropButton"'),
        )
        self.assertIn("candidatePath.textContent = candidateFileLabel(candidate);", picker.CROP_HTML)
        self.assertIn("candidatePath.title = candidate.path || candidate.filename || \"\";", picker.CROP_HTML)
        self.assertIn("function candidateFileLabel(candidate)", picker.CROP_HTML)
        self.assertIn("function filenameFromPath(path)", picker.CROP_HTML)
        self.assertIn("input.disabled = true;", picker.CROP_HTML)
        self.assertIn("input.disabled = false;", picker.CROP_HTML)
        self.assertIn('mode: event.target?.id === "cropResizeHandle" ? "resize" : "move"', picker.CROP_HTML)
        self.assertIn('state.cropDrag.mode === "resize"', picker.CROP_HTML)
        self.assertIn("function updateCropPreview()", picker.CROP_HTML)
        self.assertIn("context.drawImage(", picker.CROP_HTML)
        self.assertIn("function rotateCropDraft(rotationDegrees)", picker.CROP_HTML)
        self.assertIn("function rotateCropDraftByQuarterTurn()", picker.CROP_HTML)
        self.assertIn("function fineRotateCropDraft(offsetDegrees)", picker.CROP_HTML)
        self.assertIn("rotation_degrees", picker.CROP_HTML)
        self.assertIn("function cropExtendsBeyondImage(crop)", picker.CROP_HTML)
        self.assertIn("-normalizeRotationDegrees(crop.rotation_degrees)", picker.CROP_HTML)
        self.assertIn("fill_color", picker.CROP_HTML)
        self.assertIn("Estimate crop", picker.CROP_HTML)
        self.assertIn("Reset crop", picker.CROP_HTML)
        self.assertIn('title="Shortcut: Return. Save crop offsets to staging">Save crop (Return)</button>', picker.CROP_HTML)
        self.assertNotIn("Batch estimate missing", picker.CROP_HTML)
        self.assertNotIn("function startCropEstimateBatch()", picker.CROP_HTML)
        self.assertNotIn("function pollCropEstimateBatch(jobId)", picker.CROP_HTML)
        self.assertNotIn("function setCropEstimateBatchStatus(message)", picker.CROP_HTML)
        self.assertNotIn('fetchJson("/api/crop-estimate-batch"', picker.CROP_HTML)
        self.assertNotIn("No missing crop estimates to run.", picker.CROP_HTML)
        self.assertIn("function savedCropSourceLabel(candidate)", picker.CROP_HTML)
        self.assertIn('if (source === "estimated") return "Saved estimate";', picker.CROP_HTML)
        self.assertIn("${cropSource} loaded. Adjust it if needed.", picker.CROP_HTML)
        self.assertNotIn("function autoEstimateCropIfMissing()", picker.CROP_HTML)
        self.assertNotIn("autoEstimateCropIfMissing();", picker.CROP_HTML)
        self.assertNotIn("function alignmentCropForCandidate(candidate)", picker.CROP_HTML)
        self.assertNotIn("alignmentCropForCandidate(candidate)", picker.CROP_HTML)
        self.assertNotIn('document.getElementById("cropStatus").textContent = "Resetting crop before estimating."', picker.CROP_HTML)
        self.assertIn('const existingCrop = state.cropDraft ? {...state.cropDraft} : {};', picker.CROP_HTML)
        self.assertIn('document.getElementById("cropStatus").textContent = "Estimating crop with current rotation."', picker.CROP_HTML)
        self.assertIn("crop: existingCrop", picker.CROP_HTML)
        self.assertIn("Estimated crop staged with current rotation.", picker.CROP_HTML)
        self.assertIn("function shouldPreserveEstimatedCropOnReset()", picker.CROP_HTML)
        self.assertIn('preserve_estimate: preserveEstimate', picker.CROP_HTML)
        self.assertIn("Saved estimate restored. Adjust it if needed.", picker.CROP_HTML)
        self.assertIn("const selected = cropEntryById(requested)", picker.CROP_HTML)
        self.assertIn("function cropEntryAfter(entryId)", picker.CROP_HTML)
        self.assertIn("function ensureOpenCropGroup()", picker.CROP_HTML)
        self.assertIn("function hasOpenVisibleCropMonth()", picker.CROP_HTML)
        self.assertIn("rememberOpenGroups(cropEntryById(state.selectedEntryId) || state.entries[0]);", picker.CROP_HTML)
        self.assertIn("if (collection.has(key) && element) element.open = true;", picker.CROP_HTML)
        self.assertIn('fetchJson(`/api/crop-entries?crop_filter=${encodeURIComponent(cropFilter)}`)', picker.CROP_HTML)
        self.assertIn('fetchJson(`/api/crop-entry/', picker.CROP_HTML)
        self.assertIn('fetchJson("/api/crop"', picker.CROP_HTML)
        self.assertIn('fetchJson("/api/crop-reset"', picker.CROP_HTML)
        self.assertIn('fetchJson("/api/crop-suggestion"', picker.CROP_HTML)
        self.assertIn("function resizeCropDraft(size)", picker.CROP_HTML)
        self.assertIn("}, {allowOverflow: true});", picker.CROP_HTML)
        self.assertIn('document.getElementById("cropSizeSlider").oninput = event => resizeCropDraft(event.target.value);', picker.CROP_HTML)
        self.assertIn('document.getElementById("cropRotationSlider").oninput = event => fineRotateCropDraft(event.target.value);', picker.CROP_HTML)
        self.assertIn('id="cropFilter"', picker.CROP_HTML)
        self.assertIn('<option value="missing">No saved crop data</option>', picker.CROP_HTML)
        self.assertIn('<option value="estimated">Saved estimates</option>', picker.CROP_HTML)
        self.assertIn('<option value="confirmed">User saved crops</option>', picker.CROP_HTML)
        self.assertIn("function groupedEntriesByYearMonth(entries)", picker.CROP_HTML)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AssertionError(exc.read().decode("utf-8")) from exc


def _wait_for_job(state: picker.PickerState, job_id: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        job = state.crawl_job(job_id)
        if job and job["status"] in {"pass", "fail"}:
            return job
        time.sleep(0.05)
    raise AssertionError("crawl job did not finish")


def _wait_for_crop_estimate_job(state: picker.PickerState, job_id: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        job = state.crop_estimate_job(job_id)
        if job and job["status"] in {"pass", "fail"}:
            return job
        time.sleep(0.05)
    raise AssertionError("crop estimate job did not finish")


def _write_queue(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "entry_id",
        "entry_date",
        "project365_media_asset_id",
        "current_match_status",
        "current_decision",
        "candidate_path",
        "candidate_filename",
        "candidate_sha256",
        "byte_size",
        "mime_type",
        "filename_dates",
        "media_creation_dates",
        "filesystem_dates",
        "evidence",
        "review_decision",
        "review_notes",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()


def _append_search_entry(path: Path) -> None:
    with path.open(newline="") as handle:
        fieldnames = list(csv.DictReader(handle).fieldnames or [])
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writerow(
            {
                "entry_id": "project365:1998-04-12",
                "entry_date": "1998-04-12",
                "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                "review_decision": "search_needed",
            }
        )


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


def _insert_people(canonical_root: Path, entry_id: str, names: list[str]) -> None:
    connection = sqlite3.connect(canonical_root / "canonical.db")
    try:
        for name in names:
            connection.execute(
                """
                INSERT INTO people (entry_id, canonical_name, diarium_tag, review_status, source)
                VALUES (?, ?, ?, 'suggested', 'digikam_xmp')
                """,
                (entry_id, name, f"person:{name}"),
            )
        connection.commit()
    finally:
        connection.close()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


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


def _jpeg_with_gps_metadata() -> bytes:
    tiff = (
        b"II\x2a\x00\x08\x00\x00\x00"
        b"\x01\x00"
        b"\x25\x88\x04\x00\x01\x00\x00\x00\x1a\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x01\x00"
        b"\x01\x00\x02\x00\x02\x00\x00\x00N\x00\x00\x00"
        b"\x00\x00\x00\x00"
    )
    exif = b"Exif\x00\x00" + tiff
    return b"\xff\xd8\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif + b"\xff\xd9"


def _gif_with_dimensions(width: int, height: int) -> bytes:
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00\x00\x00"


def _psd_with_dimensions(width: int, height: int) -> bytes:
    return (
        b"8BPS"
        + (1).to_bytes(2, "big")
        + b"\x00" * 6
        + (3).to_bytes(2, "big")
        + height.to_bytes(4, "big")
        + width.to_bytes(4, "big")
        + (8).to_bytes(2, "big")
        + (3).to_bytes(2, "big")
    )


if __name__ == "__main__":
    unittest.main()
