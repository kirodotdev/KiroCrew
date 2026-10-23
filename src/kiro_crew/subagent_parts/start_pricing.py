"""What a start costs in memory, and the reserve the admission gate holds for starts
already admitted: shared and dedicated pricing, the learned settled size, and the one
off-loop host reading every waiting start shares.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import time

    from kiro_crew.subagent import (
        _HOST_READ_OFF_LOOP_SECS,
        _RSS_SAMPLES_TO_SETTLE,
        _SETTLE_AFTER_SECS,
        DEFAULT_SPAWN_MIN_MEMORY_GB,
        DEFAULT_SUBAGENT_COST_GB,
        MEMORY_CAUSE_READ_UNANSWERED,
        Any,
        Iterable,
        KiroCrewConfig,
        Mapping,
        SubagentInfo,
        asyncio,
        check_memory_available,
        learned_settled_for,
        math,
        pop_memory_check_cause,
    )


# A start that takes a session on its parent's running runtime skips the process
# a dedicated start launches -- the kiro-cli launcher and chat process and their
# first-session warm-up -- but NOT the agent's MCP servers: kiro-cli starts a
# fresh copy of every declared server for each session, shared or not. Measured
# on kiro-cli 2.26.1, the skipped part is ~0.35 GiB (subagent.md, *Memory
# guard*), so a shared start is priced at the dedicated projection less that,
# never below ``_SHARED_START_MIN_GB``: strictly above 0, so no admitted start
# is priced as free.
_SHARED_START_SAVING_GB = 0.35


_SHARED_START_MIN_GB = 0.05


# What a dedicated start of a bucket with no learned settled figure yet is
# reserved at: a ``kiro-cli acp`` process (2.26.1) driven through Kiro Crew's own
# initialize / session/new shape with the default agent's MCP roster measured
# 0.80 GiB of process-tree USS after its first session, ~0.96 GiB with the
# gateway's own per-session MCP stubs at their real size; rounded up. A fresh
# install is exactly the low-memory host the floor protects, so its first
# starts must not be priced at a guess (subagent.md, *Memory guard*).
_UNLEARNED_DEDICATED_START_GB = 1.0


# The most a learned settled figure may raise a dedicated start's price. A bucket
# priced out of admission runs no dedicated starts and so never relearns, so a
# few mis-measured readings must not lock it out for good: this bounds the bar
# such a bucket meets at the floor plus 2 GiB. A heavy MCP roster measured
# ~1.5 GiB, under it.
_SETTLED_PROJECTION_CEILING_GB = 2.0


def _shared_start_price_gb(dedicated_gb: float) -> float:
    """Price of a start that will share its parent's runtime, given its bucket's
    dedicated projection: that projection less the process it does not launch."""
    return max(_SHARED_START_MIN_GB, dedicated_gb - _SHARED_START_SAVING_GB)


def _selection_kind(info: SubagentInfo) -> str:
    """``"template"`` or ``"member"``: how *info*'s run selects what it executes.

    An explicit ``agent`` is always a template selection (a crew spawn naming
    one keeps its member identity in ``execution_context`` instead); otherwise
    the admitted execution context decides. Raises when neither exists: a run
    with no admitted execution never reaches a start (``_run_inner`` refuses it
    first), and the admission gate reads an unknown answer as dedicated.
    """
    if info.agent:
        return "template"
    if info.execution_context is None:
        raise ValueError(f"subagent {info.id} has no admitted execution context")
    return info.execution_context.selection_kind


class _SharingPlan(NamedTuple):
    """How one run will start: the ONE decision the run and admission both read.

    ``shared`` is True only when the run takes the shared-runtime arm; the
    other fields are the run's effective model / effort pins, which force the
    dedicated arm when set and which the run hands its provider.
    """

    eff_model: str
    eff_effort: str
    shared: bool


def _dedicated_start_price_gb(
    cost_gb: float, settled_gb: Mapping[str, float] | None, bucket: str
) -> float:
    """What a dedicated start of *bucket* is reserved at.

    ``max(cost, learned settled)`` once the bucket has a learned settled figure
    (``settled_gb`` via :func:`kiro_crew.subagent_cost.read_learned_costs_checked`, three
    readings at least), else ``max(cost, _UNLEARNED_DEDICATED_START_GB)``.
    ``cost_gb`` (``agent.subagent_cost_gb``) is the floor of the price, and a
    learned figure never raises it past ``_SETTLED_PROJECTION_CEILING_GB``. The
    settled figure is what such a runtime holds once it is up -- kiro-cli plus
    its MCP servers, before its work grows the tree -- so a burst of dedicated
    starts is reserved at what each will actually settle at, not at a start
    price it outgrows unreserved. Never the whole-run peak.
    """
    learned = learned_settled_for(settled_gb, bucket)
    if learned is None or not math.isfinite(learned):
        projected = _UNLEARNED_DEDICATED_START_GB
    else:
        projected = min(learned, _SETTLED_PROJECTION_CEILING_GB)
    return max(max(0.0, cost_gb), projected)


def _spawn_memory_floor_and_cost(agent_cfg: Any = None) -> tuple[float, float]:
    """``(spawn_min_memory_gb, max(0, subagent_cost_gb))``, never raising.

    *agent_cfg* is an ``AgentConfig`` the caller already loaded; without one it
    is loaded here. Unreadable values fall back to the shipped defaults, so the
    gate and the dedicated top-up can never price from different fallbacks.
    """
    try:
        if agent_cfg is None:
            agent_cfg = KiroCrewConfig.load().agent
        return float(agent_cfg.spawn_min_memory_gb), max(0.0, float(agent_cfg.subagent_cost_gb))
    except Exception:
        return DEFAULT_SPAWN_MIN_MEMORY_GB, DEFAULT_SUBAGENT_COST_GB


def _host_memory_reading(min_gb: float) -> tuple[float, str]:
    """The floor's host reading and its cause, on whichever thread calls it.

    The cause is thread-local (:func:`pop_memory_check_cause`), so it is read on
    the same thread as the reading it describes and travels back with it.
    """
    pop_memory_check_cause()  # drop a cause left by any earlier reading
    _ok, avail = check_memory_available(min_gb=min_gb)
    return avail, pop_memory_check_cause()


async def _host_memory_reading_off_loop(min_gb: float) -> tuple[float, str]:
    """The floor's host reading (GiB, -1 when unmeasurable) and its cause, off the loop.

    For the spawn gate's second half (``MemoryReadPoint``) and the dedicated
    top-up's poll: the Linux reader walks cgroup files, so it never runs on the
    loop, not even as a fallback. A worker that does not answer within
    ``_HOST_READ_OFF_LOOP_SECS``, or a pool that cannot start a thread, comes
    back as :data:`MEMORY_CAUSE_READ_UNANSWERED`: both callers treat that as a
    start that does not fit yet, so it keeps waiting (the gate re-checks after
    the admit wait, the top-up at its next poll). It is never taken as
    "unmeasurable" (that fails open) and never read again on the loop (that is
    the stall this split exists to avoid).

    Single-flight: one read is in flight per process, whatever bar each caller
    holds. The reading and its cause do not depend on the bar (the reader's own
    verdict against *min_gb* is dropped), so a caller that arrives while a read
    is in flight on this loop awaits that read and compares its figure against
    its own bar. *min_gb* is the bar of the caller that started the read, passed
    through to the reader unchanged. The timeout ends a caller's wait, never the
    worker, so without this a reader that hangs would take one more executor
    thread at every re-check, and one more for every distinct bar (the bar
    differs per agent bucket and moves as warming rows settle), until the pool
    every ``to_thread`` caller shares ran dry.
    """
    global _host_read_in_flight
    loop = asyncio.get_running_loop()
    pending = _host_read_in_flight
    if pending is None or pending.done() or pending.get_loop() is not loop:
        pending = loop.create_task(asyncio.to_thread(_host_memory_reading, min_gb))
        _host_read_in_flight = pending

        def _forget(done: "asyncio.Future[tuple[float, str]]") -> None:
            global _host_read_in_flight
            if _host_read_in_flight is done:
                _host_read_in_flight = None
            if not done.cancelled():
                # Retrieve it, so a read that raised after every waiter timed
                # out logs no "exception was never retrieved" warning.
                done.exception()

        pending.add_done_callback(_forget)
    try:
        return await asyncio.wait_for(asyncio.shield(pending), timeout=_HOST_READ_OFF_LOOP_SECS)
    except (asyncio.TimeoutError, RuntimeError):
        return -1.0, MEMORY_CAUSE_READ_UNANSWERED


#: The one host reading every caller is waiting on (:func:`_host_memory_reading_off_loop`).
#: Process-wide, as the executor whose threads it rations is; it is cleared when
#: its read finishes, and one left by a loop that has gone is replaced.
_host_read_in_flight: "asyncio.Future[tuple[float, str]] | None" = None


def _row_settled(info: SubagentInfo, now: float) -> bool:
    """Whether *info*'s memory is already inside the free-memory reading.

    Two sweeps measured it, or (where nothing can measure it) its current
    process's own session answered ``_SETTLE_AFTER_SECS`` ago, on the monotonic
    clock so a laptop's sleep does not settle every row at once.
    """
    if info._rss_samples >= _RSS_SAMPLES_TO_SETTLE:
        return True
    answered = info._first_stream_mono
    return (
        answered is not None
        and info._first_stream_generation == info._rss_generation
        and now - answered >= _SETTLE_AFTER_SECS
    )


def _parked_at_spawn_approval(info: SubagentInfo) -> bool:
    """Whether *info* is parked on the pre-execution SPAWN approval: it has no process.

    ``_awaiting_approval`` alone does not say so: ``run.py`` sets it for TOOL
    prompts inside a running run as well, and ``_exec_started`` (stamped once,
    when execution begins) is what tells the two apart. The manager package's
    one copy; ``dashboard/handlers/messaging.py`` keeps its own, for the reason
    its ``_awaiting_spawn_approval`` gives.
    """
    return (
        getattr(info, "_awaiting_approval", False) is True
        and getattr(info, "_exec_started", None) is None
    )


def _owns_dedicated_runtime(
    agents: Iterable[SubagentInfo], *, claim_prices: Mapping[str, tuple[float, bool]]
) -> bool:
    """Whether a dedicated subagent runtime of this gateway is running or warming.

    The runtimes the macOS kernel memory-pressure hold waits on; which rows and
    claims count, and why shared-priced ones do not, is subagent.md's (*macOS:
    the kernel memory-pressure hold*). *claim_prices* is ``_claim_prices``.
    """
    if any(not priced_shared for _price, priced_shared in claim_prices.values()):
        return True
    return any(_holds_dedicated_runtime(info) for info in agents)


def _holds_dedicated_runtime(info: SubagentInfo) -> bool:
    """Whether *info* is a live row with a dedicated process of its own, running or warming.

    Admitted at the dedicated price and not confirmed shared, and past the
    points where a row has no process yet (parked at the spawn approval, or
    approved and waiting for the pump to release it). The one row predicate
    behind the macOS hold (:func:`_owns_dedicated_runtime`) and the per-lane
    memory share, which counts a lane's dedicated children with it.
    """
    return (
        not info.done
        and not info.queued
        and not info._session_sharing
        and not info._start_priced_shared
        and not _parked_at_spawn_approval(info)
        and not (info._start_release is not None and info._exec_started is None)
    )


def _startup_memory_reserve_gb(
    agents: list[SubagentInfo],
    *,
    running_count: int,
    cost_gb: float,
    next_start_gb: float | None = None,
    settled_gb: Mapping[str, float] | None = None,
    claim_prices: Iterable[float] = (),
    now: float | None = None,
) -> float:
    """Memory promised to starts but not observed in RSS yet.

    Three terms: the next start (*next_start_gb*, default ``cost_gb``), claims
    awaiting registration (each at the price its admission checked, from
    *claim_prices*, else ``cost_gb``), and every live row that has not settled
    yet. Queued and terminal rows promise nothing; a yielded parent still owns
    its process.

    A row owes its admitted price (``_start_price_gb``: the shared price
    (:func:`_shared_start_price_gb`) for a start predicted to share, the
    dedicated projection otherwise) in full until it settles
    (:func:`_row_settled`); a settled row owes nothing, since its memory is
    already inside the free-memory reading. No credit is taken for the RSS a
    warming row already shows: the sweep reads summed VmRSS, which counts a
    tree's shared pages once per process and so runs ~1.5x the PSS the prices
    are in, and a shared row's reading is a share of a runtime others use. A
    dedicated row with no admitted price owes the dedicated projection for its
    bucket (:func:`_dedicated_start_price_gb`); a shared one with none, nothing.

    The projection is never the whole-run peak or a whole-tree p90: a run's
    peak RSS is its whole process subtree -- test suites, builds and MCP
    servers it launched included -- so pricing a start at it held ordinary
    spawns at 10 GB+ on a laptop while the runtime itself needs a fraction.
    """
    clock = time.monotonic() if now is None else now
    live = [info for info in agents if not info.done and not info.queued]
    cost = max(0.0, cost_gb)
    unregistered = max(0, running_count - sum(not info._slot_released for info in live))
    claimed = [max(0.0, price) for price in claim_prices][:unregistered]
    gaps = 0.0
    for info in live:
        if _row_settled(info, clock):
            continue
        price = info._start_price_gb
        if price is None and not info._session_sharing:
            price = _dedicated_start_price_gb(
                cost,
                settled_gb,
                _cost_bucket(
                    info.agent, info.execution_context, remote=bool(info._remote_workspace)
                ),
            )
        gaps += max(0.0, price or 0.0)
    next_start = cost if next_start_gb is None else max(0.0, next_start_gb)
    return next_start + sum(claimed) + cost * (unregistered - len(claimed)) + gaps


#: Suffix on the cost bucket of a run whose workspace is remote (see _cost_bucket).
REMOTE_COST_BUCKET_SUFFIX = "@remote"


def _cost_bucket(agent: str, execution: Any, *, remote: bool = False) -> str:
    """The cost-store key one run's samples are written under.

    The explicit ``agent`` when the spawn named one, else the template the run
    actually executes (``execution.template_id`` -- an agent-less spawn inherits
    its parent's), so an inherited heavy template builds its OWN bucket instead
    of mixing into the default one. ONE function for the write
    (``_record_cost``) and the reads that price a start against it (the gate,
    the reserve, the dedicated top-up), so the two sides cannot key apart. Empty
    when neither is known; the store normalizes that to its default agent.

    A run in a remote session environment (*remote*) gets its own
    ``<bucket>@remote`` key: its builds and tests run in the remote workspace,
    so what it weighs here is its engine alone, and mixing those samples with
    local runs would misprice both.
    """
    base = agent or str(getattr(execution, "template_id", "") or "")
    if remote:
        return (base or "kirocrew") + REMOTE_COST_BUCKET_SUFFIX
    return base


def _parent_runs_remotely(parent_session_key: str | None) -> bool:
    """True while the parent session's runtime works in a remote workspace.

    In-memory and lock-free (set at each runtime start), so the spawn guard can
    ask it on the event loop. A child of such a session inherits that workspace
    (``inherit_environment_for_subagent``), so its file and shell work runs
    there, not on this machine.
    """
    if not parent_session_key:
        return False
    from kiro_crew.session_environment import remote_workspace_for

    return remote_workspace_for(parent_session_key) is not None
