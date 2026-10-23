"""App bridge tests: a staged app's own relative ``.py`` stdio launcher.

A non-gateway app stdio server that launches its OWN package through a relative
``.py`` launcher has that launcher resolved to its real absolute path under the app
root, so a staged (not pip-installed) package starts without a cwd or ``PYTHONPATH``
the host does not provide. Every other shape is left exactly as authored.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source
from test_app_bridges_deps_exposure import _isolated_python_args, _plant_deps_stamp

from kiro_crew import platform_compat
from kiro_crew.apps.bridges import _register_mcp_servers, resolve_stdio_command
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_LAUNCHER = "src/pkg/_mcp_launch.py"


@pytest.fixture(autouse=True)
def _pinned_interpreter(nonbundled_python_without_user_site):
    """``-s`` depends on the HOST interpreter's user-site state; pin it like the
    sibling stdio tests so the arg shape does not follow the runner."""


def _resolve(app_root, args):
    return resolve_stdio_command({"command": "python3", "args": list(args)}, app_root=app_root)


def _plant(app_root, rel=_LAUNCHER):
    launcher = app_root / rel
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("print('launched')\n")
    return launcher


def _register_stdio(tmp_path, app_env, monkeypatch, server_cfg, setup):
    import kiro_crew.apps.bridges as bmod

    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bmod, "_mcp_json_path", lambda: mcp_path)
    install_app(_make_app_source(tmp_path, mcpServers={"srv": server_cfg}))
    root = app_env["home"] / "apps" / "test-app"
    setup(root)
    manifest = AppManifest.from_json_file(root / APP_MANIFEST_FILENAME)
    assert _register_mcp_servers("test-app", manifest) == ["test-app:srv"]
    return json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["test-app:srv"]


# ── the rewrite ──────────────────────────────────────────────────────────────


def test_relative_launcher_resolves_to_absolute_staged_path(tmp_path):
    launcher = _plant(tmp_path)
    cfg = _resolve(tmp_path, [_LAUNCHER])
    assert _isolated_python_args(cfg) == [os.path.realpath(launcher)], cfg
    assert "disabled" not in cfg, cfg


def test_resolved_launcher_runs_from_an_unrelated_cwd(tmp_path):
    """The observable bug: the spawn cwd is the session's, not the app root."""
    app_root = tmp_path / "app"
    _plant(app_root)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    cfg = _resolve(app_root, [_LAUNCHER])
    out = subprocess.run(
        [sys.executable, *cfg["args"]], cwd=cwd, capture_output=True, timeout=60, **UTF8_TEXT
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "launched"


def test_interpreter_options_before_the_launcher_are_kept(tmp_path):
    launcher = _plant(tmp_path)
    cfg = _resolve(tmp_path, ["-u", "-X", "utf8", _LAUNCHER, "--flag"])
    assert _isolated_python_args(cfg) == [
        "-u",
        "-X",
        "utf8",
        os.path.realpath(launcher),
        "--flag",
    ], cfg


def test_launcher_after_a_double_dash_is_resolved(tmp_path):
    """``python -- x.py`` launches x.py; the normalizer drops ``--`` first."""
    launcher = _plant(tmp_path)
    cfg = _resolve(tmp_path, ["--", _LAUNCHER])
    assert _isolated_python_args(cfg) == [os.path.realpath(launcher)], cfg


def test_relative_launcher_still_starts_with_a_provisioned_deps_tree(tmp_path):
    from kiro_crew.apps.interpreter import app_deps_dir

    launcher = _plant(tmp_path)
    (tmp_path / "requirements.txt").write_bytes(b"requests\n")
    _plant_deps_stamp(tmp_path, b"requests\n")
    cfg = _resolve(tmp_path, [_LAUNCHER])
    iso = _isolated_python_args(cfg)
    assert iso[0].endswith("deps_boot.py"), cfg
    assert iso[1] == str(app_deps_dir(tmp_path)), cfg
    assert iso[2] == os.path.realpath(launcher), cfg


def test_registration_writes_the_absolute_launcher(tmp_path, app_env, monkeypatch):
    created: list[Path] = []
    entry = _register_stdio(
        tmp_path,
        app_env,
        monkeypatch,
        {"command": "python3", "args": [_LAUNCHER]},
        setup=lambda root: created.append(_plant(root)),
    )
    assert os.path.realpath(created[0]) in entry["args"], entry
    assert "disabled" not in entry, entry


# ── left as authored ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        "server.py",  # relative, no such file
        "../x.py",  # leaves the app root
        "a/../../x.py",
        "/abs/x.py",  # absolute: a deliberate choice, never touched
    ],
)
def test_targets_that_are_not_a_file_in_the_app_root_are_left_as_authored(tmp_path, target):
    app_root = tmp_path / "app"
    _plant(app_root)
    # An existing file just outside the app root, inside this test's own tmp dir.
    (tmp_path / "x.py").write_text("print('outside')\n")
    cfg = _resolve(app_root, [target])
    assert _isolated_python_args(cfg) == [target], cfg
    assert "disabled" not in cfg, cfg


def test_absolute_launcher_inside_the_app_root_is_left_as_authored(tmp_path):
    launcher = _plant(tmp_path)
    cfg = _resolve(tmp_path, [str(launcher)])
    assert _isolated_python_args(cfg) == [str(launcher)], cfg


def test_link_escaping_the_app_root_is_left_as_authored(tmp_path):
    app_root = tmp_path / "app"
    outside = tmp_path / "outside"
    app_root.mkdir()
    outside.mkdir()
    (outside / "x.py").write_text("print('outside')\n")
    platform_compat.symlink_or_junction(str(outside), str(app_root / "link"))
    cfg = _resolve(app_root, ["link/x.py"])
    assert _isolated_python_args(cfg) == ["link/x.py"], cfg


def test_non_py_and_module_launches_are_unchanged(tmp_path):
    _plant(tmp_path, "server.sh")
    assert _isolated_python_args(_resolve(tmp_path, ["server.sh"])) == ["server.sh"]
    assert _isolated_python_args(_resolve(tmp_path, ["-m", "myapp.server"])) == [
        "-m",
        "myapp.server",
    ]
    assert _isolated_python_args(_resolve(tmp_path, ["-c", "import server"])) == [
        "-c",
        "import server",
    ]


def test_args_after_the_script_operand_are_not_touched(tmp_path):
    launcher = _plant(tmp_path)
    _plant(tmp_path, "other.py")
    cfg = _resolve(tmp_path, [_LAUNCHER, "other.py"])
    assert _isolated_python_args(cfg) == [os.path.realpath(launcher), "other.py"], cfg


def test_gateway_module_server_is_unchanged(tmp_path):
    cfg = _resolve(tmp_path, ["-m", "kiro_crew.apps.builtins.x.mcp_server"])
    assert cfg["command"] == sys.executable, cfg
    assert _isolated_python_args(cfg) == ["-m", "kiro_crew.apps.builtins.x.mcp_server"], cfg
