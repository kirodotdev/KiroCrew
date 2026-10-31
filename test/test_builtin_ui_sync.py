"""A builtin that ships a ``ui/`` tree has it installed where the static route serves it.

The route serves ``apps/<name>/ui/<entry>`` from the data home and refuses paths
outside the install directory, so a builtin with a bundle (a ``panelTabs`` body)
must have its files copied there at registration. These tests pin the copy, the
replacement on a later registration, and the one case left alone: a ``ui`` link,
which is the documented dev-mode setup.
"""

from __future__ import annotations

import json
import os
from test.test_builtin_app_optional_enable import _ship_builtin

import pytest


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_disk_discovery(_floor_monkeypatch):
    import kiro_crew.apps.manager as mgr

    _floor_monkeypatch.setattr(mgr, "discover_builtin_apps", lambda: [])


def _register(monkeypatch, shipped_root):
    import kiro_crew.apps.execution as execution
    import kiro_crew.apps.manager as mgr
    from kiro_crew.apps.manager import register_builtin_apps

    manifest = json.loads((shipped_root / "app.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(mgr, "_BUILTIN_APPS", [manifest])
    monkeypatch.setattr(mgr, "_orphaned_builtins_cache", None)
    monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: False)
    register_builtin_apps()


def test_shipped_ui_is_copied_into_the_install_dir_and_replaced_on_reregistration(
    app_home, tmp_path, monkeypatch
):
    from kiro_crew.apps.manager import app_dir

    shipped = _ship_builtin(
        monkeypatch,
        tmp_path,
        "bundled-app",
        contributes={
            "panelTabs": [
                {
                    "id": "main",
                    "title": "Main",
                    "menuLabel": "Open Main",
                    "icon": "Code",
                    "entry": "dist/panel.mjs",
                }
            ]
        },
    )
    (shipped / "ui" / "dist").mkdir(parents=True)
    (shipped / "ui" / "dist" / "panel.mjs").write_text("export default 1\n", encoding="utf-8")
    _register(monkeypatch, shipped)

    installed = app_dir("bundled-app") / "ui" / "dist" / "panel.mjs"
    assert installed.read_text(encoding="utf-8") == "export default 1\n"
    # Still a builtin: a panel tab is not a routed `ui.entry`, which is what
    # downgrades an app's origin to `local`.
    from kiro_crew.apps.manager import get_app

    assert get_app("bundled-app")["origin"] == "builtin"

    # The next version's bundle replaces the previous one, and a file the new
    # version does not ship is removed rather than served stale.
    (shipped / "ui" / "dist" / "panel.mjs").write_text("export default 2\n", encoding="utf-8")
    (shipped / "ui" / "dist" / "extra.mjs").write_text("old\n", encoding="utf-8")
    _register(monkeypatch, shipped)
    assert installed.read_text(encoding="utf-8") == "export default 2\n"
    os.remove(shipped / "ui" / "dist" / "extra.mjs")
    _register(monkeypatch, shipped)
    assert not (app_dir("bundled-app") / "ui" / "dist" / "extra.mjs").exists()
    assert not (app_dir("bundled-app") / ".ui-incoming").exists()


def test_a_builtin_without_a_ui_tree_gets_none(app_home, tmp_path, monkeypatch):
    from kiro_crew.apps.manager import app_dir

    shipped = _ship_builtin(monkeypatch, tmp_path, "plain-app")
    _register(monkeypatch, shipped)
    assert not (app_dir("plain-app") / "ui").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics")
def test_a_linked_ui_is_left_alone(app_home, tmp_path, monkeypatch):
    """Dev mode links ``ui`` at a developer's tree; registration must not replace it."""
    from kiro_crew.apps.manager import app_dir

    shipped = _ship_builtin(monkeypatch, tmp_path, "dev-app")
    (shipped / "ui").mkdir()
    (shipped / "ui" / "panel.mjs").write_text("shipped\n", encoding="utf-8")
    dev_tree = tmp_path / "dev-ui"
    dev_tree.mkdir()
    (dev_tree / "panel.mjs").write_text("dev\n", encoding="utf-8")
    dest = app_dir("dev-app")
    dest.mkdir(parents=True, exist_ok=True)
    os.symlink(dev_tree, dest / "ui")

    _register(monkeypatch, shipped)
    assert os.path.islink(dest / "ui")
    assert (dest / "ui" / "panel.mjs").read_text(encoding="utf-8") == "dev\n"
