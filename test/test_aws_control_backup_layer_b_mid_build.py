"""Layer B gate and state-lock cases for a sessions backup in flight.

Covers a Layer B permission or conversation scope withdrawn mid-build refusing the
upload, the recheck running after ``_authorize_upload``, and when the sidecar file
lock is held across the archive PUT and released on a refusal. Also covers status
reads staying live during an upload, concurrent ``_record_run`` calls ordered by
``_run_sequence``, the state lock's timeout ceiling, and the SEL event for both
Layer B decisions. The shared setup is ``_SessionsArchiveLayerBGateHarness`` from
``aws_control_backup_helpers.py``.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any
from unittest import mock

import pytest
from aws_control_backup_helpers import (
    ACCOUNT,
    _SessionsArchiveLayerBGateHarness,
)

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.aws_control.backend import backup


class TestSessionsArchiveLayerBGateMidBuild(_SessionsArchiveLayerBGateHarness):
    """Layer B gate: a revocation landing mid-build must not ship.

    Covers ``run_sessions_backup`` refusing the upload when the permission or its
    conversation scope is withdrawn while the tar is written, and not when a grant
    lands. With Layer B on, ``_authorize_upload`` and then the recheck run inside the
    sidecar file lock held across the archive's ``put_file``; a refusal frees it.

    Also pins the lock's scope: an attended withheld upload leaves it free, a
    scheduled one holds it, and ``last_runs`` is not blocked by an upload or by a
    writer parked behind it. ``_run_sequence`` bumps under ``_run_lock``, a superseded
    run leaves the newer record and the nightly backoff in place, the lock ceiling is
    at least ``_PUSH_TIMEOUT_SECS``, and both Layer B decisions reach the audit log.
    """

    # -- a revocation landing mid-build must not ship ------------------------

    def test_a_revocation_during_the_build_refuses_the_upload(self, tmp_path, monkeypatch):
        """Enabled at the read, revoked while the tar is written: nothing ships.

        The permission is read once so the archive and its record cannot
        disagree, which leaves a window: these bytes would otherwise upload under
        a permission the operator has withdrawn, and an object in a bucket cannot
        be recalled. Refusing closes the window without breaking the invariant --
        nothing is uploaded and nothing is recorded, so there is no record to
        disagree with anything.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        answers = iter([True, False])

        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: next(answers))
        uploaded: list[str] = []

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            uploaded.append(key)

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
            mock.patch.object(backup, "_record_run") as record_run,
        ):
            with pytest.raises(RuntimeError, match="withdrawn while this archive"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        # Refused BEFORE any byte left and with no run record written -- a record
        # here would tell a reader an archive exists that does not.
        assert uploaded == []
        record_run.assert_not_called()

    def test_a_grant_landing_during_the_build_does_not_refuse(self, tmp_path, monkeypatch):
        """Only the revoked direction refuses.

        A grant arriving mid-build leaves an archive without Layer B, which is the
        withholding default and needs no refusal: the next run picks the grant up.
        Refusing here would turn an operator enabling the feature into a failed
        backup.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        answers = iter([False, True])
        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: next(answers))

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_a_scope_withdrawn_mid_build_refuses_the_upload(self, tmp_path, monkeypatch):
        """The grant staying ON does not mean it still covers the conversations.

        A disable followed by an enable that names no scope leaves the permission on
        with the marker gone, so the grant recheck passes while the conversations
        already written into this tar sit outside what the grant now covers -- and the
        object cannot be recalled once it is PUT.

        MUTATION: drop the scope half of the recheck and this reddens; the archive
        uploads.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        # Covered when the run starts, withdrawn by the time the recheck runs. The grant
        # itself never goes off, so only the scope half can catch this.
        calls: list[int] = []

        def _covers(account):
            calls.append(1)
            return len(calls) == 1

        monkeypatch.setattr(backup, "layer_b_grant_covers_conversations", _covers)

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file") as put,
        ):
            with pytest.raises(RuntimeError, match="conversation scope was withdrawn"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        assert backup.sessions_layer_b_enabled(ACCOUNT) is True
        put.assert_not_called()

    def test_the_withdrawal_check_runs_after_the_authorization_call(self, tmp_path, monkeypatch):
        """The last thing before the upload, so no network call sits in the window.

        ``_authorize_upload`` goes to the network. A check placed in front of it
        leaves that whole round trip inside the window it exists to close: the
        permission can be withdrawn while the authorization is in flight and the
        bytes still ship. Pinned as an ORDER, because both orders pass every
        other test in this class.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        order: list[str] = []

        # Withdrawn only AFTER the authorization has run. With the check in front
        # of the authorization it still reads True and the archive uploads.
        def _reader(account):
            return "authorize" not in order

        monkeypatch.setattr(backup, "sessions_layer_b_enabled", _reader)

        with (
            mock.patch.object(
                backup, "_authorize_upload", side_effect=lambda *a, **k: order.append("authorize")
            ),
            mock.patch.object(
                backup.storage, "put_file", side_effect=lambda *a, **k: order.append("upload")
            ),
        ):
            with pytest.raises(RuntimeError, match="withdrawn while this archive"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        assert order == ["authorize"]

    def test_the_upload_holds_the_setter_lock_across_the_permission_read(
        self, tmp_path, monkeypatch
    ):
        """A withdrawal cannot overtake the upload it was meant to stop.

        Rechecking the permission is not enough on its own: the reader takes no
        lock, so a withdrawal committing between the recheck and ``put_file``
        still shipped the bytes, and bytes in a bucket cannot be recalled.
        Holding the SETTER'S own lock across both makes the withdrawal land wholly
        before the block or wholly after it.

        The setter's lock is the sidecar FILE lock, taken exclusively by
        ``_state_lock`` (through ``set_sessions_layer_b`` ->
        ``_locked_state_update``). ``_upload_lock`` holds that same file lock and
        deliberately NOT ``_run_lock``, so this probes the file lock -- a
        concurrent revocation is another thread taking it, so its availability
        answers whether this call sits inside the setter's critical section.
        Non-blocking on purpose: a blocking acquisition would deadlock against the
        very lock this asserts is held.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        seen: dict[str, bool] = {}

        def _file_lock_is_free_to_another_thread() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                # A non-blocking exclusive take of the setter's file lock. It is a
                # fresh descriptor, so this is exactly the contention a concurrent
                # `set_sessions_layer_b` in another process or thread would meet.
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    # BlockingIOError (an OSError) is the "held by someone else"
                    # signal wait=False raises; anything else also means not free.
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return answer["free"]

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            # Keyed by which object is being written. The label is uploaded after
            # the lock is released, on purpose -- a caption must not hold the
            # permission's critical section -- so reading only "the last put"
            # would report the label's answer and pass with no lock at all.
            which = "label" if key.endswith(backup.LABEL_OBJECT_NAME) else "archive"
            seen[which] = _file_lock_is_free_to_another_thread()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        # The archive key was written at all, so this is the ALLOW path rather than
        # a refusal that never reached the upload.
        assert seen["archive"] is False
        assert record["layer_b"] is True

    def test_an_attended_withheld_upload_does_not_hold_the_setter_lock(self, tmp_path, monkeypatch):
        """An owner-initiated WITHHELD run uploads without holding the state lock.

        The mirror of the sibling above, and the one shape with no permission read
        left inside the block. The lock exists to order this block against the
        SETTERS. The recheck it orders is
        ``layer_b and not sessions_layer_b_enabled(account)``, which short-circuits
        on its first operand when ``layer_b`` is False; the unattended grant, the
        other read a setter can overtake, is re-read for ``CALLER_SCHEDULED`` alone
        and so is not read here. What is left -- ``is_app_enabled``,
        ``aws_consent``, STS -- lives outside this module's state file. An exclusive
        hold would order nothing.

        Scoped to ``CALLER_OWNER`` deliberately, and pinned from the other side by
        ``test_a_scheduled_withheld_upload_holds_the_setter_lock``: a scheduled run
        reaches this block with ``layer_b`` False too, and it must still hold.

        What the hold would cost is measured across accounts. The lock file is
        ``_state_path()``'s sidecar, one path for every account, so a hold here puts
        every other account's state write behind this upload for up to
        ``_STATE_LOCK_TIMEOUT_SECS`` -- including the toggles an operator reaches
        for, ``set_sessions_layer_b`` and ``set_retention_keep``. Probed the same
        way as the sibling, from another thread on a fresh descriptor and without
        blocking, so the answer is exactly the contention a concurrent writer meets.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, False)
        seen: dict[str, bool] = {}

        def _file_lock_is_free_to_another_thread() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return answer["free"]

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            # Keyed the same way as the sibling: the label is uploaded after the
            # block, so reading "the last put" would pass with any lock at all.
            which = "label" if key.endswith(backup.LABEL_OBJECT_NAME) else "archive"
            seen[which] = _file_lock_is_free_to_another_thread()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        assert seen["archive"] is True, "the withheld upload held the state lock"
        # The withheld path, and one that reached the upload: the crew half alone
        # rode, so this is not a refusal that never got as far as `put_file`.
        assert record["layer_b"] is False

    def test_a_scheduled_withheld_upload_holds_the_setter_lock(self, tmp_path, monkeypatch):
        """A SCHEDULED withheld run holds the lock, because it still has a grant to lose.

        The withheld default does not make this block safe to leave unlocked. The
        crew display half rides on every run, permitted or not, and for a scheduled
        caller that half is authorized by the unattended grant, which
        ``_authorize_upload`` re-reads inside this block and
        ``set_nightly_sessions`` writes under this very sidecar lock. Unlocked, a
        revocation committing between that read and ``put_file`` is not ordered
        against the PUT, and the transcript ships after the owner withdrew the
        grant -- an exposure with no recovery, since an object on S3 cannot be
        taken back.

        The counterpart of
        ``test_an_attended_withheld_upload_does_not_hold_the_setter_lock``: same
        withheld archive, same probe, opposite answer, and the caller is the only
        difference between them. Discriminating in the direction that matters -- a
        predicate keyed on ``layer_b`` alone fails this test.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, False)
        seen: dict[str, bool] = {}

        def _file_lock_is_free_to_another_thread() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return answer["free"]

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            which = "label" if key.endswith(backup.LABEL_OBJECT_NAME) else "archive"
            seen[which] = _file_lock_is_free_to_another_thread()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )

        assert seen["archive"] is False, "the scheduled withheld upload left the state lock free"
        assert record["layer_b"] is False

    def test_a_status_read_is_not_blocked_by_an_in_flight_upload(self, tmp_path, monkeypatch):
        """The status surface stays live during a PUT.

        ``last_runs`` takes ``_run_lock`` and the dashboard's backup-status read
        goes through it. Holding ``_run_lock`` across the hour-long PUT would block
        every account's status read behind one account's upload. ``_upload_lock``
        holds only the sidecar file lock, so this asserts
        that from inside ``put_file`` -- while the upload is in flight -- a status
        read completes without blocking. Its counterpart, that the setter STILL
        cannot interleave, is pinned by the sibling test above; together they show
        the scope is exact: the reader is free, the writer is not.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        seen: dict[str, bool] = {}

        def _status_read_completes() -> bool:
            answer: dict[str, bool] = {}

            def probe() -> None:
                # The real status read. It must return, not park, while the upload
                # holds `_upload_lock`. A `_run_lock` held across the PUT would
                # hang this until the join timeout.
                backup.last_runs(ACCOUNT)
                answer["done"] = True

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            return not thread.is_alive() and answer.get("done", False)

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return
            seen["archive"] = _status_read_completes()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)

        assert seen.get("archive") is True, "the status read blocked behind the in-flight upload"

    def test_a_status_read_is_not_blocked_by_a_writer_parked_behind_an_upload(
        self, tmp_path, monkeypatch
    ):
        """The status surface stays live while a WRITER is parked behind the upload.

        The sibling above has no concurrent writer, so it cannot observe this and
        passes either way. Omitting ``_run_lock`` from ``_upload_lock`` is not on its
        own enough to free the status read, because the stall arrives through a
        contending state WRITER rather than through the upload. A ``_state_lock`` that
        took ``_run_lock`` BEFORE parking on the sidecar file lock would leave a second
        account's run finishing during this upload holding ``_run_lock`` for the
        upload's whole duration; ``last_runs`` queues on ``_run_lock``, so every
        account's status read would queue behind that writer, bounded only by
        ``_STATE_LOCK_TIMEOUT_SECS``.

        Driven with a real second-account :func:`_record_run` rather than a hand-held
        lock, so it is the production path that parks. The park point is known
        deterministically rather than slept on: ``file_lock`` is spied on and signals
        when the writer's own thread reaches it, and under the old order ``_run_lock``
        was necessarily already held by then, because ``_state_lock`` took it before
        ``open_lock_file``. So the probe runs at exactly the moment the old order held
        the reader out.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        other_account = "444455556666"
        parked = threading.Event()
        real_file_lock = backup.file_lock
        seen: dict[str, bool] = {}
        writer: dict[str, Any] = {}

        @contextlib.contextmanager
        def spy_file_lock(fd, **kwargs):
            if threading.current_thread().name == "lock-order-writer":
                # About to block in the kernel on the lock the upload holds. Under the
                # old order `_run_lock` is already held at this point, because
                # `_state_lock` took it before `open_lock_file`.
                parked.set()
            with real_file_lock(fd, **kwargs):
                yield

        def record_a_second_accounts_run() -> None:
            try:
                backup._record_run(other_account, backup.KIND_SESSIONS, "k", 1, "fp", "v", tree="t")
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                writer["error"] = repr(exc)
            finally:
                # So a writer that failed before reaching the file lock releases the
                # wait below with a reported error instead of a bare timeout.
                parked.set()

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return
            thread = threading.Thread(
                target=record_a_second_accounts_run, name="lock-order-writer", daemon=True
            )
            writer["thread"] = thread
            thread.start()
            assert parked.wait(timeout=10), "the writer never reached the file lock"
            assert "error" not in writer, writer
            answer: dict[str, bool] = {}

            def probe() -> None:
                # The real status read. It must return while the writer sits on the
                # file lock; under the old order it parked on the writer's `_run_lock`.
                backup.last_runs(ACCOUNT)
                answer["done"] = True

            probe_thread = threading.Thread(target=probe, daemon=True)
            probe_thread.start()
            probe_thread.join(timeout=5)
            seen["archive"] = not probe_thread.is_alive() and answer.get("done", False)

        try:
            with (
                mock.patch.object(backup, "_authorize_upload"),
                mock.patch.object(backup, "file_lock", spy_file_lock),
                mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
            ):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
        finally:
            # Bounded, and in `finally`, so a failing assertion above cannot leave a
            # thread parked on a lock with an hour-long ceiling.
            thread = writer.get("thread")
            if thread is not None:
                thread.join(timeout=15)
                assert not thread.is_alive(), (
                    "the parked writer never completed after the upload released the" " file lock"
                )

        assert "error" not in writer, writer
        assert (
            seen.get("archive") is True
        ), "the status read blocked behind a writer parked on the sidecar file lock"

    def test_the_run_sequence_is_bumped_under_the_run_lock(self, tmp_path, monkeypatch):
        """``(process, sequence)`` stays unique now that the callers drop ``_run_lock``.

        The outer ``_run_lock`` in :func:`_record_run` and :func:`_record_skip` was the
        only thing serialising ``_run_sequence``, and removing it is what frees the
        status read -- so the bump has to move under its own narrow hold in the SAME
        change. ``(process, sequence)`` is the identity the compare-and-set inside
        ``mutate`` reads and the pair :func:`_run_is_newer` orders by, so two records
        sharing a sequence would let a stale baseline pass a check it must fail.

        Asserted by holding ``_run_lock`` and showing the bump cannot proceed. It is
        deterministic in both directions: the writer's REQUEST for the lock is the
        signal, and a bump left outside the hold necessarily precedes that request,
        because it is the first statement of :func:`_record_run_locked` and the only
        later acquisition is the one inside :func:`_state_lock`.
        """
        asked = threading.Event()
        real_lock = backup._run_lock

        class _WatchedLock:
            """Delegates to the real ``RLock``, so reentrancy is the real thing."""

            def _note(self) -> None:
                if threading.current_thread().name == "sequence-writer":
                    asked.set()

            def __enter__(self):
                self._note()
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

            def acquire(self, *args, **kwargs):
                self._note()
                return real_lock.acquire(*args, **kwargs)

            def release(self):
                return real_lock.release()

        outcome: dict[str, Any] = {}

        def record_a_run() -> None:
            try:
                backup._record_run(ACCOUNT, backup.KIND_SESSIONS, "k", 1, "fp", "v", tree="t")
                outcome["wrote"] = True
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                outcome["error"] = repr(exc)

        thread = threading.Thread(target=record_a_run, name="sequence-writer", daemon=True)
        before = backup._run_sequence
        try:
            with mock.patch.object(backup, "_run_lock", _WatchedLock()):
                with real_lock:
                    thread.start()
                    assert asked.wait(timeout=10), "the writer never asked for _run_lock"
                    assert backup._run_sequence == before, (
                        "_run_sequence advanced while another thread held _run_lock, so"
                        " the bump is outside the lock and two records can share one"
                        " sequence"
                    )
                thread.join(timeout=15)
        finally:
            thread.join(timeout=15)
            assert not thread.is_alive(), "the writer never completed"

        assert "error" not in outcome, outcome
        assert outcome.get("wrote") is True
        assert backup._run_sequence == before + 1

    def test_a_lower_sequence_run_does_not_clobber_a_newer_one(self, tmp_path, monkeypatch):
        """Write order can differ from sequence order, and the newer run must win.

        ``_run_lock`` is released before the sidecar file lock is taken, so the
        ``_run_sequence`` bump and the state write are not one critical section: two
        same-kind runs in this process can reach the file lock in an order that differs
        from their sequence order. Without a guard the loser persists last and the slot
        holds the older key, tree and ``layer_b``, so the status surface and the next
        baseline comparison both read a superseded run. A manual run overlapping a
        nightly wake for one account reaches this, because the nightly loop calls
        ``work`` directly and so does not take the Job SDK's ``(kind, account)`` dedupe.

        The inversion is produced rather than simulated: the low-sequence writer bumps
        first, parks in ``file_lock``, and is released only after the high-sequence
        writer has committed. Its upload metadata must still be merged, because the
        object is in the bucket either way and retention reads the versions map.
        """
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        real_file_lock = backup.file_lock
        bumped = threading.Event()
        release_low = threading.Event()
        low: dict[str, Any] = {}

        @contextlib.contextmanager
        def spy_file_lock(fd, **kwargs):
            if threading.current_thread().name == "low-seq-writer":
                # Its sequence is already taken; hold it out of the file lock until the
                # higher-sequenced run has committed.
                bumped.set()
                assert release_low.wait(timeout=10), "the high-seq writer never released"
            with real_file_lock(fd, **kwargs):
                yield

        def write_low() -> None:
            try:
                backup._record_run(
                    ACCOUNT, backup.KIND_SESSIONS, "key-low", 1, "fp-low", "v-low", tree="tree-low"
                )
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                low["error"] = repr(exc)

        thread = threading.Thread(target=write_low, name="low-seq-writer", daemon=True)
        try:
            with mock.patch.object(backup, "file_lock", spy_file_lock):
                thread.start()
                assert bumped.wait(timeout=10), "the low-seq writer never took a sequence"
                # Bumps to a HIGHER sequence and commits first.
                high = backup._record_run(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "key-high",
                    2,
                    "fp-high",
                    "v-high",
                    tree="tree-high",
                )
                release_low.set()
                thread.join(timeout=15)
        finally:
            release_low.set()
            thread.join(timeout=15)
            assert not thread.is_alive(), "the low-seq writer never completed"

        assert "error" not in low, low
        stored = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        assert stored["key"] == "key-high", "a lower-sequence run overwrote a newer one"
        assert stored["tree"] == "tree-high"
        assert stored["sequence"] == high["sequence"]
        # Both objects are really in the bucket, so retention must see both versions
        # even though only one run record survives.
        assert set(backup.uploaded_objects(ACCOUNT)) == {"key-low", "key-high"}
        assert backup.uploaded_versions(ACCOUNT)["key-low"] == "v-low"

    def test_a_superseded_run_keeps_the_nightly_backoff(self, tmp_path, monkeypatch):
        """A run that loses the slot must not end the retry backoff.

        ``_record_run_locked``'s mutate writes the run record into the ``runs`` slot
        under the supersession guard and clears the failure count under that same
        guard, because the two answer one question: a record this document has already
        superseded is not evidence of anything, so it must not retire a count a later
        failure legitimately accumulated. :func:`_merge_pending` states that reason at
        the other place a run record and this clear travel together.

        Gating the clear or leaving it ungated produces identical state everywhere but
        one window, which is why no other case in the suite tells the two apart: the
        winner clears the backoff in its own mutate, so a failure has to land BETWEEN
        the winner's commit and the loser's for the loser to be the one that meets it.

        Reaching that window takes the witness as well. ``record_nightly_failure``
        compares the run slot against the witness read before its attempt, so a failure
        whose witness predates the winner's commit is refused and writes nothing. The
        witness here is therefore read AFTER the winner commits, and the row is asserted
        PRESENT before the loser is released -- without that the assertion below would
        hold for the trivial reason that no row ever existed.

        The inversion is produced rather than simulated, the same way the clobber test
        above builds it: the low-sequence writer bumps first, parks in ``file_lock``, and
        is released only once the high-sequence writer has committed. That is what shows
        the window is reachable by two real runs rather than by an edited document.

        MUTATION: move the ``_clear_nightly_failure`` call in ``_record_run_locked`` back
        out of the ``if not superseded:`` branch and this reddens on the final assertion.
        """
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        real_file_lock = backup.file_lock
        bumped = threading.Event()
        release_low = threading.Event()
        low: dict[str, Any] = {}

        @contextlib.contextmanager
        def spy_file_lock(fd, **kwargs):
            if threading.current_thread().name == "low-seq-writer":
                # Its sequence is already taken; hold it out of the file lock until the
                # higher-sequenced run has committed AND a failure has been recorded.
                bumped.set()
                assert release_low.wait(timeout=10), "the high-seq writer never released"
            with real_file_lock(fd, **kwargs):
                yield

        def write_low() -> None:
            try:
                backup._record_run(
                    ACCOUNT, backup.KIND_SESSIONS, "key-low", 1, "fp-low", "v-low", tree="tree-low"
                )
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                low["error"] = repr(exc)

        thread = threading.Thread(target=write_low, name="low-seq-writer", daemon=True)
        try:
            with mock.patch.object(backup, "file_lock", spy_file_lock):
                thread.start()
                assert bumped.wait(timeout=10), "the low-seq writer never took a sequence"
                # Bumps to a HIGHER sequence and commits first, clearing the backoff in
                # its own mutate -- so the row the loser meets has to be written after.
                backup._record_run(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "key-high",
                    2,
                    "fp-high",
                    "v-high",
                    tree="tree-high",
                )
                witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SESSIONS)
                assert witness is not None, "the winner left no identifiable run record"
                recorded = backup.record_nightly_failure(
                    ACCOUNT, backup.KIND_SESSIONS, "eio", run_witness=witness
                )
                assert recorded, "the compare-and-set refused a witness read after the commit"
                assert backup.nightly_failures(ACCOUNT).get(backup.KIND_SESSIONS), (
                    "the window was never entered: with no failure row on the account"
                    " there is nothing for the loser to preserve, and this case cannot"
                    " tell the two placements apart"
                )
                release_low.set()
                thread.join(timeout=15)
        finally:
            release_low.set()
            thread.join(timeout=15)
            assert not thread.is_alive(), "the low-seq writer never completed"

        assert "error" not in low, low
        # The supersession really happened, so the run under test is the LOSER's: the
        # slot still holds the winner's record and the loser's write was refused.
        assert backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]["key"] == "key-high"
        surviving = backup.nightly_failures(ACCOUNT).get(backup.KIND_SESSIONS)
        assert surviving, (
            "a run whose own record was refused as stale retired the failure count"
            " anyway. That count was accumulated by a failure recorded AFTER the"
            " winning run committed, so nothing about it is over -- and the next"
            " nightly wake reads the account as due on the strength of a run that was"
            " too stale to write a key"
        )
        assert surviving["consecutive"] == 1, surviving

    def test_the_authorization_runs_inside_the_lock_it_will_upload_under(
        self, tmp_path, monkeypatch
    ):
        """Consent cannot go stale between the check and the upload.

        Acquiring the lock AFTER authorizing left a blocking wait between them: a
        concurrent account's backup can hold this lock across its own upload, so a
        consent withdrawal landing during that wait was never re-read, because the
        Layer B recheck does not cover consent. Authorizing inside the lock closes
        it, and this pins that ordering rather than the refusal it enables, because
        both orderings pass every other case in this class.

        The lock is the sidecar FILE lock ``_upload_lock`` holds (not ``_run_lock``,
        which this block does not take), so the probe is a non-blocking exclusive
        take of that file lock: unavailable means we are inside the section.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        # A LIST, not a single value: `_publish_label` authorizes again after the
        # lock is released, on purpose -- a caption must not hold the permission's
        # critical section -- so reading only "the last authorize" would report the
        # label's answer and pass with no lock around the archive at all.
        seen: list[bool] = []

        def _held_by_us() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    # BlockingIOError (an OSError) is the "held by someone else"
                    # signal wait=False raises; anything else also means not free.
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return not answer["free"]

        def fake_authorize(*args, **kwargs):
            seen.append(_held_by_us())

        with (
            mock.patch.object(backup, "_authorize_upload", side_effect=fake_authorize),
            # `return_value`, not a bare stub: `put_file` returns the version id S3
            # assigns and `_record_run` persists it, so a `MagicMock` reaches
            # `json.dumps` and the run dies on serialization rather than on anything
            # this test is about.
            mock.patch.object(backup.storage, "put_file", return_value="v-test"),
        ):
            backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)

        # The archive's authorization is the first one and it must be inside the
        # lock. Anything after it belongs to the label and is outside by design.
        assert seen, "the authorization never ran"
        assert seen[0] is True

    def test_a_refused_authorization_releases_the_lock_it_took(self, tmp_path, monkeypatch):
        """The authorization refusal is inside the lock too, so it must release it.

        `_authorize_upload` raises from within the critical section now. A file
        lock leaked on that path would wedge every later backup and every
        revocation, which is worse than the staleness the wider scope exists to
        prevent. The held lock is the sidecar FILE lock (`_upload_lock`), so the
        release is probed by taking that file lock again from another thread.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        def refuse(*args, **kwargs):
            raise RuntimeError("consent withdrawn; upload refused")

        with (
            mock.patch.object(backup, "_authorize_upload", side_effect=refuse),
            mock.patch.object(backup.storage, "put_file") as put,
        ):
            with pytest.raises(RuntimeError, match="consent withdrawn"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        # Nothing shipped, and the file lock is free again. Probed from another
        # thread on a fresh descriptor for the same reason as the sibling test.
        put.assert_not_called()
        released: dict[str, bool] = {}
        lock_path = backup._state_path().with_suffix(".lock")

        def probe() -> None:
            try:
                with backup.open_lock_file(lock_path) as fd:
                    with backup.file_lock(fd, exclusive=True, wait=False):
                        released["free"] = True
            except OSError:
                released["free"] = False

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "the probe thread blocked instead of answering"
        assert released["free"] is True

    def test_a_refused_upload_releases_the_lock_it_took(self, tmp_path, monkeypatch):
        """The refusal path must not leave the lock held.

        The refusal raises from inside the critical section. A file lock leaked
        there would be worse than the race it closes: every later backup and every
        revocation takes the same sidecar file lock, so they would all wait forever
        on a permission decision that already finished. Release comes from the
        ``with`` structure rather than from an explicit unlock, and this asserts
        that structure holds on the exception path too.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        answers = iter([True, False, False, False])
        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: next(answers))

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file"),
        ):
            with pytest.raises(RuntimeError, match="withdrawn while this archive"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        # Probed from another thread on a fresh descriptor, without blocking.
        # Asserting by acquiring the file lock in THIS thread would HANG on a leak
        # rather than fail, since the file lock is per-descriptor and a second
        # exclusive take blocks. A non-blocking probe from a thread that holds
        # nothing answers cleanly either way.
        released: dict[str, bool] = {}
        lock_path = backup._state_path().with_suffix(".lock")

        def probe() -> None:
            try:
                with backup.open_lock_file(lock_path) as fd:
                    with backup.file_lock(fd, exclusive=True, wait=False):
                        released["free"] = True
            except OSError:
                released["free"] = False

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "the probe thread blocked instead of answering"
        assert released["free"] is True

    def test_the_lock_ceiling_outlasts_the_upload_it_is_held_across(self):
        """The state lock must not refuse a contender while the holder works.

        ``file_lock``'s default ceiling is sized for a sub-second read plus an
        atomic rename, and it requires any caller that can hold the lock longer
        to override it. This gate holds the lock across the authorization and a
        PUT allowed ``_PUSH_TIMEOUT_SECS``, so the default would expire on a
        contender that is merely waiting. That is not a cosmetic failure: the
        contender sees ``OSError``, which ``_record_run`` absorbs by keeping the
        run in memory alone, so a short-lived process that exits before the next
        successful write loses a completed upload's record and leaves the nightly
        loop due. Asserting the value reaches ``file_lock`` rather than only
        asserting the arithmetic, because a constant nobody passes is the exact
        defect this pins.
        """
        seen: list[float | None] = []
        real = backup.file_lock

        @contextlib.contextmanager
        def spy(fd, **kwargs):
            seen.append(kwargs.get("timeout"))
            with real(fd, **kwargs):
                yield

        with mock.patch.object(backup, "file_lock", spy):
            backup._locked_state_update(lambda state: None)

        assert seen, "the state lock did not reach file_lock"
        assert seen[0] is not None, "the state lock took file_lock's default ceiling"
        assert seen[0] >= backup._PUSH_TIMEOUT_SECS

    def test_a_long_holder_does_not_refuse_a_waiting_contender(self, monkeypatch):
        """A holder outliving the default ceiling must not refuse a contender.

        The value assertion above cannot show the consequence, so this drives it:
        the default ceiling is patched down, a holder keeps the sidecar lock past
        it, and the contender must still acquire once the holder releases. The
        holder takes the file lock directly rather than through ``_state_lock`` so
        that the ceiling is the only thing under test -- ``_state_lock`` would also
        take ``_run_lock``, which this says nothing about. Patching
        reaches only the default: ``file_lock`` reads the module ceiling solely
        when no ``timeout`` is passed, so this fails exactly when the explicit one
        is missing.
        """
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 0.05)
        lock_path = backup._state_path().with_suffix(".lock")
        backup._state_path().parent.mkdir(parents=True, exist_ok=True)
        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with platform_compat.open_lock_file(lock_path) as fd:
                with platform_compat.file_lock(fd, exclusive=True, required=True):
                    holding.set()
                    release.wait(timeout=10)

        outcome: dict[str, Any] = {}

        def contender() -> None:
            try:
                backup._locked_state_update(lambda state: None)
                outcome["acquired"] = True
            except OSError as exc:
                outcome["acquired"] = False
                outcome["error"] = repr(exc)

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        try:
            assert holding.wait(timeout=10), "the holder never took the lock"
            contender_thread = threading.Thread(target=contender)
            contender_thread.start()
            # Outlive the patched ceiling while still held, which is the condition
            # that made the unbounded default refuse a contender that was only
            # waiting.
            # flake-ok: pre-existing 300 ms hold past the patched 50 ms lock ceiling; moved verbatim and tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing hold past the patched lock ceiling moved verbatim
            time.sleep(0.3)
            release.set()
            contender_thread.join(timeout=15)
            assert not contender_thread.is_alive(), "the contender never returned"
        finally:
            release.set()
            holder_thread.join(timeout=10)

        assert outcome.get("acquired") is True, outcome

    def test_both_layer_b_decisions_reach_the_audit_log(self, tmp_path, monkeypatch):
        """Allow and withhold each leave a SEL event, not only the refusals.

        The permission decides whether unredacted context leaves the machine, and
        the ALLOW direction is the one that ships the bytes -- so an audit that
        covered only refusals would leave exactly the interesting decision
        unrecorded. Asserted for both directions in one test, because a helper
        that fires on one and not the other is the failure to catch.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        events: list[tuple[str, str]] = []

        class _Log:
            def log_api_access(self, **kw):
                events.append((kw["operation"], kw["resources"]))

        with mock.patch.object(backup, "sel", lambda: _Log()):
            self._store(tmp_path, True)
            self._run_capturing_names(monkeypatch)
            self._store(tmp_path, False)
            self._run_capturing_names(monkeypatch)

        decisions = [r for op, r in events if op == "aws_control.backup_layer_b_decision"]
        assert len(decisions) == 2
        assert "layer_b=allowed" in decisions[0]
        assert "layer_b=withheld" in decisions[1]
        # The account rides on the event, so a multi-account drive's log says
        # WHICH bucket the decision was about.
        assert all(f"account={ACCOUNT}" in r for r in decisions)
