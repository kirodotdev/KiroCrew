"""Top-level config.json sections that core writes and reads without a dataclass model.

``voice_reply`` is the voice-settings block: the dashboard and Slack settings UI
persist it into config.json (``slack/interactions.py`` via ``data.setdefault``,
``dashboard/chat_voice.py`` via ``json.dump``), and the gateway reads it back on
startup (``slack/handler_runtime/voice.py::load_voice_reply_config``). There is
no ``SCHEMA_REGISTRY`` entry and no ``KiroCrewConfig`` field, so before the
exemption set existed every launch logged ``Config: unrecognized top-level keys:
voice_reply`` -- a false positive warning about a section the product wrote
itself.

Three properties hold the fix together:

* validation does not report a core-owned section as unrecognized, and still
  reports a genuinely unknown key beside it;
* the section round-trips through ``load()`` -> ``to_dict()`` like any other
  unmodelled section (it is NOT a reserved key, which save() drops);
* every member of the set names a key some core module actually reads -- pinned
  here for ``voice_reply`` against the reader itself, so the set cannot drift
  into a list of silenced typos.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config import validation
from kiro_crew.config.loader import (
    _KNOWN_CONFIG_SECTIONS,
    CONFIG_RESERVED_TOP_KEYS,
    KiroCrewConfig,
)

_UNRECOGNIZED = "unrecognized top-level keys"


def _unrecognized_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if _UNRECOGNIZED in r.getMessage()]


class TestCoreOwnedSectionsAreNotUnrecognized:
    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_voice_reply_section_is_not_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        data = {"agent": {"provider": "acp"}, "voice_reply": {"enabled": True, "provider": "polly"}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_genuinely_unknown_key_still_warns_beside_a_core_owned_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Excluding the core-owned set must not widen into silencing real typos.
        data = {"voice_reply": {"enabled": True}, "totally_unknown_key": 1}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "totally_unknown_key" in warnings[0]
        assert "voice_reply" not in warnings[0]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    @pytest.mark.parametrize("malformed", ["polly", ["polly"], 7, None])
    def test_a_non_object_voice_reply_value_still_warns(
        self, malformed: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exclusion covers the shape the reader keeps, not the key name.

        ``load_voice_reply_config`` keeps only a dict and treats a non-dict as
        empty, so a scalar written by hand must keep the one warning that says it
        is being ignored.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(
                {"agent": {"provider": "acp"}, "voice_reply": malformed}
            )
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "voice_reply" in warnings[0]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_load_of_a_voice_reply_config_logs_no_unrecognized_warning(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The operator-visible path: load() of a real file, not the validator alone."""
        cfgp = tmp_path / "config.json"
        cfgp.write_text(
            json.dumps({"agent": {"provider": "acp"}, "voice_reply": {"enabled": True}})
        )
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            KiroCrewConfig.load()
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_connections_section_written_by_oauth_apps_is_not_reported(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The OAuth Apps form writes ``connections.oauth_clients``; loading it stays quiet."""
        cfgp = tmp_path / "config.json"
        cfgp.write_text(
            json.dumps(
                {
                    "agent": {"provider": "acp"},
                    "connections": {"oauth_clients": {"github": {"client_id": "Iv1.abc123"}}},
                }
            )
        )
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            cfg = KiroCrewConfig.load()
        assert _unrecognized_warnings(caplog) == []
        assert cfg.to_dict()["connections"]["oauth_clients"]["github"]["client_id"] == "Iv1.abc123"

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_a_non_object_connections_value_still_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data({"agent": {"provider": "acp"}, "connections": "github"})
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "connections" in warnings[0]


class TestCoreOwnedSectionsRoundTrip:
    def test_voice_reply_section_survives_load_and_to_dict(self, tmp_path, monkeypatch) -> None:
        """Unlike a RESERVED key, a core-owned section must not be dropped on save."""
        vr = {"enabled": True, "provider": "polly", "voice_id": "Ruth"}
        cfgp = tmp_path / "config.json"
        cfgp.write_text(json.dumps({"agent": {"provider": "acp"}, "voice_reply": vr}))
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        cfg = KiroCrewConfig.load()
        assert cfg._extra_sections.get("voice_reply") == vr
        assert cfg.to_dict().get("voice_reply") == vr

    def test_set_is_disjoint_from_known_reserved_and_app_owned(self) -> None:
        """A key in two sets would be classified two ways by the same loader."""
        assert not (validation._CORE_OWNED_TOP_KEYS & set(_KNOWN_CONFIG_SECTIONS))
        assert not (validation._CORE_OWNED_TOP_KEYS & CONFIG_RESERVED_TOP_KEYS)
        assert not (validation._CORE_OWNED_TOP_KEYS & validation._APP_OWNED_TOP_KEYS)


class TestEveryCoreOwnedKeyHasAReader:
    """The set is a claim that core reads the key; pin the claim to the reader."""

    def test_members_are_exactly_the_documented_readers(self) -> None:
        # Extend this tuple together with validation._CORE_OWNED_TOP_KEYS and
        # add a reader test below; a member with no reader test is a silenced typo.
        assert validation._CORE_OWNED_TOP_KEYS == frozenset({"voice_reply", "connections"})

    def test_connections_client_id_is_read_by_resolve_oauth_client(self) -> None:
        from kiro_crew.connections.oauth_clients import _config_client_id

        config = {"connections": {"oauth_clients": {"github": {"client_id": "Iv1.abc123"}}}}
        assert _config_client_id(config, "github") == "Iv1.abc123"

    def test_connections_tool_aliases_is_read_by_the_alias_gate(self, monkeypatch) -> None:
        from kiro_crew.agent_materialization import mcp_aliases

        monkeypatch.setattr(
            mcp_aliases.agent_mod, "_load_json", lambda _p: {"connections": {"tool_aliases": True}}
        )
        assert mcp_aliases._connection_tool_aliases_enabled() is True

    def test_voice_reply_is_read_by_load_voice_reply_config(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.slack import handler as handler_mod

        # load_voice_reply_config mutates the shared module-level handler._vc
        # (global_enabled, default_voice, ...). Point it at a throwaway instance
        # for the duration of the test so the reader's writes do not leak into
        # any later test; monkeypatch restores the original object on teardown.
        fresh_vc = handler_mod._VoiceConfig()
        monkeypatch.setattr(handler_mod, "_vc", fresh_vc)
        load_voice_reply_config = handler_mod.load_voice_reply_config

        vr_settings = {"enabled": True, "provider": "polly", "voice_id": "Matthew"}
        cfgp = tmp_path / "config.json"
        cfgp.write_text(json.dumps({"agent": {"provider": "acp"}, "voice_reply": vr_settings}))
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        cfg = KiroCrewConfig.load()
        load_voice_reply_config(cfg)
        assert fresh_vc.global_enabled is True
        assert fresh_vc.default_voice == "Matthew"
