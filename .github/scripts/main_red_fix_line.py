#!/usr/bin/env python3
"""The `Fix in progress:` line of the main-red issue.

The line names the open pull requests that are fixing the break. Its input is
the issue's timeline: one row per `cross-referenced` event from a pull request,
with that pull request's body. A cross-reference alone is not a fix: a comment
on a pull request that cites the issue makes the same event. A pull request
counts only when its own body declares the issue (`Fixes #N` and the other
forms of the repository's reference grammar).

That grammar is not written here. It is `declared_issue_numbers` of
kirocrew-prepare-pr, reached through `issue_gate_refs.py`, the adapter the
Issue Gate uses, so the two cannot disagree on what a declaration is.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_FIX_PRS = 3
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")
UNKNOWN = "Fix in progress: unknown."


def _adapter() -> Any:
    """Load `issue_gate_refs.py`, the sibling adapter onto the reference grammar."""
    path = Path(__file__).resolve().with_name("issue_gate_refs.py")
    spec = importlib.util.spec_from_file_location("issue_gate_refs", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fix_prs(rows: list[dict[str, Any]], repo: str, issue: int | None) -> list[dict[str, str]]:
    """Open pull requests of ``repo`` whose body declares ``issue``, oldest first.

    Raises when a body has to be read and the reference grammar cannot be loaded.
    """
    prefix = f"https://github.com/{repo}/pull/"
    out: list[dict[str, str]] = []
    adapter = grammar = None
    for row in rows:
        url = str(row.get("url") or "")
        login = str(row.get("author") or "")
        body = row.get("body")
        if row.get("state") != "open" or not url.startswith(prefix) or not _LOGIN.match(login):
            continue
        number = url[len(prefix) :]
        if not number.isdigit() or url in {p["url"] for p in out}:
            continue
        if issue is None or not isinstance(body, str) or not body:
            continue
        if adapter is None:
            adapter = _adapter()
            grammar = adapter.load_grammar()
        if str(issue) not in adapter.declared_numbers(body, repo, grammar)[0]:
            continue
        out.append(
            {
                "url": url,
                "number": number,
                "author": login,
                "created": str(row.get("created") or ""),
            }
        )
    return sorted(out, key=lambda pr: pr["created"])[:MAX_FIX_PRS]


def _opened(stamp: str) -> str:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%MZ")
    except ValueError:
        return "at an unknown time"


def fix_line(prs: list[dict[str, str]] | None, issue_number: int | None) -> str:
    """The one line the watch rewrites on every main run; ``None`` means not known."""
    if prs is None:
        return UNKNOWN
    if not prs:
        ref = f"#{issue_number}" if issue_number else "#<this issue>"
        return (
            "Fix in progress: none yet. Anyone may open a fix PR "
            f"(put `Fixes {ref}` in its body)."
        )
    parts = []
    for pr in prs:
        who = pr["author"] if pr["author"].endswith("[bot]") else "@" + pr["author"]
        parts.append(f"#{pr['number']} ({who}, opened {_opened(pr['created'])})")
    return "Fix in progress: " + "; ".join(parts)
