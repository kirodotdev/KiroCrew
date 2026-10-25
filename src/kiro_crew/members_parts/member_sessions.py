"""A crew member's pinned DM session.

Whether a session key addresses one, the backend a member session routes to, the two
session-level MCP mounts (session control and the crew panel) with the one writer of
their shape, the slot key and session-map alias, and the per-turn lifecycle verdict the
member context reads.

:mod:`kiro_crew.members` stays the import path and the patch surface; see
:mod:`kiro_crew.members_parts`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.members_parts.member_space import (
    DM_SLOT_KEY_PREFIX,
    MEMORY_STORE_SLOT_SUFFIX,
    validate_slug,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.members")


def is_member_session_key(session_key: str | None) -> bool:
    """Whether *session_key* addresses a crew member's pinned DM session.

    Member slots are created only by the members thread endpoint with keys of
    the form ``member-<slug>``. That slot name travels under several prefixes
    depending on the layer: ``dashboard_member-<slug>`` (the chat session key)
    and ``dashboard:member-<slug>`` (the canonical session-map alias, which is
    what reaches the provider factory). Accepts all three spellings so the
    predicate works at every layer a key travels through.
    """
    if not session_key:
        return False
    key = session_key
    for prefix in ("dashboard_", "dashboard:"):
        if key.startswith(prefix):
            key = key[len(prefix) :]
            break
    return key.startswith(DM_SLOT_KEY_PREFIX)


def _slot_part(session_key: str) -> str:
    """*session_key* without the ``dashboard_`` / ``dashboard:`` layer prefix."""
    for prefix in ("dashboard_", "dashboard:"):
        if session_key.startswith(prefix):
            return session_key[len(prefix) :]
    return session_key


#: Slot keys of the sessions known to be a crewmate's own worker, in this process.
#: Filled from the worker's execution record (``mate_worker.note_worker_session``)
#: at its birth and on every read of the record, so the backend selection below is
#: a pure in-memory test, never a file read on the event loop.
_CREWMATE_WORKER_SLOTS: set[str] = set()


#: The crewmate worker sessions whose live provider takes the worker's prompt only
#: from the ``[AGENT SYSTEM PROMPT]`` block. Written at allocation from the provider
#: itself (``mate_worker_prompt_in_context``), so a later config change cannot move
#: a running worker's prompt to the other channel.
_CREWMATE_WORKER_PROMPT_IN_CONTEXT: set[str] = set()


def note_crewmate_worker_prompt_channel(session_key: str, in_context: bool) -> None:
    """Record whether worker *session_key*'s live provider reads its prompt in context."""
    if not session_key:
        return
    if in_context:
        _CREWMATE_WORKER_PROMPT_IN_CONTEXT.add(_slot_part(session_key))
    else:
        _CREWMATE_WORKER_PROMPT_IN_CONTEXT.discard(_slot_part(session_key))


def crewmate_worker_prompt_in_context(session_key: str | None) -> bool:
    """Whether worker *session_key*'s live provider reads its prompt in context."""
    return bool(session_key) and _slot_part(str(session_key)) in _CREWMATE_WORKER_PROMPT_IN_CONTEXT


def note_crewmate_worker_session(session_key: str) -> None:
    """Record *session_key* as a crewmate's own worker session (any key spelling)."""
    if session_key:
        _CREWMATE_WORKER_SLOTS.add(_slot_part(session_key))


def forget_crewmate_worker_session(session_key: str) -> None:
    """Drop *session_key* from the crewmate workers (any key spelling).

    A session switched off the worker template is not its crewmate's worker, so
    its next allocation takes the configured default backend.
    """
    if session_key:
        _CREWMATE_WORKER_SLOTS.discard(_slot_part(session_key))
        _CREWMATE_WORKER_PROMPT_IN_CONTEXT.discard(_slot_part(session_key))


def is_crewmate_worker_session(session_key: str | None) -> bool:
    """Whether *session_key* is a session this process knows as a crewmate's worker.

    Positive identity: only a key whose execution record was read as the
    crewmate's worker (the worker template on that crewmate's store) answers True.
    """
    return bool(session_key) and _slot_part(str(session_key)) in _CREWMATE_WORKER_SLOTS


def select_provider_backend(
    session_key: str | None,
    member_backend: str,
    configured_default: str,
    backend_override: str | None = None,
) -> str:
    """The per-session half of the ONE backend-selection gate (H3/H13).

    Precedence: the caller's explicit backend (``backend_override``, a
    sub-agent spawned with ``spawn_run(backend=...)``), then the member-DM
    auto-route, then the configured default. A crewmate's own worker session
    (:func:`is_crewmate_worker_session`) takes the member route too: a worker
    runs on the same backend as the crewmate that opened it, and its kit is
    applied by that backend's projection. ``None`` is no override, so the
    lower tiers decide exactly as before; ``""`` is Kiro's own id, not "unset".

    The override and member arms go through :func:`resolve_selected_backend` —
    the same governance/selectability gate the persisted field crosses, so a
    denied or unknown value degrades to kiro with the reason in the log. The
    spawn endpoint refuses an unselectable backend up front; this gate is what
    still holds when governance narrows the set between the refusal check and
    the cold start.

    Lives here rather than inline in ``create_provider_factory`` so the
    factory body stays a single selection CALL with no branching of its own:
    harness-parity H3/H13 allow exactly one selection gate on the construction
    path, and this function is an input to that gate, not a second one.
    """
    from kiro_crew.acp_backends import resolve_selected_backend

    if backend_override is not None:
        backend = resolve_selected_backend(backend_override)
        logger.info(
            "session %s: backend override %r resolved to acp_backend=%r",
            session_key,
            backend_override,
            backend,
        )
        return backend
    if is_member_session_key(session_key) or is_crewmate_worker_session(session_key):
        backend = resolve_selected_backend(member_backend)
        logger.info(
            "member session %s: routing to acp_backend=%r " "(agent.member_acp_backend=%r)",
            session_key,
            backend,
            member_backend,
        )
        return backend
    return configured_default


#: MCP server mounted per session into member DM threads — the delivery vehicle
#: for the member operating model (dispatch work into worker sessions, patrol
#: them). Session-level, so the on-disk agent template is untouched and every
#: other session on the same template keeps its ordinary tool set.
MEMBER_DISPATCH_SERVER = "kirocrew-dashboard"


def member_dispatch_session_server(
    session_key: str, session_token: str = ""
) -> dict[str, object] | None:
    """ACP ``session/new`` ``mcpServers`` element mounting session control.

    The entry carries ``KIROCREW_SESSION_KEY`` so the server's strict identity
    resolution names this member session — the same per-process trust channel
    the Claude backend's ``AcpClient`` uses. It rides the session-level param,
    which the KAS projection's credential stripping never touches (that filter
    applies to the agent-declared ``mcpServers`` block, not to what the host
    itself injects per session).

    ``session_token`` is this ACP session's own name, and it rides BESIDE the key
    rather than replacing it (see
    :func:`~kiro_crew.providers.mirrors.identity.control_plane_identity_env` for
    the full argument). It matters most on a SHARED runtime, where the key baked
    into this element names the session that was claiming when it was built while
    the token's mapping is republished on every rekey — so a member thread on a
    recycled process resolves to itself rather than to its predecessor. Empty
    leaves the entry byte-identical to the pre-token shape.

    The env also carries ``KIROCREW_BOUND_PORT`` — the port this gateway is
    actually serving. Unlike a chat session's MCP child, which inherits the
    gateway's whole environment, this entry is built from scratch, so without
    the port the child's resolution chain falls through to the run marker; that
    check needs :func:`platform_compat.find_listening_pids` (``lsof``), which
    sees no listener from inside the sandbox's user namespace, and the child
    then dials the default port. On a gateway bound anywhere else that is a
    connection refused on every dispatch call. ``KIROCREW_BOUND_PORT`` rather
    than ``KIROCREW_PORT`` because the latter means "the port an operator
    asked for" and is persisted, while this is the transient fact of what got
    bound.

    It also carries the same home override every managed Crew server carries
    (``_managed_mcp_env``): the server resolves *which gateway* to call from
    its data home, so on an install where ``KIROCREW_HOME`` is set (a pod, a
    second profile) an entry without it would present this member's identity to
    the default home's gateway, which has no such slot and refuses every verb
    as ``caller_unidentified``. On a default install the helper returns nothing
    and the entry is unchanged.

    ``None`` when the server command cannot be resolved — the member thread
    then runs as plain chat and the caller logs the degradation.
    """
    return _member_session_element(
        MEMBER_DISPATCH_SERVER, "mcp-dashboard", session_key, session_token
    )


#: MCP server mounted per session into member DM threads so a crew can publish
#: its own webview (``panel_publish``) and discover what renders it
#: (``panel_templates``). A SECOND session-level mount beside
#: :data:`MEMBER_DISPATCH_SERVER` rather than a widening of it, because
#: assignment in Kiro Crew is per server: the dashboard set is folder
#: organization plus session control, publishing a document is neither, and the
#: two are withdrawn independently (``agent.member_dispatch``,
#: ``agent.crew_panel``).
MEMBER_PANEL_SERVER = "kirocrew-panel"


def member_panel_session_server(
    session_key: str, session_token: str = ""
) -> dict[str, object] | None:
    """ACP ``session/new`` ``mcpServers`` element mounting the crew panel.

    The panel server is ``opt_in`` in ``agent._MANAGED_MCP_SERVERS``, so no spec
    emits it, and the Capabilities editor cannot offer it either: that list is
    built from CONFIGURED connections and a host-managed opt-in server is not
    one. This element is therefore the only path by which a crew member reaches
    its own webview, exactly as :func:`member_dispatch_session_server` is the
    only path to session control.

    Identity carriage, env and failure mode are that function's, through the one
    writer :func:`_member_session_element`: the panel server reads the session's
    tool policy through the same ``mcp_shared.run_mcp_stdio_loop`` path, so an
    entry without the attestation comes up present-but-unusable and refuses
    every call as ``identity_unattested``.

    ``None`` when the server command cannot be resolved; the caller logs the
    degradation and the DM thread keeps the rest of its tools.
    """
    return _member_session_element(MEMBER_PANEL_SERVER, "mcp-panel", session_key, session_token)


def crew_panel_enabled() -> bool:
    """Whether a crew member's DM session is granted its own webview.

    The operator ceiling on the zero-configuration panel grant, and the mirror of
    :func:`~kiro_crew.dashboard.session_control.member_dispatch_enabled`: default
    true is the contract the capability ships with, and ``agent.crew_panel:
    false`` withdraws it from every member at once without editing a spec.

    Fails CLOSED in both directions the ceiling can lose the operator's value,
    for the reason that reader states: a config read that RAISES resolves to
    false, and a config that LOADS having discarded the ``agent`` section
    resolves to false too. ``load()`` does not raise on a malformed section, it
    coerces the section away and falls back to the field default, which is
    permissive, so without the second check a degraded overlay carrying
    ``crew_panel: false`` would silently revert to the grant the operator meant
    to withdraw.
    """
    # circular import: the config loader's provider-backend path imports this
    # module, so both names are resolved at call time like the seams below.
    from kiro_crew.config.loader import DEGRADED_WHOLE_CONFIG, KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        logger.warning(
            "crew_panel: config read failed - withdrawing the panel grant until config loads",
            exc_info=True,
        )
        return False
    if cfg.degraded_sections & {DEGRADED_WHOLE_CONFIG, "agent"}:
        logger.warning("crew_panel: agent config section degraded - withdrawing the panel grant")
        return False
    return bool(cfg.agent.crew_panel)


def _member_session_element(
    server_name: str, invocation: str, session_key: str, session_token: str
) -> dict[str, object] | None:
    """One session-level ``mcpServers`` element for a member DM thread.

    The single writer of the shape both member mounts use. Two servers reach a
    member session and each needs the same three things: the managed home
    override, this session's identity, and the port this gateway actually bound.
    Composing the element twice is how one of them would later be built without
    one of them.

    *invocation* is the managed subcommand (``mcp-dashboard``, ``mcp-panel``).
    ``None`` when it cannot be resolved, which each caller reports in its own
    words because the capability lost differs.
    """
    # circular import: agent's module graph is heavy and imports config, which
    # sits below this module for the thread-endpoint path.
    from kiro_crew.agent import _kirocrew_mcp_invocation, _managed_mcp_env

    # circular import, same shape: port_resolution reaches config.loader, whose
    # provider-backend path imports this module.
    from kiro_crew.port_resolution import resolve_serving_port

    try:
        command, args = _kirocrew_mcp_invocation(invocation)
    except Exception:  # pragma: no cover - defensive; resolver logs its own reason
        logger.warning("member mount: could not resolve the %s server command", server_name)
        return None
    if not command:
        return None
    # The SAME home override every managed Crew server carries
    # (``_managed_mcp_env``): the server resolves the gateway to call from its
    # data home, so on an install with ``KIROCREW_HOME`` set (a pod, a second
    # profile) an entry without it would authenticate as this member to the
    # DEFAULT home's gateway — where the member slot does not exist and every
    # verb is refused as ``caller_unidentified``. Empty on a default install.
    env: list[dict[str, str]] = [{"name": k, "value": v} for k, v in _managed_mcp_env().items()]
    # Then the identity: the signed per-session token first (the resolver reads it
    # first, because it cannot go stale across a rekey), then the key as fallback
    # for the one case the token cannot cover — no SEL trust root to sign with.
    if session_token:
        env.append({"name": STUB_SESSION_TOKEN_ENV, "value": session_token})
    env.append({"name": "KIROCREW_SESSION_KEY", "value": session_key})
    # resolve_serving_port() reads KIROCREW_BOUND_PORT first and only then falls
    # through the client order, so one call covers both "the gateway exported the
    # port it bound" and "derive it" — and a malformed export is ignored rather
    # than forwarded.
    env.append({"name": "KIROCREW_BOUND_PORT", "value": str(resolve_serving_port())})
    return {
        "name": server_name,
        "command": command,
        "args": list(args),
        "env": env,
        "type": "stdio",
    }


def member_slot_key(slug: str, memory_store: str = "") -> str:
    """Derived, stable chat-slot key for a member's pinned DM thread.

    V1 uses the slug; a V2 opt-in uses its private store generation. Nothing is
    read or written. The dashboard's slot layer normalizes keys to a filename-safe
    charset, but a validated slug is already inside that charset, so the
    derived key survives ``_normalize_slot_key`` unchanged; callers must still
    use the slot layer's RETURNED key as the source of truth.
    """
    key = DM_SLOT_KEY_PREFIX + validate_slug(slug)
    if memory_store:
        from kiro_crew.memory_stores import validate_memory_store_name

        validate_memory_store_name(memory_store)
        if memory_store == "default":
            raise ValueError("Global memory has no private conversation generation")
        # The complete store name is already a unique, bounded generation ID.
        key += MEMORY_STORE_SLOT_SUFFIX + memory_store
    return key


def member_thread_session_alias(slug: str, memory_store: str = "") -> str:
    """Canonical session-map alias for a member's pinned DM thread.

    ``dashboard:<slot key>`` — the spelling the session manager and the
    conversation log key a member thread under (see
    :func:`is_member_session_key` for the full set of prefixes a member key
    travels through). This is the ONE derivation every out-of-turn touch of a
    member session goes through — flagging the warm session for re-injection
    after a rules write, probing the thread's on-disk history — so the key
    format lives here rather than being hand-built at each site, where one
    divergent spelling would silently orphan the invariant it serves.
    """
    return f"dashboard:{member_slot_key(slug, memory_store)}"


class MemberLifecycle(str, Enum):
    """Session-lifecycle states a turn can arrive in, as the member-context
    chokepoint distinguishes them.

    Derived by :func:`member_lifecycle` from the same inputs
    ``build_message`` already branches on, so the two can never disagree on
    what state a turn is in:

    * ``FRESH`` — brand-new session; the full session context is built and
      injected.
    * ``SLIM_RESUME`` — the provider restored the native transcript
      (``session/load``); only a minimal header is injected, but the restored
      member section may be stale.
    * ``WARM_REINJECTION`` — follow-up turn whose session-start context was
      compacted away (or deliberately invalidated, e.g. by a rules write);
      the one-shot re-injection flag was consumed for this turn.
    * ``WARM`` — ordinary follow-up turn; the delivered section is still live
      in the provider conversation.
    * ``MINIMAL`` — minimal-context turn (cron); never a member thread by
      contract (``member`` is ``""`` on every such call).
    """

    FRESH = "fresh"
    SLIM_RESUME = "slim_resume"
    WARM_REINJECTION = "warm_reinjection"
    WARM = "warm"
    MINIMAL = "minimal"

    @property
    def delivers_section(self) -> bool:
        """Whether a member turn in this state injects the CURRENT section.

        The lifecycle half of the chokepoint's verdict, exposed on the enum so
        a caller that knows a turn is member-shaped without holding the member
        NAME (the chat runner records delivery-at-stake the moment the session
        client exists, before the context build resolves the name) reads the
        same single source of truth :func:`member_turn_context` does.
        """
        return self in (
            MemberLifecycle.FRESH,
            MemberLifecycle.SLIM_RESUME,
            MemberLifecycle.WARM_REINJECTION,
        )


# Named by the path callers import it from, like the exception classes: what
# names a class by ``module.qualname``, pickle first, keeps the facade path.
MemberLifecycle.__module__ = "kiro_crew.members"


@dataclass(frozen=True)
class MemberTurnContext:
    """What the member layer must do on ONE turn — the chokepoint's verdict.

    Exactly one of the two flags is set for a member turn (both are ``False``
    only when the turn carries no member, or on ``MINIMAL`` turns, which
    carry no member by contract):

    * ``deliver_section`` — inject the CURRENT four-layer member section this
      turn. Delivery itself enforces the rules gate: the section builder
      reads the user's [PERMANENT RULES] fresh, and an existing-but-unreadable
      rules file aborts the turn (fail closed) instead of running the member
      unbounded.
    * ``enforce_rules_gate`` — the section is already live in the provider
      conversation, so nothing is injected, but the fail-closed rules read
      still runs: a first-turn abort leaves a warm session, and without this
      per-turn check the member would keep running after its rules file went
      unreadable.
    """

    member: str
    lifecycle: MemberLifecycle
    deliver_section: bool
    enforce_rules_gate: bool


def member_lifecycle(
    *,
    is_new_session: bool,
    resumed: bool,
    minimal_context: bool,
    needs_reinjection: bool,
) -> MemberLifecycle:
    """Map ``build_message``'s branch inputs to one lifecycle state.

    Mirrors the branch structure of ``build_message`` exactly — including the
    precedence quirks a hand-written table would have to document:
    ``minimal_context`` beats ``resumed`` (a minimal resumed build early-returns
    before any member handling), and ``needs_reinjection`` only matters on warm
    turns (on a new session the full/slim injection path already delivers).
    """
    if is_new_session:
        if minimal_context:
            return MemberLifecycle.MINIMAL
        if resumed:
            return MemberLifecycle.SLIM_RESUME
        return MemberLifecycle.FRESH
    if needs_reinjection:
        return MemberLifecycle.WARM_REINJECTION
    return MemberLifecycle.WARM


def member_turn_context(member: str, lifecycle: MemberLifecycle) -> MemberTurnContext:
    """THE decision point for the rules-currency invariant.

    Invariant: **every member turn runs under the user's CURRENT rules.**
    Every lifecycle state satisfies it one of two ways — deliver the current
    section (which reads the rules, fail closed), or run the standalone
    fail-closed rules read against the section already live in the provider
    conversation. All delivery branches call this function instead of
    branching by hand, so a future lifecycle state added to ``build_message``
    cannot silently skip both the member section and the rules gate: it has
    to be given a verdict here first, where the mapping is pinned by tests.

    ``MINIMAL`` is the one deliberate exception — such turns are never member
    threads (``member`` is ``""`` by contract at every call site), and a
    member name arriving anyway gets neither delivery nor gate, exactly as
    the branch structure disposes of it (the minimal build early-returns
    before any member handling).

    An empty *member* means the turn has no member layer at all: nothing is
    delivered and nothing is gated, whatever the lifecycle.
    """
    # Deny by default. Both verdict predicates are identity/membership tests
    # that answer False for anything that is not a genuine enum member — and
    # the str mixin makes a bare "warm" compare EQUAL to the member while
    # failing both — so an unrecognized lifecycle would otherwise yield the
    # one combination the invariant forbids (no delivery, no gate) silently,
    # at the single decision point the invariant rests on. Refuse loudly
    # instead.
    if not isinstance(lifecycle, MemberLifecycle):
        raise TypeError(f"lifecycle must be MemberLifecycle, got {lifecycle!r}")
    if not member:
        return MemberTurnContext(
            member="", lifecycle=lifecycle, deliver_section=False, enforce_rules_gate=False
        )
    gate = lifecycle is MemberLifecycle.WARM
    return MemberTurnContext(
        member=member,
        lifecycle=lifecycle,
        deliver_section=lifecycle.delivers_section,
        enforce_rules_gate=gate,
    )
