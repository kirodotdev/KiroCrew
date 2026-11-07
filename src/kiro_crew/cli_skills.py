"""The read-only ``kirocrew skills list`` and ``kirocrew skills show`` handlers.

Both verbs only read: ``list`` prints the pending candidates and the live catalog the
dashboard already lists, and ``show`` prints one pending candidate through
``SkillsLoader.get_pending_skill``, the dashboard detail API's own pinned, bounded read.
Approving or dismissing a candidate stays a dashboard action.

Everything a skill author wrote is redacted and stripped of terminal escapes before it is
printed. Nothing here imports ``kiro_crew.cli``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NoReturn

from kiro_crew.external_text import scrub_untrusted_text
from kiro_crew.skills import SkillsLoader
from kiro_crew.terminal_safe import _TERMINAL_CTRL_RE, safe_terminal_line

#: Rows printed per section. The rest are counted, not printed.
_LIST_MAX_ROWS = 500


def _fail(code: str, message: str) -> NoReturn:
    print(f"{code}: {message}", file=sys.stderr)
    raise SystemExit(1)


def _terminal_line(value: object) -> str:
    """Redact, strip terminal escapes, and bound one terminal line.

    The pass order is the dashboard's, from ``scrub_untrusted_text``; the
    terminal drops whole escape sequences rather than single control bytes.
    """
    text = scrub_untrusted_text(str(value), strip=lambda s: _TERMINAL_CTRL_RE.sub("", s))
    return safe_terminal_line(text)


def _terminal_value(value: object) -> Any:
    """Sanitize and redact every untrusted string before terminal rendering."""
    if isinstance(value, str):
        return _terminal_line(value)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            name = _terminal_line(key)
            # Two keys that differ only in a stripped character must both
            # print, so a collision gets a numbered suffix.
            unique, n = name, 2
            while unique in safe:
                unique, n = f"{name} ({n})", n + 1
            safe[unique] = _terminal_value(item)
        return safe
    if isinstance(value, list):
        return [_terminal_value(item) for item in value]
    return value


def _multiline(value: object) -> str:
    """Frame each sanitized candidate-body line as untrusted terminal text."""
    return "\n".join(f"| {_terminal_line(line)}" for line in str(value).split("\n"))


def _skills_list(loader: SkillsLoader, args: argparse.Namespace) -> None:
    include_live = bool(getattr(args, "live", False) or getattr(args, "all", False))
    include_pending = bool(getattr(args, "all", False) or not include_live)
    payload: dict[str, Any] = {}
    try:
        if include_pending:
            rows = loader.list_pending_skills()
            payload["pending"] = rows[:_LIST_MAX_ROWS]
            if len(rows) > _LIST_MAX_ROWS:
                payload["pending_omitted"] = len(rows) - _LIST_MAX_ROWS
        if include_live:
            rows = loader.list_skills()
            payload["live"] = rows[:_LIST_MAX_ROWS]
            if len(rows) > _LIST_MAX_ROWS:
                payload["live_omitted"] = len(rows) - _LIST_MAX_ROWS
    except UnicodeDecodeError:
        _fail("invalid_skill_encoding", "a SKILL.md is not valid UTF-8")
    except (OSError, RecursionError):
        _fail("skills_unreadable", "the skills directory could not be read")
    safe_payload = _terminal_value(payload)
    if getattr(args, "json", False):
        print(json.dumps(safe_payload, indent=2, sort_keys=True))
        return
    if include_pending:
        pending = safe_payload["pending"]
        if not pending:
            print("No pending skill candidates.")
        for row in pending:
            print(
                f"PENDING  {row['slug']}  [{row.get('kind', 'new')}]  "
                f"{row.get('description', '')}".rstrip()
            )
        if safe_payload.get("pending_omitted"):
            print(f"[{safe_payload['pending_omitted']} more pending candidate(s) not shown]")
    if include_live:
        live = safe_payload["live"]
        if not live:
            print("No live skills.")
        for row in live:
            print(f"LIVE     {row['key']}  {row.get('description', '')}".rstrip())
        if safe_payload.get("live_omitted"):
            print(f"[{safe_payload['live_omitted']} more live skill(s) not shown]")


def _skills_show(loader: SkillsLoader, slug: str) -> None:
    try:
        detail = loader.get_pending_skill(slug)
        staged = detail is not None or loader.pending_candidate_is_staged(slug)
    except (OSError, RecursionError):
        _fail(
            "candidate_unreadable",
            f"pending skill '{_terminal_line(slug)}' could not be read",
        )
    if detail is None:
        if staged:
            # The pinned detail read refuses a link, a non-regular file, a file
            # over its byte cap, text that is not UTF-8 or a tree over its entry
            # budget. It refuses the whole candidate, so nothing prints that
            # could look like a complete preview.
            _fail(
                "candidate_unreadable",
                f"pending skill '{_terminal_line(slug)}' is staged but is not a"
                " plain, readable tree of files, so it cannot be shown",
            )
        _fail("not_found", f"pending skill '{_terminal_line(slug)}' was not found")
    content = _multiline(detail.get("content", "")).rstrip()
    meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    script_items = detail.get("scripts")
    scripts = script_items if isinstance(script_items, list) else []
    loader_validation = detail.get("script_validation")
    if isinstance(loader_validation, dict) and isinstance(loader_validation.get("ok"), bool):
        validation = {
            "ok": loader_validation["ok"],
            "scripts": loader_validation.get("report", {}),
        }
    else:
        validation = {"status": "unavailable", "scripts": {}}
    # Render both JSON sections before printing anything, so a nesting too deep
    # to serialize fails with a coded error instead of a traceback.
    try:
        meta_text = json.dumps(_terminal_value(meta), indent=2, sort_keys=True)
        validation_text = json.dumps(_terminal_value(validation), indent=2, sort_keys=True)
    except RecursionError:
        _fail("candidate_unreadable", "the candidate metadata is nested too deeply")
    print("--- SKILL.md (untrusted; each line is prefixed with '| ') ---")
    print(content)
    print("\n--- .meta.json ---")
    print(meta_text)
    for script in scripts:
        if not isinstance(script, dict):
            continue
        filename = _terminal_line(script.get("filename", "<unnamed>"))
        body = _multiline(script.get("content", "")).rstrip()
        print(f"\n--- scripts/{filename} (untrusted; each line is prefixed with '| ') ---")
        print(body)
    print("\n--- validation ---")
    print(validation_text)


def _skills(args: argparse.Namespace) -> None:
    """List and inspect skill candidates without changing them."""
    action = getattr(args, "skills_action", None)
    if action not in ("list", "show"):
        _fail("missing_action", "choose list or show")
    try:
        loader = SkillsLoader(install_builtins=False)
    except OSError:
        _fail("skills_unreadable", "the skills directory could not be read")
    if action == "list":
        _skills_list(loader, args)
    else:
        _skills_show(loader, getattr(args, "slug", ""))
