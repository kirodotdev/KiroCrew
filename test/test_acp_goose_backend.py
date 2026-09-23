"""The goose backend: vocabulary, the routing seed, and the read-back.

The read-back is what this file mostly exists for. Every other ratchet in the tree
already holds goose to a declaration -- that it has a label, an install probe, a
credential leaf, a column in every bucket -- and a declaration is exactly the thing
that can be true while the behaviour is absent. What no other test reaches is
:meth:`AcpClient._verify_goose_routing`: whether a session whose mode is not the
required one is actually REFUSED, on both the path that creates a session and the path
that restores one.

That second path is the one with teeth. The environment seed governs a session goose
creates; it does not govern one goose already has. So a resumed session can come back
in the auto-approving mode with the seed still in the child's environment, and the only
thing standing between that and a session where no tool call reaches the host gate is
the read-back running on the load response too.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import tool_gate as gate

GOOSE = acp_backends.ACP_BACKEND_GOOSE

#: This harness's launch record. The override variable, the binary and the argv
#: tail are read from here rather than from module constants, because the record is
#: where they are declared and a second spelling could drift from it.
_GOOSE_LAUNCH = sdk_backends.launch_for(sdk_backends.ACP_BACKEND_GOOSE)


# ── Vocabulary ──


def test_goose_is_a_known_and_selectable_backend() -> None:
    """Known gates the kwarg; selectable is what the switch may persist."""
    assert GOOSE == "goose"
    assert GOOSE in sdk_backends.ACP_BACKENDS_KNOWN
    assert GOOSE in sdk_backends.BASELINE_SELECTABLE_BACKENDS


def test_goose_routing_is_verified_seeded_settings() -> None:
    """Not UNVERIFIED, and the difference is a measurement rather than a preference.

    ``register_selectable_backend`` refuses UNVERIFIED with no opt-out, so this is the
    membership that lets the backend be offered at all. It is asserted against the
    enum member rather than its string so a rename cannot silently pass.
    """
    assert sdk_backends.routing_for(GOOSE) is sdk_backends.Routing.VERIFIED_SEEDED_SETTINGS
    assert sdk_backends.routing_for(GOOSE) in gate.ENFORCED_ROUTINGS


def test_the_seeded_setting_names_the_environment_variable_and_the_required_mode() -> None:
    """One pair names what is supplied, what is read back, and what a refusal reports.

    The KEY is an environment variable rather than a config field, which is the whole
    reason this harness needs no file: goose resolves the mode from its environment
    ABOVE its own config file.
    """
    assert sdk_backends.permission_setting_for(GOOSE) == ("GOOSE_MODE", "approve")


def test_goose_carries_its_mcp_servers_on_the_session_array() -> None:
    """Membership here is what gives a goose session Crew's own tools."""
    assert GOOSE in sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY


def test_goose_is_not_in_the_capability_sets_it_has_no_evidence_for() -> None:
    """Absence is a decision here, not an oversight, so it is pinned as one.

    Each of these would make a claim the wire does not support: a shared process, a
    steer method, or an internal sandbox that would displace the credential mask
    this harness depends on.

    ``ACP_BACKENDS_COMPACT`` is deliberately NOT in this list, and the sibling
    test below says why: goose publishes ``compact`` among the built-ins its
    ``available_commands_update`` carries and dispatches it before any model
    turn, so its membership rests on evidence rather than on resemblance.
    ``test_compaction_other_backends`` holds that evidence.
    """
    assert GOOSE not in sdk_backends.ACP_BACKENDS_ACP_RUNTIME
    assert GOOSE not in sdk_backends.ACP_BACKENDS_STEER
    assert GOOSE not in sdk_backends.ACP_BACKENDS_INTERNAL_SANDBOX
    assert GOOSE not in sdk_backends.ACP_BACKENDS_SESSION_SHARING


def test_goose_compaction_is_unclassified_pending_a_capture() -> None:
    """Absence as a decision, and the decision is about the evidence CLASS.

    goose 1.50.1 -- the release this harness pins as its VERIFIED RANGE --
    dispatches ``/compact`` out of ``Agent::reply`` through ``execute_command``
    before a model turn starts, and its own ``command_starts_turn("/compact")``
    is false, so its SOURCE says the compaction finishes inside the prompt turn.
    What it lacks is a driven capture, which is the bar
    ``ACP_BACKENDS_COMPACT`` holds its members to.

    So goose is in none of the three compaction sets, and that is the position
    every arm has to agree on: it is not offered a manual ``/compact``, it is not
    claimed to manage its own context, and it is not recycled.
    """
    assert GOOSE not in sdk_backends.ACP_BACKENDS_COMPACT
    assert GOOSE not in sdk_backends.ACP_BACKENDS_INLINE_COMPACTION
    harness_managed = getattr(sdk_backends, "ACP_BACKENDS_HARNESS_MANAGED_COMPACTION", None)
    assert harness_managed is not None, "agent_sdk.backends declares no harness-managed set"
    assert GOOSE not in harness_managed
    recycle = getattr(sdk_backends, "ACP_BACKENDS_CONTEXT_RECYCLE", None)
    assert recycle is not None, "agent_sdk.backends declares no context-recycle set"
    assert GOOSE not in recycle


def test_goose_serves_session_load_so_it_needs_no_load_workaround() -> None:
    """It answers ``session/load`` and NOT ``session/resume``, so no set keys a workaround.

    Pinned because the sibling harness onboarded before it is the mirror image, and a
    membership copied across on resemblance would send this one a method it refuses.
    """
    load_without_modes = getattr(sdk_backends, "ACP_BACKENDS_LOAD_WITHOUT_MODES", frozenset())
    assert GOOSE not in load_without_modes
    resume_without_load = getattr(sdk_backends, "ACP_BACKENDS_RESUME_WITHOUT_LOAD", None)
    if resume_without_load is not None:
        assert GOOSE not in resume_without_load


# ── The read-back ──


def _stub(backend: str = GOOSE) -> AcpClient:
    """An ``AcpClient`` whose backend answers, without a spawn or a transport.

    Built through ``__new__`` because the real ``__init__`` resolves a binary and builds
    a transport, and the method under test reads neither -- it reads one response dict
    and the backend id. ``backend`` is a read-only property over ``_acp_backend``, so the
    attribute it reads is what gets set.
    """
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = backend
    client._available_mode_ids = []
    client._modes_advertised = False
    return client


def _session_response(mode: str | None, *, advertised: bool = True) -> dict:
    """A ``session/new`` / ``session/load`` result shaped as goose 1.50.1 sends one."""
    resp: dict = {"sessionId": "goose-session-1"}
    if advertised:
        resp["modes"] = {
            "currentModeId": mode,
            "availableModes": [
                {"id": "auto", "name": "auto", "description": "Automatically approve tool calls"},
                {"id": "approve", "name": "approve", "description": "Ask before every tool call"},
                {
                    "id": "smart_approve",
                    "name": "smart_approve",
                    "description": "Ask only for sensitive tool calls",
                },
                {"id": "chat", "name": "chat", "description": "Chat only, no tool calls"},
            ],
        }
    return resp


def test_the_required_mode_passes() -> None:
    """The mode goose reports IS the required one, so the session proceeds."""
    _stub()._verify_goose_routing(_session_response("approve"))


def test_the_auto_approving_mode_refuses_the_session() -> None:
    """The whole point: a session that auto-approves is one where the gate never runs.

    ``auto`` is the harness's OWN default, so this is the state an unseeded session
    lands in -- not an exotic misconfiguration.
    """
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        _stub()._verify_goose_routing(_session_response("auto"))
    assert "auto" in str(excinfo.value)


def test_the_sensitive_only_mode_refuses_the_session() -> None:
    """``smart_approve`` is refused, and refusing it is a judgement worth pinning.

    It asks for calls the HARNESS considers sensitive. A call it considers ordinary is
    a call that never reaches Crew's gate, so a mode that asks sometimes cannot stand
    in for one that always asks -- and it is the mode most likely to be mistaken for
    good enough.
    """
    with pytest.raises(AcpToolGateUnroutable):
        _stub()._verify_goose_routing(_session_response("smart_approve"))


def test_a_response_with_no_modes_block_refuses_rather_than_passing() -> None:
    """Fail closed on a shape this read was not verified against.

    goose always reports a modes block, so its absence means the response is not the
    one measured -- an older build, a different harness answering, or a shape change.
    Passing here would be reading "no evidence" as "no problem".
    """
    with pytest.raises(AcpToolGateUnroutable):
        _stub()._verify_goose_routing(_session_response(None, advertised=False))


def test_an_empty_current_mode_refuses() -> None:
    """A modes block present but carrying no current mode is also unconfirmed."""
    with pytest.raises(AcpToolGateUnroutable):
        _stub()._verify_goose_routing(_session_response(""))


def test_the_read_back_is_scoped_to_goose() -> None:
    """Another backend's session response must not be judged by goose's rule.

    The method is called from the SHARED session path, so this is what keeps it from
    becoming a condition every other backend pays for -- including kiro-cli, whose own
    modes block carries agent ids rather than permission modes and would otherwise be
    refused by a rule that is not about it.
    """
    for other in (acp_backends.ACP_BACKEND_KIRO, acp_backends.ACP_BACKEND_OPENCODE):
        _stub(other)._verify_goose_routing(_session_response("auto"))


@pytest.mark.parametrize("path", ["session/new", "session/load"])
def test_both_session_paths_call_the_read_back(path: str) -> None:
    """One call site per path, asserted on the source rather than by driving a spawn.

    The restore path is the one that would be easy to omit and impossible to notice:
    a session created correctly and then resumed permissive looks healthy from the
    outside. So the presence of BOTH calls is pinned structurally, the way the tool
    gate pins one preflight per enforced harness.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(AcpClient._initialize_session)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_verify_goose_routing"
    ]
    assert len(calls) == 2, (
        "the routing read-back must run on the session/new response AND on the "
        f"session/load response; found {len(calls)} call site(s)"
    )


def test_the_auto_mode_is_never_sent() -> None:
    """Crew must not put the auto-approving mode on the wire.

    ``session/set_mode`` ACCEPTS it on this harness -- verified live -- so a call site
    that offered it would let a session Crew vouched for turn the host gate off. The
    client defines no constant for it, so there is nothing to pass by accident.
    """
    assert not hasattr(acp_client, "_GOOSE_MODE_AUTO")
    _key, required = sdk_backends.permission_setting_for(GOOSE)
    assert required != "auto"
    # The seed is the ONLY place a mode value enters the child, and it carries the
    # required one -- so the auto mode has no path onto the wire by construction rather
    # than by a call site remembering not to send it.
    assert required == "approve"


def test_the_restore_read_back_is_not_inside_the_load_try() -> None:
    """A refusal must not be reachable by the handler that treats it as a failed load.

    ``AcpToolGateUnroutable`` subclasses ``AcpError``, and the ``session/load`` block
    catches ``(AcpError, AcpTimeoutError)`` and logs-and-continues. With the read-back
    inside that ``try``, a session restored in the auto-approving mode had its refusal
    read as "load did not work" -- and by then ``_session_id`` and ``_resumed`` were
    set, so the ``session/new`` fallback was skipped as well and the permissive session
    ran with no gate on any tool call.

    Asserted structurally because no unit test can reach it: the swallowing happens in
    an async method that needs a live transport, and the ONLY visible symptom is a
    session that works.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(AcpClient._initialize_session)))

    def _calls_read_back(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_verify_goose_routing"
            for n in ast.walk(node)
        )

    for handler_owner in ast.walk(tree):
        if not isinstance(handler_owner, ast.Try):
            continue
        # The protected BODY and the handlers are where a refusal would be caught. The
        # ``else`` clause is not: it runs after the try completes, so a raise there
        # propagates past every handler.
        for guarded in list(handler_owner.body) + [
            stmt for h in handler_owner.handlers for stmt in h.body
        ]:
            assert not _calls_read_back(guarded), (
                "the routing read-back sits inside a try whose handler catches AcpError, "
                "so its refusal is logged as a failed load and the permissive session runs"
            )


def test_goose_gets_its_own_mcp_array_at_both_session_call_sites() -> None:
    """Membership in the array set must be ANSWERED, not merely declared.

    ``_pooled_mcp_servers`` returns ``[]`` for every backend in ``MIRRORS``, handing
    that half to the mirror -- so a backend in the set with no splice of its own gets
    ``mcpServers: []`` and holds none of Crew's tools AND none of the pooled broker
    stubs, while working in every visible respect. That is strictly worse than being
    outside the set.
    """
    import ast
    import inspect
    import textwrap

    # The two arrays sit in DIFFERENT methods -- session/new is composed by
    # ``_new_session_following_substitution`` and session/load by
    # ``_initialize_session`` -- so both are read rather than assuming one owner.
    splices = 0
    for method in (
        AcpClient._new_session_following_substitution,
        AcpClient._initialize_session,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        splices += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_goose_session_mcp_servers"
        )
    assert splices == 2, (
        "goose is in ACP_BACKENDS_SESSION_MCP_ARRAY, so its array must be spliced at "
        f"BOTH the session/new and session/load call sites; found {splices}"
    )


def test_the_model_channel_matches_what_the_mirror_rules() -> None:
    """The mirror rules ``model`` DELIVERED as a config option, so the sets must agree.

    Outside these two, ``_apply_startup_model`` sends the kiro ``set_model`` method
    instead and the advertised selects are never persisted -- a documented capability
    the wire never receives.
    """
    assert GOOSE in sdk_backends.ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert GOOSE in sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION


def test_goose_owns_its_own_sessions() -> None:
    """Without this, ``session/load`` is never SENT and the restore read-back is dead code.

    The pre-check computes a kiro transcript path for a backend outside this set and
    skips the load when that file is absent -- which it always is for a harness keeping
    its sessions in its own store. Every resume would silently start fresh, and the
    read-back that guards the restore path could never run.
    """
    assert GOOSE in sdk_backends.ACP_BACKENDS_HARNESS_OWNED_SESSIONS


# ── MCP identity, and the deny set that rides on it ──
#
# The bug these pin: a spec ``mcp.deny`` set was silently ineffective on goose. The
# identity readers knew codex's ``rawInput {server, tool}`` and kiro's ``_meta.kiro``;
# goose sends ``rawInput`` as ``{"": "{}"}`` and states the identity in
# ``_meta.goose.toolCall``. So the deny check found no identity to match AND the
# compensating "unidentified MCP approval -> refuse" guard was gated on codex's marker,
# which goose does not set. On an auto-approve path the switched-off tool ran.


CORPUS = Path(__file__).parent / "fixtures" / "acp_frames" / "goose"


def _corpus_update(fixture: str, tool_call_id: str) -> dict:
    """The ``tool_call`` update for *tool_call_id*, read from the committed corpus.

    Read rather than transcribed on purpose. A transcription is a second copy of the
    wire that drifts from it silently -- which is exactly what happened here: the frame
    below was hand-copied WITHOUT the ``_meta`` its capture carried, because the
    fixture-building scrub had dropped the object whole, and three reviewers then reasoned
    about goose from a frame that misrepresented goose.
    """
    for line in (CORPUS / fixture).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        update = ((frame.get("params") or {}) if isinstance(frame.get("params"), dict) else {}).get(
            "update"
        )
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") == "tool_call" and update.get("toolCallId") == tool_call_id:
            return update
    raise AssertionError(f"no tool_call {tool_call_id!r} in {fixture}")


def _goose_shell_update() -> dict:
    """The builtin shell ``tool_call`` from the live turn capture."""
    return _corpus_update("turn-live.jsonl", "call_ub405veq")


def _goose_tool_call_frame() -> dict:
    """The ``tool_call`` update from the live MCP capture, field for field.

    Copied from ``test/fixtures/acp_frames/goose/mcp-stdio-mount-live.jsonl`` rather than
    invented, including the ``rawInput`` of ``{"": "{}"}`` -- which is the whole reason
    the rawInput channel cannot answer here and is the field a synthesized frame would
    have gotten wrong.
    """
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_h6atgqyn",
        "title": "crew-probe: crew probe echo",
        "rawInput": {"": "{}"},
        "_meta": {
            "goose": {
                "toolCall": {
                    "toolName": "crew-probe__crew_probe_echo",
                    "extensionName": "crew-probe",
                }
            }
        },
    }


def test_the_shell_fixture_carries_the_harness_tool_identity() -> None:
    """The committed shell frame must still carry ``_meta.goose.toolCall``.

    This pair is the ONLY thing on that frame which says what the tool is: goose sends no
    ACP ``kind`` on the branch Crew selects, the title is a formatter's prose, and
    ``rawInput`` is the model's argument map. A reduction that removes it turns the
    corpus into evidence for a claim the wire does not make, so the fixture is pinned
    here and an over-scrub fails loudly instead of quietly.
    """
    update = _goose_shell_update()
    assert update["_meta"]["goose"]["toolCall"] == {
        "toolName": "shell",
        "extensionName": "developer",
    }
    # And the two volatile keys the reduction is FOR are gone.
    assert set(update["_meta"]["goose"]) == {"toolCall"}


def test_the_builtin_shell_frame_classifies_as_a_command() -> None:
    """``meta_builtin_shell`` answers where ``kind`` is silent.

    Crew advertises ``terminal: false``, which selects goose's own non-terminal shell
    branch, and that branch emits no ``kind``. So ``is_shell_kind`` says False on a real
    command, the command-deny tier never sees it, and under an auto-approve policy it
    runs. The meta channel is what makes the classification possible at all.
    """
    from kiro_crew.acp._dispatch import is_shell_kind, meta_builtin_shell

    update = _goose_shell_update()
    assert "kind" not in update
    assert is_shell_kind(update.get("kind")) is False
    assert meta_builtin_shell(update) is True


def test_a_served_tool_is_not_read_as_the_harness_shell() -> None:
    """The MCP frame from the same corpus must not match the builtin-shell pair."""
    from kiro_crew.acp._dispatch import meta_builtin_shell

    assert meta_builtin_shell(_goose_tool_call_frame()) is False


def test_the_live_frame_yields_the_server_and_the_bare_tool() -> None:
    """The identity readers answer from goose's own channel, prefix stripped.

    goose spells its tool name as ``<extension>__<tool>``; the deny set and the canonical
    ``mcp__<server>__<tool>`` both spell the BARE tool, so the reader reconciles the two
    rather than leaving every comparison to do it.
    """
    from kiro_crew.acp._dispatch import _kiro_mcp_server_name, _kiro_tool_name

    frame = _goose_tool_call_frame()
    assert _kiro_mcp_server_name(frame) == "crew-probe"
    assert _kiro_tool_name(frame) == "crew_probe_echo"


def test_a_goose_builtin_is_not_misidentified_as_mcp() -> None:
    """The builtin shell names an extension, and it must still report NO MCP server.

    goose models its builtins as extensions, so the shell call fills the very field an
    MCP call fills: ``extensionName: developer``. Reporting that as a server would make a
    host command look served -- and a non-empty server is what
    ``child_mcp_identity_trusted`` and ``_is_mcp_tool_approval`` both read as proof it
    was. The pair is recognised as this harness's own shell instead, so the server is
    empty while the tool name survives.
    """
    from kiro_crew.acp._dispatch import _kiro_mcp_server_name, _kiro_tool_name

    builtin = _goose_shell_update()
    assert builtin["_meta"]["goose"]["toolCall"]["extensionName"] == "developer"
    assert _kiro_mcp_server_name(builtin) == ""
    assert _kiro_tool_name(builtin) == "shell"


def test_the_kiro_channel_is_unchanged() -> None:
    """The first channel still answers exactly as it did, and wins when both are present.

    Pinned because the readers were generalized in place: a regression here would move
    every existing backend's identity, not just goose's.
    """
    from kiro_crew.acp._dispatch import _kiro_mcp_server_name, _kiro_tool_name

    kiro_frame = {
        "_meta": {"kiro": {"mcpServerName": "kirocrew-core", "toolName": "memory_recall"}}
    }
    assert _kiro_mcp_server_name(kiro_frame) == "kirocrew-core"
    assert _kiro_tool_name(kiro_frame) == "memory_recall"
    # No prefix stripping on this channel: kiro-cli already spells the bare tool.
    assert (
        _kiro_tool_name({"_meta": {"kiro": {"mcpServerName": "s", "toolName": "s__t"}}}) == "s__t"
    )


def test_a_denied_goose_mcp_tool_is_rejected_and_audited() -> None:
    """The end of the chain: a deny set entry must reject the permission request.

    Driven through ``_deny_spec_disabled_tool`` with the identity the live frame yields,
    because that is the function the auto-approve path consults. Before the fix it
    returned False here -- no identity, so nothing to match -- and the caller went on to
    approve.
    """
    import asyncio

    from kiro_crew.acp.types import AcpEvent

    client = _stub()
    client._spec_denied_tools = {("crew-probe", "crew_probe_echo")}
    client._session_id = "goose-session-1"
    rejected: list = []
    audited: list = []

    async def _reject(request_id: str) -> None:
        rejected.append(request_id)

    client.reject_tool = _reject  # type: ignore[method-assign]
    client._audit_spec_restriction = lambda **kw: audited.append(kw)  # type: ignore[method-assign]

    event = AcpEvent(
        kind="permission_request",
        request_id="perm-1",
        mcp_server_name="crew-probe",
        tool_name="crew_probe_echo",
        mcp_identity_trusted=True,
    )
    assert asyncio.run(client._deny_spec_disabled_tool(event))
    assert rejected == ["perm-1"]
    assert audited and audited[0]["outcome"] == "denied"
    assert audited[0]["reason"] == "spec_disabled_tool"
    assert audited[0]["tool_name"] == "mcp__crew-probe__crew_probe_echo"


def test_a_goose_mcp_approval_is_recognised_without_codex_marker() -> None:
    """The unidentified-approval refusal must cover goose too.

    That guard is what catches an MCP approval the client cannot resolve. It was gated on
    codex's ``_meta.is_mcp_tool_approval``, which goose never sets, so on goose it could
    not fire at all -- leaving the auto-approve path with neither a deny match nor a
    refusal.
    """
    from kiro_crew.acp.client import _identified_mcp_call, _is_mcp_tool_approval
    from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

    msg = JsonRpcMessage(id="perm-1", method="session/request_permission", params={})
    identified = AcpEvent(
        kind="permission_request",
        request_id="perm-1",
        mcp_server_name="crew-probe",
        tool_name="crew_probe_echo",
        mcp_identity_trusted=True,
    )
    assert _is_mcp_tool_approval(msg, identified) is True
    assert _identified_mcp_call(identified) == ("crew-probe", "crew_probe_echo")

    # A builtin: no server named, so neither signal fires and the refusal stays off.
    builtin = AcpEvent(kind="permission_request", request_id="perm-2")
    assert _is_mcp_tool_approval(msg, builtin) is False
    assert _identified_mcp_call(builtin) is None

    # codex's own path is untouched: its marker alone still answers True.
    codex_msg = JsonRpcMessage(
        id="perm-3",
        method="session/request_permission",
        params={"_meta": {"is_mcp_tool_approval": True}},
    )
    assert _is_mcp_tool_approval(codex_msg) is True


def test_the_command_deny_tier_reaches_a_classified_goose_shell_call() -> None:
    """Classification is not the point; what it UNLOCKS is.

    The command-deny tier is handed the executable command, not the title, and it is
    reached only for a call the client resolved as a shell command. So the deny is
    asserted against the command the corpus frame actually carries.
    """
    from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

    update = _goose_shell_update()
    command = update["rawInput"]["command"]
    assert command == "echo goose-wire-probe"

    mgr = HookManager(HooksConfig(auto_deny_tools=[command]))
    result = mgr.on_tool_call(update["title"], command=command, is_shell=True)
    assert result.action == TOOL_DENY


def test_a_tool_call_no_channel_classified_is_refused_on_the_auto_approve_path() -> None:
    """Fail closed: neither an ACP ``kind`` nor a ``_meta`` identity means REFUSE.

    Not a hypothetical frame -- it is the corpus shell frame with its ``_meta`` removed,
    which is precisely the shape the fixture carried while the scrub was over-broad. On an
    auto-approve path there is no human to fall back to, and nothing about the call can be
    checked against either deny set, so the request is rejected and audited rather than
    answered.
    """
    import asyncio

    from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

    client = _stub()
    client._spec_denied_tools = {("some-server", "some-tool")}
    client._session_id = "goose-session-1"
    client._tool_call_unclassified = {}
    rejected: list = []
    audited: list = []

    async def _reject(request_id: str) -> None:
        rejected.append(request_id)

    client.reject_tool = _reject  # type: ignore[method-assign]
    client._audit_spec_restriction = lambda **kw: audited.append(kw)  # type: ignore[method-assign]
    client._note_pi_gate_asked = lambda _msg: None  # type: ignore[method-assign]

    stripped = dict(_goose_shell_update())
    stripped.pop("_meta")
    from kiro_crew.acp._dispatch import meta_builtin_shell

    assert meta_builtin_shell(stripped) is False
    assert "kind" not in stripped
    client._tool_call_unclassified["call_ub405veq"] = True

    event = AcpEvent(
        kind="permission_request",
        request_id="perm-9",
        tool_call_id="call_ub405veq",
    )
    client._build_permission_event = lambda _msg: event  # type: ignore[method-assign]

    async def _never(_request_id: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("an unclassified call must not be approved")

    client.approve_tool = _never  # type: ignore[method-assign]

    async def _no_spec_deny(_event) -> bool:
        return False

    client._deny_spec_disabled_tool = _no_spec_deny  # type: ignore[method-assign]

    msg = JsonRpcMessage(id="perm-9", method="session/request_permission", params={})
    asyncio.run(client._handle_permission(msg))

    assert rejected == ["perm-9"]
    assert audited and audited[0]["outcome"] == "denied"
    assert audited[0]["reason"] == "spec_disabled_tool_unclassified_call"
    assert audited[0]["tool_name"] == "tool__unclassified"


def test_an_identified_mcp_call_is_not_caught_by_the_unclassified_refusal() -> None:
    """The refusal must not swallow a harness that omits ``kind`` on MCP frames.

    Several do, which is why the predicate is keyed on the cache rather than on
    ``shell_classified``: a call whose identity IS recoverable belongs to the MCP checks
    below it, not to this one.
    """
    from kiro_crew.acp.types import AcpEvent

    client = _stub()
    client._tool_call_unclassified = {"call_x": True}

    identified = AcpEvent(
        kind="permission_request",
        request_id="perm-1",
        tool_call_id="call_x",
        mcp_server_name="crew-probe",
        tool_name="crew_probe_echo",
        mcp_identity_trusted=True,
    )
    assert client._unclassified_tool_call(identified) is False

    anonymous = AcpEvent(kind="permission_request", request_id="perm-2", tool_call_id="call_x")
    assert client._unclassified_tool_call(anonymous) is True


@pytest.mark.parametrize("kind", ["execute"])
def test_a_harness_that_sends_kind_still_classifies_through_kind(kind: str) -> None:
    """codex, opencode and pi send ``kind`` on a shell call, and that path is untouched.

    The meta channel is an ADDITIONAL answer, not a replacement: a frame carrying
    ``kind: execute`` and no ``_meta`` classifies exactly as it did, and is not reported
    as this harness's builtin shell.
    """
    from kiro_crew.acp._dispatch import is_shell_kind, meta_builtin_shell

    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_other",
        "title": "Running: ls -la",
        "kind": kind,
        "rawInput": {"command": "ls -la"},
    }
    assert is_shell_kind(frame["kind"]) is True
    assert meta_builtin_shell(frame) is False


def test_the_shared_builder_caches_the_shell_signal_for_the_permission_event() -> None:
    """The event builder must APPLY the channel, and cache it as RESOLVED.

    Caching is the whole mechanism: the permission request that follows carries no kind
    and no ``_meta``, so it inherits the classification through the toolCallId cache.
    Caching it as resolved also matters -- an unresolved entry downgrades the child event
    to low fidelity and takes the identity-keyed grants with it.
    """
    from kiro_crew.acp._dispatch import _build_tool_call_event

    shell_cache: dict[str, bool] = {}
    event = _build_tool_call_event(
        _goose_shell_update(),
        tool_input_cache={},
        shell_cache=shell_cache,
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
    )
    assert event.is_shell is True
    assert shell_cache["call_ub405veq"] is True
    # And the same frame's identity is still not an MCP server.
    assert event.mcp_server_name == ""


def test_the_client_tool_call_path_classifies_and_records_the_verdict() -> None:
    """``AcpClient``'s own tool_call handler reads the channel and fills both caches.

    A separate site from the shared builder -- the worker-pool dispatch runs through the
    client -- so it is covered separately rather than assumed to agree.
    """
    from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage

    client = _stub()
    client._tool_call_inputs = {}
    client._tool_call_input_redacted = {}
    client._tool_call_is_shell = {}
    client._tool_call_unclassified = {}
    client._tool_call_mcp_server = {}
    client._tool_call_tool_name = {}
    client._tool_call_params = {}
    client._tool_call_diff_path = {}
    client.last_prompt_stats = AcpPromptStats()

    msg = JsonRpcMessage(
        method="session/update",
        params={"sessionId": "goose-session-1", "update": _goose_shell_update()},
    )
    event = client._extract_tool_event(msg)

    assert event is not None
    assert event.is_shell is True
    assert client._tool_call_is_shell["call_ub405veq"] is True
    # Classified, so the fail-closed refusal does not apply to it.
    assert client._tool_call_unclassified["call_ub405veq"] is False
    # A builtin is not a served tool.
    assert client._tool_call_mcp_server["call_ub405veq"] == ""


def test_a_frame_with_no_channel_is_recorded_as_unclassified() -> None:
    """The other half of the same site: nothing said anything, and that is remembered."""
    from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage

    client = _stub()
    client._tool_call_inputs = {}
    client._tool_call_input_redacted = {}
    client._tool_call_is_shell = {}
    client._tool_call_unclassified = {}
    client._tool_call_mcp_server = {}
    client._tool_call_tool_name = {}
    client._tool_call_params = {}
    client._tool_call_diff_path = {}
    client.last_prompt_stats = AcpPromptStats()

    stripped = dict(_goose_shell_update())
    stripped.pop("_meta")
    msg = JsonRpcMessage(
        method="session/update",
        params={"sessionId": "goose-session-1", "update": stripped},
    )
    event = client._extract_tool_event(msg)

    assert event is not None
    assert event.is_shell is False
    assert client._tool_call_unclassified["call_ub405veq"] is True


def test_the_refusal_is_scoped_to_harnesses_that_publish_a_channel() -> None:
    """A kind-less frame is refused on goose and NOT on a harness with no channel.

    The safety evidence that a harness classifies every tool class is its recorded corpus,
    which is a handful of frames -- enough to show a ``kind`` present, never enough to
    prove none is ever missing. So the refusal is confined to harnesses that opted into an
    identity channel, where an unclassified frame contradicts the harness's own contract.
    Everything else keeps today's behaviour, whatever its frames happen to omit.
    """
    from kiro_crew.acp.types import AcpEvent

    assert acp_backends.ACP_BACKENDS_META_IDENTITY == {GOOSE}

    # The SAME unclassified event, cached identically, on each backend in turn.
    def _client(backend: str) -> AcpClient:
        client = _stub(backend)
        client._tool_call_unclassified = {"call_x": True}
        return client

    event = AcpEvent(kind="permission_request", request_id="perm-1", tool_call_id="call_x")

    assert _client(GOOSE)._unclassified_tool_call(event) is True
    for other in (
        # kiro-cli publishes a channel and is still OUT: two recorded frames of two tool
        # classes is not the measurement membership rests on, so it keeps today's path.
        acp_backends.ACP_BACKEND_KIRO,
        acp_backends.ACP_BACKEND_OPENCODE,
        acp_backends.ACP_BACKEND_CODEX,
        acp_backends.ACP_BACKEND_CLAUDE,
        acp_backends.ACP_BACKEND_PI,
    ):
        assert _client(other)._unclassified_tool_call(event) is False, other


def test_an_opencode_shaped_kindless_frame_is_not_refused() -> None:
    """The frame Design's concern is about, on the backend it is about.

    opencode publishes no identity channel, so a frame of its that happened to omit
    ``kind`` -- a tool class the corpus never recorded -- must reach the auto-approve path
    exactly as it does today rather than being denied on Crew's missing evidence.
    """
    import asyncio

    from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

    approved: list = []
    rejected: list = []

    client = _stub(acp_backends.ACP_BACKEND_OPENCODE)
    client._spec_denied_tools = {("some-server", "some-tool")}
    client._session_id = "opencode-session-1"
    client._tool_call_unclassified = {"call_oc": True}
    client._note_pi_gate_asked = lambda _msg: None  # type: ignore[method-assign]
    client._audit_spec_restriction = lambda **_kw: None  # type: ignore[method-assign]

    async def _approve(request_id: str) -> None:
        approved.append(request_id)

    async def _reject(request_id: str) -> None:  # pragma: no cover - must not run
        rejected.append(request_id)

    client.approve_tool = _approve  # type: ignore[method-assign]
    client.reject_tool = _reject  # type: ignore[method-assign]

    event = AcpEvent(kind="permission_request", request_id="perm-oc", tool_call_id="call_oc")
    client._build_permission_event = lambda _msg: event  # type: ignore[method-assign]

    async def _no_spec_deny(_event) -> bool:
        return False

    client._deny_spec_disabled_tool = _no_spec_deny  # type: ignore[method-assign]

    msg = JsonRpcMessage(id="perm-oc", method="session/request_permission", params={})
    asyncio.run(client._handle_permission(msg))

    assert approved == ["perm-oc"]
    assert rejected == []


def _tool_call_updates(directory: Path) -> list[tuple[str, dict]]:
    """Every committed ``tool_call`` update under one backend's corpus directory."""
    found: list[tuple[str, dict]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            frame = json.loads(line)
            params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
            update = params.get("update")
            if isinstance(update, dict) and update.get("sessionUpdate") == "tool_call":
                found.append((path.name, update))
    return found


def test_every_opted_in_backend_classifies_each_recorded_tool_call() -> None:
    """The measurement membership in ``ACP_BACKENDS_META_IDENTITY`` owes, per backend.

    Opting a backend into the fail-closed refusal rests on a claim: every tool call it
    emits is classifiable, by an ACP ``kind`` or by its own identity channel. This PR held
    kas OUT on the ground that its coverage was not measured -- and the same standard
    applies to every backend held IN. So this is the corpus-derived pin: for each member of
    the set, each committed ``tool_call`` frame in its corpus is classified by the SAME
    readers the client uses -- an ACP ``kind``, or the harness's own builtin-shell pair,
    or a tool identity from a ``_meta`` channel. Measured through the readers rather than
    through a per-row key so the pin exercises the real classification path and the table
    carries no membership claim of its own. Corpus-derived rather than a list of ids, in
    the shape of the protocol-version ratchet, so a backend added to the set tomorrow is
    measured against its own recordings without editing this test; a backend whose corpus
    has no ``tool_call`` at all fails rather than passing vacuously.
    """
    from acp_frame_replay_harness import fixture_dir_name

    from kiro_crew.acp._dispatch import (
        _kiro_mcp_server_name,
        _kiro_tool_name,
        is_shell_kind,
        meta_builtin_shell,
    )

    corpus_root = Path(__file__).parent / "fixtures" / "acp_frames"

    def _classified(update: dict) -> bool:
        kind = update.get("kind")
        has_kind = isinstance(kind, str) and bool(kind)
        return (
            has_kind
            or is_shell_kind(kind)
            or meta_builtin_shell(update)
            or bool(_kiro_tool_name(update))
            or bool(_kiro_mcp_server_name(update))
        )

    for backend in sorted(acp_backends.ACP_BACKENDS_META_IDENTITY):
        updates = _tool_call_updates(corpus_root / fixture_dir_name(backend))
        assert updates, f"{backend!r} is opted in but its corpus records no tool_call"
        unclassified = [name for name, update in updates if not _classified(update)]
        assert unclassified == [], (
            f"{backend!r} is in ACP_BACKENDS_META_IDENTITY, so a deny-set session refuses a "
            f"tool_call it cannot classify -- yet no reader classifies these recorded "
            f"frames: {unclassified}. Either the harness omits every channel on a real tool "
            "class (then shrink the set) or the fixture misrepresents the wire."
        )


def _deny_set_client(backend: str, placed: list[str]) -> AcpClient:
    """A stubbed client on the auto-approve path with a deny set and a known array."""
    client = _stub(backend)
    client._spec_denied_tools = {("some-server", "some-tool")}
    client._session_id = f"{backend}-session-1"
    client._tool_call_unclassified = {}
    client._session_mcp_servers = lambda: [  # type: ignore[method-assign]
        {"name": name, "command": "x", "args": [], "env": {}} for name in placed
    ]
    client._note_pi_gate_asked = lambda _msg: None  # type: ignore[method-assign]

    async def _no_spec_deny(_event) -> bool:
        return False

    client._deny_spec_disabled_tool = _no_spec_deny  # type: ignore[method-assign]
    return client


def test_a_trusted_identity_naming_an_unplaced_server_is_refused() -> None:
    """Drift, not absence: a renamed builtin extension must not pass as MCP-served.

    The channel is present and trusted; it names a server Crew never placed on the array
    and which is not the harness's own extension. The readers would file the call as an
    MCP tool -- so not a command, so the command-deny tier is skipped, and not in any deny
    set, so approved. Crew knows the session's whole server universe, so this is refused.
    """
    import asyncio

    from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

    client = _deny_set_client(GOOSE, placed=["crew-probe"])
    rejected: list = []
    audited: list = []

    async def _reject(request_id: str) -> None:
        rejected.append(request_id)

    async def _never(_request_id: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("a foreign server must not be approved")

    client.reject_tool = _reject  # type: ignore[method-assign]
    client.approve_tool = _never  # type: ignore[method-assign]
    client._audit_spec_restriction = lambda **kw: audited.append(kw)  # type: ignore[method-assign]

    # goose's shell frame as a future release might send it: the extension renamed.
    drifted = AcpEvent(
        kind="permission_request",
        request_id="perm-d",
        tool_call_id="call_ub405veq",
        mcp_server_name="dev",
        tool_name="shell",
        mcp_identity_trusted=True,
    )
    client._build_permission_event = lambda _msg: drifted  # type: ignore[method-assign]
    msg = JsonRpcMessage(id="perm-d", method="session/request_permission", params={})
    asyncio.run(client._handle_permission(msg))

    assert rejected == ["perm-d"]
    assert audited and audited[0]["reason"] == "spec_disabled_tool_foreign_server"
    assert audited[0]["tool_name"] == "mcp__dev__shell"


def test_a_placed_server_and_the_builtin_extension_are_not_foreign() -> None:
    """The two identities a goose session legitimately carries both pass this check."""
    from kiro_crew.acp.types import AcpEvent

    client = _deny_set_client(GOOSE, placed=["crew-probe"])

    placed = AcpEvent(
        kind="permission_request",
        request_id="p",
        mcp_server_name="crew-probe",
        tool_name="crew_probe_echo",
        mcp_identity_trusted=True,
    )
    assert client._foreign_mcp_identity(placed) is False

    # goose's own extension, carried by its non-shell builtins (text_editor and friends).
    builtin = AcpEvent(
        kind="permission_request",
        request_id="b",
        mcp_server_name="developer",
        tool_name="text_editor",
        mcp_identity_trusted=True,
    )
    assert client._foreign_mcp_identity(builtin) is False

    # An untrusted name is never judged: the payload's prose is not evidence.
    untrusted = AcpEvent(
        kind="permission_request", request_id="u", mcp_server_name="dev", tool_name="shell"
    )
    assert client._foreign_mcp_identity(untrusted) is False


def test_the_foreign_server_check_is_scoped_to_array_backends() -> None:
    """kiro-cli's servers reach it through the agent file, so its array is not the universe."""
    from kiro_crew.acp.types import AcpEvent

    event = AcpEvent(
        kind="permission_request",
        request_id="k",
        mcp_server_name="anything",
        tool_name="tool",
        mcp_identity_trusted=True,
    )
    assert (
        _deny_set_client(acp_backends.ACP_BACKEND_KIRO, placed=[])._foreign_mcp_identity(event)
        is False
    )
    assert _deny_set_client(GOOSE, placed=[])._foreign_mcp_identity(event) is True


def _mode_update_frames() -> list[dict]:
    """Every ``current_mode_update`` in the live session-load capture, as sent."""
    frames = []
    for line in (CORPUS / "session-load-live.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        update = params.get("update")
        if isinstance(update, dict) and update.get("sessionUpdate") == "current_mode_update":
            frames.append(frame)
    return frames


def test_a_mid_session_move_off_the_required_mode_stops_the_harness() -> None:
    """The read-back proves where a session STARTED; this holds it there.

    The corpus records goose reporting ``auto`` on its own connection after a
    ``set_mode`` -- the frame a session emits as it stops asking. In ``auto`` no
    permission request reaches Crew, so this notification is the last chance to refuse:
    the harness is killed and the turn fails with the gate's own reason, exactly as the
    open/restore read-back does for the same mode.
    """
    import asyncio

    from kiro_crew.acp.types import JsonRpcMessage

    frames = _mode_update_frames()
    modes = [f["params"]["update"]["currentModeId"] for f in frames]
    # This assertion is the corpus pin on the THIRD verified-range fact -- that goose
    # emits a current_mode_update when the mode moves -- and it is the fail-OPEN one: a
    # re-capture on a release that stopped emitting it fails here rather than shipping a
    # tripwire that never fires.
    assert "auto" in modes and "approve" in modes, modes

    client = _stub()
    client._session_id = "goose-session-1"
    client._session_key = ""
    killed: list = []

    async def _kill(force: bool = False) -> None:
        killed.append(force)

    client._kill_process = _kill  # type: ignore[method-assign]

    auto = next(f for f in frames if f["params"]["update"]["currentModeId"] == "auto")
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(
            client._tripwire_goose_mode(
                JsonRpcMessage(**{k: auto[k] for k in ("method", "params")})
            )
        )
    assert killed == [True]

    # The required mode, re-emitted on load, is a no-op: nothing killed, nothing raised.
    approve = next(f for f in frames if f["params"]["update"]["currentModeId"] == "approve")
    asyncio.run(
        client._tripwire_goose_mode(JsonRpcMessage(**{k: approve[k] for k in ("method", "params")}))
    )
    assert killed == [True]


def test_the_mode_tripwire_is_gated_on_the_read_back_predicate() -> None:
    """Every other backend's frames pass straight through; the kiro path gains no call."""
    import asyncio

    from kiro_crew.acp.types import JsonRpcMessage

    auto = next(
        f for f in _mode_update_frames() if f["params"]["update"]["currentModeId"] == "auto"
    )
    for backend in sorted(acp_backends.ACP_BACKENDS_KNOWN - {GOOSE}):
        client = _stub(backend)
        client._kill_process = None  # type: ignore[assignment]  # would raise if reached
        asyncio.run(
            client._tripwire_goose_mode(
                JsonRpcMessage(**{k: auto[k] for k in ("method", "params")})
            )
        )


def test_the_codex_rawinput_channel_still_answers_first() -> None:
    """codex resolves through ``rawInput``, and that path must not have moved."""
    from kiro_crew.acp.client import _identified_mcp_call
    from kiro_crew.acp.types import AcpEvent

    codex = AcpEvent(
        kind="permission_request",
        request_id="perm-1",
        raw_tool_params={"server": "codex-server", "tool": "codex_tool"},
        raw_params_trusted=True,
    )
    assert _identified_mcp_call(codex) == ("codex-server", "codex_tool")


# ── The spawn seam ──


def test_the_install_command_and_the_resolved_binary_cannot_drift() -> None:
    """The driver seam reads the record rather than restating what it holds."""
    from kiro_crew.agent_sdk.backends import launch_for
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    record = launch_for(sdk_backends.ACP_BACKEND_GOOSE)
    assert (
        acp_driver.self_served_install_command(sdk_backends.ACP_BACKEND_GOOSE)
        == record.install_command
    )
    assert record.binary == "goose"
    assert record.acp_args == ("acp",)
    assert record.spawn_label == "goose acp"


def test_the_argv_names_the_builtin_extension() -> None:
    """Supplying ``mcpServers`` REPLACES this harness's configured extensions.

    So a session handed Crew's servers and nothing else carries no shell and no file
    tools at all. The builtin is named on the argv to restore them, and it is pinned
    here because the failure it prevents is silent: the session works, and the agent
    simply has fewer tools than anyone expected.
    """
    assert acp_client._GOOSE_BUILTIN_ARG == "--with-builtin"
    assert acp_client._GOOSE_BUILTIN_DEVELOPER == "developer"


def test_the_resolution_ladder_prefers_the_explicit_override(monkeypatch, tmp_path) -> None:
    """Override, then mise, then PATH -- the plain-binary ladder, not the Node one.

    Executability is STUBBED rather than created on disk, the way the opencode and pi
    ladders' tests do it: what a file has to be for the host to call it runnable is a
    mode bit on POSIX and a suffix in ``_WINDOWS_RUNNABLE_HOOK_SUFFIXES`` on Windows, so
    an extensionless fake resolves on one platform and not the other. This test is about
    rung ORDER, and the rung below is stubbed to a sentinel so a host that has the
    harness on PATH cannot pass it for the wrong reason.
    """
    fake = tmp_path / "goose"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(acp_client.platform_compat, "is_executable_file", lambda _p: True)
    monkeypatch.setattr(acp_client, "_mise_which", lambda _name: "/never/reached")
    monkeypatch.setenv(_GOOSE_LAUNCH.bin_env_var, str(fake))
    resolved, _searched = acp_client._resolve_self_served_bin(sdk_backends.ACP_BACKEND_GOOSE)
    assert resolved == (acp_client._normalize_exe_casing(str(fake)) or str(fake))


def test_a_non_runnable_override_falls_through_to_mise(monkeypatch, tmp_path) -> None:
    """An override naming something unrunnable must not shadow a working install.

    Pointing the variable at a text file is a typo, and answering with it would report
    the harness present and then fail at spawn with an exec error instead of the
    ladder's own remedy message.
    """
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(acp_client.platform_compat, "is_executable_file", lambda _p: False)
    monkeypatch.setattr(acp_client, "_mise_which", lambda _name: "/opt/mise/goose")
    monkeypatch.setenv(_GOOSE_LAUNCH.bin_env_var, str(notes))
    resolved, _searched = acp_client._resolve_self_served_bin(sdk_backends.ACP_BACKEND_GOOSE)
    assert resolved == "/opt/mise/goose"


def test_an_absent_binary_reports_what_was_searched(monkeypatch, tmp_path) -> None:
    """``(None, search_path)`` rather than a raise from inside the resolver.

    The augmented path is stubbed to an empty directory rather than the environment
    being emptied: the resolver ADDS well-known bin directories to whatever ``PATH``
    holds, so a host that has the harness installed would resolve it through one of
    those and this test would pass or fail on the recording machine's contents.
    """
    monkeypatch.delenv(_GOOSE_LAUNCH.bin_env_var, raising=False)
    monkeypatch.setattr(acp_client, "_mise_which", lambda _name: None)
    monkeypatch.setattr(acp_client, "augmented_path", lambda _p: str(tmp_path))
    resolved, searched = acp_client._resolve_self_served_bin(sdk_backends.ACP_BACKEND_GOOSE)
    assert resolved is None
    assert searched == str(tmp_path)


# ── Auth ──


def test_the_credential_declaration_is_internally_consistent() -> None:
    """``adapter_own_leaves`` must be a subset of the leaves the floor was given.

    Excluding a leaf the declaration never fenced is either a no-op or an attempt to
    open something the host fenced for another reason, so the relationship is checked
    rather than assumed.
    """
    from kiro_crew.agent_sdk.host_auth import declaration_for

    decl = declaration_for(GOOSE)
    assert decl.credential_leaves == (".config/goose/secrets.yaml",)
    assert set(decl.adapter_own_leaves) <= set(decl.credential_leaves)
    assert decl.home_override_env_vars == ("XDG_CONFIG_HOME",)
    # The override replaces the ``.config`` PREFIX, so the relocated file keeps two
    # segments -- an override spelling of the final segment alone would fence a path
    # the harness never writes and leave the real one readable.
    assert decl.override_relative_leaves == ("goose/secrets.yaml",)
    assert decl.host_logout_retires_children is False


def test_every_declared_home_override_is_scrubbed_from_a_child_env() -> None:
    """A declared override that is not scrubbed hands a child the relocated store."""
    import importlib.util
    import pathlib
    import sys

    name = "deny_diff_for_goose"
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "deny_diff.py"
    assert path.is_file(), f"the deny-diff helper moved: {path}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: this script's dataclasses resolve their own string
    # annotations through ``sys.modules[cls.__module__]``, so executing it unregistered
    # raises at class-creation time rather than at use.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        assert "XDG_CONFIG_HOME" in module._INHERITED_HOME_OVERRIDE_ENV_VARS
    finally:
        sys.modules.pop(name, None)


def test_the_sign_in_remedy_renders_as_prose() -> None:
    """It reaches a div with no markdown pass, so backticks and ``--`` reach the reader."""
    from kiro_crew.agent_sdk.host_auth import declaration_for

    remedy = declaration_for(GOOSE).sign_in_remedy
    assert "`" not in remedy
    assert "--" not in remedy
    assert "goose configure" in remedy


class TestAReleaseOutsideTheVerifiedRangeIsNamedAtHandshake:
    """The one fail-open wire fact gets its signal before the first prompt (Design)."""

    @pytest.fixture(autouse=True)
    def _fresh_process_memory(self, monkeypatch):
        monkeypatch.setattr(acp_client, "_goose_versions_noted", set())

    def _client(self, tmp_path, version, backend=sdk_backends.ACP_BACKEND_GOOSE):
        client = AcpClient(work_dir=tmp_path, acp_backend=backend)
        client._agent_version = version
        return client

    @staticmethod
    def _hits(caplog):
        return [r.getMessage() for r in caplog.records if "routing contract" in r.getMessage()]

    def test_a_release_is_named_once_per_process(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, "1.51.0")._note_goose_version()
            self._client(tmp_path, "1.51.0")._note_goose_version()
            self._client(tmp_path, "2.0.0")._note_goose_version()
        assert len(self._hits(caplog)) == 2

    @pytest.mark.parametrize("version", ["1.50.1", "1.50.9"])
    def test_the_verified_range_is_silent(self, tmp_path, caplog, version):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, version)._note_goose_version()
        assert not self._hits(caplog)

    @pytest.mark.parametrize("version", ["1.51.0", "1.500.1", "1.5.0", ""])
    def test_another_or_unknown_release_is_logged_with_both_versions(
        self, tmp_path, caplog, version
    ):
        """The trailing dot on the prefix keeps 1.500.x and 1.5.x outside the range."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, version)._note_goose_version()
        hits = self._hits(caplog)
        assert len(hits) == 1
        assert acp_client.GOOSE_VERIFIED_VERSION_PREFIX in hits[0]
        assert (version or "unknown") in hits[0]

    def test_other_harnesses_are_untouched(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            self._client(tmp_path, "9.9.9", sdk_backends.ACP_BACKEND_PI)._note_goose_version()
            self._client(tmp_path, "9.9.9", sdk_backends.ACP_BACKEND_KIRO)._note_goose_version()
        assert not caplog.records

    def test_it_runs_at_the_handshake(self):
        source = inspect.getsource(AcpClient._initialize_session)
        assert "self._agent_version = agent_version_from_init(init_resp)" in source
        assert "self._note_goose_version()" in source

    def test_the_verified_prefix_is_the_recorded_handshake(self):
        """The range is anchored on the version the corpus binary actually reported."""
        frames = (CORPUS / "handshake-live.jsonl").read_text(encoding="utf-8").splitlines()
        reported = [
            json.loads(line)["result"]["agentInfo"]["version"]
            for line in frames
            if '"agentInfo"' in line
        ]
        assert reported, "the handshake corpus carries no agentInfo"
        assert all(v.startswith(acp_client.GOOSE_VERIFIED_VERSION_PREFIX) for v in reported)


# ── the drift refusals do not wait for a deny set goose never carries ───────────────


def _no_deny_set_client(backend: str, placed: list[str] | None = None) -> AcpClient:
    """A stubbed client on the auto-approve path with an EMPTY deny set -- a real goose
    session's shape, since the goose projection carries no ``denied_tools`` by ruling."""
    client = _stub(backend)
    client._spec_denied_tools = frozenset()
    client._session_id = f"{backend}-session-1"
    client._tool_call_unclassified = {}
    client._session_mcp_servers = lambda: [  # type: ignore[method-assign]
        {"name": name, "command": "x", "args": [], "env": {}} for name in (placed or [])
    ]
    client._note_pi_gate_asked = lambda _msg: None  # type: ignore[method-assign]
    return client


def test_goose_judges_its_requests_with_an_empty_deny_set() -> None:
    """The predicate is what makes a site build the event; goose is in without a deny set."""
    client = _no_deny_set_client(GOOSE)
    assert client._judges_permission_requests is True
    for other in (
        acp_backends.ACP_BACKEND_CLAUDE,
        acp_backends.ACP_BACKEND_CODEX,
        acp_backends.ACP_BACKEND_OPENCODE,
        acp_backends.ACP_BACKEND_PI,
        acp_backends.ACP_BACKEND_KIRO,
    ):
        assert _no_deny_set_client(other)._judges_permission_requests is False, other
    with_deny = _no_deny_set_client(acp_backends.ACP_BACKEND_CODEX)
    with_deny._spec_denied_tools = {("s", "t")}
    assert with_deny._judges_permission_requests is True


def test_an_unclassified_call_is_refused_on_goose_without_a_deny_set() -> None:
    """A real goose session carries no deny set, so the refusal must not wait for one:
    it fires on the empty set."""
    import asyncio

    from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

    client = _no_deny_set_client(GOOSE)
    client._tool_call_unclassified["call_ub405veq"] = True
    rejected: list = []
    audited: list = []

    async def _reject(request_id: str) -> None:
        rejected.append(request_id)

    async def _never(_request_id: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("an unclassified call must not be approved")

    client.reject_tool = _reject  # type: ignore[method-assign]
    client.approve_tool = _never  # type: ignore[method-assign]
    client._audit_spec_restriction = lambda **kw: audited.append(kw)  # type: ignore[method-assign]
    event = AcpEvent(kind="permission_request", request_id="perm-nd", tool_call_id="call_ub405veq")
    client._build_permission_event = lambda _msg: event  # type: ignore[method-assign]

    msg = JsonRpcMessage(id="perm-nd", method="session/request_permission", params={})
    asyncio.run(client._handle_permission(msg))

    assert rejected == ["perm-nd"]
    assert audited[0]["reason"] == "spec_disabled_tool_unclassified_call"


def test_a_foreign_server_is_refused_on_goose_without_a_deny_set() -> None:
    import asyncio

    from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

    client = _no_deny_set_client(GOOSE, placed=["crew-probe"])
    rejected: list = []
    audited: list = []

    async def _reject(request_id: str) -> None:
        rejected.append(request_id)

    async def _never(_request_id: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("a foreign identity must not be approved")

    client.reject_tool = _reject  # type: ignore[method-assign]
    client.approve_tool = _never  # type: ignore[method-assign]
    client._audit_spec_restriction = lambda **kw: audited.append(kw)  # type: ignore[method-assign]
    event = AcpEvent(
        kind="permission_request",
        request_id="perm-nf",
        tool_call_id="call_f",
        mcp_server_name="renamed_developer",
        tool_name="shell",
        mcp_identity_trusted=True,
    )
    client._build_permission_event = lambda _msg: event  # type: ignore[method-assign]

    msg = JsonRpcMessage(id="perm-nf", method="session/request_permission", params={})
    asyncio.run(client._handle_permission(msg))

    assert rejected == ["perm-nf"]
    assert audited[0]["reason"] == "spec_disabled_tool_foreign_server"


def test_a_classified_goose_call_is_still_approved_without_a_deny_set() -> None:
    """Judging is not refusing: a classified call on the empty set is approved as before."""
    import asyncio

    from kiro_crew.acp.types import AcpEvent, JsonRpcMessage

    client = _no_deny_set_client(GOOSE, placed=["crew-probe"])
    client._tool_call_unclassified["call_ok"] = False
    approved: list = []

    async def _approve(request_id: str) -> None:
        approved.append(request_id)

    async def _never(_request_id: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("a classified call must not be refused")

    client.approve_tool = _approve  # type: ignore[method-assign]
    client.reject_tool = _never  # type: ignore[method-assign]
    event = AcpEvent(kind="permission_request", request_id="perm-ok", tool_call_id="call_ok")
    client._build_permission_event = lambda _msg: event  # type: ignore[method-assign]

    msg = JsonRpcMessage(id="perm-ok", method="session/request_permission", params={})
    asyncio.run(client._handle_permission(msg))
    assert approved == ["perm-ok"]


def test_a_harness_outside_the_set_builds_nothing_without_a_deny_set() -> None:
    """Every other backend keeps the prior site byte for byte: no event, plain approve."""
    import asyncio

    from kiro_crew.acp.types import JsonRpcMessage

    client = _no_deny_set_client(acp_backends.ACP_BACKEND_CLAUDE)
    client._tool_call_unclassified["call_c"] = True
    approved: list = []

    async def _approve(request_id: str) -> None:
        approved.append(request_id)

    def _never_build(_msg):  # pragma: no cover - must not run
        raise AssertionError("no event is built on a session that judges nothing")

    client.approve_tool = _approve  # type: ignore[method-assign]
    client._build_permission_event = _never_build  # type: ignore[method-assign]

    msg = JsonRpcMessage(id="perm-c", method="session/request_permission", params={})
    asyncio.run(client._handle_permission(msg))
    assert approved == ["perm-c"]


def test_the_drift_refusals_run_on_both_answering_sites() -> None:
    """Structural, in the codex file's idiom: the event-yielding loop and the auto-approve
    site both run ``_refuse_identity_drift`` beside the spec refusal, and the streaming
    loop writes the cache it reads."""
    for site in ("_dispatch_events", "_handle_permission"):
        body = inspect.getsource(getattr(AcpClient, site))
        assert "self._refuse_identity_drift(" in body, site
        assert "self._deny_spec_disabled_tool(" in body, site
    stream = inspect.getsource(AcpClient.send_message_stream)
    assert "if self._judges_permission_requests:" in stream
    assert "self._extract_tool_event(msg)" in stream
