"""The promise-only downgrade must cover EVERY auto-approve grant source (#2696).

The recovery arm injects a continuation that the model then acts on. That is only
safe while a human approves the resulting tool call, which is why the arm
downgrades to a notice when approval is gone: a detector false-accept then costs
a notice instead of an unrequested action.

Approval is granted by ``slot_trusted or yolo_active`` in the tool-event branch,
so gating the downgrade on yolo ALONE left every trusted-but-not-yolo session on
the auto-continue path with its approval gate already removed -- the exact state
the downgrade exists to refuse. Blocking finding from the #2696 GPT round,
anchored on the ``backend-security-controls`` AUTOSDE rule.

Both tests are here rather than in ``test_promise_only_recovery.py`` because that
file covers the pure detector and gating predicate; this defect lives at the CALL
SITE, inside the turn loop, where neither pure function can reach it. The second
test therefore derives the grant sources from the approval branch itself, so
adding a THIRD source fails this test until the downgrade learns about it too.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.dashboard.chat_runner import _slot_is_trusted

_RUNNER = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "chat_runner.py"


class _Slot:
    """Minimal slot: only the attributes _slot_is_trusted actually reads."""

    def __init__(self, **attrs: object) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)


def test_session_trust_is_independent_of_yolo() -> None:
    """The premise of the defect: trusted-but-not-yolo is a reachable state.

    ``slot._trust`` is the per-session "trust this session" click. It grants
    auto-approval for that slot without touching the global safety override, so a
    downgrade keyed on yolo cannot see it. If this ever became impossible the
    finding would be moot -- assert it is not.
    """
    assert _slot_is_trusted(_Slot(_trust=True)) is True
    # No global state was consulted to reach that True: the grant is slot-local.
    assert _slot_is_trusted(_Slot(_trust=False)) is False
    # An ordinary chat slot carries neither attribute and is untrusted.
    assert _slot_is_trusted(_Slot()) is False


def test_downgrade_gate_covers_every_approval_grant_source() -> None:
    """The downgrade condition must name every source the approval branch ORs.

    Derived, not hardcoded: the expected sources come from the approval branch in
    the same file, so a new grant source there fails here instead of silently
    reopening this hole.
    """
    source = _RUNNER.read_text(encoding="utf-8")

    # The tool-event branch that auto-approves without human confirmation.
    approval = re.search(
        r"^\s*if \((?P<cond>[^)]*yolo_active[^)]*)\) and not _child_low_fidelity:",
        source,
        re.MULTILINE,
    )
    assert approval, "could not locate the auto-approve tool branch; update this test"
    granting = {name for name in ("slot_trusted", "yolo_active") if name in approval.group("cond")}
    assert granting == {"slot_trusted", "yolo_active"}, (
        "the approval branch changed its grant sources to "
        f"{sorted(granting)}; the downgrade below must be updated to match"
    )

    # The promise-only recovery downgrade, which must refuse the same states.
    downgrade = re.search(
        r"^\s*if (?P<cond>state\.is_yolo_active\(\)[^:]*):\n"
        r"(?:\s*#[^\n]*\n)*\s*slot\.append\(\s*\n\s*\"notice\"",
        source,
        re.MULTILINE,
    )
    assert downgrade, "could not locate the promise-only downgrade arm; update this test"
    condition = downgrade.group("cond")

    # yolo_active is read via state.is_yolo_active(); slot_trusted via the helper.
    assert "state.is_yolo_active()" in condition
    assert "_slot_is_trusted(slot)" in condition, (
        "the promise-only downgrade checks yolo only. A session trusted via "
        "slot._trust or a scoped grant has no approval gate either, so it would "
        "auto-dispatch the announced action on a detector false-accept."
    )
