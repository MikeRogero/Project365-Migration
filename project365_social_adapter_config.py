#!/usr/bin/env python3
"""Validate social export adapter configs and create local staging records."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SocialAdapterSummary:
    adapter_id: str
    report_path: str
    staging_path: str
    source_files: int
    staged_events: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a social export adapter config and stage events locally."
    )
    parser.add_argument("--config", required=True, help="Adapter config JSON path.")
    parser.add_argument("--source-root", required=True, help="Local extracted export root.")
    parser.add_argument(
        "--staging-out",
        default="Project365Canonical/staging/social/events.jsonl",
        help="Output JSONL staging path. This may contain private social text.",
    )
    parser.add_argument(
        "--report-dir",
        default="Project365Canonical/exports/verification_reports",
        help="Metadata-only validation report folder.",
    )
    args = parser.parse_args()

    summary = stage_social_events(
        config_path=Path(args.config),
        source_root=Path(args.source_root),
        staging_path=Path(args.staging_out),
        report_dir=Path(args.report_dir),
    )
    print("Project365 social adapter staging: PASS")
    print(f"Adapter: {summary.adapter_id}")
    print(f"Report: {summary.report_path}")
    print(f"Staging: {summary.staging_path}")
    print(f"Source files: {summary.source_files}")
    print(f"Staged events: {summary.staged_events}")
    return 0


def stage_social_events(
    config_path: Path,
    source_root: Path,
    staging_path: Path,
    report_dir: Path,
) -> SocialAdapterSummary:
    config = _load_config(config_path)
    adapter_id = _required_string(config, "adapter_id")
    source_name = _required_string(config, "source_name")
    files = _required_list(config, "files")
    record = _required_dict(config, "record")
    fields = _required_dict(record, "fields")
    file_id = _required_string(record, "file_id")
    record_selector = _required_string(record, "record_selector")

    resolved_files = _resolve_files(source_root, files)
    if file_id not in resolved_files:
        raise ValueError(f"record.file_id does not match a configured file: {file_id}")

    payload = _read_payload(resolved_files[file_id]["path"], resolved_files[file_id])
    records = _select_values(payload, record_selector)
    if not isinstance(records, list):
        raise ValueError("record_selector must resolve to a list of source records")

    staging_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{adapter_id}_adapter_validation.csv"
    staged_count = 0
    report_rows: list[dict[str, object]] = []

    with staging_path.open("w", encoding="utf-8") as output:
        for index, source_record in enumerate(records):
            if not isinstance(source_record, dict):
                report_rows.append(_report_row(index, "", "skipped_non_object", False))
                continue
            staged = _normalize_record(
                adapter_id,
                source_name,
                file_id,
                resolved_files[file_id]["relative_path"],
                source_record,
                fields,
            )
            if not _passes_filters(staged, record.get("filters", {})):
                report_rows.append(_report_row(index, staged["source_id"], "filtered", False))
                continue
            output.write(json.dumps(staged, ensure_ascii=False, sort_keys=True) + "\n")
            staged_count += 1
            report_rows.append(_report_row(index, staged["source_id"], "staged", True))

    _write_report(report_path, report_rows)
    return SocialAdapterSummary(
        adapter_id=adapter_id,
        report_path=str(report_path),
        staging_path=str(staging_path),
        source_files=len(resolved_files),
        staged_events=staged_count,
    )


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing adapter config: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Adapter config must be a JSON object")
    privacy = _required_dict(payload, "privacy")
    if privacy.get("local_only") is not True:
        raise ValueError("privacy.local_only must be true")
    return payload


def _resolve_files(source_root: Path, files: list[Any]) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Each files item must be an object")
        file_id = _required_string(item, "id")
        relative_path = _required_string(item, "path")
        file_path = source_root / relative_path
        if item.get("required", True) and not file_path.exists():
            raise FileNotFoundError(f"Missing required source file for {file_id}: {file_path}")
        if file_path.exists():
            resolved[file_id] = {
                **item,
                "path": file_path,
                "relative_path": relative_path,
            }
    return resolved


def _read_payload(path: Path, file_config: dict[str, Any]) -> Any:
    raw = path.read_text(encoding=file_config.get("encoding", "utf-8"))
    file_format = file_config.get("format")
    if file_format == "json":
        return json.loads(raw)
    if file_format == "json_js_assignment":
        prefix = _required_string(file_config, "assignment_prefix")
        stripped = raw.strip()
        if not stripped.startswith(prefix):
            raise ValueError(f"{path} does not start with configured assignment_prefix")
        stripped = stripped[len(prefix) :].strip()
        if stripped.endswith(";"):
            stripped = stripped[:-1].strip()
        return json.loads(stripped)
    raise ValueError(f"Unsupported file format for {path}: {file_format}")


def _normalize_record(
    adapter_id: str,
    source_name: str,
    file_id: str,
    relative_path: str,
    source_record: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    source_id = str(_first_value(source_record, _required_string(fields, "source_id")) or "")
    if not source_id:
        raise ValueError("Configured source_id selector resolved empty")
    return {
        "adapter_id": adapter_id,
        "source_name": source_name,
        "source_id": source_id,
        "timestamp": _first_value(source_record, _required_string(fields, "timestamp")),
        "text": _first_value(source_record, fields.get("text")),
        "urls": _list_values(source_record, fields.get("urls")),
        "media_references": _list_values(source_record, fields.get("media_references")),
        "latitude": _first_value(source_record, fields.get("latitude")),
        "longitude": _first_value(source_record, fields.get("longitude")),
        "place_name": _first_value(source_record, fields.get("place_name")),
        "provenance": {
            "source_file_id": file_id,
            "source_file_path": relative_path,
            "source_record_id": source_id,
        },
    }


def _passes_filters(staged: dict[str, Any], filters: Any) -> bool:
    if not isinstance(filters, dict):
        return True
    if filters.get("require_any"):
        for field_name in filters["require_any"]:
            value = staged.get(field_name)
            if value not in (None, "", []):
                return True
        return False
    return True


def _select_values(payload: Any, selector: str | None) -> Any:
    if selector in (None, "$"):
        return payload
    if selector and selector.startswith("$[*]"):
        values = payload if isinstance(payload, list) else []
        remaining = selector[4:]
        if remaining.startswith("."):
            parts = remaining[1:].split(".")
        elif remaining == "":
            return values
        else:
            raise ValueError(f"Unsupported selector: {selector}")
    elif selector and selector.startswith("$."):
        values = [payload]
        parts = selector[2:].split(".")
    else:
        raise ValueError(f"Unsupported selector: {selector}")
    for part in parts:
        next_values: list[Any] = []
        is_array = part.endswith("[*]")
        index = _part_index(part)
        key = part[:-3] if is_array else _part_key(part)
        for value in values:
            if isinstance(value, dict) and key in value:
                selected = value[key]
                if is_array:
                    if isinstance(selected, list):
                        next_values.extend(selected)
                elif index is not None:
                    if isinstance(selected, list) and 0 <= index < len(selected):
                        next_values.append(selected[index])
                else:
                    next_values.append(selected)
        values = next_values
    return values if "[*]" in selector else (values[0] if values else None)


def _first_value(source_record: dict[str, Any], selector: Any) -> Any:
    values = _list_values(source_record, selector)
    return values[0] if values else None


def _list_values(source_record: dict[str, Any], selector: Any) -> list[Any]:
    if not selector:
        return []
    if isinstance(selector, list):
        output: list[Any] = []
        for item in selector:
            output.extend(_list_values(source_record, item))
        return output
    if not isinstance(selector, str):
        raise ValueError("Field selectors must be strings or lists of strings")
    selected = _select_values(source_record, selector)
    if selected is None:
        return []
    if isinstance(selected, list):
        return [value for value in selected if value not in (None, "")]
    return [selected] if selected not in (None, "") else []


def _report_row(
    source_index: int,
    source_id: str,
    status: str,
    staged: bool,
) -> dict[str, object]:
    return {
        "source_index": source_index,
        "source_id": source_id,
        "status": status,
        "staged": str(staged).lower(),
    }


def _write_report(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source_index", "source_id", "status", "staged"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _required_dict(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_list(mapping: dict[str, Any], key: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return value


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _part_index(part: str) -> int | None:
    if not part.endswith("]") or "[" not in part:
        return None
    index_text = part.rsplit("[", 1)[1][:-1]
    if not index_text.isdigit():
        return None
    return int(index_text)


def _part_key(part: str) -> str:
    if _part_index(part) is None:
        return part
    return part.rsplit("[", 1)[0]


if __name__ == "__main__":
    raise SystemExit(main())
