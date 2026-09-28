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

    @staticmethod
    def _state_replying(text: str):
        """A state whose background session answers *text* to any prompt."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        class FakeEvent:
            def __init__(self, kind, text=""):
                self.kind = kind
                self.text = text

        class FakeClient:
            async def prompt(self, prompt):
                yield FakeEvent(EVENT_TEXT_CHUNK, text)
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

        return FakeState()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            "I cannot access these links.",
            "Sorry, I am unable to open external URLs",
            "Unable to fetch the linked pages without tool access",
        ],
    )
    async def test_a_refusal_is_never_stored_as_a_chip_label(self, reply):
        """The prompt is a list of URLs and the turn is tool-free, so the model
        narrates the denial; without the guard that sentence is a 28-character
        nav chip. The slot stays (empty) and the endpoint pads the rest.
        Mutation: drop the ``looks_like_prose`` branch -- red."""
        result = await _resolve_link_summaries(
            self._state_replying(reply),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result == [""]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("numbered", [True, False])
    async def test_a_per_link_refusal_keeps_its_slot(self, numbered):
        """Labels are merged positionally (label i -> link i). A refusal for
        link 2 must leave an EMPTY slot 2, so label 3 stays on link 3 -- in the
        bare shape the prompt asks for as much as in a numbered one. Mutation:
        ``continue`` without appending -- red (the third label shifts onto the
        second link)."""
        prefix = ("1. ", "2. ", "3. ") if numbered else ("", "", "")
        result = await _resolve_link_summaries(
            self._state_replying(
                f"{prefix[0]}Nav Panel Feature CR\n"
                f"{prefix[1]}I cannot access this link.\n"
                f"{prefix[2]}Memory V2 Design Doc\n"
            ),
            [{"url": "https://a.com"}, {"url": "https://b.com"}, {"url": "https://c.com"}],
        )
        assert result == ["Nav Panel Feature CR", "", "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    async def test_a_leading_refusal_for_the_first_link_keeps_its_slot(self):
        """A bare refusal on line 1 with as many lines as links is link 1's
        slot, not a preamble. Mutation: drop every leading prose line -- red."""
        result = await _resolve_link_summaries(
            self._state_replying("I cannot access this link.\nMemory V2 Design Doc\n"),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result == ["", "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "preamble",
        [
            "Here are the labels for your links:",
            "Labels:",
            "The following labels:",
            "Below are the labels:",
            "\u4ee5\u4e0b\u662f\u6807\u7b7e\uff1a",
        ],
    )
    @pytest.mark.parametrize("numbered", [True, False])
    async def test_a_recognised_preamble_does_not_shift_the_labels(self, preamble, numbered):
        """An unnumbered first line that IS a list-preamble phrase with a
        closing colon is not a slot: keeping it (even as "") would put every
        later label on the wrong link. Mutation: drop the preamble branch --
        red."""
        prefix = ("1. ", "2. ") if numbered else ("", "")
        result = await _resolve_link_summaries(
            self._state_replying(
                f"{preamble}\n"
                f"{prefix[0]}Nav Panel Feature CR\n"
                f"{prefix[1]}Memory V2 Design Doc\n"
            ),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result == ["Nav Panel Feature CR", "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    async def test_a_recognised_preamble_is_dropped_even_when_labels_are_missing(self):
        """A preamble plus fewer labels than links: the preamble is still not a
        slot. Keeping it would put label 1 on link 2; dropping it leaves the
        missing tail for the endpoint's padding. Mutation: require a surplus
        line before dropping -- red."""
        result = await _resolve_link_summaries(
            self._state_replying("Labels:\nMemory V2 Design Doc\nNav Panel Feature CR\n"),
            [{"url": "https://a.com"}, {"url": "https://b.com"}, {"url": "https://c.com"}],
        )
        assert result == ["Memory V2 Design Doc", "Nav Panel Feature CR"]

    @pytest.mark.asyncio
    async def test_a_first_link_refusal_with_a_trailing_courtesy_keeps_its_slot(self):
        """Refusal for link 1, real labels, then a courtesy line: the reply is
        one line longer than the link count, yet the refusal is link 1's slot
        and must stay empty rather than be dropped -- otherwise label 2 lands
        on link 1 and the merge is silently wrong for the session. The
        endpoint truncates the surplus tail. Mutation: drop a leading prose
        line whenever the reply is longer than the link list -- red."""
        result = await _resolve_link_summaries(
            self._state_replying(
                "I cannot access this link.\n"
                "Memory V2 Design Doc\n"
                "Nav Panel Feature CR\n"
                "Let me know if you need anything else.\n"
            ),
            [{"url": "https://a.com"}, {"url": "https://b.com"}, {"url": "https://c.com"}],
        )
        assert result[:3] == ["", "Memory V2 Design Doc", "Nav Panel Feature CR"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", ["Label Printing Bug:", "Following up on the outage:", "Labels and tags cleanup:"]
    )
    async def test_a_label_sharing_a_preamble_word_keeps_its_slot(self, label):
        """A first label that merely STARTS like a preamble ("Label Printing
        Bug:") is a slot, even with a surplus courtesy line: only the complete
        preamble phrase is dropped. Mutation: match on the first word -- red."""
        result = await _resolve_link_summaries(
            self._state_replying(f"{label}\nMemory V2 Design Doc\nHope this helps\n"),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result[:2] == [label, "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trailing", ["", "Hope this helps\n"])
    async def test_a_colon_terminated_first_label_keeps_its_slot(self, trailing):
        """``API Reference Docs:`` as line 1 is link 1's label, not a preamble
        -- whether or not a courtesy line trails the list and makes the reply
        longer than the link count. Only recognised preamble WORDING is ever
        dropped. Mutation: drop on colon + surplus without the wording -- red
        (the second label shifts onto the first link)."""
        result = await _resolve_link_summaries(
            self._state_replying(f"API Reference Docs:\nMemory V2 Design Doc\n{trailing}"),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result[:2] == ["API Reference Docs:", "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    async def test_the_dropped_preamble_is_logged_redacted(self, monkeypatch, caplog):
        """The dropped preamble is raw model output; the log line must see it
        only through the context-aware redactor. Mutation: log ``ln`` directly
        -- red."""
        import logging

        from kiro_crew.dashboard import chat_nav

        seen: list[str] = []

        def fake_redact(text):
            seen.append(text)
            return "<redacted>"

        monkeypatch.setattr(chat_nav, "redact_log_via_context", fake_redact)
        with caplog.at_level(logging.INFO, logger=chat_nav.__name__):
            await _resolve_link_summaries(
                self._state_replying("Here are the labels for your links:\nA doc\nB doc\n"),
                [{"url": "https://a.com"}, {"url": "https://b.com"}],
            )
        assert seen == ["Here are the labels for your links:"]
        assert "Here are the labels" not in caplog.text
        assert "<redacted>" in caplog.text

    @pytest.mark.asyncio
    async def test_a_discarded_prose_line_is_redacted_whole_before_truncation(self, monkeypatch):
        """The redactor must see the FULL line; truncating first can split a
        credential so no pattern matches the surviving fragment. Mutation:
        ``redact(ln[:120])`` -- red (the redactor sees a 120-char cut)."""
        from kiro_crew.dashboard import chat_nav

        seen: list[str] = []
        monkeypatch.setattr(
            chat_nav, "redact_log_via_context", lambda text: seen.append(text) or "<r>"
        )
        long_refusal = "I cannot access this link because " + "x" * 150
        await _resolve_link_summaries(
            self._state_replying(f"{long_refusal}\nA doc\n"),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert seen == [long_refusal]

    @pytest.mark.asyncio
    async def test_a_long_unspaced_script_label_is_still_stored(self):
        """A 3-8 word katakana/Thai label can exceed the title guard's 24
        unspaced characters; this path uses its own ceiling. Mutation: call the
        guard with the title default -- red."""
        label = "\u30bb\u30c3\u30b7\u30e7\u30f3\u30bf\u30a4\u30c8\u30eb\u30b8\u30a7\u30cd\u30ec\u30fc\u30bf\u30fc\u306e\u30ea\u30d5\u30a1\u30af\u30bf\u30ea\u30f3\u30b0"
        assert len(label) == 25
        result = await _resolve_link_summaries(
            self._state_replying(f"{label}\nA doc\n"),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result == [label, "A doc"]

    @pytest.mark.asyncio
    async def test_a_numbered_first_line_is_never_a_preamble(self):
        """``1. Docs:`` is a slot even though it ends in a colon."""
        result = await _resolve_link_summaries(
            self._state_replying("1. Docs:\n2. Memory V2 Design Doc\n"),
            [{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        assert result == ["Docs:", "Memory V2 Design Doc"]

    @pytest.mark.asyncio
    async def test_legitimate_short_labels_still_pass(self):
        """The guard must not eat real labels: identifier dots, the prompt's
        own URL-type fallback, a CJK name and a label that opens with a digit."""
        reply = (
            "1. Node.js upgrade CR\n2. Doc dVbcAXW3\n3. 记忆 V2 设计文档\n4. 2024 Design Roadmap\n"
        )
        result = await _resolve_link_summaries(
            self._state_replying(reply),
            [{"url": f"https://{i}.com"} for i in range(4)],
        )
        assert result == [
            "Node.js upgrade CR",
            "Doc dVbcAXW3",
            "记忆 V2 设计文档",
            "2024 Design Roadmap",
        ]


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
        # 123[:500] would raise TypeError -> 500
        assert _normalize_link({"url": 123, "context": "ctx"}) == {"url": "", "context": "ctx"}

    def test_non_string_context_coerced_to_empty(self):
        # "".strip() on a list would raise AttributeError -> 500
        assert _normalize_link({"url": "https://x.com", "context": ["a"]}) == {
            "url": "https://x.com",
            "context": "",
        }

    def test_null_url_coerced_to_empty(self):
        assert _normalize_link({"url": None}) == {"url": "", "context": ""}

    def test_non_dict_entry_coerced_to_empty(self):
        # link.get(...) on a str/None would raise AttributeError -> 500
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
            # Every shape that would otherwise produce a 500.
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
