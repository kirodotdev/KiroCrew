"""An app's inbound webhook is a ``webhooks`` source under its own name.

An app declares ``permissions.webhookSource`` in ``app.json``; core then answers the
``webhooks`` governance scope for it under the app's name (``webhook_sources``), and
its hook context carries ``ctx.webhook_source``. These cases pin the declaration's
parse, the refusal order (core id, declaration, policy), the live re-read, the
context gate, the install summary line, and that a shipped built-in is read from the
wheel before its record exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 -- requested by name

from kiro_crew import webhook_sources
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.webhook_source_sdk import WebhookSourceSDK
from kiro_crew.platform.governance import parse_policy

APP = "fixture-hooked"


def _install(home: Path, name: str, permissions: dict) -> Path:
    app = home / "apps" / name
    app.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "1.0.0", "permissions": permissions}
    path = app / "app.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _set_edition(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", value)


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


@pytest.mark.parametrize(
    ("value", "declared"), [(True, True), (False, False), ("true", False), (1, False)]
)
def test_only_the_literal_true_declares_a_webhook_source(value, declared):
    perms = Permissions.from_dict({"webhookSource": value})
    assert perms.webhookSource is declared
    assert ("webhookSource" in perms.to_dict()) is declared


def test_an_undeclared_app_is_refused_before_the_policy_is_asked(app_home, monkeypatch):
    _install(app_home, APP, {"api": []})
    asked: list[str] = []
    monkeypatch.setattr(webhook_sources, "source_refusal", lambda s: asked.append(s) or "")
    assert "declares no webhook source" in webhook_sources.app_source_refusal(APP)
    assert asked == []


def test_an_absent_app_declares_nothing(app_home):
    assert "declares no webhook source" in webhook_sources.app_source_refusal("fixture-absent")


def test_a_declared_app_is_governed_under_its_own_name(app_home, monkeypatch):
    _install(app_home, APP, {"webhookSource": True})
    asked: list[str] = []
    monkeypatch.setattr(webhook_sources, "source_refusal", lambda s: asked.append(s) or "")
    assert webhook_sources.app_source_refusal(APP) == ""
    assert asked == [APP]


@pytest.mark.parametrize("edition", ("oss", "lts"))
def test_the_policy_names_the_app_to_permit_it(app_home, monkeypatch, edition):
    _install(app_home, APP, {"webhookSource": True})
    _set_edition(monkeypatch, edition)
    _install_policy(monkeypatch, "agent")
    assert "refused by the security policy" in webhook_sources.app_source_refusal(APP)
    _install_policy(monkeypatch, APP)
    assert webhook_sources.app_source_refusal(APP) == ""


def test_an_app_named_as_a_core_source_is_never_that_source(app_home, monkeypatch):
    for core in webhook_sources.CORE_SOURCES:
        _install(app_home, core, {"webhookSource": True})
        monkeypatch.setattr(webhook_sources, "source_refusal", lambda _s: "")
        assert "core webhook source id" in webhook_sources.app_source_refusal(core)


def test_the_context_carries_the_seam_only_when_declared(app_home):
    declared = build_app_context(APP, app_home, permissions={"webhookSource": True})
    assert isinstance(declared.webhook_source, WebhookSourceSDK)
    assert declared.webhook_source.source == APP
    for perms in ({}, {"webhookSource": "true"}, {"webhookSource": False}):
        assert build_app_context(APP, app_home, permissions=perms).webhook_source is None


def test_the_seam_rereads_the_live_declaration(app_home, monkeypatch):
    path = _install(app_home, APP, {"webhookSource": True})
    monkeypatch.setattr(webhook_sources, "source_refusal", lambda _s: "")
    sdk = build_app_context(APP, app_home, permissions={"webhookSource": True}).webhook_source
    assert sdk is not None and sdk.refusal() == ""
    path.write_text(json.dumps({"name": APP, "version": "1.0.0"}), encoding="utf-8")
    assert "declares no webhook source" in sdk.refusal()


@pytest.mark.asyncio
async def test_the_async_answer_is_the_same(app_home, monkeypatch):
    _install(app_home, APP, {"webhookSource": True})
    monkeypatch.setattr(webhook_sources, "source_refusal", lambda _s: "refused")
    assert await WebhookSourceSDK(APP).refusal_async() == "refused"


def test_the_install_summary_names_the_source():
    from kiro_crew.apps.permissions import format_permissions_summary

    manifest = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", "permissions": {"webhookSource": True}}
    )
    assert f"webhook source '{APP}'" in format_permissions_summary(manifest)
    bare = AppManifest.from_dict({"name": APP, "version": "1.0.0"})
    assert "Webhooks" not in format_permissions_summary(bare)


def test_a_shipped_builtin_is_read_from_the_wheel_before_its_record(app_home, monkeypatch):
    from kiro_crew.apps import execution

    shipped = app_home / "wheel" / "fixture_shipped"
    shipped.mkdir(parents=True)
    (shipped / "app.json").write_text(
        json.dumps(
            {"name": "fixture-shipped", "version": "1.0.0", "permissions": {"webhookSource": True}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        execution,
        "shipped_builtin_app_root",
        lambda name: shipped if name == "fixture-shipped" else None,
    )
    monkeypatch.setattr(webhook_sources, "source_refusal", lambda _s: "")
    assert webhook_sources.app_declares_webhook_source("fixture-shipped") is True
    assert webhook_sources.app_source_refusal("fixture-shipped") == ""


def test_a_builtin_record_keeps_the_declaration():
    from kiro_crew.apps.discovery import _manifest_to_builtin_dict

    manifest = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", "permissions": {"webhookSource": True}}
    )
    assert _manifest_to_builtin_dict(manifest)["permissions"]["webhookSource"] is True


def test_the_catalog_discloses_the_declaration_before_install():
    from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest

    assert _disclosed_permissions_manifest({"permissions": {"webhookSource": True}}) == {
        "permissions": {"webhookSource": True}
    }
    assert _disclosed_permissions_manifest({"permissions": {"webhookSource": "true"}}) is None
