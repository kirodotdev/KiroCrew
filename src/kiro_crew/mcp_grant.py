"""Presence of kiro-cli's MCP OAuth grant artifacts.

A leaf module on purpose. Four callers need this — ``connections.mint`` (the
curated-provider consent flow), ``connections.status`` (the persisted
connection view), ``mcp_discovery``'s remote probe (any url a user
configured), and the dashboard's disconnect handler (which deletes the pair) —
and each of the obvious alternatives is closed:

* Keeping it in ``connections.mint`` forces the probe into a *runtime* import,
  because ``mint`` reaches the agent and ACP layers and
  ``test_the_handlers_package_does_not_import_the_mint_engine`` refuses to let
  that graph load at gateway boot. A first-time import at request time is then
  large synchronous file IO on the event loop.
* Copying the key derivation into another caller is worse still: it mirrors an
  undocumented kiro-cli internal (``mcp_client::oauth_util::compute_key``) and the
  artifact layout that binary writes, so a second copy would rot against it
  silently. ``test_connections_mint.py`` keeps recorded hashes precisely to make
  that drift fail loudly.

So the derivation lives here, where all callers reach it with an ordinary
module-scope import. Dependencies are stdlib plus ``hooks`` for the audit, which
every caller already imports — nothing here pulls the agent or ACP layers in.

Nothing in this module OPENS a token file: the artifacts are stat-ed for
presence and unlinked by name, so no credential material can enter the process
through it. Deleting them is grant LIFECYCLE management rather than credential
access, and it lives here for the same reason the layout does -- the disconnect
handler is a fourth caller, and it cannot import ``connections.mint`` at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from stat import S_ISREG
from urllib.parse import urlsplit

from kiro_crew import hooks as _hooks

logger = logging.getLogger(__name__)

# kiro-cli's MCP OAuth artifact directory, and the paired suffixes it writes per
# authorized server.
_KIRO_OAUTH_CACHE_RELATIVE = (".aws", "sso", "cache")
_TOKEN_SUFFIX = ".token.json"
_REGISTRATION_SUFFIX = ".registration.json"
_DEFAULT_HTTPS_PORT = 443
# SEL label for the grant-presence stat, registered in
# ``hooks._AUDIT_ONLY_READ_IDS``. Emitting with an unregistered id records nothing.
# The value keeps its original spelling because that registry key is the contract,
# not the module this code happens to live in.
_GRANT_PRESENCE_READ_ID = "connections_mint.oauth_grant_presence"


def kiro_oauth_cache_dir(*, home: Path | None = None) -> Path:
    """The directory kiro-cli writes MCP OAuth artifacts into."""
    return (home or Path.home()).joinpath(*_KIRO_OAUTH_CACHE_RELATIVE)


def grant_key(mcp_url: str) -> str:
    """kiro-cli's cache key for ``mcp_url``.

    Mirrors ``mcp_client::oauth_util::compute_key``: sha256 over the URL's ASCII
    origin serialization concatenated with its path. The default HTTPS port is
    omitted and an empty path normalizes to ``/`` -- both are what the Rust
    ``url`` crate does before hashing, and getting either wrong makes the key
    miss, which reports a granted provider as ungranted.
    """
    parts = urlsplit(mcp_url)
    host = (parts.hostname or "").lower().encode("idna").decode("ascii")
    # ``urlsplit().hostname`` removes the brackets that are part of an IPv6
    # origin serialization. DNS names cannot contain a colon, so restoring them
    # here also covers compressed and zone-qualified IPv6 literals.
    if ":" in host:
        host = f"[{host}]"
    origin = f"{parts.scheme.lower()}://{host}"
    if parts.port is not None and parts.port != _DEFAULT_HTTPS_PORT:
        origin = f"{origin}:{parts.port}"
    return hashlib.sha256(f"{origin}{parts.path or '/'}".encode("utf-8")).hexdigest()


def grant_artifact_paths(mcp_url: str, *, cache_dir: Path | None = None) -> tuple[Path, Path]:
    """The paired grant artifact paths for ``mcp_url`` (token, registration).

    The single source of the artifact layout lets callers distinguish absence
    from an indeterminate stat without drifting from :func:`grant_presence`.
    """
    directory = cache_dir if cache_dir is not None else kiro_oauth_cache_dir()
    key = grant_key(mcp_url)
    return (
        directory / f"{key}{_TOKEN_SUFFIX}",
        directory / f"{key}{_REGISTRATION_SUFFIX}",
    )


def artifact_presence(path: Path) -> bool | None:
    """One stat, three answers: present, definitively absent, or unknowable.

    Deliberately NOT ``Path.is_file()``. From Python 3.14 that method swallows
    every ``OSError`` and answers ``False``, so an unreadable cache home -- a
    permission error, a stalled mount -- would be indistinguishable from "nothing
    was ever written". This package declares ``requires-python >= 3.12`` with no
    ceiling, so a build running on 3.14 would silently collapse the tri-state and
    tell the owner of an authorized server to sign in again. Stat-ing explicitly
    and classifying the errno answers the same on every supported version.
    """
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False  # ENOENT-family: an answer (nothing was written), not an error
    except OSError:
        return None  # EACCES/EIO/stalled mount: nothing knowable right now
    return S_ISREG(mode)


def grant_presence(mcp_url: str, *, cache_dir: Path | None = None) -> bool | None:
    """Tri-state grant presence from ONE stat pass per paired artifact.

    Presence only: the artifacts are stat-ed and never opened, so token material
    cannot reach this process. Both must exist -- a lone token file also matches
    the single-file SSO naming this directory mixes in.

    Deliberately not a boolean check followed by a diagnostic re-stat: two passes
    race, and a transient failure that clears between them reads as a definitive
    absence. Each artifact is stat-ed exactly once and the pair combines: either
    artifact definitively absent decides the pair, any remaining failed stat makes
    the pair unknowable, otherwise present.

    Blocking: the stats are sub-millisecond against a local home but stall for as
    long as the mount does against a network-mounted one, so async callers run this
    through ``asyncio.to_thread`` rather than on the event loop.
    """
    verdicts = [artifact_presence(p) for p in grant_artifact_paths(mcp_url, cache_dir=cache_dir)]
    if False in verdicts:
        return False
    if None in verdicts:
        return None
    return True


def grant_fingerprint(mcp_url: str, *, cache_dir: Path | None = None) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of the TOKEN artifact, or ``None`` when it cannot be read.

    Presence answers "is there a grant"; this answers "is it the SAME grant". The
    distinction matters wherever a caller has already established that the pair
    currently on disk does not work: presence alone cannot then tell a completed
    consent from the dead pair it is waiting to replace, because both are simply
    "present". Completing an OAuth exchange makes kiro-cli REWRITE the token
    artifact, so a changed fingerprint is the observable event.

    Only the token artifact is fingerprinted. The registration is written once at
    dynamic-client-registration time and is not rewritten by a token exchange, so
    including it would add a value that never changes and dilute the signal.

    Stats only -- the artifact is never opened, so no token byte can enter the
    process, the same boundary :func:`grant_presence` keeps. ``None`` on any
    failure INCLUDING absence: a caller comparing two readings must treat
    "unknown" as "no evidence of change" rather than as a change, and collapsing
    absence into a sentinel tuple would manufacture one.

    Blocking for the same reason as :func:`grant_presence`, so async callers route
    it off the event loop.
    """
    token_path, _registration = grant_artifact_paths(mcp_url, cache_dir=cache_dir)
    try:
        stat = token_path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _labelled_grant_artifacts(
    mcp_url: str, *, cache_dir: Path | None = None
) -> tuple[tuple[str, Path], ...]:
    """The grant artifacts for ``mcp_url``, each paired with a stable label.

    One place binds a label to a path, so a caller can name *which* artifact
    survived without publishing the cache key: the filenames are a sha256 over
    the provider URL and carry nothing a caller needs. Destructures
    :func:`grant_artifact_paths` explicitly rather than zipping it, so the
    token/registration pairing is stated rather than positional.
    """
    token, registration = grant_artifact_paths(mcp_url, cache_dir=cache_dir)
    return (("token", token), ("registration", registration))


def surviving_grant_artifacts(mcp_url: str, *, cache_dir: Path | None = None) -> list[str]:
    """Labels of the grant artifacts that may still be on disk for ``mcp_url``.

    Presence only, the same boundary :func:`grant_presence` keeps: the paths are
    stat-ed and never opened. This exists so a caller can *state* whether the
    local grant is gone instead of inferring it from what a delete loop believed
    it removed.

    An UNREADABLE artifact counts as surviving. :func:`artifact_presence` answers
    three ways and only a definitive absence clears a label: a permission error or
    a stalled mount means nobody can say the credential went, and reporting it as
    gone is the one wrong answer -- it tells the user this machine's connection is
    dead while a usable refresh token may still be sitting there. Collapsing the
    tri-state to a boolean here (``Path.is_file`` does exactly that, and swallows
    every ``OSError`` from Python 3.14) is what would produce that claim.

    Blocking for the same reason as :func:`grant_presence` -- it stalls as long as
    a network-mounted home does -- so async callers route it off the event loop.
    """
    return [
        label
        for label, path in _labelled_grant_artifacts(mcp_url, cache_dir=cache_dir)
        if artifact_presence(path) is not False
    ]


def revoke_local_grant(mcp_url: str, *, cache_dir: Path | None = None) -> list[str]:
    """Unlink the runtime's stored OAuth artifacts for ``mcp_url``.

    Grant LIFECYCLE management, not credential access: each artifact is removed
    with ``unlink`` and never opened, so no token or refresh-token byte can enter
    this process. That is the boundary this module keeps -- kiro-cli owns the OAuth
    chain and its store, and the gateway may observe and delete but never read.

    Deleting is what makes Disconnect mean something locally. Taking the entry
    out of the MCP config alone leaves a usable refresh token on disk, so a later
    reconnect silently resumes the old grant instead of asking for consent. It
    does NOT revoke at the provider -- only the provider can do that -- which is
    why the card still sends the user to the provider's revoke page.

    The artifacts are a PAIR and their removal is verified as one: ONE pass, then
    the pair is re-stat'ed and any survivor is named. Nothing is retried --
    :func:`surviving_grant_artifacts` already reports the failure honestly, and a
    second pass only delays that report while re-running the same unlink against
    the same condition. Reporting only what came off, with the per-artifact
    failure buried at debug, is what would let a Disconnect delete the token,
    leave the registration behind, and still answer "done".

    The CALLER decides whether the grant is ours to delete, and must still hold
    the lock that decided it -- see
    :func:`kiro_crew.connections.ownership.remove_provider_entry`.
    Nothing here re-checks ownership.

    Returns the labels actually removed, for the audit record.
    """
    removed: list[str] = []
    # The single-file `{sha256}.json` form this directory also holds belongs
    # to AWS SSO and is deliberately never touched.
    for label, path in _labelled_grant_artifacts(mcp_url, cache_dir=cache_dir):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Could not unlink the %s grant artifact", label, exc_info=True)
            continue
        if label not in removed:
            removed.append(label)
    surviving = surviving_grant_artifacts(mcp_url, cache_dir=cache_dir)
    if surviving:
        logger.warning(
            "Grant artifacts survived the revoke and the connection may still be usable: %s",
            ", ".join(surviving),
        )
    return removed


async def grant_observed(mcp_url: str, *, audit_absence: bool = False) -> bool | None:
    """:func:`grant_presence` off the loop, SEL-audited on the acted-on observation.

    The access that owes a trail is the one a caller ACTS on, and ``audit_absence``
    is how the two callers differ on which results those are:

    Tri-state, like :func:`grant_presence` it wraps: ``None`` is "could not look".
    A polling caller treats that as falsy and keeps waiting; a caller that RENDERS
    the answer must not turn it into "nobody signed in".

    * The mint's watcher POLLS for up to its TTL, waiting for a grant to appear.
      Only the TRUE result moves a row to ``granted``; every negative on the way
      observed nothing and changed nothing, and auditing each would write one
      synchronous event per poll for a single flow
      (``hooks.emit_internal_read_audit`` marks the event critical so it drains
      the queue and cannot be silently lost). It keeps the default.
    * The probe READS ONCE and renders the answer whichever way it comes out --
      an absent grant is precisely what turns a row into "Sign-in required". That
      negative is acted on as much as the positive, so the probe passes
      ``audit_absence=True`` and the access is recorded either way. The outcome
      word follows :func:`hooks.safe_read_file_internal`'s vocabulary:
      ``success`` when the pair is there, ``missing`` when it is not.

    A caller that acts on both answers must therefore opt in; the default records
    only the positive, so adding a polling caller cannot silently flood the log.

    Best-effort, NOT fail-closed, which is a deliberate departure from
    :func:`hooks.safe_read_file_internal`. That gate denies on an unrecordable
    audit because a success there hands back live credential BYTES; nothing
    sensitive crosses this boundary at all -- the artifacts are stat-ed, never
    opened -- so denying would convert an SEL outage into a consent that never
    completes after the user actually granted it. An unaudited boolean is the
    lesser failure, and it still leaves a warning behind.
    """
    present = await asyncio.to_thread(grant_presence, mcp_url)
    if present is True or audit_absence:
        # ``safe_read_file_internal``'s vocabulary, so one outcome word means the
        # same thing across the SEL surface: the pair is there, definitively is
        # not, or could not be looked at.
        if present is True:
            outcome = "success"
        elif present is False:
            outcome = "missing"
        else:
            outcome = "unreadable"
        recorded = await asyncio.to_thread(
            _hooks.emit_internal_read_audit, _GRANT_PRESENCE_READ_ID, outcome
        )
        if not recorded:
            # The cache key, never the url: a caller-supplied endpoint can carry a
            # credential in userinfo or a query string, and this line lands in
            # gateway.log. The key is a sha256 and is also the more useful handle
            # -- it names the artifact pair on disk that the lookup consulted.
            logger.warning(
                "grant-presence audit for key %s could not be recorded; proceeding unaudited",
                grant_key(mcp_url),
            )
    return present
