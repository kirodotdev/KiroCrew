"""Navigable legacy notifications: the ``url``/``actions`` seam on ``notify()``.

Before this seam existed, the only way a ``notify()`` caller could ask for a deep
link was ``meta={"url": ...}`` -- and ``_RESERVED_NOTE_KEYS`` drops that name
during the meta merge (so meta cannot smuggle an unvalidated link past
``_validate_internal_url``). The result was a note with no Open button and no
action capsule, which is exactly what the pending-skill notification shipped.

These tests pin both halves: the keyword arguments reach the note THROUGH
validation, and the meta hole stays closed.
"""

from __future__ import annotations

import pytest

from kiro_crew.notifications.bus import (
    SYSTEM_CHANNELS,
    NotificationBus,
    NotificationValidationError,
    payload_from_legacy,
)


@pytest.fixture()
def sink():
    notes: list[dict] = []
    return notes, NotificationBus(notes.append)


def test_skills_is_a_registered_system_channel():
    # A staged candidate is invisible until approved, so the note is the only
    # surface announcing it -- it must not land on the generic agent fallback.
    assert "system.skills" in SYSTEM_CHANNELS
    assert SYSTEM_CHANNELS["system.skills"] == "default"


def test_legacy_payload_carries_url_and_actions(sink):
    notes, bus = sink
    bus.push(
        payload_from_legacy(
            "skills",
            "New skill awaiting review",
            "body",
            {"slug": "cand"},
            url="/capabilities?tab=skills&review=cand",
            actions=[{"id": "review-skill", "label": "Review skill", "url": "/x"}],
        )
    )
    assert notes[0]["url"] == "/capabilities?tab=skills&review=cand"
    assert notes[0]["actions"] == [
        {"id": "review-skill", "label": "Review skill", "url": "/x"}
    ]
    # kind survives verbatim even though it also names a channel.
    assert notes[0]["kind"] == "skills"
    assert notes[0]["channel"] == "system.skills"
    assert notes[0]["slug"] == "cand"


def test_legacy_url_is_validated_not_repaired(sink):
    _, bus = sink
    # Titles/bodies are repaired by the adapter; a bad deep link is NOT -- a
    # button that navigates off-dashboard is worse than no button.
    with pytest.raises(NotificationValidationError):
        bus.push(payload_from_legacy("skills", "t", "b", None, url="https://evil.test"))
    with pytest.raises(NotificationValidationError):
        bus.push(
            payload_from_legacy(
                "skills",
                "t",
                "b",
                None,
                actions=[{"id": "a", "label": "a", "url": "//evil.test"}],
            )
        )


def test_meta_url_is_still_dropped(sink):
    notes, bus = sink
    # The anti-smuggling guard is the reason the keyword arguments exist; it must
    # not be relaxed by adding them.
    bus.push(payload_from_legacy("skills", "t", "b", {"url": "/capabilities"}))
    assert "url" not in notes[0]


def test_legacy_url_and_actions_default_to_absent(sink):
    notes, bus = sink
    bus.push(payload_from_legacy("cron", "t", "b"))
    assert "url" not in notes[0]
    assert "actions" not in notes[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": object()},
        {"url": 42},
        {"actions": 42},
        {"actions": "review"},
        {"actions": {"id": "x", "label": "y"}},
    ],
)
def test_wrong_typed_navigation_raises_the_catchable_error(sink, kwargs):
    """A bad TYPE must surface as NotificationValidationError, not TypeError.

    ``DashboardState.notify`` catches only ``NotificationValidationError`` to
    honour its never-raises contract. Before the isinstance guards, a non-string
    ``url`` reached ``_validate_internal_url``'s ``.startswith()`` (AttributeError)
    and a non-list ``actions`` reached ``len()`` (TypeError) -- neither of which
    that handler catches, so the producer crashed instead of dropping the note.
    """
    _, bus = sink
    with pytest.raises(NotificationValidationError):
        bus.push(payload_from_legacy("skills", "t", "b", None, **kwargs))
