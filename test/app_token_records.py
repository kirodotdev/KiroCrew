"""Let a test's app tokens pass the installed-and-enabled check without a real record.

``validate_token_with_app`` refuses an app token unless ``installed.json`` says the
app is installed and enabled (``apps.manager.app_token_cutoff``). Tests that mint
app tokens for a fixture app with no record on disk call :func:`mark_apps_live`
so they keep testing what they test. The check itself is pinned by
``test_app_token_disabled_app.py``, which never calls this.
"""

from __future__ import annotations

from collections.abc import Container

import pytest


def mark_apps_live(monkeypatch: pytest.MonkeyPatch, names: Container[str] | None = None) -> None:
    """Treat *names* (every app when None) as installed and enabled with no cutoff."""

    def cutoff(name: str) -> float | None:
        return 0.0 if names is None or name in names else None

    monkeypatch.setattr("kiro_crew.apps.manager.app_token_cutoff", cutoff)
