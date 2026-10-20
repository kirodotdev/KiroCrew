"""Per-session liveness oracle — wellness is the detector, timeouts are the backstop.

A timeout is a poor death detector: a stale-turn window (90s) or a tool-stall
window (600s) kills healthy-but-slow work — a silent 30-minute redirected
build, a ``wait(1800)`` poll, or a long non-streamed reasoning stretch. This
module inverts that shape: an EFFECTIVE per-session oracle returns a verdict
with evidence, so the watchdog in ``session_handle_dispatch.py`` acts FASTER on real
deaths and never on healthy work.

    verdict = oracle(session) -> WORKING | DEAD | STUCK_INPUT | UNKNOWN

Policy (enforced by the caller in ``session_handle_dispatch._dispatch_events``):

- ``WORKING``     -> never act (log once per interval at most), except an
  opaque MCP tool's ``mcp subtree active`` reading, which the tool branch
  bounds by ``watchdog.tool_stall_hard_cap_secs``.
- ``DEAD``        -> act immediately: recovery lands seconds after actual death
                     instead of at a blanket 90s/600s timeout.
- ``STUCK_INPUT`` -> act immediately, with a cause the recovery nudge can name
                     ("re-run non-interactively").
- ``UNKNOWN``     -> the only timeout-governed class, with non-lethal actions.

Evidence sources (Linux ``/proc`` first-class, a ``libproc`` backend on
macOS, no new dependencies):

- **Shell tool in flight**: scan the runtime's descendant tree for a non-zombie
  child whose cmdline matches the session's cached command. A live match is
  definite WORKING (a 40-minute build runs untouched). Once matched, the pid is
  tracked so exit detection is exact: a tracked child that exits without a tool
  result frame flips to DEAD after a short grace. A matched-but-frozen subtree
  blocked reading a tty/stdin is STUCK_INPUT. When nothing matches, the two
  reasons are kept apart: an OBSERVABLE tree in which no live descendant is
  young enough to have been started by this dispatch carries the
  :data:`EVIDENCE_SHELL_CHILD_ABSENT` tag (the command is not running — the
  caller narrows the UNKNOWN window), while a tree that does hold such a
  descendant, or one whose child list cannot be read at all, keeps the plain
  evidence and the full window: the match heuristic may simply have failed to
  recognize live work.
- **MCP ``wait`` tool**: declared-duration contract — WORKING until the parsed
  ``seconds`` (+ slack) elapse, then UNKNOWN.
- **Other MCP tools**: sample the descendant tree's CPU/IO movement across
  successive checks; moving -> WORKING, flat -> UNKNOWN. A flat tree in which
  the in-flight tool's OWN MCP server (or a process below it) holds an
  established TCP connection carries the :data:`EVIDENCE_REMOTE_FLAT` tag (a
  tool blocked on its own remote call), read from ``/proc`` on Linux and from
  libproc's socket fd info on macOS.
- **Model-wait (no tool in flight)**: sample the tree's IO/CPU counters across
  checks (token/keepalive receipt moves them) and its established TCP sockets.
  Flat counters with NO established backend socket is the done-but-lost-frame
  wedge signature -> DEAD. Established-but-flat is UNKNOWN with the
  :data:`EVIDENCE_ESTABLISHED_FLAT` tag so the caller can extend the probe
  window for probably-thinking (non-streamed reasoning) turns.

On macOS there is no procfs, so the tree is read in-process through ``libproc``
(:class:`LibprocBackend`, selected once per oracle when the platform is darwin
and ``proc_root`` does not exist): ``proc_listchildpids`` enumerates the
runtime's descendants, ``PROC_PIDTBSDINFO`` dates each one and tells a zombie
apart, ``KERN_PROCARGS2`` (falling back to ``proc_pidpath``) supplies the
cmdline the SAME matching rules run against, and ``PROC_PIDTASKINFO`` sums the
subtree's CPU time. That gives macOS the shell-child match / exit detection /
absence narrowing and the MCP-subtree movement probe, with movement being
CPU-only (the evidence string says so — there is no per-process IO counter to
read). libproc's socket fd info feeds only the ``remote_flat`` scan of the
in-flight tool's own server. What has no libproc equivalent stays exactly as
absent: no model-wait socket evidence (so a flat model wait is UNKNOWN, never
DEAD, and never tagged ``established_flat``), no wchan / blocked-fd evidence (so
STUCK_INPUT is never claimed — a live tracked child whose subtree is flat is
UNKNOWN tagged ``platform_limited`` instead of WORKING, so it is bounded rather
than deferred forever). The backend is injectable (``darwin_backend`` ctor arg),
and the Linux path is untouched by its presence.

Every probe is wrapped: any error degrades the verdict to UNKNOWN — never to a
kill. Each check is cheap (<10ms of file reads); two-sample deltas are computed
across successive calls (the dispatch loop's queue-timeout ticks) rather than
sleeping inline, gated on ``sample_min_secs`` between samples.

Platform evidence matrix (what each host can attest; "absent" is DECLARED, not
inferred — an absent row never produces DEAD or STUCK_INPUT, and never WORKING
on liveness alone):

======================  ==========================  ==========================  ==========================
Evidence                Linux (``/proc``)           macOS (``libproc``)         Windows (no tree backend)
======================  ==========================  ==========================  ==========================
process tree            ``task/*/children``         ``proc_listchildpids``      absent → every shell/MCP
                                                                                verdict UNKNOWN, tagged
                                                                                ``platform_limited``
shell child match/exit  cmdline + ``starttime``     argv/path + start time      absent
subtree movement        CPU jiffies + IO bytes      CPU ns only (no IO)         root-pid CPU only
                                                                                (model-wait probe)
blocked on stdin        ``wchan`` + blocked fd →    absent → live+flat child    absent
(STUCK_INPUT)           STUCK_INPUT                 is UNKNOWN tagged
                                                    ``platform_limited``
socket / LLM wait       ``/proc/net`` established   absent → flat model wait    absent
(``established_flat``)  → UNKNOWN or DEAD           is plain UNKNOWN,
                                                    never DEAD
business progress       stream events, ``kirocrew/status`` (platform-independent; outranks all rows)
======================  ==========================  ==========================  ==========================

Two rules hold on every row. "Process alive" is never sufficient for an
indefinite deferral: where the platform cannot tell a stuck child from a quiet
one, the verdict is UNKNOWN with the :data:`EVIDENCE_PLATFORM_LIMITED` tag and
the caller's no-progress budget (``tool_stall_suspect_secs``, hard-capped)
bounds it. And "no output" alone never kills a long task that shows movement: a
subtree whose CPU (or IO, where readable) moves across two samples is WORKING
whatever it has printed. Where stdin-block evidence is absent, the W4
(``waiting_input``) classification comes from the tool layer instead —
:func:`classify_interactive_command` runs at dispatch and its verdict rides
``ToolCallState.interactive_risk`` — so a prompt-shaped command that goes
flat on macOS/Windows is classified as waiting for input at the (narrowed)
budget rather than as an opaque stall.

Attribution on a shared runtime: each handle probes only ITS OWN runtime pid,
but a runtime can host several sessions, and then that pid's process tree is not
one session's. Which probes survive that is not uniform, so it is stated per
probe rather than claimed for the module:

- the cmdline match keys shell evidence to THIS session's in-flight command, so
  the shell branch attributes;
- the ``remote_flat`` socket scan reads only the subtree of the process that
  was launched as the in-flight tool's own MCP server, and declines when that
  process is not found exactly once (a second copy of the server is another
  session's or a subagent's), so it attributes too;
- the movement and model-wait socket probes read the WHOLE tree, so a
  co-tenant's build reads as this session's progress and a co-tenant's backend
  connection suppresses this session's wedge signature. Both of those err
  toward forbearance, which costs detection latency and no work;
- the one reading that is acted on at once, the model-wait DEAD, is therefore
  gated on declared tenancy: with co-tenants it degrades to UNKNOWN tagged
  :data:`EVIDENCE_SHARED_TREE` and the caller's stale window governs instead.
  Pass ``tenancy`` to declare it; a caller that declares nothing gets the
  historical single-tenant reading.

Where attribution is impossible the verdict degrades to UNKNOWN, never to a kill.

Unit-testable against a fake ``/proc`` tree via the ``proc_root`` ctor arg and
an injectable ``now`` clock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys  # noqa: F401 - re-exported: tests patch sys.platform through it
import time
from collections.abc import Mapping
from concurrent.futures import Executor
from typing import Any, Callable, Iterable, Protocol

from kiro_crew import platform_compat
from kiro_crew.acp.liveness_command import (  # noqa: F401 - re-exported for existing importers
    _COMMAND_ELISION,
    MAX_RETAINED_COMMAND_CHARS,
    MAX_RETAINED_COMMAND_TAIL_CHARS,
    MAX_RETAINED_NAME_CHARS,
    MAX_RETAINED_TITLE_CHARS,
    LaunchRoster,
    _command_text,
    bounded_command_text,
    first_program,
    launch_matches,
    match_fragment,
    parse_wait_seconds,
)
from kiro_crew.acp.liveness_evidence import (  # noqa: F401 - re-exported for existing importers
    _DISPATCH_START_TOLERANCE_SECS,
    _GENERIC_PROGRAMS,
    _KEEPALIVE_TOOL_NAMES,
    _MCP_SEPARATOR_RE,
    _MIN_MATCH_FRAGMENT,
    _MODEL_WRAPPING_TOOLS,
    _PORTABLE_CPU_KEY,
    _READ_SYSCALL_NRS,
    _STUCK_INPUT_WCHANS,
    _WAIT_SECONDS_RE,
    _WAIT_TOOL_NAME,
    CHILD_EXIT_GRACE_SECS,
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_MCP_SUBTREE_ACTIVE,
    EVIDENCE_PLATFORM_LIMITED,
    EVIDENCE_REMOTE_FLAT,
    EVIDENCE_SAMPLING,
    EVIDENCE_SHARED_TREE,
    EVIDENCE_SHELL_CHILD_ABSENT,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    WAIT_TOOL_SLACK_SECS,
)
from kiro_crew.acp.liveness_interactive import (  # noqa: F401 - re-exported for existing importers
    _CONFIRM_TABLE,
    _EDITOR_PROGRAMS,
    _GIT_CONTINUE_EDITS,
    _GIT_PAGED_SUBCOMMANDS,
    _GIT_REVERT_KEEPS_MESSAGE,
    _GIT_VALUE_OPTIONS,
    _NON_INTERACTIVE_EDITORS,
    _PAGER_PROGRAMS,
    _PROMPT_SUBCOMMAND,
    _PROMPT_TABLE,
    _REPL_NONINTERACTIVE_OPTS,
    _REPL_PROGRAMS,
    _SEGMENT_SPLIT_RE,
    _SSH_BATCH_FLAGS,
    _SSH_NONINTERACTIVE_OPTIONS,
    _WRAPPER_PROGRAMS,
    INTERACTIVE_CONFIRM,
    INTERACTIVE_EDITOR,
    INTERACTIVE_NARROWING_RISKS,
    INTERACTIVE_NONE,
    INTERACTIVE_PAGER,
    INTERACTIVE_PROMPT,
    INTERACTIVE_REPL,
    NOT_INTERACTIVE,
    InteractiveClassification,
    _classify_segment,
    _git_editor_settings,
    _has_flag,
    _segment_program,
    classify_interactive_command,
    non_interactive_hint,
)
from kiro_crew.acp.liveness_proc import (  # noqa: F401 - re-exported for existing importers
    _DARWIN_CPU_KEY,
    _DARWIN_CPU_LABEL,
    DarwinProcessBackend,
    LibprocBackend,
    ProcessRow,
    _is_loopback_hex,
    _read_text,
    blocked_read_fd,
    children_interface_readable,
    direct_children,
    established_inodes,
    fd_target,
    iter_descendants,
    pipe_writer_pid,
    read_cmdline,
    read_io_bytes,
    read_pid_stat,
    read_wchan,
    remote_established_inodes,
    select_darwin_backend,
    socket_inodes,
    steady_now,
)
from kiro_crew.acp.liveness_tool_state import (  # noqa: F401 - re-exported for existing importers
    InFlightToolTracker,
    ToolCallState,
    wait_tool_verdict,
)
from kiro_crew.constants import WAIT_TOOL_MAX_SECS  # noqa: F401 - re-exported
from kiro_crew.platform_compat import (  # noqa: F401 - re-exported for existing importers
    boottime_now,
    process_start_boot_secs,
)
from kiro_crew.session_directive import CORE_MCP_SERVER  # noqa: F401 - re-exported

logger = logging.getLogger(__name__)


class LivenessOracle:
    """Per-session liveness verdicts from /proc evidence (libproc on macOS).

    One instance per :class:`~kiro_crew.acp.session_handle.AcpSessionHandle`.
    Stateful across checks: it tracks the matched shell child (so exit
    detection is exact) and prior counter samples (so movement deltas are
    computed across the dispatch loop's ticks instead of sleeping inline).
    Drop the baseline at turn start and on every new tool dispatch. Consumers that
    run the check inline may :meth:`reset`; one that offloads it must retire the
    instance with :meth:`fresh` instead, because a timed-out probe keeps writing
    into the object it was handed (see :meth:`reset`).

    Every public check is fully wrapped — any unexpected error returns
    ``(VERDICT_UNKNOWN, ...)``, never raises, never a kill.
    """

    def __init__(
        self,
        proc_root: str = "/proc",
        *,
        now=time.monotonic,
        sample_min_secs: float = 3.0,
        darwin_backend: DarwinProcessBackend | None = None,
        wall_now=time.time,
        steady_now_fn=steady_now,
        tenancy: Callable[[], int | None] | None = None,
        server_launches: Callable[[], LaunchRoster | None] | None = None,
    ) -> None:
        self._proc = str(proc_root)
        self._now = now
        # The darwin start-time ruler and its step detector (see
        # ``_started_after_dispatch``); injectable so a test can step one without
        # the other. Unused on a ``/proc`` host.
        self._wall_now = wall_now
        self._steady_now = steady_now_fn
        self._sample_min_secs = sample_min_secs
        # The one platform probe: decided at construction so every check on this
        # instance reads the same source. A caller passes its own backend to
        # drive the darwin paths without libproc (tests) or to pin the choice.
        self._darwin = (
            darwin_backend if darwin_backend is not None else select_darwin_backend(self._proc)
        )
        self._tracked_child: int | None = None
        self._child_gone_ts: float | None = None
        # How many sessions the probed runtime hosts, asked at verdict time
        # because a runtime gains and loses tenants while a turn runs. Only the
        # DEAD branch of the model wait reads it — see
        # :data:`EVIDENCE_SHARED_TREE` for why that branch and not the others.
        #
        # ``None`` means the caller declares NOTHING, and the module then reads
        # the tree as one session's, which is the 1:1 premise this refactor is
        # removing. It is the default so that a caller which has not been taught
        # about tenancy keeps today's verdicts exactly; a caller whose runtime
        # CAN host a second session is the one that must pass this.
        self._tenancy = tenancy
        # How this session launched its MCP servers (see :data:`LaunchRoster`),
        # read ONLY by the socket scan behind the ``remote_flat`` tag, which
        # uses it to find the in-flight tool's own server process. Asked at
        # verdict time because a resumed session re-declares its servers.
        # ``None`` -- undeclared, or the callable answering None -- keeps the
        # tag off.
        self._server_launches = server_launches
        # sample key -> (ts, counter). Keys: "io", "cpu".
        self._samples: dict[str, tuple[float, int]] = {}

    def reset(self) -> None:
        """Clear tracked child + samples in place.

        Safe only when no detached worker still holds this instance. A consult
        offloaded to ``subprocess_executor()`` keeps a bound reference to the
        oracle it sampled into, so clearing in place leaves that writer pointed at
        the live baseline — where a late write becomes the next generation's
        starting point and any delta reads as movement. Prefer :meth:`fresh`.

        Neither in-tree consumer meets that condition: ``AcpClient`` and
        ``AcpSessionHandle`` both offload their consult, so both retire the whole
        instance at each liveness-state boundary instead of clearing it. This
        method remains for an inline caller, which has no detached writer to
        confine.
        """
        self._tracked_child = None
        self._child_gone_ts = None
        self._samples.clear()

    def fresh(self) -> "LivenessOracle":
        """A new oracle carrying this one's configuration and no samples.

        Callers retire an instance instead of ``reset()``-ing it when a detached
        worker may still hold a reference to it: samples are keyed without a PID,
        so a late write would otherwise land on the live baseline and read as
        movement.
        """
        return LivenessOracle(
            self._proc,
            now=self._now,
            sample_min_secs=self._sample_min_secs,
            darwin_backend=self._darwin,
            wall_now=self._wall_now,
            steady_now_fn=self._steady_now,
            tenancy=self._tenancy,
            server_launches=self._server_launches,
        )

    # ── Public checks ──

    def _tree_observable(self) -> bool:
        """Whether this oracle has ANY view of the runtime's process tree.

        True with a darwin backend or a readable ``proc_root``; False on a host
        with neither (Windows today), where every tree-based probe reads
        absence of evidence rather than a negative — see
        :data:`EVIDENCE_PLATFORM_LIMITED`.
        """
        return self._darwin is not None or os.path.isdir(self._proc)

    def check_tool(self, runtime_pid: int | None, tool: ToolCallState) -> tuple[str, str]:
        """Verdict for an in-flight tool call. Never raises."""
        try:
            return self._check_tool(runtime_pid, tool)
        except Exception:
            logger.debug("liveness: check_tool failed", exc_info=True)
            return VERDICT_UNKNOWN, "oracle error"

    def check_model_wait(self, runtime_pid: int | None) -> tuple[str, str]:
        """Verdict for a model-wait (no tool in flight). Never raises."""
        try:
            return self._check_model_wait(runtime_pid)
        except Exception:
            logger.debug("liveness: check_model_wait failed", exc_info=True)
            return VERDICT_UNKNOWN, "oracle error"

    # ── Tool-in-flight evidence ──

    def _check_tool(self, runtime_pid: int | None, tool: ToolCallState) -> tuple[str, str]:
        if not runtime_pid:
            return VERDICT_UNKNOWN, "no runtime pid"

        wait = wait_tool_verdict(tool, self._now())
        if wait is not None:
            return wait

        if tool.is_shell:
            return self._check_shell_child(runtime_pid, tool)

        # Opaque MCP tool: any CPU/IO movement in the runtime's descendant
        # tree (which contains the serving MCP server process) reads WORKING.
        moved, evidence = self._tree_movement(runtime_pid)
        if moved:
            return VERDICT_WORKING, f"{EVIDENCE_MCP_SUBTREE_ACTIVE} ({evidence})"
        if evidence == "no readable counters" and not self._tree_observable():
            # No tree at all on this platform: the flat reading is absence of
            # evidence, and the caller's budget is the only bound. Tagged so
            # the degradation is visible rather than a plain UNKNOWN.
            return (
                VERDICT_UNKNOWN,
                f"{EVIDENCE_PLATFORM_LIMITED}: mcp subtree unobservable "
                "(no process-tree backend on this platform)",
            )
        # LLM-turn shape inside a tool (e.g. a use_subagent call wrapping a
        # model turn in kiro-cli): the subtree is GENUINELY flat (a real
        # two-sample delta, not the baseline tick or unreadable counters) and
        # the RUNTIME PROCESS ITSELF holds an established backend socket. Tag
        # it — same tag as the model-wait branch — so the caller narrows the
        # UNKNOWN window to the model-silent budget instead of the build-scale
        # forbearance. Verdict stays UNKNOWN: the tag is evidence, never an
        # action. Deliberately NARROWER than the model-wait branch's full-tree
        # ``_any_established``: here the descendants include the tool's own
        # workers, and an MCP server blocked on ITS remote socket (a long
        # remote call, zero CPU/IO while in recv) is not a model wait: it is
        # the remote_flat shape handled below, never this tag. Only
        # kiro-cli's own backend connection is LLM-wait
        # evidence. Shell-child evidence never reaches this branch (it returns
        # from _check_shell_child above), and a flat subtree without the
        # runtime-held socket keeps the plain evidence. Under the OS sandbox
        # (pid = launcher parent) the runtime holds no sockets, so this fails
        # toward the old full-window behavior, never toward over-narrowing.
        #
        # F1 attribution guard: only apply established_flat narrowing when the
        # in-flight tool is a KNOWN model-wrapping operation (tool.tool_name in
        # _MODEL_WRAPPING_TOOLS). A persistent keepalive socket to the model
        # service can exist independently of the current MCP tool: tagging a
        # quiet ReadInternalWebsites with established_flat would incorrectly
        # narrow its build-scale window to 15 minutes. Without positive tool-identity
        # attribution (empty tool_name or an unknown tool), fall back to the
        # plain mcp_subtree_flat evidence so the full build-scale window holds.
        if (
            evidence not in (EVIDENCE_SAMPLING, "no readable counters")
            and tool.tool_name in _MODEL_WRAPPING_TOOLS
        ):
            held = socket_inodes(self._proc, runtime_pid)
            if held and held & established_inodes(self._proc, runtime_pid):
                return (
                    VERDICT_UNKNOWN,
                    f"{EVIDENCE_ESTABLISHED_FLAT}: mcp subtree flat ({evidence})",
                )
        if (
            evidence
            not in (
                EVIDENCE_SAMPLING,
                "no readable counters",
            )
            and tool.mcp_server_name
        ):
            located = self._own_server_remote_holder(runtime_pid, tool.mcp_server_name)
            if located is not None:
                root, holder = located
                return (
                    VERDICT_UNKNOWN,
                    f"{EVIDENCE_REMOTE_FLAT}: mcp subtree flat, pid {holder} of the tool's "
                    f"own server (pid {root}) holds an established TCP connection ({evidence})",
                )
        return VERDICT_UNKNOWN, f"mcp subtree flat ({evidence})"

    def _launch_roster(self) -> LaunchRoster | None:
        """The declared :data:`LaunchRoster`, or None when none is declared.

        Undeclared, raising, or anything but a mapping all read as None, which
        keeps the ``remote_flat`` tag off.
        """
        if self._server_launches is None:
            return None
        try:
            roster = self._server_launches()
        except Exception:
            logger.debug("liveness: launch roster probe failed", exc_info=True)
            return None
        return roster if isinstance(roster, Mapping) else None

    def _own_server_remote_holder(self, runtime_pid: int, server: str) -> tuple[int, int] | None:
        """``(server root pid, holder pid)`` when *server*'s own subtree holds an
        ESTABLISHED TCP connection to a non-loopback peer, else None.

        Only the subtree of the process that IS the named server counts, so a
        sibling server's persistent connection (a keepalive, ``mcp-remote``) or a
        shell tool's background tunnel elsewhere in the tree never tags this
        call. The server root is found by :meth:`_own_server_root`; a server that
        cannot be found exactly once answers None. A loopback peer is a local
        service (Kiro Crew's gateway above all), not a remote call.

        On darwin a backend without ``established_tcp``, and a host with neither
        backend (Windows), answer None.
        """
        roster = self._launch_roster()
        if roster is None or not roster.get(server):
            return None
        if self._darwin is not None:
            probe = getattr(self._darwin, "established_tcp", None)
            if probe is None:
                return None
            root = self._own_server_root(runtime_pid, server, roster)
            if root is None:
                return None
            for pid in (root, *(self._darwin.descendants(root) or ())):
                if probe(pid):
                    return root, pid
            return None
        if not os.path.isdir(self._proc):
            return None
        root = self._own_server_root(runtime_pid, server, roster)
        if root is None:
            return None
        for pid in iter_descendants(self._proc, root):
            held = socket_inodes(self._proc, pid)
            if held and held & remote_established_inodes(self._proc, pid):
                return root, pid
        return None

    def _own_server_root(self, runtime_pid: int, server: str, roster: LaunchRoster) -> int | None:
        """The one process below the runtime that was launched as *server*.

        A process is the server when its argv matches one of the server's launch
        vectors (:func:`launch_matches`). A launcher that starts a versioned copy
        of itself as its child matches twice; the topmost match is the server
        and the rest sit in its subtree. Anything other than exactly ONE topmost
        match answers None, and so does a match that also reads as another
        declared server:

        - two matches is a second copy -- another session's server on a shared
          runtime, or a subagent's -- and which copy serves this call is
          unknowable from the tree;
        - a process that fits two names cannot be attributed to either.

        The runtime pid itself is never a server. On ``/proc`` a root that holds
        no socket at all is the namespace sandbox's launcher parent, and its
        direct children (the real kiro-cli) are excluded too, so kiro-cli's own
        model connection can never stand in for a server's. On darwin the
        runtime pid IS kiro-cli, so only the root is excluded.
        """
        launches = roster.get(server) or ()
        if self._darwin is not None:
            listed = self._darwin.descendants(runtime_pid)
            if not listed:
                return None
            rows: list[tuple[int, str]] = []
            for pid in listed:
                row = self._darwin.row(pid)
                if row is not None:
                    rows.append((pid, row.cmdline))
        else:
            excluded = {runtime_pid}
            if not socket_inodes(self._proc, runtime_pid):
                excluded.update(direct_children(self._proc, runtime_pid))
            rows = [
                (pid, read_cmdline(self._proc, pid))
                for pid in iter_descendants(self._proc, runtime_pid)
                if pid not in excluded
            ]
        matches = [pid for pid, cmd in rows if any(launch_matches(cmd, la) for la in launches)]
        if not matches:
            return None
        nested: set[int] = set()
        for pid in matches:
            if self._darwin is not None:
                nested.update(self._darwin.descendants(pid) or ())
            else:
                nested.update(p for p in iter_descendants(self._proc, pid) if p != pid)
        topmost = [pid for pid in matches if pid not in nested]
        if len(topmost) != 1:
            return None
        root = topmost[0]
        cmdline = next(cmd for pid, cmd in rows if pid == root)
        for other, other_launches in roster.items():
            if other != server and any(launch_matches(cmdline, la) for la in other_launches):
                return None
        return root

    def _check_shell_child(self, runtime_pid: int, tool: ToolCallState) -> tuple[str, str]:
        if self._darwin is not None:
            return self._check_shell_child_darwin(self._darwin, runtime_pid, tool)
        if not self._tree_observable():
            # Neither procfs nor a tree backend (Windows today): no child can be
            # matched, dated or watched for exit, so every shell verdict is
            # UNKNOWN. Tagged, so the caller's budget — not "alive, therefore
            # forever" — is visibly what bounds the call on this platform.
            return (
                VERDICT_UNKNOWN,
                f"{EVIDENCE_PLATFORM_LIMITED}: no process-tree backend on this platform",
            )
        descendants = iter_descendants(self._proc, runtime_pid)

        # Exact exit detection once a child was matched.
        if self._tracked_child is not None:
            stat = read_pid_stat(self._proc, self._tracked_child)
            alive = stat is not None and stat[0] != "Z" and self._tracked_child in descendants
            return self._tracked_child_verdict(self._tracked_child, alive)

        # Not matched yet: scan for a live non-zombie descendant whose cmdline
        # matches this session's cached command. The same pass answers a second,
        # cmdline-INDEPENDENT question — is any live descendant young enough to
        # have been started by this dispatch — which is what separates "the
        # command already exited" from "the command is running unrecognized".
        matched, live_descendants, possibly_running = self._scan_for_child(
            self._proc_rows(runtime_pid, descendants), tool
        )
        if matched is not None:
            return self._track_child(matched)
        if possibly_running:
            return VERDICT_UNKNOWN, "no matching shell child"
        if not live_descendants and not children_interface_readable(self._proc, runtime_pid):
            # No live descendant AND no readable child list: the tree is not
            # observable (no procfs, no CONFIG_PROC_CHILDREN, a sandbox hiding
            # the subtree), which is indistinguishable from an empty one. Absence
            # is not assertable, so nothing narrows.
            return VERDICT_UNKNOWN, "no matching shell child"
        return self._absent_child_verdict(live_descendants)

    def _check_shell_child_darwin(
        self, backend: DarwinProcessBackend, runtime_pid: int, tool: ToolCallState
    ) -> tuple[str, str]:
        """The ``/proc`` shell-child check, fed by *backend* instead of procfs.

        Same tracked-child exit detection, same matching pass, same three-way
        unmatched verdict. The observability question the ``/proc`` walk answers
        with ``children_interface_readable`` is answered here by enumeration
        itself: a tree that could not be read is None, and only a read tree
        grounds an absence claim.
        """
        descendants = backend.descendants(runtime_pid)

        if self._tracked_child is not None:
            row = backend.row(self._tracked_child)
            # Membership is tested only against a tree that could be read: a
            # transient enumeration failure must not age a live child through the
            # grace into DEAD, the one verdict this branch can be wrong about.
            alive = row is not None and (descendants is None or self._tracked_child in descendants)
            return self._tracked_child_verdict(self._tracked_child, alive)

        if descendants is None:
            return VERDICT_UNKNOWN, "no matching shell child"
        rows = (row for row in map(backend.row, descendants) if row is not None)
        matched, live_descendants, possibly_running = self._scan_for_child(rows, tool)
        if matched is not None:
            return self._track_child(matched)
        if possibly_running:
            return VERDICT_UNKNOWN, "no matching shell child"
        return self._absent_child_verdict(live_descendants)

    def _proc_rows(self, runtime_pid: int, descendants: list[int]) -> Iterable[ProcessRow]:
        """The live, non-zombie descendants in *descendants*, read from ``/proc``.

        The start is dated before the cmdline is read, on purpose: a live
        descendant whose cmdline is momentarily unreadable (mid-exec,
        mid-teardown) still counts as possibly-this-tool's in the scan.
        """
        for pid in descendants:
            if pid == runtime_pid:
                continue
            stat = read_pid_stat(self._proc, pid)
            if stat is None or stat[0] == "Z":
                continue
            yield ProcessRow(
                pid=pid,
                started=process_start_boot_secs(stat[1]),
                cmdline=read_cmdline(self._proc, pid),
            )

    def _scan_for_child(
        self, rows: Iterable[ProcessRow], tool: ToolCallState
    ) -> tuple[int | None, int, bool]:
        """``(matched_pid, live_descendants, possibly_running)`` over *rows*.

        ``matched_pid`` is the first live descendant whose cmdline matches the
        cached command AND that is young enough to be this dispatch's own.
        ``possibly_running`` is True when something that could be this command
        is running unrecognized: a descendant started since the dispatch (the
        match heuristic may have missed a live command — a shell that exec'd
        away, a cached input redacted past any usable fragment), or one whose
        cmdline matches while predating a late-taken stamp. Either vetoes the
        absence claim; the caller keeps the plain evidence and the full window.
        """
        fragment = match_fragment(tool.command)
        program = first_program(tool.command)
        live_descendants = 0
        started_since_dispatch = False
        matched_but_older = False
        for row in rows:
            live_descendants += 1
            fresh = self._started_after_dispatch(row.started, tool)
            started_since_dispatch = started_since_dispatch or fresh
            if not row.cmdline:
                continue
            matched = bool(fragment) and fragment in row.cmdline
            if not matched and program:
                base_tokens = {t.rsplit("/", 1)[-1] for t in row.cmdline.split()}
                matched = program in base_tokens
            if not matched:
                continue
            if not fresh:
                # Cmdline matches but the process predates the dispatch stamp by
                # more than the tolerance. Two causes the oracle cannot tell
                # apart: a coincidental pre-existing lookalike, or THIS command,
                # whose tool_call frame reached the dispatch loop late — the
                # stamp is taken when that frame is PROCESSED, and the consumer
                # can park for minutes on an approval, an IM send or a hook
                # (see ``_parked`` in the dispatch loop) while kiro-cli has
                # already spawned. So refuse the match, but let it veto the
                # absence claim: a live process that looks like this command is
                # not evidence that nothing is running.
                matched_but_older = True
                continue
            return row.pid, live_descendants, True
        return None, live_descendants, started_since_dispatch or matched_but_older

    def _track_child(self, pid: int) -> tuple[str, str]:
        self._tracked_child = pid
        self._child_gone_ts = None
        # Prime the stuck-detection movement baseline now so the NEXT check
        # can already compare deltas (otherwise stuck detection needs three
        # ticks: match, baseline, compare).
        self._tree_movement(pid, key_prefix="stuck")
        return VERDICT_WORKING, f"shell child {pid} matched command"

    def _tracked_child_verdict(self, pid: int, alive: bool) -> tuple[str, str]:
        """Verdict for the tracked shell child *pid* given whether it is still alive."""
        if alive:
            self._child_gone_ts = None
            if self._darwin is not None:
                return self._alive_child_verdict_platform_limited(pid)
            stuck = self._stuck_input_check(pid)
            if stuck:
                return VERDICT_STUCK_INPUT, stuck
            return VERDICT_WORKING, f"shell child {pid} alive"
        if self._child_gone_ts is None:
            self._child_gone_ts = self._now()
        gone_for = self._now() - self._child_gone_ts
        if gone_for > CHILD_EXIT_GRACE_SECS:
            return (
                VERDICT_DEAD,
                f"shell child {pid} exited {gone_for:.0f}s ago, no result frame",
            )
        return VERDICT_UNKNOWN, f"shell child exited {gone_for:.0f}s ago (grace)"

    def _alive_child_verdict_platform_limited(self, pid: int) -> tuple[str, str]:
        """The live-tracked-child verdict where stdin-block evidence is absent.

        On ``/proc`` a live child whose subtree is flat is split by ``wchan`` /
        blocked-fd evidence into STUCK_INPUT (act now) and WORKING (a quiet
        build). Without that evidence the two are one state, and the oracle
        must not pick the forgiving reading for both: a WORKING verdict is
        deferred with NO window, so a ``python`` REPL wedged on an open stdin
        pipe would hold its slot for the turn's whole ceiling. So the split is
        made on the one thing the platform CAN see — movement: a moving
        subtree is WORKING (a quiet build with CPU or a child that just
        exited still moves), and a genuinely flat one (a real two-sample
        delta, not the baseline tick) is UNKNOWN tagged
        :data:`EVIDENCE_PLATFORM_LIMITED`, which the caller bounds by the
        no-progress budget. Never STUCK_INPUT: the evidence for that claim is
        exactly what is missing, and inventing it would auto-answer nothing but
        would name a cause the recovery nudge then acts on.
        """
        moved, evidence = self._tree_movement(pid, key_prefix="stuck")
        if moved or evidence in (EVIDENCE_SAMPLING, "no readable counters"):
            return VERDICT_WORKING, f"shell child {pid} alive ({evidence})"
        return (
            VERDICT_UNKNOWN,
            f"{EVIDENCE_PLATFORM_LIMITED}: shell child {pid} alive, subtree flat "
            f"({evidence}); stdin-block evidence unavailable on this platform",
        )

    def _absent_child_verdict(self, live_descendants: int) -> tuple[str, str]:
        # The tree is observable and nothing in it was started for this tool, so
        # the command is not running: the same physical state the DEAD branch
        # above reports as "exited, no result frame" — the oracle just never got
        # to see this one alive. Still UNKNOWN, not DEAD: the claim rests on
        # start-time attribution and on the child being a DESCENDANT of the
        # runtime (a double-forked, reparented child would escape the walk), so
        # the caller shortens its non-lethal cancel rather than acting at once.
        return (
            VERDICT_UNKNOWN,
            f"{EVIDENCE_SHELL_CHILD_ABSENT}: no shell child started since dispatch "
            f"({live_descendants} live descendants, none started since dispatch)",
        )

    def _started_after_dispatch(self, started: float | None, tool: ToolCallState) -> bool:
        """Whether a process started at *started* is young enough to be *tool*'s.

        Both sides are read on the same clock — the process's start and the stamp
        ``boottime_now()`` took at EVENT_TOOL_CALL — so a host suspend between
        dispatch and this probe moves neither. Deriving the start from an age
        instead (a boot-clock age subtracted from a monotonic stamp) placed a
        live child a full suspend EARLIER than it started, so a laptop resumed
        mid-command read as "this child predates its own dispatch".

        The window opens by ``dispatch_parked_secs`` as well, because the stamp
        marks when the tool_call frame was PROCESSED, not when the runtime
        spawned: a frame queued behind a consumer-side await (an approval, an IM
        send, a hook) is stamped that much late, and its child then looks older
        than its own dispatch. The dispatch loop already measures exactly that
        park, so the bound is measured rather than guessed.

        Fail-open by design: a missing stamp or an undatable process (no tick
        rate on Linux, no readable start elsewhere) answers True, so an
        unattributable process reads as possibly-this-tool's. That keeps a live
        command matched and keeps an absence claim from resting on evidence the
        oracle does not have.
        """
        if not tool.dispatch_boot_ts:
            return True
        if started is None:
            return True
        if self._darwin is not None and self._wall_clock_stepped_since(tool):
            return True
        tolerance = _DISPATCH_START_TOLERANCE_SECS + max(0.0, tool.dispatch_parked_secs)
        return started >= tool.dispatch_boot_ts - tolerance

    def _wall_clock_stepped_since(self, tool: ToolCallState) -> bool:
        """Whether the darwin start-time ruler has moved since *tool* was stamped.

        On darwin both sides of the start-time comparison are wall clock, and a
        wall clock steps. A backward step landing between the dispatch stamp and
        the runtime's fork dates a live child BEFORE its dispatch; a command the
        matchers cannot recognise (fully redacted input) then has nothing to veto
        the absence claim, and the caller narrows to the stale window and cancels
        live work. So the stamp is paired with a steady clock: when wall elapsed
        and steady elapsed disagree by more than the attribution tolerance, the
        wall clock is not a ruler this dispatch can be measured with, and every
        row reads as possibly-this-tool's (fail-open, like an undatable process).
        A missing steady stamp is the same answer: an absence claim needs a
        validated ruler, and there is none to validate.

        A suspend does not trip this: ``CLOCK_MONOTONIC`` on darwin counts sleep,
        as the wall clock does. If a host's steady clock did stop across a sleep,
        the disagreement would still fail toward the WIDER window, never toward
        a cancel.
        """
        if tool.dispatch_boot_ts is None or tool.dispatch_steady_ts is None:
            return True
        wall_elapsed = self._wall_now() - tool.dispatch_boot_ts
        steady_elapsed = self._steady_now() - tool.dispatch_steady_ts
        return abs(wall_elapsed - steady_elapsed) > _DISPATCH_START_TOLERANCE_SECS

    def _stuck_input_check(self, pid: int) -> str:
        """STUCK_INPUT evidence for a live child subtree, or "" when not stuck.

        Requires (a) the ENTIRE matched subtree flat on CPU+IO across two
        samples, and (b) at least one process blocked reading a tty or its
        stdin pipe. A socket-blocked read is a network wait (not stuck); a
        futex/lock wait is ambiguous — both return "".

        Returning "" for a LIVE child means the caller reports WORKING, and a
        WORKING verdict is NOT bounded by the UNKNOWN timeout class: the
        dispatch loop short-circuits on it (``session_handle`` defers WORKING
        unconditionally) BEFORE ``tool_stall_suspect_secs`` /
        ``tool_stall_hard_cap_secs`` are applied. So a genuinely ambiguous hang
        — a child alive but wedged on a futex, a socket read, or a hung mount —
        is deferred for as long as the turn's own wall-clock ceiling allows, not
        for a watchdog window. That is deliberate (a quiet long build must not
        be cancelled on a timer) but it is the only path with no window of its
        own, so it is logged with escalating severity rather than capped.
        """
        subtree = iter_descendants(self._proc, pid)
        moved, move_evidence = self._tree_movement(pid, key_prefix="stuck")
        if moved or move_evidence == "sampling":
            # Moving, or no second sample yet — cannot claim a flat subtree.
            return ""
        blocked = None
        for p in subtree:
            wchan = read_wchan(self._proc, p)
            if wchan not in _STUCK_INPUT_WCHANS:
                continue
            fd = blocked_read_fd(self._proc, p)
            if fd is None:
                continue
            target = fd_target(self._proc, p, fd)
            if target.startswith(("/dev/tty", "/dev/pts")) or (
                fd == 0 and target.startswith("pipe:")
            ):
                # A live writer in the subtree (``producer | consumer``) means this
                # reader waits on a producer; keep scanning (the producer may be on a
                # tty). No procfs: keep the old verdict.
                if target.startswith("pipe:") and os.path.isdir(self._proc):
                    if pipe_writer_pid(self._proc, subtree, target, p) is not None:
                        continue
                blocked = (p, target)
                break
        if blocked is None:
            return ""
        return f"stuck_input: pid {blocked[0]} blocked reading {blocked[1]} with flat subtree"

    # ── Model-wait evidence ──

    def _check_model_wait(self, runtime_pid: int | None) -> tuple[str, str]:
        if not runtime_pid:
            return VERDICT_UNKNOWN, "no runtime pid"
        moved, evidence = self._tree_movement(runtime_pid)
        if moved:
            return VERDICT_WORKING, f"backend activity ({evidence})"
        if evidence == "no readable counters":
            # No procfs AT ALL (macOS, Windows) — the /proc-shaped evidence is
            # ABSENT, not negative, so fall back to the portable probe before the
            # caller takes its conservative branch. Linux-only counters must not
            # read as "assume dead" on the platform most backends run on: that is
            # how a macOS turn gets declared complete mid-tool.
            return self._portable_model_wait(runtime_pid)
        if evidence == EVIDENCE_SAMPLING:
            # No baseline yet — cannot attest either way.
            return VERDICT_UNKNOWN, evidence
        if self._darwin is not None:
            # A genuinely flat subtree, but the split below rests on socket
            # evidence that only ``/proc`` carries. Without it a flat wait is
            # neither the lost-frame wedge nor a probable think: UNKNOWN with
            # plain evidence, so the caller's timeout policy governs and
            # nothing is invented in either direction.
            return VERDICT_UNKNOWN, f"backend subtree flat ({evidence})"
        # Flat counters: distinguish the done-but-lost-frame wedge (no backend
        # connection at all) from a probably-thinking server-side silence
        # (established socket, nothing flowing yet).
        established = self._any_established(runtime_pid)
        if established:
            return VERDICT_UNKNOWN, f"{EVIDENCE_ESTABLISHED_FLAT}: {evidence}"
        sharing = self._shared_tree_reason()
        if sharing:
            return VERDICT_UNKNOWN, f"{EVIDENCE_SHARED_TREE}: {sharing} ({evidence})"
        return VERDICT_DEAD, f"no established backend socket and flat counters ({evidence})"

    def _shared_tree_reason(self) -> str:
        """Why tree-wide evidence is not this session's, or "" when it is.

        Asked immediately before the one verdict that is acted on at once. Three
        answers, and only the first clears the fast path:

        - a declared tenancy of 1 — the tree is this session's, evidence
          attributable, "" ;
        - a declared tenancy above 1 — co-tenants, so the reading names no
          session in particular;
        - a probe that cannot answer (it raised, or returned a non-count) —
          ABSENT evidence, which is not a declaration of exclusivity. An
          exception here means the runtime is being torn down or swapped under
          the probe, which is the least safe moment to authorise an immediate
          cancel, so it reads the same as sharing.

        No probe at all is the caller declaring nothing, which keeps the module's
        historical 1:1 reading — see the ``tenancy`` note in ``__init__``.
        """
        if self._tenancy is None:
            return ""
        try:
            count = self._tenancy()
        except Exception:
            logger.debug("liveness: tenancy probe failed", exc_info=True)
            return "tenancy unreadable"
        if not isinstance(count, int) or count < 1:
            return "tenancy unreadable"
        if count == 1:
            return ""
        return f"{count} sessions on this runtime"

    def _portable_model_wait(self, runtime_pid: int) -> tuple[str, str]:
        """Model-wait verdict from platform-neutral evidence.

        Reached only when the ``/proc`` walk read no counter whatsoever, i.e. on
        a host that has no procfs. It can only ever FORGIVE silence: a CPU delta
        on a live pid reads WORKING, and anything else keeps the UNKNOWN such a
        host already returned, so the caller's conservative branch is untouched
        wherever the portable probe cannot attest either. It deliberately never
        answers DEAD — "no counter here" is not proof of death, and the caller
        reaps on UNKNOWN anyway, so claiming it would only add a way to be wrong.

        Both halves are load-bearing. The alive check alone would forgive a
        finished-but-lost-frame backend forever (a wedged process is alive too),
        trading a 90s truncation for a full-prompt-timeout hang; the CPU delta
        alone would attest to the work of a RECYCLED pid.
        """
        moved, evidence = self._portable_movement(runtime_pid)
        if moved and platform_compat.pid_exists(runtime_pid):
            return VERDICT_WORKING, f"backend activity ({evidence}, pid alive)"
        return VERDICT_UNKNOWN, evidence

    def _any_established(self, runtime_pid: int) -> bool:
        for pid in iter_descendants(self._proc, runtime_pid):
            held = socket_inodes(self._proc, pid)
            if not held:
                continue
            if held & established_inodes(self._proc, pid):
                return True
        return False

    # ── Movement sampling ──

    def _tree_movement(self, root_pid: int, key_prefix: str = "") -> tuple[bool, str]:
        """(moved, evidence) for CPU+IO deltas of *root_pid*'s subtree.

        Deltas are computed against the previous sample when it is at least
        ``sample_min_secs`` old; the first call stores a baseline and reports
        ``(False, "sampling")``. Counters can only shrink when processes exit,
        which itself is movement — negative deltas count as moved.
        """
        if self._darwin is not None:
            return self._darwin_tree_movement(self._darwin, root_pid, key_prefix)
        io_total = 0
        cpu_total = 0
        io_seen = False
        for pid in iter_descendants(self._proc, root_pid):
            io = read_io_bytes(self._proc, pid)
            if io is not None:
                io_total += io
                io_seen = True
            stat = read_pid_stat(self._proc, pid)
            if stat is not None:
                cpu_total += stat[2]
        if not io_seen and cpu_total == 0:
            return False, "no readable counters"
        now = self._now()
        io_key = f"{key_prefix}:io" if key_prefix else "io"
        cpu_key = f"{key_prefix}:cpu" if key_prefix else "cpu"
        prev_io = self._samples.get(io_key)
        prev_cpu = self._samples.get(cpu_key)
        if prev_io is None or prev_cpu is None:
            self._samples[io_key] = (now, io_total)
            self._samples[cpu_key] = (now, cpu_total)
            return False, EVIDENCE_SAMPLING
        if now - prev_io[0] < self._sample_min_secs:
            # Too soon for a fresh delta — report against the stored baseline
            # without advancing it.
            return (io_total != prev_io[1] or cpu_total != prev_cpu[1]), (
                f"io {io_total - prev_io[1]:+d}B cpu {cpu_total - prev_cpu[1]:+d}t (early)"
            )
        io_delta = io_total - prev_io[1]
        cpu_delta = cpu_total - prev_cpu[1]
        self._samples[io_key] = (now, io_total)
        self._samples[cpu_key] = (now, cpu_total)
        return (io_delta != 0 or cpu_delta != 0), f"io {io_delta:+d}B cpu {cpu_delta:+d}t"

    def _darwin_tree_movement(
        self, backend: DarwinProcessBackend, root_pid: int, key_prefix: str
    ) -> tuple[bool, str]:
        """(moved, evidence) for the CPU delta of *root_pid*'s subtree via *backend*.

        The ``/proc`` walk's two-sample contract, CPU-only: libproc exposes no
        per-process byte counter, so an IO-bound but CPU-idle subtree (a long
        ``recv`` on a remote call) reads flat here where Linux would see bytes.
        The evidence names that limit. A tree that cannot be enumerated reads as
        "no readable counters" — absent evidence, same as an unreadable procfs.
        """
        descendants = backend.descendants(root_pid)
        if descendants is None:
            return False, "no readable counters"
        cpu_total = 0
        cpu_seen = False
        for pid in (root_pid, *descendants):
            cpu = backend.cpu_nanos(pid)
            if cpu is not None:
                cpu_total += cpu
                cpu_seen = True
        if not cpu_seen:
            return False, "no readable counters"
        now = self._now()
        key = f"{key_prefix}:{_DARWIN_CPU_KEY}" if key_prefix else _DARWIN_CPU_KEY
        prev = self._samples.get(key)
        if prev is None:
            self._samples[key] = (now, cpu_total)
            return False, EVIDENCE_SAMPLING
        cpu_delta = cpu_total - prev[1]
        if now - prev[0] < self._sample_min_secs:
            # Too soon for a fresh delta — report against the stored baseline
            # without advancing it.
            return cpu_delta != 0, f"cpu {cpu_delta:+d}ns ({_DARWIN_CPU_LABEL}, early)"
        self._samples[key] = (now, cpu_total)
        return cpu_delta != 0, f"cpu {cpu_delta:+d}ns ({_DARWIN_CPU_LABEL})"

    def _portable_movement(self, root_pid: int) -> tuple[bool, str]:
        """(moved, evidence) for *root_pid*'s CPU delta, without ``/proc``.

        Same two-sample contract as :meth:`_tree_movement` — the first call after
        a boundary stores a baseline and reports ``(False, "sampling")``, and a
        second call inside ``sample_min_secs`` compares against that baseline
        without advancing it — but it reads ONE portable counter
        (:func:`platform_compat.proc_cpu_nanos_for_pid`) instead of walking
        procfs, so it answers where that walk is blind.

        Root pid only, so a busy DESCENDANT under an idle root reads flat here.
        That is the conservative direction (it never invents movement), and the
        case where the work lives in a child — an open tool call — is governed by
        the caller's tool-stall policy rather than by this probe. On macOS the
        darwin backend's subtree probe answers first; this one is reached there
        only when that backend could not enumerate the tree.
        """
        cpu = platform_compat.proc_cpu_nanos_for_pid(root_pid)
        if cpu is None:
            return False, "no readable counters"
        now = self._now()
        prev = self._samples.get(_PORTABLE_CPU_KEY)
        if prev is None:
            self._samples[_PORTABLE_CPU_KEY] = (now, cpu)
            return False, EVIDENCE_SAMPLING
        if now - prev[0] < self._sample_min_secs:
            return cpu != prev[1], f"cpu {cpu - prev[1]:+d}ns (early)"
        self._samples[_PORTABLE_CPU_KEY] = (now, cpu)
        return cpu != prev[1], f"cpu {cpu - prev[1]:+d}ns"


# ── Offloaded-consult guard (shared by AcpClient and AcpSessionHandle) ──

# Upper bound on one awaited oracle consult. A /proc walk wedged on a stuck fd
# does not stop when this expires — the shield below detaches the awaiter and
# the guard answers "prior consult still in flight" until the worker finishes.
OFFLOADED_CONSULT_TIMEOUT_SECS = 10.0


def _consume_future_exception(future: asyncio.Future[tuple[str, str]]) -> None:
    """Retrieve a liveness consult's exception so asyncio does not report it.

    The /proc walk keeps running after its awaiter goes away, so it can finish
    with an exception nobody reads. ``Future.__del__`` reports that through the
    loop exception handler, which the gateway records as an unhandled-asyncio
    crash for what is an ordinary probe failure.
    """
    if not future.cancelled():
        future.exception()


class ConsultFutureHolder(Protocol):
    """The one field :func:`consult_offloaded` needs on its caller.

    Both consumers (``AcpClient`` and ``AcpSessionHandle``) track their single
    outstanding oracle walk in an attribute of this exact shape; the guard
    reads and writes it through the holder so the handle stays where each
    caller's liveness-state boundary (turn start, ``_reset_state`` /
    ``_retire_liveness_state``) can retire it.
    """

    _consult_future: asyncio.Future[tuple[str, str]] | None


async def consult_offloaded(
    holder: ConsultFutureHolder,
    call: Callable[..., tuple[str, str]],
    args: tuple[Any, ...],
    *,
    executor_factory: Callable[[], Executor],
    log_label: str = "liveness consult",
) -> tuple[str, str]:
    """One guarded oracle consult, offloaded off the event loop. Never raises.

    The oracle's evidence gathering is a synchronous /proc filesystem walk that
    can block on a wedged fd, so ``call`` runs on ``executor_factory()`` under a
    bounded ``wait_for``. This helper owns the full non-obvious sequence both
    watchdog paths depend on, so a fix to it lands at both call sites at once:

    - **One outstanding walk per holder.** A timed-out await does not stop its
      executor thread, so the submitted future is tracked on the holder and any
      poll that finds it unfinished answers UNKNOWN without submitting again —
      otherwise a permanently wedged /proc read grows a new blocked worker per
      tick and starves the shared pool teardown also draws from. The holder's
      liveness-state boundary retires the handle so a walk abandoned by one
      generation never gates the next.
    - **Submission stays inside the guard.** The callers are silent-read polls
      and watchdog ticks, so a refused executor job (shut down during teardown,
      thread creation refused under load) must read as UNKNOWN rather than
      abort the live turn.
    - **Exception retrieval rides a callback attached at SUBMISSION**, not only
      an ``except`` arm: a turn that ends on this verdict returns with the walk
      still running and may never look again, and ``CancelledError`` is a
      ``BaseException`` an ``except Exception`` arm would miss. ``wait_for``
      cancels the shield's outer future and shield detaches its inner-done
      callback in exactly that case, so the pre-submission consume below
      additionally covers an already-completed prior that never went through
      the callback. Retrieval is not destructive — the await still sees the
      result.
    - **Any failure degrades to UNKNOWN**, never to a raise: the callers fail
      toward their own timeout policy (reaping at the cutoff), never toward
      hanging or killing on a probe error.

    ``executor_factory`` is passed by the caller (not resolved here) so each
    call site keeps its own module-level ``subprocess_executor`` binding — the
    seam its tests patch.
    """
    prior = holder._consult_future
    if prior is not None:
        if not prior.done():
            return VERDICT_UNKNOWN, "prior consult still in flight"
        _consume_future_exception(prior)

    try:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(executor_factory(), call, *args)
        future.add_done_callback(_consume_future_exception)
        holder._consult_future = future
        return await asyncio.wait_for(
            asyncio.shield(future), timeout=OFFLOADED_CONSULT_TIMEOUT_SECS
        )
    except Exception:
        logger.debug("%s failed/timed out", log_label, exc_info=True)
        return VERDICT_UNKNOWN, "oracle offload error"
