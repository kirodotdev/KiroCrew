"""Source-provider plugin seam (backend).

Covers what a downstream edition depends on: that its plugin's URLs parse, that
its fetches are dispatched through the SHARED hardening (cache + redaction), that
an unimplemented mutation fails with a clear reason rather than falling into a
GitHub-only branch, and that its links reach the sidebar chip payload with the
plugin's own label.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import source_providers as source

CR_URL = "https://review.acme.example/cr/123"


@pytest.fixture(autouse=True)
def _mock_source_sel(monkeypatch):
    monkeypatch.setattr(source, "_sel", lambda: MagicMock())


class FakeAcmePlugin:
    """Stand-in for an enterprise edition's internal code-review system."""

    id = "acme"

    def __init__(self) -> None:
        self.full_calls = 0
        self.checks_calls = 0
        self.not_configured = False

    def parse(self, raw_url: str) -> source.SourceRef | None:
        prefix = "https://review.acme.example/cr/"
        if not raw_url.startswith(prefix):
            return None
        number = raw_url[len(prefix) :].rstrip("/")
        if not number.isdigit():
            return None
        return source.SourceRef(
            "acme",
            f"{prefix}{number}",
            "review.acme.example",
            "",
            "acme",
            int(number),
            kind="change",
        )

    async def fetch_full(self, ref: source.SourceRef, *, refresh: bool = False) -> dict[str, Any]:
        self.full_calls += 1
        if self.not_configured:
            raise source.SourceProviderNotConfigured("no token")
        return {
            "provider": "acme",
            "url": ref.url,
            "number": ref.number,
            "title": "Widen the seam",
            # A credential the shared redaction layer must scrub before caching.
            "description": "token=ghp_0123456789abcdefghijklmnopqrstuvwxyz",
            "state": "open",
            "checks": [],
            "commits": [],
            "comments": [],
            "files": [],
        }

    async def fetch_checks(self, ref: source.SourceRef) -> list[dict[str, Any]]:
        self.checks_calls += 1
        return [{"name": "acme-build", "bucket": "passed"}]

    async def fetch_check_status(self, ref: source.SourceRef) -> dict[str, str]:
        return {"ci": "passed", "state": "open", "secret": "dropped"}

    def chip_label(self, ref: source.SourceRef) -> str:
        return f"CR-{ref.number}"

    def path_markers(self) -> list[str]:
        return ["/cr/"]

    def setup_message(self) -> str:
        return "Set ACME_TOKEN to load Acme reviews."


@pytest.fixture
def plugin(monkeypatch):
    source.reset_source_providers_for_tests()
    # The shared caches are module state; keep each test independent.
    monkeypatch.setattr(source, "_CACHE", {})
    monkeypatch.setattr(source, "_FULL_FETCH_INFLIGHT", {})
    monkeypatch.setattr(source, "_FULL_FETCH_TASKS", {})
    monkeypatch.setattr(source, "_FULL_FETCH_GENERATIONS", {})
    monkeypatch.setattr(source, "_CHECKS_FETCH_INFLIGHT", {})
    instance = FakeAcmePlugin()
    source.register_source_provider(instance)
    yield instance
    source.reset_source_providers_for_tests()


def test_register_refuses_builtin_duplicate_and_malformed_id(plugin) -> None:
    class Shadow(FakeAcmePlugin):
        id = "github"

    with pytest.raises(ValueError, match="built-in"):
        source.register_source_provider(Shadow())
    with pytest.raises(ValueError, match="already registered"):
        source.register_source_provider(FakeAcmePlugin())

    class Bad(FakeAcmePlugin):
        id = "Acme Review"

    with pytest.raises(ValueError, match="must match"):
        source.register_source_provider(Bad())


def test_parse_source_url_dispatches_to_the_plugin(plugin) -> None:
    ref = source.parse_source_url(CR_URL)
    assert (ref.provider, ref.number, ref.kind, ref.url) == ("acme", 123, "change", CR_URL)


def test_unregistered_url_is_still_refused() -> None:
    source.reset_source_providers_for_tests()
    with pytest.raises(ValueError):
        source.parse_source_url(CR_URL)


def test_builtin_parsing_is_unchanged(plugin) -> None:
    gh = source.parse_source_url("https://github.com/kirodotdev/KiroCrew/pull/58")
    assert (gh.provider, gh.number) == ("github", 58)
    gl = source.parse_source_url("https://gitlab.com/g/sub/p/-/merge_requests/5")
    assert (gl.provider, gl.number) == ("gitlab", 5)


def test_source_ref_label_uses_the_plugin_chip_label(plugin) -> None:
    assert source.source_ref_label(source.parse_source_url(CR_URL)) == "CR-123"
    # Built-in conventions untouched.
    assert source.source_ref_label(
        source.parse_source_url("https://github.com/o/r/pull/12")
    ) == "#12"
    assert source.source_ref_label(
        source.parse_source_url("https://gitlab.com/g/p/-/merge_requests/5")
    ) == "!5"


def test_path_markers_include_the_plugin_contribution(plugin) -> None:
    markers = source.source_link_path_markers()
    assert "/cr/" in markers
    for builtin in ("/pull/", "/merge_requests/", "/issues/", "/browse/"):
        assert builtin in markers


@pytest.mark.asyncio
async def test_fetch_pull_request_dispatches_redacts_and_caches(plugin) -> None:
    first = await source.fetch_pull_request(CR_URL)
    assert first["provider"] == "acme"
    # Shared redaction ran on the plugin's payload.
    assert "ghp_" not in first["description"]
    assert "REDACTED" in first["description"]
    # Second read is served from the shared cache, not the plugin.
    second = await source.fetch_pull_request(CR_URL)
    assert second == first
    assert plugin.full_calls == 1


@pytest.mark.asyncio
async def test_fetch_pull_request_checks_dispatches_to_the_plugin(plugin) -> None:
    checks = await source.fetch_pull_request_checks(CR_URL)
    assert checks == [{"name": "acme-build", "bucket": "passed"}]
    assert plugin.checks_calls == 1


@pytest.mark.asyncio
async def test_not_configured_surfaces_the_plugin_setup_message(plugin) -> None:
    plugin.not_configured = True
    with pytest.raises(source.SourceProviderError, match="ACME_TOKEN"):
        await source.fetch_pull_request(CR_URL)


@pytest.mark.asyncio
async def test_chip_status_projects_only_ci_and_state(plugin) -> None:
    status = await source._fetch_check_status(CR_URL)
    assert status == {"ci": "passed", "state": "open"}


@pytest.mark.asyncio
async def test_unsupported_mutations_name_the_provider(plugin) -> None:
    # The fake plugin implements no mutation hooks, so every write must refuse
    # with a reason that names this provider rather than mentioning GitHub.
    with pytest.raises(ValueError, match="'acme'"):
        await source.comment_on_pull_request(CR_URL, "hello")
    with pytest.raises(ValueError, match="'acme'"):
        await source.resolve_pull_request_thread(CR_URL, "thread-1")
    with pytest.raises(ValueError, match="'acme'"):
        await source.mark_pull_request_ready(CR_URL)
    with pytest.raises(ValueError, match="'acme'"):
        await source.enable_pull_request_auto_merge(CR_URL)
    with pytest.raises(ValueError, match="'acme'"):
        await source.reply_to_review_thread(CR_URL, "thread-1", "hi")


@pytest.mark.asyncio
async def test_supported_mutation_hook_is_dispatched(plugin) -> None:
    seen: list[tuple[str, str]] = []

    async def comment(ref: source.SourceRef, body: str) -> None:
        seen.append((ref.url, body))

    plugin.comment = comment  # type: ignore[attr-defined]
    await source.comment_on_pull_request(CR_URL, "looks good")
    assert seen == [(CR_URL, "looks good")]


def test_sidebar_source_links_include_the_plugin_chip(plugin) -> None:
    """The sidebar chip scanner must reach a plugin URL and label it."""
    from kiro_crew.dashboard import state as state_mod

    slot = object.__new__(state_mod._ChatSlot)
    slot.messages = [{"role": "assistant", "content": f"Raised {CR_URL} for review."}]
    slot._source_links_revision = 1
    slot._source_links_cache = None

    links = state_mod._ChatSlot._pr_source_links(slot)
    assert links == [
        {
            "provider": "acme",
            "number": 123,
            "url": CR_URL,
            "kind": "change",
            "label": "CR-123",
        }
    ]
