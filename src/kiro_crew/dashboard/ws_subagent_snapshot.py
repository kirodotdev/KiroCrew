"""The ``subagent_snapshot`` replay frames a dashboard reconnect receives.

Moved out of ``ws.py`` (file line cap); ``ws`` re-exports both names.
"""

from __future__ import annotations

import time
from typing import Any

from kiro_crew.dashboard.chat_utils import subagent_event_slot
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def build_subagent_snapshot(a: Any, *, now: float | None = None) -> dict:
    """Build the ``subagent_snapshot`` replay frame's ``data`` for one agent.

    Separate from the reconnect handler so the frame's CONTENTS can be asserted
    directly — the handler around it needs a live aiohttp WS, so a missing field
    there is easy to miss.

    ``idle_secs`` is the span that justifies the stall badge. The live
    ``subagent_stalled`` event carries it and this replay frame must too:
    without it ANY reconnect during an active stall degrades the row to the
    plain "no activity" wording, which is only meant for a gateway too old to
    send the field.

    It is computed at replay time rather than replaying the original transition
    value: by reconnect the agent has usually been idle longer than it was when
    flagged, and ``last_activity`` is already the field the reaper itself
    measures. Clamped at 0 so a clock adjustment cannot produce a negative span.

    The key is OMITTED entirely when the agent is not stalled, so a client
    cannot attach an idle span to a healthy row — the reducer pairs the span
    with the flag and would otherwise have to defend against the mismatch.
    """
    ts = time.time() if now is None else now

    def _r(t: str) -> str:
        t, _ = redact_exfiltration_urls(t)
        t, _ = redact_credentials(t)
        return t

    data: dict = {
        "id": a.id,
        "slot": subagent_event_slot(a.parent_session_key),
        # The sub-agent's OWN session key (where it writes its ctx_blocks /
        # token rows), so a client can fetch this node's own context-trace and
        # render its window composition. Mirrors the run key derived in
        # SubagentManager._run: `conversation_key or subagent:<id>`.
        "child_session": getattr(a, "conversation_key", "") or f"subagent:{a.id}",
        "task": _r(a.task),
        "agent": _r(getattr(a, "run_agent", a.agent)),
        "label": _r(getattr(a, "label", "") or ""),
        "model": a.resolved_model,
        "requested_model": _r(a.requested_model),
        "streaming": _r(a.streaming_text),
        "last_tool": _r(a.last_tool),
        "last_tool_call_id": _r(getattr(a, "last_tool_call_id", "") or ""),
        "tool_count": a.tool_count,
        "stalled": a.stalled,
    }
    # The wave this agent belongs to, when it is part of one. Mirrors the live
    # ``_subagent_event`` stamp so a reconnect replay keeps the panel's batch
    # chip; omitted for a solo spawn so the client reads "no wave", not "".
    _bid = getattr(a, "batch_id", "")
    if isinstance(_bid, str) and _bid:
        data["batch_id"] = _bid
    if a.stalled:
        data["idle_secs"] = max(0, int(ts - a.last_activity))
    data["started"] = a.started
    return data


def live_remote_snapshot_frames(subagents: Any) -> list[dict]:
    """Reconnect ``subagent_snapshot`` frames for the remote runs still running.

    Without them a refresh during a remote run drops its card and its stop
    control until the run ends. ``child_session`` is the key the live spawn
    frame carried for the run. A record that cannot be framed is skipped, as
    the local replay loops do.
    """
    frames: list[dict] = []
    for a in getattr(subagents, "external_agents", None) or ():
        if a.done:
            continue
        try:
            data = build_subagent_snapshot(a)
            data["child_session"] = f"remote:{a.instance_id}:{a.remote_id}"
        except Exception:
            continue
        frames.append({"type": "subagent_snapshot", "data": data})
    return frames
