#!/usr/bin/env python3
"""Suggest a local crop/reframe box by comparing an export image to an original."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImagePixels:
    width: int
    height: int
    gray: list[int]


@dataclass(frozen=True)
class CropSuggestion:
    x: int
    y: int
    width: int
    height: int
    score: float
    confidence: str
    reference_width: int
    reference_height: int
    candidate_width: int
    candidate_height: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Suggest how to crop an original image to match a Project365 export."
    )
    parser.add_argument("reference", help="Project365 exported image.")
    parser.add_argument("candidate", help="Candidate original image.")
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    suggestion = suggest_crop(
        reference_path=Path(args.reference),
        candidate_path=Path(args.candidate),
        sample_size=args.sample_size,
    )
    if args.json:
        print(json.dumps(asdict(suggestion), indent=2, sort_keys=True))
    else:
        print(
            "crop x={x} y={y} width={width} height={height} "
            "score={score:.2f} confidence={confidence}".format(
                **asdict(suggestion)
            )
        )
    return 0


def suggest_crop(
    reference_path: Path,
    candidate_path: Path,
    sample_size: int = 24,
    candidate_rotation_degrees: float = 0.0,
) -> CropSuggestion:
    if sample_size < 8:
        raise ValueError("sample-size must be at least 8")
    reference = load_image(reference_path)
    candidate = load_image(candidate_path)
    reference_sample = _resize_grayscale(reference, sample_size, sample_size)
    rotation_degrees = _normalized_rotation_degrees(candidate_rotation_degrees)

    aspect = reference.width / reference.height
    crop_sizes = _candidate_crop_sizes(candidate.width, candidate.height, aspect)
    best: tuple[float, int, int, int, int] | None = None

    for crop_w, crop_h in crop_sizes:
        step_x = max(1, crop_w // 20)
        step_y = max(1, crop_h // 20)
        x_values = _scan_positions(candidate.width, crop_w, step_x)
        y_values = _scan_positions(candidate.height, crop_h, step_y)
        for y in y_values:
            for x in x_values:
                sample = _sample_crop(
                    candidate,
                    x,
                    y,
                    crop_w,
                    crop_h,
                    sample_size,
                    rotation_degrees=rotation_degrees,
                )
                score = _mean_squared_error(reference_sample, sample)
                if best is None or score < best[0]:
                    best = (score, x, y, crop_w, crop_h)

    if best is None:
        raise ValueError("No valid crop candidates")

    score, x, y, crop_w, crop_h = best
    return CropSuggestion(
        x=x,
        y=y,
        width=crop_w,
        height=crop_h,
        score=score,
        confidence=_confidence(score),
        reference_width=reference.width,
        reference_height=reference.height,
        candidate_width=candidate.width,
        candidate_height=candidate.height,
    )


def load_image(path: Path) -> ImagePixels:
    path = path.resolve()
    if path.suffix.lower() == ".bmp":
        return _read_bmp(path)
    with tempfile.TemporaryDirectory() as temp_dir:
        errors = []
        for converter in (_convert_to_bmp_with_sips, _convert_to_bmp_with_magick):
            bmp_path = Path(temp_dir) / f"{converter.__name__}.bmp"
            try:
                converter(path, bmp_path)
                return _read_bmp(bmp_path)
            except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
                errors.append(f"{converter.__name__}: {exc}")
        raise ValueError(f"Cannot load image for crop estimation: {path}; {'; '.join(errors)}")


def _convert_to_bmp_with_sips(source_path: Path, output_path: Path) -> None:
    subprocess.run(
        ["sips", "-s", "format", "bmp", str(source_path), "--out", str(output_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _convert_to_bmp_with_magick(source_path: Path, output_path: Path) -> None:
    magick_path = shutil.which("magick")
    if not magick_path:
        raise RuntimeError("ImageMagick is not installed")
    subprocess.run(
        [magick_path, str(source_path), "-auto-orient", "-type", "TrueColor", f"BMP3:{output_path}"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _read_bmp(path: Path) -> ImagePixels:
    payload = path.read_bytes()
    if payload[:2] != b"BM":
        raise ValueError(f"Not a BMP file: {path}")
    pixel_offset = struct.unpack_from("<I", payload, 10)[0]
    dib_size = struct.unpack_from("<I", payload, 14)[0]
    if dib_size < 40:
        raise ValueError("Unsupported BMP DIB header")
    width = struct.unpack_from("<i", payload, 18)[0]
    height_raw = struct.unpack_from("<i", payload, 22)[0]
    planes = struct.unpack_from("<H", payload, 26)[0]
    bits_per_pixel = struct.unpack_from("<H", payload, 28)[0]
    compression = struct.unpack_from("<I", payload, 30)[0]
    if planes != 1 or bits_per_pixel not in {24, 32}:
        raise ValueError("Only 24-bit or 32-bit BMP files are supported")
    if compression not in {0, 3}:
        raise ValueError("Only uncompressed or bitfield BMP files are supported")
    if compression == 3 and bits_per_pixel != 32:
        raise ValueError("Only 32-bit bitfield BMP files are supported")
    width_abs = abs(width)
    height = abs(height_raw)
    top_down = height_raw < 0
    bytes_per_pixel = bits_per_pixel // 8
    row_stride = ((width_abs * bytes_per_pixel + 3) // 4) * 4
    gray = [0] * (width_abs * height)
    for row in range(height):
        source_row = row if top_down else height - 1 - row
        row_offset = pixel_offset + source_row * row_stride
        for col in range(width_abs):
            offset = row_offset + col * bytes_per_pixel
            blue, green, red = payload[offset : offset + 3]
            gray[row * width_abs + col] = round(0.299 * red + 0.587 * green + 0.114 * blue)
    return ImagePixels(width=width_abs, height=height, gray=gray)


def _candidate_crop_sizes(width: int, height: int, aspect: float) -> list[tuple[int, int]]:
    max_w = width
    max_h = height
    if max_w / max_h > aspect:
        crop_h = max_h
        crop_w = round(crop_h * aspect)
    else:
        crop_w = max_w
        crop_h = round(crop_w / aspect)
    sizes: list[tuple[int, int]] = []
    min_scale = 0.55
    for scale_step in range(10):
        scale = 1.0 - scale_step * ((1.0 - min_scale) / 9)
        w = max(1, round(crop_w * scale))
        h = max(1, round(w / aspect))
        if w <= width and h <= height and (w, h) not in sizes:
            sizes.append((w, h))
    return sizes


def _scan_positions(limit: int, crop: int, step: int) -> list[int]:
    if crop >= limit:
        return [0]
    positions = list(range(0, limit - crop + 1, step))
    final = limit - crop
    if positions[-1] != final:
        positions.append(final)
    return positions


def _sample_crop(
    image: ImagePixels,
    x: int,
    y: int,
    width: int,
    height: int,
    sample_size: int,
    rotation_degrees: float = 0.0,
) -> list[int]:
    rotation = _normalized_rotation_degrees(rotation_degrees)
    if rotation:
        return _sample_rotated_crop(image, x, y, width, height, sample_size, rotation)
    sample = []
    for sample_y in range(sample_size):
        source_y = y + min(height - 1, round(sample_y * (height - 1) / max(1, sample_size - 1)))
        for sample_x in range(sample_size):
            source_x = x + min(width - 1, round(sample_x * (width - 1) / max(1, sample_size - 1)))
            sample.append(image.gray[source_y * image.width + source_x])
    return sample


def _sample_rotated_crop(
    image: ImagePixels,
    x: int,
    y: int,
    width: int,
    height: int,
    sample_size: int,
    rotation_degrees: float,
) -> list[int]:
    radians = -rotation_degrees * math.pi / 180
    cos_r = math.cos(radians)
    sin_r = math.sin(radians)
    center_x = image.width / 2
    center_y = image.height / 2
    sample = []
    for sample_y in range(sample_size):
        output_y = y + min(height - 1, sample_y * (height - 1) / max(1, sample_size - 1))
        for sample_x in range(sample_size):
            output_x = x + min(width - 1, sample_x * (width - 1) / max(1, sample_size - 1))
            dx = output_x - center_x
            dy = output_y - center_y
            source_x = center_x + dx * cos_r - dy * sin_r
            source_y = center_y + dx * sin_r + dy * cos_r
            sample.append(_nearest_gray(image, source_x, source_y))
    return sample


def _nearest_gray(image: ImagePixels, x: float, y: float) -> int:
    source_x = round(x)
    source_y = round(y)
    if source_x < 0 or source_y < 0 or source_x >= image.width or source_y >= image.height:
        return 0
    return image.gray[source_y * image.width + source_x]


def _resize_grayscale(image: ImagePixels, width: int, height: int) -> list[int]:
    return _sample_crop(image, 0, 0, image.width, image.height, width)


def _mean_squared_error(left: list[int], right: list[int]) -> float:
    return sum((a - b) ** 2 for a, b in zip(left, right, strict=True)) / len(left)


def _confidence(score: float) -> str:
    if score <= 20:
        return "high"
    if score <= 200:
        return "medium"
    return "low"


def _normalized_rotation_degrees(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    while number <= -180:
        number += 360
    while number > 180:
        number -= 360
    return round(number, 3)


if __name__ == "__main__":
    raise SystemExit(main())
