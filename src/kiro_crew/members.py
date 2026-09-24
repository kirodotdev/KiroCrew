"""Per-crew-member space: ``$KIROCREW_HOME/members/<slug>/``.

A crew member is the same agent running with different context, so its space
holds what belongs to that member alone rather than to the user as a whole. The
first occupant is ``activity.jsonl`` — pointers to the sessions the member took
part in, which is the signal trigger generation reads.

The directory name is a **slug**: stable, immutable, and path-safe. A member's
display name is editable independently, so a rename never has to move files.
This mirrors the artifact store's ``artifacts/<slug>/`` layout, and reuses its
:func:`~kiro_crew.artifacts.slugify` so both surfaces normalize names the same
way.

Activity entries are pointers by design: they carry the session key, not a copy
of what happened. Details are read back from the session itself, so the log
cannot drift from the transcript — and it survives session pruning, which is why
frequency counts taken from it stay stable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.artifacts import slugify
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.config.paths import data_home
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.slugs import slug_hash_fallback

logger = logging.getLogger(__name__)

#: Directory under the data home holding one subdirectory per crew member.
MEMBERS_DIR_NAME = "members"

#: Append-only pointer log inside a member's directory.
ACTIVITY_FILE_NAME = "activity.jsonl"

# LEGACY ONLY. Nothing writes this file: participation events go to the
# per-member append-only event log, and :func:`read_activity` reads that log
# rather than this path. It survives as a migration SOURCE, folded in once per
# member and then retired by rename (see the event-log service). Bounding it by
# size therefore has nothing to bound, since the writer that grew it is gone;
# the fold streams it under its own byte budget instead, because the file stays
# agent-writable whatever its size.

#: Crew-slug -> DM-thread binding inside a member's directory.
DM_FILE_NAME = "dm.json"

#: Per-member PERMANENT RULES (the user-owned layer of the member system
#: prompt: approval boundaries, forbidden actions, evidence requirements).
#: Lives under the keystone-gated ``trust/`` subtree for the same reason the
#: DM binding does: these rules are precisely what the member must not be able
#: to rewrite for itself, so they cannot sit on a path the agent's file tools
#: can write. Only the gateway (via the dashboard rules endpoint — a human
#: action) writes here.
RULES_DIR_NAME = "member-rules"

#: Hard cap on a member's permanent-rules text. Enforced on WRITE (the write
#: is a human dashboard action, so a too-long payload is refused loudly)
#: rather than truncated on read — silently dropping the tail of a rules
#: document would drop rules.
MEMBER_RULES_MAX_CHARS = 4000

#: Per-member self-maintained briefing (the member-owned layer of the member
#: system prompt: current priorities, pointers to its own scripts and notes).
#: Lives in the member's OWN directory — deliberately agent-writable, since
#: the whole point is that the member curates what its future self wakes up
#: knowing. Precedence is fixed by injection order, not trust: rules outrank
#: the briefing because they are injected above it and named as user-owned.
BRIEFING_FILE_NAME = "briefing.md"

#: Cap on how much of the briefing is INJECTED per turn (working memory, not
#: an archive). Enforced on read with a visible truncation marker so the
#: member learns its briefing overflowed instead of silently losing the tail.
MEMBER_BRIEFING_MAX_CHARS = 4000
# Bindings live under the keystone-gated ``trust/`` subtree, NOT inside the
# member's own directory. The binding IS the thread's identity authority (the
# resume/send/thread-open guards all defer to it precisely because transcript
# metadata is operator-editable), so it must not sit on a path the agent's
# file tools can write: a prompt-injected write that re-points ``member`` at a
# colliding crew would hand that crew the thread's entire transcript at the
# next restore. ``trust/`` is already in the sensitive-path floor as a whole
# directory (like the SEL HMAC key and Spec Builder's decision record), and
# keystone writers open paths there directly, so the gateway keeps working.
DM_BINDINGS_DIR_NAME = "member-bindings"

#: Slot ``mode`` tag for member DM threads. The frontend's single
#: chat-ownership predicate (``isChatPageSurface``) does not admit it, so a
#: slot born with this mode is excluded from the ordinary Sessions list on
#: every consumer with no filtering code of its own.
DM_SLOT_MODE = "member"

#: Slot-key prefix for member DM threads (``member-<slug>``), following the
#: existing ``<kind>-<id>`` key convention (``chat-<N>-<ts>``, ``cron-<id>``).
DM_SLOT_KEY_PREFIX = "member-"

#: Appended to a member's slot key when that member opts into a private memory
#: store (``member-<slug>.memory-<store>``). It belongs to the SLOT, not to the
#: slug, so every reader of a slot key drops it before treating the tail as a
#: slug -- ``validate_slug`` rejects the ``.`` otherwise.
MEMORY_STORE_SLOT_SUFFIX = ".memory-"


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


def select_provider_backend(
    session_key: str | None,
    member_backend: str,
    configured_default: str,
) -> str:
    """The per-session half of the ONE backend-selection gate (H3/H13).

    Precedence: the member-DM auto-route, then the configured default. The
    member arm goes through :func:`resolve_selected_backend` — the same
    governance/selectability gate the persisted field crosses, so a denied or
    unknown value degrades to kiro and the member thread runs as plain chat.

    Lives here rather than inline in ``create_provider_factory`` so the
    factory body stays a single selection CALL with no branching of its own:
    harness-parity H3/H13 allow exactly one selection gate on the construction
    path, and this function is an input to that gate, not a second one.
    """
    from kiro_crew.acp_backends import resolve_selected_backend

    if is_member_session_key(session_key):
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


# Same shape the artifact store enforces for its slugs: lowercase letters,
# digits and hyphens, 1-80 chars, no leading or trailing hyphen. Kept here as a
# local constant rather than imported because it is a private name there; the
# artifact store remains the source of truth for the spelling.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

#: The ONLY mode whose sessions may be recorded. An allowlist, not a denylist of
#: no-trace modes: a mode that is missing, empty (metadata not yet flushed for a
#: brand-new session) or simply unrecognized would pass a denylist and durably
#: record a private session key in a log that outlives session pruning. Failing
#: closed costs at most a missing entry in an advisory log.
_TRACEABLE_MEMORY_MODES = frozenset({"persistent"})


class MemberSlugError(ValueError):
    """Raised when a member slug is unusable or cannot be allocated."""


def members_root() -> Path:
    """Root directory for member spaces.

    Uses :func:`data_home` rather than :func:`config_dir`: this is reached from
    request and chat paths, and ``config_dir`` re-runs start-of-process
    maintenance (including a destructive leftover sweep) on every call.
    """
    return data_home() / MEMBERS_DIR_NAME


def dm_binding_path(slug: str) -> Path:
    """Absolute path to one member's DM-thread binding, containment-checked.

    Lives under the keystone-gated ``trust/`` subtree (see the note on
    ``DM_BINDINGS_DIR_NAME``): agent file tools cannot reach it, the gateway
    opens it directly. One flat ``<slug>.json`` per member — the slug is
    already validated to a safe charset, so the filename cannot traverse.
    Does NOT create the directory; :func:`write_dm_binding` does on demand.
    """
    validate_slug(slug)
    root = (data_home() / "trust" / DM_BINDINGS_DIR_NAME).resolve()
    target = (root / f"{slug}.json").resolve()
    # Defence in depth behind validate_slug, mirroring member_dir: a symlinked
    # component must not land the binding outside its trust-rooted directory.
    if target.parent != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


def validate_slug(slug: str) -> str:
    """Return *slug* unchanged when it is well-formed, else raise.

    The pattern admits no ``/``, ``.`` or whitespace, so a validated slug cannot
    traverse out of :func:`members_root` on its own. :func:`member_dir` still
    re-checks containment, because validation and use are separated by a call
    boundary a future caller could bypass.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise MemberSlugError(f"invalid member slug {slug!r}: must match {_SLUG_RE.pattern}")
    return slug


def slug_for_name(name: str) -> str:
    """Derive a candidate slug from a free-form member name.

    Not guaranteed unique: slugification is lossy, so two distinct member names
    can map to one slug. :func:`record_activity` stores the exact name in each
    entry so attribution survives that. Falls back to
    ``"member"`` when the name has no slug-safe characters, so a name written
    entirely in punctuation still yields something addressable.
    """
    base = slugify(name)
    # slugify hash-falls-back under its own module's noun; a member's stored
    # activity, rules, and DM bindings are addressed by this slug, so keep the
    # documented "member" fallback rather than adopting the artifact-prefixed
    # hash (which would also strand data recorded under "member").
    if base == slug_hash_fallback(name, "artifact"):
        base = "member"
    return validate_slug(base)


def member_slug(name: str, config=None) -> str:
    """Use persisted member identity; legacy members retain their existing slug."""
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    agent = config.agents.get(name)
    member_id = getattr(agent, "member_id", "") if agent else ""
    return validate_slug(member_id) if member_id else slug_for_name(name)


def _stable_member_slug(slug: str, name: str) -> bool:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    agent = cfg.agents.get(name)
    return bool(agent and getattr(agent, "member_id", "") == slug)


def member_dir(slug: str) -> Path:
    """Absolute path to one member's directory, containment-checked.

    Does NOT create the directory; :func:`record_activity` creates it on demand.
    """
    validate_slug(slug)
    root = members_root().resolve()
    target = (root / slug).resolve()
    # Defence in depth behind validate_slug: a symlinked root, or a future
    # caller that skipped validation, must not land outside the members root.
    if target != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


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


def read_dm_binding(slug: str) -> dict | None:
    """Return a member's DM-thread binding, or ``None`` when absent/unusable.

    Total by contract, like :func:`read_activity`: a bad slug, a missing file,
    an unreadable file, or a malformed payload all read as "not bound" — the
    binding is idempotently re-creatable, so degrading to re-creation is
    always safe and the caller needs no try/except.

    Blocking file IO: call via ``asyncio.to_thread`` from async code. The read
    goes through :func:`read_bytes_with_retry`, which retries a transient
    Windows sharing violation (an AV/indexer handle on the file this
    function's write-side twin, :func:`write_dm_binding`, just atomically
    replaced) — off-loop only, matching this function's own calling contract.
    """
    try:
        path = dm_binding_path(slug)
    except (MemberSlugError, OSError, RuntimeError):
        # member_dir resolves (and may mkdir) real filesystem paths, so an
        # unreadable directory or a symlink loop surfaces here — the totality
        # contract above says every such state reads as "not bound", and the
        # restore paths rely on that to survive any on-disk state at boot.
        return None
    try:
        raw = read_bytes_with_retry(path).decode("utf-8")
    except (OSError, UnicodeError):
        # Invalid UTF-8 is the same totality case as an unreadable file: the
        # binding reads as absent, never as a 500 out of every member API.
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    # A binding is only usable if it names the thread's slot and the exact
    # member it belongs to. Slugification is lossy (two crew names can share
    # one slug and therefore one dm.json), so the member name inside the
    # payload — not the directory — is what attributes the thread.
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("slot_key"), str)
        or not data["slot_key"]
        or not isinstance(data.get("member"), str)
        or not data["member"]
    ):
        return None
    # The slot key is a pure derivation of the slug; any other value is a
    # malformed or tampered binding. Accepting it would let dm.json point the
    # roster (and the page, which trusts `bound` rows enough to skip the
    # create POST) at an arbitrary unrelated session. Treat non-canonical as
    # absent — the thread endpoint then repairs it to the derived key.
    generation = data.get("memory_store", "")
    if not isinstance(generation, str):
        return None
    try:
        canonical = member_slot_key(slug, generation)
    except ValueError:
        return None
    if data["slot_key"] != canonical:
        return None
    # And the member must actually BELONG to this slug: a tampered dm.json in
    # slug A's directory naming crew B (a real, registered crew whose slug
    # differs) would otherwise pin A's thread — and A's restored transcript —
    # to B's identity. Colliding names are fine: every name that slugifies to
    # this slug passes; anything else reads as absent.
    if data.get("member_id") == slug:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.execution_context import member_config_for_id

        try:
            alias, _ = member_config_for_id(KiroCrewConfig.load(), slug)
        except ValueError:
            return None
        data["member"] = alias
    elif slug_for_name(data["member"]) != slug:
        return None
    return data


def write_dm_binding(slug: str, *, member: str, slot_key: str, memory_store: str = "") -> dict:
    """Persist a member's DM-thread binding atomically; return the record.

    ``slot_key`` must be the slug's own derivation — the same canonicality
    invariant :func:`read_dm_binding` enforces on the way back. Writing any
    other value would produce a binding that always reads as absent, so the
    mismatch is a caller bug worth failing loudly on.

    Raises :class:`MemberSlugError` on a bad slug and lets ``OSError``
    propagate: unlike the advisory activity log, the caller (the thread
    get-or-create endpoint) must know the binding did not land so it can
    report failure instead of advertising a thread that will not be found
    again.

    No fsync, deliberately: the binding is re-derivable — the slot key is a
    pure function of the slug and the endpoint that writes it is idempotent —
    so losing it to a crash costs one re-create, while a durability barrier
    would stall the event-loop thread pool for every thread open. The write
    itself is atomic (unique temp file + rename), so a torn file is never
    observable. Not a secret (a slot key and a crew name), so no owner-only
    permission tightening.
    """
    path = dm_binding_path(slug)
    if slot_key != member_slot_key(slug, memory_store):
        raise ValueError(
            f"non-canonical dm binding slot_key {slot_key!r} for slug {slug!r} "
            f"(expected {member_slot_key(slug, memory_store)!r}); such a binding always reads back as absent"
        )
    binding = {
        "member_id": slug if _stable_member_slug(slug, member) else "",
        "member": member,
        "slug": slug,
        "slot_key": slot_key,
        "created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if memory_store:
        binding["memory_store"] = memory_store
    path.parent.mkdir(parents=True, exist_ok=True)
    # The trust subtree is owner-only everywhere else (sel.py creates it 0o700);
    # a parents=True mkdir would otherwise leave a default-mode directory chain.
    # Best-effort: the sensitive-path floor is the real fence, the mode is
    # defence in depth, and a chmod failure must not cost the thread its binding.
    for _dir in (path.parent, path.parent.parent):
        try:
            platform_compat.restrict_dir_to_owner(_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", _dir, exc_info=True)
    # fsync: the binding is the thread's durability anchor — the orphan-history
    # guard REFUSES to rebind a slug whose binding is gone while its transcript
    # survives, so a binding lost to power failure after a transcript flush
    # would strand that transcript behind member_binding_missing. The binding
    # must be at least as durable as the transcript it attributes.
    atomic_write(path, json.dumps(binding, ensure_ascii=False), fsync=True)
    return binding


def slug_from_dm_slot_key(slot_key: str) -> str | None:
    """The member slug a DM slot key names, or ``None`` if it names no member.

    A V2 member opts into a memory store, and ``dm_slot_key`` then appends
    ``.memory-<store>`` to the key. That suffix is part of the SLOT's identity,
    never of the slug, so every reader of a slot key has to drop it -- and a
    reader that forgets sees a slug carrying a ``.``, which ``validate_slug``
    rejects. One spelling here so a third reader cannot drift from the other
    two.
    """
    if not slot_key.startswith(DM_SLOT_KEY_PREFIX):
        return None
    return slot_key[len(DM_SLOT_KEY_PREFIX) :].split(MEMORY_STORE_SLOT_SUFFIX, 1)[0]


def read_dm_binding_for_slot(slot_key: str) -> dict | None:
    """Resolve a member slot without letting a newer generation adopt its history."""
    slug = slug_from_dm_slot_key(slot_key)
    if slug is None:
        return None
    binding = read_dm_binding(slug)
    return binding if binding is not None and binding["slot_key"] == slot_key else None


def member_rules_path(slug: str) -> Path:
    """Absolute path to one member's permanent-rules file, containment-checked.

    Mirrors :func:`dm_binding_path`: keystone-gated ``trust/`` subtree, one
    flat ``<slug>.json`` per member, symlink containment re-checked behind
    ``validate_slug``. JSON rather than bare text because the payload records
    the EXACT member name (slugification is lossy, and a safety-rules file
    shared between two colliding crew names must be attributable — the same
    reason ``dm.json`` records the name). Does NOT create the directory;
    :func:`write_member_rules` does on demand.
    """
    validate_slug(slug)
    root = (data_home() / "trust" / RULES_DIR_NAME).resolve()
    target = (root / f"{slug}.json").resolve()
    if target.parent != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


class MemberRulesUnreadable(RuntimeError):
    """A rules file EXISTS but cannot be read or parsed.

    Deliberately distinct from "absent": the rules layer is the user's safety
    boundary, so an unreadable file must NOT silently read as "never set" —
    injecting identity + protocol + briefing with the user's rules quietly
    missing would be indistinguishable from a member the user never bounded.
    Callers degrade the WHOLE member section (or answer 500), never just the
    rules layer.
    """


def read_member_rules(slug: str, member: str) -> str:
    """Return *member*'s permanent rules text, or ``""`` when never set.

    Name-scoped, like every read of a lossy-slug file: the payload's recorded
    ``member`` must equal the requested name, so a colliding crew name reads
    the shared file as "no rules for me" instead of inheriting another
    member's safety boundary.

    Missing file reads as ``""`` (the normal state). An EXISTING file that
    cannot be read or parsed raises :class:`MemberRulesUnreadable` — see its
    docstring for why that must not degrade to ``""``.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    path = member_rules_path(slug)
    try:
        # Same read-side twin as read_dm_binding: write_member_rules replaces
        # this file atomically, and on Windows an AV/indexer handle on the
        # just-replaced file is a transient sharing violation, not a corrupt
        # rules file -- so it must not surface as MemberRulesUnreadable.
        raw = read_bytes_with_retry(path).decode("utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise MemberRulesUnreadable(
            f"member rules for {slug!r} exist at {path} but cannot be read; "
            f"the member will not run until the file is repaired — rewrite or "
            f"clear the rules via PUT /api/members/{slug}/rules"
        ) from exc
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise MemberRulesUnreadable(
            f"member rules for {slug!r} at {path} are malformed (not valid "
            f"JSON); the member will not run until the file is repaired — "
            f"rewrite or clear the rules via PUT /api/members/{slug}/rules"
        ) from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("member"), str)
        or not isinstance(data.get("rules"), str)
    ):
        raise MemberRulesUnreadable(
            f"member rules for {slug!r} at {path} are malformed (unexpected "
            f"shape); the member will not run until the file is repaired — "
            f"rewrite or clear the rules via PUT /api/members/{slug}/rules"
        )
    if data.get("member_id") != slug and data["member"] != member:
        # A colliding slug's file holds another exact name's rules; for THIS
        # member that is "never set", not an error.
        return ""
    return data["rules"].strip()


def write_member_rules(slug: str, *, member: str, text: str) -> None:
    """Persist a member's permanent rules atomically (human write path only).

    Records the EXACT member name in the payload so the name-scoped read can
    attribute the file across lossy-slug collisions. Raises rather than
    degrading: the caller is the dashboard rules endpoint — a human action —
    and the human must know their rules did not land. ``ValueError`` on an
    over-cap payload (refused loudly, never truncated: silently dropping the
    tail of a rules document would drop rules), and ``OSError`` propagates
    like :func:`write_dm_binding`.

    An empty/whitespace *text* deletes the rules file: "no rules" is the
    documented absent state, so clearing the editor clears the state instead
    of leaving a zero-byte file that reads back differently from "never set".

    fsync, like the DM binding: rules are the user's safety boundary for this
    member, so they must not silently vanish to a crash after the dashboard
    confirmed the save.
    """
    path = member_rules_path(slug)
    if len(text) > MEMBER_RULES_MAX_CHARS:
        raise ValueError(
            f"member rules for {slug!r} exceed {MEMBER_RULES_MAX_CHARS} chars ({len(text)})"
        )
    # JSON permits escaped lone surrogates ("\ud800"), so request-parsed text
    # can hold code points UTF-8 cannot encode. Reject them HERE, before any
    # state changes: letting the write raise mid-flight (atomic_write encodes
    # to UTF-8) turns a bad payload into a 500, and escaping them into the
    # file (ensure_ascii=True) only defers the same crash to prompt-encode
    # time — inside the member's turn instead of at the save.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"member rules for {slug!r} contain characters UTF-8 cannot encode"
        ) from exc
    if not text.strip():
        try:
            path.unlink()
        except FileNotFoundError:
            return
        # The unlink changed a directory ENTRY, which atomic_write's file-level
        # fsync never covers: without syncing the parent, a power-off after the
        # 200 can bring the cleared rules back. best_effort: the clear is
        # already committed — see fsync_dir's contract for why a raise here
        # would report completed work as failed.
        fsync_dir(path.parent, best_effort=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same defence-in-depth mode tightening as the DM binding: the trust
    # subtree is owner-only everywhere else, and a chmod failure must not
    # cost the save (the sensitive-path floor is the real fence).
    for _dir in (path.parent, path.parent.parent):
        try:
            platform_compat.restrict_dir_to_owner(_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", _dir, exc_info=True)
    payload = {
        "member": member,
        "member_id": slug if _stable_member_slug(slug, member) else "",
        "slug": slug,
        "rules": text,
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False), fsync=True)
    # atomic_write's fsync=True forces the file DATA; the rename that
    # publishes it — and, on first save, the just-created member-rules
    # directory's own entry in ITS parent — live in directory metadata a
    # power-off can still lose, returning the dashboard's confirmed save to
    # the old (or no) rules. Sync both levels, raising like the write itself:
    # the caller is the rules PUT endpoint and the human must know their
    # safety boundary did not land.
    fsync_dir(path.parent)
    fsync_dir(path.parent.parent)


def member_briefing_path(slug: str) -> Path:
    """Absolute path to one member's self-maintained briefing file.

    Inside :func:`member_dir` — deliberately agent-writable (see
    ``BRIEFING_FILE_NAME``). Does NOT create the directory; the member's own
    file tools do when it first writes its briefing.
    """
    return member_dir(slug) / BRIEFING_FILE_NAME


def member_briefing_supported() -> bool:
    """Whether this platform can read a member briefing race-free (layer 4).

    Two requirements, both open-time controls (no check-then-open race):
    a truthy ``O_NOFOLLOW`` to refuse a symlink at the final name, and the
    descriptor-relative pinned walk (:func:`~kiro_crew.pinned_fs
    .supports_pinned_walk`) to refuse an ancestor swapped for a link. Where
    either is missing (Windows), briefing reads FAIL CLOSED and the section
    builder renders the layer as unavailable rather than inviting the member
    into a futile write-then-never-injected loop.
    """
    return bool(getattr(os, "O_NOFOLLOW", 0)) and supports_pinned_walk()


def _briefing_pinned_target(slug: str) -> tuple[str, str]:
    """``(resolved members root / slug, BRIEFING_FILE_NAME)`` for the pinned open.

    The ROOT is resolved once (the pinned walk's "caller resolves once"
    contract) and the member's own directory component is appended LEXICALLY,
    never resolved: ``member_dir`` resolves ``members/<slug>`` too, and a
    ``members/<slug>`` swapped for a symlink to a peer's directory would then
    be followed *before* the walk begins -- the walk pins whatever the link
    points at and reads the peer's briefing as this member's. Left lexical,
    that component is opened with ``O_NOFOLLOW`` like every other and a link
    there is refused, which is the property the agent-writable
    ``members/<slug>/`` directory needs.
    """
    validate_slug(slug)
    root = members_root().resolve()
    return str(root / slug), BRIEFING_FILE_NAME


MEMBER_BRIEFING_TRUNCATION_MARKER = "\n[... briefing truncated at cap — prune it]"


def read_member_briefing_bounded(slug: str) -> tuple[str, float | None, bool]:
    """The bounded, UNCAPPED briefing buffer, its mtime, and whether the read hit its bound.

    The read half of :func:`read_member_briefing`, for a caller that must
    transform the text BEFORE cutting it at :data:`MEMBER_BRIEFING_MAX_CHARS`
    -- the dashboard's briefing endpoint redacts credentials, and a redaction
    run over already-capped text cannot match a token the cap split in two:
    the plaintext prefix would cross the boundary unmatched. The buffer is
    still bounded by the read itself (``(cap + 2) * 4`` bytes; see
    :func:`read_member_briefing`), so a token that straddles THAT edge is
    possible too, which is why :func:`cap_member_briefing` can drop the split
    tail. The third value is ``True`` when the file ran past the READ bound
    (the buffer lacks the file's tail); whether the text also runs past the
    character cap is :func:`cap_member_briefing`'s call, made on the text it
    is given -- after any transform -- not on the raw length. The mtime is
    from the same open as the text and ``None`` whenever the text reads as no
    briefing. Blocking file IO.
    """
    try:
        parent, name = _briefing_pinned_target(slug)
    except (MemberSlugError, OSError, RuntimeError):
        return "", None, False
    byte_cap = (MEMBER_BRIEFING_MAX_CHARS + 2) * 4
    if not member_briefing_supported():
        # Fail closed (see the docstring): without O_NOFOLLOW and the pinned
        # ancestor walk there is no race-free way to refuse a symlink on an
        # agent-writable path.
        return "", None, False
    try:
        fd = open_in_pinned_parent(
            parent,
            name,
            flags=os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            mode=0o600,
            what="member briefing",
        )
    except (PinnedPathRefusal, OSError):
        # Missing file/dir, a symlink refused anywhere on the pinned walk
        # (``members/<slug>`` itself included) or the leaf, or any unreadable
        # state — all read as "no briefing yet".
        return "", None, False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            # A FIFO, device or socket is never a briefing; reading one can
            # block or misbehave, so it reads as "no briefing yet".
            return "", None, False
        data = os.read(fd, byte_cap + 1)
    except OSError:
        return "", None, False
    finally:
        os.close(fd)
    mtime = float(st.st_mtime)
    truncated_bytes = len(data) > byte_cap
    if truncated_bytes:
        # The cut can split a multi-byte character; the tail is being
        # truncated anyway, so drop the partial character rather than failing
        # the whole read over it.
        text = data[:byte_cap].decode("utf-8", errors="ignore").strip()
    else:
        try:
            text = data.decode("utf-8").strip()
        except UnicodeError:
            return "", None, False
    return text, mtime, truncated_bytes


def cap_member_briefing(
    text: str, read_bounded: bool, *, drop_split_tail: bool = False
) -> tuple[str, bool]:
    """Cut ``text`` at :data:`MEMBER_BRIEFING_MAX_CHARS` with the visible marker.

    Returns the text and whether it was cut. ``read_bounded`` is the third
    value of :func:`read_member_briefing_bounded`: when the read hit its byte
    bound the buffer lacks the file's tail, so the marker is owed even when
    what remains fits the cap. Otherwise the cut happens only when the text
    GIVEN runs past the cap -- measured here, on the text as it is now, so a
    caller that redacted the buffer first (the dashboard endpoint) is judged
    on the redacted length: a briefing that only overflowed before its
    placeholders shrank it is shown whole, with no marker and no word lost.

    With ``drop_split_tail`` the cut also removes the trailing run of
    non-whitespace characters, so the shown text never ends in the FIRST HALF
    of a word the cap split: every credential the redaction chain knows is
    such a run, and a token that straddles the cut (either the character cap
    or the bounded read's own edge) would otherwise cross the wire as an
    unmatched plaintext prefix. At most one word of the shown tail is lost to
    it; a briefing with no whitespace at all in its first cap's worth of
    characters reads as the marker alone, which is the fail-closed answer.
    The prompt path keeps the plain cut: the member reads its own file, and
    the marker is what tells it to prune.
    """
    text = text.strip()
    if not read_bounded and len(text) <= MEMBER_BRIEFING_MAX_CHARS:
        return text, False
    head = text[:MEMBER_BRIEFING_MAX_CHARS]
    if drop_split_tail:
        stripped = head.rstrip()
        cut = len(stripped)
        while cut > 0 and not stripped[cut - 1].isspace():
            cut -= 1
        head = stripped[:cut].rstrip()
    return head + MEMBER_BRIEFING_TRUNCATION_MARKER, True


def read_member_briefing(slug: str) -> str:
    """Return a member's briefing text capped for injection, or ``""``.

    Total by contract: every failure reads as "no briefing yet", which is the
    normal state of a fresh member. Content past
    :data:`MEMBER_BRIEFING_MAX_CHARS` is cut at the cap with a visible marker,
    so the member SEES that its briefing overflowed (and can prune it) rather
    than silently losing the tail.

    The file is AGENT-WRITTEN, so two properties are enforced at the open,
    not after it:

    * **No symlink following anywhere on the path, no non-regular files, no
      blocking open.** The gateway reads this file with its own privileges
      while building context, so a briefing replaced by a symlink would pull
      any gateway-readable file — including the keystone-gated ``trust/``
      payloads the agent's tools cannot reach — into the prompt, and a
      briefing replaced by a FIFO would make a plain ``open`` block forever,
      hanging the member's turn and exhausting the embed workers. The leaf is
      opened through :func:`~kiro_crew.pinned_fs.open_in_pinned_parent`, which
      walks the ancestor chain one descriptor-relative ``openat`` at a time,
      each carrying ``O_NOFOLLOW`` — an ``O_NOFOLLOW`` on the final component
      alone is not enough, because the member's own directory
      (``members/<slug>/``) is agent-writable too, and swapping IT for a link
      redirects the whole traversal while the leaf open still finds an
      ordinary file. That is why the walk starts from the resolved members
      ROOT with ``<slug>`` appended lexically (:func:`_briefing_pinned_target`)
      rather than from :func:`member_dir`, whose own ``resolve()`` would follow
      such a link before the walk could refuse it.
      ``O_NONBLOCK`` makes a FIFO open return immediately instead of waiting
      for a writer (both at open time — no check-then-open race); ``fstat``
      then rejects anything that is not a regular file. Where the pinned walk
      or ``O_NOFOLLOW`` is unavailable (Windows —
      :func:`member_briefing_supported`) the read FAILS CLOSED to "no
      briefing": a check-then-open probe is exactly the TOCTOU an
      agent-writable path invites (swap a symlink in after the check), and
      the repo's posture on hosts lacking an OS-level control is to refuse,
      not to run the racy approximation.
    * **Bounded read.** At most ``(cap + 2) * 4`` bytes are read (4 = max
      UTF-8 bytes per char), so an arbitrarily large briefing costs a bounded
      allocation, never gateway memory.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    text, _mtime, read_bounded = read_member_briefing_bounded(slug)
    return cap_member_briefing(text, read_bounded)[0]


def record_activity(
    member: str,
    session_key: str,
    memory_mode: str,
    *,
    project: str = "",
    via: str = "",
    dedupe_session: bool = False,
) -> bool:
    """Append one pointer entry to a member's activity log.

    Takes the member's NAME and derives the slug internally, so callers need no
    try/except: every failure path — a name that yields no usable slug, a
    read-only home, a torn write — is handled here and reported ``False``. This
    is best-effort by contract; a logging failure must never break the turn that
    triggered it, and one call site (``mcp_core``) has no logger of its own.

    ``memory_mode`` is REQUIRED and positional, not an opt-in keyword: it gates
    whether the session may be recorded at all, and a caller that simply forgot
    it would durably log a private session. It is matched against an allowlist
    (:data:`_TRACEABLE_MEMORY_MODES`), so an absent, empty or unrecognized mode
    skips the write rather than passing through.

    ``dedupe_session`` suppresses a repeat entry for a member/session pair. The
    chat site needs it because its ``is_new`` flag tracks the PROVIDER session,
    not the conversation: a dead provider cold-starts the same conversation with
    ``is_new=True`` again, which would append the same pointer twice and inflate
    the counts this log exists to feed. Routing decisions are NOT deduped — each
    ``select_crew`` bind is a distinct event even for one session.

    ``via`` records HOW the member was chosen, because the two call sites mean
    different things and a mixed log cannot be read apart afterwards:

    * ``"chat"`` — the human picked this member for the session.
    * ``"select_crew"`` — the orchestrator judged this member fits the task.

    A ``select_crew`` entry records the routing *decision*, not an execution:
    binding a crew does not oblige the model to delegate to it. That is the
    useful signal for trigger generation (what the router believes belongs to
    whom), but it means these counts are intent, not runs.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    if not member or not session_key:
        return False
    if memory_mode.strip().lower() not in _TRACEABLE_MEMORY_MODES:
        return False
    # The exact member name travels IN the record rather than being implied by
    # the directory. Slugification is lossy, so two distinct member names can
    # map to one slug ("Review_Agent" and "review-agent") and share a log;
    # carrying the name keeps per-member attribution recoverable in that case,
    # which the frequency signal downstream depends on.
    #
    # The session pointer is named for what it MEANS, not just what it holds.
    # A routing decision is recorded in the session that made it — the parent —
    # while the member itself runs in a different (sub-agent) session that does
    # not exist yet at bind time. Filing both under one `session` key would let
    # a consumer counting "sessions this member took part in" count a session
    # the member never ran in. Distinct keys make that misread impossible
    # instead of leaving it to the consumer to notice `via`.
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "member": member,
    }
    if via == "select_crew":
        entry["decided_in"] = session_key
    else:
        entry["session"] = session_key
    if project:
        entry["project"] = project
    if via:
        entry["via"] = via
    # Imported BEFORE the try, not beside the sibling imports inside it: the
    # handler below reads this name, so an import that failed inside the block it
    # guards would raise NameError from the handler itself.
    from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED

    try:
        # main's resolver, not the bare fold: a member may carry an explicit
        # `member_id` in config, and member_slug honours it before falling back to
        # slug_for_name. The log has to be keyed by the same id the rest of the
        # roster uses, or a member with an explicit id reads an empty projection.
        slug = member_slug(member)
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        # ENSURE FIRST, then dedupe. ``ensure`` is what folds a pre-upgrade
        # ``activity.jsonl`` into the log, so a scan running before it reads a
        # log the legacy rows have not reached yet -- and the first
        # post-upgrade call for a session already recorded in that file would
        # dedupe against nothing and append a permanent duplicate. Both steps
        # are idempotent, so paying ensure before a scan that may return early
        # costs a caller nothing.
        svc.ensure(slug, member)
        if dedupe_session:
            # Check the member's own ACTIVITY_RECORD events for this session
            # pair instead of scanning a file. Matched on BOTH fields: a
            # colliding slug means one log can hold two members, so session
            # alone would suppress the wrong entry. Only participation entries
            # carry `session`, which is also the only kind deduped — routing
            # decisions are distinct events.
            #
            # The scan is UNBOUNDED, and a bound is not an optimisation here: the
            # log is already materialised in memory by the read below, so a limit
            # only truncates a filter over a list that was loaded either way. A
            # 200-event cap therefore bought no I/O and did buy a miss — a
            # session resumed after 200 later events deduped against nothing and
            # wrote a second participation record, inflating the activity
            # projection it feeds.
            recent = svc.history(slug, before=None, limit=None)
            for ev in recent:
                if ev.get("type") != ACTIVITY_RECORD:
                    continue
                rec = ev.get("data") or {}
                if rec.get("session") == session_key and rec.get("member") == member:
                    return False
        svc.append(slug, ACTIVITY_RECORD, entry)
        return True
    except Exception as exc:
        # An ``already_owned`` refusal is a LOSS, and it is reported rather than
        # hidden. The store's write lease is taken non-blocking, so a second
        # process appending at the same instant is refused outright instead of
        # being serialized behind the per-append lock -- and this entry is then
        # never written. The two writers are ordinary rather than pathological:
        # ``kirocrew-core`` runs as its own subprocess and records activity
        # through this function, which :meth:`MemberLog.refresh_if_changed`
        # already names as a second writer on the read side. ``crew_log.lease``
        # states the caller's duty for this code -- it reports the loss rather
        # than retrying it -- and a debug line does not discharge that: it leaves
        # a dropped routing decision indistinguishable from a member that was
        # never routed, which is the one thing this record exists to tell apart.
        if getattr(exc, "code", "") == CODE_ALREADY_OWNED:
            logger.warning(
                "member activity entry DROPPED for %r (session %r, via %r): another "
                "process owns writes to this member's log, so the entry is lost and "
                "is not retried: %s",
                member,
                session_key,
                via,
                exc,
            )
        else:
            logger.debug("member activity log write failed for %r", member, exc_info=True)
        return False


def read_activity(slug: str, limit: int = 0) -> list[dict]:
    """Return a member's activity entries, oldest first.

    The degrading view of :func:`_read_activity_checked`: it drops the
    completeness flag. Backed by the per-member append-only event log — the
    entries are the record dicts carried by ``ACTIVITY_RECORD`` events, in
    append order (oldest first). ``limit`` > 0 returns only the most recent N.
    Every failure reads as an empty list, so a caller that only displays or
    counts entries needs no try/except.
    """
    rows, _complete = _read_activity_checked(slug, limit)
    return rows


def _read_activity_checked(slug: str, limit: int = 0) -> tuple[list[dict], bool]:
    """Return a member's activity entries oldest first, and whether they are ALL of them.

    Backed by the per-member append-only event log: reads ``ACTIVITY_RECORD``
    events (newest first from the service), unwraps each to the record dict it
    carries, and reverses to oldest-first — the order the former file-backed
    reader returned. ``limit`` > 0 returns the most recent N. A bad slug or any
    read failure yields ``([], True)``.

    The second element is retained for callers that fail closed on a partial
    read of the agent-writable file. The event log commits one line
    per append under fsync and the reader repairs a torn trailing line at load,
    so a partial-read completeness gap does not arise here — the flag is
    always ``True`` on a successful read. It stays in the signature so the
    ``record_activity`` dedupe probe and its tests keep their shape.
    """
    try:
        validate_slug(slug)
    except MemberSlugError:
        return [], True
    try:
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        events = svc.history(slug, before=None, limit=None)
        records = [
            ev.get("data") or {} for ev in reversed(events) if ev.get("type") == ACTIVITY_RECORD
        ]
        rows = [r for r in records if isinstance(r, dict)]
    except Exception:
        logger.debug("member activity log read failed for %r", slug, exc_info=True)
        return [], True
    return (rows[-limit:] if limit > 0 else rows), True
