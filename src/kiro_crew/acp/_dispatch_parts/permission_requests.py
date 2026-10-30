"""A ``session/request_permission``, read into ``EVENT_PERMISSION_REQUEST``.

The advertised option kinds and the least-destructive reject, the gate-extension
envelope, pi's MCP title respelling, provable MCP identity and approval, KAS consent,
and :func:`build_permission_event` over the provenance caches.

:mod:`kiro_crew.acp._dispatch` stays the import path and the patch surface; see
:mod:`kiro_crew.acp._dispatch_parts`.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from collections.abc import Iterable
from typing import Any, cast

from kiro_crew.acp._dispatch_parts.surfaced_text import (
    _dumps_degraded,
    _loggable_request_id,
    _redact,
    extract_tool_purpose,
    redact_text,
)
from kiro_crew.acp._dispatch_parts.tool_call_identity import (
    _harness_identifier,
    _permission_tool_id,
    is_shell_kind,
)
from kiro_crew.acp._dispatch_parts.tool_call_provenance import scoped_tool_cache_key
from kiro_crew.acp.harness_tool_names import (
    CLAUDE_READ_ONLY_BUILTINS,
    GOOSE_READ_ONLY_BUILTINS,
    qualified_harness_tool_id,
)
from kiro_crew.acp.types import (
    EVENT_PERMISSION_REQUEST,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_PI,
)
from kiro_crew.mcp_gateway.tool_surface import mcp_identity_name, mcp_identity_unreadable
from kiro_crew.platform.guest_paths import GuestPathParams
from kiro_crew.session_environment import WORKSPACE_DENY_ALIASES, WORKSPACE_TOOL_KINDS

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp._dispatch")


#: The workspace tool arguments that name a guest path (``cwd`` is ``run_command``'s).
_WORKSPACE_PATH_ARGUMENTS = ("path", "cwd")


def _anchored_in_workspace(params: dict | None, root: str) -> dict | None:
    """``params`` with each relative workspace path joined to the guest ``root``, as
    the guest tool server resolves it; an absolute one is left as stated."""
    if not params:
        return params
    anchored = dict(params)
    for key in _WORKSPACE_PATH_ARGUMENTS:
        value = anchored.get(key)
        if isinstance(value, str) and not posixpath.isabs(value):
            anchored[key] = posixpath.normpath(posixpath.join(root, value or "."))
    return anchored


def _workspace_call_arguments(
    params: dict | None, server: str, tool: str
) -> tuple[dict | None, bool]:
    """``(arguments the guest receives, readable)`` for a workspace call.

    A Tool Search deferred call reaches the server through the backend's
    ``tool_call`` meta-tool, so its input is ``{"tool_id": "<server>::<tool>",
    "arguments": {...}}`` while the server gets only ``{...}``
    (``session_directive`` measured the same envelope on KAS). The inner
    arguments are judged, and only when ``tool_id`` names the very call the
    trusted identity resolved to and the envelope carries nothing else besides
    ``_meta``. Any other input naming a ``tool_id`` is unreadable: no workspace
    tool takes that argument, so it can only be an envelope for some other call.
    """
    if not isinstance(params, dict) or "tool_id" not in params:
        return params, True
    inner = params.get("arguments")
    if (
        set(params) - {"_meta"} == {"tool_id", "arguments"}
        and params.get("tool_id") == f"{server}::{tool}"
        and isinstance(inner, dict)
    ):
        return inner, True
    return params, False


def reject_option_id(params: dict) -> str | None:
    """The least-destructive reject optionId a permission request advertises.

    Used when auto-answering a ``session/request_permission`` for a session
    this client never registered (a backend-internal subagent), and by
    :meth:`AcpClient.reject_tool` for a user's explicit deny. Prefer the
    request's own ``reject_once``-kind option; fall back to a ``behavior``
    that names deny, then to a legacy id that names reject. ``None`` means
    the caller must answer with the ``cancelled`` outcome instead — kiro-cli
    maps that to cancelling the TURN, which auto-denies every later tool call
    in it without prompting, so recognition here is deliberately
    broad: any deny-shaped option beats the cancelled fallback. What it must
    never do is pick an ALLOW option, so every branch matches deny-naming
    values exactly rather than by substring.
    """
    raw = params.get("options")
    if not isinstance(raw, list):
        return None
    options = [o for o in raw if isinstance(o, dict)]
    for want_kind in ("reject_once", "reject_always"):
        for opt in options:
            opt_id = opt.get("optionId") or opt.get("id")
            if opt.get("kind") == want_kind and isinstance(opt_id, str) and opt_id:
                return opt_id
    # Adapters that speak `behavior` instead of `kind`: an exact deny/reject
    # behavior is unambiguous whatever the id is called. Same vocabulary as
    # build_permission_event's branch (_DENY_BEHAVIORS) by construction.
    # An option carrying a VALID spec `kind` is classified by that kind alone:
    # a contradictory {kind:"allow_once", behavior:"deny"} must never be
    # selected as a reject — answering with an allow optionId APPROVES the
    # tool, the exact inversion this function exists to prevent.
    for opt in options:
        opt_id = opt.get("optionId") or opt.get("id")
        if opt.get("kind") in _SPEC_OPTION_KINDS:
            continue
        behavior = opt.get("behavior")
        if (
            isinstance(behavior, str)
            and behavior.lower() in _DENY_BEHAVIORS
            and isinstance(opt_id, str)
            and opt_id
        ):
            return opt_id
    # Legacy payloads omit `kind` and `behavior` — match well-known deny ids
    # only (exact, lowercased), so an allow option can never be picked by
    # accident. Derived from _LEGACY_OPTION_KIND so this list and the event
    # builder's classification cannot drift apart. Same kind-wins precedence
    # as the behavior branch above.
    for opt in options:
        opt_id = opt.get("optionId") or opt.get("id")
        if opt.get("kind") in _SPEC_OPTION_KINDS:
            continue
        if isinstance(opt_id, str) and opt_id.lower() in _DENY_OPTION_IDS:
            return opt_id
    return None


# Legacy kiro permission options omit the spec-mandated `kind` field. Only
# synthesize a kind for these well-known literals — unknown ids stay empty so we
# don't fabricate intent the agent didn't express. Shared by both transports.
_LEGACY_OPTION_KIND: dict[str, str] = {
    OPTION_ALLOW_ONCE: "allow_once",
    "allow": "allow_once",
    OPTION_ALLOW_ALWAYS: "allow_always",
    "reject_once": "reject_once",
    "reject_always": "reject_always",
    # Deny-naming ids without a `kind`: recognising them is what keeps a user
    # denial on the per-tool reject path. Missing them drops reject_tool onto
    # the `cancelled` outcome, which kiro-cli treats as cancelling the TURN —
    # auto-denying every later tool call in it unprompted.
    "reject": "reject_once",
    "deny": "reject_once",
    "deny_once": "reject_once",
    "decline": "reject_once",
    "deny_always": "reject_always",
}

#: The four ACP-spec permission-option kinds. An option carrying one of these
#: is classified by its kind ALONE — later deny-recognition branches
#: (behavior, legacy id) must not override it, or a contradictory
#: {kind:"allow_once", behavior:"deny"} could be answered as a reject with an
#: ALLOW optionId, approving the tool the caller meant to deny.
_SPEC_OPTION_KINDS = frozenset({"allow_once", "allow_always", "reject_once", "reject_always"})

#: `behavior` values that mark an option as a deny, for adapters that speak
#: behavior instead of kind (behavior:"deny" appears as the selection RESULT in
#: claude-agent-acp; recognising it on an advertised option is defensive).
#: Exact-match only — an allow option must never be classified as a reject.
_DENY_BEHAVIORS = frozenset({"deny", "reject"})

#: Deny-naming option ids, DERIVED from the one table above so the auto-answer
#: path (`reject_option_id`) and the event builder (`build_permission_event`)
#: cannot drift on the vocabulary.
_DENY_OPTION_IDS: frozenset[str] = frozenset(
    k for k, v in _LEGACY_OPTION_KIND.items() if v in ("reject_once", "reject_always")
)


#: The marker key Kiro Crew's own gate extension puts in a harness confirm dialog.
#:
#: A harness with no permission gate of its own is gated by an extension Kiro Crew
#: loads into it (``agent_sdk.backends.Routing.VERIFIED_GATE_EXTENSION``). That
#: extension can only raise the harness's generic confirm dialog, which the adapter
#: forwards as a ``session/request_permission`` whose ``toolCall`` describes the
#: DIALOG -- ``kind: "other"``, a ``rawInput`` of ``{method, title, message}`` -- and
#: not the tool call behind it. So the extension writes the tool call into the
#: dialog's message as a JSON envelope under this marker, and the builder below
#: reads it back out. Keyed on the marker, not on a backend id: the envelope is a
#: wire contract between two pieces of Kiro Crew's own code, and any harness whose
#: gate is one of these extensions speaks it.
GATE_ENVELOPE_MARKER = "kiro-crew-gate"


def gate_envelope(tool_call: dict[str, Any], nonce: str | None) -> dict[str, Any] | None:
    """The tool call a gate-extension confirm dialog is asking about, or ``None``.

    Returns ``{"toolCallId", "title", "kind", "input", "truncated"}`` when
    *tool_call* is the adapter's rendering of Kiro Crew's own dialog: ``rawInput.method``
    is ``confirm``, ``rawInput.message`` parses as a JSON object carrying
    :data:`GATE_ENVELOPE_MARKER`, its ``nonce`` is the one this session issued,
    and its ``tool`` is the dialog's own title. Anything else -- another
    extension's dialog, a native tool-call permission, a message that is not the
    envelope -- is ``None`` and the frame is read exactly as before.

    *nonce* is the per-session value the driver put in the harness child's
    environment for the extension to echo, and ``None`` means this session runs
    no gate extension at all. Both checks exist for the same reason: on every
    harness a permission frame's ``rawInput`` IS the model's tool arguments, so a
    marker alone would let a model on any backend call any tool with
    ``{method: "confirm", message: "<envelope>"}`` and have the gate judge the
    benign call it described while the real one ran. A session without a gate
    extension therefore never consults the envelope, and one with it accepts
    only the envelope its own extension could have written. The title check
    closes the smaller gap on the gated harness itself: another extension relaying
    model text into a dialog controls that text, not the tool the harness names.

    The ``tool`` and ``kind`` fields are authored by the extension from the
    harness's own event, so they carry the same provenance the harness's own
    ``tool_call`` frame would; ``input`` is the tool's arguments, model-influenced
    like every tool input, and is treated as such downstream.
    """
    if not isinstance(nonce, str) or not nonce:
        return None
    raw_input = tool_call.get("rawInput")
    if not isinstance(raw_input, dict) or raw_input.get("method") != "confirm":
        return None
    message = raw_input.get("message")
    if not isinstance(message, str) or GATE_ENVELOPE_MARKER not in message:
        return None
    try:
        body = json.loads(message)
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get(GATE_ENVELOPE_MARKER) != 1:
        return None
    if body.get("nonce") != nonce:
        return None
    tool = body.get("tool")
    if not isinstance(tool, str) or not tool or raw_input.get("title") != tool:
        return None
    kind = body.get("kind")
    tool_call_id = body.get("toolCallId")
    return {
        "toolCallId": tool_call_id if isinstance(tool_call_id, str) and tool_call_id else "",
        "title": tool,
        "kind": kind if isinstance(kind, str) else "other",
        "input": body.get("input"),
        "truncated": body.get("truncated") is True,
    }


#: The server-name characters pi keeps in an MCP tool name; any other is ``_``.
_PI_MCP_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def pi_mcp_title(tool_name: str, mcp_servers: Iterable[str]) -> str:
    """A pi ``mcp__<server>__<tool>`` name in the server's own spelling.

    pi writes every server character outside ``[A-Za-z0-9_]`` as ``_``, so a Crew
    server ``kirocrew-core`` arrives as ``mcp__kirocrew_core__send_message``. Matched
    against the servers Crew handed the session (longest first, the first of equal
    length winning, as pi registers the first of two names it counts as one), the
    title becomes ``mcp__kirocrew-core__send_message`` -- what every other harness
    states -- so a governance or deny rule written as ``@kirocrew-core`` binds to it.
    A name no handed-off server prefixes is returned as it came.
    """
    if not tool_name.startswith("mcp__"):
        return tool_name
    rest = tool_name[len("mcp__") :]
    best = ""
    for server in mcp_servers:
        if not isinstance(server, str) or not server or len(server) <= len(best):
            continue
        prefix = f"{_PI_MCP_NAME_UNSAFE.sub('_', server)}__"
        if rest.startswith(prefix) and len(rest) > len(prefix):
            best = server
    if not best:
        return tool_name
    return f"mcp__{best}__{rest[len(_PI_MCP_NAME_UNSAFE.sub('_', best)) + 2 :]}"


def identified_mcp_call(event: AcpEvent) -> tuple[str, str] | None:
    """The ``(server, tool)`` a permission event PROVABLY refers to, or None.

    TWO trusted channels, and neither is the permission payload's own fields -- those
    are a different shape (``serverName`` and prose) and could be anything.

    The first is the params cached from the preceding ``tool_call`` frame
    (``raw_params_trusted``), whose ``server``/``tool`` keys are the adapter's own
    resolution of what will run -- codex-acp's ``createMcpRawInput``.

    The second is the ``_meta`` identity the frame carried, cached and inherited the same
    way (``mcp_identity_trusted``). It exists because rawInput is NOT a universal
    channel: goose sends ``rawInput`` as ``{"": "{}"}`` and states the identity in
    ``_meta.goose.toolCall`` instead, so a reader that only knew rawInput found no
    identity on a harness that had published one. Same trust class either way -- both are
    harness-resolved and unreachable by the model -- which is why the fallback is a
    second source and not a weaker one.
    """
    if event.raw_params_trusted:
        params = event.raw_tool_params if isinstance(event.raw_tool_params, dict) else {}
        server = params.get("server")
        tool = params.get("tool")
        if isinstance(server, str) and isinstance(tool, str) and server and tool:
            return server, tool
    if event.mcp_identity_trusted and event.mcp_server_name and event.tool_name:
        return event.mcp_server_name, event.tool_name
    return None


def is_mcp_tool_approval(msg: JsonRpcMessage, event: AcpEvent | None = None) -> bool:
    """Whether a ``session/request_permission`` is an MCP tool-call approval.

    Two signals, because no single one is present on every harness.

    codex-acp marks the REQUEST with ``_meta.is_mcp_tool_approval``
    (``buildMcpPermissionRequest``), for the correlated and the standalone shape alike,
    and sets it on neither a shell nor an edit approval.

    A harness that sets no such marker can still have SAID what the call is: a trusted
    ``_meta`` identity naming a server means the harness resolved this call to an MCP
    server, which is the same claim. Read off *event* so it comes from the cached
    ``tool_call`` frame rather than the permission payload. Without this, a harness that
    publishes its identity but not codex's marker slipped past the
    unidentified-approval refusal entirely -- and on an auto-approve path with a deny set
    that meant a switched-off tool ran.

    A built-in or shell call answers False on both signals: goose leaves
    ``extensionName`` unset for its own tools, so no server is named and nothing here
    fires.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    meta = params.get("_meta")
    if isinstance(meta, dict) and meta.get("is_mcp_tool_approval") is True:
        return True
    return bool(event is not None and event.mcp_identity_trusted and event.mcp_server_name)


#: KAS toolId of a tool whose permission request is a consent question about a
#: call that runs no host command -> (the ``_meta.kiro.consent.capability`` KAS
#: stamps on that request, the Crew tool name the same tool goes by). A sub-agent
#: spawn is the case that needs it: its ``tool_call`` frame is taken for the
#: sub-agent roster and never reaches the shared parser, and its toolCallId is
#: synthetic (``invoke_subagent_<id>``), so every toolCallId-keyed cache misses.
#: The child's own tool calls raise their own permission requests, so this
#: classification waives nothing about them.
_KAS_NON_COMMAND_TOOLS: dict[str, tuple[str, str]] = {
    "invoke_sub_agent": ("subagent", "use_subagent"),
}


def kas_consent_tool(params: object) -> tuple[str, str]:
    """The Crew tool and target a KAS permission request's own ``_meta.kiro`` names.

    Returns ``(crew_tool_name, target)``, or ``("", "")``. ``_meta`` is written by
    the KAS engine, not the model -- the same channel Crew already trusts for MCP
    identity on ``tool_call`` frames. A non-empty answer means "not a shell call,
    this is the tool, and this is the agent it starts", and is given only when
    every field agrees: the toolId is on :data:`_KAS_NON_COMMAND_TOOLS`, the
    consent capability is the one KAS assigns that toolId, ``consent.resource``
    names the target, and no ``command`` rides along (KAS sets that field only for
    a shell call). Anything else -- an unknown toolId, a missing or different
    capability, a ``shell`` capability, no target, a command -- returns
    ``("", "")`` and leaves the request unclassified, which keeps the refusal.

    The name returned is Crew's (``use_subagent``), not KAS's, because it is the
    name a deny rule or a ``tools`` ceiling is written against. The target is what
    ``capabilities.spawn``'s ``agents`` scope is judged on, so a spawn with no
    stated target is not classified at all rather than vetted as "no agent".
    """
    if not isinstance(params, dict):
        return "", ""
    meta = params.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    if not isinstance(kiro, dict) or "command" in kiro:
        return "", ""
    tool_id = kiro.get("toolId")
    known = _KAS_NON_COMMAND_TOOLS.get(tool_id) if isinstance(tool_id, str) else None
    if known is None:
        return "", ""
    capability, crew_name = known
    consent = kiro.get("consent")
    if not isinstance(consent, dict) or consent.get("capability") != capability:
        return "", ""
    target = consent.get("resource")
    if not isinstance(target, str) or not target.strip():
        return "", ""
    return crew_name, target.strip()


def build_permission_event(
    msg: JsonRpcMessage,
    *,
    tool_input_cache: dict[str, str] | None = None,
    tool_input_redacted_cache: dict[str, bool] | None = None,
    shell_cache: dict[str, bool] | None = None,
    raw_params_cache: dict[str, dict] | None = None,
    mcp_server_name_cache: dict[str, str] | None = None,
    tool_name_cache: dict[str, str] | None = None,
    cache_scope: str = "",
    diff_path_cache: dict[str, str] | None = None,
    tool_kind_cache: dict[str, str] | None = None,
    identity_unreadable_cache: dict[str, bool] | None = None,
    gate_envelope_nonce: str | None = None,
    kas_consent_meta: bool = False,
    harness_tool_name_cache: dict[str, str] | None = None,
    harness_backend: str = "",
    harness_mcp_servers: tuple[str, ...] = (),
    side_read_only: bool = False,
    deepseek_read_only_issue: str = "the harness composition was not read back",
    remote_workspace_server: str = "",
    remote_workspace_root: str = "",
) -> tuple[AcpEvent | None, dict[str, str] | None]:
    """Build an ``EVENT_PERMISSION_REQUEST`` from a ``session/request_permission``.

    ``gate_envelope_nonce`` is set only by a caller whose session runs Kiro Crew's
    gate extension (``Routing.VERIFIED_GATE_EXTENSION``); see :func:`gate_envelope`
    for what it unlocks and why the default consults nothing.

    ``kas_consent_meta`` is set only by a caller whose session runs on KAS. It lets a
    shell-cache MISS be resolved to "not a shell call" from the request's own
    ``_meta.kiro`` block (see :func:`kas_consent_tool`), and to carry that tool's
    Crew name as ``tool_name`` so deny and governance rules bind to it; it never sets
    ``is_shell`` True and never overrides a cache hit. Every other backend leaves it
    False, so its payloads are read exactly as before.

    ``harness_tool_name_cache`` (caller-owned ``toolCallId -> harness_tool_name``)
    and ``harness_backend`` give a backend that states no ``_meta.kiro.toolId`` a
    ``harness_tool_id`` anyway: the tool name its preceding ``tool_call`` frame
    stated, qualified by the backend (``goose#shell``). Only a backend with a name
    table (:mod:`kiro_crew.acp.harness_tool_names`) gets one. On a claude or goose
    Side Chat session (``side_read_only``) the same cache gives
    ``harness_builtin_tool`` instead: the kiro-cli read built-in the call stands
    for (:data:`~kiro_crew.acp.harness_tool_names.CLAUDE_READ_ONLY_BUILTINS`,
    :data:`~kiro_crew.acp.harness_tool_names.GOOSE_READ_ONLY_BUILTINS`). Every
    other session's event is built exactly as before.
    ``harness_mcp_servers`` are the server names
    Crew placed on the session, so an opencode MCP tool's fused name is split.

    Single source of truth shared by ``AcpClient`` and ``AcpSessionHandle`` so
    the two transports cannot drift on the kiro/claude permission payload shape:
    kiro nests the tool info under ``params["toolCall"]`` (not a flat
    ``params["title"]``), so reading the flat field leaves ``title`` /
    ``is_shell`` empty and trips the host trust-mode gate.

    Returns ``(event, recorded_options)`` where ``recorded_options`` is the
    ``{"once","always","reject"}`` optionId map the caller stores on the request
    id so ``approve_tool`` / ``reject_tool`` can echo the exact ids the agent
    advertised (``None`` when no allow/reject option was advertised).

    ``tool_input_cache`` (caller-owned ``toolCallId -> redacted input``) is
    consulted to recover the full tool input the preceding ``tool_call``
    notification carried; ``shell_cache`` (caller-owned ``toolCallId -> is_shell``)
    is the ONLY trusted source for the shell signal (deny-by-default — the
    permission payload's own ``kind`` is agent-influenced and must not waive the
    tool-name length cap).
    """
    if not isinstance(msg.id, (str, int, float)) or isinstance(msg.id, bool):
        logger.debug(
            "Ignoring permission request with unsupported id type: %s",
            type(msg.id).__name__,
        )
        return None, None
    # A float id is kept exactly as the frame sent it, so the answer echoes it.
    request_id = cast("str | int", msg.id)
    params = msg.params or {}
    tool_call = params.get("toolCall", {})
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    # A gate-extension dialog describes the DIALOG; the tool call it asks about is
    # in its envelope. Substitute that description for the frame's own so the rest
    # of this builder reads the real tool name, kind, id and arguments. The
    # envelope's id is the harness's own toolCallId for the call, so the caches
    # below are consulted under the same key the preceding ``tool_call`` frame wrote.
    envelope = gate_envelope(tool_call, gate_envelope_nonce)
    if envelope is not None:
        # pi spells a Crew server's tools in its own alphabet; the title is put back
        # in the server's spelling so title-derived rules (governance, deny) bind.
        if harness_backend == ACP_BACKEND_PI:
            envelope["title"] = pi_mcp_title(envelope["title"], harness_mcp_servers)
        tool_call = {
            "toolCallId": envelope["toolCallId"] or tool_call.get("toolCallId", ""),
            "title": envelope["title"],
            "kind": envelope["kind"],
            "input": envelope["input"],
        }
    raw_title = tool_call.get("title", "unknown")
    title = _redact(raw_title if isinstance(raw_title, str) else "")
    # The ACP toolCall carries a `kind` ("execute" for Bash, "read"/"edit"/…).
    # The gate keys two tiers on it (the read-only proof as an ALLOW-list, the
    # write-plane routing for edit/delete); the is_shell length-cap exemption
    # resolves from shell_cache below, never this field. Filled from the
    # tool_call cache below when this frame omits it (KAS). A non-string value
    # is read as absent: the field is an unvalidated frame value from the
    # harness subprocess, and a list or number reaching the kind-keyed set
    # membership downstream would raise rather than classify.
    _frame_kind = tool_call.get("kind", "")
    tool_kind = _frame_kind if isinstance(_frame_kind, str) else ""

    # ACP spec uses optionId/name + kind ("allow_once"|"allow_always"|
    # "reject_once"|"reject_always"); kiro-cli uses id/label with id
    # values "allow_once"/"allow_always". Accept both shapes and remember the
    # actual optionIds keyed by kind so approve/reject can echo the exact id.
    options: list[dict[str, str]] = []
    kind_to_id: dict[str, str] = {}
    raw_options = params.get("options", [])
    for o in raw_options if isinstance(raw_options, list) else []:
        if not isinstance(o, dict):
            continue
        opt_id = o.get("optionId") or o.get("id") or ""
        opt_label = o.get("name") or o.get("label") or ""
        opt_kind = o.get("kind") or ""
        # A truthy non-string id would crash opt_id.lower() below (and
        # non-string label/kind would leak into the typed options list).
        if not isinstance(opt_id, str) or not opt_id:
            continue
        if not isinstance(opt_label, str):
            opt_label = ""
        if not isinstance(opt_kind, str):
            opt_kind = ""
        options.append({"id": opt_id, "label": opt_label})
        if not opt_kind:
            opt_kind = _LEGACY_OPTION_KIND.get(opt_id.lower(), "")
        if not opt_kind:
            # Adapters that speak `behavior` instead of `kind`: an exact deny
            # behavior classifies the option as a per-tool reject whatever the
            # id is called, keeping a user denial off the turn-cancelling
            # `cancelled` fallback.
            behavior = o.get("behavior")
            if isinstance(behavior, str) and behavior.lower() in _DENY_BEHAVIORS:
                opt_kind = "reject_once"
        if opt_kind:
            kind_to_id.setdefault(opt_kind, opt_id)
    if not options:
        options = [
            {"id": OPTION_ALLOW_ONCE, "label": "Allow once"},
            {"id": OPTION_ALLOW_ALWAYS, "label": "Allow always"},
        ]
        kind_to_id = {"allow_once": OPTION_ALLOW_ONCE, "allow_always": OPTION_ALLOW_ALWAYS}

    # Record optionIds the agent advertised so approve_tool / reject_tool can
    # echo the exact ids. Record when EITHER an allow option (for approve) OR a
    # reject option (for a clean reject) was advertised. claude-agent-acp offers
    # a {kind:"reject_once", optionId:"reject"} whose selection yields
    # behavior:"deny" — far better than a "cancelled" outcome, which the adapter
    # turns into a cryptic "Tool use aborted". A payload advertising no
    # deny-shaped option at all leaves reject_tool on the "cancelled" fallback,
    # which kiro-cli maps to cancelling the TURN — auto-denying every later
    # tool call in it; that is why recognition above is deliberately
    # broad and why both fallback sites log a warning.
    any_allow = kind_to_id.get("allow_once") or kind_to_id.get("allow_always")
    any_reject = kind_to_id.get("reject_once") or kind_to_id.get("reject_always")
    recorded: dict[str, str] | None = None
    if request_id != "" and (any_allow is not None or any_reject is not None):
        recorded = {}
        if any_allow is not None:
            recorded["once"] = kind_to_id.get("allow_once") or any_allow
            recorded["always"] = kind_to_id.get("allow_always") or any_allow
        if any_reject is not None:
            recorded["reject"] = any_reject

    # Resolve full tool input — the preceding tool_call notification carries the
    # complete params cached by toolCallId; the permission message only has a
    # truncated human-readable title.
    raw_tool_call_id = tool_call.get("toolCallId", "")
    tool_call_id = raw_tool_call_id if isinstance(raw_tool_call_id, str) else ""
    # ORIGIN-BOUND cache key. toolCallIds are backend/LLM-authored: without
    # scoping, a child session could replay a consumed parent toolCallId and
    # inherit the parent's trusted provenance (params/shell/MCP identity) for
    # a DIFFERENT operation. Scoping by the emitting frame's sessionId means
    # provenance is only readable by the origin that wrote it, while
    # same-origin repeat frames (re-ask after reject_once, mode-change
    # re-prompt) still find their entry.
    _ck = scoped_tool_cache_key(cache_scope, tool_call_id)
    # A permission frame that names no ``kind`` takes the one its preceding
    # ``tool_call`` frame carried, under the same origin-scoped id. KAS sends
    # ``toolCall: {toolCallId, status, title}`` here and puts ``kind`` on the
    # tool_call only, so every KAS call otherwise reached the gate's kind-keyed
    # tiers as ``""`` -- and the write-plane routing that makes a KAS
    # ``delete_file`` of a write-protected config refusable never fired on the
    # one harness that has ``delete_file``. The cache holds ONLY write-plane
    # kinds (``edit``/``delete``; the writer in ``_build_tool_call_event``
    # retains nothing else), so this carries exactly that routing and never a
    # ``search`` -- which KAS stamps on ``grep_search``/``file_search``/
    # ``list_directory`` and which would veto the host read-only proof -- nor a
    # ``read`` that would widen the interactive allow-list on a field the
    # frame never stated. Fallback ONLY: a kind the frame does state is never
    # overridden. The cache is passed by ``AcpSessionHandle`` on the KAS
    # backend ONLY (``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL``); ``None``
    # here means "not this harness", and the kiro-cli path is unchanged.
    if not tool_kind and tool_call_id and tool_kind_cache is not None:
        tool_kind = tool_kind_cache.get(_ck, "") or ""
    tool_input = ""
    tool_input_redacted = False
    if tool_call_id and tool_input_cache is not None and _ck in tool_input_cache:
        # Retain the rendered input for same-call permission re-prompts.  A
        # non-shell rawInput may legally be a string/list rather than a dict;
        # consuming this cache made the repeat look argument-free and promoted
        # it to durable mcp__server__tool trust.  Per-turn clear() remains the
        # lifecycle boundary, matching the provenance caches below.
        tool_input = tool_input_cache.get(_ck, "")
        # A cache written by an older/minimal caller may not have the matching
        # provenance map (or may be missing just this key).  The rendered input
        # is still safe to show, but its completeness is unknown: fail closed
        # for durable trust rather than treating unknown provenance as proof
        # that no bytes were redacted.  Ordinary allow-once remains available.
        tool_input_redacted = (
            tool_input_redacted_cache.get(_ck, True)
            if tool_input_redacted_cache is not None
            else True
        )
    if not tool_input:
        raw_input = tool_call.get("input") or tool_call.get("params")
        if raw_input:
            tool_input = (
                _dumps_degraded(raw_input, indent=2)
                if isinstance(raw_input, (dict, list))
                else str(raw_input)
            )
            # SECURITY: the primary path (tool_input_cache) is already redacted
            # by the tool_call parser; on a cache miss this fallback reads raw
            # LLM-influenced input that surfaces on the dashboard permission UI,
            # so scrub exfil URLs + credentials before it leaves this function.
            safe_tool_input = redact_text(tool_input)
            tool_input_redacted = safe_tool_input != tool_input
            tool_input = safe_tool_input

    # Resolve the canonical shell signal. SECURITY (deny-by-default): the ONLY
    # trusted source is the value cached from the preceding tool_call (keyed by
    # toolCallId). We deliberately do NOT fall back to the permission payload's
    # own `kind` — that field is agent/LLM-influenced, and trusting it to waive
    # the tool-name length cap on the very name being validated would let a
    # malicious agent set kind="execute" to bypass the check. On a cache miss
    # is_shell stays False and the length cap is enforced. Use .get() (not
    # .pop()): a later tool_call_update refinement reads this same cache, so
    # popping here would make it wrongly report is_shell=False.
    cached_shell = shell_cache.get(_ck) if (shell_cache is not None and tool_call_id) else None
    if cached_shell is None and envelope is not None:
        # The envelope's kind is the gate extension's own rendering of the
        # harness's tool name -- Kiro Crew's code reading the harness's event, the
        # same provenance the cached ``tool_call`` kind has. It is NOT the frame's
        # own agent-influenced ``kind``, which the deny-by-default rule above is
        # about; that field described the dialog and was discarded.
        cached_shell = is_shell_kind(envelope["kind"])
    _kas_tool, _spawn_target = "", ""
    if cached_shell is None and kas_consent_meta:
        # A KAS sub-agent spawn's tool_call frame never reaches the caches; its
        # request's engine-written ``_meta.kiro`` is the classification instead.
        # Resolves to False only: a request it cannot read stays unclassified.
        _kas_tool, _spawn_target = kas_consent_tool(params)
        if _kas_tool:
            cached_shell = False
    is_shell = bool(cached_shell)
    if cached_shell is None and tool_input:
        logger.info(
            "Permission event resolved tool_input but missed is_shell cache "
            "(req=%s tool_call_id=%s)",
            _loggable_request_id(request_id),
            _loggable_request_id(tool_call_id),
        )

    # Resolve the STRUCTURED raw params for governance enforcement. The keystone
    # sensitive-path + write-protected-config checks (hooks.on_tool_call) read
    # event.raw_tool_params (a dict) — NOT the display title — so it must be set
    # or a title that hides the path (e.g. a generic "Editing" title over an SSH
    # key / security_policy.json) would slip past the arg-derived gate on this
    # shared path. Primary source: the raw dict the preceding tool_call cached by
    # toolCallId; fallback: an inline dict on the permission frame itself.
    _resolved_raw_params: dict | None = None
    _raw_params_trusted = False
    if tool_call_id and raw_params_cache is not None:
        # .get() (not .pop()), matching the sibling shell/MCP/tool-name caches:
        # a second permission frame for the same toolCallId (re-ask after
        # reject_once, re-prompt after a mode change) must still find the
        # params — popping made provenance single-use and downgraded the
        # repeat frame to low fidelity. The per-turn dispatch .clear()
        # handles cleanup.
        _resolved_raw_params = raw_params_cache.get(_ck)
        _raw_params_trusted = _resolved_raw_params is not None
    if _resolved_raw_params is None:
        _inline = tool_call.get("input") or tool_call.get("params")
        if isinstance(_inline, dict):
            _resolved_raw_params = _inline
            # Trusted when they came out of the gate envelope, and only then: the
            # extension read them off the harness's own tool_call event, which is
            # the same source the cache above is filled from. An inline dict on an
            # ordinary permission frame stays untrusted, as before. A TRUNCATED
            # envelope keeps its params -- the keys the path checks read survive
            # the cut -- but earns no durable trust, since a value was not seen
            # whole.
            _raw_params_trusted = envelope is not None and not envelope["truncated"]

    # Trusted MCP server + tool identity recovered from the preceding tool_call
    # (identity is never read off the permission payload). .get() (not .pop()) mirrors the
    # is_shell cache: a later tool_call_update for the same id re-reads it; the
    # per-turn dispatch .clear() handles cleanup. Empty on a miss (fail-closed
    # for the app-own-server auto-approve). The tool name lets the app-own-server
    # auto-approve govern the canonical mcp__<server>__<tool> on the permission
    # path.
    _cached_server = (
        mcp_server_name_cache.get(_ck)
        if (mcp_server_name_cache is not None and tool_call_id)
        else None
    )
    _cached_tool = (
        tool_name_cache.get(_ck) if (tool_name_cache is not None and tool_call_id) else None
    )
    _mcp_server_name = _cached_server or ""
    # The Crew name of a KAS consent-classified tool, so the deny floor and the
    # governance ceiling are asked about it and not about its title alone. It
    # does NOT set ``_mcp_identity_trusted`` below: that flag records a cache hit.
    _tool_name = _cached_tool or _kas_tool
    # KAS stamps the running tool's identity on the permission request itself and
    # NO ``toolName`` on its preceding tool_call -- that frame carries a display
    # title ("Read File") and ``toolOrigin`` only (captured live, kiro-cli 2.24.0).
    # Two frame fields, both harness-authored (the model cannot reach ``_meta``),
    # the same trust class as ``toolName``:
    #
    # * ``_meta.kiro.mcpTool.identity.{serverName, toolName}`` -- an MCP-served
    #   call's canonical pair. Read FIRST. The server half never FILLS the
    #   cached server: a KAS MCP tool_call names its server as
    #   ``_meta.kiro.serverName`` (the second ``kiro`` channel), so a request
    #   about that call agrees with the cache, and agreement is a no-op. A
    #   server the preceding tool_call did NOT name -- the cache holds "" for a
    #   built-in, or a different server -- is a disagreement between two
    #   harness-authored frames about one call. It must not be adopted: the
    #   trust flag below records the CACHE hit, and a built-in's tool_call wrote
    #   a hit with an empty server, so a server taken from the request would
    #   inherit that trust and reach the server-keyed grants (the app-own MCP
    #   auto-approve) for a call that is not that server's. On the KAS wire the
    #   disagreement is refused as an unreadable identity (the same KAS-only
    #   refusal mode as an oversized half, H13); a first-class harness, which
    #   never had the fill, ignores the half as it always did.
    # * ``_meta.kiro.toolId`` -- the built-in's id (``read_file``,
    #   ``run_command``), or ``mcp_<server>_<tool>`` for an MCP tool.
    #
    # The frame id becomes ``tool_name`` and so reaches every reader keyed on it.
    # ``mcp_identity_trusted`` stays derived from the cache hits below, and a KAS
    # built-in's tool_call writes a hit with EMPTY names, so the flag is True
    # with no server: the id therefore feeds (1) the deny tier and governance,
    # (2) the host read-only proof in ``hooks._is_host_read_only_builtin`` --
    # deliberately, so a KAS ``read_file`` is free under ``--approval reads``
    # exactly where ``fs_read`` is, and only ids on that read-only allowlist can
    # gain from it -- and (3) ``is_document_writing_tool``'s field scoping when
    # the id is a write tool's. Same trust class as ``toolName`` (harness-authored
    # ``_meta``); what it cannot do is name a SERVER, so every server-keyed grant
    # (the spawn rung, the identified-MCP-call reader) still sees none and does
    # not fire.
    #
    # Read only when the preceding tool_call WROTE the identity cache (a KAS
    # built-in's tool_call writes a hit with an empty name, which the frame id
    # then fills). A request no tool_call preceded -- a sub-agent spawn, whose
    # synthetic toolCallId misses every cache -- is classified by
    # ``kas_consent_tool`` above and by nothing else: its identity is the Crew
    # name rules are written against, given only when every consent field
    # agrees, and a raw spawn-family id or a disagreeing block must not name
    # the request by a spelling no rule carries.
    # Every value taken from the frame is bounded at the point it is retained,
    # each by the bound of what it names: the built-in ``toolId`` by
    # ``_harness_identifier`` (the KAS identifier bound ``_permission_tool_id``
    # applies), the ``mcpTool.identity`` server/tool pair by ``mcp_identity_name``
    # -- exactly what Crew's own MCP tool surface admits, so a name the surface
    # listed (up to 512 characters, any spelling) always has an identity for a
    # per-tool deny to bind to, and an oversized one reads as absent.
    # The preceding tool_call frame's verdict on its own identity halves,
    # carried under the same scoped key as the identity caches: True only when
    # that frame presented a half the bound refused.
    _mcp_identity_unreadable = bool(
        identity_unreadable_cache is not None
        and tool_call_id
        and identity_unreadable_cache.get(_ck, False)
    )
    _frame_meta = params.get("_meta") if _cached_tool is not None else None
    _frame_kiro = _frame_meta.get("kiro") if isinstance(_frame_meta, dict) else None
    if isinstance(_frame_kiro, dict):
        _mcp_tool = _frame_kiro.get("mcpTool")
        _identity = _mcp_tool.get("identity") if isinstance(_mcp_tool, dict) else None
        if isinstance(_identity, dict):
            _id_server = mcp_identity_name(_identity.get("serverName"))
            _id_tool = mcp_identity_name(_identity.get("toolName"))
            # A half that is PRESENT but over the bound names a tool Crew cannot
            # retain; the request is refused on that, never judged under the
            # cached name or the toolId as if the frame had named nothing. Only
            # for a harness handed the store (KAS, H13): the mark is what the
            # handle refuses on, and no other harness has that refusal mode.
            if identity_unreadable_cache is not None and (
                mcp_identity_unreadable(_identity.get("serverName"))
                or mcp_identity_unreadable(_identity.get("toolName"))
            ):
                _mcp_identity_unreadable = True
            if _id_server and _id_server != _mcp_server_name:
                # Never adopted (see the design note above): refused on the
                # harness handed the store, ignored elsewhere.
                if identity_unreadable_cache is not None:
                    _mcp_identity_unreadable = True
            if _id_server and _id_server == _mcp_server_name and not _tool_name and _id_tool:
                _tool_name = _id_tool
        if not _tool_name:
            _tool_name = _harness_identifier(_frame_kiro.get("toolId"))
    # Explicit identity-provenance flag (mirrors _raw_params_trusted): True iff
    # BOTH cache reads above actually HIT — a written entry may legitimately be
    # "" for a non-MCP tool, so the hit is distinguished from a miss by the
    # None default, never by the value. Deliberately derived from the reads
    # themselves, not from cache availability or non-emptiness: a future
    # inline fallback populating the identity fields from the permission
    # payload would leave this False and fail closed in
    # AcpEvent.child_mcp_identity_trusted.
    _mcp_identity_trusted = _cached_server is not None and _cached_tool is not None

    # The path the preceding tool_call's diff CONTENT BLOCK named, cached by the
    # same scoped toolCallId as the params. An edit backend may stream trusted
    # ``rawInput`` with no path key at all and name the target only in the
    # ``{"type": "diff", "path": ...}`` block; without this the permission
    # event's target set is empty and the edit gate (llm_helpers.
    # _edit_target_denial) would have nothing to judge. Same .get() lifecycle
    # as the sibling caches; "" on a miss, which that gate treats as "no
    # proven target" and denies.
    _diff_path = (
        (diff_path_cache.get(_ck) or "") if (diff_path_cache is not None and tool_call_id) else ""
    )

    # The engine's own id for the tool it asks about (KAS writes it into
    # ``_meta.kiro.toolId``). Not read under a gate envelope: the frame's _meta
    # then describes the dialog, not the call the envelope names.
    _harness_tool_id = _permission_tool_id(params) if envelope is None else ""
    # goose and opencode state no such id; the tool name their tool_call frame
    # stated stands in for it, qualified so it is read through that backend's table.
    if not _harness_tool_id and harness_tool_name_cache is not None and tool_call_id:
        _harness_tool_id = qualified_harness_tool_id(
            harness_backend, harness_tool_name_cache.get(_ck, ""), harness_mcp_servers
        )
    # claude-agent-acp states no tool name on the permission request itself, so
    # the kiro-cli built-in a claude read tool stands for comes only from the name
    # its preceding tool_call frame stamped. Only a Side Chat session gets it: the
    # name is what the READ_ONLY gate accepts as a read, and in any other session
    # it would also bind kiro-cli-named deny and governance rules to claude calls.
    _harness_builtin_tool = ""
    if (
        side_read_only
        and harness_backend == ACP_BACKEND_CLAUDE
        and harness_tool_name_cache is not None
        and tool_call_id
    ):
        _harness_builtin_tool = CLAUDE_READ_ONLY_BUILTINS.get(
            harness_tool_name_cache.get(_ck, ""), ""
        )
        # claude states ``search`` for Grep and Glob (captured live, 0.94.0),
        # a kind the READ_ONLY gate does not take. The stamped name proves the
        # call is that search, so the kind is read as ``read`` for those two
        # alone; every other kind is kept as stated.
        if _harness_builtin_tool in ("grep", "glob") and tool_kind == "search":
            tool_kind = "read"
    # goose states its tool on the tool_call frame as an (extension, tool) pair.
    # The pair is read as goose wrote it: the extension from the trusted identity
    # cache and the tool name before any prefix is stripped, so a server named
    # ``developer`` serving ``tree`` (stated ``developer__tree``) is not the
    # builtin. A matched pair is the harness's own extension, not an MCP server,
    # so it carries no server -- as the builtin shell pair already does -- and the
    # READ_ONLY gate proves it from ``harness_builtin_tool``. Side Chat only.
    elif (
        side_read_only
        and harness_backend == ACP_BACKEND_GOOSE
        and harness_tool_name_cache is not None
        and tool_call_id
        and _mcp_identity_trusted
    ):
        _harness_builtin_tool = GOOSE_READ_ONLY_BUILTINS.get(
            (_mcp_server_name, harness_tool_name_cache.get(_ck, "")), ""
        )
        if _harness_builtin_tool:
            _mcp_server_name = ""
            # goose states no ACP kind for its own tools, and its permission
            # request fills the gap with ``other`` (captured live). The proven
            # pair is the read, so that placeholder is read as ``read``; any kind
            # goose does state is kept, and a non-read one still refuses the call.
            if tool_kind in ("", "other"):
                tool_kind = "read"

    # A remote workspace's tools stand in for the engine built-ins Crew unmounted
    # for that session, so they are read as those built-ins, as the claude and
    # goose names are above: the built-in's kind, the shell signal and command for
    # ``run_command`` (titled with the command, as KAS titles its own shell), and
    # the replaced built-in's name, so a deny written for it binds. Only on a runtime
    # that attached the workspace server (``remote_workspace_server``) in a known
    # guest workdir (``remote_workspace_root``), only for a trusted identity, and the
    # server stays named, so nothing reads as local. A relative path argument is
    # anchored at that workdir, where the guest resolves it, so a filesystem rule
    # reads the guest's path and not one under this gateway's working directory.
    if (
        remote_workspace_server
        and posixpath.isabs(remote_workspace_root)
        and _mcp_identity_trusted
        and _mcp_server_name == remote_workspace_server
        and _tool_name in WORKSPACE_TOOL_KINDS
    ):
        tool_kind = WORKSPACE_TOOL_KINDS[_tool_name]
        _harness_builtin_tool = WORKSPACE_DENY_ALIASES[_tool_name][0]
        _resolved_raw_params, _readable = _workspace_call_arguments(
            _resolved_raw_params, _mcp_server_name, _tool_name
        )
        # An envelope for another call: refused before any consumer judges it.
        _mcp_identity_unreadable = _mcp_identity_unreadable or not _readable
        _resolved_raw_params = _anchored_in_workspace(_resolved_raw_params, remote_workspace_root)
        # Tagged by TYPE, which no model input can forge: governance then matches
        # these targets as the guest's POSIX paths, not as this gateway's. A call
        # with no arguments is tagged too, so a diff block's path is still a guest's.
        if _resolved_raw_params is None or isinstance(_resolved_raw_params, dict):
            _resolved_raw_params = GuestPathParams(_resolved_raw_params, root=remote_workspace_root)
        _command = _resolved_raw_params.get("command") if _resolved_raw_params else None
        _runs_shell = tool_kind == "execute"
        is_shell = is_shell or _runs_shell
        if _runs_shell and isinstance(_command, str) and _command:
            title = _redact(_command)
    elif (
        remote_workspace_server
        and posixpath.isabs(remote_workspace_root)
        and (not _mcp_identity_trusted or _mcp_server_name == remote_workspace_server)
        and (_resolved_raw_params is None or isinstance(_resolved_raw_params, dict))
    ):
        # A remote session's call the branch above could not place: an identity
        # this client did not establish, or a workspace-server tool it does not
        # know. Its targets may be the guest's or this gateway's, so they are judged
        # under both readings and must pass every rule under each.
        _resolved_raw_params = GuestPathParams(
            _resolved_raw_params, root=remote_workspace_root, also_local=True
        )
    elif isinstance(_resolved_raw_params, GuestPathParams):
        # Only the branch above may tag a call: it is reached from the runtime's own
        # remote-workspace binding, never from anything the model or guest sent. A
        # tagged object arriving any other way is stripped, so a local call is
        # always matched as this gateway's paths.
        _resolved_raw_params = dict(_resolved_raw_params)

    # dsh's permission frame names only the toolCallId, so which dsh tool a
    # request is for comes only from the name its preceding tool_call frame
    # stated. The gate reads it on a READ_ONLY turn alone, and only once the
    # launch proved stock dsh registers the read tools (``deepseek_read_only_issue``
    # empty); until then the event carries the issue and the name stays unread.
    _deepseek_tool_name = ""
    _deepseek_read_only_issue = ""
    if harness_backend == ACP_BACKEND_DEEPSEEK:
        _deepseek_read_only_issue = deepseek_read_only_issue
        if not deepseek_read_only_issue and harness_tool_name_cache is not None and tool_call_id:
            _deepseek_tool_name = harness_tool_name_cache.get(_ck, "")

    # The agent's stated reason for the call, shown beside the approval. The
    # same agent-authored display text a tool_call frame carries, read the same
    # way: from the params the preceding tool_call cached, else the frame's own.
    _purpose = extract_tool_purpose(_resolved_raw_params) or extract_tool_purpose(
        tool_call.get("rawInput")
    )
    if _purpose:
        _purpose = _redact(_purpose)

    event = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title=title,
        tool_kind=tool_kind,
        raw_params_trusted=_raw_params_trusted,
        shell_classified=cached_shell is not None,
        options=options,
        tool_input=tool_input,
        tool_input_redacted=tool_input_redacted,
        tool_call_id=tool_call_id,
        raw_tool_params=_resolved_raw_params,
        is_shell=is_shell,
        mcp_server_name=_mcp_server_name,
        tool_name=_tool_name,
        mcp_identity_trusted=_mcp_identity_trusted,
        mcp_identity_unreadable=_mcp_identity_unreadable,
        # The unreadable-identity store is handed to this parser only for a
        # member of ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL`` (KAS; the
        # handle's ``_identity_unreadable_cache_for_wire`` and the client gate
        # it on that set), so it is also the one signal that a bare built-in
        # ``tool_name`` here is a KAS id the gate may read under its kiro-cli
        # policy name. Every other harness is handed ``None`` and gets no fold.
        kas_builtin_ids=identity_unreadable_cache is not None,
        diff_path=_diff_path,
        spawn_target=_spawn_target,
        harness_tool_id=_harness_tool_id,
        harness_builtin_tool=_harness_builtin_tool,
        deepseek_tool_name=_deepseek_tool_name,
        deepseek_read_only_issue=_deepseek_read_only_issue,
        tool_purpose=_purpose,
    )
    return event, recorded
