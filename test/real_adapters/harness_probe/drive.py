"""Drive ONE harness through Crew's own ACP code and record what reached it.

Usage: ``drive.py <backend> <root>``, run by ``test_real_adapter_harness_probe.py``
in a fresh environment whose ``HOME``, ``KIROCREW_HOME``, ``XDG_*`` and ``TMPDIR``
all sit under *root*. Nothing outside *root* is written.

What it does, in order:

1. writes a Crew agent spec (and ``~/.kiro/settings/mcp.json``) carrying the probe
   servers below, the way Crew materializes a user's MCP config;
2. writes one DECOY server into the harness's OWN user and project config. A decoy
   that starts is ambient harness config reaching a Crew session;
3. points the harness at the loopback stub model with placeholder keys;
4. starts a session through ``AcpClient`` (or ``AcpRuntime`` for the runtime
   backends), waits for the probe servers, and runs one "Say hi." turn unless
   ``PROBE_NO_TURN=1``;
5. writes ``<root>/out.json``: per probe server what value it saw, which decoys
   started, and the tool bytes of the largest model request.

Probe servers in the spec:

=============  ================================  =================================
name           env                               lists its tool when
=============  ================================  =================================
p-literal      ``PROBE_TOKEN=<placeholder>``     always (the control)
p-dollar       ``PROBE_TOKEN=${PROBE_REAL}``     someone expands ``${VAR}``
p-envprefix    ``PROBE_TOKEN=${env:PROBE_REAL}`` someone expands ``${env:VAR}``
=============  ================================  =================================
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stub_model  # noqa: E402

BACKEND = sys.argv[1]
ROOT = Path(sys.argv[2])
HOME = Path(os.environ["HOME"])
WORK = ROOT / "work"
LOGS = ROOT / "logs"
PROBE = HERE / "probe_mcp.py"
PY = sys.executable
PLACEHOLDER = "probe-real-7f3a"
SPEC_PROBES = ("p-literal", "p-dollar", "p-envprefix")
#: Every gateway session has one; KAS relays withheld env only when it is set.
SESSION_KEY = "dashboard:probe-1"
#: How long to wait for the spec's probe servers to start. A harness that mounts
#: none of them (pi without a gateway) spends all of it.
MOUNT_WAIT_S = float(os.environ.get("PROBE_MOUNT_WAIT_S", "20"))


def _server(name: str, env: dict) -> dict:
    return {"command": PY, "args": [str(PROBE), name, str(LOGS)], "env": env}


def write_spec() -> None:
    servers = {
        "p-literal": _server("p-literal", {"PROBE_TOKEN": PLACEHOLDER}),
        "p-dollar": _server("p-dollar", {"PROBE_TOKEN": "${PROBE_REAL}"}),
        "p-envprefix": _server("p-envprefix", {"PROBE_TOKEN": "${env:PROBE_REAL}"}),
    }
    spec = {
        "name": "kirocrew",
        "description": "harness probe",
        "prompt": "probe",
        "mcpServers": servers,
        "tools": ["*"],
        "allowedTools": [],
    }
    agents = HOME / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "kirocrew.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    settings = HOME / ".kiro" / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def seed_decoys() -> None:
    """One decoy MCP server in each harness's own user and project config."""

    def decoy(name: str) -> dict:
        return _server(name, {"PROBE_TOKEN": PLACEHOLDER})

    def mcp_servers(name: str, **extra) -> str:
        return json.dumps({"mcpServers": {name: {**extra, **decoy(name)}}})

    # claude: ~/.claude.json (user), .mcp.json (project, enabled by user settings)
    _write(HOME / ".claude.json", mcp_servers("decoy-claude-user", type="stdio"))
    _write(HOME / ".claude" / "settings.json", json.dumps({"enableAllProjectMcpServers": True}))
    _write(WORK / ".mcp.json", mcp_servers("decoy-claude-project", type="stdio"))
    # codex: ~/.codex/config.toml (user), .codex/config.toml (project)
    for path, name in (
        (HOME / ".codex" / "config.toml", "decoy-codex-user"),
        (WORK / ".codex" / "config.toml", "decoy-codex-project"),
    ):
        table = name.replace("-", "_")
        _write(
            path,
            f'[mcp_servers.{table}]\ncommand = "{PY}"\nargs = ["{PROBE}", "{name}", "{LOGS}"]\n'
            f'[mcp_servers.{table}.env]\nPROBE_TOKEN = "{PLACEHOLDER}"\n',
        )
    # opencode: ~/.config/opencode/opencode.json (user), opencode.json (project)
    for path, name in (
        (HOME / ".config" / "opencode" / "opencode.json", "decoy-opencode-user"),
        (WORK / "opencode.json", "decoy-opencode-project"),
    ):
        entry = {
            "type": "local",
            "command": [PY, str(PROBE), name, str(LOGS)],
            "enabled": True,
            "environment": {"PROBE_TOKEN": PLACEHOLDER},
        }
        _write(path, json.dumps({"mcp": {name: entry}}))
    # goose: ~/.config/goose/config.yaml extensions (user)
    name = "decoy-goose-user"
    _write(
        HOME / ".config" / "goose" / "config.yaml",
        f"extensions:\n  {name}:\n    enabled: true\n    type: stdio\n    name: {name}\n"
        f'    cmd: {PY}\n    args: ["{PROBE}", "{name}", "{LOGS}"]\n'
        f"    envs:\n      PROBE_TOKEN: {PLACEHOLDER}\n    timeout: 60\n",
    )
    # kiro / kas: the project's .kiro/settings/mcp.json
    _write(WORK / ".kiro" / "settings" / "mcp.json", mcp_servers("decoy-kiro-project"))
    # pi: ~/.pi/agent/mcp.json (the pi MCP adapter's user file)
    _write(HOME / ".pi" / "agent" / "mcp.json", mcp_servers("decoy-pi-user"))
    # deepseek: ~/.dsh/mcp.json
    _write(HOME / ".dsh" / "mcp.json", mcp_servers("decoy-dsh-user"))
    # A placeholder (fabricated, never valid) codex credential: without one,
    # session/new refuses with "Authentication required".
    _write(
        HOME / ".codex" / "auth.json",
        json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}),
    )


def seed_providers(port: int) -> None:
    """Point each harness's model at the loopback stub (placeholder keys only)."""
    base = f"http://127.0.0.1:{port}"
    key = "stub-key-not-a-credential"
    # codex: the provider must come before the decoy's tables
    cfg = HOME / ".codex" / "config.toml"
    cfg.write_text(
        'model = "gpt-5-codex"\nmodel_provider = "stub"\n'
        f'[model_providers.stub]\nname = "stub"\nbase_url = "{base}/v1"\nwire_api = "responses"\n'
        'env_key = "PROBE_STUB_MODEL_KEY"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
        + cfg.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # opencode: a provider merged into the user config that also carries the decoy
    oc = HOME / ".config" / "opencode" / "opencode.json"
    doc = json.loads(oc.read_text(encoding="utf-8"))
    doc["provider"] = {
        "stub": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "stub",
            "options": {"baseURL": f"{base}/v1", "apiKey": key},
            "models": {"stub-model": {"name": "stub-model"}},
        }
    }
    doc["model"] = "stub/stub-model"
    oc.write_text(json.dumps(doc), encoding="utf-8")
    # goose
    gc = HOME / ".config" / "goose" / "config.yaml"
    gc.write_text(
        f"GOOSE_PROVIDER: openai\nGOOSE_MODEL: gpt-4o\nOPENAI_HOST: {base}\n"
        "OPENAI_BASE_PATH: v1/chat/completions\n" + gc.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (gc.parent / "secrets.yaml").write_text(f"OPENAI_API_KEY: {key}\n", encoding="utf-8")
    # pi
    pa = HOME / ".pi" / "agent"
    model = {"id": "stub-model", "name": "Stub", "contextWindow": 128000, "maxTokens": 4096}
    provider = {
        "baseUrl": f"{base}/v1",
        "api": "openai-completions",
        "apiKey": key,
        "models": [model],
    }
    _write(pa / "models.json", json.dumps({"providers": {"stub": provider}}))
    settings = {"defaultProvider": "stub", "defaultModel": "stub-model", "quietStartup": True}
    _write(pa / "settings.json", json.dumps(settings))
    # This driver's own environment, which every harness it spawns inherits: codex's
    # provider key variable, claude's endpoint and key, goose's keyring switch.
    spawn_env = {
        "PROBE_STUB_MODEL_KEY": key,
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_API_KEY": "sk-ant-not-a-real-key-000000",
        "GOOSE_DISABLE_KEYRING": "true",
    }
    os.environ.update(spawn_env)  # flake-ok: a driver process of its own, not a test


def probe_state() -> dict:
    """Per probe log: how often it started and the value its last start saw."""
    state = {}
    for path in sorted(LOGS.glob("*.jsonl")):
        starts, tokens, methods = 0, [], []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event["ev"] == "start":
                starts += 1
                tokens.append(event.get("token_seen"))
            else:
                methods.append(event.get("method"))
        state[path.stem] = {
            "starts": starts,
            "token": tokens[-1] if tokens else None,
            "methods": methods,
        }
    return state


def cell(state: dict, name: str) -> str:
    """``Y`` started with the real value, ``L`` with the literal reference, ``0``
    without the variable, ``-`` never started."""
    entry = state.get(name)
    if not entry or not entry["starts"]:
        return "-"
    if entry["token"] == PLACEHOLDER:
        return "Y"
    if entry["token"] and "${" in entry["token"]:
        return "L"
    return "0"


async def wait_for_mounts(bound: float = MOUNT_WAIT_S) -> None:
    """Until every spec probe has started, or MOUNT_WAIT_S. The signal is the start
    line each probe writes; a harness that mounts none of them (pi without a
    gateway) is the bounded case, and the checks read what did start."""
    from kiro_crew.testing.wait import async_wait_until

    def all_started() -> bool:
        state = probe_state()
        return all(state.get(name, {}).get("starts") for name in SPEC_PROBES)

    try:
        await async_wait_until(all_started, timeout=bound, off_loop=True)
    except AssertionError:
        pass


async def drive_client(turn_s: float = 120.0) -> dict:
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=WORK, acp_backend=BACKEND, session_key=SESSION_KEY)
    rec: dict = {}
    try:
        await asyncio.wait_for(client.ensure_ready(), timeout=240)
        await wait_for_mounts()
        if os.environ.get("PROBE_NO_TURN") != "1":
            try:
                await asyncio.wait_for(client.send_message("Say hi.", timeout=turn_s), timeout=150)
            except BaseException as exc:  # noqa: BLE001 - recorded, the checks read it
                rec["turn_error"] = f"{type(exc).__name__}: {exc}"[:600]
    finally:
        try:
            await asyncio.wait_for(client.shutdown(), timeout=60)
        except BaseException as exc:  # noqa: BLE001
            rec["shutdown_error"] = repr(exc)[:300]
    return rec


async def drive_runtime(turn_s: float = 120.0) -> dict:
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime(work_dir=WORK, acp_backend=BACKEND)
    rec: dict = {}
    try:
        await asyncio.wait_for(rt.spawn(), timeout=180)
        handle = await asyncio.wait_for(
            rt.create_session(cwd=WORK, session_key=SESSION_KEY),
            timeout=180,
        )
        await wait_for_mounts()
        if os.environ.get("PROBE_NO_TURN") != "1":

            async def turn(bound: float):
                async for _event in handle.prompt("Say hi.", timeout=bound):
                    pass

            try:
                await asyncio.wait_for(turn(turn_s), timeout=150)
            except BaseException as exc:  # noqa: BLE001 - recorded, the checks read it
                rec["turn_error"] = f"{type(exc).__name__}: {exc}"[:600]
    finally:
        try:
            await asyncio.wait_for(rt.kill(expected=True, reason="probe done"), timeout=60)
        except BaseException as exc:  # noqa: BLE001
            rec["shutdown_error"] = repr(exc)[:300]
    return rec


def stand_in_for_the_sandbox() -> bool:
    """Replace Crew's OS sandbox with identity, where this host cannot build one.

    The CI runner has no user namespaces, so every spawn would be refused there
    (and an enforced harness refuses to start without its credential mask). The
    facts measured here are the harness's, not the sandbox's, so the sandbox is the
    one layer stood in for, exactly as ``test_pi_real_adapter.py`` does in-process:
    the argv wraps return the argv unchanged, the preflight hides nothing, and the
    mask is reported as applied. A host WITH a sandbox runs the real one, unless
    ``PROBE_SANDBOX_STAND_IN=1`` asks for the stand-in to check both measure alike.
    The sandbox's own suites cover the mask. This runs in the driver's own process,
    never in the test process.
    """
    import kiro_crew.acp.client  # noqa: F401 - binds the names patched below
    import kiro_crew.acp.runtime  # noqa: F401
    import kiro_crew.sandbox as sandbox

    if os.environ.get("PROBE_SANDBOX_STAND_IN") != "1" and sandbox.userns_available():
        return False

    real = {sandbox.wrap_argv: "sync", sandbox.wrap_argv_async: "async"}

    def unwrapped(argv, *_args, **_kwargs):
        return list(argv), None

    async def unwrapped_async(argv, *_args, **_kwargs):
        return list(argv), None

    async def no_preflight(*_args, **_kwargs):
        return ()

    for name, module in list(sys.modules.items()):
        if not name.startswith("kiro_crew") or module is None:
            continue
        for attr in ("wrap_argv", "wrap_argv_async"):
            kind = real.get(getattr(module, attr, None))
            if kind:
                setattr(module, attr, unwrapped if kind == "sync" else unwrapped_async)
        if hasattr(module, "_run_preflight_bounded"):
            module._run_preflight_bounded = no_preflight
    sandbox.credential_mask_applies = lambda *_args, **_kwargs: True
    return True


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    write_spec()
    seed_decoys()
    seed_providers(stub_model.start())
    from kiro_crew.agent_sdk.backends_sessions import ACP_BACKENDS_ACP_RUNTIME

    out: dict = {"backend": BACKEND, "sandbox_stand_in": stand_in_for_the_sandbox()}
    try:
        drive = drive_runtime if BACKEND in ACP_BACKENDS_ACP_RUNTIME else drive_client
        out.update(asyncio.run(drive()))
        out["ok"] = True
    except BaseException as exc:  # noqa: BLE001 - the test reports it
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        out["traceback"] = traceback.format_exc()[-3000:]
    state = probe_state()
    out["cells"] = {name: cell(state, name) for name in SPEC_PROBES}
    out["decoys"] = sorted(
        name.removeprefix("decoy-")
        for name, entry in state.items()
        if name.startswith("decoy-") and entry["starts"]
    )
    posts = [request for request in stub_model.LOG if request["tools"] or request["bytes"]]
    largest = max(posts, key=lambda request: request["bytes"], default=None)
    if largest is not None:
        tools = largest["tools"]
        out["request"] = {
            "bytes": largest["bytes"],
            "tool_bytes": sum(tools.values()),
            "tools": len(tools),
            "crew_tool_bytes": sum(size for name, size in tools.items() if "kirocrew" in name),
            "crew_tools": sorted(name for name in tools if "kirocrew" in name),
        }
    (ROOT / "out.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
