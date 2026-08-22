"""Presence of kiro-cli's MCP OAuth grant artifacts.

A leaf module on purpose. Three callers need this — ``connections.mint`` (the
curated-provider consent flow), ``connections.status`` (the persisted
connection view), and ``mcp_discovery``'s remote probe (any url a user
configured) — and each of the obvious alternatives is closed:

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
presence, so no credential material can enter the process through it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
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
    origin = f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}"
    if parts.port is not None and parts.port != _DEFAULT_HTTPS_PORT:
        origin = f"{origin}:{parts.port}"
    return hashlib.sha256(f"{origin}{parts.path or '/'}".encode("utf-8")).hexdigest()


def grant_artifact_paths(mcp_url: str, *, cache_dir: Path | None = None) -> tuple[Path, Path]:
    """The paired grant artifact paths for ``mcp_url`` (token, registration).

    The single source of the artifact layout lets callers distinguish absence
    from an indeterminate stat without drifting from :func:`grant_present`.
    """
    directory = cache_dir if cache_dir is not None else kiro_oauth_cache_dir()
    key = grant_key(mcp_url)
    return (
        directory / f"{key}{_TOKEN_SUFFIX}",
        directory / f"{key}{_REGISTRATION_SUFFIX}",
    )


def grant_present(mcp_url: str, *, cache_dir: Path | None = None) -> bool:
    """Whether kiro-cli holds a persisted grant for ``mcp_url``.

    Presence only: the paired artifacts are stat-ed and never opened, so token
    material cannot reach this process. Both must exist -- a lone token file also
    matches the single-file SSO naming this directory mixes in.

    Blocking: the stats are sub-millisecond against a local home but stall for as
    long as the mount does against a network-mounted one, so async callers run this
    through ``asyncio.to_thread`` rather than on the event loop.
    """
    token, registration = grant_artifact_paths(mcp_url, cache_dir=cache_dir)
    return token.is_file() and registration.is_file()


async def grant_observed(mcp_url: str, *, audit_absence: bool = False) -> bool:
    """:func:`grant_present` off the loop, SEL-audited on the acted-on observation.

    The access that owes a trail is the one a caller ACTS on, and ``audit_absence``
    is how the two callers differ on which results those are:

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
    present = await asyncio.to_thread(grant_present, mcp_url)
    if present or audit_absence:
        recorded = await asyncio.to_thread(
            _hooks.emit_internal_read_audit,
            _GRANT_PRESENCE_READ_ID,
            "success" if present else "missing",
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
