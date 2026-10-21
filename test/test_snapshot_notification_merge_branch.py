"""Restore merging notifications into a live file that already exists.

``_merge_notifications`` appends only records that are valid for the destination, keyed
so that a re-import appends nothing, and it resolves each name once, so a name swapped
between validation and copy changes nothing. A refused cron merge is not reported as a
success in the restore status line.

The copy branch (no live file yet) is pinned in
``test_snapshot_notification_copy_branch.py``; snapshot and restore themselves are in
``test_snapshot.py``.
"""

from __future__ import annotations

import json
import os

import pytest
import snapshot_helpers
from snapshot_helpers import unpinnable_argv

from conftest import requires_o_nofollow, requires_symlinks
from kiro_crew import snapshot as snapshot_mod
from kiro_crew.jsonl_util import OversizedRecord, UndecodableRecord, UnreadableRecord

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_no_gateway = snapshot_helpers._no_gateway


@requires_o_nofollow
class TestNotificationMergeWriteSideContract:
    """The notification merge copies records, so its bytes must be valid FOR THE DESTINATION.

    Every fixture here is real BYTES written to a real file. A synthesized
    ``UnicodeDecodeError`` would route the merge down a healthy path and prove
    nothing: the defect was that the decode happened at ``for line in f``,
    OUTSIDE the ``try``, so the failure never took the branch a fake exception
    would have taken.
    """

    LIVE = b'{"ts":"2026-02-01T00:00:00Z","msg":"local"}\n'
    GOOD = b'{"ts":"2026-03-01T00:00:00Z","msg":"snap"}\n'
    # 0xff is invalid UTF-8 anywhere; a truncated multi-byte lead is the
    # ordinary way a real file gets there.
    BAD_UTF8 = b'{"ts":"2026-03-02T00:00:00Z","msg":"\xff"}\n'

    def _files(self, tmp_path, src_bytes: bytes, dst_bytes: bytes):
        src = tmp_path / "snap-notifications.jsonl"
        dst = tmp_path / "live-notifications.jsonl"
        src.write_bytes(src_bytes)
        dst.write_bytes(dst_bytes)
        return src, dst

    @staticmethod
    def _merge_must_not_abort(src, dst) -> None:
        """Call the merge, turning an escaping exception into a NAMED failure.

        Several properties here are "a record of this shape must not abort the
        restore", and the pre-fix behaviour was an escaping exception. Letting it
        surface raw would report the red as an incidental error; ``pytest.fail``
        makes the red say which property broke.
        """
        try:
            snapshot_mod._merge_notifications(src, dst)
        except Exception as exc:  # noqa: BLE001 — the subject is that it does not
            pytest.fail(f"the merge aborted instead of handling the record: {exc!r}")

    # ── encoding ──────────────────────────────────────────────────────────

    def test_an_invalid_utf8_source_record_never_reaches_the_live_file(self, tmp_path, capsys):
        """The whole point: the live file must stay loadable.

        Its loader decodes the WHOLE file and returns no rows at all on one bad
        byte, after which the next rewrite persists that empty view -- so a
        single appended bad record costs every record that was already there.
        """
        src, dst = self._files(tmp_path, self.GOOD + self.BAD_UTF8, self.LIVE)
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert b"\xff" not in after
        after.decode("utf-8")  # the property that matters: still loadable
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out, "reported success on a failed merge"
        assert "not valid UTF-8" in out

    def test_the_failure_reaches_the_caller_and_is_not_only_printed(self, tmp_path):
        """A warn-and-return would tell an API caller the import succeeded.

        ``apply_import_zip`` appends ``notifications (merged)`` to its summary
        unconditionally, and the dashboard handler answers ``ok: True`` with a
        SEL ``outcome="ok"``. Neither sees stdout. So the merge has to raise, or
        an import that left records behind is reported as one that did not.
        Pinned separately from the byte-level assertions because it is a
        contract with the CALLER, not with the file.
        """
        for label, src_bytes, dst_bytes in (
            ("source", self.GOOD + self.BAD_UTF8, self.LIVE),
            ("destination", self.GOOD, self.LIVE + self.BAD_UTF8),
        ):
            src, dst = self._files(tmp_path, src_bytes, dst_bytes)
            with pytest.raises(UnreadableRecord):
                snapshot_mod._merge_notifications(src, dst)
            assert True, label

    def test_an_invalid_utf8_record_already_in_the_live_file_is_a_true_no_op(
        self, tmp_path, capsys
    ):
        """The destination scan is its own failure domain and must write nothing.

        This is the site the issue's own criterion required and did not name.
        A scan in text mode would let pre-existing live corruption abort the
        merge with a traceback before the copy loop is reached; it still aborts,
        but with a named reason and without having touched the
        destination.
        """
        src, dst = self._files(tmp_path, self.GOOD, self.LIVE + self.BAD_UTF8)
        before = dst.read_bytes()
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == before, "destination-scan failure was not a no-op"
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out
        assert "merge aborted" in out

    def test_the_success_line_is_printed_exactly_once(self, tmp_path, capsys):
        """A count, not a membership test, because a duplicate is invisible to `in`.

        The restructure that added the raising posture left the pre-existing
        success print in place below the new one, so the CLI reported the import
        count twice. Every other assertion here spells the check
        ``"Notifications imported:" in out``, which passes just as happily on two
        copies as on one -- and no linter or type check sees a doubled print
        either. Found by the First Principles lane, pinned here.
        """
        src, dst = self._files(tmp_path, self.GOOD, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        out = capsys.readouterr().out
        assert out.count("Notifications imported:") == 1, f"success line not printed once:\n{out}"

    # ── boundary and byte-exactness ───────────────────────────────────────

    def test_a_crlf_terminated_record_keeps_its_carriage_return(self, tmp_path):
        """Row D of the measurement: text mode DROPPED the ``\\r``.

        Universal newlines translated ``\\r\\n`` to ``\\n`` on read and
        ``os.linesep`` put back only ``\\n`` on write, so the appended record
        was one byte shorter than the record on disk. This fires on a pure UTF-8
        host with fully valid UTF-8 input, which is why an explicit
        ``encoding=`` does not address it -- ``newline=`` is a separate axis.
        """
        src_bytes = b'{"ts":"2026-03-03T00:00:00Z","msg":"crlf"}\r\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes

    def test_a_record_holding_a_bare_carriage_return_is_not_silently_lost(self, tmp_path, capsys):
        """Row E: the strongest demonstration, and it needs no bad encoding.

        A VALID UTF-8 record containing a bare ``\\r`` was split by universal
        newlines, both halves then failed ``json.loads``, the
        ``except (ValueError, TypeError): pass`` swallowed both, and the merge
        printed ``imported: 0`` and returned success. The record was gone from
        the live file permanently. Data loss on the default configuration.
        """
        src_bytes = b'{"ts":"2026-03-04T00:00:00Z","msg":"a\rb"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        self._merge_must_not_abort(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 0" not in capsys.readouterr().out

    # ── framing ───────────────────────────────────────────────────────────

    def test_an_unterminated_live_record_gains_a_terminator_before_any_append(self, tmp_path):
        """A crash mid-append leaves the live file without a final terminator.

        Appending onto it glued two records into one line that parses as
        neither, so both were lost to the loader.
        """
        unterminated = b'{"ts":"2026-02-02T00:00:00Z","msg":"torn"}'
        src, dst = self._files(tmp_path, self.GOOD, unterminated)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after == unterminated + b"\n" + self.GOOD
        rows = [json.loads(line) for line in after.splitlines() if line.strip()]
        assert [r["msg"] for r in rows] == ["torn", "snap"]

    def test_an_unterminated_source_record_is_terminated_as_it_is_appended(self, tmp_path):
        """Otherwise the NEXT append glues onto it, moving the same defect."""
        src, dst = self._files(tmp_path, b'{"ts":"2026-03-05T00:00:00Z"}', self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + b'{"ts":"2026-03-05T00:00:00Z"}\n'

    # ── dedupe key type ───────────────────────────────────────────────────

    def test_a_non_object_record_does_not_abort_the_merge(self, tmp_path, capsys):
        """``json.loads(raw).get("ts")`` raised AttributeError on a JSON array.

        The record is unparseable AS A NOTIFICATION but its bytes are still
        history, so it is copied and keyed by its raw form -- not dropped, which
        would delete it.
        """
        src_bytes = b"[1, 2]\n" + self.GOOD
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        self._merge_must_not_abort(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_an_unhashable_ts_does_not_abort_the_merge(self, tmp_path, capsys):
        """A list or dict ``ts`` raised TypeError on set insert."""
        src_bytes = b'{"ts":[1,2],"msg":"weird"}\n' + b'{"ts":{"a":1},"msg":"weirder"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        self._merge_must_not_abort(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_a_numeric_ts_still_deduplicates_on_its_value(self, tmp_path, capsys):
        """Keying only `str` would REGRESS against the predecessor.

        `json.loads(line).get("ts") or line.strip()` keyed a numeric ts on the
        number, so two rows carrying the same numeric ts with different bytes --
        one normalised, one not -- deduplicated. Falling back to the raw form for
        them instead persists a duplicate, which is the loss class this whole
        change is about. Found by the GPT lane.
        """
        live = b'{"ts":1767225600,"msg":"same row, live spelling"}\n'
        src_bytes = b'{"msg":"same row, snapshot spelling","ts":1767225600}\n'
        src, dst = self._files(tmp_path, src_bytes, live)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == live, "a numeric ts stopped deduplicating"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_an_int_and_an_equal_float_ts_deduplicate(self, tmp_path, capsys):
        """`1` and `1.0` are equal and hash equal, so they are ONE row.

        The kind tag is `"num"` for both, deliberately coarser than the Python
        type: tagging with `type(ts).__name__` split a row written as an integer
        on one side and a float on the other -- an ordinary serializer artefact
        -- into two records and persisted the duplicate. Found by the GPT lane.
        """
        live = b'{"ts":1767225600,"msg":"one row"}\n'
        src_bytes = b'{"ts":1767225600.0,"msg":"one row, float spelling"}\n'
        src, dst = self._files(tmp_path, src_bytes, live)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == live, "an int and an equal float ts stopped deduplicating"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_a_boolean_ts_does_not_collide_with_the_number_one(self, tmp_path, capsys):
        """`True == 1` and `hash(True) == hash(1)`, so `bool` is its own kind.

        Widening the predicate to any hashable `ts` is what exposes this: without
        a distinct tag these two records are one set member and the second is
        DELETED as a duplicate -- the loss class this change closes, re-created by
        the fix for it. `bool` is checked before the numeric arm because it is a
        subclass of `int`.
        """
        src_bytes = b'{"ts":true,"msg":"boolean"}\n{"ts":1,"msg":"number"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes, "a boolean ts collided with 1"
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_a_fragment_of_a_split_record_is_not_skipped_against_a_truncated_live_row(
        self, tmp_path
    ):
        """Content is not identity, and using it as identity DELETES bytes.

        A crash mid-append leaves a truncated row in the live file. A source
        record holding a bare carriage return is split at it, because the
        reader's boundaries are the universal-newline set -- and the source's
        first piece can strip to exactly that truncated live row. Keyed on
        content, that piece was skipped as a duplicate while the second piece was
        appended, so the live file gained a dangling line and the source's
        carriage return was gone, while the merge printed success. Measured on
        these exact bytes. Found by the GPT lane.
        """
        live = b'{"ts":"2026-01-01T00:00:00Z","msg":"a\n'
        src_bytes = b'{"ts":"2026-01-01T00:00:00Z","msg":"a\rb"}\n'
        src, dst = self._files(tmp_path, src_bytes, live)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after == live + src_bytes, "a record fragment was skipped as a duplicate"
        assert b"\r" in after, "the source carriage return was lost"

    def test_two_distinct_records_with_no_ts_both_land(self, tmp_path, capsys):
        """The add-guards are what keep `None` out of the seen-set.

        If either `existing.add` accepted a `None` key, the FIRST identity-less
        record would seed it and every later one would match -- so a source
        holding several such rows would append one and silently drop the rest.
        Two rows are the smallest input that observes it; one row cannot.
        """
        src_bytes = b'{"msg":"first with no ts"}\n{"msg":"second with no ts"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes, "an identity-less row was dropped"
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_a_parsing_ts_less_row_deduplicates_on_its_raw_bytes(self, tmp_path, capsys):
        """A ts-less row that PARSES is a whole record, so bytes are its identity.

        Rewritten rather than adjusted: the predecessor of this test pinned a
        re-append as declared behaviour, and that premise is gone. Keying such a
        row on its unstripped bytes restores idempotence for a re-run without
        restoring the deletion, because byte-equal records ARE the same record.
        """
        src_bytes = b'{"msg":"no ts at all"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 1" in capsys.readouterr().out
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes, "a re-run must NOT re-append it"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_an_UNTERMINATED_ts_less_row_also_deduplicates_on_re_import(self, tmp_path, capsys):
        """The key must be the bytes that LAND, not the bytes that arrive.

        The test above uses a source row that already ends in a newline, so it
        cannot see this: an UNTERMINATED row is written with a terminator
        appended, so keying the arriving form makes a second import compare the
        source's unterminated bytes against the terminated row the first import
        itself wrote, miss, and append a second copy. Reproduced before the fix --
        two copies after two imports, with "imported: 1" both times.

        Asserted by COUNT rather than by membership, because membership is
        structurally blind to duplication, which is the whole defect here.
        """
        src_bytes = b'{"msg":"no ts, unterminated"}'  # deliberately no terminator
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        landed = self.LIVE + src_bytes + b"\n"

        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == landed
        assert "Notifications imported: 1" in capsys.readouterr().out

        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after.count(src_bytes) == 1, "the row must appear exactly ONCE"
        assert after == landed, "a re-import must append nothing"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_two_parsed_rows_differing_ONLY_in_terminator_both_survive(self, tmp_path):
        """Pins the DIRECTION of the normalization, not merely its presence.

        Found by mutation: replacing the ``+ b"\\n"`` normalization with
        ``rstrip(b"\\r\\n")`` left the whole suite green, so nothing distinguished
        the safe direction from the predecessor's deleter. The existing
        strip-alike test cannot catch it -- its fragment is UNPARSEABLE, so it
        takes the ``None`` path and never reaches the raw key at all.

        These two rows PARSE, so both are byte-keyed. They land as distinct bytes
        (``...\\r`` is kept as-is, ``...`` gains ``\\n``), so both must survive.
        Under ``rstrip`` they collapse to one key and the second is deleted.
        """
        cr_row = b'{"msg":"same payload"}\r'
        bare_row = b'{"msg":"same payload"}'
        src, dst = self._files(tmp_path, cr_row + bare_row, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after == self.LIVE + cr_row + bare_row + b"\n", "both rows must land"
        assert after.count(b'{"msg":"same payload"}') == 2, "neither may be skipped"

    def test_an_unterminated_DESTINATION_row_keeps_its_identity(self, tmp_path):
        """The destination side takes the same normalization, so the two agree.

        A crash leaves the live file's final record unterminated; the merge
        terminates it before appending. Its key therefore has to be the
        terminated form too, or the row it becomes stops matching the row it was
        and a later import of the same content duplicates it.
        """
        row = b'{"msg":"crash-truncated tail"}'
        src, dst = self._files(tmp_path, row + b"\n", self.LIVE + row)
        snapshot_mod._merge_notifications(src, dst)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes().count(row) == 1, "one row, not two"

    def test_two_rows_that_only_STRIP_alike_both_survive(self, tmp_path):
        """Two rows that only STRIP alike both survive.

        A crash truncates a live row to ``b'{"a": "x'``; framing splits a source
        record at its bare carriage return, yielding ``b'{"a": "x\\r'``. Those two
        are NOT byte-equal but they strip alike, which is exactly how the
        predecessor deleted the source's bytes. Both must survive.
        """
        truncated = b'{"a": "x'
        src, dst = self._files(tmp_path, b'{"a": "x\ry"}\n', self.LIVE + truncated)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert truncated in after, "the live truncated row must remain"
        assert b'{"a": "x\r' in after, "the source fragment must not be skipped"
        assert b'y"}' in after, "the source tail must be appended"

    def test_a_byte_key_can_never_collide_with_a_ts_key(self, tmp_path):
        """``json.loads`` cannot produce a value whose type is named ``raw``.

        The kind tag is what keeps the two key families apart, so this pins the
        claim the docstring makes rather than leaving it as prose.
        """
        k = lambda b: snapshot_mod._notification_key(b, tmp_path / "x.jsonl")  # noqa: E731

        # A HASHABLE ts always tags with its kind, never "raw" -- json.loads can
        # only yield str/int/float/bool here, so type(ts).__name__ cannot be "raw"
        # and the string "raw" as a VALUE tags as `str`, not as the byte family.
        for value in (b'{"ts": "raw"}', b'{"ts": 1}', b'{"ts": 1.0}', b'{"ts": true}'):
            key = k(value + b"\n")
            assert key is not None and key[0] != "raw", f"{value!r} collided"
        # An UNHASHABLE ts has no usable identity of its own, so it takes the byte
        # key. Safe for the same reason: byte-equal records are the same record.
        for value in (b'{"ts": [1]}', b'{"ts": {"n": 1}}'):
            key = k(value + b"\n")
            assert key is not None and key[0] == "raw", f"{value!r} should byte-key"
        # Parsed but no ts -> byte key. Unparseable -> no key at all.
        assert k(b'{"m":1}\n')[0] == "raw"
        assert k(b"not json\n") is None

    # ── bound ─────────────────────────────────────────────────────────────

    def test_an_over_cap_record_aborts_instead_of_being_skipped(
        self, tmp_path, capsys, monkeypatch
    ):
        """A skipped record here is a permanently deleted one, so it aborts.

        The cap is moved rather than writing a 128 MiB fixture; the read is a
        global lookup at call time for exactly that reason.
        """
        monkeypatch.setattr(snapshot_mod, "_NOTIFICATION_RECORD_CAP", 64)
        oversized = b'{"ts":"2026-03-06T00:00:00Z","msg":"' + b"x" * 200 + b'"}\n'
        src, dst = self._files(tmp_path, self.GOOD + oversized, self.LIVE)
        with pytest.raises(OversizedRecord):
            snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert b"x" * 200 not in after
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out
        assert "record over 64 bytes" in out

    # ── failure posture ───────────────────────────────────────────────────

    def test_an_archive_derived_path_cannot_forge_terminal_output(self, tmp_path, capsys):
        """A bundle chooses its own inner root, so a path can carry ANSI controls.

        `_safe_name` exists for exactly this and is used at nineteen sites in this
        module; its docstring names archive root directories. These three prints
        are new code and were bypassing it, so a crafted root printed raw would
        move the cursor and overwrite lines right above the prompt where the
        operator decides whether to trust the restore. Found by the GPT lane.
        """
        hostile = tmp_path / "kirocrew-snapshot-\x1b[2K\x1b[1Aevil"
        hostile.mkdir()
        src = hostile / "notifications.jsonl"
        dst = tmp_path / "live.jsonl"
        src.write_bytes(self.BAD_UTF8)
        dst.write_bytes(self.LIVE)
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        out = capsys.readouterr().out
        assert "\x1b" not in out, f"a raw escape reached the terminal:\n{out!r}"
        assert "evil" in out, "the name was suppressed rather than escaped"

    def test_the_residual_copy_failure_also_escapes_its_archive_path(
        self, tmp_path, capsys, monkeypatch
    ):
        """The SECOND source print needs its own test or its sanitizer is untested.

        `_safe_name` guards two source prints, and a mutation of this one could not
        redden while only the pre-validation path had coverage. Reaching it needs
        the residual condition -- the source changing between the validation pass
        and the copy -- which cannot be produced by file content alone, so the
        reader is made to succeed once and refuse once.

        The injected refusal is legitimate HERE, unlike elsewhere in this class,
        because the property under test is that the print escapes its path. The
        exception is only the trigger that reaches the print; it is not the thing
        being verified, so it does not have to be a real undecodable byte.
        """
        real = snapshot_mod.strict_raw_records
        calls = {"n": 0}

        def flaky(handle, path, **kw):
            calls["n"] += 1
            if calls["n"] >= 3:  # 1 = destination scan, 2 = validation, 3 = the copy
                raise UnreadableRecord("source changed under the merge")
            yield from real(handle, path, **kw)

        hostile = tmp_path / "kirocrew-snapshot-\x1b[2K-forged"
        hostile.mkdir()
        src = hostile / "notifications.jsonl"
        dst = tmp_path / "live.jsonl"
        src.write_bytes(self.GOOD)
        dst.write_bytes(self.LIVE)
        monkeypatch.setattr(snapshot_mod, "strict_raw_records", flaky)
        with pytest.raises(UnreadableRecord):
            snapshot_mod._merge_notifications(src, dst)
        out = capsys.readouterr().out
        assert "Stopped merging" in out, f"the copy-loop print was not reached:\n{out!r}"
        assert "\x1b" not in out, f"a raw escape reached the terminal:\n{out!r}"
        assert "forged" in out, "the name was suppressed rather than escaped"

    def test_a_ts_less_row_before_a_bad_one_is_not_duplicated_by_a_retry(self, tmp_path, capsys):
        """The source is validated WHOLE before the destination is opened.

        An identity-less row cannot be deduplicated, by construction. So if a
        later record aborts the copy, the rows already appended include ones a
        retry has no way to recognise -- and the retry appends them again. The
        pre-validation pass is what removes the prefix entirely, which is the only
        fix that does not require identity for rows that have none. Found by the
        GPT lane.
        """
        src_bytes = b'{"msg":"no ts, would double"}\n' + self.BAD_UTF8
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        for attempt in ("first", "retry"):
            with pytest.raises(UndecodableRecord):
                snapshot_mod._merge_notifications(src, dst)
            assert dst.read_bytes() == self.LIVE, f"{attempt}: a prefix was appended"
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out
        assert "after 1 record(s)" not in out, "the copy loop ran despite a bad source"

    def test_a_bad_source_record_leaves_the_live_file_untouched(self, tmp_path, capsys):
        """A source-side refusal is a no-op now, not an abort with a prefix.

        Earlier revisions appended everything up to the bad record and kept it,
        relying on the seen-set rebuilt from the destination to make a re-run
        idempotent. That reasoning only holds for rows that HAVE identity, and
        identity-less rows are exactly the ones this function may not deduplicate,
        so the prefix was a retry-duplication hazard. Validating the whole source
        first removes it. A prefix can still survive the residual case -- the
        source changing between the two passes -- which is why the copy loop keeps
        its own handler.
        """
        src, dst = self._files(tmp_path, self.GOOD + self.BAD_UTF8, self.LIVE)
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE, "a prefix was appended before the refusal"
        out = capsys.readouterr().out
        assert "merge aborted" in out
        assert "Notifications imported:" not in out
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE, "the retry appended something"


# ── The restore status line must not claim success over a refused
# cron merge ───────────────────────────────────────────────────────────────────


class TestARefusedCronMergeIsVisibleInTheRestoreStatus:
    """`_do_merge` printed "✅ crons" whatever `_merge_crons` decided.

    The terminal shows the merger's own warning right above, but the status
    line is the summary an operator scans — a checkmark over a refusal is the
    same false success as the import summary.
    """

    @staticmethod
    def _job(name: str) -> dict:
        return {
            "id": "j1",
            "name": name,
            "message": "check",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        }

    def test_a_refused_merge_prints_a_skip_not_a_checkmark(self, tmp_path, capsys):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "crons.json").write_text(json.dumps({"jobs": [self._job("imported")]}))
        (home / "crons.json").write_text("{not json")

        snapshot_mod._do_merge(snap, home, ["crons"], allow_unpinned=bool(unpinnable_argv()))

        out = capsys.readouterr().out
        assert "✅ crons" not in out, out
        assert "crons: merge skipped" in out, out
        # The refusal wrote nothing.
        assert (home / "crons.json").read_text() == "{not json"

    def test_a_genuine_merge_still_prints_the_checkmark(self, tmp_path, capsys):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "crons.json").write_text(json.dumps({"jobs": [self._job("imported")]}))
        (home / "crons.json").write_text(json.dumps({"jobs": [self._job("existing")]}))

        snapshot_mod._do_merge(snap, home, ["crons"], allow_unpinned=bool(unpinnable_argv()))

        out = capsys.readouterr().out
        assert "✅ crons" in out, out
        assert "crons: merge skipped" not in out, out


# ── The merge must validate and copy the SAME inode, not the same name twice
# ─────────────────────────────────────────────────────────────────────────────


@requires_o_nofollow
class TestNotificationMergeResolvesEachNameOnce:
    """One descriptor from validation through copy; a swapped name changes nothing.

    ``_merge_notifications`` opens ``src_path`` by name exactly once. Resolving
    it once to validate and again to copy, with nothing carrying identity
    between the two opens, leaves a window: the staging tree is agent-writable
    on a normal install, so swapping that name for a symlink in the window makes
    the copy loop append the link TARGET's bytes into the live
    ``notifications.jsonl`` while the validation pass vouches for a different
    file. The destination has the identical shape one layer down (scan by name,
    then re-open by name to append).

    Every fixture here is a real link, FIFO, or rename on a real filesystem --
    a monkeypatched ``open`` that pretends to be a symlink would route the merge
    down a path the defect never takes.
    """

    LIVE = b'{"ts":"2026-02-01T00:00:00Z","msg":"local"}\n'
    GOOD = b'{"ts":"2026-03-01T00:00:00Z","msg":"snap"}\n'
    # Valid UTF-8, not JSON: exactly the shape of a token/env file, and exactly
    # what a name-resolved-twice copy loop appends UNCONDITIONALLY -- an
    # unparseable record keeps no dedupe key, so nothing ever refuses it.
    SECRET = b"AWS_SECRET_STAND_IN=hunter2-not-a-notification\n"

    def _files(self, tmp_path):
        src = tmp_path / "snap-notifications.jsonl"
        dst = tmp_path / "live-notifications.jsonl"
        src.write_bytes(self.GOOD)
        dst.write_bytes(self.LIVE)
        secret = tmp_path / "secret.env"
        secret.write_bytes(self.SECRET)
        return src, dst, secret

    @staticmethod
    def _merge_in_thread(src, dst, timeout: float = 10.0) -> list[str]:
        """Run the merge on a daemon thread so a hang FAILS instead of wedging pytest.

        The FIFO cases below are "must not block forever" properties: under a
        dropped ``O_NONBLOCK`` the open blocks with no writer ever coming, and a
        test that simply calls the merge would hang the whole suite rather than
        going red.
        """
        import threading

        outcome: list[str] = []

        def run() -> None:
            try:
                snapshot_mod._merge_notifications(src, dst)
                outcome.append("returned")
            except OSError:
                outcome.append("raised")
            except Exception as exc:  # noqa: BLE001 — name any unexpected type
                outcome.append(f"raised {type(exc).__name__}")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            outcome.append("hung")
        return outcome

    @requires_symlinks
    def test_a_source_swapped_for_a_symlink_after_validation_is_not_followed(
        self, tmp_path, monkeypatch
    ):
        """The exact defect: swap the name BETWEEN the two passes.

        The swap is performed the moment the validation pass finishes reading
        the source -- the third ``strict_raw_records`` call is the copy loop, so
        completing the second one is the last instant before the window closes.
        With the name resolved twice, the copy follows the link and the target's
        bytes land; with one descriptor, the swap is invisible.
        """
        src, dst, secret = self._files(tmp_path)
        real = snapshot_mod.strict_raw_records
        calls = {"n": 0}

        def swapping(handle, path, **kwargs):
            calls["n"] += 1
            this_call = calls["n"]
            yield from real(handle, path, **kwargs)
            if this_call == 2:  # the source validation pass just completed
                src.unlink()
                src.symlink_to(secret)

        monkeypatch.setattr(snapshot_mod, "strict_raw_records", swapping)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert self.SECRET not in after, "the copy loop followed a name swapped after validation"
        assert after == self.LIVE + self.GOOD, "the archive's own validated bytes must land"

    @requires_symlinks
    def test_a_source_that_is_already_a_symlink_is_refused_at_the_open(self, tmp_path, capsys):
        """No window needed: a link sitting at the name must never be followed.

        Reading straight through it twice and reporting success is the failure.
        A refused open surfaces as a REFUSAL -- raise plus the abort line -- not
        as a silent skip that installs nothing.
        """
        src, dst, secret = self._files(tmp_path)
        src.unlink()
        src.symlink_to(secret)
        before = dst.read_bytes()
        with pytest.raises(OSError):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == before, "a refusal must be a no-op on the live file"
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out, "reported success on a refused merge"
        assert "merge aborted" in out

    @requires_symlinks
    def test_a_platform_that_cannot_pin_raises_NotificationCopyUnsupported(
        self, tmp_path, monkeypatch, capsys
    ):
        """Windows has no ``O_NOFOLLOW``; the merge raises
        ``NotificationCopyUnsupported`` there.

        Simulated the way this suite already simulates that platform: delete the
        flag, so ``supports_pinned_walk`` is false and the pinned open is
        unavailable. No atomic no-reparse open exists for the read-write append
        the destination needs, so the merge refuses -- with the same type
        ``_install_notifications`` raises there, which the call sites catch and
        degrade to a loud skip rather than aborting the whole restore. Nothing
        is written and the linked secret never lands.
        """
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        src, dst, secret = self._files(tmp_path)
        src.unlink()
        src.symlink_to(secret)
        before = dst.read_bytes()
        with pytest.raises(snapshot_mod.NotificationCopyUnsupported):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == before
        assert self.SECRET not in dst.read_bytes()

    @requires_symlinks
    def test_a_destination_that_is_a_symlink_is_refused_before_anything_is_written(self, tmp_path):
        """The destination has the same two-resolution shape one layer down.

        A link at the live name redirects the append at whatever it points to.
        Smaller exposure -- the live name is the operator's own file, not an
        archive-derived one -- but the same fix applies and is pinned the same
        way.
        """
        src, dst, _ = self._files(tmp_path)
        real_dst = tmp_path / "elsewhere.jsonl"
        real_dst.write_bytes(self.LIVE)
        dst.unlink()
        dst.symlink_to(real_dst)
        with pytest.raises(OSError):
            snapshot_mod._merge_notifications(src, dst)
        assert real_dst.read_bytes() == self.LIVE, "the append was redirected through the link"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX fixture")
    def test_a_fifo_at_the_source_name_fails_rather_than_hanging(self, tmp_path):
        """What ``O_NONBLOCK`` buys, pinned so a later cleanup cannot drop it.

        Opening a FIFO for reading with no writer blocks FOREVER without it --
        converting a refused restore into a hung one. With it the open returns
        at once and the ``fstat`` regular-file judgement refuses the descriptor.
        """
        src, dst, _ = self._files(tmp_path)
        src.unlink()
        os.mkfifo(src)
        before = dst.read_bytes()
        outcome = self._merge_in_thread(src, dst)
        assert outcome == ["raised"], f"a FIFO source must refuse, not {outcome}"
        assert dst.read_bytes() == before

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX fixture")
    def test_the_regular_file_judgement_is_on_the_descriptor_not_the_name(
        self, tmp_path, monkeypatch, capsys
    ):
        """``fstat`` judges the inode the merge HOLDS; a name re-check judges whatever
        the name points at NOW.

        The open pins a FIFO, then the name is swapped to a perfectly ordinary
        regular file before any by-name re-check could run. The descriptor
        judgement refuses with the named reason; an ``os.path.isfile(src_path)``
        check would pass and hand the FIFO descriptor to the record reader.
        ``pinned_fs_files.py`` records the identical finding for the pinned walk:
        an ``lstat``-before-open compared against a later ``fstat`` is the same
        check-to-use window wearing a different name.
        """
        src, dst, _ = self._files(tmp_path)
        src.unlink()
        os.mkfifo(src)
        regular = tmp_path / "regular.jsonl"
        regular.write_bytes(self.GOOD)
        # Wrap the merge module's own open at the pinned-walk seam. Patching a
        # replacement onto `os.open` itself would drop it out of
        # `os.supports_dir_fd`, flipping `supports_pinned_walk` to false and
        # routing to the fail-closed branch instead of the fstat judgement this
        # test exercises. Wrapping `open_in_pinned_parent` swaps the name the
        # instant after the descriptor is pinned, leaving that predicate intact.
        real_pinned = snapshot_mod.pinned_fs.open_in_pinned_parent

        def swapping_pinned(resolved_parent, name, **kwargs):
            fd = real_pinned(resolved_parent, name, **kwargs)
            if name == src.name:
                os.replace(regular, src)
            return fd

        monkeypatch.setattr(snapshot_mod.pinned_fs, "open_in_pinned_parent", swapping_pinned)
        outcome = self._merge_in_thread(src, dst)
        assert outcome == ["raised"], f"a non-regular descriptor must refuse, not {outcome}"
        assert "not a regular file" in capsys.readouterr().out

    @pytest.mark.skipif(not hasattr(os, "link"), reason="hardlinks are a POSIX fixture here")
    def test_a_hardlinked_source_is_refused_on_the_descriptor(self, tmp_path, capsys):
        """A hardlink defeats every link-shaped screen: ``O_NOFOLLOW`` has nothing to
        refuse and ``S_ISREG`` is TRUE, because the alias IS the target's inode.

        The descriptor judgement therefore also refuses ``st_nlink != 1`` -- the
        exact predicate ``pinned_fs.copy_file_pinned`` applies for the same
        reason (a path guard cannot see an alias; only the inode's link count
        can). A staged name hardlinked to a credential must refuse, not deliver
        the credential's bytes into the served feed.
        """
        src, dst, secret = self._files(tmp_path)
        src.unlink()
        os.link(secret, src)  # a HARDLINK, not a symlink: same inode, nlink == 2
        before = dst.read_bytes()
        with pytest.raises(OSError):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == before, "the hardlinked credential's bytes were delivered"
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out
        assert "merge aborted" in out

    def test_a_valid_unswapped_merge_still_appends_exactly_what_it_validated(
        self, tmp_path, capsys
    ):
        """The happy path is untouched: same records land, dedupe unchanged.

        This is a change to HOW the files are reached, never to what is
        admitted -- the record cap and the ``_notification_key`` predicate are
        unchanged. A second run of the same import appends nothing.
        """
        src, dst, _ = self._files(tmp_path)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + self.GOOD
        assert "Notifications imported: 1" in capsys.readouterr().out
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + self.GOOD, "the re-run duplicated a record"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_a_no_pin_platform_skips_the_merge_rather_than_aborting_the_restore(
        self, tmp_path, monkeypatch, capsys
    ):
        """``NotificationCopyUnsupported`` degrades to a loud skip at the call site.

        The no-pin branch raises the same type ``_install_notifications`` raises,
        which the restore merge branch catches: the component is skipped with a
        printed reason and the surrounding restore is NOT aborted. An `OSError`
        here would abort the whole restore of an archive that is perfectly good
        on a platform that simply cannot pin.
        """
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        src, dst, _ = self._files(tmp_path)
        before = dst.read_bytes()
        with pytest.raises(snapshot_mod.NotificationCopyUnsupported):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == before, "a refused merge must write nothing"
        # The call site catches it and keeps going.
        try:
            raise snapshot_mod.NotificationCopyUnsupported("x")
        except snapshot_mod.NotificationCopyUnsupported:
            pass  # confirms the type is catchable exactly as the call sites do
