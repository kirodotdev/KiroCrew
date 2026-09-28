"""Pod port derivation, the recorded-claim scan, and port allocation.

:func:`derive_port` answers which port a pod name maps to -- its pinned ``PORT=``
or its POSIX ``cksum`` slot -- without asking whether that port is free.
:func:`allocate_port` asks once per ``pod up``, walking the band past ports other
pods have recorded, and the caller records the answer as the pod's ``PORT=`` pin.

The per-pod env file and the platform flags are read from
:mod:`kiro_crew.pod.runtime` at call time: that namespace is where the pod CLI,
Dev Fleet and the pod suite reach these names, and a patch there must reach every
reader. The runtime module re-exports every name defined here.
"""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

from kiro_crew.pod import runtime
from kiro_crew.pod.config import PodConfig
from kiro_crew.pod.runtime import PodError

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
    return _port_from_env(runtime.read_env_file(cfg, name).get("PORT"))


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
            if runtime.IS_WINDOWS:
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
    env = runtime.read_env_file(cfg, name)
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
    return runtime._parse_env_text(text)


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
        if not runtime._NAME_RE.match(other):
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
