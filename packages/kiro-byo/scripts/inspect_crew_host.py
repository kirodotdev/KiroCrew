#!/usr/bin/env python3
"""Read-only KiroCrew source/archive reconnaissance for native ACP integration.

Accept an installed Python package directory, a source checkout, or a ZIP of
KiroCrew source.  Do not execute upstream Python or extract archive members to disk.
A source-level match is never labeled a real Gateway/Worker E2E pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kiro_byo.crew_native_probe import inspect_native_registration, inspect_native_sources  # noqa: E402

NEEDED = ("agent_sdk/backends.py", "agent_sdk/backends_routing.py")
MAX_FILE = 2 * 1024 * 1024


def _locate_in_zip(archive: Path) -> tuple[dict[str, str], dict[str, str]]:
    texts: dict[str, str] = {}
    errors: dict[str, str] = {}
    with zipfile.ZipFile(archive) as zf:
        for relative in NEEDED:
            matches = []
            for info in zf.infolist():
                parts = Path(info.filename.replace("\\", "/")).parts
                if (info.is_dir() or ".." in parts or info.filename.startswith("/")
                        or len(parts) < 3 or tuple(parts[-3:]) != tuple(("kiro_crew/" + relative).split("/"))):
                    continue
                matches.append(info)
            if len(matches) != 1:
                errors[relative] = f"expected one source file, found {len(matches)}"
                continue
            member = matches[0]
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000 or member.file_size > MAX_FILE:
                errors[relative] = "unsafe symlink or oversized source file"
                continue
            try:
                texts[relative] = zf.read(member).decode("utf-8")
            except (UnicodeError, RuntimeError, ValueError, OSError):
                errors[relative] = "source member cannot be safely read"
    return texts, errors


def archive_native_report(archive: Path) -> dict:
    """Analyze an archive in memory, without writing or importing host files."""
    texts, errors = _locate_in_zip(archive)
    report = inspect_native_sources(texts.get(NEEDED[0]), texts.get(NEEDED[1]))
    h = hashlib.sha256()
    with archive.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    report.update({
        "input": "source_archive", "archive_sha256": h.hexdigest(),
        "missing_or_rejected_members": errors,
        "source_tag_authenticated": False,
        "host_e2e_verified": False,
    })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--crew-package", type=Path)
    sources.add_argument("--crew-source", type=Path, help="KiroCrew repository root")
    sources.add_argument("--crew-archive", type=Path, help="KiroCrew source ZIP")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.crew_archive:
            report = archive_native_report(args.crew_archive)
        else:
            package = args.crew_package or args.crew_source / "src" / "kiro_crew"
            report = inspect_native_registration(package)
            report.update({"input": "installed_package" if args.crew_package else "source_checkout",
                           "source_tag_authenticated": False, "host_e2e_verified": False})
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        report = {"status": "source_unavailable", "native_integration_verified": False,
                  "error": str(exc), "host_e2e_verified": False}
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if report.get("status") not in {"source_unavailable", "source_invalid"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
