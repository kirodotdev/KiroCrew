"""Tests for the gateway tick that renews the stored Kiro sign-in.

Driven against a real :class:`TokenStore` in a temp data home, with the HTTP
session replaced by a scripted fake: the tick must call the same refresh the
engine callback uses, only when the token is inside the refresh margin, never
retry a refused grant, and back off on transient failures.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.auth.store import REFRESH_MARGIN_SECS, KasToken, TokenStore
from kiro_crew.dashboard import sign_in_refresher as tick


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, *, content_type: str | None = "application/json"):
        return self._payload

    async def text(self):
        return str(self._payload)


class _FakeSession:
    """Stands in for ``aiohttp.ClientSession()``; fails the test on an unscripted post."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, *, json=None, data=None, headers=None):  # noqa: A002
        self.calls.append(url)
        assert self._responses, f"unexpected refresh request to {url}"
        return self._responses.pop(0)


@pytest.fixture
def session(monkeypatch):
    """Script the responses the tick's refresh will see."""
    holder: dict[str, _FakeSession] = {}

    def _script(*responses) -> _FakeSession:
        fake = _FakeSession(responses)
        holder["s"] = fake
        return fake

    def _factory(*_a, **_kw):
        return holder.setdefault("s", _FakeSession([]))

    monkeypatch.setattr("aiohttp.ClientSession", _factory)
    return _script


def _token(*, ttl: int, refresh_token: str | None = "rt-old") -> KasToken:
    return KasToken(
        access_token="at-old",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
        provider="Google",
        identity="social",
        refresh_token=refresh_token,
        profile_arn="arn:x",
    )


_RENEWED = {"accessToken": "at-new", "refreshToken": "rt-new", "expiresIn": 3600}


@pytest.mark.asyncio
async def test_no_vault_idles_without_touching_the_network(tmp_path: Path, session):
    fake = session()
    delay, failures = await tick.refresh_once(tmp_path)
    assert (delay, failures) == (tick.IDLE_INTERVAL_SECS, 0)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_token_outside_margin_sleeps_until_it_is_due(tmp_path: Path, session):
    fake = session()
    TokenStore(tmp_path).save(_token(ttl=REFRESH_MARGIN_SECS + 60))
    delay, failures = await tick.refresh_once(tmp_path)
    assert failures == 0
    # Wakes just past the margin boundary, not at the idle cap.
    assert 55 <= delay <= 62
    assert fake.calls == []
    assert TokenStore(tmp_path).load("social").access_token == "at-old"


@pytest.mark.asyncio
async def test_far_expiry_is_capped_at_the_idle_interval(tmp_path: Path, session):
    session()
    TokenStore(tmp_path).save(_token(ttl=7200))
    delay, _ = await tick.refresh_once(tmp_path)
    assert delay == tick.IDLE_INTERVAL_SECS


@pytest.mark.asyncio
async def test_due_token_is_renewed_and_stored(tmp_path: Path, session):
    fake = session(_FakeResp(200, _RENEWED))
    TokenStore(tmp_path).save(_token(ttl=60))
    delay, failures = await tick.refresh_once(tmp_path)
    assert (delay, failures) == (tick.MIN_INTERVAL_SECS, 0)
    assert len(fake.calls) == 1
    stored = TokenStore(tmp_path).load("social")
    assert stored.access_token == "at-new"
    assert stored.refresh_token == "rt-new"
    assert not stored.is_expired()

    # The next pass schedules against the renewed expiry instead of refreshing again.
    delay, _ = await tick.refresh_once(tmp_path)
    assert delay == tick.IDLE_INTERVAL_SECS
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_refused_grant_is_recorded_and_not_retried(tmp_path: Path, session):
    fake = session(_FakeResp(401, "invalid_grant"))
    store = TokenStore(tmp_path)
    store.save(_token(ttl=60))
    delay, failures = await tick.refresh_once(tmp_path)
    assert (delay, failures) == (tick.IDLE_INTERVAL_SECS, 0)
    assert store.refresh_rejected("social") is not None

    # A second pass does not ask the issuer again while the refusal stands.
    delay, _ = await tick.refresh_once(tmp_path)
    assert delay == tick.IDLE_INTERVAL_SECS
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_transient_failure_backs_off_and_caps(tmp_path: Path, session):
    TokenStore(tmp_path).save(_token(ttl=60))
    session(*[_FakeResp(503, "busy") for _ in range(6)])
    delays = []
    failures = 0
    for _ in range(6):
        delay, failures = await tick.refresh_once(tmp_path, failures=failures)
        delays.append(delay)
    assert delays[:4] == [30.0, 60.0, 120.0, 240.0]
    assert delays[4:] == [tick.IDLE_INTERVAL_SECS, tick.IDLE_INTERVAL_SECS]
    assert failures == 6
    assert TokenStore(tmp_path).refresh_rejected("social") is None


@pytest.mark.asyncio
async def test_renewal_shorter_than_the_margin_backs_off(tmp_path: Path, session):
    """An issuer handing back a lifetime inside the margin must not be polled every few seconds."""
    short = dict(_RENEWED, expiresIn=REFRESH_MARGIN_SECS - 30)
    session(_FakeResp(200, short), _FakeResp(200, short))
    TokenStore(tmp_path).save(_token(ttl=60))
    delay, failures = await tick.refresh_once(tmp_path)
    assert (delay, failures) == (tick.RETRY_BASE_SECS, 1)
    delay, failures = await tick.refresh_once(tmp_path, failures=failures)
    assert (delay, failures) == (tick.RETRY_BASE_SECS * 2, 2)


@pytest.mark.asyncio
async def test_nothing_to_renew_with_is_left_alone(tmp_path: Path, session):
    fake = session()
    TokenStore(tmp_path).save(_token(ttl=60, refresh_token=None))
    delay, failures = await tick.refresh_once(tmp_path)
    assert (delay, failures) == (tick.IDLE_INTERVAL_SECS, 0)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_failure_log_carries_no_token_value(tmp_path: Path, session, caplog):
    session(_FakeResp(503, "at-old rt-old"))
    TokenStore(tmp_path).save(_token(ttl=60))
    caplog.set_level("DEBUG", logger=tick.__name__)
    await tick.refresh_once(tmp_path)
    assert "rt-old" not in caplog.text
    assert "at-old" not in caplog.text


@pytest.mark.asyncio
async def test_loop_stops_on_shutdown(tmp_path: Path, monkeypatch):
    calls = 0

    async def _once(_home, *, failures=0):
        nonlocal calls
        calls += 1
        return 3600.0, 0

    monkeypatch.setattr(tick, "refresh_once", _once)
    monkeypatch.setattr(tick, "data_home", lambda: tmp_path)
    stop = asyncio.Event()
    task = asyncio.create_task(tick.run_sign_in_refresher(stop))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert calls == 1


@pytest.mark.asyncio
async def test_loop_survives_an_unexpected_error(tmp_path: Path, monkeypatch):
    calls = 0
    stop = asyncio.Event()

    async def _once(_home, *, failures=0):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        stop.set()
        return 3600.0, 0

    monkeypatch.setattr(tick, "refresh_once", _once)
    monkeypatch.setattr(tick, "data_home", lambda: tmp_path)
    monkeypatch.setattr(tick, "IDLE_INTERVAL_SECS", 0.01)
    await asyncio.wait_for(tick.run_sign_in_refresher(stop), timeout=5)
    assert calls == 2


def test_importing_the_tick_does_not_load_the_auth_subsystem():
    """The gateway imports this module at boot; the auth stack (and the
    cryptography wheel behind it) must still load only on first use."""
    code = (
        "import sys; import kiro_crew.dashboard.sign_in_refresher; "
        "bad = [m for m in sys.modules if m.startswith('kiro_crew.auth') or m == 'cryptography']; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, f"tick import pulled in auth modules: {proc.stdout}{proc.stderr}"
