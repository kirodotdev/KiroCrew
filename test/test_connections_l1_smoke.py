"""Hermetic tests for the Connections L1 authorized-grant smoke harness."""

import asyncio
import builtins
import json
import pathlib
import threading
import time
from copy import deepcopy

import pytest

from kiro_crew.connections import get_provider, l1_smoke, mint
from kiro_crew.mcp_discovery import McpServerInfo

MCP_URL = "https://mcp.example.com/mcp"
SESSION_ID = "sess-abc123"
FIXTURE_TOOL = "list_issues"
CREDENTIAL = "sk-live-abcdefghijkl"
FULL_EXCHANGE = ["initialize", "notifications/initialized", "tools/list"]


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, content_type=None, body=None):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type or "application/json"
        self._payload = payload
        self._body = body

    async def json(self):
        return self._payload

    async def text(self):
        return self._body if self._body is not None else json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    """Serves queued responses in order and records every request made.

    Headers are snapshotted at send time (the harness reuses one dict across the
    exchange, so recording by reference would show the final state everywhere).
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        recorded = dict(kwargs)
        recorded["headers"] = dict(kwargs.get("headers", {}))
        self.calls.append((url, recorded))
        if not self.responses:
            raise AssertionError("the harness made more requests than the test queued")
        return self.responses.pop(0)

    @property
    def methods(self):
        return [call[1]["json"].get("method") for call in self.calls]


@pytest.fixture(autouse=True)
def forbid_real_http_client(monkeypatch):
    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("unit tests must not construct a real HTTP client")

    monkeypatch.setattr(l1_smoke.aiohttp, "ClientSession", fail_if_constructed)


@pytest.fixture
def granted(tmp_path):
    """A cache dir holding the paired grant artifacts for ``MCP_URL``."""
    key = l1_smoke.grant_key(MCP_URL)
    (tmp_path / f"{key}.token.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"{key}.registration.json").write_text("{}", encoding="utf-8")
    return tmp_path


def provider(tool=FIXTURE_TOOL, args=None):
    item = deepcopy(get_provider("linear"))
    assert item is not None
    item["name"] = "Example"
    item["slug"] = "example"
    item["mcp_url"] = MCP_URL
    item["smoke_fixture"] = {"tool": tool, "args": {} if args is None else args}
    return item


def server(headers=None):
    return McpServerInfo(name="example", url=MCP_URL, headers=headers or {})


def credentialed_server():
    return server(headers={"Authorization": f"Bearer {CREDENTIAL}"})


def initialize_ok():
    return FakeResponse(
        200,
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
        headers={"Mcp-Session-Id": SESSION_ID},
    )


def tools_list_ok(tools=(FIXTURE_TOOL, "other_tool")):
    return FakeResponse(
        200,
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": name} for name in tools]}},
    )


def jsonrpc_error(message, request_id=2):
    return FakeResponse(
        200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": message}}
    )


def happy_path():
    return [initialize_ok(), FakeResponse(202, None), tools_list_ok()]


def upto_list(final):
    """The queue for an exchange that reaches ``tools/list`` and gets ``final``."""
    return [initialize_ok(), FakeResponse(202, None), final]


async def run_one(session, item=None, srv=None, cache_dir=None, timeout=5.0):
    return await l1_smoke.smoke_provider(
        session,
        item if item is not None else provider(),
        srv if srv is not None else server(),
        timeout_seconds=timeout,
        cache_dir=cache_dir,
    )


# Grant presence: one implementation, and it never reads token material


def test_grant_helpers_are_mints_and_not_a_local_copy():
    """N7's dedupe, pinned by identity so a future copy cannot creep back in.

    A drifted second copy of kiro-cli's cache key grades a live connection
    SKIPPED; test_connections_mint already pins the formula on this function.
    """
    assert l1_smoke.grant_key is mint.grant_key
    assert l1_smoke.grant_present is mint.grant_present
    assert l1_smoke.kiro_oauth_cache_dir is mint.kiro_oauth_cache_dir


@pytest.fixture
def forbid_token_reads(monkeypatch):
    """Make ANY attempt to open a kiro-cli OAuth artifact an outright failure."""

    def guard(path):
        if str(path).endswith((".token.json", ".registration.json")):
            raise AssertionError(f"token material must never be read: {path}")

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        guard(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    for method in ("open", "read_text", "read_bytes"):
        real = getattr(pathlib.Path, method)

        def guarded(self, *args, _real=real, **kwargs):
            guard(self)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, method, guarded)


def test_grant_detection_never_opens_the_paired_artifacts(granted, forbid_token_reads):
    assert l1_smoke.grant_present(MCP_URL, cache_dir=granted) is True


@pytest.mark.asyncio
async def test_a_full_pass_run_never_opens_the_paired_artifacts(granted, forbid_token_reads):
    result = await run_one(FakeSession(happy_path()), cache_dir=granted)
    assert result["verdict"] == "PASS"


def test_the_read_guard_is_active(granted, forbid_token_reads):
    artifact = granted / f"{l1_smoke.grant_key(MCP_URL)}.token.json"
    with pytest.raises(AssertionError, match="must never be read"):
        artifact.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="must never be read"):
        builtins.open(artifact)


def test_real_http_client_guard_is_active():
    with pytest.raises(AssertionError, match="must not construct a real HTTP client"):
        l1_smoke.aiohttp.ClientSession()


def test_cache_dir_defaults_to_the_kiro_cli_oauth_store(tmp_path):
    assert l1_smoke.kiro_oauth_cache_dir(home=tmp_path) == tmp_path / ".aws" / "sso" / "cache"


@pytest.mark.asyncio
async def test_pass_runs_the_exchange_and_binds_every_timeout(granted):
    session = FakeSession(happy_path())
    result = await run_one(session, cache_dir=granted, timeout=3.5)
    assert result["verdict"] == "PASS"
    assert result["depth"] == "tools_list"
    assert result["installed"] is True
    assert result["grant_present"] is True
    assert result["fixture_tool_advertised"] is True
    assert result["tools_listed"] == 2
    assert result["errors"] == []
    assert session.methods == FULL_EXCHANGE
    assert all(call[1]["timeout"].total == 3.5 for call in session.calls)
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert all(call[0] == MCP_URL for call in session.calls)


@pytest.mark.asyncio
async def test_the_sweep_never_invokes_a_provider_tool(granted):
    """A tools/call from here would skip governed dispatch and its SEL record,
    so the fixture is checked for advertisement only -- even fully credentialed
    with the tool advertised, which is the path that used to invoke it."""
    session = FakeSession(happy_path())
    result = await run_one(session, srv=credentialed_server(), cache_dir=granted)
    assert result["verdict"] == "PASS"
    assert result["fixture_tool_advertised"] is True
    assert "tools/call" not in session.methods


@pytest.mark.asyncio
async def test_pass_echoes_the_session_id_on_every_later_request(granted):
    session = FakeSession(happy_path())
    await run_one(session, cache_dir=granted)
    ids = [call[1]["headers"].get("Mcp-Session-Id") for call in session.calls]
    assert ids == [None] + [SESSION_ID] * 2
    assert session.calls[-1][1]["json"]["method"] == "tools/list"


@pytest.mark.asyncio
async def test_sse_framed_responses_are_parsed_by_the_shared_reader(granted):
    sse = FakeResponse(
        200,
        content_type="text/event-stream",
        body='data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"list_issues"}]}}\n',
    )
    session = FakeSession([initialize_ok(), FakeResponse(202, None), sse])
    result = await run_one(session, cache_dir=granted)
    assert result["verdict"] == "PASS"
    assert result["tools_listed"] == 1


# GRANT_HELD: the expected steady state under runtime token custody


@pytest.mark.asyncio
async def test_a_tokenless_401_is_grant_held_not_needs_reconsent(granted):
    """The regression that matters most: Kiro Crew holds no bearer, so a healthy
    authorized provider answers with 401 -- grading that NEEDS_RECONSENT would
    report every working provider as broken, every run."""
    session = FakeSession([FakeResponse(401, None)])
    result = await run_one(session, cache_dir=granted)
    assert result["verdict"] == "GRANT_HELD"
    assert result["depth"] == "challenge"
    assert result["grant_present"] is True
    assert result["credential_presented"] is False
    assert result["errors"] == ["provider challenged with HTTP 401"]


@pytest.mark.asyncio
async def test_a_tokenless_403_with_a_challenge_is_grant_held(granted):
    session = FakeSession(
        [FakeResponse(403, None, headers={"WWW-Authenticate": 'Bearer resource_metadata="x"'})]
    )
    result = await run_one(session, cache_dir=granted)
    assert result["verdict"] == "GRANT_HELD"


@pytest.mark.asyncio
async def test_a_tokenless_403_without_a_challenge_is_fail(granted):
    """Not attributable to the grant (an edge proxy or geo block reads exactly
    like this), so it must not send a human to re-approve."""
    session = FakeSession([FakeResponse(403, None)])
    result = await run_one(session, cache_dir=granted)
    assert result["verdict"] == "FAIL"
    assert result["errors"] == ["provider returned HTTP 403, expected 200"]


@pytest.mark.asyncio
async def test_a_grant_held_run_stops_before_listing_tools(granted):
    session = FakeSession([FakeResponse(401, None)])
    result = await run_one(session, cache_dir=granted)
    assert session.methods == ["initialize"]
    assert result["tools_listed"] == 0
    assert result["fixture_tool_advertised"] is False


@pytest.mark.asyncio
async def test_a_refused_presented_credential_is_needs_reconsent(granted):
    session = FakeSession([FakeResponse(401, None)])
    result = await run_one(session, srv=credentialed_server(), cache_dir=granted)
    assert result["verdict"] == "NEEDS_RECONSENT"
    assert result["credential_presented"] is True
    assert result["errors"] == ["presented credential refused with HTTP 401"]


@pytest.mark.asyncio
async def test_jsonrpc_invalid_token_error_is_needs_reconsent(granted):
    session = FakeSession(upto_list(jsonrpc_error("invalid_token")))
    result = await run_one(session, cache_dir=granted)
    assert result["verdict"] == "NEEDS_RECONSENT"
    assert result["errors"] == ["invalid_token"]


@pytest.mark.asyncio
async def test_a_mid_exchange_challenge_is_still_grant_held(granted):
    """A server that admits an unauthenticated ``initialize`` and then challenges
    at ``tools/list``: still the custody signal -- alive, and demanding a bearer
    for the actual work. The tool list never arrived, so nothing claims it did."""
    challenge = FakeResponse(403, None, headers={"WWW-Authenticate": "Bearer"})
    result = await run_one(FakeSession(upto_list(challenge)), cache_dir=granted)
    assert result["verdict"] == "GRANT_HELD"
    assert result["fixture_tool_advertised"] is False
    assert result["tools_listed"] == 0
    assert result["depth"] == "challenge"


@pytest.mark.asyncio
async def test_a_missing_fixture_tool_is_fail_and_skips_the_call(granted):
    session = FakeSession([initialize_ok(), FakeResponse(202, None), tools_list_ok(("other",))])
    result = await run_one(session, cache_dir=granted)
    assert result["verdict"] == "FAIL"
    assert result["fixture_tool_advertised"] is False
    assert session.methods == ["initialize", "notifications/initialized", "tools/list"]
    assert f"{FIXTURE_TOOL} is no longer advertised" in result["errors"][0]


@pytest.mark.asyncio
async def test_a_server_error_is_fail_not_a_consent_problem(granted):
    result = await run_one(FakeSession([FakeResponse(503, None)]), cache_dir=granted)
    assert result["verdict"] == "FAIL"
    assert result["errors"] == ["provider returned HTTP 503, expected 200"]


@pytest.mark.asyncio
async def test_a_redirected_endpoint_is_fail(granted):
    """A moved endpoint must not be followed: it would smoke a different server."""
    result = await run_one(FakeSession([FakeResponse(307, None)]), cache_dir=granted)
    assert result["verdict"] == "FAIL"
    assert "HTTP 307" in result["errors"][0]


@pytest.mark.asyncio
async def test_a_transport_failure_is_fail(granted):
    class ExplodingSession(FakeSession):
        def post(self, url, **kwargs):
            raise asyncio.TimeoutError()

    result = await run_one(ExplodingSession([]), cache_dir=granted)
    assert result["verdict"] == "FAIL"
    assert result["errors"] == ["TimeoutError"]


@pytest.mark.asyncio
async def test_a_malformed_tools_list_is_fail(granted):
    session = FakeSession(
        [
            initialize_ok(),
            FakeResponse(202, None),
            FakeResponse(200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": "nope"}}),
        ]
    )
    result = await run_one(session, cache_dir=granted)
    assert result["verdict"] == "FAIL"
    assert result["errors"] == ["tools/list did not return a list"]


@pytest.mark.asyncio
async def test_a_configured_credential_is_redacted_out_of_every_error(granted):
    session = FakeSession(upto_list(jsonrpc_error(f"rejected {CREDENTIAL}")))
    result = await run_one(session, srv=credentialed_server(), cache_dir=granted)
    assert result["verdict"] == "FAIL"
    assert CREDENTIAL not in result["errors"][0]


@pytest.mark.asyncio
async def test_a_credential_straddling_the_truncation_boundary_is_still_redacted(granted):
    """Redact THEN truncate: bisecting first blinds the configured-value scrubber."""

    # A configured value with no generic SHAPE, placed so truncate-first bisects it.
    secret = "opaque-configured-value-0123456789"
    half = len(secret) // 2
    padding = "x" * (l1_smoke._MAX_ERROR_CHARS - len("rejected ") - half)
    session = FakeSession(upto_list(jsonrpc_error(f"rejected {padding}{secret}")))
    result = await run_one(
        session,
        srv=server(headers={"Authorization": f"Bearer {secret}"}),
        cache_dir=granted,
    )
    assert result["verdict"] == "FAIL"
    assert secret not in result["errors"][0]
    assert secret[:half] not in result["errors"][0]


@pytest.mark.asyncio
async def test_the_grant_stat_runs_off_the_loop_and_a_stall_answers_in_bound(granted, monkeypatch):
    """grant_present's contract: worker thread; a stall answers within its bound."""
    seen = []

    def stalls(mcp_url, *, cache_dir=None):
        seen.append(threading.current_thread().name)
        time.sleep(0.3)
        return True

    monkeypatch.setattr(l1_smoke, "grant_present", stalls)
    result = await l1_smoke.smoke_provider(
        FakeSession([]), provider(), server(), timeout_seconds=0.05, cache_dir=granted
    )
    assert seen and all(name != "MainThread" for name in seen)
    assert result["verdict"] == "FAIL"
    assert result["grant_present"] is False
    assert "stalled mount" in result["errors"][0]


@pytest.mark.asyncio
async def test_no_grant_is_skipped_without_touching_the_network(tmp_path):
    session = FakeSession([])
    result = await run_one(session, cache_dir=tmp_path)
    assert result["verdict"] == "SKIPPED"
    assert result["installed"] is True
    assert result["grant_present"] is False
    assert result["errors"] == ["no persisted grant on this host; L1 does not initiate consent"]
    assert session.calls == []


@pytest.mark.asyncio
async def test_an_unconfigured_provider_is_skipped(granted):
    session = FakeSession([])
    result = await l1_smoke.smoke_provider(
        session, provider(), None, timeout_seconds=1, cache_dir=granted
    )
    assert result["verdict"] == "SKIPPED"
    assert result["installed"] is False
    assert result["grant_present"] is True
    assert session.calls == []


@pytest.mark.asyncio
async def test_every_result_carries_the_full_record(granted):
    """No consumer of the report ever sees a partial row, whatever the verdict."""
    expected = set(l1_smoke.SmokeResult.__annotations__)
    skipped = await run_one(FakeSession([]), cache_dir=pathlib.Path("/nonexistent"))
    passed = await run_one(FakeSession(happy_path()), cache_dir=granted)
    assert set(skipped) == expected
    assert set(passed) == expected


# Aggregation


def result(slug, verdict):
    return {
        "slug": slug,
        "name": slug.title(),
        "verdict": verdict,
        "depth": "tools_list" if verdict == "PASS" else "none",
        "installed": True,
        "grant_present": verdict != "SKIPPED",
        "credential_presented": False,
        "fixture_tool": FIXTURE_TOOL,
        "fixture_tool_advertised": verdict == "PASS",
        "tools_listed": 1 if verdict == "PASS" else 0,
        "errors": [],
        "duration_ms": 1,
    }


@pytest.mark.parametrize("verdict", ["PASS", "GRANT_HELD", "SKIPPED"])
def test_healthy_verdicts_keep_the_run_green(verdict):
    report = l1_smoke.build_report([result("a", "PASS"), result("b", verdict)])
    assert report["ok"] is True
    assert report["provider_count"] == 2


@pytest.mark.parametrize("verdict", ["NEEDS_RECONSENT", "FAIL"])
def test_reconsent_and_failure_both_fail_the_run(verdict):
    assert l1_smoke.build_report([result("a", "PASS"), result("b", verdict)])["ok"] is False


def test_the_report_counts_every_verdict_separately():
    report = l1_smoke.build_report(
        [result(v.lower(), v) for v in l1_smoke._VERDICTS]  # noqa: SLF001 -- test pin
    )
    assert report["pass_count"] == 1
    assert report["grant_held_count"] == 1
    assert report["needs_reconsent_count"] == 1
    assert report["failed_count"] == 1
    assert report["skipped_count"] == 1
    assert report["exercised_count"] == 4
    assert report["schema_version"] == 1


def test_an_all_skipped_run_is_ok_when_nothing_was_required():
    report = l1_smoke.build_report([result("a", "SKIPPED")])
    assert report["ok"] is True
    assert report["vacuous"] is False


def test_an_all_skipped_run_is_vacuous_when_a_minimum_was_required():
    """The green-that-proves-nothing guard: an unseeded box must not report ok."""
    report = l1_smoke.build_report([result("a", "SKIPPED")], min_exercised=1)
    assert report["vacuous"] is True
    assert report["ok"] is False
    assert report["exercised_count"] == 0
    assert report["min_exercised"] == 1


def test_a_challenge_counts_as_exercised_against_the_minimum():
    """GRANT_HELD is real evidence, so it satisfies the minimum on its own."""
    report = l1_smoke.build_report([result("a", "GRANT_HELD")], min_exercised=1)
    assert report["vacuous"] is False
    assert report["ok"] is True


@pytest.mark.asyncio
async def test_smoke_all_caps_provider_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def fake_smoke(_session, item, _server, *, timeout_seconds, cache_dir=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return result(item["slug"], "PASS")

    monkeypatch.setattr(l1_smoke, "smoke_provider", fake_smoke)
    results = await l1_smoke.smoke_all(
        object(), [provider() for _ in range(6)], {}, concurrency=2, timeout_seconds=1
    )
    assert len(results) == 6
    assert peak == 2


@pytest.mark.asyncio
async def test_a_provider_that_outlasts_its_total_budget_is_fail(monkeypatch):
    async def never_returns(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(l1_smoke, "smoke_provider", never_returns)
    results = await l1_smoke.smoke_all(
        object(), [provider()], {}, concurrency=1, timeout_seconds=0.01
    )
    assert results[0]["verdict"] == "FAIL"
    # The budget can only expire inside the exchange, which the grant gates.
    assert results[0]["grant_present"] is True
    assert results[0]["errors"] == ["provider smoke run exceeded its total timeout"]


@pytest.mark.asyncio
async def test_one_providers_stray_defect_never_erases_the_other_rows(monkeypatch):
    """WHICH provider regressed is the report's whole point."""

    async def uneven(_session, item, _server, *, timeout_seconds, cache_dir=None):
        if item["slug"] == "a":
            raise TypeError("headers must be a mapping")
        return result(item["slug"], "PASS")

    monkeypatch.setattr(l1_smoke, "smoke_provider", uneven)
    first, second = provider(), provider()
    first["slug"], second["slug"] = "a", "b"
    results = await l1_smoke.smoke_all(
        object(), [first, second], {}, concurrency=2, timeout_seconds=1
    )
    assert [row["verdict"] for row in results] == ["FAIL", "PASS"]
    assert results[0]["errors"] == ["headers must be a mapping"]


@pytest.mark.asyncio
async def test_an_entry_under_a_real_slug_but_wrong_endpoint_is_never_smoked(granted):
    """tool_aliases' rule: identity is proven by endpoint, not the key."""
    imposter = McpServerInfo(name="example", url="https://elsewhere.example.net/mcp")
    session = FakeSession([])
    result = await run_one(session, srv=imposter, cache_dir=granted)
    assert result["verdict"] == "SKIPPED"
    assert session.calls == []
    assert "does not match the registry" in result["errors"][0]


@pytest.mark.asyncio
async def test_a_stalled_inventory_read_fails_within_the_bound(monkeypatch):
    def stalls():
        time.sleep(0.3)
        return {}

    monkeypatch.setattr(l1_smoke, "configured_remote_servers", stalls)
    with pytest.raises(RuntimeError, match="inventory read timed out"):
        await l1_smoke.run_l1(concurrency=1, timeout_seconds=0.05)


def test_the_report_is_written_before_the_loop_joins_leaked_workers(tmp_path, monkeypatch):
    """A stat thread stalled past its bound must not hold the report hostage."""
    release = threading.Event()
    report_path = tmp_path / "r.json"

    async def leaky_run_l1(**_kwargs):
        # A worker the loop cannot cancel (the stalled stat), still blocked at shutdown.
        asyncio.get_running_loop().run_in_executor(None, release.wait)
        return l1_smoke.build_report([result("a", "PASS")])

    monkeypatch.setattr(l1_smoke, "run_l1", leaky_run_l1)
    worker = threading.Thread(
        target=l1_smoke.main, args=(["--report", str(report_path)],), daemon=True
    )
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not report_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        # main is still blocked joining the worker, yet the report is on disk.
        assert worker.is_alive() and report_path.exists()
    finally:
        release.set()
        worker.join(timeout=10)


def test_only_remote_servers_are_considered_installed(monkeypatch):
    monkeypatch.setattr(
        l1_smoke,
        "list_servers",
        lambda: [
            McpServerInfo(name="example", url=MCP_URL),
            McpServerInfo(name="local-stdio", command="npx"),
            # The consent gate probe_server enforces: switched-off means never smoked.
            McpServerInfo(name="switched-off", url=MCP_URL, disabled=True),
        ],
    )
    assert list(l1_smoke.configured_remote_servers()) == ["example"]


def stub_run(monkeypatch, report):
    async def fake_run_l1(**_kwargs):
        return report

    monkeypatch.setattr(l1_smoke, "run_l1", fake_run_l1)


def test_main_writes_the_machine_report_and_returns_nonzero(tmp_path, monkeypatch):
    report = l1_smoke.build_report([result("example", "FAIL")])
    stub_run(monkeypatch, report)
    report_path = tmp_path / "report.json"
    assert l1_smoke.main(["--report", str(report_path)]) == 1
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_main_returns_zero_when_every_grant_is_still_healthy(tmp_path, monkeypatch):
    stub_run(monkeypatch, l1_smoke.build_report([result("a", "GRANT_HELD"), result("b", "PASS")]))
    assert l1_smoke.main(["--report", str(tmp_path / "report.json")]) == 0


def test_main_reports_a_vacuous_run_and_names_the_consent_step(tmp_path, monkeypatch, capsys):
    stub_run(monkeypatch, l1_smoke.build_report([result("a", "SKIPPED")], min_exercised=1))
    assert l1_smoke.main(["--report", str(tmp_path / "report.json")]) == 1
    assert "VACUOUS" in capsys.readouterr().out


def test_main_passes_the_minimum_through_to_the_sweep(tmp_path, monkeypatch):
    seen = {}

    async def fake_run_l1(**kwargs):
        seen.update(kwargs)
        return l1_smoke.build_report([])

    monkeypatch.setattr(l1_smoke, "run_l1", fake_run_l1)
    l1_smoke.main(["--report", str(tmp_path / "r.json"), "--min-exercised", "2"])
    assert seen["min_exercised"] == 2


def test_main_reports_a_fatal_error_instead_of_raising(tmp_path, monkeypatch):
    async def exploding_run_l1(**_kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(l1_smoke, "run_l1", exploding_run_l1)
    report_path = tmp_path / "report.json"
    assert l1_smoke.main(["--report", str(report_path)]) == 1
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["ok"] is False
    assert written["fatal_error"] == "RuntimeError: registry unreadable"
    assert written["providers"] == []


@pytest.mark.parametrize("flag", ["--concurrency", "--timeout"])
def test_nonpositive_tuning_is_rejected(flag):
    with pytest.raises(SystemExit):
        l1_smoke.main([flag, "0"])


def test_a_negative_minimum_is_rejected():
    with pytest.raises(SystemExit):
        l1_smoke.main(["--min-exercised", "-1"])
