"""Shared local paths for the Project365 migration workspace."""

from __future__ import annotations

from pathlib import Path


SOURCE_DATA_ROOT = Path("Source Data")
PROJECT365_PRO_EXPORT_ZIPS_DIR = SOURCE_DATA_ROOT / "Project365 Pro Export Zips"
ORIGINAL_PHOTOS_ROOT = SOURCE_DATA_ROOT / "Original Photos matching Project365 Entries"
