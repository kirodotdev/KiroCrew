"""Enterprise build: local switches for controls with no governance key are ignored.

RFC ``rfc-crew-enterprise-edition`` decision D3. Each control in
``kiro_crew.edition_locks.UNKEYED_LOCKS`` is checked at the point its feature reads
its own switch: on, in the ``oss`` edition, the local value still applies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import DEFAULT, MagicMock

import pytest

from kiro_crew import edition_locks
from kiro_crew.apps import dev_mode
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


def test_dev_mode_enable_is_refused(enterprise):
    result = dev_mode.set_dev_mode("some-app", True)
    assert result == {"error": edition_locks.LOCKED_BY_POLICY, "code": "locked_by_policy"}


def test_dev_mode_metadata_is_ignored(enterprise, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dev_mode, "_read_installed", lambda name: SimpleNamespace(dev=True))
    monkeypatch.setattr(dev_mode, "_read_dev_sentinel", lambda: {"some-app"})
    dev_mode._set_dev_cache({"some-app"})
    try:
        assert dev_mode.is_dev_mode("some-app") is False
        assert dev_mode.is_dev_mode_cached("some-app") is False
        assert dev_mode._load_dev_apps() == set()
    finally:
        dev_mode._set_dev_cache(set())


def test_oss_dev_mode_metadata_still_applies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dev_mode, "_read_installed", lambda name: SimpleNamespace(dev=True))
    assert dev_mode.is_dev_mode("some-app") is True


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


def test_cli_dev_mode_refusal_is_audited(enterprise, monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr(
        dev_mode, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: calls.append(kw))
    )
    dev_mode.set_dev_mode("some-app", True)
    assert [(c["operation"], c["outcome"], c["error"]) for c in calls] == [
        ("app_dev_mode", "denied", "locked_by_policy")
    ]


def test_schema_marks_locked_config_paths(enterprise):
    req = MagicMock()
    req.query = {}
    entries = json.loads(asyncio.run(api_config_schema(req)).body)["entries"]
    locked = {e["path"] for e in entries if e.get("lockedByPolicy")}
    assert locked == set(edition_locks.CONFIG_PATH_LOCKS)
