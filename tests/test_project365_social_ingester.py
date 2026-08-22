from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import project365_social_adapter_config as social_config
import project365_social_ingester as social_ingester


class Project365SocialIngesterTests(unittest.TestCase):
    def test_ingests_staged_x_record_into_canonical_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source_root = _write_x_archive(
                base,
                [
                    {
                        "tweet": {
                            "id_str": "1001",
                            "created_at": "Mon Apr 12 12:00:00 +0000 1998",
                            "full_text": "private social post",
                            "entities": {
                                "urls": [{"expanded_url": "https://example.test/post"}],
                            },
                            "extended_entities": {
                                "media": [{"media_url_https": "https://example.test/media.jpg"}],
                            },
                            "geo": {"coordinates": [25.033, 121.565]},
                            "place": {"full_name": "Taipei"},
                        }
                    }
                ],
            )
            staging_path = base / "staging" / "events.jsonl"
            social_config.stage_social_events(
                config_path=Path("config/social_adapters/x_archive_posts_v1.json"),
                source_root=source_root,
                staging_path=staging_path,
                report_dir=base / "reports",
            )

            summary = social_ingester.import_social_staging(
                canonical_root=base / "Project365Canonical",
                staging_jsonl=staging_path,
            )

            self.assertEqual(summary.imported_count, 1)
            self.assertEqual(summary.skipped_count, 0)
            with sqlite3.connect(base / "Project365Canonical" / "canonical.db") as connection:
                entry = connection.execute(
                    """
                    SELECT id, entry_date, source_app, original_text, import_status
                    FROM entries
                    """
                ).fetchone()
                source = connection.execute(
                    """
                    SELECT source_type, source_path, validation_status
                    FROM source_files
                    """
                ).fetchone()
                entry_source = connection.execute(
                    """
                    SELECT source_role, internal_filename
                    FROM entry_sources
                    """
                ).fetchone()
                location = connection.execute(
                    """
                    SELECT latitude, longitude, source, review_status
                    FROM locations
                    """
                ).fetchone()
                tags = connection.execute(
                    """
                    SELECT tag_type, canonical_name, review_status
                    FROM tags
                    ORDER BY tag_type, canonical_name
                    """
                ).fetchall()
            self.assertEqual(
                entry,
                (
                    "social:x_archive:1001",
                    "1998-04-12",
                    "x_archive",
                    "private social post",
                    "canonical_only",
                ),
            )
            self.assertEqual(source[0], "social_export")
            self.assertIn("data/tweets.js", source[1])
            self.assertEqual(source[2], "staged")
            self.assertEqual(entry_source, ("social_record", "1001"))
            self.assertEqual(location, (25.033, 121.565, "x_archive", "confirmed"))
            self.assertIn(("source", "x_archive", "confirmed"), tags)
            self.assertIn(("place", "Taipei", "reviewed"), tags)
            self.assertIn(("url", "https://example.test/post", "reviewed"), tags)

    def test_skips_deleted_or_unavailable_empty_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            staging_path = base / "events.jsonl"
            staging_path.write_text(
                json.dumps(
                    {
                        "adapter_id": "x_archive_posts_v1",
                        "source_name": "x_archive",
                        "source_id": "deleted-1",
                        "timestamp": "1998-04-12T12:00:00Z",
                        "text": "",
                        "urls": [],
                        "media_references": [],
                        "latitude": None,
                        "longitude": None,
                        "place_name": None,
                        "provenance": {
                            "source_file_id": "tweets",
                            "source_file_path": "data/tweets.js",
                            "source_record_id": "deleted-1",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            summary = social_ingester.import_social_staging(
                canonical_root=base / "Project365Canonical",
                staging_jsonl=staging_path,
            )

            self.assertEqual(summary.imported_count, 0)
            self.assertEqual(summary.skipped_count, 1)

    def test_selected_only_imports_only_selected_records_and_handles_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            staging_path = base / "events.jsonl"
            records = [
                _staging_record("selected", "1998-04-12T23:30:00+08:00", "import"),
                _staging_record("canonical", "1998-04-13T00:30:00+08:00", "canonical_only"),
            ]
            staging_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            summary = social_ingester.import_social_staging(
                canonical_root=base / "Project365Canonical",
                staging_jsonl=staging_path,
                selected_only=True,
            )

            self.assertEqual(summary.imported_count, 1)
            self.assertEqual(summary.skipped_count, 1)
            with sqlite3.connect(base / "Project365Canonical" / "canonical.db") as connection:
                rows = connection.execute(
                    "SELECT id, entry_date, import_status FROM entries"
                ).fetchall()
            self.assertEqual(rows, [("social:x_archive:selected", "1998-04-12", "import")])


def _write_x_archive(base: Path, tweets: list[dict[str, object]]) -> Path:
    source_root = base / "x_archive"
    data = source_root / "data"
    data.mkdir(parents=True)
    data.joinpath("tweets.js").write_text(
        "window.YTD.tweets.part0 = " + json.dumps(tweets),
        encoding="utf-8",
    )
    return source_root


def _staging_record(source_id: str, timestamp: str, action: str) -> dict[str, object]:
    return {
        "adapter_id": "x_archive_posts_v1",
        "source_name": "x_archive",
        "source_id": source_id,
        "timestamp": timestamp,
        "text": f"text {source_id}",
        "urls": [],
        "media_references": [],
        "latitude": None,
        "longitude": None,
        "place_name": None,
        "diarium_action": action,
        "provenance": {
            "source_file_id": "tweets",
            "source_file_path": "data/tweets.js",
            "source_record_id": source_id,
        },
    }


if __name__ == "__main__":
    unittest.main()
