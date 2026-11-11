"""The real agent engine against a read-only ``NODE_COMPILE_CACHE``.

Runs only where an engine is on disk: Crew's installed bundle, or the
``KIROCREW_TEST_KAS_NODE`` / ``KIROCREW_TEST_KAS_ACP_SERVER`` pair (for example
kiro-cli's own Node and ``acp-server.js``). It checks the two claims the compile
cache rests on, with the engine itself rather than a stand-in:

* the prewarm runs the engine with stdin at EOF, and the engine loads and exits
  on its own, writing the cache;
* an engine pointed at that cache made read-only answers ACP ``initialize`` with
  nothing but JSON-RPC frames on stdout, and exits 0 when stdin closes.
"""

from __future__ import annotations

import json
import os
import queue
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.acp import node_compile_cache as ncc
from kiro_crew.acp.kas_transport import build_kas_bundle_argv

_INIT_TIMEOUT_SECS = 90


def _engine() -> tuple[str, str] | None:
    node = os.environ.get("KIROCREW_TEST_KAS_NODE", "")
    server = os.environ.get("KIROCREW_TEST_KAS_ACP_SERVER", "")
    if node and server:
        return node, server
    try:
        from kiro_crew.acp import kas_bundle

        bundle = kas_bundle.installed_bundle()
    except Exception:  # noqa: BLE001 -- no engine is a skip, not a failure
        return None
    return (str(bundle.node_bin), str(bundle.acp_server)) if bundle else None


_ENGINE = _engine()
needs_engine = pytest.mark.skipif(_ENGINE is None, reason="no agent engine on this host")


def _read_only(path: Path) -> None:
    for entry in [path, *path.rglob("*")]:
        mode = entry.stat().st_mode
        entry.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _writable(path: Path) -> None:
    for entry in [path, *path.rglob("*")]:
        entry.chmod(entry.stat().st_mode | stat.S_IWUSR)


def _initialize(
    argv: list[str], cache: str, home: Path
) -> tuple[dict | None, list[bytes], int | None]:
    env = {"PATH": os.defpath, "HOME": str(home), ncc.NODE_COMPILE_CACHE_ENV: cache}
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=home,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "compile-cache-test", "version": "0"},
        },
    }
    assert proc.stdin is not None and proc.stdout is not None
    stdout = proc.stdout
    # The read runs on a thread so the deadline bounds it: a blocking
    # readline() on the main thread never returns to check the clock.
    lines: queue.Queue[bytes] = queue.Queue()

    def _pump() -> None:
        for line in iter(stdout.readline, b""):
            lines.put(line)

    reader = threading.Thread(target=_pump, daemon=True)
    stray: list[bytes] = []
    reply = None
    code = None
    try:
        proc.stdin.write((json.dumps(request) + "\n").encode())
        proc.stdin.flush()
        reader.start()
        deadline = time.monotonic() + _INIT_TIMEOUT_SECS
        while reply is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = lines.get(
                    timeout=remaining
                )  # flake-ok: the rest of the 90 s upper bound, not a barrier
            except queue.Empty:
                break
            try:
                message = json.loads(line)
            except ValueError:
                stray.append(line[:120])
                continue
            if message.get("id") == 1:
                reply = message
        proc.stdin.close()
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        code = None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        reader.join(timeout=30)
    return reply, stray, code


@needs_engine
def test_the_real_engine_prewarms_and_starts_from_a_read_only_cache(tmp_path, monkeypatch):
    assert _ENGINE is not None
    node, server = _ENGINE
    cache = tmp_path / "engines" / "node-compile" / ncc.ROUTE_ENGINE_BUNDLE
    monkeypatch.setattr(ncc, "node_compile_cache_dir", lambda _route: str(cache), raising=True)
    argv = build_kas_bundle_argv(node, server)

    assert ncc.prewarm_engine_bundle_cache(argv) is True, "the engine did not exit on stdin EOF"
    assert any(entry.is_dir() for entry in cache.iterdir()), "the prewarm wrote no cache"

    home = tmp_path / "home"
    home.mkdir()
    _read_only(cache)
    try:
        before = sorted(p.name for p in cache.rglob("*"))
        reply, stray, code = _initialize(argv, str(cache), home)
        after = sorted(p.name for p in cache.rglob("*"))
    finally:
        _writable(cache)

    assert reply is not None and "result" in reply, "initialize was not answered"
    assert stray == [], "the engine wrote non-JSON lines to stdout"
    assert code == 0
    assert after == before, "the engine changed a read-only cache"
