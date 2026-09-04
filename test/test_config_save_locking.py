"""``KiroCrewConfig.save()`` holds the sidecar advisory lock (#4767).

``save()`` used to be an UNLOCKED whole-document replace: its rename could land
inside an ``update_config_locked`` holder's read-modify-write (a CLI writer, a
boot refresh, a second gateway), and whichever renamed second published a
document that never saw the other's change — every field in the file exposed,
not one.

Simply adding the lock was tried and REVERTED once (#4371): ``save()`` is a
sync method reached from async handlers, so a contended POSIX ``flock`` inline
on the event loop stalls the whole gateway, and the sidecar lifecycle left
residue a Windows test caught. The fix that landed has two halves, and this
file pins both:

* **HALF 1 — the writer is locked.** ``save()`` writes under the SAME
  ``<config>.lock`` sidecar ``update_config_locked`` holds
  (``TestSaveHoldsTheAdvisoryLock``), and the sidecar lifecycle leaves nothing
  behind but that one shared file (``TestSidecarLifecycle``).
* **HALF 2 — the loop never waits on it.** Every coroutine reaches ``save()``
  through ``dashboard/chat_utils.run_config_write`` or ``asyncio.to_thread``,
  never inline — pinned structurally over the whole tree by
  ``TestNoInlineSaveOnTheEventLoop`` and at runtime for the log-level PUT.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.config import loader as loader_module
from kiro_crew.config.loader import KiroCrewConfig


def _write_config(directory: Path, data: dict) -> Path:
    path = directory / "config.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _read_config(directory: Path) -> dict:
    return json.loads((directory / "config.json").read_text(encoding="utf-8"))


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    """A private config home: every path save() touches derives from config_dir()."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(loader_module, "config_dir", lambda: home)
    return home


class TestSaveHoldsTheAdvisoryLock:
    _TIMEOUT = 30.0

    def test_save_waits_for_a_held_sidecar_lock(self, cfg_home):
        """RED before #4767: save() sailed past a held ``<config>.lock``.

        The holder here is the raw sidecar acquire — byte-for-byte what
        ``update_config_locked`` takes — so the assertion is exactly "save()
        participates in that lock", with no second writer's semantics mixed in.
        """
        _write_config(cfg_home, {"agent": {"log_level": "INFO"}})
        cfg = KiroCrewConfig()

        lock_fd = os.open(str(cfg_home / "config.json.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        saver = threading.Thread(target=cfg.save, daemon=True)
        try:
            with platform_compat.file_lock(lock_fd, exclusive=True):
                saver.start()
                # Poll instead of one fixed sleep: the unfixed save() finishes
                # in milliseconds, so any completion inside the hold is the bug.
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and saver.is_alive():
                    time.sleep(0.05)
                assert saver.is_alive(), (
                    "save() completed while another writer held the sidecar "
                    "advisory lock — it is not participating in the lock"
                )
        finally:
            saver.join(timeout=self._TIMEOUT)
            os.close(lock_fd)
        assert not saver.is_alive(), "save() never completed after the lock was released"
        assert "agent" in _read_config(cfg_home)

    def test_a_locked_writer_landing_inside_save_is_not_discarded(self, cfg_home, monkeypatch):
        """RED before #4767: the interleave the issue describes, end to end.

        A ``save()`` is paused INSIDE its critical window (between building its
        document and its rename landing). An ``update_config_locked`` writer
        starts in that window. Unlocked, the writer's read-modify-write ran to
        completion inside the window and ``save()``'s rename then published a
        document that never saw it — the writer's change silently discarded.
        Locked, the writer blocks until ``save()``'s rename lands, reads THAT
        document, and both changes survive.
        """
        _write_config(cfg_home, {"agent": {"log_level": "INFO"}})
        cfg = KiroCrewConfig()
        cfg.agent.log_level = "DEBUG"  # save()'s own change, must survive

        entered = threading.Event()
        release = threading.Event()
        real_write = loader_module.write_config_atomically
        trapped_once = threading.Event()

        def _trapped_write(path, data, **kwargs):
            # One-shot: pause only save()'s write. The locked writer's write
            # (second call) passes straight through.
            if not trapped_once.is_set():
                trapped_once.set()
                entered.set()
                assert release.wait(self._TIMEOUT), "test orchestration stalled"
            return real_write(path, data, **kwargs)

        monkeypatch.setattr(loader_module, "write_config_atomically", _trapped_write)

        def _locked_writer() -> None:
            def _mutate(current: dict) -> dict:
                current["concurrent_marker"] = "landed"
                return current

            loader_module.update_config_locked(cfg_home / "config.json", mutate=_mutate)

        saver = threading.Thread(target=cfg.save, daemon=True)
        writer = threading.Thread(target=_locked_writer, daemon=True)
        try:
            # Everything from the first start() lives inside this try: any
            # failing orchestration assert must still release the trapped
            # saver and join both threads, or they outlive the test with the
            # monkeypatched writer still referenced (no-test-side-effects).
            saver.start()
            assert entered.wait(self._TIMEOUT), "save() never reached its write"
            writer.start()
            # Give an UNLOCKED writer (the regression) ample time to complete
            # inside save()'s window; a locked one blocks here on the sidecar.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and writer.is_alive():
                time.sleep(0.05)
            assert writer.is_alive(), (
                "update_config_locked completed inside save()'s critical window "
                "— save() is not holding the sidecar lock"
            )
        finally:
            release.set()
            for thread in (saver, writer):
                if thread.ident is not None:
                    thread.join(timeout=self._TIMEOUT)
        assert not saver.is_alive() and not writer.is_alive()

        final = _read_config(cfg_home)
        assert (
            final.get("concurrent_marker") == "landed"
        ), "the locked writer's change was discarded by save()'s rename"
        assert final["agent"]["log_level"] == "DEBUG", "save()'s own change was lost"


class TestSidecarLifecycle:
    """The #4371 regression gate: the lock adds ONE shared sidecar, nothing else.

    On Windows a leftover extra file beside ``config.json`` is exactly what the
    orphan-lockfile test in that round caught. The sidecar itself is shared
    state — ``update_config_locked`` creates and keeps the same one — so the
    contract is: after ``save()``, the directory holds the config and the one
    ``config.json.lock`` that every locked writer reuses, and nothing more.
    """

    def test_save_leaves_only_the_shared_sidecar(self, cfg_home):
        _write_config(cfg_home, {"agent": {}})
        cfg = KiroCrewConfig()
        cfg.save()
        after_save = {p.name for p in cfg_home.iterdir()}
        assert after_save == {
            "config.json",
            "config.json.lock",
        }, "save() left residue beside config.json: %s" % sorted(
            after_save - {"config.json", "config.json.lock"}
        )
        # And it is THE sidecar update_config_locked uses — a second locked
        # writer adds no new file, because they share the lock path.
        loader_module.update_config_locked(cfg_home / "config.json", mutate=lambda d: d)
        assert {p.name for p in cfg_home.iterdir()} == after_save

    def test_save_locks_beside_a_symlinked_configs_target(self, tmp_path, monkeypatch):
        """Same placement rule as update_config_locked: the sidecar follows the
        TARGET the rename replaces, so the two writer families contend on one
        lock even for a symlinked config."""
        home = tmp_path / "home"
        home.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "real-config.json"
        target.write_text(json.dumps({"agent": {}}), encoding="utf-8")
        try:
            (home / "config.json").symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable on this platform/runner")
        monkeypatch.setattr(loader_module, "config_dir", lambda: home)

        KiroCrewConfig().save()

        assert (elsewhere / "real-config.json.lock").exists(), (
            "the sidecar must land beside the resolved target, where "
            "update_config_locked puts its own"
        )
        assert not (home / "config.json.lock").exists(), (
            "a sidecar beside the symlink would not serialize against a writer "
            "that resolved the link first"
        )


def _on_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class TestAsyncCallersOffload:
    @pytest.mark.asyncio
    async def test_log_level_put_persists_off_the_event_loop(self, monkeypatch):
        """The #4371 revert reason, as a runtime probe on a converted handler:
        the persist -- a delta RMW through update_config_locked, whose flock
        wait blocks its thread -- must run in a worker, never inline on the
        loop, and must write only the key this endpoint owns."""
        from kiro_crew.dashboard.handlers import updates

        seen: dict[str, object] = {}

        def _fake_update_config_locked(*args, **kwargs):
            seen["on_loop"] = _on_running_loop()
            doc: dict = {"unrelated": {"key": "untouched"}}
            result = kwargs["mutate"](doc)
            seen["doc"] = result
            return result

        monkeypatch.setattr(updates, "update_config_locked", _fake_update_config_locked)

        class _Req:
            async def json(self):
                return {"level": "DEBUG"}

        resp = await updates.api_log_level(_Req())
        assert resp.status == 200
        assert (
            seen.get("on_loop") is False
        ), "api_log_level ran the locked config RMW inline on the event loop"
        assert seen["doc"]["agent"]["log_level"] == "DEBUG"
        assert seen["doc"]["unrelated"] == {
            "key": "untouched"
        }, "the delta mutate must not touch keys the endpoint does not own"


class TestNoInlineSaveOnTheEventLoop:
    """Structural ratchet over ``src/kiro_crew``: no coroutine calls
    ``KiroCrewConfig.save()`` inline.

    ``save()`` holds the sidecar advisory ``flock`` (#4767); a contended
    acquire blocks its thread, so a coroutine that calls it inline blocks the
    one event loop the gateway shares — the exact failure that reverted #4371.
    The sanctioned shapes are ``await run_config_write(...)`` (preferred, holds
    the loop-side asyncio config lock too) and ``await asyncio.to_thread(...)``
    where a caller already holds that lock. In both, ``.save`` appears as a
    reference handed to the offloader, never as a call inside the coroutine.

    Walks the AST in the shape ``TestEveryConfigWriterIsLocked`` established:
    inside every ``async def``, a call ``X.save()`` is an offender when ``X``
    is bound from ``KiroCrewConfig(...)`` or ``KiroCrewConfig.load(...)`` in
    the same function — or is such a call chained directly. Nested SYNC
    functions are excluded: they execute wherever they are invoked, and the
    sanctioned pattern is precisely to define one and hand it to the offloader.
    """

    @staticmethod
    def _is_kirocrew_config_source(node: ast.AST) -> bool:
        """True for ``KiroCrewConfig(...)`` and ``KiroCrewConfig.load(...)``."""
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "KiroCrewConfig"
        if isinstance(func, ast.Attribute) and func.attr == "load":
            return isinstance(func.value, ast.Name) and func.value.id == "KiroCrewConfig"
        return False

    @classmethod
    def _offenders_in(cls, tree: ast.AST, filename: str) -> list[str]:
        offenders: list[str] = []
        async_funcs = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
        for func in async_funcs:
            config_names: set[str] = set()
            for node in ast.walk(func):
                if isinstance(node, ast.Assign) and cls._is_kirocrew_config_source(node.value):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            config_names.add(tgt.id)

            for node in cls._walk_pruning_sync_defs(func):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (isinstance(f, ast.Attribute) and f.attr == "save"):
                    continue
                recv = f.value
                hit = (isinstance(recv, ast.Name) and recv.id in config_names) or (
                    cls._is_kirocrew_config_source(recv)
                )
                if hit:
                    offenders.append(f"{filename}:{node.lineno} ({func.name})")
        return offenders

    @staticmethod
    def _walk_pruning_sync_defs(func: ast.AsyncFunctionDef):
        """Yield descendants of *func*, skipping nested sync ``def`` bodies."""
        stack: list[ast.AST] = list(ast.iter_child_nodes(func))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef):
                continue
            yield node
            stack.extend(ast.iter_child_nodes(node))

    def test_no_coroutine_calls_save_inline(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            if ".save()" not in src or "KiroCrewConfig" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            offenders.extend(self._offenders_in(tree, path.name))
        assert not offenders, (
            "KiroCrewConfig.save() holds the sidecar advisory flock (#4767); a "
            "coroutine must never call it inline on the event loop. Offload via "
            "dashboard/chat_utils.run_config_write (preferred) or "
            "asyncio.to_thread when the asyncio config lock is already held.\n  "
            + "\n  ".join(sorted(set(offenders)))
        )

    def test_the_ratchet_would_catch_a_reintroduced_inline_save(self):
        """The scan is not vacuous: both offending spellings are detected, and
        the two sanctioned shapes are not."""
        source = (
            "async def offender_via_binding():\n"
            "    cfg = KiroCrewConfig.load()\n"
            "    cfg.save()\n"
            "\n"
            "async def offender_chained():\n"
            "    KiroCrewConfig().save()\n"
            "\n"
            "async def sanctioned_to_thread():\n"
            "    cfg = KiroCrewConfig.load()\n"
            "    await asyncio.to_thread(cfg.save)\n"
            "\n"
            "async def sanctioned_nested_worker():\n"
            "    def _persist():\n"
            "        cfg = KiroCrewConfig.load()\n"
            "        cfg.save()\n"
            "    await run_config_write(_persist)\n"
        )
        offenders = self._offenders_in(ast.parse(source), "synthetic.py")
        names = {o.rsplit("(", 1)[1].rstrip(")") for o in offenders}
        assert names == {"offender_via_binding", "offender_chained"}, offenders
