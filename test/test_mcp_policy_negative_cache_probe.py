"""The tool-policy failure window ends when the gateway answers again.

When a control-plane server cannot read its session's tool policy from the
gateway, ``mcp_shared_policy`` caches the failure for ``_NEGATIVE_CACHE_TTL``
(60 s) and refuses the tool calls of every session that never resolved its
policy. Without the half-open probe the window can only end by expiring: a
gateway that comes back 3 s into an outage still has those sessions refused for
the rest of the 60 s.

Pinned here:

* inside ``_NEGATIVE_CACHE_PROBE_INTERVAL`` the cache answers and the call is
  refused without dialling the gateway;
* past the interval one call dials (the probe); an answered policy resolves the
  session and the operator's exclusion still applies;
* a probe that fails re-arms the window and still refuses, and a 5xx answer
  counts as a failure;
* one answered read, a 200 or any 4xx about the probing caller, ends the window
  for every sibling session the process serves;
* a call that returns before the read (no session key) does not spend the probe.
"""

from __future__ import annotations

import io
import json
import time
import types
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.mcp_shared as mcp_shared

GATEWAY_PORT = 47211


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home with a ``run/`` dir, and no inherited port variables."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for name in ("KIROCREW_PORT", "KIROCREW_BOUND_PORT", "KIROCREW_BOUND_HOST"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "run").mkdir(mode=0o700)
    return tmp_path


def _publish_secret(home: Path, port: int, value: str) -> None:
    """The per-port credential a gateway publishes beside its marker (synthetic)."""
    (home / "run" / f"gateway-{port}.secret").write_text(value, encoding="ascii")


def _policy_response(exclude: list[str]) -> MagicMock:
    body = MagicMock()
    body.read.return_value = json.dumps({"exclude": exclude}).encode("utf-8")
    body.__enter__ = MagicMock(return_value=body)
    body.__exit__ = MagicMock(return_value=False)
    return body


@pytest.fixture
def policy_state(monkeypatch: pytest.MonkeyPatch):
    """Fresh resolver state, a recording audit sink, and a declared session key."""
    for name, value in (
        ("_last_failure_time", 0.0),
        ("_last_probe_time", 0.0),
        ("_last_startup_race_time", 0.0),
        ("_last_startup_race_key", ""),
        ("_failure_count", 0),
        ("_last_failure_class", ""),
    ):
        monkeypatch.setattr(mcp_shared, name, value)
    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_included_tools_by_session", {})
    audit = MagicMock()
    monkeypatch.setattr(mcp_shared, "sel", lambda: audit)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-1-1")
    return audit


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ── negative-cache recovery ──────────────────────────────────────────────────


class TestNegativeCacheRecovery:
    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> _Clock:
        """The mcp_shared family's ``time`` binding, never the process-wide clock.

        Patching ``time.monotonic`` itself would hand the synthetic clock to every
        deadline and liveness check in this worker. Instead the family's ``time``
        name is replaced with a stand-in whose ``monotonic`` is the test clock and
        whose every other attribute is the real ``time`` module's. The patch goes
        through the ``mcp_shared`` facade: several owners bind ``time``, and the
        facade mirrors the write to each of them and undoes it everywhere.
        """
        c = _Clock()
        real = {name: getattr(time, name) for name in dir(time) if not name.startswith("__")}
        monkeypatch.setattr(mcp_shared, "time", types.SimpleNamespace(**{**real, "monotonic": c}))
        return c

    @pytest.fixture
    def gateway(self, home, policy_state, monkeypatch):
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(GATEWAY_PORT))
        _publish_secret(home, GATEWAY_PORT, "synthetic-credential-A")
        urlopen = MagicMock()
        monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)
        return urlopen

    def test_inside_the_probe_interval_the_cache_answers_and_refuses(self, gateway, clock):
        gateway.side_effect = urllib.error.URLError("connection refused")
        assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL - 1
        policy = mcp_shared._resolve_tool_policy()
        assert policy.unresolved == "resolution_failed"
        assert policy.detail == "URLError"
        assert gateway.call_count == 1

    def test_a_probe_after_recovery_resolves_and_keeps_the_exclusion(self, gateway, clock):
        """The gateway comes back 3s into the outage. Without the probe the session
        is refused for the rest of the 60s window; with it the first call past the
        probe interval reads the policy, and the operator's exclusion holds."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy()
        gateway.side_effect = None
        gateway.return_value = _policy_response(["blocked"])
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert clock.now - 1000.0 < mcp_shared._NEGATIVE_CACHE_TTL

        policy = mcp_shared._resolve_tool_policy()

        assert policy.unresolved == ""
        assert "blocked" in policy.excluded
        assert gateway.call_count == 2

    def test_a_failed_probe_rearms_the_window_and_still_refuses(self, gateway, clock):
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy()
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        assert gateway.call_count == 2
        # The probe's failure restarted the interval: the next call is cached again.
        clock.now += 1
        assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        assert gateway.call_count == 2

    def test_one_answered_read_ends_the_window_for_every_session(self, gateway, clock):
        """A pooled backend serves many sessions; the outage was the gateway's, so
        the first answered read clears it for the siblings that never resolved."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        gateway.side_effect = None
        gateway.return_value = _policy_response([])
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy("dashboard:chat-2-2").unresolved == ""

        clock.now += 1
        gateway.return_value = _policy_response(["blocked"])
        sibling = mcp_shared._resolve_tool_policy("dashboard:chat-3-3")

        assert sibling.unresolved == "" and sibling.excluded == {"blocked"}
        assert mcp_shared._last_failure_time == 0.0

    @pytest.mark.parametrize(
        ("status", "body", "probe_reason"),
        [
            (404, b"", "agent_not_resolved"),
            (403, b'{"error":"Forbidden","code":"member_session_unverified"}', "policy_forbidden"),
            (409, b'{"error":"bad spec"}', "policy_unreadable"),
        ],
    )
    def test_a_caller_specific_answer_still_ends_the_outage(
        self, gateway, clock, status, body, probe_reason
    ):
        """The probe that gets through may be answered about ITS caller only. The
        gateway answered, so the outage is over for the sibling sessions."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        gateway.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1/", code=status, msg="answered", hdrs=None, fp=io.BytesIO(body)
        )
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy("dashboard:chat-2-2").unresolved == probe_reason

        clock.now += 1
        gateway.side_effect = None
        gateway.return_value = _policy_response(["blocked"])
        sibling = mcp_shared._resolve_tool_policy("dashboard:chat-3-3")

        assert sibling.unresolved == "" and sibling.excluded == {"blocked"}

    def test_a_call_that_cannot_dial_does_not_spend_the_probe(self, gateway, clock, monkeypatch):
        """A call with no session key returns before the read; if it took the
        probe slot, the next real caller would wait another whole interval."""
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setattr(mcp_shared, "_policy_session_key", lambda: "")
        mcp_shared._resolve_tool_policy()
        assert gateway.call_count == 1

        gateway.side_effect = None
        gateway.return_value = _policy_response(["blocked"])
        assert mcp_shared._resolve_tool_policy("dashboard:chat-3-3").unresolved == ""
        assert gateway.call_count == 2

    def test_a_5xx_probe_keeps_the_outage_armed(self, gateway, clock):
        gateway.side_effect = urllib.error.URLError("connection refused")
        mcp_shared._resolve_tool_policy("dashboard:chat-2-2")
        gateway.side_effect = urllib.error.HTTPError(
            url="http://127.0.0.1/", code=503, msg="broken", hdrs=None, fp=io.BytesIO(b"")
        )
        clock.now += mcp_shared._NEGATIVE_CACHE_PROBE_INTERVAL
        assert mcp_shared._resolve_tool_policy("dashboard:chat-2-2").unresolved == (
            "resolution_failed"
        )
        clock.now += 1
        assert mcp_shared._resolve_tool_policy("dashboard:chat-3-3").unresolved == (
            "resolution_failed"
        )
        assert gateway.call_count == 2
