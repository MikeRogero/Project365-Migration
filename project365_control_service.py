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
DEFAULT_VIDEO_PORT = 8767
VIDEO_SERVER_SCRIPT = "project365_video_memory_review.py"
SERVER_SCRIPTS = ("project365_control_app.py", "project365_original_picker.py", VIDEO_SERVER_SCRIPT)


@dataclass(frozen=True)
class ServiceConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    video_port: int = DEFAULT_VIDEO_PORT
    runtime_dir: Path = PROJECT_ROOT / "Project365Canonical" / "runtime"

    @property
    def pid_path(self) -> Path:
        return self.runtime_dir / "project365_control_app.pid.json"

    @property
    def log_path(self) -> Path:
        return self.runtime_dir / "project365_control_app.log"

    @property
    def video_pid_path(self) -> Path:
        return self.runtime_dir / "project365_video_memory_review.pid.json"

    @property
    def video_log_path(self) -> Path:
        return self.runtime_dir / "project365_video_memory_review.log"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def video_url(self) -> str:
        return f"http://{self.host}:{self.video_port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the local Project365 control panel.")
    parser.add_argument("command", choices=["start", "stop", "status"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--video-port", type=int, default=DEFAULT_VIDEO_PORT)
    parser.add_argument("--open", action="store_true", help="Open the control panel in the default browser after start.")
    args = parser.parse_args()

    config = ServiceConfig(host=args.host, port=args.port, video_port=args.video_port)
    if args.command == "start":
        result = start_project365_services(config)
        print(_human_status(result))
        control = result["services"]["control"]
        if args.open and control.get("url"):
            subprocess.run(["open", str(control["url"])], check=False)
        return 0 if result.get("running") else 1
    if args.command == "stop":
        result = stop_project365_servers(config)
        print(_human_status(result))
        return 0
    result = project365_services_status(config)
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
            stdin=subprocess.DEVNULL,
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


def start_video_memory_review(config: ServiceConfig) -> dict[str, Any]:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    managed = _pid_record(config.video_pid_path)
    if managed and _pid_running(int(managed["pid"])):
        return {
            "status": "running",
            "running": True,
            "pid": int(managed["pid"]),
            "url": managed.get("url", config.video_url),
            "message": "Project365 video memory review is already running.",
        }

    existing = _project_server_pids(
        port=config.video_port,
        host=config.host,
        include_any_project_server=True,
        server_scripts=(VIDEO_SERVER_SCRIPT,),
        page_marker="Project365 Video Memory Review",
    )
    if existing:
        pid = existing[0]
        _write_video_pid_record(config, pid)
        return {
            "status": "running",
            "running": True,
            "pid": pid,
            "url": config.video_url,
            "message": "Project365 video memory review was already running and is now tracked.",
        }

    command = _video_memory_review_command(config)
    with config.video_log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n\n=== Project365 video memory review start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    try:
        _wait_for_video_memory_review(config, process)
    except RuntimeError:
        _terminate_process_group(process.pid)
        _remove_pid_record(config.video_pid_path)
        raise
    _write_video_pid_record(config, process.pid)
    return {
        "status": "started",
        "running": True,
        "pid": process.pid,
        "url": config.video_url,
        "log": str(config.video_log_path),
        "message": "Project365 video memory review started.",
    }


def start_project365_services(config: ServiceConfig) -> dict[str, Any]:
    control = start_control_panel(config)
    try:
        video = start_video_memory_review(config)
    except Exception:
        if control.get("status") == "started" and control.get("pid"):
            _terminate_process_group(int(control["pid"]))
            _remove_pid_record(config.pid_path)
        raise
    services = {"control": control, "video": video}
    running = all(bool(item.get("running")) for item in services.values())
    started = any(item.get("status") == "started" for item in services.values())
    return {
        "status": "started" if running and started else "running" if running else "partial",
        "running": running,
        "services": services,
        "message": "Project365 control panel and video memory review are running."
        if running
        else "Only some Project365 services are running.",
    }


def stop_project365_servers(config: ServiceConfig) -> dict[str, Any]:
    managed_pids: list[int] = []
    for path in (config.pid_path, config.video_pid_path):
        managed = _pid_record(path)
        if managed and _pid_running(int(managed["pid"])):
            managed_pids.append(int(managed["pid"]))
    unmanaged_pids = sorted(
        set(_project_server_pids(port=config.port, host=config.host, include_any_project_server=True))
        | set(
            _project_server_pids(
                port=config.video_port,
                host=config.host,
                include_any_project_server=True,
                server_scripts=(VIDEO_SERVER_SCRIPT,),
                page_marker="Project365 Video Memory Review",
            )
        )
    )
    stopped: list[int] = []
    failed: list[int] = []

    for managed_pid in managed_pids:
        if _terminate_process_group(managed_pid):
            stopped.append(managed_pid)
        else:
            failed.append(managed_pid)

    for pid in unmanaged_pids:
        if pid in managed_pids or not _pid_running(pid):
            continue
        pids = _descendant_pids(pid)
        pids.append(pid)
        if _terminate_pids(pids):
            stopped.append(pid)
        else:
            failed.append(pid)

    _remove_pid_record(config.pid_path)
    _remove_pid_record(config.video_pid_path)
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
    detected = _project_server_pids(
        port=config.port,
        host=config.host,
        server_scripts=("project365_control_app.py",),
    )
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


def video_service_status(config: ServiceConfig) -> dict[str, Any]:
    managed = _pid_record(config.video_pid_path)
    managed_pid = int(managed["pid"]) if managed and _pid_running(int(managed["pid"])) else None
    detected = _project_server_pids(
        port=config.video_port,
        host=config.host,
        include_any_project_server=True,
        server_scripts=(VIDEO_SERVER_SCRIPT,),
        page_marker="Project365 Video Memory Review",
    )
    running = bool(managed_pid or detected)
    return {
        "status": "running" if running else "stopped",
        "running": running,
        "pid": managed_pid,
        "detected_pids": detected,
        "url": managed.get("url", config.video_url) if managed else config.video_url,
        "log": str(config.video_log_path),
        "message": "Project365 video memory review is running."
        if running
        else "Project365 video memory review is stopped.",
    }


def project365_services_status(config: ServiceConfig) -> dict[str, Any]:
    control = service_status(config)
    video = video_service_status(config)
    services = {"control": control, "video": video}
    running_count = sum(bool(item.get("running")) for item in services.values())
    return {
        "status": "running" if running_count == len(services) else "partial" if running_count else "stopped",
        "running": running_count == len(services),
        "services": services,
        "message": "Project365 control panel and video memory review are running."
        if running_count == len(services)
        else "Only some Project365 services are running."
        if running_count
        else "No Project365 service is running.",
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


def _video_memory_review_command(config: ServiceConfig) -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / VIDEO_SERVER_SCRIPT),
        "--canonical-root",
        str(PROJECT_ROOT / "Project365Canonical"),
        "serve",
        "--host",
        config.host,
        "--port",
        str(config.video_port),
    ]


def _wait_for_control_panel(config: ServiceConfig, process: subprocess.Popen[Any], timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Project365 control panel exited early. Check {config.log_path}.")
        try:
            with urllib.request.urlopen(config.url, timeout=20):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"Project365 control panel did not respond at {config.url}. Check {config.log_path}.")


def _wait_for_video_memory_review(
    config: ServiceConfig,
    process: subprocess.Popen[Any],
    timeout_seconds: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Project365 video memory review exited early. Check {config.video_log_path}.")
        if _port_has_project365_page(config.host, config.video_port, "Project365 Video Memory Review"):
            return
        time.sleep(0.2)
    raise RuntimeError(
        f"Project365 video memory review did not respond at {config.video_url}. Check {config.video_log_path}."
    )


def _pid_record(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_pid_record(config: ServiceConfig, pid: int) -> None:
    _write_service_pid_record(config.pid_path, pid, config.host, config.port, config.url, config.log_path)


def _write_video_pid_record(config: ServiceConfig, pid: int) -> None:
    _write_service_pid_record(
        config.video_pid_path,
        pid,
        config.host,
        config.video_port,
        config.video_url,
        config.video_log_path,
    )


def _write_service_pid_record(path: Path, pid: int, host: str, port: int, url: str, log_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "host": host,
                "port": port,
                "url": url,
                "log": str(log_path),
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
    server_scripts: tuple[str, ...] | None = None,
    page_marker: str = "Project365 Control",
) -> list[int]:
    pids = set(
        _project_server_pids_from_ps(
            include_any_project_server=include_any_project_server,
            server_scripts=server_scripts,
        )
    )
    project365_status_port = _port_has_project365_page(host, port, page_marker)
    for pid in _listening_pids(port):
        command = _process_command(pid)
        if _is_project365_server_command(command, server_scripts=server_scripts) or project365_status_port:
            pids.add(pid)
    pids.discard(os.getpid())
    return sorted(pid for pid in pids if _pid_running(pid))


def _port_has_project365_status(host: str, port: int) -> bool:
    return _port_has_project365_page(host, port, "Project365 Control")


def _port_has_project365_page(host: str, port: int, marker: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}", timeout=20) as response:
            payload = response.read(4096).decode(errors="replace")
    except (OSError, urllib.error.URLError):
        return False
    return marker in payload


def _project_server_pids_from_ps(
    include_any_project_server: bool = False,
    server_scripts: tuple[str, ...] | None = None,
) -> list[int]:
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
        if _is_project365_server_command(
            command,
            allow_standalone=include_any_project_server,
            server_scripts=server_scripts,
        ):
            pids.append(pid)
    return pids


def _is_project365_server_command(
    command: str,
    allow_standalone: bool = True,
    server_scripts: tuple[str, ...] | None = None,
) -> bool:
    lowered = command.lower()
    if "python" not in lowered:
        return False
    scripts = server_scripts or (SERVER_SCRIPTS if allow_standalone else ("project365_control_app.py",))
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
    services = result.get("services")
    if isinstance(services, dict):
        for key, label in (("control", "Control"), ("video", "Video")):
            item = services.get(key)
            if not isinstance(item, dict):
                continue
            lines.append(f"{label}: {item.get('status', 'unknown')}")
            if item.get("url"):
                lines.append(f"  URL: {item['url']}")
            if item.get("pid"):
                lines.append(f"  PID: {item['pid']}")
            if item.get("detected_pids"):
                lines.append(f"  Detected PID(s): {', '.join(str(pid) for pid in item['detected_pids'])}")
            if item.get("log"):
                lines.append(f"  Log: {item['log']}")
        lines.append("Database: SQLite files only; no database server is started or stopped.")
        return "\n".join(line for line in lines if line)
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
