from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import project365_social_adapter_config as social_config


class Project365SocialAdapterConfigTests(unittest.TestCase):
    def test_stage_x_archive_records_with_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source_root = base / "x_archive"
            source_data = source_root / "data"
            source_data.mkdir(parents=True)
            private_text = "private social post text"
            source_data.joinpath("tweets.js").write_text(
                "window.YTD.tweets.part0 = "
                + json.dumps(
                    [
                        {
                            "tweet": {
                                "id_str": "1001",
                                "created_at": "Mon Apr 12 12:00:00 +0000 1998",
                                "full_text": private_text,
                                "entities": {
                                    "urls": [{"expanded_url": "https://example.test/post"}],
                                },
                                "extended_entities": {
                                    "media": [
                                        {
                                            "media_url_https": "https://example.test/media.jpg",
                                        }
                                    ]
                                },
                                "geo": {"coordinates": [25.033, 121.565]},
                                "place": {"full_name": "Taipei"},
                            }
                        }
                    ]
                ),
                encoding="utf-8",
            )

            staging_path = base / "staging" / "events.jsonl"
            report_dir = base / "reports"
            summary = social_config.stage_social_events(
                config_path=Path("config/social_adapters/x_archive_posts_v1.json"),
                source_root=source_root,
                staging_path=staging_path,
                report_dir=report_dir,
            )

            self.assertEqual(summary.adapter_id, "x_archive_posts_v1")
            self.assertEqual(summary.source_files, 1)
            self.assertEqual(summary.staged_events, 1)
            staged = json.loads(staging_path.read_text(encoding="utf-8"))
            self.assertEqual(staged["source_name"], "x_archive")
            self.assertEqual(staged["source_id"], "1001")
            self.assertEqual(staged["text"], private_text)
            self.assertEqual(staged["urls"], ["https://example.test/post"])
            self.assertEqual(staged["media_references"], ["https://example.test/media.jpg"])
            self.assertEqual(staged["latitude"], 25.033)
            self.assertEqual(staged["longitude"], 121.565)
            self.assertEqual(staged["place_name"], "Taipei")
            self.assertEqual(
                staged["provenance"],
                {
                    "source_file_id": "tweets",
                    "source_file_path": "data/tweets.js",
                    "source_record_id": "1001",
                },
            )

            report_text = Path(summary.report_path).read_text(encoding="utf-8")
            self.assertNotIn(private_text, report_text)
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["source_id"], "1001")
            self.assertEqual(rows[0]["status"], "staged")

    def test_missing_required_source_file_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source_root = base / "empty_export"
            source_root.mkdir()

            with self.assertRaisesRegex(FileNotFoundError, "Missing required source file"):
                social_config.stage_social_events(
                    config_path=Path("config/social_adapters/x_archive_posts_v1.json"),
                    source_root=source_root,
                    staging_path=base / "events.jsonl",
                    report_dir=base / "reports",
                )


if __name__ == "__main__":
    unittest.main()
