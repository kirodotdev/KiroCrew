"""Per-principal storage accounting, the session-create rate, and internal-caller proof."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.shared_crew import internal_caller
from kiro_crew.shared_crew.accounting import SessionCreateRate, StorageAccount
from kiro_crew.shared_crew.internal_caller import (
    ATTESTED_SESSION_KEY,
    CALLER_SESSION_HEADER,
    CALLER_TOKEN_HEADER,
    attest_internal_session,
)
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock

# -- StorageAccount -----------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        yield s
    finally:
        s.close()


def test_storage_charge_credit_and_limit(store):
    account = StorageAccount(store, clock=lambda: 1_000.0)

    async def scenario() -> tuple[int, int, int, int, bool, bool, bool]:
        first = await account.charge("p_alice", 300)
        second = await account.charge("p_alice", 200)
        credited = await account.charge("p_alice", -100)
        other = await account.used("p_bob")
        over = await account.would_exceed("p_alice", 1, 400)
        exact = await account.would_exceed("p_alice", 0, 400)
        # A negative request size never lowers the check below what is used.
        negative = await account.would_exceed("p_alice", -1_000, 399)
        return first, second, credited, other, over, exact, negative

    first, second, credited, other, over, exact, negative = asyncio.run(scenario())
    assert (first, second, credited) == (300, 500, 400)
    assert other == 0
    assert over is True
    assert exact is False
    assert negative is True


# -- SessionCreateRate --------------------------------------------------------


def test_session_create_rate_window_per_principal():
    clock = ManualClock(start=100.0)
    rate = SessionCreateRate(2, clock=clock.time)
    assert rate.allow("p_alice") == (True, 0)
    clock.advance(10.0)
    assert rate.allow("p_alice") == (True, 0)
    allowed, retry = rate.allow("p_alice")
    # The oldest event (t=100) leaves the window at t=160; now is t=110.
    assert (allowed, retry) == (False, 51)
    # Another person has their own allowance.
    assert rate.allow("p_bob") == (True, 0)
    clock.advance(50.0)
    assert rate.allow("p_alice") == (True, 0)


def test_session_create_rate_set_rate_and_floor():
    clock = ManualClock(start=0.0)
    rate = SessionCreateRate(0, clock=clock.time)  # clamped to 1
    assert rate.allow("p_alice") == (True, 0)
    assert rate.allow("p_alice")[0] is False
    rate.set_rate(3)
    assert rate.allow("p_alice") == (True, 0)
    rate.set_rate(-5)  # clamped to 1, and two are already in the window
    assert rate.allow("p_alice")[0] is False


def test_session_create_rate_drops_idle_windows():
    clock = ManualClock(start=0.0)
    rate = SessionCreateRate(1, clock=clock.time)
    assert rate.allow("p_bob") == (True, 0)
    clock.advance(61.0)
    # Bob's window empties on his next call and is then dropped by Alice's.
    assert rate.allow("p_bob") == (True, 0)
    clock.advance(61.0)
    rate._events["p_bob"].clear()
    assert rate.allow("p_alice") == (True, 0)
    assert "p_bob" not in rate._events


# -- attest_internal_session --------------------------------------------------


class _Request(dict):
    def __init__(self, headers: dict[str, str] | None = None, **items: Any) -> None:
        super().__init__(items)
        self.headers = headers if headers is not None else {}


class _NoKeys:
    headers: dict[str, str] = {}

    def get(self, key: str) -> Any:
        raise TypeError("no keys")


class _ReadOnly(_Request):
    def __setitem__(self, key: str, value: Any) -> None:
        raise TypeError("read-only")


@pytest.fixture
def signed(monkeypatch):
    calls: list[str] = []
    table = {"tok-a": "sess-a", "tok-b": "sess-b"}

    def fake_verify(token: str) -> str:
        calls.append(token)
        return table.get(token, "")

    monkeypatch.setattr("kiro_crew.session_token_sig.verify_session_token", fake_verify)
    monkeypatch.setattr("kiro_crew.member_memory_auth.verify_session_token", fake_verify)
    return calls


def test_declared_key_uses_the_shared_transport_check(monkeypatch):
    seen: list[str] = []

    def fake_check(request: Any, key: str) -> bool:
        seen.append(key)
        return True

    monkeypatch.setattr("kiro_crew.member_memory_auth.session_key_is_attested", fake_check)
    assert _attest(_Request({CALLER_SESSION_HEADER: "sess-a"})) == "sess-a"
    assert seen == ["sess-a"]


def _attest(request: Any) -> str:
    return asyncio.run(attest_internal_session(request))


def test_header_alone_proves_nothing(signed):
    req = _Request({CALLER_SESSION_HEADER: "sess-a"})
    assert _attest(req) == ""
    assert req[ATTESTED_SESSION_KEY] == ""
    assert signed == []


def test_peer_verified_declared_session_is_proved(signed):
    req = _Request({CALLER_SESSION_HEADER: " sess-a "}, peer_verified=True)
    assert _attest(req) == "sess-a"
    assert signed == []


def test_peer_verified_must_be_literal_true(signed):
    req = _Request({CALLER_SESSION_HEADER: "sess-a"}, peer_verified="yes")
    assert _attest(req) == ""


def test_signed_token_proves_matching_or_undeclared_session(signed):
    assert _attest(_Request({CALLER_SESSION_HEADER: "sess-a", CALLER_TOKEN_HEADER: "tok-a"})) == (
        "sess-a"
    )
    assert _attest(_Request({CALLER_TOKEN_HEADER: "tok-b"})) == "sess-b"


def test_signed_token_for_another_session_is_refused(signed):
    req = _Request({CALLER_SESSION_HEADER: "sess-a", CALLER_TOKEN_HEADER: "tok-b"})
    assert _attest(req) == ""


def test_unknown_token_proves_nothing(signed):
    assert _attest(_Request({CALLER_TOKEN_HEADER: "tok-x"})) == ""


def test_oversized_declared_key_is_ignored(signed):
    req = _Request({CALLER_SESSION_HEADER: "s" * 513}, peer_verified=True)
    assert _attest(req) == ""


def test_answer_is_cached_on_the_request(signed):
    req = _Request({CALLER_TOKEN_HEADER: "tok-a"})
    assert _attest(req) == "sess-a"
    assert _attest(req) == "sess-a"
    assert signed == ["tok-a"]


def test_request_without_keys_or_headers(signed):
    assert _attest(_NoKeys()) == ""
    req = _Request()
    req.headers = None  # type: ignore[assignment]
    assert _attest(req) == ""


def test_read_only_request_still_answers(signed):
    assert _attest(_ReadOnly({CALLER_TOKEN_HEADER: "tok-a"})) == "sess-a"


def test_verify_token_reads_the_signed_mapping(monkeypatch):
    monkeypatch.setattr("kiro_crew.session_token_sig.verify_session_token", lambda t: "sess-z")
    assert internal_caller._verify_token("tok") == "sess-z"
