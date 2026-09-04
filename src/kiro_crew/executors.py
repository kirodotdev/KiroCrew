"""Shared thread-pool executors for blocking maintenance work.

The asyncio event loop's *default* executor is also used by the loop itself
for ``getaddrinfo`` (DNS) and by every ``run_in_executor(None, ...)`` caller.
When long-running maintenance work (orphan-PID sweeps, cron subprocesses,
agent-overlay rewrites) piles onto that default pool it can saturate it and
starve the loop's own DNS resolution -- which is exactly the failure mode that
turns a brief network blip into a multi-second event-loop stall.

This module provides *separate*, bounded pools for that blocking work and also
names the loop's default executor's threads ``mc-default`` (Python's default
names them anonymously), so profilers like py-spy can attribute blocking work
to this gateway.

Three pools, deliberately split:

* :func:`maintenance_executor` -- the fast periodic sweeps (orphaned MCP/PID
  cleanup) and agent-overlay rewrites.  These are short and MUST stay
  responsive, because the orphan sweeps are what reap the leaked kiro-cli/MCP
  children whose mass death is the documented root cause of the loop wedge.
* :func:`subprocess_executor` -- per-process teardown work that can block on a
  *wedged kernel resource*: ``os.close`` on a PTY master fd whose far-end shell
  is in uninterruptible sleep, and the ``ps``/``pgrep`` spawns + ``os.kill``
  sweep in :mod:`kiro_crew.acp.client`.  These can hang indefinitely, so they
  get their OWN pool: a storm of wedged teardowns can occupy every worker here
  WITHOUT starving the :func:`maintenance_executor` orphan sweep that is the
  recovery action for the wedge (the bug the wedge fix would otherwise create
  by coupling the recovery mechanism to the same pool as the work that wedges).
* :func:`cron_executor` -- user cron command/script execution, which can run
  for minutes (``job.timeout`` defaults to 300s) and, because the scheduler
  dispatches every due job concurrently, can have several jobs in flight at
  once.  Keeping cron on its OWN pool means a burst of long-running cron jobs
  can never occupy all the maintenance threads and starve the sweeps.
* :func:`discovery_executor` -- browser-triggerable, read-only filesystem
  discovery for dashboard list endpoints (``GET /api/skills``,
  ``/api/agents/installed``, ``/api/prompts``): ``os.walk`` of skill/agent/SOP
  trees, per-file frontmatter reads, edition skill-root globs.  A large catalog
  one such scan take seconds, and a ``run_in_executor`` future cannot be
  cancelled once started, so N concurrent dashboard tabs would each pin a
  worker for the full scan.  This gets its OWN pool so those user-triggerable
  scans can never occupy the :func:`maintenance_executor` workers the orphan
  sweeps need to recover from a wedge.
* :func:`image_executor` -- Pillow decode/resize/encode for the MCP gateway's
  tool-result image budget (:mod:`kiro_crew.mcp_gateway.image_budget`).  Paced
  by whatever a brokered MCP server returns, and a single oversized raster
  costs a full decode plus up to seven resize+encode passes -- seconds of CPU.
  Same rationale as :func:`discovery_executor`: externally-paced, seconds-long
  work gets its OWN small pool so a burst of screenshots queues among ITSELF
  and can never occupy the :func:`maintenance_executor` workers the orphan
  sweeps need to recover from a wedge.
* :func:`stt_executor` -- in-process speech-to-text inference
  (:mod:`kiro_crew.stt.engine`, which loads the model here and decodes on it).
  A warm decode is tens of milliseconds, but a model load is seconds and the
  first ever load also compiles a GPU pipeline.  It cannot share
  :func:`subprocess_executor`: a ``run_in_executor`` future cannot be
  cancelled, so a wedged model load would hold one of the eight PTY-teardown
  workers indefinitely -- and those exist precisely so a teardown storm has
  somewhere to go.  A caller that gives up on a timeout does NOT free the
  thread, which is the whole reason this work needs a pool it can only starve
  for itself.

Long-term direction: this blocking work should move into a dedicated
supervised process (the VS Code extension-host model), so a wedge there cannot
touch the dashboard event loop at all.  These bounded pools are the in-process
containment until that process split lands.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

__all__ = [
    "CronQueueTimeout",
    "configure_default_executor",
    "maintenance_executor",
    "subprocess_executor",
    "cron_executor",
    "discovery_executor",
    "embed_executor",
    "image_executor",
    "stt_executor",
    "path_resolve_executor",
    "governance_executor",
    "cron_gate_executor",
    "CronGateTimeout",
    "CronGateWorkTimeout",
    "run_in_cron_pool",
    "run_in_cron_gate_pool",
    "cron_gate_budget",
    "run_in_embed_pool",
    "shutdown_maintenance_executor",
]

# ── Default executor naming ──
# asyncio.to_thread and run_in_executor(None, ...) route onto the loop's default
# executor.  The loop itself uses that same pool for getaddrinfo (DNS).  Python
# caps it at min(32, cpu_count + 4) and names threads anonymously.  This module
# keeps that cap but names threads ``mc-default`` so profilers like py-spy can
# attribute blocking work to this gateway.

# Bounded so a burst of maintenance work cannot spawn unbounded threads.  Four
# is enough for the periodic sweeps + agent-overlay rewrites while keeping them
# isolated from the default executor's own bounded pool.
_MAX_MAINT_WORKERS = 4

# Subprocess/PTY teardown can block on a wedged kernel resource and a single
# mass shutdown (close_all over the warm pool + active sessions) enqueues
# several tasks per client.  Size this above the maintenance pool so a burst of
# wedged teardowns queues among ITSELF rather than head-of-line-blocking the
# orphan sweep on the separate maintenance pool.  Still bounded: a started
# run_in_executor future cannot be cancelled, so this caps how many wedged
# closes/spawns we hold threads for; excess work queues here.
_MAX_SUBPROCESS_WORKERS = 8

# Cron jobs can be long (default 300s timeout) and several can be due in the
# same tick (the scheduler fires each as an independent task).  Give them their
# own bounded pool so they queue among THEMSELVES rather than evicting the fast
# maintenance sweeps.  A run_in_executor future cannot be cancelled once its
# thread starts, so this also caps how many concurrent cron subprocesses we hold
# threads for; excess jobs queue here instead of starving anything else.
_MAX_CRON_WORKERS = 4

# How long a cron call may sit in the pool's queue before we give up and report
# it as never-started.  Deliberately much larger than a typical job.timeout:
# waiting behind a busy worker and then running is CORRECT, so this exists only
# to bound the pathological case (a wedged job holding a worker indefinitely)
# so an entry cannot sit un-failed forever.  Reaching it means the pool was
# starved for a quarter of an hour, which is a fleet problem, not a job defect
# -- and the distinct CronQueueTimeout text is what makes that legible.
_CRON_QUEUE_WAIT_SECS = 900

# Dashboard discovery scans (skills/agents/SOP listing) are read-only but can
# take seconds on a large catalog, and each is browser-triggerable so several
# can be in flight at once (multiple tabs / pollers).  A started
# run_in_executor future cannot be cancelled, so bound this at a handful of
# workers: concurrent scans queue among THEMSELVES here rather than occupying
# the maintenance pool's orphan-sweep workers.
_MAX_DISCOVERY_WORKERS = 4

# Ollama embed/probe offloads (consolidation lesson writes, memory import,
# context-preview, and every build_message call — its episodic recall embeds
# the query) block on network I/O for up to embedding_timeout_secs per call —
# and against a HUNG endpoint every call eats the full timeout, since failures
# are deliberately not cached.  Give them their own bounded pool so a wedged
# Ollama parks mc-embed threads and queues further embed work behind ITSELF,
# instead of exhausting asyncio's default executor (which the loop shares for
# DNS resolution and every other asyncio.to_thread user).  Sized above the
# other pools because build_message runs on every new session across all
# surfaces (dashboard + Slack + cron + heartbeat + subagent can land
# concurrently after a restart); with a healthy endpoint each call is fast,
# so the cap only bites — deliberately — when Ollama is wedged.
_MAX_EMBED_WORKERS = 8

# Governance checks for EXTERNALLY-triggered surfaces: the per-inbound-message
# channels gate (every Slack/Discord/Telegram/Webex/WeCom message + approval
# callback)
# and the dashboard governance GETs.  These walk the ProfileStore (filesystem)
# and may do a synchronous SEL write, and their rate is driven by REMOTE senders
# — a burst of inbound messages could otherwise occupy every mc-maint worker and
# starve the orphan sweeps.  Own bounded pool so externally-paced governance I/O
# queues among ITSELF, never evicting the maintenance sweeps.
_MAX_GOVERNANCE_WORKERS = 4

# Cron FIRE-TIME governance gates get their OWN pool, deliberately not the
# governance one above.  The distinction is who paces the queue: that pool is
# driven by REMOTE senders (one submit per inbound message across five
# transports, plus the dashboard GETs), so a burst of inbound traffic puts an
# unbounded FIFO backlog ahead of a cron gate.  A cron gate is awaited INSIDE
# the job's already-armed wake deadline, so a backlog it did not cause is
# charged to the job's own execution budget -- and a message job carries no
# _pool_queue_allowance to cover it, so the wake deadline can expire before the
# job ever dispatches.  Here the only thing a cron gate queues behind is
# another cron gate, which is self-paced by the schedule.
_MAX_CRON_GATE_WORKERS = 4

# Ceiling on how long a fire-time gate may spend QUEUED before we call it
# starvation.  Unlike _CRON_QUEUE_WAIT_SECS this is small and is additionally
# capped against the job's own wake budget at the call site: for the EXECUTION
# pool, waiting past the budget and then running is the correct outcome, but a
# gate that outlives the wake deadline is never useful -- the deadline kills the
# run regardless, and the caller then cannot tell starvation from an overrun.
# So the gate's bound must fire FIRST, which is what makes the run legible as
# "never started" and keeps a one-shot from being consumed by a run that never
# dispatched.
_CRON_GATE_WAIT_SECS = 30

# The gate's bound must land STRICTLY below the wake deadline, and a bare floor
# cannot do that: max(1.0, ...) returns 1.0 for a 1s wake budget, so the two
# coincide and the OUTER deadline wins the race -- the one state in which
# starvation is indistinguishable from an overrun, which is how an undispatched
# one-shot gets consumed.  Removing the floor instead is the opposite error: the
# gate would get 0.25s at a 2s budget and could time out on a HEALTHY pool,
# converting a job that would have run into a never-started one.  Both failures
# are silent and point opposite ways, so keep the floor for budgets that can
# afford it and clamp it against a fraction of the budget, which is what makes
# "strictly below" hold for every budget rather than for typical ones.
_CRON_GATE_MIN_SECS = 1.0
_CRON_GATE_HEADROOM = 0.75

# How the gate's single budget is DIVIDED between waiting for a worker and
# running.  It has to be divided rather than handed to both phases, because
# :func:`run_in_cron_pool` treats its two bounds as independent budgets --
# *timeout* is charged to EXECUTION alone -- so passing one value to both spends
# it twice and lets the gate run for 2x it.  The budget is already
# :func:`cron_gate_budget`, sized to land strictly below the wake deadline, so a
# gate that outran it put the DEADLINE first and neither phase bound was ever
# reached: nothing raised, the caller's retention handler never ran, and a
# one-shot was consumed by a run that never dispatched.  The two shares
# therefore sum to exactly 1.0 and no more.
#
# Weighted toward EXECUTION deliberately.  Queueing is the cheap phase -- gates
# queue only behind other gates, which the schedule paces, so a healthy claim
# costs microseconds and the queue bound binds only under starvation.  Execution
# is the useful work, and starving it is the OPPOSITE failure with an opposite
# symptom: a claimed gate cut off near zero raises :class:`CronGateTimeout`, a
# :class:`CronQueueTimeout` subclass, so the one-shot is RETAINED and never
# fires.  A fixed share rather than "whatever the queue left over" is what makes
# execution's slice a floor instead of a race.
_CRON_GATE_QUEUE_SHARE = 0.25

# Pillow work for the gateway's tool-result image budget is CPU-bound (a
# decode plus up to seven LANCZOS resize+encode passes per oversized raster,
# seconds each) and paced by whatever a brokered MCP server returns.  Two
# workers bound the CPU it can burn while letting one slow raster overlap one
# fast one; excess images queue among THEMSELVES here, never evicting the
# maintenance sweeps or head-of-line blocking any other pool's work.
_MAX_IMAGE_WORKERS = 2

# In-process STT inference is the longest-running work in this module: minutes of
# CPU on a meeting-length recording, and the first call for a model size can block
# on a multi-GB weight download inside the library's constructor.  TWO workers,
# deliberately small for a reason the other pools do not share -- each in-flight
# call holds a fully quantised model in RAM (up to ~GBs for large-v3), so the
# worker count is a MEMORY ceiling, not just a CPU one.  Two lets a queued
# recording start while one finishes; more would let concurrent dictations OOM a
# small host.  Sizing it here rather than reusing the 8-worker subprocess pool is
# the point: a wedged model load cannot be cancelled, so it must only ever be able
# to starve other STT work.
_MAX_STT_WORKERS = 2

# Symlink resolution for the sensitive-path gates (``security._candidate_forms``).
# ``os.path.realpath`` walks every component with ``lstat``, and a component that
# lands on a stalled automount (macOS ``/home`` autofs backed by an unreachable
# directory server, a dead NFS/SSHFS mount, a disconnected Windows mapped drive)
# blocks in the kernel for as long as the mount does -- effectively unbounded.
# The gate runs SYNCHRONOUSLY inside ``on_tool_call`` on the event loop, so that
# block was a loop wedge -> watchdog dump-then-exit (ten identical crash dumps
# on a corp macOS around a VPN transition, the path tokens being the remote
# side of an ``ssh host 'cd /home/...'`` command that never existed locally).
# The caller bounds its wait and REFUSES the path on timeout (fail-closed --
# a lexical-only fallback would let a symlink into a credential store ride a
# stall; only a resolution that FAILS with OSError keeps the lexical forms);
# the wait does NOT free the worker, so this is its OWN tiny pool: a wedged
# resolution can only ever starve other path resolution, never the sweeps or
# the default executor's DNS.  Two workers is deliberate -- healthy resolution is
# microseconds, so the cap only bites when the filesystem is wedged, which is
# exactly when queueing behind a wedged sibling is the correct outcome.
_MAX_PATH_RESOLVE_WORKERS = 2

_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_subprocess_pool: ThreadPoolExecutor | None = None
_cron_pool: ThreadPoolExecutor | None = None
_discovery_pool: ThreadPoolExecutor | None = None
_embed_pool: ThreadPoolExecutor | None = None
_image_pool: ThreadPoolExecutor | None = None
_stt_pool: ThreadPoolExecutor | None = None
_governance_pool: ThreadPoolExecutor | None = None
_cron_gate_pool: ThreadPoolExecutor | None = None
_path_resolve_pool: ThreadPoolExecutor | None = None


def configure_default_executor() -> None:
    """Name the event loop's default executor threads.

    Call once per event loop at startup, BEFORE any ``asyncio.to_thread`` or
    ``run_in_executor(None, ...)`` runs.  Creates a fresh pool for the current
    loop; the loop owns the pool and shuts it down when it closes.

    Effects:

    * Keeps Python's default cap (``min(32, cpu_count + 4)``).
    * Names threads ``mc-default`` so they appear labeled in profilers like
      py-spy, instead of the anonymous ``ThreadPoolExecutor-N_M``.

    Does NOT make the documented "default executor free for the loop's own I/O"
    invariant literally true -- 1700+ call sites still route onto it.  That
    migration is tracked separately; this function names the pool until then.
    """
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(
        max_workers=None,  # keep Python's default: min(32, cpu_count + 4)
        thread_name_prefix="mc-default",
    )
    loop.set_default_executor(pool)


def maintenance_executor() -> ThreadPoolExecutor:
    """Return the process-wide maintenance thread pool, creating it on first use.

    Threads are named ``mc-maint`` for easy identification in watchdog stack
    dumps.  Distinct from asyncio's default executor so blocking maintenance
    work cannot starve the event loop's DNS resolution.  Reserved for the fast
    periodic sweeps + overlay rewrites -- cron uses :func:`cron_executor`.
    """
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(
                    max_workers=_MAX_MAINT_WORKERS,
                    thread_name_prefix="mc-maint",
                )
                atexit.register(shutdown_maintenance_executor)
    return _pool


def subprocess_executor() -> ThreadPoolExecutor:
    """Return the process-wide subprocess/PTY-teardown pool, creating it on first use.

    Threads are named ``mc-subproc``.  Separate from :func:`maintenance_executor`
    so a teardown call that blocks on a wedged kernel resource (PTY ``os.close``
    on an uninterruptible-sleep shell, a hung ``ps``/``pgrep`` spawn) cannot
    occupy the workers the orphan-reaping sweep needs to recover from the wedge.
    """
    global _subprocess_pool
    if _subprocess_pool is None:
        with _lock:
            if _subprocess_pool is None:
                _subprocess_pool = ThreadPoolExecutor(
                    max_workers=_MAX_SUBPROCESS_WORKERS,
                    thread_name_prefix="mc-subproc",
                )
                atexit.register(shutdown_maintenance_executor)
    return _subprocess_pool


def cron_executor() -> ThreadPoolExecutor:
    """Return the process-wide cron execution thread pool, creating it on first use.

    Threads are named ``mc-cron``.  Separate from :func:`maintenance_executor`
    so long-running, concurrent cron command/script jobs queue among themselves
    instead of occupying the maintenance threads the orphan-reaping sweeps need.
    """
    global _cron_pool
    if _cron_pool is None:
        with _lock:
            if _cron_pool is None:
                _cron_pool = ThreadPoolExecutor(
                    max_workers=_MAX_CRON_WORKERS,
                    thread_name_prefix="mc-cron",
                )
                atexit.register(shutdown_maintenance_executor)
    return _cron_pool


def discovery_executor() -> ThreadPoolExecutor:
    """Return the process-wide dashboard-discovery pool, creating it on first use.

    Threads are named ``mc-discovery``.  Separate from :func:`maintenance_executor`
    so browser-triggerable, seconds-long read-only filesystem scans (skills /
    agents / SOP listing for the dashboard) can never occupy the workers the
    orphan-reaping sweeps need to recover from an event-loop wedge.
    """
    global _discovery_pool
    if _discovery_pool is None:
        with _lock:
            if _discovery_pool is None:
                _discovery_pool = ThreadPoolExecutor(
                    max_workers=_MAX_DISCOVERY_WORKERS,
                    thread_name_prefix="mc-discovery",
                )
                atexit.register(shutdown_maintenance_executor)
    return _discovery_pool


def image_executor() -> ThreadPoolExecutor:
    """Return the process-wide Pillow image-budget pool, creating it on first use.

    Threads are named ``mc-image``.  Separate from :func:`maintenance_executor`
    so seconds-long, externally-paced Pillow decode/resize work on brokered MCP
    tool-result images can never occupy the workers the orphan-reaping sweeps
    need to recover from a wedge.
    """
    global _image_pool
    if _image_pool is None:
        with _lock:
            if _image_pool is None:
                _image_pool = ThreadPoolExecutor(
                    max_workers=_MAX_IMAGE_WORKERS,
                    thread_name_prefix="mc-image",
                )
                atexit.register(shutdown_maintenance_executor)
    return _image_pool


def stt_executor() -> ThreadPoolExecutor:
    """Return the process-wide STT inference pool, creating it on first use.

    Threads are named ``mc-stt``.  Separate from :func:`subprocess_executor` for the
    reason a started ``run_in_executor`` future cannot be cancelled: a wedged model
    load (or a first-run weight download inside the library constructor) holds its
    worker until the process exits, and on the PTY-teardown pool that would consume
    one of the eight workers whose whole purpose is absorbing a teardown storm.
    Here it can only starve other STT work, which is the containment we want.

    Callers bound their own wait (``stt.timeout_secs``); that releases the CALLER,
    never the thread — see :func:`kiro_crew.transcribe._transcribe_faster`.
    """
    global _stt_pool
    if _stt_pool is None:
        with _lock:
            if _stt_pool is None:
                _stt_pool = ThreadPoolExecutor(
                    max_workers=_MAX_STT_WORKERS,
                    thread_name_prefix="mc-stt",
                )
                atexit.register(shutdown_maintenance_executor)
    return _stt_pool


def path_resolve_executor() -> ThreadPoolExecutor:
    """Return the process-wide sensitive-path symlink-resolution pool, creating it on first use.

    Threads are named ``mc-pathres``.  Serves ``security._candidate_forms``, whose
    ``os.path.realpath`` / ``Path.resolve`` on an agent-supplied path token used
    to run inline on the event loop and could block in the kernel for as long as
    a stalled automount did.  The caller bounds its wait
    (``security._PATH_RESOLVE_TIMEOUT_SECS``) and refuses the path on timeout
    (fail-closed; only a resolution that FAILS keeps the lexical forms); the
    wait releases the CALLER, never the thread, which is why this
    work gets a pool it can only starve for itself -- on
    :func:`subprocess_executor` a wedged ``lstat`` would consume one of the PTY
    teardown workers, and on the default executor it would starve the loop's own
    DNS resolution.
    """
    global _path_resolve_pool
    if _path_resolve_pool is None:
        with _lock:
            if _path_resolve_pool is None:
                _path_resolve_pool = ThreadPoolExecutor(
                    max_workers=_MAX_PATH_RESOLVE_WORKERS,
                    thread_name_prefix="mc-pathres",
                )
                atexit.register(shutdown_maintenance_executor)
    return _path_resolve_pool


def embed_executor() -> ThreadPoolExecutor:
    """Return the process-wide Ollama embed/probe pool, creating it on first use.

    Threads are named ``mc-embed``.  Separate from asyncio's default executor
    so a hung embedding endpoint (every call eats the full
    ``embedding_timeout_secs``; failures are deliberately not cached) parks
    only these workers — embed work queues behind ITSELF instead of starving
    the loop's DNS resolution and every other ``asyncio.to_thread`` user.
    """
    global _embed_pool
    if _embed_pool is None:
        with _lock:
            if _embed_pool is None:
                _embed_pool = ThreadPoolExecutor(
                    max_workers=_MAX_EMBED_WORKERS,
                    thread_name_prefix="mc-embed",
                )
                atexit.register(shutdown_maintenance_executor)
    return _embed_pool


def governance_executor() -> ThreadPoolExecutor:
    """Return the process-wide governance-check pool, creating it on first use.

    Threads are named ``mc-gov``.  Separate from :func:`maintenance_executor` so
    the externally-paced per-inbound-message channels gate (and the dashboard
    governance GETs) — which walk the ProfileStore and may do a synchronous SEL
    write — can never occupy the workers the orphan-reaping sweeps need.  A remote
    burst of inbound messages queues among ITSELF here instead of starving the
    maintenance sweeps.
    """
    global _governance_pool
    if _governance_pool is None:
        with _lock:
            if _governance_pool is None:
                _governance_pool = ThreadPoolExecutor(
                    max_workers=_MAX_GOVERNANCE_WORKERS,
                    thread_name_prefix="mc-gov",
                )
                atexit.register(shutdown_maintenance_executor)
    return _governance_pool


def cron_gate_executor() -> ThreadPoolExecutor:
    """Return the process-wide cron fire-time gate pool, creating it on first use.

    Threads are named ``mc-crongate``.  Separate from :func:`governance_executor`
    because the two are paced by different things: that pool's rate is set by
    remote senders, this one's by the cron schedule.  A cron gate awaited inside
    an already-armed wake deadline must not queue behind an inbound-traffic
    burst, so it queues among other cron gates instead.  See
    :data:`_MAX_CRON_GATE_WORKERS`.
    """
    global _cron_gate_pool
    if _cron_gate_pool is None:
        with _lock:
            if _cron_gate_pool is None:
                _cron_gate_pool = ThreadPoolExecutor(
                    max_workers=_MAX_CRON_GATE_WORKERS,
                    thread_name_prefix="mc-crongate",
                )
                atexit.register(shutdown_maintenance_executor)
    return _cron_gate_pool


class CronQueueTimeout(asyncio.TimeoutError):
    """A cron job never got a worker slot before its queue budget ran out.

    Subclasses :class:`asyncio.TimeoutError` on purpose: a caller that has not
    been taught about the queue phase still lands in its existing timeout
    handling rather than raising something nothing catches.  Callers that DO
    distinguish the two must order ``except CronQueueTimeout`` first.
    """

    def __init__(self, waited: float) -> None:
        # Sub-second waits get decimals: `:.0f` floored a 0.3s wait to "queued
        # 0s", which reads as "did not wait at all" and blunts the very
        # diagnostic this exception exists to carry.
        shown = f"{waited:.2f}" if waited < 10 else f"{waited:.0f}"
        super().__init__(f"queued {shown}s, never ran")
        self.waited = waited


class CronGateTimeout(CronQueueTimeout):
    """A fire-time gate exhausted its own EXECUTION bound without a verdict.

    Subclasses :class:`CronQueueTimeout` deliberately.  The gate's two bounds
    mean the same thing to a caller -- no verdict was reached, so the run never
    got to its dispatch decision -- and the callers already translate
    ``CronQueueTimeout`` into the ``run_never_started`` retention marker.  Making
    this a subclass is what reaches that handler WITHOUT widening it to every
    :class:`asyncio.TimeoutError`, which would also swallow
    :class:`CronGateWorkTimeout` below and retain a job that did reach its
    decision.
    """

    def __init__(self, waited: float) -> None:
        shown = f"{waited:.2f}" if waited < 10 else f"{waited:.0f}"
        # Deliberately NOT the "queued ..., never ran" wording: this call DID get
        # a worker, so reporting it as queued would misdescribe the fault.
        Exception.__init__(self, f"ran {shown}s without a verdict")
        self.waited = waited


class CronGateWorkTimeout(asyncio.TimeoutError):
    """The gate's own work raised a timeout -- it reached its work and failed there.

    Still an :class:`asyncio.TimeoutError` so any caller that already handles
    timeouts is unaffected, but deliberately NOT a :class:`CronQueueTimeout`: the
    gate got a worker and ran, so this is a failed dispatch DECISION, not a run
    that never started.  Marking it never-started would retain a job whose gate
    did execute, which is the opposite error to the one
    :class:`CronGateTimeout` fixes.
    """


async def run_in_cron_pool(
    func: Callable[..., _T],
    /,
    *args: Any,
    timeout: float,
    queue_timeout: float | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> _T:
    """Run *func* on :func:`cron_executor`, charging *timeout* to EXECUTION only.

    ``loop.run_in_executor`` only SUBMITS work.  The cron pool is bounded at
    ``_MAX_CRON_WORKERS``, so when every worker is busy the call sits in the
    pool's queue having run no code at all -- yet ``asyncio.wait_for`` starts
    its stopwatch the moment it is awaited.  Timing the whole await therefore
    spends the job's own budget on queue wait and kills jobs that never ran a
    line, reporting them as if the job itself had overrun.

    So wait for a worker to actually pick the call up FIRST, untimed by
    *timeout*, and only then apply *timeout* to the execution.  A job delayed
    by transient contention now runs instead of dying.

    The two phases fail differently, which is the other half of the point: a
    pool starved for the whole *queue_timeout* raises
    :class:`CronQueueTimeout` (``queued Ns, never ran``) rather than
    masquerading as N separate jobs that each overran.

    A call a worker claims right AT the deadline is routed to the execution
    phase rather than reported as a queue timeout.  It really did get a worker,
    and a thread cannot be interrupted, so calling it "never ran" would both
    misreport it and free the caller to start an overlapping run beside the one
    still executing.

    *queue_timeout* defaults to :data:`_CRON_QUEUE_WAIT_SECS`.  It is
    deliberately NOT derived from *timeout*: a 30s job behind a wedged worker
    should wait well past 30s and then run, not be killed on its own clock for
    something it did not do.

    *executor* defaults to :func:`cron_executor`.  It exists so the fire-time
    gate can reuse this two-phase discipline on its own pool
    (:func:`run_in_cron_gate_pool`) rather than carry a second copy of it: only
    the CONCURRENT future's ``cancel()`` can tell queued from claimed, and that
    is the subtle part worth having in one place.
    """
    loop = asyncio.get_running_loop()
    if queue_timeout is None:
        queue_timeout = _CRON_QUEUE_WAIT_SECS
    started: asyncio.Future[None] = loop.create_future()

    def _mark_started() -> None:
        # May already be cancelled if the queue budget expired in the meantime.
        if not started.done():
            started.set_result(None)

    def _signal_then_run() -> _T:
        loop.call_soon_threadsafe(_mark_started)
        return func(*args)

    # submit() + wrap_future() is precisely what run_in_executor does
    # internally; spelled out here to keep a reference to the CONCURRENT
    # future.  Only its cancel() reports whether a worker had already claimed
    # the call: the asyncio wrapper's cancel() returns True either way, because
    # it cancels the wrapper locally while the worker thread runs on.
    call = (executor or cron_executor()).submit(_signal_then_run)
    fut = asyncio.wrap_future(call, loop=loop)
    queued_at = loop.time()
    deadline = queued_at + queue_timeout
    while not started.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            # shield: a wait_for timeout cancels whatever it was waiting on,
            # and this future has to survive to be waited on again.
            await asyncio.wait_for(asyncio.shield(started), timeout=remaining)
        except asyncio.TimeoutError:
            # A wake is NOT proof the deadline passed.  BaseEventLoop._run_once
            # dispatches a timer once `handle._when < time() + _clock_resolution`,
            # so it may fire up to one clock resolution EARLY -- 15.6ms on
            # Windows, ~0 on Linux.  Re-check the deadline instead of trusting
            # it, which is what makes `waited >= queue_timeout` hold by
            # construction rather than by tolerance.
            pass
        except asyncio.CancelledError:
            # The shield above absorbs a cancel so `started` survives to be
            # re-waited, which also means a caller cancelling this coroutine
            # would otherwise escape with the call still sitting in the pool
            # queue -- it would then run long after the caller gave up.  Cancel
            # the submitted call before re-raising; that is a no-op once a
            # worker has claimed it, so a job already in flight is never killed.
            call.cancel()
            raise
    if not started.done() and call.cancel():
        # Raise only when the cancel WON.  On the concurrent future that means
        # no worker had claimed the call, which is the one case where "never
        # ran" is true.  `started` cannot decide it alone: _mark_started is
        # delivered by call_soon_threadsafe, so it trails the worker's real
        # claim and still reads False for a call that is already executing.
        raise CronQueueTimeout(loop.time() - queued_at)
    # Cancel lost, so a worker claimed the call and it IS running.  A thread
    # cannot be interrupted, so it will run to completion whatever we report --
    # and reporting a queue timeout here would release the caller's overlap
    # guard while the command runs, letting the next fire duplicate its side
    # effects.  It got a worker, so it belongs in the execution phase, bounded
    # by `timeout` exactly as a call claimed a millisecond earlier would be.
    return await asyncio.wait_for(fut, timeout=timeout)


def cron_gate_budget(wake_budget: float) -> float:
    """Total seconds a fire-time gate may spend queued, given the job's wake budget.

    Capped against the wake budget on purpose, and this is the one place the
    codebase derives a queue bound from the caller's own clock -- the opposite of
    :func:`run_in_cron_pool`'s default, for a stated reason.  There, waiting past
    the budget and then running is the outcome you want.  Here the gate is
    awaited INSIDE the already-armed wake deadline, so a gate that outlives that
    deadline cannot help: the deadline kills the run either way, and the caller
    is then left unable to distinguish starvation from a genuine overrun -- which
    is exactly the state in which a ``delete_after_run`` job is consumed by a run
    that never dispatched.  Bounding below the wake budget makes the gate's own
    bound fire first, so the run is reported as never-started and retained.

    A quarter of the budget leaves the remaining three quarters for the dispatch
    the gate is a precondition for.

    The result is STRICTLY below *wake_budget* for every positive budget, which
    the previous ``max(1.0, ...)`` floor did not achieve: at a 1s budget it
    returned 1.0, so the gate and the wake deadline expired together and the
    outer one won.  The floor is kept for budgets that can afford it -- dropping
    it entirely would hand a 2s job a 0.5s gate and time out on a healthy pool --
    and is clamped by :data:`_CRON_GATE_HEADROOM` so the ordering holds anyway.
    """
    if wake_budget <= 0:
        return 0.0
    preferred = min(float(_CRON_GATE_WAIT_SECS), max(_CRON_GATE_MIN_SECS, wake_budget / 4.0))
    return min(preferred, wake_budget * _CRON_GATE_HEADROOM)


async def run_in_cron_gate_pool(func: Callable[..., _T], /, *args: Any, timeout: float) -> _T:
    """Run a cron fire-time gate on :func:`cron_gate_executor`, bounded both ways.

    Two changes from awaiting ``run_in_executor`` directly, which is what the
    gate sites used to do:

    * the gate no longer shares a pool with externally-paced inbound traffic, so
      an inbound burst cannot put a FIFO backlog ahead of it; and
    * the wait is BOUNDED, raising :class:`CronQueueTimeout` when the gate never
      got a worker, which is the signal the callers translate into a
      retention marker so an undispatched one-shot is not consumed.

    *timeout* is the gate's TOTAL, split across the two phases by
    :data:`_CRON_GATE_QUEUE_SHARE` rather than handed to each of them: for a
    policy check there is no equivalent of "wait a long time, then run" -- a
    verdict that arrives after the wake deadline is worthless either way -- and
    ``run_in_cron_pool`` charges *timeout* to EXECUTION alone, so giving it to
    both bounds spent the budget twice and let the gate outlive the very deadline
    it was sized to stay inside.  Abandoning a CLAIMED gate is safe in a way
    abandoning a claimed command is not: the gate dispatches no job work, so
    nothing duplicates.  The residual is audit noise only -- a gate thread that
    completes after we stopped waiting may still emit its SEL
    ``governance_decision`` for a run that then did not dispatch.

    Which exception surfaces matters, because the callers key the retention
    marker off it.  ``run_in_cron_pool`` raises :class:`CronQueueTimeout` for the
    queue phase but a PLAIN :class:`asyncio.TimeoutError` for the execution
    phase, so a gate that got a worker and then overran its bound bypassed the
    callers' ``except CronQueueTimeout`` entirely and its one-shot was consumed.
    The execution bound is therefore re-raised as :class:`CronGateTimeout`, a
    subclass, so it reaches that handler.  A timeout raised from INSIDE the
    gate's own work is wrapped as :class:`CronGateWorkTimeout` in the worker
    thread first, so it can never be mistaken for this bound -- that one is a
    failed dispatch decision, not a run that never started.
    """

    def _guard_gate_work(*args: Any) -> _T:
        try:
            return func(*args)
        except CronGateWorkTimeout:
            raise
        except asyncio.TimeoutError as exc:
            # Wrapped in the WORKER THREAD, before the bound below can see it:
            # asyncio.TimeoutError is builtins.TimeoutError on 3.11+, so a socket
            # or subprocess timeout inside a governance check is otherwise
            # type-identical to our own bound expiring.
            raise CronGateWorkTimeout(str(exc) or repr(exc)) from exc
        except TimeoutError as exc:
            # The SAME wrapping, as a second clause, because that 3.11+ identity is
            # a premise and not a given. Measured: on 3.10 -- which is in this
            # repo's CI matrix -- asyncio.TimeoutError is a DISTINCT class from
            # builtins.TimeoutError, so the clause above cannot see the plain
            # TimeoutError that a store or socket read raises there (socket.timeout
            # aliases it from 3.10 on), and the raw error escaped the gate
            # unwrapped: no CronGateWorkTimeout, and the caller could not tell a
            # failed dispatch DECISION from a bound expiring. On 3.11+ the two
            # classes are one, so this clause is simply unreachable there.
            #
            # A separate clause rather than `except (asyncio.TimeoutError,
            # TimeoutError)`: under --target-version py314 black rewrites
            # `except (A, B):` into PEP 758's `except A, B:`, a SyntaxError on
            # <=3.13 -- that is, on the exact interpreter this clause exists for.
            raise CronGateWorkTimeout(str(exc) or repr(exc)) from exc

    queued_at = asyncio.get_running_loop().time()
    # Split, not duplicated -- see :data:`_CRON_GATE_QUEUE_SHARE`.  The two bounds
    # sum to exactly *timeout*, so the gate's TOTAL stays inside the budget that
    # was sized to land below the wake deadline; subtracting rather than scaling
    # the second share is what keeps that sum exact.
    queue_bound = timeout * _CRON_GATE_QUEUE_SHARE
    try:
        return await run_in_cron_pool(
            _guard_gate_work,
            *args,
            timeout=timeout - queue_bound,
            queue_timeout=queue_bound,
            executor=cron_gate_executor(),
        )
    except CronQueueTimeout:
        # Queue starvation already carries the right type and wording.
        raise
    except CronGateWorkTimeout:
        # The gate's own work failing must stay distinguishable from the bound.
        # Kept as a separate clause rather than a tuple: under --target-version
        # py314 black rewrites `except (A, B):` to PEP 758's `except A, B:`, which
        # the interpreters this still runs on cannot parse.
        raise
    except asyncio.TimeoutError as exc:
        raise CronGateTimeout(asyncio.get_running_loop().time() - queued_at) from exc


async def run_in_embed_pool(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run a blocking Ollama embed/probe callable on :func:`embed_executor`.

    Drop-in replacement for ``asyncio.to_thread`` at embed call sites: same
    signature, but the work lands on the bounded ``mc-embed`` bulkhead pool
    instead of asyncio's shared default executor.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(embed_executor(), functools.partial(func, *args, **kwargs))


def shutdown_maintenance_executor() -> None:
    """Shut down all maintenance pools if they were created.  Idempotent.

    The default executor pool is NOT included here -- it is owned by each event
    loop and shut down by asyncio when the loop closes.
    """
    global _pool, _subprocess_pool, _cron_pool, _discovery_pool, _embed_pool
    global _governance_pool, _image_pool, _cron_gate_pool, _stt_pool, _path_resolve_pool
    with _lock:
        pool, _pool = _pool, None
        subprocess_pool, _subprocess_pool = _subprocess_pool, None
        cron_pool, _cron_pool = _cron_pool, None
        discovery_pool, _discovery_pool = _discovery_pool, None
        embed_pool, _embed_pool = _embed_pool, None
        governance_pool, _governance_pool = _governance_pool, None
        image_pool, _image_pool = _image_pool, None
        cron_gate_pool, _cron_gate_pool = _cron_gate_pool, None
        stt_pool, _stt_pool = _stt_pool, None
        path_resolve_pool, _path_resolve_pool = _path_resolve_pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
    if subprocess_pool is not None:
        subprocess_pool.shutdown(wait=False, cancel_futures=True)
    if cron_pool is not None:
        cron_pool.shutdown(wait=False, cancel_futures=True)
    if discovery_pool is not None:
        discovery_pool.shutdown(wait=False, cancel_futures=True)
    if embed_pool is not None:
        embed_pool.shutdown(wait=False, cancel_futures=True)
    if governance_pool is not None:
        governance_pool.shutdown(wait=False, cancel_futures=True)
    if image_pool is not None:
        image_pool.shutdown(wait=False, cancel_futures=True)
    if cron_gate_pool is not None:
        cron_gate_pool.shutdown(wait=False, cancel_futures=True)
    if stt_pool is not None:
        stt_pool.shutdown(wait=False, cancel_futures=True)
    if path_resolve_pool is not None:
        path_resolve_pool.shutdown(wait=False, cancel_futures=True)
