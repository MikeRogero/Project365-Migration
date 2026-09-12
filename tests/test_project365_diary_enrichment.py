from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as canonical_importer
import project365_diary_enrichment as enrichment
import project365_diarium_exporter as diarium_exporter


class Project365DiaryEnrichmentTests(unittest.TestCase):
    def test_enrichment_html_links_back_to_control_step_and_uses_url_filters(self) -> None:
        self.assertIn('href="/?step=diary_enrichment"', enrichment.ENRICHMENT_HTML)
        self.assertIn("new URLSearchParams(window.location.search)", enrichment.ENRICHMENT_HTML)
        self.assertIn('params.set("start_date", state.startDate)', enrichment.ENRICHMENT_HTML)
        self.assertIn('fetchJson(entriesApiUrl())', enrichment.ENRICHMENT_HTML)

    def test_entry_list_requires_date_filter_before_loading_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))

            result = enrichment.entry_list(canonical_root)

            self.assertTrue(result["requires_date_filter"])
            self.assertEqual(result["entries"], [])

    def test_entry_list_filters_by_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))

            result = enrichment.entry_list(
                canonical_root,
                start_date="1998-04-12",
                end_date="1998-04-12",
            )

            self.assertEqual([entry["entry_id"] for entry in result["entries"]], ["project365:1998-04-12"])
            self.assertFalse(result["has_more"])

    def test_entry_target_photo_uses_working_copy_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))

            missing = enrichment.entry_list(
                canonical_root,
                start_date="1998-04-12",
                end_date="1998-04-12",
            )["entries"][0]
            _insert_derivative(canonical_root, "project365:1998-04-12", _jpeg_with_dimensions(12, 12))
            ready = enrichment.entry_list(
                canonical_root,
                start_date="1998-04-12",
                end_date="1998-04-12",
            )["entries"][0]

            self.assertFalse(missing["primary_photo_ready"])
            self.assertEqual(missing["primary_photo_status"], "working_copy_missing")
            self.assertTrue(ready["primary_photo_ready"])
            self.assertEqual(ready["primary_photo"]["role"], "diarium_derivative")

    def test_entry_target_includes_imported_people_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical_root = _sample_canonical_root(Path(temp_dir))
            _insert_derivative(canonical_root, "project365:1998-04-12", _jpeg_with_dimensions(12, 12))
            _insert_people(canonical_root, "project365:1998-04-12", ["Alex Example", "Bea Example"])

            listed = enrichment.entry_list(
                canonical_root,
                start_date="1998-04-12",
                end_date="1998-04-12",
            )["entries"][0]
            detail = enrichment.entry_detail(canonical_root, "project365:1998-04-12")

            self.assertEqual(listed["people_names"], ["Alex Example", "Bea Example"])
            self.assertEqual(listed["primary_photo"]["people_names"], ["Alex Example", "Bea Example"])
            self.assertEqual(detail["people_names"], ["Alex Example", "Bea Example"])
            self.assertIn("peopleScript(entry.people_names)", enrichment.ENRICHMENT_HTML)
            self.assertIn('id="entryPeople"', enrichment.ENRICHMENT_HTML)

    def test_flagged_candidate_can_be_added_as_associated_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _sample_canonical_root(base)
            source_id = _insert_flagged_photo(canonical_root, base, associated_entry_date="1998-04-12")

            detail = enrichment.entry_detail(canonical_root, "project365:1998-04-12")
            self.assertEqual([candidate["media_asset_id"] for candidate in detail["candidates"]], [source_id])

            updated = enrichment.add_associated_photo(
                canonical_root,
                target_entry_id="project365:1998-04-12",
                source_media_asset_id=source_id,
            )

            self.assertEqual(updated["associated_count"], 1)
            self.assertEqual(updated["attached_photos"][0]["sha256"], detail["candidates"][0]["sha256"])
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                rows = connection.execute(
                    """
                    SELECT role, review_status, json_extract(transformation_json, '$.associated_entry_date')
                    FROM media_assets
                    WHERE entry_id = 'project365:1998-04-12'
                        AND role = 'external_original_associated_photo'
                    """
                ).fetchall()
            self.assertEqual(rows, [("external_original_associated_photo", "confirmed", "1998-04-12")])

    def test_dropped_photo_is_copied_and_attached_to_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _sample_canonical_root(base)

            updated = enrichment.add_dropped_associated_photo(
                canonical_root,
                target_entry_id="project365:1998-04-12",
                filename="extra.jpg",
                content_type="image/jpeg",
                payload=_jpeg_with_dimensions(12, 12),
            )

            self.assertEqual(updated["associated_count"], 1)
            stored = Path(updated["attached_photos"][0]["path"])
            self.assertTrue(stored.exists())
            self.assertIn("Source Data/Diary Enrichment Photos/1998-04", str(stored))

    def test_candidate_can_create_exportable_subentry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _sample_canonical_root(base)
            source_id = _insert_flagged_photo(canonical_root, base, associated_entry_date="1998-04-12")

            result = enrichment.create_subentry_from_candidate(
                canonical_root,
                parent_entry_id="project365:1998-04-12",
                source_media_asset_id=source_id,
            )

            subentry_id = result["subentry_id"]
            self.assertTrue(subentry_id.startswith("project365:1998-04-12:sub:"))
            filtered = enrichment.entry_list(
                canonical_root,
                start_date="1998-04-12",
                end_date="1998-04-12",
            )
            entry_ids = [entry["entry_id"] for entry in filtered["entries"]]
            self.assertLess(entry_ids.index("project365:1998-04-12"), entry_ids.index(subentry_id))
            self.assertTrue(result["entry"]["is_subentry"])
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                row = connection.execute(
                    """
                    SELECT source_app
                    FROM entries
                    WHERE id = ?
                    """,
                    (subentry_id,),
                ).fetchone()
                media_role = connection.execute(
                    """
                    SELECT role
                    FROM media_assets
                    WHERE entry_id = ?
                    """,
                    (subentry_id,),
                ).fetchone()[0]
            self.assertEqual(row[0], enrichment.ENRICHMENT_SOURCE_APP)
            self.assertEqual(media_role, "external_original_reference")

    def test_diarium_exporter_includes_enrichment_subentry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _sample_canonical_root(base)
            source_id = _insert_flagged_photo(canonical_root, base, associated_entry_date="1998-04-12")
            result = enrichment.create_subentry_from_candidate(
                canonical_root,
                parent_entry_id="project365:1998-04-12",
                source_media_asset_id=source_id,
            )
            _insert_derivative(canonical_root, "project365:1998-04-12", _jpeg_with_dimensions(12, 12))
            _insert_derivative(canonical_root, result["subentry_id"], _jpeg_with_dimensions(12, 12))

            summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=canonical_root / "exports" / "diarium_import_batches",
                package_name="enriched.zip",
                start_date="1998-04-12",
                end_date="1998-04-12",
                limit=10,
            )

            with zipfile.ZipFile(summary.package_path) as archive:
                payload = json.loads(archive.read("Journal.json").decode("utf-8"))
            self.assertEqual(summary.entry_count, 2)
            self.assertEqual(len(payload["entries"]), 2)


def _sample_canonical_root(base: Path) -> Path:
    import_dir = base / "Import"
    canonical_root = base / "Project365Canonical"
    import_dir.mkdir()
    _write_zip(
        import_dir / "1998-04.zip",
        {
            "1998-04-12.txt": b"private",
            "1998-04-12.png": _tiny_png(),
            "1998-04-13.png": _tiny_png(),
        },
    )
    canonical_importer.import_project365_exports(
        import_dir=import_dir,
        canonical_root=canonical_root,
    )
    return canonical_root


def _insert_flagged_photo(canonical_root: Path, base: Path, associated_entry_date: str) -> str:
    source_path = base / "flagged.jpg"
    payload = _jpeg_with_dimensions(12, 12)
    source_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    source_id = f"project365:1998-04-13:external_original_associated_photo:{digest[:16]}"
    transformation = {
        "source": "external_original_associated_photo",
        "associated_entry_date": associated_entry_date,
        "associated_date_source": "manual",
        "original_is_read_only": True,
    }
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        source_file_id, import_batch_id = connection.execute(
            """
            SELECT source_file_id, import_batch_id
            FROM media_assets
            WHERE entry_id = 'project365:1998-04-13'
                AND role = 'project365_export_png'
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO media_assets (
                id, entry_id, role, source_file_id, internal_filename, storage_path,
                sha256, byte_size, mime_type, status, review_status, selected_default,
                transformation_json, import_batch_id, created_at, updated_at
            )
            VALUES (?, 'project365:1998-04-13', 'external_original_associated_photo',
                    ?, 'flagged.jpg', ?, ?, ?, 'image/jpeg', 'available', 'confirmed',
                    0, ?, ?, '2026-08-23T00:00:00Z', '2026-08-23T00:00:00Z')
            """,
            (
                source_id,
                source_file_id,
                str(source_path),
                digest,
                len(payload),
                json.dumps(transformation, sort_keys=True),
                import_batch_id,
            ),
        )
    return source_id


def _insert_derivative(canonical_root: Path, entry_id: str, payload: bytes) -> None:
    policy = diarium_exporter.DEFAULT_DERIVATIVE_POLICY
    derivative_path = canonical_root / "media" / "diarium_derivatives" / policy / f"{entry_id.replace(':', '_')}.jpg"
    derivative_path.parent.mkdir(parents=True, exist_ok=True)
    derivative_path.write_bytes(payload)
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
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        import_batch_id = connection.execute(
            """
            SELECT id
            FROM import_batches
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO media_assets (
                id, entry_id, role, source_file_id, internal_filename, storage_path,
                sha256, byte_size, mime_type, status, review_status, selected_default,
                transformation_json, import_batch_id, created_at, updated_at
            )
            VALUES (?, ?, 'diarium_derivative', NULL, ?, ?, ?, ?, 'image/jpeg',
                    'available', 'unreviewed', 0, ?, ?, '2026-08-23T00:00:00Z',
                    '2026-08-23T00:00:00Z')
            """,
            (
                f"{entry_id}:diarium_derivative:{policy}",
                entry_id,
                derivative_path.name,
                str(derivative_path),
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                json.dumps(transformation, sort_keys=True),
                import_batch_id,
            ),
        )


def _insert_people(canonical_root: Path, entry_id: str, names: list[str]) -> None:
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        for name in names:
            connection.execute(
                """
                INSERT INTO people (entry_id, canonical_name, diarium_tag, review_status, source)
                VALUES (?, ?, ?, 'suggested', 'digikam_xmp')
                """,
                (entry_id, name, f"person:{name}"),
            )


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


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


if __name__ == "__main__":
    unittest.main()
