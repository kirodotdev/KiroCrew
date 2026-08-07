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
"""

from __future__ import annotations

import os

import pytest

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
