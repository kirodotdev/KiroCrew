"""The core-route grant seam: a core route an app's page calls is on by declaration.

``apps/core_route_grants.py`` answers which enabled app turns on a core route that
exists for an app's page. These cases pin that the answer comes from what an app
declares and the owner approved, never from its name: an enabled app holding an
approved ``permissions.api`` entry inside the route's namespace turns it on, a
disabled one does not, an entry an update added waits for the owner as every new
``permissions.api`` entry does, and a broad entry that names no feature turns
nothing on. The owner's enable is the sealed owner-enable marker, never the
sandbox-writable ``installed.json``: a record forged to read enabled holds nothing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import core_app_independence as independence
import pytest

from kiro_crew.apps import core_route_grants as crg
from kiro_crew.apps.core_route_grants import RouteGrant, core_route_grant, entry_grants_route
from kiro_crew.apps.host_exec_grants import grant_host_exec, host_exec_granted, revoke_host_exec
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    disable_app,
    enable_app,
    install_app,
    is_app_enabled,
    uninstall_app,
    update_app,
)
from kiro_crew.dashboard.token_auth_app_scope import _api_pattern_matches

APP = "folder-app"
OTHER = "other-app"
NAMESPACE = "/api/project-scaffold"
SCAN = "/api/project-scaffold/scan"
CREATE = "/api/project-scaffold/create"
UNRELATED = "/api/sessions"


@pytest.fixture(autouse=True)
def app_home(tmp_path, _floor_monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


def _source(tmp_path: Path, sub: str, name: str, api: list[str], version: str = "1.0.0") -> Path:
    src = tmp_path / sub / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": version,
        "displayName": "Folder App",
        "description": "core route grants",
        "author": "tester",
        "permissions": {"api": api},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _installed(tmp_path: Path, api: list[str], name: str = APP, *, enabled: bool = True) -> None:
    assert install_app(_source(tmp_path, f"v1-{name}", name, api)).ok
    if enabled:
        assert enable_app(name).ok


def _forge_enabled(name: str = APP) -> None:
    """Write ``enabled: true`` into the app's own record, as a sandboxed shell can."""
    path = Path(crg.apps_dir()) / name / INSTALLED_META_FILENAME
    record = json.loads(path.read_text(encoding="utf-8"))
    record["enabled"] = True
    path.write_text(json.dumps(record), encoding="utf-8")


# ── who holds the route ──────────────────────────────────────────────────────


def test_an_enabled_app_declaring_the_namespace_holds_both_routes(tmp_path):
    _installed(tmp_path, [UNRELATED, NAMESPACE])
    assert host_exec_granted(APP), "the owner's enable records the owner-enable marker"
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(holder=APP)
    assert core_route_grant(CREATE, NAMESPACE) == RouteGrant(holder=APP)


def test_a_disabled_declaring_app_holds_nothing_and_words_the_refusal(tmp_path):
    _installed(tmp_path, [NAMESPACE], enabled=False)
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(disabled_declarers=(APP,))


def test_a_disable_turns_the_route_off_on_the_next_call(tmp_path):
    _installed(tmp_path, [NAMESPACE])
    assert core_route_grant(SCAN, NAMESPACE).holder == APP
    assert disable_app(APP).ok
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(disabled_declarers=(APP,))


def test_an_uninstall_turns_the_route_off(tmp_path):
    _installed(tmp_path, [NAMESPACE])
    assert uninstall_app(APP).ok
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()


def test_with_no_app_installed_nothing_holds_the_route():
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()


def test_an_app_declaring_other_paths_holds_nothing(tmp_path):
    _installed(tmp_path, [UNRELATED, "/api/project-scaffolder", "/api/browse-dirs"])
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()


def test_any_enabled_declaring_app_holds_it_and_disabled_ones_are_listed(tmp_path):
    _installed(tmp_path, [NAMESPACE], name=OTHER, enabled=False)
    _installed(tmp_path, [NAMESPACE], name="zz-app")
    assert core_route_grant(SCAN, NAMESPACE).holder == "zz-app"
    assert disable_app("zz-app").ok
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(disabled_declarers=(OTHER, "zz-app"))


# ── the owner's enable is the sealed marker, not the record ─────────────────


def test_a_record_forged_enabled_without_the_marker_holds_nothing(tmp_path):
    """An install stores its approved entries and leaves the app off with no
    marker; a sandboxed shell then flips the record. It reads enabled, and still
    turns neither route on: the refusal says the enable was not recorded."""
    _installed(tmp_path, [NAMESPACE], enabled=False)
    _forge_enabled()
    assert is_app_enabled(APP) and not host_exec_granted(APP)
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(unrecorded_declarers=(APP,))
    assert core_route_grant(CREATE, NAMESPACE) == RouteGrant(unrecorded_declarers=(APP,))


def test_an_enabled_record_with_the_marker_holds(tmp_path):
    _installed(tmp_path, [NAMESPACE], enabled=False)
    _forge_enabled()
    assert grant_host_exec(APP)
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(holder=APP)


def test_a_marker_on_a_disabled_record_holds_nothing(tmp_path):
    _installed(tmp_path, [NAMESPACE], enabled=False)
    assert grant_host_exec(APP)
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(disabled_declarers=(APP,))


def test_a_forged_record_with_an_unapproved_entry_words_nothing(tmp_path):
    """Without an approved entry the app could not hold the route even with the
    marker, so the refusal does not tell the person to turn it off and on."""
    _installed(tmp_path, [UNRELATED], enabled=False)
    _forge_enabled()
    manifest_path = Path(crg.apps_dir()) / APP / APP_MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["permissions"]["api"].append(NAMESPACE)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()


@pytest.mark.parametrize("recover", ["re-enable", "off and on"])
def test_an_enable_from_before_the_marker_holds_again_once_the_owner_enables_it(tmp_path, recover):
    """A record enabled before the marker shipped reads enabled with no marker: the
    routes stay off until the owner enables the app again, which records it."""
    _installed(tmp_path, [NAMESPACE])
    revoke_host_exec(APP)
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(unrecorded_declarers=(APP,))

    if recover == "off and on":
        assert disable_app(APP).ok
    assert enable_app(APP).ok
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(holder=APP)


# ── the owner's approval ─────────────────────────────────────────────────────


def test_an_update_that_adds_the_entry_waits_for_the_owner(tmp_path):
    """Adding the declaration to an enabled app grants nothing until the owner
    approves it on the app's page, the path every new ``permissions.api`` entry
    takes."""
    _installed(tmp_path, [UNRELATED])
    result = update_app(_source(tmp_path, "v2", APP, [UNRELATED, NAMESPACE], version="2.0.0"))
    assert result.ok, result.error

    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()

    approved = enable_app(APP, grants_consent={"api": [NAMESPACE], "events": []})
    assert approved.ok, approved.error
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(holder=APP)


def test_a_manifest_edited_in_place_grants_nothing(tmp_path):
    """The app's ``app.json`` copy is writable from the app's own tree; an entry
    added there is outside the approved set, so it turns nothing on."""
    _installed(tmp_path, [UNRELATED])
    manifest_path = Path(crg.apps_dir()) / APP / APP_MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["permissions"]["api"].append(NAMESPACE)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()


# ── which entries count ──────────────────────────────────────────────────────


@pytest.mark.parametrize("entry", ["/api", "/api/*", "/*", "*", "/api/project-scaff*"])
def test_a_broad_entry_admits_the_path_but_turns_no_route_on(tmp_path, entry):
    """The token check admits the path for this app's own token; the entry names
    no feature, so it does not turn the route on for the person."""
    assert _api_pattern_matches(entry, SCAN)
    assert not entry_grants_route(entry, SCAN, NAMESPACE)
    _installed(tmp_path, [entry])
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()


@pytest.mark.parametrize(
    "entry",
    [
        NAMESPACE,
        f"{NAMESPACE}/*",
        f"{NAMESPACE}*",
        f" {NAMESPACE} ",
        SCAN,
        f"={SCAN}",
    ],
)
def test_an_entry_inside_the_namespace_admitting_the_path_counts(entry):
    assert entry_grants_route(entry, SCAN, NAMESPACE)


@pytest.mark.parametrize(
    "entry", ["", "   ", f"={NAMESPACE}", f"={CREATE}", CREATE, "=", f"{NAMESPACE}/"]
)
def test_an_entry_that_does_not_admit_the_path_does_not_count(entry):
    assert not entry_grants_route(entry, SCAN, NAMESPACE)


def test_an_exact_entry_turns_on_only_its_own_route(tmp_path):
    _installed(tmp_path, [f"={SCAN}"])
    assert core_route_grant(SCAN, NAMESPACE).holder == APP
    assert core_route_grant(CREATE, NAMESPACE) == RouteGrant()


@pytest.mark.parametrize(
    "path", ["/api/project-scaffolder", "/api/chat", "/api/project-scaffold2/scan"]
)
def test_a_path_outside_its_namespace_is_refused(path):
    with pytest.raises(ValueError):
        core_route_grant(path, NAMESPACE)


def test_a_relative_namespace_is_refused():
    with pytest.raises(ValueError):
        core_route_grant("api/project-scaffold/scan", "api/project-scaffold")


# ── failures grant nothing ───────────────────────────────────────────────────


def test_an_unreadable_app_grants_nothing_and_the_others_still_answer(tmp_path, monkeypatch):
    _installed(tmp_path, [NAMESPACE], name="aa-broken")
    _installed(tmp_path, [NAMESPACE], name="bb-app")
    real = crg.get_app_manifest

    def _manifest(name):
        if name == "aa-broken":
            raise OSError("unreadable")
        return real(name)

    monkeypatch.setattr(crg, "get_app_manifest", _manifest)
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant(holder="bb-app")


def test_an_unlistable_apps_folder_grants_nothing(tmp_path, monkeypatch):
    _installed(tmp_path, [NAMESPACE])

    def _unlistable(root):
        raise OSError("unlistable")

    monkeypatch.setattr(crg, "_recorded_app_names", _unlistable)
    assert core_route_grant(SCAN, NAMESPACE) == RouteGrant()


# ── the seam and its one caller name no app ──────────────────────────────────

_SRC = Path(crg.__file__).resolve().parents[1]


def _shipped_app_ids() -> set[str]:
    """Every pinned or shipped app id, in both the id and the package spelling."""
    pins = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    ids = {pin["name"] for pin in pins["pins"]}
    for manifest in (_SRC / "apps" / "builtins").glob("*/app.json"):
        ids.add(json.loads(manifest.read_text(encoding="utf-8"))["name"])
    return ids | {i.replace("-", "_") for i in ids}


@pytest.mark.parametrize(
    "module", ["apps/core_route_grants.py", "dashboard/chat_folder_scaffold.py"]
)
def test_the_seam_and_the_scaffold_gate_name_no_app(module):
    """The gate is opened by a declaration, so no app id may appear in either file:
    a string naming one would key the routes on that app again."""
    ids = _shipped_app_ids()
    assert "project-scaffolder" in ids, "the app list lost the app this gate was written for"
    tree = ast.parse((_SRC / module).read_text(encoding="utf-8"))
    assert independence.name_strings(tree, ids) == []
