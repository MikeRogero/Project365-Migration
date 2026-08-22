#!/usr/bin/env python3
"""Build a local original-photo candidate index and review queue."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project365_crop_align import _mean_squared_error, _resize_grayscale, load_image, suggest_crop


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff"}


@dataclass(frozen=True)
class OriginalMatchSummary:
    index_path: str
    review_queue_path: str
    candidate_count: int
    proposed_match_count: int
    auto_selected_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Index and review Project365 original-photo matches.")
    parser.add_argument("--canonical-root", default="Project365Canonical")
    parser.add_argument(
        "--candidate-root",
        action="append",
        default=[],
        help="Local folder containing possible original images. May be repeated.",
    )
    parser.add_argument(
        "--report-dir",
        default="Project365Canonical/exports/verification_reports",
    )
    parser.add_argument(
        "--auto-accept",
        action="store_true",
        help="Copy high-confidence exact matches into canonical media/originals.",
    )
    parser.add_argument(
        "--broad-visual-scan",
        action="store_true",
        help="When hash/date evidence is absent, visually shortlist candidate originals for review.",
    )
    parser.add_argument(
        "--visual-prefilter-limit",
        type=int,
        default=5,
        help="Maximum visually similar candidates to crop-score for each unmatched export.",
    )
    parser.add_argument(
        "--visual-prefilter-threshold",
        type=float,
        default=1500.0,
        help="Maximum thumbnail mean-squared-error allowed for broad visual candidates.",
    )
    args = parser.parse_args()

    summary = build_original_match_index(
        canonical_root=Path(args.canonical_root),
        candidate_roots=[Path(path) for path in args.candidate_root],
        report_dir=Path(args.report_dir),
        auto_accept=args.auto_accept,
        broad_visual_scan=args.broad_visual_scan,
        visual_prefilter_limit=args.visual_prefilter_limit,
        visual_prefilter_threshold=args.visual_prefilter_threshold,
    )
    print("Project365 original matching: PASS")
    print(f"Index: {summary.index_path}")
    print(f"Review queue: {summary.review_queue_path}")
    print(f"Candidates: {summary.candidate_count}")
    print(f"Proposed matches: {summary.proposed_match_count}")
    print(f"Auto-selected: {summary.auto_selected_count}")
    return 0


def build_original_match_index(
    canonical_root: Path,
    candidate_roots: list[Path],
    report_dir: Path,
    auto_accept: bool = False,
    broad_visual_scan: bool = False,
    visual_prefilter_limit: int = 5,
    visual_prefilter_threshold: float = 1500.0,
) -> OriginalMatchSummary:
    db_path = canonical_root / "canonical.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing canonical database: {db_path}")
    for root in candidate_roots:
        if not root.exists():
            raise FileNotFoundError(f"Missing candidate root: {root}")

    report_dir.mkdir(parents=True, exist_ok=True)
    index_path = report_dir / "original_photo_candidate_index.csv"
    review_queue_path = report_dir / "original_photo_review_queue.csv"
    candidates = _scan_candidates(candidate_roots)
    exports = _load_project365_exports(db_path)
    proposed_rows = _propose_matches(
        exports,
        candidates,
        broad_visual_scan=broad_visual_scan,
        visual_prefilter_limit=visual_prefilter_limit,
        visual_prefilter_threshold=visual_prefilter_threshold,
    )
    auto_selected = 0
    if auto_accept:
        auto_selected = _auto_select_high_confidence(
            canonical_root,
            db_path,
            proposed_rows,
        )
    _write_csv(index_path, _candidate_rows(candidates), _candidate_fieldnames())
    _write_csv(review_queue_path, proposed_rows, _review_fieldnames())
    return OriginalMatchSummary(
        index_path=str(index_path),
        review_queue_path=str(review_queue_path),
        candidate_count=len(candidates),
        proposed_match_count=len(proposed_rows),
        auto_selected_count=auto_selected,
    )


def _scan_candidates(candidate_roots: list[Path]) -> list[dict[str, object]]:
    rows = []
    for root in candidate_roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            rows.append(
                {
                    "path": str(path),
                    "root": str(root),
                    "filename": path.name,
                    "sha256": _sha256_file(path),
                    "byte_size": path.stat().st_size,
                    "date_hint": _date_hint(path),
                }
            )
    return rows


def _load_project365_exports(db_path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    entries.id AS entry_id,
                    entries.entry_date,
                    media_assets.id AS media_asset_id,
                    media_assets.storage_path,
                    media_assets.sha256
                FROM entries
                JOIN media_assets
                    ON media_assets.entry_id = entries.id
                    AND media_assets.role = 'project365_export_png'
                ORDER BY entries.entry_date, entries.id
                """
            )
        ]
    finally:
        connection.close()


def _propose_matches(
    exports: list[dict[str, object]],
    candidates: list[dict[str, object]],
    broad_visual_scan: bool = False,
    visual_prefilter_limit: int = 5,
    visual_prefilter_threshold: float = 1500.0,
) -> list[dict[str, object]]:
    rows = []
    visual_samples = _load_visual_samples(candidates) if broad_visual_scan else {}
    for export in exports:
        matching_candidates = [
            candidate
            for candidate in candidates
            if _candidate_could_match(export, candidate)
        ]
        visual_scan = False
        if not matching_candidates and broad_visual_scan:
            matching_candidates = _visual_prefilter_candidates(
                export,
                candidates,
                visual_samples,
                visual_prefilter_limit,
                visual_prefilter_threshold,
            )
            visual_scan = True
        if not matching_candidates:
            rows.append(_review_row(export, None, "no_candidate", 0.0, "", "fallback"))
            continue
        scored = [
            _score_candidate(export, candidate, visual_scan=visual_scan)
            for candidate in matching_candidates
        ]
        scored.sort(key=lambda row: row["confidence_score"], reverse=True)
        best = scored[0]
        tied = [
            row
            for row in scored
            if row["confidence_score"] == best["confidence_score"]
        ]
        if len(tied) > 1:
            for row in tied:
                row["decision"] = "review_ambiguous"
                row["match_status"] = "ambiguous"
                rows.append(row)
        else:
            best["decision"] = "auto_accept" if best["decision"] == "auto_accept" else "review"
            rows.append(best)
    return rows


def _score_candidate(
    export: dict[str, object],
    candidate: dict[str, object],
    visual_scan: bool = False,
) -> dict[str, object]:
    score = 0.0
    evidence = []
    fallback_candidate = _is_project365_fallback_candidate(candidate)
    if visual_scan:
        evidence.append("visual_scan")
    if candidate["sha256"] == export["sha256"]:
        score += 100.0
        evidence.append("same_sha256")
    if candidate.get("date_hint") == export["entry_date"]:
        score += 20.0
        evidence.append("date_hint")
    crop_score = ""
    crop_confidence = ""
    if candidate["sha256"] != export["sha256"]:
        try:
            suggestion = suggest_crop(Path(str(export["storage_path"])), Path(str(candidate["path"])))
            crop_score = f"{suggestion.score:.4f}"
            crop_confidence = suggestion.confidence
            if suggestion.confidence == "high":
                score += 40.0
                evidence.append("crop_high")
            elif suggestion.confidence == "medium":
                score += 15.0
                evidence.append("crop_medium")
        except Exception as exc:  # noqa: BLE001 - local review report should capture failed image reads.
            crop_score = f"error:{type(exc).__name__}"
            crop_confidence = "error"
    if fallback_candidate:
        evidence.append("project365_fallback_source")
    confidence = "high" if score >= 100 and not fallback_candidate else "medium" if score >= 40 else "low"
    auto_eligible = candidate["sha256"] == export["sha256"] and confidence == "high"
    return _review_row(
        export,
        candidate,
        "candidate",
        score,
        ";".join(evidence),
        "auto_accept" if auto_eligible else "review",
        confidence=confidence,
        crop_score=crop_score,
        crop_confidence=crop_confidence,
    )


def _load_visual_samples(candidates: list[dict[str, object]]) -> dict[str, list[int]]:
    samples = {}
    for candidate in candidates:
        path = str(candidate["path"])
        try:
            samples[path] = _resize_grayscale(load_image(Path(path)), 12, 12)
        except Exception:  # noqa: BLE001 - unreadable images remain available to hash/date matching.
            continue
    return samples


def _visual_prefilter_candidates(
    export: dict[str, object],
    candidates: list[dict[str, object]],
    visual_samples: dict[str, list[int]],
    limit: int,
    threshold: float,
) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    try:
        export_sample = _resize_grayscale(load_image(Path(str(export["storage_path"]))), 12, 12)
    except Exception:  # noqa: BLE001 - preserve fallback row when the export image cannot be read.
        return []
    scored = []
    for candidate in candidates:
        candidate_sample = visual_samples.get(str(candidate["path"]))
        if candidate_sample is None:
            continue
        scored.append((_mean_squared_error(export_sample, candidate_sample), candidate))
    scored.sort(key=lambda row: row[0])
    return [candidate for score, candidate in scored[:limit] if score <= threshold]


def _candidate_could_match(export: dict[str, object], candidate: dict[str, object]) -> bool:
    return candidate["sha256"] == export["sha256"] or candidate.get("date_hint") == export["entry_date"]


def _is_project365_fallback_candidate(candidate: dict[str, object]) -> bool:
    return "/project365_exports/" in str(candidate.get("path", "")).replace("\\", "/")


def _review_row(
    export: dict[str, object],
    candidate: dict[str, object] | None,
    match_status: str,
    confidence_score: float,
    evidence: str,
    decision: str,
    confidence: str = "",
    crop_score: str = "",
    crop_confidence: str = "",
) -> dict[str, object]:
    return {
        "entry_id": export["entry_id"],
        "entry_date": export["entry_date"],
        "project365_media_asset_id": export["media_asset_id"],
        "candidate_path": candidate["path"] if candidate else "",
        "candidate_sha256": candidate["sha256"] if candidate else "",
        "match_status": match_status,
        "confidence": confidence,
        "confidence_score": f"{confidence_score:.2f}",
        "evidence": evidence,
        "crop_score": crop_score,
        "crop_confidence": crop_confidence,
        "decision": decision,
    }


def _auto_select_high_confidence(
    canonical_root: Path,
    db_path: Path,
    proposed_rows: list[dict[str, object]],
) -> int:
    originals_root = canonical_root / "media" / "originals"
    originals_root.mkdir(parents=True, exist_ok=True)
    selected = 0
    connection = sqlite3.connect(db_path)
    try:
        for row in proposed_rows:
            if row["decision"] != "auto_accept":
                continue
            source_path = Path(str(row["candidate_path"]))
            month = str(row["entry_date"])[:7]
            destination = originals_root / month / source_path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            now = dt.datetime.now(dt.UTC).isoformat()
            transformation = {
                "source": "original_photo_match",
                "evidence": row["evidence"],
                "confidence": row["confidence"],
                "confidence_score": row["confidence_score"],
                "source_path": str(source_path),
            }
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
                SELECT
                    ?,
                    ?,
                    'best_original',
                    source_file_id,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    'available',
                    'confirmed',
                    0,
                    ?,
                    import_batch_id,
                    ?,
                    ?
                FROM media_assets
                WHERE id = ?
                ON CONFLICT(entry_id, role, source_file_id, internal_filename)
                DO UPDATE SET
                    storage_path = excluded.storage_path,
                    sha256 = excluded.sha256,
                    byte_size = excluded.byte_size,
                    mime_type = excluded.mime_type,
                    review_status = excluded.review_status,
                    transformation_json = excluded.transformation_json,
                    updated_at = excluded.updated_at
                """,
                (
                    f"{row['entry_id']}:best_original:{source_path.name}",
                    row["entry_id"],
                    source_path.name,
                    str(destination),
                    row["candidate_sha256"],
                    destination.stat().st_size,
                    _mime_type(destination),
                    json.dumps(transformation, sort_keys=True),
                    now,
                    now,
                    row["project365_media_asset_id"],
                ),
            )
            selected += 1
        connection.commit()
    finally:
        connection.close()
    return selected


def _candidate_rows(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "path": candidate["path"],
            "root": candidate["root"],
            "filename": candidate["filename"],
            "sha256": candidate["sha256"],
            "byte_size": candidate["byte_size"],
            "date_hint": candidate["date_hint"] or "",
        }
        for candidate in candidates
    ]


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _candidate_fieldnames() -> list[str]:
    return ["path", "root", "filename", "sha256", "byte_size", "date_hint"]


def _review_fieldnames() -> list[str]:
    return [
        "entry_id",
        "entry_date",
        "project365_media_asset_id",
        "candidate_path",
        "candidate_sha256",
        "match_status",
        "confidence",
        "confidence_score",
        "evidence",
        "crop_score",
        "crop_confidence",
        "decision",
    ]


def _date_hint(path: Path) -> str | None:
    text = str(path)
    for index in range(len(text) - 9):
        candidate = text[index : index + 10]
        if candidate[4:5] == "-" and candidate[7:8] == "-":
            try:
                dt.date.fromisoformat(candidate)
                return candidate
            except ValueError:
                pass
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix in {".heic", ".heif"}:
        return "image/heic"
    if suffix == ".png":
        return "image/png"
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    return "application/octet-stream"


if __name__ == "__main__":
    raise SystemExit(main())
