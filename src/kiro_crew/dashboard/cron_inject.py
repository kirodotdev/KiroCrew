"""Cron result injection into dashboard chat slots.

Extracted from handlers/cron.py to break the circular import between
gateway.py and dashboard.handlers.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.state import (
    VALID_MEMORY_MODES,
    DashboardState,
    SlotOrigin,
    append_and_surface,
    row_mid,
)
from kiro_crew.history import INCOGNITO_MEMORY_MODES, append_rows_if_absent_off_loop
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)


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
    return f"\n\n{_MARKER_PREFIX}{job.id}:{ts:.6f} -->"


#: Header prefix of a PROMPT row. Distinct from the result row's
#: ``"# Cron Job Result:"``, which does not share this prefix, so a scan for one
#: never matches the other.
_PROMPT_ROW_PREFIX = "# Cron Run:"

#: Opening of the invisible identity marker :func:`run_marker` emits, and the
#: token :func:`_prompt_row_body` finds a row's header by. Declared once and used
#: by both: a marker written with a spelling the parser does not recognise turns
#: every body read into the header text, and the repeat suppression into a no-op.
_MARKER_PREFIX = "<!-- cron-run:"

#: Body written in place of a repeated instruction. Points at a row in the
#: TRANSCRIPT -- a historical record of what a run was actually given -- and
#: deliberately not at ``job.message``, which is live configuration: a reader
#: resolving that would get whatever the instruction is NOW, which is the same
#: misattribution that makes ``/to-chat`` pass ``include_prompt=False``.
#:
#: "the nearest row above that carries one" rather than "the row above", because
#: in the steady state the row directly above is another reference. What the
#: sentence promises is exactly what :func:`_prompt_already_recorded` enforces: a
#: verbatim copy within :data:`_PROMPT_LOOKBACK_ROWS` rows, so following the
#: pointer terminates.
_UNCHANGED_PROMPT_BODY = (
    "_Same instruction as the previous run — its full text is in the nearest "
    '"# Cron Run" row above that carries one._'
)

#: Invisible token that marks a prompt row as a REFERENCE rather than a verbatim
#: instruction. It rides in the header's protected marker block -- immediately
#: after the run marker, ahead of the body separator -- so it can never be
#: confused with body text, exactly as the run marker cannot (see
#: :func:`_parse_prompt_row`).
#:
#: Reference-ness MUST be a structural fact of how the row was written, never an
#: inference from what the body renders as. The body of a reference is the human
#: prose :data:`_UNCHANGED_PROMPT_BODY`, and a real instruction can equal that
#: prose (a job whose ``message`` literally is that sentence) or quote this token
#: in its own text. Deciding "is this a reference?" from the body would then read
#: a genuine instruction row as a pointer, skip it as the latest antecedent, and
#: attribute a later identical run to the row above it -- the run-attribution
#: corruption this whole path exists to prevent. Keyed off the marker instead,
#: the two are independent: a verbatim row is compared on its real bytes however
#: they read, and a reference is recognised however its prose reads.
_REFERENCE_MARKER = "<!-- cron-ref -->"

#: Shortest instruction worth referencing instead of storing again.
#:
#: A reference is not free: the row keeps its header, stamp and marker either way,
#: so replacing the text costs ``len(_UNCHANGED_PROMPT_BODY)`` and saves
#: ``len(prompt)``. Below break-even the transcript gets BIGGER *and* loses the
#: instruction -- the worst of both -- and most crons in the wild carry a one-line
#: message ("check pipeline health"), so the common case must be left alone. The
#: threshold sits well above break-even rather than at it: a 100-char message
#: repeated across a full 200-row window costs ~20KB against a 10MB rotation
#: budget, which is not worth trading the text for. The saving only becomes worth
#: the indirection at the kilobyte-prompt scale.
_MIN_PROMPT_CHARS_TO_REFERENCE = 500

#: How far back a reference may reach for its antecedent, in ROWS of the tab.
#: A run writes two rows, so this is ~20 runs.
#:
#: The bound is what makes a reference RESOLVABLE rather than merely true. Three
#: windows disagree about what "still here" means: the live slot keeps 10,000 rows
#: (``state._MAX_SLOT_MESSAGES``), the durable transcript keeps ~200 lines
#: (``history._SESSION_KEEP_LINES``, tail), and the replay a follow-up turn reads
#: is character-budgeted and tail-heavy (``context._REPLAY_BUDGET_CHARS``). An
#: unbounded scan reads the largest of the three, so a copy that rotation has
#: already dropped from disk keeps satisfying it -- the instruction would be stored
#: once and referenced forever, with the text nowhere a reader can reach. Bounding
#: the look-back re-anchors a verbatim copy every ~20 runs instead, which is inside
#: the two ROW-counted windows (the 10,000-row slot and the ~200-line transcript
#: tail) and cannot be defeated by a long-lived gateway.
#:
#: It does NOT promise resolvability inside the replay: that window is counted in
#: CHARACTERS, so ~20 runs whose RESULTS are long can exhaust the budget before it
#: reaches the antecedent, leaving a reference whose text is on disk but outside
#: what one replay shows. No write-time row count can close that -- the budget is
#: scaled at REPLAY time by the active model's context window, which the writing
#: run cannot know and which can change between the write and the read. Closing it
#: belongs on the READING side (have the replay builder resolve a marker it can
#: see); a row count is the conservative invariant available here, and it errs
#: toward re-anchoring too often rather than too rarely.
_PROMPT_LOOKBACK_ROWS = 40


def _prompt_row_body(content: str) -> str | None:
    """The instruction text stored in a prompt row, or ``None`` if unreadable.

    ``None`` is the FAIL-SAFE answer, and the direction matters: the caller reads
    it as "this row does not record the instruction", so an unparseable row makes
    the run store its prompt verbatim. An extra copy costs bytes; suppressing
    against a row whose text was never confirmed would attribute a run to an
    instruction it never had.
    """
    return _parse_prompt_row(content)[1]


def _parse_prompt_row(content: str) -> tuple[bool, str | None]:
    """Split a prompt row into ``(is_reference, instruction_body)``.

    ``is_reference`` is read from :data:`_REFERENCE_MARKER` in the header's
    protected marker block, NOT from what the body renders as -- see that
    constant for why the distinction cannot ride on the body. ``instruction_body``
    is the verbatim text a run was given, or ``None`` on an unreadable row (the
    fail-safe :func:`_prompt_row_body` documents). A reference row reports
    ``(True, _UNCHANGED_PROMPT_BODY)``: it is not instruction-bearing, and its
    body is the pointer prose.

    The marker, when present, is its own block DIRECTLY after the header, and is
    recognised only there rather than wherever it first appears in the row. An
    instruction is untrusted text that may quote this very format -- a prompt
    asking the agent about ``<!-- cron-run:`` spellings is the obvious case -- and
    a row-wide search would cut such a body at its own quoted marker, then compare
    the remainder as though it were the whole instruction. The reference marker
    sits in that same protected block, so a body that merely quotes it is read as
    part of the instruction, not as a structural flag.
    """
    text = content.lstrip()
    if not text.startswith(_PROMPT_ROW_PREFIX):
        return (False, None)
    _header, sep, rest = text.partition("\n\n")
    if not sep:
        return (False, None)
    if rest.startswith(_MARKER_PREFIX):
        close = rest.find("-->")
        if close == -1:
            return (False, None)
        after = rest[close + 3 :]
        is_reference = after.startswith(_REFERENCE_MARKER)
        if is_reference:
            after = after[len(_REFERENCE_MARKER) :]
        return (is_reference, _after_separator(after))
    # No run marker means the row was never written as a reference (a reference is
    # only ever emitted for a run that carries one), so its whole body is verbatim.
    return (False, rest)


def _after_separator(tail: str) -> str:
    """Drop the ``\\n\\n`` the row builder puts between header and body.

    Exactly one separator, not every leading newline: an instruction may itself
    START with a blank line, and a greedy strip would read that row's body back
    as a value the run was never given. It would never equal the prompt being
    compared, so suppression would silently never fire for such a job -- the
    defect this whole path exists to fix, invisible because the fallback is the
    old behaviour.
    """
    return tail[2:] if tail.startswith("\n\n") else tail.lstrip("\n")


def _prompt_already_recorded(slot: Any, prompt_body: str, own_marker: str) -> bool:
    """Whether the run just before this one was given this exact instruction.

    The question is asked of the TRANSCRIPT, never of a flag on the job, and that
    is the whole point. A flag saying "the instruction changed" is set where the
    result is recorded, while the row that must act on it is written by a
    different component that may not run at all: ``hide_in_chat`` and
    ``persistent_session=False`` both skip this injection entirely, so a hidden
    run of an edited message would spend the signal, and the next visible run
    would reference a surviving row still holding the OLD instruction -- a
    transcript that attributes a result to something the run was never asked.
    Reading the rows cannot desync from them.

    It is deliberately the LATEST instruction-bearing prompt row that is compared,
    not any row in reach. An edit that is later reverted (A, then B, then A again)
    would satisfy an ``any()`` scan against the old A row while the run above
    actually carries B -- so the placeholder would assert "same as the previous
    run" about a different instruction, which is the exact misattribution this
    design exists to avoid. Comparing the latest one makes the sentence true by
    construction, and reverts write A again.

    Equality, not containment: a substring test would find a newly shortened
    "do X" inside a stored "do X and Y" and suppress a genuine change.

    Bounded by :data:`_PROMPT_LOOKBACK_ROWS`, which is what keeps a reference
    resolvable rather than merely accurate -- see that constant for the three
    disagreeing windows and why an unbounded scan strands the instruction off
    disk. Reaching the end of the look-back without an instruction-bearing row is
    the ordinary re-anchor path, not an error: the run writes the text again.

    Skipped entirely for a prompt shorter than
    :data:`_MIN_PROMPT_CHARS_TO_REFERENCE`, where a reference would cost more
    bytes than it saves.

    Reads ``slot.messages``, the live window: freshly hydrated from the transcript
    on a first bind, and carrying this process's appended rows afterwards. The
    ``history`` parameter is NOT usable here -- :func:`prefetch_cron_history`
    returns ``None`` once the slot is linked, which is every run of a persistent
    cron after the first, so a scan of it would see nothing exactly when the
    answer matters. Only ``user`` rows are considered, because only a run's prompt
    row is evidence of what a run was given; a row a person typed in the tab is
    not, however it is headed.

    ``own_marker`` -- this run's own :func:`run_marker` -- excludes the run asking
    the question. Injecting one run twice must render the SAME bytes, or the two
    rows dedup to neither and the tab grows: a scan counting this run's own
    earlier row as precedent would make the second call write a reference where
    the first wrote the instruction. No production caller injects one run twice
    with ``include_prompt=True`` today (``/to-chat`` re-surfaces the result alone),
    so this is an invariant the dedup layers rely on rather than a live path --
    which is why it is pinned by tests rather than left to be rediscovered.

    A run with no marker gets no suppression at all (``own_marker`` empty, which
    :func:`run_marker` returns for a job carrying no timestamp). Without a run
    identity there is nothing to tell "the row I just wrote" from "a row a
    previous run wrote", so the safe answer is the one that keeps the legacy
    spelling: write the instruction, and let content dedup collapse the repeat.
    """
    if not own_marker or len(prompt_body) < _MIN_PROMPT_CHARS_TO_REFERENCE:
        return False
    messages = getattr(slot, "messages", None) or []
    for msg in reversed(list(messages)[-_PROMPT_LOOKBACK_ROWS:]):
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content", "") or "")
        if own_marker in content:
            continue
        is_reference, body = _parse_prompt_row(content)
        # A REFERENCE row holds no instruction -- it points at the verbatim copy
        # above it -- so skip past it to that copy. Reference-ness is the
        # structural marker, never ``body == _UNCHANGED_PROMPT_BODY``: a verbatim
        # instruction whose text equals that prose is a real antecedent and must
        # be compared here, or a later run given it references the row above and
        # its result is attributed to a different instruction.
        if is_reference or body is None:
            continue
        return body == prompt_body
    return False


def _safe_job_name(job: "CronJob") -> str:
    """The display form of ``job.name``: URL pass first, credential pass over
    its output — the ORDER is part of the contract (the sibling test module
    mirrors it byte-for-byte), so it lives in one place for the slot title and
    the run headers alike."""
    safe_name, _ = redact_exfiltration_urls(job.name)
    safe_name, _ = redact_credentials(safe_name)
    return safe_name


def _bind_cron_slot(
    state: DashboardState,
    job: "CronJob",
    history: list[dict[str, Any]] | None,
) -> Any:
    """Create-or-find the job's dashboard slot, bind its identity, publish it.

    The single shared core for BOTH creator paths — the result injection below
    and the run-start pre-create (:func:`ensure_cron_slot`) — so the two can
    never diverge on the link/hydration invariant: ``linked_session_key`` and
    the hydration from the ``cron:{id}`` transcript move TOGETHER. A slot
    linked without hydration hands a follow-up turn no memory of prior runs; a
    hydration without the link would re-run on every bind. Keeping both under
    the one unlink guard makes every caller after the first an idempotent
    no-op, which is what lets the injection run unchanged after a pre-create.
    """
    slot = state.get_or_create_slot(
        name=f"cron-{job.id}",
        agent=job.member_id or job.agent_id or "",
        # A cron result is the job's output, not something the person typed.
        # A USER label would expose it to any app holding `slots:user`.
        origin=SlotOrigin.CRON,
    )
    slot.title = f"Cron: {_safe_job_name(job)}"
    # A provider-template alias on a legacy V1 job cannot authorize a private
    # member. Private cron dispatch publishes its protected session binding;
    # follow-up turns must use that binding instead of minting one from agent_id.
    slot._memory_assignment_from_history = True
    if job.memory_store:
        slot.memory_store = job.memory_store
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
    return slot


def _resolved_memory_mode(raw: object) -> str | None:
    """A transcript header's memory mode, or None when it cannot be established.

    ALLOWLIST, not normalize-and-hope, and that is the whole point: the shared
    membership predicate deliberately does not strip whitespace and reads an
    unrecognised value as not-private, so a header carrying ``"incognito "`` would
    pass a direct membership test as unrestricted. Any decision that must fail
    closed has to resolve the mode through the valid set first and treat anything
    outside it as unknown. The API validates this field on the way in, but a
    hand-edited or partially written transcript is not bound by that.

    An absent field is a legacy persistent session -- the header predates the
    field rather than hiding a mode -- so it resolves rather than refusing.
    """
    if raw is None:
        return "persistent"
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized not in VALID_MEMORY_MODES:
        return None
    return normalized


def _deliver_to_archived_target(
    state: DashboardState, job: "CronJob", session_key: str, content: str
) -> None:
    """Persist the result into a target whose TAB is closed but whose conversation is not.

    Closing a tab archives it: the slot is popped while the transcript stays on
    disk, and reopening the conversation reads that file back. So the live-slot
    mirror is unreachable for exactly the case a target is most often named for --
    schedule something for later, close the tab, come back to it -- and without
    this leg the job keeps reporting a destination it never delivers to. The
    result is not merely late; it never lands anywhere the person can find it.

    Two conditions, both hard refusals rather than best-effort guesses, because
    this write lands in a conversation the job does not own:

    * the transcript must ALREADY exist. This path never creates one, so a
      conversation the user deleted stays deleted -- creating the file here would
      resurrect it as a chat containing nothing but one orphaned result. That is
      enforced by the append itself (``require_existing``), inside the same lock,
      because a pre-check cannot: the delete can land in the window between them.
    * it must be the SAME conversation. A slot name is reusable, so a session
      deleted and recreated under the old name would otherwise inherit a
      stranger's scheduled result. ``session_tab_id`` is the tab identity recorded
      when the target was named and the transcript carries the same value in its
      metadata, so an incarnation that disagrees is refused.
    * it must not be a conversation with memory writes DISABLED. An
      incognito/temporary target keeps nothing on disk by design, and the live leg
      honours that by never persisting from the slot, so a durable row written
      here would be the one thing that outlives the session.

    A job carrying no identity is delivered on existence alone: ``cron adopt``
    addresses a session by key and nothing else, so requiring a stamp there would
    silently drop delivery for every adopted job. That is adopt's own contract
    rather than a weakening of this one.

    Off the event loop in ONE hop. The existence probe, the metadata read and the
    append are all blocking file I/O taken behind the session lock, and this runs
    from an injection path that normally has a running loop. Probing inline and
    offloading only the write would put two disk reads straight onto the loop,
    which is the same defect as any other synchronous history call from a handler.

    Best-effort, like every other delivery leg: the job's own tab already carries
    the row, so a lock timeout or an I/O error costs a copy of the result rather
    than the run.
    """
    log = state.conversation_log
    if log is None:
        return
    expected_tab_id = getattr(job, "session_tab_id", "") or ""

    def _write() -> None:
        if not log.has_log(session_key):
            return
        # The transcript header is the ONLY authority on the target's memory mode
        # here. Both in-memory signals a restricted session carries are dropped
        # when its tab is archived -- the close pops the slot AND discards its key
        # from `state._restricted_keys`, deliberately, so the marker cannot outlive
        # the tab and block a later holder of the name -- while the header persists.
        #
        # Resolved through the shared probe rather than by testing the raw field,
        # because that probe is ALLOWLIST-first and this decision has to fail
        # closed. `is_incognito_transcript` deliberately does not strip whitespace
        # and reads anything unrecognised as not-private, so a header carrying
        # `"incognito "` misses the membership test and reads as unrestricted --
        # which on this path would put a scheduled result on disk under a session
        # that keeps none. The probe also bounds the bytes it reads, so an
        # enormous first line cannot pin memory, and reports an unreadable or
        # non-metadata header as unknown rather than as persistent.
        meta, readable = log.get_metadata_status(session_key)
        # Fail CLOSED on anything that is not a header this can read. `append`
        # writes the metadata line when it creates the file, before any message,
        # so a transcript whose first line is missing, damaged or not a metadata
        # object was not produced by a normal session and is no evidence that
        # writes are allowed. Read through the transcript store's own API rather
        # than opening the file here: it owns the key-to-path resolution, and the
        # key reaching this function comes from the job record, so resolving a
        # path from it out here would put a new raw file read on caller-influenced
        # input outside the seam that owns transcript access.
        if not readable or not meta:
            return
        mode = _resolved_memory_mode(meta.get("memory_mode"))
        if mode is None or mode in INCOGNITO_MEMORY_MODES:
            return
        if expected_tab_id:
            recorded = str(meta.get("tab_id") or "")
            # A transcript older than tab identities carries none. Unknown is
            # treated as "cannot disprove" rather than as a mismatch: refusing on
            # absence would drop delivery for every conversation predating the
            # field, which is the very failure this path exists to fix.
            if recorded and recorded != expected_tab_id:
                return
        # `append_if_absent` performs its duplicate check and the write inside one
        # critical section, so a re-fire of an identical result -- or a racing slot
        # save for a tab reopened in the meantime -- cannot stack two copies.
        # `require_existing` carries the never-create promise INTO the lock. The
        # `has_log` test above is a cheap early-out, not the guarantee: it is a
        # separate critical section, so a session deleted between it and this write
        # would be RECREATED here, bringing a conversation the user deleted back
        # holding one orphaned cron result. Checked under the append's own lock,
        # that window does not exist.
        log.append_if_absent(
            session_key,
            "assistant",
            content,
            agent=job.agent_id or None,
            cls="msg msg-a",
            require_existing=True,
        )

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning(
                "cron target delivery to archived session failed job=%s: %r", job.id, exc
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _write()
        except Exception:  # noqa: BLE001 - best-effort delivery copy
            logger.warning(
                "cron target delivery to archived session failed job=%s", job.id, exc_info=True
            )
        return
    loop.run_in_executor(None, _write).add_done_callback(_report)


def _mirror_result_to_target_session(
    state: DashboardState,
    job: "CronJob",
    content: str,
) -> None:
    """Also deliver the result row into the dashboard session the job TARGETS.

    The dashboard leg of cron delivery routes by job id alone: every result lands
    in ``cron-{job.id}``, whoever the job was created for. The channel leg already
    routes by ORIGIN — ``_deliver_cron_to_channel`` resolves the creating session
    off ``job.session_key`` and posts there — so a job created from a chat that
    happens to be a dashboard one is the only origin whose output never reaches it.
    This is that missing rung, and ``inject_workflow_result`` is the same shape: a
    terminal result belongs in the chat it was launched from.

    A MIRROR, deliberately, not a move. The job keeps its own tab and keeps
    running under ``cron:{job.id}``: rebinding the target slot would set its
    ``linked_session_key`` to the cron and hydrate the cron's prior runs into a
    person's live conversation, so their next turn would run AS the job. That is
    exactly the pairing ``_bind_cron_slot`` holds together, and it is why nothing
    here touches identity or hydration — only the assistant row is copied, never
    the prompt row, which in someone else's transcript would read as a turn they
    typed. Same choice the channel leg makes: it posts the output, it does not
    relocate the session.

    Scoped to the ``dashboard:`` namespace on purpose. A channel-origin job
    already has a delivery rung of its own, and a channel-born tab's session key
    is the channel's, so mirroring on a resolved-tab match would double-deliver
    what the channel leg posted.

    Best-effort, like every other delivery leg: the job's own tab already carries
    the result, so a deleted target or a tab since relinked costs a copy, not a run.
    Persistence is deliberately NOT this function's business — see the append.
    """
    # Type BEFORE `.strip()`, or the isinstance check below it is dead code and a
    # truthy non-string raises `AttributeError` on the way to it. The field round
    # trips through `crons.json` without coercion, so a hand-edited or corrupt
    # store can hand back an int — and here that would crash the injection of a
    # run that had already succeeded. Same degrade-rather-than-raise contract
    # `_cron_origin_key` states for the very same field.
    raw_key = getattr(job, "session_key", "")
    if not isinstance(raw_key, str):
        return
    session_key = raw_key.strip()
    if not session_key.startswith("dashboard:"):
        return
    slot_name = session_key.removeprefix("dashboard:")
    # Never the job's own tab: the caller already wrote the row there, and a
    # second append under a different door would render it twice.
    if not slot_name or slot_name == f"cron-{job.id}":
        return
    slot = state.get_slot(slot_name)
    if slot is None:
        # Closed, not gone: the conversation is still on disk and still reopenable,
        # so this is a delivery to make durably rather than a target to give up on.
        _deliver_to_archived_target(state, job, session_key, content)
        return
    # A tab whose conversation belongs to a DIFFERENT session is not this key's
    # chat — a channel-born tab keeps the channel's key, a cron tab carries
    # `cron:<id>` — so delivering there would put the result in front of someone
    # the job was not created for. The create path refuses to record such a
    # target; `cron adopt` does not, so the delivery side checks too.
    linked = getattr(slot, "linked_session_key", "") or ""
    if linked and linked != session_key:
        return
    # Dedup on content, matching `_reflect`: a re-fire of an identical result must
    # not stack copies in someone's conversation.
    if any(msg.get("content") == content for msg in getattr(slot, "messages", [])):
        return
    # One identity-carrying door (`append_and_surface`), so the live copy arrives
    # with a `meta.mid` and an explicit frame beside append's own broadcast cannot
    # render the result twice.
    #
    # And that is the WHOLE write. The mirror deliberately makes no durable write
    # of its own: `slot.append` marks the slot dirty, so the periodic slot save
    # persists this row with the rest of the conversation's window, exactly as it
    # does every other message in that chat. Writing the transcript directly from
    # here — a deferred, off-loop write aimed at a session this job does not own —
    # is what created two opposed failures with no local fix: creating the file
    # resurrected a conversation the user had deleted in the meantime, and refusing
    # to create it dropped the row for a session with nothing on disk yet. Neither
    # is reachable once persistence belongs to the slot that owns the conversation.
    # The residual is the ordinary one every dashboard message carries — a crash
    # before the next flush loses the tail — and that is the platform's durability
    # contract rather than something this path may weaken on its own.
    append_and_surface(
        state,
        slot,
        "assistant",
        content,
        "msg msg-a",
        extra={"kind": "cron_result"},
    )


def inject_cron_result_to_dashboard(
    state: DashboardState,
    job: "CronJob",
    result_text: str,
    *,
    include_prompt: bool = True,
    history: list[dict[str, Any]] | None,
    context_reading: dict[str, Any] | None = None,
) -> None:
    """Inject cron result into linked dashboard chat slot (shared by to-chat and auto-inject).

    ``history`` is the ``cron:{id}`` transcript, hydrated into the slot the first
    time this binds one. It is REQUIRED and has no default on purpose: this
    function is synchronous and every caller is async, so a default would let a
    caller silently hand the whole-transcript parse back to the event loop.
    Without a default, forgetting it is a ``TypeError`` at the call, not a stall
    in production. Async callers get the value from
    :func:`prefetch_cron_history`; a sync caller must read it itself and own the
    blocking cost.

    ``None`` is legal and means "the injection will not need it" -- the state
    :func:`prefetch_cron_history` skips its read in. It cannot mean a lost
    hydration, because the only state that consumes ``history`` is an unlinked
    slot, and the only writer of ``linked_session_key`` for a cron slot is
    :func:`_bind_cron_slot` — shared by this function and the run-start
    pre-create (:func:`ensure_cron_slot`), both of which hydrate in the same
    step that links. A slot that is already linked was hydrated when it was
    linked, whichever path did it.

    Writes the run as a PAIR: the job's own prompt as a ``user`` row, then the
    result as an ``assistant`` row, both headed by :func:`run_stamp`. The
    executor streams the prompt straight to the provider and never persists it,
    so without the user row a follow-up turn replays a stack of results with
    nothing saying what any of them was asked -- it cannot tell which run the
    person in front of it is answering. The pair is written only when the run produced a
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
    slot = _bind_cron_slot(state, job, history)
    safe_name = _safe_job_name(job)

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
            # A persistent cron carries ONE message for its whole life, so the
            # verbatim text is stored only when the transcript does not already
            # hold it -- see _prompt_already_recorded. The row itself is written
            # every run regardless: it carries the stamp and the marker, so the
            # run boundary and the user/assistant alternation hold whichever body
            # it gets. Redacted BEFORE the comparison, because the redacted form
            # is what a previous run stored.
            safe_prompt, _ = redact_exfiltration_urls(prompt)
            safe_prompt, _ = redact_credentials(safe_prompt)
            if _prompt_already_recorded(slot, safe_prompt, marker):
                # Mark the row a reference STRUCTURALLY: the invisible
                # _REFERENCE_MARKER rides in the header's protected block right
                # after the run marker (which is non-empty here -- suppression
                # only fires for a run that carries one), so the scan above can
                # tell this from a verbatim row whose text merely reads like the
                # placeholder. See _REFERENCE_MARKER.
                header_marker = f"{marker}{_REFERENCE_MARKER}"
                prompt_body = _UNCHANGED_PROMPT_BODY
            else:
                header_marker = marker
                prompt_body = safe_prompt
            _reflect(
                "user",
                f"# Cron Run: {safe_name}{stamp}{header_marker}\n\n{prompt_body}",
                "msg msg-u",
            )
        safe_result, _ = redact_exfiltration_urls(result_text)
        safe_result, _ = redact_credentials(safe_result)
        result_row = f"# Cron Job Result: {safe_name}{stamp}{marker}\n\n{safe_result}"
        _reflect("assistant", result_row, "msg msg-a")
        # After BOTH rows are queued, so the pair lands as one write.
        _flush_durable_rows()
        # Then the same result into the session the job targets, if it named one.
        #
        # FRESH delivery only. `include_prompt` is False exactly when a caller is
        # RE-SURFACING an older result rather than delivering a new one (`/to-chat`),
        # and a replay must not re-deliver into someone's conversation: the
        # in-memory dedup below only sees the target slot's bounded buffer, so a
        # result older than that window — or a restart that rebuilt the buffer —
        # reads as absent and appends the same run a second time.
        #
        # Scheduled after the flush above, not guaranteed to land after it: both
        # writes are offloaded, so this orders the dispatch and nothing more. They
        # address different keys (`cron:{id}` and the target's), so no interleaving
        # of the two can corrupt either transcript.
        if include_prompt:
            _mirror_result_to_target_session(state, job, result_row)
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


async def prefetch_cron_history(state: DashboardState, job_id: str) -> list[dict[str, Any]] | None:
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


async def ensure_cron_slot(state: DashboardState, job: "CronJob") -> None:
    """Make an eligible job's tab exist — and carry its identity — at run START.

    :func:`inject_cron_result_to_dashboard` is the other creator site for a
    ``cron-{job.id}`` slot, and it runs only after the turn finishes — too late
    for a brand-new job's FIRST run: session-control caller identity resolves
    through the live slot table (``caller_slot_key`` matches the presented
    ``cron:{job_id}`` against each slot's link), so without this pre-create every
    verb a first run calls refuses with ``caller_unidentified`` — on exactly the
    run a person watches after creating the job. The dashboard-surface registry
    has the same first-run dependency for sub-agent event routing, completion
    injection, and widget/question/approval delivery. From the second run onward
    the previous delivery's slot covers all of it.

    Eligibility lives HERE, not at call sites: only a job that will get this
    tab at delivery anyway (``job.persistent_session and not
    job.hide_in_chat``) is pre-created. For everything else,
    no-tab / no-identity / no-dispatch stays the deliberate fail-closed
    contract — an ineligible job is untouched by this call.

    Cheap on every run after the first: an existing linked slot returns before
    any transcript I/O. The first bind reads the ``cron:{id}`` history via
    :func:`prefetch_cron_history` (off-loop) BEFORE linking, because the link
    and the hydration must move together — see :func:`_bind_cron_slot`. The
    injection's own unlink guard then no-ops, so delivery behaves identically
    whether or not the tab was pre-created.
    """
    if not (job.persistent_session and not job.hide_in_chat):
        return
    slot = state.get_slot(f"cron-{job.id}")
    if slot is not None and slot.linked_session_key:
        return
    history = await prefetch_cron_history(state, job.id)
    _bind_cron_slot(state, job, history)


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
            mint_mid=False,
        )
