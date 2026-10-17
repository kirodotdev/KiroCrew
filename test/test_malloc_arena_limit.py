"""The gateway caps glibc malloc arenas in-process, honouring operator overrides."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import cli, platform_compat
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_M_ARENA_MAX = -8


class _Recorder:
    def __init__(self, result: int = 1) -> None:
        self.calls: list[tuple[int, int]] = []
        self.result = result

    def __call__(self, param: int, value: int) -> int:
        self.calls.append((param, value))
        return self.result


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)


def _one_thread() -> int:
    return 1


def _cap(**kw: object) -> platform_compat.MallocArenaCap:
    kw.setdefault("thread_count", _one_thread)
    return platform_compat.limit_malloc_arenas(**kw)  # type: ignore[arg-type]


def test_default_cap_is_applied(linux: None) -> None:
    rec = _Recorder()
    cap = platform_compat.GATEWAY_MALLOC_ARENA_MAX
    assert _cap(environ={}, mallopt=rec) == (cap, cap, 1)
    assert rec.calls == [(_M_ARENA_MAX, cap)]


@pytest.mark.parametrize("threads", [2, 13, None])
def test_cap_set_after_threads_started_is_not_reported_applied(
    linux: None, threads: int | None
) -> None:
    rec = _Recorder()
    cap = platform_compat.GATEWAY_MALLOC_ARENA_MAX
    result = _cap(environ={}, mallopt=rec, thread_count=lambda: threads)
    assert result == (0, cap, threads)
    assert rec.calls == [(_M_ARENA_MAX, cap)]


@pytest.mark.parametrize(
    "env",
    [
        {"MALLOC_ARENA_MAX": "8"},
        {"GLIBC_TUNABLES": "glibc.malloc.arena_max=6"},
        {"KIROCREW_MALLOC_ARENA_MAX": "0"},
        {"KIROCREW_MALLOC_ARENA_MAX": "-1"},
    ],
)
def test_operator_setting_wins_or_opts_out(linux: None, env: dict[str, str]) -> None:
    rec = _Recorder()
    assert _cap(environ=env, mallopt=rec) == (0, 0, None)
    assert rec.calls == []


def test_override_value_replaces_default(linux: None) -> None:
    rec = _Recorder()
    env = {"KIROCREW_MALLOC_ARENA_MAX": "6"}
    assert _cap(environ=env, mallopt=rec).applied == 6
    assert rec.calls == [(_M_ARENA_MAX, 6)]


def test_garbage_override_falls_back_to_default(linux: None) -> None:
    rec = _Recorder()
    env = {"KIROCREW_MALLOC_ARENA_MAX": "lots"}
    assert _cap(environ=env, mallopt=rec).applied == platform_compat.GATEWAY_MALLOC_ARENA_MAX


def test_rejected_or_raising_mallopt_is_harmless(linux: None) -> None:
    assert _cap(environ={}, mallopt=_Recorder(0)) == (0, 0, 1)

    def boom(_p: int, _v: int) -> int:
        raise OSError("no libc")

    assert _cap(environ={}, mallopt=boom) == (0, 0, None)


def test_no_glibc_is_a_noop(linux: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "_glibc_mallopt", lambda: None)
    assert _cap(environ={}) == (0, 0, None)


def test_non_linux_never_touches_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    rec = _Recorder()
    assert _cap(environ={}, mallopt=rec) == (0, 0, None)
    assert rec.calls == []


@pytest.mark.parametrize(
    ("result", "level", "text"),
    [
        (platform_compat.MallocArenaCap(4, 4, 1), logging.INFO, "capped at 4"),
        (platform_compat.MallocArenaCap(0, 4, 13), logging.WARNING, "13 OS threads"),
        (platform_compat.MallocArenaCap(0, 4, None), logging.WARNING, "unknown number"),
    ],
)
def test_log_says_capped_only_when_applied(
    caplog: pytest.LogCaptureFixture,
    result: platform_compat.MallocArenaCap,
    level: int,
    text: str,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.platform_compat"):
        platform_compat.log_malloc_arena_cap(result)
    [record] = caplog.records
    assert record.levelno == level
    assert text in record.getMessage()
    assert ("capped at" in record.getMessage()) == bool(result.applied)


def test_nothing_requested_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.platform_compat"):
        platform_compat.log_malloc_arena_cap(platform_compat.MallocArenaCap())
    assert caplog.records == []


def _run_child(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "MALLOC_ARENA_MAX"}
    env["KIROCREW_HOME"] = str(tmp_path / "home")
    env.pop(platform_compat.MALLOC_ARENA_MAX_ENV, None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        check=False,
        timeout=120,
        cwd=tmp_path,
        env=env,
        **UTF8_TEXT,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="glibc arenas are Linux-only")
def test_gateway_entry_is_single_threaded_at_the_cap(tmp_path: Path) -> None:
    """The real ``kirocrew gateway`` entry reaches the cap with no extra thread.

    glibc only honours the cap while the process is single-threaded, so any
    import or start-up step that spawns a thread above the call silently
    disables it. This runs the real entry point in a child up to the call.
    """
    if platform_compat._glibc_mallopt() is None:
        pytest.skip("not glibc")
    res = _run_child(
        """
        import sys
        from kiro_crew import cli, platform_compat

        real = platform_compat.limit_malloc_arenas

        def spy(**kw):
            result = real(**kw)
            print("RESULT", result.applied, result.requested, result.threads)
            raise SystemExit(0)

        platform_compat.limit_malloc_arenas = spy
        sys.argv = ["kirocrew", "gateway"]
        cli.main()
        """,
        tmp_path,
    )
    assert res.returncode == 0, res.stderr
    [line] = [ln for ln in res.stdout.splitlines() if ln.startswith("RESULT ")]
    cap = platform_compat.GATEWAY_MALLOC_ARENA_MAX
    assert line.split()[1:] == [str(cap), str(cap), "1"], res.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="glibc arenas are Linux-only")
def test_real_glibc_late_cap_is_not_reported_applied(tmp_path: Path) -> None:
    """Set after 12 allocating threads, the cap does not hold and is not claimed."""
    if platform_compat._glibc_mallopt() is None:
        pytest.skip("not glibc")
    res = _run_child(
        """
        import threading
        from kiro_crew import platform_compat

        keep, stop = [], threading.Event()
        go = threading.Barrier(13)

        def work():
            keep.append([bytearray(4096) for _ in range(2000)])
            go.wait()
            stop.wait()

        ts = [threading.Thread(target=work) for _ in range(12)]
        [t.start() for t in ts]
        go.wait()
        r = platform_compat.limit_malloc_arenas(environ={})
        stop.set()
        [t.join() for t in ts]
        print("RESULT", r.applied, r.requested, r.threads)
        """,
        tmp_path,
    )
    assert res.returncode == 0, res.stderr
    [line] = [ln for ln in res.stdout.splitlines() if ln.startswith("RESULT ")]
    cap = platform_compat.GATEWAY_MALLOC_ARENA_MAX
    assert line.split()[1:] == ["0", str(cap), "13"], res.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="glibc arenas are Linux-only")
def test_real_glibc_caps_arena_count(tmp_path: Path) -> None:
    """End to end in a child: 16 allocating threads stay within the cap."""
    if platform_compat._glibc_mallopt() is None:
        pytest.skip("not glibc")
    code = textwrap.dedent("""
        import os, threading
        from kiro_crew import platform_compat
        os.environ.pop("MALLOC_ARENA_MAX", None)
        cap = platform_compat.limit_malloc_arenas(environ={}).applied
        assert cap == platform_compat.GATEWAY_MALLOC_ARENA_MAX
        print(cap)
        go = threading.Barrier(16)
        keep = []
        def work():
            go.wait()
            keep.append([bytearray(4096) for _ in range(2000)])
        ts = [threading.Thread(target=work) for _ in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        heap = 64 * 1024 * 1024
        n = 0
        for line in open("/proc/self/maps"):
            parts = line.split()
            lo, hi = (int(x, 16) for x in parts[0].split("-"))
            if len(parts) == 5 and lo % heap == 0 and hi - lo <= heap:
                n += 1
        print(n)
        """)
    env = {k: v for k, v in __import__("os").environ.items() if k != "MALLOC_ARENA_MAX"}
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        timeout=120,
        cwd=tmp_path,
        env=env,
        **UTF8_TEXT,
    )
    assert res.returncode == 0, res.stderr
    # Main arena lives in brk; at most cap-1 non-main arenas may exist, each
    # starting on a fresh 64 MiB-aligned heap. Uncapped this is 16.
    cap, heaps = (int(x) for x in res.stdout.split()[-2:])
    assert heaps <= cap < 16


def test_gateway_command_caps_arenas_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _Stop(Exception):
        pass

    def fake_limit() -> platform_compat.MallocArenaCap:
        calls.append("limit")
        return platform_compat.MallocArenaCap(2, 2, 1)

    def fake_reexec(_argv: list[str]) -> None:
        calls.append("reexec")
        raise _Stop

    monkeypatch.setattr(cli.platform_compat, "limit_malloc_arenas", fake_limit)
    monkeypatch.setattr(cli, "maybe_reexec", fake_reexec)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "gateway"])
    with pytest.raises(_Stop):
        cli.main()
    assert calls == ["limit", "reexec"]


def test_non_gateway_command_leaves_allocator_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(cli.platform_compat, "limit_malloc_arenas", lambda: called.append(1))
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "--version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert called == []
