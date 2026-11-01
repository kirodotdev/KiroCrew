"""Move a Connections entry off a retired endpoint onto the registry URL.

When a provider's ``mcp_url`` changes, the entry its card already wrote keeps the
old URL. The card recognizes its own entry by name AND registry URL, so that
entry stops reading as managed: the card shows the provider as not connected,
Connect collides with the name, and Disconnect matches nothing.

So on each gateway start, right after the listener binds,
:func:`migrate_retired_endpoints` rewrites each slug's entry in the Kiro
Crew-scope ``mcp.json`` from a URL in :data:`RETIRED_ENDPOINTS` to the registry
URL, keeping every other key (``disabled`` included -- a migration is not
consent to run), rebuilds the agent spec, and moves the entry's owner record
(``connections.owner_mcp_servers``) with it. The user signs in once at the new
URL. Only the Kiro Crew scope is rewritten; every other source is the user's
file and is left as it is. A second start writes nothing.

No sign-in file is deleted here. kiro-cli keys each sign-in by URL, so the
retired URL's pair stays on disk until the user presses Disconnect, which
removes it through its own sharer census
(:func:`kiro_crew.connections.ownership.remove_provider_entry`). A start cannot
make that call safely: the census reads only the projects that have an open
chat, so a closed project whose own agent spec still names the retired URL
would lose its sign-in with no action from the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Slug -> retired endpoints: URLs a card entry may hold that the registry does not install.
#: Atlassian's ``v1/mcp/authv2`` serves its v1 tools, which reject the
#: agent-interface scopes the sign-in now carries ("401 scope does not match").
RETIRED_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "atlassian": ("https://mcp.atlassian.com/v1/mcp/authv2",),
}


@dataclass
class EndpointMigration:
    """What one start did for one slug, for the log and the audit record."""

    slug: str
    entry_migrated: bool = False
    owner_record_migrated: bool = False
    agent_spec_rewritten: bool | None = None

    @property
    def changed(self) -> bool:
        return self.entry_migrated or self.owner_record_migrated


def _retired_match(url: object, retired: tuple[str, ...]) -> bool:
    """Whether ``url`` signs in with the same pair as one of ``retired``.

    Compared by sign-in key, not by endpoint: an entry moved here must leave its
    old pair under the key Disconnect revokes (:func:`retired_urls`). A spelling
    with another key -- a trailing slash keeps its own pair -- is left on its URL,
    so its pair is never orphaned behind a v2 entry. A URL whose key cannot be
    proven is not moved.
    """
    from kiro_crew.connections.ownership import _UNPROVABLE, _module_artifact_key

    key = _module_artifact_key(url)
    if not isinstance(key, str) or key == _UNPROVABLE:
        return False
    return key in {_module_artifact_key(r) for r in retired}


def _current_url(slug: str, retired: tuple[str, ...]) -> str | None:
    """The registry URL to move ``slug`` to, or None when there is none.

    None when the registry does not know the provider, or installs a
    retired URL: then there is nothing to move to.
    """
    from kiro_crew.connections.registry import get_provider

    provider = get_provider(slug)
    current = provider.get("mcp_url") if provider else None
    if not isinstance(current, str) or _retired_match(current, retired):
        return None
    return current


def retired_urls(slug: str) -> tuple[str, ...]:
    """The retired endpoints whose sign-in Disconnect also removes for ``slug``.

    Empty unless the registry has moved ``slug`` off them, so a provider whose
    registry URL is still in the table never widens its Disconnect.
    """
    retired = RETIRED_ENDPOINTS.get(slug, ())
    return retired if retired and _current_url(slug, retired) is not None else ()


def _migrate_scope_entry(slug: str, retired: tuple[str, ...], current: str) -> bool:
    """Rewrite the slug's Kiro Crew-scope entry to ``current``; whether it did.

    Caller holds the MCP lock. A file that is missing, malformed or hand-commented
    is not written: a broken file read as empty and written back would drop every
    configured server.
    """
    from kiro_crew.dashboard.handlers.mcp import _atomic_write, _kirocrew_mcp_json
    from kiro_crew.dashboard.handlers.mcp_custom import _load_kirocrew_config_strict

    data = _load_kirocrew_config_strict()
    if not data:
        return False
    servers = data.get("mcpServers")
    entry = servers.get(slug) if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or not _retired_match(entry.get("url"), retired):
        return False
    entry["url"] = current
    _atomic_write(_kirocrew_mcp_json(), data)
    return True


def _migrate_owner_record(
    result: EndpointMigration, retired: tuple[str, ...], current: str
) -> None:
    """Move the slug's ``connections.owner_mcp_servers`` record to ``current``.

    Best effort, like the add's own record: a failed write leaves the entry
    working, just without the in-place sign-in, and is logged.
    """
    from kiro_crew.config.loader import update_config_locked
    from kiro_crew.connections.owner_servers import CONFIG_ROOT_KEY, CONFIG_SERVERS_KEY

    def _mutate(cfg: dict[str, Any]) -> dict[str, Any] | None:
        root = cfg.get(CONFIG_ROOT_KEY)
        servers = root.get(CONFIG_SERVERS_KEY) if isinstance(root, dict) else None
        record = servers.get(result.slug) if isinstance(servers, dict) else None
        if not isinstance(record, dict) or not _retired_match(record.get("url"), retired):
            return None
        record["url"] = current
        result.owner_record_migrated = True
        return cfg

    try:
        update_config_locked(mutate=_mutate)
    except Exception:  # noqa: BLE001 -- the entry write already landed
        result.owner_record_migrated = False
        logger.warning("could not migrate the owner record of %s", result.slug, exc_info=True)


def _migrate_entry(slug: str, retired: tuple[str, ...]) -> EndpointMigration | None:
    from kiro_crew.dashboard.handlers.mcp import _get_mcp_lock_sync

    current = _current_url(slug, retired)
    if current is None:
        return None
    result = EndpointMigration(slug)
    with _get_mcp_lock_sync():
        result.entry_migrated = _migrate_scope_entry(slug, retired, current)
        if result.entry_migrated:
            # The rendered agent spec mirrors the scope entry; rebuild it under
            # the same lock so a session started now loads the new URL.
            from kiro_crew.agent import rebuild_agent_config_reporting

            try:
                _path, result.agent_spec_rewritten = rebuild_agent_config_reporting()
            except Exception:  # noqa: BLE001 -- the entry write already landed
                logger.warning(
                    "agent config rebuild failed after migrating %s", slug, exc_info=True
                )
                result.agent_spec_rewritten = False
    # After the MCP lock, as the add records it: config.json has its own lock.
    _migrate_owner_record(result, retired, current)
    return result


def _run(step: str, results: list[EndpointMigration], run_slug: Any) -> list[EndpointMigration]:
    for slug, retired in RETIRED_ENDPOINTS.items():
        try:
            result = run_slug(slug, retired)
        except Exception:  # noqa: BLE001 -- retried on the next start
            logger.warning("Connections endpoint %s failed for %s", step, slug, exc_info=True)
            continue
        if result is None:
            continue
        results.append(result)
        if result.changed:
            logger.info(
                "Connections endpoint %s for %s: entry=%s owner_record=%s "
                "agent_spec_rewritten=%s",
                step,
                slug,
                result.entry_migrated,
                result.owner_record_migrated,
                result.agent_spec_rewritten,
            )
            _audit(result)
    return results


def migrate_retired_endpoints() -> list[EndpointMigration]:
    """Move each slug's Kiro Crew-scope entry and owner record to the registry URL.

    Blocking file IO under the MCP lock: call it from a worker thread. One slug's
    failure is logged and does not stop the others.
    """
    return _run("migration", [], _migrate_entry)


def _audit(result: EndpointMigration) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="gateway",
            operation="connections_endpoint_migration",
            outcome="ok",
            source="startup",
            resources=(
                f"provider:{result.slug} entry_migrated={result.entry_migrated} "
                f"owner_record_migrated={result.owner_record_migrated}"
            ),
        )
    except Exception:  # noqa: BLE001 -- the log line above already records it
        logger.warning("could not audit the endpoint migration of %s", result.slug, exc_info=True)
