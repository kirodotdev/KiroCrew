"""``runtime._run_cmd``'s two opt-ins for a network git step run from the gateway.

``sandboxed_hook_scan`` runs the config-hook name scan inside the sandbox, and
``max_output_bytes`` drains the child's output incrementally under a byte cap,
killing it and refusing past the cap. Both default to the behaviour every
existing caller already had, and exactly one caller engages them: the release
worktree's network runner (``release_worktree._run_network_git``).

Three groups of test, all against the REAL ``_run_cmd`` or its helpers:

* the defaults leave the shared path as it was, and only the release runner
  passes the opt-ins;
* each opt-in, engaged, takes its own branch;
* the capped reader itself, driven by a fake child.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.dev_fleet import release_worktree, runtime
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def _stub_spawn_prep(monkeypatch, argv: list[str]) -> None:
    """Skip sandbox preparation and run ``argv`` as the real child.

    ``shielded_prepare_off_loop`` is the step that wraps the command for the OS
    sandbox; standing it in with the bare argv keeps every other part of
    ``_run_cmd`` real (trusted-bin pin aside), including the spawn and the read.
    """

    async def _prep(prepare, *, executor=None):
        return (list(argv), {}, None)

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _prep)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: sys.executable)


# --------------------------------------------------------------------------
# the defaults: today's behaviour, and one caller that opts in
# --------------------------------------------------------------------------
def test_the_opt_ins_are_keyword_only_and_default_to_todays_behaviour():
    params = inspect.signature(runtime._run_cmd).parameters
    assert params["sandboxed_hook_scan"].default is False
    assert params["max_output_bytes"].default is None
    for name in ("sandboxed_hook_scan", "max_output_bytes"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_the_default_path_keeps_the_unsandboxed_scan_and_the_plain_read(monkeypatch):
    """With neither opt-in passed, the hook scan is the unsandboxed
    ``_with_config_hooks_off`` and the read is ``proc.communicate()``."""
    used: list[str] = []

    def _unsandboxed(cmd, cwd, env=None):
        used.append("unsandboxed")
        return list(cmd)

    async def _sandboxed(*_a, **_k):  # pragma: no cover - the default path never calls it
        raise AssertionError("the default path must not run the sandboxed scan")

    async def _capped(*_a, **_k):  # pragma: no cover - the default path never calls it
        raise AssertionError("the default path must not use the capped reader")

    argv = [sys.executable, "-c", "print('ok')"]
    _stub_spawn_prep(monkeypatch, argv)
    monkeypatch.setattr(runtime, "_with_config_hooks_off", _unsandboxed)
    monkeypatch.setattr(runtime, "_with_config_hooks_off_sandboxed", _sandboxed)
    monkeypatch.setattr(runtime, "_communicate_capped", _capped)
    rc, out, err = await runtime._run_cmd(list(argv))
    assert (rc, out.strip()) == (0, "ok"), err
    assert used == ["unsandboxed"]


@pytest.mark.asyncio
async def test_a_default_scan_error_still_propagates(monkeypatch):
    """The ``RuntimeError`` branch the sandboxed scan needs is scoped to that scan: on
    the default path a scan error propagates exactly as it did before the opt-in."""

    def _boom(cmd, cwd, env=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "_with_config_hooks_off", _boom)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: sys.executable)
    with pytest.raises(RuntimeError, match="boom"):
        await runtime._run_cmd([sys.executable, "-c", "pass"])


def test_only_the_release_network_runner_passes_the_opt_ins():
    """Every ``_run_cmd`` call in the package except one keeps the defaults, so the
    shared path is unchanged for them. A new caller that opts in fails here and has to
    be added on purpose."""
    root = Path(runtime.__file__).resolve().parents[3]  # src/kiro_crew
    opt_ins = {"sandboxed_hook_scan", "max_output_bytes"}
    callers: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        if "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "_run_cmd" not in text or not any(name in text for name in opt_ins):
            continue
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                if name == "_run_cmd" and opt_ins & {kw.arg for kw in node.keywords}:
                    callers.add((path.relative_to(root).as_posix(), fn.name))
    assert callers == {("apps/builtins/dev_fleet/release_worktree.py", "_run_network_git")}


@pytest.mark.asyncio
async def test_the_no_sandbox_refusal_reads_as_the_release_action_expects(monkeypatch):
    """``release_worktree`` turns ``_run_cmd``'s no-sandbox refusal into
    ``sandbox_unavailable`` by its wording, so both places ``_run_cmd`` words it --
    sandbox preparation and the sandboxed hook scan -- are pinned to that prefix."""

    async def _no_backend(prepare, *, executor=None):
        raise RuntimeError("no OS sandbox backend")

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _no_backend)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: sys.executable)
    rc, out, err = await runtime._run_cmd([sys.executable, "-c", "pass"])
    assert (rc, out) == (-1, "")
    assert release_worktree._sandbox_reason(err) == "no OS sandbox backend"

    async def _scan_no_backend(cmd, cwd, env, mode):
        raise RuntimeError("no OS sandbox backend")

    monkeypatch.setattr(runtime, "_with_config_hooks_off_sandboxed", _scan_no_backend)
    rc, out, err = await runtime._run_cmd([sys.executable, "-c", "pass"], sandboxed_hook_scan=True)
    assert (rc, out) == (-1, "")
    assert release_worktree._sandbox_reason(err) == "no OS sandbox backend"


# --------------------------------------------------------------------------
# each opt-in, engaged
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_sandboxed_scan_wraps_git_and_passes_other_commands(monkeypatch):
    seen: dict[str, object] = {}

    async def _scan(target, *, spawn_argv, mode, env=None):
        seen["target"], seen["mode"], seen["spawn_argv"] = target, mode, spawn_argv
        return ["-c", "hook.evil.enabled=false"]

    monkeypatch.setattr(runtime, "config_hook_disable_args_sandboxed_async", _scan)
    wrapped = await runtime._with_config_hooks_off_sandboxed(
        ["git", "-C", "/srv/repo", "ls-remote"], None, {"PATH": "/usr/bin"}, "strict"
    )
    assert wrapped == ["git", "-c", "hook.evil.enabled=false", "-C", "/srv/repo", "ls-remote"]
    assert seen["mode"] == "strict" and str(seen["target"]).endswith("repo")
    assert seen["spawn_argv"] is runtime.sandboxed_spawn_argv
    # A non-git command is returned untouched and the scan never runs.
    seen.clear()
    passed = await runtime._with_config_hooks_off_sandboxed(["echo", "hi"], None, {}, "standard")
    assert passed == ["echo", "hi"] and seen == {}


@pytest.mark.asyncio
async def test_opting_in_routes_the_scan_through_the_sandbox(monkeypatch):
    used: list[tuple[str, object]] = []

    async def _sandboxed(cmd, cwd, env, mode):
        used.append(("sandboxed", mode))
        return list(cmd)

    def _unsandboxed(cmd, cwd, env=None):  # pragma: no cover - must not run when opted in
        used.append(("unsandboxed", None))
        return list(cmd)

    argv = [sys.executable, "-c", "pass"]
    _stub_spawn_prep(monkeypatch, argv)
    monkeypatch.setattr(runtime, "_with_config_hooks_off_sandboxed", _sandboxed)
    monkeypatch.setattr(runtime, "_with_config_hooks_off", _unsandboxed)
    rc, _o, _e = await runtime._run_cmd(list(argv), mode="strict", sandboxed_hook_scan=True)
    assert rc == 0
    assert used == [("sandboxed", "strict")], used


@pytest.mark.asyncio
async def test_the_cap_kills_and_refuses_a_child_that_floods_stdout(monkeypatch):
    limit = 16 * 1024
    argv = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x' * ({limit} * 8))"]
    _stub_spawn_prep(monkeypatch, argv)
    rc, out, err = await runtime._run_cmd(list(argv), max_output_bytes=limit)
    assert (rc, out) == (-1, "")
    assert f"more than {limit} bytes" in err


@pytest.mark.asyncio
async def test_the_cap_returns_full_output_under_the_limit(monkeypatch):
    argv = [sys.executable, "-c", "print('ok')"]
    _stub_spawn_prep(monkeypatch, argv)
    rc, out, _err = await runtime._run_cmd(list(argv), max_output_bytes=1024 * 1024)
    assert (rc, out.strip()) == (0, "ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_the_cap_stops_a_child_that_floods_and_stays_alive(monkeypatch, stream):
    """The shape a hostile remote takes: past the cap the child is still alive, holding
    both pipes, while the two drains await them. The kill must not read those pipes
    itself (asyncio refuses a second reader with a RuntimeError, which would escape as a
    500): the child is stopped and the step answers -- a refusal for a stdout flood, a
    failed exit with a bounded stderr tail for a stderr flood."""
    limit = 16 * 1024
    # Writes until it is killed, so a full pipe buffer is still unread when the cap trips.
    code = "import sys\n" f"w = sys.{stream}.buffer.write\n" "while True:\n" "    w(b'x' * 65536)\n"
    argv = [sys.executable, "-c", code]
    _stub_spawn_prep(monkeypatch, argv)
    rc, out, err = await runtime._run_cmd(
        list(argv), max_output_bytes=limit, timeout=FALLBACK_TIMEOUT_SECS
    )
    if stream == "stdout":
        assert (rc, out) == (-1, "")
        assert f"more than {limit} bytes" in err
    else:
        assert rc not in (0, -1), (rc, err[-200:])
        assert len(err) <= limit


# --------------------------------------------------------------------------
# the capped reader, against a fake child
# --------------------------------------------------------------------------
class _FakeStream:
    """An ``asyncio.StreamReader`` stand-in that yields ``data`` in small chunks."""

    def __init__(self, data: bytes, chunk: int = 4096) -> None:
        self._data = data
        self._pos = 0
        self._chunk = chunk

    async def read(self, n: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = min(self._pos + min(n, self._chunk), len(self._data))
        out = self._data[self._pos : end]
        self._pos = end
        return out


class _FakeProc:
    """A child whose stdout/stderr are prebuilt buffers, for ``_communicate_capped``."""

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.pid = UNALLOCATABLE_PID  # never a live process, so no kill can reach one
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _record_kills(monkeypatch) -> list[int]:
    """Stand in the tree kill; return the list each kill appends the pid to. The reader's
    own ``proc.kill()`` lands on the fake child, and ``kill_and_reap`` must not run at all:
    its reap reads the pipes the drains hold."""
    killed: list[int] = []

    async def _kill_tree(pid: int) -> None:
        killed.append(pid)

    async def _no_reap(p: Any) -> None:
        raise AssertionError("the capped reader must not reap through communicate()")

    monkeypatch.setattr(runtime, "_kill_tree", _kill_tree)
    monkeypatch.setattr(runtime.platform_compat, "kill_and_reap", _no_reap)
    return killed


@pytest.mark.asyncio
async def test_the_reader_kills_and_flags_a_child_that_floods_stdout(monkeypatch):
    """A remote flooding stdout past the cap is killed mid-stream and reported exceeded,
    with no more than ``limit`` bytes retained, so a hostile advertisement can neither
    fill the gateway nor wedge a half-drained pipe until the timeout."""
    limit = 16 * 1024
    proc = _FakeProc(stdout=b"x" * (limit * 4))
    killed = _record_kills(monkeypatch)
    out, _err, exceeded = await runtime._communicate_capped(proc, limit)
    assert exceeded is True
    assert len(out) <= limit
    assert killed == [proc.pid], "the flooding child must be killed exactly once"


@pytest.mark.asyncio
async def test_the_reader_returns_all_output_under_the_cap(monkeypatch):
    """Under the cap the child is waited on normally, all output is returned, and nothing
    is killed: the honest-remote path."""
    payload = b"%s\trefs/tags/v1.2.3\n" % (b"a" * 40)
    proc = _FakeProc(stdout=payload * 50, stderr=b"warning: minor\n")
    killed = _record_kills(monkeypatch)
    out, err, exceeded = await runtime._communicate_capped(proc, 1024 * 1024)
    assert exceeded is False
    assert out == payload * 50 and err == b"warning: minor\n"
    assert killed == []
    assert proc.returncode == 0, "wait() runs on the non-killed path"


@pytest.mark.asyncio
async def test_the_reader_does_not_tree_kill_a_child_that_was_already_reaped(monkeypatch):
    """A child that exited and was reaped while its output still sat in the pipes has given
    up its pid, which may already name another process. Crossing the cap then caps and
    flags the output but skips the tree kill."""
    limit = 16 * 1024
    proc = _FakeProc(stdout=b"x" * (limit * 4))
    proc.returncode = 0  # exited and reaped, its output still buffered
    killed = _record_kills(monkeypatch)
    out, _err, exceeded = await runtime._communicate_capped(proc, limit)
    assert exceeded is True
    assert len(out) <= limit
    assert killed == [], "a reaped child's pid must not be tree-killed"


@pytest.mark.asyncio
async def test_the_reader_keeps_the_stderr_tail_and_kills_a_stderr_flood(monkeypatch):
    """stderr is kept as a rolling TAIL bounded by the cap, so a flood cannot grow the
    process yet git's final ``fatal:`` line survives; a stderr flood also kills the child.
    ``exceeded`` tracks stdout only: a stderr flood is bounded, not a refusal."""
    limit = 8 * 1024
    tail = b"\nfatal: the real last line"
    proc = _FakeProc(stdout=b"", stderr=b"e" * (limit * 3) + tail)
    killed = _record_kills(monkeypatch)
    out, err, exceeded = await runtime._communicate_capped(proc, limit)
    assert out == b"" and exceeded is False
    assert len(err) <= limit
    assert err.endswith(tail), "the final stderr line must survive in the bounded tail"
    assert killed == [proc.pid], "a stderr flood past the cap must also kill the child once"
