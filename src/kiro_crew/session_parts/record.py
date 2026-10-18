"""One registered session: its record, its first-turn observation and its queued files."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from kiro_crew.session import LLMProvider
    from kiro_crew.session_capabilities import LoadedCapabilities


StopOutcome = Literal["soft", "hard", "idle", "compacting"]


class FirstTurnState(Enum):
    """One-shot first-turn observation on a ``_Session``.

    Records what the session's creator observed at provider start, for the
    first REAL claimant to consume atomically under the per-session semaphore
    (a speculative claimant reads without consuming). A single three-member
    field — not a pair of booleans — so the illegal fourth combination the
    old ``is_new``/``resumed_armed`` pair could represent by accident (a
    resume marker armed on an already-claimed session, which would silently
    skip history injection) has no spelling.

    Internal representation only: public return shapes stay
    ``(provider, is_new, resumed)``, derived via :attr:`is_new` /
    :attr:`resumed` at the return boundary.
    """

    # Session already claimed — no first-turn observation armed. The state
    # every session reaches once a real turn has consumed the observation
    # (and the state ``get_or_create``'s real creator registers, having
    # consumed its own; ``open_task_session``'s cold path instead registers
    # FRESH unconsumed, exactly as it left ``is_new`` armed before).
    NOTHING_ARMED = auto()
    # Fresh session: the first real turn injects Kiro Crew history.
    FRESH = auto()
    # Natively resumed — a speculative resume creator's ACP ``session/load``
    # restored the persisted transcript, so the first real turn must skip
    # history injection.
    RESUMED = auto()

    @property
    def is_new(self) -> bool:
        """The ``is_new`` boolean this state derives to at the return boundary."""
        return self is not FirstTurnState.NOTHING_ARMED

    @property
    def resumed(self) -> bool:
        """The ``resumed`` boolean this state derives to at the return boundary."""
        return self is FirstTurnState.RESUMED


@dataclass
class _Session:
    provider: LLMProvider
    last_used: float = field(default_factory=time.monotonic)
    # Wall-clock spawn time, for the uptime column on the session-memory surface.
    # ``last_used`` is monotonic (correct for idle math, but it has no epoch), so
    # it cannot answer "how long has this session been alive".
    created_at: float = field(default_factory=time.time)
    # One-shot first-turn observation, consumed by the first REAL claimant
    # under the per-session semaphore (a speculative claimant reads without
    # consuming). A single ``FirstTurnState`` field replacing the old
    # ``is_new``/``resumed_armed`` boolean pair, so arming a resume marker on
    # an already-claimed session is unrepresentable rather than forbidden by
    # convention. ``RESUMED`` is selected only by a SPECULATIVE creator whose
    # provider start restored the persisted transcript via ACP session/load —
    # the existing-session fast path and the won-race path otherwise report
    # ``resumed=False``, which would make the real first turn inject Kiro
    # Crew history on top of the natively-replayed transcript. Selected
    # atomically at registration; read and cleared in one consume.
    first_turn: FirstTurnState = FirstTurnState.FRESH
    # Set when an identity sweep found this session BUSY and therefore left its
    # in-flight turn alone. The child is authenticated as an account that is no
    # longer signed in, so the NEXT turn on this key must not reuse it: the
    # post-semaphore re-validate reads this and reports the session invalid, and
    # the caller's existing stale-provider path evicts it and cold starts. Default
    # False so every existing construction site is unaffected.
    retire_on_identity_change: bool = False
    # The ``backend_override`` the session was cold-started with (``None``: the
    # factory's default backend), and the caller's ``extra_env`` and
    # ``reasoning_effort_override``. Carried by ``allocation_identity`` so a reset
    # successor or a compaction restart starts on the same harness, env and effort.
    # The env is the caller's own map: the successor's allocation applies the
    # ``aws.project_profiles`` binding again. It can hold secrets, so no ``repr``.
    backend_override: str | None = None
    extra_env: dict[str, str] | None = field(default=None, repr=False)
    reasoning_effort_override: str | None = None
    # The app launch copy kiro-cli was asked for in place of ``agent``
    # (``apps.agent_launch``); ``""`` for every other session. Carried by
    # ``allocation_identity`` so a reset successor or a compaction restart is
    # asked for the same copy rather than the bare name.
    app_launch_agent: str = ""
    # The profile ``aws.project_profiles`` set on this session's process at its
    # cold start; ``None`` when the binding set none (no map, an unbound
    # directory, a caller that chose its own profile, a pooled process). A
    # subagent shares this process only when its own directory binds the same
    # profile (``aws_profile_differs_from_parent``).
    aws_project_profile: str | None = None
    # True while the lease is held for a LIFETIME rather than a turn -- see
    # ``session_lifecycle._turn_in_flight``, which is what asks.
    lifecycle_lease: bool = False
    # Set by a lifecycle holder for the whole of its turn, INCLUDING the setup before the
    # provider registers one. ``has_active_turn`` cannot see that window.
    lifecycle_turn_active: bool = False
    prompt_count: int = 0
    consecutive_failures: int = 0
    # Bounded rather than plain: a release() call that lands on this object
    # after get_or_create() has already replaced it at the session key (see
    # SessionManager.release) must raise instead of silently pushing the
    # counter above 1, which would let a second turn acquire concurrently
    # with one still in flight.
    semaphore: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(1))
    # The task that acquired the turn permit above, recorded at every acquire
    # site and read by ``reset``: a session popped while its permit is held
    # remembers WHO held it, so that task's later key-only ``release`` is
    # absorbed instead of unlocking whatever successor now occupies the key.
    # The release that ends a recorded holding clears it in the same step, so it
    # never names a task that has let the permit go.
    turn_owner: Any = None
    # Set by the allocation layer when a claim or registration adopted entries a
    # forced stop parked (``session_lifecycle.adopt_parked_queue``); ``release``
    # reads and clears it to wake those entries' channel drains once the lease
    # is free.
    adopted_parked: bool = False
    approval_policy: str = ""  # "" (interactive) | "auto" (auto-approve all tools)
    agent: str = ""  # kiro agent name used for this session
    capability_member: str = ""
    # The model this session's allocation SELECTED, as handed to the provider.
    # Stamped at registration from the same local the factory receives, so the id
    # sent and the id readable here are one value and cannot diverge.
    #
    # It exists because a caller that pins nothing at any of its own tiers passes
    # ``model=None`` and the allocation resolves one itself, inside a call whose
    # return says only which provider, whether it is new, and whether it resumed.
    # A caller recording what it asked for therefore has nothing to record for that
    # allocation, while the session runs on a concrete id. Read through
    # ``SessionManager.allocation_requested_model``.
    #
    # ``""`` means this allocation resolved nothing, or a registration site that
    # does not resolve models made the session. Both are "no selection to report",
    # which is what a consumer of the empty value states.
    requested_model: str = ""
    # The crew log this session SUPERSEDED: what the slot-to-session mapping named
    # -- its live id, or the stash a recycle left -- at the instant the allocation
    # registered this session, read inside that registration's critical section
    # and before this session's own id was mapped. Stamped there because no read a
    # caller takes around ``get_or_create`` can be right: waiting inside the
    # allocation for the turn permit, it can be overtaken by a concurrent turn's
    # allocate-and-recycle on the same key. Kept on the session rather than in a
    # per-key table so ordinary teardown releases it: every ``/new`` or generation
    # rotation mints a fresh key, and a table keyed by them would grow for the
    # life of the gateway. Read through ``SessionManager.allocation_predecessor``.
    predecessor_sid: str = ""
    loaded_capabilities: LoadedCapabilities | None = None
    # Slack message queue: FIFO of (msg_ts, text, kwargs) waiting for the semaphore
    queue: deque[tuple[str, str, dict]] = field(default_factory=deque)
    # Set when this session's last turn was cancelled via soft-stop.
    # kiro-cli discards cancelled turns from its conversation log, so callers
    # must re-inject the cancelled turn (user prompt + partial assistant) as a
    # preamble on the next prompt. One-shot: consumers clear after use.
    prev_turn_cancelled: bool = False
    # Set when a provider switch, failed native resume, or Tool Search
    # compatibility fallback creates a fresh provider that still needs Kiro Crew
    # history. Non-destructive slash commands read without clearing; a confirmed
    # native `/clear` consumes it so replay cannot undo the user's deletion. The
    # first provider event records acceptance only in the dashboard runner's
    # turn-local state; the shared lease remains armed until a clean,
    # non-synthetic, non-empty end_turn atomically promotes the fresh SID and
    # consumes it. Cancellation, raised streams, empty verdicts, and synthetic
    # terminals leave it armed because kiro-cli discards or cannot prove those
    # turns. This preserves replay across empty streams, pre-output failures, and
    # soft Stops while surviving loss of the separate ``first_turn`` observation.
    provider_switch_replay: bool = False
    # Carries the FRESH first-turn history debt that ``first_turn`` cannot: the
    # ``first_turn`` observation is a one-shot consumed at claim time, so a turn
    # that assembled Kiro Crew history and then failed BEFORE the provider
    # accepted it (a pre-output backend 5xx the transient path re-queues onto the
    # SAME live session) spends the observation while the history never lands —
    # the re-queued turn then reads ``is_new=False`` and sends the replay bare.
    # Armed from the FRESH observation at claim, OR'd into ``_context_is_new`` so
    # the re-queued turn rebuilds history, and consumed once a non-slash
    # context-bearing turn delivers that history — it assembled the replay AND the
    # provider accepted the prompt — or a reset explicitly suppressed it. A slash
    # command bypasses that assembly and must not settle the debt; a turn that
    # assembled but failed before the provider accepted (a pre-output 5xx) leaves
    # it armed for the re-queue. A provider switch arms ``provider_switch_replay``
    # instead and never reaches this; keeping them separate leaves the replay
    # lease's SID-preservation role (``close_all``) untouched.
    first_turn_history_owed: bool = False
    # Set of msg_ts values cancelled (message deleted while processing)
    cancelled: set[str] = field(default_factory=set)
    # Set after context compaction drops the session-start skill index.
    # Consumed one-shot by the next prompt builder to re-inject the skills
    # index so the model can still discover skills post-compaction.
    needs_context_reinjection: bool = False
    # Set when this session's cold start consumed a replay suppression, so it
    # started with NO conversation. Cleared by the compaction coordinator once
    # the first confirmed reading is recorded below. Lives on the session so it
    # dies with it: a successor that never reports leaves nothing behind.
    floor_pending: bool = False
    # First confirmed context reading of a session that started with no
    # conversation: what a fresh session on this key reads before anyone has
    # said anything. ``None`` until measured, and never for a session whose
    # first turn replayed history. Read by the compaction coordinator to decide
    # whether a reset could free anything at all.
    floor_pct: float | None = None

    def adopt_provider(self, provider: LLMProvider) -> None:
        """Swap in a freshly-spawned *provider*, resetting conversation state.

        Recycling in place — rather than registering a new ``_Session`` — is what
        lets a caller already holding this session (or blocked on its semaphore)
        pick up the replacement instead of a torn-down provider: both the
        semaphore and the object identity the registry is keyed on survive.
        Everything reset below describes the OLD transcript, so carrying it onto
        a fresh provider would misreport its size or replay a preamble the new
        conversation never lost. ``agent`` and ``approval_policy`` describe the
        session's role, not its transcript, so they are kept.
        """
        self.provider = provider
        self.loaded_capabilities = None
        self.provider_switch_replay = False
        # The replacement provider is a fresh native session, not a resumed
        # one — a stale armed observation would make the next first turn skip
        # history injection it actually needs. Only the resume half of the
        # observation is stale: an armed fresh observation, or nothing armed,
        # describes the replacement just as well and carries over unchanged.
        if self.first_turn is FirstTurnState.RESUMED:
            self.first_turn = FirstTurnState.FRESH
        self.prompt_count = 0
        self.consecutive_failures = 0
        self.prev_turn_cancelled = False
        self.needs_context_reinjection = False
        # A recycled provider replays history on its first turn, so its first
        # reading is not a floor; the measurement belongs to the old provider.
        self.floor_pending = False
        self.floor_pct = None
        self.created_at = time.time()
        self.last_used = time.monotonic()


def unlink_queued_temp_paths(kwargs: dict) -> None:
    """Unlink the temp files a queue entry tracks in ``image_temp_paths``.

    Queued Slack messages defer temp-image cleanup to whichever code path
    consumes the entry, so the queued turn's text can still resolve its image
    paths at dispatch time. Every path that consumes an entry — dispatch, or
    any discard (cancel, queue clear, cancelled-skip on dequeue) — must unlink
    here, or the files sit on disk until external cleanup. Already-missing
    files are ignored: a discard can benignly follow a dispatch that already
    cleaned up.
    """
    for p in kwargs.get("image_temp_paths") or []:
        try:
            os.unlink(p)
        except OSError:
            pass


def _unlink_session_queue(session: "_Session") -> None:
    """Unlink temp files for every entry still queued on a popped session.

    Every teardown path that pops a whole ``_Session`` out of ``_sessions``
    (stale-provider eviction, ``reset``, RSS recycle, ``remove``,
    ``remove_if_unclaimed``, ``destroy``, ``discard_conversation``,
    ``drain_all_providers``) discards ``session.queue`` along with it.
    Anything still sitting there never reaches ``_dispatch_queued``'s own
    cleanup — that only runs for an entry that actually gets dispatched —
    so this is the one place responsible for unlinking the images behind a
    whole-session teardown, the same way ``cancel_queued``/``clear_queue``/
    ``dequeue``'s cancelled-skip already do for a live session's own
    piecemeal discards.
    """
    for _, _, kwargs in session.queue:
        unlink_queued_temp_paths(kwargs)


# Named by the path callers import them from: a class renders as
# ``module.qualname`` in reprs, pickles and tracebacks, so this module's name
# must not leak into them.
FirstTurnState.__module__ = "kiro_crew.session"
_Session.__module__ = "kiro_crew.session"
