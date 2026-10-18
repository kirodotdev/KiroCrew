"""Fixtures and helpers the ``test_cron_secret_env*.py`` suites share.

``_cron_caller_is_named`` and ``cron_home`` are autouse: every test runs with a nameable
cron caller and with ``cron_script.config_dir`` resolving under the patched home, or under
a per-test tmp dir when the home is not patched. ``_grant_script`` plants a grantee script
(``_GRANTEE_BODY`` by default) where that resolver looks and returns its spec.
``test_cron_secret_env.py`` and ``test_cron_secret_env_grant_endpoint.py`` import both
fixture functions (``cron_home`` through ``cron_home_fixture``, which declares that name) so
pytest registers them in each module, along with the names they read.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Cron writes require a nameable caller; these tests assume it."""


@pytest.fixture(name="cron_home", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no importing module calls monkeypatch.undo()
def cron_home_fixture(monkeypatch, tmp_path):
    """Point cron_script.config_dir at the per-test patched home.

    Same redirect as test_cron_script.py: scripts are written under
    ``<home>/.kirocrew/crons`` and the vault lives beside them, so the
    resolver, the pin computation, and the vault all agree on one root.

    Tests that do NOT patch ``Path.home`` still reach this resolver through
    the grant-pin key (every ``compute_secret_env_pin`` call), so those fall
    back to a per-test tmp dir — never the operator's real ``~/.kirocrew``,
    which this fixture must not create, read, or key. Returns the resolver
    callable so tests can plant script files where the resolver looks.
    """
    real_home = Path.home()
    fallback = tmp_path / "kirocrew-home-fallback"

    def _dir() -> Path:
        home = Path.home()
        root = fallback if home == real_home else home / ".kirocrew"
        # The real ``config_dir`` resolves AND creates the home on every call
        # (``mkdir(parents=True, exist_ok=True)``), so a stub that handed back a
        # non-existent directory would model a state production cannot be in --
        # and any caller deriving a path that must EXIST under the home would
        # then fail for a reason the stub invented.
        root.mkdir(parents=True, exist_ok=True)
        return root

    monkeypatch.setattr("kiro_crew.cron_script.config_dir", _dir)
    return _dir


_GRANTEE_BODY = "def run(ctx): pass\n"


def _grant_script(cron_home, body: str = _GRANTEE_BODY, name: str = "grantee.py") -> str:
    """Plant a script where cron_script's resolver looks; return its spec.

    For tests that do NOT patch ``Path.home`` (endpoint/store/MCP flows):
    grants apply only to script jobs, so every grant fixture needs a real,
    resolvable script file.
    """
    crons_dir = cron_home() / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / name
    # Bytes, not text: the approval digest is over the bytes ON DISK, and a
    # text-mode write would land ``\r\n`` on Windows and break that equality.
    script.write_bytes(body.encode())
    return str(script) + ":run"
