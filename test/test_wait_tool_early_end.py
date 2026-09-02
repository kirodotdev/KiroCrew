"""Tests for the `wait` tool's sleep loop and its early-end path.

The sleeping tool runs in an MCP subprocess with no listener, so its own
keepalive POST is the only channel that can reach it. These tests drive the loop
with a fake monotonic clock advanced by the fake ``time.sleep``, so a 60s wait
runs instantly while every ping boundary lands exactly where production would
put it.
"""

from __future__ import annotations

import threading
import time as _time
from unittest.mock import patch

import pytest

from kiro_crew.mcp_core import WAIT_PING_SECS, _call_tool

# Smallest wait the handler allows (seconds = max(60, ...)), so the loop makes
# 60 / WAIT_PING_SECS = 12 countdown pings before it would elapse naturally.
MIN_WAIT = 60
PINGS_TO_TERM = int(MIN_WAIT / WAIT_PING_SECS)
KEEPALIVE = "/api/session-keepalive"


class _Clock:
    """Monotonic clock advanced only by the wait loop's own sleeps.

    Modelled as an object rather than an iterator of timestamps so that an
    unrelated ``time.monotonic()`` call from the audit path cannot consume a
    value the loop needed and silently change the ping cadence under test.

    Thread-scoped via object identity (``is``) so no other live thread can
    satisfy the check.

    Additionally, the clock checks the *immediate caller's module* to
    distinguish calls originating from the code-under-test (the ``kirocrew-core``
    MCP server) from infrastructure calls that happen to share the same thread
    (e.g. pytest-xdist worker heartbeats that issue process-global
    ``time.sleep(0.001)`` between test iterations). Only calls whose immediate
    caller resides in that server advance the fake timeline; all others fall
    through to the real implementation.
    """

    def __init__(self) -> None:
        self.t = 0.0
        # Store the thread object, not its numeric ident.  Object identity
        # (``is``) guarantees that only the constructing thread can advance
        # the clock — no other live thread can satisfy the check.
        self._owner = threading.current_thread()
        # Capture the real functions before patching replaces them on _time.
        self._real_monotonic = _time.monotonic
        self._real_sleep = _time.sleep

    @staticmethod
    def _caller_is_code_under_test() -> bool:
        """True when the immediate caller of sleep/monotonic is the core server.

        Matches the whole server rather than one file: the ``wait`` handler lives
        in ``mcp_tools/control.py`` while the plumbing it calls stays in
        ``mcp_core.py``. Keying on a single module name would silently stop
        advancing the fake timeline and leave the loop sleeping for real.
        """
        import sys

        # frame 0 = this method, frame 1 = sleep/monotonic, frame 2 = actual caller
        frame = sys._getframe(2)
        filename = frame.f_code.co_filename
        return "mcp_core" in filename or "mcp_tools" in filename

    def monotonic(self) -> float:
        if threading.current_thread() is not self._owner or not self._caller_is_code_under_test():
            return self._real_monotonic()
        return self.t

    def sleep(self, secs: float) -> None:
        if threading.current_thread() is not self._owner or not self._caller_is_code_under_test():
            self._real_sleep(secs)
            return
        self.t += max(0.0, float(secs))


def _run_wait(reply_fn, *, seconds: int = MIN_WAIT, reason: str = "test", identified: bool = True):
    """Run the real `wait` tool against *reply_fn*.

    ``reply_fn(nth_post, body) -> dict`` decides what the gateway answers on
    each POST, where ``nth_post`` is 1-based.

    ``identified`` stubs ``_resolve_session_key_strict``, which the tool consults
    before publishing any wait metadata. It defaults to True because that is the
    only configuration in which the countdown exists at all; pass False to
    exercise the degraded path (touch-only pings, `end_wait` ignored).
    """
    clock = _Clock()
    posts: list[tuple[str, dict, float]] = []

    def _fake_post(path, body=None, **kwargs):
        posts.append((path, dict(body or {}), clock.t))
        return reply_fn(len(posts), dict(body or {}))

    with (
        patch("kiro_crew.mcp_core._post", side_effect=_fake_post),
        patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value="dashboard:test" if identified else "",
        ),
        patch.object(_time, "monotonic", clock.monotonic),
        patch.object(_time, "sleep", clock.sleep),
    ):
        result = _call_tool("wait", {"seconds": seconds, "reason": reason})

    return result, posts, clock


def _countdown_posts(posts):
    return [p for p in posts if not p[1].get("wait_done")]


def _done_posts(posts):
    return [p for p in posts if p[1].get("wait_done")]


class TestWaitLoopEarlyEnd:
    def test_matching_end_wait_returns_early_and_does_not_raise(self):
        """K(i). The reply names THIS wait, so the sleep returns a normal tool
        result — raising ToolCancelled would suppress the response and strand
        kiro-cli until the 600s stall watchdog."""

        def replies(n, body):
            if n == 3:
                return {"ok": True, "end_wait": body["wait_id"]}
            return {"ok": True}

        # Reaching this assertion at all is the "does not raise" half: nothing
        # between _call_tool and here catches an exception from the handler.
        result, posts, clock = _run_wait(replies)

        assert result.startswith("Wait ended early by the user after")
        # Ended on the 3rd ping, i.e. after 2 sleeps of WAIT_PING_SECS.
        assert clock.t == 2 * WAIT_PING_SECS
        assert f"after {int(2 * WAIT_PING_SECS)}s of {MIN_WAIT}s" in result
        assert result.endswith("Resuming: test")
        assert len(_countdown_posts(posts)) == 3

    def test_end_wait_for_a_different_id_is_ignored(self):
        """K(ii). Only a request naming this sleep ends it; anything else lets
        the wait run to term."""

        def replies(n, body):
            return {"ok": True, "end_wait": "some-other-wait-id"}

        result, posts, clock = _run_wait(replies)

        assert result == f"Waited {MIN_WAIT}s. Resuming: test"
        assert clock.t == float(MIN_WAIT)
        assert len(_countdown_posts(posts)) == PINGS_TO_TERM

    def test_end_wait_echoing_a_truncated_id_is_ignored(self):
        """Equality, not prefix matching."""

        def replies(n, body):
            return {"ok": True, "end_wait": body["wait_id"][:8]}

        result, _, clock = _run_wait(replies)

        assert result == f"Waited {MIN_WAIT}s. Resuming: test"
        assert clock.t == float(MIN_WAIT)

    @pytest.mark.parametrize(
        "reply",
        [
            {"error": "gateway offline"},
            {"error": "session not found"},
            {},
            {"ok": True},
            {"end_wait": None},
            {"end_wait": ""},
            None,
            "not-a-dict",
            [],
        ],
        ids=[
            "error-offline",
            "error-no-session",
            "empty",
            "ok-only",
            "end-wait-none",
            "end-wait-blank",
            "none",
            "string",
            "list",
        ],
    )
    def test_replies_without_a_matching_end_wait_never_shorten_the_wait(self, reply):
        """K(iii). ``_post`` returns {"error": ...} instead of raising, so the
        equality check has to double as the error guard — and a non-dict reply
        must not blow up the isinstance guard either."""
        result, _, clock = _run_wait(lambda n, body: reply)

        assert result == f"Waited {MIN_WAIT}s. Resuming: test"
        assert clock.t == float(MIN_WAIT)

    def test_raising_post_is_best_effort_and_the_wait_still_terminates(self):
        """A dead gateway must not turn a wait into an exception."""

        def replies(n, body):
            raise ConnectionRefusedError("gateway down")

        result, posts, clock = _run_wait(replies)

        assert result == f"Waited {MIN_WAIT}s. Resuming: test"
        assert clock.t == float(MIN_WAIT)
        # Every ping attempted, including the final wait_done.
        assert len(posts) == PINGS_TO_TERM + 1

    def test_background_thread_sleeps_do_not_advance_fake_clock(self):
        """Regression: a background thread calling time.sleep() must not pollute
        the deterministic clock. Before the thread-scoping fix, any process-wide
        sleep leaked into the fake timeline and broke exact-equality asserts."""
        leaked = threading.Event()
        stop = threading.Event()

        def _background_sleeper():
            """Simulates pytest-xdist or any other background thread."""
            leaked.set()
            while not stop.is_set():
                _time.sleep(0.001)

        bg = threading.Thread(target=_background_sleeper, daemon=True)
        bg.start()
        leaked.wait(timeout=2)  # ensure background is actively sleeping

        try:
            result, _, clock = _run_wait(lambda n, body: {"ok": True})
        finally:
            stop.set()
            bg.join(timeout=2)

        assert result == f"Waited {MIN_WAIT}s. Resuming: test"
        assert clock.t == float(MIN_WAIT)

    def test_other_thread_never_advances_fake_clock(self):
        """Pin the object-identity guard: a different thread calling
        clock.sleep must fall through to the real sleep, leaving the fake
        timeline untouched.  This holds regardless of ident values because
        the check uses ``is not`` on the thread object itself."""
        clock = _Clock()
        # Neutralize the real-sleep fallback so the test finishes instantly.
        clock._real_sleep = lambda secs: None

        leaked: list[float] = []

        def _impostor():
            """A different thread that calls clock.sleep directly."""
            clock.sleep(999.0)
            leaked.append(clock.t)

        t = threading.Thread(target=_impostor)
        t.start()
        t.join(timeout=5)
        assert t.is_alive() is False, "impostor thread did not complete"

        # The fake clock must NOT have been advanced by the impostor thread.
        assert clock.t == 0.0
        # The impostor saw the unadvanced clock too.
        assert leaked == [0.0]


class TestUnauthoritativeIdentityGate:
    """The gate that makes the whole channel safe on a default install.

    `_resolve_session_key()` -- what carries the X-Session-Key header -- ends its
    ladder with a /proc ancestor walk, so a subagent's MCP-core child resolves to
    its PARENT's slot. Publishing a wait_id under that key would put a subagent's
    deadline on the parent's pill and let the parent's button end the subagent's
    sleep, and no frontend guard can catch it: with one wait_id pinging there is
    no collision to detect. So the tool consults the strict resolver first and
    publishes nothing when identity is a guess.
    """

    def test_pings_carry_no_metadata_without_authoritative_identity(self):
        result, posts, _ = _run_wait(lambda n, body: {"ok": True}, identified=False)

        assert posts, "the keepalive must still fire -- it is what stops the reap"
        for path, body, _ts in posts:
            assert path == "/api/session-keepalive"
            assert body == {}, f"leaked metadata under a guessed identity: {body!r}"
        assert result.startswith("Waited 60s.")

    def test_end_wait_is_ignored_without_authoritative_identity(self):
        # Echo back whatever wait_id the body carried, which is the only reply
        # shape that can distinguish the gate from its absence: gated, the body
        # has no wait_id so `end_wait` comes back None and matches nothing;
        # ungated, it matches and the wait would end. A literal id would fail to
        # match either way and prove nothing.
        def _echo_end(n, body):
            return {"ok": True, "end_wait": body.get("wait_id")}

        result, posts, _ = _run_wait(_echo_end, identified=False)

        assert "ended early" not in result
        assert result.startswith("Waited 60s.")
        # One ping, not twelve: with no button to keep responsive, the cadence
        # relaxes to the 60s the staleness watchdog needs rather than paying a 12x
        # request multiplier for a latency nobody can observe.
        assert len(_countdown_posts(posts)) == 1

    def test_no_wait_done_ping_when_nothing_was_published(self):
        _result, posts, _ = _run_wait(lambda n, body: {"ok": True}, identified=False)

        # Nothing to retire, and a wait_id sent under a guessed key could blank a
        # countdown belonging to a different session.
        assert _done_posts(posts) == []

    def test_authoritative_identity_still_publishes_and_ends_early(self):
        # Control: the gate is not simply off. Same reply, identity present.
        def _end_on_second(n, body):
            return {"ok": True, "end_wait": body.get("wait_id")} if n == 2 else {"ok": True}

        result, posts, _ = _run_wait(_end_on_second, identified=True)

        assert "ended early" in result
        assert _countdown_posts(posts)[0][1].get("wait_id")
        assert len(_done_posts(posts)) == 1


class TestWaitDonePing:
    def test_wait_done_is_sent_after_a_natural_end(self):
        """K(iv), normal exit."""
        result, posts, _ = _run_wait(lambda n, body: {"ok": True})

        assert result == f"Waited {MIN_WAIT}s. Resuming: test"
        done = _done_posts(posts)
        assert len(done) == 1
        assert done[0][0] == KEEPALIVE
        assert done[0][1] == {
            "wait_id": _countdown_posts(posts)[0][1]["wait_id"],
            "wait_done": True,
        }
        # It is the LAST thing the sleep does.
        assert posts[-1] is done[0]

    def test_wait_done_is_sent_after_an_early_end(self):
        """K(iv), early exit. The tool result travels back through kiro-cli,
        which the dashboard cannot correlate to a wait_id, so the sleep has to
        announce its own end on both paths."""

        def replies(n, body):
            if n == 2:
                return {"ok": True, "end_wait": body["wait_id"]}
            return {"ok": True}

        result, posts, _ = _run_wait(replies)

        assert result.startswith("Wait ended early by the user after")
        done = _done_posts(posts)
        assert len(done) == 1
        assert done[0][1]["wait_id"] == _countdown_posts(posts)[0][1]["wait_id"]
        assert posts[-1] is done[0]


class TestWaitPingShape:
    def test_every_ping_shares_one_wait_id_scoped_to_this_sleep(self):
        _, posts, _ = _run_wait(lambda n, body: {"ok": True})

        ids = {p[1]["wait_id"] for p in posts}
        assert len(ids) == 1
        wait_id = ids.pop()
        assert len(wait_id) == 32  # uuid4().hex
        assert int(wait_id, 16) >= 0  # hex, no separators

    def test_two_sleeps_in_one_session_get_different_ids(self):
        """The whole point of the id: an end request left over from an earlier
        sleep can never terminate the next one."""
        _, first, _ = _run_wait(lambda n, body: {"ok": True})
        _, second, _ = _run_wait(lambda n, body: {"ok": True})

        assert first[0][1]["wait_id"] != second[0][1]["wait_id"]

    def test_pings_land_on_the_ping_interval_and_carry_a_falling_remaining(self):
        _, posts, _ = _run_wait(lambda n, body: {"ok": True})

        countdown = _countdown_posts(posts)
        assert [p[0] for p in countdown] == [KEEPALIVE] * PINGS_TO_TERM
        # WAIT_PING_SECS is the upper bound on how long "End wait" appears to do
        # nothing, so the cadence is part of the contract.
        assert [p[2] for p in countdown] == [i * WAIT_PING_SECS for i in range(PINGS_TO_TERM)]
        assert [p[1]["seconds"] for p in countdown] == [MIN_WAIT] * PINGS_TO_TERM
        assert [p[1]["remaining"] for p in countdown] == [
            MIN_WAIT - int(i * WAIT_PING_SECS) for i in range(PINGS_TO_TERM)
        ]

    def test_reason_is_redacted_on_the_early_path_too(self):
        """The reason is echoed back into the transcript on BOTH exits, so the
        new early-end string has to carry the same redaction as the old one."""
        secret = "poll ghp_1234567890abcdefghij1234567890abcdef"

        normal, _, _ = _run_wait(lambda n, body: {"ok": True}, reason=secret)

        def replies(n, body):
            return {"ok": True, "end_wait": body["wait_id"]} if n == 2 else {"ok": True}

        early, _, _ = _run_wait(replies, reason=secret)

        assert "ghp_1234567890abcdefghij1234567890abcdef" not in normal
        assert "ghp_1234567890abcdefghij1234567890abcdef" not in early
        assert "[REDACTED: credential]" in normal
        assert "[REDACTED: credential]" in early
