"""The nightly can carry transcripts, and only under its own grant.

Before this, ``hooks._run_once`` pushed the memory snapshot and nothing else:
the sessions archive existed and was complete, but only an owner pressing a
button ever ran it, so the conversations were the one part of an install a
nightly never protected.

The three properties the scheduling rests on, and each is a test below:

1. **The grant is its own bit.** ``nightly`` authorizes uploading memory;
   ``nightly_sessions`` authorizes uploading everything the agent was ever
   shown. Neither is ever read for the other, and the new one is absent -- so
   False -- on every install that has not asked for it.
2. **The window is its own window.** Due-ness is keyed on the SESSIONS run
   stamp, so a snapshot that ran an hour ago does not make the transcripts look
   backed up, and a wake proceeds when either kind is due.
3. **The payload is unchanged.** The loop calls ``run_sessions_backup``, the
   same function the owner-triggered archive calls, with the same arguments and
   the scheduled caller. Scheduling an existing mechanism decides nothing new
   about what is in the archive or how it is redacted; those stay properties of
   that function, and of the issue that governs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control import hooks
from kiro_crew.apps.builtins.aws_control.backend import backup

ACCOUNT = "111122223333"


@pytest.fixture(autouse=True)
def _isolated_backup_state(tmp_path, monkeypatch):
    """One fresh state file per test, for every class in this file.

    A consolidation, not a bug fix: two classes below carried an identical
    per-class copy of this, and the two above carried none, so the same isolation
    was stated twice and missing twice. Mutating it away does not redden anything
    today -- the harness already gives each test its own crew home -- so it is
    here to keep that true from one place rather than to repair a live defect.

    The unpersisted-run overlay is cleared as well. Its key begins with the state
    path, so a fresh path isolates it on its own today, but that is a property of
    the key's SHAPE rather than of this fixture.
    """
    monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
    backup._unpersisted_runs.clear()
    yield
    backup._unpersisted_runs.clear()
    backup.clear_stop()


class TestTheTranscriptGrantIsSeparate:
    """One bit per question. An operator who said yes to memory has not said
    yes to conversations, and no code path may infer that they did."""

    def test_the_snapshot_grant_does_not_authorize_transcripts(self):
        # The decisive pin, and the reason this bit exists at all: riding
        # `nightly` would upload transcripts on the strength of a grant that was
        # asked about memory.
        backup.set_nightly(ACCOUNT, True)
        assert backup.nightly_sessions_enabled(ACCOUNT) is False
        assert backup.due_for_sessions_nightly(ACCOUNT) is False

    def test_the_transcript_grant_does_not_authorize_the_snapshot(self):
        # The mirror direction. Two separate bits means separate in BOTH
        # readings, or the asymmetry just moves rather than closing.
        backup.set_nightly_sessions(ACCOUNT, True)
        assert backup.nightly_enabled(ACCOUNT) is False
        assert backup.due_for_nightly(ACCOUNT) is False

    def test_an_install_that_never_asked_is_off(self):
        # Default off is the whole consent posture: an absent key answers False,
        # so upgrading the product never starts uploading anybody's transcripts.
        assert backup.nightly_sessions_enabled(ACCOUNT) is False

    def test_the_grant_is_per_account(self):
        # Switching the default account must not carry one account's transcript
        # grant to another, the same rule the snapshot bit already follows.
        backup.set_nightly_sessions(ACCOUNT, True)
        assert backup.nightly_sessions_enabled("444455556666") is False


class TestTheTranscriptWindowIsItsOwn:
    """Due-ness is keyed on the SESSIONS stamp. A snapshot run is not evidence
    that the transcripts are backed up."""

    @pytest.fixture(autouse=True)
    def _pinning_is_available(self):
        """Hold the traversal capability TRUE while the window is under test.

        ``due_for_sessions_nightly`` answers False unconditionally where
        descriptor-pinned traversal is missing, which is correct and deliberate --
        the archive cannot be produced there at all. Windows is such a platform
        (``os.supports_dir_fd`` and ``os.supports_fd`` are empty), so these three
        assertions cannot hold there and the Windows shard failed on all three.

        Pinning it rather than skipping, because the property these tests exist
        for is which STAMP the window reads, and that has nothing to do with the
        capability. A skip would score as a pass on the one platform where the
        interaction is real, and would leave the window itself unverified there.
        The capability's own effect keeps its own test below, which patches this
        to False and therefore overrides this fixture.
        """
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            yield

    def test_authorized_and_never_run_is_due(self):
        backup.set_nightly_sessions(ACCOUNT, True)
        assert backup.due_for_sessions_nightly(ACCOUNT) is True

    def test_a_recorded_sessions_run_closes_the_window_for_a_day(self):
        backup.set_nightly_sessions(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SESSIONS, "sessions/x.tar.gz", 10)
        assert backup.due_for_sessions_nightly(ACCOUNT) is False
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
        assert backup.due_for_sessions_nightly(ACCOUNT, now=later) is True

    def test_a_snapshot_run_does_not_close_the_transcript_window(self):
        # Keyed on the wrong kind, this reads a snapshot that ran minutes ago as
        # "the transcripts are backed up", and the transcripts are then never
        # uploaded at all while the console reports a nightly that ran.
        backup.set_nightly_sessions(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 10)
        assert backup.due_for_sessions_nightly(ACCOUNT) is True

    def test_a_platform_that_cannot_pin_the_traversal_is_never_due(self):
        # `run_sessions_backup` refuses there by design. Left due, the loop would
        # raise on every wake -- a failed run record and a SEL failure every half
        # hour for a payload that platform cannot produce. `_CAN_PIN_TRAVERSAL`
        # is patched rather than the reason helper, so this pins the real
        # composition and not a stub agreeing with itself.
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            assert backup.kind_unavailable_reason(backup.KIND_SESSIONS) is not None
            assert backup.due_for_sessions_nightly(ACCOUNT) is False


def _run(coro):
    return asyncio.run(coro)


def _loop_patches(*, snapshot_due: bool, sessions_due: bool) -> list:
    """Every guard ``_run_once`` passes before a push, with both due-checks pinned.

    The backup runners are deliberately NOT in here: which of them is expected to
    be called is the assertion in most of these tests, so each test states its
    own. Entered through an ``ExitStack`` because this is a list, and the
    parenthesized ``with`` form cannot unpack one.
    """
    return [
        mock.patch.object(
            hooks.accounts_mod,
            "resolve_default_account_profile",
            AsyncMock(return_value=("p", "us-west-2")),
        ),
        mock.patch.object(
            hooks.aws_consent,
            "probe_identity",
            AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
        ),
        mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=snapshot_due),
        mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=sessions_due),
        mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
        mock.patch.object(hooks.storage_mod, "find_drive", return_value="kirocrew-drive-abc"),
        mock.patch.object(hooks.backup_mod, "other_install_ids", return_value=[]),
    ]


def _drive(*, snapshot_due: bool, sessions_due: bool, **runners):
    """Run ``_run_once`` with the given runner patches, returning them by name.

    ``runners`` maps a ``backup_mod`` attribute name to the mock it should be
    replaced by, so a test names only what it cares about and reads the result
    back under the same name.
    """
    with contextlib.ExitStack() as stack:
        for patcher in _loop_patches(snapshot_due=snapshot_due, sessions_due=sessions_due):
            stack.enter_context(patcher)
        entered = {
            name: stack.enter_context(mock.patch.object(hooks.backup_mod, name, new))
            for name, new in runners.items()
        }
        audit = stack.enter_context(mock.patch.object(hooks, "_audit"))
        _run(hooks._run_once())
    return entered, audit


class TestTheNightlyLoopCarriesTranscripts:
    """The scheduling itself: a due transcript window pushes the same archive
    the owner-triggered path pushes, as the scheduled caller."""

    def test_a_due_transcript_window_pushes_the_sessions_archive(self):
        # Red on base twice over: base has no sessions branch at all, and base
        # returns before consent when the SNAPSHOT is not due -- so a transcript
        # window that opens on a day the snapshot already ran is skipped.
        ran, _audit = _drive(
            snapshot_due=False,
            sessions_due=True,
            run_snapshot_backup=mock.Mock(return_value={"key": "snapshots/x"}),
            run_sessions_backup=mock.Mock(return_value={"key": "sessions/x"}),
        )
        ran["run_snapshot_backup"].assert_not_called()
        ran["run_sessions_backup"].assert_called_once()
        call = ran["run_sessions_backup"].call_args
        assert call.args[0] == ACCOUNT
        # Nobody is at the keyboard: the upload must not be recorded against the
        # dashboard owner, which is what the interactive caller would have done.
        assert call.kwargs["caller"] == backup.CALLER_SCHEDULED

    def test_an_unauthorized_transcript_window_pushes_nothing(self):
        # The grant is what decides. With it off the loop still runs its snapshot
        # and the archive is never produced, so the sensitive payload cannot leave
        # the host on a grant nobody gave.
        ran, _audit = _drive(
            snapshot_due=True,
            sessions_due=False,
            run_snapshot_backup=mock.Mock(return_value={"key": "snapshots/x"}),
            run_sessions_backup=mock.Mock(),
        )
        ran["run_snapshot_backup"].assert_called_once()
        ran["run_sessions_backup"].assert_not_called()

    def test_neither_due_asks_nobody_to_spend(self):
        # Both windows closed must return before consent, so a nightly-disabled
        # account is never even asked to spend money. Keeps the existing
        # early-return property now that two bits feed it.
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=False),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=False),
            mock.patch.object(hooks.aws_consent, "refuse_and_log") as refuse,
        ):
            _run(hooks._run_once())
        refuse.assert_not_called()

    def test_a_failed_snapshot_does_not_cost_the_transcripts_their_window(self):
        # The reason each kind owns its try/except. Sharing one handler turns a
        # snapshot failure into a second skipped night for a payload that had
        # nothing to do with it.
        ran, audit = _drive(
            snapshot_due=True,
            sessions_due=True,
            run_snapshot_backup=mock.Mock(side_effect=RuntimeError("no disk")),
            run_sessions_backup=mock.Mock(return_value={"key": "sessions/x"}),
        )
        ran["run_snapshot_backup"].assert_called_once()
        ran["run_sessions_backup"].assert_called_once()
        assert [c.args[2] for c in audit.call_args_list] == [
            "invoked",
            "failed",
            "invoked",
            "succeeded",
        ]

    def test_a_failed_archive_does_not_cost_the_snapshot_its_window(self):
        # The mirror direction, and the one that matters for cost: the archive is
        # the large payload, so it is the likelier one to time out, and a timeout
        # there must not stop the small backup that would have succeeded.
        ran, audit = _drive(
            snapshot_due=True,
            sessions_due=True,
            run_snapshot_backup=mock.Mock(return_value={"key": "snapshots/x"}),
            run_sessions_backup=mock.Mock(side_effect=RuntimeError("timed out")),
        )
        ran["run_snapshot_backup"].assert_called_once()
        ran["run_sessions_backup"].assert_called_once()
        assert [c.args[2] for c in audit.call_args_list] == [
            "invoked",
            "succeeded",
            "invoked",
            "failed",
        ]

    def test_the_transcript_push_is_audited_under_its_own_subject(self):
        # An unattended upload of the most sensitive payload in the product must
        # be distinguishable in the SEL trail from the snapshot beside it, or an
        # incident review cannot tell which bytes left the host.
        _ran, audit = _drive(
            snapshot_due=False,
            sessions_due=True,
            run_sessions_backup=mock.Mock(side_effect=RuntimeError("denied")),
        )
        assert [c.args[1] for c in audit.call_args_list] == [
            "backup/sessions",
            "backup/sessions",
        ]

    def test_a_cancel_during_the_archive_is_audited_and_reraised(self):
        # Teardown must stop the loop cleanly AND leave a trail that it was
        # interrupted -- and a cancel is never swallowed as a failure.
        with contextlib.ExitStack() as stack:
            for patcher in _loop_patches(snapshot_due=False, sessions_due=True):
                stack.enter_context(patcher)
            stack.enter_context(
                mock.patch.object(
                    hooks.backup_mod,
                    "run_sessions_backup",
                    side_effect=asyncio.CancelledError(),
                )
            )
            audit = stack.enter_context(mock.patch.object(hooks, "_audit"))
            with pytest.raises(asyncio.CancelledError):
                _run(hooks._run_once())
        assert [c.args[2] for c in audit.call_args_list] == ["invoked", "cancelled"]


class TestAGrantCountsOnlyWhenItIsARealYes:
    """A consent bit answers True for the value the product writes, and for
    nothing else.

    ``_account_view`` flattens a corrupt level to empty and
    ``_a_day_since_last_run`` survives a non-string stamp, so this file already
    treats a malformed state file as something to withstand rather than something
    that cannot occur. Inside that same class of damage a truthy non-bool would
    read as consent GRANTED -- the string ``"false"`` is the cheap example -- which
    turns a bit documented as fail-closed into the opposite. Only the two setters
    write these keys and both store a real bool, so nothing the product produces
    is rejected by the stricter read.
    """

    def _store(self, key: str, value: object) -> None:
        def mutate(state):
            backup._account_state(state, ACCOUNT)[key] = value

        backup._locked_state_update(mutate)

    @pytest.mark.parametrize("stored", ["false", "0", "no", 1, [0], {"a": 1}])
    def test_a_truthy_non_bool_does_not_authorize_transcripts(self, stored):
        # The decisive case. `bool("false")` is True, so a state file holding the
        # STRING would have started uploading conversations unattended on an
        # install whose owner never granted it.
        self._store("nightly_sessions", stored)
        assert backup.nightly_sessions_enabled(ACCOUNT) is False

    @pytest.mark.parametrize("stored", ["false", "0", 1, [0]])
    def test_a_truthy_non_bool_does_not_authorize_the_snapshot(self, stored):
        # The mirror. One strict reader and one loose reader of the same kind of
        # answer is the shape that drifts: whichever stays loose becomes the way
        # in, so both bits read through the same helper.
        self._store("nightly", stored)
        assert backup.nightly_enabled(ACCOUNT) is False

    def test_the_value_the_product_writes_still_reads_as_granted(self):
        # Without this the stricter read could be a blanket break and look
        # correct: every test above would pass against a reader that answers
        # False unconditionally.
        backup.set_nightly_sessions(ACCOUNT, True)
        backup.set_nightly(ACCOUNT, True)
        assert backup.nightly_sessions_enabled(ACCOUNT) is True
        assert backup.nightly_enabled(ACCOUNT) is True


def _walk_by_name(monkeypatch):
    """Stand in for the descriptor-pinned walk so the gate is tested everywhere.

    The real walk opens each directory with ``O_DIRECTORY | O_NOFOLLOW`` and keeps
    a descriptor, which is what makes it safe against a junction swapped in while
    the archive is being written -- and which needs ``os.supports_dir_fd``, absent
    on Windows. The subject here is the authorization gate that runs AFTER the
    archive is built, so the walk is replaced rather than the test skipped: a skip
    leaves the gate unverified on the very platform whose missing capability
    caused it, which is the opposite of what a platform guard is for.
    """

    def by_name(tar, root, arc_prefix):
        if not root.is_dir():
            return 0
        added = 0
        for entry in sorted(root.rglob("*")):
            if entry.is_file():
                rel = entry.relative_to(root).as_posix()
                tar.add(str(entry), arcname=f"{arc_prefix}/{rel}")
                added += 1
        return added

    monkeypatch.setattr(backup, "_add_tree", by_name)


def _authorized_env(monkeypatch):
    """Every gate in ``_authorize_upload`` satisfied except the unattended bit.

    So a refusal in the tests below can only have come from the check under
    test. Each entry mirrors one check in that function, in its order.

    The traversal capability is pinned because it is one of those gates: a host
    without descriptor-pinned traversal refuses a scheduled transcript upload for
    that reason alone, which on such a host would answer these tests with the
    wrong check and hide whichever one they are actually about.
    """
    from types import SimpleNamespace

    backup.clear_stop()
    monkeypatch.setattr(backup, "_CAN_PIN_TRAVERSAL", True)
    monkeypatch.setattr(
        "kiro_crew.deploy.engine._checked",
        lambda *a, **k: '{"Account": "%s"}' % ACCOUNT,
    )
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda *a, **k: True)
    monkeypatch.setattr(aws_consent, "is_granted", lambda *a, **k: (True, ""))
    monkeypatch.setattr(aws_consent, "read_grant", lambda *a, **k: SimpleNamespace(account=ACCOUNT))


class TestTheGrantIsRereadBeforeTheBytesLeave:
    """Withdrawing the grant during the build stops the upload.

    The archive is built for minutes in a worker thread and the grant is a
    switch in the dashboard, so the two overlap by construction. The gate
    already re-reads the live account, the app's enabled state, the S3 consent
    and that consent's account across exactly this window -- which is the
    evidence the window is real and known. The unattended bit was the one
    omitted, and it is the only one whose withdrawal cannot be undone after the
    fact: transcripts on S3 cannot be recalled.
    """

    def test_a_withdrawn_transcript_grant_refuses_the_scheduled_upload(self, monkeypatch):
        # The finding itself: the nightly decided the run was due, the build
        # took minutes, the owner turned it off in between, and the bytes must
        # not leave.
        _authorized_env(monkeypatch)
        with pytest.raises(RuntimeError, match="no longer holds"):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_SCHEDULED,
                payload_kind=backup.KIND_SESSIONS,
            )

    def test_a_held_transcript_grant_still_allows_the_scheduled_upload(self, monkeypatch):
        # The allow direction, without which the refusal above is satisfied by a
        # gate that blocks every nightly -- a break, not a fence.
        _authorized_env(monkeypatch)
        backup.set_nightly_sessions(ACCOUNT, True)
        backup._authorize_upload(
            ACCOUNT,
            "p",
            "us-west-2",
            caller=backup.CALLER_SCHEDULED,
            payload_kind=backup.KIND_SESSIONS,
        )  # no raise

    def test_a_withdrawn_snapshot_grant_refuses_the_scheduled_upload(self, monkeypatch):
        # Both bits, or the asymmetry just moves: the snapshot payload sits in
        # the same build window under its own grant.
        _authorized_env(monkeypatch)
        with pytest.raises(RuntimeError, match="no longer holds"):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_SCHEDULED,
                payload_kind=backup.KIND_SNAPSHOT,
            )

    def test_neither_kind_is_authorized_by_the_other_at_the_gate(self, monkeypatch):
        # The separation the whole change rests on, asserted where the bytes
        # actually leave rather than only at the scheduling decision.
        _authorized_env(monkeypatch)
        backup.set_nightly_sessions(ACCOUNT, True)
        with pytest.raises(RuntimeError, match="no longer holds"):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_SCHEDULED,
                payload_kind=backup.KIND_SNAPSHOT,
            )

    def test_an_owner_triggered_upload_does_not_need_the_unattended_bit(self, monkeypatch):
        # An owner who pressed the button authorized the run by pressing it. The
        # nightly bit stands in for a person who is not there, so reading it on
        # the interactive path would refuse the very action being requested.
        _authorized_env(monkeypatch)
        assert backup.nightly_sessions_enabled(ACCOUNT) is False
        backup._authorize_upload(
            ACCOUNT,
            "p",
            "us-west-2",
            caller=backup.CALLER_OWNER,
            payload_kind=backup.KIND_SESSIONS,
        )  # no raise

    def test_a_kind_with_no_registered_grant_is_refused(self, monkeypatch):
        # Fail closed on the unlisted kind. A kind added without its bit must be
        # refused loudly rather than uploaded unattended under no authorization
        # at all, which is what falling through would have meant.
        _authorized_env(monkeypatch)
        with pytest.raises(RuntimeError, match="no unattended grant is defined"):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_SCHEDULED,
                payload_kind="transcripts-v2",
            )

    def test_the_caption_write_consults_no_kind_grant(self, monkeypatch):
        # `payload_kind=None` is an answer, not an opt-out: the label is one
        # document published under both prefixes on purpose, and keying it to a
        # grant would cost an install with one kind off that prefix's caption --
        # a rename going unseen, not a transcript leaving the machine.
        _authorized_env(monkeypatch)
        assert backup.nightly_enabled(ACCOUNT) is False
        assert backup.nightly_sessions_enabled(ACCOUNT) is False
        backup._authorize_upload(
            ACCOUNT,
            "p",
            "us-west-2",
            caller=backup.CALLER_SCHEDULED,
            payload_kind=None,
        )  # no raise

    def test_every_scheduled_kind_has_a_grant_of_its_own(self):
        # Derived from the constants, not written out, so adding a kind without a
        # grant reddens here instead of reaching the gate and being refused at
        # 03:00 with no one reading the log.
        assert set(backup._NIGHTLY_CONSENT_READERS) == set(backup.JOB_KINDS)
        assert len(set(backup._NIGHTLY_CONSENT_READERS.values())) == len(backup.JOB_KINDS)

    def test_the_real_transcript_push_stops_at_the_gate(self, tmp_path, monkeypatch):
        # The gate is only worth anything if the real path reaches it. This drives
        # `run_sessions_backup` with the REAL gate and the grant withdrawn: the
        # archive is built, and nothing is uploaded.
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: tmp_path / "absent_cli")
        _authorized_env(monkeypatch)
        _walk_by_name(monkeypatch)

        with mock.patch.object(backup.storage, "put_file") as put_file:
            with pytest.raises(RuntimeError, match="no longer holds"):
                backup.run_sessions_backup(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    "bkt",
                    caller=backup.CALLER_SCHEDULED,
                )
        put_file.assert_not_called()


class TestAnOptedInRedactionStopsTheUnattendedUpload:
    """An operator who asked for outbound redaction gets no unattended transcript
    upload, because this payload cannot honour the request.

    Redaction is opt-IN and off by default, and that default is a documented
    trade: the destination is owner-only and re-verified at every upload, so
    hardening protects the payload and redaction is an extra rewrite. The narrow
    asymmetry is that ``run_snapshot_backup`` routes its payload through
    ``snapshot.prepare_redacted_copy`` and honours the switch, while the sessions
    archive cannot use that seam -- it refuses more than one root and this archive
    has ``crew`` and ``cli``. So for that operator the snapshot would leave
    redacted and the transcripts unredacted, and transcripts are the payload most
    likely to carry a pasted secret.
    """

    def _switch(self, monkeypatch, *, enabled=None, raises=False):
        from kiro_crew import snapshot_redact

        def answer():
            if raises:
                raise snapshot_redact.RedactionSwitchUnreadable("switch.json holds a JSON list")
            return enabled

        monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", answer)

    def test_redaction_off_leaves_the_nightly_due(self, monkeypatch):
        # The default, and the case that must not regress: nothing is being asked
        # for, so nothing is withheld.
        self._switch(monkeypatch, enabled=False)
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            assert backup._unattended_sessions_redaction_gap() is None
            assert backup.due_for_sessions_nightly(ACCOUNT) is True

    def test_redaction_on_withholds_the_nightly(self, monkeypatch):
        # The finding itself: rather than upload transcripts unredacted while the
        # snapshot beside them is redacted, the nightly declines.
        self._switch(monkeypatch, enabled=True)
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            assert backup._unattended_sessions_redaction_gap() is not None
            assert backup.due_for_sessions_nightly(ACCOUNT) is False

    def test_an_unreadable_switch_withholds_the_nightly(self, monkeypatch, caplog):
        # Neither silent answer is honest, and unattended is where nobody is
        # present to notice a guess, so it declines.
        #
        # Two halves, because this string is console copy now: the owner reads it
        # under the nightly switch, so it carries no exception repr, and the
        # detail a person diagnosing this needs goes to the log instead.
        self._switch(monkeypatch, raises=True)
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with caplog.at_level(logging.WARNING):
                gap = backup._unattended_sessions_redaction_gap()
            assert gap is not None
            assert "switch.json" not in gap
            assert "Traceback" not in gap and "Error" not in gap
            assert "switch.json" in caplog.text
            assert backup.due_for_sessions_nightly(ACCOUNT) is False

    def test_the_snapshot_nightly_is_untouched_by_the_gap(self, monkeypatch):
        # The snapshot honours the switch through its own redaction seam, so this
        # refusal must not reach it. Withholding both would take away a backup
        # that IS redacted, which is the opposite of what the operator asked for.
        self._switch(monkeypatch, enabled=True)
        backup.set_nightly(ACCOUNT, True)
        assert backup.due_for_nightly(ACCOUNT) is True

    def test_the_owner_triggered_archive_is_untouched_by_the_gap(self, monkeypatch):
        # Read on the SCHEDULED path only. An owner pressing the button is present
        # and choosing this archive knowingly, and that path shipped before the
        # nightly existed -- reading the switch at `kind_unavailable_reason` would
        # have taken a working button away from them.
        self._switch(monkeypatch, enabled=True)
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            assert backup.kind_unavailable_reason(backup.KIND_SESSIONS) is None


class TestASetupFailureIsAuditedAgainstWhatWasActuallyDue:
    """A failure before any push names the kinds this wake was for.

    The setup block -- drive discovery and the shared-drive note -- runs whenever
    EITHER kind is due, so a transcripts-only wake reaches it too. A fixed
    ``backup/snapshots`` subject there is therefore a record of a snapshot that
    was never due, and the SEL trail is append-only, so nothing later corrects it.
    """

    def _fail_setup(self, *, snapshot_due, sessions_due, exc):
        with contextlib.ExitStack() as stack:
            for patcher in _loop_patches(snapshot_due=snapshot_due, sessions_due=sessions_due):
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(hooks.storage_mod, "find_drive", side_effect=exc))
            audit = stack.enter_context(mock.patch.object(hooks, "_audit"))
            if isinstance(exc, asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    _run(hooks._run_once())
            else:
                _run(hooks._run_once())
        return [c.args[1] for c in audit.call_args_list]

    def test_a_transcripts_only_wake_does_not_file_its_failure_as_a_snapshot(self):
        # The finding itself. Before the fix this recorded "backup/snapshots" on a
        # run the snapshot was never due for.
        subjects = self._fail_setup(
            snapshot_due=False, sessions_due=True, exc=RuntimeError("tagging API down")
        )
        assert subjects == ["backup/sessions"]

    def test_a_snapshot_only_wake_still_files_against_the_snapshot(self):
        # The direction that was already right must stay right, or the fix is a
        # swap rather than a correction.
        subjects = self._fail_setup(
            snapshot_due=True, sessions_due=False, exc=RuntimeError("tagging API down")
        )
        assert subjects == ["backup/snapshots"]

    def test_a_wake_for_both_kinds_names_both(self):
        # One failure stopped two due payloads, so both are owed a record. A
        # single entry would leave the other kind looking like it never came due.
        subjects = self._fail_setup(
            snapshot_due=True, sessions_due=True, exc=RuntimeError("tagging API down")
        )
        assert subjects == ["backup/snapshots", "backup/sessions"]

    def test_a_cancel_during_setup_is_attributed_the_same_way(self):
        # Teardown takes the other handler, and it carried the identical hardcoded
        # subject, so fixing only the exception path would leave the same false
        # record reachable by cancelling.
        subjects = self._fail_setup(
            snapshot_due=False, sessions_due=True, exc=asyncio.CancelledError()
        )
        assert subjects == ["backup/sessions"]

    def test_the_subject_helper_is_the_only_spelling(self):
        # Two copies of this expression is how a setup failure and its push end up
        # filed under different subjects for the same kind. Derived from the
        # constants so a kind added later cannot quietly miss a mapping.
        for kind in backup.JOB_KINDS:
            assert hooks._audit_subject(kind) == f"backup/{backup.KIND_SUBPATHS[kind]}"


class TestAWithheldTranscriptNightlySaysWhy:
    """Turning the grant on and getting nothing must not be silent."""

    def test_the_redaction_gap_is_logged_when_it_withholds_the_run(self, caplog):
        # `due_for_sessions_nightly` answering False is the correct scheduling
        # decision, but on its own it leaves an operator who turned transcripts ON
        # watching a nightly that never runs with no statement of which of their
        # settings withheld it.
        with contextlib.ExitStack() as stack:
            for patcher in _loop_patches(snapshot_due=False, sessions_due=False):
                stack.enter_context(patcher)
            stack.enter_context(
                mock.patch.object(hooks.backup_mod, "nightly_sessions_enabled", return_value=True)
            )
            stack.enter_context(
                mock.patch.object(
                    hooks.backup_mod,
                    "_unattended_sessions_redaction_gap",
                    return_value="redaction is on and this payload cannot be redacted yet",
                )
            )
            with caplog.at_level("INFO", logger=hooks.logger.name):
                _run(hooks._run_once())
        assert "transcripts withheld" in caplog.text
        assert "cannot be redacted" in caplog.text

    def test_nothing_is_logged_when_the_grant_is_off(self, caplog):
        # An install that never asked for transcripts is not withholding anything,
        # so a line here would be noise on every wake of every such install.
        with contextlib.ExitStack() as stack:
            for patcher in _loop_patches(snapshot_due=False, sessions_due=False):
                stack.enter_context(patcher)
            stack.enter_context(
                mock.patch.object(hooks.backup_mod, "nightly_sessions_enabled", return_value=False)
            )
            with caplog.at_level("INFO", logger=hooks.logger.name):
                _run(hooks._run_once())
        assert "transcripts withheld" not in caplog.text


class TestAGrantedButIdleNightlyIsReported:
    """Granting the nightly on a host that cannot run it must not read as running.

    The grant and the ability to act on it are different answers, and reporting
    only the grant is what makes the failure silent: the console shows transcripts
    scheduled, nothing is ever produced, and the gap surfaces at the host loss the
    feature exists to survive. ``scheduled_sessions_blocked_reason`` is the one
    predicate both the scheduler and the status route read, so a surface cannot
    claim this is running while the loop withholds it.
    """

    def test_an_unsupported_host_reports_its_capability_reason(self):
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            reason = backup.scheduled_sessions_blocked_reason()
        assert reason is not None
        assert reason == backup.kind_unavailable_reason(backup.KIND_SESSIONS) or "pinned" in reason

    def test_a_supported_host_with_nothing_in_the_way_reports_none(self):
        # Without this the helper could be a constant refusal and every test above
        # would still pass, which would withhold the nightly everywhere.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value=None):
                assert backup.scheduled_sessions_blocked_reason() is None

    def test_the_redaction_gap_is_reported_when_the_host_is_capable(self, monkeypatch):
        # Both causes reach the same reader, so a surface needs only one field.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with mock.patch.object(
                backup, "_unattended_sessions_redaction_gap", return_value="redaction is on"
            ):
                assert backup.scheduled_sessions_blocked_reason() == "redaction is on"

    def test_the_capability_reason_wins_over_the_redaction_gap(self):
        # Ordered deliberately: the capability is a property of the machine that no
        # setting changes, while the redaction gap is something the owner can act
        # on. Reporting the actionable one first would tell them to change a
        # setting that cannot help.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            with mock.patch.object(
                backup, "_unattended_sessions_redaction_gap", return_value="redaction is on"
            ):
                assert backup.scheduled_sessions_blocked_reason() != "redaction is on"

    def test_the_scheduler_and_the_reported_reason_agree(self):
        # The property that matters, asserted directly rather than inferred from
        # the two call sites looking alike: whenever a reason is reported, the
        # nightly is not due, and the grant being on cannot change that.
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            assert backup.scheduled_sessions_blocked_reason() is not None
            assert backup.due_for_sessions_nightly(ACCOUNT) is False


class TestRedactionIsRereadBeforeTheBytesLeave:
    """Turning redaction on during the build stops the upload.

    The same window the grant re-read defends, for the other precondition a
    scheduled transcript upload stands on. The due-check reads the redaction gap
    and the archive then takes minutes to build, so an operator who turns
    redaction on in between has asked for scrubbing that the already-built
    archive does not have -- the sessions payload has no redaction seam, which is
    why the nightly is withheld when redaction is on at all. Sending it anyway is
    unrecoverable in the way that matters: transcripts on S3 cannot be recalled.
    """

    def test_redaction_turned_on_during_the_build_refuses_the_upload(self, monkeypatch):
        # The finding: due-check saw redaction off, the build ran, the operator
        # turned it on, and the bytes must not leave.
        _authorized_env(monkeypatch)
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(
            backup, "_unattended_sessions_redaction_gap", return_value="redaction is on"
        ):
            with pytest.raises(RuntimeError, match="no longer allowed here"):
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_SCHEDULED,
                    payload_kind=backup.KIND_SESSIONS,
                )

    def test_redaction_left_off_still_allows_the_upload(self, monkeypatch):
        # The allow direction, without which the refusal above is satisfied by a
        # gate that blocks every nightly transcript upload.
        _authorized_env(monkeypatch)
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value=None):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_SCHEDULED,
                payload_kind=backup.KIND_SESSIONS,
            )  # no raise

    def test_an_owner_triggered_transcript_upload_is_not_refused(self, monkeypatch):
        # Scheduled path only, same as the due-check. Somebody who pressed the
        # button is present and chose this archive knowing what it holds, and
        # taking their working button away is not what this finding asks for.
        _authorized_env(monkeypatch)
        with mock.patch.object(
            backup, "_unattended_sessions_redaction_gap", return_value="redaction is on"
        ):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_OWNER,
                payload_kind=backup.KIND_SESSIONS,
            )  # no raise

    def test_the_snapshot_payload_is_not_refused_by_the_transcript_gap(self, monkeypatch):
        # Scoped to the transcript kind. The snapshot has its own redaction seam
        # and honours the switch through it, so refusing it here would withhold a
        # payload that can be, and is, scrubbed.
        _authorized_env(monkeypatch)
        backup.set_nightly(ACCOUNT, True)
        with mock.patch.object(
            backup, "_unattended_sessions_redaction_gap", return_value="redaction is on"
        ):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_SCHEDULED,
                payload_kind=backup.KIND_SNAPSHOT,
            )  # no raise

    def test_an_unavailable_host_also_refuses_at_the_gate(self, monkeypatch):
        # The gate reads the same predicate as the due-check, so every cause that
        # withholds the run withholds the upload too. Asserted so the shared
        # predicate is the tested property and not a coincidence of two call
        # sites looking alike.
        _authorized_env(monkeypatch)
        backup.set_nightly_sessions(ACCOUNT, True)
        monkeypatch.setattr(backup, "_CAN_PIN_TRAVERSAL", False)
        with pytest.raises(RuntimeError, match="no longer allowed here"):
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_SCHEDULED,
                payload_kind=backup.KIND_SESSIONS,
            )


class TestTheBlockedAnswerIsACodeAndProse:
    """One decision, two renderings, and they cannot name different conditions.

    The console has to say this in the reader's language, so the route carries a
    stable token rather than a sentence; the log and the upload refusal want prose.
    Deriving the prose from the code is what keeps them describing the same thing.
    """

    def test_an_unsupported_host_answers_with_its_code(self):
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            assert backup.scheduled_sessions_blocked_code() == backup.BLOCK_HOST_UNSUPPORTED

    def test_the_redaction_gap_answers_with_its_code(self):
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value="on"):
                assert backup.scheduled_sessions_blocked_code() == backup.BLOCK_REDACTION_ON

    def test_nothing_in_the_way_answers_with_none(self):
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value=None):
                assert backup.scheduled_sessions_blocked_code() is None
                assert backup.scheduled_sessions_blocked_reason() is None

    def test_the_code_and_the_prose_never_disagree(self):
        # The property, asserted over both conditions rather than trusting that two
        # functions happen to read the same things in the same order.
        for pinned, gap, want in [
            (False, None, backup.BLOCK_HOST_UNSUPPORTED),
            (False, "on", backup.BLOCK_HOST_UNSUPPORTED),
            (True, "on", backup.BLOCK_REDACTION_ON),
            (True, None, None),
        ]:
            with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", pinned):
                with mock.patch.object(
                    backup, "_unattended_sessions_redaction_gap", return_value=gap
                ):
                    code = backup.scheduled_sessions_blocked_code()
                    reason = backup.scheduled_sessions_blocked_reason()
                    assert code == want
                    assert (code is None) == (reason is None)

    def test_the_code_carries_no_prose(self):
        # It reaches a console that looks it up in a catalog, so a sentence here
        # would be English on every install no matter what the reader set.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            code = backup.scheduled_sessions_blocked_code()
        assert code is not None and " " not in code

    def test_an_account_the_schedule_does_not_visit_answers_with_its_code(self):
        # The grant is settable on any registered account while the loop runs for
        # one of them, so "granted here" and "will run here" are different answers
        # and the second is the one an operator cannot otherwise see.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value=None):
                code = backup.scheduled_sessions_blocked_code(scheduled_account=False)
        assert code == backup.BLOCK_OTHER_ACCOUNT

    def test_a_condition_of_the_host_outranks_the_account(self):
        # A host that cannot produce the archive cannot produce it for the default
        # account either, so naming the account would send the operator to a page
        # where the same notice is waiting.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            code = backup.scheduled_sessions_blocked_code(scheduled_account=False)
        assert code == backup.BLOCK_HOST_UNSUPPORTED

    def test_the_scheduling_answer_can_never_be_the_account_one(self):
        # Pins the assumption the prose function rests on: it asks the SCHEDULING
        # question, which the loop only ever asks for the account it just resolved,
        # so the account condition is unreachable there. If that stops holding this
        # reddens instead of the prose silently going empty beside a live code.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value=None):
                assert backup.scheduled_sessions_blocked_code() is None
                assert backup.scheduled_sessions_blocked_reason() is None
            with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value="on"):
                assert backup.scheduled_sessions_blocked_code() == backup.BLOCK_REDACTION_ON

    def test_the_account_condition_does_not_change_whether_a_run_is_due(self):
        # due_for_sessions_nightly runs for the account the loop resolved, so the
        # new condition must not reach it: a default-account nightly stays due.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", True):
            with mock.patch.object(backup, "_unattended_sessions_redaction_gap", return_value=None):
                with mock.patch.object(backup, "nightly_sessions_enabled", return_value=True):
                    with mock.patch.object(backup, "_a_day_since_last_run", return_value=True):
                        assert backup.due_for_sessions_nightly("111122223333") is True
