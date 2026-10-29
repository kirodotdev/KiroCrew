"""MCP Tool Search: the one setting, and the two channels it travels on.

Tool Search defers MCP tool specs: the model gets a compact catalog instead of
every spec each turn, and loads a tool on demand with the ``tool_search``
built-in. The trade only works when that loader is actually in the model's tool
list -- a deferred spec with no loader is not a cheaper tool, it is a missing
one. The two kiro engines make the loader available differently, so the setting
takes a different road to each:

- **kiro-cli's Rust engine** reads it from the workspace ``cli.json`` overlay
  (``toolSearch.*`` keys, written by ``providers.acp_cli_overlay._write_tool_search_overlay``)
  and activates deferral only when the agent's ``tools`` also grants
  ``tool_search``. The engine keeps the invariant itself.
- **KAS** reads it from the ACP ``initialize`` request --
  ``clientCapabilities._meta.kiro.settings.toolSearch`` -- and never opens the
  overlay file. Its schema there is ``{minPct, minTokens, neverDefer}``, and its
  deferral path reads only ``neverDefer``: it defers EVERY MCP tool whose
  ``<server>::<tool>`` id is not on that list (or whose server is not one of the
  operator's mandatory servers), whatever ``enabled`` says, and injects its own
  ``tool_load`` / ``tool_call`` pair to reach them. So on that channel the
  invariant is the CLIENT's to keep, and :func:`kas_client_meta_settings` keeps
  it: for a spec that grants no ``tool_search`` it lists every MCP tool the spec
  can call in ``neverDefer`` (:func:`kas_never_defer_ids`), so those tools stay
  direct. ``enabled`` still goes out -- true only when the spec grants the
  loader, an explicit false otherwise -- for a host that does read it.

  A whole-server grant (``@server``) names no tools, and KAS's ``neverDefer``
  has no server-level or wildcard form, so such a grant is expanded from the
  tool list Crew already holds for that server (the last probe's
  ``tools/list``; nothing is imported into the gateway to answer). A server Crew
  holds no list for stays deferred and reachable through KAS's own ``tool_load``: that is
  the host's behaviour without this list, so the gap costs a tool's directness,
  never the tool.

The thresholds are kiro-cli's own activation floor (deferral starts once the
specs cross ``min_pct`` of the context window OR ``min_tokens``) and are
mirrored as the defaults of ``AgentConfig.tool_search_min_pct`` /
``tool_search_min_tokens``; a test pins the two spellings together. KAS accepts the same two
keys on the wire but its deferral path does not read them; they are forwarded
verbatim so one setting means one thing.
"""

from __future__ import annotations

import copy
import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

__all__ = [
    "TOOL_SEARCH_DEFAULT_MIN_PCT",
    "TOOL_SEARCH_DEFAULT_MIN_TOKENS",
    "TOOL_SEARCH_LOADER_TOOL",
    "ToolSearchSettings",
    "clamp_min_pct",
    "clamp_min_tokens",
    "kas_client_meta_settings",
    "kas_never_defer_ids",
    "resume_takes_tool_search_replay",
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


def resume_takes_tool_search_replay(
    *,
    tool_search: bool | None,
    backend: str,
    channel_id: str | None,
    session_key: object,
) -> bool:
    """Whether a resume of this session is a fresh session plus conversation replay.

    A direct dashboard kiro-cli session with Tool Search on cannot restore its
    transcript through native ``session/load``: the loaded transcript comes back
    without Tool Search's activated schemas, so the next inference cannot invoke
    a tool the loader reports as loaded. The provider rebuilds that registry in a
    fresh native session and preserves the Kiro Crew conversation with a replay
    instead (``providers.acp_runtime_start._start_kiro_runtime_impl``). A linked channel
    identity (``channel_id``) and every non-dashboard key keep native resume.

    This is the ONE definition of that decision, and it is pure on purpose: the
    resume prefetch (``chat_runner._eager_spawn``) asks it before any runtime
    exists, because a speculative load the provider replaces with a replay can
    only come back ``resumed=False`` and be refused after a full spawn. It lives
    here, below both callers, because the dashboard may not import the ACP layer
    (``scripts/check_agent_sdk_boundary.py``) and the provider must not copy the
    rule. Whether a resume is attempted at all (a persisted sid, persistent
    memory) is the caller's precondition; this answers only which shape the
    resume takes.
    """
    # Function-level on purpose: ``kiro_crew.messaging`` imports its driver, which
    # imports ``kiro_crew.acp``, whose runtime imports THIS module -- a top-level
    # import here fails with a partially initialized module whenever this module
    # is the first of the three to load.
    from kiro_crew.messaging.link import telemetry_channel_of

    return (
        tool_search is True
        and backend == ACP_BACKEND_KIRO
        and not channel_id
        and telemetry_channel_of(session_key if isinstance(session_key, str) else None)
        == "dashboard"
    )


#: The id separator KAS's ``neverDefer`` matches: it turns a tool's
#: ``@<server>/<tool>`` tag into ``<server>::<tool>`` before the lookup.
_KAS_TOOL_ID_SEPARATOR = "::"


def kas_never_defer_ids(
    spec: dict[str, Any] | None,
    server_tools: Callable[[str], Iterable[str] | None],
) -> list[str]:
    """Every MCP tool *spec* can call, as KAS's ``neverDefer`` ids, sorted.

    KAS keeps a tool direct only when ``<server>::<tool>`` is on that list, so
    each ``tools`` entry is translated: ``@server/tool`` names its one tool;
    ``@server`` and a tool glob (``@server/read_*``) are expanded from
    *server_tools*, which answers the tool names Crew holds for a server, or
    ``None`` when it holds none; ``"*"`` stands for every server the spec
    declares in ``mcpServers``. ``@builtin`` and bare built-in names are not
    MCP tools and are skipped. A server *server_tools* cannot list contributes
    nothing -- KAS then defers it and reaches it through its own loader, which
    is what it does with no list at all.

    ``excludedTools`` is not subtracted: KAS drops an excluded tool before it
    partitions, so an id for it on this list keeps nothing direct.
    """
    if not isinstance(spec, dict):
        return []
    raw = spec.get("tools")
    if raw == "*":
        entries = ["*"]
    elif isinstance(raw, list):
        entries = [t for t in raw if isinstance(t, str)]
    else:
        entries = []
    ids: set[str] = set()

    def expand(server: str, pattern: str) -> None:
        names = server_tools(server)
        for name in names or ():
            if isinstance(name, str) and name and fnmatch.fnmatchcase(name, pattern):
                ids.add(f"{server}{_KAS_TOOL_ID_SEPARATOR}{name}")

    for entry in entries:
        if entry == "*":
            servers = spec.get("mcpServers")
            for server in servers if isinstance(servers, dict) else ():
                if isinstance(server, str) and server:
                    expand(server, "*")
            continue
        if not entry.startswith("@") or entry == "@builtin" or entry.startswith("@builtin/"):
            continue
        server, sep, tool = entry[1:].partition("/")
        if not server:
            continue
        if not sep:
            expand(server, "*")
        elif any(ch in tool for ch in "*?["):
            expand(server, tool)
        elif tool:
            ids.add(f"{server}{_KAS_TOOL_ID_SEPARATOR}{tool}")
    return sorted(ids)


def kas_client_meta_settings(
    settings: ToolSearchSettings | None,
    *,
    loader_granted: bool,
    never_defer: Iterable[str] = (),
) -> dict[str, Any]:
    """The ``_meta.kiro.settings`` object KAS should read Tool Search from.

    Empty when no toggle value was threaded in (``settings is None``): the
    channel stays open and says nothing, exactly as before. Otherwise the
    ``toolSearch`` entry is ALWAYS present with an explicit ``enabled`` -- true
    only when the operator turned it on AND the agent's spec grants the loader.
    An unset key would leave the decision to whatever the host defaults to.

    For a spec that grants no loader, ``neverDefer`` carries *never_defer* (the
    caller's :func:`kas_never_defer_ids`), because KAS's deferral path reads that
    list and not ``enabled``: without it every MCP tool of such a spec would be
    deferred. A spec that grants the loader gets no ``neverDefer``, so its
    deferral is exactly what it was.
    """
    if settings is None:
        return {}
    tool_search: dict[str, Any] = {
        "enabled": bool(settings.enabled and loader_granted),
        "minPct": settings.min_pct,
        "minTokens": settings.min_tokens,
    }
    if not loader_granted:
        tool_search["neverDefer"] = sorted({t for t in never_defer if isinstance(t, str) and t})
    return {"toolSearch": tool_search}


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
