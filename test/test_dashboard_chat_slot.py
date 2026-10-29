"""Dashboard chat: ``_ChatSlot`` unit behaviour — messages, source links, reader flag, isolation."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.state import _MAX_SLOT_MESSAGES, _MAX_SOURCE_LINKS_PER_SLOT, _ChatSlot

# ── Slot unit tests ──


class TestChatSlot:
    @pytest.mark.asyncio
    async def test_turn_generation_survives_task_clear(self):
        slot = _ChatSlot("s1")
        assert slot._turn_generation == 0

        first = asyncio.create_task(asyncio.sleep(0))
        slot.task = first
        assert slot._turn_generation == 1
        await first

        slot.task = None
        assert slot._turn_generation == 1

        second = asyncio.create_task(asyncio.sleep(0))
        slot.task = second
        assert slot._turn_generation == 2
        await second

    def test_append_and_drain(self):
        slot = _ChatSlot("s1")
        slot.append("user", "hello", "msg")
        slot.append("assistant", "hi", "msg")
        pending = slot.drain()
        assert len(pending) == 2
        assert pending[0]["role"] == "user"
        assert pending[1]["role"] == "assistant"
        assert slot.drain() == []

    def test_drain_clears_stale_pending_after_reader_disconnect(self):
        """Simulate SSE reader disconnect: pending chunks must be discarded."""
        slot = _ChatSlot("s1")
        slot._has_reader = True
        slot.append("assistant", "stale response", "msg")
        assert len(slot._pending) == 1
        slot.drain()
        slot._has_reader = False
        assert slot._pending == []
        assert slot.drain() == []
        slot.append("assistant", "fresh response", "msg")
        pending = slot.drain()
        assert len(pending) == 1
        assert pending[0]["content"] == "fresh response"

    def test_total_messages_survives_trim(self):
        slot = _ChatSlot("s1")
        count = _MAX_SLOT_MESSAGES + 100
        for i in range(count):
            slot.append("user", f"msg {i}")
        assert len(slot.messages) == _MAX_SLOT_MESSAGES
        assert slot.total_messages == count

    def test_trim_keeps_latest(self):
        slot = _ChatSlot("s1")
        count = _MAX_SLOT_MESSAGES + 50
        for i in range(count):
            slot.append("user", f"msg {i}")
        assert slot.messages[0]["content"] == "msg 50"
        assert slot.messages[-1]["content"] == f"msg {count - 1}"

    def test_trim_advances_the_durable_counter_by_durable_rows_only(self):
        """Transient rows folded into the frozen prefix advance ONLY the all-rows counter.

        ``_disk_older_count`` credits every persisted trimmed row (its contract
        with the save model), while ``_disk_older_durable_count`` counts only
        the rows a durable read returns — the base absolute message positions
        are built over. Counting them together is the cursor-skew defect.

        Mutation guards: advancing the durable counter by ``persisted_trim``
        re-introduces the skew; counting the slice AFTER the ``del`` counts the
        wrong (surviving) rows — the leading transient rows here make both
        mutants visibly wrong.
        """
        slot = _ChatSlot("s1")
        # The five oldest window rows: 2 transient, 3 durable.
        slot.append("permission", "approve?", "")
        slot.append("queued", "queued prompt", "")
        for i in range(3):
            slot.append("user", f"old {i}")
        for i in range(_MAX_SLOT_MESSAGES - 5):
            slot.append("user", f"fill {i}")
        # Pretend the whole window was flushed, as a 5s save would have.
        slot._disk_window_len = len(slot.messages)
        assert slot._disk_older_count == 0
        assert slot._disk_older_durable_count == 0

        # Cross the cap by 5: the trimmed slice is exactly the 5 rows above.
        for i in range(5):
            slot.append("user", f"new {i}")

        assert slot._disk_older_count == 5, "all persisted trimmed rows are credited"
        assert (
            slot._disk_older_durable_count == 3
        ), "only the durable trimmed rows advance the durable counter"

    def test_trim_counts_evicted_durable_rows_even_when_unpersisted(self):
        """Durable rows lost to the unpersisted overflow still advance the durable counter.

        ``_disk_older_count`` excludes the overflow (its save contract: it
        claims on-disk lines, and these rows never reached disk). The durable
        counter is a POSITION base with no disk contract: if evicted durable
        rows were uncounted, every later absolute position would shift down and
        a poller's ``since`` guard would pass while rows were silently skipped
        — the silent failure a blanket refusal would make loud.
        Counting them makes such a cursor refuse loudly (``since < base``).

        Mutation guard: restricting the durable count to the persisted slice
        (``messages[:persisted_trim]``) yields 2 here instead of 5.
        """
        slot = _ChatSlot("s1")
        for i in range(_MAX_SLOT_MESSAGES):
            slot.append("user", f"old {i}")
        # Only the first 2 window rows ever reached disk.
        slot._disk_window_len = 2

        for i in range(5):
            slot.append("user", f"new {i}")

        assert slot._disk_older_count == 2, "the disk counter keeps its persisted-only contract"
        assert (
            slot._disk_older_durable_count == 5
        ), "every evicted durable row advances the position base"

    def test_to_dict(self):
        slot = _ChatSlot("s1", title="Test Chat", mode="design-critique")
        slot.append("user", "hi")
        d = slot.to_dict()
        assert d["key"] == "s1"
        assert d["title"] == "Test Chat"
        assert d["mode"] == "design-critique"
        assert d["messages"] == 1
        assert d["running"] is False
        assert d["pending_approval"] is False

    def test_issue_links_do_not_crowd_out_pr_chips(self):
        """The chip budget is shared, but pull requests claim it first.

        The slice is taken after changes are grouped ahead of issues, so a PR
        chip is never starved however many issues the session mentions -- and,
        because the check-status refresh reads the same slice, that PR's CI
        updates keep being scheduled too. Issues fill only the slots left over.
        """
        slot = _ChatSlot("s1")
        pr_url = "https://github.com/acme/widgets/pull/12"
        issue_urls = [f"https://github.com/acme/widgets/issues/{n}" for n in (1, 2, 3, 4)]
        # The issues are mentioned FIRST, so a slice taken in mention order
        # would spend the whole budget before ever reaching the pull request.
        slot.append("assistant", "\n".join([*issue_urls, pr_url]), ts="t1")

        payload = slot.to_dict()
        serialized = payload["source_links"]
        assert [link["url"] for link in serialized if link["kind"] == "change"] == [pr_url]
        # The PR takes one of the three slots; two issues fit in the rest.
        assert len([link for link in serialized if link["kind"] == "issue"]) == 2
        assert len(serialized) == 3
        assert payload["source_links_total"] == 5

    def test_jira_issue_links_scanned_from_user_messages(self):
        """A Jira URL pasted by the USER becomes a sidebar chip.

        This is the primary Jira flow: people paste the ticket they are working
        from. The scan covers every durable role, and the serialized entry
        carries a ready ``label`` because PROJ-123 is the whole identifier --
        the number alone is meaningless outside its project.

        ``repo`` is NOT sent. It carried Jira's project key only so that a
        renderer predating ``label`` -- which assembles the Jira chip name as
        ``{repo}-{number}`` -- would not print ``undefined-123`` in an
        already-open tab, since a restart without a ``kiro_crew.__version__``
        bump never trips the reload-on-upgrade guard. Releases have shipped past
        the change that introduced ``label``, so no such bundle can still be
        live. Absence is asserted rather than left unpinned so the field cannot
        drift back in and grow a second naming rule.
        """
        slot = _ChatSlot("s1")
        slot.append("user", "Please look at https://acme.atlassian.net/browse/PROJ-123", ts="t1")

        payload = slot.to_dict()
        assert payload["source_links_total"] == 1
        link = payload["source_links"][0]
        assert link["provider"] == "jira"
        assert link["kind"] == "issue"
        assert link["label"] == "PROJ-123"
        assert "repo" not in link
        assert link["number"] == 123
        assert link["url"] == "https://acme.atlassian.net/browse/PROJ-123"

    def test_jira_issue_links_reevaluated_when_jira_allowlist_loads(self, monkeypatch):
        """Self-hosted Jira inherits the generation-keyed cache invalidation."""
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(sp, "_jira_hosts_snapshot", frozenset())
        monkeypatch.setattr(sp, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(sp, "_gitlab_hosts_generation", 0)

        slot = _ChatSlot("s1")
        url = "https://jira.acme.internal/browse/CORE-5"
        slot.append("assistant", f"Tracking {url}", ts="t1")
        assert slot.to_dict()["source_links_total"] == 0

        sp._publish_provider_hosts(frozenset(), frozenset({"jira.acme.internal"}))
        refreshed = slot.to_dict()
        assert refreshed["source_links_total"] == 1
        assert refreshed["source_links"][0]["url"] == url

        sp._publish_provider_hosts(frozenset(), frozenset())
        assert slot.to_dict()["source_links_total"] == 0

    def test_pr_source_links_refresh_after_same_length_content_edit(self):
        slot = _ChatSlot("s1")
        url = "https://github.com/acme/widgets/pull/12"
        linked_content = f"Review {url}"
        unlinked_content = "x" * len(linked_content)
        slot.append("assistant", unlinked_content, ts="t1")
        assert slot.to_dict()["source_links_total"] == 0

        slot.update_message("t1", content=linked_content)
        updated = slot.to_dict()
        assert updated["source_links_total"] == 1
        assert updated["source_links"][0]["url"] == url

        slot.update_message("t1", content=unlinked_content)
        assert slot.to_dict()["source_links_total"] == 0

    def test_pr_source_links_reevaluated_when_gitlab_allowlist_loads(self, monkeypatch):
        """The sync scan can run BEFORE the first off-loop allowlist load, so the
        cold-snapshot rejection must not stay memoized until the next message
        mutation -- the allowlist generation is part of the cache key."""
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(sp, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(sp, "_gitlab_hosts_generation", 0)

        slot = _ChatSlot("s1")
        url = "https://gitlab.acme.internal/team/api/-/merge_requests/7"
        slot.append("assistant", f"Opened {url}", ts="t1")
        assert slot.to_dict()["source_links_total"] == 0

        # Allowlist arrives later; no message changed.
        sp._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        refreshed = slot.to_dict()
        assert refreshed["source_links_total"] == 1
        assert refreshed["source_links"][0]["url"] == url

        # Revocation is likewise picked up without a message mutation.
        sp._publish_provider_hosts(frozenset(), frozenset())
        assert slot.to_dict()["source_links_total"] == 0

    def test_pr_source_links_ignore_streaming_numeric_prefixes(self):
        slot = _ChatSlot("s1")
        for suffix in ("1", "12", "123"):
            slot.append("chunk", f"https://github.com/acme/widgets/pull/{suffix}")
            assert slot.to_dict()["source_links_total"] == 0

        url = "https://github.com/acme/widgets/pull/1234"
        slot.append("assistant", f"Review {url}")
        payload = slot.to_dict()
        assert payload["source_links_total"] == 1
        assert payload["source_links"][0]["url"] == url

    def test_pr_source_links_detect_markdown_wrapped_urls(self):
        """Regression: '**url**' left a trailing '**' on the candidate, so the
        numeric tail check failed and the link was silently dropped."""
        slot = _ChatSlot("s1")
        url = "https://github.com/acme/widgets/pull/166"
        for i, wrapped in enumerate(
            (f"**{url}**", f"*{url}*", f"`{url}`", f"__{url}__", f"~~{url}~~")
        ):
            slot.append("assistant", f"PR is up: {wrapped} — fix(tips)", ts=f"t{i}")
        payload = slot.to_dict()
        assert payload["source_links_total"] == 1
        assert payload["source_links"][0]["url"] == url

    @pytest.mark.parametrize("role", ["chunk", "done", "streaming", "queued", "permission"])
    def test_pr_source_links_ignore_non_durable_roles(self, role):
        slot = _ChatSlot("s1")
        slot.append(role, "https://github.com/acme/widgets/pull/12")

        assert slot.to_dict()["source_links_total"] == 0

    @pytest.mark.parametrize("role", ["chunk", "done", "streaming"])
    def test_wire_only_append_does_not_dirty_or_invalidate(self, role):
        """A streamed token must not mark the slot dirty or churn the link cache.

        A wire-only row is never persisted and carries no source link, so
        dirtying the slot would make the 5-second flush re-serialize and fsync
        the whole transcript once per cycle while a reply streams, and bumping
        the source-link revision would make the next broadcast rescan the whole
        window. Neither may happen for such a row.
        """
        slot = _ChatSlot("s1")
        slot._dirty = False
        gen_before = slot._dirty_gen
        rev_before = slot._source_links_revision

        for _ in range(50):
            slot.append(role, "tok ")

        assert slot._dirty is False
        assert slot._dirty_gen == gen_before
        assert slot._source_links_revision == rev_before

    def test_durable_append_dirties_and_invalidates(self):
        """The counterpart: a persisted row still arms the flush and the rescan.

        Guards against a fix that silences wire-only rows by weakening the
        signal for every role -- the row ``_flush_segment`` writes when a
        segment closes must still reach disk and refresh the link cache.
        """
        slot = _ChatSlot("s1")
        slot._dirty = False
        rev_before = slot._source_links_revision

        slot.append("assistant", "hello")

        assert slot._dirty is True
        assert slot._source_links_revision == rev_before + 1

    def test_pr_source_links_stop_scanning_at_per_slot_cap(self):
        class CountingMessage(dict):
            reads = 0

            def get(self, key, default=None):
                if key == "content":
                    self.reads += 1
                return super().get(key, default)

        slot = _ChatSlot("s1")
        for number in range(1, _MAX_SOURCE_LINKS_PER_SLOT + 1):
            slot.append(
                "assistant",
                f"https://github.com/acme/widgets/pull/{number}",
            )
        # The scan runs newest-first, so the message it must never reach is the
        # OLDEST one -- placed before everything else in the transcript.
        beyond_cap = CountingMessage(
            role="assistant",
            content="https://github.com/acme/widgets/pull/999",
        )
        slot.messages.insert(0, beyond_cap)
        slot.invalidate_source_links()

        links = slot._pr_source_links()

        assert len(links) == _MAX_SOURCE_LINKS_PER_SLOT
        # Newest mention leads; the cap keeps the newest links, not the first ever.
        assert links[0]["number"] == _MAX_SOURCE_LINKS_PER_SLOT
        assert beyond_cap.reads == 0

    def test_pr_source_links_are_ordered_most_recently_mentioned_first(self):
        """The chip budget serializes only the first few links, so a
        first-mention order handed those slots to the oldest pull requests and
        collapsed the one being worked on into the "+N" pill."""
        slot = _ChatSlot("s1")
        for number in (1, 2, 3, 4):
            slot.append("assistant", f"https://github.com/acme/widgets/pull/{number}")

        assert [link["number"] for link in slot._pr_source_links()] == [4, 3, 2, 1]

        # Re-mentioning an OLD pull request moves it back to the head: recency is
        # last mention, which is what "the one I am working on" actually means.
        slot.append("assistant", "picking https://github.com/acme/widgets/pull/1 back up")
        links = slot._pr_source_links()
        assert [link["number"] for link in links] == [1, 4, 3, 2]
        # Still deduplicated -- the earlier mention did not survive as a second entry.
        assert len(links) == 4

    def test_pr_source_links_order_within_one_message_by_position(self):
        """Several urls in ONE message have no turn ordering to go on, so position
        in the text is the only available proxy for "mentioned later"."""
        slot = _ChatSlot("s1")
        first = "https://github.com/acme/widgets/pull/1"
        second = "https://github.com/acme/widgets/pull/2"
        slot.append("assistant", f"opened {first} then {second}")

        assert [link["url"] for link in slot._pr_source_links()] == [second, first]

    def test_pr_source_links_stop_parsing_one_message_at_the_cap(self, monkeypatch):
        """The per-message walk must stop AT the cap, not collect the whole message
        first. One message can carry thousands of urls and this runs synchronously
        on the serialization path."""
        from kiro_crew.dashboard.handlers import source_providers

        calls = 0
        real = source_providers.parse_source_url

        def counting(url):
            nonlocal calls
            calls += 1
            return real(url)

        monkeypatch.setattr(source_providers, "parse_source_url", counting)

        slot = _ChatSlot("s1")
        flood = " ".join(
            f"https://github.com/acme/widgets/pull/{n}"
            for n in range(1, _MAX_SOURCE_LINKS_PER_SLOT * 20)
        )
        slot.append("assistant", flood)

        links = slot._pr_source_links()

        assert len(links) == _MAX_SOURCE_LINKS_PER_SLOT
        # Newest-first: the tail of the message wins, not its head.
        assert links[0]["number"] == _MAX_SOURCE_LINKS_PER_SLOT * 20 - 1
        # Parsing is the expensive half; DISTINCT valid urls are bounded by the cap
        # because each admission advances the loop condition.
        assert calls <= _MAX_SOURCE_LINKS_PER_SLOT

    def test_pr_source_links_stay_linear_on_adjacent_url_prefixes(self):
        """A candidate's end is bounded by the NEXT occurrence, not by the end of the
        message.

        Content made of adjacent `https://` prefixes has no stop character until the
        very end, so an unbounded forward scan per occurrence is quadratic -- and
        `to_dict` runs this synchronously during `push_slots_update`, so a single
        crafted message could stall the gateway past its watchdog. Measured on this
        input: 0.14s bounded vs 128s unbounded, so the absolute budget below
        separates them by ~70x without comparing ratios (which flakes on loaded
        runners)."""
        payload = "https://" * 16000 + "github.com/acme/widgets/pull/1"
        assert len(payload) > 128_000
        slot = _ChatSlot("s1")
        slot.append("assistant", payload)

        started = time.perf_counter()
        links = slot._pr_source_links()
        elapsed = time.perf_counter() - started

        assert [link["url"] for link in links] == ["https://github.com/acme/widgets/pull/1"]
        assert elapsed < 10, f"scan took {elapsed:.1f}s — the per-candidate bound is gone"

    def test_pr_source_links_bound_total_parse_attempts(self, monkeypatch):
        """Every parse attempt is charged, so no flood shape can run unbounded.

        `len(found)` advances only on a NEW valid url, so a message repeating one
        REJECTED candidate never advanced it and every occurrence reached the
        parser -- a 58 MB body froze the event loop for ~13.6s. Charging attempts
        rather than successes is what bounds rejected, repeated and distinct floods
        with one mechanism (a dedup set bounded only some of them, and cost
        unbounded memory to do it)."""
        from kiro_crew.dashboard.handlers import source_providers

        calls = 0
        real = source_providers.parse_source_url

        def counting(url):
            nonlocal calls
            calls += 1
            return real(url)

        monkeypatch.setattr(source_providers, "parse_source_url", counting)
        budget = _MAX_SOURCE_LINKS_PER_SLOT * 64

        # A rejected candidate repeated far past the budget.
        slot = _ChatSlot("s1")
        slot.append("assistant", " ".join(["https://nope.example/pull/1"] * (budget * 3)))
        assert slot._pr_source_links() == []
        assert calls <= budget

        # The budget spans the WHOLE call, not one message: many messages must not
        # multiply it.
        calls = 0
        many = _ChatSlot("s2")
        for _ in range(50):
            many.append("assistant", " ".join(["https://nope.example/pull/1"] * 200))
        assert many._pr_source_links() == []
        assert calls <= budget

    def test_pr_source_links_budget_leaves_real_transcripts_untouched(self, monkeypatch):
        """The budget must be headroom, not a ceiling a real session can hit: a
        transcript mentioning the full chip allowance still yields every link."""
        slot = _ChatSlot("s1")
        for n in range(1, _MAX_SOURCE_LINKS_PER_SLOT + 1):
            slot.append("assistant", f"opened https://github.com/acme/widgets/pull/{n} for review")

        links = slot._pr_source_links()
        assert len(links) == _MAX_SOURCE_LINKS_PER_SLOT
        assert links[0]["number"] == _MAX_SOURCE_LINKS_PER_SLOT

    def test_pr_source_links_see_a_url_nested_in_another_url(self):
        """Documented consequence of walking urls backwards: a `https://` inside
        another token is now examined on its own, where the previous forward walk
        skipped past it. A pull request reached through a redirect/tracking wrapper
        is a real link, and the backend re-validates every url before any provider
        call, so surfacing it is acceptable -- pinned here so it is a decision
        rather than a surprise."""
        slot = _ChatSlot("s1")
        nested = "https://redirect.example/?to=https://github.com/acme/widgets/pull/7"
        slot.append("assistant", nested)

        assert [link["url"] for link in slot._pr_source_links()] == [
            "https://github.com/acme/widgets/pull/7",
        ]

    def test_serialized_chips_keep_the_newest_pull_requests(self):
        slot = _ChatSlot("s1")
        for number in (1, 2, 3, 4, 5):
            slot.append("assistant", f"https://github.com/acme/widgets/pull/{number}")
        for number in (10, 11, 12, 13):
            slot.append("assistant", f"https://github.com/acme/widgets/issues/{number}")

        payload = slot.to_dict()
        serialized = payload["source_links"]
        changes = [x["number"] for x in serialized if x["kind"] == "change"]
        issues = [x["number"] for x in serialized if x["kind"] == "issue"]

        # Three in total, newest pull requests first; with the budget spent on
        # PRs every issue folds into "+N", and the total still counts everything
        # so that pill stays honest.
        assert changes == [5, 4, 3]
        assert issues == []
        assert payload["source_links_total"] == 9

    def test_to_dict_scans_pr_source_links_once(self, monkeypatch):
        slot = _ChatSlot("s1")
        slot.append("user", "https://github.com/acme/widgets/pull/12")
        original = _ChatSlot._pr_source_links
        calls = 0

        def counted(self):
            nonlocal calls
            calls += 1
            return original(self)

        monkeypatch.setattr(_ChatSlot, "_pr_source_links", counted)
        payload = slot.to_dict()
        assert payload["source_links_total"] == 1
        assert calls == 1

    def test_source_links_carry_a_kind_discriminator(self):
        slot = _ChatSlot("s1")
        pr = "https://github.com/acme/widgets/pull/12"
        issue = "https://github.com/acme/widgets/issues/13"
        mr = "https://gitlab.com/acme/widgets/-/merge_requests/14"
        gitlab_issue = "https://gitlab.com/acme/widgets/-/issues/15"
        work_item = "https://gitlab.com/acme/widgets/-/work_items/16"
        slot.append("assistant", f"{pr} {issue} {mr} {gitlab_issue} {work_item}")

        payload = slot.to_dict()

        assert payload["source_links_total"] == 5
        # Order-insensitive on purpose: this pins the kind MAPPING, and the
        # recency ordering has its own tests below. Asserting a sequence here
        # made it a second, accidental ordering oracle.
        assert {link["url"]: link["kind"] for link in slot._pr_source_links()} == {
            pr: "change",
            issue: "issue",
            mr: "change",
            gitlab_issue: "issue",
            work_item: "issue",
        }
        assert all("kind" in link for link in payload["source_links"])

    def test_group_work_item_link_yields_no_chip(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", "epic https://gitlab.com/groups/acme/-/work_items/5")

        assert slot._pr_source_links() == []
        assert slot.to_dict()["source_links_total"] == 0

    def test_issue_links_never_inherit_a_chip_status(self, monkeypatch):
        """The chip-status cache is pull-request-only, so an issue entry must
        stay bare even when the cache would answer for its URL."""
        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr(
            state_module, "_cached_check_status", lambda _url: {"ci": "passed", "state": "open"}
        )
        slot = _ChatSlot("s1")
        pr = "https://github.com/acme/widgets/pull/12"
        issue = "https://github.com/acme/widgets/issues/13"
        slot.append("assistant", f"{pr} and {issue}")

        links = slot.to_dict(include_check_status=True)["source_links"]

        by_url = {link["url"]: link for link in links}
        assert by_url[pr]["ci"] == "passed"
        assert "ci" not in by_url[issue]
        assert "state" not in by_url[issue]

    def test_source_links_without_kind_are_treated_as_changes(self, monkeypatch):
        """Older cached payloads have no `kind`; they must keep their status."""
        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr(state_module, "_cached_check_status", lambda _url: {"ci": "failed"})
        slot = _ChatSlot("s1")
        url = "https://github.com/acme/widgets/pull/12"
        slot.append("assistant", url)
        slot._pr_source_links()
        # Simulate a pre-upgrade cache entry.
        key, links = slot._source_links_cache
        slot._source_links_cache = (key, [{"provider": "github", "number": 12, "url": url}])

        assert slot.to_dict(include_check_status=True)["source_links"][0]["ci"] == "failed"

    def test_gitlab_issue_link_requires_the_allowlist(self, monkeypatch):
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(sp, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(sp, "_gitlab_hosts_generation", 0)

        slot = _ChatSlot("s1")
        url = "https://gitlab.acme.internal/team/api/-/issues/7"
        slot.append("assistant", f"Filed {url}", ts="t1")
        assert slot.to_dict()["source_links_total"] == 0

        sp._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
        refreshed = slot.to_dict()
        assert refreshed["source_links_total"] == 1
        assert refreshed["source_links"][0]["kind"] == "issue"

    def test_pending_approval_flag(self):
        slot = _ChatSlot("s1")
        loop = asyncio.new_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut
        assert slot.to_dict()["pending_approval"] is True
        fut.set_result("approved")
        assert slot.to_dict()["pending_approval"] is False
        loop.close()

    def test_pending_subagent_failures_initialized_empty(self):
        slot = _ChatSlot("s1")
        assert slot._pending_subagent_failures == []

    def test_pending_subagent_failures_drain(self):
        slot = _ChatSlot("s1")
        slot._pending_subagent_failures.append(
            "[Subagent completion event]\nAgent `a1` ❌ timed out"
        )
        slot._pending_subagent_failures.append(
            "[Subagent completion event]\nAgent `a2` ❌ timed out"
        )
        # Simulate drain logic from _turn_build_prompt (chat_turn/prompt_assembly.py)
        failures = slot._pending_subagent_failures[:]
        slot._pending_subagent_failures.clear()
        message = "\n\n".join(failures) + "\n\n" + "user message"
        assert "[Subagent completion event]" in message
        assert "Agent `a1`" in message
        assert "Agent `a2`" in message
        assert message.endswith("user message")
        assert slot._pending_subagent_failures == []


# ── Multi-slot isolation ──


class TestMultiSlotIsolation:
    @pytest.mark.asyncio
    async def test_slots_have_independent_messages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        s1.append("user", "hello from s1")
        s2.append("user", "hello from s2")
        s2.append("assistant", "reply in s2")
        s1.drain()
        s2.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            r1 = await (await client.get("/api/chat/slots/s1")).json()
            r2 = await (await client.get("/api/chat/slots/s2")).json()
            assert r1["total"] == 1
            assert r2["total"] == 2
            assert r1["messages"][0]["content"] == "hello from s1"


# ── SSE broadcast: _has_reader mutual exclusion ──


class TestHasReaderFlag:
    """Verify _has_reader prevents duplicate message delivery."""

    def test_broadcast_skipped_when_reader_active(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = True
        slot.append("assistant", "should not broadcast")
        assert len(received) == 0

    def test_broadcast_fires_when_no_reader(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("assistant", "should broadcast")
        assert len(received) == 1
        assert received[0]["role"] == "assistant"

    def test_chunk_never_broadcast(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("chunk", "text")
        assert len(received) == 0

    def test_user_never_broadcast(self, tmp_path, monkeypatch):
        """User messages are added optimistically by frontend — no SSE broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("user", "hello")
        assert len(received) == 0

    def test_tool_and_permission_broadcast(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        received: list[dict] = []
        slot._on_message = lambda key, msg: received.append(msg)

        slot._has_reader = False
        slot.append("tool", "✅ bash")
        slot.append("permission", "run ls")
        assert len(received) == 2


# ── Chunk cleanup after response ──


class TestChunkCleanup:
    def test_chunks_removed_from_messages(self):
        """After assistant response, chunk messages should be cleaned up."""
        slot = _ChatSlot("s1")
        slot.append("user", "hello")
        slot.append("chunk", "He")
        slot.append("chunk", "llo")
        slot.append("chunk", " world")
        assert sum(1 for m in slot.messages if m["role"] == "chunk") == 3

        # Simulate what _run_chat does after streaming
        slot.messages = [m for m in slot.messages if m.get("role") != "chunk"]
        slot.append("assistant", "Hello world")
        assert sum(1 for m in slot.messages if m["role"] == "chunk") == 0
        assert slot.messages[-1]["role"] == "assistant"
        assert slot.messages[0]["role"] == "user"


class TestSlotsGetWarmsGitLabAllowlist:
    """GET /api/chat/slots warms the self-managed GitLab allowlist first.

    Slot source-link extraction is synchronous and cannot load the allowlist, so
    a cold direct GET (no WebSocket yet) would serialize against the empty
    snapshot and omit every configured self-hosted MR chip. The WS path has its
    own test; this pins the direct-fetch path, which is a separate entry point.
    """

    @pytest.mark.asyncio
    async def test_cold_get_returns_the_authorized_self_hosted_link(self, monkeypatch) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.chat_handlers import api_chat_slots
        from kiro_crew.dashboard.handlers import source_providers as sp

        url = "https://gitlab.acme.internal/team/api/-/merge_requests/7"
        order: list[str] = []

        # Cold snapshot: nothing has loaded the allowlist yet.
        monkeypatch.setattr(sp, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(sp, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(sp, "_gitlab_hosts_generation", 0)

        async def fake_ensure() -> frozenset:
            order.append("ensure")
            sp._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
            return frozenset({"gitlab.acme.internal"})

        monkeypatch.setattr(sp, "ensure_gitlab_hosts_loaded", fake_ensure)
        monkeypatch.setattr(sp, "schedule_check_refresh", lambda *a, **k: [])

        state = MagicMock()
        slot = _ChatSlot("s1")
        slot.append("assistant", f"Opened {url}", ts="t1")

        def serialize(**_kwargs):
            order.append("serialize")
            return [slot.to_dict()]

        state.serialize_slots.side_effect = serialize

        @web.middleware
        async def dashboard_auth_marker(request, handler):
            request["app"] = ""
            request["user"] = ""
            return await handler(request)

        app = web.Application(middlewares=[dashboard_auth_marker])
        app["state"] = state
        app.router.add_get("/api/chat/slots", api_chat_slots)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            assert resp.status == 200
            payloads = await resp.json()

        # Warm-up strictly precedes serialization, and the authorized MR is a link.
        assert order[:2] == ["ensure", "serialize"]
        links = [link["url"] for p in payloads for link in p.get("source_links", [])]
        assert url in links


class TestSourceLinkUrlsExcludeIssues:
    """The chip-status sweep reaches `gh pr view`, which has no meaning for an
    issue. Issue links must be filtered out before scheduling, not merely
    refused downstream, so a session that mentions only issues never generates
    provider work at all."""

    def test_state_sweep_skips_issue_links(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("source")
        pr = "https://github.com/acme/widgets/pull/12"
        slot.append("assistant", f"{pr} and https://github.com/acme/widgets/issues/13")

        assert state.source_link_urls() == [pr]
        assert state.source_link_urls_for_slot("source") == [pr]

    @pytest.mark.asyncio
    async def test_slots_get_schedules_only_change_links(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import source_providers as sp

        scheduler = MagicMock(return_value=[])
        monkeypatch.setattr(sp, "schedule_check_refresh", scheduler)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slot = state.get_or_create_slot("source")
        pr = "https://github.com/acme/widgets/pull/12"
        slot.append("assistant", f"{pr} and https://github.com/acme/widgets/issues/13")

        app = _make_app(state)

        @web.middleware
        async def owner_auth(request, handler):
            request["user"] = "U_OWNER"
            request["app"] = ""
            return await handler(request)

        app.middlewares.append(owner_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            assert resp.status == 200
            payload = await resp.json()

        # Both links are serialized for the sidebar...
        links = next(item for item in payload if item["key"] == "source")["source_links"]
        assert len(links) == 2
        # ...but only the pull request is scheduled for a status read.
        assert scheduler.call_args.args[0] == [pr]
