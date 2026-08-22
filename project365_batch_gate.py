#!/usr/bin/env python3
"""Gate Diarium import batches using manifest and reconciliation reports."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BatchGateSummary:
    passed: bool
    report_path: str
    expected_entries: int
    reconciled_entries: int
    expected_media: int
    reconciled_media: int
    issues: list[str]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Project365 Diarium batch gate.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--reconciliation", required=True)
    parser.add_argument("--report-out", required=True)
    parser.add_argument("--batch-name", default="unnamed_batch")
    args = parser.parse_args()

    summary = validate_batch_gate(
        manifest_path=Path(args.manifest),
        reconciliation_path=Path(args.reconciliation),
        report_path=Path(args.report_out),
        batch_name=args.batch_name,
    )
    print(f"Project365 batch gate: {'PASS' if summary.passed else 'FAIL'}")
    print(f"Report: {summary.report_path}")
    print(f"Expected entries: {summary.expected_entries}")
    print(f"Reconciled entries: {summary.reconciled_entries}")
    print(f"Expected media: {summary.expected_media}")
    print(f"Reconciled media: {summary.reconciled_media}")
    if summary.issues:
        print("Issues:")
        for issue in summary.issues:
            print(f"- {issue}")
    return 0 if summary.passed else 1


def validate_batch_gate(
    manifest_path: Path,
    reconciliation_path: Path,
    report_path: Path,
    batch_name: str,
) -> BatchGateSummary:
    manifest_rows = _read_csv(manifest_path)
    reconciliation_rows = _read_csv(reconciliation_path)
    manifest_entry_ids = {row["entry_id"] for row in manifest_rows}
    reconciliation_entry_ids = {row["entry_id"] for row in reconciliation_rows}
    expected_media = sum(1 for row in manifest_rows if row.get("media_asset_id"))
    reconciled_media = sum(int(row.get("media_file_count") or 0) for row in reconciliation_rows)
    issues: list[str] = []

    if len(manifest_rows) != len(reconciliation_rows):
        issues.append(
            f"entry_count_mismatch: manifest={len(manifest_rows)} reconciliation={len(reconciliation_rows)}"
        )
    missing = sorted(manifest_entry_ids - reconciliation_entry_ids)
    extra = sorted(reconciliation_entry_ids - manifest_entry_ids)
    if missing:
        issues.append(f"missing_reconciled_entries: {','.join(missing)}")
    if extra:
        issues.append(f"unexpected_reconciled_entries: {','.join(extra)}")
    if expected_media != reconciled_media:
        issues.append(f"media_count_mismatch: manifest={expected_media} reconciliation={reconciled_media}")

    for row in reconciliation_rows:
        entry_id = row.get("entry_id", "")
        if row.get("match_status") != "matched":
            issues.append(f"{entry_id}: match_status={row.get('match_status')}")
        if row.get("text_status") not in {"unchanged", "normalized_unchanged", ""}:
            issues.append(f"{entry_id}: text_status={row.get('text_status')}")
        if row.get("has_reviewable_updates") == "true":
            issues.append(f"{entry_id}: has_reviewable_updates=true")
        if int(row.get("tag_count") or 0) < 1:
            issues.append(f"{entry_id}: missing_source_tag")

    passed = not issues
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "batch_name": batch_name,
                "passed": passed,
                "manifest": str(manifest_path),
                "reconciliation": str(reconciliation_path),
                "expected_entries": len(manifest_rows),
                "reconciled_entries": len(reconciliation_rows),
                "expected_media": expected_media,
                "reconciled_media": reconciled_media,
                "issues": issues,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return BatchGateSummary(
        passed=passed,
        report_path=str(report_path),
        expected_entries=len(manifest_rows),
        reconciled_entries=len(reconciliation_rows),
        expected_media=expected_media,
        reconciled_media=reconciled_media,
        issues=issues,
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing CSV: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    raise SystemExit(main())
