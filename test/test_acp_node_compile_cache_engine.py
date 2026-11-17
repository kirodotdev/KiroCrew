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
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.acp import node_compile_cache as ncc
from kiro_crew.acp.kas_transport import build_kas_bundle_argv
from kiro_crew.sandbox import detect_backend

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

#: The fake-engine confinement tests need only a Node binary, not the full engine,
#: so they can run in CI (where the real engine is absent) wherever node is on PATH.
_NODE = _ENGINE[0] if _ENGINE is not None else shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="no node on this host")

#: The ``--require`` bootstrap confines Node's POSIX home global folders
#: (``$HOME/.node_modules``, ``$HOME/.node_libraries``), which Node populates from
#: ``$HOME`` only on POSIX (Windows keys them off ``%USERPROFILE%``). The tests also
#: spawn Node with a stripped environment the real Linux OS sandbox provides but a
#: bare Windows process aborts under (no ``SystemRoot``). The prewarm itself never
#: runs on Windows anyway: with no OS sandbox backend it fails closed. So these
#: engine-spawning confinement tests are POSIX-only by construction.
needs_posix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the prewarm and its POSIX home-folder confinement run only on a Linux sandbox host",
)

#: The sandbox-layer test runs the engine's node inside the real OS sandbox, so it
#: needs both the engine (its node sits in the read-only engines leaf the sandbox
#: exposes) and a working backend.
needs_sandbox = pytest.mark.skipif(
    _ENGINE is None or detect_backend() == "none",
    reason="no sandbox backend or no engine on this host",
)


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

    # Exercises the prewarm's engine run + cache relocation + the read-only start
    # with the real engine. The live OS sandbox needs a fully populated data home
    # (the pod has one; this test's tmp home does not), so the sandbox layer is
    # bypassed here with a pass-through and verified live in
    # test_the_sandbox_denies_writes_outside_the_scratch and the pod run.
    def _passthrough(
        argv,
        mode="standard",
        *,
        env=None,
        extra_writable_dirs=(),
        first_party_fixed_argv=False,
        **_kw,
    ):
        return list(argv), dict(env or {}), None

    monkeypatch.setattr("kiro_crew.sandbox_spawn.sandboxed_spawn_argv", _passthrough, raising=True)
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


def _plant_marker_module(node_modules: Path, name: str) -> Path:
    """A ``require(name)`` target whose top-level code writes a marker beside it.

    Stands in for a module a sandboxed agent planted in a parent ``node_modules``:
    if the prewarm loads it, the marker appears.
    """
    pkg = node_modules / name
    pkg.mkdir(parents=True)
    marker = pkg / "LOADED"
    (pkg / "index.js").write_text(
        "require('fs').writeFileSync(require('path').join(__dirname, 'LOADED'), '1');\n"
        "module.exports = {};\n",
        encoding="utf-8",
    )
    return marker


def _fake_main(base: Path, node_requires: list[str]) -> tuple[Path, Path]:
    """A tiny engine laid out like the real one: engines/kas/<v>/bundle/.../acp-server.js.

    It ``require()``s each name in *node_requires* (optional modules the real
    bundle also leaves out) inside try/catch and exits 0, so it starts whether or
    not they resolve. Returns (engine root, main file).
    """
    root = base / "engines" / "kas" / "0.0.0"
    server_dir = root / "bundle" / "node_modules" / "@scope" / "pkg" / "dist" / "server"
    server_dir.mkdir(parents=True)
    main = server_dir / "acp-server.js"
    body = "".join(f"try {{ require('{name}'); }} catch (e) {{}}\n" for name in node_requires)
    main.write_text(body + "process.exit(0);\n", encoding="utf-8")
    return root, main


def _run_engine(node: str, main: Path, bootstrap: Path | None, home: Path) -> None:
    argv = [node]
    if bootstrap is not None:
        argv += ["--require", str(bootstrap)]
    argv.append(str(main))
    subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "HOME": str(home)},
        cwd=str(home),
        timeout=60,  # flake-ok: a hang ceiling for the engine run, not a timing barrier
        check=True,
    )


@needs_node
@needs_posix
def test_the_bootstrap_confines_parent_and_home_modules(tmp_path):
    assert _NODE is not None
    node = _NODE

    def layout(base: Path) -> tuple[Path, Path, list[Path]]:
        root, main = _fake_main(base, ["parentmod", "homemod", "libmod"])
        home = base / "home"
        home.mkdir(parents=True)
        markers = [
            _plant_marker_module(root / "node_modules", "parentmod"),
            _plant_marker_module(home / ".node_modules", "homemod"),
            _plant_marker_module(home / ".node_libraries", "libmod"),
        ]
        return main, home, markers

    # Control: NO bootstrap. A parent node_modules, $HOME/.node_modules and
    # $HOME/.node_libraries are all reachable by Node's default resolution, so
    # every marker appears. This proves the test is honest.
    main, home, markers = layout(tmp_path / "control")
    _run_engine(node, main, None, home)
    assert all(
        m.exists() for m in markers
    ), "a planted module is not reachable without the bootstrap"

    # With the real bootstrap, none of the three agent-writable locations is on
    # the resolution path, so no marker appears and the engine still exits 0.
    boot = tmp_path / "confine.cjs"
    boot.write_text(ncc._CONFINE_BOOTSTRAP, encoding="utf-8")
    main, home, markers = layout(tmp_path / "confined")
    _run_engine(node, main, boot, home)
    assert not any(m.exists() for m in markers), "the bootstrap let a planted module load"


@needs_node
@needs_posix
def test_the_prewarm_does_not_load_a_module_planted_above_the_bundle(tmp_path, monkeypatch):
    assert _NODE is not None
    node = _NODE

    # This test exercises the --require bootstrap (the second layer) with the real
    # node, so it bypasses the OS sandbox (the first layer, covered by
    # test_the_sandbox_denies_writes_outside_the_scratch and the pod run) with a
    # pass-through, and uses a fake engine under a pytest tmp dir the real sandbox
    # would not expose.
    def _passthrough(
        argv,
        mode="standard",
        *,
        env=None,
        extra_writable_dirs=(),
        first_party_fixed_argv=False,
        **_kw,
    ):
        return list(argv), dict(env or {}), None

    monkeypatch.setattr("kiro_crew.sandbox_spawn.sandboxed_spawn_argv", _passthrough, raising=True)
    cache = tmp_path / "engines" / "node-compile" / ncc.ROUTE_ENGINE_BUNDLE
    monkeypatch.setattr(ncc, "node_compile_cache_dir", lambda _route: str(cache), raising=True)
    root, main = _fake_main(tmp_path / "confined", ["parentmod"])
    marker = _plant_marker_module(root / "node_modules", "parentmod")

    ncc.prewarm_engine_bundle_cache([node, str(main)])

    assert not marker.exists(), "the prewarm loaded a module planted above the bundle"


@needs_sandbox
def test_the_sandbox_denies_writes_outside_the_scratch(tmp_path, monkeypatch):
    """The prewarm sandbox lets the engine write only its own scratch; a write to
    the read-only engines cache is denied. A no-sandbox control proves the write
    is otherwise possible."""
    from kiro_crew.config.paths import data_home
    from kiro_crew.sandbox_spawn import sandboxed_spawn_argv

    assert _ENGINE is not None
    node, _server = _ENGINE
    run_parent = Path(data_home()) / "run" / "node-prewarm-test"
    run_parent.mkdir(parents=True, exist_ok=True)
    # The data home is this test's own isolation dir under pytest's basetemp, so a
    # fixed name is unique and pytest removes it with the rest of the run.
    scratch = run_parent / "scratch"
    scratch.mkdir()
    outside_dir = Path(ncc.node_compile_cache_dir(ncc.ROUTE_ENGINE_BUNDLE))
    outside_dir.mkdir(parents=True, exist_ok=True)

    def _script(inside: Path, outside: Path) -> str:
        return (
            "const fs=require('fs');"
            f"try{{fs.writeFileSync({json.dumps(str(outside))},'x')}}catch(e){{}}"
            f"fs.writeFileSync({json.dumps(str(inside))},'x');"
        )

    # Control: no sandbox -> the write outside the scratch succeeds (honest test).
    c_inside, c_outside = scratch / "C_IN", outside_dir / "C_OUT"
    subprocess.run(
        [node, "-e", _script(c_inside, c_outside)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "HOME": str(scratch)},
        cwd=str(scratch),
        timeout=60,  # flake-ok: a hang ceiling for the engine run, not a timing barrier
        check=True,
    )
    assert c_outside.exists(), "the control write outside the scratch did not happen"

    # Sandboxed: the same write outside the scratch is denied; the scratch write works.
    s_inside, s_outside = scratch / "S_IN", outside_dir / "S_OUT"
    wrapped, env, cleanup = sandboxed_spawn_argv(
        [node, "-e", _script(s_inside, s_outside)],
        mode="strict",
        env={"PATH": os.defpath, "HOME": str(scratch)},
        extra_writable_dirs=(str(scratch),),
        first_party_fixed_argv=True,
    )
    try:
        result = subprocess.run(
            wrapped,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(scratch),
            timeout=60,  # flake-ok: a hang ceiling for the engine run, not a timing barrier
            check=False,
        )
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
    # The live strict sandbox needs a fully populated data home to seal (the pod
    # has one). On a bare test home the launcher fail-closes before running node;
    # that printed refusal is the ONLY legitimate skip. Any other outcome means
    # the sandbox ran, so the writable-scratch contract must hold: the scratch
    # write succeeds and the write outside it is denied.
    if b"sandbox: BLOCKED" in (result.stderr or b""):
        pytest.skip("strict sandbox could not seal this home (no populated data home)")
    assert s_inside.exists(), "the sandbox denied the allowed scratch write"
    assert not s_outside.exists(), "the sandbox allowed a write outside the scratch"
