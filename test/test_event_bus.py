"""Property tests for EventBus permission enforcement.

Feature: app-sdk-gateway-hooks
Property 17: EventBus permission enforcement.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.event_bus import EventBus

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _app_name() -> st.SearchStrategy[str]:
    return st.from_regex(r"[a-z][a-z0-9-]{2,12}", fullmatch=True)


def _event_type() -> st.SearchStrategy[str]:
    return st.from_regex(r"[a-z][a-z_]{2,15}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 17: EventBus permission enforcement
# ---------------------------------------------------------------------------


class TestEventBusPermissions:
    """Property 17: EventBus permission enforcement.

    **Validates: Requirements 5.2**
    """

    @settings(max_examples=100)
    @given(
        app_name=_app_name(),
        allowed=st.lists(_event_type(), min_size=1, max_size=5),
    )
    def test_allowed_event_succeeds(self, app_name: str, allowed: list[str]) -> None:
        """Publishing a declared event type succeeds."""
        published: list[dict] = []
        bus = EventBus(app_name, allowed, lambda payload: published.append(payload))

        event = allowed[0]
        bus.publish(event, {"key": "value"})

        assert len(published) == 1
        assert published[0]["type"] == event
        assert published[0]["app"] == app_name
        assert published[0]["data"] == {"key": "value"}

    @settings(max_examples=100)
    @given(
        app_name=_app_name(),
        allowed=st.lists(_event_type(), min_size=1, max_size=3),
        disallowed=_event_type(),
    )
    def test_disallowed_event_raises(self, app_name: str, allowed: list[str], disallowed: str) -> None:
        """Publishing an undeclared event type raises PermissionError."""
        if disallowed in allowed:
            return  # skip trivial case

        bus = EventBus(app_name, allowed, lambda payload: None)

        with pytest.raises(PermissionError):
            bus.publish(disallowed)

    @settings(max_examples=50)
    @given(app_name=_app_name(), event=_event_type())
    def test_wildcard_allows_any_event(self, app_name: str, event: str) -> None:
        """Wildcard '*' in allowed events permits any event type."""
        published: list[dict] = []
        bus = EventBus(app_name, ["*"], lambda payload: published.append(payload))

        bus.publish(event, {"x": 1})
        assert len(published) == 1
        assert published[0]["type"] == event

    @settings(max_examples=50)
    @given(app_name=_app_name(), event=_event_type())
    def test_publish_to_app_includes_scope(self, app_name: str, event: str) -> None:
        """publish_to_app includes _scope='app' in payload."""
        published: list[dict] = []
        bus = EventBus(app_name, [event], lambda payload: published.append(payload))

        bus.publish_to_app(event, {"data": True})
        assert len(published) == 1
        assert published[0]["_scope"] == "app"
        assert published[0]["app"] == app_name

    def test_empty_allowed_denies_all(self) -> None:
        """Empty allowed list denies all events."""
        bus = EventBus("test-app", [], lambda payload: None)
        with pytest.raises(PermissionError):
            bus.publish("any_event")
