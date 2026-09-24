"""First-run source folders and conservative export discovery for the import hub."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Callable


SOURCE_FOLDERS = {
    "project365": "Project365 Pro Export Zips",
    "facebook": "Facebook Data Export",
    "swarm": "Swarm Data Export",
    "x_twitter": "Twitter Data Export",
    "instagram": "Instagram Data Export",
}
SOURCE_LABELS = {
    "project365": "Project365 Pro",
    "facebook": "Facebook",
    "swarm": "Swarm",
    "x_twitter": "Twitter / X",
    "instagram": "Instagram",
}
SOURCE_CAPABILITIES = {
    "project365": "import",
    "facebook": "dayone_package",
    "swarm": "unavailable",
    "x_twitter": "stage_for_review",
    "instagram": "unavailable",
}


def _enabled(root: Path, settings_path: Path) -> tuple[bool, list[str]]:
    if not settings_path.is_file():
        return False, [key for key, folder in SOURCE_FOLDERS.items() if (root / folder).is_dir()]
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        enabled = payload["enabled"]
        if payload.get("version") != 1 or not isinstance(enabled, list) or any(
            not isinstance(key, str) or key not in SOURCE_FOLDERS for key in enabled
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("Invalid import source settings") from error
    return True, list(dict.fromkeys(enabled))


def status(root: Path, settings_path: Path) -> dict[str, object]:
    """Read-only setup and readiness status; never create or alter source folders."""
    configured, enabled = _enabled(root, settings_path)
    items: dict[str, dict[str, object]] = {}
    for key, folder_name in SOURCE_FOLDERS.items():
        folder = root / folder_name
        ready = False
        detail = ""
        if key == "project365":
            ready = any(path.is_file() for path in folder.rglob("*.zip")) if folder.is_dir() else False
            detail = "Drop Project365 Pro ZIP exports here."
        elif key == "facebook":
            try:
                resolve_facebook(folder)
                ready = True
            except ValueError as error:
                detail = str(error)
        elif key == "x_twitter":
            try:
                resolve_x(folder)
                ready = True
            except ValueError as error:
                detail = str(error)
        elif key == "swarm":
            detail = "Drop the Swarm export here. The importer is not built yet."
        else:
            detail = "Drop the Instagram export here. The importer is not built yet."
        items[key] = {
            "label": SOURCE_LABELS[key], "folder": str(folder),
            "enabled": key in enabled, "ready": ready,
            "capability": SOURCE_CAPABILITIES[key], "detail": detail,
        }
    return {"configured": configured, "enabled": enabled, "sources": items}


def configure(root: Path, settings_path: Path, enabled: list[str]) -> dict[str, object]:
    """Persist exact selected providers and create only their fixed drop folders."""
    if not isinstance(enabled, list) or any(not isinstance(key, str) or key not in SOURCE_FOLDERS for key in enabled):
        raise ValueError("Unknown import source")
    chosen = list(dict.fromkeys(enabled))
    for key in chosen:
        (root / SOURCE_FOLDERS[key]).mkdir(parents=True, exist_ok=True)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=settings_path.parent, encoding="utf-8", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump({"version": 1, "enabled": chosen}, handle)
        handle.write("\n")
    try:
        temporary.replace(settings_path)
    finally:
        temporary.unlink(missing_ok=True)
    return status(root, settings_path)


def _candidates(folder: Path, marker: Callable[[Path], bool]) -> list[Path]:
    if not folder.is_dir():
        return []
    roots = [folder, *(path for path in folder.iterdir() if path.is_dir())]
    return [root for root in roots if root.resolve().is_relative_to(folder.resolve()) and marker(root)]


def resolve_facebook(folder: Path) -> tuple[Path, Path | None]:
    """Find exactly one extracted JSON export; an optional Other folder supplements it."""
    def is_json_export(root: Path) -> bool:
        return any((root / section).is_dir() and any((root / section).glob("your_posts*.json"))
                   for section in ("your_activity_across_facebook/posts", "posts"))

    candidates = _candidates(folder, is_json_export)
    if not candidates:
        raise ValueError("Drop an extracted Facebook JSON export folder here.")
    if len(candidates) != 1:
        raise ValueError("Found multiple Facebook JSON exports; choose one in Advanced.")
    other = folder / "Other Facebook Data Exports"
    additional = other if other.is_dir() and any(path.is_dir() for path in other.iterdir()) else None
    return candidates[0], additional


def resolve_x(folder: Path) -> Path:
    candidates = _candidates(folder, lambda root: (root / "data" / "tweets.js").is_file())
    if not candidates:
        raise ValueError("Drop an extracted X archive containing data/tweets.js here.")
    if len(candidates) != 1:
        raise ValueError("Found multiple X archives; choose one in Advanced.")
    return candidates[0]
