"""Authorization-axis status for Connections providers.

This reports the one fact the card is otherwise blind to: whether kiro-cli holds
an OAuth grant for a provider. It is deliberately NOT a reachability probe. The
dashboard already learns whether an endpoint answers from ``/api/mcp`` (a real
kiro-cli handshake), and an unauthenticated 401 challenge there is
indistinguishable from a healthy-but-unauthorized server -- which is exactly how
a provider a user authorized OUTSIDE the dashboard came to render as
``needs_auth``. Grant presence resolves that ambiguity, so this module answers
"is there a grant?" and leaves "does the endpoint answer?" to the existing probe.

Grant presence is a local, network-free stat of kiro-cli's OAuth artifact
directory (never a token read), so no HTTP request is made here and no warm
runtime is involved: the intermediate mint tiers that need one live in a seam
that is not present on this runtime.

``connectedSince`` is a persisted first-authorization timestamp, not a value
invented at render time. It is stamped once when a provider is first observed to
hold a grant, kept in a small sidecar, and forgotten the moment the grant is
gone -- so it self-heals whichever route removed the connection (card Disconnect,
the MCP Servers table, a hand edit) and a reconnect starts a fresh clock.
``accountLabel`` is deliberately absent: Kiro Crew never sees a provider
credential, so there is no truthful identity to report without runtime support
the installed kiro-cli does not expose.

INVARIANT: no filesystem operation here runs on the event loop. The grant stats
and the sidecar read/write both go through ``asyncio.to_thread`` -- either can
sit on a network mount, where a stat is unbounded rather than sub-millisecond,
and a stall on the loop takes every other request with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from kiro_crew import mcp_grant
from kiro_crew.config.loader import data_home
from kiro_crew.connections.registry import Provider, get_visible_providers

logger = logging.getLogger(__name__)

# kiro-cli holds a grant for this provider. Authorization only: the card still
# gates its green badge on the endpoint answering (``/api/mcp`` server status).
STATUS_CONNECTED = "connected"
# No grant, but a mint is in flight -- the user is partway through consent.
STATUS_AWAITING_CONSENT = "awaiting_consent"
# No grant and nothing pending.
STATUS_NOT_CONNECTED = "not_connected"

_STATUS_SCHEMA_VERSION = 1

#: SEL read-id for the acted-on grant observation (first-connect stamping).
#: Distinct from the mint engine's id so the trail says which surface looked.
_GRANT_PRESENCE_READ_ID = "connections_status.oauth_grant_presence"
_STATE_FILE_NAME = "connected-since.json"

# Opt-in override for tests; None means the live data home. Resolved per call so
# an override set after import is honoured and pod isolation is preserved.
_CONNECTION_STATE_PATH: Path | None = None

#: Serializes every reconcile's load-modify-write over the connected-since
#: record. A ``threading.Lock`` (not asyncio's): the reconcile runs in
#: ``asyncio.to_thread`` worker threads, and overlapping polls from separate
#: dashboard tabs are exactly the writers that must queue.
_RECONCILE_LOCK = threading.Lock()

#: The honest record computed by the most recent reconcile whose sidecar write
#: FAILED -- what the file *should* say but does not. While set, IT (never the
#: stale file) is the baseline the next reconcile reads, so an entry that a
#: confirmed absence removed cannot be resurrected from the file it could not
#: be pruned from: a re-authorization starts the fresh clock the module's
#: contract promises instead of re-serving the pre-revocation date. Cleared on
#: the first successful save, which rewrites the whole record and cures every
#: divergence at once. Guarded by ``_RECONCILE_LOCK``. Process-local by design
#: (the reviewer-accepted boundary): across a restart the memory is gone and a
#: still-unwritable file wins again -- no second persistence channel exists to
#: do better, and the window needs the sidecar to STAY unwritable from the
#: revocation until the restart.
_UNPERSISTED_RECORD: dict[str, str] | None = None


class ConnectionStatus(TypedDict, total=False):
    """One visible provider's authorization verdict as served to the dashboard.

    ``accountLabel`` is absent by design -- see the module docstring.
    """

    slug: str
    status: str
    reason: str
    grantPresent: bool
    #: True when the grant lookup itself failed, so ``grantPresent: False`` means
    #: "could not look" rather than "absent". Reported so a card never upgrades an
    #: unreadable state into a claim, and so the recorded timestamp is preserved.
    grantIndeterminate: bool
    connectedSince: str


def _connection_state_path() -> Path:
    """Where first-authorization timestamps live: our own state dir."""
    if _CONNECTION_STATE_PATH is not None:
        return _CONNECTION_STATE_PATH
    return data_home() / "connections" / _STATE_FILE_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_connected_since() -> dict[str, str] | None:
    """Read persisted first-connect timestamps; ``None`` when unknowable.

    Tri-state, mirroring ``_artifact_presence``: a MISSING file is an answer
    (nothing was ever written -> empty), a DAMAGED file is the documented
    self-heal (unparseable content is replaceable -> empty), but any other
    ``OSError`` (EACCES/EIO/stalled mount) means the record could not be READ
    -- and "could not look" must never be flattened into "empty", because the
    caller rebuilds the record from this baseline and a successful atomic
    replace would then permanently overwrite every persisted timestamp.
    """
    try:
        raw = json.loads(_connection_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    except OSError:
        return None
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, dict):
        return {}
    recorded: dict[str, str] = {}
    for slug, entry in providers.items():
        if not isinstance(slug, str) or not isinstance(entry, dict):
            continue
        since = entry.get("connected_since")
        if isinstance(since, str) and since:
            recorded[slug] = since
    return recorded


def _save_connected_since(recorded: dict[str, str]) -> bool:
    """Persist the record; ``False`` when the write failed (e.g. read-only home).

    The caller uses the verdict to keep its published output reproducible: a
    timestamp that never reached disk must not be reported, or every poll on a
    read-only home would re-date the connection to that poll's own clock.
    """
    from kiro_crew.agent import _atomic_json_write  # circular import at module scope

    path = _connection_state_path()
    payload = {
        "schema_version": _STATUS_SCHEMA_VERSION,
        "providers": {slug: {"connected_since": since} for slug, since in recorded.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(path, payload)
    except OSError:
        # A read-only home must not fail the status read; the timestamp is
        # supplementary, so the card simply omits connected-since.
        logger.debug("Could not persist connection state %s", path, exc_info=True)
        return False
    return True


def reconcile_connected_since(statuses: list[ConnectionStatus], now: str) -> dict[str, str]:
    """Stamp first-connect times and forget providers whose grant is truly gone.

    Pruning here rather than in a disconnect path keeps the record self-healing:
    once a provider's grant is gone, its timestamp stops being reported on the
    next status read, and a later re-authorization starts a fresh clock. Note the
    condition is the GRANT being gone, not a card action: a local-only Disconnect
    removes the MCP entry while kiro-cli keeps the grant, so the stored timestamp
    survives and reconnecting continues the original clock. The clock only
    restarts once the grant itself has been revoked at the provider or removed
    locally -- which is exactly what the card's Disconnect copy tells the user it
    does not do for them.

    Stamping the first observation (rather than reading kiro-cli's artifact mtime,
    which a token refresh rewrites) is the honest lifecycle answer: it records
    when this connection first became usable.

    An INDETERMINATE grant lookup preserves whatever is stored and stamps nothing
    new. "I could not look" is not evidence of absence, and pruning on it would
    destroy a real timestamp that no later read can reconstruct.

    The whole load-modify-write runs under a module lock. Each status poll calls
    this off-loop via ``asyncio.to_thread``, so two overlapping polls (multiple
    dashboard tabs) otherwise interleave: both load the same record, one stamps
    ``now``, and the other -- loaded before that stamp landed -- writes the record
    back WITHOUT it, re-dating the first connect on the next read. Serializing
    here rather than at the call sites covers every caller, and the atomic file
    write below is no substitute: it makes each write all-or-nothing but does
    nothing about a stale read feeding the second write.

    One invariant governs every failed-write branch: the reconcile's baseline is
    the last HONEST record -- the file when it reflects the last computation,
    the in-memory ``_UNPERSISTED_RECORD`` when the save that would have made it
    so failed. Unpersisted fresh stamps are never published, and a timestamp a
    confirmed absence removed is never re-served from the file the prune could
    not reach. The read side completes the same invariant: an UNREADABLE record
    is unknowable, not empty -- with no baseline the reconcile reports nothing
    and writes nothing, so an I/O error can never launder into an overwrite of
    persisted truth.
    """
    with _RECONCILE_LOCK:
        return _reconcile_connected_since_locked(statuses, now)


def _reconcile_connected_since_locked(statuses: list[ConnectionStatus], now: str) -> dict[str, str]:
    global _UNPERSISTED_RECORD

    on_disk = _load_connected_since()
    if on_disk is None:
        # The record exists but could not be READ. No baseline can be
        # established, so nothing honest can be computed: proceeding with an
        # assumed-empty baseline would re-stamp every connected provider and --
        # since an unreadable file can sit in a perfectly writable directory --
        # OVERWRITE the persisted truth on the save. No output, no write, no
        # stamp, no audit; the next readable poll serves the intact record.
        return {}
    # THE BASELINE INVARIANT -- one rule closing the whole failed-write class
    # rather than one branch of it: the baseline is always the last HONEST
    # record. That is the file when it reflects the last computation, and the
    # in-memory record from the failed save when it does not. Reading the stale
    # file directly is what resurrected a revoked grant's timestamp: the prune
    # never landed, so a later re-authorization found the pre-revocation date
    # and re-served it instead of starting the fresh clock this module's
    # contract promises.
    stored = on_disk if _UNPERSISTED_RECORD is None else _UNPERSISTED_RECORD
    recorded: dict[str, str] = {}
    fresh: set[str] = set()
    for entry in statuses:
        slug = entry["slug"]
        if entry.get("grantIndeterminate"):
            # Carry the existing record untouched; never stamp on a non-observation.
            since = stored.get(slug)
            if since is not None:
                recorded[slug] = since
            continue
        if not entry.get("grantPresent"):
            continue
        since = stored.get(slug)
        if since is None:
            fresh.add(slug)
        recorded[slug] = since or now
    # Dirty-check against the FILE, not the baseline: while the file lags the
    # last honest computation every reconcile keeps retrying the write, which is
    # both the prune retry and what lets the memory baseline retire itself.
    if recorded != on_disk:
        if _save_connected_since(recorded):
            # The save rewrites the entire record, so the file now carries the
            # honest state and every remembered divergence is cured.
            _UNPERSISTED_RECORD = None
        else:
            # The write failed (read-only home). A fresh stamp exists only in
            # this process's memory, so publishing it would hand the card a time
            # no later read can reproduce -- each poll would re-date the
            # connection to its own clock. Baseline-carried entries are honest
            # and stay reportable.
            for slug in fresh:
                recorded.pop(slug, None)
            fresh.clear()
            _UNPERSISTED_RECORD = dict(recorded)
    if fresh:
        # The credential-store observation this module ACTS on: a first-observed
        # grant became a persisted timestamp and a Connected badge. Mirrors the
        # shared ``mcp_grant.grant_observed`` convention -- audited on the acted-on
        # observation only (this module polls, so not once per sweep), best-effort
        # rather than
        # fail-closed because nothing sensitive crosses this boundary (the
        # artifacts are stat-ed, never opened); an SEL outage must not turn the
        # status read into an error, but it does leave a warning behind.
        from kiro_crew import hooks as _hooks  # circular import at module scope

        if not _hooks.emit_internal_read_audit(_GRANT_PRESENCE_READ_ID, "success"):
            logger.warning(
                "grant-presence audit for the status read could not be recorded; "
                "proceeding unaudited"
            )
    return recorded


def _classify(granted: bool | None, mint_state: str, *, shared: bool = False) -> tuple[str, str]:
    """The authorization verdict for one provider, from local facts only.

    ``shared`` marks an UNCLAIMED premint -- a row the warm table minted ahead of any
    click (:mod:`kiro_crew.connections.warm`), holding a URL nobody has asked for. It
    must not read as consent in flight: the page folds every ``awaiting_consent`` slug
    into its waiting set, so one premint sweep flipped EVERY mintable card to the
    in-flight-consent rendering with no user action at all.

    An unclaimed row therefore falls THROUGH to the grant branches rather than
    reporting a status of its own. That is deliberate: the honest verdict for a URL
    nobody claimed is the verdict the card would get with no row at all, and a fourth
    status would be one the frontend has no rendering for. The row is not lost -- it is
    what a Connect ADOPTS (``warm.adopt_shared_mint``), and adoption clears ``shared``,
    at which point the same row reads as awaiting consent because now it truly is.
    """
    if granted:
        return STATUS_CONNECTED, "grant_present"
    if mint_state in ("minting", "waiting") and not shared:
        return STATUS_AWAITING_CONSENT, "mint_in_flight"
    if granted is None:
        # Not a claim that nothing is connected -- a statement that the grant
        # could not be read. The card keeps whatever its reachability probe says
        # rather than being told there is no authorization.
        return STATUS_NOT_CONNECTED, "grant_unreadable"
    return STATUS_NOT_CONNECTED, "no_grant"


def _provider_grant_presence(mcp_url: str) -> bool | None:
    """Tri-state grant presence, resolved by the shared leaf module.

    The derivation lives in :func:`mcp_grant.grant_presence` rather than here
    because the remote probe renders the same three answers, and two spellings of
    "present, absent, or unknowable" over the same artifacts is how one of them
    silently loses the middle one. Here the middle answer is what keeps a
    persisted timestamp that nothing could reconstruct.
    """
    return mcp_grant.grant_presence(mcp_url)


def _grant_presence_map(providers: list[Provider]) -> dict[str, bool | None]:
    """Stat kiro-cli's grant artifacts per provider. Runs off the loop.

    Tri-state, because "no grant" and "could not look" are different answers and
    only the first one may prune a recorded timestamp:

    * ``True``  -- both paired artifacts are present.
    * ``False`` -- they are genuinely absent.
    * ``None``  -- a lookup failed (EACCES, EIO, a stalled mount), so nothing is
      knowable about this provider right now.

    ``grant_present`` is left exactly as it is -- it is shared with the mint
    engine, where a bool is the right and only answer -- so the indeterminacy is
    resolved HERE by :func:`_provider_grant_presence`, against the same paired
    artifacts via the layout's single source (``grant_artifact_paths``).

    Stats only; no artifact is ever opened, so token bytes cannot reach here.
    """
    return {
        provider["slug"]: _provider_grant_presence(str(provider["mcp_url"]))
        for provider in providers
    }


async def collect_connection_statuses() -> list[ConnectionStatus]:
    """Authorization verdict + first-connect time for every visible provider.

    Reachability is intentionally not probed here -- see the module docstring.
    """
    from kiro_crew.connections.mint import pending_mint_for

    providers = get_visible_providers()
    now = _utc_now()
    if not providers:
        # Still reconcile: a provider removed since the last read must have its
        # stale timestamp pruned even when nothing is visible now.
        await asyncio.to_thread(reconcile_connected_since, [], now)
        return []

    # Read on the loop: the mint table is guarded by an asyncio lock, so it must
    # not be touched from a worker thread.
    mint_states: dict[str, str] = {}
    #: Slugs whose row is an UNCLAIMED premint. Carried alongside the state rather
    #: than folded into it, because the row's state is genuinely ``waiting`` -- what
    #: differs is whose wait it is, and only the classifier needs that distinction.
    unclaimed: set[str] = set()
    for provider in providers:
        view = pending_mint_for(provider["slug"])
        mint_states[provider["slug"]] = str(view.get("state") or "") if view else ""
        if view and view.get("shared"):
            unclaimed.add(str(provider["slug"]))

    grants = await asyncio.to_thread(_grant_presence_map, providers)

    statuses: list[ConnectionStatus] = []
    for provider in providers:
        slug = provider["slug"]
        granted = grants.get(slug, False)
        status, reason = _classify(granted, mint_states[slug], shared=slug in unclaimed)
        entry: ConnectionStatus = {
            "slug": slug,
            "status": status,
            "reason": reason,
            # Never None on the wire: the card reads a boolean, and an
            # indeterminate lookup must not read as authorized. The flag below is
            # what distinguishes it from a confirmed absence.
            "grantPresent": granted is True,
        }
        if granted is None:
            entry["grantIndeterminate"] = True
        statuses.append(entry)

    recorded = await asyncio.to_thread(reconcile_connected_since, statuses, now)
    for entry in statuses:
        since = recorded.get(entry["slug"])
        if since:
            entry["connectedSince"] = since
    return statuses
