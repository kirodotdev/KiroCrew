"""Mode plug-in registry, first compatibility seam for S4.

A Mode plug-in is a *data-only* Policy Pack at this stage; it is NOT
arbitrary Python code loaded from the user's config directory.  The registry
owns identity, activation and Type resolution.  Provider calls, Crew tools,
ACP sessions and all concrete execution remain in the shared BYO core.

Registries are intentionally rebuilt on each read so existing user Policy
Pack hot-reload semantics are preserved without persistent mutable state.
"""
from __future__ import annotations

from dataclasses import dataclass

from .modes import BuiltinMode, builtin_mode

from .policy_packs import (
    PolicyDefinition, PolicyPack, PolicyPackError,
    _discover_policy_packs,
)

MODE_PLUGIN_API_VERSION = 1


@dataclass(frozen=True)
class ModePlugin:
    """An installed Mode and its stable set of Type policies."""

    pack: PolicyPack
    declaration: BuiltinMode | None = None

    @property
    def id(self) -> str:
        return self.pack.id

    @property
    def version(self) -> str:
        return self.pack.plugin_version

    @property
    def enabled(self) -> bool:
        return self.pack.enabled

    @property
    def type_ids(self) -> tuple[str, ...]:
        return tuple(policy.id for policy in self.pack.policies)

    def resolve_type(self, type_id: str) -> PolicyDefinition:
        """Fail closed for disabled Modes, even when called outside the UI."""
        if not self.enabled:
            raise PolicyPackError(f"policy pack {self.id!r} is not available yet")
        policy = self.pack.policy(type_id)
        if policy is None:
            raise PolicyPackError(
                f"policy {type_id!r} is not present in pack {self.id!r}"
            )
        return policy


@dataclass(frozen=True)
class ModeRegistry:
    """Immutable per-read view of registered data-only Mode plug-ins."""

    plugins: tuple[ModePlugin, ...]
    errors: tuple[dict[str, str], ...] = ()

    @property
    def packs(self) -> tuple[PolicyPack, ...]:
        return tuple(plugin.pack for plugin in self.plugins)

    def get(self, mode_id: str) -> ModePlugin | None:
        key = str(mode_id or "").strip().lower()
        return next((plugin for plugin in self.plugins if plugin.id == key), None)

    def default(self) -> ModePlugin:
        enabled = [plugin for plugin in self.plugins if plugin.enabled]
        if not enabled:
            raise PolicyPackError("no enabled policy packs are installed")
        declared = [plugin for plugin in enabled if plugin.pack.is_default]
        eligible = declared or enabled
        return sorted(
            eligible,
            key=lambda plugin: (
                plugin.pack.source != "builtin", plugin.id != "tiga", plugin.pack.display_name.lower(), plugin.id,
            ),
        )[0]

    def resolve(self, mode_id: str, type_id: str) -> tuple[PolicyPack, PolicyDefinition]:
        plugin = self.get(mode_id)
        if plugin is None:
            raise PolicyPackError(f"policy pack {mode_id!r} is not installed")
        definition = plugin.resolve_type(type_id)
        return plugin.pack, definition


def load_mode_registry() -> ModeRegistry:
    """Discover installed Modes without code imports or global state mutation."""
    packs, errors = _discover_policy_packs(include_errors=True)
    return ModeRegistry(
        plugins=tuple(
            ModePlugin(pack, builtin_mode(pack.id) if pack.source == "builtin" else None)
            for pack in packs
        ),
        errors=errors,
    )
