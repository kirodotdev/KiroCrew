"""The legacy ``hooks.webhook_token`` in the config API, and the boot notice.

``hooks`` is a free-form dict outside the schema, so the schema-driven mask in
``_masked_config_dict`` never covered the legacy bearer token. These tests pin:

* ``GET /api/config/kirocrew`` masks it like any other secret field;
* a dashboard save (PUT or PATCH) never writes the mask over the stored value;
* gateway boot logs one WARNING naming plaintext credential KEY PATHS in the raw
  config documents, never their values.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict


def plaintext_credential_paths(doc: object) -> list[str]:
    from kiro_crew.config.plaintext_credentials import plaintext_credential_paths as fn

    return fn(doc)


def warn_plaintext_credentials() -> None:
    from kiro_crew.config.plaintext_credentials import warn_plaintext_credentials as fn

    fn()


_LEGACY = "legacy-hook-token-0123456789abcdef"
_BOT = "8412345678:AAsecretsecretsecretsecretsecret"
_NOTICE_LOGGER = "kiro_crew.config.loader"


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point every config path at a temp home and return the config.json path."""
    cfgp = tmp_path / "config.json"
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return cfgp


def _write(path, doc) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def _stored_token(cfgp) -> object:
    return json.loads(cfgp.read_text(encoding="utf-8"))["hooks"]["webhook_token"]


# ── Masked view ────────────────────────────────────────────────────────────


class TestMaskedView:
    def test_legacy_token_is_masked(self, cfg_home):
        _write(cfg_home, {"hooks": {"webhook_token": _LEGACY}})
        out = _masked_config_dict(KiroCrewConfig.load())
        assert out["hooks"]["webhook_token"] == _SENSITIVE_MASK
        assert _LEGACY not in json.dumps(out)

    def test_unset_token_stays_absent_or_empty(self, cfg_home):
        _write(cfg_home, {"hooks": {"webhook_token": ""}})
        out = _masked_config_dict(KiroCrewConfig.load())
        assert out["hooks"]["webhook_token"] == ""
        assert _SENSITIVE_MASK not in json.dumps(out.get("hooks", {}))

    def test_no_hooks_section(self, cfg_home):
        _write(cfg_home, {"auto_update": False})
        out = _masked_config_dict(KiroCrewConfig.load())
        assert "webhook_token" not in (out.get("hooks") or {})

    def test_secret_reference_is_masked_like_other_secrets(self, cfg_home):
        # Same rule as schema-sensitive fields: any non-empty string is masked.
        _write(cfg_home, {"hooks": {"webhook_token": "secret://HOOK_TOKEN"}})
        out = _masked_config_dict(KiroCrewConfig.load())
        assert out["hooks"]["webhook_token"] == _SENSITIVE_MASK

    def test_other_hooks_keys_untouched(self, cfg_home):
        _write(
            cfg_home,
            {"hooks": {"webhook_token": _LEGACY, "confirm_tools": ["shell"]}},
        )
        out = _masked_config_dict(KiroCrewConfig.load())
        assert out["hooks"]["confirm_tools"] == ["shell"]

    def test_local_overlay_token_is_masked(self, cfg_home):
        _write(cfg_home, {"auto_update": False})
        _write(cfg_home.parent / "config.local.json", {"hooks": {"webhook_token": _LEGACY}})
        out = _masked_config_dict(KiroCrewConfig.load())
        assert out["hooks"]["webhook_token"] == _SENSITIVE_MASK

    def test_save_path_keeps_real_value(self, cfg_home):
        _write(cfg_home, {"hooks": {"webhook_token": _LEGACY}})
        cfg = KiroCrewConfig.load()
        _masked_config_dict(cfg)
        assert cfg.to_dict()["hooks"]["webhook_token"] == _LEGACY


# ── Secret fields inside map-shaped sections ───────────────────────────────


class _DictCfg:
    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self._data))


def _secret() -> dict:
    return {"type": "string", "x-meta": {"sensitive": True}}


class TestMapShapedSecrets:
    def test_telegram_account_tokens_are_masked(self, cfg_home):
        _write(
            cfg_home,
            {
                "telegram": {
                    "bot_token": _BOT,
                    "accounts": {"one": {"bot_token": _BOT}, "two": {"bot_token": _BOT}},
                }
            },
        )
        out = _masked_config_dict(KiroCrewConfig.load())
        accounts = out["telegram"]["accounts"]
        assert accounts["one"]["bot_token"] == _SENSITIVE_MASK
        assert accounts["two"]["bot_token"] == _SENSITIVE_MASK
        assert accounts["one"]["allowed_user_ids"] == []  # non-secret untouched
        assert _BOT not in json.dumps(out)

    def test_any_map_of_secret_fields_is_masked(self, monkeypatch):
        """A map section, a map inside a list, and a map of maps, all masked."""
        from kiro_crew.config import schema as schema_mod

        entry = {"type": "object", "properties": {"api_key": _secret(), "label": {}}}
        fake = {
            "type": "object",
            "properties": {
                "vendors": {"type": "object", "additionalProperties": entry},
                "pools": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "members": {"type": "object", "additionalProperties": entry}
                        },
                    },
                },
                "regions": {
                    "type": "object",
                    "additionalProperties": {"type": "object", "additionalProperties": entry},
                },
                "plain": {"type": "object", "additionalProperties": {"type": "string"}},
            },
        }
        monkeypatch.setattr(schema_mod, "JSON_SCHEMA", fake)
        out = _masked_config_dict(
            _DictCfg(
                {
                    "vendors": {
                        "acme": {"api_key": "k1", "label": "Acme"},
                        "unset": {"api_key": ""},
                    },
                    "pools": [{"members": {"m": {"api_key": "k2"}}}],
                    "regions": {"eu": {"a": {"api_key": "k3"}}},
                    "plain": {"x": "visible"},
                }
            )  # type: ignore[arg-type]
        )
        assert out["vendors"]["acme"]["api_key"] == _SENSITIVE_MASK
        assert out["vendors"]["acme"]["label"] == "Acme"
        assert out["vendors"]["unset"]["api_key"] == ""  # unset stays unset
        assert out["pools"][0]["members"]["m"]["api_key"] == _SENSITIVE_MASK
        assert out["regions"]["eu"]["a"]["api_key"] == _SENSITIVE_MASK
        assert out["plain"]["x"] == "visible"


# ── Wire: GET, then a dashboard save ───────────────────────────────────────


class TestConfigEndpointWire:
    @pytest.mark.asyncio
    async def test_get_masks_and_put_round_trip_keeps_real_value(self, cfg_home):
        """GET hides the token; sending the masked view back via PUT keeps it."""
        from kiro_crew.dashboard.handlers import core as core_mod

        _write(cfg_home, {"hooks": {"webhook_token": _LEGACY}, "auto_update": False})
        app = web.Application()
        app.router.add_route("*", "/api/config/kirocrew", core_mod.api_kirocrew_config)
        as_owner(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config/kirocrew")
            assert resp.status == 200
            body = await resp.json()
            assert body["hooks"]["webhook_token"] == _SENSITIVE_MASK
            assert _LEGACY not in json.dumps(body)

            # The whole masked view goes back, as a careless client would send it.
            body["agent"] = {"subagent_max_turns": 10}
            put = await client.put("/api/config/kirocrew", json=body)
            assert put.status == 200, await put.text()
            assert _LEGACY not in await put.text()
        assert _stored_token(cfg_home) == _LEGACY

    @pytest.mark.asyncio
    async def test_patch_with_mask_cannot_overwrite_token(self, cfg_home):
        from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

        _write(cfg_home, {"hooks": {"webhook_token": _LEGACY}, "auto_update": False})
        app = web.Application()
        app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
        app["state"] = SimpleNamespace(subagents=MagicMock(spec=["update_completion_keep"]))
        as_owner(app)
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_home):
            async with TestClient(TestServer(app)) as client:
                refused = await client.patch(
                    "/api/config/kirocrew",
                    json={"path": "hooks.webhook_token", "value": _SENSITIVE_MASK},
                )
                assert refused.status >= 400
                ok = await client.patch(
                    "/api/config/kirocrew", json={"path": "auto_update", "value": True}
                )
                assert ok.status == 200
                echo = await ok.json()
        assert echo["hooks"]["webhook_token"] == _SENSITIVE_MASK
        assert _stored_token(cfg_home) == _LEGACY


# ── Boot notice ────────────────────────────────────────────────────────────


def _notices(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _NOTICE_LOGGER and r.getMessage().startswith("Plain-text values at ")
    ]


class TestPlaintextNotice:
    def test_legacy_and_channel_tokens_named_without_values(self, cfg_home, caplog):
        _write(
            cfg_home,
            {"hooks": {"webhook_token": _LEGACY}, "discord": {"bot_token": _BOT}},
        )
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        notes = _notices(caplog)
        assert len(notes) == 1
        assert "config.json:hooks.webhook_token" in notes[0]
        assert "config.json:discord.bot_token" in notes[0]
        assert _LEGACY not in notes[0] and _BOT not in notes[0]

    def test_nothing_set_logs_nothing(self, cfg_home, caplog):
        _write(cfg_home, {"hooks": {"webhook_token": ""}, "discord": {"bot_token": ""}})
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        assert _notices(caplog) == []

    def test_missing_files_log_nothing(self, cfg_home, caplog):
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        assert _notices(caplog) == []

    def test_malformed_file_is_skipped(self, cfg_home, caplog):
        cfg_home.write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        assert _notices(caplog) == []

    def test_env_only_token_does_not_warn(self, cfg_home, caplog, monkeypatch):
        # The notice reads the raw file, not the env-merged load.
        monkeypatch.setenv("DISCORD_BOT_TOKEN", _BOT)
        _write(cfg_home, {"discord": {"enabled": True}})
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        assert _notices(caplog) == []

    def test_local_overlay_is_checked(self, cfg_home, caplog):
        _write(cfg_home, {"auto_update": False})
        _write(cfg_home.parent / "config.local.json", {"hooks": {"webhook_token": _LEGACY}})
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        notes = _notices(caplog)
        assert len(notes) == 1
        assert "config.local.json:hooks.webhook_token" in notes[0]

    def test_webhook_advice_only_when_legacy_present(self, cfg_home, caplog):
        _write(cfg_home, {"discord": {"bot_token": _BOT}})
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        notes = _notices(caplog)
        assert len(notes) == 1
        assert "channel settings" in notes[0]
        assert "Webhooks page" not in notes[0]

    def test_legacy_only_has_no_channel_advice(self, cfg_home, caplog):
        _write(cfg_home, {"hooks": {"webhook_token": _LEGACY}})
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        notes = _notices(caplog)
        assert len(notes) == 1
        assert "Webhooks page" in notes[0]
        assert "channel settings" not in notes[0]

    def test_non_credential_schema_secrets_do_not_warn(self, cfg_home, caplog):
        # Hidden in the browser view, but not credentials, and config.json is
        # their only home: warning on them could never be cleared.
        _write(
            cfg_home,
            {
                "stt": {"custom_model_path": "model.bin", "custom_model_sha256": "ab" * 32},
                "telemetry": {"otlp_endpoint": "https://otel.example.com"},
            },
        )
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            warn_plaintext_credentials()
        assert _notices(caplog) == []


class TestNoticeScheduling:
    @pytest.mark.asyncio
    async def test_start_does_not_block_and_logs_in_background(self, cfg_home, caplog):
        from kiro_crew.config import plaintext_credentials as pc

        _write(cfg_home, {"hooks": {"webhook_token": _LEGACY}})
        with caplog.at_level(logging.WARNING, logger=_NOTICE_LOGGER):
            pc.start_plaintext_credentials_notice()  # sync: returns before the read
            task = pc._NOTICE_TASK
            assert task is not None
            await task
        assert len(_notices(caplog)) == 1

    @pytest.mark.asyncio
    async def test_failure_never_raises(self, monkeypatch):
        from kiro_crew.config import plaintext_credentials as pc

        def _boom() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(pc, "warn_plaintext_credentials", _boom)
        pc.start_plaintext_credentials_notice()
        task = pc._NOTICE_TASK
        assert task is not None
        await task  # swallowed, logged at debug
        assert task.exception() is None


class TestPlaintextPaths:
    def test_secret_references_are_not_plaintext(self):
        doc = {
            "hooks": {"webhook_token": "secret://HOOK"},
            "telegram": {"bot_token": "secret://TG"},
        }
        assert plaintext_credential_paths(doc) == []

    def test_named_accounts_map_is_walked(self):
        doc = {"telegram": {"accounts": {"one": {"bot_token": _BOT}, "two": {"bot_token": _BOT}}}}
        assert plaintext_credential_paths(doc) == [
            "telegram.accounts.one.bot_token",
            "telegram.accounts.two.bot_token",
        ]

    def test_non_string_and_non_sensitive_values_ignored(self):
        doc = {
            "hooks": {"webhook_token": 12345},
            "telegram": {"enabled": True, "allowed_user_ids": [7], "accounts": "x"},
            "weixin": {"token": _BOT},
            "discord": "not-an-object",
        }
        assert plaintext_credential_paths(doc) == ["weixin.token"]

    def test_every_channel_credential_path(self):
        doc = {
            "telegram": {"bot_token": _BOT},
            "discord": {"bot_token": _BOT},
            "webex": {"bot_token": _BOT},
            "weixin": {"token": _BOT},
            "teams": {"app_password": _BOT},
            "decisions": {"provider": {"api_key": _BOT}},
        }
        assert set(plaintext_credential_paths(doc)) == {
            "telegram.bot_token",
            "discord.bot_token",
            "webex.bot_token",
            "weixin.token",
            "teams.app_password",
        }  # decisions.provider.api_key has no channel/.env home, so it is not named

    def test_non_object_document(self):
        assert plaintext_credential_paths(["x"]) == []
