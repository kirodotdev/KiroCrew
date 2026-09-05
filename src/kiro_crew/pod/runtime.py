"""Pod runtime mechanics: git worktree resolution, port derivation, systemd
wrappers, boot, token mint.

Everything that talks to the host (``git``, ``systemctl --user``, ``cksum``, the
pod's ``.local_secret``) lives here so :mod:`kiro_crew.pod.cli` stays a thin verb
layer. No state is held; each function reads what it needs from a
:class:`PodConfig` (and, for worktree resolution, from git / the per-pod env file).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:  # POSIX only; pods are refused on hosts without it (require_backend)
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from kiro_crew import pinned_fs
from kiro_crew import seed as seed_mod
from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.dashboard.urls import dashboard_socket_name
from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import loopback_urlopen, unix_socket_urlopen
from kiro_crew.platform_compat import (
    IS_LINUX,
    IS_MACOS,
    IS_POSIX,
    find_port_listeners,
    listening_pid_tool_available,
    loopback_owner_pids,
)
from kiro_crew.pod import launchd
from kiro_crew.pod import provision as prov
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import PodConfig
from kiro_crew.seed import SeedError
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Pod names become systemd instance names and path segments; keep them strict.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$")


class PodError(RuntimeError):
    """A pod operation could not be completed (bad name, no worktree, mint failed…)."""


class PodBackendAbsent(PodError):
    """The pod service manager is provably not running on this host.

    Raised only from branches where the backend is demonstrably absent (e.g.
    Linux with no session bus socket and no DBUS_SESSION_BUS_ADDRESS). Callers
    that need to distinguish 'backend absent, no pods possible' from 'backend
    present but erroring' can catch this subclass specifically.
    """


def validate_name(name: str) -> str:
    if not name or not _NAME_RE.match(name):
        raise PodError(f"invalid pod name {name!r}")
    return name


# --------------------------------------------------------------------------- #
# Per-pod env file (pinned CHECKOUT= / PORT= / SEED= / APPROVAL=). Values are
# single-quoted on write and unquoted on read; unknown keys are preserved on
# merge.
# --------------------------------------------------------------------------- #

# Approval modes a pod's gateway may boot with, mirroring the choices on
# ``kirocrew gateway --approval``. This tuple is the ENFORCEMENT point: the env
# file is hand-editable, so ``boot`` re-validates against it instead of trusting
# whatever ``pod up`` wrote. The top-level ``cli.py`` repeats the literal for its
# argparse ``choices`` because that parser deliberately imports no pod module at
# startup; argparse is the UX layer, this tuple is the invariant.
APPROVAL_MODES: tuple[str, ...] = ("reads", "yolo", "interactive")

# Truthy spellings accepted for the boolean ``CRONS=`` key. ``pod up --crons``
# writes ``"1"``; the others are accepted because the env file is hand-editable
# and these are the obvious alternatives. Anything else is treated as OFF, which
# is the pre-existing ``--no-crons`` behavior and the safer of the two.
CRONS_TRUE: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY='value'`` lines. Split out so a caller that must open the file
    itself -- see :func:`_peer_claimed_port`, which needs no-follow semantics -- can
    reuse this exact grammar instead of carrying a second copy that would drift."""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        raw = val.strip()
        # Strip a single matched surrounding quote pair only (the form
        # write_env_file emits), so a value that legitimately contains a quote is
        # not mangled.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        out[key.strip()] = raw
    return out


def read_env_file(cfg: PodConfig, name: str) -> dict[str, str]:
    """Parsed ``KEY='value'`` pairs for pod *name*, ``{}`` on any ``OSError``.

    Fail-OPEN by design, which bounds who may use it: a missing pod, an
    unreadable pods dir and a comments-only file all yield the same empty
    mapping, so a caller that must tell "absent" from "exists but cannot be
    positively read" MUST NOT read the pin through here -- see
    ``dev_fleet._read_pin_strict``, which propagates the failure instead.

    Takes the pod NAME, so the path read is one an operator named. A caller that
    instead reads whatever files happen to be in the pods directory is choosing its
    paths from directory contents rather than from an operator, which is a different
    trust posture -- :func:`_peer_claimed_port` is that caller and does not come
    through here.
    """
    try:
        text = cfg.env_file(name).read_text()
    except OSError:
        return {}
    return _parse_env_text(text)


def write_env_file(cfg: PodConfig, name: str, updates: dict[str, str]) -> None:
    """Merge *updates* into the pod's env file, preserving existing keys.

    Values MUST be single-line: the ``KEY='value'`` format does not escape
    newlines, so a multi-line value would not round-trip. ``--seed`` is
    user-supplied, so reject a newline-bearing value loudly (fail-closed) rather
    than silently writing an un-parseable file.

    **Written atomically**, because readers are deliberately lock-free: ``boot``
    reads this file without taking the mutex (so ``pod up`` can hold it across
    the health wait without deadlocking against the process it waits for). An
    in-place truncating rewrite therefore has a window where a reader — a
    ``Restart=`` re-exec, say — sees a partial or empty file. A dropped
    ``APPROVAL`` is not a benign default: ``boot`` leaves ``approval_mode``
    unset, which falls through to ``cfg.agent.approval_mode`` and lands on
    auto-approve, the LEAST restrictive outcome. Temp-file + rename means an
    unlocked reader sees either the old file or the new one, never a torn one.

    The merge additionally re-acquires :func:`pod_name_mutex`, which every
    mutating pod path already holds at its call site. That is defense in depth
    for direct callers rather than a fix for a live race, and it mirrors what
    ``start_pod`` / ``stop_pod`` already do; reentrancy is what makes
    re-acquiring it inside an outer transaction safe.
    """
    for key, val in updates.items():
        if "\n" in val or "\r" in val:
            raise PodError(f"pod env value for {key!r} must be single-line")
    with pod_name_mutex(cfg, name):
        data = read_env_file(cfg, name)
        data.update(updates)
        for key, val in data.items():
            if "\n" in val or "\r" in val:
                raise PodError(f"pod env value for {key!r} must be single-line")
        cfg.pods_dir.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{k}='{v}'\n" for k, v in data.items())
        atomic_write(cfg.env_file(name), body, newline="")


def pin_checkout(cfg: PodConfig, name: str, checkout: Path) -> None:
    """Pin the resolved absolute checkout so the systemd-booted gateway (and any
    ``Restart=`` re-exec) resolves it without shelling git from a clean env."""
    write_env_file(cfg, name, {"CHECKOUT": str(checkout)})


# --------------------------------------------------------------------------- #
# Git-native worktree resolution. A friendly name maps to an absolute checkout
# via the pinned CHECKOUT=, else `git worktree list`, else an optional root.
# --------------------------------------------------------------------------- #
def _git_worktrees(ref: Path) -> dict[str, Path]:
    """Map ``{basename | branch | abspath -> checkout}`` for every linked worktree
    of the repo *ref* belongs to. Empty on any git error (not a repo / git absent).
    ``git worktree list`` from ANY linked worktree lists them all.
    """
    try:
        cp = subprocess.run(
            ["git", "-C", str(ref), "worktree", "list", "--porcelain"],
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if cp.returncode != 0:
        return {}
    out: dict[str, Path] = {}
    cur: Path | None = None
    for ln in cp.stdout.splitlines():
        if ln.startswith("worktree "):
            cur = Path(ln[len("worktree ") :].strip())
            out.setdefault(cur.name, cur)
            out.setdefault(str(cur), cur)
        elif ln.startswith("branch ") and cur is not None:
            br = ln[len("branch ") :].strip()
            if br.startswith("refs/heads/"):
                br = br[len("refs/heads/") :]
            out.setdefault(br, cur)
    return out


def resolve_checkout(
    cfg: PodConfig, name: str, *, cwd: Path | None = None, use_pin: bool = True
) -> Path:
    """Resolve a friendly worktree *name* to an absolute checkout path.

    Order: pinned ``CHECKOUT=`` (if the dir still exists) → ``git worktree list``
    (from ``KIROCREW_POD_REPO`` else *cwd*), matching a worktree's basename, then
    its branch (``name`` or ``feat/<name>``), then an exact path → optional
    ``KIROCREW_POD_WORKTREES_ROOT/name`` fallback → :class:`PodError`.
    """
    # 1. Pinned checkout (authoritative; the path boot() relies on).
    if use_pin:
        pinned = read_env_file(cfg, name).get("CHECKOUT")
        if pinned:
            p = Path(pinned).expanduser()
            if p.is_dir():
                return p

    # 2. Ask git. `ref` is the repo hint or the invoking working directory.
    ref = cfg.repo_hint or (cwd or Path.cwd())
    wts = _git_worktrees(ref)
    hit = wts.get(name) or wts.get(f"feat/{name}")
    if hit is not None:
        return hit

    # 3. Optional fixed-root fallback (hermetic test/CI planes; no git needed).
    if cfg.worktrees_root is not None:
        cand = cfg.worktrees_root / name
        if cand.is_dir():
            return cand

    raise PodError(
        f"no git worktree {name!r}. Create one for your branch:\n"
        f"  git worktree add ../{name} -b feat/{name} main\n"
        f"  (run `kirocrew pod up {name}` from inside a kirocrew checkout, "
        f"or set KIROCREW_POD_REPO to point at one)"
    )


# --------------------------------------------------------------------------- #
# Port derivation. POSIX ``cksum`` is a specific CRC that is NOT zlib.crc32.
# Implementing that standard algorithm here keeps existing pod ports stable while
# making a fresh Windows install independent of an external POSIX executable.
# --------------------------------------------------------------------------- #
#: Digit cap on an env-file port value. ``int()`` refuses a conversion over 4300
#: digits, and nothing anyone meant as a port is longer than this; ten rather than
#: the five a port needs so an out-of-range TYPO still parses and can be refused by
#: name (see :func:`allocate_port`) instead of silently reading as "no value".
_MAX_PORT_DIGITS = 10


def _port_from_env(value: str | None) -> int | None:
    """Parse an env-file value as a port number, or ``None`` when it is not one.

    THE one place an env-file string becomes a port. Three call sites used to guard
    this themselves and each got it wrong differently -- one on length, one on a
    sibling it forgot to audit, one on character class -- so the guard is now a
    single function they all share and the class is closed at the parse rather than
    per symptom.

    ``isdecimal``, NOT ``isdigit``, because ``isdigit`` is not the predicate that
    matches ``int()``: U+00B2 SUPERSCRIPT TWO satisfies ``isdigit`` while ``int()``
    raises ``ValueError`` on it. ``isdecimal`` admits exactly what ``int()`` accepts,
    so such a value reads as "not a port" instead of tracebacking out of ``pod url``.
    Note this deliberately still accepts a non-ASCII DECIMAL run such as U+0662
    ARABIC-INDIC DIGIT TWO, which ``int()`` parses as 2 -- rejecting on ``isascii``
    would refuse a value that is genuinely a number. Codepoints are NAMED rather
    than written literally so this file stays ASCII and the reader is not relying on
    their font to tell the cases apart.

    RANGE IS THE CALLER'S POLICY, not this function's, because the two callers
    legitimately differ: :func:`allocate_port` needs an out-of-range pin like
    ``70000`` to arrive intact so it can refuse it by name, while
    :func:`_peer_claimed_port` wants only real ports, since a value that cannot be a
    port is not a claim on one. This function's job is to be crash-proof, and the
    length cap is what makes it so.
    """
    if not value or not value.isdecimal() or len(value) > _MAX_PORT_DIGITS:
        return None
    try:
        return int(value)
    except ValueError:  # pragma: no cover - unreachable behind isdecimal + the cap
        return None


def _pinned_port(cfg: PodConfig, name: str) -> int | None:
    """A ``PORT=`` pinned in the pod's env file wins over derivation.

    Parsing (and every crash guard) lives in :func:`_port_from_env`. An unusable
    value reads as "no pin" and falls back to derivation, which beats a traceback out
    of the read-only callers -- ``pod url``, ``pod ls``, Dev Fleet -- that reach this
    through :func:`derive_port`.
    """
    return _port_from_env(read_env_file(cfg, name).get("PORT"))


def _posix_cksum(data: bytes) -> int:
    """Return the CRC printed by POSIX ``cksum`` for *data*.

    POSIX folds the byte length into the CRC, least-significant byte first, then
    complements the result.  Keep the bitwise form small and auditable; pod names
    are at most 64 bytes, so a lookup table would add complexity without useful
    performance.
    """
    crc = 0

    def _fold_byte(current: int, byte: int) -> int:
        current ^= byte << 24
        for _ in range(8):
            current = (
                ((current << 1) ^ 0x04C11DB7) if current & 0x80000000 else current << 1
            ) & 0xFFFFFFFF
        return current

    for byte in data:
        crc = _fold_byte(crc, byte)
    length = len(data)
    while length:
        crc = _fold_byte(crc, length & 0xFF)
        length >>= 8
    return (~crc) & 0xFFFFFFFF


def derive_port(cfg: PodConfig, name: str) -> int:
    """Resolve pod *name*'s port: pinned ``PORT=`` else ``base + (cksum % 199) + 1``.

    DERIVATION, not allocation: this answers "which port does this name map to",
    and every reader (``pod url``, ``pod ls``, Dev Fleet, ``pod exec``) calls it to
    agree on one answer without coordinating. It deliberately does NOT check
    whether that port is free -- a reader must not renegotiate a running pod's
    port, and a bind probe here would make the answer depend on when it was asked.

    Whether the port can actually be had is an allocation question, asked once per
    ``pod up`` by :func:`allocate_port`, which records its answer as a ``PORT=``
    pin so every later derivation returns it.
    """
    pinned = _pinned_port(cfg, name)
    if pinned is not None:
        return pinned
    cks = _posix_cksum(name.encode("utf-8"))
    return cfg.base_port + (cks % 199) + 1


def _port_is_free(port: int) -> bool:
    """Whether *port* can be bound on loopback right now.

    PRIVATE, and it must stay private. ``instances/run_marker`` states the rule:
    no "is something listening" helper may be offered, because a caller will
    mistake reachability for identity -- and ``pod``'s own health probe was held
    to that rule for exactly this reason. This function is not exempt by being
    inverted: "nobody is listening" is equally useless as an identity signal, and
    a free port says nothing about who WOULD answer on a busy one.

    So the only caller is :func:`allocate_port`, whose question genuinely is
    binding and nothing else: it is choosing a port to hand a process that has not
    started yet.

    ``SO_REUSEADDR`` is set to MIRROR THE ACTUAL BINDER. A pod's gateway serves
    through ``aiohttp``'s ``TCPSite``, which leaves ``reuse_address`` at its
    asyncio default -- set on POSIX -- so the gateway can bind a port whose only
    occupant is a ``TIME_WAIT`` remnant. A probe without the option is therefore
    STRICTER than the process it is probing for, and the difference is not
    academic: ``pod down`` followed by ``pod up`` leaves the previous gateway's
    accepted connections in ``TIME_WAIT`` on exactly that port, so every quick
    restart would read as a collision and relocate the pod off its derived port
    for nothing. ``instances/port_allocator.py`` documents the same reasoning for
    the SSH forward. ``SO_REUSEADDR`` exempts ``TIME_WAIT`` only, never a live
    ``LISTEN``, so a real collision is still caught.

    That last sentence is POSIX, and deliberately not hedged: on Windows the option
    means something closer to ``SO_REUSEPORT`` and would let this bind succeed
    against a LIVE listener, inverting the answer. It is unguarded because it is
    unreachable -- ``require_backend`` refuses pods on any host without
    ``systemd --user`` or ``launchd``, so nothing calls this there. Anyone reusing
    this probe outside the pod plane has to revisit that.

    Raises :class:`PodError` when the probe cannot be RUN at all -- socket creation
    or option-setting failing, e.g. on file-descriptor exhaustion. That is not the
    same as a port being busy and must not be coerced into ``False``: answering
    "not free" for a probe that never happened would silently relocate a pod on no
    evidence. ``instances/port_allocator.py`` states the same rule for its own probe
    -- "neither answer is true, so it propagates to the caller instead of being
    coerced into one". ``allocate_port`` lets it through and ``pod up`` turns it into
    a refusal, so the operator sees why rather than a traceback.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        raise PodError(
            f"cannot check whether port {port} is free: creating a probe socket "
            f"failed ({exc}). This is not a busy port -- the check could not run, so "
            f"no port can be chosen safely"
        ) from exc
    with sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError as exc:
            raise PodError(
                f"cannot check whether port {port} is free: configuring the probe "
                f"socket failed ({exc}). This is not a busy port -- the check could "
                f"not run, so no port can be chosen safely"
            ) from exc
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            # THIS one is a real answer: the address cannot be taken, so it is busy.
            return False
        except OverflowError:
            # bind() rejects a port outside 0-65535 with OverflowError, which is
            # NOT an OSError and would escape as a traceback. Callers are expected
            # to have validated the range (see :func:`allocate_port`); this is
            # defence in depth so a future caller cannot reintroduce the crash, and
            # "not bindable" is the honest answer for an impossible port.
            return False
    return True


#: Env key recording the port :func:`allocate_port` chose ITSELF, so a later call
#: can tell its own fallback from an operator's deliberate ``PORT=``. The VALUE is
#: stored (not a bare flag) so the marker self-invalidates: an operator who
#: hand-edits ``PORT=`` to something else no longer matches it and gets operator
#: treatment, with no way for a stale marker to reclassify their choice as ours.
AUTO_PORT_KEY = "PORT_AUTO"


def operator_pinned(cfg: PodConfig, name: str) -> bool:
    """Whether *name*'s ``PORT=`` was set by a PERSON rather than recorded by us.

    One definition, two consumers, because the two must never disagree:
    :func:`allocate_port` uses it to decide whether a busy pin may be relocated,
    and ``pod up`` uses it to decide whether to stamp :data:`AUTO_PORT_KEY` on the
    port it records. Answered from the same bytes in both cases -- if the caller
    re-derived this rule for itself, a deliberate pin could be relocated on one
    path while being honoured on the other.

    A pin counts as ours only when the marker matches the pin's VALUE, so an
    operator who hand-edits ``PORT=`` to something else stops matching and is
    treated as deliberate again.

    Compared as STRINGS, never through ``int()``. Both keys are written from the same
    ``str(port)``, so byte equality is exactly the intended test. Whether each side is
    port-SHAPED at all is asked of :func:`_port_from_env`, so this agrees with every
    other reader about what counts as a value -- a value that is not decimal is not a
    pin here for
    the same reason it is not one there.
    """
    env = read_env_file(cfg, name)
    raw = env.get("PORT", "")
    if _port_from_env(raw) is None:
        return False
    auto = env.get(AUTO_PORT_KEY, "")
    return not (_port_from_env(auto) is not None and auto == raw)


#: Cap on a peer env file read during the claim scan. These files hold a handful of
#: short ``KEY='value'`` lines; anything larger is not one, and the scan must not be
#: a way to pull an arbitrary amount of some other file into memory.
_MAX_PEER_ENV_BYTES = 64 * 1024


def _read_peer_env(path: Path) -> dict[str, str] | None:
    """Safely parse a PEER pod's env file, or ``None`` if it cannot be read.

    Returns the MAPPING rather than a port so the caller can resolve the port the
    same way :func:`derive_port` does; ``None`` means "could not positively read
    this", which is different from "read it and it names no port".

    Separate from :func:`read_env_file` because the trust posture differs. That
    function takes a pod NAME, so the operator chose the path. This one is handed a
    path the CLAIM SCAN found by globbing the pods directory, so the set of files
    read is decided by directory contents rather than by a person -- and this runs in
    the trusted CLI, outside the hooks gate that governs an agent's own reads. A
    symlink planted there would therefore make a privileged process read its target.

    So the open carries ``O_NOFOLLOW`` (a symlink raises ``ELOOP`` and is skipped)
    rather than an ``is_symlink()`` pre-check, which would leave a window between the
    check and the open -- AND ``O_NONBLOCK``, because ``O_NOFOLLOW`` does nothing
    about a FIFO: a named pipe is not a symlink, and a plain ``O_RDONLY`` open of one
    BLOCKS until a writer appears, which would hang every ``pod up`` on the plane
    indefinitely rather than merely reading the wrong bytes. Measured: the blocking
    form never returns, the non-blocking form returns at once.
    ``O_NONBLOCK`` has no effect on reading a regular file, so it costs the normal
    path nothing.

    Nonblocking alone only stops the hang, so the descriptor is then ``fstat``-ed and
    anything that is not a REGULAR file is refused -- a FIFO, a device, a directory.
    That is the check that makes "this is a pod's env file" true rather than assumed.

    The read is bounded, and every failure -- missing, unreadable, symlink, FIFO,
    undecodable -- answers ``None``, because "this peer has no claim I can read" is
    the safe answer and matches :func:`read_env_file`'s fail-open contract.

    The value goes through :func:`_port_from_env`, so every crash guard lives in one
    place. The RANGE check is this function's own policy: a value that cannot be a
    port is not a claim on one, whereas :func:`allocate_port` needs an out-of-range
    pin to arrive intact so it can name it.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace", closefd=False) as fh:
            text = fh.read(_MAX_PEER_ENV_BYTES)
    except OSError:
        return None
    finally:
        # Closed HERE in every path, including the non-regular refusal above.
        # ``closefd=False`` keeps ownership with this function rather than handing it
        # to the wrapper, so no branch can leak a descriptor -- which would be a
        # particularly poor failure in a scan whose whole job is running before a
        # process needs file descriptors of its own.
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - already closed or invalid
            pass
    return _parse_env_text(text)


def _peer_effective_port(cfg: PodConfig, name: str, path: Path) -> int | None:
    """The port peer *name* would actually resolve, or ``None`` if unreadable.

    Mirrors :func:`derive_port` -- pinned value if there is a usable one, else the
    cksum derivation -- but from the ONE safe read above rather than by calling that
    function, which uses plain ``read_text`` and would reintroduce both the symlink
    follow and the FIFO hang the safe reader exists to prevent.

    The derivation fallback is not a nicety. A pod that came up BEFORE claims were
    recorded has no ``PORT=`` at all, yet it is running on its derived port right now.
    Reading "no PORT key" as "claims nothing" would leave every pre-upgrade pod
    unprotected: during a restart gap it is listening on nothing, so the probe also
    reports its port free, and a colliding name would take it and leave the legacy pod
    crash-looping on EADDRINUSE. Resolving what that peer WOULD use closes the upgrade
    window without needing every pod restarted first.

    A value that cannot be bound (out of range) is returned as-is rather than
    filtered, because the point is to agree with :func:`derive_port` about what that
    peer resolves. Such a port simply never matches a candidate in the band walk.
    """
    data = _read_peer_env(path)
    if data is None:
        return None
    pinned = _port_from_env(data.get("PORT"))
    if pinned is not None:
        return pinned
    return cfg.base_port + (_posix_cksum(name.encode("utf-8")) % 199) + 1


def _ports_claimed_by_other_pods(cfg: PodConfig, name: str) -> dict[int, str]:
    """``{port: pod name}`` for every ``PORT=`` recorded by a pod other than *name*.

    A bind probe answers "is anyone LISTENING there right now", which is not the
    same as "is that port somebody's". Two cases turn on the difference:

    * A pod that is stopped is listening on nothing, so its operator-pinned port
      probes free. A displaced pod landing there boots fine -- and then the pinned
      pod's next ``up`` hits :func:`allocate_port`'s "a pin you set is never moved"
      refusal for a squat THIS code created, with a message blaming the operator's
      own environment.
    * A pod whose gateway has been started but has not bound yet -- units are
      ``Type=simple``, so ``start_pod`` returns before the bind -- also probes free.
      Its port is written to disk BEFORE it is started, though, so consulting the
      recorded claims sees a concurrent claim that the probe cannot.

    One directory scan, inside the plane lock, so the answer cannot change under
    the walk. Each entry is resolved through :func:`_peer_effective_port`, which reads
    it safely and works out what that peer would use -- including a PRE-UPGRADE pod
    with no ``PORT=`` at all, whose port is its derivation and which is running there
    now. An entry that cannot be positively read claims nothing, so one hostile or
    malformed file does not stop the plane from allocating.
    """
    claimed: dict[int, str] = {}
    try:
        entries = sorted(cfg.pods_dir.glob("*.env"))
    except OSError:
        return claimed
    for entry in entries:
        other = entry.name[: -len(".env")]
        if other == name:
            continue
        # Validate the peer's NAME before doing anything with it. A pod name is
        # `_NAME_RE`, the same rule `validate_name` enforces on operator input, so a
        # stem that fails it is not a pod and has no port to claim -- there is nothing
        # to protect and nothing to read.
        #
        # It is also load-bearing rather than tidy. Filenames are BYTES on POSIX, so a
        # name containing invalid UTF-8 arrives as a surrogate (measured: `bad-\xff.env`
        # is handed over as `'bad-\udcff.env'`), and the derivation fallback below has
        # to encode the name to hash it -- which raises `UnicodeEncodeError` on a
        # surrogate and would traceback out of every `pod up` on the plane. Matching
        # the regex answers False for such a stem without raising, so the check both
        # closes that and states the real precondition: peers are pods.
        if not _NAME_RE.match(other):
            continue
        port = _peer_effective_port(cfg, other, entry)
        if port is not None:
            claimed.setdefault(port, other)
    return claimed


def _walk_band_for_free(cfg: PodConfig, name: str, occupied: int, claimed: dict[int, str]) -> int:
    """First free in-band port at or after *occupied*, excluding the live plane.

    Walks from just above *occupied* and wraps, so the result is deterministic (one
    collision resolves the same way on every host and every retry) and stays near
    the derived slot rather than clustering every displaced pod at the bottom of
    the band.

    Skips three things: the live plane, any port another pod has RECORDED (passed in
    by the caller, which checks the derived port against the same set), and finally
    anything actually listening.

    Every allocation records its claim, so a concurrent allocation sees it even
    though units are ``Type=simple`` and the gateway has not bound yet. That is what
    lets this close the cross-name race without holding a lock across the boot.
    """
    span = 199
    lo = cfg.base_port + 1
    start = occupied - lo
    for step in range(1, span):
        candidate = lo + (start + step) % span
        if candidate == cfg.live_port:
            # The live plane is never a pod's port. `_up` refuses the DERIVED port
            # for this reason already; the fallback must not reintroduce it.
            continue
        if candidate in claimed:
            continue
        if _port_is_free(candidate):
            return candidate
    raise PodError(
        f"no free port for pod {name!r}: :{occupied} is busy and every port in "
        f":{lo}-:{lo + span - 1} is either occupied or claimed by another pod. Free "
        f"a port, or pin an explicit one with PORT= in {cfg.env_file(name)}"
    )


def allocate_port(cfg: PodConfig, name: str) -> tuple[int, int | None]:
    """Choose the port to boot pod *name* on. Returns ``(port, displaced_from)``.

    ``displaced_from`` is the port this call moved OFF when it was busy, else
    ``None`` -- so a caller can say so out loud rather than the move being silent,
    and knows when to record the new choice.

    Why this exists: the derivation maps every name into 199 slots
    (``base + (cksum(name) % 199) + 1``), so two worktree names colliding mod 199
    is an ordinary event, not a pathological one, and the derived port can equally
    be held by something that is not a pod at all. Without this, the loser's
    gateway exits "address already in use" and its unit crash-loops -- while
    ``pod url`` keeps printing the shared port, so the operator is pointed at a
    pod that is not the one they just built.

    The answer is recorded by the caller as a ``PORT=`` pin, which
    :func:`derive_port` already prefers over derivation. That is what keeps every
    later reader agreeing without being changed: the pin mechanism predates this
    function and is the reason the fallback needs no new plumbing.

    **An automatic pin is not an operator pin.** A fallback this function chose is
    recorded alongside :data:`AUTO_PORT_KEY`, and a later call will relocate it
    like any other busy port. Without that distinction one transient occupant of
    the derived port -- a ``TIME_WAIT`` remnant, another process for a minute --
    would pin the pod off its derived slot permanently, and every later collision
    on the fallback would hit the "never moved automatically" refusal for a
    decision the operator never made. An operator's OWN ``PORT=`` is still never
    relocated: that would defeat the reason it was pinned.

    THIS FUNCTION ONLY CHOOSES. It writes nothing, so the caller can record the
    choice in the same env write it already performs, inside the lock it already
    holds. Note the probe cannot RESERVE: the port is released before the pod's
    gateway binds it, so see :func:`pod_plane_mutex` for what closes that window
    and what remains open.

    Raises :class:`PodError` when an operator's pinned port is busy, or when the
    whole band is occupied -- a pod that cannot get a port must not appear to
    start.
    """
    claimed = _ports_claimed_by_other_pods(cfg, name)
    pinned = _pinned_port(cfg, name)
    if pinned is not None:
        # Validate the RANGE before probing. `_pinned_port` only checks that the
        # value is digits, so `PORT='70000'` reaches here intact, and bind() answers
        # an impossible port with OverflowError rather than a refusal. Port 0 is
        # equally unusable despite binding successfully: it means "any free port",
        # so the pod would come up somewhere nobody can predict -- the opposite of
        # what a pin is for. Both are operator typos, so they earn the loud path
        # rather than a silent relocation.
        if not 1 <= pinned <= 65535:
            raise PodError(
                f"pod {name!r} pins PORT={pinned} in {cfg.env_file(name)}, which is "
                f"not a usable port. Pin a port between 1 and 65535"
            )
        if pinned not in claimed and _port_is_free(pinned):
            return pinned, None
        if operator_pinned(cfg, name):
            raise PodError(
                f"pod {name!r} pins PORT={pinned} in {cfg.env_file(name)}, but that "
                f"port is already in use. A pin you set is never moved "
                f"automatically -- free it, or change the pin"
            )
        # Our own earlier fallback. Relocating it is the whole point of marking it.
        return _walk_band_for_free(cfg, name, pinned, claimed), pinned

    derived = derive_port(cfg, name)
    # The claim check matters MOST here, not only in the walk. A pod that takes its
    # derived port records that claim, and units are ``Type=simple`` so its gateway
    # has not bound by the time a concurrent allocation probes -- meaning the probe
    # alone would hand the same port to a colliding name. Consulting the recorded
    # claims is what makes the concurrent case behave like the sequential one.
    if derived not in claimed and _port_is_free(derived):
        return derived, None
    return _walk_band_for_free(cfg, name, derived, claimed), derived


def pod_unit(cfg: PodConfig, name: str) -> str:
    """systemd unit name for pod *name*."""
    return f"{cfg.unit_prefix}@{name}.service"


def pod_home(cfg: PodConfig, name: str) -> Path:
    return cfg.home_dir(name)


def pod_socket_path(cfg: PodConfig, name: str, port: int) -> Path:
    """Path of pod *name*'s private dashboard unix socket.

    The pod's gateway binds ``dashboard_socket_path(port)`` resolved against ITS
    ``KIROCREW_HOME``, which is :func:`pod_home` -- so the socket lands in the
    pod's isolated home, not in the host's data home. Calling
    ``dashboard_socket_path`` from here would resolve THIS process's home and
    name a socket the pod never binds, which is why only the file name comes from
    the shared definition and the directory comes from the pod.

    That directory is the security property: it is created owner-only
    (``make_owner_only_dir``) and the socket is ``chmod 0600``, so no other local
    user can answer here -- unlike the pod's TCP port, which any local user can
    bind once the pod releases it.
    """
    return pod_home(cfg, name) / dashboard_socket_name(port)


# --------------------------------------------------------------------------- #
# systemd --user helpers.
# --------------------------------------------------------------------------- #
def _session_runtime_dir() -> str:
    """The per-user runtime directory ``systemctl --user`` resolves against.

    ``XDG_RUNTIME_DIR`` when the caller has one, else systemd's conventional
    ``/run/user/<uid>``. ``os.getuid`` is absent on Windows; pods are Linux-only
    (:func:`require_systemd`) so that branch is unreachable at runtime, but the
    ``getattr`` keeps this module importable there.
    """
    explicit = os.environ.get("XDG_RUNTIME_DIR")
    if explicit:
        return explicit
    uid = getattr(os, "getuid", lambda: -1)()
    return f"/run/user/{uid}"


def session_bus_socket() -> str:
    """Path of the D-Bus socket that fronts this user's systemd instance."""
    return os.path.join(_session_runtime_dir(), "bus")


def has_session_bus() -> bool:
    """Whether ``systemctl --user`` can reach a per-user systemd instance.

    An explicitly-set ``DBUS_SESSION_BUS_ADDRESS`` is taken at face value (the
    caller has deliberately pointed somewhere, possibly not a filesystem path);
    otherwise the conventional socket must actually exist.
    """
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    return os.path.exists(session_bus_socket())


def _systemctl_env() -> dict[str, str]:
    """Environment for ``systemctl --user``, with the session-bus pointers
    backfilled when absent.

    ``systemctl --user`` finds the per-user systemd instance through
    ``XDG_RUNTIME_DIR`` + ``DBUS_SESSION_BUS_ADDRESS``. A process launched from
    a systemd SYSTEM unit — which is how ``kirocrew service install`` runs the
    gateway — inherits no login-session environment and therefore neither
    variable, so every pod verb died with "Failed to connect to bus: No medium
    found" even though the bus socket was present and the pod unit installed.

    Only ever ADDS: an explicitly-set value always wins, so a caller that has
    deliberately pointed at another bus is left untouched. The socket must
    exist before we name it — if ``systemd --user`` genuinely is not running we
    want systemctl's own diagnostic, not a failure against a path we invented.
    """
    env = {**os.environ}
    runtime_dir = _session_runtime_dir()
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        sock = os.path.join(runtime_dir, "bus")
        if os.path.exists(sock):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={sock}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    return env


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=_systemctl_env()
    )


def require_systemd() -> None:
    """Raise :class:`PodError` unless this host can run ``systemctl --user``.

    Pods are Linux ``systemd --user`` only (see ``pod/README.md`` → Platform).
    Without this gate the first ``subprocess.run(["systemctl", ...])`` raises a
    bare ``FileNotFoundError`` and every verb dumps a traceback on macOS /
    Windows instead of the documented "report the failure" one-liner. Checked
    here — the single chokepoint every systemd call funnels through — so no verb
    can forget it.

    The third gate is the session bus. :func:`_systemctl_env` backfills the bus
    pointers when the socket exists, but when ``systemd --user`` is genuinely
    not running (no login session and ``Linger=no``) there is nothing to point
    at and systemctl emits a raw "Failed to connect to bus: No medium found"
    that names neither the cause nor the fix. Translate it here, keyed on the
    socket's absence rather than on matching systemctl's stderr.
    """
    if not IS_LINUX:
        raise PodError(
            f"pods require Linux `systemctl --user`; this host is {sys.platform}. "
            "Use `./dev-backend.sh` to preview a worktree on this platform."
        )
    if shutil.which("systemctl") is None:
        raise PodError("pods require `systemctl --user`, but no `systemctl` was found on PATH.")
    if not has_session_bus():
        uid = getattr(os, "getuid", lambda: -1)()
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(uid)
        raise PodBackendAbsent(
            f"no `systemd --user` session bus for uid {uid} "
            f"(looked for {session_bus_socket()}).\n"
            "Pods are systemd --user units, so one is required.\n"
            f"Fix: loginctl enable-linger {user}   "
            "# keeps the per-user instance alive independently of login sessions"
        )


def require_backend() -> None:
    """Gate on whatever service manager THIS host uses for pods.

    Dispatches instead of replacing :func:`require_systemd`: that function is
    still the systemd gate with its own contract and messages, so Linux and
    Windows behaviour is provably unchanged by the macOS work — on any non-darwin
    host this is exactly ``require_systemd()``.
    """
    if IS_MACOS:
        try:
            launchd.require_backend()
        except launchd.LaunchdError as exc:  # translate to the pod error type
            raise PodError(str(exc)) from exc
        return
    require_systemd()


def systemctl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    require_systemd()
    return _run(["systemctl", "--user", *args], timeout=timeout)


def is_active(cfg: PodConfig, name: str) -> bool:
    if IS_MACOS:
        try:
            return launchd.is_active(cfg, name)
        except launchd.LaunchdError as exc:
            # Fail closed as the documented pod error, not a traceback: the
            # probe REFUSES to call a pod absent when launchctl cannot answer.
            raise PodError(str(exc)) from exc
    cp = systemctl("is-active", "--quiet", pod_unit(cfg, name))
    return cp.returncode == 0


def main_pid(cfg: PodConfig, name: str) -> int | None:
    """PID of the pod's OWN gateway process, or ``None`` when it is not running.

    This is the pod's identity, and it is exact rather than approximate because
    of how a pod boots: the unit is ``Type=simple`` running ``kirocrew pod _run
    %i``, and :func:`_run` finishes with ``os.execve`` of the worktree's
    ``kirocrew gateway``. The gateway therefore REPLACES the unit's main process
    instead of being spawned beneath it, so ``MainPID`` names the very process
    that binds the pod's port — no descendant walk, no cgroup scan.

    Raises :class:`PodError` when the service manager could not be asked at all.
    That is deliberately a different answer from ``None``: "asked, and this pod
    has no process" is a fact a caller can act on (see :func:`port_owner`, where
    it is what lets a listener be attributed to somebody else), while "could not
    ask" must leave the question open. ``systemctl show`` prints ``MainPID=0``
    for a dead or unknown unit and still exits 0, so an output with no
    ``MainPID`` line at all is the honest signal that the query itself failed.
    """
    if IS_MACOS:
        return launchd.main_pid(cfg, name)
    cp = systemctl("show", pod_unit(cfg, name), "-p", "MainPID")
    for ln in cp.stdout.splitlines():
        if ln.startswith("MainPID="):
            raw = ln.split("=", 1)[1].strip()
            pid = int(raw) if raw.isdigit() else 0
            return pid if pid > 0 else None
    raise PodError(
        f"could not read MainPID for pod {name!r} from systemctl "
        f"(rc={cp.returncode}): {(cp.stderr or cp.stdout or '').strip()}"
    )


def unit_state(cfg: PodConfig, name: str) -> tuple[str, int]:
    """(ActiveState, NRestarts) for the pod's unit — ("unknown", 0) on error.

    Lets the up-path tell a CRASHED/crash-looping worktree gateway (a broken
    build, import error, bad config) apart from one that is just slow to come up —
    so we fail fast with the gateway's own error instead of polling a dead unit
    for the full timeout.

    On macOS launchd exposes no restart counter; see
    :func:`kiro_crew.pod.launchd.unit_state` for how the crash signal is
    preserved without one.
    """
    if IS_MACOS:
        return launchd.unit_state(cfg, name)
    cp = systemctl("show", pod_unit(cfg, name), "-p", "ActiveState", "-p", "NRestarts")
    state, restarts = "unknown", 0
    for ln in cp.stdout.splitlines():
        if ln.startswith("ActiveState="):
            state = ln.split("=", 1)[1].strip()
        elif ln.startswith("NRestarts="):
            val = ln.split("=", 1)[1].strip()
            if val.isdigit():
                restarts = int(val)
    return state, restarts


def recent_journal(cfg: PodConfig, name: str, lines: int = 30) -> str:
    """Tail the pod's log — used to surface a boot failure's real cause.

    launchd has no journal, so on macOS this tails the files the pod's plist
    routes stdout/stderr to. Same contract, different mechanism.
    """
    if IS_MACOS:
        return launchd.recent_journal(cfg, name, lines=lines)
    # journalctl is a sibling of systemctl, not routed through it — gate it too,
    # or this one call still raises a bare FileNotFoundError off-Linux.
    require_systemd()
    cp = subprocess.run(
        ["journalctl", "--user", "-u", pod_unit(cfg, name), "-n", str(lines), "--no-pager"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_systemctl_env(),
    )
    return cp.stdout


def active_names(cfg: PodConfig) -> set[str]:
    """Worktree names with an active pod unit (one cheap call)."""
    if IS_MACOS:
        try:
            return launchd.active_names(cfg)
        except launchd.LaunchdError as exc:
            raise PodError(str(exc)) from exc
    pat = f"{cfg.unit_prefix}@*.service"
    cp = systemctl("list-units", pat, "--state=active", "--no-legend", "--plain", "--no-pager")
    rx = re.compile(rf"{re.escape(cfg.unit_prefix)}@(.+)\.service")
    names: set[str] = set()
    for ln in cp.stdout.splitlines():
        parts = ln.split()
        if not parts:
            continue
        m = rx.match(parts[0])
        if m:
            names.add(m.group(1))
    return names


# --------------------------------------------------------------------------- #
# Sentinel in stop_pod's stdout meaning the pod NAME was reclaimed by a new pod
# mid-teardown (down/up race). The old pod is gone, but per-name state (the env
# file pinning CHECKOUT=) now belongs to the NEW pod and must not be deleted.
RECLAIMED_MARKER = "pod-name-reclaimed-by-new-pod"


# Backend-agnostic lifecycle. The CLI calls these so no verb has to know which
# service manager it is talking to — and so the launchd-only teardown obligation
# (below) cannot be forgotten at one call site and honoured at another.
# --------------------------------------------------------------------------- #
_MUTEX_STATE = threading.local()


@contextlib.contextmanager
def pod_name_mutex(cfg: PodConfig, name: str):
    """Serialize this pod's lifecycle transactions per name, on every platform.

    ``down`` and ``up`` are independent entry points (the CLI, and Dev Fleet which
    shells out to it) with no other per-name coordination. Both platforms reclaim
    the isolated HOME on the ``down`` path, so both have the same race: a
    stop that has just confirmed the service gone races a concurrent start, whose
    checkout pin and service definition the stop's sweep would then delete. An
    exclusive flock on a sibling lock file makes each whole transaction (pin +
    definition + start on the up side; stop + drain + HOME sweep + env unlink on
    the down side) atomic with respect to the same name.

    **Reentrant within a thread** so the CLI can hold it across a transaction
    while :func:`start_pod` / :func:`stop_pod` re-acquire it internally (their own
    protection for direct callers): flock is per open-file-description, so a naive
    second acquisition in the same thread would deadlock against itself.

    Advisory and cooperative by design: every mutating path routes through here.
    Without ``fcntl`` it degrades to a no-op, which only unit tests reach — pods
    are refused on those hosts. The lock file is deliberately never deleted:
    unlinking a lock file another process may be opening reintroduces the race the
    lock exists to close.
    """
    if fcntl is None:
        yield
        return
    held = getattr(_MUTEX_STATE, "held", None)
    if held is None:
        held = _MUTEX_STATE.held = {}
    key = f"{cfg.unit_prefix}@{name}"
    if held.get(key, 0):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    lock_file = cfg.pods_dir / f"{key}.lock"
    with open(lock_file, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


#: Reserved "name" the plane-wide lock borrows from :func:`pod_name_mutex`. Safe
#: because ``_NAME_RE`` forbids ``@``, so no real pod can ever produce this key.
_PLANE_LOCK_NAME = "@plane"


@contextlib.contextmanager
def pod_plane_mutex(cfg: PodConfig):
    """Serialize port CLAIMING across the whole pod plane, not just one name.

    :func:`pod_name_mutex` is per name, which is the right grain for the pin +
    definition + start transaction it guards -- two different pods have no reason
    to serialize their lifecycles. Port allocation is the exception: it is the one
    step where two DIFFERENT names contend, because they contend for the band
    rather than for each other's state.

    Without this, two colliding names ``up``'d concurrently (Dev Fleet's normal
    shape) hold disjoint name locks, both probe the same port free, and both boot
    onto it -- exactly the crash-loop :func:`allocate_port` exists to prevent.
    ``apps/backend.py``'s ``_reserve_free_port`` carries the same lesson one
    subsystem over: "Probing without reserving ... lets two apps be handed the same
    port -- both children then bind it and the loser dies with EADDRINUSE."

    Implemented by BORROWING :func:`pod_name_mutex` under a reserved name rather
    than copying its body: the two differ only in the key, and a second
    hand-maintained copy would drift the moment either grew a feature. The key,
    lock file, reentrancy and lock ordering are therefore identical by
    construction rather than by review.

    **What this does NOT close, stated rather than implied.** The probe releases
    the port before the pod's gateway binds it, and the gateway is a separate
    process, so no lock held here can span the choose->bind gap; a unit is
    ``Type=simple``, so ``start_pod`` returns before the bind. Holding this until a
    health check confirmed the bind WOULD close it, at the cost of serializing
    every pod boot on the plane behind up to 45 health polls of the previous one.
    What closes most of the gap instead is :func:`_walk_band_for_free` consulting
    the pins already recorded on disk, so a concurrent claim is visible before its
    gateway is listening. See that function for the residue that remains.

    Held INSIDE :func:`pod_name_mutex` wherever both are taken, so the acquisition
    order is always name -> plane and cannot deadlock against a second holder.
    """
    with pod_name_mutex(cfg, _PLANE_LOCK_NAME):
        yield


def _write_and_load_unit(cfg: PodConfig) -> subprocess.CompletedProcess | None:
    """Render the template unit AND load it, or leave nothing behind.

    The single writer of the unit file, because the invariant it maintains has to
    hold for EVERY writer: *a unit file present on disk has been loaded by
    systemd.* :func:`kiro_crew.pod.unit.unit_is_current` reads the file, but what
    systemd executes is the definition it loaded — so a writer that renders the
    current hookless template and then fails to reload leaves a file that reads
    "current" in front of a cached definition still carrying the destructive
    ``ExecStopPost``. :func:`start_pod` then skips its refresh and boots the pod
    under that cached definition, whose hook deletes the pod's HOME on any
    systemd-initiated stop — including the stop half of a ``Restart=``, which no
    ``down`` gate is in the path of. Unlinking on failure keeps the on-disk state
    honest, so the next call re-renders and retries.

    Returns ``None`` on success, or the failing ``daemon-reload`` result.
    """
    unit_mod.install_unit(cfg)
    cp = systemctl("daemon-reload")
    if cp.returncode == 0:
        return None
    unit_mod.unit_path(cfg).unlink(missing_ok=True)
    return cp


def _refresh_stale_unit(cfg: PodConfig) -> subprocess.CompletedProcess | None:
    """Re-render and load the template unit; report why the caller must not proceed.

    Returns ``None`` once systemd is running the current definition, or a failure
    carrying the remedy when it is not.
    """
    cp = _write_and_load_unit(cfg)
    if cp is None:
        return None
    detail = f" {cp.stderr.strip()}" if (cp.stderr or "").strip() else ""
    return subprocess.CompletedProcess(
        args=[],
        returncode=cp.returncode or 1,
        stdout=cp.stdout or "",
        stderr=(
            f"refreshed the pod template unit but `systemctl --user daemon-reload` "
            f"failed (rc={cp.returncode}), so systemd would still run the previous "
            "definition — which deletes a pod's HOME from a stop hook. Refusing to "
            "start or stop a pod until the unit is loaded: run `kirocrew pod install` "
            f"and retry.{detail}"
        ),
    )


def loaded_teardown_hook(cfg: PodConfig, name: str) -> bool | None:
    """Whether systemd will run a teardown hook when THIS pod's unit stops.

    Asks systemd what it has LOADED instead of reading the unit file. Disk
    freshness is not proof of a load — a hand-edited unit, or any writer whose
    reload failed, leaves the two disagreeing — and the question that decides
    whether a stop is safe is only ever "what will systemd execute now".

    ``None`` means the question could not be answered; callers must treat that as
    "assume the hook is there" rather than as absence.
    """
    cp = systemctl("show", pod_unit(cfg, name), "-p", "ExecStopPost", "--value")
    if cp.returncode != 0:
        return None
    return bool((cp.stdout or "").strip())


def _install_pod_dropin(cfg: PodConfig, name: str) -> subprocess.CompletedProcess | None:
    """Pin pod *name* to its checkout binary, or return a start-blocking failure."""
    checkout = read_env_file(cfg, name).get("CHECKOUT", "")
    if not checkout:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                f"pod {name!r} has no pinned checkout, so its boot cannot use the "
                f"worktree's own kirocrew. Run `kirocrew pod up {name}` from inside "
                "the checkout."
            ),
        )
    try:
        unit_mod.install_dropin(cfg, name, Path(checkout).expanduser())
    except (OSError, ValueError) as exc:
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"could not write the boot override for pod {name!r}: {exc}",
        )
    cp = systemctl("daemon-reload")
    if cp.returncode == 0:
        return None
    unit_mod.remove_dropin(cfg, name)
    detail = f" {cp.stderr.strip()}" if (cp.stderr or "").strip() else ""
    return subprocess.CompletedProcess(
        args=[],
        returncode=cp.returncode or 1,
        stdout=cp.stdout or "",
        stderr=(
            f"wrote the boot override for pod {name!r} but `systemctl --user "
            f"daemon-reload` failed (rc={cp.returncode}), so systemd would still "
            "boot the globally installed kirocrew. Refusing to start it; retry or "
            f"run `kirocrew pod install`.{detail}"
        ),
    )


def start_pod(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """Bring pod *name* up through whichever service manager this host uses."""
    with pod_name_mutex(cfg, name):
        if IS_MACOS:
            # Re-rendered every start, which is why launchd needs no equivalent
            # of the systemd path's stale-ExecStart self-heal. The mutex
            # serializes against a concurrent stop of the same name, whose
            # definition unlink and HOME sweep would otherwise race this write.
            launchd.write_plist(cfg, name)
            return launchd.start(cfg, name)

        # Self-heal a stale installed unit before booting it: the template bakes
        # an absolute kirocrew path at install time (a pruned worktree leaves it
        # failing EXEC 203), and a unit installed by an older build can still
        # carry the teardown hook this one removed.
        if not unit_mod.unit_is_current(cfg):
            refused = _refresh_stale_unit(cfg)
            if refused is not None:
                return refused
        refused = _install_pod_dropin(cfg, name)
        if refused is not None:
            return refused
        return systemctl("start", pod_unit(cfg, name))


# Where a systemd cgroup's process list lives on a cgroup-v2 host.
_CGROUP_ROOT = Path("/sys/fs/cgroup")

# How long teardown waits for a stopped unit's process tree to go away. Named so
# the wait and the message that reports it expiring cannot drift apart.
DRAIN_TIMEOUT_SECS = 15.0


def cgroup_procs_file(cfg: PodConfig, name: str) -> Path | None:
    """``cgroup.procs`` for pod *name*'s unit, or ``None`` when unresolvable.

    Must be read while the unit is still up: systemd reports an empty
    ``ControlGroup`` once it goes inactive, so asking after the stop is too late.
    Returns ``None`` off cgroup-v2 layouts (and on any host where the path does
    not exist), which makes the drain wait an optimisation rather than a
    dependency — the post-delete verification in :func:`stop_pod` is what actually
    decides whether teardown succeeded.
    """
    cp = systemctl("show", pod_unit(cfg, name), "-p", "ControlGroup", "--value")
    rel = (cp.stdout or "").strip()
    if cp.returncode != 0 or not rel.startswith("/"):
        return None
    procs = _CGROUP_ROOT / rel.lstrip("/") / "cgroup.procs"
    return procs if procs.parent.is_dir() else None


def drain_cgroup(procs: Path, timeout: float = DRAIN_TIMEOUT_SECS) -> list[str]:
    """Wait for a stopped unit's cgroup to empty; return the PIDs still in it.

    An empty list means every pod-scoped process is gone, so the HOME can be
    deleted without racing a writer that would recreate it. A vanished cgroup
    directory counts as drained — systemd removes it once the last process exits.
    An unreadable one is reported as drained too: nothing better can be observed
    from here, and the caller verifies the deleted HOME afterwards regardless.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            pids = [ln.strip() for ln in procs.read_text().splitlines() if ln.strip()]
        except OSError:
            return []
        if not pids:
            return []
        if time.monotonic() >= deadline:
            return pids
        time.sleep(0.2)


def resolved_pod_home(cfg: PodConfig, name: str) -> Path:
    """Pod *name*'s HOME as :func:`cleanup_home` reports it.

    Teardown messages must agree on one spelling of the path. ``pod_home`` returns
    it unresolved, while ``cleanup_home`` resolves before deleting (its safety
    check needs the real parent), so quoting both in one failure read as two
    different directories wherever ``$HOME`` is a symlink — which is the default
    layout on a dev desktop.
    """
    try:
        return (cfg.pod_root / name).resolve()
    except OSError:
        return pod_home(cfg, name)


def stop_pod(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """Stop pod *name* and reclaim its isolated HOME, or say why it could not.

    Teardown lives HERE on both platforms rather than in a post-stop service hook.
    systemd runs ``ExecStopPost`` before the final kill of the unit's cgroup, so a
    hook-based delete raced the pod's own surviving subprocesses — they reopened
    their audit log in append mode and recreated the directory behind it — and it
    also ran on the stop half of a ``Restart=``, bringing the pod back up on a
    home that no longer had its sessions or config. Reclaiming after the service
    is confirmed down fixes both, at the cost of a pod that goes away without a
    ``down`` leaving its HOME behind; :func:`orphan_homes` reports those.

    Sequenced so nothing is deleted while a writer could still be alive: stop the
    service, wait for its process tree to drain, delete, then VERIFY. A HOME that
    survives is reported as a failure — never as zero residue.
    """
    with pod_name_mutex(cfg, name):
        if IS_MACOS:
            return _stop_pod_launchd(cfg, name)
        # A unit installed by an OLDER build still carries the destructive
        # ExecStopPost, and `systemctl stop` runs it before our drain — deleting
        # the HOME under the pod's own live processes, which is the exact defect
        # this path exists to remove. Refresh BEFORE stopping: daemon-reload
        # re-parses the fragment for an already-running unit, and the stop job has
        # not started yet, so the refreshed (hookless) definition is what runs.
        #
        # Gated on what systemd has LOADED, never on the unit file: disk freshness
        # is not proof of a load, so a hookless file can sit in front of a cached
        # definition that still deletes the HOME. An unanswerable query counts as
        # "hook present" — the only safe reading.
        #
        # Refuse rather than proceed when the reload fails. Proceeding would mean
        # knowingly triggering the hook-races-live-processes defect this change
        # removes, on the argument that the HOME is being deleted anyway — the
        # same reasoning the fix rejects. A pod left running after a loud,
        # retryable failure is the safer end state.
        if loaded_teardown_hook(cfg, name) is not False:
            refused = _refresh_stale_unit(cfg)
            if refused is not None:
                return refused
        # Read the cgroup path BEFORE stopping: systemd clears ControlGroup on
        # an inactive unit.
        procs_file = cgroup_procs_file(cfg, name)
        cp = systemctl("stop", pod_unit(cfg, name))
        if cp.returncode != 0:
            # The unit may still be live; deleting its HOME here is exactly the
            # race this ordering exists to avoid.
            return cp
        survivors = drain_cgroup(procs_file) if procs_file is not None else []
        # Resolved, because cleanup_home reports the resolved path: on a host
        # whose home is a symlink, naming it both ways reads as two directories.
        leftover = resolved_pod_home(cfg, name)
        if survivors:
            # Deleting now would BE the original defect. A process that outlived
            # the drain either holds the tree open or reopens its audit log in
            # append mode right behind the delete, and the verification below
            # cannot catch that because the recreation lands after it. So leave
            # the HOME alone and name what is holding it.
            shown = ", ".join(survivors[:5])
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=cp.stdout or "",
                stderr=(
                    f"pod stopped but {len(survivors)} pod process(es) are still in "
                    f"its cgroup (pid {shown}) after {DRAIN_TIMEOUT_SECS:.0f}s, so "
                    f"its isolated HOME at {leftover} was NOT deleted — this pod is "
                    f"NOT zero-residue. Reclaim it with `kirocrew pod down {name}` "
                    "once nothing is writing there."
                ),
            )
        rc = cleanup_home(cfg, name)
        dropin_path = unit_mod.dropin_path(cfg, name)
        had_dropin = dropin_path.exists() or dropin_path.is_symlink()
        dropin_gone = unit_mod.remove_dropin(cfg, name)
        reload_cp: subprocess.CompletedProcess | None = None
        if had_dropin and dropin_gone:
            reload_cp = systemctl("daemon-reload")
        if rc != 0 or leftover.exists():
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=cp.stdout or "",
                stderr=(
                    f"pod stopped but its isolated HOME is still at {leftover} — "
                    f"teardown is incomplete, so this pod is NOT zero-residue. "
                    f"Reclaim it with `kirocrew pod down {name}` once nothing is "
                    "writing there."
                ),
            )
        if not dropin_gone:
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=cp.stdout or "",
                stderr=(
                    f"pod stopped and its HOME was reclaimed, but the boot override at "
                    f"{unit_mod.dropin_path(cfg, name)} could not be removed — this pod "
                    f"is NOT zero-residue. Delete it, then run `systemctl --user "
                    "daemon-reload`."
                ),
            )
        if reload_cp is not None and reload_cp.returncode != 0:
            detail = f" {reload_cp.stderr.strip()}" if (reload_cp.stderr or "").strip() else ""
            return subprocess.CompletedProcess(
                args=[],
                returncode=reload_cp.returncode or 1,
                stdout=cp.stdout or "",
                stderr=(
                    "pod stopped and its on-disk override was removed, but `systemctl "
                    "--user daemon-reload` failed, so systemd may still retain it in "
                    f"memory — this pod is NOT zero-residue.{detail}"
                ),
            )
        return cp


def _stop_pod_launchd(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """The macOS half of :func:`stop_pod` — called with the name mutex held.

    launchd has no cgroup to drain, so the surviving-writer problem is handled by
    sweeping the grace window instead: ``bootout`` confirms the SERVICE process is
    unloaded, but a dying child can outlive it by a beat and flush state on exit
    (observed in a real teardown — cleanup ran, verification passed, then a child
    wrote settings back and resurrected the HOME milliseconds later).
    """
    # launchd.stop() is authoritative: rc 0 means the label is confirmed
    # unloaded (a bootout of an unloaded label is a no-op success). A non-zero
    # rc means the unload could NOT be confirmed — in that case do NOT touch
    # the HOME: it may belong to a live gateway.
    cp = launchd.stop(cfg, name)
    if cp.returncode != 0:
        return cp
    leftover = resolved_pod_home(cfg, name)
    # Observe the FULL window — no early exit on a clean sample (the dying
    # child that motivated this flushed state after a beat). But DO exit the
    # moment the name is claimed by a NEW pod: the mutex serializes callers that
    # route through it, and a new `up` re-writes the plist BEFORE bootstrapping,
    # so plist presence is the claim marker for any writer that bypasses it.
    # Deliberately a pure filesystem check: probing launchctl here would shell
    # out on every sweep and break on hosts without launchd (the unit suites run
    # this path on Linux/Windows CI).
    #
    # A reclaimed name is reported via RECLAIMED_MARKER in stdout so the caller
    # knows the teardown handed over: it must NOT delete the per-pod env file,
    # which now pins the NEW pod's checkout.
    for _ in range(6):
        if launchd.plist_path(cfg, name).exists():
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
            )
        cleanup_home(cfg, name)
        time.sleep(0.5)
    if launchd.plist_path(cfg, name).exists():
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
        )
    cleanup_home(cfg, name)
    if leftover.exists():
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=cp.stdout or "",
            stderr=(
                f"pod stopped but its isolated HOME keeps reappearing at "
                f"{leftover} — a process is still writing there, so teardown "
                "is incomplete. Remove it by hand and report this."
            ),
        )
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=cp.stdout or "", stderr="")


def orphan_homes(cfg: PodConfig) -> list[str]:
    """Pod HOMEs left on disk with no live pod and no installed definition.

    Reachable on BOTH platforms, because neither reclaims from a post-stop service
    hook any more (see :func:`stop_pod`): a pod that goes away without an explicit
    ``down`` — a crash, a raw ``systemctl --user stop`` / ``launchctl bootout``, a
    host reboot — leaves its isolated HOME behind. Reported rather than deleted so
    the operator decides, and so the delete still routes through
    :func:`cleanup_home`'s re-validation via ``kirocrew pod down <name>``.
    """
    try:
        # never follow a symlink: a link under pod_root can point at a LIVE
        # pod's HOME (or anywhere), and everything downstream of this
        # enumeration treats the NAME as the directory it will judge and
        # delete. A real pod HOME is always created as a plain directory.
        entries = [p for p in cfg.pod_root.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return []
    live = active_names(cfg)
    out = []
    for p in entries:
        if p.name.startswith("."):
            continue
        if p.name in live:
            continue
        # macOS writes a per-pod plist at `up` and drops it at `down`, so its
        # presence means the pod is installed rather than orphaned. systemd's
        # template unit is machine-wide, so liveness is the only signal there.
        if IS_MACOS and launchd.plist_path(cfg, p.name).exists():
            continue
        out.append(p.name)
    return sorted(out)


def install_backend(cfg: PodConfig) -> tuple[str, subprocess.CompletedProcess | None]:
    """Install whatever machine-wide definition the backend needs.

    systemd needs one template unit + a daemon-reload. launchd has no template
    concept — each pod's plist is written at ``up`` — so there is nothing to
    install, and saying so is better than writing a file that does nothing.

    Returns ``(message, reload_result)``. Raises :class:`PodError` only for an
    unusable host, and does so BEFORE writing anything, so an unsupported
    platform never leaves a stray definition behind. A failed reload comes back
    as the second element rather than an exception, because the caller reports
    that as a hard exit while the gate refusal is converted by the CLI's
    dispatch layer — two different documented behaviours.

    Routed through :func:`_write_and_load_unit` so this path upholds the same
    invariant the lifecycle paths do: a unit file left on disk has been loaded.
    A hookless file left behind by a failed reload would make
    ``unit_is_current`` report "current" while systemd still runs the old
    ``ExecStopPost`` — so :func:`start_pod` would skip its refresh and boot the
    pod under a definition that deletes its HOME from a stop hook.
    """
    require_backend()
    if IS_MACOS:
        return (
            "nothing to install on macOS: launchd has no template units, so each "
            "pod's agent plist is written at `kirocrew pod up <worktree>`.",
            None,
        )
    dst = unit_mod.unit_path(cfg)
    failed = _write_and_load_unit(cfg)
    if failed is not None:
        return (
            f"rendered the pod template unit but `systemctl --user daemon-reload` "
            f"failed, so it was removed again rather than left unloaded at {dst}",
            failed,
        )
    return (
        f"installed pod template unit → {dst}\nsystemctl --user daemon-reload OK",
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )


# --------------------------------------------------------------------------- #
# Port ownership — WHO answers the pod's port, not merely whether anyone does.
#
# A pod's port is derived, not allocated: `base + (cksum(name) % 199) + 1` maps
# every pod name into 199 slots, and `PORT=` in the per-pod env file can pin any
# port by hand. So two pods colliding on one port is an ordinary event, and the
# live gateway is reachable on the same loopback interface. Whoever binds first
# wins; the loser's gateway exits "address already in use" and the unit
# crash-loops behind it.
#
# A bare `GET /api/health` cannot tell those apart. `{"ok": true}` is the same
# answer from any Kiro Crew gateway on the host, and its identity fields say
# `app=kirocrew` plus a version — true of the squatter as well. Reading a 200 as
# "this pod is up" therefore reports a crash-looping pod as healthy and points
# the operator's browser at somebody else's instance.
#
# `instances/run_marker` states the rule this module was breaking: it
# "deliberately does not offer a bare 'is something listening' helper, so no
# caller can mistake reachability for identity". The pod probe is now held to it
# — the reachability probe is private and every caller goes through `health`.
# --------------------------------------------------------------------------- #
#: Proven: the pod's own gateway process holds the port.
OWNER_POD = "pod"
#: Proven: something OTHER than this pod holds the port (another pod, the live
#: gateway, an unrelated process).
OWNER_FOREIGN = "foreign"
#: Not decidable on this host — no listener-lookup tool, or the service manager
#: could not be asked. Callers keep their pre-identity behaviour.
OWNER_UNPROVEN = "unproven"

#: :func:`health` verdict for a port that answers but is NOT served by this pod.
#: Negative like ``_wait_healthy``'s crash sentinel, so it can never collide with
#: an HTTP status or with ``0`` (unreachable).
HEALTH_FOREIGN = -2


class PodOwnershipUnproven(PodError):
    """Ownership could not be PROVEN either way, so a credential was withheld.

    A distinct type because the two refusals want different handling. A
    :data:`OWNER_FOREIGN` verdict is positive knowledge that the port belongs to
    somebody else, and every caller should stop. "Could not prove it" is not that
    — the pod may well be serving — so a caller whose main job is something other
    than the credential (``pod up``, which has already booted the pod) can go on
    and report what it does know, while still never putting the secret on the
    wire. ``pod token`` has nothing else to do and still fails.
    """


def _pod_pid_record_path(cfg: PodConfig, name: str, port: int) -> Path:
    """Path of pod *name*'s gateway pid sidecar inside its isolated home."""
    return cfg.home_dir(name) / run_marker.RUN_DIR_NAME / run_marker.pid_file_name(port)


def _pod_recorded_pid(cfg: PodConfig, name: str, port: int) -> int | None:
    """Gateway PID recorded in pod *name*'s isolated home, PROVEN to still name
    that same process, or ``None``.

    A bare pid is not an identity. ``clear_marker`` runs only on a graceful
    shutdown, so a crashed or SIGKILLed pod leaves its sidecar behind, and the
    number in it can afterwards be recycled onto an unrelated process. The
    gateway therefore records its start-time identity in a ``.start`` sidecar
    beside the pid (``run_marker.pid_start_token``), and this reader re-derives
    that identity live: a recycled pid answers with its OWN start time, which
    cannot match the one the pod's gateway recorded.

    Fails CLOSED on every way of not knowing -- no record, no start identity (a
    pod whose checkout predates the binding), or a host that will not report a
    start time at all, which includes a Windows box with no implementation. An
    unproven record must read as "no record", never as one that agrees.
    """
    record = run_marker.read_pid_record_path(_pod_pid_record_path(cfg, name, port))
    if record is None:
        return None
    pid, recorded_start = record
    if not recorded_start:
        return None
    live_start = run_marker.pid_start_token(pid)
    if not live_start or live_start != recorded_start:
        return None
    return pid


def _unproven_remedy(cfg: PodConfig, name: str, port: int) -> str:
    """How to fix an unproven ownership verdict -- the two causes differ.

    A record that is absent, malformed, or names a pid whose start identity no
    longer matches is crash residue or a stale generation, and a restart writes a
    fresh, provable one. A record that carries NO start identity is a different
    failure with the opposite remedy: a pod's gateway is its checkout's own venv
    binary, so a worktree branched before the start-identity sidecar existed
    writes no token, and restarting it writes none either -- telling the operator
    to restart would send them round a loop that cannot terminate.
    """
    record = run_marker.read_pid_record_path(_pod_pid_record_path(cfg, name, port))
    if record is not None and not record[1]:
        return (
            f"The record names a pid but carries no start identity, and a restart "
            f"cannot add one: a pod's gateway is its worktree's own venv binary, so "
            f"this checkout predates the start-identity sidecar. Update that "
            f"worktree to a build that writes one and re-provision it "
            f"(`kirocrew pod provision {name}`), then `kirocrew pod down {name} && "
            f"kirocrew pod up {name}`."
        )
    return (
        f"A record left behind by a crash cannot attest, so restarting it is what "
        f"re-establishes the proof: `kirocrew pod down {name} && "
        f"kirocrew pod up {name}`."
    )


def port_owner(cfg: PodConfig, name: str, port: int) -> str:
    """Attest who serves *port* for pod *name*.

    The proof is agreement between two independent pod-owned facts: the gateway
    PID sidecar in the pod's isolated ``0700`` home and the service manager's
    current ``MainPID`` (or launchd PID). The pod unit is ``Type=simple`` and
    execs the gateway in place, so that PID is the process that bound the port,
    and the sidecar is written only AFTER that bind succeeds. A pod that lost
    the bind race therefore has no agreeing record to offer.

    That agreement is only worth anything because the record proves its own
    freshness: :func:`_pod_recorded_pid` answers with a pid ONLY when the
    process it names still has the start-time identity the record was written
    with, so a sidecar left behind by a crash cannot attest once its pid has
    been recycled. Without that binding the number alone could name somebody
    else, which is the only reason listener attribution was ever load-bearing.

    Listener attribution is CORROBORATION, not a precondition. Its view is
    scoped to the 127.0.0.1 listener the HTTP client reaches, so a pid on the
    port that is not ours is positive proof of a foreign responder and outranks
    any record. But when the host has no usable listener evidence -- no lookup
    tool, a lookup that failed, or a lookup that simply could not see the socket
    -- a provably fresh PID-record/MainPID agreement is sufficient. That is the
    normal supported path on a minimal Linux host, and on any host where the
    caller cannot see another process's sockets (an unprivileged process asking
    ``lsof`` about a gateway started by the user's service manager, which is
    exactly how ``pod api`` runs). Requiring attribution there would withhold
    the credential from every healthy pod forever.

    Listener attribution is still never sufficient ON ITS OWN: a pid that holds
    the port but has no fresh record behind it stays :data:`OWNER_UNPROVEN`.
    """
    if not IS_POSIX:
        return OWNER_UNPROVEN
    try:
        recorded = _pod_recorded_pid(cfg, name, port)
        ours = main_pid(cfg, name)
    except Exception:
        return OWNER_UNPROVEN
    attested = recorded is not None and ours is not None and recorded == ours
    verdict = OWNER_POD if attested else OWNER_UNPROVEN

    if not listening_pid_tool_available():
        return verdict
    try:
        pids = set(loopback_owner_pids(find_port_listeners(port)))
    except Exception:
        return verdict
    if not pids:
        return verdict
    if ours is None or ours not in pids:
        return OWNER_FOREIGN
    return verdict


def _probe_health(port: int, timeout: int = 3) -> int:
    """Raw HTTP status of ``/api/health`` on *port*, or 0 if unreachable.

    Reachability ONLY — it says nothing about who answered, which is why it is
    private. Callers want :func:`health`.

    Every failure collapses to 0, ``http.client.HTTPException`` included: a
    process holding the port that answers the TCP handshake without speaking
    HTTP raises ``BadStatusLine``, which is neither ``OSError`` nor ``URLError``.
    That case is not hypothetical here — a foreign listener on a derived port is
    the reason this probe is being hardened — and it must read as "not serving"
    rather than escaping as a traceback out of ``pod status``.
    """
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        # Loopback-only probe to the pod's own gateway on 127.0.0.1; the URL is
        # internally derived (never attacker-supplied), so the dynamic-URL SSRF
        # audit rule is a false positive here.
        with loopback_urlopen(url, timeout=timeout) as resp:  # nosemgrep
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0


def health(cfg: PodConfig, name: str, port: int, timeout: int = 3) -> int:
    """HTTP status of **this pod's** ``/api/health``, or a non-positive verdict.

    * 200 = open; 401/403 = serving but gated — all three mean this pod is up.
    * 0 = nothing reachable on the port.
    * :data:`HEALTH_FOREIGN` = the port answers, but the responder is provably
      not this pod, so this pod is NOT up.

    The ownership check runs only once something has answered, which keeps the
    common case free: a stopped pod costs one refused connection and no process
    lookup, exactly as before.
    """
    code = _probe_health(port, timeout)
    if code == 0:
        return 0
    if port_owner(cfg, name, port) == OWNER_FOREIGN:
        return HEALTH_FOREIGN
    return code


# --------------------------------------------------------------------------- #
# Token mint — reads the pod's OWN .local_secret (in its isolated HOME), then
# calls /api/token/local with X-Local-Secret. Keeps the secret read inside this
# process (never an agent-issued `cat`).
# --------------------------------------------------------------------------- #
def mint_token(cfg: PodConfig, name: str, ttl: str = "2h") -> str:
    secret_file = cfg.home_dir(name) / ".local_secret"
    try:
        secret = secret_file.read_text().strip()
    except FileNotFoundError as exc:
        raise PodError(
            f"no .local_secret for pod {name!r} — is it running? ({secret_file})"
        ) from exc
    port = derive_port(cfg, name)
    owner = port_owner(cfg, name, port)
    if owner != OWNER_POD:
        # Positive proof REQUIRED here, unlike `health`. This call sends the pod's
        # own ``.local_secret`` and returns a dashboard credential for whatever
        # answered, so the two ways of being wrong are not symmetrical: refusing a
        # live pod costs an error message, while proceeding on an unproven port
        # hands a different local user -- who can bind 127.0.0.1 but cannot read
        # this 0600 secret -- a credential for this pod. That is the same reason
        # ``port_resolution._gateway_owns_port`` fails closed on the path that
        # sends the secret, including when the listener lookup is simply missing.
        if owner == OWNER_FOREIGN:
            raise PodError(
                f"refusing to mint a credential for pod {name!r}: :{port} is held "
                f"by another process, not this pod's gateway. `kirocrew pod status "
                f"{name}` shows the same verdict; a credential minted here would "
                f"belong to whatever owns that port."
            )
        raise PodOwnershipUnproven(
            f"withholding a credential for pod {name!r}: could not prove "
            f"ownership of :{port} from the gateway pid record and the service "
            f"manager's current MainPID, so this call cannot prove which "
            f"process would receive the pod's secret. "
            f"{_unproven_remedy(cfg, name, port)}"
        )
    url = f"http://127.0.0.1:{port}/api/token/local?ttl={urllib.parse.quote(str(ttl))}"
    req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
    try:
        # Loopback-only call to the pod's own gateway on 127.0.0.1; the URL is
        # internally derived, so the dynamic-URL SSRF audit rule is a false positive.
        with loopback_urlopen(req, timeout=5) as resp:  # nosemgrep
            token = json.loads(resp.read()).get("token", "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PodError(f"token mint failed on :{port} ({name}): {exc}") from exc
    if not token:
        raise PodError(f"gateway returned empty token on :{port} ({name})")
    return token


# --------------------------------------------------------------------------- #
# Authenticated API front door. The caller never handles the pod credential:
# mint_token() proves ownership, then this module adds the query token expected
# by dashboard.token_auth — and sends it over the pod's private unix socket, so
# only the pod that owns the credential can ever receive it.
# --------------------------------------------------------------------------- #
API_METHODS: tuple[str, ...] = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
API_READ_METHODS: tuple[str, ...] = ("GET", "HEAD")
API_TIMEOUT_SECS = 30
API_BODY_MAX_BYTES = 32 * 1024 * 1024


def api_path(path: str) -> str:
    """Return *path* as a rooted ``/api`` path without any credential.

    Full URLs are accepted but their authority is discarded: every request is
    still sent to the selected pod on loopback. A caller-supplied ``token`` query
    parameter is refused before host access; the error never repeats its value or
    the credential-bearing URL.
    """
    raw = path.strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise PodError("invalid request path (value withheld)") from exc
    if parts.scheme or parts.netloc:
        raw = urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))
        try:
            parts = urllib.parse.urlsplit(raw)
        except ValueError as exc:  # defensive: the authority was already removed
            raise PodError("invalid request path (value withheld)") from exc
    try:
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    except ValueError as exc:
        raise PodError("invalid request query (value withheld)") from exc
    if any(key == "token" for key, _value in query):
        raise PodError(
            "the request path carries a `token` query parameter; refusing to "
            "forward or display it. `pod api` mints the pod credential itself — "
            "remove that parameter and retry."
        )
    normalized = parts.path or "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/api" and not normalized.startswith("/api/"):
        normalized = "/api" + normalized
    if parts.query:
        normalized += "?" + parts.query
    return normalized


def _authenticated_url(port: int, path: str, token: str) -> str:
    parts = urllib.parse.urlsplit(api_path(path))
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("token", token))
    target = urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query), fragment="")
    )
    return f"http://127.0.0.1:{port}{target}"


def _read_capped(stream: object, method: str, path: str, name: str) -> str:
    """Read and decode one response without buffering more than the hard cap."""
    try:
        chunk = stream.read(API_BODY_MAX_BYTES + 1)  # type: ignore[attr-defined]
    except (OSError, http.client.HTTPException) as exc:
        raise PodError(
            f"{method} {api_path(path)} on pod {name!r}: the response body could "
            f"not be read ({type(exc).__name__}).\n"
            f"  Is it healthy? kirocrew pod status {name}\n"
            f"  Its logs:      kirocrew pod logs {name}"
        ) from exc
    if len(chunk) > API_BODY_MAX_BYTES:
        raise PodError(
            f"{method} {api_path(path)} on pod {name!r} returned more than "
            f"{API_BODY_MAX_BYTES} bytes; refusing to buffer it. Narrow the request."
        )
    return chunk.decode("utf-8", "replace")


def _scrub_token_string(text: str, token: str) -> str:
    """Remove printable encodings of *token* from one string."""
    escaped = "".join(f"\\u{ord(char):04x}" for char in token)
    forms = {
        token,
        urllib.parse.quote(token, safe=""),
        urllib.parse.quote_plus(token, safe=""),
        escaped,
        escaped.upper().replace("\\U", "\\u"),
    }
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = text.replace(form, "<token>")
    return text


def _scrub_json_tokens(value: object, token: str) -> object:
    """Recursively scrub every JSON string key and value."""
    if isinstance(value, str):
        return _scrub_token_string(value, token)
    if isinstance(value, list):
        return [_scrub_json_tokens(item, token) for item in value]
    if isinstance(value, dict):
        return {
            _scrub_token_string(key, token): _scrub_json_tokens(item, token)
            for key, item in value.items()
        }
    return value


def _scrub_token(text: str, token: str) -> str:
    """Remove raw, encoded, and JSON-escaped forms of *token* from text."""
    if not token:
        return text
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return _scrub_token_string(text, token)
    try:
        scrubbed = _scrub_json_tokens(parsed, token)
    except RecursionError:
        return "<response omitted: JSON nesting exceeds scrubber limit>"
    return json.dumps(scrubbed, ensure_ascii=False)


def pod_api(
    cfg: PodConfig,
    name: str,
    method: str,
    path: str,
    *,
    data: str = "",
    allow_write: bool = False,
) -> tuple[int, str]:
    """Make one authenticated request to pod *name* and return status/body.

    The request is delivered over the pod's own dashboard unix socket and nowhere
    else. Its port is not a transport here, only the ``Host`` the gateway sees:
    the credential this function mints is valid as an ``mc_token_<port>`` cookie,
    so delivering it on a fresh TCP connection would hand it to whatever holds
    that port by then -- a pod that exits between the mint and the send leaves
    the port free for any local user to bind. The socket cannot be answered by
    another user, because it lives in the pod's owner-only home.
    """
    validate_name(name)
    method = method.upper()
    normalized = api_path(path)  # validation happens before any host access
    if method not in API_METHODS:
        raise PodError(f"unsupported method {method!r} (expected one of: {', '.join(API_METHODS)})")
    if method not in API_READ_METHODS and not allow_write:
        raise PodError(
            f"refusing to send {method} to pod {name!r}: `pod api` permits only "
            f"{', '.join(API_READ_METHODS)} unless --allow-write is passed.\n"
            f"  Retry: kirocrew pod api {name} {method} {normalized} --allow-write"
        )
    if not is_active(cfg, name):
        raise PodError(
            f"pod {name!r} is not running, so there is nothing to call.\n"
            f"  Start it:   kirocrew pod up {name}\n"
            f"  What is up: kirocrew pod ls"
        )
    port = derive_port(cfg, name)
    socket_path = pod_socket_path(cfg, name, port)
    if not socket_path.exists():
        # Refuse BEFORE minting: `mint_token` sends the pod's `.local_secret` to
        # obtain a credential, so a request that cannot be delivered must not pay
        # for one. Existence is checked only to produce this actionable message
        # -- correctness does not rest on it, because the send below has no TCP
        # path to fall back to if the file disappears in between.
        raise PodError(
            f"refusing to send {method} {normalized} to pod {name!r}: its private API "
            f"socket is not there ({socket_path}).\n"
            f"  `pod api` will not retry on 127.0.0.1:{port}. That port is ordinary "
            f"loopback, so a process that is not this pod can hold it, and the "
            f"credential this command mints would be sent to whatever answered.\n"
            f"  Is it healthy? kirocrew pod status {name}\n"
            f"  Its logs:      kirocrew pod logs {name}\n"
            f"  Restart it:    kirocrew pod down {name} && kirocrew pod up {name}"
        )
    token = mint_token(cfg, name)
    url = _authenticated_url(port, normalized, token)
    body = data.encode("utf-8") if data else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        # Over the pod's own unix socket, never TCP: `unix_socket_urlopen` has no
        # TCP handler, so a dead or replaced listener cannot receive this token.
        # Caller input contributes only the path, never the host or the socket.
        with unix_socket_urlopen(  # nosemgrep
            request, timeout=API_TIMEOUT_SECS, socket_path=socket_path
        ) as response:
            raw = _read_capped(response, method, normalized, name)
            return response.status, _scrub_token(raw, token)
    except urllib.error.HTTPError as exc:
        raw = _read_capped(exc, method, normalized, name)
        return exc.code, _scrub_token(raw, token)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # urllib may include request.full_url in exception text. Do not render
        # exception values at all: the authenticated URL is credential-bearing.
        raise PodError(
            f"{method} {normalized} on pod {name!r} did not complete over its API "
            f"socket ({socket_path}): {type(exc).__name__}.\n"
            f"  Not retried on 127.0.0.1:{port} — the credential must not be sent "
            f"to a process that is not this pod.\n"
            f"  Is it healthy? kirocrew pod status {name}\n"
            f"  Its logs:      kirocrew pod logs {name}"
        ) from exc


# --------------------------------------------------------------------------- #
# Seed sanitization — deny-by-default. A seeded pod must NEVER be able to grab a
# live messaging identity, so we only ever return a config with the tunnel and
# every self-activating channel DISABLED. Anything that prevents us from positively
# guaranteeing that (missing file, bad JSON) returns None → the caller skips the
# seed and the pod boots blank.
# --------------------------------------------------------------------------- #

# Config sections a sanitized seed boots with ``enabled=False``. Deny-by-default:
# every channel carrying a config-level ``enabled`` is listed, because that flag is
# the only thing between a seed cloned from the real config (the intended
# ``--seed ~/.kiro/crew`` workflow) and a pod that answers real people as the
# operator's bot. The channel's credential is not a second gate — Telegram,
# Discord, Webex and Weixin read their token straight out of the seeded
# config.json; iMessage needs no credential at all, since its transport is the
# operator's own signed-in Messages.app; and Teams keeps its App ID in config.json
# while ``MICROSOFT_APP_PASSWORD`` reaches the pod through the inherited env.
# Slack is deliberately absent: it has no config-level enable, being gated purely
# on credentials that ``build_pod_env`` scrubs.
#
# ``test_pod.py`` pins this tuple against ``channels.builtin_channel_descriptors()``
# so a channel added to the roster cannot reach a pod ungated.
SEED_DISABLED_SECTIONS: tuple[str, ...] = (
    "tunnel",
    "wecom",
    "telegram",
    "discord",
    "webex",
    "teams",
    "weixin",
    "imessage",
    # WhatsApp is the sharpest case on this list: its transport is the operator's
    # OWN account, paired as a linked device, so a seeded pod booting it live would
    # send from the operator's real number using a credential that is not an env
    # var this env can scrub (the session store lives under the data home).
    "whatsapp",
    "feishu",
)


def _force_seed_agent_security(data: dict) -> None:
    """Keep a copied config from disabling the pod agent's isolation floor.

    A fixture or explicit seed directory is data, not authority to unconfine the
    agent that will inspect it. ``auto`` selects the platform backend and fails
    closed when none exists; both opt-outs are reset so a copied live config
    cannot turn that refusal into unsandboxed execution or suppress its warning.
    Other agent settings survive for realistic fixtures.
    """
    if not isinstance(data.get("agent"), dict):
        data["agent"] = {}
    agent = data["agent"]
    agent["sandbox"] = "auto"
    agent["sandbox_allow_unsandboxed_exec"] = False
    agent["sandbox_allow_no_isolation"] = False


def _apply_seed_config_floor(data: dict) -> None:
    """Disable self-activating sections and restore the agent sandbox floor."""
    for section in SEED_DISABLED_SECTIONS:
        if not isinstance(data.get(section), dict):
            data[section] = {}
        data[section]["enabled"] = False
    _force_seed_agent_security(data)


def sanitized_seed_config(seed_dir: Path) -> dict | None:
    """Read ``<seed_dir>/config.json`` and return it with ``enabled`` forced to
    False on every ``SEED_DISABLED_SECTIONS`` section, or None if it can't be
    safely sanitized (no file / bad JSON / sensitive path). Never copies any other
    state (DB / sessions / crons)."""
    # ``--seed`` is a user-supplied path: refuse to read from a sensitive /
    # credential location before touching the file. Resolve first so a symlink or
    # ".." can't smuggle past the guard.
    from kiro_crew.security import is_sensitive_path

    src_cfg = seed_dir / "config.json"
    if is_sensitive_path(os.path.realpath(str(src_cfg))):
        print(f"WARN: refusing to read seed config from sensitive path: {src_cfg} — skipping seed")
        return None
    if not src_cfg.is_file():
        return None
    try:
        data = json.loads(src_cfg.read_text())
    except (OSError, ValueError):
        print("WARN: could not parse seed config.json — skipping seed (pod boots blank)")
        return None
    if not isinstance(data, dict):
        return None
    _apply_seed_config_floor(data)
    return data


def is_scenario_ref(value: str) -> bool:
    """Return whether a seed value is a fixture name rather than a directory path."""
    if not value or value.startswith(("~", ".")):
        return False
    if "/" in value or "\\" in value or os.sep in value:
        return False
    return True


def resolve_seed_scenario(value: str) -> str:
    """Validate that *value* names a shipped fixture."""
    available = seed_mod.available_fixtures()
    if value in available:
        return value
    listed = ", ".join(available) if available else "(none)"
    raise PodError(
        f"unknown seed scenario {value!r}. Available scenarios: {listed}.\n"
        f"  To seed from a directory instead, pass a path: "
        f"--seed ./{value} or --seed /abs/path/{value}"
    )


def _open_seed_regular_file(home_fd: int, name: str) -> int:
    """Open seed metadata without letting a FIFO block before type validation."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(name, flags, dir_fd=home_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"seeded home entry {name!r} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _prepare_seeded_home_fd(home_fd: int) -> None:
    """Finish pod-owned setup without reopening the seeded home by name."""
    try:
        fd = _open_seed_regular_file(home_fd, "config.json")
    except FileNotFoundError:
        data: dict = {}
    except OSError as exc:
        raise PodError(f"could not open seeded config.json: {exc}") from exc
    else:
        try:
            with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as handle:
                fd = -1
                text = handle.read(1024 * 1024 + 1)
        except (OSError, UnicodeError) as exc:
            raise PodError(f"could not read seeded config.json: {exc}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if len(text) > 1024 * 1024:
            raise PodError("seeded config.json exceeds the 1 MiB pod setup limit")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PodError(f"seeded config.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise PodError("seeded config.json must contain a JSON object")

    _apply_seed_config_floor(data)
    atomic_write_at(home_fd, "config.json", json.dumps(data, indent=2), fsync=True, mode=0o600)

    try:
        os.mkdir("workspace", 0o700, dir_fd=home_fd)
    except FileExistsError:
        pass
    workspace_fd = os.open("workspace", pinned_fs.dir_flags(), dir_fd=home_fd)
    os.close(workspace_fd)


def _fixture_name_from_manifest_text(text: str) -> str:
    """Read the one package-owned scalar used as the seed completion marker."""
    for line in text.splitlines():
        if not line.startswith("fixture-name:"):
            continue
        recorded = line.partition(":")[2].strip()
        if len(recorded) >= 2 and recorded[0] == recorded[-1] and recorded[0] in "\"'":
            recorded = recorded[1:-1]
        return recorded
    return ""


def _seeded_scenario_from_fd(home_fd: int) -> str | None:
    """Return the completion marker from a pinned pod-home descriptor."""
    fd = -1
    try:
        fd = _open_seed_regular_file(home_fd, seed_mod.FIXTURE_MANIFEST)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            fd = -1
            return _fixture_name_from_manifest_text(handle.read(64 * 1024))
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def seed_home_from_scenario(cfg: PodConfig, name: str, scenario: str) -> bool:
    """Populate pod *name* from *scenario* through a pinned home descriptor.

    The final home is created/opened relative to a pinned pod-root descriptor and
    every fixture entry is copied through that held directory. Nothing is
    published by renaming a deterministic staging name, so swapping a path entry
    cannot redirect writes into another pod. The fixture manifest is copied last
    and remains the completion marker: a failed partial copy is refused on the
    next ``up`` rather than treated as a completed seed.
    """
    home_dir = cfg.home_dir(name)
    resolve_seed_scenario(scenario)

    if not pinned_fs.supports_pinned_tree_walk():
        raise PodError(
            "this host cannot pin a fixture copy to directory descriptors; "
            "refusing an unpinned pod seed"
        )

    try:
        root_fd = pinned_fs.create_and_open_dir_pinned(
            home_dir.parent,
            what="pod root",
            refusal=PodError,
        )
    except (OSError, PodError) as exc:
        if isinstance(exc, PodError):
            raise
        raise PodError(f"could not inspect or prepare pod home {home_dir}: {exc}") from exc

    home_fd = -1
    try:
        try:
            os.mkdir(home_dir.name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            home_fd = os.open(home_dir.name, pinned_fs.dir_flags(), dir_fd=root_fd)
        except OSError as exc:
            raise PodError(
                f"pod home {home_dir} is not a plain directory; refusing to seed it: {exc}"
            ) from exc
        try:
            entries = os.listdir(home_fd)
            if entries:
                recorded = _seeded_scenario_from_fd(home_fd)
                if recorded != scenario:
                    found = f"scenario {recorded!r}" if recorded else "no completion marker"
                    raise PodError(
                        f"pod home {home_dir} is populated but holds {found}, not "
                        f"requested scenario {scenario!r}; refusing to boot or "
                        "overwrite it. Run `kirocrew pod down "
                        f"{name}` before retrying the seed."
                    )
                _prepare_seeded_home_fd(home_fd)
                return False
            seed_mod.copy_fixture_into_dir_fd(scenario, home_fd)
            _prepare_seeded_home_fd(home_fd)
            seed_mod.publish_fixture_manifest(scenario, home_fd)
            held = os.fstat(home_fd)
            named = os.stat(home_dir.name, dir_fd=root_fd, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise PodError(
                    f"pod home {home_dir} changed while it was being seeded; "
                    "refusing to boot any path now present at that name"
                )
        except (SeedError, OSError) as exc:
            raise PodError(
                f"seeding pod {name!r} from scenario {scenario!r} failed: {exc}. "
                f"A partial home may remain at {home_dir}; reclaim it with "
                f"`kirocrew pod down {name}` before retrying."
            ) from exc
    finally:
        if home_fd >= 0:
            os.close(home_fd)
        os.close(root_fd)
    return True


def seeded_scenario_in_home(cfg: PodConfig, name: str) -> str | None:
    """Return the fixture name recorded in a seeded pod home, if present."""
    home = cfg.home_dir(name)
    try:
        home_fd = pinned_fs.open_dir_pinned(
            home,
            what="seeded pod home",
            refusal=PodError,
        )
    except (OSError, PodError):
        return None
    try:
        return _seeded_scenario_from_fd(home_fd)
    finally:
        os.close(home_fd)


def build_pod_env(cfg: PodConfig, home_dir: Path, port: int, checkout: Path) -> dict[str, str]:
    """Construct the isolated gateway environment for a pod.

    Scrubs messaging-identity creds so the pod can't inherit and re-use the live
    plane's Slack / WeCom / Telegram / Teams / Feishu identity via the systemd
    --user manager env: ``SLACK_*``, ``WECOM_*`` (WECOM_BOT_ID / WECOM_SECRET),
    ``MICROSOFT_APP_*``, ``FEISHU_*`` and non-AWS ``*_TOKEN`` (covers
    ``TELEGRAM_BOT_TOKEN``). Teams and Feishu each need their own prefix because
    none of ``MICROSOFT_APP_ID`` / ``MICROSOFT_APP_PASSWORD`` /
    ``MICROSOFT_APP_TENANT_ID`` / ``FEISHU_APP_ID`` / ``FEISHU_APP_SECRET`` ends
    in ``_TOKEN``, so the generic suffix rule that catches every other channel's
    bot credential passes the Azure Bot secret and the Feishu app secret straight
    through. ``AWS_*`` is kept on purpose (pods run agent turns), and the
    ``_TOKEN`` scrub deliberately EXCLUDES ``AWS_`` so ``AWS_SESSION_TOKEN`` (temp
    creds) survives intact — scrubbing it would leave half a credential and break
    every AWS call. Config-level channel enables are additionally forced off by
    ``sanitized_seed_config`` (defense-in-depth).
    """
    env = {
        **os.environ,
        "HOME": os.environ.get("HOME", str(Path.home())),
        "KIROCREW_HOME": str(home_dir),
        "KIROCREW_PORT": str(port),
        "KIROCREW_PROJECT_DIR": str(checkout),
        # Declare pod identity. A pod is ephemeral by construction — `pod down`
        # deletes this home and the checkout venv — so the agent-spec write guard
        # keys on THIS marker rather than on "has an isolated KIROCREW_HOME",
        # which would also catch a CI test gateway or a user who simply relocated
        # their data home, and wrongly stop both from writing their own specs.
        "KIROCREW_POD": "1",
        # The pod's OWN kiro user home, so its agent specs, prompts, skills and
        # chat transcripts all live under the pod instead of the machine-wide
        # ``~/.kiro``. This is what stops a pod boot from rewriting the real
        # install's specs -- and, just as importantly, stops a pod that was merely
        # BLOCKED from rewriting them falling back to the shared spec, whose env
        # pins the LIVE data home (so a pod's ``learn_add`` would have written the
        # real lessons). Safe only because every KiroCrew reader of the transcripts
        # dir now resolves through ``kiro_sessions_dir()``; without that the pod
        # would write sessions somewhere KiroCrew never looks and lose resume.
        # Inside the pod HOME so ``pod down``'s teardown reclaims it.
        "KIRO_HOME": str(home_dir / "kiro"),
        # Give the pod its OWN workspace root. Without this, `workspace_root()`
        # finds no `KIROCREW_WORKSPACE` and no `config_dir()/workspace_dir` file in
        # a fresh pod home, so it falls through to the platform default under the
        # REAL `HOME` — and every agent turn in the pod (a `chat`/`run`/`tui`
        # through `pod exec`, and the pod gateway's own sessions) would read and
        # WRITE the live workspace. `KIROCREW_WORKSPACE` is the documented override
        # (config/loader.py:220, used as-is) and `eval/runner.py` already scopes a
        # run the same way, so this is the existing mechanism rather than new
        # resolution behaviour. Placing it inside the pod HOME means `pod down`'s
        # teardown removes it with everything else.
        "KIROCREW_WORKSPACE": str(home_dir / "workspace"),
        # Pin the bind address to the SAME loopback every pod-plane client dials.
        # `health()` and `mint_token()` connect to `http://127.0.0.1:<port>`, and
        # the ownership attestation vouches for
        # "the process our sidecar names" — never for which ADDRESS that process
        # bound. An inherited KIROCREW_BIND (the official image exports
        # `0.0.0.0`; a user shell can carry `::1`) flows through the systemd
        # --user manager env into the pod gateway, which reads it in
        # dashboard/urls.py. On `::1` the gateway would serve the IPv6 loopback
        # while 127.0.0.1:<port> stays free for a foreign local process to bind
        # — a fresh, truthful attestation would then authorize a mint whose HTTP
        # request lands on the foreigner. Pinning collapses the listener and the
        # attestation onto one address; it also stops a stray `0.0.0.0` from
        # exposing a pod beyond the host.
        #
        # `pod_api()` is deliberately NOT in that list: it spells the same URL,
        # but only so the gateway's Host check sees what it would on TCP — the
        # connection itself goes over the pod's unix socket, which no bind address
        # can redirect. It needs no pin, and that is the point.
        "KIROCREW_BIND": "127.0.0.1",
        # The pod's OWN venv leads PATH, ahead of cfg.gateway_path (which starts
        # with ~/.local/bin). Without this a bare `kirocrew` inside a pod — an
        # agent bash turn, a subprocess, `_kirocrew_bin()`'s "console-script on
        # PATH" probe — resolves the machine-wide shim instead of the checkout
        # under test, so the pod silently exercises the global install and stays
        # coupled to a symlink it does not own.
        "PATH": os.pathsep.join([str(prov.venv_bin_dir(checkout)), cfg.gateway_path]),
    }
    for key in [
        k
        for k in env
        if k.startswith("SLACK_")
        or k.startswith("WECOM_")
        or k.startswith("MICROSOFT_APP_")
        or k.startswith("FEISHU_")
        or (k.endswith("_TOKEN") and not k.startswith("AWS_"))
    ]:
        env.pop(key, None)
    # Cross-plane guard: a gateway-descended caller inherits the LIVE
    # gateway's KIROCREW_BOUND_PORT (dashboard.server._export_bound_port).
    # Inside a pod env it would name the wrong plane — the pod's own
    # KIROCREW_PORT above is the target — so drop it unconditionally rather
    # than rely on resolution precedence alone.
    env.pop("KIROCREW_BOUND_PORT", None)
    return env


def write_pod_config(home_dir: Path, seed: str) -> None:
    """Ensure the pod HOME exists (owner-only) with a tunnel-disabled config.json.

    Every pod — blank or seeded — gets a config with ``tunnel.enabled=False`` so
    "never grabs the live Slack identity" is guaranteed by config, not merely by
    the absence of ``SLACK_*`` in the inherited env. The HOME dir is ``0o700`` and
    ``config.json`` is ``0o600`` — the seeded config can carry provider tokens /
    API keys, which must not be world-readable on a shared host.

    The seeded ``tunnel.enabled=False`` is NOT by itself what keeps a pod from
    publishing. This function is create-only (it returns early when ``config.json``
    already exists), and the value stays rewritable afterwards by anything that
    composes config — a provider, a migration, a hand edit. The enforcement is
    ``--no-tunnel`` on the boot argv, re-asserted at every exec. A target checkout
    whose gateway cannot parse that flag does not get the guarantee: it keeps this
    seeded value and behaves exactly as it did before the flag existed. See ``boot``.
    """
    home_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home_dir, stat.S_IRWXU)  # 0o700 owner-only (mkdir mode is umask-masked)
    # The pod's own workspace root (see build_pod_env's KIROCREW_WORKSPACE).
    # Created here so the gateway never falls back to the live workspace.
    (home_dir / "workspace").mkdir(mode=0o700, exist_ok=True)
    dst_cfg = home_dir / "config.json"
    if dst_cfg.exists():
        return
    sanitized = sanitized_seed_config(Path(seed)) if seed else None
    cfg_data = sanitized if sanitized is not None else {"tunnel": {"enabled": False}}
    # Create-only (the exists() guard above): lock the temp down before any
    # token-bearing payload reaches the published name. write_text then chmod
    # left the file at its inherited DACL until the chmod returned, and the
    # chmod itself was a no-op on Windows. atomic_write encodes UTF-8; the
    # previous write_text call did not pass encoding=.
    atomic_write(
        dst_cfg,
        json.dumps(cfg_data, indent=2),
        restrict_to_owner=True,
    )


def cleanup_home(cfg: PodConfig, name: str) -> int:
    """Delete pod *name*'s isolated HOME and report whether it is really gone.

    Routed through Python (not a raw ``rm -rf {pod_root}/<name>``) because the rm
    safety must NOT rely on a service manager's instance-name semantics: a systemd
    ``%i`` cannot contain ``/`` but CAN be ``..``, and the template unit is a
    standalone artifact that bypasses the CLI's ``validate_name``. Re-validate the
    name and confirm the target is a direct child of pod_root before deleting, so
    teardown can never escape to ``$HOME`` or a parent.

    Returns 0 only when the directory is gone afterwards. ``rmtree`` runs with
    ``ignore_errors`` — it has to, since a partially-removed tree is still progress
    — so the removal itself is silent; a tree that SURVIVES (a live process holds
    it, or recreated it in append mode right behind the delete) returns 1 and names
    what is left. Without that check a caller cannot tell a reclaimed HOME from a
    swallowed failure.
    """
    try:
        validate_name(name)
    except PodError:
        print(f"refusing pod cleanup for invalid instance name {name!r}")
        return 2
    root = cfg.pod_root.resolve()
    unresolved = cfg.pod_root / name
    # Refuse to delete THROUGH a symlink: resolving first lets a link planted
    # under pod_root pass the containment check below while the tree it names
    # lives elsewhere — including another, live pod's HOME. A real pod HOME is
    # always created as a plain directory, so a link here is never ours to follow.
    if unresolved.is_symlink():
        print(f"refusing pod cleanup: {unresolved} is a symlink, not a pod HOME")
        return 2
    target = unresolved.resolve()
    if target == root or target.parent != root:
        print(f"refusing pod cleanup: {target} is not a pod dir under {root}")
        return 2
    # Delete by the UNRESOLVED name, never the resolved target: between the
    # symlink pre-check above and this call the entry can be swapped for a
    # symlink (check-to-use race), and rmtree on the RESOLVED path would then
    # delete the live sibling the link points at. rmtree itself refuses a
    # top-level symlink, so deleting by name makes the swap harmless — nothing
    # is removed and the survivor check below reports the failure.
    shutil.rmtree(unresolved, ignore_errors=True)
    # Verify by the ENTRY itself, never the resolved target: an entry swapped
    # to a DANGLING symlink during the delete makes rmtree refuse silently
    # (suppressed by ignore_errors), and the resolved target of a dangling
    # link does not exist — so a target-existence check would report a clean
    # reclaim while the link remains as residue that orphan_homes (which
    # skips symlinks) can never surface again.
    if not os.path.lexists(unresolved):
        return 0
    if unresolved.is_symlink():
        # Swapped to a symlink mid-delete: rmtree refused it (correctly), and
        # the link itself is the residue — name it rather than the target.
        print(
            f"pod cleanup did not remove {unresolved}: the entry is now a "
            "symlink, which teardown refuses to follow — remove it by hand"
        )
        return 1
    survivors = _surviving_entries(target)
    print(
        f"pod cleanup did not fully remove {target}: still present "
        f"({', '.join(survivors)}) — either something is still writing there or "
        "the tree cannot be unlinked (permissions)"
    )
    return 1


def _surviving_entries(target: Path, limit: int = 5) -> list[str]:
    """Names of the first few entries left under a HOME that survived teardown.

    Diagnostics only: the point is to name a culprit ("security_events.jsonl")
    rather than report a bare failure, so an unlistable directory degrades to the
    directory itself instead of raising inside teardown.
    """
    try:
        names = sorted(p.name for p in target.iterdir())
    except OSError:
        return [target.name]
    if not names:
        return [f"{target.name} (empty)"]
    if len(names) > limit:
        return [*names[:limit], f"… +{len(names) - limit} more"]
    return names


def pod_context(cfg: PodConfig, name: str) -> tuple[Path, dict[str, str]]:
    """Resolve pod *name* to ``(its own kirocrew binary, its isolated env)``.

    The single seam every pod-scoped command goes through, so ``boot``,
    :func:`exec_in_pod` and ``pod env`` cannot drift apart. Notably the env comes
    from :func:`build_pod_env`, which means a command run against a pod inherits
    the SAME messaging-credential scrubbing as the pod's own gateway — a
    hand-rolled env here would silently let a throwaway instance act as the live
    Slack / WeCom / Telegram identity.

    Raises :class:`PodError` when the pod has no pinned checkout (never brought
    up from inside a checkout) or that checkout has no provisioned venv.
    """
    validate_name(name)
    checkout_str = read_env_file(cfg, name).get("CHECKOUT")
    if not checkout_str:
        raise PodError(
            f"pod {name!r} has no pinned checkout — run `kirocrew pod up {name}` "
            f"from inside a kirocrew checkout first"
        )
    checkout = Path(checkout_str).expanduser()
    bin_path = prov.venv_bin(checkout)
    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        raise PodError(f"no kirocrew venv at {bin_path} (provision {name} first)")
    env = build_pod_env(cfg, cfg.home_dir(name), derive_port(cfg, name), checkout)
    return bin_path, env


# `pod exec` forwards to a real kirocrew, so it inherits the WHOLE CLI — including
# verbs that manage the HOST rather than any one instance. This is an ALLOWLIST
# rather than a denylist because the set of host-scoped verbs is open-ended (a
# by-name denylist repeatedly missed verbs — `stop`, then `restart`, then
# `service`): every verb below acts only on `KIROCREW_HOME` state, which
# `pod exec` has already pointed at the pod.
# Anything else — present or newly added — is refused until it is deliberately
# listed, so the failure mode of drift is "temporarily unavailable" rather than
# "silently operated on the user's live machine".
#
# Deliberately EXCLUDED, with the reason each is host-scoped, not pod-scoped:
#   setup, update  — rewrite the install and the ~/.local/bin launcher
#   app            — `apps/bridges.py` edits `~/.kiro/settings/mcp.json`, the
#                    HOST registry, which is NOT covered by KIROCREW_HOME or
#                    KIRO_HOME. (The app agent JSONs it symlinks now follow
#                    `kiro_agents_dir()`, so those land under the pod's own
#                    KIRO_HOME — but the settings registry still does not, so a
#                    pod install/uninstall would still mutate host state.)
#   stop, restart  — service-aware: `cli_server._stop` short-circuits to
#                    systemctl when no explicit --port is passed, so they hit the
#                    LIVE gateway; and `restart` additionally leaves a DETACHED
#                    replacement that `pod down` cannot stop
#   service        — installs/removes the machine-wide systemd unit
#   gateway        — would race a second gateway against the pod's own unit
#   pod            — pod management from inside a pod (a nested `pod down` would
#                    tear down its own supervisor)
#   cloud          — provisions resources in the user's AWS account
#   browse         — writes browser auth state outside KIROCREW_HOME
#   manifest       — emits a Slack app manifest tied to the real identity
#   run            — `task_reporter.save_progress` writes `TASK_PROGRESS.md`
#                    "next to the spec file" (`Path(run.spec_path).parent`), so
#                    `run /host/TASK.md` writes `/host/TASK_PROGRESS.md`. An
#                    IMPLICIT write outside the pod, derived from a path the user
#                    supplied as an input rather than a destination.
#   snapshot       — its destination is CONFIGURABLE (`snapshot_dir`, or a
#                    positional dir) and `--keep N` DELETES older archives beyond
#                    N. `sanitized_seed_config` only forces tunnel/telegram/wecom
#                    off, so a pod seeded from the live config inherits the user's
#                    real backup directory — `snapshot --keep 1` from a pod would
#                    prune live backups. Destructive and cross-plane.
#   doctor         — NOT read-only: `cli_doctor.py:126` does
#                    `atomic_write(agent_path, ...)` where `agent_path` is
#                    `KIRO_AGENTS_DIR / AGENT_FILENAME` (line 405), i.e. under the
#                    real HOME. It auto-adds missing MCP servers, so a pod
#                    `doctor` rewrites the LIVE agent configuration.
#   tui, chat      — `cli_chat._tui` resolves its port as
#                    `getattr(args, "port", None) or cfg…get("port", 5476)` — the
#                    CONFIG dashboard port, falling back to literal 5476, never
#                    `KIROCREW_PORT`. A pod's config names no dashboard port, so it
#                    lands on the LIVE gateway. `chat` is excluded too because
#                    `chat --tui` branches straight into `_tui` (cli.py:1818), so
#                    excluding only `tui` left the same hole open. Every OTHER
#                    client verb (`status`, `logout`, and the credential verb) goes
#                    through `port_resolution.resolve_client_port`, which DOES honour
#                    `KIROCREW_PORT` — so this hazard is confined to `_tui`.
#   logs           — `cli_server._logs_cmd` runs `journalctl -u <SERVICE_NAME>`,
#                    the HOST service unit, so inside a pod it would show the LIVE
#                    gateway's journal while appearing to show the pod's. Wrong
#                    answer rather than damage, but a confidently wrong one.
#                    `kirocrew pod logs NAME` reads the pod's own unit.
#   mcp-*          — stdio server entrypoints, not user-facing commands
#
# `agent` and `workspace` ARE listed: both dispatchers were checked and operate
# only on `config_dir()` state (the config.json agents/workspaces maps), never on
# `~/.kiro/agents`. `workspace` is safe specifically because it asserts every
# source and destination `is_relative_to(config_dir())`; without that guard it
# would belong with `snapshot`. `restore` is listed because although its SOURCE
# archive path is arbitrary, that is a read — everything it writes lands in
# `config_dir()`.
#
# The inclusion test, stated once so it does not have to be rediscovered. A verb
# qualifies only if BOTH hold:
#   (a) every path it writes IMPLICITLY — anywhere the user did not name as a
#       destination — is inside `config_dir()`; and
#   (b) it deletes nothing outside `config_dir()`.
# An explicitly-named output destination is fine (`memory export -o FILE` writes
# where the user pointed it, no differently from shell redirection). What fails is
# an implicit write derived from an INPUT path (`run` → `TASK_PROGRESS.md` beside
# the spec), a host registry (`app`), a host service (`service`, `stop`,
# `restart`), a deletion driven by config (`snapshot --keep`), or a host-scoped
# read that merely LOOKS pod-scoped (`logs`).
_POD_SAFE_VERBS = frozenset(
    {
        "agent",
        "artifact",
        "config",
        "consolidate",
        "cron",
        "eval",
        "knowledge",
        "learn",
        "logout",
        "memory",
        "policy",
        "restore",
        "security",
        "spawn",
        "status",
        "token",
        "workspace",
    }
)

# The pod-native equivalent to suggest for the verbs users are most likely to try.
_POD_EQUIVALENT: dict[str, str] = {
    "stop": "kirocrew pod down {name}",
    "restart": "kirocrew pod down {name} && kirocrew pod up {name}",
    "gateway": "kirocrew pod up {name}",
    "logs": "kirocrew pod logs {name}",
}


def require_pod_safe_verb(argv: list[str], name: str) -> None:
    """Raise :class:`PodError` unless ``argv[0]`` is a pod-scoped verb.

    The verb must come FIRST. That is a deliberate constraint rather than a
    limitation to work around: allowing global flags ahead of it reintroduces the
    parsing ambiguity that made the previous denylist bypassable (``-v stop``, and
    worse ``--log-level DEBUG stop``, where the flag's value is indistinguishable
    from a verb). Flags AFTER the verb are untouched, so `-- status --json` works.
    """
    verb = argv[0] if argv else ""
    if verb in _POD_SAFE_VERBS:
        return
    if verb in _POD_EQUIVALENT:
        hint = f" Use `{_POD_EQUIVALENT[verb].format(name=name)}` instead."
    elif verb.startswith("-"):
        hint = " The verb must come first; put global flags after it."
    else:
        hint = (
            " Only verbs that act on the pod's own data are allowed: "
            + ", ".join(sorted(_POD_SAFE_VERBS))
            + "."
        )
    raise PodError(f"refusing `{verb or '(nothing)'}` inside `pod exec`:{hint}")


def exec_in_pod(cfg: PodConfig, name: str, argv: list[str]) -> int:
    """``exec`` *argv* as the pod's own kirocrew, in the pod's isolated env.

    Replaces the current process on success, so the child's exit status and its
    stdio (including a TTY, which matters for ``chat`` / ``tui``) reach the caller
    untouched. Returns an exit code only when the exec itself fails.
    """
    require_pod_safe_verb(argv, name)
    bin_path, env = pod_context(cfg, name)
    # Run INSIDE the pod's workspace, not the caller's cwd. Agent verbs resolve
    # relative paths against the working directory, so inheriting the invoking
    # shell's cwd (often the live checkout) would let `-- run ./TASK.md` or a file
    # edit land outside the pod even with KIROCREW_WORKSPACE set correctly.
    workspace = Path(env["KIROCREW_WORKSPACE"])
    try:
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chdir(workspace)
    except OSError as exc:
        print(f"FATAL: could not enter the pod workspace {workspace}: {exc}")
        return 70
    try:
        os.execve(str(bin_path), [str(bin_path), *argv], env)
    except OSError as exc:  # pragma: no cover - exec failure is environmental
        print(f"FATAL: could not exec {bin_path}: {exc}")
        return 70
    return 0  # unreachable on success


# --------------------------------------------------------------------------- #
# Boot — the ExecStart body. Re-entered as ``kirocrew pod _run <name>`` by the
# systemd unit. Reads the PINNED checkout (never shells git), then exec()s the
# worktree's own gateway with an isolated HOME; never returns on success.
# --------------------------------------------------------------------------- #
def target_supports_flag(checkout: Path, flag: str) -> bool:
    """Whether the gateway in *checkout* will accept *flag* on its argv.

    A pod's argv is built by the CONTROL PLANE (the template unit's ``ExecStart``
    resolves ONE kirocrew at ``pod install`` time and every instance re-enters it
    via ``%i``), but the gateway that argv reaches is the TARGET WORKTREE's own
    binary. The two are independent checkouts, so an updated control plane can
    hand a flag to a gateway too old to declare it -- argparse exits 2, and
    ``Restart=on-failure`` + ``RestartSec=5`` (the unit carries no
    ``RestartPreventExitStatus``) turns that into a restart loop every 5s rather
    than a visible failure. Dev Fleet's whole point is worktrees at different
    commits, so this is the ordinary case and not an exotic one.

    Read from the checkout's own ``cli.py`` rather than by running
    ``gateway --help``: the source IS what will execute (provisioning installs the
    checkout editable), and a subprocess on every pod boot costs an interpreter
    start for a question a string search answers.

    Unreadable source answers False -- the flag is DROPPED, not forced. Refusing
    to boot would be the fail-closed instinct, but here it is strictly worse: with
    no ``RestartPreventExitStatus`` a refusal is itself the 5s restart loop this
    exists to prevent, while dropping the flag leaves that pod at exactly the
    guarantee it had before this flag existed (the seeded ``tunnel.enabled=False``)
    -- no regression, just no improvement. The caller says so out loud.
    """
    try:
        src = (checkout / "src" / "kiro_crew" / "cli.py").read_text(encoding="utf-8")
    except OSError:
        return False
    # Comment lines do not declare anything, and a checkout that only MENTIONS the
    # flag in prose would otherwise pass the probe and then argparse-exit on it --
    # the exact restart loop this exists to prevent. The real declaration is a bare
    # quoted literal on its own line inside ``add_argument(...)``, so matching the
    # quoted form (rather than ``add_argument("--flag"``) is what keeps this working
    # against the repo's actual formatting; ``test_the_probe_accepts_this_very_repo``
    # is the ratchet that catches a refactor moving the declaration elsewhere.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if f'"{flag}"' in stripped or f"'{flag}'" in stripped:
            return True
    return False


def boot(cfg: PodConfig, name: str) -> int:
    """Boot the isolated gateway for pod *name*. Returns an exit code on failure;
    on success it ``exec``s and does not return."""
    validate_name(name)
    env_data = read_env_file(cfg, name)
    checkout_str = env_data.get("CHECKOUT")
    if not checkout_str:
        print(
            f"FATAL: pod {name!r} has no pinned checkout — run "
            f"`kirocrew pod up {name}` from inside a kirocrew checkout first"
        )
        return 3
    checkout = Path(checkout_str).expanduser()
    home_dir = cfg.home_dir(name)
    bin_path = prov.venv_bin(checkout)

    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        print(f"FATAL: no kirocrew venv at {bin_path} (provision {name} first)")
        return 3
    if not (checkout / "src" / "kiro_crew" / "static" / "dist").is_dir():
        print(f"FATAL: no built dist for {name} (build the worktree first)")
        return 3

    port = derive_port(cfg, name)
    if port == cfg.live_port:
        print(f"FATAL: derived port is the live plane :{cfg.live_port} — refusing")
        return 70

    seed = env_data.get("SEED", "")
    approval = env_data.get("APPROVAL", "")
    if approval and approval not in APPROVAL_MODES:
        # `pod up` constrains the flag, but this file is hand-editable, so an
        # unknown value can still reach here. Do NOT merely drop it: omitting
        # --approval does not mean "interactive". The gateway leaves
        # ``approval_mode`` unset, and slack/events.py falls through to
        # ``cfg.agent.approval_mode``, which config/loader.py defaults to
        # "auto" -- auto-approve every tool. Dropping would therefore be the
        # LEAST restrictive outcome. Pin interactive explicitly instead.
        print(
            f"kirocrew-pod: ignoring unknown APPROVAL={approval!r} "
            f"(expected one of: {', '.join(APPROVAL_MODES)}); "
            f"forcing --approval interactive"
        )
        approval = "interactive"

    crons_raw = env_data.get("CRONS", "")
    crons = crons_raw.strip().lower() in CRONS_TRUE
    if crons_raw and not crons:
        # Same reasoning as APPROVAL above: hand-editable file, so an
        # unrecognised value falls back to the safer setting (scheduler off)
        # instead of guessing, and the pod still boots.
        print(
            f"kirocrew-pod: ignoring unrecognised CRONS={crons_raw!r} "
            f"(expected one of: {', '.join(sorted(CRONS_TRUE))}); scheduler stays off"
        )

    # A named scenario owns the whole home and must land before the create-only
    # config writer. Directory seeds keep their existing config-only behavior.
    scenario = seed if is_scenario_ref(seed) else ""
    if scenario:
        try:
            fresh = seed_home_from_scenario(cfg, name, scenario)
        except PodError as exc:
            print(f"FATAL: {exc}")
            return 3
        print(
            f"kirocrew-pod: seeded home from scenario {scenario!r}"
            if fresh
            else f"kirocrew-pod: home already populated — scenario {scenario!r} not re-applied"
        )
    if not scenario:
        # Named scenarios finish config/workspace setup through the pinned home
        # descriptor before their completion marker is published. Directory
        # seeds keep the existing config-only path.
        write_pod_config(home_dir, seed)

    print(f"kirocrew-pod: name={name} port={port} home={home_dir} checkout={checkout}")

    pod_env = build_pod_env(cfg, home_dir, port, checkout)
    argv = ["gateway"]
    if not crons:
        argv.append("--no-crons")
    # Unconditional for any checkout that understands it, and deliberately not an
    # env key like CRONS above: a pod is a throwaway instance and must have no
    # published surface, so there is nothing for the operator to opt into.
    # ``write_pod_config`` seeds ``tunnel.enabled=False`` too, but that is a value
    # in a file it only writes ONCE (it returns early when config.json exists) and
    # anything composing config later can turn it back on — after which the pod
    # published on every boot and nothing re-asserted the guarantee. This flag is
    # re-asserted at every exec, so the seeded value is now defense in depth rather
    # than the enforcement. Reach a pod on the 127.0.0.1 port ``pod url`` prints,
    # over ``ssh -L`` from another host.
    #
    # Probed rather than assumed because this argv is built by the control plane
    # while the gateway it reaches belongs to the target worktree — see
    # ``target_supports_flag`` for why a miss must drop the flag instead of
    # refusing the boot.
    if target_supports_flag(checkout, "--no-tunnel"):
        argv.append("--no-tunnel")
    else:
        # This gateway predates the flag, so it does not RECEIVE the new guarantee:
        # it keeps the pod's seeded ``tunnel.enabled=False`` and behaves exactly as
        # it did before the flag existed. No regression, no improvement -- and
        # deliberately nothing more.
        #
        # Config is NOT re-pinned here to hand it the guarantee anyway. That was
        # tried and the premise does not hold: ``KiroCrewConfig.load()`` deep-merges
        # ``config.local.json`` OVER ``config.json`` with the overlay winning, and
        # ``kirocrew config set`` writes that overlay by default -- so pinning
        # ``config.json`` is not pinning the setting. Nor would it be sufficient if
        # it were: the gateway's enable test is an OR
        # (``cfg.tunnel.enabled or current_context().tunnel.enabled()``) and no
        # config file reaches the provider half at all.
        #
        # Refusing the boot is likewise unavailable: the refusal exit is non-zero
        # and ``Restart=on-failure`` + ``RestartSec=5`` (the unit carries no
        # ``RestartPreventExitStatus``) turns a refusal into a 5s restart loop.
        print(
            "kirocrew-pod: --no-tunnel not found in this checkout's cli.py, so the "
            "flag was not passed -- this pod keeps the tunnel behaviour it had "
            "before the flag existed. Update the checkout to get the guarantee. "
            f"Checkout: {checkout}. If that checkout DOES declare the flag, the "
            "probe has drifted -- see target_supports_flag."
        )
    if approval:
        argv += ["--approval", approval]
    os.execve(str(bin_path), [str(bin_path), *argv], pod_env)
    return 0  # unreachable on success
