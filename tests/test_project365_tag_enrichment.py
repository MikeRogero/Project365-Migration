from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as canonical_importer
import project365_digikam_people_importer as digikam_people
import project365_diarium_exporter as diarium_exporter
import project365_tag_enrichment as tag_enrichment


class Project365TagEnrichmentTests(unittest.TestCase):
    def test_export_label_strips_only_its_category(self) -> None:
        self.assertEqual(tag_enrichment._export_tag_label("person: Alex Example", "person"), "Alex Example")
        self.assertEqual(tag_enrichment._export_tag_label("stories: Holiday", "stories"), "Holiday")
        self.assertEqual(tag_enrichment._export_tag_label("place:Taipei", "person"), "place:Taipei")
        self.assertEqual(tag_enrichment._export_tag_label("source:project365", "source"), "source:project365")

    def test_digikam_sidecar_people_export_as_entry_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": _tiny_png()})
            canonical_importer.import_project365_exports(import_dir=import_dir, canonical_root=canonical_root)
            _insert_derivative(canonical_root, "project365:1998-04-12")
            entry_id = "project365:1998-04-12"
            sidecar_root = canonical_root / "media" / "diarium_derivatives" / diarium_exporter.DEFAULT_DERIVATIVE_POLICY
            (sidecar_root / "project365_1998-04-12.jpg.xmp").write_text(
                _people_xmp("Alex Example", "Rejected Example")
            )
            (sidecar_root / "Project365 Working Copy - 1998-04-12 - sq2560.jpg.xmp").write_text(
                _people_xmp("Stale Example")
            )
            initial = digikam_people.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[sidecar_root],
                suggestions_csv=None,
                report_path=canonical_root / "exports" / "verification_reports" / "people.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )
            self.assertEqual(initial.removed_suggestion_count, 0)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                tag_enrichment.upsert_person(connection, entry_id, "Unreviewed Example", "suggested", "manual")
                tag_enrichment.upsert_person(connection, entry_id, "Rejected Example", "rejected", "manual_review")
                tag_enrichment.upsert_person(connection, entry_id, "Legacy Reviewed", "reviewed", "digikam_xmp")
                connection.commit()
                photo_people = connection.execute("SELECT canonical_name FROM media_people").fetchall()
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM people WHERE canonical_name = 'Stale Example'"
                ).fetchone())
            self.assertEqual(photo_people, [("Alex Example",), ("Rejected Example",)])

            tags = tag_enrichment.load_exportable_diarium_tags(canonical_root / "canonical.db", [entry_id])
            self.assertEqual(tags[entry_id], ["source:project365"])

            package = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=canonical_root / "exports" / "diarium_import_batches",
                package_name="pilot.zip",
                start_date="1998-04-12",
                end_date="1998-04-12",
                limit=1,
            )
            with zipfile.ZipFile(package.package_path) as archive:
                payload = json.loads(archive.read("Journal.json"))
            self.assertEqual(payload["entries"][0]["tags"], ["source:project365", "Alex Example"])

            (sidecar_root / "project365_1998-04-12.jpg.xmp").write_text(_people_xmp("Bea Example"))
            renamed = digikam_people.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[sidecar_root],
                suggestions_csv=None,
                report_path=canonical_root / "exports" / "verification_reports" / "people.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )
            self.assertEqual(renamed.removed_suggestion_count, 1)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                self.assertEqual(
                    connection.execute("SELECT canonical_name FROM media_people").fetchall(),
                    [("Bea Example",)],
                )
                self.assertEqual(
                    connection.execute("SELECT canonical_name, review_status FROM people ORDER BY canonical_name").fetchall(),
                    [
                        ("Bea Example", "suggested"),
                        ("Legacy Reviewed", "reviewed"),
                        ("Rejected Example", "rejected"),
                        ("Unreviewed Example", "suggested"),
                    ],
                )

            (sidecar_root / "project365_1998-04-12.jpg.xmp").write_text("<broken")
            malformed = digikam_people.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[sidecar_root],
                suggestions_csv=None,
                report_path=canonical_root / "exports" / "verification_reports" / "people.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )
            self.assertEqual(malformed.error_count, 1)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                self.assertEqual(connection.execute("SELECT canonical_name FROM media_people").fetchall(), [("Bea Example",)])

            (sidecar_root / "project365_1998-04-12.jpg.xmp").unlink()
            removed = digikam_people.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[sidecar_root],
                suggestions_csv=None,
                report_path=canonical_root / "exports" / "verification_reports" / "people.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )
            self.assertEqual(removed.removed_suggestion_count, 1)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_people").fetchone()[0], 0)
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM people WHERE canonical_name = 'Bea Example'"
                ).fetchone())

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
            _insert_derivative(canonical_root, "project365:1998-04-12")
            review_csv = base / "tag_review.csv"
            review_csv.write_text(
                "\n".join(
                    [
                        "entry_id,tag_type,name,review_status,source",
                        "project365:1998-04-12,person, Alex   Example ,reviewed,manual",
                        "project365:1998-04-12,person,Alex Example,reviewed,manual",
                        "project365:1998-04-12,place,Taipei,confirmed,manual",
                        "project365:1998-04-12,things,Camera,reviewed,manual",
                        "project365:1998-04-12,stories,Holiday,reviewed,manual",
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
                    ("things", "Camera", "things:Camera", "reviewed"),
                    ("stories", "Holiday", "stories:Holiday", "reviewed"),
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
                ["source:project365", "Taipei", "Holiday", "Camera", "Alex Example"],
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
                ["source:project365", "Taipei", "Holiday", "Camera", "Alex Example"],
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


def _people_xmp(*names: str) -> str:
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<dc:subject><rdf:Bag>'
        + ''.join(f'<rdf:li>People|{name}</rdf:li>' for name in names)
        + '</rdf:Bag></dc:subject>'
        '</x:xmpmeta>'
    )


def _insert_derivative(canonical_root: Path, entry_id: str) -> None:
    policy = diarium_exporter.DEFAULT_DERIVATIVE_POLICY
    payload = _jpeg_with_dimensions(12, 12)
    filename = f"{entry_id.replace(':', '_')}.jpg"
    derivative_path = canonical_root / "media" / "diarium_derivatives" / policy / filename
    derivative_path.parent.mkdir(parents=True, exist_ok=True)
    derivative_path.write_bytes(payload)
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        source_file_id, import_batch_id = connection.execute(
            """
            SELECT source_file_id, import_batch_id
            FROM media_assets
            WHERE entry_id = ? AND selected_default = 1
            """,
            (entry_id,),
        ).fetchone()
        transformation = {
            "crop": {
                "x": 0,
                "y": 0,
                "size": 1,
                "candidate_width": 1,
                "candidate_height": 1,
                "source": "manual",
                "unit": "source_pixels",
                "shape": "square",
            }
        }
        connection.execute(
            """
            INSERT INTO media_assets (
                id, entry_id, role, source_file_id, internal_filename, storage_path,
                sha256, byte_size, mime_type, status, review_status, selected_default,
                transformation_json, import_batch_id, created_at, updated_at
            )
            VALUES (?, ?, 'diarium_derivative', ?, ?, ?, ?, ?, 'image/jpeg',
                    'available', 'unreviewed', 0, ?, ?, '2026-08-17T00:00:00Z',
                    '2026-08-17T00:00:00Z')
            """,
            (
                f"{entry_id}:diarium_derivative:{policy}",
                entry_id,
                source_file_id,
                derivative_path.name,
                str(derivative_path),
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                json.dumps(transformation, sort_keys=True),
                import_batch_id,
            ),
        )


def _jpeg_with_dimensions(width: int, height: int) -> bytes:
    sof = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


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
