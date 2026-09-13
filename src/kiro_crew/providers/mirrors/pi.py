"""pi's agent-config mirror (``pi-acp``).

The wire face only. pi-acp reads no Crew agent file -- it boots one pi session
per ACP ``sessionId`` in-process -- so the ``session/new`` ``mcpServers`` array
is the ONLY channel Crew has onto a pi session. An empty array is therefore not
a neutral default but the whole defect ``providers/mirrors/README.md`` exists
for: pi is in ``BASELINE_SELECTABLE_BACKENDS``, so a public build would serve a
harness with no ``spawn_run``, no ``cron_add`` and no ``send_message`` --
working in every visible respect, with every Crew tool silently absent.

The translation is claude's (:func:`kiro_crew.acp.session_mcp.session_mcp_servers`):
the agent spec is the single source of truth, the ``tools`` allowlist decides
which servers enter the array, and the registry ceiling and control-plane
re-derivation apply unchanged. What is pi-specific is what this module adds on
top, and each rule was READ off the adapter's own source (``pi-acp/src``) rather
than inferred:

1. **No transport is dropped.** Unlike codex-acp -- which answers ``-32600`` for
   the WHOLE ``session/new`` on one ``sse`` element -- pi-acp accepts ``stdio``,
   ``http`` and ``sse`` (its ``initialize`` advertises all three) and SKIPS an
   unknown element shape with a log while the request succeeds. There is no
   fatal shape to filter for, so this mirror forwards the translated array whole.
   The adapter's tolerance is the reason: ``mcp-bridge.ts connectMcpServer``
   returns ``null`` for an unknown shape instead of failing ``session/new``.
2. **Names are sanitized the way the adapter sanitizes them**
   (:func:`pi_name`, byte-identical to ``mcp-bridge.ts sanitizeName``:
   ``[^a-zA-Z0-9_-]`` becomes ``_``, capped at 64 chars). The adapter mounts
   each server as ``mcp__<server>__<tool>`` pi tools, and that title is the only
   channel naming the tool on a permission request -- pi emits no ``_meta.kiro``
   -- so the denied-tools set below is spelled in registered (sanitized) names
   and the client matches permission titles against it exactly.
3. **Crew's own control plane carries the session identity on the element.**
   pi-acp spawns each MCP stdio child with the adapter's own environment PLUS
   the element's ``env`` (``mcp-bridge.ts`` merges both), so unlike codex's
   ``env_clear`` the child would inherit an adapter-wide key -- but the adapter
   is spawned without one, and the key is per-session. Riding it on the element
   keeps the credential on the two entries Crew derived itself
   (``kirocrew-core``, ``kirocrew-cron``) and off every hand-editable spec line,
   exactly as codex does. Without it the out-of-band session-directive path
   (``dashboard/directive_queue``) has nothing to claim against.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from typing import Any, Mapping

from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS, session_mcp_projection
from kiro_crew.acp_backends import ACP_BACKEND_PI
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
    SessionProjection,
)

logger = logging.getLogger(__name__)

_D = Disposition

#: Maximum tool-name length the adapter registers. Longer names are cut, so a
#: name past this length would register under a spelling nothing here predicts.
_PI_NAME_MAX = 64


def pi_name(name: str) -> str:
    """*name* spelled the way pi-acp will register it.

    Byte-identical to the adapter's ``sanitizeName`` (``pi-acp/src/mcp-bridge.ts``):
    every character outside ``[a-zA-Z0-9_-]`` becomes ``_``, and the result is
    capped at 64 chars. Underscores SURVIVE the fold, so ``mcp__my_srv__tool``
    is ambiguous as a parse -- the denied-tools match below therefore compares
    whole titles against the denied set rather than splitting on ``__``.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:_PI_NAME_MAX] or "unnamed"


def pi_tool_title(server: str, tool: str) -> str:
    """The ACP title pi-acp reports for a bridged tool call.

    The adapter mounts each remote tool as ``mcp__<server>__<tool>`` (both halves
    sanitized as :func:`pi_name`) and reports that same string as the
    ``tool_call`` title -- the only channel naming the tool, since pi emits no
    ``_meta.kiro``. The client refuses a spec-disabled control-plane tool by
    exact match against this spelling (``AcpClient._deny_spec_disabled_tool``).
    """
    return f"mcp__{pi_name(server)}__{pi_name(tool)}"


def _pi_identity_env(session_key: str, channel_id: str) -> dict[str, str]:
    """The env Crew's own control plane needs, resolved for this session.

    Same values codex carries (:func:`codex._identity_env`): the managed home,
    the session key and channel, and the serving port. Resolved live rather than
    read from the spec, exactly as ``managed_mcp_spec_entry`` resolves the
    command. Fail-soft throughout: a config-plane failure must not fail a spawn.
    """
    # circular import: agent's module graph is heavy (it imports config), and
    # port_resolution reaches config.loader, whose provider-backend path imports
    # members. Both resolved at call time, as session_mcp.py resolves them.
    from kiro_crew.agent import _managed_mcp_env
    from kiro_crew.port_resolution import resolve_serving_port

    env: dict[str, str] = {}
    try:
        env.update(_managed_mcp_env())
    except Exception:  # pragma: no cover - defensive; the helper is fail-soft
        logger.warning("pi session MCP: could not resolve the managed home", exc_info=True)
    if session_key:
        env["KIROCREW_SESSION_KEY"] = session_key
    if channel_id:
        env["KIROCREW_CHANNEL_ID"] = channel_id
    try:
        env["KIROCREW_BOUND_PORT"] = str(resolve_serving_port())
    except Exception:  # pragma: no cover - defensive
        logger.warning("pi session MCP: could not resolve the serving port", exc_info=True)
    return env


def _with_env(element: dict[str, Any], extra: Mapping[str, str]) -> dict[str, Any]:
    """*element* with *extra* merged into its ACP array-of-pairs ``env``.

    Later wins, so a value resolved here replaces a stale one the entry carried --
    the same precedence ``managed_mcp_spec_entry`` applies to the command. The
    adapter accepts ``env`` as an array or a record; the translation emits the
    array form, so the merge keeps that form rather than converting it.
    """
    pairs: list[dict[str, str]] = [
        p for p in element.get("env") or [] if isinstance(p, dict) and p.get("name") not in extra
    ]
    pairs.extend({"name": k, "value": v} for k, v in extra.items())
    out = dict(element)
    out["env"] = pairs
    return out


def _identity_bound_crew_servers() -> frozenset[str]:
    """Crew's own managed servers that mounting would leave UNUSABLE on pi.

    Every managed server minus the control plane. The control plane is the part
    :func:`pi_elements` rebuilds with this session's identity; everything else
    reaches pi from the agent spec unreplaced, so it would come up bound to no
    session and answer ``not_bound`` to every call. That present-but-unusable
    shape is the defect this whole folder exists to kill, so those names are
    withheld and the absence is logged.

    DERIVED, not enumerated -- see ``codex._identity_bound_crew_servers`` for why
    a hand-copy of this subtraction drifts in the bad direction. Read from
    :mod:`kiro_crew.mcp_cleanup`, which a ratchet test already pins equal to
    ``agent._MANAGED_MCP_SERVERS``.
    """
    return frozenset(KIROCREW_BIN_MCP_SERVERS) - frozenset(CONTROL_PLANE_SERVERS)


def pi_withheld_servers(restricted: frozenset[str]) -> frozenset[str]:
    """Every server name this transport must not mount for a session.

    ONE owner for the question, because two consumers ask it: the projection, and
    the pooled-broker append. A stub the shared MCP gateway wraps carries the SAME
    name as the entry it rewrites, so a name withheld from the projection and then
    re-added as a stub is un-withheld -- and the stub is the UNRESTRICTED server,
    which is the worse of the two.

    Two reasons a name lands here, and they share a shape: this transport cannot
    deliver the thing that makes the server correct.

    * its spec narrows it per tool and pi has no deny channel
      (:func:`~kiro_crew.acp.session_mcp.session_mcp_restricted_servers`);
    * it is one of Crew's own identity-bound servers, which cannot be handed this
      session's credential on an element the spec describes
      (:func:`_identity_bound_crew_servers`).

    ``restricted`` comes from the caller's own parse: the projection derives the
    translation and this set from ONE read of the spec, so a spec gaining
    ``disabledTools`` between two reads cannot yield a withhold set from the old
    bytes applied to a translation of the new ones.
    """
    return frozenset(restricted) | _identity_bound_crew_servers()


def pi_elements(
    elements: list[dict[str, Any]],
    *,
    session_key: str = "",
    channel_id: str = "",
) -> list[dict[str, Any]]:
    """Apply pi's spelling and identity rules to a translated array.

    There is deliberately no transport filter here: pi-acp accepts ``stdio``,
    ``http`` and ``sse`` and skips an unknown shape with a log while
    ``session/new`` succeeds, so unlike codex there is no fatal shape whose
    presence costs the session every other server.

    **The identity env goes on Crew's OWN CONTROL PLANE and nowhere else.**
    ``KIROCREW_SESSION_KEY`` authenticates a session-directive claim, so it may
    ride only an element Crew itself derived. ``kirocrew-core`` and
    ``kirocrew-cron`` are exactly that: the shared translation REPLACES them
    from ``managed_mcp_spec_entry``, so no part of the hand-editable spec
    reaches the child. Their names are matched before sanitizing. Everything
    else in the array gets NO Crew identity: an element the spec describes is
    one whose command, args and env the spec chose, so handing it this session's
    credential would let a hand-edited line drive the session it was mounted
    into.

    A sanitized name is still claimed only once, first writer keeping it: two
    spec entries CAN fold together, the adapter registers by name, and a session
    whose roster does not match what registered is worth a warning either way.
    """
    identity = _pi_identity_env(session_key, channel_id) if session_key or channel_id else {}
    claimed: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        raw_name = str(element.get("name") or "")
        is_control_plane = raw_name in CONTROL_PLANE_SERVERS
        sanitized = pi_name(raw_name)
        element = dict(element)
        element["name"] = sanitized
        if identity and is_control_plane:
            element = _with_env(element, identity)
        if sanitized in claimed:
            logger.warning(
                "pi session MCP: dropping a second server that sanitizes to the name %r "
                "(raw name %r) -- the adapter registers by name, so keeping both would let one "
                "silently take the other's slot",
                sanitized,
                raw_name,
            )
            continue
        order.append(sanitized)
        claimed[sanitized] = element
    return [claimed[name] for name in order]


def pi_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
) -> SessionProjection:
    """The whole pi array -- spec translation AND pooled stubs -- plus the deny set.

    The mirror's :meth:`~PiMirror.session_projection`, as a function so it can be
    called and tested without the class. ``denied_tools`` is the client obligation
    :class:`~kiro_crew.providers.mirrors.base.SessionProjection` describes: the
    ``(server, tool)`` pairs this session's spec switched off, which the client
    refuses when pi asks permission for them
    (``AcpClient._deny_spec_disabled_tool``). Server names are pi-sanitized
    (:func:`pi_name`), because the title pi reports a call under is the REGISTERED
    spelling, and comparing a raw name against a sanitized one would silently
    never match.

    Stubs are appended as given rather than run through :func:`pi_elements`: they
    are gateway-authored, their env is the broker's own, and none of them is Crew's
    control plane, so there is no identity to add. They ARE held to the spec's
    ``tools`` allowlist, from the same parse that filtered the translated half.

    ``denied_tools`` is derived on the same parse as the array, so it cannot name a
    tool on a spec revision the array never saw.

    Blocking (parses the agent spec once), so callers run it off the event loop.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    withheld = pi_withheld_servers(projection.restricted)
    kept: list[dict[str, Any]] = []
    for element in projection.servers:
        name = element.get("name")
        if name in withheld:
            logger.warning(
                "pi session MCP: withholding server %r -- this transport cannot deliver "
                "what makes it correct (a per-tool restriction it has no deny channel for, "
                "or the session identity a Crew server binds to), and a mounted server that "
                "cannot work is the defect this projection exists to remove",
                name,
            )
            continue
        kept.append(element)
    out: list[dict[str, Any]] = pi_elements(kept, session_key=session_key, channel_id=channel_id)
    for stub in stub_elements:
        if not isinstance(stub, Mapping):
            continue
        name = stub.get("name")
        if name in withheld:
            logger.warning(
                "pi session MCP: withholding pooled stub %r -- the projection withheld "
                "the server it wraps, and a stub re-adds it unrestricted",
                name,
            )
            continue
        if not projection.allowlist.grants(str(name)):
            logger.info(
                "pi session MCP: not mounting pooled stub %r -- the agent spec's `tools` "
                "does not reference it, and the allowlist that filtered the translated "
                "half applies to a stub of the same name",
                name,
            )
            continue
        out.append(dict(stub))
    denied = frozenset(
        (pi_name(server), pi_name(tool)) for server, tool in projection.disabled_tools
    )
    return SessionProjection(params={"mcpServers": out}, denied_tools=denied)


class PiMirror(AgentConfigMirror):
    """Projects the agent spec onto pi-acp."""

    backend = ACP_BACKEND_PI

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.TRANSLATED,
                "the session/new mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers and then narrowed by this "
                "module. Unlike codex's adapter -- which answers -32600 for the "
                "WHOLE session/new on one sse element -- pi-acp accepts stdio, "
                "http and sse and SKIPS an unknown shape with a log while the "
                "request succeeds (mcp-bridge.ts), so there is no fatal shape to "
                "filter for and the translated array is forwarded whole. Two "
                "rules ARE pi's own. (1) A third-party server whose spec narrows "
                "it per tool is OMITTED, not forwarded un-narrowed: pi-acp has "
                "no per-tool deny slot on its session/new element, so "
                "session_mcp_restricted_servers names those servers and the "
                "array omits them -- an availability cost is the honest price "
                "of a deny channel this transport lacks. (2) Crew's own control "
                "plane carries KIROCREW_SESSION_KEY on the element, because the "
                "session identity must ride something Crew derived itself; that "
                "credential rides ONLY kirocrew-core and kirocrew-cron, the two "
                "entries the translation replaces from the managed source, so "
                "no part of a hand-editable spec reaches a child holding it. "
                "Every OTHER managed Crew server is withheld rather than "
                "mounted credential-less, and that set is DERIVED as the managed "
                "servers minus the control plane. The array and the withhold "
                "set come from ONE parse of the spec (session_mcp_projection). "
                "Unlike claude this array is NOT conditional on Crew owning a "
                "permission file: pi's routing is `Routing.SESSION_CONFIG`, the "
                "one mechanism in tool_gate.ENFORCED_ROUTINGS, so a session that "
                "cannot arm mode=read-only is refused before its first prompt "
                "rather than run unasked",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "`tools` is not sent; it is applied during translation as the "
                "allowlist deciding which servers enter the array, so a server the "
                "spec declares but never references is not mounted here either -- "
                "kiro-cli parity. It carries the same residual claude has: an "
                "`@server/tool` grant narrows to one tool on kiro-cli but mounts "
                "the whole server here, because the tool set is not knowable "
                "without connecting",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "the restriction is honoured by WITHHOLDING THE SERVER: pi-acp's "
                "session/new element has no per-tool deny slot, so "
                "acp.session_mcp.session_mcp_restricted_servers names those "
                "servers and the array omits them. Crew's own CONTROL PLANE is "
                "the one exception, honoured differently rather than dropped: "
                "withholding kirocrew-core would leave the session unable to "
                "report back at all, so it stays mounted and the client refuses "
                "a call to a switched-off tool when pi asks permission for it "
                "(AcpClient._deny_spec_disabled_tool, matched by EXACT title: "
                "pi reports bridged calls as `mcp__<server>__<tool>` with both "
                "halves sanitized, and the denied set carries that same "
                "registered spelling, so the match is equality, never a parse). "
                "`disabled` needs nothing here -- build_agent_config strips a "
                "disabled server's @alias from `tools`, and the allowlist mounts "
                "nothing `tools` does not name",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "there is no harness-side allowlist to project: pi-acp's "
                "permission options are per-call (`once`/`always`/`reject`), and "
                "a pre-approval carried into the harness would skip Crew's "
                "permission gate, its governance ceiling and its SEL audit. Every "
                "MCP call must reach the host gate",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "not through this mirror: pi is in "
                "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION, so the resolved model is "
                "pushed with session/set_config_option('model', ...) after "
                "session/new. Named here rather than left out so a reader does not "
                "read this mirror's silence as the model being dropped",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the direction is reversed on this backend: pi-acp advertises "
                "its own model list as a session/new configOptions select, and "
                "that list is the ONLY source of ids set_config_option accepts -- "
                "the static registry has no pi provider, and kiro's catalog names "
                "models pi refuses. So Crew CAPTURES the advertised set into the "
                "`pi` registry namespace "
                "(ACP_BACKENDS_ADVERTISED_MODEL_SELECTION) instead of sending one. "
                "Projecting the spec's availableModels here would offer ids that "
                "kill the session",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "pi-acp has a `mode` selector and Crew writes it -- but it "
                "writes the FIXED value tool_gate demands (mode=read-only), not "
                "the mode the spec asked for. Honouring a spec-requested mode "
                "would let an agent file widen a pi session past the one boundary "
                "that makes this harness offerable, and the assertion is per "
                "session rather than seeded to a file precisely so nothing can "
                "inherit a looser one. A deliberate override, not a dropped "
                "setting",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "not a mirror concern on any backend: the prompt reaches every "
                "harness as ordinary prompt text in the [AGENT SYSTEM PROMPT] "
                "context block, which is backend-agnostic and already works",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "same as PROMPT -- steering files are injected as context text, not "
                "projected into a backend's config",
            ),
            Concern.HOOKS: Ruling(
                _D.NO_CHANNEL,
                "the adapter carries no hooks field on its session/new element "
                "set: pi-acp boots one pi session per ACP sessionId with only "
                "its permission-gate extension, so a user's per-agent hooks block "
                "reaches kiro-cli and no other backend, exactly the gap claude "
                "and codex record. Crew's OWN hooks (hooks.py, fired on ACP tool "
                "events) are unaffected and work on this backend already; this "
                "gap is only the spec block",
                channel="a hooks face on the pi-acp session/new contract (a Crew-"
                "owned hooks projection the adapter mounts beside its gate), or "
                "a Crew-owned PI_AGENT_DIR overlay under create-or-decline -- Crew "
                "writes no pi file today, which is why this needs a decision "
                "rather than a writer",
            ),
        }

    def session_params(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        work_dir: object = None,
        session_key: str = "",
        channel_id: str = "",
        **kwargs: object,
    ) -> dict[str, object]:
        """The wire face: the ``mcpServers`` array for this pi session.

        ``permission_surface_owned`` is accepted and IGNORED (it arrives in
        ``kwargs``), which is the documented behaviour for a mirror outside
        claude's class: pi has no permission file in play, its routing is
        asserted per session over ``session/set_config_option`` and is the one
        mechanism in ``tool_gate.ENFORCED_ROUTINGS``, so a session that cannot
        arm ``mode=read-only`` is REFUSED rather than run. Failing closed on the
        flag here would withhold every Crew tool from every pi session on the
        strength of a condition that does not describe this backend.

        Blocking -- it reads the agent spec. The caller warms this on the pi
        spawn path and serves the shared ``session/new`` call site from that cache
        (H13). The client calls :meth:`session_projection`, which also carries the
        deny set derived on the same parse; this method IS that projection's
        ``params``, so the two faces cannot drift.
        """
        stubs = kwargs.get("stub_elements") or ()
        return self.session_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stubs if isinstance(stubs, (list, tuple)) else (),
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
        ).params

    def session_projection(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        stub_elements: Collection[Mapping[str, Any]] = (),
        work_dir: object = None,
        session_key: str = "",
        channel_id: str = "",
        **kwargs: object,
    ) -> SessionProjection:
        """The structured face: :func:`pi_projection`, with ``kwargs`` ignored as
        :meth:`session_params` documents (``permission_surface_owned`` arrives there)."""
        del kwargs
        return pi_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
        )
