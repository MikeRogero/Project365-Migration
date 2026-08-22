#!/usr/bin/env python3
"""Create local Project365 canonical backup snapshots."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path


DEFAULT_INCLUDE_PATHS = [
    "Source Data",
    "Project365Canonical",
    "Reports",
    "docs",
    "config",
    "PROJECT365_TO_DIARIUM_MIGRATION_PLAN.md",
]


@dataclass(frozen=True)
class SnapshotSummary:
    snapshot_path: str
    manifest_path: str
    file_count: int
    byte_count: int
    dry_run: bool


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local Project365 backup snapshot.")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--backup-root", default="Project365Backups")
    parser.add_argument("--snapshot-name", help="Override timestamped snapshot folder name.")
    parser.add_argument(
        "--include",
        action="append",
        dest="includes",
        help="Relative path to include. May be repeated. Defaults to core Project365 paths.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    summary = create_snapshot(
        workspace_root=Path(args.workspace_root),
        backup_root=Path(args.backup_root),
        snapshot_name=args.snapshot_name,
        includes=args.includes or DEFAULT_INCLUDE_PATHS,
        dry_run=args.dry_run,
    )
    print("Project365 backup snapshot: PASS")
    print(f"Snapshot: {summary.snapshot_path}")
    print(f"Manifest: {summary.manifest_path}")
    print(f"Files: {summary.file_count}")
    print(f"Bytes: {summary.byte_count}")
    print(f"Dry run: {str(summary.dry_run).lower()}")
    return 0


def create_snapshot(
    workspace_root: Path,
    backup_root: Path,
    snapshot_name: str | None,
    includes: list[str],
    dry_run: bool = False,
) -> SnapshotSummary:
    workspace_root = workspace_root.resolve()
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = snapshot_name or f"project365_snapshot_{timestamp}"
    snapshot_path = backup_root / name
    manifest_path = snapshot_path / "snapshot_manifest.csv"
    summary_path = snapshot_path / "snapshot_summary.json"

    rows: list[dict[str, object]] = []
    for include in includes:
        source = workspace_root / include
        if not source.exists():
            raise FileNotFoundError(f"Snapshot include path does not exist: {source}")
        rows.extend(_scan_source(workspace_root, source))

    file_count = len(rows)
    byte_count = sum(int(row["byte_count"]) for row in rows)

    if not dry_run:
        snapshot_path.mkdir(parents=True, exist_ok=False)
        for include in includes:
            source = workspace_root / include
            destination = snapshot_path / include
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
        _write_manifest(manifest_path, rows)
        summary_path.write_text(
            json.dumps(
                {
                    "created_at": timestamp,
                    "workspace_root": str(workspace_root),
                    "include_paths": includes,
                    "file_count": file_count,
                    "byte_count": byte_count,
                    "dry_run": dry_run,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    return SnapshotSummary(
        snapshot_path=str(snapshot_path),
        manifest_path=str(manifest_path),
        file_count=file_count,
        byte_count=byte_count,
        dry_run=dry_run,
    )


def _scan_source(workspace_root: Path, source: Path) -> list[dict[str, object]]:
    if source.is_file():
        return [_manifest_row(workspace_root, source)]
    rows = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            rows.append(_manifest_row(workspace_root, path))
    return rows


def _manifest_row(workspace_root: Path, path: Path) -> dict[str, object]:
    return {
        "relative_path": str(path.relative_to(workspace_root)),
        "byte_count": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "byte_count", "sha256"])
        writer.writeheader()
        writer.writerows(rows)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
