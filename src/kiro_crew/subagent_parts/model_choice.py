"""Which model and reasoning effort a sub-agent run uses, and the notes that tell the
parent whether an effort was applied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        DEFAULT_MODEL,
        Any,
        SubagentInfo,
        asyncio,
        effort_settings_key,
        logger,
        model_supports_effort,
    )


def _resolved_model_of(client: object) -> str:
    """The model id *client*'s live session actually resolved to serve, or ``""``.

    Reads the provider's PUBLIC ``served_model`` accessor (never private
    ``_client`` internals, which are free to move) — the same contract the
    poisoned-conversation canary and ``AcpProvider.served_model`` use. Both
    provider shapes are covered: ``AcpSessionProvider.served_model`` prefers the
    explicit ``set_model`` and falls back to the ``session/new|load`` response's
    ``currentModelId`` (so a session on the backend-selected DEFAULT is still
    readable at spawn), while the raw ``AcpClient`` reports ``_resolved_model_id``
    once the backend has answered (known after the first turn on the CC path).

    The ``DEFAULT_MODEL`` (``"auto"``) sentinel — "let the backend pick", not yet
    resolved — is filtered to ``""`` (unknown/inconclusive) so a caller never
    renders it as if it were a real model, and callers must treat ``""`` as
    "don't show", never as a wildcard. Never raises — an unreadable or
    duck-typed client (test doubles) yields ``""``.
    """
    try:
        model = str(getattr(client, "served_model", "") or "").strip()
    except Exception:
        return ""
    return "" if model == DEFAULT_MODEL else model


def _subagent_default_model(cfg: Any = None) -> str:
    """Explicit sub-agent model pin (``agent.role_models['subagent']``), or ``""``.

    Returns ``""`` when the sub-agent role is unpinned so the caller OMITS the
    model kwarg and keeps deferring to the provider's configured default —
    rather than forcing the chat default on as an explicit override (which also
    breaks callers/mocks that don't expect the kwarg). Only a deliberate pin
    overrides. Never raises. *cfg* is a config the caller already loaded, so a
    caller holding one does not load it again here.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig, normalize_agent_model

        cfg = cfg if cfg is not None else KiroCrewConfig.load()
        model = normalize_agent_model(cfg.agent.role_models.get("subagent", ""))
        return model if isinstance(model, str) else ""
    except Exception:
        return ""


def _subagent_default_effort(cfg: Any = None) -> str:
    """Explicit sub-agent effort pin (``agent.role_efforts['subagent']``), or ``""``.

    Returns ``""`` when unpinned so the caller omits ``reasoning_effort_override``
    and the factory's default effort applies. Only a deliberate pin overrides.
    Never raises. *cfg* as for :func:`_subagent_default_model`.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = cfg if cfg is not None else KiroCrewConfig.load()
        val = cfg.agent.role_efforts.get("subagent", "")
        return val if isinstance(val, str) else ""
    except Exception:
        return ""


async def _route_unpinned_start(info: "SubagentInfo", kind: str, sessions: Any) -> None:
    """Route an UNPINNED start through ``model.route``'s derived tiers. Never raises.

    A start is unpinned when it carries no model of its own (``info.model``), no
    role pin (``agent.role_models['subagent']``), no named agent, crew or backend,
    and is not a continued conversation: each of those already chose its model,
    and a chosen model is never routed. ``model_route`` answers only while
    ``decisions.model_route_derive`` is on, so with it off this sends, writes and
    changes nothing.

    The task is classified by the question a chat turn gets, under the PARENT's
    session key, and priced against the model the parent runs with routing off
    (:func:`~kiro_crew.decisions.points.model_route.inherited_baseline`) -- not its
    live model, which is the tier its last routed turn landed on. A parent with
    no live provider, or a substitute serving it with nothing named to go back
    to, routes nothing. Asked ONCE per run: a recovery respawn of a start already
    routed keeps that routing, because a dead process or an orphaned session says
    nothing about whether the tier was right. Sets ``info.routed_model`` /
    ``info.routed_tier``; ``routed_model`` stays ``""`` when the tier keeps the
    inherited model.
    """
    if (
        info.routed_tier
        or info.model
        or info.agent
        or info.crew
        or info.backend is not None
        or info.conversation_key
        or kind != "template"
        or not info.parent_session_key
    ):
        return
    try:
        from kiro_crew.decisions.points import model_route
        from kiro_crew.llm_helpers import provider_advertised_ids

        if not model_route.derive_enabled():
            return
        if await asyncio.to_thread(_subagent_default_model):
            return
        provider = sessions.get_provider(info.parent_session_key)
        if provider is None:
            return
        baseline = model_route.inherited_baseline(provider)
        if baseline is None:
            return
        advertised = provider_advertised_ids(provider)
        chosen, tier = await model_route.routed_spawn_model(
            info.task,
            session_key=info.parent_session_key,
            current_model=baseline,
            advertised=advertised,
            run_id=info.id,
            rate_of=model_route.rate_lookup(provider),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "model.route: leaving sub-agent %s on its inherited model", info.id, exc_info=True
        )
        return
    if tier:
        info.routed_tier = tier
        info.routed_model = chosen


def _spawn_effective_model(
    model: str, agent: str, *, crew_agent: str | None = None, backend: str | None = None
) -> str | None:
    """Resolve the factory's model; ``""`` means auto, ``None`` means unavailable.

    Not a re-encoding of the factory's precedence — the selection itself is
    :meth:`KiroCrewConfig.acp_effective_model`, the same function the factory
    calls, so this verdict cannot drift from the gate it reports on. What this
    wrapper reproduces is only the CALLER side of the chain, exactly as the
    spawn path drives ``get_or_create``: the kwarg the spawn passes (explicit
    per-spawn *model*, else the subagent role pin — see ``_run_inner``, which
    forwards raw ``info.model`` including an explicit ``"auto"``), and, when no
    kwarg is passed, ``session._session_model`` for *agent* (the session's own
    ladder: crew pin, template pin, global, installed agent file).
    An explicit empty ``crew_agent`` retains the template namespace; a member
    claim retains that member's pin and bound template. Omitted claims keep the
    helper's crew-name inference. Reporting never prepares a capability runtime.
    """
    try:
        # circular imports (config.loader / session import sibling modules at
        # load time, matching the lazy-import convention of _subagent_default_*)
        from kiro_crew.config.loader import KiroCrewConfig, resolve_crew_identity
        from kiro_crew.session import _session_model

        # The kwarg the spawn path actually passes (see _run_inner): raw
        # info.model — an explicit "auto" flows through VERBATIM and the
        # factory treats it as a truthy override — else the role pin.
        override: str | None = model or _subagent_default_model() or None
        cfg = KiroCrewConfig.load()
        claim = resolve_crew_identity(cfg, agent or None, crew_agent)
        if claim:
            member = cfg.agents.get(claim)
            if member is None or not isinstance(member.kiro_agent, str):
                return None
            # The factory receives the bound provider template, never the alias.
            # This matters when the member defers to the template's own model.
            agent = member.kiro_agent
        if override is None:
            # No kwarg: get_or_create resolves the session chain and passes
            # its result (possibly None) as model_override.
            override = _session_model(
                cfg, agent or None, crew_agent=claim, backend_override=backend
            )
        # A per-spawn backend is translated into ITS namespace, as the factory
        # does once the override wins the selection gate.
        namespace = None
        if backend is not None:
            from kiro_crew.agent_sdk.capabilities import capabilities_for

            namespace = capabilities_for(backend).model_id_namespace
        return cfg.acp_effective_model(agent or None, override, namespace=namespace) or ""
    except Exception:
        return None


def _effort_channel(backend: str | None) -> str | None:
    """How *backend* receives a reasoning-effort level, or ``None`` if it takes none.

    ``"cli_json"`` for the Kiro family (kiro-cli reads the level from its
    workspace ``cli.json`` at spawn), ``"config_option"`` for a harness that takes
    it as a live ``set_config_option`` push. Both answers are opt-in MEMBERSHIP in
    the sets the provider itself gates on, never "not one of the others": a
    harness in neither set never receives the level. ``None`` (no per-spawn
    backend) is the gateway default, which the verdict has always described as
    the Kiro family.
    """
    from kiro_crew.acp_backends import (
        ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
        ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    )

    if backend is None or backend in ACP_BACKENDS_KIRO_SLASH_COMMANDS:
        return "cli_json"
    if backend in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION:
        return "config_option"
    return None


def _effort_judged_at_session_start(backend: str | None) -> bool:
    """Whether *backend* advertises its own effort levels, so no verdict is possible here.

    Such a harness reports its model ids and effort levels with ``session/new``,
    and the provider validates the level then (``AcpProvider._resolve_effort``);
    the registry the verdict reads has none of them. Only a per-spawn backend is
    asked: without one the verdict keeps describing the default, as before.
    """
    if backend is None:
        return False
    from kiro_crew.acp_backends import ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION

    return backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION


def effort_drop_reason(
    model: str,
    reasoning_effort: str,
    agent: str = "",
    *,
    crew_agent: str | None = None,
    backend: str | None = None,
) -> str:
    """Why a requested per-spawn effort will not take effect, or ``""``.

    Mirrors the model resolution the provider factory's effort gate actually
    sees (explicit per-spawn model, else the subagent role pin, else the selected
    member's pin, template pin and global fallback). Returns a human-readable
    reason when *reasoning_effort* is set but the resolved model is not
    effort-capable; ``""`` means the effort will be delivered, none was
    requested, its outcome is decided at session start
    (:func:`effort_pending_note`), or the selection could not be resolved.
    Reporting-only: never raises and never influences whether or how a spawn
    proceeds. ``crew_agent`` has the same namespace semantics as allocation.
    """
    if not reasoning_effort or _effort_judged_at_session_start(backend):
        return ""
    if _effort_channel(backend) is None:
        return f"backend '{backend}' takes no reasoning-effort level, so it is not applied"
    resolved = _spawn_effective_model(model, agent, crew_agent=crew_agent, backend=backend)
    if not resolved:
        return ""
    if not model_supports_effort(resolved):
        return f"model '{resolved}' does not support effort configuration"
    return ""


def effort_applied_note(
    model: str,
    reasoning_effort: str,
    agent: str = "",
    *,
    crew_agent: str | None = None,
    backend: str | None = None,
) -> str:
    """The delivery mirror of :func:`effort_drop_reason`, or ``""``.

    Names the resolved model and the family-specific cli.json settings key the
    level is delivered under (``reasoning`` for GPT, ``output_config`` for
    Claude) when a requested per-spawn effort WILL take effect. The key matters
    because kiro-cli silently ignores a level written under the wrong family
    key, so a bare "applied" would leave that failure mode unobservable.
    For a requested effort whose selection resolves, exactly one of this note,
    the drop reason and :func:`effort_pending_note` is non-empty. An unavailable
    selection leaves all three empty, and so does a per-spawn *backend* that
    advertises its own effort levels. Reporting-only, same totality contract.
    """
    if not reasoning_effort or _effort_judged_at_session_start(backend):
        return ""
    resolved = _spawn_effective_model(model, agent, crew_agent=crew_agent, backend=backend)
    if not resolved or not model_supports_effort(resolved):
        return ""
    channel = _effort_channel(backend)
    if channel == "config_option":
        from kiro_crew.acp_backends import effort_config_option_id

        return f"{resolved} → config option {effort_config_option_id(str(backend))}"
    if channel != "cli_json":
        return ""
    return f"{resolved} → {effort_settings_key(resolved)}.effort"


def effort_pending_note(
    model: str,
    reasoning_effort: str,
    agent: str = "",
    *,
    crew_agent: str | None = None,
    backend: str | None = None,
) -> str:
    """Why a requested per-spawn effort can only be judged at session start, or ``""``.

    With no model pinned there is no model to judge the level against yet. The
    factory carries the level unkeyed and the provider binds it at start to the
    model the backend reports serving (``AcpProvider._bind_unbound_effort``),
    which is not known here on any backend: an unpinned kiro-cli serves its own
    ``chat.defaultModel`` when one is set, and ``auto``, which takes no level,
    when none is. The provider decides there and logs a level it does not
    apply, so this reports neither a drop nor a delivery. Third outcome beside
    :func:`effort_drop_reason` and :func:`effort_applied_note`, same totality
    contract.
    """
    if not reasoning_effort or _effort_judged_at_session_start(backend):
        return ""
    if _effort_channel(backend) is None:
        return ""
    resolved = _spawn_effective_model(model, agent, crew_agent=crew_agent, backend=backend)
    if resolved != "":
        return ""
    return (
        "no model is pinned, so it is bound at start to the model the backend "
        "serves: applied if that model takes effort, otherwise not applied "
        "(logged by the gateway); pass an effort-capable model= to know now"
    )
