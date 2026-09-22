"""Process tracking and orphan cleanup for kiro-cli sessions.

Manages PID files (``kiro_pids.txt`` and ``kiro_session_pids.txt``) that
track spawned kiro-cli processes.  Provides startup cleanup, periodic
sweeping, and per-process track/untrack operations.

See ``session.py`` module docstring for the full Process Sweep Architecture.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.agent_sdk.backends import agent_process_markers, node_adapter_entry_relpaths
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import (
    KIROCREW_SPAWN_INSTANCE_ENV,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS

logger = logging.getLogger(__name__)

_PID_FILE = "kiro_pids.txt"
_SESSION_PID_FILE = "kiro_session_pids.txt"

# ── Orphan-sweep spawn grace period ──────────────────────────────────────────
# A freshly spawned kiro-cli PID is tracked in kiro_session_pids.txt immediately
# by _track_session_pid(), but the _starting_pids protection set is only
# populated AFTER provider.start() returns (multi-second window). During this
# window the sweep may classify the PID as orphaned and SIGKILL it. To prevent
# this, any tracked PID younger than SWEEP_SPAWN_GRACE_SECONDS is unconditionally
# skipped (left alive) in _sweep_pid_entries. A missed kill self-heals next
# cycle; a wrong kill does not.
SWEEP_SPAWN_GRACE_SECONDS = 120


def _pid_age_seconds(pid: int, proc_root: str = "/proc") -> float | None:
    """Return the process age in seconds, or None if it cannot be determined.

    On Linux, reads /proc/<pid>/stat field 22 (starttime in clock ticks since
    boot). The comm field (field 2) can contain spaces and parentheses — split
    on the substring AFTER the LAST ')' in the line.

    On macOS (and other POSIX without /proc): derived from
    ``platform_compat.get_process_start_id``, whose darwin value is the process
    start time in epoch ``seconds.microseconds`` — so this needs no
    ``subprocess`` and is safe on the event loop. Empirically required: the
    startup sweep SIGKILL'd a live kiro-cli off a stale dead-gateway entry on
    macOS because the grace window silently did not apply there.

    On Windows: returns None (no grace — sweep behavior unchanged there).

    The *proc_root* parameter allows injection of a fake /proc tree for testing.
    """
    if platform_compat.IS_WINDOWS:
        return None
    if sys.platform != "linux":
        start_id = platform_compat.get_process_start_id(pid)
        if start_id is None:
            return None
        try:
            return max(0.0, time.time() - float(start_id))
        except ValueError:
            return None
    try:
        stat_data = Path(f"{proc_root}/{pid}/stat").read_text()
        # Field 22 is starttime. Fields before it: pid (1), comm (2, in parens,
        # may contain spaces), state (3), ... The reliable parse is to find the
        # LAST ')' — everything after is space-separated fields starting at
        # field 3 (state).
        close_paren = stat_data.rfind(")")
        if close_paren < 0:
            return None
        fields_after_comm = stat_data[close_paren + 2 :].split()
        # starttime is field 22 overall. After comm (field 2), state is field 3
        # which is index 0 of fields_after_comm. So field 22 = index 19.
        starttime_ticks = int(fields_after_comm[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime = float(Path(f"{proc_root}/uptime").read_text().split()[0])
        now = time.time()
        boot_time = now - uptime
        start_seconds = boot_time + (starttime_ticks / clk_tck)
        return now - start_seconds
    except (OSError, ValueError, IndexError):
        return None


def _pid_in_spawn_grace(pid: int) -> bool:
    """Return True if the PID is within the spawn grace period and should be skipped.

    - Windows: returns False (no age source — fall through to existing kill
      behavior so the sweep remains functional there).
    - POSIX (Linux via /proc, macOS via ``ps -o etime=``) + successful age
      read: True if age < SWEEP_SPAWN_GRACE_SECONDS.
    - POSIX + read failure (age is None): True (treat as young — safe
      direction; dead processes are already pruned by the earlier liveness check).
    """
    if platform_compat.IS_WINDOWS:
        return False
    age = _pid_age_seconds(pid)
    if age is None:
        return True  # cannot determine age → treat as young (safe direction)
    return age < SWEEP_SPAWN_GRACE_SECONDS


def _pid_start_token(pid: int) -> str | None:
    """Stable, persistable identity token for a live PID (PID-recycle guard).

    A thin delegate to ``platform_compat.get_process_start_id``, which is
    in-process on every platform (``/proc`` read on Linux, ``libproc`` ctypes on
    macOS) — deliberately NOT ``ps``, so the token lookup itself is non-blocking
    and safe to call from the asyncio event loop. (Whether an enclosing tracker
    may run on the loop is governed by that tracker's exclusive file lock, not
    by this lookup — see ``AUTOSDE: no-blocking-call-on-event-loop``.)

    Returns ``None`` when identity cannot be determined, meaning a process we may
    not introspect or a read that failed. Every platform Crew supports HAS a
    source — ``/proc`` on Linux, ``libproc`` on macOS, the creation FILETIME on
    Windows — so ``None`` is a failed read rather than an unsupported host.
    Callers MUST treat ``None`` as "unknown", never as a mismatch — see the sweep
    call sites.

    Note this cannot reuse ``acp.client._get_start_time``: that hashes with
    builtin ``hash()``, which is PYTHONHASHSEED-randomized per interpreter and
    therefore meaningless once written to disk and compared by a later gateway.

    SUBTRACTIVE ONLY. A live value that differs from the recorded one proves the PID
    was recycled and prunes the entry without a signal; a value that MATCHES never
    authorizes one, because the tracking file is same-uid-writable and this token is
    readable from ``/proc`` for any introspectable PID. What authorizes a kill is the
    argv test plus each arm's own reparent or gateway-liveness condition.

    That rule is also why the Linux value needs no boot scope here: it counts
    ``/proc`` start ticks from BOOT, so a post-reboot PID can repeat an earlier
    boot's pair — which under subtractive use costs a missed prune, not a wrong kill.
    ``platform_compat._own_identity_token`` is the reboot-unique form, should a
    future change want one.
    """
    return platform_compat.get_process_start_id(pid)


def _pid_file_path() -> Path:
    return config_dir() / _PID_FILE


def _session_pid_file_path() -> Path:
    return config_dir() / _SESSION_PID_FILE


@contextmanager
def _session_pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for session PID file operations."""
    lock_path = _session_pid_file_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open non-truncating; see ``platform_compat.open_lock_file`` for why ``"w"``
    # loses the lock on Windows (GH-9248). The helper does the create-or-open in
    # one syscall; the parent mkdir above stays because it does not.
    with platform_compat.open_lock_file(lock_path) as lock_fd:
        with platform_compat.file_lock(lock_fd, exclusive=True):
            yield


def _track_session_pid(pid: int) -> None:
    """Append a kiro-cli PID to the session tracking file (dedup).

    Entries are written as ``<gateway_pid>:<child_pid>:<start_token>`` so each
    gateway instance can identify and sweep only its own children, and so the
    sweep can verify the PID still names the SAME process before killing
    (PID-recycle guard — see ``_pid_start_token``). A token is available on every
    platform Crew supports, Windows included, so the legacy
    ``<gateway_pid>:<child_pid>`` form is written only when the probe itself
    fails; the sweep then rests on the argv gate and the spawn grace alone. That gate
    recognises every registered harness (``_MANAGED_AGENT_BASENAMES``, projected from
    the backend registry) wherever a command line is readable, and on Windows only an
    image name is -- so an interpreter-hosted adapter reads as ``node.exe`` there and a
    token-less entry for one is not recognised. See :func:`_is_managed_agent_process`.
    """
    token = _pid_start_token(pid)
    prefix = f"{os.getpid()}:{pid}"
    entry = f"{prefix}:{token}" if token else prefix
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # Dedup on the gw:pid prefix (not the full entry) so a re-track
            # never duplicates a legacy 2-field line with a 3-field one.
            for line in path.read_text(encoding="utf-8").split():
                if line == prefix or line.startswith(prefix + ":"):
                    return
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{entry}\n")


@contextmanager
def _pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for all PID file read-modify-write operations."""
    lock_path = _pid_file_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open non-truncating; see ``platform_compat.open_lock_file`` for why ``"w"``
    # loses the lock on Windows (GH-9248). The helper does the create-or-open in
    # one syscall; the parent mkdir above stays because it does not.
    with platform_compat.open_lock_file(lock_path) as lock_fd:
        with platform_compat.file_lock(lock_fd, exclusive=True):
            yield


def _rewrite_pid_file(path: Path, content: str) -> bool:
    """Replace *path*'s content atomically; log and return ``False`` on failure.

    Atomic (temp file + rename) because a plain ``write_text`` truncates the
    file to zero BEFORE writing the kept entries: a failure inside that window
    leaves a SHORT file whose surviving content is perfectly well-formed, and
    every dropped entry is an agent runtime no reaper can find again.

    A failure here is REPORTED, not propagated. Pruning an entry is idempotent
    and self-retrying: the next sweep — or the next gateway start — re-reads the
    file, finds that PID already dead, and prunes it again, so a failed rewrite
    costs one stale line rather than a runtime. Propagating would be worse than
    the problem: ``cleanup_orphaned_sessions`` runs unguarded on the gateway's
    startup path, and on Windows ``replace_with_retry`` deliberately declines to
    retry a sharing violation while an event loop is running (an indexer or AV
    scanner holding the temp file is enough), so an escaping error there aborts
    startup entirely.

    The tracking direction is the opposite case and must NOT be quieted this
    way: failing to RECORD a freshly spawned PID is unrecoverable, because no
    reaper can identify that runtime afterwards.
    """
    try:
        atomic_write(path, content)
        return True
    except OSError:
        logger.error(
            "Could not rewrite PID file %s; its entries stay until the next sweep",
            path,
            exc_info=True,
        )
        return False


# Basenames of agent runtimes whose lifecycle Kiro Crew manages through PID-file
# tracking (kiro_pids.txt / kiro_session_pids.txt). Used to re-validate tracked
# PIDs before a kill, and as a NEGATIVE gate in the work-orphan sweep: these
# runtimes are reclaimed by their own tracked-PID sweep, never by the
# marker-based work sweep (see _is_sweepable_orphan_work).
#
# PROJECTED from the backend registry rather than spelled here. A hand-written
# pair ("kiro-cli", "claude") answered for two of the eight harnesses Crew
# spawns, so a dead gateway's codex-acp, opencode, pi-acp, goose or dsh orphan
# answered "not ours" — and the reclaim's branch for an unrecognised PID both
# SPARES the process and DROPS its tracking entry, which is the one file every
# sweep mechanism keys off to find it. The orphan was spared and forgotten.
#
# ``agent_process_markers`` is the one place a harness's process name is written,
# beside the launch table three of them already read, and a ratchet in
# ``test_pid_lifecycle`` fails when a registered backend has no name there. So a
# harness added later is one row in that table, not an edit here.
#
# ``kiro_crew.agent_sdk.backends`` is a stdlib-only leaf, which is what makes this
# import safe: ``check_agent_sdk_boundary`` forbids ``kiro_crew.acp`` and
# ``kiro_crew.providers`` from this module, and ``test_agent_lifecycle_cycle`` pins
# their absence. The adapters' own basename constants live with their resolvers in
# the ACP layer; a test asserts the two agree rather than this module reaching for
# them.
#
# Windows is NOT fixed by widening this set: ``process_matches`` reads only the
# image name there, so a Node-hosted adapter is ``node.exe`` whatever names this
# holds. That is tracked separately, and the reclaim leaves such an entry to its
# other guards rather than pretending to recognise it.
#: The Claude CLI's own binary, which is not any backend's primary launch name.
#: Projecting only the primary names would stop recognising a process this file
#: recognises today — the same leak in the other direction, and
#: ``test_claude_runtime_basename_also_detected`` pins it.
#:
#: ``kiro-cli-chat`` is deliberately NOT here: this tuple is matched as a SUBSTRING,
#: so every cmdline it would match already matches the projected ``kiro-cli``. It
#: appears in the exact-match set below, where it does carry coverage.
_LEGACY_AGENT_MARKERS: tuple[str, ...] = ("claude",)

#: Matched as a SUBSTRING of a whole command line, which is what
#: ``platform_compat.process_matches`` does. Substring is the right subject there: an
#: adapter hosted by an interpreter appears as ``node /path/to/codex-acp``, and only a
#: substring test finds the adapter in it.
_MANAGED_AGENT_MARKERS: tuple[str, ...] = tuple(
    sorted(set(agent_process_markers()) | set(_LEGACY_AGENT_MARKERS))
)

#: Matched as an EXACT argv0 basename, for the two consumers whose subject is a
#: basename rather than a command line: the work sweep's negative gate
#: (:func:`_is_sweepable_orphan_work`) and the untracked-runtime report
#: (:func:`_is_untracked_managed_agent_orphan`).
#:
#: Exact, because substring over a basename over-matches on the short generic names
#: the projection introduced — ``mongoose`` contains ``goose``, and a basename holding
#: ``dsh`` is not ``dsh``. On the negative gate an over-match wrongly EXCLUDES a
#: process from the work sweep; on the report it names a process that is not a harness.
#: Neither is a kill (the report says so in as many words), and both are wrong.
#:
#: ``kiro-cli-chat`` belongs here and not above: as an exact basename it is not
#: covered by ``kiro-cli``.
_MANAGED_AGENT_BASENAMES: frozenset[bytes] = frozenset(
    name.encode() for name in {*agent_process_markers(), "claude", "kiro-cli-chat"}
)


def _basename_of(token: bytes) -> bytes:
    """The last path segment of an argv token, for both separators.

    Windows records ``C:\\Program Files\\nodejs\\node.exe``, so splitting on ``/``
    alone would carry a whole backslashed path into an exact-name test and never match.
    """
    return token.rsplit(b"/", 1)[-1].rsplit(b"\\", 1)[-1]


def _basename_names_a_harness(basename: bytes) -> bool:
    """True when *basename* IS a harness process name, exactly.

    Trimmed at the first control byte before comparing. argv0 is set by the process
    itself, so it is untrusted, and a real basename never contains one — while a
    hostile argv0 carrying a newline is precisely the case the report's escaping
    exists for, and it must still reach it. Trimming grants nothing: a process that
    can name itself ``kiro-cli\nfoo`` can name itself ``kiro-cli``.
    """
    for index, byte in enumerate(basename):
        if byte < 0x20:
            basename = basename[:index]
            break
    return basename in _MANAGED_AGENT_BASENAMES


# Which harnesses the scope reaper may anchor on, SELECTED out of
# _MANAGED_AGENT_BASENAMES rather than spelled again. Both sets test an exact argv0
# basename, so a name written twice is a name that can drift; selecting keeps one
# spelling and leaves the two free to disagree about the thing they SHOULD disagree
# about, which is authority.
#
# Narrower on purpose, and the reason is the difference between the two questions.
# _MANAGED_AGENT_BASENAMES answers "is this a harness process" for a negative sweep gate
# and a report, neither of which terminates anything. This set authorizes an
# abandoned-scope reclaim to KILL, and widening a kill path to five more harnesses is
# its own change with its own review, so the projection is left here to reuse rather
# than consumed by a set that grew on speculation. Until then a codex-acp, opencode, pi-acp, goose or dsh tree in an
# abandoned scope is not anchored, and that gap is recorded rather than quietly closed
# by a set that happened to grow.
#
# The selection is checked: ``test_the_scope_anchor_is_a_subset_of_the_projection``
# fails if a name here stops appearing in the projection, so a registry rename cannot
# silently empty this set and disarm the reaper.
_SCOPE_REAP_ANCHOR_NAMES: frozenset[str] = frozenset(
    {"claude", "claude-agent-acp", "kiro-cli", "kiro-cli-chat"}
)
_MANAGED_AGENT_RUNTIME_BASENAMES: frozenset[bytes] = frozenset(
    name for name in _MANAGED_AGENT_BASENAMES if name.decode() in _SCOPE_REAP_ANCHOR_NAMES
)


# Interpreters that RUN a harness rather than being one. Every bespoke adapter Crew
# spawns is a Node entry script with a ``#!/usr/bin/env node`` shebang, so the kernel
# execs the interpreter and ``/proc/<pid>/cmdline`` reads ``node /opt/n/bin/codex-acp``
# -- argv0 names the interpreter and the harness is in argv1. Listed so that shape can
# be recognised WITHOUT accepting a harness name anywhere in a command line.
#
# Node spellings only. No registered backend launches under any other interpreter, and
# each name here WIDENS the slot in which a harness name is accepted, so a name added
# on speculation is authority granted for a shape nothing produces. A harness that
# ships as a Python entry script adds its interpreter here with its own review.
_HARNESS_INTERPRETERS: frozenset[bytes] = frozenset({b"node", b"nodejs", b"node.exe"})


def _argv_tokens(cmdline: bytes) -> list[bytes]:
    """Split a raw command line into argv tokens, preferring the NUL boundaries.

    Linux ``/proc/<pid>/cmdline`` separates argv with NUL, which is the EXACT
    boundary: splitting such a line on whitespace instead breaks a path containing a
    space into two tokens. That is not a hypothetical -- a macOS or Linux home
    directory named ``John Smith`` turns ``/Users/John Smith/.local/bin/node`` into
    ``/Users/John`` plus ``Smith/.local/bin/node``, so argv0's basename reads ``John``,
    the interpreter is not recognised, and the adapter in the next token is never
    examined. The reclaim then treats a real harness as unmanaged, which is the leak
    this module exists to close.

    Whitespace is the fallback for a line with no NUL in it, which is what macOS
    ``ps -o command=`` returns. There the ambiguity is the platform's, not ours --
    ``ps`` joins argv with spaces and a spaced path is unrecoverable from the result.
    Same two-step as :func:`_work_orphan_basename`.
    """
    if b"\x00" in cmdline:
        return [token for token in cmdline.split(b"\x00") if token]
    return cmdline.split()


def _harness_naming_tokens(cmdline: bytes) -> list[bytes]:
    """The argv tokens whose basename is allowed to name a harness.

    Two, at most, and each chosen by POSITION rather than by content.

    - ``argv[0]``, always: the file the kernel actually execed.
    - ``argv[1]``, and only when ``argv[0]``'s basename is an interpreter from
      :data:`_HARNESS_INTERPRETERS`. That is the script slot of the shebang shape above,
      and it is the only way a bespoke adapter appears at all.

    ``argv[1]`` exactly, never "the first token that does not look like a flag". Crew
    launches a Node adapter as ``[node, <script>]`` (``_resolve_node_adapter_argv``) and
    passes no interpreter options, so the script is always at index 1 -- and scanning past
    options instead hands an option VALUE to the name test. ``--require`` and its kin take
    one, so ``node app.js --require /any/path`` would offer ``/any/path`` as the script
    slot, and a path an unrelated process merely MENTIONS would authorize a SIGKILL of it.
    Telling a value-taking option from a boolean one needs a table of Node's flags, which
    is an open set and a moving one; the position is closed and is the shape Crew produces.

    A leading ``-`` at index 1 therefore opens no slot: Crew never emits one, so that
    command line is not ours to reason about.

    What is excluded is the rest of argv. A process's arguments are chosen by whoever
    started it and say nothing about what it IS, so ``node build.js --agent goose`` or an
    editor opened on a file called ``dsh`` would otherwise be answered "this is a harness"
    -- and on the reclaim path that answer authorizes a SIGKILL of a PID this gateway never
    spawned.
    """
    tokens = _argv_tokens(cmdline)
    if not tokens:
        return []
    naming = [tokens[0]]
    if (
        _basename_of(tokens[0]) in _HARNESS_INTERPRETERS
        and len(tokens) > 1
        and not tokens[1].startswith(b"-")
    ):
        naming.append(tokens[1])
    return naming


# A Node adapter reaches ``node`` two ways: its installed bin shim
# (``node /opt/n/bin/codex-acp``), whose basename IS the adapter's name, or the package
# entry the resolver builds, whose basename is ``index.js`` and names nothing. The second
# shape therefore needs the path, and WHICH path is the whole question.
#
# The identity is the EXACT relative launch path, not a name found along it. Crew spawns a
# Node adapter from one of three published packages, and
# ``backends.node_adapter_entry_relpaths()`` is that list -- the same table the resolvers
# read to build the path, so the two cannot disagree.
#
# Matching a package DIRECTORY NAME against the harness names is what this replaces, and
# the difference is the axis. A directory name is chosen by whoever installed the package,
# so "what a process may call itself" stays open: an unrelated npm application at
# ``node /srv/goose/dist/index.js`` carries a directory named for a harness Crew never
# launches through Node at all, and the reclaim would SIGKILL it. Comparing the resolved
# relative path closes the axis, because the three paths are a set this repository owns.
#
# Segment-aligned on purpose: a trailing-substring test would accept
# ``/srv/evil-pi-acp/dist/index.js``.
_NODE_ADAPTER_ENTRY_SEGMENTS: frozenset[tuple[bytes, ...]] = frozenset(
    tuple(segment.encode("utf-8") for segment in relpath.split("/"))
    for relpath in node_adapter_entry_relpaths()
)


def _token_is_node_adapter_entry(token: bytes) -> bool:
    """True when *token* is a path Crew launches a Node-hosted adapter with.

    The token's tail must equal one of the resolved ``<package>/dist/index.js`` relative
    paths, segment for segment. Absolute prefix is free -- the package can be installed
    anywhere (a global root, a project ``node_modules``, a vendored tree) and the resolver
    walks several -- but everything from the package name down is exact.
    """
    segments = tuple(seg for seg in token.replace(b"\\", b"/").split(b"/") if seg)
    for candidate in _NODE_ADAPTER_ENTRY_SEGMENTS:
        if len(segments) >= len(candidate) and segments[-len(candidate) :] == candidate:
            return True
    return False


def _cmdline_names_a_harness(cmdline: bytes) -> bool:
    """True when a harness-naming token IS a harness process name.

    Exact per token, because this answer authorizes a signal. A raw substring test over
    the whole command line accepts any line that merely contains a needle, and the
    projected names include three-character ones: ``dsh`` sits inside ``friendship``,
    ``goose`` inside ``mongoose``. A recycled PID landing on such a process would pass
    the recycle guard and be killed.

    Which tokens may answer is :func:`_harness_naming_tokens` -- argv0, plus the script
    slot of an interpreter-hosted adapter. That slot gets two tries, and they test two
    different kinds of identity:

    - the BASENAME, exactly, for the bin-shim spelling (``node /opt/n/bin/codex-acp``);
    - the resolved ENTRY PATH, exactly, for the package spelling
      (:func:`_token_is_node_adapter_entry`), whose basename is ``index.js`` and names
      nothing.

    The resolver hands the adapter to Node either way, so recognising only the first left
    the ``dist/index.js`` launch unreclaimable. The second is a path this repository
    publishes rather than a name read out of one, which is what keeps an unrelated npm
    application from answering for a harness.
    """
    for token in _harness_naming_tokens(cmdline):
        if _basename_names_a_harness(_basename_of(token)):
            return True
        if _token_is_node_adapter_entry(token):
            return True
    return False


def _is_managed_agent_process(pid: int) -> bool:
    """Check if a PID belongs to an agent process managed by Kiro Crew (recycle guard).

    Tokenized wherever a command line can be read, which is Linux and macOS. Linux uses
    ``_pid_cmdline``'s ``/proc`` read; macOS uses ``platform_compat.process_command_line``,
    whose ``ps -o command=`` call is the same one ``process_matches`` already pays for
    there, so the precision costs nothing extra.

    Both platforms need it equally. The projected names include three-character ones and
    a raw substring test over a whole command line accepts any line containing them, so
    tightening only Linux would leave macOS strictly MORE collision-prone than the
    two-name pair this set replaced.

    WINDOWS: only an image name is readable cheaply there (a real command line means a
    WMI query per PID, and these sweep loops ask for every tracked entry), so the name is
    compared EXACTLY against the same basename set rather than passed to
    ``process_matches``, whose Windows arm is a substring test. Exactness matters in the
    same direction as the tokenizing above: the projected set has three-character names,
    and a substring test on an image name is the one place ``mongoose.exe`` could answer
    for ``goose``. It can only ever false-match there, because a true adapter's image
    name is ``node.exe`` -- which is the RESIDUAL, and why Windows reclaim needs an
    identity independent of argv rather than a better name match. Carried as a stated
    residual rather than closed here: closing it needs a per-PID identity Windows can read
    cheaply, which is its own change.
    """
    cmdline = _pid_cmdline(pid)
    if not cmdline and sys.platform == "darwin":
        cmdline = platform_compat.process_command_line(pid).encode("utf-8", "replace")
    if cmdline:
        return _cmdline_names_a_harness(cmdline)
    if platform_compat.IS_WINDOWS:
        image = platform_compat.process_image_name(pid)
        if not image:
            return False
        stem = image.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
        candidates = {stem}
        if stem.endswith(".exe"):
            candidates.add(stem[: -len(".exe")])
        return any(name.decode().lower() in candidates for name in _MANAGED_AGENT_BASENAMES)
    return platform_compat.process_matches(pid, _MANAGED_AGENT_MARKERS)


def _pid_gone_or_unmanaged(pid: int) -> bool:
    """Return ``True`` when it is safe to *untrack* ``pid`` from the PID files.

    Safe means the process is confirmed gone. Returns ``False`` when a process
    with this PID is still alive (or is unsignalable): a teardown kill may have
    failed to reap our agent (``killpg`` misses children in other process
    groups; a mid-init crash can race the descendant scan in ``_kill_process``),
    so the tracking entry is **retained**. The periodic orphan sweep — which
    re-validates ownership via ``_is_managed_agent_process`` before it kills
    anything — then reaps a genuine survivor and skips a recycled PID.
    Untracking a live survivor here would orphan it permanently, since every
    sweep mechanism keys off these files (the ``kiro-cli-chat acp`` memory-leak
    class). Fail-safe: any inconclusive result retains.

    Routes through ``platform_compat.pid_liveness`` (a non-blocking probe, safe
    on the asyncio event loop) rather than a raw ``os.kill(pid, 0)`` — on
    Windows that call TERMINATES the target. This is stricter than upstream
    ``33da30e6``, which untracks on ``PermissionError`` (assumes a recycled,
    other-user PID): ``pid_liveness`` collapses EPERM into ``PID_UNSIGNALABLE``,
    which we treat as "retain", so an unsignalable PID stays tracked for the
    sweep to re-validate off the hot path. Never orphaning a live survivor is
    the invariant that matters; a retained-but-recycled PID is harmless (the
    sweep's ownership recheck skips it). It deliberately does NOT call
    ``_is_managed_agent_process`` (which shells out to ``ps`` on macOS): that
    would block the loop and could mislabel a live-but-transiently-unreadable
    agent as unmanaged — the exact leak this guards against.
    """
    return platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD


def _collect_active_pids(sessions: "dict") -> tuple[set[int], bool]:
    """Extract PIDs from live sessions. Returns ``(pids, ok)``.

    If any session's PID is not an int or extraction fails,
    returns ``(partial_set, False)`` — caller should skip the sweep.
    """
    pids: set[int] = _protected_pids()  # shared _bg / subagent runtimes shielded from the sweep
    for sess in sessions.values():
        # ACP provider: long-lived process PID via client._pid
        client = getattr(sess.provider, "client", None)
        if client is not None:
            try:
                pid = client._pid  # type: ignore[attr-defined]
                if not isinstance(pid, int):
                    logger.warning(
                        "PID for session is not an int (%r) — skipping orphan sweep this cycle", pid
                    )
                    return pids, False
                pids.add(pid)
            except Exception:
                logger.warning("Failed to read PID for session — skipping orphan sweep this cycle")
                return pids, False
        # CC provider: protect long-lived process PID (per_session mode)
        cc_proc = getattr(sess.provider, "_proc", None)
        if cc_proc is not None and cc_proc.returncode is None:
            pids.add(cc_proc.pid)
        # CC provider: protect in-flight subprocess PID (ephemeral mode)
        active_proc = getattr(sess.provider, "_active_proc", None)
        if active_proc is not None and active_proc.returncode is None:
            pids.add(active_proc.pid)
    return pids, True


def _kill_pid_tree(pid: int) -> tuple[int, bool]:
    """Kill *pid* and its descendant agent processes (bottom-up).

    Returns ``(total_killed, root_killed)`` so callers can distinguish
    whether the root process itself was sent SIGKILL.

    The argv gate below is a PID-RECYCLE guard and the only thing that authorizes a
    signal here: it asks whether this PID still names the kind of process the
    tracking entry described. :data:`_MANAGED_AGENT_MARKERS` is projected from the
    backend registry, so it answers for every harness Crew spawns rather than for
    two of them.

    A recorded start token does NOT authorize a kill anywhere in this module, by
    design. It is subtractive evidence only — a live token that differs from the
    recorded one proves the PID was recycled, and the entry is pruned without a
    signal — because the tracking file is same-uid-writable and a token is readable
    from ``/proc`` for any introspectable PID, so a matching token is not a
    capability this file's contents may confer. That is the rule
    ``kiro_pids.txt``'s own arm follows, and ``session.md`` states it for both.
    """
    if pid <= 0:
        return 0, False
    killed = 0
    root_killed = False
    try:
        # circular import: session_pid → acp.client → session → session_pid
        from kiro_crew.acp.client import _get_child_pids

        children = _get_child_pids(pid)
        for cpid in reversed(children):
            if cpid <= 0 or not _is_managed_agent_process(cpid):
                continue
            try:
                platform_compat.kill_pid(cpid, platform_compat.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
    except Exception:
        logger.debug("Error killing children of PID %s", pid, exc_info=True)
    if not _is_managed_agent_process(pid):
        return killed, root_killed
    try:
        if platform_compat.IS_WINDOWS:
            # _get_child_pids() returns [] on Windows (no pgrep/proc), so the
            # per-child loop above is empty — the root kill MUST reap the whole
            # descendant tree here (taskkill /T), or orphaned kiro-cli MCP/node/
            # python children leak and accumulate across gateway restarts. (On
            # POSIX the children were already SIGKILL'd in the loop above and the
            # root is a single-PID kill.) kill_process_tree raises on non-zero
            # taskkill rc, same shape POSIX uses, so the except below catches
            # a genuine failure and leaves root_killed=False for the caller.
            platform_compat.kill_process_tree(pid, platform_compat.SIGKILL)
        else:
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
        killed += 1
        root_killed = True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return killed, root_killed


def _windows_pending_pid_entry(line: str) -> bool:
    """Pending exact pins prevent every PID-file sweep from retiring the record."""
    if not platform_compat.IS_WINDOWS:
        return False
    parts = line.strip().split(":")
    try:
        pid = int(parts[1] if len(parts) in (2, 3) else parts[0])
    except ValueError:
        return False
    token = parts[2] if len(parts) == 3 else None
    return platform_compat.windows_tree_cleanup_pending(pid, token)


def _windows_pending_child_entry(line: str) -> bool:
    """The same rule for the tracked-child file, whose rows have a different shape.

    ``kiro_pids.txt`` rows are ``child_pid:parent_pid[:child-start-id]``, so the
    positions invert the session file's ``gw:pid[:start-id]``: the pid this row is
    ABOUT is first and the optional identity belongs to that child, never to the
    parent. Both ends are therefore asked by pid alone -- a pin is keyed by the
    root it owns, so a child's own identity cannot match one, and the parent's
    identity is not recorded here to match with.

    Either end being an unretired root retains the row: the parent because the
    drain still owns the tree this child belongs to, the child because a nested
    runtime is itself a root. Retention is bounded by the reservation budget,
    and an unretired pin is what a restart needs to find these identities at all.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    parts = line.strip().split(":")
    try:
        ends = [int(parts[0])] if len(parts) == 1 else [int(parts[0]), int(parts[1])]
    except (ValueError, IndexError):
        return False
    return any(platform_compat.windows_tree_cleanup_pending(pid, None) for pid in ends)


def _write_back_pid_file(killed_or_dead: set[str]) -> None:
    """Remove *killed_or_dead* entries from the session PID file.

    Rewrites via :func:`atomic_write` (temp file + rename). A plain
    ``write_text`` truncates the file to zero BEFORE writing the kept entries,
    so a crash or a write failure inside that window leaves a SHORT file whose
    surviving content is perfectly well-formed — every dropped entry becomes an
    agent runtime no reaper can ever find again, with nothing raised and
    nothing logged. Rename makes the file either wholly old or wholly new.
    """
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if path.exists():
            current = path.read_text(encoding="utf-8").splitlines()
            keep = [
                entry
                for entry in current
                if entry.strip()
                and (entry.strip() not in killed_or_dead or _windows_pending_pid_entry(entry))
            ]
            _rewrite_pid_file(path, ("\n".join(keep) + "\n") if keep else "")


def _sweep_pid_entries(
    lines: list[str],
    *,
    should_skip_tagged: "Callable[[int, int], bool]",
    should_skip_bare: "Callable[[int], bool]",
    is_managed: "Callable[[int], bool] | None" = None,
    dry_run: bool = False,
) -> tuple[int, set[str], list[int]]:
    """Shared per-entry sweep logic for startup and periodic cleanup.

    Parses each line, applies caller-provided skip predicates, probes
    liveness, and either kills orphaned kiro-cli processes or collects
    them as candidates (when *dry_run* is True).

    Returns:
        ``(killed_count, killed_or_dead_entries, candidates)`` where
        *candidates* is non-empty only when ``dry_run=True``.
    """
    killed = 0
    killed_or_dead: set[str] = set()
    candidates: list[int] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            recorded_token: str | None = None
            if ":" in stripped:
                # ``gw:pid`` (legacy) or ``gw:pid:start_token`` (recycle guard).
                parts = stripped.split(":")
                if len(parts) == 3:
                    recorded_token = parts[2] or None
                elif len(parts) != 2:
                    killed_or_dead.add(stripped)
                    continue
                try:
                    gw_pid = int(parts[0])
                    pid = int(parts[1])
                except ValueError:
                    killed_or_dead.add(stripped)
                    continue
                if gw_pid <= 0 or pid <= 0:
                    killed_or_dead.add(stripped)
                    continue
                if should_skip_tagged(gw_pid, pid):
                    continue
            else:
                try:
                    pid = int(stripped)
                except ValueError:
                    killed_or_dead.add(stripped)
                    continue
                if pid <= 0:
                    killed_or_dead.add(stripped)
                    continue
                if should_skip_bare(pid):
                    continue
            if _windows_pending_pid_entry(stripped):
                continue
            # Probe liveness, three-way (os.kill(pid, 0) would *terminate* on
            # Windows, so route through platform_compat). DEAD -> prune;
            # UNSIGNALABLE (POSIX EPERM: alive but owned by another user) -> LEAVE
            # ALONE, never prune or kill a PID we merely can't signal; ALIVE ->
            # fall through to the managed-process check below.
            liveness = platform_compat.pid_liveness(pid)
            if liveness == platform_compat.PID_DEAD:
                killed_or_dead.add(stripped)
                continue
            if liveness == platform_compat.PID_UNSIGNALABLE:
                logger.debug("No permission to signal PID %s — skipping", pid)
                continue
            # Managed check (periodic only)
            if is_managed is not None and is_managed(pid):
                continue
            # ── PID-recycle identity check ──────────────────────────
            # The strongest guard: the entry recorded the child's start token
            # at spawn. If the live process's token DIFFERS, this PID has been
            # RECYCLED onto a different (agent) process — e.g. a fresh
            # gateway's own just-spawned backend landing on a stale dead-
            # gateway entry's PID (empirically reproduced on macOS: sweep
            # SIGKILL'd a live kiro-cli, surfacing as 'process exited
            # (rc=None)'). Prune the stale entry, never kill.
            #
            # An UNREADABLE live token (None) is "identity unknown", NOT a
            # mismatch: pruning there would untrack a live genuine orphan and
            # leak it forever, since every sweep keys off this file (same
            # fail-safe as _pid_gone_or_unmanaged — "any inconclusive result
            # retains"). Keep the entry and fall through to the grace check;
            # the next sweep retries.
            #
            # It runs AHEAD of the argv gate below because both answer the same
            # question — "does this PID still name the process the entry
            # described?" — and the token answers it exactly, for every harness,
            # on every platform, while :data:`_MANAGED_AGENT_MARKERS` answers it
            # by resemblance for two of the eight. Behind the argv gate, a
            # confirmed orphan whose argv resembles neither marker took the
            # prune arm: the entry was dropped as though the PID had been
            # recycled and the process was spared, which untracks a live orphan
            # that every sweep mechanism keys off this file to find. The same
            # ordering argument the PPid fallback carries in
            # :func:`cleanup_orphaned_session_roots` applies to the argv gate
            # here: a weaker guard must not veto a settled identity.
            token_settled = False
            if recorded_token is not None:
                live_token = _pid_start_token(pid)
                if live_token is not None and live_token != recorded_token:
                    killed_or_dead.add(stripped)
                    continue
                if live_token is None:
                    continue  # identity unknown — retain entry, retry next sweep
                token_settled = True
            # The argv test is what authorizes the kill, for every entry. A
            # matching token above does not substitute for it: the token is
            # subtractive evidence, and the tracking file is same-uid-writable.
            if not _is_managed_agent_process(pid):
                if token_settled:
                    # Two pieces of evidence disagree, and the DISPOSAL of the entry
                    # must follow the stronger one. A settled token is proof this PID
                    # is still the process this gateway spawned; the argv gate merely
                    # failed to recognise it, which is what happens on Windows, where
                    # only an image name is readable and an interpreter-hosted adapter
                    # reads as ``node.exe``. Pruning there spares the process AND
                    # discards the record -- and that record is the only thing any
                    # sweep keys off to find it, so the process becomes unreclaimable
                    # by anything. Retaining costs one entry re-examined per pass and
                    # keeps the deferred per-platform identity fix able to reach it.
                    logger.debug(
                        "Orphan sweep: PID %s is unrecognised by argv but its token "
                        "settled identity - retaining the entry",
                        pid,
                    )
                    continue
                killed_or_dead.add(stripped)
                continue
            # ── Spawn grace period (Fix A) ──────────────────────────
            # Skip live PIDs younger than SWEEP_SPAWN_GRACE_SECONDS.
            # POSIX-wide (Linux /proc, macOS ps -o etime=); Windows: no age
            # source, falls through to kill (behavior unchanged there).
            # POSIX read failure: treat as young (safe direction).
            # A missed kill self-heals next cycle.
            if _pid_in_spawn_grace(pid):
                continue
            if dry_run:
                candidates.append(pid)
                continue
            total_killed, root_killed = _kill_pid_tree(pid)
            killed += total_killed
            if root_killed:
                killed_or_dead.add(stripped)
            else:
                if not platform_compat.pid_exists(pid):
                    killed_or_dead.add(stripped)
        except Exception:
            logger.debug("Error processing PID entry %s", stripped, exc_info=True)
    return killed, killed_or_dead, candidates


def _periodic_pid_sweep(my_gw_pid: int, active_pids: set[int]) -> tuple[set[str], list[int]]:
    """Phase 1: identify orphan candidates in a thread (no killing).

    Returns ``(killed_or_dead, candidates)`` where *killed_or_dead* are
    entries to prune (dead/invalid) and *candidates* are PIDs that appear
    orphaned and should be killed — but the final kill decision is made
    back on the event loop where ``self._sessions`` is authoritative.
    """
    path = _session_pid_file_path()
    if not path.exists():
        return set(), []
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Non-truncating, for the reason spelled out in `_session_pid_file_lock`.
        # This site is the likeliest of the three to feel it: the sweep runs on a
        # timer while `_track_session_pid` is contending for the same lock, which
        # is exactly the interleaving a truncating open turns into a crash.
        # Kept inline rather than routed through `platform_compat.open_lock_file`:
        # this fd is held across the try/finally below, not a `with` block, so a
        # with-scoped opener that closes the fd at block exit does not fit.
        lock_path.touch(exist_ok=True)
        lock_fd = open(lock_path, "r+")
    except OSError:
        return set(), []
    try:
        # Shared (read) lock so concurrent gateways can scan the pid file together.
        # Windows note: msvcrt has no shared mode, so try_acquire_lock takes an
        # EXCLUSIVE lock there (see file_lock docstring) — a second concurrent
        # gateway's request fails and it simply skips this sweep cycle and retries
        # next tick. Degraded (sweep skipped), never incorrect; no data corruption.
        if not platform_compat.try_acquire_lock(lock_fd.fileno(), exclusive=False):
            return set(), []
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        finally:
            platform_compat.release_lock(lock_fd.fileno())
    finally:
        lock_fd.close()

    if not lines:
        return set(), []

    _, killed_or_dead, candidates = _sweep_pid_entries(
        lines,
        should_skip_tagged=lambda gw, _p: gw != my_gw_pid,
        should_skip_bare=lambda _p: True,
        is_managed=lambda p: p in active_pids,
        dry_run=True,
    )
    return killed_or_dead, candidates


def _session_pid_entry_index(my_gw_pid: int) -> dict[int, tuple[str, str | None]]:
    """``{child_pid: (entry_line, recorded_token)}`` over *my_gw_pid*'s entries.

    The periodic sweep runs in two phases with an event-loop hop between them, so
    the kill phase cannot be handed a verdict computed in the scan phase and trust
    it: the PID may have been reallocated while the loop was doing something else.
    It re-reads what the ENTRY recorded and re-derives the verdict against the live
    process, which is both fresher and the only thing that can be re-derived.

    Returning the entry LINE matters as much as the token. Entry text is what
    :func:`_write_back_pid_file` matches on, and a ``<gw>:<pid>`` string rebuilt
    from parts matches only a two-field entry — so a token-bearing entry stays in
    the file after its process is reaped, and the sweep meets a dead PID there on
    every later pass.
    """
    index: dict[int, tuple[str, str | None]] = {}
    path = _session_pid_file_path()
    try:
        with _session_pid_file_lock():
            if not path.exists():
                return index
            lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("Could not read %s for the kill phase", path, exc_info=True)
        return index
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(":")
        if len(parts) not in (2, 3):
            continue
        try:
            gw_pid = int(parts[0])
            child_pid = int(parts[1])
        except ValueError:
            continue
        if gw_pid != my_gw_pid or child_pid <= 0:
            continue
        recorded_token = parts[2] or None if len(parts) == 3 else None
        index[child_pid] = (stripped, recorded_token)
    return index


def _kill_confirmed_and_writeback(
    my_gw_pid: int, confirmed: list[int], killed_or_dead: set[str]
) -> int:
    """Phase 2b: kill confirmed orphans and write back PID file (sync, thread-safe).

    The scan phase (``_sweep_pid_entries`` under ``dry_run``) reports PIDs only, so no
    verdict crosses the event-loop hop between the phases. Each candidate is re-judged
    here against the file as it reads NOW: the entry's own recorded token is re-read and
    the subtractive check re-applied, and :func:`_kill_pid_tree` re-applies the argv gate
    that authorizes the signal. A candidate the re-read finds no entry for is skipped
    rather than killed, because nothing in the file then says what that PID was.
    """
    index = _session_pid_entry_index(my_gw_pid)
    orphan_killed = 0
    for pid in confirmed:
        found = index.get(pid)
        if found is None:
            # This gateway's entry for the PID is not in the file the kill phase
            # read. There is therefore nothing that records what this PID was when
            # it was tracked, so the recycle guard cannot be applied to it at all --
            # and an entry that is absent is also an entry this pass owes no
            # write-back. Killing anyway would mean signalling a PID on the strength
            # of a verdict reached before an event-loop hop, which is the exact
            # inheritance this two-phase split exists to refuse. Skip: either the
            # entry reappears on a later pass and is swept with its own evidence, or
            # it is genuinely untracked and no sweep is responsible for it.
            #
            # The unreadable-file arm of _session_pid_entry_index returns an EMPTY
            # index, so this also makes a transient read failure cost zero kills for
            # one pass instead of un-vouched kills for every candidate.
            logger.info("Orphan sweep: no PID-file entry for %s in the kill phase - skipping", pid)
            continue
        entry, recorded_token = found
        if recorded_token is not None:
            live_token = _pid_start_token(pid)
            if live_token is None:
                # Identity unknown: retain the entry and retry next sweep, the
                # same fail-safe the scan phase applies.
                continue
            if live_token != recorded_token:
                # Provably a different incarnation — prune, never kill.
                killed_or_dead.add(entry)
                continue
        total, root = _kill_pid_tree(pid)
        orphan_killed += total
        if root or not platform_compat.pid_exists(pid):
            killed_or_dead.add(entry)
    if killed_or_dead:
        _write_back_pid_file(killed_or_dead)
    return orphan_killed


# Grace given to a TERMed provider tree before the group SIGKILL. Mirrors the
# 3.0 s budget ``AcpClient._kill_process`` allows on the async teardown path, so
# a runtime gets the same chance to flush and exit through its own shutdown
# whichever path reaps it.
_PROVIDER_TERM_GRACE_SECONDS = 3.0
# Poll interval while waiting out that grace.
_PROVIDER_TERM_POLL_SECONDS = 0.05
# ``(start_id, argv0 basename)`` -- as the ACP layer records a descendant at spawn
# time. Only the start id is verified before signalling (see
# :func:`_signal_provider_descendants`); the basename rides along unread. Spelled
# here rather than imported from the ACP layer, which this module must carry no
# knowledge of: ``scripts/check_agent_sdk_boundary.py`` counts even a type-only
# import as such knowledge, and ``test_agent_lifecycle_cycle.py`` pins the absence.
_ProviderChildRecord = tuple[str | None, bytes | None]


def _isolated_provider_group(pid: int) -> int | None:
    """The process GROUP to signal for a provider rooted at *pid*, or ``None``.

    ``None`` means a pid-scoped kill is the only safe option, so the caller must
    not reach for ``killpg``.

    A group id is returned only when *pid* is an isolated group leader
    (``pgid == pid``, not our own group, not init's), which is what a provider
    spawned with ``start_new_session=True`` is. Under that predicate the group
    holds this provider's tree and nothing else, so a group signal can never
    reach a foreign process -- the same guard
    :func:`_kill_orphan_browser_daemon` carries for the same reason.

    Callers resolve this while the root is still alive and reuse the number for
    the escalation: the group outlives its leader, so the id read here still
    names the survivors after a TERM reaps the root, and reading it up front
    keeps a recycled pid out of the answer.

    POSIX only. Windows has no process groups in this sense; teardown there goes
    through ``kill_process_tree`` (``taskkill /T``), which walks the child tree
    itself.
    """
    if platform_compat.IS_WINDOWS:
        return None
    pgid = platform_compat.pgroup_of(pid)
    if pgid is None:
        return None
    if pgid == pid and pgid > 1 and pgid != os.getpgrp():
        return pgid
    return None


def _root_identity_holds(pid: int, recorded_start: str | None, *, gated: bool) -> bool:
    """Whether *pid* is still the process whose identity was recorded.

    Called again before EVERY signal, not once up front: the identity checked at
    entry can go stale inside the SIGTERM grace, because this teardown is not the
    only reaper. ``asyncio``'s child watcher can reap the leader zombie on its own
    (``_reap_provider_root`` says as much), which frees the pid, and under pid-space
    wraparound a new process can take it before the SIGKILL lands.

    ``gated=False`` is the ``_proc`` / ``_active_proc`` shape, whose pid comes from
    a live handle this process owns: there is no recorded id to compare and the
    handle already proves the child is unreaped.
    """
    if not gated:
        return True
    if recorded_start is None:
        return False
    return platform_compat.get_process_start_id(pid) == recorded_start


def _pgroup_still_ours(
    pgid: int,
    root_pid: int,
    recorded_start: str | None,
    records: dict[int, _ProviderChildRecord],
    *,
    gated: bool,
) -> bool:
    """Whether an identity-verified process still owns process group *pgid*.

    A pgid is only as trustworthy as its members. Once every process in the group
    is gone the number is free, and a group signal aimed at it lands on whichever
    tree takes it next -- so the escalation may only signal the group while it can
    still name a member it has verified: the root itself, or one of the descendants
    recorded at spawn. An unverifiable group is not signalled; the caller falls
    back to the verified descendants it can still name individually.

    A held zombie counts, and deliberately so: it is still a group member and its
    identity is still readable, which is the whole point of not reaping the root
    until the last signal is out. Membership is read through
    :func:`_pid_in_pgroup` for that reason -- see there for why ``getpgid`` alone
    would drop the zombie root on macOS. The ungated ``_proc`` shape has no
    recorded start id, so its owned live handle supplies a fresh identity read for
    the listing comparison.
    """
    if platform_compat.IS_WINDOWS:
        return False
    if _root_identity_holds(root_pid, recorded_start, gated=gated):
        root_start = (
            recorded_start
            if recorded_start is not None
            else platform_compat.get_process_start_id(root_pid)
        )
        if _pid_in_pgroup(root_pid, pgid, root_start):
            return True
    for cpid, (start, _basename) in records.items():
        if start is None:
            continue
        if platform_compat.get_process_start_id(cpid) != start:
            continue
        if _pid_in_pgroup(cpid, pgid, start):
            return True
    return False


def _pid_in_pgroup(pid: int, pgid: int, start_id: str | None) -> bool:
    """Whether *pid* is a member of process group *pgid*, zombie or not.

    ``getpgid`` answers for a live member on every POSIX host, and on Linux for a
    zombie too. macOS refuses it for a zombie (``ESRCH``: the kernel looks the
    pid up among running processes only), which is exactly the member
    :func:`_pgroup_still_ours` needs to see -- the exited-but-unreaped root is
    the one verified member left in the group once its children have been
    SIGTERMed, and losing it there suppresses the group SIGKILL, so a child that
    ignored SIGTERM outlives the teardown. ``sysctl KERN_PROC_PGRP`` lists the
    group's zombies alongside its live members, so it settles the question
    ``getpgid`` cannot.

    The listing is consulted ONLY when ``getpgid`` had no answer. A definite
    answer naming another group is final: the pid has left the group, or the
    number has moved on to a stranger's tree, and a second oracle must not be
    allowed to overrule that verdict in the direction of signalling. Unreadable
    on both is "not a member": the caller must not signal a group it cannot
    prove it owns.

    A listed member counts only when its ``start_id`` matches *start_id* as well
    as its pid. The caller verified the identity before asking, but the listing
    is a separate read: a zombie collected by another reaper in between frees the
    pid, and under wraparound a stranger's new group leader can hold it by the
    time the listing runs. Matching the start instant refuses that recycled pid;
    a caller with no identity to offer (``None``) gets "not a member" for the
    same reason.
    """
    answer = platform_compat.pgroup_of(pid)
    if answer is not None:
        return answer == pgid
    if sys.platform != "darwin" or start_id is None:
        return False
    members = platform_compat.darwin_pgroup_members(pgid)
    if members is None:
        return False
    return any(m.pid == pid and m.start_id == start_id for m in members)


def _group_from_witnessed_descendant(
    pid: int,
    recorded_start: str | None,
    records: dict[int, _ProviderChildRecord],
) -> int | None:
    """The group led by *pid*, once :func:`_pgroup_still_ours` vouches for it.

    Companion to :func:`_isolated_provider_group` for the case it cannot serve: a
    reaped leader. ``pgroup_of`` needs the leader in ``/proc``, so once it is gone
    the group has no readable id and the escalation signals nothing -- while the
    members left in that group keep running.

    ``pgroup_of_leader`` resolves the id for a reaped leader, but from the pid
    ALONE, and an unverified pid is what a RECYCLED one looks like: killpg on it
    takes a stranger's tree. So this reads the number and then hands it to the same
    ownership check the escalation itself uses, rather than carrying a second copy
    of that loop.

    What is added here is only the leader predicate: ``candidate == pid`` ties the
    number to the leader we recorded, since a group a descendant ``setsid``-ed into
    is not the one this pid led.

    ``None`` -- no group signal, exactly as before -- when no verified member
    vouches, when the group is not the one ``pid`` leads, and when signalling is
    denied. POSIX only, like :func:`_isolated_provider_group`.
    """
    if platform_compat.IS_WINDOWS:
        return None
    candidate = platform_compat.pgroup_of_leader(pid)
    if candidate is None or candidate != pid or candidate <= 1 or candidate == os.getpgrp():
        return None
    if not _pgroup_still_ours(candidate, pid, recorded_start, records, gated=True):
        return None
    return candidate


def _provider_descendant_records(
    provider: object,
    pid: int,
    *,
    include_live_walk: bool = True,
    recorded_start: str | None = None,
    gated: bool = False,
) -> dict[int, _ProviderChildRecord]:
    """Snapshot a provider's descendants as ``pid -> (start_time, basename)``.

    Merges the runtime's own spawn-time snapshot (``client._child_pids``, which
    holds descendants that have already reparented away from the root and are
    therefore invisible to a live walk) with a fresh recursive scan of the
    process tree, and records each pid's identity so
    :func:`_signal_provider_descendants` can refuse a recycled one.

    Taken BEFORE any signal: once the root exits its children reparent to init
    and no walk can find them from the root again.

    Reads the machine through ``platform_compat`` alone -- ``process_descendants``
    for the walk, ``get_process_start_id`` for the identity -- so this module
    borrows nothing from the agent layer to do it. That is possible because the
    ACP layer records the SAME neutral start id at spawn time, so a stored record
    and a fresh one are directly comparable.

    ``include_live_walk=False`` drops the fresh walk and keeps ONLY the spawn-time
    snapshot. Pass it when the root's own identity could not be verified: a walk
    from an unverified pid enumerates whoever holds it now, and because those pids
    would have their identity captured here and re-read moments later they would
    MATCH and be signalled -- turning a recycled root into a licence to kill a
    stranger's children. The stored snapshot cannot do that: its entries were
    recorded when the tree was provably ours.

    The walk is BRACKETED by the same identity check for the same reason. A root
    verified just before this call can exit while the walk runs, and the pid can be
    reused inside that window, so a walk that started on our tree can finish on a
    replacement's. Re-reading the identity afterwards and discarding the fresh
    entries on a mismatch is what keeps a stale verification from authorising the
    sweep; the spawn-time snapshot is unaffected and is still returned.
    """
    records: dict[int, _ProviderChildRecord] = {}
    client = getattr(provider, "_client", None)
    stored = getattr(client, "_child_pids", None) if client is not None else None
    if isinstance(stored, dict):
        for cpid, record in stored.items():
            if isinstance(cpid, int) and cpid > 1:
                records[cpid] = record if isinstance(record, tuple) else (record, None)
    if not include_live_walk:
        return records
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: skipping the live descendant walk for pid %d -- "
            "identity went stale before it started",
            pid,
        )
        return records
    try:
        fresh = [p for p in platform_compat.process_descendants(pid) if p > 1 and p not in records]
    except Exception:
        logger.debug("_sync_kill_provider: descendant scan failed for %d", pid, exc_info=True)
        fresh = []
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: discarding %d freshly walked descendant(s) of pid %d -- "
            "the root's identity changed while the walk ran, so they are not ours",
            len(fresh),
            pid,
        )
        return records
    # Capture each identity, THEN re-confirm ancestry from a second snapshot. The
    # enumeration above and the capture below are separate reads, so a descendant
    # can exit and its pid be reused in between, handing us a stranger's start id
    # that then verifies at signal time.
    #
    # The second snapshot is bracketed by the root check as well, and it is NOT
    # enough to argue that an intersection can only shrink the set. Membership is
    # not the only thing these two reads decide: if the ROOT pid is itself reused
    # by a foreign process F while this runs, the second snapshot enumerates F's
    # children, so a first-walk pid that F's child Y now holds is confirmed by the
    # filter and carries Y's captured id -- which the signal-time re-read then
    # agrees with instead of catching. Re-reading the root identity here is what
    # separates "this pid is still in OUR tree" from "this pid is in whatever tree
    # holds that number now".
    captured = {cpid: platform_compat.get_process_start_id(cpid) for cpid in fresh}
    try:
        still_ours = set(platform_compat.process_descendants(pid))
    except Exception:
        logger.debug("_sync_kill_provider: re-scan failed for %d", pid, exc_info=True)
        still_ours = set()
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: discarding %d freshly walked descendant(s) of pid %d -- "
            "the root's identity changes across the re-scan, so the tree scanned is "
            "not ours",
            len(captured),
            pid,
        )
        return records
    for cpid, start_id in captured.items():
        if cpid in still_ours:
            records[cpid] = (start_id, None)
        else:
            logger.warning(
                "_sync_kill_provider: dropping pid %d -- it stops being a descendant of "
                "%d between the walk and its identity capture, so the id may be a "
                "stranger's",
                cpid,
                pid,
            )
    return records


def _signal_provider_descendants(records: dict[int, _ProviderChildRecord], sig: int) -> None:
    """Send *sig* to each snapshotted descendant still alive, leaf-first.

    Covers the descendant a group signal cannot reach: one that ``setsid``-ed
    out of the group, and one already reparented before the snapshot. Ownership
    is re-verified per pid against the recorded start id, so a pid recycled
    inside the teardown window is skipped rather than signalled (deny-by-default).
    Leaf-first so a parent cannot fork a replacement while its own children are
    being reaped.

    The start id alone decides this, through ``platform_compat`` rather than the
    agent layer's own check: it is microsecond libproc on macOS, 100 ns creation
    time on Windows and stat field 22 on Linux, so two processes on the same pid
    at different times cannot share one. The recorded basename is deliberately
    NOT compared, because reading a live pid's basename would mean borrowing the
    ACP layer's reader and this module is held to no knowledge of that layer
    (``scripts/check_agent_sdk_boundary.py``). Basename was only ever a second
    opinion on top of the start id, and it cannot rescue a case the start id
    misses -- a matching start id already means it is the same process.
    """
    if platform_compat.IS_WINDOWS:
        return
    for cpid in reversed(list(records)):
        try:
            if not platform_compat.pid_exists(cpid):
                continue
            expected_start, _expected_basename = records[cpid]
            actual_start = platform_compat.get_process_start_id(cpid)
            if expected_start is None or actual_start is None or actual_start != expected_start:
                logger.debug("_sync_kill_provider: skipping pid %d -- not ours (recycled?)", cpid)
                continue
            platform_compat.kill_pid(cpid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _pid_exited_but_unreaped(pid: int) -> bool:
    """True when *pid* has finished running: a zombie, or already gone.

    Lets the teardown observe the root's exit WITHOUT reaping it. That matters
    because a zombie still owns its pid, and for a group leader that pid IS the
    process group id -- reaping it frees the number, after which the pgid may be
    handed to an unrelated new leader. So the escalation has to be able to say
    "the root has exited" without also making its pgid ambiguous.

    Conservative on an unreadable state: returns False, i.e. "still running", so
    a caller waits out its grace rather than exiting early on a guess. On macOS
    the state comes from ``sysctl KERN_PROC_PID``, which lists zombies where
    libproc refuses them; a liveness probe would not do, because a zombie
    answers ``kill(pid, 0)`` as present and the root would read as running until
    someone else reaped it -- which this teardown deliberately does not, until
    the last group signal is sent. On other non-Linux platforms there is no
    zombie state to read, so this falls back to plain liveness: an
    exited-but-unreaped root reads as alive and the caller waits out the grace.
    """
    if sys.platform == "darwin":
        zombie = platform_compat.darwin_pid_is_zombie(pid)
        return bool(zombie) if zombie is not None else False
    if sys.platform != "linux":
        return not platform_compat.pid_exists(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True  # already gone
    except OSError:
        return False  # unreadable -- do not claim it exited
    rparen = stat.rfind(")")
    if rparen < 0:
        return False
    fields = stat[rparen + 2 :].split()
    return bool(fields) and fields[0] == "Z"


def _pgroup_has_member_besides(pgid: int, root_pid: int) -> bool:
    """True when some process other than *root_pid* is still in group *pgid*.

    ``pgroup_exists`` cannot answer this: a retained zombie leader is itself a
    group member, so ``killpg(pgid, 0)`` keeps succeeding after everything real
    has died (measured). Without this distinction, holding the zombie for pgid
    safety would cost every teardown its full SIGTERM grace.

    One ``/proc`` pass reading each stat's pgrp (field 5, the third field after
    the last ``)``, the same parse :func:`_pid_parent_and_token` uses). Callers
    gate it behind the cheap probes, so the common path runs it once.

    Conservative on a scan failure: returns True, i.e. "assume the group still
    holds something", so the caller escalates rather than declaring the tree
    gone on unread evidence.

    macOS lists the group with ``sysctl KERN_PROC_PGRP`` and applies the same
    rule -- a zombie member is not holding the group open. Other non-Linux
    platforms can only ask whether the group exists, which a retained zombie
    leader keeps answering yes to, so there the caller waits out its grace.
    """
    if sys.platform == "darwin":
        members = platform_compat.darwin_pgroup_members(pgid)
        if members is None:
            return True
        return any(m.pid != root_pid and not m.zombie for m in members)
    if sys.platform != "linux":
        return platform_compat.pgroup_exists(pgid)
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        logger.debug("_sync_kill_provider: /proc scan failed for pgid %d", pgid, exc_info=True)
        return True
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        member = int(name)
        if member == root_pid:
            continue
        try:
            stat = (entry / "stat").read_text()
        except (OSError, ValueError):
            continue  # exited mid-scan; it is not holding the group open
        rparen = stat.rfind(")")
        if rparen < 0:
            continue
        fields = stat[rparen + 2 :].split()
        try:
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        except (IndexError, ValueError):
            continue
    return False


def group_vouching_available() -> bool:
    """Whether the instance-vouched group read can reach a tree on this host.

    The vouch reads ``/proc/<pid>/environ`` for the per-spawn incarnation token,
    which exists on Linux alone. Everywhere else :func:`_marked_group_members`
    answers ``{}`` and a teardown whose root identity is not proven reaches
    NOTHING -- a bounded leak the orphan sweep reports, never a signal to a
    stranger. Callers need to be able to SAY that, so the platform test lives
    here once rather than being re-derived at each reader.
    """
    return sys.platform == "linux"


def _marked_group_members(pgid: int, instance: str) -> dict[int, str | None]:
    """Live members of process group *pgid* spawned as incarnation *instance*.

    Returned as ``pid -> start id`` so a caller that signals the group twice can
    prove on the second pass that it is still the SAME group: a pid and a start
    instant name one process for good, where the group number alone does not
    (see :func:`_signal_orphaned_runtime_group`).

    *instance* is the per-spawn ``KIROCREW_SPAWN_INSTANCE`` the runtime put on
    its root's environment, and a member counts only if it carries that exact
    value. The generic ``KIROCREW_SPAWNED`` marker proves a group is SOME Kiro
    Crew runtime's; it cannot prove it is this one's, because every runtime here
    is a marked session leader and a root pid released to a fresh spawn names a
    group that carries the marker just as well. The instance is what tells them
    apart. An empty *instance* matches nothing.

    The proof a teardown needs once the group's LEADER is gone. A leader spawned
    with ``start_new_session=True`` names its group by its own pid, so the number
    survives the leader -- but a bare number is what a recycled pid looks like
    too, and ``killpg`` on it would take a stranger's tree. A member that carries
    ``KIROCREW_SPAWNED`` in its exec-time environment is the positive identity
    that a group is one Kiro Crew spawned; a detached survivor that merely
    inherited the marker is excluded by the argv identity check, the same pair
    the tracked sweep's systemd arm requires.

    Linux only. The environ read is Linux-only and fail-closed everywhere else,
    so the answer is ``{}`` on macOS and Windows and the caller signals nothing
    -- a missed reap there, never a wrong kill. Zombies are skipped: they hold
    no memory and cannot be signalled into exiting.
    """
    if not group_vouching_available() or pgid <= 1 or not instance:
        return {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return {}
    members: dict[int, str | None] = {}
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        member = int(name)
        try:
            stat = (entry / "stat").read_text()
        except (OSError, ValueError):
            continue
        rparen = stat.rfind(")")
        if rparen < 0:
            continue
        fields = stat[rparen + 2 :].split()
        try:
            if int(fields[2]) != pgid or fields[0] == "Z":
                continue
        except (IndexError, ValueError):
            continue
        if (
            _env_spawn_instance(member) == instance
            and _env_has_kirocrew_marker(member)
            and _tracked_child_has_runtime_identity(member)
        ):
            members[member] = _pid_start_token(member)
    return members


#: Errnos that mean "this kernel does not have the pidfd syscall", as opposed to
#: "it has it and refused this call". Only the first may use the numeric path.
_PIDFD_ABSENT_ERRNOS = frozenset({errno.ENOSYS, errno.EPERM})


def _signal_pid_by_identity(pid: int, sig: int, start: str) -> bool:
    """Signal *pid* only while it is still the process whose start id is *start*.

    Returns True when the signal was delivered, False when the identity fails to
    match, and raises the delivery error otherwise.

    Re-reading the start id and then calling ``os.kill`` leaves a window between
    the two: the number can be recycled in it, and the signal lands on whatever
    holds it now. Narrowing that window cannot close it, because a pid is not a
    handle -- it is a key the kernel may reissue. A pidfd IS a handle: it refers
    to one process for as long as it is open, so once the identity is confirmed
    AFTER opening it, ``pidfd_send_signal`` cannot reach a later occupant of the
    number no matter how long the descriptor is held. The order is open, then
    verify, then signal -- opening first is what makes the verification binding.

    ``pidfd_open`` needs Linux 5.3 and this whole path is Linux-only, but a
    kernel without it must still be reachable, so there the re-verified
    ``os.kill`` stands: the same narrow window as before, never a wider one.
    """

    def _by_number() -> bool:
        """The identity-verified numeric signal, for a host with no pidfd."""
        if platform_compat.get_process_start_id(pid) != start:
            return False
        os.kill(pid, sig)
        return True

    opener = getattr(os, "pidfd_open", None)
    sender = getattr(signal, "pidfd_send_signal", None)
    if opener is None or sender is None:
        return _by_number()
    try:
        fd = opener(pid)
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno in _PIDFD_ABSENT_ERRNOS:
            # The attribute exists on any Linux build, but the SYSCALL answers
            # ENOSYS before 5.3 (and a seccomp filter can hide it with EPERM).
            # The capability is absent on this host, which is precisely the case
            # the numeric path is for.
            return _by_number()
        # The call exists and refused this open (EMFILE, ENOMEM): no handle, so no
        # atomic signal. Falling back to the number here would reopen the window
        # the handle closes, so fail closed and leave the member to the orphan
        # sweep -- a leak over a signal to a stranger, the trade the rest of this
        # path makes. A fallback is for a capability that is missing, never for one
        # that is present and said no.
        logger.warning(
            "_signal_pid_by_identity: no pidfd for pid %d, so signal %d is not sent; "
            "leaving it to the orphan sweep",
            pid,
            sig,
            exc_info=True,
        )
        return False
    try:
        # AFTER the open: the descriptor already pins whichever process answered,
        # so a match here proves the pinned process is the one that vouched.
        if platform_compat.get_process_start_id(pid) != start:
            return False
        try:
            sender(fd, sig)
        except OSError as exc:
            # pidfd_send_signal landed in 5.1 and pidfd_open in 5.3, so the two
            # can disagree; the same split applies.
            if exc.errno in _PIDFD_ABSENT_ERRNOS:
                return _by_number()
            raise
        return True
    finally:
        os.close(fd)


def _signal_orphaned_runtime_group(
    pgid: int,
    sig: int,
    instance: str,
    *,
    expected: Mapping[int, str | None] | None = None,
) -> dict[int, str | None]:
    """Signal a runtime's group members after its leader has been reaped, if ours.

    The kill path that signals a live tree is ``killpg(getpgid(root))``, and it
    has a hole exactly where the leak lives: ``getpgid`` raises once the root has
    exited, the caller reads that as "already dead", and the launcher, agent and
    chat processes left in the group are never signalled. They reparent to init
    holding their memory, and if the root died before any descendant was
    recorded there is no tracking entry to find them by either.

    So resolve the group from the contract instead of from the dead pid -- the
    root was a session leader, so ``pgid == root pid`` -- and find its members
    through :func:`_marked_group_members`, which vouches each one by *instance*:
    the per-spawn token the runtime put on its root's environment, which its
    whole tree inherited. Then signal THOSE MEMBERS, each re-verified by start id
    at the instant of the signal -- not the group number. The number is the
    reaped root's pid and can be handed to a fresh session leader at any moment,
    including between the vouch and the signal; a member's pid plus its start
    instant cannot be. No vouching member means no signal: declining to act
    costs a leak the sweep still reports, where acting costs an unrelated
    process. ``pgid`` is still refused for 0, 1 and our own group, since a
    member listing keyed on one of those would be a listing of the wrong thing.

    The instance is the incarnation pin, and it is what the generic marker
    cannot supply. Every runtime here is a marked session leader, so a root pid
    released and handed to a fresh spawn names a group that carries the marker
    just as well -- and a signal aimed by the number and the marker alone would
    terminate that fresh runtime's live session. A fresh spawn carries a
    different instance, so it does not vouch for this one's group.

    An escalation additionally passes the members the first pass vouched, as
    *expected*, and signals only those of them still alive under the same start
    id -- nothing found for the first time now: the instance proves the
    incarnation, the start id proves the pass is looking at the same processes,
    and a process that was not signalled on the first pass owes no escalation.

    Returns the members actually signalled, ``pid -> start id`` -- the value a
    caller hands back as *expected* -- or an empty map when nothing was. A
    member that exited between the vouch and the signal, or whose identity no
    longer reads as vouched, is skipped. A refused signal is logged and skipped:
    a teardown must finish clearing its own state and pruning its PID entries
    whatever the kernel answered, and a signal this process was refused is one
    the periodic sweep retries on its own cadence.
    """
    if platform_compat.IS_WINDOWS or pgid <= 1 or pgid == os.getpgrp() or not instance:
        return {}
    members = _marked_group_members(pgid, instance)
    if not members:
        return {}
    if expected is not None:
        still_ours: dict[int, str | None] = {
            p: start
            for p, start in members.items()
            if p in expected and start is not None and start == expected[p]
        }
        if not still_ours:
            logger.info(
                "_signal_orphaned_runtime_group: none of the %d member(s) vouched for "
                "group %d are still alive; not re-signalling a group that may be a "
                "newer runtime's",
                len(expected),
                pgid,
            )
            return {}
        # The escalation's target set is the FIRST pass's, not a fresh census: a
        # member found only now was not signalled then, owes no grace, and is
        # exactly what a newer incarnation of the number would look like.
        members = still_ours
    # Signal the MEMBERS, each re-verified by start id at the instant of the
    # signal, never the group number. ``killpg(pgid)`` is aimed at a number, and
    # the number is the reaped root's pid, which the kernel can hand to a fresh
    # session leader between the vouch above and the signal; a pid plus a start
    # instant names one process for good, so a member whose identity still reads
    # as vouched is the process that vouched. Highest pid first as a cheap
    # leaf-first order, so a parent cannot fork a replacement while its own
    # children are being signalled.
    signalled: dict[int, str | None] = {}
    for member in sorted(members, reverse=True):
        start = members[member]
        if start is None:
            continue
        try:
            if not _signal_pid_by_identity(member, sig, start):
                continue
        except ProcessLookupError:
            continue
        except OSError:
            logger.warning(
                "_signal_orphaned_runtime_group: signal %d to pid %d (group %d) refused; "
                "leaving it to the orphan sweep",
                sig,
                member,
                pgid,
                exc_info=True,
            )
            continue
        signalled[member] = start
    return signalled


def _provider_tree_gone(
    pid: int, pgid: int | None, records: dict[int, _ProviderChildRecord]
) -> bool:
    """True when nothing of the provider's tree is alive any more.

    Probes the GROUP when one was resolved -- a group outlives its leader, so a
    dead root proves nothing about the launcher and agent processes left in it,
    which is the whole failure this teardown exists to close. The root is judged
    by :func:`_pid_exited_but_unreaped` rather than by liveness, because the
    escalation deliberately holds its zombie to keep the pgid unambiguous, and a
    zombie answers every liveness probe as present.

    The cheap record probe runs first so the ``/proc`` group scan is reached only
    once the recorded descendants are all gone.
    """
    if any(platform_compat.pid_exists(cpid) for cpid in records):
        return False
    if not _pid_exited_but_unreaped(pid):
        return False
    if pgid is not None:
        return not _pgroup_has_member_besides(pgid, pid)
    return True


def _reap_provider_root(pid: int, recorded_start: str | None, *, gated: bool) -> None:
    """Reap *pid* if it is still the process whose identity was recorded.

    A zombie answers every liveness probe as present until someone waits on it.
    ``ChildProcessError`` means the process is not ours to reap (or asyncio's
    child watcher got there first), which is not a failure here.

    Identity is re-checked immediately before the wait, for the same reason every
    signal re-checks it: that watcher can reap the leader zombie on its own, which
    frees the pid, and ``waitpid`` on a recycled pid consumes an UNRELATED child's
    exit status. That loss is silent and nothing detects or repairs it, so this
    reads the identity here rather than trusting the one taken at entry.

    WHY THE RE-READ IS ENOUGH, on every platform. Linux keeps ``/proc/<pid>/stat``
    readable for a zombie. macOS ``proc_pidinfo`` refuses one, so
    :func:`platform_compat.get_process_start_id` falls back to ``sysctl``, which
    reads the same start instant from the kernel's zombie list. Either way the
    identity of the state this teardown CREATES -- a killed root held unreaped to
    keep its pgid unambiguous -- survives the exit and the check passes for the case
    this exists to handle.

    Where an identity cannot be read at all, refusing is deny-by-default working as
    designed, not a gap to route around. An unreadable identity is exactly the case
    where a recycled pid cannot be told from our own zombie: a freed pid can be taken
    by another child of THIS process that is itself an unreaped zombie, and that
    occupant reads as unreadable too, so a wait would steal its exit status and its
    own watcher would report a code that never happened.

    What such a refusal leaves behind is a ZOMBIE, which has already released its
    memory: one process-table entry and an exit status, bounded by pid space and
    gone when this process exits. That is not the leak this teardown exists to
    close -- that one is a RUNNING descendant holding hundreds of megabytes with no
    tracking entry left to find it by. And the entry is not necessarily permanent:
    asyncio's child watcher may still reap it afterwards, since the root is this
    process's child. A benign bounded entry is the cheaper side of the trade.
    """
    if not _root_identity_holds(pid, recorded_start, gated=gated):
        logger.warning(
            "_sync_kill_provider: NOT reaping pid %d -- identity does not hold, so a "
            "wait would consume an unrelated child's status",
            pid,
        )
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _sync_kill_provider(provider: object) -> None:
    """Synchronously kill a provider's whole process tree.

    Used during CancelledError handling where async shutdown is unreliable
    (asyncio.shield + await raises CancelledError immediately, leaving
    shutdown fire-and-forget).  Escalates SIGTERM to SIGKILL after a bounded
    grace.

    Signals the process GROUP, not the pid. An agent runtime is a tree -- a
    sandbox launcher, the agent binary, its own chat subprocess and a handful of
    MCP stub children -- and the pid recorded for a provider is the tree's group
    leader (spawned ``start_new_session=True``). A pid-scoped signal reaps that
    leader alone; everything below it survives, reparents to init, and holds its
    memory for the life of the machine, with no tracking entry left to find it
    by. Descendants outside the group are swept individually.

    ``provider`` is deliberately ``object`` rather than ``LLMProvider``.  Every
    read below goes through ``getattr(..., None)`` against a PRIVATE attribute
    that the provider ABC does not declare, so the ABC never described this
    parameter -- and importing it here for the annotation alone closed a cycle:
    session_pid -> providers.base -> acp.types -> acp/__init__ -> acp.runtime ->
    session_pid.  That cycle was fatal, not cosmetic: importing this module
    first raised ``ImportError`` on ``_track_pid``.  It is why sibling
    leaves carry ``LLMProvider = Any`` runtime stubs and why this module reaches
    acp.client through function-local imports.  ``test_agent_lifecycle_cycle.py``
    pins the absence; keep this leaf ignorant of the agent layer.
    """
    # ACP provider: long-lived process via client._pid
    client = getattr(provider, "_client", None)
    pid = getattr(client, "_pid", None) if client else None
    # Whether the pid is a RECORDED number (staleness-prone, so identity-gated
    # below) or one read from a live handle this process owns.
    pid_from_client = pid is not None
    # CC provider: long-lived process via _proc.pid or ephemeral via _active_proc.pid
    if pid is None:
        proc = getattr(provider, "_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        proc = getattr(provider, "_active_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        return
    # Only ever signal a real, positive, non-init PID. Test stand-ins are the
    # sharp edge: a Mock attribute passes the None check and coerces to 1 via
    # __index__, so an unguarded os.kill would SIGTERM init / the container
    # entrypoint (observed as a CI sandbox dying with exit 143). pid <= 1 also
    # excludes the kill(0)/kill(-n) process-group semantics outright.
    if not isinstance(pid, int) or pid <= 1:
        logger.debug("_sync_kill_provider: refusing to signal invalid pid %r", pid)
        return
    # Deny-by-default on the ROOT's own identity -- but only where the pid can go
    # stale. ``_client._pid`` is a RECORDED number that outlives a failed start, so
    # it can name a process the OS has since handed to someone else; and for a group
    # leader that pid IS the pgid, so an unverified ``killpg`` takes a stranger's
    # whole process tree. The pid-scoped fallback is the same hazard one process
    # wide, so an unproven root is not signalled AT ALL rather than signalled
    # narrowly. Recorded descendants are still swept: each carries its own
    # spawn-time identity and is verified against it, which is exactly the check
    # the root was missing.
    #
    # The ``_proc`` / ``_active_proc`` pids are NOT gated: they come from a live
    # handle this process owns whose ``returncode`` is None, so the child is
    # unreaped and its pid cannot have been recycled -- the handle is already
    # better evidence than a recorded id would be, and there is no recorded id
    # for that shape to compare against.
    #
    # Same source on both sides or the comparison is meaningless: the ACP layer
    # records ``_start_time`` with ``platform_compat.get_process_start_id`` (both
    # its client and its runtime do), which is what this reads back. It is also
    # what makes this work on Windows, where there are no process groups but a
    # recycled pid would still send ``taskkill /T`` down a foreign tree.
    root_verified = True
    recorded_start: str | None = None
    if pid_from_client:
        recorded_start = getattr(client, "_start_time", None)
        live_start = platform_compat.get_process_start_id(pid)
        root_verified = (
            isinstance(recorded_start, str)
            and live_start is not None
            and live_start == recorded_start
        )
        if not root_verified:
            logger.warning(
                "_sync_kill_provider: NOT signalling root pid %d -- identity unproven "
                "(recorded=%r, live=%r); sweeping only descendants recorded at spawn",
                pid,
                recorded_start,
                live_start,
            )
    # On Windows there is no SIGTERM/SIGKILL distinction (taskkill /F is a hard
    # kill) and no os.waitpid for non-child PIDs, so a single kill suffices.
    if platform_compat.IS_WINDOWS:
        if not root_verified:
            # Nothing further to do here: the descendant sweep is a POSIX-only
            # arm (no process groups to escape on Windows, and ``taskkill /T``
            # is what normally covers the tree), so refusing the root refuses
            # the whole kill rather than narrowing it.
            return
        # Every Windows shape uses exact-tree cleanup and the same capacity
        # admission. Refusal preserves the whole tree; no root-only fallback.
        try:
            if pid_from_client:
                # PINNED: the query handle that verified this identity is held
                # open across taskkill, and Windows reserves a pid while any
                # handle to the process object exists -- so the pid still means
                # the same process when taskkill resolves it. Reading the start
                # id and then calling the plain variant releases that handle
                # first, which is the window taskkill /T would tear a stranger's
                # whole tree down through. False means identity unconfirmed, and
                # is a refusal to reap rather than a failure to report.
                assert recorded_start is not None  # implied by root_verified
                if not platform_compat.kill_process_tree_pinned(
                    pid, recorded_start, platform_compat.SIGKILL
                ):
                    logger.warning(
                        "_sync_kill_provider: NOT killing tree of pid %d -- Windows "
                        "identity could not be pinned across the terminate",
                        pid,
                    )
                    return
            else:
                # An owned Popen still participates in the same cleanup budget.
                # Its handle pins the source while the identity is read; an
                # unreadable identity is not permission for a numeric fallback.
                start = platform_compat.get_process_start_id(pid)
                if start is None or not platform_compat.kill_process_tree_pinned(
                    pid, start, platform_compat.SIGKILL
                ):
                    return
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.warning(
                "_sync_kill_provider: Windows tree cleanup incomplete for PID %d (%s)",
                pid,
                exc,
            )
            return
        logger.warning("_sync_kill_provider: killed PID %d for leaked provider", pid)
        return
    # Resolved once, while the root is alive. The root's zombie is then held
    # unreaped until every group signal has been sent (see below), so this id
    # keeps naming OUR group for the whole escalation. Read BEFORE the descendant
    # scan: that scan is unbounded in the width of the tree, and the watcher can
    # reap the leader while it runs, which would cost a verified root its group.
    pgid = _isolated_provider_group(pid) if root_verified else None
    records = _provider_descendant_records(
        provider,
        pid,
        include_live_walk=root_verified,
        recorded_start=recorded_start,
        gated=pid_from_client,
    )
    if pgid is None and pid_from_client:
        # No group id from the root. Usually that root is a RECYCLED pid, and
        # reading a group off it would name whatever group holds that pid now. But
        # it is also the shape a REAPED leader presents -- no /proc entry to verify
        # against -- and that root's group is still full of our running members.
        # Telling the two apart needs evidence the pid cannot give, so the group is
        # derived from a spawn-recorded descendant still in it; failing that this
        # stays None and nothing is signalled, as before.
        #
        # Only for the RECORDED-pid shape. A `_proc`/`_active_proc` root came from a
        # live handle whose returncode was None, so it was alive moments ago and
        # `_isolated_provider_group` already had its answer -- there is no reaped
        # leader to recover. That shape also passes `gated=False`, which makes every
        # `_root_identity_holds` check pass on trust, including the ones bracketing
        # the live descendant walk: a pid recycled to a foreign root would have that
        # root's children walked into `records` and verified against identities read
        # from the same stranger, so a witness drawn from them proves nothing. The
        # recorded shape cannot reach that state -- an unverified root turns the walk
        # off (`include_live_walk=root_verified`), leaving only the spawn snapshot.
        pgid = _group_from_witnessed_descendant(pid, recorded_start, records)
    for sig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
        # killpg is authorized by GROUP OWNERSHIP, not by the root still being alive.
        # `_pgroup_still_ours` proves an identity-verified member of our tree owns this
        # pgid -- the root if it is still there, otherwise a spawn-recorded descendant --
        # and that is exactly the property killpg needs. Demanding the root's identity
        # on top of it suppresses the escalation in the one case it matters: asyncio's
        # watcher collects the leader zombie during the grace, so the SIGKILL round is
        # skipped, and a descendant that forked into the group AFTER the snapshot is
        # reached by neither the group signal nor the recorded sweep. It survives the
        # teardown, which is the leak this whole change exists to stop.
        #
        # The pid-scoped fallback below is different: it names the root itself, so it
        # keeps the root identity check.
        group_ok = pgid is not None and _pgroup_still_ours(
            pgid, pid, recorded_start, records, gated=pid_from_client
        )
        root_ok = root_verified and _root_identity_holds(pid, recorded_start, gated=pid_from_client)
        if group_ok or root_ok:
            try:
                if group_ok:
                    os.killpg(pgid, sig)  # type: ignore[arg-type]
                elif pgid is None:
                    platform_compat.kill_pid(pid, sig)
                else:
                    logger.warning(
                        "_sync_kill_provider: pgid %d holds no verified member "
                        "of pid %d's tree; signalling recorded descendants only",
                        pgid,
                        pid,
                    )
            except ProcessLookupError:
                # The root (or its whole group) is gone. Descendants that escaped it
                # can still be alive, so sweep before deciding this teardown is done.
                pass
            except OSError:
                pass
        _signal_provider_descendants(records, sig)
        if sig == platform_compat.SIGTERM:
            deadline = time.monotonic() + _PROVIDER_TERM_GRACE_SECONDS
            while True:
                # Deliberately NOT reaping here. A zombie owns its pid, and for a
                # group leader that pid IS the pgid, so reaping the root mid-grace
                # frees the number while the SIGKILL escalation below still aims
                # at it -- a pid recycled into a new group leader would take that
                # SIGKILL. The root's exit is read from its zombie state instead,
                # and it is reaped only once no further group signal can be sent.
                if _provider_tree_gone(pid, pgid, records):
                    _reap_provider_root(pid, recorded_start, gated=pid_from_client)
                    logger.warning(
                        "_sync_kill_provider: killed PID %d for leaked provider "
                        "(SIGTERM, scope=%s, descendants=%d)",
                        pid,
                        "pgid" if pgid is not None else "pid",
                        len(records),
                    )
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(_PROVIDER_TERM_POLL_SECONDS)
    _reap_provider_root(pid, recorded_start, gated=pid_from_client)
    logger.warning(
        "_sync_kill_provider: killed PID %d for leaked provider "
        "(SIGKILL, scope=%s, descendants=%d)",
        pid,
        "pgid" if pgid is not None else "pid",
        len(records),
    )


def _tracked_child_has_runtime_identity(child_pid: int) -> bool:
    """Positive argv identity for the tracked sweep's systemd kill arm.

    True only when the live process looks like something Kiro Crew tracks in
    ``kiro_pids.txt``: a managed agent runtime (:data:`_MANAGED_AGENT_MARKERS`),
    an MCP entrypoint (:func:`_is_orphan_mcp`), or a fingerprint-less MCP
    launcher shape (:func:`_is_marked_mcp_launcher` -- callers pair this arm
    with the environ marker). FAIL-CLOSED: unreadable argv is inconclusive and
    returns ``False``, so the sweep prunes without killing. This keeps an
    intentional survivor (a detached process that merely inherited the
    tree-wide ``KIROCREW_SPAWNED`` marker, e.g. a preview server) out of the
    systemd arm's kill authority even if a tracking entry names its PID.
    """
    if _is_managed_agent_process(child_pid):
        return True
    cmdline = _pid_cmdline(child_pid)
    if not cmdline:
        return False
    return _is_orphan_mcp(cmdline) or _is_marked_mcp_launcher(cmdline)


def _cleanup_orphaned_mcp_servers() -> int:
    """Kill tracked child PIDs whose parent kiro-cli session is dead.

    Child entries are stored as ``child_pid:parent_pid[:start-id]`` in
    ``kiro_pids.txt`` (the optional third field is the child's process-start
    identity, recorded at track time).  A child is orphaned when its parent
    PID is dead.  Bare PID lines (sandbox root PIDs) are pruned
    when the process is confirmed dead.

    Zero false positives: we only kill PIDs we tracked, only when the
    specific parent session that spawned them is confirmed dead, and never
    when the start identity proves the PID was recycled.

    A row whose own pid or whose parent is a Windows tree holding an unretired
    cleanup pin is left untouched, kill and prune alike: that drain owns the
    tree, and this file is where the identities it still needs are recorded.
    """
    path = _pid_file_path()
    if not path.exists():
        return 0

    # Hold the lock for the entire read-kill-write cycle so that a concurrent
    # _untrack_child_pids (clean shutdown) cannot remove an entry between our
    # read and our kill decision.  os.kill is non-blocking so lock duration is
    # negligible.
    with _pid_file_lock():
        lines = path.read_text(encoding="utf-8").splitlines()
        killed = 0
        lines_to_remove: set[str] = set()
        # Lazily computed on the first orphan-kill decision: the /proc scan is
        # only worth paying when at least one tracked child has a dead parent.
        accepted_ppids: set[int] | None = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if _windows_pending_child_entry(stripped):
                # An unretired pin owns this tree, so the drain -- not this
                # sweep -- decides when it is gone. Every arm below ends in
                # retiring the row, including the arms that reach it after a
                # kill that RAISED, and this file is the only record that
                # survives the process, so retiring one here would strand a
                # live descendant with nothing left to name it.
                continue
            if ":" not in stripped:
                # Bare PID (sandbox root). Prune if dead.
                try:
                    bare_pid = int(stripped)
                except ValueError:
                    continue
                if not platform_compat.pid_exists(bare_pid):
                    lines_to_remove.add(stripped)
                continue
            parts = stripped.split(":")
            try:
                child_pid = int(parts[0])
                parent_pid = int(parts[1])
            except (ValueError, IndexError):
                continue
            # Optional third field: process-start identity recorded at track
            # time (see _track_child_pids). Legacy two-field entries have none.
            recorded_token = parts[2] if len(parts) >= 3 and parts[2] else None

            # Is the child still alive? (os.kill(pid, 0) would terminate on Windows)
            if not platform_compat.pid_exists(child_pid):
                lines_to_remove.add(stripped)  # confirmed dead — prune
                continue

            # Is the parent session still alive?
            if platform_compat.pid_exists(parent_pid):
                continue  # parent alive (or unknown) — leave child running

            # Parent confirmed dead -> child is orphaned -- kill it, unless
            # the PID names a different incarnation than the one we tracked.
            #
            # The start token (recorded at track time via _pid_start_token)
            # is SUBTRACTIVE evidence only: a live token that differs from
            # the recorded one proves the PID was recycled -> prune without
            # killing. A matching or unreadable token never authorizes the
            # kill by itself -- the record is same-uid-writable, so a forged
            # line must not be able to aim the sweep at an arbitrary
            # process. The kill still requires the reparent heuristic below,
            # exactly the authority the sweep has always had.
            #
            # Heuristic: a true orphan reparented to init or the nearest
            # subreaper (systemd --user) -- the same accepted-parent set
            # _our_orphan_pids uses -- or still shows the dead parent's PID
            # (kill/reparent race). The init and dead-parent arms keep their
            # historical shape on every platform. The systemd arm is
            # stricter: under systemd --user EVERY manager-started service
            # carries the manager's PID as its PPid for its whole life, and
            # the KIROCREW_SPAWNED environ marker is tree-wide (an
            # intentional survivor such as a detached preview server
            # inherits it too), so killing there requires BOTH the marker
            # AND positive runtime argv identity -- the process must look
            # like something this file tracks (managed agent runtime, MCP
            # entrypoint, or marked launcher). Unreadable argv fails closed
            # to prune-without-kill.
            if recorded_token is not None:
                live_token = _pid_start_token(child_pid)
                if live_token is not None and live_token != recorded_token:
                    # Provably a different incarnation -- PID reuse.
                    lines_to_remove.add(stripped)
                    continue
            if accepted_ppids is None:
                accepted_ppids = _accepted_subreaper_pids()
            actual_ppid = platform_compat.get_ppid(child_pid)
            ours = actual_ppid in (1, parent_pid) or (
                actual_ppid in accepted_ppids
                and _env_has_kirocrew_marker(child_pid)
                and _tracked_child_has_runtime_identity(child_pid)
            )
            if not ours:
                # PID was reused by an unrelated process — just prune
                lines_to_remove.add(stripped)
                continue
            try:
                platform_compat.kill_pid(child_pid, platform_compat.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
            lines_to_remove.add(stripped)

        if lines_to_remove:
            kept = [ln for ln in lines if ln.strip() not in lines_to_remove]
            _rewrite_pid_file(path, "\n".join(kept) + "\n" if kept else "")

    return killed


def cleanup_orphaned_sessions(*, narrow_with_leaders: bool = True) -> None:
    """Kill leftover agent-harness processes from a previous gateway run.

    Reads ``kiro_session_pids.txt`` (written at spawn time), validates each
    PID still names the process the entry recorded — by start token, falling
    back to cmdline resemblance for a token-less entry (guards against PID
    recycling) — kills descendants bottom-up, then truncates the file.

    Runs at gateway startup before any new sessions are created, so the file
    contains only PIDs from the previous run.

    Also sweeps orphaned MCP server processes via ``_cleanup_orphaned_mcp_servers``
    which uses the separate ``kiro_pids.txt`` (child:parent[:start-id] format).

    ``narrow_with_leaders`` is forwarded to
    :func:`_prune_stale_session_pid_files`. The gateway passes ``False`` on its
    boot path and in its force-exit handler, so both do exactly the work they
    did before the recycled-pid change; the narrowing applies on the graceful
    shutdown path, which is not spawning sessions.


    Additionally cleans up:
    - Stale ``session_pid_*.txt`` files for processes that no longer exist.
    - Empty directories under ``sessions/`` left by subagents that produced
      no output before timing out.
    """
    # Step 1: Read file under lock (fast I/O only)
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        lines: list[str] = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    # Step 2: Process outside lock (slow: os.kill, _get_child_pids, SIGKILL)
    def _skip_tagged(gw_pid: int, _pid: int) -> bool:
        """Skip if owning gateway is still alive."""
        # pid_exists() returns True on a live PID or one we can't signal
        # (can't tell — preserve), and False only when confirmed dead.
        return platform_compat.pid_exists(gw_pid)

    killed, killed_or_dead, _ = _sweep_pid_entries(
        lines,
        should_skip_tagged=_skip_tagged,
        should_skip_bare=lambda _pid: False,  # startup processes all entries
    )

    # Step 3: Re-read and write under lock — only remove handled entries,
    # preserving entries for alive gateways and un-signalable processes.
    if killed_or_dead:
        _write_back_pid_file(killed_or_dead)

    if killed:
        logger.info("Cleaned up %d orphaned kiro-cli processes", killed)

    # Second pass: sweep MCP servers that escaped process-group kill
    mcp_killed = _cleanup_orphaned_mcp_servers()
    if mcp_killed:
        logger.info("Cleaned up %d orphaned MCP server processes", mcp_killed)

    # Third pass: remove stale session_pid_*.txt files for dead processes
    _prune_stale_session_pid_files(narrow_with_leaders=narrow_with_leaders)
    # Fourth pass: bound the accumulation of session-token mappings, which are
    # keyed by a token hash rather than by a pid and so cannot be probed for
    # liveness at all.
    _prune_stale_session_token_files()

    # Fourth pass: remove empty session workspace dirs (orphaned subagent dirs)
    sessions_dir = config_dir() / "sessions"
    empty_dirs = 0
    if sessions_dir.exists():
        for d in sessions_dir.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                    empty_dirs += 1
                except OSError:
                    pass  # directory became non-empty or was already removed
    if empty_dirs:
        logger.info("Cleaned up %d empty session workspace dirs", empty_dirs)


def _prune_stale_session_pid_files(*, narrow_with_leaders: bool = True) -> int:
    """Remove ``session_pid_<pid>.txt`` mappings whose pid is not that session.

    ``narrow_with_leaders`` decides whether the thread-group-leaders snapshot is
    taken. It costs one ``/proc`` directory read for the whole pass and is what
    catches a pid recycled as a THREAD of a live process, but it is work the
    gateway boot path may not carry: ``no-new-work-on-gateway-boot-path`` names
    orphan sweeps specifically, so the boot caller passes ``False``. The
    narrowing is asked for on the graceful shutdown path instead.

    A live session's pid is both signalable and a thread-group leader, so it is
    retained under either setting, and this pass never touches the shared
    ``kiro_session_pids.txt`` that pass 1 rewrites.

    Returns the number of mapping files removed.
    """
    stale_pid_files = 0
    pid_files = list(config_dir().glob("session_pid_*.txt"))
    # Snapshot the host's thread-group leaders ONCE for the whole sweep — one
    # directory read instead of a synchronous /proc read per mapping.
    #
    # Ordering matters: snapshot AFTER globbing. A pid that starts in the window
    # between the two lands IN the set and is retained; one that exits in that
    # window is absent and is pruned, which is correct. Snapshotting first would
    # invert both.
    leaders = platform_compat.live_thread_group_leaders() if narrow_with_leaders else None
    for pid_file in pid_files:
        try:
            pid = int(pid_file.stem.removeprefix("session_pid_"))
        except ValueError:
            # Malformed filename (e.g. MagicMock leak) -- safe to delete
            logger.debug("Removing malformed pid file: %s", pid_file.name)
            try:
                pid_file.unlink(missing_ok=True)
                stale_pid_files += 1
            except OSError:
                logger.debug("Could not remove malformed pid file: %s", pid_file.name)
            continue
        # os.kill(pid, 0) would terminate the process on Windows — probe instead.
        #
        # The leaders set narrows the liveness test: a dead session's pid can be
        # recycled as a THREAD of an unrelated live process, and a tid satisfies
        # ``pid_exists``, so that probe alone would keep the mapping forever.
        #
        # Resolution of a TOKEN-BEARING mapping is already safe without this:
        # ``session_pid_sig._pid_recycled`` compares the live start token and
        # refuses on a mismatch on both the strict and the lenient path, and a
        # tid's live start token cannot match the dead process's. What this
        # sweep adds is (a) pruning LEGACY token-less mappings, where that
        # guard has no recorded token to compare and callers keep resolving,
        # and (b) bounding accumulation — observed on a host whose pid counter
        # had wrapped: 233 mappings, 1 still naming a 6-day-dead session via a
        # thread of an unrelated process.
        #
        # ``leaders is None`` means the question was unanswerable (non-Linux,
        # unreadable /proc), so it never contributes to a prune.
        if platform_compat.pid_exists(pid):
            if leaders is None or pid in leaders:
                continue
            # Absence from the snapshot selects a CANDIDATE, never the outcome.
            # The snapshot was read before this loop, so a pid recycled since --
            # whose mapping the new owner has already republished at this same
            # path -- is missing from it while naming a LIVE session. Unlinking
            # that mapping would lose a live session's identity, so the decision
            # needs a reading for this pid taken now. Retain on anything but a
            # definite "not a process", and pay the per-pid read only for the
            # few candidates rather than for every mapping.
            if platform_compat.is_thread_group_leader(pid) is not False:
                continue
        pid_file.unlink(missing_ok=True)
        # Remove the HMAC sidecar (session_pid_<pid>.sig) alongside its
        # .txt — a dangling sidecar is harmless (verification requires
        # both) but would accumulate forever.
        pid_file.with_suffix(".sig").unlink(missing_ok=True)
        stale_pid_files += 1
    if stale_pid_files:
        logger.info("Cleaned up %d stale session PID files", stale_pid_files)
    return stale_pid_files


#: How long an unrefreshed ``session_token_<sha256>.sig`` mapping is kept.
#:
#: Age is the ONLY signal available: the filename is a token hash, so unlike a
#: ``session_pid_<pid>`` mapping there is no process to probe. It is nonetheless a
#: safe signal, and for a specific reason rather than because the window is
#: generous: the mapping is republished at the START of every turn
#: (``messaging.identity.publish_turn_identity``), BEFORE anything in that turn can
#: call a tool. So pruning a long-idle session's mapping cannot cost it identity —
#: its next turn rewrites the file before the first resolution — and the window
#: only decides how much disk an abandoned session holds in the meantime.
_SESSION_TOKEN_TTL_SECS = 7 * 24 * 60 * 60


def _prune_stale_session_token_files(ttl_secs: float = _SESSION_TOKEN_TTL_SECS) -> int:
    """Remove ``session_token_*.sig`` mappings unrefreshed for *ttl_secs*.

    This pass is the ONLY retraction path, deliberately: a teardown hook cannot
    reach the case that actually accumulates files — a gateway that dies without
    running one — so an age-based pass is what bounds the directory.

    It runs where its caller runs: :func:`cleanup_orphaned_sessions` is startup and
    graceful-shutdown only, NOT periodic. A long-lived gateway therefore holds one
    mapping per session started since its last boot or clean stop. Stated because
    the TTL below reads like a continuous expiry and is not one.

    A mapping outliving its session is not a forgery risk: it names a session that
    does not exist, and any holder of its token is inside the trust boundary. So
    this is hygiene rather than a control, and it fails soft on every file it
    cannot read or unlink.

    Returns the number of mappings removed.
    """
    removed = 0
    now = time.time()
    try:
        candidates = list(config_dir().glob("session_token_*.sig"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if now - path.stat().st_mtime <= ttl_secs:
                continue
            path.unlink(missing_ok=True)
        except OSError:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - path.name is the sha256 DIGEST of a token, never a token  # noqa: E501
            logger.debug("could not prune identity mapping %s", path.name, exc_info=True)
            continue
        removed += 1
    if removed:
        logger.info("Cleaned up %d stale session-token mappings", removed)
    return removed


def retire_windows_tree_tracking(root_pid: int) -> None:
    """Retire tracking under the cleanup owner's exact root pin, or fail closed."""
    if not _untrack_pid(root_pid) or not _untrack_session_pid(root_pid):
        raise OSError("Windows tree tracking retirement did not complete")
    unregister_protected_pid(root_pid)


def cleanup_orphaned_session_roots() -> int:
    """Advance failed exact-handle drains, then reap dead-gateway PID records.

    On Windows, process-local cleanup state is attempted first. It owns exact
    root and intermediary handles transferred by failed runtime/client teardown;
    this pass never reconstructs that authority from a PID. The ordinary
    ``kiro_session_pids.txt`` crash-orphan sweep then keeps its existing rules.

    Reads ``kiro_session_pids.txt`` entries (format
    ``<gateway_pid>:<child_pid>[:<start_token>]``), checks if the gateway PID is
    alive, and for dead gateways validates the child PID still names the process the
    entry recorded before issuing SIGKILL. What AUTHORIZES the signal is
    ``_is_managed_agent_process`` — the argv gate, applied to every entry whatever it
    recorded. The recorded start token only ever SUBTRACTS: a live value that differs
    prunes without a signal, an unreadable one retains the entry, and a value that
    matches settles identity well enough to skip the weaker PPid reparent test. A
    token-less entry is judged by the argv gate plus that PPid test.

    Called periodically from ``session.py``'s ``_cleanup_loop``. Returns the
    number of exact-handle trees completed plus orphaned processes killed.
    """

    # The drain calls ``retire_windows_tree_tracking`` itself, under the
    # pending-state lock and while the exact root handle still pins this
    # incarnation, so the reuse window is already closed for every caller and
    # this sweep adds no retirement of its own. A second retirement here would be
    # a second chance to fail a write that has already succeeded, holding a fully
    # drained tree's handles for another tick.
    completed_roots = platform_compat.retry_pending_windows_process_trees()
    killed = len(completed_roots)
    path = _session_pid_file_path()
    if not path.exists():
        return killed

    with _session_pid_file_lock():
        lines = path.read_text(encoding="utf-8").splitlines()

    if not lines:
        return killed

    my_gw_pid = os.getpid()
    entries_to_remove: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue

        # ``gw:pid`` (legacy) or ``gw:pid:start_token`` (recycle guard) — see
        # _track_session_pid. A bare split(":", 1) would leave "pid:token" in
        # parts[1] and int() it into a prune, silently discarding every
        # token-bearing entry instead of sweeping it.
        parts = stripped.split(":")
        recorded_token: str | None = None
        token_settled = False
        if len(parts) == 3:
            recorded_token = parts[2] or None
        elif len(parts) != 2:
            entries_to_remove.add(stripped)
            continue
        try:
            gw_pid = int(parts[0])
            child_pid = int(parts[1])
        except (ValueError, IndexError):
            entries_to_remove.add(stripped)
            continue

        if gw_pid <= 0 or child_pid <= 0:
            entries_to_remove.add(stripped)
            continue

        # Skip entries owned by the current (live) gateway
        if gw_pid == my_gw_pid:
            continue

        # Check if the owning gateway is still alive. Route through
        # platform_compat: os.kill(pid, 0) would *terminate* the process on
        # Windows, so use the three-way liveness probe instead.
        gw_liveness = platform_compat.pid_liveness(gw_pid)
        if gw_liveness == platform_compat.PID_ALIVE:
            continue  # gateway alive — its responsibility
        if gw_liveness == platform_compat.PID_UNSIGNALABLE:
            continue  # can't determine — skip
        # gw_liveness == PID_DEAD — orphan candidate

        # Gateway is dead. A process-local pending tree still owns its tracking.
        if platform_compat.windows_tree_cleanup_pending(child_pid, recorded_token):
            continue
        # Gateway is dead. Check if the child PID is still alive.
        child_liveness = platform_compat.pid_liveness(child_pid)
        if child_liveness == platform_compat.PID_DEAD:
            # Already dead — just prune the entry
            entries_to_remove.add(stripped)
            continue
        if child_liveness == platform_compat.PID_UNSIGNALABLE:
            continue  # can't signal — skip

        # Child is alive. Two independent questions follow, in this order: is this
        # PID provably a DIFFERENT incarnation (the entry's own start token, which can
        # only subtract), and is it a harness at all (the argv gate, which is what
        # authorizes the signal and runs for every entry).

        # Strongest PID-reuse guard FIRST: the entry recorded the child's start
        # token at spawn (see _pid_start_token). A MISMATCH means this PID now
        # names a DIFFERENT process — prune, never kill. An unreadable live
        # token is "identity unknown", not a mismatch: retain the entry so a
        # live genuine orphan is not untracked (and thus leaked forever) on one
        # transient probe failure; the next sweep retries.
        #
        # A token that MATCHES is positive proof this PID is still the process
        # we spawned, so it settles identity on its own and the weaker PPid
        # heuristic below MUST NOT be allowed to veto it. That ordering is
        # load-bearing: an orphan does not always reparent to init. A child
        # placed in its own cgroup scope by the service manager reparents to
        # that *user manager*, which is a subreaper, so its PPid is neither 1
        # nor the dead gateway's. Running the PPid check first classified every
        # such orphan as "PID recycled" and pruned its tracking entry WITHOUT
        # killing it — sparing the process and then forgetting it, so no later
        # sweep could ever reap it.
        if recorded_token is not None:
            live_token = _pid_start_token(child_pid)
            if live_token is not None and live_token != recorded_token:
                entries_to_remove.add(stripped)
                continue
            if live_token is None:
                continue  # identity unknown — retain entry, retry next sweep
            token_settled = True

        # The argv test authorizes, for every entry. A matching token does not
        # substitute for it: the tracking file is same-uid-writable, so its contents
        # may not confer a capability.
        if not _is_managed_agent_process(child_pid):
            if token_settled:
                # The stronger evidence decides the entry's FATE, exactly as in the
                # periodic sweep: a settled token proves this PID still names the
                # process the entry recorded, so pruning would spare the process and
                # discard the only record any sweep could find it by. That is the
                # unreclaimable state, not a conservative one. Retain and re-examine
                # next start; the deferred per-platform identity work is what closes
                # the gap for good.
                logger.debug(
                    "Startup reclaim: PID %s is unrecognised by argv but its token "
                    "settled identity - retaining the entry",
                    child_pid,
                )
                continue
            # PID was recycled by an unrelated process — prune entry
            entries_to_remove.add(stripped)
            continue

        # The PPid test is a SECOND recycle guard, and a settled token makes it
        # unnecessary. That ordering predates this change and is load-bearing: an
        # orphan does not always reparent to init, because a child placed in its own
        # cgroup scope by the service manager reparents to that user manager, which is
        # a subreaper — so its PPid is neither 1 nor the dead gateway's. Running PPid
        # over a settled token classified every such orphan as recycled and pruned its
        # entry WITHOUT killing it: spared, then forgotten. platform_compat.get_ppid
        # returns -1 on failure (Linux /proc, macOS libproc, Windows snapshot).
        if not token_settled:
            try:
                actual_ppid = platform_compat.get_ppid(child_pid)
            except Exception:
                actual_ppid = -1

            if actual_ppid not in (1, gw_pid, -1):
                # PPid is something else entirely — PID was reused, prune
                entries_to_remove.add(stripped)
                continue

        # Confirmed orphan: kill the process tree
        total_killed, root_killed = _kill_pid_tree(child_pid)
        killed += total_killed
        if root_killed:
            entries_to_remove.add(stripped)
        else:
            # Check if root died between our signal and now
            if not platform_compat.pid_exists(child_pid):
                entries_to_remove.add(stripped)

    # Write back cleaned entries
    if entries_to_remove:
        _write_back_pid_file(entries_to_remove)

    if killed:
        logger.info(
            "cleanup_orphaned_session_roots: settled %d session roots "
            "(completed exact-handle tree drains plus orphan kills)",
            killed,
        )

    return killed


def _track_pid(pid: int) -> None:
    """Append a PID to the tracking file."""
    with _pid_file_lock():
        path = _pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{pid}\n")


def _track_child_pids(pids: Mapping[int, object], parent_pid: int = 0) -> None:
    """Append descendant PIDs to the tracking file as ``child:parent[:start-id]``.

    The third field is the child's process-start identity
    (:func:`_pid_start_token` -- colon-free, in-process and non-blocking on
    every platform), recorded so the orphan sweep can prove a PID was
    recycled before killing it. A child whose identity cannot be read at
    track time is written in the legacy two-field shape.
    """
    if not pids:
        return
    with _pid_file_lock():
        path = _pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
        with open(path, "a", encoding="utf-8") as f:
            for pid in pids:
                key = f"{pid}:{parent_pid}"
                if any(e == key or e.startswith(key + ":") for e in existing):
                    continue
                token = _pid_start_token(pid)
                entry = f"{key}:{token}" if token else key
                f.write(f"{entry}\n")
                existing.add(entry)


def _recorded_start_token(record: object) -> str | None:
    """The start identity a caller already captured, as a file field.

    The write path must NEVER read a live pid's identity for itself. Reading it
    at write time reopens the reuse window the caller closed: a descendant that
    exited and had its number taken would be written with the STRANGER's token,
    and the sweep's guard compares live against recorded -- both the stranger's,
    so they match, and it kills an unrelated process. A caller earns the right to
    write a token by capturing it while the pid was confirmed to be its own; this
    only carries that value through.

    Accepts the ``(start_id, basename)`` record shape and the legacy scalar. A
    value that cannot be a field -- absent, or carrying the ``:`` the format
    separates on -- degrades to ``None``, which writes the two-field shape and
    leaves the sweep with no token to match on.
    """
    value = record[0] if isinstance(record, tuple) and record else record
    if value is None:
        return None
    token = str(value)
    return token if token and ":" not in token else None


def _replace_child_pids(
    pids: Mapping[int, object], parent_pid: int, *, drop: Iterable[int] = ()
) -> bool:
    """Rewrite this parent's lines for the children the caller names.

    The whole-set counterpart to :func:`_track_child_pids`, for a caller that
    re-enumerates its tree and knows the complete answer each time. It is what
    :func:`~kiro_crew.acp.runtime.AcpRuntime._snapshot_descendants` needs and an
    append cannot give: a pid whose start identity changed already has a line
    under the same ``child:parent`` key, and the append dedupes on that prefix,
    so the stale identity would survive. Removing the line and appending a fresh
    one is two writes, and a caller cannot tell that the first one failed --
    ``_untrack_child_pids`` discards the rewrite's answer, by design, because
    pruning a dead entry is self-retrying. Replacing a live entry is not.

    One lock, one atomic rewrite, and the answer is returned.

    **Only the caller's own children are touched.** A line is removed when its
    child pid is named in *pids* or in *drop* -- never merely because it sits
    under this ``parent_pid``. A root pid is reused like any other: a descendant
    that outlived an earlier runtime holding this number is still tracked under
    it, and wiping the block by owner alone would untrack that survivor
    permanently, which is the leak this file exists to prevent. Lines under
    another parent, and the bare root lines, are likewise untouched.

    *drop* names the children to remove without rewriting -- the ones the caller
    has confirmed gone. Passing neither *pids* nor *drop* writes nothing.

    Field 3 is :func:`_recorded_start_token` of the mapping's VALUE, so the
    identity written is the one the caller captured under confirmation. Same
    two-field fallback as :func:`_track_child_pids`, so the file stays one
    format.

    Returns ``False`` when the rewrite failed, so the caller can leave its own
    in-memory state alone and retry on its next pass.
    """
    if not parent_pid:
        return False
    owned = {str(p) for p in pids} | {str(p) for p in drop}
    if not owned:
        return True
    with _pid_file_lock():
        path = _pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        kept: list[str] = []
        for raw in lines:
            entry = raw.strip()
            if not entry:
                continue
            fields = entry.split(":")
            if len(fields) >= 2 and fields[1] == str(parent_pid) and fields[0] in owned:
                continue
            kept.append(entry)
        for pid, record in pids.items():
            key = f"{pid}:{parent_pid}"
            token = _recorded_start_token(record)
            kept.append(f"{key}:{token}" if token else key)
        return _rewrite_pid_file(path, "\n".join(kept) + "\n" if kept else "")


def _untrack_child_pids(pids: Mapping[int, object]) -> None:
    """Remove descendant PIDs from the tracking file."""
    if not pids:
        return
    to_remove = {str(p) for p in pids}
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [
            ln for ln in lines if ":" not in ln.strip() or ln.strip().split(":")[0] not in to_remove
        ]
        _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


def _untrack_pid(pid: int) -> bool:
    """Remove a PID, reporting whether its tracking file was updated."""
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return True
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if ln.strip() != str(pid)]
        return _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


def _untrack_session_pid(pid: int) -> bool:
    """Remove this gateway's ``<gw_pid>:<pid>`` entry from the session PID
    tracking file.  Called on clean provider shutdown so the periodic
    orphan sweep doesn't race against legitimate still-running kiro-cli
    processes whose in-memory session entry has transiently gone away
    (e.g. during compaction/reset/replace). Return whether the write succeeded.
    """
    prefix = f"{os.getpid()}:{pid}"
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if not path.exists():
            return True
        lines = path.read_text(encoding="utf-8").splitlines()
        # Match both the legacy ``gw:pid`` form and the token-bearing
        # ``gw:pid:token`` form (see _track_session_pid).
        lines = [
            ln for ln in lines if ln.strip() != prefix and not ln.strip().startswith(prefix + ":")
        ]
        return _rewrite_pid_file(path, "\n".join(lines) + "\n" if lines else "")


# ── Sweep-protected PIDs ──────────────────────────────────────────────────
# Live agent-process PIDs tracked in the PID file but NOT registered as
# SessionMap sessions (e.g. app-managed worker pools / shared ACP runtimes).
# The periodic orphan sweep consults _protected_pids() to avoid killing them.
_PROTECTED_PIDS: set[int] = set()
_PROTECTED_LOCK = threading.Lock()


def register_protected_pid(pid: int) -> None:
    """Shield a live agent-process PID from the periodic orphan sweep.

    For app-managed worker pools whose processes are tracked in the PID file but
    not registered as SessionMap sessions. Pair every call with
    ``unregister_protected_pid`` on worker shutdown/replacement."""
    if isinstance(pid, int) and pid > 0:
        with _PROTECTED_LOCK:
            _PROTECTED_PIDS.add(pid)


def unregister_protected_pid(pid: int) -> None:
    """Drop a PID from the sweep-protected set (worker shut down / replaced)."""
    with _PROTECTED_LOCK:
        _PROTECTED_PIDS.discard(pid)


def _protected_pids() -> set[int]:
    with _PROTECTED_LOCK:
        return set(_PROTECTED_PIDS)


# ── Untracked orphan MCP sweep (defense-in-depth) ──────────
# Catches KiroCrew-spawned MCP subtrees that escaped PID-file tracking.
# Split into find + kill so the caller can re-verify active PIDs between phases.

_ORPHAN_SWEEP_MAX_KILLS = 30
_ORPHAN_MIN_AGE_SECONDS = 120  # Never reap processes younger than this

# Dedicated, more conservative age floor for the WORK-process orphan class
# (agent-spawned pytest/build/shim subtrees identified purely by the
# KIROCREW_SPAWNED environ marker — see _is_sweepable_orphan_work). Work
# processes get a much more generous grace than the 120s MCP floor: a
# long-running legitimate build or test run whose agent briefly detaches must
# not be raced, and the floor also guarantees a just-detached spawn is never
# swept before its agent could have re-attached tracking.
_ORPHAN_WORK_MIN_AGE_SECONDS = 600

# Execnet's popen-worker bootstrap: the single ``-c`` payload pytest-xdist
# workers run under (verified empirically against pytest-xdist 3.x). Matched
# as an EXACT argv element, never as a substring.
_XDIST_BOOTSTRAP = b"import sys;exec(eval(sys.stdin.readline()))"


def _work_sweep_cmdline_is_test_runner(cmdline: bytes) -> bool:
    """Structural test-runner match on parsed argv — never substring.

    A test runner is never a legitimate long-lived daemon, unlike other
    marked-but-detached processes an agent may deliberately leave running
    (a preview server, for instance) — so this is the positive shape gate for
    the work-orphan sweep. Matching is structural to avoid path-fragment
    false positives (``node /work/pytest-dashboard/server.js`` must NOT
    match). Exactly three shapes qualify:

    * argv0 basename is exactly ``pytest`` (a venv console script), or
    * an adjacent ``-m pytest`` argument pair (``python -m pytest ...``), or
    * a ``-c`` argument whose payload is exactly execnet's worker bootstrap
      (:data:`_XDIST_BOOTSTRAP`).
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    if len(args) <= 1:
        # Space-joined fallback (ps output); NUL-split is canonical on Linux.
        args = [a for a in cmdline.split(b" ") if a]
    if not args:
        return False
    if args[0].rsplit(b"/", 1)[-1] == b"pytest":
        return True
    for i in range(len(args) - 1):
        if args[i] == b"-m" and args[i + 1] == b"pytest":
            return True
        if args[i] == b"-c" and args[i + 1] == _XDIST_BOOTSTRAP:
            return True
    return False


# A candidate PID can exit between the /proc (or ps) snapshot and the per-PID
# probe. Linux surfaces that as FileNotFoundError/ProcessLookupError reading
# /proc/<pid>/cmdline; macOS as a non-zero `ps -p <pid>` exit. All three mean
# "already gone", which is the sweep's goal — not a failure worth a traceback.
_PID_VANISHED_ERRORS = (
    FileNotFoundError,
    ProcessLookupError,
    subprocess.CalledProcessError,
)

# Entrypoints that positively identify a KiroCrew-spawned MCP/worker process.
# Each marker MUST be unique to a process KiroCrew itself launches — the sweep
# SIGKILLs any user-owned orphan that matches, so a marker naming a server the
# core does not spawn would reap an unrelated process. The upstream project's
# reaper also lists an enterprise-only MCP server it manages, but this public
# fork never spawns that server (the CPP companion contributes it, not the
# core), so that marker is deliberately omitted here.
_MCP_ENTRYPOINT_MARKERS = (
    b"kirocrew_sandbox_",  # sandbox wrapper script (session-spawned)
    b"kiro_crew.mcp_gateway.stub",  # gateway pool worker (not gatewayd itself)
)

# Gateway/CLI entrypoints — these are peer gateways, never orphan MCP targets.
# Checked BEFORE _MCP_ENTRYPOINT_MARKERS to prevent prefix overlap.
_GATEWAYD_MODULE = b"kiro_crew.mcp_gateway.gatewayd"
_GATEWAY_MARKERS = (
    _GATEWAYD_MODULE,
    b"kiro_crew.cli",
    b"kiro_crew.__main__",
)

# MCP launcher cmdline shapes that carry NO KiroCrew fingerprint (a user's own
# shell can produce identical cmdlines), so matching them requires the
# ``KIROCREW_SPAWNED`` environ marker as positive identity — the public fork's
# only fingerprint-less launcher is the public ``@playwright/mcp`` server, which
# runs as ``npx @playwright/mcp`` -> node (see ``mcp_playwright_proxy``): neither
# its argv0 (``npx``/``node``) nor its args mention KiroCrew, so a grandchild
# escaping the probe/session tree evades the cmdline-fingerprint sweep entirely.
_MARKED_MCP_LAUNCHER_MARKERS = (
    b"@playwright/mcp",  # ``npx @playwright/mcp`` (npx shim + node server)
    b"mcp start-server",  # generic ``<launcher> mcp start-server <name>`` shims
)

# ── Stranded playwright-cli browser daemon ───────────────────────────────────
# playwright-core spawns its browser daemon as
#   ``node <...>/playwright-core/lib/entry/cliDaemon.js <session-name> [flags]``
# with ``detached: true`` and no ``env`` override (cli-client/session.js
# ``startDaemon``). Two consequences make it its own orphan class:
#
# * detached => it is its own SESSION and PROCESS-GROUP leader, so it is
#   invisible to the teardown child snapshot, to ``kill_process_tree``, and to
#   the SID-based ownership test the work class uses (its SID is its own pid).
# * no env override => it inherits the spawning agent's environment verbatim,
#   so its EXEC-TIME environ carries both ``KIROCREW_SPAWNED`` and the
#   generated ``PLAYWRIGHT_CLI_SESSION``. Exec-time environ is kernel-held and
#   immutable after exec, so it is ownership evidence no process can forge for
#   another -- unlike any on-disk registry or claim file, which a same-UID
#   agent can write.
#
# Deliberately NOT keyed on the socket path the way the gatewayd class is:
# ``Session._connect`` UNLINKS the socket whenever a connect fails, so an
# absent socket means "a client already cleaned up after a refused connect",
# not "the daemon is unreachable" -- and the daemon holds its listening fd
# regardless, so absence proves nothing about the browser tree.
_BROWSER_DAEMON_ENTRY = b"cliDaemon.js"

#: Must track :data:`kiro_crew.browser_cli.launch.SESSION_ENV`. Duplicated as a
#: literal rather than imported because ``session_pid`` is imported early by
#: ``acp.runtime`` and must not pull the browser package's import graph onto
#: that path; a test asserts the two stay equal.
_BROWSER_SESSION_ENV = "PLAYWRIGHT_CLI_SESSION"

#: Generated-session prefix from ``browser_cli.launch`` (``kc-<8hex>``).
_BROWSER_SESSION_PREFIX = b"kc-"

# Grace given to a TERMed browser-daemon GROUP before escalating to SIGKILL.
# Chromium exits on TERM within a second or two; this leaves room for a profile
# flush without letting a wedged tree hold the sweep's budget.
_BROWSER_DAEMON_TERM_GRACE_SECONDS = 5.0


def _is_generated_browser_session(name: bytes) -> bool:
    """True for a Kiro-Crew-generated ``kc-<8hex>`` session name.

    Mirrors ``browser_cli.launch._session_leaf``. ONLY generated names are
    ever sweepable: an operator who named a session (``default``, ``chrome``,
    an ``attach`` workflow) owns its lifetime, and the ``kc-`` prefix is
    reserved precisely so the two populations cannot be confused.
    """
    if not name.startswith(_BROWSER_SESSION_PREFIX):
        return False
    leaf = name[len(_BROWSER_SESSION_PREFIX) :]
    return len(leaf) == 8 and all(c in b"0123456789abcdef" for c in leaf)


def _browser_daemon_session_arg(cmdline: bytes) -> bytes | None:
    """Generated session name from a cliDaemon cmdline, or ``None``.

    The daemon's argv is ``node <entry>/cliDaemon.js <session-name> [flags]``,
    so the name is the element immediately following the entry script --
    matched on the script's BASENAME so an npx/global/vendored install path
    all resolve. NUL-separated argv ONLY: the space-joined ``ps`` fallback
    cannot delimit a path containing spaces, and a mis-split argv could pair
    the script with the wrong token, so anything without NULs fails closed.
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    if len(args) <= 1:
        return None
    for index, arg in enumerate(args[:-1]):
        if arg.rsplit(b"/", 1)[-1] == _BROWSER_DAEMON_ENTRY:
            name = args[index + 1]
            return name if _is_generated_browser_session(name) else None
    return None


def _env_value(pid: int, key: str, proc_root: Path | None = None) -> bytes | None:
    """Exec-time environment value for *key* in *pid*, or ``None`` if unset.

    Deliberately PROPAGATES ``OSError`` instead of swallowing it like
    :func:`_env_has_kirocrew_marker`: the callers here need to tell "read
    said the key is absent" apart from "the read failed", because those two
    outcomes must fail closed in OPPOSITE directions -- an absent owner
    permits a kill, an unreadable one normally forbids it. Linux-only; returns
    ``None`` elsewhere so every caller fails closed off Linux. *proc_root* is a
    fixture seam for tests and never changes the production ``/proc`` root.
    """
    if sys.platform != "linux":
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    prefix = key.encode() + b"="
    environ = (root / str(pid) / "environ").read_bytes()
    for item in environ.split(b"\x00"):
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


_BROWSER_PLAUSIBLE_OWNER_NAMES = frozenset(
    {
        "bash",
        "claude",
        "codex",
        "dash",
        "fish",
        "java",
        "kas",
        "kiro-cli",
        "kirocrew",
        "launcher",
        "node",
        "opencode",
        "playwright-cli",
        "sh",
        "zsh",
    }
)
_BROWSER_PLAUSIBLE_OWNER_PREFIXES = ("chrome", "chromium", "kiro-", "playwright", "python")


def _browser_process_name_is_plausible(name: str) -> bool:
    """Whether an unreadable process could own generated browser tooling."""
    return name in _BROWSER_PLAUSIBLE_OWNER_NAMES or name.startswith(
        _BROWSER_PLAUSIBLE_OWNER_PREFIXES
    )


def _browser_session_owner_alive(
    pid: int,
    session: bytes,
    *,
    proc_root: Path | None = None,
) -> bool:
    """True while any live process OUTSIDE *pid*'s own tree holds *session*.

    This is the ownership proof, and it is drawn entirely from the kernel.
    Kiro Crew injects the generated ``PLAYWRIGHT_CLI_SESSION`` into exactly
    one spawned agent process, so that value in a live process's EXEC-TIME
    environ means the browser still has an owner. Scanning the whole process
    table (not a manager-local set) is what makes a peer gateway sharing this
    data home see its own live sessions and protect them.

    The daemon's own tree is excluded by SID: it is spawned ``detached``, so
    it is its own session leader and every Chromium child inherits that SID --
    those inherit the variable too and must not be mistaken for owners.

    FAIL-CLOSED to "alive" for an unreadable ``/proc`` listing, process stat,
    process name, or plausible owner's environ. A positively named non-owner in
    a stable different cgroup from the daemon cannot be part of the spawn tree
    that inherited its generated browser session, so its unreadable environ
    does not veto the scan. A plausible process, same cgroup, unreadable cgroup,
    or changing cgroup keeps the daemon. A process that vanishes is safe to skip.
    """
    if sys.platform != "linux":
        return True
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        entries = [entry for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return True
    my_uid = os.getuid()
    for entry in entries:
        try:
            other = int(entry.name)
        except ValueError:
            continue
        if other == pid:
            continue
        try:
            if entry.stat().st_uid != my_uid:
                continue
        except _PID_VANISHED_ERRORS:
            continue
        except OSError:
            return True
        if _linux_pid_sid(other, proc_root) == pid:
            continue  # the daemon's own detached tree, not an owner
        try:
            owner_session = _env_value(other, _BROWSER_SESSION_ENV, proc_root)
            if owner_session == session:
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=keep reason=matching_session",
                    pid,
                    other,
                )
                return True
        except _PID_VANISHED_ERRORS:
            continue
        except OSError:
            process_name = platform_compat.linux_process_name(other, proc_root=root)
            if process_name is None:
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=keep reason=process_name_unreadable",
                    pid,
                    other,
                )
                return True
            if _browser_process_name_is_plausible(process_name):
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=keep reason=plausible_owner_unreadable",
                    pid,
                    other,
                )
                return True
            cgroups_match = platform_compat.process_cgroups_match(
                other,
                pid,
                proc_root=root,
            )
            if cgroups_match is False:
                logger.debug(
                    "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                    "decision=ignore reason=different_cgroup_unreadable",
                    pid,
                    other,
                )
                continue
            reason = (
                "same_cgroup_unreadable" if cgroups_match is True else "cgroup_identity_unreadable"
            )
            logger.debug(
                "browser_session_owner_probe daemon_pid=%s candidate_pid=%s "
                "decision=keep reason=%s",
                pid,
                other,
                reason,
            )
            return True
    return False


def _browser_sweep_decision(pid: int, *, sweep: bool, reason: str) -> bool:
    """Log and return one structured browser-daemon sweep verdict."""
    logger.debug(
        "browser_daemon_sweep pid=%s decision=%s reason=%s",
        pid,
        "sweep" if sweep else "keep",
        reason,
    )
    return sweep


def _is_sweepable_orphan_browser_daemon(
    pid: int,
    cmdline: bytes,
    age_seconds: float,
    *,
    proc_root: Path | None = None,
) -> bool:
    """Fifth positive-identity path: a browser daemon whose owner is gone.

    Positive identity is the conjunction of:

    1. a structural cliDaemon argv carrying a GENERATED ``kc-<8hex>`` session
       name (:func:`_browser_daemon_session_arg`, NUL-argv only) -- an
       operator-named session is structurally excluded and never signalled;
    2. that same name in the process's exec-time environ, which ties this
       daemon to a name Kiro Crew itself generated rather than one an agent
       passed with ``-s=``;
    3. the ``KIROCREW_SPAWNED`` environ marker, proving Kiro Crew spawned the
       tree (:func:`_env_has_kirocrew_marker`, Linux-only, fail-closed);
    4. NO live process outside the daemon's own tree still holding that
       session (:func:`_browser_session_owner_alive`);
    5. age past :data:`_ORPHAN_WORK_MIN_AGE_SECONDS` -- the generous work-class
       floor, not the 120s MCP one, so a daemon whose agent is mid-spawn or
       briefly detached is never raced.

    Every signal is a kernel fact (argv, exec-time environ, SID, process
    liveness). Nothing here reads agent-writable filesystem state, which is
    what would make a reaper unsafe. *proc_root* exists only for fixture-owned
    process-table tests.
    """
    if not cmdline:
        return False  # kernel thread / zombie -- nothing meaningful to kill
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    session = _browser_daemon_session_arg(cmdline)
    if session is None:
        return False
    if age_seconds < _ORPHAN_WORK_MIN_AGE_SECONDS:
        return _browser_sweep_decision(pid, sweep=False, reason="below_age_floor")
    try:
        daemon_session = _env_value(pid, _BROWSER_SESSION_ENV, proc_root)
        if daemon_session != session:
            return _browser_sweep_decision(
                pid,
                sweep=False,
                reason="session_environment_mismatch",
            )
    except OSError:
        return _browser_sweep_decision(
            pid,
            sweep=False,
            reason="daemon_environment_unreadable",
        )
    if not _env_has_kirocrew_marker(pid, proc_root):
        return _browser_sweep_decision(pid, sweep=False, reason="spawn_marker_absent")
    if _browser_session_owner_alive(pid, session, proc_root=proc_root):
        return _browser_sweep_decision(
            pid,
            sweep=False,
            reason="owner_alive_or_inconclusive",
        )
    return _browser_sweep_decision(
        pid,
        sweep=True,
        reason="no_owner_outside_daemon_tree",
    )


def _accepted_subreaper_pids() -> set[int]:
    """PIDs an orphan may legitimately reparent to: init plus same-uid systemd.

    An orphaned process reparents to init (pid 1) or the nearest subreaper --
    under a ``systemd --user`` gateway that is the user manager process, not
    pid 1. On Linux this scans ``/proc`` for same-uid processes whose comm is
    ``systemd``; elsewhere only init/launchd (pid 1) is a reparent target.
    Single source of truth shared by :func:`_our_orphan_pids` and the PID-reuse
    guard in :func:`_cleanup_orphaned_mcp_servers`, so the two reapers agree on
    what an orphan's parent may look like -- a guard accepting only pid 1 would
    misread a systemd-reparented orphan as PID reuse and prune it without
    killing.

    We deliberately do NOT include the gateway's launcher ppid: doing so would
    widen the candidate set to the launcher's other live children (peer
    processes from the same shell/tmux/supervisor), adding wrong-kill surface
    with no orphan-reaping benefit.
    """
    accepted: set[int] = {1}
    if sys.platform != "linux":
        return accepted
    try:
        my_uid = os.getuid()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != my_uid:
                    continue
                # Detect systemd --user (user-session subreaper)
                if (entry / "comm").read_text().strip() == "systemd":
                    accepted.add(int(entry.name))
            except (OSError, ValueError):
                continue
    except Exception:
        # Callers include the startup sweep, which has no catch-all of its
        # own: degrade to the init-only set rather than aborting the sweep.
        logger.warning("_accepted_subreaper_pids /proc scan failed", exc_info=True)
    return accepted


def _our_orphan_pids() -> list[int]:
    """PIDs owned by current user whose parent is init (pid 1) or systemd --user.

    POSIX-only: relies on ``os.getuid`` and either ``/proc`` (Linux) or ``ps``
    (macOS). On Windows there is no init/systemd concept and no ``os.getuid``;
    the orphan-sweep is inactive there and returns an empty list.
    """
    if platform_compat.IS_WINDOWS:
        return []
    my_uid = os.getuid()
    # Pass 1 detects the accepted reparent targets (init + systemd --user
    # subreapers); pass 2 classifies orphans (needs the complete subreaper set
    # before any child can be matched against accepted_ppids).
    accepted_ppids = _accepted_subreaper_pids()
    try:
        if sys.platform == "linux":
            result: list[int] = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != my_uid:
                        continue
                    pid = int(entry.name)
                    for ln in (entry / "status").read_text().splitlines():
                        if ln.startswith("PPid:"):
                            parts = ln.split(maxsplit=1)
                            if len(parts) < 2:
                                break
                            if int(parts[1]) in accepted_ppids:
                                result.append(pid)
                            break
                except (OSError, ValueError, IndexError):
                    pass
            return result
        else:
            result = []
            out = subprocess.check_output(
                ["ps", "-o", "pid=,ppid=", "-U", str(my_uid)],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            for ln in out.decode().splitlines():
                parts = ln.split()
                if len(parts) == 2 and parts[0].isdigit():
                    pid, ppid = int(parts[0]), int(parts[1])
                    if ppid in accepted_ppids:
                        result.append(pid)
            return result
    except Exception:
        logger.warning("_our_orphan_pids failed", exc_info=True)
    return []


def _is_orphan_mcp(cmdline: bytes) -> bool:
    """True if cmdline matches a KiroCrew MCP entrypoint (not a peer gateway)."""
    # Exclude peer gateways — they're not orphan MCP targets
    if any(marker in cmdline for marker in _GATEWAY_MARKERS):
        return False
    # Parse argv: null-separated on Linux, space-separated on macOS ps output
    args = cmdline.split(b"\x00")
    if len(args) == 1:
        args = cmdline.split(b" ")
    argv0 = args[0].rsplit(b"/", 1)[-1]
    # A sandbox/worker script exec'd directly via its shebang puts the script
    # (not a python interpreter) in argv0 — match the marker there too so such
    # orphans aren't missed.
    if any(marker in argv0 for marker in _MCP_ENTRYPOINT_MARKERS):
        return True
    # Otherwise require python interpreter + known entrypoint in remaining args
    if b"python" not in argv0:
        return False
    return any(any(marker in a for marker in _MCP_ENTRYPOINT_MARKERS) for a in args[1:])


def _is_marked_mcp_launcher(cmdline: bytes) -> bool:
    """True if cmdline looks like a fingerprint-less MCP launcher (e.g. ``npx``).

    NOT sufficient on its own — the caller MUST pair this with
    :func:`_env_has_kirocrew_marker` because a user's own shell produces
    identical cmdlines. NULs are normalized to spaces first so the multi-token
    markers match both the Linux NUL-separated ``/proc`` form and the macOS
    space-separated ``ps`` form.
    """
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    return any(marker in normalized for marker in _MARKED_MCP_LAUNCHER_MARKERS)


def _read_env_has_kirocrew_marker(pid: int, proc_root: Path | None = None) -> bool | None:
    """Tri-state read of *pid*'s ``KIROCREW_SPAWNED`` environment marker.

    ``None`` distinguishes an unreadable environment from a readable one that
    lacks the marker. An explicit *proc_root* permits fixture-owned process
    tables on every host and always takes the ``/proc`` path, so a fixture's
    verdict never depends on the host it runs on.

    Two production arms, one per platform that HAS a same-uid environ oracle:

    * Linux reads ``/proc/<pid>/environ``.
    * macOS reads the same exec-time environment out of ``sysctl
      KERN_PROCARGS2`` (:func:`platform_compat.darwin_process_environ`) -- the
      kernel record ``ps -E`` reads, answered for a same-uid process with no
      entitlement and no elevated privilege, and already relied on in this
      codebase for argv.

    Both are in-process kernel reads of a copy fixed at exec, which is what
    makes the marker ownership evidence rather than a claim: a same-uid process
    can write any file and set any argv, but it cannot alter another process's
    exec-time environment. Every other platform has no such oracle and stays
    ``None``, which the boolean wrapper turns into a refusal.
    """
    needle = f"{KIROCREW_SPAWNED_ENV}={KIROCREW_SPAWNED_VALUE}".encode()
    if proc_root is None:
        if sys.platform == "darwin":
            entries = platform_compat.darwin_process_environ(pid)
            return None if entries is None else needle in entries
        if sys.platform != "linux":
            return None
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        environ = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    return needle in environ.split(b"\x00")


def _env_spawn_instance(pid: int, proc_root: Path | None = None) -> str | None:
    """*pid*'s ``KIROCREW_SPAWN_INSTANCE``, or ``None`` when absent or unreadable.

    Same read as :func:`_read_env_has_kirocrew_marker`, same Linux-only,
    fail-closed posture. The value is the per-spawn token the runtime put on its
    root's environment; every descendant inherits it, and a runtime spawned
    later carries a different one -- which is the whole point of reading it.
    """
    if sys.platform != "linux" and proc_root is None:
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    prefix = f"{KIROCREW_SPAWN_INSTANCE_ENV}=".encode()
    try:
        environ = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for entry in environ.split(b"\x00"):
        if entry.startswith(prefix):
            value = entry[len(prefix) :]
            return value.decode("ascii", "replace") if value else None
    return None


def _env_has_kirocrew_marker(pid: int, proc_root: Path | None = None) -> bool:
    """True if *pid*'s environment carries the ``KIROCREW_SPAWNED`` marker.

    Reads the exec-time environment through :func:`_read_env_has_kirocrew_marker`
    (``/proc`` on Linux, ``sysctl KERN_PROCARGS2`` on macOS) and collapses its
    tri-state answer to a verdict. FAIL-CLOSED: any read failure, and every
    platform with no same-UID environ oracle — Windows — returns ``False``, so a
    sweep path that needs this marker never kills without positive identity.
    *proc_root* is a test seam for fixture-owned process tables.
    """
    return _read_env_has_kirocrew_marker(pid, proc_root) is True


def _is_sweepable_orphan_mcp(pid: int, cmdline: bytes) -> bool:
    """Positive-identity gate for the orphan sweep (find AND pre-kill re-verify).

    Two independent paths:
    1. cmdline carries a KiroCrew fingerprint (:func:`_is_orphan_mcp`) —
       the pre-existing behavior, works on Linux and macOS.
    2. cmdline is a fingerprint-less MCP launcher shape AND the process
       environ carries the ``KIROCREW_SPAWNED`` marker (catches escaped
       ``npx @playwright/mcp`` and ``<launcher> mcp start-server`` trees).
       Needs a same-uid environ oracle, which Linux and macOS have and Windows
       does not, so this arm is fail-closed there.
    """
    if _is_orphan_mcp(cmdline):
        return True
    return _is_marked_mcp_launcher(cmdline) and _env_has_kirocrew_marker(pid)


# Grace given to a TERMed unreachable gatewayd before killpg SIGKILL. TERM is
# sent first, deliberately: gatewayd's signal handler routes into the same
# graceful stop path a supervised shutdown takes, so the daemon drains
# in-flight work and reaps its own pooled backend subprocesses — a direct
# SIGKILL would orphan them for a later sweep instead. Derived from the
# daemon's own total shutdown budget (the same discipline the supervisor's
# SIGTERM→SIGKILL grace follows) so the escalation can never fire while a
# correctly-draining daemon is still inside its drain window.
_GATEWAYD_TERM_GRACE_SECONDS = float(TOTAL_SHUTDOWN_BUDGET_SECS)


def _gatewayd_socket_arg(cmdline: bytes) -> bytes | None:
    """Extract the ``--socket`` argument from a gatewayd cmdline, or ``None``.

    Accepts the two-token ``--socket <path>`` form (the shape every Kiro Crew
    spawn site produces) and the argparse-equivalent ``--socket=<path>``.
    NUL-separated argv ONLY: the space-joined ``ps`` fallback (macOS) cannot
    delimit a path containing spaces, and statting a truncated path would
    read as ENOENT — a wrong-kill — so anything without NULs fails closed.
    ABSOLUTE paths only, for the same reason: a relative path would be
    resolved against the SWEEPER's working directory, not the daemon's, so
    a reachable daemon bound to ``gw.sock`` in another cwd would read as
    ENOENT here. When the flag repeats, the LAST occurrence is returned —
    argparse binds last-wins, so that is the path the daemon actually
    created.
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    if len(args) <= 1:
        return None
    candidate: bytes | None = None
    for i, arg in enumerate(args):
        if arg == b"--socket" and i + 1 < len(args):
            candidate = args[i + 1]
        elif arg.startswith(b"--socket="):
            candidate = arg[len(b"--socket=") :]
    if candidate is not None and os.path.isabs(os.fsdecode(candidate)):
        return candidate
    return None


def _is_sweepable_orphan_gatewayd(cmdline: bytes) -> bool:
    """Fourth positive-identity path: a gatewayd whose listening socket is gone.

    :data:`_GATEWAY_MARKERS` excludes gateway entrypoints from every other
    sweep path because the cmdline alone cannot distinguish a live dev pod's
    daemon from a dead launcher's. This path supplies the missing
    information: gatewayd creates the socket it is invoked with, and once
    that path is absent from disk no stub can ever connect to the daemon
    again — it is provably unreachable regardless of who launched it.

    Positive identity is the conjunction of:

    1. a structural ``-m kiro_crew.mcp_gateway.gatewayd`` argv pair — never
       ``kiro_crew.cli`` / ``kiro_crew.__main__``, which stay unconditionally
       excluded (they carry no socket argument and no equivalent
       reachability predicate);
    2. a ``--socket`` path in argv (:func:`_gatewayd_socket_arg`, NUL-argv
       only, fail-closed);
    3. that path absent from disk — ``ENOENT`` only; any other stat failure
       is inconclusive and fails closed.

    The callers preserve the rest of the sweep discipline: same-uid +
    reparented-to-init candidacy, the age floor, the kill budget, and
    re-verification immediately before signalling.
    """
    args = [a for a in cmdline.split(b"\x00") if a]
    is_gatewayd = any(
        args[i] == b"-m" and args[i + 1] == _GATEWAYD_MODULE for i in range(len(args) - 1)
    )
    if not is_gatewayd:
        return False
    sock = _gatewayd_socket_arg(cmdline)
    if sock is None:
        return False
    try:
        os.stat(os.fsdecode(sock))
    except FileNotFoundError:
        return True
    except OSError:
        return False  # inconclusive (EACCES, EIO, …) — fail closed
    return False


def _kill_orphan_gatewayd(pid: int, cmdline: bytes) -> int:
    """SIGTERM an unreachable gatewayd; escalate to killpg SIGKILL if wedged.

    TERM first so the daemon's graceful stop path drains and reaps its own
    pooled backends. If the process is still alive after
    :data:`_GATEWAYD_TERM_GRACE_SECONDS`, its identity is re-verified (PID
    recycling) and the whole group is SIGKILLed — the daemon is its own
    group leader (``start_new_session=True``), so ``killpg`` cannot reach
    any foreign process.
    """
    try:
        platform_compat.kill_pid(pid, platform_compat.SIGTERM)
    except ProcessLookupError:
        return 0
    deadline = time.monotonic() + _GATEWAYD_TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        # platform_compat.pid_exists, not a raw `os.kill(pid, 0)`: on Windows a
        # signal-zero "probe" TERMINATES the target instead of testing it, and
        # raw os.kill/SIGKILL do not exist there. This path is POSIX-only in
        # practice, but routing through the shim keeps it correct on its own
        # terms rather than depending on a caller's early-out — the same
        # rationale the browser-daemon probe below already carries.
        if not platform_compat.pid_exists(pid):
            _sel_orphan_kill(pid, pid, cmdline, "sigterm")
            return 1
        time.sleep(0.1)
    # Still alive past the grace: re-verify identity before force-kill so a
    # recycled PID is never SIGKILLed.
    try:
        if sys.platform == "linux":
            current = Path(f"/proc/{pid}/cmdline").read_bytes()
            if current != cmdline:
                _sel_orphan_kill(pid, pid, cmdline, "sigterm")
                return 1
        pgid = os.getpgid(pid)
        if pgid == pid and pgid != os.getpgrp() and pgid > 1:
            os.killpg(pgid, signal.SIGKILL)
        else:
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    _sel_orphan_kill(pid, pid, cmdline, "sigterm+sigkill")
    return 1


def _kill_orphan_browser_daemon(pid: int, cmdline: bytes) -> int:
    """Group-TERM a stranded browser daemon, escalating to a group SIGKILL.

    Signals the process GROUP, not the pid. The daemon is spawned
    ``detached``, so it is its own group leader and its Chromium children
    inherit that group -- the browser tree is the whole point of the reclaim
    (it is where the gigabytes are), and a pid-only signal would kill the
    supervisor and leave Chromium reparented to init as a fresh, now
    completely unattributable leak.

    TERM first so Chromium exits through its own shutdown path and flushes
    its profile. The group is signalled only when the daemon is genuinely an
    isolated leader (``pgid == pid``, not our own group, not init's), so
    ``killpg`` can never reach a foreign process; identity is re-verified
    before the escalation so a PID recycled inside the grace window is never
    SIGKILLed.
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return 0
    if not (pgid == pid and pgid != os.getpgrp() and pgid > 1):
        # Not an isolated group leader: killpg would reach processes this
        # predicate never identified. Leave it for a later sweep.
        return 0
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return 0
    deadline = time.monotonic() + _BROWSER_DAEMON_TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        # platform_compat.pid_exists, not a raw `os.kill(pid, 0)`: on Windows a
        # signal-zero "probe" TERMINATES the target instead of testing it. This
        # path is POSIX-only in practice, but routing through the shim keeps it
        # correct on its own terms rather than depending on a caller's early-out.
        if not platform_compat.pid_exists(pid):
            _sel_orphan_kill(pid, pgid, cmdline, "browser-daemon-sigterm")
            return 1
        time.sleep(0.1)
    try:
        if sys.platform == "linux":
            if Path(f"/proc/{pid}/cmdline").read_bytes() != cmdline:
                _sel_orphan_kill(pid, pgid, cmdline, "browser-daemon-sigterm")
                return 1
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    _sel_orphan_kill(pid, pgid, cmdline, "browser-daemon-sigterm+sigkill")
    return 1


def _work_orphan_basename(cmdline: bytes) -> bytes:
    """argv0's basename from a raw command line.

    The same two steps the reclaim's own gate uses, reached through the same helpers
    rather than spelled again: :func:`_argv_tokens` prefers the NUL boundaries and falls
    back to spaces only off Linux, and :func:`_basename_of` splits on ``/`` and ``\\``.

    Splitting on a single space here, with no ``\\`` handling, is the spaced-path defect
    this module documents: a home directory like ``/Users/John Smith`` makes one argv0 read
    as two tokens, so the basename answers ``John`` and the process is not recognised.
    Sharing the helpers is what keeps one fix from reaching only one of the two readers.
    """
    tokens = _argv_tokens(cmdline)
    if not tokens:
        return b""
    return _basename_of(tokens[0])


def _is_agent_runtime_anchor(cmdline: bytes, *, has_kirocrew_marker: bool) -> bool:
    """True when *cmdline* positively identifies an agent-runtime tree member.

    This is the scope-reaper's authorization anchor, deliberately separate from
    marker/descent ownership. It recognizes the generated sandbox launcher and
    fingerprinted MCP workers through :func:`_is_orphan_mcp`, direct managed
    runtimes (``kiro-cli`` / ``kiro-cli-chat`` / ``claude-agent-acp`` /
    ``claude``) by exact argv0 basename, and the existing fingerprint-less MCP
    launcher shapes only when that member itself carries the Kiro Crew spawn
    marker. Peer gateway/CLI entrypoints are excluded.
    """
    if not cmdline:
        return False
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    if _is_orphan_mcp(cmdline):
        return True
    basename = _work_orphan_basename(cmdline)
    if basename in _MANAGED_AGENT_RUNTIME_BASENAMES:
        return True
    return has_kirocrew_marker and _is_marked_mcp_launcher(cmdline)


def _is_sweepable_orphan_work(pid: int, cmdline: bytes, age_seconds: float) -> bool:
    """Third positive-identity path: agent-spawned TEST-RUNNER process
    (pytest coordinator or pytest-xdist/execnet worker) that outlived its
    agent session.

    The positive identity is the conjunction of:

    1. A structural test-runner argv match
       (:func:`_work_sweep_cmdline_is_test_runner`). Test runners
       are never legitimate long-lived daemons, unlike other marked-but-
       detached processes an agent may deliberately leave running (a preview
       server started with ``start_new_session=True``, for instance) — those
       are intentional survivors and MUST NOT be swept, so a marker alone is
       not sufficient identity.
    2. The ``KIROCREW_SPAWNED`` environ marker (:func:`_env_has_kirocrew_marker`,
       Linux-only, fail-closed elsewhere). The marker is only ever injected
       into environments Kiro Crew itself spawns (sandbox wrapper, ACP client
       and runtime, MCP gateway backend) and is inherited by every descendant,
       so it can never identify a user-launched process.
    3. Reparenting to init/systemd --user — guaranteed by the caller, which
       only iterates :func:`_our_orphan_pids` — AND the owning session's
       LEADER being gone (:func:`_work_orphan_session_leader_alive`). Kiro
       Crew starts every agent runtime with ``start_new_session=True``, so
       the runtime is a session leader and every descendant inherits its SID
       — including through ``nohup`` and reparenting. A work process whose
       session leader still exists belongs to a LIVE agent session that may
       be polling its output (a backgrounded test run, for instance) and is
       never swept; only when the leader is gone has the owning session
       positively ended, making the run unreachable by any agent.
    4. Age above :data:`_ORPHAN_WORK_MIN_AGE_SECONDS` — deliberately much
       higher than the 120s MCP floor so a just-detached spawn is never raced
       and a slow-but-legitimate long test run gets generous grace.

    Two NEGATIVE gates keep the blast radius tight: managed agent runtimes
    (:data:`_MANAGED_AGENT_MARKERS`) stay owned by their own tracked-PID
    lifecycle, and gateway/CLI entrypoints (:data:`_GATEWAY_MARKERS`) are
    excluded so agent-launched peer gateways (e.g. dev pods) are never swept.
    """
    if age_seconds < _ORPHAN_WORK_MIN_AGE_SECONDS:
        return False
    if not cmdline:
        return False  # kernel thread / zombie — nothing meaningful to kill
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    if not _work_sweep_cmdline_is_test_runner(cmdline):
        return False
    basename = _work_orphan_basename(cmdline)
    if _basename_names_a_harness(basename):
        return False
    if _work_orphan_session_leader_alive(pid):
        return False  # owning agent session still live — a backgrounded run
    return _env_has_kirocrew_marker(pid)


# PIDs already reported by the untracked-runtime detector, so a persisting
# orphan costs ONE log line rather than one line per sweep. Replaced wholesale
# at the end of each scan with the set still detected, which bounds the set by
# the live orphan count and re-arms the report if the PID disappears and a
# later process reappears under the same detection.
_reported_untracked_agent_pids: set[int] = set()


# Per tracking file, the index of the colon-field naming the process a reaper
# actually TERMINATES for that entry. Only that field counts as tracked: the
# other one names the OWNER whose death makes the entry reapable
# (``_sweep_pid_entries`` skips a session entry while its gateway lives;
# ``_cleanup_orphaned_mcp_servers`` kills the child once its parent is gone), and
# an owner is never reclaimed *through* the entry that names it. Counting an
# owner field would let a stale entry whose owner has died and had its PID
# recycled silently suppress a genuine leak report — exactly the silence this
# field exists to prevent. A bare line names its own process, whichever file it
# is in.
_REAPABLE_PID_FIELD: tuple[tuple[str, int], ...] = (
    ("session", 1),  # kiro_session_pids.txt: <gateway_pid>:<child_pid>[:start-id]
    ("child", 0),  # kiro_pids.txt: <child_pid>:<parent_pid>[:start-id]
)


def _read_tracked_agent_pids() -> tuple[set[int], bool]:
    """Return the tracked PID snapshot and whether it is complete.

    Both files are read because a runtime absent from BOTH is exactly what
    :func:`_is_untracked_managed_agent_orphan` reports, and each reaper keys off
    only one of them. Within a line only the reapable field counts — see
    :data:`_REAPABLE_PID_FIELD` for which, and why the owner field does not. A
    session entry's third field is a start-time identity, numeric on Linux, and
    is never read as a PID.

    Reads remain lock-free. Readers cannot tear: rewrites
    go through :func:`_rewrite_pid_file` (temp file + rename, so a reader sees
    either the whole old or the whole new content) and tracking appends are
    single short lines. ``complete`` is false whenever a potential PID could
    have been dropped; a missing file is a complete empty contribution.
    """
    tracked: set[int] = set()
    complete = True
    paths = (_session_pid_file_path(), _pid_file_path())
    for path, (_label, reapable_index) in zip(paths, _REAPABLE_PID_FIELD):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                complete = False
            continue
        for line in raw.split():
            fields = line.split(":")
            index = 0 if len(fields) == 1 else reapable_index
            if index >= len(fields):
                complete = False
                continue
            try:
                value = int(fields[index])
            except ValueError:
                complete = False
                continue
            if value > 0:
                tracked.add(value)
    return tracked, complete


def _tracked_agent_pids() -> set[int]:
    """PIDs a reaper can terminate, preserving report-only fail-open behavior.

    Diagnostic callers intentionally accept a partial set. Any caller that can
    authorize a kill must use :func:`_read_tracked_agent_pids` and require its
    completeness flag.
    """
    tracked, _complete = _read_tracked_agent_pids()
    return tracked


def tracked_agent_pid_owners() -> dict[int, int]:
    """``{tracked pid: the pid that owns its registry entry}`` -- READ ONLY.

    A diagnostic accessor, added for :mod:`kiro_crew.diag.procs` so a process
    view does not have to re-spell this file format. It grants nothing and
    authorizes nothing: it neither writes, locks, signals, nor reports
    completeness, and no reaper consults it.

    The two files record opposite field orders (:data:`_REAPABLE_PID_FIELD`),
    and the owner is the field the reapers deliberately ignore: in
    ``kiro_session_pids.txt`` it is the GATEWAY that spawned the runtime, and in
    ``kiro_pids.txt`` it is the tracked PARENT the descendant hangs off. Both
    answer "who does this process belong to" for an operator reading a tree,
    which is why they are surfaced together here and nowhere else.

    Session entries win a collision: the two files share one number space, and
    the session entry is the one that names a gateway. A legacy single-field
    line records no owner at all and is skipped rather than given a fabricated
    one. This is a SUBTRACTIVE read in the same sense as
    :func:`_pid_start_token` -- a pid absent from the result means "no recorded
    owner", never "not ours".
    """
    owners: dict[int, int] = {}
    paths = (_session_pid_file_path(), _pid_file_path())
    for path, (_label, reapable_index) in zip(paths, _REAPABLE_PID_FIELD):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        owner_index = 1 - reapable_index
        for line in raw.split():
            fields = line.split(":")
            if len(fields) < 2:
                continue  # legacy bare-PID line: no owner recorded
            try:
                pid = int(fields[reapable_index])
                owner = int(fields[owner_index])
            except ValueError:
                continue
            if pid > 0 and owner > 0:
                owners.setdefault(pid, owner)
    return owners


def _is_untracked_managed_agent_orphan(pid: int, cmdline: bytes, tracked_pids: set[int]) -> bool:
    """REPORT-ONLY: a managed agent runtime that no reaper can reach.

    Every existing reaper declines this process, which is why a leaked runtime
    has no reproduction:

    * :func:`cleanup_orphaned_sessions` and :func:`_periodic_pid_sweep` iterate
      ``kiro_session_pids.txt`` and cannot see a PID the file never recorded.
    * :func:`_is_sweepable_orphan_mcp` declines it — a runtime argv is not an
      MCP entrypoint.
    * :func:`_is_sweepable_orphan_work` NEGATIVE-gates
      :data:`_MANAGED_AGENT_MARKERS` (runtimes are owned by their tracked-PID
      lifecycle, not by the marker sweep) and separately requires a test-runner
      argv.

    Positive identity is the conjunction of: reparenting to init/``systemd
    --user`` — guaranteed by the caller, which only iterates
    :func:`_our_orphan_pids`, so ownership is not re-derived here — an argv0
    basename naming a managed runtime (:data:`_MANAGED_AGENT_MARKERS`), the
    ``KIROCREW_SPAWNED`` environ marker (:func:`_env_has_kirocrew_marker`,
    Linux-only and fail-closed elsewhere), and absence from BOTH PID files
    (:func:`_tracked_agent_pids`). Peer gateways and CLIs
    (:data:`_GATEWAY_MARKERS`) are excluded: they are not agent runtimes and
    are never tracked as such.

    This grants NO kill authority and is wired to nothing that terminates — a
    hit only logs. Blast radius is therefore zero, which is what makes the
    detector safe to ship ahead of a maintainer's ruling on whether an
    untracked runtime may be reaped at all. It also means a cross-data-home
    false positive (a second install's live runtime, tracked in ITS config dir
    and so absent from ours) is diagnostic noise rather than a wrong kill.
    """
    if not cmdline:
        return False  # kernel thread / zombie — no argv to identify
    normalized = cmdline.replace(b"\x00", b" ")
    if any(marker in normalized for marker in _GATEWAY_MARKERS):
        return False
    basename = _work_orphan_basename(cmdline)
    if not _basename_names_a_harness(basename):
        return False
    if pid in tracked_pids:
        return False  # a reaper can already reach it
    return _env_has_kirocrew_marker(pid)


def _work_orphan_session_leader_alive(pid: int) -> bool:
    """True when *pid*'s session LEADER still exists as a session leader.

    The SID of an agent-spawned work process is the PID of the kiro-cli
    runtime that (transitively) spawned it — the runtime is started with
    ``start_new_session=True`` and neither ``nohup`` nor reparenting to init
    changes a process's SID. A live leader means the owning agent session may
    still be driving or polling the work process, so the sweep must leave it
    alone. PID-recycling is handled by requiring the leader candidate to
    itself be a session leader (a leader's SID equals its own PID); a
    recycled PID that is not a leader does not resurrect ownership.

    FAIL-CLOSED for the sweep: any read failure returns True ("assume
    alive"), so the work path never kills without positively verifying the
    owning session ended.
    """
    sid = _linux_pid_sid(pid)
    if sid <= 0:
        return True  # unreadable — assume the owner is alive, do not sweep
    if sid == pid:
        # The work process became its own session leader (setsid'd daemon):
        # SID carries no ownership information. Assume alive — the shape gate
        # already restricts this path to test runners, and a coordinator that
        # setsid'd itself is not distinguishable from an owned one.
        return True
    leader_sid = _linux_pid_sid(sid)
    return leader_sid == sid  # alive AND still a session leader


def find_orphan_mcp_candidates(active_pids: set[int]) -> list[int]:
    """Scan process table for orphaned MCP processes not in any active set.

    Returns candidate PIDs. Caller should re-verify against fresh active PIDs
    before killing (two-phase pattern to eliminate races).

    Also REPORTS — never returns as a candidate — any untracked managed-agent
    runtime orphan (:func:`_is_untracked_managed_agent_orphan`). That class is
    unreachable by every reaper, so it would otherwise leak silently with no
    reproduction; the report deliberately carries no kill authority, which is
    why such a PID is excluded from ``candidates``.
    """
    candidates: list[int] = []
    my_pid = os.getpid()
    now = time.time()

    orphan_pids = _our_orphan_pids()
    # Read once per scan, not per PID: the files are small but the scan is not.
    # Empty on Windows and on any run with no orphans, so the diagnostic read is
    # skipped entirely in the common case.
    tracked_pids = _tracked_agent_pids() if orphan_pids else set()
    untracked_seen: set[int] = set()

    for pid in orphan_pids:
        if pid == my_pid or pid in active_pids:
            continue
        try:
            if sys.platform == "linux":
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                # Use /proc/pid/stat field 22 (starttime in clock ticks) for
                # canonical process age — immune to /proc mtime heuristic issues.
                pid_age = _linux_pid_age(pid, now)
            else:
                # Single ps call fetches both age and command (two -o flags
                # avoid the BSD header-label comma ambiguity). etime is
                # whitespace-free, so split(None, 1) cleanly separates the
                # two fields.
                ps_out = subprocess.check_output(
                    ["ps", "-o", "etime=", "-o", "command=", "-p", str(pid)],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
                fields = ps_out.split(None, 1)
                pid_age = _parse_etime(fields[0].decode() if fields else "")
                cmdline = fields[1] if len(fields) > 1 else b""
        except _PID_VANISHED_ERRORS:
            # Expected TOCTOU race: the PID was in the /proc (or ps) snapshot
            # taken by _our_orphan_pids() and exited before this probe read it.
            # That is the outcome the sweep wants, so log one line — a stack
            # trace here would overstate a routine event.
            logger.debug("Orphan candidate pid %s vanished before probe", pid)
            continue
        except Exception:
            logger.debug(
                "Orphan candidate probe failed for pid %s",
                pid,
                exc_info=True,
            )
            continue
        if pid_age < _ORPHAN_MIN_AGE_SECONDS:
            continue
        # Report-only arm. Placed AFTER the age gate so a runtime whose tracking
        # append has not landed yet is never reported: the gate is orders of
        # magnitude wider than the spawn-to-append window. Reported PIDs are
        # deliberately NOT appended to ``candidates`` — this arm has no kill
        # authority (see the predicate's docstring).
        if _is_untracked_managed_agent_orphan(pid, cmdline, tracked_pids):
            untracked_seen.add(pid)
            if pid not in _reported_untracked_agent_pids:
                # %r, not %s: argv0 is set by the process itself, so a newline
                # in it would forge whole log lines in gateway.log and through
                # /api/logs. repr escapes control characters.
                logger.error(
                    "Leaked agent runtime pid=%s (argv0 %r, age %.0fs): reparented "
                    "to init/systemd with a KIROCREW_SPAWNED environ marker but "
                    "recorded in NEITHER PID file, so no reaper can reclaim it. "
                    "Not terminated — report only.",
                    pid,
                    _work_orphan_basename(cmdline).decode("utf-8", "replace"),
                    pid_age,
                )
        if not (
            _is_sweepable_orphan_mcp(pid, cmdline)
            or _is_sweepable_orphan_gatewayd(cmdline)
            or _is_sweepable_orphan_work(pid, cmdline, pid_age)
            or _is_sweepable_orphan_browser_daemon(pid, cmdline, pid_age)
        ):
            continue
        candidates.append(pid)

    # Keep only what is still detected, so a persisting orphan stays deduped
    # while a vanished PID re-arms the report for a future process.
    _reported_untracked_agent_pids.clear()
    _reported_untracked_agent_pids.update(untracked_seen)

    return candidates


def _linux_pid_sid(pid: int, proc_root: Path | None = None) -> int:
    """Session id (SID) from /proc/pid/stat (field 6, index 3 after state).

    The SID of an agent-spawned work process points at the kiro-cli session
    leader that (transitively) spawned it — kiro-cli is started with
    ``start_new_session=True``, so every descendant inherits its SID even
    after the direct parent dies and the process reparents to init. Returns
    -1 when unreadable (caller must fail closed). *proc_root* is a test seam
    for fixture-owned process tables.
    """
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        stat_data = (root / str(pid) / "stat").read_text()
        close_paren = stat_data.rfind(")")
        fields = stat_data[close_paren + 2 :].split()
        return int(fields[3])  # field 6 (session) = index 3 after state
    except (OSError, ValueError, IndexError):
        return -1


def _linux_pid_age(pid: int, now: float) -> float:
    """Process age in seconds using /proc/pid/stat starttime (canonical)."""
    try:
        stat_data = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 is starttime (after comm which may contain spaces/parens)
        close_paren = stat_data.rfind(")")
        fields = stat_data[close_paren + 2 :].split()
        starttime_ticks = int(fields[19])  # field 22 is index 19 after state
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        boot_time = now - uptime
        start_seconds = boot_time + (starttime_ticks / clk_tck)
        return now - start_seconds
    except (OSError, ValueError, IndexError):
        return 0.0  # Cannot determine age — min-age guard will skip


def _parse_etime(etime: str) -> float:
    """Parse ps etime format [[DD-]HH:]MM:SS into seconds."""
    try:
        days = 0
        if "-" in etime:
            day_part, etime = etime.split("-", 1)
            days = int(day_part)
        parts = etime.split(":")
        if len(parts) == 3:
            return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return days * 86400 + int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        pass
    return 0.0


def kill_orphan_mcps(pids: list[int]) -> int:
    """Kill confirmed orphan MCP processes. Uses killpg if isolated, else direct kill.

    Re-verifies cmdline immediately before kill to mitigate PID-reuse TOCTOU.

    POSIX-only: the whole flow depends on process groups (``os.getpgrp`` /
    ``os.killpg`` / ``os.getpgid``) and ``signal.SIGKILL``, none of which exist
    on Windows. On Windows the orphan sweep is a no-op — the tree-kill after a
    session ends already went through ``taskkill /T``.
    """
    if platform_compat.IS_WINDOWS:
        return 0
    my_pgid = os.getpgrp()
    my_pid = os.getpid()
    killed = 0
    # Parent->children map for the subtree reap, built at most ONCE per sweep
    # (one full /proc pass) and only when a marked MCP orphan is actually
    # confirmed -- the common sweep finds none and pays nothing.
    child_map: dict[int, list[int]] | None = None
    for pid in pids:
        if killed >= _ORPHAN_SWEEP_MAX_KILLS:
            break
        if pid == my_pid:
            continue
        try:
            # Start identity FIRST -- before any other read about this pid.
            # Everything below (the cmdline, the eligibility verdict, the pgid)
            # is evidence about whichever process held this PID at the moment it
            # was read, so capturing identity after any of them leaves a window
            # where the orphan exits, the PID is reused, and that stale evidence
            # licenses signalling the replacement. There is no earlier point.
            root_token = _pid_start_token(pid)
            # Re-verify identity right before kill (TOCTOU mitigation):
            # PID may have been recycled between find and kill phases.
            if sys.platform == "linux":
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            else:
                cmdline = subprocess.check_output(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            if _is_sweepable_orphan_mcp(pid, cmdline):
                pgid = os.getpgid(pid)
                # ── PID-recycle invariant ──────────────────────────────
                # No signal in this branch reaches a PID whose start identity
                # was not captured BEFORE any other read about it and
                # re-confirmed IMMEDIATELY before the signal. The three signal
                # sites are this root killpg, this root kill, and each
                # descendant's kill inside _kill_orphan_mcp_descendants (guarded
                # there, with its own live parent-edge check).
                #
                # Enumerate the subtree BEFORE signalling the root: once the root
                # dies its children reparent to init and the parent links this
                # walk needs are gone.
                if child_map is None:
                    child_map = _build_child_map()
                subtree = _orphan_descendants(pid, child_map)
                # ── Descendants FIRST, root LAST ───────────────────────
                # The root is the handle on this tree: it is marked and
                # sweepable, so while it lives the whole tree stays
                # re-enumerable on a later sweep. Killing it before the
                # descendants are accounted for is what loses that handle --
                # when the tree exceeds the kill cap the survivors can include
                # the UNMARKED intermediate, which reparents to init, is not
                # sweepable, and hides its marked children behind a non-init
                # ppid. That is precisely the leak this function exists to
                # close, so the ordering below is load-bearing, not stylistic.
                #
                # Observed shape, produced by any launcher wrapper that resolves
                # a package and then execs the resolved binary:
                #     <wrapper> mcp start-server <pkg>      <- marked
                #       -> <wrapper> mcp start-server ...   <- marked
                #         -> node .../bin/<pkg>-server      <- UNMARKED
                #           -> npm exec <pkg>@latest        <- marked
                # One host accumulated 112 such processes (15.2 GB RSS) over 23
                # days of sweeps that were running the whole time.
                #
                # Reaping descendants first also makes the killpg below pure
                # belt-and-braces for anything still sharing the root's group:
                # a launcher that ``setsid``-s its payload escapes killpg
                # entirely, which is why the explicit walk exists at all.
                killed += _kill_orphan_mcp_descendants(
                    subtree, root=pid, budget=_ORPHAN_SWEEP_MAX_KILLS - killed
                )
                if killed >= _ORPHAN_SWEEP_MAX_KILLS:
                    # Budget spent on the subtree. Leave the root ALIVE and
                    # unsignalled: it stays a marked, sweepable candidate, so the
                    # next sweep re-enumerates what is left of this tree with a
                    # fresh budget. Killing it here would strand the survivors
                    # behind an unsweepable ancestor.
                    logger.debug(
                        "Orphan MCP sweep: kill cap reached on the subtree of root "
                        "pid=%d — leaving the root alive so the remainder stays "
                        "discoverable next sweep",
                        pid,
                    )
                    continue
                # Revalidate the FULL evidence set immediately before signalling
                # the root: identity, eligibility, and the group being targeted.
                # The token alone is not enough -- it proves the process, not that
                # the argv still qualifies it or that it is still in this group.
                live_token = _pid_start_token(pid)
                if root_token is None or live_token is None or live_token != root_token:
                    # Unproven or changed identity: never signal a PID that may
                    # now belong to someone else. An unavailable token is never
                    # read as a match -- see _pid_start_token's contract -- and a
                    # genuine orphan is re-reaped next sweep.
                    logger.debug(
                        "Orphan MCP sweep: skipping root pid=%d — identity changed or "
                        "unavailable across the subtree scan (pre=%r post=%r)",
                        pid,
                        root_token,
                        live_token,
                    )
                    continue
                try:
                    if sys.platform == "linux":
                        live_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                    else:
                        live_cmdline = subprocess.check_output(
                            ["ps", "-o", "command=", "-p", str(pid)],
                            stderr=subprocess.DEVNULL,
                            timeout=2,
                        )
                    if not _is_sweepable_orphan_mcp(pid, live_cmdline):
                        continue  # no longer qualifies — do not signal it
                    if os.getpgid(pid) != pgid:
                        continue  # left the group; that group is no longer ours
                except (OSError, subprocess.SubprocessError):
                    continue  # exited between the token read and here
                if pgid == pid and pgid != my_pgid and pgid > 1:
                    os.killpg(pgid, signal.SIGKILL)
                    killed += 1
                    _sel_orphan_kill(pid, pgid, cmdline, "killpg")
                else:
                    # Candidate already passed UID + orphan-ppid + positive MCP
                    # marker + two-phase active-PID re-verify + the full
                    # evidence revalidation above. Routed through the
                    # platform_compat shim like the work-tree reaper (exception
                    # types are identical on POSIX).
                    #
                    # This signals the root ALONE. Its descendants were already
                    # reaped explicitly above, which is what this commit adds:
                    # the older reasoning here -- that surviving children with an
                    # MCP marker get reclaimed on a subsequent sweep, and that
                    # unmarked ones were never candidates -- is what the
                    # 112-process leak falsified. An UNMARKED intermediate IS a
                    # candidate yet is not sweepable, so it never reparents into
                    # view and keeps its marked children behind a non-init ppid.
                    platform_compat.kill_pid(pid, platform_compat.SIGKILL)
                    killed += 1
                    _sel_orphan_kill(pid, pgid, cmdline, "kill")
                continue
            # Unreachable-gatewayd orphan: re-verify the FULL identity —
            # the socket-path stat AND the age floor — right before
            # signalling. The age recheck matters: a candidate that exited
            # after the find phase can have its PID recycled by a brand-new
            # gatewayd that has not bound its socket yet, and without the
            # floor that pre-bind daemon would read as "socket absent" and
            # be TERMed. TERM-first so the daemon drains its own backends.
            gw_age = _linux_pid_age(pid, time.time()) if sys.platform == "linux" else 0.0
            if gw_age >= _ORPHAN_MIN_AGE_SECONDS and _is_sweepable_orphan_gatewayd(cmdline):
                killed += _kill_orphan_gatewayd(pid, cmdline)
                continue
            # Work-class orphan (KIROCREW_SPAWNED marker, no launcher shape).
            # Re-verify the full identity — including the age floor — right
            # before the kill; _is_sweepable_orphan_work fails closed off Linux.
            work_age = _linux_pid_age(pid, time.time()) if sys.platform == "linux" else 0.0
            if _is_sweepable_orphan_work(pid, cmdline, work_age):
                killed += _kill_orphan_work_tree(
                    pid, cmdline, work_age, budget=_ORPHAN_SWEEP_MAX_KILLS - killed
                )
                continue
            # Stranded browser daemon. Re-verify the FULL identity — argv
            # shape, exec-time environ, live-owner probe and the age floor —
            # immediately before signalling, so a PID recycled since the find
            # phase cannot inherit the verdict.
            daemon_age = _linux_pid_age(pid, time.time()) if sys.platform == "linux" else 0.0
            if _is_sweepable_orphan_browser_daemon(pid, cmdline, daemon_age):
                live_token = _pid_start_token(pid)
                if root_token is None or live_token is None or live_token != root_token:
                    logger.debug(
                        "Orphan browser sweep: skipping pid=%d — identity changed "
                        "or unavailable before TERM (pre=%r post=%r)",
                        pid,
                        root_token,
                        live_token,
                    )
                    continue
                live_cmdline = _pid_cmdline(pid)
                if not live_cmdline or live_cmdline != cmdline:
                    logger.debug(
                        "Orphan browser sweep: skipping pid=%d — cmdline changed "
                        "or became unreadable before TERM",
                        pid,
                    )
                    continue
                killed += _kill_orphan_browser_daemon(pid, live_cmdline)
        except (
            ProcessLookupError,
            PermissionError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            try:
                # Lazy import: session_pid is imported early by acp.runtime, so
                # a module-level `from kiro_crew.sel import sel` would be circular.
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key="gateway",
                    agent="kirocrew",
                    source="background",
                    tool_name="orphan_mcp_sweep",
                    tool_kind="process_kill",
                    outcome="failed",
                    resources=f"pid={pid}",
                    metadata={"error": str(exc)},
                )
            except Exception:
                logger.debug("SEL orphan-kill audit failed", exc_info=True)
    if killed:
        logger.warning("Orphan MCP sweep: killed %d untracked process(es)", killed)
    return killed


def _sel_orphan_kill(pid: int, pgid: int, cmdline: bytes, method: str) -> None:
    """Emit SEL audit event for an orphan MCP kill."""
    try:
        # Lazy import to avoid a circular import (see kill_orphan_mcps).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="gateway",
            agent="kirocrew",
            source="background",
            tool_name="orphan_mcp_sweep",
            tool_kind="process_kill",
            outcome="completed",
            resources=f"pid={pid} pgid={pgid} method={method}",
            metadata={
                "cmdline": cmdline[:200].decode("utf-8", errors="replace"),
            },
        )
    except Exception:
        logger.debug("SEL orphan-kill audit failed", exc_info=True)


def _pid_cmdline(pid: int, proc_root: Path | None = None) -> bytes:
    """Best-effort argv for *pid* on Linux; ``b""`` when unreadable or off-Linux.

    Empty is inconclusive, never "clean": every caller treats it as fail-closed
    (skip the process) rather than assuming it is safe to touch.

    Off-Linux deliberately has NO ``ps`` branch. Every consumer of this argv
    feeds a decision that also requires :func:`_env_has_kirocrew_marker`, which
    is fail-closed off Linux, so a subprocess here would only ever supply
    evidence for a verdict that is already "refuse". An explicit *proc_root*
    permits fixture-owned process tables on every host.
    """
    if sys.platform != "linux" and proc_root is None:
        return b""
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        return (root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return b""


def _pid_parent_and_token(pid: int) -> tuple[int | None, str | None]:
    """``(ppid, start_token)`` for *pid* from ONE ``/proc/<pid>/stat`` read.

    Both values must come from the SAME read. Reading the parent edge and the
    start identity separately leaves a window in which the PID exits between
    them, so a recycled PID's fresh token gets paired with the dead process's
    parent edge -- and that token then matches at kill time, which is precisely
    how a live worker gets SIGKILLed.

    ``stat`` field 4 is PPid and field 22 is starttime; ``comm`` (field 2) can
    contain spaces and parentheses, so both are read after the LAST ``)``, the
    same way :func:`_build_child_map` and
    ``platform_compat.get_process_start_id`` parse it.

    ``(None, None)`` on any failure, and off Linux -- where the whole subtree
    reap is already a no-op because :func:`_env_has_kirocrew_marker` is
    fail-closed. Callers must treat ``None`` as unproven, never as a mismatch.
    """
    if sys.platform != "linux":
        return (None, None)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        rparen = stat.rfind(")")
        if rparen < 0:
            return (None, None)
        fields = stat[rparen + 2 :].split()
        return (int(fields[1]), fields[19])
    except (OSError, ValueError, IndexError):
        return (None, None)  # exited mid-read or unreadable — fail closed


def _prune_from_orphan_walk(pid: int) -> bool:
    """True when the walk must neither include *pid* NOR descend into it.

    Prunes gateway/CLI entrypoints (:data:`_GATEWAY_MARKERS`) -- an
    agent-launched peer gateway or dev pod. Excluding only the entrypoint's own
    PID is not enough: the walk is flat, so its live workers would still be
    enumerated, and each carries ``KIROCREW_SPAWNED`` with no gateway marker in
    its own argv, so each would pass the per-member gate and be SIGKILLed,
    crashing that pod's active sessions. The whole subtree has to go.

    An unreadable argv also prunes: it is either a process that just exited (no
    children to find) or one whose identity cannot be established, and neither
    is a case for descending.
    """
    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return True
    return any(marker in cmdline.replace(b"\x00", b" ") for marker in _GATEWAY_MARKERS)


def _orphan_descendants(pid: int, child_map: dict[int, list[int]]) -> list[tuple[int, str | None]]:
    """Preorder descendants of a confirmed orphan root, each with its identity.

    Traverses *child_map* -- the authoritative parent->children map from
    :func:`_build_child_map`, which reads every process's ``stat`` PPid field.
    Deliberately NOT ``/proc/<pid>/task/*/children``: that needs
    ``CONFIG_CHECKPOINT_RESTORE``/``CONFIG_PROC_CHILDREN`` and is documented
    reliable only for frozen/stopped tasks, so for a live task it can return an
    incomplete child set and silently drop whole subtrees -- which is precisely
    the leak this sweep exists to close, so reaping through it could no-op with
    no signal.

    Always called BEFORE the root is signalled: after the root dies its children
    reparent to init and the parent links this walk needs are gone.

    Each member is returned with its :func:`_pid_start_token`, captured HERE so
    the kill can refuse a PID that was recycled in between (see
    :func:`_kill_orphan_mcp_descendants`).

    *child_map* is a SNAPSHOT, and it is reused across every candidate root in
    one sweep, so an edge in it can be stale by the time the walk reads it: the
    child may have exited and its PID been reused. Each child's live PPid is
    therefore re-read here and must still equal the parent it was traversed
    from; a PID that no longer points back at that parent is a different
    process and is dropped with its subtree. The PPid and the start token come
    from ONE ``stat`` read (:func:`_pid_parent_and_token`) so they cannot
    describe two different processes.

    Iterative, not recursive: an orphan chain deeper than Python's recursion
    limit would raise ``RecursionError``, which the caller's ``except`` clause
    does not name and which fires BEFORE the root is signalled -- aborting the
    whole sweep, every cycle, and preserving the very tree being reclaimed.

    The visited set bounds the walk, so a PID cycle terminates instead of
    looping forever -- checked per CHILD, which covers a self-parent too.

    A pruned child (:func:`_prune_from_orphan_walk`) is skipped WITH its whole
    subtree, so a peer gateway's live workers are never enumerated.
    """
    out: list[tuple[int, str | None]] = []
    seen: set[int] = {pid}
    # DFS stack of (child, parent) pairs still to validate. The parent travels
    # WITH the child because it is what the live-PPid check compares against.
    # Each pair is validated and emitted when POPPED, and its own children are
    # pushed reversed, which is what makes the emitted order preorder -- the
    # order the leaf-first kill reverses. Emitting inside the child loop instead
    # would yield level order and kill a parent before its children.
    stack: list[tuple[int, int]] = [(c, pid) for c in reversed(child_map.get(pid, []))]
    while stack:
        child, parent = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        live_ppid, token = _pid_parent_and_token(child)
        if live_ppid != parent:
            # Stale edge: this PID exited and a different process now holds it,
            # or its identity cannot be read. Either way it is not the child
            # that was enumerated, so neither it nor anything the snapshot hangs
            # beneath it may be signalled.
            continue
        if _prune_from_orphan_walk(child):
            continue  # gateway subtree (or unreadable) -- do not descend
        out.append((child, token))
        for grandchild in reversed(child_map.get(child, [])):
            stack.append((grandchild, child))
    return out


def _kill_orphan_mcp_descendants(
    descendants: list[tuple[int, str | None]], *, root: int, budget: int
) -> int:
    """SIGKILL leftover subtree members of a reaped MCP-launcher orphan, leaf-first.

    Mirrors :func:`_kill_orphan_work_tree`: descendants were enumerated once
    (preorder) and are killed in reverse so every process dies before its
    parent. *budget* is the caller's remaining
    :data:`_ORPHAN_SWEEP_MAX_KILLS` allowance, so subtree members count
    against the same global cap; survivors are re-reaped next sweep.

    Positive identity per member — the root passing the sweep gate does NOT
    license killing arbitrary descendants:

    * ``KIROCREW_SPAWNED`` in the member's exec-time environ, proving it
      belongs to a tree Kiro Crew spawned (:func:`_env_has_kirocrew_marker`,
      Linux-only and fail-closed, so this whole reap is a no-op off Linux —
      matching the work-class floor).
    * NOT a gateway/CLI entrypoint (:data:`_GATEWAY_MARKERS`), so an
      agent-launched peer gateway or dev pod under the same tree survives.
    * Never this process, its group leader, or pid <= 1.
    * The SAME process the walk saw -- its ``_pid_start_token`` must still
      match the one captured at enumeration. Without this the reap has a
      PID-recycle hole: the root's ``killpg`` reaps a descendant, the kernel
      hands that PID to a NEW Kiro-Crew-spawned worker, and the stale entry
      then SIGKILLs a live process that passes every other gate. A token that
      cannot be read on either side is treated as unproven identity and the
      member is skipped, never as a mismatch -- declining to act is not the
      same as asserting recycling, and a skipped orphan is re-reaped next
      sweep. Logged at debug so a host where identity is never available is
      diagnosable rather than a silent no-op.

    A member whose cmdline is unreadable is skipped rather than killed: the
    marker read and the exclusion check both need it, and failing closed here
    costs one sweep cycle while failing open could kill a live peer.

    Returns the number of processes killed.
    """
    if budget <= 0 or not descendants:
        return 0
    my_pid = os.getpid()
    my_pgid = os.getpgrp()
    killed = 0
    for target, walk_token in reversed(descendants):
        if killed >= budget:
            break  # global kill cap exhausted; next sweep cycle finishes the job
        if target <= 1 or target == my_pid or target == my_pgid or target == root:
            continue
        cmdline = _pid_cmdline(target)
        if not cmdline:
            continue  # vanished or unreadable — fail closed
        if any(marker in cmdline.replace(b"\x00", b" ") for marker in _GATEWAY_MARKERS):
            # Defence in depth: _prune_from_orphan_walk already dropped this
            # subtree during enumeration. Kept because a caller could pass a
            # list it assembled some other way.
            continue
        if not _env_has_kirocrew_marker(target):
            continue  # not provably part of a Kiro Crew tree
        live_token = _pid_start_token(target)
        if walk_token is None or live_token is None:
            logger.debug(
                "Orphan MCP sweep: skipping pid=%d — start identity unavailable "
                "(walk=%r live=%r), re-reaped next sweep",
                target,
                walk_token,
                live_token,
            )
            continue  # identity unproven — never kill on an unverifiable PID
        if live_token != walk_token:
            logger.debug(
                "Orphan MCP sweep: skipping pid=%d — PID recycled since enumeration",
                target,
            )
            continue  # a different process now holds this PID
        try:
            platform_compat.kill_pid(target, platform_compat.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        killed += 1
        logger.info(
            "Orphan MCP sweep: SIGKILL pid=%d reason=descendant of MCP launcher orphan %d",
            target,
            root,
        )
    if killed:
        _sel_orphan_mcp_subtree_kill(root, killed)
    return killed


def _sel_orphan_mcp_subtree_kill(root: int, killed: int) -> None:
    """Emit SEL audit event for an MCP-launcher subtree kill."""
    try:
        # Lazy import to avoid a circular import (see kill_orphan_mcps).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="gateway",
            agent="kirocrew",
            source="background",
            tool_name="orphan_mcp_sweep",
            tool_kind="process_kill",
            outcome="completed",
            resources=f"root={root} method=mcp_subtree",
            metadata={
                "killed_in_tree": killed,
                "reason": "descendants of KIROCREW_SPAWNED MCP launcher orphan",
            },
        )
    except Exception:
        logger.debug("SEL orphan-mcp-subtree-kill audit failed", exc_info=True)


def _kill_orphan_work_tree(pid: int, cmdline: bytes, age_seconds: float, budget: int) -> int:
    """SIGKILL a confirmed work-class orphan and its WHOLE subtree, leaf-first.

    Deliberately NOT :func:`_kill_pid_tree`: that helper only reaps
    descendants that are themselves managed agent runtimes (kiro-cli/claude),
    which is correct for tracked agent PIDs but wrong here — every descendant
    of a marked work orphan inherited the ``KIROCREW_SPAWNED`` environment
    and is sweepable (an orphaned pytest's own python/shim children are
    exactly the processes that pile up).

    Descendants are enumerated once (preorder) and killed in reverse, so
    every process dies before its parent — no child is re-parented away
    mid-kill and the enumeration stays valid. The root goes last. *budget*
    bounds the total SIGKILLs so the caller's global
    :data:`_ORPHAN_SWEEP_MAX_KILLS` cap covers subtree members too; if the
    budget runs out mid-subtree the survivors are re-reaped next sweep cycle.

    Returns the number of processes killed.
    """
    if budget <= 0:
        return 0
    descendants: list[int] = []
    try:
        # circular import: session_pid → acp.client → session → session_pid
        from kiro_crew.acp.client import _get_child_pids

        descendants = _get_child_pids(pid)
    except Exception:
        logger.debug("Error enumerating descendants of work orphan %s", pid, exc_info=True)
    my_pid = os.getpid()
    basename = _work_orphan_basename(cmdline).decode("utf-8", errors="replace")
    killed = 0
    for target in [*reversed(descendants), pid]:
        if killed >= budget:
            break  # global kill cap exhausted; next sweep cycle finishes the job
        if target <= 0 or target == my_pid:
            continue
        try:
            platform_compat.kill_pid(target, platform_compat.SIGKILL)
            killed += 1
            if target == pid:
                logger.info(
                    "Orphan work sweep: SIGKILL pid=%d basename=%s age=%ds "
                    "reason=KIROCREW_SPAWNED work orphan (reparented to init)",
                    target,
                    basename,
                    int(age_seconds),
                )
            else:
                logger.info(
                    "Orphan work sweep: SIGKILL pid=%d reason=descendant of work orphan %d",
                    target,
                    pid,
                )
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if killed:
        _sel_orphan_work_kill(pid, basename, age_seconds, cmdline, killed)
    return killed


def _sel_orphan_work_kill(
    pid: int, basename: str, age_seconds: float, cmdline: bytes, killed: int
) -> None:
    """Emit SEL audit event for a work-orphan subtree kill."""
    try:
        # Lazy import to avoid a circular import (see kill_orphan_mcps).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="gateway",
            agent="kirocrew",
            source="background",
            tool_name="orphan_work_sweep",
            tool_kind="process_kill",
            outcome="completed",
            resources=f"pid={pid} basename={basename} age={int(age_seconds)}s method=work_tree",
            metadata={
                "cmdline": cmdline[:200].decode("utf-8", errors="replace"),
                "killed_in_tree": killed,
                "reason": "KIROCREW_SPAWNED orphan work process",
            },
        )
    except Exception:
        logger.debug("SEL orphan-work-kill audit failed", exc_info=True)


_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _read_rss_pages(pid: int, proc_root: Path | None = None) -> int:
    """Resident *pages* of a single PID via ``/proc/<pid>/statm`` (Linux only).

    Returns pages, NOT MiB: callers accumulate the whole process tree and
    convert to MiB once at the end, so per-PID sub-MiB remainders are not
    truncated away. (A per-PID ``// MiB`` would under-count a tree by up to
    ~1 MiB per process, i.e. the recycle could fire late or never for a tree
    sitting just over the ceiling.) Returns 0 if the process is gone or the
    field can't be read — a missing PID simply contributes nothing to the sum.

    Windows never reaches here: ``get_session_rss_mb`` measures whole trees
    through ``platform_compat.proc_rss_tree_mb_for_pid`` instead.

    *proc_root* overrides the ``/proc`` mount (test seam only).
    """
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        # statm fields are in pages; field 2 (index 1) is resident set size.
        fields = (root / str(pid) / "statm").read_text().split()
        return int(fields[1])
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OSError):
        return 0


def _build_child_map(proc_root: Path | None = None) -> dict[int, list[int]]:
    """Parent-PID -> direct-children map from one pass over ``/proc/<pid>/stat``.

    Reads the ``PPid`` (4th) field of every process's ``stat`` file. This is
    authoritative and complete for all live processes regardless of kernel
    config, and deliberately replaces the earlier
    ``/proc/<pid>/task/*/children`` walk, which requires
    ``CONFIG_CHECKPOINT_RESTORE``/``CONFIG_PROC_CHILDREN`` and is documented as
    reliable only for frozen/stopped tasks — for a live task it could return an
    incomplete child set, silently dropping whole descendant subtrees from the
    RSS sum (so the memory-protection feature could no-op with no signal).

    A failure to scan ``/proc`` is logged at debug rather than swallowed
    silently, so a degraded reading is diagnosable.

    Windows deliberately has NO branch here and returns an empty map: Toolhelp's
    ``th32ParentProcessID`` is never cleared when a parent exits and Windows
    recycles PIDs aggressively, so a raw parent->child walk can attach an
    unrelated subtree to a recycled PID -- which would let the watchdog recycle a
    healthy session. ``get_session_rss_mb`` routes Windows through
    ``platform_compat.proc_rss_tree_mb_for_pid``, which validates every
    parent->child edge against creation/exit times, instead of coming here.

    *proc_root* overrides the ``/proc`` mount (test seam only).
    """
    root = proc_root if proc_root is not None else Path("/proc")
    child_map: dict[int, list[int]] = {}
    try:
        for entry in root.iterdir():
            name = entry.name
            if not name.isdigit():
                continue
            try:
                # Format: "pid (comm) state ppid ...". comm can contain spaces
                # and parentheses, so locate the LAST ')' and read ppid after
                # it rather than naively splitting on whitespace.
                stat = (entry / "stat").read_text()
                rparen = stat.rfind(")")
                ppid = int(stat[rparen + 2 :].split()[1])
            except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OSError):
                # Process exited mid-scan or stat unreadable — skip this PID.
                continue
            child_map.setdefault(ppid, []).append(int(name))
    except (FileNotFoundError, OSError):
        logger.debug("RSS watchdog: /proc scan for child map failed", exc_info=True)
    return child_map


def _rss_mb_from_tree(
    pid: int,
    child_map: dict[int, list[int]],
    exclude_pids: set[int] = frozenset(),  # type: ignore[assignment]
    proc_root: Path | None = None,
) -> int:
    """RSS (MiB) of *pid* + its descendant tree using a PREBUILT child map.

    Split out from ``get_session_rss_mb`` so a caller measuring many session
    trees in one sweep can build the ``/proc`` parent->child map ONCE (via
    ``_build_child_map``) and reuse it across every tree, rather than re-scanning
    all of ``/proc`` per tree. The map is read-only here, so it is safe to share
    across sequential/threaded calls. Any PID in *exclude_pids* is skipped along
    with its subtree. Resident pages are summed across the tree and converted to
    MiB once at the end.
    """
    total_pages = 0
    seen: set[int] = set()
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        if current in seen or current in exclude_pids:
            continue
        seen.add(current)
        total_pages += _read_rss_pages(current, proc_root)
        frontier.extend(child_map.get(current, ()))
    return (total_pages * _PAGE_SIZE) // (1024 * 1024)


def get_session_rss_mb(
    pid: int,
    exclude_pids: set[int] = frozenset(),  # type: ignore[assignment]
    proc_root: Path | None = None,
) -> int:
    """Total RSS (MiB) of *pid* plus its descendant tree, via ``/proc``.

    Single-tree convenience: builds the parent->child map with one
    ``/proc/*/stat`` scan (see ``_build_child_map``) and delegates to
    ``_rss_mb_from_tree``. To measure MANY trees in one sweep, build the map
    once with ``_build_child_map()`` and call ``_rss_mb_from_tree()`` per tree so
    ``/proc`` is scanned only once, not once per tree.

    Any PID in *exclude_pids* is skipped along with the entire subtree beneath
    it — a defensive barrier so a caller can exclude a shared sub-tree (e.g. a
    pooled backend). Resident pages are summed and converted to MiB once at the
    end, so the reading is not biased downward by per-PID truncation.

    *proc_root* overrides the ``/proc`` mount (test seam only).

    Linux reads ``/proc``. Windows has neither ``/proc`` nor a safe parent->child
    walk (see ``_build_child_map``), so it delegates to
    ``platform_compat.proc_rss_tree_mb_for_pid``, which sums only
    lineage-validated descendants; without that the ceiling measured every tree
    as 0 MiB there and no session was ever recycled. macOS has no ctypes-only
    per-pid RSS path, so it returns 0 and the ceiling stays inert.

    *exclude_pids* is honoured on the ``/proc`` route. The Windows route derives
    its own validated descendant set, so a caller that needs a subtree barrier
    there must exclude the pid before calling.
    """
    if platform_compat.IS_WINDOWS and proc_root is None:
        tree_mb = platform_compat.proc_rss_tree_mb_for_pid(pid)
        return 0 if tree_mb is None else int(tree_mb)
    if sys.platform != "linux":
        return 0
    child_map = _build_child_map(proc_root)
    return _rss_mb_from_tree(pid, child_map, exclude_pids, proc_root)
