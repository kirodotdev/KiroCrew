"""An app refused ``permissions.sessionApproval`` is told which part is missing, and the fix.

The grant needs the flag in the manifest, the owner's consent and the app enabled.
``session_approval_denial_cause`` names the missing part, the same decision
``app_can_manage_session_approvals`` grants from, and the ``403
session_approval_not_granted`` body words it: the ``app.json`` change, the owner's
"Enable and allow chat control", the owner's "Enable", or a retry.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.apps import permissions
from kiro_crew.apps.manager import InstalledApp
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard import session_approval_refusal as refusal

APP = "ledger-lens"


def _manifest(session_approval: bool) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "displayName": "Ledger Lens",
            "description": "session approval refusal fixture",
            "permissions": {"sessionApproval": session_approval},
        }
    )


def _stub(monkeypatch, *, declared=True, record=True, pending=False, enabled=True, live=True):
    manifest = _manifest(declared)
    monkeypatch.setattr(permissions, "get_app_manifest", lambda name: manifest)
    meta = InstalledApp(name=APP, enabled=enabled)
    meta.sessionApprovalConsentPending = pending
    monkeypatch.setattr(
        permissions, "_read_installed", lambda name, **_kw: meta if record else None
    )
    monkeypatch.setattr(permissions, "is_app_enabled", lambda name: live)


@pytest.mark.parametrize(
    ("state", "cause"),
    [
        ({}, ""),
        ({"declared": False}, "not_declared"),
        ({"pending": True, "enabled": False}, "consent_pending"),
        ({"enabled": False}, "disabled"),
        ({"record": False}, "unavailable"),
        ({"live": False}, "unavailable"),
    ],
)
def test_the_cause_and_the_grant_are_one_decision(monkeypatch, state, cause):
    _stub(monkeypatch, **state)

    assert permissions.session_approval_denial_cause(APP) == cause
    assert permissions.app_can_manage_session_approvals(APP) is (cause == "")


def test_an_unreadable_or_absent_manifest_is_unavailable(monkeypatch):
    monkeypatch.setattr(permissions, "get_app_manifest", lambda name: None)
    assert permissions.session_approval_denial_cause(APP) == "unavailable"

    def _boom(name):
        raise OSError("unreadable")

    monkeypatch.setattr(permissions, "get_app_manifest", _boom)
    assert permissions.session_approval_denial_cause(APP) == "unavailable"
    assert permissions.session_approval_denial_cause("") == "unavailable"
    assert permissions.app_can_manage_session_approvals(APP) is False


def _body(monkeypatch, **state):
    _stub(monkeypatch, **state)
    response = asyncio.run(refusal.session_approval_refusal(APP))
    assert response.status == 403
    return json.loads(response.body)


def test_an_undeclared_grant_names_the_manifest_change_and_the_owner_step(monkeypatch):
    body = _body(monkeypatch, declared=False)

    assert set(body) == {"error", "code", "cause"}
    assert body["code"] == "session_approval_not_granted"
    assert body["cause"] == "not_declared"
    assert body["error"].startswith(f'App "{APP}" cannot approve tool requests')
    assert '"sessionApproval": true' in body["error"]
    assert f"kirocrew app update {APP}" in body["error"]
    assert "registers its new manifest again" in body["error"]
    assert f"/apps/detail/{APP}" in body["error"]
    assert '"Enable and allow chat control"' in body["error"]


def test_a_pending_consent_names_the_owner_step(monkeypatch):
    body = _body(monkeypatch, pending=True, enabled=False)

    assert body["cause"] == "consent_pending"
    assert '"Enable and allow chat control"' in body["error"]
    assert f"/apps/detail/{APP}" in body["error"]
    assert 'add "sessionApproval"' not in body["error"]


def test_a_disabled_app_names_the_enable_button(monkeypatch):
    body = _body(monkeypatch, enabled=False)

    assert body["cause"] == "disabled"
    assert '"Enable"' in body["error"]
    assert "chat control" not in body["error"]


def test_a_grant_that_reads_as_held_by_the_refusal_asks_for_a_retry(monkeypatch):
    """The state moved between the refusing read and this one: no cause is claimed."""
    body = _body(monkeypatch)

    assert body["cause"] == "unavailable"
    assert f"kirocrew app info {APP}" in body["error"]
