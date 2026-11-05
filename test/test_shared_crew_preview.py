"""The Shared Crew release gate: ``KIROCREW_SHARED_CREW_PREVIEW=1`` (``shared_crew.preview``)."""

from __future__ import annotations

import json

import pytest

from kiro_crew.config.loader import config_path
from kiro_crew.config.sections_shared_crew import SharedCrewConfig, load_shared_crew_config
from kiro_crew.shared_crew import admission_entry, admission_gate, capabilities
from kiro_crew.shared_crew.preview import PREVIEW_ENV, preview_enabled

#: Every switch on and every limit moved off its default.
_ALL_ON = {
    "admission_enabled": True,
    "hard_limits": True,
    "containment_enabled": False,
    "tailnet_direct": True,
    "local_authorization": True,
    "principal_concurrency": 2,
    "crew_concurrency": 3,
    "typing_idle_seconds": 30,
}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dashboard": {"shared_crew": _ALL_ON}}), encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["", "0", "true", "yes", " 1"])
def test_only_exactly_1_turns_the_preview_on(monkeypatch, value):
    monkeypatch.setenv(PREVIEW_ENV, value)
    assert preview_enabled() is False
    monkeypatch.setenv(PREVIEW_ENV, "1")
    assert preview_enabled() is True


def test_gate_off_ignores_every_switch_in_config(configured, monkeypatch):
    monkeypatch.delenv(PREVIEW_ENV, raising=False)
    assert load_shared_crew_config() == SharedCrewConfig()
    # The stored values are untouched: only the runtime read is gated.
    stored = json.loads(configured.read_text(encoding="utf-8"))
    assert stored["dashboard"]["shared_crew"] == _ALL_ON
    assert admission_gate._configured_hard_limits() is False
    assert admission_gate._load_limits().enabled is False
    assert admission_gate._load_limits().hard_limits is False


def test_gate_on_reads_config_as_before(configured, monkeypatch):
    monkeypatch.setenv(PREVIEW_ENV, "1")
    cfg = load_shared_crew_config()
    assert cfg.admission_enabled is True and cfg.hard_limits is True
    assert cfg.containment_enabled is False and cfg.typing_idle_seconds == 30
    assert admission_gate._configured_hard_limits() is True
    assert admission_gate._load_limits().enabled is True


@pytest.mark.asyncio
async def test_gate_off_app_spawn_passes_through(configured, monkeypatch):
    monkeypatch.delenv(PREVIEW_ENV, raising=False)
    monkeypatch.setattr(admission_gate, "_SERVICE", None)
    admission = await admission_entry.admit_app_spawn(None, "demo")
    assert admission.passthrough


@pytest.mark.parametrize("on", [False, True])
def test_capabilities_report_the_preview(monkeypatch, on):
    if on:
        monkeypatch.setenv(PREVIEW_ENV, "1")
    else:
        monkeypatch.delenv(PREVIEW_ENV, raising=False)
    assert capabilities.aggregate_capabilities().to_dict()["shared_crew_preview"] is on
