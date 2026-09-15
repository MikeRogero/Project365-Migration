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

    def test_picker_links_multiple_associated_photos_without_replacing_selected_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            source_root.joinpath("1998-04-12 extra 1.jpg").write_bytes(_jpeg_with_dimensions(81, 60))
            source_root.joinpath("1998-04-12 extra 2.jpg").write_bytes(_jpeg_with_dimensions(82, 60))
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
            candidates_by_name = {candidate["filename"]: candidate for candidate in detail["candidates"]}

            state.save_decision(
                entry_id=detail["entry_id"],
                candidate_path=candidates_by_name["1998-04-12 original.jpg"]["path"],
                decision="use_external_original",
                notes="primary",
            )
            state.save_decision(
                entry_id=detail["entry_id"],
                candidate_path=candidates_by_name["1998-04-12 extra 1.jpg"]["path"],
                decision="external_original_associated_photo",
                notes="extra",
                associated_entry_date=detail["entry_date"],
                associated_date_source="entry",
            )
            updated = state.save_decision(
                entry_id=detail["entry_id"],
                candidate_path=candidates_by_name["1998-04-12 extra 2.jpg"]["path"],
                decision="external_original_associated_photo",
                notes="extra",
                associated_entry_date=detail["entry_date"],
                associated_date_source="entry",
            )

            rows_by_name = {Path(row["candidate_path"]).name: row for row in state._entry_rows(detail["entry_id"])}
            self.assertEqual(rows_by_name["1998-04-12 original.jpg"]["review_decision"], "use_external_original")
            self.assertEqual(
                rows_by_name["1998-04-12 extra 1.jpg"]["review_decision"],
                "external_original_associated_photo",
            )
            self.assertEqual(
                rows_by_name["1998-04-12 extra 2.jpg"]["review_decision"],
                "external_original_associated_photo",
            )
            self.assertEqual(updated["selected_count"], 1)
            self.assertEqual(updated["associated_count"], 2)
            self.assertEqual(
                state.summary()["pending_decisions"],
                {"accepted": 1, "rejected": 0, "associated": 2},
            )

            result = state.apply_decisions()

            self.assertEqual(result["selected_count"], 1)
            self.assertEqual(result["associated_count"], 2)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                roles = connection.execute(
                    """
                    SELECT role, COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role IN ('external_original_reference', 'external_original_associated_photo')
                    GROUP BY role
                    """,
                    (detail["entry_id"],),
                ).fetchall()
            self.assertEqual(dict(roles), {"external_original_associated_photo": 2, "external_original_reference": 1})

    def test_picker_unlinks_associated_photo_without_clearing_selected_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            source_root.joinpath("1998-04-12 original.jpg").write_bytes(_jpeg_with_dimensions(80, 60))
            source_root.joinpath("1998-04-12 extra.jpg").write_bytes(_jpeg_with_dimensions(81, 60))
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
            candidates_by_name = {candidate["filename"]: candidate for candidate in detail["candidates"]}

            state.save_decision(
                entry_id=detail["entry_id"],
                candidate_path=candidates_by_name["1998-04-12 original.jpg"]["path"],
                decision="use_external_original",
                notes="primary",
            )
            linked = state.save_decision(
                entry_id=detail["entry_id"],
                candidate_path=candidates_by_name["1998-04-12 extra.jpg"]["path"],
                decision="external_original_associated_photo",
                notes="extra",
                associated_entry_date=detail["entry_date"],
                associated_date_source="entry",
            )
            unlinked = state.save_decision(
                entry_id=detail["entry_id"],
                candidate_path=candidates_by_name["1998-04-12 extra.jpg"]["path"],
                decision="unlink_associated_photo",
                notes="",
            )

            rows_by_name = {Path(row["candidate_path"]).name: row for row in state._entry_rows(detail["entry_id"])}
            self.assertEqual(linked["associated_count"], 1)
            self.assertEqual(unlinked["selected_count"], 1)
            self.assertEqual(unlinked["associated_count"], 0)
            self.assertEqual(rows_by_name["1998-04-12 original.jpg"]["review_decision"], "use_external_original")
            self.assertEqual(rows_by_name["1998-04-12 extra.jpg"]["review_decision"], "")
            self.assertEqual(rows_by_name["1998-04-12 extra.jpg"]["associated_entry_date"], "")
            self.assertEqual(rows_by_name["1998-04-12 extra.jpg"]["associated_date_source"], "")
            self.assertEqual(
                state.summary()["pending_decisions"],
                {"accepted": 1, "rejected": 0, "associated": 0},
            )

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
            default_photo_index_folder = base / "Source Data" / "Original Photos matching Project365 Entries"
            default_photo_index_folder.mkdir(parents=True)
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
            self.assertEqual(summary["default_photo_index_folder"], str(default_photo_index_folder.resolve()))
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
            library_root = _project_originals_root(base)
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
            self.assertIn("manual_range_scope_filename_index", nearby["evidence"])
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
            library_root = _project_originals_root(base)
            library_root.joinpath("1998-04-15 nearby.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            library_root.joinpath("1998-04-17 wider.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import ExiftoolPhotoMetadata, build_photo_library_index, default_index_db

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
            library_root = _project_originals_root(base)
            library_root.joinpath("1998-04-15 rejected-nearby.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            library_root.joinpath("1998-04-17 next-range.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import ExiftoolPhotoMetadata, build_photo_library_index, default_index_db

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
            project_originals_root = _project_originals_root(base)
            first_library.mkdir()
            second_library.mkdir()
            project_originals_root.joinpath("1998-04-15 rejected-nearby.jpg").write_bytes(
                _jpeg_with_dimensions(18, 12)
            )
            first_library.joinpath("1998-04-15 rejected-nearby.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            second_library.joinpath("1998-04-17 different-album.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(
                default_index_db(canonical_root),
                [project_originals_root, first_library, second_library],
                reset=True,
            )
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
            library_root = _project_originals_root(base)
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
            selected_root = _project_originals_root(base)
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

            self.assertEqual(result["added_count"], 2)
            self.assertEqual(result["photo_index_folder"], "")
            self.assertEqual(
                {candidate["filename"] for candidate in result["entry"]["candidates"]},
                {"1998-04-12 exact.jpg", "1998-04-27 selected.jpg", "1998-04-27 outside.jpg"},
            )
            nearby = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == "1998-04-27 selected.jpg"
            )
            self.assertIn("manual_range_15_days", nearby["evidence"])
            self.assertIn("manual_range_scope_filename_index", nearby["evidence"])

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
                {"1998-04-12 exact.jpg", "1998-04-27 selected.jpg", "1998-04-27 outside.jpg"},
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
            selected_root = _project_originals_root(base)
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

            self.assertEqual(constrained["added_count"], 2)
            self.assertFalse(constrained["search_whole_index"])
            self.assertEqual(constrained["photo_index_folder"], "")
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
            self.assertNotIn("manual_default_scope_filename_index", selected["evidence"])
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

    def test_picker_reset_decisions_restores_queue_after_whole_index_replacement(self) -> None:
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
            library_root.joinpath("1998-04-12 indexed.jpg").write_bytes(_jpeg_with_dimensions(18, 12))
            library_root.joinpath("1998-04-13 indexed.jpg").write_bytes(_jpeg_with_dimensions(20, 14))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            queue_path = Path(search_summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            whole_index = state.expand_date_range(
                "project365:1998-04-12",
                1,
                search_whole_index=True,
            )
            reset = state.save_decision(
                "project365:1998-04-12",
                "",
                "clear",
                "",
            )

            self.assertTrue(whole_index["replace_candidates"])
            self.assertEqual(
                {candidate["filename"] for candidate in whole_index["entry"]["candidates"]},
                {"1998-04-12 indexed.jpg", "1998-04-13 indexed.jpg"},
            )
            self.assertEqual(
                [candidate["filename"] for candidate in reset["candidates"]],
                ["1998-04-12 indexed.jpg", "1998-04-12 exact.jpg"],
            )
            self.assertNotIn(
                "project365:1998-04-12",
                pipeline.load_reject_all_range_state(queue_path.parent),
            )

            restarted = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            restarted_detail = restarted.entry_detail("project365:1998-04-12", rank_if_needed=False)

            self.assertIsNotNone(restarted_detail)
            self.assertEqual(
                [candidate["filename"] for candidate in restarted_detail["candidates"]],
                ["1998-04-12 indexed.jpg", "1998-04-12 exact.jpg"],
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

            index_db = default_index_db(canonical_root)
            build_photo_library_index(index_db, [library_root], reset=True)
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

    def test_picker_marks_project_originals_source_candidates_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            project_originals_root = base / "Source Data" / "Original Photos matching Project365 Entries"
            project_originals_root.mkdir(parents=True)
            project_candidate = project_originals_root / "1998-04-12 z-source.jpg"
            project_candidate.write_bytes(_jpeg_with_dimensions(12, 9))
            search_summary = pipeline.build_external_original_search_queue(
                canonical_root=canonical_root,
                search_roots=[project_originals_root],
                review_queue_path=base / "missing_review_queue.csv",
                report_dir=canonical_root / "exports" / "verification_reports",
                scan_metadata_dates=False,
            )
            library_root = base / "library"
            library_root.mkdir()
            library_candidate = library_root / "1998-04-12 a-index.jpg"
            library_candidate.write_bytes(_jpeg_with_dimensions(18, 12))
            from project365_photo_library_index import build_photo_library_index, default_index_db

            build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.expand_default_date_range(
                "project365:1998-04-12",
                photo_index_folder=library_root,
            )

            candidates = result["entry"]["candidates"]
            self.assertEqual([candidate["filename"] for candidate in candidates], [project_candidate.name, library_candidate.name])
            self.assertTrue(candidates[0]["project_originals_source"])
            self.assertFalse(candidates[1]["project_originals_source"])
            self.assertEqual(result["added_count"], 1)

            manual_candidate = base / "manually-linked.jpg"
            manual_candidate.write_bytes(_jpeg_with_dimensions(22, 14))
            linked_detail = state.add_linked_candidate("project365:1998-04-12", str(manual_candidate))
            self.assertEqual(
                [candidate["filename"] for candidate in linked_detail["candidates"]],
                [manual_candidate.name, project_candidate.name, library_candidate.name],
            )
            self.assertEqual(linked_detail["candidates"][0]["evidence"], "manual_link")
            self.assertTrue(linked_detail["candidates"][1]["project_originals_source"])

    def test_picker_default_filename_only_uses_filename_dates_only_without_whole_index_scope(self) -> None:
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
            library_root = _project_originals_root(base)
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
                photo_index_folder=library_root,
                filename_dates_only=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertFalse(result["search_whole_index"])
            self.assertTrue(result["filename_dates_only"])
            self.assertFalse(result["whole_index_filename_only"])
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

    def test_picker_whole_index_date_range_excludes_modified_dates_by_default(self) -> None:
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

            index_db = default_index_db(canonical_root)
            build_photo_library_index(index_db, [library_root], reset=True)
            with sqlite3.connect(index_db) as connection:
                connection.execute("DELETE FROM photo_library_dates WHERE file_path = ?", (str(filesystem_only.resolve()),))
                connection.execute(
                    "INSERT INTO photo_library_dates (file_path, date, source) VALUES (?, ?, ?)",
                    (str(filesystem_only.resolve()), "1998-04-15", "filesystem_modified_date"),
                )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=Path(search_summary.search_queue_path))
            )

            result = state.expand_date_range(
                "project365:1998-04-12",
                3,
                search_whole_index=True,
            )

            self.assertEqual(result["added_count"], 0)
            self.assertTrue(result["search_whole_index"])
            self.assertFalse(result["include_modified_dates"])
            self.assertNotIn(
                filesystem_only.name,
                {candidate["filename"] for candidate in result["entry"]["candidates"]},
            )

            with_modified = state.expand_date_range(
                "project365:1998-04-12",
                3,
                search_whole_index=True,
                include_modified_dates=True,
            )

            self.assertEqual(with_modified["added_count"], 1)
            self.assertTrue(with_modified["include_modified_dates"])
            candidate = next(
                candidate
                for candidate in with_modified["entry"]["candidates"]
                if candidate["filename"] == filesystem_only.name
            )
            self.assertIn("filesystem_modified_date", candidate["evidence"])
            self.assertIn("manual_range_date_source_modified_date", candidate["evidence"])
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
                filename_dates_only=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertTrue(result["filename_dates_only"])
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
            selected_root = _project_originals_root(base)
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

            self.assertEqual(constrained["added_count"], 2)
            self.assertEqual(constrained["photo_index_folder"], "")
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

    def test_picker_custom_index_date_search_includes_modified_dates_when_selected(self) -> None:
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
                include_modified_dates=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertTrue(result["include_modified_dates"])
            candidate = next(
                candidate
                for candidate in result["entry"]["candidates"]
                if candidate["filename"] == filesystem_only.name
            )
            self.assertIn("filesystem_modified_date", candidate["evidence"])
            self.assertIn("manual_index_date_source_modified_date", candidate["evidence"])
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
                filename_dates_only=True,
            )

            self.assertEqual(result["added_count"], 1)
            self.assertTrue(result["filename_dates_only"])
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
            self.assertEqual(_read_csv(Path(summary.search_queue_path)), [])
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

    def test_picker_explicit_date_lookup_can_open_completed_database_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "Originals"
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
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=detail["candidates"][0]["path"],
                decision="use_external_original",
                notes="apply test",
            )
            state.apply_decisions()

            self.assertIsNone(state.entry_detail("project365:1998-04-12"))
            page = state.entry_page(status="all", entry_dates={"1998-04-12"})
            reopened = state.entry_detail("project365:1998-04-12", include_database=True)

            self.assertEqual([entry["entry_date"] for entry in page["entries"]], ["1998-04-12"])
            self.assertEqual(page["entries"][0]["status"], "selected")
            self.assertEqual(page["entries"][0]["candidate_count"], 1)
            self.assertEqual(page["entries"][0]["pending_commit_count"], 0)
            self.assertFalse(page["entries"][0]["commit_ready"])
            self.assertIsNotNone(reopened)
            self.assertEqual(reopened["status"], "selected")
            self.assertEqual(reopened["selected_count"], 1)
            self.assertEqual(reopened["pending_commit_count"], 0)
            self.assertFalse(reopened["commit_ready"])
            self.assertEqual(len(reopened["candidates"]), 1)

    def test_picker_explicit_date_range_lookup_loads_canonical_entries(self) -> None:
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
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            page = state.entry_page(
                status="all",
                entry_dates={"1998-04-12", "1998-04-13", "1998-04-14"},
                limit=2,
                offset=0,
            )
            second_page = state.entry_page(
                status="all",
                entry_dates={"1998-04-12", "1998-04-13", "1998-04-14"},
                limit=2,
                offset=2,
            )

            self.assertEqual([entry["entry_date"] for entry in page["entries"]], ["1998-04-12", "1998-04-13"])
            self.assertTrue(page["has_more"])
            self.assertEqual([entry["entry_date"] for entry in second_page["entries"]], ["1998-04-14"])
            self.assertFalse(second_page["has_more"])

    def test_picker_prunes_target_confirmed_by_external_process_on_load(self) -> None:
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
            queue_path = Path(summary.search_queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            fieldnames, queue_rows = state._read_rows_with_fieldnames()
            external_rows = [dict(row) for row in queue_rows if row["entry_id"] == "project365:1998-04-12"]
            external_rows[0]["review_decision"] = "use_external_original"
            reviewed_path = base / "external_decision.csv"
            with reviewed_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(external_rows)
            pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            self.assertIn("project365:1998-04-12", {row["entry_id"] for row in _read_csv(queue_path)})
            detail = state.entry_detail("project365:1998-04-12")

            self.assertIsNone(detail)
            self.assertEqual(
                {row["entry_id"] for row in _read_csv(queue_path)},
                {"project365:1998-04-13"},
            )
            self.assertEqual(
                [entry["entry_id"] for entry in state.entries(status="needs_action")],
                ["project365:1998-04-13"],
            )

    def test_picker_apply_prunes_stale_pending_selection_after_external_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            source_root = base / "external"
            source_root.mkdir()
            stale_path = source_root / "1998-04-12 stale.png"
            winner_path = source_root / "1998-04-12 winner.png"
            stale_path.write_bytes(_tiny_png())
            winner_path.write_bytes(_tiny_png() + b"winner")
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
            stale_candidate = next(candidate for candidate in detail["candidates"] if candidate["path"] == str(stale_path))
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=stale_candidate["path"],
                decision="use_external_original",
                notes="stale local selection",
            )
            fieldnames, queue_rows = state._read_rows_with_fieldnames()
            winner_rows = [dict(row) for row in queue_rows if row["candidate_path"] == str(winner_path)]
            winner_rows[0]["review_decision"] = "use_external_original"
            reviewed_path = base / "external_decision.csv"
            with reviewed_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(winner_rows)
            pipeline.apply_reviewed_external_references(
                canonical_root=canonical_root,
                reviewed_csv=reviewed_path,
            )

            result = state.apply_decisions()

            self.assertEqual(result["selected_count"], 0)
            self.assertEqual(_read_csv(queue_path), [])
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                row = connection.execute(
                    """
                    SELECT storage_path
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role = 'external_original_reference'
                    """,
                    ("project365:1998-04-12",),
                ).fetchone()
            self.assertEqual(row, (str(winner_path),))

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

    def test_commit_deduplicates_repeated_dropped_queue_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            dropped = base / "1998-04-12 dropped.jpg"
            dropped_payload = _jpeg_with_dimensions(12, 9)
            dropped.write_bytes(dropped_payload)
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            row = {
                "entry_id": "project365:1998-04-12",
                "entry_date": "1998-04-12",
                "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                "candidate_path": str(dropped),
                "candidate_filename": dropped.name,
                "candidate_sha256": hashlib.sha256(dropped_payload).hexdigest(),
                "byte_size": str(dropped.stat().st_size),
                "mime_type": "image/jpeg",
                "filename_dates": "1998-04-12",
                "filesystem_dates": "1998-04-12",
                "evidence": "manual_drop_copy",
                "review_decision": "use_external_original",
                "review_notes": "Dropped photo accepted automatically.",
            }
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writerow(row)
                writer.writerow(row)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            detail = state.entry_detail("project365:1998-04-12")
            page = state.entry_page(status="accepted_not_applied")
            result = state.commit_entry_decision("project365:1998-04-12")

            self.assertEqual(detail["candidate_count"], 1)
            self.assertEqual(detail["selected_count"], 1)
            self.assertEqual(detail["pending_commit_count"], 1)
            self.assertEqual(page["entries"][0]["selected_count"], 1)
            self.assertEqual(result["selected_count"], 1)
            self.assertEqual(result["applied_count"], 1)

    def test_commit_deletes_unselected_generated_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "main.jpg",
                    "image/jpeg",
                    _jpeg_with_dimensions(12, 9),
                )
                selected_path = Path(next(candidate["path"] for candidate in detail["candidates"] if candidate["selected"]))
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "unused.jpg",
                    "image/jpeg",
                    _jpeg_with_dimensions(20, 15),
                    associate=True,
                )
            unused_path = Path(next(candidate["path"] for candidate in detail["candidates"] if candidate["associated"]))
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=str(unused_path),
                decision="unlink_associated_photo",
                notes="",
            )

            self.assertTrue(selected_path.exists())
            self.assertTrue(unused_path.exists())

            result = state.commit_entry_decision("project365:1998-04-12")

            self.assertEqual(result["deleted_unused_project_additions"], 1)
            self.assertTrue(selected_path.exists())
            self.assertFalse(unused_path.exists())

    def test_apply_deletes_unselected_generated_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "main.jpg",
                    "image/jpeg",
                    _jpeg_with_dimensions(12, 9),
                )
                selected_path = Path(next(candidate["path"] for candidate in detail["candidates"] if candidate["selected"]))
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "unused.jpg",
                    "image/jpeg",
                    _jpeg_with_dimensions(20, 15),
                    associate=True,
                )
            unused_path = Path(next(candidate["path"] for candidate in detail["candidates"] if candidate["associated"]))
            state.save_decision(
                entry_id="project365:1998-04-12",
                candidate_path=str(unused_path),
                decision="unlink_associated_photo",
                notes="",
            )

            self.assertTrue(selected_path.exists())
            self.assertTrue(unused_path.exists())

            result = state.apply_decisions()

            self.assertEqual(result["deleted_unused_project_additions"], 1)
            self.assertTrue(selected_path.exists())
            self.assertFalse(unused_path.exists())

    def test_apply_skips_unknown_test_targets_without_blocking_valid_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            valid_path = base / "valid-original.png"
            unknown_path = base / "unknown-test-original.png"
            valid_path.write_bytes(_tiny_png())
            unknown_path.write_bytes(_tiny_png())
            with queue_path.open(newline="") as handle:
                fieldnames = list(csv.DictReader(handle).fieldnames or [])
            with queue_path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writerow(
                    {
                        "entry_id": "project365:1998-04-12",
                        "entry_date": "1998-04-12",
                        "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                        "candidate_path": str(valid_path),
                        "candidate_filename": valid_path.name,
                        "review_decision": "use_external_original",
                    }
                )
                writer.writerow(
                    {
                        "entry_id": "project365:2026-09-14",
                        "entry_date": "2026-09-14",
                        "project365_media_asset_id": "project365:2026-09-14:project365_export_png",
                        "candidate_path": str(unknown_path),
                        "candidate_filename": unknown_path.name,
                        "review_decision": "use_external_original",
                    }
                )
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            result = state.apply_decisions()

            self.assertEqual(result["selected_count"], 1)
            self.assertEqual(result["skipped_unknown_media_count"], 1)
            self.assertEqual(state.summary()["pending_decisions"]["accepted"], 1)
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
        self.assertIn('id="imagePreviewModal"', picker.PICKER_HTML)
        self.assertIn('class="image-preview-modal"', picker.PICKER_HTML)
        self.assertIn("function openImagePreviewFromTrigger(trigger)", picker.PICKER_HTML)
        self.assertIn("function closeImagePreview()", picker.PICKER_HTML)
        self.assertIn("function handleImagePreviewBackdrop(event)", picker.PICKER_HTML)
        self.assertIn('params.set("max", "2048");', picker.PICKER_HTML)
        self.assertIn("image.src = largeImageUrl(url);", picker.PICKER_HTML)
        self.assertIn('image.removeAttribute("src");', picker.PICKER_HTML)
        self.assertIn('event.key === "Escape"', picker.PICKER_HTML)
        self.assertIn("data-preview-url", picker.PICKER_HTML)
        self.assertIn('onclick="openImagePreviewFromTrigger(this)"', picker.PICKER_HTML)
        self.assertIn(".image-preview-modal.is-open", picker.PICKER_HTML)
        self.assertIn("width: fit-content", picker.PICKER_HTML)
        self.assertIn("display: inline-flex", picker.PICKER_HTML)
        self.assertIn("align-items: center", picker.PICKER_HTML)
        self.assertIn("cursor: zoom-in", picker.PICKER_HTML)
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
        self.assertIn("Reject all (R)", picker.PICKER_HTML)
        self.assertIn('Shortcut: P. Use the photo already stored in the Project365 entry instead of an external original', picker.PICKER_HTML)
        self.assertIn('>Use Project365 photo (P)</button>', picker.PICKER_HTML)
        self.assertIn('>Reset decisions</button>', picker.PICKER_HTML)
        self.assertIn('data-action="link"', picker.PICKER_HTML)
        self.assertIn('Link this as a supplemental attachment for this diary entry', picker.PICKER_HTML)
        self.assertIn('Click to unlink it.', picker.PICKER_HTML)
        self.assertIn('saveDecision(candidate, "unlink_associated_photo"', picker.PICKER_HTML)
        self.assertIn('${candidateLinked(candidate) ? "linked" : ""}', picker.PICKER_HTML)
        self.assertIn('aria-pressed="${candidateLinked(candidate) ? "true" : "false"}"', picker.PICKER_HTML)
        self.assertIn('${escapeHtml(linkButtonLabel(candidate))}', picker.PICKER_HTML)
        self.assertIn('card.querySelector(\'[data-action="link"]\')?.addEventListener("click", () => linkAssociatedPhoto(candidate, card));', picker.PICKER_HTML)
        self.assertIn("background: #7f1d1d;", picker.PICKER_HTML)
        self.assertNotIn('data-action="reject">Reject</button>', picker.PICKER_HTML)
        self.assertIn('data-action="commit-entry"', picker.PICKER_HTML)
        self.assertIn("const hasPendingLink = entryHasPendingCommit(entry);", picker.PICKER_HTML)
        self.assertIn("function entryPendingCommitCount(entryOrCount)", picker.PICKER_HTML)
        self.assertIn("function commitEntryDecision(entryId, button = null)", picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/commit-entry"', picker.PICKER_HTML)
        self.assertIn('commitEntryDecision(entry.entry_id, event.currentTarget);', picker.PICKER_HTML)
        self.assertIn("if (button) button.disabled = true;", picker.PICKER_HTML)
        self.assertIn("if (state.summaryRefreshTimer) clearTimeout(state.summaryRefreshTimer);", picker.PICKER_HTML)
        self.assertIn("suppressedCommittedEntryIds: new Set()", picker.PICKER_HTML)
        self.assertIn("state.entries = state.entries.filter(entry => entry.entry_id !== entryId);", picker.PICKER_HTML)
        self.assertIn("await loadEntries(nextEntryId, true, committedEntryDate);", picker.PICKER_HTML)
        self.assertNotIn("Commit the linked original for", picker.PICKER_HTML)
        self.assertIn('confirm(`Reset pending selections, rejections, notes, and candidate expansions for ${state.currentEntry.entry_date}?`)', picker.PICKER_HTML)
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
        self.assertIn('id="previousEntryButton"', picker.PICKER_HTML)
        self.assertIn('id="nextEntryButton"', picker.PICKER_HTML)
        self.assertIn('id="entryPosition"', picker.PICKER_HTML)
        self.assertIn("function currentEntryPositionLabel()", picker.PICKER_HTML)
        self.assertIn("function selectAdjacentEntry(direction)", picker.PICKER_HTML)
        self.assertIn("function handlePickerKeyboardShortcut(event)", picker.PICKER_HTML)
        self.assertIn('event.key === "ArrowLeft"', picker.PICKER_HTML)
        self.assertIn('event.key === "ArrowRight"', picker.PICKER_HTML)
        self.assertIn('event.key.toLowerCase() === "r"', picker.PICKER_HTML)
        self.assertIn('event.key.toLowerCase() === "p"', picker.PICKER_HTML)
        self.assertIn('handled = runPickerShortcutButton("fallbackButton", () => document.getElementById("fallbackButton").click());', picker.PICKER_HTML)
        self.assertIn('document.addEventListener("keydown", handlePickerKeyboardShortcut);', picker.PICKER_HTML)
        self.assertIn('id="candidateFilter"', picker.PICKER_HTML)
        self.assertIn('id="candidateEvidenceFilter"', picker.PICKER_HTML)
        self.assertIn('id="candidateFolderFilter"', picker.PICKER_HTML)
        self.assertIn('id="candidateSort"', picker.PICKER_HTML)
        self.assertIn('id="candidateLocationOnly"', picker.PICKER_HTML)
        self.assertIn("const candidates = Array.isArray(entry?.candidates) ? entry.candidates : [];", picker.PICKER_HTML)
        self.assertIn("function candidateMatchesLocation(candidate)", picker.PICKER_HTML)
        self.assertIn("activeCandidateTimeFilters: new Set()", picker.PICKER_HTML)
        self.assertIn("function activeCandidateTimeFilterLabels()", picker.PICKER_HTML)
        self.assertIn("function candidateMatchesActiveTimeFilters(candidate)", picker.PICKER_HTML)
        self.assertIn("function candidateTimeOfDayMinutes(candidate)", picker.PICKER_HTML)
        self.assertIn("function candidateTimeMatchesFilter(minutes, filter)", picker.PICKER_HTML)
        self.assertIn('if (filter === "morning") return minutes >= 4 * 60 && minutes < 8 * 60;', picker.PICKER_HTML)
        self.assertIn('if (filter === "midday") return minutes >= 8 * 60 && minutes < 12 * 60;', picker.PICKER_HTML)
        self.assertIn('if (filter === "early-afternoon") return minutes >= 12 * 60 && minutes < 16 * 60;', picker.PICKER_HTML)
        self.assertIn('if (filter === "late-afternoon") return minutes >= 16 * 60 && minutes < 20 * 60;', picker.PICKER_HTML)
        self.assertIn('if (filter === "early-evening") return minutes >= 20 * 60;', picker.PICKER_HTML)
        self.assertIn('if (filter === "late-evening") return minutes < 4 * 60;', picker.PICKER_HTML)
        self.assertIn("function toggleCandidateTimeFilter(button)", picker.PICKER_HTML)
        self.assertIn("function resetCandidateTimeFilters()", picker.PICKER_HTML)
        self.assertIn("resetCandidateTimeFilters();", picker.PICKER_HTML)
        self.assertIn("function updateCandidateTimeFilterButtons()", picker.PICKER_HTML)
        self.assertIn("candidateMatchesActiveTimeFilters(candidate)", picker.PICKER_HTML)
        self.assertIn('document.querySelectorAll("[data-time-filter]").forEach(button => {', picker.PICKER_HTML)
        self.assertIn("function candidateScopeCandidates(candidates)", picker.PICKER_HTML)
        self.assertIn("function candidateMatchesCurrentIndexDefaultScope(candidate)", picker.PICKER_HTML)
        self.assertIn("function candidateInProjectOriginalsFolder(candidate)", picker.PICKER_HTML)
        self.assertIn("function candidateFilenameStartsWithCurrentScopeDate(candidate)", picker.PICKER_HTML)
        self.assertIn("function currentFilenameDateScopeDates()", picker.PICKER_HTML)
        self.assertIn("const scopedCandidates = candidateScopeCandidates(candidates);", picker.PICKER_HTML)
        self.assertIn("renderCandidateFolderFilter(scopedCandidates);", picker.PICKER_HTML)
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
        self.assertIn('class="time-filter-controls" role="group" aria-label="Filter candidates by time of day"', picker.PICKER_HTML)
        self.assertIn('data-time-filter="morning" aria-pressed="false" title="Morning, 4:00 AM to 7:59 AM">4-8a</button>', picker.PICKER_HTML)
        self.assertIn('data-time-filter="midday" aria-pressed="false" title="Midday, 8:00 AM to 11:59 AM">8-12p</button>', picker.PICKER_HTML)
        self.assertIn('data-time-filter="early-afternoon" aria-pressed="false" title="Early afternoon, 12:00 PM to 3:59 PM">12-4p</button>', picker.PICKER_HTML)
        self.assertIn('data-time-filter="late-afternoon" aria-pressed="false" title="Late afternoon, 4:00 PM to 7:59 PM">4-8p</button>', picker.PICKER_HTML)
        self.assertIn('data-time-filter="early-evening" aria-pressed="false" title="Early evening, 8:00 PM to 11:59 PM">8-12a</button>', picker.PICKER_HTML)
        self.assertIn('data-time-filter="late-evening" aria-pressed="false" title="Late evening, 12:00 AM to 3:59 AM">12-4a</button>', picker.PICKER_HTML)
        self.assertLess(
            picker.PICKER_HTML.index('id="defaultDateRange"'),
            picker.PICKER_HTML.index('data-range-days="1"'),
        )
        self.assertLess(
            picker.PICKER_HTML.index('data-range-days="30"'),
            picker.PICKER_HTML.index('class="time-filter-controls"'),
        )
        self.assertLess(
            picker.PICKER_HTML.index('class="time-filter-controls"'),
            picker.PICKER_HTML.index('class="manual-date-search"'),
        )
        self.assertIn(".date-range-controls .action-button.used-range", picker.PICKER_HTML)
        self.assertIn('.time-filter-controls .action-button[aria-pressed="true"]', picker.PICKER_HTML)
        self.assertIn(".crawl-status.is-running", picker.PICKER_HTML)
        self.assertIn("async function expandDefaultDateRange()", picker.PICKER_HTML)
        self.assertNotIn('id="indexSearchFilenameOnly"', picker.PICKER_HTML)
        self.assertIn('id="indexSearchModifiedDate"', picker.PICKER_HTML)
        self.assertIn('class="index-search-options"', picker.PICKER_HTML)
        self.assertIn("> Include modified dates</label>", picker.PICKER_HTML)
        self.assertNotIn("> Other filename dates only</label>", picker.PICKER_HTML)
        self.assertIn("grid-template-columns: 104px 104px max-content auto", picker.PICKER_HTML)
        self.assertIn(".manual-date-search input[type=\"date\"]", picker.PICKER_HTML)
        self.assertIn("box-sizing: border-box;", picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/expand-default-date-range"', picker.PICKER_HTML)
        self.assertIn('document.getElementById("defaultDateRange").onclick = expandDefaultDateRange', picker.PICKER_HTML)
        self.assertNotIn('document.getElementById("indexSearchFilenameOnly").onchange', picker.PICKER_HTML)
        self.assertNotIn('wholeIndex.checked = true;', picker.PICKER_HTML)
        self.assertIn('document.getElementById("indexSearchModifiedDate").onchange = () => {', picker.PICKER_HTML)
        self.assertIn("renderCandidateGrid();", picker.PICKER_HTML)
        self.assertIn('setCrawlStatus(`Searching ${scopeLabel} from ${startDate} to ${endDate}.`, true);', picker.PICKER_HTML)
        self.assertIn('setCrawlStatus(`Expanding ${scopeLabel} candidates to ±${days} days.`, true);', picker.PICKER_HTML)
        self.assertIn("function updateDateRangeButtons(entry)", picker.PICKER_HTML)
        self.assertIn("function usedDateRangeDays(entry)", picker.PICKER_HTML)
        self.assertIn("function defaultDateRangeUsed(entry)", picker.PICKER_HTML)
        self.assertIn("function defaultEvidenceMatchesCurrentIndexScope(evidence)", picker.PICKER_HTML)
        self.assertIn("function rangeEvidenceMatchesCurrentIndexScope(evidence)", picker.PICKER_HTML)
        self.assertIn("function currentIndexSearchWholeIndex()", picker.PICKER_HTML)
        self.assertNotIn("function currentIndexFilenameOnly()", picker.PICKER_HTML)
        self.assertIn("function currentIndexIncludeModifiedDates()", picker.PICKER_HTML)
        self.assertIn("function currentIndexRangeDateSourceLabel()", picker.PICKER_HTML)
        self.assertIn("function currentIndexDateSourceLabel()", picker.PICKER_HTML)
        self.assertIn("wholeIndexCheckbox.checked = false;", picker.PICKER_HTML)
        self.assertIn("modifiedDateCheckbox.checked = false;", picker.PICKER_HTML)
        self.assertNotIn("checkbox.checked = !constrained;", picker.PICKER_HTML)
        self.assertIn("modifiedDateCheckbox.disabled = !state.currentEntry || !wholeIndex;", picker.PICKER_HTML)
        self.assertIn("const includeUnscopedRangeState = currentIndexSearchWholeIndex();", picker.PICKER_HTML)
        self.assertIn("if (!rangeEvidenceMatchesCurrentIndexScope(evidence)) continue;", picker.PICKER_HTML)
        self.assertIn("updateDateRangeButtons(state.currentEntry);", picker.PICKER_HTML)
        self.assertIn('button.classList.toggle("used-range", used);', picker.PICKER_HTML)
        self.assertLess(
            picker.PICKER_HTML.index('class="date-range-controls"'),
            picker.PICKER_HTML.index('id="photoDropTarget"'),
        )
        self.assertIn('fetchJson("/api/expand-date-range"', picker.PICKER_HTML)
        self.assertIn("defaultPhotoIndexFolder", picker.PICKER_HTML)
        self.assertIn('params.get("photo_index_folder")', picker.PICKER_HTML)
        self.assertIn('params.has("photo_index_folder")', picker.PICKER_HTML)
        self.assertIn('Object.prototype.hasOwnProperty.call(summary, "default_photo_index_folder")', picker.PICKER_HTML)
        self.assertIn('Object.prototype.hasOwnProperty.call(summary, "active_photo_index_folder")', picker.PICKER_HTML)
        self.assertIn('window.localStorage.setItem("project365.activePhotoIndexFolder"', picker.PICKER_HTML)
        self.assertNotIn('window.localStorage.getItem("project365.activePhotoIndexFolder")', picker.PICKER_HTML)
        self.assertIn(
            "const payload = {entry_id: entryId, days, search_whole_index: wholeIndex, filename_dates_only: filenameOnly, include_modified_dates: includeModifiedDates};",
            picker.PICKER_HTML,
        )
        self.assertEqual(picker.PICKER_HTML.count("const filenameOnly = !wholeIndex;"), 3)
        self.assertNotIn("currentIndexFilenameOnly", picker.PICKER_HTML)
        self.assertIn("function ensureIndexSearchScopeAvailable()", picker.PICKER_HTML)
        self.assertIn("filename_dates_only: filenameOnly", picker.PICKER_HTML)
        self.assertIn("include_modified_dates: includeModifiedDates", picker.PICKER_HTML)
        self.assertNotIn("payload.photo_index_folder = state.activePhotoIndexFolder", picker.PICKER_HTML)
        self.assertIn("state.entryDetailCache.set(entryId, result.entry);", picker.PICKER_HTML)
        self.assertNotIn('png-candidate', picker.PICKER_HTML)
        self.assertIn('className = `candidate-card ${candidate.selected ? "selected" : ""} ${candidateProjectOriginalsSource(candidate) ? "project-originals-source" : ""}`', picker.PICKER_HTML)
        self.assertIn("function candidateMatchesTargetDate(candidate)", picker.PICKER_HTML)
        self.assertIn("function candidateTargetDateRank(candidate)", picker.PICKER_HTML)
        self.assertIn("const targetDateCompare = candidateTargetDateRank(left) - candidateTargetDateRank(right);", picker.PICKER_HTML)
        self.assertNotIn(".candidate-card.entry-date-match", picker.PICKER_HTML)
        self.assertIn("function candidateProjectOriginalsSource(candidate)", picker.PICKER_HTML)
        self.assertIn(".candidate-card.project-originals-source", picker.PICKER_HTML)
        self.assertIn("#b42318", picker.PICKER_HTML)
        self.assertIn("Original Photos matching Project365 Entries", picker.PICKER_HTML)
        self.assertIn('${escapeHtml(selectButtonLabel(candidate))}', picker.PICKER_HTML)
        self.assertIn('class="action-button ${candidate.associated ? "flagged" : ""}" data-action="flag"', picker.PICKER_HTML)
        self.assertNotIn('data-action="toggle-notes"', picker.PICKER_HTML)
        self.assertNotIn('data-notes-editor hidden', picker.PICKER_HTML)
        self.assertNotIn('function toggleCandidateNotes', picker.PICKER_HTML)
        self.assertNotIn('document.getElementById(notesId).focus();', picker.PICKER_HTML)
        self.assertIn('${escapeHtml(flagButtonLabel(candidate))}', picker.PICKER_HTML)
        self.assertIn('function flagButtonLabel(candidate)', picker.PICKER_HTML)
        self.assertIn('function flagButtonTitle(candidate)', picker.PICKER_HTML)
        self.assertIn('function applyFlagButtonState(button, candidate)', picker.PICKER_HTML)
        self.assertIn('function applySelectButtonState(card, candidate)', picker.PICKER_HTML)
        self.assertIn('function applyLinkButtonState(card, candidate)', picker.PICKER_HTML)
        self.assertIn('function applyEntryListState(entry)', picker.PICKER_HTML)
        self.assertIn('function capturePickerScroll()', picker.PICKER_HTML)
        self.assertIn('function restorePickerScroll(snapshot)', picker.PICKER_HTML)
        self.assertIn('if (isLink) {', picker.PICKER_HTML)
        self.assertIn('applyEntryListState(entrySummary || state.currentEntry);', picker.PICKER_HTML)
        self.assertIn('applySelectButtonState(itemCard, item);', picker.PICKER_HTML)
        self.assertIn('applyLinkButtonState(itemCard, item);', picker.PICKER_HTML)
        self.assertIn('restorePickerScroll(linkScrollState);', picker.PICKER_HTML)
        self.assertIn('.entry-commit[aria-hidden="true"]', picker.PICKER_HTML)
        self.assertIn('.action-button.flagged:not(:disabled)', picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/link-candidate"', picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/import-dropped-candidate"', picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/import-linked-candidate"', picker.PICKER_HTML)
        self.assertIn("candidate_path: candidatePath, associate", picker.PICKER_HTML)
        self.assertIn("linkCandidatePath(candidatePath, true)", picker.PICKER_HTML)
        self.assertIn("Photo copied and accepted", picker.PICKER_HTML)
        self.assertIn("Dropped photo linked as an associated attachment", picker.PICKER_HTML)
        self.assertIn(".photo-drop-row", picker.PICKER_HTML)
        self.assertIn("grid-template-columns: minmax(0, 4fr) minmax(120px, 1fr)", picker.PICKER_HTML)
        self.assertIn("Drop photo/video here", picker.PICKER_HTML)
        self.assertNotIn("Drop original photo here", picker.PICKER_HTML)
        self.assertIn('id="photoLinkDropTarget"', picker.PICKER_HTML)
        self.assertIn("photo-link-drag-lane", picker.PICKER_HTML)
        self.assertIn("Linked photo", picker.PICKER_HTML)
        self.assertIn('id="chooseLinkedPhoto"', picker.PICKER_HTML)
        self.assertIn("Open drop window", picker.PICKER_HTML)
        self.assertIn('document.getElementById("chooseLinkedPhoto").onclick = chooseLinkedPhoto;', picker.PICKER_HTML)
        self.assertIn("function chooseLinkedPhoto()", picker.PICKER_HTML)
        self.assertIn('"/picker/link-drop"', picker.PICKER_HTML)
        self.assertIn('"/link-drop"', picker.PICKER_HTML)
        self.assertIn('"width=640,height=460"', picker.PICKER_HTML)
        self.assertIn('window.addEventListener("message", handleLinkedDropWindowMessage);', picker.PICKER_HTML)
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
        self.assertIn('linkDropTarget.addEventListener("drop", handleLinkDrop)', picker.PICKER_HTML)
        self.assertIn('document.addEventListener("drop", handleFallbackLinkDrop, true)', picker.PICKER_HTML)
        self.assertIn('target?.closest?.("#photoDropTarget")', picker.PICKER_HTML)
        self.assertIn("let photoDropDragDepth = 0;", picker.PICKER_HTML)
        self.assertIn("let photoLinkDragDepth = 0;", picker.PICKER_HTML)
        self.assertIn("photoDropDragDepth += 1;", picker.PICKER_HTML)
        self.assertIn("photoLinkDragDepth += 1;", picker.PICKER_HTML)
        self.assertIn("photoDropDragDepth = Math.max(0, photoDropDragDepth - 1);", picker.PICKER_HTML)
        self.assertIn("photoLinkDragDepth = Math.max(0, photoLinkDragDepth - 1);", picker.PICKER_HTML)
        self.assertIn('dropTarget.addEventListener("dragend"', picker.PICKER_HTML)
        self.assertIn('linkDropTarget.addEventListener("dragend"', picker.PICKER_HTML)
        self.assertIn('event.dataTransfer.dropEffect = "link";', picker.PICKER_HTML)
        self.assertIn("function handleLinkDrop(event)", picker.PICKER_HTML)
        self.assertIn("function droppedLinkPath(event)", picker.PICKER_HTML)
        self.assertIn("function describePhotoDropTransfer(event)", picker.PICKER_HTML)
        self.assertIn('types.includes("Files") || files.length > 0 || items.length > 0', picker.PICKER_HTML)
        self.assertNotIn("if (!isFileDrag(event)) return;", picker.PICKER_HTML)
        self.assertIn("Safari did not expose one usable image file for this drop.", picker.PICKER_HTML)
        self.assertIn("Try Open drop window, or export from Photos to Finder first.", picker.PICKER_HTML)
        self.assertIn("videoFrameModal", picker.PICKER_HTML)
        self.assertIn("Choose movie screenshots", picker.PICKER_HTML)
        self.assertIn("function showVideoFrameChooser(payload, entry)", picker.PICKER_HTML)
        self.assertIn("videoFramePlayer", picker.PICKER_HTML)
        self.assertIn("videoFrameSlider", picker.PICKER_HTML)
        self.assertIn("Capture frame", picker.PICKER_HTML)
        self.assertIn("function captureCurrentVideoFrame()", picker.PICKER_HTML)
        self.assertIn("function stepVideoFrameTime(deltaSeconds)", picker.PICKER_HTML)
        self.assertIn('return candidate.associated ? "Linked" : "Link";', picker.PICKER_HTML)
        self.assertIn('function candidateIsGeneratedAddition(candidate)', picker.PICKER_HTML)
        self.assertIn('const showFlagControl = !generatedAddition;', picker.PICKER_HTML)
        self.assertIn('const actionClass = generatedAddition && candidate.selected ? "main-only" : generatedAddition ? "compact" : "";', picker.PICKER_HTML)
        self.assertIn('if (candidate.selected) return "Main";', picker.PICKER_HTML)
        self.assertIn('return candidate.selected ? "Main" : "Select";', picker.PICKER_HTML)
        self.assertIn("> Main</label>", picker.PICKER_HTML)
        self.assertIn("> Link</label>", picker.PICKER_HTML)
        self.assertNotIn("Add supplemental", picker.PICKER_HTML)
        self.assertNotIn("Main image</label>", picker.PICKER_HTML)
        self.assertIn("videoFramePlayPause", picker.PICKER_HTML)
        self.assertIn('title="Play/pause movie. Shortcut: Space"', picker.PICKER_HTML)
        self.assertIn('title="Capture the current frame. Shortcut: Enter"', picker.PICKER_HTML)
        self.assertIn('event.key === "Enter"', picker.PICKER_HTML)
        self.assertIn("function toggleVideoFramePlayback()", picker.PICKER_HTML)
        self.assertIn("function reviewSortVideoFrames(review)", picker.PICKER_HTML)
        self.assertIn("function renderVideoFrameGrid(selection = currentVideoFrameSelection())", picker.PICKER_HTML)
        self.assertIn("function videoFrameShotDateLabel(entryDate, captureTimestamp)", picker.PICKER_HTML)
        self.assertIn("function videoFrameTimeKey(timeSeconds)", picker.PICKER_HTML)
        self.assertIn("Move slightly before capturing again.", picker.PICKER_HTML)
        self.assertIn("custom-frame", picker.PICKER_HTML)
        self.assertIn('content: "Captured";', picker.PICKER_HTML)
        self.assertIn("event.shiftKey ? 1 : 0.1", picker.PICKER_HTML)
        self.assertIn('event.key === "ArrowLeft" || event.key === "ArrowRight"', picker.PICKER_HTML)
        self.assertIn("function openVideoFramePreview(trigger)", picker.PICKER_HTML)
        self.assertIn('event.code === "Space"', picker.PICKER_HTML)
        self.assertIn("Preparing movie screenshots", picker.PICKER_HTML)
        self.assertIn("/api/discard-video-frames", picker.PICKER_HTML)
        self.assertIn("showVideoFrameProcessing(entry, file)", picker.PICKER_HTML)
        self.assertNotIn("video-frame-hover-preview", picker.PICKER_HTML)
        self.assertNotIn("showVideoFrameHoverPreview", picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/extract-video-frame"', picker.PICKER_HTML)
        self.assertIn('fetchJson("/api/import-video-frames"', picker.PICKER_HTML)
        self.assertIn('detail.kind === "video_frame_choices"', picker.PICKER_HTML)
        self.assertIn("types.length > 0 || files.length > 0 || items.length > 0", picker.PICKER_HTML)
        self.assertIn('"text/x-moz-url"', picker.PICKER_HTML)
        self.assertIn("Drop one image file or a local file path.", picker.PICKER_HTML)
        self.assertIn("This drag exposed:", picker.PICKER_HTML)
        self.assertIn("previousEntryStack: []", picker.PICKER_HTML)
        self.assertIn("function rememberPreviousEntry(entry)", picker.PICKER_HTML)
        self.assertIn("function loadRememberedPreviousEntry()", picker.PICKER_HTML)
        self.assertIn("!state.previousEntryStack.length", picker.PICKER_HTML)
        self.assertIn("function candidateManualLinkRank(candidate)", picker.PICKER_HTML)
        self.assertIn("manualLinkCompare", picker.PICKER_HTML)
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
        self.assertIn("function scheduleCandidateScrollReset()", picker.PICKER_HTML)
        self.assertIn("function resetCandidateScroll()", picker.PICKER_HTML)
        self.assertIn("if (entryChanged) {", picker.PICKER_HTML)
        self.assertIn("scheduleCandidateScrollReset();", picker.PICKER_HTML)
        self.assertIn("window.requestAnimationFrame(() => {", picker.PICKER_HTML)
        self.assertIn("window.setTimeout(resetCandidateScroll, 0);", picker.PICKER_HTML)
        self.assertIn('document.querySelector(".workspace")', picker.PICKER_HTML)
        self.assertIn("window.scrollTo(0, 0);", picker.PICKER_HTML)
        self.assertIn('id="commitCropButton"', picker.CROP_HTML)
        self.assertIn('id="pendingCropCommitCount"', picker.CROP_HTML)
        self.assertIn('fetchJson("/api/crop-commit"', picker.CROP_HTML)
        self.assertIn("function commitStagedCrops()", picker.CROP_HTML)

    def test_link_drop_popup_uses_associated_photo_import(self) -> None:
        html = picker.link_drop_html("/picker/api")

        self.assertIn("Linked photo drop", html)
        self.assertIn("Drop the linked photo here", html)
        self.assertIn("width: min(100%, 620px)", html)
        self.assertIn("max-height: min(92vh, 460px)", html)
        self.assertIn("min-height: 190px", html)
        self.assertIn('const API_PREFIX = "/picker/api";', html)
        self.assertIn("describeTransfer(transfer)", html)
        self.assertIn("Safari did not expose a usable file or local path", html)
        self.assertIn('`${API_PREFIX}/import-linked-candidate`', html)
        self.assertIn('`${API_PREFIX}/link-candidate`', html)
        self.assertIn("associate: true", html)
        self.assertIn('type: "project365-linked-photo"', html)

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

    def test_picker_uses_photo_index_geolocation_for_existing_heic_queue_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            library_root = base / "library"
            library_root.mkdir()
            candidate_path = library_root / "1998-04-12 indexed.heic"
            candidate_path.write_bytes(_tiny_png())
            from project365_photo_library_index import ExiftoolPhotoMetadata, build_photo_library_index, default_index_db

            with mock.patch.object(
                picker,
                "_has_embedded_geolocation",
                return_value=False,
            ), mock.patch(
                "project365_photo_library_index._exiftool_photo_metadata",
                return_value={
                    str(candidate_path.resolve()): ExiftoolPhotoMetadata(
                        capture_timestamp="1998-04-12T08:09:10",
                        capture_timestamp_source="exif_datetime_original",
                        gps_latitude=25.0962305555556,
                        gps_longitude=121.587294444444,
                        gps_source="composite_gps",
                    )
                },
            ):
                build_photo_library_index(default_index_db(canonical_root), [library_root], reset=True)
                with sqlite3.connect(default_index_db(canonical_root)) as connection:
                    connection.execute("UPDATE photo_library_files SET has_gps = 0")
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
                            "candidate_path": str(candidate_path),
                            "candidate_filename": candidate_path.name,
                            "candidate_sha256": hashlib.sha256(_tiny_png()).hexdigest(),
                            "byte_size": str(candidate_path.stat().st_size),
                            "mime_type": "image/heic",
                            "filename_dates": "1998-04-12",
                            "media_creation_dates": "1998-04-12",
                            "evidence": "photo_library_index",
                        }
                    )

                state = picker.PickerState(
                    picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
                )
                detail = state.entry_detail("project365:1998-04-12")
                candidate = detail["candidates"][0]
                facts = state.candidate_facts([candidate["token"]])

            self.assertTrue(candidate["has_embedded_geolocation"])
            self.assertTrue(facts[candidate["token"]]["has_embedded_geolocation"])

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
            self.assertFalse(detail["candidates"][0]["associated"])
            self.assertEqual(Path(detail["candidates"][0]["path"]), original_path.resolve())
            self.assertFalse((canonical_root / "picker_drops").exists())

            repeated = state.add_linked_candidate("project365:1998-04-12", str(original_path))
            self.assertEqual(repeated["candidate_count"], 1)

    def test_picker_links_absolute_drop_path_as_associated_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            original_path = base / "known-original.png"
            original_path.write_bytes(_tiny_png())
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            detail = state.add_linked_candidate(
                "project365:1998-04-12",
                str(original_path),
                associate=True,
            )

            self.assertEqual(detail["status"], "needs_review")
            self.assertEqual(detail["accepted_count"], 0)
            self.assertEqual(detail["associated_count"], 1)
            self.assertFalse(detail["candidates"][0]["selected"])
            self.assertTrue(detail["candidates"][0]["associated"])
            self.assertEqual(
                detail["candidates"][0]["review_decision"],
                "external_original_associated_photo",
            )
            self.assertEqual(detail["candidates"][0]["associated_entry_date"], "1998-04-12")
            self.assertEqual(state.summary()["pending_decisions"]["associated"], 1)

    def test_picker_links_absolute_drop_path_for_database_only_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            linked_path = base / "linked-extra.jpg"
            linked_path.write_bytes(_jpeg_with_dimensions(20, 15))
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                state.add_copied_candidate(
                    "project365:1998-04-12",
                    "accepted-original.jpg",
                    "image/jpeg",
                    _jpeg_with_dimensions(30, 20),
                )
            self.assertEqual(state.apply_decisions()["selected_count"], 1)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            detail = state.add_linked_candidate(
                "project365:1998-04-12",
                str(linked_path),
                associate=True,
            )

            self.assertEqual(detail["entry_id"], "project365:1998-04-12")
            self.assertEqual(detail["associated_count"], 1)
            self.assertTrue(detail["candidates"][0]["associated"])
            self.assertEqual(state.summary()["pending_decisions"]["associated"], 1)

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

            with mock.patch.object(picker, "_set_project_addition_timestamps") as timestamp_mock:
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
                / "1998-04-12 000001 (Project365 project file addition).png"
            )
            timestamp_mock.assert_called_once_with(
                mock.ANY,
                dt.datetime(1998, 4, 12, 0, 0, 1),
            )
            self.assertEqual(detail["candidate_count"], 1)
            self.assertEqual(detail["status"], "selected")
            self.assertTrue(detail["candidates"][0]["selected"])
            self.assertEqual(detail["candidates"][0]["evidence"], "manual_drop_copy")
            self.assertEqual(Path(detail["candidates"][0]["path"]), copied_path.resolve())
            self.assertEqual(detail["candidates"][0]["filename_dates"], "1998-04-12")
            self.assertEqual(detail["candidates"][0]["media_creation_dates"], "")
            self.assertEqual(detail["candidates"][0]["filesystem_dates"], "1998-04-12")
            self.assertEqual(copied_path.read_bytes(), _tiny_png())
            self.assertEqual(queue_path.read_bytes(), before_queue)

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                repeated = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "known-original.png",
                    "image/png",
                    _tiny_png(),
                )
            self.assertEqual(repeated["candidate_count"], 1)

    def test_picker_converts_dropped_avif_into_managed_heic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            def fake_convert(source: Path, destination: Path) -> None:
                self.assertEqual(source.suffix, ".avif")
                self.assertEqual(destination.suffix, ".heic")
                destination.write_bytes(_tiny_png())

            with (
                mock.patch.object(picker, "_convert_dropped_photo_to_heic", side_effect=fake_convert) as convert_mock,
                mock.patch.object(picker, "_set_project_addition_timestamps") as timestamp_mock,
            ):
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "known-original.avif",
                    "image/avif",
                    b"avif payload",
                )

            copied_path = (
                base
                / "Source Data"
                / "Original Photos matching Project365 Entries"
                / "1998-04"
                / "1998-04-12 000001 (Project365 project file addition).heic"
            )
            convert_mock.assert_called_once()
            timestamp_mock.assert_called_once_with(
                mock.ANY,
                dt.datetime(1998, 4, 12, 0, 0, 1),
            )
            self.assertEqual(detail["candidate_count"], 1)
            self.assertEqual(detail["status"], "selected")
            self.assertEqual(Path(detail["candidates"][0]["path"]), copied_path.resolve())
            self.assertEqual(detail["candidates"][0]["mime_type"], "image/heic")
            self.assertEqual(copied_path.read_bytes(), _tiny_png())

    def test_picker_converts_dropped_webp_into_managed_heic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            def fake_convert(source: Path, destination: Path) -> None:
                self.assertEqual(source.suffix, ".webp")
                self.assertEqual(destination.suffix, ".heic")
                destination.write_bytes(_tiny_png())

            with (
                mock.patch.object(picker, "_convert_dropped_photo_to_heic", side_effect=fake_convert),
                mock.patch.object(picker, "_set_project_addition_timestamps"),
            ):
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "known-original.webp",
                    "image/webp",
                    b"webp payload",
                )

            self.assertEqual(detail["candidate_count"], 1)
            self.assertEqual(Path(detail["candidates"][0]["path"]).suffix, ".heic")
            self.assertEqual(detail["candidates"][0]["mime_type"], "image/heic")

    def test_picker_prepares_and_imports_dropped_video_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            upload_path = base / "clip.mov"
            upload_path.write_bytes(b"movie")
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            def fake_extract(video_path: Path, frame_dir: Path, max_count: int) -> list[tuple[float, Path]]:
                self.assertEqual(video_path.name, "clip.mov")
                self.assertEqual(max_count, picker.VIDEO_FRAME_COUNT)
                first = frame_dir / "frame-01.jpg"
                second = frame_dir / "frame-02.jpg"
                first.write_bytes(_jpeg_with_dimensions(12, 9))
                second.write_bytes(_jpeg_with_dimensions(20, 15))
                return [(1.25, first), (7.5, second)]

            with (
                mock.patch.object(picker, "_extract_video_frames", side_effect=fake_extract),
                mock.patch.object(
                    picker,
                    "_video_capture_timestamp",
                    return_value=("1998-04-12T08:09:10", "quicktime_content_create_date"),
                ),
            ):
                review = state.prepare_video_frame_choices(
                    "project365:1998-04-12",
                    "clip.mov",
                    upload_path,
                )

            self.assertEqual(review["kind"], "video_frame_choices")
            self.assertEqual(review["entry_id"], "project365:1998-04-12")
            self.assertEqual(review["capture_timestamp"], "1998-04-12T08:09:10")
            self.assertEqual(review["capture_timestamp_source"], "quicktime_content_create_date")
            self.assertEqual(len(review["frames"]), 2)
            self.assertFalse(upload_path.exists())
            frame_dir = canonical_root / "cache" / "picker_video_frames" / review["session_id"]
            self.assertTrue(frame_dir.exists())

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                detail = state.import_video_frames(
                    entry_id="project365:1998-04-12",
                    session_id=review["session_id"],
                    main_frame_id=review["frames"][0]["frame_id"],
                    supplemental_frame_ids=[review["frames"][1]["frame_id"]],
                )

            pending = state.summary()["pending_decisions"]
            self.assertEqual(pending["accepted"], 1)
            self.assertEqual(pending["associated"], 1)
            self.assertEqual(detail["entry_id"], "project365:1998-04-12")
            self.assertEqual(detail["selected_count"], 1)
            self.assertEqual(
                sum(1 for candidate in detail["candidates"] if candidate["selected"]),
                1,
            )
            copied = sorted(
                (
                    base
                    / "Source Data"
                    / "Original Photos matching Project365 Entries"
                    / "1998-04"
                ).glob("*.jpg")
            )
            self.assertEqual(len(copied), 2)
            self.assertFalse(frame_dir.exists())

    def test_picker_adds_custom_dropped_video_frame_at_slider_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            upload_path = base / "clip.mov"
            upload_path.write_bytes(b"movie")
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            def fake_extract(video_path: Path, frame_dir: Path, max_count: int) -> list[tuple[float, Path]]:
                first = frame_dir / "frame-01.jpg"
                first.write_bytes(_jpeg_with_dimensions(12, 9))
                return [(1.25, first)]

            def fake_extract_at_time(video_path: Path, frame_path: Path, time_seconds: float) -> None:
                self.assertEqual(video_path.name, "clip.mov")
                self.assertEqual(time_seconds, 3.5)
                frame_path.write_bytes(_jpeg_with_dimensions(20, 15))

            with (
                mock.patch.object(picker, "_extract_video_frames", side_effect=fake_extract),
                mock.patch.object(picker, "_video_duration_seconds", return_value=8.0),
            ):
                review = state.prepare_video_frame_choices(
                    "project365:1998-04-12",
                    "clip.mov",
                    upload_path,
                )

            self.assertEqual(review["duration_seconds"], 8.0)
            frame_dir = canonical_root / "cache" / "picker_video_frames" / review["session_id"]
            self.assertTrue(frame_dir.exists())
            with mock.patch.object(picker, "_extract_video_frame_at_time", side_effect=fake_extract_at_time):
                custom = state.extract_video_frame_at_time(
                    entry_id="project365:1998-04-12",
                    session_id=review["session_id"],
                    time_seconds=3.5,
                )

            frame = custom["frame"]
            self.assertEqual(frame["frame_id"], "custom-02")
            self.assertEqual(frame["label"], "0:04")
            self.assertTrue(frame["custom"])
            with self.assertRaisesRegex(ValueError, "already in the grid"):
                state.extract_video_frame_at_time(
                    entry_id="project365:1998-04-12",
                    session_id=review["session_id"],
                    time_seconds=3.5,
                )

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                detail = state.import_video_frames(
                    entry_id="project365:1998-04-12",
                    session_id=review["session_id"],
                    main_frame_id=frame["frame_id"],
                )

            self.assertEqual(detail["entry_id"], "project365:1998-04-12")
            self.assertTrue(detail["candidates"])
            self.assertEqual(detail["selected_count"], 1)
            self.assertEqual(
                sum(1 for candidate in detail["candidates"] if candidate["selected"]),
                1,
            )
            pending = state.summary()["pending_decisions"]
            self.assertEqual(pending["accepted"], 1)
            self.assertFalse(frame_dir.exists())

    def test_picker_copies_link_drop_as_associated_photo(self) -> None:
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

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "known-original.png",
                    "image/png",
                    _tiny_png(),
                    associate=True,
                )

            copied_path = (
                base
                / "Source Data"
                / "Original Photos matching Project365 Entries"
                / "1998-04"
                / "1998-04-12 000001 (Project365 project file addition).png"
            )
            self.assertEqual(detail["status"], "needs_review")
            self.assertEqual(detail["accepted_count"], 0)
            self.assertEqual(detail["associated_count"], 1)
            self.assertFalse(detail["candidates"][0]["selected"])
            self.assertTrue(detail["candidates"][0]["associated"])
            self.assertEqual(detail["candidates"][0]["evidence"], "manual_drop_copy")
            self.assertEqual(Path(detail["candidates"][0]["path"]), copied_path.resolve())
            self.assertEqual(
                detail["candidates"][0]["review_decision"],
                "external_original_associated_photo",
            )
            self.assertEqual(detail["candidates"][0]["associated_entry_date"], "1998-04-12")
            self.assertEqual(state.summary()["pending_decisions"]["associated"], 1)
            self.assertEqual(queue_path.read_bytes(), before_queue)

    def test_picker_copies_link_drop_for_database_only_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            queue_path = canonical_root / "exports" / "verification_reports" / "queue.csv"
            _write_queue(queue_path)
            _append_search_entry(queue_path)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )
            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                state.add_copied_candidate(
                    "project365:1998-04-12",
                    "accepted-original.jpg",
                    "image/jpeg",
                    _jpeg_with_dimensions(30, 20),
                )
            self.assertEqual(state.apply_decisions()["selected_count"], 1)
            state = picker.PickerState(
                picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path)
            )

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                detail = state.add_copied_candidate(
                    "project365:1998-04-12",
                    "linked-extra.jpg",
                    "image/jpeg",
                    _jpeg_with_dimensions(20, 15),
                    associate=True,
                )

            self.assertEqual(detail["entry_id"], "project365:1998-04-12")
            self.assertEqual(detail["accepted_count"], 0)
            self.assertEqual(detail["associated_count"], 1)
            self.assertTrue(detail["candidates"][0]["associated"])
            self.assertEqual(state.summary()["pending_decisions"]["associated"], 1)
            result = state.apply_decisions()
            self.assertEqual(result["associated_count"], 1)
            self.assertEqual(result["skipped_unknown_media_count"], 0)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                roles = connection.execute(
                    """
                    SELECT role, COUNT(*)
                    FROM media_assets
                    WHERE entry_id = ?
                        AND role IN ('external_original_reference', 'external_original_associated_photo')
                    GROUP BY role
                    """,
                    ("project365:1998-04-12",),
                ).fetchall()
            self.assertEqual(dict(roles), {"external_original_associated_photo": 1, "external_original_reference": 1})

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

            with mock.patch.object(picker, "_set_project_addition_timestamps"):
                state.add_copied_candidate(
                    "project365:1998-04-12", "photo.jpg", "image/jpeg", _jpeg_with_dimensions(12, 9)
                )
                detail = state.add_copied_candidate(
                    "project365:1998-04-12", "photo.jpg", "image/jpeg", _jpeg_with_dimensions(20, 15)
                )

            self.assertEqual(detail["candidate_count"], 2)
            self.assertEqual(len({candidate["path"] for candidate in detail["candidates"]}), 2)
            self.assertTrue(
                all(
                    Path(candidate["path"]).name.startswith(
                        "1998-04-12 000001 (Project365 project file addition)"
                    )
                    for candidate in detail["candidates"]
                )
            )

    def test_project_addition_timestamps_only_set_file_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "photo.jpg"
            path.write_bytes(_jpeg_with_dimensions(12, 9))
            completed = mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(picker.shutil, "which", return_value="/usr/local/bin/exiftool"),
                mock.patch.object(picker.subprocess, "run", return_value=completed) as run_mock,
                mock.patch.object(picker.os, "utime") as utime_mock,
            ):
                picker._set_project_addition_timestamps(path, dt.datetime(1998, 4, 12, 0, 0, 1))

            command = run_mock.call_args.args[0]
            self.assertIn("-FileCreateDate=1998:04:12 00:00:01", command)
            self.assertIn("-FileModifyDate=1998:04:12 00:00:01", command)
            self.assertNotIn("-AllDates=1998:04:12 00:00:01", command)
            self.assertFalse(any("DateTimeOriginal" in part for part in command))
            self.assertFalse(any(part.startswith("-CreateDate=") for part in command))
            self.assertFalse(any(part.startswith("-ModifyDate=") for part in command))
            utime_mock.assert_called_once()

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
        self.assertIn("pageEntries = (payload.entries || []).filter(entry => !state.suppressedCommittedEntryIds.has(entry.entry_id))", picker.PICKER_HTML)
        self.assertIn("const seenEntryIds = new Set();", picker.PICKER_HTML)
        self.assertIn("if (seenEntryIds.has(entry.entry_id)) continue;", picker.PICKER_HTML)
        self.assertIn("explicitScope && payload.has_more", picker.PICKER_HTML)
        self.assertIn('entries.sort((left, right) => String(left.entry_date || "").localeCompare(String(right.entry_date || ""))', picker.PICKER_HTML)
        self.assertIn("state.entryHasMore = explicitScope ? false : Boolean(payload.has_more);", picker.PICKER_HTML)
        self.assertIn('id="loadMoreEntries"', picker.PICKER_HTML)
        self.assertIn("function loadMoreEntries()", picker.PICKER_HTML)
        self.assertIn("function maybeLoadMoreEntriesNearEnd()", picker.PICKER_HTML)
        self.assertIn("index < state.entries.length - 2", picker.PICKER_HTML)
        self.assertIn('params.set("limit", String(state.entryLimit))', picker.PICKER_HTML)
        self.assertIn('id="entryDateJump" type="text"', picker.PICKER_HTML)
        self.assertIn('id="entryDateJumpEnd" type="text"', picker.PICKER_HTML)
        self.assertIn('placeholder="yyyy-mm-dd"', picker.PICKER_HTML)
        self.assertIn("grid-template-columns: 112px 112px auto;", picker.PICKER_HTML)
        self.assertIn("function entryDateScopeLabel(dateTexts)", picker.PICKER_HTML)
        self.assertIn("function isValidEntryDateText(dateText)", picker.PICKER_HTML)
        self.assertIn('"?include_database=1"', picker.PICKER_HTML)
        self.assertIn('id="entryDateJumpButton"', picker.PICKER_HTML)
        self.assertIn("async function jumpToEntryDate()", picker.PICKER_HTML)
        self.assertIn('document.getElementById("filter").value = "all";', picker.PICKER_HTML)
        self.assertIn('state.urlEntryDates = dateTexts;', picker.PICKER_HTML)
        self.assertIn('replaceEntryDateScopeUrl(dateTexts);', picker.PICKER_HTML)
        self.assertIn('for (const inputId of ["entryDateJump", "entryDateJumpEnd"])', picker.PICKER_HTML)
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
        self.assertIn('const candidateImageUrl = `/image/${candidate.token}?max=640`;', picker.PICKER_HTML)
        self.assertIn('data-src="${escapeHtml(candidateImageUrl)}"', picker.PICKER_HTML)
        self.assertIn('?max=96', picker.PICKER_HTML)
        self.assertIn('?max=640', picker.PICKER_HTML)
        self.assertIn("function nextEntryIdAfterCurrent()", picker.PICKER_HTML)
        self.assertIn("function shouldAdvanceAfterDecision(decision, updatedEntry)", picker.PICKER_HTML)
        self.assertIn('decision === "use_external_original"', picker.PICKER_HTML)
        self.assertIn("const currentEntryBeforeSave = state.currentEntry;", picker.PICKER_HTML)
        self.assertIn("Array.isArray(updatedEntry.candidates) ? updatedEntry.candidates : currentCandidates", picker.PICKER_HTML)
        self.assertIn('decision === "external_original_associated_photo"', picker.PICKER_HTML)
        self.assertIn('async function linkAssociatedPhoto(candidate, card)', picker.PICKER_HTML)
        self.assertIn('associated_date_source: "entry"', picker.PICKER_HTML)
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
        self.assertIn('id="previousCropEntryButton"', picker.CROP_HTML)
        self.assertIn('id="nextCropEntryButton"', picker.CROP_HTML)
        self.assertIn("Previous entry (Left)", picker.CROP_HTML)
        self.assertIn("Next entry (Right)", picker.CROP_HTML)
        self.assertIn("function selectAdjacentCropEntry(direction)", picker.CROP_HTML)
        self.assertIn('} else if (event.key === "ArrowLeft") {', picker.CROP_HTML)
        self.assertIn('handled = runCropShortcutButton("previousCropEntryButton", () => selectAdjacentCropEntry(-1));', picker.CROP_HTML)
        self.assertIn('} else if (event.key === "ArrowRight") {', picker.CROP_HTML)
        self.assertIn('handled = runCropShortcutButton("nextCropEntryButton", () => selectAdjacentCropEntry(1));', picker.CROP_HTML)
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
        "gps_latitude",
        "gps_longitude",
        "gps_source",
        "has_gps",
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


def _project_originals_root(base: Path) -> Path:
    root = base / "Source Data" / "Original Photos matching Project365 Entries"
    root.mkdir(parents=True, exist_ok=True)
    return root


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
