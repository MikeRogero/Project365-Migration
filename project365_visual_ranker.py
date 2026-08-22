#!/usr/bin/env python3
"""Cache and rank local visual similarity for Project365 original candidates."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import sqlite3
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project365_crop_align import _mean_squared_error


METHOD_VERSION = "visual_similarity_v2"
DEFAULT_SAMPLE_SIZE = 16
DEFAULT_LIKELY_LIMIT = 20
DEFAULT_OVERSIZED_THRESHOLD = 20


@dataclass(frozen=True)
class VisualImage:
    width: int
    height: int
    rgb: list[tuple[int, int, int]]


@dataclass(frozen=True)
class VisualRankSummary:
    queue_path: str
    cache_path: str
    entry_count: int
    candidate_count: int
    ranked_count: int
    error_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Rank oversized Project365 original-candidate sets locally.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument(
        "--queue",
        default="Project365Canonical/exports/verification_reports/original_photo_external_search_queue.csv",
    )
    parser.add_argument("--entry-id", action="append", default=[])
    parser.add_argument("--entry-date", action="append", default=[])
    parser.add_argument("--threshold", type=int, default=DEFAULT_OVERSIZED_THRESHOLD)
    parser.add_argument("--likely-limit", type=int, default=DEFAULT_LIKELY_LIMIT)
    args = parser.parse_args()

    summary = rank_queue(
        canonical_root=Path(args.canonical_root),
        queue_path=Path(args.queue),
        target_entry_ids=set(args.entry_id),
        target_entry_dates=set(args.entry_date),
        threshold=args.threshold,
        likely_limit=args.likely_limit,
    )
    print("Project365 visual ranking: PASS")
    print(f"Queue: {summary.queue_path}")
    print(f"Cache: {summary.cache_path}")
    print(f"Entries: {summary.entry_count}")
    print(f"Candidate rows: {summary.candidate_count}")
    print(f"Ranked rows: {summary.ranked_count}")
    print(f"Error rows: {summary.error_count}")
    return 0


def rank_queue(
    canonical_root: Path,
    queue_path: Path,
    target_entry_ids: set[str] | None = None,
    target_entry_dates: set[str] | None = None,
    threshold: int = DEFAULT_OVERSIZED_THRESHOLD,
    likely_limit: int = DEFAULT_LIKELY_LIMIT,
) -> VisualRankSummary:
    if not queue_path.exists():
        raise FileNotFoundError(f"Missing search queue: {queue_path}")
    fieldnames, rows = _read_csv_with_fieldnames(queue_path)
    fieldnames = _with_visual_fieldnames(fieldnames)
    source_media = _load_source_media(canonical_root / "canonical.db")
    cache_path = default_cache_path(canonical_root)
    cache = VisualDescriptorCache(cache_path)

    try:
        entry_count = 0
        candidate_count = 0
        ranked_count = 0
        error_count = 0
        grouped = _group_by_entry(rows)
        for entry_id, entry_rows in grouped.items():
            if target_entry_ids and entry_id not in target_entry_ids:
                continue
            entry_date = str(entry_rows[0].get("entry_date", ""))
            if target_entry_dates and entry_date not in target_entry_dates:
                continue
            candidate_rows = [row for row in entry_rows if row.get("candidate_path", "").strip()]
            if len(candidate_rows) <= threshold:
                continue
            source_path = source_media.get(entry_id)
            if source_path is None:
                for row in candidate_rows:
                    _set_visual_error(row, "missing_reference")
                entry_count += 1
                candidate_count += len(candidate_rows)
                error_count += len(candidate_rows)
                continue
            results = rank_candidate_rows(cache, source_path, candidate_rows, likely_limit=likely_limit)
            entry_count += 1
            candidate_count += len(candidate_rows)
            for row in candidate_rows:
                result = results.get(str(row.get("candidate_path", "")))
                if not result:
                    continue
                row.update(result)
                if result.get("visual_error"):
                    error_count += 1
                else:
                    ranked_count += 1
    finally:
        cache.close()
    _write_csv(queue_path, rows, fieldnames)
    return VisualRankSummary(
        queue_path=str(queue_path),
        cache_path=str(cache_path),
        entry_count=entry_count,
        candidate_count=candidate_count,
        ranked_count=ranked_count,
        error_count=error_count,
    )


def rank_candidate_rows(
    cache: "VisualDescriptorCache",
    reference_path: Path,
    candidate_rows: list[dict[str, str]],
    likely_limit: int = DEFAULT_LIKELY_LIMIT,
) -> dict[str, dict[str, str]]:
    reference_result = cache.descriptor(reference_path)
    if reference_result.get("error"):
        return {
            str(row.get("candidate_path", "")): _visual_error_fields(str(reference_result["error"]))
            for row in candidate_rows
        }
    reference = reference_result["descriptor"]
    scored: list[tuple[float, str, str]] = []
    errors: dict[str, str] = {}
    for row in candidate_rows:
        path_text = str(row.get("candidate_path", ""))
        descriptor_result = cache.descriptor(Path(path_text))
        if descriptor_result.get("error"):
            errors[path_text] = str(descriptor_result["error"])
            continue
        score, view = _visual_score(reference, descriptor_result["descriptor"])
        scored.append((score, path_text, view))
    scored.sort(key=lambda item: (item[0], item[1]))
    best_score = scored[0][0] if scored else 0.0
    results: dict[str, dict[str, str]] = {}
    for rank, (score, path_text, view) in enumerate(scored, start=1):
        results[path_text] = {
            "visual_rank": str(rank),
            "visual_score": f"{score:.4f}",
            "visual_score_gap": f"{score - best_score:.4f}",
            "visual_likely": "true" if rank <= likely_limit else "false",
            "visual_method": METHOD_VERSION,
            "visual_best_view": view,
            "visual_error": "",
        }
    for path_text, error in errors.items():
        results[path_text] = _visual_error_fields(error)
    return results


class VisualDescriptorCache:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_descriptors (
                path TEXT PRIMARY KEY,
                byte_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                method_version TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                error TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def descriptor(self, path: Path) -> dict[str, Any]:
        try:
            stat = path.stat()
        except OSError:
            return {"error": "missing_file"}
        path_text = str(path)
        row = self._connection.execute(
            """
            SELECT byte_size, mtime_ns, method_version, descriptor_json, error
            FROM visual_descriptors
            WHERE path = ?
            """,
            (path_text,),
        ).fetchone()
        if (
            row
            and int(row["byte_size"]) == stat.st_size
            and int(row["mtime_ns"]) == stat.st_mtime_ns
            and row["method_version"] == METHOD_VERSION
        ):
            if row["error"]:
                return {"error": row["error"]}
            return {"descriptor": json.loads(row["descriptor_json"])}
        error = ""
        descriptor: dict[str, Any] = {}
        try:
            descriptor = build_descriptor(path)
        except Exception as exc:  # noqa: BLE001 - cache stores local decode failures as review evidence.
            error = type(exc).__name__
        self._connection.execute(
            """
            INSERT INTO visual_descriptors (
                path, byte_size, mtime_ns, method_version, descriptor_json, error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path)
            DO UPDATE SET
                byte_size = excluded.byte_size,
                mtime_ns = excluded.mtime_ns,
                method_version = excluded.method_version,
                descriptor_json = excluded.descriptor_json,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                path_text,
                stat.st_size,
                stat.st_mtime_ns,
                METHOD_VERSION,
                json.dumps(descriptor, sort_keys=True),
                error,
                dt.datetime.now(dt.UTC).isoformat(),
            ),
        )
        self._connection.commit()
        if error:
            return {"error": error}
        return {"descriptor": descriptor}

    def close(self) -> None:
        self._connection.close()


def build_descriptor(path: Path, sample_size: int = DEFAULT_SAMPLE_SIZE) -> dict[str, Any]:
    image = load_visual_image(path)
    views = []
    for name, x, y, width, height in _candidate_views(image.width, image.height):
        views.append(_view_descriptor(image, name, x, y, width, height, sample_size))
    return {
        "method": METHOD_VERSION,
        "width": image.width,
        "height": image.height,
        "views": views,
    }


def load_visual_image(path: Path) -> VisualImage:
    path = path.resolve()
    if path.suffix.lower() == ".bmp":
        return _read_bmp_rgb(path)
    with tempfile.TemporaryDirectory() as temp_dir:
        bmp_path = Path(temp_dir) / "image.bmp"
        try:
            subprocess.run(
                ["sips", "-s", "format", "bmp", str(path), "--out", str(bmp_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            magick_path = shutil.which("magick")
            if not magick_path:
                raise
            subprocess.run(
                [magick_path, str(path), str(bmp_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return _read_bmp_rgb(bmp_path)


def _read_bmp_rgb(path: Path) -> VisualImage:
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
    rgb = [(0, 0, 0)] * (width_abs * height)
    for row in range(height):
        source_row = row if top_down else height - 1 - row
        row_offset = pixel_offset + source_row * row_stride
        for col in range(width_abs):
            offset = row_offset + col * bytes_per_pixel
            blue, green, red = payload[offset : offset + 3]
            rgb[row * width_abs + col] = (red, green, blue)
    return VisualImage(width=width_abs, height=height, rgb=rgb)


def _candidate_views(width: int, height: int) -> list[tuple[str, int, int, int, int]]:
    views = [("full", 0, 0, width, height)]
    size = min(width, height)
    if width == height:
        return views
    if width > height:
        positions = [0, round((width - size) / 2), width - size]
        names = ["left_square", "center_square", "right_square"]
        views.extend((name, x, 0, size, size) for name, x in zip(names, positions, strict=True))
    else:
        positions = [0, round((height - size) / 2), height - size]
        names = ["top_square", "center_square", "bottom_square"]
        views.extend((name, 0, y, size, size) for name, y in zip(names, positions, strict=True))
    return views


def _view_descriptor(
    image: VisualImage,
    name: str,
    x: int,
    y: int,
    width: int,
    height: int,
    sample_size: int,
) -> dict[str, Any]:
    gray: list[int] = []
    red_total = 0
    green_total = 0
    blue_total = 0
    for sample_y in range(sample_size):
        source_y = y + min(height - 1, round(sample_y * (height - 1) / max(1, sample_size - 1)))
        for sample_x in range(sample_size):
            source_x = x + min(width - 1, round(sample_x * (width - 1) / max(1, sample_size - 1)))
            red, green, blue = image.rgb[source_y * image.width + source_x]
            gray.append(round(0.299 * red + 0.587 * green + 0.114 * blue))
            red_total += red
            green_total += green
            blue_total += blue
    count = sample_size * sample_size
    return {
        "name": name,
        "gray": gray,
        "mean_rgb": [
            round(red_total / count, 3),
            round(green_total / count, 3),
            round(blue_total / count, 3),
        ],
    }


def _visual_score(reference: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, str]:
    reference_view = reference["views"][0]
    best: tuple[float, str] | None = None
    for candidate_view in candidate.get("views", []):
        gray_score = _mean_squared_error(reference_view["gray"], candidate_view["gray"])
        color_score = _mean_squared_error(reference_view["mean_rgb"], candidate_view["mean_rgb"])
        score = gray_score + color_score * 0.15
        if best is None or score < best[0]:
            best = (score, str(candidate_view["name"]))
    if best is None:
        raise ValueError("Candidate has no visual descriptor views")
    return best


def default_cache_path(canonical_root: Path) -> Path:
    return canonical_root / "visual_similarity_cache.sqlite"


def visual_fieldnames() -> list[str]:
    return [
        "visual_rank",
        "visual_score",
        "visual_score_gap",
        "visual_likely",
        "visual_method",
        "visual_best_view",
        "visual_error",
    ]


def _with_visual_fieldnames(fieldnames: list[str]) -> list[str]:
    updated = list(fieldnames)
    for field in visual_fieldnames():
        if field not in updated:
            updated.append(field)
    return updated


def _visual_error_fields(error: str) -> dict[str, str]:
    return {
        "visual_rank": "",
        "visual_score": "",
        "visual_score_gap": "",
        "visual_likely": "false",
        "visual_method": METHOD_VERSION,
        "visual_best_view": "",
        "visual_error": error,
    }


def _set_visual_error(row: dict[str, str], error: str) -> None:
    row.update(_visual_error_fields(error))


def _read_csv_with_fieldnames(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)


def _group_by_entry(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("entry_id", "")), []).append(row)
    return grouped


def _load_source_media(db_path: Path) -> dict[str, Path]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT entry_id, storage_path
            FROM media_assets
            WHERE role = 'project365_export_png'
            """
        ).fetchall()
    finally:
        connection.close()
    return {entry_id: Path(storage_path) for entry_id, storage_path in rows if storage_path}


if __name__ == "__main__":
    raise SystemExit(main())
