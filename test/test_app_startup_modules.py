"""App startup modules (``ui.startup``): admission, approval and the ``/api/apps`` row."""

from __future__ import annotations

import json

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 -- requested by name
from app_manager_helpers import _make_app_source

from kiro_crew.apps import protected_app_state, startup_modules
from kiro_crew.apps.manager import app_dir, enable_app, install_app, uninstall_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import UIConfig

_UI = {"entry": "page.mjs", "startup": "startup.mjs"}


def _browser_only(**extra):
    return {"name": "ghost", "version": "1.0.0", "ui": dict(_UI), "iconPath": "ui/x.svg", **extra}


@pytest.fixture(name="no_confinement")
def no_confinement_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.sandbox.agent_confinement_evidence", lambda: None)


def _install(tmp_path, **manifest):
    src = _make_app_source(tmp_path, name="ghost", ui=dict(_UI), **manifest)
    result = install_app(src)
    assert result.ok, result.error
    enabled = enable_app("ghost")
    assert enabled.ok, enabled.error
    return src


def _row_app(owner=True):
    apps = [{"name": "ghost"}]
    startup_modules.stamp_startup(apps, owner=owner)
    return apps[0]


def _row(owner=True):
    apps = [{"name": "ghost"}]
    startup_modules.stamp_startup(apps, owner=owner)
    return apps[0].get("startup")


# --- manifest ----------------------------------------------------------------


def test_ui_startup_round_trips_and_is_omitted_when_undeclared():
    assert UIConfig.from_dict(_UI).to_dict()["startup"] == "startup.mjs"
    assert "startup" not in UIConfig.from_dict({"entry": "page.mjs"}).to_dict()
    assert UIConfig.from_dict({"startup": None}).startup == ""


@pytest.mark.parametrize(
    ("startup", "message"),
    [
        ("style.css", "ui.startup refused: its ui.startup 'style.css' is not a .mjs or .js module"),
        ("../escape.mjs", "ui.startup"),
    ],
)
def test_ui_startup_validation_refuses(startup, message):
    manifest = AppManifest.from_dict(
        {
            "name": "ghost",
            "version": "1.0.0",
            "displayName": "G",
            "description": "d",
            "ui": {"startup": startup},
        }
    )
    errors = manifest.validate()
    assert any(e.startswith(message) for e in errors), errors


# --- eligibility -------------------------------------------------------------


def test_browser_only_manifest_is_eligible():
    raw = _browser_only(contributes={"panelTabs": [], "commands": []}, desktop={})
    assert startup_modules.eligibility_refusal(raw, "gateway") is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("highlights", ["x"]),
        ("useCases", ["x"]),
        ("configuration", ["x"]),
        ("screenshotsDark", ["ui/s.png"]),
        ("heroImage", "ui/h.png"),
        ("heroImageDark", "ui/h.png"),
        ("heroImageDetail", "ui/h.png"),
        ("heroImageDetailDark", "ui/h.png"),
        ("iconPathDark", "ui/x.svg"),
        ("icon", "Star"),
    ],
)
def test_store_page_keys_keep_an_app_eligible(key, value):
    assert startup_modules.eligibility_refusal(_browser_only(**{key: value}), "gateway") is None


@pytest.mark.parametrize(
    ("raw", "resources", "reason"),
    [
        (
            _browser_only(backend={"entryPoint": "x"}),
            "gateway",
            "it declares 'backend', which runs outside the browser",
        ),
        (
            _browser_only(openCommand="open x"),
            "gateway",
            "it declares 'openCommand', which runs outside the browser",
        ),
        (
            _browser_only(somethingNew=1),
            "gateway",
            "it declares 'somethingNew', which runs outside the browser",
        ),
        (
            _browser_only(contributes={"fileMenuItems": []}),
            "gateway",
            "it declares 'contributes.fileMenuItems', which runs outside the browser",
        ),
        (
            _browser_only(ui={**_UI, "overlays": [{"id": "a"}]}),
            "gateway",
            "it declares 'ui.overlays'",
        ),
        (_browser_only(), "app", "the app manages its own resources"),
    ],
)
def test_ineligible_manifest_names_its_reason(raw, resources, reason):
    assert startup_modules.eligibility_refusal(raw, resources) == reason


# --- state -------------------------------------------------------------------


def _state(**over):
    args = {
        "entry": "startup.mjs",
        "version": "1.0.0",
        "digest": lambda: "abc",
        "enabled": True,
        "ceiling_reason": None,
        "eligibility": None,
        "approval": {"version": "1.0.0", "entry": "startup.mjs", "digest": "abc"},
    }
    return startup_modules.startup_state(**{**args, **over})["state"]


def test_state_admits_only_an_approved_eligible_enabled_app():
    assert _state() == "admitted"
    assert _state(enabled=False) == "denied"
    assert _state(ceiling_reason="third-party apps are off") == "denied"
    assert _state(eligibility="it declares 'backend'") == "denied"
    assert _state(approval=None) == "needs-approval"
    assert _state(version="1.0.1") == "needs-approval"  # an update asks again
    assert _state(entry="other.mjs") == "needs-approval"  # so does a changed entry
    assert _state(digest=lambda: "def") == "needs-approval"  # and changed code


@pytest.mark.parametrize("text", ["", "[]", '{"version": 1}', '{"version": "1", "entry": "s"}', "{"])
def test_malformed_approvals_read_as_none(app_home, text):
    protected_app_state.write_startup_text("ghost", text)
    assert startup_modules.read_approval("ghost") is None


# --- approval end to end ------------------------------------------------------


def test_approve_admits_then_revoke_withdraws(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    assert _row() == {
        "entry": "startup.mjs",
        "state": "needs-approval",
        "reason": "the owner has not approved this code",
    }

    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    # Stored outside the app's tree, with the app's other owner decisions.
    record = json.loads(protected_app_state.read_startup_text("ghost"))
    empty_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert record == {"version": "1.0.0", "entry": "startup.mjs", "digest": empty_sha256}
    assert not list(app_dir("ghost").rglob("*startup*.json"))
    assert _row()["state"] == "admitted"
    # Another caller (a non-owner session) is never told it may mount the module.
    assert _row(owner=False) == {
        "entry": "startup.mjs",
        "state": "denied",
        "reason": "only the owner's dashboard runs it",
    }

    startup_modules.set_approval("ghost", False)
    assert _row()["state"] == "needs-approval"


def test_approval_is_refused_under_agent_confinement(app_home, tmp_path, monkeypatch):
    _install(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.sandbox.agent_confinement_evidence", lambda: "running inside the agent sandbox"
    )
    assert startup_modules.set_approval("ghost", True) == {
        "error": "refused: running inside the agent sandbox"
    }
    assert protected_app_state.read_startup_text("ghost") is None


def test_ineligible_app_stays_denied_after_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path, openCommand="true")
    assert startup_modules.set_approval("ghost", True)["state"] == "denied"
    assert _row()["reason"] == "it declares 'openCommand', which runs outside the browser"


def test_app_without_startup_entry_gets_no_row(app_home, tmp_path):
    src = _make_app_source(tmp_path, name="plain", ui={"entry": "page.mjs"})
    assert install_app(src).ok
    apps = [{"name": "plain"}]
    startup_modules.stamp_startup(apps, owner=True)
    assert "startup" not in apps[0]


def _write_startup(code):
    ui = app_dir("ghost") / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "startup.mjs").write_text(code, encoding="utf-8")


def test_code_changed_after_approval_needs_approval_again(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    _write_startup("export default () => null\n")
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    # Same name, version and entry, different bytes: what an agent shell could write.
    _write_startup("export default () => { steal() }\n")
    assert _row()["state"] == "needs-approval"
    _write_startup("export default () => null\n")
    assert _row()["state"] == "admitted"


def test_uninstall_drops_the_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    startup_modules.set_approval("ghost", True)
    assert uninstall_app("ghost").ok
    assert protected_app_state.read_startup_text("ghost") is None


def test_revoke_runs_for_an_app_that_is_gone(app_home, no_confinement):
    record = {"version": "1.0.0", "entry": "s.mjs", "digest": "x"}
    protected_app_state.write_startup_text("ghost", json.dumps(record))
    assert "error" not in startup_modules.set_approval("ghost", False)
    assert startup_modules.read_approval("ghost") is None


def test_digest_is_not_read_for_a_disabled_or_unapproved_app():
    def unreadable():
        raise AssertionError("ui/ was read")

    assert _state(enabled=False, digest=unreadable) == "denied"
    assert _state(approval=None, digest=unreadable) == "needs-approval"


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (
            "../../other/ui/x.mjs",
            "its ui.startup '../../other/ui/x.mjs' is not a plain path inside ui/",
        ),
        ("/abs/x.mjs", "its ui.startup '/abs/x.mjs' is not a plain path inside ui/"),
        ("a\\b.mjs", "its ui.startup 'a\\\\b.mjs' is not a plain path inside ui/"),
        ("style.css", "its ui.startup 'style.css' is not a .mjs or .js module"),
        (
            "%2e%2e/%2e%2e/other/ui/x.mjs",
            "its ui.startup '%2e%2e/%2e%2e/other/ui/x.mjs' is not a plain path inside ui/",
        ),
        (".%2E/x.mjs", "its ui.startup '.%2E/x.mjs' is not a plain path inside ui/"),
        (".\t./x.mjs", "its ui.startup '.\\t./x.mjs' is not a plain path inside ui/"),
        ("dist/.hidden.mjs", "its ui.startup 'dist/.hidden.mjs' is not a plain path inside ui/"),
    ],
)
def test_entry_refusal(entry, reason):
    assert startup_modules.entry_refusal(entry) == reason


def test_an_entry_edited_to_escape_ui_is_denied_after_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    manifest = app_dir("ghost") / "app.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["ui"]["startup"] = "../../helper/ui/x.mjs"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert _row()["state"] == "denied"
    assert startup_modules.set_approval("ghost", True) == {
        "error": "app 'ghost': its ui.startup '../../helper/ui/x.mjs' is not a plain path inside ui/"
    }


def test_digest_covers_every_file_skips_links_and_refuses_oversize(app_home, tmp_path, monkeypatch):
    _install(tmp_path)
    ui = app_dir("ghost") / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "core.JS").write_text("one", encoding="utf-8")
    before = startup_modules.ui_digest("ghost")
    (ui / "core.JS").write_text("two", encoding="utf-8")  # upper-case suffix still counts
    assert startup_modules.ui_digest("ghost") != before
    after = startup_modules.ui_digest("ghost")
    (ui / "link.mjs").symlink_to(ui / "core.JS")  # the route refuses links; so does the digest
    assert startup_modules.ui_digest("ghost") == after
    monkeypatch.setattr(startup_modules, "_MAX_FILE_BYTES", 2)
    with pytest.raises(startup_modules.OversizeFile):
        startup_modules.ui_digest("ghost")


def test_an_oversize_file_blocks_approval_and_admission(
    app_home, tmp_path, monkeypatch, no_confinement
):
    _install(tmp_path)
    _write_startup("export default () => null\n")
    assert startup_modules.set_approval("ghost", True)["state"] == "admitted"
    monkeypatch.setattr(startup_modules, "_MAX_FILE_BYTES", 4)
    assert "startup" not in _row_app()  # the read fails closed: no row, nothing mounts
    assert startup_modules.set_approval("ghost", True)["error"].startswith(
        "app 'ghost': could not hash its ui/"
    )


def test_a_refused_approval_is_audited(app_home, tmp_path, monkeypatch):
    events = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.sandbox.agent_confinement_evidence", lambda: "sandboxed")
    startup_modules.set_approval("ghost", True)
    assert [(e["operation"], e["outcome"], e["source"]) for e in events] == [
        ("app_startup_approve", "denied", "cli")
    ]


def test_a_failed_uninstall_restores_the_approval(app_home, tmp_path, no_confinement):
    _install(tmp_path)
    startup_modules.set_approval("ghost", True)
    approved = startup_modules.read_approval("ghost")
    snapshot = protected_app_state.snapshot_app("ghost")
    protected_app_state.forget_grants("ghost")
    assert startup_modules.read_approval("ghost") is None
    protected_app_state.restore_app("ghost", snapshot)
    assert startup_modules.read_approval("ghost") == approved
