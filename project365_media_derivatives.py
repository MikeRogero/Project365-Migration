#!/usr/bin/env python3
"""Generate local Diarium media derivatives from canonical media assets."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_DERIVATIVE_POLICY = "Project365_square_2560_q88"


@dataclass(frozen=True)
class DerivativeSummary:
    output_dir: str
    report_path: str
    generated_count: int
    skipped_count: int
    not_ready_count: int
    format: str
    long_edge: int
    not_ready_dates: tuple[str, ...] = ()


@dataclass(frozen=True)
class DerivativeReadinessSummary:
    source_count: int
    ready_count: int
    not_ready_count: int
    current_count: int
    needs_update_count: int
    primary_source_count: int = 0
    primary_ready_count: int = 0
    primary_not_ready_count: int = 0
    associated_source_count: int = 0
    associated_ready_count: int = 0
    associated_not_ready_count: int = 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Diarium-ready media derivatives locally."
    )
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--format", choices=["jpeg", "heic"], default="jpeg")
    parser.add_argument("--long-edge", type=int, default=2560)
    parser.add_argument("--quality", type=int, default=88)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate every derivative even when an existing output is current.",
    )
    parser.add_argument(
        "--include-associated",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include linked photos in working copies (default: included).",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=0,
        help="Print progress every N source rows. Disabled by default.",
    )
    parser.add_argument(
        "--reuse-existing-newer-than",
        default="",
        help="When forcing, adopt an existing derivative file newer than this ISO timestamp instead of rewriting it.",
    )
    parser.add_argument(
        "--commit-interval",
        type=int,
        default=100,
        help="Commit derivative metadata every N processed rows. Use 0 to commit only at the end.",
    )
    args = parser.parse_args()

    summary = generate_derivatives(
        canonical_root=Path(args.canonical_root),
        output_format=args.format,
        long_edge=args.long_edge,
        quality=args.quality,
        limit=args.limit,
        start_date=args.start_date,
        end_date=args.end_date,
        force=args.force,
        include_associated=args.include_associated,
        progress_interval=args.progress_interval,
        progress_sink=lambda message: print(message, flush=True),
        reuse_existing_newer_than=args.reuse_existing_newer_than,
        commit_interval=args.commit_interval,
    )
    print("Project365 media derivative generation: PASS")
    print(f"Output: {summary.output_dir}")
    print(f"Report: {summary.report_path}")
    print(f"Generated: {summary.generated_count}")
    print(f"Skipped: {summary.skipped_count}")
    print(f"Not ready: {summary.not_ready_count}")
    if summary.not_ready_dates:
        print(f"Not ready dates: {', '.join(summary.not_ready_dates)}")
    print(f"Format: {summary.format}")
    print(f"Long edge: {summary.long_edge}")
    if args.start_date or args.end_date:
        print(f"Date scope: {args.start_date or 'start'} to {args.end_date or 'end'}")
    return 0


def generate_derivatives(
    canonical_root: Path,
    output_format: str = "jpeg",
    long_edge: int = 2560,
    quality: int = 88,
    limit: int | None = None,
    start_date: str = "",
    end_date: str = "",
    force: bool = False,
    include_associated: bool = True,
    progress_interval: int = 0,
    progress_sink: Callable[[str], None] | None = None,
    reuse_existing_newer_than: str = "",
    commit_interval: int = 100,
) -> DerivativeSummary:
    if output_format not in {"jpeg", "heic"}:
        raise ValueError("output_format must be jpeg or heic")
    if long_edge <= 0:
        raise ValueError("long_edge must be positive")
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")

    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")

    policy = derivative_policy_name(output_format, long_edge, quality)
    output_root = canonical_root / "media" / "diarium_derivatives" / policy
    report_path = canonical_root / "exports" / "verification_reports" / f"media_derivatives_{policy}.csv"
    output_root.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    reuse_existing_after = _parse_optional_datetime(reuse_existing_newer_than)

    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = _load_source_media(
            connection,
            policy,
            limit,
            start_date,
            end_date,
            include_associated=include_associated,
        )
        staged_crops = _load_staged_review_crops(canonical_root)
        report_rows = []
        generated_count = 0
        skipped_count = 0
        not_ready_count = 0
        not_ready_dates: set[str] = set()
        total_count = len(rows)
        processed_count = 0
        _emit_derivative_progress(
            progress_sink,
            progress_interval,
            processed_count,
            total_count,
            generated_count,
            skipped_count,
            not_ready_count,
        )
        for row in rows:
            source_path = Path(row["storage_path"])
            month = row["entry_date"][:7]
            extension = "jpg" if output_format == "jpeg" else "heic"
            output_stem = _derivative_output_stem(row, long_edge)
            output_path = output_root / month / f"{output_stem}.{extension}"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            crop = _review_crop_from_transformation(row["source_transformation_json"])
            crop = _staged_review_crop_for_row(staged_crops, row) or crop
            derivative_id = str(row["derivative_id"])
            derivative_role = str(row["derivative_role"])
            if not source_path.exists():
                not_ready_count += 1
                not_ready_dates.add(str(row["entry_date"]))
                _mark_derivative_not_ready(connection, row, derivative_id, "missing_source_media")
                report_rows.append(
                    _report_row(
                        row=row,
                        derivative_id=derivative_id,
                        output_path=output_path,
                        output_format=output_format,
                        long_edge=long_edge,
                        quality=quality,
                        crop=crop,
                        width="",
                        height="",
                        byte_size=int(row["derivative_byte_size"] or 0),
                        sha256=str(row["derivative_sha256"] or ""),
                        status="not_ready_missing_source",
                    )
                )
                processed_count += 1
                _emit_derivative_progress(
                    progress_sink,
                    progress_interval,
                    processed_count,
                    total_count,
                    generated_count,
                    skipped_count,
                    not_ready_count,
                    str(row["entry_id"]),
                )
                continue
            if crop is None:
                not_ready_count += 1
                not_ready_dates.add(str(row["entry_date"]))
                _mark_derivative_not_ready(connection, row, derivative_id, "missing_review_crop")
                report_rows.append(
                    _report_row(
                        row=row,
                        derivative_id=derivative_id,
                        output_path=output_path,
                        output_format=output_format,
                        long_edge=long_edge,
                        quality=quality,
                        crop=None,
                        width="",
                        height="",
                        byte_size=int(row["derivative_byte_size"] or 0),
                        sha256=str(row["derivative_sha256"] or ""),
                        status="not_ready_missing_crop",
                    )
                )
                processed_count += 1
                _emit_derivative_progress(
                    progress_sink,
                    progress_interval,
                    processed_count,
                    total_count,
                    generated_count,
                    skipped_count,
                    not_ready_count,
                    str(row["entry_id"]),
                )
                continue
            transformation = {
                "source_media_asset_id": row["media_asset_id"],
                "source_role": row["source_role"],
                "derivative_role": derivative_role,
                "source_sha256": row["source_sha256"],
                "source_byte_size": int(row["source_byte_size"]),
                "source_path": row["storage_path"],
                "format": output_format,
                "long_edge": long_edge,
                "quality": quality,
                "crop": crop,
            }
            if not force and _derivative_is_current(row, output_path, transformation):
                skipped_count += 1
                report_rows.append(
                    _report_row(
                        row=row,
                        derivative_id=derivative_id,
                        output_path=output_path,
                        output_format=output_format,
                        long_edge=long_edge,
                        quality=quality,
                        crop=crop,
                        width="",
                        height="",
                        byte_size=int(row["derivative_byte_size"] or output_path.stat().st_size),
                        sha256=str(row["derivative_sha256"] or ""),
                        status="skipped",
                    )
                )
                processed_count += 1
                _emit_derivative_progress(
                    progress_sink,
                    progress_interval,
                    processed_count,
                    total_count,
                    generated_count,
                    skipped_count,
                    not_ready_count,
                    str(row["entry_id"]),
                )
                _commit_progress(connection, processed_count, commit_interval)
                continue

            if force and _can_reuse_existing_derivative(output_path, reuse_existing_after):
                sha256 = _sha256_file(output_path)
                byte_size = output_path.stat().st_size
                width, height = _image_dimensions(output_path)
                if width == height:
                    _upsert_derivative_asset(
                        connection=connection,
                        row=row,
                        derivative_id=derivative_id,
                        derivative_role=derivative_role,
                        output_path=output_path,
                        sha256=sha256,
                        byte_size=byte_size,
                        output_format=output_format,
                        transformation=transformation,
                    )
                    skipped_count += 1
                    report_rows.append(
                        _report_row(
                            row=row,
                            derivative_id=derivative_id,
                            output_path=output_path,
                            output_format=output_format,
                            long_edge=long_edge,
                            quality=quality,
                            crop=crop,
                            width=width,
                            height=height,
                            byte_size=byte_size,
                            sha256=sha256,
                            status="skipped_recent_existing",
                        )
                    )
                    processed_count += 1
                    _emit_derivative_progress(
                        progress_sink,
                        progress_interval,
                        processed_count,
                        total_count,
                        generated_count,
                        skipped_count,
                        not_ready_count,
                        str(row["entry_id"]),
                    )
                    _commit_progress(connection, processed_count, commit_interval)
                    continue

            try:
                _convert_image_atomically(source_path, output_path, output_format, long_edge, quality, crop)
                if not output_path.exists():
                    raise FileNotFoundError(f"Derivative conversion did not create output: {output_path}")
                sha256 = _sha256_file(output_path)
                byte_size = output_path.stat().st_size
                width, height = _image_dimensions(output_path)
                if width != height:
                    output_path.unlink(missing_ok=True)
                    raise ValueError(
                        f"Derivative output is not square for {row['entry_id']}: {width} x {height}"
                    )
            except (subprocess.CalledProcessError, RuntimeError, FileNotFoundError, ValueError) as exc:
                not_ready_count += 1
                not_ready_dates.add(str(row["entry_date"]))
                _mark_derivative_not_ready(connection, row, derivative_id, "conversion_failed")
                report_rows.append(
                    _report_row(
                        row=row,
                        derivative_id=derivative_id,
                        output_path=output_path,
                        output_format=output_format,
                        long_edge=long_edge,
                        quality=quality,
                        crop=crop,
                        width="",
                        height="",
                        byte_size=int(row["derivative_byte_size"] or 0),
                        sha256=str(row["derivative_sha256"] or ""),
                        status="not_ready_conversion_failed",
                        error=_format_derivative_error(exc),
                    )
                )
                processed_count += 1
                _emit_derivative_progress(
                    progress_sink,
                    progress_interval,
                    processed_count,
                    total_count,
                    generated_count,
                    skipped_count,
                    not_ready_count,
                    str(row["entry_id"]),
                )
                continue
            now = dt.datetime.now(dt.UTC).isoformat()
            _upsert_derivative_asset(
                connection=connection,
                row=row,
                derivative_id=derivative_id,
                derivative_role=derivative_role,
                output_path=output_path,
                sha256=sha256,
                byte_size=byte_size,
                output_format=output_format,
                transformation=transformation,
                now=now,
            )
            generated_count += 1
            report_rows.append(
                _report_row(
                    row=row,
                    derivative_id=derivative_id,
                    output_path=output_path,
                    output_format=output_format,
                    long_edge=long_edge,
                    quality=quality,
                    crop=crop,
                    width=width,
                    height=height,
                    byte_size=byte_size,
                    sha256=sha256,
                    status="generated",
                )
            )
            processed_count += 1
            _emit_derivative_progress(
                progress_sink,
                progress_interval,
                processed_count,
                total_count,
                generated_count,
                skipped_count,
                not_ready_count,
                str(row["entry_id"]),
            )
            _commit_progress(connection, processed_count, commit_interval)
        connection.commit()
    finally:
        connection.close()

    _write_report(report_path, report_rows)
    return DerivativeSummary(
        output_dir=str(output_root),
        report_path=str(report_path),
        generated_count=generated_count,
        skipped_count=skipped_count,
        not_ready_count=not_ready_count,
        format=output_format,
        long_edge=long_edge,
        not_ready_dates=tuple(sorted(not_ready_dates)),
    )


def _emit_derivative_progress(
    progress_sink: Callable[[str], None] | None,
    progress_interval: int,
    processed_count: int,
    total_count: int,
    generated_count: int,
    skipped_count: int,
    not_ready_count: int,
    current_entry_id: str = "",
) -> None:
    if progress_sink is None or progress_interval <= 0:
        return
    if processed_count not in {0, total_count} and processed_count % progress_interval != 0:
        return
    current = f" · current {current_entry_id}" if current_entry_id else ""
    progress_sink(
        "Progress: "
        f"{processed_count}/{total_count} sources · "
        f"generated {generated_count} · "
        f"skipped {skipped_count} · "
        f"not ready {not_ready_count}"
        f"{current}"
    )


def _parse_optional_datetime(value: str) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _can_reuse_existing_derivative(output_path: Path, newer_than: dt.datetime | None) -> bool:
    if newer_than is None or not output_path.exists():
        return False
    output_mtime = dt.datetime.fromtimestamp(output_path.stat().st_mtime, dt.UTC)
    return output_mtime >= newer_than


def _commit_progress(
    connection: sqlite3.Connection,
    processed_count: int,
    commit_interval: int,
) -> None:
    if commit_interval > 0 and processed_count > 0 and processed_count % commit_interval == 0:
        connection.commit()


def _upsert_derivative_asset(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    derivative_id: str,
    derivative_role: str,
    output_path: Path,
    sha256: str,
    byte_size: int,
    output_format: str,
    transformation: dict[str, object],
    now: str | None = None,
) -> None:
    timestamp = now or dt.datetime.now(dt.UTC).isoformat()
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available',
                'unreviewed', 0, ?, ?, ?, ?)
        ON CONFLICT(id)
        DO UPDATE SET
            source_file_id = excluded.source_file_id,
            internal_filename = excluded.internal_filename,
            storage_path = excluded.storage_path,
            sha256 = excluded.sha256,
            byte_size = excluded.byte_size,
            mime_type = excluded.mime_type,
            status = excluded.status,
            review_status = excluded.review_status,
            transformation_json = excluded.transformation_json,
            import_batch_id = excluded.import_batch_id,
            updated_at = excluded.updated_at
        """,
        (
            derivative_id,
            row["entry_id"],
            derivative_role,
            row["source_file_id"],
            output_path.name,
            str(output_path),
            sha256,
            byte_size,
            _mime_type(output_format),
            json.dumps(transformation, sort_keys=True),
            row["import_batch_id"],
            timestamp,
            timestamp,
        ),
    )


def _format_derivative_error(error: BaseException) -> str:
    text = str(error).replace("\n", " ").strip()
    if isinstance(error, subprocess.CalledProcessError):
        command = " ".join(str(part) for part in error.cmd)
        text = f"{command} returned exit status {error.returncode}"
    return text[:500]


def derivative_policy_name(output_format: str, long_edge: int, quality: int) -> str:
    if output_format == "jpeg" and long_edge == 2560 and quality == 88:
        return DEFAULT_DERIVATIVE_POLICY
    return f"{output_format}_{long_edge}_q{quality}"


def derivative_readiness_summary(
    canonical_root: Path,
    output_format: str = "jpeg",
    long_edge: int = 2560,
    quality: int = 88,
    start_date: str = "",
    end_date: str = "",
    include_associated: bool = True,
) -> DerivativeReadinessSummary:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        return DerivativeReadinessSummary(
            source_count=0,
            ready_count=0,
            not_ready_count=0,
            current_count=0,
            needs_update_count=0,
        )

    policy = derivative_policy_name(output_format, long_edge, quality)
    output_root = canonical_root / "media" / "diarium_derivatives" / policy
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = _load_source_media(
            connection,
            policy,
            None,
            start_date,
            end_date,
            include_associated=include_associated,
        )
        staged_crops = _load_staged_review_crops(canonical_root)
    except sqlite3.Error:
        return DerivativeReadinessSummary(
            source_count=0,
            ready_count=0,
            not_ready_count=0,
            current_count=0,
            needs_update_count=0,
        )
    finally:
        connection.close()

    ready_count = 0
    current_count = 0
    needs_update_count = 0
    primary_source_count = 0
    primary_ready_count = 0
    associated_source_count = 0
    associated_ready_count = 0
    extension = "jpg" if output_format == "jpeg" else "heic"
    for row in rows:
        is_associated = str(row["source_role"]) == "associated"
        if is_associated:
            associated_source_count += 1
        else:
            primary_source_count += 1
        try:
            crop = _review_crop_from_transformation(row["source_transformation_json"])
        except (TypeError, ValueError):
            crop = None
        crop = _staged_review_crop_for_row(staged_crops, row) or crop
        if not Path(row["storage_path"]).exists():
            continue
        if crop is None:
            continue
        ready_count += 1
        if is_associated:
            associated_ready_count += 1
        else:
            primary_ready_count += 1
        month = row["entry_date"][:7]
        output_path = output_root / month / f"{_derivative_output_stem(row, long_edge)}.{extension}"
        transformation = {
            "source_media_asset_id": row["media_asset_id"],
            "source_role": row["source_role"],
            "derivative_role": row["derivative_role"],
            "source_sha256": row["source_sha256"],
            "source_byte_size": int(row["source_byte_size"]),
            "source_path": row["storage_path"],
            "format": output_format,
            "long_edge": long_edge,
            "quality": quality,
            "crop": crop,
        }
        if _derivative_is_current(row, output_path, transformation):
            current_count += 1
        else:
            needs_update_count += 1
    return DerivativeReadinessSummary(
        source_count=len(rows),
        ready_count=ready_count,
        not_ready_count=len(rows) - ready_count,
        current_count=current_count,
        needs_update_count=needs_update_count,
        primary_source_count=primary_source_count,
        primary_ready_count=primary_ready_count,
        primary_not_ready_count=primary_source_count - primary_ready_count,
        associated_source_count=associated_source_count,
        associated_ready_count=associated_ready_count,
        associated_not_ready_count=associated_source_count - associated_ready_count,
    )


def _load_staged_review_crops(canonical_root: Path) -> dict[tuple[str, str], dict[str, object]]:
    report_dir = canonical_root / "exports" / "verification_reports"
    paths = [report_dir / "original_photo_external_search_queue_crop_staging.json"]
    if report_dir.exists():
        paths.extend(
            path
            for path in sorted(report_dir.glob("*_crop_staging.json"))
            if path not in paths
        )
    staged: dict[tuple[str, str], dict[str, object]] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        if not isinstance(entries, dict):
            continue
        for entry_id, candidates in entries.items():
            if not isinstance(candidates, dict):
                continue
            for candidate_path, record in candidates.items():
                if not isinstance(record, dict):
                    continue
                crop = record.get("crop")
                if isinstance(crop, dict):
                    staged[(str(entry_id), str(candidate_path))] = dict(crop)
    return staged


def _staged_review_crop_for_row(
    staged_crops: dict[tuple[str, str], dict[str, object]],
    row: sqlite3.Row,
) -> dict[str, object] | None:
    return staged_crops.get((str(row["entry_id"]), str(row["storage_path"])))


def _load_source_media(
    connection: sqlite3.Connection,
    policy: str,
    limit: int | None,
    start_date: str = "",
    end_date: str = "",
    include_associated: bool = True,
) -> list[sqlite3.Row]:
    query = """
        WITH source_rows AS (
            SELECT
                entries.id AS entry_id,
                entries.entry_date,
                COALESCE(external_media.id, fallback_media.id) AS media_asset_id,
                COALESCE(external_media.source_file_id, fallback_media.source_file_id) AS source_file_id,
                COALESCE(external_media.storage_path, fallback_media.storage_path) AS storage_path,
                COALESCE(external_media.sha256, fallback_media.sha256) AS source_sha256,
                COALESCE(external_media.byte_size, fallback_media.byte_size) AS source_byte_size,
                COALESCE(external_media.transformation_json, fallback_media.transformation_json) AS source_transformation_json,
                COALESCE(external_media.import_batch_id, fallback_media.import_batch_id) AS import_batch_id,
                COALESCE(external_media.updated_at, fallback_media.updated_at) AS source_updated_at,
                'primary' AS source_role,
                'diarium_derivative' AS derivative_role,
                entries.id || ':diarium_derivative:' || ? AS derivative_id
            FROM entries
            LEFT JOIN media_assets AS external_media
                ON external_media.id = (
                    SELECT id
                    FROM media_assets
                    WHERE entry_id = entries.id
                        AND role = 'external_original_reference'
                        AND review_status = 'confirmed'
                    ORDER BY updated_at DESC, id
                    LIMIT 1
                )
            LEFT JOIN media_assets AS fallback_media
                ON fallback_media.entry_id = entries.id
                AND fallback_media.selected_default = 1
            WHERE COALESCE(external_media.id, fallback_media.id) IS NOT NULL
    """
    params: list[object] = [policy]
    if include_associated:
        query += """
            UNION ALL
            SELECT
                entries.id AS entry_id,
                entries.entry_date,
                associated_media.id AS media_asset_id,
                associated_media.source_file_id AS source_file_id,
                associated_media.storage_path AS storage_path,
                associated_media.sha256 AS source_sha256,
                associated_media.byte_size AS source_byte_size,
                associated_media.transformation_json AS source_transformation_json,
                associated_media.import_batch_id AS import_batch_id,
                associated_media.updated_at AS source_updated_at,
                'associated' AS source_role,
                'diarium_associated_derivative' AS derivative_role,
                associated_media.id || ':diarium_associated_derivative:' || ? AS derivative_id
            FROM entries
            JOIN media_assets AS associated_media
                ON associated_media.entry_id = entries.id
                AND associated_media.role = 'external_original_associated_photo'
                AND associated_media.review_status = 'confirmed'
                AND associated_media.status = 'available'
                AND COALESCE(associated_media.storage_path, '') != ''
        """
        params.append(policy)
    query += """
        )
        SELECT
            source_rows.entry_id,
            source_rows.entry_date,
            source_rows.media_asset_id,
            source_rows.source_file_id,
            source_rows.storage_path,
            source_rows.source_sha256,
            source_rows.source_byte_size,
            source_rows.source_transformation_json,
            source_rows.import_batch_id,
            source_rows.source_updated_at,
            source_rows.source_role,
            source_rows.derivative_role,
            source_rows.derivative_id,
            derivative_media.id AS derivative_media_asset_id,
            derivative_media.storage_path AS derivative_storage_path,
            derivative_media.sha256 AS derivative_sha256,
            derivative_media.byte_size AS derivative_byte_size,
            derivative_media.status AS derivative_status,
            derivative_media.transformation_json AS derivative_transformation_json,
            derivative_media.updated_at AS derivative_updated_at
        FROM source_rows
        LEFT JOIN media_assets AS derivative_media
            ON derivative_media.id = source_rows.derivative_id
        WHERE 1 = 1
    """
    if str(start_date or "").strip():
        query += " AND source_rows.entry_date >= ?\n"
        params.append(str(start_date).strip())
    if str(end_date or "").strip():
        query += " AND source_rows.entry_date <= ?\n"
        params.append(str(end_date).strip())
    query += """
        ORDER BY source_rows.entry_date, source_rows.entry_id, source_rows.source_role, source_rows.media_asset_id
    """
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return list(connection.execute(query, params))


def _derivative_output_stem(row: sqlite3.Row, long_edge: int) -> str:
    entry_date = str(row["entry_date"])
    size_token = f"sq{long_edge}"
    if str(row["source_role"]) != "associated":
        return f"Project365 Working Copy - {entry_date} - {size_token}"
    digest = hashlib.sha256(str(row["media_asset_id"]).encode("utf-8")).hexdigest()[:12]
    return f"Project365 Working Copy - {entry_date} - associated {size_token} - {digest}"


def _derivative_is_current(
    row: sqlite3.Row,
    output_path: Path,
    expected_transformation: dict[str, object],
) -> bool:
    if not row["derivative_media_asset_id"]:
        return False
    if row["derivative_status"] != "available":
        return False
    if str(row["derivative_storage_path"] or "") != str(output_path):
        return False
    if not output_path.exists():
        return False
    if not str(row["derivative_sha256"] or "").strip():
        return False
    source_updated_at = str(row["source_updated_at"] or "")
    derivative_updated_at = str(row["derivative_updated_at"] or "")
    if source_updated_at and derivative_updated_at and source_updated_at > derivative_updated_at:
        return False
    return _transformation_is_current(row["derivative_transformation_json"], expected_transformation)


def _transformation_is_current(
    existing_text: str,
    expected: dict[str, object],
) -> bool:
    try:
        existing = json.loads(existing_text or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(existing, dict):
        return False
    required_keys = ("source_media_asset_id", "format", "long_edge", "quality", "crop")
    for key in required_keys:
        if existing.get(key) != expected.get(key):
            return False
    optional_keys = ("source_sha256", "source_byte_size", "source_path")
    for key in optional_keys:
        if key in existing and existing.get(key) != expected.get(key):
            return False
    return True


def _has_valid_review_crop(value: str) -> bool:
    try:
        return _review_crop_from_transformation(value) is not None
    except (TypeError, ValueError):
        return False


def _mark_derivative_not_ready(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    derivative_id: str,
    reason: str,
) -> None:
    if not row["derivative_media_asset_id"]:
        return
    now = dt.datetime.now(dt.UTC).isoformat()
    transformation = _parse_json_object(row["derivative_transformation_json"])
    transformation["not_ready_reason"] = reason
    if reason == "missing_source_media":
        review_status = "needs_source"
    elif reason == "conversion_failed":
        review_status = "needs_conversion"
    else:
        review_status = "needs_crop"
    connection.execute(
        """
        UPDATE media_assets
        SET status = 'not_ready',
            review_status = ?,
            transformation_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (review_status, json.dumps(transformation, sort_keys=True), now, derivative_id),
    )


def _parse_json_object(value: object) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _convert_image_atomically(
    source_path: Path,
    output_path: Path,
    output_format: str,
    long_edge: int,
    quality: int,
    crop: dict[str, object],
) -> None:
    with tempfile.TemporaryDirectory(dir=output_path.parent) as temp_dir:
        temporary_output = Path(temp_dir) / output_path.name
        _convert_image(source_path, temporary_output, output_format, long_edge, quality, crop)
        if not temporary_output.exists():
            raise FileNotFoundError(f"Derivative conversion did not create output: {temporary_output}")
        temporary_output.replace(output_path)


def _convert_image(
    source_path: Path,
    output_path: Path,
    output_format: str,
    long_edge: int,
    quality: int,
    crop: dict[str, object],
) -> None:
    magick_path = shutil.which("magick")
    convert_source = source_path
    temp_context: tempfile.TemporaryDirectory[str] | None = None
    temp_context = tempfile.TemporaryDirectory()
    cropped_path = Path(temp_context.name) / "cropped.png"
    if magick_path:
        try:
            _crop_image_with_magick(magick_path, source_path, cropped_path, crop)
        except subprocess.CalledProcessError:
            if _crop_requires_magick(crop):
                raise
            _crop_image_with_sips(source_path, cropped_path, crop)
    else:
        if _crop_requires_magick(crop):
            raise RuntimeError("ImageMagick is required for rotated crops or crops that extend beyond the source image")
        try:
            _crop_image_with_sips(source_path, cropped_path, crop)
        except subprocess.CalledProcessError:
            raise
    convert_source = cropped_path
    if magick_path:
        try:
            _convert_image_with_magick(magick_path, convert_source, output_path, long_edge, quality)
            if temp_context:
                temp_context.cleanup()
            return
        except subprocess.CalledProcessError:
            pass
    command = [
        "sips",
        "-Z",
        str(long_edge),
        "-s",
        "format",
        output_format,
        "-s",
        "formatOptions",
        str(quality),
        str(convert_source),
        "--out",
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        if not magick_path:
            raise
        _convert_image_with_magick(magick_path, convert_source, output_path, long_edge, quality)
        if temp_context:
            temp_context.cleanup()
        return
    if output_format == "jpeg" and magick_path and _should_retry_black_derivative(convert_source, output_path):
        _convert_image_with_magick(magick_path, convert_source, output_path, long_edge, quality)
    if temp_context:
        temp_context.cleanup()


def _crop_image_with_sips(source_path: Path, output_path: Path, crop: dict[str, object]) -> None:
    x, y, size = _validated_crop_tuple(crop)
    subprocess.run(
        [
            "sips",
            "-c",
            str(size),
            str(size),
            "--cropOffset",
            str(y),
            str(x),
            str(source_path),
            "--out",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _crop_image_with_magick(
    magick_path: str,
    source_path: Path,
    output_path: Path,
    crop: dict[str, object],
) -> None:
    x, y, size = _validated_crop_tuple(crop)
    fill_color = _validated_fill_color(crop.get("fill_color", "#000000"))
    rotation_degrees = _validated_rotation_degrees(crop.get("rotation_degrees", 0))
    candidate_width = int(crop.get("candidate_width", 0) or 0)
    candidate_height = int(crop.get("candidate_height", 0) or 0)
    if candidate_width > 0 and candidate_height > 0 and not rotation_degrees:
        left_pad = max(0, -x)
        top_pad = max(0, -y)
        right_pad = max(0, x + size - candidate_width)
        bottom_pad = max(0, y + size - candidate_height)
        canvas_width = candidate_width + left_pad + right_pad
        canvas_height = candidate_height + top_pad + bottom_pad
        subprocess.run(
            [
                magick_path,
                "-size",
                f"{canvas_width}x{canvas_height}",
                f"xc:{fill_color}",
                str(source_path),
                "-auto-orient",
                "-alpha",
                "remove",
                "-alpha",
                "off",
                "-geometry",
                f"+{left_pad}+{top_pad}",
                "-composite",
                "-crop",
                _crop_geometry(size, x + left_pad, y + top_pad),
                "+repage",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    rotation_args = ["-distort", "SRT", _format_float(rotation_degrees)] if rotation_degrees else []
    subprocess.run(
        [
            magick_path,
            str(source_path),
            "-auto-orient",
            "-background",
            fill_color,
            "-alpha",
            "remove",
            "-alpha",
            "off",
            "-virtual-pixel",
            "background",
            *rotation_args,
            "-crop",
            _crop_geometry(size, x, y),
            "+repage",
            "-background",
            fill_color,
            "-gravity",
            "northwest",
            "-extent",
            f"{size}x{size}",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _review_crop_from_transformation(value: str) -> dict[str, object] | None:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return None
    crop = payload.get("review_crop")
    if not isinstance(crop, dict):
        return None
    x, y, size = _validated_crop_tuple(crop)
    candidate_width = int(crop.get("candidate_width", 0) or 0)
    candidate_height = int(crop.get("candidate_height", 0) or 0)
    result: dict[str, object] = {
        "x": x,
        "y": y,
        "size": size,
        "candidate_width": candidate_width,
        "candidate_height": candidate_height,
        "source": str(crop.get("source", "") or "manual"),
        "unit": "source_pixels",
        "shape": "square",
    }
    fill_color = str(crop.get("fill_color", "") or "").strip()
    if fill_color:
        result["fill_color"] = _validated_fill_color(fill_color)
    rotation_degrees = _validated_rotation_degrees(crop.get("rotation_degrees", 0))
    if rotation_degrees:
        result["rotation_degrees"] = rotation_degrees
    return result


def _validated_crop_tuple(crop: dict[str, object]) -> tuple[int, int, int]:
    try:
        x = int(crop["x"])
        y = int(crop["y"])
        size = int(crop["size"])
        candidate_width = int(crop.get("candidate_width", 0) or 0)
        candidate_height = int(crop.get("candidate_height", 0) or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid crop metadata") from exc
    if size <= 0:
        raise ValueError("Invalid crop metadata")
    if candidate_width and (x >= candidate_width or x + size <= 0):
        raise ValueError("Crop does not overlap source width")
    if candidate_height and (y >= candidate_height or y + size <= 0):
        raise ValueError("Crop does not overlap source height")
    return x, y, size


def _crop_requires_magick(crop: dict[str, object]) -> bool:
    return _crop_extends_source_bounds(crop) or bool(_validated_rotation_degrees(crop.get("rotation_degrees", 0)))


def _crop_extends_source_bounds(crop: dict[str, object]) -> bool:
    x, y, size = _validated_crop_tuple(crop)
    candidate_width = int(crop.get("candidate_width", 0) or 0)
    candidate_height = int(crop.get("candidate_height", 0) or 0)
    return (
        x < 0
        or y < 0
        or (candidate_width > 0 and x + size > candidate_width)
        or (candidate_height > 0 and y + size > candidate_height)
    )


def _crop_geometry(size: int, x: int, y: int) -> str:
    return f"{size}x{size}{x:+d}{y:+d}"


def _validated_fill_color(value: object) -> str:
    text = str(value or "").strip()
    if re.match(r"^#[0-9a-fA-F]{6}$", text):
        return text.lower()
    return "#000000"


def _validated_rotation_degrees(value: object) -> float:
    try:
        number = float(str(value or "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid crop rotation metadata") from exc
    if not -180 <= number <= 180:
        raise ValueError("Crop rotation must be between -180 and 180 degrees")
    return round(number, 3)


def _format_float(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _convert_image_with_magick(
    magick_path: str,
    source_path: Path,
    output_path: Path,
    long_edge: int,
    quality: int,
) -> None:
    subprocess.run(
        [
            magick_path,
            str(source_path),
            "-auto-orient",
            "-resize",
            f"{long_edge}x{long_edge}",
            "-quality",
            str(quality),
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _should_retry_black_derivative(source_path: Path, output_path: Path) -> bool:
    source_stats = _image_intensity_stats(source_path)
    output_stats = _image_intensity_stats(output_path)
    if source_stats is None or output_stats is None:
        return False
    source_mean, source_max = source_stats
    output_mean, output_max = output_stats
    source_has_visible_pixels = source_max > 0.02 or source_mean > 0.02
    output_is_black = output_max <= 0.001 and output_mean <= 0.001
    return source_has_visible_pixels and output_is_black


def _image_intensity_stats(path: Path) -> tuple[float, float] | None:
    magick_path = shutil.which("magick")
    if not magick_path:
        return None
    try:
        result = subprocess.run(
            [
                magick_path,
                "identify",
                "-format",
                "%[fx:mean] %[fx:maxima]",
                str(path),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _image_dimensions(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    width = 0
    height = 0
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pixelWidth:"):
            width = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("pixelHeight:"):
            height = int(stripped.split(":", 1)[1].strip())
    return width, height


def _report_row(
    row: sqlite3.Row,
    derivative_id: str,
    output_path: Path,
    output_format: str,
    long_edge: int,
    quality: int,
    crop: dict[str, object] | None,
    width: int | str,
    height: int | str,
    byte_size: int,
    sha256: str,
    status: str,
    error: str = "",
) -> dict[str, object]:
    return {
        "entry_id": row["entry_id"],
        "source_media_asset_id": row["media_asset_id"],
        "derivative_media_asset_id": derivative_id,
        "source_path": row["storage_path"],
        "derivative_path": str(output_path),
        "format": output_format,
        "long_edge": long_edge,
        "quality": quality,
        "crop": json.dumps(crop, sort_keys=True) if crop else "",
        "width": width,
        "height": height,
        "byte_size": byte_size,
        "sha256": sha256,
        "status": status,
        "error": error,
    }


def _write_report(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "entry_id",
        "source_media_asset_id",
        "derivative_media_asset_id",
        "source_path",
        "derivative_path",
        "format",
        "long_edge",
        "quality",
        "crop",
        "width",
        "height",
        "byte_size",
        "sha256",
        "status",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mime_type(output_format: str) -> str:
    if output_format == "jpeg":
        return "image/jpeg"
    return "image/heic"


if __name__ == "__main__":
    raise SystemExit(main())
