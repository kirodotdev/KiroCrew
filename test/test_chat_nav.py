"""Tests for chat_nav link summary resolution."""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.chat_nav import (
    _build_link_summary_prompt,
    _normalize_link,
    _resolve_link_summaries,
)


class TestBuildLinkSummaryPrompt:
    def test_single_link_no_context(self):
        links = [{"url": "https://git.example.com/reviews/CR-123"}]
        prompt = _build_link_summary_prompt(links)
        assert "1. URL: https://git.example.com/reviews/CR-123" in prompt
        assert "Context:" not in prompt

    def test_single_link_with_context(self):
        links = [{"url": "https://docs.example.com/abc", "context": "Design doc for memory"}]
        prompt = _build_link_summary_prompt(links)
        assert "Context: Design doc for memory" in prompt

    def test_multiple_links(self):
        links = [
            {"url": "https://a.com"},
            {"url": "https://b.com", "context": "ctx"},
        ]
        prompt = _build_link_summary_prompt(links)
        assert "1. URL: https://a.com" in prompt
        assert "2. URL: https://b.com" in prompt

    def test_context_truncated_at_300(self):
        links = [{"url": "https://x.com", "context": "a" * 500}]
        prompt = _build_link_summary_prompt(links)
        # Context should be truncated
        assert "a" * 300 in prompt
        assert "a" * 301 not in prompt

    def test_empty_context_stripped(self):
        links = [{"url": "https://x.com", "context": "   "}]
        prompt = _build_link_summary_prompt(links)
        assert "Context:" not in prompt


class TestResolveLinkSummaries:
    @pytest.mark.asyncio
    async def test_parses_numbered_lines(self, monkeypatch):
        """LLM returns numbered lines like '1. Label Here'."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        class FakeEvent:
            def __init__(self, kind, text=""):
                self.kind = kind
                self.text = text

        class FakeClient:
            async def prompt(self, prompt):
                yield FakeEvent(EVENT_TEXT_CHUNK, "1. Nav Panel Feature CR\n2. Memory V2 Design Doc\n")
                yield FakeEvent(EVENT_COMPLETE)

            async def reject_tool(self, rid):
                pass

            async def destroy(self):
                pass

        class FakeSessions:
            async def get_bg_session(self):
                return FakeClient()

        class FakeState:
            sessions = FakeSessions()

        result = await _resolve_link_summaries(
            FakeState(),
            [{"url": "https://cr.com", "context": ""}, {"url": "https://quip.com", "context": ""}],
        )
        assert result == ["Nav Panel Feature CR", "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    async def test_preserves_labels_starting_with_digits(self, monkeypatch):
        """Labels like '2024 Design Roadmap' should not be corrupted."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        class FakeEvent:
            def __init__(self, kind, text=""):
                self.kind = kind
                self.text = text

        class FakeClient:
            async def prompt(self, prompt):
                yield FakeEvent(EVENT_TEXT_CHUNK, "2024 Design Roadmap\n3-phase rollout plan\n")
                yield FakeEvent(EVENT_COMPLETE)

            async def reject_tool(self, rid):
                pass

            async def destroy(self):
                pass

        class FakeSessions:
            async def get_bg_session(self):
                return FakeClient()

        class FakeState:
            sessions = FakeSessions()

        result = await _resolve_link_summaries(
            FakeState(),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result == ["2024 Design Roadmap", "3-phase rollout plan"]


class TestApiEndpoint:
    @pytest.mark.asyncio
    async def test_invalid_json(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        app = web.Application()
        app["state"] = None
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/nav/resolve-links", data="not json")
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_empty_links(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        app = web.Application()
        app["state"] = None
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/nav/resolve-links", json={"links": []})
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "links_required"
            # A present-but-not-a-list links field refuses at the same site.
            resp = await client.post("/api/chat/nav/resolve-links", json={"links": "x"})
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "links_required"

    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import chat_nav
        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        async def mock_resolve(state, links):
            return ["Summary " + str(i) for i in range(len(links))]

        monkeypatch.setattr(chat_nav, "_resolve_link_summaries", mock_resolve)

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/nav/resolve-links",
                json={"links": [{"url": "https://x.com", "context": "test"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["summaries"] == ["Summary 0"]

    @pytest.mark.asyncio
    async def test_caps_at_20(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import chat_nav
        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        received = []

        async def mock_resolve(state, links):
            received.extend(links)
            return ["s"] * len(links)

        monkeypatch.setattr(chat_nav, "_resolve_link_summaries", mock_resolve)

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            links = [{"url": f"https://{i}.com"} for i in range(25)]
            resp = await client.post("/api/chat/nav/resolve-links", json={"links": links})
            assert resp.status == 200
            assert len(received) == 20


class TestNormalizeLink:
    def test_valid_passthrough(self):
        assert _normalize_link({"url": "https://x.com", "context": "ctx"}) == {
            "url": "https://x.com",
            "context": "ctx",
        }

    def test_missing_fields_default_to_empty(self):
        assert _normalize_link({}) == {"url": "", "context": ""}

    def test_non_string_url_coerced_to_empty(self):
        # 123[:500] used to raise TypeError -> 500
        assert _normalize_link({"url": 123, "context": "ctx"}) == {"url": "", "context": "ctx"}

    def test_non_string_context_coerced_to_empty(self):
        # "".strip() on a list used to raise AttributeError -> 500
        assert _normalize_link({"url": "https://x.com", "context": ["a"]}) == {
            "url": "https://x.com",
            "context": "",
        }

    def test_null_url_coerced_to_empty(self):
        assert _normalize_link({"url": None}) == {"url": "", "context": ""}

    def test_non_dict_entry_coerced_to_empty(self):
        # link.get(...) on a str/None used to raise AttributeError -> 500
        assert _normalize_link("https://x.com") == {"url": "", "context": ""}
        assert _normalize_link(None) == {"url": "", "context": ""}


class TestApiEndpointResilience:
    """Regression: malformed link shapes must fail soft (200), never 500."""

    @pytest.mark.asyncio
    async def test_malformed_links_do_not_500(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import chat_nav
        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        async def mock_resolve(state, links):
            # Exercise the REAL prompt builder to prove normalized links never
            # raise during prompt construction (the original 500 root cause).
            _build_link_summary_prompt(links)
            return [""] * len(links)

        monkeypatch.setattr(chat_nav, "_resolve_link_summaries", mock_resolve)

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            # Every shape that previously produced a 500.
            links = [
                {"url": 123, "context": "x"},
                {"url": "https://ok.com", "context": 99},
                {"url": None},
                "https://not-a-dict.com",
                None,
                {},
            ]
            resp = await client.post("/api/chat/nav/resolve-links", json={"links": links})
            assert resp.status == 200
            data = await resp.json()
            # Index-aligned: one summary per input link.
            assert len(data["summaries"]) == len(links)

    @pytest.mark.asyncio
    async def test_non_dict_body_returns_400(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            # A valid-but-non-dict JSON body would raise on body.get() -> 500.
            for payload in ([1, 2, 3], "x", 42):
                resp = await client.post("/api/chat/nav/resolve-links", json=payload)
                assert resp.status == 400, f"body={payload!r} should be 400"
                body = await resp.json()
                assert body["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_resolver_error_fails_soft_with_audit_event(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import chat_nav
        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        async def mock_resolve(state, links):
            raise RuntimeError("provider boom")

        monkeypatch.setattr(chat_nav, "_resolve_link_summaries", mock_resolve)

        # Capture the diagnostic SEL event instead of writing the real audit log.
        events: list[dict] = []

        class FakeSel:
            def log_tool_invocation(self, **kwargs):
                events.append(kwargs)

        monkeypatch.setattr(chat_nav, "sel", lambda: FakeSel())

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/nav/resolve-links",
                json={"links": [{"url": "https://a.com"}, {"url": "https://b.com"}]},
            )
            # Cosmetic feature must never 5xx on resolver failure.
            assert resp.status == 200
            data = await resp.json()
            assert data["summaries"] == ["", ""]
        # Failure stayed diagnosable after fail-soft hid the 5xx.
        assert len(events) == 1
        assert events[0]["outcome"] == "error"
        assert events[0]["error"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_fail_soft_survives_sel_emit_error(self, monkeypatch):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import chat_nav
        from kiro_crew.dashboard.chat_nav import api_chat_nav_resolve_links

        async def mock_resolve(state, links):
            raise RuntimeError("provider boom")

        monkeypatch.setattr(chat_nav, "_resolve_link_summaries", mock_resolve)

        # The audit emit itself raises -- it must not defeat fail-soft.
        class ExplodingSel:
            def log_tool_invocation(self, **kwargs):
                raise OSError("sel sink down")

        monkeypatch.setattr(chat_nav, "sel", lambda: ExplodingSel())

        app = web.Application()
        app["state"] = object()
        app.router.add_post("/api/chat/nav/resolve-links", api_chat_nav_resolve_links)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/nav/resolve-links", json={"links": [{"url": "https://a.com"}]}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["summaries"] == [""]
