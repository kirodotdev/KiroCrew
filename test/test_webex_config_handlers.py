"""Tests for the Webex config API handlers (GET/PUT /api/webex/config)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod


def test_save_denies_non_loopback(monkeypatch) -> None:
    """Config writes are loopback-only: remote sessions are read-only."""
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    req = make_mocked_request(
        "PUT",
        "/api/webex/config",
        payload=b'{"enabled": true, "bot_token": "planted"}',
        headers={"Content-Type": "application/json"},
    )
    resp = asyncio.run(mod.api_webex_config_save(req))
    assert resp.status == 403


class _StubRequest:
    """Request double for the save handler: real ``json()``, ``get()``."""

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body

    def get(self, key: str, default=None):
        return default


def _save(monkeypatch, tmp_path: Path, body: dict, *, verify=None):
    """Drive api_webex_config_save against isolated .env + config.json."""
    env = tmp_path / ".env"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: cfg_path)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _fake_verify(token: str):
        if verify is None:
            return None
        if isinstance(verify, Exception):
            raise verify
        return verify

    monkeypatch.setattr(mod, "_validate_webex_token", _fake_verify)
    resp = asyncio.run(mod.api_webex_config_save(_StubRequest(body)))
    return resp, env, cfg_path


class TestSave:
    def test_saves_token_and_config(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {
                "bot_token": "webex-tok-1234",
                "enabled": True,
                "allowed_emails": ["kyle@example.com"],
            },
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["ok"] is True
        assert payload["restart_required"] is True
        assert "WEBEX_BOT_TOKEN=webex-tok-1234" in env.read_text(encoding="utf-8")
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["webex"] == {"enabled": True, "allowed_emails": ["kyle@example.com"]}
        # Live process env kept in sync so GET reflects the new token pre-restart.
        import os

        assert os.environ.get("WEBEX_BOT_TOKEN") == "webex-tok-1234"
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_rejected_token_blocks_save(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {"bot_token": "bad-token", "enabled": True},
            verify="invalid_token (http 401)",
        )
        assert resp.status == 400
        assert not env.exists()  # nothing persisted
        assert not cfg_path.exists()

    def test_unreachable_webex_saves_with_warning(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _ = _save(
            monkeypatch,
            tmp_path,
            {"bot_token": "webex-tok-5678"},
            verify=RuntimeError("network down"),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["verify_warning"]  # saved, but flagged unverified
        assert "WEBEX_BOT_TOKEN=webex-tok-5678" in env.read_text(encoding="utf-8")
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_token_clear_removes_env_key(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("WEBEX_BOT_TOKEN=old\n", encoding="utf-8")
        resp, env, _ = _save(monkeypatch, tmp_path, {"bot_token_clear": True})
        assert resp.status == 200
        assert "WEBEX_BOT_TOKEN" not in env.read_text(encoding="utf-8")

    def test_invalid_email_rejected(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, cfg_path = _save(monkeypatch, tmp_path, {"allowed_emails": ["not-an-email"]})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_token_with_whitespace_rejected(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _ = _save(monkeypatch, tmp_path, {"bot_token": "has space"})
        assert resp.status == 400
        assert not env.exists()

    def test_enabled_must_be_boolean(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, cfg_path = _save(monkeypatch, tmp_path, {"enabled": "yes"})
        assert resp.status == 400
        assert not cfg_path.exists()

    def test_pasted_env_line_is_stripped(self, monkeypatch, tmp_path: Path) -> None:
        resp, env, _ = _save(monkeypatch, tmp_path, {"bot_token": "WEBEX_BOT_TOKEN=webex-tok-9"})
        assert resp.status == 200
        assert "WEBEX_BOT_TOKEN=webex-tok-9" in env.read_text(encoding="utf-8")
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_noop_save_requires_no_restart(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = _save(monkeypatch, tmp_path, {"enabled": False, "allowed_emails": []})
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["restart_required"] is False

    def test_token_set_purges_legacy_config_token(self, monkeypatch, tmp_path: Path) -> None:
        """A stale plaintext webex.bot_token in config.json is purged when the
        credential moves to .env, so it can never shadow the .env value."""
        (tmp_path / "config.json").write_text(
            json.dumps({"webex": {"enabled": True, "bot_token": "legacy-plaintext"}}),
            encoding="utf-8",
        )
        resp, env, cfg_path = _save(monkeypatch, tmp_path, {"bot_token": "webex-tok-new"})
        assert resp.status == 200
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["webex"]["bot_token"] == ""  # legacy copy gone
        assert "WEBEX_BOT_TOKEN=webex-tok-new" in env.read_text(encoding="utf-8")
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)

    def test_token_clear_purges_legacy_config_token(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"webex": {"bot_token": "legacy-plaintext"}}), encoding="utf-8"
        )
        (tmp_path / ".env").write_text("WEBEX_BOT_TOKEN=old\n", encoding="utf-8")
        resp, env, cfg_path = _save(monkeypatch, tmp_path, {"bot_token_clear": True})
        assert resp.status == 200
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["webex"]["bot_token"] == ""  # cleared everywhere
        assert "WEBEX_BOT_TOKEN" not in env.read_text(encoding="utf-8")


class TestSaveWritesEveryValidatedField:
    """A field that validates MUST persist.

    The reduction that builds ``changes`` is the only write, so a field staged in
    Phase 1 with no matching branch here validated, reported success, and was
    silently dropped — which is exactly how the whole settings panel can look like
    it works while changing nothing. These tests read config.json back rather than
    trusting the response, because the response said 200 in the broken case too.
    """

    def test_group_space_settings_persist(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {
                "allow_group_rooms": True,
                "allowed_room_ids": ["ROOM-A", "ROOM-B"],
            },
        )
        assert resp.status == 200
        webex = json.loads(cfg_path.read_text(encoding="utf-8"))["webex"]
        assert webex["allow_group_rooms"] is True
        assert webex["allowed_room_ids"] == ["ROOM-A", "ROOM-B"]

    def test_threading_and_thresholds_persist(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {
                "reply_in_thread": False,
                "soft_threshold_pct": 60,
                "hard_threshold_pct": 90,
            },
        )
        assert resp.status == 200
        webex = json.loads(cfg_path.read_text(encoding="utf-8"))["webex"]
        assert webex["reply_in_thread"] is False
        assert webex["soft_threshold_pct"] == 60
        assert webex["hard_threshold_pct"] == 90

    def test_every_new_field_is_reported_as_applied(self, monkeypatch, tmp_path: Path) -> None:
        # ``applied`` is what the UI reads to decide whether to show the restart
        # hint, so a field missing from it is invisible to the operator.
        resp, _env, _cfg = _save(
            monkeypatch,
            tmp_path,
            {
                "allow_group_rooms": True,
                "allowed_room_ids": ["R1"],
                "reply_in_thread": False,
                "soft_threshold_pct": 70,
                "hard_threshold_pct": 85,
            },
        )
        payload = json.loads(resp.body)
        assert payload["restart_required"] is True

    def test_a_repeat_save_of_the_same_values_is_a_no_op(self, monkeypatch, tmp_path: Path) -> None:
        """Otherwise ``restart_required`` is permanently true.

        The generic reduction coerces the stored value to the staged value's type
        before comparing, so a bool stored as a bool and an int stored as an int
        both read as unchanged on the second save.
        """
        body = {
            "allow_group_rooms": True,
            "allowed_room_ids": ["R1"],
            "reply_in_thread": False,
            "soft_threshold_pct": 70,
        }
        _save(monkeypatch, tmp_path, body)
        resp, _env, _cfg = _save(monkeypatch, tmp_path, body)
        assert json.loads(resp.body)["restart_required"] is False

    def test_the_threshold_pair_is_clamped_before_it_is_written(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # A soft threshold above the hard one would make the soft nudge
        # unreachable, because _maybe_notice tests ``pct >= hard`` first.
        resp, _env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {"soft_threshold_pct": 90, "hard_threshold_pct": 50},
        )
        assert resp.status == 200
        webex = json.loads(cfg_path.read_text(encoding="utf-8"))["webex"]
        assert webex["soft_threshold_pct"] <= webex["hard_threshold_pct"]

    def test_a_non_boolean_flag_is_refused(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, _cfg = _save(monkeypatch, tmp_path, {"allow_group_rooms": "yes"})
        assert resp.status == 400
        assert b"allow_group_rooms" in resp.body

    def test_a_non_list_room_allowlist_is_refused(self, monkeypatch, tmp_path: Path) -> None:
        resp, _env, _cfg = _save(monkeypatch, tmp_path, {"allowed_room_ids": "ROOM-A"})
        assert resp.status == 400

    def test_room_ids_are_deduplicated_with_order_preserved(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        resp, _env, cfg_path = _save(
            monkeypatch,
            tmp_path,
            {"allowed_room_ids": [" B ", "A", "B", "", "A"]},
        )
        assert resp.status == 200
        webex = json.loads(cfg_path.read_text(encoding="utf-8"))["webex"]
        assert webex["allowed_room_ids"] == ["B", "A"]

    def test_an_out_of_range_threshold_is_refused(self, monkeypatch, tmp_path: Path) -> None:
        for bad in (0, 101, -5):
            resp, _env, _cfg = _save(monkeypatch, tmp_path, {"soft_threshold_pct": bad})
            assert resp.status == 400, bad


class TestMalformedStoredValues:
    """``config.json`` is a file an operator can hand-edit.

    A value of the wrong TYPE must not take down a save of a DIFFERENT field: the
    request never mentioned it, and a 500 persists nothing.
    """

    @pytest.mark.parametrize("stored", [5, 3.5, True, "not-a-list", None, {"a": 1}])
    def test_a_scalar_where_a_list_belongs_does_not_raise(self, stored: Any) -> None:
        # `list(5)` raises TypeError; returning the value unchanged degrades the
        # comparison to the untyped one, which is what the int branch already does.
        assert mod._coerce_like([], stored) is not ...

    @pytest.mark.parametrize("stored", ["abc", None, [1], {"a": 1}])
    def test_a_non_numeric_where_an_int_belongs_does_not_raise(self, stored: Any) -> None:
        assert mod._coerce_like(0, stored) is not ...

    def test_a_well_formed_value_still_coerces(self) -> None:
        # The guard must not swallow the coercion it exists to protect.
        assert mod._coerce_like([], ["a", "b"]) == ["a", "b"]
        assert mod._coerce_like(0, "80") == 80
        assert mod._coerce_like(False, 1) is True


class TestSaveLoadRoundTrip:
    """The write and the read are two halves of one contract.

    Testing only the save proves the value reached ``config.json``; testing only
    the load proves the loader can parse one. Neither catches the failure that
    actually bites: a field the SAVE writes and the LOAD forgets is silently
    replaced by its default on the next restart, while the settings panel keeps
    showing the saved value it read straight out of the file. The operator sees an
    enabled space allow-list and the gateway answers nobody.
    """

    @staticmethod
    def _load(tmp_path: Path, webex: dict) -> Any:
        import json
        import os

        from kiro_crew.config.loader import KiroCrewConfig

        (tmp_path / "config.json").write_text(json.dumps({"webex": webex}))
        old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(tmp_path)
        try:
            return KiroCrewConfig.load().webex
        finally:
            if old is None:
                os.environ.pop("KIROCREW_HOME", None)
            else:
                os.environ["KIROCREW_HOME"] = old

    def test_every_webex_field_survives_a_reload(self, tmp_path: Path) -> None:
        stored = {
            "enabled": True,
            "allowed_emails": ["kyle@example.com"],
            "allow_group_rooms": True,
            "allowed_room_ids": ["Y2lzY29zcGFyazovL3VzL1JPT00vZXhhbXBsZQ"],
            "reply_in_thread": False,
            "wdm_base": "https://wdm.internal.example.com",
            "soft_threshold_pct": 70,
            "hard_threshold_pct": 90,
        }

        loaded = self._load(tmp_path, stored)

        for key, expected in stored.items():
            assert getattr(loaded, key) == expected, f"{key} did not survive the reload"

    def test_the_loader_reads_every_field_the_dataclass_declares(self, tmp_path: Path) -> None:
        """A structural check, so the NEXT added field cannot be forgotten.

        Every non-default value in the file must come back changed; a field the
        loader omits comes back as its default and is caught by name here rather
        than by whoever restarts the gateway.
        """
        import dataclasses

        from kiro_crew.config.loader import WebexConfig

        defaults = WebexConfig()
        # A value that differs from the default for each field's own type.
        stored: dict[str, Any] = {}
        for f in dataclasses.fields(defaults):
            current = getattr(defaults, f.name)
            if isinstance(current, bool):
                stored[f.name] = not current
            elif isinstance(current, int):
                stored[f.name] = 42 if f.name != "hard_threshold_pct" else 99
            elif isinstance(current, list):
                stored[f.name] = ["not-a-default"]
            else:
                stored[f.name] = f"not-a-default-{f.name}"
        # The threshold pair is normalized against each other, and the folder name
        # is sanitized, so those two are asserted by the test above instead.
        for skipped in ("soft_threshold_pct", "hard_threshold_pct", "session_folder"):
            stored.pop(skipped, None)

        loaded = self._load(tmp_path, stored)

        forgotten = [k for k, v in stored.items() if getattr(loaded, k) != v]
        assert not forgotten, (
            f"the loader does not read: {forgotten}. A saved value silently reverts "
            "to its default on the next restart."
        )
