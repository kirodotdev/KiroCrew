"""Regression tests for #4418 — a signed local bootstrap subject
(``local-app`` / ``local-startup``) is owner-equivalent even when a (Slack)
``owner_id`` is configured.

Before the fix, ``is_owner_dashboard_request`` (and its siblings
``_authorize_owner_request`` and ``kiro_prerequisite._is_dashboard_owner``)
accepted the bootstrap subjects ONLY when no ``owner_id`` was configured. Because
a browser session's subject is baked at mint time and never re-derived from
``owner_id``, setting ``KIROCREW_OWNER_ID`` afterward locked a bootstrap-seeded
dashboard out of every owner-gated endpoint (e.g. ``POST /api/chat/mode``).
"""

from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import kiro_prerequisite
from kiro_crew.dashboard.handlers import source_providers as source


class _FakeRequest:
    """Minimal stand-in matching the attribute surface the predicates read."""

    def __init__(self, *, user="U_OWNER", app="", owner_id="U_OWNER", has_app=True):
        state = MagicMock()
        state.owner_id = owner_id
        self.app = {"state": state}
        self._claims = {"user": user}
        if has_app:
            self._claims["app"] = app

    def get(self, key, default=None):
        return self._claims.get(key, default)

    def __getitem__(self, key):
        return self._claims[key]

    def __contains__(self, key):
        return key in self._claims


# (user, app, owner_id, has_app) -> expected owner?
_MATRIX = [
    # The regression: owner configured, session carries a bootstrap subject.
    ("local-app", "", "U_OWNER", True, True),
    ("local-startup", "", "U_OWNER", True, True),
    # Owner configured, session carries the owner subject.
    ("U_OWNER", "", "U_OWNER", True, True),
    # Owner configured, a different (non-owner) dashboard subject.
    ("U_OTHER", "", "U_OWNER", True, False),
    # No owner configured — bootstrap subject accepted (unchanged behavior).
    ("local-app", "", "", True, True),
    ("U_OTHER", "", "", True, False),
    # App tokens are NEVER the owner, even carrying a bootstrap-looking subject
    # (this is the #3836 Finding-02 guard — must stay closed).
    ("local-app", "some-app", "U_OWNER", True, False),
    ("local-app", "some-app", "", True, False),
    # Missing user claim / missing app claim => not the owner.
    ("", "", "U_OWNER", True, False),
    ("local-app", "", "U_OWNER", False, False),
]


@pytest.mark.parametrize(("user", "app", "owner_id", "has_app", "expected"), _MATRIX)
def test_is_owner_dashboard_request(user, app, owner_id, has_app, expected):
    req = _FakeRequest(user=user, app=app, owner_id=owner_id, has_app=has_app)
    assert source.is_owner_dashboard_request(req) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize(("user", "app", "owner_id", "has_app", "expected"), _MATRIX)
def test_is_dashboard_owner_kiro_prerequisite(user, app, owner_id, has_app, expected):
    req = _FakeRequest(user=user, app=app, owner_id=owner_id, has_app=has_app)
    assert kiro_prerequisite._is_dashboard_owner(req) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize("subject", ["local-app", "local-startup"])
def test_authorize_owner_request_accepts_bootstrap_subject_with_owner_set(
    monkeypatch, subject
):
    """The #4418 fix: with an owner configured, a signed bootstrap subject is
    authorized rather than 403'd."""
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    req = _FakeRequest(user=subject, app="", owner_id="U_OWNER")
    assert source._authorize_owner_request(req, "op") is None  # type: ignore[arg-type]


def test_authorize_owner_request_still_rejects_non_owner(monkeypatch):
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    req = _FakeRequest(user="U_OTHER", app="", owner_id="U_OWNER")
    resp = source._authorize_owner_request(req, "op")  # type: ignore[arg-type]
    assert resp is not None and resp.status == 403


def test_authorize_owner_request_still_rejects_app_token(monkeypatch):
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    req = _FakeRequest(user="local-app", app="some-app", owner_id="U_OWNER")
    resp = source._authorize_owner_request(req, "op")  # type: ignore[arg-type]
    assert resp is not None and resp.status == 403


def test_authorize_owner_request_no_owner_mutation_lockdown_preserved(monkeypatch):
    """With no owner configured, a bootstrap subject is still refused when
    ``allow_local_no_owner`` is False (the deliberate mutation lockdown)."""
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    req = _FakeRequest(user="local-app", app="", owner_id="")
    resp = source._authorize_owner_request(req, "op", allow_local_no_owner=False)
    assert resp is not None and resp.status == 403


def test_authorize_owner_request_no_owner_readonly_allowed(monkeypatch):
    """With no owner and ``allow_local_no_owner`` True, bootstrap subject passes
    (unchanged read-only behavior)."""
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    req = _FakeRequest(user="local-startup", app="", owner_id="")
    assert (
        source._authorize_owner_request(req, "op", allow_local_no_owner=True) is None
    )  # type: ignore[arg-type]
