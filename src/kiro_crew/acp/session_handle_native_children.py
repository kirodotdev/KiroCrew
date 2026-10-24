"""Harness-native child sessions.

The KAS sub-agent roster and child chunks, the codex-acp announced-child roster, the
child-session bookkeeping, the resume refusal and the uncharged report (harness-parity
row H16).

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_announced_subagent_roster``, ``_kas_subagent_roster``,
``_legacy_subagent_tool_ids``, ``_native_child_overflow``, ``_native_child_sids``,
``_prompt_or_tool_seen``.

Instance fields read: ``_session_id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.acp import kas_wire
from kiro_crew.acp._dispatch import parse_text_chunk, redact_text
from kiro_crew.acp.types import (
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    UPDATE_SUBAGENT_SPAWNED,
    UPDATE_SUBAGENT_STATE,
    AcpEvent,
)
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle


# Harness-native subtask boundary (RFC §14.8, SPEC-ADDENDUM §7, parity row H16).
#: Typed refusal prefix for a ``spawn_continue``-style resume that names a
#: native child session id: the child has no conversation, task row or
#: runtime of its own, so the only resumable identity is its parent's.
NATIVE_CHILD_NOT_RESUMABLE = "native_child_not_resumable"
#: ``HostBudget.snapshot()["uncharged"]`` key under which observed native
#: children are REPORTED — never charged (they live inside the parent's
#: already-charged runtime process).
NATIVE_CHILDREN_UNCHARGED_KIND = "native_children"
#: Bound on distinct native child ids remembered per turn. Ids are
#: backend-controlled bytes; past the cap they are counted in
#: ``native_child_overflow`` rather than stored, so a flooding harness cannot
#: grow gateway memory through this set. The same number bounds
#: ``AcpRuntime._subagent_sessions``: recognition there decides a child's
#: approvals, and a roster the handle counts in full but the runtime recognises
#: only part of would split one announced roster into two governance classes.
NATIVE_CHILD_ROSTER_CAP = 4096
#: Bound on each backend-authored display string STORED in a native-child
#: roster row (name, title, status). The row count cap bounds memory only when
#: every field is bounded too — 4096 rows of an unbounded title is unbounded.
#: Far above any real sub-agent name, so a clipped label means a hostile or
#: broken payload rather than a long one.
NATIVE_CHILD_LABEL_CAP = 512


def _bounded_label(value: object) -> str:
    """One backend-authored display string, bounded for STORAGE.

    Applied at the point a native-child roster row is written, because that is
    the only place the string is RETAINED: a per-row byte bound plus the row
    cap is what makes the roster's memory claim true, and a bound applied at a
    render site downstream would leave the store itself unbounded.
    """
    return str(value)[:NATIVE_CHILD_LABEL_CAP]


def _handle_kas_subagent(self: AcpSessionHandle, update: dict) -> list[AcpEvent] | None:
    """Route KAS PARENT sub-agent frames to EVENT_SUBAGENT_LIST.

    Returns a list of events when the frame is a PARENT sub-agent lifecycle
    frame (``kind:"agent-subtask"`` or ``pipeline``), or ``None`` when it is
    a child nested tool or an ordinary frame that should fall through to the
    shared parser (so caches populate and tool events render).
    """
    kiro = kas_wire.kiro_meta(update)
    if kiro is None:
        return None

    agent_subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
    pipeline = kiro.get(kas_wire.FIELD_PIPELINE)

    if not agent_subtask_id and not pipeline:
        return None

    # ANY frame carrying subtask lineage means the wave has started: a
    # spawned child can mutate state before its first activity frame is
    # observed, so the parent lifecycle frame itself closes the
    # registration-throttle window. Placed at the acceptance gate so the
    # pipeline arm, the parent arm and the child fall-through all close it.
    self._prompt_or_tool_seen = True

    # Pipeline frame: one entry per stage.
    if isinstance(pipeline, dict):
        stages = pipeline.get(kas_wire.FIELD_STAGES)
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                s_id = stage.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
                if not isinstance(s_id, str) or not s_id:
                    continue
                s_status = _bounded_label(stage.get("status") or "in_progress")
                s_name = _bounded_label(stage.get("name") or stage.get("role") or "")
                # Native-subtask residency seam (RFC §14.8): counted on
                # the parent, never charged — see native_child_sessions.
                # The set's admission answer also gates the display row, so
                # this dict cannot hold an id the count refused (over-long,
                # the parent's own, or past NATIVE_CHILD_ROSTER_CAP) and
                # cannot outgrow the cap within a turn.
                if not self._note_native_child(s_id):
                    continue
                self._kas_subagent_roster[s_id] = {
                    "sessionId": s_id,
                    "sessionName": s_name,
                    "agentName": s_name,
                    "initialQuery": s_name,
                    "status": {"type": s_status, "message": ""},
                }
        return [
            AcpEvent(
                kind=EVENT_SUBAGENT_LIST,
                subagents=list(self._kas_subagent_roster.values()),
            )
        ]

    # Individual agent-subtask frame (kind == "agent-subtask") → PARENT.
    is_parent = kiro.get(kas_wire.FIELD_KIND) == kas_wire.KIND_AGENT_SUBTASK

    if is_parent:
        subtask_id = str(agent_subtask_id)
        status = _bounded_label(update.get("status") or "in_progress")
        title = _bounded_label(update.get("title") or "")
        name = title.replace("Sub-agent: ", "") if title.startswith("Sub-agent: ") else title
        # Native-subtask residency seam (RFC §14.8): counted on the
        # parent, never charged — see native_child_sessions. Same
        # admission answer gates the row as in the stage loop above; the
        # frame is still a PARENT sub-agent frame either way, so the list
        # event is emitted (returning None would re-render it as an
        # ordinary tool call).
        if self._note_native_child(subtask_id):
            self._kas_subagent_roster[subtask_id] = {
                "sessionId": subtask_id,
                "sessionName": title,
                "agentName": name,
                "initialQuery": title,
                "status": {"type": status, "message": ""},
            }
        return [
            AcpEvent(
                kind=EVENT_SUBAGENT_LIST,
                subagents=list(self._kas_subagent_roster.values()),
            )
        ]

    # Child nested tool_call/tool_call_update (has agentSubtaskId but NOT
    # kind:"agent-subtask" or pipeline) → return None so the caller falls
    # through to parse_session_update (populates caches + renders tool
    # events). The caller prepends the activity prefix separately.
    return None


def _handle_kas_subagent_chunk(self: AcpSessionHandle, update: dict) -> list[AcpEvent] | None:
    """Route KAS agent_message_chunk with agentSubtaskId to activity.

    Returns a list when the chunk belongs to a child sub-agent, else None
    (fall through to normal chunk handling).
    """
    kiro = kas_wire.kiro_meta(update)
    if kiro is None:
        return None
    subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
    if not isinstance(subtask_id, str) or not subtask_id:
        return None
    # Must NOT have kind:"agent-subtask" or pipeline — those are parent frames
    if kiro.get(kas_wire.FIELD_KIND) == kas_wire.KIND_AGENT_SUBTASK or kiro.get(
        kas_wire.FIELD_PIPELINE
    ):
        return None
    text, _thinking = parse_text_chunk(update)
    if not text or _thinking:
        # A child's private reasoning (thinking/reasoning content) must not
        # surface as visible sub-agent activity — parity with the kiro native
        # subagent path, which only forwards non-thinking agent_message_chunk.
        return []
    # Observed child output: the wave did work, so the registration-throttle
    # window closes — the same closure the tool prefix builder applies.
    self._prompt_or_tool_seen = True
    return [
        AcpEvent(
            kind=EVENT_SUBAGENT_ACTIVITY,
            sub_session_id=subtask_id,
            text=redact_text(text),
        )
    ]


def _build_child_tool_activity_prefix(self: AcpSessionHandle, update: dict) -> list[AcpEvent]:
    """Build an EVENT_SUBAGENT_ACTIVITY prefix for a child nested tool frame.

    Called when ``_handle_kas_subagent`` returns None (child tool) so the
    activity attribution is emitted BEFORE the tool events from
    ``parse_session_update``.
    """
    kiro = kas_wire.kiro_meta(update)
    if kiro is None:
        return []
    subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
    if not isinstance(subtask_id, str) or not subtask_id:
        return []
    tool_call_id = str(update.get("toolCallId") or "")
    if not tool_call_id:
        return []
    # A KAS child's nested tool call is this session's side effect for
    # replay purposes — the same closure every other child tool route
    # applies. Latched here, in the one builder every caller shares, so a
    # new call site cannot forget it.
    self._prompt_or_tool_seen = True
    title = redact_text(str(update.get("title") or ""))
    return [
        AcpEvent(
            kind=EVENT_SUBAGENT_ACTIVITY,
            sub_session_id=subtask_id,
            tool_call_id=tool_call_id,
            title=title,
        )
    ]


#: Roster status types for the lifecycle states codex-acp reports. Every value
#: that is not in ``_native_subagent_sync``'s running set closes the card, and a
#: ``failed`` with a message shows that message as the card's error.
_RUNNING = {"type": "working", "message": ""}
_COMPLETED = {"type": "completed", "message": ""}


def _failed(message: str) -> dict[str, str]:
    return {"type": "failed", "message": _bounded_label(message or "failed")}


#: codex-acp 2.2.x ``subagent_state_update.state`` values.
_LEGACY_STATE_STATUS: dict[str, dict[str, str]] = {
    "completed": _COMPLETED,
    "failed": _failed("failed"),
    "cancelled": _failed("cancelled"),
    # Sent for a child still open when a history replay ends: its end was not
    # observed, so the card closes without claiming it succeeded.
    "disconnected": _failed("disconnected"),
}


def _name_from_agent_path(path: str, fallback: str) -> str:
    """``/root/probe_worker`` -> ``Probe worker``, the name codex-acp itself shows."""
    leaf = path.strip().rstrip("/").rsplit("/", 1)[-1].strip()
    words = " ".join(leaf.replace("_", " ").replace("-", " ").split())
    return words[:1].upper() + words[1:] if words else fallback


def _announced_row(
    self: AcpSessionHandle, child_sid: str, parent_sid: str, name: str, task: str
) -> dict[str, Any] | None:
    """Get or create the roster row for ``child_sid``; None when the cap refuses it."""
    row = self._announced_subagent_roster.get(child_sid)
    if row is not None:
        return row
    if not self._note_native_child(child_sid):
        return None
    name = _bounded_label(name or f"Agent {child_sid[-8:]}")
    row = {
        "sessionId": child_sid,
        "sessionName": name,
        "agentName": name,
        "initialQuery": _bounded_label(task or f"Delegated task for {name}"),
        "status": dict(_RUNNING),
        # The session the child was announced on: this handle's own for a
        # direct child, a child's for a nested one.
        "parentSessionId": "" if parent_sid == self._session_id else parent_sid,
    }
    self._announced_subagent_roster[child_sid] = row
    return row


def _roster_event(self: AcpSessionHandle) -> list[AcpEvent]:
    # Observed children mean the wave started: a spawned child can act before
    # its first frame reaches this handle, so the announcement itself closes
    # the registration-throttle window, as the KAS lifecycle frame does.
    self._prompt_or_tool_seen = True
    return [
        AcpEvent(
            kind=EVENT_SUBAGENT_LIST,
            subagents=[dict(row) for row in self._announced_subagent_roster.values()],
        )
    ]


def _handle_announced_subagent(
    self: AcpSessionHandle, update: dict, frame_sid: str
) -> list[AcpEvent]:
    """Turn a native-subagent lifecycle ``session/update`` into the card roster.

    codex-acp announces each native child on the session that spawned it and
    reports its state there; the child's own work arrives under the child's id
    and is re-tagged as activity by ``_handle_update``. The shape read is
    codex-acp 2.2.x's ``subagent_spawned`` / ``subagent_state_update``, keyed on
    ``subagentSessionId``.

    The roster is emitted whole, as kiro-cli's ``subagent/list_update`` is, so
    ``_native_subagent_sync`` sees one shape from every harness. A state for a
    child this turn never announced is dropped: it has no card to settle, and
    a row created from it would be unbounded by any announcement.
    """
    kind = update.get("sessionUpdate")
    parent_sid = frame_sid or self._session_id
    if kind == UPDATE_SUBAGENT_SPAWNED:
        child = update.get("subagentSessionId")
        if not isinstance(child, str) or not child:
            return []
        row = self._announced_row(
            child, parent_sid, str(update.get("name") or ""), str(update.get("task") or "")
        )
        return self._roster_event() if row is not None else []
    if kind == UPDATE_SUBAGENT_STATE:
        child = update.get("subagentSessionId")
        row = self._announced_subagent_roster.get(child) if isinstance(child, str) else None
        status = _LEGACY_STATE_STATUS.get(str(update.get("state") or ""))
        if row is None or status is None:
            return []
        row["status"] = dict(status)
        return self._roster_event()
    return []


#: Longest ``toolCallId`` of a legacy subagent row this handle remembers. The ids
#: codex-acp mints are ``call_<32 hex>`` and ``subagent-completed-<uuid>``, well
#: under the session-id bound reused here; a longer one is still read, just not
#: remembered, so its trailing update takes the ordinary path.
LEGACY_ROW_TOOL_CALL_ID_CAP = MAX_ACP_SESSION_ID_LEN

#: ``rawInput.activityKind`` of codex-acp's legacy subagent activity rows.
_LEGACY_ACTIVITY_STATUS: dict[str, dict[str, str]] = {
    "started": _RUNNING,
    "completed": _COMPLETED,
    "interrupted": _failed("cancelled"),
}


def _handle_legacy_codex_subagent(self: AcpSessionHandle, update: dict) -> list[AcpEvent] | None:
    """Read codex-acp's legacy "Start/Complete subagent" rows as card lifecycle.

    An adapter whose ACP SDK predates ``clientCapabilities.subagents`` drops the
    declaration and reports a native child only as generic tool rows on the
    parent, ``rawInput`` carrying ``agentThreadId``, ``agentPath`` and
    ``activityKind``. Those rows still give the child an identity and a
    lifecycle, so they feed the same roster; the child's own work is not sent
    by such an adapter at all, so its card has no output.

    Returns None for every other frame, which then takes the ordinary path.
    A row read here is not also rendered as a tool call, and its trailing
    ``tool_call_update`` is dropped with it.
    """
    tool_call_id = update.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    if tool_call_id in self._legacy_subagent_tool_ids:
        return []
    raw = update.get("rawInput")
    if not isinstance(raw, dict):
        return None
    child = raw.get("agentThreadId")
    path = raw.get("agentPath")
    status = _LEGACY_ACTIVITY_STATUS.get(str(raw.get("activityKind") or ""))
    if not isinstance(child, str) or not child or not isinstance(path, str) or status is None:
        return None
    row = self._announced_row(child, self._session_id, _name_from_agent_path(path, ""), "")
    if row is None:
        return []
    # Remembered only for a child that got a row, and only while the id is a
    # plausible call id: the set is bounded in count AND in bytes per entry.
    if (
        len(tool_call_id) <= LEGACY_ROW_TOOL_CALL_ID_CAP
        and len(self._legacy_subagent_tool_ids) < NATIVE_CHILD_ROSTER_CAP
    ):
        self._legacy_subagent_tool_ids.add(tool_call_id)
    row["status"] = dict(status)
    return self._roster_event()


def _note_native_child(self: AcpSessionHandle, child_sid: object) -> bool:
    """Count one native child id on this handle (bounded, type-checked).

    Ids are backend-controlled: non-strings, empties, over-long values and
    this handle's own id are ignored; past the roster cap the id is
    counted in ``native_child_overflow`` instead of stored.

    Returns whether the id is TRACKED in ``native_child_sessions`` after
    the call — True for an id already known, which is the ordinary
    re-report of a status change. A caller that keeps its own per-child row
    (:attr:`_kas_subagent_roster`) keys that row on this answer, so this
    set is the single bound on every native-child store on the handle and
    no such store can outgrow :data:`NATIVE_CHILD_ROSTER_CAP`. An id this
    set refused must get no row: a row for an unremembered id could never
    be recognised as a duplicate, so it would reintroduce exactly the
    unbounded growth the cap refuses.
    """
    if not isinstance(child_sid, str) or not child_sid or len(child_sid) > MAX_ACP_SESSION_ID_LEN:
        return False
    if child_sid == self._session_id:
        # A parent is never its own sub-agent, in the count or in a roster row.
        return False
    if child_sid in self._native_child_sids:
        return True
    if len(self._native_child_sids) >= NATIVE_CHILD_ROSTER_CAP:
        self._native_child_overflow += 1
        return False
    self._native_child_sids.add(child_sid)
    return True


def _note_native_roster(self: AcpSessionHandle, subagents: object) -> None:
    """Count every child id in a roster notification (kiro-cli
    ``subagent_list`` shape: ``sessionId`` / ``session_id`` per entry).

    Every entry is offered to :meth:`_note_native_child`, whose cap is the
    only entry bound: an id is stored below :data:`NATIVE_CHILD_ROSTER_CAP`
    and counted in ``native_child_overflow`` past it. A tighter slice here
    would drop a long roster's tail from BOTH the set and the counter, and
    drop it invisibly — a truncated tail reads exactly like a roster that
    never named the child, so the residency number would under-report and
    those children would lose their typed
    :meth:`native_child_resume_refusal`. Memory is bounded by the cap and
    not by the entry count, and the per-frame work is proportional to a
    payload the reader has already parsed — the same shape as the KAS stage
    loop in :meth:`_handle_kas_subagent`.
    """
    if not isinstance(subagents, list):
        return
    for entry in subagents:
        if isinstance(entry, dict):
            self._note_native_child(entry.get("sessionId") or entry.get("session_id"))


def native_child_resume_refusal(self: AcpSessionHandle, conversation_id: str) -> str | None:
    """The typed reason a ``spawn_continue``-style resume of
    ``conversation_id`` must be refused, or None when the id is not one of
    this handle's native children.

    A native child has no conversation, task row or runtime of its own —
    resuming "it" can only mean resuming its parent — so the refusal names
    the parent and the lever that exists. Structured (prefix
    :data:`NATIVE_CHILD_NOT_RESUMABLE`) so a caller can act on it rather
    than on the generic ``conversation_gone`` a lookup miss would produce.
    """
    if conversation_id not in self._native_child_sids:
        return None
    return (
        f"{NATIVE_CHILD_NOT_RESUMABLE}: {conversation_id} is a harness-native "
        f"child of session {self._session_id}; it has no conversation of its "
        "own and cannot be resumed, steered or cancelled independently -- "
        "continue or cancel the parent session instead"
    )


def report_native_children(self: AcpSessionHandle, budget: Any) -> int:
    """Report this turn's native children to a ``HostBudget``-shaped
    ``budget`` as UNCHARGED residency, returning the count reported.

    Reporting only: the budget's ``procs`` / ``rss_mb`` / ``fds`` counters
    and its admission decisions are untouched, because the children run
    inside the parent's runtime process, which is already charged. Duck-
    typed on ``report_uncharged(kind, count)`` so the daemon's budget and
    a gateway-side health mirror take the same call.
    """
    count = len(self._native_child_sids) + self._native_child_overflow
    report = getattr(budget, "report_uncharged", None)
    if callable(report):
        report(NATIVE_CHILDREN_UNCHARGED_KIND, count, label=self._session_id)
    return count
