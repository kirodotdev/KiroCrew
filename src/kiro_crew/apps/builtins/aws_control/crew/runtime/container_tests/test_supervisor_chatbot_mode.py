"""A chatbot crew must not start a sidecar, because starting one is a boot loop.

``run_sidecar`` logs and returns when no bucket is configured, and
``_wait_for_shutdown`` treats ANY child exiting as the end of the task. Together
those two reasonable behaviours make an unconditional sidecar fatal in chatbot
mode: the container comes up, loses its sidecar within a second, shuts down, and
ECS replaces it, forever. Nothing in the deploy would say why, because each piece
is behaving as designed.

The supervisor tests used to run with ``backup_bucket=None`` and assert that the
sidecar starts, so the suite encoded the boot loop as correct. That is why these
tests exist separately: they assert the mode, not the wiring.
"""

from __future__ import annotations

from pathlib import Path

from container.supervisor import __main__ as entry

from .test_supervisor_main import FakePG, make_settings, wired

__all__ = ["wired"]  # re-exported so pytest resolves the imported fixture


def test_a_chatbot_crew_starts_no_sidecar(wired, tmp_path: Path) -> None:
    events = wired
    entry.run(make_settings(tmp_path, bucket=None), wait_for_shutdown=lambda c: "signal")
    assert "start_sidecar" not in events, (
        "a sidecar was started with no bucket. It exits immediately and any child "
        "exiting ends the task, so this is a boot loop, not a harmless no-op."
    )


def test_a_persistent_crew_still_starts_one(wired, tmp_path: Path) -> None:
    """The other half. MUTATION: make the condition unconditional-false and this
    reddens, so the guard cannot be tightened into never starting a sidecar."""
    events = wired
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "start_sidecar" in events


def test_the_chatbot_task_does_not_watch_a_process_it_never_started(wired, tmp_path: Path) -> None:
    """What is watched is what decides whether the task stays up."""
    seen: list[list[str]] = []

    def record_and_signal(children) -> str:
        seen.append([c.name for c in children])
        return "signal"

    entry.run(make_settings(tmp_path, bucket=None), wait_for_shutdown=record_and_signal)
    assert seen == [["backend", "front"]], seen


def test_teardown_survives_a_crew_that_never_had_a_sidecar(tmp_path: Path) -> None:
    events: list[str] = []
    front = FakePG("front", events)
    backend = FakePG("backend", events)
    # settings is required now: teardown runs a final backup cycle for a
    # persistent crew. A chatbot crew has no sidecar and no bucket, so this
    # call also asserts that path stays a pure drain.
    entry._teardown(front, backend, None, make_settings(tmp_path, bucket=None))
    assert events == ["term:front", "term:backend"], events
