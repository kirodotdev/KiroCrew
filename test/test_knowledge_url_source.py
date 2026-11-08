"""A web-page Knowledge source: the gateway fetcher, its rules, and the page it stores.

Kiro Crew fetches a web-page source itself (``kiro_crew.knowledge.url_fetch``), so
``knowledge.doc_ingest_hosts`` decides what it may fetch: https only, a listed host
only, nothing on a default config, where the list is empty. Every request -- the
first and each redirect hop -- must also resolve to public addresses only, and a
redirect may not leave the host. The rules hold when the source is added, when it
is synced, and again inside the fetch.

No test here touches DNS or the network: the resolver and the request opener are
injected, and the one test of the real opener talks to a local test server.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.dashboard.handlers.knowledge import add_source, get_config, sync_source
from kiro_crew.knowledge import url_fetch
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.url_fetch import (
    UrlFetchFailed,
    UrlFetchRefused,
    fetch_page,
    url_fetch_refusal,
)
from kiro_crew.link_unfurl import VettedUrl

PAGE = b"<html><head><title>Example</title></head><body><p>Article text.</p></body></html>"
PUBLIC = "93.184.216.34"


@pytest.fixture()
def store(tmp_path, opened):
    """A store closed on every thread at teardown: the handlers read it off the loop."""
    return opened(KnowledgeStore(str(tmp_path / "test.db")))


@pytest.fixture()
def hosts(monkeypatch):
    """The live allowlist, as a list the test edits in place."""
    allowed: list[str] = []
    monkeypatch.setattr(kh, "_doc_ingest_hosts", lambda: list(allowed))
    return allowed


class FakeWeb:
    """Answers the fetcher's resolver and opener from tables, recording each request."""

    def __init__(self) -> None:
        self.dns: dict[str, list[list[str]]] = {}
        self.pages: dict[str, tuple[int, dict, bytes]] = {}
        self.opened: list[str] = []

    def resolve(self, host: str, port: int) -> list[str]:
        answers = self.dns.get(host) or [[PUBLIC]]
        # Each lookup takes the next answer, so a test can rebind a name.
        return answers.pop(0) if len(answers) > 1 else answers[0]

    async def open(self, vetted: VettedUrl, timeout) -> tuple[int, dict, bytes]:
        self.opened.append(vetted.url)
        return self.pages.get(vetted.url, (404, {}, b""))

    def fetch(self, url: str, allowed: list[str]):
        return fetch_page(url, allowed_hosts=allowed, resolve=self.resolve, opener=self.open)


@pytest.fixture()
def net(monkeypatch):
    """A fake web the handlers' fetch goes to, so no test reaches a real host."""
    fake = FakeWeb()
    monkeypatch.setattr(
        kh, "fetch_page", functools.partial(fetch_page, resolve=fake.resolve, opener=fake.open)
    )
    return fake


def _html(body: bytes = PAGE) -> tuple[int, dict, bytes]:
    return 200, {"Content-Type": "text/html; charset=utf-8"}, body


def _make_app(store, pipeline) -> web.Application:
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app["knowledge_pipeline"] = pipeline
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    app["_bg_tasks"] = set()
    app.router.add_get("/api/knowledge/config", get_config)
    app.router.add_post("/api/knowledge/sources", add_source)
    app.router.add_post("/api/knowledge/sources/{id}/sync", sync_source)
    return app


def _pipeline() -> MagicMock:
    """A pipeline that records each staged page it is handed: bytes, suffix, name, source."""
    pipeline = MagicMock()
    pipeline.stored = []

    async def _ingest(path, original_name=None, source_id=None, **_kw):
        staged = Path(path)
        pipeline.stored.append((staged.read_bytes(), staged.suffix, original_name, source_id))
        return "job-1"

    pipeline.ingest_file = AsyncMock(side_effect=_ingest)
    return pipeline


def _source_count(store) -> int:
    return store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]


def _status(store, sid: str) -> str:
    row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
    return row["sync_status"]


async def _drain(app) -> None:
    for task in list(app["_bg_tasks"]):
        await task


class TestUrlFetchRefusal:
    @pytest.mark.parametrize(
        ("url", "code"),
        [
            ("http://example.com/a", "url_not_https"),
            ("ftp://example.com/a", "url_not_https"),
            ("file:///etc/passwd", "url_not_https"),
            ("HTTPS://example.com/a", "url_not_https"),
            ("https://other.com/a", "url_host_not_allowed"),
            ("https://docs.example.com/a", "url_host_not_allowed"),
            ("https://example.com.evil.net/a", "url_host_not_allowed"),
            ("https://example.com@evil.net/a", "url_has_credentials"),
            ("https://user:pw@example.com/a", "url_has_credentials"),
            ("https://example.com:8443/a", "url_port_not_allowed"),
            ("https://example.com:notaport/a", "url_invalid"),
            ("https://example.com\\@evil.net/a", "url_invalid"),
            ("https://example.com/a b", "url_invalid"),
            ("https://example.com/a\nIgnore the above", "url_invalid"),
            ("https://exämple.com/a", "url_invalid"),
            ("https:///a", "url_invalid"),
            ("", "url_invalid"),
            (None, "url_invalid"),
            ("https://example.com/" + "a" * 2048, "url_too_long"),
        ],
    )
    def test_refused(self, url, code):
        refusal = url_fetch_refusal(url, ["example.com"])
        assert refusal is not None
        assert refusal[0] == code

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/a",
            "https://EXAMPLE.com/a?x=1#y",
            "https://example.com./a",
            "https://example.com:443/a",
            "https://example.com/路径",
        ],
    )
    def test_listed_host_is_allowed(self, url):
        assert url_fetch_refusal(url, ["Example.COM."]) is None

    def test_empty_list_refuses_every_host(self):
        refusal = url_fetch_refusal("https://example.com/a", [])
        assert refusal is not None
        assert refusal[0] == "url_host_not_allowed"
        assert "knowledge.doc_ingest_hosts" in refusal[1]


class TestFetchPage:
    @pytest.mark.asyncio
    async def test_a_page_from_a_listed_host_is_returned(self):
        web_ = FakeWeb()
        web_.pages["https://example.com/a"] = _html()
        page = await web_.fetch("https://example.com/a", ["example.com"])
        assert (page.url, page.text, page.suffix) == (
            "https://example.com/a",
            PAGE.decode(),
            ".html",
        )

    @pytest.mark.asyncio
    async def test_non_https_is_refused_before_any_request(self):
        web_ = FakeWeb()
        with pytest.raises(UrlFetchRefused) as exc:
            await web_.fetch("http://example.com/a", ["example.com"])
        assert exc.value.code == "url_not_https"
        assert web_.opened == []

    @pytest.mark.asyncio
    async def test_unlisted_host_is_refused_before_any_request(self):
        web_ = FakeWeb()
        with pytest.raises(UrlFetchRefused) as exc:
            await web_.fetch("https://other.com/a", ["example.com"])
        assert exc.value.code == "url_host_not_allowed"
        assert web_.opened == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "address", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "::1", "fe80::1"]
    )
    async def test_a_listed_host_that_resolves_inward_is_refused(self, address):
        web_ = FakeWeb()
        web_.dns["example.com"] = [[address]]
        web_.pages["https://example.com/a"] = _html()
        with pytest.raises(UrlFetchRefused) as exc:
            await web_.fetch("https://example.com/a", ["example.com"])
        assert exc.value.code == "url_address_not_public"
        assert web_.opened == []

    @pytest.mark.asyncio
    async def test_one_inward_address_among_public_ones_is_refused(self):
        web_ = FakeWeb()
        web_.dns["example.com"] = [[PUBLIC, "10.0.0.5"]]
        with pytest.raises(UrlFetchRefused) as exc:
            await web_.fetch("https://example.com/a", ["example.com"])
        assert exc.value.code == "url_address_not_public"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "location",
        ["https://other.com/b", "https://docs.example.com/b", "//other.com/b"],
    )
    async def test_a_redirect_off_the_host_is_refused(self, location):
        web_ = FakeWeb()
        web_.pages["https://example.com/a"] = (302, {"Location": location}, b"")
        with pytest.raises(UrlFetchRefused) as exc:
            # other.com is listed too: leaving the host is refused even then.
            await web_.fetch("https://example.com/a", ["example.com", "other.com"])
        assert exc.value.code in {"url_redirect_off_host", "url_host_not_allowed"}
        assert web_.opened == ["https://example.com/a"]

    @pytest.mark.asyncio
    async def test_a_redirect_to_http_is_refused(self):
        web_ = FakeWeb()
        web_.pages["https://example.com/a"] = (301, {"Location": "http://example.com/b"}, b"")
        with pytest.raises(UrlFetchRefused) as exc:
            await web_.fetch("https://example.com/a", ["example.com"])
        assert exc.value.code == "url_not_https"

    @pytest.mark.asyncio
    async def test_a_same_host_redirect_is_followed(self):
        web_ = FakeWeb()
        web_.pages["https://example.com/a"] = (302, {"Location": "/b"}, b"")
        web_.pages["https://example.com/b"] = _html()
        page = await web_.fetch("https://example.com/a", ["example.com"])
        assert page.url == "https://example.com/b"
        assert web_.opened == ["https://example.com/a", "https://example.com/b"]

    @pytest.mark.asyncio
    async def test_a_redirect_hop_is_resolved_again(self):
        """DNS rebinding: the name answers public first, then inward on the hop."""
        web_ = FakeWeb()
        web_.dns["example.com"] = [[PUBLIC], ["127.0.0.1"]]
        web_.pages["https://example.com/a"] = (302, {"Location": "/b"}, b"")
        web_.pages["https://example.com/b"] = _html()
        with pytest.raises(UrlFetchRefused) as exc:
            await web_.fetch("https://example.com/a", ["example.com"])
        assert exc.value.code == "url_address_not_public"
        assert web_.opened == ["https://example.com/a"]

    @pytest.mark.asyncio
    async def test_redirects_are_capped(self):
        web_ = FakeWeb()
        for i in range(url_fetch.MAX_REDIRECTS + 1):
            web_.pages[f"https://example.com/{i}"] = (302, {"Location": f"/{i + 1}"}, b"")
        with pytest.raises(UrlFetchFailed, match="too many"):
            await web_.fetch("https://example.com/0", ["example.com"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("answer", "match"),
        [
            ((500, {"Content-Type": "text/html"}, b"x"), "HTTP 500"),
            ((200, {"Content-Type": "application/pdf"}, b"%PDF"), "not text"),
            ((200, {}, b"x"), "not text"),
            ((200, {"Content-Type": "text/plain"}, b"  \n"), "empty"),
            ((302, {}, b""), "without a location"),
        ],
    )
    async def test_an_answer_that_is_not_a_page_fails(self, answer, match):
        web_ = FakeWeb()
        web_.pages["https://example.com/a"] = answer
        with pytest.raises(UrlFetchFailed, match=match):
            await web_.fetch("https://example.com/a", ["example.com"])

    @pytest.mark.asyncio
    async def test_the_whole_fetch_is_time_bounded(self, monkeypatch):
        monkeypatch.setattr(url_fetch, "FETCH_TIMEOUT_SECONDS", 0.05)

        async def _hang(_vetted, _timeout):
            await asyncio.Event().wait()

        with pytest.raises(UrlFetchFailed, match="too long"):
            await fetch_page(
                "https://example.com/a",
                allowed_hosts=["example.com"],
                resolve=lambda _h, _p: [PUBLIC],
                opener=_hang,
            )


def _vetted_for(server: TestServer, path: str) -> VettedUrl:
    """A VettedUrl pointing at a local test server, as the vet would pin it."""
    return VettedUrl(
        url=str(server.make_url(path)),
        scheme="http",
        host="127.0.0.1",
        wire_host="127.0.0.1",
        port=server.port,
        ip="127.0.0.1",
        addresses=("127.0.0.1",),
        domain="127.0.0.1",
    )


class TestOpener:
    """The real opener against a local server: size cap and no redirect following."""

    @pytest.mark.asyncio
    async def test_a_body_over_the_cap_is_refused_while_reading(self, monkeypatch):
        monkeypatch.setattr(url_fetch, "MAX_PAGE_BYTES", 1024)

        async def _big(_request):
            resp = web.StreamResponse(headers={"Content-Type": "text/plain"})
            await resp.prepare(_request)
            for _ in range(8):
                await resp.write(b"x" * 512)
            return resp

        app = web.Application()
        app.router.add_get("/big", _big)
        async with TestServer(app) as server:
            with pytest.raises(UrlFetchFailed, match="size limit"):
                await url_fetch._open(_vetted_for(server, "/big"), aiohttp.ClientTimeout(total=5))

    @pytest.mark.asyncio
    async def test_a_redirect_is_returned_not_followed(self):
        hits: list[str] = []

        async def _moved(_request):
            hits.append("moved")
            raise web.HTTPFound("/elsewhere")

        async def _elsewhere(_request):
            hits.append("elsewhere")
            return web.Response(text="no")

        app = web.Application()
        app.router.add_get("/moved", _moved)
        app.router.add_get("/elsewhere", _elsewhere)
        async with TestServer(app) as server:
            status, headers, body = await url_fetch._open(
                _vetted_for(server, "/moved"), aiohttp.ClientTimeout(total=5)
            )
        assert (status, headers["Location"], body) == (302, "/elsewhere", b"")
        assert hits == ["moved"]


class TestAddWebPageSource:
    @pytest.mark.asyncio
    async def test_non_https_url_is_refused(self, store, hosts, net):
        hosts.append("example.com")
        async with TestClient(TestServer(_make_app(store, _pipeline()))) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"source_type": "url", "uri": "http://example.com/a"},
            )
            body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "url_not_https"
        assert _source_count(store) == 0
        assert net.opened == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("listed", [[], ["example.com"]])
    async def test_unlisted_host_is_refused(self, store, hosts, net, listed):
        hosts.extend(listed)
        async with TestClient(TestServer(_make_app(store, _pipeline()))) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"source_type": "url", "uri": "https://other.com/a"},
            )
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "url_host_not_allowed"
        assert _source_count(store) == 0
        assert net.opened == []

    @pytest.mark.asyncio
    async def test_page_from_listed_host_is_stored(self, store, hosts, net):
        hosts.append("example.com")
        url = "https://example.com/article"
        net.pages[url] = _html()
        pipeline = _pipeline()
        app = _make_app(store, pipeline)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"source_type": "url", "uri": url, "name": "Article"},
            )
            assert resp.status == 201
            sid = (await resp.json())["id"]
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            await _drain(app)
        row = store.db.execute(
            "SELECT uri, source_type FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        assert (row["uri"], row["source_type"]) == (url, "url")
        assert pipeline.stored == [(PAGE, ".html", "Article", sid)]
        assert _status(store, sid) == "synced"


class TestSyncWebPageSource:
    @pytest.mark.asyncio
    async def test_sync_of_unlisted_host_is_refused_and_row_is_untouched(self, store, hosts, net):
        sid = store.add_source("Old", "url", "https://other.com/a")
        store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
        store.db.commit()
        app = _make_app(store, _pipeline())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "url_host_not_allowed"
        assert _status(store, sid) == "synced"
        assert not app["_bg_tasks"]
        assert net.opened == []

    @pytest.mark.asyncio
    async def test_a_refusal_at_fetch_time_is_audited(self, store, hosts, net, monkeypatch):
        """The host was removed after the sync was accepted: nothing is fetched,
        the row ends in error, and the refusal is written to the audit log."""
        sid = store.add_source("Page", "url", "https://example.com/a")
        audited: list[tuple[str, dict]] = []
        monkeypatch.setattr(kh, "_sel_log", lambda event, **fields: audited.append((event, fields)))
        pipeline = _pipeline()
        await kh._background_url_sync(sid, "https://example.com/a", "Page", store, pipeline)
        assert _status(store, sid) == "error"
        assert net.opened == []
        pipeline.ingest_file.assert_not_awaited()
        assert audited == [
            (
                "source.sync_denied",
                {"source_id": sid, "reason": "url_host_not_allowed", "outcome": "denied"},
            )
        ]


class TestConfigReportsHosts:
    @pytest.mark.asyncio
    async def test_config_lists_allowed_hosts(self, store, hosts):
        hosts.append("example.com")
        async with TestClient(TestServer(_make_app(store, _pipeline()))) as client:
            resp = await client.get("/api/knowledge/config")
            body = await resp.json()
        assert body["url_allowed_hosts"] == ["example.com"]


def _config(**flags):
    """A config load as the loader would return it, with the given read flags."""
    from dataclasses import replace

    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.knowledge.doc_ingest_hosts = ["example.com"]
    return replace(cfg, **flags)


class TestUnreadableHostList:
    """A config file the loader could not read must not stand in for the user's list."""

    @pytest.mark.parametrize("flag", ["_base_unreadable", "_overlay_unreadable"])
    def test_a_partial_load_raises(self, monkeypatch, flag):
        monkeypatch.setattr(
            kh.KiroCrewConfig, "load", staticmethod(lambda: _config(**{flag: True}))
        )
        with pytest.raises(kh.DocIngestHostsUnavailable):
            kh._doc_ingest_hosts()

    def test_a_degraded_knowledge_section_raises(self, monkeypatch):
        monkeypatch.setattr(
            kh.KiroCrewConfig,
            "load",
            staticmethod(lambda: _config(_degraded_sections=frozenset({"knowledge"}))),
        )
        with pytest.raises(kh.DocIngestHostsUnavailable):
            kh._doc_ingest_hosts()

    def test_a_whole_load_returns_the_list(self, monkeypatch):
        monkeypatch.setattr(kh.KiroCrewConfig, "load", staticmethod(lambda: _config()))
        assert kh._doc_ingest_hosts() == ["example.com"]

    @pytest.fixture()
    def unreadable(self, monkeypatch):
        def _raise():
            raise kh.DocIngestHostsUnavailable("unreadable")

        monkeypatch.setattr(kh, "_doc_ingest_hosts", _raise)

    @pytest.mark.asyncio
    async def test_config_reports_null_not_an_empty_list(self, store, unreadable):
        async with TestClient(TestServer(_make_app(store, _pipeline()))) as client:
            resp = await client.get("/api/knowledge/config")
            body = await resp.json()
        assert resp.status == 200
        assert body["url_allowed_hosts"] is None

    @pytest.mark.asyncio
    async def test_add_and_sync_fail_closed_with_a_reason(self, store, unreadable, net):
        sid = store.add_source("Page", "url", "https://example.com/a")
        app = _make_app(store, _pipeline())
        async with TestClient(TestServer(app)) as client:
            add = await client.post(
                "/api/knowledge/sources",
                json={"source_type": "url", "uri": "https://example.com/b"},
            )
            add_body = await add.json()
            sync = await client.post(f"/api/knowledge/sources/{sid}/sync")
            sync_body = await sync.json()
        assert (add.status, add_body["code"]) == (503, "doc_ingest_hosts_unavailable")
        assert (sync.status, sync_body["code"]) == (503, "doc_ingest_hosts_unavailable")
        assert _source_count(store) == 1
        assert not app["_bg_tasks"]
        assert net.opened == []


class TestPageText:
    @pytest.mark.asyncio
    async def test_the_declared_charset_decodes_the_page(self):
        web_ = FakeWeb()
        body = "<p>Café, naïve, 東京</p>".encode("shift_jis", errors="replace")
        web_.pages["https://example.com/a"] = (
            200,
            {"Content-Type": "text/html; charset=Shift_JIS"},
            body,
        )
        page = await web_.fetch("https://example.com/a", ["example.com"])
        assert "東京" in page.text

    @pytest.mark.asyncio
    async def test_a_charset_declared_only_in_the_markup_decodes_the_page(self):
        web_ = FakeWeb()
        body = '<html><head><meta charset="windows-1252"></head><p>Café résumé</p>'.encode(
            "windows-1252"
        )
        web_.pages["https://example.com/a"] = (200, {"Content-Type": "text/html"}, body)
        page = await web_.fetch("https://example.com/a", ["example.com"])
        assert "Café résumé" in page.text

    @pytest.mark.asyncio
    async def test_a_charset_named_in_plain_text_prose_is_not_obeyed(self):
        web_ = FakeWeb()
        body = "Café. To force Latin-1, a page says charset=iso-8859-1 in its head.".encode()
        web_.pages["https://example.com/a"] = (200, {"Content-Type": "text/plain"}, body)
        page = await web_.fetch("https://example.com/a", ["example.com"])
        assert page.text.startswith("Café.")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["text/plain; charset=bogus", "text/markdown; charset="])
    async def test_an_unusable_header_charset_does_not_fall_back_to_the_prose(self, header):
        web_ = FakeWeb()
        body = 'Café. Example: <meta charset="iso-8859-1"> in a page head.'.encode()
        web_.pages["https://example.com/a"] = (200, {"Content-Type": header}, body)
        page = await web_.fetch("https://example.com/a", ["example.com"])
        assert page.text.startswith("Café.")

    @pytest.mark.asyncio
    async def test_an_unknown_charset_falls_back_to_utf8(self):
        web_ = FakeWeb()
        web_.pages["https://example.com/a"] = (
            200,
            {"Content-Type": "text/plain; charset=no-such-codec"},
            "naïve".encode(),
        )
        page = await web_.fetch("https://example.com/a", ["example.com"])
        assert page.text == "naïve"

    @pytest.mark.asyncio
    async def test_a_host_the_vet_cannot_parse_is_invalid_not_inward(self, monkeypatch):
        from kiro_crew.link_unfurl import UnfurlRejected

        def _reject(_url, resolve=None):
            raise UnfurlRejected("invalid_url")

        monkeypatch.setattr(url_fetch, "vet_unfurl_url", _reject)
        with pytest.raises(UrlFetchRefused) as exc:
            await FakeWeb().fetch("https://example.com/a", ["example.com"])
        assert exc.value.code == "url_invalid"

    @pytest.mark.asyncio
    async def test_credentials_in_the_page_are_scrubbed_before_ingest(self, store, hosts, net):
        hosts.append("example.com")
        secret = "AKIAIOSFODNN7EXAMPLE"
        net.pages["https://example.com/a"] = (
            200,
            {"Content-Type": "text/plain"},
            f"Notes. The key is {secret} here.".encode(),
        )
        sid = store.add_source("Page", "url", "https://example.com/a")
        pipeline = _pipeline()
        await kh._background_url_sync(sid, "https://example.com/a", "Page", store, pipeline)
        stored, suffix, _name, _sid = pipeline.stored[0]
        assert suffix == ".txt"
        assert secret.encode() not in stored
        assert b"[REDACTED" in stored


class TestCompressedAnswer:
    @pytest.mark.asyncio
    async def test_a_compressed_body_is_refused(self):
        import gzip

        async def _gz(_request):
            return web.Response(
                body=gzip.compress(b"<p>hello</p>"),
                headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
            )

        app = web.Application()
        app.router.add_get("/gz", _gz)
        async with TestServer(app) as server:
            with pytest.raises(UrlFetchFailed, match="compressed"):
                await url_fetch._open(_vetted_for(server, "/gz"), aiohttp.ClientTimeout(total=5))
