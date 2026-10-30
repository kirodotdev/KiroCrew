"""Which tool a ``tool_call`` frame names, read from what the adapter wrote, not the model.

The ACP shell-kind test, :func:`classify_tool_call`'s transport-aware
:class:`ToolCallIdentity` verdict, the bounded harness tool identifiers a permission
request and a tool row carry, and the table of harness-authored ``_meta`` identity
channels with the readers over it.

:mod:`kiro_crew.acp._dispatch` stays the import path and the patch surface; see
:mod:`kiro_crew.acp._dispatch_parts`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from kiro_crew.acp.harness_tool_names import MAX_HARNESS_TOOL_NAME_LEN
from kiro_crew.acp_backends import ACP_BACKEND_ANTIGRAVITY
from kiro_crew.mcp_gateway.tool_surface import mcp_identity_name, mcp_identity_unreadable
from kiro_crew.platform.tool_paths import _TARGET_PATH_MAX_NODES

_ACP_SHELL_KIND = "execute"


def is_shell_kind(kind: str | None) -> bool:
    """True when an ACP tool_kind denotes a shell/exec command.

    Kind-only view. A frame's ``kind`` is not sufficient on its own to call a
    tool call a shell command -- codex-acp reports MCP tool calls with the same
    ``execute`` kind as its shell tool -- so callers holding the whole update
    must use :func:`classify_tool_call`, which consults the adapter-authored
    MCP markers first. This helper remains for callers that only have a kind
    (the pill-title rule's fallback).
    """
    return kind == _ACP_SHELL_KIND


#: codex-acp marks the ``tool_call`` frame of an MCP call with this ``_meta``
#: key (``createMcpToolCallUpdate``). The adapter writes ``_meta``; the model
#: authors only ``rawInput.arguments`` and, for a shell tool, ``command``.
_CODEX_MCP_TOOL_CALL_MARKER = "is_mcp_tool_call"


@dataclass(frozen=True)
class ToolCallIdentity:
    """What one ``tool_call``/``tool_call_update`` frame says about its tool.

    ``kind_resolved`` is whether the frame carried a usable ``kind`` at all: a
    refinement that omits ``kind`` must not clobber a classification cached from
    the initial frame. ``is_shell`` is the provider-neutral shell signal.
    ``mcp_server_name``/``tool_name`` are the adapter-resolved identity, empty
    when the frame names none; ``identity_trusted`` is True only when BOTH came
    from an adapter-authored source, mirroring ``AcpEvent.mcp_identity_trusted``.
    """

    kind_resolved: bool
    is_shell: bool
    mcp_server_name: str
    tool_name: str
    identity_trusted: bool
    #: The frame PRESENTED an identity half the MCP-name bound refused
    #: (``tool_surface.mcp_identity_unreadable``): it names a tool Crew cannot
    #: retain, which is not the same as naming none. Carried to the permission
    #: path so the request is refused rather than judged under no identity.
    identity_unreadable: bool = False

    @property
    def tool_identity_trusted(self) -> bool:
        """Whether ``tool_name`` came from an adapter-authored identity channel.

        ``classify_tool_call`` never populates ``tool_name`` from display titles
        or inline permission payloads, so non-emptiness is provenance at this
        classifier boundary. MCP pair provenance remains ``identity_trusted``.
        """
        return bool(self.tool_name)


def _str_field(mapping: object, key: str) -> str:
    if not isinstance(mapping, dict):
        return ""
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def classify_tool_call(update: dict[str, Any]) -> ToolCallIdentity:
    """Classify a tool-call frame by its transport, not by its ``kind`` alone.

    Adapters disagree on what ``kind`` an MCP-served tool call carries:
    kiro-cli reports the tool's own kind and names the server in
    ``_meta.kiro.mcpServerName``; goose names it in
    ``_meta.goose.toolCall.extensionName`` and sends its builtin shell with no
    ``kind`` at all; codex-acp reuses its shell builder, so an MCP
    call arrives as ``kind="execute"`` with ``rawInput={server, tool,
    arguments}`` and a ``_meta.is_mcp_tool_call`` marker; claude-agent-acp and
    opencode send ``kind="other"`` with no identity. Reading ``kind`` alone
    therefore calls a codex MCP call a shell command, and because its params
    carry no ``command`` the deny-by-default gate in ``HookManager.on_tool_call``
    refuses every such call ("shell command could not be verified").

    The two markers carry different weight against the kind. kiro-cli chooses
    the kind per tool, so its ``execute`` means a shell tool even when the
    frame also names a server: the identity is carried but never waives the
    shell verdict (``AcpEvent.child_mcp_identity_trusted`` pins that). codex-acp
    does not choose: every MCP call goes through its shell builder, so on a
    frame carrying ``_meta.is_mcp_tool_call`` the ``execute`` kind says nothing
    about the tool and the marker decides -- a codex shell call never carries
    it. Both markers live under ``_meta``, which the adapter writes and the
    model cannot reach, and the codex ``server``/``tool`` pair is the adapter's
    own resolution of what runs (``createMcpRawInput``), never model text --
    the same pair ``client._identified_mcp_call`` already relies on. A codex
    marker with an UNREADABLE pair is a malformed frame: it is not shell
    (there are no command bytes to verify) but it resolves NOTHING --
    ``kind_resolved`` is False so the shell cache stays unwritten, the later
    permission event reads an absent classification (``shell_classified``
    False, ``child_low_fidelity`` True) and every fidelity-gated auto-approve
    stays closed. Minting a resolved non-shell verdict there would re-open the
    title-keyed grant paths with no verified identity behind them. A frame
    with no marker and no ``execute`` kind is neither shell nor MCP.
    """
    kind = update.get("kind")
    kind_resolved = isinstance(kind, str) and bool(kind)
    shell_by_kind = is_shell_kind(kind if isinstance(kind, str) else None)
    meta = update.get("_meta")
    meta = meta if isinstance(meta, dict) else {}

    # Harness ``_meta`` identity channels (kiro-cli's ``_meta.kiro``, goose's
    # ``_meta.goose.toolCall``; see _MCP_IDENTITY_META_CHANNELS). A server is set
    # ONLY for MCP-served calls, a tool name for every tool (built-ins included).
    # The kind is the harness's own verdict on the tool, so it stands.
    meta_server, meta_tool, meta_shell, meta_unreadable = _meta_identity_full(update)
    if meta_shell:
        # The harness named its OWN shell (goose: developer/shell) and sends no
        # ``kind`` on that branch. Harness-authored, so it RESOLVES the call:
        # it is a command, and no MCP server served it.
        return ToolCallIdentity(True, True, "", meta_tool, False)
    if meta_server:
        return ToolCallIdentity(
            kind_resolved,
            shell_by_kind,
            meta_server,
            meta_tool,
            bool(meta_tool),
            identity_unreadable=meta_unreadable,
        )

    # codex-acp: adapter marker plus the adapter-resolved (server, tool) pair.
    # Not for a frame a bounded channel already reported unreadable: Antigravity
    # stamps the same marker beside its ``_meta.mcp``, and a server half over the
    # bound must reach the refusal below rather than be replaced by a pair read
    # from ``rawInput``, where Antigravity puts the tool's own arguments.
    if meta.get(_CODEX_MCP_TOOL_CALL_MARKER) is True and not meta_unreadable:
        raw = update.get("rawInput")
        # A first-class harness's pair, read as it always was (H13): the MCP-name
        # bound and the unreadable mark belong to the bounded channels only.
        server = _str_field(raw, "server")
        tool = _str_field(raw, "tool")
        if server and tool:
            return ToolCallIdentity(kind_resolved, False, server, tool, True)
        # Marker without a readable pair: malformed. Resolve nothing, so the
        # shell cache is left unwritten and the permission path fails closed
        # to low fidelity instead of a minted non-shell verdict.
        return ToolCallIdentity(False, False, "", "", False)

    # No MCP marker: the kind decides. The harness's built-in tool name (no
    # server) is still carried so hooks can match a host-known built-in.
    return ToolCallIdentity(
        kind_resolved, shell_by_kind, "", meta_tool, False, identity_unreadable=meta_unreadable
    )


#: Bound on a harness tool id. KAS's are short snake_case names; anything longer
#: is not one and is dropped rather than cut.
_MAX_HARNESS_TOOL_ID_LEN = 128

_HARNESS_TOOL_ID_RE = re.compile(r"[A-Za-z0-9_.\-/@:]+")


def _harness_identifier(value: object) -> str:
    """*value* as a harness-authored identifier, or "" when absent or malformed.

    The bound on a BUILT-IN harness tool id -- the permission frame's
    ``_meta.kiro.toolId``, a KAS engine name such as ``fs_read`` -- applied at
    the point the value is retained, so it can never reach ``AcpEvent.tool_name``
    (and from there the deny tier, governance, the read-only proof, the audit
    log) longer than ``_MAX_HARNESS_TOOL_ID_LEN`` or spelled outside
    ``_HARNESS_TOOL_ID_RE``: word characters, ``.``, ``-`` and the ``/``, ``@``
    and ``:`` separators. No glob metacharacter or whitespace survives, so the
    value is matched as a name and logged unescaped. An MCP server or tool name
    is NOT bounded here: it is the server's string, admitted up to 512
    characters in any spelling by Crew's own tool surface, and a per-tool deny
    binds to it exactly, so the identity channels retain it under
    :func:`kiro_crew.mcp_gateway.tool_surface.mcp_identity_name` instead --
    emptying a valid 129-character name here would drop the deny target and
    hand the call to the title-keyed grant loop.
    """
    if (
        not isinstance(value, str)
        or len(value) > _MAX_HARNESS_TOOL_ID_LEN
        or not _HARNESS_TOOL_ID_RE.fullmatch(value)
    ):
        return ""
    return value


def _permission_tool_id(params: dict[str, Any]) -> str:
    """``_meta.kiro.toolId`` of a permission request, or "" when absent or malformed.

    Bounded by :func:`_harness_identifier`.
    """
    meta = params.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    tool_id = kiro.get("toolId") if isinstance(kiro, dict) else None
    return _harness_identifier(tool_id)


def harness_tool_name(update: dict[str, Any], *, backend: str = "") -> str:
    """The tool's own name as a ``tool_call`` frame states it, or "".

    goose writes it in ``_meta.goose.toolCall.toolName`` and claude-agent-acp in
    ``_meta.claudeCode.toolName``; opencode writes it as the first frame's
    ``title`` (``bash``), where later updates carry the command. On *backend*
    Antigravity, a native non-execution tool name is wrapped as ``Run <name>?``;
    that spelling is unwrapped only there. Read from the first frame only, and
    only as an identifier: a title with whitespace or any other prose character
    is not a name and yields "". Which backend's table the name is looked up in
    is decided by the permission event's caller
    (:func:`kiro_crew.acp.harness_tool_names.qualified_harness_tool_id`), so the
    title of a backend with no table is never read as a name.
    """
    meta = update.get("_meta")
    goose = meta.get("goose") if isinstance(meta, dict) else None
    call = goose.get("toolCall") if isinstance(goose, dict) else None
    name = call.get("toolName") if isinstance(call, dict) else None
    claude = meta.get("claudeCode") if isinstance(meta, dict) else None
    if name is None and isinstance(claude, dict):
        # claude-agent-acp builds its title from the call's arguments (a search
        # query, a path), so only its own stamp may name the tool.
        name = claude.get("toolName") or ""
    if name is None:
        name = update.get("title")
        if (
            backend == ACP_BACKEND_ANTIGRAVITY
            and isinstance(name, str)
            and name.startswith("Run ")
            and name.endswith("?")
        ):
            name = name[4:-1]
    # Bounded by MAX_HARNESS_TOOL_NAME_LEN, not KAS's 128: an opencode MCP title
    # fuses an operator-chosen server name with the tool's, and one dropped here
    # leaves a deny hook matching the title alone.
    if (
        not isinstance(name, str)
        or len(name) > MAX_HARNESS_TOOL_NAME_LEN
        or not _HARNESS_TOOL_ID_RE.fullmatch(name)
    ):
        return ""
    return name


#: How Antigravity's tools spell the command and the files they act on. Every key
#: has a PascalCase form and a snake_case twin. In the Windows archive the names are
#: strings in the helper ``localharness_external.exe`` only (none is a whole token
#: in ``agy_acp_server.exe``, and ``search_path`` is not one in the helper either);
#: on macOS and Linux they are in both executables. The counts are the same in
#: 1.4.4 and 1.4.5. They have not yet been seen on the wire. The shared readers
#: look for neither: they read ``command`` and the ``TARGET_PATH_KEYS`` spellings
#: (``path``, ``file_path``, ``filePath``, ``targetFile``). The runtime defines
#: more argument names than these, so the lists are not trusted to be complete:
#: any argument NAMED like a path or a command is read too (below).
_ANTIGRAVITY_COMMAND_KEYS = ("CommandLine", "command_line")
_ANTIGRAVITY_PATH_KEYS = (
    "AbsolutePath",
    "absolute_path",
    "TargetFile",
    "target_file",
    "SearchPath",
    "search_path",
    "DirectoryPath",
    "directory_path",
    "SearchDirectory",
    "search_directory",
)
#: Parts of an argument name (lower-cased, ``_`` and ``-`` removed) that mark its
#: value as a file target, or as a command. Over-reading is the safe direction: the
#: path check refuses on any hit and a value that is no path matches nothing, and a
#: second command can only make a shell call unverifiable. ``target`` is not a part:
#: ``TargetFile`` is listed above, and the edit tool's ``TargetContent`` is a body.
_ANTIGRAVITY_PATH_NAME_PARTS = (
    "path",
    "file",
    "dir",
    "folder",
    "uri",
    "url",
    "cwd",
    "workspace",
    "location",
    "glob",
    "include",
    "exclude",
)
_ANTIGRAVITY_COMMAND_NAME_PARTS = ("command", "cmd")


def _antigravity_name_has(key: object, parts: tuple[str, ...]) -> bool:
    plain = key.lower().replace("_", "").replace("-", "") if isinstance(key, str) else ""
    return any(part in plain for part in parts)


_ANTIGRAVITY_KNOWN_COMMAND_KEYS = (*_ANTIGRAVITY_COMMAND_KEYS, "command")


def _antigravity_stated_commands(params: dict[str, Any]) -> list[Any]:
    """Every non-empty command spelling *params* state, ``command`` included."""
    return [params[k] for k in _ANTIGRAVITY_KNOWN_COMMAND_KEYS if params.get(k) not in (None, "")]


def _antigravity_other_commands(params: dict[str, Any]) -> list[Any]:
    """Every other argument named like a command holding a string, a list or a mapping.

    These only take part in the agreement check and never supply ``command``: a list
    or mapping is no one command string, and a lone one leaves the call unverifiable.
    """
    return [
        value
        for key, value in params.items()
        if key not in _ANTIGRAVITY_KNOWN_COMMAND_KEYS
        and _antigravity_name_has(key, _ANTIGRAVITY_COMMAND_NAME_PARTS)
        and ((isinstance(value, str) and value) or isinstance(value, (list, dict)))
    ]


def _antigravity_file_uri_paths(uri: str) -> list[str]:
    """The local paths a ``file:`` URI may name, once and twice percent-decoded.

    Never raises: a URI the platform decoder rejects yields its percent-decoded
    path instead, which the path check still reads.
    """
    try:
        path = urlsplit(uri).path
    except ValueError:
        rest = uri[5:].lstrip("/")
        path = "/" + rest
    found = []
    for candidate in (path, unquote(path)):
        try:
            found.append(url2pathname(candidate))
        except (OSError, ValueError):
            found.append(unquote(candidate))
    return found


def _antigravity_named_paths(params: dict[str, Any]) -> list[str]:
    """Every string under an argument named like a path, at any depth.

    Everything under such a name counts, a nested list or mapping included. A
    ``file:`` value also yields the local paths it may name. The walk stops where
    ``target_paths`` does; a call that large is refused there as unverifiable,
    since the copy it walks is no smaller.
    """
    found: dict[str, None] = {}
    stack: list[tuple[object, bool]] = [(params, False)]
    nodes = 0
    while stack and nodes < _TARGET_PATH_MAX_NODES:
        node, named = stack.pop()
        nodes += 1
        if isinstance(node, str):
            if named and node.strip():
                found[node] = None
                if node[:5].lower() == "file:":
                    found.update(dict.fromkeys(_antigravity_file_uri_paths(node)))
        elif isinstance(node, dict):
            stack.extend(
                (v, named or _antigravity_name_has(k, _ANTIGRAVITY_PATH_NAME_PARTS))
                for k, v in reversed(node.items())
            )
        elif isinstance(node, list):
            stack.extend((v, named) for v in reversed(node))
    return [path for path in found if path.strip()]


def antigravity_command_conflict(params: object) -> bool:
    """True when *params* state a command and its spellings are not one string.

    Such a call has no command the gate can verify, whichever spelling runs.
    """
    if not isinstance(params, dict):
        return False
    stated = [*_antigravity_stated_commands(params), *_antigravity_other_commands(params)]
    return bool(stated) and not (all(isinstance(v, str) for v in stated) and len(set(stated)) == 1)


def antigravity_gate_params(params: dict[str, Any] | None, *, shell: bool) -> dict[str, Any] | None:
    """*params* with Antigravity's argument names also under the names the gate reads.

    Without this a shell call carries no ``command`` and the unverifiable-shell tier
    refuses it, and a file tool carries no path the sensitive-path check can see.
    Returns a copy; the cached trusted params are never edited. The spellings were
    read from the pinned runtime's strings, not from a signed-in session.

    ``command`` is set only on a shell call (*shell*), and only when every spelling
    present, an existing ``command`` included, states the same string. Any
    disagreement removes ``command``, so the call stays unverifiable and is refused.
    Any other argument named like a command takes part in that agreement but never
    supplies ``command``.
    Each path-spelling value, and every string under any other argument named like
    a path, is added to ``path``. A lone value leaves ``path`` a
    string, so a rule that reads a string ``path`` still sees it.
    """
    if not isinstance(params, dict):
        return params
    out = dict(params)
    if shell:
        if antigravity_command_conflict(params):
            out.pop("command", None)
        else:
            stated = _antigravity_stated_commands(params)
            if stated:
                out["command"] = stated[0]
    targets = [params[k] for k in _ANTIGRAVITY_PATH_KEYS if params.get(k) not in (None, "")]
    listed = {t for t in targets if isinstance(t, str)}
    targets.extend(p for p in _antigravity_named_paths(params) if p not in listed)
    if targets:
        prior = out.get("path")
        kept = prior if isinstance(prior, list) else ([prior] if prior not in (None, "") else [])
        kept_strs = {k for k in kept if isinstance(k, str)}
        merged = [
            *kept,
            *(t for t in targets if (t not in kept_strs if isinstance(t, str) else t not in kept)),
        ]
        out["path"] = merged if isinstance(prior, list) or len(merged) > 1 else merged[0]
    return out


class _MetaIdentityChannel(NamedTuple):
    """Where one harness puts the ``(server, tool)`` identity of a tool call.

    A TABLE rather than a per-backend branch, because this is a property of the
    harness's wire format and not of Crew's control flow: a reader consults every
    channel and takes the first that answers, so a new harness is one row and no call
    site learns its name.

    ``nested`` is the sub-object the fields sit under, or ``None`` when they sit
    directly on the harness's own ``_meta`` block.

    ``strips_server_prefix`` records a harness that spells its tool name as
    ``<server><sep><tool>``. The deny set and the canonical ``mcp__<server>__<tool>``
    name both spell the BARE tool, so the prefix is removed here rather than at each
    comparison -- one place where the two spellings are reconciled.
    """

    key: str
    server_field: str
    tool_field: str
    nested: str | None = None
    strips_server_prefix: str = ""
    #: The ``(server, tool)`` pair, as this channel spells it, that names the harness's
    #: OWN shell rather than a served MCP tool. A harness that models its builtins as
    #: extensions reports one under the same fields an MCP call uses, so without this the
    #: server field reads as "an MCP server served this" on a shell command. Matching it
    #: means two things at once: the call is a shell command, and it has no MCP server.
    builtin_shell: tuple[str, str] | None = None
    #: Whether this channel's halves are bounded at retention by the MCP-name
    #: bound Crew's own tool surface admits (``mcp_identity_name``), with a
    #: PRESENT half over it reported as unreadable. True for the KAS spelling,
    #: whose identity caches sit under the KAS-only admission, and for
    #: Antigravity's ``_meta.mcp``, whose client keeps the unreadable mark and
    #: refuses on it (``client_tool_events``, ``client_permissions``): on each, an
    #: unreadable half earns that harness's refusal. A first-class harness's
    #: channel reads as it always did -- a string is retained verbatim -- so no
    #: first-class path gains a bound or a refusal (H13).
    bounds_names: bool = False
    #: Whether this row answers only for a block that carries its own
    #: ``server_field``. True for the KAS row only, which shares ``toolName``
    #: with the kiro-cli row below it (see the table). A row with a field set of
    #: its own answers for a tool half alone, so a bounded tool-only frame is
    #: still bounded rather than read as no identity.
    requires_server: bool = False


#: Harness ``_meta`` channels that carry a trusted tool identity, in read order.
#:
#: TRUST CLASS: every entry is HARNESS-emitted, not model-authored -- the same class as
#: kiro-cli's own ``_meta.kiro``. The harness resolves which server and tool it is about
#: to run and states it; the model cannot reach these fields, which is exactly why the
#: user-visible ``title`` (LLM prose) cannot be used for identity and these can.
#:
#: kiro-cli sets its pair ONLY for MCP-served calls (``kiro_tool_identity_meta`` in the
#: engine), and goose sets ``extensionName`` only when an extension served the call, so
#: in both cases a non-empty server is the discriminator "this was served by MCP" that a
#: security gate needs to tell a genuine MCP tool from a shell command whose stdout the
#: model authored.
_MCP_IDENTITY_META_CHANNELS: tuple[_MetaIdentityChannel, ...] = (
    # The KAS row FIRST, and it answers only when its own ``serverName`` is present on
    # the block (``requires_server``: ``_meta_identity_full`` skips the row when its
    # server field is absent): the two ``kiro`` rows share the ``toolName`` field, so
    # consulted second the KAS row would never see a KAS MCP frame's tool half (the
    # kiro-cli row would have taken it, unbounded), and consulted first without the
    # presence gate it would take every kiro-cli built-in's ``toolName`` -- a frame
    # with no server of either spelling -- under the KAS bound.
    # KAS (kiro-agent) spells the MCP server on a tool_call as ``_meta.kiro.serverName``
    # and stamps NO tool name on that frame -- captured live on kiro-cli 2.24.0 against a
    # stdio server: ``{"kiro": {"serverName": "probefs", "toolOrigin": "client"}}``. The
    # tool half arrives later, on the permission request, as
    # ``_meta.kiro.mcpTool.identity.{serverName,toolName}`` (``build_permission_event``
    # reads that). Without this row a KAS MCP call read as a BUILT-IN: no server, so the
    # built-in-only readers (the kiro-cli-name alias fold, the host read-only proof)
    # treated a server's ``read_file`` as the engine's own.
    _MetaIdentityChannel(
        key="kiro",
        server_field="serverName",
        tool_field="toolName",
        bounds_names=True,
        requires_server=True,
    ),
    _MetaIdentityChannel(
        key="kiro",
        server_field="mcpServerName",
        tool_field="toolName",
        # Read for identity on kiro-cli and kas alike. Which backends the fail-closed
        # refusal covers is not this table's question: that is ``ACP_BACKENDS_META_IDENTITY``
        # in the vocabulary module, and the coverage pin measures each member through
        # these readers rather than through a per-row claim.
    ),
    # goose (measured on 1.50.1): ``_meta.goose.toolCall.{extensionName,toolName}``,
    # captured live in ``test/fixtures/acp_frames/goose/mcp-stdio-mount-live.jsonl``. Its
    # ``toolName`` is the fused ``<extension>__<tool>`` form it also puts in the title, so
    # the extension prefix is stripped to leave the bare tool the deny set spells.
    _MetaIdentityChannel(
        key="goose",
        server_field="extensionName",
        tool_field="toolName",
        nested="toolCall",
        strips_server_prefix="__",
        # goose serves its builtin shell from the ``developer`` extension, captured live
        # in ``test/fixtures/acp_frames/goose/turn-live.jsonl``. It is the harness's own
        # tool, not one of Crew's MCP servers, and on the branch Crew selects the frame
        # carries no ``kind`` -- so this pair is the only thing on the wire that says
        # "this is a command".
        builtin_shell=("developer", "shell"),
    ),
    # Google Antigravity states an MCP call in the tool frame's ``_meta.mcp``
    # object. Both halves are harness-authored; eager MCP tools carry the same
    # pair, so an identity does not depend on the dispatch-envelope spelling.
    # Bounded like the KAS row: the client caches both halves per open call for
    # the permission event, so an unbounded half would be held at whatever size
    # the frame sent for as long as the call stays open. A half over the bound
    # is reported unreadable, and the client refuses that call's permission
    # request on the mark; it is never cut to fit.
    _MetaIdentityChannel(
        key="mcp",
        server_field="server",
        tool_field="tool",
        bounds_names=True,
    ),
)


def _meta_identity_full(update: dict[str, Any]) -> tuple[str, str, bool, bool]:
    """``(server, tool, builtin_shell, unreadable)`` from the first meta channel that answers.

    ``("", "", False, False)`` when none does. ``unreadable`` is True when the
    answering channel PRESENTED a half the MCP-name bound refused
    (:func:`mcp_identity_unreadable`): the frame names a tool Crew cannot retain,
    which the permission path must refuse rather than read as no identity. Both identity halves come from ONE channel:
    mixing a server from one harness's block with a tool name from another's would be an
    identity no harness asserted.

    A pair matching that channel's :attr:`_MetaIdentityChannel.builtin_shell` is reported
    with an EMPTY server, because the harness named its own extension there and Crew's
    consumers read a non-empty server as "an MCP server served this call". Returning the
    name would make the harness's shell look MCP-served, which is the inverse of what the
    frame says.
    """
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return "", "", False, False
    for channel in _MCP_IDENTITY_META_CHANNELS:
        block = meta.get(channel.key)
        if not isinstance(block, dict):
            continue
        if channel.nested is not None:
            block = block.get(channel.nested)
            if not isinstance(block, dict):
                continue
        if channel.requires_server and channel.server_field not in block:
            # The KAS row answers only for a frame spelled in its own vocabulary
            # (its server field present); a frame that carries only the shared
            # tool field is another harness's and falls through to that row.
            continue
        server = block.get(channel.server_field)
        tool = block.get(channel.tool_field)
        if channel.bounds_names:
            # The KAS and Antigravity spellings: bounded at the point of retention
            # by the MCP-name bound Crew's own tool surface admits
            # (``mcp_identity_name``, 512 characters, any spelling), because the
            # halves are cached per call for the permission path. Every
            # name the surface would list keeps its identity (an exact per-tool
            # deny binds to nothing else); a PRESENT half over the bound is
            # reported unreadable, never read as absent, so the request is
            # refused rather than judged under no identity.
            unreadable = mcp_identity_unreadable(tool) or mcp_identity_unreadable(server)
            tool = mcp_identity_name(tool)
            server = mcp_identity_name(server)
        else:
            # A first-class harness's channel: retained as it always was -- a
            # string verbatim, anything else absent -- and never unreadable (H13).
            unreadable = False
            tool = tool if isinstance(tool, str) else ""
            server = server if isinstance(server, str) else ""
        # Matched on the RAW pair, before any prefix reduction: the pair is what the
        # harness wrote, and the reduction below is Crew's own normalization of a fused
        # MCP tool name.
        if channel.builtin_shell is not None and (server, tool) == channel.builtin_shell:
            return "", tool, True, False
        if channel.strips_server_prefix and server and tool:
            prefix = f"{server}{channel.strips_server_prefix}"
            if tool.startswith(prefix):
                tool = tool[len(prefix) :]
        if server or tool or unreadable:
            return server, tool, False, unreadable
    return "", "", False, False


def _meta_identity(update: dict[str, Any]) -> tuple[str, str]:
    """``(server, tool)`` from the first meta channel that answers, else ``("", "")``."""
    server, tool, _, _ = _meta_identity_full(update)
    return server, tool


def meta_builtin_server_names() -> frozenset[str]:
    """The ``server`` half of every channel's builtin-shell pair.

    A harness that models its builtins as extensions names one under the same field an
    MCP call fills, so these are the identities that are NOT foreign to a session even
    though Crew never placed them on its server array: they are the harness's own.
    """
    return frozenset(
        channel.builtin_shell[0]
        for channel in _MCP_IDENTITY_META_CHANNELS
        if channel.builtin_shell is not None
    )


def meta_builtin_shell(update: dict[str, Any]) -> bool:
    """True when a harness's own ``_meta`` channel says this call is ITS shell.

    The trusted answer to "is this a command?" on a harness that sends no ACP ``kind``.
    Same trust class as the identity it sits beside: harness-resolved and unreachable by
    the model, unlike the ``title`` (a formatter's or the model's prose) and unlike
    ``rawInput`` (the model's own argument map, copied before the harness parses it).
    """
    return _meta_identity_full(update)[2]


def _kiro_tool_name(update: dict[str, Any]) -> str:
    """The real tool name from a harness's own ``_meta`` identity channel, or "".

    The user-visible ``title`` is LLM-authored prose ("Creating task list: …"), so it
    identifies nothing. Only a ``_meta`` channel is stable, and
    :data:`_MCP_IDENTITY_META_CHANNELS` holds the ones that exist.

    The ``kiro`` in the name reads as the QUESTION this answers ("what tool is this?")
    rather than as the harness: every channel in that table is consulted here.
    """
    return _meta_identity(update)[1]


def _kiro_mcp_server_name(update: dict[str, Any]) -> str:
    """The MCP server name from a harness's own ``_meta`` identity channel, or "" for
    built-in/shell tools.

    A non-empty value is the trusted discriminator "this tool call was served by an MCP
    server" — the signal a security gate needs to tell a genuine MCP directive tool from
    a shell command whose stdout the model authored. Every channel in
    :data:`_MCP_IDENTITY_META_CHANNELS` sets it only for a served call, so the emptiness
    carries the same meaning on each.
    """
    return _meta_identity(update)[0]
