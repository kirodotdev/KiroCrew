"""Non-revoking recovery and shared permission-audit contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import test_connected_computers_desktop_host as desktop_helpers
import test_connected_computers_protected as helpers
import test_connected_computers_remote_routes as remote_helpers

from kiro_crew.connected_computers import cli, desktop_host
from kiro_crew.connected_computers.permission_audit import as_caller, permission_changed
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import finished

vault = helpers.vault
protector = helpers.protector
store = helpers.store
host = desktop_helpers.host
flag = desktop_helpers.flag
enrolled = desktop_helpers.enrolled
gw = remote_helpers.gw


@pytest.fixture
def audited(monkeypatch):
    events = []
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    )
    return events


@pytest.mark.asyncio
async def test_desktop_unknown_to_on_reconciles_without_revocation(monkeypatch):
    instance = object.__new__(desktop_host.DesktopHost)
    instance.controller = SimpleNamespace(start=Mock(), reconcile=AsyncMock(), off=AsyncMock())
    instance._switch_state = AsyncMock(side_effect=[None, True])
    waiting = asyncio.Event()
    parked = asyncio.Event()
    calls = []

    async def wait(_seconds):
        calls.append(1)
        if len(calls) == 1:
            return
        waiting.set()
        await finished(parked.wait(), "stop the recovery watcher")

    monkeypatch.setattr(desktop_host, "asyncio", SimpleNamespace(sleep=wait))
    task = asyncio.create_task(instance._watch_switch())
    try:
        await finished(waiting.wait(), "watcher reconciled and resumed watching")
        instance.controller.start.assert_called_once()
        instance.controller.reconcile.assert_awaited_once()
        instance.controller.off.assert_not_awaited()
    finally:
        task.cancel()
        await finished(asyncio.gather(task, return_exceptions=True), "release watcher")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="native root approval is Linux-only"
)
async def test_desktop_share_and_revoke_emit_one_secret_free_permission_event(
    host,
    store,
    enrolled,
    tmp_path,
    audited,
    monkeypatch,
):
    monkeypatch.setattr(host.controller, "start", lambda: None)
    monkeypatch.setattr(host.controller, "reconcile", AsyncMock())
    root = tmp_path / "project"
    root.mkdir()
    await finished(desktop_helpers._hello(host), "open host")
    described = await finished(desktop_helpers._describe(host), "describe consent")
    audited.clear()
    reply = await finished(
        desktop_helpers._share(host, consent=described["consent"], root=root), "share folder"
    )
    assert reply["ok"], reply
    assert [e["operation"] for e in audited] == ["connected_computer_share"]
    assert audited[0]["caller"] == "desktop-host"
    grant = store.active()[0]
    audited.clear()
    monkeypatch.setattr(host.controller, "_kick_drain", lambda: None)
    await finished(
        host.handle({"id": 3, "op": "revoke", "grant_id": grant.grant_id}), "revoke local share"
    )
    assert [e["operation"] for e in audited] == ["connected_computer_local_revoke"]
    assert audited[0]["caller"] == "desktop-host"
    assert "cred-from-ssh" not in json.dumps(audited)
    assert desktop_helpers.PEER_KEY not in json.dumps(audited)


def test_cli_enroll_and_revoke_each_emit_one_permission_event(tmp_path, monkeypatch, audited):
    from kiro_crew import gateway_identity
    from kiro_crew.connected_computers import flag as preview
    from kiro_crew.connected_computers import vault_doc

    remote = SecretVault(tmp_path / "remote")
    monkeypatch.setattr(vault_doc, "open_vault", lambda: remote)
    monkeypatch.setattr(gateway_identity, "gateway_id", lambda **_kw: "gateway")
    monkeypatch.setattr(preview, "preview_on", lambda: True)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "owner")
    monkeypatch.setattr(
        cli, "_read_stdin", lambda: {"base": "laptop", "fingerprint": "ab" * 32, "label": "Laptop"}
    )
    replies = []
    monkeypatch.setattr(cli, "_reply_on_channel", replies.append)
    assert cli._remote_enroll() == 0
    assert [e["operation"] for e in audited] == ["connected_computer_enroll"]
    assert audited[0]["caller"] == "cli"
    assert replies[0]["credential"] not in json.dumps(audited)
    assert replies[0]["peer_key"] not in json.dumps(audited)
    audited.clear()
    assert cli._remote_end(replies[0]["device_id"], remove=False) == 0
    assert [e["operation"] for e in audited] == ["connected_computer_revoke"]
    assert audited[0]["caller"] == "cli"


def test_local_audit_is_outside_compare_and_swap_retry(store, monkeypatch, audited):
    grant = store.add(helpers._grant())
    audited.clear()
    real_update = store._doc.update

    snapshot = json.loads(helpers._raw(store._doc._vault))

    def retry(mutate):
        mutate(snapshot)
        return real_update(mutate)

    monkeypatch.setattr(store._doc, "update", retry)
    with as_caller("cli"):
        store.revoke(grant.grant_id)
    assert [e["operation"] for e in audited] == ["connected_computer_local_revoke"]
    assert audited[0]["caller"] == "cli"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "remove"])
async def test_remote_end_keeps_authenticated_subject_without_duplicate(gw, monkeypatch, action):
    async def owner(request, operation):
        request["user"] = "authenticated-subject"
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request", owner
    )
    record = await remote_helpers._enroll(gw, 1)
    status, _ = await remote_helpers._call(
        "POST", f"{remote_helpers.DEVICES}/{record.device_id}/{action}"
    )
    assert status == 200
    assert gw.audits == [
        {
            "caller": "authenticated-subject",
            "operation": f"connected_computer_{action}",
            "outcome": "ok",
            "source": "connected_computers",
            "resources": record.device_id,
        }
    ]


@pytest.mark.asyncio
async def test_audit_caller_is_scoped_and_crosses_worker_thread(audited):
    with as_caller("http-subject"):
        await finished(asyncio.to_thread(permission_changed, "local_off"), "audit off loop")
    permission_changed("local_off")
    assert [e["caller"] for e in audited] == ["http-subject", "connected_computers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "off"])
async def test_local_http_mutation_keeps_request_subject(store, audited, monkeypatch, action):
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import local_routes

    grant = await asyncio.to_thread(store.add, helpers._grant())
    audited.clear()

    async def revoke(grant_id):
        return await asyncio.to_thread(store.revoke, grant_id)

    async def off():
        return await asyncio.to_thread(store.revoke_all)

    monkeypatch.setattr(local_routes, "_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(
        controller_mod, "get_controller", lambda: SimpleNamespace(revoke=revoke, off=off)
    )
    request = make_mocked_request("POST", "/", match_info={"grant_id": grant.grant_id})
    request["user"] = "local-http-subject"
    handler = (
        local_routes.api_local_grant_revoke if action == "revoke" else local_routes.api_local_off
    )
    response = await finished(handler(request), "local HTTP permission mutation")
    assert response.status == 200
    assert [(event["caller"], event["operation"]) for event in audited] == [
        ("local-http-subject", f"connected_computer_local_{action}")
    ]


@pytest.mark.parametrize("phase", ["before", "after"])
def test_connected_computer_floor_clears_every_runtime_binding(monkeypatch, phase):
    from contextlib import closing
    from contextvars import ContextVar
    from threading import Lock

    from conftest_resets import _reset_connected_computers_state

    modules = {
        "broker": SimpleNamespace(_BROKER=object()),
        "controller": SimpleNamespace(_CONTROLLER=object()),
        "disable": SimpleNamespace(_STARTUP_TASK=object(), _TASKS={object()}),
        "connector": SimpleNamespace(_LATE_STOPS={object()}),
        "launch": SimpleNamespace(_WATCHERS={object()}),
        "local_routes": SimpleNamespace(_consents={"old": object()}, _consents_lock=Lock()),
        "permission_audit": SimpleNamespace(_caller=ContextVar("test-caller", default="old")),
    }
    reset = _reset_connected_computers_state.__wrapped__()
    if phase == "after":
        next(reset)
    with monkeypatch.context() as patch, closing(reset):
        for name, module in modules.items():
            patch.setitem(sys.modules, "kiro_crew.connected_computers." + name, module)
        if phase == "before":
            next(reset)
        else:
            with pytest.raises(StopIteration):
                next(reset)
        assert modules["controller"]._CONTROLLER is None
        assert modules["broker"]._BROKER is None
        assert modules["disable"]._STARTUP_TASK is None
        assert not modules["disable"]._TASKS
        assert not modules["connector"]._LATE_STOPS
        assert not modules["launch"]._WATCHERS
        assert not modules["local_routes"]._consents
        assert not modules["local_routes"]._consents_lock.locked()
        assert modules["permission_audit"]._caller.get() == "connected_computers"


@pytest.mark.asyncio
async def test_reconcile_revokes_repaired_off_without_a_config_change_event(store, monkeypatch):
    from kiro_crew.config import live
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import flag as preview
    from kiro_crew.connected_computers import local_routes
    from kiro_crew.shared_crew import preview as shared_preview

    settings = SimpleNamespace(feature_previews={preview.PREVIEW_NAME: False})
    cfg = SimpleNamespace(dashboard=settings, _read_degraded_sections={"dashboard"})
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr(shared_preview, "preview_enabled", lambda: False)
    await asyncio.to_thread(store.install_key)
    grant = await asyncio.to_thread(store.add, helpers._grant())
    generation = await asyncio.to_thread(store.off_generation)
    consent = local_routes._mint_consent("inst-1", generation, grant.coordinates)
    controller = controller_mod.LocalController(store, instance_lookup=lambda _: None)
    stopped = AsyncMock()
    drained = Mock()
    monkeypatch.setattr(controller, "stop_all", stopped)
    monkeypatch.setattr(controller, "_kick_drain", drained)

    assert preview.local_switch() is None
    await finished(controller.reconcile(), "preserve grants during unknown configuration")
    assert (await asyncio.to_thread(store.get, grant.grant_id)).active
    assert await asyncio.to_thread(store.off_generation) == generation
    assert local_routes.peek_consent(consent) == "inst-1"
    drained.assert_not_called()

    # Only read provenance changes; normalized feature settings stay identical.
    cfg._read_degraded_sections = set()
    assert preview.local_switch() is False
    await finished(
        asyncio.gather(controller.reconcile(), controller.reconcile()),
        "revoke repaired Off once across concurrent reconciles",
    )
    assert not (await asyncio.to_thread(store.get, grant.grant_id)).active
    assert await asyncio.to_thread(store.off_generation) == generation + 1
    assert local_routes.peek_consent(consent) is None
    assert len(await asyncio.to_thread(store.pending_forgets)) == 1
    assert drained.call_count >= 1
    await finished(controller.reconcile(), "keep an already reconciled Off idempotent")
    assert await asyncio.to_thread(store.off_generation) == generation + 1
    assert len(await asyncio.to_thread(store.pending_forgets)) == 1
    assert stopped.await_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [None, False])
async def test_reconcile_uses_injected_tristate_not_boolean_or_global(store, monkeypatch, switch):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import flag as preview

    def forbidden_global():
        raise AssertionError("an injected controller must not read the global switch")

    monkeypatch.setattr(preview, "local_switch", forbidden_global)
    reader = AsyncMock(return_value=switch)
    controller = controller_mod.LocalController(
        store, instance_lookup=lambda _: None, enabled=lambda: False, switch_state=reader
    )
    monkeypatch.setattr(controller, "_kick_drain", Mock())
    grant = await asyncio.to_thread(store.add, helpers._grant())
    generation = await asyncio.to_thread(store.off_generation)
    await finished(controller.reconcile(), "read the owning host's tri-state switch")
    reader.assert_awaited_once()
    assert (await asyncio.to_thread(store.get, grant.grant_id)).active is (switch is None)
    assert await asyncio.to_thread(store.off_generation) == generation + (switch is False)


@pytest.mark.asyncio
async def test_reconcile_with_the_preview_off_still_retries_a_due_remote_revoke(store, monkeypatch):
    from kiro_crew.connected_computers import controller as controller_mod

    controller = controller_mod.LocalController(
        store,
        instance_lookup=lambda _: None,
        enabled=lambda: False,
        switch_state=AsyncMock(return_value=False),
    )
    kicked = Mock()
    monkeypatch.setattr(controller, "_kick_drain", kicked)
    await asyncio.to_thread(store.install_key)
    await asyncio.to_thread(store.add, helpers._grant())
    await finished(controller.off(), "revoke while the remote is unreachable")
    assert len(await asyncio.to_thread(store.pending_forgets)) == 1
    kicked.reset_mock()

    await finished(controller.reconcile(), "reconcile with the preview off")
    kicked.assert_called_once()


def test_refused_enroll_and_other_gateway_forget_each_emit_a_denied_event(
    tmp_path, monkeypatch, audited
):
    from kiro_crew import gateway_identity
    from kiro_crew.connected_computers import flag as preview
    from kiro_crew.connected_computers import vault_doc
    from kiro_crew.connected_computers.devices import DeviceRegistry

    remote = SecretVault(tmp_path / "remote")
    monkeypatch.setattr(vault_doc, "open_vault", lambda: remote)
    monkeypatch.setattr(gateway_identity, "gateway_id", lambda **_kw: "gateway-b")
    monkeypatch.setattr(cli, "_print", lambda _answer: None)

    monkeypatch.setattr(preview, "preview_on", lambda: False)
    assert cli._remote_enroll() == 2
    assert [(e["operation"], e["outcome"]) for e in audited] == [
        ("connected_computer_enroll", "denied:disabled")
    ]
    audited.clear()

    record = DeviceRegistry(remote).enroll(
        base="laptop", fingerprint="ab" * 32, label="Laptop", owner="owner"
    )
    body = {
        "device_id": record.device_id,
        "fingerprint": record.fingerprint,
        "cgen": record.cgen,
        "gateway_id": "gateway-a",
    }
    monkeypatch.setattr(cli, "_read_stdin", lambda: body)
    assert cli._remote_forget() == 0
    assert DeviceRegistry(remote).get(record.device_id).active, "B's enrollment is kept"
    assert [(e["operation"], e["outcome"]) for e in audited] == [
        ("connected_computer_forget", "denied:other_gateway")
    ]


def test_a_tool_call_refused_on_this_computer_emits_a_denied_event(audited):
    from kiro_crew.connected_computers.mcp_filter import ToolFilter

    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rm"}}
    to_child, _reply = ToolFilter(["rm"]).inbound(json.dumps(call).encode("utf-8") + b"\n")
    assert to_child == b""
    assert [(e["operation"], e["resources"], e["outcome"]) for e in audited] == [
        ("connected_computer_tool_call", "rm", "denied:turned_off")
    ]
