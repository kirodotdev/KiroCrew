"""Only rostered servers get a broker stub; sharing is a separate decision."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_gateway.rewriter import _rewrite_single_spec
from kiro_crew.mcp_gateway.session_servers import pooled_session_servers

STUB_MARKER = "mcp_gateway.stub"


def _rewrite(
    spec: dict,
    tmp_path: Path,
    *,
    stub: frozenset[str] = frozenset(),
    pooling_enabled: bool = False,
) -> tuple[dict, int]:
    return _rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=stub,
        pooling_enabled=pooling_enabled,
    )


def _spec() -> dict:
    return {
        "name": "kirocrew",
        "mcpServers": {
            "alpha-mcp": {"command": sys.executable, "args": ["--serve"]},
            "beta-mcp": {"command": sys.executable, "args": []},
        },
    }


def _argv(entry: dict) -> str:
    return " ".join([str(entry.get("command", ""))] + [str(a) for a in entry.get("args") or []])


def test_an_empty_roster_routes_nothing(tmp_path: Path) -> None:
    """An explicit empty roster preserves direct launches."""
    spec = _spec()
    out, wrapped = _rewrite(spec, tmp_path)

    assert wrapped == 0
    for name, entry in spec["mcpServers"].items():
        assert out["mcpServers"][name] == entry, name
        assert STUB_MARKER not in _argv(out["mcpServers"][name])


def test_fresh_install_injects_only_core_and_does_not_share(tmp_path: Path, monkeypatch) -> None:
    """The shipped roster must reach session/new, not just a settings flag."""
    import json

    crew_home = tmp_path / "crew"
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    cfg = KiroCrewConfig().mcp_gateway
    spec = _spec()
    spec["mcpServers"]["kirocrew-core"] = {
        "command": sys.executable,
        "args": ["-m", "kiro_crew", "mcp-core"],
    }
    out, wrapped = _rewrite(
        spec, tmp_path, stub=frozenset(cfg.stub_servers), pooling_enabled=cfg.enabled
    )
    assert wrapped == 1
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "kirocrew.json").write_text(json.dumps(out), encoding="utf-8")
    injected = pooled_session_servers(overlay, "kirocrew")
    assert [entry["name"] for entry in injected] == ["kirocrew-core"]
    assert "--poolable" not in injected[0]["args"]
    assert injected[0]["env"] == [{"name": "KIROCREW_HOME", "value": str(crew_home)}]
    for name in ("alpha-mcp", "beta-mcp"):
        assert out["mcpServers"][name] == spec["mcpServers"][name]


@pytest.mark.parametrize("python_override", ["none", "path", "home"])
def test_stub_launch_ignores_workspace_python_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python_override: str
) -> None:
    """Launch the emitted stub in a project that would shadow its package."""
    crew_home = tmp_path / "crew"
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    project = tmp_path / "project"
    package = project / "kiro_crew"
    package.mkdir(parents=True)
    payload = "raise SystemExit('workspace Python code executed')\n"
    (package / "__init__.py").write_text(payload, encoding="utf-8")
    (project / "sitecustomize.py").write_text(payload, encoding="utf-8")
    (project / "argparse.py").write_text(payload, encoding="utf-8")
    out, _ = _rewrite(_spec(), tmp_path, stub=frozenset({"alpha-mcp"}))
    entry = out["mcpServers"]["alpha-mcp"]
    child_env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "KIROCREW_"))}
    child_env.update(HOME=str(tmp_path / "user"), USERPROFILE=str(tmp_path / "user"))
    child_env.update(entry["env"])
    if python_override == "path":
        child_env["PYTHONPATH"] = str(project)
    elif python_override == "home":
        child_env["PYTHONHOME"] = str(project)
    result = subprocess.run(
        [entry["command"], "-B", *entry["args"], "--help"],
        cwd=project,
        env=child_env,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "proxies kiro-cli stdio to the local gateway" in result.stdout
    assert "--target-command" in result.stdout


@pytest.mark.parametrize("no_user_site", [0, 1])
def test_stub_loads_the_gateway_source_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_user_site: int
) -> None:
    """A source-only gateway must not launch another installed Crew copy."""
    import json
    from types import SimpleNamespace

    from kiro_crew.mcp_gateway import rewriter

    source_root = tmp_path / "gateway source's"
    package = source_root / "kiro_crew"
    gateway = package / "mcp_gateway"
    gateway.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (gateway / "__init__.py").write_text("", encoding="utf-8")
    stub_file = gateway / "stub.py"
    stub_file.write_text(
        "import json, sys\n"
        "print(json.dumps({'source': __file__, 'args': sys.argv[1:], "
        "'no_user_site': sys.flags.no_user_site}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rewriter, "__file__", str(gateway / "rewriter.py"))
    monkeypatch.setattr(
        rewriter,
        "sys",
        SimpleNamespace(
            executable=sys.executable, flags=SimpleNamespace(no_user_site=no_user_site)
        ),
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    out, _ = _rewrite(_spec(), tmp_path, stub=frozenset({"alpha-mcp"}))
    entry = out["mcpServers"]["alpha-mcp"]
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    child_env.update(HOME=str(tmp_path / "user"), USERPROFILE=str(tmp_path / "user"))
    child_env.update(entry["env"])
    child_env["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [entry["command"], "-B", *entry["args"], "--help"],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    assert result.stdout.startswith("{"), result.stdout
    emitted = json.loads(result.stdout)
    assert Path(emitted["source"]).resolve() == stub_file.resolve()
    assert emitted["no_user_site"] == no_user_site
    assert emitted["args"][:2] == ["--server", "alpha-mcp"]
    assert emitted["args"][-1] == "--help"


@pytest.mark.parametrize("session_key", ["", "subagent:child"])
def test_stub_resolves_identity_with_a_sanitized_harness_env(
    tmp_path: Path, monkeypatch, session_key: str
) -> None:
    """KAS starts the stub without inherited Crew env; PID lookup still works."""
    import json

    crew_home = tmp_path / "crew"
    crew_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    spec = _spec()
    # Declared server credentials belong to the backend, not the stub's ACP env.
    spec["mcpServers"]["alpha-mcp"]["env"] = {
        "API_TOKEN": "test-only-secret",
        "KIROCREW_HOME": str(tmp_path / "wrong-home"),
    }
    out, _ = _rewrite(spec, tmp_path, stub=frozenset({"alpha-mcp"}))
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "kirocrew.json").write_text(json.dumps(out), encoding="utf-8")
    injected = pooled_session_servers(overlay, "kirocrew", session_key=session_key)[0]

    # Exercise the same lookup the recaller retries after a cold start.
    key = "dashboard:member-ledger-test"
    (crew_home / f"session_pid_{os.getpid()}.txt").write_text(key, encoding="utf-8")
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("KIROCREW_")}
    child_env.update(HOME=str(tmp_path / "user"), USERPROFILE=str(tmp_path / "user"))
    child_env.update({pair["name"]: pair["value"] for pair in injected["env"]})
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from kiro_crew.mcp_gateway.stub import _build_caller_block; "
            "print(_build_caller_block(None)['session_key'])",
        ],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    assert result.stdout.strip() == (session_key or key)
    expected_env = [{"name": "KIROCREW_HOME", "value": str(crew_home)}]
    if session_key:
        expected_env.append({"name": "KIROCREW_SESSION_KEY", "value": session_key})
    assert injected["env"] == expected_env


def test_only_the_routed_server_gets_a_stub(tmp_path: Path) -> None:
    """Opting one server in must not drag its neighbours along."""
    out, wrapped = _rewrite(_spec(), tmp_path, stub=frozenset({"alpha-mcp"}))

    assert wrapped == 1
    assert STUB_MARKER in _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER not in _argv(out["mcpServers"]["beta-mcp"])


def test_routed_without_sharing_is_a_private_backend(tmp_path: Path) -> None:
    """Stub-only is the useful middle state for a stateful server: it can render
    server-authored UI without ever getting a co-tenant."""
    out, _ = _rewrite(
        _spec(), tmp_path, stub=frozenset({"alpha-mcp"}), pooling_enabled=False
    )

    argv = _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER in argv
    assert "--poolable" not in argv


def test_sharing_applies_to_every_routed_server(tmp_path: Path) -> None:
    """Sharing is global over the stub set — there is no per-server sharing
    switch left to consult, so both stubbed servers must be marked."""
    out, wrapped = _rewrite(
        _spec(),
        tmp_path,
        stub=frozenset({"alpha-mcp", "beta-mcp"}),
        pooling_enabled=True,
    )

    assert wrapped == 2
    for name in ("alpha-mcp", "beta-mcp"):
        assert "--poolable" in _argv(out["mcpServers"][name]), name


def test_sharing_alone_routes_nothing(tmp_path: Path) -> None:
    """Turning sharing on with an empty stub set must not resurrect the old
    default. Sharing decides how a stubbed backend is acquired; it never routes."""
    spec = _spec()
    out, wrapped = _rewrite(spec, tmp_path, stub=frozenset(), pooling_enabled=True)

    assert wrapped == 0
    for name, entry in spec["mcpServers"].items():
        assert out["mcpServers"][name] == entry, name


def test_a_spec_level_poolable_key_no_longer_opts_a_server_in(tmp_path: Path) -> None:
    """``poolable: true`` in an agent spec is retired as a stub trigger.

    It could not be honoured coherently. The broker's start gate and the
    session's overlay resolution both read ``mcp_gateway.stub_servers``, and
    teaching them to read agent specs instead would put filesystem IO behind
    every ``KiroCrewConfig.load()``. Wrapping the entry here while those gates
    stayed blind produced a stub nothing pointed at, plus a dashboard row that
    reported ``stub`` for a server that had none.

    So the config list is the single source of truth, and the key is stripped
    from the emitted entry exactly as before — it is ours, not kiro-cli's, and
    must never reach the overlay.
    """
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "alpha-mcp": {"command": sys.executable, "args": [], "poolable": True},
            "beta-mcp": {"command": sys.executable, "args": []},
        },
    }
    out, wrapped = _rewrite(spec, tmp_path, stub=frozenset())

    assert wrapped == 0, "a spec key must not conjure a stub the gates know nothing about"
    assert STUB_MARKER not in _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER not in _argv(out["mcpServers"]["beta-mcp"])
    # The internal hint is ours, not kiro-cli's, and must never reach the overlay.
    assert "poolable" not in out["mcpServers"]["alpha-mcp"]
    assert "poolable" not in out["mcpServers"]["beta-mcp"]


def test_the_config_list_still_opts_that_same_server_in(tmp_path: Path) -> None:
    """The replacement path: list it, and the spec key is irrelevant either way."""
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "alpha-mcp": {"command": sys.executable, "args": [], "poolable": True},
            "beta-mcp": {"command": sys.executable, "args": []},
        },
    }
    out, wrapped = _rewrite(spec, tmp_path, stub=frozenset({"alpha-mcp"}))

    assert wrapped == 1
    assert STUB_MARKER in _argv(out["mcpServers"]["alpha-mcp"])
    assert STUB_MARKER not in _argv(out["mcpServers"]["beta-mcp"])
    assert "poolable" not in out["mcpServers"]["beta-mcp"]
