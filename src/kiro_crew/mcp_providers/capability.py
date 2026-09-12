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
"""Upper bound on registry entries this provider hands on per call — a
misbehaving edition manager can't flood the fan-out with an unbounded list.

It caps MATCHES, not rows scanned. Capping the rows first made the guard bound
the search WINDOW as well: a registry larger than it was truncated before any
client-side filter ran, so the searchable set was the first 500 rows in whatever
order the manager listed them and every row past it was unreachable. Measured on
an internal registry of 5612 servers: a search for a bundle sorted past the cap
returned only substring noise from the rows inside it.

:meth:`CapabilityProvider.search` also passes the query DOWN to the manager (see
:func:`registry_accepts_query`) so an edition that can filter server-side does
not have to return its whole catalog. That hint is advisory, though — a manager
may narrow its own truncation, or ignore the query entirely — and the reach of
this provider must not depend on it. Matching before the cap is what makes the
two independent: the hint saves the manager work, the local order fixes reach."""


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


def _matches(entry: dict[str, str | bool], needle: str) -> bool:
    """Client-side match for one normalized entry against a lowercased needle.

    One spelling, used by both the pre-cap filter in
    :meth:`CapabilityProvider._list_entries` and :meth:`CapabilityProvider.search`.
    They must agree: the cap is applied to what this returns, so a needle the
    search would have accepted but this rejects is a row search can never see.
    ``fetch_detail`` passes a server id, which is part of the haystack, so the
    row it wants survives the same filter.
    """
    return needle in f"{entry['id']} {entry['title']} {entry['description']}".lower()


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

        Which is why the guard caps MATCHES rather than rows scanned. The rows
        are already materialized — the manager returned the whole list before
        this coroutine resumed — so capping the list first bounded the search
        window instead of the fan-out, and a manager that ignored the hint kept
        the reach the hint was added to fix: on the internal 5612-server
        registry, a bundle sorted past the cap stayed unfindable. Matching
        first costs one :func:`_normalize_row` per row and leaves the guard
        doing the job it is documented to do — bounding what this provider
        hands on.
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
        needle = query.strip().lower() if query else ""
        entries: list[dict[str, str | bool]] = []
        for row in rows:
            entry = _normalize_row(row)
            if entry is None:
                continue
            if needle and not _matches(entry, needle):
                continue
            entries.append(entry)
            if len(entries) >= _LIST_LIMIT_GUARD:
                break
        if needle and len(entries) >= _LIST_LIMIT_GUARD:
            # Now reachable only when the MATCHES overflow the guard, not merely
            # the catalog: results past the cap are invisible to search, so say
            # so instead of reporting a silently partial catalog.
            logger.info(
                "capability registry matched at least %d rows for query %r; "
                "searching the first %d only",
                _LIST_LIMIT_GUARD,
                query,
                _LIST_LIMIT_GUARD,
            )
        return entries

    async def search(self, query: str, *, limit: int = 20) -> list[McpSearchResult]:
        """List the edition registry and filter client-side.

        The needle is also passed DOWN to the manager, which may filter
        server-side. The client-side pass stays regardless, so a manager that
        ignores the hint is still correct — and ``_list_entries`` applies the
        same match before its guard, so an ignored hint no longer costs reach
        either."""
        needle = query.strip().lower()
        if not needle:
            return []
        results: list[McpSearchResult] = []
        for entry in await self._list_entries(needle):
            if not _matches(entry, needle):
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
