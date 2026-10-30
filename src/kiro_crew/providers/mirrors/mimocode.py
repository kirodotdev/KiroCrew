"""MiMo Code's agent-config mirror (``mimo acp``).

The wire face only: the ``session/new`` ``mcpServers`` array is this backend's whole
MCP channel. Each face this projection depends on was measured on 0.1.15 or read off
its source:

* The element ``acp/session_mcp.py::acp_server_element`` emits --
  ``{"name", "command", "args", "env": [{"name","value"}]}`` -- is ACCEPTED: the
  child is spawned, answers ``initialize`` and ``tools/list``, and a ``tools/call``
  on it completes after a permission request
  (``test/fixtures/acp_frames/mimocode/mcp-stdio-mount-live.jsonl``). The tool comes back
  under the fused ``<server>_<tool>`` title.
* ``type: "stdio"`` is REMOVED by :func:`_mimocode_local_element`. MiMo Code's ACP
  layer sends an element down its remote branch on the mere PRESENCE of ``type``
  (``acp/agent.ts:1144-1163`` on 0.1.15), and that branch reads ``headers``, which a
  stdio element does not have. ACP v1 makes stdio the untagged member, so the tag
  carries no information and dropping it removes the dependence.
* The array is keyed by server name before MiMo Code mounts it, the later element
  winning (``acp/agent.ts:1144``), so :func:`place_mimocode_array` builds it the
  same way and reports what is mounted.
* A switched-off tool is a ``deny`` rule under the fused id, placed after the seed's
  ``"*": "ask"`` in ``MIMOCODE_CONFIG_CONTENT``: the denied tool was absent from the
  model's tool list while its sibling stayed and asked, and ``mimo debug config``
  printed the rule in force, in order, unmasked.

Crew writes no MiMo Code config of its own beyond the permission routing
(``acp.harness.mimocode._mimocode_routing_config``: the ``tool_gate`` setting plus
one ``deny`` per switched-off MCP tool). MCP servers do not travel there:
``MIMOCODE_CONFIG_CONTENT`` MERGES with project and user config, while the array is
mounted apart from it, one ``mcp.add`` per element on every ``session/new``, load,
fork and resume (``acp/agent.ts:1165-1180``), so a server in both would have two
sources. The array is the one channel.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any, Mapping

from kiro_crew.acp.harness_tool_names import MAX_HARNESS_TOOL_NAME_LEN, mimocode_spelling
from kiro_crew.acp.mcp_session_report import sanitize_sink_text
from kiro_crew.acp.session_mcp import (
    CONTROL_PLANE_SERVERS,
    SessionMcpProjection,
    session_mcp_projection,
)
from kiro_crew.acp_backends import ACP_BACKEND_MIMOCODE
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
    SessionProjection,
)
from kiro_crew.providers.mirrors.identity import (
    control_plane_identity_env,
    with_env,
    withheld_servers,
)

logger = logging.getLogger(__name__)

_D = Disposition


def _logged(name: object) -> str:
    """A server name from the agent spec or a stub, redacted and bounded for a log line."""
    return sanitize_sink_text(str(name), MAX_HARNESS_TOOL_NAME_LEN)


def _mimocode_local_element(element: dict[str, Any]) -> dict[str, Any]:
    """*element* in the shape MiMo Code's ACP layer reads as a LOCAL server.

    MiMo Code 0.1.15 picks the kind by key presence alone: an element WITH a
    ``type`` key becomes ``{type: "remote", url, headers}`` and one without it
    ``{type: "local", command, environment}`` (``acp/agent.ts:1144-1163``). Crew's
    stdio elements arrive tagged ``type: "stdio"`` -- the translated half from
    ``acp_server_element``, the broker stubs from ``mcp_gateway.session_servers``
    -- and a tagged one would be read as remote and fail on its absent ``headers``.
    So the stdio tag is dropped here, from both halves; an ``http`` or ``sse``
    element keeps its tag, which is what sends it down the remote branch. Returns
    a copy when it changes anything.
    """
    if element.get("type") != "stdio":
        return element
    return {key: value for key, value in element.items() if key != "type"}


def mimocode_elements(
    elements: list[dict[str, Any]],
    *,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> list[dict[str, Any]]:
    """Apply this harness's identity rule to an already-translated array.

    **The identity env goes on Crew's OWN CONTROL PLANE only.**
    ``KIROCREW_SESSION_KEY`` authenticates a session-directive claim, so it rides
    only elements Crew derived: ``kirocrew-core`` and ``kirocrew-cron``, which the
    shared translation REPLACES from ``managed_mcp_spec_entry``. A spec-described
    element's command, args and env are the spec's, so this session's credential
    would let a hand-edited line drive the session. Unlike codex-rs (which
    ``env_clear()``s children), this harness's children inherit the ambient env minus
    its own two credential variables (``util/credential-env.ts``), so a dropped rule
    would hand them the gateway's key, the WRONG one, rather than none.

    **The stdio ``type`` tag is REMOVED:** the ACP-to-native mapping branches on its
    PRESENCE (``acp/agent.ts:1146-1157``), so a tagged stdio element would be routed
    remote and throw on its absent ``headers``; ``http`` and ``sse`` keep theirs.

    **No name folding:** the harness registers the name as given
    (``sdk.mcp.add({directory, name, config})``, ``acp/agent.ts:1169-1170``); only
    the TOOL id it shows the model is sanitised, which this projection does not
    compare against. Pure and in-memory.
    """
    identity = (
        control_plane_identity_env(
            session_key, channel_id, label="mimocode", session_token=session_token
        )
        if session_key or channel_id or session_token
        else {}
    )
    out: list[dict[str, Any]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        # The shared translator emits the tag for claude, which needs it; this
        # harness discards it, and depending on that discarding is what the
        # docstring above declines to do.
        element = _mimocode_local_element(dict(element))
        if identity and str(element.get("name") or "") in CONTROL_PLANE_SERVERS:
            element = with_env(element, identity)
        out.append(element)
    return out


#: MiMo Code's two permission groups, ``EDIT_TOOLS`` and ``READ_TOOLS`` in 0.1.15's
#: ``permission/index.ts:699-700``. Its ``disabled()`` (``:702-722``) decides each of
#: these tools by the LAST rule naming EITHER the tool itself OR its group, so an
#: ``edit`` rule placed after ``"write": "deny"`` re-enables ``write``. This is
#: MiMo Code's own rule: it adds ``multiedit`` and ``view_image``, and it keeps the
#: tool's own name in play, where OpenCode swaps the name for the group's.
MIMOCODE_PERMISSION_GROUPS: Mapping[str, tuple[str, ...]] = {
    "edit": ("edit", "write", "apply_patch", "multiedit"),
    "read": ("read", "view_image"),
}

#: The grouped tools an MCP tool's fused id can collide with. A fused id always
#: holds the ``_`` joining its halves, so of the six grouped tools only
#: ``apply_patch`` and ``view_image`` are reachable (server ``apply``, tool
#: ``patch``). A deny Crew seeds under such an id holds by its own name, but any
#: later ``edit`` or ``read`` rule decides it too -- an agent's own permission is
#: appended after the seed -- and that rule names no MCP tool at all. So no rule of
#: Crew's is trusted for these ids, and the server is withheld whole instead.
_GROUP_REACHABLE_TOOL_IDS = frozenset(
    tool for tools in MIMOCODE_PERMISSION_GROUPS.values() for tool in tools if "_" in tool
)


def mimocode_tool_id(server: str, tool: str) -> str:
    """The id this harness gives *tool* on *server*, which its permission rules key on.

    ``sanitize(server) + "_" + sanitize(tool)`` (0.1.15 ``mcp/index.ts:1175``), each
    half in the harness's UTF-16 spelling (:func:`mimocode_spelling`). The id holds no
    ``*`` or ``?``, so a deny built from it stays a literal key in the harness's rule
    matcher. Two pairs can fuse to one id (``a_b``/``c``, ``a``/``b_c``); a deny for one
    then denies both, which can only switch off more than the spec asked.
    """
    return f"{mimocode_spelling(server)}_{mimocode_spelling(tool)}"


def mimocode_deny_rules(disabled_tools: Collection[tuple[str, str]]) -> tuple[str, ...]:
    """The tool ids Crew seeds as ``deny`` for *disabled_tools*, sorted and unique.

    An id that collides with one of MiMo Code's grouped tools is left out, because a
    group rule can outrank it (see :data:`_GROUP_REACHABLE_TOOL_IDS`). Free of I/O.
    """
    ids = {mimocode_tool_id(server, tool) for server, tool in disabled_tools}
    return tuple(sorted(ids - _GROUP_REACHABLE_TOOL_IDS))


def _withhold_reason(
    name: object, withheld: frozenset[str], unhonoured: frozenset[str], *, stub: bool
) -> str:
    """Why the element named *name* stays out of the array, as a log clause; ``""`` to keep it."""
    if name not in withheld:
        return ""
    if stub:
        return "the projection withheld the server it wraps, and a stub re-adds it unrestricted"
    if name in unhonoured:
        return (
            "one of its tools is switched off and this session cannot put that per-tool "
            "rule in force (see the earlier warning for why), so mounting it would make "
            "the tool reachable. This session runs without that server"
            + (
                "; with kirocrew-core withheld it cannot report back to its channel"
                if name in CONTROL_PLANE_SERVERS
                else ""
            )
        )
    return (
        "it is one of Crew's own servers, and an element the agent spec describes "
        "cannot carry this session's identity"
    )


def place_mimocode_array(
    projection: SessionMcpProjection,
    *,
    unhonoured: frozenset[str],
    stub_elements: Collection[Mapping[str, Any]] = (),
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> list[dict[str, Any]]:
    """The ``mcpServers`` array as MiMo Code will mount it: one element per name.

    MiMo Code 0.1.15 keys the array by server name before mounting it, so of two
    elements with one name only the LATER is mounted (``acp/agent.ts:1144``). The
    array is built the same way here, keyed by name with the later element
    replacing the earlier, so the array Crew reports is the set MiMo Code mounts.

    Two halves go in, in order: the spec-translated elements, then the shared
    gateway's broker stubs. A stub carries the SAME name as the spec entry it
    rewrites, so a stub for a withheld name would mount the unrestricted server
    under it; the withhold rule therefore covers both halves. *unhonoured* names the
    narrowed servers whose per-tool rule is not in force, withheld whole; Crew's
    identity-bound servers are withheld from the spec-described half for the reason
    :func:`~kiro_crew.providers.mirrors.identity.withheld_servers` gives. A stub is
    also held to the spec's ``tools`` allowlist from the same parse. A narrowed
    server that is NOT withheld is fine to mount as a stub: the per-tool rule keys
    on the server name, which the stub shares.
    """
    withheld = withheld_servers(frozenset()) | unhonoured
    translated = [
        element
        for element in projection.servers
        if not _logged_withhold(element.get("name"), withheld, unhonoured, stub=False)
    ]
    by_name: dict[str, dict[str, Any]] = {}
    for element in mimocode_elements(
        translated, session_key=session_key, channel_id=channel_id, session_token=session_token
    ):
        by_name[str(element.get("name") or "")] = element
    for stub in stub_elements:
        if not isinstance(stub, Mapping):
            continue
        name = stub.get("name")
        if _logged_withhold(name, withheld, unhonoured, stub=True):
            continue
        if not projection.allowlist.grants(str(name)):
            logger.info(
                "mimocode session MCP: not mounting pooled stub %r -- the agent spec's "
                "`tools` does not reference it, and the allowlist that filtered the "
                "translated half applies to a stub of the same name",
                _logged(name),
            )
            continue
        by_name[str(name or "")] = _mimocode_local_element(dict(stub))
    return list(by_name.values())


def _logged_withhold(
    name: object, withheld: frozenset[str], unhonoured: frozenset[str], *, stub: bool
) -> bool:
    """Whether *name* is withheld, logging why when it is."""
    reason = _withhold_reason(name, withheld, unhonoured, stub=stub)
    if reason:
        logger.warning(
            "mimocode session MCP: withholding %s %r -- %s",
            "pooled stub" if stub else "server",
            _logged(name),
            reason,
        )
    return bool(reason)


def mimocode_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
    denies_in_force: Collection[str] | None = None,
) -> SessionProjection:
    """The whole MiMo Code array -- spec translation AND pooled stubs -- plus its deny rules.

    The mirror's :meth:`~MimoCodeMirror.session_projection`, as a function so it can
    be called and tested without the class.

    A narrowed server stays MOUNTED: each switched-off tool is a ``deny`` under its
    fused id (:func:`mimocode_deny_rules`), returned as ``harness_deny_rules`` for the
    client to seed after ``"*": "ask"``; it hides that one tool, keyed on the server
    name, so a pooled stub shares it. *denies_in_force* is the set the read-back found
    IN FORCE, or ``None`` before any are seeded; a narrowed server whose id is not in
    it is withheld whole: an id colliding with a group (:data:`_GROUP_REACHABLE_TOOL_IDS`)
    or a rule outranked by a lower source naming the same tool, which keeps its earlier
    key position (``config/permission.ts:26-31``). ``denied_tools`` stays EMPTY: this
    harness sends only a fused title, never the ``(server, tool)`` pair a client-side
    refusal needs. Blocking (parses the agent spec once).
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    rules = mimocode_deny_rules(projection.disabled_tools)
    in_force = frozenset(rules if denies_in_force is None else denies_in_force)
    unhonoured = frozenset(
        server
        for server, tool in projection.disabled_tools
        if mimocode_tool_id(server, tool) not in in_force
    )
    for server in sorted(unhonoured):
        logger.warning(
            "mimocode session MCP: a switched-off tool on %r has no deny rule in force on "
            "this session -- either its tool id collides with MiMo Code's edit or read "
            "group, or a lower MiMo Code config source names that tool and outranks Crew's "
            "seed. Remove that tool's key from your MiMo Code config to restore per-tool "
            "deny for it",
            _logged(server),
        )
    out = place_mimocode_array(
        projection,
        unhonoured=unhonoured,
        stub_elements=stub_elements,
        session_key=session_key,
        channel_id=channel_id,
        session_token=session_token,
    )
    return SessionProjection(
        params={"mcpServers": out},
        disabled_servers=projection.disabled_servers,
        # Only the names actually withheld for a restriction: every other narrowed
        # server is mounted, with its rule in force.
        restricted_servers=unhonoured,
        unhonoured_servers=unhonoured,
        harness_deny_rules=rules,
        derived_spec_snapshot=projection.derived_spec_snapshot,
        zero_tools=projection.zero_tools,
        agent_spec=projection.agent_spec,
    )


class MimoCodeMirror(AgentConfigMirror):
    """Projects the agent spec onto ``mimo acp``."""

    backend = ACP_BACKEND_MIMOCODE

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers with NO new translator and "
                "then narrowed by this module two ways. Measured on 0.1.15: the "
                "element Crew emits is accepted, the child is spawned, its tools are "
                "listed and a call on one completes after a permission request "
                "(test/fixtures/acp_frames/mimocode/mcp-stdio-mount-live.jsonl). The "
                "narrowing: "
                "(1) a third-party server whose spec narrows it per tool stays "
                "mounted with a per-tool deny rule, and is withheld whole only when "
                "that rule is not in force -- see DENIED_TOOLS; "
                "(2) Crew's own control plane carries KIROCREW_SESSION_KEY (plus "
                "CHANNEL_ID, BOUND_PORT and any KIROCREW_HOME override) on the "
                "element, and ONLY kirocrew-core and kirocrew-cron do, because "
                "those are the two entries the translation replaces from the "
                "managed source so no part of a hand-editable spec reaches a child "
                "holding that credential. Here the carriage is about SCOPE rather "
                "than about survival: this harness's MCP children inherit the "
                "ambient environment, so without the element a control-plane child "
                "would pick up the gateway's key instead of this session's. Every "
                "OTHER managed Crew server is withheld rather than mounted "
                "credential-less, since it would answer not_bound to every call, "
                "and that set is DERIVED as the managed servers minus the control "
                "plane (providers.mirrors.identity.identity_bound_crew_servers). "
                "The array and the withhold set come from ONE parse of the spec "
                "(session_mcp_projection), so a narrowing that lands between two "
                "reads of a user-writable file cannot leave a narrowed server "
                "mounted un-narrowed. NO transport filter, unlike codex: the harness "
                "maps any element carrying `type` onto its own `remote` kind "
                "(acp/agent.ts:1144-1163 on 0.1.15), and a remote server is tried "
                "over streamable HTTP first and SSE after (mcp/index.ts:565-580), so "
                "http and sse are both taken. Two elements with one name mount only "
                "the later (acp/agent.ts:1144), and the array is built keyed by name "
                "the same way (place_mimocode_array). Unlike claude the array "
                "is NOT conditional on Crew owning a permission file: this harness's "
                "routing "
                "is Routing.VERIFIED_SEEDED_SETTINGS -- a mechanism in "
                "tool_gate.ENFORCED_ROUTINGS -- seeded on MIMOCODE_CONFIG_CONTENT "
                "and READ BACK from the harness's own config resolution before the "
                "first prompt, so a session that cannot establish the asking "
                "posture is refused rather than run",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "`tools` is not sent; it is applied during translation as the "
                "allowlist deciding which servers enter the array -- the "
                "translated half AND the pooled broker stubs, from the same parse "
                "-- so a server the spec declares but never references is not "
                "mounted here either, which is kiro-cli parity. A missing or "
                "non-list `tools` is an EMPTY allowlist, not 'no filter', for the "
                "same reason. It carries the same residual claude and codex have: "
                "an `@server/tool` grant narrows to one tool on kiro-cli but "
                "mounts the whole server here, because the tool set is not "
                "knowable without connecting",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "into a per-tool `deny` rule in the harness's own `permission` "
                "config, which Crew already seeds on MIMOCODE_CONFIG_CONTENT for the "
                'permission routing: `{"*": "ask", "<server>_<tool>": "deny"}`, the '
                "tool id spelled the way the harness names an MCP "
                "tool (mimocode_tool_id). Measured on 0.1.15, the rule "
                "removes that one tool from the model's tool list, while the server's "
                "other tools stay listed and still ask per call -- so a narrowed "
                "server stays MOUNTED, Crew's own control plane included, and "
                "per_tool_deny is settings-file. The rule is held to the harness's "
                "resolved config, not to the seed: the harness lets the LAST matching "
                "rule win, and a lower source that already names the same tool keeps "
                "its earlier place behind the seed's `*`, which turns the deny into an "
                "ask. The routing read-back evaluates every rule with MiMo Code's own "
                "disabled() (permission/index.ts:702-722), and a server whose rule did "
                "not come out in force is withheld whole, with a warning naming the "
                "cause. An id that collides with MiMo Code's edit or read group "
                "(_GROUP_REACHABLE_TOOL_IDS: apply_patch, view_image) is withheld the "
                "same way, since a later group rule also decides it. denied_tools stays EMPTY: "
                "the harness sends only a fused `<server>_<tool>` title on a call, so "
                "the client has no structured pair to refuse by",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "this harness's nearest equivalent is its own `permission` setting, "
                "and Crew already writes the one value the host gate needs there "
                "(ask) and verifies it by reading the harness's resolved config "
                "back. Translating autoApprove would pre-approve the call INSIDE "
                "the harness, which then never sends session/request_permission -- "
                "skipping Crew's permission gate, its governance ceiling and its "
                "SEL audit. This is the harness whose asking is the reason it is "
                "offered at all, so every MCP call must reach the host gate",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "not through this mirror: mimocode is in "
                "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION, so the resolved model is "
                "pushed with session/set_config_option after session/new, and in "
                "ACP_BACKENDS_MODEL_SELECT_UNCHECKED, so only a value the advertised "
                "select carries is written: the harness stores any value and then "
                "fails every prompt on one it cannot resolve. Named "
                "here rather than left out so a reader does not read this mirror's "
                "silence as the model being dropped",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the direction is reversed on this backend, as on codex: the harness "
                "advertises its own model list as a session/new configOptions "
                "select and that advertised list is the only source of ids "
                "set_config_option accepts, so Crew CAPTURES the advertised set "
                "(ACP_BACKENDS_ADVERTISED_MODEL_SELECTION) instead of sending one. "
                "Projecting the spec's availableModels here would offer ids the "
                "harness refuses",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "the harness has a `permission` setting and Crew writes it -- but it "
                "writes the FIXED value tool_gate demands (ask), not the mode the "
                "spec asked for, and then reads the harness's own resolved config "
                "back to check no higher-precedence source overrode it. Honouring "
                "a spec-requested mode would let an agent file widen a MiMo Code "
                "session past the one boundary that makes this harness offerable. "
                "A deliberate override, not a dropped setting",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "not a mirror concern on any backend: the prompt reaches every "
                "harness as ordinary prompt text in the [AGENT SYSTEM PROMPT] "
                "context block, which is backend-agnostic and already works",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "same as PROMPT -- steering files are injected as context text, "
                "not projected into a backend's config",
            ),
            Concern.HOOKS: Ruling(
                _D.WITHHELD,
                "the session/new element set has no hooks field and this backend is not "
                "in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS: its memory, history and task tools, "
                "and an edit under its memory directory, never consult its permission "
                "service, and question and plan_exit ask through its question service "
                "instead (tool/question.ts:47-51, tool/plan.ts:42-57 on 0.1.15), which "
                "its ACP layer never forwards. A PreToolUse hook fired from the "
                "permission request would silently miss all of them. A saved hooks "
                "block is reported as a projection gap rather than claimed",
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
        session_token: str = "",
        **kwargs: object,
    ) -> dict[str, object]:
        """The wire face: the ``mcpServers`` array for this MiMo Code session.

        ``permission_surface_owned`` (in ``kwargs``) is IGNORED, as for any mirror
        outside claude's class: it guards claude's permission file, and this
        harness's routing is seeded on ``MIMOCODE_CONFIG_CONTENT`` and READ BACK, so
        failing closed on it would withhold every Crew tool for nothing.
        ``session_key`` and ``channel_id`` scope the control plane: MCP children
        inherit the ambient env, so without them they would hold the GATEWAY's key.
        ``work_dir`` is required for correctness: a project-only agent under
        ``<work_dir>/.kiro/agents`` would otherwise read as "no spec" and lose its
        ``tools`` allowlist. ``stub_elements`` are the gateway's broker stubs, placed
        here so one withhold rule covers both halves (:func:`mimocode_projection`).
        Blocking (reads the agent spec); the caller warms it on the spawn path and
        serves the shared ``session/new`` call site from that cache (H13).
        """
        stubs = kwargs.get("stub_elements") or ()
        return self.session_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stubs if isinstance(stubs, (list, tuple)) else (),
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
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
        session_token: str = "",
        **kwargs: object,
    ) -> SessionProjection:
        """The structured face: :func:`mimocode_projection`.

        ``harness_denies_in_force`` arrives in ``kwargs``: the rule ids the client's
        read-back found in force, or ``None`` before any were seeded. Every other
        keyword in ``kwargs`` is ignored, as :meth:`session_params` documents
        (``permission_surface_owned`` arrives there)."""
        in_force = kwargs.get("harness_denies_in_force")
        return mimocode_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
            denies_in_force=(
                frozenset(str(rule) for rule in in_force)
                if isinstance(in_force, (set, frozenset, list, tuple))
                else None
            ),
        )
