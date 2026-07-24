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
  an HMAC-SHA256 over ``"<pid>:<session_key>"``, keyed with a subkey **derived
  from** the SEL trust root (``sel_hmac.key`` — the same key that makes the
  security event log tamper-evident, and whose reads are deny-listed for agent
  shells in ``security.py``) via a domain-separation label. The raw root key
  never signs a sidecar directly, so this protocol and the SEL audit chain
  never share a signing key.
* :func:`verify_session_pid` — used by STRICT identity resolvers
  (state-mutating MCP tools). Returns the session key only when the sidecar
  verifies; missing/invalid signature fails closed to ``""``.

Why forgery dies: an agent cannot read ``sel_hmac.key`` (deny-listed), so it
cannot produce a valid sidecar for a forged ``.txt``. Replaying another pid's
``.txt``/``.sig`` pair under its own pid fails because the pid is bound into
the MAC. Residual risk (an agent evading the deny-list to read the key) is
identical to the existing SEL tamper-evidence threat model.

Lenient resolvers (read-only callers where misattribution is harmless) keep
reading the ``.txt`` without a signature check, but through
:func:`read_session_pid_txt` (same hardened no-follow read path) — the
sidecar is additive, no format break.

Threat model — what the sidecar does and does NOT defend against:

* IN SCOPE (blocked): file forgery (agent writes a bare ``.txt`` mapping its
  own pid to another slot's key — no valid sidecar can be produced without
  the deny-listed root key), cross-pid replay (copying another pid's
  ``.txt``/``.sig`` pair — the pid is bound into the MAC), tampering
  (redirecting a signed ``.txt`` — the old MAC no longer matches), and
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
  ``~/.kirocrew`` — the exact capability that already lets it read the
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
import stat
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.sel import sel_hmac_key_path

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


def _txt_path(pid: int | str, cfg: Path) -> Path:
    return cfg / f"session_pid_{pid}.txt"


def _sig_path(pid: int | str, cfg: Path) -> Path:
    return cfg / f"session_pid_{pid}.sig"


def _load_hmac_key() -> bytes | None:
    """Load the SEL trust-root key; None when absent/short (never creates).

    Only ``SecurityEventLog`` creates the key (gateway boot). Creating it here
    would let a first-touch race mint a key the SEL then distrusts. The path
    comes from :func:`kiro_crew.sel.sel_hmac_key_path` so both protocols
    always anchor on the same file.
    """
    try:
        raw = sel_hmac_key_path().read_bytes()
    except OSError:
        return None
    if len(raw) < _HMAC_KEY_MIN_BYTES:
        return None
    return raw


def _derive_subkey(root: bytes) -> bytes:
    """Derive the sidecar-signing subkey from the SEL trust root.

    Domain-separated key derivation: the sidecar MAC key is a one-way function
    of the SEL root and :data:`_SUBKEY_DOMAIN`, so the sidecar protocol and the
    SEL audit chain never share a signing key. Reversing the derivation to
    recover the root is infeasible, and a MAC produced by either protocol is
    valueless to the other.
    """
    return hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _compute_sig(key: bytes, pid: int | str, session_key: str) -> str:
    subkey = _derive_subkey(key)
    return hmac.new(
        subkey, f"{pid}:{session_key}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def publish_session_pid(pid: int, session_key: str) -> None:
    """Publish the pid -> session-key mapping with its HMAC sidecar.

    Gateway-side only. Writes ``session_pid_<pid>.txt`` (the lenient-reader
    contract, unchanged) and ``session_pid_<pid>.sig`` (the strict-resolver
    trust anchor). When the SEL key is unavailable the mapping is published
    unsigned and any stale sidecar is removed — strict resolvers then fail
    closed for this pid (pre-sidecar behavior) instead of trusting a
    signature that no longer matches.

    Both files are written via :func:`kiro_crew.atomic_write.atomic_write`
    (fresh temp file + ``os.replace``), NEVER an in-place ``write_text``:
    the destination paths are predictable and live in the same-uid
    agent-writable config dir, so an agent could pre-plant a symlink at
    ``session_pid_<pid>.txt``/``.sig`` pointing at an arbitrary writable
    file — an in-place open would follow it and truncate the target.
    ``os.replace`` swaps the symlink itself out instead of following it.
    """
    cfg = config_dir()
    atomic_write(_txt_path(pid, cfg), session_key)
    key = _load_hmac_key()
    if key is None:
        logger.warning(
            "sel_hmac.key unavailable — session_pid_%s published unsigned "
            "(strict resolvers will refuse this identity)",
            pid,
        )
        try:
            _sig_path(pid, cfg).unlink(missing_ok=True)
        except OSError:
            pass
        return
    atomic_write(_sig_path(pid, cfg), _compute_sig(key, pid, session_key))


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
    """
    txt = _read_regular_nofollow(_txt_path(pid, cfg if cfg is not None else config_dir()))
    return txt.strip() if txt is not None else ""


def verify_session_pid(pid: int | str) -> str:
    """Return the session key for *pid* iff its HMAC sidecar verifies.

    Fails closed to ``""`` on: missing ``.txt``, missing ``.sig``, a symlink
    or non-regular file at either path (see :func:`_read_regular_nofollow`),
    missing or short SEL key, or signature mismatch. Never raises.
    """
    cfg = config_dir()
    txt = _read_regular_nofollow(_txt_path(pid, cfg))
    sig_raw = _read_regular_nofollow(_sig_path(pid, cfg))
    if txt is None or sig_raw is None:
        return ""
    session_key = txt.strip()
    sig = sig_raw.strip()
    if not session_key or not sig:
        return ""
    key = _load_hmac_key()
    if key is None:
        # Distinguishable from the MAC-mismatch warning below: this branch
        # means the trust root itself is absent/short ON THE VERIFY SIDE —
        # the signature of a publisher/verifier trust-root split (e.g. SEL
        # initialized with a custom base_dir in one process only) or a
        # missing key, NOT forgery. Without this log, a trust-root drift
        # silently reproduces the original sandboxed-session bug
        # (strict resolvers fail closed everywhere) while looking
        # identical to a forgery refusal.
        logger.warning(
            "SEL trust-root key absent/short at %s — refusing session_pid_%s "
            "identity (strict resolvers fail closed; if monitoring is broken "
            "in sandboxed sessions, check for a publisher/verifier trust-root "
            "split)",
            sel_hmac_key_path(),
            pid,
        )
        return ""
    expected = _compute_sig(key, pid, session_key)
    if not hmac.compare_digest(expected, sig):
        logger.warning(
            "session_pid_%s signature mismatch — refusing identity "
            "(possible forgery or stale sidecar)",
            pid,
        )
        return ""
    return session_key
