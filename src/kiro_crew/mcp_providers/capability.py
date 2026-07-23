"""Edition-capability registry provider — wraps the CPP ``CapabilityManager`` seam.

The public core carries no package-manager CLI of its own (PR #286 removed the
last internal-CLI residue): an *edition* may install a ``CapabilityManager``
that owns its registry grammar, output parsing, and error translation. This
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

from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.mcp_providers.base import (
    McpSearchResult,
    McpServerDetail,
    ProviderUnavailableError,
)

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager

_LIST_LIMIT_GUARD = 500
"""Upper bound on registry rows consumed per call — a misbehaving edition
manager can't flood the fan-out with an unbounded list."""


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

    async def _list_entries(self) -> list[dict[str, str | bool]]:
        mgr = self._manager_factory()
        if not mgr.available():
            raise ProviderUnavailableError("capability manager not available")
        rows = await mgr.registry()
        if not isinstance(rows, list):
            return []
        entries: list[dict[str, str | bool]] = []
        for row in rows[:_LIST_LIMIT_GUARD]:
            entry = _normalize_row(row)
            if entry is not None:
                entries.append(entry)
        return entries

    async def search(self, query: str, *, limit: int = 20) -> list[McpSearchResult]:
        """List the edition registry and filter client-side (the seam has no
        search parameter — registries behind it are small, hundreds not
        millions)."""
        needle = query.strip().lower()
        if not needle:
            return []
        results: list[McpSearchResult] = []
        for entry in await self._list_entries():
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
        is no spec to preview core-side."""
        for entry in await self._list_entries():
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
