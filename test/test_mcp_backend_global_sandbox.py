"""A shared MCP backend runs inside the one global sandbox, or per chat.

``backend._sandboxed_backend_argv`` wraps every backend but the ones the daemon
attests as Kiro Crew's own managed servers in the ``standard`` profile each chat's agent runs in, with the
backend's private temp dir writable, then in its cgroup scope. A host that needs
a sandbox and cannot build one, or a plain declared key the launcher would drop,
or a dynamic-loader variable refuses the share, and the daemon answers that as a missing target so the stub
runs the server per chat, vault-resolved keys included. Windows and a host
with no confinement keep the direct exec.
The wrap itself is faked here: a test worker is already inside the sandbox, so a
nested one cannot be built (the real run is proven on a host outside it).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import backend

#: Opts this module out of the conftest guard that spawns backends unwrapped.
BACKEND_SANDBOX = True


@pytest.fixture(autouse=True)
def _a_host_with_a_sandbox(_floor_monkeypatch):
    """These cases are about Linux and macOS; the Windows one sets its own platform."""
    from kiro_crew import platform_compat

    _floor_monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)


@pytest.fixture
def launcher(tmp_path) -> str:
    path = tmp_path / "launcher.py"
    path.write_text("")
    return str(path)


@pytest.fixture
def wraps(monkeypatch, launcher):
    """Every ``wrap_argv_async`` call, answered by a fake launcher file this test owns."""
    import kiro_crew.sandbox_spawn as spawn

    calls: list[dict] = []

    async def _wrap(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return ["/launcher", *argv], launcher

    monkeypatch.setattr(spawn, "wrap_argv_async", _wrap)
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: ["scope", *argv])
    return calls


def _run(
    server: str,
    env: dict | None = None,
    tmp: Path | None = Path("/run/t/abc"),
    secrets: tuple[str, ...] = (),
    managed: bool = False,
):
    return asyncio.run(
        backend._sandboxed_backend_argv(
            ["npx", "-y", "srv"],
            env or {},
            tmp,
            secrets,
            server=server,
            managed_control_plane=managed,
        )
    )


def test_a_shared_backend_gets_the_standard_profile_its_temp_and_a_cgroup(wraps, launcher):
    argv, cleanup, sandboxed = _run("weather")

    assert argv == ["scope", "/launcher", "npx", "-y", "srv"]
    assert (cleanup, sandboxed) == (launcher, True)
    assert wraps == [
        {
            "argv": ["npx", "-y", "srv"],
            "mode": "standard",
            "forward_ssh_auth_sock": False,
            "extra_writable_dirs": (str(Path("/run/t/abc")),),
        }
    ]


def test_an_attested_managed_server_keeps_the_direct_exec(wraps):
    assert _run("kirocrew-core", managed=True) == (["npx", "-y", "srv"], None, False)
    assert wraps == []


def test_a_reserved_name_without_the_attestation_is_wrapped(wraps):
    """A declaration can carry any name; only the daemon's attestation exempts it."""
    _argv, _cleanup, sandboxed = _run("kirocrew-computer")

    assert sandboxed is True


@pytest.mark.parametrize("key", ["LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"])
def test_a_loader_variable_keeps_the_server_per_chat(wraps, key, monkeypatch):
    monkeypatch.delenv(key, raising=False)

    with pytest.raises(backend.BackendSandboxRefused, match=key):
        _run("weather", {key: "/opt/lib/x.so"})
    assert wraps == []


def test_the_daemons_own_loader_path_does_not_refuse(wraps, monkeypatch):
    """The host's own value already loads into gatewayd; refusing it would refuse every share."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/local/cuda/lib64")

    _argv, _cleanup, sandboxed = _run("weather", {"LD_LIBRARY_PATH": "/usr/local/cuda/lib64"})

    assert sandboxed is True


def test_no_sandbox_on_a_host_that_needs_one_refuses_the_share(monkeypatch):
    import kiro_crew.sandbox_spawn as spawn
    from kiro_crew.sandbox import SandboxUnavailableError

    async def _unavailable(argv, **kwargs):
        raise SandboxUnavailableError("no backend")

    monkeypatch.setattr(spawn, "wrap_argv_async", _unavailable)

    with pytest.raises(backend.BackendSandboxRefused, match="no sandbox"):
        _run("weather")


def test_a_plain_declared_key_the_launcher_drops_keeps_the_server_per_chat(wraps, launcher):
    with pytest.raises(backend.BackendSandboxRefused, match="AWS_SECRET_ACCESS_KEY"):
        _run("aws-tool", {"AWS_SECRET_ACCESS_KEY": "x"})
    # The launcher built for the refused share is removed.
    assert not Path(launcher).exists()


def test_a_vault_resolved_key_the_launcher_drops_still_refuses_and_names_it(wraps, caplog):
    """No third-party backend starts unwrapped where a sandbox applies, vault or not."""
    env = {"AWS_SECRET_ACCESS_KEY": "resolved-value", "AWS_SESSION_TOKEN": "resolved-value"}
    secrets = ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")

    with caplog.at_level("WARNING", logger=backend.logger.name):
        with pytest.raises(backend.BackendSandboxRefused):
            _run("aws-tool", env, secrets=secrets)

    assert "AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN" in caplog.text
    assert "resolved-value" not in caplog.text


def test_a_declared_python_path_reaches_the_server(wraps):
    """The Python environment is kept, as it is unwrapped, so a ``-m`` server still imports."""
    _argv, _cleanup, sandboxed = _run("py-tool", {"PYTHONPATH": "/opt/srv"})

    assert sandboxed is True
    assert "strip_python_env" not in wraps[0]


def test_windows_keeps_the_direct_exec(wraps, monkeypatch):
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    env = {"AWS_SECRET_ACCESS_KEY": "x"}

    assert _run("weather", env) == (["npx", "-y", "srv"], None, False)
    assert wraps == []


def test_a_named_ssh_agent_socket_is_forwarded_not_refused(wraps):
    _argv, _cleanup, sandboxed = _run("git-tool", {"SSH_AUTH_SOCK": "/tmp/agent.sock"})

    assert sandboxed is True
    assert wraps[0]["forward_ssh_auth_sock"] is True


def test_a_host_with_no_confinement_keeps_the_server_per_chat(monkeypatch):
    """A no-op wrap would run the shared backend wider than a per-chat copy."""
    import kiro_crew.sandbox_spawn as spawn

    async def _no_op(argv, **kwargs):
        return list(argv), None

    monkeypatch.setattr(spawn, "wrap_argv_async", _no_op)

    with pytest.raises(backend.BackendSandboxRefused, match="runs per chat"):
        _run("weather", {"AWS_SECRET_ACCESS_KEY": "x"})


@pytest.mark.parametrize("macos", [True, False], ids=["macos", "linux"])
@pytest.mark.parametrize(
    "argv",
    [["npx", "-y", "@playwright/mcp@latest"], ["npx", "-y", "chrome-devtools-mcp"]],
)
def test_a_browser_server_is_wrapped_like_any_other(wraps, monkeypatch, argv, macos):
    """No launch text lets a shared backend out of the sandbox, on either host."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_MACOS", macos)

    _argv, _cleanup, sandboxed = asyncio.run(
        backend._sandboxed_backend_argv(argv, {}, Path("/run/t/abc"), server="pw")
    )
    assert sandboxed is True
    assert wraps[0]["argv"][: len(argv)] == argv


@pytest.mark.parametrize(
    "argv",
    [
        ["npx", "-y", "@playwright/mcp@latest", "--headless"],
        ["npx", "@playwright/mcp"],
        ["node", "/home/u/.npm/_npx/abc/node_modules/@playwright/mcp/cli.js"],
        ["playwright-mcp", "--isolated"],
    ],
)
def test_a_wrapped_playwright_server_turns_only_chromes_own_sandbox_off(wraps, argv):
    """Chrome's sandbox cannot start inside ours; ours still wraps the whole tree."""
    _argv, _cleanup, sandboxed = asyncio.run(
        backend._sandboxed_backend_argv(argv, {}, Path("/run/t/abc"), server="pw")
    )

    assert sandboxed is True
    assert wraps[0]["argv"] == [*argv, "--no-sandbox"]


@pytest.mark.parametrize(
    "argv",
    [
        ["npx", "-y", "@playwright/mcp@latest", "--sandbox"],
        ["npx", "-y", "@playwright/mcp@latest", "--no-sandbox"],
        ["npx", "-y", "@modelcontextprotocol/server-puppeteer"],
        ["npx", "-y", "playwright-mcp-fork"],
    ],
)
def test_an_explicit_choice_or_another_server_is_left_as_declared(wraps, argv):
    asyncio.run(backend._sandboxed_backend_argv(argv, {}, Path("/run/t/abc"), server="x"))

    assert wraps[0]["argv"] == argv


def test_with_no_confinement_a_playwright_server_is_not_shared(monkeypatch):
    """No sandbox for the shared backend: it runs per chat, Chrome's sandbox untouched."""
    import kiro_crew.sandbox_spawn as spawn

    async def _no_op(argv, **_kw):
        return list(argv), None

    monkeypatch.setattr(spawn, "wrap_argv_async", _no_op)
    argv = ["npx", "-y", "@playwright/mcp@latest"]

    with pytest.raises(backend.BackendSandboxRefused):
        asyncio.run(backend._sandboxed_backend_argv(argv, {}, None))


class _Spawned(Exception):
    """Raised by the fake spawn seam once it has seen the env."""


@pytest.mark.asyncio
@pytest.mark.parametrize("sandboxed", [True, False])
async def test_a_sandboxed_backend_does_not_inherit_the_gateways_channel_credentials(
    monkeypatch, tmp_path, sandboxed
):
    """The agent's own spawn drops these keys; a shared backend in its sandbox does too."""
    from kiro_crew.mcp_gateway import backend as backend_mod
    from kiro_crew.mcp_gateway.pool import PoolKey

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-daemon")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-daemon")
    seen: dict = {}

    async def _argv(argv, env, *_a, **_k):
        return argv, None, sandboxed

    async def _spawn(argv, *, env, **_k):
        seen.update(env)
        raise _Spawned()

    monkeypatch.setattr(backend_mod, "_sandboxed_backend_argv", _argv)
    monkeypatch.setattr(backend_mod, "_spawn_backend_process", _spawn)
    monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
    # The server declares the same Telegram token the daemon runs with: equal
    # values, yet the operator chose it for this server.
    env = {
        "SLACK_BOT_TOKEN": "xoxb-daemon",
        "TELEGRAM_BOT_TOKEN": "tg-daemon",
        "PATH": os.environ.get("PATH", ""),
    }
    key = PoolKey(
        server_name="srv",
        command_args_hash="cah",
        effective_env_hash="eeh",
        work_dir=str(tmp_path),
        binary_version="1.0",
        os_uid=1000,
    )

    with pytest.raises(_Spawned):
        await backend_mod.spawn_backend(
            key, "cmd", [], env, str(tmp_path), declared_env_keys=("TELEGRAM_BOT_TOKEN",)
        )

    # The inherited value is dropped; one the server declared itself is kept.
    assert ("SLACK_BOT_TOKEN" in seen) is not sandboxed
    assert seen["TELEGRAM_BOT_TOKEN"] == "tg-daemon"


def test_a_differently_cased_declared_key_does_not_keep_the_inherited_token():
    """Off Windows ``slack_bot_token`` is another variable, so the real token still goes."""
    from kiro_crew.mcp_gateway import backend as backend_mod

    env = {"SLACK_BOT_TOKEN": "xoxb-daemon", "slack_bot_token": "x"}

    kept = backend_mod._without_inherited_channel_credentials(env, ("slack_bot_token",))

    assert "SLACK_BOT_TOKEN" not in kept
    assert kept["slack_bot_token"] == "x"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "declared", "sandboxed", "expected"),
    [
        (["-y", "@modelcontextprotocol/server-puppeteer"], {}, True, True),
        (["-y", "@modelcontextprotocol/server-puppeteer@2025.5.12"], {}, True, True),
        (["-y", "@modelcontextprotocol/server-puppeteer"], {}, False, False),
        (
            ["-y", "@modelcontextprotocol/server-puppeteer"],
            {"ALLOW_DANGEROUS": "false"},
            True,
            False,
        ),
        (
            ["-y", "@modelcontextprotocol/server-puppeteer"],
            {"PUPPETEER_LAUNCH_OPTIONS": '{"headless": true}'},
            True,
            False,
        ),
        (["-y", "@playwright/mcp"], {}, True, False),
    ],
)
async def test_a_wrapped_puppeteer_server_turns_only_chromes_own_sandbox_off(
    monkeypatch, tmp_path, args, declared, sandboxed, expected
):
    """Inside our sandbox only, and never over launch options the operator set."""
    from kiro_crew.mcp_gateway import backend as backend_mod
    from kiro_crew.mcp_gateway.pool import PoolKey

    monkeypatch.delenv("PUPPETEER_LAUNCH_OPTIONS", raising=False)
    monkeypatch.delenv("ALLOW_DANGEROUS", raising=False)
    seen: dict = {}

    async def _argv(argv, env, *_a, **_k):
        return argv, None, sandboxed

    async def _spawn(argv, *, env, **_k):
        seen.update(env)
        raise _Spawned()

    monkeypatch.setattr(backend_mod, "_sandboxed_backend_argv", _argv)
    monkeypatch.setattr(backend_mod, "_spawn_backend_process", _spawn)
    monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
    key = PoolKey(
        server_name="puppeteer",
        command_args_hash="cah",
        effective_env_hash="eeh",
        work_dir=str(tmp_path),
        binary_version="1.0",
        os_uid=1000,
    )
    env = {"PATH": os.environ.get("PATH", ""), **declared}

    with pytest.raises(_Spawned):
        await backend_mod.spawn_backend(
            key, "npx", args, env, str(tmp_path), declared_env_keys=tuple(declared)
        )

    if expected:
        assert seen["PUPPETEER_LAUNCH_OPTIONS"] == '{"args": ["--no-sandbox"]}'
        assert seen["ALLOW_DANGEROUS"] == "true"
    else:
        for key_name, value in declared.items():
            assert seen[key_name] == value
        assert ("ALLOW_DANGEROUS" in seen) is ("ALLOW_DANGEROUS" in declared)
        assert ("PUPPETEER_LAUNCH_OPTIONS" in seen) is ("PUPPETEER_LAUNCH_OPTIONS" in declared)


def test_gatewayd_drops_an_inherited_in_sandbox_marker(tmp_path):
    """With the marker inherited, every backend wrap would be a no-op passthrough."""
    import subprocess
    import sys

    code = (
        "import os, asyncio\n"
        "from kiro_crew.mcp_gateway import gatewayd\n"
        "gatewayd.ensure_data_home = lambda: None\n"
        "gatewayd._prepare_telemetry = lambda: None\n"
        "async def _amain():\n"
        "    print(os.environ.get('KIROCREW_SANDBOX_ACTIVE'), os.environ.get('KIROCREW_SANDBOX_LEVEL'))\n"
        "    return 0\n"
        "gatewayd._amain = _amain\n"
        "gatewayd.main()\n"
    )
    env = {
        **os.environ,
        "KIROCREW_HOME": str(tmp_path / "crew"),
        "KIROCREW_SANDBOX_ACTIVE": "1",
        "KIROCREW_SANDBOX_LEVEL": "standard",
    }
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,  # flake-ok: bounds a child that only imports, not a wait
    )

    assert out.stdout.split()[:2] == ["None", "None"], out.stderr[-800:]
