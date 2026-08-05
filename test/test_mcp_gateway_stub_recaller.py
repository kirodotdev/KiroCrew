"""Warm-pool caller-repair tests for the MCP stub (recaller path).

Covers the stub side of the warm-pool caller identity fix:

* ``build_register_payload`` resolves the session key via the
  ``CallerContext.from_env`` PID-file ancestor walk when the env var is
  absent (warm-pool mode), and
* ``_recaller_loop`` emits exactly one ``recaller`` frame once the key
  materializes, and exits silently when the bridge tears down first.

The gatewayd side lives in ``test_mcp_gateway_recaller.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.mcp_caller
from kiro_crew.mcp_gateway import stub as stub_mod

# Pin to one xdist worker (requires --dist loadgroup) alongside the other
# mcp_gateway suites.
pytestmark = pytest.mark.xdist_group("mcp_gateway")


# Required argv fields matching the rewriter's generated args.
_REQUIRED_STUB_ARGV_FIELDS = (
    "--server", "fake-mcp",
    "--agent", "kirocrew",
    "--target-command", "/usr/bin/true",
    "--target-args=--foo|bar",
    "--sandbox-mode", "standard",
    "--work-dir", "/tmp",
    "--env", "FOO=bar,BAZ=qux",
    "--auto-approve", "ToolA,ToolB",
    "--approval-mode", "interactive",
    "--channel-id", "C0AUNEY55NV",
    "--socket", "/tmp/gw.sock",
)


def _parse(argv: list[str]) -> argparse.Namespace:
    return stub_mod._parse_args(argv)


# --- session_key resolves from warm-pool PID file when env absent -----------


def test_stub_session_key_from_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm-pool: ``KIROCREW_SESSION_KEY`` is absent at register time, but a
    ``session_pid_<pid>.txt`` exists in the config dir (written once the
    session is claimed). ``build_register_payload`` must resolve the key via
    the ancestor walk (``CallerContext.from_env``) so the Register carries a
    real caller instead of an empty one — otherwise gatewayd stamps
    ``caller=None`` and state-mutating tools break."""
    # Reset the process-lifetime from_env cache so a previously-resolved
    # identity from another test cannot leak in (and monkeypatch teardown
    # reverts whatever this test caches).
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)

    from kiro_crew.config.loader import config_dir

    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    # from_env walks from os.getppid() upward; the immediate ancestor's file
    # is found on the first iteration.
    (cfg / f"session_pid_{os.getppid()}.txt").write_text(
        "dashboard:chat-9-42", encoding="utf-8"
    )

    argv = list(_REQUIRED_STUB_ARGV_FIELDS)
    for i, tok in enumerate(argv):
        if tok == "--channel-id":
            del argv[i : i + 2]
            break
    payload = stub_mod.build_register_payload(_parse(argv))
    assert payload["session_key"] == "dashboard:chat-9-42"
    assert payload["caller"]["session_key"] == "dashboard:chat-9-42"
    assert payload["caller"]["session_type"] == "dashboard"


# --- recaller loop re-registers the caller after a warm-pool claim ----------


class _CapWriter:
    """Minimal StreamWriter double capturing whole JSON frames."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._buf = b""

    def write(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line:
                self.frames.append(json.loads(line.decode("utf-8")))

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
async def test_recaller_loop_sends_frame_when_key_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the session key materializes, the loop emits exactly one
    ``recaller`` frame carrying the resolved caller, then exits."""
    # Reset the from_env cache: without this, the key this test injects would
    # be cached process-wide and poison later tests (and a leaked identity
    # from an earlier test could mask the env var this test sets).
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    monkeypatch.setattr(stub_mod, "_RECALLER_POLL_INTERVAL_SECS", 0.01)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-3-7")

    w = _CapWriter()
    stop = asyncio.Event()
    await asyncio.wait_for(stub_mod._recaller_loop(w, "C_X", stop), timeout=2.0)

    assert len(w.frames) == 1
    frame = w.frames[0]
    assert frame["type"] == "recaller"
    assert frame["session_key"] == "dashboard:chat-3-7"
    assert frame["caller"]["session_key"] == "dashboard:chat-3-7"
    assert frame["caller"]["session_type"] == "dashboard"
    assert frame["channel_id"] == "C_X"


@pytest.mark.asyncio
async def test_recaller_loop_noop_when_stopped_before_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the bridge tears down (stop_event set) before a key appears, the
    loop exits without emitting a frame — no task leak, no bogus caller."""
    # Cache-reset for hygiene (this test never resolves an identity — stop is
    # pre-set — but a leaked cache from another test must not linger either).
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    monkeypatch.setattr(stub_mod, "_RECALLER_POLL_INTERVAL_SECS", 5.0)
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)

    w = _CapWriter()
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(stub_mod._recaller_loop(w, None, stop), timeout=1.0)
    assert w.frames == []


class _CriticalSectionWriter:
    """StreamWriter double that detects a broken write+drain critical section:
    its drain() yields the loop, and it flags if any OTHER write() lands between
    a given writer's write() and its drain() completing. Serializing write+drain
    under the shared lock must prevent that."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._in_flight = False
        self.overlap = False

    def write(self, data: bytes) -> None:
        if self._in_flight:
            # Another writer's write+drain was still in progress.
            self.overlap = True
        for line in data.split(b"\n"):
            if line:
                self.frames.append(json.loads(line.decode("utf-8")))

    async def drain(self) -> None:
        self._in_flight = True
        try:
            await asyncio.sleep(0)  # yield: a concurrent write() would overlap
        finally:
            self._in_flight = False


@pytest.mark.asyncio
async def test_write_frame_serializes_concurrent_writers() -> None:
    """stdin_pump and _recaller_loop share one writer and both await drain().
    Without serialization, one coroutine's write() lands inside another's
    write+drain critical section (the interleave that corrupts the socket
    stream). _write_frame must hold the writer's _mc_write_lock across write+
    drain so the sections never overlap."""
    w = _CriticalSectionWriter()
    setattr(w, "_mc_write_lock", asyncio.Lock())

    async def spam(tag: str) -> None:
        for i in range(20):
            await stub_mod._write_frame(w, {"type": tag, "n": i})

    await asyncio.gather(spam("a"), spam("b"))

    assert not w.overlap, "write+drain critical sections overlapped (not serialized)"
    assert len(w.frames) == 40


@pytest.mark.asyncio
async def test_run_bridge_installs_write_lock() -> None:
    """run_bridge must install a _mc_write_lock on the shared writer so its two
    writer coroutines serialize. Verified by driving a minimal bridge to EOF."""
    reader = asyncio.StreamReader()
    reader.feed_eof()  # immediate EOF -> stdin_pump sends Unregister and exits
    w = _CapWriter()
    stop = asyncio.Event()
    stdin = asyncio.StreamReader()
    stdin.feed_eof()
    await asyncio.wait_for(
        stub_mod.run_bridge(reader, w, stop, stdin=stdin, stdout_writer=w),
        timeout=2.0,
    )
    assert getattr(w, "_mc_write_lock", None) is not None
