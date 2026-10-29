"""Tests for the Signal config API handlers (GET/PUT /api/signal/config)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.signal_settings as mod
from kiro_crew.dashboard import handlers
from kiro_crew.signal_cli import bridge_path


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


class _Audit:
    """Collects ``log_api_access`` calls instead of writing the real SEL."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def audit(monkeypatch: Any) -> _Audit:
    rec = _Audit()
    monkeypatch.setattr(handlers, "sel", lambda: rec)
    return rec


@pytest.fixture
def cfg_path(monkeypatch: Any, tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    # `config_path` is imported at the handler module's scope, so that module holds
    # its own binding: patching only the loader would leave the handler writing the
    # real config.json. Both are patched so the GET's loader read follows too.
    monkeypatch.setattr(loader, "config_path", lambda: path)
    monkeypatch.setattr(mod, "config_path", lambda: path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _no_watcher() -> None:
        return None

    monkeypatch.setattr(mod, "_hot_apply_after_write", _no_watcher)
    return path


def _save(body: Any) -> Any:
    return asyncio.run(mod.api_signal_config_save(_StubRequest(body)))


def _section(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.exists():
        return {}
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    section = data.get("signal")
    return section if isinstance(section, dict) else {}


def _body(resp: Any) -> dict[str, Any]:
    return json.loads(resp.text)


def _get(state: Any = None) -> dict[str, Any]:
    req = make_mocked_request("GET", "/api/signal/config")
    req.app["state"] = state if state is not None else type("S", (), {})()
    return _body(asyncio.run(mod.api_signal_config_get(req)))


class TestRegistration:
    def test_the_routes_reach_the_handlers(self) -> None:
        from aiohttp import web

        from kiro_crew.dashboard.routes import messaging as routes

        app = web.Application()
        routes.register(app)
        table = {
            (r.method, r.resource.canonical): r.handler
            for r in app.router.routes()
            if r.resource is not None
        }
        assert table[("GET", "/api/signal/config")] is handlers.api_signal_config_get
        assert table[("PUT", "/api/signal/config")] is handlers.api_signal_config_save


class TestRemoteIsReadOnly:
    def test_save_denies_a_non_loopback_request(self, monkeypatch: Any, audit: _Audit) -> None:
        # A remote session must not be able to widen who may reach the agent.
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        req = make_mocked_request(
            "PUT",
            "/api/signal/config",
            payload=b'{"enabled": true, "allowed_numbers": ["+15550109991"]}',
            headers={"Content-Type": "application/json"},
        )
        resp = asyncio.run(mod.api_signal_config_save(req))
        assert resp.status == 403
        assert _body(resp)["code"] == "remote_read_only"
        assert audit.calls[-1]["outcome"] == "denied"

    def test_get_reports_read_only_to_a_remote_session(
        self, monkeypatch: Any, cfg_path: Path
    ) -> None:
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        assert _get()["read_only"] is True


class TestSave:
    def test_a_valid_payload_is_persisted(self, cfg_path: Path, audit: _Audit) -> None:
        resp = _save(
            {
                "enabled": True,
                "account": "+447700900123",
                "allowed_numbers": ["+447700900123", "+15550101234"],
                "session_folder": "Signal",
            }
        )
        assert resp.status == 200, resp.text
        section = _section(cfg_path)
        assert section["enabled"] is True
        assert section["account"] == "+447700900123"
        assert section["allowed_numbers"] == ["+447700900123", "+15550101234"]
        assert section["session_folder"] == "Signal"
        assert audit.calls[-1]["operation"] == "signal.config.update"
        assert audit.calls[-1]["outcome"] == "ok"

    def test_the_leading_plus_survives_the_round_trip(self, cfg_path: Path) -> None:
        """Stored as a JSON STRING, so no parser can read it back as an integer."""
        _save({"account": "+447700900123", "allowed_numbers": ["+15550101234"]})
        raw = cfg_path.read_text(encoding="utf-8")
        assert '"account": "+447700900123"' in raw
        assert '"+15550101234"' in raw
        body = _get()
        assert body["account"] == "+447700900123"
        assert body["allowed_numbers"] == ["+15550101234"]

    def test_formatting_is_canonicalized_and_keeps_the_plus(self, cfg_path: Path) -> None:
        # The stored form is the one the transport compares, so a typed space
        # or a 00 prefix cannot leave the owner un-authorized.
        _save({"account": "+44 7700-900123", "allowed_numbers": ["0044 7700 900123"]})
        section = _section(cfg_path)
        assert section["account"] == "+447700900123"
        assert section["allowed_numbers"] == ["+447700900123"]

    def test_a_json_integer_gains_its_plus_rather_than_losing_it(self, cfg_path: Path) -> None:
        _save({"allowed_numbers": [447700900001]})
        assert _section(cfg_path)["allowed_numbers"] == ["+447700900001"]

    def test_an_aci_is_allowed_on_the_list_and_lowercased(self, cfg_path: Path) -> None:
        aci = "A1B2C3D4-0000-4000-8000-ABCDEFABCDEF"
        _save({"allowed_numbers": [aci]})
        assert _section(cfg_path)["allowed_numbers"] == [aci.lower()]

    def test_blanks_and_duplicates_are_dropped_in_order(self, cfg_path: Path) -> None:
        _save({"allowed_numbers": ["+15550101234", "  ", "+1 555 010 1234", "+15550100101"]})
        assert _section(cfg_path)["allowed_numbers"] == ["+15550101234", "+15550100101"]

    def test_an_empty_account_clears_it(self, cfg_path: Path) -> None:
        _save({"account": "+447700900123"})
        _save({"account": ""})
        assert _section(cfg_path)["account"] == ""

    def test_boot_keys_restart_the_channel_not_the_gateway(self, cfg_path: Path) -> None:
        """``enabled`` and ``account`` are applied by restarting the CHANNEL in
        process (the registry's ``boot_keys``), so no gateway restart is claimed."""
        resp = _save({"enabled": True, "account": "+447700900123"})
        assert _body(resp)["restart_required"] is False

    def test_the_allow_list_and_folder_reload_live(self, cfg_path: Path) -> None:
        resp = _save({"allowed_numbers": ["+15550101234"], "session_folder": "Signal"})
        assert _body(resp)["restart_required"] is False

    def test_a_no_op_save_does_not_rewrite_the_file(self, cfg_path: Path, audit: _Audit) -> None:
        _save({"enabled": True})
        assert audit.calls[-1]["resources"] == "enabled"
        written = cfg_path.read_text(encoding="utf-8")
        resp = _save({"enabled": True})
        assert _body(resp)["restart_required"] is False
        assert cfg_path.read_text(encoding="utf-8") == written
        assert audit.calls[-1]["resources"] == ""

    def test_an_absent_field_is_left_alone(self, cfg_path: Path) -> None:
        _save({"account": "+447700900123"})
        _save({"enabled": True})
        assert _section(cfg_path)["account"] == "+447700900123"

    def test_other_sections_are_preserved(self, cfg_path: Path) -> None:
        cfg_path.write_text(
            json.dumps({"imessage": {"enabled": True}, "signal": {"soft_threshold_pct": 70}}),
            encoding="utf-8",
        )
        _save({"enabled": True})
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["imessage"] == {"enabled": True}
        assert data["signal"]["soft_threshold_pct"] == 70
        assert data["signal"]["enabled"] is True

    def test_no_credential_is_ever_written(self, cfg_path: Path) -> None:
        _save({"enabled": True})
        assert not (cfg_path.parent / ".env").exists()


class TestWarnings:
    def test_enabled_without_an_account_warns(self, cfg_path: Path) -> None:
        resp = _save({"enabled": True, "allowed_numbers": ["+15550101234"]})
        assert resp.status == 200
        assert "no account" in _body(resp)["verify_warning"]

    def test_enabled_with_an_empty_allow_list_warns(self, cfg_path: Path) -> None:
        resp = _save({"enabled": True, "account": "+447700900123", "allowed_numbers": []})
        assert "answer nobody" in _body(resp)["verify_warning"]

    def test_a_complete_channel_carries_no_warning(self, cfg_path: Path) -> None:
        resp = _save(
            {"enabled": True, "account": "+447700900123", "allowed_numbers": ["+447700900123"]}
        )
        assert _body(resp)["verify_warning"] == ""


class TestValidation:
    @pytest.mark.parametrize(
        "key", ["cli_path", "bridge_path", "binary", "signal_cli", "KIROCREW_SIGNAL_CLI"]
    )
    def test_a_binary_path_key_is_refused(self, cfg_path: Path, audit: _Audit, key: str) -> None:
        """The executable is never a setting: config.json is agent-writable."""
        resp = _save({"enabled": True, key: "/tmp/payload"})
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_field"
        assert "KIROCREW_SIGNAL_CLI" in _body(resp)["error"]
        # Refused whole: not even the valid field beside it is written.
        assert not cfg_path.exists()
        assert audit.calls[-1]["outcome"] == "denied"

    def test_a_threshold_is_not_editable_here(self, cfg_path: Path) -> None:
        resp = _save({"soft_threshold_pct": 50})
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_field"

    def test_a_non_object_body_is_rejected(self, cfg_path: Path) -> None:
        resp = _save(["not", "an", "object"])
        assert resp.status == 400
        assert _body(resp)["code"] == "body_not_object"
        assert not cfg_path.exists()

    def test_invalid_json_is_rejected(self, cfg_path: Path) -> None:
        resp = _save(ValueError("bad json"))
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_json"
        assert not cfg_path.exists()

    def test_enabled_must_be_a_strict_boolean(self, cfg_path: Path) -> None:
        resp = _save({"enabled": "yes"})
        assert resp.status == 400
        assert not cfg_path.exists()

    @pytest.mark.parametrize(
        "bad", ["447700900123", "+12345", "+4477009001234567", "not-a-number", True, 1.5, None]
    )
    def test_a_malformed_allow_list_entry_is_rejected(self, cfg_path: Path, bad: Any) -> None:
        # signal-cli always reports +E.164, so a number stored without its +
        # would never match: refuse it instead of storing a silent deny.
        resp = _save({"allowed_numbers": [bad]})
        assert resp.status == 400, bad
        assert _body(resp)["code"] == "invalid_number"
        assert not cfg_path.exists()

    def test_the_rejection_names_the_entry_without_echoing_it(
        self, cfg_path: Path, audit: _Audit
    ) -> None:
        # A number is personal data, and the denial text reaches the SEL too.
        resp = _save({"allowed_numbers": ["+15550101234", "447700900456"]})
        error = _body(resp)["error"]
        assert "allowed number 2" in error
        assert "3361" not in error
        assert "3361" not in audit.calls[-1]["error"]

    def test_allowed_numbers_must_be_a_list(self, cfg_path: Path) -> None:
        resp = _save({"allowed_numbers": "+15550101234"})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_an_oversized_allow_list_is_rejected(self, cfg_path: Path) -> None:
        numbers = [f"+1555{i:07d}" for i in range(mod.MAX_ALLOWED_NUMBERS + 1)]
        resp = _save({"allowed_numbers": numbers})
        assert resp.status == 400
        assert not cfg_path.exists()

    @pytest.mark.parametrize("bad", ["447700900123", "+12", 42.0, ["+15550101234"]])
    def test_a_malformed_account_is_rejected(self, cfg_path: Path, bad: Any) -> None:
        resp = _save({"account": bad})
        assert resp.status == 400, bad
        assert _body(resp)["code"] == "invalid_account"
        assert not cfg_path.exists()

    def test_the_account_must_be_a_number_not_an_aci(self, cfg_path: Path) -> None:
        resp = _save({"account": "a1b2c3d4-0000-4000-8000-abcdefabcdef"})
        assert resp.status == 400
        assert "phone number" in _body(resp)["error"]

    def test_an_invalid_session_folder_is_rejected(self, cfg_path: Path) -> None:
        resp = _save({"session_folder": 123})
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_session_folder"

    def test_a_corrupt_config_file_fails_without_clobbering_it(self, cfg_path: Path) -> None:
        cfg_path.write_text("{not json", encoding="utf-8")
        resp = _save({"enabled": True})
        assert resp.status == 500
        assert _body(resp)["code"] == "config_corrupt"
        assert cfg_path.read_text(encoding="utf-8") == "{not json"


class TestGet:
    def test_the_payload_carries_no_credential_or_path_fields(
        self, monkeypatch: Any, cfg_path: Path
    ) -> None:
        cfg_path.write_text(
            json.dumps(
                {
                    "signal": {
                        "enabled": True,
                        "account": "+447700900123",
                        "allowed_numbers": ["+447700900123"],
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(bridge_path, "resolve_bridge_path", lambda: "/usr/bin/signal-cli")
        body = _get()
        assert body["enabled"] is True
        assert body["account"] == "+447700900123"
        assert body["allowed_numbers"] == ["+447700900123"]
        assert body["configured"] is True
        assert body["cli_found"] is True
        # Whether a binary resolved, never where.
        assert "/usr/bin/signal-cli" not in json.dumps(body)
        for key in body:
            assert "token" not in key
            assert "secret" not in key
            assert "path" not in key

    def test_status_comes_from_the_dashboard_state(self, cfg_path: Path) -> None:
        state = type("S", (), {"signal_connected": False, "signal_connect_error": "x" * 300})()
        body = _get(state)
        assert body["connected"] is False
        assert body["connect_error"] == "x" * 120
        live = type("S", (), {"signal_connected": True, "signal_connect_error": ""})()
        assert _get(live)["connected"] is True

    def test_a_missing_binary_is_reported(self, monkeypatch: Any, cfg_path: Path) -> None:
        monkeypatch.setattr(bridge_path, "resolve_bridge_path", lambda: "")
        assert _get()["cli_found"] is False

    def test_an_integer_entry_in_config_reads_back_with_its_plus(self, cfg_path: Path) -> None:
        # A hand-edited, unquoted number is a JSON integer; the panel shows the
        # +E.164 the transport matches, so a save from it stores the string.
        cfg_path.write_text(
            json.dumps({"signal": {"allowed_numbers": [15550101234]}}), encoding="utf-8"
        )
        assert _get()["allowed_numbers"] == ["+15550101234"]

    @pytest.mark.parametrize(
        "section",
        [
            {"enabled": True, "allowed_numbers": ["+15550101234"]},
            {"enabled": True, "account": "+447700900123"},
            {"account": "+447700900123", "allowed_numbers": ["+15550101234"]},
        ],
    )
    def test_configured_needs_enabled_account_and_allow_list(
        self, cfg_path: Path, section: dict[str, Any]
    ) -> None:
        cfg_path.write_text(json.dumps({"signal": section}), encoding="utf-8")
        assert _get()["configured"] is False


_BOT = "+447700900123"
_OWNER = "+447700900456"
_FRIEND = "+447700900789"
_ACI = "1b2c3d4e-0000-4000-8000-000000000001"

#: (account, allowed_numbers, operator_mode) -- every case of the rule, with
#: formatting the canonical comparison must see through.
_OPERATOR_CASES = [
    (_BOT, [], "none"),
    (_BOT, [_OWNER], "sole_entry"),
    (_BOT, [_OWNER, _FRIEND], "none"),
    (_BOT, [_OWNER, "+44 7700 900-123"], "account"),
    (_BOT, [_OWNER, "0044.7700.900123"], "account"),
    (_BOT, [_BOT], "account"),
    ("", [_OWNER, _FRIEND], "none"),
    ("", [_OWNER], "sole_entry"),
    # The same number twice is one entry, so it is still the sole entry.
    (_BOT, [_OWNER, "+44 7700 900456"], "sole_entry"),
]


class TestOperatorMode:
    """The GET reports who the operator is, by the rule the transport applies."""

    @pytest.mark.parametrize(("account", "allowed", "expected"), _OPERATOR_CASES)
    def test_the_get_reports_the_operator_mode(
        self, cfg_path: Path, account: str, allowed: list[str], expected: str
    ) -> None:
        cfg_path.write_text(
            json.dumps({"signal": {"account": account, "allowed_numbers": allowed}}),
            encoding="utf-8",
        )
        assert _get()["operator_mode"] == expected

    @pytest.mark.parametrize(("account", "allowed", "expected"), _OPERATOR_CASES)
    def test_the_transport_applies_the_same_rule(
        self, account: str, allowed: list[str], expected: str
    ) -> None:
        from kiro_crew.signal_cli.client import SignalClient, account_identifier
        from kiro_crew.signal_cli.transport import SignalTransport, allowed_numbers_from_config

        client = SignalClient(account_identifier(account))
        transport = SignalTransport(client, allowed_numbers=allowed_numbers_from_config(allowed))
        assert transport.operator_mode() == expected
        operators = {n for n in (_BOT, _OWNER, _FRIEND) if transport.is_operator(n)}
        if expected == "account":
            assert operators == {_BOT}
        elif expected == "sole_entry":
            assert operators == {_OWNER}
        else:
            assert operators == set()

    def test_an_aci_the_running_client_learned_counts_as_the_account(self, cfg_path: Path) -> None:
        cfg_path.write_text(
            json.dumps({"signal": {"account": _BOT, "allowed_numbers": [_OWNER, _ACI]}}),
            encoding="utf-8",
        )
        assert _get()["operator_mode"] == "none"

        class _Client:
            def is_self(self, ident: str) -> bool:
                return ident in (_BOT, _ACI)

            def self_ids(self) -> frozenset[str]:
                return frozenset({_BOT, _ACI})

        transport = type("T", (), {"client": _Client()})()
        state = type("S", (), {"channel_transports": {"signal": transport}})()
        assert _get(state)["operator_mode"] == "account"

    def test_a_running_client_for_another_account_is_not_consulted(self, cfg_path: Path) -> None:
        cfg_path.write_text(
            json.dumps({"signal": {"account": _BOT, "allowed_numbers": [_OWNER, _ACI]}}),
            encoding="utf-8",
        )

        class _OldClient:
            def is_self(self, ident: str) -> bool:
                return ident in (_FRIEND, _ACI)

            def self_ids(self) -> frozenset[str]:
                return frozenset({_FRIEND, _ACI})

        transport = type("T", (), {"client": _OldClient()})()
        state = type("S", (), {"channel_transports": {"signal": transport}})()
        assert _get(state)["operator_mode"] == "none"


class TestHowASaveIsApplied:
    """What the watcher does with each field the panel writes.

    A save never asks for a gateway restart; the registry decides per field
    whether the running channel is reconfigured or restarted in process.
    """

    def _descriptor(self) -> Any:
        from kiro_crew.channels import builtin_channel_descriptors

        return next(d for d in builtin_channel_descriptors() if d.channel_type == "signal")

    def test_enabled_and_account_restart_the_channel(self) -> None:
        assert {"enabled", "account"} <= set(self._descriptor().boot_keys)

    def test_the_allow_list_is_applied_live(self) -> None:
        # Not a boot key: SignalTransport.reconfigure adopts it on reload.
        assert "allowed_numbers" not in self._descriptor().boot_keys
        assert "session_folder" not in self._descriptor().boot_keys

    @pytest.mark.parametrize("field", sorted(mod.SAVE_FIELDS))
    def test_no_saved_field_needs_a_gateway_restart(self, field: str) -> None:
        from kiro_crew.config.schema import requires_restart

        assert requires_restart(f"signal.{field}") is False
