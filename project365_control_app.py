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
        self.history = _load_control_run_history(CONTROL_RUN_HISTORY)

    def status(self, steps: list[str] | None = None) -> dict[str, Any]:
        if steps is not None:
            return self._scoped_status(steps)
        diarium_package = _diarium_package_status(DIARIUM_IMPORT_BATCH_DIR)
        diarium_local = _diarium_local_status(DIARIUM_DB_PATH)
        status = {
            "paths": {
                "project365_zips": _path_status(PROJECT365_PRO_EXPORT_ZIPS_DIR),
                "original_photos": _path_status(ORIGINAL_PHOTOS_ROOT),
                "canonical_db": _path_status(CANONICAL_ROOT / "canonical.db"),
                "original_queue": _path_status(ORIGINAL_QUEUE),
                "original_batch_plan": _path_status(ORIGINAL_BATCH_PLAN),
                "original_search_attempts": _path_status(ORIGINAL_SEARCH_ATTEMPTS),
                "photo_library_index": _path_status(PHOTO_LIBRARY_INDEX),
                "tag_queue": _path_status(TAG_QUEUE),
                "digikam_people_report": _path_status(DIGIKAM_PEOPLE_REPORT),
            },
            "database": _database_status(CANONICAL_ROOT / "canonical.db"),
            "photo_library_index": _photo_library_index_status(PHOTO_LIBRARY_INDEX),
            "original_queue": _queue_status(ORIGINAL_QUEUE),
            "crop_confirmation": self.crop_confirmation_status(),
            "original_remainder_overview": _remainder_overview(ORIGINAL_UNCLEAR_GROUPS),
            "original_batch_plan": _batch_plan_status(ORIGINAL_BATCH_PLAN, ORIGINAL_SEARCH_ATTEMPTS),
            "original_search_attempts": _search_attempt_status(ORIGINAL_SEARCH_ATTEMPTS),
            "diarium_package": diarium_package,
            "diarium_local": diarium_local,
            "diarium_import_verification": _diarium_import_verification(
                diarium_package,
                diarium_local,
            ),
            "history": self.history[-20:],
            "active_jobs": self._active_jobs(),
        }
        status["top_metrics"] = _top_metrics(
            status["database"],
            status["photo_library_index"],
        )
        status["workflow_history"] = _workflow_history(self.history)
        return status

    def _scoped_status(self, steps: list[str]) -> dict[str, Any]:
        requested = {
            owner
            for owner in (_owner_step(str(step).strip()) for step in steps)
            if owner in WORKFLOW_STEPS
        }
        status: dict[str, Any] = {
            "top_metrics": _initial_top_metrics(),
            "history": self.history[-20:],
            "active_jobs": self._active_jobs(),
            "workflow_history": _workflow_history(self.history),
        }
        paths: dict[str, Any] = {}
        if "import_zips" in requested:
            paths["project365_zips"] = _path_status(PROJECT365_PRO_EXPORT_ZIPS_DIR)
        if "build_photo_index" in requested:
            paths["photo_library_index"] = _path_status(PHOTO_LIBRARY_INDEX)
            status["photo_library_index"] = _photo_library_index_status(PHOTO_LIBRARY_INDEX)
        if "match_easy_originals" in requested:
            status["photo_library_index"] = _photo_library_index_status(PHOTO_LIBRARY_INDEX)
            status["original_queue"] = _queue_status(ORIGINAL_QUEUE)
            status["original_remainder_overview"] = _remainder_overview(ORIGINAL_UNCLEAR_GROUPS)
            status["original_batch_plan"] = _batch_plan_status(ORIGINAL_BATCH_PLAN, ORIGINAL_SEARCH_ATTEMPTS)
            status["original_search_attempts"] = _search_attempt_status(ORIGINAL_SEARCH_ATTEMPTS)
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
        return status

    def crop_confirmation_status(self) -> dict[str, Any]:
        try:
            picker_state = self.picker_state()
            entries = picker_state.crop_entries(crop_filter="all")
            pending_commits = picker_state.pending_crop_commits()
        except Exception as exc:  # noqa: BLE001 - status panel should stay readable if crop state is unavailable.
            return {
                "exists": False,
                "error": str(exc),
                "total_count": 0,
                "with_crop_count": 0,
                "queued_count": 0,
                "pending_commit_count": 0,
            }
        with_crop_count = sum(1 for entry in entries if entry.get("crop_has_crop"))
        queued_count = max(0, len(entries) - with_crop_count)
        return {
            "exists": True,
            "total_count": len(entries),
            "with_crop_count": with_crop_count,
            "queued_count": queued_count,
            "pending_commit_count": int(pending_commits.get("pending_count") or 0),
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

    def _active_jobs(self) -> list[dict[str, Any]]:
        with self._job_lock:
            return [
                dict(job)
                for job in self.jobs.values()
                if job.get("status") in {"queued", "running"}
            ]

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
            return self._active_photo_index_folder

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
    wait_timeout = 1 if cancel_event and cancel_event.is_set() else 5
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
    if step in {"build_photo_index", "refresh_photo_index_metadata"}:
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
        if _has_targeted_search_scope(payload):
            command.append("--merge-existing-queue")
        return [command]
    if step == "build_photo_index":
        roots = []
        for root in payload.get("search_roots", []):
            root_text = str(root).strip()
            if root_text and root_text not in roots:
                roots.append(root_text)
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
        roots = _photo_library_index_roots(PHOTO_LIBRARY_INDEX)
        if not roots:
            raise ValueError("No existing photo index roots found. Choose folders and build the photo index first.")
        command = [
            python,
            "project365_photo_library_index.py",
            "--canonical-root",
            str(CANONICAL_ROOT),
        ]
        for root in roots:
            command.extend(["--index-root", root])
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
        return [
            [
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
        ]
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


def _workflow_snapshot(step: str = "", include_details: bool = True) -> dict[str, Any]:
    snapshot = {
        "database": _database_snapshot(CANONICAL_ROOT / "canonical.db"),
    }
    if step in {"build_photo_index", "refresh_photo_index_metadata", ""}:
        snapshot["photo_index"] = _photo_index_snapshot(PHOTO_LIBRARY_INDEX)
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
        "error": _safe_error(error, outputs),
    }


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
    if step == "generate_derivatives":
        return [
            {"label": "Generated working copies", "value": parsed.get("Generated", "0")},
            {"label": "Skipped unchanged copies", "value": parsed.get("Skipped", "0")},
            {"label": "Not ready for export", "value": parsed.get("Not ready", "0")},
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
            {"label": "Photo readiness", "value": "ready" if package_after.get("photo_ready") else "attention"},
        ]
    return [common["entries"], common["media_records"]]


def _summary_deltas(step: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    db_before = before.get("database", {})
    db_after = after.get("database", {})
    index_before = before.get("photo_index", {})
    index_after = after.get("photo_index", {})
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
    }
    if step == "import_zips":
        return {key: values[key] for key in ("diary_entries", "unique_diary_days", "source_links", "media_records")}
    if step in {"build_photo_index", "refresh_photo_index_metadata"}:
        return {key: values[key] for key in ("photo_index_files", "photo_index_dates")}
    if step in {"face_tagging", "import_digikam_people"}:
        return {key: values[key] for key in ("people", "tags")}
    if step == "generate_derivatives":
        return {"diarium_derivatives": values["diarium_derivatives"]}
    return values


def _summary_scope(step: str, payload: dict[str, Any]) -> list[str]:
    if step == "import_zips":
        return [f"ZIP folder: {PROJECT365_PRO_EXPORT_ZIPS_DIR}"]
    if step == "build_photo_index":
        roots = payload.get("search_roots") or [str(SOURCE_DATA_ROOT)]
        reset = "replace existing index" if payload.get("reset_photo_index") else "incremental"
        return [f"Folders: {'; '.join(str(root) for root in roots)}", f"Mode: {reset}"]
    if step == "refresh_photo_index_metadata":
        return ["Existing indexed folders"]
    if step == "match_easy_originals":
        folder = str(payload.get("photo_index_folder") or "").strip()
        return [f"Photo index folder: {folder}" if folder else "Photo index folder: all indexed folders"]
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
        "search_originals": "Original-photo search",
        "generate_derivatives": "Working photo copies",
        "face_tagging": "Build tag queue",
        "import_digikam_people": "Import digiKam suggestions",
        "generate_diarium_package": "Diarium import package",
    }
    return titles.get(step, step.replace("_", " "))


def _top_metrics(database: dict[str, Any], photo_index: dict[str, Any]) -> dict[str, Any]:
    project365_total = int(database.get("project365_entry_count") or 0)
    missing_photos = int(database.get("project365_missing_photo_count") or 0)
    without_identified_original = int(database.get("without_identified_original_count") or 0)
    return {
        "unique_diary_days": int(database.get("unique_day_count") or 0),
        "diary_entries": int(database.get("entry_count") or 0),
        "photo_index_files": int(photo_index.get("file_count") or 0),
        "project365_entries": project365_total,
        "missing_photos": missing_photos,
        "missing_photos_percent": _percentage(missing_photos, project365_total),
        "without_identified_original": without_identified_original,
        "without_identified_original_percent": _percentage(
            without_identified_original,
            project365_total,
        ),
    }


def _workflow_history(history: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in reversed(history):
        step = str(record.get("step") or "")
        if not step:
            continue
        grouped.setdefault(step, [])
        if len(grouped[step]) < 5:
            grouped[step].append(_persistable_history_record(record))
    return grouped


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
        "project365_missing_photo_count": project365_photo_gaps["missing_photos"],
        "project365_missing_photo_percent": _percentage(
            project365_photo_gaps["missing_photos"],
            project365_entry_count,
        ),
        "identified_original_count": project365_photo_gaps["identified_originals"],
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
        "source_counts": [dict(row) for row in source_rows],
        "media": [dict(row) for row in rows],
    }


def _project365_photo_gap_counts(connection: sqlite3.Connection) -> dict[str, int]:
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(media_assets)").fetchall()
    }
    if "entry_id" not in columns:
        return {
            "missing_photos": 0,
            "identified_originals": 0,
            "without_identified_original": 0,
        }
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS project365_entries,
            SUM(
                CASE WHEN NOT EXISTS (
                    SELECT 1
                    FROM media_assets AS export_media
                    WHERE export_media.entry_id = entries.id
                        AND export_media.role = 'project365_export_png'
                ) THEN 1 ELSE 0 END
            ) AS missing_photos,
            SUM(
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM media_assets AS original_media
                    WHERE original_media.entry_id = entries.id
                        AND original_media.role = 'external_original_reference'
                        AND original_media.review_status = 'confirmed'
                ) THEN 1 ELSE 0 END
            ) AS identified_originals
        FROM entries
        WHERE entries.source_app = 'project365'
        """
    ).fetchone()
    project365_entries = int(row["project365_entries"] or 0)
    identified_originals = int(row["identified_originals"] or 0)
    return {
        "missing_photos": int(row["missing_photos"] or 0),
        "identified_originals": identified_originals,
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


def _photo_library_index_status(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {"exists": False}
    connection = sqlite3.connect(index_path)
    try:
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
        gps_coordinate_count = (
            connection.execute(
                """
                SELECT COUNT(*)
                FROM photo_library_files
                WHERE gps_latitude IS NOT NULL
                    AND gps_longitude IS NOT NULL
                """
            ).fetchone()[0]
            if {"gps_latitude", "gps_longitude"}.issubset(file_columns)
            else 0
        )
        roots = [
            row[0]
            for row in connection.execute(
                "SELECT root FROM photo_library_files GROUP BY root ORDER BY root"
            ).fetchall()
        ]
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
    finally:
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
    return [str(row[0]) for row in rows if str(row[0]).strip()]


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
                elif parsed.path in {"/crop", "/crop/"}:
                    self._send_html(_embedded_crop_html())
                elif parsed.path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                elif parsed.path == "/api/status":
                    query = urllib.parse.parse_qs(parsed.query)
                    steps = query.get("step")
                    self._send_json(state.status(steps if steps else None))
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
                if parsed.path.startswith("/api/job/") and parsed.path.endswith("/cancel"):
                    job_id = parsed.path.removeprefix("/api/job/").removesuffix("/cancel")
                    try:
                        self._send_json(state.cancel_job(job_id))
                    except KeyError as exc:
                        self._send_error(HTTPStatus.NOT_FOUND, str(exc))
                    return
                if parsed.path == "/api/crop-estimate-batch":
                    self._send_json(state.picker_state().start_crop_estimate_batch())
                    return
                if parsed.path == "/picker/api/decision":
                    payload = self._read_json()
                    detail = state.picker_state().save_decision(
                        entry_id=str(payload.get("entry_id", "")),
                        candidate_path=str(payload.get("candidate_path", "")),
                        decision=str(payload.get("decision", "")),
                        notes=str(payload.get("notes", "")),
                        include_candidates=False,
                    )
                    self._send_json(detail)
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
                    self._send_json(state.picker_state().start_crop_estimate_batch())
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
                if parsed.path == "/picker/api/apply-decisions":
                    self._send_json(state.picker_state().apply_decisions())
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
    "crop_confirmation",
    "generate_derivatives",
    "face_tagging",
    "generate_diarium_package",
)

STEP_OWNER = {
    "refresh_photo_index_metadata": "build_photo_index",
    "search_originals": "match_easy_originals",
    "apply_original_decisions": "match_easy_originals",
    "import_digikam_people": "face_tagging",
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
        .replace(
            "__INITIAL_MISSING_PHOTOS__",
            f'{metrics["missing_photos"]} · {metrics["missing_photos_percent"]}%',
        )
        .replace(
            "__INITIAL_WITHOUT_IDENTIFIED_ORIGINAL__",
            (
                f'{metrics["without_identified_original"]} · '
                f'{metrics["without_identified_original_percent"]}%'
            ),
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


def _initial_top_metrics() -> dict[str, Any]:
    canonical_db = CANONICAL_ROOT / "canonical.db"
    photo_gaps = _initial_project365_photo_gap_counts(canonical_db)
    project365_entries = photo_gaps["project365_entries"]
    return {
        "unique_diary_days": _initial_unique_day_count(canonical_db),
        "diary_entries": _initial_entry_count(canonical_db),
        "photo_index_files": _initial_photo_index_file_count(PHOTO_LIBRARY_INDEX),
        "project365_entries": project365_entries,
        "missing_photos": photo_gaps["missing_photos"],
        "missing_photos_percent": _percentage(photo_gaps["missing_photos"], project365_entries),
        "without_identified_original": photo_gaps["without_identified_original"],
        "without_identified_original_percent": _percentage(
            photo_gaps["without_identified_original"],
            project365_entries,
        ),
    }


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
                "missing_photos": full_counts["missing_photos"],
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
        'tell application "Finder" to activate\n'
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
        rows = connection.execute(
            """
            SELECT path, root
            FROM photo_library_files
            WHERE filename = ?
                AND byte_size = ?
            ORDER BY path
            """,
            (filename, byte_size),
        ).fetchall()
    finally:
        connection.close()
    matches = []
    for path_text, root_text in rows:
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            continue
        try:
            if _sha256_file(path) == sha256:
                root = Path(root_text)
                matches.append((path, root if root.exists() and root.is_dir() else path.parent))
        except OSError:
            continue
    if not matches:
        raise ValueError("Chosen file was not found in the photo index. Pick a file from an indexed folder.")
    indexed_roots = sorted({str(root) for _, root in matches})
    if len(indexed_roots) > 1:
        raise ValueError(
            "Chosen file appears in multiple indexed roots. Pick a file unique to the target folder."
        )
    return {"path": indexed_roots[0], "matched_file": str(matches[0][0])}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Project365 workflow control app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--picker-url", default="/picker")
    args = parser.parse_args()
    config = ControlConfig(host=args.host, port=args.port, picker_url=args.picker_url)
    server = serve_control_app(config)
    print(f"Project365 control app: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


CONTROL_HTML = """<!doctype html>
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
  transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease, transform 80ms ease;
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
.field {
  display: grid;
  gap: 5px;
}
.field label {
  font-size: 12px;
  color: var(--muted);
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
.step-list {
  margin: 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 13px;
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
    <div class="metric-card"><span>Without identified original</span><strong id="metricWithoutIdentifiedOriginal">__INITIAL_WITHOUT_IDENTIFIED_ORIGINAL__</strong></div>
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
        <div class="subtle">Leave blank to scan Source Data and all of its subfolders.</div>
      </div>
      <label class="checkbox-line">
        <input id="resetPhotoIndex" type="checkbox">
        Replace existing photo index
      </label>
      <button class="button primary" onclick="buildPhotoIndex()">Build photo index</button>
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

    <section class="panel workflow-step" data-step="crop_confirmation">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 4</div>
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
        <button id="openCropConfirmationButton" class="button" type="button" onclick="openCropConfirmation()">Open crop confirmation</button>
        <span id="cropEstimateBatchStatus" class="inline-status" role="status" aria-live="polite"></span>
      </div>
      <div class="step-result" data-step-result="crop_confirmation"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="generate_derivatives">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 5</div>
          <h2>Working photo copies</h2>
        </div>
        <div class="step-status-bar" data-step-status="generate_derivatives">No runs yet</div>
        <button class="button small step-toggle" data-step-toggle="generate_derivatives" onclick="toggleWorkflowStep('generate_derivatives')" aria-expanded="false">Open</button>
      </div>
      <div class="workflow-step-body">
      <div class="subtle">Creates date-organized JPEG working copies from confirmed originals where available. Local face-recognition tools such as digiKam can scan this collection.</div>
      <div id="workingCopyReadiness" class="result-grid"></div>
      <button class="button primary" onclick="runStep('generate_derivatives')">Generate working copies</button>
      <div class="step-result" data-step-result="generate_derivatives"></div>
      <div class="step-history" data-step-history="generate_derivatives"></div>
      </div>
    </section>

    <section class="panel workflow-step" data-step="face_tagging">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 6</div>
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

    <section class="panel workflow-step" data-step="generate_diarium_package">
      <div class="workflow-step-header">
        <div>
          <div class="step-kicker">Step 7</div>
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
const STEP_OWNER = {
  refresh_photo_index_metadata: "build_photo_index",
  search_originals: "match_easy_originals",
  apply_original_decisions: "match_easy_originals",
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

function statusUrlFor(steps) {
  const requestedSteps = Array.isArray(steps) ? steps.filter(Boolean) : expandedStatusSteps();
  if (!requestedSteps.length) return "/api/status";
  const params = new URLSearchParams();
  for (const step of requestedSteps) params.append("step", step);
  return `/api/status?${params.toString()}`;
}

function expandedStatusSteps() {
  return Array.from(manuallyExpandedSteps).filter(step => Boolean(workflowCard(step)));
}

async function loadStatus(steps) {
  try {
    const payload = await fetchJson(statusUrlFor(steps));
    lastStatus = payload;
    setStatusLoadError("");
    renderStatus(payload);
    renderWorkingCopyReadiness(payload);
    renderPhotoIndexBox(payload);
    renderEasyMatchBox(payload);
    renderCropConfirmationBox(payload);
    renderBatchOverview(payload);
    renderAttemptOverview(payload);
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
  target.innerHTML = `
    <div class="result-metric"><span>Ready for export</span><strong>${ready}</strong></div>
    <div class="result-metric"><span>Source chosen</span><strong>${sources}</strong></div>
    <div class="result-metric"><span>Needs crop</span><strong>${notReady}</strong></div>
  `;
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
    <div class="result-metric"><span>Queued without crop</span><strong>${crop.queued_count || 0}</strong></div>
  `;
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
  setText(
    "metricWithoutIdentifiedOriginal",
    formatCountPercent(metrics.without_identified_original || 0, project365Total)
  );
  setText("metricPhotoIndexFiles", metrics.photo_index_files || 0);
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
  return `<span class="status-pill">Crop review</span>${crop.with_crop_count || 0} with crop information · ${crop.queued_count || 0} queued without crop`;
}

function openCropConfirmation() {
  window.location.assign("/crop");
}

async function startCropEstimateBatch() {
  const button = document.getElementById("estimateCropBatchButton");
  if (!button || button.dataset.jobId) return;
  button.disabled = true;
  markButtonRunning(button, true);
  setCropEstimateBatchStatus("Starting batch estimate.");
  try {
    const job = await fetchJson("/api/crop-estimate-batch", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({})
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
  const firstError = Array.isArray(job.errors) && job.errors.length ? job.errors[0] : null;
  const errorDetail = firstError
    ? ` · First error: ${firstError.entry_id || "item"} ${firstError.error || "estimate failed"}`
    : "";
  const label = runningNow ? "Working" : job.status === "pass" ? "Complete" : "Failed";
  setCropEstimateBatchStatus(
    `${label} · ${processed}/${total} checked · ${estimated} estimated · ${skipped} skipped · ${failed} failed${current}${errorDetail}`
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
  const metrics = (summary.metrics || []).map(metric => `
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
  const error = summary.error ? `<div class="status-note bad"><strong>Error:</strong> ${escapeHtml(summary.error)}</div>` : "";
  return `
    ${scope}
    <div class="result-grid">${metrics}</div>
    ${archives}
    ${zero}
    ${error}
  `;
}

function formatActiveWorkflowResult(job) {
  const startedAt = Date.parse(job.started_at || new Date().toISOString());
  const elapsed = formatElapsed(Date.now() - startedAt);
  return `
    <div class="run-progress status-${escapeHtml(job.status || "running")}">
      <div class="run-progress-header">
        <div class="run-progress-main">
          <span class="spinner"></span>
          <strong>${escapeHtml(formatStepName(job.step || progressStep))} · ${escapeHtml(formatJobStatus(job.status || "running"))}</strong>
        </div>
        <div class="run-progress-actions">
          <span class="subtle">${escapeHtml(elapsed)}</span>
          <button class="button danger small" onclick="cancelActiveJob()" ${activeJobId ? "" : "hidden"}>Stop</button>
        </div>
      </div>
      <div class="progress-track"><div class="progress-bar"></div></div>
      <div class="subtle">${escapeHtml(progressDetail(job))}</div>
    </div>
  `;
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
    const metrics = ((record.summary || {}).metrics || []).slice(0, 2)
      .map(metric => `${metric.label}: ${metric.value}`)
      .join(" · ");
    return `<div><span>${escapeHtml(formatShortDateTime(record.finished_at || record.started_at))}</span><strong>${escapeHtml(formatJobStatus(record.status || ""))}${metrics ? " · " + escapeHtml(metrics) : ""}</strong></div>`;
  }).join("");
  return `<div class="run-list">${rows}</div>`;
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

function formatShortDateTime(value) {
  if (!value) return "unknown time";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
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
  return `${batch.latest_search_finished_at || "unknown time"} · ${candidates} candidates`;
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
      <td>${escapeHtml(attempt.finished_at || "")}</td>
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
  document.getElementById("output").textContent = "Opening folder picker...";
  try {
    const payload = await fetchJson("/api/choose-folder");
    if (payload.path) {
      if (mode === "replace") {
        document.getElementById(targetInputId).value = payload.path;
      } else {
        appendSearchRoot(payload.path, targetInputId);
      }
      document.getElementById("output").textContent = `Added folder: ${payload.path}`;
    } else {
      document.getElementById("output").textContent = "Folder selection canceled.";
    }
  } catch (error) {
    const message = `Folder picker failed: ${error.message}`;
    document.getElementById("output").textContent = message;
    if (targetInputId === "easyMatchIndexFolder") {
      setStepMessage("match_easy_originals", message, "error");
    }
  } finally {
    markButtonRunning(trigger, false);
  }
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
  const folder = document.getElementById("easyMatchIndexFolder").value.trim();
  if (limitToFolder && !folder) {
    setStepMessage("match_easy_originals", "Choose an indexed folder before running filtered easy matches.", "fail");
    return;
  }
  await runStep("match_easy_originals", {
    limit_to_photo_index_folder: limitToFolder,
    photo_index_folder: limitToFolder ? folder : ""
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
  if (!searchRoots.length) setStepMessage("build_photo_index", "Scanning Source Data...", "running");
  await runStep("build_photo_index", {
    search_roots: searchRoots,
    reset_photo_index: resetPhotoIndex,
    reset_confirmation: resetPhotoIndex ? "replace-photo-index" : ""
  });
}

async function refreshPhotoIndexMetadata() {
  setStepMessage("refresh_photo_index_metadata", "Refreshing metadata for existing indexed folders...", "running");
  await runStep("refresh_photo_index_metadata");
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
    .map(value => value.trim())
    .filter(Boolean);
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

async function runStep(step, extra = {}) {
  if (running) return;
  const trigger = activeButton();
  running = true;
  setButtons(true, trigger);
  markButtonRunning(trigger, true);
  startProgress(step);
  setStepMessage(step, "Starting...", "running");
  document.getElementById("output").textContent = `Starting ${step}...`;
  try {
    const startedJob = await fetchJson("/api/run", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify(Object.assign({step: step}, extra || {}))
    });
    const result = startedJob.job_id ? await waitForJob(startedJob.job_id) : startedJob;
    finishProgress(result);
    setStepMessage(
      step,
      result.status === "pass" ? stepCompletionMessage(step) : (result.error || "Task failed."),
      result.status === "pass" ? "" : "error"
    );
    if (result.status === "pass" && step === "build_photo_index") {
      document.getElementById("indexRoots").value = "";
    }
    await loadStatus();
  } catch (error) {
    stopProgress();
    setStepMessage(step, error.message, "error");
    document.getElementById("output").textContent = error.message;
  } finally {
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
    import_digikam_people: "digikamPeopleMessage"
  };
  const target = document.getElementById(targetIds[step] || "");
  if (!target) return;
  target.textContent = message;
  target.className = `inline-status ${state}`.trim();
}

function stepCompletionMessage(step) {
  if (step === "build_photo_index") return "Photo index completed.";
  if (step === "refresh_photo_index_metadata") return "Photo index metadata refreshed.";
  if (step === "match_easy_originals") return "Match search completed. Refreshing results...";
  if (step === "import_digikam_people") return "digiKam suggestions imported. Tag queue refreshed.";
  return "Task completed.";
}

function startProgress(step) {
  progressStep = step;
  progressStartedAt = Date.now();
  activeJobId = "";
  manuallyExpandedSteps.add(ownerStep(step));
  updateProgress({step, status: "starting", started_at: new Date().toISOString(), outputs: []});
  progressTimer = setInterval(() => {
    updateProgress({step: progressStep, status: "running", started_at: new Date(progressStartedAt).toISOString(), outputs: []});
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
  if (!activeJobId) return;
  const stopButton = document.querySelector(".run-progress button");
  if (stopButton) stopButton.disabled = true;
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
  const latest = latestOutput(job);
  if (latest) {
    const lines = latest.trim().split("\\n").filter(Boolean);
    return lines[lines.length - 1] || "Working...";
  }
  if (job.current_command) return "Running command...";
  if (job.status === "queued") return "Waiting for the current task to finish.";
  if (job.status === "cancelling") return "Stopping...";
  if (job.status === "cancelled") return "Stopped.";
  if (job.status === "pass") return "Finished.";
  if (job.status === "fail") return job.error || "Failed.";
  return "Working...";
}

function formatJob(job) {
  const startedAt = job.started_at ? Date.parse(job.started_at) : 0;
  const finishedAt = job.finished_at ? Date.parse(job.finished_at) : 0;
  const elapsed = startedAt ? `Elapsed: ${formatElapsed((finishedAt || Date.now()) - startedAt)}\\n` : "";
  const header = `${formatStepName(job.step || progressStep)} · ${formatJobStatus(job.status || "")}\\n${elapsed}`;
  const body = formatResult(job);
  const error = job.error ? `\\nError: ${job.error}\\n` : "";
  return `${header}${error}${body || progressDetail(job)}`;
}

function formatResult(result) {
  return (result.outputs || []).map(item => `$ ${item.command}\\n\\n${item.output || ""}`).join("\\n\\n---\\n\\n");
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

function activeButton() {
  return document.activeElement instanceof HTMLButtonElement ? document.activeElement : null;
}

function markButtonRunning(button, runningNow) {
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

applyInitialWorkflowExpansion();
if (initialOwnerStep) {
  loadStatus([initialOwnerStep]).catch(function(error) {
    document.getElementById("output").textContent = error.message;
  });
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
