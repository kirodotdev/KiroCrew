"""Tests for the WhatsApp channel setup endpoints.

Scope mirrors ``test_weixin_qr.py``: the decisions a reviewer would want pinned,
not the happy path alone.

* The rotating QR code IS a pairing credential. Whoever scans it links THEIR
  phone as a device on the operator's account with full read and send access to
  every chat, so ``whatsapp_qr_status`` withholds ``qr_data_url`` from anything
  that is not a direct-local request while still reporting ``state``.
* ``whatsapp_config_save``, ``whatsapp_qr_start`` and ``whatsapp_unlink`` are
  direct-local only.
* A FAILED logout KEEPS the session store. Deleting it anyway would leave the
  device linked on WhatsApp's side, still receiving and able to send, while
  destroying the only credential that could retry the unlink.
* Every non-2xx body carries a machine-readable ``code``, so the assertions are
  on that field rather than on the prose, which has no i18n catalog path.

``neonize`` is never imported: the client is a fake. Requests run over a real
loopback ``TestClient``, so the gate under test is the production predicate
rather than a stub, and a remote caller is spelled the way a proxy spells it,
a forwarding header on a loopback peer.

``_render_qr`` imports ``segno``, which arrives with the optional ``whatsapp``
extra. The tests that assert on real PNG bytes skip without it; the ones that
assert WHICH rotating code was selected, and the security halves, install a
recording stand-in so they run on a stock install too.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.config.loader as loader
from kiro_crew.dashboard.handlers import whatsapp_setup as mod

#: A forwarding header on a loopback peer is how every remote path (tunnel,
#: reverse proxy) reaches the gateway, and is what the production gate keys on.
REMOTE_HEADERS = {"X-Forwarded-For": "203.0.113.7"}

#: Each rotating code is valid for about 20 seconds, so an elapsed time of 50s
#: selects index 2. Both neighbours are 10s away, which is far more slack than
#: a loaded runner can consume between two statements.
_ELAPSED_INTO_THIRD_CODE = 50.0


class _FakeQrImage:
    """What the stand-in encoder hands back: carries the payload it was given."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def png_data_uri(self, *, scale: int) -> str:
        return f"data:image/png;base64,{self.payload}-{scale}"


class _FakeSegno:
    """Stand-in for the ``segno`` module.

    The encoder is not what these tests are about: what matters is which of the
    rotating codes reaches it, and that a raising encoder is absorbed. Recording
    the payload asserts the first directly, and keeps the QR paths runnable
    without the optional ``whatsapp`` extra installed.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.encoded: list[Any] = []
        self._fail = fail

    def make(self, payload: Any) -> _FakeQrImage:
        self.encoded.append(payload)
        if self._fail:
            raise ValueError("unencodable payload")
        return _FakeQrImage(payload)


class _FakeClient:
    """The WhatsAppClient surface these endpoints touch, and nothing else."""

    def __init__(
        self,
        *,
        state: str = "pairing",
        latest_qr: list[Any] | None = None,
        latest_qr_at: float = 0.0,
        state_detail: str = "",
        is_connected: bool = True,
        db_path: str = "",
        groups: list[dict] | None = None,
        logout_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.latest_qr = latest_qr if latest_qr is not None else []
        self.latest_qr_at = latest_qr_at
        self.state_detail = state_detail
        self.is_connected = is_connected
        self.db_path = db_path
        self._groups = groups if groups is not None else []
        self._logout_error = logout_error
        self.logouts = 0

    async def logout(self) -> None:
        self.logouts += 1
        if self._logout_error is not None:
            raise self._logout_error

    async def list_groups(self) -> list[dict]:
        return self._groups


class _FakeTransport:
    def __init__(self, client: Any) -> None:
        self.client = client


class _FakeState:
    """The gateway-state attributes the endpoints read."""

    def __init__(
        self,
        client: Any = None,
        *,
        connected: bool = False,
        connect_error: str = "",
    ) -> None:
        self.channel_transports = {"whatsapp": _FakeTransport(client)} if client else {}
        self.whatsapp_connected = connected
        self.whatsapp_connect_error = connect_error


@pytest.fixture
def cfg_file(tmp_path: Path, monkeypatch) -> Path:
    """Point every config read and write at a file under tmp_path.

    One namespace covers both paths now: the save endpoint mutates through
    ``update_config_locked``, which resolves the target itself with the loader's
    own ``config_path()`` (and puts its sidecar lock beside it), and
    ``KiroCrewConfig.load`` resolves the same binding.
    """
    cp = tmp_path / "config.json"
    monkeypatch.setattr(loader, "config_path", lambda: cp)
    return cp


@pytest.fixture
def folder_calls(monkeypatch) -> list[dict]:
    """Record ``ensure_channel_folder`` calls instead of building a folder store."""
    calls: list[dict] = []

    async def _record(state: Any, namespace: str, name: str, *, relabel: bool = False) -> str:
        calls.append({"namespace": namespace, "name": name, "relabel": relabel})
        return "folder-id"

    monkeypatch.setattr(mod, "ensure_channel_folder", _record)
    return calls


@pytest.fixture
def fake_segno(monkeypatch):
    """Install a recording stand-in for ``segno`` and hand it back."""

    def _install(*, fail: bool = False) -> _FakeSegno:
        stub = _FakeSegno(fail=fail)
        monkeypatch.setitem(sys.modules, "segno", stub)
        return stub

    return _install


def _serve(state: Any = None) -> web.Application:
    """An app carrying the production route table, plus optional gateway state."""
    app = web.Application()
    if state is not None:
        app["state"] = state
    mod.setup_whatsapp_routes(app)
    return app


def _call(app: web.Application, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    """Issue one request over a real loopback client; return (status, body).

    A loopback peer with no forwarding header is what the production gate reads
    as direct-local, so ``headers=REMOTE_HEADERS`` is the only difference
    between the local and the remote case.
    """

    async def _go() -> tuple[int, Any]:
        async with TestClient(TestServer(app)) as client:
            resp = await client.request(method, path, **kw)
            return resp.status, await resp.json()

    return asyncio.run(_go())


# ── GET /api/whatsapp/config ──────────────────────────────────────────────────


def test_config_get_reports_policy_and_live_state(cfg_file: Path) -> None:
    cfg_file.write_text(
        json.dumps(
            {
                "whatsapp": {
                    "enabled": True,
                    "dm_policy": "allowlist",
                    "allowed_wa_ids": ["15551234567"],
                    "groups": [{"jid": "g1@g.us", "name": "Family", "mode": "mention"}],
                    "session_folder": "WhatsApp",
                }
            }
        ),
        encoding="utf-8",
    )
    state = _FakeState(_FakeClient(state="connected"), connected=True)
    status, body = _call(_serve(state), "GET", "/api/whatsapp/config")
    assert status == 200
    assert body["configured"] is True
    assert body["connected"] is True
    assert body["state"] == "connected"
    assert body["read_only"] is False
    assert body["enabled"] is True
    assert body["dm_policy"] == "allowlist"
    assert body["allowed_wa_ids"] == ["15551234567"]
    # The loader fills a stored rule out to its full shape before the panel sees it.
    assert body["groups"] == [
        {"jid": "g1@g.us", "name": "Family", "mode": "mention", "rules": "", "cooldown_s": 120}
    ]
    assert body["session_folder"] == "WhatsApp"


def test_config_get_marks_a_remote_session_read_only(cfg_file: Path) -> None:
    """The panel renders its read-only view from this flag, so it must flip."""
    status, body = _call(
        _serve(_FakeState()), "GET", "/api/whatsapp/config", headers=REMOTE_HEADERS
    )
    assert status == 200
    assert body["read_only"] is True


def test_config_get_without_a_live_client_reports_unpaired(cfg_file: Path) -> None:
    """No transport means no client, and the channel reads as unpaired, not absent."""
    status, body = _call(_serve(_FakeState()), "GET", "/api/whatsapp/config")
    assert status == 200
    assert body["state"] == "unpaired"
    assert body["configured"] is False
    assert body["connected"] is False


def test_config_get_truncates_a_long_connect_error(cfg_file: Path) -> None:
    """The error is a status line in the panel, so its length is bounded."""
    state = _FakeState(connect_error="x" * 500)
    status, body = _call(_serve(state), "GET", "/api/whatsapp/config")
    assert status == 200
    assert len(body["connect_error"]) == 120


# ── PUT /api/whatsapp/config ──────────────────────────────────────────────────


def test_config_save_denies_a_remote_session(cfg_file: Path) -> None:
    """SECURITY: a config write is direct-local only, and nothing is persisted."""
    status, body = _call(
        _serve(_FakeState()),
        "PUT",
        "/api/whatsapp/config",
        json={"enabled": True, "dm_policy": "open"},
        headers=REMOTE_HEADERS,
    )
    assert status == 403
    assert body["code"] == "remote_read_only"
    assert not cfg_file.exists()


def test_config_save_rejects_invalid_json(cfg_file: Path) -> None:
    status, body = _call(
        _serve(_FakeState()),
        "PUT",
        "/api/whatsapp/config",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert body["code"] == "invalid_json"
    assert not cfg_file.exists()


def test_config_save_rejects_a_non_object_body(cfg_file: Path) -> None:
    status, body = _call(_serve(_FakeState()), "PUT", "/api/whatsapp/config", json=[1, 2, 3])
    assert status == 400
    assert body["code"] == "body_not_object"


def test_config_save_rejects_a_non_bool_enabled(cfg_file: Path) -> None:
    """A truthy string must not switch the channel on by coercion."""
    status, body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"enabled": "yes"}
    )
    assert status == 400
    assert body["code"] == "enabled_not_bool"
    assert not cfg_file.exists()


def test_config_save_rejects_an_unknown_dm_policy(cfg_file: Path) -> None:
    """An unknown policy denies everyone at runtime, so it is refused at the door."""
    status, body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"dm_policy": "everyone"}
    )
    assert status == 400
    assert body["code"] == "invalid_dm_policy"


def test_config_save_accepts_every_documented_dm_policy(cfg_file: Path) -> None:
    for policy in ("self", "allowlist", "open", "disabled"):
        status, body = _call(
            _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"dm_policy": policy}
        )
        assert status == 200, policy
        assert body["ok"] is True
        stored = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert stored["whatsapp"]["dm_policy"] == policy


def test_config_save_rejects_non_list_allowed_wa_ids(cfg_file: Path) -> None:
    status, body = _call(
        _serve(_FakeState()),
        "PUT",
        "/api/whatsapp/config",
        json={"allowed_wa_ids": "15551234567"},
    )
    assert status == 400
    assert body["code"] == "allowed_wa_ids_not_list"


def test_config_save_rejects_non_list_groups(cfg_file: Path) -> None:
    status, body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"groups": {"jid": "g1@g.us"}}
    )
    assert status == 400
    assert body["code"] == "groups_not_list"


def test_config_save_rejects_an_invalid_session_folder(cfg_file: Path) -> None:
    status, body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"session_folder": "a/b"}
    )
    assert status == 400
    assert body["code"] == "invalid_session_folder"
    assert not cfg_file.exists()


def test_config_save_rejects_a_non_text_session_folder(cfg_file: Path) -> None:
    """A number is refused rather than coerced into a folder named 123."""
    status, body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"session_folder": 123}
    )
    assert status == 400
    assert body["code"] == "invalid_session_folder"


def test_config_save_reports_a_corrupt_config(cfg_file: Path) -> None:
    """A malformed config is reported, never replaced by the whatsapp section alone."""
    cfg_file.write_text("{not valid json", encoding="utf-8")
    status, body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"enabled": True}
    )
    assert status == 500
    assert body["code"] == "config_corrupt"
    assert cfg_file.read_text(encoding="utf-8") == "{not valid json"


def test_config_save_reports_a_non_object_config(cfg_file: Path) -> None:
    """Valid JSON that is not an object is corrupt for this purpose too."""
    cfg_file.write_text("[1, 2, 3]", encoding="utf-8")
    status, body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"enabled": True}
    )
    assert status == 500
    assert body["code"] == "config_corrupt"
    assert cfg_file.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_config_save_persists_policy_and_preserves_other_sections(
    cfg_file: Path, folder_calls: list[dict]
) -> None:
    cfg_file.write_text(
        json.dumps({"slack": {"command": "kirocrew"}, "agent": {"model": "auto"}}),
        encoding="utf-8",
    )
    status, body = _call(
        _serve(_FakeState()),
        "PUT",
        "/api/whatsapp/config",
        json={
            "enabled": True,
            "dm_policy": "allowlist",
            "allowed_wa_ids": ["15551234567"],
            "groups": [{"jid": "g1@g.us"}],
        },
    )
    assert status == 200
    assert body == {"ok": True, "restart_required": True}
    stored = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert stored["slack"] == {"command": "kirocrew"}
    assert stored["agent"] == {"model": "auto"}
    assert stored["whatsapp"] == {
        "enabled": True,
        "dm_policy": "allowlist",
        "allowed_wa_ids": ["15551234567"],
        "groups": [{"jid": "g1@g.us"}],
    }
    assert folder_calls == []  # no session_folder configured, so nothing to create


def test_config_save_replaces_a_non_object_whatsapp_section(cfg_file: Path) -> None:
    """A hand-edited scalar section is rebuilt rather than mutated in place."""
    cfg_file.write_text(json.dumps({"whatsapp": "on"}), encoding="utf-8")
    status, _body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"enabled": False}
    )
    assert status == 200
    stored = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert stored["whatsapp"] == {"enabled": False}


def test_config_save_drops_blank_ids_and_non_object_groups(cfg_file: Path) -> None:
    """An empty allow-list entry grants nothing but reads as authoritative."""
    status, _body = _call(
        _serve(_FakeState()),
        "PUT",
        "/api/whatsapp/config",
        json={
            "allowed_wa_ids": ["  15551234567  ", "", "   "],
            "groups": [{"jid": "g1@g.us"}, "g2@g.us", 7],
        },
    )
    assert status == 200
    stored = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert stored["whatsapp"]["allowed_wa_ids"] == ["15551234567"]
    assert stored["whatsapp"]["groups"] == [{"jid": "g1@g.us"}]


def test_config_save_creates_the_session_folder_and_relabels_it(
    cfg_file: Path, folder_calls: list[dict]
) -> None:
    """A save that carries session_folder is the one save allowed to rename."""
    status, _body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"session_folder": "  WhatsApp  "}
    )
    assert status == 200
    stored = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert stored["whatsapp"]["session_folder"] == "WhatsApp"
    assert folder_calls == [{"namespace": "whatsapp", "name": "WhatsApp", "relabel": True}]


def test_config_save_reuses_the_folder_without_relabelling_it(
    cfg_file: Path, folder_calls: list[dict]
) -> None:
    """An unrelated save must not revert a rename the user made in the sidebar."""
    cfg_file.write_text(json.dumps({"whatsapp": {"session_folder": "Chats"}}), encoding="utf-8")
    status, _body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"enabled": True}
    )
    assert status == 200
    assert folder_calls == [{"namespace": "whatsapp", "name": "Chats", "relabel": False}]


def test_config_save_skips_the_folder_when_the_gateway_has_no_state(
    cfg_file: Path, folder_calls: list[dict]
) -> None:
    """Without gateway state there is no folder store, and the save still lands."""
    status, _body = _call(
        _serve(), "PUT", "/api/whatsapp/config", json={"session_folder": "WhatsApp"}
    )
    assert status == 200
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["whatsapp"]["session_folder"] == (
        "WhatsApp"
    )
    assert folder_calls == []


def test_config_save_treats_a_cleared_session_folder_as_off(
    cfg_file: Path, folder_calls: list[dict]
) -> None:
    status, _body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"session_folder": "   "}
    )
    assert status == 200
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["whatsapp"]["session_folder"] == ""
    assert folder_calls == []


# ── POST /api/channels/whatsapp/qr/start ──────────────────────────────────────


def test_qr_start_denies_a_remote_session() -> None:
    """SECURITY: pairing is direct-local only."""
    state = _FakeState(_FakeClient(state="pairing"))
    status, body = _call(
        _serve(state), "POST", "/api/channels/whatsapp/qr/start", headers=REMOTE_HEADERS
    )
    assert status == 403
    assert body["code"] == "local_only"


def test_qr_start_reports_the_channel_is_not_running() -> None:
    status, body = _call(_serve(_FakeState()), "POST", "/api/channels/whatsapp/qr/start")
    assert status == 409
    assert body["code"] == "channel_not_running"


def test_qr_start_reports_the_live_pairing_state() -> None:
    state = _FakeState(_FakeClient(state="pairing"))
    status, body = _call(_serve(state), "POST", "/api/channels/whatsapp/qr/start")
    assert status == 200
    assert body == {"ok": True, "state": "pairing"}


# ── GET /api/channels/whatsapp/qr/status ──────────────────────────────────────


def test_qr_status_reports_disabled_without_a_live_client() -> None:
    status, body = _call(_serve(_FakeState()), "GET", "/api/channels/whatsapp/qr/status")
    assert status == 200
    assert body == {"state": "disabled", "qr_data_url": None, "detail": ""}


def test_qr_status_returns_the_qr_to_a_direct_local_request(fake_segno) -> None:
    """SECURITY, first half: the pairing credential reaches a local operator."""
    stub = fake_segno()
    client = _FakeClient(state="pairing", latest_qr=["code-a"], latest_qr_at=0.0)
    status, body = _call(_serve(_FakeState(client)), "GET", "/api/channels/whatsapp/qr/status")
    assert status == 200
    assert body["state"] == "pairing"
    assert body["qr_data_url"] == "data:image/png;base64,code-a-6"
    assert stub.encoded == ["code-a"]


def test_qr_status_withholds_the_qr_from_a_remote_session(fake_segno) -> None:
    """SECURITY, second half: scanning the code links the scanner's OWN phone as
    a device on the operator's account, so a remote caller never receives it.
    ``state`` still comes back, because a 403 would leave a remote operator
    unable to see that the channel is connected."""
    stub = fake_segno()
    client = _FakeClient(state="pairing", latest_qr=["code-a"], latest_qr_at=0.0)
    status, body = _call(
        _serve(_FakeState(client)),
        "GET",
        "/api/channels/whatsapp/qr/status",
        headers=REMOTE_HEADERS,
    )
    assert status == 200
    assert body["qr_data_url"] is None
    assert body["state"] == "pairing"  # the badge still renders remotely
    assert stub.encoded == []  # the code is never even rendered


def test_qr_status_omits_the_qr_once_paired(fake_segno) -> None:
    """A code outside the pairing state would be a stale credential."""
    stub = fake_segno()
    client = _FakeClient(state="connected", latest_qr=["code-a"], latest_qr_at=0.0)
    status, body = _call(_serve(_FakeState(client)), "GET", "/api/channels/whatsapp/qr/status")
    assert status == 200
    assert body["state"] == "connected"
    assert body["qr_data_url"] is None
    assert stub.encoded == []


def test_qr_status_omits_the_qr_before_the_first_code_arrives(fake_segno) -> None:
    stub = fake_segno()
    client = _FakeClient(state="pairing", latest_qr=[])
    status, body = _call(_serve(_FakeState(client)), "GET", "/api/channels/whatsapp/qr/status")
    assert status == 200
    assert body["qr_data_url"] is None
    assert stub.encoded == []


def test_qr_status_truncates_a_long_detail() -> None:
    client = _FakeClient(state="connecting", state_detail="d" * 500)
    status, body = _call(_serve(_FakeState(client)), "GET", "/api/channels/whatsapp/qr/status")
    assert status == 200
    assert len(body["detail"]) == 200


# ── _render_qr ────────────────────────────────────────────────────────────────


def test_render_qr_returns_a_loadable_png_data_url() -> None:
    """The panel puts this straight in an <img src>, so it must be real PNG bytes."""
    pytest.importorskip("segno", reason="ships with the optional whatsapp extra")
    uri = mod._render_qr(["2@abc,def,ghi"], 0.0)
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"  # exact PNG signature


def test_render_qr_encodes_the_first_code_when_no_timestamp_is_known(fake_segno) -> None:
    stub = fake_segno()
    assert mod._render_qr(["a", "b", "c"], 0.0) == "data:image/png;base64,a-6"
    assert stub.encoded == ["a"]


def test_render_qr_encodes_the_code_valid_now(fake_segno) -> None:
    """Each code lasts about 20s, so 50s in the third one is current."""
    stub = fake_segno()
    emitted_at = time.monotonic() - _ELAPSED_INTO_THIRD_CODE
    assert mod._render_qr(["a", "b", "c", "d", "e"], emitted_at) == "data:image/png;base64,c-6"
    assert stub.encoded == ["c"]


def test_render_qr_clamps_to_the_last_code_once_all_have_expired(fake_segno) -> None:
    """An expired batch renders its last code rather than indexing past the end."""
    stub = fake_segno()
    assert mod._render_qr(["a", "b", "c"], time.monotonic() - 10_000) == "data:image/png;base64,c-6"
    assert stub.encoded == ["c"]


def test_render_qr_returns_none_when_the_encoder_fails(fake_segno) -> None:
    """A render failure degrades to no image, never to a 500 on the poll."""
    stub = fake_segno(fail=True)
    assert mod._render_qr(["a"], 0.0) is None
    assert stub.encoded == ["a"]


# ── POST /api/channels/whatsapp/unlink ────────────────────────────────────────


def test_unlink_denies_a_remote_session(tmp_path: Path) -> None:
    """SECURITY: unlinking is direct-local only, and the store survives."""
    db = tmp_path / "session.db"
    db.write_text("session", encoding="utf-8")
    client = _FakeClient(state="connected", db_path=str(db))
    status, body = _call(
        _serve(_FakeState(client)), "POST", "/api/channels/whatsapp/unlink", headers=REMOTE_HEADERS
    )
    assert status == 403
    assert body["code"] == "local_only"
    assert db.exists()
    assert client.logouts == 0


def test_unlink_reports_the_channel_is_not_running() -> None:
    status, body = _call(_serve(_FakeState()), "POST", "/api/channels/whatsapp/unlink")
    assert status == 409
    assert body["code"] == "channel_not_running"


def test_unlink_keeps_the_session_store_when_logout_fails(tmp_path: Path) -> None:
    """SECURITY: the device is STILL LINKED on WhatsApp's side after a failed
    logout, so the session store is the only credential that can retry the
    unlink. Deleting it would leave that device receiving and able to send while
    the operator had been told the unlink succeeded."""
    db = tmp_path / "session.db"
    db.write_text("session", encoding="utf-8")
    client = _FakeClient(
        state="connected", db_path=str(db), logout_error=RuntimeError("socket closed")
    )
    status, body = _call(_serve(_FakeState(client)), "POST", "/api/channels/whatsapp/unlink")
    assert status == 502
    assert body["code"] == "logout_failed"
    assert db.exists()
    assert db.read_text(encoding="utf-8") == "session"


def test_unlink_deletes_the_session_store_after_a_successful_logout(tmp_path: Path) -> None:
    db = tmp_path / "session.db"
    db.write_text("session", encoding="utf-8")
    client = _FakeClient(state="connected", db_path=str(db))
    status, body = _call(_serve(_FakeState(client)), "POST", "/api/channels/whatsapp/unlink")
    assert status == 200
    assert body == {"ok": True}
    assert not db.exists()
    assert client.logouts == 1


def test_unlink_succeeds_when_the_session_store_is_already_gone(tmp_path: Path) -> None:
    """missing_ok: a store removed by hand is not an unlink failure."""
    client = _FakeClient(state="connected", db_path=str(tmp_path / "absent.db"))
    status, body = _call(_serve(_FakeState(client)), "POST", "/api/channels/whatsapp/unlink")
    assert status == 200
    assert body == {"ok": True}


def test_unlink_reports_a_session_file_it_could_not_remove(tmp_path: Path) -> None:
    """The logout DID succeed here, so the leftover file is stale, not live: the
    call reports ok with a warning the operator can act on."""
    stuck = tmp_path / "session.db"
    stuck.mkdir()  # unlink on a directory raises OSError on every platform
    client = _FakeClient(state="connected", db_path=str(stuck))
    status, body = _call(_serve(_FakeState(client)), "POST", "/api/channels/whatsapp/unlink")
    assert status == 200
    assert body["ok"] is True
    assert body["code"] == "session_file_kept"
    assert body["warning"]
    assert stuck.exists()


# ── GET /api/whatsapp/groups ──────────────────────────────────────────────────


def test_groups_get_is_empty_without_a_live_client() -> None:
    status, body = _call(_serve(_FakeState()), "GET", "/api/whatsapp/groups")
    assert status == 200
    assert body == {"groups": []}


def test_groups_get_is_empty_while_disconnected() -> None:
    """The picker shows nothing rather than a stale list from a dead socket."""
    client = _FakeClient(state="pairing", is_connected=False, groups=[{"jid": "g1@g.us"}])
    status, body = _call(_serve(_FakeState(client)), "GET", "/api/whatsapp/groups")
    assert status == 200
    assert body == {"groups": []}


def test_groups_get_lists_the_joined_groups() -> None:
    groups = [{"jid": "g1@g.us", "name": "Family"}, {"jid": "g2@g.us", "name": "Work"}]
    client = _FakeClient(state="connected", is_connected=True, groups=groups)
    status, body = _call(_serve(_FakeState(client)), "GET", "/api/whatsapp/groups")
    assert status == 200
    assert body == {"groups": groups}


# ── route table ───────────────────────────────────────────────────────────────


def test_setup_registers_every_whatsapp_route() -> None:
    """The panel addresses these paths by hand, so the table is part of the API."""
    app = web.Application()
    mod.setup_whatsapp_routes(app)
    # aiohttp registers a HEAD companion for every GET, which is not part of the
    # API surface the panel addresses.
    registered = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None and route.method != "HEAD"
    }
    assert registered == {
        ("GET", "/api/whatsapp/config"),
        ("PUT", "/api/whatsapp/config"),
        ("GET", "/api/whatsapp/groups"),
        ("POST", "/api/channels/whatsapp/qr/start"),
        ("GET", "/api/channels/whatsapp/qr/status"),
        ("POST", "/api/channels/whatsapp/unlink"),
    }


# ── the save goes through the locked primitive ────────────────────────────────


def test_the_save_preserves_an_owner_only_config_mode(cfg_file: Path) -> None:
    """config.json can carry other channels' inline credentials.

    A replacement written under the process umask would publish them to every
    local user, so the mode of the file that was there has to survive the write.
    """
    if os.name == "nt":
        pytest.skip("the POSIX mode is not the access control on Windows")
    cfg_file.write_text(json.dumps({"whatsapp": {"enabled": False}}), encoding="utf-8")
    os.chmod(cfg_file, 0o600)
    status, _body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"enabled": True}
    )
    assert status == 200
    assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600


def test_the_save_takes_the_sidecar_lock(cfg_file: Path, monkeypatch) -> None:
    """The other settings writers live in other PROCESSES too, so the in-process
    asyncio lock cannot serialize against them; the advisory lock on
    ``<path>.lock`` is what does, and it is held across the read AND the write.
    """
    seen: list[bool] = []
    real = mod.update_config_locked

    def _spy(*a: Any, **kw: Any) -> Any:
        seen.append((cfg_file.parent / (cfg_file.name + ".lock")).exists())
        return real(*a, **kw)

    monkeypatch.setattr(mod, "update_config_locked", _spy)
    status, _body = _call(
        _serve(_FakeState()), "PUT", "/api/whatsapp/config", json={"enabled": True}
    )
    assert status == 200
    assert seen, "the save must go through the locked primitive"


def test_the_unlink_removes_the_wal_and_shm_sidecars(cfg_file: Path, tmp_path: Path) -> None:
    """SQLite keeps recently written pages in the write-ahead log.

    Removing only `session.db` can therefore leave the NEWEST session material on
    disk after an unlink that reported success, which is why the sidecars are
    covered by the same sensitive-path protection and must be covered by the same
    deletion.
    """
    db = tmp_path / "session.db"
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"session.db{suffix}").write_bytes(b"x")
    client = _FakeClient(state="connected", db_path=str(db))
    status, body = _call(_serve(_FakeState(client)), "POST", "/api/channels/whatsapp/unlink")
    assert status == 200 and body.get("ok") is True
    for suffix in ("", "-wal", "-shm"):
        leftover = tmp_path / f"session.db{suffix}"
        assert not leftover.exists(), f"{leftover.name} survived the unlink"


def test_the_session_delete_happens_off_the_event_loop(
    cfg_file: Path, tmp_path: Path, monkeypatch
) -> None:
    """The data home can be a network mount.

    An unlink that stalls there would stall the one gateway loop, and with it every
    session and the liveness heartbeat. Asserted BEHAVIOURALLY rather than by
    reading the source: the recorder asks for a running loop, which only answers on
    the loop thread, so this can only pass if the delete really ran on a worker.
    """
    db = tmp_path / "session.db"
    db.write_bytes(b"x")
    on_loop: list[bool] = []
    real_unlink = Path.unlink

    class _RecordingPath(type(db)):  # type: ignore[misc]
        def unlink(self, missing_ok: bool = False) -> None:
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
            real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(mod, "Path", _RecordingPath)
    client = _FakeClient(state="connected", db_path=str(db))
    status, _body = _call(_serve(_FakeState(client)), "POST", "/api/channels/whatsapp/unlink")
    assert status == 200
    assert on_loop and not any(on_loop), "the session delete must run on a worker thread"
