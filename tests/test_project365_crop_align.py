from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_crop_align as crop_align


class Project365CropAlignTests(unittest.TestCase):
    def test_suggests_square_crop_from_wide_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            candidate = _make_gradient_pixels(80, 40)
            reference = _crop(candidate, 80, 20, 0, 40, 40)
            candidate_path = base / "candidate.bmp"
            reference_path = base / "reference.bmp"
            _write_bmp(candidate_path, 80, 40, candidate)
            _write_bmp(reference_path, 40, 40, reference)

            suggestion = crop_align.suggest_crop(reference_path, candidate_path)

            self.assertLessEqual(abs(suggestion.x - 20), 4)
            self.assertEqual(suggestion.y, 0)
            self.assertEqual(suggestion.width, 40)
            self.assertEqual(suggestion.height, 40)
            self.assertEqual(suggestion.confidence, "high")

    def test_suggests_crop_against_rotated_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            candidate = _make_grayscale_gradient_pixels(80, 40)
            candidate_path = base / "candidate.bmp"
            reference_path = base / "reference.bmp"
            _write_bmp(candidate_path, 80, 40, candidate)
            candidate_image = crop_align.ImagePixels(
                width=80,
                height=40,
                gray=[red for red, _green, _blue in candidate],
            )
            reference_gray = crop_align._sample_crop(  # noqa: SLF001 - verifies rotated crop sampling contract.
                candidate_image,
                20,
                0,
                40,
                40,
                40,
                rotation_degrees=90,
            )
            _write_bmp(reference_path, 40, 40, [(value, value, value) for value in reference_gray])

            suggestion = crop_align.suggest_crop(
                reference_path,
                candidate_path,
                candidate_rotation_degrees=90,
            )

            self.assertLessEqual(abs(suggestion.x - 20), 4)
            self.assertEqual(suggestion.y, 0)
            self.assertEqual(suggestion.width, 40)
            self.assertEqual(suggestion.height, 40)

    def test_refines_low_confidence_largest_crop_between_scan_positions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            candidate = _make_smooth_gradient_pixels(430, 300)
            reference = _brighten(_crop(candidate, 430, 67, 0, 300, 300), 20)
            candidate_path = base / "candidate.bmp"
            reference_path = base / "reference.bmp"
            _write_bmp(candidate_path, 430, 300, candidate)
            _write_bmp(reference_path, 300, 300, reference)

            suggestion = crop_align.suggest_crop(reference_path, candidate_path)

            self.assertEqual(suggestion.x, 67)
            self.assertEqual(suggestion.y, 0)
            self.assertEqual(suggestion.width, 300)
            self.assertEqual(suggestion.height, 300)
            self.assertEqual(suggestion.confidence, "low")

    def test_does_not_refine_high_confidence_largest_crop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            candidate = _make_gradient_pixels(80, 40)
            reference = _crop(candidate, 80, 20, 0, 40, 40)
            candidate_path = base / "candidate.bmp"
            reference_path = base / "reference.bmp"
            _write_bmp(candidate_path, 80, 40, candidate)
            _write_bmp(reference_path, 40, 40, reference)

            with mock.patch.object(crop_align, "_refine_crop") as refine_crop:
                suggestion = crop_align.suggest_crop(reference_path, candidate_path)

            self.assertLessEqual(abs(suggestion.x - 20), 4)
            refine_crop.assert_not_called()

    def test_loads_bmp_dimensions_and_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.bmp"
            pixels = [(255, 0, 0), (0, 255, 0)]
            _write_bmp(path, 2, 1, pixels)

            image = crop_align.load_image(path)

            self.assertEqual(image.width, 2)
            self.assertEqual(image.height, 1)
            self.assertNotEqual(image.gray[0], image.gray[1])

    def test_load_image_falls_back_when_sips_writes_non_bmp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sample.heic"
            source_path.write_bytes(b"not-real-heic")

            def fake_run(command: list[str], **_kwargs: object) -> mock.Mock:
                output = Path(command[-1].removeprefix("BMP3:"))
                if command[0] == "sips":
                    output.write_bytes(b"not a bmp")
                else:
                    _write_bmp(output, 1, 1, [(255, 0, 0)])
                return mock.Mock(returncode=0)

            def fake_which(name: str) -> str | None:
                if name == "magick":
                    return "/usr/local/bin/magick"
                return None

            with mock.patch.object(crop_align.shutil, "which", side_effect=fake_which):
                with mock.patch.object(crop_align.subprocess, "run", side_effect=fake_run) as run:
                    image = crop_align.load_image(source_path)

            self.assertEqual(image.width, 1)
            self.assertEqual(image.height, 1)
            self.assertEqual(run.call_count, 2)

    def test_load_image_falls_back_when_sips_writes_black_bmp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sample.heic"
            source_path.write_bytes(b"not-real-heic")

            def fake_run(command: list[str], **_kwargs: object) -> mock.Mock:
                if command[0] == "sips" and Path(command[4]).suffix == ".heic":
                    _write_bmp(Path(command[-1]), 2, 2, [(0, 0, 0)] * 4)
                    return mock.Mock(returncode=0)
                if command[0] == "/usr/bin/qlmanage":
                    preview_dir = Path(command[5])
                    _write_bmp(preview_dir / f"{source_path.name}.png", 2, 2, [(0, 0, 0)] * 4)
                    return mock.Mock(returncode=0)
                if command[0] == "sips":
                    _write_bmp(Path(command[-1]), 2, 2, [(0, 0, 0), (80, 80, 80), (160, 160, 160), (255, 255, 255)])
                    return mock.Mock(returncode=0)
                raise AssertionError(command)

            def fake_which(name: str) -> str | None:
                if name == "qlmanage":
                    return "/usr/bin/qlmanage"
                return None

            with mock.patch.object(crop_align.shutil, "which", side_effect=fake_which):
                with mock.patch.object(crop_align.subprocess, "run", side_effect=fake_run) as run:
                    image = crop_align.load_image(source_path)

            self.assertEqual(image.width, 2)
            self.assertEqual(image.height, 2)
            self.assertGreater(max(image.gray), 0)
            self.assertEqual(run.call_count, 3)


def _make_gradient_pixels(width: int, height: int) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            red = (x * 3 + y * 5) % 256
            green = (x * 7 + y * 11) % 256
            blue = (x * 13 + y * 17) % 256
            pixels.append((red, green, blue))
    return pixels


def _make_grayscale_gradient_pixels(width: int, height: int) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            value = (x * 3 + y * 17) % 256
            pixels.append((value, value, value))
    return pixels


def _make_smooth_gradient_pixels(width: int, height: int) -> list[tuple[int, int, int]]:
    pixels = []
    for y in range(height):
        for x in range(width):
            red = (x * 2 + y) % 256
            green = (x + y * 3) % 256
            blue = (x * 5 + y * 7) % 256
            pixels.append((red, green, blue))
    return pixels


def _brighten(pixels: list[tuple[int, int, int]], amount: int) -> list[tuple[int, int, int]]:
    return [
        (
            max(0, min(255, red + amount)),
            max(0, min(255, green + amount)),
            max(0, min(255, blue + amount)),
        )
        for red, green, blue in pixels
    ]


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
