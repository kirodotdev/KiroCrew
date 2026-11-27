"""Internal-secret calls that name no session, from a caller the kernel does not place on the host.

``dashboard.token_auth_sessionless`` checks such a call with the local owner-token
mint's host-provenance evidence. In ``"log"`` mode (the default) it admits the call
and writes one security-log row per caller and path per window; in ``"enforce"`` it
refuses with 403 ``session_required``. A call that names a session, one the agent
socket admitted on a capability, a host caller and every Windows call are never
checked. The host check itself is replaced here: its kernel evidence is
``member_memory_auth``'s and is pinned there.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard import token_auth

SECRET = "internal-secret-sessionless"
INTERNAL = frozenset({"/api/crons"})


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sel_rows(_floor_monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    recorder = MagicMock()
    recorder.log_api_access.side_effect = lambda **kw: rows.append(kw)
    _floor_monkeypatch.setattr(token_auth, "_sel_fn", lambda: recorder)
    return rows


@pytest.fixture
def host(_floor_monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Whether the caller is on the host, and how many times that was asked."""
    answer: dict[str, Any] = {"on_host": False, "asked": 0}

    def _on_host(_request: object) -> bool:
        answer["asked"] += 1
        return bool(answer["on_host"])

    _floor_monkeypatch.setattr(token_auth, "_caller_on_host", _on_host)
    return answer


@pytest.fixture(autouse=True)
def _posix(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here is about a POSIX host; the Windows test rebinds this itself."""
    _floor_monkeypatch.setattr(token_auth, "_platform_has_sandbox", lambda: True)


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(token_auth, "sessionless_internal_call_mode", lambda: mode)


def _request(headers: dict[str, str]) -> web.Request:
    req = MagicMock(spec=web.Request)
    req.path = "/api/crons"
    req.method = "GET"
    req.query = {}
    req.cookies = {}
    req.remote = "127.0.0.1"
    req.headers = {"X-Internal-Secret": SECRET, **headers}
    store: dict = {}
    req.__setitem__.side_effect = store.__setitem__
    req.__getitem__.side_effect = store.__getitem__
    req.__contains__.side_effect = store.__contains__
    req.get.side_effect = store.get
    state = MagicMock()
    state._slots = {}
    state.crons._jobs = []
    state.subagents._agents = {}
    req.app = {"state": state}
    return req


@pytest.mark.asyncio
async def _ok(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


@pytest.mark.asyncio
async def _call(headers: dict[str, str]) -> web.StreamResponse:
    mw = token_auth.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    return await mw(_request(headers), _ok)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["log", "enforce"])
async def test_credential_callers_are_logged_and_counted_as_other(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict, mode: str
) -> None:
    from kiro_crew.testing.synthetic_credentials import github_classic

    _mode(monkeypatch, mode)
    clock = _Clock()
    credentials = [github_classic(seed=seed) for seed in (1, 2)]
    for credential in credentials:
        refused = await token_auth.sessionless_internal_call_refused(
            _request({"X-Internal-Caller": credential}),
            "/api/crons",
            capability_granted=False,
            clock=clock,
        )
        assert refused is (mode == "enforce")
    assert host["asked"] == 2
    rows = [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]
    assert [r["caller"] for r in rows] == ["other"] * (2 if mode == "enforce" else 1)
    assert all(credential not in r["caller"] for credential in credentials for r in rows)
    assert token_auth._SESSIONLESS_COUNTS == ({("other", "/api/crons"): 2} if mode == "log" else {})
    assert token_auth._sessionless_log_last == (
        {("other", "/api/crons"): clock.now} if mode == "log" else {}
    )


@pytest.mark.asyncio
async def test_log_mode_admits_and_writes_one_row_per_window(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "log")
    first = await _call({"X-Internal-Caller": "kirocrew-dashboard"})
    second = await _call({"X-Internal-Caller": "kirocrew-dashboard"})
    assert (first.status, second.status) == (200, 200)
    rows = [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]
    assert [(r["outcome"], r["caller"], r["resources"]) for r in rows] == [
        ("would_deny", "kirocrew-dashboard", "/api/crons")
    ]


@pytest.mark.asyncio
async def test_log_mode_looks_up_every_call_but_logs_once_per_window(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "log")
    host["on_host"] = False
    for _ in range(3):
        assert (await _call({"X-Internal-Caller": "kirocrew-dashboard"})).status == 200
    assert host["asked"] == 3
    assert token_auth._SESSIONLESS_COUNTS[("kirocrew-dashboard", "/api/crons")] == 3
    rows = [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]
    assert [r["outcome"] for r in rows] == ["would_deny"]


@pytest.mark.asyncio
async def test_concurrent_log_calls_write_one_row(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list
) -> None:
    _mode(monkeypatch, "log")
    clock = _Clock()
    loop = asyncio.get_running_loop()
    lookups: list[asyncio.Future[bool]] = []

    def _lookup(_executor: Any, _func: Any, _request: Any) -> asyncio.Future[bool]:
        future: asyncio.Future[bool] = loop.create_future()
        lookups.append(future)
        # Both requests must reach the lookup before either lookup completes.
        if len(lookups) == 2:
            for pending in lookups:
                pending.set_result(False)
        return future

    monkeypatch.setattr(loop, "run_in_executor", _lookup)
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                token_auth.sessionless_internal_call_refused(
                    _request({"X-Internal-Caller": "kirocrew-dashboard"}),
                    "/api/crons",
                    capability_granted=False,
                    clock=clock,
                )
                for _ in range(2)
            )
        ),
        timeout=30,
    )
    assert results == [False, False]
    assert len(lookups) == 2
    assert token_auth._SESSIONLESS_COUNTS[("kirocrew-dashboard", "/api/crons")] == 2
    rows = [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]
    assert [(r["outcome"], r["caller"], r["resources"]) for r in rows] == [
        ("would_deny", "kirocrew-dashboard", "/api/crons")
    ]


@pytest.mark.asyncio
async def test_a_host_call_does_not_hide_a_sandboxed_caller(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    # Same (caller, path) over the window, first on the host then not: the host call
    # must not consume the window, so the sandboxed call still writes its row.
    _mode(monkeypatch, "log")
    host["on_host"] = True
    assert (await _call({"X-Internal-Caller": "unnamed"})).status == 200
    host["on_host"] = False
    assert (await _call({"X-Internal-Caller": "unnamed"})).status == 200
    rows = [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]
    assert [r["outcome"] for r in rows] == ["would_deny"]


@pytest.mark.asyncio
async def test_an_on_host_verdict_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    # A host caller is re-checked every call: a pid can be reused by a sandboxed
    # process within the window, so an on-host verdict is never cached.
    _mode(monkeypatch, "log")
    host["on_host"] = True
    for _ in range(3):
        assert (await _call({"X-Internal-Caller": "cli"})).status == 200
    assert host["asked"] == 3


@pytest.mark.asyncio
async def test_enforce_mode_looks_up_every_call(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "enforce")
    host["on_host"] = True
    for _ in range(2):
        assert (await _call({})).status == 200
    assert host["asked"] == 2


@pytest.mark.asyncio
async def test_enforce_mode_refuses_with_session_required(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "enforce")
    for _ in range(2):
        resp = await _call({})
        assert resp.status == 403
        assert "session_required" in resp.text
    assert host["asked"] == 2
    assert not token_auth._SESSIONLESS_COUNTS
    assert not token_auth._sessionless_log_last
    rows = [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]
    assert [r["outcome"] for r in rows] == ["denied", "denied"]


@pytest.mark.asyncio
async def test_a_host_caller_is_admitted_without_a_row(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "enforce")
    host["on_host"] = True
    resp = await _call({})
    assert resp.status == 200
    assert not [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "dashboard:ui"])
async def test_the_ui_surface_key_names_no_session(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict, key: str
) -> None:
    _mode(monkeypatch, "enforce")
    resp = await _call({"X-Session-Key": key})
    assert resp.status == 403
    assert host["asked"] == 1


@pytest.mark.asyncio
async def test_a_call_that_names_its_session_is_not_checked(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "enforce")
    resp = await _call({"X-Session-Key": "dashboard:s1"})
    assert resp.status == 200
    assert host["asked"] == 0


@pytest.mark.asyncio
async def test_a_platform_without_a_sandbox_is_not_checked(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "enforce")
    monkeypatch.setattr(token_auth, "_platform_has_sandbox", lambda: False)
    assert not await token_auth.sessionless_internal_call_refused(
        _request({}), "/api/crons", capability_granted=False
    )
    assert host["asked"] == 0


@pytest.mark.asyncio
async def test_a_capability_grant_is_not_checked(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "enforce")
    assert not await token_auth.sessionless_internal_call_refused(
        _request({}), "/api/crons", capability_granted=True
    )
    assert host["asked"] == 0


@pytest.mark.asyncio
async def test_the_window_reopens_after_it_passes(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "log")
    clock = _Clock()
    for step in (0.0, 1.0, token_auth.LOG_WINDOW_SECS):
        clock.now += step
        await token_auth.sessionless_internal_call_refused(
            _request({}), "/api/crons", capability_granted=False, clock=clock
        )
    rows = [r for r in sel_rows if r["operation"] == "internal_auth.sessionless_caller"]
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_log_counts_and_timestamps_evict_the_same_oldest_pair(
    monkeypatch: pytest.MonkeyPatch, sel_rows: list, host: dict
) -> None:
    _mode(monkeypatch, "log")
    monkeypatch.setattr(token_auth, "_MAX_TRACKED", 2)
    clock = _Clock()
    for caller in ("kirocrew-dashboard", "kirocrew-crew-log", "kirocrew-debug"):
        assert not await token_auth.sessionless_internal_call_refused(
            _request({"X-Internal-Caller": caller}),
            "/api/crons",
            capability_granted=False,
            clock=clock,
        )
    assert token_auth._SESSIONLESS_COUNTS == {
        ("kirocrew-crew-log", "/api/crons"): 1,
        ("kirocrew-debug", "/api/crons"): 1,
    }
    assert token_auth._sessionless_log_last == {
        ("kirocrew-crew-log", "/api/crons"): clock.now,
        ("kirocrew-debug", "/api/crons"): clock.now,
    }


class TestTheMode:
    def test_unset_reads_as_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(live, "snapshot", lambda: None)
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(KiroCrewConfig))
        assert token_auth.sessionless_internal_call_mode() == "log"

    def test_enforce_on_disk_holds_before_the_watcher_is_primed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The boot window: no snapshot yet. The operator's on-disk "enforce" must
        # still apply, not the "log" default.
        from dataclasses import replace

        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        base = KiroCrewConfig()
        on_disk = replace(
            base, dashboard=replace(base.dashboard, sessionless_internal_calls="enforce")
        )
        monkeypatch.setattr(live, "snapshot", lambda: None)
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: on_disk))
        assert token_auth.sessionless_internal_call_mode() == "enforce"

    def test_an_unreadable_config_reads_as_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        def _unreadable() -> KiroCrewConfig:
            raise OSError("config.json unreadable")

        monkeypatch.setattr(live, "snapshot", lambda: None)
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_unreadable))
        assert token_auth.sessionless_internal_call_mode() == "log"

    @pytest.mark.parametrize("value,expected", [("enforce", "enforce"), ("bogus", "log")])
    def test_the_live_value_is_read(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
    ) -> None:
        from types import SimpleNamespace

        from kiro_crew.config import live

        cfg = SimpleNamespace(dashboard=SimpleNamespace(sessionless_internal_calls=value))
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        assert token_auth.sessionless_internal_call_mode() == expected

    def test_the_config_default_is_log(self) -> None:
        from kiro_crew.config.sections_dashboard import DashboardConfig

        assert DashboardConfig().sessionless_internal_calls == "log"
