"""Signed publication and verification of the per-session TOKEN -> session-key map.

The sibling of :mod:`kiro_crew.session_pid_sig`, and the reason both exist is that
they answer the same question from two different keys. That module maps a PROCESS
(``session_pid_<pid>.txt``) to the session that owns it; this one maps a per-ACP-
SESSION token to the session that minted it. The distinction is what makes a
switch-free identity possible at all:

* a pid names a PROCESS, and one kiro-cli process hosts many ACP sessions
  (``agent.session_sharing``: a ``spawn_run`` subagent runs on its parent's
  process), so every pid-keyed channel answers with the PARENT's session for a
  subagent's MCP child;
* a token names ONE session, is minted per ``session/new``
  (``mcp_gateway.claim.mint_stub_session_token``), and rides that session's own
  ``mcpServers`` elements in ``env``, so a subagent's control-plane server
  carries a name its parent's does not.

The mapping exists so that the token has a reader needing no daemon, no stub and no
config switch. gatewayd is the other reader, and it matches the token against a claim
frame — a channel that exists only where the shared MCP gateway is enabled AND the
server is listed in ``mcp_gateway.stub_servers``. Where it does not, the only
remaining source is the env var, which goes stale on a warm-pool rekey and names the
parent for a subagent sharing its process; that is the "no identity channel on this
install" branch of :func:`kiro_crew.mcp_core.strict_identity_diagnosis`. A signed
file answers on a default install, where neither of those holds.

Why the file is not the trust root: it lives in ``config_dir()``, which is
same-uid agent-writable, so an agent could otherwise forge a mapping from a token
it holds to another slot's key. So:

* :func:`publish_session_token` — the ONLY legitimate write path (gateway-side).
* :func:`verify_session_token` — the reader, used by BOTH the strict and the
  lenient resolver. Fails closed to ``""`` on anything odd.

Retraction belongs to ``session_pid._prune_stale_session_token_files`` rather than to a
call at session teardown, and its lifecycle is worth stating exactly, because "sweep"
suggests something it is not: it runs from ``cleanup_orphaned_sessions``, which is
STARTUP AND GRACEFUL SHUTDOWN ONLY (``session.py`` says so in as many words). So a
gateway that runs for a month prunes nothing in between, and the bound on the directory
is one file per session started since the last boot or clean stop.

That is a deliberate cost rather than an oversight. A mapping outliving its session is
not a forgery risk — it names a session that does not exist, and any holder of its token
is inside the boundary below — so what is owed is bounding accumulation, not prompt
removal; and a boot-time pass covers the case a teardown hook structurally cannot, a
gateway killed without running one. Pruning also cannot cost a LIVE session its
identity at any cadence: the mapping is republished at the start of every turn, before
anything in that turn can call a tool.

Trust boundary, stated plainly because it is the same one the env var already has
and not a wider one:

* IN SCOPE (blocked): **forgery** — minting a valid MAC needs the SEL trust root
  (``sel_hmac.key``) that the subkey derives from, and ``is_sensitive_path``
  refuses it on the agent FILE-TOOL path. The strength of that is worth stating
  rather than rounding up: ``security.md`` classifies the leaf ``VISIBLE``, so it
  has no OS fence and no bash-layer fence, and a spawned shell reaches a file
  through an ``open()`` that never routes through the tool gate. So forgery is
  blocked against the file tools and NOT against a shell — the same residual the
  SEL audit chain itself carries, since both rest on that one key, and closing it
  means moving the in-sandbox reader behind the gateway so the leaf can become
  ``HIDDEN`` (``security.md``'s own remedy), never another path matcher. A shell
  that can read the key is also a shell that can mint a local API token and skip
  client-side resolution entirely, so this protocol is not the narrow point;
  **cross-token replay** —
  copying another session's file to the name derived from a token you hold fails,
  because the TOKEN itself is bound into the MAC, not just the filename;
  **tampering** — editing the session key invalidates the MAC; **symlink
  planting** at the predictable path, on both sides (publication uses
  ``atomic_write``/``os.replace``, which swaps a symlink out rather than following
  it; verification opens with ``O_NOFOLLOW`` and refuses a non-regular file).
* OUT OF SCOPE, and UNCHANGED from the env-var baseline: a same-uid process that
  can read the mapping directory. Such a process can already read the pid
  mappings and present another session's key through ``KIROCREW_SESSION_KEY``, and
  a shell-capable same-uid agent can bypass client-side resolution entirely by
  minting a local API token. Same-uid processes are inside the boundary; this
  module neither narrows that nor widens it. Authenticating the calling process
  itself (SO_PEERCRED over a gateway-owned socket) is the stronger, orthogonal
  follow-up tracked with the pid sidecar's own note.

Which carriers deliver the token, and the one that does not. It rides the ``env`` of a
session's control-plane MCP elements — the mirror projections
(:func:`kiro_crew.providers.mirrors.identity.control_plane_identity_env`), the
member-dispatch element, the broker stubs — and the child environment of a
one-session process (``acp.client.AcpClient``, which serves the kiro backend among
others). The SHARED kiro runtime (``acp.runtime.AcpRuntime`` on that backend) has
neither: its control-plane servers reach kiro-cli through ``--agent`` rather than a
session array, and the process is session-unbound by design because it multiplexes N
sessions. So no per-session carrier exists there to stamp, and identity on that one
path is what it is for every non-token source —
:func:`kiro_crew.mcp_core.strict_identity_diagnosis` names the missing channel for an
operator. Giving it one means injecting session-level control-plane entries, and a
session-injected element outranks the spec entry it shadows while carrying neither
that entry's ``tools`` allowlist nor its ``disabledTools`` narrowing, so that is a
tool-surface decision rather than a carriage one.

One consequence of binding the token rather than the filename deserves stating on
its own: the filename is ``sha256(token)`` and carries NO secret, so the mapping
directory can be listed without leaking a bearer name — and a listing is useless,
because verification requires the token that hashes to the name.

Domain separation: the MAC key is derived under :data:`_SUBKEY_DOMAIN`
(``kirocrew.session_token.sig.v1``), a DIFFERENT label from the pid sidecar's, so
a MAC minted by one protocol can never verify in the other even though both
anchor on the same key file. That is not decorative — both messages are
``"<identifier>:<session_key>"`` shaped, so without the split a pid sidecar for
pid ``N`` would verify as a token sidecar for a token spelled ``N``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.sel import sel_hmac_key_path

# The hardened primitives are IMPORTED, not re-implemented. Both are security
# behaviour rather than convenience: ``_read_regular_nofollow`` is the symlink /
# non-regular / size discipline every read of an agent-writable mapping path owes,
# and ``_load_hmac_key`` carries the trust-root resolution AND the operator
# reporting that goes with a broken key. A second copy of either would be a place
# for the two protocols' read hardening to drift apart, and this codebase already
# states the direction that drift takes: "a hand-copy of a subtraction drifts in
# the bad direction here". Sharing them means a fix to the reader fixes both
# sidecars at once. What is deliberately NOT shared is the subkey derivation —
# that one exists to differ (see :data:`_SUBKEY_DOMAIN`).
from kiro_crew.session_pid_sig import _load_hmac_key, _read_regular_nofollow

logger = logging.getLogger(__name__)

#: Domain-separation label for the token sidecar's signing subkey. See the module
#: docstring for why a label DIFFERENT from ``session_pid_sig``'s is load-bearing
#: rather than tidy. Versioned: bumping it (``.v2``) rotates every token sidecar's
#: effective key without touching the SEL root or the key file on disk.
_SUBKEY_DOMAIN = b"kirocrew.session_token.sig.v1"


def _sig_path(token: str, cfg: Path) -> Path:
    """The mapping file for *token*, named by ``sha256(token)``.

    The RAW token never appears in a filename. It is a bearer name for a
    session's identity (``mcp_gateway.claim.mint_stub_session_token``: "it must
    never be logged, exported in ``stats()``, or written to the stub fallback
    journal"), and a filename is the least confidential thing on a host — it
    reaches ``ls``, a backup index, a crash report and any process that can read
    the directory. Hashing means the directory can be listed without handing out
    a token, while a reader that HOLDS the token still finds its file in one
    ``open`` with no scan.
    """
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return cfg / f"session_token_{digest}.sig"


def _derive_subkey(root: bytes) -> bytes:
    """Derive this protocol's signing subkey from the SEL trust root.

    One HMAC step (HKDF-extract-style key separation) keyed by
    :data:`_SUBKEY_DOMAIN`. The raw root key NEVER signs a sidecar directly, so
    the SEL audit chain, the pid sidecar and this protocol hold three distinct
    signing keys derived from one file. Reversing the derivation to recover the
    root is infeasible.
    """
    return hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _compute_sig(key: bytes, token: str, body: str) -> str:
    """MAC over ``"<token>:<body>"``.

    Binding the TOKEN — not the filename it hashes to — is what makes replay
    fail: an agent that copies another session's file to the name derived from
    its OWN token still cannot make it verify, because the MAC covers the token
    the reader presents. Binding the body means editing the session key
    invalidates it.
    """
    return hmac.new(
        _derive_subkey(key), f"{token}:{body}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _split_record(raw: str) -> tuple[str, str] | None:
    """Split a mapping file into ``(mac, body)``, or ``None`` when malformed.

    Wire form is ``"<mac_hex>\\n<body>"``: the MAC FIRST, because it is the part
    with a fixed shape. A body-first layout would need the reader to know where
    the body ends before it can find the MAC, which is exactly the kind of
    guess-the-parse that :func:`session_pid_sig._parse_mapping_body` refuses.
    With the MAC first the split is at the first newline and everything after it
    is body — so a body that later grows a second line (a runtime start id, say)
    is a shape this reader already handles and an old file still verifies.
    """
    mac, _, body = raw.partition("\n")
    mac = mac.strip()
    body = body.strip()
    if not mac or not body:
        return None
    return mac, body


def _invalidate(path: Path) -> None:
    """Remove the mapping at *path*, and say so loudly if that is refused.

    One step, deliberately. Unlinking removes a planted symlink itself rather than
    its target, rewrites nothing, and asks the filesystem for no space, so it is
    the one invalidation that is both safe against this same-uid-writable
    directory and available under the failure most likely to have brought us here
    (ENOSPC). Anything more — truncating in place, say — is a destructive WRITE to
    a path an agent may control, and needs its own vetting to be safe; that is
    surface this protocol does not need, because a mapping's absence already costs
    a caller only the fallbacks a rekey has invalidated anyway.

    The residual is named rather than implied: if the unlink is refused (a parent
    that denies it), the stale record survives and a reader still answers with it
    until a later publication succeeds. That is why this logs at WARNING where its
    caller logs at debug — an uninvalidated mapping is the single state in this
    protocol where identity can be WRONG instead of absent, and it must not be
    silent.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "could not invalidate the identity mapping at %s — a strict resolver "
            "may still read the PREVIOUS session's key from it until a later "
            "publication succeeds",
            path.name,
        )


def publish_session_token(token: str, session_key: str) -> None:
    """Publish the *token* -> *session_key* mapping, signed. Gateway-side only.

    Called when the token is minted (``session/new``) and again on every
    ``rekey()``/claim, because a warm-pool process is re-keyed to a DIFFERENT
    session while its MCP children — and the token in their env — live on. The
    token deliberately survives a rekey (``AcpClient``: "a fresh token would
    leave the live ones carrying a name no claim will ever mention again"), so
    the file is what changes, and re-publication is the whole mechanism by which
    a warm-pool claim becomes visible to an already-running MCP child.

    ONE file, written with a single ``atomic_write``/``os.replace``. That is a
    correctness requirement rather than tidiness: with the MAC and the body in
    two files a reader could observe the new MAC against the old body — or the
    reverse — mid-rekey and refuse a live session's identity. One ``os.replace``
    of one file has no torn state to observe. It is also what makes publication
    symlink-safe: ``os.replace`` swaps a pre-planted symlink out instead of
    following it and truncating its target.

    No-ops on an empty *token* or *session_key* — a caller with neither has
    nothing to publish, and writing a mapping to ``""`` would let an unkeyed
    warm-pool worker answer as the empty session.

    Any publication that does not succeed INVALIDATES the mapping rather than
    leaving the last one standing (:func:`_invalidate`), whether it stopped on a
    missing trust root or on an ``OSError`` from the write. On the rekey path a
    surviving record names the previous session, so absent is the safe state and
    stale is not. The mapping is never published unsigned. That differs from the pid sidecar, which keeps
    writing a bare ``.txt`` because a LENIENT reader contract depends on it; this
    protocol has one file and one reader, both of which require the MAC, so an
    unsigned file would be pure liability — unreadable to the only consumer and
    forgeable in place. Strict resolvers fail closed for this token instead, and
    the trust-root break is reported by ``_load_hmac_key`` itself.

    Blocking file I/O; callers on an event loop must offload it. Never raises.
    """
    if not token or not session_key:
        return
    cfg = config_dir()
    path = _sig_path(token, cfg)
    key = _load_hmac_key()
    if key is None:
        _invalidate(path)
        return
    try:
        atomic_write(
            path,
            f"{_compute_sig(key, token, session_key)}\n{session_key}",
            mode=0o600,
        )
    except OSError:
        # Invalidate before giving up. A failed write is not a lost update here:
        # this function runs on every ``rekey()``, so the record it could not
        # replace names the session this process served BEFORE the claim, and a
        # strict resolver reading it would authenticate the rekeyed caller as that
        # earlier session for the rest of the turn. Deleting it costs the caller
        # only the fallbacks a rekey already invalidated (a stale env var, a pid
        # that names the parent), which is why absent beats stale.
        _invalidate(path)
        # Identity publication must never break a turn — the caller falls back to
        # whatever other source it has (the env var, the pid sidecar).
        logger.debug("could not publish identity mapping", exc_info=True)


def schedule_session_token_publish(token: str, session_key: str) -> None:
    """Publish the mapping without blocking the event loop. Safe from sync code.

    ``rekey()`` is synchronous and is called from the event loop, while
    :func:`publish_session_token` is a key read plus an ``atomic_write`` — blocking
    filesystem work that must not run there. This offloads it to the maintenance
    executor when a loop is running (the same pool
    ``messaging.identity.publish_turn_identity`` uses for the pid mapping) and
    publishes inline when there is none, so a CLI or test context still gets the
    file written rather than silently skipped.

    Fire-and-forget, like ``mcp_gateway.claim.schedule_claim``: the identity a turn
    resolves is republished by the next turn anyway, so a lost publication costs at
    most the window before it and must never fail a claim.

    Both arguments are TYPE-checked, not merely truth-checked, and for the same
    reason ``schedule_claim`` type-checks its socket path and pid: every caller
    reads the token off a session handle with ``getattr``, so nothing upstream
    pins it to a ``str``. A non-string reaching the hashing below would raise
    ``TypeError`` out of ``rekey()`` — a warm-pool claim failing because of an
    identity side effect, which is strictly worse than the claim proceeding
    without one.
    """
    if not isinstance(token, str) or not isinstance(session_key, str):
        return
    if not token or not session_key:
        return
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        publish_session_token(token, session_key)
        return
    # Imported here rather than at module scope: this module is imported by the
    # kirocrew-core stdio MCP server for VERIFICATION only, and that side must not
    # pay for the executor module's import graph.
    from kiro_crew.executors import maintenance_executor

    loop.run_in_executor(maintenance_executor(), publish_session_token, token, session_key)


def verify_session_token(token: str) -> str:
    """Return the session key *token* maps to, iff the MAC verifies.

    Fails closed to ``""`` on: an empty token, a missing file, a symlink or
    non-regular or oversized file at the path (see
    :func:`session_pid_sig._read_regular_nofollow`), a malformed record, a
    missing or short SEL trust root, or a MAC mismatch. Never raises.

    The mapping directory is :func:`config_dir` with no override parameter: the
    pid sidecar's readers take one because several call sites there have already
    resolved it and pass it through, and none of this protocol's callers has.
    """
    if not token:
        return ""
    cfg = config_dir()
    raw = _read_regular_nofollow(_sig_path(token, cfg))
    if raw is None:
        return ""
    record = _split_record(raw)
    if record is None:
        return ""
    mac, body = record
    key = _load_hmac_key()
    if key is None:
        # Distinguishable from the mismatch below: the trust root itself is
        # absent/short on the VERIFY side. That is the signature of a
        # publisher/verifier trust-root split (SEL initialized with a custom
        # base_dir in one process only) or a deleted key — NOT forgery. Without
        # this line such a drift silently reproduces the bug this protocol
        # exists to fix, and looks identical to a refused forgery.
        logger.warning(
            "SEL trust-root key absent/short at %s — refusing session-token "
            "identity (strict resolvers fail closed; if identity is broken for "
            "every session, check for a publisher/verifier trust-root split)",
            sel_hmac_key_path(),
        )
        return ""
    if not hmac.compare_digest(_compute_sig(key, token, body), mac):
        # The token is a secret, so it is NOT logged — the digest that names the
        # file is, which is enough to find the mapping on disk and carries no
        # bearer value.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - the argument is the sha256 DIGEST naming the file, never the token  # noqa: E501
        logger.warning(
            "identity mapping signature mismatch for %s — refusing identity "
            "(possible forgery or stale sidecar)",
            _sig_path(token, cfg).name,
        )
        return ""
    return body
