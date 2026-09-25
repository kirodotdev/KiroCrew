"""Tests for ACP dynamic config propagation (effort levels, models)."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.dashboard.chat_persistence import (
    _REASONING_EFFORT_FALLBACK,
    _SAFE_EFFORT_RE,
    get_reasoning_effort_ordered,
    get_reasoning_effort_values,
    register_reasoning_effort_values,
    update_reasoning_effort_values,
)


class TestStoreSessionConfig:
    def test_stores_config_options(self):
        client = AcpClient()
        resp = {
            "configOptions": [
                {"id": "effort", "options": [{"value": "low"}, {"value": "high"}]},
            ],
        }
        with patch.object(client, "_sync_effort_levels"):
            client._store_session_config(resp)
        assert client._acp_config_options == resp["configOptions"]

    def test_ignores_non_list_config_options(self):
        client = AcpClient()
        client._store_session_config({"configOptions": "invalid"})
        assert client._acp_config_options == []

    def test_calls_sync_effort_levels(self):
        client = AcpClient()
        resp = {"configOptions": [{"id": "effort", "options": [{"value": "low"}]}]}
        with patch.object(client, "_sync_effort_levels") as mock_sync:
            client._store_session_config(resp)
        mock_sync.assert_called_once()


class TestHandleConfigOptionUpdate:
    def test_updates_config_options_from_notification(self):
        client = AcpClient()
        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [
                        {"id": "effort", "options": [{"value": "medium"}, {"value": "max"}]},
                    ],
                }
            },
        )
        with patch.object(client, "_sync_effort_levels"):
            client._handle_config_option_update(msg)
        assert len(client._acp_config_options) == 1
        assert client._acp_config_options[0]["id"] == "effort"

    def test_ignores_non_dict_update(self):
        client = AcpClient()
        msg = JsonRpcMessage(method="session/update", params={"update": "not a dict"})
        client._handle_config_option_update(msg)
        assert client._acp_config_options == []

    def test_ignores_missing_config_options(self):
        client = AcpClient()
        msg = JsonRpcMessage(method="session/update", params={"update": {"other": "stuff"}})
        client._handle_config_option_update(msg)
        assert client._acp_config_options == []

    def test_ignores_none_params(self):
        client = AcpClient()
        msg = JsonRpcMessage(method="session/update", params=None)
        client._handle_config_option_update(msg)
        assert client._acp_config_options == []


class TestGetValidEffortLevels:
    def test_extracts_effort_levels_in_order(self):
        client = AcpClient()
        client._acp_config_options = [
            {"id": "model", "options": [{"value": "opus"}]},
            {"id": "effort", "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}]},
        ]
        assert client.get_valid_effort_levels() == ["low", "medium", "high"]

    def test_returns_empty_when_no_effort_config(self):
        client = AcpClient()
        client._acp_config_options = [{"id": "model", "options": [{"value": "opus"}]}]
        assert client.get_valid_effort_levels() == []

    def test_skips_non_dict_options(self):
        client = AcpClient()
        client._acp_config_options = [
            {"id": "effort", "options": ["invalid", {"value": "low"}, {"no_value": True}]},
        ]
        assert client.get_valid_effort_levels() == ["low"]

    def test_skips_non_dict_config_entries(self):
        client = AcpClient()
        client._acp_config_options = ["not a dict", {"id": "effort", "options": [{"value": "max"}]}]
        assert client.get_valid_effort_levels() == ["max"]

    def test_empty_config(self):
        client = AcpClient()
        assert client.get_valid_effort_levels() == []


class TestSupportsConfigOption:
    def test_true_when_option_advertised(self):
        client = AcpClient()
        client._acp_config_options = [
            {"id": "model", "options": []},
            {"id": "effort", "options": [{"value": "high"}]},
        ]
        assert client.supports_config_option("effort") is True
        assert client.supports_config_option("model") is True

    def test_false_when_option_absent(self):
        # Older claude-agent-acp: advertises model but no effort selector.
        client = AcpClient()
        client._acp_config_options = [{"id": "model", "options": []}]
        assert client.supports_config_option("effort") is False

    def test_true_when_no_options_reported_yet(self):
        # No options reported yet → don't permanently treat as unsupported
        # (a backend may advertise them lazily after the first turn).
        client = AcpClient()
        client._acp_config_options = []
        assert client.supports_config_option("effort") is True

    def test_skips_non_dict_entries(self):
        client = AcpClient()
        client._acp_config_options = ["not a dict", {"id": "effort", "options": []}]
        assert client.supports_config_option("effort") is True
        client._acp_config_options = ["not a dict"]
        assert client.supports_config_option("effort") is False


class TestSyncEffortLevels:
    def test_syncs_levels_to_persistence(self):
        client = AcpClient()
        client._acp_config_options = [
            {"id": "effort", "options": [{"value": "low"}, {"value": "high"}]},
        ]
        with patch(
            "kiro_crew.dashboard.chat_persistence.update_reasoning_effort_values"
        ) as mock_update:
            client._sync_effort_levels()
        mock_update.assert_called_once_with(["low", "high"])

    def test_does_not_sync_when_no_levels(self):
        client = AcpClient()
        client._acp_config_options = []
        with patch(
            "kiro_crew.dashboard.chat_persistence.update_reasoning_effort_values"
        ) as mock_update:
            client._sync_effort_levels()
        mock_update.assert_not_called()


class TestUpdateReasoningEffortValues:
    def setup_method(self):
        import kiro_crew.dashboard.chat_persistence as mod

        self._mod = mod
        self._orig_values = mod._reasoning_effort_values.copy()
        self._orig_ordered = mod._reasoning_effort_ordered[:]
        self._orig_marked = mod._reasoning_effort_marked.copy()

    def teardown_method(self):
        self._mod._reasoning_effort_values = self._orig_values
        self._mod._reasoning_effort_ordered = self._orig_ordered
        self._mod._reasoning_effort_marked = self._orig_marked

    def test_updates_values_and_ordered(self):
        update_reasoning_effort_values(["low", "medium", "high", "max"])
        values = get_reasoning_effort_values()
        assert "low" in values
        assert "medium" in values
        assert "high" in values
        assert "max" in values
        assert "" in values
        ordered = get_reasoning_effort_ordered()
        assert ordered == ["low", "medium", "high", "max"]

    def test_preserves_fallback_values(self):
        update_reasoning_effort_values(["turbo"])
        values = get_reasoning_effort_values()
        for fallback in _REASONING_EFFORT_FALLBACK:
            assert fallback in values
        assert "turbo" in values

    def test_sanitizes_invalid_input(self):
        update_reasoning_effort_values(["; rm -rf /", "UPPERCASE", "valid-level", ""])
        values = get_reasoning_effort_values()
        assert "; rm -rf /" not in values
        assert "UPPERCASE" not in values
        assert "valid-level" in values

    def test_regex_rejects_too_long(self):
        assert not _SAFE_EFFORT_RE.match("a" * 25)

    def test_regex_accepts_valid(self):
        assert _SAFE_EFFORT_RE.match("low")
        assert _SAFE_EFFORT_RE.match("ultra-high")
        assert _SAFE_EFFORT_RE.match("level_2")

    def test_regex_rejects_trailing_newline(self):
        # `\Z` (not `$`) so a trailing newline cannot slip through to the
        # persistence / --effort subprocess boundary.
        assert not _SAFE_EFFORT_RE.match("low\n")
        assert not _SAFE_EFFORT_RE.match("low\nrm -rf /")

    def test_empty_string_excluded_from_ordered(self):
        update_reasoning_effort_values(["low", "", "high"])
        ordered = get_reasoning_effort_ordered()
        assert "" not in ordered
        assert ordered == ["low", "high"]

    def test_validation_set_is_union_only(self):
        # A custom level reported by one session must stay valid even after a
        # later session reports a narrower config (so a slot that persisted it
        # is not silently reset on restore).
        update_reasoning_effort_values(["turbo"])
        assert "turbo" in get_reasoning_effort_values()
        update_reasoning_effort_values(["low", "high"])  # narrower, no "turbo"
        assert "turbo" in get_reasoning_effort_values()  # still valid (union-only)
        # …but the ordered DISPLAY list reflects only the latest report.
        assert get_reasoning_effort_ordered() == ["low", "high"]

    def test_remote_registration_does_not_reorder_local_fallback(self):
        before = get_reasoning_effort_ordered()
        register_reasoning_effort_values(["minimal", "UPPERCASE", "low\n"])
        assert "minimal" in get_reasoning_effort_values()
        assert "UPPERCASE" not in get_reasoning_effort_values()
        assert "low\n" not in get_reasoning_effort_values()
        assert get_reasoning_effort_ordered() == before

    def test_selected_dynamic_level_survives_cold_restore(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self._mod, "config_dir", lambda: tmp_path)
        self._mod._reasoning_effort_values = set(_REASONING_EFFORT_FALLBACK)
        # A transcript value alone cannot add an unadvertised level.
        assert self._mod._validate_reasoning_effort("minimal") == ""
        self._mod._remember_reasoning_effort_for_restore("minimal")
        assert not (tmp_path / "crew-panels" / "validated_effort_levels" / "minimal").exists()

        register_reasoning_effort_values(["minimal"])
        self._mod._remember_reasoning_effort_for_restore("minimal")
        marker = hashlib.sha256(b"minimal").hexdigest()
        assert (tmp_path / "crew-panels" / "validated_effort_levels" / marker).is_file()

        # Recreate the cold-start allowlist before ACP/peer capabilities load.
        self._mod._reasoning_effort_values = set(_REASONING_EFFORT_FALLBACK)
        marker = self._mod._has_validated_effort_marker("minimal")
        assert marker is True
        assert self._mod._validate_reasoning_effort("minimal", persisted_marker=marker) == "minimal"
        assert "minimal" in get_reasoning_effort_values()
        assert self._mod._validate_reasoning_effort("extreme") == ""

    def test_windows_reserved_level_uses_portable_marker_name(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self._mod, "config_dir", lambda: tmp_path)
        register_reasoning_effort_values(["con"])

        self._mod._remember_reasoning_effort_for_restore("con")

        marker = hashlib.sha256(b"con").hexdigest()
        assert (tmp_path / "crew-panels" / "validated_effort_levels" / marker).is_file()
        self._mod._reasoning_effort_values = set(_REASONING_EFFORT_FALLBACK)
        assert self._mod._has_validated_effort_marker("con")
        assert self._mod._validate_reasoning_effort("con", persisted_marker=True) == "con"

    def test_durable_marker_count_uses_retention_cap(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self._mod, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            self._mod,
            "MAX_RETAINED_REASONING_EFFORT_VALUES",
            len(_REASONING_EFFORT_FALLBACK) + 1,
        )
        self._mod._reasoning_effort_values = set(_REASONING_EFFORT_FALLBACK) | {"turbo", "minimal"}

        self._mod._remember_reasoning_effort_for_restore("turbo")
        with pytest.raises(ValueError, match="marker limit"):
            self._mod._remember_reasoning_effort_for_restore("minimal")
        assert self._mod._has_validated_effort_marker("turbo")
        assert not self._mod._has_validated_effort_marker("minimal")

    def test_concurrent_marker_writes_share_the_durable_cap(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self._mod, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            self._mod,
            "MAX_RETAINED_REASONING_EFFORT_VALUES",
            len(_REASONING_EFFORT_FALLBACK) + 1,
        )
        self._mod._reasoning_effort_values = set(_REASONING_EFFORT_FALLBACK) | {"turbo", "minimal"}

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self._remember_or_error, ("turbo", "minimal")))

        assert sorted(results) == ["accepted", "rejected"]
        assert (
            sum(
                path.is_file()
                for path in (tmp_path / "crew-panels" / "validated_effort_levels").iterdir()
                if path.name != ".lock"
            )
            == 1
        )

    def _remember_or_error(self, level):
        try:
            self._mod._remember_reasoning_effort_for_restore(level)
        except ValueError:
            return "rejected"
        return "accepted"

    def test_marked_restore_survives_peer_levels_filling_the_cap(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self._mod, "config_dir", lambda: tmp_path)
        register_reasoning_effort_values(["turbo"])
        self._mod._remember_reasoning_effort_for_restore("turbo")

        self._mod._reasoning_effort_values = set(_REASONING_EFFORT_FALLBACK)
        self._mod._reasoning_effort_marked = set()
        monkeypatch.setattr(
            self._mod,
            "MAX_RETAINED_REASONING_EFFORT_VALUES",
            len(_REASONING_EFFORT_FALLBACK) + 1,
        )
        assert register_reasoning_effort_values(["minimal"]) == ["minimal"]
        assert self._mod._has_validated_effort_marker("turbo")

        assert self._mod._validate_reasoning_effort("turbo", persisted_marker=True) == "turbo"
        assert "turbo" in get_reasoning_effort_values()
        assert "minimal" not in get_reasoning_effort_values()

    def test_marker_directory_is_inside_existing_hidden_gateway_root(self):
        from kiro_crew.sandbox import (
            _CREW_HIDDEN_LEAVES,
            _CREW_PRECREATE_HIDDEN_DIR_LEAVES,
        )
        from kiro_crew.security.paths import _CREW_SECRET_LEAVES

        assert self._mod._VALIDATED_EFFORT_DIR == "crew-panels/validated_effort_levels"
        assert "crew-panels" in _CREW_HIDDEN_LEAVES
        assert "crew-panels" in _CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert "crew-panels" in _CREW_SECRET_LEAVES
        assert "validated_effort_levels" not in _CREW_HIDDEN_LEAVES

    def test_linked_gateway_parent_cannot_authorize_effort(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self._mod, "config_dir", lambda: tmp_path)
        destination = tmp_path / "destination"
        destination.mkdir()
        try:
            (tmp_path / "crew-panels").symlink_to(destination, target_is_directory=True)
        except (NotImplementedError, OSError):
            pytest.skip("directory symlinks are unavailable")
        register_reasoning_effort_values(["turbo"])

        with pytest.raises(OSError, match="parent is a symlink"):
            self._mod._remember_reasoning_effort_for_restore("turbo")
        assert not self._mod._has_validated_effort_marker("turbo")
        assert not (destination / "validated_effort_levels").exists()

    def test_total_retention_cap_rejects_and_logs_new_levels(self, monkeypatch, caplog):
        monkeypatch.setattr(
            self._mod,
            "MAX_RETAINED_REASONING_EFFORT_VALUES",
            len(self._mod._reasoning_effort_values) + 1,
        )
        before = get_reasoning_effort_ordered()
        assert register_reasoning_effort_values(["minimal", "turbo"]) == ["minimal"]
        assert "minimal" in get_reasoning_effort_values()
        assert "turbo" not in get_reasoning_effort_values()
        assert get_reasoning_effort_ordered() == before
        update_reasoning_effort_values(["turbo"])
        assert get_reasoning_effort_ordered() == before
        update_reasoning_effort_values(["turbo", "low"])
        assert "turbo" not in get_reasoning_effort_values()
        assert get_reasoning_effort_ordered() == ["low"]
        assert "retained limit" in caplog.text


class TestAcpProperties:
    def test_acp_config_options_property(self):
        client = AcpClient()
        client._acp_config_options = [{"id": "test"}]
        assert client.acp_config_options == [{"id": "test"}]


@pytest.mark.asyncio
async def test_api_effort_levels_global_fallback():
    # No ?slot= → serve the process-global ordered fallback list.
    import kiro_crew.dashboard.chat_persistence as mod
    from kiro_crew.dashboard.handlers.agents import api_effort_levels

    orig_ordered = mod._reasoning_effort_ordered[:]
    try:
        mod._reasoning_effort_ordered = ["low", "medium", "high", "max"]
        request = MagicMock()
        request.query = {}  # no slot param
        resp = await api_effort_levels(request)
        assert resp.status == 200
        import json

        body = json.loads(resp.body)
        assert body == ["low", "medium", "high", "max"]
    finally:
        mod._reasoning_effort_ordered = orig_ordered


@pytest.mark.asyncio
async def test_api_effort_levels_per_slot():
    # ?slot= resolves to the slot's live ACP provider; its current-model levels
    # win over the process-global fallback (no cross-slot bleed).
    import kiro_crew.dashboard.chat_persistence as mod
    from kiro_crew.dashboard.handlers.agents import api_effort_levels

    orig_ordered = mod._reasoning_effort_ordered[:]
    try:
        mod._reasoning_effort_ordered = ["low", "max"]  # global (other slot)
        provider = MagicMock()
        provider.get_valid_effort_levels.return_value = ["low", "medium", "high", "xhigh"]
        sessions = MagicMock()
        sessions.get_provider.return_value = provider
        state = MagicMock()
        state.sessions = sessions
        request = MagicMock()
        request.query = {"slot": "slot-b"}
        request.app = {"state": state}
        resp = await api_effort_levels(request)
        assert resp.status == 200
        import json

        assert json.loads(resp.body) == ["low", "medium", "high", "xhigh"]
    finally:
        mod._reasoning_effort_ordered = orig_ordered


@pytest.mark.asyncio
async def test_api_effort_levels_slot_without_live_provider_falls_back():
    import kiro_crew.dashboard.chat_persistence as mod
    from kiro_crew.dashboard.handlers.agents import api_effort_levels

    orig_ordered = mod._reasoning_effort_ordered[:]
    try:
        mod._reasoning_effort_ordered = ["low", "medium", "high", "max"]
        sessions = MagicMock()
        sessions.get_provider.return_value = None  # no live session for the slot
        state = MagicMock()
        state.sessions = sessions
        request = MagicMock()
        request.query = {"slot": "cold-slot"}
        request.app = {"state": state}
        resp = await api_effort_levels(request)
        assert resp.status == 200
        import json

        assert json.loads(resp.body) == ["low", "medium", "high", "max"]
    finally:
        mod._reasoning_effort_ordered = orig_ordered
