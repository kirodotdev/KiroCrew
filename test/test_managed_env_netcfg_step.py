"""The COMMON_DEPENDENCIES setup step and its undo.

Every case asserts the requests the fake service received.

A fake service with a failure switch; no real service, no write anywhere.
"""

from __future__ import annotations

import json
import stat

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env import netcfg_step as st
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.guard import SandboxGuard

PRIOR = ["corp.example.com", "a.example.org"]


@pytest.fixture
def rig(tmp_path):
    bff = FakeBff()
    bff.netcfg.update(networkAccess="INTEGRATIONS_ONLY", customDomains=list(PRIOR), row=True)
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    client = BffClient(
        lambda: "bff-tok", guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock
    )
    yield bff, client, tmp_path / "sandbox"
    bff.close()


def updates(bff):
    return [r["body"] for r in bff.requests if r["op"] == "UpdateNetworkConfiguration"]


def yes(_text):
    return True


def no(_text):
    return False


def cfg_body(access, domains):
    return {"networkConfig": {"networkAccess": access, "customDomains": list(domains)}}


# ---- apply ----


def test_agree_writes_common_deps_with_the_prior_list_verbatim_and_the_prior_file_comes_first(rig):
    bff, client, state = rig
    seen_at_update = {}
    orig = bff._answer

    def spy(op, body):
        if op == "UpdateNetworkConfiguration":
            p = state / st.PRIOR_FILE
            seen_at_update["exists"] = p.exists()
            seen_at_update["mode"] = stat.S_IMODE(p.stat().st_mode) if p.exists() else None
        return orig(op, body)

    bff._answer = spy
    res = st.ensure_common_dependencies(client, state, yes, instance_id="i-1", now=lambda: 0)
    assert res.code == st.APPLIED and res.ok
    ups = updates(bff)
    assert len(ups) == 1
    assert ups[0]["networkConfig"] == {
        "networkAccess": "COMMON_DEPENDENCIES",
        "customDomains": PRIOR,
    }
    assert seen_at_update == {"exists": True, "mode": 0o600}
    saved = json.loads((state / st.PRIOR_FILE).read_text())
    assert saved["networkAccess"] == "INTEGRATIONS_ONLY" and saved["customDomains"] == PRIOR
    assert saved["instance_id"] == "i-1" and saved["read_at"] == "1970-01-01T00:00:00Z"
    assert bff.netcfg["networkAccess"] == "COMMON_DEPENDENCIES"  # read-back equal
    assert bff.ops().count("GetNetworkConfiguration") == 3  # first read, re-read, read-back


def test_the_dialog_text_names_all_sandboxes_restore_and_settings_network():
    t = st.DIALOG_TEXT
    assert "ALL of your sandboxes" in t and "Kiro Settings > Network" in t and "restore" in t


def test_decline_writes_nothing(rig):
    bff, client, state = rig
    res = st.ensure_common_dependencies(client, state, no)
    assert res.code == st.DECLINED
    assert updates(bff) == [] and not (state / st.PRIOR_FILE).exists()


def test_an_ask_that_returns_something_other_than_true_is_no(rig):
    bff, client, state = rig
    assert st.ensure_common_dependencies(client, state, lambda _t: "yes").code == st.DECLINED
    assert st.ensure_common_dependencies(client, state, lambda _t: 1).code == st.DECLINED
    assert updates(bff) == []


def test_non_interactive_writes_nothing_and_names_the_mode_and_the_denial(rig):
    bff, client, state = rig
    res = st.ensure_common_dependencies(client, state, None)
    assert res.code == st.NOT_INTERACTIVE
    assert "INTEGRATIONS_ONLY" in res.message and "GitHub" in res.message and "PyPI" in res.message
    assert updates(bff) == [] and bff.count("GetNetworkConfiguration") == 1


@pytest.mark.parametrize("mode", ["COMMON_DEPENDENCIES", "OPEN_INTERNET"])
def test_already_wider_is_neither_narrowed_nor_widened(rig, mode):
    bff, client, state = rig
    bff.netcfg.update(networkAccess=mode)
    res = st.ensure_common_dependencies(client, state, yes)
    assert res.code == st.ALREADY and updates(bff) == []
    assert bff.netcfg["networkAccess"] == mode


def test_an_unknown_mode_is_left_alone(rig):
    bff, client, state = rig
    bff.netcfg.update(networkAccess="SOMETHING_NEW")
    assert st.ensure_common_dependencies(client, state, yes).code == st.UNSUPPORTED_MODE
    assert updates(bff) == []


@pytest.mark.parametrize("status", [401, 500])
def test_a_failed_read_writes_nothing(rig, status):
    bff, client, state = rig
    bff.queue("GetNetworkConfiguration", status, {"__type": "x#Boom", "message": "no"})
    res = st.ensure_common_dependencies(client, state, yes)
    assert res.code == st.READ_FAILED and "nothing was changed" in res.message
    assert updates(bff) == [] and not (state / st.PRIOR_FILE).exists()


def test_a_timeout_on_the_read_writes_nothing(rig):
    bff, client, state = rig
    bff.queue("GetNetworkConfiguration", "drop")
    assert st.ensure_common_dependencies(client, state, yes).code == st.READ_FAILED
    assert updates(bff) == []


@pytest.mark.parametrize("status,word", [(400, "nothing was changed"), (500, "may or may not")])
def test_update_failure_keeps_the_prior_file_and_does_not_retry(rig, status, word):
    bff, client, state = rig
    bff.queue("UpdateNetworkConfiguration", status, {"__type": "x#Bad", "message": "m"})
    res = st.ensure_common_dependencies(client, state, yes)
    assert res.code == st.WRITE_FAILED and word in res.message
    assert len(updates(bff)) == 1
    assert (state / st.PRIOR_FILE).exists()


def test_a_lost_update_answer_is_unknown_not_success(rig):
    bff, client, state = rig
    bff.drop_update_after_commit = True
    res = st.ensure_common_dependencies(client, state, yes)
    assert res.code == st.WRITE_FAILED and "may or may not" in res.message
    assert len(updates(bff)) == 1 and (state / st.PRIOR_FILE).exists()


def test_readback_that_differs_stops_and_keeps_the_prior_file(rig):
    bff, client, state = rig
    same_now = cfg_body("INTEGRATIONS_ONLY", PRIOR)
    bff.queue("GetNetworkConfiguration", 200, same_now)
    bff.queue("GetNetworkConfiguration", 200, same_now)
    bff.queue(
        "GetNetworkConfiguration",
        200,
        cfg_body("COMMON_DEPENDENCIES", PRIOR + ["extra.example.com"]),
    )
    res = st.ensure_common_dependencies(client, state, yes)
    assert res.code == st.READBACK_DIFFERS and not res.ok
    assert (state / st.PRIOR_FILE).exists() and len(updates(bff)) == 1


def test_a_change_between_the_first_read_and_the_write_stops_with_nothing_saved(rig):
    bff, client, state = rig

    def ask_then_someone_edits(_t):
        bff.netcfg.update(customDomains=PRIOR + ["added-elsewhere.example.com"])
        return True

    res = st.ensure_common_dependencies(client, state, ask_then_someone_edits)
    assert res.code == st.CHANGED
    assert res.shown["first_read"]["customDomains"] == PRIOR
    assert "added-elsewhere.example.com" in res.shown["second_read"]["customDomains"]
    assert updates(bff) == [] and not (state / st.PRIOR_FILE).exists()


def test_a_mode_change_between_reads_stops(rig):
    bff, client, state = rig

    def ask(_t):
        bff.netcfg.update(networkAccess="OPEN_INTERNET")
        return True

    assert st.ensure_common_dependencies(client, state, ask).code == st.CHANGED
    assert updates(bff) == []


def test_the_setting_the_user_was_shown_must_still_be_current(rig):
    bff, client, state = rig
    shown = st.NetConfig("INTEGRATIONS_ONLY", ("old.example.com",), True)
    res = st.ensure_common_dependencies(client, state, yes, seen=shown)
    assert res.code == st.CHANGED and updates(bff) == []


def test_an_absent_row_is_sent_as_an_empty_list(rig):
    bff, client, state = rig
    bff.netcfg.update(row=False, customDomains=[])
    assert st.ensure_common_dependencies(client, state, yes).code == st.APPLIED
    assert updates(bff)[0]["networkConfig"]["customDomains"] == []


# ---- undo ----


def apply_then(rig):
    bff, client, state = rig
    assert st.ensure_common_dependencies(client, state, yes).code == st.APPLIED
    bff.requests.clear()
    return bff, client, state


def test_undo_with_the_setting_as_set_writes_the_saved_prior_and_removes_the_file(rig):
    bff, client, state = apply_then(rig)
    res = st.restore_network_setting(client, state)
    assert res.code == st.RESTORED
    ups = updates(bff)
    assert len(ups) == 1
    assert ups[0]["networkConfig"] == {"networkAccess": "INTEGRATIONS_ONLY", "customDomains": PRIOR}
    assert (
        bff.netcfg["networkAccess"] == "INTEGRATIONS_ONLY" and bff.netcfg["customDomains"] == PRIOR
    )
    assert not (state / st.PRIOR_FILE).exists()


def test_undo_after_the_user_changed_it_elsewhere_writes_nothing_and_shows_both(rig):
    bff, client, state = apply_then(rig)
    bff.netcfg.update(customDomains=PRIOR + ["mine.example.com"])
    res = st.restore_network_setting(client, state)
    assert res.code == st.NOT_OURS
    assert updates(bff) == []
    assert "mine.example.com" in res.shown["current"]["customDomains"]
    assert res.shown["saved_previous"]["customDomains"] == PRIOR
    assert (state / st.PRIOR_FILE).exists()


def test_undo_when_already_back_to_the_prior_writes_nothing(rig):
    bff, client, state = apply_then(rig)
    bff.netcfg.update(networkAccess="INTEGRATIONS_ONLY")  # restored in Kiro Settings > Network
    assert st.restore_network_setting(client, state).code == st.RESTORED
    assert updates(bff) == [] and not (state / st.PRIOR_FILE).exists()


def test_undo_with_no_prior_file_writes_nothing_and_names_kiro_settings_network(rig):
    bff, client, state = rig
    res = st.restore_network_setting(client, state)
    assert res.code == st.NO_PRIOR
    assert "Kiro Settings > Network" in res.message and "this machine" in res.message
    assert bff.requests == []


def test_undo_with_a_corrupt_prior_file_writes_nothing(rig):
    bff, client, state = rig
    state.mkdir(parents=True)
    (state / st.PRIOR_FILE).write_text("{not json")
    assert st.restore_network_setting(client, state).code == st.NO_PRIOR
    assert bff.requests == []


def test_undo_read_failure_writes_nothing_and_keeps_the_file(rig):
    bff, client, state = apply_then(rig)
    bff.queue("GetNetworkConfiguration", 500, {"__type": "x#Boom", "message": "no"})
    assert st.restore_network_setting(client, state).code == st.READ_FAILED
    assert updates(bff) == [] and (state / st.PRIOR_FILE).exists()


def test_undo_failure_keeps_the_file(rig):
    bff, client, state = apply_then(rig)
    bff.queue("UpdateNetworkConfiguration", 500, {"__type": "x#Boom", "message": "no"})
    res = st.restore_network_setting(client, state)
    assert res.code == st.WRITE_FAILED and (state / st.PRIOR_FILE).exists()
    assert len(updates(bff)) == 1


def test_undo_readback_that_differs_keeps_the_file(rig):
    bff, client, state = apply_then(rig)
    now = cfg_body("COMMON_DEPENDENCIES", PRIOR)
    bff.queue("GetNetworkConfiguration", 200, now)
    bff.queue(
        "GetNetworkConfiguration", 200, cfg_body("COMMON_DEPENDENCIES", PRIOR)
    )  # read-back: not restored
    res = st.restore_network_setting(client, state)
    assert res.code == st.READBACK_DIFFERS and (state / st.PRIOR_FILE).exists()


# ---- the routes and the CLI ----


import asyncio  # noqa: E402

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from test_managed_env_engine import FakeRegistry  # noqa: E402

from kiro_crew.managed_env.engine import SandboxLaunchEngine  # noqa: E402
from kiro_crew.managed_env.routes import SandboxRoutes, register_sandbox_routes  # noqa: E402


def rcall(routes, method, path, js=None):
    async def go():
        app = web.Application()
        register_sandbox_routes(app, routes)
        async with TestClient(TestServer(app)) as c:
            r = await c.request(method, path, json=js)
            return r.status, await r.json()

    return asyncio.run(go())


@pytest.fixture
def step_env(rig, tmp_path, monkeypatch):
    """Flag on, owner allowed; the routes use the rig's client and a state dir of their own."""
    from kiro_crew.dashboard.handlers import _shared

    bff, client, _ = rig
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: FakeRegistry())
    routes = SandboxRoutes(lambda: engine, run_in_thread=False)

    async def allow(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", allow)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    routes._step_state_dir = lambda: tmp_path / "step-state"
    return routes, bff, tmp_path / "step-state"


SEEN = {"networkAccess": "INTEGRATIONS_ONLY", "customDomains": PRIOR}


def test_route_status_returns_the_dialog_and_writes_nothing(step_env):
    routes, bff, state = step_env
    status, body = rcall(routes, "GET", "/api/managed-environments/network-step/status")
    assert status == 200 and body["needsAgreement"] is True and body["canRestore"] is False
    assert "ALL of your sandboxes" in body["dialog"] and updates(bff) == []


def test_route_apply_with_agree_true_and_the_shown_setting_writes_once(step_env):
    routes, bff, state = step_env
    status, body = rcall(
        routes,
        "POST",
        "/api/managed-environments/network-step/apply",
        {"agree": True, "seen": SEEN},
    )
    assert status == 200 and body["code"] == st.APPLIED
    assert updates(bff)[0]["networkConfig"] == {
        "networkAccess": "COMMON_DEPENDENCIES",
        "customDomains": PRIOR,
    }
    assert (
        rcall(routes, "GET", "/api/managed-environments/network-step/status")[1]["canRestore"]
        is True
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"seen": SEEN},
        {"agree": False, "seen": SEEN},
        {"agree": "true", "seen": SEEN},
        {"agree": 1, "seen": SEEN},
    ],
)
def test_route_apply_without_a_literal_true_agree_is_no(step_env, payload):
    routes, bff, state = step_env
    _, body = rcall(routes, "POST", "/api/managed-environments/network-step/apply", payload)
    assert body["code"] in (st.DECLINED, st.NOT_INTERACTIVE) and updates(bff) == []


def test_route_apply_without_the_shown_setting_never_writes(step_env):
    routes, bff, state = step_env
    _, body = rcall(routes, "POST", "/api/managed-environments/network-step/apply", {"agree": True})
    assert body["code"] == st.NOT_INTERACTIVE and updates(bff) == []


def test_route_apply_when_the_setting_moved_after_the_dialog_shows_both(step_env):
    routes, bff, state = step_env
    bff.netcfg.update(customDomains=PRIOR + ["x.example.com"])
    _, body = rcall(
        routes,
        "POST",
        "/api/managed-environments/network-step/apply",
        {"agree": True, "seen": SEEN},
    )
    assert body["code"] == st.CHANGED and updates(bff) == []
    assert body["shown"]["shown_to_you"]["customDomains"] == PRIOR


def test_route_restore_after_apply(step_env):
    routes, bff, state = step_env
    rcall(
        routes,
        "POST",
        "/api/managed-environments/network-step/apply",
        {"agree": True, "seen": SEEN},
    )
    _, body = rcall(routes, "POST", "/api/managed-environments/network-step/restore")
    assert body["code"] == st.RESTORED and bff.netcfg["networkAccess"] == "INTEGRATIONS_ONLY"


def test_route_a_write_is_never_a_get_and_status_is_never_a_post(step_env):
    routes, bff, state = step_env
    assert rcall(routes, "GET", "/api/managed-environments/network-step/apply")[0] == 405
    assert rcall(routes, "GET", "/api/managed-environments/network-step/restore")[0] == 405
    assert rcall(routes, "POST", "/api/managed-environments/network-step/status")[0] == 405
    assert bff.requests == []


def test_route_flag_off_is_404_and_calls_nothing(step_env, monkeypatch):
    routes, bff, state = step_env
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    for method, op in (("GET", "status"), ("POST", "apply"), ("POST", "restore")):
        assert (
            rcall(
                routes,
                method,
                f"/api/managed-environments/network-step/{op}",
                {"agree": True, "seen": SEEN},
            )[0]
            == 404
        )
    assert bff.requests == []
