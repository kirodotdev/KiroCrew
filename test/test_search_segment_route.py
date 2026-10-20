"""``POST /api/search/segment`` and its mcp-core client: one dictionary per host.

The invariant: the gateway holds the host's only copy of the Chinese dictionary,
every mcp-core process asks it instead of loading one, and the route that shares it
answers ONLY a local process holding the internal secret, on the gateway's existing
listener. Each condition is pinned on its own: loopback (or the gateway's unix
socket) only; the secret required, so a missing, empty or wrong one is refused; the
cookie fall-through refused at the handler; bounded input; and no new listener.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import history_search, history_words, mcp_core
from kiro_crew.dashboard.handlers import search_segment
from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    _STRICT_INTERNAL_API_PATHS,
    _register_mcp_routes,
)
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.mcp_tools import sessions
from kiro_crew.subprocess_utf8 import UTF8_TEXT

PATH = "/api/search/segment"
SECRET = "the-real-secret"


@pytest.fixture()
def split(monkeypatch):
    """Replace the dictionary with a recorder, so a refusal can prove it never ran."""
    calls: list[list[str]] = []

    def fake(runs):
        calls.append(list(runs))
        return [[run[:1], run[1:]] for run in runs]

    monkeypatch.setattr(search_segment, "segment_runs", fake)
    return calls


def _client() -> TestClient:
    """The route behind the REAL middleware and the production path set."""
    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                internal_secret=SECRET,
                local_only=True,
            )
        ]
    )
    app.router.add_post(PATH, search_segment.api_search_segment)
    return TestClient(TestServer(app))


class _Req:
    """The request attributes the handler reads, for the cases no socket can stage."""

    def __init__(self, *, remote: str = "127.0.0.1", internal_auth: bool = True, body: Any = None):
        self.remote = remote
        self.path = PATH
        self.transport = None
        self._data = {"internal_auth": internal_auth} if internal_auth else {}
        self._body = body

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def _payload(response: web.Response) -> dict[str, Any]:
    return json.loads(response.body)


def test_the_path_is_strict_internal_and_not_mixed():
    assert PATH in _STRICT_INTERNAL_API_PATHS
    assert PATH not in _MIXED_INTERNAL_API_PATHS


class TestThroughTheRealMiddleware:
    @pytest.mark.asyncio
    async def test_a_local_caller_with_the_secret_gets_words(self, split):
        async with _client() as client:
            resp = await client.post(
                PATH, json={"runs": ["吉普力"]}, headers={"X-Internal-Secret": SECRET}
            )
            body = await resp.json()

        assert resp.status == 200
        assert body == {"ok": True, "words": [["吉", "普力"]]}
        assert split == [["吉普力"]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [{}, {"X-Internal-Secret": ""}, {"X-Internal-Secret": "guessed-wrong"}],
        ids=["missing", "empty", "wrong"],
    )
    async def test_a_caller_without_the_secret_is_refused(self, split, headers):
        async with _client() as client:
            resp = await client.post(PATH, json={"runs": ["吉普力"]}, headers=headers)

        assert resp.status == 403
        assert split == [], "the dictionary must not run for a refused request"


class TestHandlerGuards:
    @pytest.mark.asyncio
    async def test_a_non_local_caller_is_refused_even_after_the_secret_check(self, split):
        """``local_only=False`` reclassifies strict paths as mixed; the handler still says no."""
        resp = await search_segment.api_search_segment(
            _Req(remote="10.0.0.7", body={"runs": ["吉普力"]})  # type: ignore[arg-type]
        )

        assert resp.status == 403
        assert _payload(resp)["code"] == "loopback_only"
        assert split == []

    @pytest.mark.asyncio
    async def test_a_cookie_caller_is_refused(self, split):
        """With the header absent the middleware falls through to cookie auth."""
        resp = await search_segment.api_search_segment(
            _Req(internal_auth=False, body={"runs": ["吉普力"]})  # type: ignore[arg-type]
        )

        assert resp.status == 403
        assert _payload(resp)["code"] == "internal_secret_required"
        assert split == []

    @pytest.mark.asyncio
    async def test_the_gateway_unix_socket_counts_as_local(self, split, monkeypatch):
        monkeypatch.setattr(search_segment, "request_is_unix_socket", lambda request: True)

        resp = await search_segment.api_search_segment(
            _Req(remote="", body={"runs": ["吉普力"]})  # type: ignore[arg-type]
        )

        assert resp.status == 200
        assert split == [["吉普力"]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            ["吉普力"],
            {},
            {"runs": []},
            {"runs": "吉普力"},
            {"runs": ["吉"]},
            {"runs": ["テスト"]},
            {"runs": ["abc"]},
            {"runs": [7]},
            {"runs": ["内" * (history_search.CJK_SEGMENT_MAX_RUN_CHARS + 1)]},
            {"runs": ["内存"] * (history_search.CJK_SEGMENT_MAX_RUNS + 1)},
        ],
    )
    async def test_a_body_outside_the_bounds_is_refused(self, split, body):
        resp = await search_segment.api_search_segment(_Req(body=body))  # type: ignore[arg-type]

        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_runs"
        assert split == []

    @pytest.mark.asyncio
    async def test_the_largest_valid_body_is_accepted(self, split):
        runs = [
            "内" * history_search.CJK_SEGMENT_MAX_RUN_CHARS
        ] * history_search.CJK_SEGMENT_MAX_RUNS

        resp = await search_segment.api_search_segment(_Req(body={"runs": runs}))  # type: ignore[arg-type]

        assert resp.status == 200
        assert split == [runs]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["unavailable", "raises"])
    async def test_no_dictionary_is_a_503(self, monkeypatch, failure):
        def segment(runs):
            if failure == "raises":
                raise RuntimeError("broken wheel")
            return None

        monkeypatch.setattr(search_segment, "segment_runs", segment)

        resp = await search_segment.api_search_segment(_Req(body={"runs": ["吉普力"]}))  # type: ignore[arg-type]

        assert resp.status == 503
        assert _payload(resp)["code"] == "segmenter_unavailable"


def test_the_route_rides_the_existing_gateway_app_and_opens_no_listener():
    """No new listener: the route is one more POST on the app the gateway serves."""
    app = web.Application()
    _register_mcp_routes(app)

    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource}
    assert ("POST", PATH) in routes
    for module in (search_segment, history_words):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for opener in (
            "TCPSite",
            "UnixSite",
            "AppRunner",
            "start_server",
            "socket.socket",
            "listen(",
        ):
            assert opener not in source, f"{module.__name__} must not open {opener}"


class TestClientAndRegistration:
    def test_the_client_posts_the_runs_and_returns_the_words(self, monkeypatch):
        seen: list[tuple] = []

        def post(path, body=None, *, timeout=30, session_key=None):
            seen.append((path, body, timeout))
            return {"ok": True, "words": [["吉普", "力"]]}

        monkeypatch.setattr(mcp_core, "_post", post)

        assert sessions.gateway_cjk_words(["吉普力"]) == [["吉普", "力"]]
        assert seen == [(PATH, {"runs": ["吉普力"]}, sessions._SEGMENT_TIMEOUT_SECS)]

    @pytest.mark.parametrize(
        "answer",
        [
            {"error": "gateway unreachable", "refused": True},
            {"error": "timed out", "transport_error": True},
            {"ok": False, "code": "segmenter_unavailable"},
            {"ok": True, "words": "吉普 力"},
            ["not", "a", "dict"],
        ],
    )
    def test_any_failure_answers_none(self, monkeypatch, answer):
        monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: answer)

        assert sessions.gateway_cjk_words(["吉普力"]) is None

    def test_the_core_server_registers_the_gateway_client(self, monkeypatch):
        monkeypatch.setattr(mcp_core, "run_mcp_stdio_loop", lambda *a, **k: None)

        mcp_core.run_mcp_core_server()

        assert history_search._cjk_slot.segment is sessions.gateway_cjk_words

    def test_the_gateway_boot_registers_the_in_process_dictionary(self):
        from kiro_crew.slack.gateway import GatewayOrchestrator

        boot = inspect.getsource(GatewayOrchestrator._init_services)
        assert "register_cjk_segmenter(segment_runs)" in boot


def test_one_dictionary_per_host(tmp_path):
    """mcp-core never loads the dictionary; the gateway loads it only on first use.

    A fresh interpreter, because this worker may already hold the dictionary.
    """
    script = textwrap.dedent("""
        import json, sys
        from kiro_crew import history_search, mcp_core
        from kiro_crew.mcp_tools import sessions

        mcp_core._post = lambda *a, **k: {"ok": True, "words": [["修复", "内存", "泄漏"]]}
        history_search.register_cjk_segmenter(sessions.gateway_cjk_words)
        needles, _, _ = history_search.parse_search_query("修复内存泄漏")
        core = {
            "words": [n.text for n in needles if n.word],
            "rjieba_loaded": "rjieba" in sys.modules,
        }
        from kiro_crew import history_words
        imported = "rjieba" in sys.modules
        split = history_words.segment_runs(["修复内存泄漏"])
        print(json.dumps({"core": core, "gateway_import": imported, "split": split,
                          "after": "rjieba" in sys.modules}))
        """)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=60, cwd=tmp_path, **UTF8_TEXT
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])

    assert result["core"] == {"words": ["修复", "内存", "泄漏"], "rjieba_loaded": False}
    assert result["gateway_import"] is False, "importing the module must not load it"
    assert result["split"] == [["修复", "内存", "泄漏"]]
    assert result["after"] is True
