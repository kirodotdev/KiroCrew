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
    return sorted(DASHBOARD.glob("*.py"))


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


def _replies_seeding_an_optimistic_row() -> list[tuple[str, str]]:
    """Each reply a client turns straight into an optimistic row, with its own slot name.

    A row built from a reply that omits the incarnation stamps the close tombstone
    ``undefined``, so the differing-incarnation reveal can never fire for that session.
    """
    handlers = (DASHBOARD / "chat_handlers.py").read_text(encoding="utf-8")
    fork = (DASHBOARD / "chat_fork.py").read_text(encoding="utf-8")
    return [
        ("resume, live slot", handlers, '"key": existing.key,'),
        ("resume, cold slot", handlers, '"key": slot.key,'),
        ("fork", fork, '"key": new_slot.key,'),
    ]


def test_every_reply_seeding_an_optimistic_row_carries_the_incarnation() -> None:
    missing: list[str] = []
    for label, src, anchor in _replies_seeding_an_optimistic_row():
        i = src.find(anchor)
        assert i != -1, f"{label}: anchor {anchor!r} not found, so this pin proves nothing"
        if "incarnation" not in src[i : i + 200]:
            missing.append(label)
    assert missing == [], "reply omits the incarnation: " + "; ".join(missing)
