"""A company sign-in that can use several Kiro profiles asks which one to use.

Discovery reads every page of every control plane; the poll that redeems the token
answers ``choose_profile`` and holds it; a poll naming one of the offered ARNs
stores it. The network is faked with the scripted session the service tests use.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from test_kas_login_api import _FakeRequest
from test_kas_login_service import (
    _EU_CPS,
    _GAMMA_CPS,
    _US_CPS,
    _begin_oidc,
    _FailingResp,
    _FakeResp,
    _FakeSession,
    _page,
    _service,
)

from kiro_crew.auth import service as auth_service
from kiro_crew.auth import store as auth_store
from kiro_crew.auth.login import builder_id, control_plane
from kiro_crew.dashboard.handlers import kas_login

_A = "arn:aws:codewhisperer:us-east-1:111122223333:profile/AAAAAAAAAAAA"
_B = "arn:aws:codewhisperer:us-east-1:111122223333:profile/BBBBBBBBBBBB"
_C = "arn:aws:codewhisperer:eu-central-1:111122223333:profile/CCCCCCCCCCCC"
_Z = "arn:aws:codewhisperer:us-east-1:111122223333:profile/ZZZZZZZZZZZZ"
_START = "https://acme.awsapps.com/start"


def _token(access="at-held", expires_in=3600) -> _FakeResp:
    return _FakeResp(200, {"accessToken": access, "refreshToken": "rt", "expiresIn": expires_in})


def _p(arn: str, name: str) -> dict:
    return {"arn": arn, "profileName": name}


def _three_profiles() -> list[_FakeResp]:
    """Host 1 lists A then, on page 2, B (and A again); host 2 lists C."""
    return [
        _page([_p(_A, "Alpha")], next_token="t2"),
        _page([_p(_B, "Beta"), _p(_A, "Alpha")]),
        _page([_p(_C, "Gamma")]),
    ]


async def _waiting(tmp_path, monkeypatch, *, token=None, listings=None):
    """A service whose IdC login has just answered ``choose_profile``."""
    service, session = _service(
        tmp_path, responses=[token or _token(), *(listings or _three_profiles())]
    )
    begin = await _begin_oidc(service, monkeypatch, "idc", start_url=_START)
    first = await service.poll_device(begin["login_id"])
    return service, session, begin["login_id"], first


def _store_has(tmp_path) -> str | None:
    saved = auth_store.TokenStore(tmp_path).resolve()
    return saved.profile_arn if saved else None


# --- discovery -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_hosts_answer_choose_profile_with_every_profile_once_then_a_pick_stores_it(
    tmp_path, monkeypatch
):
    service, session, login_id, first = await _waiting(tmp_path, monkeypatch)
    assert first == {
        "status": "choose_profile",
        "profiles": [
            {"arn": _A, "name": "Alpha", "region": "us-east-1"},
            {"arn": _B, "name": "Beta", "region": "us-east-1"},
            {"arn": _C, "name": "Gamma", "region": "eu-central-1"},
        ],
        "selected": _A,
    }
    assert "at-held" not in json.dumps(first)
    assert [u for u, _ in session.calls if "kiro.dev" in u] == [_US_CPS, _US_CPS, _EU_CPS]
    assert _store_has(tmp_path) is None
    assert await service.poll_device(login_id, _C) == {
        "status": "authorized",
        "provider": "Enterprise",
    }
    assert _store_has(tmp_path) == _C
    assert login_id not in service._pending


@pytest.mark.parametrize(
    "second_page",
    [_FakeResp(500, {"message": "boom"}), _FailingResp(asyncio.TimeoutError())],
    ids=["control-plane-error", "timeout"],
)
@pytest.mark.asyncio
async def test_a_failed_second_page_keeps_the_first_pages_profile(second_page):
    session = _FakeSession([_page([_p(_A, "Alpha")], next_token="t2"), second_page, _page([])])
    listing = await control_plane.list_available_profiles("at", region="us-east-1", session=session)
    assert [p.arn for p in listing.profiles] == [_A]
    assert [u for u, _ in session.calls] == [_US_CPS, _US_CPS, _EU_CPS]


@pytest.mark.asyncio
async def test_a_later_hosts_transport_error_keeps_the_listing():
    session = _FakeSession(
        [_page([_p(_A, "Alpha")]), _FailingResp(aiohttp.ClientConnectionError("reset"))]
    )
    listing = await control_plane.list_available_profiles("at", region="us-east-1", session=session)
    assert [p.arn for p in listing.profiles] == [_A]
    assert [u for u, _ in session.calls] == [_US_CPS, _EU_CPS]


@pytest.mark.asyncio
async def test_a_refusing_first_prod_host_and_a_listing_next_host_is_that_profile():
    session = _FakeSession([_FakeResp(403, {"message": "no"}), _page([_p(_C, "Gamma")])])
    listing = await control_plane.list_available_profiles("at", region="us-east-1", session=session)
    assert [p.arn for p in listing.profiles] == [_C]


@pytest.mark.asyncio
async def test_a_refusing_viewer_host_and_a_listing_prod_host_is_that_profile():
    session = _FakeSession([_FakeResp(403, {"message": "no"}), _page([_p(_A, "Alpha")]), _page([])])
    listing = await control_plane.list_available_profiles(
        "at", region="us-east-1", session=session, viewer_cps_url=_GAMMA_CPS
    )
    assert [p.arn for p in listing.profiles] == [_A]


@pytest.mark.asyncio
async def test_a_viewer_listing_is_final_and_no_prod_host_is_asked():
    session = _FakeSession([_page([_p(_B, "Viewer")]), _page([_p(_A, "Prod")])])
    listing = await control_plane.list_available_profiles(
        "at", region="us-east-1", session=session, viewer_cps_url=_GAMMA_CPS
    )
    assert [p.arn for p in listing.profiles] == [_B]
    assert [u for u, _ in session.calls] == [_GAMMA_CPS]


# --- the wait for a pick -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unlisted_pick_is_refused_and_the_login_keeps_waiting(tmp_path, monkeypatch):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch)
    with pytest.raises(auth_service.UnknownProfileError):
        await service.poll_device(login_id, _Z)
    audited: list[tuple] = []

    async def _audit(request, *args, **kwargs):
        audited.append(args)

    monkeypatch.setattr(kas_login, "_audit", _audit)
    resp = await kas_login.api_kas_login_poll(
        _FakeRequest(service, {"login_id": login_id, "profile_arn": _Z})
    )
    assert resp.status == 400
    assert json.loads(resp.text) == {
        "error": "That profile is not one this sign-in offered.",
        "code": "unknown_profile",
    }
    assert audited == []
    assert _store_has(tmp_path) is None
    assert (await service.poll_device(login_id))["status"] == "choose_profile"
    resp = await kas_login.api_kas_login_poll(
        _FakeRequest(service, {"login_id": login_id, "profile_arn": _B})
    )
    assert resp.status == 200
    assert json.loads(resp.text)["status"] == "authorized"
    assert _store_has(tmp_path) == _B


@pytest.mark.asyncio
async def test_a_poll_without_a_pick_repeats_the_choice_and_never_polls_the_code(
    tmp_path, monkeypatch
):
    service, _, login_id, first = await _waiting(tmp_path, monkeypatch)

    async def _never(*_a, **_k):
        raise AssertionError("a redeemed device code was polled again")

    monkeypatch.setattr(builder_id, "poll_token_once", _never)
    assert await service.poll_device(login_id) == first
    assert await service.poll_device(login_id, "") == first


@pytest.mark.asyncio
async def test_a_cancel_during_the_wait_wins(tmp_path, monkeypatch):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch)
    await service.cancel(login_id)
    with pytest.raises(auth_service.UnknownLoginError):
        await service.poll_device(login_id, _A)
    resp = await kas_login.api_kas_login_poll(
        _FakeRequest(service, {"login_id": login_id, "profile_arn": _A})
    )
    assert resp.status == 404
    assert _store_has(tmp_path) is None


@pytest.mark.asyncio
async def test_an_in_process_sign_out_during_the_wait_wins(tmp_path, monkeypatch):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch)
    await service.logout("identity_center")
    with pytest.raises(auth_service.UnknownLoginError):
        await service.poll_device(login_id, _A)
    assert _store_has(tmp_path) is None


@pytest.mark.asyncio
async def test_a_sign_out_in_another_process_during_the_wait_wins(tmp_path, monkeypatch):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch)
    # What `kirocrew kas logout` in a shell leaves behind.
    auth_store.TokenStore(tmp_path).bump_signout_generation()
    resp = await kas_login.api_kas_login_poll(
        _FakeRequest(service, {"login_id": login_id, "profile_arn": _A})
    )
    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "signed_out_during_login"
    assert _store_has(tmp_path) is None


@pytest.mark.asyncio
async def test_selected_is_the_first_profile_without_a_stored_one(tmp_path, monkeypatch):
    _, _, _, first = await _waiting(tmp_path, monkeypatch)
    assert first["selected"] == _A


@pytest.mark.asyncio
async def test_selected_is_the_stored_profile_when_it_is_offered(tmp_path, monkeypatch):
    auth_store.TokenStore(tmp_path).save(
        auth_store.KasToken(
            access_token="at-old",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="Enterprise",
            identity="identity_center",
            profile_arn=_B,
        )
    )
    _, _, _, first = await _waiting(tmp_path, monkeypatch)
    assert first["selected"] == _B


@pytest.mark.asyncio
async def test_an_unreadable_store_reads_as_no_stored_profile(tmp_path, monkeypatch):
    service, session = _service(tmp_path, responses=[_token(), *_three_profiles()])
    begin = await _begin_oidc(service, monkeypatch, "idc", start_url=_START)

    def _broken(identity):
        raise auth_store.TokenStoreError("vault unreadable")

    monkeypatch.setattr(service._store, "load", _broken)
    first = await service.poll_device(begin["login_id"])
    assert first["status"] == "choose_profile"
    assert first["selected"] == _A
    assert begin["login_id"] in service._pending


@pytest.mark.asyncio
async def test_the_wait_outlives_the_device_code_and_ends_at_its_own_deadline(
    tmp_path, monkeypatch
):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch)
    pending = service._pending[login_id]
    held = pending.held_token
    assert held is not None
    assert pending.choice_deadline is not None
    soon = datetime.now(timezone.utc) + timedelta(minutes=9)
    assert soon < pending.choice_deadline <= held.expires_at
    # Approved just before the device code lapsed: the code's deadline is behind us,
    # the wait's is not.
    pending.auth.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert (await service.poll_device(login_id))["status"] == "choose_profile"
    assert await service.poll_device(login_id, _B) == {
        "status": "authorized",
        "provider": "Enterprise",
    }
    assert _store_has(tmp_path) == _B


@pytest.mark.asyncio
async def test_the_wait_is_capped_by_the_held_tokens_expiry(tmp_path, monkeypatch):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch, token=_token(expires_in=60))
    pending = service._pending[login_id]
    assert pending.held_token is not None
    assert pending.choice_deadline == pending.held_token.expires_at


@pytest.mark.asyncio
async def test_past_the_deadline_the_poll_is_expired_and_nothing_is_stored(tmp_path, monkeypatch):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch)
    service._pending[login_id].choice_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await service.poll_device(login_id, _A) == {"status": "expired"}
    assert login_id not in service._pending
    assert _store_has(tmp_path) is None


@pytest.mark.asyncio
async def test_a_held_token_never_reaches_a_repr(tmp_path, monkeypatch):
    service, _, login_id, _ = await _waiting(tmp_path, monkeypatch)
    assert "at-held" not in repr(service._pending[login_id])


@pytest.mark.asyncio
async def test_one_profile_ignores_an_unrelated_pick_and_stores_the_listed_one(
    tmp_path, monkeypatch
):
    service, _ = _service(tmp_path, responses=[_token(), _page([_p(_A, "Alpha")]), _page([])])
    begin = await _begin_oidc(service, monkeypatch, "idc", start_url=_START)
    assert await service.poll_device(begin["login_id"], _Z) == {
        "status": "authorized",
        "provider": "Enterprise",
    }
    assert _store_has(tmp_path) == _A
