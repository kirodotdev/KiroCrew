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
    def test_disallowed_event_raises(
        self, app_name: str, allowed: list[str], disallowed: str
    ) -> None:
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


class TestBroadcastAdapterPreservesIdentityAndScope:
    """The `(msg_type, data)` WS adapter must not drop the envelope's identity.

    `broadcast_ws` puts its one data argument under `{type, data}` on the wire, so
    everything the publisher set beyond `type` — `app` (who emitted it) and
    `_scope` (broadcast vs app-scoped) — survives only if it rides inside that
    data arg. The adapter previously forwarded just `payload["data"]`, so a client
    got an event with no sender and no scope: indistinguishable from any other
    app's and unfilterable once per-app WS routing exists.
    """

    def test_app_and_scope_reach_the_sender(self) -> None:
        from kiro_crew.apps.event_bus import build_broadcast_fn

        sent: list[tuple] = []
        fn = build_broadcast_fn(lambda t, d: sent.append((t, d)))  # type: ignore[arg-type]
        fn({"type": "thing.updated", "app": "probe", "data": {"n": 1}, "_scope": "app"})
        assert len(sent) == 1
        msg_type, data = sent[0]
        # App events ride under one namespaced WS type; the real event name is
        # carried inside so it can never collide with a core WS type.
        assert msg_type == "app_event"
        assert data["event"] == "thing.updated"
        assert data["app"] == "probe"
        assert data["_scope"] == "app"
        assert data["data"] == {"n": 1}

    def test_plain_broadcast_keeps_app(self) -> None:
        from kiro_crew.apps.event_bus import build_broadcast_fn

        sent: list[tuple] = []
        fn = build_broadcast_fn(lambda t, d: sent.append((t, d)))  # type: ignore[arg-type]
        fn({"type": "e", "app": "probe", "data": {"k": "v"}})
        msg_type, data = sent[0]
        assert msg_type == "app_event" and data["event"] == "e"
        assert data["app"] == "probe" and data["data"] == {"k": "v"}
        assert "_scope" not in data  # plain publish set none; adapter invents none

    def test_end_to_end_from_publish_to_app(self) -> None:
        """EventBus.publish_to_app → adapter: app + scope arrive at the sender."""
        from kiro_crew.apps.event_bus import EventBus, build_broadcast_fn

        sent: list[tuple] = []
        adapter = build_broadcast_fn(lambda t, d: sent.append((t, d)))  # type: ignore[arg-type]
        bus = EventBus("probe", ["thing.x"], adapter)
        bus.publish_to_app("thing.x", {"v": True})
        msg_type, data = sent[0]
        assert msg_type == "app_event"
        assert data["event"] == "thing.x"
        assert data["app"] == "probe"
        assert data["_scope"] == "app"
        assert data["data"] == {"v": True}
