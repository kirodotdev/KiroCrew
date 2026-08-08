"""Integration test: several REAL stub processes must share ONE backend.

Every other gateway test drives the stub seam in-process -- ``attach_stub("s1")``
takes a string label, not a process. That leaves the one property pooling exists
for completely unasserted: *do N independent client processes end up behind a
single MCP server?*

This matters because a pooling failure is SILENT. When the handshake breaks the
stub degrades to per-session ``exec`` (``stub.py``'s always-degrade guarantee),
so every tool call still works, no error surfaces, and the only difference is
that the memory saving is gone. Two such failures were found during the Windows
port and both presented as "everything works":

* ``os.getuid()`` in the Register frame raised on Windows, so pooling had never
  once taken effect there;
* the peer-identity check ran before the first pipe read, so admission denied
  100% of connections.

Assertions that say "nothing errored" cannot catch either. Counting the backends
can, so that is what this module does -- and it counts them from the OUTSIDE, by
having the spawned server append a line per launch, rather than by reading the
pool's private dict. A closed-box count survives refactors of the pool internals.

Platform coverage is the whole point, so this file deliberately contains no
platform branch other than where the endpoint is placed. Permission modes
(POSIX ``chmod`` vs Windows DACL) and identity verdicts (``SO_PEERCRED`` vs
token SID vs macOS ``UNVERIFIABLE``) are genuinely platform-shaped and are
covered by their own platform-split tests; folding them in here would turn this
into a per-platform mock farm and cost the portability that
:mod:`kiro_crew.mcp_gateway.transport` was written to provide.

``test_pool_integ_is_never_silently_skipped_on_windows`` guards the coverage
itself: a Windows skip would remove the only positive proof of pooling on the
platform where the transport is newest, and would do it while CI stayed green.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import transport

_FAKE_SERVER = Path(__file__).with_name("fake_pool_mcp_server.py")

#: How long to wait for a stub to register, drive a frame through the gateway
#: and get the reply back. Generous because Windows process creation plus a
#: named-pipe round trip is far slower than a unix socket, and a flaky timeout
#: here would read as "pooling broke".
_REPLY_TIMEOUT = 60.0


def _endpoint_dir() -> str | None:
    """Where to put the test endpoint.

    On POSIX this binds a real socket, and ``AF_UNIX`` caps ``sun_path`` at
    ~104 bytes -- pytest's ``tmp_path`` blows past that on macOS -- so it binds
    under ``/tmp``. A Windows named pipe has neither the length limit nor a
    ``/tmp``, so the platform default applies there. This is a path choice, not
    a behavioural branch: everything below is identical on all three platforms.
    """
    return None if pc.IS_WINDOWS else "/tmp"


def _init_frame(req_id: int) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pool-integ", "version": "0.0.0"},
        },
    }) + "\n"


async def _spawn_stub(
    *, socket_path: Path, server: str, agent: str, work_dir: Path, home: Path
) -> asyncio.subprocess.Process:
    """Launch a REAL stub process, exactly as the rewriter's overlay would."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kiro_crew.mcp_gateway.stub",
        "--server", server,
        "--agent", agent,
        "--target-command", sys.executable,
        "--target-args", str(_FAKE_SERVER),
        "--work-dir", str(work_dir),
        "--socket", str(socket_path),
        "--sandbox-mode", "off",
        "--approval-mode", "auto",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # KIROCREW_HOME redirects the stub's fallback audit log into the test's
        # own tree, so a degradation is observable instead of landing in the
        # developer's real home.
        env={**_clean_env(), "KIROCREW_HOME": str(home)},
    )


def _clean_env() -> dict[str, str]:
    import os

    # Drop any inherited channel id: it feeds PoolKey.channel_id, and a value
    # leaking in from the developer's shell would silently change partitioning.
    return {k: v for k, v in os.environ.items() if k != "KIROCREW_CHANNEL_ID"}


async def _drive_initialize(proc: asyncio.subprocess.Process, req_id: int) -> dict:
    """Write one ``initialize`` through the stub and return the parsed reply.

    The pool spawns lazily -- the Register handshake alone creates no backend --
    so a real frame is required to force the spawn-or-reuse decision. Getting
    the reply back also proves the whole chain (stub -> gateway -> backend ->
    stub) carried it, not merely that a process appeared.
    """
    assert proc.stdin is not None and proc.stdout is not None
    # Bound to locals so the None-narrowing survives into the closure below;
    # mypy does not carry an outer narrowing across a nested function.
    stdin, stdout = proc.stdin, proc.stdout
    stdin.write(_init_frame(req_id).encode("utf-8"))
    await stdin.drain()

    async def _read_reply() -> dict:
        while True:
            line = await stdout.readline()
            if not line:
                raise AssertionError(
                    "stub closed stdout before replying to initialize"
                )
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON diagnostics are not part of the protocol
            if msg.get("id") == req_id:
                return msg

    return await asyncio.wait_for(_read_reply(), timeout=_REPLY_TIMEOUT)


async def _reap(procs: list[asyncio.subprocess.Process]) -> None:
    """Tear down stubs through the portable primitive.

    ``kill_process_tree_async``, not ``os.killpg``/``signal.SIGKILL``: those
    names do not exist on Windows, and the shipped code was caught making
    exactly this mistake during the port -- the arguments are evaluated before
    the call, so the ``AttributeError`` escaped handlers written for
    ``ProcessLookupError``/``OSError``.
    """
    for p in procs:
        if p.returncode is None:
            try:
                await pc.kill_process_tree_async(p.pid, pc.SIGKILL)
            except Exception:  # noqa: BLE001 - teardown must never mask a failure
                pass
    for p in procs:
        try:
            await asyncio.wait_for(p.wait(), timeout=15)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass


def _launch_count(log: Path) -> int:
    """How many backend processes the pool actually created."""
    if not log.exists():
        return 0
    return len([ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()])


@pytest.mark.asyncio
async def test_real_stubs_sharing_a_key_share_one_backend(tmp_path: Path, short_sock_dir) -> None:
    """THE pooling assertion: 3 real stub processes -> exactly 1 MCP server.

    Also asserts partitioning in the same run: a fourth stub under a DIFFERENT
    agent must get its OWN backend. Reuse alone would pass even if PoolKey
    ignored the agent entirely, which would silently break isolation between
    agents -- a correctness bug, not merely a lost optimisation.
    """
    endpoint_root = short_sock_dir
    sock = endpoint_root / "gw.sock"
    work_dir = tmp_path / "ws"
    work_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    launch_log = tmp_path / "launches.txt"

    def _resolver(_key: object) -> tuple[str, list[str], dict[str, str], str]:
        # Every key resolves to the same target, so the ONLY thing that can
        # make the pool spawn twice is the key differing -- which is exactly
        # the property under test.
        return (sys.executable, [str(_FAKE_SERVER), str(launch_log)], {}, str(work_dir))

    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(
            socket_path=sock,
            max_backends=8,
            idle_timeout_secs=300,
            stop_event=stop,
            target_resolver=_resolver,
            prewarm_count=0,
        )
    )
    procs: list[asyncio.subprocess.Process] = []
    try:
        for _ in range(100):  # wait for the endpoint to accept
            if transport.endpoint_exists(sock):
                break
            await asyncio.sleep(0.05)
        assert transport.endpoint_exists(sock), "gatewayd never bound its endpoint"

        # --- three stubs, one identical key -------------------------------
        for i in range(3):
            proc = await _spawn_stub(
                socket_path=sock, server="fake", agent="probe",
                work_dir=work_dir, home=home,
            )
            procs.append(proc)
            reply = await _drive_initialize(proc, req_id=i + 1)
            assert "result" in reply, f"stub {i} got no initialize result: {reply}"

        assert _launch_count(launch_log) == 1, (
            f"3 stubs sharing one PoolKey spawned {_launch_count(launch_log)} "
            "backends, expected 1 — pooling is not taking effect. Note this "
            "fails the same way whether the pool double-spawned or the stubs "
            "degraded to per-session exec, which is the point: both are the "
            "silent no-saving failure this test exists to make loud."
        )

        # No stub may have taken the degrade path; a fallback would still have
        # answered initialize, so the reply assertions above cannot see it.
        fallback = home / "logs" / "stub_fallback.jsonl"
        assert not fallback.exists(), (
            f"a stub degraded to per-session exec: {fallback.read_text()}"
        )

        # --- a different agent must NOT share it --------------------------
        other = await _spawn_stub(
            socket_path=sock, server="fake", agent="other-agent",
            work_dir=work_dir, home=home,
        )
        procs.append(other)
        reply = await _drive_initialize(other, req_id=99)
        assert "result" in reply, f"partition stub got no result: {reply}"

        assert _launch_count(launch_log) == 2, (
            f"a stub under a different agent produced "
            f"{_launch_count(launch_log)} backends total, expected 2 — PoolKey "
            "is not partitioning by agent, so two agents would share one MCP "
            "server process and its state."
        )
    finally:
        await _reap(procs)
        stop.set()
        try:
            await asyncio.wait_for(daemon, timeout=30)
        except asyncio.TimeoutError:  # pragma: no cover - daemon shutdown hang
            daemon.cancel()


def _windows_collect_ignore() -> list[str]:
    """Read the Windows exclusion list from its DATA FILE, not conftest's runtime state.

    Importing conftest and reading the attribute looks equivalent and is not:
    the list is assigned inside ``if platform_compat.IS_WINDOWS:``, so on the
    Linux matrix -- the only place this guard runs -- the attribute does not
    exist at all and ``getattr(..., [])`` silently yields an empty set. Every
    membership assertion against it then passes vacuously, which is precisely
    the class of false pass this guard exists to prevent. Reading the file is
    platform-independent.

    The names used to be string literals inside conftest and were extracted by
    parsing its AST. They now live in ``windows-collect-ignore.txt`` because a
    second reader needs them: naming a file explicitly on the pytest command
    line bypasses ``collect_ignore``, so the CI reduced-scope selector
    (``scripts/ci-surface-tests.py``) has to apply the same exclusion itself.
    Reading that file keeps this guard pointed at the real source of truth --
    an AST walk over conftest now finds no literals and would go blind.
    """
    listfile = Path(__file__).with_name("windows-collect-ignore.txt")
    found = [
        name
        for name in (
            ln.split("#", 1)[0].strip()
            for ln in listfile.read_text(encoding="utf-8").splitlines()
        )
        if name
    ]
    assert found, (
        f"could not read any excluded filenames from {listfile.name} — this "
        "guard has gone blind and would pass no matter what was excluded"
    )
    return found


def test_pool_integ_is_never_silently_skipped_on_windows() -> None:
    """Guard the coverage, not the code.

    Windows has two silent filters -- ``conftest.collect_ignore`` (whole files)
    and ``windows-expected-failures.txt`` (node ids marked skip) -- and nothing
    audits either. During the port a set of enforcing tests was written,
    mutation-verified, and still never ran on Windows, because their whole file
    sat in ``collect_ignore``; CI stayed green throughout.

    A skipped test asserts nothing, so this guard has to live outside the suite
    it protects. It runs on Linux, where it always executes, and fails loudly if
    the pooling proof is ever excluded from the platform whose transport is
    newest.
    """
    this_file = Path(__file__).name

    assert this_file not in set(_windows_collect_ignore()), (
        f"{this_file} was added to conftest.collect_ignore — the only positive "
        "proof that pooling happens would stop running on Windows"
    )

    listfile = Path(__file__).with_name("windows-expected-failures.txt")
    if listfile.exists():
        entries = [
            ln.strip()
            for ln in listfile.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        offenders = [e for e in entries if e.startswith(f"test/{this_file}::")]
        assert not offenders, (
            f"pooling assertions were added to the Windows skip list: {offenders}"
        )
