"""The incarnation identifies the LIVE slot object, so no restore path may carry it over.

``created_at`` is overwritten from persisted metadata by several paths, which is why it
cannot tell a resumed replacement from the instance a client was closing. The incarnation
exists to answer that, and only holds if it is assigned exactly once.
"""

from __future__ import annotations

import pathlib
import re

DASHBOARD = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"


def _sources() -> list[pathlib.Path]:
    # Recursive: the slot and its handlers now live under ``state_parts/`` and
    # ``chat_api/``, so a top-level glob would scan neither and report a vacuous zero.
    return sorted(DASHBOARD.rglob("*.py"))


def test_created_at_is_restored_somewhere_so_the_control_is_real() -> None:
    """Positive control: if nothing restored created_at the whole finding would be moot."""
    restores = [
        p.name
        for p in _sources()
        if re.search(r"^\s*slot\.created_at\s*=", p.read_text(encoding="utf-8"), re.M)
    ]
    assert restores, "no path assigns slot.created_at; this pin would prove nothing"


def test_no_path_reassigns_the_incarnation() -> None:
    offenders: list[str] = []
    for path in _sources():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"^\s*(slot|self)\.incarnation\s*=", line) and "uuid" not in line:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert (
        offenders == []
    ), "the incarnation must be minted once per live slot object and never restored: " + "; ".join(
        offenders
    )


def test_the_incarnation_is_published_on_the_wire() -> None:
    projection = (DASHBOARD / "slot_projection.py").read_text(encoding="utf-8")
    assert '"incarnation": slot.incarnation' in projection
