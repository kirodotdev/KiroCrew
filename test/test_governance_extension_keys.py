"""Governance keys for extensions, plugins, app versions and app developer mode.

RFC crew-enterprise-edition, appendix rows "Extension install", "Extension
uninstall", "Plugin sync", "App versions" and "App developer mode"
(``kiro_crew.extension_governance``). Enterprise: an unnamed key answers the
baseline, which is off. ``oss``: an unnamed key permits, as before.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import extension_governance as eg
from kiro_crew.apps import dev_mode
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.edition_locks import LOCKED_BY_POLICY
from kiro_crew.platform.capability_bound import BoundedCapabilityManager
from kiro_crew.platform.governance import CapabilityGate, ScopedRuleset, parse_policy

_ALL_OPS = [
    "install_mcp",
    "install_skill",
    "install_agent",
    "uninstall_mcp",
    "uninstall_skill",
    "uninstall_agent",
    "sync_plugins",
]


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


@pytest.fixture
def enterprise(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def _install_policy(monkeypatch, body: dict | None) -> None:
    from kiro_crew.platform import context as pc

    ceiling = parse_policy({"version": 1, "boot": {}, **body}) if body is not None else None
    ctx = SimpleNamespace(governance=ceiling)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)


def _install_on(extensions: list[str]) -> dict:
    return {
        "capabilities": {
            "extension_install": {"scopes": {"extensions": {"mode": "allow", "allow": extensions}}}
        }
    }


class _Inner:
    def __init__(self, drift: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self.drift = drift or []

    async def _ok(self, name: str):
        from kiro_crew.platform.interfaces import CapabilityResult

        self.calls.append(name)
        return CapabilityResult(ok=True)

    def __getattr__(self, name: str):
        if name in _ALL_OPS:
            return lambda *_a: self._ok(name)
        raise AttributeError(name)

    async def plugins_out_of_sync(self):
        return list(self.drift)


def _run_all(mgr: BoundedCapabilityManager, ident: str = "github") -> list[bool]:
    async def _go():
        out = []
        for op in _ALL_OPS:
            fn = getattr(mgr, op)
            out.append((await (fn() if op == "sync_plugins" else fn(ident))).ok)
        return out

    return asyncio.run(_go())


def test_keys_parse_with_their_catalog_shapes() -> None:
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {},
            "app_versions": {"mode": "allow", "allow": ["file-explorer@1.*"]},
            **_install_on(["mcp:github"]),
        }
    )
    gate = ceiling.get(eg.EXTENSIONS_SCOPE)
    assert isinstance(gate, CapabilityGate) and gate.enabled is True
    extensions = gate.scopes["extensions"]
    assert isinstance(extensions, ScopedRuleset) and extensions.matcher == "identifier"
    assert isinstance(ceiling.get(eg.APP_VERSIONS_SCOPE), ScopedRuleset)
    named = parse_policy({"version": 1, "boot": {}, "capabilities": {"app_dev_mode": {}}})
    dev = named.get(eg.DEV_MODE_SCOPE)
    assert isinstance(dev, CapabilityGate) and dev.enabled is True  # default on when named


def test_oss_without_a_policy_permits_everything(isolated_home, fake_sel, monkeypatch) -> None:
    _install_policy(monkeypatch, None)
    inner = _Inner(drift=["reviewer"])
    assert all(_run_all(BoundedCapabilityManager(inner)))  # type: ignore[arg-type]
    assert inner.calls == _ALL_OPS
    assert eg.app_version_denied("any-app", "9.9.9") is None
    assert eg.developer_mode_permitted() is True
    fake_sel.log_governance_decision.assert_not_called()


@pytest.mark.parametrize("body", [None, {}], ids=["no-ceiling", "empty-policy"])
def test_enterprise_unnamed_keys_answer_the_baseline(
    enterprise, isolated_home, fake_sel, monkeypatch, body
) -> None:
    _install_policy(monkeypatch, body)
    inner = _Inner(drift=["reviewer"])
    assert not any(_run_all(BoundedCapabilityManager(inner)))  # type: ignore[arg-type]
    assert inner.calls == []
    assert eg.app_version_denied("any-app", "1.0.0")
    assert eg.developer_mode_permitted() is False
    assert fake_sel.log_governance_decision.call_count == len(_ALL_OPS) + 1


def test_enterprise_policy_names_what_may_be_installed(
    enterprise, isolated_home, fake_sel, monkeypatch
) -> None:
    _install_policy(monkeypatch, _install_on(["mcp:github", "plugin:reviewer"]))
    inner = _Inner(drift=["reviewer"])
    mgr = BoundedCapabilityManager(inner)  # type: ignore[arg-type]
    assert _run_all(mgr) == [True, False, False, True, True, True, True]
    assert _run_all(mgr, "other")[:3] == [False, False, False]
    rows = {
        (c.kwargs["item"], c.kwargs["outcome"])
        for c in fake_sel.log_governance_decision.call_args_list
    }
    assert ("extensions:mcp:github", "allowed") in rows
    assert ("extensions:mcp:other", "denied") in rows


def test_plugin_sync_refused_when_a_drifted_package_is_unlisted(
    enterprise, isolated_home, fake_sel, monkeypatch
) -> None:
    _install_policy(monkeypatch, _install_on(["plugin:reviewer"]))
    inner = _Inner(drift=["reviewer", "unlisted"])
    res = asyncio.run(BoundedCapabilityManager(inner).sync_plugins())  # type: ignore[arg-type]
    assert res.ok is False and "governance" in res.message
    assert inner.calls == []


def test_oss_policy_pinning_install_off_refuses_it(isolated_home, fake_sel, monkeypatch) -> None:
    _install_policy(monkeypatch, {"capabilities": {"extension_install": {"enabled": False}}})
    inner = _Inner()
    assert not any(_run_all(BoundedCapabilityManager(inner)))  # type: ignore[arg-type]
    assert inner.calls == []


@pytest.mark.parametrize(("version", "ok"), [("1.4.0", True), ("2.0.0", False), ("", False)])
def test_app_versions_list_gates_admission(
    enterprise, isolated_home, fake_sel, monkeypatch, version, ok
) -> None:
    _install_policy(
        monkeypatch, {"app_versions": {"mode": "allow", "allow": ["file-explorer@1.*"]}}
    )
    manifest = SimpleNamespace(version=version, signer="")
    denied = app_admission_denied("file-explorer", manifest=manifest, action="boot")  # type: ignore[arg-type]
    assert (denied is None) is ok
    # A caller that asked the version off the loop skips the second read.
    skipped = app_admission_denied(
        "file-explorer", manifest=manifest, action="register_external", check_version=False  # type: ignore[arg-type]
    )
    assert skipped is None


def test_a_registration_without_a_manifest_is_judged_by_its_own_version(
    enterprise, isolated_home, fake_sel, monkeypatch
) -> None:
    _install_policy(monkeypatch, {"app_versions": {"mode": "allow", "allow": ["my-app@1.0.0"]}})
    assert app_admission_denied("my-app", manifest=None, version="1.0.0") is None
    assert app_admission_denied("my-app", manifest=None, version="2.0.0")
    # Re-enable or boot of that registration: no manifest, no caller version, so the
    # installed record's version is judged.
    from kiro_crew.apps import manager

    record = SimpleNamespace(version="1.0.0")
    monkeypatch.setattr(manager, "_read_installed", lambda name: record)
    assert app_admission_denied("my-app", manifest=None, action="enable") is None
    record.version = "2.0.0"
    assert app_admission_denied("my-app", manifest=None, action="enable")
    # A manifest that names no version is judged the same way on activation...
    versionless = SimpleNamespace(version="", signer="")
    record.version = "1.0.0"
    assert app_admission_denied("my-app", manifest=versionless, action="enable") is None  # type: ignore[arg-type]
    # ...but an install never borrows the installed version.
    assert app_admission_denied("my-app", manifest=versionless, action="install")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("version", "ok"),
    [("1.0.0", True), ("1.0.0-beta.1", True), ("2.1.0", False), ("", False), ("None", False)],
)
def test_a_deny_list_also_refuses_an_unknown_version(
    isolated_home, fake_sel, monkeypatch, version, ok
) -> None:
    _install_policy(monkeypatch, {"app_versions": {"mode": "deny", "deny": ["my-app@2.*"]}})
    assert (eg.app_version_denied("my-app", version) is None) is ok


def test_an_unknown_version_is_permitted_when_nothing_governs(
    isolated_home, fake_sel, monkeypatch
) -> None:
    _install_policy(monkeypatch, None)
    assert eg.app_version_denied("my-app", "") is None


def test_enterprise_policy_can_turn_developer_mode_on(
    enterprise, isolated_home, fake_sel, monkeypatch
) -> None:
    _install_policy(monkeypatch, {"capabilities": {"app_dev_mode": {"enabled": True}}})
    assert eg.developer_mode_permitted() is True
    fake_sel.log_governance_decision.assert_not_called()
    assert eg.developer_mode_permitted(audit=True) is True
    assert fake_sel.log_governance_decision.call_args.kwargs["outcome"] == "allowed"


def test_an_ungoverned_switch_writes_no_audit(isolated_home, fake_sel, monkeypatch) -> None:
    _install_policy(monkeypatch, None)
    assert eg.developer_mode_permitted(audit=True) is True
    fake_sel.log_governance_decision.assert_not_called()


def test_a_governance_error_denies(isolated_home, fake_sel, monkeypatch) -> None:
    from kiro_crew.platform import governance_profiles

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(governance_profiles, "governance_permits", _boom)
    assert eg.extension_install_denied("mcp", "github")
    assert eg.developer_mode_permitted() is False


def test_unknown_extension_kind_is_a_bug() -> None:
    with pytest.raises(ValueError):
        eg.extension_install_denied("theme", "x")


# ── App developer mode: the toggle and every read follow the key ─────────────


def test_dev_mode_enable_is_refused_and_audited(enterprise, isolated_home, monkeypatch) -> None:
    _install_policy(monkeypatch, {})
    calls: list[dict] = []
    monkeypatch.setattr(
        dev_mode, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: calls.append(kw))
    )
    assert dev_mode.set_dev_mode("some-app", True) == {
        "error": LOCKED_BY_POLICY,
        "code": "locked_by_policy",
    }
    assert [(c["operation"], c["outcome"], c["error"]) for c in calls] == [
        ("app_dev_mode", "denied", "locked_by_policy")
    ]


@pytest.mark.parametrize("edition", ["oss", "enterprise"])
def test_dev_mode_metadata_follows_the_key(isolated_home, fake_sel, monkeypatch, edition) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", edition)
    _install_policy(monkeypatch, {})
    monkeypatch.setattr(dev_mode, "_read_installed", lambda name: SimpleNamespace(dev=True))
    monkeypatch.setattr(dev_mode, "_read_dev_sentinel", lambda: {"some-app"})
    dev_mode._set_gated_dev_cache({"some-app"})
    on = edition == "oss"
    try:
        assert dev_mode.is_dev_mode("some-app") is on
        assert dev_mode.is_dev_mode_cached("some-app") is on
        assert dev_mode._load_dev_apps() == ({"some-app"} if on else set())
    finally:
        dev_mode._set_dev_cache(set())


def test_a_null_manifest_version_is_refused_by_a_deny_list(
    isolated_home, fake_sel, monkeypatch
) -> None:
    from kiro_crew.apps.manifest import AppManifest

    _install_policy(monkeypatch, {"app_versions": {"mode": "deny", "deny": ["demoapp@2.*"]}})
    manifest = AppManifest.from_dict({"name": "demoapp", "version": None})
    assert app_admission_denied("demoapp", manifest=manifest, action="install")
