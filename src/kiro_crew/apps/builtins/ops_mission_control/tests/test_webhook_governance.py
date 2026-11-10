"""The signal webhook asks the core governance gate for its own source id first.

The ``webhooks`` scope (``kiro_crew.webhook_sources``) names the inbound sources a
policy permits, and ``ops-mission-control`` is this app's. The webhook provider asks
:func:`kiro_crew.webhook_sources.source_refusal` before its own switch, token or
signature checks, so a policy that does not name the source refuses it, and an
LTS build refuses it with no policy at all (RFC crew-lts-edition,
appendix Red "Webhooks by source").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook
from kiro_crew.platform.governance import parse_policy

EDITIONS = ("oss", "lts")


def _set_edition(monkeypatch: pytest.MonkeyPatch, value: str) -> bool:
    monkeypatch.setattr("kiro_crew.edition.EDITION", value)
    return value == "lts"


def _install_policy(monkeypatch: pytest.MonkeyPatch, *allowed: str) -> None:
    from kiro_crew.platform import context as pc
    from kiro_crew.platform import governance_profiles

    doc = {
        "version": 1,
        "boot": {"fail_closed": True},
        "webhooks": {"mode": "allow", "allow": list(allowed)},
    }
    ctx = SimpleNamespace(governance=parse_policy(doc))
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: None)


@pytest.mark.parametrize("edition", EDITIONS)
def test_the_webhook_source_is_off_in_lts(monkeypatch, edition) -> None:
    lts = _set_edition(monkeypatch, edition)
    monkeypatch.setattr(webhook, "provider_enabled", lambda _id: False)

    accepted, detail = webhook.enqueue(b"{}", "sha256=x")

    assert accepted is False
    assert ("LTS edition" in detail) is lts


@pytest.mark.parametrize("edition", EDITIONS)
def test_the_webhook_asks_its_own_source(monkeypatch, edition) -> None:
    _set_edition(monkeypatch, edition)
    monkeypatch.setattr(webhook, "provider_enabled", lambda _id: False)
    _install_policy(monkeypatch, "agent")
    accepted, detail = webhook.enqueue(b"{}", "sha256=x")
    assert accepted is False
    assert "refused by the security policy" in detail
    # Named: the app's own checks answer next.
    _install_policy(monkeypatch, "ops-mission-control")
    assert webhook.enqueue(b"{}", "sha256=x") == (False, "webhook source is not enabled")
