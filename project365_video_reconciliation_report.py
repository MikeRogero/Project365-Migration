#!/usr/bin/env python3
"""Generate metadata-only Project365 video reconciliation reports."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from project365_photo_library_index import (
    VIDEO_EXTENSIONS,
    default_legacy_video_sidecar_index_db,
    default_reexport_sidecar_index_db,
)


DEFAULT_REPORT_PREFIX = "video_metadata_reconciliation"
DEFAULT_DURATION_TOLERANCE_SECONDS = 1.0
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 2.0
DEFAULT_DATE_TOLERANCE_DAYS = 1
DEFAULT_MIN_SCORE = 45


@dataclass(frozen=True)
class VideoIndexRow:
    path: str
    root: str
    filename: str
    extension: str
    byte_size: int
    normalized_stem: str
    capture_timestamp: str
    capture_timestamp_source: str
    capture_datetime: dt.datetime | None
    dates: tuple[str, ...]
    has_gps: bool
    media_width: int | None
    media_height: int | None
    media_duration_seconds: float | None


@dataclass(frozen=True)
class VideoCandidate:
    new_video: VideoIndexRow
    legacy_video: VideoIndexRow
    score: int
    reasons: tuple[str, ...]
    timestamp_delta_seconds: float | None
    duration_delta_seconds: float | None
    cluster_id: int = 0
    cluster_new_count: int = 1
    cluster_legacy_count: int = 1


@dataclass(frozen=True)
class VideoReconciliationSummary:
    candidate_report_path: str
    summary_report_path: str
    html_report_path: str
    new_video_count: int
    legacy_video_count: int
    candidate_row_count: int
    new_videos_with_candidate_legacy_matches: int
    candidate_legacy_matches_4k_or_higher: int
    legacy_higher_resolution_missing_gps_new_has_gps: int
    ambiguous_cluster_count: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Project365 video sidecar indexes using metadata-only candidate matching."
    )
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument("--reexport-index", help="Re-export sidecar SQLite path.")
    parser.add_argument("--legacy-video-index", help="Legacy video sidecar SQLite path.")
    parser.add_argument("--report-dir", help="Report output directory.")
    parser.add_argument("--report-prefix", default=DEFAULT_REPORT_PREFIX)
    parser.add_argument(
        "--duration-tolerance-seconds",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE_SECONDS,
    )
    parser.add_argument(
        "--timestamp-tolerance-seconds",
        type=float,
        default=DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    )
    parser.add_argument(
        "--date-tolerance-days",
        type=int,
        default=DEFAULT_DATE_TOLERANCE_DAYS,
    )
    parser.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE)
    args = parser.parse_args()

    canonical_root = Path(args.canonical_root)
    summary = generate_video_reconciliation_report(
        reexport_db=Path(args.reexport_index)
        if args.reexport_index
        else default_reexport_sidecar_index_db(canonical_root),
        legacy_db=Path(args.legacy_video_index)
        if args.legacy_video_index
        else default_legacy_video_sidecar_index_db(canonical_root),
        report_dir=Path(args.report_dir)
        if args.report_dir
        else canonical_root / "exports" / "verification_reports",
        report_prefix=args.report_prefix,
        duration_tolerance_seconds=args.duration_tolerance_seconds,
        timestamp_tolerance_seconds=args.timestamp_tolerance_seconds,
        date_tolerance_days=args.date_tolerance_days,
        min_score=args.min_score,
    )
    print("Project365 video metadata reconciliation: PASS")
    print(f"Candidate report: {summary.candidate_report_path}")
    print(f"Summary report: {summary.summary_report_path}")
    print(f"Review page: {summary.html_report_path}")
    print(f"New videos: {summary.new_video_count}")
    print(f"Legacy videos: {summary.legacy_video_count}")
    print(f"Candidate rows: {summary.candidate_row_count}")
    print(f"New videos with candidate legacy matches: {summary.new_videos_with_candidate_legacy_matches}")
    print(f"Candidate legacy matches 4K-or-higher: {summary.candidate_legacy_matches_4k_or_higher}")
    print(
        "Legacy higher resolution, missing GPS while new has GPS: "
        f"{summary.legacy_higher_resolution_missing_gps_new_has_gps}"
    )
    print(f"Ambiguous clusters requiring review: {summary.ambiguous_cluster_count}")
    print("Classification: metadata-only candidate same-video; not deletion-safe")
    return 0


def generate_video_reconciliation_report(
    reexport_db: Path,
    legacy_db: Path,
    report_dir: Path,
    report_prefix: str = DEFAULT_REPORT_PREFIX,
    duration_tolerance_seconds: float = DEFAULT_DURATION_TOLERANCE_SECONDS,
    timestamp_tolerance_seconds: float = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS,
    min_score: int = DEFAULT_MIN_SCORE,
) -> VideoReconciliationSummary:
    if duration_tolerance_seconds < 0:
        raise ValueError("duration_tolerance_seconds must not be negative")
    if timestamp_tolerance_seconds < 0:
        raise ValueError("timestamp_tolerance_seconds must not be negative")
    if date_tolerance_days < 0:
        raise ValueError("date_tolerance_days must not be negative")
    if min_score < 0:
        raise ValueError("min_score must not be negative")
    if not reexport_db.exists():
        raise FileNotFoundError(f"Missing re-export sidecar index: {reexport_db}")
    if not legacy_db.exists():
        raise FileNotFoundError(f"Missing legacy video sidecar index: {legacy_db}")

    report_dir.mkdir(parents=True, exist_ok=True)
    new_videos = _load_video_rows(reexport_db)
    legacy_videos = _load_video_rows(legacy_db)
    candidates = _assign_ambiguous_clusters(
        _find_candidates(
            new_videos,
            legacy_videos,
            duration_tolerance_seconds=duration_tolerance_seconds,
            timestamp_tolerance_seconds=timestamp_tolerance_seconds,
            date_tolerance_days=date_tolerance_days,
            min_score=min_score,
        )
    )

    candidate_report_path = report_dir / f"{report_prefix}_candidates.csv"
    summary_report_path = report_dir / f"{report_prefix}_summary.json"
    html_report_path = report_dir / f"{report_prefix}_review.html"
    _write_candidate_report(candidate_report_path, candidates)
    summary = _summary(
        candidate_report_path=candidate_report_path,
        summary_report_path=summary_report_path,
        html_report_path=html_report_path,
        new_videos=new_videos,
        legacy_videos=legacy_videos,
        candidates=candidates,
    )
    _write_summary_report(
        summary_report_path,
        summary=summary,
        reexport_db=reexport_db,
        legacy_db=legacy_db,
        duration_tolerance_seconds=duration_tolerance_seconds,
        timestamp_tolerance_seconds=timestamp_tolerance_seconds,
        date_tolerance_days=date_tolerance_days,
        min_score=min_score,
    )
    _write_html_report(html_report_path, summary=summary, candidates=candidates)
    return summary


def _load_video_rows(index_db: Path) -> list[VideoIndexRow]:
    connection = _read_only_connection(index_db)
    try:
        connection.row_factory = sqlite3.Row
        if not _sqlite_table_exists(connection, "photo_library_files"):
            raise ValueError(f"Missing photo_library_files table in {index_db}")
        extension_placeholders = ", ".join("?" for _ in VIDEO_EXTENSIONS)
        rows = connection.execute(
            f"""
            SELECT
                path,
                root,
                filename,
                extension,
                byte_size,
                capture_timestamp,
                capture_timestamp_source,
                has_gps,
                media_width,
                media_height,
                media_duration_seconds
            FROM photo_library_files
            WHERE lower(extension) IN ({extension_placeholders})
            ORDER BY path
            """,
            tuple(sorted(VIDEO_EXTENSIONS)),
        ).fetchall()
        dates_by_path = _load_dates_by_path(connection)
        return [_video_row_from_sqlite(row, dates_by_path.get(str(row["path"]), ())) for row in rows]
    finally:
        connection.close()


def _read_only_connection(index_db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{index_db.resolve().as_uri()}?mode=ro", uri=True, timeout=60)


def _sqlite_table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _load_dates_by_path(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    if not _sqlite_table_exists(connection, "photo_library_dates"):
        return {}
    dates_by_path: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        """
        SELECT file_path, date
        FROM photo_library_dates
        WHERE date != ''
        ORDER BY file_path, date
        """
    ):
        dates_by_path[str(row[0])].add(str(row[1]))
    return {path: tuple(sorted(dates)) for path, dates in dates_by_path.items()}


def _video_row_from_sqlite(row: sqlite3.Row, dates: tuple[str, ...]) -> VideoIndexRow:
    capture_timestamp = str(row["capture_timestamp"] or "")
    capture_datetime = _parse_capture_datetime(capture_timestamp)
    date_values = set(dates)
    if capture_datetime is not None:
        date_values.add(capture_datetime.date().isoformat())
    return VideoIndexRow(
        path=str(row["path"]),
        root=str(row["root"] or ""),
        filename=str(row["filename"] or Path(str(row["path"])).name),
        extension=str(row["extension"] or Path(str(row["path"])).suffix).lower(),
        byte_size=int(row["byte_size"] or 0),
        normalized_stem=_normalized_stem(str(row["filename"] or Path(str(row["path"])).name)),
        capture_timestamp=capture_timestamp,
        capture_timestamp_source=str(row["capture_timestamp_source"] or ""),
        capture_datetime=capture_datetime,
        dates=tuple(sorted(date_values)),
        has_gps=bool(row["has_gps"]),
        media_width=_optional_int(row["media_width"]),
        media_height=_optional_int(row["media_height"]),
        media_duration_seconds=_optional_float(row["media_duration_seconds"]),
    )


def _parse_capture_datetime(value: str) -> dt.datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _normalized_stem(filename: str) -> str:
    stem = Path(filename).stem
    normalized = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower().strip()
    suffix_pattern = re.compile(
        r"(?:\s+copy|\s+duplicate|\s+dup|\s+alternate|\s+edited|\s+trimmed|\s*\(\d+\)|\s+-\s*\d+)$"
    )
    previous = ""
    while normalized and normalized != previous:
        previous = normalized
        normalized = suffix_pattern.sub("", normalized).strip()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _find_candidates(
    new_videos: list[VideoIndexRow],
    legacy_videos: list[VideoIndexRow],
    duration_tolerance_seconds: float,
    timestamp_tolerance_seconds: float,
    date_tolerance_days: int,
    min_score: int,
) -> list[VideoCandidate]:
    legacy_by_stem: dict[str, list[VideoIndexRow]] = defaultdict(list)
    legacy_by_date: dict[str, list[VideoIndexRow]] = defaultdict(list)
    for legacy in legacy_videos:
        if legacy.normalized_stem:
            legacy_by_stem[legacy.normalized_stem].append(legacy)
        for date_value in legacy.dates:
            legacy_by_date[date_value].append(legacy)

    candidates: list[VideoCandidate] = []
    for new_video in new_videos:
        possible: dict[str, VideoIndexRow] = {}
        if new_video.normalized_stem:
            for legacy in legacy_by_stem.get(new_video.normalized_stem, []):
                possible[legacy.path] = legacy
        for date_value in _date_neighborhood(new_video.dates, date_tolerance_days):
            for legacy in legacy_by_date.get(date_value, []):
                possible[legacy.path] = legacy
        for legacy in possible.values():
            candidate = _score_candidate(
                new_video,
                legacy,
                duration_tolerance_seconds=duration_tolerance_seconds,
                timestamp_tolerance_seconds=timestamp_tolerance_seconds,
            )
            if candidate.score >= min_score and _has_identity_anchor(candidate):
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item.new_video.path, -item.score, item.legacy_video.path))


def _date_neighborhood(dates: tuple[str, ...], tolerance_days: int) -> set[str]:
    values: set[str] = set()
    for value in dates:
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError:
            continue
        for offset in range(-tolerance_days, tolerance_days + 1):
            values.add((parsed + dt.timedelta(days=offset)).isoformat())
    return values


def _score_candidate(
    new_video: VideoIndexRow,
    legacy: VideoIndexRow,
    duration_tolerance_seconds: float,
    timestamp_tolerance_seconds: float,
) -> VideoCandidate:
    score = 0
    reasons: list[str] = []
    timestamp_delta = _timestamp_delta_seconds(new_video, legacy)
    duration_delta = _duration_delta_seconds(new_video, legacy)
    if new_video.normalized_stem and new_video.normalized_stem == legacy.normalized_stem:
        score += 35
        reasons.append("normalized_stem_match")
    if timestamp_delta is not None and timestamp_delta <= timestamp_tolerance_seconds:
        score += 30
        reasons.append("capture_timestamp_within_tolerance")
    elif set(new_video.dates) & set(legacy.dates):
        score += 15
        reasons.append("capture_date_match")
    if duration_delta is not None and duration_delta <= duration_tolerance_seconds:
        score += 25
        reasons.append("duration_within_tolerance")
    if _same_dimensions(new_video, legacy):
        score += 20
        reasons.append("dimensions_match")
    elif _same_orientation(new_video, legacy):
        score += 8
        reasons.append("orientation_match")
    if new_video.extension and new_video.extension == legacy.extension:
        score += 5
        reasons.append("extension_match")
    if new_video.has_gps == legacy.has_gps:
        score += 3
        reasons.append("gps_presence_match")
    if new_video.root and new_video.root == legacy.root:
        score += 2
        reasons.append("source_root_match")
    return VideoCandidate(
        new_video=new_video,
        legacy_video=legacy,
        score=score,
        reasons=tuple(reasons),
        timestamp_delta_seconds=timestamp_delta,
        duration_delta_seconds=duration_delta,
    )


def _has_identity_anchor(candidate: VideoCandidate) -> bool:
    reasons = set(candidate.reasons)
    if "normalized_stem_match" in reasons or "capture_timestamp_within_tolerance" in reasons:
        return True
    return {
        "capture_date_match",
        "duration_within_tolerance",
        "dimensions_match",
    }.issubset(reasons)


def _timestamp_delta_seconds(new_video: VideoIndexRow, legacy: VideoIndexRow) -> float | None:
    if new_video.capture_datetime is None or legacy.capture_datetime is None:
        return None
    first = new_video.capture_datetime
    second = legacy.capture_datetime
    if first.tzinfo is None and second.tzinfo is not None:
        second = second.replace(tzinfo=None)
    elif first.tzinfo is not None and second.tzinfo is None:
        first = first.replace(tzinfo=None)
    return abs((first - second).total_seconds())


def _duration_delta_seconds(new_video: VideoIndexRow, legacy: VideoIndexRow) -> float | None:
    if new_video.media_duration_seconds is None or legacy.media_duration_seconds is None:
        return None
    return abs(new_video.media_duration_seconds - legacy.media_duration_seconds)


def _same_dimensions(new_video: VideoIndexRow, legacy: VideoIndexRow) -> bool:
    new_dims = _dimension_pair(new_video)
    legacy_dims = _dimension_pair(legacy)
    return bool(new_dims and legacy_dims and new_dims == legacy_dims)


def _same_orientation(new_video: VideoIndexRow, legacy: VideoIndexRow) -> bool:
    new_dims = _dimension_pair(new_video)
    legacy_dims = _dimension_pair(legacy)
    if not new_dims or not legacy_dims:
        return False
    return (new_dims[0] >= new_dims[1]) == (legacy_dims[0] >= legacy_dims[1])


def _dimension_pair(video: VideoIndexRow) -> tuple[int, int] | None:
    if video.media_width is None or video.media_height is None:
        return None
    return tuple(sorted((video.media_width, video.media_height), reverse=True))


def _resolution_pixels(video: VideoIndexRow) -> int:
    if video.media_width is None or video.media_height is None:
        return 0
    return video.media_width * video.media_height


def _is_4k_or_higher(video: VideoIndexRow) -> bool:
    dims = _dimension_pair(video)
    return bool(dims and dims[0] >= 3840 and dims[1] >= 2160)


def _legacy_higher_resolution_missing_gps_new_has_gps(candidate: VideoCandidate) -> bool:
    return (
        _resolution_pixels(candidate.legacy_video) > _resolution_pixels(candidate.new_video)
        and not candidate.legacy_video.has_gps
        and candidate.new_video.has_gps
    )


def _assign_ambiguous_clusters(candidates: list[VideoCandidate]) -> list[VideoCandidate]:
    if not candidates:
        return []
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(first: str, second: str) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    for candidate in candidates:
        union(f"new:{candidate.new_video.path}", f"legacy:{candidate.legacy_video.path}")

    grouped: dict[str, list[VideoCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[find(f"new:{candidate.new_video.path}")].append(candidate)

    cluster_numbers: dict[str, int] = {}
    next_cluster = 1
    enriched: list[VideoCandidate] = []
    for root, group in grouped.items():
        new_paths = {candidate.new_video.path for candidate in group}
        legacy_paths = {candidate.legacy_video.path for candidate in group}
        ambiguous = len(new_paths) != 1 or len(legacy_paths) != 1
        if ambiguous:
            cluster_numbers[root] = next_cluster
            next_cluster += 1
        cluster_id = cluster_numbers.get(root, 0)
        for candidate in group:
            enriched.append(
                VideoCandidate(
                    new_video=candidate.new_video,
                    legacy_video=candidate.legacy_video,
                    score=candidate.score,
                    reasons=candidate.reasons,
                    timestamp_delta_seconds=candidate.timestamp_delta_seconds,
                    duration_delta_seconds=candidate.duration_delta_seconds,
                    cluster_id=cluster_id,
                    cluster_new_count=len(new_paths),
                    cluster_legacy_count=len(legacy_paths),
                )
            )
    return sorted(enriched, key=lambda item: (item.new_video.path, -item.score, item.legacy_video.path))


def _summary(
    candidate_report_path: Path,
    summary_report_path: Path,
    html_report_path: Path,
    new_videos: list[VideoIndexRow],
    legacy_videos: list[VideoIndexRow],
    candidates: list[VideoCandidate],
) -> VideoReconciliationSummary:
    return VideoReconciliationSummary(
        candidate_report_path=str(candidate_report_path),
        summary_report_path=str(summary_report_path),
        html_report_path=str(html_report_path),
        new_video_count=len(new_videos),
        legacy_video_count=len(legacy_videos),
        candidate_row_count=len(candidates),
        new_videos_with_candidate_legacy_matches=len({candidate.new_video.path for candidate in candidates}),
        candidate_legacy_matches_4k_or_higher=len(
            {candidate.legacy_video.path for candidate in candidates if _is_4k_or_higher(candidate.legacy_video)}
        ),
        legacy_higher_resolution_missing_gps_new_has_gps=sum(
            1 for candidate in candidates if _legacy_higher_resolution_missing_gps_new_has_gps(candidate)
        ),
        ambiguous_cluster_count=len({candidate.cluster_id for candidate in candidates if candidate.cluster_id}),
    )


def _write_candidate_report(path: Path, candidates: list[VideoCandidate]) -> None:
    fieldnames = [
        "classification",
        "deletion_safety",
        "ambiguous_cluster",
        "cluster_new_count",
        "cluster_legacy_count",
        "score",
        "match_reasons",
        "new_path",
        "legacy_path",
        "new_root",
        "legacy_root",
        "new_filename",
        "legacy_filename",
        "new_extension",
        "legacy_extension",
        "new_normalized_stem",
        "legacy_normalized_stem",
        "new_capture_timestamp",
        "legacy_capture_timestamp",
        "timestamp_delta_seconds",
        "new_dates",
        "legacy_dates",
        "new_duration_seconds",
        "legacy_duration_seconds",
        "duration_delta_seconds",
        "new_width",
        "new_height",
        "legacy_width",
        "legacy_height",
        "new_resolution_pixels",
        "legacy_resolution_pixels",
        "new_has_gps",
        "legacy_has_gps",
        "legacy_is_4k_or_higher",
        "legacy_higher_resolution_missing_gps_new_has_gps",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(_candidate_csv_row(candidate))


def _candidate_csv_row(candidate: VideoCandidate) -> dict[str, object]:
    new_video = candidate.new_video
    legacy = candidate.legacy_video
    return {
        "classification": "candidate_same_video",
        "deletion_safety": "not_deletion_safe_metadata_only",
        "ambiguous_cluster": candidate.cluster_id,
        "cluster_new_count": candidate.cluster_new_count,
        "cluster_legacy_count": candidate.cluster_legacy_count,
        "score": candidate.score,
        "match_reasons": ";".join(candidate.reasons),
        "new_path": new_video.path,
        "legacy_path": legacy.path,
        "new_root": new_video.root,
        "legacy_root": legacy.root,
        "new_filename": new_video.filename,
        "legacy_filename": legacy.filename,
        "new_extension": new_video.extension,
        "legacy_extension": legacy.extension,
        "new_normalized_stem": new_video.normalized_stem,
        "legacy_normalized_stem": legacy.normalized_stem,
        "new_capture_timestamp": new_video.capture_timestamp,
        "legacy_capture_timestamp": legacy.capture_timestamp,
        "timestamp_delta_seconds": _format_float(candidate.timestamp_delta_seconds),
        "new_dates": ";".join(new_video.dates),
        "legacy_dates": ";".join(legacy.dates),
        "new_duration_seconds": _format_float(new_video.media_duration_seconds),
        "legacy_duration_seconds": _format_float(legacy.media_duration_seconds),
        "duration_delta_seconds": _format_float(candidate.duration_delta_seconds),
        "new_width": new_video.media_width or "",
        "new_height": new_video.media_height or "",
        "legacy_width": legacy.media_width or "",
        "legacy_height": legacy.media_height or "",
        "new_resolution_pixels": _resolution_pixels(new_video) or "",
        "legacy_resolution_pixels": _resolution_pixels(legacy) or "",
        "new_has_gps": int(new_video.has_gps),
        "legacy_has_gps": int(legacy.has_gps),
        "legacy_is_4k_or_higher": int(_is_4k_or_higher(legacy)),
        "legacy_higher_resolution_missing_gps_new_has_gps": int(
            _legacy_higher_resolution_missing_gps_new_has_gps(candidate)
        ),
    }


def _format_float(value: float | None) -> str:
    if value is None:
        return ""
    if math.isclose(value, round(value), abs_tol=0.000001):
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _write_summary_report(
    path: Path,
    summary: VideoReconciliationSummary,
    reexport_db: Path,
    legacy_db: Path,
    duration_tolerance_seconds: float,
    timestamp_tolerance_seconds: float,
    date_tolerance_days: int,
    min_score: int,
) -> None:
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "classification_note": "metadata-only candidate same-video; not deletion-safe",
        "reexport_db": str(reexport_db),
        "legacy_db": str(legacy_db),
        "candidate_report_path": summary.candidate_report_path,
        "summary_report_path": summary.summary_report_path,
        "html_report_path": summary.html_report_path,
        "duration_tolerance_seconds": duration_tolerance_seconds,
        "timestamp_tolerance_seconds": timestamp_tolerance_seconds,
        "date_tolerance_days": date_tolerance_days,
        "min_score": min_score,
        "new_video_count": summary.new_video_count,
        "legacy_video_count": summary.legacy_video_count,
        "candidate_row_count": summary.candidate_row_count,
        "new_videos_with_candidate_legacy_matches": summary.new_videos_with_candidate_legacy_matches,
        "candidate_legacy_matches_4k_or_higher": summary.candidate_legacy_matches_4k_or_higher,
        "legacy_higher_resolution_missing_gps_new_has_gps": (
            summary.legacy_higher_resolution_missing_gps_new_has_gps
        ),
        "ambiguous_cluster_count": summary.ambiguous_cluster_count,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_html_report(
    path: Path,
    summary: VideoReconciliationSummary,
    candidates: list[VideoCandidate],
) -> None:
    rows = [_candidate_csv_row(candidate) for candidate in candidates]
    payload = json.dumps({"summary": summary.__dict__, "rows": rows}, sort_keys=True).replace("</", "<\\/")
    path.write_text(_html_report_document(payload), encoding="utf-8")


def _html_report_document(payload_json: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 Video Candidate Review</title>
<style>
:root {{
  --bg: #f7f4ee;
  --ink: #1f2933;
  --muted: #667085;
  --line: #d8d2c5;
  --panel: #fffdf8;
  --accent: #17695d;
  --accent-soft: #dceee8;
  --warn: #9f580a;
  --warn-soft: #fff0cf;
  --danger: #9b2c2c;
  --danger-soft: #ffe2dc;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
button, input, select {{ font: inherit; }}
.app {{ min-height: 100vh; display: grid; grid-template-rows: auto auto 1fr; }}
header {{ padding: 18px 24px 14px; border-bottom: 1px solid var(--line); background: #fbfaf6; }}
h1 {{ margin: 0; font-size: 22px; line-height: 1.2; letter-spacing: 0; }}
.subtitle {{ margin-top: 6px; color: var(--muted); font-size: 13px; }}
.metrics {{ display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 8px; margin-top: 14px; }}
.metric {{ border: 1px solid var(--line); background: var(--panel); border-radius: 6px; padding: 10px 12px; min-width: 0; }}
.metric-value {{ font-size: 22px; font-weight: 700; line-height: 1.1; }}
.metric-label {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
.toolbar {{ display: grid; grid-template-columns: minmax(180px, 1fr) auto auto; gap: 10px; align-items: center; padding: 12px 24px; border-bottom: 1px solid var(--line); background: #fffaf1; }}
.search {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; background: #fff; color: var(--ink); }}
.filters {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.chip {{ border: 1px solid var(--line); border-radius: 999px; padding: 7px 10px; background: #fff; color: var(--ink); cursor: pointer; }}
.chip[aria-pressed="true"] {{ border-color: var(--accent); background: var(--accent-soft); color: #0b433b; }}
.sort {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px 9px; background: #fff; color: var(--ink); }}
.main {{ display: grid; grid-template-columns: minmax(300px, 420px) 1fr; min-height: 0; }}
.list-pane {{ border-right: 1px solid var(--line); min-height: 0; overflow: auto; background: #fbfaf6; }}
.list-header {{ position: sticky; top: 0; z-index: 1; padding: 10px 14px; border-bottom: 1px solid var(--line); background: #fbfaf6; color: var(--muted); font-size: 12px; }}
.candidate-button {{ width: 100%; display: grid; gap: 6px; padding: 12px 14px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: inherit; text-align: left; cursor: pointer; }}
.candidate-button:hover, .candidate-button.is-selected {{ background: #eef7f2; }}
.candidate-title {{ display: flex; gap: 8px; align-items: center; min-width: 0; }}
.candidate-name {{ font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.badges {{ display: flex; gap: 5px; flex-wrap: wrap; }}
.badge {{ display: inline-flex; align-items: center; min-height: 20px; padding: 2px 7px; border-radius: 999px; background: #ece7dc; color: #344054; font-size: 11px; white-space: nowrap; }}
.badge.warn {{ background: var(--warn-soft); color: var(--warn); }}
.badge.danger {{ background: var(--danger-soft); color: var(--danger); }}
.candidate-meta {{ color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.detail-pane {{ min-width: 0; min-height: 0; overflow: auto; padding: 18px 22px 26px; }}
.detail-top {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; margin-bottom: 12px; }}
.detail-title {{ font-size: 19px; font-weight: 700; overflow-wrap: anywhere; }}
.safety {{ border: 1px solid var(--warn); background: var(--warn-soft); color: #693a05; border-radius: 6px; padding: 8px 10px; font-size: 12px; max-width: 360px; }}
.compare-grid {{ display: grid; grid-template-columns: repeat(2, minmax(260px, 1fr)); gap: 14px; }}
.panel {{ border: 1px solid var(--line); border-radius: 6px; background: var(--panel); min-width: 0; }}
.panel h2 {{ margin: 0; padding: 12px 14px; border-bottom: 1px solid var(--line); font-size: 15px; letter-spacing: 0; }}
.facts {{ display: grid; padding: 6px 14px 14px; }}
.fact {{ display: grid; grid-template-columns: 132px minmax(0, 1fr); gap: 12px; padding: 7px 0; border-bottom: 1px solid #eee7dc; }}
.fact:last-child {{ border-bottom: 0; }}
.fact-label {{ color: var(--muted); font-size: 12px; }}
.fact-value {{ overflow-wrap: anywhere; font-size: 13px; }}
.reason-row {{ margin-top: 14px; display: flex; flex-wrap: wrap; gap: 6px; }}
.empty {{ padding: 36px; color: var(--muted); text-align: center; }}
@media (max-width: 900px) {{
  .metrics {{ grid-template-columns: repeat(2, minmax(130px, 1fr)); }}
  .toolbar {{ grid-template-columns: 1fr; }}
  .main {{ grid-template-columns: 1fr; }}
  .list-pane {{ max-height: 38vh; border-right: 0; border-bottom: 1px solid var(--line); }}
  .compare-grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>Project365 Video Candidate Review</h1>
    <div class="subtitle">Metadata-only candidates from local sidecar indexes. Nothing here is deletion-safe without review.</div>
    <div id="metrics" class="metrics"></div>
  </header>
  <section class="toolbar" aria-label="Review controls">
    <input id="searchInput" class="search" type="search" placeholder="Search filenames, paths, dates, reasons">
    <div class="filters" role="group" aria-label="Filters">
      <button class="chip" type="button" data-filter="all" aria-pressed="true">All</button>
      <button class="chip" type="button" data-filter="ambiguous" aria-pressed="false">Ambiguous</button>
      <button class="chip" type="button" data-filter="gpsRisk" aria-pressed="false">GPS risk</button>
      <button class="chip" type="button" data-filter="legacy4k" aria-pressed="false">Legacy 4K+</button>
    </div>
    <select id="sortSelect" class="sort" aria-label="Sort candidates">
      <option value="score">Score high to low</option>
      <option value="ambiguous">Ambiguous first</option>
      <option value="filename">Filename</option>
    </select>
  </section>
  <main class="main">
    <aside class="list-pane">
      <div id="listHeader" class="list-header"></div>
      <div id="candidateList"></div>
    </aside>
    <section id="detailPane" class="detail-pane"></section>
  </main>
</div>
<script id="reportData" type="application/json">{payload_json}</script>
<script>
const data = JSON.parse(document.getElementById("reportData").textContent);
const rows = data.rows || [];
let activeFilter = "all";
let selectedIndex = 0;
let visibleRows = [];

const metrics = [
  ["New videos", data.summary.new_video_count],
  ["Legacy videos", data.summary.legacy_video_count],
  ["Candidate rows", data.summary.candidate_row_count],
  ["Matched new videos", data.summary.new_videos_with_candidate_legacy_matches],
  ["Ambiguous clusters", data.summary.ambiguous_cluster_count],
  ["GPS/resolution risk", data.summary.legacy_higher_resolution_missing_gps_new_has_gps]
];

function formatNumber(value) {{
  return Number(value || 0).toLocaleString();
}}

function renderMetrics() {{
  const container = document.getElementById("metrics");
  container.replaceChildren(...metrics.map(([label, value]) => {{
    const item = document.createElement("div");
    item.className = "metric";
    const number = document.createElement("div");
    number.className = "metric-value";
    number.textContent = formatNumber(value);
    const text = document.createElement("div");
    text.className = "metric-label";
    text.textContent = label;
    item.append(number, text);
    return item;
  }}));
}}

function matchesFilter(row) {{
  if (activeFilter === "ambiguous") return row.ambiguous_cluster !== "0";
  if (activeFilter === "gpsRisk") return row.legacy_higher_resolution_missing_gps_new_has_gps === "1";
  if (activeFilter === "legacy4k") return row.legacy_is_4k_or_higher === "1";
  return true;
}}

function matchesSearch(row, query) {{
  if (!query) return true;
  const haystack = [
    row.new_filename, row.legacy_filename, row.new_path, row.legacy_path,
    row.new_dates, row.legacy_dates, row.match_reasons, row.score
  ].join(" ").toLowerCase();
  return haystack.includes(query);
}}

function sortedRows(input) {{
  const mode = document.getElementById("sortSelect").value;
  return [...input].sort((a, b) => {{
    if (mode === "filename") return `${{a.new_filename}} ${{a.legacy_filename}}`.localeCompare(`${{b.new_filename}} ${{b.legacy_filename}}`);
    if (mode === "ambiguous") return Number(b.ambiguous_cluster !== "0") - Number(a.ambiguous_cluster !== "0") || Number(b.score) - Number(a.score);
    return Number(b.score) - Number(a.score) || a.new_filename.localeCompare(b.new_filename);
  }});
}}

function currentRows() {{
  const query = document.getElementById("searchInput").value.trim().toLowerCase();
  return sortedRows(rows.filter(row => matchesFilter(row) && matchesSearch(row, query)));
}}

function badge(text, kind = "") {{
  const el = document.createElement("span");
  el.className = `badge ${{kind}}`.trim();
  el.textContent = text;
  return el;
}}

function renderList() {{
  visibleRows = currentRows();
  if (selectedIndex >= visibleRows.length) selectedIndex = Math.max(0, visibleRows.length - 1);
  document.getElementById("listHeader").textContent = `${{formatNumber(visibleRows.length)}} of ${{formatNumber(rows.length)}} candidate rows`;
  const list = document.getElementById("candidateList");
  if (!visibleRows.length) {{
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No candidates match the current view.";
    list.replaceChildren(empty);
    renderDetail(null);
    return;
  }}
  list.replaceChildren(...visibleRows.map((row, index) => {{
    const button = document.createElement("button");
    button.type = "button";
    button.className = `candidate-button${{index === selectedIndex ? " is-selected" : ""}}`;
    button.addEventListener("click", () => {{
      selectedIndex = index;
      renderList();
    }});

    const title = document.createElement("div");
    title.className = "candidate-title";
    const name = document.createElement("div");
    name.className = "candidate-name";
    name.textContent = row.new_filename || row.new_path;
    title.append(name);

    const badges = document.createElement("div");
    badges.className = "badges";
    badges.append(badge(`Score ${{row.score}}`));
    if (row.ambiguous_cluster !== "0") badges.append(badge(`Cluster ${{row.ambiguous_cluster}}`, "warn"));
    if (row.legacy_is_4k_or_higher === "1") badges.append(badge("Legacy 4K+", "warn"));
    if (row.legacy_higher_resolution_missing_gps_new_has_gps === "1") badges.append(badge("GPS risk", "danger"));

    const meta = document.createElement("div");
    meta.className = "candidate-meta";
    meta.textContent = `${{row.legacy_filename || row.legacy_path}} · ${{row.new_dates || row.legacy_dates || "no date"}}`;
    button.append(title, badges, meta);
    return button;
  }}));
  renderDetail(visibleRows[selectedIndex]);
}}

function fact(label, value) {{
  const row = document.createElement("div");
  row.className = "fact";
  const key = document.createElement("div");
  key.className = "fact-label";
  key.textContent = label;
  const val = document.createElement("div");
  val.className = "fact-value";
  val.textContent = value || "—";
  row.append(key, val);
  return row;
}}

function panel(title, fields) {{
  const section = document.createElement("section");
  section.className = "panel";
  const heading = document.createElement("h2");
  heading.textContent = title;
  const facts = document.createElement("div");
  facts.className = "facts";
  facts.append(...fields.map(([label, value]) => fact(label, value)));
  section.append(heading, facts);
  return section;
}}

function renderDetail(row) {{
  const detail = document.getElementById("detailPane");
  if (!row) {{
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No candidate selected.";
    detail.replaceChildren(empty);
    return;
  }}
  const top = document.createElement("div");
  top.className = "detail-top";
  const title = document.createElement("div");
  title.className = "detail-title";
  title.textContent = row.new_filename || row.new_path;
  const safety = document.createElement("div");
  safety.className = "safety";
  safety.textContent = "Candidate same-video only. Metadata does not establish a deletion-safe duplicate.";
  top.append(title, safety);

  const compare = document.createElement("div");
  compare.className = "compare-grid";
  compare.append(
    panel("New re-export", [
      ["Filename", row.new_filename],
      ["Path", row.new_path],
      ["Root", row.new_root],
      ["Capture time", row.new_capture_timestamp],
      ["Dates", row.new_dates],
      ["Duration", row.new_duration_seconds],
      ["Dimensions", dimensions(row.new_width, row.new_height)],
      ["GPS present", yesNo(row.new_has_gps)],
      ["Extension", row.new_extension]
    ]),
    panel("Legacy candidate", [
      ["Filename", row.legacy_filename],
      ["Path", row.legacy_path],
      ["Root", row.legacy_root],
      ["Capture time", row.legacy_capture_timestamp],
      ["Dates", row.legacy_dates],
      ["Duration", row.legacy_duration_seconds],
      ["Dimensions", dimensions(row.legacy_width, row.legacy_height)],
      ["GPS present", yesNo(row.legacy_has_gps)],
      ["Extension", row.legacy_extension]
    ])
  );

  const reasons = document.createElement("div");
  reasons.className = "reason-row";
  reasons.append(badge(`Score ${{row.score}}`));
  for (const reason of (row.match_reasons || "").split(";").filter(Boolean)) reasons.append(badge(reason));
  if (row.ambiguous_cluster !== "0") reasons.append(badge(`Ambiguous cluster ${{row.ambiguous_cluster}}`, "warn"));
  if (row.legacy_higher_resolution_missing_gps_new_has_gps === "1") reasons.append(badge("Legacy higher-res without GPS", "danger"));
  detail.replaceChildren(top, compare, reasons);
}}

function dimensions(width, height) {{
  return width && height ? `${{width}} × ${{height}}` : "";
}}

function yesNo(value) {{
  return value === "1" ? "Yes" : "No";
}}

document.querySelectorAll(".chip").forEach(button => {{
  button.addEventListener("click", () => {{
    activeFilter = button.dataset.filter || "all";
    document.querySelectorAll(".chip").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
    selectedIndex = 0;
    renderList();
  }});
}});
document.getElementById("searchInput").addEventListener("input", () => {{ selectedIndex = 0; renderList(); }});
document.getElementById("sortSelect").addEventListener("change", () => {{ selectedIndex = 0; renderList(); }});
document.addEventListener("keydown", event => {{
  if (event.target && ["INPUT", "SELECT"].includes(event.target.tagName)) return;
  if (event.key === "ArrowDown" || event.key.toLowerCase() === "j") {{
    selectedIndex = Math.min(selectedIndex + 1, Math.max(0, visibleRows.length - 1));
    renderList();
    event.preventDefault();
  }}
  if (event.key === "ArrowUp" || event.key.toLowerCase() === "k") {{
    selectedIndex = Math.max(selectedIndex - 1, 0);
    renderList();
    event.preventDefault();
  }}
}});

renderMetrics();
renderList();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
