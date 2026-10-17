"""Shared fixtures and builders for the app manager suite (``test_app_manager.py`` and
its ``test_app_manager_<topic>.py`` parts): a minimal app source, the install's
root-``data`` refusal sentences, and an isolated ``KIROCREW_HOME``.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: The install's own refusal for a root `data` the data directory cannot stand
#: beside -- the one explanation the install log, the AppResult and the audit
#: record carry, so the tests pin the sentence, not a fragment of it.
_DATA_IS_A_FILE = (
    "`data` in the app tree is a file; Kiro Crew creates the app's data directory at "
    "that path and cannot install beside it."
)


_DATA_IS_A_LINK = _DATA_IS_A_FILE.replace("is a file", "is a link")


def _make_app_source(tmp_path, name="test-app", **manifest_overrides):
    """Create a minimal app source directory with a valid app.json."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app for unit tests",
        "author": "tester",
        **manifest_overrides,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


@pytest.fixture(name="app_home")
def app_home_fixture(tmp_path, monkeypatch):
    """Set KIROCREW_HOME to a temp directory for isolated testing."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Lifecycle success tests explicitly admit their synthetic third-party apps.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home
