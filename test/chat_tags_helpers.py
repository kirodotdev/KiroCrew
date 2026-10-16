"""The autouse fixture the ``test_chat_tags*.py`` suites share.

It pins the grants-store signing key for every test, and each suite binds it by name
so pytest registers it for that module.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse fixture of test_chat_tags.py and the suites split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _hermetic_signing_secret(monkeypatch):
    """Pin the grants-store provenance key so no test touches the real
    ``token_signing.key`` (lazy creation would pollute the developer's or
    runner's live config home)."""
    from kiro_crew.dashboard import token_secret

    monkeypatch.setattr(token_secret, "_get_secret", lambda: b"test-signing-key")
