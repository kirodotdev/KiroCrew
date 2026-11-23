"""Settings → Messaging Channels → Slack → Share Slack access: the routes behind it.

Turning it on needs the page's explicit confirm; an add is refused unless Slack
confirms the id now; writes come only from this machine's own browser and never
from an app token or an internal caller; each write is audited with the id only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import loader
from kiro_crew.dashboard import slack_shared_access as mod
from kiro_crew.slack import shared_access

OWNER = "UOWNER0001"
FRIEND = "UFRIEND001"
TEAM = "T0000TEAM1"


class _Web:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {
            FRIEND: {"team_id": TEAM, "profile": {"real_name": "Friend Person"}},
            "UBOT000001": {"team_id": TEAM, "is_bot": True},
        }

    async def auth_test(self) -> dict[str, Any]:
        return {"team_id": TEAM}

    async def users_info(self, user: str) -> dict[str, Any]:
        if user not in self.users:
            raise RuntimeError("user_not_found")
        return {"user": self.users[user]}


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.rows.append(kw)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    envf = tmp_path / ".env"
    envf.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(loader, "env_path", lambda: envf)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    sel = _Sel()
    monkeypatch.setattr(mod, "sel", lambda: sel)
    web_client = _Web()
    monkeypatch.setattr(mod, "_web_client", lambda token: web_client)

    async def _identity() -> tuple[str, str]:
        return "xoxb-test", OWNER

    monkeypatch.setattr(mod, "_slack_identity", _identity)
    local = {"value": True}
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: local["value"])
    owner = {"value": True}

    async def _owner_refusal(request: Any, operation: str) -> web.Response | None:
        return None if owner["value"] else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(mod, "_owner_refusal", _owner_refusal)
    shared_access.reset()
    yield {"cfg": cfg, "sel": sel, "local": local, "owner": owner, "web": web_client}
    shared_access.reset()


def _call(
    env: dict[str, Any],
    method: str,
    path: str,
    body: dict | None = None,
    *,
    request_marks: dict[str, Any] | None = None,
) -> tuple[int, dict]:
    @web.middleware
    async def _marks(request: web.Request, handler: Any) -> web.StreamResponse:
        for key, value in (request_marks or {}).items():
            request[key] = value
        return await handler(request)

    async def _run() -> tuple[int, dict]:
        app = web.Application(middlewares=[_marks])
        mod.register(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.request(method, path, json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run())


def _stored(env: dict[str, Any]) -> dict:
    if not env["cfg"].exists():
        return {}
    return (
        json.loads(env["cfg"].read_text(encoding="utf-8")).get("slack", {}).get("shared_access", {})
    )


def test_it_reads_off_and_empty(env: dict) -> None:
    status, body = _call(env, "GET", "/api/slack/shared-access")
    assert status == 200
    assert body == {
        "enabled": False,
        "verify_with_slack": False,
        "users": [],
        "read_only": False,
    }


def test_turning_it_on_needs_the_confirm(env: dict) -> None:
    status, body = _call(env, "PUT", "/api/slack/shared-access", {"enabled": True})
    assert status == 400 and body["code"] == "confirm_required"
    assert _stored(env) == {}
    status, _ = _call(env, "PUT", "/api/slack/shared-access", {"enabled": True, "confirm": True})
    assert status == 200 and _stored(env)["enabled"] is True
    # Turning it off needs no confirm.
    status, _ = _call(env, "PUT", "/api/slack/shared-access", {"enabled": False})
    assert status == 200 and _stored(env)["enabled"] is False


@pytest.mark.parametrize(
    "marks", [{"internal_auth": True}, {"app": "some-app"}], ids=["internal", "app"]
)
def test_an_app_or_an_internal_caller_changes_nothing(env: dict, marks: dict) -> None:
    for method, path, body in (
        ("PUT", "/api/slack/shared-access", {"enabled": True, "confirm": True}),
        ("POST", "/api/slack/shared-access/users", {"user_id": FRIEND}),
        ("DELETE", f"/api/slack/shared-access/users/{FRIEND}", None),
    ):
        status, _ = _call(env, method, path, body, request_marks=marks)
        assert status == 403
    assert _stored(env) == {}


def test_a_remote_session_reads_but_cannot_write(env: dict) -> None:
    env["local"]["value"] = False
    status, body = _call(env, "GET", "/api/slack/shared-access")
    assert status == 200 and body["read_only"] is True
    status, _ = _call(env, "POST", "/api/slack/shared-access/users", {"user_id": FRIEND})
    assert status == 403 and _stored(env) == {}


def _verify_with_slack(env: dict) -> None:
    """Turn ``slack.shared_access.verify_with_slack`` on in config.json."""
    section = {"shared_access": {"verify_with_slack": True}}
    env["cfg"].write_text(json.dumps({"slack": section}), encoding="utf-8")


def test_with_verify_an_add_is_kept_only_once_slack_confirms_it(env: dict) -> None:
    _verify_with_slack(env)
    status, body = _call(env, "POST", "/api/slack/shared-access/users", {"user_id": FRIEND})
    assert status == 200 and body["user"]["name"] == "Friend Person"
    assert _stored(env)["user_ids"] == [FRIEND]
    for user_id, code in (
        ("UBOT000001", shared_access.REASON_BOT),
        (OWNER, shared_access.REASON_OWNER),
        ("UNOBODY001", shared_access.REASON_UNVERIFIABLE),
        ("B123", shared_access.REASON_MALFORMED),
    ):
        status, body = _call(env, "POST", "/api/slack/shared-access/users", {"user_id": user_id})
        assert status == 400 and body["code"] == code, user_id
    assert _stored(env)["user_ids"] == [FRIEND]
    status, body = _call(env, "GET", "/api/slack/shared-access")
    assert body["users"] == [
        {"user_id": FRIEND, "name": "Friend Person", "confirmed": True, "reason": "ok"}
    ]


def test_a_removal_is_stored_and_audited_by_id(env: dict) -> None:
    _call(env, "POST", "/api/slack/shared-access/users", {"user_id": FRIEND})
    status, _ = _call(env, "DELETE", f"/api/slack/shared-access/users/{FRIEND}")
    assert status == 200 and _stored(env)["user_ids"] == []
    ops = [(r["operation"], r["outcome"], r.get("resources", "")) for r in env["sel"].rows]
    assert ("slack.shared_access.add", "ok", FRIEND) in ops
    assert ("slack.shared_access.remove", "ok", FRIEND) in ops


def test_every_route_is_the_dashboard_owners(env: dict) -> None:
    env["owner"]["value"] = False
    for method, path, body in (
        ("GET", "/api/slack/shared-access", None),
        ("PUT", "/api/slack/shared-access", {"enabled": True, "confirm": True}),
        ("POST", "/api/slack/shared-access/users", {"user_id": FRIEND}),
        ("DELETE", f"/api/slack/shared-access/users/{FRIEND}", None),
    ):
        status, body_ = _call(env, method, path, body)
        assert status == 403 and body_["code"] == "owner_only", (method, path)
    assert _stored(env) == {}


def test_a_write_the_gateway_did_not_apply_is_not_reported_as_done(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_live_matches", lambda stored: False)
    status, body = _call(env, "PUT", "/api/slack/shared-access", {"enabled": False})
    assert status == 409 and body["code"] == "not_applied"


@pytest.mark.parametrize(
    "observed, named",
    [
        ({"slack"}, ["slack"]),
        ({"slack", "*", "*config.json"}, ["slack"]),
        # A whole-file marker the Slack watcher never consults (a repaired tear), and
        # another section it never waits on, name nothing.
        ({"*", "*config.json"}, []),
        ({"publish"}, []),
        (set(), []),
    ],
)
def test_an_unapplied_write_names_what_kiro_crew_could_not_read_and_nothing_else(
    env: dict, monkeypatch: pytest.MonkeyPatch, observed: set, named: list
) -> None:
    from kiro_crew.config import resolution

    token = "xoxb-" + "1" * 12 + "-" + "2" * 12 + "-SECRETVALUE"
    loader.config_path().write_text(
        json.dumps({"slack": {"bot_token": token, "owner_id": OWNER}}), encoding="utf-8"
    )
    monkeypatch.setattr(resolution, "_OBSERVED_DEGRADED_SECTIONS", set(observed))
    monkeypatch.setattr(mod, "_live_matches", lambda stored: False)
    status, body = _call(env, "PUT", "/api/slack/shared-access", {"enabled": False})
    assert status == 409 and body["code"] == "not_applied"
    # Paths only: a section or file name, never a value from the file.
    assert body["unread"] == named
    assert token not in json.dumps(body) and OWNER not in json.dumps(body)


def test_a_removed_id_is_checked_with_slack_again_when_re_listed(env: dict) -> None:
    shared_access.install(env["web"])  # the page checks with the gateway's own bot
    _call(env, "POST", "/api/slack/shared-access/users", {"user_id": FRIEND})
    assert shared_access._verdicts.get(FRIEND) is not None
    _call(env, "DELETE", f"/api/slack/shared-access/users/{FRIEND}")
    assert shared_access._verdicts.get(FRIEND) is None


def test_the_page_shows_the_live_grant_after_an_unapplied_removal(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    _call(env, "POST", "/api/slack/shared-access/users", {"user_id": FRIEND})
    live = SimpleNamespace(enabled=True, user_ids=[FRIEND], verify_with_slack=False)
    monkeypatch.setattr(mod, "_live_shared", lambda: live)
    status, _ = _call(env, "DELETE", f"/api/slack/shared-access/users/{FRIEND}")
    assert status == 409 and _stored(env)["user_ids"] == []  # saved, not applied
    status, body = _call(env, "GET", "/api/slack/shared-access")
    assert status == 200 and body["enabled"] is True
    assert [u["user_id"] for u in body["users"]] == [FRIEND]  # still has access


def test_a_check_with_other_credentials_never_admits_anyone(env: dict) -> None:
    # A bot token saved but not yet live names another workspace: the page shows its
    # answer, but the gateway's gate keeps nothing from it.
    shared_access.install(_Web())  # the live bot, a different client
    status, body = _call(env, "POST", "/api/slack/shared-access/users", {"user_id": FRIEND})
    assert status == 200 and body["user"]["confirmed"] is True
    assert shared_access._verdicts.get(FRIEND) is None


def test_without_verify_an_add_is_kept_on_the_list_alone(env: dict) -> None:
    asked: list[str] = []

    async def _never(user: str) -> dict[str, Any]:
        asked.append(user)
        raise AssertionError("users.info must not be called")

    env["web"].users_info = _never
    for user_id in (FRIEND, "UBOT000001", "UNOBODY001"):
        status, body = _call(env, "POST", "/api/slack/shared-access/users", {"user_id": user_id})
        assert status == 200 and body["user"]["reason"] == shared_access.REASON_LISTED, user_id
    for user_id, code in (
        (OWNER, shared_access.REASON_OWNER),
        ("B123", shared_access.REASON_MALFORMED),
    ):
        status, body = _call(env, "POST", "/api/slack/shared-access/users", {"user_id": user_id})
        assert status == 400 and body["code"] == code, user_id
    assert asked == []
    status, body = _call(env, "GET", "/api/slack/shared-access")
    assert body["verify_with_slack"] is False
    assert body["users"][0] == {
        "user_id": FRIEND,
        "name": "",
        "confirmed": True,
        "reason": shared_access.REASON_LISTED,
    }


def test_turning_it_on_stores_the_default_so_a_load_does_not_migrate_it(env: dict) -> None:
    status, _ = _call(env, "PUT", "/api/slack/shared-access", {"enabled": True, "confirm": True})
    assert status == 200
    assert _stored(env)["verify_with_slack"] is False


def test_a_write_keeps_verify_with_slack_as_the_owner_set_it(env: dict) -> None:
    _verify_with_slack(env)
    status, _ = _call(env, "DELETE", f"/api/slack/shared-access/users/{FRIEND}")
    assert status == 200
    assert _stored(env)["verify_with_slack"] is True
