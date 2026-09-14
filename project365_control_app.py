#!/usr/bin/env python3
"""Local control app for the Project365-to-Diarium workflow."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from project365_original_reference_pipeline import (
    ACCEPT_DECISIONS,
    FALLBACK_DECISIONS,
    REJECT_DECISIONS,
)
from project365_media_derivatives import DEFAULT_DERIVATIVE_POLICY, derivative_readiness_summary
import project365_original_picker as original_picker
import project365_broad_visual_match as broad_visual_match
import project365_diary_enrichment as diary_enrichment
import project365_media_dedupe_review as media_dedupe
from project365_paths import ORIGINAL_PHOTOS_ROOT, PROJECT365_PRO_EXPORT_ZIPS_DIR


CANONICAL_ROOT = Path("Project365Canonical")
SOURCE_DATA_ROOT = Path("Source Data")
REPORT_DIR = Path("Reports")
VERIFY_REPORT_DIR = CANONICAL_ROOT / "exports" / "verification_reports"
ORIGINAL_QUEUE = VERIFY_REPORT_DIR / "original_photo_external_search_queue.csv"
ORIGINAL_UNCLEAR_GROUPS = VERIFY_REPORT_DIR / "original_photo_unclear_groups.csv"
ORIGINAL_BATCH_PLAN = VERIFY_REPORT_DIR / "original_photo_search_batch_plan.csv"
ORIGINAL_SEARCH_ATTEMPTS = VERIFY_REPORT_DIR / "original_photo_search_attempts.csv"
PHOTO_LIBRARY_INDEX = CANONICAL_ROOT / "photo_library_index.sqlite"
BROAD_VISUAL_DB = CANONICAL_ROOT / "broad_visual_match.sqlite"
TAG_QUEUE = VERIFY_REPORT_DIR / "tag_review_queue.csv"
DIGIKAM_PEOPLE_REPORT = VERIFY_REPORT_DIR / "digikam_people_import_report.csv"
DIARIUM_IMPORT_BATCH_DIR = CANONICAL_ROOT / "exports" / "diarium_import_batches"
DIARIUM_DB_PATH = (
    Path.home() / "Library" / "Containers" / "mac.partl.Diarium" / "Data" / "data.db"
)
DERIVATIVE_POLICY = DEFAULT_DERIVATIVE_POLICY
STATUS_BATCH_LIMIT = 50
CONTROL_RUN_HISTORY = VERIFY_REPORT_DIR / "control_run_history.jsonl"
RUN_HISTORY_LIMIT = 50
ARCHIVE_RESULT_LIMIT = 25
BROAD_REVIEW_ENTRY_LIMIT = 1
_BROAD_MONTHLY_COVERAGE_CACHE: dict[str, Any] = {"key": None, "rows": []}


@dataclass(frozen=True)
class ControlConfig:
    host: str
    port: int
    picker_url: str


class ControlState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._picker_lock = threading.Lock()
        self.history: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self._job_cancel_events: dict[str, threading.Event] = {}
        self._job_processes: dict[str, subprocess.Popen[Any]] = {}
        self._picker_state: original_picker.PickerState | None = None
        self._active_photo_index_folder = ""
        self._broad_image_paths: dict[str, Path] = {}
        self.history = _load_control_run_history(CONTROL_RUN_HISTORY)

    def status(
        self,
        steps: list[str] | None = None,
        include_broad_monthly_coverage: bool = False,
    ) -> dict[str, Any]:
        if steps is not None:
            return self._scoped_status(
                steps,
                include_broad_monthly_coverage=include_broad_monthly_coverage,
            )
        active_jobs = self._active_jobs(include_crop_estimates=True)
        active_owners = {_owner_step(str(job.get("step", ""))) for job in active_jobs}
        diarium_package = _diarium_package_status(DIARIUM_IMPORT_BATCH_DIR)
        diarium_local = _diarium_local_status(DIARIUM_DB_PATH)
        photo_library_index = (
            _busy_photo_library_index_status(PHOTO_LIBRARY_INDEX)
            if "build_photo_index" in active_owners
            else _photo_library_index_status(PHOTO_LIBRARY_INDEX)
        )
        status = {
            "paths": {
                "project365_zips": _path_status(PROJECT365_PRO_EXPORT_ZIPS_DIR),
                "original_photos": _path_status(ORIGINAL_PHOTOS_ROOT),
                "canonical_db": _path_status(CANONICAL_ROOT / "canonical.db"),
                "original_queue": _path_status(ORIGINAL_QUEUE),
                "original_batch_plan": _path_status(ORIGINAL_BATCH_PLAN),
                "original_search_attempts": _path_status(ORIGINAL_SEARCH_ATTEMPTS),
                "photo_library_index": _path_status(PHOTO_LIBRARY_INDEX),
                "broad_visual_match": _path_status(BROAD_VISUAL_DB),
                "tag_queue": _path_status(TAG_QUEUE),
                "digikam_people_report": _path_status(DIGIKAM_PEOPLE_REPORT),
            },
            "database": _database_status(CANONICAL_ROOT / "canonical.db"),
            "photo_library_index": photo_library_index,
            "original_queue": _queue_status(ORIGINAL_QUEUE),
            "crop_confirmation": self.crop_confirmation_status(),
            "original_remainder_overview": _remainder_overview(ORIGINAL_UNCLEAR_GROUPS),
            "original_batch_plan": _batch_plan_status(ORIGINAL_BATCH_PLAN, ORIGINAL_SEARCH_ATTEMPTS),
            "original_search_attempts": _search_attempt_status(ORIGINAL_SEARCH_ATTEMPTS),
            "broad_visual_match": _broad_visual_status(),
            "diarium_package": diarium_package,
            "diarium_local": diarium_local,
            "diarium_import_verification": _diarium_import_verification(
                diarium_package,
                diarium_local,
            ),
            "history": self.history[-20:],
            "active_jobs": active_jobs,
        }
        _attach_visual_job_progress(status)
        status["top_metrics"] = _top_metrics(
            status["database"],
            status["photo_library_index"],
        )
        status["workflow_history"] = _current_workflow_history(self.history, status)
        return status

    def _scoped_status(
        self,
        steps: list[str],
        include_broad_monthly_coverage: bool = False,
    ) -> dict[str, Any]:
        requested = {
            owner
            for owner in (_owner_step(str(step).strip()) for step in steps)
            if owner in WORKFLOW_STEPS or owner == "workflow_overview"
        }
        active_jobs = self._active_jobs(include_crop_estimates="crop_confirmation" in requested)
        active_owners = {_owner_step(str(job.get("step", ""))) for job in active_jobs}
        needs_photo_index_metric = bool(
            requested & {"build_photo_index", "match_easy_originals"}
        ) and "build_photo_index" not in active_owners
        include_photo_index_metric = needs_photo_index_metric and "workflow_overview" not in requested
        status: dict[str, Any] = {
            "top_metrics": _initial_top_metrics(
                include_photo_index=include_photo_index_metric
            ),
            "active_jobs": active_jobs,
        }
        if "workflow_overview" in requested:
            status["photo_library_index"] = _photo_library_index_latest_run_status(PHOTO_LIBRARY_INDEX)
            status["original_queue"] = _queue_status(ORIGINAL_QUEUE)
            status["original_remainder_overview"] = _remainder_overview(ORIGINAL_UNCLEAR_GROUPS)
            status["original_batch_plan"] = _batch_plan_status(ORIGINAL_BATCH_PLAN, ORIGINAL_SEARCH_ATTEMPTS)
            status["original_search_attempts"] = _search_attempt_status(ORIGINAL_SEARCH_ATTEMPTS)
            status["broad_visual_match"] = _broad_visual_overview_status()
            status["crop_confirmation"] = self.crop_confirmation_status()
            status["diarium_package"] = _diarium_package_status(DIARIUM_IMPORT_BATCH_DIR)
        paths: dict[str, Any] = {}
        if "import_zips" in requested:
            paths["project365_zips"] = _path_status(PROJECT365_PRO_EXPORT_ZIPS_DIR)
        if "build_photo_index" in requested:
            paths["photo_library_index"] = _path_status(PHOTO_LIBRARY_INDEX)
            status["photo_library_index"] = (
                _busy_photo_library_index_status(PHOTO_LIBRARY_INDEX)
                if "build_photo_index" in active_owners
                else _photo_library_index_status(PHOTO_LIBRARY_INDEX)
            )
        if "match_easy_originals" in requested:
            status["photo_library_index"] = (
                _busy_photo_library_index_status(PHOTO_LIBRARY_INDEX)
                if "build_photo_index" in active_owners
                else _photo_library_index_status(PHOTO_LIBRARY_INDEX)
            )
            status["original_queue"] = _queue_status(ORIGINAL_QUEUE)
            status["original_remainder_overview"] = _remainder_overview(ORIGINAL_UNCLEAR_GROUPS)
            status["original_batch_plan"] = _batch_plan_status(ORIGINAL_BATCH_PLAN, ORIGINAL_SEARCH_ATTEMPTS)
            status["original_search_attempts"] = _search_attempt_status(ORIGINAL_SEARCH_ATTEMPTS)
        if "broad_visual_match" in requested or "rough_visual_match" in requested:
            paths["broad_visual_match"] = _path_status(BROAD_VISUAL_DB)
            status["broad_visual_match"] = _broad_visual_status(
                include_monthly_coverage=include_broad_monthly_coverage,
                include_prefilter_stale_count=False,
                include_review_runs="broad_visual_match" in requested,
            )
            _attach_visual_job_progress(status)
        if "crop_confirmation" in requested:
            status["crop_confirmation"] = self.crop_confirmation_status()
        if "generate_derivatives" in requested:
            status["database"] = _database_status(CANONICAL_ROOT / "canonical.db")
        if "face_tagging" in requested:
            paths["tag_queue"] = _path_status(TAG_QUEUE)
            paths["digikam_people_report"] = _path_status(DIGIKAM_PEOPLE_REPORT)
        if "generate_diarium_package" in requested:
            diarium_package = _diarium_package_status(DIARIUM_IMPORT_BATCH_DIR)
            diarium_local = _diarium_local_status(DIARIUM_DB_PATH)
            status["database"] = _database_status(CANONICAL_ROOT / "canonical.db")
            status["diarium_package"] = diarium_package
            status["diarium_local"] = diarium_local
            status["diarium_import_verification"] = _diarium_import_verification(
                diarium_package,
                diarium_local,
            )
        if paths:
            status["paths"] = paths
        status["workflow_history"] = _current_workflow_history(self.history, status, requested)
        return status

    def crop_confirmation_status(self) -> dict[str, Any]:
        try:
            picker_state = self.picker_state()
            entries = picker_state.crop_entries(crop_filter="all")
            pending_commits = picker_state.pending_crop_commits()
            crop_estimate_batch = picker_state.latest_crop_estimate_job() or {}
        except Exception as exc:  # noqa: BLE001 - status panel should stay readable if crop state is unavailable.
            return {
                "exists": False,
                "error": str(exc),
                "total_count": 0,
                "with_crop_count": 0,
                "estimated_crop_count": 0,
                "confirmed_crop_count": 0,
                "queued_count": 0,
                "pending_commit_count": 0,
                "crop_estimate_batch": {},
            }
        with_crop_count = sum(1 for entry in entries if entry.get("crop_has_crop"))
        estimated_crop_count = sum(
            1
            for entry in entries
            if entry.get("crop_has_crop") and str(entry.get("crop_source", "")).strip().lower() == "estimated"
        )
        confirmed_crop_count = max(0, with_crop_count - estimated_crop_count)
        queued_count = max(0, len(entries) - with_crop_count)
        return {
            "exists": True,
            "total_count": len(entries),
            "with_crop_count": with_crop_count,
            "estimated_crop_count": estimated_crop_count,
            "confirmed_crop_count": confirmed_crop_count,
            "queued_count": queued_count,
            "pending_commit_count": int(pending_commits.get("pending_count") or 0),
            "crop_estimate_batch": crop_estimate_batch,
        }

    def run_step(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        commands = _commands_for_step(step, payload)
        self._update_active_photo_index_folder(step, payload)
        started = dt.datetime.now(dt.UTC).isoformat()
        with self._lock:
            record = self._execute_commands(step, commands, started, payload=payload)
            self.history.append(record)
            _save_control_run_history(CONTROL_RUN_HISTORY, self.history)
        return record

    def start_step(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        commands = _commands_for_step(step, payload)
        self._update_active_photo_index_folder(step, payload)
        started = dt.datetime.now(dt.UTC).isoformat()
        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "step": step,
            "status": "queued",
            "started_at": started,
            "finished_at": "",
            "current_command": "",
            "outputs": [],
            "error": "",
            "last_update_at": started,
        }
        cancel_event = threading.Event()
        with self._job_lock:
            self.jobs[job_id] = job
            self._job_cancel_events[job_id] = cancel_event
        thread = threading.Thread(
            target=self._run_step_job,
            args=(job_id, step, commands, started, dict(payload), cancel_event),
            daemon=True,
        )
        thread.start()
        return self.job_status(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._job_lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(f"Unknown job: {job_id}")
            if job.get("status") not in {"queued", "running"}:
                return dict(job)
            cancel_event = self._job_cancel_events.get(job_id)
            process = self._job_processes.get(job_id)
        if cancel_event:
            cancel_event.set()
        if process and process.poll() is None:
            _terminate_process_group(process, force=False)
        self._update_job(job_id, status="cancelling", error="Cancelling...")
        return self.job_status(job_id)

    def job_status(self, job_id: str) -> dict[str, Any]:
        with self._job_lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(f"Unknown job: {job_id}")
            return dict(job)

    def _active_jobs(self, include_crop_estimates: bool = False) -> list[dict[str, Any]]:
        with self._job_lock:
            active_jobs = [
                dict(job)
                for job in self.jobs.values()
                if job.get("status") in {"queued", "running"}
            ]
        if include_crop_estimates:
            try:
                crop_estimate_jobs = self.picker_state().crop_estimate_jobs(active_only=True)
            except Exception:  # noqa: BLE001 - status should stay readable if crop state is unavailable.
                crop_estimate_jobs = []
            for job in crop_estimate_jobs:
                active_jobs.append(_crop_estimate_active_job(job))
        return active_jobs

    def picker_state(self) -> original_picker.PickerState:
        with self._picker_lock:
            if self._picker_state is None:
                self._picker_state = original_picker.PickerState(
                    original_picker.PickerConfig(
                        canonical_root=CANONICAL_ROOT,
                        queue_path=ORIGINAL_QUEUE,
                        batch_plan_path=ORIGINAL_BATCH_PLAN,
                    )
                )
            return self._picker_state

    def active_photo_index_folder(self) -> str:
        with self._picker_lock:
            folder = self._active_photo_index_folder
        return folder or original_picker.project_originals_source_folder(CANONICAL_ROOT)

    def broad_image_token(self, path_text: str) -> str:
        text = str(path_text or "").strip()
        if not text:
            return ""
        path = Path(text)
        token = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        with self._picker_lock:
            self._broad_image_paths[token] = path
        return token

    def broad_image_path(self, token: str) -> Path | None:
        with self._picker_lock:
            return self._broad_image_paths.get(token)

    def broad_preview_path(self, token: str, max_size: int | None) -> Path | None:
        path = self.broad_image_path(token)
        if path is None or max_size is None:
            return path
        picker_state = self.picker_state()
        picker_token = picker_state.image_token_for_path(path)
        return picker_state.preview_path(picker_token, max_size)

    def _update_active_photo_index_folder(self, step: str, payload: dict[str, Any]) -> None:
        if step not in {"match_easy_originals", "search_originals"}:
            return
        folder = str(payload.get("photo_index_folder", "")).strip()
        if step == "match_easy_originals" and not payload.get("limit_to_photo_index_folder"):
            folder = ""
        with self._picker_lock:
            self._active_photo_index_folder = folder

    def _invalidate_picker_state(self) -> None:
        with self._picker_lock:
            self._picker_state = None

    def _run_step_job(
        self,
        job_id: str,
        step: str,
        commands: list[list[str]],
        started: str,
        payload: dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        try:
            with self._lock:
                if cancel_event.is_set():
                    raise RuntimeError("Cancelled before start.")
                self._update_job(job_id, status="running")
                record = self._execute_commands(
                    step,
                    commands,
                    started,
                    payload=payload,
                    job_id=job_id,
                    cancel_event=cancel_event,
                    on_command_start=lambda command: self._update_job(
                        job_id,
                        current_command=" ".join(command),
                    ),
                    on_command_output=lambda outputs: self._update_job(
                        job_id,
                        outputs=outputs,
                    ),
                )
                self.history.append(record)
                _save_control_run_history(CONTROL_RUN_HISTORY, self.history)
                self._update_job(
                    job_id,
                    status=record["status"],
                    finished_at=record["finished_at"],
                    current_command="",
                    outputs=record["outputs"],
                    error=record.get("error", ""),
                    summary=record.get("summary", {}),
                )
        except Exception as exc:  # noqa: BLE001 - local app should surface readable errors.
            finished = dt.datetime.now(dt.UTC).isoformat()
            status = "cancelled" if cancel_event.is_set() else "fail"
            record = {
                "step": step,
                "status": status,
                "started_at": started,
                "finished_at": finished,
                "outputs": [],
                "error": str(exc),
                "summary": _workflow_run_summary(
                    step=step,
                    status=status,
                    payload={},
                    before={},
                    after={},
                    outputs=[],
                    error=str(exc),
                ),
            }
            with self._lock:
                self.history.append(record)
                _save_control_run_history(CONTROL_RUN_HISTORY, self.history)
            self._update_job(job_id, status=status, finished_at=finished, error=str(exc))
        finally:
            with self._job_lock:
                self._job_cancel_events.pop(job_id, None)
                self._job_processes.pop(job_id, None)

    def _execute_commands(
        self,
        step: str,
        commands: list[list[str]],
        started: str,
        payload: dict[str, Any] | None = None,
        job_id: str | None = None,
        cancel_event: threading.Event | None = None,
        on_command_start: Any | None = None,
        on_command_output: Any | None = None,
    ) -> dict[str, Any]:
        outputs = []
        payload = payload or {}
        if _step_replaces_original_queue(step, payload):
            _clear_original_search_outputs()
            self._invalidate_picker_state()
        before = _workflow_snapshot(step, include_details=False)
        for command in commands:
            if cancel_event and cancel_event.is_set():
                break
            if on_command_start:
                on_command_start(command)
            output = _run_command(
                command,
                timeout_seconds=_step_timeout_seconds(step),
                cancel_event=cancel_event,
                on_process_start=(
                    (lambda process: self._set_job_process(job_id, process))
                    if job_id
                    else None
                ),
                on_output=lambda text, command=command: (
                    on_command_output(outputs + [_command_output_record(command, None, text)])
                    if on_command_output
                    else None
                ),
            )
            outputs.append(output)
            if on_command_output:
                on_command_output(outputs)
            if output["returncode"] != 0:
                break
        cancelled = bool(cancel_event and cancel_event.is_set())
        status = "cancelled" if cancelled else "pass" if all(item["returncode"] == 0 for item in outputs) else "fail"
        if not cancelled and _step_writes_original_queue(step):
            self._invalidate_picker_state()
        after = before if cancelled else _workflow_snapshot(step, include_details=True)
        error = "Cancelled by user." if cancelled else ""
        return {
            "step": step,
            "status": status,
            "started_at": started,
            "finished_at": dt.datetime.now(dt.UTC).isoformat(),
            "outputs": outputs,
            "error": error,
            "summary": _workflow_run_summary(
                step=step,
                status=status,
                payload=payload,
                before=before,
                after=after,
                outputs=outputs,
                error=error,
            ),
        }

    def _update_job(self, job_id: str, **changes: Any) -> None:
        with self._job_lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.update(changes)
            job["last_update_at"] = dt.datetime.now(dt.UTC).isoformat()

    def _set_job_process(self, job_id: str | None, process: subprocess.Popen[Any]) -> None:
        if not job_id:
            return
        with self._job_lock:
            self._job_processes[job_id] = process


def _command_output_record(
    command: list[str],
    returncode: int | None,
    output: str,
) -> dict[str, Any]:
    return {
        "command": " ".join(command),
        "returncode": returncode,
        "output": output[-8000:],
    }


def _run_command(
    command: list[str],
    timeout_seconds: int,
    cancel_event: threading.Event | None = None,
    on_process_start: Any | None = None,
    on_output: Any | None = None,
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        cwd=Path.cwd(),
        stdin=subprocess.DEVNULL,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    if on_process_start:
        on_process_start(process)
    output_parts: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    assert process.stdout is not None
    try:
        while True:
            readable, _, _ = select.select([process.stdout], [], [], 0.5)
            if readable:
                line = process.stdout.readline()
                if line:
                    output_parts.append(line)
                    if on_output:
                        on_output("".join(output_parts)[-8000:])
            if cancel_event and cancel_event.is_set() and process.poll() is None:
                _terminate_process_group(process, force=False)
                output_parts.append("\nCancelled by user.\n")
                break
            if process.poll() is not None:
                rest = process.stdout.read()
                if rest:
                    output_parts.append(rest)
                    if on_output:
                        on_output("".join(output_parts)[-8000:])
                break
            if time.monotonic() > deadline:
                _terminate_process_group(process, force=True)
                output_parts.append(f"\nTimed out after {timeout_seconds} seconds.\n")
                break
    finally:
        process.stdout.close()
    wait_timeout = 30 if cancel_event and cancel_event.is_set() else 5
    try:
        returncode = process.wait(timeout=wait_timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process, force=True)
        output_parts.append("\nForced stop after cancellation did not exit cleanly.\n")
        returncode = process.wait()
    return _command_output_record(command, returncode, "".join(output_parts))


def _terminate_process_group(process: subprocess.Popen[Any], force: bool = False) -> None:
    signal_number = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(process.pid), signal_number)
        return
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _step_timeout_seconds(step: str) -> int:
    if step in {
        "build_photo_index",
        "refresh_photo_index_metadata",
        "broad_visual_index",
        "broad_visual_match",
        "rough_prefilter_build",
        "rough_visual_match",
    }:
        return 12 * 60 * 60
    return 60 * 60


def _step_replaces_original_queue(step: str, payload: dict[str, Any]) -> bool:
    return step in {"match_easy_originals", "search_originals"} and bool(payload.get("replace_existing_queue"))


def _step_writes_original_queue(step: str) -> bool:
    return step in {"match_easy_originals", "search_originals", "apply_original_decisions"}


def _clear_original_search_outputs() -> None:
    for path in (ORIGINAL_QUEUE, ORIGINAL_UNCLEAR_GROUPS, ORIGINAL_BATCH_PLAN):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _commands_for_step(step: str, payload: dict[str, Any]) -> list[list[str]]:
    python = sys.executable
    if step == "import_zips":
        return [
            [
                python,
                "project365_canonical_importer.py",
                "--import-dir",
                str(PROJECT365_PRO_EXPORT_ZIPS_DIR),
                "--canonical-root",
                str(CANONICAL_ROOT),
                "--report-dir",
                str(REPORT_DIR),
            ]
        ]
    if step == "match_easy_originals":
        payload = dict(payload)
        payload["replace_existing_queue"] = True
        if payload.get("limit_to_photo_index_folder"):
            folder = str(payload.get("photo_index_folder", "")).strip()
            if not folder:
                raise ValueError("Choose a photo-index folder before running filtered easy matches.")
        else:
            payload.pop("photo_index_folder", None)
        return _commands_for_step("search_originals", payload)
    if step == "search_originals":
        roots = [str(ORIGINAL_PHOTOS_ROOT)]
        for root in payload.get("search_roots", []):
            root_text = str(root).strip()
            if root_text and root_text not in roots:
                roots.append(root_text)
        command = [
            python,
            "project365_original_reference_pipeline.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "--report-dir",
            str(VERIFY_REPORT_DIR),
        ]
        for root in roots:
            command.extend(["--search-root", root])
        for entry_id in _payload_list(payload, "entry_ids"):
            command.extend(["--entry-id", entry_id])
        for entry_date in _payload_list(payload, "entry_dates"):
            command.extend(["--entry-date", entry_date])
        if str(payload.get("crawl_start_date", "")).strip():
            command.extend(["--start-date", str(payload["crawl_start_date"]).strip()])
        if str(payload.get("crawl_end_date", "")).strip():
            command.extend(["--end-date", str(payload["crawl_end_date"]).strip()])
        max_targets = payload.get("max_targets")
        if max_targets not in (None, ""):
            command.extend(["--max-targets", str(int(max_targets))])
        photo_index_folder = str(payload.get("photo_index_folder", "")).strip()
        if photo_index_folder:
            command.extend(["--photo-index-folder", photo_index_folder])
        if payload.get("replace_existing_queue"):
            command.append("--replace-existing-queue")
        if payload.get("include_low_quality_matches"):
            command.append("--include-low-quality-matches")
        if _has_targeted_search_scope(payload):
            command.append("--merge-existing-queue")
        return [command]
    if step == "build_photo_index":
        roots = []
        for root in payload.get("search_roots", []):
            root_text = _folder_root_from_text(str(root))
            if root_text and root_text not in roots:
                roots.append(root_text)
        if not roots:
            roots = _photo_library_index_roots(PHOTO_LIBRARY_INDEX)
        if not roots:
            roots.append(str(SOURCE_DATA_ROOT))
        reset_photo_index = bool(payload.get("reset_photo_index"))
        if reset_photo_index and payload.get("reset_confirmation") != "replace-photo-index":
            raise ValueError("Photo index replacement requires confirmation.")
        command = [
            python,
            "project365_photo_library_index.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
        ]
        if reset_photo_index:
            command.append("--reset")
        for root in roots:
            command.extend(["--index-root", root])
        return [command]
    if step == "refresh_photo_index_metadata":
        roots = []
        for root in payload.get("search_roots", []):
            root_text = _folder_root_from_text(str(root))
            if root_text and root_text not in roots:
                roots.append(root_text)
        if not roots:
            roots = _photo_library_index_roots(PHOTO_LIBRARY_INDEX)
        if not roots:
            raise ValueError("No existing photo index roots found. Choose folders and build the photo index first.")
        command = [
            python,
            "project365_photo_library_index.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
        ]
        if payload.get("reconcile_moves_only"):
            command.append("--reconcile-moves-only")
        for root in roots:
            command.extend(["--index-root", root])
        return [command]
    if step == "broad_visual_index":
        roots = []
        for root in payload.get("candidate_roots", []):
            root_text = _folder_root_from_text(str(root))
            if root_text and root_text not in roots:
                roots.append(root_text)
        confirmed_only = bool(payload.get("confirmed_only"))
        if confirmed_only:
            command = [
                python,
                "project365_broad_visual_match.py",
                "--canonical-root",
                str(CANONICAL_ROOT),
                "index-confirmed",
                "--density",
                str(int(payload.get("density") or broad_visual_match.DEFAULT_DENSITY)),
            ]
            for entry_id in _payload_list(payload, "entry_ids"):
                command.extend(["--entry-id", entry_id])
            start_date = str(payload.get("start_date", "")).strip()
            end_date = str(payload.get("end_date", "")).strip()
            if start_date:
                command.extend(["--start-date", start_date])
            if end_date:
                command.extend(["--end-date", end_date])
            needed_list = str(payload.get("broad_search_needed_list", "")).strip()
            if needed_list:
                command.extend(["--broad-search-needed-list", needed_list])
            if payload.get("include_low_quality_candidates"):
                command.append("--include-low-quality")
            if payload.get("overwrite_existing_fingerprints") or payload.get("rebuild_stale_descriptors"):
                command.append("--overwrite-existing")
            if payload.get("dry_run"):
                command.append("--dry-run")
            return [command]
        if not roots:
            roots = _photo_library_index_roots(PHOTO_LIBRARY_INDEX)
        if not roots:
            raise ValueError("Choose candidate folders or build the photo index first.")
        command = [
            python,
            "project365_broad_visual_match.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "index",
            "--density",
            str(int(payload.get("density") or broad_visual_match.DEFAULT_DENSITY)),
        ]
        for root in roots:
            command.extend(["--candidate-root", root])
        start_date = str(payload.get("start_date", "")).strip()
        end_date = str(payload.get("end_date", "")).strip()
        if start_date:
            command.extend(["--start-date", start_date])
        if end_date:
            command.extend(["--end-date", end_date])
        date_window_days = int(payload.get("date_window_days") or 0)
        if date_window_days:
            command.extend(["--date-window-days", str(date_window_days)])
        if payload.get("include_low_quality_candidates"):
            command.append("--include-low-quality")
        if payload.get("overwrite_existing_fingerprints") or payload.get("rebuild_stale_descriptors"):
            command.append("--overwrite-existing")
        if payload.get("dry_run"):
            command.append("--dry-run")
        return [command]
    if step == "broad_visual_match":
        if payload.get("confirmed_only"):
            raise ValueError(
                "Confirmed-original accuracy test is not an unresolved search. "
                "Use Measure accuracy to compare against already confirmed originals, "
                "or uncheck it before searching unresolved photos."
            )
        start_date = str(payload.get("start_date", "")).strip()
        end_date = str(payload.get("end_date", "")).strip()
        candidate_scope = str(payload.get("candidate_scope") or "date_window_limited")
        date_window_days = int(payload.get("date_window_days") or 0)
        if start_date and end_date and candidate_scope == "whole_indexed_library":
            raise ValueError(
                "Date-range unresolved search must use a candidate date window. "
                "Whole indexed library would compare each target to every stored fingerprint."
            )
        if candidate_scope == "date_window_limited" and date_window_days <= 0:
            raise ValueError("Candidate date window must be greater than 0 days.")
        command = [
            python,
            "project365_broad_visual_match.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "match",
            "--candidate-scope",
            candidate_scope,
            "--max-results",
            str(int(payload.get("max_results") or broad_visual_match.DEFAULT_TOP_N)),
            "--density",
            str(int(payload.get("density") or broad_visual_match.DEFAULT_DENSITY)),
        ]
        for entry_id in _payload_list(payload, "entry_ids"):
            command.extend(["--entry-id", entry_id])
        if start_date:
            command.extend(["--start-date", start_date])
        if end_date:
            command.extend(["--end-date", end_date])
        needed_list = str(payload.get("broad_search_needed_list", "")).strip()
        if needed_list:
            command.extend(["--broad-search-needed-list", needed_list])
        for root in payload.get("candidate_roots", []):
            root_text = _folder_root_from_text(str(root))
            if root_text:
                command.extend(["--candidate-root", root_text])
        command.extend(["--date-window-days", str(date_window_days)])
        if payload.get("include_low_quality_candidates"):
            command.append("--include-low-quality")
        if payload.get("resume_existing_run"):
            latest_run_id = broad_visual_match.current_match_run_id(
                BROAD_VISUAL_DB,
                broad_visual_match.CURRENT_BROAD_MATCH_SLOT,
            )
            if latest_run_id:
                command.extend(["--resume-run", latest_run_id])
        if payload.get("dry_run"):
            command.append("--dry-run")
        return [command]
    if step == "rough_prefilter_build":
        command = [
            python,
            "project365_broad_visual_match.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "prefilter",
            "--density",
            str(int(payload.get("density") or broad_visual_match.DEFAULT_DENSITY)),
            "--commit-interval",
            str(int(payload.get("commit_interval") or broad_visual_match.DEFAULT_PREFILTER_COMMIT_INTERVAL)),
        ]
        if payload.get("overwrite_existing_prefilter") or payload.get("rebuild_stale_prefilter"):
            command.append("--overwrite-existing")
        if payload.get("dry_run"):
            command.append("--dry-run")
        return [command]
    if step == "rough_visual_match":
        start_date = str(payload.get("start_date", "")).strip()
        end_date = str(payload.get("end_date", "")).strip()
        command = [
            python,
            "project365_broad_visual_match.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "match-no-date",
            "--max-results",
            str(int(payload.get("max_results") or broad_visual_match.DEFAULT_TOP_N)),
            "--shortlist-size",
            str(int(payload.get("shortlist_size") or broad_visual_match.DEFAULT_PREFILTER_SHORTLIST_SIZE)),
            "--per-band-hit-limit",
            str(int(payload.get("per_band_hit_limit") or broad_visual_match.DEFAULT_PREFILTER_BAND_HIT_LIMIT)),
            "--density",
            str(int(payload.get("density") or broad_visual_match.DEFAULT_DENSITY)),
        ]
        for entry_id in _payload_list(payload, "entry_ids"):
            command.extend(["--entry-id", entry_id])
        if start_date:
            command.extend(["--start-date", start_date])
        if end_date:
            command.extend(["--end-date", end_date])
        needed_list = str(payload.get("broad_search_needed_list", "")).strip()
        if needed_list:
            command.extend(["--broad-search-needed-list", needed_list])
        if payload.get("include_low_quality_candidates"):
            command.append("--include-low-quality")
        if payload.get("resume_existing_run"):
            latest_run_id = _latest_rough_no_date_run_id(BROAD_VISUAL_DB)
            if latest_run_id:
                command.extend(["--resume-run", latest_run_id])
        if payload.get("dry_run"):
            command.append("--dry-run")
        return [command]
    if step == "broad_visual_benchmark":
        command = [
            python,
            "project365_broad_visual_match.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "benchmark",
            "--max-results",
            str(int(payload.get("max_results") or broad_visual_match.DEFAULT_TOP_N)),
            "--report-dir",
            str(VERIFY_REPORT_DIR),
        ]
        for entry_id in _payload_list(payload, "entry_ids"):
            command.extend(["--entry-id", entry_id])
        start_date = str(payload.get("start_date", "")).strip()
        end_date = str(payload.get("end_date", "")).strip()
        if start_date:
            command.extend(["--start-date", start_date])
        if end_date:
            command.extend(["--end-date", end_date])
        needed_list = str(payload.get("broad_search_needed_list", "")).strip()
        if needed_list:
            command.extend(["--broad-search-needed-list", needed_list])
        return [command]
    if step == "rough_visual_benchmark":
        command = [
            python,
            "project365_broad_visual_match.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "benchmark",
            "--use-prefilter",
            "--max-results",
            str(int(payload.get("max_results") or broad_visual_match.DEFAULT_TOP_N)),
            "--shortlist-size",
            str(int(payload.get("shortlist_size") or broad_visual_match.DEFAULT_PREFILTER_SHORTLIST_SIZE)),
            "--per-band-hit-limit",
            str(int(payload.get("per_band_hit_limit") or broad_visual_match.DEFAULT_PREFILTER_BAND_HIT_LIMIT)),
            "--report-dir",
            str(VERIFY_REPORT_DIR),
        ]
        shortlist_sizes = str(payload.get("shortlist_sizes", "")).strip()
        if shortlist_sizes:
            command.extend(["--shortlist-sizes", shortlist_sizes])
        for entry_id in _payload_list(payload, "entry_ids"):
            command.extend(["--entry-id", entry_id])
        start_date = str(payload.get("start_date", "")).strip()
        end_date = str(payload.get("end_date", "")).strip()
        if start_date:
            command.extend(["--start-date", start_date])
        if end_date:
            command.extend(["--end-date", end_date])
        needed_list = str(payload.get("broad_search_needed_list", "")).strip()
        if needed_list:
            command.extend(["--broad-search-needed-list", needed_list])
        return [command]
    if step == "apply_original_decisions":
        return [
            [
                python,
                "project365_original_reference_pipeline.py",
                "--canonical-root",
                str(CANONICAL_ROOT),
                "--apply-reviewed",
                str(ORIGINAL_QUEUE),
            ],
            [
                python,
                "project365_original_reference_pipeline.py",
                "--canonical-root",
                str(CANONICAL_ROOT),
                "--report-dir",
                str(VERIFY_REPORT_DIR),
                "--prune-applied-reviewed",
                str(ORIGINAL_QUEUE),
            ]
        ]
    if step == "mark_original_fallback":
        entry_ids = _payload_list(payload, "entry_ids")
        entry_dates = _payload_list(payload, "entry_dates")
        crawl_start_date = str(payload.get("crawl_start_date", "")).strip()
        crawl_end_date = str(payload.get("crawl_end_date", "")).strip()
        if not entry_ids and not entry_dates and not (crawl_start_date and crawl_end_date):
            raise ValueError("Choose a batch before marking fallback.")
        command = [
            python,
            "project365_original_reference_pipeline.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "--review-queue",
            str(VERIFY_REPORT_DIR / "original_photo_review_queue.csv"),
            "--mark-fallback",
        ]
        for entry_id in entry_ids:
            command.extend(["--entry-id", entry_id])
        for entry_date in entry_dates:
            command.extend(["--entry-date", entry_date])
        if crawl_start_date:
            command.extend(["--start-date", crawl_start_date])
        if crawl_end_date:
            command.extend(["--end-date", crawl_end_date])
        max_targets = payload.get("max_targets")
        if max_targets not in (None, ""):
            command.extend(["--max-targets", str(int(max_targets))])
        return [command]
    if step == "score_alignment":
        entry_ids = _payload_list(payload, "entry_ids")
        entry_dates = _payload_list(payload, "entry_dates")
        if not entry_ids and not entry_dates:
            raise ValueError("Choose a batch before scoring alignment.")
        command = [
            python,
            "project365_original_reference_pipeline.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "--score-queue-alignment",
            str(ORIGINAL_QUEUE),
            "--progress-interval",
            "10",
        ]
        for entry_id in entry_ids:
            command.extend(["--entry-id", entry_id])
        for entry_date in entry_dates:
            command.extend(["--entry-date", entry_date])
        max_candidates = payload.get("max_candidates")
        if max_candidates not in (None, ""):
            command.extend(["--score-max-candidates", str(int(max_candidates))])
        return [command]
    if step == "face_tagging":
        return [
            [
                python,
                "project365_tag_enrichment.py",
                "--canonical-root",
                str(CANONICAL_ROOT),
                "--queue-out",
                str(TAG_QUEUE),
            ]
        ]
    if step == "import_digikam_people":
        xmp_roots = _payload_list(payload, "xmp_roots")
        suggestions_csv = str(payload.get("suggestions_csv", "")).strip()
        if not xmp_roots and not suggestions_csv:
            raise ValueError("Choose a digiKam XMP folder or suggestions CSV.")
        command = [
            python,
            "project365_digikam_people_importer.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "--report-out",
            str(DIGIKAM_PEOPLE_REPORT),
            "--queue-out",
            str(TAG_QUEUE),
        ]
        for root in xmp_roots:
            command.extend(["--xmp-root", root])
        if suggestions_csv:
            command.extend(["--suggestions-csv", suggestions_csv])
        return [command]
    if step == "generate_derivatives":
        command = [
            python,
            "project365_media_derivatives.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
            "--format",
            "jpeg",
            "--long-edge",
            "2560",
            "--quality",
            "88",
        ]
        start_date = str(payload.get("start_date", "")).strip()
        end_date = str(payload.get("end_date", "")).strip()
        if start_date:
            command.extend(["--start-date", start_date])
        if end_date:
            command.extend(["--end-date", end_date])
        if payload.get("force"):
            command.append("--force")
        return [command]
    if step == "generate_diarium_package":
        date_range = _entry_date_range(CANONICAL_ROOT / "canonical.db")
        start_date = str(payload.get("start_date") or date_range[0] or "1900-01-01")
        end_date = str(payload.get("end_date") or date_range[1] or "2999-12-31")
        limit = str(payload.get("limit") or 100000)
        package_name = str(payload.get("package_name") or f"project365_full_{start_date}_{end_date}_dayone.zip")
        return [
            [
                python,
                "project365_diarium_exporter.py",
                "--canonical-root",
                str(CANONICAL_ROOT),
                "--output-dir",
                str(DIARIUM_IMPORT_BATCH_DIR),
                "--package-name",
                package_name,
                "--start-date",
                start_date,
                "--end-date",
                end_date,
                "--limit",
                limit,
                "--derivative-policy",
                DERIVATIVE_POLICY,
            ]
        ]
    raise ValueError(f"Unsupported step: {step}")


def _payload_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def _has_targeted_search_scope(payload: dict[str, Any]) -> bool:
    return bool(
        _payload_list(payload, "entry_ids")
        or _payload_list(payload, "entry_dates")
        or str(payload.get("crawl_start_date", "")).strip()
        or str(payload.get("crawl_end_date", "")).strip()
        or str(payload.get("max_targets", "")).strip()
    )


def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "file_count": _file_count(path) if path.exists() else 0,
    }


def _file_count(path: Path) -> int:
    if path.is_file():
        return 1
    return sum(1 for item in path.rglob("*") if item.is_file())


def _load_control_run_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
    except (OSError, json.JSONDecodeError):
        return []
    return rows[-RUN_HISTORY_LIMIT:]


def _save_control_run_history(path: Path, history: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    visible = history[-RUN_HISTORY_LIMIT:]
    payload = "\n".join(json.dumps(_persistable_history_record(row), sort_keys=True) for row in visible)
    path.write_text(f"{payload}\n" if payload else "", encoding="utf-8")


def _persistable_history_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": record.get("step", ""),
        "status": record.get("status", ""),
        "started_at": record.get("started_at", ""),
        "finished_at": record.get("finished_at", ""),
        "error": record.get("error", ""),
        "outputs": record.get("outputs", []),
        "summary": record.get("summary", {}),
    }


def _broad_visual_status(
    include_monthly_coverage: bool = True,
    include_prefilter_stale_count: bool = False,
    include_review_runs: bool = True,
) -> dict[str, Any]:
    if not CANONICAL_ROOT.exists():
        return {
            "exists": False,
            "path": str(CANONICAL_ROOT / "broad_visual_match.sqlite"),
            "descriptor_count": 0,
            "descriptor_error_count": 0,
            "result_count": 0,
            "latest_run": {},
            "latest_no_date_run": {},
            "latest_index_run": {},
            "rough_prefilter": {},
            "latest_index_errors": [],
            "latest_benchmark": {},
            "monthly_coverage": [],
            "review_runs": [],
        }
    if (
        not include_monthly_coverage
        and not include_prefilter_stale_count
        and not include_review_runs
    ):
        status = broad_visual_match.rough_visual_status(BROAD_VISUAL_DB)
    else:
        status = broad_visual_match.broad_status(
            BROAD_VISUAL_DB,
            include_prefilter_stale_count=include_prefilter_stale_count,
        )
    status["latest_benchmark"] = _latest_broad_visual_benchmark(VERIFY_REPORT_DIR)
    status["monthly_coverage"] = _cached_broad_monthly_coverage() if include_monthly_coverage else []
    status["review_ready_run"] = broad_visual_match.review_ready_summary(
        CANONICAL_ROOT,
        BROAD_VISUAL_DB,
        picker_queue_path=ORIGINAL_QUEUE,
        slot=broad_visual_match.CURRENT_BROAD_MATCH_SLOT,
    )
    status["rough_review_ready_run"] = broad_visual_match.review_ready_summary(
        CANONICAL_ROOT,
        BROAD_VISUAL_DB,
        picker_queue_path=ORIGINAL_QUEUE,
        slot=broad_visual_match.CURRENT_ROUGH_MATCH_SLOT,
    )
    if include_review_runs:
        status["review_runs"] = broad_visual_match.review_runs(BROAD_VISUAL_DB, limit=10)
    else:
        status["review_runs"] = []
    return status


def _broad_visual_overview_status() -> dict[str, Any]:
    return _broad_visual_status(
        include_monthly_coverage=False,
        include_prefilter_stale_count=False,
        include_review_runs=True,
    )


def _photo_library_index_latest_run_status(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {"exists": False}
    try:
        with sqlite3.connect(index_path, timeout=1) as connection:
            _ensure_photo_library_index_run_table(connection)
            row = connection.execute(
                """
                SELECT
                    started_at,
                    finished_at,
                    roots,
                    reset,
                    scanned_file_count,
                    indexed_file_count,
                    skipped_file_count,
                    file_count_before,
                    file_count_after,
                    new_file_count
                FROM photo_library_index_runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error as exc:
        return {"exists": True, "error": str(exc), "recent_runs": []}
    if not row:
        return {"exists": True, "recent_runs": []}
    return {
        "exists": True,
        "recent_runs": [
            {
                "started_at": row[0],
                "finished_at": row[1],
                "roots": row[2],
                "reset": bool(row[3]),
                "scanned_file_count": row[4],
                "indexed_file_count": row[5],
                "skipped_file_count": row[6],
                "file_count_before": row[7],
                "file_count_after": row[8],
                "new_file_count": row[9],
            }
        ],
    }


def _attach_visual_job_progress(status: dict[str, Any]) -> None:
    broad = status.get("broad_visual_match") or {}
    prefilter = broad.get("rough_prefilter") or {}
    progress_by_step = {
        "broad_visual_index": ("broad_visual_index_run", broad.get("latest_index_run") or {}),
        "broad_visual_match": ("broad_visual_run", broad.get("latest_run") or {}),
        "rough_prefilter_build": ("rough_prefilter_run", prefilter.get("latest_run") or {}),
        "rough_visual_match": ("rough_visual_run", broad.get("latest_no_date_run") or {}),
    }
    for job in status.get("active_jobs") or []:
        target = progress_by_step.get(str(job.get("step") or ""))
        if not target:
            continue
        key, latest_run = target
        if _active_visual_run(latest_run):
            job[key] = latest_run


def _active_visual_run(run: dict[str, Any]) -> bool:
    if not run.get("run_id"):
        return False
    status = str(run.get("status") or "running")
    return status not in {"pass", "fail", "cancelled"} and not str(run.get("finished_at") or "")


def _cached_broad_monthly_coverage() -> list[dict[str, Any]]:
    key = (
        _file_cache_key(CANONICAL_ROOT / "canonical.db"),
        _file_cache_key(PHOTO_LIBRARY_INDEX),
        _file_cache_key(BROAD_VISUAL_DB),
    )
    if _BROAD_MONTHLY_COVERAGE_CACHE.get("key") != key:
        _BROAD_MONTHLY_COVERAGE_CACHE["key"] = key
        _BROAD_MONTHLY_COVERAGE_CACHE["rows"] = broad_visual_match.monthly_fingerprint_coverage(
            CANONICAL_ROOT,
            BROAD_VISUAL_DB,
        )
    return list(_BROAD_MONTHLY_COVERAGE_CACHE.get("rows") or [])


def _file_cache_key(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (str(path), 0, 0)
    return (str(path), int(stat.st_mtime_ns), int(stat.st_size))


def _latest_broad_visual_benchmark(report_dir: Path) -> dict[str, Any]:
    latest = report_dir / "broad_visual_benchmark_latest.json"
    candidates = [latest] if latest.exists() else []
    if not candidates:
        candidates = sorted(
            path
            for path in report_dir.glob("broad_visual_benchmark_*.json")
            if path.name != "broad_visual_benchmark_latest.json"
        )
    if not candidates:
        return {}
    report_path = candidates[-1]
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(report_path), "error": "Could not read latest benchmark report."}
    summary = dict(payload.get("summary") or {})
    summary["path"] = str(report_path)
    return summary


def _broad_photo_facts(path_text: str) -> dict[str, Any]:
    path = Path(str(path_text or ""))
    if not path_text or not path.exists() or not path.is_file():
        return {"mime_type": "", "byte_size": 0, "dimensions": "", "has_geolocation": False}
    mime_type, _encoding = mimetypes.guess_type(str(path))
    indexed_geo = _photo_index_has_geolocation(PHOTO_LIBRARY_INDEX, path)
    embedded_geo = False if indexed_geo else original_picker._has_embedded_geolocation(path)
    return {
        "mime_type": mime_type or "application/octet-stream",
        "byte_size": path.stat().st_size,
        "dimensions": original_picker._image_dimensions(path),
        "has_geolocation": bool(indexed_geo or embedded_geo),
    }


def _photo_index_has_geolocation(index_path: Path, path: Path) -> bool:
    if not index_path.exists():
        return False
    try:
        with sqlite3.connect(index_path, timeout=1) as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(photo_library_files)")
            }
            if not {"gps_latitude", "gps_longitude"}.issubset(columns):
                return False
            has_gps_select = "has_gps" if "has_gps" in columns else "0"
            row = connection.execute(
                f"""
                SELECT {has_gps_select}, gps_latitude, gps_longitude
                FROM photo_library_files
                WHERE path = ?
                LIMIT 1
                """,
                (str(path.resolve()),),
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and (row[0] or (row[1] is not None and row[2] is not None)))


def _photo_index_geolocation_by_path(index_path: Path, paths: list[str]) -> dict[str, bool]:
    normalized_paths = [str(Path(path).resolve()) for path in paths if str(path or "").strip()]
    if not normalized_paths or not index_path.exists():
        return {}
    try:
        with sqlite3.connect(index_path, timeout=1) as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(photo_library_files)")
            }
            if not {"gps_latitude", "gps_longitude"}.issubset(columns):
                return {}
            has_gps_select = "has_gps" if "has_gps" in columns else "0"
            placeholders = ", ".join("?" for _ in normalized_paths)
            rows = connection.execute(
                f"""
                SELECT path, {has_gps_select}, gps_latitude, gps_longitude
                FROM photo_library_files
                WHERE path IN ({placeholders})
                """,
                normalized_paths,
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        str(path): bool(has_gps or (latitude is not None and longitude is not None))
        for path, has_gps, latitude, longitude in rows
    }


def _broad_result_photo_facts(result: dict[str, Any], geolocation_by_path: dict[str, bool]) -> dict[str, Any]:
    width = int(result.get("candidate_width") or 0)
    height = int(result.get("candidate_height") or 0)
    path_text = str(result.get("candidate_path") or "").strip()
    resolved_path = str(Path(path_text).resolve()) if path_text else ""
    return {
        "mime_type": str(result.get("mime_type") or "application/octet-stream"),
        "byte_size": int(result.get("byte_size") or 0),
        "dimensions": f"{width} x {height}" if width and height else str(result.get("dimensions") or ""),
        "has_geolocation": bool(geolocation_by_path.get(resolved_path)),
    }


def _workflow_snapshot(step: str = "", include_details: bool = True) -> dict[str, Any]:
    snapshot = {
        "database": _database_snapshot(CANONICAL_ROOT / "canonical.db"),
    }
    if step in {"build_photo_index", "refresh_photo_index_metadata", ""}:
        snapshot["photo_index"] = _photo_index_snapshot(PHOTO_LIBRARY_INDEX)
    if step in {
        "broad_visual_index",
        "broad_visual_match",
        "broad_visual_benchmark",
        "rough_prefilter_build",
        "rough_visual_match",
        "rough_visual_benchmark",
        "",
    }:
        snapshot["broad_visual_match"] = _broad_visual_status(
            include_monthly_coverage=step in {"broad_visual_index", "broad_visual_match", "broad_visual_benchmark", ""},
            include_prefilter_stale_count=False,
            include_review_runs=step in {"broad_visual_index", "broad_visual_match", "broad_visual_benchmark", ""},
        )
    if include_details and step in {"match_easy_originals", "search_originals", ""}:
        snapshot["queue"] = _queue_status(ORIGINAL_QUEUE)
        snapshot["original_remainder_overview"] = _remainder_overview(ORIGINAL_UNCLEAR_GROUPS)
        snapshot["batch_plan"] = _batch_plan_status(ORIGINAL_BATCH_PLAN, ORIGINAL_SEARCH_ATTEMPTS)
    if include_details and step in {"face_tagging", "import_digikam_people", ""}:
        snapshot["tag_queue"] = _csv_count_snapshot(TAG_QUEUE)
        snapshot["digikam_people"] = _csv_status_snapshot(DIGIKAM_PEOPLE_REPORT, "status")
    if include_details and step in {"generate_diarium_package", ""}:
        snapshot["diarium_package"] = _diarium_package_status(DIARIUM_IMPORT_BATCH_DIR)
    return snapshot


def _database_snapshot(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"exists": False}
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        source_files = [
            {
                "source_path": row["source_path"],
                "zip_filename": row["zip_filename"],
                "month": row["month"],
                "zip_sha256": row["zip_sha256"],
                "zip_bytes": row["zip_bytes"],
                "validation_status": row["validation_status"],
                "import_batch_id": row["import_batch_id"],
                "imported_at": row["imported_at"],
            }
            for row in connection.execute(
                """
                SELECT source_path, zip_filename, month, zip_sha256, zip_bytes,
                    validation_status, import_batch_id, imported_at
                FROM source_files
                ORDER BY zip_filename, source_path
                """
            ).fetchall()
        ]
        role_counts = {
            str(row["role"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT role, COUNT(*) AS count
                FROM media_assets
                GROUP BY role
                """
            ).fetchall()
        }
        return {
            "exists": True,
            "entries": _table_count(connection, "entries"),
            "unique_days": _distinct_count(connection, "entries", "entry_date"),
            "entry_sources": _table_count(connection, "entry_sources"),
            "media_assets": _table_count(connection, "media_assets"),
            "source_files": _table_count(connection, "source_files"),
            "tags": _table_count(connection, "tags"),
            "people": _table_count(connection, "people"),
            "diarium_derivatives": role_counts.get("diarium_derivative", 0),
            "source_file_rows": source_files,
        }
    except sqlite3.Error as exc:
        return {"exists": True, "error": str(exc)}
    finally:
        connection.close()


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _distinct_count(connection: sqlite3.Connection, table: str, column: str) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(DISTINCT {column}) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _photo_index_snapshot(index_path: Path) -> dict[str, Any]:
    status = _photo_library_index_status(index_path)
    if not status.get("exists"):
        return status
    return {
        "exists": True,
        "file_count": int(status.get("file_count") or 0),
        "date_count": int(status.get("date_count") or 0),
        "capture_timestamp_count": int(status.get("capture_timestamp_count") or 0),
        "gps_coordinate_count": int(status.get("gps_coordinate_count") or 0),
        "roots": status.get("roots", []),
    }


def _csv_count_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "rows": 0}
    with path.open(newline="", encoding="utf-8") as handle:
        return {"exists": True, "rows": sum(1 for _ in csv.DictReader(handle))}


def _csv_status_snapshot(path: Path, status_field: str) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "rows": 0, "status_counts": {}}
    counts: dict[str, int] = {}
    rows = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            status = str(row.get(status_field, "") or "blank")
            counts[status] = counts.get(status, 0) + 1
    return {"exists": True, "rows": rows, "status_counts": counts}


def _workflow_run_summary(
    step: str,
    status: str,
    payload: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    outputs: list[dict[str, Any]],
    error: str,
) -> dict[str, Any]:
    metrics = _summary_metrics(step, before, after, outputs)
    archive_results_all = _import_archive_results(before, after, status) if step == "import_zips" else []
    archive_anomaly_count = sum(1 for item in archive_results_all if _is_archive_anomaly(item))
    archive_results = _visible_archive_results(archive_results_all)
    archive_changed_count = sum(1 for item in archive_results_all if item.get("status") != "unchanged/skipped")
    deltas = _summary_deltas(step, before, after)
    zero_change = _is_zero_change_summary(
        step=step,
        status=status,
        deltas=deltas,
        archive_changed_count=archive_changed_count,
        archive_results=archive_results,
    )
    return {
        "title": _step_title(step),
        "scope": _summary_scope(step, payload),
        "metrics": metrics,
        "deltas": deltas,
        "archive_results": archive_results,
        "archive_result_count": len(archive_results_all),
        "archive_result_hidden_count": max(0, archive_anomaly_count - len(archive_results)),
        "archive_anomaly_count": archive_anomaly_count,
        "archive_changed_count": archive_changed_count,
        "zero_change": zero_change,
        "zero_change_message": _zero_change_message(step) if zero_change else "",
        "warnings": _summary_warnings(step, outputs),
        "error": _safe_error(error, outputs),
    }


def _summary_warnings(step: str, outputs: list[dict[str, Any]]) -> list[str]:
    if step != "broad_visual_match":
        return []
    parsed = _parse_key_value_output(outputs)
    coverage_warning = parsed.get("coverage_warning", "")
    return [coverage_warning] if coverage_warning else []


def _summary_metrics(
    step: str,
    before: dict[str, Any],
    after: dict[str, Any],
    outputs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    parsed = _parse_key_value_output(outputs)
    db_before = before.get("database", {})
    db_after = after.get("database", {})
    index_after = after.get("photo_index", {})
    queue_after = after.get("queue", {})
    batch_after = after.get("batch_plan", {})
    tag_after = after.get("tag_queue", {})
    digikam_after = after.get("digikam_people", {})
    package_after = after.get("diarium_package", {})
    broad_after = after.get("broad_visual_match", {})
    common = {
        "entries": _delta_metric("Diary entries", db_before.get("entries"), db_after.get("entries")),
        "unique_days": _delta_metric("Unique diary days", db_before.get("unique_days"), db_after.get("unique_days")),
        "source_links": _delta_metric("Source links", db_before.get("entry_sources"), db_after.get("entry_sources")),
        "media_records": _delta_metric("Media records", db_before.get("media_assets"), db_after.get("media_assets")),
    }
    if step == "import_zips":
        return [
            {"label": "Archives scanned", "value": str(len(after.get("database", {}).get("source_file_rows", [])))},
            common["entries"],
            common["unique_days"],
            common["source_links"],
            common["media_records"],
        ]
    if step in {"build_photo_index", "refresh_photo_index_metadata"}:
        return [
            _delta_metric("Indexed photos", before.get("photo_index", {}).get("file_count"), index_after.get("file_count")),
            _delta_metric("Indexed dates", before.get("photo_index", {}).get("date_count"), index_after.get("date_count")),
            {"label": "New files in latest index run", "value": parsed.get("New files added", "0")},
            {"label": "Skipped files", "value": parsed.get("Skipped files", "0")},
        ]
    if step in {"match_easy_originals", "search_originals"}:
        pending = queue_after.get("pending_apply", {})
        return [
            {"label": "Queue rows", "value": str(queue_after.get("rows", 0))},
            {"label": "Pending picker decisions", "value": str(pending.get("decision_count", 0))},
            {"label": "Review-ready entries", "value": str(after.get("original_remainder_overview", {}).get("review_ready_entry_count", 0))},
            {"label": "Search batches", "value": str(batch_after.get("rows", 0))},
        ]
    if step == "broad_visual_index":
        latest_index = broad_after.get("latest_index_run") or {}
        return [
            {"label": "Stored fingerprints", "value": str(broad_after.get("descriptor_count", 0))},
            {"label": "New/rebuilt fingerprints", "value": str(latest_index.get("indexed_descriptor_count", 0))},
            {"label": "Reused fingerprints", "value": str(latest_index.get("reused_descriptor_count", 0))},
            {"label": "Fingerprint errors", "value": str(latest_index.get("error_count", 0))},
        ]
    if step == "broad_visual_match":
        latest_run = broad_after.get("latest_run") or {}
        processed = max(
            _to_int(latest_run.get("processed_target_count")),
            _to_int(latest_run.get("target_count")),
        )
        target_count = _to_int(latest_run.get("target_count"))
        entries_value = f"{processed}/{target_count}" if target_count else str(processed)
        return [
            {"label": "Entries searched", "value": entries_value},
            {"label": "Candidate comparisons", "value": str(latest_run.get("scanned_count", 0))},
            {"label": "Matched entries", "value": str(latest_run.get("matched_entries", 0))},
            {"label": "Saved candidate rows", "value": str(latest_run.get("result_count", 0))},
            {"label": "Search errors", "value": str(latest_run.get("error_count", 0))},
        ]
    if step == "broad_visual_benchmark":
        latest_benchmark = broad_after.get("latest_benchmark") or {}
        return [
            {"label": "Accuracy targets tested", "value": str(latest_benchmark.get("confirmed_count", 0))},
            {"label": "Top-1 hits", "value": str(latest_benchmark.get("top_1_count", 0))},
            {"label": "Top-5 hits", "value": str(latest_benchmark.get("top_5_count", 0))},
            {"label": "Top-N hits", "value": str(latest_benchmark.get("top_n_count", 0))},
            {"label": "Benchmark errors", "value": str(latest_benchmark.get("error_count", 0))},
        ]
    if step == "rough_prefilter_build":
        prefilter = broad_after.get("rough_prefilter") or {}
        latest = prefilter.get("latest_run") or {}
        return [
            {"label": "Stored prefilter rows", "value": str(prefilter.get("feature_count", 0))},
            {"label": "Stale/missing rows", "value": str(prefilter.get("stale_count", 0))},
            {"label": "New/rebuilt rows", "value": str(latest.get("indexed_feature_count", 0))},
            {"label": "Reused rows", "value": str(latest.get("reused_feature_count", 0))},
            {"label": "Prefilter errors", "value": str(latest.get("error_count", 0))},
        ]
    if step == "rough_visual_match":
        latest_run = broad_after.get("latest_no_date_run") or {}
        metrics = latest_run.get("prefilter_metrics") or {}
        processed = max(
            _to_int(latest_run.get("processed_target_count")),
            _to_int(latest_run.get("target_count")),
        )
        target_count = _to_int(latest_run.get("target_count"))
        entries_value = f"{processed}/{target_count}" if target_count else str(processed)
        return [
            {"label": "Entries searched", "value": entries_value},
            {"label": "Shortlisted candidates", "value": str(metrics.get("shortlist_size", 0))},
            {"label": "Dense descriptor loads", "value": str(latest_run.get("scanned_count", 0))},
            {"label": "Matched entries", "value": str(latest_run.get("matched_entries", 0))},
            {"label": "Saved candidate rows", "value": str(latest_run.get("result_count", 0))},
            {"label": "Capped band hits", "value": str(metrics.get("capped_band_count", 0))},
            {"label": "Search errors", "value": str(latest_run.get("error_count", 0))},
        ]
    if step == "rough_visual_benchmark":
        latest_benchmark = broad_after.get("latest_benchmark") or {}
        return [
            {"label": "Accuracy targets tested", "value": str(latest_benchmark.get("confirmed_count", 0))},
            {"label": "Prefilter recall @1000", "value": str(latest_benchmark.get("prefilter_recall_at_1000_count", 0))},
            {"label": "Top-1 hits", "value": str(latest_benchmark.get("top_1_count", 0))},
            {"label": "Top-N hits", "value": str(latest_benchmark.get("top_n_count", 0))},
            {"label": "Benchmark errors", "value": str(latest_benchmark.get("error_count", 0))},
        ]
    if step == "generate_derivatives":
        return [
            {"label": "Generated working copies", "value": parsed.get("Generated", "0")},
            {"label": "Skipped unchanged copies", "value": parsed.get("Skipped", "0")},
            {"label": "Not ready for export", "value": parsed.get("Not ready", "0")},
            {"label": "Not ready dates", "value": parsed.get("Not ready dates", "") or "none"},
            _delta_metric("Derivative records", db_before.get("diarium_derivatives"), db_after.get("diarium_derivatives")),
        ]
    if step in {"face_tagging", "import_digikam_people"}:
        metrics = [
            {"label": "Tag queue rows", "value": str(tag_after.get("rows", 0))},
            _delta_metric("People records", db_before.get("people"), db_after.get("people")),
            _delta_metric("Tag records", db_before.get("tags"), db_after.get("tags")),
        ]
        if step == "import_digikam_people":
            metrics.extend(
                [
                    {"label": "Suggested people", "value": parsed.get("Suggested people", "0")},
                    {"label": "Applied suggestions", "value": parsed.get("Applied suggestions", "0")},
                    {"label": "Import report rows", "value": str(digikam_after.get("rows", 0))},
                ]
            )
        return metrics
    if step == "generate_diarium_package":
        return [
            {"label": "Package entries", "value": str(package_after.get("journal_entries", parsed.get("Entries", "0")))},
            {"label": "Package photos", "value": str(package_after.get("photo_files", parsed.get("Media assets", "0")))},
            {"label": "Manifest rows", "value": str(package_after.get("manifest_rows", 0))},
            {"label": "Skipped entries", "value": parsed.get("Skipped entries", "0")},
            {"label": "Skipped dates", "value": parsed.get("Skipped dates", "") or "none"},
            {"label": "Photo readiness", "value": "ready" if package_after.get("photo_ready") else "attention"},
        ]
    return [common["entries"], common["media_records"]]


def _summary_deltas(step: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    db_before = before.get("database", {})
    db_after = after.get("database", {})
    index_before = before.get("photo_index", {})
    index_after = after.get("photo_index", {})
    broad_before = before.get("broad_visual_match", {})
    broad_after = after.get("broad_visual_match", {})
    values = {
        "diary_entries": _numeric_delta(db_before.get("entries"), db_after.get("entries")),
        "unique_diary_days": _numeric_delta(db_before.get("unique_days"), db_after.get("unique_days")),
        "source_links": _numeric_delta(db_before.get("entry_sources"), db_after.get("entry_sources")),
        "media_records": _numeric_delta(db_before.get("media_assets"), db_after.get("media_assets")),
        "photo_index_files": _numeric_delta(index_before.get("file_count"), index_after.get("file_count")),
        "photo_index_dates": _numeric_delta(index_before.get("date_count"), index_after.get("date_count")),
        "people": _numeric_delta(db_before.get("people"), db_after.get("people")),
        "tags": _numeric_delta(db_before.get("tags"), db_after.get("tags")),
        "diarium_derivatives": _numeric_delta(db_before.get("diarium_derivatives"), db_after.get("diarium_derivatives")),
        "broad_descriptors": _numeric_delta(broad_before.get("descriptor_count"), broad_after.get("descriptor_count")),
        "broad_results": _numeric_delta(broad_before.get("result_count"), broad_after.get("result_count")),
        "rough_prefilter": _numeric_delta(
            (broad_before.get("rough_prefilter") or {}).get("feature_count"),
            (broad_after.get("rough_prefilter") or {}).get("feature_count"),
        ),
    }
    if step == "import_zips":
        return {key: values[key] for key in ("diary_entries", "unique_diary_days", "source_links", "media_records")}
    if step in {"build_photo_index", "refresh_photo_index_metadata"}:
        return {key: values[key] for key in ("photo_index_files", "photo_index_dates")}
    if step in {"face_tagging", "import_digikam_people"}:
        return {key: values[key] for key in ("people", "tags")}
    if step == "generate_derivatives":
        return {"diarium_derivatives": values["diarium_derivatives"]}
    if step == "broad_visual_index":
        return {"broad_descriptors": values["broad_descriptors"]}
    if step == "broad_visual_match":
        return {"broad_results": values["broad_results"]}
    if step == "rough_prefilter_build":
        return {"rough_prefilter": values["rough_prefilter"]}
    if step == "rough_visual_match":
        return {"broad_results": values["broad_results"]}
    if step in {"broad_visual_benchmark", "rough_visual_benchmark"}:
        return {}
    return values


def _summary_scope(step: str, payload: dict[str, Any]) -> list[str]:
    if step == "import_zips":
        return [f"ZIP folder: {PROJECT365_PRO_EXPORT_ZIPS_DIR}"]
    if step == "build_photo_index":
        roots = payload.get("search_roots") or [str(SOURCE_DATA_ROOT)]
        reset = "replace existing index" if payload.get("reset_photo_index") else "incremental"
        return [f"Folders: {'; '.join(str(root) for root in roots)}", f"Mode: {reset}"]
    if step == "refresh_photo_index_metadata":
        roots = payload.get("search_roots") or []
        mode = "reconcile moved/renamed files" if payload.get("reconcile_moves_only") else "refresh metadata"
        if roots:
            return [f"Folders: {'; '.join(str(root) for root in roots)}", f"Mode: {mode}"]
        return ["Existing indexed folders", f"Mode: {mode}"]
    if step == "match_easy_originals":
        folder = str(payload.get("photo_index_folder") or "").strip()
        return [f"Photo index folder: {folder}" if folder else "Photo index folder: all indexed folders"]
    if step == "broad_visual_index":
        roots = payload.get("candidate_roots") or []
        return [
            f"Candidate roots: {'; '.join(str(root) for root in roots) if roots else 'existing photo-index roots'}",
            f"Density: {payload.get('density') or broad_visual_match.DEFAULT_DENSITY}",
        ]
    if step == "broad_visual_match":
        scope = str(payload.get("target_scope") or "all_unresolved")
        candidate_scope = str(payload.get("candidate_scope") or "date_window_limited")
        date_window = payload.get("date_window_days")
        items = [
            f"Target scope: {scope}",
            f"Candidate scope: {candidate_scope}",
            f"Max results: {payload.get('max_results') or broad_visual_match.DEFAULT_TOP_N}",
        ]
        if date_window not in (None, ""):
            items.insert(2, f"Candidate date window: +/- {date_window} days")
        return items
    if step == "broad_visual_benchmark":
        return [f"Max results: {payload.get('max_results') or broad_visual_match.DEFAULT_TOP_N}"]
    if step == "rough_prefilter_build":
        return [
            f"Density: {payload.get('density') or broad_visual_match.DEFAULT_DENSITY}",
            f"Mode: {'rebuild' if payload.get('overwrite_existing_prefilter') else 'incremental'}",
        ]
    if step == "rough_visual_match":
        return [
            f"Target scope: {payload.get('target_scope') or 'all_unresolved'}",
            f"Shortlist size: {payload.get('shortlist_size') or broad_visual_match.DEFAULT_PREFILTER_SHORTLIST_SIZE}",
            f"Max results: {payload.get('max_results') or broad_visual_match.DEFAULT_TOP_N}",
        ]
    if step == "rough_visual_benchmark":
        return [
            f"Shortlist size: {payload.get('shortlist_size') or broad_visual_match.DEFAULT_PREFILTER_SHORTLIST_SIZE}",
            f"Max results: {payload.get('max_results') or broad_visual_match.DEFAULT_TOP_N}",
        ]
    if step == "import_digikam_people":
        roots = payload.get("xmp_roots") or []
        csv_path = str(payload.get("suggestions_csv") or "").strip()
        parts = []
        if roots:
            parts.append(f"XMP folders: {'; '.join(str(root) for root in roots)}")
        if csv_path:
            parts.append(f"CSV: {csv_path}")
        return parts or ["No input scope"]
    if step == "generate_diarium_package":
        return [
            f"Date range: {payload.get('start_date') or 'auto'} to {payload.get('end_date') or 'auto'}",
            f"Limit: {payload.get('limit') or 100000}",
        ]
    return []


def _import_archive_results(
    before: dict[str, Any],
    after: dict[str, Any],
    status: str,
) -> list[dict[str, str]]:
    before_rows = {
        row.get("source_path"): row
        for row in before.get("database", {}).get("source_file_rows", [])
        if row.get("source_path")
    }
    after_rows = after.get("database", {}).get("source_file_rows", [])
    results = []
    for row in after_rows:
        old = before_rows.get(row.get("source_path"))
        if old is None:
            archive_status = "new/imported"
        elif old.get("zip_sha256") != row.get("zip_sha256") or old.get("zip_bytes") != row.get("zip_bytes"):
            archive_status = "changed/re-imported"
        else:
            archive_status = "unchanged/skipped"
        results.append(
            {
                "filename": str(row.get("zip_filename") or Path(str(row.get("source_path", ""))).name),
                "status": archive_status,
                "month": str(row.get("month") or ""),
                "validation_status": str(row.get("validation_status") or ""),
            }
        )
    if status != "pass" and not results:
        results.extend(_validation_report_archive_results(REPORT_DIR / "source_files.csv"))
    return results


def _visible_archive_results(results: list[dict[str, str]]) -> list[dict[str, str]]:
    anomalies = [item for item in results if _is_archive_anomaly(item)]
    return sorted(
        anomalies,
        key=lambda item: (item.get("validation_status", ""), item.get("filename", "")),
    )[:ARCHIVE_RESULT_LIMIT]


def _is_archive_anomaly(item: dict[str, str]) -> bool:
    status = str(item.get("status") or "").lower()
    validation_status = str(item.get("validation_status") or "").lower()
    return status in {"failed", "invalid"} or validation_status not in {"", "pass"}


def _validation_report_archive_results(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    results = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            validation_status = str(row.get("validation_status") or "").strip()
            if validation_status not in {"pass", "true"}:
                results.append(
                    {
                        "filename": row.get("zip_filename", ""),
                        "status": "invalid" if validation_status else "failed",
                        "month": row.get("month", ""),
                        "validation_status": validation_status,
                    }
                )
    return results[:100]


def _parse_key_value_output(outputs: list[dict[str, Any]]) -> dict[str, str]:
    values = {}
    for output in outputs:
        for line in str(output.get("output", "")).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                values[key] = value
    return values


def _delta_metric(label: str, before: Any, after: Any) -> dict[str, str]:
    before_number = _to_int(before)
    after_number = _to_int(after)
    delta = after_number - before_number
    suffix = f" ({delta:+d})" if delta else " (+0)"
    return {"label": label, "value": f"{after_number}{suffix}"}


def _numeric_delta(before: Any, after: Any) -> int:
    return _to_int(after) - _to_int(before)


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_meaningful_delta(deltas: dict[str, int]) -> bool:
    return any(value != 0 for value in deltas.values())


def _is_zero_change_summary(
    step: str,
    status: str,
    deltas: dict[str, int],
    archive_changed_count: int,
    archive_results: list[dict[str, str]],
) -> bool:
    return (
        step == "import_zips"
        and status == "pass"
        and not _has_meaningful_delta(deltas)
        and not archive_changed_count
        and not archive_results
    )


def _zero_change_message(step: str) -> str:
    if step == "import_zips":
        return "Known ZIP inputs were unchanged or skipped."
    return ""


def _has_changed_archive(archive_results: list[dict[str, str]]) -> bool:
    return any(item.get("status") != "unchanged/skipped" for item in archive_results)


def _safe_error(error: str, outputs: list[dict[str, Any]]) -> str:
    if error:
        return error[-2000:]
    failing = [item for item in outputs if item.get("returncode") not in {0, None}]
    if not failing:
        return ""
    return str(failing[-1].get("output", ""))[-2000:]


def _step_title(step: str) -> str:
    titles = {
        "import_zips": "Import zips",
        "build_photo_index": "Build photo index",
        "refresh_photo_index_metadata": "Refresh photo index metadata",
        "match_easy_originals": "Original-photo review",
        "media_dedupe_review": "Media dedupe review",
        "search_originals": "Original-photo search",
        "broad_visual_index": "Broad descriptor index",
        "broad_visual_match": "Broad visual match",
        "broad_visual_benchmark": "Broad visual benchmark",
        "rough_prefilter_build": "Rough visual prefilter",
        "rough_visual_match": "No-date visual match",
        "rough_visual_benchmark": "No-date visual benchmark",
        "diary_enrichment": "Diary enrichment",
        "generate_derivatives": "Working photo copies",
        "face_tagging": "Build tag queue",
        "import_digikam_people": "Import digiKam suggestions",
        "generate_diarium_package": "Diarium import package",
    }
    return titles.get(step, step.replace("_", " "))


def _media_dedupe_workflow_record() -> dict[str, Any] | None:
    db_path = media_dedupe.decision_db_path(CANONICAL_ROOT)
    if not db_path.exists():
        return None
    counts = {
        "photo_candidates": 0,
        "video_candidates": 0,
        "photo_decisions": 0,
        "video_decisions": 0,
    }
    try:
        with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=1) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "media_dedupe_candidates" in tables:
                for media_type, count in connection.execute(
                    "SELECT media_type, COUNT(*) FROM media_dedupe_candidates GROUP BY media_type"
                ):
                    key = f"{media_type}_candidates"
                    if key in counts:
                        counts[key] = int(count or 0)
            if "media_dedupe_decisions" in tables:
                for media_type, count in connection.execute(
                    "SELECT media_type, COUNT(*) FROM media_dedupe_decisions GROUP BY media_type"
                ):
                    key = f"{media_type}_decisions"
                    if key in counts:
                        counts[key] = int(count or 0)
    except sqlite3.Error:
        return None
    return _workflow_record(
        "media_dedupe_review",
        started_at=_path_mtime_iso(db_path),
        metrics=[
            {"label": "Photo candidate pairs", "value": counts["photo_candidates"]},
            {"label": "Photo decisions", "value": counts["photo_decisions"]},
            {"label": "Video candidate pairs", "value": counts["video_candidates"]},
            {"label": "Video decisions", "value": counts["video_decisions"]},
        ],
        scope=[f"Decision database: {db_path}"],
        zero_change=not (counts["photo_decisions"] or counts["video_decisions"]),
        zero_change_message="No dedupe decisions recorded yet.",
    )


def _path_mtime_iso(path: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC).isoformat()
    except OSError:
        return ""


def _workflow_record(
    step: str,
    *,
    status: str = "pass",
    started_at: str = "",
    finished_at: str = "",
    metrics: list[dict[str, Any]] | None = None,
    scope: list[str] | None = None,
    zero_change: bool = False,
    zero_change_message: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "step": step,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at or started_at,
        "error": error,
        "outputs": [],
        "summary": {
            "title": _step_title(step),
            "scope": scope or [],
            "metrics": metrics or [],
            "deltas": [],
            "archive_results": [],
            "archive_result_count": 0,
            "archive_result_hidden_count": 0,
            "archive_anomaly_count": 0,
            "archive_changed_count": 0,
            "zero_change": zero_change,
            "zero_change_message": zero_change_message,
            "warnings": [],
            "error": error,
        },
    }


def _top_metrics(database: dict[str, Any], photo_index: dict[str, Any]) -> dict[str, Any]:
    project365_total = int(database.get("project365_entry_count") or 0)
    without_identified_original = int(database.get("without_identified_original_count") or 0)
    return {
        "unique_diary_days": int(database.get("unique_day_count") or 0),
        "diary_entries": int(database.get("entry_count") or 0),
        "photo_index_files": int(photo_index.get("file_count") or 0),
        "project365_entries": project365_total,
        "missing_photos": without_identified_original,
        "missing_photos_percent": _percentage(without_identified_original, project365_total),
        "associated_photos": int(database.get("associated_photo_count") or 0),
        "without_identified_original": without_identified_original,
        "without_identified_original_percent": _percentage(
            without_identified_original,
            project365_total,
        ),
    }


def _workflow_history(
    history: list[dict[str, Any]],
    owners: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if owners is None or "media_dedupe_review" in owners:
        media_dedupe_record = _media_dedupe_workflow_record()
        if media_dedupe_record is not None:
            grouped.setdefault("media_dedupe_review", []).append(media_dedupe_record)
    for record in reversed(history):
        step = str(record.get("step") or "")
        if not step:
            continue
        if owners is not None and _owner_step(step) not in owners:
            continue
        grouped.setdefault(step, [])
        if len(grouped[step]) < 5:
                grouped[step].append(_persistable_history_record(record))
    return grouped


def _current_workflow_history(
    history: list[dict[str, Any]],
    status: dict[str, Any],
    owners: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    grouped = _workflow_history(history, owners)

    def wants(step: str) -> bool:
        return owners is None or step in owners or _owner_step(step) in owners or "workflow_overview" in owners

    def prepend(record: dict[str, Any] | None) -> None:
        if not record:
            return
        step = str(record.get("step") or "")
        if not step or not wants(step):
            return
        grouped.setdefault(step, [])
        grouped[step].insert(0, record)

    broad_status = status.get("broad_visual_match", {})
    if isinstance(broad_status, dict):
        prepend(_visual_run_workflow_record("broad_visual_match", broad_status.get("review_ready_run", {})))
        prepend(_visual_run_workflow_record("rough_visual_match", broad_status.get("rough_review_ready_run", {})))

    photo_index = status.get("photo_library_index", {})
    if isinstance(photo_index, dict):
        recent_runs = photo_index.get("recent_runs") or []
        latest_run = recent_runs[0] if recent_runs else photo_index.get("latest_run", {})
        if isinstance(latest_run, dict):
            prepend(
                _workflow_record(
                    "build_photo_index",
                    started_at=str(latest_run.get("started_at") or latest_run.get("finished_at") or ""),
                    finished_at=str(latest_run.get("finished_at") or latest_run.get("started_at") or ""),
                    metrics=[
                        {"label": "Indexed files", "value": latest_run.get("file_count_after", "")},
                        {"label": "New files", "value": latest_run.get("new_file_count", "")},
                    ],
                    scope=[str(latest_run.get("roots") or "")] if latest_run.get("roots") else [],
                )
            )

    original_attempt = status.get("original_search_attempts", {})
    latest_attempt = original_attempt.get("latest", {}) if isinstance(original_attempt, dict) else {}
    if isinstance(latest_attempt, dict) and latest_attempt:
        prepend(
            _workflow_record(
                "search_originals",
                started_at=str(latest_attempt.get("started_at") or latest_attempt.get("finished_at") or ""),
                finished_at=str(latest_attempt.get("finished_at") or latest_attempt.get("started_at") or ""),
                scope=[str(latest_attempt.get("search_roots") or "")] if latest_attempt.get("search_roots") else [],
            )
        )

    crop_status = status.get("crop_confirmation", {})
    if isinstance(crop_status, dict) and crop_status:
        prepend(
            _workflow_record(
                "crop_confirmation",
                metrics=[
                    {"label": "Queued", "value": crop_status.get("queued_count", 0)},
                    {"label": "Estimated crops", "value": crop_status.get("estimated_crop_count", 0)},
                    {"label": "Confirmed crops", "value": crop_status.get("confirmed_crop_count", 0)},
                ],
            )
        )

    database = status.get("database", {})
    if isinstance(database, dict) and any(str(database.get(key, "")) not in {"", "0"} for key in (
        "working_copy_source_count",
        "working_copy_ready_count",
        "working_copy_current_count",
        "working_copy_not_ready_count",
        "working_copy_needs_update_count",
    )):
        prepend(
            _workflow_record(
                "generate_derivatives",
                metrics=[
                    {"label": "Working-copy sources", "value": database.get("working_copy_source_count", 0)},
                    {"label": "Ready", "value": database.get("working_copy_ready_count", 0)},
                ],
            )
        )

    diarium_package = status.get("diarium_package", {})
    if isinstance(diarium_package, dict) and diarium_package.get("path"):
        prepend(
            _workflow_record(
                "generate_diarium_package",
                started_at=_path_mtime_iso(Path(str(diarium_package.get("path")))),
                metrics=[
                    {"label": "Journal entries", "value": diarium_package.get("journal_entries", 0)},
                    {"label": "Photo files", "value": diarium_package.get("photo_files", 0)},
                ],
                scope=[str(diarium_package.get("filename") or diarium_package.get("path") or "")],
            )
        )

    if wants("import_zips"):
        prepend(_latest_import_zip_workflow_record())
    if wants("face_tagging") and (TAG_QUEUE.exists() or DIGIKAM_PEOPLE_REPORT.exists()):
        prepend(
            _workflow_record(
                "face_tagging",
                started_at=max((_path_mtime_iso(path) for path in (TAG_QUEUE, DIGIKAM_PEOPLE_REPORT) if path.exists()), default=""),
            )
        )
    return grouped


def _visual_run_workflow_record(step: str, run: Any) -> dict[str, Any] | None:
    if not isinstance(run, dict) or not run:
        return None
    processed = int(run.get("processed_target_count") or 0)
    total = int(run.get("target_count") or 0)
    return _workflow_record(
        step,
        status=str(run.get("status") or "pass"),
        started_at=str(run.get("started_at") or run.get("finished_at") or ""),
        finished_at=str(run.get("finished_at") or run.get("started_at") or ""),
        metrics=[
            {"label": "Entries searched", "value": f"{processed}/{total}" if total else str(processed)},
            {"label": "Matched entries", "value": run.get("matched_entries", 0)},
            {"label": "Saved candidate rows", "value": str(run.get("result_count", 0))},
            {"label": "Candidates scanned", "value": run.get("scanned_count", 0)},
        ],
    )


def _latest_import_zip_workflow_record() -> dict[str, Any] | None:
    db_path = CANONICAL_ROOT / "canonical.db"
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path, timeout=1) as connection:
            row = connection.execute(
                """
                SELECT started_at, finished_at, source_file_count, entry_count, media_asset_count, status, import_dir
                FROM import_batches
                WHERE source_type = 'project365_zip'
                ORDER BY COALESCE(finished_at, started_at) DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return _workflow_record(
        "import_zips",
        status=str(row[5] or "pass"),
        started_at=str(row[0] or row[1] or ""),
        finished_at=str(row[1] or row[0] or ""),
        metrics=[
            {"label": "Source files", "value": row[2]},
            {"label": "Entries", "value": row[3]},
            {"label": "Media records", "value": row[4]},
        ],
        scope=[str(row[6] or "")] if row[6] else [],
    )


def _database_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {}
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT role, review_status, COUNT(*) AS count
            FROM media_assets
            GROUP BY role, review_status
            ORDER BY role, review_status
            """
        ).fetchall()
        entry_count = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        unique_day_count = connection.execute(
            "SELECT COUNT(DISTINCT entry_date) FROM entries"
        ).fetchone()[0]
        source_rows = connection.execute(
            """
            SELECT source_app, COUNT(*) AS count
            FROM entries
            GROUP BY source_app
            ORDER BY source_app
            """
        ).fetchall()
        project365_entry_count = connection.execute(
            "SELECT COUNT(*) FROM entries WHERE source_app = 'project365'"
        ).fetchone()[0]
        project365_photo_gaps = _project365_photo_gap_counts(connection)
        project365_date_row = connection.execute(
            """
            SELECT MIN(entry_date), MAX(entry_date)
            FROM entries
            WHERE source_app = 'project365'
            """
        ).fetchone()
        date_range = _entry_date_range(db_path)
        derivative_readiness = derivative_readiness_summary(db_path.parent)
    finally:
        connection.close()
    return {
        "entry_count": entry_count,
        "unique_day_count": unique_day_count,
        "project365_entry_count": project365_entry_count,
        "non_project365_entry_count": entry_count - project365_entry_count,
        "project365_missing_photo_count": project365_photo_gaps["without_identified_original"],
        "project365_missing_photo_percent": _percentage(
            project365_photo_gaps["without_identified_original"],
            project365_entry_count,
        ),
        "identified_original_count": project365_photo_gaps["identified_originals"],
        "associated_photo_count": project365_photo_gaps["associated_photos"],
        "without_identified_original_count": project365_photo_gaps["without_identified_original"],
        "without_identified_original_percent": _percentage(
            project365_photo_gaps["without_identified_original"],
            project365_entry_count,
        ),
        "date_start": date_range[0],
        "date_end": date_range[1],
        "project365_date_start": project365_date_row[0] if project365_date_row else None,
        "project365_date_end": project365_date_row[1] if project365_date_row else None,
        "working_copy_source_count": derivative_readiness.source_count,
        "working_copy_ready_count": derivative_readiness.ready_count,
        "working_copy_not_ready_count": derivative_readiness.not_ready_count,
        "working_copy_current_count": derivative_readiness.current_count,
        "working_copy_needs_update_count": derivative_readiness.needs_update_count,
        "source_counts": [dict(row) for row in source_rows],
        "media": [dict(row) for row in rows],
    }


def _working_copy_readiness_status(start_date: str = "", end_date: str = "") -> dict[str, Any]:
    readiness = derivative_readiness_summary(
        CANONICAL_ROOT,
        start_date=str(start_date or "").strip(),
        end_date=str(end_date or "").strip(),
    )
    return {
        "scope": {
            "start_date": str(start_date or "").strip(),
            "end_date": str(end_date or "").strip(),
        },
        "database": {
            "working_copy_source_count": readiness.source_count,
            "working_copy_ready_count": readiness.ready_count,
            "working_copy_not_ready_count": readiness.not_ready_count,
            "working_copy_current_count": readiness.current_count,
            "working_copy_needs_update_count": readiness.needs_update_count,
        },
    }


def _project365_photo_gap_counts(connection: sqlite3.Connection) -> dict[str, int]:
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(media_assets)").fetchall()
    }
    if "entry_id" not in columns:
        return {
            "identified_originals": 0,
            "associated_photos": 0,
            "without_identified_original": 0,
        }
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS project365_entries,
            SUM(
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM media_assets AS original_media
                    WHERE original_media.entry_id = entries.id
                        AND original_media.role = 'external_original_reference'
                        AND original_media.review_status = 'confirmed'
                ) THEN 1 ELSE 0 END
            ) AS identified_originals,
            SUM(
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM media_assets AS associated_media
                    WHERE associated_media.entry_id = entries.id
                        AND associated_media.role = 'external_original_associated_photo'
                        AND associated_media.review_status = 'confirmed'
                ) THEN 1 ELSE 0 END
            ) AS associated_photos
        FROM entries
        WHERE entries.source_app = 'project365'
        """
    ).fetchone()
    project365_entries = int(row["project365_entries"] or 0)
    identified_originals = int(row["identified_originals"] or 0)
    return {
        "identified_originals": identified_originals,
        "associated_photos": int(row["associated_photos"] or 0),
        "without_identified_original": max(0, project365_entries - identified_originals),
    }


def _percentage(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((count / total) * 100, 1)


def _entry_date_range(db_path: Path) -> tuple[str | None, str | None]:
    if not db_path.exists():
        return (None, None)
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT MIN(entry_date), MAX(entry_date) FROM entries").fetchone()
    finally:
        connection.close()
    return (row[0], row[1])


def _queue_status(queue_path: Path) -> dict[str, Any]:
    if not queue_path.exists():
        return {"exists": False}
    with queue_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        fieldnames = next(reader, [])
        entry_id_index = _field_index(fieldnames, "entry_id")
        candidate_path_index = _field_index(fieldnames, "candidate_path")
        review_decision_index = _field_index(fieldnames, "review_decision")
        rows = 0
        decisions: dict[str, int] = {}
        selected_count = 0
        rejected_count = 0
        fallback_entry_ids: set[str] = set()
        pending_entry_ids: set[str] = set()
        for row in reader:
            rows += 1
            decision = _csv_value(row, review_decision_index).strip().lower() or "blank"
            decisions[decision] = decisions.get(decision, 0) + 1
            entry_id = _csv_value(row, entry_id_index).strip()
            candidate_path = _csv_value(row, candidate_path_index).strip()
            if decision in ACCEPT_DECISIONS and candidate_path:
                selected_count += 1
                if entry_id:
                    pending_entry_ids.add(entry_id)
            elif decision in REJECT_DECISIONS and candidate_path:
                rejected_count += 1
                if entry_id:
                    pending_entry_ids.add(entry_id)
            elif decision in FALLBACK_DECISIONS and entry_id:
                fallback_entry_ids.add(entry_id)
                pending_entry_ids.add(entry_id)
    fallback_count = len(fallback_entry_ids)
    return {
        "exists": True,
        "rows": rows,
        "decisions": decisions,
        "pending_apply": {
            "entry_count": len(pending_entry_ids),
            "selected_count": selected_count,
            "rejected_count": rejected_count,
            "fallback_count": fallback_count,
            "decision_count": selected_count + rejected_count + fallback_count,
        },
    }


def _field_index(fieldnames: list[str], fieldname: str) -> int | None:
    try:
        return fieldnames.index(fieldname)
    except ValueError:
        return None


def _csv_value(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def _query_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _photo_library_index_status(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {"exists": False}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(index_path, timeout=1)
        connection.execute("PRAGMA busy_timeout = 1000")
        _ensure_photo_library_index_run_table(connection)
        file_count = connection.execute(
            "SELECT COUNT(*) FROM photo_library_files"
        ).fetchone()[0]
        date_count = connection.execute(
            "SELECT COUNT(DISTINCT date) FROM photo_library_dates"
        ).fetchone()[0]
        file_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(photo_library_files)")
        }
        capture_timestamp_count = (
            connection.execute(
                "SELECT COUNT(*) FROM photo_library_files WHERE capture_timestamp != ''"
            ).fetchone()[0]
            if "capture_timestamp" in file_columns
            else 0
        )
        gps_coordinate_count = 0
        if {"gps_latitude", "gps_longitude"}.issubset(file_columns):
            has_gps_clause = "has_gps != 0 OR " if "has_gps" in file_columns else ""
            gps_coordinate_count = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM photo_library_files
                WHERE {has_gps_clause}(gps_latitude IS NOT NULL
                    AND gps_longitude IS NOT NULL)
                """
            ).fetchone()[0]
        roots = _canonical_index_root_strings(
            [
                row[0]
                for row in connection.execute(
                    "SELECT root FROM photo_library_files GROUP BY root ORDER BY root"
                ).fetchall()
            ]
        )
        recent_runs = [
            {
                "started_at": row[0],
                "finished_at": row[1],
                "roots": row[2],
                "reset": bool(row[3]),
                "scanned_file_count": row[4],
                "indexed_file_count": row[5],
                "skipped_file_count": row[6],
                "file_count_before": row[7],
                "file_count_after": row[8],
                "new_file_count": row[9],
            }
            for row in connection.execute(
                """
                SELECT
                    started_at,
                    finished_at,
                    roots,
                    reset,
                    scanned_file_count,
                    indexed_file_count,
                    skipped_file_count,
                    file_count_before,
                    file_count_after,
                    new_file_count
                FROM photo_library_index_runs
                ORDER BY finished_at DESC
                LIMIT 5
                """
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        status = _busy_photo_library_index_status(index_path)
        status["error"] = str(exc)
        return status
    finally:
        if connection is not None:
            connection.close()
    return {
        "exists": True,
        "path": str(index_path),
        "file_count": file_count,
        "date_count": date_count,
        "capture_timestamp_count": capture_timestamp_count,
        "gps_coordinate_count": gps_coordinate_count,
        "roots": roots,
        "recent_runs": recent_runs,
    }


def _busy_photo_library_index_status(index_path: Path) -> dict[str, Any]:
    return {
        "exists": index_path.exists(),
        "path": str(index_path),
        "busy": True,
        "file_count": 0,
        "date_count": 0,
        "capture_timestamp_count": 0,
        "gps_coordinate_count": 0,
        "roots": [],
        "recent_runs": [],
    }


def _photo_library_index_roots(index_path: Path) -> list[str]:
    if not index_path.exists():
        return []
    connection = sqlite3.connect(index_path)
    try:
        rows = connection.execute(
            """
            SELECT root
            FROM photo_library_files
            WHERE root != ''
            GROUP BY root
            ORDER BY root
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return _canonical_index_root_strings([str(row[0]) for row in rows if str(row[0]).strip()])


def _latest_broad_match_run_id(db_path: Path) -> str:
    if not db_path.exists():
        return ""
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        row = connection.execute(
            """
            SELECT run_id
            FROM broad_match_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        connection.close()


def _latest_rough_no_date_run_id(db_path: Path) -> str:
    if not db_path.exists():
        return ""
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        row = connection.execute(
            """
            SELECT run_id
            FROM broad_match_runs
            WHERE candidate_scope_json LIKE '%rough_prefilter_no_date%'
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        connection.close()


def _canonical_index_root_strings(roots: list[str]) -> list[str]:
    root_pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        root_text = str(root).strip()
        if not root_text:
            continue
        resolved = str(Path(root_text).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        root_pairs.append((root_text, resolved))
    redundant: set[str] = set()
    resolved_roots = [resolved for _, resolved in root_pairs]
    for resolved in resolved_roots:
        for possible_parent in resolved_roots:
            if resolved != possible_parent and _path_is_within(resolved, possible_parent):
                redundant.add(resolved)
                break
    return [root_text for root_text, resolved in root_pairs if resolved not in redundant]


def _path_is_within(path_text: str, parent_text: str) -> bool:
    parent_prefix = parent_text if parent_text.endswith(os.sep) else f"{parent_text}{os.sep}"
    return path_text.startswith(parent_prefix)


def _ensure_photo_library_index_run_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_library_index_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            roots TEXT NOT NULL,
            reset INTEGER NOT NULL,
            scanned_file_count INTEGER NOT NULL,
            indexed_file_count INTEGER NOT NULL,
            skipped_file_count INTEGER NOT NULL,
            file_count_before INTEGER NOT NULL,
            file_count_after INTEGER NOT NULL,
            new_file_count INTEGER NOT NULL
        )
        """
    )
    connection.commit()


def _batch_plan_status(batch_plan_path: Path, attempt_path: Path | None = None) -> dict[str, Any]:
    if not batch_plan_path.exists():
        return {"exists": False}
    with batch_plan_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    attempts = _read_search_attempts(attempt_path)
    next_batch = _next_action_batch(rows)
    hidden_rejected_total = sum(
        int(row.get("hidden_rejected_candidate_count", 0) or 0)
        for row in rows
    )
    hidden_export_equivalent_total = sum(
        int(row.get("hidden_export_equivalent_candidate_count", 0) or 0)
        for row in rows
    )
    status_counts: dict[str, int] = {}
    for row in rows:
        for status in str(row.get("statuses", "")).split(";"):
            if not status:
                continue
            status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "exists": True,
        "rows": len(rows),
        "hidden_rejected_candidate_count": hidden_rejected_total,
        "hidden_export_equivalent_candidate_count": hidden_export_equivalent_total,
        "status_counts": status_counts,
        "batch_limit": STATUS_BATCH_LIMIT,
        "batches": [_batch_summary(row, attempts) for row in rows[:STATUS_BATCH_LIMIT]],
        "next_batch": {
            **_batch_summary(next_batch, attempts),
        },
    }


def _remainder_overview(group_path: Path) -> dict[str, Any]:
    if not group_path.exists():
        return {"exists": False}
    with group_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    status_counts: dict[str, int] = {}
    total_entries = 0
    actionable_candidates = 0
    hidden_rejected = 0
    hidden_export_equivalent = 0
    for row in rows:
        status = row.get("status", "")
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        total_entries += int(row.get("entry_count", 0) or 0)
        actionable_candidates += int(row.get("candidate_count", 0) or 0)
        hidden_rejected += int(row.get("hidden_rejected_candidate_count", 0) or 0)
        hidden_export_equivalent += int(row.get("hidden_export_equivalent_candidate_count", 0) or 0)

    review_ready_dates = sum(
        1
        for row in rows
        if int(row.get("candidate_count", 0) or 0) > 0
    )
    review_ready_entries = sum(
        int(row.get("entry_count", 0) or 0)
        for row in rows
        if int(row.get("candidate_count", 0) or 0) > 0
    )
    folder_needed_dates = status_counts.get("no_external_candidates", 0)
    only_export_equivalent_dates = status_counts.get("only_export_equivalent_candidates", 0)
    all_rejected_dates = status_counts.get("all_candidates_rejected", 0)
    return {
        "exists": True,
        "date_count": len(rows),
        "entry_count": total_entries,
        "review_ready_date_count": review_ready_dates,
        "review_ready_entry_count": review_ready_entries,
        "folder_needed_date_count": folder_needed_dates,
        "only_export_equivalent_date_count": only_export_equivalent_dates,
        "all_rejected_date_count": all_rejected_dates,
        "actionable_candidate_count": actionable_candidates,
        "hidden_rejected_candidate_count": hidden_rejected,
        "hidden_export_equivalent_candidate_count": hidden_export_equivalent,
        "status_counts": status_counts,
    }


def _next_action_batch(rows: list[dict[str, str]]) -> dict[str, str]:
    for row in rows:
        if int(row.get("candidate_count", 0) or 0) > 0:
            return row
    return rows[0] if rows else {}


def _batch_summary(row: dict[str, str], attempts: list[dict[str, str]] | None = None) -> dict[str, str]:
    matching_attempts = [
        attempt
        for attempt in attempts or []
        if _attempt_matches_batch(attempt, row)
    ]
    latest_attempt = matching_attempts[-1] if matching_attempts else {}
    review_date_count = _batch_review_date_count(row)
    folder_needed_date_count = _batch_folder_needed_date_count(row, review_date_count)
    return {
        "batch_id": row.get("batch_id", ""),
        "start_date": row.get("start_date", ""),
        "end_date": row.get("end_date", ""),
        "entry_count": row.get("entry_count", ""),
        "candidate_count": row.get("candidate_count", ""),
        "review_date_count": str(review_date_count),
        "folder_needed_date_count": str(folder_needed_date_count),
        "hidden_rejected_candidate_count": row.get("hidden_rejected_candidate_count", ""),
        "hidden_export_equivalent_candidate_count": row.get("hidden_export_equivalent_candidate_count", ""),
        "statuses": row.get("statuses", ""),
        "entry_dates": row.get("entry_dates", ""),
        "entry_ids": row.get("entry_ids", ""),
        "recommended_action": row.get("recommended_action", ""),
        "search_attempt_count": str(len(matching_attempts)),
        "latest_search_finished_at": latest_attempt.get("finished_at", ""),
        "latest_search_roots": latest_attempt.get("search_roots", ""),
        "latest_search_candidate_count": latest_attempt.get("candidate_count", ""),
        "next_step": _batch_next_step(row, matching_attempts, latest_attempt),
    }


def _batch_review_date_count(row: dict[str, str]) -> int:
    explicit = row.get("review_date_count", "")
    if explicit:
        return int(explicit)
    return 1 if int(row.get("candidate_count", 0) or 0) > 0 else 0


def _batch_folder_needed_date_count(row: dict[str, str], review_date_count: int) -> int:
    explicit = row.get("folder_needed_date_count", "")
    if explicit:
        return int(explicit)
    date_count = int(row.get("date_count", 0) or 0)
    if not date_count:
        date_count = len(_split_semicolon(row.get("entry_dates", "")))
    return max(date_count - review_date_count, 0)


def _batch_next_step(
    row: dict[str, str],
    matching_attempts: list[dict[str, str]],
    latest_attempt: dict[str, str],
) -> str:
    candidate_count = int(row.get("candidate_count", 0) or 0)
    hidden_rejected_count = int(row.get("hidden_rejected_candidate_count", 0) or 0)
    hidden_export_equivalent_count = int(row.get("hidden_export_equivalent_candidate_count", 0) or 0)
    if candidate_count > 0:
        return "Review candidates"
    if hidden_rejected_count > 0:
        return "Try another folder or keep fallback"
    if hidden_export_equivalent_count > 0:
        return "Try another folder or keep fallback"
    if not matching_attempts:
        return "Choose folder"
    return "Try another folder"


def _search_attempt_status(attempt_path: Path) -> dict[str, Any]:
    if not attempt_path.exists():
        return {"exists": False}
    rows = _read_search_attempts(attempt_path)
    latest = rows[-1] if rows else {}
    return {
        "exists": True,
        "rows": len(rows),
        "latest": _search_attempt_summary(latest),
        "recent": [
            _search_attempt_summary(row)
            for row in reversed(rows[-10:])
        ],
    }


def _read_search_attempts(attempt_path: Path | None) -> list[dict[str, str]]:
    if attempt_path is None or not attempt_path.exists():
        return []
    with attempt_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _attempt_matches_batch(attempt: dict[str, str], batch: dict[str, str]) -> bool:
    if not batch:
        return False
    attempt_ids = _split_semicolon(attempt.get("target_entry_ids", ""))
    attempt_dates = _split_semicolon(attempt.get("target_entry_dates", ""))
    batch_ids = _split_semicolon(batch.get("entry_ids", ""))
    batch_dates = _split_semicolon(batch.get("entry_dates", ""))
    if not attempt_ids and not attempt_dates and not attempt.get("start_date") and not attempt.get("end_date") and not attempt.get("max_targets"):
        return False
    if attempt_dates:
        return attempt_dates == batch_dates
    if attempt_ids:
        return attempt_ids == batch_ids
    return (
        attempt.get("start_date", "") == batch.get("start_date", "")
        and attempt.get("end_date", "") == batch.get("end_date", "")
        and attempt.get("max_targets", "") == batch.get("entry_count", "")
    )


def _split_semicolon(value: str) -> set[str]:
    return {item.strip() for item in value.split(";") if item.strip()}


def _search_attempt_summary(row: dict[str, str]) -> dict[str, str]:
    return {
        "attempt_id": row.get("attempt_id", ""),
        "finished_at": row.get("finished_at", ""),
        "search_roots": row.get("search_roots", ""),
        "target_entry_ids": row.get("target_entry_ids", ""),
        "target_entry_dates": row.get("target_entry_dates", ""),
        "start_date": row.get("start_date", ""),
        "end_date": row.get("end_date", ""),
        "max_targets": row.get("max_targets", ""),
        "unclear_entry_count": row.get("unclear_entry_count", ""),
        "candidate_count": row.get("candidate_count", ""),
        "hidden_rejected_candidate_count": row.get("hidden_rejected_candidate_count", ""),
        "hidden_export_equivalent_candidate_count": row.get("hidden_export_equivalent_candidate_count", ""),
    }


def _diarium_package_status(package_dir: Path) -> dict[str, Any]:
    if not package_dir.exists():
        return {"exists": False}
    packages = sorted(package_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime)
    if not packages:
        return {"exists": True, "package_count": 0}

    package_path = packages[-1]
    manifest_path = package_dir / f"{package_path.stem}_manifest.csv"
    with zipfile.ZipFile(package_path) as archive:
        names = archive.namelist()
        name_set = set(names)
        photo_files = [name for name in names if name.startswith("photos/") and not name.endswith("/")]
        journal_entries = 0
        journal_photo_refs = 0
        missing_photo_refs = 0
        missing_photo_markers = 0
        zero_dimension_photo_refs = 0
        photo_hashes = []
        if "Journal.json" in names:
            journal = json.loads(archive.read("Journal.json").decode("utf-8"))
            entries = journal.get("entries", []) if isinstance(journal, dict) else []
            journal_entries = len(entries)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text", "")
                if not isinstance(text, str):
                    text = ""
                for photo in entry.get("photos", []):
                    if not isinstance(photo, dict):
                        continue
                    journal_photo_refs += 1
                    media_type = str(photo.get("type") or "jpeg")
                    extension = "jpeg" if media_type == "jpeg" else media_type
                    photo_hash = str(photo.get("md5") or "")
                    if photo_hash:
                        photo_hashes.append(photo_hash)
                    photo_name = f"photos/{photo_hash}.{extension}"
                    if photo_name not in name_set:
                        missing_photo_refs += 1
                    if not int(photo.get("width") or 0) or not int(photo.get("height") or 0):
                        zero_dimension_photo_refs += 1
                    identifier = str(photo.get("identifier") or "")
                    if identifier and f"dayone-moment://{identifier}" not in text:
                        missing_photo_markers += 1

    manifest_rows = 0
    manifest_photo_rows = 0
    photo_hash_entries = []
    if manifest_path.exists():
        with manifest_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        manifest_rows = len(rows)
        manifest_photo_rows = sum(1 for row in rows if row.get("photo_zip_path", "").strip())
        for row in rows:
            photo_zip_path = row.get("photo_zip_path", "").strip()
            if not photo_zip_path:
                continue
            photo_hash_entries.append(
                {
                    "entry_id": row.get("entry_id", ""),
                    "entry_date": row.get("entry_date", "") or _entry_date_from_id(row.get("entry_id", "")),
                    "photo_hash": Path(photo_zip_path).stem,
                    "photo_zip_path": photo_zip_path,
                }
            )
    photo_ready = (
        journal_photo_refs > 0
        and len(photo_files) >= journal_photo_refs
        and manifest_photo_rows == journal_photo_refs
        and missing_photo_refs == 0
        and missing_photo_markers == 0
        and zero_dimension_photo_refs == 0
    )

    return {
        "exists": True,
        "package_count": len(packages),
        "path": str(package_path),
        "filename": package_path.name,
        "journal_entries": journal_entries,
        "photo_files": len(photo_files),
        "journal_photo_refs": journal_photo_refs,
        "missing_photo_refs": missing_photo_refs,
        "missing_photo_markers": missing_photo_markers,
        "zero_dimension_photo_refs": zero_dimension_photo_refs,
        "photo_hashes": sorted(set(photo_hashes)),
        "photo_hash_entries": photo_hash_entries,
        "photo_ready": photo_ready,
        "manifest_path": str(manifest_path),
        "manifest_rows": manifest_rows,
        "manifest_photo_rows": manifest_photo_rows,
    }


def _diarium_local_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"exists": False, "path": str(db_path)}

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            entry_count = connection.execute("SELECT COUNT(*) FROM Entries").fetchone()[0]
            media_count = connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0]
            empty_media_count = connection.execute(
                "SELECT COUNT(*) FROM Media WHERE Data IS NULL OR length(Data) = 0"
            ).fetchone()[0]
            entries_without_media = connection.execute(
                """
                SELECT COUNT(*)
                FROM Entries AS entries
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM Media AS media
                    WHERE media.DiaryEntryId = entries.DiaryEntryId
                )
                """
            ).fetchone()[0]
            date_row = connection.execute(
                "SELECT MIN(DiaryEntryId), MAX(DiaryEntryId) FROM Entries"
            ).fetchone()
            media_types = [
                {
                    "type": row[0],
                    "file_ending": row[1],
                    "count": row[2],
                }
                for row in connection.execute(
                    """
                    SELECT Type, FileEnding, COUNT(*)
                    FROM Media
                    GROUP BY Type, FileEnding
                    ORDER BY COUNT(*) DESC, FileEnding
                    """
                ).fetchall()
            ]
            media_names = [
                _normalized_diarium_media_name(row[0])
                for row in connection.execute(
                    """
                    SELECT Name
                    FROM Media
                    WHERE Name IS NOT NULL AND TRIM(Name) != ''
                    """
                ).fetchall()
            ]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {
            "exists": True,
            "path": str(db_path),
            "readable": False,
            "error": str(exc),
        }

    return {
        "exists": True,
        "path": str(db_path),
        "readable": True,
        "entry_count": entry_count,
        "media_count": media_count,
        "empty_media_count": empty_media_count,
        "entries_without_media": entries_without_media,
        "date_start": _diarium_ticks_to_date(date_row[0]) if date_row and date_row[0] else "",
        "date_end": _diarium_ticks_to_date(date_row[1]) if date_row and date_row[1] else "",
        "media_types": media_types,
        "media_names": sorted(set(media_names)),
    }


def _normalized_diarium_media_name(name: str) -> str:
    value = str(name or "").strip()
    if "." in value:
        return Path(value).stem
    return value


def _entry_date_from_id(entry_id: str) -> str:
    prefix = "project365:"
    value = str(entry_id or "")
    if value.startswith(prefix):
        return value.removeprefix(prefix)
    return ""


def _diarium_import_verification(
    package_status: dict[str, Any],
    local_status: dict[str, Any],
) -> dict[str, Any]:
    expected_entries = int(package_status.get("journal_entries") or 0)
    expected_photos = int(package_status.get("photo_files") or 0)
    actual_entries = int(local_status.get("entry_count") or 0)
    actual_photos = int(local_status.get("media_count") or 0)
    entries_without_media = int(local_status.get("entries_without_media") or 0)
    empty_media_count = int(local_status.get("empty_media_count") or 0)
    expected_media_names = set(package_status.get("photo_hashes") or [])
    actual_media_names = set(local_status.get("media_names") or [])
    media_name_match_count = len(expected_media_names & actual_media_names)
    missing_media_name_entries = [
        item
        for item in package_status.get("photo_hash_entries", [])
        if item.get("photo_hash") and item.get("photo_hash") not in actual_media_names
    ]

    base = {
        "expected_entries": expected_entries,
        "expected_photos": expected_photos,
        "actual_entries": actual_entries,
        "actual_photos": actual_photos,
        "entries_without_media": entries_without_media,
        "empty_media_count": empty_media_count,
        "expected_media_name_count": len(expected_media_names),
        "actual_media_name_count": len(actual_media_names),
        "media_name_match_count": media_name_match_count,
        "missing_media_name_entries": missing_media_name_entries,
        "photo_imported": False,
        "review_hint": (
            "Diarium stores imported Day One photos as attachments. "
            "Use Attachments > Photos to review them; they may not appear inline in entry text. "
            "Diarium JSON exports keep the photos as separate media files under media/."
        ),
    }
    if not package_status or not package_status.get("exists"):
        return {
            **base,
            "status": "missing_package",
            "message": "No Diarium import package has been generated yet.",
        }
    if not package_status.get("photo_ready"):
        return {
            **base,
            "status": "package_attention",
            "message": "The latest Diarium package is not photo-ready.",
        }
    if not local_status or not local_status.get("exists"):
        return {
            **base,
            "status": "missing_diarium",
            "message": "The local Diarium database was not found.",
        }
    if local_status.get("readable") is False:
        return {
            **base,
            "status": "diarium_unreadable",
            "message": f"Could not read the local Diarium database: {local_status.get('error', '')}",
        }

    counts_match = (
        expected_entries == actual_entries
        and expected_photos == actual_photos
        and entries_without_media == 0
        and empty_media_count == 0
    )
    names_match = not expected_media_names or media_name_match_count == len(expected_media_names)
    if counts_match and names_match:
        return {
            **base,
            "status": "pass",
            "photo_imported": True,
            "message": (
                f"Verified: Diarium has {actual_photos} photo attachments "
                f"for {actual_entries} imported entries."
            ),
        }

    problems = []
    if expected_entries != actual_entries:
        problems.append(f"entries {actual_entries}/{expected_entries}")
    if expected_photos != actual_photos:
        problems.append(f"photos {actual_photos}/{expected_photos}")
    if entries_without_media:
        problems.append(f"{entries_without_media} entries without attachments")
    if empty_media_count:
        problems.append(f"{empty_media_count} empty attachments")
    should_report_media_name_mismatch = (
        expected_media_names
        and actual_photos > 0
        and media_name_match_count != len(expected_media_names)
    )
    if should_report_media_name_mismatch:
        missing_dates = _format_missing_media_dates(missing_media_name_entries)
        problems.append(
            f"media names {media_name_match_count}/{len(expected_media_names)} match latest package{missing_dates}"
        )
    return {
        **base,
        "status": "attention",
        "message": f"Attention: {'; '.join(problems)}.",
    }


def _format_missing_media_dates(entries: list[dict[str, Any]]) -> str:
    dates = [str(item.get("entry_date") or item.get("entry_id") or "").strip() for item in entries]
    dates = [value for value in dates if value]
    if not dates:
        return ""
    visible = ", ".join(dates[:5])
    if len(dates) > 5:
        visible += f", +{len(dates) - 5} more"
    return f" ({visible})"


def _diarium_ticks_to_date(ticks: int) -> str:
    epoch = dt.datetime(1, 1, 1)
    value = epoch + dt.timedelta(microseconds=ticks / 10)
    return value.date().isoformat()


MEDIA_DEDUPE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 Media Dedupe Review</title>
<style>
:root {
  --bg: #f6f6f2;
  --panel: #fff;
  --ink: #202124;
  --muted: #667085;
  --line: #d7d9d2;
  --accent: #17695d;
  --fail: #9f2d2d;
  --warn: #9f580a;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
button, input, select, textarea { font: inherit; }
.shell { max-width: 1560px; margin: 0 auto; padding: 18px; display: grid; gap: 12px; }
.header, .toolbar, .actionbar { display: flex; justify-content: space-between; gap: 10px; align-items: center; flex-wrap: wrap; }
h1 { margin: 0; font-size: 24px; letter-spacing: 0; }
h2 { margin: 0; font-size: 15px; letter-spacing: 0; }
.subtle, .status, .shortcut { color: var(--muted); font-size: 13px; }
.panel { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 12px; display: grid; gap: 10px; }
.button { border: 1px solid var(--line); background: #fff; border-radius: 6px; min-height: 34px; padding: 0 10px; cursor: pointer; }
.button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.button.danger { border-color: var(--fail); color: var(--fail); }
.button.warn { border-color: var(--warn); color: var(--warn); }
.button:disabled { opacity: 0.55; cursor: wait; }
select, textarea { border: 1px solid var(--line); border-radius: 6px; background: #fff; min-height: 34px; padding: 6px 8px; }
textarea { width: 100%; min-height: 52px; resize: vertical; }
.compare { display: grid; grid-template-columns: repeat(2, minmax(320px, 1fr)); gap: 14px; align-items: start; }
.media-card { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); min-width: 0; overflow: hidden; }
.media-card header { min-height: 46px; padding: 8px 10px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; gap: 10px; align-items: center; }
.side-label { font-size: 12px; color: var(--muted); }
.media-wrap { position: relative; background: #111; min-height: 470px; display: flex; align-items: center; justify-content: center; }
video, img.review-media { width: 100%; max-height: 72vh; object-fit: contain; background: #111; }
.zoom-button { position: absolute; top: 10px; right: 10px; width: 38px; height: 38px; border: 1px solid rgba(255,255,255,0.7); border-radius: 999px; background: rgba(255,255,255,0.92); color: #111827; display: inline-flex; align-items: center; justify-content: center; cursor: zoom-in; }
.zoom-button svg { width: 19px; height: 19px; }
.facts-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.facts { display: grid; padding: 8px 2px 2px; align-content: start; }
.fact { display: grid; grid-template-columns: 118px minmax(0, 1fr); gap: 10px; padding: 6px 0; border-bottom: 1px solid #ece7dc; }
.fact:last-child { border-bottom: 0; }
.fact-label { color: var(--muted); font-size: 12px; }
.fact-value { font-size: 13px; overflow-wrap: anywhere; }
.decision-note { border: 1px solid #d8b45b; background: #fff8e1; border-radius: 6px; color: #614700; padding: 8px; font-size: 13px; }
.bad { color: var(--fail); }
.badge-row { display: flex; flex-wrap: wrap; gap: 6px; }
.badge { display: inline-flex; min-height: 22px; align-items: center; padding: 2px 8px; border-radius: 999px; background: #ece7dc; color: #344054; font-size: 12px; }
.sticky-actions { position: sticky; top: 0; z-index: 5; background: var(--bg); padding-top: 4px; }
.preview-modal { position: fixed; inset: 0; z-index: 20; display: none; align-items: center; justify-content: center; padding: 28px; background: rgba(22, 27, 34, 0.78); }
.preview-modal.is-open { display: flex; }
.preview-dialog { position: relative; width: min(96vw, 1700px); max-height: 94vh; display: grid; gap: 8px; }
.preview-dialog img, .preview-dialog video { width: 100%; max-height: 86vh; object-fit: contain; background: #111; border-radius: 8px; box-shadow: 0 20px 60px rgba(0,0,0,0.32); }
.preview-close { position: absolute; top: 10px; right: 10px; width: 38px; height: 38px; border: 1px solid rgba(255,255,255,0.7); border-radius: 999px; background: rgba(255,255,255,0.92); color: #111827; font-size: 24px; line-height: 1; cursor: pointer; }
.preview-caption { max-width: min(96vw, 1700px); color: #fff; font-size: 13px; overflow-wrap: anywhere; text-shadow: 0 1px 2px rgba(0,0,0,0.45); }
a { color: var(--accent); }
@media (max-width: 900px) {
  .compare, .facts-grid { grid-template-columns: 1fr; }
  .media-wrap { min-height: 300px; }
}
</style>
</head>
<body>
<main class="shell">
  <div class="header">
    <div>
      <a href="/?step=media_dedupe_review">Project365 Control Panel</a>
      <h1>Media Dedupe Review</h1>
      <div class="subtle">Review one candidate pair at a time. Decisions are recorded only; no files are deleted, moved, or copied.</div>
    </div>
    <div class="toolbar">
      <button class="button" onclick="loadCandidate(currentOffset)">Refresh</button>
      <button class="button" onclick="rebuildCandidates()">Rebuild candidates</button>
    </div>
  </div>

  <section class="panel sticky-actions">
    <div class="toolbar">
      <div class="toolbar">
        <label class="subtle">Media type
          <select id="mediaType" onchange="resetReview()">
            <option value="photo">Photos</option>
            <option value="video">Videos</option>
          </select>
        </label>
        <label class="subtle">View
          <select id="reviewStatus" onchange="resetReview()">
            <option value="unreviewed">Unreviewed</option>
            <option value="all">All candidates</option>
          </select>
        </label>
      </div>
      <div class="toolbar">
        <button class="button" onclick="previousCandidate()">Previous (Left)</button>
        <button class="button" onclick="nextCandidate()">Next (Right)</button>
      </div>
    </div>
    <div class="actionbar">
      <button class="button primary" onclick="recordDecision('confirm_duplicate_delete_legacy')">Duplicate: keep re-export, discard old (O)</button>
      <button class="button warn" onclick="recordDecision('confirm_duplicate_delete_reexport')">Duplicate: keep old, discard re-export (N)</button>
      <button class="button danger" onclick="recordDecision('reject_duplicate')">Not duplicates (R)</button>
    </div>
    <textarea id="decisionNotes" placeholder="Optional note for this pair"></textarea>
    <div id="status" class="status" role="status" aria-live="polite">Loading...</div>
    <div class="shortcut">Shortcuts: Left/Right navigate, O keep re-export/discard old, N keep old/discard re-export, R reject duplicate, Escape closes large preview.</div>
  </section>

  <section id="candidatePanel" class="panel"></section>
</main>
<div id="dedupePreviewModal" class="preview-modal" onclick="handlePreviewBackdrop(event)" aria-hidden="true">
  <div class="preview-dialog" role="dialog" aria-modal="true" aria-label="Large media preview">
    <button class="preview-close" type="button" onclick="closeDedupePreview()" aria-label="Close large preview">x</button>
    <div id="dedupePreviewMedia"></div>
    <div id="dedupePreviewCaption" class="preview-caption"></div>
  </div>
</div>
<script>
let currentOffset = 0;
let currentPayload = null;
let busy = false;

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Request failed");
  return payload;
}

function mediaType() {
  return document.getElementById("mediaType").value || "photo";
}

function reviewStatus() {
  return document.getElementById("reviewStatus").value || "unreviewed";
}

async function loadCandidate(offset = currentOffset, refresh = false) {
  if (busy) return;
  busy = true;
  setStatus("Loading candidate...");
  try {
    const params = new URLSearchParams({media_type: mediaType(), status: reviewStatus(), limit: "1", offset: String(Math.max(0, offset))});
    if (refresh) params.set("refresh", "1");
    currentPayload = await fetchJson(`/dedupe/api/candidates?${params.toString()}`);
    currentOffset = Number(currentPayload.offset || 0);
    renderCandidate(currentPayload);
    const shown = Number(currentPayload.returned_count || 0);
    const total = Number(currentPayload.total_count || 0);
    const reviewed = Number(currentPayload.reviewed_count || 0);
    setStatus(`${shown ? currentOffset + 1 : 0} of ${total} ${reviewStatus()} ${mediaType()} candidates | ${reviewed} decisions recorded`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    busy = false;
  }
}

function resetReview() {
  currentOffset = 0;
  document.getElementById("decisionNotes").value = "";
  loadCandidate(0);
}

function rebuildCandidates() {
  currentOffset = 0;
  document.getElementById("decisionNotes").value = "";
  loadCandidate(0, true);
}

function renderCandidate(payload) {
  const panel = document.getElementById("candidatePanel");
  const candidate = (payload.candidates || [])[0];
  if (!candidate) {
    panel.innerHTML = `<div class="subtle">No candidates match this view.</div>`;
    return;
  }
  panel.innerHTML = `
    <div class="badge-row">
      <span class="badge">Score ${escapeHtml(candidate.score)}</span>
      ${(candidate.reasons || []).map(reason => `<span class="badge">${escapeHtml(reason)}</span>`).join("")}
      <span class="badge">${escapeHtml(candidate.deletion_safety || "")}</span>
    </div>
    <div class="decision-note">This page records review intent only. Buttons do not delete files.</div>
    <div class="compare">
      ${mediaPane("Re-export", candidate.reexport, candidate.media_type)}
      ${mediaPane("Old library", candidate.legacy, candidate.media_type)}
    </div>
    <div class="facts-grid">
      ${factsPane("Re-export metadata", candidate.reexport)}
      ${factsPane("Old library metadata", candidate.legacy)}
    </div>
  `;
}

function mediaPane(title, row, type) {
  return `
    <article class="media-card">
      <header>
        <h2>${escapeHtml(title)}</h2>
        <div class="side-label">${escapeHtml(row.filename || "")}</div>
      </header>
      <div class="media-wrap">
        ${mediaElement(row, type)}
        ${row.media_url ? zoomButton(title, row, type) : ""}
      </div>
    </article>
  `;
}

function factsPane(title, row) {
  return `
    <article class="panel">
      <h2>${escapeHtml(title)}</h2>
      <div class="facts">
        ${fact("Path", row.path)}
        ${fact("Capture time", row.capture_timestamp)}
        ${fact("Dates", (row.dates || []).join("; "))}
        ${fact("Dimensions", dimensions(row))}
        ${fact("Duration", formatDuration(row.media_duration_seconds))}
        ${fact("Size", formatFileSize(row.byte_size))}
        ${fact("GPS", row.has_gps ? "Yes" : "No")}
        ${fact("SHA-256", row.sha256)}
      </div>
    </article>
  `;
}

function mediaElement(row, type) {
  if (!row.media_url) return `<div class="subtle">Media unavailable.</div>`;
  if (type === "video") return `<video controls preload="metadata" src="${escapeHtml(row.media_url)}"></video>`;
  return `<img class="review-media" src="${escapeHtml(row.media_url)}" alt="">`;
}

function zoomButton(title, row, type) {
  return `
    <button class="zoom-button" type="button" onclick="openDedupePreview('${escapeJs(row.media_url || "")}', '${escapeJs(type)}', '${escapeJs(title)}', '${escapeJs(row.path || "")}')" aria-label="Open larger ${escapeHtml(title)} preview" title="Open larger preview">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="11" cy="11" r="7"></circle>
        <path d="m21 21-4.3-4.3"></path>
        <path d="M11 8v6M8 11h6"></path>
      </svg>
    </button>
  `;
}

function fact(label, value) {
  return `<div class="fact"><div class="fact-label">${escapeHtml(label)}</div><div class="fact-value">${escapeHtml(value || "-")}</div></div>`;
}

async function recordDecision(decision) {
  const candidate = (currentPayload?.candidates || [])[0];
  if (!candidate || busy) return;
  busy = true;
  setStatus("Recording decision...");
  try {
    await fetchJson("/dedupe/api/decision", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        media_type: candidate.media_type,
        candidate_key: candidate.candidate_key,
        reexport_path: candidate.reexport.path,
        legacy_path: candidate.legacy.path,
        decision,
        notes: document.getElementById("decisionNotes").value || ""
      })
    });
    document.getElementById("decisionNotes").value = "";
    busy = false;
    await loadCandidate(reviewStatus() === "unreviewed" ? currentOffset : currentOffset + 1);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    busy = false;
  }
}

function previousCandidate() {
  loadCandidate(Math.max(0, currentOffset - 1));
}

function nextCandidate() {
  if (!currentPayload?.has_more) return;
  loadCandidate(currentOffset + 1);
}

function openDedupePreview(url, type, title, path) {
  const modal = document.getElementById("dedupePreviewModal");
  const media = document.getElementById("dedupePreviewMedia");
  const caption = document.getElementById("dedupePreviewCaption");
  const closeButton = modal?.querySelector(".preview-close");
  if (!modal || !media || !caption || !url) return;
  const largeUrl = type === "photo" ? largeImageUrl(url) : url;
  media.innerHTML = type === "video"
    ? `<video controls autoplay src="${escapeHtml(largeUrl)}"></video>`
    : `<img src="${escapeHtml(largeUrl)}" alt="${escapeHtml(title || "Large preview")}">`;
  caption.textContent = path ? `${title} | ${fileName(path)}` : title;
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
  closeButton?.focus();
}

function closeDedupePreview() {
  const modal = document.getElementById("dedupePreviewModal");
  const media = document.getElementById("dedupePreviewMedia");
  if (!modal || !media) return;
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
  media.innerHTML = "";
}

function handlePreviewBackdrop(event) {
  if (event.target?.id === "dedupePreviewModal") closeDedupePreview();
}

function largeImageUrl(url) {
  const [base, queryText = ""] = String(url || "").split("?");
  const params = new URLSearchParams(queryText);
  params.set("max", "2048");
  return `${base}?${params.toString()}`;
}

function setStatus(message, error = false) {
  const target = document.getElementById("status");
  target.textContent = message;
  target.classList.toggle("bad", error);
}

function dimensions(row) {
  return row.media_width && row.media_height ? `${row.media_width} x ${row.media_height}` : "";
}

function formatDuration(value) {
  const seconds = Number(value || 0);
  if (!seconds) return "";
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${rest}`;
}

function formatFileSize(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "";
  if (value >= 1000000000) return `${(value / 1000000000).toFixed(1)} GB`;
  if (value >= 1000000) return `${(value / 1000000).toFixed(1)} MB`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)} KB`;
  return `${value} B`;
}

function fileName(path) {
  const parts = String(path || "").split("/");
  return parts[parts.length - 1] || path || "";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[char]));
}

function escapeJs(value) {
  return String(value ?? "").replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/\n/g, "\\n").replace(/\r/g, "");
}

function editableShortcutTarget(target) {
  return Boolean(target?.closest?.("input, textarea, select, button, video, [contenteditable='true']"));
}

document.addEventListener("keydown", event => {
  const modal = document.getElementById("dedupePreviewModal");
  if (event.key === "Escape" && modal?.classList.contains("is-open")) {
    closeDedupePreview();
    event.preventDefault();
    return;
  }
  if (event.repeat || event.metaKey || event.ctrlKey || event.altKey || editableShortcutTarget(event.target)) return;
  let handled = true;
  if (event.key === "ArrowLeft") previousCandidate();
  else if (event.key === "ArrowRight") nextCandidate();
  else if (event.key.toLowerCase() === "o") recordDecision("confirm_duplicate_delete_legacy");
  else if (event.key.toLowerCase() === "n") recordDecision("confirm_duplicate_delete_reexport");
  else if (event.key.toLowerCase() === "r") recordDecision("reject_duplicate");
  else handled = false;
  if (handled) event.preventDefault();
});

loadCandidate(0);
</script>
</body>
</html>
"""


def create_handler(state: ControlState, config: ControlConfig) -> type[BaseHTTPRequestHandler]:
    class ControlHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/":
                    query = urllib.parse.parse_qs(parsed.query)
                    initial_step = query.get("step", [""])[0]
                    self._send_html(_control_html(config.picker_url, initial_step=initial_step), no_store=True)
                elif parsed.path in {"/picker", "/picker/"}:
                    self._send_html(_embedded_picker_html())
                elif parsed.path in {"/enrich", "/enrich/"}:
                    self._send_html(diary_enrichment.ENRICHMENT_HTML, no_store=True)
                elif parsed.path in {"/broad-review", "/broad-review/"}:
                    self._send_html(BROAD_REVIEW_HTML, no_store=True)
                elif parsed.path in {"/crop", "/crop/"}:
                    self._send_html(_embedded_crop_html())
                elif parsed.path in {"/dedupe", "/dedupe/"}:
                    self._send_html(MEDIA_DEDUPE_HTML, no_store=True)
                elif parsed.path == "/dedupe/api/candidates":
                    query = urllib.parse.parse_qs(parsed.query)
                    payload = media_dedupe.candidate_page(
                        CANONICAL_ROOT,
                        media_type=query.get("media_type", ["photo"])[0],
                        limit=original_picker._query_int(query, "limit", 1) or 1,
                        offset=original_picker._query_int(query, "offset", 0) or 0,
                        status=query.get("status", ["unreviewed"])[0],
                        refresh=_query_truthy(query.get("refresh", [""])[0]),
                    )
                    self._send_json(self._with_dedupe_media_urls(payload))
                elif parsed.path.startswith("/dedupe/media/"):
                    self._send_dedupe_media(parsed.path.removeprefix("/dedupe/media/"))
                elif parsed.path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                elif parsed.path == "/api/status":
                    query = urllib.parse.parse_qs(parsed.query)
                    steps = query.get("step") or query.get("steps")
                    self._send_json(
                        state.status(
                            steps if steps else None,
                            include_broad_monthly_coverage=_query_truthy(
                                query.get("include_broad_monthly_coverage", [""])[0]
                            ),
                        )
                    )
                elif parsed.path == "/api/working-copy-readiness":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        _working_copy_readiness_status(
                            start_date=query.get("start_date", [""])[0],
                            end_date=query.get("end_date", [""])[0],
                        )
                    )
                elif parsed.path.startswith("/api/crop-estimate-batch/"):
                    job_id = urllib.parse.unquote(parsed.path.removeprefix("/api/crop-estimate-batch/"))
                    job = state.picker_state().crop_estimate_job(job_id)
                    if job is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown crop estimate batch")
                    else:
                        self._send_json(job)
                elif parsed.path.startswith("/api/job/"):
                    job_id = parsed.path.removeprefix("/api/job/")
                    try:
                        self._send_json(state.job_status(job_id))
                    except KeyError as exc:
                        self._send_error(HTTPStatus.NOT_FOUND, str(exc))
                elif parsed.path == "/broad/api/status":
                    self._send_json(_broad_visual_status())
                elif parsed.path == "/broad/api/review-runs":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        {
                            "runs": broad_visual_match.review_runs(
                                BROAD_VISUAL_DB,
                                limit=original_picker._query_int(query, "limit", 25) or 25,
                            )
                        }
                    )
                elif parsed.path == "/broad/api/results":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        broad_visual_match.review_results(
                            BROAD_VISUAL_DB,
                            run_id=query.get("run_id", [""])[0],
                            entry_id=query.get("entry_id", [""])[0],
                            limit=original_picker._query_int(
                                query,
                                "limit",
                                broad_visual_match.DEFAULT_RESULT_PAGE_SIZE,
                            )
                            or broad_visual_match.DEFAULT_RESULT_PAGE_SIZE,
                            offset=original_picker._query_int(query, "offset", 0) or 0,
                        )
                    )
                elif parsed.path == "/broad/api/review-entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    payload = broad_visual_match.review_entries(
                        CANONICAL_ROOT,
                        BROAD_VISUAL_DB,
                        run_id=query.get("run_id", [""])[0],
                        entry_id=query.get("entry_id", [""])[0],
                        limit=BROAD_REVIEW_ENTRY_LIMIT,
                        offset=original_picker._query_int(query, "offset", 0) or 0,
                        after_date=query.get("after_date", [""])[0],
                        before_date=query.get("before_date", [""])[0],
                        picker_queue_path=ORIGINAL_QUEUE,
                    )
                    self._send_json(self._with_broad_image_urls(payload))
                elif parsed.path == "/broad/api/benchmark-entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    payload = broad_visual_match.benchmark_review_entries(
                        CANONICAL_ROOT,
                        report_dir=VERIFY_REPORT_DIR,
                        entry_id=query.get("entry_id", [""])[0],
                        limit=BROAD_REVIEW_ENTRY_LIMIT,
                        offset=original_picker._query_int(query, "offset", 0) or 0,
                    )
                    self._send_json(self._with_broad_image_urls(payload))
                elif parsed.path.startswith("/broad/image/"):
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_broad_image(
                        parsed.path.removeprefix("/broad/image/"),
                        original_picker._query_int(query, "max", None),
                    )
                elif parsed.path == "/api/choose-folder":
                    self._send_json(_choose_folder_dialog())
                elif parsed.path == "/picker/api/choose-folder":
                    self._send_json(original_picker._choose_folder_dialog())
                elif parsed.path == "/picker/api/choose-photo":
                    self._send_json(original_picker._choose_photo_dialog())
                elif parsed.path == "/picker/api/summary":
                    summary = state.picker_state().summary()
                    summary["active_photo_index_folder"] = state.active_photo_index_folder()
                    self._send_json(summary)
                elif parsed.path == "/picker/api/batches":
                    self._send_json(state.picker_state().batches())
                elif parsed.path == "/enrich/api/entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        diary_enrichment.entry_list(
                            CANONICAL_ROOT,
                            image_tokens=state.picker_state(),
                            start_date=query.get("start_date", [""])[0],
                            end_date=query.get("end_date", [""])[0],
                            limit=original_picker._query_int(query, "limit", 500) or 500,
                        )
                    )
                elif parsed.path.startswith("/enrich/api/entry/"):
                    query = urllib.parse.parse_qs(parsed.query)
                    entry_id = urllib.parse.unquote(parsed.path.removeprefix("/enrich/api/entry/"))
                    detail = diary_enrichment.entry_detail(
                        CANONICAL_ROOT,
                        entry_id,
                        image_tokens=state.picker_state(),
                        candidate_days=original_picker._query_int(query, "days", 0) or 0,
                    )
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown entry")
                    else:
                        self._send_json(detail)
                elif parsed.path == "/picker/api/entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    status = query.get("status", ["all"])[0]
                    self._send_json(
                        state.picker_state().entry_page(
                            status=status,
                            entry_ids=set(original_picker._query_values(query, "entry_id")),
                            entry_dates=set(original_picker._query_values(query, "entry_date")),
                            limit=original_picker._query_int(
                                query,
                                "limit",
                                original_picker.DEFAULT_ENTRY_PAGE_SIZE,
                            ),
                            offset=original_picker._query_int(query, "offset", 0) or 0,
                        )
                    )
                elif parsed.path == "/picker/api/candidate-facts":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_json(
                        {
                            "facts": state.picker_state().candidate_facts(
                                original_picker._query_values(query, "token")
                            )
                        }
                    )
                elif parsed.path.startswith("/picker/api/entry/"):
                    entry_id = urllib.parse.unquote(parsed.path.removeprefix("/picker/api/entry/"))
                    detail = state.picker_state().entry_detail(
                        entry_id,
                        candidate_limit=None,
                        rank_if_needed=False,
                    )
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown entry")
                    else:
                        self._send_json(detail)
                elif parsed.path == "/crop/api/entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    status = query.get("status", ["selected"])[0]
                    self._send_json(
                        {
                            "entries": state.picker_state().entries(
                                status=status,
                                entry_ids=set(original_picker._query_values(query, "entry_id")),
                                entry_dates=set(original_picker._query_values(query, "entry_date")),
                            )
                        }
                    )
                elif parsed.path == "/crop/api/crop-entries":
                    query = urllib.parse.parse_qs(parsed.query)
                    crop_filter = query.get("crop_filter", ["missing"])[0]
                    self._send_json(
                        {
                            "entries": state.picker_state().crop_entries(crop_filter=crop_filter),
                            "pending_crop_commits": state.picker_state().pending_crop_commits(),
                            "crop_estimate_batch": state.picker_state().latest_crop_estimate_job() or {},
                        }
                    )
                elif parsed.path.startswith("/crop/api/entry/"):
                    entry_id = urllib.parse.unquote(parsed.path.removeprefix("/crop/api/entry/"))
                    detail = state.picker_state().crop_entry_detail(entry_id)
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown entry")
                    else:
                        self._send_json(detail)
                elif parsed.path.startswith("/crop/api/crop-entry/"):
                    entry_id = urllib.parse.unquote(parsed.path.removeprefix("/crop/api/crop-entry/"))
                    detail = state.picker_state().crop_entry_detail(entry_id)
                    if detail is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown entry")
                    else:
                        self._send_json(detail)
                elif parsed.path.startswith("/picker/api/crawl/"):
                    job_id = urllib.parse.unquote(parsed.path.removeprefix("/picker/api/crawl/"))
                    job = state.picker_state().crawl_job(job_id)
                    if job is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown crawl job")
                    else:
                        self._send_json(job)
                elif parsed.path.startswith("/picker/api/crop-estimate-batch/"):
                    job_id = urllib.parse.unquote(parsed.path.removeprefix("/picker/api/crop-estimate-batch/"))
                    job = state.picker_state().crop_estimate_job(job_id)
                    if job is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown crop estimate batch")
                    else:
                        self._send_json(job)
                elif parsed.path.startswith("/crop/api/crop-estimate-batch/"):
                    job_id = urllib.parse.unquote(parsed.path.removeprefix("/crop/api/crop-estimate-batch/"))
                    job = state.picker_state().crop_estimate_job(job_id)
                    if job is None:
                        self._send_error(HTTPStatus.NOT_FOUND, "Unknown crop estimate batch")
                    else:
                        self._send_json(job)
                elif parsed.path.startswith("/picker/image/"):
                    query = urllib.parse.parse_qs(parsed.query)
                    self._send_picker_image(
                        parsed.path.removeprefix("/picker/image/"),
                        original_picker._query_int(query, "max", None),
                    )
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            except Exception as exc:  # noqa: BLE001 - local app should surface readable errors.
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/api/run":
                    payload = self._read_json()
                    self._send_json(state.start_step(str(payload.get("step", "")), payload))
                    return
                if parsed.path == "/api/photo-index-folder-from-file":
                    payload = self._read_json()
                    self._send_json(
                        _photo_index_folder_from_file(
                            filename=str(payload.get("filename", "")),
                            byte_size=int(payload.get("byte_size", 0) or 0),
                            sha256=str(payload.get("sha256", "")),
                        )
                    )
                    return
                if parsed.path == "/api/validate-folder-path":
                    payload = self._read_json()
                    self._send_json(_validate_folder_path(str(payload.get("path", ""))))
                    return
                if parsed.path.startswith("/api/job/") and parsed.path.endswith("/cancel"):
                    job_id = parsed.path.removeprefix("/api/job/").removesuffix("/cancel")
                    try:
                        self._send_json(state.cancel_job(job_id))
                    except KeyError as exc:
                        self._send_error(HTTPStatus.NOT_FOUND, str(exc))
                    return
                if parsed.path == "/api/crop-estimate-batch":
                    payload = self._read_json()
                    self._send_json(
                        state.picker_state().start_crop_estimate_batch(
                            apply_estimates=bool(payload.get("apply_estimates"))
                        )
                    )
                    return
                if parsed.path == "/broad/api/send-to-picker":
                    payload = self._read_json()
                    result = broad_visual_match.send_results_to_picker(
                        canonical_root=CANONICAL_ROOT,
                        db_path=BROAD_VISUAL_DB,
                        queue_path=ORIGINAL_QUEUE,
                        result_ids=[int(value) for value in payload.get("result_ids", [])],
                    )
                    state._invalidate_picker_state()
                    self._send_json(result)
                    return
                if parsed.path == "/broad/api/confirm-match":
                    payload = self._read_json()
                    result = broad_visual_match.confirm_broad_match(
                        canonical_root=CANONICAL_ROOT,
                        db_path=BROAD_VISUAL_DB,
                        result_id=int(payload.get("result_id") or 0),
                    )
                    state._invalidate_picker_state()
                    self._send_json(result)
                    return
                if parsed.path == "/broad/api/keep-project365":
                    payload = self._read_json()
                    result = broad_visual_match.keep_project365_export_for_broad_entry(
                        canonical_root=CANONICAL_ROOT,
                        db_path=BROAD_VISUAL_DB,
                        run_id=str(payload.get("run_id", "")),
                        entry_id=str(payload.get("entry_id", "")),
                    )
                    state._invalidate_picker_state()
                    self._send_json(result)
                    return
                if parsed.path == "/broad/api/reject-entry":
                    payload = self._read_json()
                    result = broad_visual_match.reject_broad_entry(
                        canonical_root=CANONICAL_ROOT,
                        db_path=BROAD_VISUAL_DB,
                        run_id=str(payload.get("run_id", "")),
                        entry_id=str(payload.get("entry_id", "")),
                    )
                    state._invalidate_picker_state()
                    self._send_json(result)
                    return
                if parsed.path == "/broad/api/undo-entry-decision":
                    payload = self._read_json()
                    result = broad_visual_match.undo_broad_entry_decision(
                        canonical_root=CANONICAL_ROOT,
                        db_path=BROAD_VISUAL_DB,
                        run_id=str(payload.get("run_id", "")),
                        entry_id=str(payload.get("entry_id", "")),
                    )
                    state._invalidate_picker_state()
                    self._send_json(result)
                    return
                if parsed.path == "/broad/api/clear-review-run":
                    payload = self._read_json()
                    self._send_json(
                        broad_visual_match.clear_review_run(
                            BROAD_VISUAL_DB,
                            run_id=str(payload.get("run_id", "")),
                        )
                    )
                    return
                if parsed.path == "/broad/api/clear-all-review-runs":
                    self._send_json(broad_visual_match.clear_all_review_runs(BROAD_VISUAL_DB))
                    return
                if parsed.path == "/picker/api/decision":
                    payload = self._read_json()
                    detail = state.picker_state().save_decision(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        decision=str(payload.get("decision", "")),
                        notes=str(payload.get("notes", "")),
                        include_candidates=False,
                        associated_entry_date=str(payload.get("associated_entry_date", "")),
                        associated_date_source=str(payload.get("associated_date_source", "manual")),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/picker/api/associated-date-choices":
                    payload = self._read_json()
                    self._send_json(
                        state.picker_state().associated_date_choices(
                            entry_id=str(payload.get("entry_id", "")),
                            candidate_path=str(payload.get("candidate_path", "")),
                        )
                    )
                    return
                if parsed.path == "/picker/api/reject-all":
                    payload = self._read_json()
                    self._send_json(
                        state.picker_state().reject_all_candidates(
                            entry_id=str(payload.get("entry_id", "")),
                            notes=str(payload.get("notes", "")),
                            include_candidates=False,
                            photo_index_folder=str(
                                payload.get("photo_index_folder", "") or state.active_photo_index_folder()
                            ),
                        )
                    )
                    return
                if parsed.path == "/crop/api/crop":
                    payload = self._read_json()
                    detail = state.picker_state().save_crop(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        crop=payload.get("crop") if isinstance(payload.get("crop"), dict) else {},
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/crop/api/crop-reset":
                    payload = self._read_json()
                    detail = state.picker_state().reset_crop(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        preserve_estimate=bool(payload.get("preserve_estimate")),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/crop/api/crop-commit":
                    self._send_json(state.picker_state().commit_staged_crops())
                    return
                if parsed.path == "/crop/api/crop-reject-original":
                    payload = self._read_json()
                    result = state.picker_state().reject_crop_original(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        notes=str(payload.get("notes", "")),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/crop/api/crop-suggestion":
                    payload = self._read_json()
                    result = state.picker_state().suggest_crop_for_candidate(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        crop=payload.get("crop") if isinstance(payload.get("crop"), dict) else None,
                    )
                    self._send_json(result)
                    return
                if parsed.path in {"/picker/api/crop-estimate-batch", "/crop/api/crop-estimate-batch"}:
                    payload = self._read_json()
                    self._send_json(
                        state.picker_state().start_crop_estimate_batch(
                            apply_estimates=bool(payload.get("apply_estimates"))
                        )
                    )
                    return
                if parsed.path == "/picker/api/crawl":
                    payload = self._read_json()
                    job = state.picker_state().start_crawl(
                        entry_ids=[str(value) for value in payload.get("entry_ids", [])],
                        search_roots=[str(value) for value in payload.get("search_roots", [])],
                        scan_metadata_dates=bool(payload.get("scan_metadata_dates", True)),
                    )
                    self._send_json(job)
                    return
                if parsed.path == "/picker/api/expand-date-range":
                    payload = self._read_json()
                    result = state.picker_state().expand_date_range(
                        entry_id=str(payload.get("entry_id", "")),
                        days=int(payload.get("days", 0)),
                        photo_index_folder=str(
                            payload.get("photo_index_folder", "") or state.active_photo_index_folder()
                        ),
                        search_whole_index=bool(payload.get("search_whole_index", False)),
                        whole_index_filename_only=bool(payload.get("whole_index_filename_only", False)),
                        filename_dates_only=bool(payload.get("filename_dates_only", False)),
                        include_modified_dates=bool(payload.get("include_modified_dates", False)),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/picker/api/expand-default-date-range":
                    payload = self._read_json()
                    result = state.picker_state().expand_default_date_range(
                        entry_id=str(payload.get("entry_id", "")),
                        photo_index_folder=str(
                            payload.get("photo_index_folder", "") or state.active_photo_index_folder()
                        ),
                        search_whole_index=bool(payload.get("search_whole_index", False)),
                        whole_index_filename_only=bool(payload.get("whole_index_filename_only", False)),
                        filename_dates_only=bool(payload.get("filename_dates_only", False)),
                        include_modified_dates=bool(payload.get("include_modified_dates", False)),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/picker/api/search-index-date-range":
                    payload = self._read_json()
                    result = state.picker_state().search_index_date_range(
                        entry_id=str(payload.get("entry_id", "")),
                        start_date=str(payload.get("start_date", "")),
                        end_date=str(payload.get("end_date", "")),
                        search_whole_index=bool(payload.get("search_whole_index", False)),
                        photo_index_folder=str(
                            payload.get("photo_index_folder", "") or state.active_photo_index_folder()
                        ),
                        whole_index_filename_only=bool(payload.get("whole_index_filename_only", False)),
                        filename_dates_only=bool(payload.get("filename_dates_only", False)),
                        include_modified_dates=bool(payload.get("include_modified_dates", False)),
                    )
                    self._send_json(result)
                    return
                if parsed.path == "/picker/api/link-candidate":
                    payload = self._read_json()
                    detail = state.picker_state().add_linked_candidate(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/picker/api/import-dropped-candidate":
                    payload = self._read_bytes(original_picker.MAX_DROP_BYTES)
                    detail = state.picker_state().add_copied_candidate(
                        entry_id=urllib.parse.unquote(self.headers.get("x-entry-id", "")),
                        filename=urllib.parse.unquote(self.headers.get("x-file-name", "")),
                        content_type=self.headers.get("content-type", ""),
                        payload=payload,
                        include_candidates=False,
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/enrich/api/add-associated":
                    payload = self._read_json()
                    self._send_json(
                        diary_enrichment.add_associated_photo(
                            CANONICAL_ROOT,
                            target_entry_id=str(payload.get("target_entry_id", "")),
                            source_media_asset_id=str(payload.get("source_media_asset_id", "")),
                            image_tokens=state.picker_state(),
                        )
                    )
                    return
                if parsed.path == "/enrich/api/create-subentry":
                    payload = self._read_json()
                    self._send_json(
                        diary_enrichment.create_subentry_from_candidate(
                            CANONICAL_ROOT,
                            parent_entry_id=str(payload.get("parent_entry_id", "")),
                            source_media_asset_id=str(payload.get("source_media_asset_id", "")),
                            image_tokens=state.picker_state(),
                        )
                    )
                    return
                if parsed.path == "/enrich/api/import-dropped-associated":
                    payload = self._read_bytes(original_picker.MAX_DROP_BYTES)
                    detail = diary_enrichment.add_dropped_associated_photo(
                        CANONICAL_ROOT,
                        target_entry_id=urllib.parse.unquote(self.headers.get("x-entry-id", "")),
                        filename=urllib.parse.unquote(self.headers.get("x-file-name", "")),
                        content_type=self.headers.get("content-type", ""),
                        payload=payload,
                        image_tokens=state.picker_state(),
                    )
                    self._send_json(detail)
                    return
                if parsed.path == "/picker/api/apply-decisions":
                    payload = self._read_json()
                    if payload.get("confirm_apply_decisions") != original_picker.APPLY_DECISIONS_CONFIRM_TOKEN:
                        self._send_error(HTTPStatus.BAD_REQUEST, "Apply decisions requires explicit confirmation.")
                        return
                    self._send_json(state.picker_state().apply_decisions())
                    return
                if parsed.path == "/dedupe/api/decision":
                    payload = self._read_json()
                    self._send_json(
                        media_dedupe.record_decision(
                            CANONICAL_ROOT,
                            media_type=str(payload.get("media_type", "")),
                            candidate_key=str(payload.get("candidate_key", "")),
                            reexport_path=str(payload.get("reexport_path", "")),
                            legacy_path=str(payload.get("legacy_path", "")),
                            decision=str(payload.get("decision", "")),
                            notes=str(payload.get("notes", "")),
                        )
                    )
                    return
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Not found")
                    return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:  # noqa: BLE001 - local app should surface readable errors.
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length", "0"))
            payload = self.rfile.read(length)
            return json.loads(payload.decode("utf-8")) if payload else {}

        def _read_bytes(self, maximum: int) -> bytes:
            length = int(self.headers.get("content-length", "0"))
            if length > maximum:
                raise ValueError("Dropped image exceeds the 250 MB limit.")
            return self.rfile.read(length)

        def _send_html(self, body: str, no_store: bool = False) -> None:
            payload = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/html; charset=utf-8")
            if no_store:
                self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, body: dict[str, Any]) -> None:
            payload = json.dumps(body, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_picker_image(self, token: str, max_size: int | None = None) -> None:
            path = state.picker_state().preview_path(urllib.parse.unquote(token), max_size)
            if path is None or not path.exists() or not path.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "Image not found")
                return
            payload = path.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(path))
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", mime_type or "application/octet-stream")
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(payload)

        def _send_broad_image(self, token: str, max_size: int | None = None) -> None:
            path = state.broad_preview_path(urllib.parse.unquote(token), max_size)
            if path is None or not path.exists() or not path.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "Image not found")
                return
            payload = path.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(path))
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", mime_type or "application/octet-stream")
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(payload)

        def _send_dedupe_media(self, token: str) -> None:
            path = state.broad_image_path(urllib.parse.unquote(token))
            if path is None or not path.exists() or not path.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "Media not found")
                return
            self._send_file_response(path)

        def _send_file_response(self, path: Path) -> None:
            file_size = path.stat().st_size
            mime_type, _ = mimetypes.guess_type(str(path))
            range_header = self.headers.get("Range", "")
            start = 0
            end = file_size - 1
            partial = False
            if range_header.startswith("bytes="):
                start_text, _, end_text = range_header.removeprefix("bytes=").partition("-")
                try:
                    start = int(start_text) if start_text else 0
                    end = int(end_text) if end_text else file_size - 1
                    start = max(0, min(start, file_size - 1))
                    end = max(start, min(end, file_size - 1))
                    partial = True
                except ValueError:
                    start = 0
                    end = file_size - 1
                    partial = False
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
            self.send_header("content-type", mime_type or "application/octet-stream")
            self.send_header("accept-ranges", "bytes")
            self.send_header("content-length", str(length))
            if partial:
                self.send_header("content-range", f"bytes {start}-{end}/{file_size}")
            self.send_header("cache-control", "private, max-age=86400")
            self.end_headers()
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    self.wfile.write(chunk)

        def _with_dedupe_media_urls(self, payload: dict[str, object]) -> dict[str, object]:
            enriched = dict(payload)
            media_type = str(payload.get("media_type") or "")
            candidates = []
            for candidate in payload.get("candidates", []):
                candidate_copy = dict(candidate)
                for side in ("reexport", "legacy"):
                    row = dict(candidate_copy.get(side) or {})
                    path_text = str(row.get("path") or "").strip()
                    path = Path(path_text)
                    if path_text and path.exists() and path.is_file():
                        if media_type == "photo":
                            row["media_url"] = self._broad_image_url(path_text, max_size=1280)
                        else:
                            token = state.broad_image_token(path_text)
                            row["media_url"] = f"/dedupe/media/{urllib.parse.quote(token)}"
                    else:
                        row["media_url"] = ""
                    candidate_copy[side] = row
                candidates.append(candidate_copy)
            enriched["candidates"] = candidates
            return enriched

        def _with_broad_image_urls(self, payload: dict[str, Any]) -> dict[str, Any]:
            enriched = dict(payload)
            entries = []
            for entry in payload.get("entries", []):
                entry_copy = dict(entry)
                entry_copy["source_url"] = self._broad_image_url(entry_copy.get("source_path", ""), max_size=1280)
                entry_copy["source_facts"] = _broad_photo_facts(entry_copy.get("source_path", ""))
                entry_copy["confirmed_url"] = self._broad_image_url(entry_copy.get("confirmed_path", ""), max_size=1280)
                entry_copy["confirmed_facts"] = _broad_photo_facts(entry_copy.get("confirmed_path", ""))
                results = []
                candidate_paths = [
                    str(result.get("candidate_path", "")).strip()
                    for result in entry_copy.get("results", [])
                    if str(result.get("candidate_path", "")).strip()
                ]
                geolocation_by_path = _photo_index_geolocation_by_path(PHOTO_LIBRARY_INDEX, candidate_paths)
                for result in entry_copy.get("results", []):
                    result_copy = dict(result)
                    candidate_url = self._broad_image_url(result_copy.get("candidate_path", ""), max_size=640)
                    if not candidate_url:
                        continue
                    result_copy["candidate_url"] = candidate_url
                    result_copy.update(_broad_result_photo_facts(result_copy, geolocation_by_path))
                    results.append(result_copy)
                entry_copy["results"] = results
                if results:
                    entries.append(entry_copy)
            try:
                entry_limit = int(enriched.get("limit") or len(entries))
            except (TypeError, ValueError):
                entry_limit = len(entries)
            if entry_limit >= 0:
                entries = entries[:entry_limit]
            enriched["entries"] = entries
            enriched["returned_count"] = len(entries)
            return enriched

        def _broad_image_url(self, path_text: str, max_size: int | None = None) -> str:
            text = str(path_text or "").strip()
            path = Path(text)
            if not text or not path.exists() or not path.is_file():
                return ""
            token = state.broad_image_token(text)
            if not token:
                return ""
            url = f"/broad/image/{urllib.parse.quote(token)}"
            if max_size is not None:
                url = f"{url}?max={int(max_size)}"
            return url

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            payload = json.dumps({"error": message}).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ControlHandler


WORKFLOW_STEPS = (
    "import_zips",
    "build_photo_index",
    "match_easy_originals",
    "media_dedupe_review",
    "broad_visual_match",
    "rough_visual_match",
    "crop_confirmation",
    "generate_derivatives",
    "face_tagging",
    "diary_enrichment",
    "generate_diarium_package",
)

STEP_OWNER = {
    "refresh_photo_index_metadata": "build_photo_index",
    "search_originals": "match_easy_originals",
    "apply_original_decisions": "match_easy_originals",
    "broad_visual_index": "broad_visual_match",
    "broad_visual_benchmark": "broad_visual_match",
    "rough_prefilter_build": "rough_visual_match",
    "rough_visual_benchmark": "rough_visual_match",
    "import_digikam_people": "face_tagging",
}


def _crop_estimate_active_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        **job,
        "step": "crop_confirmation",
        "kind": "crop_estimate_batch",
        "cancellable": False,
    }


def _owner_step(step: str) -> str:
    return STEP_OWNER.get(step, step or "")


def _control_html(picker_url: str, initial_step: str = "") -> str:
    metrics = _initial_top_metrics()
    html = (
        CONTROL_HTML
        .replace("__PICKER_URL__", picker_url)
        .replace("__INITIAL_UNIQUE_DIARY_DAYS__", str(metrics["unique_diary_days"]))
        .replace("__INITIAL_DIARY_ENTRIES__", str(metrics["diary_entries"]))
        .replace("__INITIAL_PHOTO_INDEX_FILES__", str(metrics["photo_index_files"]))
        .replace("__INITIAL_ASSOCIATED_PHOTOS__", str(metrics["associated_photos"]))
        .replace(
            "__INITIAL_MISSING_PHOTOS__",
            f'{metrics["missing_photos"]} · {metrics["missing_photos_percent"]}%',
        )
    )
    requested_step = _owner_step(initial_step.strip())
    expanded_step = requested_step if requested_step in WORKFLOW_STEPS else ""
    for step in WORKFLOW_STEPS:
        section_class = "panel workflow-step is-expanded" if step == expanded_step else "panel workflow-step"
        html = re.sub(
            rf'<section class="panel workflow-step(?: is-expanded)?" data-step="{re.escape(step)}">',
            f'<section class="{section_class}" data-step="{step}">',
            html,
        )
        expanded = "true" if step == expanded_step else "false"
        label = "Collapse" if step == expanded_step else "Open"
        html = re.sub(
            rf'(data-step-toggle="{re.escape(step)}" onclick="toggleWorkflowStep\(\'{re.escape(step)}\'\)" aria-expanded=")(?:true|false)(">)(?:Collapse|Open)(</button>)',
            rf'\g<1>{expanded}\g<2>{label}\g<3>',
            html,
        )
    return html


def _initial_top_metrics(include_photo_index: bool = True) -> dict[str, Any]:
    canonical_db = CANONICAL_ROOT / "canonical.db"
    photo_gaps = _initial_project365_photo_gap_counts(canonical_db)
    project365_entries = photo_gaps["project365_entries"]
    metrics = {
        "unique_diary_days": _initial_unique_day_count(canonical_db),
        "diary_entries": _initial_entry_count(canonical_db),
        "project365_entries": project365_entries,
        "missing_photos": photo_gaps["without_identified_original"],
        "missing_photos_percent": _percentage(
            photo_gaps["without_identified_original"],
            project365_entries,
        ),
        "without_identified_original": photo_gaps["without_identified_original"],
        "associated_photos": photo_gaps["associated_photos"],
        "without_identified_original_percent": _percentage(
            photo_gaps["without_identified_original"],
            project365_entries,
        ),
    }
    if include_photo_index:
        metrics["photo_index_files"] = _initial_photo_index_file_count(PHOTO_LIBRARY_INDEX)
    return metrics


def _initial_entry_count(db_path: Path) -> int:
    return _initial_sqlite_count(db_path, "SELECT COUNT(*) FROM entries")


def _initial_unique_day_count(db_path: Path) -> int:
    return _initial_sqlite_count(db_path, "SELECT COUNT(DISTINCT entry_date) FROM entries")


def _initial_photo_index_file_count(index_path: Path) -> int:
    return _initial_sqlite_count(index_path, "SELECT COUNT(*) FROM photo_library_files")


def _initial_project365_photo_gap_counts(db_path: Path) -> dict[str, int]:
    empty = {
        "project365_entries": 0,
        "missing_photos": 0,
        "associated_photos": 0,
        "without_identified_original": 0,
    }
    if not db_path.exists():
        return empty
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            full_counts = _project365_photo_gap_counts(connection)
            project365_entries = int(
                connection.execute(
                    "SELECT COUNT(*) FROM entries WHERE source_app = 'project365'"
                ).fetchone()[0]
                or 0
            )
            return {
                "project365_entries": project365_entries,
                "associated_photos": full_counts["associated_photos"],
                "without_identified_original": full_counts["without_identified_original"],
            }
        finally:
            connection.close()
    except sqlite3.Error:
        return empty


def _initial_sqlite_count(path: Path, query: str) -> int:
    if not path.exists():
        return 0
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            return int(connection.execute(query).fetchone()[0] or 0)
        finally:
            connection.close()
    except sqlite3.Error:
        return 0


def serve_control_app(config: ControlConfig) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((config.host, config.port), create_handler(ControlState(), config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Project365 control panel.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    picker_url = f"http://{args.host}:{args.port}/picker"
    server = serve_control_app(ControlConfig(host=args.host, port=args.port, picker_url=picker_url))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _embedded_picker_html() -> str:
    return (
        original_picker.PICKER_HTML
        .replace('fetchJson("/api/', 'fetchJson("/picker/api/')
        .replace('fetchJson(`/api/', 'fetchJson(`/picker/api/')
        .replace('src="/image/${', 'src="/picker/image/${')
        .replace('`/image/${', '`/picker/image/${')
    )


def _embedded_crop_html() -> str:
    return (
        original_picker.CROP_HTML
        .replace('fetchJson("/api/', 'fetchJson("/crop/api/')
        .replace('fetchJson(`/api/', 'fetchJson(`/crop/api/')
        .replace('src="/image/${', 'src="/picker/image/${')
        .replace('`/image/${', '`/picker/image/${')
    )


def _choose_folder_dialog() -> dict[str, str]:
    script = (
        'POSIX path of (choose folder with prompt '
        '"Choose the photo folder to scan for the Project365 photo index")'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        if "User canceled" in error or "-128" in error:
            return {"path": ""}
        raise ValueError(error or "Folder picker failed")
    path = result.stdout.strip()
    if path and not Path(path).exists():
        raise ValueError(f"Chosen folder does not exist: {path}")
    return {"path": path}


def _validate_folder_path(path_text: str) -> dict[str, str]:
    path_text = _strip_wrapping_quotes(path_text)
    if not path_text:
        raise ValueError("Enter a folder path.")
    path = Path(path_text).expanduser()
    if not path.exists():
        raise ValueError(f"Folder does not exist: {path_text}")
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        raise ValueError(f"Path is not a folder: {path_text}")
    return {"path": str(path.resolve())}


def _folder_root_from_text(path_text: str) -> str:
    path_text = _strip_wrapping_quotes(path_text)
    if not path_text:
        return ""
    path = Path(path_text).expanduser()
    if path.exists() and path.is_file():
        return str(path.parent)
    return path_text


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value


def _photo_index_folder_from_file(
    filename: str,
    byte_size: int,
    sha256: str,
    index_path: Path = PHOTO_LIBRARY_INDEX,
) -> dict[str, str]:
    sha256 = sha256.strip().lower()
    if not filename:
        raise ValueError("Choose a file first.")
    if byte_size <= 0:
        raise ValueError("Chosen file has no readable size.")
    if len(sha256) != 64:
        raise ValueError("Chosen file hash is missing or invalid.")
    if not index_path.exists():
        raise ValueError("Photo index does not exist. Build the photo index first.")
    connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        columns = _sqlite_table_columns(connection, "photo_library_files")
        sha256_select = "sha256" if "sha256" in columns else "'' AS sha256"
        rows = connection.execute(
            f"""
            SELECT path, root, byte_size, {sha256_select}
            FROM photo_library_files
            WHERE filename = ?
            ORDER BY path
            """,
            (filename,),
        ).fetchall()
        latest_run = _latest_photo_index_run_summary(connection)
    finally:
        connection.close()
    if not rows:
        detail = f" No indexed file is named {filename!r}."
        if latest_run:
            detail += f" {latest_run}"
        raise ValueError(
            "Chosen file was not found in the photo index."
            f"{detail} Pick a file from an indexed folder, or rebuild the photo index with that folder included."
        )
    size_matches = [row for row in rows if int(row[2] or 0) == byte_size]
    if not size_matches:
        indexed_sizes = ", ".join(str(size) for size in sorted({int(row[2] or 0) for row in rows})[:5])
        raise ValueError(
            "Chosen file was not found in the photo index."
            f" The index has {filename!r}, but with byte size {indexed_sizes};"
            f" the selected file is {byte_size} bytes."
        )
    matches = []
    for path_text, root_text, _, indexed_sha256 in size_matches:
        path = Path(path_text)
        indexed_sha256 = str(indexed_sha256 or "").strip().lower()
        try:
            hash_matches = indexed_sha256 == sha256
            if not indexed_sha256 and path.exists() and path.is_file():
                hash_matches = _sha256_file(path) == sha256
            if hash_matches:
                root = Path(root_text)
                matches.append((path, root if root.exists() and root.is_dir() else path.parent))
        except OSError:
            continue
    if not matches:
        raise ValueError(
            "Chosen file was not found in the photo index."
            f" The index has {filename!r} with the selected byte size, but the file hash does not match."
        )
    indexed_roots = sorted({str(root) for _, root in matches})
    if len(indexed_roots) > 1:
        raise ValueError(
            "Chosen file appears in multiple indexed roots. Pick a file unique to the target folder."
        )
    return {"path": indexed_roots[0], "matched_file": str(matches[0][0])}


def _sqlite_table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _latest_photo_index_run_summary(connection: sqlite3.Connection) -> str:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'photo_library_index_runs'"
        )
    }
    if "photo_library_index_runs" not in tables:
        return ""
    row = connection.execute(
        """
        SELECT finished_at, roots, new_file_count
        FROM photo_library_index_runs
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return ""
    finished_at, roots, new_file_count = row
    roots_text = str(roots or "").strip() or "unknown roots"
    if len(roots_text) > 180:
        roots_text = roots_text[:177] + "..."
    date_text = str(finished_at or "").split("T", 1)[0]
    return f"Latest index run {date_text} scanned {roots_text} and added {int(new_file_count or 0)} new files."


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


BROAD_REVIEW_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Broad Visual Review</title>
<style>
:root {
  --bg: #f6f6f2;
  --panel: #fff;
  --ink: #202124;
  --muted: #667085;
  --line: #d7d9d2;
  --accent: #17695d;
  --fail: #9f2d2d;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.shell { max-width: 1400px; margin: 0 auto; padding: 18px; display: grid; gap: 12px; }
.header { display: grid; gap: 8px; justify-items: start; }
.button-row, .entry-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
.entry-head { position: sticky; top: 0; z-index: 5; background: var(--panel); padding: 8px 0; border-bottom: 1px solid var(--line); }
h1 { margin: 0; font-size: 24px; }
h2 { margin: 0; font-size: 18px; }
h3 { margin: 0; font-size: 14px; }
.subtle, .status, .meta { color: var(--muted); font-size: 13px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; display: grid; gap: 12px; }
.field-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.field { display: grid; gap: 5px; }
label { font-size: 12px; color: var(--muted); }
input, select { width: 100%; min-height: 34px; border: 1px solid var(--line); border-radius: 6px; padding: 0 8px; background: #fff; }
input[type="checkbox"] { width: 16px; min-height: 16px; }
.button { border: 1px solid var(--line); background: #fff; border-radius: 6px; min-height: 34px; padding: 0 10px; cursor: pointer; }
.button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.button.danger { color: var(--fail); border-color: #d8a2a2; }
.review-layout { display: grid; grid-template-columns: minmax(380px, 520px) minmax(360px, 1fr); gap: 18px; align-items: start; }
.source-pane { position: sticky; top: 64px; align-self: start; display: grid; gap: 8px; }
.source-frame { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fff; display: grid; gap: 8px; min-width: 0; }
.source-image { width: 100%; max-height: 560px; object-fit: contain; background: #f1f1ec; border-radius: 6px; }
.source-meta { display: grid; gap: 4px; }
.candidate-pane { display: grid; gap: 8px; min-width: 0; }
.candidate-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; align-items: start; }
.candidate-card { border: 1px solid var(--line); border-radius: 8px; padding: 8px; overflow: hidden; display: grid; grid-template-rows: 210px auto auto; gap: 7px; min-width: 0; background: #fff; }
.candidate-image { width: 100%; height: 210px; object-fit: contain; background: #f1f1ec; border-radius: 6px; }
.image-preview-trigger { border: 0; padding: 0; background: transparent; cursor: zoom-in; width: 100%; height: 100%; }
.image-preview-trigger:focus-visible { outline: 3px solid rgba(23, 105, 93, 0.45); outline-offset: 2px; border-radius: 6px; }
.source-frame .image-preview-trigger { height: auto; }
.image-preview-modal { position: fixed; inset: 0; z-index: 20; display: none; align-items: center; justify-content: center; padding: 28px; background: rgba(22, 27, 34, 0.78); }
.image-preview-modal.is-open { display: flex; }
.image-preview-dialog { position: relative; max-width: min(96vw, 1600px); max-height: 94vh; display: grid; gap: 8px; }
.image-preview-dialog img { max-width: min(96vw, 1600px); max-height: 86vh; object-fit: contain; background: #f1f1ec; border-radius: 8px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.32); }
.image-preview-close { position: absolute; top: 10px; right: 10px; width: 38px; height: 38px; border: 1px solid rgba(255, 255, 255, 0.7); border-radius: 999px; background: rgba(255, 255, 255, 0.92); color: #111827; font-size: 24px; line-height: 1; cursor: pointer; }
.image-preview-caption { max-width: min(96vw, 1600px); color: #fff; font-size: 13px; overflow-wrap: anywhere; text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45); }
.candidate-meta { display: grid; gap: 4px; min-width: 0; }
.candidate-filename-row { display: flex; align-items: center; gap: 6px; min-width: 0; }
.candidate-title { font-weight: 600; }
.file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; color: var(--muted); }
.facts { color: var(--muted); font-size: 12px; display: flex; flex-wrap: wrap; gap: 6px; }
.location-indicator { width: 17px; height: 17px; margin-left: auto; flex: 0 0 17px; color: #98a2b3; }
.location-indicator.has-location { color: #b42318; }
.match-badge { color: var(--accent); font-size: 12px; font-weight: 700; }
.candidate-actions { display: flex; justify-content: flex-end; align-items: end; }
.action-button { border: 1px solid var(--line); background: #fff; border-radius: 6px; min-height: 34px; padding: 0 10px; cursor: pointer; }
.action-button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.candidate-card.known-match { border-color: var(--accent); box-shadow: 0 0 0 1px rgba(23, 105, 93, 0.18); }
.review-loading { opacity: 0.55; transition: opacity 120ms ease; }
button:disabled { cursor: wait; opacity: 0.62; }
.path { overflow-wrap: anywhere; font-size: 12px; color: var(--muted); }
.bad { color: var(--fail); }
a { color: var(--accent); }
@media (max-width: 1200px) {
  .candidate-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 850px) {
  .review-layout { grid-template-columns: 1fr; }
  .source-pane { position: static; }
  .entry-head { position: static; }
}
@media (max-width: 560px) {
  .candidate-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<main class="shell">
  <div class="header">
    <a href="/?step=broad_visual_match" title="Return to the Broad Visual Match controls">Project365 Control Panel</a>
    <div>
      <h1>Broad Visual Review</h1>
      <div class="subtle">Visual review for stored broad-search results. Accuracy mode shows the known original when one is already confirmed; unresolved mode keeps review focused on one selected candidate at a time.</div>
    </div>
  </div>
  <section class="panel">
	    <div class="button-row">
	      <button class="button" onclick="previousPage()">Previous entry (Left)</button>
	      <button class="button" onclick="nextPage()">Next entry (Right)</button>
	      <button id="undoBroadDecisionButton" class="button danger" onclick="undoLastBroadDecision()" disabled>Undo last action</button>
	    </div>
	    <div id="status" class="status" role="status" aria-live="polite">Loading...</div>
	  </section>
  <section id="entries"></section>
</main>
<div id="imagePreviewModal" class="image-preview-modal" onclick="handleImagePreviewBackdrop(event)" aria-hidden="true">
  <div class="image-preview-dialog" role="dialog" aria-modal="true" aria-label="Large image preview">
    <button class="image-preview-close" type="button" onclick="closeBroadImagePreview()" aria-label="Close image preview">&times;</button>
    <img id="imagePreviewImage" alt="">
    <div id="imagePreviewCaption" class="image-preview-caption"></div>
  </div>
</div>
<script>
let currentOffset = 0;
let currentHasMore = false;
let currentReviewMode = "search";
let currentReviewSet = "broad";
let currentRunId = "";
let currentEntryId = "";
let currentEntryDate = "";
const currentLimit = 1;
let lastBroadDecision = null;
let currentReviewBusy = false;
function applyInitialQuery() {
  const query = new URLSearchParams(window.location.search);
  const mode = query.get("mode");
  if (mode === "search" || mode === "benchmark") {
    currentReviewMode = mode;
  }
  const reviewSet = query.get("review_set") || "";
  currentReviewSet = ["rough", "no_date", "no-date"].includes(reviewSet) ? "rough" : "broad";
  currentRunId = query.get("run_id") || "";
  currentEntryId = query.get("entry_id") || "";
}
async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Request failed");
  return payload;
}
async function loadEntries(offset = currentOffset) {
  const params = new URLSearchParams();
  const mode = currentReviewMode;
  if (mode === "search" && currentRunId) params.set("run_id", currentRunId);
  if (mode === "search" && !currentRunId && currentReviewSet !== "broad") params.set("review_set", currentReviewSet);
  if (currentEntryId) params.set("entry_id", currentEntryId);
  params.set("limit", String(currentLimit));
  params.set("offset", String(offset));
  setReviewBusy(true);
  setStatus(mode === "benchmark" ? "Loading latest accuracy benchmark..." : "Loading stored broad-search candidates...");
  try {
    const endpoint = mode === "benchmark" ? "/broad/api/benchmark-entries" : "/broad/api/review-entries";
    const payload = await fetchJson(`${endpoint}?${params.toString()}`);
    currentOffset = payload.offset || 0;
    currentHasMore = Boolean(payload.has_more);
    currentRunId = payload.run_id || currentRunId;
    currentEntryId = (payload.entries || [])[0]?.entry_id || "";
    currentEntryDate = (payload.entries || [])[0]?.entry_date || "";
    renderEntries(payload, mode);
    const source = mode === "benchmark" ? "current accuracy benchmark" : reviewSetLabel();
    setStatus(formatBroadReviewPageStatus(payload, source, `offset ${currentOffset}`));
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setReviewBusy(false);
  }
}
async function loadEntriesByDate(direction, entryDate) {
  if (!entryDate) return loadEntries(currentOffset);
  const params = new URLSearchParams();
  const mode = currentReviewMode;
  if (mode === "search" && currentRunId) params.set("run_id", currentRunId);
  if (mode === "search" && !currentRunId && currentReviewSet !== "broad") params.set("review_set", currentReviewSet);
  params.set("limit", String(currentLimit));
  params.set("offset", "0");
  params.set(direction === "before" ? "before_date" : "after_date", entryDate);
  setReviewBusy(true);
  setStatus(mode === "benchmark" ? "Loading latest accuracy benchmark..." : "Loading stored broad-search candidates...");
  try {
    const endpoint = mode === "benchmark" ? "/broad/api/benchmark-entries" : "/broad/api/review-entries";
    const payload = await fetchJson(`${endpoint}?${params.toString()}`);
    currentOffset = payload.offset || 0;
    currentHasMore = Boolean(payload.has_more);
    currentRunId = payload.run_id || currentRunId;
    currentEntryId = (payload.entries || [])[0]?.entry_id || "";
    currentEntryDate = (payload.entries || [])[0]?.entry_date || "";
    renderEntries(payload, mode);
    const source = mode === "benchmark" ? "current accuracy benchmark" : reviewSetLabel();
    setStatus(formatBroadReviewPageStatus(payload, source, `${direction} ${entryDate}`));
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setReviewBusy(false);
  }
}
async function loadEntriesAfterRemoval(removedEntryDate) {
  const referenceDate = removedEntryDate || currentEntryDate;
  if (!referenceDate) return loadEntries(currentOffset);
  await loadEntriesByDate("after", referenceDate);
  if (!currentEntryDate) await loadEntriesByDate("before", referenceDate);
  resetBroadReviewScrollToCandidateList();
}
async function loadEntryById(entryId, runId = currentRunId) {
  if (!entryId) return loadEntries(currentOffset);
  const params = new URLSearchParams();
  if (runId) params.set("run_id", runId);
  if (!runId && currentReviewSet !== "broad") params.set("review_set", currentReviewSet);
  params.set("entry_id", entryId);
  params.set("limit", "1");
  params.set("offset", "0");
  setReviewBusy(true);
  setStatus("Reloading restored entry...");
  try {
    const payload = await fetchJson(`/broad/api/review-entries?${params.toString()}`);
    currentReviewMode = "search";
    currentRunId = payload.run_id || runId || "";
    currentEntryId = entryId;
    currentOffset = payload.offset || 0;
    currentHasMore = Boolean(payload.has_more);
    currentEntryDate = (payload.entries || [])[0]?.entry_date || "";
    renderEntries(payload, "search");
    setStatus(formatBroadReviewPageStatus(payload, reviewSetLabel(), `restored ${entryId}`));
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setReviewBusy(false);
  }
}
function reviewSetLabel() {
  return currentReviewSet === "rough" ? "current no-date review set" : "current Broad review set";
}
function formatBroadReviewPageStatus(payload, source, context) {
  const shown = Number(payload.returned_count || 0);
  const total = Number(payload.total_count || 0);
  const shownText = `${shown} entr${shown === 1 ? "y" : "ies"} shown`;
  const totalText = total ? ` · ${total} ready entr${total === 1 ? "y" : "ies"} available` : "";
  return `${shownText}${totalText} · ${source} · ${context}`;
}
function renderEntries(payload, mode) {
  const target = document.getElementById("entries");
  const entries = payload.entries || [];
  if (!entries.length) {
    const message = mode === "benchmark"
      ? "No visual accuracy benchmark is available. Run Measure accuracy first."
      : "No stored broad-search results. Run Search unresolved photos first.";
    target.innerHTML = `<section class="panel"><div class="subtle">${message}</div></section>`;
    return;
  }
  target.innerHTML = entries.map(entry => `
    <section class="panel">
      <div class="entry-head">
        <div>
          <h2>${escapeHtml(entry.entry_date || "")}</h2>
          <div class="subtle">${escapeHtml(entry.entry_id || "")}</div>
        </div>
        <div class="button-row">
          <div class="meta">${entryStatus(entry, mode)} · ${(entry.results || []).length} stored candidates</div>
          ${mode === "search" ? `<button class="button" onclick="keepProject365Photo('${escapeJs(payload.run_id || "")}', '${escapeJs(entry.entry_id || "")}')">Use Project365 photo</button>` : ""}
          ${mode === "search" ? `<button class="button danger" onclick="rejectBroadEntry('${escapeJs(payload.run_id || "")}', '${escapeJs(entry.entry_id || "")}')">Reject all (R)</button>` : ""}
        </div>
      </div>
      <div class="review-layout">
        <div class="source-pane">
          ${sourceBox("Project365 target", entry.source_url, entry.source_path, entry.source_facts)}
          ${entry.confirmed_url
            ? sourceBox("Known confirmed original", entry.confirmed_url, entry.confirmed_path, entry.confirmed_facts)
            : `<div class="source-frame"><h3>Known confirmed original</h3><div class="subtle">None recorded for this entry.</div></div>`}
        </div>
        <div class="candidate-pane">
          <h3>Calculated candidates</h3>
          <div class="candidate-grid">${renderCandidates(entry, entry.results || [], mode)}</div>
        </div>
      </div>
    </section>
  `).join("");
}
function sourceBox(title, url, path, facts = {}) {
  return `
    <div class="source-frame">
      <h3>${escapeHtml(title)}</h3>
      ${url ? `
        <button class="image-preview-trigger" type="button" onclick="openBroadImagePreviewFromTrigger(this)" data-preview-url="${escapeHtml(url)}" data-preview-title="${escapeHtml(title)}" data-preview-path="${escapeHtml(path || "")}" aria-label="Open larger ${escapeHtml(title)} image">
          <img class="source-image" src="${escapeHtml(url)}" loading="lazy" alt="">
        </button>
      ` : `<div class="subtle">Image path unavailable.</div>`}
      <div class="source-meta">
        <div class="candidate-filename-row">
          <div class="file-name" title="${escapeHtml(path || "")}">${escapeHtml(fileName(path))}</div>
          ${locationIndicator(Boolean(facts.has_geolocation))}
        </div>
        <div class="facts">${escapeHtml(formatBroadPhotoFacts(facts))}</div>
      </div>
    </div>
  `;
}
function entryStatus(entry, mode) {
  if (mode !== "benchmark") return "Unresolved search";
  const rank = Number(entry.expected_rank || 0);
  if (!rank) return `Accuracy: miss`;
  return `Accuracy: hit at rank ${rank}`;
}
function renderCandidates(entry, rows, mode) {
  if (!rows.length) return `<div class="subtle">No candidates stored for this entry.</div>`;
  return rows.map((row, index) => {
    const matchesKnown = candidateMatchesKnownOriginal(entry, row);
    const previewTitle = matchesKnown ? "Known original candidate" : `Candidate ${index + 1}`;
    return `
    <div class="candidate-card ${matchesKnown ? "known-match" : ""}">
      ${row.candidate_url ? `
        <button class="image-preview-trigger" type="button" onclick="openBroadImagePreviewFromTrigger(this)" data-preview-url="${escapeHtml(row.candidate_url)}" data-preview-title="${escapeHtml(previewTitle)}" data-preview-path="${escapeHtml(row.candidate_path || "")}" aria-label="Open larger ${escapeHtml(previewTitle)} image">
          <img class="candidate-image" src="${escapeHtml(row.candidate_url)}" loading="lazy" alt="">
        </button>
      ` : `<div class="candidate-image subtle">Image unavailable.</div>`}
      <div class="candidate-meta">
        <div class="candidate-title">${matchesKnown ? "Known original" : "Candidate"}${matchesKnown ? ` <span class="match-badge">match</span>` : ""}</div>
        <div class="candidate-filename-row">
          <div class="file-name" title="${escapeHtml(row.candidate_path || "")}">${escapeHtml(row.candidate_filename || fileName(row.candidate_path))}</div>
          ${locationIndicator(row.has_geolocation)}
        </div>
        <div class="facts">${escapeHtml(formatBroadPhotoFacts({
          mime_type: row.mime_type,
          byte_size: row.byte_size,
          dimensions: row.dimensions,
          has_geolocation: row.has_geolocation
        }))}</div>
      </div>
      ${mode === "search" && row.result_id && row.candidate_url
        ? `<div class="candidate-actions"><button class="action-button primary" onclick="confirmBroadMatch(${Number(row.result_id || 0)})">Match</button></div>`
        : ""}
    </div>
  `;
  }).join("");
}
function candidateMatchesKnownOriginal(entry, row) {
  return Boolean(entry.confirmed_path && row.candidate_path && entry.confirmed_path === row.candidate_path);
}
function locationIndicator(hasLocation) {
  return `
    <svg class="location-indicator ${hasLocation ? "has-location" : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="${hasLocation ? "Embedded location metadata" : "No embedded location metadata"}">
      <circle cx="12" cy="12" r="10"></circle>
      <path d="M2 12h20M12 2a15.3 15.3 0 0 1 0 20M12 2a15.3 15.3 0 0 0 0 20"></path>
    </svg>
  `;
}
function largeBroadImageUrl(url) {
  const parts = String(url || "").split("#");
  const hash = parts.length > 1 ? `#${parts.slice(1).join("#")}` : "";
  const [base, queryText = ""] = parts[0].split("?");
  const params = new URLSearchParams(queryText);
  params.set("max", "2048");
  return `${base}?${params.toString()}${hash}`;
}
function openBroadImagePreviewFromTrigger(trigger) {
  if (!trigger) return;
  openBroadImagePreview(trigger.dataset.previewUrl || "", trigger.dataset.previewTitle || "Image preview", trigger.dataset.previewPath || "");
}
function openBroadImagePreview(url, title, path) {
  const modal = document.getElementById("imagePreviewModal");
  const image = document.getElementById("imagePreviewImage");
  const caption = document.getElementById("imagePreviewCaption");
  const closeButton = modal?.querySelector(".image-preview-close");
  if (!modal || !image || !caption || !url) return;
  caption.textContent = path ? `${title} | ${fileName(path)}` : title;
  image.alt = title || "Large image preview";
  image.src = largeBroadImageUrl(url);
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
  closeButton?.focus();
}
function closeBroadImagePreview() {
  const modal = document.getElementById("imagePreviewModal");
  const image = document.getElementById("imagePreviewImage");
  if (!modal || !image) return;
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
  image.removeAttribute("src");
}
function handleImagePreviewBackdrop(event) {
  if (event.target?.id === "imagePreviewModal") closeBroadImagePreview();
}
async function confirmBroadMatch(resultId) {
  if (!resultId) {
    setStatus("This row cannot be matched.", true);
    return;
  }
  setStatus("Recording broad visual match...");
  setReviewBusy(true);
  try {
    const payload = await fetchJson("/broad/api/confirm-match", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({result_id: resultId})
    });
    setLastBroadDecision(payload);
    setStatus(`Matched ${payload.entry_id || "entry"}. Loading next entry...`);
    await loadEntriesAfterRemoval(payload.entry_date || "");
  } catch (error) {
    setStatus(error.message, true);
    setReviewBusy(false);
  }
}
async function rejectBroadEntry(runId, entryId) {
  if (!entryId) {
    setStatus("This entry cannot be rejected.", true);
    return;
  }
  setStatus("Recording broad visual rejection...");
  setReviewBusy(true);
  try {
    const payload = await fetchJson("/broad/api/reject-entry", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({run_id: runId, entry_id: entryId, review_set: currentReviewSet})
    });
    setLastBroadDecision(payload);
    setStatus(`Rejected ${payload.rejected_count || 0} candidates for ${payload.entry_id || entryId}. Loading next entry...`);
    await loadEntriesAfterRemoval(payload.entry_date || "");
  } catch (error) {
    setStatus(error.message, true);
    setReviewBusy(false);
  }
}
async function keepProject365Photo(runId, entryId) {
  if (!entryId) {
    setStatus("This entry cannot use the Project365 photo.", true);
    return;
  }
  if (!window.confirm("Keep the Project365 export for this entry and remove it from Broad Visual Review?")) return;
  setStatus("Recording Project365 photo fallback...");
  setReviewBusy(true);
  try {
    const payload = await fetchJson("/broad/api/keep-project365", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({run_id: runId, entry_id: entryId, review_set: currentReviewSet})
    });
    setLastBroadDecision(payload);
    setStatus(`Kept Project365 photo for ${payload.entry_id || entryId}. Loading next entry...`);
    await loadEntriesAfterRemoval(payload.entry_date || "");
  } catch (error) {
    setStatus(error.message, true);
    setReviewBusy(false);
  }
}
function formatBroadPhotoFacts(facts = {}) {
  const parts = [];
  const mime = String(facts.mime_type || "");
  if (mime) parts.push(mime.replace(/^image\//, "").toUpperCase());
  const size = formatFileSize(Number(facts.byte_size || 0));
  if (size) parts.push(size);
  if (String(facts.dimensions || "").trim()) parts.push(String(facts.dimensions).trim());
  return parts.join(" | ");
}
function formatFileSize(bytes) {
  if (!bytes) return "";
  if (bytes >= 1000000000) return `${(bytes / 1000000000).toFixed(1)} GB`;
  if (bytes >= 1000000) return `${(bytes / 1000000).toFixed(1)} MB`;
  if (bytes >= 1000) return `${(bytes / 1000).toFixed(1)} KB`;
  return `${bytes} B`;
}
function previousPage() {
  if (currentEntryDate) loadEntriesByDate("before", currentEntryDate);
  else loadEntries(Math.max(0, currentOffset - currentLimit));
}
function nextPage() {
  if (currentEntryDate) loadEntriesByDate("after", currentEntryDate);
  else if (currentHasMore) loadEntries(currentOffset + currentLimit);
}
function resetBroadReviewScrollToCandidateList() {
  const target = document.querySelector(".candidate-pane") || document.getElementById("entries");
  if (!target) return;
  target.scrollIntoView({block: "start", inline: "nearest"});
}
async function undoLastBroadDecision() {
  if (!lastBroadDecision) {
    setStatus("No Broad Visual action is available to undo.", true);
    return;
  }
  const action = lastBroadDecision;
  setReviewBusy(true);
  setStatus("Undoing last Broad Visual action...");
  try {
    const payload = await fetchJson("/broad/api/undo-entry-decision", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({run_id: action.run_id, entry_id: action.entry_id, review_set: currentReviewSet})
    });
    setLastBroadDecision(null);
    setStatus(`Undid ${payload.decision || "decision"} for ${payload.entry_id || action.entry_id}. Reloading entry...`);
    await loadEntryById(payload.entry_id || action.entry_id, payload.run_id || action.run_id);
  } catch (error) {
    setStatus(error.message, true);
    setReviewBusy(false);
  }
}
function setLastBroadDecision(payload) {
  if (payload && payload.run_id && payload.entry_id) {
    lastBroadDecision = {
      run_id: String(payload.run_id),
      entry_id: String(payload.entry_id),
      decision: String(payload.decision || "")
    };
  } else {
    lastBroadDecision = null;
  }
  updateUndoButton();
}
function updateUndoButton() {
  const button = document.getElementById("undoBroadDecisionButton");
  if (!button) return;
  button.disabled = !lastBroadDecision;
}
function setReviewBusy(isBusy) {
  currentReviewBusy = Boolean(isBusy);
  document.getElementById("entries").classList.toggle("review-loading", Boolean(isBusy));
  document.querySelectorAll("button").forEach(button => {
    button.disabled = Boolean(isBusy);
  });
  if (!isBusy) updateUndoButton();
}
function isBroadShortcutEditableTarget(target) {
  if (!target) return false;
  const focusedControl = target.closest?.("input, select, textarea, button, a, [contenteditable='true']");
  return Boolean(focusedControl);
}
function handleBroadReviewKeyboardShortcut(event) {
  const previewModal = document.getElementById("imagePreviewModal");
  if (event.key === "Escape" && previewModal?.classList.contains("is-open")) {
    closeBroadImagePreview();
    event.preventDefault();
    return;
  }
  if (currentReviewBusy || event.repeat || event.metaKey || event.ctrlKey || event.altKey || isBroadShortcutEditableTarget(event.target)) return;
  let handled = false;
  if (event.key === "ArrowLeft") {
    previousPage();
    handled = true;
  } else if (event.key === "ArrowRight") {
    nextPage();
    handled = true;
  } else if (event.key.toLowerCase() === "r" && currentReviewMode === "search") {
    rejectBroadEntry(currentRunId, currentEntryId);
    handled = true;
  }
  if (handled) event.preventDefault();
}
function setStatus(message, error = false) {
  const target = document.getElementById("status");
  target.textContent = message;
  target.classList.toggle("bad", error);
}
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[char]));
}
function escapeJs(value) {
  return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}
function fileName(path) {
  const text = String(path || "");
  if (!text) return "";
  const parts = text.split("/");
  return parts[parts.length - 1] || text;
}
applyInitialQuery();
updateUndoButton();
document.addEventListener("keydown", handleBroadReviewKeyboardShortcut);
loadEntries(0);
</script>
</body>
</html>
"""


CONTROL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project365 Control</title>
<style>
:root {
  --bg: #f6f6f2;
  --panel: #fff;
  --ink: #202124;
  --muted: #667085;
  --line: #d7d9d2;
  --accent: #17695d;
  --accent-soft: #dceeea;
  --fail: #9f2d2d;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
button, input, select { font: inherit; }
.shell {
  max-width: 1180px;
  margin: 0 auto;
  padding: 18px;
  display: grid;
  gap: 14px;
}
.header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
}
h1 {
  margin: 0;
  font-size: 24px;
}
.subtle {
  color: var(--muted);
  font-size: 13px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.metric-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
}
.metric-card span {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.metric-card strong {
  display: block;
  margin-top: 4px;
  font-size: 24px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
}
.workflow {
  display: grid;
  gap: 12px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  display: grid;
  gap: 10px;
}
.panel h2 {
  margin: 0;
  font-size: 16px;
}
.workflow-step {
  padding: 0;
  overflow: hidden;
}
.workflow-step.is-running {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}
.workflow-step-header {
  display: grid;
  grid-template-columns: minmax(220px, 0.55fr) minmax(280px, 1fr) auto;
  gap: 12px;
  align-items: center;
  padding: 12px;
  border-bottom: 1px solid var(--line);
}
.workflow-step:not(.is-expanded) .workflow-step-header {
  border-bottom: 0;
}
.step-kicker {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
.step-status-bar {
  min-height: 34px;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  color: var(--muted);
  font-size: 13px;
  text-align: left;
}
.status-pill {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0 8px;
  background: #f9faf8;
  color: var(--muted);
  font-weight: 600;
}
.status-pill.pass { color: var(--accent); border-color: var(--accent-soft); background: var(--accent-soft); }
.status-pill.fail,
.status-pill.cancelled { color: var(--fail); border-color: #efd0d0; background: #fff1f1; }
.heartbeat {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 0 rgb(23 105 93 / 45%);
  animation: pulse 1.2s ease-out infinite;
}
.workflow-step-body {
  display: grid;
  gap: 10px;
  padding: 12px;
}
.workflow-step:not(.is-expanded) .workflow-step-body {
  display: none;
}
.step-toggle {
  justify-self: end;
}
.step-result,
.step-history {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #f9faf8;
  padding: 9px;
  display: grid;
  gap: 8px;
  font-size: 13px;
}
.step-result:empty,
.step-history:empty {
  display: none;
}
.result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}
.result-metric {
  border-bottom: 1px solid var(--line);
  padding-bottom: 5px;
}
.result-metric span {
  color: var(--muted);
  display: block;
  font-size: 12px;
}
.result-metric strong {
  display: block;
  margin-top: 2px;
}
.run-list {
  display: grid;
  gap: 5px;
}
.run-list div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 5px;
}
.archive-table {
  width: 100%;
  border-collapse: collapse;
}
.archive-table th,
.archive-table td {
  border-bottom: 1px solid var(--line);
  padding: 5px 4px;
  text-align: left;
}
.button-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.button {
  border: 1px solid var(--line);
  background: #fff;
  border-radius: 6px;
  min-height: 34px;
  padding: 0 10px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease, transform 80ms ease;
}
a.button {
  text-decoration: none;
  color: var(--ink);
}
.button.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.button.danger {
  color: var(--fail);
  border-color: var(--fail);
}
.button.small {
  padding: 4px 8px;
  font-size: 12px;
}
.button:active {
  transform: translateY(1px);
  box-shadow: inset 0 0 0 999px rgb(0 0 0 / 8%);
}
.button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.button:disabled {
  opacity: 0.55;
  cursor: wait;
}
.button.is-running {
  opacity: 1;
  cursor: progress;
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}
.button.is-running::after {
  content: "";
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-left: 8px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: -1px;
}
.inline-status {
  color: var(--subtle);
  font-size: 13px;
}
.workflow-guide {
  margin: 0;
  padding-left: 20px;
  color: var(--subtle);
  font-size: 13px;
  line-height: 1.45;
}
.field {
  display: grid;
  gap: 5px;
}
.field label {
  font-size: 12px;
  color: var(--muted);
}
.field-hint {
  color: var(--subtle);
  font-size: 12px;
  line-height: 1.35;
}
.folder-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}
input {
  width: 100%;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0 8px;
}
input[type="checkbox"] {
  width: 16px;
  height: 16px;
}
.checkbox-line {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 13px;
}
.inline-status {
  min-height: 20px;
  color: var(--muted);
  font-size: 13px;
}
.inline-status.running {
  color: var(--accent);
}
.inline-status.error {
  color: var(--fail);
}
select {
  width: 100%;
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  padding: 0 8px;
}
pre {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #202124;
  color: #f5f5f5;
  border-radius: 8px;
  padding: 12px;
  max-height: 360px;
  overflow: auto;
}
.status-list {
  display: grid;
  gap: 6px;
}
.status-item {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 6px;
  font-size: 13px;
}
.status-item span {
  flex: 0 0 auto;
}
.status-item strong {
  text-align: right;
  overflow-wrap: anywhere;
}
.status-note {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #f9faf8;
  color: var(--muted);
  padding: 8px;
  font-size: 13px;
}
.batch-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.batch-table th,
.batch-table td {
  border-bottom: 1px solid var(--line);
  padding: 6px 4px;
  text-align: left;
  vertical-align: top;
}
.batch-table th {
  color: var(--muted);
  font-weight: 600;
}
.batch-table .number {
  text-align: right;
}
.ok { color: var(--accent); }
.bad { color: var(--fail); }
.attempt-warning {
  border: 1px solid #d8b45b;
  background: #fff8e1;
  border-radius: 6px;
  color: #614700;
  padding: 8px;
  font-size: 13px;
}
.attempt-warning[hidden] {
  display: none;
}
.compact-list {
  display: grid;
  gap: 6px;
  font-size: 13px;
}
.compact-list div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 5px;
}
.compact-list strong {
  text-align: right;
}
.compact-list .coverage-table-wrap {
  display: block;
  border-bottom: 1px solid var(--line);
  padding-bottom: 5px;
}
.compact-list .review-runs-wrap {
  display: grid;
  gap: 6px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 5px;
}
.compact-list .review-runs-head,
.compact-list .review-run-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: center;
  border-bottom: 0;
  padding-bottom: 0;
}
.review-run-label {
  overflow-wrap: anywhere;
}
.review-run-meta {
  color: var(--muted);
  font-size: 12px;
}
.review-run-actions {
  display: flex;
  gap: 6px;
}
.coverage-table-wrap h3 {
  margin: 0 0 6px;
}
.coverage-table-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
.coverage-table-head h3 {
  margin: 0;
}
.compact-list .coverage-check-status {
  display: block;
  border-bottom: 0;
  padding-bottom: 6px;
  color: var(--muted);
}
.compact-list .coverage-check-status.status-error {
  color: var(--fail);
}
.compact-list .coverage-check-status.status-running {
  color: var(--accent);
}
.coverage-table-scroll {
  max-height: 260px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
}
.coverage-table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}
.coverage-table th,
.coverage-table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--line);
  text-align: right;
  white-space: nowrap;
}
.coverage-table th:first-child,
.coverage-table td:first-child {
  text-align: left;
}
.coverage-table tr.low-coverage td {
  color: var(--fail);
  font-weight: 600;
}
.step-list {
  margin: 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 13px;
}
.diagnostic-section {
  border-top: 1px solid var(--line);
  padding-top: 10px;
  display: grid;
  gap: 8px;
}
.diagnostic-section h3 {
  margin: 0;
}
.run-progress {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #f9faf8;
  padding: 10px;
  display: grid;
  gap: 8px;
}
.run-progress[hidden] {
  display: none;
}
.run-progress-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
}
.run-progress-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.run-progress-detail {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}
.run-progress-text {
  min-width: 0;
}
.run-progress-live {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  animation: live-pulse 1s ease-in-out infinite;
}
.run-progress.status-pass .run-progress-live,
.run-progress.status-fail .run-progress-live,
.run-progress.status-cancelled .run-progress-live {
  animation: none;
}
.run-progress-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--accent-soft);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  flex: 0 0 auto;
}
.run-progress.status-pass .spinner {
  animation: none;
  border-color: var(--accent);
  border-top-color: var(--accent);
}
.run-progress.status-fail .spinner,
.run-progress.status-cancelled .spinner {
  animation: none;
  border-color: var(--fail);
  border-top-color: var(--fail);
}
.progress-track {
  height: 4px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--accent-soft);
}
.progress-bar {
  width: 40%;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  animation: progress-slide 1.2s ease-in-out infinite;
}
.run-progress.status-pass .progress-bar,
.run-progress.status-fail .progress-bar,
.run-progress.status-cancelled .progress-bar {
  width: 100%;
  animation: none;
  transform: none;
}
.run-progress.status-fail .progress-bar,
.run-progress.status-cancelled .progress-bar {
  background: var(--fail);
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
@keyframes live-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}
@keyframes progress-slide {
  0% { transform: translateX(-105%); }
  100% { transform: translateX(255%); }
}
@keyframes pulse {
  100% { box-shadow: 0 0 0 10px rgb(23 105 93 / 0%); }
}
@media (max-width: 760px) {
  .metric-grid,
  .workflow-step-header {
    grid-template-columns: 1fr;
  }
  .step-status-bar {
    justify-content: flex-start;
    text-align: left;
  }
}
a { color: var(--accent); }
</style>
</head>
<body>
<main class="shell">
  <div class="header">
    <div>
      <h1>Project365 Control</h1>
      <div class="subtle">Local workflow runner for canonical import, original-photo review, tagging, derivatives, and Diarium packages.</div>
    </div>
    <button class="button" onclick="loadStatus()">Refresh</button>
  </div>

  <section class="metric-grid" aria-label="Project summary">
    <div class="metric-card"><span>Unique diary days</span><strong id="metricUniqueDays">__INITIAL_UNIQUE_DIARY_DAYS__</strong></div>
    <div class="metric-card"><span>Total diary entries</span><strong id="metricDiaryEntries">__INITIAL_DIARY_ENTRIES__</strong></div>
    <div class="metric-card"><span>Missing photos</span><strong id="metricMissingPhotos">__INITIAL_MISSING_PHOTOS__</strong></div>
    <div class="metric-card"><span>Associated photos</span><strong id="metricAssociatedPhotos">__INITIAL_ASSOCIATED_PHOTOS__</strong></div>
    <div class="metric-card"><span>Photos in index</span><strong id="metricPhotoIndexFiles">__INITIAL_PHOTO_INDEX_FILES__</strong></div>
  </section>
  <div id="statusLoadError" class="inline-status error" role="status" aria-live="polite" hidden></div>

  <section class="workflow" id="workflow">
    <section class="panel workflow-step" data-step="import_zips">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 1</div>
          <h2>Import Project365 Pro zips</h2>
        </div>
        <div class="step-status-bar" data-step-status="import_zips">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="import_zips" onclick="toggleWorkflowStep('import_zips')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Imports new, changed, and existing zips idempotently into the canonical database.</div>
      <button class="button primary" onclick="runStep('import_zips')">Import zips</button>
      <div class="step-result" data-step-result="import_zips"></div>
      <div class="step-history" data-step-history="import_zips"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="build_photo_index">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 2</div>
          <h2>Build photo index</h2>
        </div>
        <div class="step-status-bar" data-step-status="build_photo_index">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="build_photo_index" onclick="toggleWorkflowStep('build_photo_index')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Adds photo metadata from selected folders into the reusable local index.</div>
      <div id="photoIndexBox" class="compact-list"></div>
      <div class="field">
        <label>Index folders, separated by semicolons</label>
        <div class="folder-row">
          <input id="indexRoots" placeholder="/Volumes/External Drive/Photos; /another/folder">
          <button class="button" onclick="chooseFolderInFinder('indexRoots')">Choose folder</button>
        </div>
        <div class="subtle">Leave blank to refresh existing indexed folders. First run only: blank scans Source Data.</div>
      </div>
      <label class="checkbox-line">
        <input id="resetPhotoIndex" type="checkbox">
        Replace existing photo index
      </label>
      <button class="button primary" onclick="buildPhotoIndex()">Build photo index</button>
      <label class="checkbox-line">
        <input id="reconcileMovedPhotoIndex" type="checkbox">
        Moved/renamed only
      </label>
      <button class="button" onclick="refreshPhotoIndexMetadata()">Refresh existing index metadata</button>
      <div id="photoIndexMessage" class="inline-status" role="status" aria-live="polite"></div>
      <div class="step-result" data-step-result="build_photo_index"></div>
      <div class="step-history" data-step-history="build_photo_index"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="match_easy_originals">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 3</div>
          <h2>Original-photo review</h2>
        </div>
        <div class="step-status-bar" data-step-status="match_easy_originals">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="match_easy_originals" onclick="toggleWorkflowStep('match_easy_originals')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <ol class="step-list">
        <li>Match existing originals already in Source Data.</li>
        <li>Use the photo index for entries still missing an original.</li>
        <li>Review batches, search folders, and choose the identified original in the visual picker.</li>
      </ol>
      <label class="checkbox-line">
        <input id="limitEasyMatchToIndexFolder" type="checkbox">
        Limit easy matches to one indexed folder
      </label>
      <label class="checkbox-line">
        <input id="includeLowQualityMatches" type="checkbox">
        Recheck low-quality confirmed matches
      </label>
      <div class="field">
        <label>Indexed folder for easy matches</label>
        <div class="folder-row">
          <input id="easyMatchIndexFolder" placeholder="/Volumes/External Drive/Photos/One Folder">
          <button class="button" onclick="chooseFolderFromIndexedFile('easyMatchIndexFolder')">Choose file in folder</button>
        </div>
        <input id="easyMatchIndexFile" type="file" accept="image/*" hidden>
        <div class="subtle">When enabled, the photo index contributes matches only from this folder and its subfolders.</div>
      </div>
      <button class="button primary" onclick="runEasyMatch()">Find easy matches</button>
      <button id="reviewEasyMatchesButton" class="button" onclick="openEasyMatchesInPicker()">Review easy matches (0)</button>
      <div id="easyMatchMessage" class="inline-status" role="status" aria-live="polite"></div>
      <a href="__PICKER_URL__">Open visual picker</a>
      <div class="step-result" data-step-result="match_easy_originals"></div>
      <div class="step-history" data-step-history="match_easy_originals"></div>
      <div>
        <h2>Original-photo batches</h2>
        <div id="batchOverview" class="subtle">No batch data loaded.</div>
      </div>
      <div>
        <h2>Recent original-photo searches</h2>
        <div id="attemptOverview" class="subtle">No search attempts yet.</div>
      </div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="broad_visual_match">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 4</div>
          <h2>Broad Visual Match</h2>
        </div>
        <div class="step-status-bar" data-step-status="broad_visual_match">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="broad_visual_match" onclick="toggleWorkflowStep('broad_visual_match')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Separate broad search for unresolved originals. A new search becomes the current Broad review set.</div>
      <ol class="workflow-guide">
        <li><strong>Build fingerprints</strong> fingerprints original-photo candidates. Existing current fingerprints are reused unless "overwrite existing fingerprints" is checked.</li>
        <li><strong>Search unresolved photos</strong> searches entries without a confirmed original and saves candidates for review.</li>
        <li><strong>Review unresolved results</strong> opens the current review-ready unresolved-search candidates.</li>
      </ol>
      <div id="broadVisualBox" class="compact-list"></div>
      <div class="field">
        <label title="Original-photo folders to fingerprint when building a normal broad index. Not needed when indexing confirmed originals only.">Original-photo folders to fingerprint/search</label>
        <div class="folder-row">
          <input id="broadCandidateRoots" placeholder="/Volumes/External Drive/Photos">
          <button class="button" onclick="chooseFolderInFinder('broadCandidateRoots')">Choose folder</button>
        </div>
        <div class="field-hint">Leave blank to reuse existing photo-index roots. Confirmed-only test indexes use the confirmed original paths for the selected Project365 entries.</div>
      </div>
      <div class="grid">
        <div class="field">
          <label title="Which Project365 entries to search. These dates are the target entries, not candidate-photo metadata dates.">Project365 entries to search</label>
          <select id="broadTargetScope">
            <option value="all_unresolved">All unresolved</option>
            <option value="entry_ids">Specific entries</option>
            <option value="date_range">Date range</option>
            <option value="broad_search_needed_list">Broad-search-needed list</option>
          </select>
          <div class="field-hint">Use Date range or Specific entries for a small first pass before running a broad unresolved search.</div>
        </div>
        <div class="field">
          <label title="Which indexed original-photo candidates each Project365 entry is compared against. Date-window is the safe default for unresolved date-range searches.">Original candidates to compare</label>
          <select id="broadCandidateScope">
            <option value="date_window_limited">Only candidates dated near each entry</option>
            <option value="whole_indexed_library">Whole indexed library</option>
            <option value="folder_limited">Folder-limited</option>
            <option value="same_setting_folder_limited">Same-setting/folder-limited</option>
          </select>
          <div class="field-hint">Date-window compares each target only with already-fingerprinted candidates whose own dates are nearby. Whole indexed library is intentionally broad.</div>
        </div>
        <div class="field">
          <label title="How many candidate originals to keep for each Project365 entry.">Candidates kept per entry</label>
          <input id="broadMaxResults" type="number" min="1" value="20">
        </div>
        <div class="field">
          <label title="How many square crop positions to sample across a landscape candidate. Higher can help off-center crops but takes longer.">Square crop positions</label>
          <input id="broadDensity" type="number" min="1" value="9">
          <div class="field-hint">Default 9 is a balanced setting for landscape originals cropped into square Project365 exports.</div>
        </div>
        <div class="field">
          <label title="Used when Original candidates to compare is set to 'Only candidates dated near each entry'. A value of 30 means candidates within plus/minus 30 days of each Project365 entry date.">Candidate date window, +/- days</label>
          <input id="broadDateWindowDays" type="number" min="0" value="30">
          <div class="field-hint">Default 30 keeps date-range unresolved searches bounded to nearby already-fingerprinted originals.</div>
        </div>
        <div class="field">
          <label title="Use only when Project365 entries to search is set to Specific entries. Separate multiple dates or IDs with semicolons.">Specific entry dates</label>
          <input id="broadEntryIds" placeholder="2026-09-11; 2026-09-12">
          <div class="field-hint">Dates are converted to Project365 entry IDs when the command runs.</div>
        </div>
        <div class="field">
          <label title="First Project365 entry date for a date-range target scope. Candidate filtering is controlled by Original candidates to compare.">Target start date</label>
          <input id="broadStartDate" placeholder="YYYY-MM-DD or YYYY-MM">
        </div>
        <div class="field">
          <label title="Last Project365 entry date for a date-range target scope. Candidate filtering is controlled by Original candidates to compare.">Target end date</label>
          <input id="broadEndDate" placeholder="YYYY-MM-DD or YYYY-MM">
        </div>
        <div class="field">
          <label title="Optional file containing Project365 entry IDs that need broad search.">Broad-search-needed list</label>
          <input id="broadNeededList" placeholder="/path/to/entry_ids.csv">
        </div>
      </div>
      <label class="checkbox-line" title="Include candidates that the broad index would otherwise skip as low quality. Usually leave off for the first pass."><input id="broadIncludeLowQuality" type="checkbox"> Include low-quality candidates</label>
      <label class="checkbox-line" title="Only affects Search unresolved photos. Continues the current Broad review set if one exists."><input id="broadResumeExisting" type="checkbox"> Continue current search</label>
      <label class="checkbox-line" title="Only affects Build/refresh index. When unchecked, already-current fingerprints are skipped for speed. When checked, every scanned item is recomputed."><input id="broadOverwriteFingerprints" type="checkbox"> Overwrite existing fingerprints</label>
      <label class="checkbox-line" title="Show what would run without writing index, match, or report output."><input id="broadDryRun" type="checkbox"> Dry run</label>
      <div class="button-row">
        <button class="button primary" title="Step 1. Fingerprint original-photo candidates into the separate broad database." onclick="buildBroadVisualIndex()">1. Build fingerprints</button>
        <button class="button primary" title="Step 2. Uses an existing index to precompute broad candidates for unresolved-photo review." onclick="runBroadVisualMatch()">2. Search unresolved photos</button>
        <button id="broadReviewResultsButton" class="button primary" data-review-mode="search" title="Step 3. Opens saved unresolved Broad Visual results. The count updates after status loads." onclick="openBroadVisualReview()">3. Review unresolved results (loading)</button>
      </div>
      <div id="broadVisualActionMessage" class="inline-status" role="status" aria-live="polite"></div>
      <div class="review-options">
        <h3>Review options</h3>
        <div class="grid">
          <div class="field">
            <label title="Optional jump target for already-saved review results. Use a Project365 date, or paste a raw project365:YYYY-MM-DD entry ID from a result URL.">Jump to entry date</label>
            <input id="broadReviewEntryId" placeholder="YYYY-MM-DD or project365:YYYY-MM-DD">
            <div class="field-hint">Leave blank to open the first saved result.</div>
          </div>
          <div class="field">
            <label>Review page</label>
            <div class="field-hint">Opens one target and its candidates at a time.</div>
          </div>
        </div>
      </div>
      <div class="diagnostic-section">
        <h3>Accuracy diagnostics</h3>
        <div class="field-hint">Optional known-original benchmark. It does not create unresolved review rows.</div>
        <label class="checkbox-line" title="Only affects Build fingerprints for accuracy benchmarks. It fingerprints originals that are already confirmed for the selected Project365 entries; it does not rebuild monthly candidate coverage."><input id="broadConfirmedOnly" type="checkbox"> Fingerprint already confirmed originals for accuracy benchmark</label>
        <div class="button-row">
          <button class="button" title="Known-answer diagnostic. Exports accuracy numbers for entries that already have confirmed originals." onclick="runBroadVisualBenchmark()">Measure accuracy</button>
          <button class="button" title="Open the latest known-original accuracy benchmark review." onclick="openBroadVisualReview('benchmark')">Open accuracy benchmark review</button>
        </div>
        <div id="broadVisualBenchmarkMessage" class="inline-status" role="status" aria-live="polite"></div>
      </div>
      <div class="step-result" data-step-result="broad_visual_match"></div>
      <div class="step-history" data-step-history="broad_visual_match"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="rough_visual_match">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 4B</div>
          <h2>No-Date Visual Match</h2>
        </div>
        <div class="step-status-bar" data-step-status="rough_visual_match">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="rough_visual_match" onclick="toggleWorkflowStep('rough_visual_match')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Separate no-date search for unresolved originals. It builds a rough prefilter from stored fingerprints, shortlists candidates without date constraints, then uses the dense scorer only on the shortlist.</div>
      <div id="roughVisualBox" class="compact-list"></div>
      <div class="grid">
        <div class="field">
          <label title="Which unresolved Project365 entries to search without candidate date constraints.">Project365 entries to search</label>
          <select id="roughTargetScope">
            <option value="all_unresolved">All unresolved</option>
            <option value="entry_ids">Specific entries</option>
            <option value="date_range">Date range</option>
            <option value="broad_search_needed_list">Broad-search-needed list</option>
          </select>
        </div>
        <div class="field">
          <label title="Maximum rough candidates loaded into the dense scorer for each Project365 entry.">Shortlist size</label>
          <input id="roughShortlistSize" type="number" min="1" value="1000">
        </div>
        <div class="field">
          <label title="Maximum stored prefilter rows accepted from any one rough lookup band.">Per-band hit cap</label>
          <input id="roughPerBandHitLimit" type="number" min="1" value="50000">
        </div>
        <div class="field">
          <label title="How many final candidates to save per Project365 entry for manual review.">Candidates kept per entry</label>
          <input id="roughMaxResults" type="number" min="1" value="20">
        </div>
        <div class="field">
          <label title="Must match the density used for the stored broad fingerprints.">Square crop positions</label>
          <input id="roughDensity" type="number" min="1" value="9">
        </div>
        <div class="field">
          <label title="Use only when Project365 entries to search is set to Specific entries. Separate multiple dates or IDs with semicolons.">Specific entry dates</label>
          <input id="roughEntryIds" placeholder="2026-09-11; 2026-09-12">
          <div class="field-hint">Dates are converted to Project365 entry IDs when the command runs.</div>
        </div>
        <div class="field">
          <label title="First unresolved target date for a bounded no-date run. Candidate dates are ignored.">Target start date</label>
          <input id="roughStartDate" placeholder="YYYY-MM-DD or YYYY-MM">
        </div>
        <div class="field">
          <label title="Last unresolved target date for a bounded no-date run. Candidate dates are ignored.">Target end date</label>
          <input id="roughEndDate" placeholder="YYYY-MM-DD or YYYY-MM">
        </div>
        <div class="field">
          <label title="Optional file containing Project365 entry IDs that need broad search.">Broad-search-needed list</label>
          <input id="roughNeededList" placeholder="/path/to/entry_ids.csv">
        </div>
        <div class="field">
          <label title="Only affects Measure no-date accuracy. Reports rough prefilter recall at several shortlist cutoffs.">Accuracy benchmark depth</label>
          <select id="roughBenchmarkShortlistSizes">
            <option value="100,500,1000,5000" selected>Standard: 100 / 500 / 1,000 / 5,000</option>
            <option value="100,500,1000">Quick: 100 / 500 / 1,000</option>
            <option value="500,1000,5000,10000">Deep: 500 / 1,000 / 5,000 / 10,000</option>
          </select>
        </div>
      </div>
      <label class="checkbox-line" title="Include candidates that the broad index marked as low-quality."><input id="roughIncludeLowQuality" type="checkbox"> Include low-quality candidates</label>
      <label class="checkbox-line" title="Only affects Build rough prefilter. When unchecked, current rows are reused."><input id="roughOverwritePrefilter" type="checkbox"> Overwrite existing prefilter rows</label>
      <label class="checkbox-line" title="Only affects no-date search. Continues the current no-date review set if one exists."><input id="roughResumeExisting" type="checkbox"> Continue current no-date search</label>
      <label class="checkbox-line" title="Show what would run without writing prefilter, match, or report output."><input id="roughDryRun" type="checkbox"> Dry run</label>
      <div class="button-row">
        <button class="button primary" title="Build compact rough lookup rows from stored broad fingerprints." onclick="buildRoughPrefilter()">1. Build rough prefilter</button>
        <button class="button" title="Check whether rough prefilter rows exist for the current dense fingerprint method." onclick="checkRoughPrefilterReadiness()">Check readiness</button>
        <button class="button primary" title="Search unresolved entries without candidate date constraints, then densely score only the shortlist." onclick="runRoughVisualMatch()">2. Search no-date matches</button>
        <button class="button" title="Benchmark prefilter recall separately from final dense rank recall." onclick="runRoughVisualBenchmark()">Measure no-date accuracy</button>
        <button id="roughReviewResultsButton" class="button" title="Open the current no-date visual review set." onclick="openRoughVisualReview()">Review no-date results</button>
      </div>
      <div id="roughVisualMessage" class="inline-status" role="status" aria-live="polite"></div>
      <div class="step-result" data-step-result="rough_visual_match"></div>
      <div class="step-history" data-step-history="rough_visual_match"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="crop_confirmation">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 5</div>
          <h2>Crop confirmation</h2>
        </div>
        <div class="step-status-bar" data-step-status="crop_confirmation">Review selected originals</div>
        <button class="button small step-toggle" data-step-toggle="crop_confirmation" onclick="toggleWorkflowStep('crop_confirmation')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">After an original has been selected, confirm the square crop on the identified original next to the Project365 target.</div>
      <div id="cropConfirmationOverview" class="result-grid"></div>
      <div class="button-row">
	        <button id="estimateCropBatchButton" class="button" type="button" onclick="startCropEstimateBatch()">Batch estimate crop</button>
	        <label class="checkbox-line inline-checkbox">
	          <input id="applyCropEstimates" type="checkbox" checked>
	          Save estimates as calculated
	        </label>
        <button id="openCropConfirmationButton" class="button" type="button" onclick="openCropConfirmation()">Open crop confirmation</button>
        <span id="cropEstimateBatchStatus" class="inline-status" role="status" aria-live="polite"></span>
      </div>
      <div class="step-result" data-step-result="crop_confirmation"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="generate_derivatives">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 6</div>
          <h2>Working photo copies</h2>
        </div>
        <div class="step-status-bar" data-step-status="generate_derivatives">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="generate_derivatives" onclick="toggleWorkflowStep('generate_derivatives')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Creates date-organized JPEG working copies from confirmed originals where available. Unchanged copies are skipped unless forced.</div>
      <div class="grid">
        <div class="field">
          <label>Start date</label>
          <input id="workingCopyStartDate" placeholder="YYYY-MM-DD">
        </div>
        <div class="field">
          <label>End date</label>
          <input id="workingCopyEndDate" placeholder="YYYY-MM-DD">
        </div>
        <label class="checkbox-line inline-checkbox">
          <input id="forceWorkingCopies" type="checkbox">
          Force rewrite all matching working copies
        </label>
      </div>
      <div id="workingCopyReadiness" class="result-grid"></div>
      <button class="button primary" onclick="runWorkingCopies()">Generate working copies</button>
      <div id="workingCopyMessage" class="inline-status" role="status" aria-live="polite"></div>
      <div class="step-result" data-step-result="generate_derivatives"></div>
      <div class="step-history" data-step-history="generate_derivatives"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="face_tagging">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 7</div>
          <h2>People / face tagging</h2>
        </div>
        <div class="step-status-bar" data-step-status="face_tagging">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="face_tagging" onclick="toggleWorkflowStep('face_tagging')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Builds the metadata-safe people/tag review queue after the working copies exist. Scan the working-copy folder locally in digiKam, then import XMP sidecars or a suggestions CSV as reviewed suggestions.</div>
      <button class="button primary" onclick="runStep('face_tagging')">Build tag queue</button>
      <div class="field">
        <label>digiKam XMP folders, separated by semicolons</label>
        <div class="folder-row">
          <input id="digikamXmpRoots" placeholder="/path/to/working/copies/or/sidecars">
          <button class="button" onclick="chooseFolderInFinder('digikamXmpRoots')">Choose folder</button>
        </div>
      </div>
      <div class="field">
        <label>digiKam suggestions CSV</label>
        <input id="digikamSuggestionsCsv" placeholder="/path/to/digikam_people.csv">
      </div>
      <button class="button" onclick="importDigiKamPeople()">Import digiKam suggestions</button>
      <div id="digikamPeopleMessage" class="inline-status" role="status" aria-live="polite"></div>
      <div class="step-result" data-step-result="face_tagging"></div>
      <div class="step-history" data-step-history="face_tagging"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="diary_enrichment">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 8</div>
          <h2>Diary enrichment</h2>
        </div>
        <div class="step-status-bar" data-step-status="diary_enrichment">Choose date range</div>
        <button class="button small step-toggle" data-step-toggle="diary_enrichment" onclick="toggleWorkflowStep('diary_enrichment')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Add subordinate photos to existing diary entries or create same-date sub-entries after working copies and face tags are ready.</div>
      <div class="grid">
        <div class="field">
          <label>Start date</label>
          <input id="diaryEnrichmentStartDate" placeholder="YYYY-MM-DD">
        </div>
        <div class="field">
          <label>End date</label>
          <input id="diaryEnrichmentEndDate" placeholder="YYYY-MM-DD">
        </div>
        <div class="field">
          <label>Entry limit</label>
          <input id="diaryEnrichmentLimit" type="number" min="1" max="1000" value="200">
        </div>
      </div>
      <button class="button primary" type="button" onclick="openDiaryEnrichment()">Open diary enrichment</button>
      <div id="diaryEnrichmentMessage" class="inline-status" role="status" aria-live="polite"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="generate_diarium_package">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 9</div>
          <h2>Diarium import package</h2>
        </div>
        <div class="step-status-bar" data-step-status="generate_diarium_package">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="generate_diarium_package" onclick="toggleWorkflowStep('generate_diarium_package')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Generate a Day One ZIP. In Diarium, use Settings > Diary > Migrate from other app > Day One. Do not use Import diary; that expects a Diarium database backup and will say the ZIP is not a database.</div>
      <div class="field">
        <label>Start date</label>
        <input id="startDate" placeholder="auto">
      </div>
      <div class="field">
        <label>End date</label>
        <input id="endDate" placeholder="auto">
      </div>
      <div class="field">
        <label>Limit</label>
        <input id="limit" value="100000">
      </div>
      <button class="button primary" onclick="runDiariumPackage()">Generate package</button>
      <div class="step-result" data-step-result="generate_diarium_package"></div>
      <div class="step-history" data-step-history="generate_diarium_package"></div>
      </div>
    </section>
  </section>
  <pre id="output" hidden>No runs yet.</pre>
</main>

<script>
let running = false;
let lastStatus = {};
let progressTimer = null;
let progressStartedAt = null;
let progressStep = "";
let activeJobId = "";
let statusRefreshTimer = null;
let startRequestController = null;
const ACTIVE_STATUS_REFRESH_MS = 1500;
const STEP_OWNER = {
  refresh_photo_index_metadata: "build_photo_index",
  search_originals: "match_easy_originals",
  apply_original_decisions: "match_easy_originals",
  broad_visual_index: "broad_visual_match",
  broad_visual_benchmark: "broad_visual_match",
  rough_prefilter_build: "rough_visual_match",
  rough_visual_benchmark: "rough_visual_match",
  import_digikam_people: "face_tagging"
};
const initialStep = new URLSearchParams(window.location.search).get("step") || "";
const requestedInitialOwnerStep = ownerStep(initialStep);
const initialOwnerStep = workflowCard(requestedInitialOwnerStep) ? requestedInitialOwnerStep : "";
const manuallyExpandedSteps = new Set(initialOwnerStep ? [initialOwnerStep] : []);
async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Request failed");
  return payload;
}

function statusUrlFor(steps, options = {}) {
  const requestedSteps = Array.isArray(steps) ? steps.filter(Boolean) : expandedStatusSteps();
  if (!requestedSteps.length && !options.includeBroadMonthlyCoverage) return "/api/status";
  const params = new URLSearchParams();
  for (const step of requestedSteps) params.append("step", step);
  if (options.includeBroadMonthlyCoverage) params.set("include_broad_monthly_coverage", "1");
  return `/api/status?${params.toString()}`;
}

function expandedStatusSteps() {
  return Array.from(manuallyExpandedSteps).filter(step => Boolean(workflowCard(step)));
}

async function loadStatus(steps, options = {}) {
  try {
    const payload = await fetchJson(statusUrlFor(steps, options));
    lastStatus = payload;
    setStatusLoadError("");
    renderStatus(payload);
    renderWorkingCopyReadiness(payload);
    renderPhotoIndexBox(payload);
    renderEasyMatchBox(payload);
    renderBroadVisualBox(payload);
    renderRoughVisualBox(payload);
    renderCropConfirmationBox(payload);
    attachCropEstimateBatchJob(payload);
    renderBatchOverview(payload);
    renderAttemptOverview(payload);
    scheduleActiveStatusRefresh(payload);
    return payload;
  } catch (error) {
    setStatusLoadError(`Could not load current database status: ${error.message}`);
    throw error;
  }
}

function setStatusLoadError(message) {
  const target = document.getElementById("statusLoadError");
  if (!target) return;
  target.textContent = message;
  target.hidden = !message;
}

function renderStatus(payload) {
  renderTopMetrics(payload);
  renderWorkflowSummaries(payload);
  const status = document.getElementById("status");
  if (!status) return;
  const paths = payload.paths || {};
  const db = payload.database || {};
  const queue = payload.original_queue || {};
  const remainder = payload.original_remainder_overview || {};
  const batchPlan = payload.original_batch_plan || {};
  const nextBatch = batchPlan.next_batch || {};
  const attempts = payload.original_search_attempts || {};
  const latestAttempt = attempts.latest || {};
  const photoIndex = payload.photo_library_index || {};
  const packageStatus = payload.diarium_package || {};
  const diariumLocal = payload.diarium_local || {};
  const diariumVerification = payload.diarium_import_verification || {};
  status.innerHTML = `
    ${pathLine("Project365 Pro zips", paths.project365_zips)}
    ${pathLine("Original photos", paths.original_photos)}
    ${pathLine("Canonical database", paths.canonical_db)}
    ${pathLine("Photo index", paths.photo_library_index)}
    <div class="status-item"><span>Project365 entries</span><strong>${db.project365_entry_count || 0}</strong></div>
    <div class="status-item"><span>Canonical entries</span><strong>${formatCanonicalEntries(db)}</strong></div>
    <div class="status-item"><span>Project365 date range</span><strong>${db.project365_date_start || ""} to ${db.project365_date_end || ""}</strong></div>
    <div class="status-item"><span>Indexed photos</span><strong>${photoIndex.file_count || 0} files · ${photoIndex.date_count || 0} dates</strong></div>
    <div class="status-item"><span>Original queue rows</span><strong>${queue.rows || 0}</strong></div>
    <div class="status-item"><span>Queue decisions</span><strong>${escapeHtml(JSON.stringify(queue.decisions || {}))}</strong></div>
    <div class="status-item"><span>Pending picker decisions</span><strong>${formatPendingApply(queue.pending_apply || {})}</strong></div>
    <div class="status-item"><span>Remainder overview</span><strong>${formatRemainderOverview(remainder)}</strong></div>
    <div class="status-item"><span>Original search batches</span><strong>${batchPlan.rows || 0}</strong></div>
    <div class="status-item"><span>Hidden rejected candidates</span><strong>${batchPlan.hidden_rejected_candidate_count || 0}</strong></div>
    <div class="status-item"><span>Hidden export copies</span><strong>${batchPlan.hidden_export_equivalent_candidate_count || 0}</strong></div>
    <div class="status-item"><span>Next search batch</span><strong>${formatBatch(nextBatch)}</strong></div>
    <div class="status-item"><span>Search attempts</span><strong>${attempts.rows || 0}</strong></div>
    <div class="status-item"><span>Latest search attempt</span><strong>${formatSearchAttempt(latestAttempt)}</strong></div>
    <div class="status-item"><span>Latest Diarium package</span><strong>${formatDiariumPackage(packageStatus)}</strong></div>
    ${formatDiariumImportInstruction(packageStatus)}
    <div class="status-item"><span>Package photo readiness</span><strong>${formatDiariumPackageReadiness(packageStatus)}</strong></div>
    <div class="status-item"><span>Package manifest</span><strong>${packageStatus.manifest_rows || 0} rows · ${packageStatus.manifest_photo_rows || 0} photo rows</strong></div>
    <div class="status-item"><span>Diarium local diary</span><strong>${formatDiariumLocal(diariumLocal)}</strong></div>
    <div class="status-item"><span>Diarium import check</span><strong>${formatDiariumImportCheck(packageStatus, diariumLocal)}</strong></div>
    <div class="status-item"><span>Diarium photo import</span><strong class="${diariumVerification.photo_imported ? "ok" : "bad"}">${formatDiariumImportVerification(diariumVerification)}</strong></div>
    ${formatDiariumAttachmentNote(diariumVerification)}
  `;
}

function renderWorkingCopyReadiness(payload) {
  const target = document.getElementById("workingCopyReadiness");
  if (!target) return;
  const db = payload.database || {};
  const ready = Number(db.working_copy_ready_count || 0);
  const sources = Number(db.working_copy_source_count || 0);
  const notReady = Number(db.working_copy_not_ready_count || 0);
  const current = Number(db.working_copy_current_count || 0);
  const needsUpdate = Number(db.working_copy_needs_update_count || 0);
  target.innerHTML = `
    <div class="result-metric"><span>Ready for export</span><strong>${ready}</strong></div>
    <div class="result-metric"><span>Source chosen</span><strong>${sources}</strong></div>
    <div class="result-metric"><span>Already current</span><strong>${current}</strong></div>
    <div class="result-metric"><span>Need update</span><strong>${needsUpdate}</strong></div>
    <div class="result-metric"><span>Not ready</span><strong>${notReady}</strong></div>
  `;
}

function workingCopyDateScope() {
  return {
    startDate: document.getElementById("workingCopyStartDate")?.value.trim() || "",
    endDate: document.getElementById("workingCopyEndDate")?.value.trim() || ""
  };
}

async function refreshWorkingCopyReadiness() {
  const scope = workingCopyDateScope();
  const params = new URLSearchParams();
  if (scope.startDate) params.set("start_date", scope.startDate);
  if (scope.endDate) params.set("end_date", scope.endDate);
  try {
    const payload = await fetchJson(`/api/working-copy-readiness?${params.toString()}`);
    renderWorkingCopyReadiness(payload);
  } catch (error) {
    setWorkingCopyMessage(`Could not refresh working-copy status: ${error.message}`, true);
  }
}

function renderCropConfirmationBox(payload) {
  const target = document.getElementById("cropConfirmationOverview");
  if (!target) return;
  const crop = payload.crop_confirmation || {};
  if (crop.error) {
    target.innerHTML = `<div class="result-metric"><span>Status</span><strong class="bad">${escapeHtml(crop.error)}</strong></div>`;
    return;
  }
  target.innerHTML = `
    <div class="result-metric"><span>With crop information</span><strong>${crop.with_crop_count || 0}</strong></div>
    <div class="result-metric"><span>Saved estimates</span><strong>${crop.estimated_crop_count || 0}</strong></div>
    <div class="result-metric"><span>User saved</span><strong>${crop.confirmed_crop_count || 0}</strong></div>
    <div class="result-metric"><span>Queued without crop</span><strong>${crop.queued_count || 0}</strong></div>
  `;
}

function attachCropEstimateBatchJob(payload) {
  const job = (payload.crop_confirmation || {}).crop_estimate_batch || {};
  if (!job.id) return;
  renderCropEstimateBatchJob(job);
  if (job.status === "queued" || job.status === "running") {
    const button = document.getElementById("estimateCropBatchButton");
    if (button) button.dataset.jobId = job.id;
  }
}

function renderTopMetrics(payload) {
  const metrics = payload.top_metrics || {};
  const project365Total = Number(metrics.project365_entries || 0);
  setText("metricUniqueDays", metrics.unique_diary_days || 0);
  setText("metricDiaryEntries", metrics.diary_entries || 0);
  setText(
    "metricMissingPhotos",
    formatCountPercent(metrics.missing_photos || 0, project365Total)
  );
  setText("metricAssociatedPhotos", metrics.associated_photos || 0);
  if (Object.prototype.hasOwnProperty.call(metrics, "photo_index_files")) {
    setText("metricPhotoIndexFiles", metrics.photo_index_files || 0);
  }
}

function formatCountPercent(count, total) {
  const value = Number(count || 0);
  const denominator = Number(total || 0);
  const percent = denominator > 0 ? ((value / denominator) * 100).toFixed(1) : "0.0";
  return `${value} · ${percent}%`;
}

function renderWorkflowSummaries(payload) {
  const history = payload.workflow_history || {};
  const activeJobs = payload.active_jobs || [];
  const cancellableJob = activeJobs.find(job => job.job_id && ["queued", "running"].includes(job.status || "running"));
  if (cancellableJob) activeJobId = cancellableJob.job_id;
  for (const card of document.querySelectorAll(".workflow-step[data-step]")) {
    const owner = card.dataset.step;
    const active = activeJobs.find(job => ownerStep(job.step) === owner);
    const records = workflowRecordsFor(owner, history);
    renderWorkflowCard(card, owner, active, records);
  }
}

function applyInitialWorkflowExpansion() {
  for (const card of document.querySelectorAll(".workflow-step[data-step]")) {
    const owner = card.dataset.step;
    const expanded = manuallyExpandedSteps.has(owner);
    card.classList.toggle("is-expanded", expanded);
    const toggle = card.querySelector(`[data-step-toggle="${owner}"]`);
    if (toggle) {
      toggle.textContent = expanded ? "Collapse" : "Open";
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    }
  }
}

function renderWorkflowCard(card, owner, active, records) {
  const latest = active || records[0] || null;
  const expanded = Boolean(active) || manuallyExpandedSteps.has(owner);
  card.classList.toggle("is-running", Boolean(active));
  card.classList.toggle("is-expanded", expanded);
  const statusTarget = card.querySelector(`[data-step-status="${owner}"]`);
  if (statusTarget) {
    if (owner === "crop_confirmation" && !active) {
      statusTarget.innerHTML = formatCropConfirmationStatus((lastStatus || {}).crop_confirmation || {});
    } else if (owner === "diary_enrichment" && !active) {
      statusTarget.innerHTML = `<span class="status-pill">Interactive</span>Set date range`;
    } else {
      statusTarget.innerHTML = latest ? formatWorkflowStatus(latest, Boolean(active)) : "No runs yet";
    }
  }
  const toggle = card.querySelector(`[data-step-toggle="${owner}"]`);
  if (toggle) {
    toggle.textContent = expanded ? "Collapse" : "Open";
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  }
  const resultTarget = card.querySelector(`[data-step-result="${owner}"]`);
  if (resultTarget) resultTarget.innerHTML = latest ? formatWorkflowResult(latest, Boolean(active)) : "";
  const historyTarget = card.querySelector(`[data-step-history="${owner}"]`);
  if (historyTarget) historyTarget.innerHTML = records.length ? formatWorkflowHistory(records) : "";
}

function formatCropConfirmationStatus(crop) {
  if (crop.error) return `<span class="status-pill fail">Needs attention</span>${escapeHtml(crop.error)}`;
  return `<span class="status-pill">Crop review</span>${crop.queued_count || 0} without crop · ${crop.estimated_crop_count || 0} saved estimates · ${crop.confirmed_crop_count || 0} user saved`;
}

function openCropConfirmation() {
  const job = ((lastStatus || {}).crop_confirmation || {}).crop_estimate_batch || {};
  const url = new URL("/crop", window.location.href);
  if (job.apply_estimates && Number(job.estimated_count || 0) > 0 && ["queued", "running"].includes(job.status || "")) {
    url.searchParams.set("crop_filter", "estimated");
  }
  window.location.assign(url.toString());
}

function setDiaryEnrichmentMessage(message, isError = false) {
  const target = document.getElementById("diaryEnrichmentMessage");
  if (!target) return;
  target.textContent = message;
  target.classList.toggle("error", Boolean(isError));
}

function setWorkingCopyMessage(message, isError = false) {
  const target = document.getElementById("workingCopyMessage");
  if (!target) return;
  target.textContent = message;
  target.classList.toggle("error", Boolean(isError));
}

function validIsoDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}

function openDiaryEnrichment() {
  const startDate = document.getElementById("diaryEnrichmentStartDate").value.trim();
  const endDate = document.getElementById("diaryEnrichmentEndDate").value.trim();
  const limit = Number(document.getElementById("diaryEnrichmentLimit").value || "200");
  if (!validIsoDate(startDate) || !validIsoDate(endDate)) {
    setDiaryEnrichmentMessage("Enter both start and end dates as YYYY-MM-DD.", true);
    return;
  }
  if (startDate > endDate) {
    setDiaryEnrichmentMessage("Start date must be before or equal to end date.", true);
    return;
  }
  const boundedLimit = Math.max(1, Math.min(Math.round(Number.isFinite(limit) ? limit : 200), 1000));
  const url = new URL("/enrich", window.location.href);
  url.searchParams.set("start_date", startDate);
  url.searchParams.set("end_date", endDate);
  url.searchParams.set("limit", String(boundedLimit));
  window.location.assign(url.toString());
}

async function runWorkingCopies() {
  const startDate = document.getElementById("workingCopyStartDate").value.trim();
  const endDate = document.getElementById("workingCopyEndDate").value.trim();
  if (!validIsoDate(startDate) || !validIsoDate(endDate)) {
    setWorkingCopyMessage("Enter both start and end dates as YYYY-MM-DD.", true);
    return;
  }
  if (startDate > endDate) {
    setWorkingCopyMessage("Start date must be before or equal to end date.", true);
    return;
  }
  setWorkingCopyMessage(
    document.getElementById("forceWorkingCopies").checked
      ? "Starting forced rewrite for selected dates..."
      : "Starting selected dates; unchanged copies will be skipped."
  );
  await runStep("generate_derivatives", {
    start_date: startDate,
    end_date: endDate,
    force: Boolean(document.getElementById("forceWorkingCopies").checked)
  });
  await refreshWorkingCopyReadiness();
}

async function startCropEstimateBatch() {
  const button = document.getElementById("estimateCropBatchButton");
  const applyEstimates = Boolean(document.getElementById("applyCropEstimates")?.checked);
  if (!button || button.dataset.jobId) return;
  button.disabled = true;
  markButtonRunning(button, true);
  setCropEstimateBatchStatus(applyEstimates ? "Starting batch estimate and saving estimates." : "Starting batch estimate preview.");
  try {
    const job = await fetchJson("/api/crop-estimate-batch", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({apply_estimates: applyEstimates})
    });
    renderCropEstimateBatchJob(job);
    if (job.status === "queued" || job.status === "running") {
      button.dataset.jobId = job.id || "";
      pollCropEstimateBatch(job.id);
    } else {
      button.dataset.jobId = "";
      markButtonRunning(button, false);
      button.disabled = false;
      await loadStatus();
    }
  } catch (error) {
    button.dataset.jobId = "";
    markButtonRunning(button, false);
    button.disabled = false;
    setCropEstimateBatchStatus(error.message);
  }
}

async function pollCropEstimateBatch(jobId) {
  const button = document.getElementById("estimateCropBatchButton");
  try {
    const job = await fetchJson(`/api/crop-estimate-batch/${encodeURIComponent(jobId)}`);
    renderCropEstimateBatchJob(job);
    if (job.status === "queued" || job.status === "running") {
      setTimeout(() => pollCropEstimateBatch(jobId), 1200);
      return;
    }
    if (button) {
      button.dataset.jobId = "";
      markButtonRunning(button, false);
      button.disabled = false;
    }
    await loadStatus();
  } catch (error) {
    if (button) {
      button.dataset.jobId = "";
      markButtonRunning(button, false);
      button.disabled = false;
    }
    setCropEstimateBatchStatus(error.message);
  }
}

function setCropEstimateBatchStatus(message) {
  const target = document.getElementById("cropEstimateBatchStatus");
  if (target) target.textContent = message;
}

function renderCropEstimateBatchJob(job) {
  const button = document.getElementById("estimateCropBatchButton");
  const runningNow = job.status === "queued" || job.status === "running";
  if (button) {
    button.disabled = runningNow;
    markButtonRunning(button, runningNow);
  }
  if (job.message) {
    setCropEstimateBatchStatus(job.message);
    return;
  }
  const processed = Number(job.processed_count || 0);
  const total = Number(job.target_count || 0);
  if (job.status === "pass" && total <= 0) {
    setCropEstimateBatchStatus("No missing crop estimates to run.");
    return;
  }
  const estimated = Number(job.estimated_count || 0);
  const skipped = Number(job.skipped_count || 0);
  const failed = Number(job.failed_count || 0);
  const current = job.current_entry_id ? ` · ${job.current_entry_id}` : "";
  const mode = job.apply_estimates ? "saved" : "previewed";
  const firstError = Array.isArray(job.errors) && job.errors.length ? job.errors[0] : null;
  const errorDetail = firstError
    ? ` · First error: ${firstError.entry_id || "item"} ${firstError.error || "estimate failed"}`
    : "";
  const label = runningNow ? "Working" : job.status === "pass" ? "Complete" : "Failed";
  setCropEstimateBatchStatus(
    `${label} · ${processed}/${total} checked · ${estimated} ${mode} estimates · ${skipped} skipped · ${failed} failed${current}${errorDetail}`
  );
}

function toggleWorkflowStep(step) {
  if (manuallyExpandedSteps.has(step)) {
    manuallyExpandedSteps.delete(step);
  } else {
    manuallyExpandedSteps.add(step);
  }
  const card = workflowCard(step);
  if (!card) return;
  const active = ((lastStatus || {}).active_jobs || []).find(job => ownerStep(job.step) === step);
  renderWorkflowCard(card, step, active, workflowRecordsFor(step, (lastStatus || {}).workflow_history || {}));
  if (manuallyExpandedSteps.has(step)) {
    loadStatus(expandedStatusSteps()).catch(function(error) {
      document.getElementById("output").textContent = error.message;
    });
  }
}

function workflowCard(step) {
  for (const card of document.querySelectorAll(".workflow-step[data-step]")) {
    if (card.dataset.step === step) return card;
  }
  return null;
}

function workflowRecordsFor(owner, history) {
  const records = [];
  for (const [step, stepRecords] of Object.entries(history || {})) {
    if (ownerStep(step) !== owner) continue;
    records.push(...(stepRecords || []));
  }
  return records
    .sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")))
    .slice(0, 5);
}

function ownerStep(step) {
  return STEP_OWNER[step] || step || "";
}

function formatWorkflowStatus(record, active) {
  const status = record.status || "running";
  const heartbeat = active ? `<span class="heartbeat" aria-hidden="true"></span>` : "";
  const label = formatJobStatus(status);
  const time = active
    ? formatElapsed(Date.now() - Date.parse(record.started_at || new Date().toISOString()))
    : formatShortDateTime(record.finished_at || record.started_at);
  return `${heartbeat}<span class="status-pill ${escapeHtml(status)}">${escapeHtml(label)}</span><span>${escapeHtml(time)}</span>`;
}

function formatWorkflowResult(record, active) {
  if (active) return formatActiveWorkflowResult(record);
  const summary = record.summary || {};
  const metrics = workflowHistoryMetrics(record).map(metric => `
    <div class="result-metric"><span>${escapeHtml(metric.label)}</span><strong>${escapeHtml(metric.value)}</strong></div>
  `).join("");
  const scope = (summary.scope || []).length
    ? `<div class="subtle">${summary.scope.map(escapeHtml).join(" · ")}</div>`
    : "";
  const showArchives = ownerStep(record.step || "") === "import_zips";
  const archives = showArchives
    ? formatArchiveResults(
        summary.archive_results || [],
        summary.archive_result_hidden_count || 0,
        summary.archive_result_count || 0,
        summary.archive_anomaly_count || 0,
        summary.archive_changed_count || 0
      )
    : "";
  const zeroMessage = summary.zero_change_message || (showArchives ? "Known ZIP inputs were unchanged or skipped." : "");
  const zero = summary.zero_change && zeroMessage ? `<div class="status-note"><strong>No changes.</strong> ${escapeHtml(zeroMessage)}</div>` : "";
  const warnings = (summary.warnings || []).map(warning => `
    <div class="status-note bad"><strong>Warning:</strong> ${escapeHtml(warning)}</div>
  `).join("");
  const error = summary.error ? `<div class="status-note bad"><strong>Error:</strong> ${escapeHtml(summary.error)}</div>` : "";
  return `
    ${scope}
    <div class="result-grid">${metrics}</div>
    ${archives}
    ${zero}
    ${warnings}
    ${error}
  `;
}

function formatActiveWorkflowResult(job) {
  const startedAt = Date.parse(job.started_at || new Date().toISOString());
  const elapsed = formatElapsed(Date.now() - startedAt);
  const canCancel = job.cancellable !== false;
  const jobId = canCancel ? (job.job_id || activeJobId) : "";
  const canCancelStart = !jobId && running && (job.status === "starting" || job.status === "running");
  const actionLabel = jobId ? "Stop" : "Cancel";
  const status = job.status || "running";
  const liveLabel = ["queued", "running", "starting", "cancelling"].includes(status) ? "Working" : formatJobStatus(status);
  const progressPercent = jobProgressPercent(job);
  const progressStyle = progressPercent === null
    ? ""
    : ` style="width: ${progressPercent.toFixed(1)}%; animation: none; transform: none;"`;
  return `
    <div class="run-progress status-${escapeHtml(status)}">
      <div class="run-progress-header">
        <div class="run-progress-main">
          <span class="spinner"></span>
          <strong>${escapeHtml(formatStepName(job.step || progressStep))}</strong>
        </div>
        <div class="run-progress-actions">
          <span class="subtle">${escapeHtml(elapsed)}</span>
          <button class="button danger small" onclick="cancelActiveJob()" ${jobId || canCancelStart ? "" : "hidden"}>${escapeHtml(actionLabel)}</button>
        </div>
      </div>
      <div class="progress-track"><div class="progress-bar"${progressStyle}></div></div>
      <div class="subtle run-progress-detail">
        <span class="run-progress-text">${escapeHtml(progressDetail(job))}</span>
        <span class="run-progress-live">${escapeHtml(liveLabel)}</span>
      </div>
    </div>
  `;
}

function jobProgressPercent(job) {
  const step = job.step || progressStep;
  if (job.kind === "crop_estimate_batch") {
    return percentComplete(job.processed_count, job.target_count);
  }
  if (step === "broad_visual_index") {
    const run = job.broad_visual_index_run || {};
    return percentComplete(run.scanned_count, run.total_candidate_count);
  }
  if (step === "broad_visual_match") {
    const run = job.broad_visual_run || {};
    return percentComplete(run.processed_target_count, run.target_count);
  }
  if (step === "rough_prefilter_build") {
    const run = job.rough_prefilter_run || {};
    return percentComplete(run.scanned_count, run.total_descriptor_count);
  }
  if (step === "rough_visual_match") {
    const run = job.rough_visual_run || {};
    return percentComplete(run.processed_target_count, run.target_count);
  }
  return null;
}

function percentComplete(done, total) {
  const totalCount = Number(total || 0);
  if (!totalCount) return null;
  const doneCount = Math.max(0, Math.min(Number(done || 0), totalCount));
  return Math.max(0, Math.min(100, (doneCount / totalCount) * 100));
}

function formatArchiveResults(archives, hiddenCount = 0, totalCount = 0, anomalyCount = 0, changedCount = 0) {
  if (!archives.length) {
    if (!totalCount) return "";
    const changedNote = changedCount
      ? ` ${changedCount} archives were new or changed.`
      : " No archive-level changes required attention.";
    return `<div class="status-note"><strong>No archive anomalies.</strong> ${totalCount} archives checked.${changedNote}</div>`;
  }
  const visibleArchives = archives.slice(0, 25);
  const effectiveHiddenCount = hiddenCount || Math.max(0, anomalyCount - visibleArchives.length);
  const hiddenNote = effectiveHiddenCount
    ? `<div class="subtle">${effectiveHiddenCount} additional archive anomalies hidden.</div>`
    : "";
  const rows = visibleArchives.map(archive => `
    <tr>
      <td>${escapeHtml(archive.filename || "")}</td>
      <td>${escapeHtml(archive.status || "")}</td>
      <td>${escapeHtml(archive.month || "")}</td>
      <td>${escapeHtml(archive.validation_status || "")}</td>
    </tr>
  `).join("");
  return `
    <div class="subtle">Archive anomalies: ${anomalyCount || visibleArchives.length} of ${totalCount || visibleArchives.length} checked.</div>
    ${hiddenNote}
    <table class="archive-table">
      <thead><tr><th>Archive</th><th>Result</th><th>Month</th><th>Validation</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function formatWorkflowHistory(records) {
  const rows = records.map(record => {
    const metrics = workflowHistoryMetrics(record).slice(0, 2)
      .map(metric => `${metric.label}: ${metric.value}`)
      .join(" · ");
    return `<div><span>${escapeHtml(formatShortDateTime(record.finished_at || record.started_at))}</span><strong>${escapeHtml(formatJobStatus(record.status || ""))}${metrics ? " · " + escapeHtml(metrics) : ""}</strong></div>`;
  }).join("");
  return `<div class="run-list">${rows}</div>`;
}

function workflowHistoryMetrics(record) {
  const metrics = ((record.summary || {}).metrics || []);
  const step = record.step || "";
  if (step === "broad_visual_match") {
    return metrics
      .filter(metric => [
        "Entries searched",
        "Candidate comparisons",
        "Matched entries",
        "Saved candidate rows",
        "Search errors",
        "Unresolved candidates saved"
      ].includes(metric.label))
      .map(metric => metric.label === "Unresolved candidates saved"
        ? { label: "Saved candidate rows", value: metric.value }
        : metric
      );
  }
  if (step === "broad_visual_index") {
    return metrics.filter(metric => [
      "Stored fingerprints",
      "New/rebuilt fingerprints",
      "Reused fingerprints",
      "Fingerprint errors"
    ].includes(metric.label));
  }
  if (step === "broad_visual_benchmark") {
    return metrics.filter(metric => [
      "Accuracy targets tested",
      "Top-1 hits",
      "Top-5 hits",
      "Top-N hits",
      "Benchmark errors"
    ].includes(metric.label));
  }
  if (step === "rough_prefilter_build") {
    return metrics.filter(metric => [
      "Stored prefilter rows",
      "Stale/missing rows",
      "New/rebuilt rows",
      "Reused rows",
      "Prefilter errors"
    ].includes(metric.label));
  }
  if (step === "rough_visual_match") {
    return metrics.filter(metric => [
      "Entries searched",
      "Shortlisted candidates",
      "Dense descriptor loads",
      "Matched entries",
      "Saved candidate rows",
      "Capped band hits",
      "Search errors"
    ].includes(metric.label));
  }
  if (step === "rough_visual_benchmark") {
    return metrics.filter(metric => [
      "Accuracy targets tested",
      "Prefilter recall @1000",
      "Top-1 hits",
      "Top-N hits",
      "Benchmark errors"
    ].includes(metric.label));
  }
  return metrics;
}

function setText(id, value) {
  const target = document.getElementById(id);
  if (target) target.textContent = value;
}

function renderPhotoIndexBox(payload) {
  const target = document.getElementById("photoIndexBox");
  if (!target) return;
  const photoIndex = payload.photo_library_index || {};
  const runs = photoIndex.recent_runs || [];
  const rootCount = (photoIndex.roots || []).length;
  const runRows = runs.length
    ? runs.map(run => {
        const rootLabel = splitAttemptList(run.roots || "").map(path => path.split("/").filter(Boolean).pop() || path).join("; ");
        return `<div><span>Index run · ${escapeHtml(formatShortDateTime(run.finished_at))}</span><strong>${run.indexed_file_count || 0} indexed · ${run.new_file_count || 0} new · ${run.file_count_after || 0} total · ${escapeHtml(rootLabel)}</strong></div>`;
      }).join("")
    : `<div><span>Recent index runs</span><strong>none recorded yet</strong></div>`;
  target.innerHTML = `
    <div><span>Indexed photos</span><strong>${photoIndex.file_count || 0}</strong></div>
    <div><span>Capture times</span><strong>${photoIndex.capture_timestamp_count || 0}</strong></div>
    <div><span>GPS coordinates</span><strong>${photoIndex.gps_coordinate_count || 0}</strong></div>
    <div><span>Indexed dates</span><strong>${photoIndex.date_count || 0}</strong></div>
    <div><span>Indexed roots</span><strong>${rootCount}</strong></div>
    ${runRows}
  `;
}

function renderEasyMatchBox(payload) {
  const summary = payload.original_remainder_overview || {};
  const reviewCount = Number(summary.review_ready_entry_count || 0);
  const folderCount = Number(summary.folder_needed_date_count || 0);
  const reviewButton = document.getElementById("reviewEasyMatchesButton");
  if (reviewButton) reviewButton.textContent = `Review easy matches (${reviewCount})`;
  if (!summary.exists) {
    setStepMessage("match_easy_originals", "No match results yet. Run Find easy matches.");
  } else if (reviewCount > 0) {
    const remaining = folderCount ? ` ${folderCount} entries still need another folder.` : "";
    setStepMessage(
      "match_easy_originals",
      `${reviewCount} entries ready for review.${remaining} Next: review easy matches.`
    );
  } else if (folderCount > 0) {
    setStepMessage(
      "match_easy_originals",
      `No easy matches are ready. ${folderCount} entries need another folder.`
    );
  } else {
    setStepMessage("match_easy_originals", "No easy matches are ready for review.");
  }
}

function renderBroadVisualBox(payload) {
  const target = document.getElementById("broadVisualBox");
  if (!target) return;
  const broad = payload.broad_visual_match || {};
  const latestRun = broad.review_ready_run?.run_id ? broad.review_ready_run : (broad.latest_run || {});
  const latestIndex = broad.latest_index_run || {};
  const latestBenchmark = broad.latest_benchmark || {};
  const active = (payload.active_jobs || []).find(job => ownerStep(job.step) === "broad_visual_match");
  const activeIndex = active && active.step === "broad_visual_index";
  const activeSearch = active && active.step === "broad_visual_match";
  updateBroadReviewButton(latestRun, broad.review_ready_run || {});
  const skippedIndex = Math.max(
    0,
    latestIndex.skipped_candidate_count === undefined || latestIndex.skipped_candidate_count === null
      ? Number(latestIndex.scanned_count || 0)
        - Number(latestIndex.reused_descriptor_count || 0)
        - Number(latestIndex.indexed_descriptor_count || 0)
        - Number(latestIndex.error_count || 0)
      : Number(latestIndex.skipped_candidate_count || 0)
  );
  const totalIndexCandidates = Number(latestIndex.total_candidate_count || 0);
  target.innerHTML = `
    <div><span>Broad match database</span><strong>${broad.exists ? "ready" : "not created"} · ${escapeHtml(broad.path || "")}</strong></div>
    <div><span>Stored candidate fingerprints</span><strong>${broad.descriptor_count || 0}</strong></div>
    <div><span>Active job</span><strong>${active ? escapeHtml(`${formatStepName(active.step || "")} · ${active.status || "running"}`) : "none"}</strong></div>
    <div><span>Latest fingerprint build</span><strong>${escapeHtml(formatBroadIndexSummary(latestIndex, skippedIndex))}</strong></div>
    <div><span>Fingerprints checked</span><strong>${latestIndex.scanned_count || 0}${totalIndexCandidates ? ` / ${totalIndexCandidates}` : ""}</strong></div>
    <div><span>New/rebuilt fingerprints</span><strong>${latestIndex.indexed_descriptor_count || 0}</strong></div>
    <div><span>Already reused</span><strong>${latestIndex.reused_descriptor_count || 0}</strong></div>
    <div><span>Not fingerprinted</span><strong>${skippedIndex}</strong></div>
    <div><span>Speed</span><strong>${escapeHtml(formatBroadIndexThroughput(latestIndex, Boolean(activeIndex)))}</strong></div>
    <div><span>Current item</span><strong>${escapeHtml(formatBroadCurrentItem(latestIndex))}</strong></div>
    <div><span>Date coverage</span><strong>${escapeHtml(formatBroadDateCoverage(latestIndex))}</strong></div>
    <div><span>Latest fingerprint errors</span><strong>${escapeHtml(formatBroadIndexErrors(broad.latest_index_errors || [], Number(latestIndex.error_count || 0)))}</strong></div>
    <div><span>Accuracy benchmark (diagnostic)</span><strong>${escapeHtml(formatBroadBenchmarkSummary(latestBenchmark))}</strong></div>
    <div><span>Current unresolved search</span><strong>${escapeHtml(formatBroadSearchSummary(latestRun, Boolean(activeSearch)))}</strong></div>
    <div><span>Review readiness</span><strong>${escapeHtml(formatBroadReviewReadiness(latestRun, broad.review_ready_run || {}))}</strong></div>
    <div><span>Search heartbeat</span><strong>${escapeHtml(formatBroadSearchHeartbeat(latestRun, Boolean(activeSearch)))}</strong></div>
    <div><span>Saved unresolved candidate rows</span><strong>${latestRun.result_count || 0}</strong></div>
    <div><span>Errors</span><strong>${Number(latestIndex.error_count || 0) + Number(latestRun.error_count || 0) + Number(latestBenchmark.error_count || 0)}</strong></div>
    ${renderBroadMonthlyCoverage(broad.monthly_coverage || [])}
  `;
  if (!activeSearch && latestRun.run_id) {
    setStepMessage("broad_visual_match", broadVisualRunCompleteMessage(latestRun, broad.review_ready_run || {}));
  }
}

function renderRoughVisualBox(payload) {
  const target = document.getElementById("roughVisualBox");
  if (!target) return;
  const broad = payload.broad_visual_match || {};
  const prefilter = broad.rough_prefilter || {};
  const latestBuild = prefilter.latest_run || {};
  const latestRun = broad.rough_review_ready_run?.run_id ? broad.rough_review_ready_run : (broad.latest_no_date_run || {});
  const metrics = latestRun.prefilter_metrics || {};
  const active = (payload.active_jobs || []).find(job => ownerStep(job.step) === "rough_visual_match");
  const activeBuild = active && active.step === "rough_prefilter_build";
  const activeSearch = active && active.step === "rough_visual_match";
  const reviewButton = document.getElementById("roughReviewResultsButton");
  if (reviewButton) {
    const count = Number(latestRun.review_ready_entry_count || 0);
    reviewButton.textContent = count ? `Review no-date results (${count})` : "Review no-date results (0)";
  }
  target.innerHTML = `
    <div><span>Rough prefilter</span><strong>${prefilter.ready ? "ready" : "not ready"} · ${prefilter.feature_count || 0} rows · ${prefilter.band_count || 0} bands</strong></div>
    <div><span>Source fingerprints</span><strong>${prefilter.descriptor_count || 0}</strong></div>
    <div><span>Stale/missing prefilter rows</span><strong>${prefilter.stale_count || 0}</strong></div>
    <div><span>Latest prefilter build</span><strong>${escapeHtml(formatRoughBuildSummary(latestBuild, Boolean(activeBuild)))}</strong></div>
    <div><span>Latest prefilter errors</span><strong>${escapeHtml(formatBroadIndexErrors(prefilter.latest_errors || [], Number(prefilter.error_count || 0)))}</strong></div>
    <div><span>Active job</span><strong>${active ? escapeHtml(`${formatStepName(active.step || "")} · ${active.status || "running"}`) : "none"}</strong></div>
    <div><span>Current no-date search</span><strong>${escapeHtml(formatBroadSearchSummary(latestRun, Boolean(activeSearch)))}</strong></div>
    <div><span>No-date search heartbeat</span><strong>${escapeHtml(formatBroadSearchHeartbeat(latestRun, Boolean(activeSearch)))}</strong></div>
    <div><span>Shortlisted candidates</span><strong>${metrics.shortlist_size || 0}</strong></div>
    <div><span>Dense descriptor loads</span><strong>${latestRun.scanned_count || 0}</strong></div>
    <div><span>Capped band hits</span><strong>${metrics.capped_band_count || 0}</strong></div>
    <div><span>Prefilter timing</span><strong>${formatElapsed(Number(metrics.elapsed_ms || 0))}</strong></div>
  `;
  if (!activeSearch && latestRun.run_id) {
    setStepMessage("rough_visual_match", roughVisualRunCompleteMessage(latestRun));
  }
}

function formatRoughBuildSummary(latestBuild, active) {
  if (!latestBuild || !latestBuild.run_id) return "none yet";
  const total = Number(latestBuild.total_descriptor_count || 0);
  const scanned = Number(latestBuild.scanned_count || 0);
  const progress = total ? `${scanned}/${total}` : `${scanned}`;
  const state = active ? "running" : (latestBuild.status || "finished");
  return `${state} · ${progress} fingerprints checked · ${latestBuild.indexed_feature_count || 0} built · ${latestBuild.reused_feature_count || 0} reused`;
}

function roughVisualRunCompleteMessage(latestRun) {
  if (!Number(latestRun.target_count || 0)) {
    return "No-date visual search found no entries in the selected scope. Nothing was searched.";
  }
  const metrics = latestRun.prefilter_metrics || {};
  return `No-date visual match complete: ${latestRun.processed_target_count || 0}/${latestRun.target_count || 0} entries, ${metrics.shortlist_size || 0} shortlisted, ${latestRun.scanned_count || 0} dense descriptor loads, ${latestRun.result_count || 0} saved rows.`;
}

function updateBroadReviewButton(latestRun, reviewReadyRun = {}) {
  const button = document.getElementById("broadReviewResultsButton");
  if (!button) return;
  const unresolvedReadyCount = Number(reviewReadyRun.review_ready_entry_count || 0);
  const latestSearchRunId = latestRun.run_id || reviewReadyRun.run_id || "";
  button.textContent = latestSearchRunId
    ? `3. Review unresolved results (${unresolvedReadyCount} ready)`
    : "3. Review unresolved results (none)";
  button.title = latestSearchRunId
    ? unresolvedReadyCount
      ? `Open ${unresolvedReadyCount} ready unresolved Broad Visual result${unresolvedReadyCount === 1 ? "" : "s"}.`
      : `No unresolved entries are currently review-ready. ${formatBroadReviewReadiness(latestRun, reviewReadyRun)}.`
    : "No unresolved Broad Visual results are ready yet.";
  button.disabled = Boolean(latestSearchRunId) && unresolvedReadyCount <= 0;
  button.dataset.reviewMode = "search";
  button.dataset.reviewSet = "broad";
  button.dataset.unresolvedReadyCount = String(unresolvedReadyCount);
}

function broadVisualRunCompleteMessage(latestRun, reviewReadyRun = {}) {
  if (!Number(latestRun.target_count || 0)) {
    return "Broad visual search found no entries in the selected scope. Nothing was searched.";
  }
  const readiness = formatBroadReviewReadiness(latestRun, reviewReadyRun);
  const readinessNote = readiness && readiness !== "none ready" ? ` Review readiness: ${readiness}.` : "";
  return `Broad visual match complete: ${latestRun.processed_target_count || 0}/${latestRun.target_count || 0} entries, ${latestRun.scanned_count || 0} candidate comparisons, ${latestRun.result_count || 0} saved rows.${readinessNote}`;
}

function formatBroadReviewReadiness(latestRun, reviewReadyRun = {}) {
  const ready = Number(reviewReadyRun.review_ready_entry_count || 0);
  if (ready > 0) return `${ready} entries ready for Broad review`;
  const savedEntries = Number(reviewReadyRun.review_total_entry_count || latestRun.matched_entries || 0);
  if (!savedEntries) return "none ready";
  const decided = Number(reviewReadyRun.review_decision_entry_count || 0);
  const confirmed = Number(reviewReadyRun.review_confirmed_entry_count || 0);
  const filtered = Number(reviewReadyRun.review_filtered_entry_count || 0);
  const parts = [];
  if (decided) parts.push(`${decided} already decided`);
  if (confirmed) parts.push(`${confirmed} already confirmed`);
  if (filtered) parts.push(`${filtered} filtered out`);
  if (!parts.length) return `${savedEntries} saved entries, 0 ready`;
  return `0 ready · ${parts.join(" · ")}`;
}

function renderBroadMonthlyCoverage(rows) {
  const header = `
    <div class="coverage-table-head">
      <h3>Monthly fingerprint coverage</h3>
      <button class="button primary small" title="Check selected date-window fingerprint coverage before starting a search." onclick="checkBroadVisualCoverage()">Check coverage</button>
    </div>
    <div id="broadCoverageCheckStatus" class="coverage-check-status" aria-live="polite"></div>
  `;
  if (!rows.length) {
    return `
      <div class="coverage-table-wrap">
        ${header}
        <div><span>Status</span><strong>none yet</strong></div>
      </div>
    `;
  }
  const body = rows.map(row => {
    const photoCount = Number(row.photo_index_count || 0);
    const fingerprintCount = Number(row.fingerprint_count || 0);
    const coverage = photoCount ? fingerprintCount / photoCount : 0;
    const low = photoCount >= 20 && coverage < 0.8;
    return `
      <tr class="${low ? "low-coverage" : ""}">
        <td>${escapeHtml(row.month || "")}</td>
        <td>${photoCount}</td>
        <td>${fingerprintCount}</td>
        <td>${formatPercent(coverage)}</td>
        <td>${Number(row.missing_original_targets || 0)}</td>
      </tr>
    `;
  }).join("");
  return `
    <div class="coverage-table-wrap">
      ${header}
      <div class="coverage-table-scroll">
        <table class="coverage-table">
          <thead>
            <tr>
              <th>Month</th>
              <th>Indexed photos</th>
              <th>Fingerprints</th>
              <th>Coverage</th>
              <th>Missing originals</th>
            </tr>
          </thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    </div>
  `;
}

function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function formatBroadRunState(active, latestRun) {
  if (active) return `${active.status || "running"} active`;
  if (!latestRun || !latestRun.run_id) return "idle";
  return latestRun.status || "completed";
}

function formatBroadIndexSummary(latestIndex, skippedIndex) {
  if (!latestIndex || !latestIndex.run_id) return "none yet";
  const scope = latestIndex.roots || "selected folders";
  const total = Number(latestIndex.total_candidate_count || 0);
  const checked = Number(latestIndex.scanned_count || 0);
  const progress = total ? `${checked}/${total}` : `${checked}`;
  return `${latestIndex.status || "finished"} · ${scope} · ${progress} files checked (${skippedIndex} skipped)`;
}

function formatBroadIndexThroughput(latestIndex, active) {
  if (!latestIndex || !latestIndex.run_id || !latestIndex.started_at) return "none yet";
  const startedAt = Date.parse(latestIndex.started_at);
  if (!Number.isFinite(startedAt)) return "unknown";
  const finishedAt = latestIndex.finished_at ? Date.parse(latestIndex.finished_at) : 0;
  const heartbeatAt = latestIndex.heartbeat_at ? Date.parse(latestIndex.heartbeat_at) : 0;
  const endAt = active ? (heartbeatAt || Date.now()) : (finishedAt || heartbeatAt || Date.now());
  const elapsedMs = Math.max(0, endAt - startedAt);
  const elapsedSeconds = Math.max(1, elapsedMs / 1000);
  const checked = Number(latestIndex.scanned_count || 0);
  const total = Number(latestIndex.total_candidate_count || 0);
  const rate = checked / elapsedSeconds;
  const pieces = [`${formatElapsed(elapsedMs)} elapsed`];
  if (checked) pieces.push(`${rate.toFixed(rate >= 10 ? 1 : 2)} files/sec`);
  if (active && total > checked && rate > 0) {
    pieces.push(`${formatElapsed(((total - checked) / rate) * 1000)} remaining`);
  }
  return pieces.join(" · ");
}

function formatBroadIndexErrors(errors, errorCount) {
  const count = Number(errorCount || 0);
  if (!count) return "none";
  const first = Array.isArray(errors) && errors.length ? errors[0] : null;
  if (!first) return `${count} errors`;
  const name = first.filename || first.path || "file";
  const phase = first.phase || "fingerprinting";
  const more = count > 1 ? `; ${count - 1} more` : "";
  return `${count} errors · ${phase} · ${name}${more}`;
}

function formatBroadCurrentItem(latestIndex) {
  if (!latestIndex || !latestIndex.current_candidate_index) return "none";
  const index = Number(latestIndex.current_candidate_index || 0);
  const total = Number(latestIndex.total_candidate_count || 0);
  const phase = latestIndex.current_phase || "working";
  const name = latestIndex.current_candidate_name || "candidate";
  const ext = latestIndex.current_candidate_extension || "";
  const size = formatBytes(Number(latestIndex.current_candidate_byte_size || 0));
  const date = latestIndex.current_candidate_date || "";
  const parts = [`${phase}`, `file ${index}${total ? `/${total}` : ""}`];
  if (date) parts.push(date);
  if (ext) parts.push(ext);
  if (size) parts.push(size);
  parts.push(name);
  return parts.join(" · ");
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "";
  if (bytes >= 1000000000) return `${(bytes / 1000000000).toFixed(1)} GB`;
  if (bytes >= 1000000) return `${(bytes / 1000000).toFixed(1)} MB`;
  if (bytes >= 1000) return `${(bytes / 1000).toFixed(1)} KB`;
  return `${bytes} B`;
}

function formatBroadDateCoverage(latestIndex) {
  if (!latestIndex || !latestIndex.date_coverage_json) return "none yet";
  const payload = parseJsonObject(latestIndex.date_coverage_json);
  const totalDates = Number(payload.total_date_count || 0);
  if (!totalDates) return "none yet";
  const completeDates = Number(payload.complete_date_count || 0);
  const completeRange = payload.complete_start_date && payload.complete_end_date
    ? `${payload.complete_start_date} to ${payload.complete_end_date}`
    : "";
  const current = payload.current_date
    ? `${payload.current_date} ${payload.current_checked || 0}/${payload.current_total || 0}`
    : "";
  if (completeRange && current) return `${completeDates}/${totalDates} dates complete (${completeRange}); current ${current}`;
  if (completeRange) return `${completeDates}/${totalDates} dates complete (${completeRange})`;
  if (current) return `${completeDates}/${totalDates} dates complete; current ${current}`;
  return `${completeDates}/${totalDates} dates complete`;
}

function formatBroadBenchmarkSummary(latestBenchmark) {
  if (!latestBenchmark || !latestBenchmark.path) return "none yet";
  const tested = Number(latestBenchmark.confirmed_count || 0);
  const top1 = Number(latestBenchmark.top_1_count || 0);
  const topN = Number(latestBenchmark.top_n_count || 0);
  const misses = Number(latestBenchmark.miss_count || 0);
  return `${tested} known originals tested · ${top1} top choice · ${topN} in top ${latestBenchmark.max_results || "N"} · ${misses} missed`;
}

function formatBroadSearchSummary(latestRun, active) {
  if (!latestRun || !latestRun.run_id) return "none yet";
  const scope = formatJsonScope(latestRun.target_scope_json || "");
  const total = Number(latestRun.target_count || 0);
  const done = Number(latestRun.processed_target_count || 0);
  const current = Number(latestRun.current_target_index || 0);
  const progress = active && total ? `${done}/${total} done${current > done ? ` · checking ${current}/${total}` : ""}` : `${total} searched`;
  return `${progress} · ${latestRun.scanned_count || 0} candidates scanned · ${latestRun.matched_entries || 0} entries have saved candidates${scope ? ` · ${scope}` : ""}`;
}

function formatBroadSearchHeartbeat(latestRun, active) {
  if (!latestRun || !latestRun.run_id) return "none yet";
  const phase = latestRun.phase || (active ? "running" : "finished");
  const entry = latestRun.current_entry_id || "";
  const candidateCount = Number(latestRun.current_candidate_count || 0);
  const heartbeatAt = latestRun.heartbeat_at ? Date.parse(latestRun.heartbeat_at) : 0;
  const age = heartbeatAt ? `${formatElapsed(Date.now() - heartbeatAt)} ago` : "unknown";
  const pieces = [phase];
  if (entry) pieces.push(entry);
  if (candidateCount) pieces.push(`${candidateCount} current candidates`);
  pieces.push(`updated ${age}`);
  return pieces.join(" · ");
}

function formatJsonScope(value) {
  if (!value) return "";
  try {
    const parsed = parseJsonObject(value);
    const start = parsed.start_date || "";
    const end = parsed.end_date || "";
    if (start || end) return `${start || "start"} to ${end || "end"}`;
    if ((parsed.entry_ids || []).length) return `${parsed.entry_ids.length} entry IDs`;
    if (parsed.broad_search_needed_list) return "broad-search-needed list";
  } catch (error) {
    return "";
  }
  return "";
}

function parseJsonObject(value) {
  if (!value) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (error) {
    return {};
  }
}

function formatShortDateTime(value) {
  if (!value) return "unknown time";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return [
    parsed.getFullYear(),
    pad2(parsed.getMonth() + 1),
    pad2(parsed.getDate())
  ].join("-") + " " + [
    pad2(parsed.getHours()),
    pad2(parsed.getMinutes()),
    pad2(parsed.getSeconds())
  ].join(":");
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function formatCanonicalEntries(db) {
  const total = Number(db.entry_count || 0);
  const nonProject365 = Number(db.non_project365_entry_count || 0);
  const extra = nonProject365 ? ` · ${nonProject365} non-Project365 staged` : "";
  return `${total} total${extra}`;
}

function formatPendingApply(pending) {
  const decisions = Number(pending.decision_count || 0);
  if (!decisions) return "none";
  return `${pending.entry_count || 0} entries · ${pending.selected_count || 0} selected · ${pending.rejected_count || 0} rejected · ${pending.fallback_count || 0} fallback`;
}

function formatRemainderOverview(summary) {
  if (!summary || !summary.exists) return "not generated";
  return `${summary.date_count || 0} dates · ${summary.review_ready_date_count || 0} review-ready · ${summary.folder_needed_date_count || 0} need folders · ${summary.only_export_equivalent_date_count || 0} export-copy only · ${summary.all_rejected_date_count || 0} all rejected`;
}

function formatDiariumLocal(status) {
  if (!status || !status.exists) return "not found";
  if (status.readable === false) return `not readable · ${escapeHtml(status.error || "")}`;
  const missing = Number(status.entries_without_media || 0);
  const empty = Number(status.empty_media_count || 0);
  const alerts = [];
  if (missing) alerts.push(`${missing} entries without photo attachments`);
  if (empty) alerts.push(`${empty} empty photo attachments`);
  const dateRange = status.date_start && status.date_end ? ` · ${escapeHtml(status.date_start)} to ${escapeHtml(status.date_end)}` : "";
  const alertText = alerts.length ? ` · ${escapeHtml(alerts.join(" · "))}` : "";
  return `${status.entry_count || 0} entries · ${status.media_count || 0} photo attachments${dateRange}${alertText}`;
}

function formatDiariumPackage(status) {
  if (!status || !status.exists) return "no package";
  const filename = status.filename || (status.path || "").split("/").pop() || "package";
  return `${escapeHtml(filename)} · ${status.journal_entries || 0} entries · ${status.photo_files || 0} photo files · ${status.journal_photo_refs || 0} photo refs`;
}

function formatDiariumImportInstruction(status) {
  if (!status || !status.exists || !status.path) return "";
  return `<div class="status-note"><strong>Import method:</strong> Use Diarium Settings > Diary > Migrate from other app > Day One, then choose this ZIP. Do not use Import diary; this file is not a Diarium database backup.</div>`;
}

function formatDiariumPackageReadiness(status) {
  if (!status || !status.exists) return "no package";
  if (status.photo_ready) return "photo-ready";
  const problems = [];
  const refs = Number(status.journal_photo_refs || 0);
  const files = Number(status.photo_files || 0);
  const manifestPhotos = Number(status.manifest_photo_rows || 0);
  if (!refs) problems.push("no photo refs in Journal.json");
  if (files < refs) problems.push(`photo files ${files}/${refs}`);
  if (manifestPhotos !== refs) problems.push(`manifest photos ${manifestPhotos}/${refs}`);
  if (Number(status.missing_photo_refs || 0)) problems.push(`${status.missing_photo_refs} refs missing files`);
  if (Number(status.missing_photo_markers || 0)) problems.push(`${status.missing_photo_markers} missing entry markers`);
  if (Number(status.zero_dimension_photo_refs || 0)) problems.push(`${status.zero_dimension_photo_refs} zero-dimension refs`);
  return `attention · ${escapeHtml(problems.join(" · "))}`;
}

function formatDiariumImportCheck(packageStatus, diariumLocal) {
  if (!packageStatus || !packageStatus.exists) return "no package";
  if (!packageStatus.photo_ready) return `package attention · ${formatDiariumPackageReadiness(packageStatus).replace(/^attention · /, "")}`;
  if (!diariumLocal || !diariumLocal.exists) return "local Diarium not found";
  if (diariumLocal.readable === false) return "local Diarium not readable";
  const packageEntries = Number(packageStatus.journal_entries || 0);
  const packagePhotos = Number(packageStatus.photo_files || 0);
  const localEntries = Number(diariumLocal.entry_count || 0);
  const localPhotos = Number(diariumLocal.media_count || 0);
  const missingPhotos = Number(diariumLocal.entries_without_media || 0);
  const emptyPhotos = Number(diariumLocal.empty_media_count || 0);
  const packageHashes = Array.isArray(packageStatus.photo_hashes) ? packageStatus.photo_hashes : [];
  const localNames = new Set(Array.isArray(diariumLocal.media_names) ? diariumLocal.media_names : []);
  const mediaNameMatches = packageHashes.filter(name => localNames.has(name)).length;
  const namesMatch = !packageHashes.length || mediaNameMatches === packageHashes.length;
  const matches = packageEntries === localEntries && packagePhotos === localPhotos && !missingPhotos && !emptyPhotos && namesMatch;
  if (matches) {
    return `counts match · ${localEntries} entries · ${localPhotos} photo attachments`;
  }
  const problems = [];
  if (packageEntries !== localEntries) problems.push(`entries ${localEntries}/${packageEntries}`);
  if (packagePhotos !== localPhotos) problems.push(`photos ${localPhotos}/${packagePhotos}`);
  if (missingPhotos) problems.push(`${missingPhotos} entries without attachments`);
  if (emptyPhotos) problems.push(`${emptyPhotos} empty attachments`);
  if (!namesMatch) problems.push(`media names ${mediaNameMatches}/${packageHashes.length} match latest package${formatMissingMediaDates(packageStatus, localNames)}`);
  return `attention · ${escapeHtml(problems.join(" · "))}`;
}

function formatMissingMediaDates(packageStatus, localNames) {
  const entries = Array.isArray(packageStatus.photo_hash_entries) ? packageStatus.photo_hash_entries : [];
  const missing = entries
    .filter(item => item.photo_hash && !localNames.has(item.photo_hash))
    .map(item => item.entry_date || item.entry_id)
    .filter(Boolean);
  if (!missing.length) return "";
  const visible = missing.slice(0, 5).join(", ");
  const suffix = missing.length > 5 ? `, +${missing.length - 5} more` : "";
  return ` (${visible}${suffix})`;
}

function formatDiariumImportVerification(verification) {
  if (!verification || !verification.message) return "not checked";
  return escapeHtml(verification.message);
}

function formatDiariumAttachmentNote(verification) {
  if (!verification || !verification.photo_imported) return "";
  return `<div class="status-note"><strong>Photos are imported.</strong> ${escapeHtml(verification.review_hint || "")}</div>`;
}

function formatBatch(batch) {
  if (!batch || !batch.start_date) return "none";
  const hidden = Number(batch.hidden_rejected_candidate_count || "0");
  const rejectedText = hidden ? ` · ${hidden} rejected hidden` : "";
  return `${escapeHtml(batch.start_date)} to ${escapeHtml(batch.end_date)} · ${escapeHtml(batch.entry_count || "0")} entries${rejectedText} · ${escapeHtml(batch.recommended_action || "")}`;
}

function formatSearchAttempt(attempt) {
  if (!attempt || !attempt.finished_at) return "none";
  const target = formatAttemptScope(attempt);
  return `${escapeHtml(attempt.finished_at)} · ${escapeHtml(attempt.unclear_entry_count || "0")} entries · ${escapeHtml(attempt.candidate_count || "0")} candidates · ${escapeHtml(target || "all unmatched")}`;
}

function formatAttemptScope(attempt) {
  if (!attempt) return "all unmatched";
  if (attempt.target_entry_dates) return replaceAllText(attempt.target_entry_dates, ";", "; ");
  if (attempt.target_entry_ids) return replaceAllText(attempt.target_entry_ids, ";", "; ");
  if (attempt.start_date && attempt.end_date) return `${attempt.start_date} to ${attempt.end_date}`;
  return "all unmatched";
}

function renderBatchOverview(payload) {
  const target = document.getElementById("batchOverview");
  if (!target) return;
  const batchPlan = payload.original_batch_plan || {};
  const batches = (batchPlan.batches || []);
  if (!batches.length) {
    target.textContent = "No unresolved batches.";
    return;
  }
  const limitNote = Number(batchPlan.rows || 0) > Number(batchPlan.batch_limit || batches.length)
    ? `<div>Showing first ${batchPlan.batch_limit} of ${batchPlan.rows} batches. Use Review easy matches for the full queue.</div>`
    : "";
  const rows = batches.map(batch => {
    const hidden = Number(batch.hidden_rejected_candidate_count || "0");
    const hiddenCopies = Number(batch.hidden_export_equivalent_candidate_count || "0");
    return `
      <tr>
        <td><strong>${escapeHtml(batch.batch_id)}</strong></td>
        <td>${escapeHtml(batch.start_date)} to ${escapeHtml(batch.end_date)}</td>
        <td class="number">${escapeHtml(batch.entry_count || "0")}</td>
        <td class="number">${escapeHtml(batch.candidate_count || "0")}</td>
        <td class="number">${escapeHtml(batch.review_date_count || "0")}</td>
        <td class="number">${escapeHtml(batch.folder_needed_date_count || "0")}</td>
        <td class="number">${hidden}</td>
        <td class="number">${hiddenCopies}</td>
        <td>${escapeHtml(formatBatchStatuses(batch.statuses || ""))}</td>
        <td>${escapeHtml(batch.next_step || "")}</td>
        <td class="number">${escapeHtml(batch.search_attempt_count || "0")}</td>
        <td title="${escapeHtml(batch.latest_search_roots || "")}">${escapeHtml(formatBatchAttempt(batch))}</td>
        <td><a href="${escapeHtml(pickerUrlForBatch(batch))}">Review in picker</a></td>
      </tr>
    `;
  }).join("");
  target.innerHTML = `
    ${limitNote}
    <table class="batch-table">
      <thead>
        <tr>
          <th>Batch</th>
          <th>Dates</th>
          <th class="number">Entries</th>
          <th class="number">Candidates</th>
          <th class="number">Review dates</th>
          <th class="number">Need folders</th>
          <th class="number">Rejected</th>
          <th class="number">Export copies</th>
          <th>Status</th>
          <th>Next step</th>
          <th class="number">Searches</th>
          <th>Last search</th>
          <th></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function formatBatchStatuses(statuses) {
  const labels = {
    needs_choice: "Review candidates",
    no_external_candidates: "Choose another folder",
    only_export_equivalent_candidates: "Only export-copy candidates",
    all_candidates_rejected: "All found candidates rejected"
  };
  const parts = splitAttemptList(statuses || "");
  if (!parts.length) return "";
  return parts.map(status => labels[status] || replaceAllText(status, "_", " ")).join("; ");
}

function formatBatchAttempt(batch) {
  const count = Number(batch.search_attempt_count || "0");
  if (!count) return "not searched";
  const candidates = batch.latest_search_candidate_count || "0";
  return `${formatShortDateTime(batch.latest_search_finished_at)} · ${candidates} candidates`;
}

function renderAttemptOverview(payload) {
  const target = document.getElementById("attemptOverview");
  if (!target) return;
  const attempts = ((payload.original_search_attempts || {}).recent || []);
  if (!attempts.length) {
    target.textContent = "No search attempts yet.";
    return;
  }
  const rows = attempts.map(attempt => `
    <tr>
      <td>${escapeHtml(formatShortDateTime(attempt.finished_at || ""))}</td>
      <td>${escapeHtml(formatAttemptScope(attempt))}</td>
      <td>${escapeHtml(replaceAllText(attempt.search_roots || "", ";", "; "))}</td>
      <td class="number">${escapeHtml(attempt.unclear_entry_count || "0")}</td>
      <td class="number">${escapeHtml(attempt.candidate_count || "0")}</td>
      <td class="number">${escapeHtml(attempt.hidden_rejected_candidate_count || "0")}</td>
      <td class="number">${escapeHtml(attempt.hidden_export_equivalent_candidate_count || "0")}</td>
    </tr>
  `).join("");
  target.innerHTML = `
    <table class="batch-table">
      <thead>
        <tr>
          <th>Finished</th>
          <th>Scope</th>
          <th>Folders</th>
          <th class="number">Entries</th>
          <th class="number">Candidates</th>
          <th class="number">Rejected</th>
          <th class="number">Export copies</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function pathLine(label, item) {
  const exists = item && item.exists;
  return `<div class="status-item"><span>${escapeHtml(label)}</span><strong class="${exists ? "ok" : "bad"}">${exists ? "ok" : "missing"} · ${escapeHtml(item ? item.path : "")}</strong></div>`;
}

async function chooseFolderInFinder(targetInputId = "indexRoots", mode = "append") {
  const trigger = activeButton();
  markButtonRunning(trigger, true);
  try {
    await promptForFolderPath(targetInputId, mode);
  } finally {
    markButtonRunning(trigger, false);
  }
}

async function promptForFolderPath(targetInputId, mode) {
  const promptText = "Paste the folder path here:";
  const typedPath = window.prompt(promptText, "");
  if (!typedPath) {
    const message = "Folder selection canceled. You can also paste a folder path directly into the field.";
    setFolderChooserMessage(targetInputId, message, "error");
    if (targetInputId === "easyMatchIndexFolder") {
      setStepMessage("match_easy_originals", message, "error");
    }
    return;
  }
  setFolderChooserMessage(targetInputId, "Checking folder path...", "running");
  try {
    const payload = await fetchJson("/api/validate-folder-path", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({path: typedPath})
    });
    if (mode === "replace") {
      document.getElementById(targetInputId).value = payload.path;
    } else {
      appendSearchRoot(payload.path, targetInputId);
    }
    setFolderChooserMessage(targetInputId, `Added folder: ${payload.path}`, "ok");
  } catch (validationError) {
    const message = `Folder path rejected: ${validationError.message}`;
    setFolderChooserMessage(targetInputId, message, "error");
    if (targetInputId === "easyMatchIndexFolder") {
      setStepMessage("match_easy_originals", message, "error");
    }
  }
}

function setFolderChooserMessage(targetInputId, message, state = "") {
  const output = document.getElementById("output");
  if (output) {
    output.hidden = false;
    output.textContent = message;
  }
  const targetId = targetInputId === "indexRoots"
    ? "photoIndexMessage"
    : targetInputId === "broadCandidateRoots"
      ? "broadVisualActionMessage"
    : targetInputId === "digikamXmpRoots"
      ? "digikamPeopleMessage"
      : "";
  const target = targetId ? document.getElementById(targetId) : null;
  if (!target) return;
  target.hidden = false;
  target.textContent = message;
  target.classList.toggle("error", state === "error");
  target.classList.toggle("ok", state === "ok");
  target.classList.toggle("is-running", state === "running");
}

function appendSearchRoot(path, targetInputId = "indexRoots") {
  const input = document.getElementById(targetInputId);
  const existing = parseDelimited(targetInputId);
  if (!existing.includes(path)) {
    existing.push(path);
  }
  input.value = existing.join("; ");
}

function chooseFolderFromIndexedFile(targetInputId) {
  const input = document.getElementById("easyMatchIndexFile");
  input.value = "";
  input.onchange = () => resolveFolderFromIndexedFile(input, targetInputId);
  input.click();
}

async function resolveFolderFromIndexedFile(fileInput, targetInputId) {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  setStepMessage("match_easy_originals", "Matching selected file against the photo index...", "running");
  document.getElementById("output").textContent = "Matching selected file against the photo index...";
  try {
    const sha256 = await sha256File(file);
    const payload = await fetchJson("/api/photo-index-folder-from-file", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({filename: file.name, byte_size: file.size, sha256})
    });
    document.getElementById(targetInputId).value = payload.path || "";
    const message = payload.matched_file
      ? `Using folder: ${payload.path}`
      : "Folder selected.";
    setStepMessage("match_easy_originals", message);
    document.getElementById("output").textContent = message;
  } catch (error) {
    const message = `Could not resolve selected file: ${error.message}`;
    setStepMessage("match_easy_originals", message, "error");
    document.getElementById("output").textContent = message;
  }
}

async function sha256File(file) {
  if (!window.crypto || !window.crypto.subtle) {
    throw new Error("This browser cannot hash local files from the current page.");
  }
  const hashBuffer = await window.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(hashBuffer))
    .map(value => value.toString(16).padStart(2, "0"))
    .join("");
}

async function runEasyMatch() {
  const limitToFolder = document.getElementById("limitEasyMatchToIndexFolder").checked;
  const includeLowQualityMatches = document.getElementById("includeLowQualityMatches").checked;
  const folder = document.getElementById("easyMatchIndexFolder").value.trim();
  if (limitToFolder && !folder) {
    setStepMessage("match_easy_originals", "Choose an indexed folder before running filtered easy matches.", "fail");
    return;
  }
  await runStep("match_easy_originals", {
    limit_to_photo_index_folder: limitToFolder,
    photo_index_folder: limitToFolder ? folder : "",
    include_low_quality_matches: includeLowQualityMatches
  });
}

async function buildPhotoIndex() {
  const searchRoots = parseDelimited("indexRoots");
  const resetControl = document.getElementById("resetPhotoIndex");
  const resetPhotoIndex = resetControl.checked;
  if (resetPhotoIndex && !confirmPhotoIndexReplacement()) {
    resetControl.checked = false;
    setStepMessage("build_photo_index", "Photo index replacement canceled.");
    return;
  }
  if (!searchRoots.length) setStepMessage("build_photo_index", "Refreshing existing indexed folders...", "running");
  await runStep("build_photo_index", {
    search_roots: searchRoots,
    reset_photo_index: resetPhotoIndex,
    reset_confirmation: resetPhotoIndex ? "replace-photo-index" : ""
  });
}

async function refreshPhotoIndexMetadata() {
  const searchRoots = parseDelimited("indexRoots");
  const reconcileMovesOnly = document.getElementById("reconcileMovedPhotoIndex").checked;
  const message = searchRoots.length
    ? (reconcileMovesOnly ? "Reconciling moved/renamed files in selected folder..." : "Refreshing metadata for selected folder...")
    : (reconcileMovesOnly ? "Reconciling moved/renamed files in existing indexed folders..." : "Refreshing metadata for existing indexed folders...");
  setStepMessage("refresh_photo_index_metadata", message, "running");
  await runStep("refresh_photo_index_metadata", {
    search_roots: searchRoots,
    reconcile_moves_only: reconcileMovesOnly
  });
}

async function buildBroadVisualIndex() {
  const settings = broadVisualSettings();
  if (settings.confirmed_only && settings.target_scope === "all_unresolved") {
    setStepMessage("broad_visual_index", "Choose a date range, entry IDs, or list for a constrained confirmed-only index.", "error");
    return;
  }
  if (settings.confirmed_only) {
    setStepMessage("broad_visual_index", "Confirmed-only fingerprints support Measure accuracy; they do not improve Monthly fingerprint coverage.", "running");
  }
  await runStep("broad_visual_index", settings);
}

async function runBroadVisualMatch() {
  const settings = broadVisualSettings();
  const trigger = activeButton();
  if (settings.confirmed_only) {
    setStepMessage("broad_visual_match", "Fingerprint already confirmed originals is for Build fingerprints plus Measure accuracy. Uncheck it before searching unresolved photos.", "error");
    return;
  }
  if (settings.target_scope === "entry_ids" && !settings.entry_ids.length) {
    setStepMessage("broad_visual_match", "Enter at least one entry ID.", "error");
    return;
  }
  if (settings.target_scope === "date_range" && (!settings.start_date || !settings.end_date)) {
    setStepMessage("broad_visual_match", "Enter both start and end dates.", "error");
    return;
  }
  if (settings.target_scope === "broad_search_needed_list" && !settings.broad_search_needed_list) {
    setStepMessage("broad_visual_match", "Enter a broad-search-needed list path.", "error");
    return;
  }
  if (["folder_limited", "same_setting_folder_limited"].includes(settings.candidate_scope) && !settings.candidate_roots.length) {
    setStepMessage("broad_visual_match", "Choose at least one candidate folder for folder-limited matching.", "error");
    return;
  }
  if (settings.candidate_scope === "date_window_limited" && !settings.date_window_days) {
    setStepMessage("broad_visual_match", "Set Candidate date window, +/- days. This is separate from Target start/end date; use 30 to 90 days, or choose Whole indexed library.", "error");
    return;
  }
  if (settings.target_scope === "date_range" && settings.candidate_scope === "whole_indexed_library") {
    setStepMessage("broad_visual_match", "Date-range unresolved search must use a candidate date window. Whole indexed library compares each target to every stored fingerprint.", "error");
    return;
  }
  setButtons(true, trigger);
  markButtonRunning(trigger, true);
  setStepMessage("broad_visual_match", "Checking fingerprint coverage before search...", "running");
  try {
    const coverage = await checkBroadVisualCoverage();
    if (coverage.blocking) return;
    await runStep("broad_visual_match", settings, trigger);
  } catch (error) {
    setStepMessage("broad_visual_match", error.message || "Could not start broad visual search.", "error");
    document.getElementById("output").textContent = error.message || String(error);
  } finally {
    if (!running) {
      markButtonRunning(trigger, false);
      setButtons(false);
    }
  }
}

async function runBroadVisualBenchmark() {
  const settings = broadVisualSettings();
  await runStep("broad_visual_benchmark", settings);
}

function roughVisualSettings() {
  const scope = document.getElementById("roughTargetScope")?.value || "all_unresolved";
  return {
    target_scope: scope,
    entry_ids: scope === "entry_ids" ? parseDelimited("roughEntryIds").map(normalizeProject365EntryJump).filter(Boolean) : [],
    start_date: scope === "date_range" ? document.getElementById("roughStartDate").value.trim() : "",
    end_date: scope === "date_range" ? document.getElementById("roughEndDate").value.trim() : "",
    broad_search_needed_list: scope === "broad_search_needed_list" ? document.getElementById("roughNeededList").value.trim() : "",
    shortlist_size: Number(document.getElementById("roughShortlistSize").value || "1000"),
    per_band_hit_limit: Number(document.getElementById("roughPerBandHitLimit").value || "50000"),
    max_results: Number(document.getElementById("roughMaxResults").value || "20"),
    density: Number(document.getElementById("roughDensity").value || "9"),
    shortlist_sizes: document.getElementById("roughBenchmarkShortlistSizes").value.trim(),
    include_low_quality_candidates: Boolean(document.getElementById("roughIncludeLowQuality").checked),
    overwrite_existing_prefilter: Boolean(document.getElementById("roughOverwritePrefilter").checked),
    resume_existing_run: Boolean(document.getElementById("roughResumeExisting").checked),
    dry_run: Boolean(document.getElementById("roughDryRun").checked)
  };
}

async function buildRoughPrefilter() {
  const settings = roughVisualSettings();
  await runStep("rough_prefilter_build", settings);
}

async function checkRoughPrefilterReadiness() {
  setStepMessage("rough_visual_match", "Checking rough prefilter readiness...", "running");
  const status = await loadStatus(["rough_visual_match"]);
  const prefilter = ((status.broad_visual_match || {}).rough_prefilter || {});
  if (prefilter.ready && !Number(prefilter.stale_count || 0)) {
    setStepMessage("rough_visual_match", `Rough prefilter ready: ${prefilter.feature_count || 0} rows.`);
    return true;
  }
  if (prefilter.ready) {
    setStepMessage("rough_visual_match", `Rough prefilter has ${prefilter.feature_count || 0} rows, with ${prefilter.stale_count || 0} stale or missing. Build rough prefilter before a large no-date run.`, "error");
    return false;
  }
  setStepMessage("rough_visual_match", "Build rough prefilter before running no-date matching.", "error");
  return false;
}

async function runRoughVisualMatch() {
  const settings = roughVisualSettings();
  if (settings.target_scope === "entry_ids" && !settings.entry_ids.length) {
    setStepMessage("rough_visual_match", "Enter at least one entry ID.", "error");
    return;
  }
  if (settings.target_scope === "date_range" && (!settings.start_date || !settings.end_date)) {
    setStepMessage("rough_visual_match", "Enter both target start and end dates.", "error");
    return;
  }
  if (settings.target_scope === "broad_search_needed_list" && !settings.broad_search_needed_list) {
    setStepMessage("rough_visual_match", "Enter a broad-search-needed list path.", "error");
    return;
  }
  if (!settings.shortlist_size || settings.shortlist_size <= 0) {
    setStepMessage("rough_visual_match", "Shortlist size must be greater than 0.", "error");
    return;
  }
  if (!settings.per_band_hit_limit || settings.per_band_hit_limit <= 0) {
    setStepMessage("rough_visual_match", "Per-band hit cap must be greater than 0.", "error");
    return;
  }
  await runStep("rough_visual_match", settings);
}

async function runRoughVisualBenchmark() {
  const settings = roughVisualSettings();
  await runStep("rough_visual_benchmark", settings);
}

function openBroadVisualReview(modeOverride = "", reviewSetOverride = "broad") {
  const button = document.getElementById("broadReviewResultsButton");
  const mode = modeOverride || button?.dataset.reviewMode || "search";
  const reviewSet = reviewSetOverride || button?.dataset.reviewSet || "broad";
  const entryId = normalizeProject365EntryJump(document.getElementById("broadReviewEntryId")?.value || "");
  const unresolvedReadyCount = Number(button?.dataset.unresolvedReadyCount || 0);
  if (mode === "search" && reviewSet === "broad" && !entryId && unresolvedReadyCount <= 0) {
    setStepMessage("broad_visual_match", button?.title || "No unresolved Broad Visual results are ready yet.", "error");
    return;
  }
  const params = new URLSearchParams();
  params.set("mode", mode);
  if (mode === "search" && reviewSet !== "broad") params.set("review_set", reviewSet);
  if (entryId) params.set("entry_id", entryId);
  params.set("limit", "1");
  window.location.assign(`/broad-review?${params.toString()}`);
}

function normalizeProject365EntryJump(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return /^\d{4}-\d{2}-\d{2}$/.test(text) ? `project365:${text}` : text;
}

function openRoughVisualReview() {
  openBroadVisualReview("search", "rough");
}

function broadVisualSettings() {
  const scope = document.getElementById("broadTargetScope")?.value || "all_unresolved";
  return {
    candidate_roots: parseDelimited("broadCandidateRoots"),
    target_scope: scope,
    entry_ids: scope === "entry_ids" ? parseDelimited("broadEntryIds").map(normalizeProject365EntryJump).filter(Boolean) : [],
    start_date: scope === "date_range" ? document.getElementById("broadStartDate").value.trim() : "",
    end_date: scope === "date_range" ? document.getElementById("broadEndDate").value.trim() : "",
    broad_search_needed_list: scope === "broad_search_needed_list" ? document.getElementById("broadNeededList").value.trim() : "",
    candidate_scope: document.getElementById("broadCandidateScope").value,
    max_results: Number(document.getElementById("broadMaxResults").value || "20"),
    density: Number(document.getElementById("broadDensity").value || "9"),
    date_window_days: Number(document.getElementById("broadDateWindowDays").value || "0"),
    include_low_quality_candidates: Boolean(document.getElementById("broadIncludeLowQuality").checked),
    confirmed_only: Boolean(document.getElementById("broadConfirmedOnly").checked),
    resume_existing_run: Boolean(document.getElementById("broadResumeExisting").checked),
    overwrite_existing_fingerprints: Boolean(document.getElementById("broadOverwriteFingerprints").checked),
    dry_run: Boolean(document.getElementById("broadDryRun").checked)
  };
}

async function checkBroadVisualCoverage() {
  const settings = broadVisualSettings();
  let status = lastStatus || {};
  setBroadCoverageStatus("Checking fingerprint coverage...", "running");
  if (!((status.broad_visual_match || {}).monthly_coverage || []).length) {
    status = await loadStatus(["broad_visual_match"], {includeBroadMonthlyCoverage: true});
  }
  const coverage = broadCoveragePreview(settings, status);
  setBroadCoverageStatus(coverage.message, coverage.ok ? "" : "error");
  return coverage;
}

function setBroadCoverageStatus(message, state = "") {
  const target = document.getElementById("broadCoverageCheckStatus");
  if (!target) return;
  target.textContent = message || "";
  target.className = `coverage-check-status${state ? ` status-${state}` : ""}`;
}

function broadCoveragePreview(settings, status) {
  if (settings.candidate_scope !== "date_window_limited") {
    return {ok: true, blocking: false, message: "Coverage check applies to date-window candidate searches."};
  }
  const rows = ((status.broad_visual_match || {}).monthly_coverage || []);
  if (!rows.length) {
    return {ok: false, blocking: true, message: "Fingerprint coverage is unavailable. Refresh status or build fingerprints first."};
  }
  const candidateMonths = broadCandidateMonths(settings);
  const targetMonths = broadTargetMonths(settings);
  const selectedRows = candidateMonths
    ? rows.filter(row => candidateMonths.has(row.month))
    : rows;
  const measuredRows = selectedRows.filter(row => Number(row.photo_index_count || 0) >= 20);
  if (!measuredRows.length) {
    return {ok: false, blocking: true, message: "No indexed candidate photos found for the selected date window."};
  }
  const lowRows = measuredRows.filter(row => broadCoverageRatio(row) < 0.8);
  if (lowRows.length) {
    return {
      ok: false,
      blocking: false,
      message: `Fingerprint coverage too low: ${formatCoverageRowList(lowRows)}. Search will continue; run Build fingerprints with Fingerprint already confirmed originals unchecked to improve recall.`
    };
  }
  const minRow = measuredRows.reduce((lowest, row) => (
    broadCoverageRatio(row) < broadCoverageRatio(lowest) ? row : lowest
  ));
  const missingOriginals = targetMonths
    ? rows.filter(row => targetMonths.has(row.month)).reduce((sum, row) => sum + Number(row.missing_original_targets || 0), 0)
    : rows.reduce((sum, row) => sum + Number(row.missing_original_targets || 0), 0);
  return {
    ok: true,
    blocking: false,
    message: `Fingerprint coverage OK: minimum ${formatCoverageRow(minRow)} across ${measuredRows.length} candidate month(s); ${missingOriginals} missing-original target(s).`
  };
}

function broadTargetMonths(settings) {
  if (settings.target_scope !== "date_range" || !settings.start_date || !settings.end_date) return null;
  const start = parseDateBound(settings.start_date, false);
  const end = parseDateBound(settings.end_date, true);
  if (!start || !end) return null;
  return monthsBetween(start, end);
}

function broadCandidateMonths(settings) {
  if (settings.target_scope !== "date_range" || !settings.start_date || !settings.end_date) return null;
  const start = parseDateBound(settings.start_date, false);
  const end = parseDateBound(settings.end_date, true);
  if (!start || !end) return null;
  const windowDays = Number(settings.date_window_days || 0);
  return monthsBetween(addUtcDays(start, -windowDays), addUtcDays(end, windowDays));
}

function parseDateBound(value, endOfMonth = false) {
  const text = String(value || "").trim();
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (dateMatch) {
    return new Date(Date.UTC(Number(dateMatch[1]), Number(dateMatch[2]) - 1, Number(dateMatch[3])));
  }
  const monthMatch = /^(\d{4})-(\d{2})$/.exec(text);
  if (!monthMatch) return null;
  const year = Number(monthMatch[1]);
  const month = Number(monthMatch[2]);
  if (month < 1 || month > 12) return null;
  return endOfMonth
    ? new Date(Date.UTC(year, month, 0))
    : new Date(Date.UTC(year, month - 1, 1));
}

function addUtcDays(date, days) {
  const next = new Date(date.getTime());
  next.setUTCDate(next.getUTCDate() + Number(days || 0));
  return next;
}

function monthsBetween(start, end) {
  const months = new Set();
  let current = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), 1));
  const last = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), 1));
  while (current <= last) {
    months.add(`${current.getUTCFullYear()}-${String(current.getUTCMonth() + 1).padStart(2, "0")}`);
    current.setUTCMonth(current.getUTCMonth() + 1);
  }
  return months;
}

function broadCoverageRatio(row) {
  const photoCount = Number(row.photo_index_count || 0);
  return photoCount ? Number(row.fingerprint_count || 0) / photoCount : 0;
}

function formatCoverageRow(row) {
  return `${row.month} ${formatPercent(broadCoverageRatio(row))} (${Number(row.fingerprint_count || 0)}/${Number(row.photo_index_count || 0)})`;
}

function formatCoverageRowList(rows) {
  return rows.slice(0, 4).map(formatCoverageRow).join(", ") + (rows.length > 4 ? `, +${rows.length - 4} more` : "");
}

function confirmPhotoIndexReplacement() {
  const firstConfirmed = window.confirm(
    "Replace the existing photo index? This will delete the current index before rebuilding it."
  );
  if (!firstConfirmed) return false;
  return window.confirm(
    "Are you sure? This is the final confirmation. The existing photo index will be replaced."
  );
}

function openEasyMatchesInPicker() {
  const url = new URL("__PICKER_URL__", window.location.href);
  url.searchParams.set("status", "needs_review");
  const folder = activePhotoIndexFolderForPicker();
  if (folder) url.searchParams.set("photo_index_folder", folder);
  window.location.assign(url.toString());
}

function pickerUrlForBatch(batch) {
  if (!batch || !batch.start_date) return "__PICKER_URL__";
  const url = new URL("__PICKER_URL__", window.location.href);
  const folder = activePhotoIndexFolderForPicker();
  if (folder) url.searchParams.set("photo_index_folder", folder);
  if (batch.batch_id) url.searchParams.set("batch", batch.batch_id);
  url.searchParams.set("status", "needs_action");
  for (const entryId of splitAttemptList(batch.entry_ids || "")) {
    url.searchParams.append("entry_id", entryId);
  }
  for (const entryDate of splitAttemptList(batch.entry_dates || "")) {
    url.searchParams.append("entry_date", entryDate);
  }
  return url.toString();
}

function activePhotoIndexFolderForPicker() {
  const limitToFolder = document.getElementById("limitEasyMatchToIndexFolder").checked;
  const folder = document.getElementById("easyMatchIndexFolder").value.trim();
  return limitToFolder ? folder : "";
}

function parseDelimited(id) {
  return document.getElementById(id).value
    .split(";")
    .map(value => stripWrappingQuotes(value))
    .filter(Boolean);
}

function stripWrappingQuotes(value) {
  let stripped = String(value || "").trim();
  while (stripped.length >= 2) {
    const first = stripped[0];
    const last = stripped[stripped.length - 1];
    if ((first !== "'" && first !== '"') || first !== last) break;
    stripped = stripped.slice(1, -1).trim();
  }
  return stripped;
}

function splitAttemptList(value) {
  return String(value)
    .split(";")
    .map(item => item.trim())
    .filter(Boolean);
}

async function runDiariumPackage() {
  await runStep("generate_diarium_package", {
    start_date: document.getElementById("startDate").value.trim(),
    end_date: document.getElementById("endDate").value.trim(),
    limit: Number(document.getElementById("limit").value || "100000")
  });
}

async function importDigiKamPeople() {
  await runStep("import_digikam_people", {
    xmp_roots: parseDelimited("digikamXmpRoots"),
    suggestions_csv: document.getElementById("digikamSuggestionsCsv").value.trim()
  });
}

async function runStep(step, extra = {}, triggerOverride = null) {
  if (running) return;
  const trigger = triggerOverride || activeButton();
  running = true;
  setButtons(true, trigger);
  markButtonRunning(trigger, true);
  startProgress(step);
  setStepMessage(step, "Starting...", "running");
  document.getElementById("output").textContent = `Starting ${step}...`;
  startRequestController = new AbortController();
  try {
    const startedJob = await fetchJson("/api/run", {
      method: "POST",
      headers: {"content-type": "application/json"},
      signal: startRequestController.signal,
      body: JSON.stringify(Object.assign({step: step}, extra || {}))
    });
    startRequestController = null;
    const result = startedJob.job_id ? await waitForJob(startedJob.job_id) : startedJob;
    finishProgress(result);
    setStepMessage(
      step,
      result.status === "pass" ? stepCompletionMessage(step) : (result.error || "Task failed."),
      result.status === "pass" ? "" : "error"
    );
    const refreshed = await loadStatus(expandedStatusSteps());
    if (result.status === "pass") {
      setStepMessage(step, stepRefreshedMessage(step, refreshed));
    }
  } catch (error) {
    const message = error.name === "AbortError"
      ? "Start cancelled before a server job was created."
      : error.message;
    stopProgress();
    updateProgress({step, status: "cancelled", started_at: new Date(progressStartedAt || Date.now()).toISOString(), error: message, outputs: []});
    setStepMessage(step, message, error.name === "AbortError" ? "" : "error");
    document.getElementById("output").textContent = message;
  } finally {
    startRequestController = null;
    running = false;
    markButtonRunning(trigger, false);
    setButtons(false);
  }
}

async function waitForJob(jobId) {
  while (true) {
    const job = await fetchJson(`/api/job/${encodeURIComponent(jobId)}`);
    updateProgress(job);
    setStepMessage(
      job.step,
      `${formatJobStatus(job.status || "running")} · ${formatElapsed(Date.now() - Date.parse(job.started_at))}`,
      "running"
    );
    document.getElementById("output").textContent = formatJob(job);
    if (["pass", "fail", "cancelled"].includes(job.status)) {
      return job;
    }
    await sleep(1000);
  }
}

function setStepMessage(step, message, state = "") {
  const targetIds = {
    build_photo_index: "photoIndexMessage",
    refresh_photo_index_metadata: "photoIndexMessage",
    match_easy_originals: "easyMatchMessage",
    broad_visual_index: "broadVisualActionMessage",
    broad_visual_match: "broadVisualActionMessage",
    broad_visual_benchmark: "broadVisualBenchmarkMessage",
    rough_prefilter_build: "roughVisualMessage",
    rough_visual_match: "roughVisualMessage",
    rough_visual_benchmark: "roughVisualMessage",
    generate_derivatives: "workingCopyMessage",
    import_digikam_people: "digikamPeopleMessage"
  };
  const target = document.getElementById(targetIds[step] || "");
  if (!target) return;
  target.textContent = message;
  target.className = `inline-status ${state}`.trim();
}

function stepCompletionMessage(step) {
  if (step === "build_photo_index") return "Photo index completed.";
  if (step === "refresh_photo_index_metadata") return "Photo index refresh completed.";
  if (step === "match_easy_originals") return "Match search completed. Refreshing results...";
  if (step === "broad_visual_index") return "Broad visual descriptor index completed.";
  if (step === "broad_visual_match") return "Broad visual match completed. Refreshing results...";
  if (step === "broad_visual_benchmark") return "Broad visual benchmark exported.";
  if (step === "rough_prefilter_build") return "Rough visual prefilter completed.";
  if (step === "rough_visual_match") return "No-date visual match completed. Refreshing results...";
  if (step === "rough_visual_benchmark") return "No-date visual benchmark exported.";
  if (step === "generate_derivatives") return "Working photo copies completed.";
  if (step === "import_digikam_people") return "digiKam suggestions imported. Tag queue refreshed.";
  return "Task completed.";
}

function stepRefreshedMessage(step, payload) {
  if (step === "broad_visual_match") {
    const run = ((payload || {}).broad_visual_match || {}).latest_run || {};
    if (run.run_id) {
      return broadVisualRunCompleteMessage(run);
    }
  }
  if (step === "rough_visual_match") {
    const run = ((payload || {}).broad_visual_match || {}).latest_no_date_run || {};
    if (run.run_id) {
      return roughVisualRunCompleteMessage(run);
    }
  }
  if (step === "match_easy_originals") return "Match search completed.";
  return stepCompletionMessage(step).replace(" Refreshing results...", "");
}

function startProgress(step) {
  progressStep = step;
  progressStartedAt = Date.now();
  activeJobId = "";
  manuallyExpandedSteps.add(ownerStep(step));
  updateProgress({step, status: "starting", started_at: new Date().toISOString(), outputs: []});
  progressTimer = setInterval(() => {
    if (activeJobId) return;
    updateProgress({step: progressStep, status: "starting", started_at: new Date(progressStartedAt).toISOString(), outputs: []});
  }, 1000);
}

function updateProgress(job) {
  if (job.job_id) activeJobId = job.job_id;
  const owner = ownerStep(job.step || progressStep);
  const card = workflowCard(owner);
  if (!card) return;
  renderWorkflowCard(card, owner, job, workflowRecordsFor(owner, (lastStatus || {}).workflow_history || {}));
}

function finishProgress(job) {
  stopProgress();
  updateProgress(job);
  document.getElementById("output").textContent = formatJob(job);
}

function stopProgress() {
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
}

async function cancelActiveJob() {
  const stopButton = document.querySelector(".run-progress button");
  if (stopButton) stopButton.disabled = true;
  if (!activeJobId) {
    if (startRequestController) startRequestController.abort();
    stopProgress();
    const message = "Start cancelled before a server job was created.";
    updateProgress({step: progressStep, status: "cancelled", started_at: new Date(progressStartedAt || Date.now()).toISOString(), error: message, outputs: []});
    setStepMessage(progressStep, message);
    document.getElementById("output").textContent = message;
    running = false;
    setButtons(false);
    if (stopButton) stopButton.disabled = false;
    return;
  }
  try {
    const job = await fetchJson(`/api/job/${encodeURIComponent(activeJobId)}/cancel`, {method: "POST"});
    updateProgress(job);
    document.getElementById("output").textContent = formatJob(job);
  } catch (error) {
    setStepMessage(progressStep, error.message, "error");
  } finally {
    if (stopButton) stopButton.disabled = false;
  }
}

function progressDetail(job) {
  if (job.kind === "crop_estimate_batch") {
    return cropEstimateProgressDetail(job);
  }
  if ((job.step || progressStep) === "broad_visual_index" && job.broad_visual_index_run) {
    return broadVisualIndexProgressDetail(job);
  }
  if ((job.step || progressStep) === "broad_visual_match" && job.broad_visual_run) {
    return broadVisualMatchProgressDetail(job);
  }
  if ((job.step || progressStep) === "rough_prefilter_build" && job.rough_prefilter_run) {
    return roughPrefilterProgressDetail(job);
  }
  if ((job.step || progressStep) === "rough_visual_match" && job.rough_visual_run) {
    return roughVisualProgressDetail(job);
  }
  if ((job.step || progressStep) === "generate_derivatives") {
    return workingCopyProgressDetail(job);
  }
  const latest = latestOutput(job);
  if (latest) {
    const lines = latest.trim().split("\n").filter(Boolean);
    return lines[lines.length - 1] || "Working...";
  }
  if (job.status === "starting" && !job.job_id && !activeJobId) return "Waiting for the server to create a job...";
  if (job.current_command) return genericRunningProgressDetail(job);
  if (job.status === "queued") return "Waiting for the current task to finish.";
  if (job.status === "cancelling") return "Stopping...";
  if (job.status === "cancelled") return "Stopped.";
  if (job.status === "pass") return "Finished.";
  if (job.status === "fail") return job.error || "Failed.";
  return "Working...";
}

function cropEstimateProgressDetail(job) {
  const processed = Number(job.processed_count || 0);
  const total = Number(job.target_count || 0);
  const estimated = Number(job.estimated_count || 0);
  const skipped = Number(job.skipped_count || 0);
  const failed = Number(job.failed_count || 0);
  const mode = job.apply_estimates ? "saved" : "previewed";
  const current = job.current_entry_id ? ` · ${job.current_entry_id}` : "";
  return `${processed}/${total} checked · ${estimated} ${mode} estimates · ${skipped} skipped · ${failed} failed${current}`;
}

function broadVisualIndexProgressDetail(job) {
  const run = job.broad_visual_index_run || {};
  const checked = Number(run.scanned_count || 0);
  const total = Number(run.total_candidate_count || 0);
  const indexed = Number(run.indexed_descriptor_count || 0);
  const reused = Number(run.reused_descriptor_count || 0);
  const errors = Number(run.error_count || 0);
  const skipped = Math.max(
    0,
    run.skipped_candidate_count === undefined || run.skipped_candidate_count === null
      ? checked - indexed - reused - errors
      : Number(run.skipped_candidate_count || 0)
  );
  const current = formatBroadCurrentItem(run);
  const currentText = current === "none" ? "" : ` · ${current}`;
  const heartbeat = run.heartbeat_at ? ` · heartbeat ${formatHeartbeatAge(run.heartbeat_at)}` : "";
  const pieces = [
    total ? `${checked}/${total} files` : `${checked} files`,
    `${indexed} fingerprinted`,
    `${reused} reused`,
    `${skipped} skipped`
  ];
  if (errors) pieces.push(`${errors} errors`);
  return `${pieces.join(" · ")}${currentText}${heartbeat}`;
}

function broadVisualMatchProgressDetail(job) {
  return visualMatchProgressDetail(job.broad_visual_run || {}, "candidates scanned");
}

function roughPrefilterProgressDetail(job) {
  const run = job.rough_prefilter_run || {};
  const scanned = Number(run.scanned_count || 0);
  const total = Number(run.total_descriptor_count || 0);
  const indexed = Number(run.indexed_feature_count || 0);
  const reused = Number(run.reused_feature_count || 0);
  const errors = Number(run.error_count || 0);
  const phase = run.current_phase || "working";
  const current = run.current_path ? ` · ${phase} · ${pathBasename(run.current_path)}` : ` · ${phase}`;
  const heartbeat = run.heartbeat_at ? ` · heartbeat ${formatHeartbeatAge(run.heartbeat_at)}` : "";
  const pieces = [
    total ? `${scanned}/${total} fingerprints` : `${scanned} fingerprints`,
    `${indexed} built`,
    `${reused} reused`
  ];
  if (errors) pieces.push(`${errors} errors`);
  return `${pieces.join(" · ")}${current}${heartbeat}`;
}

function roughVisualProgressDetail(job) {
  return visualMatchProgressDetail(job.rough_visual_run || {}, "dense loads");
}

function visualMatchProgressDetail(run, scanLabel) {
  const processed = Number(run.processed_target_count || 0);
  const total = Number(run.target_count || 0);
  const matched = Number(run.matched_entries || 0);
  const savedRows = Number(run.result_count || 0);
  const scanned = Number(run.scanned_count || 0);
  const errors = Number(run.error_count || 0);
  const current = run.current_entry_id
    ? ` · current ${run.current_entry_id}`
    : Number(run.current_target_index || 0)
      ? ` · current ${run.current_target_index}${total ? `/${total}` : ""}`
      : "";
  const heartbeat = run.heartbeat_at ? ` · heartbeat ${formatHeartbeatAge(run.heartbeat_at)}` : "";
  const stopNote = savedRows
    ? ` · Stop now: ${savedRows} saved row${savedRows === 1 ? "" : "s"} ready to review`
    : processed
      ? " · Stop now: progress saved, no review rows yet"
      : " · Stop now: no review rows yet";
  const pieces = [
    total ? `${processed}/${total} entries` : `${processed} entries`,
    `${matched} matched`,
    `${savedRows} saved rows`,
    `${scanned} ${scanLabel}`
  ];
  if (errors) pieces.push(`${errors} errors`);
  return `${pieces.join(" · ")}${current}${heartbeat}${stopNote}`;
}

function pathBasename(value) {
  const parts = String(value || "").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(value || "");
}

function formatHeartbeatAge(value) {
  const parsed = Date.parse(value || "");
  if (!Number.isFinite(parsed)) return "unknown";
  const ageMs = Math.max(0, Date.now() - parsed);
  if (ageMs < 5000) return "just now";
  if (ageMs < 60000) return `${Math.floor(ageMs / 1000)}s ago`;
  return `${formatElapsed(ageMs)} ago`;
}

function workingCopyProgressDetail(job) {
  const latest = latestOutput(job);
  const parsed = parseColonLines(latest);
  const generated = parsed.Generated || "";
  const skipped = parsed.Skipped || "";
  const notReady = parsed["Not ready"] || "";
  const notReadyDates = parsed["Not ready dates"] || "";
  const pieces = [];
  if (generated) pieces.push(`generated ${generated}`);
  if (skipped) pieces.push(`skipped ${skipped}`);
  if (notReady) pieces.push(`not ready ${notReady}`);
  if (notReadyDates) pieces.push(`missing: ${notReadyDates}`);
  if (pieces.length) return pieces.join(" · ");
  if (job.status === "queued") return "Waiting to generate working copies.";
  if (job.status === "cancelling") return "Stopping working-copy generation.";
  if (job.status === "cancelled") return "Stopped.";
  if (job.status === "pass") return "Working copies finished.";
  if (job.status === "fail") return job.error || "Working-copy generation failed.";
  return "Generating working copies.";
}

function genericRunningProgressDetail(job) {
  const startedAt = Date.parse(job.started_at || "");
  const elapsed = Number.isFinite(startedAt)
    ? ` · elapsed ${formatElapsed(Date.now() - startedAt)}`
    : "";
  if (job.status === "queued") return "Waiting for the current task to finish.";
  if (job.status === "cancelling") return "Stopping...";
  return `Still running · no detailed progress output yet${elapsed}`;
}

function parseColonLines(text) {
  const parsed = {};
  for (const line of String(text || "").split("\n")) {
    const index = line.indexOf(":");
    if (index <= 0) continue;
    parsed[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  }
  return parsed;
}

function formatJob(job) {
  const startedAt = job.started_at ? Date.parse(job.started_at) : 0;
  const finishedAt = job.finished_at ? Date.parse(job.finished_at) : 0;
  const elapsed = startedAt ? `Elapsed: ${formatElapsed((finishedAt || Date.now()) - startedAt)}\n` : "";
  const header = `${formatStepName(job.step || progressStep)} · ${formatJobStatus(job.status || "")}\n${elapsed}`;
  const body = formatResult(job);
  const error = job.error ? `\nError: ${job.error}\n` : "";
  return `${header}${error}${body || progressDetail(job)}`;
}

function formatResult(result) {
  return (result.outputs || []).map(item => `$ ${item.command}\n\n${item.output || ""}`).join("\n\n---\n\n");
}

function latestOutput(job) {
  const outputs = job.outputs || [];
  if (!outputs.length) return "";
  return outputs[outputs.length - 1].output || "";
}

function formatStepName(step) {
  return replaceAllText(step || "Task", "_", " ");
}

function replaceAllText(value, search, replacement) {
  return String(value).split(search).join(replacement);
}

function formatJobStatus(status) {
  if (status === "pass") return "complete";
  if (status === "fail") return "failed";
  if (status === "cancelled") return "stopped";
  if (status === "cancelling") return "stopping";
  if (status === "queued") return "queued";
  if (status === "starting") return "starting";
  return "running";
}

function formatElapsed(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const two = value => String(value).padStart(2, "0");
  return hours ? `${hours}:${two(minutes)}:${two(seconds)}` : `${two(minutes)}:${two(seconds)}`;
}

function sleep(milliseconds) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

function scheduleActiveStatusRefresh(payload) {
  if (statusRefreshTimer) {
    window.clearTimeout(statusRefreshTimer);
    statusRefreshTimer = null;
  }
  const activeJobs = (payload.active_jobs || []).filter(job => ["queued", "running"].includes(job.status || "running"));
  if (!activeJobs.length) return;
  const steps = new Set(expandedStatusSteps());
  for (const job of activeJobs) {
    const owner = ownerStep(job.step);
    if (owner) steps.add(owner);
  }
  statusRefreshTimer = window.setTimeout(function() {
    statusRefreshTimer = null;
    loadStatus(Array.from(steps)).catch(function(error) {
      document.getElementById("output").textContent = error.message;
    });
  }, ACTIVE_STATUS_REFRESH_MS);
}

function activeButton() {
  return document.activeElement instanceof HTMLButtonElement ? document.activeElement : null;
}

function markButtonRunning(button, runningNow) {
  if (runningNow) {
    for (const other of document.querySelectorAll(".button.is-running")) {
      if (other === button) continue;
      other.classList.remove("is-running");
      other.removeAttribute("aria-busy");
    }
  }
  if (!button) return;
  button.classList.toggle("is-running", runningNow);
  if (runningNow) {
    button.setAttribute("aria-busy", "true");
  } else {
    button.removeAttribute("aria-busy");
  }
}

function setButtons(disabled, activeButtonElement = null) {
  for (const button of document.querySelectorAll("button")) {
    button.disabled = disabled;
  }
  if (activeButtonElement) {
    activeButtonElement.disabled = false;
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[char]));
}

function escapeJs(value) {
  return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

for (const id of ["workingCopyStartDate", "workingCopyEndDate"]) {
  const input = document.getElementById(id);
  if (input) input.addEventListener("change", refreshWorkingCopyReadiness);
}

applyInitialWorkflowExpansion();
loadStatus(["workflow_overview"])
  .then(function() {
    if (initialOwnerStep) return loadStatus([initialOwnerStep]);
    return null;
  })
  .catch(function(error) {
    document.getElementById("output").textContent = error.message;
  });
</script>
</body>
</html>
"""


def _patch_control_html(html: str) -> str:
    if 'data-step="media_dedupe_review"' in html:
        return html
    html = html.replace('      <a class="button" href="/dedupe">Open media dedupe review</a>\n', "")
    section = """
    <section class="panel workflow-step" data-step="media_dedupe_review">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 4</div>
          <h2>Media dedupe review</h2>
        </div>
        <div class="step-status-bar" data-step-status="media_dedupe_review">Review candidate duplicates</div>
        <button class="button small step-toggle" data-step-toggle="media_dedupe_review" onclick="toggleWorkflowStep('media_dedupe_review')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">One-by-one side-by-side review for photo and video duplicate candidates. Decisions are recorded only; no files are deleted, moved, or copied.</div>
      <div class="button-row">
        <a class="button primary" href="/dedupe">Open dedupe review</a>
      </div>
      <div class="step-result" data-step-result="media_dedupe_review"></div>
      <div class="step-history" data-step-history="media_dedupe_review"></div>
      </div>
    </section>

"""
    marker = '    <section class="panel workflow-step" data-step="broad_visual_match">'
    html = html.replace(marker, section + marker, 1)
    replacements = {
        '<div class="step-kicker">Step 4</div>\n          <h2>Broad Visual Match</h2>': '<div class="step-kicker">Step 5</div>\n          <h2>Broad Visual Match</h2>',
        '<div class="step-kicker">Step 4B</div>\n          <h2>No-Date Visual Match</h2>': '<div class="step-kicker">Step 5B</div>\n          <h2>No-Date Visual Match</h2>',
        '<div class="step-kicker">Step 5</div>\n          <h2>Crop confirmation</h2>': '<div class="step-kicker">Step 6</div>\n          <h2>Crop confirmation</h2>',
        '<div class="step-kicker">Step 6</div>\n          <h2>Working photo copies</h2>': '<div class="step-kicker">Step 7</div>\n          <h2>Working photo copies</h2>',
        '<div class="step-kicker">Step 7</div>\n          <h2>People / face tagging</h2>': '<div class="step-kicker">Step 8</div>\n          <h2>People / face tagging</h2>',
        '<div class="step-kicker">Step 8</div>\n          <h2>Diary enrichment</h2>': '<div class="step-kicker">Step 9</div>\n          <h2>Diary enrichment</h2>',
        '<div class="step-kicker">Step 9</div>\n          <h2>Diarium import package</h2>': '<div class="step-kicker">Step 10</div>\n          <h2>Diarium import package</h2>',
    }
    for old, new in replacements.items():
        html = html.replace(old, new, 1)
    return html

CONTROL_HTML = _patch_control_html(CONTROL_HTML)

if __name__ == "__main__":
    raise SystemExit(main())
