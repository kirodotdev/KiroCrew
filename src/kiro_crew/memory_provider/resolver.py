"""The one selection point: which memory answers a store, for a session.

``route_for(store_id, session_key=...)`` is the only place a provider is chosen. No
caller branches on a provider id: it asks the route whether Crew's own memory
answers (``route.builtin_answers``), whether a provider replaces it
(``route.replaces``) or adds to it (``route.augments``), and acts on that.

With ``memory.provider`` at its default (mode ``builtin``, no id) the answer is the
shared :data:`BUILTIN_ROUTE` and nothing is read but the config already loaded, so
every existing install takes exactly the path it took before this seam existed.

A session's choice is frozen the first time it is asked for, in the session's
metadata record (``memory_provider_snapshot``): a switch applies to new chats and
never mixes two providers in one conversation. A chat created before the last switch
keeps what it started with. Child work reads its parent's record through the same
session key, so it inherits the parent's choice.

A provider that cannot answer -- its app disabled, uninstalled, unhealthy, refused by
governance, missing the owner's egress consent, or asked for under Shared Crew --
gives a route with ``unavailable`` set. Callers then show memory as unavailable;
nothing ever falls back to Crew's own memory in its place.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from kiro_crew.memory_provider.contract import MemoryProvider

logger = logging.getLogger("kiro_crew.memory_provider")

MODE_BUILTIN = "builtin"
MODE_REPLACE = "replace"
MODE_AUGMENT = "augment"
BUILTIN_PROVIDER_ID = "builtin"
SNAPSHOT_FIELD = "memory_provider_snapshot"
GOVERNANCE_SCOPE = "capabilities.memory_provider"

#: Sessions whose frozen choice is kept in this process; past it the oldest goes and
#: is read back from its record when next asked for.
_SNAPSHOT_CACHE_MAX = 512


@dataclass(frozen=True)
class ProviderSnapshot:
    """What a session was frozen to."""

    mode: str
    provider_id: str = ""
    app_version: str = ""
    api_version: int = 0

    def to_record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "id": self.provider_id,
            "app_version": self.app_version,
            "api_version": self.api_version,
        }

    @classmethod
    def from_record(cls, record: object) -> ProviderSnapshot | None:
        if not isinstance(record, dict):
            return None
        mode = record.get("mode")
        if mode not in (MODE_BUILTIN, MODE_REPLACE, MODE_AUGMENT):
            return None
        provider_id = record.get("id")
        api_version = record.get("api_version")
        app_version = record.get("app_version")
        return cls(
            mode=str(mode),
            provider_id=provider_id if isinstance(provider_id, str) else "",
            app_version=app_version if isinstance(app_version, str) else "",
            api_version=(
                api_version
                if isinstance(api_version, int) and not isinstance(api_version, bool)
                else 0
            ),
        )


BUILTIN_SNAPSHOT = ProviderSnapshot(MODE_BUILTIN)


@dataclass(frozen=True)
class MemoryRoute:
    """Which memory answers one store for one caller."""

    mode: str
    provider_id: str = ""
    provider: MemoryProvider | None = None
    #: Why the provider cannot answer (empty when it can, or in ``builtin`` mode).
    unavailable: str = ""
    display_name: str = ""

    @property
    def builtin_answers(self) -> bool:
        """Crew's own memory serves reads and takes writes (``builtin`` and ``augment``)."""
        return self.mode != MODE_REPLACE

    @property
    def replaces(self) -> bool:
        return self.mode == MODE_REPLACE

    @property
    def augments(self) -> bool:
        return self.mode == MODE_AUGMENT

    @property
    def is_builtin(self) -> bool:
        return self.mode == MODE_BUILTIN

    @property
    def name(self) -> str:
        """How prompts and notices name the provider."""
        return self.display_name or self.provider_id or "the memory provider"


BUILTIN_ROUTE = MemoryRoute(MODE_BUILTIN, BUILTIN_PROVIDER_ID, display_name="Crew memory")

_snapshot_lock = threading.Lock()
_snapshots: OrderedDict[str, ProviderSnapshot] = OrderedDict()


def reset_snapshot_cache() -> None:
    """Forget every frozen choice held in this process (tests, and a data-home switch)."""
    with _snapshot_lock:
        _snapshots.clear()


def _load_config(config: Any) -> Any:
    if config is not None:
        return config
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load()


def _chosen(provider: Any) -> tuple[str, str]:
    """``(mode, id)`` as config holds them; anything but strings reads as builtin.

    ``MemoryProviderConfig.from_raw`` only ever produces strings, so a non-string
    is a stand-in config, and it carries no choice.
    """
    mode = getattr(provider, "mode", MODE_BUILTIN)
    provider_id = getattr(provider, "id", "")
    if not isinstance(mode, str) or not isinstance(provider_id, str):
        return MODE_BUILTIN, ""
    return mode, provider_id


def provider_configured(config: Any = None) -> bool:
    """Whether any provider was ever chosen. False on every install that never chose one."""
    mode, provider_id = _chosen(_load_config(config).memory.provider)
    return mode != MODE_BUILTIN or bool(provider_id)


def _store_opts_out(config: Any, store_id: str | None) -> bool:
    if not store_id or store_id == "default":
        return False
    store = config.memory_stores.get(store_id)
    return getattr(store, "provider_mode", "inherit") == MODE_BUILTIN


def _metadata_log() -> Any:
    from kiro_crew.history import ConversationLog

    return ConversationLog()


def _created_at(record: dict) -> float | None:
    from datetime import datetime

    created = record.get("created_at")
    if not isinstance(created, str):
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def session_snapshot(session_key: str, config: Any = None) -> ProviderSnapshot:
    """The choice *session_key* is frozen to, freezing it now if it has none."""
    cfg = _load_config(config)
    with _snapshot_lock:
        cached = _snapshots.get(session_key)
        if cached is not None:
            _snapshots.move_to_end(session_key)
            return cached
    snapshot: ProviderSnapshot | None = None
    record: dict = {}
    log = None
    try:
        log = _metadata_log()
        record = log.get_metadata(session_key) or {}
        snapshot = ProviderSnapshot.from_record(record.get(SNAPSHOT_FIELD))
    except Exception:  # noqa: BLE001 - an unreadable record freezes from config below
        logger.debug("memory provider snapshot unreadable for %s", session_key, exc_info=True)
    if snapshot is None:
        provider = cfg.memory.provider
        created = _created_at(record) if record else None
        if provider.mode == MODE_BUILTIN or (
            created is not None and provider.switched_at and created < provider.switched_at
        ):
            snapshot = BUILTIN_SNAPSHOT
        else:
            snapshot = ProviderSnapshot(provider.mode, provider.id)
            app_version = _provider_app_version(provider.id)
            if app_version:
                snapshot = ProviderSnapshot(provider.mode, provider.id, app_version, 1)
        if log is not None and record:
            try:
                log.update_metadata(session_key, {SNAPSHOT_FIELD: snapshot.to_record()})
            except Exception:  # noqa: BLE001 - the in-process copy still freezes it
                logger.debug("memory provider snapshot not recorded", exc_info=True)
    if not record:
        # No session record (the dashboard's static key, a CLI caller): nothing to
        # freeze, so it follows config on every call, including a switch back.
        return snapshot
    with _snapshot_lock:
        _snapshots[session_key] = snapshot
        while len(_snapshots) > _SNAPSHOT_CACHE_MAX:
            _snapshots.popitem(last=False)
    return snapshot


def _provider_app_version(provider_id: str) -> str:
    try:
        from kiro_crew.memory_provider import app_client
    except ImportError:
        return ""
    return app_client.provider_app_version(provider_id)


def _external_route(
    snapshot: ProviderSnapshot, store_id: str | None, config: Any, session_key: str | None
) -> MemoryRoute:
    from kiro_crew.memory_provider import app_client

    provider = app_client.provider_for_id(snapshot.provider_id)
    if provider is None:
        return MemoryRoute(
            snapshot.mode, snapshot.provider_id, unavailable="the provider app is not available"
        )
    provider.context_timeout = float(config.memory.provider.timeout_secs)
    reason = availability_refusal(provider, store_id, session_key=session_key, config=config)
    return MemoryRoute(
        snapshot.mode,
        snapshot.provider_id,
        provider=None if reason else provider,
        unavailable=reason,
        display_name=provider.display_name,
    )


#: The one refusal the owner clears from the picker, by consenting in the sheet.
CONSENT_REFUSAL = "the owner has not agreed to send memory to this provider"


def availability_refusal(
    provider: MemoryProvider,
    store_id: str | None,
    *,
    session_key: str | None = None,
    config: Any = None,
) -> str:
    """Why *provider* may not answer *store_id* now, or ``""`` when it may."""
    from kiro_crew.memory_provider import consent
    from kiro_crew.memory_provider.contract import ProviderError

    if _shared_crew_on():
        return "external memory providers are not available in a Shared Crew"
    if not _governance_permits(session_key):
        return "external memory providers are blocked by policy"
    try:
        caps = provider.describe()
    except ProviderError as exc:
        return f"the provider did not describe itself ({exc.code})"
    if store_id and store_id != "default" and not caps.multi_store:
        return "the provider keeps one memory and cannot hold this Bot's memory"
    if caps.egress == "remote" and not consent.has_egress_consent(provider):
        return CONSENT_REFUSAL
    return ""


def _shared_crew_on() -> bool:
    try:
        from kiro_crew.shared_crew.preview import preview_enabled

        return bool(preview_enabled())
    except Exception:  # noqa: BLE001 - an unreadable switch reads as on: refuse
        return True


def _governance_permits(session_key: str | None) -> bool:
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            GOVERNANCE_SCOPE, "", session_key=session_key or "", log_warning=False
        )
    except Exception:  # noqa: BLE001 - an unanswerable ceiling denies the external path
        logger.debug("memory provider governance check failed", exc_info=True)
        return False
    return bool(getattr(decision, "permitted", True))


def route_for(
    store_id: str | None,
    *,
    session_key: str | None = None,
    config: Any = None,
) -> MemoryRoute:
    """Which memory answers *store_id* for *session_key* (or for the store, without one).

    *store_id* is the store Crew already resolved for the caller (``""`` or
    ``"default"`` for the global store); the provider never picks it.
    """
    cfg = _load_config(config)
    provider_cfg = cfg.memory.provider
    mode, provider_id = _chosen(provider_cfg)
    if mode == MODE_BUILTIN and not provider_id:
        return BUILTIN_ROUTE
    if _store_opts_out(cfg, store_id):
        return BUILTIN_ROUTE
    if session_key:
        snapshot = session_snapshot(session_key, cfg)
    elif mode == MODE_BUILTIN:
        snapshot = BUILTIN_SNAPSHOT
    else:
        snapshot = ProviderSnapshot(mode, provider_id)
    if snapshot.mode == MODE_BUILTIN or not snapshot.provider_id:
        return BUILTIN_ROUTE
    return _external_route(snapshot, store_id, cfg, session_key)


def builtin_writes_allowed(
    store_id: str | None, *, session_key: str | None = None, config: Any = None
) -> bool:
    """Whether a writer may change Crew's own memory for *store_id*.

    False only while a provider replaces it: Crew's own memory then stays exactly as
    it was, so switching back loses nothing. The secondary writers (change cards,
    task-runner lessons, onboarding import, the built-in apps) ask this and skip
    with a log line.
    """
    return route_for(store_id, session_key=session_key, config=config).builtin_answers


def builtin_background_allowed(config: Any = None) -> bool:
    """Whether Crew's own memory upkeep runs (embedding backfill, dreaming, history prune).

    Off while a provider replaces Crew's memory for the global store, so the data a
    switch back returns to is the data that was there when it was switched away.
    """
    return _chosen(_load_config(config).memory.provider)[0] != MODE_REPLACE


def note_skip(writer: str, store_id: str | None) -> None:
    """The log line a secondary writer leaves when a provider replaces Crew's memory."""
    logger.info(
        "memory provider: %s skipped for store %r; the configured provider replaces "
        "Crew's own memory",
        writer,
        store_id or "default",
    )


def now() -> float:
    return time.time()
