"""DeepSeek Harness as an ACP backend: the decisions that must not drift.

The harness is a plugin host and ACP is one of the profiles it boots, so most of
what this file pins is ordinary onboarding vocabulary. Three things are not:

* it serves ``session/resume`` and REJECTS ``session/load``, so the shared restore
  path has to pick both the capability it reads and the verb it sends from one
  membership set;
* it decides its own tool calls, so its routing is ``UNVERIFIED`` and it is not
  offered on the switch -- and the tests that would assert a gate assert its
  ABSENCE instead, which is the only honest form;
* it advertises reasoning effort under its own option id, so the id is read from a
  table rather than spelled at each site.
"""

from __future__ import annotations

import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_RESUME_WITHOUT_LOAD,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_STEER,
    BASELINE_SELECTABLE_BACKENDS,
    POLICY_ID_BY_BACKEND,
    Routing,
    effort_option_id,
    routing_for,
)

# ── Vocabulary ───────────────────────────────────────────────────────────────


def test_the_id_is_known_and_nameable_by_a_policy_author() -> None:
    """A known id must be spellable in a governance rule, or it cannot be denied."""
    assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_KNOWN
    assert POLICY_ID_BY_BACKEND[ACP_BACKEND_DEEPSEEK] == "deepseek"


def test_it_is_known_but_not_shipped_selectable() -> None:
    """Known so it can be registered and denied; unselectable because it is ungated.

    The two halves of the selectability bar are an install probe and ROUTED tool
    calls. This harness has the first and not the second, so the switch would offer
    a harness Crew cannot gate.
    """
    assert ACP_BACKEND_DEEPSEEK not in BASELINE_SELECTABLE_BACKENDS


@pytest.mark.parametrize(
    "membership, expected",
    [
        (ACP_BACKENDS_HARNESS_OWNED_SESSIONS, True),
        (ACP_BACKENDS_LOAD_WITHOUT_MODES, True),
        (ACP_BACKENDS_RESUME_WITHOUT_LOAD, True),
        (ACP_BACKENDS_SESSION_MCP_ARRAY, True),
        (ACP_BACKENDS_ADVERTISED_MODEL_SELECTION, True),
        (ACP_BACKENDS_STEER, False),
        (ACP_BACKENDS_COMPACT, False),
        (ACP_BACKENDS_INTERNAL_SANDBOX, False),
        (ACP_BACKENDS_MEMBER_DISPATCH, False),
    ],
)
def test_every_capability_is_an_explicit_decision(membership: frozenset, expected: bool) -> None:
    """One row per set, so a silently granted capability names its harness.

    Written out rather than derived from the sets themselves, which would pass
    tautologically.
    """
    assert (ACP_BACKEND_DEEPSEEK in membership) is expected


def test_the_resume_set_is_the_only_member_and_says_why() -> None:
    """Sole membership is the claim: no other harness Crew carries lacks the verb."""
    assert ACP_BACKENDS_RESUME_WITHOUT_LOAD == frozenset({ACP_BACKEND_DEEPSEEK})


def test_the_effort_option_id_is_this_harness_own_spelling() -> None:
    """The table answers a spelling; every other harness keeps the default."""
    assert effort_option_id(ACP_BACKEND_DEEPSEEK) == "reasoning_effort"
    assert effort_option_id(ACP_BACKEND_CLAUDE) == "effort"
    assert effort_option_id(ACP_BACKEND_KIRO) == "effort"


def _fixture_frames(name: str) -> list[dict]:
    """Every frame of one committed fixture, header excluded."""
    import json

    path = pathlib.Path(__file__).parent / "fixtures" / "acp_frames" / "deepseek" / name
    return [json.loads(line) for line in path.read_text().splitlines()[1:]]


def test_the_session_mcp_array_membership_rests_on_a_captured_round_trip() -> None:
    """Membership here is load-bearing, so it is pinned to evidence rather than prose.

    If this harness did NOT mount stdio, ``session/new`` would fail whole rather than
    degrade -- a broken backend, not a tool-less one -- and its ``initialize``
    advertises ``mcpCapabilities: {"http": true}`` with no stdio flag, which reads
    like a refusal. The capture is what settles it: a real stdio MCP server was
    mounted, its tool was called, and its result came back.
    """
    frames = _fixture_frames("mcp-stdio-mount-live.jsonl")

    created = [
        frame
        for frame in frames
        if isinstance(frame.get("result"), dict) and frame["result"].get("sessionId")
    ]
    assert created, "session/new must have SUCCEEDED with the stdio element mounted"

    updates = [
        (frame.get("params") or {}).get("update", {})
        for frame in frames
        if isinstance(frame.get("params"), dict)
    ]
    calls = [u for u in updates if u.get("sessionUpdate") == "tool_call"]
    assert calls, "the capture must carry a tool_call for the mounted MCP tool"
    # The harness's own MCP grammar: mcp__<serverName>__<toolName>. Asserted because
    # the host contract's tool-name-grammar row states it as measured.
    assert calls[0]["title"].startswith("mcp__"), calls[0]["title"]
    assert calls[0]["title"].count("__") >= 2, calls[0]["title"]

    results = [u for u in updates if u.get("sessionUpdate") == "tool_call_update"]
    assert any(u.get("status") == "completed" for u in results), (
        "a mount that is accepted but whose tool never returns proves reachability of "
        "nothing; the capture must carry a completed result"
    )


def test_an_unstartable_mcp_element_fails_the_whole_session() -> None:
    """The hazard half, pinned because it changes how a pooled stub failure behaves.

    codex-acp drops a malformed element and creates the session anyway. This harness
    rolls the whole session back, so ONE pooled broker stub that cannot start costs a
    session entirely. The host contract's loader-strictness row states that; this is
    what makes the statement checkable.
    """
    frames = _fixture_frames("mcp-stdio-rollback-live.jsonl")

    errors = [frame for frame in frames if isinstance(frame.get("error"), dict)]
    assert errors, "the rollback capture must carry the session/new error"
    detail = str((errors[0]["error"].get("data") or {}).get("details", ""))
    assert "initial connection or tool synchronization failed" in detail, detail
    assert not [
        frame
        for frame in frames
        if isinstance(frame.get("result"), dict) and frame["result"].get("sessionId")
    ], "no session may have been created by the rolled-back request"


# ── Routing: the absence of a gate, asserted ─────────────────────────────────


def test_the_routing_is_unverified_and_therefore_unenforced() -> None:
    """``UNVERIFIED`` is the honest member when nothing makes a harness ask.

    Its sandbox decides a tool call itself, and ``session/request_permission``
    carries only a model-initiated escalation, so there is no precondition to seed
    and nothing to read back. A ``VERIFIED_SEEDED_SETTINGS`` entry would read back
    a real setting and assert a guarantee nothing performs.
    """
    from kiro_crew import acp_tool_gate as gate

    assert routing_for(ACP_BACKEND_DEEPSEEK) is Routing.UNVERIFIED
    assert gate.is_enforced(ACP_BACKEND_DEEPSEEK) is False
    verdict, _reason = gate.routing_verdict(ACP_BACKEND_DEEPSEEK)
    assert verdict is gate.Verdict.INDETERMINATE


def test_registering_an_unverified_harness_as_selectable_is_refused() -> None:
    """KNOWN must not be enough to reach the switch, and this is where that is enforced.

    Before this harness existed, ``register_selectable_backend`` raising on an id
    outside ``ACP_BACKENDS_KNOWN`` WAS the guard: an ungated harness could not be
    named, so it could not be registered. Putting deepseek in KNOWN -- which it must
    be, so a governance rule can deny it -- removes that guard for it. One call from
    an out-of-repo edition would otherwise put a harness on the switch whose tool
    calls never reach ``HookManager.on_tool_call``, whose INDETERMINATE verdict
    refuses nothing, and whose spawn path applies no credential mask.
    """
    from kiro_crew.agent_sdk import backends as sdk_backends

    baseline_before = set(sdk_backends._baseline)
    selectable_before = set(sdk_backends._selectable)
    try:
        with pytest.raises(ValueError) as raised:
            sdk_backends.register_selectable_backend(ACP_BACKEND_DEEPSEEK)
        message = str(raised.value)
        assert "unverified" in message
        # The message names the remedy -- declare routing -- rather than a flag, because
        # there is no flag: an escape hatch here would be a documented way to put an
        # ungated harness on the switch.
        assert "ACP_BACKEND_ROUTING" in message
        assert "allow_unrouted" not in message
        # And it must not half-register: a refusal that mutated either set would leave
        # the harness selectable anyway.
        assert set(sdk_backends._baseline) == baseline_before
        assert set(sdk_backends._selectable) == selectable_before
    finally:
        sdk_backends._baseline.clear()
        sdk_backends._baseline.update(baseline_before)
        sdk_backends._selectable.clear()
        sdk_backends._selectable.update(selectable_before)


def test_the_refusal_has_no_escape_hatch() -> None:
    """No parameter may turn the refusal off, and that is the point of it.

    A keyword flag would be a documented path to an ungated selectable harness, and no
    shipped caller wants one: every known backend but one is routed, and that one is
    deliberately absent from the selectable baseline. An edition that genuinely needs
    otherwise arrives with its own caller and its own justification.
    """
    import inspect

    from kiro_crew.agent_sdk import backends as sdk_backends

    signature = inspect.signature(sdk_backends.register_selectable_backend)
    assert list(signature.parameters) == ["backend"], (
        "register_selectable_backend takes the backend id and nothing else; a second "
        f"parameter would be a way to bypass the routing refusal: {signature}"
    )


def test_a_routed_harness_still_registers() -> None:
    """The other arm: the refusal keys on the ROUTING, not on this harness's id.

    Without this a guard that simply named deepseek would pass, and the next
    ``UNVERIFIED`` harness would walk straight onto the switch. It is also what keeps the
    unconditional refusal from being a blanket one -- a routed harness still registers.
    """
    from kiro_crew.agent_sdk import backends as sdk_backends

    baseline_before = set(sdk_backends._baseline)
    selectable_before = set(sdk_backends._selectable)
    try:
        sdk_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)
        assert ACP_BACKEND_CLAUDE in sdk_backends.selectable_backends()
    finally:
        sdk_backends._baseline.clear()
        sdk_backends._baseline.update(baseline_before)
        sdk_backends._selectable.clear()
        sdk_backends._selectable.update(selectable_before)


def test_deepseek_is_the_only_unverified_known_backend() -> None:
    """The audit the refusal rests on, kept as a test so it cannot go quietly stale.

    If a second harness ever resolves to ``UNVERIFIED`` -- including by being absent
    from the routing table, which ``routing_for`` answers ``UNVERIFIED`` for -- the
    refusal above starts applying to it too. That may be right, but it must be
    noticed rather than discovered when an edition's registration begins failing.
    """
    unverified = {b for b in ACP_BACKENDS_KNOWN if routing_for(b) is Routing.UNVERIFIED}
    assert unverified == {ACP_BACKEND_DEEPSEEK}
    # Every known id is named EXPLICITLY, so none of the others is unverified merely
    # by omission.
    from kiro_crew.acp_backends import ACP_BACKEND_ROUTING

    assert set(ACP_BACKEND_ROUTING) >= ACP_BACKENDS_KNOWN
    # And no unverified id is in the shipped baseline, which is what makes the
    # refusal a no-op for every harness carried today.
    assert not unverified & set(BASELINE_SELECTABLE_BACKENDS)


def test_it_carves_nothing_out_of_the_credential_deny_list() -> None:
    """An empty ``adapter_own_leaves`` is what keeps this harness out of that blast radius.

    A non-empty one removes the named leaf from the OS deny list for the whole
    sandboxed process tree, not for the adapter alone -- and this harness ships a
    ``bash`` tool, so a shell it spawns would reach the live token with a plain
    ``open()``. Being unenforced, it asks the mask for nothing and the mask gives it
    nothing.
    """
    from kiro_crew.agent_sdk import host_auth
    from kiro_crew.agent_sdk import tool_gate as gate

    assert host_auth.declaration_for(ACP_BACKEND_DEEPSEEK).adapter_own_leaves == ()
    assert ACP_BACKEND_DEEPSEEK not in gate.ADAPTER_OWN_CREDENTIAL_LEAVES
    assert gate.adapter_hidden_credential_dirs(ACP_BACKEND_DEEPSEEK) == ()


def test_the_credential_leaves_are_declared_with_their_override_spelling() -> None:
    """The floor re-anchors by the spelling declared here, so it is stated.

    ``DSH_HOME`` stands in for the leaves' own parent, so each keeps its final
    segment. That happens to be what an empty tuple would anchor; spelling it out
    is what lets a reader check the right file is fenced without re-deriving which
    prefix the variable replaces.
    """
    from kiro_crew.agent_sdk import host_auth

    declaration = host_auth.declaration_for(ACP_BACKEND_DEEPSEEK)
    assert declaration.credential_leaves == (".dsh/.credentials.yaml", ".dsh/.env")
    assert declaration.home_override_env_vars == ("DSH_HOME",)
    assert declaration.override_relative_leaves == (".credentials.yaml", ".env")
    assert declaration.host_logout_retires_children is False


# ── The restore verb ─────────────────────────────────────────────────────────


def _client(tmp_path: pathlib.Path, backend: str) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    return client


def _arm_restore(client: AcpClient, capabilities: dict, restore_result: dict) -> list[str]:
    """Drive one handshake with a resume id pending; return the methods sent."""
    sent: list[str] = []
    responses = [{"protocolVersion": 1, "agentCapabilities": capabilities}, restore_result]

    async def fake_send(method: str, params: dict) -> int:
        sent.append(method)
        return len(sent)

    async def fake_wait(req_id: int, timeout: float = 50.0, *, method="", expected_mcp=None):
        # A handshake that does not restore falls through to session/new, so every
        # id past the scripted ones answers as a fresh session rather than as {}.
        if req_id <= len(responses):
            scripted = responses[req_id - 1]
            if sent and sent[req_id - 1] == "session/new":
                return {"sessionId": "fresh"}
            return scripted
        return {"sessionId": "fresh"}

    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client._resume_session_id = "prior-session"
    return sent


@pytest.mark.asyncio
async def test_a_member_is_sent_session_resume(tmp_path: pathlib.Path) -> None:
    """The verb follows the membership set, and the restore is adopted."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    sent = _arm_restore(
        client,
        {"sessionCapabilities": {"resume": {}, "list": {}, "close": {}}},
        {"configOptions": []},
    )

    await client._initialize_session()

    assert "session/resume" in sent
    assert "session/load" not in sent
    assert client._session_id == "prior-session"
    assert client._resumed is True


@pytest.mark.asyncio
async def test_a_non_member_is_still_sent_session_load(tmp_path: pathlib.Path) -> None:
    """The other arm, so the set is what decides rather than the new code path.

    Without this the verb swap could be unconditional and every kiro-family resume
    would be sent a method kiro-cli does not serve.
    """
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    sent = _arm_restore(client, {"loadSession": True}, {"modes": ["chat"]})

    await client._initialize_session()

    assert "session/load" in sent
    assert "session/resume" not in sent


@pytest.mark.asyncio
async def test_the_capability_is_read_where_this_harness_advertises_it(
    tmp_path: pathlib.Path,
) -> None:
    """A harness advertising no ``resume`` must not be sent the verb at all.

    ``loadSession`` is absent from this harness's ``initialize`` result, so reading
    that flag would answer False and silently start every reopened session fresh.
    Reading the wrong key is the failure this pins.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    sent = _arm_restore(client, {"sessionCapabilities": {"list": {}}}, {"configOptions": []})

    await client._initialize_session()

    assert "session/resume" not in sent
    assert client._resumed is False


@pytest.mark.asyncio
async def test_a_restore_with_no_modes_block_is_adopted(tmp_path: pathlib.Path) -> None:
    """This harness can never return ``modes``; gating on one would discard the restore."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    _arm_restore(client, {"sessionCapabilities": {"resume": {}}}, {"configOptions": []})

    await client._initialize_session()

    assert client._resumed is True


def test_both_varying_reads_are_keyed_on_the_one_set() -> None:
    """The capability AND the verb, from the same membership, in the shared path.

    Two sets would invite an entry in one and not the other, which reads as "cannot
    restore" and starts every reopened session fresh. A source scan because the
    hazard is a second decision site appearing, which no behavioural test sees.
    """
    body = inspect.getsource(AcpClient._initialize_session)
    assert body.count("ACP_BACKENDS_RESUME_WITHOUT_LOAD") == 2
    assert 'session_capabilities.get("resume")' in body
    assert "METHOD_SESSION_RESUME" in body
    assert "restore_method" in body


def test_the_restore_params_are_built_once_for_either_verb() -> None:
    """One params dict, because the two calls share a contract.

    ``ResumeSessionRequest`` carries the same fields as ``LoadSessionRequest``, so a
    second dict would be a copy that can drift rather than an abstraction.
    """
    body = inspect.getsource(AcpClient._initialize_session)
    assert body.count("load_params: dict = {") == 1


def test_the_restore_method_is_resolved_before_the_try() -> None:
    """The failure log names the verb, so the name must be bound on every path."""
    body = inspect.getsource(AcpClient._initialize_session)
    resolved = body.index("restore_method = (")
    guarded = body.index("try:", resolved - 400)
    assert resolved < guarded, "restore_method must be bound before the guarded block"


# ── Spawn and handshake ──────────────────────────────────────────────────────


def test_a_stalled_restore_keeps_its_mcp_detail() -> None:
    """The timeout message enricher has to know BOTH restore verbs.

    A stalled restore is one of the few failures an operator reads verbatim, and the
    MCP progress detail the caller supplies is what makes it actionable. The enricher
    gates on a set of methods, so a harness sent the other verb loses that detail
    while the message still claims to describe the restore.
    """
    body = inspect.getsource(AcpClient._wait_for_response)
    assert (
        "{METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SESSION_RESUME}" in body
    ), "the enricher must name every restore verb this client can send"


def test_the_handshake_is_the_spec_dialect() -> None:
    """Integer ``protocolVersion`` 1, captured off its own wire."""
    from kiro_crew.acp.client import _PROTOCOL_VERSION_BY_BACKEND

    assert _PROTOCOL_VERSION_BY_BACKEND[ACP_BACKEND_DEEPSEEK] == 1


def test_the_argv_is_the_host_binary_plus_the_shipped_profile() -> None:
    """The ACP package is a plugin with no executable; the host binary boots it."""
    from kiro_crew.acp.client import (
        DEEPSEEK_ACP_PROFILE_ARGS,
        DEEPSEEK_BIN,
        DEEPSEEK_INSTALL_COMMAND,
    )

    assert DEEPSEEK_BIN == "dsh"
    assert DEEPSEEK_ACP_PROFILE_ARGS == ("--profile", "acp")
    assert DEEPSEEK_INSTALL_COMMAND == "npm i -g @deepseek-ai/dsh"

    body = inspect.getsource(AcpClient._spawn)
    assert "argv = [deepseek_bin, *DEEPSEEK_ACP_PROFILE_ARGS]" in body


def test_the_resolution_ladder_prefers_the_explicit_override(monkeypatch, tmp_path) -> None:
    """Override, then mise, then PATH -- the plain-binary ladder.

    What is pinned is the ORDER, so executability is STUBBED rather than staged on
    disk. A file written and chmod-ed here answers ``is_executable_file`` on POSIX
    and not on Windows, where an executable needs a recognised extension -- so a
    disk-staged override fell through to the mise rung there, and this test was
    asserting the platform's notion of an executable instead of the precedence it
    exists to pin.
    """
    from kiro_crew.acp import client as client_module

    binary = tmp_path / "dsh"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("DSH_BIN", str(binary))
    monkeypatch.setattr(
        client_module.platform_compat,
        "is_executable_file",
        lambda candidate: str(candidate) == str(binary),
    )
    monkeypatch.setattr(client_module, "_mise_which", lambda _name: "/never/reached")

    resolved, _searched = client_module._resolve_deepseek_bin()
    assert resolved == str(binary)


def test_an_absent_binary_reports_what_was_searched(monkeypatch) -> None:
    """A caller must be able to say where it looked rather than raising from inside."""
    from kiro_crew.acp import client as client_module

    monkeypatch.delenv("DSH_BIN", raising=False)
    monkeypatch.setattr(client_module, "_mise_which", lambda _name: None)
    monkeypatch.setattr(client_module.shutil, "which", lambda *_a, **_kw: None)

    resolved, searched = client_module._resolve_deepseek_bin()
    assert resolved is None
    assert searched


def test_the_sandbox_posture_is_pinned_in_the_child_environment() -> None:
    """Defence in depth, and not a routing claim.

    One variable selects both a sandbox mode and an approval policy on this
    harness, and its permissive end runs sensitive actions without asking anyone.
    Pinning the confined value keeps an inherited shell variable from selecting
    that end; it does not make a tool call reach Crew's gate.
    """
    from kiro_crew.acp.client import _ENV_DEEPSEEK_PERMISSION_MODE, DEEPSEEK_PERMISSION_MODE

    assert _ENV_DEEPSEEK_PERMISSION_MODE == "DSH_PERMISSION_MODE"
    assert DEEPSEEK_PERMISSION_MODE == "workspace-write"

    body = inspect.getsource(AcpClient._spawn)
    assert "env[_ENV_DEEPSEEK_PERMISSION_MODE] = DEEPSEEK_PERMISSION_MODE" in body


def test_the_arm_runs_no_credential_mask_preflight() -> None:
    """An unenforced harness must not take a preflight call site.

    ``test_acp_tool_gate.test_every_enforced_harness_reaches_the_spawn_preflight``
    counts one ``_sandbox_preflight`` call per ENFORCED harness, so a call on this
    arm is not merely dead -- it breaks that count. Pinned here as well so the
    reason travels with the arm rather than only with the counter.
    """
    body = inspect.getsource(AcpClient._spawn)
    arm = body[body.index("elif self._is_deepseek:") :]
    arm = arm[: arm.index("\n        else:")]
    assert "_sandbox_preflight" not in arm
    assert "adapter_expose_files" not in arm


# ── The effort option id, read rather than spelled ───────────────────────────


def test_the_effort_levels_are_parsed_under_this_harness_own_option_id(tmp_path) -> None:
    """A hard-coded ``effort`` here reads as "no levels offered" rather than a miss."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    client._acp_config_options = [
        {
            "id": "reasoning_effort",
            "options": [{"value": "off"}, {"value": "low"}, {"value": "high"}],
        }
    ]

    assert client.get_valid_effort_levels() == ["off", "low", "high"]


def test_another_harness_still_parses_the_default_option_id(tmp_path) -> None:
    """The other arm of the table, so the read is keyed and not simply renamed."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    client._acp_config_options = [{"id": "effort", "options": [{"value": "high"}]}]

    assert client.get_valid_effort_levels() == ["high"]


def test_the_levels_parser_reads_the_table(tmp_path) -> None:
    """The id comes from ``effort_option_id``, not from a literal at this site."""
    body = inspect.getsource(AcpClient.get_valid_effort_levels)
    assert "effort_option_id(self.backend)" in body
    assert '== "effort"' not in body


def test_no_site_that_asks_about_effort_spells_the_id_itself() -> None:
    """Membership in the effort set is worth nothing if a consumer hard-codes the id.

    The levels parser is one of four sites. The advertised-option CHECK and the two
    pushes live in ``providers/acp.py``, and a literal at any of them makes the whole
    channel a silent no-op for this harness: the check reports the option
    unsupported, the push never happens, and the dropdown still offers levels that
    can never be applied.
    """
    from kiro_crew.providers import acp as provider_module

    body = inspect.getsource(provider_module.AcpProvider)
    assert 'supports_config_option("effort")' not in body
    assert 'set_config_option("effort"' not in body
    assert body.count("effort_option_id(self._client.backend)") == 2


@pytest.mark.asyncio
async def test_the_effort_push_uses_this_harness_own_option_id(tmp_path) -> None:
    """The push reaches the wire under the id the harness advertised."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = MagicMock()
    provider._client.backend = ACP_BACKEND_DEEPSEEK
    provider._client.supports_config_option = MagicMock(return_value=True)
    provider._client.set_config_option = AsyncMock()
    provider._client._model = "deepseek-v4-flash"

    await provider._set_effort_config_option("high")

    asked = provider._client.supports_config_option.call_args[0][0]
    pushed = provider._client.set_config_option.await_args[0][0]
    assert asked == "reasoning_effort"
    assert pushed == "reasoning_effort"


def test_a_grouped_model_select_is_flattened_before_the_value_filter(tmp_path) -> None:
    """This harness groups its model options, and the capture is the only vocabulary.

    Its ``session/new`` nests the real choices under a ``{"group": …, "options": […]}``
    wrapper that carries no ``value`` of its own. A filter keeping only entries with a
    ``value`` empties on that shape, and an empty list reads as "advertised no
    models" -- which for a harness whose ids exist nowhere else means no model can be
    offered or resolved at all.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    grouped = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": '["deepseek-official","deepseek-v4-flash"]',
                "options": [
                    {
                        "group": "deepseek-official",
                        "name": "DeepSeek",
                        "options": [
                            {
                                "value": '["deepseek-official","deepseek-v4-flash"]',
                                "name": "DeepSeek-V4-Flash",
                            }
                        ],
                    }
                ],
            }
        ]
    }

    envelope = client._models_from_config_options(grouped)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == [
        '["deepseek-official","deepseek-v4-flash"]'
    ]
    assert envelope["currentModelId"] == '["deepseek-official","deepseek-v4-flash"]'


def test_a_flat_model_select_is_still_read_unchanged(tmp_path) -> None:
    """The other arm: a harness that does not group must be unaffected by the flatten."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    flat = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": "sonnet",
                "options": [{"value": "sonnet", "name": "Sonnet"}],
            }
        ]
    }

    envelope = client._models_from_config_options(flat)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == ["sonnet"]


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(123, id="nested-options-is-a-number"),
        pytest.param(True, id="nested-options-is-a-bool"),
        pytest.param("abc", id="nested-options-is-a-string"),
        pytest.param({"a": {"value": "x"}}, id="nested-options-is-a-mapping"),
        pytest.param([None, 7, "x"], id="nested-options-holds-non-objects"),
    ],
)
def test_a_malformed_group_degrades_instead_of_failing_the_session(tmp_path, malformed) -> None:
    """Every level of this payload comes off the wire, so none of it may be trusted.

    A truthy non-iterable in a group's ``options`` raised ``TypeError`` from inside
    ``session/new`` handling, so a malformed or hostile agent response failed session
    initialization rather than degrading to "this harness advertised no models".
    Parametrized over the shapes a wire payload can actually take, because the outer
    list and the nested one need the SAME narrowing and only the outer one had it.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    payload = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "options": [{"group": "g", "name": "G", "options": malformed}],
            }
        ]
    }

    assert client._models_from_config_options(payload) is None


def test_a_malformed_group_beside_a_good_one_keeps_the_good_one(tmp_path) -> None:
    """Degrading must not mean discarding: one bad group cannot cost the whole list."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    payload = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "options": [
                    {"group": "broken", "options": 123},
                    {"group": "good", "options": [{"value": "real-id", "name": "Real"}]},
                ],
            }
        ]
    }

    envelope = client._models_from_config_options(payload)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == ["real-id"]


def test_the_committed_fixture_is_the_shape_the_capture_must_read(tmp_path) -> None:
    """Read the real captured frame, so the parser is pinned against the wire itself.

    A hand-written grouped payload could drift from what the harness sends; the
    fixture cannot, because it IS what the harness sent.
    """
    import json

    fixture = (
        pathlib.Path(__file__).parent
        / "fixtures"
        / "acp_frames"
        / "deepseek"
        / "handshake-live.jsonl"
    )
    session_new = next(
        frame["result"]
        for frame in (json.loads(line) for line in fixture.read_text().splitlines()[1:])
        if isinstance(frame.get("result"), dict) and "configOptions" in frame["result"]
    )

    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    envelope = client._models_from_config_options(session_new)

    assert envelope is not None
    assert envelope["availableModels"], "the captured select must yield at least one model"
