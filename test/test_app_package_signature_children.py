"""App Python children read no bytecode from the app folder under the signature policy.

The registry ``onInstall`` child gets ``PYTHONPYCACHEPREFIX`` and
``PYTHONDONTWRITEBYTECODE``; an app's cron script child gets ``-B -X pycache_prefix``
after the interpreter, which ``-I`` does not switch off. Without the policy both are
exactly what they were. The backend, stdio MCP, lifecycle and ``openCommand`` sites are
pinned beside their own tests.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_escaping_link
from kiro_crew.apps import admission, signed_first
from kiro_crew.apps.registry_pipeline.install import _install_script_env
from kiro_crew.apps.registry_pipeline.subprocess_env import minimal_env
from kiro_crew.apps.signed_first import FD_ENV, LIST_ENV
from kiro_crew.cron_script import _app_python_args, run_script_sandboxed
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

APP = "child-env-app"


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "crew-home"
    (home / "crons").mkdir(parents=True)
    app = home / "apps" / APP
    app.mkdir(parents=True)
    (app / "job.py").write_text("def run(ctx):\n    return None\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:child-env-test")
    return home


def _enforce(home: Path) -> None:
    (home / "app_admission.json").write_text(
        json.dumps({"mode": "enforce", "require_signature": True, "trust_keys": {}}),
        encoding="utf-8",
    )


def test_the_on_install_env_is_unchanged_without_the_policy(home):
    assert admission.new_pycache_dir(APP) is None
    assert _install_script_env(None) == minimal_env(NONINTERACTIVE="1")


def test_the_on_install_env_names_an_out_of_folder_prefix_under_the_policy(home):
    _enforce(home)
    env = _install_script_env(admission.new_pycache_dir(APP))
    expected = admission.signed_pycache_env(env["PYTHONPYCACHEPREFIX"])
    assert expected and {k: env[k] for k in expected} == expected
    assert {k: v for k, v in env.items() if k not in expected} == minimal_env(NONINTERACTIVE="1")
    prefix = Path(expected["PYTHONPYCACHEPREFIX"])
    assert not prefix.is_relative_to(home / "apps" / APP)


def _cron_argv(monkeypatch, home: Path) -> list[str]:
    """The argv an app cron script's child is spawned with, stopped before it runs."""
    seen: list[list[str]] = []

    def _record(argv, **_kwargs):
        seen.append(list(argv))
        raise OSError("recorded the cron child's argv; stop before the spawn")

    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _record)
    with contextlib.suppress(OSError):
        run_script_sandboxed(
            f"{home / 'apps' / APP / 'job.py'}:run", "job-child-env", timeout=FALLBACK_TIMEOUT_SECS
        )
    assert seen, "the script cron never reached its spawn"
    return seen[-1]


def test_a_cron_script_child_gets_the_prefix_flags_only_under_the_policy(home, monkeypatch):
    off = _cron_argv(monkeypatch, home)
    assert "-B" not in off
    assert not any(arg.startswith("pycache_prefix=") for arg in off)

    _enforce(home)
    # The fixture is unsigned, so the fire-time package check would refuse it; this
    # test measures only the interpreter flags.
    monkeypatch.setattr(
        "kiro_crew.cron_script._app_script_owner", lambda script_path, file_path: (APP, None, None)
    )
    on = _cron_argv(monkeypatch, home)
    flags = on[1:4]
    assert flags[:2] == ["-B", "-X"] and flags[2].startswith("pycache_prefix=")
    # The launcher is a fresh temp file each run, so compare the shape around it.
    assert on[0] == off[0] and len(on) == len(off) + len(flags)


def test_a_script_outside_every_app_gets_no_flags(home, monkeypatch):
    _enforce(home)
    script = home / "crons" / "own.py"
    script.write_text("def run(ctx):\n    return None\n", encoding="utf-8")
    seen: list[list[str]] = []

    def _record(argv, **_kwargs):
        seen.append(list(argv))
        raise OSError("stop")

    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _record)
    with contextlib.suppress(OSError):
        run_script_sandboxed(f"{script}:run", "job-own", timeout=FALLBACK_TIMEOUT_SECS)
    assert seen and "-B" not in seen[-1]


def test_the_npm_installs_keep_their_argv_without_the_policy_and_use_the_lockfile_with_it(
    home, tmp_path
):
    from kiro_crew.apps.admission import signed_npm_install_argv

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    backend = ["npm", "install", "--production", "--no-audit", "--no-fund"]
    backend_ci = ["npm", "ci", "--omit=dev", "--no-audit", "--no-fund"]
    assert signed_npm_install_argv(backend, pkg, ci_argv=backend_ci) == backend
    assert signed_npm_install_argv(["npm", "install"], pkg, ci_argv=["npm", "ci"]) == [
        "npm",
        "install",
    ]
    _enforce(home)
    assert signed_npm_install_argv(backend, pkg, ci_argv=backend_ci) == [*backend, "--no-save"]
    assert signed_npm_install_argv(["npm", "install"], pkg, ci_argv=["npm", "ci"]) == [
        "npm",
        "install",
        "--no-save",
    ]
    (pkg / "package-lock.json").write_text("{}\n", encoding="utf-8")
    assert signed_npm_install_argv(backend, pkg, ci_argv=backend_ci) == backend_ci
    assert signed_npm_install_argv(["npm", "install"], pkg, ci_argv=["npm", "ci"]) == ["npm", "ci"]


# Signed-first imports in the Python children the gateway launches


def _shim_run(
    tmp_path: Path, root: Path, files: list[str], *target: str, app: str = APP, handed: bool = True
) -> subprocess.CompletedProcess:
    """Run *target* through the signed-first shim, the list handed over a pipe as the gateway does.

    *files* are listed with the digest they have now, as a signed inventory would.
    """
    digests = {rel: _sha256(root / rel) for rel in files}
    payload = json.dumps({"app": APP, "root": os.path.realpath(root), "files": digests})
    env = {k: v for k, v in os.environ.items() if k not in (FD_ENV, LIST_ENV)}
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload.encode("utf-8"))
    finally:
        os.close(write_fd)
    spawn: dict = {}
    if handed and sys.platform == "win32":
        import msvcrt

        handle = msvcrt.get_osfhandle(read_fd)
        os.set_handle_inheritable(handle, True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [handle]}
        env[FD_ENV] = str(handle)
        spawn = {"startupinfo": startup, "close_fds": True}
    elif handed:
        env[FD_ENV] = str(read_fd)
        spawn = {"pass_fds": (read_fd,)}
    try:
        return subprocess.run(
            [sys.executable, "-B", signed_first.__file__, app, *target],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=FALLBACK_TIMEOUT_SECS,
            **UTF8_TEXT,
            **spawn,
        )
    finally:
        os.close(read_fd)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _list_file(root: Path, checked: dict) -> str:
    """Write *checked* as the gateway writes a stdio checked list; the env holds its path."""
    path = root.parent / f"{root.name}-checked-list.json"
    path.write_text(json.dumps(checked), encoding="utf-8")
    return str(path)


def _out(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_an_unlisted_parent_package_does_not_stand_in_for_the_signed_asgi_module(tmp_path):
    """uvicorn imports ``backend.app``: a plain launch picks an unlisted
    ``backend/app/__init__.py`` over the signed ``backend/app.py``, so the shim refuses
    to start while that unsigned package shadows the signed module."""
    root = tmp_path / "app"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app.py").write_text("VALUE = 'signed'\n", encoding="utf-8")
    (root / "backend" / "app" / "__init__.py").write_text("VALUE = 'unlisted'\n", encoding="utf-8")
    code = "import backend.app as m; print(m.VALUE)"
    plain = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=root,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    assert plain.stdout.strip() == "unlisted", plain.stderr  # control: the stock order
    result = _shim_run(tmp_path, root, ["backend/app.py"], "-c", code)
    assert result.returncode != 0
    assert "unlisted" not in result.stdout
    assert "signed_first: refusing to run" in result.stderr
    assert "backend/app/__init__.py" in result.stderr and "shadows a signed module" in result.stderr


def test_python_m_runs_the_signed_module_not_an_unlisted_package(tmp_path):
    """``-m worker`` would run an unlisted ``worker/`` package over the signed
    ``worker.py`` (control), so the shim refuses to start."""
    root = tmp_path / "app"
    (root / "worker").mkdir(parents=True)
    (root / "worker.py").write_text("print('signed')\n", encoding="utf-8")
    (root / "worker" / "__init__.py").write_text("", encoding="utf-8")
    (root / "worker" / "__main__.py").write_text("print('unlisted')\n", encoding="utf-8")
    plain = subprocess.run(
        [sys.executable, "-B", "-m", "worker"],
        cwd=root,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    assert plain.stdout.strip() == "unlisted", plain.stderr  # control: the stock order
    result = _shim_run(tmp_path, root, ["worker.py"], "-m", "worker")
    assert result.returncode != 0
    assert "unlisted" not in result.stdout
    assert "worker/__init__.py" in result.stderr and "shadows a signed module" in result.stderr


def test_an_unlisted_module_that_shadows_nothing_signed_still_imports(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text("import extra\nprint(extra.VALUE)\n", encoding="utf-8")
    (root / "extra.py").write_text("VALUE = 'unlisted helper'\n", encoding="utf-8")
    result = _shim_run(tmp_path, root, ["main.py"], str(root / "main.py"))
    assert _out(result) == "unlisted helper"


def test_a_script_entry_runs_through_the_shim(tmp_path):
    """``python server.py`` under the policy: ``runpy.run_path`` asks for an importer for
    the script file itself, and the signed-first hook must not claim a file."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "server.py").write_text("import helpers\nprint(helpers.VALUE)\n", encoding="utf-8")
    (root / "helpers.py").write_text("VALUE = 'served'\n", encoding="utf-8")
    result = _shim_run(tmp_path, root, ["server.py", "helpers.py"], str(root / "server.py"))
    assert _out(result) == "served"


def test_the_shim_refuses_a_listed_file_changed_after_the_list_was_made(tmp_path):
    """The kiro-cli-spawned stdio server case: the gateway's last check ran at
    registration, so the shim re-hashes the listed files against the signed digests."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "server.py").write_text("print('signed')\n", encoding="utf-8")
    checked = {"app": APP, "root": os.path.realpath(root), "files": {}}
    checked["files"] = {"server.py": _sha256(root / "server.py")}
    (root / "server.py").write_text("print('changed')\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in (FD_ENV, LIST_ENV)}
    env[LIST_ENV] = _list_file(root, checked)
    result = subprocess.run(
        [sys.executable, "-B", signed_first.__file__, APP, str(root / "server.py")],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    assert result.returncode != 0
    assert "changed" not in result.stdout
    assert "does not match the signed inventory" in result.stderr


def test_a_child_cannot_change_its_own_checked_list(tmp_path):
    """App code forges the list every way it could reach one: it writes a list file
    where an earlier design kept it, sets both channel variables, opens a pipe of its
    own holding an empty list, and only then plants the shadowing package. The import
    still gets the signed module, because the shim read its list once, from the
    gateway's pipe, before any app code ran; and with the package already there at
    start the shim refuses to run at all."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "helpers.py").write_text("VALUE = 'signed'\n", encoding="utf-8")
    forged = json.dumps({"app": APP, "root": os.path.realpath(root), "files": {}})
    planted = tmp_path / "cache" / "app-signed-files" / f"{APP}.json"
    package = root / "helpers"
    (root / "main.py").write_text(
        "import json, os, pathlib, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        f"p = pathlib.Path({str(planted)!r}); p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"p.write_text({forged!r})\n"
        "r, w = os.pipe(); os.write(w, " + repr(forged.encode()) + "); os.close(w)\n"
        f"os.environ[{FD_ENV!r}] = str(r)\n"
        f"os.environ[{LIST_ENV!r}] = {forged!r}\n"
        f"pkg = pathlib.Path({str(package)!r}); pkg.mkdir(exist_ok=True)\n"
        "(pkg / '__init__.py').write_text(\"VALUE = 'unlisted'\\n\")\n"
        "import helpers\n"
        "print(helpers.VALUE)\n",
        encoding="utf-8",
    )
    forged_run = _shim_run(tmp_path, root, ["helpers.py", "main.py"], str(root / "main.py"))
    assert _out(forged_run) == "signed"  # the forged list and the late package changed nothing
    refused = _shim_run(tmp_path, root, ["helpers.py", "main.py"], str(root / "main.py"))
    assert refused.returncode != 0  # the package is there now, before any app code runs
    assert "helpers/__init__.py" in refused.stderr and "shadows a signed module" in refused.stderr


def test_the_shim_refuses_to_run_without_a_handed_list_or_with_another_apps(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text("print('ran')\n", encoding="utf-8")
    for result in (
        _shim_run(tmp_path, root, ["main.py"], str(root / "main.py"), handed=False),
        _shim_run(tmp_path, root, ["main.py"], str(root / "main.py"), app="other-app"),
    ):
        assert result.returncode != 0
        assert "ran" not in result.stdout
        assert "refusing to run" in result.stderr


def test_signed_first_launch_is_unchanged_without_the_policy(home):
    args = ["-s", "-m", "uvicorn", "backend.app:app"]
    assert admission.signed_first_python_args(APP, args) is args
    assert admission.signed_first_launch_env(APP, args) == {}
    with admission.signed_first_pipe(APP, [sys.executable, *args]) as (env, spawn):
        assert (env, spawn) == ({}, {})
    assert _app_python_args(str(home / "apps" / APP / "job.py"), args) is args
    assert not (home / "cache" / "app-signed-files").exists()


def test_signed_first_launch_puts_the_shim_at_the_target_and_writes_no_file(home, monkeypatch):
    monkeypatch.setattr(
        admission,
        "verified_signed_inventory",
        lambda name: {"job.py": "a" * 64, "ui/index.mjs": "b" * 64},
    )
    flags = ["-B", "-X", "pycache_prefix=p"]
    shim = os.path.abspath(signed_first.__file__)
    for args, target in (
        ([*flags, "-s", "-m", "uvicorn", "backend.app:app"], ["-m", "uvicorn", "backend.app:app"]),
        ([*flags, "-mworker"], ["-m", "worker"]),
        ([*flags, "server.py", "--stdio"], ["server.py", "--stdio"]),
    ):
        out = admission.signed_first_python_args(APP, args)
        at = out.index(shim)
        assert out[at + 1 :] == [APP, *target], out
    launch = admission.signed_first_launch_env(APP, [shim, APP, "server.py"])
    assert Path(launch[LIST_ENV]) == admission.signed_first_list_path(APP)
    assert json.loads(Path(launch[LIST_ENV]).read_text(encoding="utf-8")) == {
        "app": APP,
        "root": os.path.realpath(home / "apps" / APP),
        "files": {"job.py": "a" * 64, "ui/index.mjs": "b" * 64},  # the whole inventory
        "unchecked": [],
    }
    with admission.signed_first_pipe(APP, [sys.executable, shim, APP, "x.py"]) as (env, spawn):
        assert set(env) == {FD_ENV} and spawn
    assert not (home / "cache" / "app-signed-files").exists()


# Deferred launches and the registration sources, under the policy


def _signed_app(home: Path, **manifest: object) -> Path:
    """Install a signed copy of the test app under the policy; its installed folder."""
    from kiro_crew.apps.manager import app_dir, install_app
    from kiro_crew.apps.package_signature import sign_app_directory

    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    (home / "app_admission.json").write_text(
        json.dumps({"mode": "enforce", "require_signature": True, "trust_keys": {"acme": "k"}}),
        encoding="utf-8",
    )
    src = home.parent / "source" / "signed-cron-app"
    src.mkdir(parents=True)
    (src / "app.json").write_text(
        json.dumps(
            {
                "name": "signed-cron-app",
                "version": "1.0.0",
                "displayName": "Signed cron app",
                "description": "A signed app for the child tests.",
            }
            | ({"permissions": {"cron": True}} if "crons" in manifest else {})
            | manifest
        ),
        encoding="utf-8",
    )
    (src / "job.py").write_text("def run(ctx):\n    return None\n", encoding="utf-8")
    sign_app_directory(src, "acme", "k")
    result = install_app(src)
    assert result.ok, result.error
    return app_dir("signed-cron-app")


def test_a_cron_script_changed_after_activation_is_refused_at_the_next_fire(home, monkeypatch):
    root = _signed_app(home)
    spawned: list[list[str]] = []

    def _record(argv, **_kwargs):
        spawned.append(list(argv))
        raise OSError("stop before the spawn")

    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _record)
    with contextlib.suppress(OSError):  # positive control: the signed script reaches its spawn
        run_script_sandboxed(f"{root / 'job.py'}:run", "job-ok", timeout=FALLBACK_TIMEOUT_SECS)
    assert spawned
    spawned.clear()
    (root / "job.py").write_text("def run(ctx):\n    print('changed')\n", encoding="utf-8")
    result = run_script_sandboxed(
        f"{root / 'job.py'}:run", "job-changed", timeout=FALLBACK_TIMEOUT_SECS
    )
    assert result["status"] == "error"
    assert "Refusing to run app cron script" in result["error"]
    assert spawned == []


def test_a_planted_cron_in_the_sidecar_is_not_registered_under_the_policy(home, monkeypatch):
    from kiro_crew.apps import bridges
    from kiro_crew.apps.bridges_crons import _admitted_app_cron_defs, _app_crons_path

    _signed_app(home, crons=[{"name": "declared", "every": 3600, "message": "go"}])
    # The execution decision is judged elsewhere; this pins only where the jobs come from.
    monkeypatch.setattr(bridges, "_registration_denied", lambda *a, **k: None)
    path = _app_crons_path("signed-cron-app")
    defs = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    defs.append({"name": "signed-cron-app:planted", "every": 60, "message": "planted"})
    path.write_text(json.dumps(defs), encoding="utf-8")
    names = [d.get("name") for d in _admitted_app_cron_defs("signed-cron-app")[0]]
    assert names and not any("planted" in str(n) for n in names), names


def test_a_refused_apps_mcp_server_is_not_restored_by_a_config_rebuild(home, monkeypatch):
    from kiro_crew.agent_materialization.mcp_sources import _collect_app_mcp_servers
    from kiro_crew.apps import bridges

    root = _signed_app(home, mcpServers={"tools": {"command": "python3", "args": ["job.py"]}})
    live = {"signed-cron-app:tools": {"command": "python3", "args": ["job.py"]}}
    monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: dict(live))
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
    assert "signed-cron-app:tools" in _collect_app_mcp_servers(audit=False)  # control
    (root / "job.py").write_text("print('changed')\n", encoding="utf-8")
    assert "signed-cron-app:tools" not in _collect_app_mcp_servers(audit=False)


def test_a_drive_qualified_cron_script_names_its_file_not_the_drive():
    from kiro_crew.apps.package_signature import _cron_script_file

    assert _cron_script_file("C:\\apps\\demo\\job.py:run") == "C:\\apps\\demo\\job.py"
    assert _cron_script_file("job.py:run") == "job.py"
    assert _cron_script_file("C:\\apps\\demo\\job.py") == "C:\\apps\\demo\\job.py"


def test_a_changed_extensionless_signed_mcp_target_is_refused_at_child_start(tmp_path):
    """A stdio server whose signed target has no suffix (``bin/serve``) or is a ``.pyz``:
    the shim re-hashes every signed file, not only the importable ones."""
    root = tmp_path / "app"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "serve").write_text("print('signed')\n", encoding="utf-8")
    checked = {
        "app": APP,
        "root": os.path.realpath(root),
        "files": {"bin/serve": _sha256(root / "bin" / "serve")},
    }
    (root / "bin" / "serve").write_text("print('changed')\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in (FD_ENV, LIST_ENV)}
    env[LIST_ENV] = _list_file(root, checked)
    result = subprocess.run(
        [sys.executable, "-B", signed_first.__file__, APP, str(root / "bin" / "serve")],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    assert result.returncode != 0
    assert "changed" not in result.stdout
    assert "bin/serve" in result.stderr


def test_a_signed_cron_script_swapped_for_a_link_to_an_unsigned_crons_file_is_refused(
    home, monkeypatch
):
    root = _signed_app(home)
    outside = home / "crons" / "unsigned"
    outside.mkdir(parents=True)
    (outside / "secret.py").write_text("def run(ctx):\n    print('unsigned')\n", encoding="utf-8")
    # A file link where the host allows one, else a junction: the persisted path is
    # under the app and resolves to the unsigned file outside it.
    rel = make_escaping_link(root, outside)
    spawned: list[list[str]] = []

    def _record(argv, **_kwargs):
        spawned.append(list(argv))
        raise OSError("stop before the spawn")

    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _record)
    result = run_script_sandboxed(f"{root / rel}:run", "job-link", timeout=FALLBACK_TIMEOUT_SECS)
    assert result["status"] == "error"
    assert "Refusing to run app cron script" in result["error"]
    assert spawned == []


def _collect_for(monkeypatch, name: str, manifest) -> dict:
    from kiro_crew.agent_materialization.mcp_sources import _collect_app_mcp_servers
    from kiro_crew.apps import bridges

    monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.apps.manager.list_apps", lambda: [{"name": name}])
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda n: manifest)
    return _collect_app_mcp_servers(audit=False)


def test_a_shipped_builtins_mcp_servers_survive_a_rebuild_under_the_policy(home, monkeypatch):
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manifest import AppManifest

    _enforce(home)
    shipped = shipped_builtin_app_root("mochi")
    assert shipped is not None
    manifest = AppManifest.from_json_file(shipped / "app.json")
    assert manifest.mcpServers
    collected = _collect_for(monkeypatch, "mochi", manifest)
    assert any(key.startswith("mochi:") for key in collected), collected


def test_a_data_home_app_claiming_to_be_builtin_is_still_checked(home, monkeypatch):
    """Only this build's package says what is builtin; a record or manifest that claims
    it does not, so an unsigned app is refused."""
    from kiro_crew.apps.manifest import AppManifest

    _enforce(home)
    name = "claims-builtin"
    root = home / "apps" / name
    root.mkdir(parents=True)
    document = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Claims builtin",
        "description": "Not shipped by this build.",
        "source": "builtin",
        "mcpServers": {"tools": {"command": "python3", "args": ["server.py"]}},
    }
    (root / "app.json").write_text(json.dumps(document), encoding="utf-8")
    (root / "installed.json").write_text(
        json.dumps({"name": name, "origin": "builtin", "source": "builtin", "enabled": True}),
        encoding="utf-8",
    )
    (root / "server.py").write_text("print('unsigned')\n", encoding="utf-8")
    manifest = AppManifest.from_json_file(root / "app.json")
    assert f"{name}:tools" not in _collect_for(monkeypatch, name, manifest)


def _run_inline(root: Path, checked: dict, *argv: str) -> subprocess.CompletedProcess:
    """Run ``python *argv`` from *root* with *checked* handed over as launch-config env."""
    env = {k: v for k, v in os.environ.items() if k not in (FD_ENV, LIST_ENV)}
    env[LIST_ENV] = _list_file(root, checked)
    return subprocess.run(
        [sys.executable, *argv],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )


def test_a_clustered_m_option_launches_through_the_shim(tmp_path, home, monkeypatch):
    """``["-Bm", "worker"]`` is ``-B -m worker`` to Python, so the shim takes the
    module's place and the server still starts."""
    monkeypatch.setattr(admission, "verified_signed_inventory", lambda name: {"worker.py": "x"})
    args = admission.signed_first_python_args(APP, ["-Bm", "worker"])
    shim = os.path.abspath(signed_first.__file__)
    assert args == ["-B", shim, APP, "-m", "worker"], args
    assert admission.signed_first_python_args(APP, ["-Bmworker"]) == args
    assert admission.signed_first_python_args(APP, ["-Bc", "print(1)"])[1:3] == [shim, APP]
    root = tmp_path / "app"
    root.mkdir()
    (root / "worker.py").write_text("print('worker up')\n", encoding="utf-8")
    checked = {
        "app": APP,
        "root": os.path.realpath(root),
        "files": {"worker.py": _sha256(root / "worker.py")},
    }
    assert _out(_run_inline(root, checked, *args)) == "worker up"


def test_the_child_skips_exactly_the_dev_mode_ui_files_admission_skips(home, monkeypatch):
    """An out-of-install ``ui/`` grant leaves ``ui/`` files out at admission; the child
    leaves out the same files, so an edited granted tree still starts."""
    monkeypatch.setattr(
        admission,
        "verified_signed_inventory",
        lambda name: {"job.py": "a" * 64, "ui/index.mjs": "b" * 64, "ui/agent.json": "c" * 64},
    )
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda name: object())
    skipped = {"ui/index.mjs"}
    monkeypatch.setattr(admission, "_ui_skip", lambda name, manifest, root: skipped.__contains__)
    checked = json.loads(admission.signed_first_payload(APP) or "{}")
    assert checked["unchecked"] == ["ui/index.mjs"]


def test_an_edited_unchecked_file_does_not_stop_the_child(tmp_path):
    root = tmp_path / "app"
    (root / "ui").mkdir(parents=True)
    (root / "main.py").write_text("print('started')\n", encoding="utf-8")
    (root / "ui" / "index.mjs").write_text("export default 1;\n", encoding="utf-8")
    checked = {
        "app": APP,
        "root": os.path.realpath(root),
        "files": {
            "main.py": _sha256(root / "main.py"),
            "ui/index.mjs": _sha256(root / "ui" / "index.mjs"),
        },
        "unchecked": ["ui/index.mjs"],
    }
    (root / "ui" / "index.mjs").write_text("export default 'edited';\n", encoding="utf-8")
    shim = signed_first.__file__
    assert _out(_run_inline(root, checked, "-B", shim, APP, str(root / "main.py"))) == "started"


def test_a_granted_signed_cron_reaches_its_spawn_under_the_policy(home, monkeypatch):
    """A granted run hides the script's folder from the child; the child's recheck
    leaves out exactly the listed files there, which the gateway checked at this spawn."""
    root = _signed_app(home)
    monkeypatch.setattr(
        "kiro_crew.cron_script._secret_env_precheck",
        lambda *a, **k: ({"KC_SIGNED_CRON_TEST": "value"}, None),
    )
    hidden_seen: list[tuple[str, ...]] = []
    real_payload = admission.signed_first_payload
    payloads: list[dict] = []

    def _payload(name, **kwargs):
        hidden_seen.append(tuple(kwargs.get("hidden_dirs", ())))
        text = real_payload(name, **kwargs)
        payloads.append(json.loads(text) if text else {})
        return text

    monkeypatch.setattr(admission, "signed_first_payload", _payload)
    # A wrapped argv, as an OS sandbox gives one: a granted run refuses to start on a
    # host whose wrap leaves the argv unchanged (no sandbox to hold the secrets in).
    monkeypatch.setattr(
        "kiro_crew.cron_script.wrap_argv", lambda argv, **kw: (["sandbox-wrap", *argv], None)
    )
    spawned: list[list[str]] = []

    def _spawn(argv, **_kwargs):
        spawned.append(list(argv))
        raise OSError("recorded the granted cron's spawn")

    monkeypatch.setattr("kiro_crew.cron_script.popen_limited", _spawn)
    with contextlib.suppress(OSError):
        run_script_sandboxed(
            f"{root / 'job.py'}:run",
            "job-granted",
            timeout=FALLBACK_TIMEOUT_SECS,
            secret_env={"KC_SIGNED_CRON_TEST": "vault:x"},
            secret_env_pin="pin",
        )
    assert spawned, "the granted signed cron never reached its spawn"
    assert payloads and "job.py" in payloads[-1]["unchecked"], payloads
    assert any(os.path.realpath(root) in map(os.path.realpath, h) for h in hidden_seen)


def test_the_shim_refuses_a_listed_file_reached_through_a_link(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("print('elsewhere')\n", encoding="utf-8")
    rel = make_escaping_link(root, outside)
    (root / "main.py").write_text("print('started')\n", encoding="utf-8")
    checked = {
        "app": APP,
        "root": os.path.realpath(root),
        "files": {"main.py": _sha256(root / "main.py"), rel: _sha256(outside / "secret.py")},
    }
    result = _run_inline(root, checked, "-B", signed_first.__file__, APP, str(root / "main.py"))
    assert result.returncode != 0
    assert "started" not in result.stdout
    assert rel in result.stderr


def test_the_shim_refuses_an_unsigned_package_beside_a_signed_module(tmp_path):
    root = tmp_path / "app"
    (root / "helpers").mkdir(parents=True)
    (root / "helpers.py").write_text("VALUE = 'signed'\n", encoding="utf-8")
    (root / "helpers" / "__init__.py").write_text("VALUE = 'unsigned'\n", encoding="utf-8")
    (root / "main.py").write_text("import helpers\nprint(helpers.VALUE)\n", encoding="utf-8")
    checked = {
        "app": APP,
        "root": os.path.realpath(root),
        "files": {"helpers.py": _sha256(root / "helpers.py"), "main.py": _sha256(root / "main.py")},
    }
    result = _run_inline(root, checked, "-B", signed_first.__file__, APP, str(root / "main.py"))
    assert result.returncode != 0
    assert "helpers/__init__.py" in result.stderr


def test_a_server_added_to_an_installed_builtins_manifest_is_not_emitted(home, monkeypatch):
    """Under the policy a shipped builtin's servers come from its shipped manifest."""
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manifest import AppManifest

    _enforce(home)
    shipped = shipped_builtin_app_root("mochi")
    assert shipped is not None
    document = json.loads((shipped / "app.json").read_text(encoding="utf-8"))
    document.setdefault("mcpServers", {})["planted"] = {"command": "python3", "args": ["x.py"]}
    installed = AppManifest.from_dict(document)
    collected = _collect_for(monkeypatch, "mochi", installed)
    assert "mochi:planted" not in collected, collected
    assert any(key.startswith("mochi:") for key in collected), collected


def test_the_manifest_fallback_spec_carries_the_shim(home, monkeypatch):
    from kiro_crew.agent_materialization.mcp_sources import _collect_app_mcp_servers
    from kiro_crew.apps import bridges

    _signed_app(home, mcpServers={"tools": {"command": "python3", "args": ["job.py"]}})
    monkeypatch.setattr(bridges, "registered_app_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True, raising=False)
    spec = _collect_app_mcp_servers(audit=False)["signed-cron-app:tools"]
    assert os.path.abspath(signed_first.__file__) in spec["args"], spec
    assert LIST_ENV in spec.get("env", {}), spec


# Per-spawn bytecode cache dirs and the builtin exemption, under the policy


@pytest.mark.asyncio
async def test_each_spawn_gets_a_fresh_private_pycache_dir_removed_at_exit(home):
    import asyncio
    import stat as stat_module

    _enforce(home)
    first, second = admission.new_pycache_dir(APP), admission.new_pycache_dir(APP)
    assert first and second and first != second
    for path in (first, second):
        assert os.listdir(path) == []
        assert Path(path).is_relative_to(home / "run" / "app-pycache")
        if os.name == "posix":
            assert stat_module.S_IMODE(os.stat(path).st_mode) == 0o700
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    admission.release_pycache_dir_after(proc, first)
    await proc.wait()
    await asyncio.wait_for(
        asyncio.gather(*list(admission._pycache_reapers)), timeout=FALLBACK_TIMEOUT_SECS
    )
    assert not os.path.exists(first)  # removed once the child exited
    admission.hold_pycache_dir("backend:" + APP, second)
    replacement = admission.new_pycache_dir(APP)
    admission.hold_pycache_dir("backend:" + APP, replacement)  # the next spawn replaces it
    assert not os.path.exists(second) and os.path.isdir(replacement)
    admission.release_pycache_dir("backend:" + APP)  # the stop path
    assert not os.path.exists(replacement)


#: The shipped builtins this build installs with a backend or a role every activation
#: path (enable, boot, restart) checks through ``builtin_admission_exempt``.
_SHIPPED_BUILTINS = (
    "design-tweak",
    "dev-fleet",
    "file-explorer",
    "md-notebook",
    "workflows",
    "ops-mission-control",
)


def _installed_builtin_document(name: str) -> dict:
    """The ``app.json`` ``register_builtin_apps`` writes for shipped builtin *name*."""
    from kiro_crew.apps.discovery import _manifest_to_builtin_dict
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manifest import AppManifest

    shipped = shipped_builtin_app_root(name)
    assert shipped is not None, name
    built = _manifest_to_builtin_dict(AppManifest.from_json_file(shipped / "app.json"))
    return json.loads(json.dumps(built, indent=2))


def _installed_as(monkeypatch, document: dict) -> None:
    from kiro_crew.apps.manifest import AppManifest

    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest", lambda n: AppManifest.from_dict(document)
    )


@pytest.mark.parametrize("name", _SHIPPED_BUILTINS)
def test_every_shipped_builtin_as_installed_stays_exempt_under_the_policy(name, home, monkeypatch):
    """Enable, boot and restart all decide through ``builtin_admission_exempt``; the
    installed copy the builtin install writes must keep its exemption."""
    _enforce(home)
    _installed_as(monkeypatch, _installed_builtin_document(name))
    assert admission.builtin_admission_exempt(name, "builtin")


def test_a_builtin_with_an_added_server_is_checked_and_refused_unsigned(home, monkeypatch):
    from kiro_crew.apps.manager import app_dir

    _enforce(home)
    name = "file-explorer"
    document = _installed_builtin_document(name)
    document.setdefault("mcpServers", {})["planted"] = {"command": "python3", "args": ["x.py"]}
    _installed_as(monkeypatch, document)
    assert not admission.builtin_admission_exempt(name, "builtin")
    root = app_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(document), encoding="utf-8")
    from kiro_crew.apps.manifest import AppManifest

    refusal = admission.app_admission_denied(
        name, manifest=AppManifest.from_dict(document), action="enable", app_root=root
    )
    assert refusal, "an unsigned copy with an added server must be refused"


def test_a_builtin_whose_installed_backend_runs_another_module_is_not_exempt(home, monkeypatch):
    """Exempt only while everything the installed manifest runs is the shipped code."""
    _enforce(home)
    name = "file-explorer"
    document = _installed_builtin_document(name)
    _installed_as(monkeypatch, document)
    assert admission.builtin_admission_exempt(name, "builtin")  # the unmodified builtin
    changed = json.loads(json.dumps(document))
    changed["backend"]["entryPoint"] = "http.server"
    _installed_as(monkeypatch, changed)
    assert not admission.builtin_admission_exempt(name, "builtin")


@pytest.mark.asyncio
async def test_the_signed_load_prep_runs_off_the_loop_and_the_load_reuses_it(
    home, monkeypatch, tmp_path
):
    """An async caller's hook or route load reads the inventory and scans the importer
    cache in a worker thread; the load inside the block applies that result."""
    import threading

    from kiro_crew.apps import module_loader

    loop_thread = threading.get_ident()
    calls: list[int] = []
    real = module_loader.prepare_signed_load

    def _recording(app_name, app_dir):
        calls.append(threading.get_ident())
        return real(app_name, app_dir)

    monkeypatch.setattr(module_loader, "prepare_signed_load", _recording)
    async with module_loader.signed_load_off_loop(APP, tmp_path):
        handed = module_loader._prep_for(APP, tmp_path)
    assert calls and calls[0] != loop_thread  # the blocking half ran in a worker thread
    assert len(calls) == 1  # the load inside the block did not repeat it
    assert handed is None  # no policy: nothing to switch
    module_loader._prep_for(APP, tmp_path)  # outside the block it computes, as before
    assert len(calls) == 2


def test_a_large_checked_list_keeps_the_stdio_launch_env_small(tmp_path, home, monkeypatch):
    """A list far larger than an env string may be rides in the read-only list file;
    the launch env holds only its path, and the shim reads the whole list from it."""
    # ~480 KiB: 4000 entries of a 40-character path and a 64-character digest each.
    inventory = {f"pkg/deeply/nested/module_{i:06d}.py": f"{i:064x}" for i in range(4000)}
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text("print('started')\n", encoding="utf-8")
    inventory["main.py"] = _sha256(root / "main.py")
    unchecked = sorted(rel for rel in inventory if rel != "main.py")
    monkeypatch.setattr(admission, "verified_signed_inventory", lambda name: dict(inventory))
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda name: None)
    monkeypatch.setattr("kiro_crew.apps.manager.app_dir", lambda name: root)
    shim = os.path.abspath(signed_first.__file__)
    launch = admission.signed_first_launch_env(APP, [shim, APP, "main.py"])
    assert len(json.dumps(launch)) < 4096, len(json.dumps(launch))
    listed = Path(launch[LIST_ENV])
    assert listed.stat().st_size > 300 * 1024
    # The listed modules do not exist here; the child is told not to re-hash them.
    document = json.loads(listed.read_text(encoding="utf-8"))
    document["unchecked"] = unchecked
    listed.write_text(json.dumps(document), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in (FD_ENV, LIST_ENV)}
    env[LIST_ENV] = str(listed)
    result = subprocess.run(
        [sys.executable, "-B", shim, APP, str(root / "main.py")],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    assert _out(result) == "started"
    admission.remove_signed_first_list(APP)
    assert not listed.exists()


def test_a_self_registered_app_gets_no_shim_and_no_list(home, monkeypatch):
    """A real self-registered app's folder holds only metadata; its inventory names
    files elsewhere. Its Python stdio launch keeps main's shape: no shim, no list."""
    from kiro_crew.apps.manager import app_dir
    from kiro_crew.apps.manifest import AppManifest

    _enforce(home)
    name = "self-reg-app"
    root = app_dir(name)
    root.mkdir(parents=True)
    document = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Self registered",
        "description": "Runs from its own location.",
        "mcpServers": {"tools": {"command": "python3", "args": ["/srv/self-reg/server.py"]}},
    }
    (root / "app.json").write_text(json.dumps(document), encoding="utf-8")
    (root / "installed.json").write_text(
        json.dumps({"name": name, "lifecycle": "app"}), encoding="utf-8"
    )
    monkeypatch.setattr(admission, "verified_signed_inventory", lambda n: {"server.py": "a" * 64})
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest", lambda n: AppManifest.from_dict(document)
    )
    monkeypatch.setattr(admission, "_self_registered", lambda n, r, m: n == name)
    args = ["-B", "/srv/self-reg/server.py"]
    assert admission.signed_first_payload(name) is None
    assert admission.signed_first_python_args(name, args) is args
    assert admission.signed_first_launch_env(name, args) == {}
    assert not admission.signed_first_list_path(name).exists()


def test_a_signed_app_module_named_like_a_shim_import_still_wins(tmp_path):
    """The shim imports ``json`` before the target; a signed ``backend/json.py`` at the
    top of the script's import path must still be what the app imports, as on main."""
    root = tmp_path / "app"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "json.py").write_text(
        "def load_settings():\n    return 'app settings'\n", encoding="utf-8"
    )
    (root / "backend" / "app.py").write_text(
        "from json import load_settings\nprint(load_settings())\n", encoding="utf-8"
    )
    result = _shim_run(
        tmp_path, root, ["backend/json.py", "backend/app.py"], str(root / "backend" / "app.py")
    )
    assert _out(result) == "app settings"


def test_an_app_without_a_shadowing_module_keeps_the_stdlib_json(tmp_path):
    root = tmp_path / "app"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "app.py").write_text(
        "import json\nprint(json.dumps([1]))\n", encoding="utf-8"
    )
    result = _shim_run(tmp_path, root, ["backend/app.py"], str(root / "backend" / "app.py"))
    assert _out(result) == "[1]"


def test_a_signed_module_named_like_a_dispatch_import_still_wins(tmp_path):
    """``deps_boot`` imports ``zipfile`` before the target; a signed ``zipfile.py``
    beside the script must still be what the app imports, as on main."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "zipfile.py").write_text("WHO = 'app zipfile'\n", encoding="utf-8")
    (root / "main.py").write_text("import zipfile\nprint(zipfile.WHO)\n", encoding="utf-8")
    result = _shim_run(tmp_path, root, ["zipfile.py", "main.py"], str(root / "main.py"))
    assert _out(result) == "app zipfile"


def test_an_app_without_a_zipfile_module_keeps_the_stdlib_one(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text(
        "import zipfile\nprint(zipfile.is_zipfile.__module__)\n", encoding="utf-8"
    )
    result = _shim_run(tmp_path, root, ["main.py"], str(root / "main.py"))
    assert _out(result) == "zipfile"
