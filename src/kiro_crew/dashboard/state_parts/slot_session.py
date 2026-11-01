"""Who a slot is and what its session binds.

Identity and provenance, the relay-archive binding, model and effort choices
with the spawn-time withhold verdict, the served model, the crew-log
predecessor latch, memory and workspace, the channel link, title and summary
bookkeeping, deferred resets, eager spawn and the MCP report.

Instance fields of ``_ChatSlot`` this module reads and writes:

Writes: ``_app``, ``_auto_tagged``, ``_binding``, ``_channel_runtime_origin``, ``_created_by``,
``_created_by_sid``, ``_crew_log_opened_sid``, ``_crew_log_pending_channel``,
``_crew_log_previous_from_mapping``, ``_crew_log_previous_sid``,
``_crew_log_previous_undecided``, ``_detail_render_lock``, ``_eager_spawn_failures``,
``_eager_spawn_retry_at``, ``_eager_spawn_task``, ``_effort_pick_gen``, ``_fork_lock``,
``_human_seen``, ``_last_stop_reason``, ``_lineage_minted``, ``_mcp_report``,
``_mcp_report_session_id``, ``_memory_assignment_from_history``, ``_model_pick_gen``,
``_model_pick_lock``, ``_model_runtime_pick_gen``, ``_model_withheld``,
``_model_withheld_for``, ``_origin``, ``_pending_discard_conversation_key``,
``_deferred_model_switch_key``, ``_pending_discard_from_wake``, ``_pending_memory_mode``, ``_pending_model_pick``,
``_pending_reset_history_key``, ``_prefetch_ttl_task``, ``_reasoning_effort``,
``_reports_to_creator``, ``_revived_by``, ``_saved_created_by``,
``_session_requested_model``,
``_slack_channel``, ``_slack_linked``, ``_slack_thread_ts``, ``_summary_in_flight``,
``_summary_turn_mark``, ``_tab_id``, ``_title_epoch``, ``_title_in_flight``,
``_title_low_signal``, ``_title_origin``, ``_title_refresh_mark``,
``_title_retry_pending``, ``_title_task``, ``_titled``, ``_turn_timeout_hydrated``,
``_welcomed_agent``, ``agent_kind``, ``app_agent_owner``, ``autocompact_pct``,
``channel_origin``, ``created_at``, ``crew_mode_agent``, ``executor``, ``forked_from``, ``instance_id``,
``jev_route``, ``memory_store``,
``remote_slot``, ``served_model``, ``turn_timeout_secs``.
Reads only: ``model``.

A function whose first parameter is ``self`` is a ``_ChatSlot`` method: the
class binds it under the same name (``state_parts/chat_slot.py``), so it reads
and writes the slot's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.dashboard.transcript_binding import TranscriptBinding
from kiro_crew.history import MEMORY_MODES

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot


# Display label for a chat slot that has no real title yet — shown in the UI
# instead of the internal ``chat-N-<ts>`` key (which is an identifier, not a
# name). Applied at the serialization boundary (``_ChatSlot.display_title``),
# so a brand-new empty session, the pre-send window, and the pre-LLM window all
# read the same. The LLM auto-title / fallback replace it with a real title.
NEW_SESSION_TITLE = "New Session…"


# Matches a slot-key *identifier* used as a title (both the stripped
# ``chat-N-<ts>`` and the resumed ``dashboard_chat-N-<ts>`` forms). An untitled
# slot whose title is still such an identifier should display as
# NEW_SESSION_TITLE, not the raw key. Real titles never match this.
_SLOT_KEY_TITLE_RE = re.compile(r"(?:dashboard_)?chat-\d+-\d+$")


VALID_MEMORY_MODES = MEMORY_MODES


class CrewLogPrevious(NamedTuple):
    """What a `session/opened` should say about the store its slot was writing.

    Three states, because an empty ``sid`` carries two different facts and a log
    that records the wrong one reads as something it is not. ``sid`` set is the
    predecessor, named. Empty with ``undecided`` false says the slot has no earlier
    store, which makes this log a chain START. Empty with ``undecided`` true says it
    HAS one that could not be determined, so the log is a chain BREAK -- a later
    fold may pass over a chain start when ranking, and must refuse on a break rather
    than electing the log before it.

    A FOURTH state keeps those two honest: ``undecided`` ``None`` says nothing was
    determined either way. A source can come back empty because it looked and there
    is nothing, or because it had nothing to give -- a store holding units it cannot
    rank, handing the question on. Only the first is a finding about the slot.
    Recording the second as one would have a log with earlier siblings declare itself
    their chain start, in an append-only entry, and a later fold would pass over it.

    ``from_mapping`` says the ``sid`` came from the slot's session mapping rather
    than from the slot's own record or its units. That matters because the mapping
    is a generation behind while an allocation holds the prior resumable id for a
    provider that defers promotion, and whether it is doing so CANNOT be read where
    the resolver runs: the marker is an attribute of a live session, and the
    resolver runs before the session for this turn exists. A flagged id is
    therefore provisional, and the decision to cite it or record a break is made
    where the marker is answerable.
    """

    sid: str
    undecided: bool | None
    from_mapping: bool = False


def init_session_fields(self: _ChatSlot) -> None:
    """Set this module's slot fields to their defaults (``_ChatSlot.__init__``)."""
    # Which namespace ``agent`` was chosen in: "member" (a configured crew),
    # "template" (a shared provider template), or "" when the choice was
    # made by name alone or restored from history. Display provenance for
    # the picker; never an authorization input.
    self.agent_kind = ""
    # The agent Crew Mode turned on with ("" when the chat never turned it on):
    # stamped by the agent switch (``crew_mode.stamp_for_switch``) and persisted,
    # so the chat stays in Crew Mode after ``agent.crew_mode_agent`` changes.
    self.crew_mode_agent = ""
    # ``"<app>/<agent>"`` when this session was opened by an app's palette
    # command to run as that app's OWN agent; "" otherwise. Every turn checks it
    # (``chat_runner``): the bare ``agent`` name must still resolve to that app's
    # registered spec, never to a project-local or other spec declaring the same
    # name, or the app-authored prompt would run with that spec's tools. Bound to
    # the agent name, so a later switch to another agent leaves it inert.
    self.app_agent_owner = ""
    # The agent whose ``welcomeMessage`` this slot has already rendered.
    # The hint is a ONE-SHOT per activation: the switch row emits it and
    # the session start that the switch's own reset produces must not emit
    # it again, so both paths clear through this single field rather than
    # each guessing whether the other already ran. Not persisted — the row
    # itself is, and a re-emit after a gateway restart costs one duplicated
    # notice rather than a per-turn repeat.
    self._welcomed_agent = ""
    # Whether the owner asked Jev to pick this turn's model tier
    # (`decisions.points.model_route`), set by the picker's "Auto (Jev)" entry
    # and cleared by any concrete pick. A FLAG beside `model`, never a
    # sentinel inside it: `model` is a provider model id -- it reaches
    # `session/set_model`, the session allocation and the composer chip -- and
    # a value no provider advertises would have to be filtered at each of
    # those, which is one filter per reader and a real breakage the first time
    # one is missed. The flag leaves `model` meaning exactly what it meant.
    self.jev_route = False
    # Spawn-time withhold verdict for `model`, and the model id it was
    # computed for. Read through the `model_withheld` property, never these
    # two directly: the pairing is what makes the verdict self-invalidating
    # when any of slot.model's writers re-pins the slot.
    self._model_withheld = False
    self._model_withheld_for = ""
    # The model selection handed to the provider allocation that produced
    # the live session. None means this process did not observe that
    # allocation; "" means every selection tier deferred to the backend.
    # Session/opened reads this instead of re-resolving at first turn, since
    # an eager allocation can outlive a config change.
    self._session_requested_model = None
    # The crew log store this slot was writing BEFORE any allocation this
    # process performed, latched at the first observation and not overwritten
    # by a later one. Two sites allocate for a slot -- the eager prefetch and
    # the first real turn -- and the eager one publishes its successor over
    # the slot's mapping before the turn runs, so a turn that read the mapping
    # itself would read the successor and name no predecessor at all. Written
    # only while empty, because the second observation is the successor rather
    # than an earlier store. Cleared once `session/opened` has carried it, so
    # the next supersede of this slot latches afresh. "" = nothing to follow.
    self._crew_log_previous_sid = ""
    # Whether the resolver COULD NOT NAME this slot's predecessor, as opposed to
    # there being none. Both leave the id above empty and they are different
    # facts: the first says an edge exists and is unrecorded, the second says the
    # log is a chain start. The announce writes them differently so a later fold
    # can pass over the chain start and refuse on the unrecorded one. ``None`` is
    # the third fact and the default: nothing was determined either way, so the
    # announce states nothing -- which is what a source handing the question on
    # leaves behind, and what a slot no resolver has answered for holds.
    self._crew_log_previous_undecided = None
    # Whether the id above came from the slot's SESSION MAPPING rather than from
    # this process's own record of the store the slot is on. A mapped id is
    # provisional, because the mapping is deliberately a generation behind while
    # an allocation holds the prior resumable id for a provider that defers
    # promotion -- and whether it is doing so cannot be read when the id is
    # latched, since the marker belongs to a session that does not exist yet.
    # The edge is downgraded to a break as it is taken, where the answer is real.
    self._crew_log_previous_from_mapping = False
    # The store a `session/opened` of this slot was last written FOR, recorded
    # as the edge above is handed over. It is what the slot's next allocation
    # names as its predecessor: the mapping can be a generation behind while a
    # replay is pending, and the store's own units carry a wall-clock stamp and
    # are written by a background writer that may not have run yet. "" = this
    # process has not opened a crew log for this slot.
    self._crew_log_opened_sid = ""
    # Whether the crew-log class recorder has recorded this slot as published to
    # a channel. `chat_runner._crew_log_class` folds it into the class (the name is
    # `chat_runner.PENDING_CHANNEL_ATTR`), so a log opened after the link is gone
    # still records the restriction. Never cleared.
    self._crew_log_pending_channel = False
    # The model id the live session resolved to, for a slot that is
    # inheriting rather than pinning. "" = unknown. Written through
    # `record_served_model`.
    self.served_model = ""
    # Reasoning effort: "" = provider default, else one of
    # low/medium/high/xhigh/max (``EFFORT_LEVELS`` in ``effort.py``). A live
    # session applies it as ``session/set_config_option`` on the backends in
    # ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` (claude, codex, deepseek, KAS as
    # ``effortLevel``, pi) and as the ``/effort`` command on kiro-cli.
    # Every write that changes the level bumps ``_effort_pick_gen`` (see the
    # property), so a queued session_set_model effort can tell that a newer
    # choice landed even when that choice returned to the level it saw.
    self._effort_pick_gen = 0
    self._reasoning_effort = ""
    # Per-session auto-compact threshold override (percent). None = follow
    # the global session.autocompact_pct. Persisted with the slot and
    # re-seeded into the SessionManager after restore.
    self.autocompact_pct = None
    # Per-session turn time limit (seconds). None = follow the global
    # agent.chat_turn_timeout_secs. Persisted with the slot; read by the turn
    # dispatch (turn_dispatch.resolve_turn_limit) when each turn starts.
    self.turn_timeout_secs = None
    # False while a reopened tab's own limit is still unread from its
    # transcript: its saves carry the on-disk value forward meanwhile.
    self._turn_timeout_hydrated = True
    # The crew's memory silo, or "" for the global store. Held on the slot
    # rather than re-resolved per save because it is SLOT-OWNED metadata:
    # absence retracts it, so a save that could not name it would drop the
    # binding and silently return that session to the global store.
    self.memory_store = ""
    # Transcript fields can restore display state, never a new private
    # assignment. Only a protected binding or an explicit owner pick clears
    # that admission boundary; this marker is not persisted in the transcript.
    self._memory_assignment_from_history = False
    # A private binding until ``bind_transcript`` joins it to a live alias group.
    self._binding = TranscriptBinding()
    # Relay-archive binding, read off disk. ``executor`` is "local" for every
    # ordinary slot; "remote" marks a chat an older build ran on the peer
    # ``instance_id`` (its slot ``remote_slot``). Such a slot is read-only:
    # every run path refuses it (``dashboard/relay_archive.py``), whether or
    # not the other two fields are populated.
    self.executor = "local"
    self.instance_id = ""
    self.remote_slot = ""
    self.created_at = datetime.now(timezone.utc).isoformat()
    self._titled = False  # True once a title has been assigned
    # Provenance of the current title: "auto" (LLM auto-titler or its
    # fallback) or "user" (manual rename). Governs the background title
    # REFRESH: only "auto" titles are ever refreshed, so a manual rename is
    # final. Persisted as ``title_origin`` and rehydrated in
    # chat_persistence; a legacy title with no stored origin rehydrates as
    # "user" so a possibly-manual name is never rewritten. "" = untitled.
    self._title_origin = ""
    # Monotonic counter bumped on every EXPLICIT title assignment (manual
    # rename or the manual generate-title endpoint). Title generators --
    # the background tasks and the foreground generate-title endpoint
    # alike -- snapshot it before generating and re-check it before
    # applying what they generated, so an explicit title landing
    # mid-generation is never overwritten (see
    # chat_title._maybe_auto_title / maybe_refresh_title /
    # api_chat_slot_generate_title).
    self._title_epoch = 0
    # User-message count at the last background title refresh ATTEMPT (0 =
    # never refreshed). Each milestone in chat_title._TITLE_REFRESH_MILESTONES
    # fires at most once, attempt-counted (a KEEP/SKIP/error consumes it), so
    # the refresh token budget is hard-bounded. Persisted so a gateway
    # restart cannot re-spend consumed milestones.
    self._title_refresh_mark = 0
    # True when the current AUTO title was derived from a low-signal first
    # message (URL/identifier-dominated, e.g. a pasted ticket link) — the
    # one case where the name can only restate the link. Makes the title
    # refresh due once the first turn's transcript exists (see
    # chat_title._TITLE_EARLY_REFRESH_MILESTONE) instead of waiting for the
    # first ordinary milestone. Persisted as ``title_low_signal`` and
    # rehydrated in chat_persistence; absent on legacy sessions = False.
    self._title_low_signal = False
    self._auto_tagged = False  # True once auto-tag has been attempted
    # Guards against concurrent LLM auto-title attempts (on-send trigger vs
    # the end-of-turn chat_done trigger racing on the same slot).
    self._title_in_flight = False
    # Handle of the on-send auto-title task (chat_handlers), so chat_done's
    # chained title→refresh pass can WAIT for the in-flight attempt to
    # settle instead of bouncing off the ``_title_in_flight`` guard. Without
    # the wait, a slow on-send attempt locks a low-signal title AFTER both
    # chained calls returned — and a one-message session gets no later
    # chat_done to spend its early refresh milestone. Never persisted.
    self._title_task = None
    # Records a chat_done retry that arrived during the on-send attempt.
    self._title_retry_pending = False
    # Excludes concurrent session-summary generations for this slot. A
    # summary pass outlives the turn that triggered it, so a fast follow-up
    # turn would otherwise start a second pass over the same transcript.
    self._summary_in_flight = False
    # Serializes the slot-detail render offload (see api_chat_slot_detail):
    # rendering redacts the ENTIRE history with a regex battery, so on a
    # multi-MB session two concurrent refetches (WS reconnect + switchSlot)
    # would burn that CPU twice in parallel worker threads for the same
    # payload. The lock queues them instead; each holder re-renders from
    # fresh state, so a queued waiter never serves a stale response.
    self._detail_render_lock = asyncio.Lock()
    # User-turn count at the last successful summary, so the configured
    # regeneration cadence can be honored without re-reading the sidecar.
    self._summary_turn_mark = 0
    # Stop reason of the most recently completed turn. Recorded because a
    # turn that ended on a timeout, cancel or tool stall did not really
    # finish, and deriving anything from it would describe work that was
    # interrupted mid-flight as if it had concluded.
    self._last_stop_reason = ""
    #: The resolved slot key of the caller that asked for this session via the
    #: session-control create verb, or "" for a slot nobody asked for -- a
    #: person's own tab, a fork, a restore. Read by
    #: ``DashboardState.creator_slot_count`` for ``MAX_SLOTS_PER_CREATOR``.
    self._created_by = ""
    #: Shared Crew creator attribution (``shared_crew.attribution``): ``None``
    #: until stamped or read back, then write-once. Persisted by the
    #: metadata codec's ``attribution`` row.
    self._attribution = None
    #: The ``created_by`` the session's saved metadata line names, read on every
    #: hydration (restore, recent, resume). DISPLAY ONLY: the Crew Mode stamp reads
    #: it to keep a reopened worker under its root (``crew_mode.stamp_rows``). It
    #: grants nothing: session-control ownership reads ``_created_by`` alone.
    self._saved_created_by = ""
    #: The creator's ACP session id, FROZEN at mint (see ``session_control``).
    #: Read at the child's first turn to stamp ``parent_sid`` on the immutable
    #: ``session/opened`` crew log entry -- never re-read live, so a creator slot
    #: closed and replaced after mint cannot corrupt this child's lineage.
    #: In-memory only: it is never written to or restored from the transcript,
    #: because that file is editable by an agent's file tools and the crew log
    #: is fenced from them precisely so nothing there can be forged as
    #: gateway-authored.
    self._created_by_sid = ""
    #: True only on a slot THIS gateway process minted through the
    #: session-control create verb. Never persisted or restored: it is the
    #: witness that ``_created_by`` / ``_created_by_sid`` were stamped by the
    #: gateway at mint rather than read back from transcript metadata, and the
    #: crew log ``session/opened.parent`` lineage is written only when it is set.
    #: A child whose gateway restarted before its first turn writes no
    #: ``parent`` -- ``_created_by`` alone is restored for authorization, never
    #: promoted to lineage.
    self._lineage_minted = False
    #: True once the slot's own creator sent it a message through session-control
    #: in THIS process. With ``_lineage_minted`` it is the live witness that a
    #: crewmate's worker report may wake its creator
    #: (``dashboard.mate_thread_wakes``); never persisted or restored.
    self._reports_to_creator = False
    #: Slot key of the session-control caller that REVIVED this slot from
    #: history, or "". Cap attribution only: ``creator_slot_count`` counts it
    #: beside ``_created_by`` for the per-caller slot cap, since a revive keeps
    #: the target's own creator and would otherwise be free. In memory only,
    #: never persisted or restored, never read for ownership.
    self._revived_by = ""
    # What THIS slot's agent session reported about its MCP servers, as
    # published by the ACP layer at session init and updated by later
    # registration frames. None = this slot has no live session that
    # reported, which the UI must render as absence of knowledge — NOT as
    # "no servers". Cleared on session reset: the report is evidence, and a
    # report describing a torn-down session is worse than none.
    self._mcp_report = None
    # The session the cached report describes — see set_mcp_report.
    self._mcp_report_session_id = ""
    # Set by api_chat_slot_project; consumed in _run_chat instead of
    # inline because the endpoint can be reached from inside the kiro-cli
    # process group via the set_project MCP tool.
    self._pending_reset_history_key = None
    # Set by the reset_conversation directive; consumed alongside the
    # project-change reset above. Deferred for the same reason: the tool is
    # called from inside the turn it wants to end, and the immediate route
    # refuses a busy slot rather than tearing down a turn mid-write.
    self._pending_discard_conversation_key = None
    # True when a patrol wake queued that discard: it then also waits, at
    # the boundary, while a question card or tool approval is pending.
    self._pending_discard_from_wake = False
    # Set by session_set_model on an idle slot; consumed at the start of the
    # next turn (session_control.apply_pending_model_pick), which re-checks
    # the caller's authorization and commits the model in one synchronous
    # step. Runtime only: a pick does not survive a gateway restart.
    self._pending_model_pick = None
    # Set by api_chat_slot_model when the pick arrives while a turn is in
    # flight: slot.model is already committed, and this names the session the
    # live session was on, so the turn boundary knows which session still runs
    # the old model. Consumed by session_control.apply_deferred_model_switch at
    # the end of that turn (and at the next turn's start as the backstop), which
    # moves the now-idle session live or arms the deferred reset. Runtime only:
    # a restart cold-starts on the committed slot.model anyway.
    self._deferred_model_switch_key = ""
    # Debounced speculative session-creation task (session.eager_spawn).
    # At most one per slot: scheduling a new one cancels the previous, so
    # rapid signals (create + project set) collapse into a single spawn.
    self._eager_spawn_task = None
    # Consecutive failed background starts, and the monotonic time before
    # which the next one waits (chat_runner._note_eager_spawn_failure).
    self._eager_spawn_failures = 0
    self._eager_spawn_retry_at = 0.0
    # Unclaimed-prefetch teardown timer (resume prefetch). At most one per
    # slot: a newer resumed prefetch cancels the previous timer.
    self._prefetch_ttl_task = None
    self._slack_linked = False  # True when linked to a Slack thread
    self._slack_channel = ""
    self._slack_thread_ts = ""
    # Explicit model-pick generation. Bumped ONLY by the explicit set-model
    # surfaces (single-slot pick, bulk switch, provider-switch clear) —
    # never by the automatic provider backfill — so the fallback restore
    # probe can tell a genuine user pick (drop sticky state, never
    # override) from the backfill writing the served fallback into an
    # unpinned slot (heal and restore). _fallback_pick_gen is the value
    # snapshotted when the fallback activated.
    self._model_pick_gen = 0
    # The pick generation a RUNTIME write produced (the provider-switch clear),
    # or None. That write bumps _model_pick_gen like a pick does, for the
    # fallback probe's sake, but nobody chose it: a reader that treats a moved
    # generation as a person's pick (the Issue Radar crew's triage model
    # switch) skips a generation that equals this one.
    self._model_runtime_pick_gen = None
    # A save thread records a stricter folded line mode for loop-side adoption.
    self._pending_memory_mode = None
    self._app = ""  # App identity tag (App Kit §5.2)
    # Unattended approval park. Evidence that a HUMAN has driven
    # this slot through a dashboard-user route (typed a message, answered an
    # approval). Only ever set by a caller with an empty ``request_app``, so
    # an app cannot forge it. It is the escape hatch on ``unattended``: an
    # app-owned tab a person is actually working in gets the full 2h
    # approval window back from their first interaction onward.
    #
    # PERSISTED (``human_seen`` in the session metadata, restored by both
    # slot-restore paths) and monotonic — it only ever goes False → True, so
    # it needs no clearing rule and the ``auto_tagged`` once-flag beside it
    # is the shape to copy. Persistence is load-bearing rather than tidy: a
    # gateway restart is not evidence that the person left. It happens on
    # every upgrade and every crash, the browser tab reconnects to the same
    # slot, and without the flag on disk that tab's approval window silently
    # collapses from 2h to the 180s deny-fast — a behaviour change for EVERY
    # app-owned session, not just for worker fleets. The fast deny still
    # covers every app-owned slot no human has ever touched, which is what
    # a crew, a cron worker and an app-spawned session all are.
    self._human_seen = False
    # Deliberately "" (not USER): a slot built outside get_or_create_slot
    # matches NO slots:* scope, so it stays invisible to app tokens rather
    # than being silently classified as user-initiated. Deny-by-default.
    self._origin = ""
    self.forked_from = None  # parent slot key if this is a fork
    self._fork_lock = asyncio.Lock()  # serialises concurrent forks on this slot
    # Serialises explicit model-pick transactions (check → mutate → live
    # switch → rollback) on this slot: picks interleaving at the set_model
    # await could otherwise roll back each other's state. Deliberately NOT
    # slot._lock, which guards message-window edits and must not be held
    # across a multi-second network await.
    self._model_pick_lock = asyncio.Lock()
    self._tab_id = ""  # permanent tab identity for cross-restart session chaining
    # True only when this slot was created to DISPLAY a conversation that
    # already lives in a channel transcript (the reconciler surfacing a
    # thread, a restore, a History resume). It is what separates such a tab
    # from a dashboard slot that merely happens to be NAMED like one --
    # a filename-shaped name is not provenance, and inferring it from the
    # name would let `POST /api/chat/slots` with a colliding `slack_<ts>`
    # name write a fresh conversation into an existing thread's transcript.
    self.channel_origin = False
    # Set ONLY by the path that surfaced this slot from a channel session THIS
    # process observed. `channel_origin` cannot carry that weight: it round-trips
    # through the transcript's own metadata line, which an agent can write, so a
    # lookalike named for a live stem can arrive already claiming it. Never
    # persisted, so a restored slot always starts without it.
    self._channel_runtime_origin = False


def set_mcp_report(self: _ChatSlot, report: dict[str, Any] | None, session_id: str = "") -> bool:
    """Replace this slot's MCP session report. True when it changed.

    The payload is built and sanitized by
    :class:`kiro_crew.acp.mcp_session_report.McpSessionReport`, which owns
    redaction and the per-bucket caps; this only stores what it produced, so
    a caller cannot widen those bounds by writing here.

    ``session_id`` is the session the report DESCRIBES. It is stored beside
    the payload because this copy outlives its owner: the report itself lives
    on the transport and is inherently the session's, but a cached
    projection is only as valid as the identity it was taken under.
    """
    normalised = report if isinstance(report, dict) else None
    if normalised == self._mcp_report and session_id == self._mcp_report_session_id:
        return False
    self._mcp_report = normalised
    self._mcp_report_session_id = session_id if normalised is not None else ""
    return True


def clear_mcp_report(self: _ChatSlot) -> bool:
    """Drop the report because this slot's session is gone. True if it had one.

    Distinct from ``set_mcp_report(None)`` only in intent: it is called from
    the session-reset funnel so a report can never outlive the session it
    describes and be read as the next one's.
    """
    return self.set_mcp_report(None)


def mcp_report_payload(self: _ChatSlot) -> dict[str, Any] | None:
    """The stored MCP session report, or None when this slot has none."""
    return self._mcp_report


def record_model_withheld(self: _ChatSlot, withheld: bool | None) -> None:
    """Pin a spawn-time withhold verdict to the model it was computed for.

    ``None`` forgets the verdict (back to unknown) -- what a session
    teardown does, since the verdict describes the session that advertised
    the list, not the slot.
    """
    if withheld is None:
        self._model_withheld = False
        self._model_withheld_for = ""
        return
    self._model_withheld = withheld
    self._model_withheld_for = self.model or ""


def record_served_model(self: _ChatSlot, model_id: str | None) -> None:
    """Record the model id the live session actually resolved to.

    The counterpart to :meth:`record_model_withheld` for the case that
    verdict cannot describe: an INHERITED model. A slot pinned to nothing —
    or one whose pin was withheld — runs on whatever the backend assigned,
    and the pin alone cannot name it, so the composer chip can only say
    "auto". This carries the id the session reports, so the chip can name
    the model a turn will actually use.

    ``None``/``""`` forgets it (back to unknown) — what a session teardown
    does, since the id describes that session and not the slot. Unlike the
    withhold verdict it is NOT pinned to ``slot.model``: it answers a
    question about the SESSION, which the pin does not determine.

    DISPLAY only. Never a write source: the persisted pin stays whatever the
    user chose.
    """
    self.served_model = model_id or ""


def latch_crew_log_previous(
    self: _ChatSlot, sid: str, *, undecided: bool | None = None, from_mapping: bool = False
) -> None:
    """Remember the crew log store this slot was writing, if none is remembered.

    Called by every site that is about to ALLOCATE a session for this slot,
    before the allocation publishes its own id over the slot's mapping. The
    write is conditional on the latch being empty, and that is the whole
    point: the eager prefetch and the first real turn both allocate, and by
    the time the turn runs the prefetch has already mapped the successor, so a
    second observation names the successor rather than an earlier store.
    Keeping the FIRST observation keeps the predecessor a `session/opened` can
    cite, and an empty ``sid`` latches nothing rather than latching a store
    with no name.

    ``sid`` is what the slot's MAPPING answers, and the mapping is a proxy for
    this question rather than its authority. An allocation whose history replay
    is pending keeps the prior resumable id there deliberately, so that the id
    a restart can resume stays durable -- and for that window the mapping names
    a generation OLDER than the newest store this slot wrote. Latching it makes
    two successive stores cite one predecessor and leaves the store between
    them cited by nobody, which is the single chain gap a walker cannot detect:
    both neighbours are well formed and neither says a store is missing.

    So what this slot last handed to a `session/opened` decides, and ``sid``
    serves only when that is empty -- a slot this process has not yet opened a
    crew log for. The slot's own record is the authority because it is the
    statement of the writer itself, taken at the moment the store became this
    slot's current one, which no other source observes: the mapping tracks
    resumability instead, and the store's own units carry a wall-clock stamp
    and are written by a background writer that has not run yet.
    """
    if self._crew_log_previous_sid:
        return
    chosen = self._crew_log_opened_sid or sid
    if chosen:
        self._crew_log_previous_sid = chosen
        # The flag describes THIS latch, so the winning branch clears it rather
        # than leaving an earlier one's reason standing. An earlier latch can have
        # set it with no sid -- the prefetch's store read refused while this turn's
        # resolver then named one -- and the two halves leave together, so a stale
        # true would hand the entry a named edge reported undetermined, which is a
        # pair the entry's own reader is promised never to see.
        self._crew_log_previous_undecided = False
        # Provisional only when the MAPPING supplied the id. The slot's own record
        # wins over ``sid`` here, and that record is this process's own statement
        # about which store the slot is on, so it is never provisional. Set on this
        # branch ALONE, which is what makes it mean "a mapped id is latched": an
        # answer naming nothing has no provenance to record, and flagging it would
        # have the take write a break claiming a predecessor exists.
        self._crew_log_previous_from_mapping = bool(from_mapping and not self._crew_log_opened_sid)
        return
    # Nothing nameable. ``undecided`` says WHY, and only here can it be known:
    # the resolver that could not read the store is the one caller that can tell
    # "this slot has no earlier store" from "it has one I could not name" from
    # "nothing here determined either".
    #
    # ASSIGNED, not merely set. Two latches before one entry is owed is ordinary,
    # since the eager prefetch and the turn each run their own resolver, and a
    # later answer supersedes an earlier one: a prefetch whose store read REFUSED
    # carries no information about the content, so leaving its refusal standing
    # would have the entry report a predecessor as existing-but-unnameable for a
    # slot the turn's own successful read determined has none. The branch above
    # does the same for the reason beside a named id.
    self._crew_log_previous_undecided = undecided


def take_crew_log_previous(
    self: _ChatSlot, *, now_writing: str, replay_pending: bool = False
) -> "CrewLogPrevious":
    """The latched predecessor edge, clearing it as it is handed over.

    Read-and-clear, because the value is owed to exactly one
    ``session/opened``: leaving it behind would make the NEXT store of this
    slot cite a predecessor two links back and skip the store between them,
    which is the one thing a chain walker cannot detect. An empty ``sid`` with
    ``undecided`` false is "no edge to write".

    Both halves leave in ONE call for the same reason ``now_writing`` does: the
    sid and the reason it is empty are one statement, and a caller that could
    take the sid alone would write a log that claims to be a chain start when
    the truth is that its predecessor was never determined.

    ``replay_pending`` is asked HERE, not where the id was read, and the
    placement is the point. A latch happens before this turn's session is
    allocated, and the replay marker is an attribute of a live session, so a
    resolver asking it gets "no replay owed" both when none is owed and when
    there is nobody to ask -- and the second of those is a cold start, which is
    precisely when the mapping is most likely to be holding the older
    generation. By the time an entry is taken the session exists, so the answer
    means what it says. It applies only to an id the MAPPING supplied: the
    slot's own record is this process's statement about which store it is on.

    ``now_writing`` is the store that entry is FOR, and recording it here is
    what lets the slot's next allocation name a predecessor without consulting
    anything outside this process. It is recorded whether or not an entry is
    written, since it states which store the slot is on rather than what was
    appended.
    """
    edge = CrewLogPrevious(
        sid=self._crew_log_previous_sid, undecided=self._crew_log_previous_undecided
    )
    if self._crew_log_previous_from_mapping and replay_pending:
        # The mapping supplied this id and it is knowingly a generation behind:
        # allocation holds the prior resumable id there for a provider that
        # defers promotion. Citing it makes two successive stores name one
        # predecessor and leaves the store between them cited by nobody. The id
        # still PROVES a predecessor exists, so the honest entry is a break.
        #
        # No emptiness test beside the flag, because the flag is set only where a
        # sid was latched: an answer that named nothing is not provenance, it is
        # the absence of one, and a break claiming a predecessor exists must not
        # be written for it.
        #
        # The downgrade happens here rather than where the id was read because
        # only here is the question answerable. The marker lives on a live
        # session, the resolver runs BEFORE this turn's session exists, and a
        # missing session reads as "no replay owed" -- which is exactly the
        # cold-start case where the mapping is most likely to be holding the
        # older generation.
        edge = CrewLogPrevious(sid="", undecided=True)
    self._crew_log_previous_sid = ""
    # Cleared to "nothing determined", not to "determined there is none": with the
    # edge spent, no resolver has answered for whatever store this slot opens next,
    # and an entry written before one does must state nothing rather than claim to
    # start the slot's chain.
    self._crew_log_previous_undecided = None
    self._crew_log_previous_from_mapping = False
    if now_writing:
        self._crew_log_opened_sid = now_writing
    return edge


def forget_session_model_state(self: _ChatSlot) -> None:
    """Drop every fact that described the session being torn down.

    The withhold verdict and the served model id are a PAIR: both describe
    the session that advertised the list, not the slot, so a teardown that
    forgets one and keeps the other labels the next session with the
    previous one's answer. Every teardown site calls this one method so a
    site added later cannot drop half the pair.
    """
    self.record_model_withheld(None)
    self._session_requested_model = None
    self.record_served_model(None)
