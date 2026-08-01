"""Tests for PATCH /api/config/kirocrew validators (enum, int, float, bool, str)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


_UNSET: object = object()


def _make_app_with_state(
    subagents: object = _UNSET,
) -> tuple[web.Application, MagicMock | None]:
    """Build a PATCH-handler app with a stubbed ``state.subagents``.

    Returns the app and the subagents mock so tests can assert call args.
    The ``agent.completion_keep`` / ``agent.completion_keep_chars`` PATCH
    paths consult ``request.app["state"].subagents`` to hot-reload the
    cached values; without the stub the handler raises ``KeyError``.

    The default builds a fresh ``MagicMock``. Pass ``subagents=None``
    explicitly to exercise the gateway-during-startup case where the
    manager is not yet wired up. The ``_UNSET`` sentinel distinguishes
    that from the default so an explicit ``None`` is preserved end-to-end.
    """
    app = _make_app()
    if subagents is _UNSET:
        subagents = MagicMock(spec=["update_completion_keep"])
    app["state"] = SimpleNamespace(subagents=subagents)
    return app, subagents  # type: ignore[return-value]


def _seed_config() -> dict:
    return {
        "agents": {
            "kirocrew": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            }
        },
        "default_agent": "kirocrew",
        "session": {"pool_agent": "", "timeout_secs": 3600, "autocompact_pct": 50.0},
        "agent": {"approval_mode": "auto", "sandbox": "auto"},
        "auto_update": False,
    }


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_seed_config()), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


# ── General ──────────────────────────────────────────────────────────────


class TestPatchGeneral:
    @pytest.mark.asyncio
    async def test_unknown_field_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "nonexistent.field", "x")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_body_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400


# ── Enum validator ───────────────────────────────────────────────────────


class TestEnumValidator:
    @pytest.mark.asyncio
    async def test_valid_enum_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.approval_mode", "interactive")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_enum_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.approval_mode", "bogus")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_enum_wrong_type_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "agent.approval_mode", 123)
            assert resp.status == 400


# ── Int validator ────────────────────────────────────────────────────────


class TestIntValidator:
    @pytest.mark.asyncio
    async def test_valid_int_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", 120)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_int_below_min_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", -1)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_int_above_max_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", 100000)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_int_non_numeric_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.timeout_secs", "abc")
            assert resp.status == 400


# ── Float validator ──────────────────────────────────────────────────────


class TestFloatValidator:
    @pytest.mark.asyncio
    async def test_valid_float_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", 25.0)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_float_below_min_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", 1.0)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_float_above_max_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", 95.0)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_float_nan_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", float("nan"))
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_float_non_numeric_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.autocompact_pct", "abc")
            assert resp.status == 400


# ── Bool validator ───────────────────────────────────────────────────────


class TestBoolValidator:
    @pytest.mark.asyncio
    async def test_valid_bool_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "auto_update", True)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_bool_non_bool_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "auto_update", "true")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_instances_enabled_toggle(self, tmp_config) -> None:
        # The Instances settings panel flips instances.enabled via this endpoint.
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "instances.enabled", True)
            assert resp.status == 200
            resp = await _patch(c, "instances.enabled", "yes")  # non-bool rejected
            assert resp.status == 400
        # value is written nested under the instances section
        written = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert written["instances"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_beacon_enabled_opt_out(self, tmp_config) -> None:
        """Settings → Privacy flips the beacon through this endpoint.

        This is the GUI twin of ``kirocrew telemetry disable`` and must persist
        to the SAME key, so the choice survives restarts and the CLI reports it.
        """
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "telemetry.beacon_enabled", False)).status == 200
        written = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert written["telemetry"]["beacon_enabled"] is False

        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "telemetry.beacon_enabled", True)).status == 200
        written = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert written["telemetry"]["beacon_enabled"] is True

    @pytest.mark.asyncio
    async def test_beacon_enabled_rejects_non_bool(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "telemetry.beacon_enabled", "off")).status == 400

    @pytest.mark.asyncio
    async def test_beacon_endpoint_is_not_editable(self, tmp_config) -> None:
        """Only the boolean opt-out is reachable from the dashboard.

        Exposing ``beacon_endpoint`` would let a dashboard caller redirect the
        heartbeat to an arbitrary host, so it stays CLI/config-file-only.
        """
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "telemetry.beacon_endpoint", "https://evil.example")
            assert resp.status == 400


# ── Str validator (pool_agent) ───────────────────────────────────────────


class TestStrValidator:
    @pytest.mark.asyncio
    async def test_valid_agent_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "kirocrew")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_empty_string_passes(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_string_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", 123)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_exceeds_max_len_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "a" * 257)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_400(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "session.pool_agent", "nonexistent")
            assert resp.status == 400
            data = await resp.json()
            assert "invalid value" in data["error"]


# ── completion_keep hot-reload ───────────────────────────────────────────


class TestCompletionKeepHotReload:
    """Settings UI changes must propagate to the live SubagentManager."""

    @pytest.mark.asyncio
    async def test_mode_change_calls_setter_with_loader_validated_value(self, tmp_config) -> None:
        """PATCH agent.completion_keep invokes update_completion_keep with the
        loader-validated mode and the current chars value."""
        app, subagents = _make_app_with_state()
        async with TestClient(TestServer(app)) as c:
            resp = await _patch(c, "agent.completion_keep", "tail")
            assert resp.status == 200
        subagents.update_completion_keep.assert_called_once()
        mode, chars = subagents.update_completion_keep.call_args.args
        assert mode == "tail"
        # Default chars come from the loader since the seed config doesn't
        # set agent.completion_keep_chars.
        assert isinstance(chars, int)

    @pytest.mark.asyncio
    async def test_chars_change_calls_setter(self, tmp_config) -> None:
        """PATCH agent.completion_keep_chars invokes update_completion_keep."""
        app, subagents = _make_app_with_state()
        async with TestClient(TestServer(app)) as c:
            resp = await _patch(c, "agent.completion_keep_chars", 7500)
            assert resp.status == 200
        subagents.update_completion_keep.assert_called_once()
        mode, chars = subagents.update_completion_keep.call_args.args
        assert chars == 7500
        assert mode in ("head", "tail", "both")  # whatever the loader settled on

    @pytest.mark.asyncio
    async def test_invalid_mode_does_not_call_setter(self, tmp_config) -> None:
        """A 400 from the validator must short-circuit before the hot-reload."""
        app, subagents = _make_app_with_state()
        async with TestClient(TestServer(app)) as c:
            resp = await _patch(c, "agent.completion_keep", "bogus")
            assert resp.status == 400
        subagents.update_completion_keep.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrelated_field_does_not_call_setter(self, tmp_config) -> None:
        """PATCHes to other config fields must NOT touch the subagent manager."""
        app, subagents = _make_app_with_state()
        async with TestClient(TestServer(app)) as c:
            resp = await _patch(c, "session.timeout_secs", 600)
            assert resp.status == 200
        subagents.update_completion_keep.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_subagent_manager_is_no_op(self, tmp_config) -> None:
        """When state.subagents is None, the hot-reload silently no-ops.

        This matches the gateway-during-startup case and prevents a 500 if
        the manager is not yet wired up.
        """
        app, subagents = _make_app_with_state(subagents=None)
        # Sanity-check the helper actually preserved None end-to-end so this
        # test exercises the real None-guard path in the handler.
        assert subagents is None
        assert app["state"].subagents is None
        async with TestClient(TestServer(app)) as c:
            resp = await _patch(c, "agent.completion_keep", "both")
            assert resp.status == 200


# ── User profile fields (onboarding step 2 / Settings > General) ─────────


class TestUserProfilePatch:
    @pytest.mark.asyncio
    async def test_valid_role_persists(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "dashboard.user_role", "designer")
            assert resp.status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["dashboard"]["user_role"] == "designer"

    @pytest.mark.asyncio
    async def test_valid_technical_level_persists(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "dashboard.user_technical_level", "somewhat-technical")
            assert resp.status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["dashboard"]["user_technical_level"] == "somewhat-technical"

    @pytest.mark.asyncio
    async def test_empty_clears_profile_field(self, tmp_config) -> None:
        """'' is a legal enum value — deselecting an answer clears it."""
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.user_role", "developer")).status == 200
            assert (await _patch(c, "dashboard.user_role", "")).status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["dashboard"]["user_role"] == ""

    @pytest.mark.asyncio
    async def test_invalid_role_rejected(self, tmp_config) -> None:
        """Free text must not sneak into the structured slug field."""
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "dashboard.user_role", "designing a banking app")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_technical_level_rejected(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "dashboard.user_technical_level", "expert")
            assert resp.status == 400


# ── Default model + default reasoning effort (Settings > Chat) ────────────


def _make_app_with_sessions() -> tuple[web.Application, MagicMock]:
    """Build a PATCH app whose state exposes an awaitable refresh_defaults.

    ``agent.model`` / ``agent.reasoning_effort`` reload the provider factory so
    the new default reaches new sessions without a gateway restart; without the
    stub the handler raises ``KeyError``.
    """
    app = _make_app()
    sessions = MagicMock(spec=["refresh_defaults", "reload_provider_factory"])
    sessions.refresh_defaults = AsyncMock()
    sessions.reload_provider_factory = AsyncMock()
    app["state"] = SimpleNamespace(sessions=sessions)
    return app, sessions


class TestDefaultModelPatch:
    @pytest.mark.asyncio
    async def test_kiro_style_id_persists(self, tmp_config) -> None:
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            resp = await _patch(c, "agent.model", "claude-opus-4.8")
            assert resp.status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["agent"]["model"] == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_canonical_registry_key_persists(self, tmp_config) -> None:
        """Canonical keys carry a bracket-free suffix and must survive the grammar."""
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.model", "opus-4.8-1m")).status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["agent"]["model"] == "opus-4.8-1m"

    @pytest.mark.asyncio
    async def test_auto_persists(self, tmp_config) -> None:
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.model", "auto")).status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["agent"]["model"] == "auto"

    @pytest.mark.asyncio
    async def test_reloads_provider_factory(self, tmp_config) -> None:
        """The factory captures the model at build time — defaults must refresh."""
        app, sessions = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.model", "claude-sonnet-4.5")).status == 200
        sessions.refresh_defaults.assert_awaited_once()
        # A default change must NEVER take the destructive path — that clears
        # _sessions and shuts live providers down, killing in-flight turns.
        sessions.reload_provider_factory.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            "claude opus",  # whitespace
            "model;rm -rf /",  # shell metacharacters
            "../../etc/passwd",  # path traversal
            "model$(id)",  # command substitution
            "model\nnewline",
        ],
    )
    async def test_malformed_ids_rejected(self, tmp_config, bad) -> None:
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.model", bad)).status == 400
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert "model" not in data["agent"]

    @pytest.mark.asyncio
    async def test_overlong_id_rejected(self, tmp_config) -> None:
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.model", "a" * 65)).status == 400

    @pytest.mark.asyncio
    async def test_non_string_rejected(self, tmp_config) -> None:
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.model", 42)).status == 400


class TestDefaultReasoningEffortPatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    async def test_each_level_persists(self, tmp_config, level) -> None:
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.reasoning_effort", level)).status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["agent"]["reasoning_effort"] == level

    @pytest.mark.asyncio
    async def test_empty_clears_to_model_default(self, tmp_config) -> None:
        """'' is the 'let the model decide' sentinel, not an invalid value."""
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.reasoning_effort", "high")).status == 200
            assert (await _patch(c, "agent.reasoning_effort", "")).status == 200
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["agent"]["reasoning_effort"] == ""

    @pytest.mark.asyncio
    async def test_unknown_level_rejected(self, tmp_config) -> None:
        app, _ = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.reasoning_effort", "ultra")).status == 400

    @pytest.mark.asyncio
    async def test_reloads_provider_factory(self, tmp_config) -> None:
        app, sessions = _make_app_with_sessions()
        async with TestClient(TestServer(app)) as c:
            assert (await _patch(c, "agent.reasoning_effort", "xhigh")).status == 200
        sessions.refresh_defaults.assert_awaited_once()
        # A default change must NEVER take the destructive path — that clears
        # _sessions and shuts live providers down, killing in-flight turns.
        sessions.reload_provider_factory.assert_not_awaited()
