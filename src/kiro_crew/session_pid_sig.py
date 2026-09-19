"""Signed publication and verification of the ``session_pid_<pid>.txt`` contract.

The gateway maps its direct child pid to a session key by writing
``config_dir()/session_pid_<pid>.txt`` on session claim. Sandboxed identity
resolvers look the file up directly via the launcher-exported
``KIROCREW_HOST_PID`` (PID-namespace sandboxing strips ``KIROCREW_SESSION_KEY``
from the sandboxed env, and renumbers pids so a ``/proc`` walk cannot match).

The bare ``.txt`` file is NOT a trust root: it lives in ``config_dir()`` which
is same-uid agent-writable, so an agent (or subagent) could forge a mapping for
its own host pid pointing at another slot's key and cross the session
authorization boundary. This module makes the mapping authenticated:

* :func:`publish_session_pid` — the ONLY legitimate write path (gateway-side).
  Writes the ``.txt`` file plus a ``session_pid_<pid>.sig`` sidecar containing
  an HMAC-SHA256 over ``"<pid>:<body>"`` — *body* being the full published
  ``.txt`` content: the session key alone (legacy), or the session key plus a
  second line carrying the process START TOKEN (PID-recycle guard; see
  below). The MAC is keyed with a subkey **derived
  from** the SEL trust root (``sel_hmac.key`` — the same key that makes the
  security event log tamper-evident, and whose reads are deny-listed for agent
  shells in ``security.py``) via a domain-separation label. The raw root key
  never signs a sidecar directly, so this protocol and the SEL audit chain
  never share a signing key.
* :func:`verify_session_pid` — used by STRICT identity resolvers
  (state-mutating MCP tools). Returns the session key only when the sidecar
  verifies; missing/invalid signature fails closed to ``""``.

PID-recycle guard: binding only the pid NUMBER would let a mapping keep
verifying once the OS recycled the pid, answering for the NEW process with
the previous owner's session key until the next restart's orphan sweep.
Publication records the process
incarnation (``platform_compat.get_process_start_id`` — the same identity
``session_pid.py`` writes into its ``<gw>:<pid>:<start_token>`` sweep
records) and BOTH readers refuse on a proven mismatch, while an absent
(legacy file) or unreadable (Windows) token stays "unknown", never a
mismatch. Same-uid only: this is a robustness/misattribution guard, not a
privilege boundary — the recycled process already runs as the same user.

Why forgery dies: the authoritative mapping lives in a directory the sandbox
cannot write (``session-identity``, masked by ``sandbox.py``), so an agent has
nowhere to put a binding a strict resolver will read. Replaying another pid's
``.txt``/``.sig`` pair under its own pid fails because the pid is bound into
the MAC.

What the signature does NOT do: it does not make the binding unforgeable by
itself. Verifying an HMAC needs the same bytes as signing one, so any process
that can verify locally can also sign, and ``sandbox.py`` exposes
``sel_hmac.key`` to the sandbox so the in-sandbox audit writer can chain
entries. The sidecar is therefore tamper-EVIDENCE for the unsandboxed readers,
and the directory fence is what authorizes. A strict resolver does not run
in-sandbox: it asks the gateway, which answers from the kernel-attested peer pid
of the connection.

Lenient resolvers (read-only callers where misattribution is harmless) keep
reading the ``.txt`` without a signature check, but through
:func:`read_session_pid_txt` (same hardened no-follow read path) — the
sidecar is additive, no format break.

Threat model — what the sidecar does and does NOT defend against:

* IN SCOPE (blocked): file forgery (agent writes a bare ``.txt`` mapping its
  own pid to another slot's key -- the authoritative copy lives in the masked
  ``session-identity`` root, so a binding written anywhere an agent can reach
  is not read by anything that authorizes), cross-pid replay (copying another pid's
  ``.txt``/``.sig`` pair — the pid is bound into the MAC), tampering
  (redirecting a signed ``.txt`` — the MAC does not match the new bytes), and
  symlink planting at the predictable paths on BOTH sides: publication uses
  ``atomic_write``/``os.replace`` (swaps a symlink out rather than following
  it), and verification opens with ``O_NOFOLLOW`` + regular-file check so a
  planted symlink can never make the trusted MCP process read a sensitive
  target (see :func:`_read_regular_nofollow`).
* OUT OF SCOPE (unchanged from the env-only baseline): a same-uid agent
  deliberately launching its OWN process with attacker-chosen env
  (``KIROCREW_HOST_PID=<victim pid>``) to reuse a legitimate sidecar. This
  is not a capability the sidecar adds — the identical attack defeats
  env-only resolution (``KIROCREW_SESSION_KEY=<victim key>`` with the key
  read from the same world-readable ``.txt`` files), and a shell-capable
  same-uid agent can bypass client-side resolution entirely by minting a
  local API token. The same equivalence covers STALE-pair replay: an agent
  retaining a valid ``.txt``/``.sig`` pair and restoring it after the pid
  is rekeyed to another session needs same-uid write access to
  ``~/.kiro/crew`` — the exact capability that already lets it read the
  victim key from the pre-existing ``.txt`` and present it via env, so a
  generation/nonce scheme here would not remove any attacker capability
  (the publisher overwrites the pair atomically on every rekey, so stale
  pairs never persist absent that deliberate same-uid interference). No
  client-side resolver can prove process ownership; within gateway-managed
  process trees (the strict resolver's actual threat model)
  ``KIROCREW_HOST_PID`` is launcher-declared, not attacker-chosen.
  Authenticating the calling process itself (e.g. SO_PEERCRED over a
  gateway-controlled unix socket) is the stronger, orthogonal follow-up
  tracked separately.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import stat
import threading
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.sel import _sel_hmac_key_bytes, sel_hmac_key_path

logger = logging.getLogger(__name__)

# Mirrors sel.py: minimum trust-root key length. A shorter key on disk means
# truncation/corruption/tampering — signing with it would yield a predictable,
# forgeable MAC, so both publish and verify treat a short key as absent (fail
# closed on the verify side). The key PATH is not re-derived here: it comes
# from :func:`kiro_crew.sel.sel_hmac_key_path`, the single source of truth
# owned by the key's creator, so the sidecar protocol and the SEL audit chain
# can never resolve different trust-root files (config_dir() honors
# KIROCREW_HOME while SEL's default dir does not — deriving the path
# independently would split the trust root under isolated-home deployments).
_HMAC_KEY_MIN_BYTES = 32

# Domain-separation label. ``sel_hmac.key`` anchors two independent protocols:
# the SEL audit-log chain (``sel.py``) and this pid -> session-key sidecar. To
# keep them cryptographically isolated we NEVER sign the sidecar with the raw
# root key. Instead we derive a purpose-specific subkey via one HMAC step
# (HKDF-extract-style key separation) keyed by this label. Consequences:
#   * the two protocols use *different* signing keys, so a MAC minted under one
#     can never be presented as a valid MAC for the other (no cross-protocol
#     confusion / replay), even though both roots-of-trust are the same file;
#   * the label is versioned — bumping it (``.v2``) rotates every sidecar's
#     effective key without touching the SEL root or on-disk key file.
# The raw root key is used ONLY as the derivation input, never to sign a
# sidecar directly.
_SUBKEY_DOMAIN = b"kirocrew.session_pid.sig.v1"


#: The fenced root holding the AUTHORITATIVE bindings. A direct child of the data
#: home so ``sandbox.py`` can mask it with a plain ``mkdir`` (a nested leaf would have
#: an agent-writable ancestor a rename could swap out from under the mask), and listed
#: in both ``_CREW_HIDDEN_LEAVES`` and ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` there.
_IDENTITY_SUBDIR = "session-identity"


def identity_dir(cfg: Path) -> Path:
    """The directory holding the authoritative pid -> session bindings."""
    return cfg / _IDENTITY_SUBDIR


#: The identity protocol's OWN signing root, inside the masked directory. Separate
#: from the SEL chain key on purpose: that key stays readable in the sandbox so the
#: in-sandbox audit writer can chain entries, and a key the sandbox can read cannot
#: bind anything against a caller in the sandbox. Keeping identity on its own root
#: makes the two controls independent -- the directory mask stops a binding being
#: WRITTEN, this key stops one being SIGNED -- so neither is load-bearing alone.
_IDENTITY_KEY_FILE = "identity_hmac.key"
_IDENTITY_KEY_BYTES = 32


def _identity_key_path(cfg: Path) -> Path:
    return identity_dir(cfg) / _IDENTITY_KEY_FILE


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*.

    A short write would leave the identity root too short to load, and because the
    create is exclusive the next attempt sees the file already there, re-reads
    those same short bytes, and refuses: strict identity fails closed for the life
    of the host with no path back except deleting the file by hand. Named so a test
    can fail it without patching the ``os`` module for the whole process.
    """
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])


def _load_identity_key(cfg: Path, *, create: bool = False) -> bytes | None:
    """Load the identity signing root; ``None`` when absent, short, or unreadable.

    *create* is passed ONLY by :func:`publish_session_pid`, which runs in the
    gateway on session claim. A verifier never creates: a first-touch create on the
    verify side would mint a key the publisher never signed with, turning a
    trust-root problem into a silent accept. Absent means fail closed.

    The read is deliberately plain. Unlike the attribution copy in the data-home
    root, this path is inside a directory no sandboxed process can open, so there is
    no symlink-planting surface here to harden against.
    """
    path = _identity_key_path(cfg)
    try:
        raw = path.read_bytes()
    except OSError:
        raw = b""
    if len(raw) >= _IDENTITY_KEY_BYTES:
        # Re-arm this root's report, for the same reason the audit root re-arms its
        # own: on a gateway that is never restarted the log is the only signal an
        # operator gets, so a root that breaks, is restored, then breaks again must
        # produce a second ERROR rather than the debug line a retained entry gives.
        _clear_reports_for(str(path))
        return raw
    if not create:
        return None
    try:
        identity_dir(cfg).mkdir(mode=0o700, parents=True, exist_ok=True)
        fresh = secrets.token_bytes(_IDENTITY_KEY_BYTES)
        # Exclusive create, so two gateways racing on first claim cannot each
        # believe they minted the root: the loser re-reads the winner's bytes.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, fresh)
        except BaseException:
            # Close BEFORE unlinking: Windows refuses to remove a file that still
            # has an open handle, so unlinking first would leave exactly the
            # partial key this cleanup exists to remove.
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        os.close(fd)
        return fresh
    except FileExistsError:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        return raw if len(raw) >= _IDENTITY_KEY_BYTES else None
    except OSError:
        return None


def _attribution_txt_path(pid: int | str, cfg: Path) -> Path:
    """The lenient resolvers' ATTRIBUTION copy: agent-writable, never authorizing.

    Deliberately left in the data-home root where it has always been. Lenient
    readers run in-sandbox (audit attribution, telemetry), so this copy has to stay
    reachable from there, and a wrong answer only mislabels a log line. Nothing that
    authorizes reads it: :func:`verify_session_pid` reads
    :func:`identity_dir` alone.
    """
    return cfg / f"session_pid_{pid}.txt"


def _txt_path(pid: int | str, cfg: Path) -> Path:
    return identity_dir(cfg) / f"session_pid_{pid}.txt"


def _sig_path(pid: int | str, cfg: Path) -> Path:
    return identity_dir(cfg) / f"session_pid_{pid}.sig"


def _load_hmac_key() -> bytes | None:
    """Load the SEL trust-root key; None when absent/short (never creates).

    Only ``SecurityEventLog`` creates the key (gateway boot). Creating it here
    would let a first-touch race mint a key the SEL then distrusts. The path
    comes from :func:`kiro_crew.sel.sel_hmac_key_path` so both protocols
    always anchor on the same file.

    The FILE is authoritative when it loads: it is the anchor every other
    process resolves independently, so a publisher must not sign with anything
    else while a verifier can still read it. When the file does NOT load, fall
    back to the identical bytes the live ``SecurityEventLog`` validated at init
    (:func:`kiro_crew.sel.sel_hmac_key_bytes`). Without that fallback this
    protocol dies permanently the moment the resolved path stops resolving.
    :func:`kiro_crew.sel.sel_hmac_key_path` re-resolves per call, so
    a key relocated by a concurrent process (legacy -> ``trust/`` migration) is
    followed rather than mourned; this fallback still carries the cases no path
    can resolve away — deleted, chmod'd, truncated, or a relocation whose bytes
    do not match the anchor and so are deliberately not adopted — where SEL
    itself keeps signing from its cached copy and would otherwise leave every
    identity-dependent MCP tool dead with no failing audit chain to point at it.
    """
    try:
        raw = sel_hmac_key_path().read_bytes()
    except OSError:
        raw = b""
    if len(raw) >= _HMAC_KEY_MIN_BYTES:
        # The FILE is intact — the only state every OTHER process can observe
        # too — so re-arm both reports: a later break deserves a fresh
        # operator-facing message rather than the debug line a retained entry
        # would produce. Without this a trust root that breaks, is restored,
        # then breaks again is silent, in the one case (long-lived gateway,
        # never restarted) where the log is the only signal there is.
        _clear_trust_root_reports()
        return raw
    live = _sel_hmac_key_bytes()
    if live is not None:
        _report_trust_root_broken()
    return live


_report_lock = threading.Lock()
# ``(kind, resolved path)`` pairs already reported, so each operator-facing
# message is emitted once per path per process instead of once per session
# claim. Guarded by ``_report_lock``: publication runs on concurrent session
# claims, and an unlocked check-then-add lets two of them both pass the
# membership test and emit duplicate reports.
_reported: set[tuple[str, str]] = set()


def _report_once(kind: str, path: str | None = None) -> tuple[bool, str]:
    """Claim the first report of *kind* for a resolved path.

    Returns ``(is_first, path)``. Keyed on the path so a genuine relocation is
    reported again rather than suppressed by the previous location's entry, and
    on the KIND so the two messages below — which tell an operator different
    things — never silence each other.

    *path* defaults to the SEL trust root, which is what the audit-chain reports
    resolve. The identity root passes its own, because the two roots now fail
    independently: keying an identity failure on the audit path would name an
    intact file in the message and let a healthy audit read re-arm a report about
    a root it says nothing about.
    """
    path = path if path is not None else str(sel_hmac_key_path())
    key = (kind, path)
    with _report_lock:
        first = key not in _reported
        _reported.add(key)
    return first, path


def _clear_reports_for(path: str) -> None:
    """Re-arm every report keyed on *path*."""
    with _report_lock:
        _reported.difference_update({k for k in _reported if k[1] == path})


def _clear_trust_root_reports() -> None:
    """Re-arm every report for the currently resolved SEL trust root."""
    _clear_reports_for(str(sel_hmac_key_path()))


def _report_trust_root_broken() -> None:
    """Report a broken key FILE that this process survived from memory.

    Signing succeeds here, so nothing else in this process would complain — but
    the file is what every OTHER process resolves, independently and fresh. A
    verifier that never held these bytes still fails closed, so staying quiet
    would move the original silent failure one layer over rather than remove
    it: capability dead elsewhere, clean log here.
    """
    first, path = _report_once("file_broken")
    if not first:
        logger.debug("SEL trust root %s still unreadable (signing from memory)", path)
        return
    logger.error(
        "SEL trust root %s is unreadable or shorter than %d bytes. This process "
        "keeps signing session identities from the key bytes SecurityEventLog "
        "validated at its own init, so publication still succeeds HERE — but "
        "every other process resolves that file independently, so a verifier "
        "which never held those bytes refuses the identity and sub-agent "
        "dispatch and memory writes fail there. Restore the file: the in-memory "
        "fallback lasts only as long as this process. Repeat occurrences log at "
        "debug.",
        path,
        _HMAC_KEY_MIN_BYTES,
    )


def _report_signing_unavailable(path: str | None = None) -> None:
    """Report an identity root this process cannot sign with at all, once per path.

    Publication happens on every session claim, so an unthrottled log drowns
    the file (observed at several lines a minute) — and a message that names
    only the mechanism ("published unsigned") leaves an operator with no way to
    connect it to the capabilities that just disappeared. This names the
    consequence and the path to fix.

    *path* is the identity root, passed by the caller because that is the root
    identity signs with. Defaulting it to the audit root would send an operator
    to repair a file that is intact.
    """
    first, path = _report_once("unsignable", path)
    if not first:
        logger.debug("session identity signing still unavailable (identity root %s)", path)
        return
    logger.error(
        "cannot sign session identities: identity root %s is unreadable or "
        "shorter than %d bytes, so every session_pid mapping is published "
        "unsigned. Strict identity resolvers refuse an unsigned mapping, which "
        "means the MCP tools that require a verified session — sub-agent "
        "dispatch and memory writes among them — are refused in sandboxed "
        "sessions until this is fixed. Restore the key file at that path, or "
        "restart the gateway so the next session claim mints it. Repeat "
        "occurrences log at debug.",
        path,
        _IDENTITY_KEY_BYTES,
    )


def signing_health() -> tuple[bool, Path]:
    """Report whether this process can sign session identities, and from where.

    For diagnostic surfaces (``kirocrew doctor``), which is the only place that
    asks proactively: publication itself reports through
    :func:`_report_signing_unavailable`, so an operator whose gateway is
    actually claiming sessions already gets one loud line. This answers the
    same question without waiting for a claim, and deliberately does NOT
    construct :class:`~kiro_crew.sel.SecurityEventLog` — creating the trust
    root as a side effect of asking about it would make the check report on a
    state it just produced, and would put a directory mkdir plus a key write
    behind a read-only command.

    Blocking file I/O: callers on an event loop must offload it.
    """
    return _load_identity_key(config_dir()) is not None, _identity_key_path(config_dir())


def _derive_subkey(root: bytes) -> bytes:
    """Derive the sidecar-signing subkey from the SEL trust root.

    Domain-separated key derivation: the sidecar MAC key is a one-way function
    of the SEL root and :data:`_SUBKEY_DOMAIN`, so the sidecar protocol and the
    SEL audit chain never share a signing key. Reversing the derivation to
    recover the root is infeasible, and a MAC produced by either protocol is
    valueless to the other.
    """
    return hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _compute_sig(key: bytes, pid: int | str, payload: str) -> str:
    """MAC over ``"<pid>:<payload>"``, *payload* being the canonical ``.txt``
    body: the bare session key (legacy) or ``"<session_key>\\n<start_token>"``
    (recycle-guarded — see :func:`publish_session_pid`). Binding the pid
    blocks cross-pid replay; covering the whole body means the start token,
    when present, is signed — flipping only the token invalidates the MAC.
    A legacy body produces a byte-identical message to the pre-token scheme,
    so every signed mapping written before the format change still verifies.
    """
    subkey = _derive_subkey(key)
    return hmac.new(subkey, f"{pid}:{payload}".encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_mapping_body(raw: str) -> tuple[str, str | None] | None:
    """Split a ``.txt`` body into ``(session_key, start_token)``.

    Dual-parse, following ``session_pid.py``'s legacy-vs-guarded record
    handling (``<gw>:<pid>`` vs ``<gw>:<pid>:<start_token>``): one line is
    the legacy pre-token form (token ``None``); two lines are
    ``<session_key>\\n<start_token>``. The token rides a second LINE rather
    than a colon field because — unlike that record's integer fields — the
    session key itself contains colons (``dashboard:chat-7-...``), so a
    colon split could not tell a legacy key from a key+token pair. Anything
    else was never written by :func:`publish_session_pid` — refuse
    (``None``) rather than guess a parse.
    """
    lines = raw.strip().split("\n")
    if len(lines) == 1:
        session_key = lines[0].strip()
        return (session_key, None) if session_key else None
    if len(lines) == 2:
        session_key, token = lines[0].strip(), lines[1].strip()
        return (session_key, token) if session_key and token else None
    return None


def _pid_recycled(pid: int | str, recorded_token: str) -> bool:
    """True iff *pid*'s LIVE start token is readable and differs from
    *recorded_token* — positive evidence the pid number was recycled to a
    different process since the mapping was published.

    The asymmetry here is the whole correctness argument (mirroring the
    sweep guard around ``session_pid.py``'s ``_pid_start_token``): a
    MISMATCH proves the pid now names a DIFFERENT process, so answering
    with the mapped key would attribute the new process to the previous
    owner's session — refuse, on the strict AND the lenient path. An
    UNREADABLE live token (Windows, exited process, permission) is merely
    UNKNOWN, never a mismatch — callers keep today's behaviour there, as
    they do for an ABSENT recorded token (legacy file).
    """
    try:
        live = platform_compat.get_process_start_id(int(pid))
    except (TypeError, ValueError):
        return False  # unparseable pid — identity unknown, not a mismatch
    return live is not None and live != recorded_token


def publish_session_pid(pid: int, session_key: str) -> None:
    """Publish the pid -> session-key mapping with its HMAC sidecar.

    Gateway-side only. Writes ``session_pid_<pid>.txt`` (the lenient-reader
    contract) and ``session_pid_<pid>.sig`` (the strict-resolver
    trust anchor). When the SEL key is unavailable the mapping is published
    unsigned and any stale sidecar is removed — strict resolvers then fail
    closed for this pid instead of trusting a
    signature that no longer matches.

    Both files are written via :func:`kiro_crew.atomic_write.atomic_write`
    (fresh temp file + ``os.replace``), NEVER an in-place ``write_text``:
    the destination paths are predictable and live in the same-uid
    agent-writable config dir, so an agent could pre-plant a symlink at
    ``session_pid_<pid>.txt``/``.sig`` pointing at an arbitrary writable
    file — an in-place open would follow it and truncate the target.
    ``os.replace`` swaps the symlink itself out instead of following it.

    PID-recycle guard: when the live process's start token is
    readable (``platform_compat.get_process_start_id`` — the same
    incarnation identity ``session_pid.py`` records in its
    ``<gw>:<pid>:<start_token>`` sweep entries), it is appended to the
    ``.txt`` as a second line and covered by the MAC, so readers can tell
    "still the process this mapping was published for" from "the OS
    recycled this pid number". An unreadable token (Windows, probe failure)
    degrades to the legacy single-line form — readers then treat identity
    as unknown, exactly as for a legacy file.
    """
    cfg = config_dir()
    token = platform_compat.get_process_start_id(pid)
    # The "\n" guard keeps a pathological multi-line session key (never
    # produced by any surface — keys are single-line ``surface:slot`` shapes)
    # from aliasing the legacy and token-bearing forms under one MAC.
    if token and "\n" not in session_key:
        body = f"{session_key}\n{token}"
    else:
        body = session_key
    atomic_write(_attribution_txt_path(pid, cfg), body)
    # The authoritative copy, in the masked root. Created 0700 here rather than left
    # to the first write: the gateway is the only writer, and a missing directory
    # would make every strict resolver fail closed for want of a parent.
    ident = identity_dir(cfg)
    try:
        ident.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        # Nothing to authorize on is the correct outcome for an unwritable trust
        # root: strict resolvers fail closed, which is what they did before this
        # directory existed. The attribution copy above still lands.
        _report_signing_unavailable(str(_identity_key_path(cfg)))
        return
    atomic_write(_txt_path(pid, cfg), body)
    key = _load_identity_key(cfg, create=True)
    if key is None:
        _report_signing_unavailable(str(_identity_key_path(cfg)))
        try:
            _sig_path(pid, cfg).unlink(missing_ok=True)
        except OSError:
            pass
        return
    atomic_write(_sig_path(pid, cfg), _compute_sig(key, pid, body))


# Upper bound for mapping-file reads. Session keys are short strings
# (< a few hundred bytes) and the sidecar is a 64-char hex MAC; anything
# larger is not a legitimate mapping file. Bounding the read means an
# agent swapping in a huge file cannot make the trusted MCP process
# buffer it into memory / stall on a synchronous read.
_MAX_MAPPING_FILE_BYTES = 4096


def _read_regular_nofollow(path: Path) -> str | None:
    """Read *path* as UTF-8, refusing symlinks, non-regular and oversized files.

    The mapping directory is same-uid agent-writable, so an agent could
    replace ``session_pid_<pid>.txt``/``.sig`` with a symlink to a sensitive
    file; a plain ``read_text()`` in the trusted MCP process would follow it
    (bypassing the agent-facing sensitive-path gate). Defenses, in order:

    * ``O_NOFOLLOW`` (POSIX): the open itself refuses a symlink final
      component — race-free, unlike an ``is_symlink()`` pre-check.
    * ``lstat`` pre-check + post-open identity check (platforms without
      ``O_NOFOLLOW``, i.e. Windows): the pre-check refuses a symlink final
      component before the open, and the opened handle's ``fstat``
      ``(st_dev, st_ino)`` must match the pre-check's ``lstat``. A swap to
      a symlink in the lstat->open window makes the open follow the link,
      so the handle reflects the TARGET file — whose identity cannot match
      the vetted regular file — and the read is refused. This closes the
      TOCTOU race without platform-specific open flags (``st_ino`` is the
      NTFS file index on Windows since Python 3.5).
    * ``fstat``/``S_ISREG``: rejects FIFOs/devices.
    * Size bound (:data:`_MAX_MAPPING_FILE_BYTES`, checked against both
      ``fstat`` and the actual bytes read): rejects oversized files so
      verification can never buffer unbounded agent-controlled data.

    Returns ``None`` on any refusal or I/O error (callers fail closed).
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    pre: os.stat_result | None = None
    try:
        if not nofollow:
            pre = os.lstat(path)
            if stat.S_ISLNK(pre.st_mode):
                return None
        fd = os.open(path, os.O_RDONLY | nofollow)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if pre is not None and (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
            # Path swapped between lstat and open (no-O_NOFOLLOW platforms):
            # the handle points at a different file than the one vetted as a
            # non-symlink — a symlink planted in that window would open its
            # TARGET, which can never share the vetted file's identity.
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_MAPPING_FILE_BYTES:
            return None
        chunks = []
        remaining = _MAX_MAPPING_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_MAPPING_FILE_BYTES:
            # Grew between fstat and read — treat as hostile.
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def read_session_pid_txt(pid: int | str, cfg: Path | None = None) -> str:
    """Return the session key from ``session_pid_<pid>.txt`` WITHOUT signature
    verification, but through the same hardened read path as the strict
    verifier (:func:`_read_regular_nofollow`: symlink refusal, regular-file
    check, size bound).

    This is the LENIENT read: callers that tolerate misattribution (the
    lenient resolver's warm-pool / ancestor-walk fallback, read-only
    audit/telemetry attribution) use it so the trusted MCP process never
    follows a planted symlink on these predictable, same-uid agent-writable
    paths. State-mutating callers MUST use :func:`verify_session_pid`.

    *cfg* overrides the mapping directory (callers that already resolved
    ``config_dir()`` pass it through); defaults to :func:`config_dir`.
    Returns ``""`` on any refusal or I/O error. Never raises.

    A PROVEN pid-recycle (recorded start token present and the live token
    readable but different — see :func:`_pid_recycled`) refuses on this
    lenient path too, deliberately: several call sites try
    :func:`verify_session_pid` first and fall back here (``peer_resolve``),
    so a mismatch surfaced only from the strict path would be silently
    recovered by the fallback and the stale attribution kept. A mismatch is
    positive evidence of a wrong owner — unlike an absent or unreadable
    token, which is merely unknown and resolves as before.
    """
    cfg = cfg if cfg is not None else config_dir()
    # Attribution copy first: it is the one an in-sandbox lenient caller can still
    # read. The authoritative copy is a fallback for UNSANDBOXED lenient callers, so
    # a data home whose root copy was swept still attributes; in-sandbox that read
    # simply fails against the mask and the caller degrades as it did before.
    txt = _read_regular_nofollow(_attribution_txt_path(pid, cfg))
    if txt is None:
        txt = _read_regular_nofollow(_txt_path(pid, cfg))
    if txt is None:
        return ""
    parsed = _parse_mapping_body(txt)
    if parsed is None:
        return ""
    session_key, token = parsed
    if token is not None and _pid_recycled(pid, token):
        return ""
    return session_key


def verify_session_pid(pid: int | str, cfg: Path | None = None) -> str:
    """Return the session key for *pid* iff its HMAC sidecar verifies.

    Fails closed to ``""`` on: missing ``.txt``, missing ``.sig``, a symlink
    or non-regular file at either path (see :func:`_read_regular_nofollow`),
    missing or short SEL key, or signature mismatch. Never raises.

    *cfg* overrides the mapping directory (mirrors
    :func:`read_session_pid_txt`); defaults to :func:`config_dir`.
    """
    if cfg is None:
        cfg = config_dir()
    txt = _read_regular_nofollow(_txt_path(pid, cfg))
    sig_raw = _read_regular_nofollow(_sig_path(pid, cfg))
    if txt is None or sig_raw is None:
        return ""
    parsed = _parse_mapping_body(txt)
    sig = sig_raw.strip()
    if parsed is None or not sig:
        return ""
    session_key, token = parsed
    key = _load_identity_key(cfg)
    if key is None:
        # Distinguishable from the MAC-mismatch warning below: this branch
        # means the identity root itself is absent/short ON THE VERIFY SIDE.
        # Absent is the normal state before the gateway's first session claim
        # mints it, so strict resolvers fail closed until then; a root that
        # disappears later is a host problem worth surfacing rather than a
        # missing key, NOT forgery. Without this log, a trust-root drift
        # silently reproduces the original sandboxed-session bug
        # (strict resolvers fail closed everywhere) while looking
        # identical to a forgery refusal.
        logger.warning(
            "identity root absent/short at %s - refusing session_pid_%s "
            "identity (strict resolvers fail closed until the gateway's next "
            "session claim mints it)",
            _identity_key_path(cfg),
            pid,
        )
        return ""
    # Recompute over the canonical body: legacy files (no token) produce the
    # exact pre-change message, so existing signed mappings keep verifying.
    payload = session_key if token is None else f"{session_key}\n{token}"
    expected = _compute_sig(key, pid, payload)
    if not hmac.compare_digest(expected, sig):
        logger.warning(
            "session_pid_%s signature mismatch — refusing identity "
            "(possible forgery or stale sidecar)",
            pid,
        )
        return ""
    # Recycle check AFTER the MAC: the recorded token is only meaningful
    # once the signature proves it is the one the publisher wrote. Mismatch
    # = the pid was recycled → refuse; absent/unreadable = unknown → resolve
    # (see _pid_recycled for why the asymmetry is load-bearing).
    if token is not None and _pid_recycled(pid, token):
        logger.warning(
            "session_pid_%s start-token mismatch — pid was recycled; "
            "refusing the previous owner's session identity",
            pid,
        )
        return ""
    return session_key
