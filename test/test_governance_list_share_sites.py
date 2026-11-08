"""``capabilities.social_share`` ``sites``: which share sites the dialog may offer.

RFC crew-enterprise-edition, appendix row "Share to sites". The key's parse and
evaluator contract is in ``test_governance_list_keys.py``; this file covers the
call site: ``social_share.permitted_sites``, which ``GET /api/dashboard/config``
reports.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.platform.governance import parse_policy


def _policy(scope: str, body: dict) -> dict:
    return {
        "version": 1,
        "boot": {"fail_closed": True},
        "capabilities": {scope.split(".", 1)[1]: body},
    }


def _allow(scope: str, key: str, allowed: str) -> dict:
    return _policy(scope, {"scopes": {key: {"mode": "allow", "allow": [allowed]}}})


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_sel(monkeypatch):
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return fake


def _install_policy(monkeypatch, doc: dict | None) -> None:
    from kiro_crew.metrics import provider
    from kiro_crew.platform import context as pc

    ceiling = parse_policy(doc) if doc is not None else None
    ctx = SimpleNamespace(governance=ceiling)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    # ``metrics.provider`` binds the name at import, so it is patched there too.
    monkeypatch.setattr(provider, "current_context", lambda: ctx)


# ── The call sites ──────────────────────────────────────────────────────


class TestShareSites:
    _SCOPE = "capabilities.social_share"

    def test_only_listed_sites_are_offered(self, isolated_home, fake_sel, monkeypatch) -> None:
        from kiro_crew.dashboard import social_share

        _install_policy(monkeypatch, _allow(self._SCOPE, "sites", "linkedin"))
        assert social_share.permitted_sites() == ["linkedin"]

    def test_a_gate_pinned_off_offers_none(self, isolated_home, fake_sel, monkeypatch) -> None:
        from kiro_crew.dashboard import social_share

        _install_policy(monkeypatch, _policy(self._SCOPE, {"enabled": False}))
        assert social_share.permitted_sites() == []

    def test_no_policy_offers_every_site(self, isolated_home, fake_sel, monkeypatch) -> None:
        from kiro_crew.dashboard import social_share

        _install_policy(monkeypatch, None)
        assert social_share.permitted_sites() == list(social_share.SHARE_SITES)

    def test_an_unexpected_probe_error_offers_none(
        self, isolated_home, fake_sel, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import social_share

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(social_share, "vet_and_audit", _boom)
        assert social_share.permitted_sites() == []
