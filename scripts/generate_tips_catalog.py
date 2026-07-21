#!/usr/bin/env python3
"""Generate tips_catalog.json from docs/*.md at release time.

Usage:
    python scripts/generate_tips_catalog.py

Scans src/kiro_crew/docs/*.md (same logic as tips._scan_docs_catalog) and writes
a static JSON catalog to src/kiro_crew/data/tips_catalog.json.
Idempotent — safe to run multiple times.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _REPO_ROOT / "src" / "kiro_crew" / "docs"

# Single source of truth for which docs are user-facing tip candidates.
# tips_allowlist is dependency-free, so importing it never pulls the runtime.
sys.path.insert(0, str(_REPO_ROOT / "src"))
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST  # noqa: E402


def _get_git_mtime(filepath: Path) -> float:
    """Get last-modified commit timestamp for a file via git log.

    Falls back to filesystem mtime if git is unavailable or the file is untracked.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(filepath)],
            capture_output=True,
            text=True,
            cwd=str(filepath.parent),
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    # Fall back to filesystem mtime
    try:
        return filepath.stat().st_mtime
    except OSError:
        return 0.0


def _scan_docs_catalog() -> list[dict]:
    """Scan docs/*.md for H1 + first paragraph — mirrors tips._scan_docs_catalog."""
    entries: list[dict] = []
    if not _DOCS_DIR.is_dir():
        return entries
    for md in sorted(_DOCS_DIR.glob("*.md")):
        if md.name not in TIP_DOC_ALLOWLIST:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Extract H1
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        if not m:
            continue
        title = m.group(1).strip()
        # First non-empty paragraph after H1
        rest = text[m.end():].lstrip("\n")
        para = ""
        for block in rest.split("\n\n"):
            stripped = block.strip()
            if stripped and not stripped.startswith("#"):
                para = " ".join(stripped.split())
                break
        if not para:
            continue
        mtime = _get_git_mtime(md)
        entries.append({
            "feature": title,
            "title": title,
            "summary": para[:300],
            "doc": md.name,
            "mtime": mtime,
        })
    return entries


def main() -> None:
    entries = _scan_docs_catalog()
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    out_path = _REPO_ROOT / "src" / "kiro_crew" / "data" / "tips_catalog.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(entries)} catalog entries -> {out_path}")


if __name__ == "__main__":
    main()
