"""Enterprise build: local switches for controls with no governance key are ignored.

RFC ``rfc-crew-enterprise-edition`` decision D3. Each control in
``kiro_crew.edition_locks.UNKEYED_LOCKS`` is checked at the point its feature reads
its own switch: on, in the ``oss`` edition, the local value still applies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import DEFAULT, MagicMock

import pytest

from kiro_crew import edition_locks
from kiro_crew.computer_use import enable_state
from kiro_crew.config.sections_telemetry import TelemetryConfig
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.handlers.agents import api_config_schema
from kiro_crew.metrics import provider as metrics_provider
from kiro_crew.platform import update_layout
from kiro_crew.platform.defaults import DefaultTelemetryProvider


@pytest.fixture(autouse=True)
def _fresh_lock_log():
    edition_locks.reset_logged()
    yield
    edition_locks.reset_logged()


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture
def channel_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "channel").write_text("nightly\n", encoding="utf-8")
    monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)


def test_every_lock_id_is_listed():
    with pytest.raises(KeyError):
        edition_locks.local_enable_locked("no-such-control")


def test_oss_locks_nothing():
    assert not any(edition_locks.local_enable_locked(c) for c in edition_locks.UNKEYED_LOCKS)
    assert edition_locks.locked_controls() == ()
    assert not edition_locks.config_path_locked("tunnel.enabled")


def test_enterprise_lock_logs_once(enterprise, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.edition_locks"):
        assert edition_locks.local_enable_locked("tunnel")
        assert edition_locks.local_enable_locked("tunnel")
    assert [r.getMessage() for r in caplog.records].count(
        f"enterprise build: {edition_locks.UNKEYED_LOCKS['tunnel']} "
        f"({edition_locks.LOCKED_BY_POLICY})"
    ) == 1


def test_config_paths_name_listed_controls():
    assert set(edition_locks.CONFIG_PATH_LOCKS.values()) <= set(edition_locks.UNKEYED_LOCKS)


def test_enterprise_reports_locked_config_paths(enterprise):
    assert edition_locks.config_path_locked("tunnel.enabled")
    assert edition_locks.config_path_locked("telemetry.otlp_endpoint")
    assert not edition_locks.config_path_locked("telemetry.enabled")


def test_release_channel_file_is_ignored(enterprise, channel_file):
    assert update_layout.release_channel() == "stable"


def test_oss_release_channel_file_still_applies(channel_file):
    assert update_layout.release_channel() == "nightly"


def test_computer_use_opt_in_is_ignored(enterprise):
    assert enable_state.is_enabled({"enabled": True}) is False


def test_oss_computer_use_opt_in_still_applies():
    assert enable_state.is_enabled({"enabled": True}) is True


@pytest.fixture
def default_telemetry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(metrics_provider, "_telemetry_provider", DefaultTelemetryProvider)
    return TelemetryConfig(otlp_endpoint="http://collector.example:4318/v1/metrics")


def test_otlp_endpoint_is_ignored_before_any_provider_reads_it(enterprise, default_telemetry):
    assert metrics_provider._otlp_destinations(default_telemetry) == ()
    assert metrics_provider.otlp_egress_active(default_telemetry) is False


def test_oss_otlp_endpoint_still_applies(default_telemetry):
    assert len(metrics_provider._otlp_destinations(default_telemetry)) == 1
    assert metrics_provider.otlp_egress_active(default_telemetry) is True


def _owner_request(body: object):
    """A request stub the owner-gated update route accepts (same shape as its own tests)."""
    req = MagicMock()

    async def _json() -> object:
        return body

    req.json = _json
    state = MagicMock()
    state.owner_id = ""
    req.app = {"state": state}
    claims = {"app": "", "user": "local-app"}
    req.__contains__.side_effect = lambda key: key in claims
    req.__getitem__.side_effect = lambda key: claims[key] if key in claims else DEFAULT
    req.get.side_effect = lambda key, *default: claims[key] if key in claims else DEFAULT
    return req


def test_channel_switch_api_answers_locked_and_writes_nothing(enterprise, tmp_path, monkeypatch):

    from kiro_crew.platform.update_layout import InstallLayout

    monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)
    monkeypatch.setattr(updates, "resolve_provider", lambda: None)
    wheel = InstallLayout(
        kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
    )
    monkeypatch.setattr(updates, "detect_install_layout", lambda: wheel)
    resp = asyncio.run(updates.api_update_channel(_owner_request({"channel": "nightly"})))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "locked_by_policy"
    assert not (tmp_path / "channel").exists()


def test_schema_marks_locked_config_paths(enterprise):
    req = MagicMock()
    req.query = {}
    entries = json.loads(asyncio.run(api_config_schema(req)).body)["entries"]
    locked = {e["path"] for e in entries if e.get("lockedByPolicy")}
    assert locked == set(edition_locks.CONFIG_PATH_LOCKS)


# ── KIROCREW_DEV_MODE and slack.use_tunnel_url ──


def test_dev_mode_env_does_not_enable_session_environments(enterprise, monkeypatch):
    from kiro_crew import session_environment

    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    assert session_environment.session_environments_enabled() is False


def test_oss_dev_mode_env_still_enables_session_environments(monkeypatch):
    from kiro_crew import session_environment

    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    assert session_environment.session_environments_enabled() is True


def _registers_simulate(monkeypatch, home: str) -> bool:
    from aiohttp import web

    from kiro_crew.dashboard.routes import system

    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    monkeypatch.setenv("KIROCREW_HOME", home)
    app = web.Application()
    system.register(app)
    return any(
        r.method == "POST"
        and r.resource is not None
        and r.resource.canonical == "/api/update/simulate"
        for r in app.router.routes()
    )


def test_dev_switches_do_not_register_the_update_simulator(enterprise, monkeypatch, tmp_path):
    assert _registers_simulate(monkeypatch, str(tmp_path / "crew-dev")) is False


def test_oss_dev_switches_still_register_the_update_simulator(monkeypatch, tmp_path):
    assert _registers_simulate(monkeypatch, str(tmp_path / "crew-dev")) is True


def test_slack_tunnel_opt_in_reads_off(enterprise, monkeypatch):
    from kiro_crew import tunnel
    from kiro_crew.dashboard import urls

    monkeypatch.setattr(tunnel, "get_tunnel_url", lambda: "https://t.example")
    assert urls.slack_tunnel_opt_in(True) is False
    assert urls.tunnel_origin_if_opted_in(True) == ""


def test_oss_slack_tunnel_opt_in_still_applies(monkeypatch):
    from kiro_crew import tunnel
    from kiro_crew.dashboard import urls

    monkeypatch.setattr(tunnel, "get_tunnel_url", lambda: "https://t.example")
    assert urls.slack_tunnel_opt_in(True) is True
    assert urls.tunnel_origin_if_opted_in(True) == "https://t.example"


def _send_link_ensure_calls(monkeypatch) -> int:
    from unittest.mock import AsyncMock, patch

    from kiro_crew.slack import allowlist as al

    ensure = AsyncMock(return_value="connected")
    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D1")
    slack.post_message = AsyncMock()
    cfg = MagicMock()
    cfg.slack.use_tunnel_url = True
    cfg.dashboard.url = "http://127.0.0.1:5476"
    with (
        patch.object(al, "get_tunnel_url", return_value=""),
        patch.object(al, "sel", return_value=MagicMock()),
        patch(
            "kiro_crew.platform.current_context",
            return_value=MagicMock(tunnel=MagicMock(ensure_available=ensure)),
        ),
        patch.object(al.KiroCrewConfig, "load", return_value=cfg),
    ):
        asyncio.run(al.send_dashboard_link(slack, "U1", ttl=600))
    return ensure.await_count


def test_slack_link_does_not_provision_a_tunnel(enterprise, monkeypatch):
    assert _send_link_ensure_calls(monkeypatch) == 0


def test_oss_slack_link_still_offers_the_provisioning_seam(monkeypatch):
    assert _send_link_ensure_calls(monkeypatch) == 1
