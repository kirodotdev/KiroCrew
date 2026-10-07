"""Two guards on the guest's lifecycle-hook listener.

The listener answers the platform's hooks and binds every interface, and it
cannot authenticate who called it: the platform presents no credential a guest
could check. So each handler has to be safe for a stranger to call, at any moment
in the guest's life, twice at once.

These are the two places where that was not true.

**A terminate during bootstrap.** The guest decided whether to hand its managed
node back by looking for a live supervisor process. That reading is right and the
question is wrong: it cannot tell a crew that has STOPPED from one that has not
STARTED yet, and the gap between the node registering and the supervisor coming
up holds two Secrets Manager reads, the shared-credential wait and the bundle
install. A terminate in that gap deregistered the node of a crew that was about
to serve, and nothing reaches a crew whose node is gone.

**Two ``/run`` calls at once.** The single-shot marker is written at the END of
the bootstrap and checked at the start, so two calls arriving before it exists
both passed. Re-reading the marker does not help -- both read a true value. Only
one of them may ACT on it, which is a lock.
"""

from __future__ import annotations

import json
import threading

import pytest
from container.microvm import hooks


@pytest.fixture()
def guest_state(tmp_path, monkeypatch):
    """The guest's own state directory, redirected into a temp tree.

    Requested by name rather than autouse: a test that forgets it would write to
    the real ``/var/lib/microvm-guest``, and the fixture that silently prevents
    that is the one nobody notices is missing.
    """
    monkeypatch.setattr(hooks, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(hooks, "RUN_MARKER", str(tmp_path / "run.done"))
    monkeypatch.setattr(hooks, "BOOT_STATE", str(tmp_path / "boot.json"))
    return tmp_path


def _boot(guest_state, **data) -> None:
    (guest_state / "boot.json").write_text(json.dumps(data), encoding="utf-8")


class TestATerminateDuringBootstrapKeepsTheNode:
    """The verdict needs BOTH questions answered."""

    def test_a_crew_that_never_started_keeps_its_node(self, guest_state, monkeypatch):
        """The defect, as the one case that matters: registered, not yet serving."""
        monkeypatch.setattr(hooks, "crew_is_serving", lambda: False)
        _boot(guest_state, stage="supervisor")

        hand_back, reason = hooks.terminate_verdict()

        assert hand_back is False
        assert "not finished starting" in reason

    def test_a_crew_with_no_boot_state_at_all_keeps_its_node(self, guest_state, monkeypatch):
        """Nothing written yet is the earliest moment of the same window."""
        monkeypatch.setattr(hooks, "crew_is_serving", lambda: False)
        assert hooks.terminate_verdict()[0] is False

    def test_a_crew_that_ran_and_stopped_hands_its_node_back(self, guest_state, monkeypatch):
        """Non-vacuity: the hook must still do its job, or the node leaks on every
        real shutdown and the sweeper reports it forever."""
        monkeypatch.setattr(hooks, "crew_is_serving", lambda: False)
        _boot(guest_state, stage="started", started_at=1.0)

        hand_back, _ = hooks.terminate_verdict()
        assert hand_back is True

    def test_a_boot_that_failed_hands_its_node_back(self, guest_state, monkeypatch):
        """That boot is over and will never serve, so its node is dead weight."""
        monkeypatch.setattr(hooks, "crew_is_serving", lambda: False)
        _boot(guest_state, stage="failed:secrets")
        assert hooks.terminate_verdict()[0] is True

    def test_a_serving_crew_keeps_its_node_whatever_the_boot_state_says(
        self, guest_state, monkeypatch
    ):
        monkeypatch.setattr(hooks, "crew_is_serving", lambda: True)
        _boot(guest_state, stage="started")
        assert hooks.terminate_verdict()[0] is False

    def test_an_unreadable_boot_state_keeps_the_node(self, guest_state, monkeypatch):
        """A guest that cannot tell what it has done must not cut its crew off."""
        monkeypatch.setattr(hooks, "crew_is_serving", lambda: False)
        (guest_state / "boot.json").write_text("{ not json", encoding="utf-8")
        assert hooks.supervisor_has_run() is False
        assert hooks.terminate_verdict()[0] is False


class TestTwoRunCallsAtOnceBootstrapOnce:
    """The marker is written at the end, so the lock is what makes it single-shot."""

    def test_only_one_of_two_concurrent_calls_bootstraps(self, guest_state, monkeypatch):
        entered: list[int] = []
        release = threading.Event()

        def _once(body):
            # Hold the bootstrap open so the second caller is genuinely concurrent
            # rather than merely later.
            entered.append(1)
            release.wait(timeout=5)
            (guest_state / "run.done").write_text("", encoding="ascii")
            return {"ok": True, "repeat": False}

        monkeypatch.setattr(hooks, "_handle_run_once", _once)

        answers: list[dict] = []
        workers = [
            threading.Thread(target=lambda: answers.append(hooks.handle_run(b"{}")))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        # The first caller is inside the bootstrap and holding the lock; let it go
        # only once the second is waiting on that lock.
        release.set()
        for worker in workers:
            worker.join(timeout=10)

        assert len(entered) == 1, f"the bootstrap ran {len(entered)} times"
        assert len(answers) == 2, "a caller got no answer"
        assert sum(1 for a in answers if a.get("repeat")) == 1

    def test_a_call_after_the_marker_exists_is_a_repeat(self, guest_state, monkeypatch):
        """Non-vacuity: the lock must not have replaced the marker check."""
        (guest_state / "run.done").write_text("", encoding="ascii")
        monkeypatch.setattr(
            hooks,
            "_handle_run_once",
            lambda body: pytest.fail("a repeat call re-ran the bootstrap"),
        )
        assert hooks.handle_run(b"{}") == {"ok": True, "repeat": True}
