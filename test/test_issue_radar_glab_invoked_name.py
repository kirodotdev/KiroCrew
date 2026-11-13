"""Issue Radar's ``glab`` launch keeps the name a multicall install dispatches on.

``test/test_provider_cli_invoked_name.py`` pins the rule in the shared resolver and
that every core consumer launches whatever it returns, with the ownership model and
the stand-in shell this reuses. This is the same check for the app's own consumer,
``gitlab_client._glab_run``: it carries no code of its own for the rule, so a
per-user ``glab`` link must reach the child under its own name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_provider_cli_invoked_name import (  # noqa: F401
    _REPORT_NAME,
    _SHELL,
    _hermetic_resolution,
    _link,
    _model_ownership,
    _needs_a_plain_shell,
)

from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl

pytestmark = pytest.mark.skipif(os.name != "posix", reason="multicall links are a POSIX layout")


@pytest.fixture
def installed_as(monkeypatch, tmp_path):
    """Install the stand-in behind a user-owned link, as a per-user tool
    distribution does, with the binary behind it the user's too."""

    def install(name: str) -> Path:
        home = tmp_path / "home"
        link = _link(home / "bin", name, _SHELL)
        _model_ownership(monkeypatch, user_owned=(home, _SHELL))
        return link

    return install


@_needs_a_plain_shell
def test_issue_radar_glab(monkeypatch, installed_as):
    monkeypatch.setenv("KIROCREW_ISSUE_RADAR_GLAB", str(installed_as("glab")))
    monkeypatch.setattr(gl, "_glab_bin_cache", None)
    monkeypatch.setattr(gl, "_audit", lambda *args, **kwargs: None)

    proc = gl._glab_run(["glab", "-c", _REPORT_NAME], host="gitlab.com", timeout=30.0)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "glab"
