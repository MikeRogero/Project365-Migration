from __future__ import annotations

import csv
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import project365_canonical_importer as canonical_importer
import project365_original_matcher as matcher


class Project365OriginalMatcherTests(unittest.TestCase):
    def test_exact_match_auto_selects_best_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            import_dir, canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            candidates = base / "candidates"
            candidates.mkdir()
            candidates.joinpath("1998-04-12-original.png").write_bytes(_tiny_png())

            summary = matcher.build_original_match_index(
                canonical_root=canonical_root,
                candidate_roots=[candidates],
                report_dir=canonical_root / "exports" / "verification_reports",
                auto_accept=True,
            )

            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(summary.proposed_match_count, 1)
            self.assertEqual(summary.auto_selected_count, 1)
            rows = _read_csv(Path(summary.review_queue_path))
            self.assertEqual(rows[0]["decision"], "auto_accept")
            self.assertIn("same_sha256", rows[0]["evidence"])
            with sqlite3.connect(canonical_root / "canonical.db") as connection:
                media_rows = connection.execute(
                    "SELECT role, review_status FROM media_assets WHERE role = 'best_original'"
                ).fetchall()
            self.assertEqual(media_rows, [("best_original", "confirmed")])

    def test_no_match_preserves_fallback_review_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            _, canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            candidates = base / "candidates"
            candidates.mkdir()

            summary = matcher.build_original_match_index(
                canonical_root=canonical_root,
                candidate_roots=[candidates],
                report_dir=canonical_root / "exports" / "verification_reports",
            )

            rows = _read_csv(Path(summary.review_queue_path))
            self.assertEqual(rows[0]["match_status"], "no_candidate")
            self.assertEqual(rows[0]["decision"], "fallback")

    def test_duplicate_exact_candidates_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            _, canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            candidates = base / "candidates"
            candidates.mkdir()
            candidates.joinpath("1998-04-12-a.png").write_bytes(_tiny_png())
            candidates.joinpath("1998-04-12-b.png").write_bytes(_tiny_png())

            summary = matcher.build_original_match_index(
                canonical_root=canonical_root,
                candidate_roots=[candidates],
                report_dir=canonical_root / "exports" / "verification_reports",
                auto_accept=True,
            )

            self.assertEqual(summary.auto_selected_count, 0)
            rows = _read_csv(Path(summary.review_queue_path))
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["match_status"] for row in rows}, {"ambiguous"})
            self.assertEqual({row["decision"] for row in rows}, {"review_ambiguous"})

    def test_date_candidate_without_exact_hash_goes_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            _, canonical_root = _import_sample(base, {"1998-04-12.png": _tiny_png()})
            candidates = base / "candidates"
            candidates.mkdir()
            candidates.joinpath("1998-04-12-other.png").write_bytes(_other_tiny_png())

            summary = matcher.build_original_match_index(
                canonical_root=canonical_root,
                candidate_roots=[candidates],
                report_dir=canonical_root / "exports" / "verification_reports",
                auto_accept=True,
            )

            self.assertEqual(summary.auto_selected_count, 0)
            rows = _read_csv(Path(summary.review_queue_path))
            self.assertEqual(rows[0]["match_status"], "candidate")
            self.assertIn(rows[0]["decision"], {"review", "review_ambiguous"})
            self.assertIn("date_hint", rows[0]["evidence"])

    def test_broad_visual_scan_shortlists_camera_filename_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            candidate_pixels = _make_gradient_pixels(80, 40)
            reference_pixels = _crop(candidate_pixels, 80, 20, 0, 40, 40)
            reference_bytes = _png_bytes_from_pixels(base, "reference", 40, 40, reference_pixels)
            _, canonical_root = _import_sample(base, {"1998-04-12.png": reference_bytes})
            candidates = base / "candidates"
            candidates.mkdir()
            candidates.joinpath("IMG_1234.png").write_bytes(
                _png_bytes_from_pixels(base, "candidate", 80, 40, candidate_pixels)
            )

            summary = matcher.build_original_match_index(
                canonical_root=canonical_root,
                candidate_roots=[candidates],
                report_dir=canonical_root / "exports" / "verification_reports",
                broad_visual_scan=True,
                visual_prefilter_threshold=5000.0,
            )

            self.assertEqual(summary.auto_selected_count, 0)
            rows = _read_csv(Path(summary.review_queue_path))
            self.assertEqual(rows[0]["match_status"], "candidate")
            self.assertEqual(rows[0]["decision"], "review")
            self.assertIn("visual_scan", rows[0]["evidence"])
            self.assertIn("crop_high", rows[0]["evidence"])


def _import_sample(base: Path, members: dict[str, bytes]) -> tuple[Path, Path]:
    import_dir = base / "Import"
    canonical_root = base / "Project365Canonical"
    import_dir.mkdir()
    with zipfile.ZipFile(import_dir / "1998-04.zip", "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    canonical_importer.import_project365_exports(
        import_dir=import_dir,
        canonical_root=canonical_root,
    )
    return import_dir, canonical_root


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
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


def _other_tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
        b"\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _make_gradient_pixels(width: int, height: int) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            red = (x * 3 + y * 5) % 256
            green = (x * 7 + y * 11) % 256
            blue = (x * 13 + y * 17) % 256
            pixels.append((red, green, blue))
    return pixels


def _crop(
    pixels: list[tuple[int, int, int]],
    source_width: int,
    x: int,
    y: int,
    width: int,
    height: int,
) -> list[tuple[int, int, int]]:
    cropped = []
    for row in range(y, y + height):
        offset = row * source_width
        cropped.extend(pixels[offset + x : offset + x + width])
    return cropped


def _bmp_bytes(width: int, height: int, pixels: list[tuple[int, int, int]]) -> bytes:
    row_stride = ((width * 3 + 3) // 4) * 4
    pixel_bytes = bytearray()
    for row in range(height - 1, -1, -1):
        row_bytes = bytearray()
        for col in range(width):
            red, green, blue = pixels[row * width + col]
            row_bytes.extend([blue, green, red])
        row_bytes.extend(b"\x00" * (row_stride - width * 3))
        pixel_bytes.extend(row_bytes)
    file_size = 14 + 40 + len(pixel_bytes)
    header = bytearray()
    header.extend(b"BM")
    header.extend(file_size.to_bytes(4, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend((54).to_bytes(4, "little"))
    header.extend((40).to_bytes(4, "little"))
    header.extend(width.to_bytes(4, "little", signed=True))
    header.extend(height.to_bytes(4, "little", signed=True))
    header.extend((1).to_bytes(2, "little"))
    header.extend((24).to_bytes(2, "little"))
    header.extend((0).to_bytes(4, "little"))
    header.extend(len(pixel_bytes).to_bytes(4, "little"))
    header.extend((2835).to_bytes(4, "little", signed=True))
    header.extend((2835).to_bytes(4, "little", signed=True))
    header.extend((0).to_bytes(4, "little"))
    header.extend((0).to_bytes(4, "little"))
    return bytes(header) + bytes(pixel_bytes)


def _png_bytes_from_pixels(
    base: Path,
    stem: str,
    width: int,
    height: int,
    pixels: list[tuple[int, int, int]],
) -> bytes:
    bmp_path = base / f"{stem}.bmp"
    png_path = base / f"{stem}.png"
    bmp_path.write_bytes(_bmp_bytes(width, height, pixels))
    subprocess.run(
        ["sips", "-s", "format", "png", str(bmp_path), "--out", str(png_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return png_path.read_bytes()


if __name__ == "__main__":
    unittest.main()
