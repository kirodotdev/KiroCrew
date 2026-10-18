"""Whether a process is a managed agent runtime, decided from its command line.

The marker and basename sets projected from the agent backends, argv tokenising, the
namespace sandbox-launcher unwrap, the positional harness-naming rule and
:func:`_is_managed_agent_process`, the argv gate every tracked-pid kill re-checks before
it signals; plus the two command-line readers the gate and the orphan sweeps share.

Split out of ``kiro_crew.session_pid``, which stays the import path and the patch target:
every name here is reached as ``kiro_crew.session_pid.<name>``, and a patch through that
module reaches this one (``kiro_crew._facade_mirror``). Nothing here imports it.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.agent_sdk.backends import agent_process_markers, node_adapter_entry_relpaths

#: The family logs as ``kiro_crew.session_pid``, the name callers, log filters and
#: ``caplog`` assertions use.
logger = logging.getLogger("kiro_crew.session_pid")


# Basenames of agent runtimes whose lifecycle Kiro Crew manages through PID-file
# tracking (kiro_pids.txt / kiro_session_pids.txt). Consulted to re-validate tracked
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


# The CPython executable, as ``sandbox.namespace_argv`` spells it in argv0 of the
# launcher it builds: ``sys.executable``, so whatever interpreter the GATEWAY itself
# runs under -- ``python``, ``python3`` or ``python3.<minor>``. Matched as a PREFIX of
# the lowercased basename because the minor version is open-ended, and the direction of
# that looseness is the safe one: it cannot admit a command line on its own, since the
# launcher flags and the generated script name still have to be in their exact
# positions, and a host whose interpreter is named something else entirely (a renamed or
# embedded build) costs a MISSED reclaim rather than a signal to a process Crew never
# spawned.
_LAUNCHER_INTERPRETER_NAME_PREFIX = b"python"


def _sandbox_launcher_wrapped_argv(tokens: list[bytes]) -> list[bytes] | None:
    """The argv Crew's own Linux sandbox launcher was asked to RUN, or ``None``.

    When the namespace backend is in use -- ``sandbox.detect_backend() == "namespace"``,
    which is what the shipped ``auto`` resolves to on any Linux host whose kernel allows
    an unprivileged user namespace -- the pid the gateway tracks for an agent is NOT the
    harness. ``sandbox.namespace_argv`` wraps the spawn as

        ``<interpreter> -I -S <run dir>/kirocrew_sandbox_<pid>_<rand>.py <harness argv…>``

    and the launcher's parent never execs: it writes the child's uid/gid maps and then
    blocks in ``waitpid`` for the life of the session
    (``sandbox_launcher_program.main``). So the gate's ``argv[0]`` is the interpreter and
    the harness sits past the script, out of reach of :func:`_harness_naming_tokens`'s two
    positions.

    Recognising it is not optional, because an unrecognised agent root is UNRECLAIMABLE
    rather than spared: :func:`_sweep_pid_entries` RETAINS a settled-token entry the argv
    gate does not recognise (dropping the record would leave the process unfindable by
    every sweep) and the scope reaper refuses a scope holding a tracked pid, so for as
    long as the OWNING GATEWAY lives nothing reaches the kiro-cli runtime under it.
    :func:`cleanup_orphaned_session_roots` applies this same gate, so a restart does not
    clear it either; what ends the leak is the launcher REPARENTING when its gateway
    dies, which moves it into the orphan-MCP sweep's population
    (:func:`_is_orphan_mcp` accepts it on :data:`_SANDBOX_LAUNCHER_MARKER`). A gateway
    that stays up holds the whole runtime for its own lifetime.

    Recognition is POSITIVE and names exactly the one shape ``namespace_argv`` emits.
    Five things must all hold, and the first four are what keep an arbitrary Python
    process out of a KILL path:

    * ``argv[0]``'s basename is a CPython spelling
      (:data:`_LAUNCHER_INTERPRETER_NAME_PREFIX`);
    * the launcher's interpreter flags occupy the next positions EXACTLY, in order;
    * that token's basename carries the prefix AND the suffix ``sandbox_argv.py`` generates
      its launcher tempfile with -- the suffix because the ``.sb`` seatbelt profile is
      written beside it under the same prefix;
    * its DIRECTORY is the one ``namespace_argv`` writes launchers into;
    * at least one token follows, which is the wrapped argv.

    Every one of those values comes from the ``sandbox`` module rather than being spelled
    again here -- the three constants through :func:`_namespace_launcher_shape`, the
    directory through :func:`sandbox.namespace_launcher_script_dir` -- so adding a flag,
    moving the run directory or renaming the artifact moves this recogniser's idea of the
    script slot with the builder's instead of silently handing a flag token to the name
    test.

    A plain ``python foo.py kiro-cli`` fails three of the five, which is the point: the
    wrapped slot is only consulted for a command line that already presents Crew's own
    generated launcher, in Crew's own directory, in Crew's own position for it.

    The directory comparison is EXACT and lexical in both directions, because
    ``namespace_argv`` hands ``mkstemp`` an already-normalized directory and ``mkstemp``
    returns ``os.path.join`` of it. Normalizing the token instead would widen what a kill
    path accepts, to ``run/../run`` and to a relative spelling resolved against the
    SWEEPING gateway's cwd rather than anything the target process named.

    ORDER IS DELIBERATE, and it is about cost and about blast radius. The free tests on
    the command line run first and the FILESYSTEM-touching one runs last: resolving the
    launcher directory goes through ``config_dir()``, which creates the data home if it
    is missing and can raise, and this function is called once per tracked entry by every
    sweep. An ordinary ``kiro-cli`` argv, which is the common case, now answers without
    reaching it at all.

    AND IT NEVER RAISES. Returning ``None`` means "not the launcher", which costs a
    missed reclaim; propagating would abort the whole sweep for every remaining entry,
    and ``cleanup_orphaned_session_roots``'s caller swallows the error without a
    per-entry guard, so one unreadable data home would silently stop reclaiming
    anything. The broad ``except`` is therefore the fail-safe direction, not a widening:
    the only thing it can do is decline to recognise.

    ONE unwrap, not a loop. A launcher inside a launcher is not merely something Crew
    declines to build: the launcher's own seccomp-BPF filter DENIES ``unshare`` (and
    ``mount``/``umount2``/``setns``/``pivot_root``) for everything in the sandboxed tree,
    and refuses the spawn outright on an architecture where that filter cannot be
    installed -- so a second launcher inside the first cannot reach its own namespace at
    all. ``wrap_argv`` detecting the in-sandbox marker and passing through is the layer
    above that. Accepting the nested shape would therefore be authority granted for one
    the OS already makes impossible.
    """
    if len(tokens) < 3:
        return None
    if not _basename_of(tokens[0]).lower().startswith(_LAUNCHER_INTERPRETER_NAME_PREFIX):
        return None
    try:
        flags, script_prefix, script_suffix = _namespace_launcher_shape()
        script_index = 1 + len(flags)
        if len(tokens) < script_index + 2:
            return None
        if tuple(tokens[1:script_index]) != flags:
            return None
        # ONE split for both halves. Taking the name from ``_basename_of`` (which also
        # splits on ``\``, for a Windows argv0 carried whole into an exact-name test) and
        # the directory from ``os.path.dirname`` (which on POSIX splits only on ``/``)
        # let ``<run dir>/zz\kirocrew_sandbox_1.py`` satisfy both at once, though the
        # real file name there starts with ``zz``.
        directory, name = os.path.split(tokens[script_index])
        if not name.startswith(script_prefix) or not name.endswith(script_suffix):
            return None
        # Last, because this is the one part that touches the filesystem. The TOKEN is
        # compared as the writer spelled it, with no normalization of its own: the writer
        # hands ``mkstemp`` an already-normalized ABSOLUTE directory, so the only spelling
        # this accepts is the one it produces. Normalizing here would additionally admit
        # ``run/../run`` and a trailing separator, and would resolve a RELATIVE token
        # against the SWEEPING gateway's cwd -- so ``run/kirocrew_sandbox_1.py`` would
        # match whenever that cwd happened to be the data home, naming a path the target
        # process never did.
        if directory != _namespace_launcher_script_dir():
            return None
        return tokens[script_index + 1 :]
    except Exception:
        # WARNING, not debug: the only way here is the launcher shape being unreadable,
        # which means ``config_dir()`` is failing, and the visible consequence is that
        # every sandboxed agent root silently stops being reclaimable. Bounded by the
        # tracked entries one sweep examines, the same way the identity-mismatch warnings
        # on this path are.
        logger.warning(
            "managed-agent gate: could not read the sandbox launcher shape; treating "
            "the command line as not-the-launcher, so a sandboxed agent root will not "
            "be reclaimed this pass",
            exc_info=True,
        )
        return None


def _namespace_launcher_shape() -> tuple[tuple[bytes, ...], bytes, bytes]:
    """``sandbox``'s launcher-shape constants, in the bytes domain this module reads in.

    The flags, the artifact prefix and the launcher's suffix, read off the module that
    GENERATES the launcher rather than spelled again here, so adding a flag or renaming
    the artifact moves this recogniser's idea of the script slot with the builder's.
    ``clone_setup`` reads the same two name constants the same way.

    Read through the module object, not bound at import, so a test that repoints any of
    them gets its own back -- and imported inside the call, for a reason that is not
    import weight: ``acp.runtime`` already imports ``sandbox`` at module scope, so by the
    time any sweep runs this is a ``sys.modules`` hit. It is that ``session_pid`` is the
    pid bookkeeping leaf, and a module-scope import would make
    ``import kiro_crew.session_pid`` fail for anything that goes wrong in ``sandbox``'s
    own import graph. That has cost this repository a whole test RUN before: an
    import-time assertion in ``sandbox`` made every backend test fail at COLLECTION
    rather than in one test. A gate that must never raise should not be reachable only
    through an import that can.
    """
    from kiro_crew import sandbox

    return (
        tuple(flag.encode() for flag in sandbox._LAUNCHER_INTERPRETER_FLAGS),
        sandbox._SANDBOX_ARTIFACT_PREFIX.encode(),
        sandbox._LAUNCHER_SCRIPT_SUFFIX.encode(),
    )


def _namespace_launcher_script_dir() -> bytes:
    """:func:`sandbox.namespace_launcher_script_dir`, encoded.

    Already normalized there, which is what lets the comparison against a ``/proc`` token
    be exact: ``namespace_argv`` hands that same value to ``mkstemp``, so an uncollapsed
    data home is collapsed on the WRITER's side and both sides agree without this one
    touching the spelling at all. The case that needs it is the DEFAULT home under a
    ``HOME`` containing ``..`` — an explicit ``KIROCREW_HOME`` arrives already clean,
    because ``config.paths._valid_override_home`` calls ``.resolve()`` on it.

    ``os.fsencode``, not ``str.encode``: a data home whose path is not valid UTF-8
    reaches Python as surrogate escapes, and only ``fsencode`` turns those back into the
    bytes ``/proc/<pid>/cmdline`` actually holds.
    """
    from kiro_crew import sandbox

    return os.fsencode(sandbox.namespace_launcher_script_dir())


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

    Both positions are taken on the WRAPPED argv when the command line is Crew's own
    Linux sandbox launcher (:func:`_sandbox_launcher_wrapped_argv`), because there the
    kernel execed the launcher and the harness is what the launcher was handed. The two
    rules above then apply unchanged to that inner argv -- including the Node script
    slot, so a sandboxed bespoke adapter is recognised the same way an unsandboxed one
    is. Stepping over the wrapper is the ONLY thing that changes; nothing about which
    inner position may name a harness is relaxed to pay for it.
    """
    tokens = _argv_tokens(cmdline)
    if not tokens:
        return []
    wrapped = _sandbox_launcher_wrapped_argv(tokens)
    if wrapped is not None:
        tokens = wrapped
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
