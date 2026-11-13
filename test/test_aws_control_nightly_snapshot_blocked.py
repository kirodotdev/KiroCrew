"""The snapshot nightly says when it is granted here and will never run.

The sibling of ``test_aws_control_nightly_sessions``'s blocked-code class, for the
snapshot kind. ``set_nightly`` records the grant per ACCOUNT while the nightly
loop resolves ONE account (the default), so an operator who switches the snapshot
on while viewing a non-default account gets a toggle that reads back on and a
schedule that never visits it. The sessions kind already reports this through
``scheduled_sessions_blocked_code``; this pins the snapshot mirror.

The snapshot kind carries exactly ONE condition where the sessions kind carries
three. It has no capability gate -- ``kind_unavailable_reason`` answers ``None``
for it on every platform -- and no redaction gap, because the snapshot payload has
its own redaction seam and honours the switch rather than standing down. So the
only way a granted snapshot nightly is idle is a grant recorded on an account the
loop does not visit, and ``due_for_nightly`` is deliberately unchanged: the loop
reads the blocked code for the account it just resolved, where ``scheduled_account``
is true by construction.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup

ACCOUNT = "111122223333"


@pytest.fixture(autouse=True)
def _isolated_backup_state(tmp_path, _floor_monkeypatch):
    """One fresh state file per test, so a grant set here cannot leak.

    Patches through ``_floor_monkeypatch`` rather than the shared ``monkeypatch``:
    an autouse fixture on the shared stack is lifted by any test that calls
    ``monkeypatch.undo()``, and the floor fixture is undone independently (D11).
    """
    _floor_monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
    backup._unpersisted_runs.clear()
    yield
    backup._unpersisted_runs.clear()
    backup.clear_stop()


class TestTheSnapshotBlockedAnswerIsACode:
    """One stable token the console looks up in the reader's language, never a
    sentence chosen here that would be English on every install."""

    def test_the_default_account_with_nothing_in_the_way_answers_none(self):
        # The loop's own reading: it asks for the account it resolved, so
        # scheduled_account is true and the snapshot is reported as running.
        # Without this the helper could be a constant refusal and every other
        # test here would still pass while withholding the nightly everywhere.
        assert backup.scheduled_snapshot_blocked_code(scheduled_account=True) is None

    def test_an_account_the_schedule_does_not_visit_answers_with_its_code(self):
        # The finding itself: the grant is settable on any registered account
        # while the loop runs for one of them, so "granted here" and "will run
        # here" are different answers and the second is the one an operator cannot
        # otherwise see until the host is gone.
        assert (
            backup.scheduled_snapshot_blocked_code(scheduled_account=False)
            == backup.BLOCK_OTHER_ACCOUNT
        )

    def test_the_code_carries_no_prose(self):
        # It reaches a console that looks it up in a catalogue, so a space-bearing
        # sentence would be untranslatable copy frozen in English.
        code = backup.scheduled_snapshot_blocked_code(scheduled_account=False)
        assert code is not None and " " not in code

    def test_the_snapshot_kind_has_no_capability_or_redaction_block(self):
        # The snapshot kind's scope is one condition. It never reports the
        # sessions-only blocks, so a surface rendering this code never needs the
        # host or redaction strings for the snapshot switch. Pinned against the
        # real predicate -- with the traversal capability absent and a redaction
        # gap present, both of which block the SESSIONS kind -- so the snapshot's
        # independence is a tested property rather than an assumption.
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            with mock.patch.object(
                backup, "_unattended_sessions_redaction_gap", return_value="redaction is on"
            ):
                assert backup.scheduled_snapshot_blocked_code(scheduled_account=True) is None
                assert (
                    backup.scheduled_snapshot_blocked_code(scheduled_account=False)
                    == backup.BLOCK_OTHER_ACCOUNT
                )


class TestTheAccountConditionDoesNotChangeDueness:
    """The new condition is a reporting answer only. ``due_for_nightly`` runs for
    the account the loop resolved, so it must stay exactly as it was."""

    def test_a_default_account_snapshot_nightly_stays_due(self):
        # The loop never passes scheduled_account=False, so adding the blocked
        # code must not reach the due-check: a default-account nightly granted and
        # within its window is still due.
        backup.set_nightly(ACCOUNT, True)
        assert backup.due_for_nightly(ACCOUNT) is True

    def test_the_grant_being_on_for_a_second_account_does_not_make_it_due_there(self):
        # due_for_nightly keys on the grant and the day gap for the account asked
        # about; the blocked code is a separate, surface-only answer. The grant on
        # a second account is honestly recorded, and the loop simply never asks
        # about that account -- the whole point the console now surfaces.
        backup.set_nightly("444455556666", True)
        # The loop resolves the default account; a grant on another one is not its
        # concern, and due_for_nightly does not consult the blocked code.
        assert backup.due_for_nightly(ACCOUNT) is False
