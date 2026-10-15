#!/usr/bin/env python3
"""Source-agnostic providers for the shared crop workbench."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import types
from pathlib import Path
from typing import Any, Callable

from project365_visual_crop import estimate_visual_crop
from swarm_media_workflow import SwarmMediaReviewState, SwarmMediaStore


PROJECT365_PROVIDER_ID = "project365"
SWARM_PROVIDER_ID = "swarm"


class CropProviderRegistry:
    """Resolve explicit crop-provider IDs without cross-provider fallback."""

    def __init__(self, providers: list[Any]):
        self._providers: dict[str, Any] = {}
        for provider in providers:
            descriptor = provider.descriptor()
            provider_id = str(descriptor.get("id", "")).strip()
            if not provider_id:
                raise ValueError("Crop provider is missing an ID")
            if provider_id in self._providers:
                raise ValueError(f"Duplicate crop provider: {provider_id}")
            self._providers[provider_id] = provider

    def descriptors(self) -> list[dict[str, Any]]:
        return [dict(provider.descriptor()) for provider in self._providers.values()]

    def get(self, provider_id: str) -> Any:
        normalized = str(provider_id or "").strip()
        provider = self._providers.get(normalized)
        if provider is None:
            raise ValueError(f"Unknown crop provider: {normalized or '(empty)'}")
        return provider


class Project365CropProvider:
    """Adapter preserving PickerState's existing staged crop semantics."""

    def __init__(self, picker_state: Any | Callable[[], Any]):
        self._picker_state = picker_state

    def _state(self) -> Any:
        if isinstance(self._picker_state, (types.FunctionType, types.MethodType)):
            return self._picker_state()
        return self._picker_state

    @staticmethod
    def descriptor() -> dict[str, Any]:
        return {
            "id": PROJECT365_PROVIDER_ID,
            "label": "Project365",
            "save_mode": "staged",
            "capabilities": {
                "commit": True,
                "reject": True,
                "private": True,
                "estimate": True,
                "estimate_batches": True,
                "reference": True,
            },
            "filters": [
                {"id": "missing", "label": "No saved crop data"},
                {"id": "estimated", "label": "Saved estimates"},
                {"id": "confirmed", "label": "User saved crops"},
                {"id": "all", "label": "All crop targets"},
            ],
            "labels": {
                "reference": "Original Project365 target",
                "candidate": "Identified original",
            },
        }

    def list_entries(
        self,
        crop_filter: str = "missing",
        entry_dates: set[str] | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        return self._state().crop_entries(
            crop_filter=crop_filter,
            entry_dates=entry_dates or set(),
            start_date=start_date,
            end_date=end_date,
        )

    def detail(self, entry_id: str, candidate_path: str = "") -> dict[str, Any] | None:
        options: dict[str, Any] = {"mark_estimated_viewed": False}
        if candidate_path:
            options["candidate_path"] = candidate_path
        return self._state().crop_entry_detail(entry_id, **options)

    def save(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any],
        source: str = "manual",
    ) -> dict[str, Any]:
        return self._state().save_crop(
            entry_id=entry_id,
            candidate_path=candidate_path,
            crop=crop,
            source=source,
        )

    def reset(
        self,
        entry_id: str,
        candidate_path: str,
        preserve_estimate: bool = False,
    ) -> dict[str, Any]:
        return self._state().reset_crop(
            entry_id=entry_id,
            candidate_path=candidate_path,
            preserve_estimate=preserve_estimate,
        )

    def commit(self) -> dict[str, Any]:
        return self._state().commit_staged_crops()

    def suggest(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._state().suggest_crop_for_candidate(
            entry_id=entry_id,
            candidate_path=candidate_path,
            crop=crop,
        )

    def image_path(self, token: str) -> Path | None:
        return self._state().image_path(token)

    def pending_commits(self) -> dict[str, Any]:
        return self._state().pending_crop_commits()

    def latest_estimate_job(self) -> dict[str, Any]:
        return self._state().latest_crop_estimate_job() or {}

    def private_status(self, entry_id: str) -> dict[str, Any]:
        return self._state().private_status(entry_id)

    def set_private(self, entry_id: str, private: Any) -> dict[str, Any]:
        return self._state().set_private(entry_id=entry_id, private=private)

    def reject(self, entry_id: str, candidate_path: str, notes: str) -> dict[str, Any]:
        return self._state().reject_crop_original(
            entry_id=entry_id,
            candidate_path=candidate_path,
            notes=notes,
        )

    def estimate_job(self, job_id: str) -> dict[str, Any] | None:
        return self._state().crop_estimate_job(job_id)


class SwarmCropProvider:
    """Adapt queued Swarm photos to the shared crop workbench contract."""

    def __init__(self, store: SwarmMediaStore):
        self.store = store
        self._review_state = SwarmMediaReviewState(store)

    @staticmethod
    def descriptor() -> dict[str, Any]:
        return {
            "id": SWARM_PROVIDER_ID,
            "label": "Swarm imports",
            "save_mode": "immediate",
            "capabilities": {
                "commit": False,
                "reject": False,
                "private": False,
                "estimate": True,
                "estimate_batches": False,
                "reference": False,
            },
            "filters": [
                {"id": "missing", "label": "Needs crop"},
                {"id": "confirmed", "label": "Completed crops"},
                {"id": "all", "label": "All queued crops"},
            ],
            "labels": {
                "reference": "Selected crop preview",
                "candidate": "Imported Swarm photo",
            },
        }

    @staticmethod
    def _token(photo_key: str) -> str:
        return hashlib.sha256(photo_key.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _target_id(cls, photo_key: str) -> str:
        return f"{SWARM_PROVIDER_ID}:{cls._token(photo_key)}"

    def _photo_key(self, entry_id: str) -> str:
        prefix = f"{SWARM_PROVIDER_ID}:"
        if not str(entry_id).startswith(prefix):
            raise ValueError("Foreign crop target ID")
        token = str(entry_id)[len(prefix):]
        if not token:
            raise ValueError("Invalid Swarm crop target ID")
        photo_key = self._review_state.photo_key_by_token.get(token)
        if photo_key is None:
            self._review_state.refresh_tokens()
            photo_key = self._review_state.photo_key_by_token.get(token)
        if photo_key is None:
            raise ValueError("Unknown Swarm crop target")
        return photo_key

    @staticmethod
    def _ready(row: dict[str, Any]) -> bool:
        derivative = str(row.get("crop_derivative_path") or "")
        return bool(
            row.get("crop_queued")
            and derivative
            and str(row.get("crop_source_sha256") or "") == str(row.get("source_sha256") or "")
            and Path(derivative).is_file()
        )

    @staticmethod
    def _crop(row: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(str(row.get("crop_json") or "{}"))
        except json.JSONDecodeError as error:
            raise ValueError("Invalid saved Swarm crop") from error
        return value if isinstance(value, dict) else {}

    def _summary(self, row: dict[str, Any]) -> dict[str, Any]:
        ready = self._ready(row)
        target_id = self._target_id(str(row["photo_key"]))
        source_path = Path(str(row["source_path"]))
        width = int(row.get("width") or 0)
        height = int(row.get("height") or 0)
        return {
            "provider_id": SWARM_PROVIDER_ID,
            "entry_id": target_id,
            "entry_date": str(row.get("local_date") or ""),
            "title": str(row.get("title") or "Swarm photo"),
            "display_title": str(row.get("title") or "Swarm photo"),
            "status": "selected",
            "source_token": "",
            "source_exists": False,
            "source_byte_size": "",
            "source_file_type": "",
            "source_dimensions": "",
            "people_names": [],
            "candidate_path": target_id,
            "candidate_role": "swarm_import_photo",
            "candidate_filename": source_path.name,
            "candidate_dimensions": f"{width} x {height}" if width and height else "",
            "candidate_token": self._token(str(row["photo_key"])),
            "crop_has_crop": ready,
            "crop_source": "manual" if ready else "",
            "crop_source_state": "applied" if ready else "queued",
            "crop_commit_pending": False,
            "crop_status": "User saved crop" if ready else "No saved crop",
            "estimate_batch_id": "",
            "estimate_batch_number": 0,
            "estimate_batch_complete": False,
        }

    def _row_for_target(self, entry_id: str) -> dict[str, Any]:
        row = self.store.queued_photo(self._photo_key(entry_id))
        if row is None:
            raise ValueError("Swarm photo is not in the active crop queue")
        return row

    def list_entries(
        self,
        crop_filter: str = "missing",
        entry_dates: set[str] | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        if crop_filter not in {"all", "missing", "with_crop", "confirmed", "estimated"}:
            raise ValueError("Unsupported crop filter")
        requested_dates = entry_dates or set()
        entries: list[dict[str, Any]] = []
        for row in self.store.queued_photos():
            entry_date = str(row.get("local_date") or "")
            if requested_dates and entry_date not in requested_dates:
                continue
            if start_date and entry_date < start_date:
                continue
            if end_date and entry_date > end_date:
                continue
            ready = self._ready(row)
            if crop_filter == "missing" and ready:
                continue
            if crop_filter in {"with_crop", "confirmed"} and not ready:
                continue
            if crop_filter == "estimated":
                continue
            entries.append(self._summary(row))
        return entries

    def detail(self, entry_id: str, candidate_path: str = "") -> dict[str, Any]:
        row = self._row_for_target(entry_id)
        summary = self._summary(row)
        target_id = str(summary["entry_id"])
        if candidate_path and candidate_path != target_id:
            raise ValueError("Crop candidate does not belong to this Swarm target")
        source_path = Path(str(row["source_path"]))
        width = int(row.get("width") or 0)
        height = int(row.get("height") or 0)
        crop = self._crop(row)
        byte_size = source_path.stat().st_size if source_path.is_file() else 0
        candidate = {
            "path": target_id,
            "filename": source_path.name,
            "folder_path": "",
            "folder_label": "Swarm import",
            "token": self._token(str(row["photo_key"])),
            "exists": source_path.is_file(),
            "evidence": "Swarm export",
            "byte_size": str(byte_size or ""),
            "mime_type": mimetypes.guess_type(source_path.name)[0] or "",
            "dimensions": f"{width} x {height}" if width and height else "",
            "review_crop_x": crop.get("x", ""),
            "review_crop_y": crop.get("y", ""),
            "review_crop_size": crop.get("size", ""),
            "review_crop_candidate_width": crop.get("candidate_width", ""),
            "review_crop_candidate_height": crop.get("candidate_height", ""),
            "review_crop_source": crop.get("source", ""),
            "review_crop_fill_color": crop.get("fill_color", ""),
            "review_crop_rotation_degrees": crop.get("rotation_degrees", ""),
            "review_decision": "use_import_photo",
            "review_notes": "",
            "selected": True,
            "associated": False,
            "database_confirmed_role": "swarm_import_photo",
        }
        return {
            **summary,
            "candidate_count": 1,
            "selected_count": 1,
            "pending_commit_count": 0,
            "commit_ready": False,
            "accepted_count": 1,
            "associated_count": 0,
            "rejected_count": 0,
            "fallback_count": 0,
            "current_match_status": "",
            "current_decision": "",
            "manual_search_message": "",
            "used_range_days": [],
            "candidates": [candidate],
        }

    def save(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any],
        source: str = "manual",
    ) -> dict[str, Any]:
        if source != "manual":
            raise ValueError("Swarm crop saves must be manual")
        row = self._row_for_target(entry_id)
        target_id = self._target_id(str(row["photo_key"]))
        if candidate_path and candidate_path != target_id:
            raise ValueError("Crop candidate does not belong to this Swarm target")
        self.store.render_crop(str(row["photo_key"]), crop)
        return self.detail(target_id, target_id)

    def reset(
        self,
        entry_id: str,
        candidate_path: str,
        preserve_estimate: bool = False,
    ) -> dict[str, Any]:
        if preserve_estimate:
            raise ValueError("Swarm crops do not have saved estimates")
        row = self._row_for_target(entry_id)
        target_id = self._target_id(str(row["photo_key"]))
        if candidate_path and candidate_path != target_id:
            raise ValueError("Crop candidate does not belong to this Swarm target")
        self.store.clear_crop(str(row["photo_key"]))
        return self.detail(target_id, target_id)

    def commit(self) -> dict[str, Any]:
        raise ValueError("Swarm crop saves are applied immediately")

    def suggest(
        self,
        entry_id: str,
        candidate_path: str,
        crop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self._row_for_target(entry_id)
        target_id = self._target_id(str(row["photo_key"]))
        if candidate_path and candidate_path != target_id:
            raise ValueError("Crop candidate does not belong to this Swarm target")
        return {
            "entry": self.detail(target_id, target_id),
            "crop": estimate_visual_crop(Path(str(row["source_path"]))),
        }

    def image_path(self, token: str) -> Path | None:
        path = self._review_state.media_by_token.get(str(token))
        if path is None:
            self._review_state.refresh_tokens()
            path = self._review_state.media_by_token.get(str(token))
        return path

    @staticmethod
    def pending_commits() -> dict[str, Any]:
        return {"pending_count": 0}

    @staticmethod
    def latest_estimate_job() -> dict[str, Any]:
        return {}
