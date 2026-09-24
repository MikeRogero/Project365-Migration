from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as canonical_importer
import project365_digikam_people_importer as digikam
import project365_tag_enrichment as tag_enrichment


class Project365DigiKamPeopleImporterTests(unittest.TestCase):
    def test_imports_csv_suggestions_as_suggested_people(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, ["1998-04-12"])
            suggestions = base / "people.csv"
            suggestions.write_text(
                "\n".join(
                    [
                        "media_path,person_name",
                        f"{canonical_root}/media/diarium_derivatives/Project365_square_2560_q88/1998-04/project365_1998-04-12.jpg, Alex Example ",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = digikam.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[],
                suggestions_csv=suggestions,
                report_path=canonical_root / "exports" / "verification_reports" / "digikam_report.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )

            self.assertEqual(summary.applied_count, 1)
            self.assertEqual(summary.error_count, 0)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                people = connection.execute(
                    "SELECT canonical_name, diarium_tag, review_status, source FROM people"
                ).fetchall()
            self.assertEqual(people, [("Alex Example", "person:Alex Example", "suggested", "digikam_csv")])
            self.assertTrue(Path(summary.tag_queue_path).exists())

    def test_import_reports_unknown_working_copy_without_applying(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, ["1998-04-12"])
            suggestions = base / "people.csv"
            suggestions.write_text(
                "media_path,person_name\n/working/1998-04/project365_1998-04-13.jpg,Alex Example\n",
                encoding="utf-8",
            )

            summary = digikam.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[],
                suggestions_csv=suggestions,
                report_path=canonical_root / "exports" / "verification_reports" / "digikam_report.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )

            self.assertEqual(summary.applied_count, 0)
            self.assertEqual(summary.error_count, 1)
            rows = _read_csv(Path(summary.report_path))
            self.assertEqual(rows[0]["reason"], "unknown_working_copy")

    def test_import_is_idempotent_and_reports_duplicate_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, ["1998-04-12"])
            media_path = f"{canonical_root}/media/diarium_derivatives/Project365_square_2560_q88/1998-04/project365_1998-04-12.jpg"
            suggestions = base / "people.csv"
            suggestions.write_text(
                "\n".join(
                    [
                        "media_path,person_name",
                        f"{media_path},Alex Example",
                        f"{media_path},Alex Example",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            first = digikam.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[],
                suggestions_csv=suggestions,
                report_path=canonical_root / "exports" / "verification_reports" / "digikam_report.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )
            second = digikam.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[],
                suggestions_csv=suggestions,
                report_path=canonical_root / "exports" / "verification_reports" / "digikam_report.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )

            self.assertEqual(first.applied_count, 1)
            self.assertEqual(first.skipped_count, 1)
            self.assertEqual(second.applied_count, 0)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                count = connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            self.assertEqual(count, 1)

    def test_import_preserves_confirmed_people(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, ["1998-04-12"])
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                tag_enrichment.upsert_person(connection, "project365:1998-04-12", "Alex Example", "confirmed", "manual")
                connection.commit()
            suggestions = base / "people.csv"
            suggestions.write_text(
                f"media_path,person_name\n/working/1998-04/project365_1998-04-12.jpg,Alex Example\n",
                encoding="utf-8",
            )

            summary = digikam.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[],
                suggestions_csv=suggestions,
                report_path=canonical_root / "exports" / "verification_reports" / "digikam_report.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )

            self.assertEqual(summary.applied_count, 0)
            rows = _read_csv(Path(summary.report_path))
            self.assertEqual(rows[0]["reason"], "preserved_reviewed_person")
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                people = connection.execute(
                    "SELECT canonical_name, review_status, source FROM people"
                ).fetchall()
            self.assertEqual(people, [("Alex Example", "confirmed", "manual")])

    def test_imports_people_from_xmp_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, ["1998-04-12"])
            xmp_root = base / "xmp" / "1998-04"
            xmp_root.mkdir(parents=True)
            sidecar = xmp_root / "project365_1998-04-12.jpg.xmp"
            sidecar.write_text(
                """<?xml version="1.0"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/">
  <mwg-rs:Regions>
    <mwg-rs:RegionList>
      <rdf:Seq xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
        <rdf:li mwg-rs:Name="Alex Example" />
      </rdf:Seq>
    </mwg-rs:RegionList>
  </mwg-rs:Regions>
</x:xmpmeta>
""",
                encoding="utf-8",
            )

            summary = digikam.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[base / "xmp"],
                suggestions_csv=None,
                report_path=canonical_root / "exports" / "verification_reports" / "digikam_report.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )

            self.assertEqual(summary.applied_count, 1)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                people = connection.execute(
                    "SELECT canonical_name, review_status, source FROM people"
                ).fetchall()
            self.assertEqual(people, [("Alex Example", "suggested", "digikam_xmp")])

    def test_sidecar_without_current_working_copy_is_not_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = _import_sample(base, ["1998-04-12"])
            xmp_root = canonical_root / "media" / "diarium_derivatives" / "Project365_square_2560_q88" / "1998-04"
            xmp_root.mkdir(parents=True)
            sidecar = xmp_root / "Project365 Working Copy - 1998-04-12 - sq2560.jpg.xmp"
            sidecar.write_text(
                """<?xml version="1.0"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:subject>
    <rdf:Bag xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
      <rdf:li>People|Alex Example</rdf:li>
    </rdf:Bag>
  </dc:subject>
</x:xmpmeta>
""",
                encoding="utf-8",
            )

            summary = digikam.import_digikam_people(
                canonical_root=canonical_root,
                xmp_roots=[canonical_root / "media" / "diarium_derivatives" / "Project365_square_2560_q88"],
                suggestions_csv=None,
                report_path=canonical_root / "exports" / "verification_reports" / "digikam_report.csv",
                queue_path=canonical_root / "exports" / "verification_reports" / "tag_queue.csv",
            )

            self.assertEqual(summary.applied_count, 0)
            self.assertEqual(summary.error_count, 0)
            self.assertEqual(summary.unmatched_sidecar_count, 1)
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                people = connection.execute(
                    "SELECT canonical_name, diarium_tag, review_status, source FROM people"
                ).fetchall()
            self.assertEqual(people, [])

    def test_current_working_copy_names_map_to_entry(self) -> None:
        self.assertEqual(
            digikam._entry_id_from_media_path("/working/1998-04/Project365 Working Copy - 1998-04-12 - sq2560.jpg"),
            "project365:1998-04-12",
        )
        self.assertEqual(
            digikam._entry_id_from_media_path("/working/1998-04/Project365 Working Copy - 1998-04-12 - associated sq2560 - 123456789abc.jpg"),
            "project365:1998-04-12",
        )
        self.assertIsNone(digikam._entry_id_from_media_path("/working/1998-04/Project365 Working Copy - invalid - sq2560.jpg"))


def _import_sample(base: Path, dates: list[str]) -> Path:
    import_dir = base / "Import"
    canonical_root = base / "Project365Canonical"
    import_dir.mkdir()
    with zipfile.ZipFile(import_dir / "1998-04.zip", "w") as archive:
        for date in dates:
            archive.writestr(f"{date}.png", _tiny_png())
    canonical_importer.import_project365_exports(import_dir=import_dir, canonical_root=canonical_root)
    return canonical_root


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
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


if __name__ == "__main__":
    unittest.main()
