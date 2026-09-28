"""Regression tests: two agent-influenced spawns must not hand their child the
gateway's agent-denied environment.

Both halves drive the REAL production seam, not a rebuilt copy of its literals:

* ``apps/routes.py::handle_open_app`` is driven through its own HTTP route, so
  the environment under test is the one that endpoint actually builds.
* ``knowledge/llm_pool.py::CCWorker._spawn`` is driven directly, with only
  ``_claude_bin`` redirected at a stub, so every environment decision stays in
  production code.

``KIROCREW_OWNER_ID`` is the probe key at both sites: it is a member of
``sandbox._AGENT_DENIED_ENV_KEYS`` that carries no credential material of its
own, so the planted value can stay a neutral literal. The other members of that
list -- the chat-platform bot-token class, the tracker/forge token class and the
central-governance fetch header -- are live bearer credentials and are named by
class only, never set here.

Each negative assertion has a positive control beside it, because the cheap
wrong way to pass a test like this is to hand the child an environment so empty
it cannot work: a desktop launcher needs the display variables, and the
knowledge worker needs its CLI on ``PATH``, its ``HOME`` config and the provider
key the product deliberately passes to agent children.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_app_execution import _install_test_app, _route_app
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew import platform_compat
from kiro_crew.knowledge.llm_pool import CCWorker

MARKER = "S29_MARKER"
PROBE_KEY = "KIROCREW_OWNER_ID"

#: A provider key the product deliberately passes to an agent child: it is in
#: neither ``_SENSITIVE_ENV_PREFIXES`` nor ``_AGENT_DENIED_ENV_KEYS``, so the
#: agent enforcement point keeps it and the external CLI can still authenticate.
PROVIDER_KEY = "ANTHROPIC_API_KEY"

#: What a desktop launcher needs to reach the running session. The allowlist the
#: install/build commands from the same manifest get holds none of these, so the
#: open-command site copies them through on top of it.
DISPLAY_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
)

#: The operator's SSH agent socket. The allowlist carries it because the install
#: and build commands run git against the owner's own repositories, but a
#: launcher never needs it and an app-authored shell holding it authenticates as
#: the operator. The value is a neutral path literal, never a real socket.
AGENT_SOCKET_KEY = "SSH_AUTH_SOCK"

#: The gateway's OWN model credential, which no scrub list names: it is not a
#: channel token, so a subtract-the-known-bad environment leaves it in place and
#: an app-authored shell can read it. Values here are neutral literals -- the
#: real credentials are never set by this file.
MODEL_CREDENTIAL_KEYS = ("AWS_ACCESS_KEY_ID", "ANTHROPIC_API_KEY", "KIRO_API_KEY")


# ---------------------------------------------------------------------------
# F48 -- apps/routes.py::handle_open_app
# ---------------------------------------------------------------------------


async def _open_app_child_env(tmp_path, monkeypatch) -> dict[str, str]:
    """POST the open route for an admitted app; return the child's env dict.

    Everything the route decides is production code. The spawn primitive is the
    one seam replaced, and only to read back the ``env`` the route handed it --
    the observable under test.
    """
    import kiro_crew.apps.routes as routes
    from kiro_crew.apps import execution

    _install_test_app(
        tmp_path,
        monkeypatch,
        enabled=True,
        manifest_extra={"openCommand": "true"},
    )
    for key in DISPLAY_KEYS:
        monkeypatch.setenv(key, f"{key.lower()}-value")
    for key in MODEL_CREDENTIAL_KEYS:
        monkeypatch.setenv(key, MARKER)
    monkeypatch.setenv(AGENT_SOCKET_KEY, MARKER)
    monkeypatch.setenv(PROBE_KEY, MARKER)
    monkeypatch.setattr(execution, "third_party_execution_allowed", lambda: True)
    monkeypatch.setattr(routes, "wrap_argv", lambda argv, **kwargs: (argv, None))
    monkeypatch.setattr(routes, "cgroup_scope_argv", lambda argv: argv)

    captured: dict[str, dict[str, str]] = {}

    async def _spawn(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(pid=_UNALLOCATABLE_PID)

    monkeypatch.setattr(routes, "create_subprocess_limited", _spawn)
    async with TestClient(TestServer(_route_app())) as client:
        response = await client.post("/api/apps/execution-test-app/open")
        assert response.status == 200, await response.text()
    assert "env" in captured, "the route never reached the spawn"
    env = captured["env"]
    assert env is not None, (
        "handle_open_app spawned the manifest's openCommand with no env=, so the "
        "child inherits this process's environment whole"
    )
    return dict(env)


@pytest.mark.asyncio
async def test_open_command_child_does_not_receive_an_agent_denied_key(
    tmp_path, monkeypatch
) -> None:
    """The fix: the manifest-authored shell cannot read an agent-denied key."""
    env = await _open_app_child_env(tmp_path, monkeypatch)

    assert PROBE_KEY not in env, (
        f"{PROBE_KEY} (a sandbox._AGENT_DENIED_ENV_KEYS member) reached the "
        f"app-authored openCommand shell; the standard tier this spawn wraps at "
        f"scrubs no denied key, so the parent-side scrub is the only thing that "
        f"removes it"
    )
    assert (
        MARKER not in env.values()
    ), f"a value planted on {PROBE_KEY} reached the child under another name"


@pytest.mark.asyncio
async def test_open_command_child_does_not_receive_the_model_credential(
    tmp_path, monkeypatch
) -> None:
    """The gateway's own model credential must not reach app-authored content.

    These names sit in no scrub list -- they are not channel tokens -- so only an
    allowlisted environment withholds them. A denied-key scrub hands every one of
    them to the app's own shell.
    """
    env = await _open_app_child_env(tmp_path, monkeypatch)

    assert AGENT_SOCKET_KEY not in env, (
        f"{AGENT_SOCKET_KEY} reached the openCommand shell; the allowlist carries "
        f"it for the git-running install and build commands, so the launch site "
        f"has to take it back out -- with it an app authenticates as the operator"
    )
    for key in MODEL_CREDENTIAL_KEYS:
        assert key not in env, (
            f"{key} reached the openCommand shell; an installed app is untrusted "
            f"content and this is the credential the gateway calls its own model with"
        )


@pytest.mark.asyncio
async def test_open_command_child_still_reaches_the_desktop_session(tmp_path, monkeypatch) -> None:
    """Positive control: the launcher keeps what a desktop launch needs.

    This is the over-strict direction. The route only reaches its spawn on a
    host that HAS a display, so a child without these variables is a launcher
    with no session to draw into -- and ``PATH``/``HOME`` are what let the
    command resolve a binary and find its own config at all.
    """
    env = await _open_app_child_env(tmp_path, monkeypatch)

    for key in DISPLAY_KEYS:
        assert env.get(key) == f"{key.lower()}-value", (
            f"{key} must survive: without it an openCommand app cannot reach the "
            f"running desktop session"
        )
    assert env.get("PATH") == os.environ.get("PATH")
    assert env.get("HOME") == os.environ.get("HOME")


@pytest.mark.asyncio
async def test_open_command_scrub_does_not_mutate_the_gateway_env(tmp_path, monkeypatch) -> None:
    """The scrub builds a copy; the gateway keeps its own variables."""
    await _open_app_child_env(tmp_path, monkeypatch)

    assert os.environ[PROBE_KEY] == MARKER


# ---------------------------------------------------------------------------
# F53 -- knowledge/llm_pool.py::CCWorker._spawn
# ---------------------------------------------------------------------------


def _write_env_recording_stub(tmp_path):
    """A stub 'agent CLI' that records its own environment and exits.

    POSIX-only by construction: the shebang is what makes this file directly
    executable, and Windows has none. Its three callers are skipped there, and
    the tier test covers that platform through a fake spawn instead.
    """
    dump = tmp_path / "worker_env.txt"
    stub = tmp_path / "stub_cli"
    stub.write_text(f'#!/bin/sh\nenv > "{dump}"\nexit 0\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, dump


def _dumped_env(dump) -> dict[str, str]:
    if not dump.exists():
        return {}
    return dict(
        line.split("=", 1)
        for line in dump.read_text(encoding="utf-8", errors="replace").splitlines()
        if "=" in line
    )


async def _run_worker_spawn(stub, monkeypatch) -> None:
    """Drive the production ``CCWorker._spawn`` against *stub*.

    The OS-level launcher is replaced with identity, as the sibling spawn
    tests in ``test/test_llm_pool.py`` do. Without that these tests require a
    user-namespace backend on the host: ``wrap_argv`` refuses when none is
    available and ``allow_unsandboxed_exec`` is unset, which is every CI
    container, and the refusal decides the outcome before the assertion runs.
    What is under test is the ``env`` the spawn builds, which ``_spawn`` owns
    and the launcher never touches at this tier.

    Teardown is unconditionally suppressed so a raising cleanup cannot decide
    this test's outcome instead of the assertion.
    """
    import kiro_crew.knowledge.llm_pool as llm_pool

    # The spawn inherits this process's working directory, and ``_spawn`` takes no
    # ``cwd``, so without this the stub child runs from the repository root. Any
    # relative path it touched would land in the checkout.
    monkeypatch.chdir(stub.parent)
    monkeypatch.setattr(llm_pool, "wrap_argv", lambda argv, **kwargs: (argv, None))
    monkeypatch.setattr(llm_pool, "cgroup_scope_argv", lambda argv: argv)
    worker = CCWorker()
    worker._claude_bin = str(stub)
    try:
        await worker._spawn()
        proc = worker._proc
        assert proc is not None, "CCWorker._spawn recorded no child process"
        await asyncio.wait_for(proc.wait(), timeout=60)
    finally:
        task = worker._reader_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the stub child is a POSIX shell script")
@pytest.mark.asyncio
async def test_knowledge_worker_child_does_not_receive_an_agent_denied_key(
    tmp_path, monkeypatch
) -> None:
    """The fix: the agent-CLI worker cannot read an agent-denied key."""
    monkeypatch.setenv(PROBE_KEY, MARKER)
    stub, dump = _write_env_recording_stub(tmp_path)

    await _run_worker_spawn(stub, monkeypatch)

    env = _dumped_env(dump)
    assert env, "the stub child never ran, so nothing was proven"
    assert PROBE_KEY not in env, (
        f"{PROBE_KEY} (a sandbox._AGENT_DENIED_ENV_KEYS member) reached the "
        f"knowledge pool's agent-CLI worker, which runs with its permission "
        f"prompts disabled over untrusted document content"
    )
    assert MARKER not in env.values()


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the stub child is a POSIX shell script")
@pytest.mark.asyncio
async def test_knowledge_worker_child_does_not_receive_the_interpreter_env(
    tmp_path, monkeypatch
) -> None:
    """Second class at the same site: Kiro Crew's own interpreter paths.

    A foreign runtime that inherits ``PYTHONPATH`` imports Kiro Crew's
    site-packages instead of its own -- the hazard
    ``sandbox._PYTHON_ENV_PREFIXES`` documents.
    """
    monkeypatch.setenv("PYTHONPATH", MARKER)
    stub, dump = _write_env_recording_stub(tmp_path)

    await _run_worker_spawn(stub, monkeypatch)

    env = _dumped_env(dump)
    assert env, "the stub child never ran, so nothing was proven"
    assert "PYTHONPATH" not in env


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the stub child is a POSIX shell script")
@pytest.mark.asyncio
async def test_knowledge_worker_child_keeps_what_the_cli_needs(tmp_path, monkeypatch) -> None:
    """Positive control: the worker can still find and authenticate its CLI.

    The over-strict direction for this site. The external CLI is resolved from
    ``PATH``, reads its configuration under ``HOME``, and authenticates with a
    provider key the product deliberately forwards to agent children -- the same
    three things the guarded agent-CLI spawns in ``dashboard/handlers``
    forward through this identical helper.
    """
    monkeypatch.setenv(PROVIDER_KEY, "provider-key-value")
    stub, dump = _write_env_recording_stub(tmp_path)

    await _run_worker_spawn(stub, monkeypatch)

    env = _dumped_env(dump)
    assert env, "the stub child never ran, so nothing was proven"
    assert env.get(PROVIDER_KEY) == "provider-key-value", (
        f"{PROVIDER_KEY} is in neither the sensitive-prefix list nor the "
        f"agent-denied list, so scrubbing it would leave the worker unable to "
        f"authenticate"
    )
    assert env.get("PATH") == os.environ.get("PATH")
    assert env.get("HOME") == os.environ.get("HOME")


@pytest.mark.asyncio
async def test_knowledge_worker_wraps_at_the_operator_configured_tier(monkeypatch) -> None:
    """The operator's ``agent.sandbox`` setting reaches this worker's wrap.

    Asserted on the ``mode`` the sandbox wrap receives, which is where the
    setting has its effect, and with a tier that is not ``wrap_argv``'s own
    default -- so a worker that ignored the argument would read as ``auto``
    here rather than as the configured value.
    """
    import kiro_crew.knowledge.llm_pool as llm_pool

    seen: dict[str, object] = {}

    def _fake_wrap(argv, **kwargs):
        seen.update(kwargs)
        return argv, None

    async def _fake_spawn(*argv, **kwargs):
        return SimpleNamespace(
            pid=_UNALLOCATABLE_PID,
            stdout=None,
            wait=lambda: asyncio.sleep(0),
            returncode=0,
        )

    monkeypatch.setattr(llm_pool, "wrap_argv", _fake_wrap)
    monkeypatch.setattr(llm_pool, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(llm_pool, "create_subprocess_limited", _fake_spawn)

    worker = CCWorker(sandbox_mode="strict")
    worker._claude_bin = "/usr/bin/true"
    await worker._spawn()
    task = worker._reader_task
    if task is not None:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    assert seen.get("mode") == "strict", (
        "the tier the operator configured must reach the knowledge worker's "
        "sandbox wrap, as it reaches every other agent child's"
    )
    assert seen.get("strip_python_env") is True


@pytest.mark.asyncio
async def test_a_respawned_worker_reads_the_current_tier_not_the_first_one(
    monkeypatch,
) -> None:
    """A worker constructed without a tier must re-read it on every spawn.

    ``_spawn`` serves the respawn paths too -- ``send_message``'s recovery and
    ``reset_conversation`` -- so a tier stored at the first spawn would outlive
    the operator's setting for the life of the pool. The reader is patched to
    answer differently the second time, which is what an operator editing
    ``agent.sandbox`` between spawns looks like from here.
    """
    import kiro_crew.knowledge.llm_pool as llm_pool

    modes = iter(("standard", "strict"))
    seen: list[object] = []

    def _fake_wrap(argv, **kwargs):
        seen.append(kwargs.get("mode"))
        return argv, None

    async def _fake_spawn(*argv, **kwargs):
        return SimpleNamespace(
            pid=_UNALLOCATABLE_PID,
            stdout=None,
            wait=lambda: asyncio.sleep(0),
            returncode=0,
        )

    monkeypatch.setattr(llm_pool, "_get_sandbox_mode", lambda *a, **k: next(modes))
    monkeypatch.setattr(llm_pool, "wrap_argv", _fake_wrap)
    monkeypatch.setattr(llm_pool, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(llm_pool, "create_subprocess_limited", _fake_spawn)

    worker = CCWorker()
    worker._claude_bin = "/usr/bin/true"
    for _ in range(2):
        await worker._spawn()
        task = worker._reader_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    assert seen == ["standard", "strict"], (
        "the respawn reused the tier read at the first spawn, so an operator who "
        f"changed agent.sandbox keeps the old one for the pool's life: saw {seen}"
    )


@pytest.mark.asyncio
async def test_pool_reads_the_tier_per_worker_not_once_at_start(monkeypatch) -> None:
    """Each worker the pool builds must get the tier as it reads NOW.

    A pool outlives many workers: the idle reaper scales it to zero, ``acquire``
    builds it back, a dead worker is replaced. A tier stored at start became the
    tier for every worker the pool ever built, so an operator's later
    ``agent.sandbox`` edit reached none of them -- and the per-spawn resolve
    inside the worker cannot help, because a pool-built worker is handed an
    explicit value and never resolves for itself.
    """
    import kiro_crew.knowledge.llm_pool as llm_pool

    modes = iter(("standard", "strict"))
    monkeypatch.setattr(llm_pool, "_get_sandbox_mode", lambda *a, **k: next(modes))

    pool = llm_pool.LLMPool()
    pool._provider_type = "claude_code"

    async def _no_start(self) -> None:
        return None

    monkeypatch.setattr(CCWorker, "start", _no_start)

    first = await pool._create_worker()
    second = await pool._create_worker()

    assert first._sandbox_mode == "standard"
    assert second._sandbox_mode == "strict", (
        "the second worker was built with the tier read for the first, so an "
        f"agent.sandbox change never reaches a replacement worker: got "
        f"{second._sandbox_mode}"
    )
