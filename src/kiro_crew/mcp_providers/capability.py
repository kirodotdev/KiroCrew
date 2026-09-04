"""Edition-capability registry provider — wraps the CPP ``CapabilityManager`` seam.

The public core carries no package-manager CLI of its own: an *edition* may
install a ``CapabilityManager`` that owns its registry grammar, output parsing,
and error translation. This
provider surfaces that seam inside MCP discovery so a companion edition's
registry shows up next to the official MCP registry with a provider badge.

On the public build the Default manager reports ``available() → False`` and
this provider is simply never registered — external installs only see the
official registry.

The manager is injected as a zero-arg factory rather than imported from the
dashboard layer so ``kiro_crew.mcp_providers`` stays importable standalone
(and tests can hand in a fake without patching module globals).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.mcp_providers.base import (
    McpSearchResult,
    McpServerDetail,
    ProviderUnavailableError,
)
from kiro_crew.mcp_utils import registry_accepts_query

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager

logger = logging.getLogger(__name__)

_LIST_LIMIT_GUARD = 500
"""Upper bound on registry rows consumed per call — a misbehaving edition
manager can't flood the fan-out with an unbounded list.

This bound is why :meth:`CapabilityProvider.search` passes the query DOWN to the
manager (see :func:`_accepts_query`): a registry larger than the guard is
truncated before any client-side filter runs, so without the hint the searchable
window is the first 500 rows in whatever order the manager listed them, and every
row past it is unreachable. Measured on an internal registry of 5612 servers: a
search for a bundle sorted past the cap returned only substring noise from the
rows inside it. The guard stays — a manager that IGNORES the hint is still
bounded, it just keeps the old reach."""


def _normalize_row(row: Any) -> dict[str, str | bool] | None:
    """Normalize one manager registry row to the fields discovery consumes.

    The seam contract says rows conventionally carry ``id``, ``installed``,
    ``title``, ``description`` (plus edition extras we ignore). Defensive:
    rows without a usable ``id`` are skipped, non-string fields coerced.
    """
    if not isinstance(row, dict):
        return None
    server_id = row.get("id", "")
    if not isinstance(server_id, str) or not server_id:
        return None
    title = row.get("title", "")
    description = row.get("description", "")
    return {
        "id": server_id,
        "title": title if isinstance(title, str) else "",
        "description": description if isinstance(description, str) else "",
        # The seam's ``installed`` is truthy-string or bool depending on the
        # edition ("yes" / True) — collapse to bool here.
        "installed": bool(row.get("installed")),
    }


class CapabilityProvider:
    """Discovery provider backed by the edition's ``CapabilityManager``."""

    def __init__(self, manager_factory: Callable[[], "CapabilityManager"]):
        self._manager_factory = manager_factory

    @property
    def name(self) -> str:
        return "capability"

    @property
    def display_name(self) -> str:
        # Edition-neutral badge: matches the dashboard's pluginRegistryName
        # label ("Packages") rather than naming any specific edition backend.
        return "Packages"

    def is_available(self) -> bool:
        try:
            return bool(self._manager_factory().available())
        except Exception:
            return False

    async def _list_entries(self, query: str | None = None) -> list[dict[str, str | bool]]:
        """Registry rows, optionally asking the manager to filter first.

        ``query`` is a HINT, not a contract: a manager may filter server-side,
        narrow its own truncation, or ignore it entirely. Callers must still
        filter the result themselves.
        """
        mgr = self._manager_factory()
        if not mgr.available():
            raise ProviderUnavailableError("capability manager not available")
        if query and registry_accepts_query(mgr.registry):
            rows = await mgr.registry(query=query)
        else:
            rows = await mgr.registry()
        if not isinstance(rows, list):
            return []
        entries: list[dict[str, str | bool]] = []
        for row in rows[:_LIST_LIMIT_GUARD]:
            entry = _normalize_row(row)
            if entry is not None:
                entries.append(entry)
        if query and len(rows) > _LIST_LIMIT_GUARD:
            # Reachable only when the manager ignored the hint or its filter still
            # overflows the guard: results past the cap are invisible to search, so
            # say so instead of reporting a silently partial catalog.
            logger.info(
                "capability registry returned %d rows for query %r; searching the " "first %d only",
                len(rows),
                query,
                _LIST_LIMIT_GUARD,
            )
        return entries

    async def search(self, query: str, *, limit: int = 20) -> list[McpSearchResult]:
        """List the edition registry and filter client-side.

        The needle is also passed DOWN to the manager, which may filter
        server-side — without that a registry larger than
        :data:`_LIST_LIMIT_GUARD` is truncated before this filter ever runs. The
        client-side pass stays regardless, so a manager that ignores the hint is
        still correct."""
        needle = query.strip().lower()
        if not needle:
            return []
        results: list[McpSearchResult] = []
        for entry in await self._list_entries(needle):
            haystack = f"{entry['id']} {entry['title']} {entry['description']}".lower()
            if needle not in haystack:
                continue
            results.append(
                McpSearchResult(
                    id=str(entry["id"]),
                    name=str(entry["title"]) or str(entry["id"]),
                    title=str(entry["title"]),
                    description=str(entry["description"]),
                    provider=self.name,
                    version="",
                    repo_url="",
                    installed=bool(entry["installed"]),
                    methods=["capability"],
                    deprecated=False,
                )
            )
            if len(results) >= limit:
                break
        return results

    async def fetch_detail(self, server_id: str) -> McpServerDetail | None:
        """Find one registry entry by id. install_plan is always None — the
        edition manager owns the install recipe (``install_mcp``), so there
        is no spec to preview core-side.

        The id doubles as the query hint: on a registry larger than
        :data:`_LIST_LIMIT_GUARD` an unfiltered listing may not contain the row
        the user just clicked in search results, which would 404 a server that
        exists."""
        for entry in await self._list_entries(server_id):
            if entry["id"] == server_id:
                return McpServerDetail(
                    id=str(entry["id"]),
                    name=str(entry["title"]) or str(entry["id"]),
                    title=str(entry["title"]),
                    description=str(entry["description"]),
                    provider=self.name,
                    install_plan=None,
                    required_env=[],
                )
        return None
