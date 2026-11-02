"""``dashboard/handlers/weixin_qr.py``: the handlers and failure paths ``test_weixin_qr.py`` skips.

That file pins the ``.env``/``config.json`` write helpers and one confirmed sign-in. This one drives
QR start, status polling and the config GET/PUT. The iLink client, the QR renderer and the
credential commit are replaced, so nothing opens a socket and every file lands in ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import weixin_qr as qr
from kiro_crew.hidden_leaf_staging import EnvStagingRefused

LONG = "x" * 300
PNG = "data:image/png;base64,QQ=="
LOGIN = "https://login.example.invalid/q"
BAD_FOLDER = {"code": "invalid_session_folder"}
CREDS = {"bot_token": "tok", "ilink_bot_id": "acct"}


def _client(reply: Any = None, fails: str = "") -> SimpleNamespace:
    """A ``WeixinClient`` stand-in: each call answers ``reply``, except ``fails``, which raises."""

    def call(name: str) -> AsyncMock:
        error = ConnectionError(f"{name} refused {LONG}") if name == fails else None
        return AsyncMock(return_value=reply, side_effect=error)

    names = ("connect", "get_bot_qrcode", "get_qrcode_status")
    return SimpleNamespace(close=AsyncMock(), **{name: call(name) for name in names})


def _run(handler: Any, request: Any) -> tuple[int, dict[str, Any]]:
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.text)


@pytest.fixture(autouse=True)
def sessions(_floor_monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """An empty QR-session table for the test, and a loopback caller."""
    table: dict[str, dict[str, Any]] = {}
    _floor_monkeypatch.setattr(qr, "_SESSIONS", table)
    _floor_monkeypatch.setattr(qr, "is_direct_local_request", lambda request: True)
    return table


def test_env_helpers_cope_with_a_missing_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(qr, "env_path", lambda: tmp_path / ".env")
    assert qr._read_env_value("WEIXIN_TOKEN") is None
    # a rollback has nothing to restore into: refused when a prior line existed, a no-op if not
    with pytest.raises(OSError, match="vanished"):
        qr._restore_env_entry("WEIXIN_TOKEN", ["WEIXIN_TOKEN=old"], "WEIXIN_TOKEN=new")
    assert qr._restore_env_entry("WEIXIN_TOKEN", [], "WEIXIN_TOKEN=new") is None
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    "handler", [qr.weixin_qr_start, qr.weixin_qr_status, qr.weixin_config_save]
)
def test_remote_callers_are_refused_before_anything_is_touched(sessions, monkeypatch, handler):
    monkeypatch.setattr(qr, "is_direct_local_request", lambda request: False)
    monkeypatch.setattr(qr, "WeixinClient", MagicMock(side_effect=AssertionError("client built")))
    refusal = {"error": "read-only from remote sessions (local machine only)"}
    assert _run(handler, SimpleNamespace(query={"session_id": "s1"})) == (403, refusal)
    assert sessions == {}


def _start(monkeypatch, client: SimpleNamespace, render: Any = None):
    """Run the start handler against ``client``; return status, body and what was rendered."""
    rendered: list[str] = []

    def paint(data: str) -> str:
        rendered.append(data)
        return PNG

    monkeypatch.setattr(qr, "WeixinClient", MagicMock(return_value=client))
    monkeypatch.setattr(qr, "_render_qr_data_uri", render or paint)
    return (*_run(qr.weixin_qr_start, SimpleNamespace()), rendered)


@pytest.mark.parametrize(
    ("reply", "scanned"),
    [({"qrcode": "hex", "qrcode_img_content": LOGIN}, LOGIN), ({"qrcode": "hex"}, "hex")],
)
def test_start_renders_the_login_url_or_falls_back_to_the_token(
    sessions, monkeypatch, reply, scanned
):
    client = _client(reply)
    monkeypatch.setattr(qr, "time", SimpleNamespace(time=lambda: 1234.5))
    sessions["stale"] = {"client": None, "created_at": 0}
    status, body, rendered = _start(monkeypatch, client)
    assert (status, rendered, body["qrcode_img_content"]) == (200, [scanned], PNG)
    qr.WeixinClient.assert_called_once_with(token="", base_url=qr.ILINK_BASE_URL)
    # the expired entry was pruned, and the new session keeps the live client
    kept = {"client": client, "qrcode": "hex", "created_at": 1234.5}
    assert sessions == {body["session_id"]: kept}
    client.close.assert_not_awaited()


def _no_encoder(data: str) -> str:
    raise RuntimeError("no encoder")


@pytest.mark.parametrize(
    ("reply", "fails", "render", "status", "body"),
    [
        ({"qrcode": "h"}, "connect", None, 502, {"error": "qr_start_failed", "detail": ""}),
        ({"qrcode": "h"}, "get_bot_qrcode", None, 502, {"error": "qr_start_failed", "detail": ""}),
        ({"qrcode": ""}, "", None, 502, {"error": "invalid_qr_response"}),
        ({"qrcode": "h"}, "", _no_encoder, 500, {"error": "qr_render_failed"}),
    ],
)
def test_start_failures_answer_with_their_status_and_close_the_client(
    sessions, monkeypatch, reply, fails, render, status, body
):
    client = _client(reply, fails)
    if "detail" in body:  # the iLink error text, cut to 200 characters
        body = {**body, "detail": f"{fails} refused {LONG}"[:200]}
    assert _start(monkeypatch, client, render)[:2] == (status, body)
    client.close.assert_awaited_once()
    assert sessions == {}


def _poll(sessions: dict[str, dict[str, Any]], client: SimpleNamespace):
    sessions["s1"] = {"client": client, "qrcode": "hex", "created_at": 0}
    return _run(qr.weixin_qr_status, SimpleNamespace(query={"session_id": "s1"}))


@pytest.mark.parametrize("query", [{}, {"session_id": "nope"}])
def test_status_of_an_unknown_session_is_a_404(query):
    request = SimpleNamespace(query=query)
    assert _run(qr.weixin_qr_status, request) == (404, {"error": "unknown_session"})


@pytest.mark.parametrize(
    ("reply", "fails", "reported", "kept"),
    [
        (None, "get_qrcode_status", "pending", True),  # a failed poll reads as still pending
        ({}, "", "pending", True),
        ({"status": "scaned"}, "", "scaned", True),
        ({"status": "expired"}, "", "expired", False),
    ],
)
def test_status_poll_reports_the_state_and_ends_the_session_only_on_expiry(
    sessions, reply, fails, reported, kept
):
    client = _client(reply, fails)
    assert _poll(sessions, client) == (200, {"status": reported})
    client.get_qrcode_status.assert_awaited_once_with("hex")
    assert (client.close.await_count, list(sessions)) == ((0, ["s1"]) if kept else (1, []))


@pytest.mark.parametrize(
    ("creds", "failure", "status", "body"),
    [
        ({"bot_token": "tok"}, None, 502, {"error": "confirmed_without_credentials"}),
        ({"ilink_bot_id": "acct"}, None, 502, {"error": "confirmed_without_credentials"}),
        (
            CREDS,
            EnvStagingRefused(errno.EACCES, "staging is unusable"),
            409,
            {"error": "staging is unusable", "code": "env_staging_refused"},
        ),
        (CREDS, RuntimeError(LONG), 500, {"error": "persist_failed", "detail": LONG[:200]}),
    ],
)
def test_a_confirmed_sign_in_that_cannot_be_saved_is_reported_and_ends_the_session(
    sessions, monkeypatch, creds, failure, status, body
):
    commit = MagicMock(side_effect=failure)
    monkeypatch.setattr(qr, "_stage_weixin_config", lambda **kw: (Path("config.json"), "{}"))
    monkeypatch.setattr(qr, "_commit_credential_and_config", commit)
    client = _client({"status": "confirmed", **creds})
    assert _poll(sessions, client) == (status, body)
    assert commit.called is (failure is not None)  # without both credentials nothing is written
    assert (client.close.await_count, sessions) == (1, {})


def _config(creds: dict[str, str] | None = None, **weixin: Any) -> SimpleNamespace:
    """What ``KiroCrewConfig.load()`` returns: a weixin section and a credential store."""
    section = {"enabled": True, "account_id": "acct", "dm_policy": "open", "token": ""}
    section.update({"allowed_user_ids": [], "session_folder": "", **weixin})
    reader = MagicMock(return_value=creds or {})
    return SimpleNamespace(weixin=SimpleNamespace(**section), load_credentials=reader)


def _get(monkeypatch, cfg: SimpleNamespace, state: Any = None):
    monkeypatch.setattr(qr, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    request = SimpleNamespace(app={} if state is None else {"state": state})
    return _run(qr.weixin_config_get, request)


def test_config_get_reports_the_channel_and_never_the_credential(monkeypatch):
    cfg = _config(
        {qr.CRED_WEIXIN_TOKEN: "tok"},
        dm_policy="allowlist",
        allowed_user_ids=[7, "u-2"],
        session_folder="Team",
    )
    state = SimpleNamespace(weixin_connected=True, weixin_connect_error=LONG)
    assert _get(monkeypatch, cfg, state) == (
        200,
        {
            "connected": True,
            "connect_error": LONG[:120],
            "configured": True,
            "read_only": False,
            "credential_set": True,
            "enabled": True,
            "account_id": "acct",
            "dm_policy": "allowlist",
            "allowed_user_ids": ["7", "u-2"],
            "session_folder": "Team",
        },
    )
    cfg.load_credentials.assert_called_once_with(propagate=False)  # a read must not re-seed environ
    # a remote caller is not refused: the reply is flagged read-only instead
    monkeypatch.setattr(qr, "is_direct_local_request", lambda request: False)
    assert _get(monkeypatch, cfg, state)[1]["read_only"] is True


@pytest.mark.parametrize(
    ("creds", "weixin", "credential_set", "configured"),
    [
        ({}, {"token": "inline"}, True, True),
        ({qr.CRED_WEIXIN_TOKEN: "tok"}, {"enabled": False}, True, False),
        ({qr.CRED_WEIXIN_TOKEN: "tok"}, {"account_id": ""}, True, False),
        ({}, {}, False, False),
    ],
)
def test_config_get_is_configured_only_with_a_credential_an_account_and_the_channel_on(
    monkeypatch, creds, weixin, credential_set, configured
):
    body = _get(monkeypatch, _config(creds, **weixin))[1]
    assert (body["credential_set"], body["configured"]) == (credential_set, configured)
    assert (body["connected"], body["connect_error"]) == (False, "")  # no gateway state yet


def _save(monkeypatch, tmp_path: Path, body: Any, config: str = "{}", state: Any = None):
    """PUT ``body`` over a config.json holding ``config``; return the reply and the doubles."""
    cp = tmp_path / "config.json"
    cp.write_text(config, encoding="utf-8")
    applied, folder = AsyncMock(), AsyncMock()
    monkeypatch.setattr(qr, "config_path", lambda: cp)
    monkeypatch.setattr(qr, "_hot_apply_after_write", applied)
    monkeypatch.setattr(qr, "ensure_channel_folder", folder)
    raises = body if isinstance(body, Exception) else None
    read = AsyncMock(return_value=body, side_effect=raises)
    request = SimpleNamespace(json=read, app={} if state is None else {"state": state})
    status, reply = _run(qr.weixin_config_save, request)
    return SimpleNamespace(status=status, body=reply, path=cp, applied=applied, folder=folder)


@pytest.mark.parametrize(
    ("body", "reply"),
    [
        (ValueError("not json"), {"error": "invalid JSON"}),
        (["a list"], {"error": "body must be an object"}),
        ({"enabled": "yes"}, {"error": "enabled must be a boolean"}),
        ({"dm_policy": "everyone"}, {"error": "invalid dm_policy"}),
        ({"allowed_user_ids": "u-1"}, {"error": "allowed_user_ids must be a list"}),
        ({"session_folder": 5}, {"error": "Folder name must be text", **BAD_FOLDER}),
        ({"session_folder": "a/b"}, {"error": "Folder name cannot contain / or \\", **BAD_FOLDER}),
    ],
)
def test_config_save_rejects_a_bad_body_and_writes_nothing(monkeypatch, tmp_path, body, reply):
    saved = _save(monkeypatch, tmp_path, body, config='{"keep": 1}')
    assert (saved.status, saved.body) == (400, reply)
    assert saved.path.read_text(encoding="utf-8") == '{"keep": 1}'
    saved.applied.assert_not_awaited()


@pytest.mark.parametrize("config", ["{not json", "[1, 2]"])
def test_config_save_refuses_to_overwrite_a_corrupt_config(monkeypatch, tmp_path, config):
    saved = _save(monkeypatch, tmp_path, {"enabled": True}, config=config)
    assert (saved.status, saved.body) == (500, {"error": "config.json is corrupt"})
    assert saved.path.read_text(encoding="utf-8") == config
    saved.applied.assert_not_awaited()


def test_config_save_writes_each_field_and_keeps_the_rest_of_the_file(monkeypatch, tmp_path):
    state = object()
    body = {"enabled": True, "dm_policy": "allowlist", "allowed_user_ids": [" u-1 ", "", 2]}
    body["session_folder"] = " Team "
    config = '{"other": {"a": 1}, "weixin": "garbled"}'  # a non-object section is replaced
    saved = _save(monkeypatch, tmp_path, body, config=config, state=state)
    assert (saved.status, saved.body) == (200, {"ok": True, "restart_required": False})
    written = json.loads(saved.path.read_text(encoding="utf-8"))
    assert written["other"] == {"a": 1}
    assert written["weixin"] == {
        "enabled": True,
        "dm_policy": "allowlist",
        "allowed_user_ids": ["u-1", "2"],
        "session_folder": "Team",
    }
    saved.applied.assert_awaited_once()
    saved.folder.assert_awaited_once_with(state, "weixin", "Team", relabel=True)


@pytest.mark.parametrize(
    ("config", "state", "filed"),
    [
        ('{"weixin": {"session_folder": "Team"}}', object(), True),
        ('{"weixin": {"session_folder": "Team"}}', None, False),  # no gateway state
        ('{"weixin": {"session_folder": 7}}', object(), False),  # a non-text value reads as off
    ],
)
def test_an_unrelated_save_files_the_stored_folder_only_when_it_can(
    monkeypatch, tmp_path, config, state, filed
):
    saved = _save(monkeypatch, tmp_path, {"enabled": True}, config=config, state=state)
    assert saved.status == 200
    if filed:
        saved.folder.assert_awaited_once_with(state, "weixin", "Team", relabel=False)
    else:
        saved.folder.assert_not_awaited()
