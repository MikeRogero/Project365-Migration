from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import project365_batch_gate as batch_gate


class Project365BatchGateTests(unittest.TestCase):
    def test_batch_gate_passes_matching_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            manifest = base / "manifest.csv"
            reconciliation = base / "reconciliation.csv"
            report = base / "gate.json"
            manifest.write_text(
                "\n".join(
                    [
                        "entry_id,media_asset_id",
                        "project365:1998-04-12,media-1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            reconciliation.write_text(
                "\n".join(
                    [
                        "entry_id,match_status,text_status,media_file_count,tag_count,has_reviewable_updates",
                        "project365:1998-04-12,matched,unchanged,1,1,false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = batch_gate.validate_batch_gate(
                manifest_path=manifest,
                reconciliation_path=reconciliation,
                report_path=report,
                batch_name="pilot",
            )

            self.assertTrue(summary.passed)
            self.assertEqual(summary.issues, [])
            payload = json.loads(report.read_text())
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["expected_entries"], 1)

    def test_batch_gate_accepts_normalized_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            manifest = base / "manifest.csv"
            reconciliation = base / "reconciliation.csv"
            report = base / "gate.json"
            manifest.write_text(
                "\n".join(
                    [
                        "entry_id,media_asset_id",
                        "project365:1998-04-12,media-1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            reconciliation.write_text(
                "\n".join(
                    [
                        "entry_id,match_status,text_status,media_file_count,tag_count,has_reviewable_updates",
                        "project365:1998-04-12,matched,normalized_unchanged,1,1,false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = batch_gate.validate_batch_gate(
                manifest_path=manifest,
                reconciliation_path=reconciliation,
                report_path=report,
                batch_name="pilot",
            )

            self.assertTrue(summary.passed)
            self.assertEqual(summary.issues, [])

    def test_batch_gate_fails_on_count_or_diff_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            manifest = base / "manifest.csv"
            reconciliation = base / "reconciliation.csv"
            report = base / "gate.json"
            manifest.write_text(
                "\n".join(
                    [
                        "entry_id,media_asset_id",
                        "project365:1998-04-12,media-1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            reconciliation.write_text(
                "\n".join(
                    [
                        "entry_id,match_status,text_status,media_file_count,tag_count,has_reviewable_updates",
                        "project365:1998-04-12,matched,changed,0,0,true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = batch_gate.validate_batch_gate(
                manifest_path=manifest,
                reconciliation_path=reconciliation,
                report_path=report,
                batch_name="bad",
            )

            self.assertFalse(summary.passed)
            self.assertIn("media_count_mismatch: manifest=1 reconciliation=0", summary.issues)
            self.assertIn("project365:1998-04-12: text_status=changed", summary.issues)
            self.assertIn("project365:1998-04-12: missing_source_tag", summary.issues)
            self.assertIn("project365:1998-04-12: has_reviewable_updates=true", summary.issues)


if __name__ == "__main__":
    unittest.main()
