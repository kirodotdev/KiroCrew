"""``GET /api/token/local`` never mints for an app backend, and the owner gate refuses app claims.

An app backend (a process the gateway spawned, or one of its descendants) holds the
local secret like any host process, but it is arbitrary code the owner enabled. A token
minted for it would carry no ``app`` claim and so pass ``is_owner_dashboard_request``.
These tests call the handler and the gate directly with a constructed caller: a patched
peer pid, a patched process table and parent chain, and a stand-in for the signing
function. No process, listener or real token is involved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _req

import kiro_crew.apps.backend as bmod
from kiro_crew import member_memory_auth as auth
from kiro_crew.apps.backend import AppProcess
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

_PEER_PID = 900
_ROOT_PID = 700
_OWNER = "owner-1"
_REFUSED = "member_owner_token_refused"


@pytest.fixture
def signed(monkeypatch):
    """Replace the signing function with a recorder; a refusal leaves it uncalled."""
    calls: list[dict] = []

    def _generate(owner, ttl_seconds=0, **kwargs):
        calls.append({"owner": owner, **kwargs})
        return "stand-in-value"

    monkeypatch.setattr(core_mod, "generate_token", _generate)
    return calls


@pytest.fixture
def caller(monkeypatch):
    """A kernel-identified peer at ``_PEER_PID``; the host-process verdict is a parameter."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
    monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: _PEER_PID)
    monkeypatch.setattr(
        auth.platform_compat,
        "get_process_start_id",
        lambda pid: "synthetic-start" if pid == _PEER_PID else None,
    )

    def _set_host(verdict: bool) -> None:
        monkeypatch.setattr(auth, "_verified_host_process", lambda _pid: verdict)

    return _set_host


def _backend_registry(monkeypatch, name: str, chain: dict[int, int], root: int = _ROOT_PID) -> None:
    """One live gateway-spawned backend named ``name`` rooted at ``root``."""
    live = AppProcess(
        app_name=name,
        pid=root,
        proc=SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(bmod, "_processes", {name: live})
    monkeypatch.setattr(bmod.platform_compat, "get_ppid", lambda pid: chain.get(pid, 1))


async def _mint(sel_row_holder):
    request = _req(
        app={"local_secret": "right", "state": SimpleNamespace(owner_id=_OWNER)},
        headers={"X-Local-Secret": "right"},
    )
    response = await core_mod.api_token_local(request)
    return response, sel_row_holder.log_api_access.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("host_leg", [True, False])
async def test_a_spawned_backend_is_refused_and_no_token_is_signed(
    monkeypatch, fake_sel, caller, signed, host_leg
) -> None:
    caller(host_leg)
    _backend_registry(monkeypatch, "demo-app", {}, root=_PEER_PID)
    response, row = await _mint(fake_sel)
    assert response.status == 403
    assert json.loads(response.body)["code"] == _REFUSED
    assert signed == []
    assert row["outcome"] == "denied"
    assert row["resources"] == "app-backend-refused app=demo-app"


@pytest.mark.asyncio
@pytest.mark.parametrize("host_leg", [True, False])
async def test_a_descendant_of_a_spawned_backend_is_refused_like_the_backend(
    monkeypatch, fake_sel, caller, signed, host_leg
) -> None:
    caller(host_leg)
    # 900 -> 800 -> 700 (the backend root): two levels below the root.
    _backend_registry(monkeypatch, "demo-app", {_PEER_PID: 800, 800: _ROOT_PID})
    response, row = await _mint(fake_sel)
    assert response.status == 403
    assert json.loads(response.body)["code"] == _REFUSED
    assert signed == []
    assert row["resources"] == "app-backend-refused app=demo-app"


@pytest.mark.asyncio
async def test_a_process_beyond_the_ancestry_limit_is_not_admitted_by_the_backend_leg(
    monkeypatch, fake_sel, caller, signed
) -> None:
    caller(False)
    # The root sits nine parents above the peer; the walk stops at eight.
    path = [_PEER_PID, *range(801, 809), _ROOT_PID]
    _backend_registry(monkeypatch, "demo-app", dict(zip(path, path[1:])))
    assert bmod.spawned_backend_owner(_PEER_PID) is None
    response, _row = await _mint(fake_sel)
    assert response.status == 403
    assert signed == []


@pytest.mark.asyncio
async def test_a_descendant_reparented_to_pid_1_is_refused_because_the_host_leg_says_no(
    monkeypatch, fake_sel, caller, signed
) -> None:
    """A double-forked descendant leaves the ancestry walk, so only the host leg refuses it."""
    caller(False)
    _backend_registry(monkeypatch, "demo-app", {_PEER_PID: 800, 800: 1})
    assert bmod.spawned_backend_owner(_PEER_PID) is None
    response, row = await _mint(fake_sel)
    assert response.status == 403
    assert signed == []
    assert row["resources"] == "unverified-owner-process"


@pytest.mark.asyncio
async def test_a_host_process_still_gets_an_owner_token_without_an_app_claim(
    monkeypatch, fake_sel, caller, signed
) -> None:
    caller(True)
    monkeypatch.setattr(bmod, "_processes", {})
    response, row = await _mint(fake_sel)
    assert response.status == 200
    assert set(json.loads(response.body)) == {"token", "expires_in"}
    assert [c["owner"] for c in signed] == [_OWNER]
    assert "app" not in signed[0]
    assert row["outcome"] == "success"
    assert row["resources"] == "token-issued"


@pytest.mark.asyncio
async def test_a_process_admitted_by_neither_leg_is_refused(
    monkeypatch, fake_sel, caller, signed
) -> None:
    caller(False)
    monkeypatch.setattr(bmod, "_processes", {})
    response, row = await _mint(fake_sel)
    assert response.status == 403
    assert json.loads(response.body)["code"] == _REFUSED
    assert signed == []
    assert row["resources"] == "unverified-owner-process"


@pytest.mark.asyncio
async def test_a_backend_whose_registry_name_is_empty_is_refused(
    monkeypatch, fake_sel, caller, signed
) -> None:
    """The backend leg admits the caller but names no app: refuse, do not fall to the host leg."""
    caller(True)
    _backend_registry(monkeypatch, "", {_PEER_PID: _ROOT_PID})
    assert bmod.spawned_backend_owner(_PEER_PID) == ""
    response, row = await _mint(fake_sel)
    assert response.status == 403
    assert json.loads(response.body)["code"] == _REFUSED
    assert signed == []
    assert row["outcome"] == "denied"


def _gate_request(user: str, app_claim: str, owner_id: str):
    return _req(app={"state": SimpleNamespace(owner_id=owner_id)}, user=user, app_token=app_claim)


@pytest.mark.parametrize(
    ("user", "owner_id"),
    [(_OWNER, _OWNER), ("local-app", ""), ("local-startup", "")],
)
@pytest.mark.parametrize("app_claim", ["demo-app", "x", " "])
def test_the_owner_gate_refuses_any_non_empty_app_claim(user, owner_id, app_claim) -> None:
    assert is_owner_dashboard_request(_gate_request(user, app_claim, owner_id)) is False


@pytest.mark.parametrize(("user", "owner_id"), [(_OWNER, _OWNER), ("local-app", "")])
def test_the_owner_gate_admits_the_same_caller_with_an_empty_app_claim(user, owner_id) -> None:
    assert is_owner_dashboard_request(_gate_request(user, "", owner_id)) is True
