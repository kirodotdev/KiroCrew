"""Kiro agent spec -> the ACP ``session/new`` ``mcpServers`` array.

For a harness in :data:`~kiro_crew.acp_backends.ACP_BACKENDS_SESSION_MCP_ARRAY`,
the ``session/new`` / ``session/load`` ``mcpServers`` parameter is where Kiro
Crew's MCP servers come from and the only place: neither claude-agent-acp nor
codex-acp reads ``~/.kiro/agents/<name>.json``. kiro-cli reads native servers
through ``--agent``; managed control planes can also receive per-session
overrides solely to carry ordinary identity. Without
the translation here such a session runs with ZERO Kiro Crew tools -- the harness
itself works (prompts, streaming, permissions) but ``send_message``,
``spawn_run``, ``cron_add`` and every user-installed server are simply absent.

Nothing here is Anthropic-specific by design: the module is keyed on the
capability, not on the harness, so the next adapter that reads no agent spec of
Crew's joins the set rather than growing a second translator. What is genuinely
per-adapter stays with that adapter's mirror -- codex narrows this output in
:mod:`kiro_crew.providers.mirrors.codex` (it drops any transport this session's
adapter did not advertise, and its child processes inherit no environment), and
the shape notes below are claude-agent-acp's own zod schema.

The agent spec stays the single source of truth; there is no second,
claude-shaped registry to keep in sync. It is read per spawn, so installing or
toggling an MCP server takes effect on the NEXT session with no gateway restart.
Nothing here raises: a missing or malformed spec degrades to Crew's own control
plane, never to a failed spawn.

Shape notes -- these are claude-agent-acp's zod schema rather than anything in
the ACP spec at large:

* ``env`` (stdio) and ``headers`` (http/sse) are REQUIRED arrays of
  ``{"name", "value"}`` objects. Omitting either fails ``session/new`` outright
  with ``-32602 Invalid params (expected array, received undefined)``, so they
  are always emitted -- empty when there is nothing to carry.
* A url-bearing entry is routed by ``type``. Without one the adapter takes the
  stdio branch and rejects the entry for having no ``command``, so the transport
  is always spelled out.
* kiro-cli-only keys (``timeout``, ``disabledTools``, ``autoApprove``) cannot
  ride along in an element. ``disabledTools`` is a RESTRICTION, so dropping it
  outright would widen the tool surface; it comes back as a
  ``permissions.deny`` rule instead (see :func:`session_mcp_deny_rules`).
  ``autoApprove`` is dropped deliberately, not for want of a mapping:
  Claude's nearest equivalent is a ``permissions.allow`` entry, and a
  pre-approved tool is one Claude never asks about -- so the call never reaches
  the host ``canUseTool`` gate that carries the deny floor, the sensitive-path
  check and the governance ceiling. Every MCP call on this backend is gated.

**The governing rule, stated once because each corner of it is easy to argue
separately: this module matches kiro-cli, and deviating in EITHER direction
is the defect.** Granting what kiro-cli would drop widens the session's tool
surface behind the user's back; withholding what kiro-cli would keep removes
capability from a session with no error to explain it. Two consequences that are
otherwise easy to argue backwards:

* **Which specs are resolved.** kiro-cli resolves ``--agent`` against the project
  checkout's ``.kiro/agents/*.json`` as well as ``~/.kiro/agents/``, so this
  module must too, project-nearest first. Resolving only the user level looked
  conservative and was the opposite: a project-only agent found no spec, so its
  ``tools`` allowlist never ran and the control plane mounted unrestricted --
  a user-declared restriction dropped silently. See :func:`_agent_spec_for`.
* **What the registry ceiling governs.** Registry mode withholds every
  SPEC-DECLARED server, because nothing here can resolve a marker against the
  admin's catalog. It does NOT withhold Crew's own control plane, which is
  re-derived from the managed source rather than read from the spec. That is
  kiro-cli parity, not an exemption: ``agent._install_agent_spec`` stamps the
  managed servers with ``"type": "registry"`` precisely so the client keeps them
  under registry mode, and ``agent._mcp_registry_mode`` records their
  disappearance as the defect ("the features they carry (``spawn_run``,
  ``cron_add``, ``learn_add``, ...) disappear with no local error"). Withholding
  them HERE would make this backend stricter than kiro-cli and reproduce that
  failure on the installs least able to diagnose it. The control plane is still
  subject to the ``tools`` allowlist, which is the restriction that does apply.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew import agent as _agent_mod
from kiro_crew import mcp_provenance
from kiro_crew.acp.mcp_session_report import sanitize_sink_text
from kiro_crew.agent import (
    _mcp_registry_mode,
    agent_spec_path,
    ensure_agent_materialized,
    managed_mcp_spec_entry,
    require_fresh_derived_spec,
)
from kiro_crew.agent_discovery import _read_agent_spec, project_agent_files, project_agent_name
from kiro_crew.agent_sdk.mcp_refs import parse_tools_refs
from kiro_crew.env import sanitize_spec_env
from kiro_crew.mcp_cleanup import (
    CONTROL_PLANE_SERVERS,
    KIROCREW_BIN_MCP_SERVERS,
    mcp_entry_is_muted,
)

logger = logging.getLogger(__name__)

# Crew's own control plane, defined in the ``mcp_cleanup`` leaf and re-exported
# here. Re-derived from the managed source of truth on every spawn so a stale
# hand-edited command in the spec cannot cost a claude session the tools it needs
# to report back to its channel at all. Both are always-on (no gate, not
# opt_in), so ``managed_mcp_spec_entry`` returns them unless the install is
# broken. Re-derived, not read from the spec, is also what keeps them out of the
# registry filter below: they are the host's own process, not a third-party
# server the admin's catalog governs.
#
# PUBLIC on this module because the codex projection carries this session's
# identity onto these two entries and onto NOTHING else, and the safety of that
# carriage rests on this being the set the loop below REPLACES from the managed
# source: the element's command, args and env are Crew's own by construction, not
# the spec's. The definition sits in the leaf so a consumer the agent-SDK import
# boundary keeps off ``kiro_crew.acp`` -- the broker-stub ceiling in
# ``mcp_gateway.session_servers`` -- reads the same tuple rather than a second
# copy of it.

# Every managed Crew server that must be handed the session's IDENTITY when it
# is mounted -- a wider set than the control plane above, and a different
# question. The control plane is what a session gets whether or not its spec
# names it; this is what a session's tool calls on any Crew server need to
# succeed. Each of these servers runs through ``mcp_shared.run_mcp_stdio_loop``,
# whose ``tools/call`` reads the session's tool policy from the gateway, and the
# gateway reads a declared session key only behind an attestation. A server that
# is granted (``@kirocrew-dashboard`` in ``tools``) but carries no token comes up
# present-but-unusable: it resolves the session key through the pid sidecar and
# then refuses every call as ``identity_unattested``. On the kiro backend the
# runtime is session-unbound, so identity travels per MCP element
# (:func:`kiro_control_plane_servers`), and that carriage has to cover the
# opt-in servers too, not only the two always-on ones.
#
# Derived from the managed set rather than spelled out so a server added to it
# later is covered by construction; gatewayd mirrors this for its per-frame
# token hand-off (``CONTROL_PLANE_BACKENDS``) and a ratchet test pins the two.
# The control plane leads, in ITS order: a session that grants only the two
# always-on servers emits the same elements in the same order it always did,
# and the opt-in servers follow in the managed set's order.
IDENTITY_BOUND_SERVERS: tuple[str, ...] = CONTROL_PLANE_SERVERS + tuple(
    name for name in KIROCREW_BIN_MCP_SERVERS if name not in CONTROL_PLANE_SERVERS
)

# kiro-cli's enterprise-governance discriminator, mirrored rather than imported
# (``agent._MCP_REGISTRY_TYPE`` is private; a ratchet test pins the two equal).
_KIRO_REGISTRY_TYPE = "registry"


def _acp_pairs(raw: Any) -> list[dict[str, str]]:
    """A kiro-agent-JSON ``env``/``headers`` mapping in ACP's array-of-pairs form.

    Values are stringified because the adapter's schema types them as strings
    while the agent spec is hand-editable JSON, where a port number or a boolean
    is an easy thing to write.
    """
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def acp_server_element(name: str, spec: Any) -> dict[str, Any] | None:
    """One ``mcpServers`` entry as a claude-agent-acp array element.

    ``None`` when the entry declares no usable transport -- neither a ``url`` nor
    a ``command``. Skipping is the right outcome there: an element the adapter
    rejects fails the whole ``session/new``, taking every other server with it.
    """
    if not isinstance(spec, dict):
        return None
    url = spec.get("url")
    if isinstance(url, str) and url:
        # Only ``sse`` is distinguished; anything else (including a missing
        # ``type``) is streamable HTTP, which is the adapter's own default and
        # the shape every modern remote server speaks.
        stype = "sse" if spec.get("type") == "sse" else "http"
        return {
            "name": name,
            "type": stype,
            "url": url,
            "headers": _acp_pairs(spec.get("headers")),
        }
    command = spec.get("command")
    if not isinstance(command, str) or not command:
        logger.debug("session MCP: skipping %r -- entry declares no command and no url", name)
        return None
    # Only a sequence is iterated. The spec is hand-editable JSON, so ``"args":
    # 8080`` or ``"args": "--flag"`` is an easy thing to write -- and iterating a
    # number raises ``TypeError`` while iterating a string would explode it into
    # one argument per character. Nothing in this module may raise: the exception
    # would travel out through ``session_mcp_servers`` and fail the whole
    # ``session/new``, costing the session every OTHER server as well.
    raw_args = spec.get("args")
    args = [
        a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
        for a in (raw_args if isinstance(raw_args, (list, tuple)) else ())
    ]
    if raw_args and not isinstance(raw_args, (list, tuple)):
        logger.warning(
            "session MCP: %r declares a non-list args (%s); launching it with none",
            name,
            type(raw_args).__name__,
        )
    return {
        "name": name,
        "command": command,
        "args": args,
        "env": _acp_pairs(spec.get("env")),
        "type": "stdio",
    }


class _Unread:
    """Marker for "the caller did not hand in a parsed spec", distinct from None.

    ``None`` is a real answer -- "there is no spec" -- and the projection must be
    able to say it. If ``None`` also meant "please read it", a projection whose
    one read came back empty would have every helper re-read behind its back, and
    a spec that became readable between those reads would be translated by one
    helper while the allowlist, computed from the ``None``, granted everything: an
    unreferenced pooled stub mounted past a grant-all allowlist. The sentinel makes
    "not supplied" and "supplied, and it is nothing" different values.
    """


_UNREAD = _Unread()


def _read_mcp_settings(path: Path) -> dict[str, Any]:
    """Read settings through the credential gate; only absence means no restrictions."""
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes, validate_file_path

    # Screen before even probing existence: a Windows link can name an untrusted share.
    if validate_file_path(str(path)) is None:
        raise ValueError("MCP settings path was refused")
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    try:
        raw = safe_read_file_bytes(str(path))
    except FileTooLargeError as exc:
        raise ValueError("MCP settings exceed the safe read limit") from exc
    if raw is None:
        raise ValueError("MCP settings could not be safely read")
    try:
        settings = json.loads(raw.decode("utf-8"))
    except RecursionError as exc:
        raise ValueError("MCP settings exceed the JSON nesting limit") from exc
    if not isinstance(settings, dict) or not isinstance(settings.get("mcpServers", {}), dict):
        raise ValueError("MCP settings must contain an object of servers")
    return settings


def _global_settings(*, strict: bool = False) -> dict[str, Any]:
    """The user's global ``~/.kiro/settings/mcp.json``, ``{}`` when absent or bad.

    Read here because the dashboard's tool-off action (``/api/mcp/toggle-tool``)
    writes ``disabledTools`` to THIS file and to nothing else -- kiro-cli reads it
    for enforcement alongside the agent spec, and the spec rebuild copies a global
    entry into the spec only when the spec lacks that server, never onto Crew's
    own managed servers. So a restriction on ``kirocrew-core`` written the ordinary
    way lives only here, and a deny set read from the spec alone would miss the one
    path a user actually takes. Resolved through the ``agent`` module attribute at
    call time so a test can point it at a temp file the same way it points the
    agents directory.

    ``strict`` preserves read failures for Kiro overrides: native restrictions
    must remain authoritative when their settings cannot be inspected safely.
    """
    try:
        return _read_mcp_settings(_agent_mod._KIRO_MCP_JSON)
    except (OSError, ValueError):
        if strict:
            raise
        logger.debug("session MCP: global settings unreadable")
        return {}


class ToolsAllowlist(NamedTuple):
    """The spec's ``tools`` allowlist, as a value rather than a filter pass.

    Exists so the SAME allowlist can gate every element that reaches a session
    -- the translated entries inside :func:`session_mcp_servers`, and the pooled
    broker stubs a mirror appends beside them. A stub carries the name of the
    entry it rewrites, so a stub for a server the spec's ``tools`` never
    references is an unreferenced server mounted anyway, which is the exact
    thing the filter on the translated half exists to prevent.

    ``applies`` is False when there is no spec at all: then there is no allowlist
    to apply and everything stands, the same reading :func:`session_mcp_servers`
    takes for the control plane. A spec whose ``tools`` is missing or not a list
    is an EMPTY allowlist, not "no filter" (see the filter's own comment).

    Why an allowlist at all: kiro-cli loads a server only when ``tools`` references
    it (``@server`` or ``@server/tool``), so an ``mcpServers`` entry with no
    reference is declared but never mounted. An ACP array has no such indirection
    -- everything in it is mounted -- so the reference is applied here instead.
    Without it, an entry the user deliberately left unreferenced (the shape every
    ``opt_in`` grant uses, and what a narrowed-by-hand spec looks like) would come
    alive the moment the session ran on a mirrored backend.

    The refs are read through :func:`~kiro_crew.agent_sdk.mcp_refs.parse_tools_refs`
    rather than scanned here, so this module and the unresolved-ref detector cannot
    disagree about what an entry names -- a guard that read ``@srv`` where this
    read nothing would report a ref as unresolved while the server mounted, and the
    reverse would mount a server the guard called absent. Only the bare ``*``
    grants everything: ``@*`` is a server LITERALLY named ``*`` there, matching
    this repo's other readers. ``@builtin`` is not special-cased -- a server
    actually called ``builtin`` is mountable, and the namespace exclusion belongs
    to the guard asking whether a ref resolves.
    """

    applies: bool
    grant_all: bool
    refs: frozenset[str]

    def grants(self, name: str) -> bool:
        return (not self.applies) or self.grant_all or name in self.refs


def _tools_allowlist(spec: dict[str, Any] | None) -> ToolsAllowlist:
    """The allowlist *spec* declares; see :class:`ToolsAllowlist` for the two edges."""
    if spec is None:
        return ToolsAllowlist(applies=False, grant_all=False, refs=frozenset())
    tools = spec.get("tools")
    grant_all, refs = parse_tools_refs(tools if isinstance(tools, list) else [])
    return ToolsAllowlist(applies=True, grant_all=grant_all, refs=frozenset(refs))


def _project_spec_path_for(agent: str, work_dir: str | Path | None) -> Path | None:
    """The project checkout's spec for *agent*, or ``None``.

    ``<work_dir>/.kiro/agents/*.json`` is the only project location kiro-cli
    itself resolves ``--agent`` against, so it is the only one whose names are
    dispatchable and therefore the only one this module honours.
    ``project_agent_files`` already refuses a sensitive project root and
    ``project_agent_name`` applies the same declared-name-beats-filename order
    kiro-cli lists by, so neither rule is restated here.

    Never raises: an unreadable checkout resolves to no project spec rather than
    failing the spawn.
    """
    if not work_dir:
        return None
    try:
        for spec in project_agent_files(
            work_dir, operation="session_mcp_project_agent", source="unknown"
        ):
            if project_agent_name(spec) == agent:
                return spec
    except OSError:
        logger.debug("session MCP: could not scan %s for project agents", work_dir, exc_info=True)
    return None


def _agent_spec_for(agent: str, work_dir: str | Path | None = None) -> dict[str, Any] | None:
    """The materialized kiro spec for *agent*, or ``None`` when unreadable.

    The spec alone; :func:`_agent_spec_and_snapshot_for` is the same read returning
    also the freshness snapshot a derived agent was verified against, for the caller
    whose ``session/new`` array CONSUMES that spec and has to re-verify it afterwards.
    """
    return _agent_spec_and_snapshot_for(agent, work_dir)[0]


def _agent_spec_and_snapshot_for(
    agent: str, work_dir: str | Path | None = None
) -> tuple[dict[str, Any] | None, Any]:
    """The spec for *agent* AND the ``DerivedSpecSnapshot`` it was verified against.

    The second element is ``None`` for every agent that mirrors nothing. For a derived
    agent it is the snapshot whose bytes are returned as the first element, so the
    caller can prove after the host has consumed them -- at the ``session/new`` or
    ``session/load`` response -- that the default spec did not change in between. A
    caller that dropped it would have the gate and the projection but no way to close
    the window between them.

    **Project-nearest first.** kiro-cli resolves ``--agent`` against the project
    checkout as well as the user level, so a project-only agent must not read as
    "no spec": that dropped its ``tools`` allowlist and mounted the control plane
    unrestricted, which is a user-declared restriction lost rather than a default
    applied. The project spec therefore wins when both declare the name, the way
    a nearer config layer normally does.

    Materializes first: a source checkout that skipped setup has no spec on disk
    at all, and the claude spawn path -- unlike kiro-cli's ``--agent`` one -- has
    no other reason to write it. Best-effort and never raises.

    A DERIVED agent (``kirocrew-worker``) is answered from the freshness gate's own
    snapshot and reads nothing here -- see the comment at that branch for why a read
    after the gate is a second observation rather than a tighter one.

    Reads through ``agent_discovery._read_agent_spec``, the module's documented
    ONE reader, rather than parsing the file here: the agents directory is
    user-writable and shared with other tools, so the guards it applies are the
    point -- a symlink whose resolved target is sensitive
    (``kirocrew.json -> ~/.aws/credentials``) is refused and audited, an oversized
    file is refused at the size cap instead of being read into memory during a
    spawn, and non-UTF-8 bytes or non-object JSON come back as ``None``. The
    labels name THIS surface so a refusal is attributed to the session-MCP
    translation rather than to an unrelated agent listing; ``source`` is
    ``"unknown"`` because a session is started from every channel Crew has.
    """
    ensure_agent_materialized(agent)
    # Refuse a derived spec that predates the default rather than PROJECT it: this
    # function's answer becomes the session's MCP surface, so a stale mirror here is
    # the revoked server reaching the session.
    snapshot = require_fresh_derived_spec(agent, work_dir)
    if snapshot is not None and snapshot.spec is not None:
        # The snapshot travels WITH the bytes. This is the ONE consumption the array-
        # backed hosts have -- they read no spec of their own, the array IS the load --
        # so the post-consume check has to compare against this observation and not a
        # fresh one taken later, which would judge the file rather than the array.
        # ZERO reads below this line for a derived agent. The gate already read and
        # verified those bytes, so re-reading the file here would be a SECOND
        # observation of it -- and a revocation landing between the two would become the
        # session's MCP surface as though it had been checked. No lock closes that gap:
        # both halves are this process's own reads, so the second read is removed rather
        # than re-verified. The project-spec branch below is unreachable for a derived
        # agent anyway: the gate REFUSES a checkout that declares one.
        return snapshot.spec, snapshot
    project = _project_spec_path_for(agent, work_dir)
    if project is not None:
        return (
            _read_agent_spec(project, operation="session_mcp_project_agent", source="unknown"),
            None,
        )
    try:
        path = agent_spec_path(agent)
    except ValueError:
        # Two specs declare this name, so which one is live is undefined. No
        # answer is the honest one; the control plane still loads below.
        logger.warning("session MCP: ambiguous agent spec for %r", agent, exc_info=True)
        return None, None
    if path is None:
        logger.info(
            "session MCP: no spec on disk for agent %r; loading Crew's control plane only", agent
        )
        return None, None
    return _read_agent_spec(path, operation="session_mcp_servers", source="unknown"), None


def agent_spec_snapshot(
    agent: str | None, *, work_dir: str | Path | None = None
) -> dict[str, Any] | None:
    """The spec for *agent* exactly as this module's own translation reads it.

    Exported for the unresolved-ref detector's two callers --
    :mod:`kiro_crew.acp.mcp_ref_guard` at session establishment and
    ``agent_sdk.drivers.acp.agent_spec_mcp_refs`` for ``kirocrew doctor`` -- which
    have to judge the spec's ``tools`` refs against the array a session receives.
    Reading the file themselves would give them a SECOND resolution order, and
    either could then report a ref as unresolved because it read a different spec
    than the one the projection ran on -- see :func:`_agent_spec_for` for why the order (project
    checkout nearest, then user level) is load-bearing rather than incidental.

    Blocking, and never raises: callers run it off the event loop and treat
    ``None`` as "nothing to say".
    """
    return _agent_spec_for(agent, work_dir) if agent else None


def session_mcp_deny_rules(agent: str | None, *, work_dir: str | Path | None = None) -> list[str]:
    """Claude ``permissions.deny`` rules re-applying the spec's per-TOOL narrowing.

    ``disabledTools`` is a kiro-cli-only key, so it cannot ride along in the
    array element -- but it is a RESTRICTION, and dropping a restriction while
    forwarding the server that carries it widens the session's tool surface
    behind the user's back. The dashboard writes that key when someone turns an
    individual tool off, and the repo already treats losing it as a defect
    elsewhere ("dropping ``disabledTools`` on a save would silently widen the
    agent's tool surface"). Claude has no per-server allowlist, but it does have
    ``permissions.deny``, which is evaluated ahead of every allow rule and of the
    host callback, so the disabled tool is refused rather than merely asked
    about.

    Returned as rules for the settings writer rather than applied here: this
    module owns the array, ``settings.local.json`` belongs to the client. Ordered
    and de-duplicated so a re-seed produces a byte-identical file. The pairs come
    from :func:`session_mcp_disabled_tools`, so a restriction the dashboard wrote
    to the global settings file is carried too, not only one written in the spec.

    Note the asymmetry this does NOT close: a ``tools`` reference of the
    ``@server/tool`` form grants ONE tool on kiro-cli, while the array mounts the
    whole server here, and the set of tools to deny is not knowable without
    connecting to the server. Those extra tools still reach the host permission
    gate; they are a wider surface, not an ungated one.
    """
    return sorted(
        f"mcp__{server}__{tool}"
        for server, tool in session_mcp_disabled_tools(agent, work_dir=work_dir)
    )


def session_mcp_disabled_tools(
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    spec: dict[str, Any] | None | _Unread = _UNREAD,
    settings: dict[str, Any] | _Unread = _UNREAD,
) -> frozenset[tuple[str, str]]:
    """Every ``(server, tool)`` pair switched off for this session, from BOTH sources.

    The structured form of :func:`session_mcp_deny_rules`, which spells the same
    pairs as claude ``permissions.deny`` rules. Kept as PAIRS here because the
    ``mcp__server__tool`` spelling is lossy -- it splits on the last ``__``, so a
    tool name containing ``__`` reads back as part of the server -- and a consumer
    that compares against an identity the adapter reports as two separate fields
    (codex's ``rawInput.server`` / ``rawInput.tool``) must not go through it.

    No server is exempt, the control plane included. This set answers "what did
    the user switch off", and that is true of ``kirocrew-core`` exactly as it is
    of a third-party server: the dashboard writes the key on an ordinary tool-off
    action for any of them. What differs per server is how -- or whether -- a
    given backend can HONOUR it, and that is the caller's question, not this one's
    (see :func:`session_mcp_restricted_servers` for the one place the control
    plane is treated differently, and why).

    Two sources, unioned. The agent spec carries the key when a spec author wrote
    it or the rebuild copied a global entry in. The global ``settings/mcp.json``
    carries it when the dashboard's tool-off action wrote it -- and for Crew's own
    managed servers that is the ONLY place it can be, because the spec rebuild
    never copies a global entry onto a managed server (see
    :func:`_global_settings`). kiro-cli reads both for enforcement; so does this.
    A union is the only safe combination: a restriction can only ever restrict.

    ``spec`` and ``settings`` let a caller hand in what it has ALREADY parsed; an
    explicit ``None`` spec means "there is no spec" and is honoured as such, never
    re-read (see :class:`_Unread`). Never raises: an unreadable source switches
    nothing off from that source.
    """
    if isinstance(spec, _Unread):
        spec = _agent_spec_for(agent, work_dir) if agent else None
    if isinstance(settings, _Unread):
        settings = _global_settings()
    pairs: set[tuple[str, str]] = set()
    for source in (spec, settings):
        if not isinstance(source, dict):
            continue
        raw = source.get("mcpServers")
        if not isinstance(raw, dict):
            continue
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            disabled = entry.get("disabledTools")
            if not isinstance(disabled, list):
                continue
            for tool in disabled:
                if isinstance(tool, str) and tool:
                    pairs.add((str(name), tool))
    return frozenset(pairs)


def session_mcp_disabled_servers(spec: Any, settings: Any) -> frozenset[str]:
    """Every server switched off WHOLE by ``disabled: true``, from both sources.

    A different question from :func:`session_mcp_restricted_servers`, and the
    difference is what makes this its own function: a per-tool narrowing leaves a
    server the user still wants, while ``disabled`` withdraws the server itself. The
    array honours that through the ``tools`` allowlist -- ``agent.build_agent_config``
    strips a disabled server's ``@alias``, so nothing mounts it -- but an allowlist
    only governs elements the SPEC describes. A caller that appends an element of its
    OWN (``AcpClient._append_member_dispatch_server``, which mounts a server the
    template deliberately does not name) is outside that rule and has to ask this.

    Unlike the restriction sets there is no backend condition on the answer: a
    whole-server disable has no per-call form, so no harness can refuse a call to a
    server it was handed. The only faithful action anywhere is not mounting it.

    Same two sources as :func:`session_mcp_disabled_tools`, unioned for the same
    reason -- a switch-off can only ever switch off -- and taken as the caller's
    ALREADY-PARSED bytes so this cannot disagree with the array built beside it. The
    control plane is NOT exempt: ``disabled`` on ``kirocrew-core`` is the user
    saying so, and this function only reports it. Free of I/O.
    """
    names: set[str] = set()
    for source in (spec, settings):
        if not isinstance(source, dict):
            continue
        raw = source.get("mcpServers")
        if not isinstance(raw, dict):
            continue
        for name, entry in raw.items():
            # The shared launch predicate (fail-closed on a non-boolean), the same
            # read the projections and the gateway rewriter make.
            if mcp_entry_is_muted(entry):
                names.add(str(name))
    return frozenset(names)


def session_mcp_server_is_disabled(
    name: str, agent: str | None, *, work_dir: str | Path | None = None
) -> bool:
    """Whether *name* is switched off WHOLE for a session running as *agent*.

    The reading form of :func:`session_mcp_disabled_servers`, for a caller that holds
    no parse of its own to pass in. ``AcpRuntime`` is that caller: it composes the
    array for an ``ACP_BACKENDS_ACP_RUNTIME`` host, and half of those hosts have no
    mirror to carry the answer down -- KAS projects through ``acp.kas_agents`` rather
    than through an ``mcpServers`` array at all -- so a field on the mirrored
    projection would answer for one of them and not the other.

    One read of each source, which on a mirrored host is a SECOND read of files its
    projection also read. The direction that costs is the safe one: a switch-off can
    only ever switch off, so the window between two reads can withhold a mount whose
    switch-off arrived a moment ago and can never mount one it missed. A caller that
    HAS the parse uses :func:`session_mcp_disabled_servers` instead and keeps its
    answers on one read.

    Blocking (reads the agent spec and the global settings file); callers run it off
    the event loop. Never raises: an unreadable source switches nothing off, the same
    contract :func:`session_mcp_disabled_tools` keeps.
    """
    spec = _agent_spec_for(agent, work_dir) if agent else None
    return name in session_mcp_disabled_servers(spec, _global_settings())


def session_mcp_restricted_servers(disabled_tools: Collection[tuple[str, str]]) -> frozenset[str]:
    """Servers whose per-TOOL narrowing no transport can carry as an element.

    The sibling of :func:`session_mcp_deny_rules`, for a backend that has no file
    to put deny rules in. Same input, same reason to exist: ``disabledTools`` is a
    RESTRICTION, and a backend that forwards the server while dropping it widens
    the session's tool surface behind the user's back -- the dashboard writes that
    key on an ordinary tool-off action, and this repo already treats losing it as a
    defect ("dropping ``disabledTools`` on a save would silently widen the agent's
    tool surface").

    Claude re-applies the restriction as ``permissions.deny`` rules and so may keep
    the server. A backend with no deny channel has only one faithful option, which
    is to not mount the server at all -- so this returns the NAMES and lets that
    backend omit them. Withholding a server is an availability cost; forwarding an
    un-narrowed one is a capability the user switched off.

    **Crew's own control plane is exempt from WITHHOLDING, not from the
    restriction.** ``kirocrew-core`` / ``kirocrew-cron`` are re-derived from
    ``managed_mcp_spec_entry``, which emits only command/args/env, so a
    ``disabledTools`` on their spec entry never reaches the element -- and
    withholding the whole server on the strength of it would leave the session
    unable to report back to its channel at all, which is the exact defect this
    module exists to fix. The restriction itself is still honoured, on the one
    channel this transport does have: every call to one of these servers reaches
    Crew as a ``session/request_permission`` (their tools carry no annotations, so
    codex prompts for each), and the client answers a call to a switched-off tool
    with the adapter's reject option before anything runs. The pairs it checks
    come from :func:`session_mcp_disabled_tools`, on the same parse as this set.
    That channel is complete for the control plane and NOT for a third-party
    server -- a tool annotated ``readOnlyHint`` is auto-approved inside codex and
    never prompts -- which is why the two are treated differently here rather
    than both being mounted.

    ``disabled`` is deliberately NOT part of this. A disabled server is already
    absent from the array by a different mechanism: ``agent.build_agent_config``
    strips its ``@alias`` from ``tools``, and the allowlist filter in
    :func:`session_mcp_servers` mounts nothing ``tools`` does not name. Re-checking
    it here would be a second spelling of a rule that already holds.

    Derived from the ``(server, tool)`` PAIRS :func:`session_mcp_disabled_tools`
    resolved, never from the spec on its own, so the two decisions a restriction
    drives -- withhold the server, or refuse the call -- read the same sources. The
    dashboard's tool-off action writes to the global settings file and not to the
    spec, so a withhold set read from the spec alone would miss a third-party server
    narrowed the ordinary way; that server would mount, and the per-call refusal
    cannot reach it (a tool annotated ``readOnlyHint`` is approved inside codex
    without asking), so the switched-off tool would run. Its one caller is
    :func:`session_mcp_projection`, which hands in the pairs it already resolved
    from one read of each source. A server named only in the global file and not in
    the spec lands here too and is harmless: nothing mounts it. Free of I/O.
    """
    return frozenset(server for server, _tool in disabled_tools) - frozenset(CONTROL_PLANE_SERVERS)


def _registry_mode() -> bool:
    """Whether the operator declared this install registry-governed.

    Wrapped so a config-plane failure cannot be read as "no ceiling declared".
    Registry mode is a CEILING: while it is on, every server in the spec is
    withheld, because this backend cannot resolve a registry marker against the
    admin's catalog. An unreadable declaration is therefore read as GOVERNED
    rather than as the ungoverned default -- guessing "off" launches the
    session's unmarked local servers past a ceiling the operator may well have
    set, which is the one outcome the ceiling exists to prevent. The cost is
    stated rather than hidden: an install whose config plane is broken loses its
    session MCP surface until the read succeeds again, and the warning says so.
    """
    try:
        return _mcp_registry_mode()
    except Exception:  # pragma: no cover - defensive; the helper is fail-soft
        logger.warning(
            "session MCP: could not read registry mode; treating this install as "
            "registry-governed and withholding every agent-spec server, so an "
            "unmarked local server cannot launch past a ceiling that may be in "
            "force. This session runs without its agent-spec MCP servers.",
            exc_info=True,
        )
        return True


class SessionMcpProjection(NamedTuple):
    """Everything a backend derives from the agent spec, from ONE parse of it."""

    #: The translated ``mcpServers`` array (:func:`session_mcp_servers`).
    servers: list[dict[str, Any]]
    #: Servers whose per-tool narrowing forces withholding them
    #: (:func:`session_mcp_restricted_servers`).
    restricted: frozenset[str]
    #: Every ``(server, tool)`` the spec switches off, no server exempt
    #: (:func:`session_mcp_disabled_tools`).
    disabled_tools: frozenset[tuple[str, str]]
    #: Servers switched off WHOLE by ``disabled: true``
    #: (:func:`session_mcp_disabled_servers`). Separate from ``restricted`` because
    #: no backend has a per-call form for it, so nothing may mount one.
    disabled_servers: frozenset[str]
    #: The ``tools`` allowlist the translated half was filtered by, so a caller
    #: appending elements of its own (pooled stubs) can hold them to the same one.
    allowlist: ToolsAllowlist
    #: The ``agent.DerivedSpecSnapshot`` the spec above was verified against, or
    #: ``None`` for an agent that mirrors nothing. The array IS where an array-backed
    #: host consumes the spec, so the caller re-verifies THIS after ``session/new`` /
    #: ``session/load`` -- one snapshot per consumed load.
    derived_spec_snapshot: Any = None


def session_mcp_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    work_dir: str | Path | None = None,
) -> SessionMcpProjection:
    """The array, the withhold set AND the per-tool restrictions, from ONE parse.

    A backend that translates the spec, withholds part of it on the strength of the
    spec, and refuses individual calls on the strength of the spec must derive all
    three from the same bytes. Independent parses of a user-writable file are a
    consistency window: an entry that gains ``disabledTools`` between two of them
    produces a restriction set that does not mention it and a translation that
    carries it, so the narrowed server mounts un-narrowed -- or a deny set that
    names a tool on a server the array, read a moment earlier, never mounted.

    Returning them from one call makes that structural rather than a convention. The
    alternative -- documenting that callers should thread a ``spec=`` through three
    functions -- is a rule a future caller can forget, and forgetting it is silent.
    The read's ANSWER is what is threaded, including "there is no spec": a helper
    handed ``None`` honours it rather than reading again (see :class:`_Unread`), so
    a spec that appears between this read and a helper's cannot be translated by
    one helper and ignored by another.

    Blocking (parses the spec and the global settings file once each), so callers
    run it off the event loop.
    """
    spec, snapshot = _agent_spec_and_snapshot_for(agent, work_dir) if agent else (None, None)
    settings = _global_settings()
    disabled_tools = session_mcp_disabled_tools(
        agent, work_dir=work_dir, spec=spec, settings=settings
    )
    return SessionMcpProjection(
        servers=session_mcp_servers(
            agent, stub_server_names=stub_server_names, work_dir=work_dir, spec=spec
        ),
        restricted=session_mcp_restricted_servers(disabled_tools),
        disabled_tools=disabled_tools,
        disabled_servers=session_mcp_disabled_servers(spec, settings),
        allowlist=_tools_allowlist(spec),
        derived_spec_snapshot=snapshot,
    )


def session_mcp_servers(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    work_dir: str | Path | None = None,
    spec: dict[str, Any] | None | _Unread = _UNREAD,
) -> list[dict[str, Any]]:
    """The ACP ``mcpServers`` array for a session running as *agent*.

    Called only for a backend in ``ACP_BACKENDS_SESSION_MCP_ARRAY``; every other
    harness reads the same spec itself and gets an empty array.

    *stub_server_names* are the servers that will ALSO arrive in this array as
    MCP-gateway broker stubs, which the caller appends after this list. A stub
    carries the SAME name as the agent-spec entry it wraps (it is a rewrite of
    that entry), so emitting both would put two elements with one ``name`` into
    a single array: either the raw entry shadows the stub and the session
    bypasses the broker, or both register and every pooled backend runs twice --
    the regression ``injection_server_names`` exists to detect. The KAS spec
    projection resolves the same set for the same reason; the caller owns the
    overlay, so it resolves the set and passes it down.

    ``spec`` lets a caller hand in a spec it has already parsed; see
    :func:`session_mcp_projection` for why a second parse is a consistency window
    rather than a cost.

    Blocking when it parses (so callers run it off the event loop). Deterministically
    ordered by server name, which keeps the array comparable across a session/new and
    the session/load that resumes it.
    """
    servers: dict[str, Any] = {}
    tools: Any = None
    if isinstance(spec, _Unread):
        spec = _agent_spec_for(agent, work_dir) if agent else None
    if spec is not None:
        raw = spec.get("mcpServers")
        if isinstance(raw, dict):
            servers = {str(k): v for k, v in raw.items()}
        tools = spec.get("tools")

    # kiro-cli's registry filter is SYMMETRIC (see ``agent._mcp_registry_mode``),
    # but only ONE half of it is reproducible here, and the asymmetry decides the
    # safe direction rather than being papered over:
    #
    # * OUTSIDE registry mode, kiro-cli drops the entries that CARRY the marker.
    #   That half is mirrored exactly: the marker is on the entry, the decision
    #   needs nothing else, and mirroring it is what keeps this backend from
    #   launching servers kiro-cli refuses.
    # * INSIDE registry mode, kiro-cli resolves each marked entry against the
    #   ADMIN'S CATALOG by map key, drops the ones the catalog does not list, and
    #   applies the catalog's own command override. None of that is available
    #   here: only kiro-cli fetches the registry URL, and it persists neither the
    #   URL nor the catalog, so nothing on disk can say whether a marked entry is
    #   authorized or whether its local command is the one the admin published.
    #   An entry that cannot be positively authorized is therefore WITHHELD, not
    #   launched -- a governed install must not have its policy decided by
    #   whichever harness the session happened to run on, and a local
    #   ``"type": "registry"`` marker is a line any user can add to a spec.
    #
    # In registry mode that leaves nothing from the spec, since the unmarked
    # entries are the ones kiro-cli drops. Crew's own control plane is re-added
    # below and is deliberately NOT subject to this: it is the host's own
    # process, re-derived from the managed source rather than read from the
    # user-editable spec, and withholding it would leave the session unable to
    # report back to its channel at all -- the exact defect this module exists to
    # fix. The residual difference from kiro-cli is stated for what it is: an
    # administrator who omits ``kirocrew-core`` from the catalog has it dropped
    # there and kept here, one host-owned server wider; every third-party server
    # goes the other way, withheld here and possibly mounted there.
    registry_mode = _registry_mode()
    for name, entry in list(servers.items()):
        marked = isinstance(entry, dict) and entry.get("type") == _KIRO_REGISTRY_TYPE
        if registry_mode:
            logger.info(
                "session MCP: withholding server %r -- registry mode is on and %s",
                name,
                (
                    "this backend cannot resolve the marker against the admin's catalog"
                    if marked
                    else "the entry carries no registry marker, so kiro-cli drops it too"
                ),
            )
            servers.pop(name)
        elif marked:
            logger.info(
                "session MCP: withholding server %r -- registry mode is off and the entry"
                " carries the registry marker, so kiro-cli drops it too",
                name,
            )
            servers.pop(name)

    for name in CONTROL_PLANE_SERVERS:
        managed = managed_mcp_spec_entry(name)
        if managed is not None:
            servers[name] = managed

    for name in stub_server_names:
        if servers.pop(str(name), None) is not None:
            logger.debug(
                "session MCP: yielding %r to its broker stub, which the caller appends", name
            )

    # A spec's ``tools`` is the allowlist, so once a spec EXISTS the filter always
    # runs -- a missing or non-list ``tools`` is an EMPTY allowlist, not "no
    # filter". The spec is hand-editable JSON, so `"tools": "@srv"` is an easy
    # mistake, and skipping the filter on it would mount every declared server,
    # including an ``opt_in`` one the user deliberately left unreferenced, the
    # moment the session happened to run on claude. Failing closed matches
    # kiro-cli, which mounts a server only when ``tools`` names it and so grants
    # nothing from a spec that references nothing; the warning is what keeps a
    # typo from being silent. The control plane is deliberately NOT exempt --
    # kiro-cli drops ``kirocrew-core`` from a spec whose ``tools`` stops naming
    # it, and this backend must not re-grant what kiro-cli would drop
    # (``test_a_spec_that_drops_the_reference_still_drops_the_server``); with no
    # spec at all there is no allowlist to apply and the control plane stands.
    if spec is not None:
        if tools is not None and not isinstance(tools, list):
            logger.warning(
                "session MCP: agent spec %r has a non-list 'tools' (%s); treating it as an"
                " empty allowlist, so this session mounts NO MCP server -- fix the spec",
                agent,
                type(tools).__name__,
            )
        allow = _tools_allowlist(spec)
        servers = {n: e for n, e in servers.items() if allow.grants(n)}

    out: list[dict[str, Any]] = []
    for name in sorted(servers):
        element = acp_server_element(name, servers[name])
        if element is not None:
            out.append(element)
    return out


def _managed_element_env(declared: Any) -> dict[str, str]:
    """One managed control plane's element ``env``, owned the way the disk path owns it.

    :func:`kiro_control_plane_servers` re-declares Crew's OWN servers, and a
    session-injected element outranks the spec's same-named entry at launch, so
    this ``env`` is the whole environment that shim receives. It therefore answers
    to the same rules ``agent._enforce_managed_mcp_ownership`` applies to the entry
    it writes for this same population, in the same order and no wider: a non-dict
    declaration is nothing, ``sanitize_spec_env`` drops Crew's reserved namespace
    and the loader channels, the home-deriving and launcher-exec classes are
    dropped, and Crew's own managed env is pinned last.

    Parity in BOTH directions is the property. Granting less would cost a user the
    ordinary variable they declared, for no reason but which backend the session
    happened to run on; granting more would let a spec that copies the managed
    command choose what Crew's own shim executes and which data home it reads, on
    the one element that also carries this session's identity token.

    A mirror rather than a shared helper: the disk consumer mutates a dict entry in
    place while this builds one array element. The key classes are read from
    ``agent`` rather than restated here, and the regression test derives its
    withheld set from those same frozensets, so the two cannot drift apart.
    """
    env = sanitize_spec_env(declared.items()) if isinstance(declared, dict) else {}
    for home_key in [k for k in env if k.upper() in _agent_mod._HOME_DERIVING_ENV_KEYS]:
        env.pop(home_key, None)
        logger.warning(
            "session MCP: dropping %r from a managed control plane's element env: it would"
            " move the data home this shim shares with the gateway",
            home_key,
        )
    for exec_key in [k for k in env if k.upper() in _agent_mod._LAUNCHER_EXEC_ENV_KEYS]:
        env.pop(exec_key, None)
        logger.warning(
            "session MCP: dropping %r from a managed control plane's element env: it would"
            " choose what this shim executes rather than configure it (see"
            " agent._LAUNCHER_EXEC_ENV_KEYS)",
            exec_key,
        )
    env.update(_agent_mod._managed_mcp_env())
    return env


def _declared_launch(source: Mapping[str, Any]) -> tuple[str, list[str]]:
    """``(command, args)`` as a spec or settings entry declares them, for comparison.

    A non-sequence ``args`` (``8080``, ``"--flag"``) reads as no args rather than
    being iterated: it cannot equal the managed launch either way, and raising
    here would abort ``session/new`` over a typo the module's own rules say must
    not raise (see :func:`acp_server_element`).
    """
    raw_args = source.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, (list, tuple)) else []
    return str(source.get("command", "") or ""), args


# The keys a native declaration of a managed server may carry and still be
# re-declared as a per-session element: the table the spec rebuild keeps on a
# managed entry (``agent._MANAGED_MCP_ENTRY_KEYS`` -- ``command``, ``args``,
# ``env``, ``type``, ``autoApprove``, ``timeout``, ``disabled``,
# ``disabledTools``), read from there so the two cannot drift, minus the one key
# in it that :func:`acp_server_element` does not carry: ``timeout``. The element
# REPLACES the declaration it is named after (an injected server wins over a
# same-named native one), so a ``timeout`` the user set there would run on the
# default with nothing to say so; the declaration stays native and the element
# is withheld, naming the key, the same way the KAS carriage leaves a ``timeout``
# customization in the agent block where that field is honoured. Anything else
# outside the table is a kiro-cli-only setting the element cannot express and is
# withheld for the same reason (see :func:`native_mount_withholding`). A key in
# the table is not by itself a restriction -- ``autoApprove`` narrows no tool
# surface -- only the values the arms below read.
_NATIVE_ELEMENT_KEYS: frozenset[str] = _agent_mod._MANAGED_MCP_ENTRY_KEYS - {"timeout"}

# Kiro Crew's OWN bookkeeping on an entry: the provenance marker the global-file
# sync stamps on an entry it authored, and the derived-field record the spec
# rebuild writes. Neither is a setting kiro-cli reads -- unknown keys are dropped
# at deserialization -- so neither restricts the server, and an element that
# omits them expresses everything the declaration says. Judging them as "a key
# the element cannot carry" would refuse the default agent for Crew's own stamp,
# with a message telling the user to remove it.
_NATIVE_INERT_KEYS = frozenset({mcp_provenance.MARKER_KEY, mcp_provenance.DERIVED_KEY})

# How the three places a managed server is declared are named to a user. These
# land in the message that refuses a projected search agent, so they name the
# FILE someone would open, not the variable that holds it.
NATIVE_SOURCE_SPEC = "the agent spec"
NATIVE_SOURCE_GLOBAL = "the global MCP settings"
NATIVE_SOURCE_PROJECT = "the project's MCP settings"

# The dashboard's MCP tab is the one writer of ``disabledTools`` and ``disabled``
# on Crew's own servers (``/api/mcp/toggle-tool`` and the server toggle, both
# into the global settings), so it is the recovery the message points at first.
_NATIVE_REMEDY_DASHBOARD = "in the dashboard's MCP tab"

# How many disabled tools a refusal names before it counts the rest: the
# message reaches a log line and an error dialog, and the list is user-authored.
_NATIVE_NAMED_TOOLS = 8

# How many characters of ONE user-authored value a refusal repeats -- a tool
# name, a key, a transport, the ``repr`` of a value that is not what it should
# be. The count bound above bounds how many names a message carries; this
# bounds each of them, and the two together bound the message, which the
# projection RETAINS per agent (``NativeSkillProjection.errors``) for the life
# of the runtime while the file it came from is read up to the reader's 50 MB
# ceiling. Tool names are identifiers of a few dozen characters; a value this
# long is not one, and the message says so by cutting it.
_NATIVE_NAMED_CHARS = 64

# How many characters of a settings file's PATH a refusal repeats. The path is
# the remedy -- it names the file the user opens -- so it is cut from the FRONT,
# keeping the tail that locates the file under its project. A project path is
# admitted, not typed, but its length is the filesystem's ceiling and not ours,
# and the string is retained per agent for the life of the runtime.
_NATIVE_NAMED_PATH_CHARS = 256

# How many characters of ONE value the cleaner READS before the message repeats
# its bounded head. The value comes out of a settings file -- the project's is
# checked out with the repository -- and the message reaches the gateway's log,
# so it goes through the package's one sink cleaner
# (:func:`~kiro_crew.acp.mcp_session_report.sanitize_sink_text`: redact, drop
# control characters, THEN cut), never raw: a newline or an escape sequence in
# a tool name would otherwise write its own log line. The cleaner runs over
# this window whole, so nothing the message repeats was cut before it was
# redacted; and the window, not the file, is what a value costs, because the
# redactors are regular expressions whose time grows faster than the text on
# some shapes (measured: 100,000 digits, 10.8 s) and the reader admits 50 MB.
_NATIVE_NAMED_WINDOW = 16 * _NATIVE_NAMED_CHARS


def _named_path(path: Path) -> str:
    """*path* as a message repeats it: its tail, within :data:`_NATIVE_NAMED_PATH_CHARS`.

    Cleaned like every other value the message carries: the path is the session's
    working directory as admitted, and a control character in it would reach the
    same log line.
    """
    text = str(path)
    text = sanitize_sink_text(text, len(text))
    if len(text) <= _NATIVE_NAMED_PATH_CHARS:
        return text
    return "..." + text[-(_NATIVE_NAMED_PATH_CHARS - 3) :]


class NativeSettingsSource(NamedTuple):
    """One settings file kiro-cli enforces beside the agent spec.

    ``servers`` is its ``mcpServers`` map, empty for an absent file -- only
    absence means "no restrictions"; a file that cannot be read safely never
    becomes a source (see :func:`native_settings_sources`).
    """

    label: str
    path: Path
    servers: Mapping[str, Any]

    @property
    def named(self) -> str:
        """The source as a refusal names it: the label and the file, bounded."""
        return f"{self.label} ({_named_path(self.path)})"


class NativeSettingsUnreadable(ValueError):
    """A settings file exists but could not be read safely.

    Raised by :func:`native_settings_sources` in place of the reader's own
    ``OSError``/``ValueError`` (kept as ``__cause__``) so a caller can name the
    file it could not read. It IS a ``ValueError``, so a caller that catches the
    reader's exceptions still catches this one.
    """

    def __init__(self, label: str, path: Path) -> None:
        super().__init__(f"{label} ({_named_path(path)}) could not be read safely")
        self.label = label
        self.path = path

    def explain(self, outcome: str) -> str:
        """One sentence for a user: why every native element is withheld."""
        return (
            f"{self.label} ({_named_path(self.path)}) could not be read safely, so no Kiro Crew"
            f" server can be mounted with this session's identity; repair or remove"
            f" that file to restore {outcome}"
        )


def native_settings_sources(work_dir: str | Path | None) -> list[NativeSettingsSource]:
    """The settings files kiro-cli enforces beside the agent spec, global first.

    This is the source set that decides whether a managed server's native
    declaration can be re-declared as a per-session element. Its one reader is
    :func:`kiro_control_plane_servers`, which mounts the element or not at each
    session start and hands its verdict on; the skill projection, which decides
    whether an agent may depend on the element, judges the agent spec alone and
    never reads these files, so no two readers can answer from different files
    or from the same file at different moments. The global file is the one the
    dashboard's tool toggle writes; the per-project file is read beside the
    session's working directory, the same directory the projection is prepared
    for and ``session/new`` receives as ``cwd``.

    Raises :class:`NativeSettingsUnreadable` when a file exists but fails the
    gated read; the caller then withholds EVERY element, because a restriction
    it cannot read must stay authoritative. An absent file contributes nothing.
    """
    reads: list[tuple[str, Path, Any]] = [
        (NATIVE_SOURCE_GLOBAL, _agent_mod._KIRO_MCP_JSON, lambda _p: _global_settings(strict=True))
    ]
    if work_dir:
        project_path = Path(work_dir) / ".kiro" / "settings" / "mcp.json"
        reads.append((NATIVE_SOURCE_PROJECT, project_path, _read_mcp_settings))
    sources: list[NativeSettingsSource] = []
    for label, path, read in reads:
        try:
            settings = read(path)
        except (OSError, ValueError) as exc:
            raise NativeSettingsUnreadable(label, path) from exc
        declared = settings.get("mcpServers", {}) if isinstance(settings, dict) else {}
        sources.append(
            NativeSettingsSource(label, path, declared if isinstance(declared, dict) else {})
        )
    return sources


def native_declarations(
    name: str, spec_entry: Any, settings: Collection[NativeSettingsSource]
) -> list[tuple[str, Any]]:
    """Every declaration of *name* a session is subject to, as ``(source, entry)``.

    The spec's own entry first, then each settings file that names the server,
    in the order :func:`native_settings_sources` returns them. The entries are
    returned as declared -- a settings file is hand-editable JSON, so one may not
    even be an object -- and the caller decides what a malformed one means.
    """
    declarations: list[tuple[str, Any]] = [(NATIVE_SOURCE_SPEC, spec_entry)]
    for source in settings:
        if name in source.servers:
            declarations.append((source.named, source.servers[name]))
    return declarations


class NativeMountWithholding(NamedTuple):
    """Why a managed server's per-session element is withheld, named for a user.

    ``source`` is the declaration that carries the restriction (one of the
    ``NATIVE_SOURCE_*`` labels, with the file's path for a settings file),
    ``restriction`` what it says, ``remedy`` what removes it. :meth:`explain`
    renders the sentence both readers of the predicate hand on: the projection
    as the reason a search agent was refused, the element writer as its log line.
    """

    server: str
    source: str
    restriction: str
    remedy: str

    def explain(self, outcome: str) -> str:
        return (
            f"{self.server} is withheld by {self.source}: {self.restriction};"
            f" {self.remedy} to restore {outcome}"
        )


class NativeControlPlaneMount(NamedTuple):
    """What ONE read of the sources yields for a session's identity-bound servers.

    ``elements`` is the per-session ``mcpServers`` array
    :func:`kiro_control_plane_servers` produced. ``withheld`` names, for each
    identity-bound server the spec grants whose element it did NOT produce
    because a declaration keeps the server native, that declaration's
    :class:`NativeMountWithholding` -- or, when a settings file could not be read
    safely, the :class:`NativeSettingsUnreadable` that withheld every element. A
    server in neither was not granted, not declared, or already in the array
    with the files allowing it. Both come from the one read of the settings
    files the mount performs, so a caller that refuses a session on ``withheld``
    refuses it for the reason the array was built on, never for what the files
    say a moment later.
    """

    elements: list[dict[str, Any]]
    withheld: dict[str, NativeMountWithholding | NativeSettingsUnreadable]


def _named(value: Any) -> str:
    """*value* as a message repeats it: at most :data:`_NATIVE_NAMED_CHARS` characters.

    Every user-authored field a refusal interpolates goes through here, so the
    bound is one bound and a field added later cannot miss it by being spelled
    differently. The text is cleaned before it is cut -- the first
    :data:`_NATIVE_NAMED_WINDOW` characters go through the sink cleaner whole, so
    a credential is redacted before a cut could split it and a control character
    never reaches the message -- and a cut value ends in ``...`` inside the bound,
    never past it. A value the window did not hold whole is a cut value too.
    """
    text = value if isinstance(value, str) else repr(value)
    window = text[:_NATIVE_NAMED_WINDOW]
    clean = sanitize_sink_text(window, len(window))
    if len(text) <= _NATIVE_NAMED_WINDOW and len(clean) <= _NATIVE_NAMED_CHARS:
        return clean
    return clean[: _NATIVE_NAMED_CHARS - 3] + "..."


def _named_some(values: list[Any]) -> str:
    """The first :data:`_NATIVE_NAMED_TOOLS` of *values*, each bounded, then a count."""
    named = ", ".join(_named(value) for value in values[:_NATIVE_NAMED_TOOLS])
    if len(values) > _NATIVE_NAMED_TOOLS:
        named += f" and {len(values) - _NATIVE_NAMED_TOOLS} more"
    return named


def _named_tools(value: Any) -> str:
    """Render a ``disabledTools`` value for a message, bounded and typed."""
    if not isinstance(value, list) or not all(isinstance(tool, str) for tool in value):
        return f"is {_named(repr(value))}, not a list of tool names"
    return f"lists {_named_some(value)}"


def _native_restriction(
    declared: Any, *, dashboard_writes: bool = False, spec_declaration: bool = False
) -> tuple[str, str] | None:
    """``(restriction, remedy)`` when one declaration keeps its server native.

    The order is the order the questions are cheapest to answer and matters only
    for which reason a declaration with several is named for. The two flags say
    which declaration this is, because two answers depend on it. ``dashboard_writes``:
    the declaration lives in the file the dashboard's MCP tab writes -- the global
    settings -- so a mute or a ``disabledTools`` there is offered the tab as the
    way back; a spec or project entry is not, since following that remedy would
    leave the restriction where it is and refuse the next session again.
    ``spec_declaration``: this is the agent spec's own managed entry, the one
    place the spec rebuild stamps and un-stamps the ``registry`` marker, so the
    marker is Crew's own there and restricts nothing.
    """
    if not isinstance(declared, dict):
        return ("its entry is not a server object", "declare it as an object there")
    extra = sorted(str(key) for key in set(declared) - _NATIVE_ELEMENT_KEYS - _NATIVE_INERT_KEYS)
    if extra:
        return (
            f"its entry carries {_named_some(extra)}, which a per-session element cannot"
            " express",
            "remove that setting from the entry there",
        )
    transport = declared.get("type", "stdio")
    # ``registry`` on the SPEC's managed entry is Crew's OWN marker, not a
    # transport a user chose: the spec rebuild stamps it there on every managed
    # entry while ``agent.mcp_registry_mode`` is on and removes it when the mode
    # is off (``_enforce_managed_mcp_ownership``). The mount answers registry mode
    # before it reaches this predicate, so the value restricts nothing on the
    # spec; reading it as "not stdio" would refuse the default agent on every
    # enterprise-governed install and tell the operator to delete a marker the
    # next rebuild re-stamps. In a settings file the same value is a declaration
    # a user or an admin wrote -- no Crew writer puts it there -- and outside
    # registry mode kiro-cli DROPS a marked entry, so mounting the element for it
    # would launch a server kiro-cli refuses: the declaration stays native.
    transports = ("stdio", _KIRO_REGISTRY_TYPE) if spec_declaration else ("stdio",)
    if transport not in transports:
        return (
            f"its type is {_named(repr(transport))}, not stdio",
            "declare it as a stdio server there",
        )
    if mcp_entry_is_muted(declared):
        return (
            "it is disabled",
            _remedy("re-enable the server", "set disabled to false there", tab=dashboard_writes),
        )
    disabled_tools = declared.get("disabledTools", [])
    if disabled_tools != []:
        return (
            f"its disabledTools {_named_tools(disabled_tools)}",
            _remedy(
                "re-enable those tools",
                "remove the disabledTools entry there",
                tab=dashboard_writes,
            ),
        )
    return None


def _remedy(action: str, edit: str, *, tab: bool) -> str:
    """The way back, one grammatical sentence whichever file holds the restriction.

    Where the dashboard's MCP tab writes the file, the tab is offered first and the
    hand edit is the alternative, joined by the ``or`` that two options need.
    Where it does not, the edit stands alone: *action* would only restate what the
    edit does, and a connective belongs to the arm that has two clauses to join.
    """
    if tab:
        return f"{action} {_NATIVE_REMEDY_DASHBOARD}, or {edit}"
    return edit


def native_mount_withholding(
    name: str, spec_entry: Any, settings: Collection[NativeSettingsSource]
) -> NativeMountWithholding | None:
    """Whether *name*'s per-session element must be withheld, and why.

    THE predicate for "does a native restriction keep this managed server out of
    the per-session ``mcpServers`` array": ``None`` means the element may be
    mounted, anything else names the declaration and the restriction. A
    per-session element cannot carry a kiro-cli-only setting -- a mute, a
    ``disabledTools`` list, a non-stdio transport, any key outside
    :data:`_NATIVE_ELEMENT_KEYS` -- and dropping one would widen the session's
    tool surface behind the user's back, so the declaration that carries it
    stays native and the element is withheld. Crew's own marks on an entry are
    not settings and restrict nothing: the ``registry`` transport value, the
    provenance marker and the derived-field record (:data:`_NATIVE_INERT_KEYS`).
    That is the same answer for every
    declaration the session is subject to, the spec's and each settings file's
    (:func:`native_declarations`).

    Two readers ask this question and MUST reach the same answer for the same
    declaration: :func:`kiro_control_plane_servers`, which mounts the element or
    not and hands its verdict on (:class:`NativeControlPlaneMount`), and the
    skill projection, which decides whether an agent may rely on the element
    being there. They split the sources by what each one is. The projection
    judges the agent spec's entry at preparation, over no settings source, since
    a restriction authored there is static for the view's life; the mount is the
    ONE reader of the settings files, at each session start, and a runtime that
    refuses a session for what a file says refuses it on the mount's verdict
    rather than on a read of its own. A projection judging the spec by a rule
    of its own would promise an element the mount then withholds, and
    ``session/new`` would fail with an error pointing at the wrong file; so both
    call this one function, and neither keeps a copy of the merge or of the
    predicate.
    """
    dashboard_writes = {s.named for s in settings if s.label == NATIVE_SOURCE_GLOBAL}
    for source, declared in native_declarations(name, spec_entry, settings):
        verdict = _native_restriction(
            declared,
            dashboard_writes=source in dashboard_writes,
            spec_declaration=source == NATIVE_SOURCE_SPEC,
        )
        if verdict is not None:
            restriction, remedy = verdict
            return NativeMountWithholding(name, source, restriction, remedy)
    return None


def kiro_control_plane_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None,
    existing_names: Collection[str] = (),
    spec_override: dict[str, Any] | None = None,
) -> NativeControlPlaneMount:
    """Carry ordinary session identity without widening Kiro's native tool surface.

    Only an existing managed stdio declaration can be overridden. Native-only
    restrictions stay in the native declaration instead of being discarded by
    ACP shaping. Registry entries remain the enterprise catalog's responsibility.
    The element's ``env`` is owned by :func:`_managed_element_env`, which holds it to
    the rule the disk-writing consumer applies to this same population.

    Covers every server in :data:`IDENTITY_BOUND_SERVERS` the spec grants, not
    only the control plane: an opt-in server such as ``kirocrew-dashboard`` is
    mounted by kiro-cli straight from the spec with no session-valued environment,
    so this element is the ONLY way its calls can carry the attestation the
    gateway's tool-policy read demands. The grant itself is still the spec's --
    ``allow.grants`` -- and the expected invocation is resolved with
    :func:`~kiro_crew.agent.managed_mcp_spec_entry` under ``include_opt_in=True``,
    the same form ``mcp_gateway.gatewayd`` reads: it answers for a granted opt-in
    server where the writers' emission question would refuse to mint one, and
    still yields ``None`` behind a closed ``spec_gate``.

    Returns the array AND the verdicts it withheld on
    (:class:`NativeControlPlaneMount`), both from this call's one read of the
    settings files. The verdict is reached before the array is consulted: a name
    a broker stub already carries gets no element either way, but the
    restriction a declaration puts on it is what the session-start guard has to
    know -- a stub carries a kiro-cli-only restriction no better than the
    element does -- and it has to come from this read. A second read for it
    would be the window in which a toggle undone in between answers ``None``
    and the session falls back to a guard that names the wrong file.
    """
    if not agent or _registry_mode():
        return NativeControlPlaneMount([], {})
    spec = spec_override if spec_override is not None else _agent_spec_for(agent, work_dir)
    if not isinstance(spec, dict) or not isinstance(spec.get("mcpServers"), dict):
        return NativeControlPlaneMount([], {})
    allow = _tools_allowlist(spec)
    try:
        settings = native_settings_sources(work_dir)
    except NativeSettingsUnreadable as exc:
        logger.debug("session MCP: withholding Kiro overrides: %s", exc)
        return NativeControlPlaneMount([], {name: exc for name in IDENTITY_BOUND_SERVERS})
    out: list[dict[str, Any]] = []
    withheld_by_name: dict[str, NativeMountWithholding | NativeSettingsUnreadable] = {}
    for name in IDENTITY_BOUND_SERVERS:
        if not allow.grants(name):
            continue
        entry = spec["mcpServers"].get(name)
        managed = managed_mcp_spec_entry(name, include_opt_in=True)
        if not isinstance(entry, dict) or not isinstance(managed, dict):
            continue
        withheld = native_mount_withholding(name, entry, settings)
        if withheld is not None:
            logger.debug("session MCP: %s", withheld.explain("its per-session element"))
            withheld_by_name[name] = withheld
            continue
        if name in existing_names:
            continue
        # The launch is the managed source's, never the spec's. A hand-authored
        # command for a reserved name cannot be right across users or upgrades
        # (the managed path carries the home and the installed version; the one
        # writable spelling, bare ``kirocrew``, resolves to the Toolbox
        # dispatcher), and skipping such an entry cost the session every
        # control-plane tool while the server looked mounted. The
        # module docstring already states the rule -- "re-derived from the
        # managed source of truth on every spawn so a stale hand-edited command
        # in the spec cannot cost a claude session the tools it needs" -- and the
        # codex/opencode projections REPLACE the same way; this applies it here.
        # Restrictions (mute, ``disabledTools``, non-stdio) still withhold above:
        # they narrow the grant, which stays the spec's; only the invocation is
        # ours. Only a sequence is iterated, as in ``acp_server_element``: the
        # spec is hand-editable JSON, so ``"args": 8080`` is an easy thing to
        # write, and it must read as "not the managed launch", not abort
        # ``session/new`` with a TypeError from inside a comprehension.
        declared = [
            _declared_launch(source)
            for _source_label, source in native_declarations(name, entry, settings)
            if isinstance(source, dict) and ("command" in source or "args" in source)
        ]
        managed_launch = _declared_launch(managed)
        if any(launch != managed_launch for launch in declared):
            logger.warning(
                "session MCP: agent %r declares reserved server %r with a command that is"
                " not the managed invocation; mounting the managed one so the gateway can"
                " attest it",
                agent,
                name,
            )
        owned = {
            **entry,
            "command": managed_launch[0],
            "args": list(managed_launch[1]),
            "env": _managed_element_env(entry.get("env")),
        }
        element = acp_server_element(name, owned)
        if element is not None:
            out.append(element)
    return NativeControlPlaneMount(out, withheld_by_name)
