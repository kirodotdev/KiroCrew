"""The subagent_snapshot's ``streaming`` field MUST include all in-flight tokens.

The client-side chunk buffer discards buffered text when an authoritative
snapshot arrives, relying on the snapshot's ``streaming`` field to subsume
everything the buffer held. That cross-layer contract is load-bearing:

    // A snapshot is authoritative: it replaces any buffered partial text.
    subagentChunkBufRef.current.delete(key)

If a snapshot producer ever omits in-flight tokens — e.g. a stale read, a
parallel sender that didn't share the accumulator, or a future refactor that
builds ``streaming`` from a different source — the client would silently drop
text that was buffered but not yet flushed.

This test pins the invariant at the boundary: ``build_subagent_snapshot`` must
return a ``streaming`` field that contains the full ``streaming_text`` from
the SubagentInfo. The accumulator that feeds ``streaming_text`` is the same
one that feeds ``subagent_chunk`` events (subagent.py:6032–6035), so as long
as this test passes, the snapshot cannot omit in-flight tokens.
"""

from __future__ import annotations

from types import SimpleNamespace

# Import cycle workaround: see test_subagent_snapshot_idle_secs.py for rationale.
import kiro_crew.dashboard.handlers  # noqa: F401
from kiro_crew.dashboard.ws import build_subagent_snapshot


def _agent(**over):
    """A minimal stand-in for the SubagentInfo fields the frame reads."""
    base = dict(
        id="a1",
        parent_session_key="dashboard:main",
        task="do a thing",
        agent="kirocrew",
        resolved_model="model-1",
        requested_model="",
        streaming_text="",
        last_tool="",
        tool_count=0,
        stalled=False,
        started=1000.0,
        last_activity=1000.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_snapshot_streaming_includes_all_accumulated_text():
    """The snapshot's ``streaming`` field must contain the full accumulated
    ``streaming_text`` — not a stale or partial view.

    This is the invariant the client buffer relies on: when a snapshot arrives,
    the buffer discards its contents because the snapshot subsumes them. If
    the snapshot ever omitted in-flight tokens, text would be silently lost.
    """
    # Simulate three chunks having been accumulated
    accumulated = "first chunk " + "second chunk " + "third chunk"
    a = _agent(streaming_text=accumulated)

    data = build_subagent_snapshot(a, now=1000.0)

    assert data["streaming"] == accumulated


def test_snapshot_streaming_is_empty_when_no_chunks_received():
    """A freshly-spawned agent with no output yet must have empty streaming."""
    a = _agent(streaming_text="")
    data = build_subagent_snapshot(a, now=1000.0)
    assert data["streaming"] == ""


def test_snapshot_streaming_preserves_whitespace_and_newlines():
    """Formatting characters must survive the round-trip — they are semantically
    significant for code output and tool traces."""
    text = "line 1\n  indented line 2\n\ttabbed line 3\n"
    a = _agent(streaming_text=text)
    data = build_subagent_snapshot(a, now=1000.0)
    assert data["streaming"] == text


def test_snapshot_streaming_redacts_credentials():
    """The streaming field is rendered in the Activity Viewer, so credential-
    shaped strings must be redacted at the snapshot boundary."""
    # AKIA... is the AWS access key pattern
    text = "Calling AWS with AKIAIOSFODNN7EXAMPLE"
    a = _agent(streaming_text=text)
    data = build_subagent_snapshot(a, now=1000.0)
    assert "AKIAIOSFODNN7EXAMPLE" not in data["streaming"]


def test_snapshot_streaming_field_is_always_present():
    """The field must be present even when empty, so the client reducer does
    not need to distinguish 'missing' from 'empty string'."""
    a = _agent(streaming_text="")
    data = build_subagent_snapshot(a, now=1000.0)
    assert "streaming" in data
    assert isinstance(data["streaming"], str)
