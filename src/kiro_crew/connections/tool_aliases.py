"""Resolve MCP tool-name collisions into kiro-cli ``toolAliases`` declarations.

Two exposed MCP servers may expose the SAME tool name -- Linear and Vercel both
ship ``list_projects``, ``get_project`` and ``list_teams``; Linear, GitHub and
GitLab all ship ``list_issues``. kiro-cli addresses a tool by bare name, so the
second mount SHADOWS the first: one provider's tool becomes unreachable and the
agent silently calls the other provider's. ``toolAliases`` is kiro-cli's remedy
(``{"@server/tool": "new_name"}``); this module decides what goes in it.

GOVERNING DECISION TABLE
========================
Three axes decide every case. Enumerated in full because point-fixes to one axis
kept contradicting another: EXPOSURE (what ``tools`` actually mounts) x IDENTITY
(is this really that provider) x DECLARATION LIFECYCLE (has the registry moved
since the spec was last written).

``renamed`` on the lifecycle axis means the registry renamed a TOOL KEY. An
alias-VALUE rename is unreachable by construction: every alias is pinned to
``<slug>_<tool>`` (see :func:`derived_alias`), so the registry validator rejects
any other spelling.

Outcomes: ``ALIAS`` = rename the colliding exposed tools; ``NONE`` = emit nothing
for this provider; ``STRIP`` = a previously generated ref is removed. STRIP is
orthogonal to ALIAS/NONE and applies on every row, because the generated domain
is recomputed from the registry rather than read back off the spec. WHICH pairs
STRIP may touch is not decided here: it is decided by the persisted record of
what the last pass emitted (:mod:`kiro_crew.connections.alias_record`), so every
STRIP below is read as "strip it IF the record claims it".

IDENTITY = registry-URL-matched (the only identity that can produce an alias)

  #   EXPOSURE            LIFECYCLE   OUTCOME
  1   @slug               current     ALIAS: all declared sources exposed; rename
                                      each name another provider also EXPOSES
  2   @slug               renamed     ALIAS(new) + STRIP(old): the superseded pair
                                      was recorded, so it goes
  3   @slug               removed     NONE + STRIP(old): nothing left to declare
  4   @slug/tool          current     ALIAS only if that ONE tool is also exposed
                                      by another provider -- per-tool exposure is
                                      not provider-wide exposure
  5   @slug/tool          renamed     ALIAS(new) for that tool only + STRIP(old)
  6   @slug/tool          removed     NONE: the ref names a tool the registry no
                                      longer declares, so it is not a source
  7   *                   current     as row 1 for every mounted provider
  8   *                   renamed     as row 2
  9   *                   removed     as row 3
  10  @slug/*             current     as row 1 -- see WILDCARD FAIL-SAFE below
  11  @slug/*             renamed     as row 2
  12  @slug/*             removed     as row 3
  13  allowedTools-only   current     NONE: ``allowedTools`` auto-approves, it
                                      does not MOUNT. A ref absent from the
                                      closed ``tools`` allowlist exposes no tool,
                                      so it cannot collide with anything.
  14  allowedTools-only   renamed     NONE + STRIP(old)
  15  allowedTools-only   removed     NONE + STRIP(old)
  16  absent              current     NONE: not mounted
  17  absent              renamed     NONE + STRIP(old)
  18  absent              removed     NONE + STRIP(old)

IDENTITY = URL-mismatched (rows 19-36)
IDENTITY = custom-server-carrying-a-registry-name (rows 37-54)

  Both collapse to NONE for all 6 exposures x 3 lifecycles, and STRIP still
  applies. Identity is gated BEFORE exposure is consulted, so neither of the
  other two axes can discriminate: a server that is not the provider has no
  claim on that provider's declarations whatever ``tools`` says about it. One
  behaviour is NOT collapsed -- such a server's per-tool refs still RESERVE
  their tool names against generated destinations (see DESTINATION RESERVATION),
  because a name it occupies is occupied regardless of who owns the server.

WILDCARD FAIL-SAFE (rows 10-12): kiro-cli documents ``@server/tool`` and
``@server`` for ``tools`` and globs only for ``allowedTools``, so ``@slug/*`` in
``tools`` may expose everything or nothing. It is read as whole-server because
the two errors are not symmetric: over-reading exposes a tool that is not
mounted, and renaming an unmounted tool is inert; under-reading leaves a real
collision shadowed.

DESTINATION RESERVATION: a generated name must not land on a name already in
use, or the rename recreates the shadowing it exists to remove. Reserved =
surviving hand-authored alias targets + declared natural names of exposed
providers + every tool name named in a per-tool ``tools`` ref of ANY exposed
server (custom servers included) + the builtin names in ``tools``.

  OUT OF SCOPE BY CONSTRUCTION: a custom server mounted WHOLE (``@mycustom``
  with no per-tool refs) publishes its tool names only at runtime, over the
  wire. Nothing in a static spec can name them, so a generated destination
  colliding with one is undetectable HERE by construction -- not merely
  unhandled. It degrades to shadowing (the fail-safe direction) and belongs to a
  runtime tool-list reconciliation, not to spec emission.

INVARIANTS 1-7 are this module's and decide WHICH aliases resolve. The emission
pass carries three of its own (staleness, destination safety, gate-off
inertness); see :func:`kiro_crew.agent._apply_connection_tool_aliases`.

1. **Aliases are registry-sourced only.** Every alias comes from a provider's
   committed ``tool_aliases`` declaration. Nothing is synthesized from a prefix
   rule at emission time, and nothing is learned by probing a live server -- a
   spec rebuild must not depend on network reachability.

2. **Collision detection is deterministic and closed over the EXPOSED set.**
   A tool name collides iff two or more identity-verified providers EXPOSE it.
   Same inputs always give the same output, key order included (sorted).

3. **A non-colliding tool keeps its natural name.** A declaration alone never
   renames anything, and neither does provider-level co-residence: two providers
   mounted per-tool on tools that do not overlap are not a collision.

4. **Exposure means MOUNTED, at tool granularity.** Rows 1-18 above.

5. **Provider identity is proven by endpoint, not assumed from the key.** A
   declaration says "MY tools collide", which is only true of the real provider.
   Eligibility requires the entry's ``url`` to match the registry's pinned
   ``mcp_url``. (The ``x-kirocrew`` provenance marker cannot serve here: by
   design it appears only in files Kiro Crew does not own, and is stripped on the
   way into both the store and the rendered spec -- see
   :mod:`kiro_crew.mcp_provenance`.)

6. **Every failure degrades to shadowing, never to a wrong rename.** An
   unreadable registry, an unknowable custom tool list, a retired endpoint: each
   leaves tool names alone, which is the pre-feature behaviour, not a new defect.

7. **Ownership of an already-written pair is PERSISTED, never inferred from its
   shape.** Cleanup deletes entries out of a file the user also edits, so it needs
   proof that this pass wrote them -- and the shape of a name carries no such
   proof. Three shape rules were tried and each deleted a hand-written alias or
   stranded a real one permanently; the reasoning is recorded in
   :mod:`kiro_crew.connections.alias_record`, which now owns the decision. This
   module therefore computes only what SHOULD be emitted; what may be REMOVED is
   read from that record. :func:`derived_alias` is the name generator, not an
   ownership test.

Eligible providers are identified by registry slug because Connect keys the MCP
store by slug and the emitted ``mcpServers`` key is that slug verbatim.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from kiro_crew.connections.registry import get_all_registry_providers

# kiro-cli spells an MCP tool reference "@<server>/<tool>"; see
# docs/reference/kiro-cli/custom-agents/configuration-reference.md.
_TOOL_REF = "@{server}/{tool}"

# kiro-cli's "every tool" wildcard in a ``tools`` list.
_ALL_TOOLS = "*"

# A per-server "every tool of this server" ref. Read as whole-server exposure --
# see WILDCARD FAIL-SAFE in the module docstring.
_SERVER_WILDCARD = "*"


def declared_tool_aliases() -> dict[str, dict[str, str]]:
    """Return ``{slug: {tool_name: alias}}`` for every provider that declares any.

    Reads the whole registry, not just the visible/launch set: a provider is
    installed because its entry was written to the MCP store, and a launch gate
    closing later must not retroactively un-alias a server the user connected.
    """
    return {
        provider["slug"]: dict(provider["tool_aliases"])
        for provider in get_all_registry_providers()
        if provider.get("tool_aliases")
    }


def declared_provider_urls() -> dict[str, str]:
    """Return ``{slug: normalized mcp_url}`` for providers that declare aliases."""
    urls: dict[str, str] = {}
    for provider in get_all_registry_providers():
        if not provider.get("tool_aliases"):
            continue
        normalized = normalized_endpoint(provider["mcp_url"])
        if normalized is not None:
            urls[provider["slug"]] = normalized
    return urls


def registry_slugs() -> set[str]:
    """Every registry provider slug, whether or not it declares aliases."""
    return {provider["slug"] for provider in get_all_registry_providers()}


def derived_alias(slug: str, tool: str) -> str:
    """The ONE alias this module will ever emit for ``@slug/tool``.

    A pure function of the ref. The registry validator requires every declared
    alias to equal this, so the declaration stays readable (a reader sees the
    exact name that will appear) while the emission side keeps a total function to
    compare against.

    It is NOT an ownership test. That this pass WOULD emit a given name is no
    evidence that it DID -- a user may hand-write the same string for a provider
    the registry says nothing about. Ownership is decided only by the persisted
    record in :mod:`kiro_crew.connections.alias_record`.
    """
    return f"{slug}_{tool}"


def normalized_endpoint(value: object) -> str | None:
    """Return a comparable form of an MCP endpoint URL, or None if unusable.

    Case-folds scheme and host and drops a trailing slash, so the registry's
    ``https://api.githubcopilot.com/mcp/`` matches a stored
    ``https://api.githubcopilot.com/mcp``. Query is kept significant (it can
    select a different server) and the fragment is dropped (it cannot).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    # An explicitly written scheme-default port names the SAME endpoint, so it must
    # not fail the identity check -- a provider mounted at
    # ``https://host:443/path`` would otherwise be treated as unverified and get no
    # aliases at all, leaving the collision this pass exists to remove. A
    # non-default port stays significant: it can select a different server.
    scheme = parts.scheme.lower()
    host = parts.hostname
    if parts.port is not None and parts.port != {"http": 80, "https": 443}.get(scheme):
        host = f"{host}:{parts.port}"
    # Userinfo is preserved verbatim rather than case-folded: only the host is
    # case-insensitive, and a credential is not.
    userinfo = parts.netloc.rpartition("@")[0]
    return urlunsplit(
        (scheme, f"{userinfo}@{host}" if userinfo else host, parts.path.rstrip("/"),
         parts.query, "")
    )


def _parse_tool_refs(tools: Iterable[object]) -> tuple[bool, dict[str, set[str] | None]]:
    """Split a ``tools`` list into ``(global_wildcard, {server: tools or None})``.

    A ``None`` value means whole-server exposure (``@slug`` or ``@slug/*``); a set
    lists the individually named tools. Builtins carry no ``@`` and are ignored.
    """
    global_all = False
    per_server: dict[str, set[str] | None] = {}
    for ref in tools:
        if not isinstance(ref, str):
            continue
        if ref == _ALL_TOOLS:
            global_all = True
            continue
        if not ref.startswith("@"):
            continue
        server, _, tool = ref[1:].partition("/")
        if not server:
            continue
        if not tool or tool == _SERVER_WILDCARD:
            per_server[server] = None
            continue
        if per_server.get(server, set()) is None:
            continue
        per_server.setdefault(server, set()).add(tool)  # type: ignore[union-attr]
    return global_all, per_server


def exposed_server_keys(tools: Iterable[object]) -> set[str] | None:
    """Server keys the spec's ``tools`` list exposes, or None for the wildcard."""
    global_all, per_server = _parse_tool_refs(tools)
    return None if global_all else set(per_server)


def statically_visible_tool_names(tools: Iterable[object]) -> set[str]:
    """Tool names named in a per-tool ``tools`` ref of ANY server.

    Deliberately scans the refs itself rather than reusing
    :func:`_parse_tool_refs`: that helper resolves EXPOSURE, where a whole-server
    ref supersedes its per-tool siblings (``@mycustom`` already covers
    ``@mycustom/x``, so the narrower ref adds nothing to what is mounted).
    Reservation asks a different question -- is this NAME occupied -- and the
    answer does not depend on how the owning server's exposure resolves. With
    ``["@mycustom", "@mycustom/vercel_list_projects"]`` the precedence rule
    discards the explicit name, and a generated alias would then land straight on
    a tool the user pointed at by hand.

    Includes custom servers: a name a custom mount occupies is occupied whoever
    owns the server. Only per-tool refs are visible this way -- see the OUT OF
    SCOPE note for a custom server mounted whole.
    """
    names: set[str] = set()
    for ref in tools:
        if not isinstance(ref, str) or not ref.startswith("@") or "/" not in ref:
            continue
        tool = ref.split("/", 1)[1]
        if tool and tool != _SERVER_WILDCARD:
            names.add(tool)
    return names


def exposed_declared_tools(
    servers: Mapping[str, Any], tools: Iterable[object]
) -> dict[str, frozenset[str]]:
    """Return ``{slug: exposed declared source tools}`` per identity-verified provider.

    Applies the identity gate (invariant 5) and then exposure at TOOL granularity
    (invariant 4): a whole-server or wildcard ref exposes every declared source,
    a per-tool ref exposes only the named tools that the registry actually
    declares. A provider with an empty exposed set is omitted -- it can neither
    collide nor be collided with.

    Args:
        servers: The emitted ``mcpServers`` mapping.
        tools: The emitted ``tools`` list.
    """
    declarations = declared_tool_aliases()
    provider_urls = declared_provider_urls()
    global_all, per_server = _parse_tool_refs(tools)

    exposed: dict[str, frozenset[str]] = {}
    for slug, spec in servers.items():
        sources = declarations.get(slug)
        if not sources or slug not in provider_urls:
            continue
        if not isinstance(spec, dict):
            continue
        if normalized_endpoint(spec.get("url")) != provider_urls[slug]:
            continue
        if global_all or (slug in per_server and per_server[slug] is None):
            names = frozenset(sources)
        elif slug in per_server:
            names = frozenset(t for t in (per_server[slug] or set()) if t in sources)
        else:
            # Not in ``tools`` at all: rows 13-18. ``allowedTools`` does not mount.
            continue
        # A tool this server DISABLES is not callable, so it cannot collide. Counting
        # it would rename the only reachable holder of that name for nothing: with
        # Linear and GitHub both mounted whole and GitHub's ``list_issues`` disabled,
        # Linear's ``list_issues`` is unambiguous and must keep its natural name.
        disabled = spec.get("disabledTools")
        if isinstance(disabled, list):
            names -= {t for t in disabled if isinstance(t, str)}
        if names:
            exposed[slug] = names
    return exposed


def natural_tool_names(exposed: Mapping[str, Collection[str]]) -> set[str]:
    """Declared (pre-alias) tool names of the exposed providers."""
    return {tool for tools in exposed.values() for tool in tools}


def resolve_tool_aliases(exposed: Mapping[str, Collection[str]]) -> dict[str, str]:
    """Return the ``toolAliases`` map resolving collisions among EXPOSED tools.

    Args:
        exposed: ``{slug: exposed declared source tools}``, normally from
            :func:`exposed_declared_tools`.

    Returns:
        ``{"@server/tool": "alias"}`` for colliding tools only, sorted by key.
    """
    declarations = declared_tool_aliases()

    claimants: defaultdict[str, list[str]] = defaultdict(list)
    for slug in sorted(exposed):
        sources = declarations.get(slug, {})
        for tool in sorted(exposed[slug]):
            if tool in sources:
                claimants[tool].append(slug)

    aliases: dict[str, str] = {}
    for tool, slugs in claimants.items():
        if len(set(slugs)) < 2:
            # Invariant 3: unambiguous, so the tool keeps its natural name.
            continue
        for slug in slugs:
            aliases[_TOOL_REF.format(server=slug, tool=tool)] = declarations[slug][tool]
    return dict(sorted(aliases.items()))
