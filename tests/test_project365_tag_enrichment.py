from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as canonical_importer
import project365_diarium_exporter as diarium_exporter
import project365_tag_enrichment as tag_enrichment


class Project365TagEnrichmentTests(unittest.TestCase):
    def test_review_csv_normalizes_dedupes_and_exports_diarium_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            import_dir.mkdir()
            private_text = "private tag test text"
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.txt": private_text.encode(), "1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            review_csv = base / "tag_review.csv"
            review_csv.write_text(
                "\n".join(
                    [
                        "entry_id,tag_type,name,review_status,source",
                        "project365:1998-04-12,person, Alex   Example ,reviewed,manual",
                        "project365:1998-04-12,person,Alex Example,reviewed,manual",
                        "project365:1998-04-12,place,Taipei,confirmed,manual",
                        "project365:1998-04-12,topic,Travel,suggested,manual",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = tag_enrichment.run_tag_enrichment(
                canonical_root=canonical_root,
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
                review_csv=review_csv,
            )

            self.assertTrue(Path(summary.queue_path).exists())
            self.assertNotIn(private_text, Path(summary.queue_path).read_text())
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                tags = connection.execute(
                    "SELECT tag_type, canonical_name, diarium_name, review_status FROM tags"
                ).fetchall()
                people = connection.execute(
                    "SELECT canonical_name, diarium_tag, review_status FROM people"
                ).fetchall()
            self.assertEqual(
                tags,
                [
                    ("place", "Taipei", "place:Taipei", "confirmed"),
                    ("topic", "Travel", "topic:Travel", "suggested"),
                ],
            )
            self.assertEqual(
                people,
                [("Alex Example", "person:Alex Example", "reviewed")],
            )
            tag_map = tag_enrichment.load_exportable_diarium_tags(
                canonical_root / "canonical.db",
                ["project365:1998-04-12"],
            )
            self.assertEqual(
                tag_map["project365:1998-04-12"],
                ["source:project365", "place:Taipei", "person:Alex Example"],
            )

            package_summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=output_dir,
                package_name="pilot.zip",
                start_date="1998-04-01",
                end_date="1998-04-30",
                limit=20,
            )
            with zipfile.ZipFile(package_summary.package_path) as archive:
                payload = json.loads(archive.read("Journal.json").decode("utf-8"))
            self.assertEqual(
                payload["entries"][0]["tags"],
                ["source:project365", "place:Taipei", "person:Alex Example"],
            )

    def test_review_queue_is_metadata_only_for_untagged_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            private_text = "private untagged text"
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.txt": private_text.encode(), "1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )

            summary = tag_enrichment.run_tag_enrichment(
                canonical_root=canonical_root,
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )

            with Path(summary.queue_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["entry_id"], "project365:1998-04-12")
            self.assertEqual(rows[0]["has_text"], "1")
            self.assertNotIn(private_text, Path(summary.queue_path).read_text())


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x04\x00\x00\x00\x01"
        b"\x1f\x15\xc4\x89\x00\x00\x00\x0bIDAT"
        b"\x08\xd7c\xf8\x0f\x00\x01\x01\x01\x00"
        b"\x1b\xb6\xeeV\x00\x00\x00\x00IEND\xaeB`\x82"
    )


if __name__ == "__main__":
    unittest.main()
