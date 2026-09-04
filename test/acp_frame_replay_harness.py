"""Replay recorded ACP frames into the events the dispatch layer makes of them.

Shared by ``test/test_acp_frame_replay.py``, which asserts the result against a
committed snapshot, and ``scripts/update_acp_frame_snapshots.py``, which writes
that snapshot. The split exists because a test must not create files in the repo
that outlive the run (AUTOSDE ``no-test-side-effects``), so the WRITER is a
script and the test stays read-only.

Not named ``test_*``, so pytest does not collect it. It lives under ``test/``
rather than ``src/kiro_crew/testing/`` on purpose: it imports
``kiro_crew.acp._dispatch``, and a module under ``src/`` outside ``acp/``,
``agent_sdk/`` and ``providers/`` doing that adds an edge to the
``check_agent_sdk_boundary.py`` baseline, which this change is not allowed to
grow.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from kiro_crew.acp._dispatch import (
    agent_version_from_init,
    build_permission_event,
    classify_notification,
    parse_claude_compaction_notice,
    parse_metadata,
    parse_prompt_token_usage,
    parse_session_modes,
    parse_session_update,
    parse_text_chunk,
    parse_todo_snapshot,
    parse_usage_cost,
    parse_usage_update,
)
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND


def fixture_dir_name(backend: str) -> str:
    """Return the corpus directory name for a backend id.

    The kiro-cli backend's id is the empty string, which is not a filename, so
    the mapping cannot be identity. It reuses ``POLICY_ID_BY_BACKEND`` -- the
    module that already had to give every backend id a writable wire name --
    rather than introducing a second table that could disagree with it. Every
    caller passes an id from ``ACP_BACKENDS_KNOWN``, all of which have an entry,
    so an unknown id is a programming error and raises ``KeyError`` rather than
    being mapped to a directory that does not exist.
    """
    return POLICY_ID_BY_BACKEND[backend]


#: The corpus root. Resolved from this file so a script and a test agree.
CORPUS = Path(__file__).resolve().parent / "fixtures" / "acp_frames"

#: Recording provenance values a fixture's ``_meta`` may declare. "live" means it
#: came off a real backend's wire; "synthesized" means it was written from the
#: shapes this repo parses. The corpus is only trustworthy if the difference is
#: stated per file rather than assumed, so an unknown value fails.
RECORDED_KINDS = frozenset({"live", "synthesized"})

#: Every action ``classify_notification`` can return. Pinned as a tuple, not
#: derived, because the point is to fail when production grows an action
#: :func:`replay_frames` does not replay — deriving it from the same source would
#: make the check agree with itself.
HANDLED_ACTIONS = (
    "permission",
    "steer",
    "update",
    "metadata",
    "compaction",
    "clear",
    "agent_switched",
    "mcp_oauth_request",
    "mcp_server_initialized",
    "mcp_server_init_failure",
    "subagent_list",
    "subagent_activity",
    "server_request_unknown",
    "skip",
)


# ── corpus discovery ────────────────────────────────────────────────────────


def backend_dirs() -> list[Path]:
    if not CORPUS.is_dir():
        return []
    return sorted(p for p in CORPUS.iterdir() if p.is_dir())


def fixture_files() -> list[Path]:
    return sorted(path for d in backend_dirs() for path in sorted(d.glob("*.jsonl")))


def fixture_ids() -> list[str]:
    return [f"{path.parent.name}/{path.name}" for path in fixture_files()]


def expected_path(fixture: Path) -> Path:
    return fixture.with_suffix(".expected.json")


class FixtureError(Exception):
    """A fixture file is not shaped like a fixture."""


def read_fixture(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split a fixture into its ``_meta`` header and its frames.

    The header is a frame-shaped line carrying only ``_meta``, so a fixture is
    still valid JSONL and a reader that does not care about provenance can skip
    it on the same ``_meta`` key it would skip any other non-frame line.
    """
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise FixtureError(f"{path.name} is empty")
    header = json.loads(lines[0])
    if not isinstance(header, dict) or "_meta" not in header:
        raise FixtureError(
            f"{path.name}: the first line must be a {{'_meta': ...}} provenance header, "
            "so a reviewer can tell a live capture from a synthesized one"
        )
    frames: list[dict[str, Any]] = []
    for number, line in enumerate(lines[1:], start=2):
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise FixtureError(
                f"{path.name}:{number}: a frame must be a JSON object, got "
                f"{type(frame).__name__}; JSON-RPC frames are objects, so this line "
                "is not something a backend wrote"
            )
        frames.append(frame)
    return header["_meta"], frames


# ── replay ──────────────────────────────────────────────────────────────────


def event_dict(event: AcpEvent) -> dict[str, Any]:
    """Serialize an event, dropping fields still at their default.

    Defaults are dropped so a snapshot shows what a frame actually produced
    rather than 30 zero-valued fields, and so ADDING a field to ``AcpEvent``
    does not rewrite every expected file. Removing or renaming one that a
    fixture exercises still fails, which is the direction that matters.
    """
    defaults = {f.name: f.default for f in dataclasses.fields(AcpEvent)}
    out: dict[str, Any] = {}
    for name, value in dataclasses.asdict(event).items():
        if name == "usage":
            usage = {k: v for k, v in value.items() if v}
            if usage:
                out[name] = usage
            continue
        if value == defaults.get(name):
            continue
        if value in (None, "", 0, 0.0, False, [], {}):
            continue
        out[name] = value
    return out


def _response_summary(msg: JsonRpcMessage) -> dict[str, Any]:
    """Summarize a response frame through the parsers its awaiting caller uses.

    A response carries no method, so which request it answers is not knowable
    from the frame alone — the runtime matches it by id. Rather than fake that
    bookkeeping, every response parser runs and only the ones that find
    something contribute, which is shape-driven and stable.
    """
    result = msg.result if isinstance(msg.result, dict) else {}
    summary: dict[str, Any] = {"frame": "response", "id": msg.id}
    if msg.error is not None:
        summary["error"] = msg.error
        return summary

    version = agent_version_from_init(result)
    if version:
        summary["agent_version"] = version
    if "protocolVersion" in result:
        summary["protocol_version"] = result["protocolVersion"]
    if "agentCapabilities" in result:
        summary["agent_capabilities"] = result["agentCapabilities"]
    if "sessionId" in result:
        summary["session_id"] = result["sessionId"]

    mode_ids, current_mode, advertised = parse_session_modes(result)
    if advertised or mode_ids or current_mode:
        summary["modes"] = {
            "ids": mode_ids,
            "current": current_mode,
            "advertised": advertised,
        }
    if "configOptions" in result:
        summary["config_options"] = result["configOptions"]

    tokens = parse_prompt_token_usage(result)
    if tokens is not None:
        summary["prompt_tokens"] = list(tokens)
    if "stopReason" in result:
        summary["stop_reason"] = result["stopReason"]
    return summary


def replay_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn a raw frame sequence into the snapshot form.

    Mirrors the dispatch the two reader loops perform: classify, then hand the
    frame to the parser for its action. Caches are per replay, matching the
    runtime's per-session caches, so a ``tool_call_update`` recovers the input
    its originating ``tool_call`` recorded.
    """
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "tool_input_redacted_cache": {},
        "cache_scope": "replay",
    }

    out: list[dict[str, Any]] = []
    for raw in frames:
        msg = JsonRpcMessage.from_dict(raw)

        # A response (id, and a result or error, but no method) is routed by id
        # to its awaiting caller, never through classify_notification.
        if msg.method is None and msg.id is not None and (msg.result is not None or msg.error):
            out.append(_response_summary(msg))
            continue

        action = classify_notification(msg)
        entry: dict[str, Any] = {"frame": msg.method or "", "action": action}
        params = msg.params if isinstance(msg.params, dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}

        if action == "permission":
            event, options = build_permission_event(msg, **caches)
            entry["events"] = [event_dict(event)]
            entry["recorded_options"] = options
        elif action in ("update", "subagent_activity"):
            discriminant = update.get("sessionUpdate", "")
            entry["session_update"] = discriminant
            entry["events"] = [event_dict(e) for e in parse_session_update(update, **caches)]
            if discriminant in ("agent_message_chunk", "agent_thought_chunk"):
                text, thinking = parse_text_chunk(update)
                entry["text_chunk"] = {"text": text, "thinking": thinking}
                if text:
                    notice = parse_claude_compaction_notice(text)
                    if notice is not None:
                        entry["compaction_notice"] = list(notice)
            if discriminant == "usage_update":
                entry["usage"] = list(parse_usage_update(update))
                entry["usage_cost"] = parse_usage_cost(update)
            if discriminant == "tool_call_update":
                todo = parse_todo_snapshot(update)
                if todo is not None:
                    entry["todo"] = todo
        elif action == "steer":
            entry["session_update"] = update.get("sessionUpdate", "")
        elif action == "metadata":
            context_pct, credits = parse_metadata(params)
            entry["metadata"] = {"context_pct": context_pct, "credits": credits}
        else:
            # compaction / clear / agent_switched / mcp_* / subagent_list /
            # server_request_unknown / skip carry no parser in _dispatch; the
            # handlers read params directly, so the params ARE the behaviour.
            if params:
                entry["params"] = params
        out.append(entry)
    return out


def snapshot_json(replayed: list[dict[str, Any]]) -> str:
    """Render a replay result in the stable on-disk form."""
    return json.dumps(replayed, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
