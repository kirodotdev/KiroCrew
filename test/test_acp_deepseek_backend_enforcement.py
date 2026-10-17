"""What keeps the DeepSeek Harness's verified routing enforced: nothing of the adapter on the
shared construction path, gate artifacts and their sweep in the sealed leaf, the ungated-call
tripwire, the refusal to register an unverified harness, and a deny list it carves nothing from.
"""

from __future__ import annotations

import os
import pathlib
from pathlib import PurePath
from unittest.mock import AsyncMock

import pytest
from acp_deepseek_backend_helpers import (  # noqa: F401 -- fixtures are used by name
    _pinned_kiro_cli_version,
)

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import deepseek as deepseek_mod
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    BASELINE_SELECTABLE_BACKENDS,
    Routing,
    routing_for,
)


def test_the_shared_construction_tail_carries_nothing_from_this_adapter(
    monkeypatch, tmp_path
) -> None:
    """harness-parity H13: no work of this adapter's runs on the Kiro construction path.

    The launch tail and the post-spawn path are shared by EVERY backend, the kiro one
    included, so anything of this adapter's placed there runs on every Kiro session
    start. Every entry point of the adapter -- its launch, its env steps, its probe,
    its vault and key-name reads, its seal and patch writers -- is booby-trapped here,
    and the full Kiro launch is then driven through ``_spawn`` (and its post-spawn
    bookkeeping) to the same launch facts the committed golden records.
    """
    import acp_launch_capture as capture_mod

    def _trap(name):
        def _raise(*_a, **_kw):
            raise AssertionError(f"{name} ran on the Kiro construction path (harness-parity H13)")

        return _raise

    for name in ("resolve_spawn", "apply_spawn_env", "prepare_spawn_env"):
        monkeypatch.setattr(deepseek_mod.DeepseekLaunch, name, _trap(f"DeepseekLaunch.{name}"))
    for name in (
        "_verify_deepseek_gate",
        "_deepseek_vault_env",
        "_deepseek_vault_env_names",
        "_seal_deepseek_gate_extension",
        "_write_deepseek_gate_patch",
        "_validate_deepseek_env_mapping",
        "_read_deepseek_gate_marker",
    ):
        monkeypatch.setattr(deepseek_mod, name, _trap(name))

    answer = capture_mod.capture(ACP_BACKEND_KIRO, tmp_path)
    assert answer == capture_mod.read_golden()[capture_mod.golden_key(ACP_BACKEND_KIRO)]


def test_a_denial_the_harness_reports_does_not_trip_the_ungated_call_guard() -> None:
    """A denied call cannot be mistaken for one that ran without asking.

    Two shapes of denial reach a gated session, and neither trips the guard, for two
    different reasons observed against the harness rather than assumed:

    * a SANDBOX denial happens while the tool body runs, so the call was gated
      normally first -- the plugin upgraded the downstream ``allow`` to ``ask``, the
      client answered, and only then did the sandbox refuse the write. Its id is in
      the asked set, so the ``completed`` it reports is a call the gate saw;
    * a POLICY denial from a downstream ``tools/pre-execute`` listener is preserved
      by the plugin WITHOUT asking, which is the point -- the harness's own refusal
      must not soften into a question. Such a call never runs, and the harness
      reports it ``failed`` rather than ``completed``.

    The guard only ever fires on ``completed``, so the second shape is outside it by
    construction. This pins that, because the alternative -- treating a preserved
    deny as an ungated execution -- would kill the harness every time an operator's
    own policy plugin said no.
    """
    import asyncio

    from kiro_crew.acp.types import JsonRpcMessage

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}
    client._session_id = "s1"
    client._session_key = "k1"
    client._kill_process = AsyncMock(return_value=None)

    def update(tool_call_id: str, status: str) -> JsonRpcMessage:
        return JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "status": status,
                },
            },
        )

    # A policy denial the plugin preserved without asking: never asked, never ran,
    # reported ``failed``. Must not trip.
    asyncio.run(client._tripwire_pi_gate(update("policy-denied", "failed")))
    assert client._kill_process.await_count == 0, (
        "a call the harness itself denied was counted as an ungated execution; an "
        "operator policy plugin saying no would kill every session"
    )

    # A sandbox denial: gated normally, so its id IS in the asked set, and the
    # ``completed`` it reports carries the sandbox's refusal in the tool result.
    client._pi_gate_asked_ids.add("sandbox-denied")
    asyncio.run(client._tripwire_pi_gate(update("sandbox-denied", "completed")))
    assert client._kill_process.await_count == 0

    # The guard still has teeth for the case it exists for.
    from kiro_crew.acp.client import AcpToolGateUnroutable

    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_pi_gate(update("never-asked", "completed")))
    assert client._kill_process.await_count == 1


def test_the_load_marker_is_never_written_into_a_sealed_leaf() -> None:
    """The marker is written by the CHILD, so it cannot live where the child cannot write.

    The gate-artifact leaf is sealed read-only against every harness child on
    purpose, so that no child can plant what a later session loads
    (``sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES``). That makes it exactly the
    wrong home for the one file the child must create: a marker directed there is
    never written, the read-back reports an absent gate, and every session is
    refused for a gate that had in fact loaded. This pins the pairing itself rather
    than the current path spelling, so moving the marker back under any sealed leaf
    fails here instead of in production.
    """
    from kiro_crew import sandbox as sandbox_module
    from kiro_crew.acp import client as client_module
    from kiro_crew.agent_sdk.tool_gate import PI_GATE_ARTIFACT_LEAF

    # The leaf the gate's own code is sealed into is read-only to the child ...
    assert PI_GATE_ARTIFACT_LEAF in sandbox_module._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    # ... so the marker must not be placed in it, nor in any other sealed leaf.
    artifact_dir = pathlib.Path(client_module._pi_gate_artifact_dir())
    marker_dir = pathlib.Path("/scratch-window/session-1")
    marker = marker_dir / "kirocrew_dsh_gate_deadbeef.marker.json"
    assert artifact_dir not in marker.parents
    for leaf in sandbox_module._CREW_NOFOLLOW_READONLY_DIR_LEAVES:
        assert leaf not in marker.parts, f"the marker sits under the sealed leaf {leaf!r}"
    # The name carries the per-session nonce rather than only a pid, so two clients
    # in one gateway process cannot collide on it.
    assert "deadbeef" in marker.name


def test_every_file_the_gate_writers_leave_in_the_leaf_is_swept(monkeypatch, tmp_path) -> None:
    """The leaf's invariant is a ratchet for this harness too, not a comment.

    The pi suite pins that every name ITS writers produce is claimed by the leaf
    sweep's family table; this harness's writers landed in the same leaf without
    that pin, so the sealed plugin and its patch were never reclaimed after the
    gateway that wrote them exited -- one pair per gateway process, forever. The
    property is asserted here so a future writer dropping a differently-named file
    into the leaf fails this test rather than leaving an unswept file behind.
    """
    from kiro_crew import sandbox as sandbox_module
    from kiro_crew.acp import client as client_module

    artifact_dir = tmp_path / "pi-gate"
    artifact_dir.mkdir()
    monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(artifact_dir))
    sealed = client_module._seal_deepseek_gate_extension()
    client_module._write_deepseek_gate_patch(sealed)
    families = sandbox_module._PI_GATE_DIR_ARTIFACTS
    written = sorted(entry.name for entry in artifact_dir.iterdir())
    assert written, "the writers produced nothing to check"
    # The mkstemp stages are renamed away on success, so they are not on disk to be
    # listed; a stage orphaned by a crash between mkstemp and replace has to be
    # reclaimed too, so both stage spellings are checked alongside the survivors.
    pid = os.getpid()
    written += [f"kirocrew_dsh_gate_{pid}_stage.tmp", f"kirocrew_dsh_patch_{pid}_stage.tmp"]
    for name in written:
        prefix = next((p for p in families if name.startswith(p)), None)
        assert prefix is not None, f"{name} is written to the leaf but no sweep family claims it"
        assert any(
            name.endswith(suffix) for suffix in families[prefix]
        ), f"{name} carries a suffix the sweep family does not reclaim"


def test_stale_dsh_gate_artifacts_are_swept_from_the_leaf(monkeypatch, tmp_path) -> None:
    """A dead gateway's sealed plugin, patch and orphaned stages are all reclaimed."""
    from kiro_crew import sandbox as sandbox_module

    artifact_dir = tmp_path / "pi-gate"
    artifact_dir.mkdir()
    stale = [
        artifact_dir / "kirocrew_dsh_gate_999999.mjs",
        artifact_dir / "kirocrew_dsh_gate_999999.patch.yml",
        artifact_dir / "kirocrew_dsh_gate_999999_abc123.tmp",
        artifact_dir / "kirocrew_dsh_patch_999999_abc123.tmp",
    ]
    for path in stale:
        path.write_text("gate", encoding="utf-8")
    monkeypatch.setattr(sandbox_module.platform_compat, "pid_exists", lambda _pid: False)
    removed = sandbox_module.cleanup_stale_sandbox_profiles(
        data_home=tmp_path, legacy_dir=str(tmp_path / "absent")
    )
    assert removed == len(stale)
    assert not any(path.exists() for path in stale)


def test_a_live_gateways_dsh_gate_artifacts_survive_the_sweep(monkeypatch, tmp_path) -> None:
    """The artifacts are written once and REUSED by every later spawn of the gateway.

    Their age therefore says nothing, and the sweep must decide on the owner's
    liveness alone -- an age rule here would delete the sealed plugin from under a
    running gateway's next deepseek spawn.
    """
    from kiro_crew import sandbox as sandbox_module

    artifact_dir = tmp_path / "pi-gate"
    artifact_dir.mkdir()
    live = [
        artifact_dir / "kirocrew_dsh_gate_424242.mjs",
        artifact_dir / "kirocrew_dsh_gate_424242.patch.yml",
    ]
    for path in live:
        path.write_text("gate", encoding="utf-8")
        # Well past the launcher age threshold: age must not be what decides.
        ancient = 1_000_000_000
        os.utime(path, (ancient, ancient))
    monkeypatch.setattr(sandbox_module.platform_compat, "pid_exists", lambda _pid: True)
    removed = sandbox_module.cleanup_stale_sandbox_profiles(
        data_home=tmp_path, legacy_dir=str(tmp_path / "absent")
    )
    assert removed == 0
    assert all(path.exists() for path in live)


def test_a_completed_call_the_gate_never_asked_about_stops_the_session() -> None:
    """The in-band guard that speaks for the SESSION's own child, not for a probe.

    The read-back proves the gate loaded in the child it booted. That the harness
    then asks for every call is its own behaviour, which no client-side read pins
    across upgrades -- and this harness's marker is written by the gate itself, so
    the read-back attests that Crew's code ran rather than that the harness reports
    it running. This is the check that does not depend on either: a
    ``tool_call_update`` reaching ``completed`` for an id no permission frame ever
    named is a call that ran with none of Crew's controls consulted, so the harness
    is stopped and the turn fails rather than continuing ungoverned.

    Shared with pi deliberately -- one tripwire, one state, so a fix to either
    lands in both.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable
    from kiro_crew.acp.types import JsonRpcMessage

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}
    client._session_id = "s1"
    client._session_key = "k1"
    client._kill_process = AsyncMock(return_value=None)

    completed = JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "never-asked",
                "status": "completed",
            },
        },
    )
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_pi_gate(completed))
    assert "never-asked" in str(refused.value)
    assert client._kill_process.await_count == 1

    # The same update for a call the gate DID ask about is not a bypass.
    client._kill_process = AsyncMock(return_value=None)
    client._pi_gate_asked_ids.add("asked")
    ok = JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "asked",
                "status": "completed",
            },
        },
    )
    asyncio.run(client._tripwire_pi_gate(ok))
    assert client._kill_process.await_count == 0


def test_the_permission_frame_records_the_call_the_tripwire_will_look_for() -> None:
    """The frame names the call directly here, unlike pi's dialog envelope.

    The tripwire only trusts a completed call the gate asked about first, so the
    recording side has to read this harness's own shape: an ordinary ACP permission
    frame whose ``toolCall`` carries just a ``toolCallId``. Reading it through pi's
    envelope would record nothing and every completed call would trip.
    """
    from kiro_crew.acp.types import JsonRpcMessage

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}

    client._note_pi_gate_asked(
        JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={"sessionId": "s1", "toolCall": {"toolCallId": "call-1"}},
        )
    )
    assert client._pi_gate_asked_ids == {"call-1"}
    assert client._pi_gate_request_tool["7"] == "call-1"


def _deepseek_tripwire_client(monkeypatch):
    """A gated DeepSeek client whose kill is observable, shaped as the pi tripwire tests do."""
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}
    client._permission_options = {}
    client._permission_gate_events = {}
    client._session_id = "s1"
    client._session_key = "k1"
    killed: list = []

    async def _kill(*, force=False):
        killed.append(force)

    async def _send(request_id, payload):
        pass

    monkeypatch.setattr(client, "_kill_process", _kill)
    monkeypatch.setattr(client, "_send_response", _send)
    return client, killed


def _asked(client, frame) -> None:
    """The frame was asked about: the tripwire noted it and the client recorded its event.

    ``approve_tool`` approves only a request the client built an event for, so a
    test that approves seeds the event the builder would have recorded.
    """
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    client._note_pi_gate_asked(frame)
    client._permission_gate_events[frame.id] = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST, request_id=frame.id, title="notes.txt"
    )


def _deepseek_permission_frame(tool_call_id: str, request_id: int):
    """This harness's own permission frame: the call is named directly, with no envelope."""
    from kiro_crew.acp.types import JsonRpcMessage

    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={"sessionId": "s1", "toolCall": {"toolCallId": tool_call_id}},
    )


def _deepseek_completed(tool_call_id: str):
    from kiro_crew.acp.types import JsonRpcMessage

    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": "completed",
            },
        },
    )


def test_a_re_asked_call_is_judged_on_its_fresh_verdict_not_the_stale_deny(monkeypatch) -> None:
    """A denial must not outlive the id it was recorded against, and here ids recur.

    This harness forwards the PROVIDER's tool-call id, and an OpenAI-compatible
    provider that mints per-response ids repeats them across the several steps it runs
    in one turn -- so the same id can be denied once and legitimately asked about again.
    A denial that survived the second ask would kill a healthy session on the approved
    call's own ``completed`` frame, which is why a fresh ask clears the deny.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable

    client, killed = _deepseek_tripwire_client(monkeypatch)

    first = _deepseek_permission_frame("call_0", 1)
    client._note_pi_gate_asked(first)
    asyncio.run(client.reject_tool(first.id))
    assert "call_0" in client._pi_gate_denied_ids

    second = _deepseek_permission_frame("call_0", 2)
    _asked(client, second)
    assert "call_0" not in client._pi_gate_denied_ids, "a fresh ask is a fresh verdict"
    asyncio.run(client.approve_tool(second.id))

    asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert killed == []

    # And the third link is untouched: with no fresh frame, the denial still holds.
    other, other_killed = _deepseek_tripwire_client(monkeypatch)
    denied = _deepseek_permission_frame("call_1", 1)
    other._note_pi_gate_asked(denied)
    asyncio.run(other.reject_tool(denied.id))
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(other._tripwire_pi_gate(_deepseek_completed("call_1")))
    assert "DENIED" in str(refused.value)
    assert other_killed == [True]


def test_a_completed_call_consumes_its_ask_so_a_reused_id_must_be_asked_again(monkeypatch) -> None:
    """An approval must not outlive the call it was given for, because ids recur here.

    The mirror of the stale-deny case above: this harness forwards the PROVIDER's
    tool-call id, and a provider that mints per-response ids repeats ``call_0`` across
    the steps of one turn. An ask that survived its call's ``completed`` frame would
    vouch for the NEXT ``call_0`` -- one the gate was never asked about -- and the
    tripwire would trust it. So the terminal frame consumes the id's state, and a
    reused id is an unasked call until a fresh permission frame names it.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable

    client, killed = _deepseek_tripwire_client(monkeypatch)
    asked = _deepseek_permission_frame("call_0", 1)
    _asked(client, asked)
    asyncio.run(client.approve_tool(asked.id))
    asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert killed == [], "the asked-and-approved call completes without a kill"
    assert "call_0" not in client._pi_gate_asked_ids, "the terminal frame consumed the ask"

    # The same id again, with NO fresh permission frame: an unasked call.
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert "without asking" in str(refused.value)
    assert killed == [True]

    # And a fresh ask for the reused id earns it a fresh, trusted verdict.
    fresh, fresh_killed = _deepseek_tripwire_client(monkeypatch)
    for request_id in (1, 2):
        frame = _deepseek_permission_frame("call_0", request_id)
        _asked(fresh, frame)
        asyncio.run(fresh.approve_tool(frame.id))
        asyncio.run(fresh._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert fresh_killed == []


def test_a_failed_terminal_consumes_the_gate_state_too(monkeypatch) -> None:
    """``failed`` is the other terminal, and it must not leave a verdict behind either.

    A denied call is EXPECTED to fail, and that failure ends the call. Were the deny
    to survive it, a later reuse of the id that the gate approved afresh would still
    be judged on the old deny -- the stale-state kill the fresh-ask discard exists to
    prevent -- and were an ask to survive its call's failure, the reuse would inherit
    an approval it never earned.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable
    from kiro_crew.acp.types import JsonRpcMessage

    def failed(tool_call_id: str) -> JsonRpcMessage:
        frame = _deepseek_completed(tool_call_id)
        frame.params["update"]["status"] = "failed"
        return frame

    client, killed = _deepseek_tripwire_client(monkeypatch)
    denied = _deepseek_permission_frame("call_0", 1)
    client._note_pi_gate_asked(denied)
    asyncio.run(client.reject_tool(denied.id))
    asyncio.run(client._tripwire_pi_gate(failed("call_0")))
    assert killed == [], "a denied call failing is the expected outcome"
    assert "call_0" not in client._pi_gate_denied_ids
    assert "call_0" not in client._pi_gate_asked_ids

    # The id, reused with no fresh ask, is now judged as UNASKED rather than as denied.
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert "without asking" in str(refused.value)
    assert killed == [True]


def test_registering_an_unverified_harness_as_selectable_is_refused(monkeypatch) -> None:
    """KNOWN must not be enough to reach the switch, and this is where that is enforced.

    The guard outlives its original subject. deepseek was the harness that needed
    it -- KNOWN, so a governance rule could deny it, and unrouted, so it must not
    reach the switch -- and now that its routing is VERIFIED there is no KNOWN id
    left that is UNVERIFIED (``test_no_known_backend_is_unverified``). So the
    refusal is exercised by making this same id unrouted again, which keeps the
    guard covered for the next harness onboarded in that state rather than
    deleting it with the condition that motivated it.
    """
    from kiro_crew.agent_sdk import backends as sdk_backends

    monkeypatch.setitem(sdk_backends.ACP_BACKEND_ROUTING, ACP_BACKEND_DEEPSEEK, Routing.UNVERIFIED)
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


def test_no_known_backend_is_unverified() -> None:
    """The audit the refusal rests on, kept as a test so it cannot go quietly stale.

    deepseek was the one member of this set, and this change empties it. If a
    harness ever resolves to ``UNVERIFIED`` again -- including by being absent from
    the routing table, which ``routing_for`` answers ``UNVERIFIED`` for -- the
    refusal above starts applying to it. That may be right, but it must be noticed
    rather than discovered when an edition's registration begins failing.
    """
    unverified = {b for b in ACP_BACKENDS_KNOWN if routing_for(b) is Routing.UNVERIFIED}
    assert unverified == set()
    # Every known id is named EXPLICITLY, so none of them is unverified merely by
    # omission.
    from kiro_crew.acp_backends import ACP_BACKEND_ROUTING

    assert set(ACP_BACKEND_ROUTING) >= ACP_BACKENDS_KNOWN
    # And the shipped baseline now carries this harness, which is the point of the
    # change: every KNOWN harness is selectable because every one of them is routed.
    assert ACP_BACKEND_DEEPSEEK in set(BASELINE_SELECTABLE_BACKENDS)


def test_it_carves_nothing_out_of_the_deny_list_because_its_key_comes_from_the_vault() -> None:
    """An ENFORCED harness that is fed its key names NO leaf, and that is the point.

    ``adapter_hidden_credential_dirs`` denies the whole read-gate floor minus the
    harness's own leaf, so every other enforced harness spares one or it cannot
    authenticate -- and that carve-out applies to the whole sandboxed process TREE,
    which on a harness shipping ``bash`` means the model can ``open()`` the
    operator's provider key. This harness resolves a key from its INHERITED
    ENVIRONMENT above both files, so Crew hands it one from its own vault
    (``agent.deepseek_env``) and both leaves stay masked. Asserted against the
    resolved mask, not just the declaration: a projection that stopped honouring an
    empty ``adapter_own_leaves`` would reopen them.
    """
    from kiro_crew.agent_sdk import host_auth
    from kiro_crew.agent_sdk import tool_gate as gate

    declaration = host_auth.declaration_for(ACP_BACKEND_DEEPSEEK)
    assert declaration.adapter_own_leaves == ()
    assert declaration.entitlement_source == host_auth.ENTITLEMENT_HOST_VAULT
    # ABSENT from the projection rather than present-and-empty, so the ``.get``
    # readers stay unchanged.
    assert ACP_BACKEND_DEEPSEEK not in gate.ADAPTER_OWN_CREDENTIAL_LEAVES
    # The mask is on, which is what makes the absence of a carve-out mean anything.
    hidden = gate.adapter_hidden_credential_dirs(ACP_BACKEND_DEEPSEEK)
    assert hidden != ()

    # Compared by PATH SEGMENTS, not by substring. The declarations are canonical
    # POSIX-spelled leaves, but the targets come back joined with the OS separator,
    # so a ``str.endswith(".pi/agent/auth.json")`` holds on POSIX and is False on
    # Windows for the same correct target -- which reads as a missing entry rather
    # than as a test that cannot see one.
    def _has_leaf(targets: tuple[str, ...], leaf: str) -> bool:
        want = tuple(PurePath(leaf).parts)
        return any(tuple(PurePath(t).parts)[-len(want) :] == want for t in targets)

    # This harness's own store is DENIED to its own child ...
    assert _has_leaf(hidden, ".dsh/.credentials.yaml")
    assert _has_leaf(hidden, ".dsh/.env")
    # ... on the same footing as every other harness's store, so the floor this mask
    # is derived from has no hole in it at all for this harness.
    assert _has_leaf(hidden, ".pi/agent/auth.json")
    # And nothing is re-exposed read-only inside the masked directories either: a
    # re-exposure would hand the same file back through the other door.
    assert gate.adapter_expose_files(ACP_BACKEND_DEEPSEEK, hidden) == ()


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
