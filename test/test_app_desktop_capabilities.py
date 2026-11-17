"""Desktop capabilities: what an app's hosted desktop windows may ask the shell for.

An app names them in ``permissions.desktopCapabilities`` (``apps/desktop_capabilities.py``);
the gateway publishes what is declared and approved as the row's
``desktopCapabilitiesGrant``, which the desktop shell's window host reads before it
serves any of them. These cases install a real app into a private home so the row
comes from the same manifest, approved-grants set and record ``/api/apps`` reads. They
pin the parse, the disclosure, an install approving what it declares, an update that
adds one waiting for the owner, a set stored before the key existed approving nothing,
and the built-in Mochi declaring what its shell code already uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps import desktop_capabilities as dc
from kiro_crew.apps import protected_app_state
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    _write_approve_declared,
    app_dir,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest
from kiro_crew.apps.permissions import format_permissions_summary
from kiro_crew.apps.point_grants import (
    explicit_only_grant_families,
    grant_source,
    point_grant_families,
)

APP = "desk-tool"
ALL = ["screenCapture", "localFiles", "gatewayChoice"]
_MOCHI_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "src/kiro_crew/apps/builtins/mochi"
    / APP_MANIFEST_FILENAME
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    """A private app home that admits a local third-party app."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )


def _source(tmp_path: Path, sub: str, caps: list[str] | None, version: str = "1.0.0") -> Path:
    src = tmp_path / sub / APP
    src.mkdir(parents=True)
    permissions: dict = {"storage": True}
    if caps is not None:
        permissions["desktopCapabilities"] = caps
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Desk Tool",
        "description": "a desktop window app",
        "author": "tester",
        "permissions": permissions,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _install(tmp_path: Path, caps: list[str] | None) -> None:
    result = install_app(_source(tmp_path, "v1", caps))
    assert result.ok, result.error
    assert enable_app(APP).ok


def _update(tmp_path: Path, caps: list[str] | None) -> None:
    result = update_app(_source(tmp_path, "v2", caps, version="2.0.0"))
    assert result.ok, result.error


def _grant() -> dict | None:
    row = get_app(APP)
    assert row is not None
    return row.get("desktopCapabilitiesGrant")


# ── the declaration ─────────────────────────────────────────────────────────


def test_only_known_capabilities_in_a_list_declare_anything():
    parsed = Permissions.from_dict(
        {"desktopCapabilities": ["gatewayChoice", "keylogger", 3, "screenCapture"]}
    )
    assert parsed == Permissions(desktopCapabilities=["screenCapture", "gatewayChoice"])
    assert parsed.to_dict() == {"desktopCapabilities": ["screenCapture", "gatewayChoice"]}
    for malformed in ("localFiles", True, {"localFiles": True}, None):
        assert Permissions.from_dict({"desktopCapabilities": malformed}).desktopCapabilities == []
    assert "desktopCapabilities" not in Permissions().to_dict()


def test_the_declaration_is_an_explicit_only_staged_grant_family():
    assert dc.GRANT_FAMILY in point_grant_families()
    assert dc.GRANT_FAMILY in explicit_only_grant_families()
    manifest = {"permissions": {"desktopCapabilities": ["localFiles", "nope"]}}
    assert grant_source(manifest)[dc.GRANT_FAMILY] == ["localFiles"]


def test_the_catalog_and_the_install_summary_disclose_it():
    entry = {"permissions": {"desktopCapabilities": ["gatewayChoice", "everything"]}}
    assert _disclosed_permissions_manifest(entry) == {
        "permissions": {"desktopCapabilities": ["gatewayChoice"]}
    }
    manifest = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", "permissions": {"desktopCapabilities": ALL}}
    )
    assert format_permissions_summary(manifest).splitlines() == [
        "  Desktop:   may capture your screen, show and open your local files,"
        " switch among your gateways"
    ]


# ── the owner's approval on install and update ──────────────────────────────


def test_an_app_that_declares_nothing_has_no_grant_row(tmp_path):
    _install(tmp_path, None)
    assert _grant() is None


def test_an_install_approves_what_it_declares(tmp_path):
    _install(tmp_path, ALL)
    assert _grant() == {"approved": ALL, "staged": []}


def test_an_update_that_adds_one_waits_for_the_owner(tmp_path):
    _install(tmp_path, ["localFiles"])
    _update(tmp_path, ["localFiles", "screenCapture"])
    assert _grant() == {"approved": ["localFiles"], "staged": ["screenCapture"]}

    approved = enable_app(APP, grants_consent={dc.GRANT_FAMILY: ["screenCapture"]})
    assert approved.ok, approved.error
    assert _grant() == {"approved": ["screenCapture", "localFiles"], "staged": []}


def test_an_update_that_drops_one_no_longer_lists_it(tmp_path):
    _install(tmp_path, ALL)
    _update(tmp_path, ["gatewayChoice"])
    assert _grant() == {"approved": ["gatewayChoice"], "staged": []}


def test_a_set_stored_before_the_key_existed_approves_nothing(tmp_path):
    """A capability could not have been shown to the owner before this host knew the key."""
    _install(tmp_path, ALL)
    stored = json.loads(protected_app_state.read_grants_text(APP) or "{}")
    stored.pop(dc.GRANT_FAMILY)
    protected_app_state.write_grants_text(APP, json.dumps(stored))

    assert _grant() == {"approved": [], "staged": ALL}
    assert enable_app(APP, grants_consent={dc.GRANT_FAMILY: ["localFiles"]}).ok
    assert _grant() == {"approved": ["localFiles"], "staged": ["screenCapture", "gatewayChoice"]}


def test_a_record_from_before_approved_sets_approves_none_of_them(tmp_path):
    """The one-time import stores "approve what is declared" for an install from
    before approved sets. Freezing it must not approve an explicit-only family: no
    owner saw one in a prompt then, so screen capture would be granted unasked. The
    session-reach family is explicit-only too, so it is declared here as well."""
    src = _source(tmp_path, "v1", ALL)
    manifest_path = src / APP_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["permissions"]["reachableByUserSessions"] = ["read"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert install_app(src).ok
    assert enable_app(APP).ok
    assert get_app(APP)["userSessionReachGrant"]["approved"] == ["read"], "an install approves it"
    _write_approve_declared(APP)
    record = app_dir(APP) / INSTALLED_META_FILENAME
    data = json.loads(record.read_text(encoding="utf-8"))
    data.pop("approvedGrantsStored", None)
    record.write_text(json.dumps(data), encoding="utf-8")

    assert _grant() == {"approved": [], "staged": ALL}
    assert get_app(APP)["userSessionReachGrant"] == {"approved": [], "staged": ["read"]}
    frozen = json.loads(protected_app_state.read_grants_text(APP) or "{}")
    assert frozen.get("approveDeclared") is None, "the first read stored a real set"
    for family in explicit_only_grant_families():
        assert frozen[family] == [], family


# ── the built-in that uses them today ───────────────────────────────────────


def test_mochi_declares_what_its_shell_code_already_uses():
    """Wheel copy first: an external Mochi declaring these is then no widening (E9)."""
    raw = json.loads(_MOCHI_MANIFEST.read_text(encoding="utf-8"))
    assert raw["permissions"]["desktopCapabilities"] == ALL
    assert "desktop" not in raw, "Mochi's own windows are still the shell's; none is hosted"


def test_a_builtin_declaration_is_approved_as_packaged():
    """A built-in stores no approved-grants set, so its row lists every declared one."""
    row = dc.desktop_capabilities_grant_row("mochi", {"permissions": {"desktopCapabilities": ALL}})
    assert row == {"desktopCapabilitiesGrant": {"approved": ALL, "staged": []}}
    assert dc.desktop_capabilities_grant_row("mochi", {"permissions": {}}) == {}
