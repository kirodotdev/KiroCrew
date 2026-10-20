"""What a warm spec says: its fixed names, its sentinel-first body, and the marks of one of ours.

Warm mode names are FIXED under the cold mint's prefix, so one glob finds them all, and only the
cold engine's ``-<pid>-<8hex>`` suffix tells a warm spec from a cold one whose server is literally
named ``warm-*`` (:func:`_is_stale_warm_spec`). Every body comes through :func:`_warm_spec_body`,
which puts :data:`_WARM_SPEC_SENTINEL` first in the description and adds the ``toolAliases`` the
mounted set needs (:func:`connections_tool_aliases`): the one writer chokepoint the ownership
judge relies on. :func:`_warm_ownership_marks` reads the stock fields off the builder that writer
wraps (``mint._mint_spec_body``), so the marks cannot drift from what is written.

The judge itself (``warm._warm_spec_is_foreign``) and its private-scope reader stay in the
facade, where the link-screen baseline pins them, and the gated spec writer stays beside them as
a seam the runtime calls and tests substitute; this module owns only the text they write and
compare against. The registry's declared tool collisions are read through the facade's
``declared_tool_aliases`` binding at call time (:func:`_warm_facade`), the seam the warm
engine's tests substitute.
"""

from __future__ import annotations

import re
from types import ModuleType
from typing import Any

from kiro_crew.connections.mint import _MINT_AGENT_PREFIX, _MINT_NAME_RE, _mint_spec_body
from kiro_crew.connections.tool_aliases import resolve_tool_aliases
from kiro_crew.mcp_utils import mcp_server_alias


def _warm_facade() -> ModuleType:
    """The warm facade, resolved per call because it holds the names this module reads late.

    Imported here rather than at module scope because the facade imports this module: a
    top-level import would make a cold import of either module circular.
    """
    from kiro_crew.connections import warm  # circular import: the facade composes this module

    return warm


#: Warm specs are FIXED names under the cold mint's prefix, so one glob finds them all. They
#: carry no ``-<pid>-<8hex>`` suffix, which keeps them out of the cold engine's manifest sweep
#: -- and is the only thing telling a warm spec from a cold one whose server is literally named
#: ``warm-*`` (see ``_is_stale_warm_spec``). The character class MUST match the cold engine's: a
#: case only ONE pattern accepts reads as ours and gets its live spec unlinked.
_WARM_AGENT_PREFIX = f"{_MINT_AGENT_PREFIX}warm-"
_WARM_NAME_RE = re.compile(rf"^{re.escape(_WARM_AGENT_PREFIX)}[a-z0-9_.-]+$")
_WARM_BASE_AGENT = f"{_WARM_AGENT_PREFIX}base"
_WARM_ALL_AGENT = f"{_WARM_AGENT_PREFIX}all"
#: Prefixed onto the ``description`` of every spec the warm engine writes, and the mark that makes
#: ownership provable. The other fields the writer fixes (``model``, ``includeMcpJson``,
#: ``prompt``, ``allowedTools``) are STOCK DEFAULTS a hand-written or scaffolded agent
#: plausibly carries, so on their own they judged a user's own spec at a warm path as ours.
#: ``description`` is the only schema-legal field free enough to carry a marker: kiro-cli
#: rejects an unknown spec key, and the agent-spec migration sweep strips bookkeeping keys.
_WARM_SPEC_SENTINEL = "Kiro Crew warm mint spec (machine-written; safe to delete)"


def connections_tool_aliases(server_aliases: list[str]) -> dict[str, str]:
    """``toolAliases`` for a spec mounting ``server_aliases``, or ``{}``.

    kiro-cli exposes MCP tool names RAW, so two mounted servers exporting the same
    name leave only one reachable. The collision set is DECLARED by the registry and
    resolved by :func:`resolve_tool_aliases`, so it is known before consent -- the
    MCP inventory carries no tool list for a server that never authorized.

    KEY SHAPE. The resolver keys by registry SLUG (``@slug/tool``) while this spec mounts
    servers under ``mcp_server_alias(slug)``, and where the two differ kiro-cli applies no
    rename and the collision comes back silently -- so keys are re-pointed at the MOUNTED
    alias here. Every registry slug is slash-free today, making this an identity map that
    holds the shape contract of the spec we WRITE rather than fixing a reachable bug; the
    design note's "Tool-alias key shape" carries the full reasoning.
    """
    declared = _warm_facade().declared_tool_aliases()
    wanted = set(server_aliases)
    mounted = {slug: alias for slug in declared if (alias := mcp_server_alias(slug)) in wanted}
    resolved = resolve_tool_aliases(
        {slug: set(tools) for slug, tools in declared.items() if slug in mounted}
    )
    aliased: dict[str, str] = {}
    for ref, alias in resolved.items():
        # rpartition, not partition: a registry slug may itself contain a slash while a tool
        # name never does, so the LAST separator reliably splits server from tool.
        slug, _, tool = ref.lstrip("@").rpartition("/")
        aliased[f"@{mounted.get(slug, slug)}/{tool}"] = alias
    return dict(sorted(aliased.items()))


def _warm_spec_description(detail: str) -> str:
    """Every description this module writes: the sentinel, then the caller's detail.

    Sentinel-FIRST rather than appended, so the judge tests a prefix. A suffix could be
    truncated by any writer that clips the field, and a prefix a user would have to type
    verbatim to impersonate.
    """
    detail = detail.strip()
    return f"{_WARM_SPEC_SENTINEL} {detail}" if detail else _WARM_SPEC_SENTINEL


def _warm_spec_body(name: str, servers: dict[str, Any], description: str) -> dict[str, Any]:
    """A mint spec body, sentinel-stamped, plus the ``toolAliases`` its mounted set needs.

    THE writer chokepoint: every spec the warm engine puts on disk comes through here, which is
    what lets :func:`_warm_spec_is_foreign` treat the sentinel as present-or-not-ours.
    """
    body = _mint_spec_body(name, servers, _warm_spec_description(description))
    aliases = connections_tool_aliases(list(servers))
    if aliases:
        body["toolAliases"] = aliases
    return body


def _is_stale_warm_spec(stem: str, plan_names: frozenset[str]) -> bool:
    """Whether ``stem`` is a warm spec from a previous plan, safe to unlink.

    Three conjuncts, and the third is not redundant: a COLD mint spec for a server
    literally named ``warm-*`` shares this module's prefix
    (``kirocrew-mint-warm-foo-4821-9ab3c1de``), so prefix plus not-in-plan alone would
    delete a live cold mint's spec and strand a user mid-consent. Only the
    ``-<pid>-<8hex>`` suffix separates the two, over one shared character class.

    Necessary but NOT sufficient to unlink: the name says where a spec of ours would go,
    and :func:`_warm_spec_is_foreign` is what says the file there is one.
    """
    return (
        stem not in plan_names
        and _WARM_NAME_RE.match(stem) is not None
        and _MINT_NAME_RE.match(f"{stem}.json") is None
    )


def _warm_ownership_marks(name: str) -> dict[str, Any]:
    """The fields a warm spec named ``name`` carries no matter which plan wrote it.

    Read off the WRITER rather than restated, so a change to the spec body cannot leave
    the warm engine unable to recognise its own files -- which would turn every rewrite into a
    refusal and hand the process a stale spec. ``mcpServers`` and ``tools`` vary with the
    plan, so neither can carry ownership.

    NECESSARY BUT NOT SUFFICIENT: every value here is a stock default, so a user's own spec
    plausibly matches all four. ``description`` is what discriminates -- not because its
    detail is fixed (it is not) but because its PREFIX is :data:`_WARM_SPEC_SENTINEL`, which
    :func:`_warm_spec_is_foreign` requires on top of these marks.
    """
    probe = _mint_spec_body(name, {}, "")
    return {key: probe[key] for key in ("model", "includeMcpJson", "prompt", "allowedTools")}
