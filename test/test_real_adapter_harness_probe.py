"""What each third-party harness does with the MCP servers Crew hands it.

One run per harness: ``real_adapters/harness_probe/drive.py`` starts the REAL
pinned harness through Crew's own ``AcpClient`` / ``AcpRuntime``, in a throwaway
``HOME`` and ``KIROCREW_HOME``, against a loopback fake model and placeholder
keys. No account, no secret, no network beyond ``127.0.0.1``. The run records
which probe servers started with which value, which DECOY servers from the
harness's own user/project config started, and the tool bytes of the largest
model request.

Four checks read that record, each against a committed per-harness expectation in
``real_adapters/harness_probe/expectations.json``:

1. a literal-env MCP server mounts and lists its tool;
2. ``${VAR}`` and ``${env:VAR}`` in a server's env reach it expanded;
3. no server from the harness's OWN user or project config starts;
4. Crew's own tool specs on a model request stay under a ceiling.

A fifth check runs the harness again with a spec that mounts nothing (a tool-less
helper) and asserts which of the harness's own config servers still start.

Each check first asserts the run matches the expectation EXACTLY, so a harness
that changes behaviour in either direction goes red here and the expectation is
re-measured. Where the expectation records a known defect, the check then marks
itself xfail with the issue that tracks it. A fix flips the measurement, the
exact match reds, and the fix PR updates the expectation, which removes the xfail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from real_adapter_gate import GOOSE_PIN, MANIFEST_DIR, require_real_adapter

ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = MANIFEST_DIR / "harness_probe"
EXPECTATIONS = json.loads((PROBE_DIR / "expectations.json").read_text(encoding="utf-8"))
NODE_MODULES = MANIFEST_DIR / "node_modules"
ISSUES = EXPECTATIONS["issues"]
#: Above this, Crew's own tool specs are sent instead of deferred (issue 3).
CREW_TOOL_BYTES_TARGET = EXPECTATIONS["crew_tool_bytes_target"]


def _entry(env: str, default: Path | None) -> str:
    value = os.environ.get(env, "")
    if value:
        return value if os.path.exists(value) else ""
    return str(default) if default is not None and default.exists() else ""


def _npm(*parts: str) -> Path:
    return NODE_MODULES.joinpath(*parts)


#: backend -> (override variable, default entry, what, install hint). The CI lane
#: sets every override to what it installed; a developer box falls back to the
#: lockfile's node_modules.
_ADAPTERS: dict[str, list[tuple[str, Path | None, str, str]]] = {
    "claude": [
        (
            "CLAUDE_AGENT_ACP_BIN",
            _npm(".bin", "claude-agent-acp"),
            "claude-agent-acp",
            "npm ci in test/real_adapters",
        )
    ],
    "codex": [
        (
            "CODEX_ACP_BIN",
            _npm("@agentclientprotocol", "codex-acp", "dist", "index.js"),
            "codex-acp",
            "npm ci in test/real_adapters",
        )
    ],
    "opencode": [
        ("OPENCODE_BIN", _npm(".bin", "opencode"), "opencode", "npm ci in test/real_adapters")
    ],
    "pi": [
        (
            "PI_ACP_BIN",
            _npm("pi-acp", "dist", "index.js"),
            "pi-acp",
            "npm ci in test/real_adapters",
        ),
        ("PI_ACP_PI_COMMAND", _npm(".bin", "pi"), "pi", "npm ci in test/real_adapters"),
    ],
    "deepseek": [("DSH_BIN", _npm(".bin", "dsh"), "dsh", "npm ci in test/real_adapters")],
    "strands": [
        ("STRANDS_BIN", _npm(".bin", "strands"), "strands", "npm ci in test/real_adapters"),
        (
            "STRANDS_PROBE_VENV",
            None,
            "the Strands project venv",
            "python3 -m venv <dir> && <dir>/bin/pip install --require-hashes "
            "-r test/real_adapters/strands-requirements.txt, then set STRANDS_PROBE_VENV=<dir>",
        ),
    ],
    "goose": [
        (
            "GOOSE_BIN",
            None,
            "goose",
            f"download goose {GOOSE_PIN['version']} ({GOOSE_PIN['url']}), check its sha256, set GOOSE_BIN",
        )
    ],
}

#: Harnesses whose default model is not one the stub can stand in for, or that
#: refuse a turn without a provider key: their session runs, their turn does not.
_NO_TURN = {"deepseek", "strands"}

_RUNS: dict[tuple[str, bool], dict] = {}


def _run(
    backend: str,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    git_s: float = 60.0,
    run_s: float = 600.0,
    toolless: bool = False,
) -> dict:
    """The one probe run for *backend*, shared by its four checks.

    *toolless* is a second run whose spec mounts nothing (``PROBE_TOOLLESS``)."""
    if (backend, toolless) in _RUNS:
        return _RUNS[(backend, toolless)]
    entries = {}
    for env, default, what, install in _ADAPTERS[backend]:
        resolved = _entry(env, default)
        require_real_adapter(resolved, what=what, install=install)
        entries[env] = resolved
    node = shutil.which("node")
    require_real_adapter(node, what="node", install="install Node 22 or newer")
    root = tmp_path_factory.mktemp(f"probe-{backend}{'-toolless' if toolless else ''}")
    home, crew, work = root / "home", root / "crew", root / "work"
    for path in (home, crew, work, root / "tmp"):
        path.mkdir()
    # A git root stops every harness's upward project-config walk inside *root*.
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", str(work)],
        check=True,
        encoding="utf-8",
        timeout=git_s,
    )
    if "STRANDS_PROBE_VENV" in entries:
        project = crew / "pi-gate" / "strands"
        project.mkdir(parents=True)
        (project / ".venv").symlink_to(entries.pop("STRANDS_PROBE_VENV"), target_is_directory=True)
    path_dirs = [
        str(NODE_MODULES / ".bin"),
        os.path.dirname(str(node)),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    if "GOOSE_BIN" in entries:
        path_dirs.insert(0, os.path.dirname(entries["GOOSE_BIN"]))
    # Built from nothing, so no variable of the runner (or of a developer's shell)
    # reaches the harness: each one reads only what this run wrote.
    env = {
        "PATH": os.pathsep.join(path_dirs),
        "HOME": str(home),
        # Windows reads the home from USERPROFILE, not HOME: without it,
        # Path.home() in the ACP client stack raises before the driver writes out.json.
        "USERPROFILE": str(home),
        "USER": "probe",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "KIROCREW_HOME": str(crew),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "TMPDIR": str(root / "tmp"),
        "PYTHONPATH": str(ROOT / "src"),
        # No __pycache__ beside the checkout's sources from this child or the
        # Python servers it starts: the run writes only under *root*.
        "PYTHONDONTWRITEBYTECODE": "1",
        "PROBE_REAL": "probe-real-7f3a",
        "PROBE_NO_TURN": "1" if backend in _NO_TURN else "0",
        "NO_BROWSER": "1",
        "DO_NOT_TRACK": "1",
        "PROBE_SANDBOX_STAND_IN": os.environ.get("PROBE_SANDBOX_STAND_IN", ""),
        "PROBE_TOOLLESS": "1" if toolless else "0",
        **entries,
    }
    completed = subprocess.run(
        [sys.executable, str(PROBE_DIR / "drive.py"), backend, str(root)],
        env=env,
        cwd=str(work),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=run_s,
    )
    out_file = root / "out.json"
    assert (
        out_file.exists()
    ), f"{backend}: no out.json\n{completed.stdout[-3000:]}\n{completed.stderr[-3000:]}"
    result = json.loads(out_file.read_text(encoding="utf-8"))
    assert result.get("ok"), f"{backend}: {result.get('error')}\n{result.get('traceback')}"
    _RUNS[(backend, toolless)] = result
    return result


def _expected(backend: str) -> dict:
    return EXPECTATIONS["backends"][backend]


def _backends(*, needs: str) -> list:
    return [name for name, spec in EXPECTATIONS["backends"].items() if needs in spec]


@pytest.mark.real_adapter
@pytest.mark.parametrize("backend", _backends(needs="literal"))
def test_a_literal_env_server_mounts_with_its_tool(backend, tmp_path_factory):
    run = _run(backend, tmp_path_factory)
    expected = _expected(backend)["literal"]
    assert run["cells"]["p-literal"] == expected, (
        f"{backend}: the literal-env probe measured {run['cells']['p-literal']!r}, "
        f"expectations.json says {expected!r}; re-measure and update it"
    )
    if expected != "Y":
        pytest.xfail(
            f"{backend} mounts none of the spec's servers here: {ISSUES['pi_spec_servers']}"
        )


@pytest.mark.real_adapter
@pytest.mark.parametrize("probe", ["p-dollar", "p-envprefix"])
@pytest.mark.parametrize("backend", _backends(needs="expansion"))
def test_env_references_reach_the_server_expanded(backend, probe, tmp_path_factory):
    run = _run(backend, tmp_path_factory)
    expected = _expected(backend)["expansion"][probe]
    assert run["cells"][probe] == expected, (
        f"{backend}: {probe} measured {run['cells'][probe]!r}, expectations.json says "
        f"{expected!r}; re-measure and update it"
    )
    if expected != "Y":
        pytest.xfail(
            f"{backend} forwards the {probe} reference unexpanded: {ISSUES['env_expansion']}"
        )


@pytest.mark.real_adapter
@pytest.mark.parametrize("backend", _backends(needs="ambient"))
def test_the_harness_own_mcp_config_stays_out_of_the_session(backend, tmp_path_factory):
    run = _run(backend, tmp_path_factory)
    expected = sorted(_expected(backend)["ambient"])
    assert run["decoys"] == expected, (
        f"{backend}: ambient decoys that started {run['decoys']}, expectations.json says "
        f"{expected}; re-measure and update it"
    )
    if expected:
        pytest.xfail(
            f"{backend} mounts its own config's servers {expected}: {ISSUES['ambient_config']}"
        )


@pytest.mark.real_adapter
@pytest.mark.parametrize("backend", _backends(needs="toolless_ambient"))
def test_a_toolless_helper_starts_none_of_the_harness_own_mcp_config(backend, tmp_path_factory):
    run = _run(backend, tmp_path_factory, toolless=True)
    expected = sorted(_expected(backend)["toolless_ambient"])
    assert run["decoys"] == expected, (
        f"{backend}: a tool-less helper started the ambient decoys {run['decoys']}, "
        f"expectations.json says {expected}; re-measure and update it"
    )
    if expected:
        pytest.xfail(
            f"{backend} helper mounts its own config's servers {expected}: "
            f"{ISSUES['ambient_config']}"
        )


@pytest.mark.real_adapter
@pytest.mark.parametrize("backend", _backends(needs="crew_tool_bytes_ceiling"))
def test_crew_tool_specs_on_a_request_stay_under_the_ceiling(backend, tmp_path_factory):
    run = _run(backend, tmp_path_factory)
    assert "request" in run, f"{backend}: the turn made no model request: {run.get('turn_error')}"
    measured = run["request"]["crew_tool_bytes"]
    ceiling = _expected(backend)["crew_tool_bytes_ceiling"]
    assert measured <= ceiling, (
        f"{backend}: Crew's tool specs are {measured} bytes on one request, over this "
        f"harness's recorded ceiling {ceiling}"
    )
    if ceiling > CREW_TOOL_BYTES_TARGET:
        assert measured > CREW_TOOL_BYTES_TARGET, (
            f"{backend}: Crew's tool specs are down to {measured} bytes; lower "
            f"crew_tool_bytes_ceiling in expectations.json to {CREW_TOOL_BYTES_TARGET}"
        )
        pytest.xfail(f"{backend} sends {measured} bytes of Crew tool specs: {ISSUES['tool_specs']}")


def test_the_codex_session_waits_for_every_mcp_server_before_its_turn(monkeypatch, tmp_path):
    """The probe's mount wait sees only the probe servers, so codex's own
    startup wait is what keeps a slow kirocrew-core's tools on the measured request.
    The driver asks for it on top of whatever meta the client already sends."""
    import importlib.util
    from types import SimpleNamespace

    monkeypatch.setattr(sys, "argv", ["drive.py", "codex", str(tmp_path)])
    monkeypatch.setattr(sys, "path", list(sys.path))  # drive.py prepends its own dir
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("probe_drive", PROBE_DIR / "drive.py")
    drive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drive)

    calls = []

    def own(method, params, agent, **kwargs):
        calls.append(method)
        return {**params, "_meta": {"own": 1}}, agent

    harness = SimpleNamespace(session_request=own)
    drive.await_codex_mcp_startup(harness)

    new, agent = harness.session_request("session/new", {"cwd": "w"}, "a", agent_capabilities={})
    assert (new, agent) == (
        {
            "cwd": "w",
            "_meta": {"own": 1, "mcpStartupAwaitTimeoutMs": drive.CODEX_MCP_STARTUP_AWAIT_MS},
        },
        "a",
    )
    # A resumed session is not a fresh mount: only session/new waits.
    load, _ = harness.session_request("session/load", {"cwd": "w"}, "a", agent_capabilities={})
    assert load == {"cwd": "w", "_meta": {"own": 1}}
    assert calls == ["session/new", "session/load"]
    # The wait must end before the runtime's own session/new budget floor does.
    from kiro_crew.acp.runtime_start import _SESSION_NEW_TIMEOUT

    assert drive.CODEX_MCP_STARTUP_AWAIT_MS / 1000 < _SESSION_NEW_TIMEOUT
