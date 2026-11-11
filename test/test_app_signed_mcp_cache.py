"""Signed Python MCP cache selection survives isolation and authored cache options."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest
from test_app_signed_round3 import sign
from test_app_signed_round3 import signed_home as _signed_home
from test_app_signed_round3 import write_app

from kiro_crew.apps import signed_mcp
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

signed_home = _signed_home


@pytest.mark.parametrize(
    "flags",
    [
        ["-E"],
        ["-I"],
        ["-uE"],
        ["-sE"],
        ["-E", "-X", "pycache_prefix={bad}"],
        ["-Xpycache_prefix={bad}"],
        ["-EXpycache_prefix={bad}"],
    ],
)
def test_pinned_cache_executes_source_instead_of_preexisting_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    monkeypatch.setattr(sys, "pycache_prefix", None)
    root = tmp_path / "app"
    root.mkdir()
    helper = root / "cache_probe_helper.py"
    helper.write_text("VALUE = 'publisher'\n", encoding="utf-8")
    evil = tmp_path / "cache_input.py"
    evil.write_text("VALUE = 'cached'\n", encoding="utf-8")
    server = root / "server.py"
    server.write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "import cache_probe_helper\nprint(cache_probe_helper.VALUE)\n",
        encoding="utf-8",
    )
    bad = tmp_path / "authored-cache"
    has_cache_option = any("pycache_prefix" in item for item in flags)
    if has_cache_option:
        monkeypatch.setattr(sys, "pycache_prefix", str(bad))
    cache = Path(importlib.util.cache_from_source(str(helper)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(
        str(evil),
        cfile=str(cache),
        dfile=str(helper),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    fresh = tmp_path / "gateway-cache"
    fresh.mkdir()
    args = [item.format(bad=bad) for item in flags] + [str(server)]
    cfg = {"command": sys.executable, "args": args}
    env = {"PYTHONPYCACHEPREFIX": str(fresh), "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    control = subprocess.run(
        [cfg["command"], *args],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert control.returncode == 0, control.stderr
    assert control.stdout.strip() == "cached"
    pinned = signed_mcp.with_python_cache_prefix(cfg, str(fresh))
    fixed = subprocess.run(
        [pinned["command"], *pinned["args"]],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert fixed.returncode == 0, fixed.stderr
    assert fixed.stdout.strip() == "publisher"
    assert cfg["args"] == args


@pytest.mark.parametrize(
    "args, insertion",
    [
        (["-E", "script.py", "-X", "pycache_prefix=script-argument"], 1),
        (["-I", "-mserver", "-Xpycache_prefix=module-argument"], 1),
        (["-E", "-cprint('ok')", "-Xpycache_prefix=code-argument"], 1),
        (["-Emserver", "-Xpycache_prefix=module-argument"], 0),
        (["-Ecprint('ok')", "argument"], 0),
        (["-W", "ignore", "-X", "dev", "--", "-script.py"], 4),
        (["-x", "script.py"], 1),
    ],
)
def test_cache_option_preserves_target_arguments(args: list[str], insertion: int) -> None:
    cfg = {"command": sys.executable, "args": args}
    result = signed_mcp.with_python_cache_prefix(cfg, "gateway-cache")
    assert result["args"] == [
        *args[:insertion],
        "-X",
        "pycache_prefix=gateway-cache",
        *args[insertion:],
    ]


@pytest.mark.parametrize("command", ["node", "/bin/bash", "python-tool"])
def test_non_python_command_keeps_its_argument_contract(command: str) -> None:
    cfg = {"command": command, "args": ["server", "-E"]}
    assert signed_mcp.with_python_cache_prefix(cfg, "gateway-cache") is cfg


@pytest.mark.parametrize("required", [False, True])
def test_real_mcp_registration_pins_cache_only_under_signature_policy(
    signed_home: Path, monkeypatch: pytest.MonkeyPatch, required: bool
) -> None:
    from kiro_crew.apps import bridges

    root = signed_home / "apps" / "probe"
    manifest = write_app(
        root, mcpServers={"test": {"command": "python", "args": ["-E", "server.py"]}}
    )
    (root / "server.py").write_text("print('server')\n", encoding="utf-8")
    manifest = sign(root)
    if not required:
        (signed_home / "app_admission.json").unlink()
    saved: dict = {}
    monkeypatch.setattr(bridges, "_live_port_for", lambda *args: None)
    monkeypatch.setattr(bridges, "_maybe_provision_backendless_deps", lambda *args: None)
    monkeypatch.setattr(bridges, "_health_reconcile_guard", contextlib.nullcontext)
    monkeypatch.setattr(bridges, "_mcp_lock", contextlib.nullcontext)
    monkeypatch.setattr(bridges, "_read_mcp_json_unlocked", lambda **kwargs: {})
    monkeypatch.setattr(bridges, "_write_mcp_json_unlocked", saved.update)
    monkeypatch.setattr(bridges, "_schedule_unresolvable_warning", lambda *args: None)
    assert bridges._register_mcp_servers("probe", manifest) == ["probe:test"]
    cfg = saved["mcpServers"]["probe:test"]
    assert "-E" in cfg["args"]
    if required:
        prefix = cfg["env"]["PYTHONPYCACHEPREFIX"]
        assert cfg["args"][-3:-1] == ["-X", "pycache_prefix=" + prefix]
        assert Path(prefix).is_dir()
    else:
        assert all(not arg.startswith("pycache_prefix=") for arg in cfg["args"])


@pytest.mark.parametrize(
    "options, kept",
    [
        (["-E", "-X", "pycache_prefix=authored"], ["-E"]),
        (["-Xpycache_prefix=authored", "-I"], ["-I"]),
        (["-EXpycache_prefix=authored"], ["-E"]),
        (["-EX", "pycache_prefix=authored", "-X", "dev"], ["-E", "-X", "dev"]),
    ],
)
def test_authored_cache_selections_are_removed_without_changing_script_arguments(
    options: list[str], kept: list[str]
) -> None:
    target = ["server.py", "-X", "pycache_prefix=script-value"]
    cfg = {"command": sys.executable, "args": [*options, *target]}
    result = signed_mcp.with_python_cache_prefix(cfg, "gateway-cache")
    assert result["args"] == [*kept, "-X", "pycache_prefix=gateway-cache", *target]
    assert cfg["args"] == [*options, *target]
