"""Tests for the durable session-pulse session counter (session_pulse_counter).

Covers the count that gates the survey's "new user" window: default 0 when
unset, atomic increment + persistence, monotonic sequential increments, and
fail-safe handling of a missing/corrupt file (treated as 0, never raising).
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard import session_pulse_counter as spc


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # Point the counter's config_dir at a throwaway dir so tests never touch the
    # real ~/.kiro/crew state.
    monkeypatch.setattr(spc, "config_dir", lambda: tmp_path)
    return tmp_path


def test_get_returns_zero_when_file_absent() -> None:
    assert spc.get_user_session_count() == 0


def test_increment_creates_file_and_returns_one(_isolated_home) -> None:
    assert spc.increment_user_session_count() == 1
    # Persisted to disk under config_dir.
    path = _isolated_home / "session_pulse_sessions.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {"user_sessions": 1}


def test_sequential_increments_are_monotonic() -> None:
    assert [spc.increment_user_session_count() for _ in range(3)] == [1, 2, 3]
    assert spc.get_user_session_count() == 3


class TestOffLoopIncrement:
    """The slot-birth path must not do this module's disk I/O on the event loop.

    `get_or_create_slot` is synchronous and every request-layer birth -- a new
    chat tab, a fork, the session-control create verb -- runs it on the gateway
    loop, so an inline read + mkdir + tempfile write + replace stalls the loop on
    slow storage. The offload belongs to the counter rather than the allocation:
    suspending inside `get_or_create_slot` would let callers observe a
    half-configured slot.
    """

    def test_without_a_running_loop_it_increments_inline(_self, _isolated_home) -> None:
        # A CLI, a test, or a background thread has no loop to protect, so the
        # count must still land rather than being silently dropped.
        spc.increment_user_session_count_off_loop()
        assert spc.get_user_session_count() == 1

    @pytest.mark.asyncio
    async def test_with_a_running_loop_the_io_leaves_the_loop_thread(_self, _isolated_home) -> None:
        import asyncio
        import threading

        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = spc.increment_user_session_count

        def _recording() -> int:
            # Recorded AFTER the write returns, not before: the poll below exits as
            # soon as `seen` is non-empty, so appending first would let the test --
            # and `tmp_path` teardown -- run while this worker is still writing.
            # The ident is the same either way, since the whole wrapper runs on the
            # worker thread.
            result = real()
            seen.append(threading.get_ident())
            return result

        spc.increment_user_session_count = _recording  # type: ignore[assignment]
        try:
            spc.increment_user_session_count_off_loop()
            # Fire-and-forget: give the executor a moment to pick the job up.
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0.01)
        finally:
            spc.increment_user_session_count = real  # type: ignore[assignment]

        assert seen, "the increment never ran"
        assert seen[0] != loop_thread, (
            "the counter's read-modify-write ran on the event loop thread; a slow "
            "or contended disk would stall every session in the gateway"
        )


def test_corrupt_file_is_treated_as_zero(_isolated_home) -> None:
    (_isolated_home / "session_pulse_sessions.json").write_text("{not json", encoding="utf-8")
    assert spc.get_user_session_count() == 0
    # A corrupt file must not wedge the counter forever: the next increment
    # overwrites it with a clean value.
    assert spc.increment_user_session_count() == 1


def test_negative_or_wrong_type_value_is_treated_as_zero(_isolated_home) -> None:
    path = _isolated_home / "session_pulse_sessions.json"
    path.write_text(json.dumps({"user_sessions": -5}), encoding="utf-8")
    assert spc.get_user_session_count() == 0
    path.write_text(json.dumps({"user_sessions": "10"}), encoding="utf-8")
    assert spc.get_user_session_count() == 0


@pytest.mark.parametrize("raw", ["null", "5", '"x"', "[1, 2]", "true"])
def test_valid_non_object_json_is_treated_as_zero_not_a_crash(_isolated_home, raw) -> None:
    # A valid JSON file that is not an object has no ``.get`` -- it must NOT
    # raise (this runs inside session creation), just read as 0. Regression for
    # the GPT blocking finding "valid non-object JSON crashes session creation".
    (_isolated_home / "session_pulse_sessions.json").write_text(raw, encoding="utf-8")
    assert spc.get_user_session_count() == 0
    # And the counter still recovers cleanly on the next increment.
    assert spc.increment_user_session_count() == 1
