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

from kiro_crew import pinned_fs
from kiro_crew import seed as seed_mod
from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.dashboard.urls import dashboard_socket_name
from kiro_crew.identity_stores import StoreMapping, store_mappings
from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import loopback_urlopen, unix_socket_urlopen
from kiro_crew.platform_compat import (
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    attributed_descendants,
    file_lock,
    find_port_listeners,
    listening_pid_tool_available,
    loopback_owner_pids,
    open_file_no_reparse,
    open_lock_file,
    pin_directory,
    process_start_time,
)
from kiro_crew.pod import launchd
from kiro_crew.pod import provision as prov
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod import windows as win_backend
from kiro_crew.pod.config import (
    EXIT_PROVISIONING,
    EXIT_REFUSED_UNRECOVERABLE,
    TERMINAL_BOOT_EXIT_CODES,
    PodConfig,
)
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
# Per-pod env file (pinned CHECKOUT= / PORT= / SEED= / APPROVAL= / CRONS= /
# EMBEDDINGS=). Values are single-quoted on write and unquoted on read; unknown
# keys are preserved on merge.
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

# Falsy spellings accepted for the ``EMBEDDINGS=`` key. Note the inverted
# polarity against ``CRONS``: embeddings are ON for every pod that says nothing,
# so this key exists to express the OFF request and an unrecognised value leaves
# them ON -- the default, hence the safer answer, by the
# same reasoning ``CRONS`` uses for its own default. ``pod up --no-embeddings``
# writes ``"0"``; the rest are accepted because the file is hand-editable.
EMBEDDINGS_FALSE: frozenset[str] = frozenset({"0", "false", "no", "off"})

#: The env var that turns the embedding-model download off, restated here rather
#: than imported because this module runs on the gateway boot path and
#: :mod:`kiro_crew.embeddings` carries the vendored llama.cpp resolution with it --
#: the same reasoning that keeps the OAuth-grant suffixes below a local literal.
#: ``test_the_skip_download_env_name_matches_the_embedder`` pins it equal to the
#: embedder's own constant, so the two cannot drift.
SKIP_MODEL_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"

#: The embedder's two model-override variables, restated for the same reason
#: and pinned to its constants by
#: ``test_the_embed_model_override_env_names_match_the_embedder``.
#: :func:`build_pod_env` drops both alongside the skip switch: that switch gates
#: only the DOWNLOAD, and :func:`kiro_crew.embeddings.resolve_custom_model` reads
#: ``KIROCREW_EMBED_MODEL_PATH`` first -- so without this a pod booted without
#: embeddings loads the operator's custom GGUF and embeds anyway.
EMBED_MODEL_OVERRIDE_ENVS: tuple[str, ...] = (
    "KIROCREW_EMBED_MODEL_PATH",
    "KIROCREW_EMBED_MODEL_URL",
)


def embeddings_disabled(env_data: dict[str, str]) -> bool:
    """Does this pod's env file ask to boot WITHOUT the embedding model?

    Takes the already-parsed mapping rather than the pod name so the two
    application points -- the gateway's own boot and every ``pod exec``, which
    reaches the env through :func:`pod_context` -- share
    one parsing rule and cannot drift into disagreeing about the same file. Both
    already read that file for ``CHECKOUT``, so this adds no second read.
    """
    return env_data.get("EMBEDDINGS", "").strip().lower() in EMBEDDINGS_FALSE


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

    THE one place an env-file string becomes a port. Every call site shares this
    single function rather than guarding itself, so the class is closed at the parse
    rather than per symptom -- a hand-rolled guard gets it wrong differently each
    time (one on length, one on a sibling it forgot to audit, one on character
    class).

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

    That last sentence is POSIX. On Windows ``SO_REUSEADDR`` means something
    closer to ``SO_REUSEPORT`` and would let this bind succeed against a LIVE
    listener, inverting the answer, so the Windows probe sets
    ``SO_EXCLUSIVEADDRUSE`` instead: a bind that fails there is a port somebody
    holds, and a bind that succeeds is one the gateway can take. The ``TIME_WAIT``
    concern that motivates ``SO_REUSEADDR`` on POSIX does not apply, because
    Winsock lets a fresh listener bind over ``TIME_WAIT`` remnants without any
    option.

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
            if IS_WINDOWS:
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            else:
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
#: hand-edits ``PORT=`` to something else does not match it and gets operator
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
    still the systemd gate with its own contract and messages, so Linux
    behaviour is provably unchanged by the macOS and Windows work — on any host
    that is neither darwin nor win32 this is exactly ``require_systemd()``.
    """
    if IS_MACOS:
        try:
            launchd.require_backend()
        except launchd.LaunchdError as exc:  # translate to the pod error type
            raise PodError(str(exc)) from exc
        return
    if IS_WINDOWS:
        try:
            win_backend.require_backend()
        except win_backend.WindowsTaskError as exc:  # translate to the pod error type
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
    if IS_WINDOWS:
        return win_backend.is_active(cfg, name)
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

    Windows has no ``exec``, so the gateway there is the wrapper's CHILD rather
    than its replacement; :func:`kiro_crew.pod.windows.supervise_gateway` records
    a pid plus its creation identity that may be the launcher ancestor of the
    process binding the port. :func:`port_owner` accounts for that shape by
    proving ownership through attributed descendants.
    """
    if IS_MACOS:
        return launchd.main_pid(cfg, name)
    if IS_WINDOWS:
        return win_backend.main_pid(cfg, name)
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
    preserved without one. Windows Task Scheduler exposes neither a restart
    counter nor a restart policy, and its status output is localized; see
    :func:`kiro_crew.pod.windows.unit_state` for the two recorded facts the
    same signal is derived from there.
    """
    if IS_MACOS:
        return launchd.unit_state(cfg, name)
    if IS_WINDOWS:
        return win_backend.unit_state(cfg, name)
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
    """Tail the pod's log — surface a boot failure's real cause.

    launchd has no journal, so on macOS this tails the files the pod's plist
    routes stdout/stderr to. Task Scheduler has none either, so on Windows this
    tails the files the generated ``.cmd`` wrapper redirects into. Same
    contract, different mechanism.
    """
    if IS_MACOS:
        return launchd.recent_journal(cfg, name, lines=lines)
    if IS_WINDOWS:
        return win_backend.recent_journal(cfg, name, lines=lines)
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
    if IS_WINDOWS:
        return win_backend.active_names(cfg)
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
    protection for direct callers): an advisory lock is per open-file-description,
    so a naive second acquisition in the same thread would deadlock against itself.

    Advisory and cooperative by design: every mutating path routes through here.
    The lock itself goes through :func:`kiro_crew.platform_compat.file_lock`, which
    is `flock` on POSIX and `msvcrt.locking` on Windows, so a pod plane on either
    platform is serialized by the same call. The fd comes from
    :func:`kiro_crew.platform_compat.open_lock_file`, which creates-or-opens
    without truncating: `open(path, "w")` truncates BEFORE any lock is held, and on
    Windows a contender then locks an already-emptied file. `file_lock` fails
    CLOSED, so a stuck holder raises instead of letting a second transaction run
    unserialized. The lock file is deliberately never deleted: unlinking one
    another process may be opening reintroduces the race the lock exists to close.
    """
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
    with open_lock_file(lock_file) as fd:
        with file_lock(fd, exclusive=True):
            held[key] = 1
            try:
                yield
            finally:
                held[key] = 0


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

        if IS_WINDOWS:
            # Same reasoning as macOS: the wrapper script and the task are both
            # re-created on every start, so neither can go stale against a moved
            # worktree, and the mutex serializes against a concurrent stop of the
            # same name whose script unlink and HOME sweep would race this write.
            return win_backend.start(cfg, name)

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
    home stripped of its sessions or config. Reclaiming after the service
    is confirmed down fixes both, at the cost of a pod that goes away without a
    ``down`` leaving its HOME behind; :func:`orphan_homes` reports those.

    Sequenced so nothing is deleted while a writer could still be alive: stop the
    service, wait for its process tree to drain, delete, then VERIFY. A HOME that
    survives is reported as a failure — never as zero residue.
    """
    with pod_name_mutex(cfg, name):
        if IS_MACOS:
            return _stop_pod_launchd(cfg, name)
        if IS_WINDOWS:
            return _stop_pod_windows(cfg, name)
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


def _stop_pod_windows(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """The Windows half of :func:`stop_pod` — called with the name mutex held.

    Structurally the launchd path, because the two platforms have the same shape
    of problem: no cgroup to drain, so the surviving-writer hazard is handled by
    sweeping a grace window rather than by observing a container empty. What
    differs is only what "confirmed stopped" means —
    :func:`kiro_crew.pod.windows.stop` proves it by watching the SUPERVISED PID
    die (and escalating to a pinned tree kill if it will not), because Task
    Scheduler's own status output is localized and its ``/End`` reaches only the
    task's own process.
    """
    # windows.stop() is authoritative: rc 0 means the gateway is confirmed gone
    # AND the task is deleted. A non-zero rc means one of those could not be
    # confirmed — in that case do NOT touch the HOME: it may belong to a live
    # gateway.
    cp = win_backend.stop(cfg, name)
    if cp.returncode != 0:
        return cp
    leftover = resolved_pod_home(cfg, name)
    # Observe the FULL window — no early exit on a clean sample. But DO exit the
    # moment the name is claimed by a NEW pod: a new `up` writes the wrapper
    # script BEFORE creating the task, so script presence is the claim marker for
    # any writer that bypasses the mutex. Deliberately a pure filesystem check,
    # for the same reason the macOS path is: probing the service manager here
    # would shell out on every sweep and break on hosts without schtasks (the
    # unit suites run this path on Linux and macOS CI).
    for _ in range(6):
        if win_backend.task_script_path(cfg, name).exists():
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
            )
        cleanup_home(cfg, name)
        time.sleep(0.5)
    if win_backend.task_script_path(cfg, name).exists():
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
        )
    cleanup_home(cfg, name)
    # The recorded boot result is per-pod state like the wrapper script, so it
    # must not outlive the pod: a stale non-zero code would make the NEXT `up` of
    # this name read as already-failed before its own boot recorded anything.
    try:
        win_backend.result_path(cfg, name).unlink(missing_ok=True)
    except OSError:
        pass
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
        # presence means the pod is installed rather than orphaned. Windows does
        # the same with its per-pod `.cmd` wrapper. systemd's template unit is
        # machine-wide, so liveness is the only signal there.
        if IS_MACOS and launchd.plist_path(cfg, p.name).exists():
            continue
        if IS_WINDOWS and win_backend.task_script_path(cfg, p.name).exists():
            continue
        out.append(p.name)
    return sorted(out)


def install_backend(cfg: PodConfig) -> tuple[str, subprocess.CompletedProcess | None]:
    """Install whatever machine-wide definition the backend needs.

    systemd needs one template unit + a daemon-reload. launchd has no template
    concept — each pod's plist is written at ``up`` — and neither does Task
    Scheduler, whose per-pod ``.cmd`` wrapper and task are both created at ``up``.
    So on both of those there is nothing to install, and saying so is better than
    writing a file that does nothing.

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
    if IS_WINDOWS:
        return (
            "nothing to install on Windows: Task Scheduler has no template tasks, "
            "so each pod's task and its .cmd wrapper are created at "
            "`kirocrew pod up <worktree>`.",
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
    start time at all. An unproven record must read as "no record", never as one
    that agrees. Windows is NOT in that last group: ``pid_start_token`` reads the
    process creation ``FILETIME`` there, which is what lets a pod on that platform
    prove ownership at all.
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

    **Windows reaches this the same way**, which it did not before pods had a
    backend there. The proof needs exactly two things, and both now answer on
    win32: ``run_marker.pid_start_token`` has a Windows leg (the process creation
    ``FILETIME``, read through a query-only handle), and :func:`main_pid` reads the
    pid ``windows.supervise_gateway`` records. Keeping the old blanket refusal here
    would not have been strictness, it would have been unsatisfiable — ``pod up``
    mints a token and :func:`mint_token` requires positive proof, so every healthy
    Windows pod would have been refused its own credential forever. Listener
    corroboration works there too (``netstat`` via ``trusted_system_bin``).

    **On Windows the recorded pid and the binding pid are different processes, and
    both are the pod.** A pip console script is an ``.exe`` launcher stub that
    starts the interpreter as a child and waits, so ``supervise_gateway`` records
    the stub while that child binds the port, and the gateway's own pid sidecar
    names that child. The Windows leg therefore reads the stub's descendants once
    (:func:`kiro_crew.platform_compat.process_descendants`) and accepts them in
    both places the proof compares pids: the sidecar's pid attests when it is the
    stub or one of its descendants, and a listener inside that tree corroborates.
    That widens the accepted set DOWNWARD only: a sidecar or a listener outside
    this pod's tree still fails exactly as before.

    **Every edge of that tree is attributed by creation order, or the widening
    would point the wrong way.** Windows never invalidates a snapshot's
    ``th32ParentProcessID`` when the parent dies, so once that number is recycled
    to the stub, processes the stub never spawned are listed beneath it — and a
    FOREIGN listener inside such a phantom subtree would be read as this pod
    holding its own port, which is the single question this function answers.
    :func:`kiro_crew.pod.windows.created_after` is the rule, shared with
    ``windows.stop`` so the two call sites cannot drift apart, and a candidate
    whose creation time cannot be read is dropped as unattributable — failing
    toward ``OWNER_FOREIGN`` / ``OWNER_UNPROVEN`` rather than toward a false claim
    of ownership.
    """
    try:
        recorded = _pod_recorded_pid(cfg, name, port)
        ours = main_pid(cfg, name)
    except Exception:
        return OWNER_UNPROVEN
    # On Windows the gateway's own sidecar names the interpreter that bound the
    # port, while ``ours`` is the launcher stub the supervisor recorded, so the
    # two agree only through the process tree. One snapshot serves both this
    # attestation and the listener corroboration below.
    #
    # The snapshot's parent pids are STALE BY DESIGN: Windows keeps a dead
    # parent's number on its children, so once that number is recycled to the
    # stub, processes it never spawned appear beneath it. Unfiltered, that is a
    # widening in the wrong direction — a FOREIGN listener sitting in such a
    # phantom subtree would be read as this pod holding its own port, which is
    # the one thing this function exists to decide. Every edge is therefore
    # attributed by creation order through the same rule ``windows.stop`` uses,
    # so a candidate that predates the stub is not this pod's child. A child
    # whose creation time cannot be read is not attributable and is dropped,
    # which fails toward OWNER_FOREIGN/UNPROVEN rather than toward a false claim
    # of ownership.
    tree: set[int] = set()
    if IS_WINDOWS and ours is not None:
        try:
            ours_token = process_start_time(ours)
            if ours_token:
                # EVERY edge attributed, not just "created after the pod's gateway".
                # This set decides whether a listener is THIS pod, and the answer
                # gates minting a credential: a stale orphan under a recycled
                # intermediate pid also postdates the gateway, so a root-only
                # comparison could call a foreign listener ours and send it the
                # pod's secret. The walk drops an unattributable child with its
                # subtree, which fails toward UNPROVEN rather than toward a false
                # claim of ownership.
                tree = set(attributed_descendants(ours, ours_token))
        except Exception:
            tree = set()
    attested = (
        recorded is not None
        and ours is not None
        and (recorded == ours or (IS_WINDOWS and recorded in tree))
    )
    verdict = OWNER_POD if attested else OWNER_UNPROVEN

    if not listening_pid_tool_available():
        return verdict
    try:
        pids = set(loopback_owner_pids(find_port_listeners(port)))
    except Exception:
        return verdict
    if not pids:
        return verdict
    if ours is not None and ours in pids:
        return verdict
    if IS_WINDOWS and pids & tree:
        # A console-script `.exe` on Windows is a launcher stub: it starts the
        # interpreter as a CHILD and waits, so the pid `supervise_gateway` records
        # is the stub while the pid that BINDS the port is that child. Both are
        # this pod's tree, and the Job object attached at spawn is what keeps the
        # tree bounded, so a descendant holding the port is the pod holding it.
        # Read from ONE process-table snapshot through the shared helper rather
        # than a new matcher, and only ever widened DOWNWARD: a pid outside the
        # recorded pid's descendants is still a foreign responder below.
        return verdict
    return OWNER_FOREIGN


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
    if IS_WINDOWS:
        # Answered BEFORE the existence check, because on this platform the file
        # is not missing -- it is never created. CPython on Windows exposes no
        # `AF_UNIX` (measured: `hasattr(socket, "AF_UNIX")` is False on 3.12
        # win32), so the pod's gateway binds no dashboard socket and no restart
        # can produce one. The generic refusal below would send the operator
        # round a diagnose-and-restart loop that cannot succeed and would reset
        # the pod's state each time, while never naming the cause or the way
        # through. There IS a way through, and it is the one the README
        # documents: mint a token and drive the loopback port yourself.
        raise PodError(
            f"`pod api` cannot reach pod {name!r} on Windows: it needs the pod's "
            "private AF_UNIX dashboard socket, and CPython on this platform has no "
            "AF_UNIX at all, so the pod binds none.\n"
            "  This is not a broken pod and a restart will not fix it.\n"
            f"  Do this instead: kirocrew pod token {name}\n"
            f"  then send your own authenticated request to 127.0.0.1:{port}.\n"
            "  `pod api` will not fall back to that port itself: the port is "
            "ordinary loopback, so a process that is not this pod can hold it, and "
            "the credential would go to whatever answered."
        )
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


def _refuse_reparse_chain(home_dir: Path) -> None:
    """Refuse when any component of *home_dir* or its parents is a reparse point.

    The win32 stand-in for the ``O_NOFOLLOW`` on every component of a pinned walk.
    Windows exposes no ``dir_fd``, so the ancestors cannot be held open one at a
    time; screening each of them for a symlink or a junction is what keeps a
    planted link from redirecting the whole pod home somewhere else. Checked from
    the drive root downward so the outermost swap is the first refusal.
    """
    for component in (*reversed(home_dir.parents), home_dir):
        if pinned_fs.is_reparse_point(component):
            raise PodError(
                f"refusing to seed pod home {home_dir}: {component} is a symbolic link "
                "or a junction, so the path does not name the directory it appears to"
            )


def _seed_home_windows(cfg: PodConfig, name: str, scenario: str, home_dir: Path) -> bool:
    """Populate pod *name* on win32, stating exactly what it does and does not prove.

    **The guarantee, and the ORDER is the guarantee.** The deepest ancestor of the
    home that already exists is screened for a reparse point and PINNED before
    anything is created, and every level below it is created under the pin of the
    level above; the home itself is then created by this process and a handle plus
    an :func:`pinned_fs.fd_real_path` witness is taken on it -- the kernel's own
    name for the inode now held open, which carries no link component left to
    swap. Both paths run under that pin, the fresh seed and the
    already-seeded restart. Each fixture file is copied from a pinned source
    descriptor (``copy_file_pinned``'s ``src_fd`` form, the only pinned source form
    on this platform) into an ``O_CREAT | O_EXCL`` destination under that witnessed
    home, so nothing existing is ever overwritten. The witness is re-read before the
    completion manifest is published, so a home that changed identity mid-seed is
    refused instead of booted, and the manifest stays the last write -- a partial
    tree carries no completion marker and is refused on the next ``up``.

    **The residual, named rather than implied.** The held handle is opened without
    ``FILE_SHARE_DELETE`` (``platform_compat.pin_directory``), so from the moment
    the anchor pin is taken the home and every directory above it can be neither
    renamed nor deleted; an ancestor swap after that point is refused by the
    kernel, not merely detected. Before it, the anchor is still LOCATED by name,
    and no witness closes that -- a witness taken before the pin can only be
    compared against itself. What also stays open is the home's CONTENTS: each
    fixture entry is reached by name under the pinned home, so a process running as
    this same user could plant a reparse point at a not-yet-written child name
    inside that window. ``O_EXCL`` refuses an entry that already exists, which
    covers a planted file or link at a leaf, and the ``_prepare_seeded_home_dir``
    screen covers the two subdirectories the seed creates. Windows offers no
    ``dir_fd`` to close the rest of that window, and a pod home lives under a plane
    root only this user can write, so the residual is exactly the trust domain the
    OS already grants that user -- the same boundary ``pod/README.md`` records for
    pod isolation generally.

    Returns True when this call seeded the home, False when a complete seed for the
    requested scenario is already present -- the same contract as the POSIX branch.
    """
    _refuse_reparse_chain(home_dir)
    fds: list[int] = []
    home_fd = -1
    try:
        try:
            # Pin BEFORE creating, for the reason
            # ``_pin_outermost_existing_windows`` documents: the old order walked
            # the ancestors with ``mkdir(parents=True)`` and took its first pin
            # only at the home itself, so an ancestor swapped in that window had
            # the whole fixture -- and the completion marker that certifies it --
            # published inside the swapped tree, and this function returned True.
            anchor_fd, anchor = _pin_outermost_existing_windows(home_dir, what="the pod home")
            fds.append(anchor_fd)
            target = anchor
            for part in home_dir.relative_to(anchor).parts[:-1]:
                target = target / part
                fds.append(_pin_created_dir_windows(fds[-1], target, what=f"pod root {part}"))
        except OSError as exc:
            raise PodError(f"could not prepare pod root {home_dir.parent}: {exc}") from exc

        # The LAST level is created here rather than through
        # ``_pin_created_dir_windows`` because this branch has to know whether IT
        # created the home: that helper's ``mkdir(exist_ok=True)`` would erase the
        # fresh / already-seeded distinction the restart path below depends on. The
        # screen it would have done is done here instead, and the parent is pinned
        # either way, so the ordering guarantee is unchanged.
        if pinned_fs.is_reparse_point(home_dir):
            raise PodError(
                f"refusing to seed pod home {home_dir}: it is a symbolic link or a "
                "junction, so the path does not name the directory it appears to"
            )
        fresh = True
        try:
            home_dir.mkdir(mode=0o700)
        except FileExistsError:
            fresh = False
        except OSError as exc:
            raise PodError(f"could not create pod home {home_dir}: {exc}") from exc

        # Pinned and witnessed for BOTH paths, including the restart path below.
        # Reading the completion marker and re-preparing the home purely by name
        # would let a pod that has been seeded once be restarted out of a directory
        # swapped in since, which is the one path where nothing at all is held.
        #
        # TRANSLATED in the same operation that takes the pin, exactly as the POSIX
        # branch translates its ``os.open(..., dir_flags())``: ``pin_directory``
        # refuses anything that is not a plain directory -- a stale FILE left at the
        # home name, or a reparse point planted between the screen above and this
        # open -- and it does so with ``NotADirectoryError``. ``boot`` and the
        # scenario call site catch only ``PodError``, so an untranslated one escapes
        # as an UNINSTRUMENTED crash: no FATAL line, no recorded refusal, no terminal
        # exit code. Do NOT pre-screen with ``home_dir.is_dir()`` instead -- that is a
        # by-name stat which FOLLOWS a reparse point, so it passes the adversarial
        # case this open exists to refuse, and it re-opens the check/use window the
        # pin closes.
        try:
            home_fd = pin_directory(home_dir)
        except OSError as exc:
            raise PodError(
                f"pod home {home_dir} is not a plain directory; refusing to seed it: {exc}"
            ) from exc
        witness = pinned_fs.fd_real_path(home_fd)
        if not witness:
            # Fail CLOSED: with no witness there is nothing to validate the writes
            # against, which is the one thing that makes a by-name destination
            # acceptable here at all.
            raise PodError(
                f"could not read the real path of pod home {home_dir} from its own "
                "handle, so a seed written by name cannot be validated; refusing"
            )

        # ONE translation frame over BOTH paths, like the POSIX branch's single
        # ``try`` around its restart and fresh-seed blocks: an OSError from the
        # restart path (``iterdir``, ``_prepare_seeded_home_dir``) is a refusal in
        # exactly the same sense as one from the copy, and only ``PodError`` survives
        # this frame.
        try:
            if not fresh:
                # Same fresh / already-populated split the POSIX branch makes: a home
                # that already carries THIS scenario's completion marker is restarted
                # unchanged, and anything else is refused rather than overwritten.
                if not home_dir.is_dir():
                    raise PodError(
                        f"pod home {home_dir} is not a plain directory; refusing to seed it"
                    )
                if any(home_dir.iterdir()):
                    recorded = _seeded_scenario_in_dir(home_dir)
                    if recorded != scenario:
                        found = f"scenario {recorded!r}" if recorded else "no completion marker"
                        raise PodError(
                            f"pod home {home_dir} is populated but holds {found}, not "
                            f"requested scenario {scenario!r}; refusing to boot or overwrite "
                            f"it. Run `kirocrew pod down {name}` before retrying the seed."
                        )
                    _prepare_seeded_home_dir(home_dir)
                    if pinned_fs.fd_real_path(home_fd) != witness:
                        raise PodError(
                            f"pod home {home_dir} changed while it was being prepared for "
                            "restart; refusing to boot any path now present at that name"
                        )
                    return False

            seed_mod.copy_fixture_into_witnessed_dir(scenario, home_dir)
            _prepare_seeded_home_dir(home_dir)
            if pinned_fs.fd_real_path(home_fd) != witness:
                raise PodError(
                    f"pod home {home_dir} changed while it was being seeded; "
                    "refusing to boot any path now present at that name"
                )
            seed_mod.publish_fixture_manifest_into_witnessed_dir(scenario, home_dir)
        except (SeedError, OSError) as exc:
            # The frame now covers the RESTART path too, where the home is a complete
            # one this seed did not write, so "a partial home may remain" would be
            # false there -- and `pod down` DELETES the home, which is the wrong
            # advice for a transient failure over state worth keeping.
            raise PodError(
                f"seeding pod {name!r} from scenario {scenario!r} failed: {exc}. "
                f"If the home at {home_dir} is incomplete, reclaim it with "
                f"`kirocrew pod down {name}` before retrying -- a restart failure "
                "leaves the existing home intact, so retry that before nuking it."
            ) from exc
    finally:
        if home_fd >= 0:
            os.close(home_fd)
        pinned_fs.close_all(fds)
    return True


def _seeded_scenario_in_dir(home_dir: Path) -> str | None:
    """Return the completion marker from a caller-created pod home, by name.

    The win32 twin of :func:`_seeded_scenario_from_fd`. It reads a marker this
    process wrote under a directory it created, and a link at the marker's own name
    is refused rather than followed.
    """
    marker = home_dir / seed_mod.FIXTURE_MANIFEST
    if pinned_fs.is_reparse_point(marker):
        return None
    try:
        return _fixture_name_from_manifest_text(marker.read_text(errors="replace")[: 64 * 1024])
    except OSError:
        return None


def _prepare_seeded_home_dir(home_dir: Path) -> None:
    """Finish pod-owned setup on win32, under a home this process created.

    The win32 twin of :func:`_prepare_seeded_home_fd`. Every path it touches is a
    direct child of the caller-witnessed home, and each is screened for a reparse
    point before it is read or written, because a by-name read that follows a link
    would import config from outside the pod.
    """
    config = home_dir / "config.json"
    if pinned_fs.is_reparse_point(config):
        raise PodError(f"seeded config.json is a link; refusing to read it: {config}")
    data: dict = {}
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise PodError(f"could not read seeded config.json: {exc}") from exc
        if len(text) > 1024 * 1024:
            raise PodError("seeded config.json exceeds the 1 MiB pod setup limit")
        try:
            loaded = json.loads(text)
        except ValueError as exc:
            raise PodError(f"seeded config.json is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise PodError("seeded config.json must contain a JSON object")
        data = loaded

    _apply_seed_config_floor(data)
    atomic_write(config, json.dumps(data, indent=2), fsync=True, mode=0o600)

    workspace = home_dir / "workspace"
    if pinned_fs.is_reparse_point(workspace):
        raise PodError(f"seeded workspace is a link; refusing to use it: {workspace}")
    workspace.mkdir(mode=0o700, exist_ok=True)


def seed_home_from_scenario(cfg: PodConfig, name: str, scenario: str) -> bool:
    """Populate pod *name* from *scenario* through a pinned home descriptor.

    The final home is created/opened relative to a pinned pod-root descriptor and
    every fixture entry is copied through that held directory. Nothing is
    published by renaming a deterministic staging name, so swapping a path entry
    cannot redirect writes into another pod. The fixture manifest is copied last
    and remains the completion marker: a failed partial copy is refused on the
    next ``up`` rather than treated as a completed seed.

    Windows reaches :func:`_seed_home_windows` instead, which states its own
    weaker-but-named guarantee: that platform has no ``dir_fd``, so a destination
    cannot be addressed relative to a held descriptor and the pinned walk this
    branch performs does not exist there. Every OTHER host without a pinned tree
    walk keeps the refusal below.
    """
    home_dir = cfg.home_dir(name)
    resolve_seed_scenario(scenario)

    if not pinned_fs.supports_pinned_tree_walk():
        if IS_WINDOWS:
            return _seed_home_windows(cfg, name, scenario, home_dir)
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
            # Same wording as the win32 twin, and for the same reason: this frame
            # also covers the RESTART path above, where the home is a complete one
            # this seed did not write, so "a partial home may remain" would be false
            # -- and `pod down` DELETES the home, which is the wrong advice for a
            # transient failure over state worth keeping.
            raise PodError(
                f"seeding pod {name!r} from scenario {scenario!r} failed: {exc}. "
                f"If the home at {home_dir} is incomplete, reclaim it with "
                f"`kirocrew pod down {name}` before retrying -- a restart failure "
                "leaves the existing home intact, so retry that before nuking it."
            ) from exc
    finally:
        if home_fd >= 0:
            os.close(home_fd)
        os.close(root_fd)
    return True


def seeded_scenario_in_home(cfg: PodConfig, name: str) -> str | None:
    """Return the fixture name recorded in a seeded pod home, if present.

    On win32 the home is held through :func:`platform_compat.pin_directory` for
    the read (no rename or delete of it or its ancestors while the handle lives,
    and a reparse point at its name is refused by the open itself) and the marker
    is read by name under it through :func:`_seeded_scenario_in_dir`, the same
    pair the win32 seed branch writes with. Every other platform reads the marker
    through the pinned home descriptor.
    """
    home = cfg.home_dir(name)
    if IS_WINDOWS:
        try:
            home_fd = pin_directory(home)
        except OSError:
            return None
        try:
            return _seeded_scenario_in_dir(home)
        finally:
            os.close(home_fd)
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


def build_pod_env(
    cfg: PodConfig,
    home_dir: Path,
    port: int,
    checkout: Path,
    *,
    skip_model_download: bool = False,
) -> dict[str, str]:
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

    ``KIROCREW_OS_HOME`` points the pod's own :mod:`kiro_crew.mcp_grant` reads
    (mint, status, disconnect, mcp_discovery's remote probe -- all resolved
    through ``config.paths.kiro_oauth_cache_home``) at a dedicated
    ``<home_dir>/os-home`` tree INSTEAD of the real host home. Without this a
    pod's gateway process stats and unlinks MCP OAuth grant artifacts under the
    REAL ``~/.aws/sso/cache`` -- so a Connections card in the pod reads
    "Connected" from a grant the operator minted on the real machine, and a
    grant minted inside the pod is a real, durable machine-level credential
    that OUTLIVES ``pod down``. This directory is nested INSIDE ``home_dir`` so
    ``cleanup_home``'s teardown reclaims it with everything else. It holds no
    secret by itself -- see ``_seed_pod_os_home`` for what is staged into it,
    and ``acp/client.py`` / ``acp/runtime.py`` for the matching ``HOME`` remap
    on the pod's OWN kiro-cli children, which is what makes kiro-cli's writes
    land in this same tree.

    ``skip_model_download`` boots the pod in the documented no-embedding-model
    mode by exporting ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` into THIS env only. It is
    a value in the returned mapping, never a write to the operator's shell,
    profile or real data home -- the whole point is that a load test can run
    embedding-free without the host losing its own model. The pod then serves
    memory and knowledge search through the keyword fallback, which is a
    supported mode rather than a broken one, so the instance stays usable.

    Deliberately the existing skip-download switch rather than a
    ``KIROCREW_EMBED_MODEL_URL`` pointed at an unreachable host. That spelling
    reaches the same end state only after the downloader has spent its full
    attempt budget on HTTPS requests to a host chosen to fail, and it carries a
    live footgun: :func:`kiro_crew.embeddings._resolve_model_url` IGNORES any
    override that is not ``https://`` and falls back to the real CDN, so one
    malformed sentinel downloads the very model the option exists to avoid.
    """
    os_home = home_dir / "os-home"
    env = {
        **os.environ,
        "HOME": os.environ.get("HOME", str(Path.home())),
        "KIROCREW_HOME": str(home_dir),
        "KIROCREW_OS_HOME": str(os_home),
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
    if skip_model_download:
        # Set AFTER the scrub loop so no present-or-future scrub pattern can strip
        # the guarantee back out.
        env[SKIP_MODEL_DOWNLOAD_ENV] = "1"
        # The switch gates only the DOWNLOAD (embeddings.ensure_model). An inherited
        # KIROCREW_EMBED_MODEL_PATH is read FIRST by resolve_custom_model, so with it
        # in place the pod would load the operator's custom GGUF and embed while the
        # journal says it does not -- a load measurement wrong in exactly the
        # direction this option exists to control. Drop every embed-model override
        # with the switch so the returned mapping describes one mode. The URL
        # override is moot once nothing downloads; it goes too so the mapping cannot
        # say two things at once.
        for key in EMBED_MODEL_OVERRIDE_ENVS:
            env.pop(key, None)
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
    _ensure_pod_dir(home_dir, what="pod home")
    # The pod's own workspace root (see build_pod_env's KIROCREW_WORKSPACE).
    # Created here so the gateway never falls back to the live workspace.
    _ensure_pod_dir(home_dir / "workspace", what="pod workspace")
    dst_cfg = home_dir / "config.json"
    # The create-only guard is an LSTAT, not ``exists()``. ``exists()`` follows a
    # link, so a link planted at ``config.json`` pointing at any existing host file
    # read as "already configured" and the pod booted on the attacker's file. A link
    # here is refused outright rather than treated as either absent or present.
    existing = os.stat(dst_cfg, follow_symlinks=False) if dst_cfg.is_symlink() else None
    if existing is not None:
        raise PodError(f"refusing to seed pod config: {dst_cfg} is a symbolic link")
    if dst_cfg.exists():
        return
    sanitized = sanitized_seed_config(Path(seed)) if seed else None
    cfg_data = sanitized if sanitized is not None else {"tunnel": {"enabled": False}}
    # Create-only (the guard above): lock the temp down before any token-bearing
    # payload reaches the published name. write_text then chmod left the file at its
    # inherited DACL until the chmod returned, and the chmod itself was a no-op on
    # Windows -- which is why this stays ``atomic_write(restrict_to_owner=True)``
    # rather than moving to the pinned publisher: that helper applies a POSIX mode,
    # and this file carries provider tokens on every platform. The link the pinned
    # path would have refused is refused by the lstat above instead.
    atomic_write(
        dst_cfg,
        json.dumps(cfg_data, indent=2),
        restrict_to_owner=True,
    )


# Suffixes of the two-file MCP OAuth grant PAIRS under ``.aws/sso/cache``, which
# :mod:`kiro_crew.mcp_grant` owns (``<sha256>.token.json`` /
# ``<sha256>.registration.json``). These are the ONLY names the seeding below
# refuses. They are per-server CREDENTIALS a Connect click mints: copying one
# forward would let a pod boot already "Connected" to a provider nobody consented
# to from inside it, and copying one back at teardown would leave a real grant on
# the host after the pod that minted it is gone. Restated here rather than
# imported at module scope because this runs on the gateway boot path; the values
# are asserted against ``mcp_grant``'s own constants by test.
#: The two-file MCP OAuth grant PAIRS (``<sha256>.token.json`` /
#: ``<sha256>.registration.json``) that :mod:`kiro_crew.mcp_grant` owns are the
#: ONLY thing a pod's ``.aws/sso/cache`` ever holds: the pod's own kiro-cli mints
#: them there and ``pod down`` reclaims them. Nothing is copied in from the host,
#: so no host bearer token exists in that tree for an agent shell to read.


def _runtime_auth_store_mappings() -> tuple[StoreMapping, ...]:
    """Source->staged mappings for the agent runtime's own identity stores.

    ``test_the_agent_runtime_auth_stores_stay_visible`` pins these OUT of every
    masking tier for a stated reason: "the agent runtime is itself spawned inside
    this sandbox and resolves its own access token from that store, so masking it
    would break the agent's model auth". Not masking it is only half the
    requirement -- under the pod's remapped ``HOME`` the store has to EXIST there
    too, which is what this staging supplies. Without it the child reaches
    kiro-cli's own login gate no matter what else the pod home contains.

    DERIVED from ``identity_stores.store_mappings`` rather than a hardcoded list,
    the same authoritative-table discipline ``acp.client``'s env scrub uses. An
    earlier revision hardcoded the two POSIX ``.local/share`` paths, so a macOS
    host (``~/Library/Application Support/...``) or a host with a redirected
    ``XDG_DATA_HOME`` staged NOTHING -- and the viability probe then ACCEPTED the
    resulting signed-out pod, because signed-out is a legitimate boot state. The
    table follows the env override on the SOURCE side and keeps the fixed default
    layout on the staged side, which is exactly what a pod needs: read from
    wherever the operator's store really is, write where the child will look.
    """
    return store_mappings(sys.platform, Path.home(), os.environ)


#: Runaway guard on ONE store's staging. The runtime's own identity store is small
#: by construction, so a tree that exceeds this is a sign the layout changed rather
#: than a case to serve; staging stops and the pod boots signed-out (loudly, via the
#: viability probe) instead of copying an unbounded tree on every boot.
_RUNTIME_AUTH_STORE_FILE_CAP = 512

#: Filename suffixes that make a staged file a SQLite DATABASE rather than bytes to
#: copy. Snapshotted through the backup API (see :func:`_snapshot_sqlite_pinned`), so
#: a live writer cannot hand the pod a torn generation.
_SQLITE_SUFFIXES: tuple[str, ...] = (".sqlite3", ".sqlite")

#: Sidecars a SQLite database keeps beside itself. NEVER staged: a backup already
#: contains every committed transaction they hold, and copying them alongside a
#: separately-copied main file is precisely what produced a mismatched set.
_SQLITE_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")


def _is_sqlite_sidecar(name: str) -> bool:
    """Is *name* a sidecar of a database this staging snapshots instead of copying?"""
    for sidecar in _SQLITE_SIDECAR_SUFFIXES:
        if name.endswith(sidecar):
            stem = name[: -len(sidecar)]
            if any(stem.endswith(suffix) for suffix in _SQLITE_SUFFIXES):
                return True
    return False


def _snapshot_sqlite_pinned(
    *,
    src_dir_fd: int,
    src_name: str,
    dst_dir_fd: int,
    dst_name: str,
) -> bool:
    """Stage ONE SQLite database as a consistent snapshot. True when it landed.

    **Why not a byte copy.** The host store belongs to a LIVE kiro-cli, and a
    WAL-mode database is a SET of files whose contents only agree at an instant.
    Copying ``data.sqlite3`` and then its ``-wal`` with two separate reads takes
    those files at two different times, so a checkpoint landing in between yields a
    main file from after it and a WAL from before -- a torn snapshot whose identity
    rows are missing or malformed, staged into the pod as if it were sign-in state.
    Nothing in a per-file copy loop can close that window, because the window is
    between the copies. SQLite's backup API reads the database through the engine
    under a read transaction, so what it writes is one generation by construction,
    and the sidecars need not be staged at all -- the result already contains every
    committed transaction they held.

    **How the pinned discipline survives an API that takes a path.** ``sqlite3``
    opens by NAME, which is the one thing this module refuses to do on a tree it
    does not own. The bridge is :func:`pinned_fs.fd_real_path`: both ends are opened
    as descriptors FIRST (source ``O_NOFOLLOW`` relative to the already-validated
    ``src_dir_fd``; destination ``O_CREAT | O_EXCL`` relative to ``dst_dir_fd``, so
    anything sitting at that name is a plant and creation refuses it rather than
    following it), and the path handed to ``sqlite3`` is then the KERNEL's own name
    for the inode already held open. That name has no symlink component left to
    swap, which is the property the descriptor discipline exists to get. Both opens
    fail closed: no descriptor, or no readable real path, and the store is refused.

    The mode is set with ``fchmod`` on the created descriptor -- before any bytes
    are written and on the fd rather than the name -- so the file is never briefly
    group-readable and the mode cannot land on some other inode.

    **Temp-then-rename, both inside the pinned destination directory.** A backup is
    not atomic, so an interrupted one leaves a short database that looks staged. The
    snapshot is built at a temp name and ``os.rename``d onto the real one with BOTH
    ``src_dir_fd`` and ``dst_dir_fd`` pinned to the same validated directory, so the
    name a reader can see either does not exist or is a complete snapshot, and the
    rename cannot be redirected out of the directory it was checked in. A leftover
    temp from a killed boot is cleaned up on the next attempt: it is created
    ``O_EXCL``, so a stale one is unlinked through the pinned fd first.

    Read-only on the source (``mode=ro``), so staging a pod can never write to the
    operator's live store.
    """
    import sqlite3

    tmp_name = f".{dst_name}.staging"
    src_fd: int | None = None
    dst_fd: int | None = None
    try:
        try:
            src_fd = os.open(
                src_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=src_dir_fd,
            )
        except OSError:
            return False
        if not stat.S_ISREG(os.fstat(src_fd).st_mode):
            return False
        src_real = pinned_fs.fd_real_path(src_fd)
        if not src_real:
            return False
        # A stale temp from an interrupted boot would defeat O_EXCL below. Removed
        # through the pinned fd, so the unlink cannot escape this directory.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dst_dir_fd)
        try:
            dst_fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
        except OSError:
            return False
        os.fchmod(dst_fd, 0o600)
        dst_real = pinned_fs.fd_real_path(dst_fd)
        if not dst_real:
            return False
        src_uri = f"file:{urllib.parse.quote(src_real)}?mode=ro"
        with contextlib.closing(sqlite3.connect(src_uri, uri=True)) as source:
            with contextlib.closing(sqlite3.connect(dst_real)) as target:
                source.backup(target)
        os.rename(tmp_name, dst_name, src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)
        return True
    except (OSError, sqlite3.Error):
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dst_dir_fd)
        return False
    finally:
        for fd in (src_fd, dst_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


def _stage_runtime_auth_store(os_home: Path, mapping: StoreMapping) -> int:
    """Mirror one HOME-relative runtime auth store into *os_home*. Best-effort.

    Same discipline as the SSO-cache staging above: every directory is created
    through a PINNED no-follow descriptor so a link planted at any component cannot
    redirect the copy, every level is forced to ``0o700`` and every file to
    ``0o600``, and copies are create-only so a pod that already refreshed its own
    credential is not clobbered.

    A SQLite database is SNAPSHOTTED rather than copied, and its ``-wal`` / ``-shm``
    / ``-journal`` sidecars are skipped entirely -- see
    :func:`_snapshot_sqlite_pinned` for why a per-file copy of a live database's
    file set cannot be consistent. Databases in a directory are handled BEFORE its
    plain files, and a database that cannot be snapshotted REFUSES THE WHOLE STORE
    (returns 0): the token is what the store is for, so a tree staged without it
    would present a pod as provisioned while every agent turn failed to sign in.
    Because ``os.walk`` is top-down and the database sits at the store root, that
    refusal lands before anything has been staged rather than half-way through.

    Returns the number of files staged. Never raises: a missing or unreadable host
    store just means the pod boots signed-out, which the boot-time viability probe
    reports. The whole tree is mirrored rather than a chosen filename, because this
    store's internal layout is the runtime's own contract (kiro-cli resolves its
    token from it through the ``dirs`` crate) and guessing a name is what made the
    SSO-cache staging a silent no-op in an earlier revision.
    """
    source_root = mapping.source
    parts = mapping.staged_relative.parts
    staged = 0
    if not pinned_fs.supports_pinned_walk():
        # No ``O_DIRECTORY``/``O_NOFOLLOW``/``dir_fd`` on this platform (Windows).
        # Every write below goes through a pinned no-follow descriptor precisely
        # because it moves sign-in material, so there is no by-name fallback to
        # degrade to -- see ``pinned_fs.supports_pinned_walk``. Callers inside
        # ``_seed_pod_os_home`` are already past that platform refusal; this guard
        # is what makes the helper safe to call (and to unit-test) directly.
        return 0
    fds: list[int] = []
    try:
        try:
            source_root_fd = pinned_fs.open_dir_pinned(
                source_root, what=f"host {parts[-1]} identity store", refusal=PodError
            )
        except (OSError, PodError):
            return 0  # no such store on this host; nothing to mirror
        fds.append(source_root_fd)
        for dirpath, dirnames, filenames in os.walk(source_root):
            rel = Path(dirpath).relative_to(source_root)
            # Recreate this level under the pod home, pinned at every component.
            target = os_home
            try:
                for label in (*parts, *rel.parts):
                    target = target / label
                    level_fd = pinned_fs.create_and_open_dir_pinned(
                        target, what=f"pod auth store {label}", refusal=PodError
                    )
                    fds.append(level_fd)
                    os.fchmod(level_fd, stat.S_IRWXU)
            except (OSError, PodError):
                dirnames[:] = []  # this subtree is unsafe or unwritable; skip it
                continue
            dst_dir_fd = fds[-1]
            try:
                src_dir_fd = pinned_fs.open_dir_pinned(
                    Path(dirpath), what=f"host {parts[-1]} identity store", refusal=PodError
                )
            except (OSError, PodError):
                dirnames[:] = []
                continue
            fds.append(src_dir_fd)
            # Databases first, so a refusal happens before anything is staged.
            names = sorted(filenames)
            databases = [n for n in names if n.endswith(_SQLITE_SUFFIXES)]
            for name in databases:
                if pinned_fs.stat_at(dst_dir_fd, name) is not None:
                    continue  # create-only, exactly like the copy path
                if not _snapshot_sqlite_pinned(
                    src_dir_fd=src_dir_fd,
                    src_name=name,
                    dst_dir_fd=dst_dir_fd,
                    dst_name=name,
                ):
                    print(
                        f"kirocrew-pod: refusing auth store {'/'.join(parts)}: "
                        f"{name} could not be snapshotted consistently"
                    )
                    return 0
                staged += 1
            for name in names:
                if name in databases or _is_sqlite_sidecar(name):
                    continue
                if staged >= _RUNTIME_AUTH_STORE_FILE_CAP:
                    print(
                        f"kirocrew-pod: auth store {'/'.join(parts)} exceeded "
                        f"{_RUNTIME_AUTH_STORE_FILE_CAP} files; staging stopped"
                    )
                    return staged
                try:
                    pinned_fs.copy_file_pinned(
                        str(Path(dirpath) / name),
                        dir_fd=src_dir_fd,
                        name=name,
                        dst_dir_fd=dst_dir_fd,
                        dst_name=name,
                        skip_existing=True,
                        force_mode=0o600,
                    )
                except OSError:
                    continue
                staged += 1
    finally:
        pinned_fs.close_all(fds)
    return staged


def _refuse(cfg: PodConfig, name: str, code: int, reason: str) -> int:
    """Print a FATAL for *reason*, record it, and return the terminal *code*.

    Every terminal exit goes through here so the record and the exit cannot drift.
    They did drift once: ``_run_internal`` prints "recorded at <path>" whenever it
    translates a terminal code for launchd, but only the OS-home refusal wrote the
    file, so the provisioning (3) and live-port (70) exits named a path that did
    not exist (found in review). Those two are translated to 0 on macOS exactly
    like 78 is, so they have the same legibility problem and need the same record.
    """
    print(f"FATAL: {reason}")
    _record_refusal(cfg, name, reason)
    return code


def terminal_exit_code(cfg: PodConfig, name: str, code: int) -> int:
    """The exit status to hand the SERVICE MANAGER for *code*.

    The record-conditional wrapper around :func:`kiro_crew.pod.launchd.launchd_exit_code`,
    and the ONLY translation callers should use. ``launchd_exit_code`` states the
    platform semantics (launchd restarts on non-zero, so a terminal refusal has to
    exit 0 or it loops every ``ThrottleInterval``); this adds the condition that
    makes exiting 0 honest.

    **Translating unconditionally was a real hole.** Exit 0 tells launchd the boot
    ended cleanly, and the only thing that keeps a refusal legible after that is
    the host-side note. If the note failed to land -- the write refused a planted
    link, the directory was unusable, the disk was full -- then translating anyway
    produces a pod that looks cleanly stopped with NO record anywhere of why, which
    is strictly worse than the restart loop the translation exists to prevent. So a
    terminal code is translated only when :func:`refusal_reason` confirms the record
    is actually readable; otherwise the honest non-zero survives and launchd's
    retry, noisy as it is, at least keeps the failure visible.

    Non-terminal codes and 0 pass through untouched on every platform, so ordinary
    crash recovery is unaffected.
    """
    if code not in TERMINAL_BOOT_EXIT_CODES:
        return code
    if not IS_MACOS:
        # systemd exempts these codes via RestartPreventExitStatus, so the honest
        # code is also the non-looping one there. Windows Task Scheduler has no
        # restart policy at all -- a task whose action exits non-zero is recorded
        # with that result and stays down -- so the honest code is already
        # terminal, and `windows.unit_state` reads that recorded result as the
        # crash signal. Nothing to translate on either.
        return code
    if refusal_reason(cfg, name) is None:
        print(
            f"kirocrew-pod: keeping exit {code} — the refusal could not be recorded at "
            f"{cfg.refusal_file(name)}, so exiting 0 would hide it entirely"
        )
        return code
    return launchd.launchd_exit_code(code)


def _close_fd(fd: int) -> None:
    """Close *fd*, ignoring an already-closed descriptor."""
    try:
        os.close(fd)
    except OSError:
        pass


def _ensure_pod_dir(target: Path, *, what: str) -> None:
    """Create-if-absent *target* as an owner-only directory, refusing planted links.

    THE directory half of the boot path's write hardening, and the companion to
    :func:`pinned_fs.write_file_pinned`. Returns nothing on purpose: an earlier
    revision handed the caller a raw descriptor to ``fchmod``, which made the mode
    the caller's problem and does not exist on the platform where ``fchmod`` is a
    no-op. The mode is applied here, through the descriptor where there is one.

    The ANCESTOR chain is created by name first, deliberately: ``pinned_fs``
    creates only the final component, and the chain above a pod home is the pod
    ROOT (``~/.kiro/crew/pods`` by default), which is host-owned state this module
    already creates by name in two other places. What is agent-influenced is the
    pod's own directory and everything under it, and that is what gets pinned.

    Platform split matches :func:`pinned_fs.write_file_pinned` exactly -- pinned
    create plus ``fchmod`` where :func:`pinned_fs.supports_pinned_walk` holds, and
    elsewhere the ``lstat`` link refusal (the everywhere-floor) plus a by-name
    ``mkdir``. Raises :class:`PodError` on a planted link or an unusable component;
    ``boot`` converts that into a recorded terminal refusal.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PodError(f"could not create the parent of {what} {target}: {exc}") from exc
    if not pinned_fs.supports_pinned_walk():
        existing = pinned_fs.lstat_by_name(target)
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise PodError(f"refusing to use {what} {target}: it is a symbolic link")
        try:
            target.mkdir(mode=0o700, exist_ok=True)
            os.chmod(target, stat.S_IRWXU)
        except OSError as exc:
            raise PodError(f"could not prepare {what} {target}: {exc}") from exc
        return
    fd = pinned_fs.create_and_open_dir_pinned(target, what=what, refusal=PodError)
    try:
        os.fchmod(fd, stat.S_IRWXU)  # mkdir's mode is umask-masked; this is not
    except OSError as exc:
        raise PodError(f"could not tighten {what} {target}: {exc}") from exc
    finally:
        _close_fd(fd)


def _record_refusal(cfg: PodConfig, name: str, reason: str) -> None:
    """Record a TERMINAL boot refusal for *name* on the HOST side.

    Best-effort by design: the refusal itself is already decided and printed, so a
    failure to write the note must not turn into a second failure mode. What it
    buys is legibility on macOS, where :func:`kiro_crew.pod.launchd.launchd_exit_code`
    has to exit 0 to stop launchd's restart loop and the refusal would otherwise
    be indistinguishable from a clean exit.

    **Refuses to write outside the pod plane, regardless of caller.** The name is
    re-validated here and the resulting path is proven to be a direct child of
    ``cfg.pods_dir`` before anything is published. That is defense in depth, not the
    primary control -- ``boot`` validates before entering its guarded region, so a
    path-shaped name should never arrive -- but the primary control is one caller's
    ordering and this is a property of the function. Without it, any future caller
    that records before validating turns a best-effort note into an arbitrary host
    write: ``pod _run /tmp/important`` would have atomically overwritten
    ``/tmp/important.refused``. Same reasoning as ``cleanup_home``'s independent
    name re-validation, and the same "protected on one path only is not protected"
    rule this module states elsewhere.
    """
    try:
        validate_name(name)
    except PodError:
        print(f"kirocrew-pod: refusing to record a refusal for invalid pod name {name!r}")
        return
    target = cfg.refusal_file(name)
    try:
        root = cfg.pods_dir.resolve()
        if target.resolve().parent != root:
            print(f"kirocrew-pod: refusing to write a refusal note outside {root}")
            return
    except OSError:
        # Cannot prove the location, so do not write. A missing note costs
        # legibility; an unproven one costs an arbitrary host file.
        return
    try:
        pinned_fs.write_file_pinned(
            target,
            f"{reason}\n",
            what="pod refusal note",
            mode=0o600,
            refusal=PodError,
        )
    except (OSError, PodError, ValueError):
        pass


def _clear_refusal(cfg: PodConfig, name: str) -> None:
    """Drop a stale refusal note so it only ever describes the LAST boot."""
    try:
        pinned_fs.unlink_pinned(cfg.refusal_file(name), what="pod refusal note")
    except (OSError, ValueError):
        pass


def refusal_reason(cfg: PodConfig, name: str) -> str | None:
    """The recorded terminal-refusal reason for *name*, or None if its last boot
    did not refuse. Unreadable is reported as refused-for-an-unknown-reason rather
    than as clean -- the file existing is itself the signal.

    Read through the PINNED no-follow chokepoint, the same one ``_record_refusal``
    publishes through. A by-name ``read_text`` here followed a link at the final
    component, so a planted ``<name>.refused`` symlink pointed at any host file the
    gateway could read made ``kirocrew pod ls`` print that file's contents under
    the note's own label -- a disclosure primitive on the exact path whose WRITE
    side was already pinned (found in review). A non-regular note is refused with a
    reason instead of being followed: the file existing still reports a refusal, so
    the signal survives while the contents never do.
    """
    target = cfg.refusal_file(name)
    try:
        text = pinned_fs.read_file_pinned(target, what="pod refusal note", refusal=PodError).strip()
    except FileNotFoundError:
        return None
    except PodError:
        return "boot refused (reason file is not a regular file; refusing to read it)"
    except (OSError, ValueError):
        return "boot refused (reason file unreadable)"
    return text or "boot refused (reason not recorded)"


def _pin_outermost_existing_windows(target: Path, *, what: str) -> tuple[int, Path]:
    """Pin the deepest ANCESTOR of *target* that already exists, before creating.

    The win32 stand-in for the anchor a ``dir_fd`` walk gets for free, and the one
    level :func:`_pin_created_dir_windows` cannot supply: it demands an
    already-pinned parent, so something has to pin the first one.

    ``platform_compat.pin_directory`` opens without ``FILE_SHARE_DELETE``, and
    that freezes the directory it opens AND every directory above it for the
    handle's lifetime — so pinning the deepest existing ancestor is what makes the
    whole chain above unrenamable while the levels below are created. Doing it in
    the other order is the hole this function closes: a by-name
    ``mkdir(parents=True)`` walks and creates through ancestors nothing is holding,
    so a same-UID process that swaps one between the screen and the first pin has
    the rest of the build land in its own tree.

    The open refuses to follow a reparse point, so a junction planted at the
    anchor's name fails the open rather than being pinned in its target's place.

    Returns the pin and the anchor it names. The caller creates every level from
    there down through :func:`_pin_created_dir_windows`, so no level is ever
    created under an unpinned parent.

    RESIDUAL: the anchor is still located BY NAME, and no witness can close that —
    a witness taken before the pin can only be compared against itself. What the
    pin rules out is a reparse point at the anchor and any rename of it or of
    anything above it from that moment on; what remains is a same-UID rename-swap
    landing in the instant before the pin, which is the operational-isolation
    boundary the pod threat model already records rather than a new one.
    """
    anchor = target.parent
    while not anchor.is_dir() and anchor != anchor.parent:
        anchor = anchor.parent
    if pinned_fs.is_reparse_point(anchor):
        raise PodError(
            f"refusing to build {what} under {anchor}: it is a symbolic link or a "
            "junction, so the path does not name the directory it appears to"
        )
    return pin_directory(anchor), anchor


def _pin_created_dir_windows(parent_fd: int, target: Path, *, what: str) -> int:
    """Create *target* under an already-pinned parent and return its own pin.

    The win32 stand-in for ``pinned_fs.create_and_open_dir_pinned``, which needs
    ``dir_fd`` and so cannot run here. Three steps, in this order, and the order
    is the guarantee: the parent is ALREADY pinned by the caller (so it can be
    neither renamed nor deleted, and neither can anything above it, for as long
    as that handle lives), the child name is screened with
    ``pinned_fs.is_reparse_point`` before it is created, and
    ``platform_compat.pin_directory`` then opens the child WITHOUT following a
    reparse point, so a junction planted at the name between the screen and the
    open fails the open instead of being pinned in its target's place.

    ``parent_fd`` is taken rather than read, so a caller that has not pinned the
    parent cannot reach this. A mode argument is deliberately absent: NTFS
    carries no POSIX mode, ``os.fchmod`` does not exist on this platform, and the
    ancestor pin plus the plane root's own ACL is what bounds who can reach the
    tree -- see the caller for what that costs.
    """
    if parent_fd < 0:  # pragma: no cover - caller bug, never a runtime state
        raise PodError(f"refusing to create {what} without a pinned parent")
    if pinned_fs.is_reparse_point(target):
        raise PodError(
            f"refusing to build {what} at {target}: it is a symbolic link or a junction, "
            "so the path does not name the directory it appears to"
        )
    target.mkdir(exist_ok=True)
    return pin_directory(target)


def _seed_pod_os_home_windows(os_home: Path) -> None:
    """Build the pod OS home on win32, stating what it proves and what it does not.

    **The guarantee, and the ORDER is the guarantee.** The deepest ancestor of
    *os_home* that already exists is screened for a reparse point and PINNED
    first, which freezes it and every directory above it for as long as the handle
    lives. Only then is each remaining level — down through *os_home* and on
    through ``.aws/sso/cache`` — created under the pin of the level above it and
    immediately pinned itself with ``platform_compat.pin_directory``, which opens
    without ``FILE_SHARE_DELETE`` and refuses to follow a reparse point, so a
    junction planted at a name fails the open rather than being pinned in its
    target's place. No level is created under a parent nothing is holding. Every
    handle is held until the whole tree exists and the staging is finished, and
    they are closed in one place.

    The earlier order screened the ancestors by name and then walked them with
    ``mkdir(parents=True)``, taking its first pin at *os_home* itself: every level
    below obeyed "create under a pinned parent" and the outermost level, the only
    one with no parent pin to inherit, did not. A same-UID process that swapped an
    ancestor in that window had this function build the pod's whole OAuth grant
    corridor inside its directory and return normally — with no witness able to
    notice, since the witness compares a value only against itself.

    **The mode tightening is a no-op here, and that is stated rather than
    emulated.** The POSIX branch forces every level to ``0o700`` through
    ``os.fchmod``, which this platform does not implement, and NTFS carries no
    POSIX mode for it to set. What bounds who can reach this tree is the pod plane
    root's own ACL plus the ancestor pins above, not a mode bit. Do not "fix" this
    by calling ``os.chmod``: on Windows that touches only the read-only attribute,
    so it would read as a permission tightening while granting nothing.

    **The residual, named rather than implied.** Two things the pins do not do.
    They do not stop a same-UID process from writing INTO these directories, and
    they do not extend past this function: the child receives ``os_home`` as a
    path and re-resolves it at its own open. And the anchor is still LOCATED by
    name, so a same-UID rename-swap landing in the instant before the first pin is
    not excluded — no witness closes that, because a witness taken before the pin
    can only be compared against itself. What the pin does exclude, from that
    moment on, is a reparse point at any level and any rename or delete of these
    directories or of anything above them. A pod home lives under a plane root
    only this user can write, so the residual is the same trust domain the OS
    already grants that user, and the same operational isolation the pod threat
    model records -- a pod is not protection from arbitrary same-UID processes.
    Closing it would need the child to accept a descriptor instead of a path,
    which the kiro-cli interface does not offer.

    Raises :class:`PodError` when the tree cannot be built, and that refusal
    aborts the boot: this directory becomes the pod child's ``HOME``, so an
    unverified one is the machine-level grant writer the whole mechanism exists to
    prevent. Staging the sign-in material stays best-effort, so a signed-out host
    boots a pod that can prompt for sign-in inside it.
    """
    _refuse_reparse_chain(os_home)
    fds: list[int] = []
    try:
        try:
            # Pin BEFORE creating. The old order screened the ancestors by name
            # and then walked them with mkdir(parents=True), taking its first pin
            # only at ``os_home`` itself — so every level below obeyed "create
            # under a pinned parent" and the outermost one, the only level that
            # had no parent pin to inherit, did not. A same-UID process that
            # swapped an ancestor in that window had this function build the pod's
            # whole OAuth grant corridor inside its directory and return normally.
            anchor_fd, anchor = _pin_outermost_existing_windows(os_home, what="the pod OS home")
            fds.append(anchor_fd)
            # Create every remaining level from the anchor DOWN, each under the
            # pin of the level above it, so no directory is ever created under a
            # parent nothing is holding.
            target = anchor
            for part in os_home.relative_to(anchor).parts:
                target = target / part
                fds.append(_pin_created_dir_windows(fds[-1], target, what=f"pod OS home {part}"))
            for label in (".aws", "sso", "cache"):
                target = target / label
                fds.append(_pin_created_dir_windows(fds[-1], target, what=f"pod OS home {label}"))
        except OSError as exc:
            # The directory the child would receive as HOME does not exist or is
            # not ours, so this is the same fail-closed case as a refused
            # component rather than a skippable seed.
            raise PodError(f"could not build the pod OS home under {os_home}: {exc}") from exc
        # ---- BEST-EFFORT from here: the tree is sound, only the staging can fail.
        for mapping in _runtime_auth_store_mappings():
            staged = _stage_runtime_auth_store_windows(os_home, mapping)
            if staged:
                print(
                    f"kirocrew-pod: staged {staged} file(s) from "
                    f"{mapping.staged_relative.as_posix()}"
                )
        # Nothing host-derived is staged into ``<os-home>/.aws/sso/cache`` on any
        # platform: it is created so the pod's own kiro-cli writes its MCP OAuth
        # grants there, and that is all it ever holds.
    finally:
        pinned_fs.close_all(fds)


def _stage_runtime_auth_store_windows(os_home: Path, mapping: StoreMapping) -> int:
    """Mirror one runtime auth store into *os_home* on win32. Best-effort.

    The win32 twin of :func:`_stage_runtime_auth_store`, and it keeps the two
    properties that matter while dropping the one this platform cannot express:

    * The SOURCE stays pinned. Each file is opened once and handed to
      ``pinned_fs.copy_file_pinned`` as ``src_fd``, documented as the only pinned
      source form here, so the bytes copied are the bytes of the inode that open
      reached rather than of whatever the name means afterwards.
    * Every DESTINATION is an ``O_CREAT | O_EXCL`` create under a directory this
      function pinned, so it can only add files it created. That is also what
      keeps the copy create-only: an occupied name is skipped, so a pod that has
      already refreshed its own credential is never clobbered.
    * What it cannot keep is destination ancestor pinning by ``dir_fd``. Each
      level is created under its pinned parent and pinned itself, which blocks a
      rename or a delete of it, and the final open is still by name.

    A SQLite database is skipped along with its sidecars rather than snapshotted:
    ``_snapshot_sqlite_pinned`` copies through ``dir_fd`` descriptors, and a
    per-file copy of a live database's file set cannot be consistent. A store
    whose token lives in a database therefore stages nothing and the pod boots
    signed-out, which the boot-time viability probe reports.

    Returns the number of files staged, and never raises: an unreadable host
    store just means the pod prompts for sign-in inside itself.
    """
    parts = mapping.staged_relative.parts
    staged = 0
    fds: list[int] = []
    try:
        source_root = mapping.source
        if not source_root.is_dir() or pinned_fs.is_reparse_point(source_root):
            return 0
        for dirpath, dirnames, filenames in os.walk(source_root):
            # ``os.walk`` DESCENDS into a junction on this platform: a directory
            # reparse point is just a directory to it. The file loop below screens
            # each leaf, but a junction planted anywhere under the host's auth store
            # would be stepped into before any leaf is reached, and its target's
            # contents staged into the pod as though they were sign-in material.
            # Prune here, at the entry to the body, because by the time a leaf is
            # screened the traversal itself has already happened.
            dirnames[:] = [d for d in dirnames if not pinned_fs.is_reparse_point(Path(dirpath) / d)]
            rel = Path(dirpath).relative_to(source_root)
            level_fds: list[int] = []
            target = os_home
            try:
                parent_fd = pin_directory(os_home)
            except OSError:
                return staged
            fds.append(parent_fd)
            try:
                for label in (*parts, *rel.parts):
                    target = target / label
                    parent_fd = _pin_created_dir_windows(
                        parent_fd, target, what=f"pod auth store {label}"
                    )
                    level_fds.append(parent_fd)
                    fds.append(parent_fd)
            except (OSError, PodError):
                dirnames[:] = []  # this subtree is unsafe or unwritable; skip it
                continue
            if not level_fds:  # pragma: no cover - parts is never empty
                continue
            for name in sorted(filenames):
                if name.endswith(_SQLITE_SUFFIXES) or _is_sqlite_sidecar(name):
                    # A live database cannot be copied file by file consistently,
                    # and the snapshot helper needs dir_fd descriptors.
                    continue
                if staged >= _RUNTIME_AUTH_STORE_FILE_CAP:
                    print(
                        f"kirocrew-pod: auth store {'/'.join(parts)} exceeded "
                        f"{_RUNTIME_AUTH_STORE_FILE_CAP} files; staging stopped"
                    )
                    return staged
                src = Path(dirpath) / name
                try:
                    # NOT ``os.open(..., getattr(os, "O_NOFOLLOW", 0))``: that flag
                    # does not exist here, so the fallback is 0 and the open FOLLOWS
                    # a reparse point at the leaf — a control that reads as portable
                    # and enforces nothing. Screening first and opening after is no
                    # better: an adversary that can plant the link chooses when.
                    # ``open_file_no_reparse`` refuses it in the SAME operation.
                    src_fd = open_file_no_reparse(src)
                except OSError:
                    continue
                try:
                    copied = pinned_fs.copy_file_pinned(
                        str(src),
                        str(target / name),
                        src_fd=src_fd,
                        skip_existing=True,
                        force_mode=0o600,
                    )
                except (OSError, ValueError):
                    continue
                if copied:
                    staged += 1
    finally:
        pinned_fs.close_all(fds)
    return staged


def _seed_pod_os_home(os_home: Path) -> None:
    """Create-only: stage the agent runtime's identity store into *os_home*.

    This is what lets a sign-in performed ONCE on the operator's real machine be
    reused by every pod, while a pod's own MCP OAuth grants stay confined to
    ``os_home`` -- see ``build_pod_env``'s
    ``KIROCREW_OS_HOME`` docstring for the split this closes. The sign-in material
    staged is the AGENT RUNTIME's own identity store
    (``_runtime_auth_store_mappings``, derived from ``identity_stores``);
    the ``.aws/sso/cache`` tree is CREATED empty and never populated from the host,
    so the pod's grant corridor holds only what the pod itself mints.

    **Every component is created and opened through a PINNED no-follow
    descriptor, never by name.** This function copies a HOST credential into a
    tree under the pod root, and it runs again on every boot -- so a name-based
    ``mkdir(parents=True)`` plus a by-name write would follow a symlink planted
    at ``os-home`` (or at any component beneath it) and deposit the operator's
    SSO token wherever that link pointed, including an agent-readable workspace.
    ``pinned_fs.create_and_open_dir_pinned`` refuses a link at the component it
    creates and pins the parent chain first, and ``pinned_fs.copy_file_pinned``
    validates the descriptor it copies rather than the name, so the inode
    written is the inode checked. This is the same discipline
    ``seed_home_from_scenario`` in this module already applies to a seeded home.

    Create-only and per-file: ``skip_existing`` leaves an existing destination
    untouched (a pod that already signed in, or already refreshed its own token,
    is not clobbered), and a missing or unreadable source token is skipped
    rather than aborting the whole pod boot -- a signed-out host still boots a
    pod that can prompt for sign-in inside it, which is strictly better than
    refusing to boot at all. The staged token is forced to ``0o600`` and every
    directory to ``0o700``, so a token never lands world-readable even if the
    source file's own mode is looser.

    **Raises PodError when the TREE ITSELF cannot be built through pinned
    no-follow descriptors, and that refusal must abort the boot** (``boot`` does
    exactly this). The two phases are deliberately NOT equally forgiving:

    * Building the tree is MANDATORY. This directory becomes the pod child's
      ``HOME`` (``build_pod_env`` exports it as ``KIROCREW_OS_HOME``,
      ``acp.client._apply_pod_home_remap`` assigns it), so if a component is a
      planted symlink the refusal here is the ONLY thing standing between the
      pod's kiro-cli and the real host tree the link points at. An earlier
      revision swallowed this refusal and booted anyway: nothing was written
      through the link by THIS function, but the child then received the refused
      path as its ``HOME`` and wrote its own MCP OAuth grants through the link --
      turning the pod back into the machine-level grant writer this whole
      mechanism exists to prevent. Skipping the seed is safe; booting on an
      unverified ``HOME`` is not, so the two outcomes must not share a branch.
    * Copying the tokens is BEST-EFFORT, unchanged. An unreadable host cache
      (signed out, permission error, stalled mount) and an individual token that
      cannot be copied both leave the pod booting signed-out.

    Every component is chmodded to ``0o700`` rather than only the leaf, so no
    level of the path this credential lands under is group- or world-writable --
    that is the narrowest replacement window the pinned primitives allow. The
    residual is a genuine TOCTOU: a same-UID process can still swap a component
    between this function returning and the child's ``exec``. Per the recorded
    threat model a pod is operational isolation, not protection from arbitrary
    same-UID processes, so that window is documented rather than claimed closed;
    closing it would require handing the child a descriptor instead of a path,
    which the kiro-cli interface does not accept.

    Windows reaches :func:`_seed_pod_os_home_windows` instead, which states its own
    narrower guarantee: that platform has no ``O_DIRECTORY``/``O_NOFOLLOW`` and no
    ``dir_fd``, so it pins each level with ``platform_compat.pin_directory`` (which
    blocks a rename or a delete of the directory and of everything above it, and
    refuses to follow a reparse point) and says plainly that the mode tightening
    has no equivalent there. Every OTHER host without a pinned walk keeps the
    refusal below.
    """
    if not pinned_fs.supports_pinned_walk():
        if IS_WINDOWS:
            _seed_pod_os_home_windows(os_home)
            return
        # No O_DIRECTORY/O_NOFOLLOW on this platform, so the tree cannot be built
        # through pinned no-follow descriptors. REFUSE rather than fall back to a
        # by-name copy: this moves a HOST credential, and an unpinned write is
        # exactly the symlink-redirect the pinning exists to prevent. Refusing to
        # SEED is not enough on its own, because the same unverified directory
        # would still become the child's HOME -- so this is raised, not returned,
        # and the boot stops.
        raise PodError(
            f"pod OS home {os_home} needs O_DIRECTORY/O_NOFOLLOW descriptors to be "
            "built safely and this platform provides none"
        )
    fds: list[int] = []
    try:
        # Each level is created through its PINNED parent, so a link planted at
        # any component is refused instead of followed. Passing the full path
        # per level is deliberate: create_and_open_dir_pinned pins the whole
        # ancestor chain itself and creates only the final component.
        try:
            target = os_home
            for label in ("os-home", ".aws", "sso", "cache"):
                if label != "os-home":
                    target = target / label
                fds.append(
                    pinned_fs.create_and_open_dir_pinned(
                        target, what=f"pod OS home {label}", refusal=PodError
                    )
                )
                # Tighten EVERY level, not just the leaf: a group-writable
                # ancestor is a replacement window for the credential below it.
                os.fchmod(fds[-1], stat.S_IRWXU)
        except OSError as exc:
            # ENOSPC/EACCES/EIO building the tree. The directory the child would
            # receive as HOME does not exist or is not ours, so this is the same
            # fail-closed case as a refused component, not a skippable seed.
            raise PodError(f"could not build the pod OS home under {os_home}: {exc}") from exc
        # ---- BEST-EFFORT from here: the tree is sound, only the staging can fail.
        # Every failure below leaves the pod booting signed-out, which is why none
        # of them may escape as the PodError that aborts the boot.
        #
        # The runtime's OWN identity store comes first because it is what decides
        # whether the child is signed in at all: kiro-cli resolves its access token
        # from its own identity store (see ``_runtime_auth_store_mappings``), not from
        # the SSO cache below. Staging only the cache is what left the child at
        # kiro-cli's login gate with a readable, correctly-unmasked corridor.
        for mapping in _runtime_auth_store_mappings():
            staged = _stage_runtime_auth_store(os_home, mapping)
            if staged:
                print(
                    f"kirocrew-pod: staged {staged} file(s) from "
                    f"{mapping.staged_relative.as_posix()}"
                )
        # NOTHING host-derived is staged into `<os-home>/.aws/sso/cache`. The
        # directory is created (above, pinned and 0o700) because the pod's own
        # kiro-cli writes its MCP OAuth grants there, and that is ALL it ever
        # holds. An earlier revision copied the host's SSO tokens in, which a
        # security review correctly flagged: the corridor that keeps the grant
        # store writable also made those copied HOST bearer tokens readable to any
        # agent shell in the pod. Live acceptance settled that the copy was never
        # load-bearing -- sign-in comes from the runtime's own data store staged
        # just above, not from this cache -- so the copy is deleted rather than
        # hidden. Deleting the material beats masking it from the process that has
        # to write beside it.
    finally:
        pinned_fs.close_all(fds)


#: How many times :func:`cleanup_home` re-attempts the removal, and how long it
#: pauses between attempts. Windows releases a dead process's handles
#: ASYNCHRONOUSLY, so the instant after a gateway exits its HOME is still
#: undeletable even though nothing is running: MEASURED on a native host, killing
#: a holder and deleting immediately leaves the tree in place, and a retry clears
#: it on the SECOND attempt (~100 ms later). A control with no holder deletes on
#: the first attempt, and a holder that is still ALIVE survives every attempt — so
#: this absorbs the OS's release latency without weakening the survivor check
#: below, which still reports a tree that genuinely cannot be reclaimed.
_HOME_RECLAIM_ATTEMPTS = 10
_HOME_RECLAIM_PAUSE_SECS = 0.1


def _rmtree_bounded(unresolved: Path) -> bool:
    """Remove *unresolved* by NAME, retrying while the OS releases handles.

    Returns True when the entry is gone. Always deletes by the unresolved name for
    the reason the caller documents (a swap to a symlink mid-delete must make
    ``rmtree`` refuse rather than follow), and re-checks with ``lexists`` so an
    entry swapped to a dangling link is not mistaken for a clean reclaim.
    """
    for attempt in range(_HOME_RECLAIM_ATTEMPTS):
        shutil.rmtree(unresolved, ignore_errors=True)
        if not os.path.lexists(unresolved):
            return True
        if unresolved.is_symlink():
            # A swap happened; retrying cannot help and must not be attempted —
            # the caller reports the link itself as the residue.
            return False
        if attempt + 1 < _HOME_RECLAIM_ATTEMPTS:
            time.sleep(_HOME_RECLAIM_PAUSE_SECS)
    return not os.path.lexists(unresolved)


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
    — so the removal itself is silent, and it is RETRIED within a bounded window
    because on Windows the handles of a process that has already exited are
    released asynchronously: the delete that runs immediately after a gateway goes
    away can fail on a tree nothing is using. A tree that survives the whole window
    (a live process holds it, or recreated it in append mode right behind the
    delete) returns 1 and names what is left. Without that check a caller cannot
    tell a reclaimed HOME from a swallowed failure; without the retry it cannot
    tell a locked HOME from a slow one.
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
    #
    # Bounded RETRY, because "still there" and "cannot be reclaimed" are not the
    # same state on Windows: handles of a process that has already exited are
    # released asynchronously, so the delete that runs immediately after a gateway
    # goes away fails on a tree nothing is using. See _rmtree_bounded for the
    # measurement. A tree a LIVE process holds survives every attempt and still
    # reports below, so no failure is hidden — only the OS's own latency is.
    if _rmtree_bounded(unresolved):
        return 0
    # Verify by the ENTRY itself, never the resolved target: an entry swapped
    # to a DANGLING symlink during the delete makes rmtree refuse silently
    # (suppressed by ignore_errors), and the resolved target of a dangling
    # link does not exist — so a target-existence check would report a clean
    # reclaim while the link remains as residue that orphan_homes (which
    # skips symlinks) can never surface again.
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

    The single seam every pod-scoped command goes through, so ``boot`` and
    :func:`exec_in_pod` cannot drift apart. Notably the env comes
    from :func:`build_pod_env`, which means a command run against a pod inherits
    the SAME messaging-credential scrubbing as the pod's own gateway — a
    hand-rolled env here would silently let a throwaway instance act as the live
    Slack / WeCom / Telegram identity. The pod's ``EMBEDDINGS=`` setting travels
    the same way, so a ``pod exec`` against an embedding-light pod does not
    quietly download the model the pod was booted to do without.

    Raises :class:`PodError` when the pod has no pinned checkout (never brought
    up from inside a checkout) or that checkout has no provisioned venv.
    """
    validate_name(name)
    env_data = read_env_file(cfg, name)
    checkout_str = env_data.get("CHECKOUT")
    if not checkout_str:
        raise PodError(
            f"pod {name!r} has no pinned checkout — run `kirocrew pod up {name}` "
            f"from inside a kirocrew checkout first"
        )
    checkout = Path(checkout_str).expanduser()
    bin_path = prov.venv_bin(checkout)
    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        raise PodError(f"no kirocrew venv at {bin_path} (provision {name} first)")
    env = build_pod_env(
        cfg,
        cfg.home_dir(name),
        derive_port(cfg, name),
        checkout,
        skip_model_download=embeddings_disabled(env_data),
    )
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


#: Bound on the child-bootstrap probe. A harness that has not reached either
#: terminal state by now is treated as VIABLE, not as a failure: staying alive on
#: stdin is exactly what a healthy ``acp`` child does, and a slow host must not
#: turn a working pod into a refusal.
_CHILD_VIABILITY_TIMEOUT_SECS = 20.0


def _probe_pod_child_bootstrap(pod_env: dict[str, str]) -> None:
    """Refuse the boot when the pod's kiro-cli child cannot bootstrap.

    Raises :class:`PodError`, which ``boot``'s guard turns into a RECORDED
    terminal refusal (``_refuse`` + ``EXIT_REFUSED_UNRECOVERABLE``), so a pod
    whose child cannot start never reaches health 200 and systemd does not
    restart into the same failure every 5s.

    **Why this exists.** The pod remaps the child's ``HOME`` so its OAuth grants
    die with the pod, and that remap broke every ACP spawn in a pod for a whole
    revision while ``/health`` answered 200 the entire time: the failure surfaced
    only as ``agent_unreachable`` on each provider's Connect/Test, which reads as
    a Connections bug rather than a boot failure. A pod that cannot run an agent
    turn is not a working pod, so it must fail at ``pod up``, loudly, with the
    reason recorded where ``kirocrew pod status`` shows it.

    **The probe is the real spawn path, not an approximation.** It resolves the
    executable the same way and passes it through
    ``acp.client.apply_pod_bundle_spawn``, so the bundle-binary substitution the
    pod child depends on is what gets exercised. Anything cheaper (a bare
    ``--version``) short-circuits before the harness bootstraps and would have
    reported the broken revision as healthy -- that is precisely the mistake an
    earlier round's component test made.

    Three outcomes, and only one refuses:

    * Still running when the bound expires -- a healthy ``acp`` child waiting on
      stdin. Killed and accepted.
    * Exited naming its own login gate -- started fine, has no credential.
      Accepted with a warning, because seeding is best-effort.
    * Exited any other way -- could not bootstrap. REFUSED, with the captured
      stderr tail as the recorded reason.

    A missing kiro-cli is NOT a refusal: it is a separate prerequisite Kiro Crew
    does not bundle, it has its own message elsewhere, and failing the boot for it
    would break every pod on a host that simply has not installed it.
    """
    # Function-local: this runs on the gateway boot path, where a module-level
    # import would pull the agent stack into every pod process that never spawns a
    # child. Reached through ``agent_sdk`` -- the ONE sanctioned surface for the
    # agent backend (``scripts/check_agent_sdk_boundary.py``); application code,
    # this module included, may not import ``kiro_crew.acp`` directly. The probe's
    # spawn logic lives there for that reason, and returns a VERDICT because
    # refusing a boot is a pod concept this function owns, not the SDK's.
    from kiro_crew.agent_sdk.pod_child_probe import (
        PROBE_DEAD,
        PROBE_SIGNED_OUT,
        PROBE_UNAVAILABLE,
        probe_pod_child_bootstrap,
    )

    result = probe_pod_child_bootstrap(pod_env, timeout_secs=_CHILD_VIABILITY_TIMEOUT_SECS)
    if result.verdict == PROBE_UNAVAILABLE:
        print("kirocrew-pod: child viability probe skipped (no kiro-cli on this host)")
        return
    if result.verdict == PROBE_SIGNED_OUT:
        print(
            "kirocrew-pod: child bootstrapped but is signed out; "
            f"sign in inside the pod. Child said: {result.detail}"
        )
        return
    if result.verdict == PROBE_DEAD:
        raise PodError(
            f"the pod's kiro-cli child {result.detail}, so every agent turn in this pod "
            f"would fail while /health still answered 200. "
            f"Child: {result.child} with HOME={result.home}."
        )


def boot(cfg: PodConfig, name: str) -> int:
    """Boot the isolated gateway for pod *name*, converting EVERY refusal into a
    recorded terminal exit. Returns an exit code on failure; on POSIX it ``exec``s
    on success and does not return, while on Windows — which has no ``exec`` — it
    supervises the gateway and returns its exit code once it ends.

    **This wrapper is the class closure for "a refusal that escapes the boot path
    with a non-terminal exit".** Two narrower guards do not cover the class: routing
    the explicit ``return`` sites through :func:`_refuse` misses a ``raise``, and an
    AST guard over those returns misses it for the same reason -- a ``raise
    PodError`` is neither a bare return nor visible to a scan over returns. Such a
    raise escapes to the CLI's generic handler, which exits 1, a code NOT in
    :data:`TERMINAL_BOOT_EXIT_CODES`, so systemd retries it and launchd's KeepAlive
    restarts it every ``ThrottleInterval``: the exact restart loop the terminal-exit
    contract exists to prevent, reached by the one shape a return-shaped guard
    cannot see.

    Enumerating raises does not close it either. ``PodError`` is raised from roughly
    forty sites under ``pod/``, many of them transitively reachable from here
    (``validate_name``, ``read_env_file``, ``write_pod_config``,
    ``seed_home_from_scenario``, ``_ensure_pod_dir``, the whole ``pinned_fs``
    refusal surface), and any future one joins them silently. So the conversion is
    structural instead: the body cannot raise ``PodError`` past this frame, and a
    refusal added tomorrow is recorded and given a terminal code without anyone
    remembering to route it.

    ``execve`` replaces the process on the POSIX success path, so nothing after the
    body can run and the wrapper costs the happy path nothing. On Windows the body
    returns the supervised gateway's own exit code instead; that code is not in
    :data:`TERMINAL_BOOT_EXIT_CODES`, so it flows out untranslated and the wrapper
    still only ever converts refusals.

    **Name validation happens BEFORE the guard, deliberately.** The wrapper records
    every refusal it catches, and ``_record_refusal`` derives its path from *name* --
    so catching a NAME-validation failure turned the refusal machinery into a
    host-write primitive: ``pod _run /tmp/important`` would have written
    ``/tmp/important.refused`` outside the pod plane. There is no legitimate pod to
    record against when the name itself is rejected, so that failure exits with the
    honest error and writes nothing. The guard therefore only ever sees a validated
    name, which is also what lets ``_record_refusal`` treat a path-shaped name as
    unreachable rather than merely unlikely.
    """
    try:
        validate_name(name)
    except PodError as exc:
        # Outside the guard on purpose: see the docstring. No record is written --
        # there is no pod this could be a refusal FOR.
        print(f"FATAL: {exc}")
        return EXIT_PROVISIONING
    try:
        return _boot_unguarded(cfg, name)
    except PodError as exc:
        # Already-recorded refusals return through _refuse and never arrive here;
        # this is the escape hatch closing, so record and give it a terminal code.
        return _refuse(cfg, name, EXIT_REFUSED_UNRECOVERABLE, str(exc))


def _boot_unguarded(cfg: PodConfig, name: str) -> int:
    """The boot body. Call :func:`boot`, never this: a ``PodError`` raised here is
    a refusal that must be recorded and given a terminal exit code, and only the
    wrapper does that."""
    validate_name(name)
    env_data = read_env_file(cfg, name)
    checkout_str = env_data.get("CHECKOUT")
    if not checkout_str:
        return _refuse(
            cfg,
            name,
            3,
            f"pod {name!r} has no pinned checkout — run "
            f"`kirocrew pod up {name}` from inside a kirocrew checkout first",
        )
    checkout = Path(checkout_str).expanduser()
    home_dir = cfg.home_dir(name)
    bin_path = prov.venv_bin(checkout)

    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        return _refuse(cfg, name, 3, f"no kirocrew venv at {bin_path} (provision {name} first)")
    if not (checkout / "src" / "kiro_crew" / "static" / "dist").is_dir():
        return _refuse(cfg, name, 3, f"no built dist for {name} (build the worktree first)")

    port = derive_port(cfg, name)
    if port == cfg.live_port:
        return _refuse(cfg, name, 70, f"derived port is the live plane :{cfg.live_port} — refusing")

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
            return _refuse(cfg, name, 3, str(exc))
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
    # Independent of the scenario/directory-seed split above: every pod, seeded
    # or blank, gets its own OAuth-grant-cache home, with the runtime identity
    # store snapshotted in so the harness can resolve its access token (see
    # ``_seed_pod_os_home`` and ``build_pod_env``'s ``KIROCREW_OS_HOME``
    # docstring). The host's SSO cache is NOT copied -- ``.aws/sso/cache`` is
    # created EMPTY and holds only grants this pod itself mints. Create-only per
    # file, so re-running boot against an already-seeded home is a no-op.
    #
    # A REFUSAL here is fatal, not skippable. This directory becomes the pod
    # child's ``HOME``; if a component is a planted symlink, booting anyway hands
    # the pod's kiro-cli the real host tree and it writes machine-level grants
    # through the link. Exit ``EXIT_REFUSED_UNRECOVERABLE`` so systemd does not
    # restart into the same refusal every 5s -- see that constant.
    try:
        # ``create_and_open_dir_pinned`` pins the ANCESTOR chain and creates only
        # the final component, so the pod home must already exist. It does on both
        # branches above (``write_pod_config`` creates it; a scenario seed builds
        # it), but pinning that here keeps a refusal meaning "a component was
        # unsafe" rather than "a branch happened not to create the parent" -- and
        # the create itself is pinned, so a link planted AT the pod home cannot
        # redirect it.
        _ensure_pod_dir(home_dir, what="pod home")
        _seed_pod_os_home(home_dir / "os-home")
    except PodError as exc:
        return _refuse(
            cfg,
            name,
            EXIT_REFUSED_UNRECOVERABLE,
            f"{exc}. Refusing to boot without a verified pod OS home -- the pod's "
            "kiro-cli would write OAuth grants outside the pod. Inspect "
            f"{home_dir / 'os-home'} for a replaced component, then "
            f"`kirocrew pod down {name}` and bring it up again.",
        )
    # Past every terminal refusal: clear any marker an earlier refused boot left,
    # so the record means "the LAST boot refused" rather than "a boot once did".
    _clear_refusal(cfg, name)

    print(f"kirocrew-pod: name={name} port={port} home={home_dir} checkout={checkout}")
    embedless = embeddings_disabled(env_data)
    pod_env = build_pod_env(cfg, home_dir, port, checkout, skip_model_download=embedless)
    if pod_env.get(SKIP_MODEL_DOWNLOAD_ENV) == "1":
        # Keyed on the env the pod will RUN with, not on the env file: a switch the
        # service manager's environment already exports is inherited by build_pod_env
        # and boots the same embedding-light pod without EMBEDDINGS=0 ever being
        # written, so a file-keyed announce stayed silent about exactly that pod.
        # The journal is the only place an operator can confirm the mode after the
        # fact, and "no embed model" is not otherwise observable from a healthy pod:
        # search still answers, just through the keyword fallback. Say it once here,
        # naming the source, so a load measurement taken against this pod is
        # attributable. `== "1"` is the embedder's own test (it ignores every other
        # spelling), so this cannot announce a mode the pod will not be in.
        source = (
            "EMBEDDINGS=0 in the pod env file"
            if embedless
            else "inherited from the boot environment"
        )
        print(
            f"kirocrew-pod: embeddings off ({SKIP_MODEL_DOWNLOAD_ENV}=1, {source}) — the "
            f"model is not downloaded and memory/knowledge search uses the keyword fallback"
        )
    # Last gate before the gateway serves: a pod whose kiro-cli child cannot
    # bootstrap answers /health 200 while every agent turn fails, so it must
    # refuse HERE rather than present itself as up. Raises PodError, which
    # ``boot``'s guard records as a terminal refusal.
    _probe_pod_child_bootstrap(pod_env)
    # Everything this function printed is still sitting in Python's block-buffered
    # stdout (the journal is a pipe, not a tty), and the exec below REPLACES the
    # process image, discarding that buffer. A refusal survives because it returns
    # and exits, which flushes; the success path does not, so the probe's own
    # "signed out" and boot banner lines were being lost. Flush before the exec.
    sys.stdout.flush()
    sys.stderr.flush()
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
    if IS_WINDOWS:
        # Windows has no exec. CPython's os.execve there SPAWNS and terminates the
        # caller, which would break this path twice: the pid would change (so
        # `main_pid` would stop naming the process that bound the port) and the
        # scheduled task's own process would exit while the gateway kept running
        # orphaned, with Task Scheduler reporting the task finished. Supervise the
        # gateway as a child instead and return its exit code, which keeps every
        # caller's contract identical and the wrapper alive as its parent.
        return win_backend.supervise_gateway(
            cfg,
            name,
            bin_path,
            argv,
            pod_env,
            gateway_pid_record=_pod_pid_record_path(cfg, name, port),
        )
    os.execve(str(bin_path), [str(bin_path), *argv], pod_env)
    return 0  # unreachable on success
