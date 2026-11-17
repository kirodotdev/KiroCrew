"""The manifest ``contributes.memoryProviders`` block: memory an app's backend serves.

An app may offer Crew a memory provider: a fixed JSON contract
(``kiro_crew.memory_provider.contract``) its own supervised backend serves under one
route of its API. The gateway is the only caller (``memory_provider/app_client.py``);
no app code runs in the gateway for it. The provider's id is
``app:<app id>/<provider id>``, and the owner picks it in Settings.

What a declaration can and cannot do is decided here, before anything reads it:

- ``id`` is short lowercase (``^[a-z][a-z0-9-]{0,31}$``) and unique in the app;
- ``route`` is a plain path below the app's own API, never a URL, query or ``..``;
- ``displayName`` (at most 60 characters) and ``description`` (at most 200) are app
  literals the picker shows;
- ``settingsPage`` is an optional page path of the app's own UI (where the provider
  is connected);
- an app declares at most four providers, and a provider needs a process backend.

A malformed block is kept with its errors: ``AppManifest.validate`` refuses the
install, and the reader skips every provider of a block that carries an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_MEMORY_PROVIDERS_PER_APP = 4
MEMORY_PROVIDER_NAME_MAX = 60
MEMORY_PROVIDER_DESCRIPTION_MAX = 200
MEMORY_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
#: A route below the app's own API, the same shape ``agentTools`` routes take, with an
#: optional leading slash.
MEMORY_PROVIDER_ROUTE_RE = re.compile(
    r"^/?[a-z0-9][a-z0-9_-]{0,63}(?:/[a-z0-9][a-z0-9_-]{0,63}){0,3}$"
)
MEMORY_PROVIDER_PAGE_RE = re.compile(r"^/[A-Za-z0-9_\-/]{0,120}$")
_MEMORY_PROVIDER_KEYS = frozenset({"id", "displayName", "route", "description", "settingsPage"})


@dataclass(frozen=True)
class MemoryProviderDecl:
    id: str
    display_name: str
    route: str
    description: str = ""
    settings_page: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "displayName": self.display_name,
            "route": "/" + self.route,
        }
        if self.description:
            out["description"] = self.description
        if self.settings_page:
            out["settingsPage"] = self.settings_page
        return out


@dataclass
class MemoryProvidersConfig:
    providers: list[MemoryProviderDecl] = field(default_factory=list)
    #: Problems found while reading; any one refuses the install. Not serialized.
    errors: list[str] = field(default_factory=list)
    present: bool = False

    @classmethod
    def from_value(cls, value: object) -> MemoryProvidersConfig:
        if value is None:
            return cls()
        if not isinstance(value, list):
            return cls(present=True, errors=["contributes.memoryProviders must be an array"])
        errors: list[str] = []
        providers: list[MemoryProviderDecl] = []
        seen: set[str] = set()
        if len(value) > MAX_MEMORY_PROVIDERS_PER_APP:
            errors.append(
                f"contributes.memoryProviders declares {len(value)} providers; "
                f"at most {MAX_MEMORY_PROVIDERS_PER_APP} are allowed"
            )
        for index, entry in enumerate(value):
            where = f"contributes.memoryProviders[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{where} must be an object")
                continue
            unknown = sorted(set(entry) - _MEMORY_PROVIDER_KEYS)
            if unknown:
                errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            pid = entry.get("id")
            if not isinstance(pid, str) or not MEMORY_PROVIDER_ID_RE.match(pid):
                errors.append(f"{where}.id must match {MEMORY_PROVIDER_ID_RE.pattern}")
                continue
            if pid in seen:
                errors.append(f"{where}.id {pid!r} is declared twice")
                continue
            seen.add(pid)
            name = entry.get("displayName")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{where}.displayName is required")
                continue
            if len(name) > MEMORY_PROVIDER_NAME_MAX:
                errors.append(f"{where}.displayName is longer than {MEMORY_PROVIDER_NAME_MAX}")
                continue
            route = entry.get("route")
            if not isinstance(route, str) or not MEMORY_PROVIDER_ROUTE_RE.match(route):
                errors.append(f"{where}.route must be a plain path below the app's API")
                continue
            description = entry.get("description", "")
            if (
                not isinstance(description, str)
                or len(description) > MEMORY_PROVIDER_DESCRIPTION_MAX
            ):
                errors.append(
                    f"{where}.description must be a string of at most {MEMORY_PROVIDER_DESCRIPTION_MAX}"
                )
                continue
            page = entry.get("settingsPage", "")
            if page and (not isinstance(page, str) or not MEMORY_PROVIDER_PAGE_RE.match(page)):
                errors.append(f"{where}.settingsPage must be a page path such as /settings")
                continue
            providers.append(
                MemoryProviderDecl(
                    id=pid,
                    display_name=name.strip(),
                    route=route.lstrip("/"),
                    description=description.strip(),
                    settings_page=page or "",
                )
            )
        return cls(providers=providers, errors=errors, present=True)

    def to_list(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.providers]

    def validate(self, process_backed: bool) -> list[str]:
        errors = list(self.errors)
        if self.providers and not process_backed:
            errors.append(
                "contributes.memoryProviders needs a process backend (backend.entryPoint): "
                "the gateway calls the provider over the app's own backend"
            )
        return errors

    def usable(self) -> list[MemoryProviderDecl]:
        """The declared providers, or none when the block carries any error."""
        return [] if self.errors else list(self.providers)
