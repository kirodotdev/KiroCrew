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

from kiro_crew import history, history_search
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.slot_projection import SlotProjection

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

    def search_ref(self, token: str) -> tuple[str, tuple[str, ...]] | None:
        """Recognize this edition's own review ids, however a transcript wrote them.

        A real implementation of the optional hook rather than a lambda: this is
        the seam's only in-tree consumer, so it has to be driven the way a
        downstream edition would drive it.
        """
        import re

        match = re.fullmatch(r"(?:cr-)?(\d{2,9})", token.casefold())
        if match is None:
            return None
        number = match.group(1)
        return (f"cr-{number}", (f"review.acme.example/cr/{number}", f"cr {number}"))

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


def test_path_markers_refuse_prefilter_defeating_contributions(plugin) -> None:
    # A bare "/" (or any one-character marker) would admit essentially every
    # URL and turn the prefilter into a full parse of the whole transcript,
    # and an unbounded marker inflates every substring check the scanner runs.
    # Only the well-formed contribution survives.
    plugin.path_markers = lambda: ["/", "/x", "cr/", "/" + "a" * 100, "/ok/"]
    markers = source.source_link_path_markers()
    assert "/ok/" in markers
    for rejected in ("/", "/x", "cr/", "/" + "a" * 100):
        assert rejected not in markers


def test_path_markers_cap_consumption_of_a_generator_hook(plugin) -> None:
    # "Bounded per plugin" must hold for the materialization step too: islice
    # stops pulling at the cap, so a generator-returning hook is not exhausted
    # before slicing.
    consumed: list[int] = []

    def markers_gen():
        for index in range(50):
            consumed.append(index)
            yield f"/gen{index:02d}/"

    plugin.path_markers = lambda: markers_gen()
    contributed = [m for m in source.source_link_path_markers() if m.startswith("/gen")]
    assert len(contributed) == source._MAX_PLUGIN_PATH_MARKERS
    # islice may look one element past the cap, but a list()-then-slice would
    # have consumed all 50 -- the exhaustion is what the bound forbids.
    assert len(consumed) <= source._MAX_PLUGIN_PATH_MARKERS + 1


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
async def test_confirmation_required_from_a_plugin_keeps_its_answerable_type(plugin) -> None:
    # `ConfirmationRequired` is what makes the mutation response carry
    # `confirmationRequired: True` -- the client's only cue to offer the
    # confirm-and-retry affordance. The redaction boundary must not downcast
    # it to the plain-`ValueError` arm (which would render a dead-end error),
    # and its message is scrubbed like any other plugin failure text.
    async def enable_auto_merge(ref, *, confirm_immediate_merge: bool = False):
        raise source.ConfirmationRequired(
            "would merge now; token=ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        )

    plugin.enable_auto_merge = enable_auto_merge
    with pytest.raises(source.ConfirmationRequired) as excinfo:
        await source.enable_pull_request_auto_merge(CR_URL)
    assert "ghp_" not in str(excinfo.value)


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
    # This test builds the slot with ``object.__new__`` to skip ``__init__``, so it
    # must hand-supply what the scanner reaches for. The scan body now lives on
    # SlotProjection, which is stateless (every method is a staticmethod taking the
    # slot), so a bare instance is the whole dependency.
    slot._projection = SlotProjection()

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


def test_sidebar_dedups_on_ref_identity_not_canonical_url(plugin) -> None:
    """One change mentioned under two URL shapes renders ONE chip, newest pin wins.

    A provider whose grammar accepts an optional revision pin and keeps it in
    the canonical URL (the seam's ``SourceRef`` has no revision field, so the
    URL is the only place a pin can travel) produces two canonical URLs for one
    change. Keying the dedup on the ref identity collapses them; the backwards
    walk makes the most recent mention's URL the chip's link target.
    """
    from kiro_crew.dashboard import state as state_mod

    prefix = "https://review.acme.example/cr/"

    def parse_with_revision_pin(raw_url: str) -> source.SourceRef | None:
        if not raw_url.startswith(prefix):
            return None
        tail = raw_url[len(prefix) :].rstrip("/")
        number, _, pin = tail.partition("/")
        if not number.isdigit():
            return None
        if pin and not (pin.startswith("revisions/") and pin[len("revisions/") :].isdigit()):
            return None
        canonical = f"{prefix}{number}/{pin}" if pin else f"{prefix}{number}"
        return source.SourceRef(
            "acme",
            canonical,
            "review.acme.example",
            "",
            "acme",
            int(number),
            kind="change",
        )

    plugin.parse = parse_with_revision_pin  # type: ignore[attr-defined]

    slot = object.__new__(state_mod._ChatSlot)
    slot.messages = [
        {"role": "assistant", "content": f"Raised {CR_URL} for review."},
        {"role": "assistant", "content": f"New revision: {CR_URL}/revisions/2"},
    ]
    slot._source_links_revision = 1
    slot._source_links_cache = None
    slot._projection = SlotProjection()

    links = state_mod._ChatSlot._pr_source_links(slot)
    # One chip, not two -- and it carries the NEWEST mention's URL (the pinned
    # revision), because the backwards walk admits the most recent mention first.
    assert links == [
        {
            "provider": "acme",
            "number": 123,
            "url": f"{CR_URL}/revisions/2",
            "kind": "change",
            "label": "CR-123",
        }
    ]


def test_sidebar_keeps_same_numbered_changes_on_different_repos_distinct(plugin) -> None:
    """Identity keying must not over-collapse: same number, different repo."""
    from kiro_crew.dashboard import state as state_mod

    slot = object.__new__(state_mod._ChatSlot)
    slot.messages = [
        {
            "role": "assistant",
            "content": (
                "Compare https://github.com/acme/tools/pull/7 with "
                "https://github.com/acme/gadgets/pull/7 today."
            ),
        }
    ]
    slot._source_links_revision = 1
    slot._source_links_cache = None
    slot._projection = SlotProjection()

    links = state_mod._ChatSlot._pr_source_links(slot)
    assert {link["url"] for link in links} == {
        "https://github.com/acme/tools/pull/7",
        "https://github.com/acme/gadgets/pull/7",
    }


def test_sidebar_keeps_same_host_jira_context_paths_distinct(plugin, monkeypatch) -> None:
    """Two Jira instances on one host, same issue key, must stay two chips.

    A self-hosted Jira's context path exists only in the canonical URL, so an
    identity that dropped the URL entirely would collide them and one link
    would silently disappear from the sidebar.
    """
    from kiro_crew.dashboard import state as state_mod

    monkeypatch.setattr(source, "_jira_hosts_snapshot", frozenset({"jira.acme.internal"}))
    monkeypatch.setattr(source, "_gitlab_hosts_loaded_at", 1.0)
    monkeypatch.setattr(source.time, "monotonic", lambda: 2.0)

    slot = object.__new__(state_mod._ChatSlot)
    slot.messages = [
        {
            "role": "assistant",
            "content": (
                "Compare https://jira.acme.internal/teamA/browse/PROJ-9 with "
                "https://jira.acme.internal/teamB/browse/PROJ-9 today."
            ),
        }
    ]
    slot._source_links_revision = 1
    slot._source_links_cache = None
    slot._projection = SlotProjection()

    links = state_mod._ChatSlot._pr_source_links(slot)
    assert {link["url"] for link in links} == {
        "https://jira.acme.internal/teamA/browse/PROJ-9",
        "https://jira.acme.internal/teamB/browse/PROJ-9",
    }


def test_search_ref_is_absent_until_a_plugin_offers_the_hook(plugin, monkeypatch) -> None:
    # Every optional hook is looked up with getattr, so a plugin offering no
    # callable contributes nothing rather than failing obscurely.
    monkeypatch.setattr(plugin, "search_ref", None)
    assert source.source_search_ref("cr-123") is None


def test_search_ref_hands_the_contribution_through_unchanged(plugin) -> None:
    # The collector is the FAN-OUT across plugins and nothing else. Casefolding,
    # the cap and the dedup are the search module's, applied once there.
    plugin.search_ref = lambda token: ("CR-123", ("Reviews/CR-123", "CR 123"))

    assert source.source_search_ref("CR-123") == (
        "CR-123",
        ("Reviews/CR-123", "CR 123"),
    )


def test_search_ref_drops_a_malformed_contribution(plugin) -> None:
    # A bare string would iterate into characters, and an empty canonical names
    # no item -- both degrade to "this provider does not claim the token". The
    # collector hands the answer through; the single normalizer is what rejects
    # it, so the contract is asserted where the search actually consumes it.
    for answer in (("", ("a/1",)), ("cr-123", "reviews/cr-123"), (), "cr-123", (1, ["a/1"])):
        plugin.search_ref = lambda token, answer=answer: answer
        assert history_search._provider_search_ref("cr-123") is None, answer


def test_the_search_caps_a_contribution_the_collector_passed_through(plugin) -> None:
    # End-to-end: each spelling costs one substring scan of every scanned session
    # per field, so the ceiling has to hold wherever the answer came from.
    plugin.search_ref = lambda token: ("cr-123", tuple(f"s{i}/123" for i in range(50)))

    found = history_search._provider_search_ref("cr-123")

    assert found is not None
    assert len(found[1]) == history_search._MAX_SEARCH_REF_SPELLINGS


def test_search_ref_survives_a_raising_hook(plugin) -> None:
    def boom(token: str):
        raise RuntimeError("provider is on fire")

    plugin.search_ref = boom

    # Contained by the normalizer's call guard, which wraps the whole fan-out
    # because this collector IS the resolver it calls -- so a plugin defect
    # cannot escape into an HTTP 500 on every search.
    assert history_search._provider_search_ref("cr-123") is None


def test_registration_publishes_the_collector_into_the_search(plugin) -> None:
    # dashboard -> core, at registration rather than from a route handler:
    # parse_search_query is also reached from paths that serve no HTTP, and a
    # process that never ran a route would answer the same query differently.
    assert history_search._search_ref_resolver is source.source_search_ref
    plugin.search_ref = lambda token: (
        ("rev-987654321", ("reviews/rev-987654321",)) if token == "rev-987654321" else None
    )

    needles, _, _ = history.parse_search_query("rev-987654321")

    required = [n for n in needles if n.required]
    assert len(required) == 1, required
    assert required[0].text == "rev-987654321"
    assert required[0].alts == ("reviews/rev-987654321",)


def test_resetting_the_registry_also_unpublishes_the_collector() -> None:
    source.register_source_provider(FakeAcmePlugin())
    assert history_search._search_ref_resolver is source.source_search_ref

    source.reset_source_providers_for_tests()

    assert history_search._search_ref_resolver is None


class FakeBetaPlugin(FakeAcmePlugin):
    """A SECOND edition, whose items carry their own numbers."""

    id = "beta"

    def parse(self, raw_url: str) -> source.SourceRef | None:
        return None

    def search_ref(self, token: str) -> tuple[str, tuple[str, ...]] | None:
        import re

        match = re.fullmatch(r"(?:rev-)?(\d{2,9})", token.casefold())
        if match is None:
            return None
        number = match.group(1)
        return (f"rev-{number}", (f"review.beta.example/r/{number}",))


def test_a_raising_provider_does_not_abort_the_fan_out(plugin) -> None:
    # One broken edition must not hide every later provider's items: without
    # per-provider isolation the raise escapes and the beta match never surfaces.
    def boom(token: str):
        raise RuntimeError("provider is on fire")

    plugin.search_ref = boom
    source.register_source_provider(FakeBetaPlugin())

    found = source.source_search_ref("4287")

    assert found is not None, "the second provider's match was lost"
    canonical, alts = found
    assert canonical == "rev-4287", canonical
    assert "review.beta.example/r/4287" in alts, alts


def test_a_numeric_token_also_stops_at_the_first_matching_provider(plugin) -> None:
    # First answer wins for EVERY token shape. A cross-plugin merge would serve
    # two registrants holding the same number, and this repo registers none.
    source.register_source_provider(FakeBetaPlugin())

    found = source.source_search_ref("4287")

    assert found is not None
    canonical, alts = found
    assert canonical == "cr-4287"
    assert "review.acme.example/cr/4287" in alts
    assert not any("beta" in alt for alt in alts), alts


def test_a_prefixed_token_still_stops_at_the_first_provider(plugin) -> None:
    # A prefixed id names ONE item, so merging would conflate distinct items.
    source.register_source_provider(FakeBetaPlugin())

    found = source.source_search_ref("cr-4287")

    assert found is not None
    canonical, alts = found
    assert canonical == "cr-4287"
    assert not any("beta" in alt for alt in alts), alts


def test_core_bounds_the_spellings_one_provider_can_contribute(plugin, monkeypatch) -> None:
    # The cap lives once, in core: the collector keeps no ceiling of its own,
    # which would be a second number pinned equal to core's by a comment.
    monkeypatch.setattr(
        plugin, "search_ref", lambda token: ("cr-4287", tuple(f"a{i}/4287" for i in range(50)))
    )

    normalized = history_search._provider_search_ref("4287")

    assert normalized is not None
    assert len(normalized[1]) == history_search._MAX_SEARCH_REF_SPELLINGS
    assert not hasattr(source, "_MAX_SEARCH_SPELLINGS_PER_PROVIDER")


def test_the_seam_finds_a_transcript_that_cited_the_item_only_by_url(plugin, tmp_path) -> None:
    """End to end through the registered plugin's own hook -- the seam's point.

    Nothing in this repo registers a source provider in production, so this is
    the only place the hook is driven the way a downstream edition drives it.
    """
    log = history.ConversationLog(base_dir=tmp_path)
    log.append("by_url", "assistant", f"opened {CR_URL} for review")
    log.append("unrelated", "assistant", "notes about the deploy window")

    keys = {s["key"] for s in log.search_sessions("cr-123", 10)}

    assert keys == {"by_url"}, keys
    # The control: the transcript spells the id `cr/123`, never `cr-123`, so the
    # hit came from the plugin's spelling. Withhold the hook and it disappears.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(plugin, "search_ref", None)
    try:
        assert log.search_sessions("cr-123", 10) == []
    finally:
        monkeypatch.undo()
