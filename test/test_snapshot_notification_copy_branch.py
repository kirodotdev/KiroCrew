"""The restore's copy branch, taken when no live ``notifications.jsonl`` exists.

The copy validates every record before it creates the destination, installs exactly the
bytes it validated, refuses links, FIFOs and other non-regular sources, and orders itself
against a concurrent delivery on the single notification worker. On a platform without
``O_NOFOLLOW`` the component is skipped with a named reason instead.

The merge branch (a live file exists) is pinned in
``test_snapshot_notification_merge_branch.py``.
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
    """The OTHER branch of the same ``if``, which validated nothing.

    ``_merge_notifications`` runs when a live ``notifications.jsonl`` exists. When
    one does not -- a fresh install, a first restore -- the restore took
    ``shutil.copy2`` instead, so the same snapshot that ABORTED the restore in one
    case was installed silently in the other. A byte-exact copy is correct as a
    copy and that is the defect: it faithfully installs bytes the destination's own
    reader refuses, and that reader loses the WHOLE file to one of them.

    Every fixture is real bytes on a real file, for the reason the merge class
    states: a synthesized ``UnicodeDecodeError`` routes the copy down a healthy
    path and proves nothing. The undecodable record is measured as INSTALLED
    without the fix -- ``strict_raw_records`` frames and bounds records but does
    not decode, so framing alone reproduces ``copy2``'s behaviour exactly.
    """

    def test_an_undecodable_record_is_never_installed(self, tmp_path, capsys):
        """The whole point, and it must reach the CALLER, not only stdout.

        ``apply_import_zip`` appends ``notifications (copied)`` to its summary and
        the dashboard handler answers ``ok: True``; neither sees a print. So the
        posture is the merge branch's -- abort by raising -- and the success lines
        must not be reached.
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

        With a live file present the merge branch runs, and on a platform without
        ``O_NOFOLLOW`` it raises ``NotificationCopyUnsupported``; the call site
        catches it, prints the SKIPPED line, and control must not fall through to
        the component tick -- a component that imported nothing never reports one,
        exactly as the crons branch gates its tick on ``crons_ok``.
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

        This is the test that separates a real fix from a no-op. Framing the
        records without decoding them installs the bad byte just as ``copy2`` did,
        and every byte-level assertion above still passes; only loading the
        installed file through ``_load_notifications`` shows it. Measured on the
        unfixed branch: 0 rows from a file holding 2 valid records, then the next
        rewrite persists that empty view.
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
        """Byte-exactness is the property ``copy2`` had and the fix must keep.

        A ``\\r\\n`` terminator survives and a bare ``\\r`` inside the file ends a
        record without being rewritten -- the text-mode round trip
        removed from the merge is not reintroduced here.
        """
        src_bytes = b'{"ts":"1","msg":"a"}\r\n{"ts":"2","msg":"b"}\r{"ts":"3","msg":"c"}\n'
        snap, home = self._snap(tmp_path, src_bytes)
        self._merge(snap, home)
        assert (home / "notifications.jsonl").read_bytes() == src_bytes

    def test_an_unterminated_final_record_gains_a_terminator(self, tmp_path):
        """Otherwise the first notification after the restore glues onto it.

        ``_persist_notification`` appends ``json.dumps(note) + "\\n"``, so an
        unterminated last line plus that append is one line that parses as neither
        row. The merge branch makes the same repair through ``dst_unterminated``;
        writing record-wise makes it this branch's job too.
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

        Review's finding, and the slip it names is one an exclusive create invites:
        creating the live file with ``O_EXCL`` proves this call CREATED it, not that
        it is the only thing that has since written to it. ``apply_import_zip`` runs
        inside the live gateway, so the dashboard's notification sink can append to
        a file the copy just created -- and the rollback for a later bad record then
        deleted that operator's notification along with the prefix.

        Now structural rather than defended: the whole source is validated before the
        destination is created, so the ordinary refusal has nothing to roll back. The
        delivery is injected at the validation of the record that aborts, which is
        the interleaving that broke the earlier revision. Measured on it, the live
        file and the delivered note are both gone.
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

        A temp file in the data home is published through a NAME, and a same-user
        process that can list the directory can swap what that name holds between
        the write and the publish. Writing straight to an ``O_EXCL`` destination
        needs no such name. Asserted as "the directory holds nothing else" rather
        than "no file called .tmp", so any future intermediate is caught whatever it
        is named.
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
        """Replaces a retired test, and the reversal is the point.

        The predecessor read the file twice and this scenario was its worst outcome: a
        source truncated between the passes gave pass 2 a clean EOF, so nothing raised,
        the success line printed, and the install was silently short -- measured at 5
        records validated, 2 installed. That test asserted the damage was BOUNDED.

        Reading once makes the scenario unrepresentable rather than bounded, so the
        same setup now asserts the opposite: truncating the source after it has been
        read changes nothing, because there is no name left to resolve and no handle
        left open. This pins the PRECONDITION -- one read -- and reddens on any design
        that goes back to the file, which "no partial install" would not, since that
        passes trivially once the window is gone.

        The truncation is injected at the first validation call, which is after the
        read and before the write.
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

        A whole-file cap is what single-read needs and streaming did not: the
        per-record cap bounds one record, not a file made of many, and the source is
        archive-derived so nothing bounds it from outside. The failure mode to avoid is
        not a crash -- it is a cap that quietly drops the tail, which would rebuild the
        silent-partial-install defect this redesign removes, one layer up.

        The cap is patched small rather than writing 32 MiB to disk. The size must
        appear in the message so an operator can tell an over-cap refusal from a
        corrupt archive without reading code.
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
        """A ratchet on the cap, because the cap is the part that can actually break.

        Every other property here is now structural -- there is no window to reopen --
        so the live risk is somebody raising this number for convenience on a large
        archive and reintroducing an unbounded hold. This assertion goes red on that
        day, which is the day it matters.

        32 MiB is ~4x the product-bounded worst case (400 records at the largest
        record observed on a live install, 8,316,000 bytes) and a quarter of the
        per-record cap this same file already accepts for ONE record. Peak held is
        about twice the cap. Raising it is a memory decision, so it should require
        editing a test that says so.
        """
        assert snapshot_mod._NOTIFICATION_SOURCE_CAP == 32 * 1024 * 1024
        assert snapshot_mod._NOTIFICATION_SOURCE_CAP < snapshot_mod._NOTIFICATION_RECORD_CAP

    # ── one resolution per path (the class behind three findings) ──────────

    @requires_symlinks
    def test_a_source_swapped_for_a_symlink_between_the_passes_is_not_followed(self, tmp_path):
        """The second by-name open was the finding; the descriptor is the fix.

        A running agent replaces the extracted notification file with a symlink to
        a credential after the first pass has validated it. The earlier revision
        opened the NAME again for the copy, followed the link, and wrote the secret
        into an agent-readable ``notifications.jsonl``. Reusing the held descriptor
        makes the swap invisible, so the assertion is that the archive's own bytes
        land and the secret does not -- not that the copy is refused.
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

        The caller selected this branch on ``is_file()``, which is False for a FIFO,
        so reaching one means the name changed afterwards. Without ``O_NONBLOCK`` the
        open blocks forever waiting for a writer and the restore HANGS, which is
        worse than failing because a hang reports nothing at all. Dropping the flag
        makes this test TIME OUT rather than fail, and the timeout is the evidence.

        Calls the helper directly, since ``_do_merge`` cannot reach a FIFO through
        its own ``is_file()`` gate -- the hazard is the name changing after it.
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

        Written after the mutation harness showed the first version of this coverage
        was passing for the wrong reason: a FIFO is caught by ``seek`` failing on a
        pipe, so removing the ``S_ISREG`` check reddened nothing. A character device
        is the case ``seek`` cannot catch -- it is seekable, so without the check the
        read simply returns EOF, and the restore CREATES AN EMPTY
        ``notifications.jsonl`` and reports success. That is the outcome being
        refused: not a crash, a silent claim to have imported a history that is gone.
        """
        dst = tmp_path / "notifications.jsonl"
        with pytest.raises(OSError):
            snapshot_mod._copy_notifications(Path("/dev/null"), dst)
        assert not dst.exists(), "an empty notification file was installed and called success"

    def test_a_notification_delivered_during_the_copy_survives(self, tmp_path, monkeypatch):
        """``O_APPEND``, and it needs a CONCURRENT writer to be observable at all.

        The dashboard's sink appends with ``O_APPEND``, so its write goes to
        end-of-file; an ordinary write goes to this handle's own offset, which is
        stale once buffered. The flush then wrote over the delivered row. A test that
        only copies into an empty destination cannot tell the two apart -- every
        byte-level assertion passes either way -- so the delivery has to happen
        mid-copy. Measured on the pre-fix flags: 152 bytes with the row gone, against
        203 with both writers' records present.

        The seam is the DESTINATION OPEN, not a per-record hook. The single-read
        redesign calls ``_notification_key`` once per record during validation and not
        at all during the write, so the old per-record injection point went dead and
        this test silently stopped delivering anything -- it passed while proving
        nothing, which is the same failure mode as a mutation that never applies.
        Injecting when the ``O_CREAT`` open happens puts the delivery exactly where it
        belongs: after the live file exists, before this function's writes flush.
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

        This is a CONVENTION fix, not a corruption fix, and the difference is worth
        recording. A review lane held that the missing flag corrupts records on
        Windows; ``test_a_clean_source_is_copied_record_for_record`` asserts byte-exact
        ``\\r\\n`` survival through these very descriptors, carries no skip marker, and
        passed on the Windows lane -- so the mechanism was tested and did not occur.
        What remains is that every sibling passes the flag (``crash_dump_store`` uses
        this exact read-flag triple) and this one did not.

        Asserted on the FLAGS, because what they guard against cannot be reproduced on
        a POSIX host: ``O_BINARY`` does not exist here, so its value is simulated and
        the assertion is that both opens carry it. The helper is called directly rather
        than through ``_do_merge`` so that every recorded ``os.open`` is one of this
        function's two and the count can be asserted exactly.
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
        """The one that matters: `O_APPEND` saves the bytes and loses the row anyway.

        `O_APPEND` stops a concurrent notification being OVERWRITTEN, and then orders it
        BEFORE the archive's rows -- the dashboard's append reaches end-of-file
        immediately while this copy's writes are still buffered. `_load_notifications`
        keeps the last `_MAX_PERSISTED_NOTIFICATIONS` rows POSITIONALLY, not the newest
        by timestamp, so importing a full 200-record history pushes the live row out of
        the window. Measured before the fix: line 0 of 201, and the reader returned 200
        rows without it.

        Asserted on the ORDERING, not on who wins a race. The first version of this test
        submitted the append and then checked the file, which passed with the
        serialisation removed because the copy's remaining writes happened to finish
        first -- a flaky test that proves nothing, caught by the mutation run. What makes
        the guarantee observable is that the single worker CANNOT run the queued append
        while the copy occupies it: the wait below times out with the fix and returns
        immediately without it, because a free worker executes a trivial job at once.

        The note is SUBMITTED to the executor rather than written inline, because that is
        the product's own route and the only one the serialisation can order.
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
            # Scoped to the copy's own destination open, not to "the first O_CREAT
            # seen anywhere": this hook patches `os.open` PROCESS-WIDE, and on a
            # loaded CI shard the first creating open can belong to another thread
            # entirely. A foreign trigger
            # submits the append while the worker is still FREE, and the assertion
            # then reads a real ordering guarantee as broken. The destination is the
            # one open the copy performs while it occupies the worker, so it is the
            # only trigger that measures the serialisation.
            if flags & os.O_CREAT and os.fspath(path) == dst and not submitted:
                submitted.append(pool.submit(append_note))
                # A FREE worker runs this in microseconds; a worker the copy is running
                # on cannot run it at all until the copy returns. RECORDED HERE rather
                # than read back after the merge: the instant the copy returns, the
                # worker is released and runs the queued append -- which is CORRECT and
                # sets the event. `is_set()` after `_merge` therefore measured whether
                # the main thread reached the assertion before that legitimate run, a
                # footrace it loses on a loaded shard (scoping the trigger left this
                # observation point). The wait's own
                # return value is taken while the copy still holds the worker, which is
                # the only window in which the defect -- and nothing else -- can set it.
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
        # Drained BEFORE any teardown, deliberately. `monkeypatch.undo()` here reverts
        # the patched `KIROCREW_HOME` while the append is still queued, so the worker
        # resolves a different data home and writes the note somewhere this assertion
        # never looks -- which reads exactly like the defect and is not it. FIFO, so
        # waiting on a no-op drains the append ahead of it.
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

        If the copy is ever reached from ON the single worker, submitting to that worker
        blocks for a job that cannot start until the submitter returns. The failure mode
        is a HANG, not an error, so it is worth a test even though nothing reaches it
        today: a deadlock inside a restore reports nothing at all.

        Against a THROWAWAY pool with the same thread-name prefix, not the product's, so
        that a regression wedges this test's worker rather than the shared one every
        other test in the process depends on. The timeout turns the hang into a named
        failure instead of a stalled suite.
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

        Review's finding, and the branch it names was an absence-reading: the copy asked
        whether an executor existed and ran inline when none did. On a fresh gateway
        nothing has persisted a notification yet, so a delivery arriving during the
        restore CREATES the executor and appends through it, concurrently -- which puts
        the live row back at line 0 of 201 and outside the reader's positional window.
        Measured on the pre-fix branch: `append ran DURING the copy: True`, `note
        position: [0] of 201`, `survives reader: False`.

        Acquiring rather than asking closes it: the delivery gets the SAME executor the
        copy is occupying and queues behind it. Asserted for the `pool is None` path
        specifically, because that is the branch whose reasoning was wrong.
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
            # Same scoping as the READER test above, same reason: the
            # hook patches `os.open` PROCESS-WIDE, and on a loaded shard the first
            # O_CREAT can come from a foreign thread while the notification worker is
            # still FREE -- the append then runs immediately and the assertion reads
            # the ordering guarantee as broken. Only the copy's own destination open
            # happens while the copy occupies the worker, so it is the only trigger
            # that measures the serialisation this test is about.
            if flags & os.O_CREAT and os.fspath(path) == dst and not fired:
                fired.append(1)
                # The delivery sink's own route on a fresh gateway: it ACQUIRES the
                # executor, creating it if the copy did not.
                dashboard_state._notification_io_executor().submit(append_note)
                # Measured INSIDE the trigger, for the reason the READER test above
                # records: after the copy returns, the released worker runs the queued
                # append legitimately and sets the event, so a post-merge `is_set()`
                # read is a footrace against correct behaviour rather than a
                # measurement of the ordering.
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
        """The loaded-shard condition itself, kept as a test.

        Both ordering tests in this class patch ``os.open`` PROCESS-WIDE, and both
        fired on a contended CI shard for PRs that never touch this path: some other
        thread's ``O_CREAT`` open arrived first, the append was submitted while the
        notification worker was still FREE, and it ran immediately -- the assertion
        then read a scheduling accident as a broken ordering guarantee. The fix is
        the destination-path scope on the trigger, and this test is that condition
        made deterministic: a churn thread is CONFIRMED to have done a creating open
        before the merge even starts, so under the old first-O_CREAT-anywhere shape
        the trigger is GUARANTEED foreign and the test fails every run, not one CI
        run in a thousand. Under the scoped shape the churn is invisible, however
        the scheduler interleaves it, and the ordering measurement is captured
        INSIDE the trigger -- the wait's return value, taken while the copy still
        holds the worker -- where the append's legitimate post-copy run cannot
        race it: the trigger fired on the destination, the worker stayed held,
        the note survived.
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
                # The measurement is the WAIT'S RETURN VALUE, captured here. A free
                # worker runs the queued append in microseconds and the wait returns
                # True; the worker this copy occupies cannot run it at all, so the
                # wait times out False. Re-reading the EVENT after the merge returns
                # would race the append's legitimate post-copy run instead (the
                # churner join below hands the worker exactly that window).
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
        """The narrowing itself, which the broad ``except`` made untestable.

        ``except Exception`` turned "I could not check" into "there is nothing to check":
        an import failing inside a live gateway for any reason other than absence would
        have run the copy inline and lost the ordering with no signal. Widening the
        clause back cannot be caught by any test that only exercises ``ImportError``,
        because a wider except is a superset -- so this drives the case the narrowing
        exists for and requires it to PROPAGATE.
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
        """The other absence-reading, narrowed to ``ImportError``.

        A genuinely absent dashboard module is the CLI restore, which has no writer to
        order against, so running inline is correct there. What was wrong was catching
        ``Exception``: an import failing for any other reason inside a live gateway would
        have degraded the ordering in silence. This asserts the narrow fallback still
        works; it cannot assert ORDERING, because by construction there is no dashboard
        to order against on this path.
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

        Two threads could each observe ``None``, each construct a pool, and each
        proceed -- one assignment won the global while the loser's worker was already
        live with its job queued, so the two callers were not serialised against one
        another at all. That voids the only guarantee this executor provides, and it is
        the precondition of the ordering this restore path depends on.

        The construction window is WIDENED on purpose. Measured first: with 16 threads
        released from a barrier and the lock removed, this passed 5 out of 5 rounds --
        CPython's switch interval lets the first thread finish constructing before any
        other is scheduled, so the natural window is real but far too small to observe.
        A test that cannot fail proves nothing, so the constructor is made slow enough
        that a second caller is guaranteed to be inside the window. What is being tested
        is the mutual exclusion, not the timing.

        Asserted by object identity, because calling the accessor twice in sequence
        returns the same object with or without the lock.
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
        """``is_file()`` calls a dangling link absent, and ``copy2`` wrote THROUGH it.

        So the branch was chosen because "no live file exists" while a link sat at
        the name, and the archive's bytes landed on the link's target -- outside
        the data home. The publish refuses instead, and the refusal must not delete
        the file the link points at either.
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

        Simulated the way this suite already simulates that platform -- by deleting the
        constant -- because the behaviour is a property of the flag being absent, not of
        Windows.

        The source here is a PERFECTLY ORDINARY regular file: no link, no reparse point,
        nothing wrong with it. That is the point of the test. The predecessor checked
        ``is_reparse_point`` by name and then opened the same name, which an adversary
        that chooses the timing walks straight through, and ``fstat``'s ``S_ISREG`` does
        not close it because a reparse point resolving to a regular ``.env`` passes it.
        So the refusal cannot be conditional on the source looking wrong at check time --
        a test that only refuses a symlinked source would pass with the old, racy floor
        still in place.
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

        The distinction is deliberate and it is not the abort posture the undecodable
        record has. An undecodable record says THIS ARCHIVE is bad, so the restore
        stops. A missing ``O_NOFOLLOW`` says THIS PLATFORM can never do this safely,
        which is not a reason to refuse the other components -- and the records are
        still in the snapshot, so nothing is destroyed by skipping.

        What must NOT happen is the restore reporting a copy it did not make. That is
        why the skip is asserted on the OUTPUT and the absence of the file together.
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
        reaching here. Counting calls rather than reading the source, so reintroducing
        the check -- the racy remedy this PR removed -- reddens here.
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
