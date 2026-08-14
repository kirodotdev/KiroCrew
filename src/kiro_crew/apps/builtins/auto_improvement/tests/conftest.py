"""Test-suite fixtures for the auto-improvement builtin.

## Why a git identity is forced here

Several tests drive PRODUCTION commit paths (``backend/commit.py``, the driver's provisional
commit, the watcher's export) against a throwaway repo. Those call ``git commit`` without
setting ``user.name``/``user.email`` — correctly, because in production the operator's own git
config supplies them.

On a developer workstation that always works even with no config file, because git falls back
to an identity derived from the SYSTEM ACCOUNT (the gecos full name plus ``user@hostname``).
A CI runner has no gecos name, so the same code fails there with::

    Author identity unknown
    *** Please tell me who you are.

That asymmetry is why these files were originally excluded from CI collection, and why an
attempt to verify them locally by emptying ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` was
unsound: it removed the config files but not the account fallback, so the suite passed locally
and still failed in CI. Measured: ``git var GIT_AUTHOR_IDENT`` with both config paths sent to
``/dev/null`` still returned a full identity from the account.

Setting the identity through the ENVIRONMENT fixes it for every subprocess without touching
any repository's config, and mirrors what several tests in this directory already do inline
(``test_suite_scope.py``, ``test_dogfood_learnings.py``). ``autouse`` + ``session`` scope so a
test that shells out to git cannot forget it. Deliberately NOT a real address, so a commit
that escaped a temporary directory would be obvious in a log.

## Why the data home is redirected here

``store.data_dir()`` resolves to the OPERATOR's live app directory, so a test that writes
``store.config_path()`` without redirecting it replaces the repository the app is configured
against. ``write_json_atomic`` REPLACES the document rather than merging, so the live
``target_url``/``target_display`` are dropped and ``clone`` is left naming a pytest temporary
directory that is reaped when the session ends. The app's page then renders with no repository,
and calibration refuses because the configured clone no longer exists.

Nothing in the suite fails when that happens — the damage lands outside the assertions — so the
redirect is ``autouse``: a per-file opt-in fixture only protects the files that remember to ask
for it. Several files here already define their own ``data_home`` fixture; those still work,
because a function-scoped ``monkeypatch.setattr`` on the same attribute simply wins over this
one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ..backend import store

#: Git reads these before falling back to config files or the system account, so they make the
#: identity explicit and hermetic on every host.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "auto-improvement tests",
    "GIT_AUTHOR_EMAIL": "tests@auto-improvement.invalid",
    "GIT_COMMITTER_NAME": "auto-improvement tests",
    "GIT_COMMITTER_EMAIL": "tests@auto-improvement.invalid",
}


@pytest.fixture(scope="session", autouse=True)
def _git_identity_for_production_commit_paths() -> None:
    """Give every ``git commit`` in this suite an identity, on any host.

    ``os.environ`` directly rather than ``monkeypatch``: that fixture is function-scoped and
    this has to hold for the whole session, including subprocesses launched from module-level
    helpers. Pre-existing values are left alone so a developer who has deliberately exported
    an identity keeps it.
    """
    for key, value in _GIT_IDENTITY.items():
        os.environ.setdefault(key, value)


@pytest.fixture(autouse=True)
def _isolated_app_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's data root and clone scratch at this test's temp directory.

    Only ``data_dir`` is redirected, not ``workspace_dir``: every other path in
    :mod:`..backend.store` derives from it, so one seam covers config, the per-repo
    subtree, sessions and logs. Files that additionally want ``workspace_dir ==
    data_dir`` (so flat fixture paths and the per-repo layout coincide) keep pinning
    it themselves.

    ``AUTO_IMPROVEMENT_SCRATCH`` moves clones out of ``~/.autoimprove-scratch`` for the
    same reason: a test that clones into the real scratch root leaves a tree the app
    then refuses to reuse, because setup compares the requested URL against the
    push-neutralized origin the app itself wrote.

    ``KIROCREW_HOME`` is the outer fence, and it is the one that matters most. Patching
    ``data_dir`` only redirects callers that go through :mod:`..backend.store`, while
    ``do_not_pollute_paths`` reaches the operator's home directly through
    ``config.loader.config_dir()`` — and that resolver is not a read: on the default
    path it migrates a legacy home, writes a recovery breadcrumb outside ``~/.kiro/``,
    and sweeps archive leftovers. Pointing the variable at a temp directory redirects
    every reader of the home, including the ones no fixture knows about.
    """
    data = tmp_path / "app-data-home"
    data.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "kirocrew-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(store, "data_dir", lambda: data)
    monkeypatch.setenv("AUTO_IMPROVEMENT_SCRATCH", str(tmp_path / "app-scratch"))
    return data
