"""``slack.shared_access.verify_with_slack``: an install that already shares access keeps its Slack check.

The key's default is ``false`` (list-only), so a config that had sharing on before the
key existed would lose the Slack check on upgrade. A load writes ``true`` into such a
config. A stored value, a disabled section and a config with no section are left alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.migration import MIGRATE_SHARED_ACCESS_VERIFY, shared_access_verify_due


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict, local: dict | None = None):
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps(data), encoding="utf-8")
    localp = tmp_path / "config.local.json"
    if local is not None:
        localp.write_text(json.dumps(local), encoding="utf-8")
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: localp)
    cfg = KiroCrewConfig.load()
    stored = json.loads(cfgp.read_text(encoding="utf-8"))
    return cfg, stored.get("slack", {}).get("shared_access")


def _shared(**fields: Any) -> dict:
    return {"slack": {"shared_access": {"user_ids": ["U0ADA00001"], **fields}}}


def test_an_enabled_config_without_the_key_is_migrated_to_true(tmp_path, monkeypatch) -> None:
    cfg, stored = _load(tmp_path, monkeypatch, _shared(enabled=True))
    assert cfg.slack.shared_access.verify_with_slack is True
    assert stored["verify_with_slack"] is True
    assert stored["enabled"] is True and stored["user_ids"] == ["U0ADA00001"]


def test_the_migration_runs_once_and_a_later_false_is_kept(tmp_path, monkeypatch) -> None:
    _load(tmp_path, monkeypatch, _shared(enabled=True))
    path = tmp_path / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["slack"]["shared_access"]["verify_with_slack"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    assert cfg.slack.shared_access.verify_with_slack is False
    assert (
        json.loads(path.read_text(encoding="utf-8"))["slack"]["shared_access"]["verify_with_slack"]
        is False
    )


@pytest.mark.parametrize("value", [False, True])
def test_a_stored_value_is_kept(tmp_path, monkeypatch, value: bool) -> None:
    cfg, stored = _load(tmp_path, monkeypatch, _shared(enabled=True, verify_with_slack=value))
    assert cfg.slack.shared_access.verify_with_slack is value
    assert stored["verify_with_slack"] is value


def test_a_fresh_config_defaults_to_false_and_gains_no_section(tmp_path, monkeypatch) -> None:
    cfg, stored = _load(tmp_path, monkeypatch, {"agent": {"provider": "acp"}})
    assert cfg.slack.shared_access.verify_with_slack is False
    assert stored is None


def test_a_disabled_config_without_the_key_is_left_untouched(tmp_path, monkeypatch) -> None:
    # Sharing is off, so nothing is admitted and nothing was relying on the check. The
    # dashboard stores the key itself when the owner turns sharing on.
    for enabled in (False, "true"):
        cfg, stored = _load(tmp_path, monkeypatch, _shared(enabled=enabled))
        assert cfg.slack.shared_access.verify_with_slack is False
        assert "verify_with_slack" not in stored


def test_the_overlay_naming_the_key_wins_and_nothing_is_written(tmp_path, monkeypatch) -> None:
    local = {"slack": {"shared_access": {"verify_with_slack": False}}}
    cfg, stored = _load(tmp_path, monkeypatch, _shared(enabled=True), local)
    assert cfg.slack.shared_access.verify_with_slack is False
    assert "verify_with_slack" not in stored


def test_the_due_rule_reads_the_document_only() -> None:
    assert shared_access_verify_due({"slack": {"shared_access": {"enabled": True}}}) is not None
    for data in (
        {},
        {"slack": None},
        {"slack": {"shared_access": []}},
        {"slack": {"shared_access": {"enabled": False}}},
        {"slack": {"shared_access": {"enabled": True, "verify_with_slack": None}}},
    ):
        assert shared_access_verify_due(data) is None
    assert MIGRATE_SHARED_ACCESS_VERIFY == "shared_access_verify"
