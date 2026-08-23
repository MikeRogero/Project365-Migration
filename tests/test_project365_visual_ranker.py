from __future__ import annotations

import csv
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_original_picker as picker
import project365_visual_ranker as ranker


class Project365VisualRankerTests(unittest.TestCase):
    def test_rank_candidate_rows_prefers_matching_square_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            cache = ranker.VisualDescriptorCache(base / "cache.sqlite")
            candidate_pixels = _make_gradient_pixels(80, 40)
            reference_pixels = _crop(candidate_pixels, 80, 20, 0, 40, 40)
            wrong_pixels = _make_solid_pixels(40, 40, (230, 40, 40))
            reference_path = base / "reference.bmp"
            candidate_path = base / "candidate.bmp"
            wrong_path = base / "wrong.bmp"
            _write_bmp(reference_path, 40, 40, reference_pixels)
            _write_bmp(candidate_path, 80, 40, candidate_pixels)
            _write_bmp(wrong_path, 40, 40, wrong_pixels)

            results = ranker.rank_candidate_rows(
                cache,
                reference_path,
                [
                    {"candidate_path": str(wrong_path)},
                    {"candidate_path": str(candidate_path)},
                ],
                likely_limit=1,
            )
            cache.close()

            self.assertEqual(results[str(candidate_path)]["visual_rank"], "1")
            self.assertEqual(results[str(candidate_path)]["visual_likely"], "true")
            self.assertIn("square", results[str(candidate_path)]["visual_best_view"])
            self.assertEqual(results[str(wrong_path)]["visual_likely"], "false")

    def test_rank_queue_writes_visual_columns_for_oversized_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            source_path = base / "source.bmp"
            source_pixels = _make_gradient_pixels(40, 40)
            _write_bmp(source_path, 40, 40, source_pixels)
            _write_minimal_canonical_db(canonical_root / "canonical.db", source_path)
            candidate_paths = []
            for index in range(21):
                path = base / f"candidate-{index:02d}.bmp"
                pixels = source_pixels if index == 7 else _make_solid_pixels(40, 40, (index * 7 % 255, 30, 180))
                _write_bmp(path, 40, 40, pixels)
                candidate_paths.append(path)
            queue_path = base / "queue.csv"
            _write_queue(queue_path, candidate_paths)

            summary = ranker.rank_queue(canonical_root, queue_path, threshold=20, likely_limit=20)

            self.assertEqual(summary.entry_count, 1)
            rows = _read_csv(queue_path)
            best = min(rows, key=lambda row: int(row["visual_rank"] or "9999"))
            self.assertEqual(best["candidate_path"], str(candidate_paths[7]))
            self.assertEqual(best["visual_rank"], "1")
            self.assertEqual(best["visual_method"], ranker.METHOD_VERSION)

    def test_picker_entry_detail_triggers_cached_visual_ranking_for_large_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            canonical_root = base / "Project365Canonical"
            canonical_root.mkdir()
            source_path = base / "source.bmp"
            source_pixels = _make_gradient_pixels(40, 40)
            _write_bmp(source_path, 40, 40, source_pixels)
            _write_minimal_canonical_db(canonical_root / "canonical.db", source_path)
            candidate_paths = []
            for index in range(21):
                path = base / f"candidate-{index:02d}.bmp"
                pixels = source_pixels if index == 3 else _make_solid_pixels(40, 40, (20, index * 9 % 255, 150))
                _write_bmp(path, 40, 40, pixels)
                candidate_paths.append(path)
            queue_path = base / "queue.csv"
            _write_queue(queue_path, candidate_paths)
            state = picker.PickerState(picker.PickerConfig(canonical_root=canonical_root, queue_path=queue_path))

            entry = state.entry_detail("project365:1998-04-12")

            self.assertIsNotNone(entry)
            best = min(entry["candidates"], key=lambda candidate: int(candidate["visual_rank"] or "9999"))
            self.assertEqual(best["path"], str(candidate_paths[3]))
            rows = _read_csv(queue_path)
            self.assertIn("visual_rank", rows[0])
            self.assertTrue(any(row["visual_likely"] == "true" for row in rows))

    def test_load_visual_image_uses_vipsthumbnail_for_thumbnail_ppm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            input_path = base / "input.jpg"
            input_path.write_bytes(b"placeholder")
            output_ppm = (
                b"P6\n"
                b"# generated by fake vipsthumbnail\n"
                b"2 1\n"
                b"255\n"
                b"\x20\x00\x10\x00\xff\x80"
            )
            fake_tool = base / "vipsthumbnail"
            fake_tool.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import pathlib, sys",
                        "out = pathlib.Path(sys.argv[sys.argv.index('--path') + 1])",
                        f"out.write_bytes({output_ppm!r})",
                    ]
                ),
                encoding="utf-8",
            )
            fake_tool.chmod(fake_tool.stat().st_mode | stat.S_IXUSR)

            with mock.patch.dict(os.environ, {"PATH": f"{base}{os.pathsep}{os.environ.get('PATH', '')}"}):
                image = ranker.load_visual_image(input_path, max_dimension=1024, thumbnail_tool="vipsthumbnail")

        self.assertEqual(image.width, 2)
        self.assertEqual(image.height, 1)
        self.assertEqual(image.rgb, [(32, 0, 16), (0, 255, 128)])


def _write_minimal_canonical_db(db_path: Path, source_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE media_assets (
                id TEXT PRIMARY KEY,
                entry_id TEXT NOT NULL,
                role TEXT NOT NULL,
                storage_path TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO media_assets (id, entry_id, role, storage_path)
            VALUES ('project365:1998-04-12:project365_export_png', 'project365:1998-04-12', 'project365_export_png', ?)
            """,
            (str(source_path),),
        )
        connection.commit()


def _write_queue(path: Path, candidate_paths: list[Path]) -> None:
    fieldnames = [
        "entry_id",
        "entry_date",
        "project365_media_asset_id",
        "current_match_status",
        "current_decision",
        "candidate_path",
        "candidate_filename",
        "candidate_sha256",
        "byte_size",
        "mime_type",
        "filename_dates",
        "media_creation_dates",
        "filesystem_dates",
        "capture_timestamp",
        "capture_timestamp_source",
        "date_distance",
        "evidence",
        "candidate_filter_reason",
        "review_decision",
        "review_notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candidate_path in candidate_paths:
            writer.writerow(
                {
                    "entry_id": "project365:1998-04-12",
                    "entry_date": "1998-04-12",
                    "project365_media_asset_id": "project365:1998-04-12:project365_export_png",
                    "candidate_path": str(candidate_path),
                    "candidate_filename": candidate_path.name,
                    "byte_size": str(candidate_path.stat().st_size),
                    "mime_type": "image/bmp",
                    "date_distance": "0",
                    "evidence": "test",
                }
            )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _make_gradient_pixels(width: int, height: int) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            red = (x * 3 + y * 5) % 256
            green = (x * 7 + y * 11) % 256
            blue = (x * 13 + y * 17) % 256
            pixels.append((red, green, blue))
    return pixels


def _make_solid_pixels(width: int, height: int, color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    return [color] * (width * height)


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


def _write_bmp(
    path: Path,
    width: int,
    height: int,
    pixels: list[tuple[int, int, int]],
) -> None:
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
    path.write_bytes(bytes(header) + bytes(pixel_bytes))


if __name__ == "__main__":
    unittest.main()
