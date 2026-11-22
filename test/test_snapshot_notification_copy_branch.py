"""The restore's copy branch, taken when no live ``notifications.jsonl`` exists.

The copy validates every record before creating the destination, installs exactly the
validated bytes, refuses non-regular sources, and orders itself against a concurrent
delivery. Without ``O_NOFOLLOW`` the component is skipped with a named reason. The merge
branch is pinned in ``test_snapshot_notification_merge_branch.py``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
import snapshot_helpers
from snapshot_helpers import unpinnable_argv

from conftest import requires_o_nofollow, requires_symlinks
from kiro_crew import snapshot as snapshot_mod
from kiro_crew.jsonl_util import OversizedRecord, UndecodableRecord
from kiro_crew.testing.wait import wait_until

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_no_gateway = snapshot_helpers._no_gateway


# ── The copy branch must not install unvalidated notification bytes ──────


class _NotificationCopyFixtures:
    """Shared fixtures for the copy branch, held apart from any skip marker.

    A mixin rather than a base test class: the copy-behaviour tests cannot run where
    ``O_NOFOLLOW`` is absent (the copy refuses outright there, by design), while the
    refusal tests must run EVERYWHERE. A skip on a shared base would be inherited by
    both, so the marker goes on one subclass and the fixtures live here.
    """

    GOOD = b'{"ts":"2026-03-01T00:00:00Z","msg":"snap"}\n'
    MORE = b'{"ts":"2026-03-02T00:00:00Z","msg":"snap2"}\n'
    BAD_UTF8 = b'{"ts":"2026-03-03T00:00:00Z","msg":"\xff"}\n'

    def _snap(self, tmp_path, src_bytes: bytes) -> tuple[Path, Path]:
        """An extracted-snapshot staging dir and a data home with no live file."""
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir(parents=True)
        home.mkdir(parents=True)
        (snap / "notifications.jsonl").write_bytes(src_bytes)
        return snap, home

    def _merge(self, snap: Path, home: Path) -> None:
        """Drive the real restore, so the CALL SITE is under test, not the helper."""
        snapshot_mod._do_merge(
            snap, home, ["notifications"], allow_unpinned=bool(unpinnable_argv())
        )


@requires_o_nofollow
class TestNotificationCopyWhenNoLiveFileExists(_NotificationCopyFixtures):
    """With no live file (fresh install), the copy must refuse what the merge refuses.

    A byte-exact copy installs records the destination's reader refuses, and that reader
    loses the WHOLE file to one of them. Fixtures are real bytes on real files: a
    synthesized ``UnicodeDecodeError`` takes a healthy path and proves nothing, and
    ``strict_raw_records`` frames records without decoding them.
    """

    def test_an_undecodable_record_is_never_installed(self, tmp_path, capsys):
        """The refusal must reach the CALLER by raising, not only stdout.

        ``apply_import_zip`` and the dashboard handler never see a print.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.BAD_UTF8)
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)
        assert not (
            home / "notifications.jsonl"
        ).exists(), "a partially copied file was left where the reader will find it"
        out = capsys.readouterr().out
        assert "Notifications: copied" not in out, "reported success on a refused copy"
        assert "✅ notifications" not in out
        assert "not valid UTF-8" in out

    def test_a_platform_skip_does_not_print_the_success_tick(self, tmp_path, monkeypatch, capsys):
        """A caught NotificationCopyUnsupported skip must NOT report ✅ notifications.

        A component that imported nothing never reports a tick, as the crons branch
        gates its tick on ``crons_ok``.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        (home / "notifications.jsonl").write_bytes(b'{"ts":"2026-02-01T00:00:00Z","msg":"local"}\n')
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        snapshot_mod._do_merge(
            snap, home, ["notifications"], allow_unpinned=bool(unpinnable_argv())
        )
        out = capsys.readouterr().out
        assert "Notifications: SKIPPED" in out, out
        assert "✅ notifications" not in out, "a skipped component reported a success tick"

    def test_the_reader_loads_every_record_the_copy_installs(self, tmp_path, monkeypatch):
        """The consequence, asserted through the reader that actually loses the file.

        Framing without decoding passes every byte-level check above; only loading
        through ``_load_notifications`` shows a bad byte costs all rows.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE + self.BAD_UTF8)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)

        # Same source without the bad record: the copy must be fully loadable.
        clean, home2 = self._snap(tmp_path / "clean", self.GOOD + self.MORE)
        monkeypatch.setenv("KIROCREW_HOME", str(home2))
        self._merge(clean, home2)
        from kiro_crew.dashboard import state as dashboard_state

        assert len(dashboard_state._load_notifications()) == 2

    def test_a_clean_source_is_copied_record_for_record(self, tmp_path):
        """The copy stays byte-exact: ``\\r\\n`` and a bare ``\\r`` are not rewritten."""
        src_bytes = b'{"ts":"1","msg":"a"}\r\n{"ts":"2","msg":"b"}\r{"ts":"3","msg":"c"}\n'
        snap, home = self._snap(tmp_path, src_bytes)
        self._merge(snap, home)
        assert (home / "notifications.jsonl").read_bytes() == src_bytes

    def test_an_unterminated_final_record_gains_a_terminator(self, tmp_path):
        """Otherwise the first notification after the restore glues onto it.

        ``_persist_notification`` appends ``json.dumps(note) + "\\n"``, so an
        unterminated last line plus that append parses as neither row.
        """
        snap, home = self._snap(tmp_path, b'{"ts":"1","msg":"a"}\n{"ts":"2","msg":"b"}')
        self._merge(snap, home)
        installed = (home / "notifications.jsonl").read_bytes()
        assert installed.endswith(b"\n")
        assert installed == b'{"ts":"1","msg":"a"}\n{"ts":"2","msg":"b"}\n'

    def test_an_oversized_record_installs_nothing(self, tmp_path, monkeypatch):
        """The cap is a memory bound, and its refusal owes the same all-or-nothing.

        Pinned separately from the encoding case because it is a DIFFERENT
        exception out of the same reader, and an ``except`` narrowed to the
        encoding one would leave this reason escaping past the cleanup.
        """
        monkeypatch.setattr(snapshot_mod, "_NOTIFICATION_RECORD_CAP", 64)
        snap, home = self._snap(tmp_path, self.GOOD + b'{"msg":"' + b"x" * 200 + b'"}\n')
        with pytest.raises(OversizedRecord):
            self._merge(snap, home)
        assert not (home / "notifications.jsonl").exists()

    def test_a_concurrent_notification_is_not_deleted_by_the_rollback(self, tmp_path, monkeypatch):
        """A file this call did not create must survive its refusal.

        ``O_EXCL`` proves this call created a file, not that nothing else wrote to it
        since: the dashboard sink can append inside the live gateway. The whole source is
        validated before the destination exists, so a refusal has nothing to roll back.
        The delivery is injected at the validation of the record that aborts.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.BAD_UTF8)
        live = home / "notifications.jsonl"
        delivered = b'{"ts":"2026-03-09T00:00:00Z","msg":"delivered during the restore"}\n'
        real_key = snapshot_mod._notification_key
        seen: list[bytes] = []

        def keyed(record, path):
            seen.append(record)
            if len(seen) == 2:
                # What `_persist_notification` does, in its own mode.
                with open(live, "a", encoding="utf-8") as f:
                    f.write(delivered.decode())
            return real_key(record, path)

        monkeypatch.setattr(snapshot_mod, "_notification_key", keyed)
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)
        assert live.is_file(), "the rollback deleted a file this call did not create"
        assert live.read_bytes() == delivered, "the delivered notification was altered or lost"

    def test_a_live_file_that_appears_mid_copy_is_not_replaced(self, tmp_path):
        """A clean source must not clobber a name that filled while it validated.

        The branch was chosen because no live file existed; by publish time one can.
        Replacing it would delete whatever the dashboard persisted in between, so
        the publish refuses and leaves the operator's file exactly as it found it.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        live = home / "notifications.jsonl"
        appeared = b'{"ts":"2026-03-09T00:00:00Z","msg":"appeared"}\n'
        live.write_bytes(appeared)
        with pytest.raises(FileExistsError):
            snapshot_mod._copy_notifications(snap / "notifications.jsonl", live)
        assert live.read_bytes() == appeared

    def test_the_data_home_gains_nothing_but_the_notifications_file(self, tmp_path):
        """No intermediate artifact may appear in the data home.

        A temp file is published through a NAME a same-user process can swap before the
        publish. Asserted as "the directory holds nothing else", so an intermediate is
        caught whatever it is named.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.BAD_UTF8)
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)
        assert sorted(p.name for p in home.iterdir()) == []

        clean, home2 = self._snap(tmp_path / "clean", self.GOOD)
        self._merge(clean, home2)
        assert sorted(p.name for p in home2.iterdir()) == ["notifications.jsonl"]

    def test_the_installed_file_is_no_tighter_than_one_the_product_writes(self, tmp_path):
        """A restored file the dashboard cannot manage is its own outage.

        ``os.open`` takes an explicit mode, so this is a real choice and not a
        default: 0o666 lets the kernel apply the umask, which is what the plain
        ``open(path, "a")`` in ``_persist_notification`` gets.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        self._merge(snap, home)
        by_product = home / "written-by-the-product.jsonl"
        with open(by_product, "a", encoding="utf-8") as f:
            f.write('{"ts":"1"}\n')
        installed = (home / "notifications.jsonl").stat().st_mode & 0o777
        assert installed == by_product.stat().st_mode & 0o777, oct(installed)

    # ── read once: the precondition, not the consequence ──────────────────

    def test_a_source_truncated_after_the_read_cannot_affect_the_install(
        self, tmp_path, monkeypatch
    ):
        """Truncating the source after the single read changes nothing in the install.

        This pins the precondition (one read): a design that reads the file again would
        install a silently short file. The truncation is injected at the first
        validation call, after the read and before the write.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE)
        source = snap / "notifications.jsonl"
        real_key = snapshot_mod._notification_key
        fired: list[int] = []

        def keyed(record, path):
            if not fired:
                fired.append(1)
                source.write_bytes(b"")  # the source is gone from here on
            return real_key(record, path)

        monkeypatch.setattr(snapshot_mod, "_notification_key", keyed)
        self._merge(snap, home)

        assert fired, "the injection point never ran, so this test proved nothing"
        installed = (home / "notifications.jsonl").read_bytes()
        assert installed == self.GOOD + self.MORE, "the install followed the file, not the bytes"

    # ── the whole-file cap: the load-bearing addition ──────────────────────

    def test_a_source_over_the_cap_is_refused_with_its_size_named(self, tmp_path, monkeypatch):
        """Over-cap must REFUSE, loudly, and install nothing.

        A single read needs a whole-file cap: the per-record cap bounds one record, and
        the archive bounds nothing. A cap that quietly dropped the tail would be a silent
        partial install. The size is named so an operator can tell this from a corrupt
        archive.
        """
        monkeypatch.setattr(snapshot_mod, "_NOTIFICATION_SOURCE_CAP", 64)
        oversized = self.GOOD * 4
        assert len(oversized) > 64
        snap, home = self._snap(tmp_path, oversized)

        with pytest.raises(OSError) as caught:
            self._merge(snap, home)
        message = str(caught.value)
        assert str(len(oversized)) in message, f"the size was not named: {message}"
        assert "64" in message, f"the limit was not named: {message}"
        assert not (
            home / "notifications.jsonl"
        ).exists(), "an over-cap source was partially installed instead of refused"

    def test_the_source_cap_is_the_value_this_design_was_measured_for(self):
        """A ratchet on the cap: raising it is a memory decision and must edit this test.

        32 MiB is ~4x the product-bounded worst case (400 records at the largest record
        seen on a live install, 8,316,000 bytes). Peak memory held is about twice the cap.
        """
        assert snapshot_mod._NOTIFICATION_SOURCE_CAP == 32 * 1024 * 1024
        assert snapshot_mod._NOTIFICATION_SOURCE_CAP < snapshot_mod._NOTIFICATION_RECORD_CAP

    # ── one resolution per path (the class behind three findings) ──────────

    @requires_symlinks
    def test_a_source_swapped_for_a_symlink_between_the_passes_is_not_followed(self, tmp_path):
        """A symlink swapped in after validation is invisible to the held descriptor.

        Opening the NAME again would write a credential into an agent-readable
        ``notifications.jsonl``. The archive's bytes land; the copy is not refused.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE)
        source = snap / "notifications.jsonl"
        secret = tmp_path / "dot-env"
        secret.write_bytes(b'{"ts":"1","msg":"AWS_SECRET_ACCESS_KEY=hunter2"}\n')
        real_key = snapshot_mod._notification_key
        seen: list[bytes] = []

        def keyed(record, path):
            seen.append(record)
            # Pass 1 has validated both records; swap before pass 2 would re-resolve.
            if len(seen) == 2:
                source.unlink()
                source.symlink_to(secret)
            return real_key(record, path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(snapshot_mod, "_notification_key", keyed)
            self._merge(snap, home)

        installed = (home / "notifications.jsonl").read_bytes()
        assert b"hunter2" not in installed, "the swapped symlink was followed"
        assert installed == self.GOOD + self.MORE

    @requires_symlinks
    def test_a_source_that_is_already_a_symlink_is_refused(self, tmp_path):
        """``is_file()`` follows links, so the branch is entered on one.

        ``O_NOFOLLOW`` refuses at the open rather than reading the target, which is
        the same posture ``_backup_and_copy`` already takes for a symlinked file
        coming out of an archive.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        source = snap / "notifications.jsonl"
        secret = tmp_path / "dot-env"
        secret.write_bytes(b'{"ts":"1","msg":"AWS_SECRET_ACCESS_KEY=hunter2"}\n')
        source.unlink()
        source.symlink_to(secret)
        with pytest.raises(OSError):
            self._merge(snap, home)
        assert not (home / "notifications.jsonl").exists()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
    def test_a_source_that_became_a_fifo_fails_instead_of_hanging(self, tmp_path):
        """Pins ``O_NONBLOCK``.

        Without ``O_NONBLOCK`` the open waits forever for a writer and the restore hangs;
        dropping the flag makes this test time out. Calls the helper directly because
        ``_do_merge``'s ``is_file()`` gate cannot reach a FIFO.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        source = snap / "notifications.jsonl"
        source.unlink()
        os.mkfifo(source)
        with pytest.raises(OSError):
            snapshot_mod._copy_notifications(source, home / "notifications.jsonl")
        assert not (home / "notifications.jsonl").exists()

    @pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs a character device")
    def test_a_non_regular_source_is_refused_not_read_as_empty(self, tmp_path):
        """Pins the ``S_ISREG`` judgement on the descriptor, which ``seek`` does not.

        A FIFO is also caught by ``seek``, but a character device is seekable: without the
        check the read returns EOF and the restore installs an empty file as success.
        """
        dst = tmp_path / "notifications.jsonl"
        with pytest.raises(OSError):
            snapshot_mod._copy_notifications(Path("/dev/null"), dst)
        assert not dst.exists(), "an empty notification file was installed and called success"

    def test_a_notification_delivered_during_the_copy_survives(self, tmp_path, monkeypatch):
        """``O_APPEND``, and it needs a CONCURRENT writer to be observable at all.

        Without it this handle writes at its own stale offset over the delivered row, so
        the delivery must happen mid-copy. The seam is the ``O_CREAT`` destination open:
        ``_notification_key`` runs only during validation, so a per-record hook would
        deliver nothing and pass while proving nothing.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE)
        live = home / "notifications.jsonl"
        delivered = b'{"ts":"2026-03-09T00:00:00Z","msg":"delivered-mid-copy"}\n'
        real_open = os.open

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                # What `_persist_notification` does, in its own mode, the instant the
                # live file exists.
                with open(live, "a", encoding="utf-8") as f:
                    f.write(delivered.decode())
            return fd

        monkeypatch.setattr(os, "open", opening)
        self._merge(snap, home)

        body = live.read_bytes()
        assert delivered in body, "the delivered notification was overwritten"
        assert self.GOOD in body and self.MORE in body, "an archive record was lost"
        body.decode("utf-8")

    def test_both_descriptors_ask_for_binary_mode(self, tmp_path, monkeypatch):
        """``os.open`` is the one API here that can be in TEXT mode.

        A convention, matching ``crash_dump_store``: byte-exact ``\\r\\n`` survival on
        Windows is pinned by ``test_a_clean_source_is_copied_record_for_record``.
        ``O_BINARY`` is simulated on POSIX, so the FLAGS are asserted. The helper is
        called directly so both recorded opens are its own.
        """
        monkeypatch.setattr(os, "O_BINARY", 1 << 20, raising=False)
        seen: list[int] = []
        real_open = os.open

        def recording(path, flags, *args, **kwargs):
            seen.append(flags)
            # Record the flags as the product composed them, then STRIP the
            # simulated bit before the real syscall sees it. The value above is
            # a stand-in for a constant this platform does not have, so it is
            # free to collide with one it does: on Darwin ``O_DIRECTORY`` IS
            # ``1 << 20``, and passing it for a regular file answers ENOTDIR.
            return real_open(path, flags & ~os.O_BINARY, *args, **kwargs)

        snap, home = self._snap(tmp_path, self.GOOD)
        monkeypatch.setattr(os, "open", recording)
        snapshot_mod._copy_notifications(snap / "notifications.jsonl", home / "notifications.jsonl")

        assert len(seen) == 2, f"expected exactly a source and a destination open: {seen}"
        assert all(
            f & os.O_BINARY for f in seen
        ), f"an open in this span did not ask for binary mode: {[hex(f) for f in seen]}"

    def test_a_note_delivered_during_the_copy_survives_the_READER(self, tmp_path, monkeypatch):
        """`O_APPEND` saves the bytes, but the row still needs ordering after the archive.

        An unordered append lands BEFORE the archive's rows, and `_load_notifications`
        keeps the last `_MAX_PERSISTED_NOTIFICATIONS` rows by POSITION. The single worker
        cannot run the queued append while the copy holds it, so the wait below times
        out; the note goes through the executor, the product's own route.
        """
        from kiro_crew.dashboard import state as dashboard_state

        snap, home = self._snap(
            tmp_path,
            b"".join(
                b'{"ts":"2026-01-%02dT00:00:00Z","msg":"archive-%d","kind":"agent"}\n'
                % ((i % 28) + 1, i)
                for i in range(dashboard_state._MAX_PERSISTED_NOTIFICATIONS)
            ),
        )
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        pool = dashboard_state._notification_io_executor()
        note = {"ts": "2026-09-05T00:00:00Z", "msg": "LIVE-NOTE", "kind": "agent"}
        ran_during_copy = threading.Event()
        ran_while_worker_held: list[bool] = []
        submitted: list[object] = []
        dst = str(home / "notifications.jsonl")
        real_open = os.open

        def append_note() -> None:
            dashboard_state._persist_notification(note)
            ran_during_copy.set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            # Scoped to the copy's destination open: this hook patches `os.open`
            # PROCESS-WIDE, and a foreign thread's O_CREAT would submit the append while
            # the worker is still free.
            if flags & os.O_CREAT and os.fspath(path) == dst and not submitted:
                submitted.append(pool.submit(append_note))
                # Recorded here, while the copy holds the worker. After `_merge` the
                # released worker runs the append correctly, so a later `is_set()` races it.
                # flake-ok: negative window moved verbatim: the wait times out while the copy holds the
                # single worker, so a slow runner only weakens it; tracked for a de-flake pass
                ran_while_worker_held.append(ran_during_copy.wait(timeout=1.0))
            return fd

        monkeypatch.setattr(os, "open", opening)
        self._merge(snap, home)

        assert submitted, "the note was never queued, so this test proved nothing"
        assert ran_while_worker_held == [False], (
            "the append ran while the copy was still writing, so the two are concurrent "
            "rather than ordered"
        )
        # Drained before teardown: reverting `KIROCREW_HOME` with the append still queued
        # would write the note to another data home. FIFO, so a no-op drains it.
        # flake-ok: unbounded drain of the single notification worker behind a one-row append,
        # moved verbatim; pytest-timeout names a hang; tracked for a de-flake pass
        pool.submit(lambda: None).result()

        rows = dashboard_state._load_notifications()
        assert any(r.get("msg") == "LIVE-NOTE" for r in rows), (
            "the live notification was ordered ahead of the archive and dropped by the "
            f"reader's positional cap ({len(rows)} rows returned)"
        )

    def test_running_on_the_notification_worker_does_not_deadlock(self, tmp_path, monkeypatch):
        """The serialisation must not wait for a queue only it can drain.

        Called from ON the single worker, a submit would wait for a job that cannot start.
        Uses a throwaway pool with the same thread-name prefix so a regression wedges this
        test's worker, not the shared one; the timeout names the hang.
        """
        import concurrent.futures

        snap, home = self._snap(tmp_path, self.GOOD)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        from kiro_crew.dashboard import state as dashboard_state

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="notif-io")
        monkeypatch.setattr(dashboard_state, "_notification_io_pool", pool)
        try:
            future = pool.submit(
                snapshot_mod._copy_notifications,
                snap / "notifications.jsonl",
                home / "notifications.jsonl",
            )
            wait_until(
                future.done, timeout=15, describe=lambda: "the notification copy is still running"
            )
            future.result()
        finally:
            pool.shutdown(wait=False)
        assert (home / "notifications.jsonl").read_bytes() == self.GOOD

    def test_a_FRESH_gateway_still_orders_the_copy_against_a_delivery(self, tmp_path, monkeypatch):
        """The writer is what makes the pool, so "no pool" never meant "no writer".

        On a fresh gateway a delivery during the restore CREATES the executor. The copy
        acquires the executor rather than running inline when none exists, so the delivery
        queues behind it. Pinned for the `pool is None` path.
        """
        from kiro_crew.dashboard import state as dashboard_state

        snap, home = self._snap(
            tmp_path,
            b"".join(
                b'{"ts":"2026-01-%02dT00:00:00Z","msg":"archive-%d","kind":"agent"}\n'
                % ((i % 28) + 1, i)
                for i in range(dashboard_state._MAX_PERSISTED_NOTIFICATIONS)
            ),
        )
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # A FRESH gateway: nothing has persisted a notification, so there is no pool.
        monkeypatch.setattr(dashboard_state, "_notification_io_pool", None)
        note = {"ts": "2026-09-05T00:00:00Z", "msg": "LIVE-NOTE", "kind": "agent"}
        ran_during_copy = threading.Event()
        ran_while_worker_held: list[bool] = []
        fired: list[int] = []
        dst = str(home / "notifications.jsonl")
        real_open = os.open

        def append_note() -> None:
            dashboard_state._persist_notification(note)
            ran_during_copy.set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            # Scoped to the destination open, as in the READER test above.
            if flags & os.O_CREAT and os.fspath(path) == dst and not fired:
                fired.append(1)
                # The delivery sink's own route on a fresh gateway: it ACQUIRES the
                # executor, creating it if the copy did not.
                dashboard_state._notification_io_executor().submit(append_note)
                # Measured inside the trigger, as in the READER test above.
                # flake-ok: negative window moved verbatim: the wait times out while the copy holds the
                # single worker, so a slow runner only weakens it; tracked for a de-flake pass
                ran_while_worker_held.append(ran_during_copy.wait(timeout=1.0))
            return fd

        monkeypatch.setattr(os, "open", opening)
        self._merge(snap, home)

        assert fired, "the delivery was never queued, so this test proved nothing"
        assert ran_while_worker_held == [False], (
            "a delivery on a fresh gateway ran concurrently with the copy: 'no pool' "
            "was read as 'no writer'"
        )
        # flake-ok: unbounded drain of the single notification worker behind a one-row append,
        # moved verbatim; pytest-timeout names a hang; tracked for a de-flake pass
        dashboard_state._notification_io_executor().submit(lambda: None).result()
        rows = dashboard_state._load_notifications()
        assert any(
            r.get("msg") == "LIVE-NOTE" for r in rows
        ), f"the live notification was dropped by the positional cap ({len(rows)} rows)"

    def test_the_ordering_trigger_ignores_a_foreign_O_CREAT_open(self, tmp_path, monkeypatch):
        """The trigger fires only on the destination open, not a foreign thread's O_CREAT.

        A churn thread is confirmed to have done a creating open before the merge, so a
        first-O_CREAT-anywhere trigger would be foreign on every run, not one CI run in a
        thousand.
        """
        from kiro_crew.dashboard import state as dashboard_state

        snap, home = self._snap(
            tmp_path,
            b"".join(
                b'{"ts":"2026-01-%02dT00:00:00Z","msg":"archive-%d","kind":"agent"}\n'
                % ((i % 28) + 1, i)
                for i in range(dashboard_state._MAX_PERSISTED_NOTIFICATIONS)
            ),
        )
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(dashboard_state, "_notification_io_pool", None)
        note = {"ts": "2026-09-05T00:00:00Z", "msg": "LIVE-NOTE", "kind": "agent"}
        ran_during_copy = threading.Event()
        ran_while_worker_held: list[bool] = []
        fired: list[str] = []
        dst = str(home / "notifications.jsonl")
        real_open = os.open

        def append_note() -> None:
            dashboard_state._persist_notification(note)
            ran_during_copy.set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT and os.fspath(path) == dst and not fired:
                fired.append(os.fspath(path))
                dashboard_state._notification_io_executor().submit(append_note)
                # The wait's return value is the measurement: a held worker times out
                # False. Re-reading the event after the merge would race the post-copy run.
                # flake-ok: negative window moved verbatim: the wait times out while the copy holds the
                # single worker, so a slow runner only weakens it; tracked for a de-flake pass
                ran_while_worker_held.append(ran_during_copy.wait(timeout=1.0))
            return fd

        monkeypatch.setattr(os, "open", opening)

        noise_dir = tmp_path / "foreign-o-creat"
        noise_dir.mkdir()
        stop = threading.Event()
        churned_once = threading.Event()

        def churn() -> None:
            i = 0
            while not stop.is_set():
                p = noise_dir / f"scratch-{i}.tmp"
                fd = os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
                p.unlink(missing_ok=True)
                churned_once.set()
                i += 1

        churner = threading.Thread(target=churn, name="foreign-o-creat", daemon=True)
        churner.start()
        try:
            assert churned_once.wait(timeout=5.0), "the churn thread never opened a file"
            self._merge(snap, home)
        finally:
            stop.set()
            churner.join(timeout=5.0)

        assert fired == [dst], f"the trigger fired on the wrong open(s): {fired}"
        assert ran_while_worker_held == [False], (
            "the append ran while the copy occupied the worker: a foreign trigger "
            "submitted it while the worker was still free, or the ordering broke"
        )
        # flake-ok: unbounded drain of the single notification worker behind a one-row append,
        # moved verbatim; pytest-timeout names a hang; tracked for a de-flake pass
        dashboard_state._notification_io_executor().submit(lambda: None).result()
        rows = dashboard_state._load_notifications()
        assert any(
            r.get("msg") == "LIVE-NOTE" for r in rows
        ), f"the live notification was dropped ({len(rows)} rows)"

    def test_an_import_failure_that_is_not_ImportError_is_not_swallowed(
        self, tmp_path, monkeypatch
    ):
        """An import failure other than ``ImportError`` must PROPAGATE.

        Swallowing it would run the copy inline in a live gateway and lose the ordering
        silently. Only a non-``ImportError`` case can catch a widened ``except``.
        """
        import builtins

        snap, home = self._snap(tmp_path, self.GOOD)
        real_import = builtins.__import__

        def failing(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "kiro_crew.dashboard" and "state" in (fromlist or ()):
                raise RuntimeError("dashboard state failed to initialise")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", failing)
        with pytest.raises(RuntimeError, match="failed to initialise"):
            snapshot_mod._copy_notifications(
                snap / "notifications.jsonl", home / "notifications.jsonl"
            )

    def test_an_unimportable_dashboard_falls_back_instead_of_failing(self, tmp_path, monkeypatch):
        """An absent dashboard (the CLI restore) runs the copy inline.

        There is no writer to order against on this path, so only the fallback is pinned.
        """
        import sys

        snap, home = self._snap(tmp_path, self.GOOD)
        # `None` in sys.modules makes the import raise ImportError, which is the exact
        # branch under test rather than a stand-in for it.
        monkeypatch.setitem(sys.modules, "kiro_crew.dashboard.state", None)
        snapshot_mod._copy_notifications(snap / "notifications.jsonl", home / "notifications.jsonl")
        assert (home / "notifications.jsonl").read_bytes() == self.GOOD

    def test_concurrent_callers_all_get_the_SAME_executor(self, monkeypatch):
        """The lazy init must not be an unlocked check-then-set.

        Two pools would leave their callers unserialised against each other. The
        constructor is made slow on purpose: without it, the lock removed still passed
        5 of 5 rounds. Asserted by object identity across threads.
        """
        import concurrent.futures

        from kiro_crew.dashboard import state as dashboard_state

        real_executor = concurrent.futures.ThreadPoolExecutor

        class SlowToBuild(real_executor):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs):
                # flake-ok: pre-existing 0.2 s construction window moved verbatim; the assertion is
                # object identity, which no timing can satisfy falsely; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing construction window moved verbatim
                time.sleep(0.2)  # the window, held open
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(dashboard_state, "_notification_io_pool", None)
        monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", SlowToBuild)
        threads = 8
        barrier = threading.Barrier(threads)
        created: list[object] = []
        lock = threading.Lock()

        def racer() -> None:
            barrier.wait(timeout=10)
            pool = dashboard_state._notification_io_executor()
            with lock:
                created.append(pool)

        workers = [threading.Thread(target=racer) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=30)

        assert len(created) == threads, f"only {len(created)} of {threads} racers reported"
        distinct = {id(p) for p in created}
        assert len(distinct) == 1, (
            f"{len(distinct)} different executors were created, so callers holding "
            "different pools are not serialised against each other"
        )
        # Whatever the race produced must also be what the module kept, or a caller is
        # queuing onto a pool the module does not hand out.
        assert created[0] is dashboard_state._notification_io_pool

    @requires_symlinks
    def test_a_dangling_symlink_at_the_live_name_is_refused(self, tmp_path):
        """``is_file()`` calls a dangling link absent, so the copy branch meets one.

        Writing through it would land the archive's bytes outside the data home. The
        publish refuses and leaves the link in place.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        outside = tmp_path / "outside.jsonl"
        (home / "notifications.jsonl").symlink_to(outside)
        with pytest.raises(FileExistsError):
            self._merge(snap, home)
        assert not outside.exists(), "the archive's bytes were written outside the data home"
        assert (home / "notifications.jsonl").is_symlink(), "the operator's link was removed"


class TestNotificationCopyRefusalWithoutONofollow(_NotificationCopyFixtures):
    """The platform refusal, which must be exercised on EVERY platform.

    Deliberately NOT marked ``requires_o_nofollow``: on a platform that truly lacks
    the flag these are the only tests in this area that can run, and they are the
    ones that matter there. On a platform that has it the absence is simulated by
    deleting the constant, the idiom this suite already uses.
    """

    def test_a_platform_without_o_nofollow_refuses_the_copy_ENTIRELY(self, tmp_path, monkeypatch):
        """No ``O_NOFOLLOW`` means no safe copy, so the copy does not happen at all.

        The source is an ordinary regular file on purpose: a by-name reparse check races
        the open, and a reparse point to a regular ``.env`` passes ``S_ISREG``. So the
        refusal cannot depend on the source looking wrong at check time.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        with pytest.raises(snapshot_mod.NotificationCopyUnsupported) as caught:
            snapshot_mod._copy_notifications(
                snap / "notifications.jsonl", home / "notifications.jsonl"
            )

        assert not (home / "notifications.jsonl").exists(), "a destination was created"
        message = str(caught.value)
        # A silent skip is the bug class being fixed, so the diagnosis has to be IN the
        # message: the missing primitive, and what was not imported.
        assert "O_NOFOLLOW" in message, message
        assert "NOT imported" in message, message
        assert "notifications.jsonl" in message, message

    def test_the_refusal_happens_before_anything_is_opened_or_acquired(self, tmp_path, monkeypatch):
        """The refusal is a question about the platform, not about this source.

        So it must be decided before the executor is acquired and before the source is
        opened -- otherwise a platform that can never do this safely still pays a thread
        hop and still touches the file. Asserted by counting, so an edit that moves the
        gate below the acquire reddens here.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        opens: list[object] = []
        acquires: list[object] = []
        real_open = os.open

        def counting_open(path, *a, **kw):
            opens.append(path)
            return real_open(path, *a, **kw)

        from kiro_crew.dashboard import state as dashboard_state

        real_exec = dashboard_state._notification_io_executor

        def counting_exec():
            acquires.append(1)
            return real_exec()

        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.setattr(os, "open", counting_open)
        monkeypatch.setattr(dashboard_state, "_notification_io_executor", counting_exec)

        with pytest.raises(snapshot_mod.NotificationCopyUnsupported):
            snapshot_mod._copy_notifications(
                snap / "notifications.jsonl", home / "notifications.jsonl"
            )

        assert opens == [], f"the source was opened despite the refusal: {opens}"
        assert acquires == [], "the executor was acquired despite the refusal"

    def test_the_restore_CONTINUES_and_reports_the_skip_rather_than_aborting(
        self, tmp_path, monkeypatch, capsys
    ):
        """A platform refusal skips ONE component; it does not fail the restore.

        An undecodable record means the archive is bad, so the restore stops; a missing
        ``O_NOFOLLOW`` is a platform limit, and the records stay in the snapshot. The
        restore must not report a copy it did not make.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        (snap / "config.json").write_text('{"k": "v"}')
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        raised = None
        try:
            self._merge(snap, home)
        except BaseException as exc:  # noqa: BLE001 - the point is that none arrives
            raised = exc

        out = capsys.readouterr().out
        assert raised is None, f"the whole restore aborted on a platform refusal: {raised!r}"
        assert not (home / "notifications.jsonl").exists(), "records were installed anyway"
        assert "SKIPPED" in out, out
        assert "O_NOFOLLOW" in out, out
        assert "Notifications: copied" not in out, "reported a copy it did not make"

    def test_no_by_name_reparse_check_is_made_on_either_path(self, tmp_path):
        """The by-name check is gone, not merely bypassed where the open can decide.

        The by-name check is not a floor gated on the flag's absence: where the
        flag exists the open decides, and where it does not the copy is refused before
        reaching here. A by-name check races the open, so calls are counted.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        calls: list[object] = []
        real = snapshot_mod.pinned_fs.is_reparse_point

        def counted(path):
            calls.append(path)
            return real(path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(snapshot_mod.pinned_fs, "is_reparse_point", counted)
            self._merge(snap, home)
        assert calls == [], f"a by-name reparse check ran: {calls}"
