from __future__ import annotations

import tempfile
import unittest
import zipfile
import csv
from pathlib import Path

import project365_export_validator as validator


class Project365ExportValidatorTests(unittest.TestCase):
    def test_valid_month_zip_records_entry_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir)
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.txt": b"private diary sample",
                    "1998-04-12.png": b"png bytes",
                    "1998-04-13.png": b"more png bytes",
                },
            )

            report = validator.validate_exports(import_dir)

            self.assertTrue(report.ok)
            self.assertEqual(report.months_found, ["1998-04"])
            self.assertEqual(len(report.month_exports), 1)
            export = report.month_exports[0]
            self.assertTrue(export.readable)
            self.assertEqual(len(export.entries), 2)
            self.assertTrue(export.entries[0].has_txt)
            self.assertTrue(export.entries[0].has_png)
            self.assertFalse(str(report).find("private diary sample") >= 0)

    def test_nested_year_folders_are_scanned_for_month_zips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir)
            nested_dir = import_dir / "1998"
            nested_dir.mkdir()
            _write_zip(nested_dir / "1998-04.zip", {"1998-04-12.png": b"image"})

            report = validator.validate_exports(import_dir)

            self.assertTrue(report.ok)
            self.assertEqual(report.zip_count, 1)
            self.assertEqual(report.months_found, ["1998-04"])
            self.assertEqual(Path(report.month_exports[0].zip_file).parent.name, "1998")

    def test_expected_range_reports_missing_months(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir)
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": b"image"})
            _write_zip(import_dir / "1998-06.zip", {"1998-06-01.png": b"image"})

            report = validator.validate_exports(
                import_dir,
                start_month="1998-04",
                end_month="1998-06",
            )

            self.assertFalse(report.ok)
            self.assertEqual(report.missing_months, ["1998-05"])
            self.assertIn("missing_month", {issue.code for issue in report.issues})

    def test_sparse_sample_folder_does_not_infer_missing_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir)
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": b"image"})
            _write_zip(import_dir / "2004-01.zip", {"2004-01-01.png": b"image"})

            report = validator.validate_exports(import_dir)

            self.assertTrue(report.ok)
            self.assertEqual(report.expected_start_month, None)
            self.assertEqual(report.expected_end_month, None)
            self.assertEqual(report.missing_months, [])

    def test_invalid_internal_names_and_month_mismatches_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir)
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-31.png": b"invalid date",
                    "1998-05-01.png": b"wrong month",
                    "nested/1998-04-12.txt": b"not root",
                    "1998-04-12.jpg": b"wrong extension",
                },
            )

            report = validator.validate_exports(import_dir)

            self.assertFalse(report.ok)
            self.assertEqual(
                {
                    "invalid_internal_date",
                    "internal_month_mismatch",
                    "invalid_internal_filename",
                },
                {issue.code for issue in report.issues},
            )

    def test_unreadable_zip_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir)
            (import_dir / "1998-04.zip").write_bytes(b"not a zip")

            report = validator.validate_exports(import_dir)

            self.assertFalse(report.ok)
            self.assertIn("unreadable_zip", {issue.code for issue in report.issues})

    def test_duplicate_month_detects_non_normalized_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import_dir = Path(temp_dir)
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.png": b"image"})
            _write_zip(
                import_dir / "1998-04 copy.zip",
                {"1998-04-13.png": b"image"},
            )

            report = validator.validate_exports(import_dir)

            self.assertFalse(report.ok)
            self.assertEqual(report.duplicate_months, ["1998-04"])
            self.assertIn("invalid_zip_filename", {issue.code for issue in report.issues})
            self.assertIn("duplicate_month", {issue.code for issue in report.issues})

    def test_report_files_do_not_include_text_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            report_dir = base / "Reports"
            import_dir.mkdir()
            report_dir.mkdir()
            secret = "do not print this diary sentence"
            _write_zip(import_dir / "1998-04.zip", {"1998-04-12.txt": secret.encode()})

            report = validator.validate_exports(import_dir)
            json_path = report_dir / "report.json"
            md_path = report_dir / "report.md"
            validator.write_json_report(report, json_path)
            validator.write_markdown_report(report, md_path)

            self.assertNotIn(secret, json_path.read_text())
            self.assertNotIn(secret, md_path.read_text())

    def test_source_manifest_contains_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir = base / "Import"
            report_dir = base / "Reports"
            import_dir.mkdir()
            secret = "private text must not leak"
            _write_zip(
                import_dir / "1998-04.zip",
                {
                    "1998-04-12.txt": secret.encode(),
                    "1998-04-12.png": b"image",
                },
            )

            report = validator.validate_exports(import_dir)
            manifest_path = report_dir / "source_files.csv"
            validator.write_source_manifest(report, manifest_path)

            manifest_text = manifest_path.read_text()
            self.assertNotIn(secret, manifest_text)
            with manifest_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["zip_filename"], "1998-04.zip")
            self.assertEqual(rows[0]["month"], "1998-04")
            self.assertEqual(rows[0]["validation_status"], "pass")
            self.assertEqual(rows[0]["entry_count"], "1")
            self.assertEqual(rows[0]["png_day_count"], "1")
            self.assertEqual(rows[0]["txt_day_count"], "1")


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


if __name__ == "__main__":
    unittest.main()
