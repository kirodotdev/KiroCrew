"""``kirocrew eval capture <session-key>``: save a recorded chat as an eval scenario.

A chat that went wrong is the best regression test there is, and writing its scenario
file by hand is the step nobody does. This reads the chat's transcript, takes each user
message as a turn, seeds assertions for the tools the agent called, and writes a
scenario file the developer then edits and runs with ``kirocrew eval --file``.

The transcript is read only through the derivation seam, so an incognito or temporary
chat (or one that cannot be read right now) is never written out, and the write holds
the same transcripts so a chat made incognito since the read is not written either.
Every string that reaches the file goes through the credential redactor first. The file
is created with the eval package's guarded exclusive writer, so an existing file is
never replaced unless ``--force`` asks for the guarded atomic replace.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: The most user turns a captured scenario keeps; the rest of a long chat is left out.
MAX_TURNS = 200
#: The most tool calls one turn keeps. A turn that ran a long tool loop would otherwise write
#: thousands of ``tool_order`` entries; fifty is about what a hand-edited scenario stays readable at.
MAX_TOOLS_PER_TURN = 50
#: The longest user message kept whole; redaction runs before this cut.
MAX_USER_CHARS = 4000
#: The longest scenario name, so the file name stays short.
MAX_NAME_CHARS = 60
#: The criterion every turn is seeded with; ``--judge`` scores against it, nothing else reads it.
SEEDED_CRITERION = "The reply does what the user asked in this turn, without errors."


def scenario_name(key: str) -> str:
    """A file-safe scenario name for the chat *key*: letters, digits, ``-`` and ``_``."""
    slug = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in key)
    return "captured_" + slug.strip("_")[:MAX_NAME_CHARS]


class Omitted(NamedTuple):
    """What a capture left out: unnamed tool calls, turns past ``MAX_TURNS``, cut messages,
    tool calls made in turns no captured user message opened, and calls past
    ``MAX_TOOLS_PER_TURN`` in one turn."""

    unnamed: int
    turns: int
    cut: int
    automated: int = 0
    tools: int = 0


def _clean(text: str) -> str:
    from kiro_crew.security import redact

    return redact(text)


def _turn_assertions(tools: list[str]) -> list[dict[str, Any]]:
    """The assertions a captured turn starts with: one per distinct tool, their order, a judge."""
    seeded: list[dict[str, Any]] = [
        {"type": "tool_called", "value": t} for t in dict.fromkeys(tools)
    ]
    if len(tools) > 1:
        seeded.append({"type": "tool_order", "tools": tools})
    seeded.append({"type": "judge", "value": SEEDED_CRITERION})
    return seeded


def _inject_kind(row: dict[str, Any]) -> object:
    """The ``injectKind`` an ``inject`` row's meta names, or None."""
    meta = row.get("meta")
    return meta.get("injectKind") if isinstance(meta, dict) else None


def build_scenario(rows: list[dict], name: str) -> tuple[dict[str, Any], Omitted]:
    """``(scenario, omitted)`` from a chat's transcript *rows*.

    A user row opens a turn and the tool rows after it belong to that turn; a row that
    opens a turn of another kind (a scheduled delivery, an auto-nudge, a subagent's
    completion, or a user row with no text) ends it, and the tool rows up to the next
    captured user row are counted in *omitted*, not given to the earlier turn. An
    ``inject`` row ends it only when its ``injectKind`` opens new work
    (``TURN_OPENING_INJECT_KINDS``); a refusal note or a recovery stays inside the turn,
    as the dispatcher keeps working in it. The rows
    one call leaves (its call, then its approval or refusal) count once. A tool is
    asserted only when its row recorded the tool's own name: the eval runner names a
    call from that field and never from a display title, so an assertion built from a
    title would fail on a correct run. *omitted* counts the tool rows left out, the user
    turns past ``MAX_TURNS`` (with their tool rows), the calls past ``MAX_TOOLS_PER_TURN`` in
    one turn and the messages cut to ``MAX_USER_CHARS``.
    """
    from kiro_crew.dashboard.chat_delivery import (
        STEER_STATE_REQUEUED,
        STEER_STATE_RETRACTED,
        STEER_STATE_SUPERSEDED,
    )
    from kiro_crew.dashboard.slot_persistence.turn_marker import _LOCAL_TURN_PROMPT_ROLES
    from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS

    # A steer the turn never took: moved to the queue, where its own row comes later
    # (requeued, then superseded by that row), or taken back by the person.
    not_taken = {STEER_STATE_REQUEUED, STEER_STATE_RETRACTED, STEER_STATE_SUPERSEDED}
    turns: list[dict[str, Any]] = []
    calls: list[list[str | None]] = []
    seen: list[dict[str, int]] = []
    dropped = cut = automated = over = 0
    open_turn = False
    automated_ids: set[str] = set()
    over_ids: set[str] = set()
    for row in rows:
        role = row.get("role")
        content = row.get("content")
        row_meta = row.get("meta")
        if (
            role in ("user", "nudge")
            and isinstance(row_meta, dict)
            and row_meta.get("steer") is True
            and row_meta.get("steerState") in not_taken
        ):
            # It neither opens a turn nor closes the one it was sent into.
            continue
        if role == "user" and isinstance(content, str) and content.strip():
            if len(turns) >= MAX_TURNS:
                dropped += 1
                continue
            cleaned = _clean(content)
            if len(cleaned) > MAX_USER_CHARS:
                cut += 1
            turns.append({"user": cleaned[:MAX_USER_CHARS]})
            calls.append([])
            seen.append({})
            open_turn = True
            automated_ids.clear()
            over_ids.clear()
        elif role in _LOCAL_TURN_PROMPT_ROLES and (
            role != "inject" or _inject_kind(row) in TURN_OPENING_INJECT_KINDS
        ):
            open_turn = False
        elif role == "tool" and not dropped:
            meta = row.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            if not open_turn:
                # One call's rows count once, as inside a captured turn.
                call_id = meta.get("tool_call_id")
                if not (isinstance(call_id, str) and call_id in automated_ids):
                    automated += 1
                if isinstance(call_id, str) and call_id:
                    automated_ids.add(call_id)
                continue
            tool = meta.get("tool_name")
            called = _clean(tool) if isinstance(tool, str) and tool else None
            call_id = meta.get("tool_call_id")
            # A denied or approved call writes a second row under the same call id.
            if isinstance(call_id, str) and call_id in seen[-1]:
                if called and calls[-1][seen[-1][call_id]] is None:
                    calls[-1][seen[-1][call_id]] = called
                continue
            if len(calls[-1]) >= MAX_TOOLS_PER_TURN:
                if not (isinstance(call_id, str) and call_id in over_ids):
                    over += 1
                if isinstance(call_id, str) and call_id:
                    over_ids.add(call_id)
                continue
            if isinstance(call_id, str) and call_id:
                seen[-1][call_id] = len(calls[-1])
            calls[-1].append(called)
    unnamed = 0
    for turn, made in zip(turns, calls):
        unnamed += made.count(None)
        turn["assertions"] = _turn_assertions([c for c in made if c])
    scenario = {
        "name": name,
        "description": "Captured from a recorded chat. Edit the assertions before relying on it.",
        "dimensions": ["captured"],
        "sessions": [{"name": "captured", "turns": turns}],
    }
    return scenario, Omitted(unnamed, dropped, cut, automated, over)


def _find_transcript_key(log: ConversationLog, session_key: str) -> str | None:
    """The transcript key *session_key* names, spelled as it was given or as a chat tab, else None."""
    from kiro_crew.dashboard.chat_utils_slots import slot_transcript_key
    from kiro_crew.dashboard.state import _normalize_slot_key

    for candidate in (session_key, slot_transcript_key(_normalize_slot_key(session_key))):
        if log.has_log(candidate):
            return candidate
    return None


def _publish(out: Path, text: str, *, force: bool) -> None:
    """Write *text* to *out*: replace it with ``force``, else create it or refuse.

    Without ``force`` the file is created exclusively, so a file that appeared after the
    caller's check (another capture to the same path) is refused instead of replaced.
    """
    import os

    from kiro_crew.eval.bench import safepath

    if force:
        safepath.write_text_atomic_nofollow(out, text, what="scenario file")
        return
    fd = safepath.open_write_nofollow(out, what="scenario file")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        out.unlink(missing_ok=True)
        raise


def run_capture(args: argparse.Namespace, log: ConversationLog | None = None) -> int:
    """Run ``kirocrew eval capture KEY [--out FILE] [--force]``; the exit code.

    Writes nothing, and says why on stderr, when the chat is unknown, restricted, busy or
    has no user turn, or when the destination exists and ``--force`` was not given. An
    ``--out`` that names an existing folder gets the default file name inside it.
    """
    from kiro_crew.eval.bench.errors import BenchRefusal
    from kiro_crew.eval.bench.safepath import guard_write_path
    from kiro_crew.history import ConversationLog, TranscriptBusy, TranscriptWithheld
    from kiro_crew.terminal_safe import safe_terminal_line

    keys = list(args.scenarios[1:])
    if len(keys) != 1:
        print(
            "Error: usage: kirocrew eval capture <session-key> [--out FILE] [--force]",
            file=sys.stderr,
        )
        return 1
    session_key = keys[0]
    shown = safe_terminal_line(session_key)
    busy = f"Warning: the transcript of '{shown}' is busy; nothing captured. Try again."
    withheld = (
        f"Warning: '{shown}' is an incognito or temporary chat, or cannot be read; "
        "nothing captured."
    )
    log = log or ConversationLog()
    transcript_key = _find_transcript_key(log, session_key)
    if transcript_key is None:
        print(f"Error: no recorded chat named '{shown}'.", file=sys.stderr)
        return 1
    try:
        rows, chain = log.derive_messages_chained_full_with_keys(transcript_key)
    except TranscriptBusy:
        print(busy, file=sys.stderr)
        return 1
    except TranscriptWithheld:
        print(withheld, file=sys.stderr)
        return 1
    name = scenario_name(transcript_key)
    scenario, omitted = build_scenario(rows, name)
    if not scenario["sessions"][0]["turns"]:
        print(f"Error: '{shown}' has no user messages to capture.", file=sys.stderr)
        return 1
    out = Path(getattr(args, "out", None) or Path("evals") / f"{name}.json").expanduser()
    if out.is_dir():
        out = out / f"{name}.json"
    force = bool(getattr(args, "eval_force", False))
    exists = safe_terminal_line(f"Error: '{out}' exists; pass --force to replace it.")
    if out.exists() and not force:
        print(exists, file=sys.stderr)
        return 1
    try:
        # One file is written, so only that file's own place is judged: a folder that
        # merely holds a protected tree (the home folder) is a fine place for it.
        guard_write_path(out, what="scenario file")
        # The chain the rows came from is held and re-checked through the write, so a
        # chat made incognito or temporary since they were read is not written out.
        with log.publication_hold(transcript_key, expected_keys=chain):
            out.parent.mkdir(parents=True, exist_ok=True)
            _publish(out, json.dumps(scenario, indent=2) + "\n", force=force)
    except TranscriptBusy:
        print(busy, file=sys.stderr)
        return 1
    except TranscriptWithheld:
        print(withheld, file=sys.stderr)
        return 1
    except (BenchRefusal, OSError) as exc:
        # Another capture created the file after the check above; its file is kept.
        lost_race = not force and out.exists() and not out.is_symlink()
        print(exists if lost_race else f"Error: {safe_terminal_line(str(exc))}", file=sys.stderr)
        return 1
    turns = scenario["sessions"][0]["turns"]
    print(safe_terminal_line(f"Captured {len(turns)} turn(s) to {out}"))
    if omitted.unnamed:
        print(
            f"{omitted.unnamed} tool call(s) recorded no tool name and were left out of the "
            "assertions."
        )
    limits = []
    if omitted.turns:
        limits.append(f"left out {omitted.turns} user turn(s) past the first {MAX_TURNS}")
    if omitted.cut:
        limits.append(f"cut {omitted.cut} message(s) to {MAX_USER_CHARS} characters")
    if omitted.automated:
        limits.append(f"left out {omitted.automated} tool call(s) made in automated turns")
    if omitted.tools:
        limits.append(
            f"left out {omitted.tools} tool call(s) past the first {MAX_TOOLS_PER_TURN} in a turn"
        )
    if limits:
        print("The capture limits " + " and ".join(limits) + ".")
    print(safe_terminal_line(f"Edit the assertions, then run: kirocrew eval --file {out}"))
    return 0
