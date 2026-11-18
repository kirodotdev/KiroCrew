"""Where signature scheme 2 is enforced: activation, update and execution boundaries.

Under ``require_signature`` an installed app whose signed files changed is refused at
enable, at the boot resource pass, at restart and at update; an execution boundary
judges the manifest the caller acts on and audits only a fixed classification; the
dev-mode grant skips only ``ui/`` files that are not code, and only for a ``ui/``
linked out of the install; and app Python never reads bytecode from the app folder.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import py_compile
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import make_dir_link
from kiro_crew.apps import admission, execution
from kiro_crew.apps.admission import (
    PACKAGE_MISMATCH,
    app_admission_denied,
    installed_package_refusal,
    new_pycache_dir,
    signed_pycache_env,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.package_signature import sign_app_directory

SIGNER = "acme"
SECRET = "publisher-trust-key"
APP = "signed-pkg"


@pytest.fixture(autouse=True)
def _restore_import_state():
    """Undo what a signed-first load leaves in the process: path hooks, roots, digests."""
    from kiro_crew.apps import module_loader, package_signature

    hooks = list(sys.path_hooks)
    roots = dict(module_loader._source_only_roots)
    yield
    sys.path_hooks[:] = hooks
    module_loader._source_only_roots.clear()
    module_loader._source_only_roots.update(roots)
    package_signature._DIGESTS.clear()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


def _enforce(home: Path) -> None:
    (home / "app_admission.json").write_text(
        json.dumps({"mode": "enforce", "require_signature": True, "trust_keys": {SIGNER: SECRET}}),
        encoding="utf-8",
    )


def _package(tmp_path: Path, **manifest: Any) -> Path:
    src = tmp_path / "source" / APP
    (src / "backend").mkdir(parents=True)
    (src / "ui").mkdir()
    (src / "app.json").write_text(
        json.dumps(
            {"name": APP, "version": "1.0.0", "displayName": "Signed", "description": "d"}
            | manifest
        ),
        encoding="utf-8",
    )
    (src / "backend" / "__init__.py").write_text("", encoding="utf-8")
    (src / "backend" / "app.py").write_text("print('publisher')\n", encoding="utf-8")
    (src / "ui" / "index.mjs").write_text("export default 1;\n", encoding="utf-8")
    (src / "ui" / "agent.json").write_text('{"name": "ui-agent"}\n', encoding="utf-8")
    sign_app_directory(src, SIGNER, SECRET)
    return src


def _installed(tmp_path: Path, home: Path, **manifest: Any) -> Path:
    from kiro_crew.apps.manager import app_dir, install_app

    _enforce(home)
    result = install_app(_package(tmp_path, **manifest))
    assert result.ok, result.error
    return app_dir(APP)


def _tamper(root: Path) -> None:
    (root / "backend" / "app.py").write_text("print('attacker')\n", encoding="utf-8")


def _manifest(root: Path) -> AppManifest:
    return AppManifest.from_json_file(root / "app.json")


# Activation and update surfaces


def test_enable_refuses_a_changed_signed_file(tmp_path, home):
    from kiro_crew.apps.manager import enable_app

    _tamper(_installed(tmp_path, home))
    result = enable_app(APP)
    assert not result.ok
    assert "backend/app.py" in (result.error or "")


def test_restart_refuses_a_changed_signed_file(tmp_path, home, monkeypatch):
    from kiro_crew.apps.backend_runtime import restart
    from kiro_crew.apps.backend_runtime.restart import _activation_denied

    # Governance is not under test here; admission is.
    monkeypatch.setattr(restart, "_app_activation_denied", lambda name, fail_closed=False: None)
    root = _installed(tmp_path, home)
    assert _activation_denied(APP, "restart").denied is None
    _tamper(root)
    assert "backend/app.py" in (_activation_denied(APP, "restart").denied or "")


def test_the_boot_resource_pass_refuses_a_changed_signed_file(tmp_path, home, monkeypatch):
    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps import bridges

    root = _installed(tmp_path, home)
    registered: list[str] = []
    monkeypatch.setattr(bridges, "register_app", lambda name: registered.append(name))
    monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
    monkeypatch.setattr(bmod, "start_app_backend", lambda name: None)
    monkeypatch.setattr(bmod, "list_apps", lambda: [{"name": APP, "enabled": True}])
    # Positive control: the untampered package is registered.
    bmod.start_enabled_app_backends()
    assert registered == [APP]
    _tamper(root)
    bmod.start_enabled_app_backends()
    assert registered == [APP]


def test_update_refuses_a_source_whose_signed_file_changed(tmp_path, home):
    from kiro_crew.apps.manager import update_app

    _installed(tmp_path, home)
    source = tmp_path / "source" / APP
    _tamper(source)
    result = update_app(source)
    assert not result.ok
    assert "backend/app.py" in (result.error or "")


# Execution boundaries


def test_a_boundary_judges_the_manifest_the_caller_acts_on(tmp_path, home):
    root = _installed(tmp_path, home)
    acted_on = _manifest(root).to_dict()
    assert installed_package_refusal(APP, root, acted_on) is None
    acted_on["backend"] = {"entryPoint": "backend/evil.py"}
    refusal = installed_package_refusal(APP, root, acted_on)
    assert refusal is not None and "changed while it was being checked" in refusal


def test_a_check_that_raises_is_a_refusal(tmp_path, home, monkeypatch):
    from kiro_crew.apps import manager

    root = _installed(tmp_path, home)

    def _boom(_name: str) -> None:
        raise OSError("unreadable")

    monkeypatch.setattr(manager, "get_app_manifest", _boom)
    refusal = installed_package_refusal(APP, root, {})
    assert refusal is not None and "could not run" in refusal


def test_code_from_a_shipped_builtin_package_is_exempt(home):
    _enforce(home)
    root, name = next(iter(execution._shipped_builtin_apps()))
    assert installed_package_refusal(name, root, {}) is None


def test_the_audit_row_names_neither_the_app_nor_the_path(tmp_path, home, monkeypatch):
    root = _installed(tmp_path, home)
    _tamper(root)
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        execution, "sel", lambda: SimpleNamespace(log_api_access=lambda **k: events.append(k))
    )
    reason = execution.app_execution_denied(
        APP, action="backend_spawn", app_root=root / "backend" / "app.py", manifest=_manifest(root)
    )
    assert reason and "backend/app.py" in reason
    assert events[0]["error"] == PACKAGE_MISMATCH
    assert APP not in events[0]["error"] and "backend" not in events[0]["error"]


# Dev mode


def _grant_linked_ui(root: Path, tmp_path: Path, monkeypatch) -> None:
    """``ui/`` linked to a developer's source tree, under the operator's grant."""
    from kiro_crew.apps import dev_mode

    dev_src = tmp_path / "dev-src"
    dev_src.mkdir()
    (dev_src / "index.mjs").write_text("export default 'live edit';\n", encoding="utf-8")
    (dev_src / "agent.json").write_text('{"name": "edited"}\n', encoding="utf-8")
    for entry in (root / "ui").iterdir():
        entry.unlink()
    (root / "ui").rmdir()
    make_dir_link(root / "ui", dev_src)
    granted = os.path.realpath(root / "ui")
    monkeypatch.setattr(dev_mode, "dev_mode_granted_root", lambda name: granted)


def test_an_out_of_install_ui_grant_skips_only_ui_files_that_are_not_code(
    tmp_path, home, monkeypatch
):
    root = _installed(tmp_path, home)
    _grant_linked_ui(root, tmp_path, monkeypatch)
    manifest = _manifest(root)
    assert app_admission_denied(APP, manifest=manifest, action="enable", app_root=root) is None
    # Outside ui/, nothing is skipped.
    _tamper(root)
    assert "backend/app.py" in (
        app_admission_denied(APP, manifest=manifest, action="enable", app_root=root) or ""
    )


def test_a_ui_path_the_manifest_names_as_code_stays_checked(tmp_path, home, monkeypatch):
    root = _installed(tmp_path, home, agents=["ui/agent.json"])
    _grant_linked_ui(root, tmp_path, monkeypatch)
    denied = app_admission_denied(APP, manifest=_manifest(root), action="enable", app_root=root)
    assert "ui/agent.json" in (denied or "")


def test_an_in_install_ui_grant_skips_nothing(tmp_path, home, monkeypatch):
    from kiro_crew.apps import dev_mode

    root = _installed(tmp_path, home)
    (root / "ui" / "index.mjs").write_text("export default 2;\n", encoding="utf-8")
    granted = os.path.realpath(root / "ui")
    monkeypatch.setattr(dev_mode, "dev_mode_granted_root", lambda name: granted)
    denied = app_admission_denied(APP, manifest=_manifest(root), action="enable", app_root=root)
    assert "ui/index.mjs" in (denied or "")


def test_a_ui_script_named_by_setup_stays_checked(tmp_path, home, monkeypatch):
    root = _installed(tmp_path, home, setup={"onEnable": "python3 ui/agent.json"})
    _grant_linked_ui(root, tmp_path, monkeypatch)
    denied = app_admission_denied(APP, manifest=_manifest(root), action="enable", app_root=root)
    assert "ui/agent.json" in (denied or "")


def test_an_app_manifest_whose_document_differs_is_refused(tmp_path, home):
    root = _installed(tmp_path, home)
    other = _manifest(root)
    other.document = {**other.document, "description": "swapped"}
    refusal = installed_package_refusal(APP, root, other)
    assert refusal is not None and "changed while it was being checked" in refusal


def test_a_caller_with_no_manifest_gets_its_own_reason(tmp_path, home):
    root = _installed(tmp_path, home)
    refusal = installed_package_refusal(APP, root, {})
    assert refusal is not None and "has no manifest to check" in refusal


def test_a_builtin_exemption_that_cannot_read_the_policy_falls_back_to_provenance(monkeypatch):
    def _boom(**_k: Any) -> None:
        raise ValueError("malformed policy")

    monkeypatch.setattr(admission, "load_app_admission_policy", _boom)
    shipped = next(iter(execution._shipped_builtin_apps()))[1]
    assert admission.builtin_admission_exempt(shipped, "builtin") is True
    assert admission.builtin_admission_exempt("not-shipped", "builtin") is False


# Audit rows of every admission site


def _audited(monkeypatch, module: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        module, "sel", lambda: SimpleNamespace(log_api_access=lambda **k: events.append(k))
    )
    return events


def _assert_class_only(events: list[dict[str, Any]], operation: str) -> None:
    rows = [e for e in events if e.get("operation") == operation]
    assert rows, f"no {operation} audit row"
    assert rows[0]["error"] == PACKAGE_MISMATCH
    assert APP not in rows[0]["resources"] and APP not in rows[0]["error"]
    assert "backend" not in rows[0]["error"]


def test_the_install_audit_row_carries_only_the_class(tmp_path, home, monkeypatch):
    from kiro_crew.apps import manager_install
    from kiro_crew.apps.manager import install_app

    _enforce(home)
    source = _package(tmp_path)
    _tamper(source)
    events = _audited(monkeypatch, manager_install)
    result = install_app(source)
    assert not result.ok and "backend/app.py" in (result.error or "")
    _assert_class_only(events, "admission")


def test_the_update_audit_row_carries_only_the_class(tmp_path, home, monkeypatch):
    from kiro_crew.apps import manager_install
    from kiro_crew.apps.manager import update_app

    _installed(tmp_path, home)
    source = tmp_path / "source" / APP
    _tamper(source)
    events = _audited(monkeypatch, manager_install)
    assert not update_app(source).ok
    _assert_class_only(events, "admission")


def test_the_enable_audit_row_carries_only_the_class(tmp_path, home, monkeypatch):
    from kiro_crew.apps import manager_activation
    from kiro_crew.apps.manager import enable_app

    _tamper(_installed(tmp_path, home))
    events = _audited(monkeypatch, manager_activation)
    assert not enable_app(APP).ok
    _assert_class_only(events, "admission")


# Bytecode beside a signed .py


def test_app_python_children_keep_their_bytecode_outside_the_app(tmp_path, home):
    from kiro_crew.apps.manager import app_dir

    assert new_pycache_dir(APP) is None and signed_pycache_env(None) == {}
    _enforce(home)
    env = signed_pycache_env(new_pycache_dir(APP))
    prefix = Path(env["PYTHONPYCACHEPREFIX"]).resolve()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not prefix.is_relative_to(app_dir(APP).resolve())
    assert prefix.is_relative_to(home.resolve())
    assert not prefix.is_relative_to((home / "cache").resolve())


def _in_folder_pyc(source: Path) -> Path:
    """The ``__pycache__`` path the stock loader reads for *source*, inside its folder."""
    return source.parent / "__pycache__" / f"{source.stem}.{sys.implementation.cache_tag}.pyc"


def _plant_pyc(source: Path, planted_text: str, monkeypatch) -> None:
    """A ``__pycache__`` entry for *source* that holds other code with a matching header.

    ``sys.pycache_prefix`` is cleared so the stock loader looks in the folder, where
    this plants it, whatever prefix the test process runs with.
    """
    monkeypatch.setattr(sys, "pycache_prefix", None)
    assert len(planted_text) == len(source.read_text(encoding="utf-8"))
    stand_in = source.with_name("stand_in_src.py")
    stand_in.write_text(planted_text, encoding="utf-8")
    info = source.stat()
    os.utime(stand_in, ns=(info.st_atime_ns, info.st_mtime_ns))
    py_compile.compile(
        str(stand_in),
        cfile=str(_in_folder_pyc(source)),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    stand_in.unlink()


def _load(loader_cls: type, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "planted_probe", str(path), loader=loader_cls("planted_probe", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_planted_pyc_is_not_used_by_the_source_only_loader(tmp_path, monkeypatch):
    from kiro_crew.apps.module_loader import _SourceOnlyLoader

    source = tmp_path / "probe.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _plant_pyc(source, "VALUE = 2\n", monkeypatch)
    # Control: the stock loader runs the planted bytecode.
    assert _load(importlib.machinery.SourceFileLoader, source).VALUE == 2
    assert _load(_SourceOnlyLoader, source).VALUE == 1


def test_a_hook_sibling_import_skips_a_planted_pyc(tmp_path, home, monkeypatch):
    from kiro_crew.apps.module_loader import load_app_module, unload_app_modules

    src = _package(tmp_path)
    (src / "backend" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (src / "backend" / "routes.py").write_text(
        "from . import helper\n\n\ndef value():\n    return helper.VALUE\n", encoding="utf-8"
    )
    sign_app_directory(src, SIGNER, SECRET)
    from kiro_crew.apps.manager import app_dir, install_app

    _enforce(home)
    assert install_app(src).ok
    root = app_dir(APP)
    _plant_pyc(root / "backend" / "helper.py", "VALUE = 2\n", monkeypatch)
    planted = _in_folder_pyc(root / "backend" / "helper.py")
    assert planted.is_file() and planted.resolve().is_relative_to(root.resolve())
    try:
        assert load_app_module(APP, root, "backend.routes:value")() == 1
    finally:
        unload_app_modules(APP)


def test_the_policy_is_read_quietly_on_the_execution_side(home, monkeypatch):
    (home / "app_admission.json").write_text("{not json", encoding="utf-8")
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        admission, "sel", lambda: SimpleNamespace(log_api_access=lambda **k: events.append(k))
    )
    assert admission.signed_packages_required() is True
    assert events == []


def test_a_signed_module_wins_over_an_unlisted_package_of_the_same_name(tmp_path, home):
    from kiro_crew.apps.manager import app_dir, install_app
    from kiro_crew.apps.module_loader import load_app_module, unload_app_modules

    src = _package(tmp_path)
    (src / "backend" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (src / "backend" / "routes.py").write_text(
        "from . import helper\n\n\ndef value():\n    return helper.VALUE\n", encoding="utf-8"
    )
    sign_app_directory(src, SIGNER, SECRET)
    _enforce(home)
    assert install_app(src).ok
    root = app_dir(APP)
    (root / "backend" / "helper").mkdir()
    (root / "backend" / "helper" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    try:
        assert load_app_module(APP, root, "backend.routes:value")() == 1
    finally:
        unload_app_modules(APP)


def test_a_listed_bare_pyc_still_loads(tmp_path, home):
    from kiro_crew.apps.manager import app_dir, install_app
    from kiro_crew.apps.module_loader import load_app_module, unload_app_modules

    src = _package(tmp_path)
    (src / "backend" / "routes.py").write_text(
        "from . import compiled\n\n\ndef value():\n    return compiled.VALUE\n",
        encoding="utf-8",
    )
    stand_in = src / "backend" / "compiled_src.py"
    stand_in.write_text("VALUE = 3\n", encoding="utf-8")
    py_compile.compile(str(stand_in), cfile=str(src / "backend" / "compiled.pyc"), doraise=True)
    stand_in.unlink()
    sign_app_directory(src, SIGNER, SECRET)
    _enforce(home)
    assert install_app(src).ok
    try:
        assert load_app_module(APP, app_dir(APP), "backend.routes:value")() == 3
    finally:
        unload_app_modules(APP)


def test_a_self_registered_app_is_judged_by_its_manifest_signature_only(home):
    """``register_external_app`` posts a manifest only; its listed files are elsewhere."""
    from kiro_crew.apps.manager import app_dir, get_app_manifest, register_external_app
    from kiro_crew.apps.package_signature import package_signature

    _enforce(home)
    document = {
        "name": APP,
        "version": "1.0.0",
        "displayName": "Self",
        "description": "d",
        "signer": SIGNER,
        "signatureScheme": 2,
        "signedFiles": {"server.py": "0" * 64},
    }
    document["signature"] = package_signature(SECRET, document)
    assert register_external_app(APP, "1.0.0", "Self", manifest_data=document).ok
    manifest = get_app_manifest(APP)
    root = app_dir(APP)
    assert app_admission_denied(APP, manifest=manifest, action="enable", app_root=root) is None
    assert installed_package_refusal(APP, root, manifest) is None


@pytest.mark.asyncio
async def test_the_enable_route_audits_only_the_class(tmp_path, home, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from dashboard_owner_helpers import as_owner

    from kiro_crew.apps import routes_enable
    from kiro_crew.apps.routes import register_app_routes

    _tamper(_installed(tmp_path, home))
    events = _audited(monkeypatch, routes_enable)
    app = web.Application()
    register_app_routes(app)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(f"/api/apps/{APP}/enable", json={})
        assert response.status == 400
        assert "backend/app.py" in (await response.json())["error"]
    _assert_class_only(events, "app_enable")


def test_a_forged_self_registration_record_does_not_skip_the_files(tmp_path, home):
    """``lifecycle: "app"`` written into a registry app's own record changes nothing."""
    from kiro_crew.apps.manager import _read_installed, _write_installed

    root = _installed(tmp_path, home)
    record = _read_installed(APP)
    assert record is not None
    record.lifecycle = "app"
    _write_installed(APP, record)
    _tamper(root)
    manifest = _manifest(root)
    denied = app_admission_denied(APP, manifest=manifest, action="enable", app_root=root)
    assert "backend/app.py" in (denied or "")
    assert "backend/app.py" in (installed_package_refusal(APP, root, manifest) or "")


def _self_register(home: Path, **fields: Any) -> Path:
    from kiro_crew.apps.manager import app_dir, register_external_app
    from kiro_crew.apps.package_signature import package_signature

    _enforce(home)
    document = {
        "name": APP,
        "version": "1.0.0",
        "displayName": "Self",
        "description": "d",
        "signer": SIGNER,
        "signatureScheme": 2,
        "signedFiles": {},
        **fields,
    }
    document["signature"] = package_signature(SECRET, document)
    assert register_external_app(APP, "1.0.0", "Self", manifest_data=document).ok
    return app_dir(APP)


def _admitted(root: Path) -> None:
    from kiro_crew.apps.manager import get_app_manifest

    manifest = get_app_manifest(APP)
    assert app_admission_denied(APP, manifest=manifest, action="enable", app_root=root) is None
    assert installed_package_refusal(APP, root, manifest) is None


def test_a_self_registered_app_naming_agents_elsewhere_is_admitted(home):
    _admitted(_self_register(home, agents=["agents/helper.json"], skills=["skills/s"]))


def test_a_registry_self_managed_layout_is_admitted(home, tmp_path):
    """Its code lives in ``app-sources/<name>/``, outside the installed folder."""
    source = tmp_path / "app-sources" / APP
    source.mkdir(parents=True)
    (source / "server.py").write_text("print('outside')\n", encoding="utf-8")
    root = _self_register(
        home,
        openCommand=f"python3 {source / 'server.py'}",
        mcpServers={"tools": {"command": "python3", "args": [str(source / "server.py")]}},
    )
    _admitted(root)


def test_a_forged_lifecycle_with_a_named_file_under_data_is_refused(tmp_path, home):
    from kiro_crew.apps.manager import _read_installed, _write_installed

    root = _installed(tmp_path, home, agents=["data/agent.json"])
    record = _read_installed(APP)
    assert record is not None
    record.lifecycle = "app"
    _write_installed(APP, record)
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "agent.json").write_text('{"name": "planted"}\n', encoding="utf-8")
    denied = app_admission_denied(APP, manifest=_manifest(root), action="enable", app_root=root)
    assert "data/agent.json" in (denied or "")
