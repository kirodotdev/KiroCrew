"""A fork must carry the cleared-project marker, not just the project.

An empty ``project`` states two different things: never scoped, or explicitly cleared. Only
``project_cleared`` separates them, and only the cleared answer invalidates a warm pooled
child. A fork that copies the project alone therefore claims no directory, the pool is not
blocked, and the fork binds the very directory its parent cleared.
"""

from __future__ import annotations

import pytest

from kiro_crew.config.paths import CWD_CLEARED
from kiro_crew.dashboard.state import _ChatSlot


class TestForkingKeepsTheClearedProjectState:
    def test_a_fork_of_a_cleared_slot_still_claims_cleared(self):
        parent = _ChatSlot("chat-1")
        parent.project = "/workspace/old"
        assert parent.claim_cwd == "/workspace/old", "precondition: the parent was not scoped"

        # The user clears the project: the directory goes, the MARKER is what remains.
        parent.project = ""
        parent.project_cleared = True
        assert parent.claim_cwd == CWD_CLEARED, "precondition: the clear did not register"

        fork = _ChatSlot("chat-2")
        _copy_as_fork_does(parent, fork)

        assert fork.claim_cwd == CWD_CLEARED, (
            "the fork lost the cleared marker, so its claim states no directory -- which does "
            "not invalidate a warm child, and the fork binds the directory the parent cleared; "
            f"claim_cwd={fork.claim_cwd!r}"
        )

    def test_a_fork_of_a_never_scoped_slot_still_states_nothing(self):
        """The marker must be COPIED, not asserted: an unscoped parent stays unscoped."""
        parent = _ChatSlot("chat-1")
        assert parent.claim_cwd is None, "precondition: the parent was scoped"

        fork = _ChatSlot("chat-2")
        _copy_as_fork_does(parent, fork)

        assert fork.claim_cwd is None, (
            "a fork of a slot that never had a project claims CLEARED, which discards the warm "
            f"pool and its stored-cwd resume override for every fork; claim_cwd={fork.claim_cwd!r}"
        )

    def test_a_fork_of_a_scoped_slot_inherits_the_directory(self):
        parent = _ChatSlot("chat-1")
        parent.project = "/workspace/live"

        fork = _ChatSlot("chat-2")
        _copy_as_fork_does(parent, fork)

        assert (
            fork.claim_cwd == "/workspace/live"
        ), "the fork stopped inheriting its parent's working directory"


def _copy_as_fork_does(parent: _ChatSlot, fork: _ChatSlot) -> None:
    """Mirror the fork endpoint's project-carrying lines, read from its own source.

    Driven off the SOURCE rather than hardcoded, so a copy line the endpoint adds or drops is
    reflected here instead of this control silently testing a stale contract.
    """
    import re
    from pathlib import Path

    import kiro_crew.dashboard.chat_fork as fork_mod

    text = Path(fork_mod.__file__).read_text(encoding="utf-8")
    copies = re.findall(r"^\s*new_slot\.(project(?:_cleared)?)\s*=\s*slot\.(\w+)\s*$", text, re.M)
    assert copies, "precondition: the fork's project-copy lines were not found in source"
    for attr, source_attr in copies:
        setattr(fork, attr, getattr(parent, source_attr))


@pytest.mark.parametrize("attr", ["project", "project_cleared"])
def test_the_slot_carries_both_halves_of_the_project_answer(attr):
    """Both must live in __slots__, or a fork cannot carry them at all."""
    assert attr in _ChatSlot.__slots__, f"{attr} left __slots__, so it cannot be set on a slot"
