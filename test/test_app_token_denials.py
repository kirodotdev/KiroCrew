"""Repeated app token exchange refusals are counted, not written once each.

One app retrying a stale secret at ``POST /api/apps/<name>/token`` must not rotate the
security event log with one ``app_token_exchange`` ``denied`` row per refusal. These
tests drive the handler and pin the streak rules in ``dashboard/app_token_denials.py``:
the first refusal is a row and a 403, later ones are a 429 with ``Retry-After``, and
one summary row per window keeps every attempt counted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_handlers_core_coverage_helpers import _req

from kiro_crew.dashboard import app_token_denials as denials
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.sel_request_provenance import bind_request_forwarded
from kiro_crew.testing.clock import ManualClock

_APP = "notes"


@pytest.fixture
def recorder(monkeypatch) -> Iterator[MagicMock]:
    rec = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: rec)
    denials._reset()
    yield rec
    denials._reset()


@pytest.fixture
def clock(monkeypatch) -> ManualClock:
    c = ManualClock()
    c.install(monkeypatch, denials, time=True, datetime=True)
    return c


@pytest.fixture
def secret_ok(monkeypatch) -> dict:
    """``validate_app_secret`` accepts only ``state["secret"]``.

    Every app holds the owner's grant except a name that starts with ``stranger``.
    """
    state = {"secret": "right"}
    monkeypatch.setattr(
        "kiro_crew.dashboard.token_auth.validate_app_secret",
        lambda _name, provided: provided == state["secret"],
    )
    monkeypatch.setattr("kiro_crew.dashboard.token_auth.generate_token", lambda *_a, **_k: "tok")
    monkeypatch.setattr(
        "kiro_crew.apps.host_exec_grants.host_exec_granted",
        lambda name: not name.startswith("stranger"),
    )
    return state


async def _exchange(secret: str | None = "wrong", app: str = _APP):
    headers = {} if secret is None else {"X-App-Secret": secret}
    return await core_mod.api_app_token(_req(match_info={"name": app}, headers=headers))


def _rows(rec: MagicMock) -> list[dict]:
    return [c.kwargs for c in rec.log_api_access.call_args_list]


def _summaries(rec: MagicMock) -> list[dict]:
    return [r for r in _rows(rec) if "metadata" in r]


def _native_executable(tmp_path: Path) -> Path:
    """A real native executable, so the restart's interpreter guard passes."""
    import shutil

    from kiro_crew import platform_compat

    source = next((p for p in ("/bin/true", "/usr/bin/true") if Path(p).exists()), None)
    if source is None:  # pragma: no cover - a host without either
        pytest.skip("no native binary available to copy")
    target = tmp_path / "python"
    shutil.copyfile(source, target)
    platform_compat.chmod_safe(target, 0o700)
    return target


@pytest.mark.asyncio
async def test_first_refusal_is_a_row_and_a_403_later_ones_a_counted_429(
    recorder, clock, secret_ok
) -> None:
    first = await _exchange()
    assert first.status == 403
    assert "Retry-After" not in first.headers
    assert _rows(recorder) == [
        {
            "caller": _APP,
            "operation": "app_token_exchange",
            "outcome": "denied",
            "source": "app_auth",
            "error": "invalid secret",
        }
    ]
    body_403 = json.loads(first.body)
    assert body_403["code"] == "invalid_secret"

    clock.advance(1)
    second = await _exchange()
    assert second.status == 429
    assert second.headers["Retry-After"] == str(denials.RETRY_AFTER_SECS)
    body = json.loads(second.body)
    assert body["code"] == "auth_throttled"
    assert body["retryAfter"] == denials.RETRY_AFTER_SECS
    # The 403's reason and its upgrade hint reach the caller unchanged.
    assert body["error"] == body_403["error"] == "invalid secret"
    assert body["hint"] == body_403["hint"]
    assert len(_rows(recorder)) == 1, "a repeat must be counted, not written"


async def _through(request: web.Request, handler) -> web.StreamResponse:
    return await handler(request)


def _audit_layer(chain: str):
    """The real ``sel_audit_middleware`` of *chain*, its barriers passed through."""
    import kiro_crew.dashboard.server as server

    app = web.Application()
    common = {
        "deny_audit_middleware": _through,
        "host_validation_middleware": _through,
        "csrf_middleware": _through,
        "internal_secret": "per-boot",
        "port": 0,
        "local_only": True,
        "tailnet_trust": None,
    }
    if chain == "dashboard":
        server._install_dashboard_middlewares(
            app,
            host_canonical_redirect=_through,
            tailnet_host="",
            configured_host="127.0.0.1",
            dashboard_url="",
            **common,
        )
    else:
        server._install_api_middlewares(app, **common)
    return app, next(mw for mw in app.middlewares if mw.__name__ == "sel_audit_middleware")


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", ["dashboard", "headless"])
async def test_the_request_audit_writes_no_row_for_a_counted_refusal(
    chain, recorder, clock, secret_ok, monkeypatch
) -> None:
    """Counted refusals add no request row; the first refusal and a grant keep theirs.

    Both chains' request audit logs every mutating ``/api/`` request, so an
    unmarked 429 would add one ``POST`` row per attempt.
    """
    import kiro_crew.dashboard.server as server

    monkeypatch.setattr(server, "sel", lambda: recorder)
    app, audit = _audit_layer(chain)

    async def _post(secret: str) -> int:
        request = make_mocked_request(
            "POST",
            f"/api/apps/{_APP}/token",
            headers={"X-App-Secret": secret},
            match_info={"name": _APP},
            app=app,
        )
        return (await audit(request, core_mod.api_app_token)).status

    statuses = [await _post("wrong") for _ in range(4)]
    statuses.append(await _post("right"))
    assert statuses == [403, 429, 429, 429, 200]
    request_rows = [r for r in _rows(recorder) if r["operation"].startswith("POST ")]
    assert [r["outcome"] for r in request_rows] == ["error", "ok"]
    exchange_rows = [r for r in _rows(recorder) if r["operation"] == "app_token_exchange"]
    assert [(r["outcome"], r.get("metadata", {}).get("repeated")) for r in exchange_rows] == [
        ("denied", None),
        ("denied", 3),
        ("granted", None),
    ]


@pytest.mark.asyncio
async def test_each_window_writes_one_summary_with_its_count(recorder, clock, secret_ok) -> None:
    await _exchange()
    for _ in range(4):
        clock.advance(30)
        assert (await _exchange()).status == 429
    assert _summaries(recorder) == []

    # 120 s in; the next refusal 180 s later closes the first window.
    clock.advance(180)
    assert (await _exchange()).status == 429
    [summary] = _summaries(recorder)
    assert summary["caller"] == _APP
    assert summary["operation"] == "app_token_exchange"
    assert summary["outcome"] == "denied"
    assert summary["error"] == "invalid secret"
    assert summary["resources"] == "repeated 4 times"
    meta = summary["metadata"]
    assert meta["repeated"] == 4
    assert meta["first_at"] < meta["last_at"]

    # The refusal that closed the window opened the next one with a count of 1.
    denials.write_pending()
    assert [s["metadata"]["repeated"] for s in _summaries(recorder)] == [4, 1]


@pytest.mark.asyncio
async def test_the_timer_writes_a_window_no_later_refusal_closes(
    recorder, clock, secret_ok
) -> None:
    await _exchange()
    clock.advance(10)
    await _exchange()
    clock.advance(10)
    await _exchange()
    handle = denials._flush_handle
    assert handle is not None, "a counted refusal must arm the window timer"
    assert handle.when() - asyncio.get_running_loop().time() <= denials.WINDOW_SECS

    # Run the callback by hand on the virtual clock instead of waiting for real time.
    handle.cancel()
    clock.advance(denials.WINDOW_SECS)
    denials.flush_due()
    [summary] = _summaries(recorder)
    assert summary["metadata"]["repeated"] == 2
    assert denials._flush_handle is None, "nothing left to count, so no timer"


@pytest.mark.asyncio
async def test_a_quiet_window_ends_the_streak(recorder, clock, secret_ok) -> None:
    await _exchange()
    clock.advance(5)
    await _exchange()
    clock.advance(denials.WINDOW_SECS + 1)
    again = await _exchange()
    assert again.status == 403, "after a quiet window the next refusal is a first one"
    rows = _rows(recorder)
    assert [r.get("metadata", {}).get("repeated") for r in rows] == [None, 1, None]


@pytest.mark.asyncio
async def test_a_grant_writes_pending_counts_first_and_ends_the_streak(
    recorder, clock, secret_ok
) -> None:
    await _exchange()
    clock.advance(1)
    await _exchange()
    clock.advance(1)
    await _exchange()

    granted = await _exchange("right")
    assert granted.status == 200, "a correct secret is granted inside a streak"
    rows = _rows(recorder)
    assert rows[-2]["metadata"]["repeated"] == 2
    assert rows[-1]["outcome"] == "granted"

    clock.advance(1)
    assert (await _exchange()).status == 403


@pytest.mark.asyncio
async def test_streaks_are_per_app_reason_and_forwarded(recorder, clock, secret_ok) -> None:
    assert (await _exchange()).status == 403
    assert (await _exchange(None)).status == 403, "a missing header is its own reason"
    assert (await _exchange(app="other")).status == 403
    with bind_request_forwarded(True):
        assert (await _exchange()).status == 403, "a forwarded request is its own streak"
    assert (await _exchange()).status == 429
    assert (await _exchange(None)).status == 429
    missing = json.loads((await _exchange(None)).body)
    assert missing == {
        "error": "missing X-App-Secret header",
        "code": "auth_throttled",
        "hint": None,
        "retryAfter": denials.RETRY_AFTER_SECS,
    }


@pytest.mark.asyncio
async def test_shutdown_writes_what_is_still_counted(recorder, clock, secret_ok) -> None:
    await _exchange()
    await _exchange()
    await _exchange(app="other")
    await _exchange(app="other")
    await _exchange(app="other")
    await denials.write_pending_at_exit(MagicMock())
    counts = sorted((s["caller"], s["metadata"]["repeated"]) for s in _summaries(recorder))
    assert counts == [(_APP, 1), ("other", 2)]
    assert denials._streaks == {}
    assert denials._flush_handle is None


@pytest.mark.asyncio
async def test_a_refusal_after_the_exit_write_is_a_row_and_a_403(
    recorder, clock, secret_ok
) -> None:
    """The routes serve until the process ends, and the exit drops whatever is counted.

    So after the exit's write nothing is counted: each refusal is written as before.
    """
    await _exchange()
    await _exchange()
    await denials.write_pending_at_exit(MagicMock())
    before = len(_rows(recorder))

    statuses = [(await _exchange()).status for _ in range(3)]

    assert statuses == [403, 403, 403]
    assert len(_rows(recorder)) == before + 3
    assert _summaries(recorder)[-1]["metadata"]["repeated"] == 1
    assert denials._streaks == {}
    assert denials._flush_handle is None


def test_install_writes_on_cleanup_and_on_the_pre_exec_seam() -> None:
    from kiro_crew import pre_exec_stops

    app = web.Application()
    denials.install(app)
    assert denials.write_pending_at_exit in list(app.on_cleanup)
    assert pre_exec_stops._stops == {denials.PRE_EXEC_STOP_NAME: denials.write_pending_at_exit}


@pytest.mark.asyncio
async def test_the_in_place_restart_writes_and_drains_the_counts_before_its_exec(
    recorder, clock, secret_ok, monkeypatch, tmp_path
) -> None:
    """The exec skips ``on_cleanup``: the seam writes the summary, the drain lands it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from kiro_crew.dashboard.handlers import updates

    seen: list[str] = []
    recorder.log_api_access.side_effect = lambda **kw: seen.append(
        "summary" if "metadata" in kw else "row"
    )

    async def _drain(*_a, **_k) -> None:
        # A refusal that arrives after the pre-exec stops, while the restart winds down.
        seen.append(f"late {(await _exchange()).status}")
        seen.append(f"late {(await _exchange()).status}")
        seen.append("drain")

    monkeypatch.setattr("kiro_crew.sel.flush_audit_queue_before_hard_exit", _drain)
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(updates, "reexec_launcher", Mock())
    monkeypatch.setattr(updates, "reexec_python_module", lambda *_a, **_k: seen.append("exec"))
    exe = _native_executable(tmp_path)
    denials.install(web.Application())
    await _exchange()
    await _exchange()
    await _exchange()
    state = SimpleNamespace(
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=AsyncMock()),
        _mcp_gateway_stop=AsyncMock(),
    )

    assert await updates._restart_gateway(state, resolver=lambda: str(exe)) is True

    assert seen == ["row", "summary", "row", "late 403", "row", "late 403", "drain", "exec"]
    [summary] = _summaries(recorder)
    assert summary["metadata"]["repeated"] == 2
    assert denials._streaks == {}, "nothing is left counted for the exec to drop"


@pytest.mark.asyncio
async def test_the_owner_restart_writes_the_counts_before_its_drain_and_exit(
    recorder, clock, secret_ok, monkeypatch
) -> None:
    """``/kirocrew restart`` ends in ``os._exit``, which runs no cleanup hook."""
    from unittest.mock import AsyncMock, patch

    from kiro_crew.slack.events import _handle_restart
    from kiro_crew.slack.handler import set_owner_id

    seen: list[str] = []
    recorder.log_api_access.side_effect = lambda **kw: seen.append(
        "summary" if "metadata" in kw else "row"
    )

    async def _drain(*_a, **_k) -> None:
        seen.append(f"late {(await _exchange()).status}")
        seen.append(f"late {(await _exchange()).status}")
        seen.append("drain")

    await _exchange()
    await _exchange()
    set_owner_id("U_OWNER")
    monkeypatch.setenv("INVOCATION_ID", "abc")
    orch = MagicMock()
    orch.sessions = MagicMock()
    orch.sessions.close_all = AsyncMock()
    orch.dashboard_state = None
    with (
        patch("kiro_crew.slack.events.sel", return_value=MagicMock()),
        patch("kiro_crew.sel.flush_audit_queue_before_hard_exit", _drain),
        patch("os._exit", side_effect=lambda _code: seen.append("exit")),
    ):
        await _handle_restart(orch, "U_OWNER", "", AsyncMock())

    assert seen == ["row", "summary", "row", "late 403", "row", "late 403", "drain", "exit"]
    assert denials._streaks == {}


@pytest.mark.asyncio
async def test_the_table_is_bounded(recorder, clock, secret_ok, monkeypatch) -> None:
    monkeypatch.setattr(denials, "_MAX_STREAKS", 2)
    await _exchange(app="a")
    await _exchange(app="a")
    await _exchange(app="b")
    await _exchange(app="c")
    assert set(k[0] for k in denials._streaks) == {"c"}
    [summary] = _summaries(recorder)
    assert (summary["caller"], summary["metadata"]["repeated"]) == ("a", 1)


@pytest.mark.asyncio
async def test_names_without_a_grant_share_one_streak(recorder, clock, secret_ok) -> None:
    """A caller sending a new name each time writes one first row and one summary."""
    names = [f"stranger-{i}" for i in range(40)]
    statuses = [(await _exchange(app=name)).status for name in names]
    assert statuses == [403] * 40, "a name's first refusal is a 403, granted or not"
    clock.advance(1)
    assert (await _exchange(app=names[0])).status == 429, "its own name decides the 429"
    assert (await _exchange()).status == 403, "a granted app keeps its own streak"

    assert [(r["caller"], r.get("metadata")) for r in _rows(recorder)] == [
        ("stranger-0", None),
        (_APP, None),
    ]
    denials.write_pending()
    [summary] = _summaries(recorder)
    assert summary["caller"] == denials.NOT_ENABLED_KEY
    assert summary["error"] == "invalid secret"
    assert summary["metadata"]["repeated"] == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", ["dashboard", "headless"])
async def test_the_request_audit_writes_no_row_for_a_counted_new_name(
    chain, recorder, clock, secret_ok, monkeypatch
) -> None:
    """A counted 403 for a new name without a grant adds no request row either."""
    import kiro_crew.dashboard.server as server

    monkeypatch.setattr(server, "sel", lambda: recorder)
    app, audit = _audit_layer(chain)
    statuses = []
    for name in ("stranger-a", "stranger-b", "stranger-c"):
        request = make_mocked_request(
            "POST",
            f"/api/apps/{name}/token",
            headers={"X-App-Secret": "wrong"},
            match_info={"name": name},
            app=app,
        )
        statuses.append((await audit(request, core_mod.api_app_token)).status)
    assert statuses == [403, 403, 403]
    request_rows = [r for r in _rows(recorder) if r["operation"].startswith("POST ")]
    assert [r["outcome"] for r in request_rows] == ["error"]


@pytest.mark.asyncio
async def test_new_names_at_the_bound_write_no_pending_count(
    recorder, clock, secret_ok, monkeypatch
) -> None:
    """New names fill the status table only, so a granted app's count stays pending."""
    monkeypatch.setattr(denials, "_MAX_STREAKS", 3)
    await _exchange()
    assert (await _exchange()).status == 429
    for i in range(10):
        await _exchange(app=f"stranger-{i}")
    assert _summaries(recorder) == [], "the bound wrote a summary early"
    assert len(denials._answered) <= 3
    assert set(k[0] for k in denials._streaks) == {_APP, denials.NOT_ENABLED_KEY}
    denials.write_pending()
    counts = sorted((s["caller"], s["metadata"]["repeated"]) for s in _summaries(recorder))
    assert counts == [(denials.NOT_ENABLED_KEY, 9), (_APP, 1)]


@pytest.mark.asyncio
async def test_a_key_keeps_no_more_of_the_name_than_an_app_name_can_hold(
    recorder, clock, secret_ok
) -> None:
    from kiro_crew.apps.manifest_names import APP_NAME_MAX_LENGTH

    long_name = "a" * 8000
    await _exchange(app=long_name)
    assert (await _exchange(app=long_name)).status == 429
    [key] = denials._streaks
    assert key[0] == long_name[:APP_NAME_MAX_LENGTH]


@pytest.mark.asyncio
async def test_a_summary_write_failure_does_not_lose_the_others(recorder, clock, secret_ok) -> None:
    await _exchange(app="a")
    await _exchange(app="a")
    await _exchange(app="b")
    await _exchange(app="b")

    def _fail_for_a(**kw):
        if kw["caller"] == "a":
            raise OSError("disk full")

    recorder.log_api_access.side_effect = _fail_for_a
    denials.write_pending()
    callers = [c.kwargs["caller"] for c in recorder.log_api_access.call_args_list[-2:]]
    assert sorted(callers) == ["a", "b"]


def test_log_api_access_metadata_cannot_set_forwarded(monkeypatch) -> None:
    from kiro_crew.sel import SecurityEventLog

    log = object.__new__(SecurityEventLog)
    seen: list = []
    monkeypatch.setattr(log, "log", lambda event, critical=False: seen.append(event), raising=False)
    log.log_api_access(
        caller="x",
        operation="op",
        outcome="denied",
        forwarded=False,
        metadata={"repeated": 3, "forwarded": True},
    )
    log.log_api_access(caller="x", operation="op", outcome="denied", metadata={"repeated": 1})
    assert seen[0].metadata == {"repeated": 3, "forwarded": False}
    assert seen[1].metadata == {"repeated": 1}
