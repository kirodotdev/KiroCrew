"""``/api/token/local`` leg 2: an app granted ``dashboard_proxy`` mints with its derived value.

Leg 1 is unchanged: the real local secret from a verified owner process mints an owner
token. Leg 2 admits ONLY a gateway-spawned backend of app X, whose process start id
still matches the spawn record, holding the grant and the owner-enable marker, presenting
``D(X)``. The token carries ``via_app=X`` (never the ``app`` claim). Every leg-2 refusal
leaves an SEL row; the derived value and the token never reach a log. Secret-shaped values
here are described in words, not spelled out.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _req

from kiro_crew import member_memory_auth as auth
from kiro_crew.apps import dashboard_proxy
from kiro_crew.dashboard.handlers import core as core_mod

_REAL = "the-real-local-secret-in-words"
_PEER = 4242
_APP = "tunnel-like"

#: Leg two is refused on win32 (``unverified-platform``, ``test_win32_never_takes_leg_two``),
#: so a test of what leg two admits or refuses by reason runs where it can be taken.
_LEG_TWO_HOST = pytest.mark.skipif(
    sys.platform == "win32", reason="leg two is never taken on win32"
)


@pytest.fixture
def minted(monkeypatch) -> dict:
    seen: dict = {}

    def _generate(owner, ttl_seconds=0, extra=None):
        seen.update({"owner": owner, "extra": extra})
        return "issued-value"

    monkeypatch.setattr(core_mod, "generate_token", _generate)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
    return seen


@pytest.fixture
def world(monkeypatch):
    """One installed, granted app ``tunnel-like`` whose backend owns the calling pid."""
    state = {"owner_app": _APP, "host": False, "granted": True, "identity": "pkg-one-v1"}
    monkeypatch.setattr(auth, "_request_peer_pid", lambda _r: _PEER)
    monkeypatch.setattr(
        auth.platform_compat, "get_process_start_id", lambda pid: "start" if pid == _PEER else None
    )
    monkeypatch.setattr(auth, "_verified_host_process", lambda _pid: state["host"])
    monkeypatch.setattr(
        auth,
        "_gateway_spawned_app_backend",
        lambda pid, **_kw: state["owner_app"] if pid == _PEER else None,
    )
    dashboard_proxy.set_gateway_secret(_REAL)
    monkeypatch.setattr(dashboard_proxy, "install_identity", lambda _n: state["identity"])
    monkeypatch.setattr(
        dashboard_proxy, "_marker_state", lambda _n: (state["granted"], state["granted"])
    )
    monkeypatch.setattr(dashboard_proxy, "_stored_identity", lambda _n: "pkg-one-v1")
    monkeypatch.setattr(dashboard_proxy, "_stored_grant_id", lambda _n: "grant-one")
    return state


async def _mint(value: str):
    return await core_mod.api_token_local(
        _req(
            app={"local_secret": _REAL, "state": SimpleNamespace(owner_id="owner-1")},
            headers={"X-Local-Secret": value},
        )
    )


@pytest.mark.asyncio
async def test_the_real_secret_from_a_host_process_still_mints(world, minted, fake_sel):
    world.update(owner_app=None, host=True)
    resp = await _mint(_REAL)
    assert resp.status == 200
    assert "via_app" not in (minted["extra"] or {})


@_LEG_TWO_HOST
@pytest.mark.asyncio
async def test_leg_two_mints_for_the_apps_own_backend(world, minted, fake_sel):
    resp = await _mint(dashboard_proxy.stand_in_value(_APP))
    assert resp.status == 200
    claim = dashboard_proxy.grant_claim(_APP)
    assert claim, "the token names the grant it was minted under"
    assert minted["extra"] == {"owner_mint": "1", "via_app": _APP, "via_grant": claim}
    row = fake_sel.log_api_access.call_args.kwargs
    assert row["resources"] == f"token-issued app={_APP} basis=spawned-app via=delegated"
    for call in fake_sel.log_api_access.call_args_list:
        assert dashboard_proxy.stand_in_value(_APP) not in json.dumps(call.kwargs, default=str)


async def _assert_refused(value: str, fake_sel, reason: str, app: str = _APP):
    resp = await _mint(value)
    assert resp.status == 403
    row = fake_sel.log_api_access.call_args.kwargs
    assert row["operation"] == "token.local"
    assert row["outcome"] == "denied"
    assert row["resources"].startswith(f"token.local denied {reason} app={app} ") or (
        f"denied {reason}" in row["resources"]
    )
    assert "via=delegated" in row["resources"]


@_LEG_TWO_HOST
@pytest.mark.asyncio
async def test_a_value_derived_for_another_app_is_refused(world, minted, fake_sel):
    other = dashboard_proxy.stand_in_value("another-app")
    await _assert_refused(other, fake_sel, "wrong-value")


@_LEG_TWO_HOST
@pytest.mark.asyncio
async def test_a_host_process_with_the_derived_value_is_refused(world, minted, fake_sel):
    value = dashboard_proxy.stand_in_value(_APP)
    world.update(owner_app=None, host=True)
    await _assert_refused(value, fake_sel, "not-spawned-app", app="none")


@_LEG_TWO_HOST
@pytest.mark.asyncio
async def test_a_value_from_another_boot_is_refused(world, minted, fake_sel, monkeypatch):
    value = dashboard_proxy.stand_in_value(_APP)
    monkeypatch.setattr("kiro_crew.dashboard.boot_id.current_boot_id", lambda: "a-later-boot")
    await _assert_refused(value, fake_sel, "wrong-value")


@_LEG_TWO_HOST
@pytest.mark.asyncio
async def test_a_value_from_before_a_reinstall_is_refused(world, minted, fake_sel):
    value = dashboard_proxy.stand_in_value(_APP)
    world["identity"] = "pkg-two-v1"
    await _assert_refused(value, fake_sel, "not-granted")


@_LEG_TWO_HOST
@pytest.mark.asyncio
async def test_a_value_after_the_owner_disables_the_app_is_refused(world, minted, fake_sel):
    value = dashboard_proxy.stand_in_value(_APP)
    world["granted"] = False
    await _assert_refused(value, fake_sel, "not-granted")


def test_a_recycled_pid_does_not_pass_as_the_spawn_record(monkeypatch):
    """The registry match checks the pid AND its start id against the spawn record."""
    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps.backend import AppProcess

    live = AppProcess(
        app_name=_APP,
        pid=11,
        proc=SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        pid_start_time="recorded-start",
    )
    monkeypatch.setattr(bmod, "_processes", {_APP: live})
    monkeypatch.setattr(bmod.platform_compat, "process_start_time", lambda pid: "recycled-start")
    assert bmod.spawned_backend_owner(11, verify_start=True) is None
    monkeypatch.setattr(bmod.platform_compat, "process_start_time", lambda pid: "recorded-start")
    assert bmod.spawned_backend_owner(11, verify_start=True) == _APP


@pytest.mark.asyncio
async def test_an_empty_or_wrong_value_stays_invalid_secret(world, minted, fake_sel):
    for value in ("", "a-wrong-value"):
        resp = await _mint(value)
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "invalid_secret"
        row = fake_sel.log_api_access.call_args.kwargs
        assert row["resources"] == "invalid-secret"


@pytest.mark.asyncio
async def test_win32_never_takes_leg_two(world, minted, fake_sel, monkeypatch):
    value = dashboard_proxy.stand_in_value(_APP)
    monkeypatch.setattr(auth, "_host_process_basis", lambda: "win32-unverified")
    resp = await _mint(value)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_the_real_secret_from_a_spawned_app_is_refused_on_a_sandboxed_backend(
    world, minted, fake_sel, monkeypatch
):
    """A backend that somehow holds the real secret never mints with it where a sandbox is."""
    monkeypatch.setattr(dashboard_proxy, "sandboxed_backend", lambda: True)
    resp = await _mint(_REAL)
    assert resp.status == 403
    assert "owner" not in minted
    row = fake_sel.log_api_access.call_args.kwargs
    assert row["operation"] == "token.local"
    assert row["outcome"] == "denied"
    assert row["resources"] == f"denied real-secret-from-app app={_APP} basis=spawned-app"


@pytest.mark.asyncio
async def test_the_real_secret_from_a_spawned_app_still_mints_with_no_sandbox(
    world, minted, fake_sel, monkeypatch
):
    monkeypatch.setattr(dashboard_proxy, "sandboxed_backend", lambda: False)
    assert (await _mint(_REAL)).status == 200
