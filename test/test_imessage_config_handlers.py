"""Tests for the iMessage config API handlers (GET/PUT /api/imessage/config)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod


class _StubRequest:
    """Request double for the save handler: real ``json()``, ``get()``, ``app``."""

    def __init__(self, body: Any) -> None:
        self._body = body
        self.app: dict[str, Any] = {}

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def get(self, key: str, default: Any = None) -> Any:
        return default


def _save(monkeypatch: Any, tmp_path: Path, body: Any) -> tuple[Any, Path]:
    """Drive api_imessage_config_save against an isolated config.json."""
    cfg_path = tmp_path / "config.json"
    # `config_path` is imported at the handler module's scope, so that module holds
    # its own binding: patching only the loader would leave the handler reading the
    # real config.json. Both are patched so indirect callers redirect too.
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    resp = asyncio.run(mod.api_imessage_config_save(_StubRequest(body)))
    return resp, cfg_path


def _section(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.exists():
        return {}
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    section = data.get("imessage")
    return section if isinstance(section, dict) else {}


def _body(resp: Any) -> dict[str, Any]:
    return json.loads(resp.text)


class TestRemoteIsReadOnly:
    def test_save_denies_a_non_loopback_request(self, monkeypatch: Any) -> None:
        # A remote session must not be able to widen who may reach the agent.
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        req = make_mocked_request(
            "PUT",
            "/api/imessage/config",
            payload=b'{"enabled": true, "allowed_handles": ["+15559999999"]}',
            headers={"Content-Type": "application/json"},
        )
        resp = asyncio.run(mod.api_imessage_config_save(req))
        assert resp.status == 403


class TestSave:
    def test_a_valid_payload_is_persisted(self, monkeypatch: Any, tmp_path: Path) -> None:
        resp, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {
                "enabled": True,
                "allowed_handles": ["+1 (555) 123-4567", "me@example.com"],
                "db_path": "/tmp/chat.db",
                "service": "auto",
            },
        )
        assert resp.status == 200
        section = _section(cfg_path)
        assert section["enabled"] is True
        assert section["allowed_handles"] == ["+1 (555) 123-4567", "me@example.com"]
        assert section["db_path"] == "/tmp/chat.db"
        assert section["service"] == "auto"

    def test_boot_read_fields_report_restart_required(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        resp, _ = _save(monkeypatch, tmp_path, {"enabled": True})
        assert _body(resp)["restart_required"] is True

    def test_the_session_folder_alone_reloads_live(self, monkeypatch: Any, tmp_path: Path) -> None:
        resp, _ = _save(monkeypatch, tmp_path, {"session_folder": "iMessage"})
        assert _body(resp)["restart_required"] is False

    def test_a_no_op_save_does_not_claim_a_restart_is_needed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        _save(monkeypatch, tmp_path, {"enabled": True})
        resp, _ = _save(monkeypatch, tmp_path, {"enabled": True})
        assert _body(resp)["restart_required"] is False

    def test_an_absent_field_is_left_alone(self, monkeypatch: Any, tmp_path: Path) -> None:
        _save(monkeypatch, tmp_path, {"db_path": "/custom/chat.db"})
        _save(monkeypatch, tmp_path, {"enabled": True})
        assert _section(tmp_path / "config.json")["db_path"] == "/custom/chat.db"

    def test_no_credential_is_ever_written(self, monkeypatch: Any, tmp_path: Path) -> None:
        # iMessage has no credential at all; a token-shaped key must not create one.
        _save(monkeypatch, tmp_path, {"enabled": True, "bot_token": "planted"})
        assert "bot_token" not in _section(tmp_path / "config.json")
        assert not (tmp_path / ".env").exists()


class TestValidation:
    def test_a_non_object_body_is_rejected(self, monkeypatch: Any, tmp_path: Path) -> None:
        resp, cfg_path = _save(monkeypatch, tmp_path, ["not", "an", "object"])
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_invalid_json_is_rejected(self, monkeypatch: Any, tmp_path: Path) -> None:
        resp, cfg_path = _save(monkeypatch, tmp_path, ValueError("bad json"))
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_enabled_must_be_a_strict_boolean(self, monkeypatch: Any, tmp_path: Path) -> None:
        resp, cfg_path = _save(monkeypatch, tmp_path, {"enabled": "yes"})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_an_unknown_service_is_rejected(self, monkeypatch: Any, tmp_path: Path) -> None:
        # A typo forwarded to the bridge would be refused per send, turning it
        # into a channel that accepts messages and never answers.
        resp, cfg_path = _save(monkeypatch, tmp_path, {"service": "whatsapp"})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_the_three_documented_services_are_accepted(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        for service in ("imessage", "sms", "auto"):
            resp, _ = _save(monkeypatch, tmp_path, {"service": service})
            assert resp.status == 200, service

    def test_a_handle_that_is_neither_phone_nor_email_is_rejected(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        resp, cfg_path = _save(monkeypatch, tmp_path, {"allowed_handles": ["not-a-handle"]})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_a_line_break_in_a_path_is_rejected(self, monkeypatch: Any, tmp_path: Path) -> None:
        # The value is handed to the bridge as an argument, so a newline would
        # corrupt the argument rather than be quoted.
        resp, cfg_path = _save(monkeypatch, tmp_path, {"db_path": "/tmp/a\nb"})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_a_nul_in_a_path_is_rejected(self, monkeypatch: Any, tmp_path: Path) -> None:
        resp, cfg_path = _save(monkeypatch, tmp_path, {"db_path": "/tmp/a\x00b"})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_a_non_string_path_is_rejected(self, monkeypatch: Any, tmp_path: Path) -> None:
        resp, cfg_path = _save(monkeypatch, tmp_path, {"db_path": 42})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_a_corrupt_config_file_fails_without_clobbering_it(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
        resp = asyncio.run(mod.api_imessage_config_save(_StubRequest({"enabled": True})))
        assert resp.status == 500
        assert cfg_path.read_text(encoding="utf-8") == "{not json"


class TestHandleShape:
    def test_phone_and_email_shapes_are_accepted(self) -> None:
        for good in ("+15551234567", "5551234567", "+1 (555) 123-4567", "me@example.com"):
            assert mod._is_valid_imessage_handle(good), good

    def test_junk_is_rejected(self) -> None:
        for bad in ("", "   ", "not-a-handle", "12", "@example.com", "a b@c.com", "x" * 300):
            assert not mod._is_valid_imessage_handle(bad), bad

    def test_a_tab_or_newline_is_rejected_even_in_a_phone(self) -> None:
        for bad in ("+1555\t1234", "+1555\n1234"):
            assert not mod._is_valid_imessage_handle(bad), bad

    def test_a_letter_cannot_masquerade_as_a_phone_number(self) -> None:
        # Otherwise an arbitrary identifier could be smuggled onto the allowlist.
        assert not mod._is_valid_imessage_handle("+1555abc4567")


class TestGet:
    def test_the_payload_carries_no_credential_fields(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"imessage": {"enabled": True, "allowed_handles": ["+15551234567"]}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
        req = make_mocked_request("GET", "/api/imessage/config")
        req.app["state"] = type("S", (), {})()
        body = _body(asyncio.run(mod.api_imessage_config_get(req)))
        assert body["enabled"] is True
        assert body["allowed_handles"] == ["+15551234567"]
        assert body["configured"] is True
        assert "supported" in body
        for key in body:
            assert "token" not in key
            assert "secret" not in key

    def test_an_empty_allowlist_is_not_configured(self, monkeypatch: Any, tmp_path: Path) -> None:
        # The transport fails closed on an empty list, so "enabled" alone is not
        # a working channel and the badge must not claim it is.
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"imessage": {"enabled": True}}), encoding="utf-8")
        monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "config_path", lambda: cfg_path)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
        req = make_mocked_request("GET", "/api/imessage/config")
        req.app["state"] = type("S", (), {})()
        assert _body(asyncio.run(mod.api_imessage_config_get(req)))["configured"] is False
