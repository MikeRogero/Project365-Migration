#!/usr/bin/env python3
"""Start, stop, and inspect the local Project365 control-panel server."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
SERVER_SCRIPTS = ("project365_control_app.py", "project365_original_picker.py")


@dataclass(frozen=True)
class ServiceConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    runtime_dir: Path = PROJECT_ROOT / "Project365Canonical" / "runtime"

    @property
    def pid_path(self) -> Path:
        return self.runtime_dir / "project365_control_app.pid.json"

    @property
    def log_path(self) -> Path:
        return self.runtime_dir / "project365_control_app.log"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the local Project365 control panel.")
    parser.add_argument("command", choices=["start", "stop", "status"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="Open the control panel in the default browser after start.")
    args = parser.parse_args()

    config = ServiceConfig(host=args.host, port=args.port)
    if args.command == "start":
        result = start_control_panel(config)
        print(_human_status(result))
        if args.open and result.get("url"):
            subprocess.run(["open", str(result["url"])], check=False)
        return 0
    if args.command == "stop":
        result = stop_project365_servers(config)
        print(_human_status(result))
        return 0
    result = service_status(config)
    print(_human_status(result))
    return 0 if result.get("running") else 1


def start_control_panel(config: ServiceConfig) -> dict[str, Any]:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    managed = _pid_record(config.pid_path)
    if managed and _pid_running(int(managed["pid"])):
        return {
            "status": "running",
            "running": True,
            "pid": int(managed["pid"]),
            "url": managed.get("url", config.url),
            "message": "Project365 control panel is already running.",
        }

    existing = _project_server_pids(port=config.port, host=config.host)
    if existing:
        pid = existing[0]
        _write_pid_record(config, pid)
        return {
            "status": "running",
            "running": True,
            "pid": pid,
            "url": config.url,
            "message": "Project365 control panel was already running and is now tracked.",
        }

    command = _control_panel_command(config)
    with config.log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n\n=== Project365 control panel start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    try:
        _wait_for_control_panel(config, process)
    except RuntimeError:
        _remove_pid_record(config.pid_path)
        raise
    _write_pid_record(config, process.pid)
    return {
        "status": "started",
        "running": True,
        "pid": process.pid,
        "url": config.url,
        "log": str(config.log_path),
        "message": "Project365 control panel started.",
    }


def stop_project365_servers(config: ServiceConfig) -> dict[str, Any]:
    managed = _pid_record(config.pid_path)
    managed_pid = int(managed["pid"]) if managed and _pid_running(int(managed["pid"])) else None
    unmanaged_pids = _project_server_pids(port=config.port, host=config.host, include_any_project_server=True)
    stopped: list[int] = []
    failed: list[int] = []

    if managed_pid:
        if _terminate_process_group(managed_pid):
            stopped.append(managed_pid)
        else:
            failed.append(managed_pid)

    for pid in unmanaged_pids:
        if pid == managed_pid or not _pid_running(pid):
            continue
        pids = _descendant_pids(pid)
        pids.append(pid)
        if _terminate_pids(pids):
            stopped.append(pid)
        else:
            failed.append(pid)

    _remove_pid_record(config.pid_path)
    return {
        "status": "stopped" if stopped and not failed else "not_running" if not stopped and not failed else "partial",
        "running": False,
        "stopped_pids": sorted(set(stopped)),
        "failed_pids": sorted(set(failed)),
        "message": _stop_message(stopped, failed),
    }


def service_status(config: ServiceConfig) -> dict[str, Any]:
    managed = _pid_record(config.pid_path)
    managed_pid = int(managed["pid"]) if managed and _pid_running(int(managed["pid"])) else None
    detected = _project_server_pids(port=config.port, host=config.host, include_any_project_server=True)
    running = bool(managed_pid or detected)
    return {
        "status": "running" if running else "stopped",
        "running": running,
        "pid": managed_pid,
        "detected_pids": detected,
        "url": managed.get("url", config.url) if managed else config.url,
        "log": str(config.log_path),
        "message": "Project365 control panel is running." if running else "No Project365 server process is running.",
    }


def _control_panel_command(config: ServiceConfig) -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / "project365_control_app.py"),
        "--host",
        config.host,
        "--port",
        str(config.port),
    ]


def _wait_for_control_panel(config: ServiceConfig, process: subprocess.Popen[Any], timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Project365 control panel exited early. Check {config.log_path}.")
        try:
            with urllib.request.urlopen(f"{config.url}/api/status", timeout=20):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"Project365 control panel did not respond at {config.url}. Check {config.log_path}.")


def _pid_record(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_pid_record(config: ServiceConfig, pid: int) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    config.pid_path.write_text(
        json.dumps(
            {
                "pid": pid,
                "host": config.host,
                "port": config.port,
                "url": config.url,
                "log": str(config.log_path),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _remove_pid_record(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _project_server_pids(
    port: int,
    host: str = DEFAULT_HOST,
    include_any_project_server: bool = False,
) -> list[int]:
    pids = set(_project_server_pids_from_ps(include_any_project_server=include_any_project_server))
    project365_status_port = _port_has_project365_status(host, port)
    for pid in _listening_pids(port):
        command = _process_command(pid)
        if _is_project365_server_command(command) or project365_status_port:
            pids.add(pid)
    pids.discard(os.getpid())
    return sorted(pid for pid in pids if _pid_running(pid))


def _port_has_project365_status(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=20) as response:
            payload = json.loads(response.read().decode())
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return all(key in payload for key in ("paths", "database", "photo_library_index"))


def _project_server_pids_from_ps(include_any_project_server: bool = False) -> list[int]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    pids = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if _is_project365_server_command(command, allow_standalone=include_any_project_server):
            pids.append(pid)
    return pids


def _is_project365_server_command(command: str, allow_standalone: bool = True) -> bool:
    lowered = command.lower()
    if "python" not in lowered:
        return False
    scripts = SERVER_SCRIPTS if allow_standalone else ("project365_control_app.py",)
    return any(script in command for script in scripts)


def _listening_pids(port: int) -> list[int]:
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    pids = []
    for line in completed.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            pass
    return pids


def _process_command(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip()


def _descendant_pids(pid: int) -> list[int]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            child_pid, parent_pid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent_pid, []).append(child_pid)
    descendants: list[int] = []
    pending = list(children.get(pid, []))
    while pending:
        child = pending.pop()
        descendants.append(child)
        pending.extend(children.get(child, []))
    return descendants


def _terminate_process_group(pid: int) -> bool:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return True
    if pgid == os.getpgrp():
        return _terminate_pids([pid])
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    _wait_for_exit([pid], timeout_seconds=5)
    if _pid_running(pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return True
    return _wait_for_exit([pid], timeout_seconds=5)


def _terminate_pids(pids: list[int]) -> bool:
    targets = [pid for pid in sorted(set(pids), reverse=True) if pid != os.getpid() and _pid_running(pid)]
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _wait_for_exit(targets, timeout_seconds=5)
    for pid in targets:
        if _pid_running(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return _wait_for_exit(targets, timeout_seconds=5)


def _wait_for_exit(pids: list[int], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not any(_pid_running(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return not any(_pid_running(pid) for pid in pids)


def _stop_message(stopped: list[int], failed: list[int]) -> str:
    if failed:
        return f"Stopped some Project365 processes, but could not stop: {', '.join(str(pid) for pid in failed)}"
    if stopped:
        return f"Stopped Project365 server process(es): {', '.join(str(pid) for pid in sorted(set(stopped)))}"
    return "No Project365 server process was running."


def _human_status(result: dict[str, Any]) -> str:
    lines = [str(result.get("message") or result.get("status") or "")]
    if result.get("url"):
        lines.append(f"URL: {result['url']}")
    if result.get("pid"):
        lines.append(f"PID: {result['pid']}")
    if result.get("detected_pids"):
        lines.append(f"Detected PID(s): {', '.join(str(pid) for pid in result['detected_pids'])}")
    if result.get("log"):
        lines.append(f"Log: {result['log']}")
    lines.append("Database: SQLite files only; no database server is started or stopped.")
    return "\n".join(line for line in lines if line)


if __name__ == "__main__":
    raise SystemExit(main())
