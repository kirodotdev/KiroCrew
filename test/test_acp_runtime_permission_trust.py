"""Permission trust on the shared ``AcpRuntime`` path.

Child-permission fidelity and MCP identity, the tool-kind classification cache,
the fail-closed gate in front of fidelity-unaware consumers, and the bound on
in-flight permission answers.
"""

import asyncio
import json

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _drain,
    _drain_audits,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.testing.wait import await_until


async def _drain_answers(rt) -> None:
    """Await the retained off-loop permission answers (each writes stdin under
    the transport's write lock, so a burst completes over several turns)."""
    if rt._answer_tasks:
        await asyncio.gather(*list(rt._answer_tasks), return_exceptions=True)


def test_child_low_fidelity_requires_structured_security_context():
    """A rendered-diff tool_input alone is NOT fidelity: the child gate
    requires cache-provenance structured params, a resolved shell
    classification, and (for shells) a recoverable command."""
    from kiro_crew.acp.types import AcpEvent

    # Child edit refinement: diff text cached, no structured params → LOW.
    ev = AcpEvent(kind="permission_request", sub_session_id="child-a", tool_input="--- a\n+++ b")
    assert ev.child_low_fidelity is True
    # Child shell with params but unrecoverable command → LOW.
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        is_shell=True,
        raw_tool_params={"note": "no command key"},
        raw_params_trusted=True,
        shell_classified=True,
    )
    assert ev.child_low_fidelity is True
    # Inline (agent-authored) params without cache provenance → LOW even
    # with a recoverable command.
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        is_shell=True,
        raw_tool_params={"command": "sha256sum README.md"},
        raw_params_trusted=False,
        shell_classified=True,
    )
    assert ev.child_low_fidelity is True
    # Unresolved shell classification (cache miss defaults is_shell=False) → LOW.
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        raw_tool_params={"path": "/tmp/x"},
        raw_params_trusted=True,
        shell_classified=False,
    )
    assert ev.child_low_fidelity is True
    # Child with full provenance context → parity (not low).
    ev = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        is_shell=True,
        raw_tool_params={"command": "sha256sum README.md"},
        raw_params_trusted=True,
        shell_classified=True,
    )
    assert ev.child_low_fidelity is False
    # Non-child events are never low-fidelity.
    ev = AcpEvent(kind="permission_request")
    assert ev.child_low_fidelity is False


def test_child_mcp_identity_trusted_isolates_verified_identity():
    """The identity half of the fidelity split: verified server/tool pair on a
    child event whose ARGUMENTS never reached the cache. Each requirement is
    individually load-bearing (fail-closed on its own cache miss)."""
    from kiro_crew.acp.types import AcpEvent

    def _ev(**overrides):
        base: dict = dict(
            kind="permission_request",
            sub_session_id="child-a",
            shell_classified=True,
            is_shell=False,
            mcp_server_name="example-server",
            tool_name="get-item",
            mcp_identity_trusted=True,
        )
        base.update(overrides)
        return AcpEvent(**base)

    # The issue's shape: remote MCP tool_call streamed no rawInput — low
    # fidelity (args unverified) but identity verified.
    ev = _ev()
    assert ev.child_low_fidelity is True
    assert ev.child_mcp_identity_trusted is True
    # A parent event never needs the split.
    assert _ev(sub_session_id="").child_mcp_identity_trusted is False
    # An unresolved shell classification is NOT disqualifying: a backend may
    # omit `kind` on its MCP frames, and the trusted transport identity is
    # itself proof the call is MCP-served and not a host shell command. The
    # composite stays low-fidelity, so content-matching auto-approval
    # (title-keyed auto_approve_tools) remains gated for such an event.
    kindless = _ev(shell_classified=False)
    assert kindless.child_mcp_identity_trusted is True
    assert kindless.child_low_fidelity is True
    # A resolved SHELL tool: its deny gates need the command bytes this
    # event lacks — never identity-eligible.
    assert _ev(is_shell=True).child_mcp_identity_trusted is False
    # Cache-missed identity halves are each fail-closed.
    assert _ev(mcp_server_name="").child_mcp_identity_trusted is False
    assert _ev(tool_name="").child_mcp_identity_trusted is False
    # THE HARDENING: non-empty identity fields alone are NOT provenance. An
    # event populated by any path that did not earn the explicit flag (e.g. a
    # future inline/agent-authored fallback) stays untrusted.
    assert _ev(mcp_identity_trusted=False).child_mcp_identity_trusted is False
    # Full-fidelity child: the property may hold too, and grant callers use
    # ``child_unconditional_grant_eligible`` — both True is consistent, not
    # contradictory.
    full = _ev(raw_params_trusted=True, raw_tool_params={"itemId": "i-1"})
    assert full.child_low_fidelity is False
    assert full.child_mcp_identity_trusted is True


def test_mcp_identity_trusted_defaults_false():
    """The provenance flag is opt-in at trusted population sites only: a bare
    construction (the shape any future untrusted path would produce) reads
    False."""
    from kiro_crew.acp.types import AcpEvent

    assert AcpEvent(kind="permission_request").mcp_identity_trusted is False


def test_child_unconditional_grant_eligible_matches_consumer_shapes():
    """The hoisted grant-eligibility property is exactly
    ``not child_low_fidelity or child_mcp_identity_trusted`` — pinned across
    every fidelity/identity combination the three approval surfaces
    (dashboard runner, Slack gateway, subagent manager) can see."""
    from kiro_crew.acp.types import AcpEvent

    # Full-fidelity child (low_fidelity False): eligible regardless of identity.
    full = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        raw_params_trusted=True,
        raw_tool_params={"k": "v"},
    )
    assert full.child_low_fidelity is False
    assert full.child_unconditional_grant_eligible is True
    # Low-fidelity child with verified identity: eligible.
    identity = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        mcp_server_name="example-server",
        tool_name="get-item",
        mcp_identity_trusted=True,
    )
    assert identity.child_low_fidelity is True
    assert identity.child_unconditional_grant_eligible is True
    # Low-fidelity child, identity unverified: NOT eligible.
    blind = AcpEvent(kind="permission_request", sub_session_id="child-a")
    assert blind.child_low_fidelity is True
    assert blind.child_unconditional_grant_eligible is False
    # Non-empty identity WITHOUT the provenance flag: still NOT eligible (the
    # hardening the flag buys, seen from the grant surface).
    forged = AcpEvent(
        kind="permission_request",
        sub_session_id="child-a",
        shell_classified=True,
        is_shell=False,
        mcp_server_name="example-server",
        tool_name="get-item",
    )
    assert forged.child_low_fidelity is True
    assert forged.child_unconditional_grant_eligible is False
    # Non-child events are always eligible.
    assert AcpEvent(kind="permission_request").child_unconditional_grant_eligible is True
    # Exhaustive equivalence against the un-hoisted expression.
    for lf_overrides in (
        {},  # low fidelity (no trusted params)
        {"raw_params_trusted": True, "raw_tool_params": {"k": "v"}},  # full fidelity
    ):
        for id_overrides in (
            {},
            {
                "mcp_server_name": "example-server",
                "tool_name": "get-item",
                "mcp_identity_trusted": True,
            },
        ):
            ev = AcpEvent(
                kind="permission_request",
                sub_session_id="child-a",
                shell_classified=True,
                is_shell=False,
                **lf_overrides,
                **id_overrides,
            )
            assert ev.child_unconditional_grant_eligible == (
                not ev.child_low_fidelity or ev.child_mcp_identity_trusted
            )


def test_remote_mcp_empty_rawinput_keeps_identity_through_dispatch():
    """End-to-end through the real _dispatch functions: a remote MCP server's
    tool_call frame with EMPTY/absent rawInput leaves the params cache empty
    (low fidelity) while the _meta.kiro identity still reaches the permission
    event's trusted fields — the split the grant paths rely on."""
    from kiro_crew.acp._dispatch import build_permission_event, parse_session_update

    for raw_input_shape in ({}, None):
        caches: dict = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        child_sid, tcid = "child-a", "tc-1"
        update = {
            "sessionUpdate": "tool_call",
            "toolCallId": tcid,
            "title": "@example-server/get-item",
            "kind": "other",
            "_meta": {"kiro": {"mcpServerName": "example-server", "toolName": "get-item"}},
        }
        if raw_input_shape is not None:
            update["rawInput"] = raw_input_shape
        parse_session_update(update, cache_scope=child_sid, **caches)

        class _Msg:
            id = 90
            method = "session/request_permission"
            params = {
                "sessionId": child_sid,
                "toolCall": {
                    "toolCallId": tcid,
                    "title": "@example-server/get-item",
                    "input": {"itemId": "item-0001"},
                },
                "options": [
                    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            }

        event, _ = build_permission_event(_Msg(), cache_scope=child_sid, **caches)
        event.sub_session_id = child_sid
        assert event.raw_params_trusted is False
        assert event.child_low_fidelity is True
        assert event.mcp_server_name == "example-server"
        assert event.tool_name == "get-item"
        # The real permission builder earns the provenance flag: the identity
        # pair resolved from the origin-scoped caches, never inline.
        assert event.mcp_identity_trusted is True
        assert event.child_mcp_identity_trusted is True
        assert event.child_unconditional_grant_eligible is True


def test_permission_event_cache_miss_does_not_earn_identity_flag():
    """The provenance flag is HIT-derived, never availability-derived: a
    permission frame whose toolCallId has NO cache entry (wired caches, no
    preceding tool_call) must read mcp_identity_trusted False — the flag
    reports where the values CAME FROM, so a future inline fallback that
    populates the identity fields on a miss stays untrusted."""
    from kiro_crew.acp._dispatch import build_permission_event

    class _Msg:
        id = 91
        method = "session/request_permission"
        params = {
            "sessionId": "child-a",
            "toolCall": {
                "toolCallId": "tc-never-seen",
                "title": "@example-server/get-item",
                "input": {"itemId": "item-0001"},
            },
            "options": [{"optionId": "allow_once", "name": "Allow", "kind": "allow_once"}],
        }

    event, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
        cache_scope="child-a",
    )
    assert event.mcp_server_name == ""
    assert event.tool_name == ""
    assert event.mcp_identity_trusted is False
    # Asymmetric hit: BOTH reads must hit — a lone server-name entry (a
    # partial/older writer) earns nothing.
    event2, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={"child-a|tc-never-seen": "example-server"},
        tool_name_cache={},
        cache_scope="child-a",
    )
    assert event2.mcp_server_name == "example-server"
    assert event2.tool_name == ""
    assert event2.mcp_identity_trusted is False
    event3, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={"child-a|tc-never-seen": "get-item"},
        cache_scope="child-a",
    )
    assert event3.mcp_server_name == ""
    assert event3.tool_name == "get-item"
    assert event3.mcp_identity_trusted is False
    # The None-vs-"" distinction is the load-bearing half: a written entry may
    # legitimately be "" (host shell/builtin tool_call caches "" for both), and
    # that HIT still earns the flag — deriving trust from value non-emptiness
    # instead of the hit is exactly the conflation the flag exists to remove.
    event4, _ = build_permission_event(
        _Msg(),
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={"child-a|tc-never-seen": ""},
        tool_name_cache={"child-a|tc-never-seen": ""},
        cache_scope="child-a",
    )
    assert event4.mcp_server_name == ""
    assert event4.tool_name == ""
    assert event4.mcp_identity_trusted is True


def test_missing_kind_is_not_a_resolved_shell_classification():
    """A tool_call whose `kind` never arrived must NOT cache a shell
    classification: the miss-default False would otherwise read as a RESOLVED
    non-shell on the later permission frame (shell_classified=True), skipping
    the low-fidelity downgrade without any classification having happened."""
    from kiro_crew.acp._dispatch import _build_tool_call_event, build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    raw_cache: dict[str, dict] = {}
    # No `kind` key at all — classification unresolved.
    _build_tool_call_event(
        {"title": "Doing something", "toolCallId": "tc-nk", "rawInput": {"path": "/tmp/x"}},
        None,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert "tc-nk" not in shell_cache  # unresolved, NOT cached False

    msg = JsonRpcMessage.from_dict(
        {
            "id": 7,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "child-a",
                "toolCall": {"toolCallId": "tc-nk", "title": "Doing something"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, shell_cache=shell_cache, raw_params_cache=raw_cache)
    event.sub_session_id = "child-a"
    assert event.shell_classified is False
    assert event.child_low_fidelity is True  # downgrade applies

    # An explicit kind DOES resolve (even a non-shell one).
    _build_tool_call_event(
        {"title": "Reading", "kind": "read", "toolCallId": "tc-rk"},
        None,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert shell_cache.get("tc-rk") is False  # resolved non-shell


def test_trusted_mcp_transport_earns_identity_trust_without_a_classification():
    """A kind-less frame whose `_meta.kiro.mcpServerName` is populated earns the
    IDENTITY-trusted half only: the transport discriminator is backend-authored,
    and an MCP-served tool is not a host shell command, so unconditional grant
    paths (parent_policy=auto, session trust-all) may honor the call. It must
    NOT mint a resolved shell classification: shell_classified stays False and
    child_low_fidelity stays True, keeping every content-matching auto-approve
    path (title-keyed auto_approve_tools) gated against the agent-authored
    title."""
    from kiro_crew.acp._dispatch import _build_tool_call_event, build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    raw_cache: dict[str, dict] = {}
    server_cache: dict[str, str] = {}
    name_cache: dict[str, str] = {}
    ev = _build_tool_call_event(
        {
            "title": "Asking the knowledge service",
            "toolCallId": "tc-mcp",
            "rawInput": {"question": "why"},
            "_meta": {"kiro": {"mcpServerName": "kb", "toolName": "ask"}},
        },
        None,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
        mcp_server_name_cache=server_cache,
        tool_name_cache=name_cache,
    )
    assert ev.is_shell is False
    assert "tc-mcp" not in shell_cache  # no kind -> no classification minted

    msg = JsonRpcMessage.from_dict(
        {
            "id": 11,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "child-a",
                "toolCall": {"toolCallId": "tc-mcp", "title": "Asking the knowledge service"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(
        msg,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
        mcp_server_name_cache=server_cache,
        tool_name_cache=name_cache,
    )
    event.sub_session_id = "child-a"
    assert event.shell_classified is False
    assert event.is_shell is False
    assert event.mcp_identity_trusted is True
    # The split: identity trusted (unconditional grants may honor it) ...
    assert event.child_mcp_identity_trusted is True
    assert event.child_unconditional_grant_eligible is True
    # ... while the composite stays low-fidelity (title matching stays gated).
    assert event.child_low_fidelity is True


def test_trusted_mcp_transport_never_waives_a_reported_shell_kind():
    """The transport identity may only ever vouch for a non-shell call, never
    waive a shell check: an execute-kind frame still caches True even with a
    server name, so the command-bytes gates keep firing -- and the identity
    split stays closed for it."""
    from kiro_crew.acp._dispatch import _build_tool_call_event, build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    server_cache: dict[str, str] = {}
    name_cache: dict[str, str] = {}
    _build_tool_call_event(
        {
            "title": "Running: ls",
            "kind": "execute",
            "toolCallId": "tc-both",
            "_meta": {"kiro": {"mcpServerName": "kb", "toolName": "ask"}},
        },
        None,
        shell_cache=shell_cache,
        mcp_server_name_cache=server_cache,
        tool_name_cache=name_cache,
    )
    assert shell_cache.get("tc-both") is True

    msg = JsonRpcMessage.from_dict(
        {
            "id": 13,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "child-a",
                "toolCall": {"toolCallId": "tc-both", "title": "Running: ls"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(
        msg,
        shell_cache=shell_cache,
        mcp_server_name_cache=server_cache,
        tool_name_cache=name_cache,
    )
    event.sub_session_id = "child-a"
    assert event.is_shell is True
    assert event.child_mcp_identity_trusted is False


def test_inline_mcp_server_name_on_a_permission_frame_earns_no_classification():
    """The agent-reachable permission payload is not a provenance channel: an
    inline `_meta`/`mcpServerName` there cannot manufacture the identity trust
    that only the preceding tool_call's cache write can grant."""
    from kiro_crew.acp._dispatch import build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    msg = JsonRpcMessage.from_dict(
        {
            "id": 12,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "child-a",
                "toolCall": {
                    "toolCallId": "tc-forged",
                    "title": "Asking the knowledge service",
                    "_meta": {"kiro": {"mcpServerName": "kb", "toolName": "ask"}},
                },
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, shell_cache=shell_cache)
    event.sub_session_id = "child-a"
    assert "tc-forged" not in shell_cache
    assert event.shell_classified is False
    assert event.mcp_identity_trusted is False
    assert event.child_mcp_identity_trusted is False
    assert event.child_unconditional_grant_eligible is False
    assert event.child_low_fidelity is True


def test_shared_permission_event_carries_redaction_provenance_without_secret():
    """The shared-runtime cache must remember that its display input changed.

    Re-redacting the already-clean permission event cannot recover this fact;
    command trust needs the separate boolean while the removed bytes stay out
    of the event's display input.
    """
    from kiro_crew.acp._dispatch import _build_tool_call_event, build_permission_event
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    input_cache: dict[str, str] = {}
    redacted_cache: dict[str, bool] = {}
    shell_cache: dict[str, bool] = {}
    _build_tool_call_event(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-secret",
            "title": "Run Command",
            "kind": "execute",
            "rawInput": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
        },
        input_cache,
        shell_cache=shell_cache,
        tool_input_redacted_cache=redacted_cache,
    )
    assert redacted_cache["tc-secret"] is True

    msg = JsonRpcMessage.from_dict(
        {
            "id": 71,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"toolCallId": "tc-secret", "title": "Run Command"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(
        msg,
        tool_input_cache=input_cache,
        tool_input_redacted_cache=redacted_cache,
        shell_cache=shell_cache,
    )

    assert event.tool_input_redacted is True
    assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_input
    assert "[REDACTED: credential]" in event.tool_input
    assert redacted_cache["tc-secret"] is True
    repeated, _ = build_permission_event(
        msg,
        tool_input_cache=input_cache,
        tool_input_redacted_cache=redacted_cache,
        shell_cache=shell_cache,
    )
    assert repeated.tool_input_redacted is True
    assert repeated.tool_input == event.tool_input


def test_refinement_fills_raw_params_cache_for_following_permission():
    """A tool_call_update refinement carrying rawInput must make the FOLLOWING
    permission frame full-provenance (raw_params_trusted=True) — this is the
    path that keeps backends streaming an empty initial rawInput out of the
    low-fidelity downgrade. Pins the required frame ordering explicitly."""
    from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    raw_cache: dict[str, dict] = {}
    input_cache: dict[str, str] = {}
    # Initial tool_call with EMPTY rawInput but a real kind.
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-rf",
            "title": "Running: sha256sum x",
            "kind": "execute",
        },
        tool_input_cache=input_cache,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert "tc-rf" not in raw_cache
    # Refinement supplies the complete params.
    parse_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-rf",
            "rawInput": {"command": "sha256sum x"},
        },
        tool_input_cache=input_cache,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
    )
    assert raw_cache.get("tc-rf") == {"command": "sha256sum x"}

    msg = JsonRpcMessage.from_dict(
        {
            "id": 8,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "child-a",
                "toolCall": {"toolCallId": "tc-rf", "title": "Running: sha256sum x"},
                "options": [],
            },
        }
    )
    event, _ = build_permission_event(msg, shell_cache=shell_cache, raw_params_cache=raw_cache)
    event.sub_session_id = "child-a"
    assert event.raw_params_trusted is True
    assert event.shell_classified is True
    assert event.child_low_fidelity is False


@pytest.mark.asyncio
async def test_fidelity_unaware_consumer_gate_rejects_and_audits():
    """The fail-close choke point protecting every non-dashboard consumer:
    a low-fidelity child permission request reaching _dispatch_events on a
    handle whose consumer never opted in must be REJECTED (answered, never
    yielded as a permission event) and SEL-audited."""
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
    )

    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    assert handle.child_fidelity_aware is False

    audited: list[tuple[object, str, str]] = []
    handle._audit_handle_reject = (  # type: ignore[method-assign]
        lambda request_id, title, error, sub_session_id="": audited.append(
            (request_id, title, error)
        )
    )

    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        # Announce the child so the frame routes to the owner's queue.
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        # Child permission frame with NO preceding tool_call → low fidelity.
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 77,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "child-a",
                    "toolCall": {"toolCallId": "tc-77", "title": "Running: rm -rf /"},
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")

        kinds = [ev.kind for ev in events]
        assert EVENT_PERMISSION_REQUEST not in kinds  # never yielded
        assert audited and audited[0][2] == "child_low_fidelity_unaware_consumer"
        # The reject was actually SENT (answered, not dropped).
        answer = None
        for call in proc.stdin.write.call_args_list:
            frame = json.loads(call.args[0].decode())
            if frame.get("id") == 77 and "result" in frame:
                answer = frame
        assert answer is not None
        assert answer["result"]["outcome"]["outcome"] in ("selected", "cancelled")
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_aware_consumer_receives_kindless_mcp_child_permission():
    """The end of the auto-deny, driven through the handle's own dispatch
    loop: a consumer that opted into the child-fidelity contract (as
    `kirocrew chat` now does) receives the low-fidelity permission event for
    a kindless MCP child call -- yielded with the trusted transport identity
    attached -- instead of the handle rejecting it as
    `child_low_fidelity_unaware_consumer`."""
    from kiro_crew.acp.types import (
        EVENT_PERMISSION_REQUEST,
        METHOD_REQUEST_PERMISSION,
        METHOD_SESSION_UPDATE,
    )

    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle.child_fidelity_aware = True

    audited: list[tuple[object, str, str]] = []
    handle._audit_handle_reject = (  # type: ignore[method-assign]
        lambda request_id, title, error, sub_session_id="": audited.append(
            (request_id, title, error)
        )
    )

    task = await _start_reader(rt)
    try:
        events = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)
                if ev.kind == EVENT_PERMISSION_REQUEST:
                    # Answer it so the turn can end.
                    await handle.reject_tool(ev.request_id)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        # The child's tool_call: NO kind, backend `_meta.kiro` identity only.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "child-a",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-88",
                        "title": "Asking the knowledge service",
                        "rawInput": {"question": "why"},
                        "_meta": {"kiro": {"mcpServerName": "kb", "toolName": "ask"}},
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 88,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "sessionId": "child-a",
                    "toolCall": {"toolCallId": "tc-88", "title": "Asking the knowledge service"},
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")

        perm = [ev for ev in events if ev.kind == EVENT_PERMISSION_REQUEST]
        assert perm, "the permission event never reached the aware consumer"
        ev = perm[0]
        # Low fidelity is preserved (title matching stays gated elsewhere) --
        # the aware consumer receives it rather than the handle rejecting it.
        assert ev.child_low_fidelity is True
        assert ev.mcp_identity_trusted is True
        assert ev.mcp_server_name == "kb"
        assert ev.tool_name == "ask"
        assert not audited  # the fail-close gate never fired
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_answer_task_cap_marks_dead_instead_of_growing_unbounded():
    """A backend that floods permission frames while never reading stdin
    blocks each answer task on drain(). The in-flight set must be BOUNDED,
    and the overflow must be neither a hang nor an unaudited drop: past the
    cap the runtime is marked dead (teardown resolves EVERY pending wait)
    and the denial is SEL-audited."""
    rt, _, _ = _make_runtime()
    rt._max_answer_tasks = 3

    import asyncio as _asyncio

    _never = _asyncio.Event()

    async def _blocked_answer(msg, session_id, *, reason="x"):
        await _never.wait()  # simulates send_response stuck on drain()

    rt._answer_unroutable_permission = _blocked_answer  # type: ignore[method-assign]
    audited: list[str] = []
    rt._audit_denied_off_loop = (  # type: ignore[method-assign]
        lambda msg, session_id, reason, title=None: audited.append(reason)
    )
    dead: list[str] = []

    def _fake_mark_dead(reason, **_kw):
        dead.append(reason)
        rt._dead = True  # mirror the real _mark_dead contract

    rt._mark_dead = _fake_mark_dead  # type: ignore[method-assign]

    def _frame(i):
        return JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 500 + i,
                "method": "session/request_permission",
                "params": {"sessionId": f"child-{i}", "options": []},
            }
        )

    rt._answer_cap_wait_secs = 0.05
    for i in range(5):
        await rt._spawn_answer_task(_frame(i), f"child-{i}")
    await _asyncio.sleep(0)  # let the tasks start (and block)

    assert len(rt._answer_tasks) == 3  # capped, not 5
    # Overflow was audited and escalated to mark-dead — not silently dropped.
    # Only the FIRST overflow frame audits + marks dead; once dead, further
    # frames are gated out entirely (no audit-task growth on a dead runtime).
    assert audited == ["answer_task_cap_runtime_dead"]
    assert dead and "cap" in dead[0]
    # A frame arriving after death spawns NOTHING.
    await rt._spawn_answer_task(_frame(9), "child-9")
    await _asyncio.sleep(0)
    assert len(rt._answer_tasks) == 3
    assert audited == ["answer_task_cap_runtime_dead"]
    _never.set()
    await _asyncio.sleep(0)


@pytest.mark.asyncio
async def test_capacity_freed_but_runtime_died_still_audits_the_refusal():
    """A waiter parked at the cap can be woken by a completing answer AND find
    the runtime condemned by a concurrent waiter in the same moment. Capacity
    was freed, so this is not the timeout path, but admission still fails — and
    a refused permission decision must leave a SEL record either way."""
    rt, _, _ = _make_runtime()
    rt._max_answer_tasks = 1

    import asyncio as _asyncio

    audited: list[str] = []
    rt._audit_denied_off_loop = (  # type: ignore[method-assign]
        lambda msg, session_id, reason, title=None: audited.append(reason)
    )

    release = _asyncio.Event()

    async def _held() -> None:
        await release.wait()

    holder = _asyncio.ensure_future(_held())
    rt._answer_tasks.add(holder)

    frame = JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": 907,
            "method": "session/request_permission",
            "params": {"sessionId": "child-x", "options": []},
        }
    )

    async def _condemn_then_release() -> None:
        await _asyncio.sleep(0)
        rt._dead = True  # a sibling waiter's _mark_dead lands first
        release.set()

    condemner = _asyncio.ensure_future(_condemn_then_release())
    admitted = await rt._wait_for_answer_capacity(
        frame,
        request_kind="permission",
        session_id="child-x",
        audit_reason="answer_task_cap_runtime_dead",
    )
    await condemner

    assert admitted is False
    assert audited == ["answer_task_cap_runtime_dead"], "the refusal must be audited"


@pytest.mark.asyncio
async def test_sel_audit_tasks_do_not_count_toward_answer_cap():
    """SEL audit tasks are short-lived thread offloads; a burst of them must
    never satisfy the flood cap and trip a false mark_dead that kills every
    multiplexed session."""
    rt, _, _ = _make_runtime()
    rt._max_answer_tasks = 2

    import asyncio as _asyncio

    # Simulate pending SEL audits filling the AUDIT set well past the cap.
    for _ in range(5):
        _t = _asyncio.ensure_future(_asyncio.sleep(30))
        rt._audit_tasks.add(_t)
        _t.add_done_callback(rt._audit_tasks.discard)

    dead: list[str] = []
    rt._mark_dead = lambda reason, **_kw: dead.append(reason)  # type: ignore[method-assign]

    answered: list[object] = []

    async def _quick_answer(msg, session_id, *, reason="x"):
        answered.append(msg.id)

    rt._answer_unroutable_permission = _quick_answer  # type: ignore[method-assign]

    await rt._spawn_answer_task(
        JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 700,
                "method": "session/request_permission",
                "params": {"sessionId": "child-x", "options": []},
            }
        ),
        "child-x",
    )
    await _asyncio.sleep(0)

    assert answered == [700]  # answered normally
    assert dead == []  # audit backlog did NOT trip the cap
    for _t in list(rt._audit_tasks):
        _t.cancel()
    await _asyncio.sleep(0)


@pytest.mark.asyncio
async def test_buffered_burst_with_responsive_backend_does_not_trip_cap():
    """129+ frames can be buffered so readline() never suspends; the reader
    must still yield between spawns so QUICK answers drain and a responsive
    backend is not falsely marked dead by the flood cap. (The cap fires only
    when answers genuinely cannot complete — a wedged pipe.)"""
    rt, reader, proc = _make_runtime()
    _register(rt, "sA")
    rt._max_answer_tasks = 4
    dead: list[str] = []
    rt._mark_dead = lambda reason, **_kw: dead.append(reason)  # type: ignore[method-assign]

    # Buffer MORE frames than the cap before the reader runs at all.
    for i in range(10):
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 800 + i,
                "method": "session/request_permission",
                "params": {
                    "sessionId": f"child-{i}",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
    task = await _start_reader(rt)
    try:
        await _drain(reader)
        # The answers are retained tasks that take the stdin write lock in
        # turn; wait on that observable condition rather than a turn count.
        await _drain_answers(rt)
        await _drain_audits(rt)
        # Responsive backend (writes complete immediately): every request
        # answered, cap never tripped.
        assert dead == []
        answered = {
            json.loads(c.args[0].decode()).get("id")
            for c in proc.stdin.write.call_args_list
            if "result" in json.loads(c.args[0].decode())
        }
        assert {800 + i for i in range(10)} <= answered
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


def test_cross_session_toolcallid_replay_does_not_inherit_provenance():
    """A child session reusing a PARENT's toolCallId must NOT inherit the
    parent's trusted provenance (raw params / shell class / MCP identity) —
    cache keys are origin-scoped, so cross-session replay misses and the
    request stays low fidelity, while a SAME-origin repeat frame still
    resolves."""
    from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
    from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION

    shell_cache: dict[str, bool] = {}
    raw_cache: dict[str, dict] = {}
    input_cache: dict[str, str] = {}

    # Parent's tool_call writes trusted provenance under the PARENT scope.
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-shared",
            "title": "Reading README.md",
            "kind": "read",
            "rawInput": {"path": "README.md"},
        },
        tool_input_cache=input_cache,
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
        cache_scope="parent-session",
    )

    def _perm(req_id):
        return JsonRpcMessage.from_dict(
            {
                "id": req_id,
                "method": METHOD_REQUEST_PERMISSION,
                "params": {
                    "toolCall": {"toolCallId": "tc-shared", "title": "Reading README.md"},
                    "options": [],
                },
            }
        )

    # CHILD replays the same toolCallId under its own scope: provenance MISS.
    child_ev, _ = build_permission_event(
        _perm(1),
        shell_cache=shell_cache,
        raw_params_cache=raw_cache,
        cache_scope="child-session",
    )
    child_ev.sub_session_id = "child-session"
    assert child_ev.raw_params_trusted is False
    assert child_ev.shell_classified is False
    assert child_ev.child_low_fidelity is True  # downgrade applies

    # SAME-origin permission frame (and a repeat of it) still resolves.
    for req_id in (2, 3):
        parent_ev, _ = build_permission_event(
            _perm(req_id),
            shell_cache=shell_cache,
            raw_params_cache=raw_cache,
            cache_scope="parent-session",
        )
        assert parent_ev.raw_params_trusted is True
        assert parent_ev.shell_classified is True
