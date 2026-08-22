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
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from aiohttp.test_utils import make_mocked_request

import kiro_crew.apps.bridges as bridges_mod
import kiro_crew.apps.hooks_integration as hooks_mod
import kiro_crew.apps.routes as routes_mod
import kiro_crew.dashboard.handlers_instances as handlers_instances_mod
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

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

    def set_last_active(self, instance_id: str) -> None:
        self.write_threads.append(threading.current_thread())

    def set_was_connected(self, instance_id: str, value: bool) -> None:
        self.write_threads.append(threading.current_thread())


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
async def test_mark_recovered_registry_write_off_the_loop_thread() -> None:
    """A tracked instance's recovery hint is persisted, on a worker thread."""
    loop_thread = threading.current_thread()
    registry = _RecordingRegistry()
    mgr = SshTunnelManager(registry)  # type: ignore[arg-type]
    mgr._tunnels["some-instance"] = SimpleNamespace()  # type: ignore[assignment]

    await mgr._mark_recovered("some-instance")

    assert registry.write_threads, "set_was_connected never happened"
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

    def set_was_connected(self, instance_id: str, value: bool) -> None:
        self.release.wait(timeout=10)
        self.completed = True
        super().set_was_connected(instance_id, value)


@pytest.mark.asyncio
async def test_cancelled_persist_waits_for_the_worker_write() -> None:
    """Cancelling a caller mid-persist must NOT unwind the frame (and release
    the manager lock) while the worker write is still running — a cancelled
    to_thread await does not stop the thread, so an early unwind would let the
    late write race a subsequent locked write (e.g. a disconnect's reset)."""
    registry = _BlockingRegistry()
    mgr = SshTunnelManager(registry)  # type: ignore[arg-type]
    mgr._tunnels["inst"] = SimpleNamespace()  # type: ignore[assignment]

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
        # ``_sync_builtin_config`` and ``_write_registries_config`` each end in a
        # tmp-plus-rename. ``replace_with_retry`` declines to retry a Windows
        # sharing violation while a loop runs in this thread, so calling either
        # inline from a handler gates the retry off exactly where it is needed.
        (
            routes_mod,
            {
                "list_apps",
                "_provider_is_configured",
                "_sync_builtin_config",
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
        "set_last_active",
        "set_was_connected",
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
        "set_last_active",
        "set_was_connected",
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


@pytest.mark.parametrize(
    "handler_name,enabled",
    [("handle_enable_app", True), ("handle_disable_app", False)],
)
@pytest.mark.asyncio
async def test_builtin_config_sync_runs_off_the_loop_thread(
    monkeypatch: pytest.MonkeyPatch, handler_name: str, enabled: bool
) -> None:
    """Enabling or disabling a builtin app must hand the config sync to a worker.

    ``_sync_builtin_config`` ends in a rename. On Windows that raises
    ``PermissionError`` while any other handle is open on either path, and
    ``replace_with_retry`` deliberately re-raises on the first attempt rather
    than sleeping while an event loop runs in this thread. Called inline, the
    retry is unavailable on the one path that needs it.

    This drives the REAL handler: a test that awaited ``asyncio.to_thread``
    itself would prove only that ``to_thread`` uses a worker, and would stay
    green with the production offload reverted.
    """
    loop_thread = threading.current_thread()
    sync_threads: list[threading.Thread] = []
    monkeypatch.setattr(routes_mod, "_sync_builtin_config", _recorder(sync_threads))
    monkeypatch.setattr(routes_mod, "_BUILTIN_SERVICE_APPS", {"demo": ("demo", "demo_attr")})
    monkeypatch.setattr(routes_mod, "BUILTIN_NAMES", set(), raising=False)
    monkeypatch.setattr(routes_mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **k: None))
    monkeypatch.setattr(routes_mod, "get_app", lambda name: {"origin": "builtin", "name": name})
    monkeypatch.setattr(routes_mod, "_unregister_notification_channels", lambda *a, **k: None)

    ok = SimpleNamespace(ok=True, error="", to_dict=lambda: {"ok": True})
    verb = "enable_app" if enabled else "disable_app"
    monkeypatch.setattr(routes_mod, verb, lambda *a, **k: ok)

    request = SimpleNamespace(match_info={"name": "demo"}, app={"state": None})
    await getattr(routes_mod, handler_name)(request)

    assert sync_threads, "the builtin config sync was never invoked"
    assert all(t is not loop_thread for t in sync_threads), (
        "_sync_builtin_config ran synchronously on the event loop thread, "
        "where replace_with_retry declines to retry a Windows sharing violation"
    )


def test_the_builtin_config_rename_uses_the_shared_retry() -> None:
    """The rename inside ``_sync_builtin_config`` goes through
    ``replace_with_retry``, not a bare ``Path.replace``.

    Offloading alone is not enough: on a worker the helper is what actually
    performs the retry, so a bare rename there would still lose the write.
    """
    src = inspect.getsource(routes_mod._sync_builtin_config)
    assert "replace_with_retry(" in src
    assert ".replace(path)" not in src


def test_registries_config_write_is_a_single_offloadable_callable() -> None:
    """``handle_registries`` hands ONE callable to ``asyncio.to_thread``.

    Both the ``mkdir`` and the atomic write are blocking filesystem work, and
    the write's rename needs a thread with no running loop for its retry to
    apply, so they belong in the same worker hop rather than one of each.
    """
    helper = inspect.getsource(routes_mod._write_registries_config)
    assert "mkdir(" in helper and "atomic_write(" in helper

    handler = inspect.getsource(routes_mod.handle_registries)
    assert "to_thread(_write_registries_config" in handler
    assert "atomic_write(" not in handler, "the handler must not write inline"
