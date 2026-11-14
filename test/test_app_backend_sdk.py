"""The public backend SDK (``kiro_crew.apps.backend_sdk``).

What is pinned is what an app cannot check for itself: every public name answers
with the gateway's own control (same refusals, same exception classes), the spawn
seam offers no knob that widens the sandbox, the audit writer is attributed to the
spawned app's own name, and the package imports in a spawned child without loading
the controls it does not use.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import hooks, platform_compat, sandbox
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL
from kiro_crew.apps.backend_sdk import spawn as sdk_spawn
from kiro_crew.apps.scrub_sdk import ScrubSDK
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, wait_until

_SRC = Path(__file__).resolve().parents[1] / "src"

# A fake AWS access key id: the documented example shape, never a live credential.
_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


# ── the package and its level ───────────────────────────────────────────────


def test_every_public_name_resolves_and_is_listed() -> None:
    for name in sdk.__all__:
        assert getattr(sdk, name) is not None, name
        assert name in dir(sdk)
    with pytest.raises(AttributeError):
        getattr(sdk, "safe_read_file_bytes")


def test_the_package_reports_this_build_level_and_is_published_at_seven() -> None:
    assert sdk.SDK_LEVEL == APP_SDK_LEVEL
    assert sdk.BACKEND_SDK_LEVEL == 7
    assert sdk.SDK_LEVEL >= sdk.BACKEND_SDK_LEVEL


def test_a_spawned_child_imports_it_without_loading_unused_controls(tmp_path) -> None:
    probe = (
        "import sys\n"
        "import kiro_crew.apps.backend_sdk as sdk\n"
        "print(sdk.SDK_LEVEL)\n"
        "print('kiro_crew.sandbox' in sys.modules, 'kiro_crew.hooks' in sys.modules)\n"
        "print(sdk.is_sensitive_path(sys.argv[1]))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_APP_NAME": "probe-app",
    }
    done = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path / "plain.txt")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    level, loaded, sensitive = done.stdout.split("\n")[:3]
    assert level == str(APP_SDK_LEVEL)
    assert loaded == "False False"
    assert sensitive == "False"


# ── paths ───────────────────────────────────────────────────────────────────


def test_sensitive_path_answers_with_the_gateway_gate(tmp_path) -> None:
    credential = str(Path.home() / ".ssh" / "id_ed25519")
    assert sdk.is_sensitive_path(credential) is True
    assert sdk.is_sensitive_path(str(tmp_path / "notes.txt")) is False


def test_contains_sensitive_path_sees_a_credential_home_below_the_root(tmp_path) -> None:
    assert sdk.contains_sensitive_path(str(Path.home())) is True
    assert sdk.contains_sensitive_path(str(tmp_path)) is False


def test_the_path_checks_delegate_at_call_time(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda p: seen.append(p) or 1)
    assert sdk.is_sensitive_path("/x") is True
    assert seen == ["/x"]


# ── files ───────────────────────────────────────────────────────────────────


def test_read_bytes_nofollow_reads_a_plain_file(tmp_path) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"hello")
    assert sdk.read_bytes_nofollow(str(target), within_root=str(tmp_path)) == b"hello"


def test_read_bytes_nofollow_refuses_a_link_and_a_hardlink(tmp_path) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"hello")
    hard = tmp_path / "hard.txt"
    os.link(target, hard)
    assert sdk.read_bytes_nofollow(str(hard)) is None
    if not platform_compat.IS_WINDOWS:
        soft = tmp_path / "soft.txt"
        soft.symlink_to(target)
        assert sdk.read_bytes_nofollow(str(soft)) is None


def test_read_bytes_nofollow_refuses_a_file_outside_its_root(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    assert sdk.read_bytes_nofollow(str(outside), within_root=str(root)) is None


def test_read_bytes_nofollow_raises_the_gateway_too_large_class(tmp_path) -> None:
    target = tmp_path / "big.bin"
    target.write_bytes(b"0123456789")
    assert sdk.FileTooLargeError is hooks.FileTooLargeError
    with pytest.raises(hooks.FileTooLargeError):
        sdk.read_bytes_nofollow(str(target), max_bytes=4)


def test_read_bytes_nofollow_refuses_a_sensitive_path() -> None:
    from kiro_crew.config.paths import data_home

    # The data home is pinned per test, so this `.env` is the test's own.
    secret = Path(data_home()) / ".env"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("TOKEN=x\n", encoding="utf-8")
    assert sdk.is_sensitive_path(str(secret)) is True
    assert sdk.read_bytes_nofollow(str(secret)) is None


def test_atomic_write_replaces_the_file_whole(tmp_path) -> None:
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")
    sdk.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX mode bits are the subject")
def test_atomic_write_restrict_to_owner_makes_the_file_owner_only(tmp_path) -> None:
    target = tmp_path / "secret.json"
    sdk.atomic_write(target, b"{}", restrict_to_owner=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX mode bits are the subject")
def test_ensure_owner_only_dir_is_0700_inside_the_data_home() -> None:
    from kiro_crew.config.paths import data_home

    target = Path(data_home()) / "apps" / "probe" / "data" / "cache"
    sdk.ensure_owner_only_dir(target)
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


# ── redaction ───────────────────────────────────────────────────────────────


def test_scrub_outbound_is_the_context_scrub() -> None:
    text = f"key {_FAKE_KEY} here"
    result = sdk.scrub_outbound(text)
    assert result == ScrubSDK().outbound(text)
    assert _FAKE_KEY not in result.text
    assert result.redacted is True
    assert isinstance(result, sdk.ScrubResult)


# ── audit ───────────────────────────────────────────────────────────────────


class _Sink:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kw: object) -> None:
        self.calls.append(dict(kw))


@pytest.fixture()
def sink(monkeypatch):
    s = _Sink()
    monkeypatch.setattr("kiro_crew.apps.audit_sdk.sel", lambda: s)
    return s


def test_audit_log_attributes_rows_to_the_spawned_app(monkeypatch, sink) -> None:
    monkeypatch.setenv("KIROCREW_APP_NAME", "doc-store")
    sdk.audit_log().record("publish", "success", resources="doc-1")
    (call,) = sink.calls
    assert call["caller"] == "app:doc-store"
    assert call["operation"] == "doc-store.publish"
    assert call["source"] == "app"


def test_audit_log_takes_an_explicit_name(monkeypatch, sink) -> None:
    monkeypatch.delenv("KIROCREW_APP_NAME", raising=False)
    sdk.audit_log("other").record("pull", "denied")
    assert sink.calls[0]["caller"] == "app:other"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_audit_log_refuses_a_writer_with_no_app_name(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("KIROCREW_APP_NAME", raising=False)
    else:
        monkeypatch.setenv("KIROCREW_APP_NAME", value)
    with pytest.raises(LookupError):
        sdk.audit_log()


# ── spawn ───────────────────────────────────────────────────────────────────


class _SpawnRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return (["wrapped", *args[0]], {"PATH": "/usr/bin"}, None)


def test_sandboxed_argv_forwards_only_tier_env_and_python_strip(monkeypatch) -> None:
    rec = _SpawnRecorder()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", rec)
    plan = sdk.sandboxed_argv(["git", "status"], mode="strict", strip_python_env=True)
    assert plan == (["wrapped", "git", "status"], {"PATH": "/usr/bin"}, None)
    ((args, kwargs),) = rec.calls
    assert args == (["git", "status"], "strict")
    assert kwargs == {"env": None, "strip_python_env": True}


@pytest.mark.parametrize("mode", ["off", "auto", "cc", "workflow", ""])
def test_sandboxed_argv_refuses_any_other_tier_before_preparing(monkeypatch, mode) -> None:
    rec = _SpawnRecorder()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", rec)
    with pytest.raises(ValueError):
        sdk.sandboxed_argv(["true"], mode=mode)
    assert rec.calls == []


@pytest.mark.parametrize(
    "knob",
    [
        "extra_hidden_dirs",
        "extra_visible_dirs",
        "extra_private_dirs",
        "extra_writable_dirs",
        "first_party_fixed_argv",
        "is_kiro_cli",
    ],
)
def test_sandboxed_argv_offers_no_widening_knob(knob) -> None:
    import inspect

    for fn in (sdk.sandboxed_argv, sdk.sandboxed_argv_async):
        assert knob not in inspect.signature(fn).parameters


def test_sandboxed_argv_async_forwards_the_same_choices(monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return (["w"], {}, "/tmp/cleanup")

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv_async", fake)
    plan = asyncio.run(
        asyncio.wait_for(sdk.sandboxed_argv_async(["ls"]), timeout=FALLBACK_TIMEOUT_SECS)
    )
    assert plan == (["w"], {}, "/tmp/cleanup")
    assert calls == [((["ls"], "standard"), {"env": None, "strip_python_env": False})]
    with pytest.raises(ValueError):
        asyncio.run(
            asyncio.wait_for(
                sdk.sandboxed_argv_async(["ls"], mode="off"), timeout=FALLBACK_TIMEOUT_SECS
            )
        )
    assert len(calls) == 1


def test_sandbox_unavailable_is_the_gateway_class() -> None:
    assert sdk.SandboxUnavailableError is sandbox.SandboxUnavailableError


def test_run_sandboxed_runs_the_wrapped_argv_with_the_scrubbed_env(monkeypatch, tmp_path):
    cleanup = tmp_path / "launcher.sh"
    cleanup.write_text("x", encoding="utf-8")
    seen: list[tuple] = []

    def fake_prepare(argv, mode, *, env, strip_python_env):
        seen.append(("prepare", argv, mode, env, strip_python_env))
        return (["wrap", *argv], {"SCRUBBED": "1"}, str(cleanup))

    def fake_run(argv, **kwargs):
        seen.append(("run", argv, kwargs))
        return "done"

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", fake_prepare)
    monkeypatch.setattr("kiro_crew.sandbox.run_limited", fake_run)
    got = sdk.run_sandboxed(["echo", "hi"], mode="strict", env={"A": "b"}, check=True)
    assert got == "done"
    assert seen == [
        ("prepare", ["echo", "hi"], "strict", {"A": "b"}, False),
        ("run", ["wrap", "echo", "hi"], {"env": {"SCRUBBED": "1"}, "check": True}),
    ]
    assert not cleanup.exists()


def test_run_sandboxed_removes_the_cleanup_path_when_the_run_raises(monkeypatch, tmp_path):
    cleanup = tmp_path / "launcher.sh"
    cleanup.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "kiro_crew.sandbox.sandboxed_spawn_argv",
        lambda argv, mode, **_kw: (argv, {}, str(cleanup)),
    )

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, FALLBACK_TIMEOUT_SECS)

    monkeypatch.setattr("kiro_crew.sandbox.run_limited", boom)
    with pytest.raises(subprocess.TimeoutExpired):
        sdk.run_sandboxed(["sleep", "9"], timeout=FALLBACK_TIMEOUT_SECS)
    assert not cleanup.exists()


def test_run_sandboxed_refuses_another_tier_before_spawning(monkeypatch) -> None:
    rec = _SpawnRecorder()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", rec)
    with pytest.raises(ValueError):
        sdk.run_sandboxed(["true"], mode="off")
    assert rec.calls == []


@pytest.mark.parametrize(
    "keyword",
    ["profile", "executable", "pass_fds", "close_fds", "preexec_fn", "shell", "user"],
)
def test_run_sandboxed_refuses_a_keyword_outside_its_list_before_preparing(
    monkeypatch, keyword
) -> None:
    rec = _SpawnRecorder()
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", rec)
    monkeypatch.setattr(
        "kiro_crew.sandbox.run_limited", lambda *a, **k: pytest.fail("ran the child")
    )
    with pytest.raises(TypeError, match=keyword):
        sdk.run_sandboxed(["true"], **{keyword: None})
    assert rec.calls == []


def test_run_sandboxed_forwards_every_listed_keyword(monkeypatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(
        "kiro_crew.sandbox.sandboxed_spawn_argv", lambda argv, mode, **_kw: (argv, {}, None)
    )
    monkeypatch.setattr("kiro_crew.sandbox.run_limited", lambda argv, **kw: seen.append(kw))
    listed = {name: object() for name in sdk_spawn.RUN_KWARGS}
    sdk.run_sandboxed(["true"], **listed)
    assert seen == [{"env": {}, **listed}]


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="process groups are POSIX")
def test_run_sandboxed_async_starts_the_child_in_its_own_process_group(monkeypatch, tmp_path):
    async def fake_prepare(argv, mode, *, env, strip_python_env):
        return (list(argv), {"PATH": os.environ.get("PATH", "")}, None)

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv_async", fake_prepare)
    done = asyncio.run(
        asyncio.wait_for(
            sdk.run_sandboxed_async(
                [sys.executable, "-c", "import os; print(os.getpgid(0) == os.getpid())"],
                timeout=FALLBACK_TIMEOUT_SECS,
            ),
            timeout=FALLBACK_TIMEOUT_SECS * 2,
        )
    )
    assert done.stdout.strip() == b"True"


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="runs a POSIX shell child")
def test_run_sandboxed_async_captures_output_and_removes_the_cleanup(monkeypatch, tmp_path):
    cleanup = tmp_path / "launcher.sh"
    cleanup.write_text("x", encoding="utf-8")
    prepared: list[tuple] = []

    async def fake_prepare(argv, mode, *, env, strip_python_env):
        prepared.append((argv, mode))
        return (list(argv), {"PATH": os.environ.get("PATH", "")}, str(cleanup))

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv_async", fake_prepare)
    done = asyncio.run(
        asyncio.wait_for(
            sdk.run_sandboxed_async(
                [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                input=b"ping",
                timeout=FALLBACK_TIMEOUT_SECS,
            ),
            timeout=FALLBACK_TIMEOUT_SECS * 2,
        )
    )
    assert done.returncode == 0
    assert done.stdout == b"ping"
    assert prepared[0][1] == "standard"
    assert not cleanup.exists()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="process groups are POSIX")
def test_run_sandboxed_async_cleanup_kills_a_descendant_that_outlived_the_child(
    monkeypatch, tmp_path
):
    leader_file = tmp_path / "leader.pid"
    background_file = tmp_path / "background.pid"
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    groups: list[int | None] = []
    read_group = platform_compat._isolated_group_of_live_child

    async def fake_prepare(argv, mode, *, env, strip_python_env):
        return (list(argv), {"PATH": os.environ.get("PATH", "")}, None)

    def recording_group_read(pid: int, *, reaped: bool) -> int | None:
        groups.append(read_group(pid, reaped=reaped))
        return groups[-1]

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv_async", fake_prepare)
    monkeypatch.setattr(platform_compat, "_isolated_group_of_live_child", recording_group_read)

    def pid_in(path: Path) -> int | None:
        text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        return int(text) if text.isdigit() else None

    def open_the_gate() -> bool:
        # Opening the FIFO for writing and closing it ends the shell's read with EOF.
        # Without a reader yet, a non-blocking open fails with ENXIO: try again.
        try:
            os.close(os.open(gate, os.O_WRONLY | os.O_NONBLOCK))
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
            return False
        return True

    # The shell records itself and a background sleep that keeps the captured pipes,
    # then exits: the case where the child is already reaped when the cleanup runs.
    # It waits on the gate first, and the gate opens only once run_sandboxed_async
    # has read the shell's process group: a shell that exits before that read
    # leaves no group to kill (#37024). The sleep outlasts every wait below, so
    # only the cleanup can end it in time.
    script = (
        f'echo $$ > "{leader_file}"; sleep 600 & echo $! > "{background_file}"; '
        f'read _ < "{gate}"'
    )

    async def scenario() -> None:
        task = asyncio.ensure_future(
            sdk.run_sandboxed_async(["sh", "-c", script], timeout=FALLBACK_TIMEOUT_SECS)
        )
        await async_wait_until(
            lambda: groups, describe=lambda: "run_sandboxed_async has not read the group"
        )
        await async_wait_until(
            open_the_gate, off_loop=True, describe=lambda: "the shell never opened the gate"
        )
        await async_wait_until(
            lambda: pid_in(background_file) is not None
            and pid_in(leader_file) is not None
            and not platform_compat.pid_exists(pid_in(leader_file)),
            off_loop=True,
            describe=lambda: "the shell has not exited with its background sleep running",
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=FALLBACK_TIMEOUT_SECS)

    try:
        asyncio.run(scenario())
        assert groups == [pid_in(leader_file)], "the group was not read while the shell lived"
        background = pid_in(background_file)
        assert background is not None
        wait_until(
            lambda: not platform_compat.pid_exists(background),
            timeout=FALLBACK_TIMEOUT_SECS,
            describe=lambda: f"background sleep {background} survived the cleanup",
        )
    finally:
        leftover = pid_in(background_file)
        if leftover is not None and platform_compat.pid_exists(leftover):
            platform_compat.kill_pid(leftover, platform_compat.SIGKILL)


def test_run_sandboxed_async_kills_the_child_past_its_timeout(monkeypatch, tmp_path):
    cleanup = tmp_path / "launcher.sh"
    cleanup.write_text("x", encoding="utf-8")
    killed: list[object] = []

    class _Proc:
        returncode = None
        pid = None

        async def communicate(self, _input):
            await asyncio.Event().wait()

    async def fake_prepare(argv, mode, *, env, strip_python_env):
        return (argv, {}, str(cleanup))

    async def fake_spawn(*argv, **kwargs):
        return _Proc()

    async def fake_kill(proc, *, timeout=None):
        killed.append(proc)

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv_async", fake_prepare)
    monkeypatch.setattr("kiro_crew.sandbox.create_subprocess_limited", fake_spawn)
    monkeypatch.setattr("kiro_crew.platform_compat.kill_and_reap", fake_kill)
    with pytest.raises(subprocess.TimeoutExpired):
        # The child never answers (its communicate waits on an Event nobody sets), so
        # this deadline always expires: its length is not what the test depends on.
        expiring = sdk.run_sandboxed_async(["x"], timeout=0.01)  # flake-ok: always expires
        asyncio.run(asyncio.wait_for(expiring, timeout=FALLBACK_TIMEOUT_SECS))
    assert len(killed) == 1
    assert not cleanup.exists()


# ── process helpers ─────────────────────────────────────────────────────────


def test_platform_flags_are_the_gateway_flags() -> None:
    assert sdk.IS_WINDOWS is platform_compat.IS_WINDOWS
    assert sdk.IS_MACOS is platform_compat.IS_MACOS
    assert sdk.IS_LINUX is platform_compat.IS_LINUX
    assert sdk.IS_POSIX is platform_compat.IS_POSIX


def test_link_helpers_see_a_link_and_its_ancestor(tmp_path) -> None:
    from kiro_crew.testing.links import make_dir_link

    real = tmp_path / "real"
    (real / "child").mkdir(parents=True)
    link = tmp_path / "link"
    make_dir_link(link, real)
    assert sdk.is_link_or_junction(link) is True
    assert sdk.is_link_or_junction(real) is False
    assert sdk.first_linked_ancestor(link / "child") == str(link)
    assert sdk.first_linked_ancestor(real / "child") is None


def test_rmtree_force_removes_a_tree(tmp_path) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("x", encoding="utf-8")
    assert sdk.rmtree_force(tree) is True
    assert not tree.exists()


def test_file_lock_holds_an_advisory_lock(tmp_path) -> None:
    lock_path = tmp_path / "app.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with sdk.file_lock(fd, timeout=FALLBACK_TIMEOUT_SECS):
            entered = True
    finally:
        os.close(fd)
    assert entered


def test_kill_and_reap_delegates(monkeypatch) -> None:
    seen: list[tuple] = []

    async def fake(proc, *, timeout=None):
        seen.append((proc, timeout))

    monkeypatch.setattr("kiro_crew.platform_compat.kill_and_reap", fake)
    reap = sdk.kill_and_reap("p", timeout=FALLBACK_TIMEOUT_SECS)
    asyncio.run(asyncio.wait_for(reap, timeout=FALLBACK_TIMEOUT_SECS))
    assert seen == [("p", FALLBACK_TIMEOUT_SECS)]
