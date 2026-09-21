from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import project365_canonical_importer as canonical_importer
import project365_media_derivatives as derivatives


class Project365MediaDerivativesTests(unittest.TestCase):
    def test_default_jpeg_policy_uses_square_output_name(self) -> None:
        self.assertEqual(
            derivatives.derivative_policy_name("jpeg", 2560, 88),
            "Project365_square_2560_q88",
        )

    def test_generate_jpeg_derivative_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)

            summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )

            self.assertEqual(summary.generated_count, 1)
            report_path = Path(summary.report_path)
            with report_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            derivative_path = Path(rows[0]["derivative_path"])
            self.assertTrue(derivative_path.exists())
            self.assertEqual(
                derivative_path.name,
                "Project365 Working Copy - 1998-04-12 - sq64.jpg",
            )
            self.assertEqual(rows[0]["format"], "jpeg")

            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                db_rows = connection.execute(
                    """
                    SELECT role, mime_type, selected_default
                    FROM media_assets
                    WHERE role = 'diarium_derivative'
                    """
                ).fetchall()
            self.assertEqual(db_rows, [("diarium_derivative", "image/jpeg", 0)])

    def test_generate_jpeg_derivative_records_associated_photo_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)
            associated_path = base / "associated.jpg"
            associated_path.write_bytes(_tiny_png())
            _insert_associated_original(canonical_root, associated_path)

            default_readiness = derivatives.derivative_readiness_summary(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )
            self.assertEqual(default_readiness.source_count, 1)
            self.assertEqual(default_readiness.associated_source_count, 0)

            summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
                include_associated=True,
            )

            self.assertEqual(summary.generated_count, 2)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                rows = connection.execute(
                    """
                    SELECT id, role, transformation_json
                    FROM media_assets
                    WHERE role IN ('diarium_derivative', 'diarium_associated_derivative')
                    ORDER BY role, id
                    """
                ).fetchall()
            self.assertEqual(
                [row[1] for row in rows],
                ["diarium_associated_derivative", "diarium_derivative"],
            )
            self.assertEqual(
                rows[1][0],
                "project365:1998-04-12:diarium_derivative:jpeg_64_q80",
            )
            associated_transformation = json.loads(rows[0][2])
            self.assertEqual(associated_transformation["source_role"], "associated")
            self.assertEqual(associated_transformation["derivative_role"], "diarium_associated_derivative")
            with Path(summary.report_path).open(newline="") as handle:
                report_rows = list(csv.DictReader(handle))
            associated_path = next(
                Path(row["derivative_path"])
                for row in report_rows
                if ":diarium_associated_derivative:" in row["derivative_media_asset_id"]
            )
            self.assertTrue(
                associated_path.name.startswith(
                    "Project365 Working Copy - 1998-04-12 - associated sq64 - "
                )
            )

    def test_missing_review_crop_is_not_ready_for_derivative_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )

            summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )

            self.assertEqual(summary.generated_count, 0)
            self.assertEqual(summary.not_ready_count, 1)
            self.assertEqual(summary.not_ready_dates, ("1998-04-12",))
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "not_ready_missing_crop")
            self.assertFalse(Path(rows[0]["derivative_path"]).exists())

    def test_progress_sink_reports_processed_totals_during_derivative_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            progress_messages: list[str] = []

            derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
                progress_interval=1,
                progress_sink=progress_messages.append,
            )

            self.assertEqual(
                progress_messages,
                [
                    "Progress: 0/2 sources · generated 0 · skipped 0 · not ready 0",
                    "Progress: 1/2 sources · generated 0 · skipped 0 · not ready 1 · current project365:1998-04-12",
                    "Progress: 2/2 sources · generated 0 · skipped 0 · not ready 2 · current project365:1998-04-13",
                ],
            )

    def test_staged_estimate_makes_derivative_ready_without_confirming_source_crop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            external_path = base / "external-original.png"
            external_path.write_bytes(_tiny_png())
            _insert_external_original_without_crop(canonical_root, external_path)
            _write_staged_crop(
                canonical_root,
                "project365:1998-04-12",
                external_path,
                {
                    "x": 0,
                    "y": 0,
                    "size": 1,
                    "candidate_width": 1,
                    "candidate_height": 1,
                    "source": "estimated",
                },
            )

            readiness = derivatives.derivative_readiness_summary(
                canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )
            self.assertEqual(readiness.ready_count, 1)
            self.assertEqual(readiness.not_ready_count, 0)

            summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )

            self.assertEqual(summary.generated_count, 1)
            self.assertEqual(summary.not_ready_count, 0)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                source_transformation, derivative_transformation = connection.execute(
                    """
                    SELECT source.transformation_json, derivative.transformation_json
                    FROM media_assets AS source
                    JOIN media_assets AS derivative
                        ON derivative.entry_id = source.entry_id
                    WHERE source.role = 'external_original_reference'
                        AND derivative.role = 'diarium_derivative'
                    """
                ).fetchone()
            self.assertIsNone(derivatives._review_crop_from_transformation(source_transformation))
            self.assertEqual(json.loads(derivative_transformation)["crop"]["source"], "estimated")

    def test_unchanged_derivative_is_skipped_on_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)
            first_summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )

            with mock.patch.object(derivatives, "_convert_image", side_effect=AssertionError("should skip")):
                second_summary = derivatives.generate_derivatives(
                    canonical_root=canonical_root,
                    output_format="jpeg",
                    long_edge=64,
                    quality=80,
                )

            self.assertEqual(first_summary.generated_count, 1)
            self.assertEqual(second_summary.generated_count, 0)
            self.assertEqual(second_summary.skipped_count, 1)
            with Path(second_summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "skipped")
            readiness = derivatives.derivative_readiness_summary(
                canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )
            self.assertEqual(readiness.current_count, 1)
            self.assertEqual(readiness.needs_update_count, 0)

    def test_date_range_limits_working_copy_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)

            summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
                start_date="1998-04-13",
                end_date="1998-04-13",
            )

            self.assertEqual(summary.generated_count, 1)
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["entry_id"] for row in rows], ["project365:1998-04-13"])

    def test_force_regenerates_current_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)
            derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )

            def fake_convert(
                source_path: Path,
                output_path: Path,
                output_format: str,
                long_edge: int,
                quality: int,
                crop: dict[str, object] | None,
            ) -> None:
                output_path.write_bytes(_tiny_png())

            with mock.patch.object(derivatives, "_convert_image", side_effect=fake_convert) as convert:
                summary = derivatives.generate_derivatives(
                    canonical_root=canonical_root,
                    output_format="jpeg",
                    long_edge=64,
                    quality=80,
                    force=True,
                )

            self.assertEqual(summary.generated_count, 1)
            self.assertEqual(summary.skipped_count, 0)
            self.assertEqual(convert.call_count, 1)

    def test_force_reuses_recent_existing_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)
            existing_path = (
                canonical_root
                / "media"
                / "diarium_derivatives"
                / "jpeg_64_q80"
                / "1998-04"
                / "Project365 Working Copy - 1998-04-12 - sq64.jpg"
            )
            existing_path.parent.mkdir(parents=True)
            existing_path.write_bytes(_tiny_png())

            with mock.patch.object(derivatives, "_convert_image_atomically", side_effect=AssertionError("should reuse")), mock.patch.object(
                derivatives,
                "_image_dimensions",
                return_value=(1, 1),
            ):
                summary = derivatives.generate_derivatives(
                    canonical_root=canonical_root,
                    output_format="jpeg",
                    long_edge=64,
                    quality=80,
                    force=True,
                    reuse_existing_newer_than="2000-01-01T00:00:00+00:00",
                )

            self.assertEqual(summary.generated_count, 0)
            self.assertEqual(summary.skipped_count, 1)
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "skipped_recent_existing")
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                storage_path = connection.execute(
                    """
                    SELECT storage_path
                    FROM media_assets
                    WHERE role = 'diarium_derivative'
                    """
                ).fetchone()[0]
            self.assertEqual(storage_path, str(existing_path))

    def test_changed_crop_regenerates_existing_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)
            first_summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )
            crop = {
                "review_crop": {
                    "x": 0,
                    "y": 0,
                    "size": 1,
                    "candidate_width": 1,
                    "candidate_height": 1,
                    "source": "estimated",
                }
            }
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                connection.execute(
                    """
                    UPDATE media_assets
                    SET transformation_json = ?
                    WHERE role = 'project365_export_png'
                    """,
                    (json.dumps(crop, sort_keys=True),),
                )
                connection.commit()

            def fake_convert(
                source_path: Path,
                output_path: Path,
                output_format: str,
                long_edge: int,
                quality: int,
                crop: dict[str, object] | None,
            ) -> None:
                output_path.write_bytes(_tiny_png())

            with mock.patch.object(derivatives, "_convert_image", side_effect=fake_convert) as convert:
                second_summary = derivatives.generate_derivatives(
                    canonical_root=canonical_root,
                    output_format="jpeg",
                    long_edge=64,
                    quality=80,
                )

            self.assertEqual(first_summary.generated_count, 1)
            self.assertEqual(second_summary.generated_count, 1)
            self.assertEqual(second_summary.skipped_count, 0)
            self.assertEqual(convert.call_count, 1)

    def test_missing_preferred_external_original_is_reported_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {"1998-04-12.png": _tiny_png()},
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                connection.execute(
                    """
                    INSERT INTO media_assets (
                        id, entry_id, role, source_file_id, internal_filename, storage_path,
                        sha256, byte_size, mime_type, status, review_status, selected_default,
                        transformation_json, import_batch_id, created_at, updated_at
                    )
                    SELECT
                        'missing-reference', entry_id, 'external_original_reference', NULL,
                        'missing.jpg', ?, sha256, byte_size, 'image/jpeg', 'available',
                        'confirmed', 0, '{}', import_batch_id, created_at, updated_at
                    FROM media_assets
                    WHERE role = 'project365_export_png'
                    """,
                    (str(base / "missing.jpg"),),
                )
                connection.commit()

            readiness = derivatives.derivative_readiness_summary(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )
            self.assertEqual(readiness.ready_count, 0)
            self.assertEqual(readiness.not_ready_count, 1)

            summary = derivatives.generate_derivatives(
                canonical_root=canonical_root,
                output_format="jpeg",
                long_edge=64,
                quality=80,
            )

            self.assertEqual(summary.generated_count, 0)
            self.assertEqual(summary.not_ready_count, 1)
            self.assertEqual(summary.not_ready_dates, ("1998-04-12",))
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "not_ready_missing_source")
            self.assertFalse(Path(rows[0]["derivative_path"]).exists())

    def test_conversion_failure_is_reported_not_ready_and_does_not_abort_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            canonical_root = base / "Project365Canonical"
            import_dir.mkdir()
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.png": _tiny_png(),
                    "1998-04-13.png": _tiny_png(),
                },
            )
            canonical_importer.import_project365_exports(
                import_dir=import_dir,
                canonical_root=canonical_root,
            )
            _set_project365_export_crop(canonical_root)

            def fake_convert(
                source_path: Path,
                output_path: Path,
                output_format: str,
                long_edge: int,
                quality: int,
                crop: dict[str, object],
            ) -> None:
                if output_path.name == "Project365 Working Copy - 1998-04-12 - sq64.jpg":
                    raise subprocess.CalledProcessError(13, ["sips", str(source_path)])
                output_path.write_bytes(_tiny_png())

            with mock.patch.object(derivatives, "_convert_image_atomically", side_effect=fake_convert):
                summary = derivatives.generate_derivatives(
                    canonical_root=canonical_root,
                    output_format="jpeg",
                    long_edge=64,
                    quality=80,
                )

            self.assertEqual(summary.generated_count, 1)
            self.assertEqual(summary.not_ready_count, 1)
            with Path(summary.report_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            by_entry = {row["entry_id"]: row for row in rows}
            self.assertEqual(
                by_entry["project365:1998-04-12"]["status"],
                "not_ready_conversion_failed",
            )
            self.assertIn("returned exit status 13", by_entry["project365:1998-04-12"]["error"])
            self.assertEqual(by_entry["project365:1998-04-13"]["status"], "generated")

    def test_conversion_applies_square_crop_before_resizing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source.jpg"
            output = base / "output.jpg"
            source.write_bytes(b"placeholder")
            crop = {
                "x": 12,
                "y": 8,
                "size": 40,
                "candidate_width": 80,
                "candidate_height": 60,
                "source": "manual",
            }
            with mock.patch.object(derivatives.shutil, "which", return_value=None), mock.patch.object(
                derivatives.subprocess,
                "run",
            ) as run:
                derivatives._convert_image(source, output, "jpeg", 64, 80, crop)

            self.assertEqual(run.call_count, 2)
            crop_command = run.call_args_list[0].args[0]
            self.assertEqual(crop_command[:7], ["sips", "-c", "40", "40", "--cropOffset", "8", "12"])
            convert_command = run.call_args_list[1].args[0]
            self.assertIn("-Z", convert_command)
            self.assertIn("64", convert_command)

    def test_conversion_prefers_magick_for_square_crop_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source.jpg"
            output = base / "output.jpg"
            source.write_bytes(b"placeholder")
            crop = {
                "x": 12,
                "y": 8,
                "size": 40,
                "candidate_width": 80,
                "candidate_height": 60,
                "source": "manual",
            }
            with mock.patch.object(derivatives.shutil, "which", return_value="/usr/bin/magick"), mock.patch.object(
                derivatives.subprocess,
                "run",
            ) as run:
                derivatives._convert_image(source, output, "jpeg", 64, 80, crop)

            crop_command = run.call_args_list[0].args[0]
            self.assertEqual(crop_command[:3], ["/usr/bin/magick", "-size", "80x60"])
            self.assertIn("-auto-orient", crop_command)
            self.assertIn("40x40+12+8", crop_command)

    def test_conversion_uses_fill_color_for_crop_padding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source.jpg"
            output = base / "output.jpg"
            source.write_bytes(b"placeholder")
            crop = {
                "x": -5,
                "y": 8,
                "size": 90,
                "candidate_width": 80,
                "candidate_height": 60,
                "fill_color": "#fefefe",
            }
            with mock.patch.object(derivatives.shutil, "which", return_value="/usr/bin/magick"), mock.patch.object(
                derivatives.subprocess,
                "run",
            ) as run:
                derivatives._convert_image(source, output, "jpeg", 64, 80, crop)

            crop_command = run.call_args_list[0].args[0]
            self.assertEqual(crop_command[:3], ["/usr/bin/magick", "-size", "90x98"])
            self.assertIn("xc:#fefefe", crop_command)
            self.assertIn("90x90+0+8", crop_command)

    def test_conversion_uses_magick_for_rotated_crop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source.jpg"
            output = base / "output.jpg"
            source.write_bytes(b"placeholder")
            crop = {
                "x": 12,
                "y": 8,
                "size": 40,
                "candidate_width": 80,
                "candidate_height": 60,
                "rotation_degrees": 12.5,
            }
            with mock.patch.object(derivatives.shutil, "which", return_value="/usr/bin/magick"), mock.patch.object(
                derivatives.subprocess,
                "run",
            ) as run:
                derivatives._convert_image(source, output, "jpeg", 64, 80, crop)

            crop_command = run.call_args_list[0].args[0]
            self.assertEqual(crop_command[:2], ["/usr/bin/magick", str(source)])
            self.assertIn("-distort", crop_command)
            self.assertIn("SRT", crop_command)
            self.assertIn("12.5", crop_command)
            self.assertIn("40x40+12+8", crop_command)

    @unittest.skipUnless(shutil.which("magick"), "ImageMagick is required for pixel-health checks")
    def test_black_derivative_retry_detects_visible_source_with_black_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source_path = base / "source.jpg"
            black_output_path = base / "black.jpg"
            black_source_path = base / "black-source.jpg"
            magick = shutil.which("magick")
            assert magick is not None
            subprocess.run([magick, "-size", "20x20", "xc:red", str(source_path)], check=True)
            subprocess.run([magick, "-size", "20x20", "xc:black", str(black_output_path)], check=True)
            subprocess.run([magick, "-size", "20x20", "xc:black", str(black_source_path)], check=True)

            self.assertTrue(derivatives._should_retry_black_derivative(source_path, black_output_path))
            self.assertFalse(derivatives._should_retry_black_derivative(black_source_path, black_output_path))


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def _set_project365_export_crop(canonical_root: Path) -> None:
    crop = {
        "review_crop": {
            "x": 0,
            "y": 0,
            "size": 1,
            "candidate_width": 1,
            "candidate_height": 1,
            "source": "manual",
        }
    }
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        connection.execute(
            """
            UPDATE media_assets
            SET transformation_json = ?
            WHERE role = 'project365_export_png'
            """,
            (json.dumps(crop, sort_keys=True),),
        )
        connection.commit()


def _insert_external_original_without_crop(canonical_root: Path, source_path: Path) -> None:
    transformation = {
        "source": "external_original_reference",
        "source_path": str(source_path),
        "original_is_read_only": True,
    }
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        import_batch_id = connection.execute(
            """
            SELECT import_batch_id
            FROM media_assets
            WHERE entry_id = 'project365:1998-04-12'
                AND role = 'project365_export_png'
            """
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO media_assets (
                id, entry_id, role, source_file_id, internal_filename, storage_path,
                sha256, byte_size, mime_type, status, review_status, selected_default,
                transformation_json, import_batch_id, created_at, updated_at
            )
            VALUES (
                'external-source', 'project365:1998-04-12',
                'external_original_reference', NULL, 'external-original.png', ?,
                ?, ?, 'image/png', 'available', 'confirmed', 0, ?, ?,
                '2026-08-17T00:00:00Z', '2026-08-17T00:00:00Z'
            )
            """,
            (
                str(source_path),
                hashlib.sha256(source_path.read_bytes()).hexdigest(),
                source_path.stat().st_size,
                json.dumps(transformation, sort_keys=True),
                import_batch_id,
            ),
        )
        connection.commit()


def _write_staged_crop(
    canonical_root: Path,
    entry_id: str,
    candidate_path: Path,
    crop: dict[str, object],
) -> None:
    staging_path = (
        canonical_root
        / "exports"
        / "verification_reports"
        / "original_photo_external_search_queue_crop_staging.json"
    )
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    entry_id: {
                        str(candidate_path): {
                            "crop": crop,
                            "commit_pending": False,
                        }
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _insert_associated_original(canonical_root: Path, source_path: Path) -> None:
    crop = {
        "source": "external_original_associated_photo",
        "source_path": str(source_path),
        "associated_entry_date": "1998-04-13",
        "associated_date_source": "manual",
        "review_crop": {
            "x": 0,
            "y": 0,
            "size": 1,
            "candidate_width": 1,
            "candidate_height": 1,
            "source": "manual",
        },
        "original_is_read_only": True,
    }
    with sqlite3.connect(canonical_root / "canonical.db") as connection:
        import_batch_id = connection.execute(
            """
            SELECT import_batch_id
            FROM media_assets
            WHERE entry_id = 'project365:1998-04-12'
                AND role = 'project365_export_png'
            """
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO media_assets (
                id, entry_id, role, source_file_id, internal_filename, storage_path,
                sha256, byte_size, mime_type, status, review_status, selected_default,
                transformation_json, import_batch_id, created_at, updated_at
            )
            VALUES (
                'associated-source', 'project365:1998-04-12',
                'external_original_associated_photo', NULL, 'associated.jpg', ?,
                ?, ?, 'image/jpeg', 'available', 'confirmed', 0, ?, ?,
                '2026-08-17T00:00:00Z', '2026-08-17T00:00:00Z'
            )
            """,
            (
                str(source_path),
                hashlib.sha256(source_path.read_bytes()).hexdigest(),
                source_path.stat().st_size,
                json.dumps(crop, sort_keys=True),
                import_batch_id,
            ),
        )
        connection.commit()


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
        b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f"
        b"\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


if __name__ == "__main__":
    unittest.main()
