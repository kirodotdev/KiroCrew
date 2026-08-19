"""The apps-dir walk and the instances-registry writes stay off the event loop.

``list_apps()`` walks the apps directory and reads two files per installed app;
``InstancesRegistry``'s write methods each read then atomically rewrite
``instances.json`` with an fsync. Neither belongs on the asyncio event loop:
the loop runs callbacks one at a time, so a synchronous filesystem call inside
an ``async def`` delays every other task (including the watchdog heartbeat) for
its duration. These are bounded, latency-class costs — not stall-class — but
the repo convention is to offload them via ``asyncio.to_thread``.

Two enforcement tiers:

- Behavior tests: drive each async entry point with a recording double and
  assert the filesystem-touching call executed on a worker thread, not the
  loop thread. Reverting an offload makes the recorded thread equal the loop
  thread and fails the test.
- AST ratchet: no direct (non-offloaded) call of the walk / registry-write
  functions may appear lexically inside an ``async def`` frame of the owning
  modules, so a future call site cannot silently regress. A nested ``def`` /
  ``lambda`` is a separate frame (the offloaded callable itself) and is not
  scanned, matching the scoping of ``test_no_blocking_call_on_loop.py``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast

import pytest
from aiohttp.test_utils import make_mocked_request

import kiro_crew.apps.bridges as bridges_mod
import kiro_crew.apps.hooks_integration as hooks_mod
import kiro_crew.apps.routes as routes_mod
import kiro_crew.dashboard.handlers_instances as handlers_instances_mod
import kiro_crew.instances.ssh_tunnel_manager as ssh_tunnel_manager_mod
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recorder(record: list[threading.Thread], result: Any = None) -> Callable[..., Any]:
    """A stand-in that records the thread it runs on and returns *result*."""

    def _fn(*args: Any, **kwargs: Any) -> Any:
        record.append(threading.current_thread())
        return result

    return _fn


class _RecordingRegistry:
    """Duck-typed InstancesRegistry double recording the thread of each write."""

    def __init__(self) -> None:
        self.write_threads: list[threading.Thread] = []

    def list(self) -> list[Any]:
        return []

    def get(self, instance_id: str) -> Any:
        return None

    def update(self, instance_id: str, **changes: object) -> Any:
        self.write_threads.append(threading.current_thread())
        return SimpleNamespace(id=instance_id)


def _tunnel_double(instance_id: str, *, pid: int = 4321, local_port: int = 18022) -> Any:
    """Stand-in for ``_SshTunnel`` carrying every attribute ``_mark_recovered`` reads.

    ``_SshTunnel.__init__`` assigns ``self.status`` (a real ``TunnelStatus``)
    unconditionally, before any I/O, so a double that omits it can only produce
    failures the production object cannot — and ``_mark_recovered`` does read
    ``tunnel.status.local_port`` when it refreshes the forwarder identity.
    """
    return SimpleNamespace(
        pid=pid, status=TunnelStatus(instance_id=instance_id, local_port=local_port)
    )


def _pin_forwarder_identity(monkeypatch: pytest.MonkeyPatch, *, reachable: bool) -> None:
    """Pin whether ``_mark_recovered`` takes its forwarder-identity branch.

    Unpinned, the branch is gated on ``process_start_time(pid)`` being truthy,
    which holds iff that pid names a live, queryable process on the machine
    running the test. That makes the code path a function of runner state rather
    than of the test, so each test below pins the branch it means to exercise.
    """
    monkeypatch.setattr(
        ssh_tunnel_manager_mod.platform_compat,
        "process_start_time",
        lambda pid: "pinned-start-time" if reachable else None,
    )
    if reachable:
        monkeypatch.setattr(ssh_tunnel_manager_mod, "_reclaim_identity_key", lambda: b"k" * 32)


# ---------------------------------------------------------------------------
# Behavior: the walk / write happens on a worker thread, not the loop thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_list_apps_walks_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/apps must not run the apps-dir walk on the loop thread."""
    loop_thread = threading.current_thread()
    walk_threads: list[threading.Thread] = []
    monkeypatch.setattr(routes_mod, "list_apps", _recorder(walk_threads, result=[]))
    monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])

    resp = await routes_mod.handle_list_apps(make_mocked_request("GET", "/api/apps"))

    assert resp.status == 200
    assert walk_threads, "list_apps was never invoked"
    assert all(t is not loop_thread for t in walk_threads), (
        "the apps-dir walk ran synchronously on the event loop thread"
    )


@pytest.mark.asyncio
async def test_handle_publish_providers_collects_off_the_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish-provider collection (walk + per-provider config reads)
    must not run on the loop thread."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    loop_thread = threading.current_thread()
    walk_threads: list[threading.Thread] = []
    monkeypatch.setattr(routes_mod, "list_apps", _recorder(walk_threads, result=[]))

    resp = await routes_mod.handle_publish_providers(
        make_mocked_request("GET", "/api/publish-providers")
    )

    assert resp.status == 200
    assert walk_threads, "list_apps was never invoked"
    assert all(t is not loop_thread for t in walk_threads), (
        "the publish-provider collection ran synchronously on the event loop thread"
    )


@pytest.mark.asyncio
async def test_gateway_startup_lists_apps_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.current_thread()
    walk_threads: list[threading.Thread] = []
    # Truthy dispatcher so the function proceeds past its early return; the
    # empty app list means it never actually dispatches into it.
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", SimpleNamespace())
    monkeypatch.setattr(hooks_mod, "list_apps", _recorder(walk_threads, result=[]))

    await hooks_mod.on_gateway_startup()

    assert walk_threads, "list_apps was never invoked"
    assert all(t is not loop_thread for t in walk_threads)


@pytest.mark.asyncio
async def test_gateway_shutdown_lists_apps_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.current_thread()
    walk_threads: list[threading.Thread] = []
    monkeypatch.setattr(hooks_mod, "_lifecycle_dispatcher", SimpleNamespace())
    monkeypatch.setattr(hooks_mod, "list_apps", _recorder(walk_threads, result=[]))

    await hooks_mod.on_gateway_shutdown()

    assert walk_threads, "list_apps was never invoked"
    assert all(t is not loop_thread for t in walk_threads)


@pytest.mark.asyncio
async def test_disconnect_registry_write_off_the_loop_thread() -> None:
    """disconnect() persists the port/hint reset even with no live tunnel, and
    that read-modify-rewrite of instances.json must run on a worker thread."""
    loop_thread = threading.current_thread()
    registry = _RecordingRegistry()
    mgr = SshTunnelManager(registry)  # type: ignore[arg-type]

    existed = await mgr.disconnect("no-such-instance")

    assert existed is False
    assert registry.write_threads, "the registry cleanup write never happened"
    assert all(t is not loop_thread for t in registry.write_threads), (
        "instances.json was rewritten synchronously on the event loop thread"
    )


@pytest.mark.asyncio
async def test_mark_recovered_registry_write_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked instance's recovery hint is persisted, on a worker thread."""
    loop_thread = threading.current_thread()
    registry = _RecordingRegistry()
    mgr = SshTunnelManager(registry)  # type: ignore[arg-type]
    # The double carries a pid: _mark_recovered persists the rebuilt child's
    # forwarder_pid alongside was_connected in its single hint write.
    mgr._tunnels["some-instance"] = _tunnel_double("some-instance")
    # Pin the forwarder-identity branch REACHABLE: it is the widest route to the
    # hint write, and the only one that reads tunnel.status.local_port, so the
    # offload assertion below covers the whole write path on every platform.
    _pin_forwarder_identity(monkeypatch, reachable=True)

    await mgr._mark_recovered("some-instance")

    assert registry.write_threads, "the recovery hint write never happened"
    assert all(t is not loop_thread for t in registry.write_threads)


@pytest.mark.asyncio
async def test_mark_recovered_skips_the_write_for_an_untracked_instance() -> None:
    """After a disconnect pops the tunnel, a late recovery must NOT re-mark the
    instance auto-reconnectable — the persist is gated on still being tracked
    (and runs under the manager lock, so write order equals lock order)."""
    registry = _RecordingRegistry()
    mgr = SshTunnelManager(registry)  # type: ignore[arg-type]

    await mgr._mark_recovered("disconnected-instance")

    assert registry.write_threads == [], (
        "recovery persisted was_connected=True for an instance no longer tracked"
    )


class _BlockingRegistry(_RecordingRegistry):
    """Registry double whose write blocks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.completed = False

    def update(self, instance_id: str, **changes: object) -> Any:
        self.release.wait(timeout=10)
        self.completed = True
        return super().update(instance_id, **changes)


@pytest.mark.asyncio
async def test_cancelled_persist_waits_for_the_worker_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a caller mid-persist must NOT unwind the frame (and release
    the manager lock) while the worker write is still running — a cancelled
    to_thread await does not stop the thread, so an early unwind would let the
    late write race a subsequent locked write (e.g. a disconnect's reset)."""
    registry = _BlockingRegistry()
    mgr = SshTunnelManager(registry)  # type: ignore[arg-type]
    mgr._tunnels["inst"] = _tunnel_double("inst")
    # Pin the forwarder-identity branch UNREACHABLE: the subject here is the
    # cancellation window around the BLOCKED registry write, so this wants the
    # shortest deterministic route to it. Taking the identity branch instead
    # would add two more to_thread hops ahead of that write, narrowing the
    # sleep window below for no gain — test 1 above covers that branch.
    _pin_forwarder_identity(monkeypatch, reachable=False)

    task = asyncio.create_task(mgr._mark_recovered("inst"))
    await asyncio.sleep(0.05)  # the worker write is submitted and blocked
    task.cancel()
    await asyncio.sleep(0.05)  # cancellation delivered

    # The frame must still be waiting on the in-flight worker write.
    assert not task.done(), (
        "the coroutine unwound (releasing the manager lock) while the worker "
        "write was still running"
    )
    assert mgr._lock.locked(), "the manager lock was released mid-write"

    registry.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.completed, "the worker write did not complete before unwind"
    assert not mgr._lock.locked()


def test_list_apps_is_read_only_under_version_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_apps() must not write installed.json back: it now runs concurrently
    from worker threads, and a persisted read-modify-write from a listing
    races real mutators (install/enable/register) and silently overwrites
    their fields. The manifest version is still reflected in the RETURNED
    metadata (display), just not persisted from this path."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    import json as _json

    from kiro_crew.apps.manager import InstalledApp, _write_installed, apps_dir, list_apps

    app_root = apps_dir() / "drift-app"
    app_root.mkdir(parents=True)
    (app_root / "app.json").write_text(
        _json.dumps({"name": "drift-app", "version": "2.0.0", "description": "d"}),
        encoding="utf-8",
    )
    _write_installed("drift-app", InstalledApp(name="drift-app", version="1.0.0", lifecycle="app"))
    installed_path = app_root / "installed.json"
    before = installed_path.read_text(encoding="utf-8")

    rows = list_apps()

    entry = next(a for a in rows if a["name"] == "drift-app")
    assert entry["version"] == "2.0.0", "manifest version not reflected in the listing"
    assert installed_path.read_text(encoding="utf-8") == before, (
        "list_apps() wrote installed.json — the listing must be read-only"
    )


# ---------------------------------------------------------------------------
# AST ratchet: no direct sync call of the walk / registry writes in async frames
# ---------------------------------------------------------------------------

# Scopes not descended into from an async def body: a nested function or lambda
# is a different execution frame (typically the offloaded callable itself).
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _calls_in_async_frames(tree: ast.AST) -> list[tuple[str, ast.Call]]:
    """All Call nodes lexically inside async def bodies (own frame only)."""
    found: list[tuple[str, ast.Call]] = []

    def _scan(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED_SCOPES):
                continue
            if isinstance(child, ast.Call):
                found.append((owner, child))
            _scan(child, owner)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for stmt in node.body:
                _scan(stmt, node.name)
    return found


def _module_tree(module: Any) -> ast.AST:
    return ast.parse(inspect.getsource(module))


@pytest.mark.parametrize(
    "module,banned_names",
    [
        # _provider_is_configured (not the pure collect_publish_providers) is
        # the disk-touching primitive behind the publish-provider collection: a
        # future async call with an injected non-disk resolver must not trip
        # this gate, while the default resolver's file reads must.
        # The registry config helper performs a locked read-modify-atomic-write.
        # Besides filesystem latency, the Windows rename retry is intentionally
        # available only when no event loop is running in the calling thread.
        (
            routes_mod,
            {
                "list_apps",
                "_provider_is_configured",
                "_write_registries_config",
            },
        ),
        (hooks_mod, {"list_apps"}),
        (bridges_mod, {"list_apps"}),
    ],
)
def test_no_direct_apps_walk_in_async_frames(module: Any, banned_names: set[str]) -> None:
    """The apps-dir walk may not be called directly inside an async frame; it
    must be handed to asyncio.to_thread as a callable (Name, not Call). Both
    the bare-name form (``list_apps()``) and the attribute form
    (``manager.list_apps()``) are matched, so re-importing the function under a
    module prefix does not evade the gate."""
    offenders: list[str] = []
    for owner, call in _calls_in_async_frames(_module_tree(module)):
        func = call.func
        name = ""
        if isinstance(func, ast.Name) and func.id in banned_names:
            name = func.id
        elif isinstance(func, ast.Attribute) and func.attr in banned_names:
            name = func.attr
        if name:
            offenders.append(f"{module.__name__}:{owner}:{call.lineno} calls {name}()")
    assert not offenders, "direct apps-dir walk on the event loop:\n" + "\n".join(offenders)


def test_no_direct_registry_write_in_async_frames() -> None:
    """InstancesRegistry methods (writes are a read + atomic rewrite + fsync of
    instances.json; reads still block in the registry's threading lock while a
    worker holds it across an fsync) may not be called directly inside an
    async frame of the tunnel manager. Sync helpers (e.g. _reserved_ports) are
    separate frames and stay out of scope."""
    import kiro_crew.instances.ssh_tunnel_manager as stm_mod

    write_methods = {
        "update",
        "remove",
        "get",
        "list",
        "get_last_active",
    }
    offenders: list[str] = []
    for owner, call in _calls_in_async_frames(_module_tree(stm_mod)):
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in write_methods
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "_registry"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
        ):
            offenders.append(f"{owner}:{call.lineno} calls self._registry.{func.attr}()")
    assert not offenders, "direct registry write on the event loop:\n" + "\n".join(offenders)


def test_no_direct_registry_call_in_instances_handler_async_frames() -> None:
    """The instances dashboard handlers reach the registry as ``reg.<method>``
    or ``_registry(state).<method>``; every registry touch there (reads
    included — a read blocks in the registry's threading lock while a worker
    holds it across an fsync) must be offloaded, never called directly inside
    an async frame."""
    registry_methods = {
        "list",
        "get",
        "get_last_active",
        "add",
        "update",
        "remove",
    }
    offenders: list[str] = []
    for owner, call in _calls_in_async_frames(_module_tree(handlers_instances_mod)):
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr in registry_methods):
            continue
        receiver = func.value
        is_reg_name = isinstance(receiver, ast.Name) and receiver.id == "reg"
        is_registry_call = (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "_registry"
        )
        if is_reg_name or is_registry_call:
            offenders.append(f"{owner}:{call.lineno} calls registry .{func.attr}() on the loop")
    assert not offenders, "direct registry call on the event loop:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# App config writes (#4548's class, one module over)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registries_config_write_runs_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real PUT handler hands the locked config mutation to a worker."""
    loop_thread = threading.current_thread()
    write_threads: list[threading.Thread] = []
    audits: list[dict[str, Any]] = []

    def _write(_cfg: Path, _validated: list[dict[str, str]]) -> list[tuple[str, str]]:
        write_threads.append(threading.current_thread())
        return [("github.com", "https://github.com/example/apps.git")]

    async def _json() -> dict[str, object]:
        return {"registries": [{"name": "example", "repo": "https://github.com/example/apps.git"}]}

    monkeypatch.setattr(routes_mod, "_write_registries_config", _write)
    monkeypatch.setattr(routes_mod, "_pinned_registries", lambda: [])
    monkeypatch.setattr(
        routes_mod,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kwargs: audits.append(kwargs)),
    )

    request = cast(Any, SimpleNamespace(method="PUT", json=_json))
    response = await routes_mod.handle_registries(request)

    assert response.status == 200
    assert write_threads, "the registry config write was never invoked"
    assert all(thread is not loop_thread for thread in write_threads)
    assert any(event.get("operation") == "registries.host_trust_granted" for event in audits)


@pytest.mark.asyncio
async def test_failed_registries_config_write_does_not_audit_a_trust_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write cannot produce a trust event for state that never landed."""
    audits: list[dict[str, Any]] = []

    def _write(_cfg: Path, _validated: list[dict[str, str]]) -> list[tuple[str, str]]:
        raise OSError("sharing violation")

    async def _json() -> dict[str, object]:
        return {"registries": [{"repo": "https://github.com/example/apps.git"}]}

    monkeypatch.setattr(routes_mod, "_write_registries_config", _write)
    monkeypatch.setattr(routes_mod, "_pinned_registries", lambda: [])
    monkeypatch.setattr(
        routes_mod,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kwargs: audits.append(kwargs)),
    )

    request = cast(Any, SimpleNamespace(method="PUT", json=_json))
    response = await routes_mod.handle_registries(request)

    assert response.status == 500
    assert json.loads(response.text)["code"] == "registries_config_write_failed"
    assert all(event.get("operation") != "registries.host_trust_granted" for event in audits)


def test_registries_config_write_uses_the_locked_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The worker computes host grants from the state protected by the lock."""
    observed: dict[str, object] = {}

    def _update_config_locked(path: Path, *, mutate: Callable[[dict], dict | None]) -> dict:
        data = {
            "unrelated": {"keep": True},
            "registries": [{"repo": "https://github.com/existing/apps.git"}],
        }
        result = mutate(data)
        assert result is not None
        observed["path"] = path
        observed["data"] = result
        return result

    monkeypatch.setattr(routes_mod, "update_config_locked", _update_config_locked)
    validated = [
        {
            "name": "same-host",
            "repo": "https://github.com/other/apps.git",
            "branch": "main",
            "trust": "index",
        },
        {
            "name": "new-host",
            "repo": "https://gitlab.com/new/apps.git",
            "branch": "main",
            "trust": "index",
        },
        {
            "name": "same-new-host",
            "repo": "https://gitlab.com/new/other.git",
            "branch": "main",
            "trust": "index",
        },
    ]
    cfg = tmp_path / "config.json"

    grants = routes_mod._write_registries_config(cfg, validated)

    assert observed["path"] == cfg
    assert observed["data"] == {
        "unrelated": {"keep": True},
        "registries": validated,
    }
    assert grants == [("gitlab.com", "https://gitlab.com/new/apps.git")]


@pytest.mark.asyncio
async def test_registries_config_write_holds_the_loop_side_config_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry PUT must serialize against the asyncio-lock-only writers.

    ``config.json`` has two writer generations and they do not exclude each
    other. ``_write_registries_config`` reaches the file through
    ``update_config_locked``, which takes the sidecar advisory flock ONLY; the
    dashboard's legacy writers (agents endpoint, ``updates.py``, ``security.py``,
    ``messaging.py``, ``mcp.py``, ``core.py`` STT) take ``_get_config_lock``
    ONLY. Dispatching this write with a bare ``asyncio.to_thread`` therefore
    holds nothing the legacy family respects, and a concurrent PUT there can
    commit a snapshot taken before this write and silently revert it.

    ``run_config_write`` is the one entry point that holds BOTH, so the loop-side
    lock must be held while the worker runs. Reverting the dispatch to
    ``asyncio.to_thread`` makes ``locked()`` observe ``False`` and fails here.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    lock_held: list[bool] = []
    write_threads: list[threading.Thread] = []
    loop_thread = threading.current_thread()

    def _write(_cfg: Path, _validated: list[dict[str, str]]) -> list[tuple[str, str]]:
        # Probed from the worker thread. ``LoopBoundLock.locked()`` outside a
        # running loop reports whether ANY live loop holds it, which is exactly
        # the question here.
        lock_held.append(_get_config_lock().locked())
        write_threads.append(threading.current_thread())
        return [("github.com", "https://github.com/example/apps.git")]

    async def _json() -> dict[str, object]:
        return {"registries": [{"name": "example", "repo": "https://github.com/example/apps.git"}]}

    monkeypatch.setattr(routes_mod, "_write_registries_config", _write)
    monkeypatch.setattr(routes_mod, "_pinned_registries", lambda: [])
    monkeypatch.setattr(
        routes_mod,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kwargs: None),
    )

    assert not _get_config_lock().locked(), "precondition: the config lock starts free"

    request = cast(Any, SimpleNamespace(method="PUT", json=_json))
    response = await routes_mod.handle_registries(request)

    assert response.status == 200
    assert lock_held == [True], "the loop-side config lock was not held across the write"
    # The lock must not outlive the request, or the next config writer deadlocks.
    assert not _get_config_lock().locked()
    # And the fix must not have pulled the blocking work back onto the loop.
    assert write_threads and all(thread is not loop_thread for thread in write_threads)


def test_registries_config_write_is_not_dispatched_with_a_bare_to_thread() -> None:
    """``_write_registries_config`` may not be handed straight to a raw offload.

    The behavioral test above proves the lock is held on the path it drives;
    this ratchet stops a SECOND dispatch site reintroducing the bare form
    somewhere that test does not reach. ``asyncio.to_thread`` is legitimate
    throughout this module for work that touches no shared config file, so the
    scan is deliberately narrow: it fires only when the callable being offloaded
    is ``_write_registries_config`` itself.
    """
    offenders: list[str] = []
    for owner, call in _calls_in_async_frames(_module_tree(routes_mod)):
        func = call.func
        is_to_thread = (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
        if not is_to_thread or not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Name) and first.id == "_write_registries_config":
            offenders.append(
                f"{owner}:{call.lineno} offloads _write_registries_config with a bare "
                "asyncio.to_thread; use dashboard.chat_utils.run_config_write so the "
                "loop-side config lock is held too"
            )
    assert not offenders, "config write bypasses the loop-side lock:\n" + "\n".join(offenders)


def test_the_registry_write_ratchet_can_actually_fail() -> None:
    """Self-check: a scan that matches nothing would pass vacuously.

    Feeds the ratchet's own predicate the shape it exists to reject, so a future
    edit that breaks the AST matching (a renamed helper, a changed call form)
    cannot leave the ratchet silently green.
    """
    tree = ast.parse(
        "import asyncio\n"
        "async def handler():\n"
        "    return await asyncio.to_thread(_write_registries_config, cfg, validated)\n"
    )
    matched = [
        call
        for _owner, call in _calls_in_async_frames(tree)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "to_thread"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "_write_registries_config"
    ]
    assert len(matched) == 1, "the ratchet no longer recognises the shape it must reject"
