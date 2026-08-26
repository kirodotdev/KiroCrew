"""Resolve an npm-launcher MCP command once, then launch the resolved tree.

An ``npx``-style spec re-resolves its dependency tree on every launch: npm asks
the registry what the spec means, walks the tree, and rewrites its lockfile. That
puts a network round trip and a full resolution pass on the session-start path,
where a slow or unreachable registry becomes an unbounded stall and where the
resolution's own cost grows with the cache's history.

This module moves that work off the launch path. A resolved tree lives under
``<data home>/mcp/resolved/<digest>/`` beside a :class:`ResolvedRecord`; launching
then means ``exec``-ing the recorded entry point with no npm involvement at all.

Two properties the design holds to:

* **The lookup is synchronous and cheap.** ``resolved_launch`` is called on the
  spawn path (``TargetResolver`` is a sync callable), so it only ever reads a
  small JSON file. Installing is somebody else's job, done ahead of time.
* **A miss is not a failure.** No record, an unreadable record, or a tree whose
  entry point has gone missing all return ``None``, and the caller falls back to
  the original ``npx`` invocation. The feature can only remove a stall, never
  introduce one.

Freshness follows the spec's own promise. A spec pinned to an exact version means
the same tree forever, so it is resolved once and never revisited. An unpinned
spec (``@latest``, a range, or no version at all) is asked to keep up with
upstream, so its record expires and is re-resolved -- on a clock rather than on
every single launch. The clock is ticked by a periodic pass in the gateway, not
only at boot, because a gateway that stays up for weeks would otherwise freeze an
unpinned spec at whatever it resolved to on the day it started.

Substitution is deliberately narrow, because a hit that runs the WRONG program
would be worse than never substituting at all: the record is only written when
``npx`` would unambiguously pick the same file (see :func:`_bin_relpath`) and
when ``node`` is the right way to start it (see :func:`_is_node_runnable`).
Anything else misses.

One divergence from ``npx`` remains and is accepted: ``npx`` puts the tree's
``node_modules/.bin`` on ``PATH``, so a server that shells out to a SIBLING
package's binary by bare name finds it there and would not here. It is not
fixable in the resolver wrapper: env is passed through exactly as the inner
resolver computed it so the ``PoolKey``'s env hash keeps describing what is
actually spawned, and rewriting ``PATH`` would break that. MCP servers are stdio
node processes and rarely do this, but a server that does needs its own spec left
unresolved.

A second limitation is open and deliberately NOT papered over. The store is filled
by the gateway's own ``npm`` with the gateway's own environment, so a server whose
DECLARED env points npm somewhere else -- a private registry, a token, a different
toolchain -- could be handed a tree resolved from the wrong place. The check cannot
live on the read side: the resolver is synchronous and the env it receives is
``_scrub_sensitive_env(dict(os.environ))``, a full inherited environment that
cannot be told apart from a server's declarations, while the real per-server
declarations (``gatewayd._declared_env_pairs``) require a config load and a file
read that its own docstring forbids on the event loop. Closing it properly belongs
on the write side, where the prefetcher may do I/O and can decline a spec whose
servers declare an npm-affecting env -- which needs the rewriter to surface those
declarations. Until then, an operator running MCP servers against a private
registry should not rely on this path.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from kiro_crew.platform_compat import kill_and_reap
from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv

logger = logging.getLogger(__name__)

#: How long an UNPINNED spec's resolution is trusted before it is re-resolved.
#: An unpinned spec asked to track upstream, so it cannot be frozen forever;
#: re-resolving on a clock keeps that promise while still keeping the work off
#: every launch. Pinned specs ignore this entirely -- there is nothing to re-ask.
DEFAULT_REFRESH_SECS = 24 * 60 * 60

#: Launcher basenames whose invocation is "fetch a package, then run it". These
#: are the only commands rewritten; anything else passes through untouched.
_NPM_LAUNCHERS = frozenset({"npx", "npx.cmd", "npx.exe"})

#: ``npm exec <pkg>`` is the non-shorthand spelling of the same thing.
_NPM_BINARIES = frozenset({"npm", "npm.cmd", "npm.exe"})

#: Flags ``npx`` consumes itself, so they are not the package spec and must not
#: be forwarded to the resolved entry point.
_NPX_OWN_FLAGS = frozenset({"-y", "--yes", "--prefer-offline", "--quiet"})

#: Flags that FORBID what this module does. ``npx --no-install`` means "run only
#: what is already present, otherwise fail" -- it is the user refusing a download.
#: Discarding it as a launcher flag and then installing the package anyway would
#: run the very lifecycle code that invocation declined, so a spec carrying one of
#: these is not ours to resolve at all.
_NPX_REFUSES_INSTALL = frozenset({"--no-install", "--no"})

#: A version that pins exactly: digits.digits.digits plus an optional prerelease
#: or build tail. Anything else -- a dist-tag, a range operator, a wildcard, or
#: no version at all -- is treated as unpinned.
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


@dataclass(frozen=True)
class NpmSpec:
    """An npm-launcher invocation split into the parts that matter.

    ``package`` is the spec as written (``foo``, ``foo@1.2.3``, ``@scope/foo@latest``);
    ``passthrough`` is everything after it, which belongs to the server rather
    than to npm and is replayed verbatim onto the resolved entry point.
    """

    package: str
    passthrough: tuple[str, ...]

    @property
    def pinned(self) -> bool:
        """True when the spec names one exact version and so never needs re-asking."""
        return _EXACT_VERSION.match(_version_of(self.package)) is not None

    @property
    def digest(self) -> str:
        """Stable identity for this invocation.

        Keyed on the spec as WRITTEN, not on what it resolved to: the record's
        identity is the question, its content is the answer. So an unpinned spec
        keeps one directory across refreshes instead of leaking a new one per
        upstream release.
        """
        payload = "\x00".join((self.package, *self.passthrough))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ResolvedRecord:
    """What one resolution produced, as persisted next to the installed tree.

    Deliberately holds only what a launch needs. The resolved version is NOT
    persisted here: nothing reads it, and it is already recoverable from the
    ``package.json`` of the very tree ``entrypoint`` points into, so a copy in
    this file would be duplicated state one directory away from its source. The
    install log still names the version, which is where a human looks for "what
    did ``@latest`` become".
    """

    package: str
    entrypoint: str
    resolved_at: float
    pinned: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "entrypoint": self.entrypoint,
            "resolved_at": self.resolved_at,
            "pinned": self.pinned,
        }

    @classmethod
    def from_json(cls, raw: Any) -> Optional["ResolvedRecord"]:
        """Parse a record, returning ``None`` for anything malformed.

        The file is ours, but a truncated write or a hand-edit must degrade to a
        cache miss rather than raise on the spawn path.
        """
        if not isinstance(raw, dict):
            return None
        package = raw.get("package")
        entrypoint = raw.get("entrypoint")
        resolved_at = raw.get("resolved_at")
        if not isinstance(package, str) or not package:
            return None
        if not isinstance(entrypoint, str) or not entrypoint:
            return None
        if not isinstance(resolved_at, (int, float)):
            return None
        return cls(
            package=package,
            entrypoint=entrypoint,
            resolved_at=float(resolved_at),
            pinned=bool(raw.get("pinned")),
        )


def _version_of(package: str) -> str:
    """The version part of a package spec, or ``""`` when it names none.

    A leading ``@`` is the scope marker, so the separator is the LAST ``@`` and
    only when it is not at position 0 -- otherwise ``@scope/foo`` reads as
    package ``""`` at version ``scope/foo``.
    """
    at = package.rfind("@")
    if at <= 0:
        return ""
    return package[at + 1 :]


def parse_npm_launcher(command: str, args: list[str]) -> Optional[NpmSpec]:
    """Split an npm-launcher invocation, or ``None`` if this is not one.

    Recognises ``npx <flags> <pkg> <args...>`` and its ``npm exec`` spelling.
    Deliberately conservative: an invocation carrying an npx flag this module
    does not know is left alone rather than guessed at, because mis-parsing the
    package boundary would launch the wrong thing.
    """
    base = os.path.basename(command).lower()
    rest = list(args)
    if base in _NPM_BINARIES:
        if not rest or rest[0] != "exec":
            return None
        rest = rest[1:]
    elif base not in _NPM_LAUNCHERS:
        return None

    while rest and rest[0].startswith("-"):
        # ``--`` ends npm's own arguments; whatever follows is the package. It
        # has to be handled inside this loop, not after it, or it reads as an
        # unknown flag and the whole invocation passes through.
        if rest[0] == "--":
            rest = rest[1:]
            break
        if rest[0] in _NPX_REFUSES_INSTALL:
            # The invocation itself refuses a download. Pre-resolving it would
            # install and run exactly what it declined.
            return None
        if rest[0] not in _NPX_OWN_FLAGS:
            # An unknown flag may or may not take a value, so the package
            # boundary is no longer knowable. Pass through untouched.
            return None
        rest = rest[1:]

    if not rest:
        return None
    package = rest[0]
    if package.startswith("-"):
        # After ``--`` the next word is the package by npx's rules, but nothing
        # stops it from LOOKING like an option, and this module later hands it to
        # ``npm install``. A spec of ``--prefix=/some/project`` would be parsed by
        # npm as an option and redirect the install into that directory. npm's own
        # ``--`` separator is used at the call site as well; this refuses the
        # shape outright so there is no single point to get wrong.
        logger.debug("resolve-once: flag-shaped package spec %r; leaving it to npx", package)
        return None
    return NpmSpec(package=package, passthrough=tuple(rest[1:]))


def store_root(data_home: str) -> str:
    """Directory holding every resolved tree for this data home."""
    return os.path.join(data_home, "mcp", "resolved")


def spec_dir(data_home: str, spec: NpmSpec) -> str:
    return os.path.join(store_root(data_home), spec.digest)


def _record_file(directory: str) -> str:
    return os.path.join(directory, "record.json")


def read_record(directory: str) -> Optional[ResolvedRecord]:
    """Load the record in ``directory``, or ``None`` if absent or unusable."""
    try:
        with open(_record_file(directory), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return ResolvedRecord.from_json(raw)


def write_record(directory: str, record: ResolvedRecord) -> None:
    """Persist ``record`` into ``directory`` atomically.

    Written to a sibling then renamed so a reader on the spawn path never
    observes a half-written file.

    The temp name is UNIQUE per writer, not a fixed ``record.json.tmp``. Two
    writers can legitimately be here at once -- the timed pass and an operator
    pressing "Update now" -- and a shared temp path made them collide in a way
    that corrupted the store rather than merely wasting work: the second writer
    truncated the first's temp file, the first's ``os.replace`` published the
    SECOND's bytes, and the second's own rename then failed with the temp file
    already gone, sending it down the cleanup path where it deleted the very tree
    the freshly published record now pointed at. With per-writer temp files both
    renames succeed, the last one wins, and both trees are still on disk -- the
    loser's is collected later by the grace-windowed sweep.
    """
    target = _record_file(directory)
    fd, tmp = tempfile.mkstemp(prefix="record.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record.to_json(), fh)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def is_stale(record: ResolvedRecord, *, refresh_secs: float, now: Optional[float] = None) -> bool:
    """Whether ``record`` should be re-resolved.

    A pinned spec is never stale: re-asking the registry about an exact version
    can only return the same answer. An unpinned one goes stale on the clock,
    which is what keeps ``@latest`` meaning latest without paying for it on
    every launch.
    """
    if record.pinned:
        return False
    if refresh_secs <= 0:
        return True
    current = time.time() if now is None else now
    return (current - record.resolved_at) >= refresh_secs


def resolved_launch(
    data_home: str, command: str, args: list[str]
) -> Optional[tuple[str, list[str]]]:
    """The direct ``(command, args)`` for an already-resolved spec, else ``None``.

    Synchronous and IO-light by contract: this runs on the backend spawn path.
    Every miss -- not an npm launcher, no record, an entry point that has since
    been removed -- returns ``None`` so the caller keeps today's invocation.
    Staleness is deliberately NOT consulted here: a stale tree still launches
    correctly, and refreshing is the prefetcher's job, off this path.
    """
    spec = parse_npm_launcher(command, args)
    if spec is None:
        return None
    directory = spec_dir(data_home, spec)
    record = read_record(directory)
    if record is None:
        return None
    # `spec_dir` keys on `NpmSpec.digest`, a SHA-256 truncated to 64 bits, so a
    # birthday-bound collision (~2^32 specs) could resolve one spec to the tree
    # installed for another and exec the WRONG program. The path-containment and
    # isfile checks below do not catch that: both would pass for the colliding
    # tree. Confirm the stored record was written for THIS spec before trusting
    # its entrypoint; on mismatch treat it as a cache miss and let the caller
    # keep today's invocation.
    if record.package != spec.package:
        return None
    entrypoint = os.path.join(directory, record.entrypoint)
    # Re-check containment on read, not just on write: the record is a plain
    # file, so a hand-edit or a partially-overwritten one must not turn into an
    # exec of an arbitrary path.
    if not os.path.realpath(entrypoint).startswith(os.path.realpath(directory) + os.sep):
        return None
    if not os.path.isfile(entrypoint):
        # The tree was pruned or a partial install left the record behind.
        return None
    # Bare ``node``, deliberately not ``shutil.which("node")``. This function is
    # called synchronously from the target resolver, which runs on the gateway's
    # event loop, and ``which`` stats EVERY PATH entry -- one stalled NFS or
    # autofs mount would freeze routing and heartbeat processing for every
    # session, not just this one. The spawn resolves the name off the loop
    # anyway (``sandbox.create_subprocess_limited`` does the PATH search in a
    # thread), and the launcher this replaces was itself found on PATH at spawn
    # time, so a host with no ``node`` fails exactly where it already did.
    return "node", [entrypoint, *spec.passthrough]


# --- Filling the store (async, never on the spawn path) ----------------------

#: Upper bound on one install. The point of this module is that no launch waits
#: on the network, so the install that buys that must itself be bounded -- an
#: unbounded fetch here would just move the stall to the prefetcher.
DEFAULT_INSTALL_TIMEOUT_SECS = 300.0

#: Prefix for one resolution's tree inside a spec's directory. Several may
#: coexist briefly: the record names the live one, and the others are swept.
_RESOLUTION_PREFIX = "r-"


def _drop_sandbox_launcher(path: Optional[str]) -> None:
    """Remove the sandbox's temp launcher/profile once the child is done.

    Idempotent and silent: the caller's contract is to unlink it after the child
    exits, and failing to is a leaked temp file, never a reason to fail an
    install that otherwise succeeded.
    """
    if not path:
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


def package_name(package: str) -> str:
    """The name part of a package spec, with any version removed."""
    at = package.rfind("@")
    if at <= 0:
        return package
    return package[:at]


def _read_json(path: str) -> Optional[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _bin_relpath(manifest: dict[str, Any], name: str) -> Optional[str]:
    """The executable to run for ``name``, from its ``package.json``.

    Deliberately narrow: it returns a path ONLY where ``npx`` would
    unambiguously pick the same one -- a string ``bin``, a table entry whose key
    matches the package's own last path segment, or a table with exactly one
    entry. Everything else returns ``None`` and misses to ``npx``.

    An earlier version fell back to first-by-sort-order for an ambiguous table
    and to ``main`` when ``bin`` was absent. Both were wrong in the same
    direction, and the direction is what matters: ``npx`` ERRORS on an ambiguous
    table and never runs ``main``, so those fallbacks made a successful
    pre-resolution launch a DIFFERENT program than the launch it replaced. That
    inverts this module's whole contract -- a miss is free, but a hit that runs
    the wrong binary is a failure the user only sees after a prefetch succeeds.
    """
    raw = manifest.get("bin")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict) and raw:
        preferred = name.rsplit("/", 1)[-1]
        candidate = raw.get(preferred)
        if isinstance(candidate, str) and candidate:
            return candidate
        if len(raw) == 1:
            only = next(iter(raw.values()))
            if isinstance(only, str) and only:
                return only
    # Ambiguous table, or no ``bin`` at all: not something npx would resolve to
    # one file, so this spec is not ours to substitute.
    return None


#: Suffixes ``node <file>`` runs correctly regardless of the file's first line.
_NODE_SUFFIXES = (".js", ".mjs", ".cjs")


def _is_node_runnable(path: str) -> bool:
    """Whether ``node <path>`` is the right way to start this file.

    The recorded launch is ``node <entrypoint>``, not an exec of the file
    itself, because a shebang is not portable (Windows has none) and this module
    has to behave the same on every platform the gateway supports. That choice
    only stays correct while the entry point really is a node script: ``npx``
    execs through the shebang and so happily starts a ``sh`` or ``python`` bin,
    where ``node`` would fail on the first line.

    So the interpreter is checked once here, on the write side, and a bin that
    is not node's to run misses to ``npx`` instead of being recorded as a launch
    that cannot work.
    """
    if path.lower().endswith(_NODE_SUFFIXES):
        return True
    try:
        with open(path, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        # No shebang and no node suffix: npx would hand it to the kernel, which
        # would refuse it. Nothing to substitute.
        return False
    return b"node" in first


def _describe_tree(spec_root: str, resolution: str, package: str) -> Optional[tuple[str, str]]:
    """``(entrypoint relative to spec_root, version)`` for an installed tree.

    ``None`` when the tree has no readable manifest or names no runnable file --
    an install that produced nothing launchable must not be committed, or every
    later launch would resolve to a missing path and silently fall back forever.
    """
    name = package_name(package)
    manifest_path = os.path.join(spec_root, resolution, "node_modules", name, "package.json")
    manifest = _read_json(manifest_path)
    if manifest is None:
        return None
    rel_bin = _bin_relpath(manifest, name)
    if not rel_bin:
        return None
    entrypoint = os.path.normpath(os.path.join(resolution, "node_modules", name, rel_bin))
    absolute = os.path.realpath(os.path.join(spec_root, entrypoint))
    # ``bin`` is third-party metadata, so it can name a traversing path. A
    # record is a durable instruction to exec a file, so refuse any that leaves
    # the store rather than persist a pointer outside it.
    if not absolute.startswith(os.path.realpath(spec_root) + os.sep):
        logger.warning(
            "resolve-once: %s declares an entry point outside its tree; leaving it to npx",
            package,
        )
        return None
    if not os.path.isfile(absolute):
        return None
    if not _is_node_runnable(absolute):
        logger.info(
            "resolve-once: %s entry point is not a node script; leaving it to npx",
            package,
        )
        return None
    version = manifest.get("version")
    return entrypoint, version if isinstance(version, str) else ""


#: How long a superseded resolution tree is left on disk after a newer one goes
#: live. A launch reads the record and then execs the path it named, so the two
#: are not one atomic step: deleting the old tree the instant the new record
#: lands would let a refresh remove the very file a launch already decided to
#: run. The window only has to outlast that gap, and it is a disk-space
#: tradeoff, not a correctness one -- the next sweep collects whatever this one
#: spared, so a spec holds at most two trees in steady state.
_RESOLUTION_GRACE_SECS = 3600.0


def _sweep_old_resolutions(
    spec_root: str, keep: str, *, grace_secs: float = _RESOLUTION_GRACE_SECS, now: float = 0.0
) -> None:
    """Remove resolution trees other than ``keep`` that are past the grace window.

    Best effort: a failure here wastes disk, never correctness.

    Blocking (it walks and unlinks a populated ``node_modules``), so callers on
    the event loop must hand it to a thread.

    Deliberately NOT prompt. An earlier version deleted every other tree the
    moment the record was committed, reasoning that a launch which had already
    read the old path "keeps its open files" -- which is false. A launch holds a
    PATH, not a descriptor, and execs it a moment later, so a refresh landing in
    between removed the file out from under a session that was starting. That
    turned a refresh into a failed backend, which is exactly the failure this
    module promises it cannot cause.
    """
    current = now or time.time()
    try:
        entries = os.listdir(spec_root)
    except OSError:
        return
    for entry in entries:
        if not entry.startswith(_RESOLUTION_PREFIX) or entry == keep:
            continue
        path = os.path.join(spec_root, entry)
        try:
            age = current - os.stat(path).st_mtime
        except OSError:
            continue
        if age < grace_secs:
            # Still inside the window where a launch may already have decided to
            # exec something in here. Disk, not correctness -- leave it.
            continue
        shutil.rmtree(path, ignore_errors=True)


async def _reap_install_tree(proc: "asyncio.subprocess.Process") -> None:
    """Kill an install and every descendant it started, then wait for it.

    npm is the parent of the work, not the work: an install runs third-party
    ``preinstall``/``postinstall`` scripts as grandchildren. Signalling npm alone
    leaves those running -- with filesystem and network access, and with nobody
    waiting on them -- which is how a bounded timeout turns into an unbounded
    background process. The install is spawned with ``start_new_session=True`` so
    the whole group can be signalled here.

    Escalates straight to SIGKILL: this path is only reached when the install has
    already blown its deadline or the broker is going away, so there is nothing
    left to salvage by asking politely.

    The reap is bounded and drains the pipes. This path is reached with the
    ``_drain_capped`` reader already cancelled by ``wait_for`` (timeout) or by
    broker shutdown (cancellation), so the stdout pipe is undrained: npm blocked
    writing into a full pipe -- or a lifecycle-script grandchild still holding it
    open -- would make a bare ``await proc.wait()`` hang forever.
    """
    await kill_and_reap(proc)


async def _rmtree_off_loop(path: str) -> None:
    """Delete ``path`` recursively without blocking the event loop.

    Every cleanup here runs inside the gateway process, on its loop. Removing a
    populated ``node_modules`` is thousands of unlinks, so doing it inline would
    stall the dashboard, chat, and heartbeat processing for as long as the walk
    takes. Best effort in both senses: never raises, and never blocks.
    """
    with contextlib.suppress(Exception):
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


#: How much of an install's combined output is kept for the failure log. The pipe
#: is still DRAINED to EOF -- a child whose stdout fills blocks forever -- but only
#: this much is retained. ``communicate()`` retains ALL of it, and a package whose
#: lifecycle scripts are noisy (or hostile) could make the gateway buffer
#: unbounded third-party output until it is OOM-killed. 8 KiB is far more than the
#: 400 characters actually logged.
_MAX_INSTALL_OUTPUT_BYTES = 8192


async def _drain_capped(proc: "asyncio.subprocess.Process") -> bytes:
    """Read the child's output to EOF, keeping at most the first N bytes.

    Draining matters as much as capping: if nobody reads the pipe, a child that
    writes more than the pipe buffer blocks on write and never exits, so a cap
    implemented by simply not reading would convert noisy output into a hang.
    """
    kept = bytearray()
    stream = proc.stdout
    if stream is not None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            if len(kept) < _MAX_INSTALL_OUTPUT_BYTES:
                kept.extend(chunk[: _MAX_INSTALL_OUTPUT_BYTES - len(kept)])
    await proc.wait()
    return bytes(kept)


async def install(
    data_home: str,
    spec: NpmSpec,
    *,
    timeout_secs: float = DEFAULT_INSTALL_TIMEOUT_SECS,
    npm: Optional[str] = None,
) -> Optional[ResolvedRecord]:
    """Resolve ``spec`` into the store and return the committed record.

    Returns ``None`` on any failure -- npm missing, install non-zero, timeout, a
    tree with nothing runnable -- because the caller's fallback is today's
    behaviour and a failed prefetch must cost nothing but the attempt.

    The commit is the record write, not the install: the tree lands in its own
    ``r-<stamp>`` directory and only becomes live when :func:`write_record`
    atomically replaces ``record.json``. A concurrent reader therefore sees
    either the previous resolution or the new one, never a half-built tree.

    Install scripts are NOT disabled. ``npx`` runs them today, so suppressing
    them here would change which packages work rather than only where the work
    happens.
    """
    # ``which`` stats every PATH entry, so it goes off the loop like the rest of
    # this function's filesystem work.
    npm_cmd = npm or await asyncio.to_thread(shutil.which, "npm")
    if not npm_cmd:
        logger.debug("resolve-once: npm not on PATH; leaving %s to npx", spec.package)
        return None

    spec_root = spec_dir(data_home, spec)
    # Unique per WRITER, not just per millisecond: the timed pass and an operator
    # pressing "Update now" can reach this line at the same moment, and two
    # writers sharing one resolution directory would install into each other's
    # tree. The pid and a random tail cost nothing and remove the whole class.
    resolution = "{}{:x}-{}-{}".format(
        _RESOLUTION_PREFIX, int(time.time() * 1000), os.getpid(), secrets.token_hex(3)
    )
    target = os.path.join(spec_root, resolution)
    try:
        await asyncio.to_thread(functools.partial(os.makedirs, target, exist_ok=True))
    except OSError:
        logger.debug("resolve-once: cannot create %s", target, exc_info=True)
        return None
    # Hand npm the fully-resolved prefix. npm records each package's location
    # relative to the prefix it was given, so a prefix reached through a symlink
    # makes it compute a traversing path instead of a plain ``node_modules/x``.
    # One install could absorb that, but a clean tree costs nothing.
    real_target = os.path.realpath(target)

    argv = [
        npm_cmd,
        "install",
        "--prefix",
        real_target,
        "--no-audit",
        "--no-fund",
        # Our tree is a launch artifact, not a project: a lockfile here would be
        # a second copy of the resolution that nothing reads and that npm would
        # rewrite on every future install into the same prefix.
        "--no-package-lock",
        "--omit=dev",
        "--loglevel=error",
        # Everything after ``--`` is an operand, never an option. Belt to the
        # braces of refusing flag-shaped specs in the parser: if a spec shape
        # slips past that check, npm still treats it as the package name rather
        # than as, say, another ``--prefix`` pointing at the user's project.
        "--",
        spec.package,
    ]
    # The package spec comes from MCP configuration, so this is an
    # agent-influenced spawn and goes through the sandbox chokepoint like every
    # other one: OS-level isolation plus a credential-scrubbed environment. The
    # mode matches what the probe and the launch already use for the very same
    # npm launcher, so a registry reachable for those is reachable here -- and
    # one that is not was already unreachable before this module existed, where
    # the fallback lands anyway.
    wrapped_argv, spawn_env, sandbox_cleanup = sandboxed_spawn_argv(
        argv, mode="standard", strip_python_env=True
    )
    try:
        # Limits are applied AFTER exec by the spawn shim rather than by a
        # ``preexec_fn``: an install runs third-party lifecycle scripts, so it
        # gets the same fork-bomb / FD / memory / CPU cap every other routed
        # spawn does, without forking the multi-thread gateway to deliver it
        # (issue #935).
        #
        # ``start_new_session`` puts npm in its OWN process group so the whole
        # tree can be reaped. npm is a parent, not the worker: an install runs
        # third-party ``postinstall`` scripts as grandchildren, and killing only
        # npm on a timeout would leave those running with filesystem and network
        # access and no one waiting on them. Same reasoning as the pooled
        # handshake deadline (#4509), which killpg's for exactly this.
        proc = await create_subprocess_limited(
            *wrapped_argv,
            env=spawn_env,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError:
        logger.debug("resolve-once: could not start npm for %s", spec.package, exc_info=True)
        _drop_sandbox_launcher(sandbox_cleanup)
        await _rmtree_off_loop(target)
        return None

    try:
        output = await asyncio.wait_for(_drain_capped(proc), timeout=timeout_secs)
    except asyncio.TimeoutError:
        # A hung fetch is exactly what this module exists to keep off the launch
        # path; reap it here rather than let it own a slot forever.
        await _reap_install_tree(proc)
        logger.warning(
            "resolve-once: install of %s exceeded %.0fs; leaving it to npx",
            spec.package,
            timeout_secs,
        )
        _drop_sandbox_launcher(sandbox_cleanup)
        await _rmtree_off_loop(target)
        return None
    except asyncio.CancelledError:
        # Broker shutdown cancels the prefetch task. Without this the install and
        # every lifecycle descendant it started would outlive the gateway that
        # asked for them, still writing into a tree nothing will ever commit.
        await _reap_install_tree(proc)
        _drop_sandbox_launcher(sandbox_cleanup)
        await _rmtree_off_loop(target)
        raise
    finally:
        # The temp launcher/profile is only needed while the child runs; the
        # branches above have already dropped it, and this covers the rest.
        _drop_sandbox_launcher(sandbox_cleanup)

    if proc.returncode != 0:
        logger.warning(
            "resolve-once: install of %s failed rc=%s: %s",
            spec.package,
            proc.returncode,
            (output or b"").decode("utf-8", "replace").strip()[:400],
        )
        await _rmtree_off_loop(target)
        return None

    described = await asyncio.to_thread(_describe_tree, spec_root, resolution, spec.package)
    if described is None:
        logger.warning(
            "resolve-once: %s installed but exposes nothing runnable; leaving it to npx",
            spec.package,
        )
        await _rmtree_off_loop(target)
        return None

    entrypoint, version = described
    record = ResolvedRecord(
        package=spec.package,
        entrypoint=entrypoint,
        resolved_at=time.time(),
        pinned=spec.pinned,
    )
    try:
        await asyncio.to_thread(write_record, spec_root, record)
    except OSError:
        logger.debug("resolve-once: could not commit record for %s", spec.package, exc_info=True)
        await _rmtree_off_loop(target)
        return None

    # Superseded trees are collected off the loop and only past the grace window;
    # see _sweep_old_resolutions for why prompt deletion was wrong.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(_sweep_old_resolutions, spec_root, resolution)
    logger.info(
        "resolve-once: %s resolved to %s (pinned=%s); launches now skip npm",
        spec.package,
        version or "unknown version",
        spec.pinned,
    )
    return record


async def ensure_resolved(
    data_home: str,
    command: str,
    args: list[str],
    *,
    refresh_secs: float = DEFAULT_REFRESH_SECS,
    force: bool = False,
    timeout_secs: float = DEFAULT_INSTALL_TIMEOUT_SECS,
    npm: Optional[str] = None,
) -> Optional[ResolvedRecord]:
    """Make sure ``command``/``args`` has a fresh resolution, installing if not.

    ``force`` re-resolves even a pinned, in-date record: that is the explicit
    "update now" path, where the operator is asking to go to the registry rather
    than asking whether it is time to.
    """
    spec = parse_npm_launcher(command, args)
    if spec is None:
        return None
    existing = read_record(spec_dir(data_home, spec))
    if existing is not None and not force and not is_stale(existing, refresh_secs=refresh_secs):
        return existing
    return await install(data_home, spec, timeout_secs=timeout_secs, npm=npm)


#: Env keys the rewriter writes one per stubbed server, valued ``"cmd arg arg"``.
_TARGET_PREFIXES = ("KIROCREW_MCP_TARGET_", "MC_MCP_TARGET_")


def launch_specs(target_env: Mapping[str, str]) -> list[tuple[str, list[str]]]:
    """The distinct ``(command, args)`` launches described by ``target_env``.

    Deduplicated by the spec string, because the rewriter writes both a bare
    server-name key and an args-disambiguated one for the same launch and
    resolving it twice would install the same tree twice.
    """
    seen: set[str] = set()
    out: list[tuple[str, list[str]]] = []
    for key, value in sorted(target_env.items()):
        if not any(key.startswith(prefix) for prefix in _TARGET_PREFIXES):
            continue
        spec = (value or "").strip()
        if not spec or spec in seen:
            continue
        seen.add(spec)
        try:
            parts = shlex.split(spec)
        except ValueError:
            continue
        if parts:
            out.append((parts[0], parts[1:]))
    return out


async def prefetch(
    data_home: str,
    target_env: Mapping[str, str],
    *,
    refresh_secs: float = DEFAULT_REFRESH_SECS,
    force: bool = False,
    timeout_secs: float = DEFAULT_INSTALL_TIMEOUT_SECS,
) -> dict[str, str]:
    """Resolve every npm-launcher target that needs it. Returns package -> outcome.

    Runs the installs one at a time on purpose: they are registry-bound, and a
    burst of parallel installs would trade a stall on the launch path for a
    thundering herd off it. Non-npm targets are skipped silently -- there is
    nothing to pre-resolve about a plain binary.

    Never raises. This is a background improvement to launch latency, so a
    failure has to leave the caller exactly as well off as not calling it.
    """
    outcomes: dict[str, str] = {}
    for command, args in launch_specs(target_env):
        spec = parse_npm_launcher(command, args)
        if spec is None:
            continue
        try:
            record = await ensure_resolved(
                data_home,
                command,
                args,
                refresh_secs=refresh_secs,
                force=force,
                timeout_secs=timeout_secs,
            )
        except asyncio.CancelledError:
            # Shutdown is not an outcome to swallow.
            raise
        except Exception:  # pragma: no cover — defensive: prefetch is best effort
            logger.debug("resolve-once: prefetch of %s raised", spec.package, exc_info=True)
            outcomes[spec.package] = "error"
            continue
        outcomes[spec.package] = "ready" if record is not None else "unresolved"
    return outcomes
