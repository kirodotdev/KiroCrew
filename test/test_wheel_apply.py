"""The one shadow-apply path every caller runs: preflight, outcomes, ownership.

``run_wheel_apply`` is driven for real here, on the real update worker, with
``wheel_engine.apply_wheel_update`` replaced by stand-ins that behave the way
the engine does at its seams (the lock, the cancel, a promotion).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.platform import wheel_apply, wheel_engine


@pytest.fixture(autouse=True)
def _no_applies_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_IN_FLIGHT`` is a module-global set of loop-bound futures: each test gets its own."""
    monkeypatch.setattr(wheel_apply, "_IN_FLIGHT", set())


def _assert_update_lock_free() -> None:
    """The layout's update lock can be taken again (raises ``WheelUpdateBusy`` if not)."""
    fd = wheel_engine.hold_update_lock()
    wheel_engine.release_update_lock(fd)


def _state() -> MagicMock:
    state = MagicMock()
    state.push_update_progress = MagicMock()
    return state


async def _run(
    apply: object, monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> wheel_apply.WheelApplyOutcome:
    monkeypatch.setattr(wheel_engine, "apply_wheel_update", apply)
    return await wheel_apply.run_wheel_apply(
        channel="stable",
        version="9.9.9",
        feed_base="https://feed.example",
        artifact_base="https://bytes.example",
        **kwargs,  # type: ignore[arg-type]
    )


async def _drain_loop() -> None:
    # Progress is pushed from the worker with call_soon_threadsafe.
    for _ in range(3):
        await asyncio.sleep(0)


class TestPreflight:
    def test_the_source_pin_is_checked_first_on_both_bases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.cdn_bases",
            lambda: ("https://feed.example", "https://bytes.example"),
        )

        def blocked(base: str) -> str:
            seen.append(base)
            return "pinned elsewhere" if base == "https://bytes.example" else ""

        def safe() -> bool:
            seen.append("shape")
            return True

        monkeypatch.setattr("kiro_crew.platform.update_governance.update_blocked_reason", blocked)
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", safe)
        with pytest.raises(wheel_apply.WheelApplyRefused) as info:
            wheel_apply.preflight_bases()
        assert info.value.code == "blocked_by_policy"
        assert seen == ["https://feed.example", "https://bytes.example"]

    def test_the_cdn_shape_is_checked_after_the_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason", lambda _b: ""
        )
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: False)
        with pytest.raises(wheel_apply.WheelApplyRefused) as info:
            wheel_apply.preflight_bases()
        assert info.value.code == "bad_cdn"


class TestMemoryReadiness:
    """Decided before anything is downloaded, from this process's memory startup."""

    @staticmethod
    def _phase(monkeypatch: pytest.MonkeyPatch, phase: str, error: str = "") -> None:
        import kiro_crew.memory_startup as startup

        monkeypatch.setattr(startup, "memory_startup_preparing", lambda: phase == "preparing")

        def prepared() -> None:
            if error:
                raise startup.MemoryStartupUnavailable(error)

        monkeypatch.setattr(startup, "require_memory_prepared", prepared)

    def test_only_a_running_unsettled_startup_is_preparing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.memory_startup as startup

        monkeypatch.setattr(startup, "_active", None)
        assert startup.memory_startup_preparing() is False, "no startup here (the CLI)"
        running = startup.MemoryStartup()
        monkeypatch.setattr(startup, "_active", running)
        assert startup.memory_startup_preparing() is True
        for settled in ("ready", "stopped"):
            monkeypatch.setattr(running, settled, True)
            assert startup.memory_startup_preparing() is False, settled
            monkeypatch.setattr(running, settled, False)
        monkeypatch.setattr(running, "error", "journal unreadable")
        assert startup.memory_startup_preparing() is False

    def test_still_preparing_defers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._phase(monkeypatch, "preparing")
        with pytest.raises(wheel_engine.WheelUpdateNotReady):
            wheel_apply.check_memory_ready()

    def test_a_structural_failure_refuses_with_the_fences_own_words(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._phase(monkeypatch, "failed", "Memory recovery failed: journal unreadable")
        with pytest.raises(wheel_engine.WheelUpdateSnapshotFailed, match="journal unreadable"):
            wheel_apply.check_memory_ready()

    def test_an_unresolvable_store_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.memory_stores as stores

        self._phase(monkeypatch, "ready")
        monkeypatch.setattr(stores, "active_store_names", lambda: ["default", "lost"])
        monkeypatch.setattr(
            stores, "owned_store_path", lambda name: None if name == "lost" else Path("/x.db")
        )
        with pytest.raises(wheel_engine.WheelUpdateSnapshotFailed, match="1 memory store"):
            wheel_apply.check_memory_ready()

    def test_a_store_that_failed_its_startup_gets_an_unverified_copy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import kiro_crew.memory_backup as backup
        import kiro_crew.memory_startup as startup
        import kiro_crew.memory_stores as stores

        db = tmp_path / "memory.db"
        db.write_bytes(b"sqlite bytes")
        (tmp_path / "memory.db-wal").write_bytes(b"wal bytes")
        monkeypatch.setattr(
            backup,
            "back_up_all_stores",
            lambda keep, force: {"backed_up": 0, "skipped": 0, "pruned": 0, "failed": 1},
        )
        monkeypatch.setattr(backup, "backup_dir_for", lambda path: tmp_path / "backups")
        monkeypatch.setattr(stores, "active_store_names", lambda: ["default"])
        monkeypatch.setattr(stores, "owned_store_path", lambda name: db)
        monkeypatch.setattr(startup, "memory_store_startup_error", lambda name: "restore failed")

        assert wheel_apply.snapshot_memory_before_update() == (1, "")
        copies = sorted(p.name for p in (tmp_path / "backups" / "unverified").iterdir())
        assert len(copies) == 2 and copies[0].endswith(".db") and copies[1].endswith(".db-wal")

    @pytest.mark.parametrize("planted", ["symlink", "hardlink"])
    def test_a_store_file_linked_to_a_masked_file_is_never_copied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planted: str
    ) -> None:
        """The memory folder is sandbox-writable; a planted ``-wal`` link must not
        carry a masked file (``.env``) into the agent-readable backups."""
        import kiro_crew.memory_backup as backup
        import kiro_crew.memory_startup as startup
        import kiro_crew.memory_stores as stores

        secret = tmp_path / "outside" / ".env"
        secret.parent.mkdir()
        secret.write_bytes(b"API_KEY=do-not-copy")
        db = tmp_path / "memory.db"
        db.write_bytes(b"sqlite bytes")
        wal = tmp_path / "memory.db-wal"
        try:
            if planted == "symlink":
                wal.symlink_to(secret)
            else:
                os.link(secret, wal)
        except (OSError, NotImplementedError):
            pytest.skip(f"{planted}s are not supported here")
        monkeypatch.setattr(
            backup,
            "back_up_all_stores",
            lambda keep, force: {"backed_up": 0, "skipped": 0, "pruned": 0, "failed": 1},
        )
        monkeypatch.setattr(backup, "backup_dir_for", lambda path: tmp_path / "backups")
        monkeypatch.setattr(stores, "active_store_names", lambda: ["default"])
        monkeypatch.setattr(stores, "owned_store_path", lambda name: db)
        monkeypatch.setattr(startup, "memory_store_startup_error", lambda name: "restore failed")

        copied, failure = wheel_apply.snapshot_memory_before_update()
        assert copied == 0 and failure
        out = tmp_path / "backups" / "unverified"
        for copy in out.iterdir() if out.exists() else ():
            assert b"do-not-copy" not in copy.read_bytes()


class TestMemorySnapshotHook:
    def test_a_failed_snapshot_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", lambda: (0, "disk full"))
        with pytest.raises(wheel_engine.WheelUpdateSnapshotFailed, match="snapshot failed"):
            wheel_apply.memory_snapshot_hook(lambda _m: None)()

    def test_a_snapshot_reports_its_count_and_where(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.memory_backup as backup

        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", lambda: (2, ""))
        newest = Path("/home/u/backups/m.db")
        monkeypatch.setattr(backup, "newest_backup", lambda *a: newest)
        said: list[str] = []
        wheel_apply.memory_snapshot_hook(said.append)()
        # The parent renders with the host's own separator, so compare against it.
        assert said == [
            f"Memory snapshot: 2 store(s) copied; default store copies in {newest.parent}"
        ]


class TestUsernsReattach:
    """Fakes only: the real AppArmor state of the host running the suite is never read."""

    @staticmethod
    def _wire(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        restricted: bool = True,
        applies: bool = True,
    ) -> None:
        from kiro_crew.service import apparmor
        from kiro_crew.service import linux as service_linux

        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        launcher = legacy / "bin" / "kirocrew"
        launcher.write_text("", encoding="utf-8")
        layout = wheel_engine.ManagedVenvLayout(
            legacy=legacy, stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)
        monkeypatch.setattr(wheel_apply.sys, "platform", "linux")
        monkeypatch.setattr(apparmor, "apparmor_is_active", lambda: restricted)
        monkeypatch.setattr(apparmor, "userns_restricted", lambda: restricted)
        monkeypatch.setattr(
            apparmor,
            "service_profile_attachment",
            lambda *_a: str(launcher.resolve()) if applies else None,
        )
        monkeypatch.setattr(service_linux, "kirocrew_bin", lambda: str(launcher))

    def test_an_attached_profile_needs_reattaching_after_promotion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path)
        assert wheel_apply.userns_reattach_needed("9.9.9") is True

    def test_no_restriction_needs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, restricted=False)
        assert wheel_apply.userns_reattach_needed("9.9.9") is False

    def test_a_profile_that_does_not_apply_today_is_not_ours_to_guard(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, applies=False)
        assert wheel_apply.userns_reattach_needed("9.9.9") is False

    def test_the_shared_predicate_honours_a_unit_directive(self, tmp_path: Path) -> None:
        """The predicate doctor reports with: an ``AppArmorProfile=`` line in the
        unit silently wins over the path attachment, so the profile does not apply."""
        from kiro_crew.service import apparmor

        launcher = tmp_path / "kirocrew"
        launcher.write_text("", encoding="utf-8")
        profile = tmp_path / "kirocrew-userns"
        profile.write_text(
            f'profile {apparmor.PROFILE_NAME} "{launcher.resolve()}" flags=(unconfined) {{}}\n',
            encoding="utf-8",
        )
        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Service]\nExecStart=/x\n", encoding="utf-8")
        attached = apparmor.service_profile_attachment(str(launcher), unit, profile)
        assert attached == str(launcher.resolve())
        unit.write_text("[Service]\nAppArmorProfile=kirocrew-userns\n", encoding="utf-8")
        assert apparmor.service_profile_attachment(str(launcher), unit, profile) is None


@pytest.mark.skipif(not wheel_engine.IS_POSIX, reason="the managed venv is POSIX-only")
class TestRestartReaches:
    """A restart after a promotion must exec the promoted tree, never the running one.

    Driven through the REAL ``respawn_executable`` on a layout under
    ``tmp_path``, with this process "running" from the legacy tree.
    """

    @staticmethod
    def _layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> wheel_engine.ManagedVenvLayout:
        legacy = tmp_path / "crew-venv"
        promoted = tmp_path / "crew-venv-9.9.9"
        for tree in (legacy, promoted):
            (tree / "bin").mkdir(parents=True)
            (tree / "bin" / "kirocrew").write_text("", encoding="utf-8")
            python = tree / "bin" / "python3"
            python.write_text("", encoding="utf-8")
            python.chmod(0o755)
        layout = wheel_engine.ManagedVenvLayout(
            legacy=legacy, stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)
        monkeypatch.setattr(wheel_engine.sys, "executable", str(legacy / "bin" / "python3"))
        return layout

    def test_a_link_to_the_promoted_tree_reaches_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = self._layout(tmp_path, monkeypatch)
        layout.stable_link.symlink_to(layout.versioned_tree("9.9.9"))
        assert wheel_apply.restart_reaches("9.9.9") is True

    def test_a_dangling_link_falls_back_to_the_running_tree_and_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exec-restart loop: respawn falls back to sys.executable, the old tree."""
        layout = self._layout(tmp_path, monkeypatch)
        layout.stable_link.symlink_to(tmp_path / "crew-venv-9.9.8")
        assert wheel_apply.restart_reaches("9.9.9") is False

    def test_a_link_left_on_the_running_tree_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = self._layout(tmp_path, monkeypatch)
        layout.stable_link.symlink_to(layout.legacy)
        assert wheel_apply.restart_reaches("9.9.9") is False


class TestShownFailureText:
    def test_the_step_and_the_final_error_line_survive_the_cap(self) -> None:
        retries = "".join(
            f"WARNING: Retrying (Retry(total={n})) after connection broken: pypi.org\n"
            for n in range(60)
        )
        final = "ERROR: Could not install packages due to an OSError: [Errno 28] No space"
        text = "pip install into the shadow venv exited 1: " + retries + final
        assert len(text) > 3500
        shown = wheel_apply.shown_failure_text(text)
        assert len(shown) <= wheel_engine.FAILURE_TEXT_CHARS
        assert shown.startswith("pip install into the shadow venv exited 1: ")
        assert shown.endswith(final)

    def test_a_credential_is_redacted_before_the_cut(self) -> None:
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        text = "step exited 1: " + "x" * 3000 + secret + "y" * 200
        assert secret[-10:] not in wheel_apply.shown_failure_text(text)


class TestOutcomes:
    @pytest.mark.asyncio
    async def test_a_promotion_pushes_progress_only_after_the_lock(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def apply(**kwargs: object) -> Path:
            kwargs["on_locked"]()  # type: ignore[operator]
            kwargs["progress"]("building it")  # type: ignore[operator]
            return tmp_path / "crew-venv-9.9.9"

        state = _state()
        outcome = await _run(apply, monkeypatch, state=state)
        await _drain_loop()
        assert outcome.status == "promoted"
        steps = [call.args[0] for call in state.push_update_progress.call_args_list]
        assert steps == ["pulling", "building"]

    @pytest.mark.asyncio
    async def test_a_held_lock_is_busy_and_pushes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def apply(**_kwargs: object) -> Path:
            raise wheel_engine.WheelUpdateBusy("another kirocrew update is already in progress")

        state = _state()
        outcome = await _run(apply, monkeypatch, state=state)
        await _drain_loop()
        assert outcome.status == "busy"
        state.push_update_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_second_apply_in_this_process_is_busy_without_queueing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started, release = threading.Event(), threading.Event()
        calls: list[int] = []

        def apply(**_kwargs: object) -> Path:
            calls.append(1)
            started.set()
            release.wait(30)
            raise wheel_engine.WheelUpdateError("done")

        first = asyncio.ensure_future(_run(apply, monkeypatch))
        try:
            assert await asyncio.to_thread(started.wait, 30)
            second = await _run(apply, monkeypatch)
            assert second.status == "busy"
            assert calls == [1], "the second apply never queued"
        finally:
            release.set()
            await asyncio.wait_for(first, timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ending",
        [
            None,
            wheel_engine.WheelUpdateError("wheel SHA-256 mismatch"),
            wheel_engine.WheelUpdateIncompatible("needs 3.99", version="9.9.9", sha256="a" * 64),
            wheel_engine.WheelUpdateCancelled("cancelled", reason="shutdown"),
        ],
        ids=["promoted", "failed", "incompatible", "cancelled"],
    )
    async def test_a_held_lock_is_released_however_the_apply_ends(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: Exception | None
    ) -> None:
        loop_thread = threading.current_thread()
        applied_on: list[threading.Thread] = []
        released: list[tuple[int, threading.Thread]] = []
        real_release = wheel_engine.release_update_lock

        def release(fd: int) -> None:
            released.append((fd, threading.current_thread()))
            real_release(fd)

        def apply(**_kwargs: object) -> Path:
            applied_on.append(threading.current_thread())
            if ending is not None:
                raise ending
            return tmp_path / "crew-venv-9.9.9"

        monkeypatch.setattr(wheel_engine, "release_update_lock", release)
        fd = wheel_engine.hold_update_lock()
        await _run(apply, monkeypatch, held_lock_fd=fd)
        assert released == [(fd, applied_on[0])], "the apply worker releases exactly once"
        assert applied_on[0] is not loop_thread
        await asyncio.to_thread(_assert_update_lock_free)

    @pytest.mark.asyncio
    async def test_the_lock_holder_runs_while_an_unlocked_apply_is_in_flight(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An approval that took the lock first is not burned as ``busy``.

        The other apply (the coordinator's) is in flight but has not taken the
        lock; it loses on the lock, so the holder's apply is the one that runs.
        """

        def apply(**kwargs: object) -> Path:
            if kwargs["held_lock_fd"] is None:
                wheel_engine.release_update_lock(wheel_engine.hold_update_lock())
            return tmp_path / "crew-venv-9.9.9"

        fd = wheel_engine.hold_update_lock()
        unlocked = asyncio.ensure_future(_run(apply, monkeypatch))
        await asyncio.sleep(0)  # its entry is in flight; its worker has not run
        assert wheel_apply.applies_in_flight() == 1
        holder = await _run(apply, monkeypatch, held_lock_fd=fd)
        other = await asyncio.wait_for(unlocked, timeout=30)
        assert (holder.status, other.status) == ("promoted", "busy")
        _assert_update_lock_free()

    @pytest.mark.asyncio
    async def test_a_held_lock_is_released_when_an_apply_outlives_the_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started, finish, released = threading.Event(), threading.Event(), threading.Event()
        loop_thread = threading.current_thread()
        applied_on: list[threading.Thread] = []
        releases: list[tuple[int, threading.Thread]] = []
        real_release = wheel_engine.release_update_lock

        def release(fd: int) -> None:
            releases.append((fd, threading.current_thread()))
            real_release(fd)
            released.set()

        def apply(**_kwargs: object) -> Path:
            applied_on.append(threading.current_thread())
            started.set()
            assert finish.wait(30), "the test never released the apply worker"
            raise wheel_engine.WheelUpdateCancelled("cancelled")

        monkeypatch.setattr(wheel_apply, "STOP_GRACE_SECS", 0.05)
        monkeypatch.setattr(wheel_engine, "release_update_lock", release)
        fd = wheel_engine.hold_update_lock()
        task = asyncio.ensure_future(_run(apply, monkeypatch, held_lock_fd=fd))
        try:
            assert await asyncio.to_thread(started.wait, 30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
            assert releases == [], "the apply still owns the descriptor after the grace"
            with pytest.raises(wheel_engine.WheelUpdateBusy):
                await asyncio.to_thread(_assert_update_lock_free)
        finally:
            finish.set()
            assert await asyncio.to_thread(released.wait, 30), "the worker never released its lock"
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)
        await _drain_loop()  # completion callbacks must not release the descriptor a second time
        assert releases == [(fd, applied_on[0])]
        assert applied_on[0] is not loop_thread
        await asyncio.to_thread(_assert_update_lock_free)

    @pytest.mark.asyncio
    async def test_a_cancelled_caller_releases_once_when_the_worker_finishes_inside_the_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started, finish = threading.Event(), threading.Event()
        loop_thread = threading.current_thread()
        releases: list[tuple[int, threading.Thread]] = []
        real_release = wheel_engine.release_update_lock

        def release(fd: int) -> None:
            releases.append((fd, threading.current_thread()))
            real_release(fd)

        def apply(**kwargs: object) -> Path:
            cancel: wheel_engine.ApplyCancel = kwargs["cancel"]  # type: ignore[assignment]
            cancel.on_set(finish.set)
            started.set()
            assert finish.wait(30), "the caller never cancelled the apply"
            raise wheel_engine.WheelUpdateCancelled("cancelled")

        monkeypatch.setattr(wheel_engine, "release_update_lock", release)
        monkeypatch.setattr(wheel_apply, "STOP_GRACE_SECS", 30)
        fd = wheel_engine.hold_update_lock()
        task = asyncio.ensure_future(_run(apply, monkeypatch, held_lock_fd=fd))
        try:
            assert await asyncio.to_thread(started.wait, 30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=30)
        finally:
            finish.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)
        await _drain_loop()
        assert len(releases) == 1 and releases[0][0] == fd
        assert releases[0][1] is not loop_thread
        await asyncio.to_thread(_assert_update_lock_free)

    @pytest.mark.asyncio
    async def test_rejected_submission_releases_the_held_lock_once_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.current_thread()
        releases: list[tuple[int, threading.Thread]] = []
        real_release = wheel_engine.release_update_lock

        def release(fd: int) -> None:
            releases.append((fd, threading.current_thread()))
            real_release(fd)

        class StoppedExecutor:
            def submit(self, *args: object) -> None:
                raise RuntimeError("executor is shut down")

        monkeypatch.setattr("kiro_crew.executors.update_executor", StoppedExecutor)
        monkeypatch.setattr(wheel_engine, "release_update_lock", release)
        apply = MagicMock()
        fd = wheel_engine.hold_update_lock()
        with pytest.raises(RuntimeError, match="executor is shut down"):
            await _run(apply, monkeypatch, held_lock_fd=fd)
        apply.assert_not_called()
        assert len(releases) == 1 and releases[0][0] == fd
        assert releases[0][1] is not loop_thread
        assert not wheel_apply._IN_FLIGHT
        await asyncio.to_thread(_assert_update_lock_free)

    @pytest.mark.asyncio
    async def test_an_entry_whose_loop_is_closed_is_not_in_flight(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        dead = asyncio.new_event_loop()
        dead.close()
        stale = wheel_apply._Running(wheel_engine.ApplyCancel(), dead.create_future())
        wheel_apply._IN_FLIGHT.add(stale)
        outcome = await _run(lambda **_kw: tmp_path / "crew-venv-9.9.9", monkeypatch)
        assert outcome.status == "promoted"
        assert stale not in wheel_apply._IN_FLIGHT

    @pytest.mark.asyncio
    async def test_an_incompatible_release_names_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def apply(**_kwargs: object) -> Path:
            raise wheel_engine.WheelUpdateIncompatible(
                "needs Python 3.99", version="9.9.9", sha256="a" * 64
            )

        outcome = await _run(apply, monkeypatch)
        assert (outcome.status, outcome.message) == ("incompatible", "needs Python 3.99")

    @pytest.mark.asyncio
    async def test_failure_text_is_redacted_then_capped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

        def apply(**_kwargs: object) -> Path:
            raise wheel_engine.WheelUpdateError(f"could not fetch with {secret} " + "x" * 5000)

        outcome = await _run(apply, monkeypatch)
        assert outcome.status == "failed"
        assert secret not in outcome.message
        assert len(outcome.message) <= wheel_engine.FAILURE_TEXT_CHARS

    @pytest.mark.asyncio
    async def test_an_unexpected_error_is_a_failure_not_a_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def apply(**_kwargs: object) -> Path:
            raise OSError(28, "No space left on device")

        outcome = await _run(apply, monkeypatch)
        assert outcome.status == "failed"

    @pytest.mark.asyncio
    async def test_the_deadline_starts_at_the_lock_and_stops_the_apply_through_its_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wheel_apply, "APPLY_DEADLINE_SECS", 0.05)

        def apply(**kwargs: object) -> Path:
            time.sleep(0.2)  # waiting for the lock does not count against the deadline
            cancel: wheel_engine.ApplyCancel = kwargs["cancel"]  # type: ignore[assignment]
            assert not cancel.is_set()
            fired = threading.Event()
            cancel.on_set(fired.set)
            kwargs["on_locked"]()  # type: ignore[operator]
            assert fired.wait(30), "the deadline never set the cancel"
            raise wheel_engine.WheelUpdateCancelled("cancelled", reason=cancel.reason)

        outcome = await _run(apply, monkeypatch)
        assert outcome.status == "timed_out"


class TestOwnership:
    @pytest.mark.asyncio
    async def test_stop_cancels_the_apply_in_flight_and_waits_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = threading.Event()

        def apply(**kwargs: object) -> Path:
            cancel: wheel_engine.ApplyCancel = kwargs["cancel"]  # type: ignore[assignment]
            fired = threading.Event()
            cancel.on_set(fired.set)
            started.set()
            assert fired.wait(30)
            raise wheel_engine.WheelUpdateCancelled("cancelled", reason=cancel.reason)

        monkeypatch.setattr(wheel_apply, "STOP_GRACE_SECS", 10.0)
        task = asyncio.ensure_future(_run(apply, monkeypatch))
        try:
            assert await asyncio.to_thread(started.wait, 30)
            await wheel_apply.stop_wheel_applies()
            outcome = await asyncio.wait_for(task, timeout=10)
        finally:
            wheel_apply.cancel_wheel_applies()
        assert outcome.status == "cancelled"
        assert not wheel_apply._IN_FLIGHT

    @pytest.mark.asyncio
    async def test_a_process_exit_cancels_the_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every exec seam and hard exit cancels the applies in flight through
        ``platform_compat.cancel_wheel_applies_in_flight``."""
        from kiro_crew import platform_compat

        started = threading.Event()
        reasons: list[str] = []

        def apply(**kwargs: object) -> Path:
            cancel: wheel_engine.ApplyCancel = kwargs["cancel"]  # type: ignore[assignment]
            fired = threading.Event()
            cancel.on_set(fired.set)
            started.set()
            assert fired.wait(30)
            reasons.append(cancel.reason)
            raise wheel_engine.WheelUpdateCancelled("cancelled", reason=cancel.reason)

        task = asyncio.ensure_future(_run(apply, monkeypatch))
        assert await asyncio.to_thread(started.wait, 30)
        platform_compat.cancel_wheel_applies_in_flight("exec")
        outcome = await asyncio.wait_for(task, timeout=10)
        assert outcome.status == "cancelled" and reasons == ["exec"]

    @pytest.mark.asyncio
    async def test_a_cancelled_stop_is_never_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shutdown's own deadline that lands during the grace wait still fires."""
        started, release = threading.Event(), threading.Event()

        def apply(**_kwargs: object) -> Path:
            started.set()
            release.wait(30)  # an apply slow to unwind
            raise wheel_engine.WheelUpdateCancelled("cancelled")

        # Far beyond any runner's scheduling delay, so the bound below separates
        # "the caller's deadline fired" from "the grace was waited out".
        monkeypatch.setattr(wheel_apply, "STOP_GRACE_SECS", 60.0)
        task = asyncio.ensure_future(_run(apply, monkeypatch))
        try:
            assert await asyncio.to_thread(started.wait, 30)
            began = time.monotonic()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(wheel_apply.stop_wheel_applies(), timeout=0.3)
            assert time.monotonic() - began < wheel_apply.STOP_GRACE_SECS / 2
        finally:
            release.set()
            await asyncio.wait({task}, timeout=10)

    @pytest.mark.asyncio
    async def test_an_apply_that_outlives_the_grace_is_never_an_unretrieved_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, object]] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        started, release = threading.Event(), threading.Event()

        def apply(**_kwargs: object) -> Path:
            started.set()
            release.wait(30)
            raise wheel_engine.WheelUpdateCancelled("cancelled")

        monkeypatch.setattr(wheel_apply, "STOP_GRACE_SECS", 0.05)
        try:
            task = asyncio.ensure_future(_run(apply, monkeypatch))
            assert await asyncio.to_thread(started.wait, 30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
            release.set()
            # Let the worker finish and its future resolve with nobody awaiting it.
            for _ in range(50):
                await asyncio.sleep(0.02)
            import gc

            gc.collect()
            await asyncio.sleep(0)
        finally:
            release.set()
            loop.set_exception_handler(previous)
        assert not [c for c in unhandled if "never retrieved" in str(c.get("message", ""))]
