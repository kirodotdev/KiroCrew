"""The KiroCrew Governance Model — archetypes, ceiling, evaluator, loader.

This module is the **decoupled, extensible core** of the two-level governance
model (see ``docs/system-specs/modules/governance.md`` and the design doc).
Governance is defined at two levels resolved by a single rule — *the tightest
boundary wins*:

* **Level 1 — POLICY** (``GovernanceCeiling``): the enterprise security ceiling,
  loaded once at boot from a trust-root path the agent process does not own.
  Once present, the running app and its agent cannot weaken it.
* **Level 2 — PROFILE** (``Profile``): a per-surface / per-app / per-task scope
  that may only *narrow* what policy permits.

The effective permission for any item is ``policy ∩ profile``.

Design invariant — **four archetypes, one algebra each.**  Every governed
control is exactly one of ``ScopedRuleset`` (a named set, opt-in/opt-out),
``OrdinalControl`` (a value on a strictness scale), ``CapabilityGate`` (on/off +
named sub-sets) or ``ScopedMap`` (an allowlist of members each carrying a
posture).

**Why this is decoupled / reusable / extensible** (the explicit acceptance
criterion): the evaluator never branches on a scope *name*.  It composes and
queries controls purely through archetype polymorphism (every archetype exposes
``permits``/``query``), and the two domain-specific concerns that *do* vary —
how an item string matches a pattern (``_MATCHERS``) and how an ordinal's tiers
rank (``_ORDINAL_SCALES``) — live in **enforcer-owned registries**, never in the
policy/profile document.  Adding a new governed scope (a new MCP-server
grouping, a new chat transport, a new capability) is therefore a *data* change:
one row in ``SCOPE_CATALOG`` plus, at most, one matcher/scale registry entry.  No
change to ``resolve`` / ``compose`` / ``assert_governance_floor`` is ever
required — the test suite proves this by registering a synthetic scope and
resolving it with zero evaluator edits.
"""

from __future__ import annotations

import importlib as _importlib
import json
import logging
import sys as _sys
from typing import (
    TYPE_CHECKING,
    Container,
    Dict,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

from kiro_crew import edition
from kiro_crew.config.paths import config_dir  # noqa: F401 - re-export
from kiro_crew.mcp_provenance import is_marked
from kiro_crew.platform.admission import (  # noqa: F401 - re-export
    canonical_signing_bytes,
    ed25519_verify,
    hmac_signature,
    read_policy_trust_root,
)
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_health import mark_governance_incident  # noqa: F401 - re-export
from kiro_crew.platform.guest_paths import (  # noqa: F401 - re-export
    GuestPath,
    guest_also_local,
    guest_norm,
    guest_root,
    guest_unverifiable,
)
from kiro_crew.platform.tool_paths import (  # noqa: F401 - re-export
    TARGET_PATH_KEYS,
    edit_target_candidates,
    is_edit_call,
    target_paths,
)
from kiro_crew.sel import sel
from kiro_crew.session_environment import WORKSPACE_SERVER_NAME, WORKSPACE_TOOL_KINDS  # noqa: F401

# isort: split
from kiro_crew import _facade_mirror
from kiro_crew.platform.governance_parts.archetypes import (
    MODE_ALLOW,
    MODE_DENY,
    CapabilityGate,
    OrdinalControl,
    RulesetLike,
)
from kiro_crew.platform.governance_parts.carriers import GovernanceCeiling, Profile
from kiro_crew.platform.governance_parts.catalog import ORDINAL, SCOPE_CATALOG
from kiro_crew.platform.governance_parts.evaluator import gate_decision, policy_level_control
from kiro_crew.platform.governance_parts.tiers import _POLICY_CACHE_LEAF

logger = logging.getLogger("kiro_crew.platform.governance")


#: Host builtin tool name -> the governed scopes its calls are evaluated under.
#:
#: This is the POLICY-WRITE-TIME counterpart to :func:`classify_tool_args`. The
#: gate classifies a builtin from the real call (``tool_kind`` + ``raw_params``),
#: because that is the only reliable source for a path or a URL — but deciding
#: whether a tool may be put on an auto-approve list happens BEFORE any call
#: exists, so the only thing available is the name. Hence a name map, and hence
#: it lives here next to ``SCOPE_CATALOG`` rather than in a caller: a new governed
#: builtin is a data change in this file, which is the rule AGENTS.md states for
#: scopes. A parallel copy in an app-layer module silently re-opened the
#: auto-approve shortcut for anything the copy had not heard of.
BUILTIN_TOOL_SCOPES: Dict[str, Tuple[str, ...]] = {
    # `code` is the edit/LSP/AST-rewrite tool: it writes files AND can shell out
    # (format/build), and it is itself a governed tool. Mapping it to all three
    # scopes means a ceiling that constrains filesystem.write, commands, OR the
    # tool allow-list withholds its blanket auto-approve, so its edits go through
    # the PreToolUse gate. Missing entirely, it defaulted to auto-approved and a
    # `filesystem.write` ceiling could not reach it — unrestricted edits with no
    # permission request.
    "code": ("commands", "tools", "filesystem.write"),
    "execute_bash": ("commands",),
    "fs_read": ("filesystem.read",),
    "fs_write": ("filesystem.write",),
    "glob": ("filesystem.read",),
    "grep": ("filesystem.read",),
    "web_fetch": ("network.egress",),
    "web_search": ("network.egress",),
}


#: Builtin -> the ``capabilities.*`` gate that also governs it. A capability is
#: not a ``tools`` rule, so the name check below cannot see it: a policy that
#: switches spawning off, or scopes which agents may be spawned, says nothing
#: about the tool NAME ``use_subagent``. An auto-approved spawn raises no
#: permission request, so the per-spawn check at the gate never runs for it --
#: which is why the grant itself is withheld while the capability restricts
#: anything. Every ``allowedTools`` writer asks this predicate, so no backend and
#: no channel (the wire projection, the on-disk spec) can carry the grant.
BUILTIN_TOOL_CAPABILITIES: Dict[str, str] = {"use_subagent": "capabilities.spawn"}


def _capability_restricts(control: object) -> bool:
    """Whether one level's capability gate restricts anything.

    ``None`` (undeclared) is no opinion. A declared gate restricts when it is off
    or carries any scope -- a scope is a ruleset, and even an empty allow-mode one
    denies everything. Only an enabled gate with no scopes permits every use. A
    shape this does not recognise is treated as a restriction.
    """
    if control is None:
        return False
    if not isinstance(control, CapabilityGate):
        return True
    return not control.enabled or bool(control.scopes)


# The scopes whose enforcement is an ALWAYS-ON, ceiling-independent floor applied
# at the PreToolUse gate: sensitive-path blocking for filesystem tools and
# denied-command rules for shell tools. A builtin mapping to any of these must
# never receive a blanket auto-approve (which would skip that floor) — see
# may_skip_gate. `network.egress` is deliberately excluded: it is ceiling-only,
# not an always-on floor, so network builtins may still auto-approve absent a
# ceiling.
_FLOOR_GATE_SCOPES: frozenset[str] = frozenset({"filesystem.read", "filesystem.write", "commands"})


def _ceiling_mentions_mcp_server(ceiling: Optional[GovernanceCeiling], server: str) -> bool:
    """Whether the ``mcp`` ruleset names this server at ANY granularity.

    Reads the ruleset's own patterns rather than probing, because a probe cannot
    see a per-tool rule: ``@srv/delete`` matches only itself, so a sentinel tool
    sails past it. Matches ``@server`` and ``@server/<tool>``, case-insensitively,
    in both the allow and the deny list — an ALLOW-mode ruleset that lists only
    some of a server's tools is also an opinion, and auto-approving the whole
    server would grant the rest.

    Asks the control for its patterns (``declared_patterns``) instead of reading
    ``.allow`` / ``.deny`` off it: a ceiling folded from two policy tiers is an
    ``_AndRuleset``, which carries neither attribute, and an attribute read there
    reports "no rule about this server" for a fold whose halves both name it —
    handing out the one grant that never reaches the gate. The question asked of
    the control is :class:`RulesetLike` conformance rather than "are you one of
    the two shapes that exist today", so an archetype added to this scope later
    is asked rather than silently read as empty.

    A pattern scan deliberately does NOT answer "is this scope governed" — it
    answers "is THIS SERVER named". The empty-allowlist case (allow-mode with no
    patterns = deny-all) produces no patterns to match and is caught by the
    server-level ``gate_decision`` probe in :func:`may_skip_gate` BEFORE this
    runs; see ``_ruleset_has_an_opinion`` for why counting patterns is the wrong
    test for the scope-level question. Do not "fix" this function to return True
    on an empty ruleset: that would tax every server for a rule about one.
    """
    ruleset = policy_level_control(ceiling, "mcp")
    if not isinstance(ruleset, RulesetLike):
        return False
    prefix = f"@{server}".casefold()
    for pattern in ruleset.declared_patterns():
        candidate = str(pattern).strip().casefold()
        if candidate == prefix or candidate.startswith(prefix + "/"):
            return True
    return False


def _ruleset_has_an_opinion(rules: object) -> bool:
    """Whether a scope's ruleset constrains anything at all.

    Counting patterns is NOT the test, and getting that wrong inverts the answer
    in the strictest case there is: under ``mode="allow"`` an EMPTY allowlist is
    the empty set — deny-all — so a ruleset with no patterns is MAXIMALLY
    governed, while a pattern-count check reads it as "no opinion" and hands out
    the auto-approve exemption. See :class:`ScopedRuleset`.

    * allow-mode → always an opinion (an empty allowlist denies everything).
    * deny-mode with patterns → an opinion.
    * deny-mode with no patterns → genuinely permits everything; no opinion.
    """
    if rules is None:
        return False
    mode = str(getattr(rules, "mode", "") or "").strip().lower()
    if mode == MODE_ALLOW:
        return True
    if mode == MODE_DENY:
        return bool(tuple(getattr(rules, "deny", ()) or ()))
    # An unrecognized shape is not proof of safety — treat it as an opinion so
    # the call goes through the gate rather than skipping it.
    return True


def may_skip_gate(ref: str, ceiling: Optional[GovernanceCeiling]) -> bool:
    """Whether an auto-approve entry may bypass the PreToolUse gate.

    The ONE predicate every writer of an ``allowedTools`` list must consult.
    Auto-approve is the single path that never reaches ``hooks.on_tool_call``, so
    an entry written without asking this question is a tool the ceiling can no
    longer refuse — which is exactly the guarantee the governance docs make. Two
    independent writers produce those lists (app-agent materialization and the
    host agent's shared-MCP sync); closing the bypass in one of them protects one
    agent and leaves the other with the hole, so both call this.

    ``ref`` is what actually appears in such a list:

    * ``@server`` / ``@server/tool`` — an MCP reference.
    * a bare tool name (``fs_read``, ``web_fetch``) — a host builtin.

    Coarser than the gate ON PURPOSE. Auto-approve has only whole-server (and
    whole-tool) granularity, while the ceiling may speak per tool, per path or
    per host — arguments that do not exist yet at write time. So the question here
    is only "could the ceiling ever have something to say about this", and if it
    could, the shortcut is declined and the real decision is left to the gate,
    which has the real arguments.

    Returns True (keep the entry) for an ungoverned host, so nothing changes
    without a ceiling — EXCEPT a BUILTIN whose always-on floor (sensitive-path /
    denied-command) is enforced at the gate, which is withheld even with no
    ceiling (see the floor check below). That exception is builtin-only on
    purpose: the sensitive-path / denied-command floor runs at the gate against
    the builtin ``fs_read`` / ``fs_write`` / ``execute_bash`` / ``code`` tool
    arguments. An MCP ``@server`` is a separate process whose file/command access
    the builtin floor never inspects, so on an ungoverned host an ``@server``
    keeps its grant and ONLY a ceiling's argument-derived scopes
    (``filesystem.*`` / ``network.egress``) constrain it — there is no
    ceiling-independent floor to bypass for a server. Returns False on an
    evaluator ERROR: not being able to tell cannot mean "skip the control",
    because there is no later checkpoint on that path — the cost of asking is one
    permission prompt, and no capability is lost.
    """
    # Defensive: a non-string ref (a malformed, hand-edited config) cannot be a
    # valid tool/@server reference — treat it as "do not auto-approve" rather
    # than crash on ref.startswith() below. The list writers also discard such
    # entries, but this keeps the predicate safe for any caller.
    if not isinstance(ref, str):
        return False
    # A builtin whose MANDATORY, ceiling-INDEPENDENT floor lives at the
    # PreToolUse gate must NEVER be auto-approved: auto-approve is the one path
    # that skips hooks.on_tool_call, and that floor (sensitive-path blocking for
    # filesystem tools — ~/.ssh, ~/.aws, ...; denied-command rules for shell
    # tools) is always-on and un-disableable. Skipping it would expose
    # credentials/run denied commands even with NO enterprise ceiling present.
    # Network-only builtins (web_fetch/web_search) carry no such floor, and
    # read-only file tools still auto-approve via hooks AFTER the floor runs — so
    # this only closes the bypass, it does not add prompts.
    # Workspace tools are replacements for floor-gated builtins, including
    # whole-server and wildcard grants that would bypass those same checks.
    if ref.split("/", 1)[0] == f"@{WORKSPACE_SERVER_NAME}":
        return False
    if not ref.startswith("@") and _FLOOR_GATE_SCOPES.intersection(
        BUILTIN_TOOL_SCOPES.get(ref, ())
    ):
        return False
    # An enterprise build has no ungoverned host: an unnamed scope is answered by
    # the built-in baseline, read through ``policy_level_control`` below.
    if ceiling is None and not edition.is_enterprise():
        return True
    try:
        if ref.startswith("@"):
            server = ref[1:].split("/", 1)[0]
            if not server:
                return False
            # Server-level first: the gate sees MCP tools as
            # ``mcp__<server>__<tool>``, so a sentinel classifies to
            # ``@server/probe`` and any server-level pattern (including a
            # wildcard, which a literal scan would miss) covers it.
            if not gate_decision(ceiling, None, f"mcp__{server}__probe").permitted:
                return False
            # Then per-tool rules, which the sentinel cannot see.
            if _ceiling_mentions_mcp_server(ceiling, server):
                return False
            # An MCP tool can also read/write files or reach the network, and the
            # gate enforces those scopes from the REAL call arguments
            # (classify_tool_args: the edit path, the fetch host) — arguments the
            # server-name probe cannot carry. Auto-approving the server skips that
            # gate, so if the ceiling has an opinion on ANY argument-derived scope,
            # withhold and let the gate apply it against the real path/host.
            arg_scopes = ("filesystem.read", "filesystem.write", "network.egress")
            if any(_ruleset_has_an_opinion(policy_level_control(ceiling, _s)) for _s in arg_scopes):
                return False
            return True

        # The generic `tools` scope governs tool NAMES, so it applies to EVERY
        # builtin — including one absent from the capability map. Without this an
        # unmapped tool (introspect / session / report / any future builtin)
        # returned True unconditionally and its shipped `allowedTools` grant
        # bypassed a `tools`-scope ceiling (e.g. tools.deny=["report"]). The
        # capability scopes it DOES map to add path/host granularity on top.
        scopes = ("tools",) + tuple(BUILTIN_TOOL_SCOPES.get(ref, ()))
        if any(_ruleset_has_an_opinion(policy_level_control(ceiling, scope)) for scope in scopes):
            return False
        capability = BUILTIN_TOOL_CAPABILITIES.get(ref)
        return not (capability and _capability_restricts(policy_level_control(ceiling, capability)))
    except Exception:  # noqa: BLE001 — see the fail-closed note above
        logger.warning("ceiling probe failed for %r; not auto-approving", ref, exc_info=True)
        return False


def _declared_auto_approve(emitted: Mapping[str, object]) -> Mapping[str, tuple[str, ...]]:
    """Per server, the ``autoApprove`` verbs its own spec declares.

    Fail-closed the useful way round: an unreadable owner declares nothing, so the
    floor applies to everything rather than exempting everything.
    """
    try:
        from kiro_crew.agent import declared_auto_approve

        return declared_auto_approve(emitted)
    except Exception:  # noqa: BLE001 — a lookup failure must not grant an exemption
        logger.warning("could not read the declared autoApprove verbs", exc_info=True)
        return {}


def _auto_approve_is_honoured() -> bool:
    """Whether the operator opted in to keeping an undeclared ``autoApprove``.

    Fail-closed: unreadable config withholds it; the value decides whether a gate runs.
    """
    try:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        return bool((live.snapshot() or KiroCrewConfig.load()).mcp.honour_auto_approve)
    except Exception:  # noqa: BLE001 — an unreadable config must not grant a bypass
        logger.warning("could not read mcp.honour_auto_approve; withholding", exc_info=True)
        return False


def _undeclared_verbs(asked: object, declared: Sequence[str]) -> tuple[str, ...]:
    """The verbs in *asked* that no server spec declared.

    A non-list ``autoApprove`` yields nothing rather than raising: the shape is
    the agent runtime's to validate, and this helper only decides what to report.
    """
    if not isinstance(asked, list):
        return ()
    return tuple(v for v in asked if v not in declared)


def _audit_auto_approve_honoured(name: str, verbs: Sequence[str]) -> None:
    """Record that an owner-written ``autoApprove`` was kept.

    Best-effort, exactly like the withhold audit beside it: an unavailable audit
    sink must never break a spec rebuild, because the spec is what the product
    needs to run and the record is what an operator reads afterwards.
    """
    logger.info(
        "Honoured owner-written autoApprove verbs on MCP server %s (%r): "
        "mcp.honour_auto_approve is on, so these calls skip the tool gate",
        name,
        list(verbs),
    )
    try:
        sel().log_api_access(
            caller="system",
            operation="mcp_auto_approve_honoured",
            outcome="ok",
            source="strip_ungoverned_auto_approve",
            resources=(
                f"@{name} autoApprove kept for {list(verbs)} (owner-written and no "
                "ceiling constrains it); these calls do not reach the tool gate"
            ),
        )
    except Exception:  # noqa: BLE001 — audit must not break the filter
        logger.debug("SEL audit unavailable for honoured autoApprove", exc_info=True)


def _is_owner_written(name: str, spec: Mapping[str, object], third_party: Container[str]) -> bool:
    """Whether this entry's ``autoApprove`` can be the OWNER's own statement.

    The opt-in respects a list the owner typed about their own tools. It must not
    also respect one a THIRD PARTY typed, and three sources here are not the owner:

    * An entry the CALLER names in ``third_party``, for a map that genuinely MIXES
      the owner's entries with a third party's: the shared ``mcp.json``, where the
      app entries are exactly the ones carrying that app's ``<app>:`` prefix. A
      writer whose whole map is third-party does not enumerate at all -- it passes
      ``honour_owner_written=False``, because an enumeration of app sources is a
      list that goes stale (manifest servers, the shipped agent spec's own keys, the
      per-agent policy's keys -- each found after the previous one was covered).
    * An APP's namespaced server. ``apps/bridges_mcp.py`` keys those ``<app>:<server>``
      and grants them from the manifest rather than from any user choice. Any ``:``
      in the name is treated as that shape: an owner who really used one keeps
      getting approval cards, which is the behaviour that shipped before this key
      existed, so the ambiguous case fails CLOSED rather than granting a bypass.
    * An entry Kiro Crew itself authored in a file it does not own, which carries
      :data:`~kiro_crew.mcp_provenance.MARKER_KEY`. That is our own emission, and
      what a spec declares is already handled by the declared set.
    """
    if name in third_party or ":" in name:
        return False
    try:
        return not is_marked(spec)
    except Exception:  # noqa: BLE001 — an unreadable marker must not grant a bypass
        logger.warning("could not read the MCP provenance marker on %s", name, exc_info=True)
        return False


def _is_honourable_shape(asked: object) -> bool:
    """Whether ``autoApprove`` is the ``list[str]`` kiro-cli will accept.

    The strict floor this opt-in replaces coerced any other shape to ``[]`` and
    popped the key, so a wrong-typed value never reached disk. Preserving one
    verbatim writes an agent spec kiro-cli's strict parsing rejects, and nothing
    re-sanitizes it: ``ensure_agent_materialized`` self-heals a MISSING spec file,
    not an invalid one, so every later rebuild re-preserves the same bad value and
    sessions keep failing. A wrong type therefore falls through to the floor,
    which is the recovery path that already exists.
    """
    return isinstance(asked, list) and all(isinstance(v, str) for v in asked)


def strip_ungoverned_auto_approve(
    servers: Mapping[str, object],
    *,
    audit: bool = True,
    third_party: Container[str] = (),
    honour_owner_written: bool = True,
) -> Dict[str, object]:
    """Return ``servers`` with a ceiling-governed ``autoApprove`` removed.

    ``autoApprove`` is the OTHER route to the exemption ``allowedTools`` grants,
    and a more direct one: kiro-cli approves an autoApproved MCP tool locally and
    emits no permission request, so ``hooks.on_tool_call`` never runs for it.
    ``agent.py``'s managed-server block states the rule for the servers KiroCrew
    ships ("DELIBERATELY NO autoApprove KEY, and none may ever be added"); this
    applies it to every other source.

    A MAP-level helper on purpose. The key can arrive from an app manifest, a
    per-agent MCP policy, a managed server spec, or an imported config from
    another tool — and each fix that covered only the reported source left the
    others open. Both writers of an agent config (the host's ``install_agent``
    and app-agent materialization in ``apps/bridges_agents.py``) run their final server
    map through here, so a new source is covered the moment it lands in that map
    rather than needing its own patch.

    Only the key is dropped, never the server: the tools stay available and go
    through the approval gate, which is where a per-tool ceiling rule is applied.

    An ungoverned host RESPECTS THE OWNER'S OWN CHOICE: ``mcp.honour_auto_approve``
    is on by default, so an ``autoApprove`` the owner wrote by hand survives there.
    An ``autoApprove`` is a deliberate statement about their own tools, and silently
    deleting it left them with no way to express it and nothing telling them it had
    gone. Only the OWNER's own statement is honoured. A writer whose map is entirely
    a third party's passes ``honour_owner_written=False`` and keeps nothing; a writer
    whose map MIXES the two names the third party's entries in ``third_party``; and an
    app's ``<app>:<server>`` entry, one Kiro Crew authored in a shared file, and a
    value that is not a ``list[str]`` are never honoured either (see
    :func:`_is_owner_written` and :func:`_is_honourable_shape`). Each of those falls
    through to the strict floor below. The whole-map switch exists because
    enumerating an app's sources is a list that GOES STALE: the manifest's servers,
    the shipped agent spec's own keys and the per-agent policy's keys were each found
    only after the previous one was covered, so the writer that materializes an app
    declines the opt-in outright rather than naming what it knows about today.
    Turning that key OFF restores the
    strict floor for everything, where a verb NO spec
    declares is dropped and only what a spec declares -- our own emission -- is
    kept. A governed ref keeps nothing either way: the ceiling is the OPERATOR's
    policy, not the owner's preference, so no config value can widen it, and the
    enterprise path is unchanged. The declarations are resolved here, not taken from
    the caller: of the six write paths reaching this helper only one could name them,
    and the other five would erase them.
    """
    seeded = _declared_auto_approve(servers)
    honoured = _auto_approve_is_honoured()
    out: Dict[str, object] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict) or "autoApprove" not in spec:
            out[name] = spec
            continue
        kept: list = []
        if not may_skip_gate_now(f"@{name}"):
            pass  # governed: nothing survives, and the enterprise path is unchanged
        elif (
            honoured
            and honour_owner_written
            and _is_owner_written(name, spec, third_party)
            and _is_honourable_shape(spec["autoApprove"])
        ):
            # GRANTING a gate exemption is as much a permission decision as
            # revoking one, so the honoured path audits too. Without this the
            # only autoApprove that appears in the feed is one that was taken
            # away, and the calls that skip the gate leave no trace of why they
            # were allowed to. Only an UNDECLARED list is reported: what a spec
            # declares is our own emission and is not the owner's decision.
            undeclared = _undeclared_verbs(spec["autoApprove"], seeded.get(name) or ())
            if undeclared and audit:
                _audit_auto_approve_honoured(name, undeclared)
            out[name] = spec
            continue
        else:
            asked = spec["autoApprove"]
            declared = seeded.get(name) or ()
            kept = [v for v in asked if v in declared] if isinstance(asked, list) else []
            if kept == asked:
                out[name] = spec
                continue
        trimmed = dict(spec)
        if kept:
            trimmed["autoApprove"] = kept
        else:
            trimmed.pop("autoApprove", None)
        if not audit:
            out[name] = trimmed
            continue
        logger.info(
            "Withheld autoApprove verbs on MCP server %s (kept %r): the ceiling "
            "constrains it, or no spec declared them and the opt-in is off",
            name,
            kept,
        )
        # Revoking a gate exemption is a permission DECISION — the allowedTools
        # writers emit this same SEL event, so a silent pop here would be the one
        # withhold path with no audit trail. Best-effort; never break a rebuild.
        try:
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="strip_ungoverned_auto_approve",
                resources=(
                    f"@{name} autoApprove narrowed to {kept} (governance ceiling or "
                    "the undeclared-grant floor); the rest go through the gate"
                ),
            )
        except Exception:  # noqa: BLE001 — audit must not break the filter
            logger.debug("SEL audit unavailable for autoApprove strip", exc_info=True)
        out[name] = trimmed
    return out


def _confirm_tool_patterns() -> tuple[str, ...] | None:
    """The operator's ``hooks.confirm_tools`` patterns from ``config.json``.

    ``()`` means no pattern is set. ``None`` means the operator may have set
    patterns this read cannot see; the static writers then withhold every MCP
    grant and the gate withholds every operator grant.

    A load can stand in defaults for settings it could not read: the read
    raised, a file did not parse (whole-config degradation, an unreadable base
    file), or a ``hooks`` section is not an object. Those defaults cannot show
    the list is empty, and they cannot show it is set either, so the files on
    disk decide (:func:`_config_files_state`). While a file does not read as
    written NOW, the list is unknown if some config file names the key.
    Otherwise the loaded values stand: defaults for a torn file that never
    names the key (every grant stays as it is with the key unset), the
    watcher's last good values while it keeps them, and the real values once
    the loader's process-lifetime degradation flag has gone stale.
    """
    global _CONFIRM_PATTERNS_MEMO
    loaded = True
    memo_key: tuple | None = None
    try:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig, _config_fingerprint
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
        from kiro_crew.config.validation import _CONFIG_CACHE

        cfg = live.snapshot()
        if cfg is None:
            # Until the watcher holds a snapshot (all of boot, and every process
            # that never starts one) each grant writer and gate check asked for a
            # full load: 572 of them in the first 90 s of one gateway boot. The
            # answer depends only on the config files, so reuse it while their
            # fingerprint is unchanged; any edit, create or delete moves the
            # fingerprint and reloads. The loader cache's invalidation generation
            # is part of the key, read before the load as the loader reads it, so
            # an in-process save() reloads even where a same-size replacement
            # leaves the fingerprint unchanged.
            memo_key = (
                _config_fingerprint(),
                _CONFIG_CACHE.generation(),
            )
            memo = _CONFIRM_PATTERNS_MEMO
            if memo is not None and memo[0] == memo_key:
                return memo[1]
            cfg = KiroCrewConfig.load()
        degraded: frozenset[str] = getattr(cfg, "degraded_sections", frozenset())
        hooks = cfg.hooks
        stood_in = (
            DEGRADED_WHOLE_CONFIG in degraded
            or "hooks" in degraded
            or bool(getattr(cfg, "_base_unreadable", False))
            or not isinstance(hooks, (dict, type(None)))
        )
    except Exception:  # noqa: BLE001 — a config read must not break a writer
        logger.warning("config read failed; judging hooks.confirm_tools from the files")
        logger.debug("hooks.confirm_tools read failed", exc_info=True)
        hooks, stood_in, loaded = None, True, False
    if stood_in:
        torn, names_key = _config_files_state()
        if (torn or not loaded) and names_key:
            return None
        if not loaded:
            return ()
        # Every file reads as written, or none that does not names the key:
        # the loaded values below are the whole story.
        # A load that stood in defaults also reads process state the file
        # fingerprint does not carry, so its answer is never reused.
        memo_key = None
    patterns = _patterns_from_hooks(hooks)
    if memo_key is not None:
        _CONFIRM_PATTERNS_MEMO = (memo_key, patterns)
    return patterns


#: ``(key, patterns)`` of the last :func:`_confirm_tool_patterns` answer read
#: from a clean load, keyed on the config files' fingerprint and the loader
#: cache's invalidation generation. Process-wide cache of file-derived data,
#: never per-caller state.
_CONFIRM_PATTERNS_MEMO: tuple[tuple, tuple[str, ...] | None] | None = None


def _patterns_from_hooks(hooks: object) -> tuple[str, ...] | None:
    """The ``confirm_tools`` patterns a loaded ``hooks`` section names."""
    if not isinstance(hooks, dict) or "confirm_tools" not in hooks:
        return ()
    raw = hooks["confirm_tools"]
    if not isinstance(raw, list):
        # The operator wrote the key but not as a list: what they meant to
        # confirm cannot be read, so it is unknown, not empty.
        return None
    return tuple(p for p in raw if isinstance(p, str))


def _config_files_state() -> tuple[bool, bool]:
    """``(torn, names_key)`` for the config files on disk now.

    *torn*: a present ``config.json`` / ``config.local.json`` cannot be read,
    does not parse as a JSON object, or holds a ``hooks`` value that is not an
    object -- the cases where a load stands in defaults. *names_key*: some file
    names the ``confirm_tools`` key, or cannot be read at all (bytes that cannot
    be read cannot show the key unset). Only an operator who wrote the key can
    have asked for a confirmation, so a torn file without it (a trailing comma)
    keeps every grant, Crew's own ``@kirocrew-*`` included.
    """
    try:
        from kiro_crew.config.loader import config_local_path, config_path, read_config_text

        paths = (config_path(), config_local_path())
    except Exception:  # noqa: BLE001 — a config read must not break a writer
        logger.debug("config paths unavailable", exc_info=True)
        return True, True
    torn = names_key = False
    for path in paths:
        try:
            if not path.exists():
                continue
            text = read_config_text(path)
        except Exception:  # noqa: BLE001 — bytes that cannot be read cannot show the key unset
            logger.debug("config file unreadable: %s", path, exc_info=True)
            return True, True
        names_key = names_key or "confirm_tools" in text
        try:
            data = json.loads(text)
        except ValueError:
            torn = True
            continue
        if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
            torn = True
    return torn, names_key


def confirm_tools_unknown() -> bool:
    """Whether the operator may have set ``hooks.confirm_tools`` patterns no read can see.

    The gate's own ``HooksConfig`` is built from the same degraded load, which can
    keep a base file's ``auto_approve_tools`` while dropping a torn overlay's
    ``confirm_tools``; this flag is how it learns to withhold those grants too.
    """
    return _confirm_tool_patterns() is None


def confirm_tools_unknown_now() -> bool:
    """:func:`confirm_tools_unknown` as the config watcher holds it now, for the gate.

    The gate's ``HooksConfig`` flag is set when that config is built. A tear the
    watcher rides out builds none: it keeps the last good values, so nothing
    changed and nothing is dispatched, yet the overlay it cannot read may hold
    the patterns. The watcher's snapshot does carry the tear's degradation flag,
    and while it does the files on disk decide, so the gate asks this on each
    call it would otherwise settle.

    Without a running watcher no reload replaces the gate's config, and the flag
    set when it was built is the answer: this returns False without loading
    anything. A watcher that cannot be read answers True, toward a confirmation.
    The cost is attribute reads until the process has seen a degraded load, and
    two config file reads per call after that.
    """
    try:
        from kiro_crew.config import live

        if live.snapshot() is None:
            return False
    except Exception:  # noqa: BLE001 — an unreadable watcher must not grant
        logger.debug("config watcher unreadable; treating hooks.confirm_tools as unknown")
        return True
    return confirm_tools_unknown()


def server_requires_confirmation(server: str, patterns: Sequence[str]) -> bool:
    """Whether a ``confirm_tools`` pattern can match a call on MCP server ``server``.

    The gate matches a pattern case-insensitively, with ``fnmatch`` wildcards that
    also consume ``/``, against the call's identity spellings ``@server``,
    ``@server/tool`` and ``Running: @server/tool``. A static grant hides every one
    of those calls from the gate, so it is withheld whenever the pattern could
    match ANY of them, for a tool name not known here.

    The test is on the pattern's literal prefix (the text before its first
    wildcard): every string the pattern matches starts with that prefix, so a
    pattern whose prefix is compatible with neither ``@server/`` nor
    ``running: @server/`` cannot match a call on this server. The test errs
    toward withholding, which costs a prompt the gate then settles. A pattern
    with no wildcard is compared exactly. A pattern that names a call only by its
    model-authored title acts at the gate alone.
    """
    target = server.lower()
    heads = (f"@{target}/", f"running: @{target}/")
    exact = (f"@{target}", f"running: @{target}")
    for pattern in patterns:
        text = pattern.strip().lower()
        cut = min((i for i, ch in enumerate(text) if ch in "*?["), default=-1)
        if cut < 0:
            if text in exact or text.startswith(heads):
                return True
            continue
        prefix = text[:cut]
        if prefix.startswith(heads) or any(head.startswith(prefix) for head in heads):
            return True
    return False


def _operator_requires_confirmation(ref: str) -> bool:
    """Whether ``hooks.confirm_tools`` names, or may name, the MCP server ``ref`` grants.

    A list the operator may have set but that cannot be read now counts as
    naming every server, so the grant is withheld and the gate settles each call
    (see :func:`_confirm_tool_patterns`).

    A grant ref is itself a pattern: ``*`` or ``@*`` covers every MCP call, and
    ``@ser*`` covers every server whose name starts ``ser``. Such a ref is
    withheld whenever any confirm pattern is listed, since the servers it covers
    are not known here. A ref without a wildcard in its server segment is judged
    for that one server. A ref whose literal prefix cannot begin with ``@`` (a
    builtin such as ``fs_*``) covers no MCP call.
    """
    if not isinstance(ref, str):
        return False
    cut = min((i for i, ch in enumerate(ref) if ch in "*?["), default=-1)
    literal = ref if cut < 0 else ref[:cut]
    if not literal.startswith("@") and literal != "":
        return False
    server, sep, _ = literal[1:].partition("/")
    names_one_server = literal.startswith("@") and bool(server) and (sep or cut < 0)
    if not names_one_server and cut < 0:
        return False
    patterns = _confirm_tool_patterns()
    if patterns is None:
        return True
    if not names_one_server:
        return bool(patterns)
    return server_requires_confirmation(server, patterns)


def may_skip_gate_now(ref: str) -> bool:
    """:func:`may_skip_gate` against the CURRENTLY installed ceiling.

    The entry point every ``allowedTools`` writer should call: it folds in the one
    piece each of them would otherwise re-implement — resolving the ceiling — and
    it fails CLOSED. There are five such writers (the host agent's shared-MCP
    sync, app-agent materialization, two dashboard MCP-enable paths and doctor's
    auto-fix), and every one of them independently produces the single state the
    PreToolUse gate can never see. A per-caller copy is how they diverged: the
    first fix closed one and left the other four open.

    An unreadable ceiling is deliberately NOT treated like an absent one. ``None``
    from ``current_context()`` means "ungoverned host, keep every grant"; an
    exception means "there may be a ceiling and we cannot read it", and answering
    "skip the gate" to that is the bypass itself.

    The operator's own ``hooks.confirm_tools`` opt-in withholds too: a server it
    names asks on every call, so no writer may grant it statically.
    """
    return _policy_may_skip_gate_now(ref) and not _operator_requires_confirmation(ref)


def withheld_by_confirm_tools(ref: str) -> bool:
    """Whether ``hooks.confirm_tools``, and not the security policy, withholds ``ref``'s grant.

    For a writer that explains a withheld grant (doctor), so an operator's own
    confirm list is not reported as an enterprise ceiling.
    """
    return _policy_may_skip_gate_now(ref) and _operator_requires_confirmation(ref)


def _policy_may_skip_gate_now(ref: str) -> bool:
    """The security-policy half of :func:`may_skip_gate_now`: ceiling and profiles."""
    try:
        from kiro_crew.platform.context import current_context

        ceiling = getattr(current_context(), "governance", None)
    except Exception:  # pragma: no cover - defensive
        logger.warning("cannot resolve the governance ceiling; not auto-approving %r", ref)
        return False
    if not may_skip_gate(ref, ceiling):
        return False
    # The POLICY ceiling permits (or is absent) — but governance is
    # ``policy ∩ profile``, and a Level-2 PROFILE can govern this ref even with NO
    # ceiling (a per-app profile denying ``@srv/delete``). A static allowedTools
    # writer has no surface to resolve the single active profile, so withhold the
    # shortcut if ANY configured profile could govern the ref; the runtime gate
    # then applies the specific one with the real arguments.
    try:
        from kiro_crew.platform.governance_profiles import any_configured_profile_governs

        if any_configured_profile_governs(ref):
            return False
    except Exception:  # pragma: no cover - defensive; cannot tell -> withhold
        logger.warning("cannot evaluate profiles for %r; not auto-approving", ref)
        return False
    return True


def sanitize_agent_config_governance(
    config: MutableMapping[str, object], *, audit: bool = True
) -> None:
    """In-place: strip ceiling-governed auto-approve grants from a full agent
    config about to be written to ``kirocrew.json``.

    THE filter for writers that persist the WHOLE config object (the dashboard
    ``PUT /api/agent/config`` handler, the browser-setup Playwright convergence)
    rather than mutating one ``allowedTools`` entry at a time. Those writers
    escaped the per-ref writer inventory precisely because they assign
    ``config["allowedTools"] = [...]`` / ``config["mcpServers"] = {...}`` wholesale
    — so a governed ``@denied`` ref or a governed server's ``autoApprove`` written
    through them restored the very bypass the per-ref writers close. Every
    whole-config writer MUST call this immediately before it persists, so no
    future writer can reopen the surface.

    ``audit=False`` is for a pure owner preview: filtering is identical, but
    no withdrawal log or SEL event is emitted. Publication keeps the default
    ``audit=True`` and therefore retains its existing audit contract.

    Drops non-string and ceiling-governed ``allowedTools`` entries (same rule and
    fail-closed semantics as ``may_skip_gate_now``) and removes ``autoApprove``
    from any governed MCP server (via ``strip_ungoverned_auto_approve``). ``tools``
    (mount, not auto-approve) is left intact. A no-op on an ungoverned host.
    """
    allowed = config.get("allowedTools")
    if isinstance(allowed, list):
        kept: list[str] = []
        withheld: list[str] = []
        for ref in allowed:
            if not isinstance(ref, str):
                continue  # non-string junk is not a valid ref — drop silently
            (kept if may_skip_gate_now(ref) else withheld).append(ref)
        config["allowedTools"] = kept
        if withheld and audit:
            # Withholding a grant is a permission DECISION — every other
            # allowedTools writer emits this event, so a silent drop here would
            # be the one withhold path with no audit trail. Best-effort.
            try:
                sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="sanitize_agent_config_governance",
                    resources=(
                        f"{', '.join(withheld)} removed from allowedTools "
                        "(governance ceiling); calls go through the approval gate"
                    ),
                )
            except Exception:  # noqa: BLE001 — audit must not break the write
                logger.debug("SEL audit unavailable for config sanitize", exc_info=True)
    servers = config.get("mcpServers")
    if isinstance(servers, dict):
        config["mcpServers"] = strip_ungoverned_auto_approve(servers, audit=audit)


def assert_governance_floor(
    ceiling: Optional[GovernanceCeiling], profile: Optional[Profile]
) -> None:
    """Boot-time guard: reject a profile that would *weaken* the ceiling.

    For every ORDINAL the ceiling governs, the profile (if it also governs it)
    must be **equal-or-stricter** — a looser profile value aborts boot
    (fail-closed).  Set-archetypes (ScopedRuleset/CapabilityGate/ScopedMap) can
    only ever narrow under ``resolve`` (intersection / AND), so they cannot
    weaken the ceiling by construction and need no per-item boot proof; the
    ordinal scales are the only place a profile could pick a *looser* value, so
    they are the floor check — mirroring ``assert_security_floor``'s role for the
    deny floor.

    A ``None`` ceiling (standalone, ungoverned) imposes no floor.
    """
    if ceiling is None or profile is None:
        return
    for scope, spec in SCOPE_CATALOG.items():
        if spec.kind != ORDINAL:
            continue
        pol = ceiling.get(scope)
        pro = profile.get(scope)
        if isinstance(pol, OrdinalControl) and isinstance(pro, OrdinalControl):
            if not pro.is_at_least_as_strict_as(pol):
                raise PlatformCompositionError(
                    f"governance floor violated: profile {profile.name!r} sets "
                    f"{scope}={pro.value!r} which is looser than policy ceiling "
                    f"{pol.value!r} (scale {spec.ordinal_scale!r})"
                )


def assert_governance_paths_protected() -> None:
    """Boot guard: the governance trust-root files must be on the sensitive floor.

    The keystone of "secure by default, not by mandate" is that the agent cannot
    WRITE the policy/profile files — enforced solely by their presence in
    ``security._SENSITIVE_HOME_DIRS`` (the shared read+write gate).
    ``assert_security_floor`` covers the deny-pattern floor but NOT the
    sensitive-path list, so this is an explicit, independent boot check: if a
    refactor ever drops these entries the integrity guarantee silently
    evaporates, so we fail closed at boot instead.
    """
    # Deferred import: keep ``security`` (heavy regex stack) off this module's
    # import path; only the boot check needs it.
    from kiro_crew import security

    # The data home moved to ~/.kiro/crew; check the CURRENT home's trust-root
    # paths are gated (the legacy ~/.kirocrew forms remain in the keystone too,
    # but a fresh install only ever writes the new home, so that is what the boot
    # integrity check must assert).
    required = (
        ".kiro/crew/security_policy.json",
        ".kiro/crew/profiles",
        ".kiro/crew/admission_policy.json",
        # Denied-command opt-out ceiling — the agent must not be able to write
        # its own deny opt-out state (would let it disable the deny gate).
        ".kiro/crew/denied_commands.json",
        # The centrally-distributed ceiling's cache. On this list for the same
        # reason as the policy itself and then one more: the cached metadata records
        # the source the copy came from, and the loader trusts that record when
        # deciding whether the cache is this host's last-known-good. An agent that
        # could write here would not need to touch ``security_policy.json`` to
        # replace its own ceiling — it would publish itself one, with provenance.
        f".kiro/crew/{_POLICY_CACHE_LEAF}",
    )
    sensitive = set(security._SENSITIVE_HOME_DIRS)  # noqa: SLF001 — boot integrity check
    missing = [p for p in required if p not in sensitive]
    if missing:
        raise PlatformCompositionError(
            "governance integrity violated: trust-root paths missing from the "
            f"sensitive-path floor {missing!r}; the agent could rewrite its own "
            "ceiling. Restore them in security._SENSITIVE_HOME_DIRS."
        )


__all__ = [
    "Decision",
    "RulesetLike",
    "ScopedRuleset",
    "OrdinalControl",
    "CapabilityGate",
    "ScopedMap",
    "ScopeSpec",
    "SCOPE_CATALOG",
    "GovernanceCeiling",
    "BootControls",
    "UpdatePins",
    "active_update_pins",
    "PolicyDistribution",
    "active_policy_distribution",
    "UNAVAILABLE_FAIL_CLOSED",
    "UNAVAILABLE_DEGRADE",
    "MIN_REFRESH_INTERVAL_SECS",
    "DEFAULT_FETCH_TIMEOUT_SECS",
    "MAX_POLICY_BYTES",
    "Profile",
    "Bind",
    "POLICY_VERSION",
    "MODE_ALLOW",
    "MODE_DENY",
    "COMMANDS_SCOPE",
    "SIGNATURE_VERIFIED",
    "SIGNATURE_UNVERIFIED",
    "SIGNATURE_UNSIGNED",
    "SIGNATURE_UNCHECKED",
    "policy_signing_payload",
    "CU_MCP_SERVER",
    "CU_CLASS_OBSERVE",
    "CU_CLASS_MUTATE",
    "CU_CLASS_POINTER",
    "CU_CLASS_KEYBOARD",
    "CU_CLASS_TEXT_ENTRY",
    "CU_CLASS_CONTROL",
    "computer_use_action_classes",
    "computer_use_action_from_title",
    "is_computer_use_title",
    "register_scope",
    "register_matcher",
    "mcp_title_to_ref",
    "classify_tool_title",
    "classify_tool_args",
    "deny_all_profile",
    "parse_policy",
    "parse_profile",
    "load_security_policy",
    "compose_installed_ceiling",
    "reset_process_state",
    "resolve",
    "resolve_pinned_commands",
    "resolve_ordinal",
    "gate_decision",
    "assert_governance_floor",
    "assert_governance_paths_protected",
    "assert_policy_signature_satisfied",
    "compose_profiles",
]


# This module is the facade of the governance model: every name is reached through it
# as ``kiro_crew.platform.governance.<name>``. A read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. The
# auto-approve surface above and the two boot checks stay here; the backend rules in
# ``code-review.yml`` grep this file for the trust-root tuple of
# ``assert_governance_paths_protected``. New work goes to the owner of its
# responsibility (``kiro_crew.platform.governance_parts`` describes each one):
#
#   ``matching`` -- the matchers, the strictness scales, item normalization, host extraction
#   ``tool_calls`` -- the governed (scope, item) pairs one tool call touches
#   ``archetypes`` -- the decision record, the four archetypes and their composition
#   ``catalog`` -- which scopes exist, and which archetype each one is
#   ``baseline`` -- the enterprise built-in baseline: each scope's minimal set
#   ``carriers`` -- ``GovernanceCeiling``, ``Profile`` and their policy-only blocks
#   ``parsing`` -- a policy or profile document into its carrier
#   ``signature`` -- proving a policy's ``identity.signature``
#   ``tiers`` -- the tier ladder and the installed ceiling
#   ``evaluator`` -- ``resolve``, ``resolve_ordinal``, ``gate_decision``, the command pins
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.platform.governance_parts.matching",
    "kiro_crew.platform.governance_parts.tool_calls",
    "kiro_crew.platform.governance_parts.archetypes",
    "kiro_crew.platform.governance_parts.catalog",
    "kiro_crew.platform.governance_parts.baseline",
    "kiro_crew.platform.governance_parts.carriers",
    "kiro_crew.platform.governance_parts.parsing",
    "kiro_crew.platform.governance_parts.signature",
    "kiro_crew.platform.governance_parts.tiers",
    "kiro_crew.platform.governance_parts.evaluator",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.platform.governance_parts.archetypes import (  # noqa: F401
        _MODES,
        Decision,
        ScopedMap,
        ScopedRuleset,
        _AndRuleset,
        _compose_controls,
        _compose_posture,
        _dedup,
        _refuse_unmatchable_host_entries,
        _reject_unknown_keys,
        _str_tuple,
        _unmatchable_glob,
    )
    from kiro_crew.platform.governance_parts.baseline import (  # noqa: F401
        _CAPABILITY_OFF,
        _DEFAULT_WORKSPACE_ROOTS,
        _OFF,
        BASELINE_CONTROLS,
        BASELINE_UNGATED,
    )
    from kiro_crew.platform.governance_parts.carriers import (  # noqa: F401
        _AGENTCORE_POLICY_ONLY,
        _AGENTCORE_POSTURES,
        _AGENTCORE_SCOPE,
        _POLICY_DISTRIBUTION_HEADERS_ENV,
        _POLICY_DISTRIBUTION_URL_ENV,
        _UNAVAILABLE_DISPOSITIONS,
        DEFAULT_FETCH_TIMEOUT_SECS,
        MAX_DURATION_SECS,
        MAX_POLICY_BYTES,
        MIN_REFRESH_INTERVAL_SECS,
        SIGNATURE_UNCHECKED,
        SIGNATURE_UNSIGNED,
        SIGNATURE_UNVERIFIED,
        SIGNATURE_VERIFIED,
        UNAVAILABLE_DEGRADE,
        UNAVAILABLE_FAIL_CLOSED,
        Bind,
        BootControls,
        PolicyDistribution,
        UpdatePins,
        _apply_agentcore_gateway_url,
        _apply_agentcore_posture,
        _apply_agentcore_workload_name,
        _capability_raw_for_gate,
        _command_deny_patterns,
        _is_enterprise,
        _version_tuple,
        active_policy_distribution,
        active_update_pins,
        agentcore_gateway_url,
        agentcore_posture,
        agentcore_workload_name,
        compose_profiles,
        deny_all_profile,
    )
    from kiro_crew.platform.governance_parts.catalog import (  # noqa: F401
        _SCOPE_ALIASES,
        CAPABILITY,
        COMMANDS_SCOPE,
        RULESET,
        SCOPEDMAP,
        ScopeSpec,
        register_scope,
    )
    from kiro_crew.platform.governance_parts.evaluator import (  # noqa: F401
        _BASELINE_REASON,
        _DENY_UNLISTED,
        _PERMIT_NOT_GOVERNED,
        _policy_level,
        _query_level,
        resolve,
        resolve_ordinal,
        resolve_pinned_commands,
    )
    from kiro_crew.platform.governance_parts.matching import (  # noqa: F401
        _DEFAULT_MATCHER,
        _MATCHERS,
        _NETWORK_SCHEMES,
        _ORDINAL_SCALES,
        _PLAIN_HOST_RE,
        _canonical_ip,
        _expand,
        _looks_like_host,
        _match_command,
        _match_host,
        _match_identifier,
        _match_mcp,
        _match_path,
        _norm_item,
        _url_host,
        register_matcher,
        url_policy_hosts,
    )
    from kiro_crew.platform.governance_parts.parsing import (  # noqa: F401
        _KEY_OPEN_NAMESPACES,
        _PROFILE_NAME_RE,
        _SANDBOX_FLAGS_SCOPE,
        _SANDBOX_RESERVED_FLAGS,
        _STRUCTURAL_KEYS,
        POLICY_VERSION,
        _coerce_boot_flag,
        _parse_control,
        _parse_controls,
        _read_json_file,
        baseline_controls,
        parse_policy,
        parse_profile,
    )
    from kiro_crew.platform.governance_parts.signature import (  # noqa: F401
        _audit_policy_signature,
        _mark_policy_signature_incident,
        _policy_signature_required,
        _policy_signature_state,
        _policy_trust_settings,
        _verify_policy_signature,
        assert_policy_signature_satisfied,
        policy_signing_payload,
    )
    from kiro_crew.platform.governance_parts.tiers import (  # noqa: F401
        _POLICY_DISTRIBUTION_CACHE_ONLY_ENV,
        _POLICY_ENV,
        _POLICY_HOME_LEAF,
        TIER_BUNDLED,
        TIER_CENTRAL,
        TIER_ENV,
        TIER_HOME,
        _audit_policy_tier,
        _audit_tier_intersect_once,
        _declared_distribution,
        _intersect_ceilings,
        _intersect_update_pins,
        _load_security_policy,
        _LoadStage,
        _policy_home_path,
        _process_state,
        _read_env_policy,
        _read_home_policy,
        _subordinate_ceiling,
        _TierProcessState,
        _warn_env_beneath_central_once,
        _warn_home_unreadable_beneath_authority_once,
        _with_enterprise_baseline,
        compose_installed_ceiling,
        compose_tier_ladder,
        last_composed_ceiling,
        load_security_policy,
        record_composed_ceiling,
        reset_process_state,
    )
    from kiro_crew.platform.governance_parts.tool_calls import (  # noqa: F401
        _CU_ACTION_CLASSES,
        _CU_ACTION_TOOL_PREFIX,
        _CU_TITLE_PREFIX,
        _CU_UNKNOWN_ACTION_CLASSES,
        _GUEST_UNVERIFIABLE_ITEM,
        _KIND_EDIT,
        _KIND_FETCH,
        _KIND_READ,
        _PATH_ARG_KEYS,
        _READING_PREFIX,
        _RUNNING_PREFIX,
        _TRUNCATED_SCAN_ITEM,
        _UNANCHORED_TARGET_ITEM,
        CU_CLASS_CONTROL,
        CU_CLASS_KEYBOARD,
        CU_CLASS_MUTATE,
        CU_CLASS_OBSERVE,
        CU_CLASS_POINTER,
        CU_CLASS_TEXT_ENTRY,
        CU_MCP_SERVER,
        _cu_action_key,
        _governed_targets,
        _tool_arg_paths,
        classify_tool_args,
        classify_tool_title,
        computer_use_action_classes,
        computer_use_action_from_title,
        is_computer_use_title,
        mcp_title_to_ref,
    )
