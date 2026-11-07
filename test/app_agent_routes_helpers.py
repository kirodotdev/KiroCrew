"""Shared fixtures and builders for the app agent-route test modules."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolated_token_state(tmp_path: Any, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep token issuance off the real data home, as ``test_token_auth`` does."""
    import kiro_crew.dashboard.revocation_gen as _rg
    import kiro_crew.dashboard.token_auth as _ta

    _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(_rg, "_gen", 0)
    _floor_monkeypatch.setattr(_ta, "_revoked_store_singleton", None)
    _ta._state.clear_all()
    yield
    _ta._state.clear_all()


def _manifest(**extra: Any) -> dict[str, Any]:
    return {
        "name": "slack-poller",
        "version": "1.0.0",
        "displayName": "Slack Poller",
        "description": "Polls channels.",
        **extra,
    }
