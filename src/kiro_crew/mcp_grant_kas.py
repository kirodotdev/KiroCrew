"""Presence of a KAS-held MCP OAuth grant, read from entry names alone.

On the KAS backend kiro-cli's cache (see :mod:`kiro_crew.mcp_grant`) is never
written. The KAS engine keeps a remote MCP server's sign-in through Crew's
``_kiro/secret/*`` store instead, and the persisted ``tokens`` entry lands in the
dedicated grant vault under ``<data_home>/kas/mcp-grants/.vault/`` (see
:class:`kiro_crew.acp.kas_secret_storage.McpGrantStore`). A probe that looks only
at kiro-cli's cache therefore reports "Sign-in required" for a server the engine
is signed in to.

This module answers the same presence question for that vault, gated by the
fail-closed ``agent.kas_persist_mcp_sign_in`` setting, resolved off the event loop.
With persistence off, :func:`kas_grant_observed` answers ``False`` without reading
the vault or writing an SEL audit event: leftover entries are not live grants.
It is a leaf for the reason :mod:`kiro_crew.mcp_grant` is: the remote probe imports
it at module scope, and ``kiro_crew.acp`` pulls in the client and runtime graph.
So the vault layout and persistence setting live here and the engine's store
imports them from here.

What is touched: the vault's ``secrets.enc`` envelope is parsed for its entry
NAMES, which it stores in plain text. The key file is never opened and no entry
is decrypted, so no credential byte can leave this module; the only output is a
boolean.

The entry name mirrors an undocumented engine internal,
``calculateConnectionHash`` in ``@kiro/agent``'s ``acp-server.js`` (checked
against KAS 2.36.0 and 2.37.1): sha256 over the trimmed, lower-cased url, ``|``,
the sorted ``name:value`` headers joined by ``|``, and a ``|cimd:`` suffix when
the oauth config names a client-metadata url but no client id. Crew never sends
a url entry's headers to the engine (they are stripped, or the entry becomes a
local bridge). It does send the ``oauth`` block (``kas_engine_features.carry_oauth``),
but never its ``clientMetadataUrl``, so no entry gets the ``|cimd:`` suffix and the
digest input is always ``<url>|``. An engine upgrade that re-keys this makes the
check miss; ``test_mcp_grant_kas.py`` pins a recorded hash so that change has to
be made on purpose.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from pathlib import Path

from kiro_crew import hooks as _hooks
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(__name__)

# The grant vault's directory, relative to the data home. ``McpGrantStore`` builds
# its paths from this, so the store and this check cannot disagree on where it is.
GRANT_STORE_PARTS = ("kas", "mcp-grants")
_KEY_PREFIX = "kiro.mcp"
_TOKENS_KIND = "tokens"
_CLIENT_KIND = "client"
# The ASCII characters JavaScript's ``String.prototype.trim`` removes. Python's
# ``str.strip()`` with no argument also removes \x1c-\x1f, which the engine keeps.
_JS_TRIM_ASCII = " \t\n\v\f\r"
# SEL label for the name lookup, registered in ``hooks._AUDIT_ONLY_READ_IDS``.
_KAS_GRANT_PRESENCE_READ_ID = "mcp_discovery.kas_grant_presence"


#: Set once the "saving is off because the config could not be read" warning has
#: been logged, so a gateway run says it once instead of on every read.
_OFF_WARNING_GIVEN = threading.Event()
_OFF_WARNING_LOCK = threading.Lock()


def _warn_saving_off(reason: str) -> None:
    """Warn, at most once per gateway run, that sign-ins are not being saved."""
    with _OFF_WARNING_LOCK:
        if _OFF_WARNING_GIVEN.is_set():
            return
        _OFF_WARNING_GIVEN.set()
    logger.warning(
        "MCP sign-ins are not being saved: %s, so remote MCP servers will ask to "
        "sign in again in each new session until the config reads cleanly",
        reason,
    )


def persistence_enabled() -> bool:
    """``agent.kas_persist_mcp_sign_in`` as configured. Blocking; call off the loop.

    On by default, so the read fails CLOSED: a load that raised, or one that came
    back degraded, is OFF. A ``config.json`` or ``config.local.json`` that is
    present but unreadable, or an ``agent`` section the loader discarded, builds
    the field defaults, and the default here says "store tokens" -- the opposite of
    a ``false`` the owner may have written in the file that could not be read.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        cfg = KiroCrewConfig.load()
    except Exception as exc:  # noqa: BLE001
        _warn_saving_off(f"the config could not be read ({type(exc).__name__})")
        return False
    degraded = cfg.degraded_sections
    if (
        cfg._base_unreadable
        or cfg._overlay_unreadable
        or "agent" in degraded
        or DEGRADED_WHOLE_CONFIG in degraded
    ):
        _warn_saving_off("the config read was degraded")
        return False
    return cfg.agent.kas_persist_mcp_sign_in is True


def grant_store_root(data_home: str | Path) -> Path:
    """The grant vault's root (the directory holding ``.vault``) under *data_home*."""
    return Path(data_home).joinpath(*GRANT_STORE_PARTS)


def kas_connection_hash(mcp_url: str) -> str:
    """The engine's connection hash for a url entry with no headers and no oauth.

    That is the only shape Crew sends the engine for a url entry; see the module
    docstring. The caller restricts *mcp_url* to ASCII, where JavaScript's
    ``toLowerCase`` and Python's ``lower`` agree.
    """
    normalized = mcp_url.strip(_JS_TRIM_ASCII).lower()
    return hashlib.sha256(f"{normalized}|".encode("utf-8")).hexdigest()


def kas_tokens_entry_name(mcp_url: str) -> str:
    """The vault entry name the engine stores this server's tokens under."""
    return f"{_KEY_PREFIX}.{kas_connection_hash(mcp_url)}.{_TOKENS_KIND}"


def kas_saved_sign_in_names(mcp_url: str) -> tuple[str, str]:
    """The vault entries one saved sign-in is: its ``tokens`` and its ``client``.

    The two kinds the engine's store persists; a Disconnect removes both, as a
    kiro-cli Disconnect removes its token and registration pair.
    """
    digest = kas_connection_hash(mcp_url)
    return (f"{_KEY_PREFIX}.{digest}.{_TOKENS_KIND}", f"{_KEY_PREFIX}.{digest}.{_CLIENT_KIND}")


def kas_grant_presence(mcp_url: str, *, store_root: Path | None = None) -> bool | None:
    """Whether the KAS grant vault holds a ``tokens`` entry for *mcp_url*.

    ``True``/``False`` are observations; ``None`` means the question could not be
    answered: a non-ASCII url (the engine's normalization is not mirrored for it),
    a linked ``kas``/``mcp-grants``/``.vault`` directory (refused, as the store
    refuses it), or an envelope that cannot be read or parsed. No vault yet is a
    definitive ``False``: nothing was ever stored.

    Blocking (a file read), so async callers use :func:`kas_grant_observed`.
    """
    if not mcp_url.isascii():
        return None
    if store_root is None:
        store_root = grant_store_root(data_home())
    for path in (store_root.parent, store_root, store_root / ".vault"):
        if is_link_or_junction(path):
            return None
    # Stat the envelope here rather than trust ``_load_entries``'s ``exists()``:
    # from Python 3.14 that swallows every OSError and answers "no store", which
    # would turn an unreadable vault into a definitive "Sign-in required".
    try:
        (store_root / ".vault" / "secrets.enc").stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None
    try:
        names = SecretVault(store_root).list_names()
    except (OSError, ValueError, RecursionError):
        return None
    return kas_tokens_entry_name(mcp_url) in names


async def forget_kas_grant(mcp_url: str, *, store_root: Path | None = None) -> None:
    """Delete the engine's ``tokens`` and ``client`` entries for *mcp_url*.

    By name only, so no entry is decrypted. Refuses a linked store directory, as
    the store does. For Crew-owned sign-in taking custody of a url (one grant).
    """
    if not mcp_url.isascii():
        return
    if store_root is None:
        store_root = grant_store_root(data_home())
    root = store_root
    linked = await asyncio.to_thread(
        lambda: any(is_link_or_junction(p) for p in (root.parent, root, root / ".vault"))
    )
    if linked:
        return
    vault = SecretVault(store_root)
    for kind in (_TOKENS_KIND, "client"):
        await vault.delete(f"{_KEY_PREFIX}.{kas_connection_hash(mcp_url)}.{kind}")


async def kas_grant_observed(mcp_url: str) -> bool | None:
    """:func:`kas_grant_presence` off the loop, SEL-audited when persistence is on.

    Resolve the fail-closed persistence setting off the loop first. When off,
    answer ``False`` without reading the vault or writing an audit event: any
    entries left there are not in use by the engine.

    The probe renders whichever answer comes back, so a definite absence is
    recorded as ``missing`` just as a held grant is recorded as ``success``, the
    posture :func:`kiro_crew.mcp_grant.grant_observed` takes with
    ``audit_absence=True``. A non-ASCII url never reaches the vault and records
    nothing. Best-effort for the same reason as that helper: only a boolean
    crosses this boundary, so a failed audit leaves a warning, not a refusal.
    """
    if not await asyncio.to_thread(persistence_enabled):
        return False
    present = await asyncio.to_thread(kas_grant_presence, mcp_url)
    if present is None and not mcp_url.isascii():
        return None
    if present is True:
        outcome = "success"
    elif present is False:
        outcome = "missing"
    else:
        outcome = "unreadable"
    recorded = await asyncio.to_thread(
        _hooks.emit_internal_read_audit, _KAS_GRANT_PRESENCE_READ_ID, outcome
    )
    if not recorded:
        # The hash, never the url: a configured endpoint can carry a credential
        # in its userinfo or query string, and this line lands in gateway.log.
        logger.warning(
            "KAS grant-presence audit for hash %s could not be recorded; proceeding unaudited",
            kas_connection_hash(mcp_url),
        )
    return present
