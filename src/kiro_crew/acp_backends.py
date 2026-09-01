"""Which ACP backends this build can serve — the one place that decides.

The question this module owns is **capability**: can this build drive the harness
at all? The public edition registers kiro-cli and KAS; an edition plugin adds its
own from ``ProviderRegistry.register_acp_backends`` by calling
:func:`register_selectable_backend`, the structural twin of
``publish_provider.register_provider``.

A LEAF module on purpose. ``kiro_crew/acp/__init__.py`` imports the ACP client and
runtime, so reaching ``kiro_crew.acp.types`` executes that package init and lands
back in ``config.loader`` — the cycle ``_normalize_acp_backend`` used to defer for.
That cycle is why the selectable list used to be a **literal in three unrelated
places** (the loader's ``acp_backend`` field metadata, the dashboard's PATCH
allowlist, and ``acp.types``) with a drift test standing in for a code owner: none
of the three could import the others. Nothing here imports ``kiro_crew.acp``,
``kiro_crew.config`` or ``kiro_crew.platform``, so all three now derive from this
module — and a plugin-registered backend reaches the dashboard without a core
edit, which a literal could never do.

Whether a registered backend may be selected on a *given deployment* is a separate
question (an enterprise policy bounding the fleet to one harness). It is
deliberately NOT answered here: it needs a governance ceiling, resolving a ceiling
reaches ``current_context()``, and that call's lazy branch loads config — so asking
it from :func:`resolve_selected_backend`, which runs inside
``KiroCrewConfig.load()``, re-enters that load and recurses. Keeping this module
capability-only is what makes the load path safe.
"""

from __future__ import annotations

import logging
from typing import FrozenSet, Set

logger = logging.getLogger(__name__)

# ── Backend identifiers ──
# ``acp.types`` re-exports these, so every existing call site keeps importing
# them from there; this module is only where they are DEFINED.

ACP_BACKEND_CLAUDE = "claude"
ACP_BACKEND_KAS = "kas"
# The Codex ACP adapter: a Node stdio server that boots the Codex app server and
# translates ACP onto its operations. Known so that an edition shipping a provider
# for it can register the id; absent from BASELINE_SELECTABLE_BACKENDS below, so no
# build offers it until something registers it.
ACP_BACKEND_CODEX = "codex"
# The kiro-cli backend is spelled as the empty string throughout, so name it
# rather than leaving every call site to infer it from "not claude".
ACP_BACKEND_KIRO = ""

# Membership gate for the ``acp_backend`` kwarg. An unrecognized value would
# otherwise fall through every ``_is_<backend>`` check and silently spawn
# kiro-cli, so provider construction rejects it instead.
ACP_BACKENDS_KNOWN: FrozenSet[str] = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CODEX,
    }
)

# ── Capability: where a harness gets its MCP servers ──

#: Harnesses that receive their MCP servers as a PER-SESSION array on
#: ``session/new`` / ``session/load`` instead of reading an agent file.
#:
#: kiro-cli (and KAS, which is kiro-cli's relay) is handed ``--agent`` and loads
#: the spec itself, so Crew passes it an empty array — a duplicate there would
#: shadow the spec's own entries. claude-agent-acp reads no agent file at all, so
#: the array is the ENTIRE MCP surface of the session: an empty one means the
#: harness works while every Crew tool is silently absent.
#:
#: A membership set rather than ``_is_claude`` because this is a property of the
#: transport, not of Anthropic: any ACP adapter that does not read Crew's agent
#: spec belongs here, and the next such harness should join the set rather than
#: add a second branch at the call site (harness-parity H6).
ACP_BACKENDS_SESSION_MCP_ARRAY: FrozenSet[str] = frozenset({ACP_BACKEND_CLAUDE})

# ── The selectable registry ──

#: What the public edition ships.
#:
#: ``ACP_BACKEND_CLAUDE`` is included because the public build can genuinely serve a
#: session with it: ``acp/client.py`` owns the whole spawn path (the ``_is_claude``
#: branch, ``_resolve_claude_acp_bin``, ``_resolve_claude_code_executable``) and the
#: adapter it needs is a PUBLIC npm package (``CLAUDE_ACP_NPM_PKG``). Nothing about it
#: is edition-private. An earlier revision left it out and described it as a "dormant
#: seam ... not something a public build can serve a session with", which made the
#: option render as permanently unavailable on exactly the builds that could run it —
#: the switch was the only missing piece, not the harness.
#:
#: Whether it is USABLE on a given machine is a separate question with its own answer:
#: :mod:`kiro_crew.agent_sdk.backend_install` probes for the two binaries and the
#: dashboard reports what is absent plus the command that installs it.
#:
#: ``ACP_BACKEND_CODEX`` is deliberately absent, and for a reason that does NOT apply
#: to claude: the spawn path lands here, but no provider registers it and
#: ``backend_install`` has no probe for its adapter, so a build offering the option
#: could not tell an operator what is missing when the session failed to start. It
#: becomes selectable when something calls :func:`register_selectable_backend` —
#: adding it here instead would ship an option ahead of the code that answers for it.
BASELINE_SELECTABLE_BACKENDS: FrozenSet[str] = frozenset(
    {ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS}
)

# ── Policy-facing spelling ──
# A governance rule is written by a human into ``security_policy.json`` and is
# matched as an identifier, so the kiro backend cannot be spelled the way the code
# spells it: ``ACP_BACKEND_KIRO`` is the empty string, and an empty allow/deny
# entry is indistinguishable from a typo'd blank that a JSON linter would keep.
# ``"kiro"`` is therefore the WIRE name, translated here rather than at each
# reader, so the policy vocabulary has one owner.

POLICY_ID_KIRO = "kiro"

POLICY_ID_BY_BACKEND: dict = {
    ACP_BACKEND_KIRO: POLICY_ID_KIRO,
    ACP_BACKEND_KAS: ACP_BACKEND_KAS,
    ACP_BACKEND_CLAUDE: ACP_BACKEND_CLAUDE,
    # Present even though no build ships codex as selectable: a policy author has to
    # be able to deny an id BEFORE an edition registers it, and the mapping is what
    # makes the id nameable in a rule at all.
    ACP_BACKEND_CODEX: ACP_BACKEND_CODEX,
}

#: The backend a deployment policy may never deny.
#:
#: A governance scope that can empty the selectable set is a scope that can brick
#: the install — there would be no harness left to start a session with, and the
#: operator's remedy (edit the trust-root policy) is the one file the dashboard
#: cannot reach. So the scope is additive over a floor: it can WIDEN the set past
#: what this deployment would otherwise select, never shrink it below this member.
#:
#: kiro-cli, not KAS, deliberately: KAS is not an independent harness — it is
#: served by kiro-cli's own ACP relay (``acp/kas_transport.build_kas_argv`` returns
#: ``[kiro_bin, "acp", "--agent-engine", "v3", "--auth-method", "cli"]``), so a KAS
#: floor would rest on the same binary while adding a second thing that can be
#: absent. The floor has to be the member with the fewest preconditions of its own.
#: Revisit if KAS ever ships a binary of its own.
GOVERNANCE_FLOOR_BACKEND: str = ACP_BACKEND_KIRO

# ── Two sets, because policy must be RE-APPLIED, not applied once ──
#
# ``_baseline`` is what the BUILD can serve: the public default plus whatever an
# edition registered. ``_selectable`` is what this DEPLOYMENT may currently select,
# i.e. the baseline minus whatever the live policy denies.
#
# Keeping them apart is what makes the policy re-appliable in BOTH directions. An
# earlier revision of this module had one set and a destructive
# ``deny_selectable_backend``: a ceiling installed at runtime
# (``policy_distribution.apply_ceiling`` replaces ``current_context().governance``
# mid-process) could then never be re-evaluated, so a TIGHTENED fleet policy stayed
# inert until every gateway restarted and a LOOSENED one could not restore what the
# earlier pass had already deleted. Recomputing ``baseline - denied`` has neither
# failure: it is idempotent, order-independent, and reversible.
_baseline: Set[str] = set(BASELINE_SELECTABLE_BACKENDS)
_selectable: Set[str] = set(BASELINE_SELECTABLE_BACKENDS)


def register_selectable_backend(backend: str) -> None:
    """Make *backend* selectable in ``agent.acp_backend``.

    Called from an edition's ``ProviderRegistry.register_acp_backends`` alongside
    the provider registration itself — registering the provider without this
    leaves the harness runnable but unreachable, which is exactly the state a
    hard-coded list produced: an option absent from the dashboard on a build that
    could run it.

    Writes the BASELINE and the effective set together, so an edition that
    registers after a policy pass has already run is still visible to the next
    recompute rather than being silently dropped by it.

    Idempotent, so a re-entrant bootstrap costs nothing. Rejects an id outside
    ``ACP_BACKENDS_KNOWN``: provider construction would raise on it later, and a
    dashboard option that cannot start a session is worse than an absent one.
    """
    if backend not in ACP_BACKENDS_KNOWN:
        raise ValueError(
            f"cannot register unknown ACP backend {backend!r}; "
            f"known: {sorted(ACP_BACKENDS_KNOWN)}"
        )
    _baseline.add(backend)
    _selectable.add(backend)


def selectable_backends() -> FrozenSet[str]:
    """Every backend this deployment may currently select."""
    return frozenset(_selectable)


def registered_backends() -> FrozenSet[str]:
    """Every backend the BUILD can serve, before any policy narrowing.

    The input a policy recompute iterates. Distinct from
    :func:`selectable_backends`, which is the answer AFTER narrowing — asking the
    narrowed set what to narrow is how a one-way ratchet gets built by accident.
    """
    return frozenset(_baseline)


def apply_selectable_denials(denied: Set[str]) -> FrozenSet[str]:
    """Recompute the selectable set as ``baseline - denied``. Returns what was removed.

    The ONE way deployment policy reaches this decision, and the structural
    counterpart to :func:`register_selectable_backend`: rather than adding a second
    gate somewhere downstream, the ``agent_backend`` governance scope narrows this
    registry (``agent_backend_governance.narrow_selectable_backends``, driven from
    ``bootstrap_context`` at boot AND from ``policy_distribution.apply_ceiling``
    whenever a ceiling is installed at runtime). Everything downstream —
    ``resolve_selected_backend``, the PATCH allowlist, ``GET /api/config/schema``,
    the provider factory — then reads the narrowed answer with no code of its own,
    which is what keeps selectability at exactly one gate (harness-parity H4) and
    the Kiro construction path free of an adapter-driven conditional (H13).

    ASSIGNS rather than subtracts, so calling it again with a smaller ``denied``
    RESTORES what a previous call removed. That is the property a runtime ceiling
    swap needs and a destructive remove cannot provide.

    :data:`GOVERNANCE_FLOOR_BACKEND` is force-kept even if named in ``denied``. That
    is not defence against the governance caller, which never submits the floor to
    the scope — it is so that no caller of this function can empty the set and leave
    the install with no startable harness, a state the dashboard cannot repair
    because the trust-root policy is the one file it may not write.
    """
    keep = {b for b in _baseline if b not in denied}
    if GOVERNANCE_FLOOR_BACKEND in _baseline:
        keep.add(GOVERNANCE_FLOOR_BACKEND)
    removed = frozenset(_baseline - keep)
    _selectable.clear()
    _selectable.update(keep)
    return removed


def selectable_backend_values() -> list[str]:
    """:func:`selectable_backends` as a sorted list.

    The form every operator-facing surface wants: a stable option order in the
    dashboard and a stable ``must be one of [...]`` refusal message. Kept here so
    the PATCH allowlist and the schema endpoint share one answer instead of each
    sorting its own.
    """
    return sorted(selectable_backends())


def resolve_selected_backend(value: object) -> str:
    """Coerce a persisted ``agent.acp_backend`` to a backend this build can serve.

    THE single gate, in the one place the pre-registry code already gated: called
    from ``_normalize_acp_backend`` on the way out of ``config.json``. What changed
    is only what it reads — the registry instead of a frozen literal — so the
    coercion behaviour is unchanged from before the registry existed. The Kiro
    construction path deliberately gains no second check: harness-parity H13 keeps
    that path free of conditionals added in service of an adapter, and a check there
    could not fire anyway, since ``AgentConfig`` is built in exactly one place and
    its ``acp_backend`` is never reassigned.

    Runs inside ``KiroCrewConfig.load()``, so it must stay free of anything that
    reads the platform context: ``current_context()``'s lazy branch loads config,
    so a lookup here re-enters the very load that called it and recurses until the
    stack ends — and a broad ``except`` around it does not save you, it converts
    the crash into a silent wrong answer. Reading only the registry keeps it safe.

    An unselectable or unrecognized value — a backend this build did not register, a
    typo, or the non-string shapes a hand-edited ``config.json`` can hold — degrades
    to the default with the reason in the log rather than propagating: ``AcpProvider``
    rejects an unknown backend by raising, and startup refusing with a reason is the
    contract (harness-parity H3).

    An edition that registers a backend must do so before the first config load; the
    registry is read here, not cached, so ordering is the edition's to get right.
    """
    selectable = selectable_backends()
    if isinstance(value, str) and value in selectable:
        return value
    if value not in (None, ACP_BACKEND_KIRO):
        logger.warning(
            "Ignoring agent.acp_backend %r (not selectable in this build); using "
            "the default backend. Selectable values: %s",
            value,
            ", ".join(repr(b) for b in sorted(selectable)),
        )
    return ACP_BACKEND_KIRO


# ── Capability membership (harness-parity H6, H7) ──
# Every capability a backend may claim is an OPT-IN set here, never a negation at
# the call site. ``not is_claude_backend`` reads correctly with two backends and
# then silently hands the capability to the third, so a harness that has never
# demonstrated the capability inherits it — and the operator who never opted into
# that harness is the one who finds out. Adding a member is a deliberate edit
# with evidence; inheriting a default is not a decision. See
# docs/system-specs/modules/harness-parity.md.

# Backends whose single process can host N concurrent ACP sessions (AcpRuntime
# demux) AND can persist a SHARED subagent session across teardown. KAS runs on
# AcpRuntime (multi-session), but its teardown maps to _kiro/session/delete,
# which removes the persisted session — so a shared subagent would strand
# spawn_continue (conversation_gone). KAS therefore opts in only once a
# keep-aware teardown lands (native subagent work); until then its subagents get
# dedicated sessions. claude-agent-acp runs through AcpClient (one process per
# session) and is not a member. codex-acp is not either, for the same reason: one
# adapter process serves one session, so there is nothing to share.
ACP_BACKENDS_SESSION_SHARING = frozenset({ACP_BACKEND_KIRO})

# Backends implementing the ``_session/steer`` extension (mid-turn steer). Neither
# claude-agent-acp nor codex-acp implements it, so a steer sent to either would be
# answered with method-not-found rather than reaching the turn.
ACP_BACKENDS_STEER = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends carrying their OWN internal OS sandbox, which on macOS cannot nest
# inside Kiro Crew's seatbelt (kernel EPERM) — so ``sandbox.wrap_argv`` skips
# Crew's own layer for them. This is the one membership test that fails OPEN:
# claiming it for a harness with no internal sandbox hands isolation to a layer
# that never starts and leaves the agent process unconfined. Only kiro-cli
# qualifies; a Node or Python harness does not, however it is spawned.
#
# KAS is NOT a member even though Crew now spawns it as ``kiro-cli acp
# --agent-engine v3`` and the process on the end of the argv IS kiro-cli. The
# relay spawns the KAS server without an ``--sandbox`` argument, and KAS's
# sandbox factory resolves an absent config to its no-op backend, so no OS
# sandbox starts inside — adding KAS here would skip Crew's seatbelt in favour of
# a layer that does not exist. See :mod:`kiro_crew.acp.kas_transport`.
#
# codex-acp is excluded on the same rule: it is a Node adapter, so Crew's own layer
# is the only OS confinement a codex session gets. The Codex sandbox modes the
# adapter can apply are in-process policy, not an OS sandbox that Crew's would
# nest inside.
ACP_BACKENDS_INTERNAL_SANDBOX = frozenset({ACP_BACKEND_KIRO})

# Backends served by AcpRuntime + AcpSessionHandle — the kiro-agent family
# (kiro-cli and KAS) whose single process hosts N sessions via demux.
# claude-agent-acp runs one AcpClient per session and is NOT a
# member. Membership drives the shared runtime start path and the kiro-family
# spawn conventions: members read the cli.json effort/tool-search overlay and
# receive effort at spawn, whereas claude applies it via a live push after the
# session is ready. Stated as opt-in membership (harness-parity H5/H6) so the
# four sites that mean "kiro or kas" say so positively rather than as
# ``not is_claude_backend`` — an inference that silently captures every harness
# added later. This is a SUPERSET of ACP_BACKENDS_SESSION_SHARING: running on
# AcpRuntime is necessary for session sharing but not sufficient (KAS runs here
# yet is excluded from sharing until keep-aware teardown lands). codex-acp is not a
# member: it is spawned per session and reads none of the kiro-family cli.json
# overlay, so it takes the AcpClient path.
ACP_BACKENDS_ACP_RUNTIME = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends whose sign-in lives in kiro-cli's OWN identity store, so an external
# ``kiro-cli logout`` (or a switch to another account) invalidates a process that
# is already running. Membership is what authorizes retiring a live session's
# child when that store starts naming a different account: a harness
# authenticated some other way must not be recycled on a store it never reads.
# KAS is a member: it is spawned as ``kiro-cli acp --agent-engine v3
# --auth-method cli`` (see :mod:`kiro_crew.acp.kas_transport`), and that
# ``--auth-method cli`` is precisely the demonstration this set waits for — the
# relay resolves every access token from kiro-cli's own store, so a logout that
# invalidates the kiro backend invalidates a running KAS relay identically.
# Excluding it would let a KAS session keep serving turns on the previous
# account's credentials. Positive membership rather than "not claude"
# (harness-parity H5).
#
# codex-acp is excluded: it signs in through its own credentials file, so a
# kiro-cli logout says nothing about whether a running codex session is still
# authenticated, and retiring its child on that signal would end a live turn for
# no reason.
ACP_BACKENDS_KIRO_IDENTITY_STORE = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends that switch models through ``session/set_config_option("model", ...)``
# rather than the kiro-native ``session/set_model`` request. Opt-in for the same
# reason as every set above: a switch sent down a channel the adapter does not
# implement is answered with method-not-found, and the session keeps serving turns
# on the model the operator thought they had just left.
ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION = frozenset({ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX})

# Backends that take a reasoning-effort change through
# ``session/set_config_option("effort", ...)``. A SEPARATE set from the model
# channel above despite identical membership today: the two config options are
# advertised independently, and ``AcpClient.supports_config_option`` exists
# precisely because adapter builds ship one without the other. Collapsing them
# would make an adapter that gained model-switching inherit an effort channel it
# never advertised.
ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION = frozenset({ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX})

# Backends implementing ``_kiro.dev/commands/execute`` — the kiro extension that
# runs a slash command as an RPC. Non-members have no equivalent verb, so their
# slash commands go through ``session/prompt`` and are interpreted by the adapter
# (or degrade to prompt text) instead of returning -32601 for the whole call.
#
# The same membership decides who reads the workspace ``cli.json`` overlay: the
# kiro-family harnesses take effort and Tool Search from that file at spawn, and
# writing it for a harness that never reads it leaves a stale file in the user's
# workspace that no later clear can reach.
ACP_BACKENDS_KIRO_SLASH_COMMANDS = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})
