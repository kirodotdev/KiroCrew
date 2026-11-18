"""opencode: a remote MCP server that asks for a sign-in is listed as awaiting one.

opencode sends no ACP frame when a remote server answers 401, so the chat said
nothing and ``awaiting_auth`` stayed empty. After the session opens Crew asks each
remote element with no ``Authorization`` header, with no credentials, and a
``401`` + ``Bearer`` challenge notes it in the advisory ``sign_in_suspected``
with one notice; it is never called unusable.
The server stays in the array.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.acp import client_mcp_array
from kiro_crew.acp import mcp_sign_in_probe as probe
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import EVENT_MCP_SERVER_INITIALIZED, McpSessionReport
from kiro_crew.acp.types import ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner


def _remote(name: str, url: str = "https://mcp.example.test/mcp", headers=None) -> dict:
    return {"name": name, "type": "http", "url": url, "headers": headers or []}


def test_only_remote_elements_without_headers_are_candidates():
    servers = [
        {"name": "local", "command": "x", "args": [], "env": []},
        _remote("plain"),
        {**_remote("events"), "type": "sse"},
        _remote("keyed", headers=[{"name": "authorization", "value": "Bearer x"}]),
        _remote("blank", headers=[{"name": "Authorization", "value": " "}]),
        _remote("ua", headers=[{"name": "User-Agent", "value": "crew"}]),
    ]

    # Any header of the element's own may be its credential, so only bare ones are asked.
    assert [n for n, _u in probe.sign_in_candidates(servers)] == ["plain", "events"]


def test_the_candidates_are_bounded():
    servers = [_remote(f"s{i}") for i in range(probe.MAX_PROBED_SERVERS + 3)]

    assert len(probe.sign_in_candidates(servers)) == probe.MAX_PROBED_SERVERS


def test_an_element_without_a_name_or_url_is_not_a_candidate():
    servers = [{"type": "http", "url": "https://a.test/mcp"}, {"type": "http", "name": "x"}]

    assert probe.sign_in_candidates(servers) == []
    assert probe.sign_in_candidates("not a list") == []


@pytest.mark.asyncio
async def test_no_candidate_means_no_probe():
    async def _ask(url: str) -> bool:
        raise AssertionError("nothing to ask")

    assert await probe.run_sign_in_probe(_report(), [], ask=_ask) == []


def test_scheduling_without_a_running_loop_does_nothing():
    before = set(probe._PENDING_TASKS)

    probe.schedule_sign_in_probe(_report("p-oauth"), [_remote("p-oauth")])

    assert probe._PENDING_TASKS == before


@pytest.mark.asyncio
async def test_a_scheduled_probe_records_in_the_background(monkeypatch):
    report = _report("p-oauth")

    async def _yes(url: str, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(probe, "run_sign_in_probe", _run_with(_yes))
    before = set(probe._PENDING_TASKS)
    probe.schedule_sign_in_probe(report, [_remote("p-oauth")])
    probe.schedule_sign_in_probe(report, [])  # no candidate: nothing scheduled

    started = probe._PENDING_TASKS - before
    assert len(started) == 1
    await asyncio.gather(*started)
    assert report.payload()["sign_in_suspected"] == ["p-oauth"]


@pytest.mark.asyncio
async def test_a_failing_probe_is_logged_not_raised(monkeypatch, caplog):
    async def _boom(*_a: Any, **_kw: Any) -> list[str]:
        raise RuntimeError("probe broke")

    monkeypatch.setattr(probe, "run_sign_in_probe", _boom)
    before = set(probe._PENDING_TASKS)
    with caplog.at_level("WARNING", logger=probe.logger.name):
        probe.schedule_sign_in_probe(_report("p-oauth"), [_remote("p-oauth")])
        await asyncio.gather(*(probe._PENDING_TASKS - before))

    assert any("sign-in probe failed" in r.getMessage() for r in caplog.records)


def _run_with(ask):
    real = probe.run_sign_in_probe

    async def _run(report, servers, **kw):
        return await real(report, servers, ask=ask, **kw)

    return _run


# ── the request ─────────────────────────────────────────────────────────────


async def _serve(handler) -> tuple[web.AppRunner, str, list[dict[str, str]]]:
    seen: list[dict[str, str]] = []

    async def _wrapped(request: web.Request) -> web.StreamResponse:
        seen.append(dict(request.headers))
        return await handler(request)

    app = web.Application()
    app.router.add_post("/mcp", _wrapped)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}/mcp", seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "challenge", "expected"),
    [
        (401, 'Bearer resource_metadata="https://mcp.example.test/.well-known/x"', True),
        (401, 'Bearer scope="read"', True),
        (401, 'Bearer resource_metadata="http://mcp.example.test/.well-known/x"', False),
        (401, "Bearer", False),
        (401, 'Basic realm="x"', False),
        (401, None, False),
        (403, "Bearer", False),
        (200, None, False),
    ],
)
async def test_only_401_with_a_bearer_challenge_asks_for_a_sign_in(status, challenge, expected):
    async def _handler(_request: web.Request) -> web.Response:
        headers = {"WWW-Authenticate": challenge} if challenge else {}
        return web.json_response({}, status=status, headers=headers)

    runner, url, seen = await _serve(_handler)
    try:
        assert await probe.asks_for_sign_in(url) is expected
    finally:
        await runner.cleanup()
    assert len(seen) == 1
    assert "Authorization" not in seen[0]


@pytest.mark.asyncio
async def test_a_slow_server_reads_as_no_answer():
    release = asyncio.Event()

    async def _handler(_request: web.Request) -> web.Response:
        await release.wait()  # never answers inside the probe's budget
        return web.json_response({}, status=401, headers={"WWW-Authenticate": 'Bearer scope="x"'})

    runner, url, _seen = await _serve(_handler)
    try:
        answer = await probe.asks_for_sign_in(
            url, timeout=0.2  # flake-ok: the budget is under test; the server never answers
        )
        assert answer is False
    finally:
        release.set()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_refused_or_unreachable_url_reads_as_no_answer():
    assert await probe.asks_for_sign_in("http://169.254.169.254/mcp") is False
    assert await probe.asks_for_sign_in("not a url") is False


# ── the report ──────────────────────────────────────────────────────────────


def _report(*names: str) -> McpSessionReport:
    report = McpSessionReport()
    report.begin_session([_remote(n) for n in names])
    return report


@pytest.mark.asyncio
async def test_a_server_asking_for_a_sign_in_is_listed_as_awaiting_one():
    report = _report("p-oauth", "public")
    changed: list[bool] = []
    report.on_change = lambda: changed.append(True)

    servers = [_remote("p-oauth", "https://a.test/mcp"), _remote("public", "https://b.test/mcp")]

    async def _ask_one(url: str) -> bool:
        return url == "https://a.test/mcp"

    recorded = await probe.run_sign_in_probe(report, servers, ask=_ask_one)

    assert recorded == ["p-oauth"]
    payload = report.payload()
    assert payload["sign_in_suspected"] == ["p-oauth"]
    assert payload["awaiting_auth"] == []
    assert payload["failed"] == []
    assert changed


@pytest.mark.asyncio
async def test_an_answer_for_a_replaced_or_closed_session_is_dropped():
    report = _report("p-oauth")

    async def _ask(url: str) -> bool:
        report.begin_session([])  # a new attempt began while the probe ran
        return True

    assert await probe.run_sign_in_probe(report, [_remote("p-oauth")], ask=_ask) == []
    assert report.payload()["sign_in_suspected"] == []

    report = _report("p-oauth")

    async def _yes(url: str) -> bool:
        return True

    assert (
        await probe.run_sign_in_probe(report, [_remote("p-oauth")], alive=lambda: False, ask=_yes)
        == []
    )


def test_a_probed_server_is_not_called_unusable_to_the_turn():
    """The host may hold its sign-in already, so ``not_ready`` claims nothing for it."""
    report = _report("p-oauth")

    report.record_sign_in_asked(["p-oauth"])

    assert report.payload()["sign_in_suspected"] == ["p-oauth"]
    assert report.payload()["awaiting_auth"] == []
    assert report.not_ready() == {}
    assert report.problem_summary() == ""
    report.begin_session([_remote("p-oauth")])
    assert report.payload()["sign_in_suspected"] == []


def test_a_server_that_reported_on_its_own_is_left_alone():
    report = _report("p-oauth")
    report.record_pruned("p-oauth", "its own failure")

    assert report.record_sign_in_asked(["p-oauth"]) == []
    assert report.payload()["sign_in_suspected"] == []


def test_a_later_host_report_supersedes_the_probe():
    report = _report("p-oauth")
    report.record_sign_in_asked(["p-oauth"])

    report.record_event(EVENT_MCP_SERVER_INITIALIZED, "p-oauth")
    assert report.payload()["sign_in_suspected"] == []

    # The buckets can empty again later (a reconnect); the probe's guess must not return.
    report._ready.remove("p-oauth")
    assert report.payload()["sign_in_suspected"] == []


# ── the session start ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("backend", "probed"), [(ACP_BACKEND_OPENCODE, True), (ACP_BACKEND_GOOSE, False)]
)
def test_the_probe_starts_with_the_opencode_session_only(tmp_path, monkeypatch, backend, probed):
    calls: list[Any] = []
    monkeypatch.setattr(
        client_mcp_array,
        "schedule_sign_in_probe",
        lambda report, servers, **kw: calls.append(servers),
    )
    monkeypatch.setattr(client_mcp_array, "schedule_mount_check", lambda *a, **kw: None)
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=backend)
    client._session_id = "s-1"
    client._mcp_mount_servers = [_remote("p-oauth")]

    client._start_mount_check()

    assert calls == ([[_remote("p-oauth")]] if probed else [])


# ── the notice ──────────────────────────────────────────────────────────────


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda _state, _slot, role, text, _cls="", **_kw: seen.append((role, text)),
    )
    return seen


def _slot() -> SimpleNamespace:
    return SimpleNamespace(
        _session_notice_marks=[], _mcp_sign_in_notice_shown=False, messages=[], key="chat-1"
    )


def _provider(backend: str = ACP_BACKEND_OPENCODE) -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend))


def test_an_opencode_chat_gets_one_notice_saying_may(rows):
    slot = _slot()

    chat_runner._notice_mcp_may_need_sign_in(
        SimpleNamespace(), slot, {"sign_in_suspected": ["p-oauth"]}, _provider()
    )
    chat_runner._notice_mcp_may_need_sign_in(
        SimpleNamespace(), slot, {"sign_in_suspected": ["p-oauth", "other"]}, _provider()
    )

    assert len(rows) == 1
    role, text = rows[0]
    assert role == "notice"
    assert "p-oauth" in text
    assert "may need a sign-in" in text
    assert "broken" not in text and "failed" not in text


def test_no_notice_off_opencode_or_with_nothing_waiting(rows):
    chat_runner._notice_mcp_may_need_sign_in(
        SimpleNamespace(), _slot(), {"sign_in_suspected": ["p-oauth"]}, _provider(ACP_BACKEND_GOOSE)
    )
    chat_runner._notice_mcp_may_need_sign_in(
        SimpleNamespace(), _slot(), {"sign_in_suspected": []}, _provider()
    )

    assert rows == []


def test_publishing_the_probed_report_shows_the_notice(rows):
    report = _report("p-oauth")
    report.record_sign_in_asked(["p-oauth"])
    slot = _slot()
    slot.set_mcp_report = lambda payload, session_id: False
    provider = SimpleNamespace(
        mcp_session_report=lambda: report,
        session_id="s1",
        capabilities=capabilities_for(ACP_BACKEND_OPENCODE),
    )

    chat_runner._publish_session_mcp_report(SimpleNamespace(), slot, provider)

    assert [role for role, text in rows if "p-oauth" in text] == ["notice"]
