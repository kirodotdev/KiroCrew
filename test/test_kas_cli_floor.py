"""The runtime floor that keeps a host on kiro-cli when its kiro-cli cannot serve KAS.

``kirocrew doctor`` reads ``kiro-cli acp --help`` to report whether the KAS engine
exists. The same probe gates boot: KAS configured on a kiro-cli without the engine
falls back to kiro-cli with a logged notice, and the dashboard's KAS row names what
the update must bring.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

from kiro_crew import kiro_cli
from kiro_crew.acp.kas_transport import KAS_RELAY_ENGINE, KAS_RELAY_ENGINE_FLAG
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk import backends as registry
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.platform import bootstrap

KAS = registry.ACP_BACKEND_KAS
KIRO = registry.ACP_BACKEND_KIRO

_OLD_HELP = "Usage: kiro-cli acp [OPTIONS]\n  --trust-all-tools\n"
_NEW_HELP = f"Usage: kiro-cli acp [OPTIONS]\n  {KAS_RELAY_ENGINE_FLAG} <ENGINE>  [possible values: v2, {KAS_RELAY_ENGINE}]\n"
_OTHER_ENGINE_HELP = (
    f"Usage: kiro-cli acp [OPTIONS]\n  {KAS_RELAY_ENGINE_FLAG} <ENGINE>  [possible values: v2]\n"
)


@pytest.fixture(autouse=True)
def _restore_registry():
    baseline = set(registry._baseline)
    selectable = set(registry._selectable)
    unservable = dict(registry._host_unservable)
    acp_driver._kas_help_cache.clear()
    backend_install.clear_probe_cache()
    yield
    registry._baseline.clear()
    registry._baseline.update(baseline)
    registry._selectable.clear()
    registry._selectable.update(selectable)
    registry._host_unservable.clear()
    registry._host_unservable.update(unservable)
    acp_driver._kas_help_cache.clear()
    backend_install.clear_probe_cache()


@pytest.fixture
def kas_selectable(monkeypatch):
    monkeypatch.setattr(registry, "_baseline", {KIRO, KAS})
    monkeypatch.setattr(registry, "_selectable", {KIRO, KAS})
    monkeypatch.setattr(registry, "_host_unservable", {})


@pytest.fixture
def pinned_cli(tmp_path, monkeypatch):
    binary = tmp_path / "kiro-cli"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))
    return binary


def _help(monkeypatch, text):
    calls = []

    def _fake(binary):
        calls.append(binary)
        return text

    monkeypatch.setattr(kiro_cli, "kas_relay_help", _fake)
    return calls


def _cfg(acp_backend=KAS, member_acp_backend=""):
    return SimpleNamespace(
        agent=SimpleNamespace(acp_backend=acp_backend, member_acp_backend=member_acp_backend)
    )


class TestUnsupportedReason:
    def test_help_without_the_engine_flag_is_a_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, _OLD_HELP)
        reason = acp_driver.kas_engine_unsupported_reason()
        assert reason == f"this kiro-cli has no {KAS_RELAY_ENGINE_FLAG} flag"

    def test_flag_without_the_kas_engine_is_a_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, _OTHER_ENGINE_HELP)
        reason = acp_driver.kas_engine_unsupported_reason()
        assert reason == f"this kiro-cli does not offer engine {KAS_RELAY_ENGINE}"

    def test_supported_cli_has_no_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, _NEW_HELP)
        assert acp_driver.kas_engine_unsupported_reason() is None

    def test_failed_probe_is_unknown_not_a_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, None)
        assert acp_driver.kas_engine_unsupported_reason() is None

    def test_no_pinned_cli_never_spawns(self, monkeypatch):
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, False))
        calls = _help(monkeypatch, _OLD_HELP)
        assert acp_driver.kas_engine_unsupported_reason() is None
        assert calls == []

    def test_probe_is_cached_until_the_binary_changes(self, monkeypatch, pinned_cli):
        calls = _help(monkeypatch, _OLD_HELP)
        acp_driver.kas_engine_unsupported_reason()
        acp_driver.kas_engine_unsupported_reason()
        assert len(calls) == 1
        stat = os.stat(pinned_cli)
        os.utime(pinned_cli, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        acp_driver.kas_engine_unsupported_reason()
        assert len(calls) == 2


class TestRegistryMark:
    def test_mark_drops_the_backend_and_survives_a_policy_recompute(self, kas_selectable):
        registry.mark_backend_unservable(KAS, "too old")
        assert KAS not in registry.selectable_backends()
        # A policy recompute that denies nothing must not restore it.
        assert registry.apply_selectable_denials(set()) == frozenset()
        assert KAS not in registry.selectable_backends()
        assert registry.host_unservable_reason(KAS) == "too old"

    def test_the_floor_backend_is_never_marked(self, kas_selectable):
        registry.mark_backend_unservable(KIRO, "nope")
        assert KIRO in registry.selectable_backends()
        assert registry.host_unservable_reason(KIRO) is None

    def test_resolving_a_marked_backend_degrades_without_the_generic_warning(
        self, kas_selectable, caplog
    ):
        registry.mark_backend_unservable(KAS, "too old")
        with caplog.at_level(logging.WARNING):
            assert registry.resolve_selected_backend(KAS) == KIRO
        assert "not selectable in this build" not in caplog.text


class TestBootFloor:
    def test_old_cli_falls_back_to_kiro_with_a_notice(
        self, monkeypatch, pinned_cli, kas_selectable, caplog
    ):
        _help(monkeypatch, _OLD_HELP)
        cfg = _cfg()
        with caplog.at_level(logging.WARNING, logger=bootstrap.logger.name):
            notice = bootstrap.apply_kas_cli_floor(cfg)
        assert cfg.agent.acp_backend == KIRO
        assert KAS not in registry.selectable_backends()
        assert notice is not None
        assert "Kiro Crew is using kiro-cli instead" in notice
        assert f"no {KAS_RELAY_ENGINE_FLAG} flag" in notice
        assert notice in caplog.text

    def test_supported_cli_keeps_kas(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, _NEW_HELP)
        cfg = _cfg()
        assert bootstrap.apply_kas_cli_floor(cfg) is None
        assert cfg.agent.acp_backend == KAS
        assert KAS in registry.selectable_backends()

    def test_unknown_verdict_keeps_kas(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, None)
        cfg = _cfg()
        assert bootstrap.apply_kas_cli_floor(cfg) is None
        assert cfg.agent.acp_backend == KAS

    def test_member_backend_alone_triggers_the_floor(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, _OLD_HELP)
        cfg = _cfg(acp_backend=KIRO, member_acp_backend=KAS)
        assert bootstrap.apply_kas_cli_floor(cfg) is not None
        assert registry.resolve_selected_backend(KAS) == KIRO

    def test_kas_not_configured_never_probes(self, monkeypatch, pinned_cli, kas_selectable):
        calls = _help(monkeypatch, _OLD_HELP)
        assert bootstrap.apply_kas_cli_floor(_cfg(acp_backend=KIRO)) is None
        assert calls == []
        assert KAS in registry.selectable_backends()


class TestBackendPanelRow:
    def test_component_spells_the_transport_constants(self):
        assert backend_install.COMPONENT_KAS_ENGINE == (
            f"{backend_install.COMPONENT_KIRO_CLI} acp {KAS_RELAY_ENGINE_FLAG} {KAS_RELAY_ENGINE}"
        )

    def test_marked_kas_row_names_the_missing_engine(self, monkeypatch, kas_selectable):
        monkeypatch.setattr(backend_install.acp_driver, "kiro_cli_resolves", lambda: True)
        registry.mark_backend_unservable(KAS, "too old")
        state = backend_install.probe_backend(KAS)
        assert state.installed == backend_install.MISSING
        assert state.missing_components == (backend_install.COMPONENT_KAS_ENGINE,)
        assert backend_install.probe_backend(KIRO).installed == backend_install.INSTALLED

    def test_unmarked_kas_row_follows_kiro(self, monkeypatch, kas_selectable):
        monkeypatch.setattr(backend_install.acp_driver, "kiro_cli_resolves", lambda: True)
        assert backend_install.probe_backend(KAS).installed == backend_install.INSTALLED
