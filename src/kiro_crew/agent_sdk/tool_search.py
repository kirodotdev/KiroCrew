"""MCP Tool Search: the one setting, and the two channels it travels on.

Tool Search defers MCP tool specs: the model gets a compact catalog instead of
every spec each turn, and loads a tool on demand with the ``tool_search``
built-in. The trade only works when that loader is actually in the model's tool
list -- a deferred spec with no loader is not a cheaper tool, it is a missing
one. The two kiro engines make the loader available differently, so the setting
takes a different road to each:

- **kiro-cli's Rust engine** reads it from the workspace ``cli.json`` overlay
  (``toolSearch.*`` keys, written by ``providers.acp._write_tool_search_overlay``)
  and activates deferral only when the agent's ``tools`` also grants
  ``tool_search``. The engine keeps the invariant itself.
- **KAS** reads it from the ACP ``initialize`` request --
  ``clientCapabilities._meta.kiro.settings.toolSearch`` -- and never opens the
  overlay file. It defers every MCP spec whenever ``enabled`` is true, whether or
  not the agent's ``tools`` grants the loader; a spec without the grant is
  simply left without one. So on that channel the invariant is the CLIENT's to
  keep, and :func:`kas_client_meta_settings` keeps it: ``enabled`` is sent as
  true only when the spec grants the loader, and as an explicit false otherwise,
  so a host-side default can never flip a session into "deferred, unreachable".

The thresholds are kiro-cli's own activation floor (deferral starts once the
specs cross ``min_pct`` of the context window OR ``min_tokens``) and are
mirrored as the defaults of ``AgentConfig.tool_search_min_pct`` /
``tool_search_min_tokens``; a test pins the two spellings together. KAS accepts the same two
keys on the wire; whether it honours them as a floor is its own business, and
they are forwarded verbatim either way so one setting means one thing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

__all__ = [
    "TOOL_SEARCH_DEFAULT_MIN_PCT",
    "TOOL_SEARCH_DEFAULT_MIN_TOKENS",
    "TOOL_SEARCH_LOADER_TOOL",
    "ToolSearchSettings",
    "clamp_min_pct",
    "clamp_min_tokens",
    "kas_client_meta_settings",
    "spec_grants_tool_search",
    "with_client_meta_settings",
]

#: kiro-cli's own Tool Search activation thresholds.
TOOL_SEARCH_DEFAULT_MIN_PCT = 5
TOOL_SEARCH_DEFAULT_MIN_TOKENS = 50_000

#: The built-in that loads a deferred spec. Its spelling is the same in an agent
#: spec's ``tools`` list on both kiro engines.
TOOL_SEARCH_LOADER_TOOL = "tool_search"


def clamp_min_pct(value: object) -> int:
    """Coerce a configured percentage into 0..100, falling back to the default."""
    try:
        pct = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return TOOL_SEARCH_DEFAULT_MIN_PCT
    return max(0, min(100, pct))


def clamp_min_tokens(value: object) -> int:
    """Coerce a configured token count to >= 0, falling back to the default."""
    try:
        tokens = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return TOOL_SEARCH_DEFAULT_MIN_TOKENS
    return max(0, tokens)


@dataclass(frozen=True)
class ToolSearchSettings:
    """The operator's Tool Search choice, resolved from ``agent.tool_search*``.

    ``enabled`` is the toggle; the thresholds are already clamped, so a consumer
    can forward them without re-validating.
    """

    enabled: bool
    min_pct: int = TOOL_SEARCH_DEFAULT_MIN_PCT
    min_tokens: int = TOOL_SEARCH_DEFAULT_MIN_TOKENS

    @classmethod
    def from_config(cls, enabled: bool, min_pct: object, min_tokens: object) -> ToolSearchSettings:
        return cls(
            enabled=bool(enabled),
            min_pct=clamp_min_pct(min_pct),
            min_tokens=clamp_min_tokens(min_tokens),
        )


def spec_grants_tool_search(spec: dict[str, Any] | None) -> bool:
    """Whether an agent spec mounts the ``tool_search`` loader once its own
    exclusions are applied.

    Reads the spec the way both engines do: ``"*"`` (the whole ``tools`` value,
    or an entry in it) and ``@builtin`` grant every built-in, otherwise the
    loader must be named -- and ``excludedTools`` is subtracted afterwards, so a
    spec that grants everything and then excludes ``tool_search`` (or everything)
    grants no loader. An absent or malformed list grants nothing -- the same
    answer KAS's projection gives it (``kas_agents._project_tools`` sends an
    empty allowlist), so this predicate cannot say "granted" about a spec that
    ends up with no tools.
    """
    if not isinstance(spec, dict):
        return False
    excluded_raw = spec.get("excludedTools")
    excluded = (
        {t for t in excluded_raw if isinstance(t, str)} if isinstance(excluded_raw, list) else set()
    )
    if TOOL_SEARCH_LOADER_TOOL in excluded or "*" in excluded:
        return False
    raw = spec.get("tools")
    if raw == "*":
        return True
    if not isinstance(raw, list):
        return False
    entries = {t for t in raw if isinstance(t, str)}
    return bool(entries & {"*", "@builtin", TOOL_SEARCH_LOADER_TOOL})


def kas_client_meta_settings(
    settings: ToolSearchSettings | None, *, loader_granted: bool
) -> dict[str, Any]:
    """The ``_meta.kiro.settings`` object KAS should read Tool Search from.

    Empty when no toggle value was threaded in (``settings is None``): the
    channel stays open and says nothing, exactly as before. Otherwise the
    ``toolSearch`` entry is ALWAYS present with an explicit ``enabled`` -- true
    only when the operator turned it on AND the agent's spec grants the loader.
    An unset key would leave the decision to whatever the host defaults to, and
    on KAS a default of "on" for a spec without the loader is the outage this
    gate exists to prevent.
    """
    if settings is None:
        return {}
    return {
        "toolSearch": {
            "enabled": bool(settings.enabled and loader_granted),
            "minPct": settings.min_pct,
            "minTokens": settings.min_tokens,
        }
    }


def with_client_meta_settings(
    capabilities: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    """``capabilities`` with ``settings`` merged into ``_meta.kiro.settings``.

    A deep copy: the input is the harness's shared constant and must stay the
    pristine object every other spawn sends. Existing settings keys are kept
    and the new ones win on collision. Empty ``settings`` returns an equal copy,
    so a caller can apply it unconditionally.
    """
    merged = copy.deepcopy(capabilities)
    meta = merged.setdefault("_meta", {})
    kiro = meta.setdefault("kiro", {})
    existing = kiro.get("settings")
    kiro["settings"] = {**(existing if isinstance(existing, dict) else {}), **settings}
    return merged
