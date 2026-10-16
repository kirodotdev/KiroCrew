"""The sub-agent read tools: ``spawn_list`` and ``spawn_status``.

``spawn_list`` lists the runs and the accepted spawns still waiting to start, narrowed to
the agents the caller may spawn; ``spawn_status`` reports one run. :func:`_queued_why`
words why a queued spawn waits, for both reads and for the batch spawn's reply.

This module is an owner of :mod:`kiro_crew.mcp_tools.spawn`, which serves every name here
and stays the import path and the patch seam: a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). It never imports the facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.spawn_parts.schemas import _parent_allowlist_filter
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.subagent import (
    format_subagent_usage,
    is_internal_agent_spec,
    visible_agent_names,
)
from kiro_crew.subagent_wait_reasons import queued_wait_text


def _queued_why(record: Mapping[str, Any]) -> str:
    """Why a queued spawn waits, redacted (:func:`queued_wait_text`)."""
    return redact(queued_wait_text(record))


def spawn_list(name: str, args: dict[str, Any]) -> str:
    d = mcp_core._get("/api/spawn?queued=1")
    agents = d.get("agents", [])
    # Accepted spawns with no run yet (gate-deferred, or waiting for a slot);
    # the gateway lists them apart from the runs.
    queued = d.get("queued") or []
    if not isinstance(queued, list):
        queued = []

    def _redact(text: str) -> str:
        return redact(text)

    lines: list[str] = []
    not_started = False
    for q in queued:
        if isinstance(q, Mapping) and q.get("id"):
            resuming = q.get("resuming") is True
            not_started = not_started or not resuming
            tag = "resuming" if resuming else "not started"
            lines.append(
                f"{q['id']}  [queued] ({tag}: {_queued_why(q)})  "
                f"{_redact(str(q.get('task') or ''))[:60]}"
            )
    if not_started:
        lines.append("(a not-started run you no longer need: cancel it with spawn_release)")
    if d.get("queued_truncated") is True:
        # The gateway listed only the oldest queued spawns, or could not read
        # them all; the rest exist too.
        lines.append(
            "(the queued list is partial: more spawns may be queued than listed here; "
            "they are accepted and start on their own -- do not spawn them again)"
        )
    if not agents and not lines:
        lines.append("No subagents running.")
    else:
        for a in agents:
            # A run parked on an unanswered spawn-approval prompt has launched
            # no process and produced no turn, so reporting it as "running" is
            # actively misleading to a caller ``spawn_run``'s receipt points here
            # ("Check spawn_list", in ``mcp_tools.spawn``) -- it reads as work in progress when
            # the truth is that a human has not approved it yet.
            if a.get("done"):
                status = "done"
            elif a.get("awaiting_approval"):
                status = "awaiting-approval"
            else:
                status = "running"
            err = f" error: {_redact(a['error'])}" if a.get("error") else ""
            progress = ""
            if not a.get("done"):
                turns = a.get("turns", 0)
                tool = _redact(a.get("last_tool", ""))
                elapsed = a.get("elapsed", 0)
                parts = [f"{elapsed}s"]
                if turns:
                    parts.append(f"{turns} turns")
                if tool:
                    parts.append(tool)
                progress = f" ({', '.join(parts)})"
            _withheld = a.get("context_withheld") or []
            scope = f"  ctx-withheld: {','.join(_withheld)}" if _withheld else ""
            lines.append(f"{a['id']}  [{status}]{err}{progress}{scope}  {_redact(a['task'])[:60]}")
    # Always append available agents (fresh read from disk). Same grammar filter and
    # redaction as the two bounded rosters (``_agent_roster_hint`` and the unknown-agent
    # refusal's), via the shared helper: this output is a
    # tool RESULT, so it lands in the same model context, and a spec's ``name``
    # field arrives unvalidated.
    #
    # Unbounded and ``exclude=()`` on purpose, and this is the one surface where
    # that is right: both bounded rosters cap themselves and point the caller HERE
    # ("call spawn_list", "which lists them all"), so withholding names from this
    # listing would falsify what they promise. The reserved pair is not suggested
    # elsewhere because it is reached by omitting ``agent`` -- but it is still a
    # name the gateway accepts, so a full listing shows it. Kiro Crew's own
    # generated specs are NOT: the gateway refuses them, so even the full listing
    # leaves them out (``is_internal_agent_spec``).
    try:
        names, restricted = _parent_allowlist_filter(
            a.name or "" for a in mcp_core.list_agents() if not is_internal_agent_spec(a)
        )
        names, _ = visible_agent_names(names, exclude=())
        if names:
            lines.append(f"\nAvailable agents: {', '.join(names)}")
        if restricted:
            lines.append(
                "(restricted to this agent's toolsSettings.subagent.availableAgents; "
                + ("other installed agents are" if names else "every installed agent is")
                + " refused at spawn)"
            )
    except Exception:
        pass  # list_agents failure is non-critical
    return "\n".join(lines)


def spawn_status(name: str, args: dict[str, Any]) -> str:
    agent_id = args.get("agent_id", "")
    if not agent_id or not agent_id.isalnum():
        return "Error: invalid agent_id"
    # Optional paged / filtered read of the retained transcript.
    spawn_params: dict[str, str] = {}
    offset = args.get("offset")
    limit = args.get("limit")
    grep = args.get("grep")
    if isinstance(offset, int) and offset > 0:
        spawn_params["offset"] = str(offset)
    if isinstance(limit, int) and limit > 0:
        spawn_params["limit"] = str(limit)
    if isinstance(grep, str) and grep.strip():
        spawn_params["grep"] = grep
    path = f"/api/spawn/{agent_id}"
    if spawn_params:
        path += "?" + urlencode(spawn_params)
    d = mcp_core._get(path)
    usage = format_subagent_usage(d.get("credits"), d.get("elapsed"))
    # ``error`` means two different things here. On a payload that is not a
    # finished run (a failed lookup or a transport error, such as
    # ``{"error": "HTTP 404"}``) it is the whole answer. On a finished run
    # (``done`` is True) it is the reason the run ended badly, sent next to the
    # transcript the run retained; a timed-out or reaped run keeps its partial
    # output on purpose. That transcript must stay readable, so the reason
    # becomes a header instead of replacing the output.
    ended_with_error = d.get("done") is True and bool(d.get("error"))
    if d.get("error") and not ended_with_error:
        error = f"Error: {d['error']}"
        return f"[usage: {usage}]\n{error}" if usage else error

    meta = d.get("result_meta")
    if isinstance(meta, dict) and meta.get("grep_error"):
        return f"Error: {meta['grep_error']}"

    running = d.get("done") is False
    # Present-only, and api_spawn_status sets it ONLY while the run is parked on
    # the SPAWN-approval gate (never entered execution): no process, no turn.
    # spawn_list renders that "awaiting-approval" and the CLI waiter says
    # "approve it ... to start this run", so this tool must not report work
    # under way for it either.
    awaiting = running and d.get("awaiting_approval") is True
    # Accepted but not started (the gate deferred it, or it waits for a slot).
    # It is real accepted work: re-spawning it would run it twice.
    queued = running and d.get("queued") is True
    result = d.get("result") or ""
    if queued and d.get("resuming") is True:
        # It ran before and waits to go on: never "not started".
        result = (
            f"(queued — {_queued_why(d)}. It continues on its own and its completion "
            "event arrives as usual; do not spawn it again)"
        )
    elif queued:
        result = (
            f"(not started — queued: {_queued_why(d)}. It starts on its own once that "
            "clears and its completion event arrives as usual; do not spawn it again. "
            "If you no longer need it, cancel it with spawn_release)"
        )
    elif running and not result:
        turns = d.get("turns", 0)
        if awaiting:
            result = (
                "(not started — waiting for spawn approval; approve it in the "
                "dashboard (Approvals) to start this run)"
            )
        elif isinstance(meta, dict) and meta.get("total_lines", 0) > 0:
            result = (
                f"(no partial transcript lines in this view — {turns} turns so far; "
                "adjust offset/grep to inspect the running transcript)"
            )
        else:
            result = (
                f"(no streamed text yet — {turns} turns so far; "
                "transcript arrives with the completion event)"
            )
    elif not result:
        result = "_No result._"
    result, _ = redact_exfiltration_urls(result)
    result, _ = redact_credentials(result)

    if isinstance(meta, dict) and meta:
        # Paged/grepped read — prepend a compact header so the LLM knows how
        # much it saw and how to continue, without re-reading the whole file.
        hdr: list[str] = []
        if usage:
            hdr.append(f"usage: {usage}")
        total = meta.get("total_lines", "?")
        if "matched_lines" in meta:
            hdr.append(f"{meta['matched_lines']} line(s) matched grep of {total} total")
        start = meta.get("offset", 0)
        returned = meta.get("returned_lines", 0)
        hdr.append(f"showing lines {start}-{start + returned} of {total}")
        if meta.get("has_more"):
            hdr.append(f"more available — call again with offset={start + returned}")
        result = f"[{' | '.join(hdr)}]\n{result}"

    if running:
        if queued:
            state = "QUEUED"
        elif awaiting:
            state = "AWAITING-APPROVAL"
        else:
            state = "RUNNING"
        status = [state]
        if "elapsed" in d:
            status.append(f"{d['elapsed']}s")
        if "turns" in d:
            status.append(f"{d['turns']} turns")
        if d.get("last_tool"):
            status.append(f"last tool: {d['last_tool']}")
        header = f"[{' · '.join(status)}]"
        header, _ = redact_exfiltration_urls(header)
        header, _ = redact_credentials(header)
        return f"{header}\n{result}"
    if isinstance(meta, dict) and meta:
        out = result
    else:
        out = f"[usage: {usage}]\n{result}" if usage else result
    if ended_with_error:
        reason, _ = redact_exfiltration_urls(str(d["error"]))
        reason, _ = redact_credentials(reason)
        out = f"[ENDED WITH ERROR · {reason}]\n{out}"
    return out
