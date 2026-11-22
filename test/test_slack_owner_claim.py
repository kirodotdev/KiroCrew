"""Slack owner claim: the window, its filters, the candidates, confirm and the routes.

Design: docs/request-for-change/rfc-slack-owner-claim.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.messaging as msg
from kiro_crew.slack import owner_claim as oc
from kiro_crew.slack import reconnect as rc

TEAM = "T0TEAM"
OWNER = "U0OWNER"
BOT = "xoxb-test-bot"
APP = "xapp-test-app"
#: Every wait here awaits a signal; the ceiling only turns a hang into a named failure.
_CEILING = 60.0


class _Socket:
    def __init__(self, *, fail: bool = False) -> None:
        self.socket_mode_request_listeners: list[Any] = []
        self.fail = fail
        self.connected = False
        self.closed = False
        self.acks: list[str] = []

    async def connect(self) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def send_socket_mode_response(self, resp: Any) -> None:
        self.acks.append(resp.envelope_id)


class _Web:
    """auth.test and users.info only; any other Web API call fails the test."""

    def __init__(self, auth: dict[str, Any], users: dict[str, Any]) -> None:
        self.auth = auth
        self.users = users
        self.user_calls: list[str] = []

    async def auth_test(self) -> dict[str, Any]:
        return self.auth

    async def users_info(self, *, user: str) -> dict[str, Any]:
        self.user_calls.append(user)
        found = self.users.get(user)
        if found is None:
            raise RuntimeError("user_not_found")
        return {"user": found}

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"claim mode called Slack {name}")


def _user(uid: str, **extra: Any) -> dict[str, Any]:
    base = {
        "id": uid,
        "name": f"h-{uid.lower()}",
        "team_id": TEAM,
        "profile": {"display_name": f"Name {uid}", "image_72": "https://a.example/x.png"},
    }
    base.update(extra)
    return base


class _Claim(oc.OwnerClaim):
    def __init__(self, orch: Any, web_: _Web, sock: _Socket, **kw: Any) -> None:
        super().__init__(orch, **kw)
        self.web_ = web_
        self.sock = sock
        self.enterprise_ok = True
        self.transport_ok = True

    def _web_client(self, bot: str) -> Any:
        return self.web_

    def _socket_client(self, app: str, web_: Any) -> Any:
        return self.sock

    async def _enterprise_permits(self, bot: str) -> bool:
        return self.enterprise_ok

    async def _transport_permitted(self) -> bool:
        return self.transport_ok


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    sel = MagicMock()
    monkeypatch.setattr(oc, "sel", lambda: sel)
    return sel


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store = {"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP}

    async def _read() -> dict[str, str]:
        return dict(store)

    monkeypatch.setattr(rc, "_read_credentials", _read)
    monkeypatch.setattr(
        oc, "_saved_env_text", lambda: "".join(f"{k}={v}\n" for k, v in store.items())
    )
    # The saved-owner fallback reads the process environment for a key the
    # file lacks; keep a host value out of it.
    monkeypatch.delenv("KIROCREW_OWNER_ID", raising=False)
    return store


def _orch() -> SimpleNamespace:
    return SimpleNamespace(
        _owner_id="",
        _cfg=SimpleNamespace(slack_enterprise_ids=set()),
        slack=None,
        _socket_client=None,
        _background_tasks=set(),
        dashboard_state=SimpleNamespace(slack_client=None),
    )


def _claim(
    *, auth: dict[str, Any] | None = None, users: dict[str, Any] | None = None, **kw: Any
) -> _Claim:
    web_ = _Web(
        auth if auth is not None else {"team_id": TEAM, "team": "Acme", "user": "kirocrew-you"},
        users if users is not None else {OWNER: _user(OWNER)},
    )
    return _Claim(_orch(), web_, _Socket(), **kw)


def _dm(user: str = OWNER, text: str = "hello it is me", **event: Any) -> tuple[dict, dict]:
    ev = {"type": "message", "channel_type": "im", "user": user, "text": text}
    ev.update(event)
    return {"team_id": TEAM}, ev


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=_CEILING))


async def _opened(claim: _Claim) -> _Claim:
    await claim.start()
    return claim


# ── opening ──────────────────────────────────────────────────────────────────


def test_start_connects_a_private_client_and_publishes_nothing(creds, audit) -> None:
    claim = _claim()
    snap = _run(claim.start())

    assert snap["open"] and snap["team_name"] == "Acme" and snap["bot_name"] == "kirocrew-you"
    assert claim.sock.connected
    assert claim.sock.socket_mode_request_listeners == [claim._on_request]
    assert claim._orch.slack is None and claim._orch._socket_client is None
    assert claim._orch.dashboard_state.slack_client is None
    assert "claim_window_opened" in audit.log_api_access.call_args.kwargs["resources"]


@pytest.mark.parametrize("where", ["gateway", "saved"])
def test_start_refuses_when_either_owner_is_set(creds, audit, where) -> None:
    claim = _claim()
    if where == "gateway":
        claim._orch._owner_id = OWNER
    else:
        creds["KIROCREW_OWNER_ID"] = OWNER
    with pytest.raises(oc.ClaimRefused) as err:
        _run(claim.start())
    assert (err.value.code, err.value.status) == ("owner_set", 409)
    assert not claim.sock.connected


def test_start_validates_the_workspace_under_the_config_lock(creds, audit) -> None:
    """A Settings save cannot land a half-committed allowlist under the validation."""
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    seen: list[bool] = []

    class _Watching(_Claim):
        async def _enterprise_permits(self, bot: str) -> bool:
            seen.append(_get_config_lock().locked())
            return True

    claim = _Watching(_orch(), _Web({"team_id": TEAM, "team": "Acme"}, {}), _Socket())
    _run(claim.start())
    assert seen == [True]
    assert not _get_config_lock().locked()


def test_start_refuses_a_second_window(creds, audit) -> None:
    async def _go() -> None:
        claim = await _opened(_claim())
        with pytest.raises(oc.ClaimRefused) as err:
            await claim.start()
        assert (err.value.code, err.value.status) == ("window_open", 409)

    _run(_go())


def test_start_refuses_an_org_wide_install(creds, audit) -> None:
    claim = _claim(auth={"is_enterprise_install": True, "enterprise_id": "E0ORG"})
    with pytest.raises(oc.ClaimRefused) as err:
        _run(claim.start())
    assert err.value.code == "org_install_refused"
    assert not claim.sock.connected


def test_start_refuses_without_both_tokens(creds, audit) -> None:
    del creds["SLACK_APP_TOKEN"]
    with pytest.raises(oc.ClaimRefused) as err:
        _run(_claim().start())
    assert (err.value.code, err.value.status) == ("tokens_missing", 400)


@pytest.mark.parametrize(
    "gate, code",
    [("enterprise_ok", "enterprise_validation_failed"), ("transport_ok", "denied_by_policy")],
)
def test_start_runs_the_boot_gates(creds, audit, gate, code) -> None:
    claim = _claim()
    setattr(claim, gate, False)
    with pytest.raises(oc.ClaimRefused) as err:
        _run(claim.start())
    assert err.value.code == code
    assert not claim.sock.connected


def test_a_failed_connect_leaves_no_window(creds, audit) -> None:
    claim = _claim()
    claim.sock.fail = True
    with pytest.raises(oc.ClaimRefused) as err:
        _run(claim.start())
    assert err.value.status == 502
    assert not claim.is_open and claim.sock.closed


# ── closing ──────────────────────────────────────────────────────────────────


def test_expiry_disconnects_and_leaves_the_gateway_ownerless(creds, audit) -> None:
    async def _go() -> _Claim:
        claim = await _opened(_claim(window_secs=0.01))
        assert claim._expiry is not None
        await asyncio.wait_for(claim._expiry, timeout=_CEILING)
        return claim

    claim = _run(_go())
    assert not claim.is_open and claim.sock.closed
    assert claim._orch._owner_id == "" and claim._orch.slack is None
    assert "reason=expired" in audit.log_api_access.call_args.kwargs["resources"]


def test_cancel_disconnects(creds, audit) -> None:
    async def _go() -> _Claim:
        claim = await _opened(_claim())
        assert await claim.close("cancelled")
        assert not await claim.close("cancelled")
        return claim

    claim = _run(_go())
    assert claim.sock.closed and not claim.is_open


async def _drain(claim: _Claim) -> None:
    await asyncio.wait_for(asyncio.gather(*claim._orch._background_tasks), timeout=_CEILING)


def test_a_recheck_after_a_token_change_closes_the_window(creds, audit) -> None:
    async def _go() -> _Claim:
        claim = await _opened(_claim())
        creds["SLACK_BOT_TOKEN"] = "xoxb-another-workspace"
        claim.recheck_soon("credential_change")
        await _drain(claim)
        return claim

    claim = _run(_go())
    assert not claim.is_open and claim.sock.closed


def test_a_recheck_of_the_same_saved_tokens_keeps_the_window(creds, audit) -> None:
    """The watcher's late notice of the save the window opened after."""

    async def _go() -> _Claim:
        claim = await _opened(_claim())
        claim.recheck_soon("credential_change")
        await _drain(claim)
        return claim

    assert _run(_go()).is_open


def test_a_recheck_after_an_owner_is_saved_closes_the_window(creds, audit) -> None:
    async def _go() -> _Claim:
        claim = await _opened(_claim())
        creds["KIROCREW_OWNER_ID"] = "U0MANUAL"
        claim.recheck_soon("credential_write")
        await _drain(claim)
        return claim

    assert not _run(_go()).is_open


def test_start_refuses_when_the_connect_tokens_are_not_the_saved_ones(
    creds, audit, monkeypatch
) -> None:
    """An exported token beside a different .env line, or a CLI save between the reads."""

    async def _read() -> dict[str, str]:
        return {**creds, "SLACK_BOT_TOKEN": "xoxb-exported-elsewhere"}

    monkeypatch.setattr(rc, "_read_credentials", _read)
    claim = _claim()
    with pytest.raises(oc.ClaimRefused) as err:
        _run(claim.start())
    assert (err.value.code, err.value.status) == ("credentials_mismatch", 400)
    assert not claim.sock.connected


# ── filters ──────────────────────────────────────────────────────────────────


_REJECTED = [
    ("not im", {}, {"channel_type": "channel"}),
    ("app_mention", {}, {"type": "app_mention"}),
    ("bot_id", {}, {"bot_id": "B1"}),
    ("subtype", {}, {"subtype": "message_changed"}),
    ("other envelope team", {"team_id": "T0OTHER"}, {}),
    ("user_team mismatch", {}, {"user_team": "T0OTHER"}),
    ("bad user id", {}, {"user": "C0CHANNEL"}),
]


@pytest.mark.parametrize(
    "name, payload_patch, event_patch", _REJECTED, ids=[r[0] for r in _REJECTED]
)
def test_event_filters_reject(creds, audit, name, payload_patch, event_patch) -> None:
    async def _go() -> _Claim:
        claim = await _opened(_claim())
        payload, event = _dm(**event_patch)
        payload.update(payload_patch)
        assert not await claim.offer(payload, event)
        return claim

    claim = _run(_go())
    assert claim.snapshot()["candidates"] == [] and claim.snapshot()["ignored"] == 1


@pytest.mark.parametrize(
    "flag",
    ["deleted", "is_bot", "is_app_user", "is_restricted", "is_ultra_restricted", "is_stranger"],
)
def test_user_record_flags_reject(creds, audit, flag) -> None:
    async def _go() -> bool:
        claim = await _opened(_claim(users={OWNER: _user(OWNER, **{flag: True})}))
        return await claim.offer(*_dm())

    assert not _run(_go())


def test_both_team_fields_missing_is_rejected(creds, audit) -> None:
    async def _go() -> bool:
        claim = await _opened(_claim(users={OWNER: _user(OWNER, team_id="")}))
        return await claim.offer(*_dm())

    assert not _run(_go())


def test_users_info_team_mismatch_is_rejected(creds, audit) -> None:
    async def _go() -> bool:
        claim = await _opened(_claim(users={OWNER: _user(OWNER, team_id="T0OTHER")}))
        return await claim.offer(*_dm(user_team=TEAM))

    assert not _run(_go())


def test_a_failed_users_info_is_rejected(creds, audit) -> None:
    async def _go() -> bool:
        claim = await _opened(_claim(users={}))
        return await claim.offer(*_dm())

    assert not _run(_go())


def test_the_origin_gate_refusal_is_rejected(creds, audit, monkeypatch) -> None:
    import kiro_crew.platform as platform

    gate = SimpleNamespace(check_message_origin=lambda team: False)
    monkeypatch.setattr(platform, "current_context", lambda: SimpleNamespace(slack_gate=gate))

    async def _go() -> bool:
        claim = await _opened(_claim())
        return await claim.offer(*_dm())

    assert not _run(_go())


# ── candidates ───────────────────────────────────────────────────────────────


def test_a_candidate_row_carries_text_time_and_profile(creds, audit) -> None:
    async def _go() -> dict:
        claim = await _opened(_claim())
        assert await claim.offer(*_dm(text="it is me, from my laptop", user_team=TEAM))
        return claim.snapshot()

    snap = _run(_go())
    (row,) = snap["candidates"]
    assert row["user_id"] == OWNER and row["text"] == "it is me, from my laptop"
    assert row["handle"] == "h-u0owner" and row["display_name"] == "Name U0OWNER"
    assert row["team_name"] == "Acme" and row["received_at"] > 0
    assert row["avatar"].startswith("https://")
    assert snap["multiple"] is False


def test_a_flood_from_one_sender_keeps_one_row_and_one_lookup(creds, audit) -> None:
    other = "U0OTHER"

    async def _go() -> _Claim:
        claim = await _opened(_claim(users={OWNER: _user(OWNER), other: _user(other)}))
        await claim.offer(*_dm(OWNER, "mine"))
        for i in range(20):
            await claim.offer(*_dm(other, f"spam {i}"))
        return claim

    claim = _run(_go())
    snap = claim.snapshot()
    assert [c["user_id"] for c in snap["candidates"]] == [other, OWNER]
    assert snap["candidates"][0]["text"] == "spam 19"
    assert snap["multiple"] is True
    assert claim.web_.user_calls == [OWNER, other]


def test_a_fourth_sender_is_ignored_and_the_first_three_stay(creds, audit) -> None:
    ids = ["U01", "U02", "U03", "U04"]

    async def _go() -> tuple[list[bool], dict]:
        claim = await _opened(_claim(users={u: _user(u) for u in ids}))
        kept = [await claim.offer(*_dm(u)) for u in ids]
        kept.append(await claim.offer(*_dm("U01", "me again")))
        return kept, claim.snapshot()

    kept, snap = _run(_go())
    assert kept == [True, True, True, False, True]
    assert [c["user_id"] for c in snap["candidates"]] == ["U03", "U02", "U01"]
    assert snap["candidates"][2]["text"] == "me again"
    assert snap["ignored"] == 1
    assert snap["multiple"] is True


def test_nothing_is_offered_when_the_window_is_closed(creds, audit) -> None:
    claim = _claim()
    assert not _run(claim.offer(*_dm()))
    assert claim.snapshot()["ignored"] == 0


def test_audit_and_logs_carry_no_name_handle_or_text(creds, audit, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="kiro_crew.slack.owner_claim")

    async def _go() -> None:
        claim = await _opened(_claim())
        await claim.offer(*_dm(text="secret words"))
        await claim.close("cancelled")

    _run(_go())
    dumped = json.dumps([c.kwargs for c in audit.log_api_access.call_args_list])
    dumped += caplog.text
    for leaked in ("secret words", "Name U0OWNER", "h-u0owner", OWNER):
        assert leaked not in dumped
    assert oc.member_hash(OWNER) in dumped


# ── silence ──────────────────────────────────────────────────────────────────


def _req(kind: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=kind, envelope_id=f"env-{kind}", payload=payload)


def test_the_listener_acks_everything_and_routes_nothing(creds, audit, monkeypatch) -> None:
    from kiro_crew.slack import events

    monkeypatch.setattr(events, "_route_message", AsyncMock(side_effect=AssertionError("routed")))

    async def _go() -> _Claim:
        claim = await _opened(_claim())
        payload, event = _dm()
        for req in (
            _req("slash_commands", {"command": "/kirocrew", "user_id": OWNER}),
            _req("interactive", {"type": "block_actions", "user": {"id": OWNER}}),
            _req(
                "events_api", {"team_id": TEAM, "event": {"type": "app_home_opened", "user": OWNER}}
            ),
            _req("events_api", {**payload, "event": event}),
        ):
            await claim._on_request(claim.sock, req)
        return claim

    claim = _run(_go())
    assert len(claim.sock.acks) == 4
    assert [c["user_id"] for c in claim.snapshot()["candidates"]] == [OWNER]
    # _Web raises on any call but auth.test / users.info, so nothing was posted,
    # and no client was published for any outbound path to use.
    assert claim._orch.slack is None and claim._orch._socket_client is None


# ── confirm ──────────────────────────────────────────────────────────────────


def _connected_reconnect(claim: _Claim, posted: list) -> AsyncMock:
    async def _reconnect() -> dict[str, Any]:
        claim._orch._owner_id = written[0]
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D0DM")
        slack.post_message = AsyncMock(side_effect=lambda ch, text: posted.append((ch, text)))
        claim._orch.slack = slack
        return {"connected": True, "connect_error": ""}

    written: list[str] = []
    mock = AsyncMock(side_effect=_reconnect)
    mock.written = written  # type: ignore[attr-defined]
    return mock


def test_confirm_writes_the_id_verbatim_reconnects_and_welcomes(creds, audit) -> None:
    grid_user = "W0GRIDUSER"
    posted: list = []

    async def _go() -> tuple[_Claim, dict, list]:
        claim = await _opened(_claim(users={grid_user: _user(grid_user)}))
        await claim.offer(*_dm(grid_user))
        reconnect = _connected_reconnect(claim, posted)

        async def _write(value: str, check: Any) -> None:
            check(f"SLACK_BOT_TOKEN={BOT}\nSLACK_APP_TOKEN={APP}\n")
            reconnect.written.append(value)

        result = await claim.confirm(grid_user, write_owner=_write, reconnect=reconnect)
        return claim, result, reconnect.written

    claim, result, written = _run(_go())
    assert written == [grid_user]
    assert result == {"owner_id": grid_user, "connected": True, "connect_error": ""}
    assert not claim.is_open and claim.sock.closed
    assert posted and posted[0][0] == "D0DM"
    assert claim.snapshot()["confirmed"]["user_id"] == grid_user


def test_confirm_refuses_an_id_that_is_not_a_candidate(creds, audit) -> None:
    async def _go() -> None:
        claim = await _opened(_claim())
        with pytest.raises(oc.ClaimRefused) as err:
            await claim.confirm(OWNER, write_owner=AsyncMock(), reconnect=None)
        assert err.value.status == 404

    _run(_go())


class _EnvWriter:
    """The credential writer's shape: runs *check* on the saved ``.env`` text, then writes."""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.written: list[str] = []

    async def __call__(self, value: str, check: Any) -> None:
        check("".join(f"{k}={v}\n" for k, v in self.store.items()))
        self.written.append(value)


def test_confirm_compares_before_it_writes(creds, audit) -> None:
    write = _EnvWriter(creds)

    async def _go() -> _Claim:
        claim = await _opened(_claim())
        await claim.offer(*_dm())
        creds["KIROCREW_OWNER_ID"] = "U0MANUAL"  # a manual Save after start
        with pytest.raises(oc.ClaimRefused) as err:
            await claim.confirm(OWNER, write_owner=write, reconnect=None)
        assert (err.value.code, err.value.status) == ("owner_set", 409)
        return claim

    claim = _run(_go())
    assert write.written == []
    assert not claim.is_open


@pytest.mark.parametrize("key", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
def test_confirm_refuses_after_a_token_changed(creds, audit, key, monkeypatch) -> None:
    write = _EnvWriter(creds)

    async def _go() -> None:
        claim = await _opened(_claim())
        await claim.offer(*_dm())
        # A CLI save: the file holds the new token while the process
        # environment still holds the old one until the watcher's next poll.
        monkeypatch.setenv(key, creds[key])
        creds[key] = creds[key] + "-new"
        with pytest.raises(oc.ClaimRefused) as err:
            await claim.confirm(OWNER, write_owner=write, reconnect=None)
        assert err.value.code == "credentials_changed"
        assert not claim.is_open

    _run(_go())
    assert write.written == []


def test_a_process_value_beside_a_saved_line_does_not_change_the_comparison(
    creds, audit, monkeypatch
) -> None:
    """Start and confirm read the saved tokens one way, whatever else the process holds."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-exported-elsewhere")
    write = _EnvWriter(creds)

    async def _go() -> dict:
        claim = await _opened(_claim())
        await claim.offer(*_dm())
        return await claim.confirm(OWNER, write_owner=write, reconnect=None)

    assert _run(_go())["owner_id"] == OWNER
    assert write.written == [OWNER]


def test_a_case_variant_owner_line_counts_as_a_saved_owner(monkeypatch) -> None:
    """On Windows ``kirocrew_owner_id=`` is the same entry as ``KIROCREW_OWNER_ID``."""
    import kiro_crew.config.loader as loader

    # Through the facade: the mirror carries the patch to every holder.
    monkeypatch.setattr(loader, "_env_name_key", str.upper)
    monkeypatch.delenv("KIROCREW_OWNER_ID", raising=False)
    saved = oc._persisted_slack_credentials(
        f"SLACK_BOT_TOKEN={BOT}\nSLACK_APP_TOKEN={APP}\nkirocrew_owner_id=U0MANUAL\n"
    )
    assert saved["KIROCREW_OWNER_ID"] == "U0MANUAL"


def test_a_process_provided_reference_is_resolved_like_a_saved_one(monkeypatch) -> None:
    import kiro_crew.config.loader as loader

    monkeypatch.setenv("SLACK_BOT_TOKEN", "secret://SLACK_BOT_TOKEN")
    monkeypatch.delenv("KIROCREW_OWNER_ID", raising=False)
    # Through the facade: the mirror carries the patch to every holder.
    monkeypatch.setattr(
        loader,
        "resolve_env_credentials",
        lambda values: {
            k: (BOT if v == "secret://SLACK_BOT_TOKEN" else v) for k, v in values.items()
        },
    )
    saved = oc._persisted_slack_credentials(f"SLACK_APP_TOKEN={APP}\n")
    assert saved == {"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP, "KIROCREW_OWNER_ID": ""}


def test_removing_a_saved_token_refuses_even_while_the_process_holds_it(
    creds, audit, monkeypatch
) -> None:
    write = _EnvWriter(creds)

    async def _go() -> None:
        claim = await _opened(_claim())
        await claim.offer(*_dm())
        monkeypatch.setenv("SLACK_BOT_TOKEN", creds.pop("SLACK_BOT_TOKEN"))
        with pytest.raises(oc.ClaimRefused) as err:
            await claim.confirm(OWNER, write_owner=write, reconnect=None)
        assert err.value.code == "credentials_changed"

    _run(_go())
    assert write.written == []


def test_an_unresolvable_saved_owner_still_counts_as_saved(creds, audit) -> None:
    write = _EnvWriter(creds)

    async def _go() -> None:
        claim = await _opened(_claim())
        await claim.offer(*_dm())
        creds["KIROCREW_OWNER_ID"] = "secret://NOT_IN_THE_VAULT"
        with pytest.raises(oc.ClaimRefused) as err:
            await claim.confirm(OWNER, write_owner=write, reconnect=None)
        assert err.value.code == "owner_set"

    _run(_go())
    assert write.written == []


def test_an_unresolvable_saved_token_refuses_the_confirm(creds, audit) -> None:
    write = _EnvWriter(creds)

    async def _go() -> None:
        claim = await _opened(_claim())
        await claim.offer(*_dm())
        creds["SLACK_BOT_TOKEN"] = "secret://NOT_IN_THE_VAULT"
        with pytest.raises(oc.ClaimRefused) as err:
            await claim.confirm(OWNER, write_owner=write, reconnect=None)
        assert err.value.code == "credentials_changed"

    _run(_go())
    assert write.written == []


# ── the routes ───────────────────────────────────────────────────────────────


_ROUTES = [
    ("get", "/api/slack/owner-claim", "api_slack_owner_claim_get"),
    ("post", "/api/slack/owner-claim/start", "api_slack_owner_claim_start"),
    ("post", "/api/slack/owner-claim/cancel", "api_slack_owner_claim_cancel"),
    ("post", "/api/slack/owner-claim/confirm", "api_slack_owner_claim_confirm"),
]


def _call(
    monkeypatch, method: str, path: str, handler: str, *, local: bool, claim: Any, body: Any = None
) -> tuple[int, dict]:
    monkeypatch.setattr(msg, "is_direct_local_request", lambda req: local)
    monkeypatch.setattr(msg, "_sel", lambda: MagicMock())

    async def _go() -> tuple[int, dict]:
        app = web.Application()
        app["state"] = SimpleNamespace(slack_owner_claim=claim, slack_reconnect=None)
        app.router.add_route(method.upper(), path, getattr(msg, handler))
        async with TestClient(TestServer(app)) as client:
            resp = await client.request(method.upper(), path, json=body)
            return resp.status, json.loads(await resp.text())

    return asyncio.run(_go())


@pytest.mark.parametrize("method, path, handler", _ROUTES)
def test_every_route_refuses_a_remote_request(monkeypatch, method, path, handler) -> None:
    claim = MagicMock()
    status, body = _call(monkeypatch, method, path, handler, local=False, claim=claim)
    assert status == 403 and body["code"] == "remote_read_only"
    assert claim.mock_calls == []


@pytest.mark.parametrize("method, path, handler", _ROUTES)
def test_every_route_without_a_claim_owner_is_unavailable(
    monkeypatch, method, path, handler
) -> None:
    status, body = _call(monkeypatch, method, path, handler, local=True, claim=None)
    assert status == 503


def test_start_route_maps_a_refusal(monkeypatch) -> None:
    claim = MagicMock()
    claim.start = AsyncMock(side_effect=oc.ClaimRefused("owner_set"))
    status, body = _call(monkeypatch, *_ROUTES[1], local=True, claim=claim)
    assert status == 409 and body["code"] == "owner_set"


def test_get_and_cancel_routes_answer_the_snapshot(monkeypatch) -> None:
    claim = MagicMock()
    claim.snapshot = MagicMock(return_value={"open": False})
    claim.close = AsyncMock(return_value=True)
    assert _call(monkeypatch, *_ROUTES[0], local=True, claim=claim) == (200, {"open": False})
    assert _call(monkeypatch, *_ROUTES[2], local=True, claim=claim) == (200, {"open": False})
    claim.close.assert_awaited_once_with("cancelled")


def test_start_route_answers_the_opened_window(monkeypatch) -> None:
    claim = MagicMock()
    claim.start = AsyncMock(return_value={"open": True})
    assert _call(monkeypatch, *_ROUTES[1], local=True, claim=claim) == (200, {"open": True})


@pytest.mark.parametrize("route", [1, 3])
def test_start_and_confirm_routes_report_an_unreadable_store(monkeypatch, route) -> None:
    claim = MagicMock()
    claim.start = AsyncMock(side_effect=rc.CredentialStoreUnreadable("OSError"))
    claim.confirm = AsyncMock(side_effect=rc.CredentialStoreUnreadable("OSError"))
    status, body = _call(
        monkeypatch, *_ROUTES[route], local=True, claim=claim, body={"user_id": OWNER}
    )
    assert (status, body["code"]) == (500, "credential_store_unreadable")


def test_confirm_route_maps_a_refusal(monkeypatch) -> None:
    claim = MagicMock()
    claim.confirm = AsyncMock(side_effect=oc.ClaimRefused("not_a_candidate", 404))
    status, body = _call(monkeypatch, *_ROUTES[3], local=True, claim=claim, body={"user_id": OWNER})
    assert (status, body["code"]) == (404, "not_a_candidate")


def test_confirm_route_writes_through_the_checked_env_writer(monkeypatch) -> None:
    """The route hands confirm a writer that passes its check on to the .env write."""
    seen: list[tuple[dict, Any]] = []

    async def _fake_write(updates: dict, rollback: Any, *, check: Any = None) -> None:
        seen.append((updates, check))

    monkeypatch.setattr(msg, "_write_env_or_roll_back", _fake_write)

    async def _confirm(user_id: str, *, write_owner: Any, reconnect: Any) -> dict:
        await write_owner(user_id, "the-check")
        return {"owner_id": user_id, "connected": True, "connect_error": ""}

    claim = MagicMock()
    claim.confirm = _confirm
    status, body = _call(monkeypatch, *_ROUTES[3], local=True, claim=claim, body={"user_id": OWNER})
    assert status == 200 and body["owner_id"] == OWNER
    assert seen == [({"KIROCREW_OWNER_ID": OWNER}, "the-check")]


def test_confirm_route_rejects_a_body_that_is_not_json(monkeypatch) -> None:
    claim = MagicMock()
    claim.confirm = AsyncMock()
    status, body = _call(monkeypatch, *_ROUTES[3], local=True, claim=claim)
    assert (status, body["code"]) == (400, "invalid_json")
    claim.confirm.assert_not_awaited()


def test_confirm_route_rejects_a_malformed_id(monkeypatch) -> None:
    claim = MagicMock()
    claim.confirm = AsyncMock()
    status, body = _call(
        monkeypatch, *_ROUTES[3], local=True, claim=claim, body={"user_id": "C0CHANNEL"}
    )
    assert status == 400
    claim.confirm.assert_not_awaited()


@pytest.mark.parametrize("body", [[], "U0OWNER", 7], ids=repr)
def test_confirm_route_answers_a_non_object_body_with_400(monkeypatch, body) -> None:
    claim = MagicMock()
    claim.confirm = AsyncMock()
    status, payload = _call(monkeypatch, *_ROUTES[3], local=True, claim=claim, body=body)
    assert status == 400 and payload["code"] == "body_not_object"
    claim.confirm.assert_not_awaited()


def test_the_routes_are_registered() -> None:
    from kiro_crew.dashboard.routes import messaging as routes

    app = web.Application()
    routes.register(app)
    seen = {(r.method, r.resource.canonical) for r in app.router.routes()}  # type: ignore[union-attr]
    for method, path, _ in _ROUTES:
        assert (method.upper(), path) in seen


def test_the_config_get_does_not_read_the_claim() -> None:
    import inspect

    from kiro_crew.dashboard.messaging_api import slack_settings

    assert "owner_claim" not in inspect.getsource(slack_settings.api_slack_config_get)


def test_a_cancel_during_confirm_waits_and_finds_no_window(creds, audit) -> None:
    async def _go() -> tuple[bool, dict]:
        claim = await _opened(_claim())
        await claim.offer(*_dm())
        writing = asyncio.Event()
        release = asyncio.Event()

        async def _write(value: str, check: Any) -> None:
            writing.set()
            await asyncio.wait_for(release.wait(), timeout=_CEILING)

        confirm = asyncio.create_task(claim.confirm(OWNER, write_owner=_write, reconnect=None))
        await asyncio.wait_for(writing.wait(), timeout=_CEILING)
        assert claim._lock.locked()
        cancel = asyncio.create_task(claim.close("cancelled"))
        release.set()
        result = await asyncio.wait_for(confirm, timeout=_CEILING)
        return await asyncio.wait_for(cancel, timeout=_CEILING), result

    cancelled, result = _run(_go())
    assert cancelled is False
    assert result["owner_id"] == OWNER


def test_the_user_cache_keeps_only_bounded_fields(creds, audit) -> None:
    long_user = _user(OWNER, extra_blob="x" * 10_000)
    long_user["profile"]["display_name"] = "n" * 500
    long_user["profile"]["image_72"] = "https://a.example/" + "p" * 1000

    async def _go() -> tuple[_Claim, dict]:
        claim = await _opened(_claim(users={OWNER: long_user}))
        assert await claim.offer(*_dm())
        return claim, claim.snapshot()

    claim, snap = _run(_go())
    cached = claim._users[OWNER]
    assert cached is not None and "extra_blob" not in cached
    assert len(cached["profile"]["display_name"]) <= 80
    assert snap["candidates"][0]["avatar"] == ""


def test_a_reconnect_with_a_saved_owner_closes_an_open_window(creds, audit, monkeypatch) -> None:
    async def _go() -> _Claim:
        claim = await _opened(_claim())
        claim._orch.dashboard_state.slack_owner_claim = claim
        creds["KIROCREW_OWNER_ID"] = OWNER

        async def _refuse(orch: Any, token: str) -> tuple[None, dict]:
            return None, {"connected": False, "connect_error": "stop-here"}

        monkeypatch.setattr(rc, "_check_workspace", _refuse)
        orch = claim._orch
        orch._slack_reconnecting = False
        orch._slack_give_up_task = None
        orch._slack_client_lost = False
        await rc._reconnect(orch)
        return claim

    claim = _run(_go())
    assert not claim.is_open and claim.sock.closed


def test_a_credential_write_during_a_connecting_start_closes_it(creds, audit) -> None:
    async def _go() -> _Claim:
        claim = _claim()
        connecting = asyncio.Event()
        release = asyncio.Event()

        async def _slow_connect() -> None:
            connecting.set()
            await asyncio.wait_for(release.wait(), timeout=_CEILING)
            claim.sock.connected = True

        claim.sock.connect = _slow_connect  # type: ignore[method-assign]
        start = asyncio.create_task(claim.start())
        await asyncio.wait_for(connecting.wait(), timeout=_CEILING)
        creds["SLACK_APP_TOKEN"] = "xapp-another-app"
        claim.recheck_soon("credential_write")
        release.set()
        await asyncio.wait_for(start, timeout=_CEILING)
        await asyncio.wait_for(asyncio.gather(*claim._orch._background_tasks), timeout=_CEILING)
        return claim

    claim = _run(_go())
    assert not claim.is_open and claim.sock.closed
