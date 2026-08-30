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
    assert (
        source.source_ref_label(source.parse_source_url("https://github.com/o/r/pull/12")) == "#12"
    )
    assert (
        source.source_ref_label(
            source.parse_source_url("https://gitlab.com/g/p/-/merge_requests/5")
        )
        == "!5"
    )


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
async def test_plugin_error_text_is_redacted_before_it_reaches_the_client(plugin) -> None:
    """A plugin's EXCEPTION goes through the scrubber its payload goes through.

    `_redact_provider_data` covers the returned dict, but an exception took a
    second route to the client that skipped every scrubber: the message lands
    verbatim in the 503 (`SourceProviderError`) or 400 (`ValueError`) body. A
    built-in cannot leak this way because every built-in failure path runs its
    provider's stderr through `_safe_error` first.
    """
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"

    async def fetch_full(ref, *, refresh: bool = False):
        raise source.SourceProviderError(f"acme said: token={secret}")

    plugin.fetch_full = fetch_full
    with pytest.raises(source.SourceProviderError) as read_exc:
        await source.fetch_pull_request(CR_URL)
    assert secret not in str(read_exc.value)
    # The surrounding prose survives, so the operator still learns what failed.
    assert "acme said" in str(read_exc.value)

    async def comment(ref, body: str) -> None:
        raise ValueError(f"acme refused: token={secret}")

    plugin.comment = comment
    with pytest.raises(ValueError) as write_exc:
        await source.comment_on_pull_request(CR_URL, "hello")
    assert secret not in str(write_exc.value)
    assert "acme refused" in str(write_exc.value)


@pytest.mark.asyncio
async def test_plugin_setup_message_is_redacted_too(plugin) -> None:
    # `setup_message()` is edition-authored guidance, but it is still provider
    # text on the same 503 route, so it gets the same treatment.
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    plugin.not_configured = True
    plugin.setup_message = lambda: f"Set ACME_TOKEN (currently token={secret})"
    with pytest.raises(source.SourceProviderError) as exc:
        await source.fetch_pull_request(CR_URL)
    assert secret not in str(exc.value)
    assert "ACME_TOKEN" in str(exc.value)


@pytest.mark.asyncio
async def test_not_configured_from_a_mutation_hook_is_redacted(plugin) -> None:
    """`SourceProviderNotConfigured` from a WRITE is scrubbed, type intact.

    The fetch callers substitute the plugin's setup guidance for this signal,
    but the mutation hooks have no such substitution: the exception's own
    message is what reaches the 503 body. It must go through the same scrubber
    as every other plugin exception, while keeping its subclass so the caller's
    handling is unchanged.
    """
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"

    async def comment(ref, body: str) -> None:
        raise source.SourceProviderNotConfigured(f"acme not configured: token={secret}")

    plugin.comment = comment
    with pytest.raises(source.SourceProviderNotConfigured) as exc:
        await source.comment_on_pull_request(CR_URL, "hello")
    assert secret not in str(exc.value)
    assert "acme not configured" in str(exc.value)


@pytest.mark.asyncio
async def test_capacity_error_from_a_plugin_keeps_its_retryable_type(plugin) -> None:
    # The HTTP layer maps `SourceCapacityError` to a retryable response, so the
    # redaction boundary must preserve the subclass rather than flatten it to
    # the parent and silently change the status code a plugin's caller sees.
    async def fetch_full(ref, *, refresh: bool = False):
        raise source.SourceCapacityError("acme is busy")

    plugin.fetch_full = fetch_full
    with pytest.raises(source.SourceCapacityError):
        await source.fetch_pull_request(CR_URL)


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


@pytest.mark.asyncio
async def test_thread_write_hooks_are_dispatched(plugin) -> None:
    """resolveThreads is one capability: resolve, reopen, and reply all dispatch.

    Reopen rides the same `resolve_thread` hook with `resolved=False`, and reply
    rides `reply_to_thread` -- the affordances the frontend renders for
    `capabilities.resolveThreads` must all reach the plugin instead of falling
    into the GitHub-only GraphQL path and 400ing.
    """
    calls: list[tuple[str, ...]] = []

    async def resolve_thread(ref, thread_id: str, *, resolved: bool) -> None:
        calls.append(("resolve", ref.url, thread_id, str(resolved)))

    async def reply_to_thread(ref, thread_id: str, body: str) -> None:
        calls.append(("reply", ref.url, thread_id, body))

    plugin.resolve_thread = resolve_thread  # type: ignore[attr-defined]
    plugin.reply_to_thread = reply_to_thread  # type: ignore[attr-defined]
    await source.resolve_pull_request_thread(CR_URL, "acme-thread-9")
    await source.unresolve_pull_request_thread(CR_URL, "acme-thread-9")
    await source.reply_to_review_thread(CR_URL, "acme-thread-9", "done in r2")
    assert calls == [
        ("resolve", CR_URL, "acme-thread-9", "True"),
        ("resolve", CR_URL, "acme-thread-9", "False"),
        ("reply", CR_URL, "acme-thread-9", "done in r2"),
    ]


@pytest.mark.asyncio
async def test_reopen_refusal_names_reopening_not_replies(plugin) -> None:
    # The fake plugin has no resolve_thread hook: the reopen refusal must say so
    # in reopen's own words, not borrow the reply wording.
    with pytest.raises(ValueError, match="Reopening review threads"):
        await source.unresolve_pull_request_thread(CR_URL, "thread-1")


def test_plugin_issue_refs_are_refused_at_admission(plugin) -> None:
    """No plugin fetch path serves an issue, so an issue ref must never admit.

    An admitted issue ref would become a sidebar chip whose panel can only 400;
    refusing at parse keeps the two layers agreeing that a plugin provider is
    change-only.
    """

    def parse_issue(raw_url: str):
        ref = FakeAcmePlugin.parse(plugin, raw_url)
        if ref is None:
            return None
        return source.SourceRef(
            ref.provider, ref.url, ref.host, ref.owner, ref.repo, ref.number, kind="issue"
        )

    plugin.parse = parse_issue  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        source.parse_source_url(CR_URL)


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
