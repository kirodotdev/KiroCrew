"""Cron result injection into dashboard chat slots.

Extracted from handlers/cron.py to break the circular import between
gateway.py and dashboard.handlers.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.state import DashboardState, SlotOrigin, row_mid
from kiro_crew.history import append_rows_if_absent_off_loop
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.cron import CronJob


def context_meter_reading(client: object) -> dict[str, Any] | None:
    """Best-effort context-meter reading from a live provider, or ``None``.

    The cron executor resets its agent session the moment the run finishes, so
    by the time the user opens the injected ``cron-{id}`` slot there is no
    resident provider for the slot-detail open path to read and no snapshot
    either — ``broadcast_context_usage`` (the meter's single writer) is only
    reached by dashboard-driven turns. The bar therefore rendered 0% for a
    session with a full transcript. This helper captures the reading while the
    provider is still alive; :func:`inject_cron_result_to_dashboard` routes it
    through the single writer so the open path serves it like any other
    cold-session snapshot.

    Mirrors ``chat_runner._context_usage_payload``'s accessor discipline: the
    provider's PUBLIC accessors only (``last_prompt_stats`` lives on the inner
    AcpClient and would always miss on the pooled provider), and token counts
    ship only when both are measured — ``used == 0`` means "not measured", and
    asserting a false "0 / W tokens" is worse than omitting the pair.

    Returns ``None`` when nothing was measured (``pct <= 0``): a provider that
    just ran a turn always occupies context (the prompt itself), so a
    zero/absent pct here means "not reported", never "measured empty" — the
    same contract as ``_context_reading`` on the read side. Skipping the write
    also deliberately preserves an earlier run's real snapshot rather than
    overwriting it with an unmeasured zero (which would recreate the 0%-bar
    symptom this helper exists to fix); the genuine post-compaction 0% reset
    frame is emitted by the session manager's compact callback for live
    sessions, not by this capture path. Never raises — this feeds best-effort
    display state and must not fail the cron delivery that calls it.
    """
    try:
        # Deferred: dashboard.handlers.__init__ imports this module, so a
        # top-level import of handlers.usage would close a circular import.
        from kiro_crew.dashboard.handlers.usage import read_context_tokens

        pct_fn = getattr(client, "context_usage_pct", None)
        pct = float(pct_fn()) if callable(pct_fn) else 0.0
        if not math.isfinite(pct) or pct <= 0:
            return None
        used, window = read_context_tokens(client)
        reading: dict[str, Any] = {"pct": round(pct, 1)}
        if used > 0 and window > 0:
            reading["used_tokens"] = used
            reading["window_tokens"] = window
        return reading
    except Exception:
        return None


def run_stamp(job: "CronJob") -> str:
    """The header suffix identifying WHICH run a row belongs to, or ``""``.

    A persistent cron reuses one session and one ``cron-{id}`` tab for its whole
    life, so every run appended a row headed only by the job's name. N days of
    runs therefore read as one undated pile -- in the tab, and in the
    ``build_session_replay`` reconstruction a follow-up turn opens with, which
    is how a reply meant for the newest run lands against an older one.

    Read straight off ``job.last_result_stamp``, which ``CronJob.set_run_result``
    rendered when the result was recorded. This function deliberately does no
    formatting and no timezone lookup: the stamp is part of the row's dedup key,
    so a re-render under an edited ``timezone`` would spell an existing row
    differently and append a duplicate instead of collapsing onto it.

    ``""`` when the job carries no stamp (a store written by an older build, or
    a job that has never run): the header keeps its pre-stamp spelling, so a row
    already on disk still matches and is not re-appended beside a stamped twin.
    """
    stamp = getattr(job, "last_result_stamp", "")
    return stamp if isinstance(stamp, str) else ""


def run_marker(job: "CronJob") -> str:
    """The row's IDENTITY as an invisible marker, or ``""``.

    A row is recognised as already-written by its exact text: both dedup layers
    compare content (``_reflect``'s window scan and
    ``ConversationLog.append_if_absent``). Leaving identity to the DISPLAYED
    stamp made resolution load-bearing -- at minutes, two runs in one minute with
    identical output collapsed; at seconds, two runs in one second still did. The
    bound moves but never closes, because a stamp is written for a person to read
    and a person does not need microseconds.

    So identity stops riding on the display string. This marker carries the run's
    own ``last_result_ts`` at full precision, and the stamp is free to stay
    human-readable. An HTML comment because it must not render: the tab shows the
    stamp, while the replay a follow-up turn reads gets an explicit, unambiguous
    run boundary.

    Built only from ``job.id`` and the timestamp -- both internal, neither
    user-supplied -- so it carries nothing the redactors would need to touch.

    Emitted BEFORE the body, not after it. A prompt or result is untrusted text
    that can end inside an unclosed ``` fence, and everything following such a
    fence renders as code -- which would print this comment verbatim in the tab
    instead of hiding it. Position does not affect identity, since the dedup
    compares the row's whole content either way.

    ``""`` when the job has no timestamp, matching :func:`run_stamp`: a legacy
    row already on disk keeps its historical spelling and still dedups.
    """
    ts = getattr(job, "last_result_ts", 0.0)
    if not isinstance(ts, (int, float)) or not ts:
        return ""
    # Fixed 6dp rather than repr(): it round-trips through the JSON store
    # unchanged, so the marker a reloaded job renders is byte-identical to the
    # one the executor wrote.
    return f"\n\n<!-- cron-run:{job.id}:{ts:.6f} -->"


def inject_cron_result_to_dashboard(
    state: DashboardState, job: "CronJob", result_text: str,
    *,
    include_prompt: bool = True,
    history: list[dict[str, Any]] | None,
    context_reading: dict[str, Any] | None = None,
) -> None:
    """Inject cron result into linked dashboard chat slot (shared by to-chat and auto-inject).

    ``history`` is the ``cron:{id}`` transcript, hydrated into the slot the first
    time this binds one. It is REQUIRED and has no default on purpose: this
    function is synchronous and every caller is async, so a default would let a
    caller silently hand the whole-transcript parse back to the event loop --
    the defect issue #7408 fixed at five sites, four of which were exactly that
    omission. Without a default, forgetting it is a ``TypeError`` at the call,
    not a stall in production. Async callers get the value from
    :func:`prefetch_cron_history`; a sync caller must read it itself and own the
    blocking cost.

    ``None`` is legal and means "the injection will not need it" -- the state
    :func:`prefetch_cron_history` skips its read in. It cannot mean a lost
    hydration, because the only state that consumes ``history`` is an unlinked
    slot, and the sole writer of ``linked_session_key`` for a cron slot is the
    line below, which runs in this same synchronous block.

    Writes the run as a PAIR: the job's own prompt as a ``user`` row, then the
    result as an ``assistant`` row, both headed by :func:`run_stamp`. The
    executor streams the prompt straight to the provider and never persists it,
    so a follow-up turn used to replay a stack of results with nothing saying
    what any of them had been asked -- it could not tell which run the person in
    front of it was answering. The pair is written only when the run produced a
    result, so neither row can appear without its counterpart.

    ``include_prompt`` is False for a caller that is RE-SURFACING an older
    result rather than delivering a fresh one (``/to-chat``). Only the run that
    produced the result knows the prompt that produced it: ``job.message`` is
    live configuration a user can edit afterwards, so a re-surfacing caller
    reading it would pair the stored output with an instruction that never ran.
    Such a caller writes the result row alone -- the pre-existing behaviour --
    and any prompt row the executor already wrote is still in the history it
    hydrates from.

    ``context_reading`` is the run's context-meter reading captured by
    :func:`context_meter_reading` while the cron's provider was still resident.
    When present it is routed through ``broadcast_context_usage`` — the meter's
    single writer — so an open tab updates live and the slot-detail open path
    can serve it after the executor resets the session. ``None`` (the to-chat
    replay path, or a run that measured nothing) records nothing and keeps
    whatever snapshot an earlier run stored.
    """
    slot_name = f"cron-{job.id}"
    slot = state.get_or_create_slot(
        name=slot_name,
        agent=job.agent_id or "",
        # A cron result is the job's output, not something the person typed.
        # A USER label would expose it to any app holding `slots:user`.
        origin=SlotOrigin.CRON,
    )
    safe_name, _ = redact_exfiltration_urls(job.name)
    safe_name, _ = redact_credentials(safe_name)
    slot.title = f"Cron: {safe_name}"
    if not slot.linked_session_key:
        slot.linked_session_key = f"cron:{job.id}"
        hydrate_slot_from_history(slot, history or [])
    # Publish the (possibly just-created) tab to the dashboard-surface registry
    # BEFORE anything routes against it. Every gate that asks "does this session
    # have a tab?" — dashboard_slot_key for sub-agent event routing and
    # completion injection, widget/question/approval delivery — reads that
    # registry, and a created-but-unpublished slot silently fails those gates
    # until some unrelated slot change happens to republish. (Same invariant as
    # channel_slots.reconcile — see the comment there.)
    from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots

    _sync_dashboard_slots(state)

    # Rows this call owes the durable transcript, in the order they happened.
    # Collected rather than written per row: the pair is flushed once, below,
    # under a single ``atomic_appends`` hold -- see the flush for why.
    durable_rows: list[tuple[str, str, str, str | None]] = []

    def _reflect(role: str, content: str, cls: str) -> None:
        """Put one row in the live slot and queue it for the durable write."""
        if any(msg.get("content") == content for msg in slot.messages):
            return
        # The durable copy must carry the SAME ``meta.mid`` the window copy is
        # minted (read off the append's return via ``row_mid``): an id-less
        # durable row cannot be matched by the bounded read's identity walk,
        # which then treats the window copy as still owed and re-appends the
        # injection.
        window_mid = row_mid(slot.append(role, content, cls))
        durable_rows.append((role, content, cls, window_mid))

    def _flush_durable_rows() -> None:
        """Write the queued rows to the canonical log as ONE grouped append.

        Persisted under the linked session key so a dashboard follow-up turn has
        the run as context: the cron execution path (gateway
        stream_and_collect) streams text into job.last_result but never writes
        the dashboard conversation_log, and slot.append only updates the
        in-memory slot. Without this, chat_runner.build_session_replay reads an
        empty cron:{id} log and the follow-up agent opens with no memory of the
        run the user is looking at. The stable linked key (cron:{id}) covers
        both persistent and stateless crons, since the slot always links there
        regardless of the per-run execution key.

        ONE grouped write, not one per row: a run writes a prompt row AND a
        result row, and dispatching them separately hands two worker threads two
        independent appends -- they can land out of order, or one can fail
        alone, leaving the replay with a reversed or half-written run that no
        timestamp ordering repairs. ``append_rows_if_absent_off_loop`` holds
        ``atomic_appends`` for the group, which is the contract's stated
        companion for a multi-append caller that offloads.

        Each row keeps ``append_if_absent``'s idempotence, so the duplicate
        check and the write stay one critical section per row: an unlocked
        read_messages() + append would leave a TOCTOU window in which a
        concurrent slot save (or a cron re-fire) lands the identical row between
        the check and the write. Best-effort -- lock/IO errors only skip the
        durable copy, which the slot above already carries.
        """
        if state.conversation_log is None or not durable_rows:
            return
        append_rows_if_absent_off_loop(
            state.conversation_log,
            f"cron:{job.id}",
            durable_rows,
            agent=job.agent_id or None,
        )

    if result_text:
        stamp = run_stamp(job)
        # Identity, separate from the displayed stamp -- see run_marker.
        marker = run_marker(job)
        # Prompt first, so the transcript reads in the order it happened and a
        # replay pairs each result with the instruction that produced it. Gated
        # on the result: a lone prompt row would be a run boundary with nothing
        # behind it, which is what ``/to-chat`` on a job that has never produced
        # one would otherwise write.
        # Stored VERBATIM. ``.strip()`` belongs in the emptiness test, not in
        # the stored value: the executor sends ``job.message`` as written, so
        # persisting a trimmed copy records a prompt the run was never given --
        # and leading indentation is not decoration in a message carrying a
        # fenced block or an indented snippet. The strip still decides whether a
        # prompt EXISTS, so a whitespace-only message writes no row (dropping it
        # entirely would emit a run boundary whose body is blank).
        raw_prompt = job.message or ""
        prompt = raw_prompt if include_prompt and raw_prompt.strip() else ""
        if prompt:
            safe_prompt, _ = redact_exfiltration_urls(prompt)
            safe_prompt, _ = redact_credentials(safe_prompt)
            _reflect(
                "user",
                f"# Cron Run: {safe_name}{stamp}{marker}\n\n{safe_prompt}",
                "msg msg-u",
            )
        safe_result, _ = redact_exfiltration_urls(result_text)
        safe_result, _ = redact_credentials(safe_result)
        _reflect(
            "assistant",
            f"# Cron Job Result: {safe_name}{stamp}{marker}\n\n{safe_result}",
            "msg msg-a",
        )
        # After BOTH rows are queued, so the pair lands as one write.
        _flush_durable_rows()
    if context_reading:
        # Same frame shape as chat_runner._context_usage_payload. `reset` when
        # the counts are unknown is load-bearing: the frontend stores pct and
        # token counts in independent slices, and a bare {slot, pct} frame
        # would leave stale counts beside a fresh percentage.
        payload: dict[str, Any] = {"slot": slot.key, "pct": context_reading["pct"]}
        if context_reading.get("window_tokens"):
            payload["used_tokens"] = context_reading.get("used_tokens", 0)
            payload["window_tokens"] = context_reading["window_tokens"]
        else:
            payload["reset"] = True
        state.broadcast_context_usage(slot.key, payload)
    state.push_slots_update()


async def prefetch_cron_history(
    state: DashboardState, job_id: str
) -> list[dict[str, Any]] | None:
    """Off-loop read of the ``cron:{id}`` transcript for the injection above.

    :func:`inject_cron_result_to_dashboard` is synchronous and hydrates a
    newly linked slot from the transcript, which means reading and parsing the
    whole file (100-300 ms on a large store). Its ``history`` parameter is
    required precisely so an async caller cannot leave that read to it; this is
    the helper that produces the value, on a worker thread.

    Returns ``None`` (read skipped, nothing added to the caller's cost) when the
    slot already exists AND is already linked, because that is exactly the state
    in which the injection does not consume ``history`` at all. ``None`` is a
    legal value for the parameter, so the skip needs no special handling at the
    call site.
    """
    if state.conversation_log is None:
        return None
    slot = state.get_slot(f"cron-{job_id}")
    if slot is not None and slot.linked_session_key:
        return None
    return await asyncio.to_thread(state.conversation_log.read_messages, f"cron:{job_id}")


def hydrate_slot_from_history(slot: Any, messages: list[dict[str, Any]]) -> None:
    """Load last 50 messages from pre-loaded history into a new slot.

    Each row's ``meta`` rides along so a persisted ``meta.mid`` survives the
    round trip (``_ChatSlot.append`` preserves a supplied id and mints one only
    when absent). Dropping it would hand every hydrated row a FRESH id while
    the disk keeps the old ones: with the durable injection copies now
    id-carrying, the on-disk window region reads all-id, the slot-detail
    reconciliation selects the identity walk, no window id matches, and the
    whole hydrated history is served twice until the next flush rewrites it.
    """
    for msg in messages[-50:]:
        role = msg.get("role", "assistant")
        content = msg.get("content", "")
        if not content:
            continue
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
        if any(m.get("content") == content for m in slot.messages):
            continue
        slot.append(
            role,
            content,
            f"msg msg-{'a' if role == 'assistant' else 'u'}",
            broadcast=False,
            meta=(msg["meta"] if isinstance(msg.get("meta"), dict) else None),
        )
