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

``allowedTools`` is the only input the DERIVED rules come from. A ``permissions``
block the spec's author wrote is a second input, and it travels only as a request
that is INTERSECTED with the ceiling-derived result -- never added to it. The four
rules that decide what survives are in :func:`merge_user_permissions`.

Reading the block is not a softening of the ceiling, it is the fix for the case
the derivation cannot cover: a pure-KAS agent authors ``permissions`` and no
``allowedTools``, so with the block dropped the field reaches the backend absent,
and absent resolves every request to ``ask`` -- a prompt for each of the calls the
author had just described a policy for. On disk the block is left untouched (it is
the user's file, and it applies when Crew is not injecting an agent); on the wire
it passes the same ceiling the derived rules do, or it does not travel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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


#: Glob syntax kiro-cli's ``allowedTools`` matcher documents for the TOOL part
#: of an ``@server/tool`` entry (``@server/read_*``), and KAS's resource matcher
#: reads the same way. Only these two travel; see :data:`_UNSHARED_GLOB_SYNTAX`.
_SHARED_GLOB_SYNTAX = frozenset("*?")

#: Glob syntax KAS's resource matcher honours and kiro-cli's does not document.
#: An entry carrying any of these means one thing to the list it was written on
#: and possibly something wider here, so it is never translated (see
#: :func:`_mcp_pattern`).
_UNSHARED_GLOB_SYNTAX = frozenset("[]{}!")

#: Every character either matcher treats as a glob. A SERVER name carrying any of
#: these is refused outright (see :func:`_mcp_pattern`).
_GLOB_METACHARACTERS = _SHARED_GLOB_SYNTAX | _UNSHARED_GLOB_SYNTAX


def _mcp_pattern(entry: str) -> str | None:
    """Resource glob for an ``@server`` / ``@server/tool`` entry.

    KAS addresses an MCP tool as ``<server>/<tool>``, so a bare server becomes a
    one-level glob, a named action becomes an exact match, and a tool-part glob
    (``@srv/query_*``) travels as written: kiro-cli documents ``*`` and ``?`` in
    the tool part of an ``allowedTools`` entry with exactly the meaning KAS gives
    the same text, so dropping it would make one line of text a grant on one
    backend and a prompt on the other — the asymmetry this module exists to
    avoid.

    ``None`` when the SERVER part is not one literal name. ``@*`` would become
    the pattern ``*/*``, which KAS resolves as every tool on every server, and a
    server-part glob is the one shape kiro-cli's own reading is not pinned down
    for. Translation must not be the step that widens a grant, so an entry we
    cannot read as one literal server is left to prompt instead of being guessed
    at. The tool part may carry only the shared syntax (``*``, ``?``); bracket,
    brace and negation forms are refused for the same reason.
    """
    ref = entry[len(_MCP_PREFIX) :]
    server, slash, tool = ref.partition("/")
    if _GLOB_METACHARACTERS.intersection(server):
        return None
    if _UNSHARED_GLOB_SYNTAX.intersection(tool):
        return None
    return ref if slash else f"{ref}/*"


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


#: KAS's own capability vocabulary (``policy/capabilities.ts``
#: ``VALID_CAPABILITIES``). A user block naming anything else is refused whole: a
#: capability this module cannot place is one it cannot intersect with the ceiling
#: either, and KAS's parser skips such a rule fail-soft, so accepting it here
#: would relay a line that means nothing on either side.
KAS_CAPABILITIES: frozenset[str] = frozenset(
    {
        "all",
        "builtin",
        "filesystem",
        "fs_read",
        "fs_write",
        "shell",
        "web_fetch",
        "web_search",
        "mcp",
        "subagent",
        "skill",
        "power",
        "context",
        "diagnostics",
        "sandbox_network",
    }
)

#: The concrete capabilities KAS's ``builtin`` meta-capability stands for
#: (``policy/capabilities.ts`` ``BUILTIN``), kept in KAS's own order so the two
#: tables can be read side by side.
_BUILTIN_CAPABILITIES: tuple[str, ...] = (
    "fs_read",
    "fs_write",
    "shell",
    "web_fetch",
    "web_search",
    "subagent",
    "skill",
    "power",
    "context",
    "diagnostics",
)

#: Meta capability -> what KAS expands it into (``META_CAPABILITIES``).
#:
#: Expansion is what makes the refusal in :data:`WITHHELD_CAPABILITIES` total.
#: ``{"capability": "all", "effect": "allow"}`` is four words that carry ``shell``
#: and both filesystem capabilities inside them, so a rule read at face value
#: hands over exactly the family this module refuses when it is named outright.
META_CAPABILITY_EXPANSION: dict[str, tuple[str, ...]] = {
    "all": _BUILTIN_CAPABILITIES + (_MCP_CAPABILITY,),
    "builtin": _BUILTIN_CAPABILITIES,
    "filesystem": ("fs_read", "fs_write"),
}

#: :data:`WITHHELD_FROM_AUTO_APPROVE` in KAS's currency: the capabilities a user
#: ``allow`` cannot obtain here, whatever the ceiling says.
#:
#: The argument is the one already made for the tool names -- auto-approval is the
#: absence of a permission request, and Crew's deny floor and sensitive-path check
#: run ON that request -- and it does not weaken when the grant arrives as a
#: capability instead of a tool name. A user ``allow`` naming one of these is
#: dropped, which leaves the capability with no rule, and no rule is ``ask``.
#: This is the one place the merge is deliberately not symmetric with kiro-cli,
#: and it is the direction that keeps Crew's gate in the path.
WITHHELD_CAPABILITIES: frozenset[str] = frozenset({"shell", "fs_read", "fs_write"})

#: Capability -> EVERY ``allowedTools`` ref that asks the governance ceiling about
#: it. Inverted from :data:`CAPABILITY_BY_TOOL` rather than written out, because
#: the ceiling predicate speaks in refs and a user block speaks in capabilities;
#: two hand-kept tables would answer differently the first time one is extended.
#:
#: A TUPLE per capability, and not for symmetry: the source map is many-to-one, so
#: an inversion that keeps one ref per capability silently drops the others the
#: first time a second tool maps to one capability, and then asks the ceiling a
#: question about a ref the author's rule does not cover. Every ref is asked and
#: all must permit, because the capability grants all of them.
#:
#: A capability with no ref here cannot be put to the ceiling at all, and rule 3
#: needs an affirmative answer, so such a rule does not travel.
CEILING_REFS_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    capability: tuple(sorted(t for t, c in CAPABILITY_BY_TOOL.items() if c == capability))
    for capability in sorted(set(CAPABILITY_BY_TOOL.values()))
}

#: The keys KAS's permissions schema defines, at both levels
#: (``services/custom-agents/types.ts`` for disk, ``resolve-client-agents.ts``
#: for the wire -- the same shape twice).
_BLOCK_KEYS = frozenset({"rules", "policies"})
_RULE_KEYS = frozenset({"capability", "match", "exclude", "effect"})

#: The effects KAS's schema enumerates.
_EFFECTS = frozenset({"allow", "deny", "ask"})

#: How a merge reports one permission decision: the refs it is about, the outcome
#: (``withheld`` or ``relayed``) and the reason. Injected, because this module is a
#: leaf and the caller owns the audit contract -- and named as one alias so the
#: call shapes cannot drift apart.
_AuditDecision = Callable[[str, str, str], None]

#: Size bounds on an authored block, over which it is refused whole.
#:
#: Not tidiness. KAS compiles each rule into Cedar statements and its own compiler
#: records that a condition tree past roughly 114 patterns overflows cedar-wasm's
#: stack -- ``match`` patterns are split into one statement each to stay shallow,
#: but ``exclude`` patterns are ANDed onto every statement and are NOT split, so a
#: long exclude list is the shape that still goes deep (``cedar-compiler.ts``
#: ``buildConditionVariants``). A crash there is a failed session, not a refused
#: rule, so the bound belongs on this side where the cost is one log line. The
#: pattern-count cap sits under KAS's own number with room to spare; the rule cap
#: and the length cap bound the projected payload, which travels on every
#: ``session/new``.
_MAX_RULES = 200
_MAX_PATTERNS_PER_RULE = 64
_MAX_PATTERN_LENGTH = 1024


def _refuse_block(
    agent_id: str,
    why: str,
    audit_decision: _AuditDecision = lambda refs, outcome, reason: None,
) -> None:
    """Report a refused ``permissions`` block, at WARNING because it is the
    author's own file being declined rather than a grant quietly narrowed, and in
    the security event log because refusing a block the author wrote withholds
    every grant in it."""
    audit_decision("the authored `permissions` block", "withheld", f"refused: {why}")
    logger.warning(
        "agent %r: refusing its whole `permissions` block -- %s. None of it travels; "
        "the rules derived from `allowedTools` still do, and anything the block "
        "described keeps prompting",
        agent_id,
        why,
    )


def _parse_rule(
    entry: Any,
    index: int,
    agent_id: str,
    audit_decision: _AuditDecision,
) -> dict[str, Any] | None:
    """One rule of a user block, or ``None`` when the whole block must be refused.

    Every failure here is fatal to the BLOCK, not to the rule: a block that is
    half relayed is a policy nobody wrote, and the half that survives is the half
    with no ``deny`` in it. KAS itself is stricter still in the same direction --
    an agent-profile policy it cannot parse fail-closes its engine to deny-all
    (``policy-session.ts`` ``parseAgentPermissions``) -- so refusing to forward is
    also the reading that keeps a malformed block from taking the session with it.

    An unknown key is a refusal too. KAS's schema ignores one, but a key this
    module cannot read may be the half of the rule that NARROWS it, and honouring
    the rest of a rule whose narrowing was dropped is how a grant widens.
    """
    where = f"rule {index}"
    if not isinstance(entry, dict):
        _refuse_block(
            agent_id,
            f"{where} is a {type(entry).__name__}, not an object",
            audit_decision=audit_decision,
        )
        return None
    unknown = sorted(str(key) for key in set(entry) - _RULE_KEYS)
    if unknown:
        _refuse_block(
            agent_id,
            f"{where} carries unknown key(s) {', '.join(unknown)}",
            audit_decision=audit_decision,
        )
        return None
    capability = entry.get("capability")
    if not isinstance(capability, str) or capability not in KAS_CAPABILITIES:
        _refuse_block(
            agent_id,
            f"{where} names no KAS capability ({capability!r})",
            audit_decision=audit_decision,
        )
        return None
    effect = entry.get("effect")
    # `isinstance` BEFORE the membership test, and not for tidiness: an unhashable
    # value (`"effect": ["allow"]`, the shape its array-valued siblings `match` and
    # `exclude` invite) raises `TypeError` out of a `frozenset` lookup, and nothing
    # on the path from here to session creation catches it -- the harness handles
    # only `KasAgentTranslationError` and `ForkGovernanceUnresolved`. A malformed
    # block must cost the block, never the session.
    if not isinstance(effect, str) or effect not in _EFFECTS:
        _refuse_block(
            agent_id,
            f"{where} has effect {effect!r}, not allow/deny/ask",
            audit_decision=audit_decision,
        )
        return None
    rule: dict[str, Any] = {"capability": capability}
    for key in ("match", "exclude"):
        if key not in entry:
            continue
        patterns = entry[key]
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) and p.strip() for p in patterns
        ):
            _refuse_block(
                agent_id,
                f"{where} `{key}` is not an array of patterns",
                audit_decision=audit_decision,
            )
            return None
        if len(patterns) > _MAX_PATTERNS_PER_RULE:
            _refuse_block(
                agent_id,
                f"{where} `{key}` carries {len(patterns)} patterns, over the "
                f"{_MAX_PATTERNS_PER_RULE} KAS's policy compiler stays shallow for",
                audit_decision=audit_decision,
            )
            return None
        if any(len(p) > _MAX_PATTERN_LENGTH for p in patterns):
            _refuse_block(
                agent_id,
                f"{where} `{key}` carries a pattern over {_MAX_PATTERN_LENGTH} characters",
                audit_decision=audit_decision,
            )
            return None
        rule[key] = list(patterns)
    rule["effect"] = effect
    return rule


def parse_user_permissions(
    raw: Any,
    *,
    agent_id: str = "",
    audit_decision: _AuditDecision = lambda refs, outcome, reason: None,
) -> list[dict[str, Any]] | None:
    """Rule 1: the author's ``permissions`` block, read against KAS's own shape.

    ``None`` for an absent block and for a refused one alike -- the caller's
    behaviour is the same either way, and the log line is what separates them.

    A ``policies`` reference refuses the block. It is well formed, and that is the
    problem: KAS expands a named bundle inline into ``allow`` rules held on the
    backend (``policy-parser.ts`` ``expandPolicies`` over ``presets.ts``), so the
    grant is not in this block at all and there is nothing here to intersect with
    the ceiling. The shipped ``dev-shell`` bundle alone carries shell allows,
    which is the one family rule 4 exists to refuse.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _refuse_block(
            agent_id,
            f"a permissions block must be an object, got {type(raw).__name__}",
            audit_decision=audit_decision,
        )
        return None
    unknown = sorted(str(key) for key in set(raw) - _BLOCK_KEYS)
    if unknown:
        _refuse_block(
            agent_id, f"unknown key(s) {', '.join(unknown)}", audit_decision=audit_decision
        )
        return None
    policies = raw.get("policies")
    if policies is not None and not isinstance(policies, list):
        # Tested for TYPE before emptiness: `""` and `0` are falsey, so a truth test
        # alone reads a malformed value as "no bundles" and forwards the rules
        # beside it -- the one shape where a refusal is skipped by accident.
        _refuse_block(
            agent_id, "`policies` must be an array of policy ids", audit_decision=audit_decision
        )
        return None
    if policies:
        _refuse_block(
            agent_id,
            "it references named policy bundles, whose rules live on the backend "
            "where Crew's ceiling cannot see them",
            audit_decision=audit_decision,
        )
        return None
    rules = raw.get("rules")
    if not isinstance(rules, list):
        _refuse_block(agent_id, "`rules` must be an array", audit_decision=audit_decision)
        return None
    if len(rules) > _MAX_RULES:
        _refuse_block(
            agent_id,
            f"it carries {len(rules)} rules, over the {_MAX_RULES} this projection "
            "relays -- the policy travels on every session",
            audit_decision=audit_decision,
        )
        return None
    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(rules):
        rule = _parse_rule(entry, index, agent_id, audit_decision)
        if rule is None:
            return None
        parsed.append(rule)
    return parsed


def _bounded_mcp_servers(match: Any) -> list[str] | None:
    """The literal servers an ``mcp`` rule's ``match`` confines it to.

    ``None`` when the rule is not confined to servers that can be named: no
    ``match`` and an empty one both mean every resource to KAS
    (``cedar-compiler.ts`` ``isUnconditionalMatch``), and a glob in the SERVER
    part means servers that do not exist yet. The ceiling is asked per server, so
    a rule with no nameable server has no question to ask and does not travel.
    """
    if not isinstance(match, list) or not match:
        return None
    servers: list[str] = []
    for pattern in match:
        server = pattern.strip().split("/", 1)[0]
        if not server or _GLOB_METACHARACTERS.intersection(server):
            return None
        servers.append(server)
    return servers


def _deriver_could_emit(rule: dict[str, Any]) -> bool:
    """Whether :func:`allowed_tools_to_permissions` can express this rule itself.

    Exactly the shapes that function emits: an ``allow``, no ``exclude``, and either
    the one ``mcp`` rule whose ``match`` names literal servers, or a bare capability
    rule from :data:`CAPABILITY_BY_TOOL` with no ``match`` at all. Anything else -- a
    ``deny``, an ``ask``, a scoped or excluded ``allow``, a capability that table does
    not carry -- is something a tool-name allowlist cannot say, so it can only have
    been authored.

    Read on the VALUES, not on key presence. ``exclude: []`` excludes nothing and
    ``match: []`` matches every resource (``cedar-compiler.ts``
    ``isUnconditionalMatch``), so both rules mean exactly what the bare form means --
    and a key-presence test would read them as narrowed, hand the grant back to the
    block, and let two empty arrays carry a rule past the channel that owns it.
    """
    if rule["effect"] != "allow" or rule.get("exclude"):
        return False
    capability = rule["capability"]
    if capability == _MCP_CAPABILITY:
        return _bounded_mcp_servers(rule.get("match")) is not None
    return capability in CEILING_REFS_BY_CAPABILITY and not rule.get("match")


def _user_allow_travels(
    rule: dict[str, Any],
    *,
    ceiling_permits: Callable[[str], bool],
    audit_decision: _AuditDecision,
    agent_id: str,
) -> bool:
    """Rules 3 and 4 for one user ``allow``.

    Read over the rule's EXPANSION, so a meta capability is answered for every
    concrete capability inside it and one refusal is enough to drop the rule. The
    rule is never rewritten down to its permitted part: narrowing a grant the
    author wrote and relaying the remainder produces a rule nobody authored.

    Every decision is reported through ``audit_decision`` as well as logged. It is
    a permission DECISION, and it is the ordinary case here rather than an edge one
    -- an authored block is the input this merge exists to read, and a governed host
    withholding a server is routine -- so the trail that the sibling derived path
    emits deliberately (``kas_agents._ceiling_permitted``) must cover this path too.
    The ``reason`` argument is what tells the classes apart in the log: a ceiling
    withhold, Crew's own standing refusal of the shell and filesystem families, and
    a whole-block refusal are three different decisions and a reader of the trail
    has to be able to see which one happened.
    """
    capabilities = META_CAPABILITY_EXPANSION.get(rule["capability"], (rule["capability"],))
    withheld = sorted(set(capabilities) & WITHHELD_CAPABILITIES)
    if withheld:
        logger.info(
            "agent %r: not relaying its `permissions` allow for %r -- %s cannot be "
            "auto-approved on this backend, because an auto-approved call raises no "
            "permission request and Crew's deny floor and sensitive-path check run on "
            "that request. It keeps prompting",
            agent_id,
            rule["capability"],
            ", ".join(withheld),
        )
        audit_decision(rule["capability"], "withheld", "shell/filesystem family, Crew policy")
        return False
    for capability in capabilities:
        if capability == _MCP_CAPABILITY:
            servers = _bounded_mcp_servers(rule.get("match"))
            if servers is None:
                logger.info(
                    "agent %r: not relaying its `permissions` allow for %r -- its "
                    "`match` names no literal server, so the ceiling cannot be asked "
                    "about the servers it would cover",
                    agent_id,
                    rule["capability"],
                )
                audit_decision(
                    rule["capability"], "withheld", "unbounded `match`, ceiling unaskable"
                )
                return False
            refused = sorted({s for s in servers if not ceiling_permits(f"{_MCP_PREFIX}{s}")})
            if refused:
                logger.info(
                    "agent %r: not relaying its `permissions` allow for %r -- the "
                    "governance ceiling withholds auto-approval for %s",
                    agent_id,
                    rule["capability"],
                    ", ".join(refused),
                )
                audit_decision(
                    ", ".join(f"{_MCP_PREFIX}{s}" for s in refused),
                    "withheld",
                    "governance ceiling",
                )
                return False
            continue
        refs = CEILING_REFS_BY_CAPABILITY.get(capability, ())
        if not refs:
            logger.info(
                "agent %r: not relaying its `permissions` allow for %r -- %s has no "
                "tool ref to put to the governance ceiling, and a grant travels only "
                "on an affirmative answer",
                agent_id,
                rule["capability"],
                capability,
            )
            audit_decision(capability, "withheld", "no tool ref, ceiling unaskable")
            return False
        withheld_refs = sorted(ref for ref in refs if not ceiling_permits(ref))
        if withheld_refs:
            logger.info(
                "agent %r: not relaying its `permissions` allow for %r -- the "
                "governance ceiling withholds auto-approval for %s",
                agent_id,
                rule["capability"],
                ", ".join(withheld_refs),
            )
            audit_decision(", ".join(withheld_refs), "withheld", "governance ceiling")
            return False
    return True


def merge_user_permissions(
    derived: dict[str, Any] | None,
    raw: Any,
    *,
    ceiling_permits: Callable[[str], bool],
    audit_decision: _AuditDecision = lambda refs, outcome, reason: None,
    allowlist_present: bool = True,
    agent_id: str = "",
) -> dict[str, Any] | None:
    """The ``allowedTools``-derived policy, with the author's own block folded in.

    Four rules, and the order they are applied in is the order they are numbered:

    1. The block is parsed against KAS's shape and refused whole when it does not
       fit (:func:`parse_user_permissions`). No salvage: a partially accepted
       block is a policy nobody wrote.
    2. A user ``deny`` or ``ask`` travels unconditionally. Narrowing is always
       safe, and an author who writes ``deny`` is entitled to be obeyed even where
       the ceiling would have allowed.
    3. A user ``allow`` travels only when the ceiling permits the same capability
       AND the same resource scope. For an ``mcp`` rule both halves are one
       question -- the resource IS a server, and ``may_skip_gate_now("@server")``
       is the predicate that answers for a server, per-tool rules included. For a
       builtin capability the ref is the tool name the ceiling knows.
    4. The shell and filesystem families do not travel as an ``allow`` at all
       (:data:`WITHHELD_CAPABILITIES`), which leaves them with no rule, and no
       rule is ``ask``.

    ``allowlist_present`` says whether the spec carries an ``allowedTools`` list, and
    it decides which channel owns a grant that BOTH could express. A rule the
    derivation could have emitted itself (:func:`_deriver_could_emit`) is dropped when
    the list is there, because the list is the governed input and it is re-derived on
    every projection -- so a ``permissions`` block that has gone stale against it,
    including one Crew's own seeder wrote and then preserved
    (``agent._seed_kas_permissions``), cannot put a revoked grant back. With no list at
    all there is no derivation to be authoritative, so such a rule is the author's own
    and travels: that is the pure-KAS agent this merge exists for. The residue is named
    rather than hidden -- a spec whose ``allowedTools`` key was deleted outright while a
    stale block stayed behind reads as authored, and for Crew's own managed specs that
    state does not survive the next ``rebuild_agent_config``.

    ``ceiling_permits`` and ``audit_decision`` are injected rather than imported so
    this module stays a leaf that depends on nothing else in the package; the caller
    passes :func:`kiro_crew.platform.governance.may_skip_gate_now`, which fails
    closed, and its own security-event-log writer. ``audit_decision`` defaults to a
    no-op so a caller that only wants the translation is not forced to supply one,
    and it is called with ``(refs, reason)`` -- the refs the ceiling refused and why
    -- rather than with a formatted sentence, so the event's wording belongs to the
    module that owns the audit contract.

    The surviving user rules are placed BEFORE the derived ones. Under KAS's
    evaluator the position carries no meaning: rules compile to Cedar permit /
    forbid statements and the decision is the most restrictive match, ``deny`` over
    ``ask`` over ``allow``, with an unmatched request resolving to ``ask``
    (``policy-engine.ts``, ``cedar-compiler.ts``). The order is chosen so the array
    does not depend on that reading being right -- every derived rule is an
    ``allow``, so putting the author's rules first is also the safe order under a
    first-match-wins evaluator, where a user ``deny`` would otherwise lose to a
    derived ``allow`` it was written to override. One order that is correct under
    both readings beats a proof that only one reading is live.
    """
    rules = parse_user_permissions(raw, agent_id=agent_id, audit_decision=audit_decision)
    if not rules:
        return derived
    kept: list[dict[str, Any]] = []
    for rule in rules:
        if rule["effect"] == "allow":
            if allowlist_present and _deriver_could_emit(rule):
                logger.info(
                    "agent %r: not relaying its `permissions` allow for %r -- this spec "
                    "has an `allowedTools` list, which is the governed input for a grant "
                    "of that shape and is re-derived every projection, so the block "
                    "cannot add one the list does not carry",
                    agent_id,
                    rule["capability"],
                )
                audit_decision(rule["capability"], "withheld", "derivable from `allowedTools`")
                continue
            if not _user_allow_travels(
                rule,
                ceiling_permits=ceiling_permits,
                audit_decision=audit_decision,
                agent_id=agent_id,
            ):
                continue
        kept.append(rule)
    if not kept:
        return derived
    audit_decision(
        ", ".join(f"{r['capability']}:{r['effect']}" for r in kept),
        "relayed",
        "authored `permissions`, ceiling cleared",
    )
    base = list(derived["rules"]) if derived else []
    return {"rules": kept + base}
