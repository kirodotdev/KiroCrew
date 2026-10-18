"""Auto-improvement regressions: starting, retargeting and tearing down the app's runs.

Run start-up, retargeting and calibration hold the clone lock; the profile tree stays
off the gateway's import path and the MCP server's launch command resolves on every
platform; a run cannot start twice, and setup, config writes, drafts and commits
recheck the run status inside the lock; watchers start only on opt-in; and a
watcher's teardown keeps work it could not export.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.tests.dogfood_learnings_helpers import (  # noqa: F401
    _cwd_is_the_scratch_dir,
    synthetic_clone_is_safe,
)


@pytest.mark.usefixtures("synthetic_clone_is_safe")
class TestRunStartupSharesTheCloneLock:
    """`POST /run` mutated the shared clone without holding the clone lock.

    `_build_driver` calls `clone_setup.checkout_branch`, which runs `git checkout -B` —
    precisely the operation `clone_lock`'s own docstring names as the race it exists to
    prevent ("another thread's ``checkout -B`` landing between our apply and our commit
    is exactly what merges two findings into one commit"). The draft route holds the lock
    across its whole materialize → commit → draft → rollback sequence; run startup held
    nothing, so a Start click landing mid-draft could move HEAD under it.

    The run-status gate is not a substitute. It stops the DRAFT from racing the loop, but
    a run is not yet "running" while `_build_driver` is still doing git work, so the two
    can overlap in exactly the window that matters. Raised by the GPT review.
    """

    def test_startup_holds_the_lock_while_it_touches_the_clone(self, monkeypatch) -> None:
        """Behavioral: the checkout cannot proceed while the lock is held elsewhere."""
        import threading

        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        checked_out = threading.Event()

        def _fake_checkout(_clone, _branch, **_kw):
            checked_out.set()
            return True, "checked out"

        monkeypatch.setattr(runner_mod.clone_setup, "checkout_branch", _fake_checkout)
        # Stop startup right after the checkout: everything past it (profile build, agent
        # provider probe) is irrelevant to the lock question and needs a real repo. Patched
        # on the PROFILES package because that is where `_build_driver` looks the name up.
        from kiro_crew.apps.builtins.auto_improvement import profiles as profiles_pkg

        monkeypatch.setattr(
            profiles_pkg, "build_profile", lambda _c: (_ for _ in ()).throw(ValueError("stop"))
        )

        sup = runner_mod.RunSupervisor()
        # The worker's exception is the test's signal, not noise: `build_profile` is stubbed
        # to raise once startup gets past the checkout, so a captured `ValueError("stop")`
        # proves the worker took the lock and ran to the stub after we released it. Left
        # uncaught it would only surface as an unhandled-thread warning that never fails.
        outcome: list[BaseException] = []

        def _startup() -> None:
            try:
                sup._build_driver({"clone": "/tmp/x", "branch": "main"})
            except BaseException as exc:  # asserted on below
                outcome.append(exc)

        with commit_mod.clone_lock():
            worker = threading.Thread(target=_startup, daemon=True)
            worker.start()
            # While WE hold the lock the worker must not reach the checkout. A generous
            # window: the failure mode is that it proceeds immediately.
            # flake-ok: pre-existing bounded negative wait proving the worker stays blocked on the held clone lock, moved verbatim; tracked for a de-flake pass
            assert not checked_out.wait(timeout=1.0), (
                "run startup ran `git checkout -B` on the shared clone while another "
                "operator mutation held the clone lock"
            )
        worker.join(timeout=5.0)
        assert not worker.is_alive(), "startup never finished after the lock was released"
        assert checked_out.is_set(), "startup never took the lock it was waiting for"
        assert [type(e) for e in outcome] == [ValueError] and str(
            outcome[0]
        ) == "stop", f"startup stopped somewhere other than the profile-build stub: {outcome!r}"

    def test_the_lock_is_reentrant_so_nesting_cannot_deadlock(self) -> None:
        """`_build_driver` is also reached from `calibrate()`, which may already hold the
        lock; an ordinary Lock would self-deadlock the first time that happened."""
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod

        with commit_mod.clone_lock():
            with commit_mod.clone_lock():
                assert True


class TestTheProfileImportStaysLazy:
    """Review asked to hoist ``from ..profiles import build_profile`` to module scope
    (AUTOSDE ``top-level-imports``). DECLINED, with the cost measured.

    ``runner`` is on the GATEWAY BOOT PATH: ``auto_improvement/__init__`` imports
    ``backend.routes``, which imports ``runner`` at module scope, and the gateway imports
    the package on every startup. Measured in one interpreter: importing ``runner`` pulls
    268 modules; importing the github_repo profile pulls **116 more**. Hoisting therefore
    puts the whole profile + spine tree into every boot — including CLI invocations that
    never start a run — which is precisely what ``profiles.build_profile``'s own docstring
    says the lazy import exists to prevent, and what ``test_perf_boot_path.py`` ratchets.

    The rule is also satisfiable without the regression: the repo's own precedent
    (``docs/system-specs/modules/computer-use.md`` on ``macos_ffi.py``) is that
    ``top-level-imports`` governs where the import STATEMENT sits, and deferring work that
    must not run at module scope is legitimate. There is a real cycle too — the profile
    imports back into ``..backend`` — so a hoist is not a pure no-op.

    Pinned so a future "consistency" cleanup has to read this reasoning first.
    """

    def test_build_profile_is_imported_inside_the_function(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        src = inspect.getsource(runner_mod.RunSupervisor._build_driver)
        assert "from ..profiles import build_profile" in src, (
            "the profile import moved out of the function; if that was deliberate, "
            "re-measure the gateway boot import count first"
        )

    def test_importing_the_backend_does_not_pull_the_profile_tree(self, tmp_path) -> None:
        """The property that actually matters, asserted directly rather than via the
        import statement's position: a fresh interpreter that imports the boot path must
        not have the profile module loaded."""
        import os
        import subprocess
        import sys

        import kiro_crew

        # `github_repo.pr_recipe` IS already on the boot path (routes.py imports it at
        # module scope for the draft route), and that is fine — measured, it is cheap. The
        # expensive one is `github_repo.profile`, which drags the spine in behind it, so
        # that is the module asserted absent rather than the whole package.
        code = (
            "import sys\n"
            "import kiro_crew.apps.builtins.auto_improvement.backend.runner\n"
            "heavy = 'kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile'\n"
            "print(1 if heavy in sys.modules else 0)\n"
        )
        # A CLEAN interpreter, so this measures a real boot rather than whatever the test
        # session has already imported. `kiro_crew` may be on the path via the source tree
        # rather than installed, so its parent is passed through explicitly.
        env = dict(os.environ)
        src_root = str(Path(kiro_crew.__file__).resolve().parent.parent)
        env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        # ``-c`` puts the interpreter's cwd on ``sys.path``; pinned to tmp_path so
        # the measurement cannot be flattered by whatever pytest was launched from.
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
            cwd=tmp_path,
        )
        assert out.returncode == 0, out.stderr[-500:]
        assert out.stdout.strip() == "0", (
            "importing the backend loaded github_repo.profile — that pulls ~116 further "
            "modules (measured) into every gateway boot, which is the regression the lazy "
            "import prevents"
        )


@pytest.mark.usefixtures("synthetic_clone_is_safe")
class TestRetargetIsAtomicWithRunStartup:
    """`POST /setup-clone` checked "is a run live?", then cloned (slow: network + git), then
    persisted the new `clone`/`target_url`. `POST /run` reads config independently. So a
    Start click landing inside the clone window read the OLD config and launched against the
    OLD repository, while the dashboard — reading config after the persist — showed the NEW
    one. The run's artifacts then hang off a different `workspace_key` than the UI displays.

    The busy check cannot close this on its own: it only looks at whether a run is ALREADY
    live, and here the run starts afterwards. Serializing both routes' critical sections on
    the shared clone lock is what makes the pair atomic — setup holds it across
    clone → persist, and run startup already holds it across config-read → driver build.
    Raised by the GPT review.
    """

    def test_setup_holds_the_clone_lock_across_clone_and_persist(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_setup_clone)
        assert "clone_lock()" in src, (
            "setup-clone does not hold the clone lock, so a Start click during the clone "
            "can launch a run against the repository being replaced"
        )
        # The lock must cover BOTH steps. Holding it only around the clone would leave the
        # persist unguarded, which is the half that actually publishes the new target.
        assert "_clone_and_persist" in src, (
            "the clone and the persist must run inside ONE locked section; separate "
            "acquisitions leave the same window open between them"
        )

    def test_run_startup_reads_config_under_the_same_lock(self) -> None:
        """The other half of the pair. A lock held by only one side serializes nothing."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_run_start)
        assert "clone_lock()" in src, (
            "run startup reads config outside the clone lock, so it can capture a config "
            "that setup-clone is midway through replacing"
        )

    def test_the_two_sections_actually_exclude_each_other(self, monkeypatch) -> None:
        """Behavioral, not structural: with the lock held, a would-be run startup must wait."""
        import threading

        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        reached = threading.Event()

        def _mark_reached(*_a, **_k):
            reached.set()
            return True, "ok"

        monkeypatch.setattr(runner_mod.clone_setup, "checkout_branch", _mark_reached)
        from kiro_crew.apps.builtins.auto_improvement import profiles as profiles_pkg

        monkeypatch.setattr(
            profiles_pkg, "build_profile", lambda _c: (_ for _ in ()).throw(ValueError("stop"))
        )

        sup = runner_mod.RunSupervisor()
        with commit_mod.clone_lock():  # stands in for setup-clone's section
            t = threading.Thread(
                target=lambda: sup._build_driver({"clone": "/tmp/x", "branch": "main"}),
                daemon=True,
            )
            t.start()
            # flake-ok: pre-existing bounded negative wait proving the worker stays blocked on the held clone lock, moved verbatim; tracked for a de-flake pass
            assert not reached.wait(
                timeout=1.0
            ), "a run reached the clone while a retarget held the lock"
        t.join(timeout=5.0)


class TestCalibrationHoldsTheCloneLock:
    """`_calibrate_loop` does its OWN `checkout_branch` and never goes through
    `_build_driver`, so the lock added there did not cover it — a claim made when that lock
    landed that turned out to be wrong, and worth recording as such.

    Calibration is the longest clone-holding operation in the app: checkout, then
    `baseline_samples` running the target's whole suite N times. A manual draft mutating the
    same clone underneath it either measures a tree that changed mid-baseline (a ruler
    calibrated against two different revisions) or has its own `checkout -B` land between
    calibration's apply and commit. Raised by the GPT review.
    """

    def test_the_calibrate_loop_body_is_inside_the_lock(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        src = inspect.getsource(runner_mod.RunSupervisor._calibrate_loop)
        assert (
            "clone_lock()" in src
        ), "calibration checks out and measures the shared clone without the lock"

    def test_the_lock_covers_the_measurement_not_just_the_checkout(self) -> None:
        """Locking only the checkout would be worse than useless: it would look correct
        while leaving the long baseline run — the part that actually needs a stable tree —
        exposed. Asserted by position: the lock must be entered BEFORE the checkout and the
        baseline sampling must sit inside it."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        src = inspect.getsource(runner_mod.RunSupervisor._calibrate_loop)
        # Anchor on the CALLS, not on any substring: prose in the surrounding comments names
        # both operations, and a bare `.index("baseline_samples")` matches the comment that
        # explains the lock rather than the measurement it guards. That false positive
        # happened while writing this test.
        lock_at = src.index("with clone_lock():")
        checkout_at = src.index("clone_setup.checkout_branch(")
        baseline_at = src.index(".baseline_samples(")
        assert lock_at < checkout_at, "the lock is taken after the checkout it must guard"
        assert lock_at < baseline_at, "the baseline measurement runs outside the lock"

    def test_calibration_waits_for_a_draft_to_finish(self, monkeypatch) -> None:
        """Behavioral: with the lock held, calibration must not reach the clone."""
        import threading

        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        reached = threading.Event()

        def _mark_reached(*_a, **_k):
            reached.set()
            return True, "ok"

        monkeypatch.setattr(runner_mod.clone_setup, "checkout_branch", _mark_reached)
        from kiro_crew.apps.builtins.auto_improvement import profiles as profiles_pkg

        monkeypatch.setattr(
            profiles_pkg, "build_profile", lambda _c: (_ for _ in ()).throw(ValueError("stop"))
        )

        sup = runner_mod.RunSupervisor()
        with commit_mod.clone_lock():
            t = threading.Thread(
                target=sup._calibrate_loop,
                args=({"clone": "/tmp/x", "branch": "main"},),
                daemon=True,
            )
            t.start()
            # flake-ok: pre-existing bounded negative wait proving the worker stays blocked on the held clone lock, moved verbatim; tracked for a de-flake pass
            assert not reached.wait(
                timeout=1.0
            ), "calibration checked out the shared clone while a draft held the lock"
        t.join(timeout=5.0)
        assert reached.is_set(), "calibration never took the lock it was waiting for"


class TestAFailedExportDoesNotDeleteTheWork:
    """`_run_watcher`'s `finally` deleted the isolated clone unconditionally, while
    `_export_fix` is best-effort — its own docstring says "a failed export is a lost patch".

    Put together, those two are a data-loss bug: the clone's origin is deliberately dead, so
    the patch in the PR queue is the ONLY durable copy of an agent pass's work. If the queue
    write fails (unwritable dir, full disk, a path the sanitizer rejects), the export logs an
    error and returns — and the `finally` then removes the directory holding the commits. A
    completed, verified agent pass is destroyed by a filesystem hiccup with nothing to retry
    from.

    The fix keeps cleanup for the normal case (a disposable clone must not leak) but RETAINS
    the directory when work exists that was never exported, so the next pass or an operator
    can recover it. Raised by the GPT review.
    """

    def test_an_export_failure_retains_the_clone(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        clone = tmp_path / "iso-clone"
        clone.mkdir()
        (clone / "fix.py").write_text("# the agent's work\n", encoding="utf-8")

        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        st.clone = str(clone)

        # An export that cannot write is the whole scenario.
        def _boom(_self, _st, _clone, _attempt):
            raise OSError("read-only file system")

        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", _boom, raising=True)
        # `_log` reaches for the registry lock, which a bare `__new__` instance lacks; the
        # log line is not what this test is about.
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        assert (
            reg._export_is_durable(st, str(clone), 1) is False
        ), "a raising export must not report success"
        assert clone.is_dir(), "the clone was deleted despite the export failing"
        assert (clone / "fix.py").exists(), "the agent's work was destroyed"

    def test_a_successful_export_still_cleans_up(self, tmp_path, monkeypatch) -> None:
        """The disposable clone must not leak on the normal path — retaining always would
        turn a bounded scratch dir into an unbounded one."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        clone = tmp_path / "iso-clone2"
        clone.mkdir()
        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp2", pr="https://github.com/o/r/pull/2")
        st.clone = str(clone)

        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path)
        (tmp_path / "fp2.nudge-1.diff").write_text("--- a\n", encoding="utf-8")
        assert reg._export_is_durable(st, str(clone), 1) is True

    def test_the_orphan_sweeper_also_spares_retained_work(self, tmp_path, monkeypatch) -> None:
        """Retaining the clone at teardown is pointless if the disk reclaimer deletes it on
        the next pass. The sweeper keeps clones of LIVE watchers; a finished-but-unexported
        one looked exactly like an orphan to it."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        reg = pw.get_registry()
        st = pw.WatcherState(fp="keepme", pr="https://github.com/o/r/pull/9")
        st.unexported_work = True
        clone = Path(reg._clone_dir("keepme"))
        clone.mkdir(parents=True, exist_ok=True)
        (clone / "work.py").write_text("# unexported\n", encoding="utf-8")

        orphan = clone.parent / "gone-0123456789ab"
        orphan.mkdir(parents=True, exist_ok=True)

        with reg._lock:
            reg._watchers["keepme"] = st
        try:
            monkeypatch.setattr(pw.PRWatcherRegistry, "is_alive", lambda _s, _fp: False)
            pw.sweep_orphan_clones()
            assert clone.is_dir(), "the sweeper deleted a clone holding unexported work"
            assert (clone / "work.py").exists()
            # It must still do its job on a genuine orphan, or the fix has just disabled it.
            assert not orphan.exists(), "the sweeper stopped reclaiming real orphans"
        finally:
            with reg._lock:
                reg._watchers.pop("keepme", None)

    def test_a_timed_out_pass_still_checks_durability(self, tmp_path, monkeypatch) -> None:
        """A pass that TIMED OUT may already have edited and committed, and the clone's origin
        is dead, so those commits exist nowhere else.

        `_run_agent_pass` returned early on `not result.ok` — BEFORE the durability check — so
        `unexported_work` stayed False and teardown's `_cleanup_clone` `rmtree`d the only copy.
        `timeout after …` is the likeliest way in: `SessionAgentRunner._finish` itself calls it
        an EXPECTED common outcome. The two doors D-100/D-101 close live INSIDE
        `_export_is_durable`, which the early return skipped entirely. Raised by the Opus
        review; verified RED against the pre-fix `return True`."""
        from types import SimpleNamespace

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        clone = tmp_path / "iso-timeout"
        clone.mkdir()
        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fpto", pr="https://github.com/o/r/pull/7")
        st.clone = str(clone)

        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_set", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_verify_isolation", lambda *_a, **_k: True)
        monkeypatch.setattr(pw, "build_nudge_prompt", lambda *_a, **_k: "prompt")
        # The work IS undurable — that is what the check must discover and flag.
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_is_durable", lambda *_a, **_k: False)
        reg._isolate_clone = True

        runner = SimpleNamespace(
            run=lambda *_a, **_k: SimpleNamespace(ok=False, error="timeout after 900s", text="")
        )
        assert reg._run_agent_pass(st, str(clone), {}, runner, 1) is True
        assert st.unexported_work is True, (
            "a timed-out pass skipped the durability check, so teardown will delete the only "
            "copy of any commits it made"
        )

    def test_a_faulted_pass_still_checks_durability(self, tmp_path, monkeypatch) -> None:
        """Same hole on the runner-EXCEPTION path: a fault after the agent committed left
        `unexported_work` False. Raised by the Opus review."""
        from types import SimpleNamespace

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        clone = tmp_path / "iso-fault"
        clone.mkdir()
        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fpfault", pr="https://github.com/o/r/pull/8")
        st.clone = str(clone)

        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_set", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_verify_isolation", lambda *_a, **_k: True)
        monkeypatch.setattr(pw, "build_nudge_prompt", lambda *_a, **_k: "prompt")
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_is_durable", lambda *_a, **_k: False)
        reg._isolate_clone = True

        def _raise(*_a, **_k):
            raise RuntimeError("runner exploded")

        runner = SimpleNamespace(run=_raise)
        assert reg._run_agent_pass(st, str(clone), {}, runner, 1) is True
        assert st.unexported_work is True, (
            "a faulted pass skipped the durability check, so teardown will delete the only "
            "copy of any commits it made"
        )

    def test_every_exit_from_a_pass_asks_the_same_question(self) -> None:
        """Structural: all three exits from `_run_agent_pass` (success, failed/timed-out
        result, runner exception) must route through the retention check, so a fourth exit
        added later cannot silently reintroduce the data-loss path."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        src = inspect.getsource(pw.PRWatcherRegistry._run_agent_pass)
        assert src.count("_retain_if_work_is_undurable") >= 3, (
            "an exit from `_run_agent_pass` skips the durability check — a pass that edited "
            "and committed before failing would have its only copy deleted at teardown"
        )

    def test_the_cleanup_is_gated_on_the_export(self) -> None:
        """Structural: the `finally` must consult the export outcome, not fire blind."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        src = inspect.getsource(pw.PRWatcherRegistry._run_watcher)
        assert "unexported_work" in src, (
            "the cleanup in `finally` still runs unconditionally, so a failed export "
            "destroys the only copy of the agent's commits"
        )


class TestTheMcpServerLaunchesOnEveryPlatform:
    """`app.json` hard-coded `"command": "python3"`, which does not exist on a native Windows
    install: a venv there ships `python.exe`/`pythonw.exe` and no `python3.exe`, so the spawn
    fails with ENOENT and every auto-improvement MCP tool is silently unavailable.

    Review proposed `"python"`. That is better but still not right, for a reason this repo has
    already written down twice:
      * `mcp_gateway/rewriter_spec.py` bakes `sys.executable` into its stub entry precisely because
        "a `python3` on PATH that can import `kiro_crew` is [not] guaranteed" — kiro-cli strips
        env when spawning MCP subprocesses, so PATH resolution can find an interpreter that
        cannot import the package at all.
      * `platform_compat._is_windows_store_python_stub` exists because on Windows
        `shutil.which("python")` can resolve a 0-BYTE Microsoft-Store reparse point — so bare
        `python` is itself a known-broken candidate.

    So the manifest keeps a readable default and registration RESOLVES it to the running
    interpreter, which is the only value guaranteed to import `kiro_crew`. Raised by the GPT
    review; the stronger fix comes from the repo's own precedent.
    """

    def test_the_manifest_command_is_resolved_to_a_real_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json
        import site
        import sys
        from pathlib import Path

        from kiro_crew import platform_compat
        from kiro_crew.apps import bridges

        monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
        monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: False)

        manifest = json.loads(
            (
                Path(bridges.__file__).resolve().parent
                / "builtins"
                / "auto_improvement"
                / "app.json"
            ).read_text(encoding="utf-8")
        )
        entry = manifest["mcpServers"]["auto-improvement"]
        resolved = bridges.resolve_stdio_command(dict(entry))
        assert (
            resolved["command"] == sys.executable
        ), f"the MCP command was not resolved to the running interpreter: {resolved['command']!r}"
        # The module argv survives after the shared user-site isolation prefix.
        assert resolved["args"] == ["-s", *entry["args"]]

    def test_a_non_python_command_is_left_alone(self) -> None:
        """Only a bare python launcher is substituted. An app that names `node` or an absolute
        path meant it, and rewriting that would break it."""
        from kiro_crew.apps import bridges

        for cmd in ("node", "/usr/local/bin/my-server", "docker"):
            out = bridges.resolve_stdio_command({"command": cmd, "args": []})
            assert out["command"] == cmd, f"{cmd!r} was rewritten"

    def test_an_http_entry_is_untouched(self) -> None:
        from kiro_crew.apps import bridges

        entry = {"url": "http://127.0.0.1:1234/mcp"}
        assert bridges.resolve_stdio_command(dict(entry)) == entry


class TestARunCannotDoubleStart:
    """`start`/`calibrate` assigned `self._thread = thread` INSIDE the lock but called
    `thread.start()` after releasing it, while every "is a run active?" guard tested
    `self._thread.is_alive()`.

    `threading.Thread.is_alive()` is False for an assigned-but-unstarted thread — verified
    directly — so between the assignment and the `start()` there is a window where the
    supervisor holds a thread object that every guard reads as INACTIVE. Two concurrent
    requests (a `POST /run` and a `POST /calibrate`, or two Start clicks) could therefore both
    pass, and two workers would mutate the same clone and overwrite each other's `RunState`.
    The re-check under the lock does not help: it asks the same `is_alive()` question.

    Fixed with an explicit in-flight reservation set under the lock, so the guard's answer no
    longer depends on whether the OS thread has been scheduled yet. Raised by the GPT review.
    """

    def test_an_assigned_but_unstarted_thread_is_not_alive(self) -> None:
        """The premise, asserted so the reasoning cannot rot."""
        import threading

        t = threading.Thread(target=lambda: None, daemon=True)
        assert t.is_alive() is False, "an unstarted thread now reports alive — re-derive D-94"
        t.start()
        t.join(timeout=5)

    def test_a_second_start_is_refused_before_the_thread_runs(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        sup = runner_mod.RunSupervisor()
        monkeypatch.setattr(sup, "_build_driver", lambda _c: object())
        # Never let the worker actually run: the window under test is BEFORE `start()` takes
        # effect, and a real loop would need a repository.
        monkeypatch.setattr(runner_mod.threading, "Thread", _NeverStarts)
        first = sup.start({"clone": "/tmp/x"})
        assert first.get("run_id"), "the first start did not take"
        try:
            sup.start({"clone": "/tmp/x"})
        except RuntimeError as exc:
            assert "already active" in str(exc)
        else:
            raise AssertionError(
                "a second run started while the first was assigned but not yet scheduled — "
                "two workers would mutate the same clone"
            )

    def test_calibrate_is_refused_while_a_start_is_in_flight(self, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        sup = runner_mod.RunSupervisor()
        monkeypatch.setattr(sup, "_build_driver", lambda _c: object())
        monkeypatch.setattr(runner_mod.threading, "Thread", _NeverStarts)
        sup.start({"clone": "/tmp/x"})
        try:
            sup.calibrate({"clone": "/tmp/x"})
        except RuntimeError as exc:
            assert "already active" in str(exc)
        else:
            raise AssertionError("calibration began while a run start was in flight")


class _NeverStarts:
    """A Thread stand-in that records `start()` without scheduling anything, reproducing the
    window where a thread is assigned but not yet running."""

    def __init__(self, *_a, **_k) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return None


class TestSetupCannotRetargetARunThatStartedMeanwhile:
    """`_handle_setup_clone` checks "is a run live?" BEFORE acquiring `clone_lock`, so the
    check and the mutation are not atomic with each other — only with a run's own locked
    section. A setup request that passes the status check and then blocks on `clone_lock` can
    have a run start while it waits; when it finally gets the lock it clones and persists a NEW
    target, and the active run's artifacts are stranded in the old workspace under a
    `workspace_key` the dashboard does not display.

    D-77's mutual exclusion between setup and run-startup is necessary but not sufficient:
    mutual exclusion decides WHO goes first, it does not re-validate the precondition after
    waiting. The status must be re-checked INSIDE the lock, immediately before the clone.
    """

    def test_the_status_is_rechecked_inside_the_lock(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_setup_clone)
        locked = src[src.index("clone_lock()") :]
        assert "_run_is_active()" in locked, (
            "the run status is never re-checked after acquiring `clone_lock`, so a run that "
            "started while setup waited gets retargeted underneath it"
        )

    def test_a_run_that_starts_during_the_wait_blocks_the_retarget(self, monkeypatch) -> None:
        """Behavioral: with the supervisor reporting an active run, the locked section must
        refuse rather than clone."""
        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import runner as runner_mod

        class _Running:
            def status(self) -> dict:
                return {"status": runner_mod.STATUS_RUNNING, "run_id": "run-1"}

        monkeypatch.setattr(runner_mod, "get_supervisor", lambda: _Running())
        assert routes_mod._run_is_active() is True, "the in-lock recheck cannot see an active run"

    def test_an_idle_supervisor_permits_the_retarget(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        assert routes_mod._run_is_active() is False, "an idle supervisor was reported active"


class TestConfigWritesCannotRaceRunStartup:
    """`PUT /config` had the same pre-lock-only guard D-95 fixed in `POST /setup-clone`: it
    refused while a run was live, then applied the patch with no lock and no re-check.

    `branch` is in `_CONFIG_WRITABLE` and `store.workspace_key()` reads config FRESH on every
    path lookup, so a write that lands between two helper calls sends the ruler, results,
    ledger and PR queue to DIFFERENT workspaces within one run. D-68 closed the "already
    running" case; this closes the "starts while we are writing" case, which the earlier guard
    cannot see because the run does not exist yet when it checks.

    Same remedy as D-95, deliberately: take `clone_lock`, re-check `_run_is_active()` inside it,
    and 409 on conflict. Reusing the pattern rather than inventing a third is the point — this
    is the fourth handler to need it. Raised by the GPT review.
    """

    def test_the_config_write_takes_the_clone_lock(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_put_config)
        assert "clone_lock()" in src, (
            "`PUT /config` applies its patch without the clone lock, so a run starting "
            "concurrently can split its artifacts across two workspaces"
        )

    def test_the_status_is_rechecked_inside_the_lock(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_put_config)
        locked = src[src.index("clone_lock()") :]
        assert "_run_is_active()" in locked, (
            "the run status is not re-checked after acquiring the lock, so a run that started "
            "while this write waited gets its workspace moved underneath it"
        )

    def test_every_workspace_mutating_handler_uses_the_same_guard(self) -> None:
        """Structural: a fifth hand-rolled copy is how the guarded set drifts."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        for handler in (routes_mod._handle_put_config, routes_mod._handle_setup_clone):
            src = inspect.getsource(handler)
            assert "_run_is_active()" in src, f"{handler.__name__} lacks the in-lock recheck"
            assert "clone_lock()" in src, f"{handler.__name__} lacks the lock"


class TestDraftAndCommitRecheckRunStatusInsideTheLock:
    """The manual draft and one-click commit routes each mutate the shared clone (checkout /
    apply / reset / push) under `clone_lock`, and each had ONLY the pre-lock
    `_refuse_while_running` guard. That guard proves no run was live when the request ARRIVED,
    not that none started while it waited on the lock — the same not-atomic gap D-95/D-96 closed
    for `POST /setup-clone` and `PUT /config`. A run that starts during the wait is mid
    checkout/apply/push on this very clone when the draft/commit then runs the same sequence,
    reintroducing the two-mutations-in-one-clone corruption the guard exists to prevent.

    Same remedy, deliberately reused: hold `clone_lock`, re-check `_run_is_active()` inside it,
    409 on conflict. `clone_lock` is a re-entrant RLock, so the commit route can hold it and let
    `commit_finding` re-enter. Raised by the GPT review.
    """

    def test_the_draft_route_rechecks_inside_the_lock(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_draft_pr)
        locked = src[src.index("clone_lock()") :]
        assert "_run_is_active()" in locked, (
            "the draft route never re-checks the run status after acquiring `clone_lock`, so a "
            "run that started while it waited has its clone mutated underneath it"
        )

    def test_the_commit_route_rechecks_inside_the_lock(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_commit)
        assert (
            "clone_lock()" in src
        ), "the commit route no longer takes `clone_lock` before its recheck"
        locked = src[src.index("clone_lock()") :]
        assert (
            "_run_is_active()" in locked
        ), "the commit route never re-checks the run status after acquiring `clone_lock`"

    def test_all_four_clone_mutating_handlers_share_the_guard(self) -> None:
        """Structural: draft and commit join config/setup as the FULL set of clone-mutating
        handlers that must recheck in-lock, so a sixth copy cannot quietly skip it."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        for handler in (
            routes_mod._handle_put_config,
            routes_mod._handle_setup_clone,
            routes_mod._handle_draft_pr,
            routes_mod._handle_commit,
        ):
            src = inspect.getsource(handler)
            locked = src[src.index("clone_lock()") :]
            assert (
                "_run_is_active()" in locked
            ), f"{handler.__name__} does not re-check the run status inside the clone lock"


class TestAFailedDiffIsNotMistakenForNoWork:
    """D-83's own durability check had the defect D-83 exists to prevent.

    `_export_is_durable` treated empty diff stdout as "this pass produced nothing, so nothing
    was lost" — but it never looked at `returncode`. A git diff that FAILS writes its message
    to stderr and leaves stdout empty: measured, `git diff <missing-ref>...HEAD` prints
    "fatal: ambiguous argument" and returns empty stdout. So a PR retargeted to a base the
    clone never fetched made the check report DURABLE, D-83's retention never fired, and the
    clone holding the only copy of the agent's commits was deleted — exactly the data loss
    D-83 fixed, through the guard added to fix it.

    "No work" and "cannot tell" are different answers and only the first is safe to act on.
    Raised by the GPT review.
    """

    def test_a_failing_diff_reports_not_durable(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path)  # no artifact written
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)

        class _Failed:
            returncode = 128
            stdout = ""
            stderr = "fatal: ambiguous argument 'refs/x...HEAD': unknown revision"

        monkeypatch.setattr(pw, "_git", lambda *_a, **_k: _Failed())
        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        assert reg._export_is_durable(st, "/tmp/clone", 1) is False, (
            "a FAILED diff was read as 'no work', so the clone holding the only copy of the "
            "agent's commits would be deleted"
        )

    def test_a_genuinely_empty_diff_still_reports_durable(self, tmp_path, monkeypatch) -> None:
        """The distinction that matters: a pass that really produced nothing is not a loss, and
        retaining every such clone would leak disk without bound."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)

        class _Empty:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(pw, "_git", lambda *_a, **_k: _Empty())
        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp2", pr="https://github.com/o/r/pull/2")
        assert reg._export_is_durable(st, "/tmp/clone", 1) is True

    def test_a_written_artifact_is_durable_regardless(self, tmp_path, monkeypatch) -> None:
        """The artifact is the strongest signal — if it exists the work IS saved."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path)
        (tmp_path / "fp3.nudge-1.diff").write_text("--- a\n", encoding="utf-8")
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp3", pr="https://github.com/o/r/pull/3")
        assert reg._export_is_durable(st, "/tmp/clone", 1) is True


class TestUncommittedWorkIsNotMistakenForNoWork:
    """The uncommitted-work arm of the same data-loss family as D-83/D-100.

    `_export_is_durable` diffs `base...HEAD`, which sees COMMITTED history only. The agent
    turn runs with Edit/Write/Bash, so a fix it left uncommitted — a rejected commit, or a
    turn that edited files without committing — makes that diff empty even though real work
    exists, uncommitted, in the disposable clone whose origin is deliberately dead. Reading
    the committed diff alone reported DURABLE, retention never fired, and the `finally` teardown
    deleted the only copy. `git status --porcelain` tells "produced nothing" apart from
    "produced uncommitted work"; an empty committed diff is only "no work" when the tree is
    also clean. Same "no work vs cannot tell" rule as D-100, retained-on-uncertainty. Raised
    by the GPT review.
    """

    @staticmethod
    def _stub_git(monkeypatch, pw, *, diff_rc=0, diff_out="", status_rc=0, status_out=""):
        """Route `_git` by subcommand so diff and status can differ in one test."""

        class _R:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        def _fake(*args, **_kw):
            # args are the varargs passed to `_git`: ("-C", clone, <subcommand>, ...)
            sub = args[2] if len(args) > 2 else ""
            if sub == "status":
                return _R(status_rc, status_out)
            return _R(diff_rc, diff_out)

        monkeypatch.setattr(pw, "_git", _fake)

    def test_empty_diff_but_dirty_tree_is_not_durable(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path)  # no artifact
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        # Committed diff is empty, but the working tree carries an uncommitted edit.
        self._stub_git(monkeypatch, pw, diff_out="", status_out=" M fix.py\n?? new.py\n")

        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp1", pr="https://github.com/o/r/pull/1")
        assert reg._export_is_durable(st, "/tmp/clone", 1) is False, (
            "an empty COMMITTED diff over a DIRTY tree was read as 'no work', so the clone "
            "holding the agent's only (uncommitted) copy would be deleted"
        )

    def test_empty_diff_and_clean_tree_is_still_durable(self, tmp_path, monkeypatch) -> None:
        """The genuine no-work case must still clean up, or retaining every clone leaks disk."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        self._stub_git(monkeypatch, pw, diff_out="", status_out="")

        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp2", pr="https://github.com/o/r/pull/2")
        assert reg._export_is_durable(st, "/tmp/clone", 1) is True

    def test_a_failing_status_reports_not_durable(self, tmp_path, monkeypatch) -> None:
        """`git status` failing is 'cannot tell', which is retained, not deleted — same rule
        the failed-diff branch already follows."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_export_fix", lambda *_a, **_k: None)
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        self._stub_git(monkeypatch, pw, diff_out="", status_rc=128, status_out="")

        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fp3", pr="https://github.com/o/r/pull/3")
        assert (
            reg._export_is_durable(st, "/tmp/clone", 1) is False
        ), "a FAILED git status is 'cannot tell', which must keep the clone"

    def test_end_to_end_uncommitted_work_survives_teardown(self, tmp_path, monkeypatch) -> None:
        """Full path through a REAL git clone: an uncommitted edit must set `unexported_work`
        so the `finally` retains the directory. Exercises the actual `git status` call, not a
        stub, so the fix is proven against git's real porcelain output."""
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw

        clone = tmp_path / "iso-clone"
        clone.mkdir()

        def _run(*args, **_kw):
            return subprocess.run(
                ["git", *args[2:]] if args[:2] == ("-C", str(clone)) else ["git", *args],
                cwd=str(clone),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        # A real repo with one commit, then an UNCOMMITTED edit on top.
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(clone), check=True)
        (clone / "seed.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(clone), check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(clone), check=True)
        # Point the base ref at HEAD so the committed diff is empty …
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=str(clone), check=True
        )
        # … then leave a fix uncommitted, as a rejected-commit turn would.
        (clone / "fix.py").write_text("# the agent's uncommitted work\n", encoding="utf-8")

        monkeypatch.setattr(pw.store, "pr_queue_dir", lambda: tmp_path / "queue")
        (tmp_path / "queue").mkdir()
        monkeypatch.setattr(pw.PRWatcherRegistry, "_log", lambda *_a, **_k: None)
        monkeypatch.setattr(pw, "_git", _run)

        reg = pw.PRWatcherRegistry.__new__(pw.PRWatcherRegistry)
        st = pw.WatcherState(fp="fpe2e", pr="https://github.com/o/r/pull/9")
        st.base_ref = "origin/main"
        assert (
            reg._export_is_durable(st, str(clone), 1) is False
        ), "a real clone with uncommitted work reported durable — teardown would delete it"


class TestWatchersDoNotAutoStartWithoutOptIn:
    """`GET /watchers` ran `reconcile_failing_prs(force=True)`, which START*S* a shell-capable
    watcher for every FILED pull request with failing checks. So merely READING the watcher
    list spawned agents — no operator action, no consent moment.

    That matters because of what a watcher is: its prompt is built from pull-request comment
    text an outsider can write, it runs with an auto-approved `Bash`, and while the strict
    sandbox hides credential stores it does NOT isolate the network (`CLONE_NEWNET` appears
    nowhere in `sandbox.py`). D-105 documented that residual risk as an operator-accepted
    trade — but an auto-start path means the operator never accepted anything.

    Fixed by making the promotion opt-in (`watcherAutoStart`, default OFF, same shape as the
    existing `autoPublish` flag): a GET is now read-only, and an operator who wants the
    self-healing loop turns it on deliberately. `Bash` is unchanged, so the feature still works
    for anyone who opts in. Raised by the GPT review (third time on this surface); the
    auto-start half was found while re-deriving my own D-105 claim and is the part that made
    the earlier "operator consented" framing wrong.
    """

    def test_the_flag_is_writable_and_defaults_off(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _CONFIG_WRITABLE

        assert (
            "watcherAutoStart" in _CONFIG_WRITABLE
        ), "the opt-in cannot be turned on through the config API"

    def test_a_get_does_not_reconcile_unless_opted_in(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_watchers)
        assert "watcherAutoStart" in src, (
            "GET /watchers still promotes watchers unconditionally, so reading the list "
            "spawns shell-capable agents with no operator consent"
        )
        # The reconcile call must sit BEHIND the flag check, not beside it.
        assert src.index("watcherAutoStart") < src.index(
            "reconcile_failing_prs"
        ), "the flag is read after the reconcile it is supposed to gate"

    def test_disk_reclamation_still_runs_when_promotion_is_off(self) -> None:
        """Sweeping orphan clones starts nothing and reads no untrusted text — it only deletes
        scratch directories no live watcher claims. Gating it behind the opt-in would leak disk
        on every install that leaves the flag at its default, so it stays unconditional. (The
        first cut nested it inside the promotion branch, which made the docstring's promise
        false.)"""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import routes as routes_mod

        src = inspect.getsource(routes_mod._handle_watchers)
        assert src.count("sweep_orphan_clones") >= 2, (
            "the orphan sweep runs only on the promotion path, so a default-off install "
            "never reclaims scratch clones"
        )

    def test_the_default_config_leaves_it_off(self) -> None:
        """An absent key must mean OFF — a missing flag defaulting ON is how an opt-in
        silently becomes the default."""
        from typing import Any

        from kiro_crew.apps.builtins.auto_improvement.backend.runner import _as_bool

        # Annotated, not inferred: an empty literal narrows its key type to `Never`, so
        # `{}.get("watcherAutoStart")` is a type error even though it is exactly the shape the
        # caller passes (a config dict with the key absent). Same pitfall as D-72's test.
        absent: dict[str, Any] = {}
        opted_in: dict[str, Any] = {"watcherAutoStart": True}
        assert _as_bool(absent.get("watcherAutoStart"), False) is False
        assert _as_bool(opted_in.get("watcherAutoStart"), False) is True
