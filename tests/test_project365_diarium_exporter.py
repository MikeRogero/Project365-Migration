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
import project365_diarium_exporter as diarium_exporter


class Project365DiariumExporterTests(unittest.TestCase):
    def test_generate_dayone_package_from_canonical_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            import_dir.mkdir()
            private_text = "private text should be inside package only"
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.txt": private_text.encode(),
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _insert_derivative(
                canonical_root=canonical_root,
                entry_id="project365:1998-04-12",
                policy=diarium_exporter.DEFAULT_DERIVATIVE_POLICY,
                payload=_jpeg_with_dimensions(width=12, height=12),
            )
            _insert_derivative(
                canonical_root=canonical_root,
                entry_id="project365:1998-04-13",
                policy=diarium_exporter.DEFAULT_DERIVATIVE_POLICY,
                payload=_jpeg_with_dimensions(width=9, height=9),
            )

            summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=output_dir,
                package_name="pilot.zip",
                start_date="1998-04-01",
                end_date="1998-04-30",
                limit=20,
            )

            self.assertEqual(summary.entry_count, 2)
            self.assertEqual(summary.media_count, 2)
            package_path = Path(summary.package_path)
            manifest_path = Path(summary.manifest_path)
            self.assertTrue(package_path.exists())
            self.assertTrue(manifest_path.exists())
            self.assertNotIn(private_text, manifest_path.read_text())

            with zipfile.ZipFile(package_path) as archive:
                names = archive.namelist()
                self.assertIn("Journal.json", names)
                photo_names = [name for name in names if name.startswith("photos/")]
                self.assertEqual(len(photo_names), 2)
                self.assertTrue(any(name.endswith(".jpeg") for name in photo_names))
                payload = json.loads(archive.read("Journal.json").decode("utf-8"))
            self.assertEqual(payload["metadata"], {"version": "1.0"})
            self.assertEqual(len(payload["entries"]), 2)
            self.assertEqual(payload["entries"][0]["tags"], ["source:project365"])
            self.assertIn("dayone-moment://", payload["entries"][0]["text"])
            self.assertIn(private_text, payload["entries"][0]["text"])
            self.assertEqual(payload["entries"][0]["photos"][0]["type"], "jpeg")
            self.assertEqual(payload["entries"][0]["photos"][0]["width"], 12)
            self.assertEqual(payload["entries"][0]["photos"][0]["height"], 12)
            self.assertEqual(payload["entries"][1]["photos"][0]["type"], "jpeg")
            self.assertEqual(payload["entries"][1]["photos"][0]["width"], 9)
            self.assertEqual(payload["entries"][1]["photos"][0]["height"], 9)
            self.assertEqual(
                sum(len(entry.get("photos", [])) for entry in payload["entries"]),
                2,
            )

            with manifest_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["entry_id"], "project365:1998-04-12")
            self.assertEqual(rows[0]["media_role"], "diarium_derivative")
            self.assertEqual(rows[1]["media_role"], "diarium_derivative")
            self.assertEqual(rows[0]["text_present"], "true")
            self.assertEqual(rows[1]["text_present"], "false")

    def test_generate_dayone_package_requires_available_square_derivatives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )

            with self.assertRaisesRegex(ValueError, "Missing available square derivatives"):
                diarium_exporter.generate_diarium_dayone_package(
                    canonical_root=canonical_root,
                    output_dir=output_dir,
                    package_name="pilot.zip",
                    start_date="1998-04-01",
                    end_date="1998-04-30",
                    limit=20,
                )

    def test_generate_dayone_package_rejects_non_square_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _insert_derivative(
                canonical_root=canonical_root,
                entry_id="project365:1998-04-12",
                policy=diarium_exporter.DEFAULT_DERIVATIVE_POLICY,
                payload=_jpeg_with_dimensions(width=12, height=9),
            )

            with self.assertRaisesRegex(ValueError, "not square"):
                diarium_exporter.generate_diarium_dayone_package(
                    canonical_root=canonical_root,
                    output_dir=output_dir,
                    package_name="pilot.zip",
                    start_date="1998-04-01",
                    end_date="1998-04-30",
                    limit=20,
                )

    def test_generate_dayone_package_requires_derivative_crop_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _insert_derivative(
                canonical_root=canonical_root,
                entry_id="project365:1998-04-12",
                policy=diarium_exporter.DEFAULT_DERIVATIVE_POLICY,
                payload=_jpeg_with_dimensions(width=12, height=12),
                transformation={},
            )

            with self.assertRaisesRegex(ValueError, "Missing derivative crop metadata"):
                diarium_exporter.generate_diarium_dayone_package(
                    canonical_root=canonical_root,
                    output_dir=output_dir,
                    package_name="pilot.zip",
                    start_date="1998-04-01",
                    end_date="1998-04-30",
                    limit=20,
                )

    def test_generate_dayone_package_excludes_non_project365_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            output_dir = canonical_root / "exports" / "diarium_import_batches"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.txt": b"project365 text",
                    "1998-04-12.png": _tiny_png(),
                },
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _insert_derivative(
                canonical_root=canonical_root,
                entry_id="project365:1998-04-12",
                policy=diarium_exporter.DEFAULT_DERIVATIVE_POLICY,
                payload=_jpeg_with_dimensions(width=12, height=12),
            )
            _insert_non_project365_entry(canonical_root, entry_date="1998-04-12")

            summary = diarium_exporter.generate_diarium_dayone_package(
                canonical_root=canonical_root,
                output_dir=output_dir,
                package_name="pilot.zip",
                start_date="1998-04-01",
                end_date="1998-04-30",
                limit=20,
            )

            self.assertEqual(summary.entry_count, 1)
            with zipfile.ZipFile(summary.package_path) as archive:
                payload = json.loads(archive.read("Journal.json").decode("utf-8"))
            self.assertEqual(len(payload["entries"]), 1)
            self.assertNotIn("social staging text", payload["entries"][0]["text"])


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def _insert_derivative(
    canonical_root: Path,
    entry_id: str,
    policy: str,
    payload: bytes,
    transformation: dict[str, object] | None = None,
) -> None:
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
        if transformation is None:
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
                id,
                entry_id,
                role,
                source_file_id,
                internal_filename,
                storage_path,
                sha256,
                byte_size,
                mime_type,
                status,
                review_status,
                selected_default,
                transformation_json,
                import_batch_id,
                created_at,
                updated_at
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


def _insert_non_project365_entry(canonical_root: Path, entry_date: str) -> None:
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        connection.execute(
            """
            INSERT INTO entries (
                id,
                entry_date,
                source_app,
                original_text,
                corrected_text,
                correction_status,
                import_status,
                created_at,
                updated_at
            )
            VALUES (
                'social:x_archive:synthetic-1',
                ?,
                'x_archive',
                'social staging text',
                NULL,
                'uncorrected',
                'not_exported',
                '2026-08-17T00:00:00Z',
                '2026-08-17T00:00:00Z'
            )
            """,
            (entry_date,),
        )


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
