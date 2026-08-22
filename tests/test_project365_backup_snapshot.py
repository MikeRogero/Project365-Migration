from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import project365_backup_snapshot as backup_snapshot


class Project365BackupSnapshotTests(unittest.TestCase):
    def test_create_snapshot_copies_included_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workspace = base / "workspace"
            backup_root = base / "backups"
            workspace.mkdir()
            workspace.joinpath("Source Data", "Project365 Pro Export Zips").mkdir(parents=True)
            workspace.joinpath("Source Data", "Project365 Pro Export Zips", "1998-04.zip").write_bytes(b"raw export")
            workspace.joinpath("Reports").mkdir()
            workspace.joinpath("Reports", "source_files.csv").write_text("month\n1998-04\n")

            summary = backup_snapshot.create_snapshot(
                workspace_root=workspace,
                backup_root=backup_root,
                snapshot_name="test_snapshot",
                includes=["Source Data", "Reports/source_files.csv"],
            )

            self.assertFalse(summary.dry_run)
            snapshot_path = Path(summary.snapshot_path)
            self.assertTrue(snapshot_path.joinpath("Source Data", "Project365 Pro Export Zips", "1998-04.zip").exists())
            self.assertTrue(snapshot_path.joinpath("Reports", "source_files.csv").exists())
            self.assertTrue(snapshot_path.joinpath("snapshot_summary.json").exists())
            with Path(summary.manifest_path).open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                sorted(row["relative_path"] for row in rows),
                ["Reports/source_files.csv", "Source Data/Project365 Pro Export Zips/1998-04.zip"],
            )

    def test_dry_run_scans_without_creating_snapshot_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workspace = base / "workspace"
            workspace.mkdir()
            workspace.joinpath("docs").mkdir()
            workspace.joinpath("docs", "runbook.md").write_text("backup")

            summary = backup_snapshot.create_snapshot(
                workspace_root=workspace,
                backup_root=base / "backups",
                snapshot_name="dry_run",
                includes=["docs"],
                dry_run=True,
            )

            self.assertTrue(summary.dry_run)
            self.assertEqual(summary.file_count, 1)
            self.assertFalse(Path(summary.snapshot_path).exists())


if __name__ == "__main__":
    unittest.main()
