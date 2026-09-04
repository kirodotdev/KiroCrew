"""Translate a Crew agent's ``allowedTools`` into a KAS inline permissions policy.

kiro-cli and KAS express "do not prompt for this" in two different currencies.
kiro-cli takes a flat allowlist of TOOL NAMES (``allowedTools``); KAS takes a
rules list keyed by CAPABILITY plus a resource glob::

    {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}

KAS declares ``allowedTools`` a CLI-only field, so the name has no counterpart —
but the CAPABILITY it is trying to express does, and that makes the translation
mechanical rather than a guess. Two properties of KAS's evaluator are what keep
it safe:

* An unmatched request resolves to ``ask``, so a tool this module cannot
  classify keeps prompting. Silence is the fail-closed direction, which is why
  an unmappable entry emits NO rule instead of a broad one.
* ``match`` omitted means "every resource" (KAS defaults it to ``['**']``).
  That is exactly what a kiro-cli ``allowedTools`` entry means — auto-approve
  the tool whatever it is pointed at — so the omission is faithful, not lax.

The vocabulary is deliberately narrow. A tool-name allowlist carries no resource
pattern, so every rule derived from it is unscoped — "allow the ``shell``
capability for any command" is the faithful translation of auto-approving
``execute_bash``, and that is precisely why the shell and filesystem families are
refused rather than translated (see :data:`WITHHELD_FROM_AUTO_APPROVE`). Auto-
approval means no permission request, and no permission request means the deny
floor and the sensitive-path check never run.

Both consumers use this one module so the wire projection
(``kas_agents.to_client_custom_agent``) and the on-disk spec Crew writes
(``agent.rebuild_agent_config``) cannot drift into disagreeing about what a
given ``allowedTools`` list means.

``allowedTools`` is also the ONLY input either consumer derives from. A
``permissions`` block already present in a spec is never read back and never
translated: it has not passed Crew's governance ceiling, and an auto-approved
call never reaches Crew's permission callback, so relaying one would route around
the deny-list and the audit trail at once. On disk such a block is left untouched
(it is the user's file, and it applies when Crew is not injecting an agent); on
the wire it is simply not Crew's to forward.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Marks an MCP server (or one of its tools) in a Crew ``tools``/``allowedTools``
#: entry: ``@server`` for the whole server, ``@server/tool`` for one action.
_MCP_PREFIX = "@"

#: KAS's capability for any MCP-served tool.
_MCP_CAPABILITY = "mcp"

#: Tool name -> KAS capability, mirroring KAS's own tool classification for the
#: built-in tools Crew's specs actually name. Deliberately NOT exhaustive over
#: KAS's table: an entry here is a promise that auto-approving the Crew tool and
#: allowing the KAS capability mean the same thing. Anything absent is treated as
#: unclassifiable and left to prompt (see the module docstring).
CAPABILITY_BY_TOOL: dict[str, str] = {
    # Network.
    "web_fetch": "web_fetch",
    "web_search": "web_search",
    # Sub-agents and skills.
    "invoke_sub_agent": "subagent",
    "disclose_context": "skill",
}

#: Tools this module refuses to translate even though the capability exists.
#:
#: Auto-approval is not just "one fewer prompt" — it is the absence of a
#: permission request, and a Crew control that runs ON that request does not run
#: at all: the deny floor, the sensitive-path check, the ceiling's own last word.
#: For the shell and filesystem families the cost of losing those is the whole
#: blast radius (an arbitrary command, an overwritten file, a credential file
#: read), and the grant they would produce is unscoped, because a tool-name
#: allowlist carries no resource pattern to narrow it with.
#:
#: This is not a behaviour change for anything Crew ships: its own spec
#: deliberately keeps these OUT of ``allowedTools`` (see ``TestTheRealAllowlist``),
#: and on a governed host the ceiling withholds them anyway. What the refusal buys
#: is that a spec which asks for them — from an app manifest, or a hand edit on an
#: ungoverned host — cannot obtain here what it would obtain on kiro-cli. That
#: asymmetry is deliberate and it is the safe direction: no rule means prompt.
WITHHELD_FROM_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        # Filesystem reads.
        "fs_read",
        "read",
        "read_file",
        "grep",
        "glob",
        "code",
        # Filesystem writes.
        "fs_write",
        "fs_append",
        "str_replace",
        "write",
        # Shell.
        "execute_bash",
        "execute_pwsh",
        "control_bash_process",
    }
)


#: Glob syntax KAS's resource matcher honours, and Crew's auto-approve check does
#: not. An entry carrying any of these means one thing to the list it was written
#: on and something wider here, so it is never translated (see
#: :func:`_mcp_pattern`).
_GLOB_METACHARACTERS = frozenset("*?[]{}!")


def _mcp_pattern(entry: str) -> str | None:
    """Resource glob for an ``@server`` / ``@server/tool`` entry.

    KAS addresses an MCP tool as ``<server>/<tool>``, so a bare server becomes a
    one-level glob and a named action becomes an exact match.

    ``None`` for a reference that is not a plain name. Crew's own auto-approve
    check compares ``allowedTools`` entries literally, so ``@*`` on that list
    grants a server actually called ``*`` — nothing. Here it would become the
    pattern ``*/*``, which KAS resolves as every tool on every server: the same
    text would mean "no grant" on one backend and "grant everything" on the
    other. Translation must not be the step that widens a grant, so an entry we
    cannot read as one literal server (optionally one literal tool) is left to
    prompt instead of being guessed at.
    """
    ref = entry[len(_MCP_PREFIX) :]
    if _GLOB_METACHARACTERS.intersection(ref):
        return None
    return ref if "/" in ref else f"{ref}/*"


def _collapse_mcp_patterns(patterns: set[str]) -> list[str]:
    """Drop per-tool patterns already covered by their server's wildcard.

    ``@srv`` and ``@srv/one`` commonly appear together (the broad entry was
    added later and the narrow ones were never cleaned up). Emitting both is
    harmless to the evaluator but misleading to read, and a reviewer comparing
    the policy against the allowlist should not have to work out that one line
    subsumes another.
    """
    servers = {p[:-2] for p in patterns if p.endswith("/*")}
    return sorted(p for p in patterns if p.endswith("/*") or p.split("/", 1)[0] not in servers)


def allowed_tools_to_permissions(
    allowed_tools: Any,
    *,
    agent_id: str = "",
) -> dict[str, Any] | None:
    """Build a KAS inline permissions policy from a Crew ``allowedTools`` list.

    Returns ``None`` when there is nothing to say — no usable entries, or none
    that classify — so callers can omit the field entirely rather than sending
    an empty ``rules`` array. An empty policy and an absent one are NOT
    equivalent to read: absent says "this spec never described auto-approval",
    which is the truth in that case.

    Entries that cannot be classified are reported once, at debug, listing the
    names. They are not an error: they keep prompting, which is the same
    behaviour as having no policy at all, and a WARNING for a tool the user
    never asked to auto-approve would be noise.
    """
    if not isinstance(allowed_tools, list):
        return None

    mcp_patterns: set[str] = set()
    capabilities: set[str] = set()
    unclassified: list[str] = []
    withheld: list[str] = []

    for raw in allowed_tools:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = raw.strip()
        if entry.startswith(_MCP_PREFIX):
            pattern = _mcp_pattern(entry)
            # `None` for a glob; `@` alone or `@/tool` names no server. Neither
            # describes a grant we can honour, so both keep prompting.
            if pattern is None or pattern.startswith("/"):
                unclassified.append(entry)
                continue
            mcp_patterns.add(pattern)
            continue
        if entry in WITHHELD_FROM_AUTO_APPROVE:
            withheld.append(entry)
            continue
        capability = CAPABILITY_BY_TOOL.get(entry)
        if capability is None:
            unclassified.append(entry)
            continue
        capabilities.add(capability)

    if unclassified:
        logger.debug(
            "agent %r: allowedTools entries with no KAS capability, left to prompt: %s",
            agent_id,
            ", ".join(sorted(unclassified)),
        )
    if withheld:
        # Louder than `unclassified`, and separate from it: this one is a policy
        # decision the spec asked us to reverse, so a reader comparing the spec
        # against the projected policy should not have to guess which it was.
        logger.info(
            "agent %r: not auto-approving %s on this backend — an auto-approved call "
            "raises no permission request, so Crew's deny floor and sensitive-path "
            "check would not run for it",
            agent_id,
            ", ".join(sorted(withheld)),
        )

    rules: list[dict[str, Any]] = []
    if mcp_patterns:
        rules.append(
            {
                "capability": _MCP_CAPABILITY,
                "match": _collapse_mcp_patterns(mcp_patterns),
                "effect": "allow",
            }
        )
    # `match` is deliberately omitted: a tool-name allowlist entry carries no
    # resource scope, and KAS reads a missing `match` as every resource.
    rules.extend({"capability": cap, "effect": "allow"} for cap in sorted(capabilities))

    if not rules:
        return None
    return {"rules": rules}
