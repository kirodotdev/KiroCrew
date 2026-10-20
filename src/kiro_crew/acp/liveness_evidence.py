"""The liveness oracle's vocabulary: its verdicts, the evidence tags that qualify them,
and the contract tunables its probes read.

Each tag's comment says what the caller does when a verdict carries it. A new verdict,
evidence tag or probe tunable goes in this module; :mod:`kiro_crew.acp.liveness`
re-exports every name here, so callers keep importing them from there.
"""

from __future__ import annotations

import re

# ── Verdicts ──

VERDICT_WORKING = "working"
VERDICT_DEAD = "dead"
VERDICT_UNKNOWN = "unknown"
VERDICT_STUCK_INPUT = "stuck_input"

# Evidence prefix for the model-wait "established backend socket but flat
# counters" shape. The caller extends the UNKNOWN probe window for this tag
# (probably a non-streamed server-side think) instead of probing at the
# ordinary stale window.
EVIDENCE_ESTABLISHED_FLAT = "established_flat"

# Evidence prefix for a shell tool in flight whose command CANNOT be running:
# the runtime's tree is readable, holds live descendants, and not one of them is
# young enough to have been started by this dispatch. The caller narrows the
# UNKNOWN window on this tag (see the shell branch of
# ``session_handle_dispatch._dispatch_events``) instead of spending the build-scale
# suspect window on a command that already exited — the sub-second shell tool
# whose result frame was lost is never observed alive, so the plain
# "no matching shell child" evidence must not buy it the full forbearance.
#
# Deliberately NOT a DEAD verdict: absence is inferred from process start times,
# and a live command that exec'd into something the cmdline heuristic misses is
# still covered by the oracle's "young descendant exists" test, so the narrowing
# only shortens a non-lethal cancel, never skips straight to one.
EVIDENCE_SHELL_CHILD_ABSENT = "shell_child_absent"

# Evidence prefix for a verdict the oracle declined to sharpen into DEAD because
# the runtime hosts MORE THAN ONE session, so the evidence for it is not
# attributable to the session that asked.
#
# The model-wait wedge signature (flat counters, no established backend socket)
# is read over the runtime's WHOLE process tree. On a runtime with one tenant
# that tree IS that session, so the signature names it. With two tenants the
# same reading names neither: it says "nothing on this process is talking to a
# backend", and which tenant lost its frame — or whether any did — is a question
# the counters cannot answer. DEAD is the one verdict the caller acts on
# immediately (``session_handle`` cancels the turn on the spot instead of at the
# stale window), so an unattributable DEAD spends another session's in-flight
# turn to recover a guess.
#
# The same reasoning already sits at the CONSUMER in
# ``subagent_manager.monitoring``, which drops a DEAD outright while
# ``_session_sharing`` is set. Putting it in the oracle is what lets the other
# consumers inherit it: the verdict is unsound at the point the evidence is
# gathered, not at each place it is read.
#
# Degrades to UNKNOWN rather than WORKING: a shared tree is a reason not to
# trust the fast path, never a reason to defer indefinitely. The caller's
# ordinary stale window still bounds it, so a genuine wedge is still recovered —
# at the window instead of at once.
EVIDENCE_SHARED_TREE = "shared_tree"

# Evidence for a movement probe that stored a BASELINE and has nothing to
# compare it against yet. Structurally non-informative: it says "ask me again",
# never "this process is idle". A caller whose non-WORKING branch destroys work
# must therefore defer on it rather than treat it as a negative verdict — see
# ``client._prompt_loop``'s stale-turn gate, which reaped a live turn on its
# first silent read because the priming answer landed past the cutoff.
EVIDENCE_SAMPLING = "sampling"

# Evidence prefix for a verdict the oracle could NOT sharpen because the
# platform does not expose the evidence that would sharpen it: a live shell
# child whose subtree is flat on darwin (libproc has no ``wchan`` / blocked-fd
# view, so stdin-blocked and merely quiet are indistinguishable), or a shell /
# MCP tool on a host with neither procfs nor a tree backend (Windows). The
# verdict is UNKNOWN — never WORKING, because "the process is alive" is not
# sufficient for an indefinite deferral, and never DEAD or STUCK_INPUT,
# because the evidence for those is exactly what is absent. The caller keeps
# the STANDARD no-progress budget on this tag (build-scale forbearance), and
# narrows only when the dispatched command was classified interactive-risk
# (see :func:`classify_interactive_command`), because that is the one case the
# missing evidence would have answered. A tag, not a verdict: the declared
# degradation must be visible in the evidence, the metric bucket and the spec —
# a silent plain UNKNOWN would hide which platform limit produced it.
EVIDENCE_PLATFORM_LIMITED = "platform_limited"

# Evidence prefix for an opaque MCP tool whose subtree is genuinely flat (a
# real two-sample delta, not the baseline tick) while the in-flight tool's OWN
# MCP server -- the process launched as the server named in the call's trusted
# ``_meta.kiro.mcpServerName``, or a process below it -- holds an established
# TCP connection to a non-loopback peer. That is the shape of a tool blocked on
# its own remote call: the MCP server sits in ``recv`` with zero CPU and zero
# bytes,
# which is also exactly what a remote call with no client timeout looks like
# when the peer never answers. The caller narrows the UNKNOWN window on this
# tag to ``watchdog.remote_flat_probe_secs``, and only once the tree has also
# shown no movement at any probe for that long, so a slow stream that moves
# bytes now and then is never cut off between two quiet samples.
#
# Deliberately distinct from :data:`EVIDENCE_ESTABLISHED_FLAT`, which is keyed
# on kiro-cli's OWN backend connection and a model-wrapping tool name. Only the
# named server's subtree is read, so another server's persistent connection (a
# keepalive, ``mcp-remote``) or a shell tool's background tunnel cannot tag a
# quiet tool of a different server, and kiro-cli's model connection never
# counts. The server is found from how the session launched it (see
# :func:`launch_matches`); a call with no trusted server name, a server the
# tree does not show exactly once, a host with no socket view (Windows) or a
# tree that cannot be read never sets the tag, and the build-scale window holds.
EVIDENCE_REMOTE_FLAT = "remote_flat"

# Evidence prefix for an opaque MCP tool whose runtime subtree moved. It is the
# weakest WORKING there is: the movement may belong to any process in the tree,
# such as a sibling MCP server polling in the background, so it cannot tell a
# live tool from one whose result frame was lost. The tool branch therefore
# bounds this one WORKING reading (see ``session_handle_dispatch._dispatch_events``)
# while every other WORKING keeps deferring.
EVIDENCE_MCP_SUBTREE_ACTIVE = "mcp subtree active"

# kirocrew-core tools that ping ``/api/session-keepalive`` for their whole run
# and can read ``mcp subtree active``: ``mcp_tools/spawn_parts/wave.py``
# ``spawn_sub_agents``, bounded by its own collection timeout, so the tool
# branch never cuts it off on the opaque-MCP bound. The core ``wait`` pings too
# but never reaches that reading (``wait_tool_verdict`` answers first), so it
# is not listed. Matched on the adapter identity, never on the title.
_KEEPALIVE_TOOL_NAMES: frozenset[str] = frozenset({"spawn_sub_agents"})

# Tool names that are known to wrap a model call (e.g. kiro-cli's use_subagent
# which starts a sub-agent turn inside the current tool call). The
# established_flat narrowing is ONLY applied when the in-flight tool's trusted
# ``tool_name`` (from ``_meta.kiro.toolName``) is in this set. Without
# positive attribution the socket may be a persistent keepalive for an
# unrelated backend connection (e.g. model-service keep-alive) while an
# ordinary quiet MCP tool is running — narrowing the build-scale suspect window to 15
# minutes in that case would incorrectly penalise long-running ordinary tools.
#
# COUPLING: these are kiro-cli TOOL NAMES, matched verbatim against the ACP
# tool-call frame. If kiro-cli renames use_subagent or ships another
# model-wrapping tool, narrowing silently stops applying and those stalls
# fall back to the build-scale window (fail-safe direction: too patient,
# never a surprise cancel — but the fast-detection this list exists for is
# lost). No handshake advertises the model-wrapping property today, so a
# name list is the only attribution available; update it alongside any
# kiro-cli subagent-tool surface change.
_MODEL_WRAPPING_TOOLS: frozenset[str] = frozenset({"use_subagent"})

# ── Tunables (contract constants, not config — config governs the caller) ──

# A tracked shell child that exited gets this long for its tool result frame to
# arrive on the session queue before the verdict flips to DEAD.
CHILD_EXIT_GRACE_SECS = 15.0
# Declared-duration slack for the MCP wait tool: WORKING until seconds + this.
WAIT_TOOL_SLACK_SECS = 120.0
# No declared duration vouches for a session past ``WAIT_TOOL_MAX_SECS``
# (imported from ``constants``), the one bound the wait tool's schema and handler
# both clamp to.
# The wait tool's name on ``CORE_MCP_SERVER``, as the adapter identity reports it.
_WAIT_TOOL_NAME = "wait"
# Separator in a server-qualified MCP tool name: kiro-cli may report
# ``<server>___<tool>`` and the canonical prefix form is ``mcp__<server>__<tool>``.
# Mirrors ``session_directive._MCP_SEPARATOR_RE``: a run of two or more
# underscores, so a single-underscore name such as ``do_wait`` stays distinct.
_MCP_SEPARATOR_RE = re.compile(r"_{2,}")
# Minimum cmdline fragment length for a definite shell-child match.
_MIN_MATCH_FRAGMENT = 8
# How much a process may predate its tool's dispatch and still count as started
# BY that dispatch. Both timestamps come from the boot clock (see
# :func:`boottime_now`), so this covers only the dispatch-to-spawn gap, not clock
# skew.
_DISPATCH_START_TOLERANCE_SECS = 10.0
# wchan values that indicate a process blocked reading interactive input.
_STUCK_INPUT_WCHANS = frozenset({"n_tty_read", "pipe_read", "wait_woken"})
# read(2) syscall numbers: x86_64 = 0, aarch64 = 63.
_READ_SYSCALL_NRS = frozenset({0, 63})
# Shell wrappers whose bare program name is too generic to count as a match.
_GENERIC_PROGRAMS = frozenset({"bash", "sh", "zsh", "env", "sudo", "nohup", "timeout"})

# Sample key for the portable (no-procfs) CPU probe. Deliberately not one of the
# ``/proc`` walk's keys: that counter is jiffies and this one is nanoseconds, so
# a host whose procfs became readable mid-turn must not diff one against the
# other.
_PORTABLE_CPU_KEY = "portable_cpu"

_WAIT_SECONDS_RE = re.compile(r"[\"']?seconds[\"']?\s*[:=]\s*(\d+)")
