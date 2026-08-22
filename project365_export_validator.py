#!/usr/bin/env python3
"""Validate local Project365 monthly export zips without exposing diary content."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from project365_paths import PROJECT365_PRO_EXPORT_ZIPS_DIR
from typing import Iterable


STRICT_ZIP_RE = r"^\d{4}-(0[1-9]|1[0-2])\.zip$"
MONTH_PREFIX_RE = r"^(\d{4})-(0[1-9]|1[0-2]).*\.zip$"
INTERNAL_ENTRY_RE = r"^(\d{4}-\d{2}-\d{2})\.(png|txt)$"


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    message: str
    zip_file: str | None = None
    month: str | None = None
    entry: str | None = None


@dataclass
class DayEntry:
    date: str
    has_png: bool = False
    has_txt: bool = False
    png_bytes: int = 0
    txt_bytes: int = 0


@dataclass
class MonthExport:
    zip_file: str
    month: str | None
    normalized_filename: bool
    readable: bool
    zip_sha256: str | None = None
    zip_bytes: int = 0
    file_modified_at: str | None = None
    internal_file_count: int = 0
    entries: list[DayEntry] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)


@dataclass
class ValidationReport:
    generated_at: str
    import_dir: str
    expected_start_month: str | None
    expected_end_month: str | None
    zip_count: int
    months_found: list[str]
    duplicate_months: list[str]
    missing_months: list[str]
    error_count: int
    warning_count: int
    month_exports: list[MonthExport]
    issues: list[ValidationIssue]

    @property
    def ok(self) -> bool:
        return self.error_count == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate local Project365 monthly export zips."
    )
    parser.add_argument(
        "--import-dir",
        default=str(PROJECT365_PRO_EXPORT_ZIPS_DIR),
        help="Folder containing Project365 Pro export zips.",
    )
    parser.add_argument(
        "--report-dir",
        default="Reports",
        help="Folder where summary reports will be written.",
    )
    parser.add_argument("--start-month", help="Expected first month, YYYY-MM.")
    parser.add_argument("--end-month", help="Expected last month, YYYY-MM.")
    parser.add_argument(
        "--json-name",
        default="project365_export_validation.json",
        help="JSON report filename.",
    )
    parser.add_argument(
        "--md-name",
        default="project365_export_validation.md",
        help="Markdown summary filename.",
    )
    parser.add_argument(
        "--manifest-name",
        default="source_files.csv",
        help="CSV source manifest filename.",
    )
    args = parser.parse_args()

    report = validate_exports(
        import_dir=Path(args.import_dir),
        start_month=args.start_month,
        end_month=args.end_month,
    )

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / args.json_name
    md_path = report_dir / args.md_name
    manifest_path = report_dir / args.manifest_name
    write_json_report(report, json_path)
    write_markdown_report(report, md_path)
    write_source_manifest(report, manifest_path)

    status = "PASS" if report.ok else "FAIL"
    print(f"Project365 export validation: {status}")
    print(f"Zips scanned: {report.zip_count}")
    print(f"Months found: {len(report.months_found)}")
    print(f"Missing months: {len(report.missing_months)}")
    print(f"Duplicate months: {len(report.duplicate_months)}")
    print(f"Errors: {report.error_count}")
    print(f"Warnings: {report.warning_count}")
    print(f"Reports: {json_path}, {md_path}, and {manifest_path}")
    return 0 if report.ok else 1


def validate_exports(
    import_dir: Path,
    start_month: str | None = None,
    end_month: str | None = None,
) -> ValidationReport:
    import re

    import_dir = import_dir.resolve()
    issues: list[ValidationIssue] = []
    month_exports: list[MonthExport] = []

    _validate_optional_month_arg(start_month, "start-month")
    _validate_optional_month_arg(end_month, "end-month")
    if start_month and end_month and start_month > end_month:
        raise ValueError("start-month must be before or equal to end-month")

    zip_paths = sorted(
        path
        for path in import_dir.rglob("*.zip")
        if path.is_file() and not _has_hidden_path_part(path.relative_to(import_dir))
    )
    strict_zip_re = re.compile(STRICT_ZIP_RE)
    month_prefix_re = re.compile(MONTH_PREFIX_RE)

    for zip_path in zip_paths:
        filename = zip_path.name
        normalized = bool(strict_zip_re.match(filename))
        prefix_match = month_prefix_re.match(filename)
        month = (
            f"{prefix_match.group(1)}-{prefix_match.group(2)}"
            if prefix_match
            else None
        )
        export = MonthExport(
            zip_file=str(zip_path),
            month=month,
            normalized_filename=normalized,
            readable=False,
            zip_bytes=zip_path.stat().st_size,
            file_modified_at=dt.datetime.fromtimestamp(
                zip_path.stat().st_mtime, dt.UTC
            ).isoformat(),
        )

        if not normalized:
            _add_issue(
                export,
                issues,
                "error",
                "invalid_zip_filename",
                "Monthly zip filename must be exactly YYYY-MM.zip.",
            )

        export.zip_sha256 = _sha256_file(zip_path)
        _parse_zip(zip_path, export, issues)
        month_exports.append(export)

    months_by_name: dict[str, list[MonthExport]] = {}
    for export in month_exports:
        if export.month is not None:
            months_by_name.setdefault(export.month, []).append(export)

    duplicate_months = sorted(
        month for month, exports in months_by_name.items() if len(exports) > 1
    )
    for month in duplicate_months:
        for export in months_by_name[month]:
            _add_issue(
                export,
                issues,
                "error",
                "duplicate_month",
                f"More than one zip maps to month {month}.",
                month=month,
            )

    months_found = sorted(months_by_name)
    range_requested = start_month is not None or end_month is not None
    inferred_start = (
        start_month
        or (months_found[0] if range_requested and months_found else None)
    )
    inferred_end = (
        end_month
        or (months_found[-1] if range_requested and months_found else None)
    )
    expected_months = (
        list(iter_months(inferred_start, inferred_end))
        if inferred_start and inferred_end
        else []
    )
    missing_months = [
        month for month in expected_months if month not in months_by_name
    ]
    for month in missing_months:
        issues.append(
            ValidationIssue(
                level="error",
                code="missing_month",
                message=f"Expected monthly zip is missing for {month}.",
                month=month,
            )
        )

    error_count = sum(1 for issue in issues if issue.level == "error")
    warning_count = sum(1 for issue in issues if issue.level == "warning")

    return ValidationReport(
        generated_at=dt.datetime.now(dt.UTC).isoformat(),
        import_dir=str(import_dir),
        expected_start_month=inferred_start,
        expected_end_month=inferred_end,
        zip_count=len(zip_paths),
        months_found=months_found,
        duplicate_months=duplicate_months,
        missing_months=missing_months,
        error_count=error_count,
        warning_count=warning_count,
        month_exports=month_exports,
        issues=issues,
    )


def _parse_zip(
    zip_path: Path,
    export: MonthExport,
    all_issues: list[ValidationIssue],
) -> None:
    import re

    internal_entry_re = re.compile(INTERNAL_ENTRY_RE)
    seen_internal_names: set[str] = set()
    day_entries: dict[str, DayEntry] = {}

    try:
        with zipfile.ZipFile(zip_path) as archive:
            corrupt_name = archive.testzip()
            if corrupt_name is not None:
                _add_issue(
                    export,
                    all_issues,
                    "error",
                    "unreadable_zip_member",
                    "Zip member failed integrity check.",
                    entry=corrupt_name,
                )
                return

            export.readable = True
            for info in archive.infolist():
                if info.is_dir():
                    continue
                export.internal_file_count += 1
                name = info.filename

                if name in seen_internal_names:
                    _add_issue(
                        export,
                        all_issues,
                        "error",
                        "duplicate_internal_file",
                        "Zip contains the same internal filename more than once.",
                        entry=name,
                    )
                seen_internal_names.add(name)

                entry_match = internal_entry_re.match(name)
                if not entry_match:
                    _add_issue(
                        export,
                        all_issues,
                        "error",
                        "invalid_internal_filename",
                        "Internal files must be root-level YYYY-MM-DD.png or YYYY-MM-DD.txt.",
                        entry=name,
                    )
                    continue

                date_text = entry_match.group(1)
                extension = entry_match.group(2)
                if not _is_valid_date(date_text):
                    _add_issue(
                        export,
                        all_issues,
                        "error",
                        "invalid_internal_date",
                        "Internal filename date is not a real calendar date.",
                        entry=name,
                    )
                    continue

                if export.month and date_text[:7] != export.month:
                    _add_issue(
                        export,
                        all_issues,
                        "error",
                        "internal_month_mismatch",
                        "Internal file date does not match the zip month.",
                        entry=name,
                        month=export.month,
                    )

                day = day_entries.setdefault(date_text, DayEntry(date=date_text))
                if extension == "png":
                    day.has_png = True
                    day.png_bytes += info.file_size
                elif extension == "txt":
                    day.has_txt = True
                    day.txt_bytes += info.file_size
    except zipfile.BadZipFile:
        _add_issue(
            export,
            all_issues,
            "error",
            "unreadable_zip",
            "File is not a readable zip archive.",
        )
    except OSError as exc:
        _add_issue(
            export,
            all_issues,
            "error",
            "zip_read_error",
            f"Could not read zip file: {exc.__class__.__name__}.",
        )

    export.entries = [day_entries[date] for date in sorted(day_entries)]


def _add_issue(
    export: MonthExport,
    all_issues: list[ValidationIssue],
    level: str,
    code: str,
    message: str,
    month: str | None = None,
    entry: str | None = None,
) -> None:
    issue = ValidationIssue(
        level=level,
        code=code,
        message=message,
        zip_file=export.zip_file,
        month=month or export.month,
        entry=entry,
    )
    export.issues.append(issue)
    all_issues.append(issue)


def _has_hidden_path_part(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def iter_months(start_month: str, end_month: str) -> Iterable[str]:
    _validate_optional_month_arg(start_month, "start-month")
    _validate_optional_month_arg(end_month, "end-month")

    start_year, start_month_num = map(int, start_month.split("-"))
    end_year, end_month_num = map(int, end_month.split("-"))
    year = start_year
    month = start_month_num

    while (year, month) <= (end_year, end_month_num):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year += 1
            month = 1


def write_json_report(report: ValidationReport, path: Path) -> None:
    path.write_text(
        json.dumps(asdict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_markdown_report(report: ValidationReport, path: Path) -> None:
    lines = [
        "# Project365 Export Validation",
        "",
        f"- Status: {'PASS' if report.ok else 'FAIL'}",
        f"- Generated at: {report.generated_at}",
        f"- Project365 Pro export zip folder: `{report.import_dir}`",
        f"- Zips scanned: {report.zip_count}",
        f"- Months found: {len(report.months_found)}",
        f"- Expected range: {report.expected_start_month or 'n/a'} to {report.expected_end_month or 'n/a'}",
        f"- Missing months: {len(report.missing_months)}",
        f"- Duplicate months: {len(report.duplicate_months)}",
        f"- Errors: {report.error_count}",
        f"- Warnings: {report.warning_count}",
        "",
        "## Month Summary",
        "",
        "| Month | Zip | Readable | Entries | PNG Days | TXT Days | Issues |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]

    for export in report.month_exports:
        png_days = sum(1 for entry in export.entries if entry.has_png)
        txt_days = sum(1 for entry in export.entries if entry.has_txt)
        lines.append(
            "| {month} | `{zip_file}` | {readable} | {entries} | {png_days} | {txt_days} | {issues} |".format(
                month=export.month or "unknown",
                zip_file=Path(export.zip_file).name,
                readable="yes" if export.readable else "no",
                entries=len(export.entries),
                png_days=png_days,
                txt_days=txt_days,
                issues=len(export.issues),
            )
        )

    if report.missing_months:
        lines.extend(["", "## Missing Months", ""])
        lines.extend(f"- {month}" for month in report.missing_months)

    if report.duplicate_months:
        lines.extend(["", "## Duplicate Months", ""])
        lines.extend(f"- {month}" for month in report.duplicate_months)

    if report.issues:
        lines.extend(["", "## Issues", ""])
        for issue in report.issues:
            detail = issue.message
            if issue.entry:
                detail += f" Entry: `{issue.entry}`."
            if issue.zip_file:
                detail += f" Zip: `{Path(issue.zip_file).name}`."
            lines.append(f"- [{issue.level}] `{issue.code}`: {detail}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_source_manifest(report: ValidationReport, path: Path) -> None:
    fieldnames = [
        "source_path",
        "zip_filename",
        "month",
        "normalized_filename",
        "readable",
        "validation_status",
        "zip_bytes",
        "zip_sha256",
        "file_modified_at",
        "internal_file_count",
        "entry_count",
        "png_day_count",
        "txt_day_count",
        "error_count",
        "warning_count",
        "issue_codes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for export in report.month_exports:
            error_count = sum(1 for issue in export.issues if issue.level == "error")
            warning_count = sum(
                1 for issue in export.issues if issue.level == "warning"
            )
            png_day_count = sum(1 for entry in export.entries if entry.has_png)
            txt_day_count = sum(1 for entry in export.entries if entry.has_txt)
            writer.writerow(
                {
                    "source_path": export.zip_file,
                    "zip_filename": Path(export.zip_file).name,
                    "month": export.month or "",
                    "normalized_filename": str(export.normalized_filename).lower(),
                    "readable": str(export.readable).lower(),
                    "validation_status": "pass" if error_count == 0 else "fail",
                    "zip_bytes": export.zip_bytes,
                    "zip_sha256": export.zip_sha256 or "",
                    "file_modified_at": export.file_modified_at or "",
                    "internal_file_count": export.internal_file_count,
                    "entry_count": len(export.entries),
                    "png_day_count": png_day_count,
                    "txt_day_count": txt_day_count,
                    "error_count": error_count,
                    "warning_count": warning_count,
                    "issue_codes": ";".join(
                        sorted({issue.code for issue in export.issues})
                    ),
                }
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_valid_date(date_text: str) -> bool:
    try:
        dt.date.fromisoformat(date_text)
    except ValueError:
        return False
    return True


def _validate_optional_month_arg(month: str | None, name: str) -> None:
    if month is None:
        return
    try:
        parsed = dt.date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM") from exc
    if f"{parsed.year:04d}-{parsed.month:02d}" != month:
        raise ValueError(f"{name} must be YYYY-MM")


if __name__ == "__main__":
    raise SystemExit(main())
