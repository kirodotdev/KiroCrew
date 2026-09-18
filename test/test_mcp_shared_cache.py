"""Tests for ``_resolve_tool_policy`` cache + warning suppression.

Covers:
- Successful resolution caches and short-circuits subsequent calls.
- No-session-key path uses the SHORT (5s) cache and audits the no-key event.
- 404 ``agent not resolved`` uses the SHORT cache and audits ``agent_not_resolved``.
- Other HTTP errors and connection failures use the LONG (60s) cache.
- Repeated long-cache failures only emit ``_MAX_WARNING_FAILURES`` warnings,
  then a single suppression notice, then go silent.
- Both cache windows are consulted independently (long-failure cache hit
  while startup-race window is expired, and vice versa).
- Cache hits emit the ``negative_cache_hit`` audit event without re-querying.
"""

from __future__ import annotations

import io
import logging
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.mcp_shared as mcp_shared


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """Reset the module-level cache between tests."""
    mcp_shared._excluded_tools_by_session.clear()
    mcp_shared._last_failure_time = 0.0
    mcp_shared._last_startup_race_time = 0.0
    mcp_shared._failure_count = 0
    yield
    mcp_shared._excluded_tools_by_session.clear()
    mcp_shared._last_failure_time = 0.0
    mcp_shared._last_startup_race_time = 0.0
    mcp_shared._failure_count = 0


@pytest.fixture
def fake_sel():
    """Patch ``sel()`` so audit calls can be inspected without side-effects."""
    audit = MagicMock()
    with patch.object(mcp_shared, "sel", return_value=audit):
        yield audit


# Helpers ──────────────────────────────────────────────────────────────

def _make_http_response(payload: dict) -> MagicMock:
    body = MagicMock()
    body.read.return_value = b'{"exclude": ["bad-tool"]}'
    if payload is not None:
        import json
        body.read.return_value = json.dumps(payload).encode("utf-8")
    body.__enter__ = MagicMock(return_value=body)
    body.__exit__ = MagicMock(return_value=False)
    return body


def _make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost/api/session-tool-policy",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,
        fp=io.BytesIO(b""),
    )


@pytest.fixture
def patch_session_setup(monkeypatch, tmp_path):
    """Patch the gateway-config plumbing so the resolver only depends on
    what the test wants to exercise."""
    cfg = MagicMock()
    cfg.dashboard.url = "http://localhost:5476/"
    monkeypatch.setattr(mcp_shared.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(mcp_shared, "parse_dashboard_url", lambda url: ("localhost", 5476))
    # Provide a writeable config_dir() with a .local_secret.
    monkeypatch.setattr(mcp_shared, "config_dir", lambda: tmp_path)
    (tmp_path / ".local_secret").write_text("test-secret")
    return tmp_path


# ─────────────────────────────────────────────────────────────────────
# Successful resolution path.
# ─────────────────────────────────────────────────────────────────────

class TestSuccessCaching:
    def test_first_call_queries_gateway_then_caches(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(return_value=_make_http_response({"exclude": ["foo", "bar"]}))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().excluded == {"foo", "bar"}
        # Second call must NOT hit the gateway again.
        urlopen.reset_mock()
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().excluded == {"foo", "bar"}
        assert urlopen.call_count == 0

    def test_a_non_list_exclude_is_unreadable_not_empty(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(return_value=_make_http_response({"exclude": "not-a-list"}))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        # The gateway answering is not the same as this having understood the
        # answer. A present ``exclude`` of the wrong shape is a policy whose
        # meaning is unknown, so it is reported unknown rather than narrowed to
        # an empty set that would read as "the operator excluded nothing".
        assert policy.unresolved == "policy_unreadable"
        ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
        assert "tool_policy.unreadable" in ops

    def test_an_absent_exclude_key_is_a_resolved_empty_policy(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """The ordinary case, and the one that must not be refused.

        Most agents declare no exclusions at all. That is a real empty policy,
        not an unreadable one, and treating it as unknown would refuse every
        call for every such agent.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(return_value=_make_http_response({}))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        assert policy.unresolved == ""

    def test_a_non_string_entry_makes_the_policy_unreadable(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """Enforcing only the entries that parse would enforce a policy nobody wrote."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(
            return_value=_make_http_response({"exclude": ["foo", 42, None, "bar"]})
        )
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        assert policy.unresolved == "policy_unreadable"

    def test_a_well_formed_exclude_resolves(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(return_value=_make_http_response({"exclude": ["foo", "bar"]}))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == {"foo", "bar"}
        assert policy.unresolved == ""


# ─────────────────────────────────────────────────────────────────────
# Startup-race short cache (no session key, 404).
# ─────────────────────────────────────────────────────────────────────

class TestShortCacheStartupRace:
    def test_no_session_key_uses_short_cache(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        # No session_pid file in cfg_dir → resolver can't find a key.
        # urlopen should never be called.
        urlopen = MagicMock()
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        # Path 1 of 3: the empty set must be marked unresolved so a call
        # site refuses instead of reading it as "nothing is excluded".
        assert policy.unresolved == "no_session_key"
        assert urlopen.call_count == 0
        # Audit event recorded.
        ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
        assert "tool_policy.no_session_key" in ops
        # Short cache populated, NOT long.
        assert mcp_shared._last_startup_race_time > 0
        assert mcp_shared._last_failure_time == 0.0

    def test_404_response_uses_short_cache(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(404))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        # Path 2 of 3.
        assert policy.unresolved == "agent_not_resolved"
        ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
        assert "tool_policy.agent_not_resolved" in ops
        assert mcp_shared._last_startup_race_time > 0
        assert mcp_shared._last_failure_time == 0.0

    def test_short_cache_window_short_circuits(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        # Trip the short cache, then ensure the next call doesn't re-query.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(404))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            mcp_shared._resolve_tool_policy()
        urlopen.reset_mock()
        # A second call inside the cache window is silent — should hit the
        # negative-cache short-circuit and never call urlopen again.
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        # The CACHED form of a failure carries the reason of the clock it hit,
        # not a generic one: the short window caches an identity race and the
        # long window caches a gateway that was reached and failed, and the call
        # site treats those differently. One shared reason would repeat the very
        # conflation this resolver exists to undo, one level down.
        assert policy.unresolved == "no_session_key"
        assert urlopen.call_count == 0
        ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
        assert "tool_policy.negative_cache_hit" in ops

    def test_short_cache_expires_after_ttl(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        # Simulate the short TTL expiry by advancing monotonic.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(404))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            mcp_shared._resolve_tool_policy()
        # Move time past the short TTL.
        with patch.object(
            mcp_shared.time,
            "monotonic",
            return_value=mcp_shared._last_startup_race_time
            + mcp_shared._STARTUP_RACE_CACHE_TTL
            + 1,
        ):
            urlopen.reset_mock()
            with patch.object(mcp_shared, "loopback_urlopen", urlopen):
                mcp_shared._resolve_tool_policy()
            # Cache window expired → resolver retried (urlopen called once).
            assert urlopen.call_count == 1


# ─────────────────────────────────────────────────────────────────────
# Long failure cache.
# ─────────────────────────────────────────────────────────────────────

class TestLongCacheFailures:
    def test_409_is_unreadable_and_is_not_negative_cached(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """A 409 means the gateway read a spec and could not determine its policy.

        It must mark the policy unresolved so the call is refused, and must NOT
        populate either negative cache: the windows exist to debounce 5s urlopen
        timeouts and this answer is immediate, while both clocks are
        process-global, so caching a single session's malformed spec there would
        refuse tool calls for every sibling session in a pooled backend.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(409))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        assert policy.unresolved == "policy_unreadable"
        ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
        assert "tool_policy.unreadable" in ops
        assert mcp_shared._last_failure_time == 0.0
        assert mcp_shared._last_startup_race_time == 0.0
        # And the next call re-asks rather than being short-circuited.
        urlopen.reset_mock()
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().unresolved == "policy_unreadable"
        assert urlopen.call_count == 1

    def test_403_is_a_boundary_the_gateway_holds_not_a_failure(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """A declined caller gets its own reason, out of the failure catch-all.

        403 is ``member_session_unverified``: the gateway answered and would not
        tell THIS caller. Folding it into the catch-all would make one reason mean
        both "the gateway is down" and "the gateway is enforcing a boundary", and
        a refusal derived from that would deny every private member session
        permanently. Like the 409, it answers instantly and is specific to one
        caller's identity, so neither process-global clock may record it.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        for status in (403, 400):
            mcp_shared._last_failure_time = 0.0
            mcp_shared._last_startup_race_time = 0.0
            urlopen = MagicMock(side_effect=_make_http_error(status))
            with patch.object(mcp_shared, "loopback_urlopen", urlopen):
                policy = mcp_shared._resolve_tool_policy()
            assert policy.excluded == set()
            assert policy.unresolved == "policy_forbidden", status
            assert policy.unresolved not in mcp_shared._UNRESOLVED_REFUSES_CALL
            assert mcp_shared._last_failure_time == 0.0, status
            assert mcp_shared._last_startup_race_time == 0.0, status
        ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
        assert "tool_policy.forbidden" in ops

    def test_an_unenumerated_4xx_is_permissive_not_an_outage(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """The 4xx test is the status CLASS, so an unfamiliar code cannot brick.

        This is the property, not the specific codes: whatever the endpoint grows
        next, a 4xx means it ANSWERED and decided something about this caller. If
        the decision were driven by an enumerated list instead, a status the list
        never learned would fall into the failure catch-all and be refused, which
        would deny a whole class of callers over something that is not a failure.
        401 and 422 are here precisely because no arm names them.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        for status in (401, 422, 451):
            mcp_shared._last_failure_time = 0.0
            mcp_shared._last_startup_race_time = 0.0
            urlopen = MagicMock(side_effect=_make_http_error(status))
            with patch.object(mcp_shared, "loopback_urlopen", urlopen):
                policy = mcp_shared._resolve_tool_policy()
            assert policy.unresolved == "policy_forbidden", status
            assert policy.unresolved not in mcp_shared._UNRESOLVED_REFUSES_CALL
            assert mcp_shared._last_failure_time == 0.0, status

    def test_a_5xx_is_a_refusing_reason_not_a_boundary(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """A 5xx is the gateway saying it is broken, which is a failure to read.

        The other half of the same class test: 5xx must NOT be swept into the
        permissive branch alongside the 4xx codes, or a broken gateway would
        serve an unread policy as an operator's permission.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        for status in (500, 502, 503):
            mcp_shared._last_failure_time = 0.0
            urlopen = MagicMock(side_effect=_make_http_error(status))
            with patch.object(mcp_shared, "loopback_urlopen", urlopen):
                policy = mcp_shared._resolve_tool_policy()
            assert policy.unresolved == "resolution_failed", status

    def test_no_answer_is_told_apart_from_an_answered_refusal(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """A transport failure and an answered 4xx must not share one reason.

        Both are currently permissive, so this pins the DISTINCTION rather than
        either verdict: whoever revisits whether a gateway that cannot answer
        should refuse needs a reason that means only that, and collapsing the two
        again would remove the only handle for making that change safely.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=OSError("connection refused"))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        mcp_shared._last_failure_time = 0.0
        urlopen = MagicMock(side_effect=_make_http_error(403))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().unresolved == "policy_forbidden"

    def test_long_cache_hit_reports_the_refusing_reason(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        """The long window caches a gateway that was REACHED and failed.

        That is a reason a call site refuses on, so the cached form has to carry
        it rather than an identity reason -- otherwise a 60s window of
        reached-and-failed reads would be served as the permissive class.
        """
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(500))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().unresolved == "resolution_failed"
        urlopen.reset_mock()
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            cached = mcp_shared._resolve_tool_policy()
        assert urlopen.call_count == 0, "the long window should short-circuit"
        assert cached.unresolved == "resolution_failed"

    def test_500_uses_long_cache(self, fake_sel, patch_session_setup, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(500))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            policy = mcp_shared._resolve_tool_policy()
        assert policy.excluded == set()
        # Path 3 of 3.
        assert policy.unresolved == "resolution_failed"
        ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
        assert "tool_policy.resolution_failed" in ops
        # Long cache populated.
        assert mcp_shared._last_failure_time > 0
        # Short cache untouched.
        assert mcp_shared._last_startup_race_time == 0.0

    def test_url_error_uses_long_cache(self, fake_sel, patch_session_setup, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=urllib.error.URLError("connection refused"))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().excluded == set()
        assert mcp_shared._last_failure_time > 0

    def test_long_cache_short_circuits_repeated_calls(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(500))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            mcp_shared._resolve_tool_policy()
        urlopen.reset_mock()
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            mcp_shared._resolve_tool_policy()
        assert urlopen.call_count == 0


# ─────────────────────────────────────────────────────────────────────
# Warning suppression.
# ─────────────────────────────────────────────────────────────────────

class TestWarningSuppression:
    def _drive_failures(self, fake_sel, patch_session_setup, monkeypatch, n: int):
        """Trigger *n* sequential long-cache failures by busting the cache
        between calls (advance monotonic past TTL each time)."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(side_effect=_make_http_error(500))
        for _ in range(n):
            mcp_shared._last_failure_time = 0.0
            mcp_shared._last_startup_race_time = 0.0
            mcp_shared._excluded_tools_by_session.clear()
            with patch.object(mcp_shared, "loopback_urlopen", urlopen):
                mcp_shared._resolve_tool_policy()

    def test_first_failures_emit_warnings(
        self, caplog, fake_sel, patch_session_setup, monkeypatch
    ):
        caplog.set_level(logging.WARNING, logger="kiro_crew.mcp_shared")
        self._drive_failures(fake_sel, patch_session_setup, monkeypatch, n=2)
        warning_messages = [r.getMessage() for r in caplog.records]
        warn_count = sum(
            1 for m in warning_messages if "Tool policy resolution failed" in m
        )
        assert warn_count == 2

    def test_warning_after_threshold_is_suppressed_with_notice(
        self, caplog, fake_sel, patch_session_setup, monkeypatch
    ):
        caplog.set_level(logging.WARNING, logger="kiro_crew.mcp_shared")
        # 3 failures: first 2 emit the full warning, 3rd emits the
        # one-shot suppression notice.
        self._drive_failures(fake_sel, patch_session_setup, monkeypatch, n=3)
        msgs = [r.getMessage() for r in caplog.records]
        full_warns = sum(1 for m in msgs if "Tool policy resolution failed" in m)
        suppressed_notice = sum(
            1 for m in msgs if "further warnings suppressed" in m
        )
        assert full_warns == 2
        assert suppressed_notice == 1

    def test_subsequent_failures_silent(
        self, caplog, fake_sel, patch_session_setup, monkeypatch
    ):
        caplog.set_level(logging.WARNING, logger="kiro_crew.mcp_shared")
        # 5 failures total — only 3 log lines (2 warnings + 1 suppression notice).
        self._drive_failures(fake_sel, patch_session_setup, monkeypatch, n=5)
        msgs = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kiro_crew.mcp_shared"
        ]
        assert len(msgs) == 3


# ─────────────────────────────────────────────────────────────────────
# Cross-cache interaction.
# ─────────────────────────────────────────────────────────────────────

class TestCachesAreIndependent:
    def test_short_cache_hit_alone_short_circuits(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        # Set the env var so the resolver would otherwise reach urlopen —
        # the cache short-circuit at the top is the ONLY thing preventing
        # the call, which is exactly what this test asserts.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        # Manually populate only the short cache.
        mcp_shared._last_startup_race_time = mcp_shared.time.monotonic()
        mcp_shared._last_failure_time = 0.0
        urlopen = MagicMock()
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().excluded == set()
        assert urlopen.call_count == 0

    def test_long_cache_hit_alone_short_circuits(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        # See ``test_short_cache_hit_alone_short_circuits`` rationale — set
        # the session key so urlopen would be reachable absent the cache.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        mcp_shared._last_failure_time = mcp_shared.time.monotonic()
        mcp_shared._last_startup_race_time = 0.0
        urlopen = MagicMock()
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            assert mcp_shared._resolve_tool_policy().excluded == set()
        assert urlopen.call_count == 0

    def test_neither_cache_hit_does_query(
        self, fake_sel, patch_session_setup, monkeypatch
    ):
        # Both caches expired (or never set) → resolver MUST query.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:abc")
        urlopen = MagicMock(return_value=_make_http_response({"exclude": []}))
        with patch.object(mcp_shared, "loopback_urlopen", urlopen):
            mcp_shared._resolve_tool_policy()
        assert urlopen.call_count == 1
