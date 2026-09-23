#!/usr/bin/env python3
"""Produce a reproducible first-pass hotspot inventory for the Kiro Crew repository.

The script uses Kiro Crew's descriptor-pinned reader plus the Python standard
library. It prefers Git-tracked files, falls back to a filesystem walk, and
never changes the repository.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from refactor_limits import (
    MAX_CLI_ITEMS,
    MAX_RETAINED_ITEMS,
    MAX_RETAINED_STRING_CHARS,
    MAX_SOURCE_FILE_BYTES,
    BoundedAppendAction,
    bounded_cli_text,
    bounded_text,
    filesystem_decode,
    markdown_code,
)

from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

DEFAULT_EXTENSIONS = {
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".swift",
    ".ts",
    ".tsx",
}

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "generated",
    "node_modules",
    "target",
    "vendor",
}

TEST_DIRS = {
    "__tests__",
    "fixture",
    "fixtures",
    "snapshot",
    "snapshots",
    "stories",
    "test",
    "tests",
}


def run_git(
    root: Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=env,
    )


def discover_root(start: Path) -> tuple[Path, bool]:
    try:
        result = run_git(start, ["rev-parse", "--show-toplevel"])
        return Path(filesystem_decode(result.stdout.rstrip(b"\r\n"))).resolve(), True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return start.resolve(), False


def tracked_files(root: Path, is_git: bool, overflow: dict[str, int]) -> Iterable[Path]:
    if is_git:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        process = subprocess.Popen(
            ["git", "-C", str(root), "ls-files", "--deduplicate", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        current = bytearray()
        discarding = False
        max_path_bytes = MAX_RETAINED_STRING_CHARS * 4
        assert process.stdout is not None
        while chunk := process.stdout.read(64 * 1024):
            for value in chunk:
                if value == 0:
                    if discarding:
                        overflow["paths"] += 1
                    elif current:
                        yield Path(filesystem_decode(bytes(current)))
                    current.clear()
                    discarding = False
                elif not discarding:
                    if len(current) >= max_path_bytes:
                        current.clear()
                        discarding = True
                    else:
                        current.append(value)
        if discarding:
            overflow["paths"] += 1
        elif current:
            yield Path(filesystem_decode(bytes(current)))
        returncode = process.wait()
        if returncode:
            raise RuntimeError(f"git ls-files failed ({returncode})")
        return

    for base, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name.lower() not in DEFAULT_EXCLUDED_DIRS]
        base_path = Path(base)
        for name in files:
            relative = (base_path / name).relative_to(root)
            _, truncated = bounded_text(relative.as_posix())
            if truncated:
                overflow["paths"] += 1
            else:
                yield relative


def is_test_path(path: Path) -> bool:
    lower_parts = {part.lower() for part in path.parts[:-1]}
    if lower_parts & TEST_DIRS:
        return True
    name = path.name.lower()
    stem = path.stem.lower()
    return (
        name.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def under_prefix(path: Path, prefixes: list[Path]) -> bool:
    if not prefixes:
        return True
    normalized = path.as_posix()
    return any(
        prefix.as_posix() == "."
        or normalized == prefix.as_posix()
        or normalized.startswith(prefix.as_posix().rstrip("/") + "/")
        for prefix in prefixes
    )


def python_shape(text: str) -> dict[str, Any]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {"syntax_error": True}

    functions: list[int] = []
    classes: list[int] = []
    for node in ast.walk(tree):
        end = getattr(node, "end_lineno", None)
        start = getattr(node, "lineno", None)
        if end is None or start is None:
            continue
        size = int(end) - int(start) + 1
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(size)
        elif isinstance(node, ast.ClassDef):
            classes.append(size)

    return {
        "syntax_error": False,
        "function_count": len(functions),
        "max_function_lines": max(functions, default=0),
        "functions_ge_100": sum(size >= 100 for size in functions),
        "functions_ge_300": sum(size >= 300 for size in functions),
        "class_count": len(classes),
        "max_class_lines": max(classes, default=0),
        "classes_ge_500": sum(size >= 500 for size in classes),
        "classes_ge_1000": sum(size >= 1000 for size in classes),
    }


def inspect_file(root: Path, relative: Path) -> tuple[dict[str, Any] | None, str | None]:
    absolute = root / relative
    try:
        data = safe_read_file_bytes_nolink(
            str(absolute),
            within_root=str(root),
            max_bytes=MAX_SOURCE_FILE_BYTES,
        )
    except FileTooLargeError:
        return None, (
            f"{relative.as_posix()}: skipped file above the " f"{MAX_SOURCE_FILE_BYTES:,}-byte cap"
        )
    if data is None:
        return None, f"{relative.as_posix()}: skipped unsafe or unreadable file"
    if b"\0" in data:
        return None, f"{relative.as_posix()}: skipped binary content"

    physical = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    text = data.decode("utf-8", errors="replace")
    nonblank = sum(bool(line.strip()) for line in text.splitlines())
    item: dict[str, Any] = {
        "path": relative.as_posix(),
        "extension": relative.suffix.lower(),
        "physical_lines": physical,
        "nonblank_lines": nonblank,
        "bytes": len(data),
    }
    if relative.suffix.lower() == ".py":
        item["python"] = python_shape(text)
    return item, None


def git_snapshot(root: Path, is_git: bool) -> dict[str, Any]:
    if not is_git:
        return {"is_git": False, "head": None, "dirty": None}
    head_result = run_git(root, ["rev-parse", "HEAD"], check=False)
    head = head_result.stdout.decode().strip() if head_result.returncode == 0 else None
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    status = subprocess.Popen(
        ["git", "-C", str(root), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    assert status.stdout is not None
    first_byte = status.stdout.read(1)
    if first_byte:
        status.terminate()
    returncode = status.wait()
    dirty = bool(first_byte) if first_byte or returncode == 0 else None
    return {"is_git": True, "head": head, "dirty": dirty}


def render_markdown(report: dict[str, Any], top: int) -> str:
    totals = report["totals"]
    lines = [
        "# Refactor Hotspot Inventory",
        "",
        f"- Collected: {markdown_code(str(report['collected_at']))}",
        f"- Root: {markdown_code(str(report['root']))}",
        f"- Git HEAD: {markdown_code(str(report['git']['head']))}",
        f"- Dirty: {markdown_code(str(report['git']['dirty']))}",
        f"- Files measured: **{totals['files']:,}**",
        f"- Physical lines: **{totals['physical_lines']:,}**",
        f"- Nonblank lines: **{totals['nonblank_lines']:,}**",
        (
            "- Retention overflow (files / paths / warnings): "
            f"**{report['retention_overflow']['files']:,} / "
            f"{report['retention_overflow']['paths']:,} / "
            f"{report['retention_overflow']['warnings']:,}**"
        ),
        "",
        "## Thresholds",
        "",
        "| Physical lines | Files |",
        "|---:|---:|",
    ]
    for threshold, count in report["threshold_counts"].items():
        lines.append(f"| ≥ {int(threshold):,} | {count:,} |")

    lines.extend(
        [
            "",
            f"## Largest {min(top, len(report['files']))} files",
            "",
            "| Physical | Nonblank | File |",
            "|---:|---:|---|",
        ]
    )
    for item in report["files"][:top]:
        path = markdown_code(str(item["path"]))
        lines.append(f"| {item['physical_lines']:,} | {item['nonblank_lines']:,} | {path} |")

    py = report["python"]
    if py["files"]:
        lines.extend(
            [
                "",
                "## Python structure",
                "",
                f"- Parsed files: **{py['files'] - py['syntax_errors']:,}**; syntax errors: **{py['syntax_errors']:,}**",
                f"- Functions: **{py['function_count']:,}**; ≥100 lines: **{py['functions_ge_100']:,}**; ≥300 lines: **{py['functions_ge_300']:,}**; max: **{py['max_function_lines']:,}**",
                f"- Classes: **{py['class_count']:,}**; ≥500 lines: **{py['classes_ge_500']:,}**; ≥1000 lines: **{py['classes_ge_1000']:,}**; max: **{py['max_class_lines']:,}**",
            ]
        )

    if report["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {markdown_code(str(warning))}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=bounded_cli_text,
        default=".",
        help="Repository or source root (default: current directory)",
    )
    parser.add_argument(
        "--extensions",
        type=parse_extensions,
        default=sorted(DEFAULT_EXTENSIONS),
        help="Comma-separated source extensions",
    )
    parser.add_argument(
        "--exclude-dir",
        action=BoundedAppendAction,
        type=bounded_cli_text,
        default=[],
        help="Additional directory name to exclude; repeatable",
    )
    parser.add_argument(
        "--path-prefix",
        action=BoundedAppendAction,
        type=bounded_cli_text,
        default=[],
        help="Limit to a repository-relative path; repeatable",
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="Include tests, fixtures, snapshots, and stories",
    )
    parser.add_argument(
        "--thresholds",
        type=parse_thresholds,
        default=[3000, 5000],
        help="Comma-separated positive physical-line thresholds",
    )
    parser.add_argument("--top", type=int, default=50, help="Largest files to print in Markdown")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def parse_extensions(value: str) -> list[str]:
    """Parse and bound a comma-separated extension set."""

    bounded_cli_text(value)
    extensions: set[str] = set()
    for raw_item in value.split(","):
        item = raw_item.strip().lower()
        if not item:
            continue
        bounded_cli_text(item)
        extensions.add(item if item.startswith(".") else "." + item)
        if len(extensions) > MAX_CLI_ITEMS:
            raise argparse.ArgumentTypeError(
                f"extensions accepts at most {MAX_CLI_ITEMS} distinct values"
            )
    if not extensions:
        raise argparse.ArgumentTypeError("extensions must contain at least one value")
    return sorted(extensions)


def parse_thresholds(value: str) -> list[int]:
    """Parse a non-empty comma-separated set of positive thresholds."""

    bounded_cli_text(value)
    thresholds: set[int] = set()
    for raw_item in value.split(","):
        if not raw_item.strip():
            continue
        try:
            threshold = int(raw_item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("thresholds must be comma-separated integers") from exc
        if threshold <= 0:
            raise argparse.ArgumentTypeError("thresholds must be positive integers")
        thresholds.add(threshold)
        if len(thresholds) > MAX_CLI_ITEMS:
            raise argparse.ArgumentTypeError(
                f"thresholds accepts at most {MAX_CLI_ITEMS} distinct values"
            )
    if not thresholds:
        raise argparse.ArgumentTypeError("thresholds must be positive integers")
    return sorted(thresholds)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args()
    requested_root = Path(args.root).resolve()
    if not requested_root.is_dir():
        print(f"error: source root is not a directory: {requested_root}", file=sys.stderr)
        return 2
    root, is_git = discover_root(requested_root)
    extensions = set(args.extensions)
    excluded_dirs = DEFAULT_EXCLUDED_DIRS | {item.lower() for item in args.exclude_dir}
    prefixes = [Path(item) for item in args.path_prefix]
    root_prefix: Path | None = None
    if is_git and requested_root != root:
        try:
            root_prefix = requested_root.relative_to(root)
        except ValueError:
            pass
    thresholds = args.thresholds

    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    files_overflow = 0
    warnings_overflow = 0
    traversal_overflow = {"paths": 0}

    def record_warning(message: str) -> None:
        nonlocal warnings_overflow
        bounded, truncated = bounded_text(message)
        if truncated:
            warnings_overflow += 1
        if len(warnings) < MAX_RETAINED_ITEMS:
            warnings.append(bounded)
        else:
            warnings_overflow += 1

    try:
        for relative in tracked_files(root, is_git, traversal_overflow):
            if relative.suffix.lower() not in extensions:
                continue
            if any(part.lower() in excluded_dirs for part in relative.parts[:-1]):
                continue
            if not args.include_tests and is_test_path(relative):
                continue
            if root_prefix is not None and not under_prefix(relative, [root_prefix]):
                continue
            if not under_prefix(relative, prefixes):
                continue
            bounded_path, path_truncated = bounded_text(relative.as_posix())
            if path_truncated:
                record_warning(f"{bounded_path}: skipped path above the retained string cap")
                continue
            if len(files) >= MAX_RETAINED_ITEMS:
                files_overflow += 1
                continue
            item, warning = inspect_file(root, relative)
            if warning:
                record_warning(warning)
            if item is not None:
                files.append(item)
    except (OSError, RuntimeError) as exc:
        print(f"error: source inventory failed: {exc}", file=sys.stderr)
        return 2

    files.sort(key=lambda item: (-item["physical_lines"], item["path"]))
    py_items = [item["python"] for item in files if "python" in item]
    python_summary = {
        "files": len(py_items),
        "syntax_errors": sum(bool(item.get("syntax_error")) for item in py_items),
        "function_count": sum(int(item.get("function_count", 0)) for item in py_items),
        "max_function_lines": max(
            (int(item.get("max_function_lines", 0)) for item in py_items), default=0
        ),
        "functions_ge_100": sum(int(item.get("functions_ge_100", 0)) for item in py_items),
        "functions_ge_300": sum(int(item.get("functions_ge_300", 0)) for item in py_items),
        "class_count": sum(int(item.get("class_count", 0)) for item in py_items),
        "max_class_lines": max(
            (int(item.get("max_class_lines", 0)) for item in py_items), default=0
        ),
        "classes_ge_500": sum(int(item.get("classes_ge_500", 0)) for item in py_items),
        "classes_ge_1000": sum(int(item.get("classes_ge_1000", 0)) for item in py_items),
    }

    report = {
        "schema_version": 1,
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "root": str(root),
        "git": git_snapshot(root, is_git),
        "scope": {
            "extensions": sorted(extensions),
            "excluded_dirs": sorted(excluded_dirs),
            "include_tests": bool(args.include_tests),
            "path_prefixes": [path.as_posix() for path in prefixes],
            "requested_root_prefix": root_prefix.as_posix() if root_prefix else None,
        },
        "totals": {
            "files": len(files),
            "physical_lines": sum(item["physical_lines"] for item in files),
            "nonblank_lines": sum(item["nonblank_lines"] for item in files),
            "bytes": sum(item["bytes"] for item in files),
        },
        "threshold_counts": {
            str(value): sum(item["physical_lines"] >= value for item in files)
            for value in thresholds
        },
        "python": python_summary,
        "files": files,
        "warnings": warnings,
        "retention_overflow": {
            "files": files_overflow,
            "paths": traversal_overflow["paths"],
            "warnings": warnings_overflow,
        },
    }

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2, ensure_ascii=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(report, max(0, args.top)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
